from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RoleLiteral = Literal["admin", "planner", "reviewer", "operator"]
StrategyLiteral = Literal["delivery_first", "stability_first", "overtime_limited"]
EventTypeLiteral = Literal[
    "machine_failure", "new_urgent_order", "material_delay", "operation_overrun"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictModel):
    username: str
    password: str


class LoginRequest(StrictModel):
    username: str
    password: str


class WorkspaceCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)


class MemberInvite(StrictModel):
    username: str
    role: RoleLiteral = "planner"


class MemberRoleUpdate(StrictModel):
    role: RoleLiteral


class CandidateIn(StrictModel):
    resource_id: int = Field(ge=1)
    duration_seconds: int = Field(gt=0)


class OperationIn(StrictModel):
    sequence: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    setup_seconds: int = Field(default=0, ge=0)
    candidates: list[CandidateIn] = Field(default_factory=list)

    @field_validator("candidates")
    @classmethod
    def unique_candidates(cls, value: list[CandidateIn]) -> list[CandidateIn]:
        seen: set[int] = set()
        for item in value:
            if item.resource_id in seen:
                raise ValueError("同一工序的候选机器不能重复")
            seen.add(item.resource_id)
        return value


class MaterialRequirementIn(StrictModel):
    material_id: int = Field(ge=1)
    quantity: float = Field(gt=0)


class OrderIn(StrictModel):
    order_no: str = Field(min_length=1, max_length=80)
    product_name: str = Field(min_length=1, max_length=160)
    quantity: int = Field(gt=0)
    release_at: datetime
    due_at: datetime | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    status: Literal["draft", "confirmed"] = "confirmed"
    operations: list[OperationIn] = Field(default_factory=list)
    materials: list[MaterialRequirementIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_structure(self) -> "OrderIn":
        sequences = [item.sequence for item in self.operations]
        if len(sequences) != len(set(sequences)):
            raise ValueError("工序序号不能重复")
        if self.due_at is not None and self.due_at < self.release_at:
            raise ValueError("交期不能早于释放时间")
        if self.status == "confirmed":
            if not self.operations:
                raise ValueError("确认订单必须包含至少一个工序")
            for operation in self.operations:
                if not operation.candidates:
                    raise ValueError(f"工序 {operation.sequence} 缺少可执行机器")
        material_ids = [item.material_id for item in self.materials]
        if len(material_ids) != len(set(material_ids)):
            raise ValueError("同一物料不能重复")
        return self


class OrderUpdate(StrictModel):
    order_no: str | None = Field(default=None, min_length=1, max_length=80)
    product_name: str | None = Field(default=None, min_length=1, max_length=160)
    quantity: int | None = Field(default=None, gt=0)
    release_at: datetime | None = None
    due_at: datetime | None = None
    priority: int | None = Field(default=None, ge=-100, le=100)
    status: Literal["draft", "confirmed"] | None = None
    operations: list[OperationIn] | None = None
    materials: list[MaterialRequirementIn] | None = None


class CalendarShiftIn(StrictModel):
    weekday: int = Field(ge=0, le=6)
    start_minute: int = Field(ge=0, le=1439)
    end_minute: int = Field(ge=1, le=1440)

    @model_validator(mode="after")
    def validate_range(self) -> "CalendarShiftIn":
        if self.end_minute <= self.start_minute:
            raise ValueError("班次结束时间必须晚于开始时间")
        return self


class ResourceIn(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    resource_type: str = Field(default="machine", max_length=32)
    capabilities: list[str] = Field(default_factory=list)
    calendar: list[CalendarShiftIn] | None = None
    is_active: bool = True


class ResourceUpdate(StrictModel):
    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    resource_type: str | None = Field(default=None, max_length=32)
    capabilities: list[str] | None = None
    calendar: list[CalendarShiftIn] | None = None
    is_active: bool | None = None


class DowntimeIn(StrictModel):
    start_at: datetime
    end_at: datetime
    reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_range(self) -> "DowntimeIn":
        if self.end_at <= self.start_at:
            raise ValueError("停机结束时间必须晚于开始时间")
        return self


class MaterialIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    unit: str = Field(default="件", max_length=24)
    quantity: float = Field(ge=0)
    available_at: datetime


class MaterialUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    unit: str | None = Field(default=None, max_length=24)
    quantity: float | None = Field(default=None, ge=0)
    available_at: datetime | None = None


class ScheduleCreate(StrictModel):
    strategy: StrategyLiteral = "delivery_first"
    time_limit_seconds: float | None = Field(default=None, gt=0, le=3600)
    overtime_limit_seconds: int | None = Field(default=None, gt=0)
    persist_plan: bool = True
    base_plan_id: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, max_length=160)


class CompareRequest(StrictModel):
    time_limit_seconds: float | None = Field(default=None, gt=0, le=3600)
    overtime_limit_seconds: int | None = Field(default=None, gt=0)


class ReviewRequest(StrictModel):
    comment: str | None = Field(default=None, max_length=2000)


class ReviewDecision(StrictModel):
    approved: bool
    comment: str | None = Field(default=None, max_length=2000)


class ExecutionStart(StrictModel):
    actual_start_at: datetime | None = None


class ExecutionComplete(StrictModel):
    actual_end_at: datetime | None = None


class HfutImportRequest(StrictModel):
    sheet: str | None = None
    instance: str | None = None
    name: str | None = Field(default=None, max_length=160)


class HfutApplyRequest(StrictModel):
    release_at: datetime | None = None
    due_at: datetime | None = None
    confirm: bool = True


class EventCreate(StrictModel):
    event_type: EventTypeLiteral
    title: str = Field(min_length=1, max_length=160)
    occurred_at: datetime
    known_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    resource_id: int | None = Field(default=None, ge=1)
    order_id: int | None = Field(default=None, ge=1)
    operation_id: int | None = Field(default=None, ge=1)
    payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_times(self) -> "EventCreate":
        if self.known_at is not None and self.known_at < self.occurred_at:
            raise ValueError("已知时间不能早于发生时间")
        if self.event_type in {"machine_failure", "material_delay"} and self.duration_seconds is None:
            raise ValueError("机器故障/物料延迟事件需要 duration_seconds")
        return self


class RescheduleRequest(StrictModel):
    strategy: StrategyLiteral = "stability_first"
    running_task_policy: Literal["continue", "replan"] | None = None
    freeze_unaffected: bool = True
    time_limit_seconds: float | None = Field(default=None, gt=0, le=3600)