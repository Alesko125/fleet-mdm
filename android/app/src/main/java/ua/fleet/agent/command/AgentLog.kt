package ua.fleet.agent.command

import android.util.Log
import java.text.SimpleDateFormat
import java.util.ArrayDeque
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Кільцевий буфер власного журналу агента.
 *
 * Потрібен для команди «зібрати діагностику»: адміністратор має бачити, що
 * саме робив агент, не читаючи системний `logcat`. Це принципова різниця —
 * системний журнал містить записи **всіх** застосунків, зокрема чужі й
 * чутливі, тож агент його не читає й не пересилає. Тут лише те, що агент
 * записав про себе сам.
 *
 * Буфер живе в памʼяті процесу й не переживає перезапуск: це діагностика
 * «що відбувається зараз», а не архів.
 */
object AgentLog {

    private const val CAPACITY = 500
    private const val MAX_LINE_LENGTH = 300

    private val buffer = ArrayDeque<String>(CAPACITY)
    private val lock = Any()

    private val formatter = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply {
        timeZone = TimeZone.getTimeZone("UTC")
    }

    /** Записує рядок і у власний буфер, і у звичайний logcat. */
    fun info(tag: String, message: String) = append("INFO", tag, message).also {
        Log.i(tag, message)
    }

    fun warn(tag: String, message: String, error: Throwable? = null) {
        append("WARN", tag, if (error == null) message else "$message: ${error.message}")
        Log.w(tag, message, error)
    }

    private fun append(level: String, tag: String, message: String) {
        val line = buildString {
            append(formatter.format(Date()))
            append(' ')
            append(level)
            append(' ')
            append(tag)
            append(": ")
            append(message.take(MAX_LINE_LENGTH))
        }
        synchronized(lock) {
            if (buffer.size >= CAPACITY) buffer.removeFirst()
            buffer.addLast(line)
        }
    }

    /** Останні `count` рядків у хронологічному порядку. */
    fun recent(count: Int): List<String> = synchronized(lock) {
        if (count <= 0) return emptyList()
        buffer.toList().takeLast(count)
    }

    fun size(): Int = synchronized(lock) { buffer.size }

    fun clear() = synchronized(lock) { buffer.clear() }
}
