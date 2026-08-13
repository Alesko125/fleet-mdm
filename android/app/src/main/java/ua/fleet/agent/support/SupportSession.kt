package ua.fleet.agent.support

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Параметри сеансу віддаленої підтримки, які прийшли в команді.
 *
 * Тримаються в памʼяті процесу й показуються користувачу на екрані згоди —
 * жодного «тихого» сеансу без цих даних бути не може.
 */
@Serializable
data class SupportSessionRequest(
    @SerialName("session_id") val sessionId: String,
    val reason: String,
    @SerialName("requested_by") val requestedBy: String,
    @SerialName("input_requested") val inputRequested: Boolean = false,
    @SerialName("join_secret") val joinSecret: String? = null,
    @SerialName("websocket_url") val websocketUrl: String,
)

/**
 * Поточний стан сеансу на пристрої — джерело правди для UI та сервісу.
 *
 * Цей стан навмисно видимий користувачу: головний екран агента показує, чи
 * триває сеанс, хто його запитав і скільки він уже йде. «Тихого» сеансу,
 * про який власник пристрою не знає, у системі не буває.
 */
object SupportSessionState {

    @Volatile
    var current: SupportSessionRequest? = null
        private set

    /** Кадри реально захоплюються й надсилаються. */
    @Volatile
    var streaming: Boolean = false
        private set

    /** Канал до сервера відкритий (може бути відкритий і без кадрів). */
    @Volatile
    var connected: Boolean = false
        private set

    /** Коли сеанс почався — для лічильника тривалості на екрані агента. */
    @Volatile
    var startedAtMillis: Long = 0L
        private set

    /** Скільки разів довелося перепідключатися — видно користувачу. */
    @Volatile
    var reconnects: Int = 0
        private set

    /** Чи запитував адміністратор віддалене керування. */
    @Volatile
    var inputRequested: Boolean = false
        private set

    @Volatile
    var lastError: String? = null

    /** Слухачі UI: екран агента оновлюється без опитування. */
    private val listeners = java.util.concurrent.CopyOnWriteArrayList<() -> Unit>()

    fun addListener(listener: () -> Unit) {
        listeners.add(listener)
    }

    fun removeListener(listener: () -> Unit) {
        listeners.remove(listener)
    }

    private fun notifyListeners() {
        listeners.forEach { runCatching { it() } }
    }

    fun begin(request: SupportSessionRequest) {
        current = request
        inputRequested = request.inputRequested
        startedAtMillis = System.currentTimeMillis()
        reconnects = 0
        lastError = null
        notifyListeners()
    }

    fun markStreaming(active: Boolean) {
        if (streaming == active) return
        streaming = active
        notifyListeners()
    }

    fun markConnected(active: Boolean) {
        if (connected == active) return
        connected = active
        notifyListeners()
    }

    fun noteReconnect() {
        reconnects += 1
        notifyListeners()
    }

    /** Скільки секунд триває сеанс (0, якщо сеансу немає). */
    fun elapsedSeconds(): Long {
        if (current == null || startedAtMillis == 0L) return 0
        return (System.currentTimeMillis() - startedAtMillis) / 1000
    }

    fun clear() {
        current = null
        streaming = false
        connected = false
        startedAtMillis = 0L
        inputRequested = false
        notifyListeners()
    }
}
