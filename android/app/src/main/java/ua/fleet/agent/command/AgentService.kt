package ua.fleet.agent.command

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import ua.fleet.agent.FleetAgentApplication
import ua.fleet.agent.R
import ua.fleet.agent.data.AgentStore
import ua.fleet.agent.ui.MainActivity

/**
 * Фоновий сервіс агента.
 *
 * Працює як **foreground service** — Android показує постійне сповіщення, і
 * користувач завжди бачить, що пристрій під керуванням. Сервіс забирає
 * команди із сервера (миттєво через WebSocket, з періодичним опитуванням як
 * запасним каналом) і періодично надсилає перелік застосунків.
 */
/** Чим завершилася спроба підняти сервіс. */
enum class StartOutcome {
    /** Запит прийнято системою. */
    STARTED,

    /** Система заборонила старт із фону — потрібен передній план або Device Owner. */
    NOT_ALLOWED,
}

class AgentService : Service() {

    private lateinit var store: AgentStore
    private lateinit var processor: CommandProcessor
    private var socket: CommandSocket? = null
    private var loopJob: Job? = null
    private val scope = CoroutineScope(SupervisorJob())

    override fun onCreate() {
        super.onCreate()
        val app = application as FleetAgentApplication
        store = app.agentStore
        processor = CommandProcessor(this, app.enrollmentRepository)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!promoteToForeground()) {
            // Постійне сповіщення — не формальність: воно єдине, що показує
            // власнику пристрою факт керування. Якщо система його не дала,
            // агент НЕ переходить у тихий фоновий режим, а зупиняється й
            // лишає слід для інтерфейсу. Непомітного керування тут не буде.
            store.foregroundBlockedAtMillis = System.currentTimeMillis()
            Log.w(TAG, "Немає дозволу на постійне сповіщення — сервіс зупиняється")
            stopSelf()
            return START_NOT_STICKY
        }
        store.foregroundBlockedAtMillis = 0L

        if (!store.isEnrolled) {
            Log.i(TAG, "Пристрій не зареєстровано — сервіс зупиняється")
            stopSelf()
            return START_NOT_STICKY
        }

        if (loopJob == null) {
            loopJob = scope.launch { workLoop() }
            connectSocket()
        }
        return START_STICKY
    }

    /** Переходить у foreground. `false` — система відмовила. */
    private fun promoteToForeground(): Boolean = try {
        startForeground(NOTIFICATION_ID, buildNotification(getString(R.string.service_idle)))
        true
    } catch (e: IllegalStateException) {
        false
    } catch (e: SecurityException) {
        Log.w(TAG, "Бракує дозволу на тип foreground-сервісу", e)
        false
    }

    private suspend fun workLoop() {
        val intervalMs = (store.heartbeatIntervalSeconds.coerceAtLeast(30)) * 1000L
        while (scope.isActive) {
            runCatching { pollOnce() }
                .onFailure { Log.w(TAG, "Цикл опитування завершився помилкою", it) }
            delay(intervalMs)
        }
    }

    private suspend fun pollOnce() {
        // Ротація ключа — перед рештою роботи: сервер міг відкликати токен,
        // і без перевипуску всі подальші запити все одно отримали б 401.
        runCatching {
            (application as FleetAgentApplication)
                .enrollmentRepository.rotateKeyIfRequested()
        }.onFailure { Log.w(TAG, "Перевірка ротації ключа не вдалася", it) }

        val handled = processor.runPending()
        if (handled > 0) {
            notify(getString(R.string.service_working))
        }

        // Телеметрія: інтервал диктує сервер (типово раз на хвилину).
        val sinceTelemetry = System.currentTimeMillis() - store.lastTelemetryAtMillis
        if (sinceTelemetry >= store.telemetryIntervalSeconds * 1000L) {
            runCatching { processor.reportTelemetry() }
                .onFailure { Log.w(TAG, "Не вдалося надіслати телеметрію", it) }
        }

        // Геолокація: збір і відправка відбуваються лише якщо модуль увімкнено
        // адміністратором і користувач надав дозвіл (перевірки всередині).
        runCatching { processor.locationReporter.tick() }
            .onFailure { Log.w(TAG, "Цикл геолокації завершився помилкою", it) }

        val sinceInventory = System.currentTimeMillis() - store.lastInventoryAtMillis
        if (sinceInventory > INVENTORY_INTERVAL_MS) {
            runCatching { processor.reportInventory() }
                .onSuccess { store.lastInventoryAtMillis = System.currentTimeMillis() }
                .onFailure { Log.w(TAG, "Не вдалося надіслати інвентар", it) }
        }

        // Комунікаційний модуль: збираємо лише коли адміністратор увімкнув
        // його на сервері. Синхронізація завжди оновлює стан дозволів, тож
        // панель бачить реальну картину навіть при вимкненому зборі.
        runCatching {
            if (store.commsEnabled) processor.commsReporter.collectAndUpload()
            else processor.commsReporter.sync()
        }.onFailure { Log.w(TAG, "Цикл комунікаційного модуля завершився помилкою", it) }
        val locationStatus = processor.locationReporter.status()
        notify(
            if (locationStatus.enabled) {
                getString(R.string.service_idle_with_location)
            } else {
                getString(R.string.service_idle)
            }
        )
    }

    private fun connectSocket() {
        val wsUrl = store.websocketUrl ?: return
        socket = CommandSocket(
            urlProvider = { wsUrl },
            tokenProvider = {
                (application as FleetAgentApplication).enrollmentRepository.accessToken()
            },
            onCommandAvailable = {
                scope.launch {
                    runCatching { processor.runPending() }
                        .onFailure { Log.w(TAG, "Обробка команди з WS не вдалася", it) }
                }
            },
            scope = scope,
        ).also { it.connect() }
    }

    override fun onDestroy() {
        socket?.close()
        loopJob?.cancel()
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ------------------------------------------------------- сповіщення ---

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.service_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.service_channel_description)
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0
        val pendingIntent = PendingIntent.getActivity(this, 0, intent, flags)

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle(getString(R.string.service_notification_title))
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun notify(text: String) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, buildNotification(text))
    }

    companion object {
        private const val TAG = "FleetAgentService"
        private const val CHANNEL_ID = "fleet_agent_status"
        private const val NOTIFICATION_ID = 4242
        private const val INVENTORY_INTERVAL_MS = 6 * 60 * 60 * 1000L

        /**
         * Піднімає сервіс. Ніколи не кидає виняток.
         *
         * Від Android 12 систему не можна попросити показати постійне
         * сповіщення з фону: `startForegroundService` кидає
         * `ForegroundServiceStartNotAllowedException` (нащадок
         * `IllegalStateException`), а від Android 15 служби типу `dataSync`
         * додатково не запускаються з BOOT_COMPLETED. Раніше це валило
         * застосунок; тепер повертаємо стан, а рішення ухвалює викликач.
         *
         * Винятки з обмеження — застосунок на передньому плані або режим
         * Device Owner. Саме тому агент відновлюється, щойно власник
         * відкриє застосунок.
         */
        fun start(context: Context): StartOutcome {
            val intent = Intent(context, AgentService::class.java)
            return try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
                StartOutcome.STARTED
            } catch (e: IllegalStateException) {
                // Ловимо саме IllegalStateException, а не клас із API 31:
                // посилання на нього в catch змусило б верифікатор класів
                // шукати відсутній тип на старіших пристроях.
                Log.w(TAG, "Система не дозволила запустити сервіс із фону", e)
                StartOutcome.NOT_ALLOWED
            } catch (e: SecurityException) {
                Log.w(TAG, "Бракує дозволу на foreground-сервіс", e)
                StartOutcome.NOT_ALLOWED
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, AgentService::class.java))
        }
    }
}
