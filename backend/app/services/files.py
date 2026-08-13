"""Файловий модуль: дозволені каталоги, авторизація шляхів, передачі.

Ключове правило: **пристрій має доступ лише до каталогів, які адміністратор
явно додав саме для нього**. Порожній перелік `file_roots` = файловий модуль
для цього пристрою вимкнений. Кожна відмова потрапляє в аудит разом із
повним шляхом, який намагалися відкрити.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta

import anyio.to_thread
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.core.security import as_utc, utcnow
from app.db.models import Device, FileRoot, FileTransfer, User
from app.services import file_paths, storage
from app.services.audit import RESULT_DENIED, AuditAction, record_audit
from app.services.file_paths import PathViolation

logger = logging.getLogger(__name__)

ACTIVE_TRANSFER_STATUSES = ("pending", "in_progress")


# ------------------------------------------------------ дозволені каталоги --


async def list_roots(session: AsyncSession, device: Device) -> list[FileRoot]:
    rows = (
        await session.execute(
            sa.select(FileRoot)
            .where(FileRoot.device_id == device.id)
            .order_by(FileRoot.path)
        )
    ).scalars().all()
    return list(rows)


async def create_root(
    session: AsyncSession,
    *,
    device: Device,
    path: str,
    label: str | None,
    writable: bool,
    max_file_size_mb: int | None,
    allow_privileged: bool,
    user: User,
    ip: str | None,
    user_agent: str | None,
) -> FileRoot:
    try:
        normalized = file_paths.normalize_path(path)
    except PathViolation as exc:
        raise ValidationFailedError(str(exc)) from exc

    if normalized == "/":
        raise ValidationFailedError(
            "Корінь файлової системи не можна додати як дозволений каталог"
        )

    existing = (
        await session.execute(
            sa.select(FileRoot).where(
                FileRoot.device_id == device.id, FileRoot.path == normalized
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("Такий каталог уже дозволено")

    root = FileRoot(
        device_id=device.id,
        path=normalized,
        label=label,
        writable=writable,
        max_file_size_mb=max_file_size_mb,
        allow_privileged=allow_privileged,
        created_by_id=user.id,
    )
    session.add(root)
    await session.flush()

    await record_audit(
        session,
        action=AuditAction.FILE_ROOT_CHANGED,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "operation": "added",
            "path": normalized,
            "writable": writable,
            "allow_privileged": allow_privileged,
            "max_file_size_mb": max_file_size_mb,
        },
    )
    return root


async def delete_root(
    session: AsyncSession,
    *,
    device: Device,
    root_id: uuid.UUID,
    user: User,
    ip: str | None,
    user_agent: str | None,
) -> None:
    root = await session.get(FileRoot, root_id)
    if root is None or root.device_id != device.id:
        raise NotFoundError("Каталог не знайдено")

    details = {
        "operation": "removed",
        "path": root.path,
        "writable": root.writable,
        "allow_privileged": root.allow_privileged,
    }
    await session.delete(root)
    await record_audit(
        session,
        action=AuditAction.FILE_ROOT_CHANGED,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details=details,
    )


def max_bytes_for(root: FileRoot) -> int:
    settings = get_settings()
    if root.max_file_size_mb:
        return min(root.max_file_size_mb * 1024 * 1024, settings.max_file_transfer_bytes)
    return settings.max_file_transfer_bytes


# ------------------------------------------------------------ авторизація --


async def authorize_path(
    session: AsyncSession,
    *,
    device: Device,
    path: str,
    need_write: bool,
    user: User | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    operation: str = "access",
) -> tuple[str, FileRoot]:
    """Перевіряє, що шлях лежить у дозволеному каталозі пристрою.

    Повертає `(нормалізований шлях, каталог)`. Кидає `PermissionDeniedError`
    і пише в аудит, якщо доступ не дозволено.
    """
    try:
        normalized = file_paths.normalize_path(path)
    except PathViolation as exc:
        await _deny(
            session,
            device=device,
            user=user,
            ip=ip,
            user_agent=user_agent,
            path=path,
            reason=str(exc),
            operation=operation,
        )
        raise PermissionDeniedError(f"Некоректний шлях: {exc}") from exc

    roots = await list_roots(session, device)
    if not roots:
        await _deny(
            session,
            device=device,
            user=user,
            ip=ip,
            user_agent=user_agent,
            path=normalized,
            reason="no_roots_configured",
            operation=operation,
        )
        raise PermissionDeniedError(
            "Для цього пристрою не налаштовано жодного дозволеного каталогу"
        )

    match = next((root for root in roots if file_paths.is_within(root.path, normalized)), None)
    if match is None:
        await _deny(
            session,
            device=device,
            user=user,
            ip=ip,
            user_agent=user_agent,
            path=normalized,
            reason="outside_allowlist",
            operation=operation,
        )
        raise PermissionDeniedError("Шлях поза дозволеними каталогами")

    if need_write and not match.writable:
        await _deny(
            session,
            device=device,
            user=user,
            ip=ip,
            user_agent=user_agent,
            path=normalized,
            reason="read_only_root",
            operation=operation,
        )
        raise PermissionDeniedError(f"Каталог {match.path} доступний лише для читання")

    return normalized, match


async def _deny(
    session: AsyncSession,
    *,
    device: Device,
    user: User | None,
    ip: str | None,
    user_agent: str | None,
    path: str,
    reason: str,
    operation: str,
) -> None:
    await record_audit(
        session,
        action=AuditAction.FILE_ACCESS_DENIED,
        result=RESULT_DENIED,
        user=user,
        device=None if user else device,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"path": path[:1024], "reason": reason, "operation": operation},
    )
    await session.commit()


# --------------------------------------------------------------- передачі --


def _transfer_ttl() -> timedelta:
    return timedelta(seconds=get_settings().file_transfer_ttl_seconds)


async def create_transfer(
    session: AsyncSession,
    *,
    device: Device,
    direction: str,
    remote_path: str,
    filename: str,
    root: FileRoot,
    user: User,
    size_bytes: int | None = None,
    sha256: str | None = None,
    storage_path: str | None = None,
) -> FileTransfer:
    settings = get_settings()
    transfer = FileTransfer(
        device_id=device.id,
        direction=direction,
        remote_path=remote_path,
        filename=filename,
        size_bytes=size_bytes,
        sha256=sha256,
        storage_path=storage_path,
        status="pending",
        chunk_size=settings.file_transfer_chunk_kb * 1024,
        expires_at=utcnow() + _transfer_ttl(),
        root_id=root.id,
        created_by_id=user.id,
    )
    session.add(transfer)
    await session.flush()
    return transfer


async def get_transfer(
    session: AsyncSession, transfer_id: uuid.UUID, *, device: Device | None = None
) -> FileTransfer:
    transfer = await session.get(FileTransfer, transfer_id)
    if transfer is None:
        raise NotFoundError("Передачу не знайдено")
    if device is not None and transfer.device_id != device.id:
        # Пристрій не має навіть знати про існування чужих передач.
        raise NotFoundError("Передачу не знайдено")
    return transfer


def ensure_active(transfer: FileTransfer) -> None:
    if transfer.status not in ACTIVE_TRANSFER_STATUSES:
        raise ConflictError(f"Передача вже у статусі {transfer.status}")
    expires_at = as_utc(transfer.expires_at)
    if expires_at is not None and expires_at <= utcnow():
        raise ConflictError("Термін дії передачі вичерпано")


async def append_chunk(
    session: AsyncSession,
    transfer: FileTransfer,
    *,
    offset: int,
    data: bytes,
    max_bytes: int,
) -> int:
    """Дописує черговий шматок файла, що вивантажується з пристрою."""
    ensure_active(transfer)

    if transfer.received_bytes + len(data) > max_bytes:
        transfer.status = "failed"
        transfer.error = "Перевищено дозволений розмір файла"
        storage.delete_transfer(str(transfer.id))
        raise ValidationFailedError("Перевищено дозволений розмір файла")

    try:
        received = storage.append_chunk(str(transfer.id), offset, data)
    except ValueError as exc:
        # Клієнт надіслав неправильний зсув — повідомляємо фактичний.
        raise ConflictError(
            f"{exc}. Поточний зсув: {storage.transfer_size(str(transfer.id))}"
        ) from exc

    transfer.received_bytes = received
    transfer.status = "in_progress"
    return received


def _sha256_of_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def complete_transfer(
    session: AsyncSession,
    transfer: FileTransfer,
    *,
    sha256: str | None,
    size_bytes: int | None = None,
) -> FileTransfer:
    """Завершує передачу з пристрою і звіряє контрольну суму."""
    ensure_active(transfer)

    path = storage.transfer_path(str(transfer.id))
    actual_size = path.stat().st_size if path.exists() else 0

    if size_bytes is not None and size_bytes != actual_size:
        transfer.status = "failed"
        transfer.error = f"Отримано {actual_size} байт замість очікуваних {size_bytes}"
        return transfer

    if sha256:
        # Хешування великого файла — блокуюча операція, тож у окремому потоці.
        actual = await anyio.to_thread.run_sync(_sha256_of_file, path)
        if actual.lower() != sha256.lower():
            transfer.status = "failed"
            transfer.error = "Контрольна сума не збігається"
            storage.delete_transfer(str(transfer.id))
            return transfer
        transfer.sha256 = actual
        transfer.checksum_verified = True

    transfer.size_bytes = actual_size
    transfer.received_bytes = actual_size
    transfer.storage_path = str(path)
    transfer.status = "completed"
    transfer.completed_at = utcnow()
    return transfer


async def fail_transfer(
    session: AsyncSession, transfer: FileTransfer, *, error: str
) -> FileTransfer:
    transfer.status = "failed"
    transfer.error = error[:2000]
    transfer.completed_at = utcnow()
    storage.delete_transfer(str(transfer.id))
    return transfer


async def transfers_for_device(
    session: AsyncSession, device: Device, *, limit: int = 50, offset: int = 0
) -> tuple[list[FileTransfer], int]:
    total = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(FileTransfer)
            .where(FileTransfer.device_id == device.id)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(FileTransfer)
            .where(FileTransfer.device_id == device.id)
            .order_by(FileTransfer.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return list(rows), total
