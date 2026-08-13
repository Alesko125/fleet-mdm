"""DTO файлового модуля."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from app.schemas.common import ORMModel


class FileRootOut(ORMModel):
    id: uuid.UUID
    device_id: uuid.UUID | None
    path: str
    label: str | None = None
    writable: bool
    max_file_size_mb: int | None = None
    allow_privileged: bool
    created_by_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class FileRootCreate(BaseModel):
    path: str = Field(min_length=2, max_length=1024)
    label: str | None = Field(default=None, max_length=200)
    writable: bool = False
    max_file_size_mb: int | None = Field(default=None, ge=1, le=4096)
    # Привілейований (root) доступ лише для явно позначених каталогів
    # керування пристроєм. За замовчуванням вимкнено.
    allow_privileged: bool = False


class FileEntryOut(BaseModel):
    """Один запис у переліку каталогу (метадані, які надсилає агент)."""

    name: str
    path: str
    is_dir: bool = False
    size_bytes: int | None = None
    modified_at: datetime | None = None
    permissions: str | None = None
    is_symlink: bool = False
    mime_type: str | None = None
    readable: bool = True
    writable: bool = False


class FileListRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class FileNameRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=255)


class FileDeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    recursive: bool = False


class FileDownloadRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class FileTransferOut(ORMModel):
    id: uuid.UUID
    device_id: uuid.UUID
    command_id: uuid.UUID | None = None
    direction: str
    remote_path: str
    filename: str
    size_bytes: int | None = None
    received_bytes: int
    chunk_size: int
    sha256: str | None = None
    checksum_verified: bool
    status: str
    error: str | None = None
    created_by_id: uuid.UUID | None = None
    created_at: datetime
    completed_at: datetime | None = None
    expires_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress_percent(self) -> int | None:
        if not self.size_bytes:
            return 100 if self.status == "completed" else None
        return min(100, int(self.received_bytes * 100 / self.size_bytes))


# --------------------------------------------------------- агент → сервер --


class TransferProgressIn(BaseModel):
    """Прогрес передачі, коли файл іде із сервера на пристрій."""

    received_bytes: int = Field(ge=0)


class TransferCompleteIn(BaseModel):
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    size_bytes: int | None = Field(default=None, ge=0)


class TransferFailIn(BaseModel):
    error: str = Field(min_length=1, max_length=2000)


class TransferStatusOut(BaseModel):
    """Те, що бачить агент: скільки вже прийнято і куди продовжувати."""

    id: uuid.UUID
    direction: str
    remote_path: str
    filename: str
    status: str
    size_bytes: int | None
    received_bytes: int
    chunk_size: int
    sha256: str | None
    expires_at: datetime | None
