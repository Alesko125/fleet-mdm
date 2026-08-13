"""DTO сеансів віддаленої підтримки."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

from app.schemas.common import ORMModel

SessionState = Literal["requested", "awaiting_consent", "active", "denied", "ended", "expired"]

# Стани, у яких сеанс іще «живий».
OPEN_STATES = ("requested", "awaiting_consent", "active")


class SupportSessionCreate(BaseModel):
    """Причина обовʼязкова: користувач побачить її в діалозі згоди."""

    reason: str = Field(min_length=3, max_length=500)
    # Віддалене керування вмикається окремо і лише там, де його підтримує
    # Android (увімкнена служба доступності на пристрої).
    input_enabled: bool = False
    max_duration_minutes: int = Field(default=30, ge=1, le=240)


class SupportSessionOut(ORMModel):
    id: uuid.UUID
    device_id: uuid.UUID
    requested_by_id: uuid.UUID | None
    state: SessionState
    transport: str
    reason: str | None
    input_enabled: bool
    input_available: bool
    consent_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    end_reason: str | None
    expires_at: datetime | None
    last_frame_at: datetime | None
    frames_sent: int
    bytes_sent: int
    # Роздільність кадру потрібна панелі, щоб перерахувати клік у координати
    # пристрою; поки вона невідома — керування недоступне.
    frame_width: int | None
    frame_height: int | None
    input_events: int
    viewer_joins: int
    last_viewer_at: datetime | None
    stats: dict[str, Any] | None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_seconds(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.ended_at or datetime.now(tz=self.started_at.tzinfo)
        return max(0, int((end - self.started_at).total_seconds()))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


class SupportSessionCreated(SupportSessionOut):
    """Секрет приєднання повертається рівно один раз."""

    join_secret: str
    viewer_url: str


class ConnectionStatus(BaseModel):
    """Хто зараз у каналі — з Redis, а не з здогадок клієнта."""

    device_online: bool = False
    viewers: int = 0
    streaming: bool = False
    seconds_since_frame: int | None = None
    frame_width: int | None = None
    frame_height: int | None = None


class SupportSessionDetail(SupportSessionOut):
    device_name: str | None = None
    requested_by_email: str | None = None
    connection: ConnectionStatus | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def input_ready(self) -> bool:
        """Чи можна зараз керувати: дозволено, підтримується і є роздільність."""
        return bool(
            self.input_enabled
            and self.input_available
            and self.frame_width
            and self.frame_height
        )


class ViewerTicket(BaseModel):
    """Одноразовий квиток на приєднання до перегляду.

    Видається окремо від `join_secret`, живе секунди й гасне при першому
    використанні — саме тому панель може безпечно перепідключатися й
    переживати перезавантаження вкладки.
    """

    session_id: uuid.UUID
    ticket: str
    expires_in: int
    viewer_url: str


class SessionAuditEntry(ORMModel):
    """Подія журналу, що стосується конкретного сеансу."""

    id: uuid.UUID
    action: str
    result: str
    actor_type: str
    actor_label: str | None
    ip: str | None
    details: dict[str, Any] | None
    occurred_at: datetime


class SessionConsent(BaseModel):
    """Відповідь користувача пристрою на запит показу екрана."""

    granted: bool
    input_available: bool = False
    reason: str | None = Field(default=None, max_length=200)


class SessionEnd(BaseModel):
    reason: str = Field(default="stopped", max_length=64)


class SessionStats(BaseModel):
    frames: int = Field(default=0, ge=0)
    bytes: int = Field(default=0, ge=0)
    fps: float | None = Field(default=None, ge=0, le=60)
    width: int | None = Field(default=None, ge=1, le=8192)
    height: int | None = Field(default=None, ge=1, le=8192)
    jpeg_quality: int | None = Field(default=None, ge=1, le=100)


class QualityHint(BaseModel):
    """Підказка пристрою, як підлаштувати потік під канал."""

    max_fps: int = Field(default=5, ge=1, le=30)
    scale_percent: int = Field(default=50, ge=10, le=100)
    jpeg_quality: int = Field(default=60, ge=10, le=95)
