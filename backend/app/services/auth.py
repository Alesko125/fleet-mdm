"""Автентифікація операторів: вхід, видача та ротація токенів."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.security import (
    TOKEN_TYPE_REFRESH,
    TokenError,
    as_utc,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    token_fingerprint,
    token_fingerprints,
    utcnow,
    verify_password,
)
from app.db.models import RefreshToken, User
from app.services.audit import RESULT_FAILURE, AuditAction, record_audit


@dataclass(slots=True)
class TokenPairResult:
    access_token: str
    refresh_token: str
    expires_in: int
    user: User


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    stmt = sa.select(User).where(User.email == email.strip().lower())
    return (await session.execute(stmt)).scalar_one_or_none()


async def authenticate(
    session: AsyncSession, *, email: str, password: str, ip: str | None, user_agent: str | None
) -> User:
    settings = get_settings()
    user = await get_user_by_email(session, email)
    generic_error = AuthenticationError("Невірна пошта або пароль")

    if user is None:
        # Аудит без розкриття існування облікового запису у відповіді.
        await record_audit(
            session,
            action=AuditAction.LOGIN_FAILED,
            result=RESULT_FAILURE,
            actor_label=email.strip().lower()[:200],
            ip=ip,
            user_agent=user_agent,
            details={"reason": "unknown_user"},
        )
        await session.commit()
        raise generic_error

    if not user.is_active:
        await record_audit(
            session,
            action=AuditAction.LOGIN_FAILED,
            result=RESULT_FAILURE,
            user=user,
            ip=ip,
            user_agent=user_agent,
            details={"reason": "inactive"},
        )
        await session.commit()
        raise AuthenticationError("Обліковий запис деактивовано")

    if user.is_locked:
        await record_audit(
            session,
            action=AuditAction.LOGIN_FAILED,
            result=RESULT_FAILURE,
            user=user,
            ip=ip,
            user_agent=user_agent,
            details={"reason": "locked", "locked_until": user.locked_until},
        )
        await session.commit()
        raise AuthenticationError("Обліковий запис тимчасово заблоковано")

    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        reason = "bad_password"
        if user.failed_login_count >= settings.max_failed_logins:
            user.locked_until = utcnow() + timedelta(seconds=settings.account_lock_seconds)
            user.failed_login_count = 0
            reason = "locked_out"
        await record_audit(
            session,
            action=AuditAction.LOGIN_FAILED,
            result=RESULT_FAILURE,
            user=user,
            ip=ip,
            user_agent=user_agent,
            details={"reason": reason},
        )
        await session.commit()
        raise generic_error

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    await record_audit(
        session, action=AuditAction.LOGIN, user=user, ip=ip, user_agent=user_agent
    )
    return user


async def issue_token_pair(
    session: AsyncSession,
    user: User,
    *,
    ip: str | None,
    user_agent: str | None,
    family_id: uuid.UUID | None = None,
    replaces: RefreshToken | None = None,
) -> TokenPairResult:
    settings = get_settings()
    family = family_id or uuid.uuid4()
    access = create_access_token(subject=str(user.id), role=user.role)
    refresh = create_refresh_token(subject=str(user.id), family_id=family)

    row = RefreshToken(
        id=refresh.jti,
        user_id=user.id,
        family_id=family,
        token_hash=token_fingerprint(refresh.token),
        expires_at=refresh.expires_at,
        ip=ip,
        user_agent=(user_agent or "")[:400] or None,
    )
    session.add(row)
    if replaces is not None:
        replaces.replaced_by_id = row.id

    return TokenPairResult(
        access_token=access.token,
        refresh_token=refresh.token,
        expires_in=settings.access_token_ttl_seconds,
        user=user,
    )


async def _revoke_family(
    session: AsyncSession, family_id: uuid.UUID, reason: str
) -> None:
    await session.execute(
        sa.update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )


async def rotate_refresh_token(
    session: AsyncSession, raw_token: str, *, ip: str | None, user_agent: str | None
) -> TokenPairResult:
    try:
        claims = decode_token(raw_token, expected_type=TOKEN_TYPE_REFRESH)
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    # Під час ротації TOKEN_HASH_SECRET шукаємо і за новим, і за старим хешем.
    candidates = token_fingerprints(raw_token)
    stmt = sa.select(RefreshToken).where(RefreshToken.token_hash.in_(candidates))
    stored = (await session.execute(stmt)).scalar_one_or_none()

    if stored is None:
        # Валідний підпис, але запису немає — токен уже прибрано або підроблено.
        await record_audit(
            session,
            action=AuditAction.REFRESH_REUSE,
            result=RESULT_FAILURE,
            actor_label=str(claims.get("sub")),
            ip=ip,
            user_agent=user_agent,
            details={"reason": "unknown_token"},
        )
        await session.commit()
        raise AuthenticationError("Refresh-токен недійсний")

    user = await session.get(User, stored.user_id)

    if stored.revoked_at is not None:
        # Повторне використання анульованого токена → компрометація родини.
        await _revoke_family(session, stored.family_id, "reuse_detected")
        await record_audit(
            session,
            action=AuditAction.REFRESH_REUSE,
            result=RESULT_FAILURE,
            user=user,
            ip=ip,
            user_agent=user_agent,
            details={"family_id": str(stored.family_id), "token_id": str(stored.id)},
        )
        await session.commit()
        raise AuthenticationError("Refresh-токен уже було використано; сесію відкликано")

    if as_utc(stored.expires_at) <= utcnow():
        raise AuthenticationError("Термін дії refresh-токена вичерпано")

    if user is None or not user.is_active:
        await _revoke_family(session, stored.family_id, "user_inactive")
        await session.commit()
        raise AuthenticationError("Обліковий запис недоступний")

    stored.revoked_at = utcnow()
    stored.revoked_reason = "rotated"
    result = await issue_token_pair(
        session,
        user,
        ip=ip,
        user_agent=user_agent,
        family_id=stored.family_id,
        replaces=stored,
    )
    await record_audit(
        session,
        action=AuditAction.REFRESH,
        user=user,
        ip=ip,
        user_agent=user_agent,
        details={"family_id": str(stored.family_id)},
    )
    return result


async def logout(
    session: AsyncSession,
    raw_token: str | None,
    user: User,
    *,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """Анулює родину поточного refresh-токена (або всі сесії користувача)."""
    family_id: uuid.UUID | None = None
    if raw_token:
        stmt = sa.select(RefreshToken).where(
            RefreshToken.token_hash.in_(token_fingerprints(raw_token))
        )
        stored = (await session.execute(stmt)).scalar_one_or_none()
        if stored is not None and stored.user_id == user.id:
            family_id = stored.family_id

    if family_id is not None:
        await _revoke_family(session, family_id, "logout")
    else:
        await session.execute(
            sa.update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow(), revoked_reason="logout")
        )
        # Вихід із усіх сесій має вимикати й уже видані access-токени,
        # інакше вкрадений токен жив би ще до 15 хвилин після виходу.
        user.sessions_valid_from = utcnow()

    await record_audit(
        session,
        action=AuditAction.LOGOUT,
        user=user,
        ip=ip,
        user_agent=user_agent,
        details={"scope": "family" if family_id else "all"},
    )


async def revoke_all_sessions(
    session: AsyncSession, user: User, *, reason: str
) -> None:
    """Повне відкликання доступу користувача: і refresh-, і access-токени.

    Викликається при зміні пароля чи ролі, деактивації та примусовому
    завершенні сесій адміністратором.
    """
    await session.execute(
        sa.update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason[:64])
    )
    user.sessions_valid_from = utcnow()


async def prune_expired_refresh_tokens(session: AsyncSession) -> int:
    """Прибирає давно прострочені записи refresh-токенів."""
    result = await session.execute(
        sa.delete(RefreshToken).where(RefreshToken.expires_at < utcnow())
    )
    return int(result.rowcount or 0)
