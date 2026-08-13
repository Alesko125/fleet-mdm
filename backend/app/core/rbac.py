"""Ролі та дозволи — єдине місце визначення матриці доступу."""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    OPERATOR = "operator"
    AUDITOR = "auditor"


class Permission(str, Enum):
    # користувачі та система
    USER_MANAGE = "user:manage"
    SETTINGS_MANAGE = "settings:manage"
    # Ротація серверних секретів і перегляд їхнього стану.
    SECRET_ROTATE = "secret:rotate"  # noqa: S105 — це назва дозволу, не секрет

    # enrollment та пристрої
    ENROLLMENT_MANAGE = "enrollment:manage"
    DEVICE_READ = "device:read"
    DEVICE_WRITE = "device:write"
    DEVICE_DELETE = "device:delete"
    DEVICE_ACTION_SAFE = "device:action:safe"      # refresh telemetry, sync config, launch
    DEVICE_ACTION_CRITICAL = "device:action:critical"  # reboot, kiosk, політики
    # Примусова ротація ключа пристрою та відкликання його токенів.
    DEVICE_CREDENTIAL_ROTATE = "device:credential:rotate"

    # застосунки
    APP_READ = "app:read"
    APP_MANAGE = "app:manage"

    # файли
    FILE_READ = "file:read"
    FILE_WRITE = "file:write"

    # геолокація
    LOCATION_READ = "location:read"
    LOCATION_MANAGE = "location:manage"

    # віддалена підтримка
    SUPPORT_SESSION = "support:session"

    # моніторинг
    ALERT_MANAGE = "alert:manage"

    # комунікаційний модуль (SMS / журнал дзвінків) — чутливі дані
    COMMS_MANAGE = "comms:manage"   # увімкнути/вимкнути/налаштувати модуль
    COMMS_READ = "comms:read"       # переглядати зібрані дані

    # аудит
    AUDIT_READ = "audit:read"


_AUDITOR: frozenset[Permission] = frozenset(
    {
        Permission.DEVICE_READ,
        Permission.APP_READ,
        Permission.LOCATION_READ,
        Permission.AUDIT_READ,
    }
)

_OPERATOR: frozenset[Permission] = _AUDITOR | frozenset(
    {
        Permission.DEVICE_ACTION_SAFE,
        Permission.FILE_READ,
        Permission.SUPPORT_SESSION,
    }
)

_ADMIN: frozenset[Permission] = _OPERATOR | frozenset(
    {
        Permission.ENROLLMENT_MANAGE,
        Permission.DEVICE_WRITE,
        Permission.DEVICE_DELETE,
        Permission.DEVICE_ACTION_CRITICAL,
        Permission.APP_MANAGE,
        Permission.FILE_WRITE,
        Permission.LOCATION_MANAGE,
        Permission.ALERT_MANAGE,
        Permission.DEVICE_CREDENTIAL_ROTATE,
        Permission.COMMS_MANAGE,
        Permission.COMMS_READ,
    }
)

_SUPERADMIN: frozenset[Permission] = _ADMIN | frozenset(
    {
        Permission.USER_MANAGE,
        Permission.SETTINGS_MANAGE,
        Permission.SECRET_ROTATE,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.AUDITOR: _AUDITOR,
    Role.OPERATOR: _OPERATOR,
    Role.ADMIN: _ADMIN,
    Role.SUPERADMIN: _SUPERADMIN,
}


def permissions_for(role: Role | str) -> frozenset[Permission]:
    try:
        return ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return frozenset()


def role_has_permission(role: Role | str, permission: Permission) -> bool:
    return permission in permissions_for(role)


def permission_names(role: Role | str) -> list[str]:
    """Відсортований перелік дозволів ролі — віддається клієнту в /auth/me."""
    return sorted(p.value for p in permissions_for(role))


def all_roles() -> list[str]:
    return [role.value for role in Role]
