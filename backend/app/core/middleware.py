"""Middleware безпеки: заголовки, обмеження запиту, контекст логування.

Винесено з `main.py` окремо, щоб політику можна було читати й тестувати
в одному місці.
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.core.errors import error_response
from app.core.logging import request_id_ctx

logger = logging.getLogger(__name__)

# Заголовки, які отримує кожна відповідь.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Permissions-Policy": (
        "geolocation=(), microphone=(), camera=(), usb=(), payment=(), "
        "interest-cohort=(), display-capture=()"
    ),
}

# Відповіді API містять дані пристроїв — кешувати їх не можна ніде.
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    "Expires": "0",
}

# Типи тіла, які приймає API.
ALLOWED_CONTENT_TYPES = (
    "application/json",
    "multipart/form-data",
    "application/x-www-form-urlencoded",
    "application/octet-stream",
)

# Шляхи, де тіло — це потік файлу, тож ліміт JSON до них не застосовується.
STREAMING_PATH_MARKERS = ("/files/", "/apks", "/chunk")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Request-ID, тайминг і повний набір заголовків безпеки."""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        request_id = _safe_request_id(request.headers.get("x-request-id"))
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = str(duration_ms)

        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        response.headers.setdefault(
            "Content-Security-Policy", settings.content_security_policy
        )
        for header, value in NO_STORE_HEADERS.items():
            response.headers.setdefault(header, value)
        if settings.use_secure_cookies:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains; preload",
            )

        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


class RequestGuardMiddleware(BaseHTTPMiddleware):
    """Базова валідація запиту до того, як його побачить застосунок.

    Перевіряє довжину тіла й тип вмісту: це відсікає як випадкові, так і
    навмисні спроби завалити процес великим JSON ще на вході.
    """

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()

        if request.method in ("POST", "PUT", "PATCH"):
            content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
            if content_type and not content_type.startswith(ALLOWED_CONTENT_TYPES):
                return error_response(
                    415,
                    "unsupported_media_type",
                    f"Непідтримуваний тип вмісту: {content_type}",
                )

            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError:
                    return error_response(
                        400, "bad_request", "Некоректний заголовок Content-Length"
                    )
                if length < 0:
                    return error_response(
                        400, "bad_request", "Некоректний заголовок Content-Length"
                    )
                limit = _body_limit(request.url.path, settings)
                if length > limit:
                    return error_response(
                        413,
                        "payload_too_large",
                        f"Тіло запиту більше за дозволені {limit // 1024} КБ",
                    )

        return await call_next(request)


def _body_limit(path: str, settings) -> int:
    """Потокові ендпоїнти мають власні ліміти; решта — компактний JSON."""
    if any(marker in path for marker in STREAMING_PATH_MARKERS):
        return settings.max_upload_bytes
    return settings.max_json_body_bytes


def _safe_request_id(raw: str | None) -> str:
    """Request-ID з заголовка потрапляє в логи — приймаємо лише безпечний.

    Без цього клієнт міг би вставити в лог довільний текст (log injection).
    """
    if raw:
        cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "-_")[:64]
        if cleaned:
            return cleaned
    return uuid.uuid4().hex[:16]


__all__ = [
    "NO_STORE_HEADERS",
    "SECURITY_HEADERS",
    "RequestGuardMiddleware",
    "SecurityHeadersMiddleware",
]
