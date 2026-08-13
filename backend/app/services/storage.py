"""Файлове сховище артефактів (APK, файли пристроїв).

Дедуплікація за SHA-256: один і той самий APK зберігається один раз.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)

ZIP_MAGIC = b"PK\x03\x04"
CHUNK_SIZE = 1024 * 1024


class UploadTooLargeError(ValueError):
    pass


class NotAnApkError(ValueError):
    pass


def storage_root() -> Path:
    return Path(get_settings().storage_root)


def apk_dir() -> Path:
    path = storage_root() / "apk"
    path.mkdir(parents=True, exist_ok=True)
    return path


def files_dir() -> Path:
    """Сховище файлів, що передаються між панеллю та пристроями."""
    path = storage_root() / "files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def transfer_path(transfer_id: str) -> Path:
    """Один файл на передачу — так дописування чанків завжди ізольоване."""
    directory = files_dir() / str(transfer_id)[:2]
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{transfer_id}.bin"


def append_chunk(transfer_id: str, offset: int, data: bytes) -> int:
    """Дописує чанк у файл передачі. Повертає новий розмір.

    Дописування дозволено лише рівно з поточного кінця файла — це і робить
    передачу відновлюваною, і не дає «дірок» чи перезапису.
    """
    target = transfer_path(transfer_id)
    current = target.stat().st_size if target.exists() else 0
    if offset != current:
        raise ValueError(f"Очікувався зсув {current}, отримано {offset}")
    with open(target, "ab") as handle:
        handle.write(data)
    return current + len(data)


def transfer_size(transfer_id: str) -> int:
    target = transfer_path(transfer_id)
    return target.stat().st_size if target.exists() else 0


def delete_transfer(transfer_id: str) -> None:
    transfer_path(transfer_id).unlink(missing_ok=True)


def apk_path_for(sha256: str) -> Path:
    """`<storage>/apk/<ab>/<sha256>.apk` — щоб не мати мільйон файлів в одній теці."""
    return apk_dir() / sha256[:2] / f"{sha256}.apk"


async def store_upload(
    stream, *, max_bytes: int, require_zip: bool = True
) -> tuple[Path, str, int]:
    """Потоково зберігає вивантаження у тимчасовий файл.

    Повертає `(тимчасовий шлях, sha256, розмір)`. Викликач переміщує файл
    у постійне місце (`promote_apk`) або видаляє.
    """
    digest = hashlib.sha256()
    size = 0
    first_chunk = True

    tmp_dir = storage_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=tmp_dir, suffix=".part", delete=False)
    tmp_path = Path(handle.name)

    try:
        with handle:
            while True:
                chunk = await stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                if first_chunk:
                    if require_zip and not chunk.startswith(ZIP_MAGIC):
                        raise NotAnApkError("Файл не є APK (немає ZIP-сигнатури)")
                    first_chunk = False
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLargeError(
                        f"Файл більший за дозволені {max_bytes // (1024 * 1024)} МБ"
                    )
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise NotAnApkError("Порожній файл")
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return tmp_path, digest.hexdigest(), size


def promote_apk(tmp_path: Path, sha256: str) -> Path:
    """Переносить тимчасовий файл у постійне місце (ідемпотентно)."""
    target = apk_path_for(sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        tmp_path.unlink(missing_ok=True)
        return target
    os.replace(tmp_path, target)
    return target


def delete_apk(sha256: str) -> bool:
    target = apk_path_for(sha256)
    if target.exists():
        target.unlink()
        return True
    return False
