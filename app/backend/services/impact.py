from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..errors import ConflictError, NotFoundError
from ..models import Event, Operation, Order, OrderMaterial, Plan, PlanTask, User, Workspace
from ..scheduling import Scenario
from ..timeutil import epoch, from_epoch, iso, utcnow
from . import audit
from .scheduling import STRATEGY_LABELS, build_scenario, persist_plan, solve_scenario


def _latest_plan(db: Session, workspace: Workspace) -> Plan | None:
    if workspace.published_plan_id:
        plan = db.get(Plan, workspace.published_plan_id)
        if plan is not None and plan.workspace_id == workspace.id:
            return plan
    return db.scalar(
        select(Plan)
        .where(
            Plan.workspace_id == workspace.id,
            Plan.status.in_(("draft", "submitted", "approved", "published")),
        )
        .order_by(Plan.created_at.desc(), Plan.id.desc())
    )


def analyze_event(db: Session, workspace: Workspace, event: Event) -> dict:
    plan = _latest_plan(db, workspace)
    if plan is None:
        impact = {
            "plan_id": None,
            "affected_operation_ids": [],
            "affected_order_ids": [],
            "affected_resource_ids": [],
            "reasons": [{"code": "no_plan", "message": "当前工作空间没有可分析的排程计划"}],
            "analyzed_at": iso(utcnow()),
        }
        event.impact = impact
        event.impact_analyzed_at = utcnow()
        db.flush()
        return impact

    tasks = list(db.scalars(select(PlanTask).where(PlanTask.plan_id == plan.id)))
    task_by_operation = {item.operation_id: item for item in tasks}
    if not tasks:
        impact = {
            "plan_id": plan.id,
            "affected_operation_ids": [],
            "affected_order_ids": [],
            "affected_resource_ids": [],
            "reasons": [{"code": "empty_plan", "message": "计划没有任务"}],
            "analyzed_at": iso(utcnow()),
        }
        event.impact = impact
        event.impact_analyzed_at = utcnow()
        db.flush()
        return impact

    operations = {
        item.id: item
        for item in db.scalars(
            select(Operation).where(Operation.id.in_(list(task_by_operation.keys())))
        )
    }
    operations_by_order: dict[int, list[Operation]] = {}
    for operation in operations.values():
        operations_by_order.setdefault(operation.order_id, []).append(operation)
    for items in operations_by_order.values():
        items.sort(key=lambda item: item.sequence)

    event_start = epoch(event.occurred_at)
    event_end = event_start + (event.duration_seconds or 0)
    if event.event_type == "operation_overrun" and event.operation_id:
        task = task_by_operation.get(event.operation_id)
        if task is not None:
            event_end = max(event_end, epoch(task.end_at) + (event.duration_seconds or 0))

    affected_operations: set[int] = set()
    affected_resources: set[int] = set()
    reasons: list[dict] = []

    if event.event_type == "machine_failure" and event.resource_id:
        affected_resources.add(event.resource_id)
        for task in tasks:
            if task.resource_id != event.resource_id:
                continue
            task_start = epoch(task.start_at)
            task_end = epoch(task.end_at)
            if task_start <= event_end and task_end >= event_start:
                affected_operations.add(task.operation_id)
                reasons.append(
                    {
                        "code": "resource_conflict",
                        "operation_id": task.operation_id,
                        "resource_id": event.resource_id,
                    }
                )

    if event.event_type == "material_delay":
        material_id = (event.payload or {}).get("material_id")
        if material_id:
            order_ids = set(
                db.scalars(
                    select(OrderMaterial.order_id).where(OrderMaterial.material_id == material_id)
                )
            )
            for operation in operations.values():
                if operation.order_id in order_ids:
                    affected_operations.add(operation.id)
                    reasons.append(
                        {
                            "code": "material_delay",
                            "operation_id": operation.id,
                            "order_id": operation.order_id,
                            "material_id": material_id,
                        }
                    )

    if event.order_id:
        for operation in operations.values():
            if operation.order_id == event.order_id:
                affected_operations.add(operation.id)
                reasons.append(
                    {
                        "code": "order_event",
                        "operation_id": operation.id,
                        "order_id": event.order_id,
                    }
                )

    if event.event_type == "operation_overrun" and event.operation_id:
        if event.operation_id in task_by_operation:
            affected_operations.add(event.operation_id)
            reasons.append({"code": "operation_overrun", "operation_id": event.operation_id})

    if event.event_type == "new_urgent_order":
        target_order_id = event.order_id or (event.payload or {}).get("order_id")
        if target_order_id:
            for operation in operations.values():
                if operation.order_id == target_order_id:
                    affected_operations.add(operation.id)
                    reasons.append(
                        {
                            "code": "new_urgent_order",
                            "operation_id": operation.id,
                            "order_id": target_order_id,
                        }
                    )
        else:
            reasons.append(
                {
                    "code": "missing_order_reference",
                    "message": "紧急插单事件未指定 order_id，无法自动确定影响订单",
                }
            )

    if not affected_operations and event.resource_id:
        for task in tasks:
            if task.resource_id == event.resource_id:
                affected_operations.add(task.operation_id)
                affected_resources.add(task.resource_id)
                reasons.append(
                    {"code": "resource_fallback", "operation_id": task.operation_id}
                )

    if not affected_operations:
        reasons.append(
            {
                "code": "no_direct_conflict",
                "message": "事件未直接命中已排任务，需人工确认影响范围",
            }
        )

    # 订单内后继工序闭包。
    changed = True
    while changed:
        changed = False
        for order_operations in operations_by_order.values():
            affected_in_order = [item for item in order_operations if item.id in affected_operations]
            if not affected_in_order:
                continue
            latest_sequence = min(item.sequence for item in affected_in_order)
            for operation in order_operations:
                if operation.sequence > latest_sequence and operation.id not in affected_operations:
                    affected_operations.add(operation.id)
                    reasons.append(
                        {
                            "code": "downstream_precedence",
                            "operation_id": operation.id,
                            "after_operation_id": affected_in_order[0].id,
                        }
                    )
                    changed = True
        affected_resources.update(
            task_by_operation[item].resource_id
            for item in affected_operations
            if item in task_by_operation
        )

    # 资源竞争：受影响时间窗内，受影响资源上的其他任务。
    latest_end = max(
        (
            epoch(task_by_operation[item].end_at)
            for item in affected_operations
            if item in task_by_operation
        ),
        default=event_end,
    )
    latest_end = max(latest_end, event_end)
    for task in tasks:
        if task.resource_id not in affected_resources:
            continue
        if task.operation_id in affected_operations:
            continue
        task_start = epoch(task.start_at)
        if task_start <= latest_end:
            affected_operations.add(task.operation_id)
            reasons.append(
                {
                    "code": "resource_competition",
                    "operation_id": task.operation_id,
                    "resource_id": task.resource_id,
                }
            )

    # 资源竞争可能引入新的订单后继，再做一次闭包。
    for order_operations in operations_by_order.values():
        affected_in_order = [item for item in order_operations if item.id in affected_operations]
        if not affected_in_order:
            continue
        latest_sequence = min(item.sequence for item in affected_in_order)
        for operation in order_operations:
            if operation.sequence > latest_sequence:
                affected_operations.add(operation.id)

    affected_order_ids = sorted(
        {
            operations[operation_id].order_id
            for operation_id in affected_operations
            if operation_id in operations
        }
    )
    impact = {
        "plan_id": plan.id,
        "plan_status": plan.status,
        "event_window": {
            "start_at": iso(event.occurred_at),
            "end_at": iso(from_epoch(event_end)),
        },
        "affected_operation_ids": sorted(affected_operations),
        "affected_order_ids": affected_order_ids,
        "affected_resource_ids": sorted(affected_resources),
        "reasons": reasons,
        "analyzed_at": iso(utcnow()),
    }
    event.impact = impact
    event.impact_analyzed_at = utcnow()
    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=event.created_by,
        action="event.analyze",
        entity_type="event",
        entity_id=event.id,
        details={
            "plan_id": plan.id,
            "affected_operations": impact["affected_operation_ids"],
            "affected_orders": impact["affected_order_ids"],
        },
    )
    db.flush()
    return impact


def reschedule_from_event(
    db: Session,
    workspace: Workspace,
    event: Event,
    user: User,
    settings: Settings,
    *,
    strategy: str,
    running_task_policy: str | None,
    freeze_unaffected: bool,
    time_limit_seconds: float | None,
) -> dict:
    impact = event.impact or analyze_event(db, workspace, event)
    plan_id = impact.get("plan_id")
    if not plan_id:
        raise ConflictError("事件没有关联计划，无法重排")
    plan = db.get(Plan, plan_id)
    if plan is None or plan.workspace_id != workspace.id:
        raise NotFoundError("事件关联计划不存在")
    affected = set(impact.get("affected_operation_ids") or [])
    affected_tasks = [item for item in plan.tasks if item.operation_id in affected]
    running = [item for item in affected_tasks if item.execution_state == "running"]
    running_operation_ids = {item.operation_id for item in running}
    if running and running_task_policy is None:
        raise ConflictError(
            "事件影响运行中的任务，必须选择续跑或重排策略",
            {
                "code": "running_task_policy_required",
                "operation_ids": sorted(running_operation_ids),
                "options": ["continue", "replan"],
            },
        )

    def prepare(frozen: frozenset[int]) -> tuple[Scenario, dict]:
        built, snap = build_scenario(
            db,
            workspace,
            settings,
            base_plan_id=plan.id,
            freeze_operation_ids=frozen,
            freeze_policy="reschedule",
        )
        if running_task_policy == "replan" and running_operation_ids:
            replaced = []
            for operation in built.operations:
                if operation.operation_id in running_operation_ids:
                    replaced.append(
                        replace(
                            operation,
                            status="planned",
                            actual_start_epoch=None,
                            actual_end_epoch=None,
                            release_epoch=max(operation.release_epoch, epoch(event.occurred_at)),
                        )
                    )
                else:
                    replaced.append(operation)
            built = replace(built, operations=tuple(replaced))

        event_start = epoch(event.occurred_at)
        extra_seconds = max(0, event.duration_seconds or 0)
        already_applied=event.id in snap.get('active_event_ids',[])
        if not already_applied and event.event_type == "machine_failure" and event.resource_id and extra_seconds:
            built = replace(
                built,
                resources=tuple(
                    replace(
                        resource,
                        downtime_windows=resource.downtime_windows
                        + ((event_start, event_start + extra_seconds),),
                    )
                    if resource.resource_id == event.resource_id
                    else resource
                    for resource in built.resources
                ),
            )
        if not already_applied and event.event_type == "material_delay" and extra_seconds:
            delayed_order_ids = set(impact.get("affected_order_ids") or [])
            built = replace(
                built,
                operations=tuple(
                    replace(
                        operation,
                        release_epoch=max(operation.release_epoch, event_start + extra_seconds),
                    )
                    if operation.order_id in delayed_order_ids
                    else operation
                    for operation in built.operations
                ),
            )
        if not already_applied and event.event_type == "operation_overrun" and event.operation_id and extra_seconds:
            built = replace(
                built,
                operations=tuple(
                    replace(
                        operation,
                        candidates=tuple(
                            replace(candidate, duration_seconds=candidate.duration_seconds + extra_seconds)
                            for candidate in operation.candidates
                        ),
                    )
                    if operation.operation_id == event.operation_id
                    else operation
                    for operation in built.operations
                ),
            )
        return built, snap

    freeze_candidates = (
        frozenset(
            item.operation_id
            for item in plan.tasks
            if item.execution_state == "planned" and item.operation_id not in affected
        )
        if freeze_unaffected
        else frozenset()
    )
    attempts: list[tuple[str, frozenset[int]]] = []
    if freeze_candidates:
        attempts.append(("冻结未受影响任务", freeze_candidates))
    attempts.append(("全局重排", frozenset()))

    stages: list[dict] = []
    result = None
    snapshot = None
    for label, frozen in attempts:
        scenario, snapshot = prepare(frozen)
        candidate = solve_scenario(
            scenario,
            settings,
            strategy=strategy,
            time_limit_seconds=time_limit_seconds,
        )
        stages.append(
            {
                "label": label,
                "frozen_count": len(frozen),
                "status": candidate.status,
                "objective_value": candidate.objective_value,
                "wall_time_seconds": candidate.wall_time_seconds,
                "validation_valid": candidate.validation.get("valid", False),
            }
        )
        result = candidate
        if candidate.has_solution and candidate.validation.get("valid"):
            break

    if result is None or snapshot is None:
        raise ConflictError("重排没有返回结果")
    if not result.has_solution or not result.validation.get("valid"):
        return {
            "plan_id": None,
            "result": result.to_dict(include_tasks=False),
            "impact": impact,
            "stages": stages,
        }

    reschedule_meta = {
        "event_id": event.id,
        "impact": impact,
        "running_task_policy": running_task_policy,
        "freeze_unaffected": freeze_unaffected,
        "stages": stages,
    }
    new_plan = persist_plan(
        db,
        workspace,
        user,
        result,
        snapshot,
        name=f"事件重排 #{event.id} {STRATEGY_LABELS.get(strategy, strategy)}",
        base_plan_id=plan.id,
        reschedule_meta=reschedule_meta,
    )
    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="event.reschedule",
        entity_type="event",
        entity_id=event.id,
        details={
            "plan_id": new_plan.id,
            "base_plan_id": plan.id,
            "stages": stages,
            "running_task_policy": running_task_policy,
        },
    )
    db.flush()
    return {
        "plan_id": new_plan.id,
        "result": result.to_dict(include_tasks=False),
        "impact": impact,
        "stages": stages,
    }
