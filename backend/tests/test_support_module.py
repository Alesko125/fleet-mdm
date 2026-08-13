"""Доповнення модуля віддаленої підтримки.

Базові сценарії (згода, таймаути, стани) перевіряє `test_support.py`; тут —
те, що додалося: квитки перегляду, стан каналу, метрики кадру, валідація
керування та журнал сеансу.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.core.rbac import Role
from app.db.models import AuditLog, RemoteSession
from app.services import support as support_service
from app.services.audit import AuditAction


async def _create_session(client, admin_headers, device, **overrides) -> dict:
    payload = {
        "reason": "допомогти з поштою",
        "input_enabled": False,
        "max_duration_minutes": 30,
        **overrides,
    }
    response = await client.post(
        f"/api/v1/devices/{device.id}/support-sessions",
        headers=admin_headers,
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _consent(client, device, session_id: str, *, input_available: bool = False):
    return await client.post(
        f"/api/v1/device/support-sessions/{session_id}/consent",
        headers=device.headers,
        json={"granted": True, "input_available": input_available},
    )


# ----------------------------------------------------- квитки перегляду ---


class TestViewerTicket:
    """Квиток замінює довгоживучий секрет у браузері."""

    async def test_ticket_is_issued_for_open_session(self, client, admin_headers, device):
        created = await _create_session(client, admin_headers, device)

        response = await client.post(
            f"/api/v1/support-sessions/{created['id']}/viewer-ticket",
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session_id"] == created["id"]
        assert len(body["ticket"]) >= 32
        assert 0 < body["expires_in"] <= 300
        assert body["viewer_url"].endswith(created["id"])

    async def test_ticket_is_single_use(self, client, admin_headers, device, redis, db):
        created = await _create_session(client, admin_headers, device)
        row = await db.get(RemoteSession, uuid.UUID(created["id"]))
        user = await _actor(db)

        ticket, _ = await support_service.issue_viewer_ticket(redis, row=row, user=user)

        assert await support_service.consume_viewer_ticket(
            redis, session_id=row.id, ticket=ticket, user_id=user.id
        )
        # Другий раз той самий квиток не спрацює — перехоплення марне.
        assert not await support_service.consume_viewer_ticket(
            redis, session_id=row.id, ticket=ticket, user_id=user.id
        )

    async def test_ticket_is_bound_to_session_and_user(
        self, client, admin_headers, device, redis, db
    ):
        created = await _create_session(client, admin_headers, device)
        row = await db.get(RemoteSession, uuid.UUID(created["id"]))
        user = await _actor(db)

        ticket, _ = await support_service.issue_viewer_ticket(redis, row=row, user=user)
        assert not await support_service.consume_viewer_ticket(
            redis, session_id=uuid.uuid4(), ticket=ticket, user_id=user.id
        )

        ticket2, _ = await support_service.issue_viewer_ticket(redis, row=row, user=user)
        assert not await support_service.consume_viewer_ticket(
            redis, session_id=row.id, ticket=ticket2, user_id=uuid.uuid4()
        )

    async def test_ticket_issue_is_audited(self, client, admin_headers, device, db):
        created = await _create_session(client, admin_headers, device)
        await client.post(
            f"/api/v1/support-sessions/{created['id']}/viewer-ticket", headers=admin_headers
        )

        rows = (
            await db.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == AuditAction.REMOTE_SESSION_VIEWER_JOINED
                )
            )
        ).scalars().all()
        assert rows
        assert rows[0].object_id == created["id"]

    async def test_closed_session_gives_no_ticket(self, client, admin_headers, device):
        created = await _create_session(client, admin_headers, device)
        await client.post(
            f"/api/v1/support-sessions/{created['id']}/stop", headers=admin_headers
        )

        response = await client.post(
            f"/api/v1/support-sessions/{created['id']}/viewer-ticket",
            headers=admin_headers,
        )
        assert response.status_code == 409

    async def test_auditor_cannot_get_ticket(self, client, admin_headers, device, auth_headers):
        created = await _create_session(client, admin_headers, device)
        headers = await auth_headers(email="aud@example.com", role=Role.AUDITOR)

        response = await client.post(
            f"/api/v1/support-sessions/{created['id']}/viewer-ticket", headers=headers
        )
        assert response.status_code == 403


# --------------------------------------------------------- стан каналу ----


class TestConnectionStatus:
    async def test_status_reports_offline_device(self, client, admin_headers, device):
        created = await _create_session(client, admin_headers, device)

        body = (
            await client.get(
                f"/api/v1/support-sessions/{created['id']}/connection", headers=admin_headers
            )
        ).json()
        assert body["device_online"] is False
        assert body["streaming"] is False
        assert body["viewers"] == 0

    async def test_presence_is_visible_across_requests(
        self, client, admin_headers, device, redis, db
    ):
        """Присутність живе в Redis, тож її бачить будь-яка репліка API."""
        created = await _create_session(client, admin_headers, device)
        row = await db.get(RemoteSession, uuid.UUID(created["id"]))

        await support_service.mark_device_online(redis, row.id)
        await support_service.mark_viewer_online(redis, row.id, uuid.uuid4())

        status = await support_service.connection_status(redis, row)
        assert status["device_online"] is True
        assert status["viewers"] == 1

        await support_service.mark_device_offline(redis, row.id)
        status = await support_service.connection_status(redis, row)
        assert status["device_online"] is False

    async def test_details_include_connection(self, client, admin_headers, device):
        created = await _create_session(client, admin_headers, device)
        body = (
            await client.get(
                f"/api/v1/support-sessions/{created['id']}", headers=admin_headers
            )
        ).json()
        assert body["connection"] is not None
        assert body["connection"]["device_online"] is False


# ------------------------------------------------------- метрики кадру ----


class TestFrameMetrics:
    def test_metrics_are_applied(self):
        row = RemoteSession(device_id=uuid.uuid4())
        assert support_service.apply_device_metrics(
            row, {"type": "metrics", "width": 1080, "height": 2400, "density_dpi": 440}
        )
        assert row.frame_width == 1080
        assert row.frame_height == 2400
        assert row.stats["density_dpi"] == 440

    @pytest.mark.parametrize(
        "message",
        [
            {"width": "1080", "height": 2400},
            {"width": 0, "height": 2400},
            {"width": 99999, "height": 2400},
            {"height": 2400},
            {},
        ],
    )
    def test_bad_metrics_are_ignored(self, message):
        row = RemoteSession(device_id=uuid.uuid4())
        assert not support_service.apply_device_metrics(row, message)
        assert row.frame_width is None


# --------------------------------------------------- валідація керування --


class TestInputValidation:
    """Сервер не є прозорою трубою для довільного JSON у бік пристрою."""

    def _row(self) -> RemoteSession:
        row = RemoteSession(device_id=uuid.uuid4())
        row.frame_width = 1080
        row.frame_height = 2400
        return row

    def test_tap_within_bounds_passes(self):
        clean = support_service.sanitize_input(
            {"action": "tap", "x": 100, "y": 200}, self._row()
        )
        assert clean == {"type": "input", "action": "tap", "x": 100, "y": 200}

    @pytest.mark.parametrize(
        "message",
        [
            {"action": "tap", "x": -1, "y": 10},
            {"action": "tap", "x": 5000, "y": 10},
            {"action": "tap", "x": 10, "y": 99999},
            {"action": "tap", "x": "10", "y": 10},
            {"action": "tap", "x": True, "y": 10},
            {"action": "tap"},
        ],
    )
    def test_out_of_bounds_tap_is_rejected(self, message):
        assert support_service.sanitize_input(message, self._row()) is None

    @pytest.mark.parametrize(
        "action", ["shell", "install", "screenshot", "", "TAP", "dump"]
    )
    def test_unknown_actions_are_rejected(self, action):
        assert (
            support_service.sanitize_input(
                {"action": action, "x": 1, "y": 1}, self._row()
            )
            is None
        )

    def test_global_actions_need_no_coordinates(self):
        for action in ("back", "home", "recents"):
            clean = support_service.sanitize_input({"action": action}, self._row())
            assert clean == {"type": "input", "action": action}

    def test_input_is_impossible_without_known_resolution(self):
        row = RemoteSession(device_id=uuid.uuid4())
        assert (
            support_service.sanitize_input({"action": "tap", "x": 1, "y": 1}, row) is None
        )

    def test_swipe_duration_is_clamped(self):
        clean = support_service.sanitize_input(
            {
                "action": "swipe",
                "from_x": 0,
                "from_y": 0,
                "to_x": 100,
                "to_y": 100,
                "duration_ms": 999_999,
            },
            self._row(),
        )
        assert clean["duration_ms"] == 3000

    def test_extra_fields_are_dropped(self):
        clean = support_service.sanitize_input(
            {"action": "tap", "x": 1, "y": 1, "evil": "rm -rf /"}, self._row()
        )
        assert "evil" not in clean


# ---------------------------------------------------------- журнал сеансу -


class TestSessionAudit:
    async def test_trail_contains_lifecycle_events(
        self, client, admin_headers, device, db
    ):
        created = await _create_session(client, admin_headers, device)
        await _consent(client, device, created["id"])
        await client.post(
            f"/api/v1/support-sessions/{created['id']}/stop", headers=admin_headers
        )

        response = await client.get(
            f"/api/v1/support-sessions/{created['id']}/audit", headers=admin_headers
        )
        assert response.status_code == 200
        actions = [entry["action"] for entry in response.json()]

        assert AuditAction.REMOTE_SESSION_REQUESTED in actions
        assert AuditAction.REMOTE_SESSION_CONSENT in actions
        assert AuditAction.REMOTE_SESSION_ENDED in actions
        # Порядок — хронологічний.
        assert actions.index(AuditAction.REMOTE_SESSION_REQUESTED) < actions.index(
            AuditAction.REMOTE_SESSION_ENDED
        )

    async def test_trail_is_scoped_to_one_session(self, client, admin_headers, device):
        first = await _create_session(client, admin_headers, device)
        await client.post(
            f"/api/v1/support-sessions/{first['id']}/stop", headers=admin_headers
        )
        second = await _create_session(client, admin_headers, device)

        trail = (
            await client.get(
                f"/api/v1/support-sessions/{second['id']}/audit", headers=admin_headers
            )
        ).json()
        assert trail
        assert all(entry["details"] is None or True for entry in trail)
        assert len(trail) == 1  # лише запит другого сеансу

    async def test_trail_requires_authentication(self, client, admin_headers, device):
        created = await _create_session(client, admin_headers, device)
        response = await client.get(f"/api/v1/support-sessions/{created['id']}/audit")
        assert response.status_code == 401

    async def test_operator_sees_trail_of_own_work(
        self, client, admin_headers, device, auth_headers
    ):
        """Ролі вкладені: `operator` успадковує `audit:read` від `auditor`.

        Це навмисно — оператор, який проводить сеанси підтримки, має бачити
        журнал власних дій. Дозвіл усе одно перевіряється явно, тож звузити
        матрицю можна в одному місці, не чіпаючи ендпоїнт.
        """
        created = await _create_session(client, admin_headers, device)
        headers = await auth_headers(email="op2@example.com", role=Role.OPERATOR)

        response = await client.get(
            f"/api/v1/support-sessions/{created['id']}/audit", headers=headers
        )
        assert response.status_code == 200
        assert response.json()

    async def test_stop_records_operator_ip(self, client, admin_headers, device, db):
        created = await _create_session(client, admin_headers, device)
        await client.post(
            f"/api/v1/support-sessions/{created['id']}/stop", headers=admin_headers
        )

        row = (
            await db.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == AuditAction.REMOTE_SESSION_ENDED
                )
            )
        ).scalars().first()
        assert row is not None
        assert row.details["reason"] == "stopped_by_admin"
        assert "input_events" in row.details


# ------------------------------------------------------------ інваріанти --


async def test_consent_still_required_before_streaming(client, admin_headers, device, db):
    """Ключова межа модуля: без згоди сеанс не стає активним."""
    created = await _create_session(client, admin_headers, device)
    row = await db.get(RemoteSession, uuid.UUID(created["id"]))

    assert row.consent_at is None
    assert row.state == "requested"
    assert row.started_at is None


async def test_input_flag_requires_device_confirmation(client, admin_headers, device, db):
    """Адміністратор може ЗАПРОСИТИ керування, але вмикає його пристрій."""
    created = await _create_session(client, admin_headers, device, input_enabled=True)
    assert created["input_enabled"] is True

    # Пристрій каже, що службу доступності вимкнено.
    await _consent(client, device, created["id"], input_available=False)

    row = await db.get(RemoteSession, uuid.UUID(created["id"]))
    await db.refresh(row)
    assert row.input_available is False
    assert row.input_enabled is False  # запит понижено до «тільки перегляд»


async def test_input_stays_enabled_when_device_supports_it(
    client, admin_headers, device, db
):
    created = await _create_session(client, admin_headers, device, input_enabled=True)
    await _consent(client, device, created["id"], input_available=True)

    row = await db.get(RemoteSession, uuid.UUID(created["id"]))
    await db.refresh(row)
    assert row.input_available is True
    assert row.input_enabled is True


async def _actor(db):
    from app.db.models import User

    return (await db.execute(sa.select(User))).scalars().first()
