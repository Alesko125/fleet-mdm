"""Реєстрація пристроїв та автентифікація агента (challenge–response)."""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AuthenticationError, ConflictError, NotFoundError, ValidationFailedError
from app.core.redis_client import nonce_key
from app.core.security import (
    as_utc,
    challenge_message,
    create_device_token,
    generate_enrollment_token,
    key_rotation_message,
    load_device_public_key,
    normalize_enrollment_token,
    public_key_fingerprint,
    token_fingerprint,
    token_fingerprints,
    token_prefix,
    utcnow,
    verify_device_signature,
)
from app.db.models import (
    Device,
    DeviceKeyHistory,
    DeviceTelemetry,
    EnrollmentToken,
    LocationSettings,
    User,
)
from app.schemas.enrollment import DeviceEnrollRequest, EnrollmentTokenCreate
from app.services.audit import RESULT_FAILURE, AuditAction, record_audit

logger = logging.getLogger(__name__)

TRANSPARENCY_NOTICE = (
    "Цей пристрій керується адміністратором вашої організації. "
    "Агент видимий у списку застосунків і показує постійне сповіщення. "
    "Геолокація та віддалена підтримка вмикаються окремо і потребують вашої згоди."
)


@dataclass(slots=True)
class CreatedEnrollmentToken:
    model: EnrollmentToken
    raw_token: str
    enroll_url: str
    qr_payload: str


# ---------------------------------------------------- токени (адмін) ------


async def create_enrollment_token(
    session: AsyncSession, *, user: User, data: EnrollmentTokenCreate, ip: str | None,
    user_agent: str | None,
) -> CreatedEnrollmentToken:
    settings = get_settings()
    raw = generate_enrollment_token()
    ttl = data.ttl_seconds or settings.enrollment_token_ttl_seconds

    model = EnrollmentToken(
        token_hash=token_fingerprint(raw),
        token_prefix=token_prefix(raw),
        label=data.label,
        notes=data.notes,
        created_by_id=user.id,
        max_uses=data.max_uses,
        expires_at=utcnow() + timedelta(seconds=ttl),
    )
    session.add(model)
    await session.flush()

    base = settings.public_base_url.rstrip("/")
    enroll_url = f"{base}{settings.api_prefix}/enroll"
    qr_payload = json.dumps(
        {"v": 1, "server": base, "enroll_url": enroll_url, "token": raw},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    await record_audit(
        session,
        action=AuditAction.ENROLLMENT_TOKEN_CREATED,
        user=user,
        object_type="enrollment_token",
        object_id=model.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "label": data.label,
            "max_uses": data.max_uses,
            "expires_at": model.expires_at,
            "token_prefix": model.token_prefix,
        },
    )
    return CreatedEnrollmentToken(model, raw, enroll_url, qr_payload)


async def revoke_enrollment_token(
    session: AsyncSession, *, user: User, token_id: uuid.UUID, ip: str | None,
    user_agent: str | None,
) -> EnrollmentToken:
    model = await session.get(EnrollmentToken, token_id)
    if model is None:
        raise NotFoundError("Enrollment-токен не знайдено")
    if model.revoked_at is None:
        model.revoked_at = utcnow()
    await record_audit(
        session,
        action=AuditAction.ENROLLMENT_TOKEN_REVOKED,
        user=user,
        object_type="enrollment_token",
        object_id=model.id,
        ip=ip,
        user_agent=user_agent,
        details={"token_prefix": model.token_prefix},
    )
    return model


# ---------------------------------------------------- реєстрація агента ---


async def _fail_enrollment(
    session: AsyncSession, *, reason: str, ip: str | None, user_agent: str | None,
    details: dict | None = None, error: Exception,
) -> None:
    await record_audit(
        session,
        action=AuditAction.DEVICE_ENROLL_FAILED,
        result=RESULT_FAILURE,
        ip=ip,
        user_agent=user_agent,
        details={"reason": reason, **(details or {})},
    )
    await session.commit()
    raise error


async def enroll_device(
    session: AsyncSession, *, payload: DeviceEnrollRequest, ip: str | None, user_agent: str | None
) -> tuple[Device, EnrollmentToken]:
    try:
        normalized = normalize_enrollment_token(payload.token)
    except ValueError as exc:
        await _fail_enrollment(
            session, reason="malformed_token", ip=ip, user_agent=user_agent,
            error=ValidationFailedError(str(exc)),
        )
        raise  # pragma: no cover — _fail_enrollment завжди кидає

    # Рядок беремо під блокуванням: без нього два одночасні запити з тим
    # самим токеном обидва побачили б use_count < max_uses і зареєстрували б
    # більше пристроїв, ніж дозволено.
    stmt = sa.select(EnrollmentToken).where(
        EnrollmentToken.token_hash.in_(token_fingerprints(normalized))
    )
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    token = (await session.execute(stmt)).scalar_one_or_none()

    if token is None:
        await _fail_enrollment(
            session, reason="unknown_token", ip=ip, user_agent=user_agent,
            error=AuthenticationError("Недійсний enrollment-токен"),
        )
    assert token is not None

    if not token.is_usable:
        reason = (
            "revoked" if token.revoked_at
            else "expired" if as_utc(token.expires_at) <= utcnow()
            else "exhausted"
        )
        await _fail_enrollment(
            session, reason=reason, ip=ip, user_agent=user_agent,
            details={"token_prefix": token.token_prefix},
            error=AuthenticationError("Enrollment-токен більше не дійсний"),
        )

    # Ключ має бути валідним P-256 SPKI ще до того, як щось запишеться.
    try:
        load_device_public_key(payload.public_key)
    except ValueError as exc:
        await _fail_enrollment(
            session, reason="bad_public_key", ip=ip, user_agent=user_agent,
            error=ValidationFailedError(str(exc)),
        )

    fingerprint = public_key_fingerprint(payload.public_key)
    existing_key = (
        await session.execute(sa.select(Device).where(Device.key_fingerprint == fingerprint))
    ).scalar_one_or_none()
    if existing_key is not None:
        await _fail_enrollment(
            session, reason="duplicate_key", ip=ip, user_agent=user_agent,
            details={"device_id": str(existing_key.id)},
            error=ConflictError("Цей ключ пристрою вже зареєстровано"),
        )

    # Відкликаний під час ротації ключ не можна «переприв'язати» до нового
    # пристрою — інакше стара скомпрометована пара знову стала б валідною.
    retired = (
        await session.execute(
            sa.select(DeviceKeyHistory).where(
                DeviceKeyHistory.key_fingerprint == fingerprint
            )
        )
    ).scalars().first()
    if retired is not None:
        await _fail_enrollment(
            session, reason="retired_key", ip=ip, user_agent=user_agent,
            details={"device_id": str(retired.device_id)},
            error=ConflictError("Цей ключ було відкликано і використати його не можна"),
        )

    info = payload.device
    if info.android_id:
        dup = (
            await session.execute(
                sa.select(Device).where(
                    Device.android_id == info.android_id, Device.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            await _fail_enrollment(
                session, reason="duplicate_device", ip=ip, user_agent=user_agent,
                details={"device_id": str(dup.id)},
                error=ConflictError(
                    "Пристрій із таким ANDROID_ID уже зареєстровано; "
                    "видаліть його в панелі перед повторною реєстрацією"
                ),
            )

    # Використання токена «займається» атомарним UPDATE: рішення про те,
    # чи лишилася квота, ухвалює БД, а не прочитане раніше значення. Саме
    # rowcount, а не повторний SELECT, робить перевірку стійкою до гонки —
    # інакше кілька одночасних запитів побачили б use_count < max_uses
    # одночасно й зареєстрували більше пристроїв, ніж дозволено.
    claimed = await session.execute(
        sa.update(EnrollmentToken)
        .where(
            EnrollmentToken.id == token.id,
            EnrollmentToken.revoked_at.is_(None),
            EnrollmentToken.use_count < EnrollmentToken.max_uses,
        )
        .values(use_count=EnrollmentToken.use_count + 1)
    )
    if (claimed.rowcount or 0) != 1:
        await _fail_enrollment(
            session, reason="exhausted", ip=ip, user_agent=user_agent,
            details={"token_prefix": token.token_prefix},
            error=ConflictError("Enrollment-токен вичерпано"),
        )

    name = info.display_name or " ".join(
        part for part in (info.manufacturer, info.model) if part
    ) or "Android-пристрій"

    device = Device(
        name=name[:200],
        enrollment_token_id=token.id,
        public_key=payload.public_key,
        key_algorithm="EC_P256",
        key_fingerprint=fingerprint,
        android_id=info.android_id or None,
        serial_number=info.serial_number or None,
        status="active",
        management_mode=info.management_mode,
        agent_version=info.agent_version,
        enrolled_at=utcnow(),
        last_ip=ip,
    )
    session.add(device)
    await session.flush()

    session.add(
        DeviceTelemetry(
            device_id=device.id,
            manufacturer=info.manufacturer,
            model=info.model,
            product=info.product,
            android_version=info.android_version,
            sdk_int=info.sdk_int,
            security_patch=info.security_patch,
            build_id=info.build_id,
            kernel_version=info.kernel_version,
            agent_version=info.agent_version,
            collected_at=utcnow(),
        )
    )
    # Геолокація завжди починається вимкненою — вмикається окремо і з аудитом.
    session.add(LocationSettings(device_id=device.id, enabled=False))

    await record_audit(
        session,
        action=AuditAction.DEVICE_ENROLLED,
        device=device,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "token_prefix": token.token_prefix,
            "manufacturer": info.manufacturer,
            "model": info.model,
            "android_version": info.android_version,
            "management_mode": info.management_mode,
            "key_fingerprint": fingerprint,
        },
    )
    return device, token


# ------------------------------------------------ challenge–response ------


async def create_challenge(
    redis: Redis, device_id: uuid.UUID
) -> tuple[str, datetime]:
    settings = get_settings()
    nonce = secrets.token_urlsafe(24)
    ttl = settings.device_challenge_ttl_seconds
    await redis.set(nonce_key(str(device_id), nonce), "1", ex=ttl)
    return nonce, utcnow() + timedelta(seconds=ttl)


async def authenticate_device(
    session: AsyncSession,
    redis: Redis,
    *,
    device_id: uuid.UUID,
    nonce: str,
    signature: str,
    ip: str | None,
    user_agent: str | None,
) -> tuple[Device, str, int]:
    settings = get_settings()
    device = await session.get(Device, device_id)
    if device is None or device.deleted_at is not None:
        raise AuthenticationError("Пристрій не знайдено")
    if device.status != "active":
        raise AuthenticationError("Пристрій деактивовано")

    # Одноразовість nonce: DEL повертає 1 лише першому, хто його спожив.
    consumed = await redis.delete(nonce_key(str(device_id), nonce))
    if not consumed:
        await record_audit(
            session,
            action=AuditAction.DEVICE_AUTH_FAILED,
            result=RESULT_FAILURE,
            device=device,
            object_type="device",
            object_id=device.id,
            ip=ip,
            user_agent=user_agent,
            details={"reason": "unknown_or_used_nonce"},
        )
        await session.commit()
        raise AuthenticationError("Challenge недійсний або вже використаний")

    message = challenge_message(str(device_id), nonce)
    if not verify_device_signature(
        public_key_b64=device.public_key, message=message, signature_b64=signature
    ):
        await record_audit(
            session,
            action=AuditAction.DEVICE_AUTH_FAILED,
            result=RESULT_FAILURE,
            device=device,
            object_type="device",
            object_id=device.id,
            ip=ip,
            user_agent=user_agent,
            details={"reason": "bad_signature"},
        )
        await session.commit()
        raise AuthenticationError("Підпис не пройшов перевірку")

    token = create_device_token(device_id=str(device.id))
    device.last_seen_at = utcnow()
    device.last_ip = ip
    await record_audit(
        session,
        action=AuditAction.DEVICE_AUTH,
        device=device,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
    )
    return device, token.token, settings.device_token_ttl_seconds


# ------------------------------------------------ ротація ключа пристрою ---


def _rotation_nonce_key(device_id: str, nonce: str) -> str:
    return f"devrot:nonce:{device_id}:{nonce}"


def _short(fingerprint: str) -> str:
    return fingerprint[:16] + "\u2026"


async def create_rotation_challenge(
    redis: Redis, device_id: uuid.UUID
) -> tuple[str, datetime]:
    """Окремий nonce саме для ротації.

    Своя простір імен потрібна, щоб nonce звичайної автентифікації не можна
    було використати для підміни ключа й навпаки.
    """
    settings = get_settings()
    nonce = secrets.token_urlsafe(24)
    ttl = settings.device_challenge_ttl_seconds
    await redis.set(_rotation_nonce_key(str(device_id), nonce), "1", ex=ttl)
    return nonce, utcnow() + timedelta(seconds=ttl)


async def _rotation_quota_exceeded(session: AsyncSession, device: Device) -> bool:
    settings = get_settings()
    since = utcnow() - timedelta(days=1)
    used = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(DeviceKeyHistory)
            .where(
                DeviceKeyHistory.device_id == device.id,
                DeviceKeyHistory.retired_at >= since,
            )
        )
    ).scalar_one()
    return int(used) >= settings.device_key_rotation_daily_limit


async def rotate_device_key(
    session: AsyncSession,
    redis: Redis,
    *,
    device: Device,
    new_public_key: str,
    nonce: str,
    signature: str,
    reason: str,
    ip: str | None,
    user_agent: str | None,
) -> Device:
    """Замінює ключ пристрою на новий, підтверджений підписом **старого**.

    Володіння чинним ключем — єдиний спосіб довести, що ротацію просить саме
    цей пристрій. Сервер, як і раніше, не бачить приватної частини: агент
    генерує нову пару в Android Keystore і надсилає лише публічну.
    """

    async def deny(detail: str, error: Exception) -> None:
        await record_audit(
            session,
            action=AuditAction.DEVICE_KEY_ROTATION_FAILED,
            result=RESULT_FAILURE,
            device=device,
            object_type="device",
            object_id=device.id,
            ip=ip,
            user_agent=user_agent,
            details={"reason": detail},
        )
        await session.commit()
        raise error

    if await _rotation_quota_exceeded(session, device):
        await deny(
            "daily_limit",
            ConflictError("Перевищено добовий ліміт ротацій ключа для цього пристрою"),
        )

    try:
        load_device_public_key(new_public_key)
    except ValueError as exc:
        await deny("bad_new_key", ValidationFailedError(str(exc)))

    new_fingerprint = public_key_fingerprint(new_public_key)
    if new_fingerprint == device.key_fingerprint:
        await deny("same_key", ValidationFailedError("Новий ключ збігається з поточним"))

    clash = (
        await session.execute(
            sa.select(Device).where(
                Device.key_fingerprint == new_fingerprint, Device.id != device.id
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        await deny("key_in_use", ConflictError("Такий ключ уже зареєстровано"))

    reused = (
        await session.execute(
            sa.select(DeviceKeyHistory).where(
                DeviceKeyHistory.key_fingerprint == new_fingerprint
            )
        )
    ).scalars().first()
    if reused is not None:
        await deny(
            "key_reuse",
            ConflictError("Цей ключ уже використовувався раніше — згенеруйте новий"),
        )

    consumed = await redis.delete(_rotation_nonce_key(str(device.id), nonce))
    if not consumed:
        await deny(
            "unknown_or_used_nonce",
            AuthenticationError("Challenge ротації недійсний або вже використаний"),
        )

    message = key_rotation_message(str(device.id), new_public_key, nonce)
    if not verify_device_signature(
        public_key_b64=device.public_key, message=message, signature_b64=signature
    ):
        await deny(
            "bad_signature",
            AuthenticationError("Підпис старим ключем не пройшов перевірку"),
        )

    now = utcnow()
    old_fingerprint = device.key_fingerprint
    session.add(
        DeviceKeyHistory(
            device_id=device.id,
            public_key=device.public_key,
            key_fingerprint=old_fingerprint,
            key_algorithm=device.key_algorithm,
            generation=device.key_generation,
            reason=reason[:64],
            retired_at=now,
        )
    )

    device.public_key = new_public_key
    device.key_fingerprint = new_fingerprint
    device.key_generation += 1
    device.key_created_at = now
    device.key_rotated_at = now
    # Токени, видані під старий ключ, більше не діють: агент має пройти
    # challenge-response наново вже новою парою.
    device.tokens_valid_from = now

    await record_audit(
        session,
        action=AuditAction.DEVICE_KEY_ROTATED,
        device=device,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={
            "reason": reason,
            "generation": device.key_generation,
            "old_fingerprint": _short(old_fingerprint),
            "new_fingerprint": _short(new_fingerprint),
        },
    )
    return device


async def require_key_rotation(
    session: AsyncSession,
    *,
    device: Device,
    user: User,
    reason: str,
    ip: str | None,
    user_agent: str | None,
) -> Device:
    """Адміністратор відкликає токени пристрою.

    Продовжити роботу зі старими токенами агент не зможе — він піде на
    повторну автентифікацію, а за політикою віку ще й перевипустить ключ.
    """
    device.tokens_valid_from = utcnow()
    await record_audit(
        session,
        action=AuditAction.DEVICE_KEY_ROTATION_REQUIRED,
        user=user,
        object_type="device",
        object_id=device.id,
        ip=ip,
        user_agent=user_agent,
        details={"reason": reason, "generation": device.key_generation},
    )
    return device


def key_rotation_due(device: Device) -> bool:
    """Чи час пристрою перевипустити ключ за політикою віку."""
    max_age = get_settings().device_key_max_age_days
    if max_age <= 0:
        return False
    return device.key_age_days >= max_age


async def purge_key_history(session: AsyncSession) -> int:
    """Прибирає записи історії ключів, старші за політику зберігання."""
    settings = get_settings()
    cutoff = utcnow() - timedelta(days=settings.device_key_history_days)
    result = await session.execute(
        sa.delete(DeviceKeyHistory).where(DeviceKeyHistory.retired_at < cutoff)
    )
    return int(result.rowcount or 0)
