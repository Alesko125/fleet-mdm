# Fleet Агент (Android)

Kotlin-агент платформи Fleet MDM. У M1 реалізовано **потік реєстрації**:
генерація криптоідентичності, введення/сканування enrollment-токена,
реєстрація на сервері, challenge–response автентифікація та екран статусу.

## Принцип прозорості

Агент **не приховується**:

- має іконку в лаунчері й звичайну назву застосунку;
- головний екран показує, хто адміністратор, який сервер, режим керування
  і **повний перелік того, що передається**;
- окремим блоком перелічено, що **не** збирається (повідомлення, дзвінки,
  натискання клавіш, приховане аудіо/відео);
- геолокація та трансляція екрана вмикаються лише окремо і за згодою.

## Криптоідентичність

| Аспект | Реалізація |
|---|---|
| Алгоритм | EC P-256 (`secp256r1`), `SHA256withECDSA` |
| Сховище | Android Keystore, за наявності — StrongBox (`setIsStrongBoxBacked`) |
| Експорт ключа | неможливий — назовні йде лише публічна частина (SPKI/DER, base64) |
| Автентифікація | сервер видає nonce → агент підписує → отримує device-токен (1 год) |

Файл: [`crypto/DeviceKeyStore.kt`](app/src/main/java/ua/fleet/agent/crypto/DeviceKeyStore.kt)

## Потік реєстрації

```
Адмін у панелі створює токен  ──► QR або текст "FLEET-XXXX-XXXX-XXXX-XXXX-XXXX"
                                         │
Агент: ensureKeyPair() у Keystore  ◄─────┘
   │
   ├─ POST /api/v1/enroll { token, public_key, device{...} }
   │     ← device_id, назва, ws-URL, інтервал heartbeat, текст про прозорість
   │
   ├─ POST /api/v1/device/auth/challenge { device_id }        ← nonce
   ├─ підпис "fleet-device-auth:<device_id>:<nonce>"
   └─ POST /api/v1/device/auth/token { device_id, nonce, signature } ← device JWT
```

## Структура

```
app/src/main/java/ua/fleet/agent/
├── FleetAgentApplication.kt      # ініціалізація репозиторію
├── admin/FleetDeviceAdminReceiver.kt
├── crypto/DeviceKeyStore.kt      # P-256 у Keystore, підпис nonce
├── data/AgentStore.kt            # стан реєстрації (без секретів)
├── device/DeviceInfoCollector.kt # інвентар через задокументовані API
├── enroll/EnrollmentRepository.kt# реєстрація + отримання токена
├── net/                          # Retrofit-клієнт, DTO, обробка помилок
└── ui/                           # Compose: екран реєстрації та статусу
```

## Збірка

Потрібні Android SDK (platform 36) і JDK, який підтримує ваш Gradle.

```bash
cd android && ./gradlew assembleDebug
```

```bash
cd android && ./gradlew testDebugUnitTest
```

APK зʼявиться в `app/build/outputs/apk/debug/app-debug.apk`.

Версії, на яких зібрано M1: AGP 9.3.1, Gradle 9.5.0, Kotlin 2.3.21 (вбудований у AGP),
compileSdk 36, minSdk 26.

> `local.properties` (шлях до SDK) не комітиться — Gradle створює його автоматично
> в Android Studio, або задайте змінну `ANDROID_HOME`.

## Локальний сервер під час розробки

- Емулятор бачить хост як `10.0.2.2` → адреса сервера `http://10.0.2.2:8000`.
- `network_security_config.xml` дозволяє нешифрований трафік **лише** для
  `10.0.2.2` і `localhost`. Для реального пристрою потрібен HTTPS; для
  self-signed CA додайте сертифікат у `res/raw` і розкоментуйте `trust-anchors`.

## Device Owner (опційно)

Для повного набору можливостей (kiosk, тихе встановлення APK, reboot) агент
має бути Device Owner. На чистому пристрої:

```bash
adb shell dpm set-device-owner ua.fleet.agent/.admin.FleetDeviceAdminReceiver
```

Без Device Owner агент працює в обмеженому режимі: інвентар, телеметрія,
запуск застосунків, геолокація за згодою. Режим видно на екрані статусу.

## Що далі (M2)

- Foreground-сервіс із постійним сповіщенням і heartbeat.
- Повний збір телеметрії (батарея, память, сховище, мережа, uptime).
- WebSocket-канал команд із ACK і результатами.
