from __future__ import annotations

from sqlalchemy import select,update
from sqlalchemy.orm import Session

from ..errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from ..models import (
    Operation,
    Plan,
    PlanTask,
    User,
    Workspace,
)
from ..timeutil import ensure_utc, epoch, iso, utcnow
from . import audit


def get_plan(db: Session, workspace_id: int, plan_id: int) -> Plan:
    plan = db.get(Plan, plan_id)
    if plan is None or plan.workspace_id != workspace_id:
        raise NotFoundError("计划不存在")
    return plan


def submit_plan(
    db: Session,
    workspace: Workspace,
    plan: Plan,
    user: User,
    role: str,
    *,
    comment: str | None = None,
) -> Plan:
    if plan.status != "draft":
        raise ConflictError("只有草稿计划可以提交审批")
    if plan.created_by != user.id and role != "admin":
        raise ForbiddenError("只有计划创建者或工作空间管理员可以提交")
    if not plan.tasks:
        raise ConflictError("空计划不能提交")
    plan.status = "submitted"
    plan.submitted_at = utcnow()
    plan.review_comment = comment
    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="plan.submit",
        entity_type="plan",
        entity_id=plan.id,
        details={"comment": comment, "data_revision": plan.data_revision},
    )
    db.flush()
    return plan


def review_plan(
    db: Session,
    workspace: Workspace,
    plan: Plan,
    user: User,
    role: str,
    *,
    approved: bool,
    comment: str | None = None,
) -> Plan:
    if role not in {"reviewer", "admin"}:
        raise ForbiddenError("只有审核员或管理员可以审批计划")
    if plan.status != "submitted":
        raise ConflictError("只有已提交的计划可以审批")
    if plan.created_by == user.id:
        raise ForbiddenError("不能审批自己创建的计划")
    plan.status = "approved" if approved else "rejected"
    plan.reviewed_by = user.id
    plan.reviewed_at = utcnow()
    plan.review_comment = comment
    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="plan.approve" if approved else "plan.reject",
        entity_type="plan",
        entity_id=plan.id,
        details={
            "approved": approved,
            "comment": comment,
            "reviewer_id": user.id,
            "creator_id": plan.created_by,
        },
    )
    db.flush()
    return plan


def publish_plan(
    db: Session,
    workspace: Workspace,
    plan: Plan,
    user: User,
    role: str,
) -> Plan:
    if role not in {"planner", "admin"}:
        raise ForbiddenError("只有计划员或管理员可以发布计划")
    if plan.status != "approved":
        raise ConflictError("只有已批准的计划可以发布")
    if plan.data_revision != workspace.data_revision:
        raise ConflictError(
            "计划基于旧数据版本，已过期，请重新排程",
            {
                "code": "stale_plan",
                "plan_data_revision": plan.data_revision,
                "workspace_data_revision": workspace.data_revision,
            },
        )
    publication_base=(plan.model_snapshot or {}).get('published_base_plan_id',plan.base_plan_id)
    if publication_base != workspace.published_plan_id:
        raise ConflictError(
            "计划基准版本已变化，已过期，请重新排程",
            {
                "code": "stale_base_plan",
                "plan_base_plan_id": plan.base_plan_id,
                "current_published_plan_id": workspace.published_plan_id,
            },
        )
    if not plan.validation or not plan.validation.get("valid"):
        raise ConflictError("计划未通过独立校验，不能发布")
    if not plan.tasks:
        raise ConflictError("空计划不能发布")

    from ..config import Settings
    from ..services.scheduling import build_scenario
    from ..scheduling import ScheduledTask
    from ..scheduling.validator import validate
    from ..timeutil import from_epoch
    snap=plan.model_snapshot or {}
    clock=from_epoch(snap['horizon_start_epoch']+60) if snap.get('horizon_start_epoch') else None
    scenario,_=build_scenario(db,workspace,Settings.from_env(),base_plan_id=plan.base_plan_id,now=clock)
    restart_ids=set()
    if (plan.reschedule_meta or {}).get('running_task_policy')=='replan':
        from dataclasses import replace
        from ..models import Event
        event=db.get(Event,plan.reschedule_meta.get('event_id'))
        affected=set(plan.reschedule_meta.get('impact',{}).get('affected_operation_ids',[]))
        restart_ids={o.operation_id for o in scenario.operations if o.status=='running' and o.operation_id in affected}
        scenario=replace(scenario,operations=tuple(replace(o,status='planned',actual_start_epoch=None,actual_end_epoch=None,release_epoch=max(o.release_epoch,epoch(event.occurred_at))) if o.operation_id in restart_ids else o for o in scenario.operations))
    check=validate(scenario,[ScheduledTask(t.operation_id,t.resource_id,epoch(t.start_at),epoch(t.end_at),t.duration_seconds,t.is_fixed,t.execution_state) for t in plan.tasks])
    if not check['valid']:raise ConflictError('计划未通过发布前校验',{'errors':check['errors']})
    previous_id=workspace.published_plan_id
    condition=Workspace.published_plan_id.is_(None) if publication_base is None else Workspace.published_plan_id==publication_base
    result=db.execute(update(Workspace).where(Workspace.id==workspace.id,Workspace.data_revision==plan.data_revision,condition).values(published_plan_id=plan.id).execution_options(synchronize_session=False))
    if result.rowcount!=1:raise ConflictError('工作区版本发生变化，请重新检查计划')

    if previous_id and previous_id != plan.id:
        previous = db.get(Plan, previous_id)
        if previous is not None and previous.workspace_id == workspace.id:
            previous.status = "expired"
            audit.record(
                db,
                workspace_id=workspace.id,
                actor_id=user.id,
                action="plan.expire",
                entity_type="plan",
                entity_id=previous.id,
                details={"superseded_by": plan.id},
            )
    plan.status = "published"
    plan.published_by = user.id
    plan.published_at = utcnow()
    workspace.published_plan_id = plan.id
    workspace.updated_at = utcnow()
    if restart_ids:
        for op in db.scalars(select(Operation).where(Operation.id.in_(restart_ids))):
            audit.record(db,workspace_id=workspace.id,actor_id=user.id,action='task.restart',entity_type='operation',entity_id=op.id,details={'previous_actual_start':iso(op.actual_start_at),'plan_id':plan.id})
            op.status='planned';op.actual_start_at=None;op.actual_end_at=None
    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="plan.publish",
        entity_type="plan",
        entity_id=plan.id,
        details={
            "previous_published_plan_id": previous_id,
            "data_revision": workspace.data_revision,
            "base_plan_id": plan.base_plan_id,
        },
    )
    db.flush()
    return plan


def _execution_log(plan: Plan, entry: dict) -> None:
    snapshot = dict(plan.model_snapshot or {})
    entries = list(snapshot.get("execution_log") or [])
    entries.append(entry)
    snapshot["execution_log"] = entries
    plan.model_snapshot = snapshot


def start_task(
    db: Session,
    workspace: Workspace,
    plan: Plan,
    task: PlanTask,
    user: User,
    role: str,
    *,
    actual_start_at=None,
) -> PlanTask:
    if role not in {"operator", "admin"}:
        raise ForbiddenError("只有操作员或管理员可以报告开工")
    if plan.status != "published":
        raise ConflictError("只有已发布计划可以报告执行")
    if task.execution_state != "planned":
        raise ConflictError(f"任务状态 {task.execution_state} 不能开工")
    operation = db.get(Operation, task.operation_id)
    if operation is None:
        raise NotFoundError("任务关联工序不存在")
    order_operations = list(
        db.scalars(
            select(Operation)
            .where(Operation.order_id == operation.order_id)
            .order_by(Operation.sequence)
        )
    )
    for previous in order_operations:
        if previous.sequence >= operation.sequence:
            break
        if previous.status != "completed":
            raise ConflictError("前序工序尚未完成，不能开工")
    start_at = ensure_utc(actual_start_at) or utcnow()
    if task.actual_start_at is not None:
        start_at = ensure_utc(task.actual_start_at)
    task.execution_state = "running"
    task.actual_start_at = start_at
    operation.status = "running"
    operation.actual_start_at = start_at
    _execution_log(
        plan,
        {
            "action": "task.start",
            "task_id": task.id,
            "operation_id": task.operation_id,
            "actor_id": user.id,
            "at": iso(start_at),
        },
    )
    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="task.start",
        entity_type="plan_task",
        entity_id=task.id,
        details={"plan_id": plan.id, "operation_id": task.operation_id, "at": iso(start_at)},
    )
    audit.bump_revision(db,workspace)
    db.flush()
    return task


def complete_task(
    db: Session,
    workspace: Workspace,
    plan: Plan,
    task: PlanTask,
    user: User,
    role: str,
    *,
    actual_end_at=None,
) -> PlanTask:
    if role not in {"operator", "admin"}:
        raise ForbiddenError("只有操作员或管理员可以报告完工")
    if plan.status != "published":
        raise ConflictError("只有已发布计划可以报告执行")
    if task.execution_state == "completed":
        raise ConflictError("任务已经完工，不能回退或重复完成")
    if task.execution_state != "running":
        raise ConflictError("只有运行中的任务可以完工")
    end_at = ensure_utc(actual_end_at) or utcnow()
    if task.actual_start_at is not None and end_at < ensure_utc(task.actual_start_at):
        raise ValidationError("完工时间不能早于开工时间")
    task.execution_state = "completed"
    task.actual_end_at = end_at
    operation = db.get(Operation, task.operation_id)
    if operation is not None:
        operation.status = "completed"
        operation.actual_end_at = end_at
        if operation.actual_start_at is None:
            operation.actual_start_at = task.actual_start_at
    _execution_log(
        plan,
        {
            "action": "task.complete",
            "task_id": task.id,
            "operation_id": task.operation_id,
            "actor_id": user.id,
            "at": iso(end_at),
        },
    )
    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="task.complete",
        entity_type="plan_task",
        entity_id=task.id,
        details={"plan_id": plan.id, "operation_id": task.operation_id, "at": iso(end_at)},
    )
    audit.bump_revision(db,workspace)
    db.flush()
    return task


def plan_diff(plan: Plan, other: Plan) -> dict:
    current = {item.operation_id: item for item in plan.tasks}
    base = {item.operation_id: item for item in other.tasks}
    added = []
    removed = []
    moved = []
    resource_changed = []
    for operation_id, task in current.items():
        previous = base.get(operation_id)
        if previous is None:
            added.append(operation_id)
            continue
        delta = int(epoch(task.start_at) - epoch(previous.start_at))
        if delta:
            moved.append(
                {
                    "operation_id": operation_id,
                    "delta_seconds": delta,
                    "from": iso(previous.start_at),
                    "to": iso(task.start_at),
                }
            )
        if task.resource_id != previous.resource_id:
            resource_changed.append(
                {
                    "operation_id": operation_id,
                    "from_resource_id": previous.resource_id,
                    "to_resource_id": task.resource_id,
                }
            )
    for operation_id in base:
        if operation_id not in current:
            removed.append(operation_id)
    return {
        "plan_id": plan.id,
        "base_plan_id": other.id,
        "added_operations": added,
        "removed_operations": removed,
        "moved_operations": moved,
        "resource_changes": resource_changed,
        "changed_count": len(set(added) | set(removed) | {x['operation_id'] for x in moved} | {x['operation_id'] for x in resource_changed}),
        "current_metrics": (plan.validation or {}).get("metrics"),
        "base_metrics": (other.validation or {}).get("metrics"),
    }
