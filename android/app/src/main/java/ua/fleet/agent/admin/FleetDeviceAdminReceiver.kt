package ua.fleet.agent.admin

import android.app.admin.DeviceAdminReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import android.widget.Toast

/**
 * Приймач подій Device Admin / Device Owner.
 *
 * Використовуються лише задокументовані можливості DevicePolicyManager.
 * Користувач бачить статус керування в системних налаштуваннях і на
 * головному екрані агента.
 */
class FleetDeviceAdminReceiver : DeviceAdminReceiver() {

    override fun onEnabled(context: Context, intent: Intent) {
        Log.i(TAG, "Device Admin увімкнено")
        Toast.makeText(context, "Керування пристроєм активовано", Toast.LENGTH_LONG).show()
    }

    override fun onDisabled(context: Context, intent: Intent) {
        Log.i(TAG, "Device Admin вимкнено")
    }

    override fun onDisableRequested(context: Context, intent: Intent): CharSequence =
        "Після вимкнення пристрій втратить звʼязок із сервером керування."

    override fun onProfileProvisioningComplete(context: Context, intent: Intent) {
        Log.i(TAG, "Provisioning завершено — можна реєструвати пристрій")
    }

    private companion object {
        const val TAG = "FleetDeviceAdmin"
    }
}
