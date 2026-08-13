"""SQLAlchemy-моделі. Повний опис схеми — docs/DATABASE.md."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.security import as_utc, utcnow
from app.db.base import Base, JSONType, TZDateTime, UUIDType, enum_column

# --------------------------------------------------------------- enums ----

ROLE_VALUES = ("superadmin", "admin", "operator", "auditor")
DEVICE_STATUS_VALUES = ("pending", "active", "suspended", "retired")
MANAGEMENT_MODE_VALUES = ("device_owner", "profile_owner", "unmanaged")
COMMAND_STATUS_VALUES = (
    "pending", "dispatched", "acked", "succeeded", "failed", "expired", "cancelled",
)
# Закритий перелік команд. Довільного рядка для виконання на пристрої в
# системі не існує: усе, що можна надіслати, описано в app/services/
# command_registry.py, а типи поза цим переліком не проходять валідацію БД.
COMMAND_TYPE_VALUES = (
    "REFRESH_TELEMETRY", "SYNC_CONFIG", "REBOOT",
    "AGENT_RESTART", "COLLECT_DIAGNOSTICS",
    "APP_LIST", "APP_INSTALL", "APP_UPDATE", "APP_UNINSTALL", "APP_LAUNCH",
    "APP_STOP",
    "FILE_LIST", "FILE_UPLOAD", "FILE_DOWNLOAD", "FILE_RENAME", "FILE_DELETE",
    "FILE_MKDIR",
    "LOCATION_CONFIG", "LOCATION_REQUEST",
    "POLICY_APPLY", "KIOSK_SET",
    "REMOTE_SUPPORT_REQUEST", "REMOTE_SUPPORT_END",
    "COMMS_SYNC", "COMMS_COLLECT",
)
TRANSFER_DIRECTION_VALUES = ("upload", "download")
TRANSFER_STATUS_VALUES = ("pending", "in_progress", "completed", "failed")
PERMISSION_STATE_VALUES = ("unknown", "granted_fine", "granted_coarse", "denied", "restricted")
# Стан дозволу читання (READ_SMS / READ_CALL_LOG) на пристрої.
COMMS_PERMISSION_VALUES = ("unknown", "granted", "denied", "not_requested", "unavailable")
# Напрямок повідомлення / дзвінка.
COMMS_DIRECTION_VALUES = (
    "inbox", "sent", "draft", "outbox", "incoming", "outgoing", "missed", "other",
)
# Як координата потрапила на сервер (не плутати з provider: gps/fused/network).
LOCATION_SOURCE_VALUES = ("periodic", "on_demand", "offline_queue")
SESSION_STATE_VALUES = ("requested", "awaiting_consent", "active", "denied", "ended", "expired")
SESSION_TRANSPORT_VALUES = ("websocket", "webrtc")
POLICY_STATE_VALUES = ("pending", "applied", "failed")
ALERT_RULE_TYPE_VALUES = (
    "DEVICE_OFFLINE", "LOW_BATTERY", "LOW_STORAGE", "LOW_MEMORY", "HIGH_CPU",
    "OUTDATED_AGENT", "OUTDATED_ANDROID", "OUTDATED_APP",
    "LOCATION_REPORTING_STOPPED", "COMMAND_FAILURES", "ENROLLMENT_STATUS",
)
ALERT_SEVERITY_VALUES = ("info", "warning", "critical")
ALERT_STATE_VALUES = ("firing", "acknowledged", "resolved")
ACTOR_TYPE_VALUES = ("user", "device", "system")
AUDIT_RESULT_VALUES = ("success", "failure", "denied")


# ------------------------------------------------------- користувачі ------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(sa.String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    full_name: Mapped[str | None] = mapped_column(sa.String(200))
    role: Mapped[str] = mapped_column(
        enum_column(ROLE_VALUES, "role_enum"), nullable=False, default="operator"
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    failed_login_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # Access-токени, видані раніше цієї миті, більше не приймаються.
    # Оновлюється при виході, зміні пароля, ролі та деактивації —
    # саме це робить короткоживучий JWT негайно відкликуваним.
    sessions_valid_from: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow
    )
    password_changed_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_locked(self) -> bool:
        locked_until = as_utc(self.locked_until)
        return locked_until is not None and locked_until > utcnow()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    revoked_reason: Mapped[str | None] = mapped_column(sa.String(64))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    ip: Mapped[str | None] = mapped_column(sa.String(45))
    user_agent: Mapped[str | None] = mapped_column(sa.String(400))

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and as_utc(self.expires_at) > utcnow()


# --------------------------------------------------------- enrollment -----


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    label: Mapped[str | None] = mapped_column(sa.String(200))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    default_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("policies.id", ondelete="SET NULL")
    )
    max_uses: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    use_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    devices: Mapped[list[Device]] = relationship(back_populates="enrollment_token")

    @property
    def is_usable(self) -> bool:
        return (
            self.revoked_at is None
            and as_utc(self.expires_at) > utcnow()
            and self.use_count < self.max_uses
        )


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    enrollment_token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("enrollment_tokens.id", ondelete="SET NULL")
    )
    public_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    key_algorithm: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="EC_P256")
    key_fingerprint: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    # Ротація облікових даних пристрою.
    key_created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow
    )
    key_rotated_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    key_generation: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    # Токени пристрою, видані раніше — недійсні (виставляється при ротації
    # ключа, призупиненні та видаленні пристрою).
    tokens_valid_from: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow
    )
    android_id: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    serial_number: Mapped[str | None] = mapped_column(sa.String(64))
    status: Mapped[str] = mapped_column(
        enum_column(DEVICE_STATUS_VALUES, "device_status_enum"), nullable=False, default="active"
    )
    management_mode: Mapped[str] = mapped_column(
        enum_column(MANAGEMENT_MODE_VALUES, "management_mode_enum"),
        nullable=False,
        default="unmanaged",
    )
    agent_version: Mapped[str | None] = mapped_column(sa.String(32))
    enrolled_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZDateTime, index=True)
    last_ip: Mapped[str | None] = mapped_column(sa.String(45))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    enrollment_token: Mapped[EnrollmentToken | None] = relationship(back_populates="devices")
    telemetry: Mapped[DeviceTelemetry | None] = relationship(
        back_populates="device", cascade="all, delete-orphan", uselist=False
    )
    location_settings: Mapped[LocationSettings | None] = relationship(
        back_populates="device", cascade="all, delete-orphan", uselist=False
    )
    comms_settings: Mapped[CommsSettings | None] = relationship(
        back_populates="device", cascade="all, delete-orphan", uselist=False
    )
    key_history: Mapped[list[DeviceKeyHistory]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )

    @property
    def key_age_days(self) -> int:
        created = as_utc(self.key_created_at) or as_utc(self.enrolled_at)
        if created is None:
            return 0
        return (utcnow() - created).days


class DeviceKeyHistory(Base):
    """Попередні публічні ключі пристрою — для розслідувань після ротації.

    Приватної частини тут немає й ніколи не було: сервер бачить лише
    публічний ключ, який агент згенерував у Android Keystore.
    """

    __tablename__ = "device_key_history"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    public_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    key_algorithm: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="EC_P256")
    generation: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    reason: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="rotation")
    rotated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    retired_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    device: Mapped[Device] = relationship(back_populates="key_history")


# ------------------------------------------------------- інвентаризація ---


class DeviceTelemetry(Base):
    """Останній зріз стану пристрою (1:1)."""

    __tablename__ = "device_telemetry"

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    manufacturer: Mapped[str | None] = mapped_column(sa.String(100))
    model: Mapped[str | None] = mapped_column(sa.String(100))
    product: Mapped[str | None] = mapped_column(sa.String(100))
    android_version: Mapped[str | None] = mapped_column(sa.String(32))
    sdk_int: Mapped[int | None] = mapped_column(sa.Integer)
    security_patch: Mapped[str | None] = mapped_column(sa.String(32))
    build_id: Mapped[str | None] = mapped_column(sa.String(100))
    kernel_version: Mapped[str | None] = mapped_column(sa.String(255))
    uptime_seconds: Mapped[int | None] = mapped_column(sa.BigInteger)
    battery_level: Mapped[int | None] = mapped_column(sa.Integer)
    battery_status: Mapped[str | None] = mapped_column(sa.String(32))
    battery_health: Mapped[str | None] = mapped_column(sa.String(32))
    battery_temperature_c: Mapped[float | None] = mapped_column(sa.Float)
    charging: Mapped[bool | None] = mapped_column(sa.Boolean)
    ram_total_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    ram_available_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    storage_total_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    storage_free_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    cpu_cores: Mapped[int | None] = mapped_column(sa.Integer)
    cpu_load_percent: Mapped[float | None] = mapped_column(sa.Float)
    cpu_abi: Mapped[str | None] = mapped_column(sa.String(64))
    network_type: Mapped[str | None] = mapped_column(sa.String(32))
    network_operator: Mapped[str | None] = mapped_column(sa.String(64))
    wifi_ssid: Mapped[str | None] = mapped_column(sa.String(64))
    ip_address: Mapped[str | None] = mapped_column(sa.String(45))
    agent_version: Mapped[str | None] = mapped_column(sa.String(32))
    collected_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    device: Mapped[Device] = relationship(back_populates="telemetry")


class TelemetrySample(Base):
    """Історія телеметрії для графіків."""

    __tablename__ = "telemetry_samples"
    __table_args__ = (sa.Index("ix_telemetry_samples_device_time", "device_id", "collected_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    collected_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    battery_level: Mapped[int | None] = mapped_column(sa.Integer)
    charging: Mapped[bool | None] = mapped_column(sa.Boolean)
    uptime_seconds: Mapped[int | None] = mapped_column(sa.BigInteger)
    ram_available_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    storage_free_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    cpu_load_percent: Mapped[float | None] = mapped_column(sa.Float)
    network_type: Mapped[str | None] = mapped_column(sa.String(32))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType)


class DeviceApp(Base):
    __tablename__ = "device_apps"
    __table_args__ = (
        sa.UniqueConstraint("device_id", "package_name", name="uq_device_apps_device_package"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    app_name: Mapped[str | None] = mapped_column(sa.String(255))
    version_name: Mapped[str | None] = mapped_column(sa.String(64))
    version_code: Mapped[int | None] = mapped_column(sa.BigInteger)
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    installer_package: Mapped[str | None] = mapped_column(sa.String(255))
    first_installed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_updated_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


class ApkArtifact(Base):
    """APK, завантажений адміністратором для встановлення на пристрої."""

    __tablename__ = "apk_artifacts"
    __table_args__ = (
        sa.Index("ix_apk_artifacts_package_version", "package_name", "version_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    package_name: Mapped[str | None] = mapped_column(sa.String(255), index=True)
    version_name: Mapped[str | None] = mapped_column(sa.String(64))
    version_code: Mapped[int | None] = mapped_column(sa.BigInteger)
    label: Mapped[str | None] = mapped_column(sa.String(255))
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    signature_digest: Mapped[str | None] = mapped_column(sa.String(64))
    signature_schemes: Mapped[list[str] | None] = mapped_column(JSONType)
    min_sdk: Mapped[int | None] = mapped_column(sa.Integer)
    target_sdk: Mapped[int | None] = mapped_column(sa.Integer)
    compile_sdk: Mapped[int | None] = mapped_column(sa.Integer)
    abis: Mapped[list[str] | None] = mapped_column(JSONType)
    permissions: Mapped[list[str] | None] = mapped_column(JSONType)
    is_split: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    storage_path: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


# ------------------------------------------------------------- команди ----


class Command(Base):
    __tablename__ = "commands"
    __table_args__ = (sa.Index("ix_commands_device_status", "device_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(
        enum_column(COMMAND_TYPE_VALUES, "command_type_enum"), nullable=False
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(
        enum_column(COMMAND_STATUS_VALUES, "command_status_enum"), nullable=False, default="pending"
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(sa.Text)
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    progress_percent: Mapped[int | None] = mapped_column(sa.Integer)
    status_message: Mapped[str | None] = mapped_column(sa.String(255))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    acked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # Скільки пристрій виконував команду: від підтвердження (`acked_at`) до
    # результату. Рахується на сервері, бо годинник пристрою може розходитись.
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    # Скільки команда чекала, поки пристрій її забере.
    queue_ms: Mapped[int | None] = mapped_column(sa.Integer)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled", "expired")


class CommandPermission(Base):
    """Хто саме може видавати конкретний тип команди.

    Реєстр задає значення за замовчуванням; цей запис його перекриває. Так
    матрицю можна змінити без релізу — наприклад, дозволити операторам
    перезавантажувати пристрої або, навпаки, лишити встановлення застосунків
    тільки суперадміністратору.
    """

    __tablename__ = "command_permissions"
    __table_args__ = (sa.UniqueConstraint("command_type", name="uq_command_permissions_type"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    command_type: Mapped[str] = mapped_column(
        enum_column(COMMAND_TYPE_VALUES, "command_type_enum"), nullable=False
    )
    # Мінімальний дозвіл, потрібний для видачі команди цього типу.
    required_permission: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # Вимкнена команда не видається взагалі, навіть суперадміністратору.
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------- файли ----


class FileRoot(Base):
    """Явно дозволений каталог. Порожній перелік = файловий модуль вимкнено."""

    __tablename__ = "file_roots"
    __table_args__ = (
        sa.UniqueConstraint("device_id", "path", name="uq_file_roots_device_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    label: Mapped[str | None] = mapped_column(sa.String(200))
    writable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Ліміт на окремий каталог; None → діє глобальний MAX_FILE_TRANSFER_MB.
    max_file_size_mb: Mapped[int | None] = mapped_column(sa.Integer)
    # Привілейований доступ (su) дозволено лише для явно позначених каталогів
    # керування пристроєм — і лише на рутованих пристроях.
    allow_privileged: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class FileTransfer(Base):
    __tablename__ = "file_transfers"
    __table_args__ = (sa.Index("ix_file_transfers_status", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("commands.id", ondelete="SET NULL")
    )
    direction: Mapped[str] = mapped_column(
        enum_column(TRANSFER_DIRECTION_VALUES, "transfer_direction_enum"), nullable=False
    )
    remote_path: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    received_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    chunk_size: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1_048_576)
    sha256: Mapped[str | None] = mapped_column(sa.String(64))
    checksum_verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    root_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("file_roots.id", ondelete="SET NULL")
    )
    storage_path: Mapped[str | None] = mapped_column(sa.String(1024))
    status: Mapped[str] = mapped_column(
        enum_column(TRANSFER_STATUS_VALUES, "transfer_status_enum"),
        nullable=False,
        default="pending",
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)


# ---------------------------------------------------------- геолокація ----


class LocationSettings(Base):
    """Opt-in налаштування геолокації. За замовчуванням — вимкнено."""

    __tablename__ = "location_settings"

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    interval_seconds: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=900)
    min_distance_meters: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=50)
    high_accuracy: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    permission_state: Mapped[str] = mapped_column(
        enum_column(PERMISSION_STATE_VALUES, "permission_state_enum"),
        nullable=False,
        default="unknown",
    )
    background_permission: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    permission_reported_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_fix_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    consent_recorded_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    device: Mapped[Device] = relationship(back_populates="location_settings")


class CommsSettings(Base):
    """Комунікаційний модуль пристрою (SMS / журнал дзвінків).

    **Вимкнено за замовчуванням.** Модуль вмикає адміністратор явним рішенням,
    а на пристрої агент однаково показує це користувачу (постійне сповіщення
    й картка стану) — прихованого збору тут немає.

    Зберігаються лише **факт** конфігурації й час її зміни. Самі повідомлення
    та дзвінки живуть у окремих таблицях і лише в зашифрованому вигляді.
    """

    __tablename__ = "comms_settings"

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    # Головний вимикач модуля. False = агент нічого не читає й не надсилає.
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Окремі перемикачі: можна ввімкнути лише журнал дзвінків без SMS.
    sms_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    call_log_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Зберігати лише метадані (номер, час, тривалість), без тексту SMS.
    metadata_only: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    # Точний час, коли модуль востаннє УВІМКНУЛИ (вимога ТЗ).
    enabled_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    enabled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    # Коли вимкнули (для історії та ретеншну).
    disabled_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    # Ретеншн саме для цього пристрою (у межах серверної стелі).
    retention_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=30)

    # Фактичний стан дозволів на пристрої — агент повідомляє, сервер не гадає.
    sms_permission: Mapped[str] = mapped_column(
        enum_column(COMMS_PERMISSION_VALUES, "comms_permission_enum"),
        nullable=False, default="unknown",
    )
    call_log_permission: Mapped[str] = mapped_column(
        enum_column(COMMS_PERMISSION_VALUES, "comms_permission_enum"),
        nullable=False, default="unknown",
    )
    permission_reported_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_sync_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    device: Mapped[Device] = relationship(back_populates="comms_settings")


class SmsMessage(Base):
    """Запис SMS. Номер і текст зберігаються **лише зашифрованими**.

    Джерело — системний content provider `Telephony.Sms`, доступний за
    рантайм-дозволом READ_SMS. Це повідомлення, які бачить сам застосунок
    «Повідомлення»; перехоплення чужих застосунків тут немає й бути не може.
    """

    __tablename__ = "comms_sms"
    __table_args__ = (
        sa.Index("ix_comms_sms_device_time", "device_id", "sent_at"),
        sa.UniqueConstraint("device_id", "provider_id", name="uq_comms_sms_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    # _id рядка у content provider — щоб не дублювати те саме повідомлення.
    provider_id: Mapped[str | None] = mapped_column(sa.String(64))
    direction: Mapped[str] = mapped_column(
        enum_column(COMMS_DIRECTION_VALUES, "comms_direction_enum"), nullable=False
    )
    # Зашифровані AES-256-GCM. Відкритого тексту в БД немає.
    address_encrypted: Mapped[str | None] = mapped_column(sa.Text)
    body_encrypted: Mapped[str | None] = mapped_column(sa.Text)
    # Довжина тексту зберігається відкрито — для UI без розшифрування.
    body_length: Mapped[int | None] = mapped_column(sa.Integer)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    collected_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


class CallLogEntry(Base):
    """Запис журналу дзвінків. Номер зберігається **лише зашифрованим**.

    Джерело — системний content provider `CallLog.Calls` за рантайм-дозволом
    READ_CALL_LOG. Записуються метадані дзвінка (номер, час, тривалість);
    запису розмови немає — Android такого API застосунку не надає.
    """

    __tablename__ = "comms_calls"
    __table_args__ = (
        sa.Index("ix_comms_calls_device_time", "device_id", "occurred_at"),
        sa.UniqueConstraint("device_id", "provider_id", name="uq_comms_calls_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[str | None] = mapped_column(sa.String(64))
    direction: Mapped[str] = mapped_column(
        enum_column(COMMS_DIRECTION_VALUES, "comms_direction_enum"), nullable=False
    )
    number_encrypted: Mapped[str | None] = mapped_column(sa.Text)
    # Контактне ім'я, якщо система його знає (теж зашифроване).
    contact_encrypted: Mapped[str | None] = mapped_column(sa.Text)
    duration_seconds: Mapped[int | None] = mapped_column(sa.Integer)
    occurred_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    collected_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


class LocationFix(Base):
    __tablename__ = "location_fixes"
    __table_args__ = (
        sa.Index("ix_location_fixes_device_time", "device_id", "recorded_at"),
        sa.Index("ix_location_fixes_received_at", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    latitude: Mapped[float] = mapped_column(sa.Float, nullable=False)
    longitude: Mapped[float] = mapped_column(sa.Float, nullable=False)
    accuracy_m: Mapped[float | None] = mapped_column(sa.Float)
    altitude_m: Mapped[float | None] = mapped_column(sa.Float)
    speed_mps: Mapped[float | None] = mapped_column(sa.Float)
    bearing_deg: Mapped[float | None] = mapped_column(sa.Float)
    provider: Mapped[str | None] = mapped_column(sa.String(32))
    source: Mapped[str] = mapped_column(
        enum_column(LOCATION_SOURCE_VALUES, "location_source_enum"),
        nullable=False,
        default="periodic",
    )
    is_mock: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    battery_level: Mapped[int | None] = mapped_column(sa.Integer)
    recorded_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


# --------------------------------------------------- віддалена підтримка --


class RemoteSession(Base):
    __tablename__ = "remote_sessions"
    __table_args__ = (sa.Index("ix_remote_sessions_state", "state"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(
        enum_column(SESSION_STATE_VALUES, "session_state_enum"), nullable=False, default="requested"
    )
    transport: Mapped[str] = mapped_column(
        enum_column(SESSION_TRANSPORT_VALUES, "session_transport_enum"),
        nullable=False,
        default="websocket",
    )
    join_secret_hash: Mapped[str | None] = mapped_column(sa.String(64))
    reason: Mapped[str | None] = mapped_column(sa.String(500))
    # Віддалене керування дозволяється окремо і працює лише там, де його
    # підтримує Android (увімкнена служба доступності).
    input_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    input_available: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    consent_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    end_reason: Mapped[str | None] = mapped_column(sa.String(64))
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_frame_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    frames_sent: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    bytes_sent: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    # Роздільність кадру, яку повідомив агент. Потрібна панелі, щоб
    # перерахувати клік по зображенню в координати пікселів пристрою.
    frame_width: Mapped[int | None] = mapped_column(sa.Integer)
    frame_height: Mapped[int | None] = mapped_column(sa.Integer)
    # Скільки дій керування надіслано за сеанс — у звіті й в аудиті.
    input_events: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    # Скільки разів оператор приєднувався до перегляду (перепідключення теж).
    viewer_joins: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_viewer_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    @property
    def is_open_state(self) -> bool:
        """Сеанс іще живий: запитаний, чекає згоди або триває."""
        return self.state in ("requested", "awaiting_consent", "active")


# ------------------------------------------------------------ політики ----


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    is_default: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class DevicePolicy(Base):
    __tablename__ = "device_policies"
    __table_args__ = (
        sa.UniqueConstraint("device_id", "policy_id", name="uq_device_policies_device_policy"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("policies.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        enum_column(POLICY_STATE_VALUES, "policy_state_enum"), nullable=False, default="pending"
    )
    applied_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


# ------------------------------------------------- моніторинг і алерти ----


class AlertRule(Base):
    """Правило моніторингу з налаштовуваними порогами."""

    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    type: Mapped[str] = mapped_column(
        enum_column(ALERT_RULE_TYPE_VALUES, "alert_rule_type_enum"), nullable=False
    )
    severity: Mapped[str] = mapped_column(
        enum_column(ALERT_SEVERITY_VALUES, "alert_severity_enum"),
        nullable=False,
        default="warning",
    )
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    # Порогові значення залежать від типу правила (див. docs/MONITORING.md).
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    # None → правило діє на всі пристрої.
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    cooldown_seconds: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=3600)
    description: Mapped[str | None] = mapped_column(sa.Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class Alert(Base):
    """Спрацювання правила — історія сповіщень."""

    __tablename__ = "alerts"
    __table_args__ = (
        sa.Index("ix_alerts_device_state", "device_id", "state"),
        sa.Index("ix_alerts_first_seen", "first_seen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("alert_rules.id", ondelete="SET NULL")
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(
        enum_column(ALERT_RULE_TYPE_VALUES, "alert_rule_type_enum"), nullable=False
    )
    severity: Mapped[str] = mapped_column(
        enum_column(ALERT_SEVERITY_VALUES, "alert_severity_enum"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        enum_column(ALERT_STATE_VALUES, "alert_state_enum"), nullable=False, default="firing"
    )
    # Ключ дедуплікації: для OUTDATED_APP — ще й імʼя пакета.
    dedup_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    message: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    acknowledged_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)


# --------------------------------------------------------------- аудит ----


class AuditLog(Base):
    """Append-only. У production: REVOKE UPDATE, DELETE для ролі застосунку."""

    __tablename__ = "audit_log"
    __table_args__ = (
        sa.Index("ix_audit_log_occurred_at", "occurred_at"),
        sa.Index("ix_audit_log_action_time", "action", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    actor_type: Mapped[str] = mapped_column(
        enum_column(ACTOR_TYPE_VALUES, "actor_type_enum"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, sa.ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    actor_label: Mapped[str | None] = mapped_column(sa.String(200))
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    object_type: Mapped[str | None] = mapped_column(sa.String(64))
    object_id: Mapped[str | None] = mapped_column(sa.String(64))
    result: Mapped[str] = mapped_column(
        enum_column(AUDIT_RESULT_VALUES, "audit_result_enum"), nullable=False, default="success"
    )
    ip: Mapped[str | None] = mapped_column(sa.String(45))
    user_agent: Mapped[str | None] = mapped_column(sa.String(400))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
