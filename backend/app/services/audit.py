"""Сервіс аудиту. Запис створюється в тій самій транзакції, що й сама дія."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import mask_sensitive
from app.db.models import AuditLog, Device, User

logger = logging.getLogger("audit")


class AuditAction:
    """Перелік дій, що фіксуються (див. docs/SECURITY.md §8)."""

    # автентифікація
    LOGIN = "AUTH_LOGIN"
    LOGIN_FAILED = "AUTH_LOGIN_FAILED"
    LOGOUT = "AUTH_LOGOUT"
    REFRESH = "AUTH_REFRESH"
    REFRESH_REUSE = "AUTH_REFRESH_REUSE"
    PASSWORD_CHANGED = "AUTH_PASSWORD_CHANGED"
    PASSWORD_CHANGE_FAILED = "AUTH_PASSWORD_CHANGE_FAILED"
    PASSWORD_RESET = "AUTH_PASSWORD_RESET"
    SESSIONS_REVOKED = "AUTH_SESSIONS_REVOKED"
    CSRF_REJECTED = "AUTH_CSRF_REJECTED"

    # користувачі
    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"
    USER_DELETED = "USER_DELETED"

    # enrollment / пристрої
    ENROLLMENT_TOKEN_CREATED = "ENROLLMENT_TOKEN_CREATED"
    ENROLLMENT_TOKEN_REVOKED = "ENROLLMENT_TOKEN_REVOKED"
    DEVICE_ENROLLED = "DEVICE_ENROLLED"
    DEVICE_ENROLL_FAILED = "DEVICE_ENROLL_FAILED"
    DEVICE_AUTH = "DEVICE_AUTH"
    DEVICE_AUTH_FAILED = "DEVICE_AUTH_FAILED"
    DEVICE_UPDATED = "DEVICE_UPDATED"
    DEVICE_DELETED = "DEVICE_DELETED"
    DEVICE_KEY_ROTATED = "DEVICE_KEY_ROTATED"
    DEVICE_KEY_ROTATION_FAILED = "DEVICE_KEY_ROTATION_FAILED"
    DEVICE_KEY_ROTATION_REQUIRED = "DEVICE_KEY_ROTATION_REQUIRED"
    DEVICE_TOKENS_REVOKED = "DEVICE_TOKENS_REVOKED"

    # адміністрування
    DEVICE_REBOOT = "DEVICE_REBOOT"
    DEVICE_SYNC_CONFIG = "DEVICE_SYNC_CONFIG"
    DEVICE_REFRESH_TELEMETRY = "DEVICE_REFRESH_TELEMETRY"
    AGENT_RESTART = "AGENT_RESTART"
    DIAGNOSTICS_COLLECTED = "DIAGNOSTICS_COLLECTED"
    POLICY_APPLIED = "POLICY_APPLIED"

    # застосунки
    APK_UPLOADED = "APK_UPLOADED"
    APK_DOWNLOADED = "APK_DOWNLOADED"
    APK_DELETED = "APK_DELETED"
    APK_UPDATED = "APK_UPDATED"
    APP_INSTALL = "APP_INSTALL"
    APP_UPDATE = "APP_UPDATE"
    APP_UNINSTALL = "APP_UNINSTALL"
    APP_LAUNCH = "APP_LAUNCH"
    APP_STOP = "APP_STOP"
    APP_LIST_REQUESTED = "APP_LIST_REQUESTED"
    APP_INVENTORY_REPORTED = "APP_INVENTORY_REPORTED"
    COMMAND_COMPLETED = "COMMAND_COMPLETED"
    COMMAND_CANCELLED = "COMMAND_CANCELLED"
    COMMAND_ISSUED = "COMMAND_ISSUED"
    COMMAND_REJECTED = "COMMAND_REJECTED"
    COMMAND_PERMISSION_CHANGED = "COMMAND_PERMISSION_CHANGED"

    # файли
    FILE_LIST = "FILE_LIST"
    FILE_UPLOAD = "FILE_UPLOAD"
    FILE_DOWNLOAD = "FILE_DOWNLOAD"
    FILE_RENAME = "FILE_RENAME"
    FILE_DELETE = "FILE_DELETE"
    FILE_MKDIR = "FILE_MKDIR"
    FILE_ROOT_CHANGED = "FILE_ROOT_CHANGED"
    FILE_ACCESS_DENIED = "FILE_ACCESS_DENIED"
    FILE_TRANSFER_COMPLETED = "FILE_TRANSFER_COMPLETED"

    # геолокація
    LOCATION_CONFIG_CHANGED = "LOCATION_CONFIG_CHANGED"
    LOCATION_DISABLED = "LOCATION_DISABLED"
    LOCATION_PERMISSION_CHANGED = "LOCATION_PERMISSION_CHANGED"
    LOCATION_UPLOAD_REJECTED = "LOCATION_UPLOAD_REJECTED"
    LOCATION_REQUESTED = "LOCATION_REQUESTED"
    LOCATION_HISTORY_VIEWED = "LOCATION_HISTORY_VIEWED"
    LOCATION_HISTORY_PURGED = "LOCATION_HISTORY_PURGED"
    RETENTION_RUN = "RETENTION_RUN"

    # віддалена підтримка
    REMOTE_SESSION_REQUESTED = "REMOTE_SESSION_REQUESTED"
    REMOTE_SESSION_CONSENT = "REMOTE_SESSION_CONSENT"
    REMOTE_SESSION_STARTED = "REMOTE_SESSION_STARTED"
    REMOTE_SESSION_ENDED = "REMOTE_SESSION_ENDED"
    REMOTE_SESSION_VIEWER_JOINED = "REMOTE_SESSION_VIEWER_JOINED"
    REMOTE_SESSION_VIEWER_LEFT = "REMOTE_SESSION_VIEWER_LEFT"
    REMOTE_SESSION_INPUT = "REMOTE_SESSION_INPUT"

    # моніторинг і алерти
    ALERT_RULE_CREATED = "ALERT_RULE_CREATED"
    ALERT_RULE_UPDATED = "ALERT_RULE_UPDATED"
    ALERT_RULE_DELETED = "ALERT_RULE_DELETED"
    ALERT_RAISED = "ALERT_RAISED"
    ALERT_RESOLVED = "ALERT_RESOLVED"
    ALERT_ACKNOWLEDGED = "ALERT_ACKNOWLEDGED"

    # комунікаційний модуль
    COMMS_ENABLED = "COMMS_ENABLED"
    COMMS_DISABLED = "COMMS_DISABLED"
    COMMS_CONFIG_CHANGED = "COMMS_CONFIG_CHANGED"
    COMMS_PERMISSION_CHANGED = "COMMS_PERMISSION_CHANGED"
    COMMS_DATA_INGESTED = "COMMS_DATA_INGESTED"
    COMMS_DATA_VIEWED = "COMMS_DATA_VIEWED"
    COMMS_DATA_PURGED = "COMMS_DATA_PURGED"

    # доступ
    ACCESS_DENIED = "ACCESS_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    REQUEST_REJECTED = "REQUEST_REJECTED"

    # секрети
    SECRET_ROTATION_VIEWED = "SECRET_ROTATION_VIEWED"
    SECRET_ROTATION_STAGED = "SECRET_ROTATION_STAGED"


def json_safe(value: Any, _depth: int = 0) -> Any:
    """Готує довільне значення до збереження в JSON/JSONB-колонці."""
    if _depth > 8:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return json_safe(value.value, _depth + 1)
    if isinstance(value, dict):
        return {str(k): json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v, _depth + 1) for v in value]
    return str(value)


RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"
RESULT_DENIED = "denied"


async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    result: str = RESULT_SUCCESS,
    user: User | None = None,
    device: Device | None = None,
    actor_label: str | None = None,
    object_type: str | None = None,
    object_id: str | uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Додає запис в audit_log. Коміт — за викликачем."""
    if user is not None:
        actor_type = "user"
        label = actor_label or user.email
    elif device is not None:
        actor_type = "device"
        label = actor_label or device.name
    else:
        actor_type = "system"
        label = actor_label

    entry = AuditLog(
        action=action,
        result=result,
        actor_type=actor_type,
        actor_user_id=user.id if user else None,
        actor_device_id=device.id if device else None,
        actor_label=label,
        object_type=object_type,
        object_id=str(object_id) if object_id is not None else None,
        ip=ip,
        user_agent=(user_agent or "")[:400] or None,
        details=json_safe(mask_sensitive(details)) if details else None,
    )
    session.add(entry)
    logger.info(
        "audit",
        extra={
            "action": action,
            "result": result,
            "actor_type": actor_type,
            "actor": label,
            "object_type": object_type,
            "object_id": str(object_id) if object_id is not None else None,
        },
    )
    return entry
