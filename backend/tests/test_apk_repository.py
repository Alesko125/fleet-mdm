import sqlalchemy as sa

from app.core.rbac import Role
from app.db.models import ApkArtifact, AuditLog
from app.services.audit import AuditAction
from tests.axml import build_apk


def apk_file(tmp_path, name="demo.apk", **kwargs):
    path = build_apk(tmp_path / name, **kwargs)
    return {"file": (name, path.read_bytes(), "application/vnd.android.package-archive")}


async def upload(client, headers, tmp_path, **kwargs):
    name = kwargs.pop("name", "demo.apk")
    return await client.post(
        "/api/v1/apks", headers=headers, files=apk_file(tmp_path, name, **kwargs)
    )


async def test_upload_extracts_metadata(client, admin_headers, tmp_path, db):
    response = await upload(
        client,
        admin_headers,
        tmp_path,
        package="ua.example.pos",
        version_code=1207,
        version_name="3.4.1",
        min_sdk=24,
        target_sdk=35,
        abis=("arm64-v8a", "x86_64"),
        permissions=("android.permission.INTERNET", "android.permission.CAMERA"),
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["package_name"] == "ua.example.pos"
    assert body["version_name"] == "3.4.1"
    assert body["version_code"] == 1207
    assert body["min_sdk"] == 24
    assert body["target_sdk"] == 35
    assert body["abis"] == ["arm64-v8a", "x86_64"]
    assert body["architecture"] == "arm64-v8a, x86_64"
    assert body["display_version"] == "3.4.1 (1207)"
    assert len(body["sha256"]) == 64
    assert body["size_bytes"] > 0
    assert body["deduplicated"] is False
    assert "android.permission.CAMERA" in body["permissions"]

    artifact = (await db.execute(sa.select(ApkArtifact))).scalars().one()
    assert artifact.storage_path.endswith(f"{artifact.sha256}.apk")


async def test_upload_is_audited(client, admin_headers, tmp_path, db):
    await upload(client, admin_headers, tmp_path, package="ua.example.audit")
    rows = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.APK_UPLOADED))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].details["package_name"] == "ua.example.audit"
    assert rows[0].details["sha256"]
    assert rows[0].actor_type == "user"


async def test_identical_upload_is_deduplicated(client, admin_headers, tmp_path, db):
    first = await upload(client, admin_headers, tmp_path, package="ua.example.dedupe")
    second = await upload(client, admin_headers, tmp_path, package="ua.example.dedupe")

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["deduplicated"] is True
    assert first.json()["id"] == second.json()["id"]

    total = (
        await db.execute(sa.select(sa.func.count()).select_from(ApkArtifact))
    ).scalar_one()
    assert total == 1


async def test_rejects_non_apk(client, admin_headers):
    response = await client.post(
        "/api/v1/apks",
        headers=admin_headers,
        files={"file": ("notes.txt", "це просто текст".encode(), "text/plain")},
    )
    assert response.status_code == 422
    assert "APK" in response.json()["error"]["message"]


async def test_rejects_split_apk(client, admin_headers, tmp_path):
    response = await upload(client, admin_headers, tmp_path, split="config.arm64_v8a")
    assert response.status_code == 422
    assert "Split" in response.json()["error"]["message"]


async def test_upload_requires_app_manage(client, auth_headers, tmp_path):
    headers = await auth_headers(email="viewer@example.com", role=Role.AUDITOR)
    response = await client.post(
        "/api/v1/apks", headers=headers, files=apk_file(tmp_path)
    )
    assert response.status_code == 403


async def test_list_and_filter(client, admin_headers, tmp_path):
    await upload(client, admin_headers, tmp_path, name="a.apk", package="ua.example.a")
    await upload(client, admin_headers, tmp_path, name="b.apk", package="ua.example.b")

    everything = await client.get("/api/v1/apks", headers=admin_headers)
    assert everything.json()["total"] == 2

    filtered = await client.get(
        "/api/v1/apks", headers=admin_headers, params={"package": "ua.example.b"}
    )
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["package_name"] == "ua.example.b"

    searched = await client.get(
        "/api/v1/apks", headers=admin_headers, params={"search": "example.a"}
    )
    assert searched.json()["total"] == 1


async def test_admin_download_and_audit(client, admin_headers, tmp_path, db):
    created = (await upload(client, admin_headers, tmp_path)).json()

    response = await client.get(
        f"/api/v1/apks/{created['id']}/download", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.headers["X-Checksum-SHA256"] == created["sha256"]
    assert len(response.content) == created["size_bytes"]

    rows = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.APK_DOWNLOADED)
        )
    ).scalars().all()
    assert rows[0].details["channel"] == "admin"


async def test_delete_removes_artifact(client, admin_headers, tmp_path, db):
    created = (await upload(client, admin_headers, tmp_path)).json()

    response = await client.delete(f"/api/v1/apks/{created['id']}", headers=admin_headers)
    assert response.status_code == 200

    total = (
        await db.execute(sa.select(sa.func.count()).select_from(ApkArtifact))
    ).scalar_one()
    assert total == 0

    rows = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.APK_DELETED))
    ).scalars().all()
    assert len(rows) == 1


async def test_notes_can_be_updated(client, admin_headers, tmp_path):
    created = (await upload(client, admin_headers, tmp_path)).json()
    response = await client.patch(
        f"/api/v1/apks/{created['id']}",
        headers=admin_headers,
        json={"notes": "Каса, версія для складу", "label": "POS"},
    )
    assert response.status_code == 200
    assert response.json()["notes"] == "Каса, версія для складу"
    assert response.json()["label"] == "POS"
