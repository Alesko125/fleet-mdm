"""Приймання телеметрії пристрою.

Один «зріз» (`device_telemetry`) завжди відображає поточний стан, а історія
(`telemetry_samples`) потрібна для графіків і прибирається ретеншном.

Телеметрія **не пишеться в аудит**: вона надходить щохвилини від кожного
пристрою і засмітила б журнал адміністративних дій. Натомість факт її
надходження видно у `last_seen_at`, а всі похідні події (алерти) —
повноцінні записи аудиту.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utcnow
from app.db.models import Device, DeviceTelemetry, TelemetrySample
from app.schemas.telemetry import TelemetryIn

logger = logging.getLogger(__name__)

# Поля, які просто копіюються у зріз стану.
SNAPSHOT_FIELDS = (
    "manufacturer",
    "model",
    "product",
    "android_version",
    "sdk_int",
    "security_patch",
    "build_id",
    "kernel_version",
    "agent_version",
    "uptime_seconds",
    "battery_level",
    "battery_status",
    "battery_health",
    "battery_temperature_c",
    "charging",
    "ram_total_bytes",
    "ram_available_bytes",
    "storage_total_bytes",
    "storage_free_bytes",
    "cpu_cores",
    "cpu_load_percent",
    "cpu_abi",
    "network_type",
    "network_operator",
)


async def ingest(
    session: AsyncSession, *, device: Device, payload: TelemetryIn, ip: str | None = None
) -> DeviceTelemetry:
    """Оновлює зріз стану пристрою і додає точку в історію."""
    now = utcnow()
    collected_at = payload.collected_at or now

    snapshot = await session.get(DeviceTelemetry, device.id)
    if snapshot is None:
        snapshot = DeviceTelemetry(device_id=device.id)
        session.add(snapshot)

    for field in SNAPSHOT_FIELDS:
        value = getattr(payload, field)
        if value is not None:
            setattr(snapshot, field, value)
    snapshot.collected_at = collected_at
    snapshot.updated_at = now

    session.add(
        TelemetrySample(
            device_id=device.id,
            collected_at=collected_at,
            battery_level=payload.battery_level,
            charging=payload.charging,
            uptime_seconds=payload.uptime_seconds,
            ram_available_bytes=payload.ram_available_bytes,
            storage_free_bytes=payload.storage_free_bytes,
            cpu_load_percent=payload.cpu_load_percent,
            network_type=payload.network_type,
        )
    )

    device.last_seen_at = now
    if payload.agent_version:
        device.agent_version = payload.agent_version
    if ip:
        device.last_ip = ip

    return snapshot


def free_percent(free: int | None, total: int | None) -> float | None:
    if not total or free is None:
        return None
    return round(free * 100 / total, 1)
