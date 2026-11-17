from __future__ import annotations

import time
from dataclasses import dataclass

from ortools.sat.python import cp_model

from .domain import Scenario, ScenarioError, ScheduledTask, SolveResult
from .validator import validate

STRATEGIES = ("delivery_first", "stability_first", "overtime_limited")


@dataclass
class _TaskVars:
    operation_id: int
    resource_id: int
    start_var: cp_model.IntVar
    end_var: cp_model.IntVar
    duration: int
    presence: cp_model.IntVar | None = None
    fixed: bool = False
    execution_state: str = "planned"


def _clamp_windows(
    windows: tuple[tuple[int, int], ...],
    start: int,
    end: int,
) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for left, right in windows:
        clipped_left = max(left, start)
        clipped_right = min(right, end)
        if clipped_right > clipped_left:
            result.append((clipped_left, clipped_right))
    result.sort()
    return result


def _merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not windows:
        return []
    merged = [windows[0]]
    for left, right in windows[1:]:
        last_left, last_right = merged[-1]
        if left <= last_right:
            merged[-1] = (last_left, max(last_right, right))
        else:
            merged.append((left, right))
    return merged


def _blocked_by_availability(
    windows: tuple[tuple[int, int], ...],
    start: int,
    end: int,
) -> list[tuple[int, int]]:
    clipped = _merge(_clamp_windows(windows, start, end))
    if not clipped:
        return [(start, end)]
    blocked: list[tuple[int, int]] = []
    cursor = start
    for left, right in clipped:
        if left > cursor:
            blocked.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        blocked.append((cursor, end))
    return blocked


def _validate_scenario(scenario: Scenario) -> None:
    if scenario.horizon_end_epoch <= scenario.horizon_start_epoch:
        raise ScenarioError("计划时间范围无效")
    if not scenario.operations:
        raise ScenarioError("没有可调度工序")
    resources = scenario.resource_map()
    if len(resources) != len(scenario.resources):
        raise ScenarioError("资源 ID 重复")
    operations = scenario.operation_map()
    if len(operations) != len(scenario.operations):
        raise ScenarioError("工序 ID 重复")
    order_ids = {item.order_id for item in scenario.orders}
    for resource in scenario.resources:
        if not resource.is_active:
            raise ScenarioError(f"资源 {resource.code} 已停用，不能出现在候选中")
        for left, right in resource.downtime_windows:
            if right <= left:
                raise ScenarioError(f"资源 {resource.code} 的停机窗口无效")
        for left, right in resource.availability_windows:
            if right <= left:
                raise ScenarioError(f"资源 {resource.code} 的工作班次无效")
    for operation in scenario.operations:
        if operation.order_id not in order_ids:
            raise ScenarioError(f"工序 {operation.operation_id} 引用了未知订单")
        if not operation.candidates:
            raise ScenarioError(f"工序 {operation.operation_id} 没有候选机器")
        if operation.release_epoch > scenario.horizon_end_epoch:
            raise ScenarioError(f"工序 {operation.operation_id} 的释放时间超出计划范围")
        for candidate in operation.candidates:
            if candidate.resource_id not in resources:
                raise ScenarioError(
                    f"工序 {operation.operation_id} 的候选资源 {candidate.resource_id} 不存在"
                )
            if candidate.duration_seconds <= 0:
                raise ScenarioError(f"工序 {operation.operation_id} 的加工时长必须为正")
        if operation.status == "completed":
            if operation.actual_start_epoch is None or operation.actual_end_epoch is None:
                raise ScenarioError(f"已完成工序 {operation.operation_id} 缺少实际起止时间")
        if operation.status == "running" and operation.actual_start_epoch is None:
            raise ScenarioError(f"运行中工序 {operation.operation_id} 缺少实际开始时间")


def solve(
    scenario: Scenario,
    *,
    strategy: str = "delivery_first",
    time_limit_seconds: float = 10.0,
    overtime_limit_seconds: int | None = None,
    random_seed: int = 0,
    workers: int = 4,
    should_cancel=None,
) -> SolveResult:
    if strategy not in STRATEGIES:
        raise ScenarioError(f"未知策略：{strategy}")
    _validate_scenario(scenario)
    if should_cancel is not None and should_cancel():
        return SolveResult(
            strategy=strategy,
            status="CANCELLED",
            tasks=(),
            objective_value=None,
            best_bound=None,
            wall_time_seconds=0.0,
            message="任务已取消",
        )

    model = cp_model.CpModel()
    start = scenario.horizon_start_epoch
    horizon = scenario.horizon_end_epoch - start
    resources = scenario.resource_map()
    base_tasks = scenario.base_task_map()

    resource_intervals: dict[int, list[cp_model.IntervalVar]] = {
        resource.resource_id: [] for resource in scenario.resources
    }

    for resource in scenario.resources:
        blocked = _clamp_windows(resource.downtime_windows, start, scenario.horizon_end_epoch)
        if resource.calendar_defined:
            blocked += _blocked_by_availability(resource.availability_windows, start, scenario.horizon_end_epoch)
        for left, right in _merge(sorted(blocked)):
            if right <= left:
                continue
            interval = model.NewIntervalVar(
                left - start,
                right - left,
                right - start,
                f"downtime_{resource.resource_id}_{left}",
            )
            resource_intervals[resource.resource_id].append(interval)

    task_vars: list[_TaskVars] = []
    start_exprs: dict[int, cp_model.LinearExpr] = {}
    end_exprs: dict[int, cp_model.LinearExpr] = {}

    for operation in scenario.operations:
        release_index = max(0, operation.release_epoch - start)
        base_task = base_tasks.get(operation.operation_id)
        fixed_start: int | None = None
        fixed_resource: int | None = operation.fixed_resource_id
        fixed_duration: int | None = None
        execution_state = operation.status if operation.status in {"running", "completed"} else "planned"

        if operation.status == "completed":
            fixed_start = operation.actual_start_epoch
            fixed_duration = operation.actual_end_epoch - operation.actual_start_epoch
            if base_task is not None:
                fixed_resource = base_task.resource_id
            elif fixed_resource is None and len(operation.candidates) == 1:
                fixed_resource = operation.candidates[0].resource_id
        elif operation.status == "running":
            fixed_start = operation.actual_start_epoch
            if base_task is not None:
                fixed_resource = base_task.resource_id
            elif fixed_resource is None and len(operation.candidates) == 1:
                fixed_resource = operation.candidates[0].resource_id
        else:
            if operation.operation_id in scenario.frozen_operation_ids and base_task is not None:
                fixed_start = base_task.start_epoch
                fixed_resource = base_task.resource_id
            elif operation.fixed_start_epoch is not None:
                fixed_start = operation.fixed_start_epoch

        if fixed_start is not None and not (start <= fixed_start <= scenario.horizon_end_epoch):
            raise ScenarioError(
                f"工序 {operation.operation_id} 的固定开始时间超出计划时间范围"
            )

        if fixed_resource is not None and operation.candidate_for(fixed_resource) is None:
            raise ScenarioError(
                f"工序 {operation.operation_id} 的固定资源不是候选机器"
            )

        if fixed_duration is not None:
            if fixed_duration <= 0:
                raise ScenarioError(f"已完成工序 {operation.operation_id} 的实际时长无效")
            if fixed_resource is None:
                raise ScenarioError(
                    f"已完成工序 {operation.operation_id} 无法确定实际使用资源"
                )
            start_index = fixed_start - start
            end_index = start_index + fixed_duration
            if end_index > horizon:
                raise ScenarioError(f"已完成工序 {operation.operation_id} 超出计划范围")
            start_const = model.NewConstant(start_index)
            end_const = model.NewConstant(end_index)
            interval = model.NewIntervalVar(
                start_const,
                fixed_duration,
                end_const,
                f"fixed_completed_{operation.operation_id}",
            )
            resource_intervals[fixed_resource].append(interval)
            task_vars.append(
                _TaskVars(
                    operation_id=operation.operation_id,
                    resource_id=fixed_resource,
                    start_var=start_const,
                    end_var=end_const,
                    duration=fixed_duration,
                    fixed=True,
                    execution_state="completed",
                )
            )
            start_exprs[operation.operation_id] = start_const
            end_exprs[operation.operation_id] = end_const
            continue

        candidates = (
            [operation.candidate_for(fixed_resource)]
            if fixed_resource is not None
            else list(operation.candidates)
        )
        if fixed_resource is not None:
            assert candidates[0] is not None
            candidate = candidates[0]
            duration = candidate.duration_seconds + operation.setup_seconds
            if fixed_start is not None:
                start_index = fixed_start - start
                end_index = start_index + duration
                if end_index > horizon:
                    raise ScenarioError(
                        f"工序 {operation.operation_id} 的固定任务超出计划范围"
                    )
                start_const = model.NewConstant(start_index)
                end_const = model.NewConstant(end_index)
                if execution_state == "planned":
                    model.Add(start_const >= release_index)
                interval = model.NewIntervalVar(
                    start_const, duration, end_const, f"fixed_{operation.operation_id}"
                )
                resource_intervals[fixed_resource].append(interval)
                task_vars.append(
                    _TaskVars(
                        operation_id=operation.operation_id,
                        resource_id=fixed_resource,
                        start_var=start_const,
                        end_var=end_const,
                        duration=duration,
                        fixed=True,
                        execution_state=execution_state,
                    )
                )
                start_exprs[operation.operation_id] = start_const
                end_exprs[operation.operation_id] = end_const
                continue
            start_var = model.NewIntVar(
                0, max(0, horizon - duration), f"start_{operation.operation_id}"
            )
            end_var = model.NewIntVar(duration, horizon, f"end_{operation.operation_id}")
            model.Add(end_var == start_var + duration)
            model.Add(start_var >= release_index)
            interval = model.NewIntervalVar(
                start_var, duration, end_var, f"task_{operation.operation_id}"
            )
            resource_intervals[fixed_resource].append(interval)
            task_vars.append(
                _TaskVars(
                    operation_id=operation.operation_id,
                    resource_id=fixed_resource,
                    start_var=start_var,
                    end_var=end_var,
                    duration=duration,
                    fixed=False,
                    execution_state=execution_state,
                )
            )
            start_exprs[operation.operation_id] = start_var
            end_exprs[operation.operation_id] = end_var
            continue

        literals: list[cp_model.IntVar] = []
        starts: list[cp_model.IntVar] = []
        ends: list[cp_model.IntVar] = []
        option_vars: list[_TaskVars] = []
        for candidate in operation.candidates:
            duration = candidate.duration_seconds + operation.setup_seconds
            if duration > horizon:
                continue
            if fixed_start is not None:
                start_index = fixed_start - start
                end_index = start_index + duration
                if end_index > horizon:
                    continue
                start_var = model.NewIntVar(
                    start_index, start_index, f"start_{operation.operation_id}_{candidate.resource_id}"
                )
                end_var = model.NewIntVar(
                    end_index, end_index, f"end_{operation.operation_id}_{candidate.resource_id}"
                )
            else:
                start_var = model.NewIntVar(
                    0,
                    max(0, horizon - duration),
                    f"start_{operation.operation_id}_{candidate.resource_id}",
                )
                end_var = model.NewIntVar(
                    duration, horizon, f"end_{operation.operation_id}_{candidate.resource_id}"
                )
                model.Add(end_var == start_var + duration)
            presence = model.NewBoolVar(f"use_{operation.operation_id}_{candidate.resource_id}")
            model.Add(start_var >= release_index).OnlyEnforceIf(presence)
            interval = model.NewOptionalIntervalVar(
                start_var,
                duration,
                end_var,
                presence,
                f"opt_{operation.operation_id}_{candidate.resource_id}",
            )
            resource_intervals[candidate.resource_id].append(interval)
            literals.append(presence)
            starts.append(start_var)
            ends.append(end_var)
            option_vars.append(
                _TaskVars(
                    operation_id=operation.operation_id,
                    resource_id=candidate.resource_id,
                    start_var=start_var,
                    end_var=end_var,
                    duration=duration,
                    presence=presence,
                    fixed=fixed_start is not None,
                    execution_state=execution_state,
                )
            )
        model.AddExactlyOne(literals)
        start_terms: list[cp_model.IntVar] = []
        end_terms: list[cp_model.IntVar] = []
        for presence, start_var, end_var in zip(literals, starts, ends):
            start_product = model.NewIntVar(
                0, horizon, f"start_product_{operation.operation_id}_{start_var.Name()}"
            )
            end_product = model.NewIntVar(
                0, horizon, f"end_product_{operation.operation_id}_{end_var.Name()}"
            )
            model.add_multiplication_equality(start_product, [presence, start_var])
            model.add_multiplication_equality(end_product, [presence, end_var])
            start_terms.append(start_product)
            end_terms.append(end_product)
        start_expr = sum(start_terms)
        end_expr = sum(end_terms)
        start_exprs[operation.operation_id] = start_expr
        end_exprs[operation.operation_id] = end_expr
        task_vars.extend(option_vars)

    for resource_id, intervals in resource_intervals.items():
        if intervals:
            model.AddNoOverlap(intervals)

    for order_operations in scenario.operations_by_order().values():
        for previous, following in zip(order_operations, order_operations[1:]):
            model.Add(end_exprs[previous.operation_id] <= start_exprs[following.operation_id])

    makespan = model.NewIntVar(0, horizon, "makespan")
    model.AddMaxEquality(makespan, list(end_exprs.values()))

    if strategy == "delivery_first":
        tardiness_terms = []
        operations_by_order = scenario.operations_by_order()
        for order in scenario.orders:
            order_ops = operations_by_order.get(order.order_id, [])
            if not order_ops or order.due_epoch is None:
                continue
            final_operation = order_ops[-1]
            due_index = max(0, order.due_epoch - start)
            tardiness = model.NewIntVar(0, horizon, f"tardiness_{order.order_id}")
            model.Add(tardiness >= end_exprs[final_operation.operation_id] - due_index)
            tardiness_terms.append(tardiness * max(1, order.weight))
        if tardiness_terms:
            model.Minimize(sum(tardiness_terms) * 1000 + makespan)
        else:
            model.Minimize(makespan)
    elif strategy == "stability_first":
        deviation_terms = []
        for operation in scenario.operations:
            base = base_tasks.get(operation.operation_id)
            if base is None:
                continue
            deviation = model.NewIntVar(0, horizon, f"deviation_{operation.operation_id}")
            model.AddAbsEquality(
                deviation, start_exprs[operation.operation_id] - (base.start_epoch - start)
            )
            deviation_terms.append(deviation)
        if deviation_terms:
            model.Minimize(sum(deviation_terms) * 100 + makespan)
        else:
            model.Minimize(makespan)
    else:
        limit = overtime_limit_seconds if overtime_limit_seconds is not None else scenario.horizon_end_epoch
        limit_index = min(horizon, max(0, limit - start))
        for operation in scenario.operations:
            model.Add(end_exprs[operation.operation_id] <= limit_index)
        tardiness_terms = []
        operations_by_order = scenario.operations_by_order()
        for order in scenario.orders:
            order_ops = operations_by_order.get(order.order_id, [])
            if not order_ops or order.due_epoch is None:
                continue
            final_operation = order_ops[-1]
            due_index = max(0, order.due_epoch - start)
            tardiness = model.NewIntVar(0, horizon, f"overtime_tardiness_{order.order_id}")
            model.Add(tardiness >= end_exprs[final_operation.operation_id] - due_index)
            tardiness_terms.append(tardiness * max(1, order.weight))
        if tardiness_terms:
            model.Minimize(sum(tardiness_terms) * 1000 + makespan)
        else:
            model.Minimize(makespan)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.01, float(time_limit_seconds))
    solver.parameters.num_search_workers = max(1, int(workers))
    solver.parameters.random_seed = int(random_seed)
    solver.parameters.log_search_progress = False

    begin = time.perf_counter()
    status = solver.Solve(model)
    elapsed = time.perf_counter() - begin
    status_name = solver.StatusName(status)

    if should_cancel is not None and should_cancel():
        return SolveResult(
            strategy=strategy,
            status="CANCELLED",
            tasks=(),
            objective_value=None,
            best_bound=None,
            wall_time_seconds=elapsed,
            message="任务已取消",
        )

    if status_name not in {"OPTIMAL", "FEASIBLE"}:
        message = {
            "INFEASIBLE": "约束不可行，未生成计划",
            "UNKNOWN": "求解器在时间上限内未证明最优，也未获得可行解",
            "MODEL_INVALID": "调度模型无效",
        }.get(status_name, f"求解器返回 {status_name}")
        return SolveResult(
            strategy=strategy,
            status=status_name,
            tasks=(),
            objective_value=None,
            best_bound=None,
            wall_time_seconds=elapsed,
            message=message,
            has_solution=False,
        )

    tasks: list[ScheduledTask] = []
    for operation in scenario.operations:
        matching = [item for item in task_vars if item.operation_id == operation.operation_id]
        chosen: _TaskVars | None = None
        for item in matching:
            if item.presence is None:
                chosen = item
                break
            if solver.BooleanValue(item.presence):
                chosen = item
                break
        if chosen is None:
            raise ScenarioError(
                f"求解成功但工序 {operation.operation_id} 没有选中机器，模型输出不一致"
            )
        tasks.append(
            ScheduledTask(
                operation_id=chosen.operation_id,
                resource_id=chosen.resource_id,
                start_epoch=start + solver.Value(chosen.start_var),
                end_epoch=start + solver.Value(chosen.end_var),
                duration_seconds=chosen.duration,
                is_fixed=chosen.fixed,
                execution_state=chosen.execution_state,
            )
        )

    validation = validate(scenario, tasks)
    metrics = dict(validation.get("metrics") or {})
    metrics.update(
        {
            "objective_value": int(solver.ObjectiveValue()),
            "best_bound": int(solver.BestObjectiveBound()),
            "wall_time_seconds": elapsed,
            "horizon_start_epoch": scenario.horizon_start_epoch,
            "horizon_end_epoch": scenario.horizon_end_epoch,
            "strategy": strategy,
        }
    )
    if not validation["valid"]:
        return SolveResult(
            strategy=strategy,
            status="INVALID",
            tasks=tuple(tasks),
            objective_value=int(solver.ObjectiveValue()),
            best_bound=int(solver.BestObjectiveBound()),
            wall_time_seconds=elapsed,
            metrics=metrics,
            validation=validation,
            message="求解结果未通过独立校验，未作为有效计划",
            has_solution=True,
        )

    return SolveResult(
        strategy=strategy,
        status=status_name,
        tasks=tuple(tasks),
        objective_value=int(solver.ObjectiveValue()),
        best_bound=int(solver.BestObjectiveBound()),
        wall_time_seconds=elapsed,
        metrics=metrics,
        validation=validation,
        message="已通过独立校验" if status_name == "FEASIBLE" else "已证明最优并通过独立校验",
        has_solution=True,
    )
