"""DTO команд пристрою."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field

from app.schemas.common import ORMModel


class CommandOut(ORMModel):
    """Повний запис команди.

    Містить усе, що потрібно для розслідування: хто видав, якому пристрою,
    коли, з якими параметрами, чим завершилося і скільки це тривало.
    """

    id: uuid.UUID
    device_id: uuid.UUID
    type: str
    status: str
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    progress_percent: int | None = None
    status_message: str | None = None
    attempts: int = 0
    created_by_id: uuid.UUID | None = None
    created_at: datetime
    dispatched_at: datetime | None = None
    acked_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    # Скільки команда чекала в черзі й скільки виконувалася на пристрої.
    # Рахується з серверних міток: годинник пристрою може розходитись.
    queue_ms: int | None = None
    duration_ms: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled", "expired")


class CommandDetail(CommandOut):
    """Команда разом із довідкою про її тип і виконавця."""

    title: str | None = None
    created_by_email: str | None = None
    device_name: str | None = None
    disruptive: bool = False
    requires_device_owner: bool = False


class CommandIssueRequest(BaseModel):
    """Видача команди з реєстру.

    `type` має бути в реєстрі, `params` — відповідати схемі саме цього типу.
    Довільного рядка для виконання на пристрої тут немає й бути не може.
    """

    type: str = Field(max_length=32)
    params: dict[str, Any] | None = None
    # Повторна видача з тим самим ключем поверне ту саму команду.
    idempotency_key: str | None = Field(default=None, max_length=64)


class CommandParamSpec(BaseModel):
    name: str
    type: str
    required: bool
    default: Any | None = None
    description: str | None = None


class CommandSpecOut(BaseModel):
    """Опис типу команди для панелі."""

    type: str
    title: str
    description: str
    # Що побачить власник пристрою, коли команда виконається.
    user_visible: str
    required_permission: str
    enabled: bool
    # Правило змінено адміністратором, а не взяте з реєстру.
    overridden: bool
    roles: list[str]
    # Чи має право видати цю команду той, хто запитує перелік.
    allowed_for_me: bool
    disruptive: bool
    requires_device_owner: bool
    ttl_seconds: int
    params: list[CommandParamSpec]


class CommandPermissionUpdate(BaseModel):
    required_permission: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


class CommandStats(BaseModel):
    """Зведення виконання за типом команди."""

    type: str
    total: int
    succeeded: int
    failed: int
    expired: int
    cancelled: int
    pending: int
    avg_duration_ms: int | None = None
    max_duration_ms: int | None = None


class PendingCommand(BaseModel):
    """Те, що бачить агент."""

    id: uuid.UUID
    type: str
    payload: dict[str, Any] | None = None
    created_at: datetime
    expires_at: datetime | None = None


class CommandProgressUpdate(BaseModel):
    percent: int = Field(ge=0, le=100)
    message: str | None = Field(default=None, max_length=255)


class CommandResultUpdate(BaseModel):
    status: str = Field(pattern=r"^(succeeded|failed)$")
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=2000)
