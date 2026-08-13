package ua.fleet.agent.command

import android.util.Log
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import ua.fleet.agent.net.ApiClient

/**
 * WebSocket-канал команд: сервер повідомляє про нову команду одразу, без
 * очікування наступного циклу опитування. Опитування лишається запасним
 * каналом, тож втрата зʼєднання нічого не ламає.
 */
class CommandSocket(
    private val urlProvider: () -> String,
    private val tokenProvider: suspend () -> String,
    private val onCommandAvailable: () -> Unit,
    private val scope: CoroutineScope,
) {

    private val client = OkHttpClient.Builder()
        .pingInterval(30, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private var webSocket: WebSocket? = null
    private var closed = false
    private var retryDelayMs = INITIAL_RETRY_MS

    fun connect() {
        closed = false
        scope.launch { openConnection() }
    }

    private suspend fun openConnection() {
        if (closed) return
        val token = runCatching { tokenProvider() }.getOrElse { error ->
            Log.w(TAG, "Немає токена для WS", error)
            scheduleReconnect()
            return
        }

        val request = Request.Builder()
            .url("${urlProvider()}?token=$token")
            .build()

        webSocket = client.newWebSocket(
            request,
            object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    Log.i(TAG, "WS підключено")
                    retryDelayMs = INITIAL_RETRY_MS
                    // Одразу забираємо те, що накопичилося офлайн.
                    onCommandAvailable()
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    val type = runCatching {
                        val payload = ApiClient.json.parseToJsonElement(text) as? JsonObject
                        payload?.get("type")?.jsonPrimitive?.content
                    }.getOrNull()

                    when (type) {
                        "command" -> onCommandAvailable()
                        "hello", "heartbeat_ack" -> Unit
                        else -> Log.d(TAG, "WS повідомлення: $type")
                    }
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    Log.w(TAG, "WS помилка: ${t.message}")
                    scheduleReconnect()
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    Log.i(TAG, "WS закрито: $code $reason")
                    scheduleReconnect()
                }
            },
        )
    }

    private fun scheduleReconnect() {
        if (closed) return
        val delayMs = retryDelayMs
        retryDelayMs = (retryDelayMs * 2).coerceAtMost(MAX_RETRY_MS)
        scope.launch {
            delay(delayMs)
            openConnection()
        }
    }

    fun sendHeartbeat() {
        webSocket?.send("""{"type":"heartbeat"}""")
    }

    fun close() {
        closed = true
        webSocket?.close(1000, "agent_stopped")
        webSocket = null
    }

    private companion object {
        const val TAG = "FleetCommandSocket"
        const val INITIAL_RETRY_MS = 5_000L
        const val MAX_RETRY_MS = 300_000L
    }
}
