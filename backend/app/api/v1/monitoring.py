"""Моніторинг: правила, сповіщення та зведення для панелі."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.rbac import Permission
from app.core.redis_client import get_redis
from app.core.security import as_utc, utcnow
from app.db.models import Alert, AlertRule, Device, DeviceTelemetry, LocationSettings, User
from app.db.session import get_session
from app.schemas.alert import (
    ALLOWED_THRESHOLDS,
    AlertOut,
    AlertRuleCreate,
    AlertRuleOut,
    AlertRuleUpdate,
    AlertWithDevice,
    EvaluationResult,
    ThresholdSchema,
    validate_thresholds,
)
from app.schemas.common import MessageResponse, Page
from app.schemas.telemetry import DeviceHealth, MonitoringSummary
from app.services import alerts as alert_service
from app.services.audit import AuditAction, record_audit
from app.services.telemetry import free_percent

router = APIRouter(tags=["monitoring"])


# ------------------------------------------------------------- правила ----


@router.get("/alert-rules", response_model=list[AlertRuleOut])
async def list_rules(
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[AlertRuleOut]:
    rows = (
        await session.execute(sa.select(AlertRule).order_by(AlertRule.name))
    ).scalars().all()
    return [AlertRuleOut.model_validate(row) for row in rows]


@router.get("/alert-rules/schema", response_model=list[ThresholdSchema])
async def rule_schema(
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
) -> list[ThresholdSchema]:
    """Довідник порогів для форми в панелі."""
    return [
        ThresholdSchema(
            type=rule_type,  # type: ignore[arg-type]
            keys=keys,  # type: ignore[arg-type]
            defaults=alert_service.DEFAULT_THRESHOLDS.get(rule_type, {}),
        )
        for rule_type, keys in ALLOWED_THRESHOLDS.items()
    ]


@router.post("/alert-rules", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(
    request: Request,
    payload: AlertRuleCreate,
    user: User = Depends(require_permission(Permission.ALERT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> AlertRuleOut:
    ip, user_agent = request_context(request)

    if payload.device_id is not None:
        device = await session.get(Device, payload.device_id)
        if device is None or device.deleted_at is not None:
            raise NotFoundError("Пристрій не знайдено")

    rule = AlertRule(
        name=payload.name,
        type=payload.type,
        severity=payload.severity,
        enabled=payload.enabled,
        thresholds=payload.thresholds,
        device_id=payload.device_id,
        cooldown_seconds=payload.cooldown_seconds,
        description=payload.description,
        created_by_id=user.id,
    )
    session.add(rule)
    await session.flush()

    await record_audit(
        session,
        action=AuditAction.ALERT_RULE_CREATED,
        user=user,
        object_type="alert_rule",
        object_id=rule.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "name": rule.name,
            "type": rule.type,
            "severity": rule.severity,
            "thresholds": rule.thresholds,
            "device_id": str(rule.device_id) if rule.device_id else None,
            "cooldown_seconds": rule.cooldown_seconds,
        },
    )
    await session.commit()
    return AlertRuleOut.model_validate(rule)


@router.patch("/alert-rules/{rule_id}", response_model=AlertRuleOut)
async def update_rule(
    request: Request,
    rule_id: uuid.UUID,
    payload: AlertRuleUpdate,
    user: User = Depends(require_permission(Permission.ALERT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> AlertRuleOut:
    ip, user_agent = request_context(request)
    rule = await session.get(AlertRule, rule_id)
    if rule is None:
        raise NotFoundError("Правило не знайдено")

    changes: dict[str, dict[str, object]] = {}

    def apply(field: str, value: object) -> None:
        old = getattr(rule, field)
        if value is None or old == value:
            return
        changes[field] = {"from": old, "to": value}
        setattr(rule, field, value)

    apply("name", payload.name)
    apply("severity", payload.severity)
    apply("cooldown_seconds", payload.cooldown_seconds)
    apply("description", payload.description)
    if payload.enabled is not None:
        apply("enabled", payload.enabled)
    if payload.thresholds is not None:
        try:
            cleaned = validate_thresholds(rule.type, payload.thresholds)
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc
        apply("thresholds", cleaned)

    await record_audit(
        session,
        action=AuditAction.ALERT_RULE_UPDATED,
        user=user,
        object_type="alert_rule",
        object_id=rule.id,
        ip=ip,
        user_agent=user_agent,
        details={"name": rule.name, "type": rule.type, "changes": changes or "без змін"},
    )
    await session.commit()
    return AlertRuleOut.model_validate(rule)


@router.delete("/alert-rules/{rule_id}", response_model=MessageResponse)
async def delete_rule(
    request: Request,
    rule_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.ALERT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    ip, user_agent = request_context(request)
    rule = await session.get(AlertRule, rule_id)
    if rule is None:
        raise NotFoundError("Правило не знайдено")

    details = {"name": rule.name, "type": rule.type, "thresholds": rule.thresholds}
    await session.delete(rule)
    await record_audit(
        session,
        action=AuditAction.ALERT_RULE_DELETED,
        user=user,
        object_type="alert_rule",
        object_id=rule_id,
        ip=ip,
        user_agent=user_agent,
        details=details,
    )
    await session.commit()
    return MessageResponse(message="Правило видалено")


# ------------------------------------------------------------ сповіщення --


@router.get("/alerts", response_model=Page[AlertWithDevice])
async def list_alerts(
    state: str | None = Query(default=None, pattern=r"^(firing|acknowledged|resolved)$"),
    severity: str | None = Query(default=None, pattern=r"^(info|warning|critical)$"),
    alert_type: str | None = Query(default=None, alias="type", max_length=48),
    device_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> Page[AlertWithDevice]:
    conditions = []
    if state:
        conditions.append(Alert.state == state)
    if severity:
        conditions.append(Alert.severity == severity)
    if alert_type:
        conditions.append(Alert.type == alert_type)
    if device_id:
        conditions.append(Alert.device_id == device_id)
    if since:
        conditions.append(Alert.first_seen_at >= since)

    total = (
        await session.execute(sa.select(sa.func.count()).select_from(Alert).where(*conditions))
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(Alert, Device.name)
            .join(Device, Device.id == Alert.device_id)
            .where(*conditions)
            .order_by(Alert.first_seen_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = []
    for alert, device_name in rows:
        item = AlertWithDevice.model_validate(alert)
        item.device_name = device_name
        items.append(item)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge_alert(
    request: Request,
    alert_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.DEVICE_ACTION_SAFE)),
    session: AsyncSession = Depends(get_session),
) -> AlertOut:
    ip, user_agent = request_context(request)
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise NotFoundError("Сповіщення не знайдено")

    await alert_service.acknowledge(session, alert, user=user)
    await record_audit(
        session,
        action=AuditAction.ALERT_ACKNOWLEDGED,
        user=user,
        object_type="alert",
        object_id=alert.id,
        ip=ip,
        user_agent=user_agent,
        details={"type": alert.type, "device_id": str(alert.device_id), "message": alert.message},
    )
    await session.commit()
    return AlertOut.model_validate(alert)


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
async def resolve_alert(
    request: Request,
    alert_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.DEVICE_ACTION_SAFE)),
    session: AsyncSession = Depends(get_session),
) -> AlertOut:
    """Ручне закриття. Якщо причина лишилась, наступна оцінка відкриє знову."""
    ip, user_agent = request_context(request)
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise NotFoundError("Сповіщення не знайдено")

    await alert_service.resolve_manually(session, alert)
    await record_audit(
        session,
        action=AuditAction.ALERT_RESOLVED,
        user=user,
        object_type="alert",
        object_id=alert.id,
        ip=ip,
        user_agent=user_agent,
        details={"type": alert.type, "device_id": str(alert.device_id), "auto": False},
    )
    await session.commit()
    return AlertOut.model_validate(alert)


# -------------------------------------------------------------- зведення --


@router.get("/monitoring/summary", response_model=MonitoringSummary)
async def monitoring_summary(
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> MonitoringSummary:
    settings = get_settings()
    cutoff = utcnow() - timedelta(seconds=settings.device_offline_after_seconds)

    devices = (
        await session.execute(sa.select(Device).where(Device.deleted_at.is_(None)))
    ).scalars().all()
    online = sum(
        1
        for device in devices
        if device.last_seen_at is not None and as_utc(device.last_seen_at) > cutoff
    )

    firing = (
        await session.execute(
            sa.select(Alert).where(Alert.state.in_(("firing", "acknowledged")))
        )
    ).scalars().all()

    by_severity: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for alert in firing:
        by_severity[alert.severity] = by_severity.get(alert.severity, 0) + 1
        by_type[alert.type] = by_type.get(alert.type, 0) + 1

    rules_enabled = (
        await session.execute(
            sa.select(sa.func.count()).select_from(AlertRule).where(AlertRule.enabled.is_(True))
        )
    ).scalar_one()

    return MonitoringSummary(
        devices_total=len(devices),
        devices_online=online,
        devices_offline=len(devices) - online,
        alerts_firing=len(firing),
        alerts_by_severity=by_severity,
        alerts_by_type=by_type,
        rules_enabled=rules_enabled,
    )


SEVERITY_ORDER = {"critical": 3, "warning": 2, "info": 1}


@router.get("/monitoring/devices", response_model=list[DeviceHealth])
async def device_health(
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[DeviceHealth]:
    """Показники для індикаторів у списку пристроїв."""
    settings = get_settings()
    cutoff = utcnow() - timedelta(seconds=settings.device_offline_after_seconds)

    devices = (
        await session.execute(
            sa.select(Device).where(Device.deleted_at.is_(None)).order_by(Device.name)
        )
    ).scalars().all()
    if not devices:
        return []

    device_ids = [device.id for device in devices]
    telemetry = {
        row.device_id: row
        for row in (
            await session.execute(
                sa.select(DeviceTelemetry).where(DeviceTelemetry.device_id.in_(device_ids))
            )
        ).scalars().all()
    }
    location = {
        row.device_id: row
        for row in (
            await session.execute(
                sa.select(LocationSettings).where(LocationSettings.device_id.in_(device_ids))
            )
        ).scalars().all()
    }
    grouped = await alert_service.active_alerts_for(session, device_ids)

    result: list[DeviceHealth] = []
    for device in devices:
        snapshot = telemetry.get(device.id)
        settings_row = location.get(device.id)
        device_alerts = grouped.get(device.id, [])
        worst = max(
            (alert.severity for alert in device_alerts),
            key=lambda value: SEVERITY_ORDER.get(value, 0),
            default=None,
        )

        if settings_row is None or not settings_row.enabled:
            reporting = "disabled"
        elif settings_row.permission_state in ("granted_fine", "granted_coarse"):
            reporting = "active"
        else:
            reporting = "blocked"

        last_seen = as_utc(device.last_seen_at)
        result.append(
            DeviceHealth(
                device_id=str(device.id),
                online=last_seen is not None and last_seen > cutoff,
                battery_level=snapshot.battery_level if snapshot else None,
                charging=snapshot.charging if snapshot else None,
                storage_free_percent=(
                    free_percent(snapshot.storage_free_bytes, snapshot.storage_total_bytes)
                    if snapshot
                    else None
                ),
                ram_free_percent=(
                    free_percent(snapshot.ram_available_bytes, snapshot.ram_total_bytes)
                    if snapshot
                    else None
                ),
                cpu_load_percent=snapshot.cpu_load_percent if snapshot else None,
                android_version=snapshot.android_version if snapshot else None,
                agent_version=device.agent_version,
                location_reporting=reporting,
                enrollment_status=device.status,
                active_alerts=len(device_alerts),
                worst_severity=worst,
                last_seen_at=device.last_seen_at,
                collected_at=snapshot.collected_at if snapshot else None,
            )
        )
    return result


@router.post("/monitoring/evaluate", response_model=EvaluationResult)
async def evaluate_now(
    request: Request,
    user: User = Depends(require_permission(Permission.ALERT_MANAGE)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> EvaluationResult:
    """Позачергова оцінка всіх правил (штатно виконується щохвилини)."""
    ip, user_agent = request_context(request)
    result = await alert_service.evaluate_all(session, redis=redis)
    await record_audit(
        session,
        action=AuditAction.ALERT_RULE_UPDATED,
        user=user,
        object_type="monitoring",
        ip=ip,
        user_agent=user_agent,
        details={"trigger": "manual", **result},
    )
    await session.commit()
    return EvaluationResult(**result)
