"""DTO автентифікації операторів."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.rbac import Role
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    """Refresh-токен штатно приходить у HttpOnly-cookie.

    Поле лишається необовʼязковим для не-браузерних клієнтів (скрипти,
    CI); браузерна панель його не використовує і не має до нього доступу.
    """

    refresh_token: str | None = Field(default=None, min_length=16, max_length=4096)


class TokenPair(BaseModel):
    """Відповідь автентифікації.

    `refresh_token` тут навмисно відсутній: він віддається виключно
    HttpOnly-cookie, тож XSS не може його прочитати чи вивезти.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: Role
    permissions: list[str] = Field(default_factory=list)
    csrf_token: str | None = None


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: Role
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class CurrentUser(UserOut):
    """`/auth/me`: профіль разом із матрицею дозволів для UI."""

    permissions: list[str] = Field(default_factory=list)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    full_name: str | None = Field(default=None, max_length=200)
    role: Role = Role.OPERATOR

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        from app.core.security import validate_password_strength

        problems = validate_password_strength(value)
        if problems:
            raise ValueError("Слабкий пароль: " + ", ".join(problems))
        return value


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    role: Role | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    """Скидання пароля адміністратором; усі сесії користувача завершуються."""

    new_password: str = Field(min_length=12, max_length=256)

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        from app.core.security import validate_password_strength

        problems = validate_password_strength(value)
        if problems:
            raise ValueError("Слабкий пароль: " + ", ".join(problems))
        return value


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        from app.core.security import validate_password_strength

        problems = validate_password_strength(value)
        if problems:
            raise ValueError("Слабкий пароль: " + ", ".join(problems))
        return value
