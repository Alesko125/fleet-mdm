"""Список пристроїв, деталі, редагування, видалення."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import request_context, require_permission
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.rate_limit import rate_limit
from app.core.rbac import Permission
from app.core.redis_client import get_redis, is_online
from app.core.security import as_utc, utcnow
from app.db.models import Device, DeviceKeyHistory, User
from app.db.session import get_session
from app.schemas.command import CommandOut
from app.schemas.common import MessageResponse, Page
from app.schemas.device import DeviceDetail, DeviceListItem, DeviceUpdate, TelemetryOut
from app.schemas.enrollment import DeviceKeyHistoryOut, DeviceKeyStatus
from app.services import command_registry
from app.services import commands as command_service
from app.services import enrollment as enrollment_service
from app.services.audit import AuditAction, record_audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["devices"])


async def _online(redis: Redis | None, device: Device) -> bool:
    """Онлайн = свіжий heartbeat у Redis або нещодавній last_seen_at."""
    settings = get_settings()
    if redis is not None:
        try:
            if await is_online(redis, str(device.id)):
                return True
        except Exception as exc:  # noqa: BLE001 — Redis не критичний для читання статусу
            logger.warning("Не вдалося прочитати присутність", extra={"error": str(exc)})
    last_seen = as_utc(device.last_seen_at)
    if last_seen is None:
        return False
    return last_seen > utcnow() - timedelta(seconds=settings.device_offline_after_seconds)


async def _get_device(session: AsyncSession, device_id: uuid.UUID) -> Device:
    stmt = (
        sa.select(Device)
        .options(selectinload(Device.telemetry))
        .where(Device.id == device_id, Device.deleted_at.is_(None))
    )
    device = (await session.execute(stmt)).scalar_one_or_none()
    if device is None:
        raise NotFoundError("Пристрій не знайдено")
    return device


@router.get("", response_model=Page[DeviceListItem])
async def list_devices(
    search: str | None = Query(default=None, max_length=200),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> Page[DeviceListItem]:
    conditions = [Device.deleted_at.is_(None)]
    if status_filter:
        conditions.append(Device.status == status_filter)
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(
            sa.or_(Device.name.ilike(pattern), Device.android_id.ilike(pattern))
        )

    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Device).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(Device)
            .options(selectinload(Device.telemetry))
            .where(*conditions)
            .order_by(Device.last_seen_at.desc().nullslast(), Device.enrolled_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    items: list[DeviceListItem] = []
    for device in rows:
        item = DeviceListItem.model_validate(device)
        item.online = await _online(redis, device)
        if device.telemetry is not None:
            item.battery_level = device.telemetry.battery_level
            item.model = device.telemetry.model
            item.android_version = device.telemetry.android_version
        items.append(item)

    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{device_id}", response_model=DeviceDetail)
async def get_device(
    device_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> DeviceDetail:
    device = await _get_device(session, device_id)
    detail = DeviceDetail.model_validate(device)
    detail.online = await _online(redis, device)
    if device.telemetry is not None:
        detail.telemetry = TelemetryOut.model_validate(device.telemetry)
    return detail


@router.patch("/{device_id}", response_model=DeviceDetail)
async def update_device(
    request: Request,
    device_id: uuid.UUID,
    payload: DeviceUpdate,
    user: User = Depends(require_permission(Permission.DEVICE_WRITE)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> DeviceDetail:
    ip, user_agent = request_context(request)
    device = await _get_device(session, device_id)

    changes: dict[str, object] = {}
    if payload.name is not None:
        changes["name"] = payload.name
        device.name = payload.name
    if payload.notes is not None:
        changes["notes"] = payload.notes
        device.notes = payload.notes
    if payload.status is not None and payload.status != device.status:
        changes["status"] = {"from": device.status, "to": payload.status}
        device.status = payload.status
        if payload.status != "active":
            # Призупинений пристрій має втратити доступ негайно, а не
            # доживати до кінця терміну дії свого токена.
            device.tokens_valid_from = utcnow()

    await record_audit(
        session,
        action=AuditAction.DEVICE_UPDATED,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"changes": changes},
    )
    await session.commit()

    detail = DeviceDetail.model_validate(device)
    detail.online = await _online(redis, device)
    if device.telemetry is not None:
        detail.telemetry = TelemetryOut.model_validate(device.telemetry)
    return detail


@router.delete("/{device_id}", response_model=MessageResponse)
async def delete_device(
    request: Request,
    device_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.DEVICE_DELETE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    """М'яке видалення: пристрій втрачає доступ, аудит зберігає слід."""
    ip, user_agent = request_context(request)
    device = await _get_device(session, device_id)

    device.deleted_at = utcnow()
    device.status = "retired"
    device.tokens_valid_from = utcnow()  # видані токени вмирають одразу
    device.android_id = None  # звільняємо унікальний ключ для повторної реєстрації

    await record_audit(
        session,
        action=AuditAction.DEVICE_DELETED,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"name": device.name, "key_fingerprint": device.key_fingerprint},
    )
    await session.commit()
    return MessageResponse(message="Пристрій знято з обліку")


# ------------------------------------------- облікові дані пристрою -------


@router.get("/{device_id}/credentials", response_model=DeviceKeyStatus)
async def device_credentials(
    device_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> DeviceKeyStatus:
    """Стан ключа пристрою: покоління, вік і чи час його перевипускати."""
    device = await _get_device(session, device_id)
    settings = get_settings()
    return DeviceKeyStatus(
        device_id=device.id,
        key_algorithm=device.key_algorithm,
        key_fingerprint=device.key_fingerprint,
        key_generation=device.key_generation,
        key_created_at=device.key_created_at,
        key_rotated_at=device.key_rotated_at,
        key_age_days=device.key_age_days,
        rotation_due=enrollment_service.key_rotation_due(device),
        max_age_days=settings.device_key_max_age_days,
    )


@router.get("/{device_id}/credentials/history", response_model=list[DeviceKeyHistoryOut])
async def device_key_history(
    device_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[DeviceKeyHistoryOut]:
    """Відкликані ключі пристрою. Приватних частин тут немає й не було."""
    device = await _get_device(session, device_id)
    rows = (
        await session.execute(
            sa.select(DeviceKeyHistory)
            .where(DeviceKeyHistory.device_id == device.id)
            .order_by(DeviceKeyHistory.retired_at.desc())
            .limit(100)
        )
    ).scalars().all()
    return [DeviceKeyHistoryOut.model_validate(row) for row in rows]


@router.post(
    "/{device_id}/credentials/rotate",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("mutation"))],
)
async def require_credential_rotation(
    request: Request,
    device_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.DEVICE_CREDENTIAL_ROTATE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    """Відкликає токени пристрою й вимагає перевипуску ключа.

    Сам ключ підмінити з панелі не можна — приватна частина живе лише в
    Android Keystore. Сервер лише анулює доступ: агент, підключившись,
    згенерує нову пару й підтвердить її підписом старої.
    """
    ip, user_agent = request_context(request)
    device = await _get_device(session, device_id)
    await enrollment_service.require_key_rotation(
        session,
        device=device,
        user=user,
        reason="requested_by_admin",
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()
    return MessageResponse(
        message="Токени пристрою відкликано; агент перевипустить ключ при підключенні"
    )


# ------------------------------------------------------- дії над пристроєм -
#
# Кожна дія — команда з реєстру. Ці ендпоїнти існують для зручності панелі;
# те саме доступне через `POST /devices/{id}/commands` з відповідним типом.
# Дозвіл перевіряє реєстр, тож перенастроєна матриця діє в обох місцях.


async def _issue(
    request: Request,
    session: AsyncSession,
    redis: Redis,
    *,
    device_id: uuid.UUID,
    user: User,
    command_type: str,
    params: dict | None = None,
) -> CommandOut:
    ip, user_agent = request_context(request)
    device = await _get_device(session, device_id)
    command = await command_service.issue(
        session,
        device=device,
        command_type=command_type,
        params=params,
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.post(
    "/{device_id}/reboot",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reboot_device(
    request: Request,
    device_id: uuid.UUID,
    reason: str = Query(default="", max_length=200),
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Перезавантаження через `DevicePolicyManager.reboot()`.

    Потребує режиму Device Owner — це вимога самого Android, а не наша
    вигадка. Пристрій у режимі `unmanaged` отримає зрозумілу відмову одразу,
    а не «failed» через кілька хвилин.
    """
    return await _issue(
        request,
        session,
        redis,
        device_id=device_id,
        user=user,
        command_type=command_registry.REBOOT,
        params={"reason": reason},
    )


@router.post(
    "/{device_id}/telemetry/refresh",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_telemetry(
    request: Request,
    device_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Позачерговий збір телеметрії."""
    return await _issue(
        request,
        session,
        redis,
        device_id=device_id,
        user=user,
        command_type=command_registry.REFRESH_TELEMETRY,
    )


@router.post(
    "/{device_id}/config/sync",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_configuration(
    request: Request,
    device_id: uuid.UUID,
    force: bool = Query(default=False),
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Агент перечитує з сервера свою конфігурацію."""
    return await _issue(
        request,
        session,
        redis,
        device_id=device_id,
        user=user,
        command_type=command_registry.SYNC_CONFIG,
        params={"force": force},
    )


@router.post(
    "/{device_id}/agent/restart",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def restart_agent(
    request: Request,
    device_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Перезапуск фонового сервісу агента.

    Застосунок не перевстановлюється й дані не видаляються: сервіс
    зупиняється і піднімається знову. Постійне сповіщення на мить зникне —
    це видимий для власника пристрою наслідок, і так і має бути.
    """
    return await _issue(
        request,
        session,
        redis,
        device_id=device_id,
        user=user,
        command_type=command_registry.AGENT_RESTART,
    )


@router.post(
    "/{device_id}/diagnostics",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def collect_diagnostics(
    request: Request,
    device_id: uuid.UUID,
    include_agent_log: bool = Query(default=True),
    include_permissions: bool = Query(default=True),
    include_storage: bool = Query(default=True),
    log_lines: int = Query(default=200, ge=0, le=2000),
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Звіт про стан самого агента.

    Збираються лише власні дані агента й відкриті системні лічильники.
    Системний журнал цілком, дані інших застосунків і файли користувача
    сюди не потрапляють — для файлів є окремий модуль зі своїм allowlist.
    """
    return await _issue(
        request,
        session,
        redis,
        device_id=device_id,
        user=user,
        command_type=command_registry.COLLECT_DIAGNOSTICS,
        params={
            "include_agent_log": include_agent_log,
            "include_permissions": include_permissions,
            "include_storage": include_storage,
            "log_lines": log_lines,
        },
    )
