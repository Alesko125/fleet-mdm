"""Керування enrollment-токенами (адміністративна частина)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.rbac import Permission
from app.db.models import EnrollmentToken, User
from app.db.session import get_session
from app.schemas.common import Page
from app.schemas.enrollment import (
    EnrollmentTokenCreate,
    EnrollmentTokenCreated,
    EnrollmentTokenOut,
)
from app.services import enrollment as enrollment_service

router = APIRouter(prefix="/enrollment", tags=["enrollment"])


@router.post("/tokens", response_model=EnrollmentTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    request: Request,
    payload: EnrollmentTokenCreate,
    user: User = Depends(require_permission(Permission.ENROLLMENT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> EnrollmentTokenCreated:
    ip, user_agent = request_context(request)
    created = await enrollment_service.create_enrollment_token(
        session, user=user, data=payload, ip=ip, user_agent=user_agent
    )
    await session.commit()
    await session.refresh(created.model)
    return EnrollmentTokenCreated(
        **EnrollmentTokenOut.model_validate(created.model).model_dump(),
        token=created.raw_token,
        enroll_url=created.enroll_url,
        qr_payload=created.qr_payload,
    )


@router.get("/tokens", response_model=Page[EnrollmentTokenOut])
async def list_tokens(
    active_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.ENROLLMENT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> Page[EnrollmentTokenOut]:
    stmt = sa.select(EnrollmentToken)
    count_stmt = sa.select(sa.func.count()).select_from(EnrollmentToken)
    if active_only:
        condition = sa.and_(
            EnrollmentToken.revoked_at.is_(None),
            EnrollmentToken.expires_at > sa.func.now(),
            EnrollmentToken.use_count < EnrollmentToken.max_uses,
        )
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(EnrollmentToken.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return Page(
        items=[EnrollmentTokenOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/tokens/{token_id}/revoke", response_model=EnrollmentTokenOut)
async def revoke_token(
    request: Request,
    token_id: uuid.UUID,
    user: User = Depends(require_permission(Permission.ENROLLMENT_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> EnrollmentToken:
    ip, user_agent = request_context(request)
    model = await enrollment_service.revoke_enrollment_token(
        session, user=user, token_id=token_id, ip=ip, user_agent=user_agent
    )
    await session.commit()
    return model
