"""DTO репозиторію APK."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from app.schemas.common import ORMModel


class ApkOut(ORMModel):
    id: uuid.UUID
    filename: str
    label: str | None = None
    package_name: str | None = None
    version_name: str | None = None
    version_code: int | None = None
    size_bytes: int
    sha256: str
    signature_digest: str | None = None
    signature_schemes: list[str] | None = None
    min_sdk: int | None = None
    target_sdk: int | None = None
    compile_sdk: int | None = None
    abis: list[str] | None = None
    permissions: list[str] | None = None
    is_split: bool = False
    notes: str | None = None
    uploaded_by_id: uuid.UUID | None = None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def architecture(self) -> str:
        return ", ".join(self.abis) if self.abis else "universal"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_version(self) -> str:
        name = self.version_name or "?"
        code = self.version_code if self.version_code is not None else "?"
        return f"{name} ({code})"


class ApkUploadResult(ApkOut):
    """`deduplicated=True` — такий самий файл уже був у репозиторії."""

    deduplicated: bool = False


class ApkUpdate(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)
    label: str | None = Field(default=None, max_length=255)
