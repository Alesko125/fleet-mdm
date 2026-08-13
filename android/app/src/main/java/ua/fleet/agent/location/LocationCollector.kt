package ua.fleet.agent.location

import android.annotation.SuppressLint
import android.content.Context
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.CancellationSignal
import android.os.Looper
import android.util.Log
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import ua.fleet.agent.net.dto.LocationFixDto

/**
 * Разове визначення позиції через штатний `LocationManager` (AOSP, без
 * Google Play Services).
 *
 * Збір відбувається **тільки** за явним викликом: агент запитує координату
 * раз на інтервал, заданий адміністратором, а не тримає постійну підписку.
 * Це і ощадливо для батареї, і прозоро — жодного фонового потоку даних поза
 * налаштованим розкладом.
 */
class LocationCollector(private val context: Context) {

    private val manager: LocationManager? =
        context.getSystemService(Context.LOCATION_SERVICE) as? LocationManager

    /**
     * @param highAccuracy true → GPS, false → мережеві провайдери
     * @return координата або null, якщо дозволу немає / позицію не отримано
     */
    suspend fun requestFix(
        highAccuracy: Boolean,
        source: String,
        batteryPercent: Int?,
        timeoutMs: Long = DEFAULT_TIMEOUT_MS,
    ): LocationFixDto? {
        val permissions = LocationPermissions.current(context)
        if (!permissions.canCollect) {
            Log.i(TAG, "Збір пропущено: дозвіл ${permissions.permissionState}")
            return null
        }
        val provider = selectProvider(highAccuracy) ?: return null

        val location = withTimeoutOrNull(timeoutMs) { awaitLocation(provider) }
            ?: lastKnown(provider)
            ?: return null

        return location.toDto(source, batteryPercent)
    }

    private fun selectProvider(highAccuracy: Boolean): String? {
        val manager = manager ?: return null
        val available = runCatching { manager.getProviders(true) }.getOrDefault(emptyList())
        if (available.isEmpty()) return null

        val preferred = if (highAccuracy) {
            listOf(LocationManager.GPS_PROVIDER, fusedProvider(), LocationManager.NETWORK_PROVIDER)
        } else {
            listOf(LocationManager.NETWORK_PROVIDER, fusedProvider(), LocationManager.GPS_PROVIDER)
        }
        return preferred.filterNotNull().firstOrNull { it in available } ?: available.first()
    }

    private fun fusedProvider(): String? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) LocationManager.FUSED_PROVIDER else null

    @SuppressLint("MissingPermission") // дозвіл перевірено в requestFix()
    private suspend fun awaitLocation(provider: String): Location? =
        suspendCancellableCoroutine { continuation ->
            val manager = manager
            if (manager == null) {
                continuation.resume(null)
                return@suspendCancellableCoroutine
            }

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                val signal = CancellationSignal()
                continuation.invokeOnCancellation { signal.cancel() }
                runCatching {
                    manager.getCurrentLocation(
                        provider,
                        signal,
                        context.mainExecutor,
                    ) { location ->
                        if (continuation.isActive) continuation.resume(location)
                    }
                }.onFailure {
                    Log.w(TAG, "getCurrentLocation не спрацював", it)
                    if (continuation.isActive) continuation.resume(null)
                }
                return@suspendCancellableCoroutine
            }

            // Android 10 і старіші: одноразова підписка, яку знімаємо одразу.
            val listener = object : LocationListener {
                override fun onLocationChanged(location: Location) {
                    runCatching { manager.removeUpdates(this) }
                    if (continuation.isActive) continuation.resume(location)
                }

                @Deprecated("Потрібен для сумісності зі старими API")
                override fun onStatusChanged(provider: String?, status: Int, extras: android.os.Bundle?) = Unit

                override fun onProviderDisabled(provider: String) {
                    runCatching { manager.removeUpdates(this) }
                    if (continuation.isActive) continuation.resume(null)
                }

                override fun onProviderEnabled(provider: String) = Unit
            }

            runCatching {
                manager.requestLocationUpdates(provider, 0L, 0f, listener, Looper.getMainLooper())
            }.onFailure {
                Log.w(TAG, "requestLocationUpdates не спрацював", it)
                if (continuation.isActive) continuation.resume(null)
            }

            continuation.invokeOnCancellation { runCatching { manager.removeUpdates(listener) } }
        }

    @SuppressLint("MissingPermission")
    private fun lastKnown(provider: String): Location? = try {
        manager?.getLastKnownLocation(provider)?.takeIf {
            System.currentTimeMillis() - it.time < MAX_LAST_KNOWN_AGE_MS
        }
    } catch (e: SecurityException) {
        Log.w(TAG, "Немає дозволу на getLastKnownLocation", e)
        null
    }

    private fun Location.toDto(source: String, batteryPercent: Int?) = LocationFixDto(
        latitude = latitude,
        longitude = longitude,
        accuracyM = if (hasAccuracy()) accuracy else null,
        altitudeM = if (hasAltitude()) altitude else null,
        speedMps = if (hasSpeed()) speed else null,
        bearingDeg = if (hasBearing()) bearing else null,
        provider = provider,
        source = source,
        isMock = isMockLocation(),
        batteryLevel = batteryPercent,
        recordedAt = formatTimestamp(time),
    )

    private fun Location.isMockLocation(): Boolean =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            isMock
        } else {
            @Suppress("DEPRECATION")
            isFromMockProvider
        }

    companion object {
        private const val TAG = "FleetLocation"
        private const val DEFAULT_TIMEOUT_MS = 45_000L
        private const val MAX_LAST_KNOWN_AGE_MS = 10 * 60 * 1000L

        /** ISO-8601 в UTC — саме такий формат чекає бекенд. */
        fun formatTimestamp(epochMillis: Long): String {
            val format = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
            format.timeZone = TimeZone.getTimeZone("UTC")
            return format.format(Date(epochMillis))
        }

        /** Відстань у метрах між двома координатами. */
        fun distanceMeters(
            fromLat: Double,
            fromLon: Double,
            toLat: Double,
            toLon: Double,
        ): Float {
            val result = FloatArray(1)
            Location.distanceBetween(fromLat, fromLon, toLat, toLon, result)
            return result[0]
        }
    }
}
