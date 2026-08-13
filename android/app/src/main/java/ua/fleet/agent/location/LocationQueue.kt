package ua.fleet.agent.location

import java.io.File
import kotlinx.serialization.json.Json
import ua.fleet.agent.net.dto.LocationFixDto

/**
 * Офлайн-черга координат.
 *
 * Файл JSON Lines у приватному каталозі застосунку: один рядок — одна
 * координата. Якщо мережі немає, точки накопичуються тут і відправляються
 * пакетом при наступному успішному підключенні. Черга обмежена — коли
 * переповнюється, найстаріші записи відкидаються (свіжі корисніші).
 */
class LocationQueue(private val file: File, private val maxSize: Int = DEFAULT_MAX_SIZE) {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val lock = Any()

    val size: Int
        get() = synchronized(lock) { readLines().size }

    fun add(fix: LocationFixDto) = synchronized(lock) {
        val lines = readLines().toMutableList()
        lines.add(json.encodeToString(LocationFixDto.serializer(), fix))
        writeLines(trim(lines))
    }

    fun addAll(fixes: List<LocationFixDto>) = synchronized(lock) {
        if (fixes.isEmpty()) return
        val lines = readLines().toMutableList()
        fixes.forEach { lines.add(json.encodeToString(LocationFixDto.serializer(), it)) }
        writeLines(trim(lines))
    }

    /** Читає до [limit] найстаріших записів, не видаляючи їх. */
    fun peek(limit: Int): List<LocationFixDto> = synchronized(lock) {
        readLines().take(limit).mapNotNull { line ->
            runCatching { json.decodeFromString(LocationFixDto.serializer(), line) }.getOrNull()
        }
    }

    /** Видаляє [count] найстаріших записів — після успішної відправки. */
    fun drop(count: Int) = synchronized(lock) {
        if (count <= 0) return
        val lines = readLines()
        writeLines(lines.drop(count))
    }

    fun clear() = synchronized(lock) {
        if (file.exists()) file.delete()
    }

    private fun trim(lines: List<String>): List<String> =
        if (lines.size <= maxSize) lines else lines.takeLast(maxSize)

    private fun readLines(): List<String> =
        if (!file.exists()) {
            emptyList()
        } else {
            file.readLines().filter { it.isNotBlank() }
        }

    private fun writeLines(lines: List<String>) {
        if (lines.isEmpty()) {
            file.delete()
            return
        }
        file.parentFile?.mkdirs()
        file.writeText(lines.joinToString("\n", postfix = "\n"))
    }

    companion object {
        const val DEFAULT_MAX_SIZE = 2000
        const val FILE_NAME = "location_queue.jsonl"
    }
}
