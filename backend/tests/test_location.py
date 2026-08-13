from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.core.rbac import Role
from app.db.models import AuditLog, LocationFix, LocationSettings
from app.services.audit import AuditAction

KYIV = (50.4501, 30.5234)


def fix_payload(lat=KYIV[0], lon=KYIV[1], minutes_ago=0, **extra):
    recorded = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "latitude": lat,
        "longitude": lon,
        "accuracy_m": 12.5,
        "provider": "gps",
        "battery_level": 80,
        "recorded_at": recorded.isoformat(),
        **extra,
    }


async def enable_location(client, admin_headers, device_id, **overrides):
    return await client.put(
        f"/api/v1/devices/{device_id}/location/settings",
        headers=admin_headers,
        json={"enabled": True, **overrides},
    )


# ------------------------------------------------------- стан за замовч. --


async def test_location_is_disabled_by_default(client, admin_headers, device, db):
    response = await client.get(
        f"/api/v1/devices/{device.id}/location", headers=admin_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["settings"]["enabled"] is False
    assert body["settings"]["permission_state"] == "unknown"
    assert body["current"] is None
    assert body["fix_count"] == 0
    assert body["reporting_active"] is False
    assert body["retention_days"] > 0

    stored = (await db.execute(sa.select(LocationSettings))).scalars().one()
    assert stored.enabled is False


async def test_agent_sees_disabled_config(client, device):
    response = await client.get("/api/v1/device/location/config", headers=device.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["interval_seconds"] == 900
    assert body["high_accuracy"] is False


# --------------------------------------------------- конфігурація й аудит --


async def test_enabling_is_audited_with_before_and_after(client, admin_headers, device, db):
    response = await enable_location(
        client, admin_headers, device.id, interval_seconds=300, high_accuracy=True
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["interval_seconds"] == 300
    assert body["high_accuracy"] is True
    assert body["consent_recorded_at"] is not None

    rows = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.LOCATION_CONFIG_CHANGED)
        )
    ).scalars().all()
    assert len(rows) == 1
    changes = rows[0].details["changes"]
    assert changes["enabled"] == {"from": False, "to": True}
    assert changes["interval_seconds"] == {"from": 900, "to": 300}
    assert rows[0].details["resulting_config"]["high_accuracy"] is True


async def test_disabling_uses_separate_audit_action(client, admin_headers, device, db):
    await enable_location(client, admin_headers, device.id)
    response = await client.put(
        f"/api/v1/devices/{device.id}/location/settings",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert response.status_code == 200

    rows = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.LOCATION_DISABLED)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].details["changes"]["enabled"] == {"from": True, "to": False}


async def test_config_change_sends_command_to_device(client, admin_headers, device):
    await enable_location(client, admin_headers, device.id, interval_seconds=600)

    pending = await client.get("/api/v1/device/commands/pending", headers=device.headers)
    assert pending.status_code == 200
    commands = pending.json()
    assert [c["type"] for c in commands] == ["LOCATION_CONFIG"]
    assert commands[0]["payload"]["enabled"] is True
    assert commands[0]["payload"]["interval_seconds"] == 600


async def test_no_change_produces_no_command(client, admin_headers, device):
    await client.put(
        f"/api/v1/devices/{device.id}/location/settings",
        headers=admin_headers,
        json={"enabled": False},
    )
    pending = await client.get("/api/v1/device/commands/pending", headers=device.headers)
    assert pending.json() == []


async def test_interval_bounds_are_enforced(client, admin_headers, device):
    too_small = await client.put(
        f"/api/v1/devices/{device.id}/location/settings",
        headers=admin_headers,
        json={"interval_seconds": 5},
    )
    assert too_small.status_code == 422

    too_big = await client.put(
        f"/api/v1/devices/{device.id}/location/settings",
        headers=admin_headers,
        json={"interval_seconds": 999_999},
    )
    assert too_big.status_code == 422


# ---------------------------------------------------- приймання координат --


async def test_upload_rejected_when_module_disabled(client, device, db):
    response = await client.post(
        "/api/v1/device/location",
        headers=device.headers,
        json={"fixes": [fix_payload()]},
    )
    assert response.status_code == 403

    stored = (await db.execute(sa.select(sa.func.count()).select_from(LocationFix))).scalar_one()
    assert stored == 0

    denied = (
        await db.execute(
            sa.select(AuditLog).where(
                AuditLog.action == AuditAction.LOCATION_UPLOAD_REJECTED
            )
        )
    ).scalars().all()
    assert denied[0].result == "denied"
    assert denied[0].details["reason"] == "module_disabled"


async def test_batch_upload_accepted_when_enabled(client, admin_headers, device, db):
    await enable_location(client, admin_headers, device.id)

    response = await client.post(
        "/api/v1/device/location",
        headers=device.headers,
        json={
            "fixes": [
                fix_payload(minutes_ago=30, source="offline_queue"),
                fix_payload(minutes_ago=15, source="offline_queue"),
                fix_payload(minutes_ago=0, source="periodic"),
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 3
    assert body["rejected"] == 0
    assert body["config"]["enabled"] is True

    rows = (await db.execute(sa.select(LocationFix))).scalars().all()
    assert len(rows) == 3
    assert {row.source for row in rows} == {"offline_queue", "periodic"}


async def test_stale_and_future_fixes_are_rejected(client, admin_headers, device):
    await enable_location(client, admin_headers, device.id)

    response = await client.post(
        "/api/v1/device/location",
        headers=device.headers,
        json={
            "fixes": [
                fix_payload(minutes_ago=60 * 24 * 30),  # 30 днів тому
                fix_payload(minutes_ago=-120),  # на 2 години в майбутньому
                fix_payload(minutes_ago=5),
            ]
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "accepted": 1,
        "rejected": 2,
        "config": response.json()["config"],
    }


async def test_invalid_coordinates_rejected(client, admin_headers, device):
    await enable_location(client, admin_headers, device.id)
    response = await client.post(
        "/api/v1/device/location",
        headers=device.headers,
        json={"fixes": [fix_payload(lat=91.0)]},
    )
    assert response.status_code == 422


async def test_current_location_and_history(client, admin_headers, device):
    await enable_location(client, admin_headers, device.id)
    await client.post(
        "/api/v1/device/location",
        headers=device.headers,
        json={
            "fixes": [
                fix_payload(lat=50.0, lon=30.0, minutes_ago=60),
                fix_payload(lat=50.1, lon=30.1, minutes_ago=30),
                fix_payload(lat=50.2, lon=30.2, minutes_ago=1),
            ]
        },
    )

    overview = (
        await client.get(f"/api/v1/devices/{device.id}/location", headers=admin_headers)
    ).json()
    assert overview["fix_count"] == 3
    assert overview["current"]["latitude"] == 50.2
    assert overview["oldest_fix_at"] is not None

    history = await client.get(
        f"/api/v1/devices/{device.id}/location/history", headers=admin_headers
    )
    assert history.status_code == 200
    assert history.json()["total"] == 3
    # Найсвіжіша — перша.
    assert history.json()["items"][0]["latitude"] == 50.2


async def test_history_view_is_audited(client, admin_headers, device, db):
    await enable_location(client, admin_headers, device.id)
    await client.get(f"/api/v1/devices/{device.id}/location/history", headers=admin_headers)

    rows = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.LOCATION_HISTORY_VIEWED)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor_type == "user"


async def test_history_can_be_purged(client, admin_headers, device, db):
    await enable_location(client, admin_headers, device.id)
    await client.post(
        "/api/v1/device/location",
        headers=device.headers,
        json={"fixes": [fix_payload(), fix_payload(minutes_ago=10)]},
    )

    response = await client.delete(
        f"/api/v1/devices/{device.id}/location/history", headers=admin_headers
    )
    assert response.status_code == 200

    remaining = (
        await db.execute(sa.select(sa.func.count()).select_from(LocationFix))
    ).scalar_one()
    assert remaining == 0

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.LOCATION_HISTORY_PURGED)
        )
    ).scalars().all()
    assert audit[0].details["removed"] == 2


# --------------------------------------------------------------- дозволи --


async def test_permission_report_is_stored_and_audited(client, admin_headers, device, db):
    response = await client.post(
        "/api/v1/device/location/permissions",
        headers=device.headers,
        json={"permission_state": "granted_fine", "background_permission": True},
    )
    assert response.status_code == 200

    overview = (
        await client.get(f"/api/v1/devices/{device.id}/location", headers=admin_headers)
    ).json()
    assert overview["settings"]["permission_state"] == "granted_fine"
    assert overview["settings"]["background_permission"] is True
    assert overview["settings"]["permission_reported_at"] is not None
    # Дозвіл є, але модуль вимкнено → звітування не активне.
    assert overview["reporting_active"] is False

    rows = (
        await db.execute(
            sa.select(AuditLog).where(
                AuditLog.action == AuditAction.LOCATION_PERMISSION_CHANGED
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].details["to"]["permission_state"] == "granted_fine"
    assert rows[0].actor_type == "device"


async def test_repeated_permission_report_not_audited_twice(client, device, db):
    payload = {"permission_state": "denied", "background_permission": False}
    await client.post("/api/v1/device/location/permissions", headers=device.headers, json=payload)
    await client.post("/api/v1/device/location/permissions", headers=device.headers, json=payload)

    rows = (
        await db.execute(
            sa.select(AuditLog).where(
                AuditLog.action == AuditAction.LOCATION_PERMISSION_CHANGED
            )
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_reporting_active_requires_permission_and_enabled(client, admin_headers, device):
    await enable_location(client, admin_headers, device.id)
    await client.post(
        "/api/v1/device/location/permissions",
        headers=device.headers,
        json={"permission_state": "granted_fine", "background_permission": True},
    )

    overview = (
        await client.get(f"/api/v1/devices/{device.id}/location", headers=admin_headers)
    ).json()
    assert overview["reporting_active"] is True


# ------------------------------------------------------- разовий запит ----


async def test_request_now_requires_enabled_module(client, admin_headers, device):
    denied = await client.post(
        f"/api/v1/devices/{device.id}/location/request", headers=admin_headers
    )
    assert denied.status_code == 409

    await enable_location(client, admin_headers, device.id)
    accepted = await client.post(
        f"/api/v1/devices/{device.id}/location/request", headers=admin_headers
    )
    assert accepted.status_code == 202
    assert accepted.json()["type"] == "LOCATION_REQUEST"


# ------------------------------------------------------------- доступ ----


async def test_operator_cannot_change_location_settings(client, auth_headers, device):
    operator = await auth_headers(email="operator@example.com", role=Role.OPERATOR)

    read = await client.get(f"/api/v1/devices/{device.id}/location", headers=operator)
    assert read.status_code == 200

    write = await client.put(
        f"/api/v1/devices/{device.id}/location/settings",
        headers=operator,
        json={"enabled": True},
    )
    assert write.status_code == 403


async def test_device_cannot_read_other_device_location(
    client, admin_headers, device, enroll_device
):
    await enable_location(client, admin_headers, device.id)
    other = await enroll_device(android_id="ffffffffffffffff")

    # Пристрій бачить лише власну конфігурацію.
    config = await client.get("/api/v1/device/location/config", headers=other.headers)
    assert config.status_code == 200
    assert config.json()["enabled"] is False


async def test_anonymous_cannot_upload_location(client):
    response = await client.post("/api/v1/device/location", json={"fixes": [fix_payload()]})
    assert response.status_code == 401


# ------------------------------------------------------------ ретеншн ----


async def test_retention_removes_old_fixes(client, admin_headers, device, db):
    from app.core.config import get_settings
    from app.services.retention import run_retention

    await enable_location(client, admin_headers, device.id)
    await client.post(
        "/api/v1/device/location",
        headers=device.headers,
        json={"fixes": [fix_payload(minutes_ago=10)]},
    )

    # Штучно «старимо» запис: ретеншн орієнтується на received_at.
    old = datetime.now(timezone.utc) - timedelta(days=400)
    await db.execute(sa.update(LocationFix).values(received_at=old))
    await db.commit()

    settings = get_settings()
    removed = await run_retention(db, settings)
    assert removed["location_fixes"] == 1

    remaining = (
        await db.execute(sa.select(sa.func.count()).select_from(LocationFix))
    ).scalar_one()
    assert remaining == 0


async def test_retention_endpoint_requires_settings_permission(client, auth_headers):
    admin = await auth_headers(email="admin2@example.com", role=Role.ADMIN)
    assert (await client.post("/api/v1/maintenance/retention", headers=admin)).status_code == 403

    superadmin = await auth_headers(email="root@example.com", role=Role.SUPERADMIN)
    response = await client.post("/api/v1/maintenance/retention", headers=superadmin)
    assert response.status_code == 200
    assert "location_fixes" in response.json()
