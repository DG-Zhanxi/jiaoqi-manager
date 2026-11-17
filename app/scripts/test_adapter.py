import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pathlib import Path
from backend.adapters.hfut import HfutAdapter

adapter = HfutAdapter((Path(__file__).resolve().parents[2] / "data/raw/open_alternatives/hfut/a dataset.xlsx"))
print([(i.label, i.sheet, i.kind, i.lot_header) for i in adapter.available_instances()])
result = adapter.parse(instance="Inst01")
print(result.status, result.row_counts, [(j.job_key, j.lot_size) for j in result.jobs])
print(
    [
        (
            o.job_key,
            o.operation_key,
            [(c.machine_code, c.duration_seconds) for c in o.candidates],
        )
        for o in result.operations
    ]
)
exam = adapter.parse(instance="Exam01")
print("exam", exam.status, exam.row_counts, len(exam.jobs), len(exam.operations))
print(exam.operations[0])