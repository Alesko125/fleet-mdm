"""DTO правил моніторингу та сповіщень."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.common import ORMModel

RuleType = Literal[
    "DEVICE_OFFLINE",
    "LOW_BATTERY",
    "LOW_STORAGE",
    "LOW_MEMORY",
    "HIGH_CPU",
    "OUTDATED_AGENT",
    "OUTDATED_ANDROID",
    "OUTDATED_APP",
    "LOCATION_REPORTING_STOPPED",
    "COMMAND_FAILURES",
    "ENROLLMENT_STATUS",
]

Severity = Literal["info", "warning", "critical"]
AlertState = Literal["firing", "acknowledged", "resolved"]

# Які пороги допустимі для кожного типу правила і як їх перевіряти.
ALLOWED_THRESHOLDS: dict[str, dict[str, type]] = {
    "DEVICE_OFFLINE": {"offline_minutes": int},
    "LOW_BATTERY": {"battery_percent": int, "ignore_when_charging": bool},
    "LOW_STORAGE": {"free_percent": float, "free_mb": int},
    "LOW_MEMORY": {"free_percent": float},
    "HIGH_CPU": {"load_percent": float},
    "OUTDATED_AGENT": {"min_version": str},
    "OUTDATED_ANDROID": {"min_sdk": int},
    "OUTDATED_APP": {"packages": list},
    "LOCATION_REPORTING_STOPPED": {"silent_minutes": int},
    "COMMAND_FAILURES": {"count": int, "window_minutes": int},
    "ENROLLMENT_STATUS": {"states": list},
}

POSITIVE_KEYS = {
    "offline_minutes",
    "battery_percent",
    "free_percent",
    "free_mb",
    "load_percent",
    "min_sdk",
    "silent_minutes",
    "count",
    "window_minutes",
}


def validate_thresholds(rule_type: str, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Пороги мають відповідати типу правила — інакше правило не збережеться."""
    allowed = ALLOWED_THRESHOLDS.get(rule_type)
    if allowed is None:
        raise ValueError(f"Невідомий тип правила: {rule_type}")

    unknown = set(thresholds) - set(allowed)
    if unknown:
        raise ValueError(
            f"Для {rule_type} не підтримуються пороги: {', '.join(sorted(unknown))}"
        )

    cleaned: dict[str, Any] = {}
    for key, value in thresholds.items():
        if value is None:
            continue
        expected = allowed[key]
        if expected is float and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
        elif expected is int and isinstance(value, bool):
            raise ValueError(f"Поріг {key} має бути числом")
        elif not isinstance(value, expected):
            raise ValueError(f"Поріг {key} має тип {expected.__name__}")

        if key in POSITIVE_KEYS and value <= 0:
            raise ValueError(f"Поріг {key} має бути більшим за нуль")
        if key in ("battery_percent", "free_percent", "load_percent") and value > 100:
            raise ValueError(f"Поріг {key} не може перевищувати 100")
        cleaned[key] = value
    return cleaned


class AlertRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: RuleType
    severity: Severity = "warning"
    enabled: bool = True
    thresholds: dict[str, Any] = Field(default_factory=dict)
    device_id: uuid.UUID | None = None
    cooldown_seconds: int = Field(default=3600, ge=0, le=7 * 86_400)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check_thresholds(self) -> AlertRuleCreate:
        object.__setattr__(self, "thresholds", validate_thresholds(self.type, self.thresholds))
        return self


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    severity: Severity | None = None
    enabled: bool | None = None
    thresholds: dict[str, Any] | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0, le=7 * 86_400)
    description: str | None = Field(default=None, max_length=2000)


class AlertRuleOut(ORMModel):
    id: uuid.UUID
    name: str
    type: RuleType
    severity: Severity
    enabled: bool
    thresholds: dict[str, Any]
    device_id: uuid.UUID | None
    cooldown_seconds: int
    description: str | None
    created_by_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class AlertOut(ORMModel):
    id: uuid.UUID
    rule_id: uuid.UUID | None
    device_id: uuid.UUID
    type: RuleType
    severity: Severity
    state: AlertState
    dedup_key: str
    message: str
    details: dict[str, Any] | None
    first_seen_at: datetime
    last_seen_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by_id: uuid.UUID | None
    resolved_at: datetime | None


class AlertWithDevice(AlertOut):
    device_name: str | None = None


class EvaluationResult(BaseModel):
    devices: int
    rules: int
    raised: int
    resolved: int


class ThresholdSchema(BaseModel):
    """Довідник для UI: які пороги має кожен тип правила."""

    type: RuleType
    keys: dict[str, str]
    defaults: dict[str, Any]

    @field_validator("keys", mode="before")
    @classmethod
    def _stringify(cls, value: dict[str, Any]) -> dict[str, str]:
        return {key: getattr(kind, "__name__", str(kind)) for key, kind in value.items()}
