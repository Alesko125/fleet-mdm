"""WebSocket-ендпоїнти.

M1: канал пристрою (присутність + heartbeat + приймання команд).
Обробка конкретних типів команд додається у M2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.config import get_settings
from app.core.rbac import Permission, role_has_permission
from app.core.redis_client import (
    ADMIN_CHANNEL,
    clear_presence,
    device_channel,
    get_redis_ws,
    set_presence,
)
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_DEVICE,
    TokenError,
    as_utc,
    decode_token,
    token_predates,
    utcnow,
)
from app.db.models import Device, User
from app.db.session import get_sessionmaker
from app.ws.manager import registry, relay_channel

logger = logging.getLogger(__name__)
router = APIRouter()


def origin_allowed(websocket: WebSocket) -> bool:
    """Перевірка заголовка Origin для браузерних каналів.

    Токен передається параметром URL, а не cookie, тож класичного CSWSH тут
    немає — але явна перевірка джерела прибирає цілий клас помилок, якщо
    схема автентифікації колись зміниться. Агент Origin не надсилає взагалі,
    тож його з'єднання ця перевірка не зачіпає.
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    allowed = get_settings().cors_origin_list
    return origin in allowed


async def _authenticate(token: str) -> tuple[uuid.UUID, dict] | None:
    try:
        claims = decode_token(token, expected_type=TOKEN_TYPE_DEVICE)
        return uuid.UUID(str(claims["sub"])), claims
    except (TokenError, KeyError, ValueError):
        return None


@router.websocket("/ws/device")
async def device_socket(websocket: WebSocket, token: str = Query(..., max_length=4096)) -> None:
    settings = get_settings()
    authenticated = await _authenticate(token)
    if authenticated is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    device_id, claims = authenticated

    factory = get_sessionmaker()
    async with factory() as session:
        device = await session.get(Device, device_id)
        if device is None or device.deleted_at is not None or device.status != "active":
            await websocket.close(code=4403, reason="device_unavailable")
            return
        # Ротація ключа чи призупинення пристрою вбивають і цей канал.
        if token_predates(claims, as_utc(device.tokens_valid_from)):
            await websocket.close(code=4401, reason="token_revoked")
            return
        device.last_seen_at = utcnow()
        await session.commit()

    await websocket.accept()
    key = str(device_id)
    redis = get_redis_ws(websocket)
    ttl = settings.device_offline_after_seconds

    await registry.add(key, websocket)
    await set_presence(redis, key, ttl)
    relay_task = asyncio.create_task(relay_channel(redis, device_channel(key), websocket))

    try:
        await websocket.send_json({"type": "hello", "device_id": key, "server_time": str(utcnow())})
        while True:
            message = await websocket.receive_json()
            msg_type = str(message.get("type", ""))
            if msg_type == "heartbeat":
                await set_presence(redis, key, ttl)
                async with factory() as session:
                    device = await session.get(Device, device_id)
                    if device is not None:
                        device.last_seen_at = utcnow()
                        await session.commit()
                await websocket.send_json({"type": "heartbeat_ack", "server_time": str(utcnow())})
            else:
                # Типи команд (ack, result, telemetry) з'являться у M2.
                logger.info("WS повідомлення", extra={"device_id": key, "msg_type": msg_type})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Помилка WS-з'єднання", extra={"device_id": key, "error": str(exc)})
    finally:
        relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay_task
        await registry.remove(key, websocket)
        with contextlib.suppress(Exception):
            await clear_presence(redis, key)


@router.websocket("/ws/admin")
async def admin_socket(websocket: WebSocket, token: str = Query(..., max_length=4096)) -> None:
    """Живі оновлення для панелі: алерти, прогрес команд, події пристроїв.

    Канал лише для читання — надіслані клієнтом повідомлення ігноруються,
    щоб через сокет не можна було виконати жодної дії в обхід REST і RBAC.
    """
    if not origin_allowed(websocket):
        await websocket.close(code=4403, reason="origin_not_allowed")
        return

    authenticated = await _authenticate_user(token)
    if authenticated is None:
        await websocket.close(code=4401, reason="unauthorized")
        return
    user_id, claims = authenticated

    factory = get_sessionmaker()
    async with factory() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            await websocket.close(code=4403, reason="account_unavailable")
            return
        # Вихід, зміна ролі чи пароля мають закривати живий сокет.
        if token_predates(claims, as_utc(user.sessions_valid_from)):
            await websocket.close(code=4401, reason="session_revoked")
            return
        if not role_has_permission(user.role, Permission.DEVICE_READ):
            await websocket.close(code=4403, reason="forbidden")
            return

    await websocket.accept()
    key = f"admin:{user_id}"
    redis = get_redis_ws(websocket)

    await registry.add(key, websocket)
    relay_task = asyncio.create_task(relay_channel(redis, ADMIN_CHANNEL, websocket))

    try:
        await websocket.send_json({"type": "hello", "channel": "admin"})
        while True:
            # Тримаємо зʼєднання; вхідні повідомлення свідомо не обробляємо.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("Помилка WS панелі", extra={"error": str(exc)})
    finally:
        relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay_task
        await registry.remove(key, websocket)


async def _authenticate_user(token: str) -> tuple[uuid.UUID, dict] | None:
    try:
        claims = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
        return uuid.UUID(str(claims["sub"])), claims
    except (TokenError, KeyError, ValueError):
        return None
