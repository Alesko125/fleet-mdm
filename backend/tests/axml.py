"""Генератор бінарного AndroidManifest.xml (AXML) для тестів.

Дозволяє зібрати синтетичний, але формат-коректний APK без Android SDK.
Парсер `app.services.apk_parser` перевіряється й на реальному APK — див.
`test_apk_parser.py::test_parses_real_apk_if_available`.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

CHUNK_STRING_POOL = 0x0001
CHUNK_XML = 0x0003
CHUNK_START_ELEMENT = 0x0102
CHUNK_END_ELEMENT = 0x0103
CHUNK_RESOURCE_MAP = 0x0180

TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10

NO_ENTRY = 0xFFFFFFFF
ANDROID_NS = "http://schemas.android.com/apk/res/android"


class _Pool:
    def __init__(self) -> None:
        self._strings: list[str] = []

    def add(self, value: str) -> int:
        if value not in self._strings:
            self._strings.append(value)
        return self._strings.index(value)

    def build(self) -> bytes:
        offsets = bytearray()
        data = bytearray()
        for value in self._strings:
            offsets += struct.pack("<I", len(data))
            encoded = value.encode("utf-16-le")
            data += struct.pack("<H", len(value)) + encoded + b"\x00\x00"
        while len(data) % 4:
            data += b"\x00"

        header_size = 28
        strings_start = header_size + len(offsets)
        size = strings_start + len(data)
        header = struct.pack(
            "<HHIIIIII",
            CHUNK_STRING_POOL,
            header_size,
            size,
            len(self._strings),
            0,  # styleCount
            0,  # flags: UTF-16
            strings_start,
            0,  # stylesStart
        )
        return header + bytes(offsets) + bytes(data)


def _start_element(
    pool: _Pool, name: str, attributes: list[tuple[str, object]], ns_index: int
) -> bytes:
    name_idx = pool.add(name)
    body = struct.pack("<IIII", 1, NO_ENTRY, NO_ENTRY, name_idx)
    body += struct.pack("<HHHHHH", 20, 20, len(attributes), 0, 0, 0)

    for attr_name, value in attributes:
        attr_idx = pool.add(attr_name)
        if isinstance(value, int) and not isinstance(value, bool):
            body += struct.pack("<III", ns_index, attr_idx, NO_ENTRY)
            body += struct.pack("<HBBI", 8, 0, TYPE_INT_DEC, value)
        else:
            value_idx = pool.add(str(value))
            body += struct.pack("<III", ns_index, attr_idx, value_idx)
            body += struct.pack("<HBBI", 8, 0, TYPE_STRING, value_idx)

    # Розмір чанка = заголовок (8) + тіло; headerSize = 16 (заголовок + line/comment).
    return struct.pack("<HHI", CHUNK_START_ELEMENT, 16, 8 + len(body)) + body


def _end_element(pool: _Pool, name: str) -> bytes:
    body = struct.pack("<IIII", 1, NO_ENTRY, NO_ENTRY, pool.add(name))
    return struct.pack("<HHI", CHUNK_END_ELEMENT, 16, 8 + len(body)) + body


def build_manifest(
    *,
    package: str = "com.example.demo",
    version_code: int = 42,
    version_name: str = "1.4.2",
    min_sdk: int = 26,
    target_sdk: int = 34,
    permissions: tuple[str, ...] = ("android.permission.INTERNET",),
    split: str | None = None,
) -> bytes:
    """Збирає бінарний AndroidManifest.xml із заданими атрибутами."""
    pool = _Pool()
    ns_index = pool.add(ANDROID_NS)

    manifest_attrs: list[tuple[str, object]] = [
        ("package", package),
        ("versionCode", version_code),
        ("versionName", version_name),
    ]
    if split is not None:
        manifest_attrs.append(("split", split))

    chunks = [
        _start_element(pool, "manifest", manifest_attrs, ns_index),
        _start_element(
            pool,
            "uses-sdk",
            [("minSdkVersion", min_sdk), ("targetSdkVersion", target_sdk)],
            ns_index,
        ),
        _end_element(pool, "uses-sdk"),
    ]
    for permission in permissions:
        chunks.append(_start_element(pool, "uses-permission", [("name", permission)], ns_index))
        chunks.append(_end_element(pool, "uses-permission"))
    chunks.append(_end_element(pool, "manifest"))

    # Пул рядків будується останнім — до нього вже додано всі рядки.
    pool_bytes = pool.build()
    body = pool_bytes + b"".join(chunks)
    return struct.pack("<HHI", CHUNK_XML, 8, 8 + len(body)) + body


def build_apk(
    path: str | Path,
    *,
    package: str = "com.example.demo",
    version_code: int = 42,
    version_name: str = "1.4.2",
    min_sdk: int = 26,
    target_sdk: int = 34,
    permissions: tuple[str, ...] = ("android.permission.INTERNET",),
    abis: tuple[str, ...] = ("arm64-v8a",),
    split: str | None = None,
    with_v1_signature: bool = True,
    padding_bytes: int = 0,
) -> Path:
    """Створює синтетичний APK-файл на диску."""
    apk_path = Path(path)
    apk_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(
        package=package,
        version_code=version_code,
        version_name=version_name,
        min_sdk=min_sdk,
        target_sdk=target_sdk,
        permissions=permissions,
        split=split,
    )

    with zipfile.ZipFile(apk_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("AndroidManifest.xml", manifest)
        archive.writestr("classes.dex", b"dex\n035\x00" + b"\x00" * 64)
        for abi in abis:
            archive.writestr(f"lib/{abi}/libdemo.so", b"\x7fELF" + b"\x00" * 32)
        if with_v1_signature:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
            archive.writestr("META-INF/CERT.RSA", b"\x30\x82fake-certificate")
        if padding_bytes:
            archive.writestr("assets/padding.bin", b"\x00" * padding_bytes)

    return apk_path
