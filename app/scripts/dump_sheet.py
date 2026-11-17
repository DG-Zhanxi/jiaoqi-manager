from pathlib import Path
import sys

import openpyxl

path = (Path(__file__).resolve().parents[2] / "data/raw/open_alternatives/hfut/a dataset.xlsx")
wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
for name in sys.argv[1:] or ["Inst01"]:
    ws = wb[name]
    print("==", name, ws.max_row, ws.max_column)
    for index, row in enumerate(ws.iter_rows(values_only=True), start=1):
        print(index, row)