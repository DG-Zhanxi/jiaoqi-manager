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
from ..errors import NotFoundError
from ..models import ROLE_ADMIN, ROLE_PLANNER, Dataset, Order
from ..schemas import HfutApplyRequest, HfutImportRequest
from ..serializers import dataset_out, order_out
from ..services.datasets import apply_hfut_dataset, import_hfut, list_hfut_sources
from ..timeutil import iso

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["datasets"])


@router.get("/datasets/sources")
def list_sources(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    settings: Settings = Depends(get_settings),
):
    return {"items": list_hfut_sources(settings)}


@router.get("/datasets")
def list_datasets(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(Dataset)
        .where(Dataset.workspace_id == ctx.workspace.id)
        .order_by(Dataset.created_at.desc(), Dataset.id.desc())
    )
    return {"items": [dataset_out(item) for item in rows]}


@router.post("/datasets/import/hfut", status_code=201)
def import_hfut_dataset(
    payload: HfutImportRequest,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    dataset = import_hfut(
        db,
        ctx.workspace,
        ctx.user,
        settings,
        sheet=payload.sheet,
        instance=payload.instance,
        name=payload.name,
    )
    db.commit()
    db.refresh(dataset)
    return dataset_out(dataset, include_rows=True)


@router.get("/datasets/{dataset_id}")
def get_dataset(
    dataset_id: int,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.workspace_id != ctx.workspace.id:
        raise NotFoundError("数据集不存在")
    return dataset_out(dataset, include_rows=True)


@router.post("/datasets/{dataset_id}/apply", status_code=201)
def apply_dataset(
    dataset_id: int,
    payload: HfutApplyRequest,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER, ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.workspace_id != ctx.workspace.id:
        raise NotFoundError("数据集不存在")
    orders = apply_hfut_dataset(
        db,
        ctx.workspace,
        ctx.user,
        dataset,
        release_at=payload.release_at,
        due_at=payload.due_at,
        confirm=payload.confirm,
    )
    db.commit()
    return {
        "dataset_id": dataset.id,
        "orders": [order_out(item) for item in orders],
        "order_count": len(orders),
    }