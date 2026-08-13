import hashlib

import sqlalchemy as sa

from app.core.rbac import Role
from app.db.models import AuditLog, FileRoot, FileTransfer
from app.services.audit import AuditAction

ROOT = "/sdcard/Fleet"


async def add_root(client, headers, device_id, path=ROOT, **kwargs):
    return await client.post(
        f"/api/v1/devices/{device_id}/files/roots",
        headers=headers,
        json={"path": path, "label": "Робочий каталог", "writable": True, **kwargs},
    )


async def denied_reasons(db):
    rows = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.FILE_ACCESS_DENIED)
        )
    ).scalars().all()
    return [row.details["reason"] for row in rows]


# ------------------------------------------------- дозволені каталоги -----


async def test_root_can_be_added_listed_and_removed(client, admin_headers, device, db):
    created = await add_root(client, admin_headers, device.id)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["path"] == ROOT
    assert body["writable"] is True
    assert body["allow_privileged"] is False

    listing = await client.get(
        f"/api/v1/devices/{device.id}/files/roots", headers=admin_headers
    )
    assert [item["path"] for item in listing.json()] == [ROOT]

    removed = await client.delete(
        f"/api/v1/devices/{device.id}/files/roots/{body['id']}", headers=admin_headers
    )
    assert removed.status_code == 200
    total = (await db.execute(sa.select(sa.func.count()).select_from(FileRoot))).scalar_one()
    assert total == 0

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.FILE_ROOT_CHANGED)
        )
    ).scalars().all()
    assert [row.details["operation"] for row in audit] == ["added", "removed"]


async def test_root_path_is_normalized_and_validated(client, admin_headers, device):
    ok = await add_root(client, admin_headers, device.id, path="/sdcard//Fleet///")
    assert ok.status_code == 201
    assert ok.json()["path"] == ROOT

    traversal = await add_root(client, admin_headers, device.id, path="/sdcard/../etc")
    assert traversal.status_code == 422

    filesystem_root = await add_root(client, admin_headers, device.id, path="/")
    assert filesystem_root.status_code == 422


async def test_duplicate_root_rejected(client, admin_headers, device):
    assert (await add_root(client, admin_headers, device.id)).status_code == 201
    assert (await add_root(client, admin_headers, device.id)).status_code == 409


# ------------------------------------------------------- межі доступу -----


async def test_without_roots_module_is_disabled(client, admin_headers, device, db):
    response = await client.post(
        f"/api/v1/devices/{device.id}/files/list",
        headers=admin_headers,
        json={"path": "/sdcard"},
    )
    assert response.status_code == 403
    assert "не налаштовано" in response.json()["error"]["message"]
    assert await denied_reasons(db) == ["no_roots_configured"]


async def test_path_outside_allowlist_is_denied(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id)

    response = await client.post(
        f"/api/v1/devices/{device.id}/files/list",
        headers=admin_headers,
        json={"path": "/data/data/com.bank.app"},
    )
    assert response.status_code == 403
    assert await denied_reasons(db) == ["outside_allowlist"]


async def test_prefix_lookalike_directory_is_denied(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id)

    response = await client.post(
        f"/api/v1/devices/{device.id}/files/list",
        headers=admin_headers,
        json={"path": "/sdcard/FleetPrivate/notes.txt"},
    )
    assert response.status_code == 403
    assert await denied_reasons(db) == ["outside_allowlist"]


async def test_traversal_attempt_is_denied_and_audited(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id)

    response = await client.post(
        f"/api/v1/devices/{device.id}/files/list",
        headers=admin_headers,
        json={"path": "/sdcard/Fleet/../../data/misc"},
    )
    assert response.status_code == 403

    rows = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.FILE_ACCESS_DENIED)
        )
    ).scalars().all()
    assert "заборонені" in rows[0].details["reason"]
    assert rows[0].result == "denied"
    assert rows[0].details["path"] == "/sdcard/Fleet/../../data/misc"


async def test_read_only_root_blocks_writes(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id, writable=False)

    listing = await client.post(
        f"/api/v1/devices/{device.id}/files/list",
        headers=admin_headers,
        json={"path": ROOT},
    )
    assert listing.status_code == 202

    mkdir = await client.post(
        f"/api/v1/devices/{device.id}/files/mkdir",
        headers=admin_headers,
        json={"path": ROOT, "name": "logs"},
    )
    assert mkdir.status_code == 403
    assert "read_only_root" in await denied_reasons(db)


async def test_cannot_delete_the_root_itself(client, admin_headers, device):
    await add_root(client, admin_headers, device.id)
    response = await client.post(
        f"/api/v1/devices/{device.id}/files/delete",
        headers=admin_headers,
        json={"path": ROOT},
    )
    assert response.status_code == 409


# ---------------------------------------------------------- операції ------


async def test_list_creates_command_and_audit(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id)
    response = await client.post(
        f"/api/v1/devices/{device.id}/files/list",
        headers=admin_headers,
        json={"path": f"{ROOT}/logs"},
    )
    assert response.status_code == 202
    command = response.json()
    assert command["type"] == "FILE_LIST"
    assert command["payload"]["path"] == f"{ROOT}/logs"
    assert command["payload"]["root"] == ROOT

    pending = await client.get("/api/v1/device/commands/pending", headers=device.headers)
    assert [c["type"] for c in pending.json()] == ["FILE_LIST"]

    audit = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == AuditAction.FILE_LIST))
    ).scalars().all()
    assert audit[0].details["path"] == f"{ROOT}/logs"


async def test_mkdir_rename_delete_build_safe_paths(client, admin_headers, device):
    await add_root(client, admin_headers, device.id)

    mkdir = await client.post(
        f"/api/v1/devices/{device.id}/files/mkdir",
        headers=admin_headers,
        json={"path": ROOT, "name": "logs"},
    )
    assert mkdir.json()["payload"]["path"] == f"{ROOT}/logs"

    rename = await client.post(
        f"/api/v1/devices/{device.id}/files/rename",
        headers=admin_headers,
        json={"path": f"{ROOT}/old.txt", "name": "new.txt"},
    )
    assert rename.json()["payload"] == {
        "path": f"{ROOT}/old.txt",
        "target": f"{ROOT}/new.txt",
        "root": ROOT,
        "allow_privileged": False,
    }

    delete = await client.post(
        f"/api/v1/devices/{device.id}/files/delete",
        headers=admin_headers,
        json={"path": f"{ROOT}/new.txt", "recursive": True},
    )
    assert delete.json()["payload"]["recursive"] is True


async def test_rename_with_escaping_name_rejected(client, admin_headers, device):
    await add_root(client, admin_headers, device.id)
    response = await client.post(
        f"/api/v1/devices/{device.id}/files/rename",
        headers=admin_headers,
        json={"path": f"{ROOT}/a.txt", "name": "../../evil.txt"},
    )
    assert response.status_code == 422


# ------------------------------------- вивантаження з пристрою (download) --


async def start_download(client, admin_headers, device, path=f"{ROOT}/report.bin"):
    response = await client.post(
        f"/api/v1/devices/{device.id}/files/download",
        headers=admin_headers,
        json={"path": path},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def test_download_transfer_full_cycle(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id)
    transfer = await start_download(client, admin_headers, device)

    assert transfer["direction"] == "download"
    assert transfer["status"] == "pending"
    assert transfer["received_bytes"] == 0

    status = await client.get(
        f"/api/v1/device/files/transfers/{transfer['id']}", headers=device.headers
    )
    assert status.status_code == 200
    assert status.json()["received_bytes"] == 0
    assert status.json()["chunk_size"] > 0

    payload = b"fleet-report-payload" * 100
    first, second = payload[:500], payload[500:]

    r1 = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/chunk",
        headers={**device.headers, "X-Chunk-Offset": "0"},
        content=first,
    )
    assert r1.status_code == 200
    assert r1.json()["received_bytes"] == 500

    r2 = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/chunk",
        headers={**device.headers, "X-Chunk-Offset": "500"},
        content=second,
    )
    assert r2.json()["received_bytes"] == len(payload)

    digest = hashlib.sha256(payload).hexdigest()
    done = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/complete",
        headers=device.headers,
        json={"sha256": digest, "size_bytes": len(payload)},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "completed"

    stored = (await db.execute(sa.select(FileTransfer))).scalars().one()
    assert stored.checksum_verified is True
    assert stored.size_bytes == len(payload)

    content = await client.get(
        f"/api/v1/devices/{device.id}/files/transfers/{transfer['id']}/content",
        headers=admin_headers,
    )
    assert content.status_code == 200
    assert content.content == payload
    assert content.headers["X-Checksum-SHA256"] == digest


async def test_resume_after_interruption(client, admin_headers, device):
    await add_root(client, admin_headers, device.id)
    transfer = await start_download(client, admin_headers, device)

    await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/chunk",
        headers={**device.headers, "X-Chunk-Offset": "0"},
        content=b"A" * 300,
    )

    # Агент «упав» і не знає, скільки дійшло — питає сервер.
    status = await client.get(
        f"/api/v1/device/files/transfers/{transfer['id']}", headers=device.headers
    )
    assert status.json()["received_bytes"] == 300

    # Повтор того самого зсуву відхиляється, щоб не дублювати дані.
    duplicate = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/chunk",
        headers={**device.headers, "X-Chunk-Offset": "0"},
        content=b"A" * 300,
    )
    assert duplicate.status_code == 409
    assert "300" in duplicate.json()["error"]["message"]

    resumed = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/chunk",
        headers={**device.headers, "X-Chunk-Offset": "300"},
        content=b"B" * 200,
    )
    assert resumed.json()["received_bytes"] == 500


async def test_checksum_mismatch_fails_transfer(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id)
    transfer = await start_download(client, admin_headers, device)

    await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/chunk",
        headers={**device.headers, "X-Chunk-Offset": "0"},
        content=b"payload",
    )
    done = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/complete",
        headers=device.headers,
        json={"sha256": "0" * 64},
    )
    assert done.json()["status"] == "failed"

    stored = (await db.execute(sa.select(FileTransfer))).scalars().one()
    assert stored.error == "Контрольна сума не збігається"
    assert stored.checksum_verified is False

    audit = (
        await db.execute(
            sa.select(AuditLog).where(
                AuditLog.action == AuditAction.FILE_TRANSFER_COMPLETED
            )
        )
    ).scalars().all()
    assert audit[0].result == "failure"


async def test_size_limit_enforced_on_chunks(client, admin_headers, device):
    await add_root(client, admin_headers, device.id, max_file_size_mb=1)
    transfer = await start_download(client, admin_headers, device)

    oversized = b"x" * (1024 * 1024 + 10)
    response = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/chunk",
        headers={**device.headers, "X-Chunk-Offset": "0"},
        content=oversized,
    )
    assert response.status_code == 422
    assert "розмір" in response.json()["error"]["message"]


async def test_admin_cannot_fetch_unfinished_transfer(client, admin_headers, device):
    await add_root(client, admin_headers, device.id)
    transfer = await start_download(client, admin_headers, device)

    response = await client.get(
        f"/api/v1/devices/{device.id}/files/transfers/{transfer['id']}/content",
        headers=admin_headers,
    )
    assert response.status_code == 409


# ---------------------------------------- запис на пристрій (upload) ------


async def start_upload(client, admin_headers, device, content=b"hello fleet", name="note.txt"):
    return await client.post(
        f"/api/v1/devices/{device.id}/files/upload",
        headers=admin_headers,
        files={"file": (name, content, "application/octet-stream")},
        data={"target_dir": ROOT},
    )


async def test_upload_to_device_and_range_resume(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id)
    payload = bytes(range(256)) * 8

    created = await start_upload(client, admin_headers, device, content=payload)
    assert created.status_code == 202, created.text
    transfer = created.json()
    assert transfer["direction"] == "upload"
    assert transfer["remote_path"] == f"{ROOT}/note.txt"
    assert transfer["size_bytes"] == len(payload)
    assert transfer["sha256"] == hashlib.sha256(payload).hexdigest()

    pending = await client.get("/api/v1/device/commands/pending", headers=device.headers)
    command = pending.json()[0]
    assert command["type"] == "FILE_UPLOAD"
    assert command["payload"]["path"] == f"{ROOT}/note.txt"

    # Агент качає перші 100 байт, потім «розрив звʼязку».
    head = await client.get(
        f"/api/v1/device/files/transfers/{transfer['id']}/content",
        headers={**device.headers, "Range": "bytes=0-99"},
    )
    assert head.status_code == 206
    assert head.content == payload[:100]
    assert head.headers["Content-Range"] == f"bytes 0-99/{len(payload)}"

    await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/progress",
        headers=device.headers,
        json={"received_bytes": 100},
    )

    # Відновлення з того місця, де зупинилися.
    rest = await client.get(
        f"/api/v1/device/files/transfers/{transfer['id']}/content",
        headers={**device.headers, "Range": "bytes=100-"},
    )
    assert rest.status_code == 206
    assert rest.content == payload[100:]

    done = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/complete",
        headers=device.headers,
        json={"sha256": hashlib.sha256(payload).hexdigest()},
    )
    assert done.json()["status"] == "completed"

    stored = (await db.execute(sa.select(FileTransfer))).scalars().one()
    assert stored.status == "completed"
    assert stored.received_bytes == len(payload)


async def test_upload_without_range_returns_whole_file(client, admin_headers, device):
    await add_root(client, admin_headers, device.id)
    created = await start_upload(client, admin_headers, device, content=b"abcdef")
    transfer = created.json()

    response = await client.get(
        f"/api/v1/device/files/transfers/{transfer['id']}/content", headers=device.headers
    )
    assert response.status_code == 200
    assert response.content == b"abcdef"
    assert response.headers["Accept-Ranges"] == "bytes"


async def test_upload_rejects_bad_range(client, admin_headers, device):
    await add_root(client, admin_headers, device.id)
    transfer = (await start_upload(client, admin_headers, device, content=b"abc")).json()

    response = await client.get(
        f"/api/v1/device/files/transfers/{transfer['id']}/content",
        headers={**device.headers, "Range": "bytes=99-"},
    )
    assert response.status_code == 416


async def test_upload_outside_allowlist_denied(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id)
    response = await client.post(
        f"/api/v1/devices/{device.id}/files/upload",
        headers=admin_headers,
        files={"file": ("note.txt", b"data", "application/octet-stream")},
        data={"target_dir": "/data/data/com.other.app"},
    )
    assert response.status_code == 403
    assert "outside_allowlist" in await denied_reasons(db)


async def test_upload_exceeding_root_limit_rejected(client, admin_headers, device):
    await add_root(client, admin_headers, device.id, max_file_size_mb=1)
    response = await client.post(
        f"/api/v1/devices/{device.id}/files/upload",
        headers=admin_headers,
        files={"file": ("big.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream")},
        data={"target_dir": ROOT},
    )
    assert response.status_code == 422


async def test_agent_failure_report_marks_transfer_failed(client, admin_headers, device, db):
    await add_root(client, admin_headers, device.id)
    transfer = await start_download(client, admin_headers, device)

    response = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/fail",
        headers=device.headers,
        json={"error": "Немає доступу до файла на пристрої"},
    )
    assert response.status_code == 200

    stored = (await db.execute(sa.select(FileTransfer))).scalars().one()
    assert stored.status == "failed"
    assert "Немає доступу" in stored.error


# ------------------------------------------------------------ доступ ------


async def test_transfers_are_isolated_between_devices(
    client, admin_headers, device, enroll_device
):
    await add_root(client, admin_headers, device.id)
    transfer = await start_download(client, admin_headers, device)
    other = await enroll_device(android_id="ffffffffffffffff")

    status = await client.get(
        f"/api/v1/device/files/transfers/{transfer['id']}", headers=other.headers
    )
    assert status.status_code == 404

    chunk = await client.post(
        f"/api/v1/device/files/transfers/{transfer['id']}/chunk",
        headers={**other.headers, "X-Chunk-Offset": "0"},
        content=b"stolen",
    )
    assert chunk.status_code == 404


async def test_operator_can_read_but_not_write(client, auth_headers, admin_headers, device):
    await add_root(client, admin_headers, device.id)
    operator = await auth_headers(email="operator@example.com", role=Role.OPERATOR)

    listing = await client.post(
        f"/api/v1/devices/{device.id}/files/list", headers=operator, json={"path": ROOT}
    )
    assert listing.status_code == 202

    for action, payload in (
        ("mkdir", {"path": ROOT, "name": "x"}),
        ("delete", {"path": f"{ROOT}/x"}),
    ):
        response = await client.post(
            f"/api/v1/devices/{device.id}/files/{action}", headers=operator, json=payload
        )
        assert response.status_code == 403


async def test_auditor_has_no_file_access(client, auth_headers, admin_headers, device):
    await add_root(client, admin_headers, device.id)
    auditor = await auth_headers(email="auditor@example.com", role=Role.AUDITOR)

    response = await client.post(
        f"/api/v1/devices/{device.id}/files/list", headers=auditor, json={"path": ROOT}
    )
    assert response.status_code == 403


async def test_anonymous_denied_everywhere(client, device):
    assert (await client.get("/api/v1/device/files/roots")).status_code == 401
    blocked = await client.post(
        f"/api/v1/devices/{device.id}/files/list", json={"path": ROOT}
    )
    assert blocked.status_code == 401


async def test_agent_sees_only_its_own_roots(client, admin_headers, device, enroll_device):
    await add_root(client, admin_headers, device.id)
    other = await enroll_device(android_id="ffffffffffffffff")

    mine = await client.get("/api/v1/device/files/roots", headers=device.headers)
    assert [root["path"] for root in mine.json()] == [ROOT]

    theirs = await client.get("/api/v1/device/files/roots", headers=other.headers)
    assert theirs.json() == []
