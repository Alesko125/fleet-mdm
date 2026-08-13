import sqlalchemy as sa

from app.core.rbac import Role
from app.db.models import AuditLog, RefreshToken
from app.services.audit import AuditAction
from tests.conftest import TEST_PASSWORD


async def test_login_returns_token_pair(client, make_user):
    await make_user()
    response = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["role"] == "superadmin"
    assert body["expires_in"] > 0
    assert "device:read" in body["permissions"]

    # Refresh-токен не повертається в тілі — лише HttpOnly-cookie,
    # тож XSS у панелі не має до нього доступу.
    assert "refresh_token" not in body
    cookie = response.headers.get("set-cookie", "") + str(response.headers.get_list("set-cookie"))
    assert "fleet_refresh=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert client.cookies.get("fleet_refresh")


async def test_login_with_wrong_password_is_audited(client, make_user, db):
    await make_user()
    response = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "wrong-password-123"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"

    rows = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.LOGIN_FAILED))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].result == "failure"
    assert rows[0].details["reason"] == "bad_password"


async def test_login_unknown_user_does_not_leak_existence(client):
    response = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": TEST_PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Невірна пошта або пароль"


async def test_account_locks_after_repeated_failures(client, make_user, db):
    from app.core.config import get_settings

    get_settings().max_failed_logins = 3
    await make_user()
    for _ in range(3):
        await client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": "nope-nope-1234"}
        )
    response = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": TEST_PASSWORD}
    )
    assert response.status_code == 401
    assert "заблоковано" in response.json()["error"]["message"]


async def test_me_requires_token(client):
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_returns_profile(client, auth_headers):
    headers = await auth_headers()
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["email"] == "admin@example.com"
    assert response.json()["role"] == "superadmin"


async def test_refresh_rotates_token(client, make_user, login, db):
    await make_user()
    tokens = await login()

    response = await client.post("/api/v1/auth/refresh", headers=tokens["csrf_headers"])
    assert response.status_code == 200, response.text
    assert client.cookies.get("fleet_refresh") != tokens["refresh_cookie"]

    rows = (await db.execute(sa.select(RefreshToken))).scalars().all()
    assert len(rows) == 2
    revoked = [r for r in rows if r.revoked_at is not None]
    assert len(revoked) == 1
    assert revoked[0].revoked_reason == "rotated"
    assert revoked[0].replaced_by_id is not None


async def test_refresh_reuse_revokes_whole_family(client, make_user, login, db):
    await make_user()
    tokens = await login()
    first = await client.post("/api/v1/auth/refresh", headers=tokens["csrf_headers"])
    assert first.status_code == 200
    rotated_cookie = client.cookies.get("fleet_refresh")

    def replay(raw_token: str):
        """Повтор старого токена: cookie прибираємо, токен шлемо тілом."""
        client.cookies.clear()
        return client.post("/api/v1/auth/refresh", json={"refresh_token": raw_token})

    # Повторне використання вже витраченого токена.
    assert (await replay(tokens["refresh_cookie"])).status_code == 401

    rows = (await db.execute(sa.select(RefreshToken))).scalars().all()
    assert all(r.revoked_at is not None for r in rows)
    assert any(r.revoked_reason == "reuse_detected" for r in rows)

    audit = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.REFRESH_REUSE))
    ).scalars().all()
    assert len(audit) == 1

    # Свіжий токен із скомпрометованої родини теж більше не працює.
    assert (await replay(rotated_cookie)).status_code == 401


async def test_logout_revokes_session(client, make_user, login, db):
    await make_user()
    tokens = await login()

    response = await client.post("/api/v1/auth/logout", headers=tokens["auth_headers"])
    assert response.status_code == 200
    assert not client.cookies.get("fleet_refresh")

    refresh = await client.post(
        "/api/v1/auth/refresh",
        headers=tokens["csrf_headers"],
        json={"refresh_token": tokens["refresh_cookie"]},
    )
    assert refresh.status_code == 401

    audit = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.LOGOUT))
    ).scalars().all()
    assert len(audit) == 1


async def test_inactive_user_cannot_login(client, make_user):
    await make_user(email="disabled@example.com", role=Role.OPERATOR, is_active=False)
    response = await client.post(
        "/api/v1/auth/login", json={"email": "disabled@example.com", "password": TEST_PASSWORD}
    )
    assert response.status_code == 401
    assert "деактивовано" in response.json()["error"]["message"]


async def test_password_change_invalidates_sessions(client, make_user, login):
    await make_user()
    tokens = await login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = await client.post(
        "/api/v1/auth/password",
        headers=tokens["auth_headers"],
        json={"current_password": TEST_PASSWORD, "new_password": "N0vyiParol!2026Fleet"},
    )
    assert response.status_code == 200

    refresh = await client.post(
        "/api/v1/auth/refresh",
        headers=tokens["csrf_headers"],
        json={"refresh_token": tokens["refresh_cookie"]},
    )
    assert refresh.status_code == 401

    # Головне: вже виданий access-токен теж перестає діяти негайно.
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401

    assert (
        await client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "N0vyiParol!2026Fleet"},
        )
    ).status_code == 200


async def test_weak_password_rejected(client, auth_headers):
    headers = await auth_headers()
    response = await client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "weak@example.com", "password": "123456789012", "role": "operator"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_csrf_required_when_cookie_present(client, make_user, login):
    """Cookie надсилається браузером сама — тож без заголовка CSRF відмова."""
    await make_user()
    await login()
    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_csrf_mismatch_is_rejected(client, make_user, login):
    await make_user()
    await login()
    response = await client.post(
        "/api/v1/auth/refresh", headers={"X-CSRF-Token": "pidrobleniy-token"}
    )
    assert response.status_code == 403


async def test_logout_all_kills_access_token(client, make_user, login):
    await make_user()
    tokens = await login()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    assert (
        await client.post("/api/v1/auth/logout-all", headers=tokens["auth_headers"])
    ).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_role_change_revokes_existing_access_token(client, make_user, login, auth_headers):
    """Зниження ролі має діяти негайно, а не за 15 хвилин."""
    admin = await auth_headers()
    victim = await make_user(email="op@example.com", role=Role.ADMIN)
    victim_tokens = await login(email="op@example.com")
    victim_headers = {"Authorization": f"Bearer {victim_tokens['access_token']}"}

    assert (await client.get("/api/v1/auth/me", headers=victim_headers)).status_code == 200

    response = await client.patch(
        f"/api/v1/users/{victim.id}", headers=admin, json={"role": "auditor"}
    )
    assert response.status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=victim_headers)).status_code == 401


async def test_me_exposes_permission_matrix(client, auth_headers):
    headers = await auth_headers(email="auditor@example.com", role=Role.AUDITOR)
    body = (await client.get("/api/v1/auth/me", headers=headers)).json()
    assert "audit:read" in body["permissions"]
    assert "device:write" not in body["permissions"]


async def test_security_headers_present(client):
    response = await client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"


async def test_oversized_json_body_is_rejected(client, make_user):
    await make_user()
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "x" * 2_000_000},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_unsupported_content_type_is_rejected(client):
    response = await client.post(
        "/api/v1/auth/login", content=b"<xml/>", headers={"Content-Type": "application/xml"}
    )
    assert response.status_code == 415
