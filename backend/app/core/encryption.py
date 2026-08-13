"""Шифрування чутливих даних у спокої (at rest).

Використовується комунікаційним модулем: тексти SMS та номери в журналі
дзвінків зберігаються в БД **лише зашифрованими**. Навіть маючи дамп бази,
без ключа (`COMMS_ENCRYPTION_KEY`, який живе в оточенні, а не в БД) прочитати
вміст неможливо.

Алгоритм — AES-256-GCM: автентифіковане шифрування, тож підміну шифротексту
видно при розшифруванні. Ключ виводиться з переданого секрету через
HKDF-SHA256 — це дозволяє задати ключ будь-яким достатньо довгим рядком і
водночас завжди отримати рівно 32 байти.

`associated_data` (тип запису) прив'язує шифротекст до його призначення:
блок SMS не можна «підставити» замість запису дзвінка — розшифрування впаде.
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import get_settings

# Довжина nonce для GCM — 96 біт (рекомендація NIST).
_NONCE_BYTES = 12
# Фіксована «сіль» HKDF: не секрет, лише розділяє простори ключів.
_HKDF_SALT = b"fleet-comms-at-rest-v1"


class EncryptionUnavailableError(RuntimeError):
    """Ключ шифрування не налаштовано — модуль не має права зберігати дані."""


class DecryptionError(ValueError):
    """Шифротекст пошкоджено, підроблено або зашифровано іншим ключем."""


def _derive_key(secret: str) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=b"aes-256-gcm",
    )
    return hkdf.derive(secret.encode("utf-8"))


class Encryptor:
    """AES-256-GCM поверх виведеного ключа."""

    def __init__(self, secret: str) -> None:
        self._aead = AESGCM(_derive_key(secret))

    def encrypt(self, plaintext: str, *, context: str) -> str:
        """Повертає base64(`nonce || ciphertext || tag`)."""
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(
            nonce, plaintext.encode("utf-8"), context.encode("utf-8")
        )
        return base64.b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, token: str, *, context: str) -> str:
        try:
            raw = base64.b64decode(token, validate=True)
        except (ValueError, TypeError) as exc:
            raise DecryptionError("Некоректний формат шифротексту") from exc
        if len(raw) <= _NONCE_BYTES:
            raise DecryptionError("Шифротекст надто короткий")
        nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        try:
            plaintext = self._aead.decrypt(nonce, ciphertext, context.encode("utf-8"))
        except InvalidTag as exc:
            raise DecryptionError(
                "Не вдалося розшифрувати: ключ не той або дані підроблено"
            ) from exc
        return plaintext.decode("utf-8")


@lru_cache
def get_encryptor() -> Encryptor:
    """Спільний екземпляр шифрувальника.

    Кидає :class:`EncryptionUnavailableError`, якщо ключ не налаштований —
    комунікаційний модуль спирається на це, щоб ніколи не зберегти чутливі
    дані відкритим текстом навіть за помилки конфігурації.
    """
    settings = get_settings()
    secret = settings.comms_encryption_key
    if not secret or secret in settings_insecure_keys():
        raise EncryptionUnavailableError(
            "COMMS_ENCRYPTION_KEY не налаштовано — комунікаційний модуль вимкнено"
        )
    if len(secret) < 32:
        raise EncryptionUnavailableError(
            "COMMS_ENCRYPTION_KEY коротший за 32 символи"
        )
    return Encryptor(secret)


def encryption_available() -> bool:
    """Чи можна зараз шифрувати (ключ налаштований і достатньо надійний)."""
    try:
        get_encryptor()
        return True
    except EncryptionUnavailableError:
        return False


def settings_insecure_keys() -> frozenset[str]:
    return frozenset(
        {"", "change-me-comms-encryption-key-at-least-32-chars"}
    )
