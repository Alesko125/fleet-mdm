# Схема бази даних

PostgreSQL 16. Міграції — Alembic (`backend/alembic/versions/`).
Усі первинні ключі — `UUID` (генеруються застосунком, `uuid4`), усі часові
позначки — `TIMESTAMP WITH TIME ZONE` у UTC.

Перелічення (enum) реалізовані як `VARCHAR` + `CHECK`-обмеження
(`native_enum=False`), щоб міграції не залежали від типів PostgreSQL і щоб
тести могли працювати на SQLite.

---

## Огляд зв'язків

```
users ─┬─< refresh_tokens
       ├─< enrollment_tokens ─< devices
       ├─< policies ─< device_policies >─ devices
       └─< audit_log (actor_user_id)

devices ─┬─1:1─ device_telemetry
         ├─< telemetry_samples
         ├─< device_apps
         ├─< commands ─< file_transfers
         ├─< file_roots
         ├─1:1─ location_settings
         ├─< location_fixes
         ├─< remote_sessions
         └─< audit_log (actor_device_id / object_id)

apk_artifacts (незалежна, посилається з commands.payload)
```

---

## 1. Оператори та сесії

### `users`
| Колонка | Тип | Примітки |
|---|---|---|
| `id` | uuid PK | |
| `email` | varchar(320) UNIQUE | нормалізується до нижнього регістру |
| `password_hash` | text | Argon2id |
| `full_name` | varchar(200) NULL | |
| `role` | varchar(16) | `superadmin` \| `admin` \| `operator` \| `auditor` |
| `is_active` | bool | миттєве блокування доступу |
| `failed_login_count` | int | скидається після успішного входу |
| `locked_until` | timestamptz NULL | тимчасове блокування після брутфорсу |
| `last_login_at` | timestamptz NULL | |
| `created_at` / `updated_at` | timestamptz | |

### `refresh_tokens`
Ротація з виявленням повторного використання.

| Колонка | Тип | Примітки |
|---|---|---|
| `id` | uuid PK | = `jti` токена |
| `user_id` | uuid FK users ON DELETE CASCADE | |
| `family_id` | uuid IDX | одна «родина» = один ланцюг ротацій |
| `token_hash` | varchar(64) UNIQUE | HMAC-SHA256(secret, token) |
| `issued_at` / `expires_at` | timestamptz | |
| `revoked_at` | timestamptz NULL | |
| `revoked_reason` | varchar(64) NULL | `rotated` \| `logout` \| `reuse_detected` \| `admin` |
| `replaced_by_id` | uuid NULL FK self | ланцюжок ротації |
| `ip` / `user_agent` | varchar | контекст видачі |

---

## 2. Enrollment і пристрої

### `enrollment_tokens`
| Колонка | Тип | Примітки |
|---|---|---|
| `id` | uuid PK | |
| `token_hash` | varchar(64) UNIQUE | відкритий токен не зберігається |
| `token_prefix` | varchar(16) | для відображення в UI (`FLEET-A3F9…`) |
| `label` | varchar(200) NULL | «Склад №2, планшети» |
| `created_by_id` | uuid NULL FK users ON DELETE SET NULL | |
| `max_uses` / `use_count` | int | за замовчуванням 1 |
| `expires_at` | timestamptz | |
| `revoked_at` | timestamptz NULL | |
| `default_policy_id` | uuid NULL FK policies | політика, що застосується після реєстрації |
| `notes` | text NULL | |
| `created_at` | timestamptz | |

### `devices`
| Колонка | Тип | Примітки |
|---|---|---|
| `id` | uuid PK | `sub` у device-JWT |
| `name` | varchar(200) | редаговане ім'я в панелі |
| `enrollment_token_id` | uuid NULL FK | звідки прийшов пристрій |
| `public_key` | text | SPKI/DER у base64 |
| `key_algorithm` | varchar(32) | `EC_P256` |
| `key_fingerprint` | varchar(64) UNIQUE | SHA-256 від SPKI |
| `android_id` | varchar(64) NULL UNIQUE | ANDROID_ID (для виявлення дублів) |
| `serial_number` | varchar(64) NULL | лише коли доступний Device Owner |
| `status` | varchar(16) | `pending` \| `active` \| `suspended` \| `retired` |
| `management_mode` | varchar(16) | `device_owner` \| `profile_owner` \| `unmanaged` |
| `agent_version` | varchar(32) NULL | |
| `enrolled_at` | timestamptz | |
| `last_seen_at` | timestamptz NULL IDX | основа онлайн/офлайн статусу |
| `last_ip` | varchar(45) NULL | |
| `deleted_at` | timestamptz NULL | м'яке видалення (аудит зберігає слід) |
| `created_at` / `updated_at` | timestamptz | |

---

## 3. Інвентаризація

### `device_telemetry` (останній зріз, 1:1)
`device_id` (PK, FK CASCADE), `manufacturer`, `model`, `product`,
`android_version`, `sdk_int`, `security_patch`, `build_id`, `kernel_version`,
`uptime_seconds` (bigint), `battery_level` (0–100), `battery_status`,
`battery_health`, `charging` (bool), `battery_temperature_c` (float),
`ram_total_bytes`, `ram_available_bytes`, `storage_total_bytes`,
`storage_free_bytes` (bigint), `network_type` (`wifi`/`cellular`/`ethernet`/`none`),
`network_operator`, `wifi_ssid`, `ip_address`, `agent_version`,
`collected_at`, `updated_at`, `extra` (JSONB).

### `telemetry_samples` (історія)
`id`, `device_id` FK CASCADE, `collected_at`, `battery_level`, `charging`,
`uptime_seconds`, `ram_available_bytes`, `storage_free_bytes`, `network_type`,
`payload` (JSONB). Індекс `(device_id, collected_at DESC)`.
Ретеншн — `TELEMETRY_HISTORY_DAYS`.

### `device_apps`
`id`, `device_id` FK CASCADE, `package_name`, `app_name`, `version_name`,
`version_code` (bigint), `is_system`, `is_enabled`, `installer_package`,
`first_installed_at`, `last_updated_at`, `last_seen_at`.
UNIQUE `(device_id, package_name)`.

### `apk_artifacts`
`id`, `filename`, `package_name`, `version_name`, `version_code`,
`size_bytes`, `sha256` UNIQUE, `signature_digest`, `storage_path`,
`uploaded_by_id` FK users SET NULL, `notes`, `created_at`.

---

## 4. Команди

### `commands`
| Колонка | Тип | Примітки |
|---|---|---|
| `id` | uuid PK | |
| `device_id` | uuid FK CASCADE | |
| `type` | varchar(32) | див. перелік нижче |
| `payload` | JSONB | параметри |
| `status` | varchar(16) | `pending` → `dispatched` → `acked` → `succeeded`/`failed`; також `expired`, `cancelled` |
| `result` | JSONB NULL | відповідь агента |
| `error` | text NULL | |
| `idempotency_key` | varchar(64) NULL UNIQUE | захист від подвійного виконання |
| `attempts` | int | лічильник доставок |
| `created_by_id` | uuid NULL FK users | |
| `created_at`, `dispatched_at`, `acked_at`, `completed_at`, `expires_at` | timestamptz | |

Типи: `REFRESH_TELEMETRY`, `SYNC_CONFIG`, `REBOOT`, `APP_LIST`, `APP_INSTALL`,
`APP_UPDATE`, `APP_UNINSTALL`, `APP_LAUNCH`, `FILE_LIST`, `FILE_UPLOAD`,
`FILE_DOWNLOAD`, `FILE_RENAME`, `FILE_DELETE`, `LOCATION_CONFIG`,
`LOCATION_REQUEST`, `POLICY_APPLY`, `KIOSK_SET`, `REMOTE_SUPPORT_REQUEST`,
`REMOTE_SUPPORT_END`.

---

## 5. Файли (лише allowlist)

### `file_roots`
Явно налаштовані каталоги, поза якими файловий модуль не працює.

`id`, `device_id` FK NULL (NULL = глобальний шаблон для нових пристроїв),
`path` (varchar 1024, абсолютний, нормалізований), `label`, `writable` (bool),
`created_by_id`, `created_at`, `updated_at`. UNIQUE `(device_id, path)`.

> Порожній набір `file_roots` для пристрою = файловий модуль вимкнено.

### `file_transfers`
`id`, `device_id`, `command_id` FK NULL, `direction` (`upload`/`download`),
`remote_path`, `filename`, `size_bytes`, `sha256`, `storage_path`,
`status` (`pending`/`in_progress`/`completed`/`failed`), `error`,
`created_by_id`, `created_at`, `completed_at`.

---

## 6. Геолокація (opt-in)

### `location_settings` (1:1 з пристроєм)
`device_id` PK FK CASCADE, `enabled` (bool, **default false**),
`interval_seconds` (default 900, межі 60…86400), `min_distance_meters`
(default 50), `high_accuracy` (bool), `permission_state`
(`unknown`/`granted_fine`/`granted_coarse`/`denied`/`restricted`),
`background_permission` (bool), `consent_recorded_at`, `updated_by_id`,
`updated_at`.

Будь-яка зміна цих полів пише запис `LOCATION_CONFIG_CHANGED` в `audit_log`
з попереднім і новим значеннями.

### `location_fixes`
`id`, `device_id` FK CASCADE, `latitude`, `longitude` (double),
`accuracy_m`, `altitude_m`, `speed_mps`, `bearing_deg` (float NULL),
`provider` (`gps`/`fused`/`network`), `battery_level`, `recorded_at`,
`received_at`. Індекс `(device_id, recorded_at DESC)`.
Ретеншн — `LOCATION_HISTORY_DAYS`.

---

## 7. Віддалена підтримка

### `remote_sessions`
`id`, `device_id` FK CASCADE, `requested_by_id` FK users,
`state` (`requested` → `awaiting_consent` → `active` → `ended`;
також `denied`, `expired`), `transport` (`websocket`/`webrtc`),
`join_secret_hash` (varchar 64 — одноразовий секрет приєднання),
`consent_at`, `started_at`, `ended_at`, `end_reason`,
`frames_sent`, `bytes_sent` (bigint), `created_at`.

Сеанс не може перейти в `active` без `consent_at` — це перевіряється і в
БД-обмеженні, і в сервісному шарі.

---

## 8. Політики

### `policies`
`id`, `name` UNIQUE, `description`, `payload` (JSONB: kiosk, обмеження
DevicePolicyManager), `is_default` (bool), `created_by_id`, `created_at`,
`updated_at`.

### `device_policies`
`id`, `device_id`, `policy_id`, `state` (`pending`/`applied`/`failed`),
`applied_at`, `error`, `created_at`. UNIQUE `(device_id, policy_id)`.

---

## 9. Аудит

### `audit_log` (append-only)
| Колонка | Тип | Примітки |
|---|---|---|
| `id` | uuid PK | |
| `occurred_at` | timestamptz IDX | |
| `actor_type` | varchar(16) | `user` \| `device` \| `system` |
| `actor_user_id` | uuid NULL FK users SET NULL | |
| `actor_device_id` | uuid NULL FK devices SET NULL | |
| `actor_label` | varchar(200) | збережений e-mail/ім'я на момент дії |
| `action` | varchar(64) IDX | див. SECURITY.md §8 |
| `object_type` / `object_id` | varchar(64) NULL | |
| `result` | varchar(16) | `success` \| `failure` \| `denied` |
| `ip` / `user_agent` | varchar | |
| `details` | JSONB | без секретів; логер маскує чутливі ключі |

У production рекомендовано:

```sql
REVOKE UPDATE, DELETE ON audit_log FROM fleet_app;
```

---

## Міграції

```bash
# створити нову ревізію після зміни моделей
docker compose exec api alembic revision --autogenerate -m "опис"
# застосувати
docker compose exec api alembic upgrade head
```

Контейнер `api` виконує `alembic upgrade head` під час старту (entrypoint),
тож окремий крок у звичайному розгортанні не потрібен.
