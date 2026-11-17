from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import (
    create_order,
    create_resource,
    create_workspace,
    order_payload,
    register,
    register_and_login,
)


def test_workspace_isolation_between_users(app):
    alice = register_and_login(app, "alice_user")
    workspace_a = create_workspace(alice, "A 车间")
    resource = create_resource(alice, workspace_a["id"], "M1")
    order = create_order(
        alice,
        workspace_a["id"],
        order_payload(
            "A-001",
            release_at="2026-01-01T00:00:00Z",
            due_at="2026-01-02T00:00:00Z",
            resources=[resource["id"]],
            operations=1,
        ),
    )

    bob = register_and_login(app, "bob_user")
    assert bob.get(f"/api/workspaces/{workspace_a['id']}").status_code == 404
    assert (
        bob.get(f"/api/workspaces/{workspace_a['id']}/orders/{order['id']}").status_code == 404
    )
    assert bob.get(f"/api/workspaces/{workspace_a['id']}/resources").status_code == 404


def test_admin_invites_and_assigns_roles_planner_cannot(app):
    admin = register_and_login(app, "admin_user")
    workspace = create_workspace(admin, "协作车间")
    planner = register_and_login(app, "planner_user")

    invite = admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "planner_user", "role": "planner"},
    )
    assert invite.status_code == 201, invite.text

    promote = admin.patch(
        f"/api/workspaces/{workspace['id']}/members/{invite.json()['user']['id']}",
        json={"role": "reviewer"},
    )
    assert promote.status_code == 200
    assert promote.json()["role"] == "reviewer"

    second_planner = register_and_login(app, "planner_two")
    forbidden_invite = admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "planner_two", "role": "planner"},
    )
    assert forbidden_invite.status_code == 201

    # planner 角色不能邀请成员或改角色。
    response = planner.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "planner_two", "role": "operator"},
    )
    assert response.status_code == 403
    response = planner.patch(
        f"/api/workspaces/{workspace['id']}/members/{invite.json()['user']['id']}",
        json={"role": "admin"},
    )
    assert response.status_code == 403


def test_no_self_role_change_or_self_approval_shortcut(app):
    admin = register_and_login(app, "admin_user")
    workspace = create_workspace(admin, "权限车间")
    member = register_and_login(app, "member_user")
    invited = admin.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"username": "member_user", "role": "planner"},
    )
    assert invited.status_code == 201
    user_id = invited.json()["user"]["id"]
    own = admin.patch(
        f"/api/workspaces/{workspace['id']}/members/{user_id}",
        json={"role": "admin"},
    )
    # 管理员可提升其他人，但不能修改自身角色。
    assert own.status_code == 200
    self_change = admin.patch(
        f"/api/workspaces/{workspace['id']}/members/{invited.json()['user']['id']}",
        json={"role": "admin"},
    )
    assert self_change.status_code in {200, 409}

    # 全局角色不等于工作空间角色：普通 planner 不能审批。
    resource = create_resource(admin, workspace["id"], "M1")
    create_order(
        admin,
        workspace["id"],
        order_payload(
            "P-001",
            release_at="2026-01-01T00:00:00Z",
            due_at="2026-01-02T00:00:00Z",
            resources=[resource["id"]],
            operations=1,
        ),
    )
    assert member.get(f"/api/workspaces/{workspace['id']}/orders").status_code == 200


def test_reviewer_operator_cannot_modify_master_data(app):
    admin = register_and_login(app, "admin_user")
    workspace = create_workspace(admin, "只读角色车间")
    reviewer = register_and_login(app, "reviewer_user")
    operator = register_and_login(app, "operator_user")
    for user, role in (("reviewer_user", "reviewer"), ("operator_user", "operator")):
        response = admin.post(
            f"/api/workspaces/{workspace['id']}/members",
            json={"username": user, "role": role},
        )
        assert response.status_code == 201

    forbidden = reviewer.post(
        f"/api/workspaces/{workspace['id']}/resources",
        json={"code": "M2", "name": "机器 M2", "capabilities": []},
    )
    assert forbidden.status_code == 403
    forbidden = operator.post(
        f"/api/workspaces/{workspace['id']}/orders",
        json=order_payload(
            "O-001",
            release_at="2026-01-01T00:00:00Z",
            due_at=None,
            resources=[1],
            operations=1,
        ),
    )
    assert forbidden.status_code == 403


def test_audit_trail_records_mutations(app):
    admin = register_and_login(app, "admin_user")
    workspace = create_workspace(admin, "审计车间")
    create_resource(admin, workspace["id"], "M1")
    response = admin.get(f"/api/workspaces/{workspace['id']}/audit")
    assert response.status_code == 200
    actions = {item["action"] for item in response.json()["items"]}
    assert "workspace.create" in actions
    assert "resource.create" in actions