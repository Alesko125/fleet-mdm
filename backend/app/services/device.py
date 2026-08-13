"""Спільні операції з пристроями та інвентарем застосунків."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import NotFoundError
from app.core.security import utcnow
from app.db.models import Device, DeviceApp
from app.schemas.app import AppInventoryItem


async def get_device(
    session: AsyncSession, device_id: uuid.UUID, *, with_telemetry: bool = False
) -> Device:
    stmt = sa.select(Device).where(Device.id == device_id, Device.deleted_at.is_(None))
    if with_telemetry:
        stmt = stmt.options(selectinload(Device.telemetry))
    device = (await session.execute(stmt)).scalar_one_or_none()
    if device is None:
        raise NotFoundError("Пристрій не знайдено")
    return device


async def installed_apps(session: AsyncSession, device: Device) -> list[DeviceApp]:
    return list(
        (
            await session.execute(
                sa.select(DeviceApp)
                .where(DeviceApp.device_id == device.id)
                .order_by(DeviceApp.is_system, DeviceApp.package_name)
            )
        ).scalars().all()
    )


async def find_installed(
    session: AsyncSession, device: Device, package_name: str
) -> DeviceApp | None:
    return (
        await session.execute(
            sa.select(DeviceApp).where(
                DeviceApp.device_id == device.id, DeviceApp.package_name == package_name
            )
        )
    ).scalar_one_or_none()


async def sync_inventory(
    session: AsyncSession, device: Device, items: list[AppInventoryItem]
) -> tuple[int, int, int]:
    """Оновлює перелік застосунків пристрою.

    Повертає `(додано, оновлено, видалено)`.
    """
    now = utcnow()
    existing = {app.package_name: app for app in await installed_apps(session, device)}
    reported: set[str] = set()
    added = updated = 0

    for item in items:
        reported.add(item.package_name)
        app = existing.get(item.package_name)
        if app is None:
            session.add(
                DeviceApp(
                    device_id=device.id,
                    package_name=item.package_name,
                    app_name=item.app_name,
                    version_name=item.version_name,
                    version_code=item.version_code,
                    is_system=item.is_system,
                    is_enabled=item.is_enabled,
                    installer_package=item.installer_package,
                    first_installed_at=item.first_installed_at,
                    last_updated_at=item.last_updated_at,
                    last_seen_at=now,
                )
            )
            added += 1
            continue

        changed = (
            app.version_code != item.version_code
            or app.version_name != item.version_name
            or app.is_enabled != item.is_enabled
            or app.app_name != item.app_name
        )
        app.app_name = item.app_name
        app.version_name = item.version_name
        app.version_code = item.version_code
        app.is_system = item.is_system
        app.is_enabled = item.is_enabled
        app.installer_package = item.installer_package
        app.first_installed_at = item.first_installed_at or app.first_installed_at
        app.last_updated_at = item.last_updated_at or app.last_updated_at
        app.last_seen_at = now
        if changed:
            updated += 1

    removed = 0
    for package_name, app in existing.items():
        if package_name not in reported:
            await session.delete(app)
            removed += 1

    device.last_seen_at = now
    return added, updated, removed
