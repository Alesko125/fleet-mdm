package ua.fleet.agent.net

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * Прив'язка абсолютних URL із команд до власного сервера агента.
 *
 * Частина payload команд (`download_url`, `chunk_url`, `content_url`…) — це
 * готові посилання, за якими агент ходить **зі своїм Bearer-токеном**. Якщо
 * приймати їх наосліп, достатньо одного некоректного посилання, щоб токен
 * пристрою поїхав на чужий хост. Тому перед кожним таким запитом URL
 * звіряється з адресою сервера, отриманою під час enrollment: інший хост,
 * інший порт або пониження до http — і запит просто не відбувається.
 *
 * Базова адреса задається один раз при старті агента ([bind]) і змінюється
 * лише при повторній реєстрації.
 */
object ServerUrlGuard {

    class UntrustedUrlException(url: String) :
        SecurityException("Посилання не належить серверу керування: $url")

    @Volatile
    private var origin: HttpUrl? = null

    /** Запам'ятовує адресу власного сервера (виклик після enrollment/старту). */
    fun bind(serverUrl: String?) {
        origin = serverUrl?.trim()?.takeIf { it.isNotEmpty() }?.toHttpUrlOrNull()
    }

    fun boundOrigin(): String? = origin?.let { "${it.scheme}://${it.host}:${it.port}" }

    /** @return той самий URL, якщо він веде на сервер керування. */
    fun require(url: String): String {
        if (!isTrusted(url)) throw UntrustedUrlException(url)
        return url
    }

    fun isTrusted(url: String): Boolean {
        val expected = origin ?: return false
        val candidate = url.toHttpUrlOrNull() ?: return false

        // Схему не можна знижувати: якщо сервер на https, посилання теж має
        // бути https, інакше токен пішов би відкритим текстом.
        if (candidate.scheme != expected.scheme) return false
        // Порівняння хоста — точне, без «закінчується на»: `evil-example.com`
        // не має проходити перевірку на `example.com`.
        if (!candidate.host.equals(expected.host, ignoreCase = true)) return false
        if (candidate.port != expected.port) return false
        // Облікові дані в URL — ознака підробки, легальні посилання їх не мають.
        if (candidate.username.isNotEmpty() || candidate.password.isNotEmpty()) return false
        return true
    }
}
