import base64

import pytest

from app.core.logging import mask_sensitive
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TokenError,
    challenge_message,
    create_access_token,
    decode_token,
    generate_enrollment_token,
    hash_password,
    normalize_enrollment_token,
    public_key_fingerprint,
    token_fingerprint,
    validate_password_strength,
    verify_device_signature,
    verify_password,
)


def test_password_hash_roundtrip():
    hashed = hash_password("Sup3rSecretPass!2026")
    assert hashed != "Sup3rSecretPass!2026"
    assert verify_password("Sup3rSecretPass!2026", hashed)
    assert not verify_password("інший пароль", hashed)


def test_verify_password_handles_garbage_hash():
    assert verify_password("будь-що", "не-хеш") is False


def test_password_policy():
    assert validate_password_strength("Sup3rSecretPass!2026") == []
    assert validate_password_strength("короткий1") != []
    assert validate_password_strength("aaaaaaaaaaaaaaa") != []


def test_enrollment_token_format_and_normalization():
    token = generate_enrollment_token()
    assert token.startswith("FLEET-")
    assert len(token.split("-")) == 6  # FLEET + 5 груп по 4 символи
    assert normalize_enrollment_token(token.lower().replace("-", " ")) == token
    with pytest.raises(ValueError):
        normalize_enrollment_token("FLEET-TOOSHORT")


def test_token_fingerprint_is_stable_and_not_reversible():
    token = generate_enrollment_token()
    digest = token_fingerprint(token)
    assert digest == token_fingerprint(token)
    assert len(digest) == 64
    assert token not in digest


def test_access_token_type_is_enforced():
    issued = create_access_token(subject="user-1", role="admin")
    claims = decode_token(issued.token, expected_type=TOKEN_TYPE_ACCESS)
    assert claims["sub"] == "user-1"
    assert claims["role"] == "admin"
    with pytest.raises(TokenError):
        decode_token(issued.token, expected_type="device")


def test_tampered_token_rejected():
    issued = create_access_token(subject="user-1", role="admin")
    with pytest.raises(TokenError):
        decode_token(issued.token[:-3] + "abc", expected_type=TOKEN_TYPE_ACCESS)


def test_device_signature_verification(device_key):
    message = challenge_message("device-1", "nonce-1")
    signature = device_key.sign(message)
    assert verify_device_signature(
        public_key_b64=device_key.public_key_b64, message=message, signature_b64=signature
    )
    # Підпис не проходить для іншого повідомлення…
    assert not verify_device_signature(
        public_key_b64=device_key.public_key_b64,
        message=challenge_message("device-2", "nonce-1"),
        signature_b64=signature,
    )
    # …і сміття не ламає перевірку.
    assert not verify_device_signature(
        public_key_b64=device_key.public_key_b64, message=message, signature_b64="не-base64!!"
    )


def test_public_key_fingerprint_matches_sha256(device_key):
    import hashlib

    der = base64.b64decode(device_key.public_key_b64)
    assert public_key_fingerprint(device_key.public_key_b64) == hashlib.sha256(der).hexdigest()


def test_rsa_key_is_rejected():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    from app.core.security import load_device_public_key

    with pytest.raises(ValueError):
        load_device_public_key(base64.b64encode(der).decode())


def test_log_masking_hides_secrets():
    masked = mask_sensitive(
        {"password": "секрет", "nested": {"refresh_token": "abc", "keep": "видно"}}
    )
    assert masked["password"] == "***"
    assert masked["nested"]["refresh_token"] == "***"
    assert masked["nested"]["keep"] == "видно"
