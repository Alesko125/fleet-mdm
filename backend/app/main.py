"""Точка входу FastAPI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api.v1 import api_router
from app.api.v1.health import router as health_router
from app.core.config import get_settings
from app.core.errors import install_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import RequestGuardMiddleware, SecurityHeadersMiddleware
from app.core.redis_client import create_redis, ping_redis
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.services.bootstrap import ensure_bootstrap_admin
from app.services.monitoring_loop import monitoring_loop
from app.services.retention import retention_loop
from app.ws.routes import router as ws_router
from app.ws.support import router as support_ws_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    logger.info(
        "Старт застосунку", extra={"version": __version__, "environment": settings.app_env}
    )

    init_engine()
    app.state.redis = await create_redis(settings.redis_url)
    if not await ping_redis(app.state.redis):
        logger.warning("Redis недоступний на старті — обмеження частоти працюватиме в degraded")

    try:
        factory = get_sessionmaker()
        async with factory() as session:
            await ensure_bootstrap_admin(session, settings)
    except Exception as exc:  # noqa: BLE001 — БД може бути ще не мігрована
        logger.warning("Не вдалося виконати bootstrap", extra={"error": str(exc)})

    # Фонове прибирання прострочених даних (геолокація, телеметрія, аудит).
    stop_event = asyncio.Event()
    app.state.retention_stop = stop_event
    retention_task = asyncio.create_task(retention_loop(get_sessionmaker(), stop_event))
    monitoring_task = asyncio.create_task(
        monitoring_loop(get_sessionmaker(), lambda: getattr(app.state, "redis", None), stop_event)
    )

    try:
        yield
    finally:
        stop_event.set()
        retention_task.cancel()
        monitoring_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task
        with contextlib.suppress(asyncio.CancelledError):
            await monitoring_task
        if getattr(app.state, "redis", None) is not None:
            try:
                await app.state.redis.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не вдалося закрити Redis", extra={"error": str(exc)})
        await dispose_engine()
        logger.info("Застосунок зупинено")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Self-hosted платформа керування Android-пристроями, "
            "зареєстрованими їхнім власником. Агент завжди видимий користувачу."
        ),
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Порядок важливий: заголовки безпеки додаються останніми, тож
    # цей middleware реєструється першим і обгортає решту.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        # Явні переліки замість "*": з allow_credentials=True підстановка
        # будь-якого заголовка чи методу зайво розширює поверхню атаки.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            settings.csrf_header_name,
            "X-Chunk-Offset",
            "Range",
        ],
        expose_headers=["X-Request-ID", "X-Checksum-SHA256", "Content-Range"],
        max_age=600,
    )
    app.add_middleware(RequestGuardMiddleware)
    if settings.allowed_host_list:
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list
        )

    install_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)
    app.include_router(ws_router)
    app.include_router(support_ws_router)

    return app


app = create_app()
