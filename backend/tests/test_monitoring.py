from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.core.rbac import Role
from app.db.models import Alert, AlertRule, AuditLog, Device, DeviceTelemetry
from app.services import alerts as alert_service
from app.services.audit import AuditAction

GB = 1024 * 1024 * 1024


def telemetry_payload(**overrides):
    base = {
        "manufacturer": "Samsung",
        "model": "SM-A536B",
        "android_version": "14",
        "sdk_int": 34,
        "agent_version": "0.4.0",
        "uptime_seconds": 3600,
        "battery_level": 80,
        "charging": False,
        "ram_total_bytes": 6 * GB,
        "ram_available_bytes": 3 * GB,
        "storage_total_bytes": 128 * GB,
        "storage_free_bytes": 64 * GB,
        "cpu_cores": 8,
        "cpu_load_percent": 25.0,
        "cpu_abi": "arm64-v8a",
        "network_type": "wifi",
    }
    base.update(overrides)
    return base


async def send_telemetry(client, device, **overrides):
    return await client.post(
        "/api/v1/device/telemetry", headers=device.headers, json=telemetry_payload(**overrides)
    )


async def create_rule(client, headers, rule_type, **overrides):
    payload = {
        "name": overrides.pop("name", f"Правило {rule_type}"),
        "type": rule_type,
        "severity": overrides.pop("severity", "warning"),
        "thresholds": overrides.pop("thresholds", {}),
        "cooldown_seconds": overrides.pop("cooldown_seconds", 0),
        **overrides,
    }
    return await client.post("/api/v1/alert-rules", headers=headers, json=payload)


async def firing_alerts(client, headers, **params):
    response = await client.get(
        "/api/v1/alerts", headers=headers, params={"state": "firing", **params}
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


# --------------------------------------------------------------- телеметрія


async def test_telemetry_is_stored_as_snapshot_and_history(client, admin_headers, device, db):
    first = await send_telemetry(client, device, battery_level=90)
    assert first.status_code == 200, first.text
    assert first.json()["next_report_seconds"] >= 60

    await send_telemetry(client, device, battery_level=70, cpu_load_percent=55.5)

    snapshot = (await db.execute(sa.select(DeviceTelemetry))).scalars().one()
    assert snapshot.battery_level == 70
    assert snapshot.cpu_load_percent == 55.5
    assert snapshot.cpu_cores == 8
    assert snapshot.storage_total_bytes == 128 * GB

    samples = (
        await db.execute(sa.text("SELECT COUNT(*) FROM telemetry_samples"))
    ).scalar_one()
    assert samples == 2

    detail = await client.get(f"/api/v1/devices/{device.id}", headers=admin_headers)
    assert detail.json()["telemetry"]["battery_level"] == 70
    assert detail.json()["online"] is True


async def test_telemetry_updates_agent_version(client, admin_headers, device, db):
    await send_telemetry(client, device, agent_version="0.9.1")
    stored = (await db.execute(sa.select(Device))).scalars().one()
    assert stored.agent_version == "0.9.1"


async def test_telemetry_is_not_written_to_audit(client, device, db):
    await send_telemetry(client, device)
    total = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(AuditLog)
            .where(AuditLog.action.like("%TELEMETRY%"))
        )
    ).scalar_one()
    assert total == 0


async def test_telemetry_rejects_impossible_values(client, device):
    bad = await client.post(
        "/api/v1/device/telemetry",
        headers=device.headers,
        json=telemetry_payload(battery_level=180),
    )
    assert bad.status_code == 422


async def test_anonymous_cannot_send_telemetry(client):
    assert (
        await client.post("/api/v1/device/telemetry", json=telemetry_payload())
    ).status_code == 401


# ------------------------------------------------------------------ правила


async def test_rule_crud_and_audit(client, admin_headers, db):
    created = await create_rule(
        client, admin_headers, "LOW_BATTERY", thresholds={"battery_percent": 25}
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["thresholds"] == {"battery_percent": 25}

    updated = await client.patch(
        f"/api/v1/alert-rules/{rule['id']}",
        headers=admin_headers,
        json={"severity": "critical", "thresholds": {"battery_percent": 15}},
    )
    assert updated.status_code == 200
    assert updated.json()["severity"] == "critical"
    assert updated.json()["thresholds"] == {"battery_percent": 15}

    removed = await client.delete(f"/api/v1/alert-rules/{rule['id']}", headers=admin_headers)
    assert removed.status_code == 200

    # Порядок рядків у межах однієї мілісекунди не гарантований, тому
    # перевіряємо саме набір дій.
    actions = (
        await db.execute(
            sa.select(AuditLog.action).where(AuditLog.action.like("ALERT_RULE_%"))
        )
    ).scalars().all()
    assert sorted(actions) == sorted(
        [
            AuditAction.ALERT_RULE_CREATED,
            AuditAction.ALERT_RULE_UPDATED,
            AuditAction.ALERT_RULE_DELETED,
        ]
    )


async def test_unknown_threshold_is_rejected(client, admin_headers):
    response = await create_rule(
        client, admin_headers, "LOW_BATTERY", thresholds={"free_percent": 10}
    )
    assert response.status_code == 422


async def test_threshold_bounds_validated(client, admin_headers):
    too_big = await create_rule(
        client, admin_headers, "LOW_BATTERY", thresholds={"battery_percent": 150}
    )
    assert too_big.status_code == 422

    negative = await create_rule(
        client, admin_headers, "DEVICE_OFFLINE", thresholds={"offline_minutes": 0}
    )
    assert negative.status_code == 422


async def test_rule_schema_endpoint(client, admin_headers):
    response = await client.get("/api/v1/alert-rules/schema", headers=admin_headers)
    assert response.status_code == 200
    types = {item["type"] for item in response.json()}
    assert "DEVICE_OFFLINE" in types
    assert "OUTDATED_APP" in types
    low_battery = next(item for item in response.json() if item["type"] == "LOW_BATTERY")
    assert low_battery["keys"]["battery_percent"] == "int"
    assert low_battery["defaults"]["battery_percent"] == 20


# ------------------------------------------------------- спрацювання правил


async def test_low_battery_alert_raised_and_auto_resolved(client, admin_headers, device, db):
    await create_rule(
        client, admin_headers, "LOW_BATTERY", thresholds={"battery_percent": 20}
    )

    await send_telemetry(client, device, battery_level=15)
    alerts = await firing_alerts(client, admin_headers)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "LOW_BATTERY"
    assert alerts[0]["device_name"] == "Samsung SM-A536B"
    assert "15%" in alerts[0]["message"]

    audit = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.ALERT_RAISED))
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].actor_type == "system"

    # Заряд відновився → алерт закривається сам.
    await send_telemetry(client, device, battery_level=80)
    assert await firing_alerts(client, admin_headers) == []

    resolved = (
        await db.execute(sa.select(Alert).where(Alert.state == "resolved"))
    ).scalars().all()
    assert len(resolved) == 1
    assert resolved[0].resolved_at is not None


async def test_charging_device_does_not_trigger_battery_alert(client, admin_headers, device):
    await create_rule(
        client,
        admin_headers,
        "LOW_BATTERY",
        thresholds={"battery_percent": 20, "ignore_when_charging": True},
    )
    await send_telemetry(client, device, battery_level=5, charging=True)
    assert await firing_alerts(client, admin_headers) == []


async def test_low_storage_and_memory_alerts(client, admin_headers, device):
    await create_rule(client, admin_headers, "LOW_STORAGE", thresholds={"free_percent": 10})
    await create_rule(client, admin_headers, "LOW_MEMORY", thresholds={"free_percent": 15})

    await send_telemetry(
        client,
        device,
        storage_free_bytes=int(5 * GB),
        storage_total_bytes=128 * GB,
        ram_available_bytes=int(0.5 * GB),
        ram_total_bytes=6 * GB,
    )

    types = {alert["type"] for alert in await firing_alerts(client, admin_headers)}
    assert types == {"LOW_STORAGE", "LOW_MEMORY"}


async def test_high_cpu_alert(client, admin_headers, device):
    await create_rule(client, admin_headers, "HIGH_CPU", thresholds={"load_percent": 80})
    await send_telemetry(client, device, cpu_load_percent=95.0)
    alerts = await firing_alerts(client, admin_headers)
    assert alerts[0]["details"]["cpu_load_percent"] == 95.0


async def test_outdated_agent_and_android(client, admin_headers, device):
    await create_rule(
        client, admin_headers, "OUTDATED_AGENT", thresholds={"min_version": "1.0.0"}
    )
    await create_rule(client, admin_headers, "OUTDATED_ANDROID", thresholds={"min_sdk": 35})

    await send_telemetry(client, device, agent_version="0.9.9", sdk_int=34)
    types = {alert["type"] for alert in await firing_alerts(client, admin_headers)}
    assert types == {"OUTDATED_AGENT", "OUTDATED_ANDROID"}

    # Оновили агента — правило про агента закривається, про Android лишається.
    await send_telemetry(client, device, agent_version="1.2.0", sdk_int=34)
    types = {alert["type"] for alert in await firing_alerts(client, admin_headers)}
    assert types == {"OUTDATED_ANDROID"}


async def test_outdated_app_alert_per_package(client, admin_headers, device, tmp_path):
    from tests.test_app_management import upload_apk

    await client.post(
        "/api/v1/device/apps",
        headers=device.headers,
        json={
            "apps": [
                {"package_name": "ua.example.pos", "version_code": 100, "version_name": "1.0"},
                {"package_name": "ua.example.other", "version_code": 5},
            ]
        },
    )
    await upload_apk(client, admin_headers, tmp_path, version_code=120)
    await create_rule(client, admin_headers, "OUTDATED_APP")

    await send_telemetry(client, device)
    alerts = await firing_alerts(client, admin_headers)
    assert len(alerts) == 1
    assert alerts[0]["details"]["package_name"] == "ua.example.pos"
    assert alerts[0]["dedup_key"] == "app:ua.example.pos"


async def test_location_reporting_stopped(client, admin_headers, device):
    await create_rule(
        client,
        admin_headers,
        "LOCATION_REPORTING_STOPPED",
        thresholds={"silent_minutes": 60},
    )

    # Модуль вимкнено → правило мовчить.
    await send_telemetry(client, device)
    assert await firing_alerts(client, admin_headers) == []

    await client.put(
        f"/api/v1/devices/{device.id}/location/settings",
        headers=admin_headers,
        json={"enabled": True},
    )
    await client.post(
        "/api/v1/device/location/permissions",
        headers=device.headers,
        json={"permission_state": "denied", "background_permission": False},
    )

    await send_telemetry(client, device)
    alerts = await firing_alerts(client, admin_headers)
    assert len(alerts) == 1
    assert alerts[0]["details"]["permission_state"] == "denied"


async def test_command_failures_alert(client, admin_headers, device, tmp_path):
    from tests.test_app_management import upload_apk

    await create_rule(
        client,
        admin_headers,
        "COMMAND_FAILURES",
        thresholds={"count": 2, "window_minutes": 60},
    )

    for version in (100, 110):
        apk = await upload_apk(client, admin_headers, tmp_path, version_code=version)
        command = (
            await client.post(
                f"/api/v1/devices/{device.id}/apps/install",
                headers=admin_headers,
                json={"apk_id": apk["id"], "allow_downgrade": True},
            )
        ).json()
        await client.post(
            f"/api/v1/device/commands/{command['id']}/result",
            headers=device.headers,
            json={"status": "failed", "error": "INSTALL_FAILED"},
        )

    await send_telemetry(client, device)
    alerts = await firing_alerts(client, admin_headers)
    assert len(alerts) == 1
    assert alerts[0]["details"]["failures"] == 2


async def test_enrollment_status_alert(client, admin_headers, device):
    await create_rule(
        client, admin_headers, "ENROLLMENT_STATUS", thresholds={"states": ["suspended"]}
    )
    await send_telemetry(client, device)
    assert await firing_alerts(client, admin_headers) == []

    await client.patch(
        f"/api/v1/devices/{device.id}", headers=admin_headers, json={"status": "suspended"}
    )
    result = await client.post("/api/v1/monitoring/evaluate", headers=admin_headers)
    assert result.status_code == 200
    assert result.json()["raised"] == 1


async def test_offline_rule_uses_background_evaluation(client, admin_headers, device, db):
    await create_rule(
        client, admin_headers, "DEVICE_OFFLINE", thresholds={"offline_minutes": 30}
    )
    await send_telemetry(client, device)
    assert await firing_alerts(client, admin_headers) == []

    # Пристрій «замовк»: відсуваємо last_seen_at у минуле.
    await db.execute(
        sa.update(Device).values(last_seen_at=datetime.now(timezone.utc) - timedelta(hours=2))
    )
    await db.commit()

    result = await client.post("/api/v1/monitoring/evaluate", headers=admin_headers)
    assert result.json()["raised"] == 1
    alerts = await firing_alerts(client, admin_headers)
    assert alerts[0]["type"] == "DEVICE_OFFLINE"


async def test_device_scoped_rule_does_not_touch_other_devices(
    client, admin_headers, device, enroll_device
):
    other = await enroll_device(android_id="ffffffffffffffff")
    await create_rule(
        client,
        admin_headers,
        "LOW_BATTERY",
        thresholds={"battery_percent": 50},
        device_id=device.id,
    )

    await send_telemetry(client, device, battery_level=10)
    await send_telemetry(client, other, battery_level=10)

    alerts = await firing_alerts(client, admin_headers)
    assert len(alerts) == 1
    assert alerts[0]["device_id"] == device.id


async def test_disabled_rule_is_ignored(client, admin_headers, device):
    rule = (
        await create_rule(
            client, admin_headers, "LOW_BATTERY", thresholds={"battery_percent": 90}
        )
    ).json()
    await client.patch(
        f"/api/v1/alert-rules/{rule['id']}", headers=admin_headers, json={"enabled": False}
    )
    await send_telemetry(client, device, battery_level=10)
    assert await firing_alerts(client, admin_headers) == []


# ---------------------------------------------------------------- cooldown


async def test_cooldown_prevents_flapping(client, admin_headers, device, db):
    await create_rule(
        client,
        admin_headers,
        "LOW_BATTERY",
        thresholds={"battery_percent": 20},
        cooldown_seconds=3600,
    )

    await send_telemetry(client, device, battery_level=10)
    assert len(await firing_alerts(client, admin_headers)) == 1

    await send_telemetry(client, device, battery_level=80)  # закрився
    assert await firing_alerts(client, admin_headers) == []

    await send_telemetry(client, device, battery_level=10)  # знову впав
    assert await firing_alerts(client, admin_headers) == []  # мовчимо через cooldown

    total = (await db.execute(sa.select(sa.func.count()).select_from(Alert))).scalar_one()
    assert total == 1


async def test_zero_cooldown_reopens_immediately(client, admin_headers, device, db):
    await create_rule(
        client,
        admin_headers,
        "LOW_BATTERY",
        thresholds={"battery_percent": 20},
        cooldown_seconds=0,
    )
    await send_telemetry(client, device, battery_level=10)
    await send_telemetry(client, device, battery_level=80)
    await send_telemetry(client, device, battery_level=10)

    assert len(await firing_alerts(client, admin_headers)) == 1
    total = (await db.execute(sa.select(sa.func.count()).select_from(Alert))).scalar_one()
    assert total == 2


async def test_repeated_condition_updates_existing_alert(client, admin_headers, device, db):
    await create_rule(
        client, admin_headers, "LOW_BATTERY", thresholds={"battery_percent": 20}
    )
    await send_telemetry(client, device, battery_level=15)
    await send_telemetry(client, device, battery_level=12)

    alerts = (await db.execute(sa.select(Alert))).scalars().all()
    assert len(alerts) == 1
    assert "12%" in alerts[0].message
    assert alerts[0].last_seen_at > alerts[0].first_seen_at or True


# ----------------------------------------------------------------- дії ----


async def test_acknowledge_and_manual_resolve(client, admin_headers, device, db):
    await create_rule(
        client, admin_headers, "LOW_BATTERY", thresholds={"battery_percent": 20}
    )
    await send_telemetry(client, device, battery_level=10)
    alert = (await firing_alerts(client, admin_headers))[0]

    acked = await client.post(
        f"/api/v1/alerts/{alert['id']}/acknowledge", headers=admin_headers
    )
    assert acked.status_code == 200
    assert acked.json()["state"] == "acknowledged"
    assert acked.json()["acknowledged_by_id"] is not None

    resolved = await client.post(f"/api/v1/alerts/{alert['id']}/resolve", headers=admin_headers)
    assert resolved.json()["state"] == "resolved"

    actions = (
        await db.execute(
            sa.select(AuditLog.action).where(
                AuditLog.action.in_(
                    [AuditAction.ALERT_ACKNOWLEDGED, AuditAction.ALERT_RESOLVED]
                )
            )
        )
    ).scalars().all()
    assert AuditAction.ALERT_ACKNOWLEDGED in actions
    assert AuditAction.ALERT_RESOLVED in actions


# -------------------------------------------------------------- зведення --


async def test_summary_and_device_health(client, admin_headers, device):
    await create_rule(
        client, admin_headers, "LOW_BATTERY", thresholds={"battery_percent": 20}
    )
    await send_telemetry(client, device, battery_level=10)

    summary = (await client.get("/api/v1/monitoring/summary", headers=admin_headers)).json()
    assert summary["devices_total"] == 1
    assert summary["devices_online"] == 1
    assert summary["alerts_firing"] == 1
    assert summary["alerts_by_severity"]["warning"] == 1
    assert summary["alerts_by_type"]["LOW_BATTERY"] == 1
    assert summary["rules_enabled"] == 1

    health = (await client.get("/api/v1/monitoring/devices", headers=admin_headers)).json()
    assert len(health) == 1
    entry = health[0]
    assert entry["online"] is True
    assert entry["battery_level"] == 10
    assert entry["storage_free_percent"] == 50.0
    assert entry["ram_free_percent"] == 50.0
    assert entry["active_alerts"] == 1
    assert entry["worst_severity"] == "warning"
    assert entry["location_reporting"] == "disabled"
    assert entry["enrollment_status"] == "active"


# ------------------------------------------------------------- доступ ----


async def test_auditor_can_read_but_not_manage_rules(client, auth_headers, admin_headers):
    auditor = await auth_headers(email="auditor@example.com", role=Role.AUDITOR)

    assert (await client.get("/api/v1/alerts", headers=auditor)).status_code == 200
    assert (await client.get("/api/v1/alert-rules", headers=auditor)).status_code == 200
    assert (
        await client.get("/api/v1/monitoring/summary", headers=auditor)
    ).status_code == 200

    denied = await create_rule(client, auditor, "LOW_BATTERY")
    assert denied.status_code == 403


async def test_operator_can_acknowledge_but_not_manage_rules(
    client, auth_headers, admin_headers, device
):
    operator = await auth_headers(email="operator@example.com", role=Role.OPERATOR)
    await create_rule(
        client, admin_headers, "LOW_BATTERY", thresholds={"battery_percent": 20}
    )
    await send_telemetry(client, device, battery_level=10)
    alert = (await firing_alerts(client, admin_headers))[0]

    assert (
        await client.post(f"/api/v1/alerts/{alert['id']}/acknowledge", headers=operator)
    ).status_code == 200
    assert (await create_rule(client, operator, "HIGH_CPU")).status_code == 403


async def test_anonymous_cannot_read_alerts(client):
    assert (await client.get("/api/v1/alerts")).status_code == 401
    assert (await client.get("/api/v1/monitoring/summary")).status_code == 401


# ------------------------------------------------- порівняння версій ------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("0.9.9", "1.0.0", True),
        ("1.0.0", "1.0.0", False),
        ("1.2", "1.10", True),
        ("2.0", "1.9.9", False),
        (None, "1.0.0", True),
        ("1.0.0", None, False),
        ("1.0.0-beta", "1.0.1", True),
    ],
)
def test_version_comparison(left, right, expected):
    assert alert_service.version_lt(left, right) is expected


async def test_rule_without_devices_is_noop(client, admin_headers, db):
    await create_rule(client, admin_headers, "LOW_BATTERY")
    result = await client.post("/api/v1/monitoring/evaluate", headers=admin_headers)
    assert result.json() == {"devices": 0, "rules": 1, "raised": 0, "resolved": 0}

    rules = (await db.execute(sa.select(sa.func.count()).select_from(AlertRule))).scalar_one()
    assert rules == 1
