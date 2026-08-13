"""Реєстр команд.

Головне, що тут перевіряється: виконати можна **лише** описане в реєстрі,
з параметрами за схемою й з дозволом, який чинний просто зараз.
Довільної дії, яку агент виконав би «як є», в системі немає.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.core.rbac import Permission, Role
from app.db.models import AuditLog, Command, CommandPermission, Device
from app.services import command_registry
from app.services import commands as command_service
from app.services.audit import AuditAction


async def _issue(client, headers, device_id: str, command_type: str, params=None):
    return await client.post(
        f"/api/v1/devices/{device_id}/commands",
        headers=headers,
        json={"type": command_type, "params": params},
    )


# ------------------------------------------------------- сам реєстр ------


class TestRegistryShape:
    def test_every_spec_is_self_describing(self):
        """Опис команди має пояснювати наслідки — порожнім не буває."""
        for spec in command_registry.specs():
            assert spec.title
            assert spec.description
            # Що побачить власник пристрою — обовʼязкове поле.
            assert spec.user_visible
            assert spec.ttl_seconds > 0
            assert isinstance(spec.permission, Permission)

    def test_required_commands_are_present(self):
        expected = {
            "REBOOT",
            "REFRESH_TELEMETRY",
            "SYNC_CONFIG",
            "AGENT_RESTART",
            "COLLECT_DIAGNOSTICS",
            "APP_INSTALL",
            "APP_UNINSTALL",
            "APP_LAUNCH",
            "APP_STOP",
        }
        assert expected <= set(command_registry.known_types())

    def test_no_shell_like_command_exists(self):
        """У реєстрі не має бути нічого, що виконує довільний рядок."""
        forbidden = {"SHELL", "EXEC", "RUN", "SU", "ADB", "SCRIPT", "EVAL"}
        for spec in command_registry.specs():
            assert not (set(spec.type.split("_")) & forbidden), spec.type
            # Жодна схема параметрів не приймає поля на кшталт command/script.
            fields = set(spec.params_model.model_fields)
            assert not (fields & {"command", "cmd", "script", "shell", "args"})

    def test_privileged_commands_are_marked(self):
        reboot = command_registry.get_spec("REBOOT")
        assert reboot.requires_device_owner is True
        assert reboot.disruptive is True
        # Перезавантаження не може бути «безпечною» дією.
        assert reboot.permission is Permission.DEVICE_ACTION_CRITICAL

    def test_registry_types_match_database_enum(self):
        from app.db.models import COMMAND_TYPE_VALUES

        assert set(command_registry.known_types()) <= set(COMMAND_TYPE_VALUES)


# ------------------------------------------------- валідація параметрів --


class TestParameterValidation:
    def test_unknown_fields_are_rejected(self):
        """Зайве поле — помилка, а не мовчазне ігнорування."""
        spec = command_registry.get_spec("APP_LAUNCH")
        from app.core.errors import ValidationFailedError

        with pytest.raises(ValidationFailedError):
            command_service.validate_params(
                spec, {"package_name": "ua.example.app", "as_root": True}
            )

    def test_package_name_pattern_is_enforced(self):
        spec = command_registry.get_spec("APP_LAUNCH")
        from app.core.errors import ValidationFailedError

        for bad in ["../../etc", "ua.example.app; rm -rf /", "", "no-dots"]:
            with pytest.raises(ValidationFailedError):
                command_service.validate_params(spec, {"package_name": bad})

    def test_defaults_are_applied(self):
        spec = command_registry.get_spec("COLLECT_DIAGNOSTICS")
        params = command_service.validate_params(spec, {})
        assert params["include_agent_log"] is True
        assert params["log_lines"] == 200

    def test_bounds_are_enforced(self):
        spec = command_registry.get_spec("COLLECT_DIAGNOSTICS")
        from app.core.errors import ValidationFailedError

        with pytest.raises(ValidationFailedError):
            command_service.validate_params(spec, {"log_lines": 99999})


# ---------------------------------------------------------- видача -------


class TestIssuing:
    async def test_safe_command_is_issued(self, client, admin_headers, device):
        response = await _issue(client, admin_headers, device.id, "REFRESH_TELEMETRY")
        assert response.status_code == 202, response.text

        body = response.json()
        assert body["type"] == "REFRESH_TELEMETRY"
        assert body["status"] == "pending"
        assert body["device_id"] == device.id
        assert body["created_by_id"]
        assert body["created_at"]
        assert body["expires_at"]

    async def test_unknown_type_is_rejected(self, client, admin_headers, device):
        response = await _issue(client, admin_headers, device.id, "SHELL_EXEC")
        assert response.status_code == 404
        assert "реєстрі" in response.json()["error"]["message"]

    async def test_rejection_is_audited(self, client, admin_headers, device, db):
        await _issue(client, admin_headers, device.id, "SHELL_EXEC")

        rows = (
            await db.execute(
                sa.select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_REJECTED)
            )
        ).scalars().all()
        assert rows
        assert rows[0].result == "denied"
        assert rows[0].details["reason"] == "unknown_command"

    async def test_bad_params_are_rejected(self, client, admin_headers, device):
        response = await _issue(
            client, admin_headers, device.id, "APP_LAUNCH", {"package_name": "не пакет"}
        )
        assert response.status_code == 422

    async def test_issue_writes_two_audit_records(self, client, admin_headers, device, db):
        """Однорідний слід COMMAND_ISSUED плюс доменна подія."""
        await _issue(client, admin_headers, device.id, "REFRESH_TELEMETRY")

        issued = (
            await db.execute(
                sa.select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_ISSUED)
            )
        ).scalars().all()
        domain = (
            await db.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == AuditAction.DEVICE_REFRESH_TELEMETRY
                )
            )
        ).scalars().all()

        assert len(issued) == 1
        assert len(domain) == 1
        assert issued[0].details["command_type"] == "REFRESH_TELEMETRY"
        assert issued[0].details["disruptive"] is False

    async def test_duplicate_command_is_rejected(self, client, admin_headers, device):
        first = await _issue(client, admin_headers, device.id, "REFRESH_TELEMETRY")
        assert first.status_code == 202
        second = await _issue(client, admin_headers, device.id, "REFRESH_TELEMETRY")
        assert second.status_code == 409

    async def test_one_operation_per_package(self, client, admin_headers, device):
        first = await _issue(
            client, admin_headers, device.id, "APP_STOP", {"package_name": "ua.example.app"}
        )
        assert first.status_code == 202

        second = await _issue(
            client, admin_headers, device.id, "APP_LAUNCH", {"package_name": "ua.example.app"}
        )
        # APP_LAUNCH не singleton — не конфліктує.
        assert second.status_code == 202

        third = await _issue(
            client, admin_headers, device.id, "APP_STOP", {"package_name": "ua.example.app"}
        )
        assert third.status_code == 409

    async def test_command_for_unknown_device_is_404(self, client, admin_headers):
        response = await _issue(client, admin_headers, str(uuid.uuid4()), "REFRESH_TELEMETRY")
        assert response.status_code == 404


# ------------------------------------------- вимоги до режиму пристрою ---


class TestDeviceOwnerRequirement:
    async def test_reboot_requires_device_owner(
        self, client, admin_headers, enroll_device, db
    ):
        """Пристрій без Device Owner дістає зрозумілу відмову одразу."""
        unmanaged = await enroll_device(
            android_id="unmanaged0000001", management_mode="unmanaged"
        )
        response = await _issue(client, admin_headers, unmanaged.id, "REBOOT")
        assert response.status_code == 409
        assert "Device Owner" in response.json()["error"]["message"]

        rows = (
            await db.execute(
                sa.select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_REJECTED)
            )
        ).scalars().all()
        assert any(r.details["reason"] == "device_owner_required" for r in rows)

    async def test_reboot_allowed_for_device_owner(self, client, admin_headers, device):
        # Фікстура `device` реєструється в режимі device_owner.
        response = await _issue(client, admin_headers, device.id, "REBOOT", {"reason": "оновлення"})
        assert response.status_code == 202
        assert response.json()["payload"]["reason"] == "оновлення"


# --------------------------------------------- конфігуровані дозволи -----


class TestConfigurablePermissions:
    async def test_registry_lists_defaults(self, client, admin_headers):
        response = await client.get("/api/v1/commands/registry", headers=admin_headers)
        assert response.status_code == 200

        by_type = {item["type"]: item for item in response.json()}
        assert by_type["REBOOT"]["required_permission"] == "device:action:critical"
        assert by_type["REBOOT"]["overridden"] is False
        assert by_type["REBOOT"]["allowed_for_me"] is True
        assert "superadmin" in by_type["REBOOT"]["roles"]
        assert "auditor" not in by_type["REBOOT"]["roles"]

    async def test_operator_cannot_reboot_by_default(
        self, client, admin_headers, device, auth_headers
    ):
        headers = await auth_headers(email="op@example.com", role=Role.OPERATOR)
        response = await _issue(client, headers, device.id, "REBOOT")
        assert response.status_code == 403

    async def test_permission_can_be_widened(
        self, client, admin_headers, device, auth_headers
    ):
        """Матрицю можна змінити без релізу."""
        headers = await auth_headers(email="op2@example.com", role=Role.OPERATOR)
        assert (await _issue(client, headers, device.id, "REBOOT")).status_code == 403

        changed = await client.patch(
            "/api/v1/commands/registry/REBOOT/permission",
            headers=admin_headers,
            json={"required_permission": "device:action:safe"},
        )
        assert changed.status_code == 200
        assert changed.json()["overridden"] is True
        assert "operator" in changed.json()["roles"]

        assert (await _issue(client, headers, device.id, "REBOOT")).status_code == 202

    async def test_permission_can_be_narrowed(self, client, admin_headers, device):
        await client.patch(
            "/api/v1/commands/registry/REFRESH_TELEMETRY/permission",
            headers=admin_headers,
            json={"required_permission": "settings:manage"},
        )
        # Суперадміністратор має settings:manage — команда лишається доступною.
        assert (
            await _issue(client, admin_headers, device.id, "REFRESH_TELEMETRY")
        ).status_code == 202

    async def test_command_can_be_disabled_entirely(self, client, admin_headers, device):
        """Вимкнену команду не видасть навіть суперадміністратор."""
        await client.patch(
            "/api/v1/commands/registry/REBOOT/permission",
            headers=admin_headers,
            json={"enabled": False},
        )
        response = await _issue(client, admin_headers, device.id, "REBOOT")
        assert response.status_code == 403
        assert "вимкнено" in response.json()["error"]["message"]

    async def test_permission_change_is_audited(self, client, admin_headers, db):
        await client.patch(
            "/api/v1/commands/registry/REBOOT/permission",
            headers=admin_headers,
            json={"required_permission": "device:action:safe"},
        )
        rows = (
            await db.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == AuditAction.COMMAND_PERMISSION_CHANGED
                )
            )
        ).scalars().all()
        assert rows
        assert rows[0].details["before"]["required_permission"] == "device:action:critical"
        assert rows[0].details["after"]["required_permission"] == "device:action:safe"

    async def test_permission_can_be_reset(self, client, admin_headers, db):
        await client.patch(
            "/api/v1/commands/registry/REBOOT/permission",
            headers=admin_headers,
            json={"required_permission": "device:action:safe"},
        )
        response = await client.delete(
            "/api/v1/commands/registry/REBOOT/permission", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["overridden"] is False
        assert response.json()["required_permission"] == "device:action:critical"

        rows = (await db.execute(sa.select(CommandPermission))).scalars().all()
        assert not rows

    async def test_unknown_permission_is_rejected(self, client, admin_headers):
        response = await client.patch(
            "/api/v1/commands/registry/REBOOT/permission",
            headers=admin_headers,
            json={"required_permission": "device:pwn"},
        )
        assert response.status_code == 422

    async def test_config_cannot_introduce_new_command(self, client, admin_headers):
        """Конфігурація не є способом протягнути дію повз реєстр."""
        response = await client.patch(
            "/api/v1/commands/registry/SHELL_EXEC/permission",
            headers=admin_headers,
            json={"required_permission": "device:action:safe"},
        )
        assert response.status_code == 404

    async def test_only_settings_manager_changes_matrix(self, client, auth_headers):
        headers = await auth_headers(email="adm@example.com", role=Role.ADMIN)
        response = await client.patch(
            "/api/v1/commands/registry/REBOOT/permission",
            headers=headers,
            json={"enabled": False},
        )
        assert response.status_code == 403


# ------------------------------------------------- тривалість і статус ---


class TestLifecycle:
    async def test_duration_is_measured(self, client, admin_headers, device, db):
        created = (
            await _issue(client, admin_headers, device.id, "REFRESH_TELEMETRY")
        ).json()

        # Агент забирає команду, підтверджує і звітує результат.
        pending = await client.get(
            "/api/v1/device/commands/pending", headers=device.headers
        )
        assert pending.status_code == 200
        await client.post(
            f"/api/v1/device/commands/{created['id']}/ack", headers=device.headers
        )
        await client.post(
            f"/api/v1/device/commands/{created['id']}/result",
            headers=device.headers,
            json={"status": "succeeded", "result": {"battery": 80}},
        )

        row = await db.get(Command, uuid.UUID(created["id"]))
        await db.refresh(row)
        assert row.status == "succeeded"
        assert row.duration_ms is not None
        assert row.duration_ms >= 0
        assert row.queue_ms is not None

    async def test_detail_includes_registry_metadata(
        self, client, admin_headers, device
    ):
        created = (await _issue(client, admin_headers, device.id, "REBOOT")).json()

        detail = (
            await client.get(f"/api/v1/commands/{created['id']}", headers=admin_headers)
        ).json()
        assert detail["title"] == "Перезавантажити пристрій"
        assert detail["requires_device_owner"] is True
        assert detail["disruptive"] is True
        assert detail["created_by_email"] == "admin@example.com"
        assert detail["device_name"]
        assert detail["is_terminal"] is False

    async def test_stats_are_grouped_by_type(self, client, admin_headers, device):
        await _issue(client, admin_headers, device.id, "REFRESH_TELEMETRY")
        await _issue(client, admin_headers, device.id, "SYNC_CONFIG")

        stats = (
            await client.get("/api/v1/commands/stats", headers=admin_headers)
        ).json()
        by_type = {item["type"]: item for item in stats}
        assert by_type["REFRESH_TELEMETRY"]["total"] == 1
        assert by_type["REFRESH_TELEMETRY"]["pending"] == 1

    async def test_operator_can_cancel_own_safe_command(
        self, client, admin_headers, device, auth_headers
    ):
        """Право скасувати дорівнює праву видати."""
        headers = await auth_headers(email="op3@example.com", role=Role.OPERATOR)
        created = (await _issue(client, headers, device.id, "REFRESH_TELEMETRY")).json()

        response = await client.post(
            f"/api/v1/commands/{created['id']}/cancel", headers=headers
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

    async def test_operator_cannot_cancel_critical_command(
        self, client, admin_headers, device, auth_headers
    ):
        created = (await _issue(client, admin_headers, device.id, "REBOOT")).json()
        headers = await auth_headers(email="op4@example.com", role=Role.OPERATOR)

        response = await client.post(
            f"/api/v1/commands/{created['id']}/cancel", headers=headers
        )
        assert response.status_code == 403


# ------------------------------------------------- спеціальні ендпоїнти --


class TestConvenienceEndpoints:
    async def test_device_action_endpoints_use_registry(
        self, client, admin_headers, device
    ):
        for path, expected in (
            ("telemetry/refresh", "REFRESH_TELEMETRY"),
            ("config/sync", "SYNC_CONFIG"),
            ("agent/restart", "AGENT_RESTART"),
            ("diagnostics", "COLLECT_DIAGNOSTICS"),
        ):
            response = await client.post(
                f"/api/v1/devices/{device.id}/{path}", headers=admin_headers
            )
            assert response.status_code == 202, (path, response.text)
            assert response.json()["type"] == expected

    async def test_convenience_endpoint_honours_reconfigured_matrix(
        self, client, admin_headers, device, auth_headers
    ):
        """Перенастроєна матриця діє і на спеціалізованих ендпоїнтах."""
        await client.patch(
            "/api/v1/commands/registry/REFRESH_TELEMETRY/permission",
            headers=admin_headers,
            json={"required_permission": "settings:manage"},
        )
        headers = await auth_headers(email="op5@example.com", role=Role.OPERATOR)

        response = await client.post(
            f"/api/v1/devices/{device.id}/telemetry/refresh", headers=headers
        )
        assert response.status_code == 403

    async def test_agent_cannot_be_stopped_by_app_stop(
        self, client, admin_headers, device
    ):
        response = await client.post(
            f"/api/v1/devices/{device.id}/apps/stop",
            headers=admin_headers,
            json={"package_name": "ua.fleet.agent"},
        )
        assert response.status_code == 409
        assert "агента" in response.json()["error"]["message"].lower()

    async def test_suspended_device_accepts_no_commands(
        self, client, admin_headers, device, db
    ):
        row = await db.get(Device, uuid.UUID(device.id))
        row.status = "suspended"
        await db.commit()

        response = await _issue(client, admin_headers, device.id, "REFRESH_TELEMETRY")
        assert response.status_code in (409, 422)
