"""Розбір APK без зовнішніх залежностей.

APK — це ZIP, у якому `AndroidManifest.xml` збережений у бінарному форматі
Android (AXML). Тут реалізовано мінімальний, але коректний парсер AXML:
пул рядків → карта ресурсів → елементи з типізованими атрибутами.

Витягуються: package, versionCode/versionName, minSdk/targetSdk, перелік
дозволів, ABI (з `lib/<abi>/`), схеми підпису, розмір і SHA-256.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# --- типи чанків AXML ------------------------------------------------------
CHUNK_STRING_POOL = 0x0001
CHUNK_XML = 0x0003
CHUNK_XML_START_ELEMENT = 0x0102
CHUNK_XML_RESOURCE_MAP = 0x0180

# --- типи значень атрибутів ------------------------------------------------
TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12

STRING_POOL_UTF8_FLAG = 1 << 8

# Ідентифікатори системних атрибутів — потрібні, коли імена атрибутів
# у пулі порожні (буває після обфускації інструментами збірки).
ATTR_IDS = {
    0x0101021B: "versionCode",
    0x0101021C: "versionName",
    0x0101020C: "minSdkVersion",
    0x01010270: "targetSdkVersion",
    0x01010572: "compileSdkVersion",
    0x01010003: "name",
    0x01010001: "label",
}

# Відомі ABI Android — усе інше в `lib/` ігноруємо.
KNOWN_ABIS = frozenset(
    {"armeabi", "armeabi-v7a", "arm64-v8a", "x86", "x86_64", "mips", "mips64", "riscv64"}
)

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
# Сертифікат підпису v1 — це кілька кілобайтів; усе, що більше, або
# пошкоджене, або спроба «zip-бомби» через розпаковку.
MAX_CERT_BYTES = 512 * 1024
# Стеля кількості записів у архіві: розбір імен на мільйонах записів
# сам по собі був би відмовою в обслуговуванні.
MAX_ARCHIVE_ENTRIES = 100_000
# Скільки разів вміст може «розпухнути» при розпакуванні.
MAX_COMPRESSION_RATIO = 200


class ApkParseError(ValueError):
    """Файл не є коректним APK або маніфест не вдалося розібрати."""


@dataclass(slots=True)
class ApkMetadata:
    package_name: str
    version_code: int | None
    version_name: str | None
    min_sdk: int | None = None
    target_sdk: int | None = None
    compile_sdk: int | None = None
    abis: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    is_split: bool = False
    split_name: str | None = None
    signature_schemes: list[str] = field(default_factory=list)
    signature_digest: str | None = None
    sha256: str = ""
    size_bytes: int = 0

    @property
    def architecture(self) -> str:
        """Людяний опис архітектури для UI."""
        if not self.abis:
            return "universal"
        return ", ".join(sorted(self.abis))


# ---------------------------------------------------------------- AXML ----


class _StringPool:
    def __init__(self, data: bytes, offset: int) -> None:
        (chunk_type, header_size, chunk_size) = struct.unpack_from("<HHI", data, offset)
        if chunk_type != CHUNK_STRING_POOL:
            raise ApkParseError("Очікувався пул рядків AXML")
        (count, style_count, flags, strings_start, _styles_start) = struct.unpack_from(
            "<IIIII", data, offset + 8
        )
        self._utf8 = bool(flags & STRING_POOL_UTF8_FLAG)
        self._strings: list[str] = []

        offsets_at = offset + header_size
        data_at = offset + strings_start
        if data_at > len(data) or chunk_size <= 0:
            raise ApkParseError("Пошкоджений пул рядків AXML")

        for i in range(count):
            (str_offset,) = struct.unpack_from("<I", data, offsets_at + 4 * i)
            try:
                self._strings.append(self._read(data, data_at + str_offset))
            except (struct.error, IndexError, UnicodeDecodeError):
                self._strings.append("")
        # Стилі не потрібні — пропускаємо.
        _ = style_count
        self.end = offset + chunk_size

    def _read(self, data: bytes, pos: int) -> str:
        if self._utf8:
            pos, _utf16_len = self._read_len8(data, pos)
            pos, byte_len = self._read_len8(data, pos)
            return data[pos : pos + byte_len].decode("utf-8", errors="replace")
        pos, char_len = self._read_len16(data, pos)
        return data[pos : pos + char_len * 2].decode("utf-16-le", errors="replace")

    @staticmethod
    def _read_len8(data: bytes, pos: int) -> tuple[int, int]:
        value = data[pos]
        pos += 1
        if value & 0x80:
            value = ((value & 0x7F) << 8) | data[pos]
            pos += 1
        return pos, value

    @staticmethod
    def _read_len16(data: bytes, pos: int) -> tuple[int, int]:
        (value,) = struct.unpack_from("<H", data, pos)
        pos += 2
        if value & 0x8000:
            (low,) = struct.unpack_from("<H", data, pos)
            pos += 2
            value = ((value & 0x7FFF) << 16) | low
        return pos, value

    def get(self, index: int) -> str:
        if 0 <= index < len(self._strings):
            return self._strings[index]
        return ""


@dataclass(slots=True)
class _Element:
    name: str
    attributes: dict[str, object]


def _parse_axml(data: bytes) -> list[_Element]:
    if len(data) < 8:
        raise ApkParseError("AndroidManifest.xml надто малий")
    (chunk_type, header_size, _size) = struct.unpack_from("<HHI", data, 0)
    if chunk_type != CHUNK_XML:
        raise ApkParseError("AndroidManifest.xml не у форматі AXML")

    pool: _StringPool | None = None
    resource_ids: list[int] = []
    elements: list[_Element] = []

    offset = header_size
    while offset + 8 <= len(data):
        (c_type, c_header, c_size) = struct.unpack_from("<HHI", data, offset)
        if c_size < 8 or offset + c_size > len(data):
            break

        if c_type == CHUNK_STRING_POOL:
            pool = _StringPool(data, offset)
        elif c_type == CHUNK_XML_RESOURCE_MAP:
            count = (c_size - c_header) // 4
            resource_ids = list(struct.unpack_from(f"<{count}I", data, offset + c_header))
        elif c_type == CHUNK_XML_START_ELEMENT and pool is not None:
            elements.append(_parse_element(data, offset, c_header, pool, resource_ids))

        offset += c_size

    if pool is None:
        raise ApkParseError("У AndroidManifest.xml немає пулу рядків")
    return elements


def _parse_element(
    data: bytes, offset: int, header_size: int, pool: _StringPool, resource_ids: list[int]
) -> _Element:
    body = offset + header_size
    (_ns, name_idx) = struct.unpack_from("<II", data, body)
    (attr_start, attr_size, attr_count) = struct.unpack_from("<HHH", data, body + 8)

    attributes: dict[str, object] = {}
    attrs_at = body + attr_start
    for i in range(attr_count):
        pos = attrs_at + i * attr_size
        if pos + 20 > len(data):
            break
        (_attr_ns, attr_name_idx, raw_value_idx) = struct.unpack_from("<III", data, pos)
        (_size, _res0, value_type, value_data) = struct.unpack_from("<HBBI", data, pos + 12)

        key = pool.get(attr_name_idx)
        if not key and attr_name_idx < len(resource_ids):
            key = ATTR_IDS.get(resource_ids[attr_name_idx], "")
        if not key:
            continue

        attributes[key] = _attribute_value(pool, raw_value_idx, value_type, value_data)

    return _Element(name=pool.get(name_idx), attributes=attributes)


def _attribute_value(
    pool: _StringPool, raw_value_idx: int, value_type: int, value_data: int
) -> object:
    if value_type == TYPE_STRING:
        return pool.get(value_data)
    if value_type in (TYPE_INT_DEC, TYPE_INT_HEX):
        return value_data
    if value_type == TYPE_INT_BOOLEAN:
        return bool(value_data)
    if value_type == TYPE_REFERENCE:
        # Значення живе в resources.arsc — не резолвимо, лишаємо посилання.
        return f"@{value_data:08x}"
    if raw_value_idx != 0xFFFFFFFF:
        return pool.get(raw_value_idx)
    return value_data


# ------------------------------------------------------------------ APK ---


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def _as_str(value: object) -> str | None:
    if isinstance(value, str) and value and not value.startswith("@"):
        return value
    if isinstance(value, int):
        return str(value)
    return None


def parse_manifest(manifest_bytes: bytes) -> ApkMetadata:
    """Розбирає бінарний AndroidManifest.xml."""
    elements = _parse_axml(manifest_bytes)

    manifest = next((e for e in elements if e.name == "manifest"), None)
    if manifest is None:
        raise ApkParseError("У маніфесті немає елемента <manifest>")

    package = _as_str(manifest.attributes.get("package"))
    if not package:
        raise ApkParseError("У маніфесті не вказано package")

    meta = ApkMetadata(
        package_name=package,
        version_code=_as_int(manifest.attributes.get("versionCode")),
        version_name=_as_str(manifest.attributes.get("versionName")),
        compile_sdk=_as_int(manifest.attributes.get("compileSdkVersion")),
        split_name=_as_str(manifest.attributes.get("split")),
    )
    meta.is_split = meta.split_name is not None

    uses_sdk = next((e for e in elements if e.name == "uses-sdk"), None)
    if uses_sdk is not None:
        meta.min_sdk = _as_int(uses_sdk.attributes.get("minSdkVersion"))
        meta.target_sdk = _as_int(uses_sdk.attributes.get("targetSdkVersion"))

    permissions = [
        name
        for e in elements
        if e.name == "uses-permission"
        for name in [_as_str(e.attributes.get("name"))]
        if name
    ]
    # Зберігаємо порядок, але без дублів.
    meta.permissions = list(dict.fromkeys(permissions))
    return meta


def sha256_of_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def parse_apk(path: str | Path) -> ApkMetadata:
    """Повний розбір APK-файлу: метадані, ABI, підпис, хеш, розмір."""
    apk_path = Path(path)
    if not apk_path.is_file():
        raise ApkParseError("Файл APK не знайдено")

    try:
        with zipfile.ZipFile(apk_path) as archive:
            _reject_zip_bomb(archive)
            names = archive.namelist()
            if "AndroidManifest.xml" not in names:
                raise ApkParseError("У архіві немає AndroidManifest.xml — це не APK")

            info = archive.getinfo("AndroidManifest.xml")
            if info.file_size > MAX_MANIFEST_BYTES:
                raise ApkParseError("AndroidManifest.xml неправдоподібно великий")

            meta = parse_manifest(archive.read("AndroidManifest.xml"))
            meta.abis = _detect_abis(names)
            meta.signature_schemes, meta.signature_digest = _detect_signature(archive, names)
    except zipfile.BadZipFile as exc:
        raise ApkParseError("Файл не є коректним ZIP/APK") from exc

    meta.sha256 = sha256_of_file(apk_path)
    meta.size_bytes = apk_path.stat().st_size
    return meta


def _reject_zip_bomb(archive: zipfile.ZipFile) -> None:
    """Відсікає архіви, розпакування яких коштувало б надто дорого.

    Ми читаємо з APK лише маніфест і сертифікат, але навіть побудова
    переліку записів на спеціально сформованому архіві може з'їсти всю
    памʼять — тому обмеження стоять до першого читання.
    """
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ApkParseError("У архіві неправдоподібно багато записів")

    compressed = sum(info.compress_size for info in infos)
    uncompressed = sum(info.file_size for info in infos)
    if compressed > 0 and uncompressed / compressed > MAX_COMPRESSION_RATIO:
        raise ApkParseError("Підозріле співвідношення стиснення — файл відхилено")

    for info in infos:
        # Записи з абсолютним шляхом чи `..` у легальному APK не трапляються.
        name = info.filename
        if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
            raise ApkParseError(f"Некоректне імʼя запису в архіві: {name[:80]}")


def _detect_abis(names: list[str]) -> list[str]:
    abis: set[str] = set()
    for name in names:
        parts = name.split("/")
        if len(parts) >= 3 and parts[0] == "lib" and parts[1] in KNOWN_ABIS:
            abis.add(parts[1])
    return sorted(abis)


def _detect_signature(
    archive: zipfile.ZipFile, names: list[str]
) -> tuple[list[str], str | None]:
    """Схеми підпису та відбиток сертифіката v1 (якщо є)."""
    schemes: list[str] = []
    digest: str | None = None

    cert_files = sorted(
        name
        for name in names
        if name.upper().startswith("META-INF/")
        and name.upper().endswith((".RSA", ".DSA", ".EC"))
    )
    if cert_files:
        schemes.append("v1")
        info = archive.getinfo(cert_files[0])
        if info.file_size > MAX_CERT_BYTES:
            raise ApkParseError("Файл сертифіката неправдоподібно великий")
        digest = hashlib.sha256(archive.read(cert_files[0])).hexdigest()

    # Блок підпису v2/v3 лежить перед центральним каталогом ZIP.
    try:
        with open(archive.filename, "rb") as handle:  # type: ignore[arg-type]
            handle.seek(0, 2)
            size = handle.tell()
            window = min(size, 1024 * 1024)
            handle.seek(size - window)
            if b"APK Sig Block 42" in handle.read(window):
                schemes.append("v2+")
    except OSError:  # pragma: no cover — файл уже відкритий вище
        pass

    return schemes, digest
