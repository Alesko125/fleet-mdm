"""Криптографія: паролі, JWT, хешування токенів, ключі пристроїв.

Ключ пристрою ніколи не потрапляє на сервер — зберігається лише публічна
частина (SPKI/DER у base64). Автентифікація агента — challenge–response.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import get_settings

_hasher = PasswordHasher()

# Алфавіт без символів, які легко сплутати (I/L/O/U, 0/1).
_TOKEN_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"
TOKEN_TYPE_DEVICE = "device"


class TokenError(Exception):
    """Некоректний, прострочений або невідповідного типу токен."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Приводить дату до aware-UTC.

    PostgreSQL повертає timestamptz з таймзоною, SQLite (тести) — без неї.
    Порівнювати naive і aware не можна, тож нормалізуємо в одному місці.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------- паролі ---


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (
        VerifyMismatchError,
        VerificationError,
        InvalidHashError,
        UnicodeEncodeError,
        TypeError,
    ):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, UnicodeEncodeError, TypeError, ValueError):
        return True


def validate_password_strength(password: str) -> list[str]:
    """Повертає список проблем; порожній список = пароль прийнятний."""
    settings = get_settings()
    problems: list[str] = []
    if len(password) < settings.min_password_length:
        problems.append(f"мінімум {settings.min_password_length} символів")
    if password.isdigit() or password.isalpha():
        problems.append("має містити і літери, і цифри")
    if password.lower() in {"password", "adminadmin", "123456789012"}:
        problems.append("надто поширений пароль")
    return problems


# ------------------------------------------------- хешування секретів ------


def _hmac(secret: str, raw_token: str) -> str:
    return hmac.new(secret.encode(), raw_token.encode(), hashlib.sha256).hexdigest()


def token_fingerprint(raw_token: str) -> str:
    """HMAC-SHA256 від токена — саме це зберігається в БД.

    Для запису завжди береться **поточний** секрет; звірка зі збереженим
    значенням іде через :func:`token_fingerprints`, який знає і попередній.
    """
    return _hmac(get_settings().token_hash_secret, raw_token)


def token_fingerprints(raw_token: str) -> list[str]:
    """Усі відбитки токена: поточним секретом і попереднім (під час ротації).

    Дозволяє змінити TOKEN_HASH_SECRET без інвалідації живих refresh- та
    enrollment-токенів: старі записи ще знаходяться, нові пишуться новим
    ключем.
    """
    return [_hmac(secret, raw_token) for secret in get_settings().token_hash_secrets]


def generate_enrollment_token() -> str:
    """`FLEET-XXXX-XXXX-XXXX-XXXX` — 20 символів ≈ 98 біт ентропії."""
    body = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(20))
    groups = [body[i : i + 4] for i in range(0, 20, 4)]
    return "FLEET-" + "-".join(groups)


def normalize_enrollment_token(raw: str) -> str:
    """Прибирає пробіли/регістр, щоб токен можна було ввести вручну."""
    cleaned = "".join(ch for ch in raw.strip().upper() if ch.isalnum())
    if cleaned.startswith("FLEET"):
        cleaned = cleaned[5:]
    if len(cleaned) != 20:
        raise ValueError("Enrollment-токен має містити 20 символів після префікса")
    groups = [cleaned[i : i + 4] for i in range(0, 20, 4)]
    return "FLEET-" + "-".join(groups)


def token_prefix(token: str) -> str:
    """Частина токена, яку безпечно показувати в UI/логах."""
    return token[:11] + "…"


# ------------------------------------------------------------------ JWT ---


@dataclass(slots=True)
class IssuedToken:
    token: str
    jti: uuid.UUID
    expires_at: datetime


def _encode(payload: dict[str, Any]) -> str:
    settings = get_settings()
    payload = {**payload, "iss": settings.jwt_issuer, "aud": settings.jwt_audience}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(
    *, subject: str, role: str, extra: dict[str, Any] | None = None
) -> IssuedToken:
    settings = get_settings()
    now = utcnow()
    expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
    jti = uuid.uuid4()
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": TOKEN_TYPE_ACCESS,
        "role": role,
        "jti": str(jti),
        "iat": int(now.timestamp()),
        # Мілісекунди потрібні для точної звірки з `sessions_valid_from`:
        # цілих секунд `iat` замало, щоб відкликати токен, виданий у ту саму
        # секунду, і при цьому не вбити щойно виданий після відкликання.
        "iat_ms": int(now.timestamp() * 1000),
        "exp": int(expires_at.timestamp()),
    }
    if extra:
        payload.update(extra)
    return IssuedToken(_encode(payload), jti, expires_at)


def create_refresh_token(*, subject: str, family_id: uuid.UUID) -> IssuedToken:
    settings = get_settings()
    now = utcnow()
    expires_at = now + timedelta(seconds=settings.refresh_token_ttl_seconds)
    jti = uuid.uuid4()
    payload = {
        "sub": subject,
        "typ": TOKEN_TYPE_REFRESH,
        "fam": str(family_id),
        "jti": str(jti),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return IssuedToken(_encode(payload), jti, expires_at)


def create_device_token(*, device_id: str) -> IssuedToken:
    settings = get_settings()
    now = utcnow()
    expires_at = now + timedelta(seconds=settings.device_token_ttl_seconds)
    jti = uuid.uuid4()
    payload = {
        "sub": device_id,
        "typ": TOKEN_TYPE_DEVICE,
        "scope": f"device:{device_id}",
        "jti": str(jti),
        "iat": int(now.timestamp()),
        "iat_ms": int(now.timestamp() * 1000),
        "exp": int(expires_at.timestamp()),
    }
    return IssuedToken(_encode(payload), jti, expires_at)


def decode_token(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    """Перевіряє підпис, тип, issuer/audience та глобальну епоху токенів.

    Підпис приймається як поточним ключем, так і попереднім — це і робить
    ротацію JWT_SECRET безшовною. Алгоритм фіксований (жодного `alg: none`
    чи підміни на HMAC), issuer та audience перевіряються явно.
    """
    settings = get_settings()
    claims: dict[str, Any] | None = None
    expired = False

    for secret in settings.jwt_decode_secrets:
        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=[settings.jwt_algorithm],
                audience=settings.jwt_audience,
                issuer=settings.jwt_issuer,
                options={"require": ["exp", "iat", "sub", "typ"]},
            )
            break
        except jwt.ExpiredSignatureError:
            expired = True
        except jwt.InvalidTokenError:
            continue

    if claims is None:
        raise TokenError(
            "Термін дії токена вичерпано" if expired else "Некоректний токен"
        )

    if expected_type and claims.get("typ") != expected_type:
        raise TokenError(f"Очікувався токен типу {expected_type}")

    epoch = settings.token_epoch_at
    if epoch is not None and token_issued_at(claims) < epoch:
        raise TokenError("Токен анульовано глобальною ротацією")

    return claims


def token_issued_at(claims: dict[str, Any]) -> datetime:
    """Момент видачі токена з точністю до мілісекунди (якщо є `iat_ms`)."""
    issued_ms = claims.get("iat_ms")
    if issued_ms is not None:
        return datetime.fromtimestamp(int(issued_ms) / 1000, tz=timezone.utc)
    return datetime.fromtimestamp(int(claims["iat"]), tz=timezone.utc)


def token_predates(claims: dict[str, Any], moment: datetime | None) -> bool:
    """Чи виданий токен раніше за точку відсічення сесій.

    Токени старого зразка мають лише посекундний `iat`; для них мітку
    відсічення опускаємо до цілої секунди, щоб не відкинути дійсний токен
    через частки секунди. Нові токени несуть `iat_ms` і звіряються точно.
    """
    if moment is None:
        return False

    if "iat_ms" in claims:
        # `iat_ms` обрізається до мілісекунди, а мітка в БД має мікросекундну
        # точність. Без вирівнювання токен, виданий у ТУ САМУ мілісекунду, що
        # й точка відсічення, округлявся вниз і виглядав старішим за неї —
        # пристрій отримував 401 одразу після ротації ключа, коли агент
        # автентифікується наново. Тому опускаємо мітку до тієї ж
        # мілісекундної сітки: вікно неоднозначності — 1 мс.
        cutoff = moment.replace(microsecond=(moment.microsecond // 1000) * 1000)
        return token_issued_at(claims) < cutoff

    # Токени старого зразка несуть лише посекундний `iat`.
    return token_issued_at(claims) < moment.replace(microsecond=0)


# ------------------------------------------------- ключі пристроїв (EC) ---


def load_device_public_key(public_key_b64: str) -> ec.EllipticCurvePublicKey:
    """Завантажує SPKI/DER (base64) і перевіряє, що це саме P-256."""
    try:
        der = base64.b64decode(public_key_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Публічний ключ не є коректним base64") from exc
    try:
        key = serialization.load_der_public_key(der)
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise ValueError("Не вдалося розібрати публічний ключ (очікується SPKI/DER)") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError("Очікується ключ на еліптичній кривій")
    if not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Підтримується лише крива P-256 (secp256r1)")
    return key


def public_key_fingerprint(public_key_b64: str) -> str:
    """SHA-256 від DER-представлення — стабільний ідентифікатор ключа."""
    der = base64.b64decode(public_key_b64, validate=True)
    return hashlib.sha256(der).hexdigest()


def verify_device_signature(*, public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    """Перевіряє DER-підпис `SHA256withECDSA`, який робить Android Keystore."""
    try:
        key = load_device_public_key(public_key_b64)
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error):
        return False
    try:
        key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


def challenge_message(device_id: str, nonce: str) -> bytes:
    """Рядок, який підписує агент. Прив'язаний до device_id → без підміни."""
    return f"fleet-device-auth:{device_id}:{nonce}".encode()


# ----------------------------------------------------------------- CSRF ---


def generate_csrf_token() -> str:
    """Випадковий токен для схеми double-submit cookie."""
    return secrets.token_urlsafe(32)


def csrf_tokens_match(cookie_value: str | None, header_value: str | None) -> bool:
    """Порівняння в постійному часі; порожні значення завжди не збігаються."""
    if not cookie_value or not header_value:
        return False
    if len(cookie_value) > 512 or len(header_value) > 512:
        return False
    return hmac.compare_digest(cookie_value, header_value)


# ------------------------------------------------- ротація ключа пристрою --


def key_rotation_message(device_id: str, new_public_key_b64: str, nonce: str) -> bytes:
    """Рядок, який агент підписує **старим** ключем, щоб замінити його новим.

    Прив'язка одразу до трьох величин — пристрою, нового ключа й одноразового
    nonce — не дає ані підмінити ключ, ані відтворити запит повторно.
    """
    digest = hashlib.sha256(new_public_key_b64.encode()).hexdigest()
    return f"fleet-device-key-rotation:{device_id}:{digest}:{nonce}".encode()
