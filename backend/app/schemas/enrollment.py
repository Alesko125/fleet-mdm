"""DTO реєстрації пристроїв."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel

MANAGEMENT_MODES = ("device_owner", "profile_owner", "unmanaged")


# ------------------------------------------------- токени (адмін-частина) --


class EnrollmentTokenCreate(BaseModel):
    label: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    max_uses: int = Field(default=1, ge=1, le=1000)
    ttl_seconds: int | None = Field(default=None, ge=300, le=30 * 86_400)


class EnrollmentTokenOut(ORMModel):
    id: uuid.UUID
    token_prefix: str
    label: str | None
    notes: str | None
    max_uses: int
    use_count: int
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime
    created_by_id: uuid.UUID | None


class EnrollmentTokenCreated(EnrollmentTokenOut):
    """Відкритий токен повертається рівно один раз — одразу після створення."""

    token: str
    enroll_url: str
    qr_payload: str


# ------------------------------------------------- реєстрація (агент) ------


class DeviceEnrollInfo(BaseModel):
    """Мінімальний набір даних, який агент надсилає під час реєстрації."""

    manufacturer: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    product: str | None = Field(default=None, max_length=100)
    android_version: str | None = Field(default=None, max_length=32)
    sdk_int: int | None = Field(default=None, ge=1, le=100)
    security_patch: str | None = Field(default=None, max_length=32)
    build_id: str | None = Field(default=None, max_length=100)
    kernel_version: str | None = Field(default=None, max_length=255)
    android_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]*$")
    serial_number: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]*$")
    management_mode: str = Field(default="unmanaged")
    agent_version: str | None = Field(default=None, max_length=32)
    display_name: str | None = Field(default=None, max_length=200)

    @field_validator("management_mode")
    @classmethod
    def _check_mode(cls, value: str) -> str:
        if value not in MANAGEMENT_MODES:
            raise ValueError(f"management_mode має бути одним із {MANAGEMENT_MODES}")
        return value


class DeviceEnrollRequest(BaseModel):
    token: str = Field(min_length=8, max_length=64)
    public_key: str = Field(min_length=40, max_length=4096, description="SPKI/DER у base64")
    device: DeviceEnrollInfo

    @field_validator("public_key")
    @classmethod
    def _check_public_key(cls, value: str) -> str:
        from app.core.security import load_device_public_key

        load_device_public_key(value)  # кидає ValueError, якщо ключ некоректний
        return value


class DeviceEnrollResponse(BaseModel):
    device_id: uuid.UUID
    device_name: str
    server_time: datetime
    api_base_url: str
    websocket_url: str
    heartbeat_interval_seconds: int
    management_mode: str
    transparency_notice: str


# ------------------------------------------------ автентифікація агента ---


class DeviceChallengeRequest(BaseModel):
    device_id: uuid.UUID


class DeviceChallengeResponse(BaseModel):
    nonce: str
    expires_at: datetime
    message_template: str = "fleet-device-auth:{device_id}:{nonce}"
    algorithm: str = "SHA256withECDSA"


class DeviceTokenRequest(BaseModel):
    device_id: uuid.UUID
    nonce: str = Field(min_length=16, max_length=128)
    signature: str = Field(min_length=32, max_length=1024, description="DER-підпис у base64")


class DeviceTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    # Підказка агенту: ключ застарів за політикою — час перевипустити.
    key_rotation_due: bool = False
    key_age_days: int = 0


# ------------------------------------------------ ротація ключа пристрою ---


class KeyRotationChallengeRequest(BaseModel):
    """Nonce для ротації видається лише автентифікованому пристрою."""


class KeyRotationChallengeResponse(BaseModel):
    nonce: str
    expires_at: datetime
    message_template: str = (
        "fleet-device-key-rotation:{device_id}:{sha256(new_public_key)}:{nonce}"
    )
    algorithm: str = "SHA256withECDSA"


class KeyRotationRequest(BaseModel):
    """Новий публічний ключ, підписаний **старим** ключем пристрою."""

    new_public_key: str = Field(
        min_length=40, max_length=4096, description="Новий SPKI/DER у base64"
    )
    nonce: str = Field(min_length=16, max_length=128)
    signature: str = Field(
        min_length=32, max_length=1024, description="DER-підпис старим ключем, base64"
    )
    reason: str = Field(default="scheduled", max_length=64, pattern=r"^[a-z0-9_]+$")

    @field_validator("new_public_key")
    @classmethod
    def _check_public_key(cls, value: str) -> str:
        from app.core.security import load_device_public_key

        load_device_public_key(value)
        return value


class KeyRotationResponse(BaseModel):
    device_id: uuid.UUID
    key_fingerprint: str
    key_generation: int
    rotated_at: datetime
    # Старий токен анульовано — агент має пройти challenge-response наново.
    reauthentication_required: bool = True


class DeviceKeyStatus(ORMModel):
    """Стан облікових даних пристрою для панелі."""

    device_id: uuid.UUID
    key_algorithm: str
    key_fingerprint: str
    key_generation: int
    key_created_at: datetime
    key_rotated_at: datetime | None
    key_age_days: int
    rotation_due: bool
    max_age_days: int


class DeviceKeyHistoryOut(ORMModel):
    id: uuid.UUID
    key_fingerprint: str
    key_algorithm: str
    generation: int
    reason: str
    retired_at: datetime
