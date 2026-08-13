"""Сервіс геолокації.

Принципи модуля:
  * за замовчуванням **вимкнено** — рядок `location_settings` створюється
    під час enrollment з `enabled=False`;
  * сервер приймає координати **лише** коли модуль увімкнений для цього
    пристрою; інакше — 403 і запис в аудит;
  * будь-яка зміна конфігурації пише в аудит попереднє й нове значення;
  * історія прибирається за політикою ретеншну (`LOCATION_HISTORY_DAYS`).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import as_utc, utcnow
from app.db.models import Device, LocationFix, LocationSettings, User
from app.schemas.location import (
    AgentLocationConfig,
    LocationFixIn,
    LocationPermissionReport,
    LocationSettingsUpdate,
)
from app.services.audit import RESULT_DENIED, AuditAction, record_audit

logger = logging.getLogger(__name__)

# Координати, «свіжіші» за майбутнє або старіші за це, вважаємо непридатними.
MAX_FIX_AGE = timedelta(days=7)
MAX_FIX_SKEW = timedelta(minutes=10)


async def get_or_create_settings(
    session: AsyncSession, device: Device
) -> LocationSettings:
    settings = await session.get(LocationSettings, device.id)
    if settings is None:
        settings = LocationSettings(device_id=device.id, enabled=False)
        session.add(settings)
        await session.flush()
    return settings


def to_agent_config(settings: LocationSettings) -> AgentLocationConfig:
    app_settings = get_settings()
    return AgentLocationConfig(
        enabled=settings.enabled,
        interval_seconds=settings.interval_seconds,
        min_distance_meters=settings.min_distance_meters,
        high_accuracy=settings.high_accuracy,
        retention_days=app_settings.location_history_days,
        server_time=utcnow(),
    )


async def update_settings(
    session: AsyncSession,
    *,
    device: Device,
    payload: LocationSettingsUpdate,
    user: User,
    ip: str | None,
    user_agent: str | None,
) -> tuple[LocationSettings, dict[str, dict[str, object]]]:
    """Змінює конфігурацію та фіксує в аудиті кожне поле «було → стало»."""
    settings = await get_or_create_settings(session, device)

    changes: dict[str, dict[str, object]] = {}
    for field in ("enabled", "interval_seconds", "min_distance_meters", "high_accuracy"):
        new_value = getattr(payload, field)
        if new_value is None:
            continue
        old_value = getattr(settings, field)
        if old_value == new_value:
            continue
        changes[field] = {"from": old_value, "to": new_value}
        setattr(settings, field, new_value)

    if changes:
        settings.updated_by_id = user.id
        settings.updated_at = utcnow()
        if changes.get("enabled", {}).get("to") is True:
            # Фіксуємо момент, коли модуль було увімкнено.
            settings.consent_recorded_at = utcnow()

    action = (
        AuditAction.LOCATION_DISABLED
        if changes.get("enabled", {}).get("to") is False
        else AuditAction.LOCATION_CONFIG_CHANGED
    )
    await record_audit(
        session,
        action=action,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "changes": changes or "без змін",
            "resulting_config": {
                "enabled": settings.enabled,
                "interval_seconds": settings.interval_seconds,
                "min_distance_meters": settings.min_distance_meters,
                "high_accuracy": settings.high_accuracy,
            },
        },
    )
    return settings, changes


async def record_permission_state(
    session: AsyncSession,
    *,
    device: Device,
    report: LocationPermissionReport,
    ip: str | None,
    user_agent: str | None,
) -> LocationSettings:
    """Агент повідомляє фактичний стан дозволів на пристрої."""
    settings = await get_or_create_settings(session, device)
    changed = (
        settings.permission_state != report.permission_state
        or settings.background_permission != report.background_permission
    )
    previous = {
        "permission_state": settings.permission_state,
        "background_permission": settings.background_permission,
    }

    settings.permission_state = report.permission_state
    settings.background_permission = report.background_permission
    settings.permission_reported_at = utcnow()

    if changed:
        await record_audit(
            session,
            action=AuditAction.LOCATION_PERMISSION_CHANGED,
            device=device,
            object_type="device",
            object_id=device.id,
            ip=ip,
            user_agent=user_agent,
            details={
                "from": previous,
                "to": {
                    "permission_state": report.permission_state,
                    "background_permission": report.background_permission,
                },
            },
        )
    return settings


async def store_fixes(
    session: AsyncSession,
    *,
    device: Device,
    fixes: list[LocationFixIn],
    ip: str | None,
    user_agent: str | None,
) -> tuple[int, int, LocationSettings]:
    """Приймає пакет координат. Повертає `(прийнято, відхилено, налаштування)`.

    Викидає `PermissionError`, якщо модуль вимкнений — сервер ніколи не
    зберігає координати пристрою з вимкненою геолокацією.
    """
    settings = await get_or_create_settings(session, device)

    if not settings.enabled:
        await record_audit(
            session,
            action=AuditAction.LOCATION_UPLOAD_REJECTED,
            result=RESULT_DENIED,
            device=device,
            object_type="device",
            object_id=device.id,
            ip=ip,
            user_agent=user_agent,
            details={"reason": "module_disabled", "dropped": len(fixes)},
        )
        raise PermissionError("Модуль геолокації вимкнений для цього пристрою")

    now = utcnow()
    accepted = 0
    rejected = 0
    newest: LocationFix | None = None

    for fix in fixes:
        recorded_at = as_utc(fix.recorded_at)
        if recorded_at is None:
            rejected += 1
            continue
        if recorded_at > now + MAX_FIX_SKEW or recorded_at < now - MAX_FIX_AGE:
            # Занадто «майбутня» або протухла координата — не зберігаємо.
            rejected += 1
            continue

        row = LocationFix(
            device_id=device.id,
            latitude=fix.latitude,
            longitude=fix.longitude,
            accuracy_m=fix.accuracy_m,
            altitude_m=fix.altitude_m,
            speed_mps=fix.speed_mps,
            bearing_deg=fix.bearing_deg,
            provider=fix.provider,
            source=fix.source,
            is_mock=fix.is_mock,
            battery_level=fix.battery_level,
            recorded_at=recorded_at,
            received_at=now,
        )
        session.add(row)
        accepted += 1
        if newest is None or recorded_at > as_utc(newest.recorded_at):
            newest = row

    if newest is not None:
        settings.last_fix_at = as_utc(newest.recorded_at)
    device.last_seen_at = now
    return accepted, rejected, settings


async def latest_fix(session: AsyncSession, device_id: uuid.UUID) -> LocationFix | None:
    return (
        await session.execute(
            sa.select(LocationFix)
            .where(LocationFix.device_id == device_id)
            .order_by(LocationFix.recorded_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def history(
    session: AsyncSession,
    device_id: uuid.UUID,
    *,
    since=None,
    until=None,
    limit: int = 500,
    offset: int = 0,
) -> tuple[list[LocationFix], int]:
    conditions = [LocationFix.device_id == device_id]
    if since is not None:
        conditions.append(LocationFix.recorded_at >= since)
    if until is not None:
        conditions.append(LocationFix.recorded_at <= until)

    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(LocationFix).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(LocationFix)
            .where(*conditions)
            .order_by(LocationFix.recorded_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return list(rows), total


async def fix_stats(session: AsyncSession, device_id: uuid.UUID) -> tuple[int, object | None]:
    row = (
        await session.execute(
            sa.select(
                sa.func.count(LocationFix.id), sa.func.min(LocationFix.recorded_at)
            ).where(LocationFix.device_id == device_id)
        )
    ).one()
    return int(row[0] or 0), row[1]


async def purge_history(
    session: AsyncSession,
    *,
    device_id: uuid.UUID | None = None,
    older_than_days: int | None = None,
) -> int:
    """Видаляє координати. Без `older_than_days` — усю історію пристрою."""
    conditions = []
    if device_id is not None:
        conditions.append(LocationFix.device_id == device_id)
    if older_than_days is not None:
        cutoff = utcnow() - timedelta(days=older_than_days)
        conditions.append(LocationFix.received_at < cutoff)

    result = await session.execute(sa.delete(LocationFix).where(*conditions))
    return int(result.rowcount or 0)


def reporting_active(settings: LocationSettings) -> bool:
    """Чи справді йде звітування: модуль увімкнено і дозвіл надано."""
    return settings.enabled and settings.permission_state in ("granted_fine", "granted_coarse")
