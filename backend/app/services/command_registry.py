"""Реєстр команд пристрою.

Це навмисно **не** віддалена оболонка. Виконати можна лише те, що описано
тут: закритий перелік типів, у кожного — власна схема параметрів, потрібний
дозвіл, вимоги до режиму керування пристроєм і термін придатності. Довільний
рядок, який агент виконав би на пристрої, у системі передати неможливо —
ані через API, ані через payload команди.

Реєстр — єдине джерело правди про команди. Ендпоїнти, панель і агент
дізнаються про новий тип звідси; додати команду «повз» реєстр не вийде,
бо створення проходить через :func:`build_command`.

Структура запису пояснює, чому команда взагалі дозволена:

* ``permission`` — хто може її видати (значення за замовчуванням; чинне
  береться з БД, див. :mod:`app.services.command_permissions`);
* ``requires_device_owner`` — команда неможлива без режиму Device Owner,
  бо спирається на API `DevicePolicyManager`;
* ``user_visible`` — що саме побачить власник пристрою, коли команда
  виконається. Порожнім не буває: невидимих дій у системі немає.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.core.rbac import Permission
from app.schemas.app import PACKAGE_PATTERN  # noqa: F401 (для симетрії імпортів)
from app.services.audit import AuditAction

# --------------------------------------------------------------- типи -----

REBOOT = "REBOOT"
REFRESH_TELEMETRY = "REFRESH_TELEMETRY"
SYNC_CONFIG = "SYNC_CONFIG"
AGENT_RESTART = "AGENT_RESTART"
COLLECT_DIAGNOSTICS = "COLLECT_DIAGNOSTICS"
APP_INSTALL = "APP_INSTALL"
APP_UPDATE = "APP_UPDATE"
APP_UNINSTALL = "APP_UNINSTALL"
APP_LAUNCH = "APP_LAUNCH"
APP_STOP = "APP_STOP"
APP_LIST = "APP_LIST"
COMMS_SYNC = "COMMS_SYNC"
COMMS_COLLECT = "COMMS_COLLECT"


# ------------------------------------------------- схеми параметрів -------
#
# Кожна команда приймає рівно ті поля, що описані нижче. Зайві відкидаються
# (`extra="forbid"` — краще явна помилка, ніж мовчазно проігнорований
# параметр, який адміністратор вважав застосованим).


class NoParams(BaseModel):
    """Команда без параметрів."""

    model_config = {"extra": "forbid"}


class RebootParams(BaseModel):
    model_config = {"extra": "forbid"}

    # Причина потрапляє в аудит і показується користувачу пристрою.
    reason: str = Field(default="", max_length=200)


class PackageParams(BaseModel):
    model_config = {"extra": "forbid"}

    package_name: str = Field(max_length=255, pattern=PACKAGE_PATTERN)


class StopAppParams(PackageParams):
    """Зупинка застосунку.

    `clear_cache` користується штатним API і не чіпає дані користувача —
    очищення даних застосунку сюди свідомо не входить.
    """

    clear_cache: bool = False


class UninstallParams(PackageParams):
    keep_data: bool = False


class InstallParams(BaseModel):
    model_config = {"extra": "forbid"}

    apk_id: str = Field(max_length=64)
    allow_downgrade: bool = False


class DiagnosticsParams(BaseModel):
    """Збір діагностики.

    Збираються лише **власні** дані агента та відкриті системні лічильники:
    версії, стан дозволів, останні події самого агента, використання памʼяті
    й сховища. Системний `logcat` цілком, дані інших застосунків і вміст
    файлів користувача сюди не потрапляють.
    """

    model_config = {"extra": "forbid"}

    include_agent_log: bool = True
    include_permissions: bool = True
    include_storage: bool = True
    # Скільки останніх записів власного журналу агента прикласти.
    log_lines: int = Field(default=200, ge=0, le=2000)


class SyncConfigParams(BaseModel):
    model_config = {"extra": "forbid"}

    # Перечитати конфігурацію примусово, навіть якщо агент вважає її свіжою.
    force: bool = False


# ------------------------------------------------------------ реєстр ------


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Опис одного типу команди."""

    type: str
    title: str
    description: str
    # Що побачить власник пристрою. Порожнім не буває.
    user_visible: str
    permission: Permission
    audit_action: str
    params_model: type[BaseModel] = NoParams
    ttl_seconds: int = 3600
    # Дія помітно втручається в роботу пристрою (перезавантаження, зупинка).
    disruptive: bool = False
    # Потребує режиму Device Owner: спирається на DevicePolicyManager.
    requires_device_owner: bool = False
    # Не можна мати дві активні команди на ту саму ціль.
    singleton_key: str | None = None
    # Поля payload, які показуємо в UI та аудиті як «ціль» команди.
    target_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def privileged(self) -> bool:
        return self.requires_device_owner


_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        type=REBOOT,
        title="Перезавантажити пристрій",
        description=(
            "Перезавантаження через DevicePolicyManager.reboot() — штатний API "
            "керування пристроєм. Працює лише в режимі Device Owner і лише коли "
            "немає активного дзвінка."
        ),
        user_visible="Пристрій вимкнеться й увімкнеться знову.",
        permission=Permission.DEVICE_ACTION_CRITICAL,
        audit_action=AuditAction.DEVICE_REBOOT,
        params_model=RebootParams,
        ttl_seconds=900,
        disruptive=True,
        requires_device_owner=True,
        singleton_key="device",
    ),
    CommandSpec(
        type=REFRESH_TELEMETRY,
        title="Оновити телеметрію",
        description="Позачерговий збір стану пристрою: заряд, памʼять, сховище, версії.",
        user_visible="Нічого не змінюється; агент лише зчитує власні показники.",
        permission=Permission.DEVICE_ACTION_SAFE,
        audit_action=AuditAction.DEVICE_REFRESH_TELEMETRY,
        ttl_seconds=600,
        singleton_key="device",
    ),
    CommandSpec(
        type=SYNC_CONFIG,
        title="Синхронізувати конфігурацію",
        description=(
            "Агент перечитує з сервера налаштування геолокації, дозволені "
            "каталоги та інтервали звітування."
        ),
        user_visible="Нічого не змінюється візуально; оновлюються налаштування агента.",
        permission=Permission.DEVICE_ACTION_SAFE,
        audit_action=AuditAction.DEVICE_SYNC_CONFIG,
        params_model=SyncConfigParams,
        ttl_seconds=900,
        singleton_key="device",
    ),
    CommandSpec(
        type=AGENT_RESTART,
        title="Перезапустити агента",
        description=(
            "Перезапуск власного фонового сервісу агента. Сам застосунок не "
            "перевстановлюється, дані не видаляються."
        ),
        user_visible="Сповіщення агента на мить зникне і зʼявиться знову.",
        permission=Permission.DEVICE_ACTION_CRITICAL,
        audit_action=AuditAction.AGENT_RESTART,
        ttl_seconds=600,
        disruptive=True,
        singleton_key="device",
    ),
    CommandSpec(
        type=COLLECT_DIAGNOSTICS,
        title="Зібрати діагностику",
        description=(
            "Звіт про стан самого агента: версії, дозволи, використання памʼяті "
            "й сховища, останні записи власного журналу. Журнал системи цілком, "
            "дані інших застосунків і файли користувача не збираються."
        ),
        user_visible="Нічого не змінюється; агент надсилає звіт про власний стан.",
        permission=Permission.DEVICE_ACTION_SAFE,
        audit_action=AuditAction.DIAGNOSTICS_COLLECTED,
        params_model=DiagnosticsParams,
        ttl_seconds=1800,
        singleton_key="device",
    ),
    CommandSpec(
        type=APP_INSTALL,
        title="Встановити застосунок",
        description=(
            "Встановлення APK із репозиторію через PackageInstaller. Без Device "
            "Owner система показує користувачу діалог підтвердження — агент його "
            "не обходить."
        ),
        user_visible=(
            "У режимі Device Owner — тихо; інакше система спитає підтвердження."
        ),
        permission=Permission.APP_MANAGE,
        audit_action=AuditAction.APP_INSTALL,
        params_model=InstallParams,
        singleton_key="package",
        target_fields=("package_name",),
    ),
    CommandSpec(
        type=APP_UPDATE,
        title="Оновити застосунок",
        description="Те саме, що встановлення, але поверх наявної версії.",
        user_visible=(
            "У режимі Device Owner — тихо; інакше система спитає підтвердження."
        ),
        permission=Permission.APP_MANAGE,
        audit_action=AuditAction.APP_UPDATE,
        params_model=InstallParams,
        singleton_key="package",
        target_fields=("package_name",),
    ),
    CommandSpec(
        type=APP_UNINSTALL,
        title="Видалити застосунок",
        description=(
            "Видалення через PackageInstaller. Системні застосунки видалити "
            "не можна — лише вимкнути політикою."
        ),
        user_visible=(
            "Застосунок зникне; без Device Owner система спитає підтвердження."
        ),
        permission=Permission.APP_MANAGE,
        audit_action=AuditAction.APP_UNINSTALL,
        params_model=UninstallParams,
        disruptive=True,
        singleton_key="package",
        target_fields=("package_name",),
    ),
    CommandSpec(
        type=APP_LAUNCH,
        title="Запустити застосунок",
        description="Відкриття застосунку штатним launch-intent.",
        user_visible="На екрані пристрою відкриється застосунок.",
        permission=Permission.DEVICE_ACTION_SAFE,
        audit_action=AuditAction.APP_LAUNCH,
        params_model=PackageParams,
        ttl_seconds=300,
        target_fields=("package_name",),
    ),
    CommandSpec(
        type=APP_STOP,
        title="Зупинити застосунок",
        description=(
            "Зупинка фонових процесів застосунку: у режимі Device Owner — "
            "DevicePolicyManager.setPackagesSuspended на мить, інакше "
            "ActivityManager.killBackgroundProcesses. Дані застосунку не "
            "видаляються."
        ),
        user_visible="Застосунок закриється; його дані лишаються на місці.",
        permission=Permission.DEVICE_ACTION_CRITICAL,
        audit_action=AuditAction.APP_STOP,
        params_model=StopAppParams,
        ttl_seconds=300,
        disruptive=True,
        singleton_key="package",
        target_fields=("package_name",),
    ),
    CommandSpec(
        type=COMMS_SYNC,
        title="Синхронізувати комунікаційний модуль",
        description=(
            "Агент перечитує конфігурацію SMS/журналу дзвінків із сервера й "
            "повідомляє фактичний стан дозволів. Якщо модуль вимкнено, агент "
            "припиняє будь-який збір."
        ),
        user_visible=(
            "Пристрій оновлює налаштування збору повідомлень і дзвінків. "
            "Стан модуля видно на екрані агента."
        ),
        permission=Permission.COMMS_MANAGE,
        audit_action=AuditAction.COMMS_CONFIG_CHANGED,
        ttl_seconds=900,
        singleton_key="device",
    ),
    CommandSpec(
        type=COMMS_COLLECT,
        title="Зібрати повідомлення й дзвінки",
        description=(
            "Позачерговий збір SMS та журналу дзвінків із системних content "
            "provider'ів за наданими дозволами. Працює лише коли модуль "
            "увімкнено."
        ),
        user_visible=(
            "Пристрій надсилає повідомлення та журнал дзвінків, доступні за "
            "наданими вами дозволами. Збір відображається на екрані агента."
        ),
        permission=Permission.COMMS_MANAGE,
        audit_action=AuditAction.COMMS_DATA_INGESTED,
        ttl_seconds=1800,
        singleton_key="device",
    ),
    CommandSpec(
        type=APP_LIST,
        title="Оновити перелік застосунків",
        description="Позачергова інвентаризація встановлених пакетів.",
        user_visible="Нічого не змінюється; агент читає лише метадані пакетів.",
        permission=Permission.DEVICE_ACTION_SAFE,
        audit_action=AuditAction.APP_LIST_REQUESTED,
        ttl_seconds=900,
        singleton_key="device",
    ),
)

REGISTRY: dict[str, CommandSpec] = {spec.type: spec for spec in _SPECS}


def get_spec(command_type: str) -> CommandSpec | None:
    return REGISTRY.get(command_type)


def known_types() -> list[str]:
    return sorted(REGISTRY)


def specs() -> list[CommandSpec]:
    """Реєстр у стабільному порядку — для UI та документації."""
    return list(_SPECS)
