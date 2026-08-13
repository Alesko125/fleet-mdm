"""Конфігуровані дозволи на команди.

Реєстр (`command_registry`) задає значення за замовчуванням — розумну
матрицю «хто що може». Цей модуль дозволяє її **змінити без релізу**:
запис у `command_permissions` перекриває дефолт для конкретного типу.

Навіщо: різні парки живуть за різними правилами. Десь оператор має право
перезавантажити касовий термінал, десь встановлення застосунків лишають
виключно суперадміністратору, а десь окрему команду вимикають зовсім.
Зашивати це в код означало б релізити конфігурацію.

Чого тут **не** можна: додати новий тип команди. Перекрити можна лише
потрібний дозвіл і прапорець «увімкнено» для типу, який уже описаний у
реєстрі. Так конфігурація не перетворюється на спосіб протягнути
довільну дію повз перевірки.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.rbac import Permission, Role, permissions_for, role_has_permission
from app.db.models import CommandPermission, User
from app.services import command_registry
from app.services.audit import AuditAction, record_audit


@dataclass(frozen=True, slots=True)
class EffectivePermission:
    """Чинне правило для типу команди."""

    command_type: str
    required_permission: Permission
    enabled: bool
    # Правило взяте з БД, а не з реєстру.
    overridden: bool

    @property
    def roles(self) -> list[str]:
        """Ролі, яким цього дозволу вистачає."""
        if not self.enabled:
            return []
        return [
            role.value
            for role in Role
            if role_has_permission(role, self.required_permission)
        ]


async def _overrides(session: AsyncSession) -> dict[str, CommandPermission]:
    rows = (await session.execute(sa.select(CommandPermission))).scalars().all()
    return {row.command_type: row for row in rows}


async def effective(
    session: AsyncSession, command_type: str
) -> EffectivePermission | None:
    """Чинне правило для одного типу (None — типу немає в реєстрі)."""
    spec = command_registry.get_spec(command_type)
    if spec is None:
        return None

    row = (
        await session.execute(
            sa.select(CommandPermission).where(
                CommandPermission.command_type == command_type
            )
        )
    ).scalar_one_or_none()

    if row is None:
        return EffectivePermission(
            command_type=command_type,
            required_permission=spec.permission,
            enabled=True,
            overridden=False,
        )

    return EffectivePermission(
        command_type=command_type,
        required_permission=_parse_permission(row.required_permission, spec.permission),
        enabled=row.enabled,
        overridden=True,
    )


async def effective_all(session: AsyncSession) -> list[EffectivePermission]:
    """Уся матриця — для панелі та документації."""
    overrides = await _overrides(session)
    result: list[EffectivePermission] = []
    for spec in command_registry.specs():
        row = overrides.get(spec.type)
        if row is None:
            result.append(
                EffectivePermission(spec.type, spec.permission, True, False)
            )
        else:
            result.append(
                EffectivePermission(
                    spec.type,
                    _parse_permission(row.required_permission, spec.permission),
                    row.enabled,
                    True,
                )
            )
    return result


def _parse_permission(value: str, fallback: Permission) -> Permission:
    """Некоректне значення в БД не має відкривати доступ ширше за дефолт."""
    try:
        return Permission(value)
    except ValueError:
        return fallback


async def set_rule(
    session: AsyncSession,
    *,
    command_type: str,
    required_permission: str | None,
    enabled: bool | None,
    actor: User,
    ip: str | None,
    user_agent: str | None,
) -> EffectivePermission:
    """Змінює правило для типу команди."""
    spec = command_registry.get_spec(command_type)
    if spec is None:
        raise NotFoundError(f"Команди {command_type} немає в реєстрі")

    permission = spec.permission
    if required_permission is not None:
        try:
            permission = Permission(required_permission)
        except ValueError as exc:
            raise ValidationFailedError(
                f"Невідомий дозвіл {required_permission!r}"
            ) from exc

    row = (
        await session.execute(
            sa.select(CommandPermission).where(
                CommandPermission.command_type == command_type
            )
        )
    ).scalar_one_or_none()

    before = {
        "required_permission": (
            row.required_permission if row else spec.permission.value
        ),
        "enabled": row.enabled if row else True,
    }

    if row is None:
        row = CommandPermission(command_type=command_type)
        session.add(row)

    row.required_permission = permission.value
    if enabled is not None:
        row.enabled = enabled
    row.updated_by_id = actor.id

    await record_audit(
        session,
        action=AuditAction.COMMAND_PERMISSION_CHANGED,
        user=actor,
        object_type="command_permission",
        object_id=command_type,
        ip=ip,
        user_agent=user_agent,
        details={
            "command_type": command_type,
            "before": before,
            "after": {
                "required_permission": row.required_permission,
                "enabled": row.enabled,
            },
            "default_permission": spec.permission.value,
        },
    )
    await session.flush()
    return EffectivePermission(command_type, permission, row.enabled, True)


async def reset_rule(
    session: AsyncSession,
    *,
    command_type: str,
    actor: User,
    ip: str | None,
    user_agent: str | None,
) -> EffectivePermission:
    """Повертає тип до значення з реєстру."""
    spec = command_registry.get_spec(command_type)
    if spec is None:
        raise NotFoundError(f"Команди {command_type} немає в реєстрі")

    row = (
        await session.execute(
            sa.select(CommandPermission).where(
                CommandPermission.command_type == command_type
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)

    await record_audit(
        session,
        action=AuditAction.COMMAND_PERMISSION_CHANGED,
        user=actor,
        object_type="command_permission",
        object_id=command_type,
        ip=ip,
        user_agent=user_agent,
        details={"command_type": command_type, "reset_to_default": spec.permission.value},
    )
    return EffectivePermission(command_type, spec.permission, True, False)


async def allowed_types_for(session: AsyncSession, user: User) -> set[str]:
    """Типи команд, які цей користувач може видавати просто зараз."""
    granted = permissions_for(user.role)
    return {
        rule.command_type
        for rule in await effective_all(session)
        if rule.enabled and rule.required_permission in granted
    }


__all__ = [
    "EffectivePermission",
    "allowed_types_for",
    "effective",
    "effective_all",
    "reset_rule",
    "set_rule",
]
