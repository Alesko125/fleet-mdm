package ua.fleet.agent.apps

import android.content.Context
import android.util.Log
import java.io.File
import java.security.MessageDigest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import ua.fleet.agent.files.FilePathGuard
import ua.fleet.agent.net.ApiClient
import ua.fleet.agent.net.ServerUrlGuard

/**
 * Завантаження APK із сервера з перевіркою контрольної суми.
 *
 * Файл потрапляє у приватний кеш застосунку; жоден інший застосунок до нього
 * доступу не має. Якщо SHA-256 не збігається — файл видаляється й операція
 * вважається невдалою.
 */
class ApkDownloader(private val context: Context) {

    class ChecksumMismatchException(expected: String, actual: String) :
        IllegalStateException("Контрольна сума не збігається: очікували $expected, отримали $actual")

    /**
     * @param onProgress отримує 0..100 (лише етап завантаження)
     * @return файл у кеші, який слід видалити після встановлення
     */
    suspend fun download(
        url: String,
        bearerToken: String,
        expectedSha256: String,
        expectedSize: Long?,
        onProgress: suspend (Int) -> Unit,
    ): File = withContext(Dispatchers.IO) {
        // Контрольна сума приходить у payload команди й потрапляє в ім'я
        // файла — тож спершу переконуємось, що це справді hex, інакше нею
        // можна було б вийти за межі каталогу або підмішати символи оболонки.
        val digestHex = FilePathGuard.requireSha256(expectedSha256)
        // Той самий сервер, що видав enrollment: токен пристрою не має
        // потрапити на сторонній хост, навіть якщо URL прийшов у команді.
        val safeUrl = ServerUrlGuard.require(url)

        val cacheDir = File(context.cacheDir, "apk").apply { mkdirs() }
        val target = File(cacheDir, "${digestHex.take(16)}.apk")
        target.delete()

        val api = ApiClient.createRaw()
        val body = api.downloadApk(safeUrl, "Bearer $bearerToken")
        val total = expectedSize ?: body.contentLength().takeIf { it > 0 }

        val digest = MessageDigest.getInstance("SHA-256")
        var downloaded = 0L
        var lastReported = -1

        body.byteStream().use { input ->
            target.outputStream().use { output ->
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                while (true) {
                    val read = input.read(buffer)
                    if (read == -1) break
                    output.write(buffer, 0, read)
                    digest.update(buffer, 0, read)
                    downloaded += read

                    if (total != null && total > 0) {
                        val percent = ((downloaded * 100) / total).toInt().coerceIn(0, 100)
                        if (percent != lastReported && percent % 5 == 0) {
                            lastReported = percent
                            onProgress(percent)
                        }
                    }
                }
            }
        }

        val actual = digest.digest().joinToString("") { "%02x".format(it) }
        if (!actual.equals(digestHex, ignoreCase = true)) {
            target.delete()
            throw ChecksumMismatchException(digestHex, actual)
        }

        Log.i(TAG, "APK завантажено та перевірено: ${target.name} ($downloaded байт)")
        target
    }

    fun clearCache() {
        File(context.cacheDir, "apk").listFiles()?.forEach { it.delete() }
    }

    private companion object {
        const val TAG = "FleetApkDownloader"
    }
}
