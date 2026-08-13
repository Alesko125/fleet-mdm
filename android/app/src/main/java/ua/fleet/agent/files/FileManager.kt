package ua.fleet.agent.files

import android.util.Log
import java.io.File
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import ua.fleet.agent.net.dto.FileEntryDto

/**
 * Операції з файлами в межах дозволених каталогів.
 *
 * Кожна операція починається з [FilePathGuard.resolve] — без цього жоден
 * шлях не приймається. Привілейований (`su`) режим використовується лише
 * для каталогів, у яких адміністратор явно поставив `allow_privileged`,
 * і лише коли звичайний доступ не спрацював.
 */
class FileManager(private val privilegedEnabled: () -> Boolean = { false }) {

    data class Result(val ok: Boolean, val message: String? = null, val mechanism: String = "app")

    // ------------------------------------------------------------ перелік --

    suspend fun list(path: String, roots: List<FilePathGuard.Root>): List<FileEntryDto> =
        withContext(Dispatchers.IO) {
            val (dir, root) = FilePathGuard.resolve(path, roots, needWrite = false)
            if (!dir.exists()) throw NoSuchFileException(dir)
            if (!dir.isDirectory) throw FilePathGuard.Violation("Це не каталог")

            val children = dir.listFiles()
                ?: if (root.allowPrivileged && privilegedEnabled()) {
                    return@withContext listPrivileged(dir)
                } else {
                    throw FilePathGuard.Violation("Немає доступу до вмісту каталогу")
                }

            children
                .filter { child ->
                    // Симлінк, що веде за межі allowlist, у переліку не показуємо.
                    runCatching {
                        FilePathGuard.isWithin(root.path, child.canonicalPath)
                    }.getOrDefault(false)
                }
                .map { it.toDto() }
                .sortedWith(compareByDescending<FileEntryDto> { it.isDir }.thenBy { it.name })
        }

    private fun listPrivileged(dir: File): List<FileEntryDto> {
        // `ls -la` через su: розбираємо лише імена, метадані беремо з File.
        val output = runShell("ls", "-a", dir.absolutePath) ?: return emptyList()
        return output.lines()
            .map { it.trim() }
            .filter { it.isNotEmpty() && it != "." && it != ".." }
            .map { File(dir, it).toDto() }
            .sortedWith(compareByDescending<FileEntryDto> { it.isDir }.thenBy { it.name })
    }

    private fun File.toDto(): FileEntryDto {
        val symlink = runCatching { canonicalFile != absoluteFile }.getOrDefault(false)
        return FileEntryDto(
            name = name,
            path = absolutePath,
            isDir = isDirectory,
            sizeBytes = if (isFile) length() else null,
            modifiedAt = formatTimestamp(lastModified()),
            permissions = buildString {
                append(if (canRead()) "r" else "-")
                append(if (canWrite()) "w" else "-")
                append(if (isDirectory) "d" else "-")
            },
            isSymlink = symlink,
            readable = canRead(),
            writable = canWrite(),
        )
    }

    // -------------------------------------------------------- модифікації --

    suspend fun makeDirectory(path: String, roots: List<FilePathGuard.Root>): Result =
        withContext(Dispatchers.IO) {
            val (target, root) = FilePathGuard.resolve(path, roots, needWrite = true)
            if (target.exists()) return@withContext Result(true, "Каталог уже існує")
            if (target.mkdirs()) return@withContext Result(true)

            if (root.allowPrivileged && privilegedEnabled()) {
                val ok = runShell("mkdir", "-p", target.absolutePath) != null
                return@withContext Result(ok, if (ok) null else "mkdir не вдався", "su")
            }
            Result(false, "Не вдалося створити каталог")
        }

    suspend fun rename(path: String, targetPath: String, roots: List<FilePathGuard.Root>): Result =
        withContext(Dispatchers.IO) {
            val (source, root) = FilePathGuard.resolve(path, roots, needWrite = true)
            val (destination, _) = FilePathGuard.resolve(targetPath, roots, needWrite = true)

            if (!source.exists()) return@withContext Result(false, "Файл не знайдено")
            if (destination.exists()) return@withContext Result(false, "Ціль уже існує")

            if (source.renameTo(destination)) return@withContext Result(true)

            if (root.allowPrivileged && privilegedEnabled()) {
                val ok = runShell(
                    "mv", source.absolutePath, destination.absolutePath
                ) != null
                return@withContext Result(ok, if (ok) null else "mv не вдався", "su")
            }
            Result(false, "Не вдалося перейменувати")
        }

    suspend fun delete(
        path: String,
        recursive: Boolean,
        roots: List<FilePathGuard.Root>,
    ): Result = withContext(Dispatchers.IO) {
        val (target, root) = FilePathGuard.resolve(path, roots, needWrite = true)
        if (target.absolutePath.trimEnd('/') == root.path.trimEnd('/')) {
            return@withContext Result(false, "Не можна видалити сам дозволений каталог")
        }
        if (!target.exists()) return@withContext Result(true, "Уже відсутній")

        val deleted = if (target.isDirectory && recursive) {
            target.deleteRecursively()
        } else {
            target.delete()
        }
        if (deleted) return@withContext Result(true)

        if (root.allowPrivileged && privilegedEnabled()) {
            val flags = if (recursive) "-rf" else "-f"
            val ok = runShell("rm", flags, target.absolutePath) != null
            return@withContext Result(ok, if (ok) null else "rm не вдався", "su")
        }
        Result(false, "Не вдалося видалити")
    }

    // ------------------------------------------------------------ читання --

    suspend fun openForRead(path: String, roots: List<FilePathGuard.Root>): File =
        withContext(Dispatchers.IO) {
            val (file, _) = FilePathGuard.resolve(path, roots, needWrite = false)
            if (!file.isFile) throw FilePathGuard.Violation("Це не файл")
            if (!file.canRead()) throw FilePathGuard.Violation("Немає прав на читання файла")
            file
        }

    suspend fun prepareForWrite(path: String, roots: List<FilePathGuard.Root>): File =
        withContext(Dispatchers.IO) {
            val (file, _) = FilePathGuard.resolve(path, roots, needWrite = true)
            file.parentFile?.mkdirs()
            file
        }

    suspend fun sha256(file: File): String = withContext(Dispatchers.IO) {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val read = input.read(buffer)
                if (read == -1) break
                digest.update(buffer, 0, read)
            }
        }
        digest.digest().joinToString("") { "%02x".format(it) }
    }

    // -------------------------------------------------- привілейований режим

    /**
     * Виконує команду через `su`. Використовується лише для каталогів із
     * `allow_privileged` і завжди повідомляється серверу як `mechanism=su`.
     *
     * Аргументи приймаються окремим списком і склеюються в рядок **лише**
     * після екранування ([FilePathGuard.shellQuote]): збирати команду
     * підстановкою шляху в рядок не можна — шлях приходить із сервера, і
     * лапка всередині нього перетворила б його на довільну команду root.
     * Ім'я самої утиліти має бути з фіксованого переліку.
     */
    private fun runShell(tool: String, vararg arguments: String): String? {
        require(tool in ALLOWED_TOOLS) { "Недозволена привілейована утиліта: $tool" }
        arguments.forEach { argument ->
            // Прапорці (-rf, -a, -p) пропускаємо як є, решту — перевіряємо.
            if (!argument.startsWith("-")) {
                FilePathGuard.rejectShellMetacharacters(argument)
            }
        }
        val command = buildString {
            append(tool)
            arguments.forEach { argument ->
                append(' ')
                append(
                    if (argument.startsWith("-")) argument
                    else FilePathGuard.shellQuote(argument)
                )
            }
        }
        return try {
            val process = ProcessBuilder("su", "-c", command).redirectErrorStream(true).start()
            val output = process.inputStream.bufferedReader().readText()
            if (process.waitFor() == 0) output else null
        } catch (e: Exception) {
            Log.w(TAG, "Привілейована команда не виконалася", e)
            null
        }
    }

    companion object {
        private const val TAG = "FleetFileManager"

        /** Єдині утиліти, які агенту дозволено запускати з правами root. */
        private val ALLOWED_TOOLS = setOf("ls", "mkdir", "mv", "rm")

        fun formatTimestamp(epochMillis: Long): String {
            val format = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
            format.timeZone = TimeZone.getTimeZone("UTC")
            return format.format(Date(epochMillis))
        }
    }
}
