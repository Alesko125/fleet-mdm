package ua.fleet.agent.telemetry

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.os.SystemClock
import android.util.Log
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import ua.fleet.agent.BuildConfig
import ua.fleet.agent.location.LocationCollector
import ua.fleet.agent.net.dto.TelemetryDto

/**
 * Збір показників стану пристрою.
 *
 * Збирається лише те, що потрібно для моніторингу справності: заряд і стан
 * живлення, памʼять, сховище, навантаження CPU, час роботи, версії ОС та
 * агента, тип підключення. Ні назви Wi-Fi мережі, ні оператора, ні будь-яких
 * даних про користувача чи його активність тут немає.
 */
class TelemetryCollector(private val context: Context) {

    suspend fun collect(): TelemetryDto = withContext(Dispatchers.IO) {
        val battery = batteryInfo()
        val memory = memoryInfo()
        val storage = storageInfo()

        TelemetryDto(
            manufacturer = Build.MANUFACTURER,
            model = Build.MODEL,
            product = Build.PRODUCT,
            androidVersion = Build.VERSION.RELEASE,
            sdkInt = Build.VERSION.SDK_INT,
            securityPatch = Build.VERSION.SECURITY_PATCH,
            buildId = Build.ID,
            kernelVersion = System.getProperty("os.version"),
            agentVersion = BuildConfig.AGENT_VERSION,
            uptimeSeconds = SystemClock.elapsedRealtime() / 1000,
            batteryLevel = battery.level,
            batteryStatus = battery.status,
            batteryHealth = battery.health,
            batteryTemperatureC = battery.temperatureC,
            charging = battery.charging,
            ramTotalBytes = memory.first,
            ramAvailableBytes = memory.second,
            storageTotalBytes = storage.first,
            storageFreeBytes = storage.second,
            cpuCores = Runtime.getRuntime().availableProcessors(),
            cpuLoadPercent = cpuLoadPercent(),
            cpuAbi = Build.SUPPORTED_ABIS.firstOrNull(),
            networkType = networkType(),
            collectedAt = LocationCollector.formatTimestamp(System.currentTimeMillis()),
        )
    }

    // ------------------------------------------------------------ батарея --

    private data class Battery(
        val level: Int?,
        val status: String?,
        val health: String?,
        val temperatureC: Float?,
        val charging: Boolean?,
    )

    private fun batteryInfo(): Battery {
        val intent = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            ?: return Battery(null, null, null, null, null)

        val rawLevel = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        val level = if (rawLevel >= 0 && scale > 0) rawLevel * 100 / scale else null

        val status = when (intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1)) {
            BatteryManager.BATTERY_STATUS_CHARGING -> "charging"
            BatteryManager.BATTERY_STATUS_DISCHARGING -> "discharging"
            BatteryManager.BATTERY_STATUS_FULL -> "full"
            BatteryManager.BATTERY_STATUS_NOT_CHARGING -> "not_charging"
            else -> null
        }
        val health = when (intent.getIntExtra(BatteryManager.EXTRA_HEALTH, -1)) {
            BatteryManager.BATTERY_HEALTH_GOOD -> "good"
            BatteryManager.BATTERY_HEALTH_OVERHEAT -> "overheat"
            BatteryManager.BATTERY_HEALTH_DEAD -> "dead"
            BatteryManager.BATTERY_HEALTH_COLD -> "cold"
            BatteryManager.BATTERY_HEALTH_OVER_VOLTAGE -> "over_voltage"
            else -> null
        }
        val temperature = intent.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE)

        return Battery(
            level = level,
            status = status,
            health = health,
            temperatureC = if (temperature != Int.MIN_VALUE) temperature / 10f else null,
            charging = status == "charging" || status == "full",
        )
    }

    // -------------------------------------------------------------- памʼять

    /** @return `(всього, доступно)` у байтах */
    private fun memoryInfo(): Pair<Long?, Long?> {
        val manager = context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager
            ?: return null to null
        val info = ActivityManager.MemoryInfo()
        manager.getMemoryInfo(info)
        return info.totalMem to info.availMem
    }

    /** @return `(всього, вільно)` у байтах для розділу даних */
    private fun storageInfo(): Pair<Long?, Long?> = try {
        val stat = StatFs(Environment.getDataDirectory().absolutePath)
        stat.totalBytes to stat.availableBytes
    } catch (e: Exception) {
        Log.w(TAG, "Не вдалося прочитати стан сховища", e)
        null to null
    }

    // ------------------------------------------------------------------ CPU

    /**
     * Навантаження CPU за двома зрізами `/proc/stat`.
     *
     * На частині пристроїв доступ до `/proc/stat` обмежений — тоді пробуємо
     * `/proc/loadavg`, а якщо і його немає, повертаємо null (сервер це
     * коректно обробляє).
     */
    private suspend fun cpuLoadPercent(): Float? {
        readCpuTimes()?.let { first ->
            delay(SAMPLE_DELAY_MS)
            readCpuTimes()?.let { second ->
                val totalDelta = second.total - first.total
                val idleDelta = second.idle - first.idle
                if (totalDelta > 0) {
                    val load = (totalDelta - idleDelta) * 100f / totalDelta
                    return load.coerceIn(0f, 100f)
                }
            }
        }
        return loadAveragePercent()
    }

    private data class CpuTimes(val total: Long, val idle: Long)

    private fun readCpuTimes(): CpuTimes? = try {
        val line = File("/proc/stat").bufferedReader().use { it.readLine() }
        if (line == null || !line.startsWith("cpu ")) {
            null
        } else {
            val values = line.split(Regex("\\s+")).drop(1).mapNotNull { it.toLongOrNull() }
            if (values.size < 5) {
                null
            } else {
                // 4-й і 5-й стовпці — idle та iowait.
                CpuTimes(total = values.sum(), idle = values[3] + values[4])
            }
        }
    } catch (_: Exception) {
        null
    }

    private fun loadAveragePercent(): Float? = try {
        val line = File("/proc/loadavg").bufferedReader().use { it.readLine() }
        val oneMinute = line?.split(" ")?.firstOrNull()?.toFloatOrNull()
        val cores = Runtime.getRuntime().availableProcessors().coerceAtLeast(1)
        oneMinute?.let { (it / cores * 100f).coerceIn(0f, 100f) }
    } catch (_: Exception) {
        null
    }

    // ---------------------------------------------------------------- мережа

    /** Лише тип підключення; назва мережі (SSID) навмисно не збирається. */
    private fun networkType(): String {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return "unknown"
        val network = manager.activeNetwork ?: return "none"
        val capabilities = manager.getNetworkCapabilities(network) ?: return "none"

        return when {
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "vpn"
            else -> "other"
        }
    }

    private companion object {
        const val TAG = "FleetTelemetry"
        const val SAMPLE_DELAY_MS = 500L
    }
}
