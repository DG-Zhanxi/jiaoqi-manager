from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.models import User, UserSession
from backend.security import hash_password, verify_password
from backend.timeutil import epoch, from_epoch

from .conftest import PASSWORD, create_workspace, login, register


def test_registration_first_user_admin_second_planner(client, app):
    first = register(client, "admin_user")
    assert first["user"]["role"] == "admin"
    assert "password" not in first["user"]
    assert "password_hash" not in first["user"]

    second = register(client, "planner_user")
    assert second["user"]["role"] == "planner"
    with app.state.db.session() as db:
        assert db.query(User).count() == 2


def test_password_hash_is_salted_and_verifiable():
    first = hash_password(PASSWORD, 1000)
    second = hash_password(PASSWORD, 1000)
    assert first != second
    assert verify_password(PASSWORD, first)
    assert not verify_password("wrong-password", first)


def test_wrong_login_returns_401(client):
    register(client, "admin_user")
    response = login(client, "admin_user", "wrong-password-123")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_cookie_flags_and_logout_revocation(client, app):
    register(client, "admin_user")
    cookies = client.cookies
    assert cookies.get("jiaoqi_session")
    set_cookie = client.post("/api/auth/login", json={"username": "admin_user", "password": PASSWORD})
    raw_cookie = set_cookie.headers.get("set-cookie", "")
    assert "HttpOnly" in raw_cookie
    assert "SameSite=lax" in raw_cookie

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "admin_user"

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 204
    assert client.get("/api/auth/me").status_code == 401

    with app.state.db.session() as db:
        assert db.query(UserSession).filter(UserSession.revoked_at.isnot(None)).count() >= 1


def test_session_expiry_is_rejected(client, app):
    register(client, "admin_user")
    with app.state.db.session() as db:
        session = db.query(UserSession).order_by(UserSession.id.desc()).first()
        assert session is not None
        session.expires_at = int(datetime.now(timezone.utc).timestamp()) - 5
        db.commit()
    assert client.get("/api/auth/me").status_code == 401


def test_strict_origin_check_blocks_cross_site_mutation(client):
    register(client, "admin_user")
    response = client.post(
        "/api/workspaces",
        json={"name": "跨站尝试"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"


def test_missing_session_cannot_read_workspaces(client):
    assert client.get("/api/workspaces").status_code == 401


def test_login_rate_limit(settings, database):
    from fastapi.testclient import TestClient
    from backend.app import create_app

    limited = Settings = None
    from backend.config import Settings as SettingsClass

    limited = SettingsClass.from_env(**{**settings.__dict__, "login_rate_limit": 3})
    application = create_app(limited, database)
    client = TestClient(application)
    register(client, "admin_user")
    client.post("/api/auth/logout")
    for _ in range(3):
        assert login(client, "admin_user", "bad-password-123").status_code == 401
    response = login(client, "admin_user", "bad-password-123")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"