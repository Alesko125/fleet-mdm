"""Файловий модуль: дозволені каталоги та операції (адміністративна частина)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.config import get_settings
from app.core.errors import ConflictError, ValidationFailedError
from app.core.rate_limit import rate_limit
from app.core.rbac import Permission
from app.core.redis_client import get_redis
from app.db.models import User
from app.db.session import get_session
from app.schemas.command import CommandOut
from app.schemas.common import MessageResponse, Page
from app.schemas.file import (
    FileDeleteRequest,
    FileDownloadRequest,
    FileListRequest,
    FileNameRequest,
    FileRootCreate,
    FileRootOut,
    FileTransferOut,
)
from app.services import commands as command_service
from app.services import device as device_service
from app.services import file_paths, storage
from app.services import files as file_service
from app.services.audit import AuditAction, record_audit

router = APIRouter(prefix="/devices/{device_id}/files", tags=["files"])


def _transfer_urls(transfer_id: uuid.UUID) -> dict[str, str]:
    settings = get_settings()
    base = f"{settings.public_base_url.rstrip('/')}{settings.api_prefix}"
    return {
        "status_url": f"{base}/device/files/transfers/{transfer_id}",
        "chunk_url": f"{base}/device/files/transfers/{transfer_id}/chunk",
        "content_url": f"{base}/device/files/transfers/{transfer_id}/content",
        "complete_url": f"{base}/device/files/transfers/{transfer_id}/complete",
    }


# -------------------------------------------------- дозволені каталоги ----


@router.get("/roots", response_model=list[FileRootOut])
async def list_roots(
    device_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.FILE_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[FileRootOut]:
    device = await device_service.get_device(session, device_id)
    roots = await file_service.list_roots(session, device)
    return [FileRootOut.model_validate(root) for root in roots]


@router.post("/roots", response_model=FileRootOut, status_code=status.HTTP_201_CREATED)
async def add_root(
    request: Request,
    device_id: uuid.UUID,
    payload: FileRootCreate,
    user: User = Depends(require_permission(Permission.FILE_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> FileRootOut:
    """Додає каталог до allowlist. Поза цим переліком доступу немає."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    root = await file_service.create_root(
        session,
        device=device,
        path=payload.path,
        label=payload.label,
        writable=payload.writable,
        max_file_size_mb=payload.max_file_size_mb,
        allow_privileged=payload.allow_privileged,
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()
    return FileRootOut.model_validate(root)


@router.delete("/roots/{root_id}", response_model=MessageResponse)
async def remove_root(
    request: Request,
    device_id: uuid.UUID,
    root_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.FILE_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    await file_service.delete_root(
        session, device=device, root_id=root_id, user=user, ip=ip, user_agent=user_agent
    )
    await session.commit()
    return MessageResponse(message="Каталог прибрано з дозволених")


# ------------------------------------------------------------- операції ---


@router.post("/list", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def list_directory(
    request: Request,
    device_id: uuid.UUID,
    payload: FileListRequest,
    user: User = Depends(require_permission(Permission.FILE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Ставить у чергу перелік каталогу; результат приходить у команді."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    path, root = await file_service.authorize_path(
        session,
        device=device,
        path=payload.path,
        need_write=False,
        user=user,
        ip=ip,
        user_agent=user_agent,
        operation="list",
    )

    command = await command_service.create_command(
        session,
        device=device,
        command_type="FILE_LIST",
        user=user,
        payload={"path": path, "root": root.path, "allow_privileged": root.allow_privileged},
        ttl_seconds=600,
    )
    await record_audit(
        session,
        action=AuditAction.FILE_LIST,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"path": path, "command_id": str(command.id)},
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.post("/mkdir", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def make_directory(
    request: Request,
    device_id: uuid.UUID,
    payload: FileNameRequest,
    user: User = Depends(require_permission(Permission.FILE_WRITE)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    parent, root = await file_service.authorize_path(
        session,
        device=device,
        path=payload.path,
        need_write=True,
        user=user,
        ip=ip,
        user_agent=user_agent,
        operation="mkdir",
    )
    try:
        target = file_paths.join(parent, payload.name)
    except file_paths.PathViolation as exc:
        raise ValidationFailedError(str(exc)) from exc

    command = await command_service.create_command(
        session,
        device=device,
        command_type="FILE_MKDIR",
        user=user,
        payload={"path": target, "root": root.path, "allow_privileged": root.allow_privileged},
        ttl_seconds=600,
    )
    await record_audit(
        session,
        action=AuditAction.FILE_MKDIR,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"path": target, "command_id": str(command.id)},
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.post("/rename", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def rename_entry(
    request: Request,
    device_id: uuid.UUID,
    payload: FileNameRequest,
    user: User = Depends(require_permission(Permission.FILE_WRITE)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    """Перейменування в межах того самого каталогу (переміщення не робимо)."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    path, root = await file_service.authorize_path(
        session,
        device=device,
        path=payload.path,
        need_write=True,
        user=user,
        ip=ip,
        user_agent=user_agent,
        operation="rename",
    )
    try:
        target = file_paths.join(file_paths.parent_of(path), payload.name)
    except file_paths.PathViolation as exc:
        raise ValidationFailedError(str(exc)) from exc

    # Нова назва теж має лишитися в дозволеному каталозі.
    await file_service.authorize_path(
        session,
        device=device,
        path=target,
        need_write=True,
        user=user,
        ip=ip,
        user_agent=user_agent,
        operation="rename_target",
    )

    command = await command_service.create_command(
        session,
        device=device,
        command_type="FILE_RENAME",
        user=user,
        payload={
            "path": path,
            "target": target,
            "root": root.path,
            "allow_privileged": root.allow_privileged,
        },
        ttl_seconds=600,
    )
    await record_audit(
        session,
        action=AuditAction.FILE_RENAME,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"from": path, "to": target, "command_id": str(command.id)},
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


@router.post("/delete", response_model=CommandOut, status_code=status.HTTP_202_ACCEPTED)
async def delete_entry(
    request: Request,
    device_id: uuid.UUID,
    payload: FileDeleteRequest,
    user: User = Depends(require_permission(Permission.FILE_WRITE)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CommandOut:
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    path, root = await file_service.authorize_path(
        session,
        device=device,
        path=payload.path,
        need_write=True,
        user=user,
        ip=ip,
        user_agent=user_agent,
        operation="delete",
    )

    if path == root.path:
        raise ConflictError("Не можна видалити сам дозволений каталог")

    command = await command_service.create_command(
        session,
        device=device,
        command_type="FILE_DELETE",
        user=user,
        payload={
            "path": path,
            "recursive": payload.recursive,
            "root": root.path,
            "allow_privileged": root.allow_privileged,
        },
        ttl_seconds=600,
    )
    await record_audit(
        session,
        action=AuditAction.FILE_DELETE,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "path": path,
            "recursive": payload.recursive,
            "command_id": str(command.id),
        },
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return CommandOut.model_validate(command)


# ------------------------------------------------------------- передачі ---


@router.post("/download", response_model=FileTransferOut, status_code=status.HTTP_202_ACCEPTED)
async def request_download(
    request: Request,
    device_id: uuid.UUID,
    payload: FileDownloadRequest,
    user: User = Depends(require_permission(Permission.FILE_READ)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> FileTransferOut:
    """Просить пристрій віддати файл; агент вивантажує його чанками."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    path, root = await file_service.authorize_path(
        session,
        device=device,
        path=payload.path,
        need_write=False,
        user=user,
        ip=ip,
        user_agent=user_agent,
        operation="download",
    )

    transfer = await file_service.create_transfer(
        session,
        device=device,
        direction="download",
        remote_path=path,
        filename=file_paths.basename(path),
        root=root,
        user=user,
    )
    command = await command_service.create_command(
        session,
        device=device,
        command_type="FILE_DOWNLOAD",
        user=user,
        payload={
            "transfer_id": str(transfer.id),
            "path": path,
            "root": root.path,
            "allow_privileged": root.allow_privileged,
            "max_bytes": file_service.max_bytes_for(root),
            "chunk_size": transfer.chunk_size,
            **_transfer_urls(transfer.id),
        },
        ttl_seconds=3600,
    )
    transfer.command_id = command.id

    await record_audit(
        session,
        action=AuditAction.FILE_DOWNLOAD,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "path": path,
            "transfer_id": str(transfer.id),
            "command_id": str(command.id),
            "phase": "requested",
        },
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return FileTransferOut.model_validate(transfer)


@router.post(
    "/upload",
    response_model=FileTransferOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("upload"))],
)
async def upload_to_device(
    request: Request,
    device_id: uuid.UUID,
    file: UploadFile = File(...),
    target_dir: str = Form(...),
    user: User = Depends(require_permission(Permission.FILE_WRITE)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> FileTransferOut:
    """Приймає файл від адміністратора і доручає агенту записати його."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    directory, root = await file_service.authorize_path(
        session,
        device=device,
        path=target_dir,
        need_write=True,
        user=user,
        ip=ip,
        user_agent=user_agent,
        operation="upload",
    )

    try:
        filename = file_paths.validate_name(file.filename or "")
        target_path = file_paths.join(directory, filename)
    except file_paths.PathViolation as exc:
        raise ValidationFailedError(f"Некоректне імʼя файла: {exc}") from exc

    max_bytes = file_service.max_bytes_for(root)
    try:
        tmp_path, sha256, size = await storage.store_upload(
            file, max_bytes=max_bytes, require_zip=False
        )
    except storage.UploadTooLargeError as exc:
        raise ValidationFailedError(str(exc)) from exc
    except storage.NotAnApkError as exc:
        raise ValidationFailedError(str(exc)) from exc

    transfer = await file_service.create_transfer(
        session,
        device=device,
        direction="upload",
        remote_path=target_path,
        filename=filename,
        root=root,
        user=user,
        size_bytes=size,
        sha256=sha256,
    )
    # Тимчасовий файл стає тілом передачі.
    final_path = storage.transfer_path(str(transfer.id))
    tmp_path.replace(final_path)
    transfer.storage_path = str(final_path)
    transfer.checksum_verified = True

    command = await command_service.create_command(
        session,
        device=device,
        command_type="FILE_UPLOAD",
        user=user,
        payload={
            "transfer_id": str(transfer.id),
            "path": target_path,
            "root": root.path,
            "allow_privileged": root.allow_privileged,
            "size_bytes": size,
            "sha256": sha256,
            "chunk_size": transfer.chunk_size,
            "max_bytes": max_bytes,
            **_transfer_urls(transfer.id),
        },
        ttl_seconds=3600,
    )
    transfer.command_id = command.id

    await record_audit(
        session,
        action=AuditAction.FILE_UPLOAD,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "path": target_path,
            "size_bytes": size,
            "sha256": sha256,
            "transfer_id": str(transfer.id),
            "command_id": str(command.id),
            "phase": "requested",
        },
    )
    await session.commit()
    await command_service.dispatch(redis, command)
    return FileTransferOut.model_validate(transfer)


@router.get("/transfers", response_model=Page[FileTransferOut])
async def list_transfers(
    device_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.FILE_READ)),
    session: AsyncSession = Depends(get_session),
) -> Page[FileTransferOut]:
    device = await device_service.get_device(session, device_id)
    rows, total = await file_service.transfers_for_device(
        session, device, limit=limit, offset=offset
    )
    return Page(
        items=[FileTransferOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/transfers/{transfer_id}", response_model=FileTransferOut)
async def transfer_details(
    device_id: uuid.UUID,
    transfer_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.FILE_READ)),
    session: AsyncSession = Depends(get_session),
) -> FileTransferOut:
    device = await device_service.get_device(session, device_id)
    transfer = await file_service.get_transfer(session, transfer_id, device=device)
    return FileTransferOut.model_validate(transfer)


@router.get("/transfers/{transfer_id}/content")
async def download_transfer_content(
    request: Request,
    device_id: uuid.UUID,
    transfer_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.FILE_READ)),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Віддає адміністратору файл, який вивантажив пристрій."""
    ip, user_agent = request_context(request)
    device = await device_service.get_device(session, device_id)
    transfer = await file_service.get_transfer(session, transfer_id, device=device)

    if transfer.status != "completed" or not transfer.storage_path:
        raise ConflictError("Передача ще не завершена")

    path = storage.transfer_path(str(transfer.id))
    if not path.is_file():
        raise ConflictError("Файл передачі більше недоступний")

    await record_audit(
        session,
        action=AuditAction.FILE_DOWNLOAD,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "path": transfer.remote_path,
            "transfer_id": str(transfer.id),
            "sha256": transfer.sha256,
            "phase": "fetched_by_admin",
        },
    )
    await session.commit()

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=transfer.filename,
        headers={"X-Checksum-SHA256": transfer.sha256 or ""},
    )
