"""WebSocket-канал сеансу віддаленої підтримки.

Кадри екрана ретранслюються **в межах одного процесу**: пристрій підключений
до тієї ж репліки API, що й оператор. Для кількох реплік у nginx достатньо
липкої маршрутизації для `/ws/support/` — гнати відеопотік через Redis
pub/sub було б і дорого, і зайво.

Канал не має жодного «тихого» режиму: доки він відкритий, на пристрої
працює постійне сповіщення агента й системний індикатор трансляції Android.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.rbac import Permission, role_has_permission
from app.core.redis_client import get_redis_ws
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_DEVICE,
    TokenError,
    as_utc,
    decode_token,
    token_predates,
)
from app.db.models import Device, RemoteSession, User
from app.db.session import get_sessionmaker
from app.services import support as support_service
from app.services.audit import AuditAction, record_audit
from app.ws.manager import registry
from app.ws.routes import origin_allowed

logger = logging.getLogger(__name__)
router = APIRouter()

# Як часто зберігати лічильники кадрів у БД (щоб не писати на кожен кадр).
STATS_FLUSH_SECONDS = 5.0
MAX_FRAME_BYTES = 2 * 1024 * 1024
# Як часто оператор перепитує стан сеансу в БД. Права, зняті адміністратором
# (або сеанс, завершений користувачем пристрою), мають діяти майже одразу,
# а не «доживати» у відкритому сокеті до його закриття.
VIEWER_STATE_TTL_SECONDS = 2.0
# Стеля розміру керуючого повідомлення від оператора.
MAX_CONTROL_FRAME_BYTES = 16 * 1024


def device_key(session_id: uuid.UUID | str) -> str:
    return f"support:{session_id}:device"


def viewer_key(session_id: uuid.UUID | str) -> str:
    return f"support:{session_id}:viewers"


@router.websocket("/ws/support/{session_id}")
async def support_socket(
    websocket: WebSocket,
    session_id: uuid.UUID,
    token: str = Query(..., max_length=4096),
    role: str = Query(..., max_length=16),
    secret: str = Query(default="", max_length=512),
    ticket: str = Query(default="", max_length=512),
) -> None:
    if role not in ("device", "viewer"):
        await websocket.close(code=4400, reason="bad_role")
        return
    if role == "viewer" and not origin_allowed(websocket):
        await websocket.close(code=4403, reason="origin_not_allowed")
        return

    factory = get_sessionmaker()
    async with factory() as db:
        row = await db.get(RemoteSession, session_id)
        if row is None:
            await websocket.close(code=4404, reason="session_not_found")
            return
        if row.state not in support_service.OPEN_STATES:
            await websocket.close(code=4409, reason=f"session_{row.state}")
            return

        if role == "device":
            device = await _authenticate_device(db, token)
            if device is None or device.id != row.device_id:
                await websocket.close(code=4401, reason="unauthorized")
                return
            if row.consent_at is None:
                # Без згоди користувача трансляція неможлива — це і є та межа,
                # яку модуль ніколи не переступає.
                await websocket.close(code=4403, reason="consent_required")
                return
            await support_service.mark_started(db, row=row, device=device)
            await db.commit()
        else:
            user = await _authenticate_user(db, token)
            if user is None or not role_has_permission(user.role, Permission.SUPPORT_SESSION):
                await websocket.close(code=4401, reason="unauthorized")
                return

            # Приєднатися можна двома шляхами: одноразовим квитком (штатний
            # для панелі — переживає перезавантаження й перепідключення) або
            # секретом, виданим при створенні сеансу (для сценаріїв без
            # браузера). Обидва прив'язані до конкретного сеансу.
            redis_client = get_redis_ws(websocket)
            authorized = False
            if ticket:
                authorized = await support_service.consume_viewer_ticket(
                    redis_client, session_id=session_id, ticket=ticket, user_id=user.id
                )
            if not authorized and secret:
                authorized = support_service.verify_join_secret(row, secret)

            if not authorized:
                await record_audit(
                    db,
                    action=AuditAction.REMOTE_SESSION_VIEWER_JOINED,
                    result="denied",
                    user=user,
                    object_type="remote_session",
                    object_id=row.id,
                    details={"reason": "bad_ticket_or_secret"},
                )
                await db.commit()
                await websocket.close(code=4403, reason="bad_secret")
                return

            row.viewer_joins += 1
            row.last_viewer_at = support_service.utcnow()
            await db.commit()

            viewer_id = user.id
            viewer_label = user.email
            device_id = row.device_id

    await websocket.accept()
    redis = get_redis_ws(websocket)

    if role == "device":
        await _run_device(websocket, session_id, redis)
    else:
        await _run_viewer(websocket, session_id, viewer_id, viewer_label, device_id, redis)


# ------------------------------------------------------------- пристрій ---


async def _run_device(websocket: WebSocket, session_id: uuid.UUID, redis) -> None:
    key = device_key(session_id)
    await registry.add(key, websocket)
    await support_service.mark_device_online(redis, session_id)

    factory = get_sessionmaker()
    frames = 0
    total_bytes = 0
    started = time.monotonic()
    last_flush = started
    last_presence = started

    try:
        await websocket.send_json({"type": "ready", "session_id": str(session_id)})

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            payload = message.get("bytes")
            if payload is None:
                # Текстові повідомлення — службові: роздільність екрана та
                # статистика. Роздільність важлива окремо: без неї панель не
                # може перерахувати клік у координати пристрою, тож віддалене
                # керування лишається недоступним.
                text = message.get("text")
                if text:
                    await _handle_device_message(session_id, text)
                continue

            if len(payload) > MAX_FRAME_BYTES:
                await websocket.send_json({"type": "error", "message": "frame_too_large"})
                continue

            frames += 1
            total_bytes += len(payload)
            await registry.send_bytes(viewer_key(session_id), payload)

            now = time.monotonic()
            if now - last_presence >= support_service.PRESENCE_REFRESH_SECONDS:
                await support_service.mark_device_online(redis, session_id)
                last_presence = now
            if now - last_flush >= STATS_FLUSH_SECONDS:
                hint = support_service.quality_hint(total_bytes, now - started)
                await websocket.send_json({"type": "quality", **hint})

                async with factory() as db:
                    row = await db.get(RemoteSession, session_id)
                    if row is None or row.state not in support_service.OPEN_STATES:
                        break
                    await support_service.touch_frames(
                        row,
                        frames=frames,
                        size_bytes=total_bytes,
                        stats={
                            "kbps": round((total_bytes * 8 / 1000) / max(now - started, 1), 1),
                            **hint,
                        },
                    )
                    await db.commit()
                    await support_service.notify(redis, "support_session_stats", row)

                frames = 0
                total_bytes = 0
                started = now
                last_flush = now

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Помилка каналу пристрою", extra={"error": str(exc)})
    finally:
        await registry.remove(key, websocket)
        await support_service.mark_device_offline(redis, session_id)
        async with factory() as db:
            row = await db.get(RemoteSession, session_id)
            if row is not None:
                if frames:
                    await support_service.touch_frames(
                        row, frames=frames, size_bytes=total_bytes
                    )
                await db.commit()
        # Глядачам повідомляємо, що потік урвався. Сам сеанс при цьому не
        # закривається: агент має право перепідключитися, а якщо кадри так і
        # не поновляться — його закриє фонова перевірка за FRAME_SILENCE.
        await registry.send(viewer_key(session_id), {"type": "stream_ended"})


async def _handle_device_message(session_id: uuid.UUID, text: str) -> None:
    """Розбирає службове текстове повідомлення від агента."""
    try:
        message = json.loads(text)
    except (TypeError, ValueError):
        return
    if not isinstance(message, dict):
        return

    if message.get("type") != "metrics":
        logger.debug("Службове повідомлення сеансу", extra={"text": text[:200]})
        return

    factory = get_sessionmaker()
    async with factory() as db:
        row = await db.get(RemoteSession, session_id)
        if row is None:
            return
        if support_service.apply_device_metrics(row, message):
            await db.commit()
            # Панель має дізнатися розміри одразу — інакше перші кліки
            # оператора нікуди не підуть.
            await registry.send(
                viewer_key(session_id),
                {
                    "type": "metrics",
                    "width": row.frame_width,
                    "height": row.frame_height,
                },
            )


# -------------------------------------------------------------- оператор --


async def _viewer_state(session_id: uuid.UUID, user_id: uuid.UUID) -> dict | None:
    """Свіжий стан сеансу й прав оператора.

    Читається з БД, а не з копії, знятої під час підключення: інакше
    вимкнення керування чи завершення сеансу не діяли б до перепідключення.
    """
    factory = get_sessionmaker()
    async with factory() as db:
        row = await db.get(RemoteSession, session_id)
        if row is None or row.state not in support_service.OPEN_STATES:
            return None
        if row.consent_at is None:
            return None
        user = await db.get(User, user_id)
        if user is None or not user.is_active:
            return None
        if not role_has_permission(user.role, Permission.SUPPORT_SESSION):
            return None
        return {
            "row": row,
            "input_enabled": row.input_enabled,
            # Керувати можна лише коли пристрій підтвердив підтримку і
            # відома роздільність кадру — інакше координати нема з чого рахувати.
            "input_ready": bool(
                row.input_enabled
                and row.input_available
                and row.frame_width
                and row.frame_height
            ),
            "frame_width": row.frame_width,
            "frame_height": row.frame_height,
            "device_online": registry.count(device_key(session_id)) > 0,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "device_id": str(row.device_id),
            "state": row.state,
        }


async def _run_viewer(
    websocket: WebSocket,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    user_label: str,
    device_id: uuid.UUID,
    redis,
) -> None:
    key = viewer_key(session_id)
    await registry.add(key, websocket)
    await support_service.mark_viewer_online(redis, session_id, user_id)

    cached: dict | None = None
    cached_at = 0.0
    # Дії керування рахуємо й пишемо в аудит однією пачкою при від'єднанні:
    # окремий запис на кожен тап зробив би журнал нечитабельним.
    input_count = 0
    input_actions: list[str] = []

    async def state() -> dict | None:
        """Стан із коротким кешем — щоб не ходити в БД на кожну подію миші."""
        nonlocal cached, cached_at
        now = time.monotonic()
        if cached is None or now - cached_at >= VIEWER_STATE_TTL_SECONDS:
            cached = await _viewer_state(session_id, user_id)
            cached_at = now
        return cached

    async def refresh_presence() -> None:
        """Оператор здебільшого лише дивиться, тож присутність оновлюємо самі."""
        while True:
            await asyncio.sleep(support_service.PRESENCE_REFRESH_SECONDS)
            await support_service.mark_viewer_online(redis, session_id, user_id)

    presence_task = asyncio.create_task(refresh_presence())

    try:
        current = await state()
        if current is None:
            await websocket.close(code=4403, reason="session_unavailable")
            return

        await websocket.send_json(
            {
                "type": "joined",
                "session_id": str(session_id),
                "device_id": str(device_id),
                "input_enabled": current["input_enabled"],
                "input_ready": current["input_ready"],
                "streaming": current["device_online"],
                "frame_width": current["frame_width"],
                "frame_height": current["frame_height"],
                "expires_at": current["expires_at"],
            }
        )

        while True:
            raw = await websocket.receive_text()
            if len(raw) > MAX_CONTROL_FRAME_BYTES:
                await websocket.send_json({"type": "error", "message": "frame_too_large"})
                continue
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            kind = str(message.get("type", ""))

            if kind == "ping":
                # Пінг заодно підтверджує присутність оператора в каналі.
                await support_service.mark_viewer_online(redis, session_id, user_id)
                await websocket.send_json({"type": "pong"})
                continue

            current = await state()
            if current is None:
                # Сеанс закрито, права знято або обліковий запис вимкнено.
                await websocket.close(code=4403, reason="session_revoked")
                return

            if kind == "input":
                # Керування працює лише якщо адміністратор його залишив
                # увімкненим саме зараз, пристрій підтвердив підтримку
                # (служба доступності) і відома роздільність кадру.
                if not current["input_enabled"]:
                    await websocket.send_json({"type": "error", "message": "input_disabled"})
                    continue
                if not current["input_ready"]:
                    await websocket.send_json(
                        {"type": "error", "message": "input_unavailable"}
                    )
                    continue

                clean = support_service.sanitize_input(message, current["row"])
                if clean is None:
                    await websocket.send_json({"type": "error", "message": "bad_input"})
                    continue

                delivered = await registry.send(device_key(session_id), clean)
                if delivered:
                    input_count += 1
                    input_actions.append(str(clean["action"]))
            elif kind == "quality":
                await registry.send(device_key(session_id), _sanitize_quality(message))

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("Помилка каналу оператора", extra={"error": str(exc)})
    finally:
        presence_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await presence_task
        await registry.remove(key, websocket)
        await support_service.mark_viewer_offline(redis, session_id, user_id)
        await _finish_viewer(session_id, user_id, user_label, input_count, input_actions)


async def _finish_viewer(
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    user_label: str,
    input_count: int,
    input_actions: list[str],
) -> None:
    """Фіксує від'єднання оператора та підсумок його дій керування."""
    factory = get_sessionmaker()
    async with factory() as db:
        row = await db.get(RemoteSession, session_id)
        if row is None:
            return
        user = await db.get(User, user_id)

        if input_count:
            row.input_events += input_count
            if user is not None:
                await support_service.record_input_batch(
                    db, row=row, user=user, count=input_count, actions=input_actions
                )

        await record_audit(
            db,
            action=AuditAction.REMOTE_SESSION_VIEWER_LEFT,
            user=user,
            actor_label=None if user else user_label,
            object_type="remote_session",
            object_id=row.id,
            details={"input_events": input_count, "device_id": str(row.device_id)},
        )
        await db.commit()


def _sanitize_quality(message: dict) -> dict:
    """Пропускаємо лише відомі числові параметри в межах розумного."""
    bounds = {"max_fps": (1, 30), "scale_percent": (10, 100), "jpeg_quality": (20, 95)}
    clean: dict[str, object] = {"type": "quality"}
    for field, (low, high) in bounds.items():
        value = message.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            clean[field] = max(low, min(value, high))
    return clean


# ------------------------------------------------------------ авторизація -


async def _authenticate_device(db, token: str) -> Device | None:
    try:
        claims = decode_token(token, expected_type=TOKEN_TYPE_DEVICE)
        device_id = uuid.UUID(str(claims["sub"]))
    except (TokenError, KeyError, ValueError):
        return None

    device = await db.get(Device, device_id)
    if device is None or device.deleted_at is not None or device.status != "active":
        return None
    if token_predates(claims, as_utc(device.tokens_valid_from)):
        return None
    return device


async def _authenticate_user(db, token: str) -> User | None:
    try:
        claims = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
        user_id = uuid.UUID(str(claims["sub"]))
    except (TokenError, KeyError, ValueError):
        return None

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    if token_predates(claims, as_utc(user.sessions_valid_from)):
        return None
    return user
