from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Path, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .errors import ForbiddenError, NotFoundError, UnauthorizedError
from .models import (
    ROLE_ADMIN,
    Workspace,
    WorkspaceMember,
    User,
    UserSession,
)
from .services.auth import resolve_session, user_for_session


def get_db(request: Request) -> Session:
    session = request.app.state.db.session()
    try:
        yield session
    finally:
        session.close()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def get_current_session(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> tuple[UserSession, User]:
    raw_token = request.cookies.get(settings.cookie_name)
    session = resolve_session(db, raw_token)
    if session is None:
        raise UnauthorizedError()
    user = user_for_session(db, session)
    if user is None:
        raise UnauthorizedError()
    return session, user


def get_current_user(
    session_and_user: tuple[UserSession, User] = Depends(get_current_session),
) -> User:
    return session_and_user[1]


@dataclass(frozen=True)
class WorkspaceContext:
    workspace: Workspace
    user: User
    role: str
    user_id: int

    def can(self, *roles: str) -> bool:
        return self.role in roles or self.role == ROLE_ADMIN


def get_workspace_context(
    workspace_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WorkspaceContext:
    row = db.execute(
        select(Workspace, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.id == workspace_id, WorkspaceMember.user_id == user.id)
    ).first()
    if row is None:
        # 不区分“不存在”和“无权访问”，避免泄露其他工作空间。
        raise NotFoundError("工作空间不存在或无权访问")
    workspace, member = row
    return WorkspaceContext(workspace=workspace, user=user, role=member.role, user_id=user.id)


def require_workspace_roles(*roles: str):
    def dependency(ctx: WorkspaceContext = Depends(get_workspace_context)) -> WorkspaceContext:
        if not ctx.can(*roles):
            raise ForbiddenError(f"需要以下工作空间角色之一：{', '.join(roles)}")
        return ctx

    return dependency


def require_global_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise ForbiddenError("仅系统管理员可执行该操作")
    return user