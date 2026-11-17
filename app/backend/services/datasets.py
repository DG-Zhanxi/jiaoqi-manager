from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.hfut import HfutAdapter, HfutParseResult
from ..config import Settings
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import (
    Dataset,
    DatasetJob,
    DatasetMachineOption,
    DatasetOperation,
    Operation,
    OperationCandidate,
    Order,
    Resource,
    User,
    Workspace,
)
from ..timeutil import ensure_utc, utcnow
from . import audit


def resolve_hfut_workbook(settings: Settings, requested_path: str | None = None) -> Path:
    root = Path(settings.hfut_data_root).resolve()
    if requested_path:
        candidate = Path(requested_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValidationError("数据文件必须位于配置的 HFUT 数据目录内") from exc
    else:
        resolved = Path(settings.hfut_workbook_path).resolve()
    if resolved.suffix.lower() != ".xlsx":
        raise ValidationError("仅支持 .xlsx 工作簿")
    if not resolved.exists():
        raise NotFoundError("HFUT 数据文件不存在")
    return resolved


def list_hfut_sources(settings: Settings, requested_path: str | None = None) -> list[dict]:
    path = resolve_hfut_workbook(settings, requested_path)
    adapter = HfutAdapter(path)
    return [item.to_dict() for item in adapter.available_instances()]


def import_hfut(
    db: Session,
    workspace: Workspace,
    user: User,
    settings: Settings,
    *,
    sheet: str | None = None,
    instance: str | None = None,
    name: str | None = None,
    requested_path: str | None = None,
) -> Dataset:
    path = resolve_hfut_workbook(settings, requested_path)
    adapter = HfutAdapter(path)
    instances = adapter.available_instances()
    if not sheet and not instance:
        if not instances:
            raise ValidationError("工作簿中没有可导入的实例")
        instance = instances[0].label
    result: HfutParseResult = adapter.parse(sheet=sheet, instance=instance)

    dataset = Dataset(
        workspace_id=workspace.id,
        name=name or f"HFUT {result.instance_key}（{result.sheet}）",
        source_type="hfut",
        sheet_name=result.sheet,
        instance_key=result.instance_key,
        source_uri=result.source_uri,
        source_doi=result.mapping.get("source_doi"),
        license=result.mapping.get("license"),
        workbook_sha256=result.workbook_sha256,
        status=result.status,
        mapping=result.mapping,
        row_counts=result.row_counts,
        errors=result.errors,
        created_by=user.id,
    )
    db.add(dataset)
    db.flush()

    jobs: dict[str, DatasetJob] = {}
    for item in result.jobs:
        job = DatasetJob(
            dataset_id=dataset.id,
            job_key=item.job_key,
            lot_size=item.lot_size,
            source_row=item.source_row,
        )
        db.add(job)
        jobs[item.job_key] = job
    db.flush()

    for item in result.operations:
        job = jobs[item.job_key]
        operation = DatasetOperation(
            dataset_id=dataset.id,
            job_id=job.id,
            job_key=item.job_key,
            operation_key=item.operation_key,
            sequence=item.sequence,
            setup_seconds=item.setup_seconds,
            changeover_raw=item.changeover_raw,
            source_row=item.source_row,
        )
        db.add(operation)
        db.flush()
        for candidate in item.candidates:
            db.add(
                DatasetMachineOption(
                    operation_id=operation.id,
                    machine_code=candidate.machine_code,
                    duration_seconds=candidate.duration_seconds,
                    source_machine=candidate.source_machine,
                    source_time=candidate.source_time,
                    unit_time=candidate.unit_time,
                    lot_size=candidate.lot_size,
                    source_row=candidate.source_row,
                )
            )

    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="dataset.import",
        entity_type="dataset",
        entity_id=dataset.id,
        details={
            "source_type": "hfut",
            "sheet": result.sheet,
            "instance": result.instance_key,
            "workbook_sha256": result.workbook_sha256,
            "row_counts": result.row_counts,
            "status": result.status,
        },
    )
    audit.bump_revision(db, workspace)
    db.flush()
    db.refresh(dataset)
    return dataset


def apply_hfut_dataset(
    db: Session,
    workspace: Workspace,
    user: User,
    dataset: Dataset,
    *,
    release_at=None,
    due_at=None,
    confirm: bool = True,
) -> list[Order]:
    if dataset.workspace_id != workspace.id:
        raise NotFoundError("数据集不属于当前工作空间")
    if dataset.status != "valid":
        raise ValidationError("数据集存在校验错误，不能导入为订单", {"errors": dataset.errors})
    if dataset.source_type != "hfut":
        raise ValidationError("该数据集不是 HFUT 数据集")
    existing = db.scalar(
        select(Order.id).where(Order.workspace_id == workspace.id, Order.dataset_id == dataset.id)
    )
    if existing is not None:
        raise ConflictError("该数据集已经导入过订单，不能重复导入")

    release = ensure_utc(release_at) if release_at is not None else utcnow()
    due = ensure_utc(due_at) if due_at is not None else None
    if due is not None and due < release:
        raise ValidationError("交期不能早于释放时间")

    resources: dict[str, Resource] = {
        item.code: item
        for item in db.scalars(select(Resource).where(Resource.workspace_id == workspace.id))
    }
    created_orders: list[Order] = []
    for job in dataset.jobs:
        order_no = f"{dataset.instance_key}-J{job.job_key}"
        if len(order_no) > 80:
            order_no = f"DS{dataset.id}-J{job.job_key}"
        order = Order(
            workspace_id=workspace.id,
            order_no=order_no,
            product_name=f"HFUT {dataset.sheet_name} 作业 {job.job_key}",
            quantity=job.lot_size,
            release_at=release,
            due_at=due,
            priority=0,
            status="confirmed" if confirm else "draft",
            dataset_id=dataset.id,
            external_job_key=job.job_key,
            created_by=user.id,
        )
        db.add(order)
        db.flush()

        for dataset_operation in sorted(job.operations, key=lambda item: item.sequence):
            operation = Operation(
                order_id=order.id,
                sequence=dataset_operation.sequence,
                name=f"工序 {dataset_operation.sequence}",
                setup_seconds=dataset_operation.setup_seconds,
                status="planned",
            )
            db.add(operation)
            db.flush()
            for option in dataset_operation.candidates:
                resource = resources.get(option.machine_code)
                if resource is None:
                    resource = Resource(
                        workspace_id=workspace.id,
                        code=option.machine_code,
                        name=f"机器 {option.machine_code}",
                        resource_type="machine",
                        capabilities=[],
                        is_active=True,
                    )
                    db.add(resource)
                    db.flush()
                    resources[option.machine_code] = resource
                db.add(
                    OperationCandidate(
                        operation_id=operation.id,
                        resource_id=resource.id,
                        duration_seconds=option.duration_seconds,
                    )
                )
        created_orders.append(order)

    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="dataset.apply",
        entity_type="dataset",
        entity_id=dataset.id,
        details={
            "orders": len(created_orders),
            "resources": len(resources),
            "release_at": release.isoformat(),
            "due_at": due.isoformat() if due else None,
            "confirm": confirm,
        },
    )
    audit.bump_revision(db, workspace)
    db.flush()
    return created_orders