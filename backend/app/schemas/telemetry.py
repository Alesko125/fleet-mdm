"""DTO телеметрії пристрою.

Збираються **лише показники стану самого пристрою**: заряд, памʼять,
сховище, навантаження CPU, версії ПЗ, тип мережі. Нічого, що стосується
особи користувача — ні SSID мережі, ні контактів, ні активності в
застосунках — сюди не потрапляє.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TelemetryIn(BaseModel):
    # --- ідентифікація пристрою й ПЗ ---
    manufacturer: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    product: str | None = Field(default=None, max_length=100)
    android_version: str | None = Field(default=None, max_length=32)
    sdk_int: int | None = Field(default=None, ge=1, le=100)
    security_patch: str | None = Field(default=None, max_length=32)
    build_id: str | None = Field(default=None, max_length=100)
    kernel_version: str | None = Field(default=None, max_length=255)
    agent_version: str | None = Field(default=None, max_length=32)

    # --- стан ---
    uptime_seconds: int | None = Field(default=None, ge=0)
    battery_level: int | None = Field(default=None, ge=0, le=100)
    battery_status: str | None = Field(default=None, max_length=32)
    battery_health: str | None = Field(default=None, max_length=32)
    battery_temperature_c: float | None = Field(default=None, ge=-50, le=150)
    charging: bool | None = None

    ram_total_bytes: int | None = Field(default=None, ge=0)
    ram_available_bytes: int | None = Field(default=None, ge=0)
    storage_total_bytes: int | None = Field(default=None, ge=0)
    storage_free_bytes: int | None = Field(default=None, ge=0)

    cpu_cores: int | None = Field(default=None, ge=1, le=512)
    cpu_load_percent: float | None = Field(default=None, ge=0, le=100)
    cpu_abi: str | None = Field(default=None, max_length=64)

    # Тип мережі — так, назва мережі (SSID) — ні: для моніторингу вона не
    # потрібна, а район перебування з неї вивести можна.
    network_type: str | None = Field(default=None, max_length=32)
    network_operator: str | None = Field(default=None, max_length=64)

    collected_at: datetime | None = None


class TelemetryAccepted(BaseModel):
    accepted: bool = True
    next_report_seconds: int
    alerts_raised: int = 0


class DeviceHealth(BaseModel):
    """Обчислені показники для індикаторів у панелі."""

    device_id: str
    online: bool
    battery_level: int | None = None
    charging: bool | None = None
    storage_free_percent: float | None = None
    ram_free_percent: float | None = None
    cpu_load_percent: float | None = None
    android_version: str | None = None
    agent_version: str | None = None
    location_reporting: str = "disabled"
    enrollment_status: str = "active"
    active_alerts: int = 0
    worst_severity: str | None = None
    last_seen_at: datetime | None = None
    collected_at: datetime | None = None


class MonitoringSummary(BaseModel):
    devices_total: int
    devices_online: int
    devices_offline: int
    alerts_firing: int
    alerts_by_severity: dict[str, int]
    alerts_by_type: dict[str, int]
    rules_enabled: int
