package ua.fleet.agent.files

import android.content.Context
import android.util.Log
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import ua.fleet.agent.net.ApiClient
import ua.fleet.agent.net.ServerUrlGuard
import ua.fleet.agent.net.dto.TransferCompleteDto
import ua.fleet.agent.net.dto.TransferFailDto
import ua.fleet.agent.net.dto.TransferProgressDto

/**
 * Передача файлів між пристроєм і сервером із відновленням після обриву.
 *
 * * **Вивантаження з пристрою** — чанки з явним зсувом; перед стартом агент
 *   питає сервер, скільки байтів уже прийнято, і продовжує саме звідти.
 * * **Запис на пристрій** — HTTP `Range`, тимчасовий файл поруч із цільовим;
 *   у цільове місце файл переїжджає лише після збігу контрольної суми.
 */
class FileTransferClient(
    context: Context,
    private val manager: FileManager,
) {

    private val appContext = context.applicationContext
    private val octetStream = "application/octet-stream".toMediaType()

    data class Outcome(
        val ok: Boolean,
        val bytes: Long = 0,
        val sha256: String? = null,
        val error: String? = null,
    )

    // ------------------------------------------- пристрій → сервер --------

    suspend fun uploadFromDevice(
        serverUrl: String,
        bearer: String,
        statusUrl: String,
        chunkUrl: String,
        completeUrl: String,
        file: File,
        maxBytes: Long,
        onProgress: suspend (Int) -> Unit,
    ): Outcome = withContext(Dispatchers.IO) {
        // Кожен URL із payload команди звіряється з адресою власного
        // сервера: інакше токен пристрою пішов би туди, куди скаже команда.
        ServerUrlGuard.require(statusUrl)
        ServerUrlGuard.require(chunkUrl)
        ServerUrlGuard.require(completeUrl)

        val api = ApiClient.createRaw()
        val total = file.length()
        if (total > maxBytes) {
            return@withContext Outcome(
                ok = false,
                error = "Файл більший за дозволені ${maxBytes / (1024 * 1024)} МБ",
            )
        }

        // Скільки вже дійшло до сервера — звідси й продовжуємо.
        val status = api.transferStatus(statusUrl, bearer)
        var offset = status.receivedBytes
        val chunkSize = status.chunkSize.coerceAtLeast(64 * 1024)

        if (offset > total) {
            return@withContext Outcome(false, error = "Файл змінився під час передачі")
        }

        file.inputStream().use { input ->
            var skipped = 0L
            while (skipped < offset) {
                val step = input.skip(offset - skipped)
                if (step <= 0) break
                skipped += step
            }

            val buffer = ByteArray(chunkSize)
            while (offset < total) {
                val read = input.read(buffer)
                if (read <= 0) break
                val body = buffer.copyOf(read).toRequestBody(octetStream)
                val accepted = api.uploadChunk(chunkUrl, bearer, offset, body)
                offset = accepted.receivedBytes
                onProgress(((offset * 100) / total.coerceAtLeast(1)).toInt())
            }
        }

        val digest = manager.sha256(file)
        val done = api.transferComplete(
            completeUrl,
            bearer,
            TransferCompleteDto(sha256 = digest, sizeBytes = total),
        )
        if (done.status == "completed") {
            Outcome(ok = true, bytes = total, sha256 = digest)
        } else {
            Outcome(ok = false, error = "Сервер не прийняв файл: ${done.status}")
        }
    }

    // ------------------------------------------- сервер → пристрій --------

    suspend fun downloadToDevice(
        bearer: String,
        contentUrl: String,
        progressUrl: String,
        completeUrl: String,
        target: File,
        expectedSize: Long?,
        expectedSha256: String?,
        maxBytes: Long,
        onProgress: suspend (Int) -> Unit,
    ): Outcome = withContext(Dispatchers.IO) {
        ServerUrlGuard.require(contentUrl)
        ServerUrlGuard.require(progressUrl)
        ServerUrlGuard.require(completeUrl)

        if (expectedSize != null && expectedSize > maxBytes) {
            return@withContext Outcome(
                ok = false,
                error = "Файл більший за дозволені ${maxBytes / (1024 * 1024)} МБ",
            )
        }

        val api = ApiClient.createRaw()
        val partial = File(target.parentFile, "${target.name}.fleetpart")
        var downloaded = if (partial.exists()) partial.length() else 0L

        // Якщо частина вже є — просимо лише залишок.
        val range = if (downloaded > 0) "bytes=$downloaded-" else null
        val body = api.downloadRange(contentUrl, bearer, range)

        body.byteStream().use { input ->
            java.io.FileOutputStream(partial, downloaded > 0).use { output ->
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                var lastReported = -1
                while (true) {
                    val read = input.read(buffer)
                    if (read == -1) break
                    output.write(buffer, 0, read)
                    downloaded += read

                    if (downloaded > maxBytes) {
                        partial.delete()
                        return@withContext Outcome(false, error = "Перевищено дозволений розмір")
                    }
                    if (expectedSize != null && expectedSize > 0) {
                        val percent = ((downloaded * 100) / expectedSize).toInt().coerceIn(0, 100)
                        if (percent != lastReported && percent % 5 == 0) {
                            lastReported = percent
                            onProgress(percent)
                            runCatching {
                                api.transferProgress(
                                    progressUrl, bearer, TransferProgressDto(downloaded)
                                )
                            }
                        }
                    }
                }
            }
        }

        val digest = manager.sha256(partial)
        if (expectedSha256 != null && !digest.equals(expectedSha256, ignoreCase = true)) {
            partial.delete()
            runCatching {
                api.transferFail(
                    completeUrl.replace("/complete", "/fail"),
                    bearer,
                    TransferFailDto("Контрольна сума на пристрої не збіглася"),
                )
            }
            return@withContext Outcome(false, error = "Контрольна сума не збіглася")
        }

        if (target.exists() && !target.delete()) {
            partial.delete()
            return@withContext Outcome(false, error = "Не вдалося замінити наявний файл")
        }
        if (!partial.renameTo(target)) {
            partial.delete()
            return@withContext Outcome(false, error = "Не вдалося записати файл у цільовий шлях")
        }

        val done = api.transferComplete(
            completeUrl, bearer, TransferCompleteDto(sha256 = digest, sizeBytes = downloaded)
        )
        Log.i(TAG, "Файл записано: ${target.absolutePath} (${done.status})")
        Outcome(ok = done.status == "completed", bytes = downloaded, sha256 = digest)
    }

    suspend fun reportFailure(bearer: String, failUrl: String, error: String) {
        if (!ServerUrlGuard.isTrusted(failUrl)) {
            Log.w(TAG, "Пропускаємо звіт про помилку: посилання не з нашого сервера")
            return
        }
        runCatching {
            ApiClient.createRaw().transferFail(failUrl, bearer, TransferFailDto(error.take(2000)))
        }.onFailure { Log.w(TAG, "Не вдалося повідомити про збій передачі", it) }
    }

    private companion object {
        const val TAG = "FleetFileTransfer"
    }
}
