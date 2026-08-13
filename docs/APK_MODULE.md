# Модуль керування застосунками (APK)

Версія: 1.0 · 2026-08-12 · відповідає Milestone 4

Повний шлях APK: **завантаження адміністратором → репозиторій → команда
конкретному зареєстрованому пристрою → перевірка на пристрої → встановлення →
звіт про результат → аудит**.

---

## 1. Межа безпеки

> **Тихе встановлення можливе лише на пристрої, який власник сам зареєстрував
> і який має активний статус.**

Що це означає технічно:

| Контроль | Де реалізовано |
|---|---|
| APK віддається лише за токеном пристрою | `GET /device/apks/{id}/download` вимагає device-JWT |
| …і лише за наявності живої команди встановлення саме цього APK для саме цього пристрою | `device_agent.py:device_download_apk` |
| Спроба без команди → `403` + запис `APK_DOWNLOADED` з `result=denied` | там само |
| Пристрій може підтверджувати/завершувати лише **свої** команди | `commands.py:_get_device_command` |
| Видалений (`deleted_at`) або призупинений пристрій втрачає доступ | `deps.py:get_current_device` |
| Без Device Owner Android показує діалог підтвердження — агент його не обходить | `ApkInstaller.kt` |

Агент **не має** механізму встановлення на пристрій, який не пройшов
enrollment: без `device_id` і ключа в Keystore він навіть не отримає токен.

---

## 2. Завантаження та метадані

`POST /api/v1/apks` (multipart, дозвіл `app:manage`).

Сервер потоково приймає файл, рахуючи SHA-256 на льоту:

1. перевірка ZIP-сигнатури `PK\x03\x04` на першому чанку;
2. обмеження розміру (`MAX_UPLOAD_MB`);
3. дедуплікація за SHA-256 — повторне завантаження того самого файла повертає
   наявний запис із `deduplicated: true`;
4. розбір `AndroidManifest.xml` (**власний парсер AXML**, без зовнішніх залежностей);
5. збереження у `<STORAGE_ROOT>/apk/<ab>/<sha256>.apk`.

Витягується:

| Поле | Джерело |
|---|---|
| `package_name` | `<manifest package>` |
| `version_code` / `version_name` | `<manifest android:versionCode/versionName>` |
| `min_sdk` / `target_sdk` | `<uses-sdk>` |
| `compile_sdk` | `<manifest android:compileSdkVersion>` |
| `abis` → `architecture` | каталоги `lib/<abi>/` у ZIP |
| `permissions` | усі `<uses-permission android:name>` |
| `signature_schemes` | `META-INF/*.RSA\|DSA\|EC` (v1) + блок `APK Sig Block 42` (v2/v3) |
| `signature_digest` | SHA-256 сертифіката v1, якщо він є |
| `size_bytes`, `sha256` | сам файл |

Split-APK (`<manifest split="...">`) відхиляються: потрібен цілісний APK.

Парсер перевіряється і на синтетичних APK (`tests/axml.py`), і на реальному
APK агента, зібраному Gradle (`test_parses_real_apk_if_available`).

---

## 3. Життєвий цикл операції

```
адмін → POST /devices/{id}/apps/install {apk_id}
   │      перевірки: package є, не пониження версії, min_sdk ≤ sdk пристрою,
   │                 немає іншої активної операції для цього пакета
   ▼
commands(status=pending, type=APP_INSTALL|APP_UPDATE)   ──audit APP_INSTALL(phase=requested)
   │
   ├── Redis pub/sub → WS агента (миттєво)
   └── або GET /device/commands/pending (опитування, запасний канал)
   ▼
агент: POST /device/commands/{id}/ack                    status=acked
   │
   ├── progress 10..70  завантаження APK + перевірка SHA-256
   ├── progress 75      перевірка package/versionCode у файлі
   ├── progress 80      PackageInstaller (session → commit)
   └── progress 95      зчитування встановленої версії
   ▼
агент: POST /device/commands/{id}/result {status, result{mechanism, installed}}
   ▼
сервер: commands.status = succeeded|failed
        оновлення device_apps                            ──audit COMMAND_COMPLETED
```

`APP_INSTALL` чи `APP_UPDATE` вибирається автоматично — за наявністю пакета в
інвентарі пристрою.

### Механізми встановлення

Поле `result.mechanism` у команді завжди показує, як саме встановлено:

| Значення | Коли |
|---|---|
| `package_installer_device_owner` | агент є Device Owner → система дозволяє тихе встановлення |
| `package_installer_user_confirmed` | звичайний режим → Android показав діалог, користувач підтвердив |
| `root_pm_install` | запасний `su -c pm install` на власному рутованому пристрої (вимкнено за замовчуванням) |
| `already_absent` | видалення пакета, якого вже немає |
| `launch_intent` | запуск застосунку |

Root-механізм вмикається лише перемикачем на екрані агента
(`allow_root_install`), використовується **тільки** після невдачі штатного
`PackageInstaller` і завжди потрапляє в журнал операцій.

---

## 4. Перевірки на пристрої

Перед встановленням агент послідовно перевіряє:

1. **SHA-256** завантаженого файла (`ApkDownloader`) — розбіжність = `CHECKSUM_MISMATCH`,
   файл видаляється;
2. **package name** із `getPackageArchiveInfo` — розбіжність = `PACKAGE_MISMATCH`;
3. **versionCode** проти очікуваного з команди — розбіжність = `VERSION_MISMATCH`.

Файл лежить у приватному кеші застосунку й видаляється у `finally` незалежно
від результату.

---

## 5. API

| Метод | Шлях | Дозвіл | Опис |
|---|---|---|---|
| POST | `/apks` | `app:manage` | Завантажити APK (multipart `file`, `notes`) |
| GET | `/apks` | `app:read` | Перелік із фільтрами `package`, `search` |
| GET | `/apks/{id}` | `app:read` | Метадані |
| PATCH | `/apks/{id}` | `app:manage` | Мітка та примітка |
| GET | `/apks/{id}/download` | `app:read` | Вивантаження адміністратором |
| DELETE | `/apks/{id}` | `app:manage` | Видалити (блокується, якщо APK у активній операції) |
| GET | `/devices/{id}/apps` | `app:read` | Інвентар + порівняння версій |
| POST | `/devices/{id}/apps/refresh` | `device:action:safe` | Запит переліку з пристрою |
| POST | `/devices/{id}/apps/install` | `app:manage` | Встановити/оновити |
| POST | `/devices/{id}/apps/uninstall` | `app:manage` | Видалити |
| POST | `/devices/{id}/apps/launch` | `device:action:safe` | Запустити |
| GET | `/devices/{id}/commands`, `/commands` | `device:read` | Історія операцій |
| POST | `/commands/{id}/cancel` | `app:manage` | Скасувати незавершену |

Агентські (device-JWT): `GET /device/commands/pending`,
`POST /device/commands/{id}/ack|progress|result`, `POST /device/apps`,
`GET /device/apks/{id}/download`.

---

## 6. Порівняння версій

`GET /devices/{id}/apps` для кожного пакета підмішує найсвіжішу версію з
репозиторію:

```json
{
  "package_name": "ua.example.pos",
  "version_code": 100, "version_name": "1.0.100",
  "in_repository": true,
  "repository_apk_id": "…", "repository_version_code": 120,
  "update_available": true
}
```

У панелі це вкладка **Застосунки** пристрою: бейдж «є оновлення», кнопка
«Оновити» ставить `APP_UPDATE` в чергу; «Запустити» і «Видалити» — відповідні
команди. Системні застосунки показуються за окремим перемикачем і не
видаляються (лише вимикаються політикою — M5).

---

## 7. Що пишеться в аудит

| Дія | Коли |
|---|---|
| `APK_UPLOADED` | завантаження в репозиторій (пакет, версія, sha256, ABI, кількість дозволів) |
| `APK_DOWNLOADED` | вивантаження адміністратором (`channel=admin`) або агентом (`channel=device`); відмова — `result=denied` |
| `APK_DELETED`, `APK_UPDATED` | видалення / зміна мітки |
| `APP_INSTALL`, `APP_UPDATE`, `APP_UNINSTALL`, `APP_LAUNCH` | запит адміністратора (`phase=requested`, `command_id`) |
| `APP_LIST_REQUESTED` | запит інвентаря |
| `APP_INVENTORY_REPORTED` | звіт агента (додано/оновлено/видалено) |
| `COMMAND_COMPLETED` | результат від пристрою (actor = пристрій, `result=success\|failure`) |
| `COMMAND_CANCELLED` | скасування адміністратором |
| `ACCESS_DENIED` | спроба дії без потрібного дозволу |

---

## 8. Обмеження цієї версії

- Немає перевірки, що підпис APK збігається з підписом уже встановленої версії
  (Android відхилить таке встановлення сам — помилка повернеться як
  `INSTALL_FAILED_UPDATE_INCOMPATIBLE`).
- Split-APK і `.aab` не підтримуються.
- `progress` під час самого встановлення не деталізується: Android не дає
  прогресу всередині сесії `PackageInstaller`, тому крок фіксується як 80 %.
- Черга прострочених команд прибирається при наступному опитуванні пристрою;
  окремого фонового воркера ще немає (M8).
