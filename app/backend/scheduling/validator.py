"""独立调度校验器。

不导入 CP-SAT 或求解器实现；仅依据场景输入和任务输出重新检查可行性。
"""

from __future__ import annotations

from .domain import Scenario, ScheduledTask


def _overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


def _inside_window(start: int, end: int, windows: tuple[tuple[int, int], ...]) -> bool:
    return any(window_start <= start and end <= window_end for window_start, window_end in windows)


def validate(scenario: Scenario, tasks: list[ScheduledTask] | tuple[ScheduledTask, ...]) -> dict:
    errors: list[dict] = []
    checks = {
        "coverage": True,
        "eligibility": True,
        "duration": True,
        "precedence": True,
        "no_overlap": True,
        "downtime": True,
        "calendar": True,
        "release": True,
        "fixed": True,
        "horizon": True,
    }
    operations = scenario.operation_map()
    resources = scenario.resource_map()
    orders = scenario.order_map()

    if len(tasks) != len(operations):
        checks["coverage"] = False
    task_by_operation: dict[int, ScheduledTask] = {}
    for task in tasks:
        if task.operation_id in task_by_operation:
            checks["coverage"] = False
            errors.append(
                {
                    "code": "duplicate_operation_task",
                    "message": f"工序 {task.operation_id} 存在重复任务",
                }
            )
            continue
        task_by_operation[task.operation_id] = task
        if task.operation_id not in operations:
            checks["coverage"] = False
            errors.append(
                {
                    "code": "unknown_operation_task",
                    "message": f"任务引用了未知工序 {task.operation_id}",
                }
            )
    for operation_id in operations:
        if operation_id not in task_by_operation:
            checks["coverage"] = False
            errors.append(
                {
                    "code": "missing_operation_task",
                    "message": f"工序 {operation_id} 缺少任务",
                }
            )

    for operation in scenario.operations:
        task = task_by_operation.get(operation.operation_id)
        if task is None:
            continue
        resource = resources.get(task.resource_id)
        if resource is None or not resource.is_active:
            checks["eligibility"] = False
            errors.append(
                {
                    "code": "unknown_or_inactive_resource",
                    "message": f"工序 {operation.operation_id} 使用了不存在或停用的资源 {task.resource_id}",
                }
            )
            continue
        candidate = operation.candidate_for(task.resource_id)
        if candidate is None:
            checks["eligibility"] = False
            errors.append(
                {
                    "code": "ineligible_resource",
                    "message": f"工序 {operation.operation_id} 不能使用资源 {resource.code}",
                }
            )
            continue
        if task.end_epoch <= task.start_epoch:
            checks["duration"] = False
            errors.append(
                {
                    "code": "invalid_interval",
                    "message": f"工序 {operation.operation_id} 的结束时间必须晚于开始时间",
                }
            )
        expected_duration = candidate.duration_seconds + operation.setup_seconds
        if operation.status == "completed" and operation.actual_end_epoch and operation.actual_start_epoch:
            if task.end_epoch - task.start_epoch != task.duration_seconds:
                checks["duration"] = False
                errors.append(
                    {
                        "code": "duration_mismatch",
                        "message": f"工序 {operation.operation_id} 的时长与任务区间不一致",
                    }
                )
        elif task.duration_seconds != expected_duration or task.end_epoch - task.start_epoch != expected_duration:
            checks["duration"] = False
            errors.append(
                {
                    "code": "duration_mismatch",
                    "message": (
                        f"工序 {operation.operation_id} 时长应为 {expected_duration} 秒，"
                        f"任务为 {task.duration_seconds} 秒"
                    ),
                }
            )
        if task.start_epoch < scenario.horizon_start_epoch or task.end_epoch > scenario.horizon_end_epoch:
            checks["horizon"] = False
            errors.append(
                {
                    "code": "outside_horizon",
                    "message": f"工序 {operation.operation_id} 超出计划时间范围",
                }
            )
        if operation.status != "completed" and task.start_epoch < operation.release_epoch:
            checks["release"] = False
            errors.append(
                {
                    "code": "release_violation",
                    "message": (
                        f"工序 {operation.operation_id} 开始时间早于释放/物料就绪时间 "
                        f"{operation.release_epoch}"
                    ),
                }
            )
        if operation.fixed_start_epoch is not None and task.start_epoch != operation.fixed_start_epoch:
            checks["fixed"] = False
            errors.append(
                {
                    "code": "fixed_start_violation",
                    "message": f"工序 {operation.operation_id} 未遵守固定开始时间",
                }
            )
        if operation.fixed_resource_id is not None and task.resource_id != operation.fixed_resource_id:
            checks["fixed"] = False
            errors.append(
                {
                    "code": "fixed_resource_violation",
                    "message": f"工序 {operation.operation_id} 未遵守固定资源",
                }
            )
        if operation.status in {"running", "completed"} and operation.actual_start_epoch is not None:
            if task.start_epoch != operation.actual_start_epoch:
                checks["fixed"] = False
                errors.append(
                    {
                        "code": "actual_start_mismatch",
                        "message": f"工序 {operation.operation_id} 的实际开始时间与任务不一致",
                    }
                )
        if operation.status == "completed" and operation.actual_end_epoch is not None:
            if task.end_epoch != operation.actual_end_epoch:
                checks["fixed"] = False
                errors.append(
                    {
                        "code": "actual_end_mismatch",
                        "message": f"工序 {operation.operation_id} 的实际完成时间与任务不一致",
                    }
                )
        for window_start, window_end in resource.downtime_windows:
            if _overlaps(task.start_epoch, task.end_epoch, window_start, window_end):
                checks["downtime"] = False
                errors.append(
                    {
                        "code": "downtime_overlap",
                        "message": (
                            f"工序 {operation.operation_id} 与资源 {resource.code} 停机窗口重叠 "
                            f"[{window_start}, {window_end})"
                        ),
                    }
                )
        if resource.calendar_defined and not _inside_window(
            task.start_epoch, task.end_epoch, resource.availability_windows
        ):
            checks["calendar"] = False
            errors.append(
                {
                    "code": "calendar_violation",
                    "message": f"工序 {operation.operation_id} 未完整落在资源 {resource.code} 的工作班次内",
                }
            )

    for order_id, order_operations in scenario.operations_by_order().items():
        previous_end: int | None = None
        for operation in order_operations:
            task = task_by_operation.get(operation.operation_id)
            if task is None:
                continue
            if previous_end is not None and task.start_epoch < previous_end:
                checks["precedence"] = False
                errors.append(
                    {
                        "code": "precedence_violation",
                        "message": (
                            f"订单 {operation.order_no} 的工序 {operation.sequence} "
                            f"开始早于前序工序完成时间"
                        ),
                    }
                )
            previous_end = task.end_epoch
        if order_id not in orders:
            errors.append(
                {
                    "code": "unknown_order",
                    "message": f"工序引用了未知订单 {order_id}",
                }
            )

    for resource in scenario.resources:
        resource_tasks = sorted(
            (
                task
                for task in tasks
                if task.resource_id == resource.resource_id
            ),
            key=lambda item: (item.start_epoch, item.end_epoch),
        )
        for left, right in zip(resource_tasks, resource_tasks[1:]):
            if right.start_epoch < left.end_epoch:
                checks["no_overlap"] = False
                errors.append(
                    {
                        "code": "resource_overlap",
                        "message": (
                            f"资源 {resource.code} 上工序 {left.operation_id} 与 "
                            f"{right.operation_id} 时间重叠"
                        ),
                    }
                )

    makespan = (
        max((item.end_epoch for item in tasks), default=scenario.horizon_start_epoch)
        - scenario.horizon_start_epoch
    )
    weighted_tardiness = 0
    late_orders = 0
    operations_by_order = scenario.operations_by_order()
    for order in scenario.orders:
        if order.due_epoch is None:
            continue
        order_ops = operations_by_order.get(order.order_id, [])
        if not order_ops:
            continue
        final_task = task_by_operation.get(order_ops[-1].operation_id)
        if final_task is None:
            continue
        tardiness = max(0, final_task.end_epoch - order.due_epoch)
        if tardiness:
            late_orders += 1
            weighted_tardiness += tardiness * order.weight

    base_map = scenario.base_task_map()
    changed = 0
    for task in tasks:
        base = base_map.get(task.operation_id)
        if base is None or base.resource_id != task.resource_id or base.start_epoch != task.start_epoch:
            changed += 1

    metrics = {
        "makespan_seconds": makespan,
        "weighted_tardiness_seconds": weighted_tardiness,
        "late_orders": late_orders,
        "changed_operations": changed,
        "task_count": len(tasks),
        "resource_count": len(scenario.resources),
    }
    return {
        "valid": not errors,
        "errors": errors,
        "checks": checks,
        "metrics": metrics,
    }