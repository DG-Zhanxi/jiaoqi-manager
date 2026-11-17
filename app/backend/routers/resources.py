from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dependencies import (
    WorkspaceContext,
    get_db,
    get_workspace_context,
    require_workspace_roles,
)
from ..errors import ConflictError, NotFoundError,ValidationError
from ..models import (
    ROLE_ADMIN,
    ROLE_PLANNER,
    Downtime,
    Material,
    OperationCandidate,
    OrderMaterial,
    PlanTask,
    Resource,
)
from ..schemas import DowntimeIn, MaterialIn, MaterialUpdate, ResourceIn, ResourceUpdate
from ..serializers import downtime_out, material_out, resource_out
from ..services import audit
from ..timeutil import ensure_utc

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["resources"])


def _calendar_payload(value) -> list[dict] | None:
    if value is None:
        return None
    shifts=[item.model_dump() for item in value]
    for day in range(7):
        windows=sorted((s['start_minute'],s['end_minute']) for s in shifts if s['weekday']==day)
        if any(a[1]>b[0] for a,b in zip(windows,windows[1:])):raise ValidationError('同一工作日的班次不能重叠')
    return shifts


@router.get("/resources")
def list_resources(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    resources = list(
        db.scalars(
            select(Resource)
            .where(Resource.workspace_id == ctx.workspace.id)
            .order_by(Resource.code)
        )
    )
    downtime_map: dict[int, list[Downtime]] = {}
    if resources:
        for item in db.scalars(
            select(Downtime)
            .where(Downtime.resource_id.in_([resource.id for resource in resources]))
            .order_by(Downtime.start_at)
        ):
            downtime_map.setdefault(item.resource_id, []).append(item)
    return {
        "items": [
            resource_out(resource, downtime_map.get(resource.id, [])) for resource in resources
        ]
    }


@router.post("/resources", status_code=201)
def create_resource(
    payload: ResourceIn,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    code = payload.code.strip()
    existing = db.scalar(
        select(Resource.id).where(
            Resource.workspace_id == ctx.workspace.id,
            Resource.code == code,
        )
    )
    if existing is not None:
        raise ConflictError("资源编码已存在")
    resource = Resource(
        workspace_id=ctx.workspace.id,
        code=code,
        name=payload.name.strip(),
        resource_type=payload.resource_type,
        capabilities=payload.capabilities,
        calendar=_calendar_payload(payload.calendar),
        is_active=payload.is_active,
    )
    db.add(resource)
    db.flush()
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="resource.create",
        entity_type="resource",
        entity_id=resource.id,
        details={"code": resource.code, "name": resource.name},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    db.refresh(resource)
    return resource_out(resource, [])


@router.patch("/resources/{resource_id}")
def update_resource(
    resource_id: int,
    payload: ResourceUpdate,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    resource = db.get(Resource, resource_id)
    if resource is None or resource.workspace_id != ctx.workspace.id:
        raise NotFoundError("资源不存在")
    if payload.code is not None:
        code = payload.code.strip()
        existing = db.scalar(
            select(Resource.id).where(
                Resource.workspace_id == ctx.workspace.id,
                Resource.code == code,
                Resource.id != resource.id,
            )
        )
        if existing is not None:
            raise ConflictError("资源编码已存在")
        resource.code = code
    if payload.name is not None:
        resource.name = payload.name.strip()
    if payload.resource_type is not None:
        resource.resource_type = payload.resource_type
    if payload.capabilities is not None:
        resource.capabilities = payload.capabilities
    if payload.calendar is not None:
        resource.calendar = _calendar_payload(payload.calendar)
    if payload.is_active is not None:
        resource.is_active = payload.is_active
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="resource.update",
        entity_type="resource",
        entity_id=resource.id,
        details={"code": resource.code},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    db.refresh(resource)
    downtime = list(
        db.scalars(select(Downtime).where(Downtime.resource_id == resource.id))
    )
    return resource_out(resource, downtime)


@router.delete("/resources/{resource_id}", status_code=204)
def delete_resource(
    resource_id: int,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    resource = db.get(Resource, resource_id)
    if resource is None or resource.workspace_id != ctx.workspace.id:
        raise NotFoundError("资源不存在")
    referenced_candidate = db.scalar(
        select(OperationCandidate.id)
        .where(OperationCandidate.resource_id == resource.id)
        .limit(1)
    )
    referenced_task = db.scalar(
        select(PlanTask.id).where(PlanTask.resource_id == resource.id).limit(1)
    )
    if referenced_candidate is not None or referenced_task is not None:
        raise ConflictError("资源已被工序或计划引用，不能删除；可停用该资源")
    db.delete(resource)
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="resource.delete",
        entity_type="resource",
        entity_id=resource_id,
        details={"code": resource.code},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    return None


@router.get("/downtimes")
def list_downtimes(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(Downtime, Resource)
        .join(Resource, Resource.id == Downtime.resource_id)
        .where(Resource.workspace_id == ctx.workspace.id)
        .order_by(Downtime.start_at)
    ).all()
    return {
        "items": [
            {**downtime_out(item), "resource_code": resource.code}
            for item, resource in rows
        ]
    }


@router.post("/resources/{resource_id}/downtime", status_code=201)
def create_downtime(
    resource_id: int,
    payload: DowntimeIn,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    resource = db.get(Resource, resource_id)
    if resource is None or resource.workspace_id != ctx.workspace.id:
        raise NotFoundError("资源不存在")
    item = Downtime(
        resource_id=resource.id,
        start_at=ensure_utc(payload.start_at),
        end_at=ensure_utc(payload.end_at),
        reason=payload.reason,
    )
    db.add(item)
    db.flush()
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="downtime.create",
        entity_type="downtime",
        entity_id=item.id,
        details={"resource_id": resource.id},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    db.refresh(item)
    return downtime_out(item)


@router.delete("/downtimes/{downtime_id}", status_code=204)
def delete_downtime(
    downtime_id: int,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    item = db.get(Downtime, downtime_id)
    if item is None:
        raise NotFoundError("停机记录不存在")
    resource = db.get(Resource, item.resource_id)
    if resource is None or resource.workspace_id != ctx.workspace.id:
        raise NotFoundError("停机记录不存在")
    db.delete(item)
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="downtime.delete",
        entity_type="downtime",
        entity_id=downtime_id,
        details={"resource_id": resource.id},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    return None


@router.get("/materials")
def list_materials(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(Material)
        .where(Material.workspace_id == ctx.workspace.id)
        .order_by(Material.name)
    )
    return {"items": [material_out(item) for item in rows]}


@router.post("/materials", status_code=201)
def create_material(
    payload: MaterialIn,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    name = payload.name.strip()
    existing = db.scalar(
        select(Material.id).where(
            Material.workspace_id == ctx.workspace.id,
            Material.name == name,
        )
    )
    if existing is not None:
        raise ConflictError("物料名称已存在")
    item = Material(
        workspace_id=ctx.workspace.id,
        name=name,
        unit=payload.unit,
        quantity=payload.quantity,
        available_at=ensure_utc(payload.available_at),
    )
    db.add(item)
    db.flush()
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="material.create",
        entity_type="material",
        entity_id=item.id,
        details={"name": item.name},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    db.refresh(item)
    return material_out(item)


@router.patch("/materials/{material_id}")
def update_material(
    material_id: int,
    payload: MaterialUpdate,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    item = db.get(Material, material_id)
    if item is None or item.workspace_id != ctx.workspace.id:
        raise NotFoundError("物料不存在")
    if payload.name is not None:
        name = payload.name.strip()
        existing = db.scalar(
            select(Material.id).where(
                Material.workspace_id == ctx.workspace.id,
                Material.name == name,
                Material.id != item.id,
            )
        )
        if existing is not None:
            raise ConflictError("物料名称已存在")
        item.name = name
    if payload.unit is not None:
        item.unit = payload.unit
    if payload.quantity is not None:
        item.quantity = payload.quantity
    if payload.available_at is not None:
        item.available_at = ensure_utc(payload.available_at)
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="material.update",
        entity_type="material",
        entity_id=item.id,
        details={"name": item.name},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    db.refresh(item)
    return material_out(item)


@router.delete("/materials/{material_id}", status_code=204)
def delete_material(
    material_id: int,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    item = db.get(Material, material_id)
    if item is None or item.workspace_id != ctx.workspace.id:
        raise NotFoundError("物料不存在")
    referenced = db.scalar(
        select(OrderMaterial.id).where(OrderMaterial.material_id == material_id).limit(1)
    )
    if referenced is not None:
        raise ConflictError("物料已被订单引用，不能删除")
    db.delete(item)
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="material.delete",
        entity_type="material",
        entity_id=material_id,
        details={"name": item.name},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    return None
