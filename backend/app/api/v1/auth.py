"""Автентифікація операторів.

Схема зберігання токенів:

* **access-токен** — лише в памʼяті вкладки, живе 15 хвилин, надсилається
  заголовком `Authorization`;
* **refresh-токен** — виключно у cookie `HttpOnly; Secure; SameSite=strict`,
  тож JavaScript (а отже і XSS) його не бачить;
* **CSRF-токен** — у звичайній cookie плюс заголовок `X-CSRF-Token`
  (double-submit), бо cookie браузер надсилає автоматично.

Вихід, зміна пароля чи ролі зсувають `users.sessions_valid_from` — і вже
видані access-токени перестають діяти негайно.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, request_context, require_csrf
from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.rate_limit import client_ip, rate_limit
from app.core.rbac import permission_names
from app.core.security import (
    generate_csrf_token,
    hash_password,
    utcnow,
    verify_password,
)
from app.db.models import User
from app.db.session import get_session
from app.schemas.auth import (
    CurrentUser,
    LoginRequest,
    PasswordChange,
    RefreshRequest,
    TokenPair,
)
from app.schemas.common import MessageResponse
from app.services import auth as auth_service
from app.services.audit import AuditAction, record_audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _login_identity(request: Request) -> str:
    return client_ip(request)


def _set_session_cookies(response: Response, refresh_token: str) -> str:
    """Кладе refresh- і CSRF-cookie. Повертає значення CSRF-токена."""
    settings = get_settings()
    csrf_token = generate_csrf_token()
    common = {
        "path": settings.cookie_path,
        "domain": settings.cookie_domain,
        "secure": settings.use_secure_cookies,
        "samesite": settings.cookie_samesite,
        "max_age": settings.refresh_token_ttl_seconds,
    }
    response.set_cookie(
        settings.refresh_cookie_name, refresh_token, httponly=True, **common
    )
    # CSRF-cookie навмисно читається скриптом: панель дублює її в заголовок.
    #
    # Шлях у неї ширший, ніж у refresh-cookie: браузер віддає `document.cookie`
    # лише для шляхів, що збігаються з поточною сторінкою, тож при
    # Path=/api/v1/auth панель на /devices не бачила б власного CSRF-токена —
    # заголовок не додавався б і будь-яке оновлення сторінки завершувалося 403.
    # На стійкість double-submit це не впливає: захист тримається на тому, що
    # стороннє походження не може ПРОЧИТАТИ значення, а не на шляху.
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        httponly=False,
        **{**common, "path": "/"},
    )
    return csrf_token


def _clear_session_cookies(response: Response) -> None:
    settings = get_settings()
    # Шлях має збігатися з тим, з яким cookie ставили, інакше браузер
    # видалить не ту cookie (або жодної).
    for name, path in (
        (settings.refresh_cookie_name, settings.cookie_path),
        (settings.csrf_cookie_name, "/"),
    ):
        response.delete_cookie(
            name,
            path=path,
            domain=settings.cookie_domain,
            secure=settings.use_secure_cookies,
            samesite=settings.cookie_samesite,
        )


def _refresh_token_from(request: Request, payload: RefreshRequest | None) -> str:
    """Cookie має пріоритет; тіло — запасний шлях для не-браузерних клієнтів."""
    settings = get_settings()
    token = request.cookies.get(settings.refresh_cookie_name)
    if not token and payload is not None:
        token = payload.refresh_token
    if not token:
        raise AuthenticationError("Refresh-токен відсутній")
    return token


@router.post(
    "/login",
    response_model=TokenPair,
    dependencies=[Depends(rate_limit("login", fail_closed=True, identity_fn=_login_identity))],
)
async def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    ip, user_agent = request_context(request)
    user = await auth_service.authenticate(
        session, email=payload.email, password=payload.password, ip=ip, user_agent=user_agent
    )
    pair = await auth_service.issue_token_pair(session, user, ip=ip, user_agent=user_agent)
    await session.commit()

    csrf_token = _set_session_cookies(response, pair.refresh_token)
    return TokenPair(
        access_token=pair.access_token,
        expires_in=pair.expires_in,
        role=user.role,
        permissions=permission_names(user.role),
        csrf_token=csrf_token,
    )


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[Depends(rate_limit("refresh"))],
)
async def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    require_csrf(request)
    ip, user_agent = request_context(request)
    raw_token = _refresh_token_from(request, payload)

    try:
        pair = await auth_service.rotate_refresh_token(
            session, raw_token, ip=ip, user_agent=user_agent
        )
    except AuthenticationError:
        # Недійсний токен — прибираємо cookie, щоб панель не крутила цикл.
        _clear_session_cookies(response)
        raise
    await session.commit()

    csrf_token = _set_session_cookies(response, pair.refresh_token)
    return TokenPair(
        access_token=pair.access_token,
        expires_in=pair.expires_in,
        role=pair.user.role,
        permissions=permission_names(pair.user.role),
        csrf_token=csrf_token,
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    require_csrf(request)
    ip, user_agent = request_context(request)
    settings = get_settings()
    raw_token = request.cookies.get(settings.refresh_cookie_name) or (
        payload.refresh_token if payload else None
    )

    await auth_service.logout(session, raw_token, user, ip=ip, user_agent=user_agent)
    await session.commit()
    _clear_session_cookies(response)
    return MessageResponse(message="Сесію завершено")


@router.post("/logout-all", response_model=MessageResponse)
async def logout_everywhere(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    """Завершує всі сесії користувача на всіх пристроях."""
    require_csrf(request)
    ip, user_agent = request_context(request)
    await auth_service.revoke_all_sessions(session, user, reason="logout_all")
    await record_audit(
        session,
        action=AuditAction.LOGOUT,
        user=user,
        object_type="user",
        object_id=user.id,
        ip=ip,
        user_agent=user_agent,
        details={"scope": "all_devices"},
    )
    await session.commit()
    _clear_session_cookies(response)
    return MessageResponse(message="Усі сесії завершено")


@router.get("/me", response_model=CurrentUser)
async def me(user: User = Depends(get_current_user)) -> CurrentUser:
    return CurrentUser(
        **CurrentUser.model_validate(user).model_dump(exclude={"permissions"}),
        permissions=permission_names(user.role),
    )


@router.post(
    "/password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(rate_limit("password", fail_closed=True))],
)
async def change_password(
    request: Request,
    response: Response,
    payload: PasswordChange,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageResponse:
    require_csrf(request)
    ip, user_agent = request_context(request)
    if not verify_password(payload.current_password, user.password_hash):
        await record_audit(
            session,
            action=AuditAction.PASSWORD_CHANGE_FAILED,
            result="failure",
            user=user,
            object_type="user",
            object_id=user.id,
            ip=ip,
            user_agent=user_agent,
        )
        await session.commit()
        raise AuthenticationError("Поточний пароль невірний")

    if verify_password(payload.new_password, user.password_hash):
        raise AuthenticationError("Новий пароль має відрізнятися від поточного")

    user.password_hash = hash_password(payload.new_password)
    user.password_changed_at = utcnow()
    await auth_service.revoke_all_sessions(session, user, reason="password_changed")
    await record_audit(
        session,
        action=AuditAction.PASSWORD_CHANGED,
        user=user,
        object_type="user",
        object_id=user.id,
        ip=ip,
        user_agent=user_agent,
    )
    await session.commit()
    _clear_session_cookies(response)
    return MessageResponse(message="Пароль змінено, усі сесії завершено")
