from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import ConflictError, DomainError, NotFoundError, ValidationError
from ..models import (
    Downtime,
    Event,
    Dataset,
    OrderMaterial,
    Operation,
    Order,
    Plan,
    PlanTask,
    Resource,
    ScheduleJob,
    User,
    Workspace,
)
from ..scheduling import (
    BaseTask,
    Candidate,
    Operation as ScenarioOperation,
    Order as ScenarioOrder,
    Resource as ScenarioResource,
    Scenario,
    ScenarioError,
    SolveResult,
    solve,
)
from ..timeutil import epoch, ensure_utc, from_epoch, iso, utcnow
from . import audit
from .materials import allocate_materials

UTC = timezone.utc
STRATEGY_LABELS = {
    "delivery_first": "交期优先",
    "stability_first": "稳定性优先",
    "overtime_limited": "限时排产",
}


@dataclass(frozen=True)
class ProposedCandidate:
    resource_id: int
    duration_seconds: int


@dataclass(frozen=True)
class ProposedOperation:
    operation_id: int
    sequence: int
    name: str
    setup_seconds: int
    candidates: tuple[ProposedCandidate, ...]


@dataclass(frozen=True)
class ProposedOrder:
    order_id: int
    order_no: str
    product_name: str
    quantity: int
    release_epoch: int
    due_epoch: int | None
    priority: int
    operations: tuple[ProposedOperation, ...]


def proposed_order_from_schema(order_in, *, now: datetime | None = None) -> ProposedOrder:
    operations: list[ProposedOperation] = []
    for index, operation in enumerate(
        sorted(order_in.operations, key=lambda item: item.sequence), start=1
    ):
        operations.append(
            ProposedOperation(
                operation_id=-index,
                sequence=operation.sequence,
                name=operation.name,
                setup_seconds=operation.setup_seconds,
                candidates=tuple(
                    ProposedCandidate(
                        resource_id=item.resource_id,
                        duration_seconds=item.duration_seconds,
                    )
                    for item in operation.candidates
                ),
            )
        )
    return ProposedOrder(
        order_id=-1,
        order_no=order_in.order_no,
        product_name=order_in.product_name,
        quantity=order_in.quantity,
        release_epoch=epoch(order_in.release_at),
        due_epoch=epoch(order_in.due_at) if order_in.due_at else None,
        priority=order_in.priority,
        operations=tuple(operations),
    )


def _merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not windows:
        return []
    windows.sort()
    merged = [windows[0]]
    for left, right in windows[1:]:
        last_left, last_right = merged[-1]
        if left <= last_right:
            merged[-1] = (last_left, max(last_right, right))
        else:
            merged.append((left, right))
    return merged


def calendar_windows(
    calendar: Any,
    start_epoch: int,
    end_epoch: int,
) -> tuple[bool, tuple[tuple[int, int], ...]]:
    if calendar is None:
        return False, ()
    if not isinstance(calendar, list):
        return True, ()
    shifts = [item for item in calendar if isinstance(item, dict)]
    if not shifts:
        return True, ()
    window_list: list[tuple[int, int]] = []
    shop_timezone=timezone(timedelta(hours=8))
    first_day = datetime.fromtimestamp(start_epoch, shop_timezone).date() - timedelta(days=1)
    last_day = datetime.fromtimestamp(end_epoch, shop_timezone).date() + timedelta(days=1)
    current = first_day
    while current <= last_day:
        day_start = int(
            datetime.combine(current, dtime.min, tzinfo=shop_timezone).timestamp()
        )
        for shift in shifts:
            try:
                weekday = int(shift.get("weekday"))
                start_minute = int(shift.get("start_minute"))
                end_minute = int(shift.get("end_minute"))
            except (TypeError, ValueError):
                continue
            if weekday != current.weekday():
                continue
            left = day_start + start_minute * 60
            right = day_start + end_minute * 60
            left = max(left, start_epoch)
            right = min(right, end_epoch)
            if right > left:
                window_list.append((left, right))
        current += timedelta(days=1)
    return True, tuple(_merge_windows(window_list))


def resolve_base_plan(
    db: Session,
    workspace: Workspace,
    base_plan_id: int | None = None,
) -> Plan | None:
    if base_plan_id is not None:
        plan = db.get(Plan, base_plan_id)
        if plan is None or plan.workspace_id != workspace.id:
            raise NotFoundError("基准计划不存在")
        return plan
    if workspace.published_plan_id:
        plan = db.get(Plan, workspace.published_plan_id)
        if plan is not None and plan.workspace_id == workspace.id:
            return plan
    return None


def _base_tasks(plan: Plan | None) -> dict[int, BaseTask]:
    if plan is None:
        return {}
    tasks: dict[int, BaseTask] = {}
    for task in plan.tasks:
        start_at = task.start_at
        end_at = task.end_at
        if task.execution_state in {"running", "completed"} and task.actual_start_at is not None:
            start_at = task.actual_start_at
        if task.execution_state == "completed" and task.actual_end_at is not None:
            end_at = task.actual_end_at
        tasks[task.operation_id] = BaseTask(
            operation_id=task.operation_id,
            resource_id=task.resource_id,
            start_epoch=epoch(start_at),
            end_epoch=epoch(end_at),
            execution_state=task.execution_state,
        )
    return tasks


def _shift_start(calendar: Any, start_epoch: int) -> int:
    _defined, windows = calendar_windows(calendar, start_epoch, start_epoch + 86400 * 2)
    if windows:
        return windows[0][0]
    return start_epoch


def build_scenario(
    db: Session,
    workspace: Workspace,
    settings: Settings,
    *,
    base_plan_id: int | None = None,
    freeze_operation_ids: frozenset[int] = frozenset(),
    freeze_policy: str = "none",
    extra_order: ProposedOrder | None = None,
    now: datetime | None = None,
) -> tuple[Scenario, dict]:
    allocate_materials(db, workspace.id)

    orders = list(
        db.scalars(
            select(Order).where(
                Order.workspace_id == workspace.id,
                Order.status == "confirmed",
            )
        )
    )
    if not orders and extra_order is None:
        raise ValidationError("当前工作空间没有已确认订单，无法排程")

    resources = list(
        db.scalars(
            select(Resource).where(
                Resource.workspace_id == workspace.id,
                Resource.is_active.is_(True),
            )
        )
    )
    if not resources:
        raise ValidationError("当前工作空间没有可用资源，无法排程")
    resource_ids = {item.id: item for item in resources}
    downtimes = list(
        db.scalars(
            select(Downtime).where(Downtime.resource_id.in_(list(resource_ids.keys())))
        )
    )
    downtime_by_resource: dict[int, list[tuple[int, int]]] = {}
    for item in downtimes:
        downtime_by_resource.setdefault(item.resource_id, []).append(
            (epoch(item.start_at), epoch(item.end_at))
        )

    events=list(db.scalars(select(Event).where(Event.workspace_id==workspace.id)))
    delayed_orders:dict[int,int]={};overruns:dict[int,int]={};urgent_orders:set[int]=set()
    active_event_ids=[]
    for event in events:
        if (event.payload or {}).get('cancelled'):continue
        active_event_ids.append(event.id)
        end=epoch(event.occurred_at)+max(0,event.duration_seconds or 0)
        if event.event_type=='machine_failure' and event.resource_id:
            downtime_by_resource.setdefault(event.resource_id,[]).append((epoch(event.occurred_at),end))
        elif event.event_type=='material_delay':
            targets=[event.order_id] if event.order_id else []
            material_id=(event.payload or {}).get('material_id')
            if material_id:
                targets.extend(db.scalars(select(OrderMaterial.order_id).join(Order,Order.id==OrderMaterial.order_id).where(Order.workspace_id==workspace.id,OrderMaterial.material_id==material_id)))
            for oid in targets:delayed_orders[oid]=max(delayed_orders.get(oid,0),end)
        elif event.event_type=='operation_overrun' and event.operation_id:
            overruns[event.operation_id]=overruns.get(event.operation_id,0)+max(0,event.duration_seconds or 0)
        elif event.event_type=='new_urgent_order' and event.order_id:urgent_orders.add(event.order_id)

    base_plan = resolve_base_plan(db, workspace, base_plan_id)
    base_map = _base_tasks(base_plan)

    now_value = ensure_utc(now) or utcnow()
    if now is None and orders and all(order.dataset_id for order in orders):
        source_types=set(db.scalars(select(Dataset.source_type).where(Dataset.id.in_({o.dataset_id for o in orders}))))
        if source_types=={'packaging'}:
            now_value=min(ensure_utc(o.release_at) for o in orders)
    horizon_seed = epoch(now_value)
    releases: list[int] = []
    dues: list[int] = []
    actual_starts: list[int] = []
    actual_ends: list[int] = []
    fixed_starts: list[int] = []
    operations: list[ScenarioOperation] = []
    scenario_orders: list[ScenarioOrder] = []
    release_by_order: dict[int, int] = {}

    def add_db_order(order: Order) -> None:
        release = epoch(order.release_at)
        release=max(release,delayed_orders.get(order.id,0))
        effective_priority=max(order.priority,10) if order.id in urgent_orders else order.priority
        if order.material_ready_at is not None:
            release = max(release, epoch(order.material_ready_at))
        due = epoch(order.due_at) if order.due_at is not None else None
        releases.append(release)
        if due is not None:
            dues.append(due)
        release_by_order[order.id] = release
        scenario_orders.append(
            ScenarioOrder(
                order_id=order.id,
                order_no=order.order_no,
                release_epoch=release,
                due_epoch=due,
                priority=effective_priority,
                weight=max(1, effective_priority + 1),
            )
        )
        for model_operation in sorted(order.operations, key=lambda item: item.sequence):
            base = base_map.get(model_operation.id)
            status = model_operation.status
            actual_start = (
                epoch(model_operation.actual_start_at)
                if model_operation.actual_start_at is not None
                else None
            )
            actual_end = (
                epoch(model_operation.actual_end_at)
                if model_operation.actual_end_at is not None
                else None
            )
            if base is not None and status == "planned":
                if base.execution_state == "completed":
                    status = "completed"
                    actual_start = base.start_epoch
                    actual_end = base.end_epoch
                elif base.execution_state == "running":
                    status = "running"
                    actual_start = base.start_epoch
            if actual_start is not None:
                actual_starts.append(actual_start)
            if actual_end is not None:
                actual_ends.append(actual_end)
            if model_operation.fixed_start_at is not None:
                fixed_starts.append(epoch(model_operation.fixed_start_at))
            candidates: list[Candidate] = []
            for candidate_model in model_operation.candidates:
                resource = resource_ids.get(candidate_model.resource_id)
                if resource is None:
                    raise ValidationError(
                        "订单引用了不可用资源，无法生成承诺计划",
                        {
                            "order_no": order.order_no,
                            "operation_id": model_operation.id,
                            "resource_id": candidate_model.resource_id,
                            "code": "ineligible_resource",
                        },
                    )
                candidates.append(
                    Candidate(
                        resource_id=candidate_model.resource_id,
                        duration_seconds=candidate_model.duration_seconds+overruns.get(model_operation.id,0),
                    )
                )
            if not candidates:
                raise ValidationError(
                    "订单工序缺少候选机器，无法生成承诺计划",
                    {
                        "order_no": order.order_no,
                        "operation_id": model_operation.id,
                        "code": "missing_candidates",
                    },
                )
            operations.append(
                ScenarioOperation(
                    operation_id=model_operation.id,
                    order_id=order.id,
                    order_no=order.order_no,
                    name=model_operation.name,
                    sequence=model_operation.sequence,
                    setup_seconds=model_operation.setup_seconds,
                    release_epoch=release,
                    due_epoch=due,
                    priority=effective_priority,
                    weight=max(1, effective_priority + 1),
                    candidates=tuple(candidates),
                    fixed_start_epoch=(
                        epoch(model_operation.fixed_start_at)
                        if model_operation.fixed_start_at is not None
                        else None
                    ),
                    fixed_resource_id=model_operation.fixed_resource_id,
                    status=status,
                    actual_start_epoch=actual_start,
                    actual_end_epoch=actual_end,
                )
            )

    for order in orders:
        add_db_order(order)

    if extra_order is not None:
        releases.append(extra_order.release_epoch)
        if extra_order.due_epoch is not None:
            dues.append(extra_order.due_epoch)
        release_by_order[extra_order.order_id] = extra_order.release_epoch
        scenario_orders.append(
            ScenarioOrder(
                order_id=extra_order.order_id,
                order_no=extra_order.order_no,
                release_epoch=extra_order.release_epoch,
                due_epoch=extra_order.due_epoch,
                priority=extra_order.priority,
                weight=max(1, extra_order.priority + 1),
            )
        )
        for operation in extra_order.operations:
            if not operation.candidates:
                raise ValidationError(f"预览订单工序 {operation.sequence} 缺少候选机器")
            for candidate in operation.candidates:
                if candidate.resource_id not in resource_ids:
                    raise ValidationError(
                        "预览订单引用了当前工作空间不可用的资源",
                        {
                            "resource_id": candidate.resource_id,
                            "code": "ineligible_resource",
                        },
                    )
                if candidate.duration_seconds <= 0:
                    raise ValidationError("预览订单工序时长必须为正")
            operations.append(
                ScenarioOperation(
                    operation_id=operation.operation_id,
                    order_id=extra_order.order_id,
                    order_no=extra_order.order_no,
                    name=operation.name,
                    sequence=operation.sequence,
                    setup_seconds=operation.setup_seconds,
                    release_epoch=extra_order.release_epoch,
                    due_epoch=extra_order.due_epoch,
                    priority=extra_order.priority,
                    weight=max(1, extra_order.priority + 1),
                    candidates=tuple(
                        Candidate(
                            resource_id=item.resource_id,
                            duration_seconds=item.duration_seconds,
                        )
                        for item in operation.candidates
                    ),
                    status="planned",
                )
            )

    if not operations:
        raise ValidationError("没有可调度工序")

    horizon_start = min([horizon_seed] + actual_starts + fixed_starts + [
        item.start_epoch for item in base_map.values()
    ])
    horizon_start -= 60
    minimum_span = 0
    maximum_finish = horizon_seed
    for operation in operations:
        minimum_span += min(
            item.duration_seconds + operation.setup_seconds for item in operation.candidates
        )
        if operation.actual_end_epoch is not None:
            maximum_finish = max(maximum_finish, operation.actual_end_epoch)
        if operation.fixed_start_epoch is not None:
            maximum_finish = max(
                maximum_finish,
                operation.fixed_start_epoch + operation.setup_seconds + 86400,
            )
    for base in base_map.values():
        maximum_finish = max(maximum_finish, base.end_epoch)

    horizon_end = max(
        horizon_seed + settings.plan_horizon_days * 86400,
        (max(releases) if releases else horizon_seed) + 7 * 86400,
        (max(dues) + 86400) if dues else horizon_seed,
        horizon_start + minimum_span + 86400,
        maximum_finish + 86400,
    )

    scenario_resources: list[ScenarioResource] = []
    for resource in resources:
        calendar_defined, windows = calendar_windows(
            resource.calendar, horizon_start, horizon_end
        )
        scenario_resources.append(
            ScenarioResource(
                resource_id=resource.id,
                code=resource.code,
                downtime_windows=tuple(downtime_by_resource.get(resource.id, [])),
                availability_windows=windows,
                calendar_defined=calendar_defined,
                is_active=resource.is_active,
            )
        )

    scenario = Scenario(
        operations=tuple(sorted(operations, key=lambda item: (item.order_id, item.sequence))),
        resources=tuple(scenario_resources),
        orders=tuple(scenario_orders),
        horizon_start_epoch=horizon_start,
        horizon_end_epoch=horizon_end,
        base_tasks=tuple(base_map.values()),
        frozen_operation_ids=frozenset(freeze_operation_ids),
        freeze_policy=freeze_policy,
    )
    snapshot = {
        "active_event_ids": active_event_ids,
        "published_base_plan_id": workspace.published_plan_id,
        "generated_at": iso(now_value),
        "horizon_start_epoch": horizon_start,
        "horizon_end_epoch": horizon_end,
        "order_count": len(scenario_orders),
        "operation_count": len(operations),
        "resource_count": len(resources),
        "base_plan_id": base_plan.id if base_plan else None,
        "base_plan_status": base_plan.status if base_plan else None,
        "frozen_operation_ids": sorted(freeze_operation_ids),
        "freeze_policy": freeze_policy,
        "release_by_order": {str(key): value for key, value in release_by_order.items()},
        "setup_policy": "任务时长 = 候选加工时长 + 工序 Setup Time；换型序列依赖未建模",
        "duration_source": "工序候选机器加工时长均为持久化源数据/订单输入的实际秒数",
    }
    return scenario, snapshot


def solve_scenario(
    scenario: Scenario,
    settings: Settings,
    *,
    strategy: str,
    time_limit_seconds: float | None = None,
    overtime_limit_seconds: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SolveResult:
    limit = time_limit_seconds or settings.default_solver_time_seconds
    limit = min(max(0.05, float(limit)), settings.max_solver_time_seconds)
    overtime_deadline = (
        scenario.horizon_start_epoch + overtime_limit_seconds
        if overtime_limit_seconds is not None
        else None
    )
    return solve(
        scenario,
        strategy=strategy,
        time_limit_seconds=limit,
        overtime_limit_seconds=overtime_deadline,
        workers=settings.solver_workers,
        should_cancel=should_cancel,
    )


def persist_plan(
    db: Session,
    workspace: Workspace,
    user: User,
    result: SolveResult,
    snapshot: dict,
    *,
    name: str | None = None,
    base_plan_id: int | None = None,
    reschedule_meta: dict | None = None,
) -> Plan:
    if not result.has_solution or not result.validation.get("valid"):
        raise ConflictError("求解结果没有通过独立校验，不能保存为计划")
    if result.status not in {"OPTIMAL", "FEASIBLE"}:
        raise ConflictError(f"求解器状态 {result.status} 不能保存为计划")

    timestamp = utcnow().astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    plan = Plan(
        workspace_id=workspace.id,
        name=name or f"{STRATEGY_LABELS.get(result.strategy, result.strategy)} {timestamp}",
        status="draft",
        strategy=result.strategy,
        created_by=user.id,
        data_revision=workspace.data_revision,
        base_plan_id=base_plan_id,
        solver_status=result.status,
        objective_value=result.objective_value,
        best_bound=result.best_bound,
        solve_seconds=result.wall_time_seconds,
        model_snapshot={**snapshot,"published_base_plan_id":workspace.published_plan_id},
        validation=result.validation,
        reschedule_meta=reschedule_meta or {},
    )
    db.add(plan)
    db.flush()

    operation_ids = [item.operation_id for item in result.tasks]
    db_operations = {
        item.id: item
        for item in db.scalars(
            select(Operation).where(Operation.id.in_(operation_ids))
        )
    } if operation_ids else {}
    for task in result.tasks:
        operation = db_operations.get(task.operation_id)
        if operation is None:
            raise ConflictError(f"计划引用未知工序 {task.operation_id}")
        actual_start = None
        actual_end = None
        execution_state = task.execution_state
        if execution_state == "completed":
            actual_start = from_epoch(task.start_epoch)
            actual_end = from_epoch(task.end_epoch)
        elif execution_state == "running":
            actual_start = (
                ensure_utc(operation.actual_start_at)
                if operation.actual_start_at is not None
                else from_epoch(task.start_epoch)
            )
        db.add(
            PlanTask(
                plan_id=plan.id,
                operation_id=task.operation_id,
                resource_id=task.resource_id,
                start_at=from_epoch(task.start_epoch),
                end_at=from_epoch(task.end_epoch),
                duration_seconds=task.duration_seconds,
                is_fixed=task.is_fixed,
                execution_state=execution_state,
                actual_start_at=actual_start,
                actual_end_at=actual_end,
            )
        )
    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="plan.create",
        entity_type="plan",
        entity_id=plan.id,
        details={
            "strategy": result.strategy,
            "solver_status": result.status,
            "objective_value": result.objective_value,
            "task_count": len(result.tasks),
            "data_revision": workspace.data_revision,
            "base_plan_id": base_plan_id,
        },
    )
    db.flush()
    db.refresh(plan)
    return plan


def compare_strategies(
    db: Session,
    workspace: Workspace,
    settings: Settings,
    *,
    time_limit_seconds: float | None = None,
    overtime_limit_seconds: int | None = None,
) -> dict:
    scenario, snapshot = build_scenario(db, workspace, settings)
    runs: list[dict] = []
    candidates: list[dict] = []
    by_signature: dict[tuple, dict] = {}
    for strategy in ("delivery_first", "stability_first", "overtime_limited"):
        result = solve_scenario(
            scenario,
            settings,
            strategy=strategy,
            time_limit_seconds=time_limit_seconds,
            overtime_limit_seconds=overtime_limit_seconds,
        )
        runs.append(result.to_dict(include_tasks=False))
        if not result.has_solution or not result.validation.get("valid"):
            continue
        signature = result.task_signature()
        existing = by_signature.get(signature)
        if existing is not None:
            existing["strategies"].append(strategy)
            continue
        item = result.to_dict(include_tasks=True)
        item["strategies"] = [strategy]
        by_signature[signature] = item
        candidates.append(item)
    return {"snapshot": snapshot, "candidates": candidates, "runs": runs}


def start_schedule_job(
    db: Session,
    database,
    settings: Settings,
    workspace: Workspace,
    user: User,
    *,
    strategy: str,
    time_limit_seconds: float | None,
    overtime_limit_seconds: int | None = None,
    persist_plan_flag: bool = True,
    base_plan_id: int | None = None,
    name: str | None = None,
    kind: str = "schedule",
) -> ScheduleJob:
    if base_plan_id is not None:
        resolve_base_plan(db, workspace, base_plan_id)
    limit = time_limit_seconds or settings.default_solver_time_seconds
    limit = min(max(0.05, float(limit)), settings.max_solver_time_seconds)
    job = ScheduleJob(
        workspace_id=workspace.id,
        kind=kind,
        status="queued",
        progress=0,
        message="排队中",
        strategy=strategy,
        time_limit_seconds=limit,
        persist_plan=persist_plan_flag,
        base_plan_id=base_plan_id,
        requested_by=user.id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    thread = __import__("threading").Thread(
        target=run_schedule_job,
        args=(
            database,
            settings,
            job.id,
            strategy,
            limit,
            overtime_limit_seconds,
            persist_plan_flag,
            base_plan_id,
            name,
            kind,
        ),
        daemon=True,
        name=f"schedule-job-{job.id}",
    )
    thread.start()
    return job


def _set_job(
    session: Session,
    job: ScheduleJob,
    *,
    status: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    solver_status: str | None = None,
    plan_id: int | None = None,
    error: str | None = None,
    result: dict | None = None,
) -> None:
    if status is not None:
        job.status = status
    if progress is not None:
        job.progress = progress
    if message is not None:
        job.message = message
    if solver_status is not None:
        job.solver_status = solver_status
    if plan_id is not None:
        job.plan_id = plan_id
    if error is not None:
        job.error = error
    if result is not None:
        job.result = result
    session.commit()


def run_schedule_job(
    database,
    settings: Settings,
    job_id: int,
    strategy: str,
    time_limit_seconds: float,
    overtime_limit_seconds: int | None,
    persist: bool,
    base_plan_id: int | None,
    name: str | None,
    kind: str,
) -> None:
    session: Session = database.session()
    job: ScheduleJob | None = None
    try:
        job = session.get(ScheduleJob, job_id)
        if job is None:
            return
        _set_job(session, job, status="running", progress=5, message="构建调度场景")
        workspace = session.get(Workspace, job.workspace_id)
        user = session.get(User, job.requested_by)
        if workspace is None or user is None:
            _set_job(
                session,
                job,
                status="failed",
                progress=100,
                message="任务数据缺失",
                error="工作空间或用户不存在",
            )
            return

        def should_cancel() -> bool:
            session.refresh(job)
            return bool(job.cancel_requested)

        if should_cancel():
            _set_job(session, job, status="cancelled", progress=0, message="已取消")
            return

        scenario, snapshot = build_scenario(
            session,
            workspace,
            settings,
            base_plan_id=base_plan_id,
            freeze_policy=kind,
        )
        session.commit()
        if should_cancel():
            _set_job(session, job, status="cancelled", progress=10, message="已取消")
            return
        _set_job(session, job, progress=40, message="CP-SAT 求解中")

        result = solve_scenario(
            scenario,
            settings,
            strategy=strategy,
            time_limit_seconds=time_limit_seconds,
            overtime_limit_seconds=overtime_limit_seconds,
            should_cancel=should_cancel,
        )
        if result.status == "CANCELLED" or should_cancel():
            _set_job(session, job, status="cancelled", progress=90, message="已取消")
            return

        plan_id = None
        can_persist = (
            persist
            and result.has_solution
            and result.validation.get("valid")
            and result.status in {"OPTIMAL", "FEASIBLE"}
        )
        if can_persist:
            _set_job(session, job, progress=90, message="保存计划与审计记录")
            plan = persist_plan(
                session,
                workspace,
                user,
                result,
                snapshot,
                name=name,
                base_plan_id=base_plan_id,
            )
            plan_id = plan.id
            session.commit()

        payload = result.to_dict(include_tasks=False)
        payload["snapshot"] = snapshot
        payload["plan_id"] = plan_id
        if can_persist or (result.has_solution and not persist):
            _set_job(
                session,
                job,
                status="succeeded",
                progress=100,
                message=result.message,
                solver_status=result.status,
                plan_id=plan_id,
                result=payload,
            )
        else:
            _set_job(
                session,
                job,
                status="failed",
                progress=100,
                message=result.message or f"求解器状态 {result.status}",
                solver_status=result.status,
                plan_id=None,
                error=result.message or f"求解器状态 {result.status}",
                result=payload,
            )
    except DomainError as exc:
        session.rollback()
        if job is None:
            job = session.get(ScheduleJob, job_id)
        if job is not None:
            _set_job(
                session,
                job,
                status="failed",
                progress=100,
                message=exc.message,
                error=exc.message,
            )
    except Exception as exc:  # pragma: no cover - 防止后台线程静默退出
        session.rollback()
        if job is None:
            job = session.get(ScheduleJob, job_id)
        if job is not None:
            _set_job(
                session,
                job,
                status="failed",
                progress=100,
                message="调度任务执行失败",
                error=f"{type(exc).__name__}",
            )
    finally:
        session.close()
