"""Політика зберігання даних.

Персональні дані не лежать вічно: історія переміщень, телеметрія та аудит
прибираються за налаштованими строками (`*_DAYS` у конфігурації).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import utcnow
from app.db.models import AuditLog, DeviceKeyHistory, LocationFix, TelemetrySample
from app.services import auth as auth_service
from app.services.audit import AuditAction, record_audit

logger = logging.getLogger(__name__)

RETENTION_INTERVAL_SECONDS = 6 * 60 * 60


async def _delete_older_than(session: AsyncSession, model, column, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    result = await session.execute(sa.delete(model).where(column < cutoff))
    return int(result.rowcount or 0)


async def run_retention(session: AsyncSession, settings: Settings | None = None) -> dict[str, int]:
    """Прибирає прострочені дані. Повертає кількість видалених рядків."""
    settings = settings or get_settings()

    removed = {
        "location_fixes": await _delete_older_than(
            session, LocationFix, LocationFix.received_at, settings.location_history_days
        ),
        "telemetry_samples": await _delete_older_than(
            session,
            TelemetrySample,
            TelemetrySample.collected_at,
            settings.telemetry_history_days,
        ),
        "audit_log": await _delete_older_than(
            session, AuditLog, AuditLog.occurred_at, settings.audit_retention_days
        ),
        # Прострочені refresh-токени лише роздувають таблицю: перевіряти
        # їх усе одно нема сенсу, а зайві рядки — це зайва поверхня.
        "refresh_tokens": await auth_service.prune_expired_refresh_tokens(session),
        # Відкликані публічні ключі пристроїв (приватних тут ніколи не було).
        "device_key_history": await _delete_older_than(
            session,
            DeviceKeyHistory,
            DeviceKeyHistory.retired_at,
            settings.device_key_history_days,
        ),
    }

    # Комунікаційні дані мають per-device ретеншн — окремою функцією.
    from app.services import comms as comms_service

    removed.update(await comms_service.purge_expired(session))

    if any(removed.values()):
        await record_audit(
            session,
            action=AuditAction.RETENTION_RUN,
            actor_label="retention",
            details={
                "removed": removed,
                "policy_days": {
                    "location": settings.location_history_days,
                    "telemetry": settings.telemetry_history_days,
                    "audit": settings.audit_retention_days,
                    "device_key_history": settings.device_key_history_days,
                },
            },
        )
    await session.commit()
    logger.info("Ретеншн виконано", extra={"removed": removed})
    return removed


async def retention_loop(session_factory, stop_event: asyncio.Event) -> None:
    """Фонова задача: прибирання кожні 6 годин, поки застосунок працює."""
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                await run_retention(session)
        except Exception as exc:  # noqa: BLE001 — задача не має падати назавжди
            logger.warning("Ретеншн завершився помилкою", extra={"error": str(exc)})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RETENTION_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
