from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candidate:
    resource_id: int
    duration_seconds: int


@dataclass(frozen=True)
class Operation:
    operation_id: int
    order_id: int
    order_no: str
    name: str
    sequence: int
    setup_seconds: int
    release_epoch: int
    due_epoch: int | None
    priority: int
    weight: int
    candidates: tuple[Candidate, ...]
    fixed_start_epoch: int | None = None
    fixed_resource_id: int | None = None
    status: str = "planned"
    actual_start_epoch: int | None = None
    actual_end_epoch: int | None = None

    def candidate_for(self, resource_id: int) -> Candidate | None:
        for item in self.candidates:
            if item.resource_id == resource_id:
                return item
        return None

    @property
    def expected_durations(self) -> dict[int, int]:
        return {item.resource_id: item.duration_seconds + self.setup_seconds for item in self.candidates}


@dataclass(frozen=True)
class Resource:
    resource_id: int
    code: str
    downtime_windows: tuple[tuple[int, int], ...] = ()
    availability_windows: tuple[tuple[int, int], ...] = ()
    calendar_defined: bool = False
    is_active: bool = True


@dataclass(frozen=True)
class Order:
    order_id: int
    order_no: str
    release_epoch: int
    due_epoch: int | None
    priority: int
    weight: int = 1


@dataclass(frozen=True)
class BaseTask:
    operation_id: int
    resource_id: int
    start_epoch: int
    end_epoch: int
    execution_state: str = "planned"


@dataclass(frozen=True)
class Scenario:
    operations: tuple[Operation, ...]
    resources: tuple[Resource, ...]
    orders: tuple[Order, ...]
    horizon_start_epoch: int
    horizon_end_epoch: int
    base_tasks: tuple[BaseTask, ...] = ()
    frozen_operation_ids: frozenset[int] = frozenset()
    freeze_policy: str = "none"

    def operation_map(self) -> dict[int, Operation]:
        return {item.operation_id: item for item in self.operations}

    def resource_map(self) -> dict[int, Resource]:
        return {item.resource_id: item for item in self.resources}

    def order_map(self) -> dict[int, Order]:
        return {item.order_id: item for item in self.orders}

    def base_task_map(self) -> dict[int, BaseTask]:
        return {item.operation_id: item for item in self.base_tasks}

    def operations_by_order(self) -> dict[int, list[Operation]]:
        result: dict[int, list[Operation]] = {}
        for item in self.operations:
            result.setdefault(item.order_id, []).append(item)
        for items in result.values():
            items.sort(key=lambda item: item.sequence)
        return result


@dataclass(frozen=True)
class ScheduledTask:
    operation_id: int
    resource_id: int
    start_epoch: int
    end_epoch: int
    duration_seconds: int
    is_fixed: bool = False
    execution_state: str = "planned"

    def to_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "resource_id": self.resource_id,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
            "duration_seconds": self.duration_seconds,
            "is_fixed": self.is_fixed,
            "execution_state": self.execution_state,
        }


@dataclass
class SolveResult:
    strategy: str
    status: str
    tasks: tuple[ScheduledTask, ...]
    objective_value: int | None
    best_bound: int | None
    wall_time_seconds: float
    metrics: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    message: str = ""
    has_solution: bool = False

    def task_signature(self) -> tuple[tuple[int, int, int, int], ...]:
        return tuple(
            sorted(
                (
                    item.operation_id,
                    item.resource_id,
                    item.start_epoch,
                    item.end_epoch,
                )
                for item in self.tasks
            )
        )

    def to_dict(self, include_tasks: bool = True) -> dict:
        payload = {
            "strategy": self.strategy,
            "status": self.status,
            "objective_value": self.objective_value,
            "best_bound": self.best_bound,
            "wall_time_seconds": self.wall_time_seconds,
            "metrics": self.metrics,
            "validation": self.validation,
            "message": self.message,
            "has_solution": self.has_solution,
        }
        if include_tasks:
            payload["tasks"] = [item.to_dict() for item in self.tasks]
        return payload


class ScenarioError(ValueError):
    pass