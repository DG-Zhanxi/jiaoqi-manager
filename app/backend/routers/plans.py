from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..dependencies import (
    WorkspaceContext,
    get_db,
    get_workspace_context,
    require_workspace_roles,
)
from ..errors import NotFoundError
from ..models import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    Plan,
    PlanTask,
)
from ..schemas import ExecutionComplete, ExecutionStart, ReviewDecision
from ..serializers import plan_out
from ..services import audit
from ..services.plans import (
    complete_task,
    get_plan,
    plan_diff,
    publish_plan,
    review_plan,
    start_task,
    submit_plan,
)

router = APIRouter(prefix="/api/workspaces/{workspace_id}/plans", tags=["plans"])


@router.get("")
def list_plans(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    conditions = [Plan.workspace_id == ctx.workspace.id]
    if status:
        conditions.append(Plan.status == status)
    rows = db.scalars(
        select(Plan)
        .where(*conditions)
        .order_by(Plan.created_at.desc(), Plan.id.desc())
        .limit(limit)
    )
    return {"items": [plan_out(item, include_tasks=False) for item in rows]}


@router.get("/{plan_id}")
def get_plan_detail(
    plan_id: int,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    plan = get_plan(db, ctx.workspace.id, plan_id)
    return plan_out(plan)


@router.post("/{plan_id}/submit")
def submit(
    plan_id: int,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    plan = get_plan(db, ctx.workspace.id, plan_id)
    submit_plan(db, ctx.workspace, plan, ctx.user, ctx.role)
    db.commit()
    db.refresh(plan)
    return plan_out(plan)


@router.post("/{plan_id}/review")
def review(
    plan_id: int,
    payload: ReviewDecision,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_REVIEWER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    plan = get_plan(db, ctx.workspace.id, plan_id)
    review_plan(
        db,
        ctx.workspace,
        plan,
        ctx.user,
        ctx.role,
        approved=payload.approved,
        comment=payload.comment,
    )
    db.commit()
    db.refresh(plan)
    return plan_out(plan)


@router.post("/{plan_id}/publish")
def publish(
    plan_id: int,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    plan = get_plan(db, ctx.workspace.id, plan_id)
    publish_plan(db, ctx.workspace, plan, ctx.user, ctx.role)
    db.commit()
    db.refresh(plan)
    return plan_out(plan)


@router.get("/{plan_id}/diff/{base_plan_id}")
def diff(
    plan_id: int,
    base_plan_id: int,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    plan = get_plan(db, ctx.workspace.id, plan_id)
    base = get_plan(db, ctx.workspace.id, base_plan_id)
    return plan_diff(plan, base)


def _get_task(db: Session, plan: Plan, task_id: int) -> PlanTask:
    task = db.get(PlanTask, task_id)
    if task is None or task.plan_id != plan.id:
        raise NotFoundError("计划任务不存在")
    return task


@router.post("/{plan_id}/tasks/{task_id}/start")
def start_execution(
    plan_id: int,
    task_id: int,
    payload: ExecutionStart,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_OPERATOR, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    plan = get_plan(db, ctx.workspace.id, plan_id)
    task = _get_task(db, plan, task_id)
    start_task(
        db,
        ctx.workspace,
        plan,
        task,
        ctx.user,
        ctx.role,
        actual_start_at=payload.actual_start_at,
    )
    db.commit()
    db.refresh(plan)
    return plan_out(plan)


@router.post("/{plan_id}/tasks/{task_id}/complete")
def complete_execution(
    plan_id: int,
    task_id: int,
    payload: ExecutionComplete,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_OPERATOR, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    plan = get_plan(db, ctx.workspace.id, plan_id)
    task = _get_task(db, plan, task_id)
    complete_task(
        db,
        ctx.workspace,
        plan,
        task,
        ctx.user,
        ctx.role,
        actual_end_at=payload.actual_end_at,
    )
    db.commit()
    db.refresh(plan)
    return plan_out(plan)