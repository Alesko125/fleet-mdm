"""Спільні залежності FastAPI: поточний користувач, пристрій, перевірка прав."""

from __future__ import annotations

import uuid

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.rate_limit import client_ip
from app.core.rbac import Permission, Role, role_has_permission
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_DEVICE,
    TokenError,
    as_utc,
    csrf_tokens_match,
    decode_token,
    token_predates,
)
from app.db.models import Device, User
from app.db.session import get_session
from app.services.audit import RESULT_DENIED, AuditAction, record_audit

bearer_scheme = HTTPBearer(auto_error=False)

# Методи, що не змінюють стан, — CSRF для них не потрібен.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def request_context(request: Request) -> tuple[str | None, str | None]:
    return client_ip(request), request.headers.get("user-agent")


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Потрібен токен доступу")
    try:
        claims = decode_token(credentials.credentials, expected_type=TOKEN_TYPE_ACCESS)
        user_id = uuid.UUID(str(claims["sub"]))
    except (TokenError, KeyError, ValueError) as exc:
        raise AuthenticationError("Некоректний токен доступу") from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Обліковий запис недоступний")

    # Вихід, зміна пароля чи ролі зсувають `sessions_valid_from` — і вже
    # видані access-токени перестають діяти негайно, не чекаючи свого exp.
    if token_predates(claims, as_utc(user.sessions_valid_from)):
        raise AuthenticationError("Сесію завершено — увійдіть заново")

    # Роль у токені лише довідкова: авторитетне значення завжди з БД.
    request.state.user = user
    return user


def require_csrf(request: Request) -> None:
    """Double-submit cookie для запитів, автентифікованих через cookie.

    Bearer-запити CSRF не потребують (заголовок не додається браузером
    автоматично), тож перевіряємо лише там, де використовується cookie.
    """
    if request.method in SAFE_METHODS:
        return
    settings = get_settings()
    cookie = request.cookies.get(settings.refresh_cookie_name)
    if not cookie:
        return
    if not csrf_tokens_match(
        request.cookies.get(settings.csrf_cookie_name),
        request.headers.get(settings.csrf_header_name),
    ):
        raise PermissionDeniedError(
            "Не пройдено перевірку CSRF: надішліть заголовок "
            f"{settings.csrf_header_name}"
        )


def require_permission(permission: Permission):
    """Залежність, що вимагає конкретного дозволу; відмова пишеться в аудит."""

    async def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> User:
        require_csrf(request)
        if not role_has_permission(user.role, permission):
            ip, user_agent = request_context(request)
            await record_audit(
                session,
                action=AuditAction.ACCESS_DENIED,
                result=RESULT_DENIED,
                user=user,
                ip=ip,
                user_agent=user_agent,
                details={
                    "permission": permission.value,
                    "role": user.role,
                    "path": request.url.path,
                    "method": request.method,
                },
            )
            await session.commit()
            raise PermissionDeniedError(
                f"Недостатньо прав: потрібен дозвіл {permission.value}"
            )
        return user

    return dependency


def require_role(*roles: Role):
    async def dependency(request: Request, user: User = Depends(get_current_user)) -> User:
        require_csrf(request)
        if Role(user.role) not in roles:
            raise PermissionDeniedError("Недостатньо прав для цієї дії")
        return user

    return dependency


async def get_current_device(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> Device:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Потрібен токен пристрою")
    try:
        claims = decode_token(credentials.credentials, expected_type=TOKEN_TYPE_DEVICE)
        device_id = uuid.UUID(str(claims["sub"]))
    except (TokenError, KeyError, ValueError) as exc:
        raise AuthenticationError("Некоректний токен пристрою") from exc

    device = await session.get(Device, device_id)
    if device is None or device.deleted_at is not None:
        raise AuthenticationError("Пристрій не знайдено")
    if device.status != "active":
        raise AuthenticationError("Пристрій деактивовано")

    # Ротація ключа або призупинення пристрою анулюють видані токени негайно.
    if token_predates(claims, as_utc(device.tokens_valid_from)):
        raise AuthenticationError(
            "Токен пристрою відкликано — потрібна повторна авторизація"
        )

    # Токен виданий саме цьому пристрою (scope дублює sub — звіряємо обидва).
    scope = str(claims.get("scope", ""))
    if scope and scope != f"device:{device.id}":
        raise AuthenticationError("Токен пристрою має некоректну область дії")

    request.state.device = device
    return device
