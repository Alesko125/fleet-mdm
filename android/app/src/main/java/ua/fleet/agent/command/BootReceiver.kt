package ua.fleet.agent.command

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import ua.fleet.agent.data.AgentStore

/** Піднімає сервіс агента після перезавантаження — якщо його не вимкнено. */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED &&
            intent.action != Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            return
        }
        val store = AgentStore(context)
        if (store.isEnrolled && store.serviceEnabled) {
            AgentService.start(context)
        }
    }
}
