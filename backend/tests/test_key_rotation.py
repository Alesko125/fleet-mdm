"""Ротація облікових даних пристрою.

Головне, що тут перевіряється: замінити ключ може лише той, хто володіє
чинним ключем, і після заміни старі токени вмирають одразу.
"""

from __future__ import annotations

import base64

import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.rbac import Role
from app.core.security import key_rotation_message
from app.db.models import AuditLog, Device, DeviceKeyHistory
from app.services.audit import AuditAction
from tests.conftest import DeviceKey


async def _challenge(client, device) -> str:
    response = await client.post("/api/v1/device/key/challenge", headers=device.headers)
    assert response.status_code == 200, response.text
    return response.json()["nonce"]


async def _rotate(client, device, new_key: DeviceKey, *, signer: DeviceKey | None = None):
    nonce = await _challenge(client, device)
    message = key_rotation_message(device.id, new_key.public_key_b64, nonce)
    signature = (signer or device.key).sign(message)
    return await client.post(
        "/api/v1/device/key/rotate",
        headers=device.headers,
        json={
            "new_public_key": new_key.public_key_b64,
            "nonce": nonce,
            "signature": signature,
            "reason": "scheduled",
        },
    )


async def test_rotation_replaces_key_and_records_history(client, device, db):
    new_key = DeviceKey()
    response = await _rotate(client, device, new_key)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["key_generation"] == 2
    assert body["reauthentication_required"] is True

    row = await db.get(Device, __import__("uuid").UUID(device.id))
    await db.refresh(row)
    assert row.public_key == new_key.public_key_b64
    assert row.key_generation == 2
    assert row.key_rotated_at is not None

    history = (
        await db.execute(
            sa.select(DeviceKeyHistory).where(DeviceKeyHistory.device_id == row.id)
        )
    ).scalars().all()
    assert len(history) == 1
    assert history[0].public_key == device.key.public_key_b64
    # Приватної частини сервер не бачив і не зберігає — лише публічний ключ.
    assert not hasattr(history[0], "private_key")


async def test_old_device_token_dies_after_rotation(client, device):
    assert (await client.get("/api/v1/device/me", headers=device.headers)).status_code == 200

    assert (await _rotate(client, device, DeviceKey())).status_code == 200

    stale = await client.get("/api/v1/device/me", headers=device.headers)
    assert stale.status_code == 401
    assert "відкликано" in stale.json()["error"]["message"]


async def test_rotation_requires_signature_by_current_key(client, device, db):
    """Токен пристрою вкрали — але без старого ключа підмінити його не вийде."""
    impostor = DeviceKey()
    response = await _rotate(client, device, DeviceKey(), signer=impostor)
    assert response.status_code == 401

    rows = (
        await db.execute(
            sa.select(AuditLog).where(
                AuditLog.action == AuditAction.DEVICE_KEY_ROTATION_FAILED
            )
        )
    ).scalars().all()
    assert any(r.details["reason"] == "bad_signature" for r in rows)


async def test_rotation_nonce_is_single_use(client, device):
    new_key = DeviceKey()
    nonce = await _challenge(client, device)
    message = key_rotation_message(device.id, new_key.public_key_b64, nonce)
    payload = {
        "new_public_key": new_key.public_key_b64,
        "nonce": nonce,
        "signature": device.key.sign(message),
        "reason": "scheduled",
    }
    first = await client.post(
        "/api/v1/device/key/rotate", headers=device.headers, json=payload
    )
    assert first.status_code == 200

    replay = await client.post(
        "/api/v1/device/key/rotate", headers=device.headers, json=payload
    )
    assert replay.status_code in (401, 409)


async def test_auth_nonce_cannot_be_used_for_rotation(client, device):
    """Nonce автентифікації і nonce ротації живуть у різних просторах імен."""
    challenge = await client.post(
        "/api/v1/device/auth/challenge", json={"device_id": device.id}
    )
    nonce = challenge.json()["nonce"]

    new_key = DeviceKey()
    message = key_rotation_message(device.id, new_key.public_key_b64, nonce)
    response = await client.post(
        "/api/v1/device/key/rotate",
        headers=device.headers,
        json={
            "new_public_key": new_key.public_key_b64,
            "nonce": nonce,
            "signature": device.key.sign(message),
            "reason": "scheduled",
        },
    )
    assert response.status_code == 401


async def test_cannot_rotate_to_same_key(client, device):
    response = await _rotate(client, device, device.key)
    assert response.status_code == 422


async def test_retired_key_cannot_be_reused(client, device, admin_headers):
    """Відкликаний ключ не можна ані повернути, ані завести з ним новий пристрій."""
    old_public = device.key.public_key_b64
    assert (await _rotate(client, device, DeviceKey())).status_code == 200

    token = (
        await client.post("/api/v1/enrollment/tokens", headers=admin_headers, json={})
    ).json()["token"]
    response = await client.post(
        "/api/v1/enroll",
        json={
            "token": token,
            "public_key": old_public,
            "device": {"android_id": "ffffffffffffffff", "management_mode": "unmanaged"},
        },
    )
    assert response.status_code == 409
    assert "відкликано" in response.json()["error"]["message"]


async def test_non_p256_key_is_rejected(client, device):
    """Крива інша за P-256 — не приймається навіть із коректним підписом."""
    weak = ec.generate_private_key(ec.SECP192R1())
    der = weak.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key = base64.b64encode(der).decode()

    nonce = await _challenge(client, device)
    message = key_rotation_message(device.id, public_key, nonce)
    response = await client.post(
        "/api/v1/device/key/rotate",
        headers=device.headers,
        json={
            "new_public_key": public_key,
            "nonce": nonce,
            "signature": device.key.sign(message),
            "reason": "scheduled",
        },
    )
    assert response.status_code == 422


# --------------------------------------------------- керування з панелі ---


async def test_admin_can_force_rotation(client, device, admin_headers):
    response = await client.post(
        f"/api/v1/devices/{device.id}/credentials/rotate", headers=admin_headers
    )
    assert response.status_code == 200

    # Токени відкликано — агент муситиме автентифікуватись наново.
    assert (await client.get("/api/v1/device/me", headers=device.headers)).status_code == 401


async def test_operator_cannot_force_rotation(client, device, auth_headers):
    headers = await auth_headers(email="op@example.com", role=Role.OPERATOR)
    response = await client.post(
        f"/api/v1/devices/{device.id}/credentials/rotate", headers=headers
    )
    assert response.status_code == 403
    assert "device:credential:rotate" in response.json()["error"]["message"]


async def test_credentials_endpoint_reports_state(client, device, admin_headers):
    response = await client.get(
        f"/api/v1/devices/{device.id}/credentials", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["key_algorithm"] == "EC_P256"
    assert body["key_generation"] == 1
    assert body["rotation_due"] is False
    assert len(body["key_fingerprint"]) == 64


async def test_rotation_due_when_key_too_old(client, device, admin_headers, db):
    from datetime import timedelta

    from app.core.security import utcnow

    row = await db.get(Device, __import__("uuid").UUID(device.id))
    row.key_created_at = utcnow() - timedelta(days=400)
    await db.commit()

    body = (
        await client.get(f"/api/v1/devices/{device.id}/credentials", headers=admin_headers)
    ).json()
    assert body["rotation_due"] is True
    assert body["key_age_days"] >= 400

    overview = (
        await client.get("/api/v1/maintenance/device-keys", headers=admin_headers)
    ).json()
    assert overview["rotation_due"] == 1


async def test_suspending_device_revokes_its_tokens(client, device, admin_headers):
    assert (await client.get("/api/v1/device/me", headers=device.headers)).status_code == 200

    response = await client.patch(
        f"/api/v1/devices/{device.id}", headers=admin_headers, json={"status": "suspended"}
    )
    assert response.status_code == 200
    assert (await client.get("/api/v1/device/me", headers=device.headers)).status_code == 401


async def test_secret_rotation_status_never_leaks_secrets(client, admin_headers):
    from app.core.config import get_settings

    response = await client.get("/api/v1/maintenance/secrets", headers=admin_headers)
    assert response.status_code == 200

    settings = get_settings()
    body = response.text
    assert settings.jwt_secret not in body
    assert settings.token_hash_secret not in body

    data = response.json()
    assert data["jwt"]["previous_secret_configured"] is False
    assert "procedure" in data


async def test_secret_status_requires_superadmin(client, auth_headers):
    headers = await auth_headers(email="admin2@example.com", role=Role.ADMIN)
    response = await client.get("/api/v1/maintenance/secrets", headers=headers)
    assert response.status_code == 403
