package ua.fleet.agent.location

import android.Manifest
import android.app.AppOpsManager
import android.content.Context
import android.content.pm.PackageManager
import android.location.LocationManager
import android.os.Build
import androidx.core.content.ContextCompat

/**
 * Стан дозволів на геолокацію.
 *
 * Агент **не обходить** механізм дозволів Android: якщо користувач не дав
 * дозвіл — координати не збираються, а сервер бачить стан `denied`.
 */
object LocationPermissions {

    const val STATE_UNKNOWN = "unknown"
    const val STATE_GRANTED_FINE = "granted_fine"
    const val STATE_GRANTED_COARSE = "granted_coarse"
    const val STATE_DENIED = "denied"
    const val STATE_RESTRICTED = "restricted"

    val FOREGROUND_PERMISSIONS = arrayOf(
        Manifest.permission.ACCESS_FINE_LOCATION,
        Manifest.permission.ACCESS_COARSE_LOCATION,
    )

    const val BACKGROUND_PERMISSION = Manifest.permission.ACCESS_BACKGROUND_LOCATION

    data class State(
        val permissionState: String,
        val backgroundGranted: Boolean,
        val locationServicesEnabled: Boolean,
    ) {
        val canCollect: Boolean
            get() = permissionState in setOf(STATE_GRANTED_FINE, STATE_GRANTED_COARSE) &&
                locationServicesEnabled
    }

    fun current(context: Context): State {
        val fine = granted(context, Manifest.permission.ACCESS_FINE_LOCATION)
        val coarse = granted(context, Manifest.permission.ACCESS_COARSE_LOCATION)

        val state = when {
            fine -> STATE_GRANTED_FINE
            coarse -> STATE_GRANTED_COARSE
            isRestrictedByPolicy(context) -> STATE_RESTRICTED
            else -> STATE_DENIED
        }

        val background = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            granted(context, BACKGROUND_PERMISSION)
        } else {
            // До Android 10 окремого фонового дозволу не існувало.
            fine || coarse
        }

        return State(
            permissionState = state,
            backgroundGranted = background,
            locationServicesEnabled = servicesEnabled(context),
        )
    }

    /** Чи ввімкнена геолокація в системних налаштуваннях. */
    fun servicesEnabled(context: Context): Boolean {
        val manager = context.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
            ?: return false
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            manager.isLocationEnabled
        } else {
            @Suppress("DEPRECATION")
            manager.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
                manager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
        }
    }

    fun granted(context: Context, permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

    /**
     * Дозвіл може бути заблокований політикою пристрою (Device Owner) —
     * тоді користувач фізично не може його надати, і це варто показати окремо.
     */
    private fun isRestrictedByPolicy(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return false
        val ops = context.getSystemService(Context.APP_OPS_SERVICE) as? AppOpsManager
            ?: return false
        return try {
            val mode = ops.unsafeCheckOpNoThrow(
                AppOpsManager.OPSTR_FINE_LOCATION,
                android.os.Process.myUid(),
                context.packageName,
            )
            mode == AppOpsManager.MODE_ERRORED
        } catch (_: Exception) {
            false
        }
    }
}
