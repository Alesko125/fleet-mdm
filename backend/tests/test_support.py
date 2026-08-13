from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.core.rbac import Role
from app.db.models import AuditLog, RemoteSession
from app.services import support as support_service
from app.services.audit import AuditAction

REASON = "Користувач не може налаштувати пошту"


async def start_session(client, headers, device_id, **overrides):
    payload = {"reason": REASON, "input_enabled": False, **overrides}
    return await client.post(
        f"/api/v1/devices/{device_id}/support-sessions", headers=headers, json=payload
    )


async def grant_consent(client, device, session_id, **overrides):
    payload = {"granted": True, "input_available": False, **overrides}
    return await client.post(
        f"/api/v1/device/support-sessions/{session_id}/consent",
        headers=device.headers,
        json=payload,
    )


# ------------------------------------------------------------- створення --


async def test_session_starts_in_requested_state(client, admin_headers, device, db):
    response = await start_session(client, admin_headers, device.id)
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["state"] == "requested"
    assert body["reason"] == REASON
    assert body["consent_at"] is None
    assert body["started_at"] is None
    assert body["is_open"] is True
    assert body["duration_seconds"] is None
    assert body["join_secret"]
    assert body["viewer_url"].endswith(f"/ws/support/{body['id']}")
    assert body["expires_at"] is not None

    # Секрет зберігається лише як хеш.
    stored = (await db.execute(sa.select(RemoteSession))).scalars().one()
    assert stored.join_secret_hash != body["join_secret"]
    assert len(stored.join_secret_hash) == 64

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.REMOTE_SESSION_REQUESTED)
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].details["reason"] == REASON


async def test_request_is_delivered_to_device_as_command(client, admin_headers, device):
    created = (await start_session(client, admin_headers, device.id)).json()

    pending = await client.get("/api/v1/device/commands/pending", headers=device.headers)
    commands = pending.json()
    assert [c["type"] for c in commands] == ["REMOTE_SUPPORT_REQUEST"]
    payload = commands[0]["payload"]
    assert payload["session_id"] == created["id"]
    assert payload["reason"] == REASON
    assert payload["requested_by"] == "admin@example.com"
    assert payload["join_secret"] == created["join_secret"]


async def test_reason_is_required(client, admin_headers, device):
    response = await client.post(
        f"/api/v1/devices/{device.id}/support-sessions",
        headers=admin_headers,
        json={"reason": "ні"},
    )
    assert response.status_code == 422


async def test_only_one_open_session_per_device(client, admin_headers, device):
    assert (await start_session(client, admin_headers, device.id)).status_code == 201
    second = await start_session(client, admin_headers, device.id)
    assert second.status_code == 409
    assert "відкритий сеанс" in second.json()["error"]["message"]


# ----------------------------------------------------------------- згода --


async def test_consent_moves_session_forward(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()

    response = await grant_consent(client, device, created["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "awaiting_consent"
    assert body["consent_at"] is not None

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.REMOTE_SESSION_CONSENT)
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].result == "success"
    assert audit[0].actor_type == "device"


async def test_denied_consent_closes_session(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()

    response = await client.post(
        f"/api/v1/device/support-sessions/{created['id']}/consent",
        headers=device.headers,
        json={"granted": False, "reason": "Користувач відмовив"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "denied"
    assert response.json()["end_reason"] == "consent_denied"
    assert response.json()["is_open"] is False

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.REMOTE_SESSION_CONSENT)
        )
    ).scalars().all()
    assert audit[0].result == "denied"

    # Після відмови можна створити новий запит.
    assert (await start_session(client, admin_headers, device.id)).status_code == 201


async def test_repeated_consent_cannot_escalate_access(client, admin_headers, device, db):
    """Повтор відповіді (ретрай мережі) не змінює вже зафіксовану згоду."""
    created = (await start_session(client, admin_headers, device.id)).json()
    first = await grant_consent(client, device, created["id"], input_available=False)
    assert first.status_code == 200
    assert first.json()["input_enabled"] is False

    # Друга відповідь намагається «додати» керування — має бути проігнорована.
    second = await grant_consent(client, device, created["id"], input_available=True)
    assert second.status_code == 200
    assert second.json()["input_enabled"] is False
    assert second.json()["input_available"] is False
    # SQLite повертає час без таймзони, тож порівнюємо з точністю до секунди.
    assert second.json()["consent_at"][:19] == first.json()["consent_at"][:19]

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.REMOTE_SESSION_CONSENT)
        )
    ).scalars().all()
    assert len(audit) == 1


async def test_consent_after_denial_is_rejected(client, admin_headers, device):
    created = (await start_session(client, admin_headers, device.id)).json()
    await client.post(
        f"/api/v1/device/support-sessions/{created['id']}/consent",
        headers=device.headers,
        json={"granted": False},
    )
    late = await grant_consent(client, device, created["id"])
    assert late.status_code == 409


async def test_stream_without_consent_is_impossible(client, admin_headers, device, db):
    """Ключова межа модуля: `mark_started` без згоди кидає помилку."""
    from app.core.errors import PermissionDeniedError
    from app.db.models import Device

    created = (await start_session(client, admin_headers, device.id)).json()
    row = (await db.execute(sa.select(RemoteSession))).scalars().one()
    device_row = (await db.execute(sa.select(Device))).scalars().one()

    assert row.consent_at is None
    try:
        await support_service.mark_started(db, row=row, device=device_row)
        raised = False
    except PermissionDeniedError:
        raised = True
    assert raised is True
    assert row.state == "requested"

    _ = created


async def test_input_requires_device_support(client, admin_headers, device, db):
    created = (
        await start_session(client, admin_headers, device.id, input_enabled=True)
    ).json()
    assert created["input_enabled"] is True

    # Пристрій каже, що служба доступності недоступна → керування вимикається.
    response = await grant_consent(client, device, created["id"], input_available=False)
    assert response.json()["input_enabled"] is False
    assert response.json()["input_available"] is False

    stored = (await db.execute(sa.select(RemoteSession))).scalars().one()
    assert stored.input_enabled is False


async def test_input_enabled_when_supported(client, admin_headers, device):
    created = (
        await start_session(client, admin_headers, device.id, input_enabled=True)
    ).json()
    response = await grant_consent(client, device, created["id"], input_available=True)
    assert response.json()["input_enabled"] is True
    assert response.json()["input_available"] is True


# --------------------------------------------------------------- зупинка --


async def test_admin_stop_ends_session_and_notifies_device(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()
    await grant_consent(client, device, created["id"])

    response = await client.post(
        f"/api/v1/support-sessions/{created['id']}/stop",
        headers=admin_headers,
        json={"reason": "готово"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "ended"
    assert response.json()["is_open"] is False

    pending = await client.get("/api/v1/device/commands/pending", headers=device.headers)
    assert "REMOTE_SUPPORT_END" in [c["type"] for c in pending.json()]

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.REMOTE_SESSION_ENDED)
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].details["reason"] == "готово"


async def test_device_user_can_stop_session(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()
    await grant_consent(client, device, created["id"])

    response = await client.post(
        f"/api/v1/device/support-sessions/{created['id']}/end",
        headers=device.headers,
        json={"reason": "stopped_by_user"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "ended"
    assert response.json()["end_reason"] == "stopped_by_user"

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.REMOTE_SESSION_ENDED)
        )
    ).scalars().all()
    assert audit[0].actor_type == "device"


async def test_stopping_twice_is_idempotent(client, admin_headers, device):
    created = (await start_session(client, admin_headers, device.id)).json()
    first = await client.post(
        f"/api/v1/support-sessions/{created['id']}/stop", headers=admin_headers, json={}
    )
    second = await client.post(
        f"/api/v1/support-sessions/{created['id']}/stop", headers=admin_headers, json={}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["state"] == "ended"


# -------------------------------------------------------------- таймаути --


async def test_consent_timeout_expires_session(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()

    stale = datetime.now(timezone.utc) - timedelta(
        seconds=support_service.CONSENT_TIMEOUT_SECONDS + 60
    )
    await db.execute(sa.update(RemoteSession).values(created_at=stale))
    await db.commit()

    closed = await support_service.expire_stale(db)
    assert closed == 1

    detail = await client.get(
        f"/api/v1/support-sessions/{created['id']}", headers=admin_headers
    )
    assert detail.json()["state"] == "expired"
    assert detail.json()["end_reason"] == "consent_timeout"


async def test_max_duration_expires_active_session(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()
    await grant_consent(client, device, created["id"])

    await db.execute(
        sa.update(RemoteSession).values(
            state="active",
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            last_frame_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    assert await support_service.expire_stale(db) == 1
    detail = await client.get(
        f"/api/v1/support-sessions/{created['id']}", headers=admin_headers
    )
    assert detail.json()["state"] == "expired"
    assert detail.json()["end_reason"] == "max_duration_reached"


async def test_silent_stream_is_closed(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()
    await grant_consent(client, device, created["id"])

    await db.execute(
        sa.update(RemoteSession).values(
            state="active",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            last_frame_at=datetime.now(timezone.utc)
            - timedelta(seconds=support_service.FRAME_SILENCE_SECONDS + 30),
        )
    )
    await db.commit()

    assert await support_service.expire_stale(db) == 1
    detail = await client.get(
        f"/api/v1/support-sessions/{created['id']}", headers=admin_headers
    )
    assert detail.json()["state"] == "ended"
    assert detail.json()["end_reason"] == "stream_lost"


async def test_healthy_session_is_not_expired(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()
    await grant_consent(client, device, created["id"])
    await db.execute(
        sa.update(RemoteSession).values(
            state="active",
            started_at=datetime.now(timezone.utc),
            last_frame_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    assert await support_service.expire_stale(db) == 0
    _ = created


# ------------------------------------------------------- секрет і доступ --


async def test_join_secret_verification(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()
    row = (await db.execute(sa.select(RemoteSession))).scalars().one()

    assert support_service.verify_join_secret(row, created["join_secret"]) is True
    assert support_service.verify_join_secret(row, "not-the-secret") is False
    assert support_service.verify_join_secret(row, "") is False


async def test_secret_is_not_returned_in_listings(client, admin_headers, device):
    await start_session(client, admin_headers, device.id)

    listing = await client.get("/api/v1/support-sessions", headers=admin_headers)
    assert listing.status_code == 200
    item = listing.json()["items"][0]
    assert "join_secret" not in item
    assert item["device_name"] == "Samsung SM-A536B"
    assert item["requested_by_email"] == "admin@example.com"


async def test_device_cannot_touch_foreign_session(
    client, admin_headers, device, enroll_device
):
    created = (await start_session(client, admin_headers, device.id)).json()
    other = await enroll_device(android_id="ffffffffffffffff")

    response = await client.post(
        f"/api/v1/device/support-sessions/{created['id']}/consent",
        headers=other.headers,
        json={"granted": True},
    )
    assert response.status_code == 404

    status_response = await client.get(
        f"/api/v1/device/support-sessions/{created['id']}", headers=other.headers
    )
    assert status_response.status_code == 404


async def test_operator_can_run_session_auditor_cannot(client, auth_headers, device):
    operator = await auth_headers(email="operator@example.com", role=Role.OPERATOR)
    auditor = await auth_headers(email="auditor@example.com", role=Role.AUDITOR)

    created = await start_session(client, operator, device.id)
    assert created.status_code == 201

    denied = await client.post(
        f"/api/v1/support-sessions/{created.json()['id']}/stop", headers=auditor, json={}
    )
    assert denied.status_code == 403

    # Читати історію аудитор може.
    assert (await client.get("/api/v1/support-sessions", headers=auditor)).status_code == 200


async def test_anonymous_has_no_access(client, device):
    assert (
        await client.post(
            f"/api/v1/devices/{device.id}/support-sessions", json={"reason": REASON}
        )
    ).status_code == 401
    assert (await client.get("/api/v1/support-sessions")).status_code == 401


# ------------------------------------------------------------- статистика -


async def test_frame_counters_and_duration(client, admin_headers, device, db):
    created = (await start_session(client, admin_headers, device.id)).json()
    await grant_consent(client, device, created["id"])

    row = (await db.execute(sa.select(RemoteSession))).scalars().one()
    row.state = "active"
    row.started_at = datetime.now(timezone.utc) - timedelta(seconds=42)
    await support_service.touch_frames(
        row, frames=120, size_bytes=2_400_000, stats={"kbps": 900.0}
    )
    await db.commit()

    detail = (
        await client.get(f"/api/v1/support-sessions/{created['id']}", headers=admin_headers)
    ).json()
    assert detail["frames_sent"] == 120
    assert detail["bytes_sent"] == 2_400_000
    assert detail["stats"]["kbps"] == 900.0
    assert detail["duration_seconds"] >= 42


def test_quality_hint_adapts_to_bandwidth():
    slow = support_service.quality_hint(bytes_sent=20_000, elapsed_seconds=5)
    fast = support_service.quality_hint(bytes_sent=5_000_000, elapsed_seconds=5)

    assert slow["max_fps"] < fast["max_fps"]
    assert slow["jpeg_quality"] < fast["jpeg_quality"]
    assert slow["scale_percent"] <= fast["scale_percent"]
    assert 1 <= slow["max_fps"] <= 30


def test_quality_hint_handles_zero_elapsed():
    hint = support_service.quality_hint(bytes_sent=0, elapsed_seconds=0)
    assert hint["max_fps"] >= 1
    assert 10 <= hint["jpeg_quality"] <= 95


async def test_session_history_filters(client, admin_headers, device):
    created = (await start_session(client, admin_headers, device.id)).json()
    await client.post(
        f"/api/v1/support-sessions/{created['id']}/stop", headers=admin_headers, json={}
    )
    await start_session(client, admin_headers, device.id)

    ended = await client.get(
        "/api/v1/support-sessions", headers=admin_headers, params={"state": "ended"}
    )
    assert ended.json()["total"] == 1

    by_device = await client.get(
        "/api/v1/support-sessions", headers=admin_headers, params={"device_id": device.id}
    )
    assert by_device.json()["total"] == 2
