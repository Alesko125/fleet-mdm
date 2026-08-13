"""DTO комунікаційного модуля (SMS / журнал дзвінків).

Чутливий вміст (тексти, номери) з'являється у відповідях **лише** на
ендпоїнтах перегляду, захищених дозволом `comms:read`, і кожен такий перегляд
пишеться в аудит. Конфігураційні DTO вмісту не несуть узагалі.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

CommsPermission = Literal["unknown", "granted", "denied", "not_requested", "unavailable"]


class CommsSettingsOut(ORMModel):
    """Стан модуля — без жодного чутливого вмісту."""

    device_id: uuid.UUID
    enabled: bool
    sms_enabled: bool
    call_log_enabled: bool
    metadata_only: bool
    # Точний час останнього ввімкнення (вимога ТЗ).
    enabled_at: datetime | None
    enabled_by_id: uuid.UUID | None
    disabled_at: datetime | None
    retention_days: int
    sms_permission: CommsPermission
    call_log_permission: CommsPermission
    permission_reported_at: datetime | None
    last_sync_at: datetime | None
    updated_at: datetime


class CommsSettingsDetail(CommsSettingsOut):
    enabled_by_email: str | None = None
    # Чи налаштоване шифрування at-rest на сервері. Без нього модуль не
    # можна ввімкнути — панель показує це користувачу.
    encryption_ready: bool = True
    retention_max_days: int = 90


class CommsSettingsUpdate(BaseModel):
    """Зміна конфігурації модуля.

    `enabled=false` вимикає модуль повністю; агент перестає читати дані.
    Увімкнути можна лише коли на сервері налаштоване шифрування at-rest.
    """

    enabled: bool | None = None
    sms_enabled: bool | None = None
    call_log_enabled: bool | None = None
    metadata_only: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=365)


class CommsPermissionReport(BaseModel):
    """Агент повідомляє фактичний стан рантайм-дозволів на пристрої."""

    sms_permission: CommsPermission
    call_log_permission: CommsPermission


class AgentCommsConfig(BaseModel):
    """Конфігурація, яку сервер віддає агенту.

    Якщо `enabled=false`, агент нічого не збирає й не надсилає — незалежно
    від того, чи надані дозволи на пристрої.
    """

    enabled: bool
    collect_sms: bool
    collect_call_log: bool
    metadata_only: bool
    # Скільки останніх днів історії має сенс надсилати (у межах ретеншну).
    history_days: int
    max_batch: int
    # Текст для картки стану на пристрої — власник має бачити, що збирається.
    notice: str


class SmsIngestItem(BaseModel):
    provider_id: str | None = Field(default=None, max_length=64)
    direction: Literal["inbox", "sent", "draft", "outbox", "other"] = "other"
    address: str | None = Field(default=None, max_length=64)
    body: str | None = Field(default=None, max_length=8192)
    sent_at: datetime | None = None


class CallIngestItem(BaseModel):
    provider_id: str | None = Field(default=None, max_length=64)
    direction: Literal["incoming", "outgoing", "missed", "other"] = "other"
    number: str | None = Field(default=None, max_length=64)
    contact: str | None = Field(default=None, max_length=255)
    duration_seconds: int | None = Field(default=None, ge=0, le=86_400)
    occurred_at: datetime | None = None


class CommsBatch(BaseModel):
    """Пакет записів від агента."""

    sms: list[SmsIngestItem] = Field(default_factory=list, max_length=5000)
    calls: list[CallIngestItem] = Field(default_factory=list, max_length=5000)


class CommsBatchAccepted(BaseModel):
    sms_stored: int
    calls_stored: int
    rejected: int
    config: AgentCommsConfig


# ---------------------------------------------- перегляд (comms:read) ------


class SmsMessageOut(ORMModel):
    """Розшифроване SMS. Віддається лише за дозволом comms:read і з аудитом."""

    id: uuid.UUID
    direction: str
    address: str | None
    body: str | None
    body_length: int | None
    sent_at: datetime | None
    collected_at: datetime


class CallLogEntryOut(ORMModel):
    id: uuid.UUID
    direction: str
    number: str | None
    contact: str | None
    duration_seconds: int | None
    occurred_at: datetime | None
    collected_at: datetime
