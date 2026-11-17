from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AuditLog, Workspace
from ..timeutil import utcnow


def record(
    db: Session,
    *,
    workspace_id: int | None,
    actor_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    details: dict | None = None,
) -> AuditLog:
    item = AuditLog(
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or {},
        created_at=utcnow(),
    )
    db.add(item)
    return item


def bump_revision(db: Session, workspace: Workspace) -> int:
    workspace.data_revision += 1
    workspace.updated_at = utcnow()
    return workspace.data_revision