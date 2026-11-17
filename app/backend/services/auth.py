from __future__ import annotations

import threading

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import BootstrapState, ROLE_ADMIN, ROLE_PLANNER, User, UserSession
from ..security import (
    SlidingWindowLimiter,
    hash_password,
    hash_token,
    new_csrf_token,
    new_session_token,
    validate_password,
    validate_username,
    verify_password,
)
from ..timeutil import utcnow

_BOOTSTRAP_LOCK = threading.Lock()
_ADMIN_KEY = "first_admin"


def create_user(
    db: Session,
    username: str,
    password: str,
    settings: Settings,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, UserSession, str, str]:
    """注册用户并建立会话。

    首个成功注册的用户在事务内成为管理员；后续用户为计划员。
    通过 bootstrap_state 唯一键保证并发情况下只有一个管理员。
    """

    clean_username = validate_username(username)
    validate_password(password)
    password_hash = hash_password(password, settings.pbkdf2_iterations)

    with _BOOTSTRAP_LOCK:
        user = User(
            username=clean_username,
            password_hash=password_hash,
            role=ROLE_PLANNER,
        )
        db.add(user)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("用户名已存在") from exc

        marker = db.get(BootstrapState, _ADMIN_KEY)
        if marker is None:
            try:
                db.add(BootstrapState(key=_ADMIN_KEY, value=str(user.id)))
                db.flush()
                user.role = ROLE_ADMIN
            except IntegrityError:
                db.rollback()
                raise ValueError("注册冲突，请重试") from None
        db.flush()

        raw_token, raw_csrf, session = create_session(
            db,
            user,
            settings,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.commit()
        db.refresh(user)
        return user, session, raw_token, raw_csrf


def authenticate(db: Session, username: str, password: str) -> User | None:
    clean = (username or "").strip()
    user = db.scalar(select(User).where(User.username == clean))
    if user is None or not user.is_active:
        verify_password(password or "", "pbkdf2_sha256$1$AA$AA")
        return None
    if not verify_password(password or "", user.password_hash):
        return None
    return user


def create_session(
    db: Session,
    user: User,
    settings: Settings,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, str, UserSession]:
    raw_token = new_session_token()
    csrf_token = new_csrf_token()
    ttl = ttl_seconds if ttl_seconds is not None else settings.session_ttl_seconds
    now = utcnow()
    session = UserSession(
        token_hash=hash_token(raw_token),
        csrf_token=csrf_token,
        user_id=user.id,
        expires_at=int(now.timestamp()) + ttl,
        created_at=now,
        last_seen_at=now,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(session)
    db.flush()
    return raw_token, csrf_token, session


def resolve_session(db: Session, raw_token: str | None) -> UserSession | None:
    if not raw_token:
        return None
    session = db.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    )
    if session is None:
        return None
    if session.revoked_at is not None:
        return None
    if session.expires_at <= int(utcnow().timestamp()):
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    return session


def revoke_session(db: Session, session: UserSession) -> None:
    if session.revoked_at is None:
        session.revoked_at = utcnow()
        db.flush()


def user_for_session(db: Session, session: UserSession) -> User | None:
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    return user


def login_rate_key(ip_address: str, username: str) -> str:
    return f"login:{ip_address}:{(username or '').strip().lower()}"


__all__ = [
    "SlidingWindowLimiter",
    "authenticate",
    "create_session",
    "create_user",
    "login_rate_key",
    "resolve_session",
    "revoke_session",
    "user_for_session",
]