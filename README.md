# Fleet MDM

Self-hosted платформа керування Android-пристроями, які **явно зареєстровані
їхнім власником**. Розгортається повністю на приватному VPS: FastAPI +
PostgreSQL + Redis, React-панель, Kotlin-агент, Docker Compose.

> **Прозорість — архітектурна вимога.** Агент завжди видимий користувачу
> пристрою, показує, хто адміністратор і що саме передається. Прихованого
> стеження, кейлогінгу, перехоплення повідомлень чи прихованого доступу до
> камери/мікрофона в системі немає й не буде — див.
> [ARCHITECTURE.md §1](docs/ARCHITECTURE.md).

---

## Стан: усі заявлені модулі реалізовані ✅

| Складова | Стан |
|---|---|
| Архітектура, схема БД, модель безпеки | ✅ [docs/](docs/) |
| Docker Compose (postgres, redis, api, frontend, nginx TLS-ready) | ✅ |
| Бекенд: JWT + refresh у HttpOnly-cookie, RBAC, rate limit, аудит | ✅ 388 тестів проходять |
| Enrollment: токени, реєстрація, challenge–response на ключах Keystore | ✅ |
| **Репозиторій APK**: SHA-256, парсер AndroidManifest, дедуплікація | ✅ [APK_MODULE.md](docs/APK_MODULE.md) |
| **Командний канал**: черга, WS-доставка, ack/progress/result, скасування | ✅ |
| **Застосунки**: інвентар, порівняння версій, install/update/uninstall/launch | ✅ |
| **Геолокація**: opt-in, дозволи, інтервал/точність, офлайн-черга, карта | ✅ [LOCATION_MODULE.md](docs/LOCATION_MODULE.md) |
| **Файли**: allowlist каталогів, передачі з відновленням, checksum | ✅ [FILES_MODULE.md](docs/FILES_MODULE.md) |
| **Телеметрія**: заряд, RAM, сховище, CPU, версії, тип мережі | ✅ [MONITORING.md](docs/MONITORING.md) |
| **Моніторинг**: 11 типів правил, пороги, історія, WS-оновлення | ✅ |
| **Віддалена підтримка**: згода, MediaProjection, адаптація, аудит | ✅ [REMOTE_SUPPORT.md](docs/REMOTE_SUPPORT.md) |
| **Ретеншн**: фонове прибирання геолокації, телеметрії та аудиту | ✅ |
| Android-агент: P-256, PackageInstaller, геолокація, файли, foreground-сервіс | ✅ APK збирається, 25 тестів |
| Панель: пристрої, моніторинг, застосунки, APK, файли, геолокація, підтримка | ✅ React + TS |
| Kiosk-політики та набори політик | ⏳ M5 |

Повний план — [docs/ROADMAP.md](docs/ROADMAP.md).

---

## Швидкий старт

```bash
cp .env.example .env
```

Обовʼязково замініть у `.env`: `POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD`,
`REDIS_PASSWORD`, `JWT_SECRET`, `TOKEN_HASH_SECRET`, `BOOTSTRAP_ADMIN_PASSWORD`.
Готові рядки для вставки:

```bash
make secrets
```

Для `APP_ENV=production` сервер не стартує з дефолтними чи короткими
секретами, з http-адресами, порожнім `ALLOWED_HOSTS` або Redis без пароля.

```bash
docker compose up -d --build          # production-конфігурація
make up-dev                           # оточення розробки (hot-reload)
```

### Linux: запуск бекенду одним файлом

Розрахований на чисту Ubuntu/Debian — не встановленого раніше нічого не
потрібно. Скрипт сам перевірить, чого бракує (Python, Docker, Node.js), і
запропонує це поставити; перелік пакетів показується перед установкою.

```bash
./start-backend.sh
```

| Команда | Що робить |
|---|---|
| `./start-backend.sh` | налаштувати (за потреби) і запустити |
| `./start-backend.sh setup` | лише налаштувати |
| `./start-backend.sh test` | прогнати тести |
| `./start-backend.sh stop` | зупинити службові контейнери |
| `./start-backend.sh reset` | прибрати локальну конфігурацію та БД |

Прапорці: `-y` — не питати підтвердження на встановлення пакетів;
`--no-docker` — працювати на SQLite; `--host`/`--port` — адреса й порт
(типово `127.0.0.1:8000`).

Скрипт **не** додає користувача до групи `docker`: членство в ній
рівносильне правам root, і таке рішення має ухвалювати людина. Якщо доступ
до сокета лише через sudo, скрипт так і працюватиме — через sudo.

### Windows: запуск бекенду одним файлом

```
start-backend.bat
```

Скрипт ідемпотентний і робить усе сам: створює venv, ставить залежності,
генерує `backend\.env` зі свіжими секретами, застосовує міграції й піднімає
сервер на `http://127.0.0.1:8000`. Наявну конфігурацію не перезаписує.

| Команда | Що робить |
|---|---|
| `start-backend.bat` | налаштувати (за потреби) і запустити |
| `start-backend.bat setup` | лише налаштувати |
| `start-backend.bat test` | прогнати тести |
| `start-backend.bat stop` | зупинити службові контейнери |
| `start-backend.bat reset` | прибрати локальну конфігурацію та БД |

Якщо Docker доступний, скрипт підніме PostgreSQL і Redis і
під'єднається до них. Без Docker він перейде на SQLite і **попередить**, що
вхід у панель, автентифікація агентів і черга команд не працюватимуть — їм
потрібен Redis.

> `docker compose up` піднімає саме production-профіль. Налаштування
> розробки лежать в окремому `docker-compose.dev.yml` і підключаються лише
> явно — файл із назвою `docker-compose.override.yml` підхоплювався б
> автоматично й тихо перемкнув би робочий сервер у режим розробки.

API підніметься на `http://localhost:8000` (міграції застосуються автоматично),
документація — `http://localhost:8000/docs`, перевірка — `curl localhost:8000/health`.

### Перший пристрій

1. Увійти: `POST /api/v1/auth/login` з `BOOTSTRAP_ADMIN_*` із `.env`.
2. Створити токен: `POST /api/v1/enrollment/tokens` — відкритий токен
   повертається **один раз**, разом із `qr_payload`.
3. У застосунку `Fleet Агент` ввести адресу сервера й токен (або відсканувати QR).
4. Пристрій зʼявиться у `GET /api/v1/devices`.

---

## Структура

```
backend/     FastAPI + SQLAlchemy 2.0 + Alembic (REST /api/v1, WS /ws)
android/     Kotlin-агент (Compose, Android Keystore P-256, PackageInstaller)
frontend/    React + TypeScript панель оператора
infra/nginx/ TLS-проксі, HSTS, WebSocket
docs/        ARCHITECTURE · SECURITY · DATABASE · API · ROADMAP
             APK_MODULE · LOCATION_MODULE · FILES_MODULE
             MONITORING · REMOTE_SUPPORT
```

---

## Розробка

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
cd backend && python -m pytest -q
```

```bash
cd android && ./gradlew assembleDebug testDebugUnitTest
```

```bash
cd frontend && npm install && npm run build
```

Тести бекенду працюють на SQLite + fakeredis, тож окреме оточення не потрібне.
`make help` показує решту команд.

---

## Безпека

- TLS-термінація в nginx, HSTS, сучасні cipher-набори; Postgres і Redis не
  публікують портів назовні.
- Аудит безпеки та перелік контролів — [docs/SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md).
- Реєстр команд (віддаленої оболонки немає) — [docs/COMMANDS.md](docs/COMMANDS.md).
- Комунікаційний модуль (опційний, вимкнений за замовчуванням) — [docs/COMMS.md](docs/COMMS.md).
- **Розгортання в production (TLS, бекапи, оновлення)** — [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- Паролі — Argon2id; access-JWT 15 хв; refresh із ротацією та виявленням
  повторного використання (краде токен → відкликається вся родина сесій).
- У кожного пристрою власна пара ключів EC P-256, приватна частина не покидає
  Android Keystore; сервер зберігає лише публічний ключ.
- Enrollment-токени зберігаються як HMAC-SHA256, показуються один раз.
- RBAC із 4 ролей, rate limiting на Redis, незмінний журнал аудиту.
- У production сервер відмовляється стартувати з дефолтними секретами.

Деталі та модель загроз — [docs/SECURITY.md](docs/SECURITY.md).

---

## Правова рамка

Система призначена для пристроїв, які власник реєструє самостійно або які
належать організації. Перед розгортанням переконайтеся, що користувачів
поінформовано письмово (в Україні — ЗУ «Про захист персональних даних»,
ст. 163 ККУ щодо таємниці листування; в ЄС — GDPR ст. 6 і 13, ePrivacy).
Геолокація та віддалена підтримка вмикаються окремо, з видимим станом на
пристрої та записом у журналі аудиту.
