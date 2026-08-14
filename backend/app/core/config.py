"""Конфігурація застосунку (лише з оточення — жодних секретів у коді)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Значення з .env.example — у production сервер з ними не стартує.
INSECURE_DEFAULTS = {
    "change-me-jwt-secret-at-least-32-characters-long",
    "change-me-token-hash-secret-at-least-32-chars",
    "change-me-admin-password",
    "change-me-postgres",
    "change-me-postgres-app",
    "change-me-redis",
}

Environment = Literal["development", "staging", "production", "test"]


def _env_files() -> tuple[str, ...] | None:
    """Які .env-файли читати.

    Тести мають бачити рівно те оточення, яке самі задали: локальний
    `backend/.env` розробника (наприклад, згенерований `start-backend.bat`)
    не повинен мовчки змінювати їхню поведінку. Одного разу через це
    `RATE_LIMIT_ENABLED=false` з дев-конфігу вимкнув перевірки лімітів у
    тестах — і вони «зеленіли» на непрацюючому контролі.
    """
    if os.getenv("FLEET_DISABLE_DOTENV") == "1":
        return None
    return (".env", "../.env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- загальні --------------------------------------------------------
    app_env: Environment = "development"
    app_name: str = "Fleet MDM"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    public_base_url: str = "http://localhost:8000"

    # --- база даних ------------------------------------------------------
    database_url: str | None = None
    postgres_user: str = "fleet"
    postgres_password: str = "fleet"
    postgres_db: str = "fleet"
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # --- redis -----------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"

    # --- криптографія ----------------------------------------------------
    jwt_secret: str = "change-me-jwt-secret-at-least-32-characters-long"
    jwt_algorithm: str = "HS256"
    token_hash_secret: str = "change-me-token-hash-secret-at-least-32-chars"
    # Попередні значення на час ротації: підписуємо новим, приймаємо і старе.
    # Після того як усі старі токени протухли — прибрати зі змінних оточення.
    jwt_secret_previous: str = ""
    token_hash_secret_previous: str = ""
    # Позначка «усі токени, видані раніше, недійсні» (ISO-8601 або порожньо).
    token_epoch: str = ""
    jwt_issuer: str = "fleet-mdm"
    jwt_audience: str = "fleet-api"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 1_209_600
    device_token_ttl_seconds: int = 3600
    enrollment_token_ttl_seconds: int = 86_400
    device_challenge_ttl_seconds: int = 120
    min_password_length: int = 12

    # --- cookie та CSRF --------------------------------------------------
    # Refresh-токен живе у HttpOnly-cookie: XSS не може його прочитати.
    refresh_cookie_name: str = "fleet_refresh"
    csrf_cookie_name: str = "fleet_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    cookie_path: str = "/api/v1/auth"
    cookie_domain: str | None = None
    # SameSite=strict: cookie не летить із чужого сайту навіть при навігації.
    cookie_samesite: Literal["strict", "lax", "none"] = "strict"
    # Secure вмикається автоматично поза development; можна форсувати.
    cookie_secure: bool | None = None

    # --- захист від брутфорсу -------------------------------------------
    max_failed_logins: int = 10
    account_lock_seconds: int = 900

    # --- перший адміністратор -------------------------------------------
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    bootstrap_admin_name: str = "Administrator"

    # --- CORS ------------------------------------------------------------
    cors_origins: str = "http://localhost:5173"
    trusted_proxies: str = ""
    # Host-заголовки, які приймає застосунок (порожньо = приймати всі).
    # У production обовʼязково перелічити реальні домени: це закриває
    # підміну Host і отруєння посилань, що будуються з нього.
    allowed_hosts: str = ""

    # --- rate limiting ---------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_login: str = "10/minute"
    rate_limit_enroll: str = "20/hour"
    rate_limit_device_auth: str = "60/minute"
    rate_limit_default: str = "300/minute"
    rate_limit_refresh: str = "60/minute"
    rate_limit_password: str = "5/minute"
    # Дії, що змінюють стан (створення команд, файлові операції, користувачі).
    rate_limit_mutation: str = "120/minute"
    # Вивантаження великих файлів/APK.
    rate_limit_upload: str = "30/hour"
    # Телеметрія та геолокація від агентів — часті, але не безмежні.
    rate_limit_device_report: str = "120/minute"
    # Ротація ключа пристрою — рідкісна операція.
    rate_limit_key_rotation: str = "5/hour"

    # --- сховище ---------------------------------------------------------
    storage_root: str = "/var/lib/fleet/storage"
    max_upload_mb: int = 512
    # Дозволені сертифікати підпису APK (SHA-256 від META-INF/*.RSA, через кому).
    # Порожньо = приймати будь-який підписаний пакет. Заповніть, якщо в парк
    # мають потрапляти лише застосунки вашого власного ключа збірки.
    apk_allowed_signers: str = ""
    # Окремий ліміт для файлів пристроїв (не APK).
    max_file_transfer_mb: int = 256
    file_transfer_chunk_kb: int = 1024
    file_transfer_ttl_seconds: int = 86_400
    # Стеля для JSON/форм-запитів (вивантаження файлів має власні ліміти).
    max_json_body_kb: int = 512

    # --- комунікаційний модуль (SMS / журнал дзвінків) ------------------
    # Вимкнено на рівні всього сервера, доки ключ шифрування не заданий.
    # Навіть увімкнувши модуль для пристрою, без цього ключа зберегти дані
    # неможливо: чутливий вміст лягає в БД лише зашифрованим (AES-256-GCM).
    comms_encryption_key: str = ""
    # Стеля ретеншну: адміністратор може задати менше, але не більше.
    comms_retention_max_days: int = 90
    comms_retention_default_days: int = 30
    # Скільки записів приймати за один звіт агента.
    comms_max_batch: int = 2000

    # --- ретеншн / статуси ----------------------------------------------
    device_offline_after_seconds: int = 180
    telemetry_history_days: int = 30
    location_history_days: int = 90
    audit_retention_days: int = 365
    comms_history_days: int = 30

    # --- ротація облікових даних ------------------------------------------
    # Через скільки днів ключ пристрою вважається застарілим і агент має
    # його перевипустити. 0 = ротація за розкладом вимкнена.
    device_key_max_age_days: int = 180
    # Скільки нових ключів дозволено на пристрій за добу (антизлам).
    device_key_rotation_daily_limit: int = 3
    # Скільки днів зберігається попередній ключ для аудиту.
    device_key_history_days: int = 90

    # --- похідні ---------------------------------------------------------
    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [p.strip() for p in self.trusted_proxies.split(",") if p.strip()]

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def comms_encryption_configured(self) -> bool:
        key = self.comms_encryption_key
        return bool(key) and key != "change-me-comms-encryption-key-at-least-32-chars" \
            and len(key) >= 32

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_url.startswith("sqlite")

    @property
    def apk_allowed_signer_list(self) -> list[str]:
        return [v.strip() for v in self.apk_allowed_signers.split(",") if v.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_file_transfer_bytes(self) -> int:
        return self.max_file_transfer_mb * 1024 * 1024

    @property
    def max_json_body_bytes(self) -> int:
        return self.max_json_body_kb * 1024

    @property
    def use_secure_cookies(self) -> bool:
        """Secure-cookie скрізь, окрім локальної розробки та тестів.

        `development`/`test` ходять по http, і прапорець Secure просто не
        дав би браузеру (чи тестовому клієнту) надіслати cookie назад.
        У staging і production він завжди увімкнений.
        """
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_env not in ("development", "test")

    @property
    def jwt_decode_secrets(self) -> list[str]:
        """Ключі перевірки підпису: спершу поточний, далі попередній."""
        secrets_list = [self.jwt_secret]
        if self.jwt_secret_previous and self.jwt_secret_previous != self.jwt_secret:
            secrets_list.append(self.jwt_secret_previous)
        return secrets_list

    @property
    def token_hash_secrets(self) -> list[str]:
        """Ключі HMAC для звірки збережених хешів (поточний + попередній)."""
        secrets_list = [self.token_hash_secret]
        if (
            self.token_hash_secret_previous
            and self.token_hash_secret_previous != self.token_hash_secret
        ):
            secrets_list.append(self.token_hash_secret_previous)
        return secrets_list

    @property
    def token_epoch_at(self) -> datetime | None:
        """Токени, видані до цієї миті, вважаються анульованими."""
        if not self.token_epoch:
            return None
        try:
            parsed = datetime.fromisoformat(self.token_epoch)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @property
    def content_security_policy(self) -> str:
        """CSP панелі: без inline-скриптів, без зовнішніх джерел."""
        connect = "'self'" + ("" if self.is_production else " ws: wss: http: https:")
        return "; ".join(
            [
                "default-src 'self'",
                "base-uri 'none'",
                "object-src 'none'",
                "frame-ancestors 'none'",
                "form-action 'self'",
                "script-src 'self'",
                # Стилі збираються Vite у окремий файл; inline лишаємо тільки
                # для стилів, які React проставляє атрибутом style=.
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data: blob:",
                "font-src 'self' data:",
                f"connect-src {connect}",
                "media-src 'self' blob:",
                "worker-src 'self' blob:",
                "manifest-src 'self'",
                "upgrade-insecure-requests" if self.is_production else "",
            ]
        ).strip("; ").replace("; ;", ";")

    # --- перевірки безпеки ----------------------------------------------
    @model_validator(mode="after")
    def _validate_secrets(self) -> Settings:
        if not self.is_production:
            return self
        problems: list[str] = []
        for name in ("jwt_secret", "token_hash_secret"):
            value = getattr(self, name)
            if value in INSECURE_DEFAULTS:
                problems.append(f"{name.upper()} має дефолтне значення")
            elif len(value) < 32:
                problems.append(f"{name.upper()} коротший за 32 символи")
        if self.bootstrap_admin_password in INSECURE_DEFAULTS:
            problems.append("BOOTSTRAP_ADMIN_PASSWORD має дефолтне значення")
        if self.public_base_url.startswith("http://"):
            problems.append("PUBLIC_BASE_URL має використовувати https:// у production")
        if self.jwt_secret == self.token_hash_secret:
            problems.append("JWT_SECRET і TOKEN_HASH_SECRET мають відрізнятися")
        if self.comms_encryption_key and not self.comms_encryption_configured:
            problems.append(
                "COMMS_ENCRYPTION_KEY заданий, але дефолтний або коротший за 32 символи"
            )
        if self.comms_encryption_key and self.comms_encryption_key in (
            self.jwt_secret,
            self.token_hash_secret,
        ):
            problems.append("COMMS_ENCRYPTION_KEY має відрізнятися від інших секретів")
        for name in ("jwt_secret_previous", "token_hash_secret_previous"):
            value = getattr(self, name)
            if value and (value in INSECURE_DEFAULTS or len(value) < 32):
                problems.append(f"{name.upper()} некоректний (дефолт або коротший за 32)")
        if self.cookie_secure is False:
            problems.append("COOKIE_SECURE=false неприпустимо у production")
        if self.cookie_samesite == "none" and not self.use_secure_cookies:
            problems.append("SameSite=none потребує Secure-cookie")
        if not self.allowed_host_list:
            problems.append("ALLOWED_HOSTS має бути заданий у production")
        if "*" in self.allowed_host_list:
            problems.append("ALLOWED_HOSTS не може містити '*' у production")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS не може містити '*' у production")
        for origin in self.cors_origin_list:
            if origin.startswith("http://"):
                problems.append(f"CORS-джерело {origin} має бути https:// у production")
        if "@" not in self.redis_url:
            problems.append("REDIS_URL має містити пароль (redis://:пароль@host)")
        elif any(default in self.redis_url for default in INSECURE_DEFAULTS):
            problems.append("REDIS_URL містить дефолтний пароль")
        if any(default in self.sqlalchemy_url for default in INSECURE_DEFAULTS):
            problems.append("Підключення до БД використовує дефолтний пароль")
        if problems:
            raise ValueError(
                "Небезпечна конфігурація для APP_ENV=production: " + "; ".join(problems)
            )
        return self


_WINDOWS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400}


def parse_rate_limit(spec: str) -> tuple[int, int]:
    """`"10/minute"` → `(10, 60)`."""
    try:
        raw_limit, raw_window = spec.split("/", 1)
        limit = int(raw_limit.strip())
    except (ValueError, AttributeError) as exc:  # pragma: no cover — конфігураційна помилка
        raise ValueError(f"Некоректний формат ліміту: {spec!r}") from exc
    window = raw_window.strip().lower().rstrip("s")
    if window not in _WINDOWS:
        raise ValueError(f"Невідоме вікно ліміту: {raw_window!r}")
    if limit < 1:
        raise ValueError("Ліміт має бути ≥ 1")
    return limit, _WINDOWS[window]


@lru_cache
def get_settings() -> Settings:
    return Settings()
