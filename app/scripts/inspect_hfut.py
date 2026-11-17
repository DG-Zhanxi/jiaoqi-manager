from pathlib import Path
import hashlib
import sys

import openpyxl

path = sys.argv[1] if len(sys.argv) > 1 else (Path(__file__).resolve().parents[2] / "data/raw/open_alternatives/hfut/a dataset.xlsx")
with open(path, "rb") as fh:
    print("sha256", hashlib.sha256(fh.read()).hexdigest())
wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
print("sheets", wb.sheetnames)
for ws in wb.worksheets:
    print("==", repr(ws.title), ws.max_row, ws.max_column)
    for index, row in enumerate(ws.iter_rows(values_only=True), start=1):
        print(index, row)
        if index >= 6:
            break