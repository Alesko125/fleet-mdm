package ua.fleet.agent.support

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
import okio.ByteString.Companion.toByteString
import ua.fleet.agent.net.ApiClient

/**
 * Канал трансляції екрана.
 *
 * Кадри йдуть бінарними фреймами, керуючі повідомлення — JSON. Розрив
 * звʼязку не завершує сеанс одразу: клієнт перепідключається з наростаючою
 * паузою, а сервер закриє сеанс сам, якщо кадри не надходять достатньо довго.
 */
class SupportSocket(
    private val url: String,
    private val tokenProvider: suspend () -> String,
    private val onQuality: (fps: Int, scalePercent: Int, jpegQuality: Int) -> Unit,
    private val onCommand: (JsonObject) -> Unit,
    private val onClosed: () -> Unit,
    private val scope: CoroutineScope,
    /** Службові дані, які треба надіслати одразу після відкриття каналу. */
    private val onReady: (SupportSocket) -> Unit = {},
) {

    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    @Volatile private var socket: WebSocket? = null
    @Volatile private var closed = false
    private var retryMs = 2000L

    fun connect() {
        closed = false
        scope.launch { open() }
    }

    private suspend fun open() {
        if (closed) return
        val token = runCatching { tokenProvider() }.getOrElse {
            Log.w(TAG, "Немає токена для каналу підтримки", it)
            scheduleReconnect()
            return
        }

        val request = Request.Builder()
            .url("$url?role=device&token=$token")
            .build()

        socket = client.newWebSocket(
            request,
            object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    Log.i(TAG, "Канал підтримки відкрито")
                    retryMs = 2000L
                    SupportSessionState.markStreaming(true)
                    SupportSessionState.markConnected(true)
                    // Роздільність надсилаємо при кожному відкритті: після
                    // перепідключення сервер міг втратити попереднє значення,
                    // а без нього панель не зможе керувати пристроєм.
                    onReady(this@SupportSocket)
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    val payload = runCatching {
                        ApiClient.json.parseToJsonElement(text) as? JsonObject
                    }.getOrNull() ?: return

                    when (payload["type"]?.jsonPrimitive?.content) {
                        "quality" -> onQuality(
                            payload["max_fps"]?.jsonPrimitive?.content?.toIntOrNull() ?: 4,
                            payload["scale_percent"]?.jsonPrimitive?.content?.toIntOrNull() ?: 50,
                            payload["jpeg_quality"]?.jsonPrimitive?.content?.toIntOrNull() ?: 60,
                        )
                        "input" -> onCommand(payload)
                        "ready" -> Log.i(TAG, "Сервер готовий приймати кадри")
                        else -> Unit
                    }
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    Log.w(TAG, "Канал підтримки обірвано: ${t.message}")
                    SupportSessionState.markStreaming(false)
                    SupportSessionState.markConnected(false)
                    SupportSessionState.noteReconnect()
                    scheduleReconnect()
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    Log.i(TAG, "Канал закрито: $code $reason")
                    SupportSessionState.markStreaming(false)
                    SupportSessionState.markConnected(false)
                    if (code == 4409 || code == 4403) {
                        // Сеанс завершено або згоду відкликано — не перепідключаємось.
                        closed = true
                        onClosed()
                    } else {
                        scheduleReconnect()
                    }
                }
            },
        )
    }

    private fun scheduleReconnect() {
        if (closed) return
        val delayMs = retryMs
        retryMs = (retryMs * 2).coerceAtMost(30_000L)
        scope.launch {
            delay(delayMs)
            open()
        }
    }

    fun sendFrame(bytes: ByteArray): Boolean =
        socket?.send(bytes.toByteString()) ?: false

    /** Надсилає службове текстове повідомлення (роздільність, статистика). */
    fun sendText(payload: String): Boolean = socket?.send(payload) ?: false

    fun close() {
        closed = true
        socket?.close(1000, "session_finished")
        socket = null
        SupportSessionState.markStreaming(false)
        SupportSessionState.markConnected(false)
    }

    private companion object {
        const val TAG = "FleetSupportSocket"
    }
}
