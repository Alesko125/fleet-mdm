package ua.fleet.agent.support

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.graphics.Path
import android.provider.Settings
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Віддалене керування — **лише** через штатний API служби доступності.
 *
 * Службу вмикає сам користувач у системних налаштуваннях; без цього жодна
 * дія не виконується, а сервер бачить `input_available=false`. Ніяких
 * обхідних шляхів (root-інʼєкції подій, `input tap` через `su`) тут немає:
 * керування чужим екраном має лишатися явно дозволеним і видимим.
 */
class RemoteInputService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "Службу віддаленого керування підключено")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    private fun tap(x: Float, y: Float) {
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0, TAP_DURATION_MS)
        dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    private fun swipe(fromX: Float, fromY: Float, toX: Float, toY: Float, durationMs: Long) {
        val path = Path().apply {
            moveTo(fromX, fromY)
            lineTo(toX, toY)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    companion object {
        private const val TAG = "FleetRemoteInput"
        private const val TAP_DURATION_MS = 60L

        @Volatile
        private var instance: RemoteInputService? = null

        /** Чи ввімкнув користувач службу в системних налаштуваннях. */
        fun isEnabled(context: Context): Boolean {
            if (instance != null) return true
            val enabled = Settings.Secure.getString(
                context.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
            ) ?: return false
            return enabled.contains(context.packageName + "/" + RemoteInputService::class.java.name)
        }

        /**
         * Виконує дію, що прийшла від оператора.
         *
         * Якщо служба вимкнена — дія просто ігнорується (сервер уже знає, що
         * керування недоступне, і не показує оператору відповідних кнопок).
         */
        fun dispatch(context: Context, message: JsonObject) {
            val service = instance
            if (service == null) {
                Log.i(TAG, "Дію проігноровано: службу доступності не ввімкнено")
                return
            }
            if (!SupportSessionState.streaming) {
                Log.i(TAG, "Дію проігноровано: активного сеансу немає")
                return
            }

            fun number(key: String): Float? =
                message[key]?.jsonPrimitive?.content?.toFloatOrNull()

            when (message["action"]?.jsonPrimitive?.content) {
                "tap" -> {
                    val x = number("x") ?: return
                    val y = number("y") ?: return
                    service.tap(x, y)
                }

                "swipe" -> {
                    val fromX = number("from_x") ?: return
                    val fromY = number("from_y") ?: return
                    val toX = number("to_x") ?: return
                    val toY = number("to_y") ?: return
                    val duration = number("duration_ms")?.toLong() ?: 300L
                    service.swipe(fromX, fromY, toX, toY, duration)
                }

                "back" -> service.performGlobalAction(GLOBAL_ACTION_BACK)
                "home" -> service.performGlobalAction(GLOBAL_ACTION_HOME)
                "recents" -> service.performGlobalAction(GLOBAL_ACTION_RECENTS)
                else -> Log.d(TAG, "Невідома дія віддаленого керування")
            }
        }
    }
}
