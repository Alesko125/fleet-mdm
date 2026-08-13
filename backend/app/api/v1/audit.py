"""Читання аудит-журналу."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.rbac import Permission
from app.db.models import AuditLog, User
from app.db.session import get_session
from app.schemas.audit import AuditLogOut
from app.schemas.common import Page

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=Page[AuditLogOut])
async def list_audit(
    action: str | None = Query(default=None, max_length=64),
    actor_user_id: uuid.UUID | None = None,
    actor_device_id: uuid.UUID | None = None,
    object_id: str | None = Query(default=None, max_length=64),
    result: str | None = Query(default=None, pattern=r"^(success|failure|denied)$"),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.AUDIT_READ)),
    session: AsyncSession = Depends(get_session),
) -> Page[AuditLogOut]:
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if actor_user_id:
        conditions.append(AuditLog.actor_user_id == actor_user_id)
    if actor_device_id:
        conditions.append(AuditLog.actor_device_id == actor_device_id)
    if object_id:
        conditions.append(AuditLog.object_id == object_id)
    if result:
        conditions.append(AuditLog.result == result)
    if since:
        conditions.append(AuditLog.occurred_at >= since)
    if until:
        conditions.append(AuditLog.occurred_at <= until)

    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(AuditLog).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(AuditLog)
            .where(*conditions)
            .order_by(AuditLog.occurred_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    return Page(
        items=[AuditLogOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/actions", response_model=list[str])
async def list_actions(
    _: User = Depends(require_permission(Permission.AUDIT_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[str]:
    rows = (
        await session.execute(sa.select(AuditLog.action).distinct().order_by(AuditLog.action))
    ).scalars().all()
    return list(rows)
