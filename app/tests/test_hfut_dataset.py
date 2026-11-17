from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from backend.adapters.hfut import HfutAdapter

from .conftest import create_order, create_resource, create_workspace, register_and_login

HFUT_PATH = Path(__file__).resolve().parents[2] / "data/raw/open_alternatives/hfut/a dataset.xlsx"
EXPECTED_SHA256 = "e8d07c0a99db42445f85ed4d7b431570d7e7a89bf62e029b5f51a7ec0d257984"


@pytest.mark.skipif(not HFUT_PATH.exists(), reason="HFUT source workbook is not available")
def test_hfut_adapter_parses_real_cells_and_source_metadata():
    adapter = HfutAdapter(HFUT_PATH)
    instances = adapter.available_instances()
    labels = {item.label for item in instances}
    assert {"Inst01", "Inst03", "Inst06", "Inst09", "Inst11", "Inst10", "Inst12", "P3-1"}.issubset(labels)

    result = adapter.parse(instance="Inst01")
    assert result.status == "valid"
    assert result.workbook_sha256 == EXPECTED_SHA256
    assert result.row_counts["jobs"] == 3
    assert result.row_counts["operations"] == 8
    assert result.row_counts["candidate_machines"] == 9
    assert result.mapping["source_doi"] == "10.17632/mwrgpx7hhp.3"
    assert result.mapping["license"] == "CC BY 4.0"
    assert result.mapping["source_fields"]["machine"] == "C"
    job1_op1 = next(
        item for item in result.operations if item.job_key == "1" and item.operation_key == "1"
    )
    assert job1_op1.candidates[0].machine_code == "M1"
    assert job1_op1.candidates[0].duration_seconds == 180
    assert job1_op1.candidates[0].source_time == 3
    assert job1_op1.candidates[0].source_row == 2


@pytest.mark.skipif(not HFUT_PATH.exists(), reason="HFUT source workbook is not available")
def test_hfut_exam_uses_unit_time_times_lot_and_setup():
    adapter = HfutAdapter(HFUT_PATH)
    result = adapter.parse(instance="Exam01")
    assert result.status == "valid"
    assert result.kind == "exam"
    assert result.row_counts["jobs"] == 10
    assert result.row_counts["operations"] == 30
    assert result.row_counts["candidate_machines"] == 101
    first = result.operations[0]
    assert first.job_key == "1"
    assert first.setup_seconds == 40 * 60
    assert first.changeover_raw == "(20,23,22)"
    assert first.candidates[0].unit_time == 20
    assert first.candidates[0].lot_size == 3
    assert first.candidates[0].duration_seconds == 20 * 3 * 60


@pytest.mark.skipif(not HFUT_PATH.exists(), reason="HFUT source workbook is not available")
def test_hfut_shared_sheets_use_distinct_lot_size_columns():
    adapter = HfutAdapter(HFUT_PATH)
    inst03 = adapter.parse(instance="Inst03")
    inst06 = adapter.parse(instance="Inst06")
    assert [job.lot_size for job in inst03.jobs] == [9, 20, 25, 6]
    assert [job.lot_size for job in inst06.jobs] == [32, 47, 20, 17]
    assert inst03.sheet == inst06.sheet == "Inst03, Inst06"
    assert inst03.mapping["side_table"]["lot_header"] == "lotSize_03"
    assert inst06.mapping["side_table"]["lot_header"] == "lotSize_06"


def test_hfut_adapter_reports_invalid_source_rows(tmp_path):
    path = tmp_path / "bad.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Bad01"
    sheet.append(["Job", "Operation", "Machine", "Timespan", None, None, None, None, "Job", "LotSize"])
    sheet.append([1, 1, 1, 3, None, None, None, None, 1, 10])
    sheet.append([None, None, None, 5, None, None, None, None, None, None])
    sheet.append([2, 1, 1, 4, None, None, None, None, 2, 10])
    workbook.save(path)

    result = HfutAdapter(path).parse(instance="Bad01")
    assert result.status == "partial"
    assert any(error["code"] == "missing_machine" and error["row"] == 3 for error in result.errors)
    assert result.row_counts["errors"] >= 1


@pytest.mark.skipif(not HFUT_PATH.exists(), reason="HFUT source workbook is not available")
def test_dataset_import_and_apply_via_api(app):
    client = register_and_login(app, "admin_user")
    workspace = create_workspace(client, "HFUT 车间")

    sources = client.get(f"/api/workspaces/{workspace['id']}/datasets/sources")
    assert sources.status_code == 200
    labels = {item["instance"] for item in sources.json()["items"]}
    assert "Inst01" in labels

    imported = client.post(
        f"/api/workspaces/{workspace['id']}/datasets/import/hfut",
        json={"instance": "Inst01"},
    )
    assert imported.status_code == 201, imported.text
    dataset = imported.json()
    assert dataset["status"] == "valid"
    assert dataset["workbook_sha256"] == EXPECTED_SHA256
    assert dataset["source_doi"] == "10.17632/mwrgpx7hhp.3"
    assert dataset["license"] == "CC BY 4.0"
    assert dataset["row_counts"]["candidate_machines"] == 9
    assert len(dataset["jobs"]) == 3

    applied = client.post(
        f"/api/workspaces/{workspace['id']}/datasets/{dataset['id']}/apply",
        json={"release_at": "2026-01-01T00:00:00Z", "due_at": "2026-01-10T00:00:00Z"},
    )
    assert applied.status_code == 201, applied.text
    body = applied.json()
    assert body["order_count"] == 3
    assert {order["external_job_key"] for order in body["orders"]} == {"1", "2", "3"}
    order = next(item for item in body["orders"] if item["external_job_key"] == "1")
    assert order["quantity"] == 10
    assert len(order["operations"]) == 3
    assert order["operations"][0]["candidates"][0]["duration_seconds"] == 180

    resources = client.get(f"/api/workspaces/{workspace['id']}/resources").json()["items"]
    assert {item["code"] for item in resources} == {"M1", "M2", "M3"}

    again = client.post(
        f"/api/workspaces/{workspace['id']}/datasets/{dataset['id']}/apply",
        json={},
    )
    assert again.status_code == 409


@pytest.mark.skipif(not HFUT_PATH.exists(), reason="HFUT source workbook is not available")
def test_hfut_orders_can_be_solved_after_apply(app):
    client = register_and_login(app, "admin_user")
    workspace = create_workspace(client, "HFUT 排程")
    dataset = client.post(
        f"/api/workspaces/{workspace['id']}/datasets/import/hfut",
        json={"instance": "Inst01"},
    ).json()
    client.post(
        f"/api/workspaces/{workspace['id']}/datasets/{dataset['id']}/apply",
        json={"release_at": "2026-01-01T00:00:00Z"},
    )

    response = client.post(
        f"/api/workspaces/{workspace['id']}/schedule/solve",
        json={"strategy": "delivery_first", "time_limit_seconds": 5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["status"] in {"OPTIMAL", "FEASIBLE"}
    assert body["result"]["validation"]["valid"] is True
    assert len(body["result"]["tasks"]) == 8