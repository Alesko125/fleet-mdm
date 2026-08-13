import sqlalchemy as sa

from app.core.rbac import Permission, Role, permissions_for, role_has_permission
from app.db.models import AuditLog
from app.services.audit import AuditAction


def test_role_hierarchy_is_cumulative():
    assert permissions_for(Role.AUDITOR) < permissions_for(Role.OPERATOR)
    assert permissions_for(Role.OPERATOR) < permissions_for(Role.ADMIN)
    assert permissions_for(Role.ADMIN) < permissions_for(Role.SUPERADMIN)


def test_auditor_is_read_only():
    for permission in (
        Permission.DEVICE_WRITE,
        Permission.DEVICE_DELETE,
        Permission.APP_MANAGE,
        Permission.FILE_WRITE,
        Permission.LOCATION_MANAGE,
        Permission.ENROLLMENT_MANAGE,
        Permission.USER_MANAGE,
    ):
        assert not role_has_permission(Role.AUDITOR, permission)
    assert role_has_permission(Role.AUDITOR, Permission.AUDIT_READ)


def test_operator_cannot_perform_critical_actions():
    assert role_has_permission(Role.OPERATOR, Permission.DEVICE_ACTION_SAFE)
    assert not role_has_permission(Role.OPERATOR, Permission.DEVICE_ACTION_CRITICAL)


def test_unknown_role_has_no_permissions():
    assert permissions_for("hacker") == frozenset()


async def test_auditor_denied_on_enrollment_and_audited(client, auth_headers, db):
    headers = await auth_headers(email="auditor@example.com", role=Role.AUDITOR)
    response = await client.post("/api/v1/enrollment/tokens", headers=headers, json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"

    rows = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.ACCESS_DENIED))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].result == "denied"
    assert rows[0].details["permission"] == Permission.ENROLLMENT_MANAGE.value
    assert rows[0].details["role"] == "auditor"


async def test_auditor_can_read_audit_log(client, auth_headers):
    headers = await auth_headers(email="auditor@example.com", role=Role.AUDITOR)
    response = await client.get("/api/v1/audit", headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] >= 1  # щонайменше власний вхід


async def test_operator_cannot_manage_users(client, auth_headers):
    headers = await auth_headers(email="operator@example.com", role=Role.OPERATOR)
    response = await client.get("/api/v1/users", headers=headers)
    assert response.status_code == 403


async def test_admin_can_create_enrollment_token(client, auth_headers):
    headers = await auth_headers(email="admin2@example.com", role=Role.ADMIN)
    response = await client.post(
        "/api/v1/enrollment/tokens", headers=headers, json={"label": "Склад №2"}
    )
    assert response.status_code == 201, response.text
