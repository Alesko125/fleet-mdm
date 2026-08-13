"""DTO застосунків на пристрої та дій над ними."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

PACKAGE_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$"


class DeviceAppOut(ORMModel):
    package_name: str
    app_name: str | None = None
    version_name: str | None = None
    version_code: int | None = None
    is_system: bool = False
    is_enabled: bool = True
    installer_package: str | None = None
    first_installed_at: datetime | None = None
    last_updated_at: datetime | None = None
    last_seen_at: datetime | None = None


class DeviceAppWithRepository(DeviceAppOut):
    """Порівняння встановленої версії з найсвіжішою в репозиторії."""

    repository_apk_id: uuid.UUID | None = None
    repository_version_name: str | None = None
    repository_version_code: int | None = None
    update_available: bool = False
    in_repository: bool = False


class AppInventoryItem(BaseModel):
    package_name: str = Field(max_length=255, pattern=PACKAGE_PATTERN)
    app_name: str | None = Field(default=None, max_length=255)
    version_name: str | None = Field(default=None, max_length=64)
    version_code: int | None = Field(default=None, ge=0)
    is_system: bool = False
    is_enabled: bool = True
    installer_package: str | None = Field(default=None, max_length=255)
    first_installed_at: datetime | None = None
    last_updated_at: datetime | None = None


class AppInventoryReport(BaseModel):
    apps: list[AppInventoryItem] = Field(max_length=5000)
    collected_at: datetime | None = None


class StopAppRequest(BaseModel):
    """Зупинка застосунку. Дані застосунку не видаляються."""

    package_name: str = Field(max_length=255, pattern=PACKAGE_PATTERN)
    # Очищення кешу — штатний API; дані користувача не чіпаються.
    clear_cache: bool = False


class InstallRequest(BaseModel):
    apk_id: uuid.UUID
    # Дозволяє понизити версію лише за явної згоди адміністратора.
    allow_downgrade: bool = False


class PackageActionRequest(BaseModel):
    package_name: str = Field(max_length=255, pattern=PACKAGE_PATTERN)


class UninstallRequest(PackageActionRequest):
    keep_data: bool = False
