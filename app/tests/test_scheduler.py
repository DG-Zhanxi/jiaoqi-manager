from __future__ import annotations

from backend.scheduling import (
    BaseTask,
    Candidate,
    Operation,
    Order,
    Resource,
    Scenario,
    ScheduledTask,
    solve,
    validate,
)


def _op(
    operation_id,
    order_id,
    sequence,
    candidates,
    *,
    release=0,
    due=None,
    setup=0,
    status="planned",
    actual_start=None,
    actual_end=None,
    fixed_start=None,
    fixed_resource=None,
    order_no=None,
):
    return Operation(
        operation_id=operation_id,
        order_id=order_id,
        order_no=order_no or f"O{order_id}",
        name=f"op{sequence}",
        sequence=sequence,
        setup_seconds=setup,
        release_epoch=release,
        due_epoch=due,
        priority=0,
        weight=1,
        candidates=tuple(Candidate(resource_id, duration) for resource_id, duration in candidates),
        fixed_start_epoch=fixed_start,
        fixed_resource_id=fixed_resource,
        status=status,
        actual_start_epoch=actual_start,
        actual_end_epoch=actual_end,
    )


def test_solver_respects_precedence_release_and_downtime():
    start = 1_700_000_000
    scenario = Scenario(
        operations=(
            _op(1, 1, 1, [(1, 600)], release=start + 1200),
            _op(2, 1, 2, [(1, 600)]),
            _op(3, 2, 1, [(1, 600)]),
        ),
        resources=(Resource(1, "M1", downtime_windows=((start, start + 1200),)),),
        orders=(
            Order(1, "O1", start + 1200, None, 0),
            Order(2, "O2", start, None, 0),
        ),
        horizon_start_epoch=start,
        horizon_end_epoch=start + 10000,
    )
    result = solve(scenario, strategy="delivery_first", time_limit_seconds=5)
    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert result.validation["valid"] is True
    tasks = {item.operation_id: item for item in result.tasks}
    assert tasks[1].start_epoch >= start + 1200
    assert tasks[1].end_epoch <= tasks[2].start_epoch
    assert tasks[2].end_epoch <= tasks[3].start_epoch or tasks[3].end_epoch <= tasks[1].start_epoch


def test_solver_reports_infeasible_not_unknown_for_fixed_overlap():
    start = 1_700_000_000
    scenario = Scenario(
        operations=(
            _op(1, 1, 1, [(1, 600)], fixed_start=start + 100, fixed_resource=1),
            _op(2, 2, 1, [(1, 600)], fixed_start=start + 200, fixed_resource=1),
        ),
        resources=(Resource(1, "M1"),),
        orders=(Order(1, "O1", start, None, 0), Order(2, "O2", start, None, 0)),
        horizon_start_epoch=start,
        horizon_end_epoch=start + 5000,
    )
    result = solve(scenario, strategy="delivery_first", time_limit_seconds=3)
    assert result.status == "INFEASIBLE"
    assert result.tasks == ()
    assert result.validation == {}


def test_frozen_and_completed_tasks_remain_fixed():
    start = 1_700_000_000
    scenario = Scenario(
        operations=(
            _op(
                1,
                1,
                1,
                [(1, 600)],
                status="completed",
                actual_start=start,
                actual_end=start + 550,
            ),
            _op(2, 1, 2, [(1, 600)]),
            _op(3, 2, 1, [(1, 300)]),
        ),
        resources=(Resource(1, "M1"),),
        orders=(Order(1, "O1", start, None, 0), Order(2, "O2", start, None, 0)),
        horizon_start_epoch=start,
        horizon_end_epoch=start + 10000,
        base_tasks=(
            BaseTask(2, 1, start + 2000, start + 2600),
            BaseTask(3, 1, start + 600, start + 900),
        ),
        frozen_operation_ids=frozenset({2, 3}),
    )
    result = solve(scenario, strategy="stability_first", time_limit_seconds=5)
    assert result.status in {"OPTIMAL", "FEASIBLE"}
    tasks = {item.operation_id: item for item in result.tasks}
    assert tasks[1].start_epoch == start
    assert tasks[1].end_epoch == start + 550
    assert tasks[2].start_epoch == start + 2000
    assert tasks[3].end_epoch <= tasks[2].start_epoch or tasks[2].end_epoch <= tasks[3].start_epoch


def test_timeout_returns_unknown_without_fabricated_plan():
    start = 1_700_000_000
    operations = []
    for index in range(1, 80):
        operations.append(
            _op(
                index,
                index,
                1,
                [(1, 600), (2, 700), (3, 800)],
                release=start,
                due=start + 5000,
            )
        )
    scenario = Scenario(
        operations=tuple(operations),
        resources=(Resource(1, "M1"), Resource(2, "M2"), Resource(3, "M3")),
        orders=tuple(Order(index, f"O{index}", start, start + 5000, 0) for index in range(1, 80)),
        horizon_start_epoch=start,
        horizon_end_epoch=start + 50000,
    )
    result = solve(scenario, strategy="delivery_first", time_limit_seconds=0.001)
    assert result.status != "INFEASIBLE"
    assert result.status in {"UNKNOWN", "FEASIBLE", "OPTIMAL"}
    if result.status == "UNKNOWN":
        assert result.tasks == ()
        assert result.has_solution is False


def test_independent_validator_catches_tampering():
    start = 1_700_000_000
    scenario = Scenario(
        operations=(
            _op(1, 1, 1, [(1, 600), (2, 600)], order_no="A"),
            _op(2, 1, 2, [(1, 600)], order_no="A"),
        ),
        resources=(
            Resource(1, "M1", downtime_windows=((start + 1200, start + 1800),)),
            Resource(2, "M2"),
        ),
        orders=(Order(1, "A", start, None, 0),),
        horizon_start_epoch=start,
        horizon_end_epoch=start + 10000,
    )
    good = [
        ScheduledTask(1, 1, start, start + 600, 600),
        ScheduledTask(2, 1, start + 600, start + 1200, 600),
    ]
    assert validate(scenario, good)["valid"] is True

    overlap = [
        ScheduledTask(1, 1, start + 100, start + 700, 600),
        ScheduledTask(2, 1, start + 200, start + 800, 600),
    ]
    result = validate(scenario, overlap)
    assert result["valid"] is False
    assert any(error["code"] == "resource_overlap" for error in result["errors"])
    assert any(error["code"] == "precedence_violation" for error in result["errors"])

    ineligible = [
        ScheduledTask(1, 3, start, start + 600, 600),
        ScheduledTask(2, 1, start + 600, start + 1200, 600),
    ]
    errors = validate(scenario, ineligible)["errors"]
    assert any(error["code"] in {"ineligible_resource", "unknown_or_inactive_resource"} for error in errors)

    downtime = [
        ScheduledTask(1, 1, start + 1300, start + 1900, 600),
        ScheduledTask(2, 2, start, start + 600, 600),
    ]
    assert any(error["code"] == "downtime_overlap" for error in validate(scenario, downtime)["errors"])

    bad_duration = [
        ScheduledTask(1, 1, start, start + 500, 500),
        ScheduledTask(2, 1, start + 600, start + 1200, 600),
    ]
    assert any(error["code"] == "duration_mismatch" for error in validate(scenario, bad_duration)["errors"])


def test_calendar_window_too_short_is_infeasible_in_engine():
    start = 1_700_000_000
    windows = tuple((start + day * 86400, start + day * 86400 + 1800) for day in range(3))
    scenario = Scenario(
        operations=(_op(1, 1, 1, [(1, 3600)]),),
        resources=(Resource(1, "SHIFT", availability_windows=windows, calendar_defined=True),),
        orders=(Order(1, "O1", start, None, 0),),
        horizon_start_epoch=start,
        horizon_end_epoch=start + 3 * 86400,
    )
    result = solve(scenario, strategy="delivery_first", time_limit_seconds=3)
    assert result.status == "INFEASIBLE"