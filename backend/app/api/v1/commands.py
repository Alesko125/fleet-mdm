"""Реєстр, видача та історія команд (адміністративна частина).

Це не віддалена оболонка: виконати можна лише те, що описано в
`app.services.command_registry`. Ендпоїнт видачі приймає **тип** із реєстру
й параметри за схемою цього типу — довільного рядка для виконання на
пристрої в API не існує.
"""

from __future__ import annotations

import uuid
from typing import get_args, get_origin

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi import status as http_status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.errors import NotFoundError
from app.core.rate_limit import rate_limit
from app.core.rbac import Permission, permissions_for
from app.core.redis_client import get_redis
from app.db.models import Command, Device, User
from app.db.session import get_session
from app.schemas.command import (
    CommandDetail,
    CommandIssueRequest,
    CommandOut,
    CommandParamSpec,
    CommandPermissionUpdate,
    CommandSpecOut,
    CommandStats,
)
from app.schemas.common import Page
from app.services import command_permissions, command_registry
from app.services import commands as command_service
from app.services import device as device_service
from app.services.audit import AuditAction, record_audit

router = APIRouter(tags=["commands"])


# --------------------------------------------------------------- реєстр ---


def _describe_params(spec: command_registry.CommandSpec) -> list[CommandParamSpec]:
    """Розкриває схему параметрів команди у зрозумілий панелі вигляд."""
    described: list[CommandParamSpec] = []
    for name, field_info in spec.params_model.model_fields.items():
        annotation = field_info.annotation
        origin = get_origin(annotation)
        if origin is not None:
            args = [a for a in get_args(annotation) if a is not type(None)]
            annotation = args[0] if args else annotation
        described.append(
            CommandParamSpec(
                name=name,
                type=getattr(annotation, "__name__", str(annotation)),
                required=field_info.is_required(),
                default=None if field_info.is_required() else field_info.get_default(),
                description=field_info.description,
            )
        )
    return described


@router.get("/commands/registry", response_model=list[CommandSpecOut])
async def command_registry_view(
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    db: AsyncSession = Depends(get_session),
) -> list[CommandSpecOut]:
    """Повний перелік команд, які взагалі існують у системі.

    Панель будує з цього форму: які параметри приймає команда, хто має право
    її видати, чи помітна вона користувачу пристрою й чи потребує Device Owner.
    """
    granted = permissions_for(user.role)
    rules = {rule.command_type: rule for rule in await command_permissions.effective_all(db)}

    result: list[CommandSpecOut] = []
    for spec in command_registry.specs():
        rule = rules[spec.type]
        result.append(
            CommandSpecOut(
                type=spec.type,
                title=spec.title,
                description=spec.description,
                user_visible=spec.user_visible,
                required_permission=rule.required_permission.value,
                enabled=rule.enabled,
                overridden=rule.overridden,
                roles=rule.roles,
                allowed_for_me=rule.enabled and rule.required_permission in granted,
                disruptive=spec.disruptive,
                requires_device_owner=spec.requires_device_owner,
                ttl_seconds=spec.ttl_seconds,
                params=_describe_params(spec),
            )
        )
    return result


@router.patch(
    "/commands/registry/{command_type}/permission", response_model=CommandSpecOut
)
async def update_command_permission(
    request: Request,
    command_type: str,
    payload: CommandPermissionUpdate,
    actor: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_session),
) -> CommandSpecOut:
    """Змінює, кому дозволено видавати команду цього типу.

    Змінити можна лише потрібний дозвіл і прапорець «увімкнено». Додати
    новий тип команди через конфігурацію неможливо — інакше налаштування
    стало б способом протягнути довільну дію повз реєстр.
    """
    ip, user_agent = request_context(request)
    await command_permissions.set_rule(
        db,
        command_type=command_type,
        required_permission=payload.required_permission,
        enabled=payload.enabled,
        actor=actor,
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return await _spec_view(db, command_type, actor)


@router.delete(
    "/commands/registry/{command_type}/permission", response_model=CommandSpecOut
)
async def reset_command_permission(
    request: Request,
    command_type: str,
    actor: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_session),
) -> CommandSpecOut:
    """Повертає тип команди до дозволу за замовчуванням із реєстру."""
    ip, user_agent = request_context(request)
    await command_permissions.reset_rule(
        db, command_type=command_type, actor=actor, ip=ip, user_agent=user_agent
    )
    await db.commit()
    return await _spec_view(db, command_type, actor)


async def _spec_view(
    db: AsyncSession, command_type: str, user: User
) -> CommandSpecOut:
    spec = command_registry.get_spec(command_type)
    if spec is None:
        raise NotFoundError(f"Команди {command_type} немає в реєстрі")
    rule = await command_permissions.effective(db, command_type)
    assert rule is not None
    granted = permissions_for(user.role)
    return CommandSpecOut(
        type=spec.type,
        title=spec.title,
        description=spec.description,
        user_visible=spec.user_visible,
        required_permission=rule.required_permission.value,
        enabled=rule.enabled,
        overridden=rule.overridden,
        roles=rule.roles,
        allowed_for_me=rule.enabled and rule.required_permission in granted,
        disruptive=spec.disruptive,
        requires_device_owner=spec.requires_device_owner,
        ttl_seconds=spec.ttl_seconds,
        params=_describe_params(spec),
    )


# --------------------------------------------------------------- видача ---


@router.post(
    "/devices/{device_id}/commands",
    response_model=CommandOut,
    status_code=http_status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("mutation"))],
)
async def issue_command(
    request: Request,
    device_id: uuid.UUID,
    payload: CommandIssueRequest,
    # Базовий дозвіл лише відсікає сторонніх; конкретне право на саме цей
    # тип команди перевіряє реєстр усередині `issue`.
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Видає команду з реєстру.

    Перевіряються по черзі: наявність типу в реєстрі, чинний дозвіл ролі,
    режим керування пристроєм, схема параметрів і відсутність конфлікту з
    уже активною командою. Кожна відмова потрапляє в аудит із причиною.
    """
    ip, user_agent = request_context(request)
    device = await device_service.get_device(db, device_id)

    command = await command_service.issue(
        db,
        device=device,
        command_type=payload.type,
        params=payload.params,
        user=user,
        ip=ip,
        user_agent=user_agent,
        idempotency_key=payload.idempotency_key,
    )
    await db.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.get("/devices/{device_id}/commands", response_model=Page[CommandOut])
async def device_command_history(
    device_id: uuid.UUID,
    command_type: str | None = Query(default=None, alias="type", max_length=32),
    status_filter: str | None = Query(default=None, alias="status", max_length=16),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> Page[CommandOut]:
    device = await device_service.get_device(session, device_id)
    conditions = [Command.device_id == device.id]
    if command_type:
        conditions.append(Command.type == command_type)
    if status_filter:
        conditions.append(Command.status == status_filter)

    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Command).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(Command)
            .where(*conditions)
            .order_by(Command.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return Page(
        items=[CommandOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/commands", response_model=Page[CommandOut])
async def command_history(
    command_type: str | None = Query(default=None, alias="type", max_length=32),
    status_filter: str | None = Query(default=None, alias="status", max_length=16),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> Page[CommandOut]:
    conditions = []
    if command_type:
        conditions.append(Command.type == command_type)
    if status_filter:
        conditions.append(Command.status == status_filter)

    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Command).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(Command)
            .where(*conditions)
            .order_by(Command.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return Page(
        items=[CommandOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/commands/stats", response_model=list[CommandStats])
async def command_stats(
    device_id: uuid.UUID | None = None,
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[CommandStats]:
    """Зведення за типами: скільки виконано, скільки впало, скільки тривало."""
    conditions = [Command.device_id == device_id] if device_id else []
    rows = (
        await session.execute(
            sa.select(
                Command.type,
                sa.func.count().label("total"),
                sa.func.sum(sa.case((Command.status == "succeeded", 1), else_=0)),
                sa.func.sum(sa.case((Command.status == "failed", 1), else_=0)),
                sa.func.sum(sa.case((Command.status == "expired", 1), else_=0)),
                sa.func.sum(sa.case((Command.status == "cancelled", 1), else_=0)),
                sa.func.sum(
                    sa.case((Command.status.in_(command_service.ACTIVE_STATUSES), 1), else_=0)
                ),
                sa.func.avg(Command.duration_ms),
                sa.func.max(Command.duration_ms),
            )
            .where(*conditions)
            .group_by(Command.type)
            .order_by(sa.func.count().desc())
        )
    ).all()

    return [
        CommandStats(
            type=row[0],
            total=int(row[1] or 0),
            succeeded=int(row[2] or 0),
            failed=int(row[3] or 0),
            expired=int(row[4] or 0),
            cancelled=int(row[5] or 0),
            pending=int(row[6] or 0),
            avg_duration_ms=int(row[7]) if row[7] is not None else None,
            max_duration_ms=int(row[8]) if row[8] is not None else None,
        )
        for row in rows
    ]


@router.get("/commands/{command_id}", response_model=CommandDetail)
async def get_command(
    command_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> CommandDetail:
    command = await session.get(Command, command_id)
    if command is None:
        raise NotFoundError("Команду не знайдено")

    detail = CommandDetail.model_validate(command)
    spec = command_registry.get_spec(command.type)
    if spec is not None:
        detail.title = spec.title
        detail.disruptive = spec.disruptive
        detail.requires_device_owner = spec.requires_device_owner

    if command.created_by_id:
        author = await session.get(User, command.created_by_id)
        detail.created_by_email = author.email if author else None
    device = await session.get(Device, command.device_id)
    detail.device_name = device.name if device else None
    return detail


@router.post("/commands/{command_id}/cancel", response_model=CommandOut)
async def cancel_command(
    request: Request,
    command_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
) -> Command:
    """Скасовує ще не виконану команду.

    Право на скасування дорівнює праву на видачу: хто міг створити команду
    цього типу, той може її і зняти. Раніше тут стояв фіксований
    `app:manage`, через що оператор не міг скасувати навіть власну
    безпечну команду.
    """
    ip, user_agent = request_context(request)
    existing = await session.get(Command, command_id)
    if existing is None:
        raise NotFoundError("Команду не знайдено")

    rule = await command_permissions.effective(session, existing.type)
    if rule is not None and rule.required_permission not in permissions_for(user.role):
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError(
            f"Недостатньо прав: потрібен дозвіл {rule.required_permission.value}"
        )

    command = await command_service.cancel(session, command_id)
    await record_audit(
        session,
        action=AuditAction.COMMAND_CANCELLED,
        user=user,
        object_type="command",
        object_id=command.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "command_type": command.type,
            "device_id": str(command.device_id),
            "was_status": existing.status,
        },
    )
    await session.commit()
    return command
