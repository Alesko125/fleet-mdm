"""Керування застосунками на пристрої: інвентар і дії над пакетами.

Ці ендпоїнти лишаються спеціалізованими, бо роблять доменні перевірки, яких
не знає загальний реєстр: сумісність SDK, пониження версії, системні пакети.
Саму ж команду вони створюють через `command_service.issue`, тож чинна
матриця дозволів і схеми параметрів однакові тут і в
`POST /devices/{id}/commands`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.config import get_settings
from app.core.errors import ConflictError, ValidationFailedError
from app.core.rbac import Permission
from app.core.redis_client import get_redis
from app.db.models import User
from app.db.session import get_session
from app.schemas.app import (
    DeviceAppWithRepository,
    InstallRequest,
    PackageActionRequest,
    StopAppRequest,
    UninstallRequest,
)
from app.schemas.command import CommandOut
from app.services import apk_repository, command_registry
from app.services import commands as command_service
from app.services import device as device_service

router = APIRouter(prefix="/devices/{device_id}/apps", tags=["applications"])

# Пакет самого агента: зупиняти його командою APP_STOP заборонено.
AGENT_PACKAGE = "ua.fleet.agent"


def _download_url(apk_id: uuid.UUID) -> str:
    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    return f"{base}{settings.api_prefix}/device/apks/{apk_id}/download"


@router.get("", response_model=list[DeviceAppWithRepository])
async def list_device_apps(
    device_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.APP_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[DeviceAppWithRepository]:
    """Встановлені застосунки з порівнянням версій із репозиторієм."""
    device = await device_service.get_device(session, device_id)
    apps = await device_service.installed_apps(session, device)
    latest = await apk_repository.latest_by_package(
        session, [app.package_name for app in apps]
    )

    result: list[DeviceAppWithRepository] = []
    for app in apps:
        item = DeviceAppWithRepository.model_validate(app)
        artifact = latest.get(app.package_name)
        if artifact is not None:
            item.in_repository = True
            item.repository_apk_id = artifact.id
            item.repository_version_name = artifact.version_name
            item.repository_version_code = artifact.version_code
            item.update_available = (
                artifact.version_code is not None
                and app.version_code is not None
                and artifact.version_code > app.version_code
            )
        result.append(item)
    return result


@router.post("/refresh", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def refresh_app_list(
    request: Request,
    device_id: uuid.UUID,
    # Конкретне право перевіряє реєстр: базовий дозвіл лише відсікає сторонніх.
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)

    command = await command_service.issue(
        session,
        device=device,
        command_type=command_registry.APP_LIST,
        params=None,
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.post("/install", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def install_app(
    request: Request,
    device_id: uuid.UUID,
    payload: InstallRequest,
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Встановлення або оновлення APK на **зареєстрованому** пристрої."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id, with_telemetry=True)
    artifact = await apk_repository.get_artifact(session, payload.apk_id)

    if not artifact.package_name:
        raise ValidationFailedError("У APK не визначено package name")

    installed = await device_service.find_installed(session, device, artifact.package_name)
    is_update = installed is not None

    if (
        is_update
        and installed.version_code is not None
        and artifact.version_code is not None
        and artifact.version_code < installed.version_code
        and not payload.allow_downgrade
    ):
        raise ConflictError(
            f"На пристрої встановлено новішу версію ({installed.version_code}); "
            "щоб понизити — увімкніть allow_downgrade"
        )

    telemetry = device.telemetry
    if artifact.min_sdk and telemetry is not None and telemetry.sdk_int:
        if artifact.min_sdk > telemetry.sdk_int:
            raise ConflictError(
                f"APK вимагає Android SDK {artifact.min_sdk}, на пристрої — {telemetry.sdk_int}"
            )

    command_type = (
        command_registry.APP_UPDATE if is_update else command_registry.APP_INSTALL
    )
    command = await command_service.issue(
        session,
        device=device,
        command_type=command_type,
        params={
            "apk_id": str(artifact.id),
            "allow_downgrade": payload.allow_downgrade,
        },
        user=user,
        ip=ip,
        user_agent=user_agent,
        # Решта payload — довідка для агента, яку він не задає сам:
        # звідки качати, що очікувати й чим перевірити.
        extra_payload={
            "package_name": artifact.package_name,
            "version_name": artifact.version_name,
            "version_code": artifact.version_code,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "download_url": _download_url(artifact.id),
            "min_sdk": artifact.min_sdk,
            "abis": artifact.abis,
            "previous_version_code": installed.version_code if installed else None,
        },
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.post("/uninstall", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def uninstall_app(
    request: Request,
    device_id: uuid.UUID,
    payload: UninstallRequest,
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)

    installed = await device_service.find_installed(session, device, payload.package_name)
    if installed is not None and installed.is_system:
        raise ConflictError(
            "Системний застосунок не можна видалити; його можна лише вимкнути політикою"
        )

    command = await command_service.issue(
        session,
        device=device,
        command_type=command_registry.APP_UNINSTALL,
        params={"package_name": payload.package_name, "keep_data": payload.keep_data},
        user=user,
        ip=ip,
        user_agent=user_agent,
        extra_payload={
            "version_code": installed.version_code if installed else None,
        },
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.post("/launch", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def launch_app(
    request: Request,
    device_id: uuid.UUID,
    payload: PackageActionRequest,
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)

    command = await command_service.issue(
        session,
        device=device,
        command_type=command_registry.APP_LAUNCH,
        params={"package_name": payload.package_name},
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.post("/stop", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def stop_app(
    request: Request,
    device_id: uuid.UUID,
    payload: StopAppRequest,
    user: User = Depends(require_permission(Permission.DEVICE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Зупиняє застосунок штатними засобами Android.

    Дані застосунку не видаляються: команда лише завершує його процеси.
    Зупинити самого агента цим шляхом не можна — інакше адміністратор міг би
    зробити пристрій некерованим, а власника позбавити видимого індикатора.
    """
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)

    if payload.package_name == AGENT_PACKAGE:
        raise ConflictError(
            "Агента не можна зупинити цією командою — скористайтеся «Перезапустити агента»"
        )

    installed = await device_service.find_installed(session, device, payload.package_name)

    command = await command_service.issue(
        session,
        device=device,
        command_type=command_registry.APP_STOP,
        params={
            "package_name": payload.package_name,
            "clear_cache": payload.clear_cache,
        },
        user=user,
        ip=ip,
        user_agent=user_agent,
        extra_payload={"is_system": installed.is_system if installed else None},
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)
