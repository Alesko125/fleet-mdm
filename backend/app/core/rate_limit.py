"""Rate limiting на Redis (фіксоване вікно).

login/enroll — fail-closed (без Redis доступ забороняється),
решта — fail-open, щоб збій кешу не поклав усю панель.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

import jwt
from fastapi import Request
from redis.asyncio import Redis

from app.core.config import get_settings, parse_rate_limit
from app.core.errors import RateLimitedError
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)


async def check_rate_limit(
    client: Redis, *, scope: str, identity: str, limit: int, window_seconds: int
) -> tuple[bool, int]:
    """Повертає `(дозволено, скільки секунд чекати)`."""
    now = int(time.time())
    bucket = now // window_seconds
    key = f"rl:{scope}:{identity}:{bucket}"
    pipe = client.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_seconds + 1)
    current, _ = await pipe.execute()
    if int(current) > limit:
        retry_after = window_seconds - (now % window_seconds)
        return False, max(retry_after, 1)
    return True, 0


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Шляхи з власними, суворішими лімітами — глобальний їх не перекриває.
_EXPLICITLY_LIMITED = ("/auth/login", "/auth/password", "/enroll", "/device/auth", "/device/key")


def client_ip(request: Request) -> str:
    """IP клієнта; X-Forwarded-For враховується лише від довіреного проксі."""
    settings = get_settings()
    direct = request.client.host if request.client else "unknown"
    trusted = settings.trusted_proxy_list
    if trusted and direct in trusted:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return direct


def rate_limit(
    scope: str,
    spec: str | None = None,
    *,
    fail_closed: bool = False,
    identity_fn: Callable[[Request], str] | None = None,
) -> Callable[[Request], Awaitable[None]]:
    """Фабрика FastAPI-залежності обмеження частоти."""

    async def dependency(request: Request) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        raw_spec = spec or getattr(settings, f"rate_limit_{scope}", settings.rate_limit_default)
        limit, window = parse_rate_limit(raw_spec)
        identity = identity_fn(request) if identity_fn else client_ip(request)
        try:
            client = get_redis(request)
            allowed, retry_after = await check_rate_limit(
                client, scope=scope, identity=identity, limit=limit, window_seconds=window
            )
        except RateLimitedError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Rate limiter недоступний",
                extra={"scope": scope, "fail_closed": fail_closed, "error": str(exc)},
            )
            if fail_closed:
                raise RateLimitedError(
                    "Служба обмеження частоти недоступна, спробуйте пізніше", retry_after=30
                ) from exc
            return
        if not allowed:
            logger.warning("Перевищено ліміт", extra={"scope": scope, "identity": identity})
            raise RateLimitedError(retry_after=retry_after)

    return dependency


def token_identity(request: Request) -> str:
    """Ключ обмеження: суб'єкт токена, якщо він є, інакше IP.

    Прив'язка до облікового запису/пристрою важлива у двох напрямках: офіс
    за одним NAT не ділить спільний кошик, а один скомпрометований токен не
    обходить ліміт, перемикаючи вихідні адреси.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        raw = header[7:].strip()
        try:
            # Підпис перевіряти тут не потрібно: некоректний токен усе одно
            # не пройде автентифікацію, а нам треба лише стабільний ключ.
            claims = jwt.decode(raw, options={"verify_signature": False})
            subject = str(claims.get("sub", ""))
            kind = str(claims.get("typ", "t"))
            if subject:
                return f"{kind}:{subject}"
        except Exception as exc:  # noqa: BLE001 — будь-яка проблема → падаємо на IP
            logger.debug("Не вдалося прочитати суб'єкта токена", extra={"error": str(exc)})
    return f"ip:{client_ip(request)}"


def adaptive_rate_limit() -> Callable[[Request], Awaitable[None]]:
    """Глобальний ліміт для всього API.

    Читання й зміни рахуються окремими кошиками: панель багато опитує, і
    один спільний ліміт довелося б робити настільки широким, що він перестав
    би стримувати зміни стану. Ендпоїнти з власними, суворішими лімітами
    (вхід, реєстрація, ротація ключа) цей ліміт не перекриває.
    """
    read_dependency = rate_limit("default", identity_fn=token_identity)
    write_dependency = rate_limit("mutation", identity_fn=token_identity)

    async def dependency(request: Request) -> None:
        path = request.url.path
        if any(marker in path for marker in _EXPLICITLY_LIMITED):
            return
        if request.method in SAFE_METHODS:
            await read_dependency(request)
        else:
            await write_dependency(request)

    return dependency
