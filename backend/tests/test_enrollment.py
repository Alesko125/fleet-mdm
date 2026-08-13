import sqlalchemy as sa

from app.core.security import challenge_message
from app.db.models import AuditLog, Device, DeviceTelemetry, EnrollmentToken, LocationSettings
from app.services.audit import AuditAction

DEVICE_INFO = {
    "manufacturer": "Samsung",
    "model": "SM-A536B",
    "android_version": "14",
    "sdk_int": 34,
    "kernel_version": "5.10.101-android13",
    "android_id": "a1b2c3d4e5f60718",
    "management_mode": "device_owner",
    "agent_version": "0.1.0",
}


async def _create_token(client, headers, **kwargs) -> dict:
    response = await client.post("/api/v1/enrollment/tokens", headers=headers, json=kwargs)
    assert response.status_code == 201, response.text
    return response.json()


async def test_token_is_returned_once_and_stored_hashed(client, auth_headers, db):
    headers = await auth_headers()
    created = await _create_token(client, headers, label="Тестова партія")

    assert created["token"].startswith("FLEET-")
    assert created["token_prefix"].endswith("…")
    assert created["qr_payload"]

    stored = (await db.execute(sa.select(EnrollmentToken))).scalars().one()
    assert created["token"] not in stored.token_hash
    assert len(stored.token_hash) == 64

    listing = await client.get("/api/v1/enrollment/tokens", headers=headers)
    assert listing.status_code == 200
    assert "token" not in listing.json()["items"][0]


async def test_enroll_device_creates_records(client, auth_headers, db, device_key):
    headers = await auth_headers()
    created = await _create_token(client, headers)

    response = await client.post(
        "/api/v1/enroll",
        json={
            "token": created["token"],
            "public_key": device_key.public_key_b64,
            "device": DEVICE_INFO,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["device_name"] == "Samsung SM-A536B"
    assert body["websocket_url"].endswith("/ws/device")
    assert "керується адміністратором" in body["transparency_notice"]

    device = (await db.execute(sa.select(Device))).scalars().one()
    assert device.status == "active"
    assert device.management_mode == "device_owner"
    assert device.key_fingerprint

    telemetry = (await db.execute(sa.select(DeviceTelemetry))).scalars().one()
    assert telemetry.kernel_version == DEVICE_INFO["kernel_version"]

    # Геолокація завжди починається вимкненою.
    location = (await db.execute(sa.select(LocationSettings))).scalars().one()
    assert location.enabled is False

    token = (await db.execute(sa.select(EnrollmentToken))).scalars().one()
    assert token.use_count == 1

    audit = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.DEVICE_ENROLLED))
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].actor_type == "device"


async def test_token_is_single_use_by_default(client, auth_headers, device_key):
    from tests.conftest import DeviceKey

    headers = await auth_headers()
    created = await _create_token(client, headers)

    first = await client.post(
        "/api/v1/enroll",
        json={
            "token": created["token"],
            "public_key": device_key.public_key_b64,
            "device": DEVICE_INFO,
        },
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/enroll",
        json={
            "token": created["token"],
            "public_key": DeviceKey().public_key_b64,
            "device": {**DEVICE_INFO, "android_id": "ffffffffffffffff"},
        },
    )
    assert second.status_code == 401


async def test_unknown_token_is_rejected_and_audited(client, db, device_key):
    response = await client.post(
        "/api/v1/enroll",
        json={
            "token": "FLEET-AAAA-BBBB-CCCC-DDDD-EEEE",
            "public_key": device_key.public_key_b64,
            "device": DEVICE_INFO,
        },
    )
    assert response.status_code == 401

    rows = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.DEVICE_ENROLL_FAILED)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].details["reason"] == "unknown_token"


async def test_revoked_token_cannot_enroll(client, auth_headers, device_key):
    headers = await auth_headers()
    created = await _create_token(client, headers)

    revoke = await client.post(
        f"/api/v1/enrollment/tokens/{created['id']}/revoke", headers=headers
    )
    assert revoke.status_code == 200

    response = await client.post(
        "/api/v1/enroll",
        json={
            "token": created["token"],
            "public_key": device_key.public_key_b64,
            "device": DEVICE_INFO,
        },
    )
    assert response.status_code == 401


async def test_duplicate_android_id_rejected(client, auth_headers, device_key):
    from tests.conftest import DeviceKey

    headers = await auth_headers()
    first_token = await _create_token(client, headers)
    second_token = await _create_token(client, headers)

    assert (
        await client.post(
            "/api/v1/enroll",
            json={
                "token": first_token["token"],
                "public_key": device_key.public_key_b64,
                "device": DEVICE_INFO,
            },
        )
    ).status_code == 201

    duplicate = await client.post(
        "/api/v1/enroll",
        json={
            "token": second_token["token"],
            "public_key": DeviceKey().public_key_b64,
            "device": DEVICE_INFO,
        },
    )
    assert duplicate.status_code == 409


async def test_invalid_public_key_rejected(client, auth_headers):
    headers = await auth_headers()
    created = await _create_token(client, headers)
    response = await client.post(
        "/api/v1/enroll",
        json={
            "token": created["token"],
            "public_key": "bm90LWEtcmVhbC1rZXktYnV0LWxvbmctZW5vdWdoLXN0cmluZw==",
            "device": DEVICE_INFO,
        },
    )
    assert response.status_code == 422


async def test_device_challenge_response_flow(client, auth_headers, device_key, db):
    headers = await auth_headers()
    created = await _create_token(client, headers)
    enrolled = await client.post(
        "/api/v1/enroll",
        json={
            "token": created["token"],
            "public_key": device_key.public_key_b64,
            "device": DEVICE_INFO,
        },
    )
    device_id = enrolled.json()["device_id"]

    challenge = await client.post(
        "/api/v1/device/auth/challenge", json={"device_id": device_id}
    )
    assert challenge.status_code == 200
    nonce = challenge.json()["nonce"]

    signature = device_key.sign(challenge_message(device_id, nonce))
    token_response = await client.post(
        "/api/v1/device/auth/token",
        json={"device_id": device_id, "nonce": nonce, "signature": signature},
    )
    assert token_response.status_code == 200, token_response.text
    device_token = token_response.json()["access_token"]

    me = await client.get(
        "/api/v1/device/me", headers={"Authorization": f"Bearer {device_token}"}
    )
    assert me.status_code == 200
    assert me.json()["device_id"] == device_id

    audit = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.DEVICE_AUTH))
    ).scalars().all()
    assert len(audit) == 1


async def test_nonce_is_single_use(client, auth_headers, device_key):
    headers = await auth_headers()
    created = await _create_token(client, headers)
    enrolled = await client.post(
        "/api/v1/enroll",
        json={
            "token": created["token"],
            "public_key": device_key.public_key_b64,
            "device": DEVICE_INFO,
        },
    )
    device_id = enrolled.json()["device_id"]

    challenge = await client.post("/api/v1/device/auth/challenge", json={"device_id": device_id})
    nonce = challenge.json()["nonce"]
    signature = device_key.sign(challenge_message(device_id, nonce))
    payload = {"device_id": device_id, "nonce": nonce, "signature": signature}

    assert (await client.post("/api/v1/device/auth/token", json=payload)).status_code == 200
    assert (await client.post("/api/v1/device/auth/token", json=payload)).status_code == 401


async def test_wrong_signature_rejected(client, auth_headers, device_key, db):
    from tests.conftest import DeviceKey

    headers = await auth_headers()
    created = await _create_token(client, headers)
    enrolled = await client.post(
        "/api/v1/enroll",
        json={
            "token": created["token"],
            "public_key": device_key.public_key_b64,
            "device": DEVICE_INFO,
        },
    )
    device_id = enrolled.json()["device_id"]

    challenge = await client.post("/api/v1/device/auth/challenge", json={"device_id": device_id})
    nonce = challenge.json()["nonce"]
    impostor = DeviceKey().sign(challenge_message(device_id, nonce))

    response = await client.post(
        "/api/v1/device/auth/token",
        json={"device_id": device_id, "nonce": nonce, "signature": impostor},
    )
    assert response.status_code == 401

    rows = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.DEVICE_AUTH_FAILED)
        )
    ).scalars().all()
    assert rows[0].details["reason"] == "bad_signature"


async def test_device_appears_in_device_list(client, auth_headers, device_key):
    headers = await auth_headers()
    created = await _create_token(client, headers)
    await client.post(
        "/api/v1/enroll",
        json={
            "token": created["token"],
            "public_key": device_key.public_key_b64,
            "device": DEVICE_INFO,
        },
    )

    listing = await client.get("/api/v1/devices", headers=headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["name"] == "Samsung SM-A536B"
    assert item["model"] == "SM-A536B"
    assert item["online"] is False
