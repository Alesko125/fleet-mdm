"""DTO пристроїв та інвентаризації."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class TelemetryOut(ORMModel):
    manufacturer: str | None = None
    model: str | None = None
    product: str | None = None
    android_version: str | None = None
    sdk_int: int | None = None
    security_patch: str | None = None
    build_id: str | None = None
    kernel_version: str | None = None
    uptime_seconds: int | None = None
    battery_level: int | None = None
    battery_status: str | None = None
    battery_health: str | None = None
    battery_temperature_c: float | None = None
    charging: bool | None = None
    ram_total_bytes: int | None = None
    ram_available_bytes: int | None = None
    storage_total_bytes: int | None = None
    storage_free_bytes: int | None = None
    network_type: str | None = None
    network_operator: str | None = None
    wifi_ssid: str | None = None
    ip_address: str | None = None
    agent_version: str | None = None
    collected_at: datetime | None = None


class DeviceOut(ORMModel):
    id: uuid.UUID
    name: str
    status: str
    management_mode: str
    agent_version: str | None
    android_id: str | None
    serial_number: str | None
    key_fingerprint: str
    enrolled_at: datetime
    last_seen_at: datetime | None
    last_ip: str | None
    notes: str | None


class DeviceDetail(DeviceOut):
    telemetry: TelemetryOut | None = None
    online: bool = False


class DeviceListItem(DeviceOut):
    online: bool = False
    battery_level: int | None = None
    model: str | None = None
    android_version: str | None = None


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, pattern=r"^(active|suspended|retired)$")
