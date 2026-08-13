"""Збірка роутерів API v1."""

from fastapi import APIRouter, Depends

from app.api.v1 import (
    apks,
    apps,
    audit,
    auth,
    commands,
    comms,
    device_agent,
    devices,
    enrollment,
    files,
    location,
    maintenance,
    monitoring,
    support,
    users,
)
from app.core.rate_limit import adaptive_rate_limit

# Ліміт частоти діє на весь API, а не лише на «важливі» ендпоїнти:
# інакше кожен новий маршрут доводилося б не забути обмежити вручну.
api_router = APIRouter(dependencies=[Depends(adaptive_rate_limit())])
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(enrollment.router)
api_router.include_router(device_agent.router)
api_router.include_router(devices.router)
api_router.include_router(apks.router)
api_router.include_router(apps.router)
api_router.include_router(files.router)
api_router.include_router(location.router)
api_router.include_router(monitoring.router)
api_router.include_router(support.router)
api_router.include_router(comms.router)
api_router.include_router(commands.router)
api_router.include_router(audit.router)
api_router.include_router(maintenance.router)

__all__ = ["api_router"]
