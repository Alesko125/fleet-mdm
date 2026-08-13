"""WebSocket-менеджер поверх Redis pub/sub.

Локальний реєстр з'єднань + Redis-канали, щоб кілька реплік API могли
доставляти команди пристрою, підключеному до будь-якої з них.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class ConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def add(self, key: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections[key].add(websocket)

    async def remove(self, key: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections[key].discard(websocket)
            if not self._connections[key]:
                self._connections.pop(key, None)

    async def send(self, key: str, message: dict[str, Any]) -> int:
        """Надсилає локально підключеним клієнтам; повертає кількість доставок."""
        async with self._lock:
            targets = list(self._connections.get(key, ()))
        delivered = 0
        for ws in targets:
            try:
                await ws.send_json(message)
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не вдалося надіслати у WS", extra={"key": key, "error": str(exc)})
        return delivered

    async def send_bytes(self, key: str, payload: bytes) -> int:
        """Бінарна розсилка — використовується для кадрів екрана."""
        async with self._lock:
            targets = list(self._connections.get(key, ()))
        delivered = 0
        for ws in targets:
            try:
                await ws.send_bytes(payload)
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("Кадр не доставлено", extra={"key": key, "error": str(exc)})
        return delivered

    def count(self, key: str) -> int:
        return len(self._connections.get(key, ()))


registry = ConnectionRegistry()


async def publish(redis: Redis, channel: str, message: dict[str, Any]) -> None:
    await redis.publish(channel, json.dumps(message, ensure_ascii=False, default=str))


async def relay_channel(redis: Redis, channel: str, websocket: WebSocket) -> None:
    """Пересилає повідомлення з Redis-каналу у конкретне WS-з'єднання."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for raw in pubsub.listen():
            if raw.get("type") != "message":
                continue
            try:
                payload = json.loads(raw["data"])
            except (TypeError, ValueError):
                continue
            await websocket.send_json(payload)
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
