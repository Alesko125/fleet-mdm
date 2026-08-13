"""Сеанси віддаленої підтримки (адміністративна частина)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.rate_limit import rate_limit
from app.core.rbac import Permission
from app.core.redis_client import get_redis
from app.db.models import Device, RemoteSession, User
from app.db.session import get_session
from app.schemas.command import CommandOut
from app.schemas.common import Page
from app.schemas.support import (
    ConnectionStatus,
    SessionAuditEntry,
    SessionEnd,
    SupportSessionCreate,
    SupportSessionCreated,
    SupportSessionDetail,
    SupportSessionOut,
    ViewerTicket,
)
from app.services import commands as command_service
from app.services import device as device_service
from app.services import support as support_service
from app.services.audit import AuditAction, record_audit

router = APIRouter(tags=["remote-support"])


def _viewer_url(session_id: uuid.UUID) -> str:
    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    scheme = "wss" if base.startswith("https://") else "ws"
    host = base.split("://", 1)[-1]
    return f"{scheme}://{host}/ws/support/{session_id}"


@router.post(
    "/devices/{device_id}/support-sessions",
    response_model=SupportSessionCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("mutation"))],
)
async def request_session(
    request: Request,
    device_id: uuid.UUID,
    payload: SupportSessionCreate,
    user: User = Depends(require_permission(Permission.SUPPORT_SESSION)),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> SupportSessionCreated:
    """Створює запит на сеанс. Трансляція почнеться лише після згоди користувача."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(db, device_id)

    row, join_secret = await support_service.create_session(
        db,
        device=device,
        user=user,
        reason=payload.reason,
        input_enabled=payload.input_enabled,
        max_duration_minutes=payload.max_duration_minutes,
        ip=ip,
        user_agent=user_agent,
    )

    command = await command_service.create_command(
        db,
        device=device,
        command_type="REMOTE_SUPPORT_REQUEST",
        user=user,
        payload={
            "session_id": str(row.id),
            "reason": payload.reason,
            "requested_by": user.email,
            "input_requested": payload.input_enabled,
            "join_secret": join_secret,
            "websocket_url": _viewer_url(row.id),
            "expires_at": row.expires_at,
            "consent_timeout_seconds": support_service.CONSENT_TIMEOUT_SECONDS,
        },
        ttl_seconds=support_service.CONSENT_TIMEOUT_SECONDS,
    )
    await db.commit()
    await command_service.dispatch(redis, command)

    return SupportSessionCreated(
        **SupportSessionOut.model_validate(row).model_dump(),
        join_secret=join_secret,
        viewer_url=_viewer_url(row.id),
    )


@router.get("/support-sessions", response_model=Page[SupportSessionDetail])
async def list_sessions(
    device_id: uuid.UUID | None = None,
    state: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    db: AsyncSession = Depends(get_session),
) -> Page[SupportSessionDetail]:
    conditions = []
    if device_id:
        conditions.append(RemoteSession.device_id == device_id)
    if state:
        conditions.append(RemoteSession.state == state)

    total = (
        await db.execute(
            sa.select(sa.func.count()).select_from(RemoteSession).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            sa.select(RemoteSession, Device.name, User.email)
            .join(Device, Device.id == RemoteSession.device_id)
            .outerjoin(User, User.id == RemoteSession.requested_by_id)
            .where(*conditions)
            .order_by(RemoteSession.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = []
    for row, device_name, email in rows:
        item = SupportSessionDetail.model_validate(row)
        item.device_name = device_name
        item.requested_by_email = email
        items.append(item)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/support-sessions/{session_id}", response_model=SupportSessionDetail)
async def session_details(
    session_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> SupportSessionDetail:
    row = await support_service.get_session_or_404(db, session_id)
    device = await db.get(Device, row.device_id)
    requester = await db.get(User, row.requested_by_id) if row.requested_by_id else None

    item = SupportSessionDetail.model_validate(row)
    item.device_name = device.name if device else None
    item.requested_by_email = requester.email if requester else None
    item.connection = ConnectionStatus(
        **await support_service.connection_status(redis, row)
    )
    return item


@router.post(
    "/support-sessions/{session_id}/viewer-ticket",
    response_model=ViewerTicket,
    dependencies=[Depends(rate_limit("mutation"))],
)
async def issue_viewer_ticket(
    request: Request,
    session_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.SUPPORT_SESSION)),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ViewerTicket:
    """Видає одноразовий короткоживучий квиток на приєднання до перегляду.

    Раніше єдиним ключем до каналу був `join_secret`, який повертався один
    раз при створенні сеансу: перезавантаження вкладки означало втрату
    перегляду, а тримати довгоживучий секрет у браузері весь сеанс — зайвий
    ризик. Квиток живе секунди, гасне при першому використанні й прив'язаний
    до конкретного оператора, тож ним можна безпечно перепідключатися.
    """
    ip, user_agent = request_context(request)
    row = await support_service.get_session_or_404(db, session_id)

    if not row.is_open_state:
        raise ConflictError(f"Сеанс у стані {row.state} — приєднатися неможливо")

    ticket, expires_in = await support_service.issue_viewer_ticket(
        redis, row=row, user=user
    )
    await record_audit(
        db,
        action=AuditAction.REMOTE_SESSION_VIEWER_JOINED,
        user=user,
        object_type="remote_session",
        object_id=row.id,
        ip=ip,
        user_agent=user_agent,
        details={"phase": "ticket_issued", "device_id": str(row.device_id)},
    )
    await db.commit()

    return ViewerTicket(
        session_id=row.id,
        ticket=ticket,
        expires_in=expires_in,
        viewer_url=_viewer_url(row.id),
    )


@router.get(
    "/support-sessions/{session_id}/connection", response_model=ConnectionStatus
)
async def session_connection(
    session_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.DEVICE_READ)),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ConnectionStatus:
    """Живий стан каналу: чи пристрій у мережі, скільки глядачів, чи йдуть кадри."""
    row = await support_service.get_session_or_404(db, session_id)
    return ConnectionStatus(**await support_service.connection_status(redis, row))


@router.get(
    "/support-sessions/{session_id}/audit", response_model=list[SessionAuditEntry]
)
async def session_audit(
    session_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.AUDIT_READ)),
    db: AsyncSession = Depends(get_session),
) -> list[SessionAuditEntry]:
    """Журнал подій сеансу: запит, згода, старт, приєднання, керування, кінець."""
    await support_service.get_session_or_404(db, session_id)
    rows = await support_service.session_audit_trail(db, session_id)
    return [SessionAuditEntry.model_validate(row) for row in rows]


@router.post("/support-sessions/{session_id}/stop", response_model=SupportSessionOut)
async def stop_session(
    request: Request,
    session_id: uuid.UUID,
    payload: SessionEnd | None = None,
    user: User = Depends(require_permission(Permission.SUPPORT_SESSION)),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> SupportSessionOut:
    ip, user_agent = request_context(request)
    row = await support_service.get_session_or_404(db, session_id)
    device = await db.get(Device, row.device_id)

    await support_service.end_session(
        db,
        row=row,
        reason=(payload.reason if payload else "stopped_by_admin"),
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    # Присутність у каналі прибираємо одразу, не чекаючи на розрив сокетів.
    await support_service.mark_device_offline(redis, row.id)

    if device is not None and device.deleted_at is None:
        command = await command_service.create_command(
            db,
            device=device,
            command_type="REMOTE_SUPPORT_END",
            user=user,
            payload={"session_id": str(row.id), "reason": row.end_reason},
            ttl_seconds=300,
        )
        await db.commit()
        await command_service.dispatch(redis, command)
    else:
        await db.commit()

    await support_service.notify(redis, "support_session_ended", row, reason=row.end_reason)
    return SupportSessionOut.model_validate(row)


@router.post(
    "/devices/{device_id}/support-sessions/refresh",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_request(
    device_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.SUPPORT_SESSION)),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Повторно надсилає пристрою запит відкритого сеансу (якщо він «загубився»)."""
    device = await device_service.get_device(db, device_id)
    row = (
        await db.execute(
            sa.select(RemoteSession)
            .where(
                RemoteSession.device_id == device.id,
                RemoteSession.state.in_(support_service.OPEN_STATES),
            )
            .order_by(RemoteSession.created_at.desc())
        )
    ).scalars().first()
    if row is None:
        raise NotFoundError("Відкритого сеансу немає")

    command = await command_service.create_command(
        db,
        device=device,
        command_type="REMOTE_SUPPORT_REQUEST",
        user=user,
        payload={
            "session_id": str(row.id),
            "reason": row.reason,
            "requested_by": user.email,
            "input_requested": row.input_enabled,
            "websocket_url": _viewer_url(row.id),
            "resend": True,
        },
        ttl_seconds=support_service.CONSENT_TIMEOUT_SECONDS,
    )
    await db.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)
