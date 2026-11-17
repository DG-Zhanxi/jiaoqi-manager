from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..dependencies import (
    get_current_user,
    get_db,
    get_workspace_context,
    require_workspace_roles,
    WorkspaceContext,
)
from ..errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from ..models import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    AuditLog,
    User,
    Workspace,
    WorkspaceMember,
)
from ..schemas import MemberInvite, MemberRoleUpdate, WorkspaceCreate
from ..serializers import audit_out, user_out, workspace_out
from ..services import audit
from ..timeutil import iso

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.get("")
def list_workspaces(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id, Workspace.is_archived.is_(False))
        .order_by(Workspace.created_at, Workspace.id)
    ).all()
    return {"items": [workspace_out(workspace, role) for workspace, role in rows]}


@router.post("", status_code=201)
def create_workspace(
    payload: WorkspaceCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    workspace = Workspace(name=payload.name.strip(), created_by=user.id, data_revision=1)
    db.add(workspace)
    db.flush()
    db.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role=ROLE_ADMIN,
            created_by=user.id,
        )
    )
    audit.record(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="workspace.create",
        entity_type="workspace",
        entity_id=workspace.id,
        details={"name": workspace.name},
    )
    db.commit()
    db.refresh(workspace)
    return workspace_out(workspace, ROLE_ADMIN)


@router.get("/{workspace_id}")
def get_workspace(ctx: WorkspaceContext = Depends(get_workspace_context)):
    return workspace_out(ctx.workspace, ctx.role)


@router.get("/{workspace_id}/members")
def list_members(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == ctx.workspace.id)
        .order_by(WorkspaceMember.created_at, WorkspaceMember.id)
    ).all()
    return {
        "items": [
            {
                "user": user_out(user),
                "role": member.role,
                "created_at": iso(member.created_at),
            }
            for member, user in rows
        ]
    }


@router.post("/{workspace_id}/members", status_code=201)
def invite_member(
    payload: MemberInvite,
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if payload.role == ROLE_ADMIN:
        raise ForbiddenError("邀请成员时不能授予管理员角色")
    target = db.scalar(select(User).where(User.username == payload.username.strip()))
    if target is None:
        raise NotFoundError("本机不存在该用户名")
    if not target.is_active:
        raise ConflictError("该用户已停用")
    existing = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ctx.workspace.id,
            WorkspaceMember.user_id == target.id,
        )
    )
    if existing is not None:
        raise ConflictError("该用户已经是工作空间成员")
    member = WorkspaceMember(
        workspace_id=ctx.workspace.id,
        user_id=target.id,
        role=payload.role,
        created_by=ctx.user_id,
    )
    db.add(member)
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="member.invite",
        entity_type="workspace_member",
        entity_id=target.id,
        details={"username": target.username, "role": payload.role},
    )
    db.commit()
    return {"user": user_out(target), "role": member.role}


@router.patch("/{workspace_id}/members/{user_id}")
def update_member_role(
    payload: MemberRoleUpdate,
    user_id: int = Path(..., ge=1),
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if user_id == ctx.user_id:
        raise ConflictError("不能修改自己的角色，避免自我提权或移除最后管理员")
    member = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ctx.workspace.id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if member is None:
        raise NotFoundError("工作空间成员不存在")
    if member.role == ROLE_ADMIN and payload.role != ROLE_ADMIN:
        admin_count = db.scalar(
            select(func.count()).select_from(WorkspaceMember).where(
                WorkspaceMember.workspace_id == ctx.workspace.id,
                WorkspaceMember.role == ROLE_ADMIN,
            )
        )
        if (admin_count or 0) <= 1:
            raise ConflictError("不能移除最后一名工作空间管理员")
    previous = member.role
    member.role = payload.role
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="member.role_change",
        entity_type="workspace_member",
        entity_id=user_id,
        details={"from": previous, "to": payload.role},
    )
    db.commit()
    target = db.get(User, user_id)
    return {"user": user_out(target) if target else None, "role": member.role}


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(
    user_id: int = Path(..., ge=1),
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    if user_id == ctx.user_id:
        raise ConflictError("不能移除自己")
    member = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ctx.workspace.id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if member is None:
        raise NotFoundError("工作空间成员不存在")
    if member.role == ROLE_ADMIN:
        admin_count = db.scalar(
            select(func.count()).select_from(WorkspaceMember).where(
                WorkspaceMember.workspace_id == ctx.workspace.id,
                WorkspaceMember.role == ROLE_ADMIN,
            )
        )
        if (admin_count or 0) <= 1:
            raise ConflictError("不能移除最后一名工作空间管理员")
    db.delete(member)
    audit.record(
        db,
        workspace_id=ctx.workspace.id,
        actor_id=ctx.user_id,
        action="member.remove",
        entity_type="workspace_member",
        entity_id=user_id,
        details={},
    )
    db.commit()
    return None


@router.get("/{workspace_id}/audit")
def list_audit(
    ctx: WorkspaceContext = Depends(require_workspace_roles(ROLE_PLANNER)),
    db: Session = Depends(get_db),
    limit: int = 200,
):
    limit = min(max(1, limit), 500)
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.workspace_id == ctx.workspace.id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    return {"items": [audit_out(item) for item in rows]}