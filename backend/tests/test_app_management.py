import uuid

import sqlalchemy as sa

from app.core.rbac import Role
from app.db.models import AuditLog, Command, DeviceApp
from app.services.audit import AuditAction
from tests.axml import build_apk

PACKAGE = "ua.example.pos"


async def upload_apk(client, headers, tmp_path, *, version_code=100, **kwargs):
    name = kwargs.pop("name", f"pos-{version_code}.apk")
    path = build_apk(
        tmp_path / name,
        package=kwargs.pop("package", PACKAGE),
        version_code=version_code,
        version_name=kwargs.pop("version_name", f"1.0.{version_code}"),
        **kwargs,
    )
    response = await client.post(
        "/api/v1/apks",
        headers=headers,
        files={"file": (name, path.read_bytes(), "application/vnd.android.package-archive")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def session_command(db, command_id) -> Command:
    return (
        await db.execute(sa.select(Command).where(Command.id == uuid.UUID(str(command_id))))
    ).scalars().one()


async def report_installed(client, device, *, package=PACKAGE, version_code=100, **extra):
    return await client.post(
        "/api/v1/device/apps",
        headers=device.headers,
        json={"apps": [{"package_name": package, "version_code": version_code, **extra}]},
    )


# ------------------------------------------------------- повний цикл ------


async def test_full_install_cycle(client, admin_headers, device, tmp_path, db):
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=100)

    # 1. Адміністратор ставить задачу
    requested = await client.post(
        f"/api/v1/devices/{device.id}/apps/install",
        headers=admin_headers,
        json={"apk_id": apk["id"]},
    )
    assert requested.status_code == 202, requested.text
    command = requested.json()
    assert command["type"] == "APP_INSTALL"
    assert command["status"] == "pending"
    assert command["payload"]["sha256"] == apk["sha256"]
    assert command["payload"]["download_url"].endswith(f"/device/apks/{apk['id']}/download")

    # 2. Агент забирає команду
    pending = await client.get("/api/v1/device/commands/pending", headers=device.headers)
    assert pending.status_code == 200
    assert [c["id"] for c in pending.json()] == [command["id"]]

    # 3. Підтвердження і прогрес
    ack = await client.post(
        f"/api/v1/device/commands/{command['id']}/ack", headers=device.headers
    )
    assert ack.status_code == 200

    progress = await client.post(
        f"/api/v1/device/commands/{command['id']}/progress",
        headers=device.headers,
        json={"percent": 45, "message": "Завантаження APK"},
    )
    assert progress.status_code == 200

    # 4. Завантаження APK агентом
    download = await client.get(
        f"/api/v1/device/apks/{apk['id']}/download", headers=device.headers
    )
    assert download.status_code == 200
    assert download.headers["X-Checksum-SHA256"] == apk["sha256"]
    assert download.headers["X-Package-Name"] == PACKAGE

    # 5. Результат
    result = await client.post(
        f"/api/v1/device/commands/{command['id']}/result",
        headers=device.headers,
        json={
            "status": "succeeded",
            "result": {
                "installed": {
                    "package_name": PACKAGE,
                    "version_code": 100,
                    "version_name": "1.0.100",
                },
                "mechanism": "package_installer_device_owner",
            },
        },
    )
    assert result.status_code == 200

    stored = await session_command(db, command["id"])
    assert stored.status == "succeeded"
    assert stored.progress_percent == 100
    assert stored.completed_at is not None

    # 6. Інвентар оновлено
    apps = await client.get(f"/api/v1/devices/{device.id}/apps", headers=admin_headers)
    assert apps.status_code == 200
    installed = apps.json()[0]
    assert installed["package_name"] == PACKAGE
    assert installed["version_code"] == 100
    assert installed["in_repository"] is True
    assert installed["update_available"] is False


# --------------------------------------------------- порівняння версій ----


async def test_version_comparison_flags_update(client, admin_headers, device, tmp_path):
    await report_installed(
        client, device, version_code=100, version_name="1.0.100", app_name="Каса"
    )
    await upload_apk(client, admin_headers, tmp_path, version_code=120)

    apps = (
        await client.get(f"/api/v1/devices/{device.id}/apps", headers=admin_headers)
    ).json()
    assert apps[0]["update_available"] is True
    assert apps[0]["repository_version_code"] == 120
    assert apps[0]["version_code"] == 100


async def test_install_of_known_package_becomes_update(
    client, admin_headers, device, tmp_path
):
    await report_installed(client, device, version_code=100)
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=120)

    response = await client.post(
        f"/api/v1/devices/{device.id}/apps/install",
        headers=admin_headers,
        json={"apk_id": apk["id"]},
    )
    assert response.status_code == 202
    assert response.json()["type"] == "APP_UPDATE"


async def test_downgrade_blocked_unless_allowed(client, admin_headers, device, tmp_path):
    await report_installed(client, device, version_code=120)
    old = await upload_apk(client, admin_headers, tmp_path, version_code=100)

    blocked = await client.post(
        f"/api/v1/devices/{device.id}/apps/install",
        headers=admin_headers,
        json={"apk_id": old["id"]},
    )
    assert blocked.status_code == 409
    assert "новішу версію" in blocked.json()["error"]["message"]

    allowed = await client.post(
        f"/api/v1/devices/{device.id}/apps/install",
        headers=admin_headers,
        json={"apk_id": old["id"], "allow_downgrade": True},
    )
    assert allowed.status_code == 202


async def test_min_sdk_incompatibility_rejected(client, admin_headers, device, tmp_path):
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=200, min_sdk=99)
    response = await client.post(
        f"/api/v1/devices/{device.id}/apps/install",
        headers=admin_headers,
        json={"apk_id": apk["id"]},
    )
    assert response.status_code == 409
    assert "SDK" in response.json()["error"]["message"]


async def test_duplicate_operation_for_package_rejected(
    client, admin_headers, device, tmp_path
):
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=100)
    first = await client.post(
        f"/api/v1/devices/{device.id}/apps/install",
        headers=admin_headers,
        json={"apk_id": apk["id"]},
    )
    assert first.status_code == 202

    second = await client.post(
        f"/api/v1/devices/{device.id}/apps/install",
        headers=admin_headers,
        json={"apk_id": apk["id"]},
    )
    assert second.status_code == 409


# ------------------------------------------------------- видалення --------


async def test_uninstall_removes_app_from_inventory(client, admin_headers, device, db):
    await report_installed(client, device, version_code=100)

    command = (
        await client.post(
            f"/api/v1/devices/{device.id}/apps/uninstall",
            headers=admin_headers,
            json={"package_name": PACKAGE},
        )
    ).json()
    assert command["type"] == "APP_UNINSTALL"

    await client.post(
        f"/api/v1/device/commands/{command['id']}/result",
        headers=device.headers,
        json={"status": "succeeded", "result": {"mechanism": "package_installer"}},
    )

    remaining = (
        await db.execute(sa.select(sa.func.count()).select_from(DeviceApp))
    ).scalar_one()
    assert remaining == 0


async def test_system_app_cannot_be_uninstalled(client, admin_headers, device):
    await report_installed(
        client, device, package="com.android.settings", version_code=34, is_system=True
    )
    response = await client.post(
        f"/api/v1/devices/{device.id}/apps/uninstall",
        headers=admin_headers,
        json={"package_name": "com.android.settings"},
    )
    assert response.status_code == 409


async def test_failed_install_is_recorded(client, admin_headers, device, tmp_path, db):
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=100)
    command = (
        await client.post(
            f"/api/v1/devices/{device.id}/apps/install",
            headers=admin_headers,
            json={"apk_id": apk["id"]},
        )
    ).json()

    await client.post(
        f"/api/v1/device/commands/{command['id']}/result",
        headers=device.headers,
        json={
            "status": "failed",
            "error": "INSTALL_FAILED_VERSION_DOWNGRADE",
            "result": {"mechanism": "package_installer"},
        },
    )

    stored = await session_command(db, command["id"])
    assert stored.status == "failed"
    assert stored.error == "INSTALL_FAILED_VERSION_DOWNGRADE"

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_COMPLETED)
        )
    ).scalars().all()
    assert audit[0].result == "failure"
    assert audit[0].actor_type == "device"

    apps = (
        await db.execute(sa.select(sa.func.count()).select_from(DeviceApp))
    ).scalar_one()
    assert apps == 0


# ----------------------------------------------------------- безпека ------


async def test_device_cannot_download_without_active_command(
    client, admin_headers, device, tmp_path, db
):
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=100)

    response = await client.get(
        f"/api/v1/device/apks/{apk['id']}/download", headers=device.headers
    )
    assert response.status_code == 403

    denied = (
        await db.execute(
            sa.select(AuditLog).where(
                AuditLog.action == AuditAction.APK_DOWNLOADED, AuditLog.result == "denied"
            )
        )
    ).scalars().all()
    assert denied[0].details["reason"] == "no_active_install_command"


async def test_device_cannot_touch_foreign_command(
    client, admin_headers, device, enroll_device, tmp_path
):
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=100)
    command = (
        await client.post(
            f"/api/v1/devices/{device.id}/apps/install",
            headers=admin_headers,
            json={"apk_id": apk["id"]},
        )
    ).json()

    other = await enroll_device(android_id="ffffffffffffffff")
    response = await client.post(
        f"/api/v1/device/commands/{command['id']}/ack", headers=other.headers
    )
    assert response.status_code == 404

    download = await client.get(
        f"/api/v1/device/apks/{apk['id']}/download", headers=other.headers
    )
    assert download.status_code == 403


async def test_anonymous_cannot_use_agent_endpoints(client):
    assert (await client.get("/api/v1/device/commands/pending")).status_code == 401
    assert (await client.post("/api/v1/device/apps", json={"apps": []})).status_code == 401


async def test_operator_cannot_install_but_can_launch(
    client, auth_headers, device, admin_headers, tmp_path
):
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=100)
    operator = await auth_headers(email="operator@example.com", role=Role.OPERATOR)

    denied = await client.post(
        f"/api/v1/devices/{device.id}/apps/install",
        headers=operator,
        json={"apk_id": apk["id"]},
    )
    assert denied.status_code == 403

    launched = await client.post(
        f"/api/v1/devices/{device.id}/apps/launch",
        headers=operator,
        json={"package_name": PACKAGE},
    )
    assert launched.status_code == 202
    assert launched.json()["type"] == "APP_LAUNCH"


async def test_auditor_can_read_apps_but_not_act(client, auth_headers, device):
    auditor = await auth_headers(email="auditor@example.com", role=Role.AUDITOR)
    readable = await client.get(f"/api/v1/devices/{device.id}/apps", headers=auditor)
    assert readable.status_code == 200

    denied = await client.post(
        f"/api/v1/devices/{device.id}/apps/launch",
        headers=auditor,
        json={"package_name": PACKAGE},
    )
    assert denied.status_code == 403


# ------------------------------------------------------ історія операцій --


async def test_command_history_and_cancel(client, admin_headers, device, tmp_path):
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=100)
    command = (
        await client.post(
            f"/api/v1/devices/{device.id}/apps/install",
            headers=admin_headers,
            json={"apk_id": apk["id"]},
        )
    ).json()

    history = await client.get(f"/api/v1/devices/{device.id}/commands", headers=admin_headers)
    assert history.status_code == 200
    assert history.json()["total"] == 1
    assert history.json()["items"][0]["type"] == "APP_INSTALL"

    cancelled = await client.post(
        f"/api/v1/commands/{command['id']}/cancel", headers=admin_headers
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    # Після скасування пристрій уже не отримує команду.
    pending = await client.get("/api/v1/device/commands/pending", headers=device.headers)
    assert pending.json() == []


async def test_inventory_sync_adds_updates_and_removes(client, device, db):
    await client.post(
        "/api/v1/device/apps",
        headers=device.headers,
        json={
            "apps": [
                {"package_name": "ua.example.a", "version_code": 1},
                {"package_name": "ua.example.b", "version_code": 2},
            ]
        },
    )
    await client.post(
        "/api/v1/device/apps",
        headers=device.headers,
        json={
            "apps": [
                {"package_name": "ua.example.a", "version_code": 3, "version_name": "1.3"},
                {"package_name": "ua.example.c", "version_code": 1},
            ]
        },
    )

    packages = (
        await db.execute(sa.select(DeviceApp.package_name).order_by(DeviceApp.package_name))
    ).scalars().all()
    assert packages == ["ua.example.a", "ua.example.c"]

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.APP_INVENTORY_REPORTED)
        )
    ).scalars().all()
    assert audit[-1].details["removed"] == 1
    assert audit[-1].details["added"] == 1


async def test_install_request_is_audited(client, admin_headers, device, tmp_path, db):
    apk = await upload_apk(client, admin_headers, tmp_path, version_code=100)
    await client.post(
        f"/api/v1/devices/{device.id}/apps/install",
        headers=admin_headers,
        json={"apk_id": apk["id"]},
    )

    rows = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.APP_INSTALL))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].details["phase"] == "requested"
    assert rows[0].details["package_name"] == PACKAGE
    assert rows[0].actor_type == "user"
