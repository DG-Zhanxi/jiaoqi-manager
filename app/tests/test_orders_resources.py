from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .conftest import (
    create_order,
    create_resource,
    create_workspace,
    order_payload,
    register_and_login,
)

UTC = timezone.utc


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat()


def test_order_crud_search_filter_pagination(app):
    client = register_and_login(app, "admin_user")
    workspace = create_workspace(client, "订单车间")
    resource = create_resource(client, workspace["id"], "M1")
    create_order(
        client,
        workspace["id"],
        order_payload(
            "ORD-001",
            release_at=_now_iso(),
            due_at=_now_iso(3600),
            resources=[resource["id"]],
            operations=2,
            priority=3,
        ),
    )
    create_order(
        client,
        workspace["id"],
        order_payload(
            "ORD-002",
            release_at=_now_iso(),
            due_at=None,
            resources=[resource["id"]],
            operations=1,
            priority=1,
        ),
    )

    listing = client.get(
        f"/api/workspaces/{workspace['id']}/orders",
        params={"search": "002", "page": 1, "page_size": 1, "sort_by": "order_no", "sort_dir": "asc"},
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["items"][0]["order_no"] == "ORD-002"

    filtered = client.get(
        f"/api/workspaces/{workspace['id']}/orders", params={"priority_min": 2}
    ).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["order_no"] == "ORD-001"

    duplicate = client.post(
        f"/api/workspaces/{workspace['id']}/orders",
        json=order_payload(
            "ORD-001",
            release_at=_now_iso(),
            due_at=None,
            resources=[resource["id"]],
            operations=1,
        ),
    )
    assert duplicate.status_code == 409


def test_order_rejects_missing_or_inactive_candidates(app):
    client = register_and_login(app, "admin_user")
    workspace = create_workspace(client, "候选校验")
    resource = create_resource(client, workspace["id"], "M1")
    client.patch(
        f"/api/workspaces/{workspace['id']}/resources/{resource['id']}",
        json={"is_active": False},
    )
    response = client.post(
        f"/api/workspaces/{workspace['id']}/orders",
        json=order_payload(
            "BAD-001",
            release_at=_now_iso(),
            due_at=None,
            resources=[resource["id"]],
            operations=1,
        ),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"

    missing = client.post(
        f"/api/workspaces/{workspace['id']}/orders",
        json=order_payload(
            "BAD-002",
            release_at=_now_iso(),
            due_at=None,
            resources=[999],
            operations=1,
        ),
    )
    assert missing.status_code == 422


def test_commitment_preview_does_not_persist_order(app):
    client = register_and_login(app, "admin_user")
    workspace = create_workspace(client, "承诺预览")
    resource = create_resource(client, workspace["id"], "M1")
    create_order(
        client,
        workspace["id"],
        order_payload(
            "BASE-001",
            release_at=_now_iso(),
            due_at=_now_iso(7200),
            resources=[resource["id"]],
            operations=1,
        ),
    )
    preview = client.post(
        f"/api/workspaces/{workspace['id']}/orders/preview?time_limit_seconds=3",
        json=order_payload(
            "PREVIEW-001",
            release_at=_now_iso(),
            due_at=_now_iso(7200),
            resources=[resource["id"]],
            operations=1,
        ),
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["persisted"] is False
    assert body["result"]["status"] in {"OPTIMAL", "FEASIBLE"}
    assert body["result"]["validation"]["valid"] is True

    orders = client.get(f"/api/workspaces/{workspace['id']}/orders").json()
    assert {item["order_no"] for item in orders["items"]} == {"BASE-001"}


def test_downtime_is_respected_by_real_solver(app):
    client = register_and_login(app, "admin_user")
    workspace = create_workspace(client, "停机测试")
    resource = create_resource(client, workspace["id"], "M1")
    create_order(
        client,
        workspace["id"],
        order_payload(
            "DT-001",
            release_at=_now_iso(),
            due_at=None,
            resources=[resource["id"]],
            durations=[1800],
            operations=1,
        ),
    )
    start = datetime.now(UTC) + timedelta(minutes=1)
    end = start + timedelta(hours=2)
    downtime = client.post(
        f"/api/workspaces/{workspace['id']}/resources/{resource['id']}/downtime",
        json={"start_at": start.isoformat(), "end_at": end.isoformat(), "reason": "保养"},
    )
    assert downtime.status_code == 201, downtime.text

    solved = client.post(
        f"/api/workspaces/{workspace['id']}/schedule/solve",
        json={"strategy": "delivery_first", "time_limit_seconds": 5},
    )
    assert solved.status_code == 200, solved.text
    result = solved.json()["result"]
    assert result["status"] in {"OPTIMAL", "FEASIBLE"}
    task = result["tasks"][0]
    task_start = datetime.fromisoformat(task["start_epoch"].__str__()) if False else task["start_epoch"]
    assert task_start >= int(end.timestamp()) or task["end_epoch"] <= int(start.timestamp())


def test_calendar_window_too_short_is_infeasible(app):
    client = register_and_login(app, "admin_user")
    workspace = create_workspace(client, "班次测试")
    resource = client.post(
        f"/api/workspaces/{workspace['id']}/resources",
        json={
            "code": "SHIFT",
            "name": "单小时班次",
            "calendar": [
                {"weekday": day, "start_minute": 0, "end_minute": 60} for day in range(7)
            ],
        },
    ).json()
    create_order(
        client,
        workspace["id"],
        order_payload(
            "CAL-001",
            release_at=_now_iso(),
            due_at=None,
            resources=[resource["id"]],
            durations=[7200],
            operations=1,
        ),
    )
    solved = client.post(
        f"/api/workspaces/{workspace['id']}/schedule/solve",
        json={"strategy": "delivery_first", "time_limit_seconds": 5},
    )
    assert solved.status_code == 200
    assert solved.json()["result"]["status"] == "INFEASIBLE"
    assert solved.json()["result"]["tasks"] == []


def test_material_shortage_blocks_scheduling(app):
    client = register_and_login(app, "admin_user")
    workspace = create_workspace(client, "物料测试")
    resource = create_resource(client, workspace["id"], "M1")
    material = client.post(
        f"/api/workspaces/{workspace['id']}/materials",
        json={
            "name": "钢材",
            "unit": "kg",
            "quantity": 5,
            "available_at": _now_iso(),
        },
    )
    assert material.status_code == 201
    material_id = material.json()["id"]
    for order_no, required in (("MAT-001", 4), ("MAT-002", 4)):
        created = client.post(
            f"/api/workspaces/{workspace['id']}/orders",
            json=order_payload(
                order_no,
                release_at=_now_iso(),
                due_at=_now_iso(7200),
                resources=[resource["id"]],
                operations=1,
                materials=[{"material_id": material_id, "quantity": required}],
            ),
        )
        assert created.status_code == 201, created.text

    solved = client.post(
        f"/api/workspaces/{workspace['id']}/schedule/solve",
        json={"strategy": "delivery_first", "time_limit_seconds": 5},
    )
    assert solved.status_code == 422
    body = solved.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["shortages"]


def test_material_ready_time_is_used_as_release(app):
    client = register_and_login(app, "admin_user")
    workspace = create_workspace(client, "物料就绪")
    resource = create_resource(client, workspace["id"], "M1")
    ready_at = datetime.now(UTC) + timedelta(hours=1)
    material = client.post(
        f"/api/workspaces/{workspace['id']}/materials",
        json={
            "name": "铝材",
            "unit": "kg",
            "quantity": 10,
            "available_at": ready_at.isoformat(),
        },
    ).json()
    client.post(
        f"/api/workspaces/{workspace['id']}/orders",
        json=order_payload(
            "READY-001",
            release_at=_now_iso(),
            due_at=_now_iso(7200),
            resources=[resource["id"]],
            operations=1,
            materials=[{"material_id": material["id"], "quantity": 4}],
        ),
    )
    solved = client.post(
        f"/api/workspaces/{workspace['id']}/schedule/solve",
        json={"strategy": "delivery_first", "time_limit_seconds": 5},
    )
    assert solved.status_code == 200, solved.text
    task = solved.json()["result"]["tasks"][0]
    assert task["start_epoch"] >= int(ready_at.timestamp())