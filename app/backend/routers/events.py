from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..dependencies import (
    WorkspaceContext,
    get_db,
    get_settings,
    get_workspace_context,
    require_workspace_roles,
)
from ..errors import NotFoundError, ValidationError
from ..models import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_PLANNER,
    Event,
    Operation,
    Order,
    Resource,
)
from ..schemas import EventCreate, RescheduleRequest
from ..serializers import event_out, plan_out
from ..services import audit
from ..services.impact import analyze_event, reschedule_from_event
from ..timeutil import ensure_utc, utcnow

router = APIRouter(prefix="/api/workspaces/{workspace_id}/events", tags=["events"])


def _get_event(db: Session, workspace_id: int, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None or event.workspace_id != workspace_id:
        raise NotFoundError("事件不存在")
    return event


@router.get("")
def list_events(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
    event_type: str | None = None,
    limit: int = 100,
):
    limit = min(max(1, limit), 300)
    conditions = [Event.workspace_id == ctx.workspace.id]
    if event_type:
        conditions.append(Event.event_type == event_type)
    rows = db.scalars(
        select(Event)
        .where(*conditions)
        .order_by(Event.occurred_at.desc(), Event.id.desc())
        .limit(limit)
    )
    return {"items": [event_out(item) for item in rows]}


@router.post("", status_code=201)
def create_event(
    payload: EventCreate,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_OPERATOR, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if payload.resource_id is not None:
        resource = db.get(Resource, payload.resource_id)
        if resource is None or resource.workspace_id != ctx.workspace.id:
            raise ValidationError("事件引用的资源不属于当前工作空间")
    if payload.order_id is not None:
        order = db.get(Order, payload.order_id)
        if order is None or order.workspace_id != ctx.workspace.id:
            raise ValidationError("事件引用的订单不属于当前工作空间")
    if payload.operation_id is not None:
        operation = db.get(Operation, payload.operation_id)
        order = db.get(Order, operation.order_id) if operation is not None else None
        if order is None or order.workspace_id != ctx.workspace.id:
            raise ValidationError("事件引用的工序不属于当前工作空间")
    if payload.event_type == "machine_failure" and payload.resource_id is None:
        raise ValidationError("机器故障事件必须指定 resource_id")
    if payload.event_type == "new_urgent_order" and payload.order_id is None:
        raise ValidationError("紧急插单事件必须指定 order_id")
    if payload.event_type == "material_delay":
        material_id = (payload.payload or {}).get("material_id")
        if material_id is None and payload.order_id is None:
            raise ValidationError("物料延迟事件需要 payload.material_id 或 order_id")

    occurred = ensure_utc(payload.occurred_at)
    known = ensure_utc(payload.known_at) or occurred
    event = Event(
        workspace_id=ctx.workspace.id,
        event_type=payload.event_type,
        title=payload.title.strip(),
        occurred_at=occurred,
        known_at=known,
        duration_seconds=payload.duration_seconds,
        resource_id=payload.resource_id,
        order_id=payload.order_id,
        operation_id=payload.operation_id,
        payload=payload.payload or {},
        created_by=ctx.user_id,
    )
    db.add(event)
    db.flush()
    analyze_event(db, ctx.workspace, event)
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="event.create",
        entity_type="event",
        entity_id=event.id,
        details={
            "event_type": event.event_type,
            "occurred_at": occurred.isoformat(),
            "known_at": known.isoformat(),
            "duration_seconds": event.duration_seconds,
        },
    )
    audit.bump_revision(db,ctx.workspace)
    db.commit()
    db.refresh(event)
    return event_out(event)


@router.get("/{event_id}")
def get_event(
    event_id: int,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    return event_out(_get_event(db, ctx.workspace.id, event_id))


@router.post('/{event_id}/resolve')
def resolve_event(event_id:int,ctx:WorkspaceContext=Depends(require_workspace_roles(ROLE_PLANNER,ROLE_OPERATOR,ROLE_ADMIN)),db:Session=Depends(get_db)):
    event=_get_event(db,ctx.workspace.id,event_id)
    if (event.payload or {}).get('resolved'):raise ValidationError('事件已处理完成')
    now=utcnow()
    if now<ensure_utc(event.occurred_at):raise ValidationError('事件尚未发生，不能登记恢复')
    if event.event_type in ('machine_failure','material_delay'):
        event.duration_seconds=max(1,int((now-ensure_utc(event.occurred_at)).total_seconds()))
    event.payload={**(event.payload or {}),'resolved':True,'resolved_at':now.isoformat(),'resolved_by':ctx.user.id}
    audit.record(db,workspace_id=ctx.workspace.id,actor_id=ctx.user.id,action='event.resolve',entity_type='event',entity_id=event.id,details={'resolved_at':now.isoformat()})
    audit.bump_revision(db,ctx.workspace);db.commit();db.refresh(event)
    return event_out(event)


@router.post("/{event_id}/analyze")
def analyze(
    event_id: int,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_OPERATOR, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    event = _get_event(db, ctx.workspace.id, event_id)
    impact = analyze_event(db, ctx.workspace, event)
    db.commit()
    db.refresh(event)
    return {"event": event_out(event), "impact": impact}


@router.post("/{event_id}/reschedule")
def reschedule(
    event_id: int,
    payload: RescheduleRequest,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    event = _get_event(db, ctx.workspace.id, event_id)
    result = reschedule_from_event(
        db,
        ctx.workspace,
        event,
        ctx.user,
        settings,
        strategy=payload.strategy,
        running_task_policy=payload.running_task_policy,
        freeze_unaffected=payload.freeze_unaffected,
        time_limit_seconds=payload.time_limit_seconds,
    )
    db.commit()
    plan = None
    if result.get("plan_id"):
        from ..models import Plan

        plan = db.get(Plan, result["plan_id"])
    return {
        **result,
        "plan": plan_out(plan) if plan is not None else None,
        "event": event_out(event),
    }
