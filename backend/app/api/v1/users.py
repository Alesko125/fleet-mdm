"""Керування операторами (лише superadmin)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import request_context, require_permission
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.rate_limit import rate_limit
from app.core.rbac import Permission
from app.core.security import hash_password, utcnow
from app.db.models import User
from app.db.session import get_session
from app.schemas.auth import PasswordReset, UserCreate, UserOut, UserUpdate
from app.schemas.common import MessageResponse, Page
from app.services import auth as auth_service
from app.services.audit import AuditAction, record_audit

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=Page[UserOut])
async def list_users(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_permission(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> Page[UserOut]:
    total = (await session.execute(sa.select(sa.func.count()).select_from(User))).scalar_one()
    rows = (
        await session.execute(
            sa.select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return Page(
        items=[UserOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.post(
    "",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("mutation"))],
)
async def create_user(
    request: Request,
    payload: UserCreate,
    actor: User = Depends(require_permission(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> User:
    ip, user_agent = request_context(request)
    email = payload.email.strip().lower()
    exists = (
        await session.execute(sa.select(User.id).where(User.email == email))
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("Користувач із такою поштою вже існує")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=payload.role.value,
    )
    session.add(user)
    await session.flush()
    await record_audit(
        session,
        action=AuditAction.USER_CREATED,
        user=actor,
        object_type="user",
        object_id=user.id,
        ip=ip,
        user_agent=user_agent,
        details={"email": email, "role": user.role},
    )
    await session.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    request: Request,
    user_id: uuid.UUID,
    payload: UserUpdate,
    actor: User = Depends(require_permission(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> User:
    ip, user_agent = request_context(request)
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("Користувача не знайдено")

    changes: dict[str, object] = {}
    revoke_reason: str | None = None

    if payload.full_name is not None:
        changes["full_name"] = payload.full_name
        user.full_name = payload.full_name
    if payload.role is not None:
        if user.id == actor.id and payload.role.value != actor.role:
            raise ValidationFailedError("Не можна змінити власну роль")
        if payload.role.value != user.role:
            changes["role"] = {"from": user.role, "to": payload.role.value}
            user.role = payload.role.value
            # Нова роль має набути чинності негайно: старий access-токен
            # більше не приймається, панель отримає токен уже з новими правами.
            revoke_reason = "role_changed"
    if payload.is_active is not None:
        if user.id == actor.id and not payload.is_active:
            raise ValidationFailedError("Не можна деактивувати власний обліковий запис")
        if payload.is_active != user.is_active:
            changes["is_active"] = payload.is_active
            user.is_active = payload.is_active
            if not payload.is_active:
                revoke_reason = "deactivated"

    if revoke_reason:
        await auth_service.revoke_all_sessions(session, user, reason=revoke_reason)
        await record_audit(
            session,
            action=AuditAction.SESSIONS_REVOKED,
            user=actor,
            object_type="user",
            object_id=user.id,
            ip=ip,
            user_agent=user_agent,
            details={"reason": revoke_reason, "target_email": user.email},
        )

    await record_audit(
        session,
        action=AuditAction.USER_UPDATED,
        user=actor,
        object_type="user",
        object_id=user.id,
        ip=ip,
        user_agent=user_agent,
        details={"changes": changes},
    )
    await session.commit()
    return user


@router.post(
    "/{user_id}/password",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("password", fail_closed=True))],
)
async def reset_password(
    request: Request,
    user_id: uuid.UUID,
    payload: PasswordReset,
    actor: User = Depends(require_permission(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    """Скидає пароль користувача й завершує всі його сесії."""
    ip, user_agent = request_context(request)
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("Користувача не знайдено")

    user.password_hash = hash_password(payload.new_password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    await auth_service.revoke_all_sessions(session, user, reason="password_reset")

    await record_audit(
        session,
        action=AuditAction.PASSWORD_RESET,
        user=actor,
        object_type="user",
        object_id=user.id,
        ip=ip,
        user_agent=user_agent,
        details={"target_email": user.email},
    )
    await session.commit()
    return MessageResponse(message="Пароль скинуто, усі сесії користувача завершено")


@router.post("/{user_id}/sessions/revoke", response_model=MessageResponse)
async def revoke_sessions(
    request: Request,
    user_id: uuid.UUID,
    actor: User = Depends(require_permission(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    """Примусово завершує всі сесії користувача, не змінюючи пароль."""
    ip, user_agent = request_context(request)
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("Користувача не знайдено")

    await auth_service.revoke_all_sessions(session, user, reason="revoked_by_admin")
    await record_audit(
        session,
        action=AuditAction.SESSIONS_REVOKED,
        user=actor,
        object_type="user",
        object_id=user.id,
        ip=ip,
        user_agent=user_agent,
        details={"reason": "revoked_by_admin", "target_email": user.email},
    )
    await session.commit()
    return MessageResponse(message="Сесії користувача завершено")


@router.delete("/{user_id}", response_model=MessageResponse)
async def delete_user(
    request: Request,
    user_id: uuid.UUID,
    actor: User = Depends(require_permission(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    ip, user_agent = request_context(request)
    if user_id == actor.id:
        raise ValidationFailedError("Не можна видалити власний обліковий запис")
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("Користувача не знайдено")

    email = user.email
    await session.delete(user)
    await record_audit(
        session,
        action=AuditAction.USER_DELETED,
        user=actor,
        object_type="user",
        object_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={"email": email},
    )
    await session.commit()
    return MessageResponse(message="Користувача видалено")
