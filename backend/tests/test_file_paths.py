import pytest

from app.services import file_paths
from app.services.file_paths import PathViolation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/sdcard/Download", "/sdcard/Download"),
        ("/sdcard//Download///", "/sdcard/Download"),
        ("/sdcard/./Download", "/sdcard/Download"),
        ("  /data/local/tmp  ", "/data/local/tmp"),
        ("/", "/"),
    ],
)
def test_normalize_accepts_valid_paths(raw, expected):
    assert file_paths.normalize_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "/sdcard/../../etc/passwd",
        "/sdcard/Download/../../../data",
        "..",
        "/..",
        "sdcard/Download",          # не абсолютний
        "",
        "   ",
        "/sdcard/\x00evil",
        "C:\Windows\System32",     # зворотні слеші
        "/sdcard/" + "x" * 300,      # задовгий елемент
        "/" + "a" * 1100,            # задовгий шлях
    ],
)
def test_normalize_rejects_dangerous_paths(raw):
    with pytest.raises(PathViolation):
        file_paths.normalize_path(raw)


def test_normalize_rejects_none():
    with pytest.raises(PathViolation):
        file_paths.normalize_path(None)


@pytest.mark.parametrize("name", ["report.txt", "звіт 2026.pdf", "a" * 255])
def test_validate_name_accepts(name):
    assert file_paths.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "a\b", "a\x00b", "a" * 256, "line\nbreak"],
)
def test_validate_name_rejects(name):
    with pytest.raises(PathViolation):
        file_paths.validate_name(name)


def test_is_within_checks_boundaries():
    assert file_paths.is_within("/sdcard/Fleet", "/sdcard/Fleet")
    assert file_paths.is_within("/sdcard/Fleet", "/sdcard/Fleet/logs/a.txt")
    # Класична пастка: спільний префікс, але інший каталог.
    assert not file_paths.is_within("/sdcard/Fleet", "/sdcard/FleetSecret/a.txt")
    assert not file_paths.is_within("/sdcard/Fleet", "/sdcard")
    assert not file_paths.is_within("/sdcard/Fleet", "/data/local/tmp")


def test_join_and_helpers():
    assert file_paths.join("/sdcard/Fleet", "report.txt") == "/sdcard/Fleet/report.txt"
    assert file_paths.parent_of("/sdcard/Fleet/report.txt") == "/sdcard/Fleet"
    assert file_paths.parent_of("/sdcard") == "/"
    assert file_paths.basename("/sdcard/Fleet/report.txt") == "report.txt"

    with pytest.raises(PathViolation):
        file_paths.join("/sdcard/Fleet", "../escape")
