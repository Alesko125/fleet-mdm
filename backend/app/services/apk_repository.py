"""Репозиторій APK: приймання, дедуплікація, пошук останніх версій."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.db.models import ApkArtifact, User
from app.services import storage
from app.services.apk_parser import ApkParseError, parse_apk
from app.services.audit import AuditAction, record_audit

logger = logging.getLogger(__name__)

MAX_FILENAME_LENGTH = 255


def sanitize_filename(raw: str | None) -> str:
    """Лишає лише базове імʼя без шляхів — захист від traversal."""
    name = Path(raw or "upload.apk").name.strip()
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "._- ()+")
    cleaned = cleaned.strip() or "upload.apk"
    if not cleaned.lower().endswith(".apk"):
        cleaned += ".apk"
    return cleaned[:MAX_FILENAME_LENGTH]


async def register_upload(
    session: AsyncSession,
    *,
    user: User,
    stream,
    filename: str | None,
    notes: str | None,
    ip: str | None,
    user_agent: str | None,
) -> tuple[ApkArtifact, bool]:
    """Приймає вивантажений APK. Повертає `(артефакт, це_дублікат)`."""
    settings = get_settings()
    try:
        tmp_path, sha256, size = await storage.store_upload(
            stream, max_bytes=settings.max_upload_bytes
        )
    except storage.UploadTooLargeError as exc:
        raise ValidationFailedError(str(exc)) from exc
    except storage.NotAnApkError as exc:
        raise ValidationFailedError(str(exc)) from exc

    existing = (
        await session.execute(sa.select(ApkArtifact).where(ApkArtifact.sha256 == sha256))
    ).scalar_one_or_none()
    if existing is not None:
        tmp_path.unlink(missing_ok=True)
        logger.info("APK уже є в репозиторії", extra={"sha256": sha256})
        return existing, True

    try:
        metadata = parse_apk(tmp_path)
    except ApkParseError as exc:
        tmp_path.unlink(missing_ok=True)
        raise ValidationFailedError(f"Не вдалося розібрати APK: {exc}") from exc

    if metadata.is_split:
        tmp_path.unlink(missing_ok=True)
        raise ValidationFailedError(
            "Split-APK не підтримується: завантажте цілісний (universal) APK"
        )

    # Непідписаний пакет пристрій усе одно не встановить, а в репозиторії
    # він лише вводив би оператора в оману. Політика живе тут, на межі
    # приймання, а не в парсері — щоб відмова потрапила в аудит із причиною.
    if not metadata.signature_schemes:
        tmp_path.unlink(missing_ok=True)
        await record_audit(
            session,
            action=AuditAction.APK_UPLOADED,
            result="failure",
            user=user,
            ip=ip,
            user_agent=user_agent,
            details={
                "reason": "unsigned",
                "package_name": metadata.package_name,
                "sha256": sha256,
            },
        )
        await session.commit()
        raise ValidationFailedError(
            "APK не має жодної схеми підпису (v1/v2+) — встановити його неможливо"
        )

    if settings.apk_allowed_signers:
        allowed = {value.lower() for value in settings.apk_allowed_signer_list}
        digest = (metadata.signature_digest or "").lower()
        if digest not in allowed:
            tmp_path.unlink(missing_ok=True)
            await record_audit(
                session,
                action=AuditAction.APK_UPLOADED,
                result="denied",
                user=user,
                ip=ip,
                user_agent=user_agent,
                details={
                    "reason": "signer_not_allowed",
                    "package_name": metadata.package_name,
                    "signature_digest": digest or None,
                    "sha256": sha256,
                },
            )
            await session.commit()
            raise ValidationFailedError(
                "Сертифікат підпису APK не входить до переліку дозволених "
                "(APK_ALLOWED_SIGNERS)"
            )

    stored_path = storage.promote_apk(tmp_path, sha256)

    artifact = ApkArtifact(
        filename=sanitize_filename(filename),
        package_name=metadata.package_name,
        version_name=metadata.version_name,
        version_code=metadata.version_code,
        size_bytes=size,
        sha256=sha256,
        signature_digest=metadata.signature_digest,
        signature_schemes=metadata.signature_schemes or None,
        min_sdk=metadata.min_sdk,
        target_sdk=metadata.target_sdk,
        compile_sdk=metadata.compile_sdk,
        abis=metadata.abis or None,
        permissions=metadata.permissions or None,
        is_split=metadata.is_split,
        storage_path=str(stored_path),
        uploaded_by_id=user.id,
        notes=notes,
    )
    session.add(artifact)
    await session.flush()

    await record_audit(
        session,
        action=AuditAction.APK_UPLOADED,
        user=user,
        object_type="apk",
        object_id=artifact.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "filename": artifact.filename,
            "package_name": artifact.package_name,
            "version_name": artifact.version_name,
            "version_code": artifact.version_code,
            "size_bytes": size,
            "sha256": sha256,
            "abis": metadata.abis,
            "min_sdk": metadata.min_sdk,
            "target_sdk": metadata.target_sdk,
            "signature_schemes": metadata.signature_schemes,
            "permission_count": len(metadata.permissions),
        },
    )
    return artifact, False


async def get_artifact(session: AsyncSession, apk_id: uuid.UUID) -> ApkArtifact:
    artifact = await session.get(ApkArtifact, apk_id)
    if artifact is None:
        raise NotFoundError("APK не знайдено в репозиторії")
    return artifact


async def latest_by_package(
    session: AsyncSession, packages: list[str]
) -> dict[str, ApkArtifact]:
    """Найсвіжіша версія кожного пакета в репозиторії."""
    if not packages:
        return {}
    rows = (
        await session.execute(
            sa.select(ApkArtifact)
            .where(ApkArtifact.package_name.in_(packages))
            .order_by(
                ApkArtifact.package_name,
                ApkArtifact.version_code.desc().nullslast(),
                ApkArtifact.created_at.desc(),
            )
        )
    ).scalars().all()

    latest: dict[str, ApkArtifact] = {}
    for row in rows:
        if row.package_name and row.package_name not in latest:
            latest[row.package_name] = row
    return latest


def artifact_file(artifact: ApkArtifact) -> Path:
    path = Path(artifact.storage_path)
    if not path.is_file():
        # Резервний шлях на випадок перенесення сховища.
        path = storage.apk_path_for(artifact.sha256)
    if not path.is_file():
        raise NotFoundError("Файл APK відсутній у сховищі")
    return path
