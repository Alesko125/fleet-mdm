package ua.fleet.agent.command

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import ua.fleet.agent.data.AgentStore

/** Піднімає сервіс агента після перезавантаження — якщо його не вимкнено. */
class BootReceiver : BroadcastReceiver() {

    private companion object {
        const val TAG = "FleetBootReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED &&
            intent.action != Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            return
        }
        val store = AgentStore(context)
        if (!store.isEnrolled || !store.serviceEnabled) return

        // Від Android 15 службу типу dataSync не можна підняти з
        // BOOT_COMPLETED — виняток лише для Device Owner. Це не привід
        // падати: запам'ятовуємо відмову, і агент підніметься, щойно
        // власник відкриє застосунок.
        if (AgentService.start(context) == StartOutcome.NOT_ALLOWED) {
            store.foregroundBlockedAtMillis = System.currentTimeMillis()
            Log.i(TAG, "Автозапуск після перезавантаження заборонено системою")
        }
    }
}
