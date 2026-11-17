from backend.scheduling import Candidate, Operation, Order, Resource, Scenario, solve
from backend.timeutil import epoch, utcnow

start = 1_700_000_000
end = start + 10_000

ops = (
    Operation(
        operation_id=1,
        order_id=1,
        order_no="A",
        name="op1",
        sequence=1,
        setup_seconds=0,
        release_epoch=start,
        due_epoch=start + 5000,
        priority=1,
        weight=1,
        candidates=(Candidate(1, 600), Candidate(2, 900)),
    ),
    Operation(
        operation_id=2,
        order_id=1,
        order_no="A",
        name="op2",
        sequence=2,
        setup_seconds=60,
        release_epoch=start,
        due_epoch=start + 5000,
        priority=1,
        weight=1,
        candidates=(Candidate(2, 600),),
    ),
    Operation(
        operation_id=3,
        order_id=2,
        order_no="B",
        name="op1",
        sequence=1,
        setup_seconds=0,
        release_epoch=start,
        due_epoch=None,
        priority=0,
        weight=1,
        candidates=(Candidate(1, 300),),
    ),
)
scenario = Scenario(
    operations=ops,
    resources=(Resource(1, "M1"), Resource(2, "M2")),
    orders=(
        Order(1, "A", start, start + 5000, 1),
        Order(2, "B", start, None, 0),
    ),
    horizon_start_epoch=start,
    horizon_end_epoch=end,
)
for strategy in ("delivery_first", "stability_first"):
    result = solve(scenario, strategy=strategy, time_limit_seconds=5)
    print(strategy, result.status, result.objective_value, result.validation["valid"])
    for task in result.tasks:
        print(task)
    print(result.metrics)

infeasible = Scenario(
    operations=(
        Operation(
            1,
            1,
            "A",
            "op1",
            1,
            0,
            start,
            None,
            0,
            1,
            (Candidate(1, 600),),
            fixed_start_epoch=start + 100,
            fixed_resource_id=1,
        ),
        Operation(
            2,
            2,
            "B",
            "op1",
            1,
            0,
            start,
            None,
            0,
            1,
            (Candidate(1, 600),),
            fixed_start_epoch=start + 200,
            fixed_resource_id=1,
        ),
    ),
    resources=(Resource(1, "M1"),),
    orders=(Order(1, "A", start, None, 0), Order(2, "B", start, None, 0)),
    horizon_start_epoch=start,
    horizon_end_epoch=end,
)
result = solve(infeasible, time_limit_seconds=2)
print("infeasible", result.status, result.message)