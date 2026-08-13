"""Життєвий цикл команд пристрою.

pending → dispatched → acked → succeeded | failed
                                 └→ expired / cancelled

Доставка — через Redis pub/sub у WebSocket-зʼєднання агента; якщо пристрій
офлайн, команда лишається `pending` і віддається при підключенні
(`GET /device/commands/pending`).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.core.rbac import role_has_permission
from app.core.redis_client import ADMIN_CHANNEL, device_channel
from app.core.security import as_utc, utcnow
from app.db.models import Command, Device, User
from app.services import command_permissions, command_registry
from app.services.audit import RESULT_DENIED, AuditAction, json_safe, record_audit
from app.ws.manager import publish

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "dispatched", "acked")
DEFAULT_TTL_SECONDS = 3600

# Команди, яких не можна мати дві одночасно для одного пристрою й пакета.
# Перелік лишається для сумісності зі старими викликами `create_command`;
# нові команди беруть політику з реєстру (`CommandSpec.singleton_key`).
SINGLETON_TYPES = {"APP_INSTALL", "APP_UPDATE", "APP_UNINSTALL"}


async def create_command(
    session: AsyncSession,
    *,
    device: Device,
    command_type: str,
    payload: dict[str, Any] | None = None,
    user: User | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    idempotency_key: str | None = None,
) -> Command:
    if device.deleted_at is not None or device.status != "active":
        raise ValidationFailedError("Команду можна створити лише для активного пристрою")

    if idempotency_key:
        existing = (
            await session.execute(
                sa.select(Command).where(Command.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    if command_type in SINGLETON_TYPES:
        await _reject_conflicting(session, device, command_type, payload)

    command = Command(
        device_id=device.id,
        type=command_type,
        # payload зберігається в JSON-колонці, тож дати/UUID приводимо
        # до серіалізовних значень тут — один раз для всіх модулів.
        payload=json_safe(payload) if payload else payload,
        status="pending",
        created_by_id=user.id if user else None,
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        idempotency_key=idempotency_key,
    )
    session.add(command)
    await session.flush()
    return command


async def _reject_conflicting(
    session: AsyncSession, device: Device, command_type: str, payload: dict[str, Any] | None
) -> None:
    package = (payload or {}).get("package_name")
    if not package:
        return
    rows = (
        await session.execute(
            sa.select(Command).where(
                Command.device_id == device.id,
                Command.type.in_(tuple(SINGLETON_TYPES)),
                Command.status.in_(ACTIVE_STATUSES),
            )
        )
    ).scalars().all()
    for row in rows:
        if (row.payload or {}).get("package_name") == package:
            raise ConflictError(
                f"Для пакета {package} вже виконується операція (команда {row.id})"
            )


async def dispatch(redis: Redis | None, command: Command) -> None:
    """Надсилає команду в канал пристрою (якщо він онлайн)."""
    if redis is None:
        return
    try:
        await publish(
            redis,
            device_channel(str(command.device_id)),
            {
                "type": "command",
                "command": {
                    "id": str(command.id),
                    "type": command.type,
                    "payload": command.payload,
                    "created_at": command.created_at,
                    "expires_at": command.expires_at,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 — офлайн-доставка через pending
        logger.warning(
            "Не вдалося опублікувати команду",
            extra={"command_id": str(command.id), "error": str(exc)},
        )


async def notify_admins(redis: Redis | None, event: str, command: Command) -> None:
    if redis is None:
        return
    try:
        await publish(
            redis,
            ADMIN_CHANNEL,
            {
                "type": event,
                "command_id": str(command.id),
                "device_id": str(command.device_id),
                "command_type": command.type,
                "status": command.status,
                "progress_percent": command.progress_percent,
                "status_message": command.status_message,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Подію для панелі не надіслано", extra={"error": str(exc)})


async def pending_for_device(session: AsyncSession, device: Device) -> list[Command]:
    """Команди, які пристрій ще має виконати; прострочені позначаються."""
    rows = (
        await session.execute(
            sa.select(Command)
            .where(
                Command.device_id == device.id,
                Command.status.in_(("pending", "dispatched")),
            )
            .order_by(Command.created_at)
        )
    ).scalars().all()

    now = utcnow()
    fresh: list[Command] = []
    for command in rows:
        expires_at = as_utc(command.expires_at)
        if expires_at is not None and expires_at <= now:
            command.status = "expired"
            command.completed_at = now
            command.error = "Пристрій не забрав команду до завершення терміну"
            measure(command)
            continue
        command.status = "dispatched"
        command.dispatched_at = command.dispatched_at or now
        command.attempts += 1
        fresh.append(command)
    return fresh


async def _get_device_command(
    session: AsyncSession, device: Device, command_id: uuid.UUID
) -> Command:
    command = await session.get(Command, command_id)
    if command is None or command.device_id != device.id:
        raise NotFoundError("Команду не знайдено")
    return command


async def acknowledge(
    session: AsyncSession, device: Device, command_id: uuid.UUID
) -> Command:
    command = await _get_device_command(session, device, command_id)
    if command.status in ("succeeded", "failed", "cancelled"):
        raise ConflictError("Команда вже завершена")
    command.status = "acked"
    command.acked_at = utcnow()
    return command


async def update_progress(
    session: AsyncSession,
    device: Device,
    command_id: uuid.UUID,
    *,
    percent: int,
    message: str | None,
) -> Command:
    command = await _get_device_command(session, device, command_id)
    if command.status in ("succeeded", "failed", "cancelled", "expired"):
        raise ConflictError("Команда вже завершена")
    if command.status != "acked":
        command.status = "acked"
        command.acked_at = command.acked_at or utcnow()
    command.progress_percent = percent
    command.status_message = message
    return command


async def complete(
    session: AsyncSession,
    device: Device,
    command_id: uuid.UUID,
    *,
    status: str,
    result: dict[str, Any] | None,
    error: str | None,
) -> Command:
    command = await _get_device_command(session, device, command_id)
    if command.status in ("succeeded", "failed", "cancelled"):
        raise ConflictError("Команда вже завершена")

    command.status = status
    command.result = result
    command.error = error
    command.completed_at = utcnow()
    command.progress_percent = 100 if status == "succeeded" else command.progress_percent
    measure(command)
    return command


async def cancel(
    session: AsyncSession, command_id: uuid.UUID, *, reason: str = "cancelled_by_admin"
) -> Command:
    command = await session.get(Command, command_id)
    if command is None:
        raise NotFoundError("Команду не знайдено")
    if command.status not in ACTIVE_STATUSES:
        raise ConflictError("Скасувати можна лише незавершену команду")
    command.status = "cancelled"
    command.error = reason
    command.completed_at = utcnow()
    measure(command)
    return command


# ------------------------------------------------------------- реєстр ----
#
# Єдиний вхід для видачі команд. Тип має бути в реєстрі, параметри —
# відповідати його схемі, дозвіл — чинній матриці, а пристрій — вимогам
# команди. Довільної дії, яку агент виконав би «як є», тут немає.


async def issue(
    session: AsyncSession,
    *,
    device: Device,
    command_type: str,
    params: dict[str, Any] | None,
    user: User,
    ip: str | None = None,
    user_agent: str | None = None,
    extra_payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> Command:
    """Створює команду через реєстр із повною перевіркою.

    Порядок перевірок свідомий: спершу з'ясовуємо, чи така команда взагалі
    існує, потім — чи має право той, хто її видає, потім — чи здатен її
    виконати пристрій, і лише тоді розбираємо параметри. Кожна відмова
    потрапляє в аудит із причиною.
    """
    # Знятий з обліку чи призупинений пристрій команд не отримує: він або
    # більше не наш, або навмисно виведений з керування.
    if device.deleted_at is not None or device.status != "active":
        await _reject(
            session,
            user=user,
            device=device,
            command_type=command_type,
            reason="device_not_active",
            ip=ip,
            user_agent=user_agent,
            details={"device_status": device.status},
        )
        raise ConflictError(
            f"Команду можна створити лише для активного пристрою "
            f"(поточний статус — {device.status})"
        )

    spec = command_registry.get_spec(command_type)
    if spec is None:
        await _reject(
            session,
            user=user,
            device=device,
            command_type=command_type,
            reason="unknown_command",
            ip=ip,
            user_agent=user_agent,
        )
        raise NotFoundError(f"Команди {command_type} немає в реєстрі")

    rule = await command_permissions.effective(session, command_type)
    if rule is None or not rule.enabled:
        await _reject(
            session,
            user=user,
            device=device,
            command_type=command_type,
            reason="command_disabled",
            ip=ip,
            user_agent=user_agent,
        )
        raise PermissionDeniedError(f"Команду {spec.title} вимкнено адміністратором")

    if not role_has_permission(user.role, rule.required_permission):
        await _reject(
            session,
            user=user,
            device=device,
            command_type=command_type,
            reason="permission_denied",
            ip=ip,
            user_agent=user_agent,
            details={"required": rule.required_permission.value, "role": user.role},
        )
        raise PermissionDeniedError(
            f"Недостатньо прав: потрібен дозвіл {rule.required_permission.value}"
        )

    # Команда, що спирається на DevicePolicyManager, без Device Owner просто
    # не виконається — краще відмовити одразу й зрозуміло, ніж лишити
    # адміністратора з «failed» через п'ять хвилин.
    if spec.requires_device_owner and device.management_mode != "device_owner":
        await _reject(
            session,
            user=user,
            device=device,
            command_type=command_type,
            reason="device_owner_required",
            ip=ip,
            user_agent=user_agent,
            details={"management_mode": device.management_mode},
        )
        raise ConflictError(
            f"«{spec.title}» потребує режиму Device Owner; "
            f"поточний режим пристрою — {device.management_mode}"
        )

    payload = validate_params(spec, params)
    if extra_payload:
        payload.update(extra_payload)

    await _ensure_no_conflict(session, device, spec, payload)

    command = Command(
        device_id=device.id,
        type=spec.type,
        payload=json_safe(payload) if payload else None,
        status="pending",
        created_by_id=user.id,
        expires_at=utcnow() + timedelta(seconds=spec.ttl_seconds),
        idempotency_key=idempotency_key,
    )
    session.add(command)
    await session.flush()

    # Дві події навмисно: COMMAND_ISSUED — однорідний слід для всіх команд,
    # spec.audit_action — звичний доменний запис (APP_INSTALL, DEVICE_REBOOT…).
    # Ціль команди (наприклад, package_name) виноситься на верхній рівень:
    # так її видно у стрічці аудиту без розгортання вкладеного об'єкта.
    # Повний набір параметрів лишається поруч — секрети в ньому маскує
    # `record_audit`.
    common_details = {
        "command_id": str(command.id),
        "command_type": spec.type,
        "device_id": str(device.id),
        "disruptive": spec.disruptive,
        "requires_device_owner": spec.requires_device_owner,
        **{key: payload.get(key) for key in spec.target_fields},
        "params": payload or None,
    }
    await record_audit(
        session,
        action=AuditAction.COMMAND_ISSUED,
        user=user,
        object_type="command",
        object_id=command.id,
        ip=ip,
        user_agent=user_agent,
        details=common_details,
    )
    await record_audit(
        session,
        action=spec.audit_action,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={**common_details, "phase": "requested"},
    )
    return command


def validate_params(
    spec: command_registry.CommandSpec, params: dict[str, Any] | None
) -> dict[str, Any]:
    """Перевіряє параметри за схемою команди.

    Зайві поля відхиляються (`extra="forbid"`), а не ігноруються: мовчазно
    проігнорований параметр — це адміністратор, який вважає, що застосував
    налаштування, хоча воно нікуди не поїхало.
    """
    try:
        model = spec.params_model.model_validate(params or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValidationFailedError(
            f"Некоректні параметри команди {spec.type}: {problems}"
        ) from exc
    return model.model_dump()


async def _ensure_no_conflict(
    session: AsyncSession,
    device: Device,
    spec: command_registry.CommandSpec,
    payload: dict[str, Any],
) -> None:
    """Не даємо накопичити дублі однієї й тієї ж дії."""
    if spec.singleton_key is None:
        return

    rows = (
        await session.execute(
            sa.select(Command).where(
                Command.device_id == device.id,
                Command.status.in_(ACTIVE_STATUSES),
            )
        )
    ).scalars().all()

    if spec.singleton_key == "device":
        clash = next((row for row in rows if row.type == spec.type), None)
        if clash is not None:
            raise ConflictError(
                f"«{spec.title}» уже виконується для цього пристрою (команда {clash.id})"
            )
        return

    # singleton_key == "package": одна активна операція на пакет, незалежно
    # від того, це встановлення, оновлення, видалення чи зупинка.
    package = payload.get("package_name")
    if not package:
        return
    for row in rows:
        row_spec = command_registry.get_spec(row.type)
        if row_spec is None or row_spec.singleton_key != "package":
            continue
        if (row.payload or {}).get("package_name") == package:
            raise ConflictError(
                f"Для пакета {package} вже виконується операція (команда {row.id})"
            )


async def _reject(
    session: AsyncSession,
    *,
    user: User,
    device: Device,
    command_type: str,
    reason: str,
    ip: str | None,
    user_agent: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    """Пише відмову у видачі команди в аудит."""
    await record_audit(
        session,
        action=AuditAction.COMMAND_REJECTED,
        result=RESULT_DENIED,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"command_type": command_type, "reason": reason, **(details or {})},
    )
    await session.commit()


def measure(command: Command) -> None:
    """Рахує, скільки команда чекала й скільки виконувалася.

    Обидві величини рахуються з серверних міток: годинник пристрою може
    розходитися з серверним, і різниця «за даними пристрою» була б
    непорівнянною між пристроями.
    """
    created = as_utc(command.created_at)
    acked = as_utc(command.acked_at)
    completed = as_utc(command.completed_at)

    if created is not None and acked is not None:
        command.queue_ms = max(0, int((acked - created).total_seconds() * 1000))
    if completed is not None:
        start = acked or as_utc(command.dispatched_at) or created
        if start is not None:
            command.duration_ms = max(0, int((completed - start).total_seconds() * 1000))
