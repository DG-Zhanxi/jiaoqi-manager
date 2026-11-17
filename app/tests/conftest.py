from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.db import Database  # noqa: E402

PASSWORD = "CorrectHorse123"


@pytest.fixture()
def settings(tmp_path) -> Settings:
    db_path = (tmp_path / "test.db").as_posix()
    return Settings.from_env(
        database_url=f"sqlite:///{db_path}",
        pbkdf2_iterations=1200,
        default_solver_time_seconds=2.0,
        max_solver_time_seconds=10.0,
        solver_workers=2,
        plan_horizon_days=2,
        login_rate_limit=50,
        login_rate_window_seconds=300,
        registration_rate_limit=100,
        registration_rate_window_seconds=3600,
        allowed_origins=("http://localhost:5173", "http://testserver"),
    )


@pytest.fixture()
def database(settings) -> Database:
    return Database(settings.database_url)


@pytest.fixture()
def app(settings, database):
    application = create_app(settings, database)
    yield application
    database.dispose()


@pytest.fixture()
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def register(client: TestClient, username: str, password: str = PASSWORD):
    response = client.post(
        "/api/auth/register", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def login(client: TestClient, username: str, password: str = PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    return response


def register_and_login(app, username: str, password: str = PASSWORD) -> TestClient:
    client = TestClient(app)
    register(client, username, password)
    return client


def create_workspace(client: TestClient, name: str = "测试工作空间") -> dict:
    response = client.post("/api/workspaces", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def create_resource(client: TestClient, workspace_id: int, code: str, name: str | None = None):
    response = client.post(
        f"/api/workspaces/{workspace_id}/resources",
        json={
            "code": code,
            "name": name or f"机器 {code}",
            "resource_type": "machine",
            "capabilities": [],
            "calendar": None,
            "is_active": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def order_payload(
    order_no: str,
    *,
    release_at: str,
    due_at: str | None,
    resources: list[int],
    durations: list[int] | None = None,
    priority: int = 0,
    operations: int = 2,
    materials: list[dict] | None = None,
):
    durations = durations or [600] * operations
    return {
        "order_no": order_no,
        "product_name": f"产品 {order_no}",
        "quantity": 10,
        "release_at": release_at,
        "due_at": due_at,
        "priority": priority,
        "status": "confirmed",
        "operations": [
            {
                "sequence": index + 1,
                "name": f"工序 {index + 1}",
                "setup_seconds": 0,
                "candidates": [
                    {"resource_id": resource_id, "duration_seconds": durations[index]}
                    for resource_id in resources
                ],
            }
            for index in range(operations)
        ],
        "materials": materials or [],
    }


def create_order(client: TestClient, workspace_id: int, payload: dict) -> dict:
    response = client.post(f"/api/workspaces/{workspace_id}/orders", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def invoke(client: TestClient, method: str, url: str, **kwargs):
    return client.request(method, url, **kwargs)


def wait_for_job(client: TestClient, workspace_id: int, job_id: int, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/workspaces/{workspace_id}/schedule/jobs/{job_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"succeeded", "failed", "cancelled"}:
            return last
        time.sleep(0.05)
    raise AssertionError(f"排程任务超时未结束：{last}")


def create_plan(
    client: TestClient,
    workspace_id: int,
    *,
    strategy: str = "delivery_first",
    base_plan_id: int | None = None,
    name: str | None = None,
    time_limit_seconds: float = 5.0,
) -> dict:
    payload = {
        "strategy": strategy,
        "time_limit_seconds": time_limit_seconds,
        "persist_plan": True,
        "base_plan_id": base_plan_id,
        "name": name,
    }
    response = client.post(f"/api/workspaces/{workspace_id}/schedule/jobs", json=payload)
    assert response.status_code == 202, response.text
    job = wait_for_job(client, workspace_id, response.json()["id"])
    assert job["status"] == "succeeded", job
    assert job["plan_id"], job
    plan_response = client.get(f"/api/workspaces/{workspace_id}/plans/{job['plan_id']}")
    assert plan_response.status_code == 200, plan_response.text
    return plan_response.json()