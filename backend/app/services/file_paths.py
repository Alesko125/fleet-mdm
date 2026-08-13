"""Безпечна робота зі шляхами на пристрої.

Тут єдине джерело правди про те, куди файловому модулю **дозволено**
заглядати. Правила навмисно суворі й прямолінійні — краще відхилити
екзотичний, але легальний шлях, ніж пропустити вихід за межі allowlist.
"""

from __future__ import annotations

import posixpath
import re

MAX_PATH_LENGTH = 1024
MAX_NAME_LENGTH = 255

# Імена, які не можуть бути елементом шляху.
FORBIDDEN_NAMES = {"", ".", ".."}
# Керуючі символи та роздільники в імені файла.
INVALID_NAME_CHARS = re.compile(r"[/\x00-\x1f\x7f]")

# Метасимволи оболонки. Агент виконує привілейовані операції в каталогах із
# `allow_privileged` через `su`, і хоча він самостійно екранує аргументи,
# такі символи в легальному шляху керованого пристрою не трапляються.
# Відсікаємо їх на вході — щоб небезпечний рядок не дійшов до пристрою
# взагалі, а спроба потрапила в аудит.
SHELL_METACHARACTERS = frozenset("\"'`$;&|<>()[]{}*?!#~\n\r\t\\")

# Юнікодні символи, що керують напрямком тексту: ними можна замаскувати
# справжній вигляд шляху в інтерфейсі панелі.
BIDI_CONTROLS = frozenset(
    "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


class PathViolation(ValueError):
    """Шлях не пройшов перевірку — використовується для 400/403 у API."""


def normalize_path(raw: str) -> str:
    """Приводить абсолютний POSIX-шлях до канонічного вигляду.

    `..` не «розгортається», а **відхиляється**: у легальних сценаріях
    керування файлами його не буває, а будь-яка спроба — привід для запису
    в аудит.
    """
    if raw is None:
        raise PathViolation("Шлях не вказано")

    path = raw.strip()
    if not path:
        raise PathViolation("Порожній шлях")
    if "\x00" in path:
        raise PathViolation("Шлях містить нульовий байт")
    if "\\" in path:
        raise PathViolation("Зворотні слеші заборонені: очікується POSIX-шлях")
    if not path.startswith("/"):
        raise PathViolation("Очікується абсолютний шлях, що починається з /")
    if len(path) > MAX_PATH_LENGTH:
        raise PathViolation(f"Шлях довший за {MAX_PATH_LENGTH} символів")

    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise PathViolation("Переходи `..` заборонені")
        if len(segment) > MAX_NAME_LENGTH:
            raise PathViolation("Елемент шляху задовгий")
        if INVALID_NAME_CHARS.search(segment):
            raise PathViolation("Елемент шляху містить неприпустимі символи")
        _reject_dangerous(segment)
        segments.append(segment)

    return "/" + "/".join(segments) if segments else "/"


def validate_name(raw: str) -> str:
    """Перевіряє імʼя файла або каталогу (для rename / mkdir)."""
    name = (raw or "").strip()
    if name in FORBIDDEN_NAMES:
        raise PathViolation("Некоректне імʼя")
    if len(name) > MAX_NAME_LENGTH:
        raise PathViolation(f"Імʼя довше за {MAX_NAME_LENGTH} символів")
    if INVALID_NAME_CHARS.search(name):
        raise PathViolation("Імʼя містить роздільник шляху або керуючі символи")
    _reject_dangerous(name)
    return name


def _reject_dangerous(segment: str) -> None:
    """Відхиляє метасимволи оболонки та приховані керуючі символи."""
    bad = SHELL_METACHARACTERS.intersection(segment)
    if bad:
        listed = " ".join(sorted(bad))
        raise PathViolation(f"Елемент шляху містить символи оболонки: {listed}")
    if BIDI_CONTROLS.intersection(segment):
        raise PathViolation("Елемент шляху містить керуючі символи напрямку тексту")


def shell_quote(value: str) -> str:
    """Одинарні лапки для POSIX-оболонки.

    Використовується там, де рядок усе ж має потрапити в команду: усередині
    одинарних лапок оболонка не інтерпретує нічого, а сама лапка закривається
    й додається літералом.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def is_within(root: str, path: str) -> bool:
    """Чи лежить `path` усередині каталогу `root` (обидва — нормалізовані)."""
    if root == "/":
        return True
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def join(directory: str, name: str) -> str:
    """Безпечне зʼєднання каталогу та імені з повторною нормалізацією."""
    return normalize_path(posixpath.join(normalize_path(directory), validate_name(name)))


def parent_of(path: str) -> str:
    normalized = normalize_path(path)
    if normalized == "/":
        return "/"
    parent = posixpath.dirname(normalized.rstrip("/"))
    return parent or "/"


def basename(path: str) -> str:
    normalized = normalize_path(path)
    return posixpath.basename(normalized.rstrip("/")) or "/"
