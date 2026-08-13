"""Комунікаційний модуль — адміністративна частина.

Керування (`comms:manage`) і перегляд зібраних даних (`comms:read`) —
різні дозволи. Перегляд SMS чи журналу дзвінків завжди пишеться в аудит:
доступ до чутливого вмісту не буває «тихим».
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.config import get_settings
from app.core.encryption import encryption_available
from app.core.rate_limit import rate_limit
from app.core.rbac import Permission
from app.core.redis_client import get_redis
from app.db.models import User
from app.db.session import get_session
from app.schemas.common import MessageResponse, Page
from app.schemas.comms import (
    CallLogEntryOut,
    CommsSettingsDetail,
    CommsSettingsUpdate,
    SmsMessageOut,
)
from app.services import command_registry
from app.services import commands as command_service
from app.services import comms as comms_service
from app.services import device as device_service
from app.services.audit import AuditAction, record_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices/{device_id}/comms", tags=["communications"])


async def _detail(session, row) -> CommsSettingsDetail:
    settings = get_settings()
    detail = CommsSettingsDetail.model_validate(row)
    detail.encryption_ready = encryption_available()
    detail.retention_max_days = settings.comms_retention_max_days
    if row.enabled_by_id:
        author = await session.get(User, row.enabled_by_id)
        detail.enabled_by_email = author.email if author else None
    return detail


@router.get("", response_model=CommsSettingsDetail)
async def get_settings_view(
    device_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.COMMS_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> CommsSettingsDetail:
    """Стан модуля: увімкнено, коли, ким, дозволи на пристрої, ретеншн."""
    device = await device_service.get_device(session, device_id)
    row = await comms_service.get_or_create(session, device)
    await session.commit()
    return await _detail(session, row)


@router.put(
    "",
    response_model=CommsSettingsDetail,
    dependencies=[Depends(rate_limit("mutation"))],
)
async def update_settings_view(
    request: Request,
    device_id: uuid.UUID,
    payload: CommsSettingsUpdate,
    user: User = Depends(require_permission(Permission.COMMS_MANAGE)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommsSettingsDetail:
    """Вмикає, вимикає або переналаштовує модуль.

    Після зміни пристрою одразу надсилається команда синхронізації, щоб
    агент застосував нову конфігурацію (зокрема — припинив збір, якщо
    модуль вимкнули).
    """
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    row = await comms_service.update_settings(
        session, device=device, data=payload, user=user, ip=ip, user_agent=user_agent
    )

    command = None
    try:
        command = await command_service.issue(
            session,
            device=device,
            command_type=command_registry.COMMS_SYNC,
            params=None,
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
    except Exception as exc:  # noqa: BLE001 — конфіг збережено; синк наздожене циклом
        # Не валимо запит: налаштування вже застосовані, а агент підхопить їх
        # плановим циклом. Але мовчати про це не можна — інакше «команда не
        # дійшла» виглядатиме як загадка.
        logger.warning(
            "Не вдалося поставити COMMS_SYNC після зміни конфігурації",
            extra={"device_id": str(device_id), "error": str(exc)},
        )
        command = None

    await session.commit()
    if command is not None:
        await command_service.dispatch(redis, command)
    return await _detail(session, row)


@router.post(
    "/purge",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("mutation"))],
)
async def purge_view(
    request: Request,
    device_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.COMMS_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    """Негайно видаляє всі зібрані дані пристрою."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    sms, calls = await comms_service.purge_device(
        session, device, user=user, ip=ip, user_agent=user_agent
    )
    await session.commit()
    return MessageResponse(message=f"Видалено: SMS {sms}, дзвінків {calls}")


# ------------------------------------------- перегляд даних (comms:read) ---


@router.get("/messages", response_model=Page[SmsMessageOut])
async def list_messages(
    request: Request,
    device_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_permission(Permission.COMMS_READ)),
    session: AsyncSession = Depends(get_session),
) -> Page[SmsMessageOut]:
    """Розшифровані SMS. Кожен перегляд фіксується в аудиті."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    items, total = await comms_service.list_sms(session, device, limit=limit, offset=offset)

    await record_audit(
        session, action=AuditAction.COMMS_DATA_VIEWED, user=user,
        object_type="device", object_id=device.id, ip=ip, user_agent=user_agent,
        details={"kind": "sms", "returned": len(items), "offset": offset},
    )
    await session.commit()
    return Page(
        items=[SmsMessageOut.model_validate(i) for i in items],
        total=total, limit=limit, offset=offset,
    )


@router.get("/calls", response_model=Page[CallLogEntryOut])
async def list_call_log(
    request: Request,
    device_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_permission(Permission.COMMS_READ)),
    session: AsyncSession = Depends(get_session),
) -> Page[CallLogEntryOut]:
    """Розшифрований журнал дзвінків. Кожен перегляд фіксується в аудиті."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    items, total = await comms_service.list_calls(session, device, limit=limit, offset=offset)

    await record_audit(
        session, action=AuditAction.COMMS_DATA_VIEWED, user=user,
        object_type="device", object_id=device.id, ip=ip, user_agent=user_agent,
        details={"kind": "calls", "returned": len(items), "offset": offset},
    )
    await session.commit()
    return Page(
        items=[CallLogEntryOut.model_validate(i) for i in items],
        total=total, limit=limit, offset=offset,
    )
