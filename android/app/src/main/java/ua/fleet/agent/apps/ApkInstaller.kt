package ua.fleet.agent.apps

import android.app.PendingIntent
import android.app.admin.DevicePolicyManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import java.io.File
import kotlin.coroutines.resume
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Встановлення, оновлення та видалення застосунків.
 *
 * Використовується виключно задокументований `PackageInstaller`:
 *  * у режимі **Device Owner** система дозволяє тихе встановлення;
 *  * без Device Owner система показує користувачу діалог підтвердження —
 *    ми його не обходимо;
 *  * `su`-механізм є **необовʼязковим** запасним варіантом для власних
 *    рутованих пристроїв, вимкнений за замовчуванням і завжди повідомляється
 *    серверу в полі `mechanism`.
 */
class ApkInstaller(private val context: Context) {

    sealed interface Outcome {
        data class Success(val mechanism: String) : Outcome
        data class Failure(val code: String, val message: String) : Outcome
        data object AwaitingUserConfirmation : Outcome
    }

    data class ArchiveInfo(val packageName: String, val versionCode: Long, val versionName: String?)

    /** Читає метадані завантаженого файла — перевірка, що прийшов саме той пакет. */
    fun inspect(file: File): ArchiveInfo? {
        val pm = context.packageManager
        val info = pm.getPackageArchiveInfo(file.absolutePath, 0) ?: return null
        val versionCode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            @Suppress("DEPRECATION")
            info.versionCode.toLong()
        }
        return ArchiveInfo(info.packageName, versionCode, info.versionName)
    }

    fun isDeviceOwner(): Boolean {
        val dpm = context.getSystemService(Context.DEVICE_POLICY_SERVICE) as? DevicePolicyManager
        return dpm?.isDeviceOwnerApp(context.packageName) == true
    }

    suspend fun install(
        file: File,
        expectedPackage: String,
        allowRootFallback: Boolean = false,
    ): Outcome = withContext(Dispatchers.IO) {
        val archive = inspect(file)
            ?: return@withContext Outcome.Failure(
                "INVALID_ARCHIVE", "Не вдалося прочитати APK — файл пошкоджено"
            )
        if (archive.packageName != expectedPackage) {
            return@withContext Outcome.Failure(
                "PACKAGE_MISMATCH",
                "У файлі пакет ${archive.packageName}, очікували $expectedPackage",
            )
        }

        val result = runCatching { installViaPackageInstaller(file, expectedPackage) }
            .getOrElse { error ->
                Log.e(TAG, "PackageInstaller не впорався", error)
                Outcome.Failure("INSTALL_EXCEPTION", error.message ?: "Помилка встановлення")
            }

        if (result is Outcome.Failure && allowRootFallback && isRootAvailable()) {
            Log.w(TAG, "Пробуємо запасний механізм через su (увімкнено адміністратором)")
            return@withContext installViaRoot(file)
        }
        result
    }

    private suspend fun installViaPackageInstaller(file: File, packageName: String): Outcome {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(
            PackageInstaller.SessionParams.MODE_FULL_INSTALL
        ).apply {
            setAppPackageName(packageName)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                // Тихе встановлення можливе лише для Device Owner —
                // в інших випадках система все одно спитає користувача.
                setRequireUserAction(
                    PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED
                )
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                setInstallReason(PackageManager.INSTALL_REASON_POLICY)
            }
        }

        val sessionId = installer.createSession(params)
        installer.openSession(sessionId).use { session ->
            session.openWrite(WRITE_NAME, 0, file.length()).use { output ->
                file.inputStream().use { input -> input.copyTo(output) }
                session.fsync(output)
            }

            val deferred = InstallStatusReceiver.register(context, sessionId)
            session.commit(deferred.intentSender)

            val mechanism = if (isDeviceOwner()) {
                "package_installer_device_owner"
            } else {
                "package_installer_user_confirmed"
            }
            return awaitStatus(deferred, mechanism)
        }
    }

    private suspend fun awaitStatus(
        pending: InstallStatusReceiver.Registration,
        mechanism: String,
    ): Outcome {
        val status = withTimeoutOrNull(STATUS_TIMEOUT_MS) { pending.await() }
            ?: return Outcome.Failure("TIMEOUT", "Система не відповіла на запит встановлення")
        return when (status.code) {
            PackageInstaller.STATUS_SUCCESS -> Outcome.Success(mechanism)
            PackageInstaller.STATUS_FAILURE_ABORTED ->
                Outcome.Failure("ABORTED", status.message ?: "Користувач скасував встановлення")
            else -> Outcome.Failure(
                statusName(status.code),
                status.message ?: "Встановлення не вдалося (код ${status.code})",
            )
        }
    }

    suspend fun uninstall(packageName: String): Outcome = withContext(Dispatchers.IO) {
        val installer = context.packageManager.packageInstaller
        val sessionId = UNINSTALL_SESSION_BASE + packageName.hashCode().and(0xFFFF)
        val pending = InstallStatusReceiver.register(context, sessionId)
        installer.uninstall(packageName, pending.intentSender)

        val mechanism = if (isDeviceOwner()) {
            "package_installer_device_owner"
        } else {
            "package_installer_user_confirmed"
        }
        awaitStatus(pending, mechanism)
    }

    /** Запуск застосунку через штатний launch-intent. */
    fun launch(packageName: String): Outcome {
        val intent = context.packageManager.getLaunchIntentForPackage(packageName)
            ?: return Outcome.Failure(
                "NO_LAUNCH_INTENT", "Застосунок не має екрана, який можна відкрити"
            )
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            Outcome.Success("launch_intent")
        } catch (e: Exception) {
            Outcome.Failure("LAUNCH_FAILED", e.message ?: "Не вдалося запустити застосунок")
        }
    }

    // ------------------------------------------------- запасний варіант ---

    private fun isRootAvailable(): Boolean = try {
        val process = ProcessBuilder("su", "-c", "id").redirectErrorStream(true).start()
        val output = process.inputStream.bufferedReader().readText()
        process.waitFor()
        output.contains("uid=0")
    } catch (_: Exception) {
        false
    }

    private fun installViaRoot(file: File): Outcome = try {
        // Шлях екранується для оболонки: він походить від контрольної суми з
        // payload команди, тож підставляти його в рядок «як є» не можна.
        val quoted = ua.fleet.agent.files.FilePathGuard.shellQuote(file.absolutePath)
        val process = ProcessBuilder("su", "-c", "pm install -r $quoted")
            .redirectErrorStream(true)
            .start()
        val output = process.inputStream.bufferedReader().readText().trim()
        val exit = process.waitFor()
        if (exit == 0 && output.contains("Success", ignoreCase = true)) {
            Outcome.Success("root_pm_install")
        } else {
            Outcome.Failure("ROOT_INSTALL_FAILED", output.ifBlank { "pm install код $exit" })
        }
    } catch (e: Exception) {
        Outcome.Failure("ROOT_UNAVAILABLE", e.message ?: "su недоступний")
    }

    private fun statusName(code: Int): String = when (code) {
        PackageInstaller.STATUS_FAILURE_BLOCKED -> "BLOCKED"
        PackageInstaller.STATUS_FAILURE_CONFLICT -> "CONFLICT"
        PackageInstaller.STATUS_FAILURE_INCOMPATIBLE -> "INCOMPATIBLE"
        PackageInstaller.STATUS_FAILURE_INVALID -> "INVALID"
        PackageInstaller.STATUS_FAILURE_STORAGE -> "STORAGE"
        else -> "FAILURE_$code"
    }

    private companion object {
        const val TAG = "FleetApkInstaller"
        const val WRITE_NAME = "fleet_apk"
        const val STATUS_TIMEOUT_MS = 10 * 60 * 1000L
        const val UNINSTALL_SESSION_BASE = 900_000
    }
}

/** Приймає статуси від `PackageInstaller` і віддає їх корутині. */
object InstallStatusReceiver {

    const val ACTION = "ua.fleet.agent.INSTALL_STATUS"
    private const val TAG = "FleetInstallStatus"

    data class Status(val code: Int, val message: String?)

    class Registration(
        val intentSender: android.content.IntentSender,
        private val awaiter: suspend () -> Status,
    ) {
        suspend fun await(): Status = awaiter()
    }

    fun register(context: Context, sessionId: Int): Registration {
        val action = "$ACTION.$sessionId"
        val intent = Intent(action).setPackage(context.packageName)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0
        val pendingIntent = PendingIntent.getBroadcast(context, sessionId, intent, flags)

        return Registration(pendingIntent.intentSender) {
            suspendCancellableCoroutine { continuation ->
                val receiver = object : BroadcastReceiver() {
                    override fun onReceive(ctx: Context, received: Intent) {
                        val code = received.getIntExtra(
                            PackageInstaller.EXTRA_STATUS,
                            PackageInstaller.STATUS_FAILURE
                        )
                        val message =
                            received.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)

                        if (code == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                            // Без Device Owner система показує діалог — це очікувано.
                            val confirm = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                                received.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                            } else {
                                @Suppress("DEPRECATION")
                                received.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                            }
                            confirm?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                            runCatching { confirm?.let(ctx::startActivity) }
                                .onFailure { Log.w(TAG, "Не вдалося показати діалог", it) }
                            return // чекаємо фінальний статус
                        }

                        runCatching { ctx.unregisterReceiver(this) }
                        if (continuation.isActive) {
                            continuation.resume(Status(code, message))
                        }
                    }
                }

                val filter = IntentFilter(action)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
                } else {
                    @Suppress("UnspecifiedRegisterReceiverFlag")
                    context.registerReceiver(receiver, filter)
                }

                continuation.invokeOnCancellation {
                    runCatching { context.unregisterReceiver(receiver) }
                }
            }
        }
    }
}
