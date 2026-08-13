package ua.fleet.agent.files

import java.io.File
import java.io.IOException

/**
 * Локальна перевірка allowlist на самому пристрої.
 *
 * Сервер уже перевірив шлях, але агент **не покладається** на це наосліп:
 * перед будь-якою операцією він самостійно звіряє канонічний шлях із
 * переліком дозволених каталогів. Канонізація (`canonicalPath`) розгортає
 * символьні посилання — саме тому симлінк із дозволеного каталогу назовні
 * не дає доступу до чужих даних.
 */
object FilePathGuard {

    class Violation(message: String) : SecurityException(message)

    /**
     * Метасимволи оболонки. Привілейовані операції виконуються через `su`,
     * тож рядок, який туди потрапляє, не має містити нічого, що оболонка
     * могла б витлумачити як команду. Аргументи ми ще й екрануємо
     * ([shellQuote]), але приймати такі шляхи взагалі немає причин:
     * у легальних каталогах керованого пристрою вони не трапляються.
     */
    private val SHELL_METACHARACTERS: Set<Char> = setOf(
        '"', '\'', '`', '$', ';', '&', '|', '<', '>', '(', ')',
        '[', ']', '{', '}', '*', '?', '!', '#', '~', '\n', '\r', '\t',
    )

    data class Root(
        val path: String,
        val writable: Boolean,
        val allowPrivileged: Boolean,
        val maxFileSizeMb: Int?,
    )

    /**
     * @return канонічний шлях і каталог, якому він належить
     * @throws Violation якщо шлях поза allowlist або потребує запису в
     *         каталозі «лише для читання»
     */
    fun resolve(rawPath: String, roots: List<Root>, needWrite: Boolean): Pair<File, Root> {
        if (roots.isEmpty()) {
            throw Violation("Для пристрою не налаштовано жодного дозволеного каталогу")
        }
        val target = File(rawPath)
        // На Android це завжди POSIX-шлях; isAbsolute лишає тест переносним.
        if (!target.isAbsolute) {
            throw Violation("Очікується абсолютний шлях")
        }
        if (rawPath.any { it.code == 0 }) {
            throw Violation("Шлях містить нульовий байт")
        }
        if (rawPath.split(SLASH, BACKSLASH).any { it == ".." }) {
            throw Violation("Переходи `..` заборонені")
        }
        rejectShellMetacharacters(rawPath)
        val canonical = try {
            // Для ще неіснуючого файла канонізуємо батьківський каталог.
            if (target.exists()) target.canonicalFile
            else File(target.parentFile?.canonicalFile ?: target.parentFile, target.name)
        } catch (e: IOException) {
            throw Violation("Не вдалося визначити канонічний шлях: ${e.message}")
        }

        val match = roots.firstOrNull { root -> isWithin(root.path, canonical.absolutePath) }
            ?: throw Violation("Шлях поза дозволеними каталогами")

        if (needWrite && !match.writable) {
            throw Violation("Каталог ${match.path} доступний лише для читання")
        }
        return canonical to match
    }

    fun isWithin(root: String, path: String): Boolean {
        // На Android шляхи POSIX; заміна роздільника потрібна лише для того,
        // щоб ту саму логіку можна було прогнати в JVM-тестах на Windows.
        val normalizedRoot = root.replace(BACKSLASH, SLASH).trimEnd(SLASH)
        val normalizedPath = path.replace(BACKSLASH, SLASH)
        if (normalizedRoot.isEmpty()) return false
        return normalizedPath == normalizedRoot ||
            normalizedPath.startsWith(normalizedRoot + SLASH)
    }

    private const val SLASH = '/'
    /** Зворотний слеш через код символу: у Kotlin-літералі він потребує екранування. */
    private val BACKSLASH: Char = 92.toChar()

    /** Перевірка імені для rename/mkdir — без роздільників і керуючих символів. */
    fun validateName(name: String): String {
        val trimmed = name.trim()
        if (trimmed.isEmpty() || trimmed == "." || trimmed == "..") {
            throw Violation("Некоректне імʼя")
        }
        if (trimmed.length > 255) throw Violation("Імʼя задовге")
        if (trimmed.any { it == SLASH || it == BACKSLASH || it.code < 0x20 || it.code == 0x7f }) {
            throw Violation("Імʼя містить неприпустимі символи")
        }
        rejectShellMetacharacters(trimmed)
        return trimmed
    }

    /** Відхиляє символи, які оболонка витлумачила б як синтаксис. */
    fun rejectShellMetacharacters(value: String) {
        val found = value.filter { it in SHELL_METACHARACTERS }
        if (found.isNotEmpty()) {
            throw Violation("Шлях містить символи оболонки: ${found.toSortedSet().joinToString(" ")}")
        }
    }

    /**
     * Бере рядок в одинарні лапки для POSIX-оболонки.
     *
     * Усередині одинарних лапок оболонка не інтерпретує нічого; сама лапка
     * закривається, додається літералом і лапки відкриваються знову.
     * Це — другий рубіж після [rejectShellMetacharacters].
     */
    fun shellQuote(value: String): String = "'" + value.replace("'", "'\\''") + "'"

    /** Перевіряє, що рядок — це справді шістнадцятковий SHA-256. */
    fun requireSha256(value: String): String {
        val trimmed = value.trim().lowercase()
        if (trimmed.length != 64 || !trimmed.all { it in '0'..'9' || it in 'a'..'f' }) {
            throw Violation("Очікується SHA-256 у шістнадцятковому вигляді")
        }
        return trimmed
    }
}
