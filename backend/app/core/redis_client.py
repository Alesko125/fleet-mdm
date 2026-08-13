"""Клієнт Redis: присутність пристроїв, rate limit, nonce, pub/sub."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request, WebSocket
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


async def create_redis(url: str) -> Redis:
    client: Redis = Redis.from_url(url, encoding="utf-8", decode_responses=True)
    return client


async def ping_redis(client: Redis | None) -> bool:
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except Exception as exc:  # noqa: BLE001 — будь-яка помилка = недоступний Redis
        logger.warning("Redis недоступний", extra={"error": str(exc)})
        return False


def get_redis(request: Request) -> Redis:
    client: Redis | None = getattr(request.app.state, "redis", None)
    if client is None:  # pragma: no cover — можливо лише при некоректному старті
        raise RuntimeError("Redis не ініціалізовано")
    return client


def get_redis_ws(websocket: WebSocket) -> Redis:
    client: Redis | None = getattr(websocket.app.state, "redis", None)
    if client is None:  # pragma: no cover
        raise RuntimeError("Redis не ініціалізовано")
    return client


# ------------------------------------------------------------- ключі ------

def nonce_key(device_id: str, nonce: str) -> str:
    return f"devauth:nonce:{device_id}:{nonce}"


def presence_key(device_id: str) -> str:
    return f"device:presence:{device_id}"


def device_channel(device_id: str) -> str:
    return f"device:{device_id}"


ADMIN_CHANNEL = "admin:events"


def support_channel(session_id: str) -> str:
    return f"support:{session_id}"


async def set_presence(
    client: Redis,
    device_id: str,
    ttl_seconds: int,
    payload: dict[str, Any] | None = None,
) -> None:
    await client.set(presence_key(device_id), "online", ex=ttl_seconds)
    if payload:
        meta_key = f"{presence_key(device_id)}:meta"
        await client.hset(meta_key, mapping={k: str(v) for k, v in payload.items()})
        await client.expire(meta_key, ttl_seconds)


async def clear_presence(client: Redis, device_id: str) -> None:
    await client.delete(presence_key(device_id), f"{presence_key(device_id)}:meta")


async def is_online(client: Redis, device_id: str) -> bool:
    return bool(await client.exists(presence_key(device_id)))
