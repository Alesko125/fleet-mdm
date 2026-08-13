"""Рушій моніторингу: правила, оцінка стану, історія сповіщень.

Логіка навмисно проста й передбачувана:

1. правило + пристрій → 0..N «знахідок» (`Finding`), кожна зі своїм
   ключем дедуплікації;
2. знахідка без активного алерта → новий алерт (`firing`) + аудит + подія у WS;
3. активний алерт, знахідка якого зникла → `resolved` + аудит + подія;
4. `cooldown_seconds` не дає правилу миготіти: після закриття алерта той
   самий ключ мовчить задану кількість секунд.

Оцінка спирається **виключно на технічні показники стану пристрою**:
заряд, памʼять, сховище, CPU, версії ПЗ, час останнього звʼязку. Жодних
додаткових персональних даних заради моніторингу не збирається.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis_client import ADMIN_CHANNEL
from app.core.security import as_utc, utcnow
from app.db.models import (
    Alert,
    AlertRule,
    ApkArtifact,
    Command,
    Device,
    DeviceApp,
    DeviceTelemetry,
    LocationSettings,
    User,
)
from app.services.audit import AuditAction, json_safe, record_audit
from app.services.telemetry import free_percent
from app.ws.manager import publish

logger = logging.getLogger(__name__)

RULE_TYPES = (
    "DEVICE_OFFLINE",
    "LOW_BATTERY",
    "LOW_STORAGE",
    "LOW_MEMORY",
    "HIGH_CPU",
    "OUTDATED_AGENT",
    "OUTDATED_ANDROID",
    "OUTDATED_APP",
    "LOCATION_REPORTING_STOPPED",
    "COMMAND_FAILURES",
    "ENROLLMENT_STATUS",
)

# Пороги за замовчуванням — використовуються, якщо в правилі поле не задане.
DEFAULT_THRESHOLDS: dict[str, dict[str, Any]] = {
    "DEVICE_OFFLINE": {"offline_minutes": 30},
    "LOW_BATTERY": {"battery_percent": 20, "ignore_when_charging": True},
    "LOW_STORAGE": {"free_percent": 10},
    "LOW_MEMORY": {"free_percent": 10},
    "HIGH_CPU": {"load_percent": 90},
    "OUTDATED_AGENT": {"min_version": "0.1.0"},
    "OUTDATED_ANDROID": {"min_sdk": 26},
    "OUTDATED_APP": {"packages": []},
    "LOCATION_REPORTING_STOPPED": {"silent_minutes": 180},
    "COMMAND_FAILURES": {"count": 3, "window_minutes": 60},
    "ENROLLMENT_STATUS": {"states": ["suspended", "pending"]},
}


@dataclass(slots=True)
class Finding:
    """Одна проблема, знайдена правилом на пристрої."""

    dedup_key: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DeviceContext:
    """Усе, що потрібно правилам, зібране один раз на пристрій."""

    device: Device
    telemetry: DeviceTelemetry | None
    location: LocationSettings | None
    apps: list[DeviceApp]
    repository: dict[str, ApkArtifact]
    recent_failures: list[Command]


# ------------------------------------------------------------ утиліти -----


def parse_version(value: str | None) -> tuple[int, ...]:
    """`"1.12.3"` → `(1, 12, 3)`; нечислові частини ігноруються."""
    if not value:
        return ()
    parts: list[int] = []
    for chunk in str(value).replace("-", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def version_lt(left: str | None, right: str | None) -> bool:
    """Чи менша версія `left` за `right` (порожні вважаємо застарілими)."""
    left_parts, right_parts = parse_version(left), parse_version(right)
    if not right_parts:
        return False
    if not left_parts:
        return True
    length = max(len(left_parts), len(right_parts))
    padded_left = left_parts + (0,) * (length - len(left_parts))
    padded_right = right_parts + (0,) * (length - len(right_parts))
    return padded_left < padded_right


def threshold(rule: AlertRule, key: str, default: Any = None) -> Any:
    values = rule.thresholds or {}
    if key in values and values[key] is not None:
        return values[key]
    return DEFAULT_THRESHOLDS.get(rule.type, {}).get(key, default)


# ------------------------------------------------------------ контекст ----


async def build_context(session: AsyncSession, device: Device) -> DeviceContext:
    settings = get_settings()
    telemetry = await session.get(DeviceTelemetry, device.id)
    location = await session.get(LocationSettings, device.id)

    apps = list(
        (
            await session.execute(sa.select(DeviceApp).where(DeviceApp.device_id == device.id))
        ).scalars().all()
    )

    repository: dict[str, ApkArtifact] = {}
    if apps:
        packages = [app.package_name for app in apps]
        rows = (
            await session.execute(
                sa.select(ApkArtifact)
                .where(ApkArtifact.package_name.in_(packages))
                .order_by(
                    ApkArtifact.package_name,
                    ApkArtifact.version_code.desc().nullslast(),
                    ApkArtifact.created_at.desc(),
                )
            )
        ).scalars().all()
        for row in rows:
            if row.package_name and row.package_name not in repository:
                repository[row.package_name] = row

    window_start = utcnow() - timedelta(hours=24)
    failures = list(
        (
            await session.execute(
                sa.select(Command)
                .where(
                    Command.device_id == device.id,
                    Command.status.in_(("failed", "expired")),
                    Command.created_at >= window_start,
                )
                .order_by(Command.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
    )
    _ = settings
    return DeviceContext(
        device=device,
        telemetry=telemetry,
        location=location,
        apps=apps,
        repository=repository,
        recent_failures=failures,
    )


# -------------------------------------------------------------- правила ---


def evaluate_rule(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    """Повертає перелік знахідок правила для конкретного пристрою."""
    handler = _HANDLERS.get(rule.type)
    if handler is None:  # pragma: no cover — тип валідується схемою
        logger.warning("Невідомий тип правила", extra={"type": rule.type})
        return []
    return handler(rule, ctx)


def _offline(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    minutes = int(threshold(rule, "offline_minutes", 30))
    last_seen = as_utc(ctx.device.last_seen_at)
    if last_seen is None:
        return [
            Finding(
                "offline",
                "Пристрій жодного разу не виходив на звʼязок після реєстрації",
                {"offline_minutes": minutes, "last_seen_at": None},
            )
        ]
    silent_for = (utcnow() - last_seen).total_seconds() / 60
    if silent_for >= minutes:
        return [
            Finding(
                "offline",
                f"Немає звʼязку {int(silent_for)} хв (поріг {minutes} хв)",
                {"offline_minutes": round(silent_for, 1), "last_seen_at": last_seen},
            )
        ]
    return []


def _low_battery(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    telemetry = ctx.telemetry
    if telemetry is None or telemetry.battery_level is None:
        return []
    limit = int(threshold(rule, "battery_percent", 20))
    if threshold(rule, "ignore_when_charging", True) and telemetry.charging:
        return []
    if telemetry.battery_level <= limit:
        return [
            Finding(
                "battery",
                f"Заряд {telemetry.battery_level}% (поріг {limit}%)",
                {"battery_level": telemetry.battery_level, "threshold": limit},
            )
        ]
    return []


def _low_storage(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    telemetry = ctx.telemetry
    if telemetry is None:
        return []
    percent = free_percent(telemetry.storage_free_bytes, telemetry.storage_total_bytes)
    limit_percent = threshold(rule, "free_percent", 10)
    limit_mb = threshold(rule, "free_mb")

    if percent is not None and limit_percent is not None and percent <= float(limit_percent):
        return [
            Finding(
                "storage",
                f"Вільно {percent}% сховища (поріг {limit_percent}%)",
                {"free_percent": percent, "threshold_percent": limit_percent},
            )
        ]
    if limit_mb and telemetry.storage_free_bytes is not None:
        free_mb = telemetry.storage_free_bytes / (1024 * 1024)
        if free_mb <= float(limit_mb):
            return [
                Finding(
                    "storage",
                    f"Вільно {int(free_mb)} МБ сховища (поріг {limit_mb} МБ)",
                    {"free_mb": round(free_mb, 1), "threshold_mb": limit_mb},
                )
            ]
    return []


def _low_memory(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    telemetry = ctx.telemetry
    if telemetry is None:
        return []
    percent = free_percent(telemetry.ram_available_bytes, telemetry.ram_total_bytes)
    limit = float(threshold(rule, "free_percent", 10))
    if percent is not None and percent <= limit:
        return [
            Finding(
                "memory",
                f"Вільно {percent}% оперативної памʼяті (поріг {limit}%)",
                {"free_percent": percent, "threshold": limit},
            )
        ]
    return []


def _high_cpu(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    telemetry = ctx.telemetry
    if telemetry is None or telemetry.cpu_load_percent is None:
        return []
    limit = float(threshold(rule, "load_percent", 90))
    if telemetry.cpu_load_percent >= limit:
        return [
            Finding(
                "cpu",
                f"Навантаження CPU {telemetry.cpu_load_percent}% (поріг {limit}%)",
                {"cpu_load_percent": telemetry.cpu_load_percent, "threshold": limit},
            )
        ]
    return []


def _outdated_agent(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    minimum = threshold(rule, "min_version")
    current = ctx.device.agent_version
    if not minimum:
        return []
    if version_lt(current, minimum):
        return [
            Finding(
                "agent",
                f"Версія агента {current or 'невідома'} старша за мінімальну {minimum}",
                {"agent_version": current, "min_version": minimum},
            )
        ]
    return []


def _outdated_android(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    telemetry = ctx.telemetry
    minimum = threshold(rule, "min_sdk", 26)
    if telemetry is None or telemetry.sdk_int is None or minimum is None:
        return []
    if telemetry.sdk_int < int(minimum):
        return [
            Finding(
                "android",
                f"Android SDK {telemetry.sdk_int} нижчий за мінімальний {minimum}",
                {
                    "sdk_int": telemetry.sdk_int,
                    "android_version": telemetry.android_version,
                    "min_sdk": minimum,
                },
            )
        ]
    return []


def _outdated_app(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    watched = threshold(rule, "packages", []) or []
    findings: list[Finding] = []

    for app in ctx.apps:
        if watched and app.package_name not in watched:
            continue
        artifact = ctx.repository.get(app.package_name)
        if artifact is None or artifact.version_code is None:
            continue
        if app.version_code is not None and app.version_code >= artifact.version_code:
            continue
        findings.append(
            Finding(
                f"app:{app.package_name}",
                (
                    f"{app.package_name}: встановлено "
                    f"{app.version_name or app.version_code}, у репозиторії "
                    f"{artifact.version_name or artifact.version_code}"
                ),
                {
                    "package_name": app.package_name,
                    "installed_version_code": app.version_code,
                    "repository_version_code": artifact.version_code,
                    "apk_id": str(artifact.id),
                },
            )
        )
    return findings


def _location_stopped(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    settings = ctx.location
    # Правило має сенс лише коли модуль увімкнено адміністратором.
    if settings is None or not settings.enabled:
        return []

    minutes = int(threshold(rule, "silent_minutes", 180))
    last_fix = as_utc(settings.last_fix_at)

    if settings.permission_state in ("denied", "restricted"):
        return [
            Finding(
                "location",
                f"Геолокацію увімкнено, але дозвіл на пристрої: {settings.permission_state}",
                {"permission_state": settings.permission_state},
            )
        ]
    if last_fix is None:
        return [
            Finding(
                "location",
                "Геолокацію увімкнено, але координати ще не надходили",
                {"last_fix_at": None, "silent_minutes": minutes},
            )
        ]
    silent_for = (utcnow() - last_fix).total_seconds() / 60
    if silent_for >= minutes:
        return [
            Finding(
                "location",
                f"Координати не надходять {int(silent_for)} хв (поріг {minutes} хв)",
                {"last_fix_at": last_fix, "silent_minutes": round(silent_for, 1)},
            )
        ]
    return []


def _command_failures(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    count = int(threshold(rule, "count", 3))
    window = int(threshold(rule, "window_minutes", 60))
    since = utcnow() - timedelta(minutes=window)

    recent = [
        command
        for command in ctx.recent_failures
        if (as_utc(command.created_at) or utcnow()) >= since
    ]
    if len(recent) >= count:
        return [
            Finding(
                "command_failures",
                f"{len(recent)} невдалих команд за {window} хв (поріг {count})",
                {
                    "failures": len(recent),
                    "window_minutes": window,
                    "types": sorted({command.type for command in recent}),
                },
            )
        ]
    return []


def _enrollment_status(rule: AlertRule, ctx: DeviceContext) -> list[Finding]:
    states = threshold(rule, "states", ["suspended", "pending"]) or []
    if ctx.device.status in states:
        return [
            Finding(
                "enrollment",
                f"Статус реєстрації: {ctx.device.status}",
                {"status": ctx.device.status},
            )
        ]
    return []


_HANDLERS = {
    "DEVICE_OFFLINE": _offline,
    "LOW_BATTERY": _low_battery,
    "LOW_STORAGE": _low_storage,
    "LOW_MEMORY": _low_memory,
    "HIGH_CPU": _high_cpu,
    "OUTDATED_AGENT": _outdated_agent,
    "OUTDATED_ANDROID": _outdated_android,
    "OUTDATED_APP": _outdated_app,
    "LOCATION_REPORTING_STOPPED": _location_stopped,
    "COMMAND_FAILURES": _command_failures,
    "ENROLLMENT_STATUS": _enrollment_status,
}


# ------------------------------------------------------------- оцінка -----


async def applicable_rules(session: AsyncSession, device: Device) -> list[AlertRule]:
    rows = (
        await session.execute(
            sa.select(AlertRule).where(
                AlertRule.enabled.is_(True),
                sa.or_(AlertRule.device_id.is_(None), AlertRule.device_id == device.id),
            )
        )
    ).scalars().all()
    return list(rows)


async def evaluate_device(
    session: AsyncSession,
    device: Device,
    *,
    redis: Redis | None = None,
    rules: list[AlertRule] | None = None,
) -> tuple[int, int]:
    """Оцінює всі правила для пристрою. Повертає `(відкрито, закрито)`."""
    rules = rules if rules is not None else await applicable_rules(session, device)
    if not rules:
        return 0, 0

    ctx = await build_context(session, device)
    now = utcnow()
    raised = resolved = 0

    for rule in rules:
        findings = evaluate_rule(rule, ctx)
        found_keys = {finding.dedup_key for finding in findings}

        existing = (
            await session.execute(
                sa.select(Alert).where(
                    Alert.device_id == device.id,
                    Alert.rule_id == rule.id,
                    Alert.state.in_(("firing", "acknowledged")),
                )
            )
        ).scalars().all()
        by_key = {alert.dedup_key: alert for alert in existing}

        for finding in findings:
            active = by_key.get(finding.dedup_key)
            if active is not None:
                active.last_seen_at = now
                active.message = finding.message[:500]
                active.details = json_safe(finding.details)
                continue

            if await _in_cooldown(session, rule, device, finding.dedup_key, now):
                continue

            alert = Alert(
                rule_id=rule.id,
                device_id=device.id,
                type=rule.type,
                severity=rule.severity,
                state="firing",
                dedup_key=finding.dedup_key,
                message=finding.message[:500],
                details=json_safe(finding.details),
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(alert)
            await session.flush()
            raised += 1

            await record_audit(
                session,
                action=AuditAction.ALERT_RAISED,
                actor_label="monitoring",
                object_type="alert",
                object_id=alert.id,
                details={
                    "rule": rule.name,
                    "type": rule.type,
                    "severity": rule.severity,
                    "device_id": str(device.id),
                    "device_name": device.name,
                    "message": finding.message,
                    **finding.details,
                },
            )
            await _publish(redis, "alert_raised", alert, device)

        for key, alert in by_key.items():
            if key in found_keys:
                continue
            alert.state = "resolved"
            alert.resolved_at = now
            resolved += 1
            await record_audit(
                session,
                action=AuditAction.ALERT_RESOLVED,
                actor_label="monitoring",
                object_type="alert",
                object_id=alert.id,
                details={
                    "rule": rule.name,
                    "type": rule.type,
                    "device_id": str(device.id),
                    "auto": True,
                },
            )
            await _publish(redis, "alert_resolved", alert, device)

    return raised, resolved


async def _in_cooldown(
    session: AsyncSession, rule: AlertRule, device: Device, dedup_key: str, now
) -> bool:
    if rule.cooldown_seconds <= 0:
        return False
    last = (
        await session.execute(
            sa.select(Alert)
            .where(
                Alert.device_id == device.id,
                Alert.rule_id == rule.id,
                Alert.dedup_key == dedup_key,
                Alert.state == "resolved",
            )
            .order_by(Alert.resolved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None or last.resolved_at is None:
        return False
    resolved_at = as_utc(last.resolved_at)
    return (now - resolved_at).total_seconds() < rule.cooldown_seconds


async def evaluate_all(session: AsyncSession, *, redis: Redis | None = None) -> dict[str, int]:
    """Прохід по всіх активних пристроях — виконується фоновою задачею."""
    devices = (
        await session.execute(sa.select(Device).where(Device.deleted_at.is_(None)))
    ).scalars().all()

    rules = (
        await session.execute(sa.select(AlertRule).where(AlertRule.enabled.is_(True)))
    ).scalars().all()

    total_raised = total_resolved = 0
    for device in devices:
        device_rules = [
            rule for rule in rules if rule.device_id is None or rule.device_id == device.id
        ]
        raised, resolved = await evaluate_device(
            session, device, redis=redis, rules=device_rules
        )
        total_raised += raised
        total_resolved += resolved

    await session.commit()
    return {
        "devices": len(devices),
        "rules": len(rules),
        "raised": total_raised,
        "resolved": total_resolved,
    }


async def _publish(redis: Redis | None, event: str, alert: Alert, device: Device) -> None:
    if redis is None:
        return
    try:
        await publish(
            redis,
            ADMIN_CHANNEL,
            {
                "type": event,
                "alert": {
                    "id": str(alert.id),
                    "device_id": str(device.id),
                    "device_name": device.name,
                    "rule_type": alert.type,
                    "severity": alert.severity,
                    "state": alert.state,
                    "message": alert.message,
                    "first_seen_at": alert.first_seen_at,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 — доставка подій не критична
        logger.debug("Подію алерта не надіслано", extra={"error": str(exc)})


# --------------------------------------------------- дії адміністратора ---


async def acknowledge(
    session: AsyncSession, alert: Alert, *, user: User
) -> Alert:
    if alert.state == "firing":
        alert.state = "acknowledged"
        alert.acknowledged_at = utcnow()
        alert.acknowledged_by_id = user.id
    return alert


async def resolve_manually(session: AsyncSession, alert: Alert) -> Alert:
    alert.state = "resolved"
    alert.resolved_at = utcnow()
    return alert


async def active_alerts_for(
    session: AsyncSession, device_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[Alert]]:
    if not device_ids:
        return {}
    rows = (
        await session.execute(
            sa.select(Alert).where(
                Alert.device_id.in_(device_ids),
                Alert.state.in_(("firing", "acknowledged")),
            )
        )
    ).scalars().all()

    grouped: dict[uuid.UUID, list[Alert]] = {}
    for alert in rows:
        grouped.setdefault(alert.device_id, []).append(alert)
    return grouped
