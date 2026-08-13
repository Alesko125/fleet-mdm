"""Сеанси віддаленої підтримки.

Головне правило модуля: **без явної згоди користувача пристрою екран не
транслюється**. Технічно це забезпечено на трьох рівнях:

1. сервер не переводить сеанс у `active`, доки агент не надішле згоду;
2. агент показує власний екран запиту з іменем адміністратора й причиною;
3. сам Android показує системний діалог `MediaProjection` і тримає постійний
   індикатор трансляції — ми його не обходимо.

Прихованого захоплення екрана в системі немає: сеанс завжди має видимий
стан і на пристрої, і в панелі, а кожен перехід стану потрапляє в аудит.
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import time
import uuid
from datetime import timedelta

import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.redis_client import ADMIN_CHANNEL
from app.core.security import as_utc, token_fingerprint, utcnow
from app.db.models import AuditLog, Device, RemoteSession, User
from app.services.audit import RESULT_DENIED, AuditAction, record_audit
from app.ws.manager import publish

logger = logging.getLogger(__name__)

OPEN_STATES = ("requested", "awaiting_consent", "active")
# Скільки чекаємо на реакцію користувача, перш ніж вважати запит простроченим.
CONSENT_TIMEOUT_SECONDS = 180
# Якщо кадри перестали надходити — сеанс вважається обірваним.
FRAME_SILENCE_SECONDS = 90

# Квиток перегляду живе рівно стільки, скільки треба, щоб відкрити сокет.
VIEWER_TICKET_TTL_SECONDS = 60
# Присутність у каналі вважається свіжою стільки секунд.
PRESENCE_TTL_SECONDS = 20
# Як часто сторони оновлюють свою присутність.
PRESENCE_REFRESH_SECONDS = 5

# Дії керування, які сервер погоджується ретранслювати на пристрій.
ALLOWED_INPUT_ACTIONS = frozenset({"tap", "swipe", "back", "home", "recents"})


async def get_session_or_404(
    session: AsyncSession, session_id: uuid.UUID, *, device: Device | None = None
) -> RemoteSession:
    row = await session.get(RemoteSession, session_id)
    if row is None:
        raise NotFoundError("Сеанс не знайдено")
    if device is not None and row.device_id != device.id:
        # Пристрій не має знати навіть про існування чужих сеансів.
        raise NotFoundError("Сеанс не знайдено")
    return row


async def create_session(
    db: AsyncSession,
    *,
    device: Device,
    user: User,
    reason: str,
    input_enabled: bool,
    max_duration_minutes: int,
    ip: str | None,
    user_agent: str | None,
) -> tuple[RemoteSession, str]:
    """Створює запит на сеанс. Повертає `(сеанс, одноразовий секрет)`."""
    existing = (
        await db.execute(
            sa.select(RemoteSession).where(
                RemoteSession.device_id == device.id,
                RemoteSession.state.in_(OPEN_STATES),
            )
        )
    ).scalars().first()
    if existing is not None:
        raise ConflictError(
            f"Для пристрою вже є відкритий сеанс ({existing.state}); завершіть його спершу"
        )

    join_secret = secrets.token_urlsafe(32)
    now = utcnow()

    row = RemoteSession(
        device_id=device.id,
        requested_by_id=user.id,
        state="requested",
        transport="websocket",
        reason=reason,
        input_enabled=input_enabled,
        join_secret_hash=token_fingerprint(join_secret),
        expires_at=now + timedelta(minutes=max_duration_minutes),
        created_at=now,
    )
    db.add(row)
    await db.flush()

    await record_audit(
        db,
        action=AuditAction.REMOTE_SESSION_REQUESTED,
        user=user,
        object_type="remote_session",
        object_id=row.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "device_id": str(device.id),
            "device_name": device.name,
            "reason": reason,
            "input_requested": input_enabled,
            "max_duration_minutes": max_duration_minutes,
        },
    )
    return row, join_secret


def verify_join_secret(row: RemoteSession, secret: str) -> bool:
    if not row.join_secret_hash or not secret:
        return False
    return secrets.compare_digest(row.join_secret_hash, token_fingerprint(secret))


async def record_consent(
    db: AsyncSession,
    *,
    row: RemoteSession,
    device: Device,
    granted: bool,
    input_available: bool,
    reason: str | None,
    ip: str | None,
    user_agent: str | None,
) -> RemoteSession:
    """Фіксує відповідь користувача пристрою."""
    if row.consent_at is not None:
        # Повторний виклик (наприклад, ретрай агента після обриву мережі)
        # нічого не змінює: підвищити рівень доступу другою відповіддю не можна.
        return row
    if row.state not in ("requested", "awaiting_consent"):
        raise ConflictError(f"Сеанс уже у стані {row.state}")

    now = utcnow()
    if not granted:
        row.state = "denied"
        row.ended_at = now
        row.end_reason = "consent_denied"
        await record_audit(
            db,
            action=AuditAction.REMOTE_SESSION_CONSENT,
            result=RESULT_DENIED,
            device=device,
            object_type="remote_session",
            object_id=row.id,
            ip=ip,
            user_agent=user_agent,
            details={"granted": False, "reason": reason},
        )
        return row

    row.state = "awaiting_consent"
    row.consent_at = now
    row.input_available = input_available
    # Керування можливе лише якщо його і запитували, і пристрій підтримує.
    row.input_enabled = row.input_enabled and input_available

    await record_audit(
        db,
        action=AuditAction.REMOTE_SESSION_CONSENT,
        device=device,
        object_type="remote_session",
        object_id=row.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "granted": True,
            "input_available": input_available,
            "input_enabled": row.input_enabled,
        },
    )
    return row


async def mark_started(
    db: AsyncSession, *, row: RemoteSession, device: Device
) -> RemoteSession:
    """Пристрій під'єднався до каналу і починає трансляцію."""
    if row.consent_at is None:
        raise PermissionDeniedError("Трансляція без згоди користувача неможлива")
    if row.state not in ("awaiting_consent", "active"):
        raise ConflictError(f"Сеанс у стані {row.state}")

    if row.state != "active":
        row.state = "active"
        row.started_at = utcnow()
        await record_audit(
            db,
            action=AuditAction.REMOTE_SESSION_STARTED,
            device=device,
            object_type="remote_session",
            object_id=row.id,
            details={"transport": row.transport, "input_enabled": row.input_enabled},
        )
    return row


async def end_session(
    db: AsyncSession,
    *,
    row: RemoteSession,
    reason: str,
    user: User | None = None,
    device: Device | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> RemoteSession:
    if row.state in ("ended", "denied", "expired"):
        return row

    row.state = "ended"
    row.ended_at = utcnow()
    row.end_reason = reason[:64]

    duration = None
    started = as_utc(row.started_at)
    if started is not None:
        duration = int((row.ended_at - started).total_seconds())

    await record_audit(
        db,
        action=AuditAction.REMOTE_SESSION_ENDED,
        user=user,
        device=device if user is None else None,
        actor_label=None if (user or device) else "system",
        object_type="remote_session",
        object_id=row.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "reason": reason,
            "duration_seconds": duration,
            "frames": row.frames_sent,
            "bytes": row.bytes_sent,
            "input_events": row.input_events,
            "device_id": str(row.device_id),
        },
    )
    return row


async def expire_stale(db: AsyncSession, *, redis: Redis | None = None) -> int:
    """Закриває сеанси, які прострочені або замовкли. Викликається фоново."""
    now = utcnow()
    rows = (
        await db.execute(sa.select(RemoteSession).where(RemoteSession.state.in_(OPEN_STATES)))
    ).scalars().all()

    closed = 0
    for row in rows:
        reason: str | None = None
        expires_at = as_utc(row.expires_at)
        created_at = as_utc(row.created_at)
        last_frame = as_utc(row.last_frame_at)

        if expires_at is not None and expires_at <= now:
            reason = "max_duration_reached"
        elif row.state in ("requested", "awaiting_consent") and created_at is not None:
            if (now - created_at).total_seconds() > CONSENT_TIMEOUT_SECONDS:
                reason = "consent_timeout"
        elif row.state == "active" and last_frame is not None:
            if (now - last_frame).total_seconds() > FRAME_SILENCE_SECONDS:
                reason = "stream_lost"

        if reason is None:
            continue

        row.state = "expired" if reason != "stream_lost" else "ended"
        row.ended_at = now
        row.end_reason = reason
        closed += 1

        await record_audit(
            db,
            action=AuditAction.REMOTE_SESSION_ENDED,
            actor_label="system",
            object_type="remote_session",
            object_id=row.id,
            details={
                "reason": reason,
                "device_id": str(row.device_id),
                "frames": row.frames_sent,
                "auto": True,
            },
        )
        await notify(redis, "support_session_ended", row, reason=reason)

    if closed:
        await db.commit()
    return closed


async def touch_frames(
    row: RemoteSession, *, frames: int, size_bytes: int, stats: dict | None = None
) -> None:
    """Оновлює лічильники трансляції (викликається з WS-обробника)."""
    row.frames_sent += frames
    row.bytes_sent += size_bytes
    row.last_frame_at = utcnow()
    if stats:
        row.stats = {**(row.stats or {}), **stats}


async def notify(
    redis: Redis | None, event: str, row: RemoteSession, **extra
) -> None:
    if redis is None:
        return
    try:
        await publish(
            redis,
            ADMIN_CHANNEL,
            {
                "type": event,
                "session": {
                    "id": str(row.id),
                    "device_id": str(row.device_id),
                    "state": row.state,
                    "frames": row.frames_sent,
                    "bytes": row.bytes_sent,
                },
                **extra,
            },
        )
    except Exception as exc:  # noqa: BLE001 — події не критичні
        logger.debug("Подію сеансу не надіслано", extra={"error": str(exc)})


def quality_hint(bytes_sent: int, elapsed_seconds: float) -> dict[str, int]:
    """Проста адаптація під канал за фактичною пропускною здатністю.

    Рахуємо середній бітрейт і, якщо він низький, просимо пристрій
    зменшити частоту кадрів і якість JPEG.
    """
    settings = get_settings()
    _ = settings

    if elapsed_seconds <= 0:
        return {"max_fps": 5, "scale_percent": 50, "jpeg_quality": 60}

    kbps = (bytes_sent * 8 / 1000) / elapsed_seconds
    if kbps < 300:
        return {"max_fps": 2, "scale_percent": 35, "jpeg_quality": 45}
    if kbps < 1000:
        return {"max_fps": 4, "scale_percent": 50, "jpeg_quality": 55}
    if kbps < 3000:
        return {"max_fps": 8, "scale_percent": 70, "jpeg_quality": 70}
    return {"max_fps": 12, "scale_percent": 100, "jpeg_quality": 80}


# ------------------------------------------------------ квитки перегляду ---
#
# `join_secret` видається один раз при створенні сеансу й живе 30–240 хвилин.
# Тримати його в браузері весь сеанс незручно (перезавантаження вкладки — і
# перегляд утрачено) і надлишково довго. Тому для самого підключення оператор
# бере окремий **одноразовий короткоживучий квиток**: сервер видає його лише
# власнику дозволу `support:session`, пише видачу в аудит і гасить квиток при
# першому використанні.


def _ticket_key(digest: str) -> str:
    return f"support:ticket:{digest}"


async def issue_viewer_ticket(
    redis: Redis, *, row: RemoteSession, user: User
) -> tuple[str, int]:
    """Видає одноразовий квиток на приєднання до перегляду."""
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps(
        {"session_id": str(row.id), "user_id": str(user.id)}, separators=(",", ":")
    )
    await redis.set(
        _ticket_key(token_fingerprint(ticket)), payload, ex=VIEWER_TICKET_TTL_SECONDS
    )
    return ticket, VIEWER_TICKET_TTL_SECONDS


async def consume_viewer_ticket(
    redis: Redis, *, session_id: uuid.UUID, ticket: str, user_id: uuid.UUID
) -> bool:
    """Гасить квиток. Повертає True, якщо він дійсний саме для цієї пари.

    `GETDEL` робить перевірку й погашення однією операцією: перехоплений
    квиток не спрацює вдруге, навіть якщо два підключення прийдуть разом.
    """
    if not ticket:
        return False
    raw = await redis.getdel(_ticket_key(token_fingerprint(ticket)))
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return data.get("session_id") == str(session_id) and data.get("user_id") == str(user_id)


# ---------------------------------------------------------- присутність ---
#
# Стан «хто зараз у каналі» живе в Redis, а не в памʼяті процесу: панель за
# кількома репліками API має бачити одну й ту саму картину.


def _device_presence_key(session_id: uuid.UUID | str) -> str:
    return f"support:{session_id}:device_online"


def _viewers_key(session_id: uuid.UUID | str) -> str:
    return f"support:{session_id}:viewers"


async def mark_device_online(redis: Redis | None, session_id: uuid.UUID | str) -> None:
    if redis is None:
        return
    with contextlib.suppress(Exception):
        await redis.set(_device_presence_key(session_id), "1", ex=PRESENCE_TTL_SECONDS)


async def mark_device_offline(redis: Redis | None, session_id: uuid.UUID | str) -> None:
    if redis is None:
        return
    with contextlib.suppress(Exception):
        await redis.delete(_device_presence_key(session_id))


async def mark_viewer_online(
    redis: Redis | None, session_id: uuid.UUID | str, viewer_id: uuid.UUID | str
) -> None:
    if redis is None:
        return
    with contextlib.suppress(Exception):
        key = _viewers_key(session_id)
        await redis.hset(key, str(viewer_id), str(int(time.time())))
        await redis.expire(key, PRESENCE_TTL_SECONDS * 4)


async def mark_viewer_offline(
    redis: Redis | None, session_id: uuid.UUID | str, viewer_id: uuid.UUID | str
) -> None:
    if redis is None:
        return
    with contextlib.suppress(Exception):
        await redis.hdel(_viewers_key(session_id), str(viewer_id))


async def connection_status(
    redis: Redis | None, row: RemoteSession
) -> dict[str, object]:
    """Хто зараз у каналі — авторитетно, з Redis.

    Панель раніше здогадувалася про це з того, чи прилітають кадри в її
    власний сокет; тепер стан однаковий для всіх операторів і не залежить
    від того, до якої репліки API вони підключені.
    """
    # Redis тут — джерело зручності, а не істини про доступ: якщо він
    # недоступний, панель просто побачить «немає даних», а не помилку.
    device_online = False
    viewers = 0

    if redis is not None:
        with contextlib.suppress(Exception):
            device_online = bool(await redis.exists(_device_presence_key(row.id)))
        with contextlib.suppress(Exception):
            entries = await redis.hgetall(_viewers_key(row.id))
            cutoff = int(time.time()) - PRESENCE_TTL_SECONDS
            viewers = sum(
                1
                for value in entries.values()
                if str(value).isdigit() and int(value) >= cutoff
            )

    last_frame = as_utc(row.last_frame_at)
    seconds_since_frame = (
        int((utcnow() - last_frame).total_seconds()) if last_frame else None
    )

    return {
        "device_online": device_online,
        "viewers": viewers,
        # «Транслює» = пристрій у каналі І кадри надходили щойно.
        "streaming": device_online
        and seconds_since_frame is not None
        and seconds_since_frame <= FRAME_SILENCE_SECONDS,
        "seconds_since_frame": seconds_since_frame,
        "frame_width": row.frame_width,
        "frame_height": row.frame_height,
    }


# ------------------------------------------------- метрики та керування ---


def apply_device_metrics(row: RemoteSession, message: dict) -> bool:
    """Приймає роздільність кадру, яку повідомив агент.

    Без неї панель не може перерахувати клік по зображенню в координати
    пристрою, тож віддалене керування просто не вмикається.
    """
    width = message.get("width")
    height = message.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        return False
    if not (0 < width <= 8192 and 0 < height <= 8192):
        return False

    changed = row.frame_width != width or row.frame_height != height
    row.frame_width = width
    row.frame_height = height
    row.stats = {
        **(row.stats or {}),
        "frame_width": width,
        "frame_height": height,
        "density_dpi": message.get("density_dpi"),
    }
    return changed


def sanitize_input(message: dict, row: RemoteSession) -> dict | None:
    """Перевіряє дію керування перед ретрансляцією на пристрій.

    Пропускаємо лише відомі дії з координатами в межах кадру: сервер не має
    бути прозорою трубою для довільного JSON у бік пристрою.
    """
    action = message.get("action")
    if action not in ALLOWED_INPUT_ACTIONS:
        return None

    clean: dict[str, object] = {"type": "input", "action": action}
    if action in ("back", "home", "recents"):
        return clean

    width = row.frame_width or 0
    height = row.frame_height or 0
    if width <= 0 or height <= 0:
        # Роздільність невідома — координати перевірити нічим.
        return None

    def coordinate(name: str, limit: int) -> int | None:
        value = message.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = int(value)
        return number if 0 <= number <= limit else None

    if action == "tap":
        x = coordinate("x", width)
        y = coordinate("y", height)
        if x is None or y is None:
            return None
        clean.update({"x": x, "y": y})
        return clean

    from_x = coordinate("from_x", width)
    from_y = coordinate("from_y", height)
    to_x = coordinate("to_x", width)
    to_y = coordinate("to_y", height)
    if None in (from_x, from_y, to_x, to_y):
        return None

    duration = message.get("duration_ms")
    duration_ms = int(duration) if isinstance(duration, (int, float)) else 300
    clean.update(
        {
            "from_x": from_x,
            "from_y": from_y,
            "to_x": to_x,
            "to_y": to_y,
            "duration_ms": max(50, min(duration_ms, 3000)),
        }
    )
    return clean


async def record_input_batch(
    db: AsyncSession, *, row: RemoteSession, user: User, count: int, actions: list[str]
) -> None:
    """Пише в аудит підсумок дій керування.

    Кожен тап окремим записом залив би журнал; фіксуємо пачку — скільки дій
    і яких саме типів — при завершенні сеансу чи від'єднанні оператора.
    """
    if count <= 0:
        return
    await record_audit(
        db,
        action=AuditAction.REMOTE_SESSION_INPUT,
        user=user,
        object_type="remote_session",
        object_id=row.id,
        details={
            "events": count,
            "actions": sorted(set(actions)),
            "device_id": str(row.device_id),
        },
    )


# ---------------------------------------------------------- аудит сеансу --

# Події, що стосуються саме сеансів підтримки.
SESSION_AUDIT_ACTIONS = (
    AuditAction.REMOTE_SESSION_REQUESTED,
    AuditAction.REMOTE_SESSION_CONSENT,
    AuditAction.REMOTE_SESSION_STARTED,
    AuditAction.REMOTE_SESSION_ENDED,
    AuditAction.REMOTE_SESSION_VIEWER_JOINED,
    AuditAction.REMOTE_SESSION_VIEWER_LEFT,
    AuditAction.REMOTE_SESSION_INPUT,
)


async def session_audit_trail(
    db: AsyncSession, session_id: uuid.UUID, *, limit: int = 200
) -> list[AuditLog]:
    """Журнал подій конкретного сеансу — у хронологічному порядку."""
    rows = (
        await db.execute(
            sa.select(AuditLog)
            .where(
                AuditLog.object_type == "remote_session",
                AuditLog.object_id == str(session_id),
                AuditLog.action.in_(SESSION_AUDIT_ACTIONS),
            )
            .order_by(AuditLog.occurred_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)
