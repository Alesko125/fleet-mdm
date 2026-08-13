"""DTO модуля геолокації.

Модуль opt-in: за замовчуванням вимкнений, вмикається адміністратором явно,
кожна зміна конфігурації потрапляє в аудит.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

PERMISSION_STATES = ("unknown", "granted_fine", "granted_coarse", "denied", "restricted")
LOCATION_SOURCES = ("periodic", "on_demand", "offline_queue")

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 86_400


class LocationSettingsOut(ORMModel):
    device_id: uuid.UUID
    enabled: bool
    interval_seconds: int
    min_distance_meters: int
    high_accuracy: bool
    permission_state: str
    background_permission: bool
    permission_reported_at: datetime | None = None
    last_fix_at: datetime | None = None
    consent_recorded_at: datetime | None = None
    updated_by_id: uuid.UUID | None = None
    updated_at: datetime


class LocationSettingsUpdate(BaseModel):
    """Часткове оновлення: передаються лише поля, які змінюються."""

    enabled: bool | None = None
    interval_seconds: int | None = Field(
        default=None, ge=MIN_INTERVAL_SECONDS, le=MAX_INTERVAL_SECONDS
    )
    min_distance_meters: int | None = Field(default=None, ge=0, le=10_000)
    high_accuracy: bool | None = None


class LocationFixOut(ORMModel):
    id: uuid.UUID
    device_id: uuid.UUID
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    altitude_m: float | None = None
    speed_mps: float | None = None
    bearing_deg: float | None = None
    provider: str | None = None
    source: str
    is_mock: bool
    battery_level: int | None = None
    recorded_at: datetime
    received_at: datetime


class LocationOverview(BaseModel):
    """Те, що показує сторінка геолокації пристрою."""

    settings: LocationSettingsOut
    current: LocationFixOut | None = None
    fix_count: int
    oldest_fix_at: datetime | None = None
    retention_days: int
    reporting_active: bool


# ------------------------------------------------------- дані від агента ---


class LocationFixIn(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100_000)
    altitude_m: float | None = Field(default=None, ge=-1000, le=20_000)
    speed_mps: float | None = Field(default=None, ge=0, le=1000)
    bearing_deg: float | None = Field(default=None, ge=0, le=360)
    provider: str | None = Field(default=None, max_length=32)
    source: str = Field(default="periodic", pattern=r"^(periodic|on_demand|offline_queue)$")
    is_mock: bool = False
    battery_level: int | None = Field(default=None, ge=0, le=100)
    recorded_at: datetime


class LocationBatchIn(BaseModel):
    """Пакет координат; офлайн-черга віддається одним запитом."""

    fixes: list[LocationFixIn] = Field(min_length=1, max_length=500)


class LocationPermissionReport(BaseModel):
    permission_state: str = Field(
        pattern=r"^(unknown|granted_fine|granted_coarse|denied|restricted)$"
    )
    background_permission: bool = False


class AgentLocationConfig(BaseModel):
    """Конфігурація, яку агент застосовує на пристрої."""

    enabled: bool
    interval_seconds: int
    min_distance_meters: int
    high_accuracy: bool
    retention_days: int
    server_time: datetime


class LocationBatchAccepted(BaseModel):
    accepted: int
    rejected: int
    config: AgentLocationConfig
