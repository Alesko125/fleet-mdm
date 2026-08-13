"""Ендпоїнти, до яких звертається сам Android-агент."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_device, request_context
from app.core.config import get_settings
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.core.rate_limit import rate_limit
from app.core.redis_client import get_redis
from app.core.security import utcnow
from app.db.models import Command, Device, DeviceApp, FileRoot
from app.db.session import get_session
from app.schemas.app import AppInventoryReport
from app.schemas.command import (
    CommandProgressUpdate,
    CommandResultUpdate,
    PendingCommand,
)
from app.schemas.common import MessageResponse
from app.schemas.comms import (
    AgentCommsConfig,
    CommsBatch,
    CommsBatchAccepted,
    CommsPermissionReport,
)
from app.schemas.enrollment import (
    DeviceChallengeRequest,
    DeviceChallengeResponse,
    DeviceEnrollRequest,
    DeviceEnrollResponse,
    DeviceTokenRequest,
    DeviceTokenResponse,
    KeyRotationChallengeResponse,
    KeyRotationRequest,
    KeyRotationResponse,
)
from app.schemas.file import (
    FileRootOut,
    TransferCompleteIn,
    TransferFailIn,
    TransferProgressIn,
    TransferStatusOut,
)
from app.schemas.location import (
    AgentLocationConfig,
    LocationBatchAccepted,
    LocationBatchIn,
    LocationPermissionReport,
)
from app.schemas.support import SessionConsent, SessionEnd, SupportSessionOut
from app.schemas.telemetry import TelemetryAccepted, TelemetryIn
from app.services import (
    alerts as alert_service,
)
from app.services import (
    apk_repository,
    storage,
)
from app.services import (
    commands as command_service,
)
from app.services import (
    comms as comms_service,
)
from app.services import (
    device as device_service,
)
from app.services import (
    enrollment as enrollment_service,
)
from app.services import (
    files as file_service,
)
from app.services import (
    location as location_service,
)
from app.services import (
    support as support_service,
)
from app.services import (
    telemetry as telemetry_service,
)
from app.services.audit import RESULT_FAILURE, AuditAction, record_audit

router = APIRouter(tags=["device-agent"])


def _websocket_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :].rstrip("/") + "/ws/device"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :].rstrip("/") + "/ws/device"
    return base_url.rstrip("/") + "/ws/device"


@router.post(
    "/enroll",
    response_model=DeviceEnrollResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Реєстрація пристрою за одноразовим токеном",
    dependencies=[Depends(rate_limit("enroll", fail_closed=True))],
)
async def enroll(
    request: Request,
    payload: DeviceEnrollRequest,
    session: AsyncSession = Depends(get_session),
) -> DeviceEnrollResponse:
    settings = get_settings()
    ip, user_agent = request_context(request)
    device, _token = await enrollment_service.enroll_device(
        session, payload=payload, ip=ip, user_agent=user_agent
    )
    await session.commit()

    base = settings.public_base_url.rstrip("/")
    return DeviceEnrollResponse(
        device_id=device.id,
        device_name=device.name,
        server_time=utcnow(),
        api_base_url=f"{base}{settings.api_prefix}",
        websocket_url=_websocket_url(base),
        heartbeat_interval_seconds=max(30, settings.device_offline_after_seconds // 3),
        management_mode=device.management_mode,
        transparency_notice=enrollment_service.TRANSPARENCY_NOTICE,
    )


@router.post(
    "/device/auth/challenge",
    response_model=DeviceChallengeResponse,
    summary="Отримати nonce для підпису ключем пристрою",
    dependencies=[Depends(rate_limit("device_auth", fail_closed=True))],
)
async def challenge(
    payload: DeviceChallengeRequest,
    redis: Redis = Depends(get_redis),
) -> DeviceChallengeResponse:
    nonce, expires_at = await enrollment_service.create_challenge(redis, payload.device_id)
    return DeviceChallengeResponse(nonce=nonce, expires_at=expires_at)


@router.post(
    "/device/auth/token",
    response_model=DeviceTokenResponse,
    summary="Обміняти підписаний nonce на токен доступу пристрою",
    dependencies=[Depends(rate_limit("device_auth", fail_closed=True))],
)
async def device_token(
    request: Request,
    payload: DeviceTokenRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> DeviceTokenResponse:
    ip, user_agent = request_context(request)
    device, token, ttl = await enrollment_service.authenticate_device(
        session,
        redis,
        device_id=payload.device_id,
        nonce=payload.nonce,
        signature=payload.signature,
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()
    return DeviceTokenResponse(
        access_token=token,
        expires_in=ttl,
        key_rotation_due=enrollment_service.key_rotation_due(device),
        key_age_days=device.key_age_days,
    )


# ------------------------------------------------ ротація ключа пристрою ---


@router.post(
    "/device/key/challenge",
    response_model=KeyRotationChallengeResponse,
    summary="Nonce для перевипуску ключа пристрою",
    dependencies=[Depends(rate_limit("key_rotation", fail_closed=True))],
)
async def key_rotation_challenge(
    device: Device = Depends(get_current_device),
    redis: Redis = Depends(get_redis),
) -> KeyRotationChallengeResponse:
    """Видається лише вже автентифікованому пристрою: ротацію ініціює агент,
    маючи чинний ключ, а не будь-хто, хто знає device_id."""
    nonce, expires_at = await enrollment_service.create_rotation_challenge(redis, device.id)
    return KeyRotationChallengeResponse(nonce=nonce, expires_at=expires_at)


@router.post(
    "/device/key/rotate",
    response_model=KeyRotationResponse,
    summary="Замінити ключ пристрою на новий (підпис — старим ключем)",
    dependencies=[Depends(rate_limit("key_rotation", fail_closed=True))],
)
async def rotate_device_key(
    request: Request,
    payload: KeyRotationRequest,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> KeyRotationResponse:
    """Перевипуск облікових даних пристрою.

    Приватний ключ не залишає Android Keystore ані до, ані після ротації:
    агент створює нову пару локально й доводить право на заміну підписом
    старим ключем. Усі раніше видані токени цього пристрою анулюються.
    """
    ip, user_agent = request_context(request)
    updated = await enrollment_service.rotate_device_key(
        session,
        redis,
        device=device,
        new_public_key=payload.new_public_key,
        nonce=payload.nonce,
        signature=payload.signature,
        reason=payload.reason,
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()
    return KeyRotationResponse(
        device_id=updated.id,
        key_fingerprint=updated.key_fingerprint,
        key_generation=updated.key_generation,
        rotated_at=updated.key_rotated_at or utcnow(),
    )


@router.get(
    "/device/me",
    summary="Самоперевірка агента: що сервер знає про цей пристрій",
)
async def device_me(
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    device.last_seen_at = utcnow()
    await session.commit()
    return {
        "device_id": str(device.id),
        "name": device.name,
        "status": device.status,
        "management_mode": device.management_mode,
        "enrolled_at": device.enrolled_at,
        "transparency_notice": enrollment_service.TRANSPARENCY_NOTICE,
    }


# ----------------------------------------------------- командний канал ----


@router.get(
    "/device/commands/pending",
    response_model=list[PendingCommand],
    summary="Команди, які пристрій ще має виконати",
)
async def pending_commands(
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> list[PendingCommand]:
    commands = await command_service.pending_for_device(session, device)
    device.last_seen_at = utcnow()
    await session.commit()
    return [
        PendingCommand(
            id=c.id,
            type=c.type,
            payload=c.payload,
            created_at=c.created_at,
            expires_at=c.expires_at,
        )
        for c in commands
    ]


@router.post("/device/commands/{command_id}/ack", response_model=MessageResponse)
async def ack_command(
    command_id: uuid.UUID,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    command = await command_service.acknowledge(session, device, command_id)
    device.last_seen_at = utcnow()
    await session.commit()
    await command_service.notify_admins(redis, "command_acked", command)
    return MessageResponse(message="Команду прийнято до виконання")


@router.post("/device/commands/{command_id}/progress", response_model=MessageResponse)
async def command_progress(
    command_id: uuid.UUID,
    payload: CommandProgressUpdate,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    command = await command_service.update_progress(
        session, device, command_id, percent=payload.percent, message=payload.message
    )
    device.last_seen_at = utcnow()
    await session.commit()
    await command_service.notify_admins(redis, "command_progress", command)
    return MessageResponse(message="Прогрес збережено")


@router.post("/device/commands/{command_id}/result", response_model=MessageResponse)
async def command_result(
    request: Request,
    command_id: uuid.UUID,
    payload: CommandResultUpdate,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    ip, user_agent = request_context(request)
    command = await command_service.complete(
        session,
        device,
        command_id,
        status=payload.status,
        result=payload.result,
        error=payload.error,
    )
    await _apply_result_to_inventory(session, device, command)
    device.last_seen_at = utcnow()

    await record_audit(
        session,
        action=AuditAction.COMMAND_COMPLETED,
        result="success" if payload.status == "succeeded" else RESULT_FAILURE,
        device=device,
        object_type="command",
        object_id=command.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "command_type": command.type,
            "status": payload.status,
            "package_name": (command.payload or {}).get("package_name"),
            "version_code": (command.payload or {}).get("version_code"),
            "error": payload.error,
            "result": payload.result,
        },
    )
    await session.commit()
    await command_service.notify_admins(redis, "command_completed", command)
    return MessageResponse(message="Результат збережено")


async def _apply_result_to_inventory(
    session: AsyncSession, device: Device, command: Command
) -> None:
    """Тримає інвентар у синхроні одразу після успішної операції."""
    if command.status != "succeeded":
        return

    payload = command.payload or {}
    package_name = payload.get("package_name")
    if not package_name:
        return

    if command.type in ("APP_INSTALL", "APP_UPDATE"):
        installed = (command.result or {}).get("installed") or {}
        app = await device_service.find_installed(session, device, package_name)
        version_code = installed.get("version_code", payload.get("version_code"))
        version_name = installed.get("version_name", payload.get("version_name"))
        if app is None:
            session.add(
                DeviceApp(
                    device_id=device.id,
                    package_name=package_name,
                    app_name=installed.get("app_name"),
                    version_name=version_name,
                    version_code=version_code,
                    installer_package=installed.get("installer_package"),
                    last_updated_at=utcnow(),
                    last_seen_at=utcnow(),
                )
            )
        else:
            app.version_code = version_code
            app.version_name = version_name
            app.last_updated_at = utcnow()
            app.last_seen_at = utcnow()
    elif command.type == "APP_UNINSTALL":
        app = await device_service.find_installed(session, device, package_name)
        if app is not None:
            await session.delete(app)


@router.post("/device/apps", response_model=MessageResponse)
async def report_app_inventory(
    request: Request,
    payload: AppInventoryReport,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    """Агент надсилає повний перелік встановлених застосунків."""
    ip, user_agent = request_context(request)
    added, updated, removed = await device_service.sync_inventory(session, device, payload.apps)
    await record_audit(
        session,
        action=AuditAction.APP_INVENTORY_REPORTED,
        device=device,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "total": len(payload.apps),
            "added": added,
            "updated": updated,
            "removed": removed,
        },
    )
    await session.commit()
    return MessageResponse(message=f"Оновлено інвентар: {len(payload.apps)} застосунків")


# --------------------------------------------------- віддалена підтримка --


@router.get(
    "/device/support-sessions/{session_id}",
    response_model=SupportSessionOut,
    summary="Стан сеансу підтримки для агента",
)
async def device_session_status(
    session_id: uuid.UUID,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> SupportSessionOut:
    row = await support_service.get_session_or_404(session, session_id, device=device)
    return SupportSessionOut.model_validate(row)


@router.post(
    "/device/support-sessions/{session_id}/consent",
    response_model=SupportSessionOut,
    summary="Відповідь користувача пристрою на запит показу екрана",
)
async def device_session_consent(
    request: Request,
    session_id: uuid.UUID,
    payload: SessionConsent,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> SupportSessionOut:
    """Без цього виклику з `granted=true` сеанс ніколи не стане активним."""
    ip, user_agent = request_context(request)
    row = await support_service.get_session_or_404(session, session_id, device=device)

    await support_service.record_consent(
        session,
        row=row,
        device=device,
        granted=payload.granted,
        input_available=payload.input_available,
        reason=payload.reason,
        ip=ip,
        user_agent=user_agent,
    )
    device.last_seen_at = utcnow()
    await session.commit()
    await support_service.notify(
        redis,
        "support_session_consent" if payload.granted else "support_session_denied",
        row,
    )
    return SupportSessionOut.model_validate(row)


@router.post(
    "/device/support-sessions/{session_id}/end",
    response_model=SupportSessionOut,
    summary="Користувач або агент завершує сеанс",
)
async def device_session_end(
    session_id: uuid.UUID,
    payload: SessionEnd,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> SupportSessionOut:
    row = await support_service.get_session_or_404(session, session_id, device=device)
    await support_service.end_session(
        session, row=row, reason=payload.reason, device=device
    )
    device.last_seen_at = utcnow()
    await session.commit()
    await support_service.notify(redis, "support_session_ended", row, reason=row.end_reason)
    return SupportSessionOut.model_validate(row)


# ------------------------------------------------------------ телеметрія --


@router.post(
    "/device/telemetry",
    response_model=TelemetryAccepted,
    dependencies=[Depends(rate_limit("device_report"))],
    summary="Зріз стану пристрою (заряд, памʼять, сховище, CPU, версії)",
)
async def report_telemetry(
    request: Request,
    payload: TelemetryIn,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> TelemetryAccepted:
    """Приймає телеметрію й одразу переоцінює правила саме для цього пристрою.

    Сама телеметрія в аудит не пишеться (надходить щохвилини від кожного
    пристрою); в журнал потрапляють лише похідні події — алерти.
    """
    settings = get_settings()
    ip, _user_agent = request_context(request)

    await telemetry_service.ingest(session, device=device, payload=payload, ip=ip)
    raised, _resolved = await alert_service.evaluate_device(session, device, redis=redis)
    await session.commit()

    return TelemetryAccepted(
        next_report_seconds=max(60, settings.device_offline_after_seconds // 3),
        alerts_raised=raised,
    )


# ------------------------------------------------------------- файли -----


@router.get(
    "/device/files/roots",
    response_model=list[FileRootOut],
    summary="Дозволені каталоги цього пристрою (агент перевіряє їх локально)",
)
async def device_file_roots(
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> list[FileRootOut]:
    roots = await file_service.list_roots(session, device)
    device.last_seen_at = utcnow()
    await session.commit()
    return [FileRootOut.model_validate(root) for root in roots]


@router.get(
    "/device/files/transfers/{transfer_id}",
    response_model=TransferStatusOut,
    summary="Стан передачі — звідси агент дізнається, з якого зсуву продовжувати",
)
async def device_transfer_status(
    transfer_id: uuid.UUID,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> TransferStatusOut:
    transfer = await file_service.get_transfer(session, transfer_id, device=device)
    return TransferStatusOut(
        id=transfer.id,
        direction=transfer.direction,
        remote_path=transfer.remote_path,
        filename=transfer.filename,
        status=transfer.status,
        size_bytes=transfer.size_bytes,
        received_bytes=transfer.received_bytes,
        chunk_size=transfer.chunk_size,
        sha256=transfer.sha256,
        expires_at=transfer.expires_at,
    )


@router.post(
    "/device/files/transfers/{transfer_id}/chunk",
    summary="Черговий шматок файла з пристрою (заголовок X-Chunk-Offset)",
)
async def device_upload_chunk(
    request: Request,
    transfer_id: uuid.UUID,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    transfer = await file_service.get_transfer(session, transfer_id, device=device)
    if transfer.direction != "download":
        raise ValidationFailedError("Ця передача не передбачає вивантаження з пристрою")

    try:
        offset = int(request.headers.get("x-chunk-offset", "0"))
    except ValueError as exc:
        raise ValidationFailedError("Некоректний X-Chunk-Offset") from exc
    if offset < 0:
        raise ValidationFailedError("Відʼємний зсув")

    root = await session.get(FileRoot, transfer.root_id) if transfer.root_id else None
    max_bytes = (
        file_service.max_bytes_for(root)
        if root is not None
        else get_settings().max_file_transfer_bytes
    )

    data = await request.body()
    if not data:
        raise ValidationFailedError("Порожній чанк")

    received = await file_service.append_chunk(
        session, transfer, offset=offset, data=data, max_bytes=max_bytes
    )
    device.last_seen_at = utcnow()
    await session.commit()
    return {"received_bytes": received, "chunk_size": transfer.chunk_size}


@router.get(
    "/device/files/transfers/{transfer_id}/content",
    summary="Тіло файла для запису на пристрій (підтримує Range для відновлення)",
)
async def device_download_content(
    request: Request,
    transfer_id: uuid.UUID,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> Response:
    transfer = await file_service.get_transfer(session, transfer_id, device=device)
    if transfer.direction != "upload":
        raise ValidationFailedError("Ця передача не передбачає завантаження на пристрій")

    path = storage.transfer_path(str(transfer.id))
    if not path.is_file():
        raise NotFoundError("Файл передачі недоступний")

    total = path.stat().st_size
    start = 0
    end = total - 1
    partial = False

    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        # Підтримуємо звичайний випадок `bytes=<start>-[<end>]`.
        spec = range_header[len("bytes=") :].split(",")[0].strip()
        raw_start, _, raw_end = spec.partition("-")
        try:
            if raw_start:
                start = int(raw_start)
                if raw_end:
                    end = min(int(raw_end), total - 1)
            elif raw_end:
                start = max(total - int(raw_end), 0)
        except ValueError as exc:
            raise ValidationFailedError("Некоректний заголовок Range") from exc

        if start >= total or start > end:
            return Response(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                headers={"Content-Range": f"bytes */{total}"},
            )
        partial = True

    length = end - start + 1

    def stream():
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "X-Checksum-SHA256": transfer.sha256 or "",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    device.last_seen_at = utcnow()
    await session.commit()

    return StreamingResponse(
        stream(),
        status_code=status.HTTP_206_PARTIAL_CONTENT if partial else status.HTTP_200_OK,
        media_type="application/octet-stream",
        headers=headers,
    )


@router.post(
    "/device/files/transfers/{transfer_id}/progress",
    response_model=MessageResponse,
    summary="Прогрес запису файла на пристрій",
)
async def device_transfer_progress(
    transfer_id: uuid.UUID,
    payload: TransferProgressIn,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    transfer = await file_service.get_transfer(session, transfer_id, device=device)
    file_service.ensure_active(transfer)

    # Прогрес — це підказка для UI, а не джерело правди, тож приймаємо лише
    # монотонне зростання в межах оголошеного розміру. Інакше агент міг би
    # записати сюди будь-яке число й показати оператору неправдиву картину.
    reported = max(0, payload.received_bytes)
    if transfer.size_bytes:
        reported = min(reported, transfer.size_bytes)
    transfer.received_bytes = max(transfer.received_bytes or 0, reported)
    transfer.status = "in_progress"
    device.last_seen_at = utcnow()
    await session.commit()
    return MessageResponse(message="Прогрес збережено")


@router.post(
    "/device/files/transfers/{transfer_id}/complete",
    response_model=TransferStatusOut,
    summary="Завершення передачі з перевіркою контрольної суми",
)
async def device_complete_transfer(
    request: Request,
    transfer_id: uuid.UUID,
    payload: TransferCompleteIn,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> TransferStatusOut:
    ip, user_agent = request_context(request)
    transfer = await file_service.get_transfer(session, transfer_id, device=device)

    if transfer.direction == "download":
        await file_service.complete_transfer(
            session, transfer, sha256=payload.sha256, size_bytes=payload.size_bytes
        )
    else:
        # Файл писав пристрій — звіряємо суму, яку він порахував у себе.
        file_service.ensure_active(transfer)
        if payload.sha256 and transfer.sha256 and payload.sha256.lower() != transfer.sha256.lower():
            await file_service.fail_transfer(
                session, transfer, error="Контрольна сума на пристрої не збіглася"
            )
        else:
            transfer.status = "completed"
            transfer.checksum_verified = bool(payload.sha256)
            transfer.received_bytes = transfer.size_bytes or transfer.received_bytes
            transfer.completed_at = utcnow()

    await record_audit(
        session,
        action=AuditAction.FILE_TRANSFER_COMPLETED,
        result="success" if transfer.status == "completed" else RESULT_FAILURE,
        device=device,
        object_type="file_transfer",
        object_id=transfer.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "direction": transfer.direction,
            "path": transfer.remote_path,
            "size_bytes": transfer.size_bytes,
            "sha256": transfer.sha256,
            "checksum_verified": transfer.checksum_verified,
            "error": transfer.error,
        },
    )
    device.last_seen_at = utcnow()
    await session.commit()

    return TransferStatusOut(
        id=transfer.id,
        direction=transfer.direction,
        remote_path=transfer.remote_path,
        filename=transfer.filename,
        status=transfer.status,
        size_bytes=transfer.size_bytes,
        received_bytes=transfer.received_bytes,
        chunk_size=transfer.chunk_size,
        sha256=transfer.sha256,
        expires_at=transfer.expires_at,
    )


@router.post(
    "/device/files/transfers/{transfer_id}/fail",
    response_model=MessageResponse,
    summary="Агент повідомляє, що передача не вдалася",
)
async def device_fail_transfer(
    request: Request,
    transfer_id: uuid.UUID,
    payload: TransferFailIn,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    ip, user_agent = request_context(request)
    transfer = await file_service.get_transfer(session, transfer_id, device=device)
    await file_service.fail_transfer(session, transfer, error=payload.error)
    await record_audit(
        session,
        action=AuditAction.FILE_TRANSFER_COMPLETED,
        result=RESULT_FAILURE,
        device=device,
        object_type="file_transfer",
        object_id=transfer.id,
        ip=ip,
        user_agent=user_agent,
        details={"direction": transfer.direction, "error": payload.error},
    )
    await session.commit()
    return MessageResponse(message="Передачу позначено як невдалу")


# --------------------------------------------------------- геолокація ----


@router.get(
    "/device/location/config",
    response_model=AgentLocationConfig,
    summary="Поточна конфігурація геолокації для цього пристрою",
)
async def location_config(
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> AgentLocationConfig:
    settings = await location_service.get_or_create_settings(session, device)
    device.last_seen_at = utcnow()
    await session.commit()
    return location_service.to_agent_config(settings)


@router.post(
    "/device/location/permissions",
    response_model=AgentLocationConfig,
    summary="Агент повідомляє фактичний стан дозволів на геолокацію",
)
async def report_location_permissions(
    request: Request,
    payload: LocationPermissionReport,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> AgentLocationConfig:
    ip, user_agent = request_context(request)
    settings = await location_service.record_permission_state(
        session, device=device, report=payload, ip=ip, user_agent=user_agent
    )
    device.last_seen_at = utcnow()
    await session.commit()
    return location_service.to_agent_config(settings)


@router.post(
    "/device/location",
    response_model=LocationBatchAccepted,
    dependencies=[Depends(rate_limit("device_report"))],
    summary="Пакет координат (у тому числі накопичених офлайн)",
)
async def upload_location(
    request: Request,
    payload: LocationBatchIn,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> LocationBatchAccepted:
    ip, user_agent = request_context(request)
    try:
        accepted, rejected, settings = await location_service.store_fixes(
            session, device=device, fixes=payload.fixes, ip=ip, user_agent=user_agent
        )
    except PermissionError as exc:
        await session.commit()
        raise PermissionDeniedError(str(exc)) from exc

    await session.commit()
    return LocationBatchAccepted(
        accepted=accepted,
        rejected=rejected,
        config=location_service.to_agent_config(settings),
    )


@router.get(
    "/device/apks/{apk_id}/download",
    summary="Завантаження APK агентом (лише за наявності активної команди)",
)
async def device_download_apk(
    request: Request,
    apk_id: uuid.UUID,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    ip, user_agent = request_context(request)
    artifact = await apk_repository.get_artifact(session, apk_id)

    # Пристрій може завантажити APK лише тоді, коли для нього є жива команда
    # встановлення саме цього артефакту. Без цього — жодних завантажень.
    rows = (
        await session.execute(
            sa.select(Command).where(
                Command.device_id == device.id,
                Command.type.in_(("APP_INSTALL", "APP_UPDATE")),
                Command.status.in_(command_service.ACTIVE_STATUSES),
            )
        )
    ).scalars().all()
    authorized = any((row.payload or {}).get("apk_id") == str(apk_id) for row in rows)

    if not authorized:
        await record_audit(
            session,
            action=AuditAction.APK_DOWNLOADED,
            result="denied",
            device=device,
            object_type="apk",
            object_id=artifact.id,
            ip=ip,
            user_agent=user_agent,
            details={"reason": "no_active_install_command"},
        )
        await session.commit()
        raise PermissionDeniedError("Немає активної команди встановлення для цього APK")

    path = apk_repository.artifact_file(artifact)
    await record_audit(
        session,
        action=AuditAction.APK_DOWNLOADED,
        device=device,
        object_type="apk",
        object_id=artifact.id,
        ip=ip,
        user_agent=user_agent,
        details={"sha256": artifact.sha256, "channel": "device"},
    )
    device.last_seen_at = utcnow()
    await session.commit()

    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename=artifact.filename,
        headers={
            "X-Checksum-SHA256": artifact.sha256,
            "X-Package-Name": artifact.package_name or "",
            "X-Version-Code": str(artifact.version_code or ""),
        },
    )


# ------------------------------------------- комунікаційний модуль (агент) --


@router.get(
    "/device/comms/config",
    response_model=AgentCommsConfig,
    summary="Конфігурація збору SMS/дзвінків (вимкнено за замовчуванням)",
)
async def comms_config(
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> AgentCommsConfig:
    """Що саме дозволено збирати. `enabled=false` = не збирати нічого."""
    row = await comms_service.get_or_create(session, device)
    device.last_seen_at = utcnow()
    await session.commit()
    return comms_service.to_agent_config(row)


@router.post(
    "/device/comms/permissions",
    response_model=AgentCommsConfig,
    summary="Агент повідомляє фактичний стан дозволів READ_SMS/READ_CALL_LOG",
)
async def comms_permissions(
    request: Request,
    payload: CommsPermissionReport,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> AgentCommsConfig:
    ip, user_agent = request_context(request)
    row = await comms_service.record_permission_state(
        session,
        device=device,
        sms_permission=payload.sms_permission,
        call_log_permission=payload.call_log_permission,
        ip=ip,
        user_agent=user_agent,
    )
    device.last_seen_at = utcnow()
    await session.commit()
    return comms_service.to_agent_config(row)


@router.post(
    "/device/comms/data",
    response_model=CommsBatchAccepted,
    summary="Пакет SMS та журналу дзвінків (лише коли модуль увімкнено)",
    dependencies=[Depends(rate_limit("device_report"))],
)
async def comms_data(
    request: Request,
    payload: CommsBatch,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> CommsBatchAccepted:
    """Приймає й шифрує пакет.

    Якщо модуль вимкнено — сервер відхиляє запит (`409`), навіть якщо агент
    щось надіслав. Це другий рубіж на випадок розсинхронізації.
    """
    ip, user_agent = request_context(request)
    sms_stored, calls_stored, rejected = await comms_service.ingest_batch(
        session,
        device=device,
        sms=payload.sms,
        calls=payload.calls,
        ip=ip,
        user_agent=user_agent,
    )
    row = await comms_service.get_or_create(session, device)
    device.last_seen_at = utcnow()
    await session.commit()
    return CommsBatchAccepted(
        sms_stored=sms_stored,
        calls_stored=calls_stored,
        rejected=rejected,
        config=comms_service.to_agent_config(row),
    )
