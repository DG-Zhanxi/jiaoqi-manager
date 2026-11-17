from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from .conftest import (
    create_order,
    create_plan,
    create_resource,
    create_workspace,
    order_payload,
    register_and_login,
    wait_for_job,
)

UTC = timezone.utc


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat()


def _prepare_plan(app):
    admin = register_and_login(app, "admin_user")
    workspace = create_workspace(admin, "审批车间")
    resource = create_resource(admin, workspace["id"], "M1")
    order = create_order(
        admin,
        workspace["id"],
        order_payload(
            "FLOW-001",
            release_at=_now_iso(),
            due_at=_now_iso(7200),
            resources=[resource["id"]],
            durations=[900, 900],
            operations=2,
        ),
    )
    plan = create_plan(admin, workspace["id"], name="审批流计划")
    return admin, workspace, resource, order, plan


def test_plan_lifecycle_self_review_restriction_and_publish(app):
    admin, workspace, resource, order, plan = _prepare_plan(app)
    assert plan["status"] == "draft"
    assert plan["validation"]["valid"] is True
    assert plan["solver_status"] in {"OPTIMAL", "FEASIBLE"}

    submitted = admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"

    self_review = admin.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/review",
        json={"approved": True, "comment": "自我审批"},
    )
    assert self_review.status_code == 403
    assert "自己" in self_review.json()["error"]["message"]

    reviewer = register_and_login(app, "reviewer_user")
    invited = admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "reviewer_user", "role": "reviewer"},
    )
    assert invited.status_code == 201
    approved = reviewer.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/review",
        json={"approved": True, "comment": "交期与机器约束检查通过"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["reviewed_by"] == invited.json()["user"]["id"]

    published = admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/publish")
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "published"
    assert body["published_by"] is not None
    workspace_after = admin.get(f"/api/workspaces/{workspace['id']}").json()
    assert workspace_after["published_plan_id"] == plan["id"]

    audit = admin.get(f"/api/workspaces/{workspace['id']}/audit").json()["items"]
    actions = {item["action"] for item in audit}
    assert {"plan.create", "plan.submit", "plan.approve", "plan.publish"}.issubset(actions)


def test_reviewer_role_required_and_rejection(app):
    admin, workspace, resource, order, plan = _prepare_plan(app)
    planner = register_and_login(app, "planner_peer")
    admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "planner_peer", "role": "planner"},
    )
    admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/submit")
    denied = planner.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/review",
        json={"approved": True},
    )
    assert denied.status_code == 403

    reviewer = register_and_login(app, "reviewer_user")
    admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "reviewer_user", "role": "reviewer"},
    )
    rejected = reviewer.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/review",
        json={"approved": False, "comment": "需要调整"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    publish = admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/publish")
    assert publish.status_code == 409


def test_stale_plan_rejected_on_publish(app):
    admin, workspace, resource, order, plan = _prepare_plan(app)
    admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/submit")
    reviewer = register_and_login(app, "reviewer_user")
    admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "reviewer_user", "role": "reviewer"},
    )
    assert (
        reviewer.post(
            f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/review",
            json={"approved": True},
        ).status_code
        == 200
    )

    changed = admin.put(
        f"/api/workspaces/{workspace['id']}/orders/{order['id']}",
        json={"priority": 5},
    )
    assert changed.status_code == 200, changed.text

    publish = admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/publish")
    assert publish.status_code == 409
    details = publish.json()["error"]["details"]
    assert details["code"] == "stale_plan"
    assert details["plan_data_revision"] != details["workspace_data_revision"]


def test_execution_reporting_transitions_and_no_rollback(app):
    admin, workspace, resource, order, plan = _prepare_plan(app)
    admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/submit")
    reviewer = register_and_login(app, "reviewer_user")
    admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "reviewer_user", "role": "reviewer"},
    )
    reviewer.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/review",
        json={"approved": True},
    )
    assert (
        admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/publish").status_code
        == 200
    )

    operator = register_and_login(app, "operator_user")
    admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "operator_user", "role": "operator"},
    )
    detail = admin.get(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}").json()
    tasks = sorted(detail["tasks"], key=lambda item: item["id"])
    first = tasks[0]
    start_at = _now_iso()
    end_at = _now_iso(600)
    started = operator.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/tasks/{first['id']}/start",
        json={"actual_start_at": start_at},
    )
    assert started.status_code == 200, started.text
    started_task = next(
        item for item in started.json()["tasks"] if item["id"] == first["id"]
    )
    assert started_task["execution_state"] == "running"

    completed = operator.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/tasks/{first['id']}/complete",
        json={"actual_end_at": end_at},
    )
    assert completed.status_code == 200, completed.text
    completed_task = next(
        item for item in completed.json()["tasks"] if item["id"] == first["id"]
    )
    assert completed_task["execution_state"] == "completed"

    again_complete = operator.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/tasks/{first['id']}/complete",
        json={"actual_end_at": _now_iso(1200)},
    )
    assert again_complete.status_code == 409
    again_start = operator.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/tasks/{first['id']}/start",
        json={},
    )
    assert again_start.status_code == 409


def test_event_impact_and_event_reschedule(app):
    admin, workspace, resource, order, plan = _prepare_plan(app)
    admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/submit")
    reviewer = register_and_login(app, "reviewer_user")
    admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "reviewer_user", "role": "reviewer"},
    )
    reviewer.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/review",
        json={"approved": True},
    )
    admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/publish")

    event = admin.post(
        f"/api/workspaces/{workspace['id']}/events",
        json={
            "event_type": "machine_failure",
            "title": "M1 主轴故障",
            "occurred_at": _now_iso(),
            "known_at": _now_iso(),
            "duration_seconds": 3600,
            "resource_id": resource["id"],
        },
    )
    assert event.status_code == 201, event.text
    event_body = event.json()
    impact = event_body["impact"]
    assert impact["plan_id"] == plan["id"]
    assert impact["affected_operation_ids"]
    assert resource["id"] in impact["affected_resource_ids"]

    rescheduled = admin.post(
        f"/api/workspaces/{workspace['id']}/events/{event_body['id']}/reschedule",
        json={
            "strategy": "stability_first",
            "freeze_unaffected": True,
            "time_limit_seconds": 5,
        },
    )
    assert rescheduled.status_code == 200, rescheduled.text
    body = rescheduled.json()
    assert body["impact"]["affected_operation_ids"] == impact["affected_operation_ids"]
    assert body["stages"]
    assert body["plan_id"], body
    assert body["plan"]["status"] == "draft"
    assert body["plan"]["base_plan_id"] == plan["id"]
    assert body["plan"]["reschedule_meta"]["event_id"] == event_body["id"]


def test_event_reschedule_requires_running_policy(app):
    admin, workspace, resource, order, plan = _prepare_plan(app)
    admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/submit")
    reviewer = register_and_login(app, "reviewer_user")
    admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "reviewer_user", "role": "reviewer"},
    )
    reviewer.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/review",
        json={"approved": True},
    )
    admin.post(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/publish")

    operator = register_and_login(app, "operator_user")
    admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "operator_user", "role": "operator"},
    )
    detail = admin.get(f"/api/workspaces/{workspace['id']}/plans/{plan['id']}").json()
    task = sorted(detail["tasks"], key=lambda item: item["id"])[0]
    operator.post(
        f"/api/workspaces/{workspace['id']}/plans/{plan['id']}/tasks/{task['id']}/start",
        json={},
    )
    event = admin.post(
        f"/api/workspaces/{workspace['id']}/events",
        json={
            "event_type": "machine_failure",
            "title": "运行中任务受影响",
            "occurred_at": _now_iso(),
            "known_at": _now_iso(),
            "duration_seconds": 1800,
            "resource_id": resource["id"],
        },
    )
    assert event.status_code == 201, event.text
    response = admin.post(
        f"/api/workspaces/{workspace['id']}/events/{event.json()['id']}/reschedule",
        json={"strategy": "stability_first", "freeze_unaffected": True},
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["code"] == "running_task_policy_required"


def test_plan_diff_reports_changes(app):
    admin, workspace, resource, order, plan = _prepare_plan(app)
    second = create_plan(admin, workspace["id"], strategy="stability_first", base_plan_id=plan["id"])
    diff = admin.get(
        f"/api/workspaces/{workspace['id']}/plans/{second['id']}/diff/{plan['id']}"
    )
    assert diff.status_code == 200
    assert set(diff.json().keys()) >= {
        "added_operations",
        "removed_operations",
        "moved_operations",
        "resource_changes",
    }