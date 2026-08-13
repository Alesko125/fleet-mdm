package ua.fleet.agent.device

import android.annotation.SuppressLint
import android.app.admin.DevicePolicyManager
import android.content.Context
import android.os.Build
import android.provider.Settings
import android.util.Log
import ua.fleet.agent.BuildConfig
import ua.fleet.agent.net.dto.DeviceEnrollInfo

/**
 * Збір інвентарних даних пристрою через задокументовані API.
 *
 * Нічого поза цим переліком не читається: жодних контактів, повідомлень,
 * історії дзвінків чи вмісту інших застосунків.
 */
class DeviceInfoCollector(private val context: Context) {

    fun collect(): DeviceEnrollInfo = DeviceEnrollInfo(
        manufacturer = Build.MANUFACTURER,
        model = Build.MODEL,
        product = Build.PRODUCT,
        androidVersion = Build.VERSION.RELEASE,
        sdkInt = Build.VERSION.SDK_INT,
        securityPatch = Build.VERSION.SECURITY_PATCH,
        buildId = Build.ID,
        kernelVersion = kernelVersion(),
        androidId = androidId(),
        serialNumber = serialNumber(),
        managementMode = managementMode(),
        agentVersion = BuildConfig.AGENT_VERSION,
        displayName = "${Build.MANUFACTURER} ${Build.MODEL}".trim(),
    )

    fun managementMode(): String {
        val dpm = context.getSystemService(Context.DEVICE_POLICY_SERVICE) as? DevicePolicyManager
            ?: return MODE_UNMANAGED
        val packageName = context.packageName
        return when {
            dpm.isDeviceOwnerApp(packageName) -> MODE_DEVICE_OWNER
            dpm.isProfileOwnerApp(packageName) -> MODE_PROFILE_OWNER
            else -> MODE_UNMANAGED
        }
    }

    private fun kernelVersion(): String? =
        System.getProperty("os.version") ?: System.getProperty("java.vm.version")

    @SuppressLint("HardwareIds")
    private fun androidId(): String? = try {
        // ANDROID_ID унікальний для пари (застосунок, користувач) і змінюється
        // після скидання до заводських — достатньо для виявлення дублів.
        Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
    } catch (e: Exception) {
        Log.w(TAG, "ANDROID_ID недоступний", e)
        null
    }

    /** Серійний номер доступний лише у режимі Device/Profile Owner. */
    private fun serialNumber(): String? {
        if (managementMode() == MODE_UNMANAGED) return null
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                Build.getSerial().takeIf { it != Build.UNKNOWN }
            } else {
                null
            }
        } catch (e: SecurityException) {
            Log.i(TAG, "Серійний номер недоступний без відповідних прав", e)
            null
        }
    }

    companion object {
        private const val TAG = "FleetDeviceInfo"
        const val MODE_DEVICE_OWNER = "device_owner"
        const val MODE_PROFILE_OWNER = "profile_owner"
        const val MODE_UNMANAGED = "unmanaged"
    }
}
