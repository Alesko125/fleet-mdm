"""Комунікаційний модуль: SMS та журнал дзвінків.

Межі модуля навмисно жорсткі:

* **Вимкнено за замовчуванням.** Поки адміністратор явно не ввімкне модуль
  для конкретного пристрою, агент нічого не читає й не надсилає.
* **Лише офіційні джерела Android.** Дані беруться з системних content
  provider'ів `Telephony.Sms` і `CallLog.Calls` за рантайм-дозволами
  READ_SMS / READ_CALL_LOG. Перехоплення чужих застосунків, читання
  сповіщень, кейлогінгу чи витягування OTP тут немає — ці можливості
  Android застосунку легально не надає, тож вони й не реалізуються.
* **Шифрування at rest.** Тексти й номери лягають у БД лише зашифрованими
  (AES-256-GCM); без `COMMS_ENCRYPTION_KEY` модуль узагалі не вмикається.
* **Видимість для власника.** Агент показує стан модуля на своєму екрані й
  у постійному сповіщенні — прихованого стеження немає.
* **Аудит і ретеншн.** Кожна зміна конфігурації та кожен перегляд даних
  фіксуються; старі записи прибирає фонова задача.
"""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.encryption import (
    DecryptionError,
    encryption_available,
    get_encryptor,
)
from app.core.errors import ConflictError, ValidationFailedError
from app.core.security import utcnow
from app.db.models import CallLogEntry, CommsSettings, Device, SmsMessage, User
from app.schemas.comms import (
    AgentCommsConfig,
    CallIngestItem,
    CommsSettingsUpdate,
    SmsIngestItem,
)
from app.services.audit import AuditAction, record_audit

# Контексти шифрування — розділяють простори (SMS не підставити за дзвінок).
_CTX_SMS_ADDR = "comms:sms:address"
_CTX_SMS_BODY = "comms:sms:body"
_CTX_CALL_NUMBER = "comms:call:number"
_CTX_CALL_CONTACT = "comms:call:contact"

NOTICE = (
    "Адміністратор увімкнув збір SMS та/або журналу дзвінків на цьому "
    "пристрої. Дані передаються на сервер керування зашифрованим каналом і "
    "зберігаються в зашифрованому вигляді. Ви можете звернутися до "
    "адміністратора, щоб вимкнути цей модуль."
)


async def get_or_create(session: AsyncSession, device: Device) -> CommsSettings:
    row = await session.get(CommsSettings, device.id)
    if row is None:
        row = CommsSettings(device_id=device.id)
        session.add(row)
        await session.flush()
    return row


def to_agent_config(row: CommsSettings) -> AgentCommsConfig:
    settings = get_settings()
    return AgentCommsConfig(
        enabled=row.enabled,
        collect_sms=row.enabled and row.sms_enabled,
        collect_call_log=row.enabled and row.call_log_enabled,
        metadata_only=row.metadata_only,
        history_days=min(row.retention_days, settings.comms_retention_max_days),
        max_batch=settings.comms_max_batch,
        notice=NOTICE,
    )


async def update_settings(
    session: AsyncSession,
    *,
    device: Device,
    data: CommsSettingsUpdate,
    user: User,
    ip: str | None,
    user_agent: str | None,
) -> CommsSettings:
    """Змінює конфігурацію модуля з повним аудитом переходів."""
    settings = get_settings()
    row = await get_or_create(session, device)

    was_enabled = row.enabled
    before = _snapshot(row)

    if data.retention_days is not None:
        if data.retention_days > settings.comms_retention_max_days:
            raise ValidationFailedError(
                f"Ретеншн не може перевищувати {settings.comms_retention_max_days} днів"
            )
        row.retention_days = data.retention_days
    if data.sms_enabled is not None:
        row.sms_enabled = data.sms_enabled
    if data.call_log_enabled is not None:
        row.call_log_enabled = data.call_log_enabled
    if data.metadata_only is not None:
        row.metadata_only = data.metadata_only

    if data.enabled is not None:
        if data.enabled and not was_enabled:
            # Увімкнути модуль можна лише коли є куди безпечно класти дані.
            if not encryption_available():
                raise ConflictError(
                    "Неможливо ввімкнути модуль: не налаштовано шифрування "
                    "at-rest (COMMS_ENCRYPTION_KEY)"
                )
            row.enabled = True
            row.enabled_at = utcnow()
            row.enabled_by_id = user.id
            row.disabled_at = None
        elif not data.enabled and was_enabled:
            row.enabled = False
            row.disabled_at = utcnow()

    row.updated_by_id = user.id

    # Окремі, промовисті події для вмикання/вимикання — щоб їх було легко
    # знайти в журналі — плюс загальна подія зміни конфігурації.
    if data.enabled is True and not was_enabled:
        await record_audit(
            session, action=AuditAction.COMMS_ENABLED, user=user,
            object_type="device", object_id=device.id, ip=ip, user_agent=user_agent,
            details={
                "sms": row.sms_enabled, "call_log": row.call_log_enabled,
                "metadata_only": row.metadata_only, "retention_days": row.retention_days,
                "enabled_at": row.enabled_at,
            },
        )
    elif data.enabled is False and was_enabled:
        await record_audit(
            session, action=AuditAction.COMMS_DISABLED, user=user,
            object_type="device", object_id=device.id, ip=ip, user_agent=user_agent,
            details={"disabled_at": row.disabled_at},
        )

    await record_audit(
        session, action=AuditAction.COMMS_CONFIG_CHANGED, user=user,
        object_type="device", object_id=device.id, ip=ip, user_agent=user_agent,
        details={"before": before, "after": _snapshot(row)},
    )
    await session.flush()
    return row


def _snapshot(row: CommsSettings) -> dict:
    return {
        "enabled": row.enabled,
        "sms_enabled": row.sms_enabled,
        "call_log_enabled": row.call_log_enabled,
        "metadata_only": row.metadata_only,
        "retention_days": row.retention_days,
    }


async def record_permission_state(
    session: AsyncSession,
    *,
    device: Device,
    sms_permission: str,
    call_log_permission: str,
    ip: str | None,
    user_agent: str | None,
) -> CommsSettings:
    """Фіксує фактичний стан рантайм-дозволів, який повідомив агент."""
    row = await get_or_create(session, device)
    changed = (
        row.sms_permission != sms_permission
        or row.call_log_permission != call_log_permission
    )
    row.sms_permission = sms_permission
    row.call_log_permission = call_log_permission
    row.permission_reported_at = utcnow()

    if changed:
        await record_audit(
            session, action=AuditAction.COMMS_PERMISSION_CHANGED, device=device,
            object_type="device", object_id=device.id, ip=ip, user_agent=user_agent,
            details={
                "sms_permission": sms_permission,
                "call_log_permission": call_log_permission,
            },
        )
    return row


async def ingest_batch(
    session: AsyncSession,
    *,
    device: Device,
    sms: list[SmsIngestItem],
    calls: list[CallIngestItem],
    ip: str | None,
    user_agent: str | None,
) -> tuple[int, int, int]:
    """Приймає й шифрує пакет записів. Повертає (sms, calls, rejected).

    Приймається лише те, що адміністратор увімкнув: якщо збір SMS вимкнено,
    надіслані SMS відхиляються, навіть якщо агент їх прислав. Це другий
    рубіж на випадок розсинхронізації конфігурації.
    """
    row = await get_or_create(session, device)
    if not row.enabled:
        raise ConflictError("Комунікаційний модуль вимкнено для цього пристрою")

    encryptor = get_encryptor()  # кидає, якщо ключ зник — краще відмовити
    settings = get_settings()
    max_batch = settings.comms_max_batch

    rejected = 0
    sms_stored = 0
    calls_stored = 0

    if row.sms_enabled and sms:
        sms_stored = await _store_sms(session, device, row, sms[:max_batch], encryptor)
        rejected += max(0, len(sms) - max_batch)
    else:
        rejected += len(sms)

    if row.call_log_enabled and calls:
        calls_stored = await _store_calls(session, device, row, calls[:max_batch], encryptor)
        rejected += max(0, len(calls) - max_batch)
    else:
        rejected += len(calls)

    row.last_sync_at = utcnow()

    if sms_stored or calls_stored:
        # У журнал іде лише кількість — жодного вмісту.
        await record_audit(
            session, action=AuditAction.COMMS_DATA_INGESTED, device=device,
            object_type="device", object_id=device.id, ip=ip, user_agent=user_agent,
            details={"sms": sms_stored, "calls": calls_stored, "rejected": rejected},
        )
    return sms_stored, calls_stored, rejected


async def _existing_provider_ids(
    session: AsyncSession, model, device: Device, ids: list[str]
) -> set[str]:
    if not ids:
        return set()
    rows = (
        await session.execute(
            sa.select(model.provider_id).where(
                model.device_id == device.id, model.provider_id.in_(ids)
            )
        )
    ).scalars().all()
    return {r for r in rows if r is not None}


async def _store_sms(session, device, row, items, encryptor) -> int:
    known = await _existing_provider_ids(
        session, SmsMessage, device, [i.provider_id for i in items if i.provider_id]
    )
    stored = 0
    for item in items:
        if item.provider_id and item.provider_id in known:
            continue  # той самий запис уже є — не дублюємо
        body = None if row.metadata_only else item.body
        session.add(
            SmsMessage(
                device_id=device.id,
                provider_id=item.provider_id,
                direction=item.direction,
                address_encrypted=(
                    encryptor.encrypt(item.address, context=_CTX_SMS_ADDR)
                    if item.address else None
                ),
                body_encrypted=(
                    encryptor.encrypt(body, context=_CTX_SMS_BODY) if body else None
                ),
                body_length=len(item.body) if item.body else None,
                sent_at=item.sent_at,
            )
        )
        stored += 1
    return stored


async def _store_calls(session, device, row, items, encryptor) -> int:
    known = await _existing_provider_ids(
        session, CallLogEntry, device, [i.provider_id for i in items if i.provider_id]
    )
    stored = 0
    for item in items:
        if item.provider_id and item.provider_id in known:
            continue
        session.add(
            CallLogEntry(
                device_id=device.id,
                provider_id=item.provider_id,
                direction=item.direction,
                number_encrypted=(
                    encryptor.encrypt(item.number, context=_CTX_CALL_NUMBER)
                    if item.number else None
                ),
                contact_encrypted=(
                    encryptor.encrypt(item.contact, context=_CTX_CALL_CONTACT)
                    if item.contact else None
                ),
                duration_seconds=item.duration_seconds,
                occurred_at=item.occurred_at,
            )
        )
        stored += 1
    return stored


# ------------------------------------------------- перегляд (comms:read) ---


def _safe_decrypt(token: str | None, *, context: str) -> str | None:
    if not token:
        return None
    try:
        return get_encryptor().decrypt(token, context=context)
    except DecryptionError:
        # Ключ змінили без переशифрування — не валимо весь список.
        return "⚠ не вдалося розшифрувати"


async def list_sms(
    session: AsyncSession, device: Device, *, limit: int, offset: int
) -> tuple[list[dict], int]:
    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(SmsMessage).where(
                SmsMessage.device_id == device.id
            )
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(SmsMessage)
            .where(SmsMessage.device_id == device.id)
            .order_by(SmsMessage.sent_at.desc().nullslast(), SmsMessage.collected_at.desc())
            .limit(limit).offset(offset)
        )
    ).scalars().all()

    items = [
        {
            "id": r.id,
            "direction": r.direction,
            "address": _safe_decrypt(r.address_encrypted, context=_CTX_SMS_ADDR),
            "body": _safe_decrypt(r.body_encrypted, context=_CTX_SMS_BODY),
            "body_length": r.body_length,
            "sent_at": r.sent_at,
            "collected_at": r.collected_at,
        }
        for r in rows
    ]
    return items, total


async def list_calls(
    session: AsyncSession, device: Device, *, limit: int, offset: int
) -> tuple[list[dict], int]:
    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(CallLogEntry).where(
                CallLogEntry.device_id == device.id
            )
        )
    ).scalar_one()
    rows = (
        await session.execute(
            sa.select(CallLogEntry)
            .where(CallLogEntry.device_id == device.id)
            .order_by(CallLogEntry.occurred_at.desc().nullslast(), CallLogEntry.collected_at.desc())
            .limit(limit).offset(offset)
        )
    ).scalars().all()

    items = [
        {
            "id": r.id,
            "direction": r.direction,
            "number": _safe_decrypt(r.number_encrypted, context=_CTX_CALL_NUMBER),
            "contact": _safe_decrypt(r.contact_encrypted, context=_CTX_CALL_CONTACT),
            "duration_seconds": r.duration_seconds,
            "occurred_at": r.occurred_at,
            "collected_at": r.collected_at,
        }
        for r in rows
    ]
    return items, total


async def purge_device(
    session: AsyncSession, device: Device, *, user: User, ip: str | None, user_agent: str | None
) -> tuple[int, int]:
    """Видаляє всі зібрані дані пристрою (за запитом адміністратора)."""
    sms = (
        await session.execute(sa.delete(SmsMessage).where(SmsMessage.device_id == device.id))
    ).rowcount or 0
    calls = (
        await session.execute(sa.delete(CallLogEntry).where(CallLogEntry.device_id == device.id))
    ).rowcount or 0
    await record_audit(
        session, action=AuditAction.COMMS_DATA_PURGED, user=user,
        object_type="device", object_id=device.id, ip=ip, user_agent=user_agent,
        details={"sms": int(sms), "calls": int(calls), "scope": "device"},
    )
    return int(sms), int(calls)


async def purge_expired(session: AsyncSession) -> dict[str, int]:
    """Фонове прибирання за per-device ретеншном.

    Кожен пристрій має власний строк; глобальна стеля обмежує його зверху.
    """
    settings = get_settings()
    ceiling = settings.comms_retention_max_days

    removed = {"comms_sms": 0, "comms_calls": 0}
    rows = (await session.execute(sa.select(CommsSettings))).scalars().all()
    for row in rows:
        days = min(row.retention_days, ceiling)
        cutoff = utcnow() - timedelta(days=days)
        sms = (
            await session.execute(
                sa.delete(SmsMessage).where(
                    SmsMessage.device_id == row.device_id,
                    SmsMessage.collected_at < cutoff,
                )
            )
        ).rowcount or 0
        calls = (
            await session.execute(
                sa.delete(CallLogEntry).where(
                    CallLogEntry.device_id == row.device_id,
                    CallLogEntry.collected_at < cutoff,
                )
            )
        ).rowcount or 0
        removed["comms_sms"] += int(sms)
        removed["comms_calls"] += int(calls)
    return removed
