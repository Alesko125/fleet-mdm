"""Репозиторій APK: завантаження, перелік, метадані, вивантаження, видалення."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.errors import ConflictError
from app.core.rate_limit import rate_limit
from app.core.rbac import Permission
from app.db.models import ApkArtifact, Command, User
from app.db.session import get_session
from app.schemas.apk import ApkOut, ApkUpdate, ApkUploadResult
from app.schemas.common import MessageResponse, Page
from app.services import apk_repository, storage
from app.services.audit import AuditAction, record_audit

router = APIRouter(prefix="/apks", tags=["apk"])


@router.post(
    "",
    response_model=ApkUploadResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("upload"))],
)
async def upload_apk(
    request: Request,
    file: UploadFile = File(..., description="Файл APK"),
    notes: str | None = Form(default=None, max_length=2000),
    user: User = Depends(require_permission(Permission.APP_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> ApkUploadResult:
    """Приймає APK, рахує SHA-256 і витягує метадані з AndroidManifest.xml."""
    ip, user_agent = request_context(request)
    artifact, deduplicated = await apk_repository.register_upload(
        session,
        user=user,
        stream=file,
        filename=file.filename,
        notes=notes,
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()
    await session.refresh(artifact)
    return ApkUploadResult(
        **ApkOut.model_validate(artifact).model_dump(), deduplicated=deduplicated
    )


@router.get("", response_model=Page[ApkOut])
async def list_apks(
    package: str | None = Query(default=None, max_length=255),
    search: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.APP_READ)),
    session: AsyncSession = Depends(get_session),
) -> Page[ApkOut]:
    conditions = []
    if package:
        conditions.append(ApkArtifact.package_name == package)
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(
            sa.or_(
                ApkArtifact.package_name.ilike(pattern),
                ApkArtifact.filename.ilike(pattern),
                ApkArtifact.label.ilike(pattern),
            )
        )

    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(ApkArtifact).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(ApkArtifact)
            .where(*conditions)
            .order_by(ApkArtifact.package_name, ApkArtifact.version_code.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return Page(
        items=[ApkOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{apk_id}", response_model=ApkOut)
async def get_apk(
    apk_id: uuid.UUID,
    _: User = Depends(require_permission(Permission.APP_READ)),
    session: AsyncSession = Depends(get_session),
) -> ApkArtifact:
    return await apk_repository.get_artifact(session, apk_id)


@router.patch("/{apk_id}", response_model=ApkOut)
async def update_apk(
    request: Request,
    apk_id: uuid.UUID,
    payload: ApkUpdate,
    user: User = Depends(require_permission(Permission.APP_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> ApkArtifact:
    ip, user_agent = request_context(request)
    artifact = await apk_repository.get_artifact(session, apk_id)

    changes: dict[str, object] = {}
    if payload.notes is not None:
        changes["notes"] = payload.notes
        artifact.notes = payload.notes
    if payload.label is not None:
        changes["label"] = payload.label
        artifact.label = payload.label

    await record_audit(
        session,
        action=AuditAction.APK_UPDATED,
        user=user,
        object_type="apk",
        object_id=artifact.id,
        ip=ip,
        user_agent=user_agent,
        details={"changes": changes},
    )
    await session.commit()
    return artifact


@router.get("/{apk_id}/download")
async def download_apk(
    request: Request,
    apk_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.APP_READ)),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Вивантаження адміністратором (агент має власний ендпоїнт)."""
    ip, user_agent = request_context(request)
    artifact = await apk_repository.get_artifact(session, apk_id)
    path = apk_repository.artifact_file(artifact)

    await record_audit(
        session,
        action=AuditAction.APK_DOWNLOADED,
        user=user,
        object_type="apk",
        object_id=artifact.id,
        ip=ip,
        user_agent=user_agent,
        details={"sha256": artifact.sha256, "channel": "admin"},
    )
    await session.commit()

    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename=artifact.filename,
        headers={"X-Checksum-SHA256": artifact.sha256},
    )


@router.delete("/{apk_id}", response_model=MessageResponse)
async def delete_apk(
    request: Request,
    apk_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.APP_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    ip, user_agent = request_context(request)
    artifact = await apk_repository.get_artifact(session, apk_id)

    active = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Command)
            .where(
                Command.status.in_(("pending", "dispatched", "acked")),
                Command.type.in_(("APP_INSTALL", "APP_UPDATE")),
            )
        )
    ).scalar_one()
    if active:
        # Перевіряємо точніше — чи саме цей APK бере участь у активній операції.
        rows = (
            await session.execute(
                sa.select(Command).where(
                    Command.status.in_(("pending", "dispatched", "acked")),
                    Command.type.in_(("APP_INSTALL", "APP_UPDATE")),
                )
            )
        ).scalars().all()
        if any((row.payload or {}).get("apk_id") == str(artifact.id) for row in rows):
            raise ConflictError("APK використовується в активній операції встановлення")

    details = {
        "filename": artifact.filename,
        "package_name": artifact.package_name,
        "version_code": artifact.version_code,
        "sha256": artifact.sha256,
    }
    sha256 = artifact.sha256
    await session.delete(artifact)
    await record_audit(
        session,
        action=AuditAction.APK_DELETED,
        user=user,
        object_type="apk",
        object_id=apk_id,
        ip=ip,
        user_agent=user_agent,
        details=details,
    )
    await session.commit()
    storage.delete_apk(sha256)
    return MessageResponse(message="APK видалено з репозиторію")
