"""DTO аудит-журналу."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.schemas.common import ORMModel


class AuditLogOut(ORMModel):
    id: uuid.UUID
    occurred_at: datetime
    actor_type: str
    actor_user_id: uuid.UUID | None
    actor_device_id: uuid.UUID | None
    actor_label: str | None
    action: str
    object_type: str | None
    object_id: str | None
    result: str
    ip: str | None
    user_agent: str | None
    details: dict[str, Any] | None
