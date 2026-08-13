"""Створення першого адміністратора (лише якщо таблиця users порожня)."""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.rbac import Role
from app.core.security import hash_password, validate_password_strength
from app.db.models import User
from app.services.audit import AuditAction, record_audit

logger = logging.getLogger(__name__)


async def ensure_bootstrap_admin(session: AsyncSession, settings: Settings) -> User | None:
    email = (settings.bootstrap_admin_email or "").strip().lower()
    password = settings.bootstrap_admin_password
    if not email or not password:
        return None

    existing = (await session.execute(sa.select(sa.func.count()).select_from(User))).scalar_one()
    if existing:
        return None

    problems = validate_password_strength(password)
    if problems:
        logger.error(
            "BOOTSTRAP_ADMIN_PASSWORD не відповідає вимогам — адміністратора не створено",
            extra={"problems": problems},
        )
        return None

    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name=settings.bootstrap_admin_name,
        role=Role.SUPERADMIN.value,
    )
    session.add(user)
    await session.flush()
    await record_audit(
        session,
        action=AuditAction.USER_CREATED,
        actor_label="bootstrap",
        object_type="user",
        object_id=user.id,
        details={"email": email, "role": user.role, "source": "bootstrap"},
    )
    await session.commit()
    logger.warning(
        "Створено початкового адміністратора — змініть пароль після першого входу",
        extra={"email": email},
    )
    return user
