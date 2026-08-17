# Розгортання в production

Версія: 1.0 · 2026-08-13

Покрокова інструкція від чистого сервера до працюючого парку пристроїв.
Розрахована на одну машину з Docker; масштабування — у §9.

---

## 1. Вимоги

| Компонент | Мінімум | Навіщо |
|---|---|---|
| CPU / RAM | 2 vCPU / 4 ГБ | API + Postgres + Redis + nginx |
| Диск | 20 ГБ + місце під APK | сховище артефактів і файлів пристроїв |
| ОС | будь-яка з Docker 24+ | |
| Docker Compose | v2 | |
| Домен | `mdm.example.com` з A/AAAA на сервер | TLS-сертифікат і enrollment |
| Порти | 80, 443 назовні | ACME-челендж і сама панель |

Порти Postgres (5432) і Redis (6379) назовні **не відкриваються**: у
production-конфігурації вони живуть лише у внутрішній мережі Docker.

---

## 2. Секрети

```bash
git clone <репозиторій> fleet && cd fleet
cp .env.example .env
make secrets          # згенерує готові рядки
```

Перенесіть згенеровані значення у `.env`. **Обовʼязкові** до заміни:

| Змінна | Призначення |
|---|---|
| `POSTGRES_PASSWORD` | роль-власник схеми (лише міграції) |
| `POSTGRES_APP_PASSWORD` | роль застосунку (CRUD без DDL) |
| `REDIS_PASSWORD` | доступ до nonce і каналів команд |
| `JWT_SECRET` | підпис токенів |
| `TOKEN_HASH_SECRET` | HMAC збережених токенів (≠ `JWT_SECRET`) |
| `BOOTSTRAP_ADMIN_PASSWORD` | перший вхід |

Далі виставте оточення:

```ini
APP_ENV=production
PUBLIC_BASE_URL=https://mdm.example.com
ALLOWED_HOSTS=mdm.example.com
CORS_ORIGINS=https://mdm.example.com
```

> Сервер **не стартує**, якщо в production лишилися дефолтні чи короткі
> секрети, `http://` в `PUBLIC_BASE_URL`, порожній `ALLOWED_HOSTS`, `*` у
> CORS або Redis без пароля. Це навмисно: тиха робота з небезпечною
> конфігурацією гірша за відмову старту.

Опційно — комунікаційний модуль (див. [COMMS.md](COMMS.md)). Без
`COMMS_ENCRYPTION_KEY` модуль просто неможливо ввімкнути; якщо він вам не
потрібен, лишіть змінну порожньою.

Права на файл: `chmod 600 .env` — у ньому всі ключі.

---

## 3. TLS-сертифікат

nginx очікує сертифікати в `infra/nginx/certs/`:
`fullchain.pem` і `privkey.pem`.

### Let's Encrypt (рекомендовано)

```bash
mkdir -p infra/nginx/certs
docker compose up -d nginx          # підніме HTTP для ACME-челенджа

docker run --rm \
  -v "$PWD/infra/nginx/certs:/etc/letsencrypt/live-out" \
  -v fleet-mdm_certbot_www:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d mdm.example.com --agree-tos -m admin@example.com --non-interactive

cp /etc/letsencrypt/live/mdm.example.com/{fullchain,privkey}.pem \
   infra/nginx/certs/
docker compose exec nginx nginx -s reload
```

### Поновлення

Сертифікат живе 90 днів. Додайте в cron:

```cron
0 3 * * 1 cd /opt/fleet && docker run --rm \
  -v /opt/fleet/infra/nginx/certs:/certs \
  -v fleet-mdm_certbot_www:/var/www/certbot \
  certbot/certbot renew --webroot -w /var/www/certbot --quiet && \
  docker compose exec -T nginx nginx -s reload
```

### Власний CA

Покладіть свої `fullchain.pem`/`privkey.pem` у той самий каталог. Додатково
покладіть кореневий сертифікат у `android/app/src/main/res/raw/fleet_ca.crt`
і розкоментуйте блок `<certificates src="@raw/fleet_ca" />` у
`res/xml/network_security_config.xml` — інакше агент не довірятиме серверу.
`trustAll` не використовується й не буде.

---

## 4. Перший запуск

```bash
docker compose up -d --build
docker compose logs -f api        # дочекайтесь "Старт застосунку"
```

Що відбувається автоматично:

1. Postgres ініціалізується, `infra/postgres/init/01-app-role.sh` створює
   роль застосунку з правами лише на дані (без DDL);
2. `entrypoint.sh` чекає на БД і виконує `alembic upgrade head` **роллю
   власника** (`MIGRATION_DATABASE_URL`);
3. створюється bootstrap-адміністратор (лише якщо таблиця `users` порожня);
4. піднімаються фонові цикли: ретеншн і моніторинг.

Перевірка:

```bash
curl -fsS https://mdm.example.com/health          # {"status":"ok"}
```

У production `/health` навмисно віддає лише статус. Повна картина —
`/health/details` з дозволом `settings:manage`.

**Одразу після першого входу змініть пароль** і приберіть
`BOOTSTRAP_ADMIN_PASSWORD` з `.env`.

---

## 4а. Варіант: поруч із наявним зворотним проксі

Якщо на машині вже щось займає 80/443 (наприклад, Caddy обслуговує інший
проєкт), Fleet не потребує окремих портів: додається **ще один сайт** у
конфігурацію проксі, а бекенд слухає лише loopback.

1. Бекенд як служба — `infra/systemd/fleet-mdm.service`:

```bash
sudo cp infra/systemd/fleet-mdm.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now fleet-mdm
```

   `ExecStart` прив'язує uvicorn до `127.0.0.1`: назовні дивиться лише
   проксі, тож у обхід TLS до API не дістатися.

2. Сайт у Caddy — `infra/caddy/fleet-mdm.Caddyfile`, дописується в кінець
   наявного `/etc/caddy/Caddyfile`. Наявні блоки не змінюються:

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
cat infra/caddy/fleet-mdm.Caddyfile | sudo tee -a /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

   Якщо `validate` запускати через `sudo`, він створює файл логу від root, і
   служба (працює від користувача `caddy`) потім не може в нього писати —
   reload падає з `permission denied`. Лікується один раз:

```bash
sudo chown caddy:caddy /var/log/caddy/fleet-mdm.log
```

   Reload у Caddy атомарний: якщо новий конфіг не прийнявся, працює
   попередній, і сусідні сайти не страждають.

3. `backend/.env` під публічний домен:

```ini
APP_ENV=production
LOG_FORMAT=json
PUBLIC_BASE_URL=https://mdm.example.com
CORS_ORIGINS=https://mdm.example.com
ALLOWED_HOSTS=mdm.example.com,127.0.0.1,localhost
TRUSTED_PROXIES=127.0.0.1
```

   `TRUSTED_PROXIES` обов'язковий: без нього обмеження частоти рахувало б
   усіх клієнтів як `127.0.0.1`, тобто одним спільним лічильником на весь
   інтернет. Проксі при цьому має **перезаписувати** `X-Forwarded-For`
   (`header_up X-Forwarded-For {remote_host}`), інакше клієнт підкине
   власний заголовок і сховається за чужою адресою.

   `APP_ENV=production` вмикає Secure-cookie — саме тому цей режим і
   потребує HTTPS. Перевірка секретів на старті відхилить слабкі значення й
   `PUBLIC_BASE_URL` з `http://`.

---

## 5. Реєстрація першого пристрою

1. Панель → **Реєстрація** → «Створити токен». Задайте назву, кількість
   використань і термін дії. Одразу з'явиться QR і сам токен —
   відкритий токен показується **один раз**, далі в базі лише хеш.
2. Зберіть агента: `cd android && ./gradlew assembleRelease`
   (підпишіть власним ключем; `APK_ALLOWED_SIGNERS` дозволяє закріпити цей
   сертифікат у репозиторії APK).
3. Встановіть агента, введіть адресу сервера й токен (або скануйте QR).
   Агент приймає лише HTTPS — виняток зроблено для `localhost` і `10.0.2.2`
   (емулятор), див. `android/app/src/main/res/xml/network_security_config.xml`.
   Адреса в QR береться з `PUBLIC_BASE_URL`, тож вона має бути тією, за якою
   пристрій справді дістає сервер.
4. Пристрій зʼявиться у списку; телеметрія піде за хвилину.

Для режиму **Device Owner** (потрібен для перезавантаження й тихого
встановлення) пристрій має бути без облікових записів:

```bash
adb shell dpm set-device-owner ua.fleet.agent/.admin.FleetDeviceAdminReceiver
```

---

## 6. Резервне копіювання

```bash
make backup                        # ./backups/<UTC-мітка>/
make backups                       # перелік копій
make restore s=20260813T120000Z    # відновлення (питає підтвердження)
```

У копію входять дамп БД і сховище артефактів. **`.env` — ні**, свідомо:
інакше вкрадений архів давав би і дані, і ключі. Зберігайте `.env` окремо
(менеджер секретів, офлайн-носій).

> Без того самого `COMMS_ENCRYPTION_KEY` комунікаційні дані з копії не
> розшифрувати. Це властивість шифрування at-rest, а не помилка.

Cron для щоденної копії о 02:00 з ротацією 14 днів:

```cron
0 2 * * * cd /opt/fleet && make backup >> /var/log/fleet-backup.log 2>&1
30 2 * * * find /opt/fleet/backups -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
```

Перевіряйте відновлення хоча б раз на квартал — копія, яку жодного разу не
розгортали, це не резервна копія, а сподівання.

---

## 7. Оновлення

```bash
cd /opt/fleet
make backup                        # спершу копія
git pull
docker compose up -d --build       # міграції застосуються автоматично
docker compose logs -f api
```

Відкат: `git checkout <попередній тег>` → `make restore s=<копія>`.

---

## 8. Експлуатація

### Ротація секретів

Без розриву сесій (деталі — [SECURITY.md §7.1](SECURITY.md)):

```ini
JWT_SECRET_PREVIOUS=<старий>
JWT_SECRET=<новий>
```
→ перезапуск → зачекати `REFRESH_TOKEN_TTL_SECONDS` → прибрати `*_PREVIOUS`.

Стан: `GET /api/v1/maintenance/secrets`.
Аварійне відкликання всіх токенів: `TOKEN_EPOCH=<поточний час ISO-8601>`.

### Втрачено доступ до панелі

`BOOTSTRAP_ADMIN_PASSWORD` діє лише поки таблиця користувачів порожня — на
працюючій базі заміна цього значення в `.env` нічого не змінює. Скидання
пароля виконується з контейнера API:

```bash
docker compose exec api python scripts/reset_password.py admin@example.com --password '<новий>'
```

Без `--password` береться поточне значення `BOOTSTRAP_ADMIN_PASSWORD` — так
пароль у конфігурації і пароль у базі знову збігаються. Дія пишеться в аудит
(`AUTH_PASSWORD_RESET`, actor `cli`) і відкликає всі активні сесії
користувача, включно з уже виданими access-токенами.

### Що моніторити

| Сигнал | Де | Чому важливо |
|---|---|---|
| `/health` ≠ `ok` | HTTP-проба | БД недоступна |
| `AUTH_REFRESH_REUSE` | аудит | ознака викраденого токена |
| `COMMAND_REJECTED` серіями | аудит | розвідка прав |
| `DEVICE_KEY_ROTATION_FAILED` | аудит | хтось має токен, але не ключ |
| `COMMS_DATA_VIEWED` | аудит | доступ до особистих даних |
| Місце на диску | ОС | сховище APK росте |

Логи структуровані (JSON) — забирайте `docker compose logs` у ваш збирач.
Секрети маскуються на рівні логера.

### Ретеншн

Фоновий цикл кожні 6 годин прибирає геолокацію, телеметрію, аудит,
прострочені токени й комунікаційні дані за їхніми строками
(`*_HISTORY_DAYS`, `AUDIT_RETENTION_DAYS`). Ручний запуск —
`POST /api/v1/maintenance/retention`.

---

## 9. Масштабування

Поточна конфігурація — одна репліка API. Для кількох:

* **WebSocket підтримки** (`/ws/support/`) ретранслює кадри в межах процесу —
  потрібна липка маршрутизація за `session_id` у nginx;
* командний канал і присутність уже йдуть через Redis pub/sub і працюють
  на будь-якій кількості реплік;
* сховище артефактів (`storage_data`) має бути спільним томом або S3-сумісним
  сховищем (зараз — локальний том).

---

## 10. Відомі обмеження

- Перезавантаження пристрою й тихе встановлення потребують **Device Owner**;
  без нього Android показує діалог підтвердження — це обмеження системи, і
  ми його не обходимо.
- **Постійна робота агента без Device Owner неможлива.** Від Android 12
  застосунок не має права піднімати foreground-службу з фону, а від
  Android 15 — ще й із `BOOT_COMPLETED` для типу `dataSync`. Тобто після
  перезавантаження пристрою (або після того, як систему щось змусило
  зупинити службу) агент без Device Owner підніметься лише тоді, коли
  власник відкриє застосунок. Обхідних шляхів ми не застосовуємо: тиха
  робота у фоні без постійного сповіщення суперечила б самій ідеї видимого
  керування. Агент у такому разі показує пояснення на головному екрані, а
  команда «перезапустити агента» повертає `failed` із причиною — замість
  того щоб рапортувати про успіх і замовкнути.
- Запис розмов, перехоплення повідомлень чужих застосунків, кейлогінг і
  читання сповіщень задля вмісту **не реалізовані**: таких API Android
  застосунку не надає. Деталі — [COMMS.md](COMMS.md).
- Немає пакетної видачі команд на групу пристроїв і планувальника.
- MMS не збираються (лише SMS).
- Docker Compose розрахований на одну машину; HA-конфігурації немає.
