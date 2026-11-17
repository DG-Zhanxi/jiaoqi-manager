from __future__ import annotations

from .models import (
    AuditLog,
    Dataset,
    DatasetJob,
    DatasetMachineOption,
    DatasetOperation,
    Downtime,
    Event,
    Material,
    Operation,
    Order,
    Plan,
    PlanTask,
    Resource,
    ScheduleJob,
    User,
    Workspace,
)
from .timeutil import iso


def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": iso(user.created_at),
    }


def workspace_out(workspace: Workspace, role: str | None = None) -> dict:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "created_by": workspace.created_by,
        "data_revision": workspace.data_revision,
        "published_plan_id": workspace.published_plan_id,
        "role": role,
        "created_at": iso(workspace.created_at),
    }


def resource_out(resource: Resource, downtime: list[Downtime] | None = None) -> dict:
    return {
        "id": resource.id,
        "workspace_id": resource.workspace_id,
        "code": resource.code,
        "name": resource.name,
        "resource_type": resource.resource_type,
        "capabilities": resource.capabilities or [],
        "calendar": resource.calendar,
        "is_active": resource.is_active,
        "downtime": [downtime_out(item) for item in (downtime or [])],
        "created_at": iso(resource.created_at),
    }


def downtime_out(item: Downtime) -> dict:
    return {
        "id": item.id,
        "resource_id": item.resource_id,
        "start_at": iso(item.start_at),
        "end_at": iso(item.end_at),
        "reason": item.reason,
    }


def material_out(item: Material) -> dict:
    return {
        "id": item.id,
        "workspace_id": item.workspace_id,
        "name": item.name,
        "unit": item.unit,
        "quantity": item.quantity,
        "available_at": iso(item.available_at),
        "created_at": iso(item.created_at),
    }


def candidate_out(candidate) -> dict:  # noqa: ANN001
    return {
        "id": candidate.id,
        "resource_id": candidate.resource_id,
        "duration_seconds": candidate.duration_seconds,
    }


def operation_out(operation: Operation) -> dict:
    return {
        "id": operation.id,
        "sequence": operation.sequence,
        "name": operation.name,
        "setup_seconds": operation.setup_seconds,
        "status": operation.status,
        "fixed_start_at": iso(operation.fixed_start_at),
        "fixed_resource_id": operation.fixed_resource_id,
        "actual_start_at": iso(operation.actual_start_at),
        "actual_end_at": iso(operation.actual_end_at),
        "candidates": [candidate_out(item) for item in operation.candidates],
    }


def order_out(order: Order) -> dict:
    return {
        "id": order.id,
        "workspace_id": order.workspace_id,
        "order_no": order.order_no,
        "product_name": order.product_name,
        "quantity": order.quantity,
        "release_at": iso(order.release_at),
        "due_at": iso(order.due_at),
        "priority": order.priority,
        "status": order.status,
        "dataset_id": order.dataset_id,
        "external_job_key": order.external_job_key,
        "material_ready_at": iso(order.material_ready_at),
        "created_by": order.created_by,
        "created_at": iso(order.created_at),
        "updated_at": iso(order.updated_at),
        "operations": [operation_out(item) for item in order.operations],
        "materials": [
            {
                "material_id": requirement.material_id,
                "quantity": requirement.quantity,
            }
            for requirement in order.requirements
        ],
    }


def dataset_option_out(option: DatasetMachineOption) -> dict:
    return {
        "id": option.id,
        "machine_code": option.machine_code,
        "duration_seconds": option.duration_seconds,
        "source_machine": option.source_machine,
        "source_time": option.source_time,
        "unit_time": option.unit_time,
        "lot_size": option.lot_size,
        "source_row": option.source_row,
    }


def dataset_operation_out(operation: DatasetOperation) -> dict:
    return {
        "id": operation.id,
        "job_key": operation.job_key,
        "operation_key": operation.operation_key,
        "sequence": operation.sequence,
        "setup_seconds": operation.setup_seconds,
        "changeover_raw": operation.changeover_raw,
        "source_row": operation.source_row,
        "candidates": [dataset_option_out(item) for item in operation.candidates],
    }


def dataset_job_out(job: DatasetJob, include_rows: bool = False) -> dict:
    payload = {
        "id": job.id,
        "job_key": job.job_key,
        "lot_size": job.lot_size,
        "source_row": job.source_row,
    }
    if include_rows:
        payload["operations"] = [dataset_operation_out(item) for item in job.operations]
    return payload


def dataset_out(dataset: Dataset, include_rows: bool = False) -> dict:
    return {
        "id": dataset.id,
        "workspace_id": dataset.workspace_id,
        "name": dataset.name,
        "source_type": dataset.source_type,
        "sheet_name": dataset.sheet_name,
        "instance_key": dataset.instance_key,
        "source_uri": dataset.source_uri,
        "source_doi": dataset.source_doi,
        "license": dataset.license,
        "workbook_sha256": dataset.workbook_sha256,
        "status": dataset.status,
        "mapping": dataset.mapping,
        "row_counts": dataset.row_counts,
        "errors": dataset.errors,
        "created_by": dataset.created_by,
        "created_at": iso(dataset.created_at),
        "jobs": [dataset_job_out(item, include_rows=include_rows) for item in dataset.jobs],
    }


def plan_task_out(task: PlanTask) -> dict:
    return {
        "id": task.id,
        "operation_id": task.operation_id,
        "resource_id": task.resource_id,
        "start_at": iso(task.start_at),
        "end_at": iso(task.end_at),
        "duration_seconds": task.duration_seconds,
        "is_fixed": task.is_fixed,
        "execution_state": task.execution_state,
        "actual_start_at": iso(task.actual_start_at),
        "actual_end_at": iso(task.actual_end_at),
    }


def plan_out(plan: Plan, include_tasks: bool = True) -> dict:
    payload = {
        "id": plan.id,
        "workspace_id": plan.workspace_id,
        "name": plan.name,
        "status": plan.status,
        "strategy": plan.strategy,
        "created_by": plan.created_by,
        "data_revision": plan.data_revision,
        "base_plan_id": plan.base_plan_id,
        "solver_status": plan.solver_status,
        "objective_value": plan.objective_value,
        "best_bound": plan.best_bound,
        "solve_seconds": plan.solve_seconds,
        "model_snapshot": plan.model_snapshot,
        "validation": plan.validation,
        "reschedule_meta": plan.reschedule_meta,
        "created_at": iso(plan.created_at),
        "updated_at": iso(plan.updated_at),
        "submitted_at": iso(plan.submitted_at),
        "reviewed_by": plan.reviewed_by,
        "reviewed_at": iso(plan.reviewed_at),
        "review_comment": plan.review_comment,
        "published_by": plan.published_by,
        "published_at": iso(plan.published_at),
    }
    if include_tasks:
        payload["tasks"] = [plan_task_out(item) for item in plan.tasks]
    return payload


def job_out(job: ScheduleJob) -> dict:
    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "strategy": job.strategy,
        "time_limit_seconds": job.time_limit_seconds,
        "persist_plan": job.persist_plan,
        "base_plan_id": job.base_plan_id,
        "plan_id": job.plan_id,
        "solver_status": job.solver_status,
        "error": job.error,
        "result": job.result,
        "cancel_requested": job.cancel_requested,
        "requested_by": job.requested_by,
        "created_at": iso(job.created_at),
        "updated_at": iso(job.updated_at),
    }


def event_out(event: Event) -> dict:
    return {
        "id": event.id,
        "workspace_id": event.workspace_id,
        "event_type": event.event_type,
        "title": event.title,
        "occurred_at": iso(event.occurred_at),
        "known_at": iso(event.known_at),
        "duration_seconds": event.duration_seconds,
        "resource_id": event.resource_id,
        "order_id": event.order_id,
        "operation_id": event.operation_id,
        "payload": event.payload or {},
        "impact": event.impact,
        "impact_analyzed_at": iso(event.impact_analyzed_at),
        "created_by": event.created_by,
        "created_at": iso(event.created_at),
    }


def audit_out(item: AuditLog) -> dict:
    return {
        "id": item.id,
        "workspace_id": item.workspace_id,
        "actor_id": item.actor_id,
        "action": item.action,
        "entity_type": item.entity_type,
        "entity_id": item.entity_id,
        "details": item.details,
        "created_at": iso(item.created_at),
    }