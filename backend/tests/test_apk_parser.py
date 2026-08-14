import hashlib
import re
from pathlib import Path

import pytest

from app.services.apk_parser import ApkParseError, parse_apk
from tests.axml import build_apk

REAL_APK = Path(__file__).resolve().parents[2] / "android/app/build/outputs/apk/debug/app-debug.apk"
GRADLE_BUILD = Path(__file__).resolve().parents[2] / "android/app/build.gradle.kts"


def _expected_version() -> tuple[int, str]:
    """Очікувані версії беремо з build.gradle.kts, а не зашиваємо в тест.

    Інакше кожне підняття версії агента «ламало» б перевірку парсера,
    хоча парсер працює правильно.
    """
    source = GRADLE_BUILD.read_text(encoding="utf-8")
    code = int(re.search(r"versionCode\s*=\s*(\d+)", source).group(1))
    name = re.search(r'versionName\s*=\s*"([^"]+)"', source).group(1)
    return code, name


def test_extracts_core_metadata(tmp_path):
    apk = build_apk(
        tmp_path / "demo.apk",
        package="ua.example.pos",
        version_code=1207,
        version_name="3.4.1",
        min_sdk=24,
        target_sdk=35,
        permissions=("android.permission.INTERNET", "android.permission.CAMERA"),
        abis=("arm64-v8a", "armeabi-v7a"),
    )
    meta = parse_apk(apk)

    assert meta.package_name == "ua.example.pos"
    assert meta.version_code == 1207
    assert meta.version_name == "3.4.1"
    assert meta.min_sdk == 24
    assert meta.target_sdk == 35
    assert meta.permissions == [
        "android.permission.INTERNET",
        "android.permission.CAMERA",
    ]


def test_detects_architecture(tmp_path):
    multi = parse_apk(build_apk(tmp_path / "multi.apk", abis=("arm64-v8a", "x86_64")))
    assert multi.abis == ["arm64-v8a", "x86_64"]
    assert multi.architecture == "arm64-v8a, x86_64"

    universal = parse_apk(build_apk(tmp_path / "universal.apk", abis=()))
    assert universal.abis == []
    assert universal.architecture == "universal"


def test_ignores_unknown_lib_directories(tmp_path):
    apk = build_apk(tmp_path / "weird.apk", abis=("arm64-v8a", "totally-not-an-abi"))
    assert parse_apk(apk).abis == ["arm64-v8a"]


def test_checksum_and_size_match_file(tmp_path):
    apk = build_apk(tmp_path / "sized.apk", padding_bytes=4096)
    meta = parse_apk(apk)

    expected = hashlib.sha256(apk.read_bytes()).hexdigest()
    assert meta.sha256 == expected
    assert meta.size_bytes == apk.stat().st_size


def test_detects_v1_signature(tmp_path):
    signed = parse_apk(build_apk(tmp_path / "signed.apk", with_v1_signature=True))
    assert "v1" in signed.signature_schemes
    assert signed.signature_digest is not None

    unsigned = parse_apk(build_apk(tmp_path / "unsigned.apk", with_v1_signature=False))
    assert "v1" not in unsigned.signature_schemes
    assert unsigned.signature_digest is None


def test_detects_split_apk(tmp_path):
    meta = parse_apk(build_apk(tmp_path / "split.apk", split="config.arm64_v8a"))
    assert meta.is_split is True
    assert meta.split_name == "config.arm64_v8a"


def test_rejects_non_zip(tmp_path):
    broken = tmp_path / "broken.apk"
    broken.write_bytes(b"this is definitely not an apk")
    with pytest.raises(ApkParseError):
        parse_apk(broken)


def test_rejects_zip_without_manifest(tmp_path):
    import zipfile

    path = tmp_path / "empty.apk"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "нічого корисного")
    with pytest.raises(ApkParseError):
        parse_apk(path)


def test_rejects_missing_file(tmp_path):
    with pytest.raises(ApkParseError):
        parse_apk(tmp_path / "does-not-exist.apk")


@pytest.mark.skipif(not REAL_APK.exists(), reason="APK агента ще не зібрано")
def test_parses_real_apk_if_available():
    """Контроль на справжньому APK, зібраному Gradle."""
    expected_code, expected_name = _expected_version()
    meta = parse_apk(REAL_APK)
    assert meta.package_name == "ua.fleet.agent"
    assert meta.version_code == expected_code
    assert meta.version_name == expected_name
    assert meta.min_sdk == 26
    assert "android.permission.INTERNET" in meta.permissions
    assert meta.abis  # агент містить нативні бібліотеки Compose
    assert len(meta.sha256) == 64
