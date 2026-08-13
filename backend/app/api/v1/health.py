"""Health / readiness."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.deps import require_permission
from app.core.config import get_settings
from app.core.rbac import Permission
from app.core.redis_client import ping_redis
from app.db.models import User
from app.db.session import get_session
from app.schemas.common import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request, session: AsyncSession = Depends(get_session)) -> HealthResponse:
    """Стан сервісу.

    У production назовні віддається лише сам факт «живий / деградований»:
    версія збірки й розклад залежностей — це підказка для розвідки, а для
    моніторингу достатньо статусу. Повна картина доступна автентифікованому
    оператору через `/health/details`.
    """
    settings = get_settings()
    db_ok, redis_ok = await _probe(request, session)

    if settings.is_production:
        return HealthResponse(status="ok" if db_ok else "degraded")

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=__version__,
        environment=settings.app_env,
        database=db_ok,
        redis=redis_ok,
    )


@router.get("/health/details", response_model=HealthResponse)
async def health_details(
    request: Request,
    _: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> HealthResponse:
    """Повний стан — лише для оператора з правом на керування налаштуваннями."""
    settings = get_settings()
    db_ok, redis_ok = await _probe(request, session)
    return HealthResponse(
        status="ok" if db_ok and redis_ok else "degraded",
        version=__version__,
        environment=settings.app_env,
        database=db_ok,
        redis=redis_ok,
    )


async def _probe(request: Request, session: AsyncSession) -> tuple[bool, bool]:
    db_ok = True
    try:
        await session.execute(sa.text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    redis_ok = await ping_redis(getattr(request.app.state, "redis", None))
    return db_ok, redis_ok


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}
