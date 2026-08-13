"""Обслуговування: політика зберігання та стан ротації секретів."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.config import get_settings
from app.core.rbac import Permission
from app.core.security import utcnow
from app.db.models import Device, User
from app.db.session import get_session
from app.schemas.common import MessageResponse
from app.services import enrollment as enrollment_service
from app.services.audit import AuditAction, record_audit
from app.services.retention import run_retention

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/retention")
async def retention_policy(
    _: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
) -> dict[str, int]:
    settings = get_settings()
    return {
        "location_history_days": settings.location_history_days,
        "telemetry_history_days": settings.telemetry_history_days,
        "audit_retention_days": settings.audit_retention_days,
    }


@router.post("/retention")
async def trigger_retention(
    request: Request,
    user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Позачерговий запуск прибирання (штатно виконується кожні 6 годин)."""
    ip, user_agent = request_context(request)
    removed = await run_retention(session)
    await record_audit(
        session,
        action=AuditAction.RETENTION_RUN,
        user=user,
        ip=ip,
        user_agent=user_agent,
        details={"removed": removed, "trigger": "manual"},
    )
    await session.commit()
    return removed


# ------------------------------------------------------ ротація секретів ---


@router.get("/secrets")
async def secret_rotation_status(
    request: Request,
    user: User = Depends(require_permission(Permission.SECRET_ROTATE)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Стан ротації серверних секретів.

    Самих секретів тут немає й бути не може — лише те, чи налаштований
    перехідний період і скільки ще діють токени, підписані старим ключем.
    Значення задаються змінними оточення й перечитуються при перезапуску:
    зберігати їх у БД означало б зробити з бази ще одне сховище ключів.
    """
    ip, user_agent = request_context(request)
    settings = get_settings()

    await record_audit(
        session,
        action=AuditAction.SECRET_ROTATION_VIEWED,
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()

    epoch = settings.token_epoch_at
    return {
        "jwt": {
            "algorithm": settings.jwt_algorithm,
            # Перехідний період: старим ключем ще перевіряємо, новим — підписуємо.
            "previous_secret_configured": bool(settings.jwt_secret_previous),
            "access_token_ttl_seconds": settings.access_token_ttl_seconds,
            "refresh_token_ttl_seconds": settings.refresh_token_ttl_seconds,
            # Після цієї паузи попередній ключ можна прибрати з оточення.
            "safe_to_drop_previous_after_seconds": settings.refresh_token_ttl_seconds,
        },
        "token_hash": {
            "previous_secret_configured": bool(settings.token_hash_secret_previous),
            # Enrollment-токени переживають найдовше — орієнтуємось на них.
            "safe_to_drop_previous_after_seconds": max(
                settings.refresh_token_ttl_seconds, settings.enrollment_token_ttl_seconds
            ),
        },
        "token_epoch": epoch.isoformat() if epoch else None,
        "device_keys": {
            "max_age_days": settings.device_key_max_age_days,
            "daily_rotation_limit": settings.device_key_rotation_daily_limit,
            "history_retention_days": settings.device_key_history_days,
        },
        "procedure": (
            "1) JWT_SECRET_PREVIOUS=<старий>, JWT_SECRET=<новий>, перезапуск; "
            "2) зачекати повний TTL refresh-токена; "
            "3) прибрати JWT_SECRET_PREVIOUS. Те саме для TOKEN_HASH_SECRET. "
            "Негайне відкликання всіх сесій — TOKEN_EPOCH=<ISO-8601 зараз>."
        ),
    }


@router.get("/device-keys")
async def device_key_overview(
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Скільки пристроїв мають ключ, що вже час перевипустити."""
    settings = get_settings()
    devices = (
        await session.execute(sa.select(Device).where(Device.deleted_at.is_(None)))
    ).scalars().all()

    due = [d for d in devices if enrollment_service.key_rotation_due(d)]
    return {
        "total": len(devices),
        "rotation_due": len(due),
        "max_age_days": settings.device_key_max_age_days,
        "devices": [
            {
                "id": str(d.id),
                "name": d.name,
                "key_age_days": d.key_age_days,
                "key_generation": d.key_generation,
            }
            for d in sorted(due, key=lambda d: d.key_age_days, reverse=True)[:100]
        ],
    }


@router.post("/device-keys/rotate-overdue", response_model=MessageResponse)
async def rotate_overdue_device_keys(
    request: Request,
    user: User = Depends(require_permission(Permission.DEVICE_CREDENTIAL_ROTATE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    """Відкликає токени всіх пристроїв із простроченим ключем.

    Самі ключі змінюють агенти: приватна частина живе тільки в Android
    Keystore. Сервер лише знімає доступ, після чого агент при підключенні
    згенерує нову пару й підтвердить її підписом старої.
    """
    ip, user_agent = request_context(request)
    devices = (
        await session.execute(sa.select(Device).where(Device.deleted_at.is_(None)))
    ).scalars().all()

    touched = 0
    for device in devices:
        if not enrollment_service.key_rotation_due(device):
            continue
        await enrollment_service.require_key_rotation(
            session,
            device=device,
            user=user,
            reason="key_max_age_exceeded",
            ip=ip,
            user_agent=user_agent,
        )
        touched += 1

    await record_audit(
        session,
        action=AuditAction.SECRET_ROTATION_STAGED,
        user=user,
        ip=ip,
        user_agent=user_agent,
        details={"devices_marked": touched, "at": utcnow()},
    )
    await session.commit()
    return MessageResponse(
        message=f"Позначено пристроїв для перевипуску ключа: {touched}"
    )