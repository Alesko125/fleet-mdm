"""Комунікаційний модуль (SMS / журнал дзвінків).

Наголос перевірок — на межах: вимкнено за замовчуванням, без згоди/дозволу
нічого не збирається, дані в БД лише зашифровані, кожен перегляд у аудиті,
ретеншн діє, а недоступні можливості лишаються недоступними.
"""

from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa

from app.core.rbac import Role
from app.db.models import AuditLog, CallLogEntry, CommsSettings, SmsMessage
from app.services.audit import AuditAction

# Ключ шифрування для тестів — вмикаємо модуль лише коли він налаштований.
_TEST_KEY = "test-comms-encryption-key-that-is-long-enough-0001"


@pytest.fixture(autouse=True)
def _encryption_key():
    from app.core import encryption
    from app.core.config import get_settings

    settings = get_settings()
    before = settings.comms_encryption_key
    settings.comms_encryption_key = _TEST_KEY
    encryption.get_encryptor.cache_clear()
    os.environ["COMMS_ENCRYPTION_KEY"] = _TEST_KEY
    yield
    settings.comms_encryption_key = before
    encryption.get_encryptor.cache_clear()


async def _enable(client, admin_headers, device, **flags):
    payload = {"enabled": True, "sms_enabled": True, "call_log_enabled": True, **flags}
    return await client.put(
        f"/api/v1/devices/{device.id}/comms", headers=admin_headers, json=payload
    )


# ---------------------------------------------------- стан за замовчуванням


class TestDefaults:
    async def test_module_is_disabled_by_default(self, client, admin_headers, device):
        response = await client.get(
            f"/api/v1/devices/{device.id}/comms", headers=admin_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["sms_enabled"] is False
        assert body["call_log_enabled"] is False
        assert body["enabled_at"] is None

    async def test_agent_config_collects_nothing_by_default(self, client, device):
        response = await client.get("/api/v1/device/comms/config", headers=device.headers)
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["collect_sms"] is False
        assert body["collect_call_log"] is False

    async def test_operator_cannot_manage(self, client, device, auth_headers):
        headers = await auth_headers(email="op@example.com", role=Role.OPERATOR)
        response = await client.get(
            f"/api/v1/devices/{device.id}/comms", headers=headers
        )
        assert response.status_code == 403


# ------------------------------------------------- вмикання й точний час ---


class TestEnableDisable:
    async def test_enable_records_exact_time_and_actor(
        self, client, admin_headers, device, db
    ):
        response = await _enable(client, admin_headers, device)
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["enabled_at"] is not None      # точний час увімкнення
        assert body["enabled_by_email"] == "admin@example.com"

        rows = (
            await db.execute(
                sa.select(AuditLog).where(AuditLog.action == AuditAction.COMMS_ENABLED)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].details["enabled_at"]

    async def test_disable_records_time_and_is_audited(
        self, client, admin_headers, device, db
    ):
        await _enable(client, admin_headers, device)
        response = await client.put(
            f"/api/v1/devices/{device.id}/comms",
            headers=admin_headers,
            json={"enabled": False},
        )
        assert response.status_code == 200
        assert response.json()["disabled_at"] is not None

        rows = (
            await db.execute(
                sa.select(AuditLog).where(AuditLog.action == AuditAction.COMMS_DISABLED)
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_config_change_is_audited_with_before_after(
        self, client, admin_headers, device, db
    ):
        await _enable(client, admin_headers, device, retention_days=15)
        rows = (
            await db.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == AuditAction.COMMS_CONFIG_CHANGED
                )
            )
        ).scalars().all()
        assert rows
        assert "before" in rows[0].details and "after" in rows[0].details

    async def test_retention_ceiling_is_enforced(self, client, admin_headers, device):
        response = await client.put(
            f"/api/v1/devices/{device.id}/comms",
            headers=admin_headers,
            json={"retention_days": 3650},
        )
        assert response.status_code == 422

    async def test_enable_without_encryption_is_refused(
        self, client, admin_headers, device
    ):
        """Без ключа шифрування модуль увімкнути не можна."""
        from app.core import encryption
        from app.core.config import get_settings

        settings = get_settings()
        settings.comms_encryption_key = ""
        encryption.get_encryptor.cache_clear()

        response = await _enable(client, admin_headers, device)
        assert response.status_code == 409
        assert "шифрування" in response.json()["error"]["message"]


# ------------------------------------------ збір лише за увімкненого модуля


class TestIngestGating:
    async def test_ingest_rejected_when_disabled(self, client, device):
        """Агент надіслав дані при вимкненому модулі — сервер відхиляє."""
        response = await client.post(
            "/api/v1/device/comms/data",
            headers=device.headers,
            json={"sms": [{"body": "привіт", "address": "0501112233"}], "calls": []},
        )
        assert response.status_code == 409

    async def test_sms_ignored_when_only_calls_enabled(
        self, client, admin_headers, device
    ):
        await _enable(client, admin_headers, device, sms_enabled=False)
        response = await client.post(
            "/api/v1/device/comms/data",
            headers=device.headers,
            json={
                "sms": [{"provider_id": "1", "body": "текст", "address": "0501112233"}],
                "calls": [{"provider_id": "1", "number": "0501112233", "direction": "incoming"}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["sms_stored"] == 0        # SMS відхилено — збір вимкнено
        assert body["calls_stored"] == 1
        assert body["rejected"] >= 1


# ------------------------------------------------- шифрування at rest ------


class TestEncryptionAtRest:
    async def test_body_and_address_are_encrypted_in_db(
        self, client, admin_headers, device, db
    ):
        await _enable(client, admin_headers, device)
        secret_text = "код підтвердження 4821"
        secret_number = "0509998877"
        await client.post(
            "/api/v1/device/comms/data",
            headers=device.headers,
            json={
                "sms": [{
                    "provider_id": "10",
                    "direction": "inbox",
                    "address": secret_number,
                    "body": secret_text,
                }],
                "calls": [],
            },
        )

        did = uuid.UUID(device.id)
        row = (
            await db.execute(sa.select(SmsMessage).where(SmsMessage.device_id == did))
        ).scalars().one()
        # Відкритого тексту в стовпцях немає.
        assert secret_text not in (row.body_encrypted or "")
        assert secret_number not in (row.address_encrypted or "")
        assert row.body_encrypted and row.address_encrypted
        # Довжина зберігається відкрито — для UI без розшифрування.
        assert row.body_length == len(secret_text)

    async def test_read_endpoint_decrypts_and_audits(
        self, client, admin_headers, device, db
    ):
        await _enable(client, admin_headers, device)
        await client.post(
            "/api/v1/device/comms/data",
            headers=device.headers,
            json={
                "sms": [{"provider_id": "20", "body": "таємниця", "address": "0501234567"}],
                "calls": [],
            },
        )

        response = await client.get(
            f"/api/v1/devices/{device.id}/comms/messages", headers=admin_headers
        )
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["body"] == "таємниця"           # розшифровано
        assert item["address"] == "0501234567"

        viewed = (
            await db.execute(
                sa.select(AuditLog).where(AuditLog.action == AuditAction.COMMS_DATA_VIEWED)
            )
        ).scalars().all()
        assert viewed and viewed[0].details["kind"] == "sms"

    async def test_metadata_only_drops_body(self, client, admin_headers, device, db):
        await _enable(client, admin_headers, device, metadata_only=True)
        await client.post(
            "/api/v1/device/comms/data",
            headers=device.headers,
            json={
                "sms": [{"provider_id": "30", "body": "не зберігати", "address": "0501234567"}],
                "calls": [],
            },
        )
        did = uuid.UUID(device.id)
        row = (
            await db.execute(sa.select(SmsMessage).where(SmsMessage.device_id == did))
        ).scalars().one()
        assert row.body_encrypted is None        # текст не збережено
        assert row.address_encrypted is not None  # номер лишається


# ----------------------------------------------- дозволи та їхній стан ----


class TestPermissionState:
    async def test_permission_report_is_stored_and_audited(
        self, client, admin_headers, device, db
    ):
        response = await client.post(
            "/api/v1/device/comms/permissions",
            headers=device.headers,
            json={"sms_permission": "denied", "call_log_permission": "granted"},
        )
        assert response.status_code == 200

        detail = (
            await client.get(f"/api/v1/devices/{device.id}/comms", headers=admin_headers)
        ).json()
        assert detail["sms_permission"] == "denied"
        assert detail["call_log_permission"] == "granted"

        rows = (
            await db.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == AuditAction.COMMS_PERMISSION_CHANGED
                )
            )
        ).scalars().all()
        assert rows

    async def test_unavailable_permission_is_representable(self, client, device):
        """Недоступну можливість позначаємо як 'unavailable', а не обходимо."""
        response = await client.post(
            "/api/v1/device/comms/permissions",
            headers=device.headers,
            json={"sms_permission": "unavailable", "call_log_permission": "unavailable"},
        )
        assert response.status_code == 200


# --------------------------------------------------- перегляд і дозволи ----


class TestReadAccess:
    async def test_read_requires_comms_read(self, client, admin_headers, device, auth_headers):
        await _enable(client, admin_headers, device)
        operator = await auth_headers(email="op2@example.com", role=Role.OPERATOR)
        response = await client.get(
            f"/api/v1/devices/{device.id}/comms/messages", headers=operator
        )
        assert response.status_code == 403

    async def test_purge_removes_all_and_audits(self, client, admin_headers, device, db):
        await _enable(client, admin_headers, device)
        await client.post(
            "/api/v1/device/comms/data",
            headers=device.headers,
            json={
                "sms": [{"provider_id": "40", "body": "x", "address": "1"}],
                "calls": [{"provider_id": "40", "number": "1", "direction": "missed"}],
            },
        )
        response = await client.post(
            f"/api/v1/devices/{device.id}/comms/purge", headers=admin_headers
        )
        assert response.status_code == 200

        assert (
            await db.execute(sa.select(sa.func.count()).select_from(SmsMessage))
        ).scalar_one() == 0
        assert (
            await db.execute(sa.select(sa.func.count()).select_from(CallLogEntry))
        ).scalar_one() == 0

        purged = (
            await db.execute(
                sa.select(AuditLog).where(AuditLog.action == AuditAction.COMMS_DATA_PURGED)
            )
        ).scalars().all()
        assert purged


# ---------------------------------------------------------- ретеншн -------


class TestRetention:
    async def test_expired_records_are_purged(self, client, admin_headers, device, db):
        from datetime import timedelta

        from app.core.security import utcnow
        from app.services import comms as comms_service

        await _enable(client, admin_headers, device, retention_days=1)
        await client.post(
            "/api/v1/device/comms/data",
            headers=device.headers,
            json={"sms": [{"provider_id": "50", "body": "старе", "address": "1"}], "calls": []},
        )
        # Штучно «зістарюємо» запис.
        row = (
            await db.execute(sa.select(SmsMessage))
        ).scalars().one()
        row.collected_at = utcnow() - timedelta(days=3)
        await db.commit()

        removed = await comms_service.purge_expired(db)
        await db.commit()
        assert removed["comms_sms"] == 1

        assert (
            await db.execute(sa.select(sa.func.count()).select_from(SmsMessage))
        ).scalar_one() == 0

    async def test_deleting_device_cascades_comms(
        self, client, admin_headers, device, db
    ):
        await _enable(client, admin_headers, device)
        await client.post(
            "/api/v1/device/comms/data",
            headers=device.headers,
            json={"sms": [{"provider_id": "60", "body": "x", "address": "1"}], "calls": []},
        )
        settings_row = await db.get(CommsSettings, uuid.UUID(device.id))
        assert settings_row is not None
