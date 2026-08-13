"""Спільні фікстури тестів.

БД — SQLite в памʼяті (моделі навмисно переносні), Redis — fakeredis.
"""

from __future__ import annotations

import base64
import os
import tempfile
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass

# Оточення має бути виставлене ДО імпорту застосунку (Settings кешується).
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/15",
        "JWT_SECRET": "test-jwt-secret-value-that-is-long-enough-000",
        "TOKEN_HASH_SECRET": "test-token-hash-secret-value-long-enough-000",
        "BOOTSTRAP_ADMIN_EMAIL": "",
        "BOOTSTRAP_ADMIN_PASSWORD": "",
        "LOG_LEVEL": "WARNING",
        "LOG_FORMAT": "console",
        "PUBLIC_BASE_URL": "http://testserver",
        # Сховище артефактів — окремий тимчасовий каталог на весь прогін.
        "STORAGE_ROOT": tempfile.mkdtemp(prefix="fleet-test-storage-"),
        "MAX_UPLOAD_MB": "8",
    }
)

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.rbac import Role  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.main import create_app  # noqa: E402

TEST_PASSWORD = "Sup3rSecretPass!2026"


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def app(session_factory, redis):
    application = create_app()
    application.state.redis = redis

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    application.dependency_overrides[get_session] = override_get_session
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture(autouse=True)
def reset_settings():
    """Повертає змінені в тестах ліміти до вихідних значень."""
    settings = get_settings()
    snapshot = {
        "rate_limit_enabled": settings.rate_limit_enabled,
        "rate_limit_login": settings.rate_limit_login,
        "rate_limit_enroll": settings.rate_limit_enroll,
        "rate_limit_device_auth": settings.rate_limit_device_auth,
        "max_failed_logins": settings.max_failed_logins,
    }
    yield
    for key, value in snapshot.items():
        setattr(settings, key, value)


# ------------------------------------------------------------ helpers -----


@pytest_asyncio.fixture
async def make_user(db):
    async def _make(
        email: str = "admin@example.com",
        role: Role = Role.SUPERADMIN,
        password: str = TEST_PASSWORD,
        is_active: bool = True,
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            email=email.lower(),
            password_hash=hash_password(password),
            full_name="Тестовий користувач",
            role=role.value,
            is_active=is_active,
        )
        db.add(user)
        await db.commit()
        return user

    return _make


@pytest_asyncio.fixture
async def login(client):
    """Вхід у панель.

    Refresh-токен приходить у HttpOnly-cookie (клієнт httpx тримає банку
    cookie сам), тож у відповіді його немає — повертаємо тіло плюс сирий
    refresh-токен, зчитаний із cookie, для перевірок ротації.
    """

    async def _login(email: str = "admin@example.com", password: str = TEST_PASSWORD) -> dict:
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        body["refresh_cookie"] = client.cookies.get("fleet_refresh")
        body["csrf_headers"] = {"X-CSRF-Token": body["csrf_token"]}
        body["auth_headers"] = {
            "Authorization": f"Bearer {body['access_token']}",
            "X-CSRF-Token": body["csrf_token"],
        }
        return body

    return _login


@pytest_asyncio.fixture
async def auth_headers(make_user, login):
    async def _headers(email: str = "admin@example.com", role: Role = Role.SUPERADMIN) -> dict:
        await make_user(email=email, role=role)
        tokens = await login(email=email)
        return dict(tokens["auth_headers"])

    return _headers


class DeviceKey:
    """Імітація ключа Android Keystore (EC P-256, SHA256withECDSA)."""

    def __init__(self) -> None:
        self._private = ec.generate_private_key(ec.SECP256R1())

    @property
    def public_key_b64(self) -> str:
        der = self._private.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(der).decode()

    def sign(self, message: bytes) -> str:
        signature = self._private.sign(message, ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(signature).decode()


@pytest.fixture
def device_key() -> DeviceKey:
    return DeviceKey()


@pytest_asyncio.fixture
async def admin_headers(auth_headers) -> dict:
    """Заголовки суперадміністратора (створюється один раз на тест)."""
    return await auth_headers()


@dataclass(slots=True)
class EnrolledDevice:
    id: str
    headers: dict
    key: DeviceKey


@pytest_asyncio.fixture
async def enroll_device(client, admin_headers):
    """Фабрика: створює enrollment-токен, реєструє пристрій, авторизує агента."""

    async def _enroll(android_id: str = "a1b2c3d4e5f60718", **info) -> EnrolledDevice:
        from app.core.security import challenge_message

        key = DeviceKey()
        token = (
            await client.post("/api/v1/enrollment/tokens", headers=admin_headers, json={})
        ).json()["token"]

        payload = {
            "manufacturer": "Samsung",
            "model": "SM-A536B",
            "android_version": "14",
            "sdk_int": 34,
            "android_id": android_id,
            "management_mode": "device_owner",
            "agent_version": "0.2.0",
            **info,
        }
        enrolled = await client.post(
            "/api/v1/enroll",
            json={"token": token, "public_key": key.public_key_b64, "device": payload},
        )
        assert enrolled.status_code == 201, enrolled.text
        device_id = enrolled.json()["device_id"]

        challenge = await client.post(
            "/api/v1/device/auth/challenge", json={"device_id": device_id}
        )
        nonce = challenge.json()["nonce"]
        signature = key.sign(challenge_message(device_id, nonce))
        token_response = await client.post(
            "/api/v1/device/auth/token",
            json={"device_id": device_id, "nonce": nonce, "signature": signature},
        )
        assert token_response.status_code == 200, token_response.text
        access = token_response.json()["access_token"]

        return EnrolledDevice(
            id=device_id, headers={"Authorization": f"Bearer {access}"}, key=key
        )

    return _enroll


@pytest_asyncio.fixture
async def device(enroll_device) -> EnrolledDevice:
    return await enroll_device()
