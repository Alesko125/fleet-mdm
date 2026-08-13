"""Геолокація: конфігурація, поточна позиція, історія (адміністративна частина)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.config import get_settings
from app.core.errors import ConflictError
from app.core.rbac import Permission
from app.core.redis_client import get_redis
from app.db.models import User
from app.db.session import get_session
from app.schemas.command import CommandOut
from app.schemas.common import MessageResponse, Page
from app.schemas.location import (
    LocationFixOut,
    LocationOverview,
    LocationSettingsOut,
    LocationSettingsUpdate,
)
from app.services import commands as command_service
from app.services import device as device_service
from app.services import location as location_service
from app.services.audit import AuditAction, record_audit

router = APIRouter(prefix="/devices/{device_id}/location", tags=["location"])


@router.get("", response_model=LocationOverview)
async def location_overview(
    device_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.LOCATION_READ)),
    session: AsyncSession = Depends(get_session),
) -> LocationOverview:
    """Зведення для сторінки геолокації: стан, дозволи, остання позиція."""
    device = await device_service.get_device(session, device_id)
    settings = await location_service.get_or_create_settings(session, device)
    current = await location_service.latest_fix(session, device.id)
    count, oldest = await location_service.fix_stats(session, device.id)
    await session.commit()

    return LocationOverview(
        settings=LocationSettingsOut.model_validate(settings),
        current=LocationFixOut.model_validate(current) if current else None,
        fix_count=count,
        oldest_fix_at=oldest,
        retention_days=get_settings().location_history_days,
        reporting_active=location_service.reporting_active(settings),
    )


@router.get("/settings", response_model=LocationSettingsOut)
async def get_location_settings(
    device_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.LOCATION_READ)),
    session: AsyncSession = Depends(get_session),
) -> LocationSettingsOut:
    device = await device_service.get_device(session, device_id)
    settings = await location_service.get_or_create_settings(session, device)
    await session.commit()
    return LocationSettingsOut.model_validate(settings)


@router.put("/settings", response_model=LocationSettingsOut)
async def update_location_settings(
    request: Request,
    device_id: uuid.UUID,
    payload: LocationSettingsUpdate,
    user: User = Depends(require_permission(Permission.LOCATION_MANAGE)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> LocationSettingsOut:
    """Кожна зміна конфігурації аудиториться і одразу летить на пристрій."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)

    settings, changes = await location_service.update_settings(
        session, device=device, payload=payload, user=user, ip=ip, user_agent=user_agent
    )

    command = None
    if changes:
        command = await command_service.create_command(
            session,
            device=device,
            command_type="LOCATION_CONFIG",
            user=user,
            payload=location_service.to_agent_config(settings).model_dump(mode="json"),
            ttl_seconds=86_400,
        )

    await session.commit()
    if command is not None:
        await command_service.dispatch(redis, command)
    return LocationSettingsOut.model_validate(settings)


@router.post("/request", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def request_location_now(
    request: Request,
    device_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.LOCATION_MANAGE)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Разовий запит поточної позиції — працює лише коли модуль увімкнено."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    settings = await location_service.get_or_create_settings(session, device)

    if not settings.enabled:
        raise ConflictError(
            "Геолокація вимкнена для цього пристрою — увімкніть її в налаштуваннях"
        )

    command = await command_service.create_command(
        session,
        device=device,
        command_type="LOCATION_REQUEST",
        user=user,
        ttl_seconds=900,
    )
    await record_audit(
        session,
        action=AuditAction.LOCATION_REQUESTED,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"command_id": str(command.id)},
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.get("/history", response_model=Page[LocationFixOut])
async def location_history(
    request: Request,
    device_id: uuid.UUID,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_permission(Permission.LOCATION_READ)),
    session: AsyncSession = Depends(get_session),
) -> Page[LocationFixOut]:
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    rows, total = await location_service.history(
        session, device.id, since=since, until=until, limit=limit, offset=offset
    )

    # Перегляд історії переміщень — чутлива дія, тому теж в аудиті.
    await record_audit(
        session,
        action=AuditAction.LOCATION_HISTORY_VIEWED,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"returned": len(rows), "since": since, "until": until},
    )
    await session.commit()

    return Page(
        items=[LocationFixOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete("/history", response_model=MessageResponse)
async def purge_location_history(
    request: Request,
    device_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.LOCATION_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    removed = await location_service.purge_history(session, device_id=device.id)

    await record_audit(
        session,
        action=AuditAction.LOCATION_HISTORY_PURGED,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"removed": removed, "scope": "device"},
    )
    await session.commit()
    return MessageResponse(message=f"Видалено записів історії: {removed}")
