from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dependencies import (
    WorkspaceContext,
    get_db,
    get_settings,
    get_workspace_context,
    require_workspace_roles,
)
from ..errors import ConflictError, NotFoundError
from ..models import ROLE_ADMIN, ROLE_PLANNER, ScheduleJob
from ..schemas import CompareRequest, ScheduleCreate
from ..serializers import job_out
from ..services.scheduling import (
    build_scenario,
    compare_strategies,
    solve_scenario,
    start_schedule_job,
    persist_plan,
)
from ..config import Settings

router = APIRouter(prefix="/api/workspaces/{workspace_id}/schedule", tags=["schedule"])


@router.post("/jobs", status_code=202)
def create_job(
    payload: ScheduleCreate,
    request: Request,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if payload.base_plan_id is not None and payload.base_plan_id < 1:
        raise ConflictError("基准计划无效")
    job = start_schedule_job(
        db,
        request.app.state.db,
        settings,
        ctx.workspace,
        ctx.user,
        strategy=payload.strategy,
        time_limit_seconds=payload.time_limit_seconds,
        overtime_limit_seconds=payload.overtime_limit_seconds,
        persist_plan_flag=payload.persist_plan,
        base_plan_id=payload.base_plan_id,
        name=payload.name,
    )
    return job_out(job)


@router.get("/jobs")
def list_jobs(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
    limit: int = 50,
):
    limit = min(max(1, limit), 100)
    rows = db.scalars(
        select(ScheduleJob)
        .where(ScheduleJob.workspace_id == ctx.workspace.id)
        .order_by(ScheduleJob.created_at.desc(), ScheduleJob.id.desc())
        .limit(limit)
    )
    return {"items": [job_out(item) for item in rows]}


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    job = db.get(ScheduleJob, job_id)
    if job is None or job.workspace_id != ctx.workspace.id:
        raise NotFoundError("排程任务不存在")
    return job_out(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: int,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    job = db.get(ScheduleJob, job_id)
    if job is None or job.workspace_id != ctx.workspace.id:
        raise NotFoundError("排程任务不存在")
    if job.status in {"succeeded", "failed", "cancelled"}:
        raise ConflictError("任务已经结束，不能取消")
    job.cancel_requested = True
    job.message = "已请求取消，等待求解器停止"
    db.commit()
    db.refresh(job)
    return job_out(job)


@router.post("/compare")
def compare(
    payload: CompareRequest,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    import uuid
    scenario,snapshot=build_scenario(db,ctx.workspace,settings)
    revision=ctx.workspace.data_revision
    results=[];runs=[];signatures=set()
    for strategy in ('delivery_first','stability_first','overtime_limited'):
        solved=solve_scenario(scenario,settings,strategy=strategy,time_limit_seconds=payload.time_limit_seconds,overtime_limit_seconds=payload.overtime_limit_seconds)
        runs.append(solved.to_dict(include_tasks=False))
        if solved.has_solution and solved.validation.get('valid') and solved.task_signature() not in signatures:
            results.append(solved);signatures.add(solved.task_signature())
    db.refresh(ctx.workspace)
    if ctx.workspace.data_revision!=revision:raise ConflictError('计算期间数据已更新，请重新生成方案')
    group=uuid.uuid4().hex;candidates=[]
    for solved in results:
        plan=persist_plan(db,ctx.workspace,ctx.user,solved,snapshot,base_plan_id=snapshot.get('base_plan_id'),reschedule_meta={'comparison_group':group})
        candidate=solved.to_dict();candidate['plan_id']=plan.id;candidate['strategies']=[solved.strategy];candidates.append(candidate)
    result={'snapshot':snapshot,'candidates':candidates,'runs':runs,'comparison_group':group}
    db.commit()
    return result


@router.post("/solve")
def solve_now(
    payload: ScheduleCreate,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    scenario, snapshot = build_scenario(
        db,
        ctx.workspace,
        settings,
        base_plan_id=payload.base_plan_id,
    )
    result = solve_scenario(
        scenario,
        settings,
        strategy=payload.strategy,
        time_limit_seconds=payload.time_limit_seconds,
        overtime_limit_seconds=payload.overtime_limit_seconds,
    )
    db.commit()
    return {"persisted": False, "snapshot": snapshot, "result": result.to_dict(include_tasks=True)}
