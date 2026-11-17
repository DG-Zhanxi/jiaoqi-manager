"""合肥工业大学 FJSP-LS 开放数据适配器。

设计约束（与 docs/PRODUCT_REQUIREMENTS.md 对齐）：
* 只解析工作簿中的真实单元格，不生成或替换加工时间。
* 保留 workbook SHA-256、工作表、源列映射和行号，便于审计。
* 课时（lot）在本期模型中不拆分。
* Exam 工作表：duration = Unit Time * LotSize；Setup Time 每个工序计一次。
* 标准工作表：Timespan/proc-time 视为该 lot 的工序加工时间，直接使用。
* Changeover Time 原样保留在映射与工序元数据中，但当前按 lot 不拆分模型
  不把它加入任务时长，需在报告中明确说明。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable

import openpyxl
from openpyxl.utils import get_column_letter

HFUT_DOI = "10.17632/mwrgpx7hhp.3"
HFUT_LICENSE = "CC BY 4.0"
HFUT_ATTRIBUTION = "Hefei University of Technology (HFUT)"


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _int_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raw = str(value).strip()
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[+-]?\d+\.0+", raw):
        return int(float(raw))
    return None


def _float_value(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    try:
        return float(raw)
    except ValueError:
        return None


def _seconds(value: Any, lot_size: int = 1) -> int | None:
    parsed = _float_value(value)
    if parsed is None:
        return None
    result = Decimal(str(parsed)) * Decimal(60) * Decimal(lot_size)
    return int(result.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class HfutInstance:
    sheet: str
    label: str
    kind: str
    lot_column_index: int
    lot_header: str
    side_job_column_index: int

    def to_dict(self) -> dict:
        return {
            "sheet": self.sheet,
            "instance": self.label,
            "kind": self.kind,
            "lot_column": get_column_letter(self.lot_column_index + 1),
            "lot_header": self.lot_header,
        }


@dataclass(frozen=True)
class HfutMachineOption:
    machine_code: str
    duration_seconds: int
    source_machine: str
    source_time: float
    unit_time: float | None
    lot_size: int
    source_row: int

    def to_dict(self) -> dict:
        return {
            "machine_code": self.machine_code,
            "duration_seconds": self.duration_seconds,
            "source_machine": self.source_machine,
            "source_time": self.source_time,
            "unit_time": self.unit_time,
            "lot_size": self.lot_size,
            "source_row": self.source_row,
        }


@dataclass(frozen=True)
class HfutOperation:
    job_key: str
    operation_key: str
    sequence: int
    setup_seconds: int
    changeover_raw: str | None
    source_row: int
    candidates: tuple[HfutMachineOption, ...]

    def to_dict(self) -> dict:
        return {
            "job_key": self.job_key,
            "operation_key": self.operation_key,
            "sequence": self.sequence,
            "setup_seconds": self.setup_seconds,
            "changeover_raw": self.changeover_raw,
            "source_row": self.source_row,
            "candidates": [item.to_dict() for item in self.candidates],
        }


@dataclass(frozen=True)
class HfutJobRow:
    job_key: str
    lot_size: int
    source_row: int

    def to_dict(self) -> dict:
        return {"job_key": self.job_key, "lot_size": self.lot_size, "source_row": self.source_row}


@dataclass
class HfutParseResult:
    workbook_sha256: str
    source_uri: str
    sheet: str
    instance_key: str
    kind: str
    jobs: list[HfutJobRow] = field(default_factory=list)
    operations: list[HfutOperation] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    row_counts: dict = field(default_factory=dict)
    mapping: dict = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return bool(self.jobs) and bool(self.operations) and not self.errors

    @property
    def status(self) -> str:
        if self.valid:
            return "valid"
        if self.jobs or self.operations:
            return "partial"
        return "invalid"


class HfutAdapter:
    def __init__(self, workbook_path: str | Path):
        self.workbook_path = Path(workbook_path)

    def _hash(self) -> str:
        digest = hashlib.sha256()
        with self.workbook_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _load(self):
        if not self.workbook_path.exists():
            raise FileNotFoundError(f"HFUT 数据文件不存在：{self.workbook_path}")
        return openpyxl.load_workbook(self.workbook_path, read_only=True, data_only=True)

    @staticmethod
    def _layout(headers: list[Any]) -> tuple[str, int, int, list[int]]:
        kind = "exam" if _norm(headers[0] if headers else None) == "setuptime" else "standard"
        main_count = 6 if kind == "exam" else 4
        side_job = None
        for index in range(main_count, len(headers)):
            if _norm(headers[index]) in {"job", "lot"}:
                side_job = index
                break
        if side_job is None:
            return kind, main_count, -1, []
        lot_columns: list[int] = []
        for index in range(side_job + 1, len(headers)):
            if _norm(headers[index]).startswith("lotsize"):
                lot_columns.append(index)
        return kind, main_count, side_job, lot_columns

    @staticmethod
    def _labels(sheet: str, headers: list[Any], lot_columns: list[int]) -> list[str]:
        sheet_parts = [item.strip() for item in sheet.split(",") if item.strip()]
        if len(lot_columns) == 1:
            return [sheet]
        labels: list[str] = []
        for position, index in enumerate(lot_columns, start=1):
            if len(sheet_parts) == 1:
                labels.append(f"{sheet}-{position}")
                continue
            header = _text(headers[index])
            digits = re.search(r"(\d+)$", header)
            matched = None
            if digits:
                for part in sheet_parts:
                    part_digits = re.search(r"(\d+)$", part)
                    if part_digits and int(part_digits.group(1)) == int(digits.group(1)):
                        matched = part
                        break
            labels.append(matched or f"{sheet}-{position}")
        return labels

    def available_instances(self) -> list[HfutInstance]:
        workbook = self._load()
        try:
            instances: list[HfutInstance] = []
            for worksheet in workbook.worksheets:
                headers = list(next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True)))
                kind, _main_count, side_job, lot_columns = self._layout(headers)
                if side_job < 0 or not lot_columns:
                    continue
                labels = self._labels(worksheet.title, headers, lot_columns)
                for label, column_index in zip(labels, lot_columns):
                    instances.append(
                        HfutInstance(
                            sheet=worksheet.title,
                            label=label,
                            kind=kind,
                            lot_column_index=column_index,
                            lot_header=_text(headers[column_index]),
                            side_job_column_index=side_job,
                        )
                    )
            return instances
        finally:
            workbook.close()

    def parse(self, sheet: str | None = None, instance: str | None = None) -> HfutParseResult:
        instances = self.available_instances()
        selected: HfutInstance | None = None
        if instance:
            for candidate in instances:
                if candidate.label.lower() == instance.strip().lower():
                    selected = candidate
                    break
            if selected is None:
                raise ValueError(f"未找到 HFUT 实例：{instance}")
        elif sheet:
            matches = [item for item in instances if item.sheet.lower() == sheet.strip().lower()]
            if not matches:
                raise ValueError(f"未找到 HFUT 工作表：{sheet}")
            if len(matches) > 1:
                labels = ", ".join(item.label for item in matches)
                raise ValueError(f"工作表 {sheet} 包含多个实例（{labels}），请指定 instance")
            selected = matches[0]
        else:
            raise ValueError("必须提供 sheet 或 instance")

        workbook = self._load()
        try:
            worksheet = workbook[selected.sheet]
            rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
        finally:
            workbook.close()
        return self._parse_rows(selected, rows)

    def _parse_rows(self, selected: HfutInstance, rows: list[list[Any]]) -> HfutParseResult:
        if not rows:
            raise ValueError("工作表为空")
        headers = rows[0]
        errors: list[dict] = []
        side_lots: dict[str, tuple[int, int]] = {}
        side_job_index = selected.side_job_column_index
        lot_index = selected.lot_column_index

        for row_number, row in enumerate(rows[1:], start=2):
            if side_job_index >= len(row):
                continue
            raw_job = row[side_job_index] if side_job_index < len(row) else None
            raw_lot = row[lot_index] if lot_index < len(row) else None
            if raw_job is None and raw_lot is None:
                if side_lots:
                    break
                continue
            if isinstance(raw_job, str) and _norm(raw_job) == "total":
                break
            job_key = _text(raw_job)
            lot_size = _int_value(raw_lot)
            if not job_key:
                continue
            if lot_size is None or lot_size <= 0:
                errors.append(
                    {
                        "row": row_number,
                        "code": "invalid_lot_size",
                        "message": f"作业 {job_key} 的 LotSize 无效",
                        "field": selected.lot_header,
                    }
                )
                continue
            side_lots[job_key] = (lot_size, row_number)

        operations: dict[tuple[str, str], dict] = {}
        last_job: str | None = None
        last_operation: str | None = None
        main_rows = 0
        skipped_rows = 0

        for row_number, row in enumerate(rows[1:], start=2):
            row = list(row) + [None] * max(0, 7 - len(row))

            if selected.kind == "exam":
                setup_raw, changeover_raw = row[0], row[1]
                job_raw, operation_raw, machine_raw, unit_raw = row[2], row[3], row[4], row[5]
                time_raw = unit_raw
            else:
                job_raw, operation_raw, machine_raw, time_raw = row[0], row[1], row[2], row[3]
                setup_raw, changeover_raw = None, None

            if all(value is None or value == "" for value in (job_raw, operation_raw, machine_raw, time_raw)):
                skipped_rows += 1
                continue

            main_rows += 1
            if job_raw is not None and str(job_raw).strip():
                current_job = _text(job_raw)
                last_job = current_job
            else:
                current_job = last_job
            if operation_raw is not None and str(operation_raw).strip():
                current_operation = _text(operation_raw)
                last_operation = current_operation
            else:
                current_operation = last_operation

            if not current_job or not current_operation:
                errors.append(
                    {
                        "row": row_number,
                        "code": "missing_job_or_operation",
                        "message": "无法确定 Job/Operation（续行缺少可回填的上下文）",
                    }
                )
                continue

            sequence = _int_value(current_operation)
            if sequence is None:
                errors.append(
                    {
                        "row": row_number,
                        "code": "invalid_operation",
                        "message": f"Operation 不是整数：{current_operation}",
                    }
                )
                continue

            numeric_time = _float_value(time_raw)
            machine_key = _text(machine_raw)
            if not machine_key:
                errors.append(
                    {
                        "row": row_number,
                        "code": "missing_machine",
                        "message": "缺少机器编号",
                    }
                )
                continue
            if numeric_time is None or numeric_time <= 0:
                errors.append(
                    {
                        "row": row_number,
                        "code": "invalid_process_time",
                        "message": f"加工时间无效：{time_raw!r}",
                    }
                )
                continue

            lot = side_lots.get(current_job)
            if lot is None:
                errors.append(
                    {
                        "row": row_number,
                        "code": "missing_lot_size",
                        "message": f"作业 {current_job} 缺少 LotSize",
                    }
                )
                continue
            lot_size = lot[0]

            if selected.kind == "exam":
                duration = _seconds(numeric_time, lot_size)
                unit_time = numeric_time
            else:
                duration = _seconds(numeric_time, 1)
                unit_time = None
            if duration is None or duration <= 0:
                errors.append(
                    {
                        "row": row_number,
                        "code": "invalid_duration",
                        "message": f"无法换算加工时长：{time_raw!r}",
                    }
                )
                continue

            key = (current_job, current_operation)
            operation = operations.get(key)
            if operation is None:
                setup_seconds = 0
                if selected.kind == "exam":
                    parsed_setup = _seconds(setup_raw, 1)
                    if setup_raw is not None and parsed_setup is None:
                        errors.append(
                            {
                                "row": row_number,
                                "code": "invalid_setup_time",
                                "message": f"Setup Time 无效：{setup_raw!r}",
                            }
                        )
                    setup_seconds = parsed_setup or 0
                operation = {
                    "job_key": current_job,
                    "operation_key": current_operation,
                    "sequence": sequence,
                    "setup_seconds": setup_seconds,
                    "changeover_raw": _text(changeover_raw) or None,
                    "source_row": row_number,
                    "candidates": {},
                }
                operations[key] = operation
            else:
                if selected.kind == "exam" and setup_raw is not None:
                    parsed_setup = _seconds(setup_raw, 1)
                    if parsed_setup is not None and parsed_setup != operation["setup_seconds"]:
                        errors.append(
                            {
                                "row": row_number,
                                "code": "setup_conflict",
                                "message": "同一工序出现不一致的 Setup Time，已保留首次值",
                                "field": "Setup Time",
                            }
                        )
                if operation["changeover_raw"] is None and _text(changeover_raw):
                    operation["changeover_raw"] = _text(changeover_raw)

            candidates = operation["candidates"]
            if machine_key in candidates:
                errors.append(
                    {
                        "row": row_number,
                        "code": "duplicate_candidate",
                        "message": f"工序 {current_job}/{current_operation} 的机器 {machine_key} 重复",
                    }
                )
                continue
            candidates[machine_key] = HfutMachineOption(
                machine_code=machine_key if machine_key.upper().startswith("M") else f"M{machine_key}",
                duration_seconds=duration,
                source_machine=machine_key,
                source_time=numeric_time,
                unit_time=unit_time,
                lot_size=lot_size,
                source_row=row_number,
            )

        job_rows: list[HfutJobRow] = []
        for job_key, (lot_size, source_row) in side_lots.items():
            job_operations = [item for key, item in operations.items() if key[0] == job_key]
            if job_operations:
                job_rows.append(HfutJobRow(job_key=job_key, lot_size=lot_size, source_row=source_row))

        def sort_key(value: str) -> tuple[int, str]:
            parsed = _int_value(value)
            return (parsed if parsed is not None else 10**12, value)

        job_rows.sort(key=lambda item: sort_key(item.job_key))
        operation_list: list[HfutOperation] = []
        for job in job_rows:
            job_operations = [item for key, item in operations.items() if key[0] == job.job_key]
            job_operations.sort(key=lambda item: (item["sequence"], item["operation_key"]))
            for item in job_operations:
                candidates = tuple(sorted(item["candidates"].values(), key=lambda c: c.machine_code))
                operation_list.append(
                    HfutOperation(
                        job_key=item["job_key"],
                        operation_key=item["operation_key"],
                        sequence=item["sequence"],
                        setup_seconds=item["setup_seconds"],
                        changeover_raw=item["changeover_raw"],
                        source_row=item["source_row"],
                        candidates=candidates,
                    )
                )

        for job in job_rows:
            if not any(item.job_key == job.job_key for item in operation_list):
                errors.append(
                    {
                        "row": job.source_row,
                        "code": "job_without_operations",
                        "message": f"作业 {job.job_key} 没有有效工序",
                    }
                )

        column_letters = {
            "job": get_column_letter((2 if selected.kind == "exam" else 0) + 1),
            "operation": get_column_letter((3 if selected.kind == "exam" else 1) + 1),
            "machine": get_column_letter((4 if selected.kind == "exam" else 2) + 1),
            "time": get_column_letter((5 if selected.kind == "exam" else 3) + 1),
            "side_job": get_column_letter(side_job_index + 1),
            "lot_size": get_column_letter(lot_index + 1),
        }
        if selected.kind == "exam":
            column_letters["setup_time"] = "A"
            column_letters["changeover_time"] = "B"

        mapping = {
            "workbook_sha256": self._hash(),
            "source_uri": str(self.workbook_path),
            "source_doi": HFUT_DOI,
            "license": HFUT_LICENSE,
            "attribution": HFUT_ATTRIBUTION,
            "sheet": selected.sheet,
            "instance": selected.label,
            "kind": selected.kind,
            "source_fields": column_letters,
            "side_table": {
                "job_column": column_letters["side_job"],
                "lot_column": column_letters["lot_size"],
                "lot_header": selected.lot_header,
            },
            "duration_policy": (
                "Exam 工作表：duration = Unit Time × LotSize；"
                "标准工作表：Timespan/proc-time 视为该 lot 的工序时间，直接采用。"
            ),
            "setup_policy": (
                "Exam 工作表：Setup Time 每个工序计一次；"
                "标准工作表无 Setup 列，按 0 处理。"
                "Changeover Time 保留源值但不纳入本轮不拆分 lot 的任务时长。"
            ),
            "transformations": [
                "Job/Operation 续行按前一行回填（仅候选机器续行）",
                "加工时间换算为整数秒，使用 Decimal 四舍五入",
                "未拆分 lot，未建模换型序列依赖",
            ],
            "reference_comparability": (
                "HFUT FJSP-LS 原始结果包含 lot 拆分策略，不能直接与本模型的不拆分 lot 结果比较。"
            ),
        }

        row_counts = {
            "source_rows": max(0, len(rows) - 1),
            "main_rows": main_rows,
            "skipped_rows": skipped_rows,
            "jobs": len(job_rows),
            "operations": len(operation_list),
            "candidate_machines": sum(len(item.candidates) for item in operation_list),
            "errors": len(errors),
        }
        return HfutParseResult(
            workbook_sha256=mapping["workbook_sha256"],
            source_uri=str(self.workbook_path),
            sheet=selected.sheet,
            instance_key=selected.label,
            kind=selected.kind,
            jobs=job_rows,
            operations=operation_list,
            errors=errors,
            row_counts=row_counts,
            mapping=mapping,
        )


def iter_instance_labels(workbook_path: str | Path) -> Iterable[str]:
    return (item.label for item in HfutAdapter(workbook_path).available_instances())