package ua.fleet.agent.location

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.util.Log
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import ua.fleet.agent.data.AgentStore
import ua.fleet.agent.enroll.EnrollmentRepository
import ua.fleet.agent.net.ApiClient
import ua.fleet.agent.net.dto.LocationBatchDto
import ua.fleet.agent.net.dto.LocationConfigDto
import ua.fleet.agent.net.dto.LocationFixDto
import ua.fleet.agent.net.dto.LocationPermissionDto

/**
 * Оркестратор модуля геолокації на пристрої.
 *
 * Збирає координати **лише** коли:
 *  1. адміністратор увімкнув модуль для цього пристрою (конфіг із сервера);
 *  2. користувач надав дозвіл Android і геолокація ввімкнена в системі;
 *  3. дозволяє політика батареї;
 *  4. настав час чергового виміру (інтервал із конфігу).
 *
 * Якщо мережі немає, координати лягають в офлайн-чергу і відправляються
 * пакетом пізніше; невдалі відправки повторюються з наростаючою паузою.
 */
class LocationReporter(
    context: Context,
    private val repository: EnrollmentRepository,
) {

    private val appContext = context.applicationContext
    private val store: AgentStore = repository.state
    private val collector = LocationCollector(appContext)
    private val queue = LocationQueue(File(appContext.filesDir, LocationQueue.FILE_NAME))
    private val mutex = Mutex()

    data class Status(
        val enabled: Boolean,
        val permissionState: String,
        val backgroundGranted: Boolean,
        val servicesEnabled: Boolean,
        val intervalSeconds: Int,
        val highAccuracy: Boolean,
        val queuedFixes: Int,
        val lastFixAtMillis: Long,
        val batteryNote: String,
    )

    fun status(): Status {
        val permissions = LocationPermissions.current(appContext)
        val (battery, charging) = batteryState()
        return Status(
            enabled = store.locationEnabled,
            permissionState = permissions.permissionState,
            backgroundGranted = permissions.backgroundGranted,
            servicesEnabled = permissions.locationServicesEnabled,
            intervalSeconds = store.locationIntervalSeconds,
            highAccuracy = store.locationHighAccuracy,
            queuedFixes = queue.size,
            lastFixAtMillis = store.lastLocationAtMillis,
            batteryNote = BatteryPolicy.describe(battery, charging),
        )
    }

    // ------------------------------------------------------ конфігурація --

    fun applyConfig(config: LocationConfigDto) {
        store.locationEnabled = config.enabled
        store.locationIntervalSeconds = config.intervalSeconds.coerceAtLeast(60)
        store.locationMinDistanceMeters = config.minDistanceMeters.coerceAtLeast(0)
        store.locationHighAccuracy = config.highAccuracy

        if (!config.enabled) {
            // Модуль вимкнено адміністратором — нічого не тримаємо локально.
            queue.clear()
            Log.i(TAG, "Геолокацію вимкнено, чергу очищено")
        }
    }

    /** Забирає конфігурацію із сервера і застосовує її. */
    suspend fun syncConfig(): LocationConfigDto? = withContext(Dispatchers.IO) {
        val serverUrl = store.serverUrl ?: return@withContext null
        val api = ApiClient.create(serverUrl)
        val config = api.locationConfig("Bearer ${repository.accessToken()}")
        applyConfig(config)
        config
    }

    /** Повідомляє серверу фактичний стан дозволів (лише коли він змінився). */
    suspend fun syncPermissions(force: Boolean = false): Unit = withContext(Dispatchers.IO) {
        val serverUrl = store.serverUrl ?: return@withContext
        val permissions = LocationPermissions.current(appContext)
        val signature = "${permissions.permissionState}:${permissions.backgroundGranted}"
        if (!force && store.reportedPermissionState == signature) return@withContext

        val api = ApiClient.create(serverUrl)
        val config = api.reportLocationPermissions(
            "Bearer ${repository.accessToken()}",
            LocationPermissionDto(
                permissionState = permissions.permissionState,
                backgroundPermission = permissions.backgroundGranted,
            ),
        )
        store.reportedPermissionState = signature
        applyConfig(config)
    }

    // --------------------------------------------------------- збирання ---

    /**
     * Викликається періодично сервісом. Повертає true, якщо щось зробили.
     */
    suspend fun tick(): Boolean = mutex.withLock {
        if (!store.isEnrolled) return false

        runCatching { syncPermissions() }
            .onFailure { Log.w(TAG, "Не вдалося надіслати стан дозволів", it) }

        if (!store.locationEnabled) return false

        var acted = false
        if (isTimeForFix()) {
            acted = collect(source = "periodic")
        }
        if (flushQueue() > 0) acted = true
        acted
    }

    /** Разовий вимір на запит адміністратора (команда LOCATION_REQUEST). */
    suspend fun collectOnDemand(): Boolean = mutex.withLock {
        if (!store.locationEnabled) {
            Log.i(TAG, "Разовий запит проігноровано: модуль вимкнено")
            return false
        }
        val collected = collect(source = "on_demand", ignoreDistanceFilter = true)
        flushQueue()
        collected
    }

    private fun isTimeForFix(): Boolean {
        val (battery, charging) = batteryState()
        if (!BatteryPolicy.shouldCollect(battery, charging)) return false
        val interval = BatteryPolicy.effectiveIntervalSeconds(
            store.locationIntervalSeconds, battery, charging
        )
        val elapsed = System.currentTimeMillis() - store.lastLocationAtMillis
        return elapsed >= interval * 1000L
    }

    private suspend fun collect(source: String, ignoreDistanceFilter: Boolean = false): Boolean {
        val (battery, charging) = batteryState()
        val highAccuracy = BatteryPolicy.useHighAccuracy(
            store.locationHighAccuracy, battery, charging
        )

        val fix = collector.requestFix(
            highAccuracy = highAccuracy,
            source = source,
            batteryPercent = battery.takeIf { it >= 0 },
        ) ?: return false

        if (!ignoreDistanceFilter && !movedEnough(fix)) {
            Log.d(TAG, "Позиція майже не змінилася — пропускаємо")
            store.lastLocationAtMillis = System.currentTimeMillis()
            return false
        }

        queue.add(fix)
        store.lastLocationAtMillis = System.currentTimeMillis()
        store.lastLocationLat = fix.latitude.toFloat()
        store.lastLocationLon = fix.longitude.toFloat()
        return true
    }

    private fun movedEnough(fix: LocationFixDto): Boolean {
        val minDistance = store.locationMinDistanceMeters
        if (minDistance <= 0) return true
        val lastLat = store.lastLocationLat
        val lastLon = store.lastLocationLon
        if (lastLat.isNaN() || lastLon.isNaN()) return true

        val distance = LocationCollector.distanceMeters(
            lastLat.toDouble(), lastLon.toDouble(), fix.latitude, fix.longitude
        )
        return distance >= minDistance
    }

    // -------------------------------------------------------- відправка ---

    /** Відправляє чергу пакетами. Повертає кількість надісланих координат. */
    suspend fun flushQueue(): Int = withContext(Dispatchers.IO) {
        val serverUrl = store.serverUrl ?: return@withContext 0
        if (queue.size == 0) return@withContext 0
        if (System.currentTimeMillis() < store.nextUploadAttemptAtMillis) {
            Log.d(TAG, "Відправку відкладено до наступного вікна ретраю")
            return@withContext 0
        }

        val api = ApiClient.create(serverUrl)
        var sent = 0

        while (true) {
            val batch = queue.peek(BATCH_SIZE)
            if (batch.isEmpty()) break

            try {
                val response = api.uploadLocation(
                    "Bearer ${repository.accessToken()}",
                    LocationBatchDto(batch.map { it.copy(source = "offline_queue") }),
                )
                queue.drop(batch.size)
                sent += response.accepted
                store.uploadFailureCount = 0
                store.nextUploadAttemptAtMillis = 0
                applyConfig(response.config)
                store.lastContactAtMillis = System.currentTimeMillis()
            } catch (error: Throwable) {
                if (isModuleDisabled(error)) {
                    // Сервер каже, що модуль вимкнено — нічого не накопичуємо.
                    Log.i(TAG, "Сервер відхилив координати: модуль вимкнено")
                    store.locationEnabled = false
                    queue.clear()
                    break
                }
                scheduleRetry(error)
                break
            }
        }
        sent
    }

    private fun isModuleDisabled(error: Throwable): Boolean =
        error is retrofit2.HttpException && error.code() == 403

    private fun scheduleRetry(error: Throwable) {
        val failures = (store.uploadFailureCount + 1).coerceAtMost(MAX_BACKOFF_STEPS)
        store.uploadFailureCount = failures
        val delayMs = INITIAL_RETRY_MS * (1L shl (failures - 1))
        store.nextUploadAttemptAtMillis = System.currentTimeMillis() + delayMs
        Log.w(
            TAG,
            "Відправку не вдалося виконати (спроба $failures), повтор через ${delayMs / 1000} с",
            error,
        )
    }

    // ---------------------------------------------------------- батарея ---

    /** @return `(відсоток заряду, чи заряджається)`; -1 якщо невідомо. */
    private fun batteryState(): Pair<Int, Boolean> {
        val intent = appContext.registerReceiver(
            null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)
        ) ?: return -1 to false

        val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        val percent = if (level >= 0 && scale > 0) level * 100 / scale else -1

        val status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
            status == BatteryManager.BATTERY_STATUS_FULL

        return percent to charging
    }

    private companion object {
        const val TAG = "FleetLocationReporter"
        const val BATCH_SIZE = 200
        const val INITIAL_RETRY_MS = 60_000L
        const val MAX_BACKOFF_STEPS = 6 // до ~1 години
    }
}
