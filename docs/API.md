# Довідник API (v1)

Базовий шлях: `/api/v1`. Формат помилок:

```json
{"error": {"code": "unauthenticated", "message": "Невірна пошта або пароль", "details": {}}}
```

Стан на M1: реалізовані ендпоїнти позначені ✅, заплановані — ⏳.

---

## Автентифікація операторів

| Метод | Шлях | Права | Опис |
|---|---|---|---|
| ✅ POST | `/auth/login` | — | Вхід за поштою й паролем. Rate limit 10/хв (fail-closed) |
| ✅ POST | `/auth/refresh` | cookie + CSRF | Ротація refresh-токена; повторне використання відкликає родину |
| ✅ POST | `/auth/logout` | автентифікація | Завершує поточну сесію |
| ✅ POST | `/auth/logout-all` | автентифікація | Завершує всі сесії на всіх пристроях |
| ✅ GET | `/auth/me` | автентифікація | Профіль + матриця дозволів ролі |
| ✅ POST | `/auth/password` | автентифікація | Зміна пароля; завершує всі сесії. Rate limit 5/хв |

### Як зберігаються токени

* **access-токен** — у тілі відповіді, живе лише в памʼяті вкладки,
  надсилається заголовком `Authorization: Bearer …`;
* **refresh-токен** — **лише** у cookie `HttpOnly; Secure; SameSite=strict`
  з `Path=/api/v1/auth`. У тілі його немає: так його не дістане ані код
  панелі, ані впроваджений XSS-скрипт;
* **CSRF-токен** — у звичайній cookie `fleet_csrf` і в полі `csrf_token`.
  Оскільки cookie сесії браузер надсилає автоматично, кожен небезпечний
  метод має нести заголовок `X-CSRF-Token` із тим самим значенням.

```http
POST /api/v1/auth/login
{"email": "admin@example.com", "password": "…"}

200 OK
Set-Cookie: fleet_refresh=…; HttpOnly; Secure; SameSite=strict; Path=/api/v1/auth
Set-Cookie: fleet_csrf=…; Secure; SameSite=strict; Path=/api/v1/auth

{"access_token": "…", "token_type": "bearer", "expires_in": 900,
 "role": "superadmin", "permissions": ["device:read", …], "csrf_token": "…"}
```

```http
POST /api/v1/auth/refresh
Cookie: fleet_refresh=…; fleet_csrf=abc
X-CSRF-Token: abc
```

Вихід, зміна пароля чи ролі зсувають `users.sessions_valid_from` — уже
виданий access-токен перестає прийматися **негайно**, не чекаючи свого
15-хвилинного терміну.

## Користувачі

| Метод | Шлях | Дозвіл |
|---|---|---|
| ✅ GET | `/users` | `user:manage` |
| ✅ POST | `/users` | `user:manage` |
| ✅ PATCH | `/users/{id}` | `user:manage` |
| ✅ POST | `/users/{id}/password` | `user:manage` |
| ✅ POST | `/users/{id}/sessions/revoke` | `user:manage` |
| ✅ DELETE | `/users/{id}` | `user:manage` |

Зміна ролі, деактивація, скидання пароля та явне відкликання завершують усі
сесії користувача — і refresh-, і вже видані access-токени.

## Enrollment (адміністративна частина)

| Метод | Шлях | Дозвіл | Опис |
|---|---|---|---|
| ✅ POST | `/enrollment/tokens` | `enrollment:manage` | Створити токен. **Відкритий токен повертається один раз** |
| ✅ GET | `/enrollment/tokens` | `enrollment:manage` | Перелік (лише префікси) |
| ✅ POST | `/enrollment/tokens/{id}/revoke` | `enrollment:manage` | Відкликати |

```http
POST /api/v1/enrollment/tokens
{"label": "Склад №2", "max_uses": 1, "ttl_seconds": 86400}

201 Created
{"id": "…", "token": "FLEET-A3F9-K2M7-…", "token_prefix": "FLEET-A3F9…",
 "enroll_url": "https://mdm.example.com/api/v1/enroll",
 "qr_payload": "{\"v\":1,\"server\":\"https://mdm.example.com\",\"token\":\"FLEET-…\"}",
 "max_uses": 1, "use_count": 0, "expires_at": "2026-08-13T10:00:00Z"}
```

## Ендпоїнти агента

| Метод | Шлях | Авторизація | Опис |
|---|---|---|---|
| ✅ POST | `/enroll` | одноразовий токен | Реєстрація пристрою з публічним ключем |
| ✅ POST | `/device/auth/challenge` | — | Видає nonce (TTL 120 с, одноразовий) |
| ✅ POST | `/device/auth/token` | підпис nonce | Видає device-JWT (TTL 1 год) |
| ✅ GET | `/device/me` | device-JWT | Самоперевірка агента |
| ✅ GET | `/device/commands/pending` | device-JWT | Команди до виконання |
| ✅ POST | `/device/commands/{id}/ack` | device-JWT | Прийнято до виконання |
| ✅ POST | `/device/commands/{id}/progress` | device-JWT | Прогрес 0–100 + повідомлення |
| ✅ POST | `/device/commands/{id}/result` | device-JWT | Результат `succeeded`/`failed` |
| ✅ POST | `/device/apps` | device-JWT | Повний перелік застосунків |
| ✅ GET | `/device/apks/{id}/download` | device-JWT | APK — лише за наявності активної команди |
| ✅ GET | `/device/location/config` | device-JWT | Конфігурація геолокації для цього пристрою |
| ✅ POST | `/device/location/permissions` | device-JWT | Стан дозволів → повертає конфігурацію |
| ✅ POST | `/device/location` | device-JWT | Пакет координат; 403, якщо модуль вимкнено |
| ✅ GET | `/device/files/roots` | device-JWT | Дозволені каталоги цього пристрою |
| ✅ GET | `/device/files/transfers/{id}` | device-JWT | Стан передачі (звідки продовжувати) |
| ✅ POST | `/device/files/transfers/{id}/chunk` | device-JWT | Чанк із `X-Chunk-Offset` |
| ✅ GET | `/device/files/transfers/{id}/content` | device-JWT | Тіло файла, підтримує `Range` |
| ✅ POST | `/device/files/transfers/{id}/progress\|complete\|fail` | device-JWT | Прогрес і завершення |
| ✅ POST | `/device/telemetry` | device-JWT | Зріз стану: заряд, RAM, сховище, CPU, версії |

```http
POST /api/v1/enroll
{"token": "FLEET-A3F9-…", "public_key": "<SPKI/DER base64>",
 "device": {"manufacturer": "Samsung", "model": "SM-A536B", "android_version": "14",
            "sdk_int": 34, "android_id": "…", "management_mode": "device_owner",
            "agent_version": "0.1.0"}}

201 Created
{"device_id": "…", "device_name": "Samsung SM-A536B",
 "api_base_url": "https://mdm.example.com/api/v1",
 "websocket_url": "wss://mdm.example.com/ws/device",
 "heartbeat_interval_seconds": 60, "management_mode": "device_owner",
 "transparency_notice": "Цей пристрій керується адміністратором…"}
```

Підпис для `/device/auth/token` формується над рядком
`fleet-device-auth:<device_id>:<nonce>` алгоритмом `SHA256withECDSA`,
результат — DER-підпис у base64.

## Пристрої

| Метод | Шлях | Дозвіл |
|---|---|---|
| ✅ GET | `/devices` | `device:read` (фільтри `search`, `status`, пагінація) |
| ✅ GET | `/devices/{id}` | `device:read` (з телеметрією та онлайн-статусом) |
| ✅ PATCH | `/devices/{id}` | `device:write` |
| ✅ DELETE | `/devices/{id}` | `device:delete` (мʼяке видалення) |
| ✅ GET | `/devices/{id}/commands` | `device:read` — історія операцій |
| ✅ GET | `/devices/{id}/apps` | `app:read` — інвентар + порівняння версій |
| ✅ POST | `/devices/{id}/apps/refresh` | `device:action:safe` |
| ✅ POST | `/devices/{id}/apps/install` | `app:manage` |
| ✅ POST | `/devices/{id}/apps/uninstall` | `app:manage` |
| ✅ POST | `/devices/{id}/apps/launch` | `device:action:safe` |
| ✅ GET/POST/DELETE | `/devices/{id}/files/roots` | `file:read` / `file:write` — allowlist каталогів |
| ✅ POST | `/devices/{id}/files/list\|mkdir\|rename\|delete` | `file:read` / `file:write` |
| ✅ POST | `/devices/{id}/files/download\|upload` | `file:read` / `file:write` — передачі |
| ✅ GET | `/devices/{id}/files/transfers[/{id}[/content]]` | `file:read` |
| ✅ GET | `/devices/{id}/location` | `location:read` — зведення модуля геолокації |
| ✅ GET/PUT | `/devices/{id}/location/settings` | `location:read` / `location:manage` |
| ✅ POST | `/devices/{id}/location/request` | `location:manage` — разовий вимір |
| ✅ GET/DELETE | `/devices/{id}/location/history` | `location:read` / `location:manage` |
| ✅ POST | `/devices/{id}/support-sessions` | `support:session` — запит сеансу |

## Облікові дані пристроїв

Ключ пристрою перевипускає **агент** — приватна частина ніколи не залишає
Android Keystore. Сервер лише перевіряє підпис старим ключем і відкликає
видані токени.

| Метод | Шлях | Хто | Опис |
|---|---|---|---|
| ✅ POST | `/device/key/challenge` | агент | Nonce для перевипуску (окремий простір імен від nonce автентифікації) |
| ✅ POST | `/device/key/rotate` | агент | Новий публічний ключ, підписаний **старим**. Rate limit 5/год |
| ✅ GET | `/devices/{id}/credentials` | `device:read` | Покоління, вік ключа, чи час ротації |
| ✅ GET | `/devices/{id}/credentials/history` | `device:read` | Відкликані публічні ключі |
| ✅ POST | `/devices/{id}/credentials/rotate` | `device:credential:rotate` | Відкликати токени й вимагати перевипуску |
| ✅ GET | `/maintenance/device-keys` | `device:read` | Скільки пристроїв мають прострочений ключ |
| ✅ POST | `/maintenance/device-keys/rotate-overdue` | `device:credential:rotate` | Позначити всі прострочені |

```http
POST /api/v1/device/key/rotate
Authorization: Bearer <device-token>
{"new_public_key": "<SPKI/DER base64>", "nonce": "…",
 "signature": "<підпис старим ключем>", "reason": "scheduled"}

200 OK
{"device_id": "…", "key_fingerprint": "…", "key_generation": 2,
 "rotated_at": "…", "reauthentication_required": true}
```

Підписується рядок
`fleet-device-key-rotation:<device_id>:<sha256(new_public_key)>:<nonce>`
алгоритмом `SHA256withECDSA`. Після успіху всі попередні токени пристрою
недійсні — агент проходить challenge–response наново вже новою парою.

Відкликаний ключ не можна ані повернути, ані зареєструвати з ним новий
пристрій.

## Ротація серверних секретів

| Метод | Шлях | Дозвіл | Опис |
|---|---|---|---|
| ✅ GET | `/maintenance/secrets` | `secret:rotate` | Стан ротації та процедура. Самих секретів не повертає |

Порядок ротації — у [SECURITY.md §7.1](SECURITY.md).

## Репозиторій APK

| Метод | Шлях | Дозвіл | Опис |
|---|---|---|---|
| ✅ POST | `/apks` | `app:manage` | multipart `file` + `notes`; SHA-256 і метадані рахує сервер |
| ✅ GET | `/apks` | `app:read` | фільтри `package`, `search` |
| ✅ GET | `/apks/{id}` | `app:read` | метадані, дозволи, ABI |
| ✅ PATCH | `/apks/{id}` | `app:manage` | мітка й примітка |
| ✅ GET | `/apks/{id}/download` | `app:read` | заголовок `X-Checksum-SHA256` |
| ✅ DELETE | `/apks/{id}` | `app:manage` | 409, якщо APK у активній операції |

Деталі модуля — [APK_MODULE.md](APK_MODULE.md).

## Команди

Виконати можна лише те, що описано в реєстрі — довільного рядка для
виконання на пристрої в API немає. Повний опис: [COMMANDS.md](COMMANDS.md).

| Метод | Шлях | Дозвіл | Опис |
|---|---|---|---|
| ✅ GET | `/commands/registry` | `device:read` | Усі типи команд: параметри, потрібний дозвіл, що побачить користувач пристрою |
| ✅ PATCH | `/commands/registry/{type}/permission` | `settings:manage` | Змінити, кому дозволено видавати цей тип |
| ✅ DELETE | `/commands/registry/{type}/permission` | `settings:manage` | Повернути дозвіл за замовчуванням |
| ✅ POST | `/devices/{id}/commands` | перевіряє реєстр | Видати команду |
| ✅ GET | `/commands` | `device:read` | Історія (фільтри `type`, `status`) |
| ✅ GET | `/devices/{id}/commands` | `device:read` | Історія одного пристрою |
| ✅ GET | `/commands/{id}` | `device:read` | Деталі команди з довідкою про тип |
| ✅ GET | `/commands/stats` | `device:read` | Зведення за типами: успіхи, збої, тривалості |
| ✅ POST | `/commands/{id}/cancel` | як у видачі | Скасувати незавершену |

```http
POST /api/v1/devices/{id}/commands
{"type": "COLLECT_DIAGNOSTICS", "params": {"log_lines": 100}}

202 Accepted
{"id": "…", "type": "COLLECT_DIAGNOSTICS", "status": "pending",
 "created_by_id": "…", "created_at": "…", "expires_at": "…",
 "queue_ms": null, "duration_ms": null, "is_terminal": false}
```

Спеціалізовані ендпоїнти (те саме через реєстр):

| Метод | Шлях | Команда |
|---|---|---|
| ✅ POST | `/devices/{id}/reboot` | `REBOOT` |
| ✅ POST | `/devices/{id}/telemetry/refresh` | `REFRESH_TELEMETRY` |
| ✅ POST | `/devices/{id}/config/sync` | `SYNC_CONFIG` |
| ✅ POST | `/devices/{id}/agent/restart` | `AGENT_RESTART` |
| ✅ POST | `/devices/{id}/diagnostics` | `COLLECT_DIAGNOSTICS` |
| ✅ POST | `/devices/{id}/apps/install` | `APP_INSTALL` / `APP_UPDATE` |
| ✅ POST | `/devices/{id}/apps/uninstall` | `APP_UNINSTALL` |
| ✅ POST | `/devices/{id}/apps/launch` | `APP_LAUNCH` |
| ✅ POST | `/devices/{id}/apps/stop` | `APP_STOP` |
| ✅ POST | `/devices/{id}/apps/refresh` | `APP_LIST` |

## Моніторинг і сповіщення

| Метод | Шлях | Дозвіл |
|---|---|---|
| ✅ GET | `/alert-rules`, `/alert-rules/schema` | `device:read` |
| ✅ POST/PATCH/DELETE | `/alert-rules[/{id}]` | `alert:manage` |
| ✅ GET | `/alerts` | `device:read` (фільтри `state`, `severity`, `type`, `device_id`) |
| ✅ POST | `/alerts/{id}/acknowledge\|resolve` | `device:action:safe` |
| ✅ GET | `/monitoring/summary`, `/monitoring/devices` | `device:read` |
| ✅ POST | `/monitoring/evaluate` | `alert:manage` |

Деталі — [MONITORING.md](MONITORING.md).

## Віддалена підтримка

| Метод | Шлях | Дозвіл |
|---|---|---|
| ✅ POST | `/devices/{id}/support-sessions` | `support:session` |
| ✅ GET | `/support-sessions[/{id}]` | `device:read` |
| ✅ POST | `/support-sessions/{id}/stop` | `support:session` |
| ✅ POST | `/devices/{id}/support-sessions/refresh` | `support:session` |

Агентські: `GET/POST /device/support-sessions/{id}[/consent|/end]` (device-JWT).
Деталі — [REMOTE_SUPPORT.md](REMOTE_SUPPORT.md).

## Віддалена підтримка — доповнення

| Метод | Шлях | Дозвіл | Опис |
|---|---|---|---|
| ✅ POST | `/support-sessions/{id}/viewer-ticket` | `support:session` | Одноразовий квиток (TTL 60 с) на приєднання до перегляду |
| ✅ GET | `/support-sessions/{id}/connection` | `device:read` | Чи пристрій у каналі, скільки глядачів, коли був останній кадр |
| ✅ GET | `/support-sessions/{id}/audit` | `audit:read` | Журнал подій саме цього сеансу |

```http
POST /api/v1/support-sessions/{id}/viewer-ticket

200 OK
{"session_id": "…", "ticket": "…", "expires_in": 60,
 "viewer_url": "wss://mdm.example.com/ws/support/…"}
```

Квиток гасне при першому використанні й прив'язаний до пари «сеанс +
оператор». Панель бере новий на кожне підключення, тож перезавантаження
вкладки й обрив мережі не потребують зберігати нічого чутливого в браузері.

Деталі — [REMOTE_SUPPORT.md](REMOTE_SUPPORT.md).

## Аудит

| Метод | Шлях | Дозвіл | Опис |
|---|---|---|---|
| ✅ GET | `/audit` | `audit:read` | Фільтри: `action`, `actor_user_id`, `actor_device_id`, `object_id`, `result`, `since`, `until` |
| ✅ GET | `/audit/actions` | `audit:read` | Перелік дій, що трапляються в журналі |

## Службові

| Метод | Шлях | Опис |
|---|---|---|
| ✅ GET | `/health` | Стан БД і Redis (поза `/api/v1`) |
| ✅ GET | `/health/live` | Простий liveness-пробник |
| ✅ GET | `/maintenance/retention` | Поточна політика зберігання (`settings:manage`) |
| ✅ POST | `/maintenance/retention` | Позачергове прибирання прострочених даних |

Деталі модулів — [APK_MODULE.md](APK_MODULE.md), [LOCATION_MODULE.md](LOCATION_MODULE.md), [FILES_MODULE.md](FILES_MODULE.md), [MONITORING.md](MONITORING.md), [REMOTE_SUPPORT.md](REMOTE_SUPPORT.md).

## WebSocket

| Шлях | Авторизація | Стан |
|---|---|---|
| ✅ `/ws/device?token=<device-JWT>` | device-JWT | Присутність + heartbeat; типи команд — M2 |
| ✅ `/ws/admin?token=<access-JWT>` | access-JWT | Живі події: алерти, прогрес команд (лише читання) |
| ✅ `/ws/support/{id}?role=device\|viewer` | device-JWT / access-JWT + `secret` | Трансляція екрана (бінарні кадри) |

## Коди помилок

| HTTP | `code` | Коли |
|---|---|---|
| 401 | `unauthenticated` | немає/недійсний токен, невірні дані входу |
| 403 | `permission_denied` | роль не має потрібного дозволу (пишеться в аудит) |
| 404 | `not_found` | обʼєкт відсутній |
| 409 | `conflict` | дубль ключа пристрою / ANDROID_ID / пошти |
| 422 | `validation_failed` | некоректні вхідні дані |
| 429 | `rate_limited` | перевищено ліміт; є заголовок `Retry-After` |
| 503 | `service_unavailable` | недоступна залежність |

Інтерактивна документація (поза production): `GET /docs`.
