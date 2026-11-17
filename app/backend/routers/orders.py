from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..dependencies import (
    WorkspaceContext,
    get_db,
    get_settings,
    get_workspace_context,
    require_workspace_roles,
)
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import (
    ROLE_ADMIN,
    ROLE_PLANNER,
    Material,
    Operation,
    OperationCandidate,
    Order,
    OrderMaterial,
    PlanTask,
    Resource,
)
from ..schemas import OrderIn, OrderUpdate
from ..serializers import order_out
from ..services import audit
from ..services.scheduling import (
    build_scenario,
    proposed_order_from_schema,
    solve_scenario,
)
from ..timeutil import ensure_utc, utcnow

router = APIRouter(prefix="/api/workspaces/{workspace_id}/orders", tags=["orders"])


def _get_order(db: Session, workspace_id: int, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.workspace_id != workspace_id:
        raise NotFoundError("订单不存在")
    return order


def _validate_resources(db: Session, workspace_id: int, operations) -> dict[int, Resource]:
    resource_ids = {
        candidate.resource_id
        for operation in operations
        for candidate in operation.candidates
    }
    if not resource_ids:
        return {}
    resources = {
        item.id: item
        for item in db.scalars(
            select(Resource).where(
                Resource.workspace_id == workspace_id,
                Resource.id.in_(resource_ids),
                Resource.is_active.is_(True),
            )
        )
    }
    missing = sorted(resource_ids - set(resources))
    if missing:
        raise ValidationError(
            "订单包含当前工作空间不存在或已停用的机器",
            {"resource_ids": missing, "code": "ineligible_resource"},
        )
    return resources


def _validate_materials(db: Session, workspace_id: int, requirements) -> dict[int, Material]:
    material_ids = {item.material_id for item in requirements}
    if not material_ids:
        return {}
    materials = {
        item.id: item
        for item in db.scalars(
            select(Material).where(
                Material.workspace_id == workspace_id,
                Material.id.in_(material_ids),
            )
        )
    }
    missing = sorted(material_ids - set(materials))
    if missing:
        raise ValidationError("订单包含不存在的物料", {"material_ids": missing})
    return materials


def _has_plan_tasks(db: Session, order_id: int) -> bool:
    return (
        db.scalar(
            select(PlanTask.id)
            .join(Operation, Operation.id == PlanTask.operation_id)
            .where(Operation.order_id == order_id)
            .limit(1)
        )
        is not None
    )


def _add_operations(db: Session, order: Order, operations) -> None:
    for operation_in in sorted(operations, key=lambda item: item.sequence):
        operation = Operation(
            order_id=order.id,
            sequence=operation_in.sequence,
            name=operation_in.name,
            setup_seconds=operation_in.setup_seconds,
            status="planned",
        )
        db.add(operation)
        db.flush()
        for candidate in operation_in.candidates:
            db.add(
                OperationCandidate(
                    operation_id=operation.id,
                    resource_id=candidate.resource_id,
                    duration_seconds=candidate.duration_seconds,
                )
            )


def _add_materials(db: Session, order: Order, requirements) -> None:
    for item in requirements:
        db.add(
            OrderMaterial(
                order_id=order.id,
                material_id=item.material_id,
                quantity=item.quantity,
            )
        )


@router.get("")
def list_orders(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
    search: str | None = None,
    status: str | None = None,
    priority_min: int | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    sort_by: str = Query("created_at", pattern="^(created_at|due_at|release_at|priority|order_no)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    conditions = [Order.workspace_id == ctx.workspace.id]
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(or_(Order.order_no.ilike(pattern), Order.product_name.ilike(pattern)))
    if status:
        conditions.append(Order.status == status)
    if priority_min is not None:
        conditions.append(Order.priority >= priority_min)
    if due_from is not None:
        conditions.append(Order.due_at >= ensure_utc(due_from))
    if due_to is not None:
        conditions.append(Order.due_at <= ensure_utc(due_to))
    total = db.scalar(select(func.count(Order.id)).where(*conditions)) or 0
    sort_column = {
        "created_at": Order.created_at,
        "due_at": Order.due_at,
        "release_at": Order.release_at,
        "priority": Order.priority,
        "order_no": Order.order_no,
    }[sort_by]
    ordering = sort_column.asc() if sort_dir == "asc" else sort_column.desc()
    rows = db.scalars(
        select(Order)
        .where(*conditions)
        .order_by(ordering, Order.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [order_out(item) for item in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("", status_code=201)
def create_order(
    payload: OrderIn,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    order_no = payload.order_no.strip()
    existing = db.scalar(
        select(Order.id).where(
            Order.workspace_id == ctx.workspace.id,
            Order.order_no == order_no,
        )
    )
    if existing is not None:
        raise ConflictError("订单号已存在")
    _validate_resources(db, ctx.workspace.id, payload.operations)
    _validate_materials(db, ctx.workspace.id, payload.materials)
    order = Order(
        workspace_id=ctx.workspace.id,
        order_no=order_no,
        product_name=payload.product_name.strip(),
        quantity=payload.quantity,
        release_at=ensure_utc(payload.release_at),
        due_at=ensure_utc(payload.due_at),
        priority=payload.priority,
        status=payload.status,
        created_by=ctx.user_id,
    )
    db.add(order)
    db.flush()
    _add_operations(db, order, payload.operations)
    _add_materials(db, order, payload.materials)
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="order.create",
        entity_type="order",
        entity_id=order.id,
        details={
            "order_no": order.order_no,
            "quantity": order.quantity,
            "operations": len(payload.operations),
            "status": order.status,
        },
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    db.refresh(order)
    return order_out(order)


@router.get("/{order_id}")
def get_order(
    order_id: int,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    return order_out(_get_order(db, ctx.workspace.id, order_id))


@router.put("/{order_id}")
def update_order(
    order_id: int,
    payload: OrderUpdate,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    order = _get_order(db, ctx.workspace.id, order_id)
    if payload.order_no is not None:
        order_no = payload.order_no.strip()
        existing = db.scalar(
            select(Order.id).where(
                Order.workspace_id == ctx.workspace.id,
                Order.order_no == order_no,
                Order.id != order.id,
            )
        )
        if existing is not None:
            raise ConflictError("订单号已存在")
        order.order_no = order_no
    if payload.product_name is not None:
        order.product_name = payload.product_name.strip()
    if payload.quantity is not None:
        order.quantity = payload.quantity
    if payload.release_at is not None:
        order.release_at = ensure_utc(payload.release_at)
    if payload.due_at is not None:
        order.due_at = ensure_utc(payload.due_at)
    if payload.priority is not None:
        order.priority = payload.priority
    if payload.status is not None:
        order.status = payload.status
    if order.due_at is not None and order.due_at < order.release_at:
        raise ValidationError("交期不能早于释放时间")

    if payload.operations is not None:
        if _has_plan_tasks(db, order.id):
            raise ConflictError("订单工序已被计划引用，不能直接替换；请新建订单或先处理旧计划")
        _validate_resources(db, ctx.workspace.id, payload.operations)
        db.execute(delete(Operation).where(Operation.order_id == order.id))
        db.flush()
        _add_operations(db, order, payload.operations)

    if payload.materials is not None:
        _validate_materials(db, ctx.workspace.id, payload.materials)
        db.execute(delete(OrderMaterial).where(OrderMaterial.order_id == order.id))
        db.flush()
        _add_materials(db, order, payload.materials)

    if order.status == "confirmed":
        operations_count = db.scalar(
            select(func.count(Operation.id)).where(Operation.order_id == order.id)
        )
        if not operations_count:
            raise ValidationError("确认订单必须包含至少一个工序")
        operations = list(
            db.scalars(select(Operation).where(Operation.order_id == order.id))
        )
        for operation in operations:
            if not operation.candidates:
                raise ValidationError(f"工序 {operation.sequence} 缺少可执行机器")

    order.updated_at = utcnow()
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="order.update",
        entity_type="order",
        entity_id=order.id,
        details={"order_no": order.order_no},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    db.refresh(order)
    return order_out(order)


@router.delete("/{order_id}", status_code=204)
def delete_order(
    order_id: int,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    order = _get_order(db, ctx.workspace.id, order_id)
    if _has_plan_tasks(db, order.id):
        raise ConflictError("订单已被计划引用，不能删除")
    db.delete(order)
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="order.delete",
        entity_type="order",
        entity_id=order_id,
        details={"order_no": order.order_no},
    )
    audit.bump_revision(db, ctx.workspace)
    db.commit()
    return None


@router.post("/preview")
def preview_order(
    payload: OrderIn,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    time_limit_seconds: float | None = Query(default=None, gt=0, le=3600),
):
    if payload.status != "confirmed":
        raise ValidationError("承诺预览需要确认态订单")
    _validate_resources(db, ctx.workspace.id, payload.operations)
    proposed = proposed_order_from_schema(payload)
    if payload.materials:
        from dataclasses import replace
        from sqlalchemy import func
        from ..models import MaterialAllocation
        from ..services.materials import allocate_materials
        from ..timeutil import epoch
        allocate_materials(db,ctx.workspace.id)
        material_map=_validate_materials(db,ctx.workspace.id,payload.materials)
        ready=proposed.release_epoch
        for required in payload.materials:
            material=material_map[required.material_id]
            reserved=db.scalar(select(func.coalesce(func.sum(MaterialAllocation.quantity),0)).where(MaterialAllocation.material_id==material.id)) or 0
            if material.quantity-reserved+1e-9<required.quantity:raise ValidationError(f'物料 {material.name} 的可用数量不足')
            ready=max(ready,epoch(material.available_at))
        proposed=replace(proposed,release_epoch=ready)
    scenario, snapshot = build_scenario(db, ctx.workspace, settings, extra_order=proposed)
    result = solve_scenario(
        scenario,
        settings,
        strategy="delivery_first",
        time_limit_seconds=time_limit_seconds,
    )
    db.rollback()
    return {
        "persisted": False,
        "snapshot": snapshot,
        "result": result.to_dict(include_tasks=True),
        "message": "承诺预览仅求解当前约束，未保存订单或计划",
    }
