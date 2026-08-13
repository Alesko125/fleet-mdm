package ua.fleet.agent.files

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import ua.fleet.agent.command.entriesResult
import ua.fleet.agent.command.toGuardRoots
import ua.fleet.agent.data.AgentStore
import ua.fleet.agent.enroll.EnrollmentRepository
import ua.fleet.agent.net.ApiClient
import ua.fleet.agent.net.dto.CommandResultDto
import ua.fleet.agent.net.dto.PendingCommandDto

/**
 * Виконання файлових команд на пристрої.
 *
 * Перед кожною операцією агент **самостійно** отримує перелік дозволених
 * каталогів із сервера й перевіряє шлях через [FilePathGuard]; payload
 * команди не є єдиним джерелом істини.
 */
class FileCommandExecutor(
    private val store: AgentStore,
    private val repository: EnrollmentRepository,
    private val manager: FileManager,
    private val transfers: FileTransferClient,
) {

    private suspend fun roots(): List<FilePathGuard.Root> {
        val serverUrl = store.serverUrl ?: return emptyList()
        val api = ApiClient.create(serverUrl)
        return api.fileRoots("Bearer " + repository.accessToken()).toGuardRoots()
    }

    /**
     * Перечитує перелік дозволених каталогів із сервера.
     *
     * Використовується командою синхронізації конфігурації.
     *
     * @return скільки каталогів дозволено цьому пристрою
     */
    suspend fun refreshRoots(): Int = roots().size

    // ---------------------------------------------------------- перелік ---

    suspend fun list(command: PendingCommandDto): CommandResultDto {
        val path = command.payload.text("path")
            ?: return failure("BAD_PAYLOAD", "Не вказано шлях")
        return try {
            entriesResult(manager.list(path, roots()), path)
        } catch (e: FilePathGuard.Violation) {
            failure("ACCESS_DENIED", e.message ?: "Доступ заборонено")
        } catch (e: NoSuchFileException) {
            failure("NOT_FOUND", "Не знайдено: " + e.file.absolutePath)
        }
    }

    // ------------------------------------------------------ модифікації ---

    suspend fun mkdir(command: PendingCommandDto): CommandResultDto {
        val path = command.payload.text("path")
            ?: return failure("BAD_PAYLOAD", "Не вказано шлях")
        return try {
            val outcome = manager.makeDirectory(path, roots())
            if (outcome.ok) {
                ok {
                    put("path", JsonPrimitive(path))
                    put("mechanism", JsonPrimitive(outcome.mechanism))
                }
            } else {
                failure("MKDIR_FAILED", outcome.message ?: "Не вдалося створити каталог")
            }
        } catch (e: FilePathGuard.Violation) {
            failure("ACCESS_DENIED", e.message ?: "Доступ заборонено")
        }
    }

    suspend fun rename(command: PendingCommandDto): CommandResultDto {
        val path = command.payload.text("path")
            ?: return failure("BAD_PAYLOAD", "Не вказано шлях")
        val target = command.payload.text("target")
            ?: return failure("BAD_PAYLOAD", "Не вказано ціль")
        return try {
            val outcome = manager.rename(path, target, roots())
            if (outcome.ok) {
                ok {
                    put("from", JsonPrimitive(path))
                    put("to", JsonPrimitive(target))
                    put("mechanism", JsonPrimitive(outcome.mechanism))
                }
            } else {
                failure("RENAME_FAILED", outcome.message ?: "Не вдалося перейменувати")
            }
        } catch (e: FilePathGuard.Violation) {
            failure("ACCESS_DENIED", e.message ?: "Доступ заборонено")
        }
    }

    suspend fun delete(command: PendingCommandDto): CommandResultDto {
        val path = command.payload.text("path")
            ?: return failure("BAD_PAYLOAD", "Не вказано шлях")
        val recursive = command.payload.text("recursive")?.toBoolean() ?: false
        return try {
            val outcome = manager.delete(path, recursive, roots())
            if (outcome.ok) {
                ok {
                    put("path", JsonPrimitive(path))
                    put("mechanism", JsonPrimitive(outcome.mechanism))
                }
            } else {
                failure("DELETE_FAILED", outcome.message ?: "Не вдалося видалити")
            }
        } catch (e: FilePathGuard.Violation) {
            failure("ACCESS_DENIED", e.message ?: "Доступ заборонено")
        }
    }

    // ---------------------------------------------------------- передачі --

    /** Пристрій віддає файл серверу (в панелі це «завантажити з пристрою»). */
    suspend fun uploadToServer(
        command: PendingCommandDto,
        bearer: String,
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        val payload = command.payload ?: return failure("BAD_PAYLOAD", "Команда без параметрів")
        val path = payload.text("path") ?: return failure("BAD_PAYLOAD", "Не вказано шлях")
        val statusUrl = payload.text("status_url")
            ?: return failure("BAD_PAYLOAD", "Немає status_url")
        val chunkUrl = payload.text("chunk_url")
            ?: return failure("BAD_PAYLOAD", "Немає chunk_url")
        val completeUrl = payload.text("complete_url")
            ?: return failure("BAD_PAYLOAD", "Немає complete_url")
        val maxBytes = payload.text("max_bytes")?.toLongOrNull() ?: Long.MAX_VALUE
        val failUrl = completeUrl.replace("/complete", "/fail")

        return try {
            progress(10, "Перевірка доступу до файла")
            val file = manager.openForRead(path, roots())

            progress(20, "Вивантаження на сервер")
            val outcome = transfers.uploadFromDevice(
                serverUrl = store.serverUrl.orEmpty(),
                bearer = bearer,
                statusUrl = statusUrl,
                chunkUrl = chunkUrl,
                completeUrl = completeUrl,
                file = file,
                maxBytes = maxBytes,
            ) { percent -> progress(20 + percent * 75 / 100, "Передано " + percent + "%") }

            if (outcome.ok) {
                ok {
                    put("path", JsonPrimitive(path))
                    put("size_bytes", JsonPrimitive(outcome.bytes))
                    put("sha256", JsonPrimitive(outcome.sha256))
                }
            } else {
                transfers.reportFailure(bearer, failUrl, outcome.error.orEmpty())
                failure("TRANSFER_FAILED", outcome.error ?: "Передача не вдалася")
            }
        } catch (e: FilePathGuard.Violation) {
            transfers.reportFailure(bearer, failUrl, e.message.orEmpty())
            failure("ACCESS_DENIED", e.message ?: "Доступ заборонено")
        }
    }

    /** Сервер передає файл на пристрій (в панелі це «завантажити на пристрій»). */
    suspend fun downloadToDevice(
        command: PendingCommandDto,
        bearer: String,
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        val payload = command.payload ?: return failure("BAD_PAYLOAD", "Команда без параметрів")
        val path = payload.text("path") ?: return failure("BAD_PAYLOAD", "Не вказано шлях")
        val contentUrl = payload.text("content_url")
            ?: return failure("BAD_PAYLOAD", "Немає content_url")
        val completeUrl = payload.text("complete_url")
            ?: return failure("BAD_PAYLOAD", "Немає complete_url")
        val progressUrl = completeUrl.replace("/complete", "/progress")
        val failUrl = completeUrl.replace("/complete", "/fail")
        val sha256 = payload.text("sha256")
        val size = payload.text("size_bytes")?.toLongOrNull()
        val maxBytes = payload.text("max_bytes")?.toLongOrNull() ?: Long.MAX_VALUE

        return try {
            progress(10, "Підготовка цільового каталогу")
            val target = manager.prepareForWrite(path, roots())

            progress(20, "Отримання файла із сервера")
            val outcome = transfers.downloadToDevice(
                bearer = bearer,
                contentUrl = contentUrl,
                progressUrl = progressUrl,
                completeUrl = completeUrl,
                target = target,
                expectedSize = size,
                expectedSha256 = sha256,
                maxBytes = maxBytes,
            ) { percent -> progress(20 + percent * 75 / 100, "Отримано " + percent + "%") }

            if (outcome.ok) {
                ok {
                    put("path", JsonPrimitive(path))
                    put("size_bytes", JsonPrimitive(outcome.bytes))
                    put("sha256", JsonPrimitive(outcome.sha256))
                }
            } else {
                failure("TRANSFER_FAILED", outcome.error ?: "Передача не вдалася")
            }
        } catch (e: FilePathGuard.Violation) {
            transfers.reportFailure(bearer, failUrl, e.message.orEmpty())
            failure("ACCESS_DENIED", e.message ?: "Доступ заборонено")
        }
    }

    // ------------------------------------------------------- допоміжне ----

    private fun JsonObject?.text(key: String): String? {
        val element = this?.get(key) ?: return null
        val content = runCatching { element.jsonPrimitive.content }.getOrNull() ?: return null
        return if (content == "null") null else content
    }

    private inline fun ok(build: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit) =
        CommandResultDto(status = "succeeded", result = buildJsonObject(build))

    private fun failure(code: String, message: String) = CommandResultDto(
        status = "failed",
        error = code + ": " + message,
        result = buildJsonObject { put("code", JsonPrimitive(code)) },
    )
}
