from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ..config import Settings
from ..dependencies import get_current_session, get_db, get_settings
from ..errors import ConflictError, DomainError, RateLimitError, ValidationError
from ..models import User, UserSession
from ..schemas import LoginRequest, RegisterRequest
from ..security import PasswordPolicyError, SlidingWindowLimiter
from ..serializers import user_out
from ..services.auth import (
    authenticate,
    create_session,
    create_user,
    login_rate_key,
    revoke_session,
    resolve_session,
    user_for_session,
)
from ..timeutil import utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])


def set_auth_cookies(response: Response, settings: Settings, raw_token: str, csrf: str) -> None:
    response.set_cookie(
        settings.cookie_name,
        raw_token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf,
        max_age=settings.session_ttl_seconds,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


@router.post("/register")
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    ip = request.client.host if request.client else "unknown"
    limiter: SlidingWindowLimiter = request.app.state.registration_limiter
    if not limiter.allow(
        f"register:{ip}",
        settings.registration_rate_limit,
        settings.registration_rate_window_seconds,
    ):
        raise RateLimitError()
    try:
        user, _session, raw_token, csrf = create_user(
            db,
            payload.username,
            payload.password,
            settings,
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except PasswordPolicyError as exc:
        raise ValidationError(str(exc)) from exc
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    set_auth_cookies(response, settings, raw_token, csrf)
    return {"user": user_out(user), "csrf_token": csrf}


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    ip = request.client.host if request.client else "unknown"
    key = login_rate_key(ip, payload.username)
    limiter: SlidingWindowLimiter = request.app.state.login_limiter
    if not limiter.allow(key, settings.login_rate_limit, settings.login_rate_window_seconds):
        raise RateLimitError()
    user = authenticate(db, payload.username, payload.password)
    if user is None:
        raise DomainError(401, "invalid_credentials", "用户名或密码错误")
    limiter.reset(key)
    raw_token, csrf, _session = create_session(
        db,
        user,
        settings,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    set_auth_cookies(response, settings, raw_token, csrf)
    return {"user": user_out(user), "csrf_token": csrf}


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    raw_token = request.cookies.get(settings.cookie_name)
    session = resolve_session(db, raw_token)
    if session is not None:
        revoke_session(db, session)
        db.commit()
    clear_auth_cookies(response, settings)
    response.status_code = 204
    return response


@router.get("/me")
def me(
    session_and_user: tuple[UserSession, User] = Depends(get_current_session),
):
    session, user = session_and_user
    return {
        "user": user_out(user),
        "csrf_token": session.csrf_token,
        "session_expires_at": session.expires_at,
    }