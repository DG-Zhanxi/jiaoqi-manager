from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..errors import ValidationError
from ..models import Material, MaterialAllocation, Order
from ..timeutil import epoch, from_epoch


class MaterialShortageError(ValidationError):
    def __init__(self, shortages: list[dict]) -> None:
        super().__init__("物料库存不足或物料不存在，无法完成跨订单分配", {"shortages": shortages})


def allocate_materials(db: Session, workspace_id: int) -> dict[int, int | None]:
    """按优先级/交期顺序做跨订单库存分配。

    分配结果写入 material_allocations，并把每个订单的物料就绪时间汇总到
    Order.material_ready_at；调度器与校验器都使用该时间作为释放下界。
    """

    materials = {
        item.id: item
        for item in db.scalars(select(Material).where(Material.workspace_id == workspace_id))
    }
    orders = list(
        db.scalars(
            select(Order).where(
                Order.workspace_id == workspace_id,
                Order.status == "confirmed",
            )
        )
    )

    def order_sort_key(order: Order) -> tuple:
        due = epoch(order.due_at) if order.due_at is not None else 10**15
        return (-order.priority, due, order.id)

    orders.sort(key=order_sort_key)
    remaining = {material_id: float(item.quantity) for material_id, item in materials.items()}
    allocations: list[dict] = []
    shortages: list[dict] = []
    ready_by_order: dict[int, int | None] = {}

    for order in orders:
        ready: int | None = None
        for requirement in order.requirements:
            material = materials.get(requirement.material_id)
            if material is None:
                shortages.append(
                    {
                        "order_id": order.id,
                        "material_id": requirement.material_id,
                        "code": "missing_material",
                        "message": "订单引用的物料不存在",
                    }
                )
                continue
            available = remaining.get(requirement.material_id, 0.0)
            if requirement.quantity > available + 1e-9:
                shortages.append(
                    {
                        "order_id": order.id,
                        "material_id": requirement.material_id,
                        "code": "insufficient_stock",
                        "required": requirement.quantity,
                        "available": available,
                        "message": f"物料 {material.name} 库存不足",
                    }
                )
                continue
            remaining[requirement.material_id] = available - requirement.quantity
            available_epoch = epoch(material.available_at)
            ready = available_epoch if ready is None else max(ready, available_epoch)
            allocations.append(
                {
                    "order_id": order.id,
                    "material_id": requirement.material_id,
                    "quantity": requirement.quantity,
                    "ready_at": material.available_at,
                }
            )
        ready_by_order[order.id] = ready

    if shortages:
        raise MaterialShortageError(shortages)

    db.execute(delete(MaterialAllocation).where(MaterialAllocation.workspace_id == workspace_id))
    for item in allocations:
        db.add(
            MaterialAllocation(
                workspace_id=workspace_id,
                order_id=item["order_id"],
                material_id=item["material_id"],
                quantity=item["quantity"],
                ready_at=item["ready_at"],
            )
        )

    for order in orders:
        ready_epoch = ready_by_order.get(order.id)
        order.material_ready_at = from_epoch(ready_epoch) if ready_epoch is not None else None
    db.flush()
    return ready_by_order