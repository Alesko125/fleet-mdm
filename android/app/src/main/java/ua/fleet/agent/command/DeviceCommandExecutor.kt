package ua.fleet.agent.command

import android.app.ActivityManager
import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import ua.fleet.agent.BuildConfig
import ua.fleet.agent.admin.FleetDeviceAdminReceiver
import ua.fleet.agent.data.AgentStore
import ua.fleet.agent.net.dto.CommandResultDto
import ua.fleet.agent.support.RemoteInputService

/**
 * Команди рівня пристрою: перезавантаження, зупинка застосунку,
 * перезапуск агента, збір діагностики.
 *
 * Тут навмисно **немає** виконання довільних рядків. Кожна команда — це
 * виклик задокументованого API Android:
 *
 * * перезавантаження — `DevicePolicyManager.reboot()`, доступний лише
 *   Device Owner (це вимога системи, не наша);
 * * зупинка застосунку — `ActivityManager.killBackgroundProcesses()`, а в
 *   режимі Device Owner доступний і суворіший `setPackagesSuspended`;
 * * перезапуск агента — зупинка й запуск власного сервісу;
 * * діагностика — власні дані агента й відкриті системні лічильники.
 *
 * `su` тут не використовується взагалі: жодна з цих операцій його не
 * потребує, а привілейований режим лишається тільки у файловому модулі для
 * каталогів, які адміністратор явно позначив `allow_privileged`.
 */
class DeviceCommandExecutor(
    private val context: Context,
    private val store: AgentStore,
) {

    private val dpm: DevicePolicyManager?
        get() = context.getSystemService(Context.DEVICE_POLICY_SERVICE) as? DevicePolicyManager

    private val adminComponent: ComponentName
        get() = ComponentName(context, FleetDeviceAdminReceiver::class.java)

    fun isDeviceOwner(): Boolean = dpm?.isDeviceOwnerApp(context.packageName) == true

    // ------------------------------------------------------ перезавантаження

    /**
     * Перезавантаження пристрою.
     *
     * Android дозволяє його лише власнику пристрою і лише коли немає
     * активного дзвінка — обидві умови перевіряє сама система, ми лише
     * повертаємо зрозумілу помилку.
     */
    suspend fun reboot(reason: String): CommandResultDto = withContext(Dispatchers.Main) {
        val manager = dpm
        if (manager == null || !isDeviceOwner()) {
            return@withContext failure(
                "DEVICE_OWNER_REQUIRED",
                "Перезавантаження доступне лише в режимі Device Owner",
            )
        }
        try {
            Log.i(TAG, "Перезавантаження на запит адміністратора: $reason")
            manager.reboot(adminComponent)
            CommandResultDto(
                status = "succeeded",
                result = buildJsonObject {
                    put("mechanism", JsonPrimitive("device_policy_manager"))
                    put("reason", JsonPrimitive(reason))
                },
            )
        } catch (e: SecurityException) {
            failure("NOT_PERMITTED", e.message ?: "Система відхилила перезавантаження")
        } catch (e: IllegalStateException) {
            // Android не перезавантажує пристрій під час активного дзвінка.
            failure("BUSY", e.message ?: "Перезавантаження зараз неможливе")
        }
    }

    // ------------------------------------------------- зупинка застосунку --

    /**
     * Зупиняє процеси застосунку.
     *
     * Дані застосунку не видаляються: `killBackgroundProcesses` лише
     * завершує процеси, а `setPackagesSuspended` (Device Owner) тимчасово
     * призупиняє пакет і одразу знімає призупинення.
     */
    suspend fun stopApp(packageName: String, clearCache: Boolean): CommandResultDto =
        withContext(Dispatchers.IO) {
            if (packageName == context.packageName) {
                return@withContext failure(
                    "SELF_STOP_FORBIDDEN",
                    "Агент не зупиняє сам себе цією командою",
                )
            }
            if (!isInstalled(packageName)) {
                return@withContext failure("NOT_INSTALLED", "Застосунок не встановлено")
            }

            var mechanism = "activity_manager"
            val manager = dpm
            if (manager != null && isDeviceOwner() &&
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.N
            ) {
                // Призупинення на мить надійніше за kill: система сама
                // завершує процеси пакета. Одразу знімаємо — команда
                // називається «зупинити», а не «заблокувати».
                runCatching {
                    manager.setPackagesSuspended(adminComponent, arrayOf(packageName), true)
                    manager.setPackagesSuspended(adminComponent, arrayOf(packageName), false)
                    mechanism = "device_policy_manager"
                }.onFailure { Log.w(TAG, "setPackagesSuspended не спрацював", it) }
            }

            runCatching {
                val activity =
                    context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
                activity.killBackgroundProcesses(packageName)
            }.onFailure { Log.w(TAG, "killBackgroundProcesses не спрацював", it) }

            var cacheCleared = false
            if (clearCache) {
                // Чистимо лише кеш і лише власний: доступу до кешу чужих
                // застосунків у звичайного застосунку немає й бути не має.
                cacheCleared = packageName == context.packageName
            }

            CommandResultDto(
                status = "succeeded",
                result = buildJsonObject {
                    put("package_name", JsonPrimitive(packageName))
                    put("mechanism", JsonPrimitive(mechanism))
                    put("cache_cleared", JsonPrimitive(cacheCleared))
                    put("still_running", JsonPrimitive(isRunning(packageName)))
                },
            )
        }

    // -------------------------------------------------- перезапуск агента --

    /**
     * Перезапускає фоновий сервіс агента.
     *
     * Застосунок не перевстановлюється, дані не видаляються, реєстрація
     * зберігається. Постійне сповіщення на мить зникне — це видимий для
     * власника пристрою наслідок, і приховувати його ми не намагаємось.
     */
    suspend fun restartAgent(): CommandResultDto = withContext(Dispatchers.IO) {
        return@withContext try {
            AgentService.stop(context)
            // Невелика пауза, щоб сервіс встиг коректно згорнутися.
            kotlinx.coroutines.delay(RESTART_DELAY_MS)
            // Перезапуск ініціює сервер, тобто застосунок у фоні. Від
            // Android 12 система має право не дати підняти службу назад —
            // тоді ми зупинили агента й не можемо його повернути. Про це
            // треба сказати прямо, інакше в панелі буде «виконано», а на
            // пристрої — тиша до наступного відкриття застосунку.
            if (AgentService.start(context) == StartOutcome.NOT_ALLOWED) {
                return@withContext CommandResultDto(
                    status = "failed",
                    error = "Android заборонив підняти службу з фону. " +
                        "Агент відновиться, коли власник відкриє застосунок; " +
                        "для автоматичного перезапуску потрібен режим Device Owner.",
                )
            }
            CommandResultDto(
                status = "succeeded",
                result = buildJsonObject {
                    put("mechanism", JsonPrimitive("foreground_service_restart"))
                    put("agent_version", JsonPrimitive(BuildConfig.AGENT_VERSION))
                },
            )
        } catch (e: Exception) {
            failure("RESTART_FAILED", e.message ?: "Не вдалося перезапустити сервіс")
        }
    }

    // ------------------------------------------------------- діагностика ---

    /**
     * Звіт про стан самого агента.
     *
     * Збираються **власні** дані агента й відкриті системні лічильники:
     * версії, режим керування, стан дозволів, використання сховища,
     * останні записи власного журналу. Системний `logcat` цілком, дані
     * інших застосунків і вміст файлів користувача сюди не потрапляють.
     */
    suspend fun collectDiagnostics(params: JsonObject?): CommandResultDto =
        withContext(Dispatchers.IO) {
            fun flag(key: String, fallback: Boolean = true): Boolean =
                params?.get(key)?.jsonPrimitive?.content?.toBooleanStrictOrNull() ?: fallback

            val includeLog = flag("include_agent_log")
            val includePermissions = flag("include_permissions")
            val includeStorage = flag("include_storage")
            val logLines = params?.get("log_lines")?.jsonPrimitive?.content?.toIntOrNull()
                ?: DEFAULT_LOG_LINES

            val report = buildJsonObject {
                put("agent_version", JsonPrimitive(BuildConfig.AGENT_VERSION))
                put("android_sdk", JsonPrimitive(Build.VERSION.SDK_INT))
                put("android_release", JsonPrimitive(Build.VERSION.RELEASE))
                put("management_mode", JsonPrimitive(store.managementMode))
                put("device_owner", JsonPrimitive(isDeviceOwner()))
                put("service_enabled", JsonPrimitive(store.serviceEnabled))
                put("key_generation", JsonPrimitive(store.keyGeneration))
                put(
                    "last_contact_at",
                    JsonPrimitive(store.lastContactAtMillis),
                )

                if (includePermissions) {
                    put(
                        "permissions",
                        buildJsonObject {
                            put(
                                "location_reported",
                                JsonPrimitive(store.reportedPermissionState ?: "unknown"),
                            )
                            put(
                                "accessibility_service",
                                JsonPrimitive(RemoteInputService.isEnabled(context)),
                            )
                            put("location_module_enabled", JsonPrimitive(store.locationEnabled))
                        },
                    )
                }

                if (includeStorage) {
                    put("storage", storageReport())
                    put("memory", memoryReport())
                }

                if (includeLog && logLines > 0) {
                    // Лише власний журнал агента, відфільтрований за нашими
                    // тегами. Читання системного журналу цілком не робиться.
                    put("agent_log", buildJsonArray {
                        AgentLog.recent(logLines).forEach { add(JsonPrimitive(it)) }
                    })
                    put("agent_log_lines", JsonPrimitive(AgentLog.size()))
                }
            }

            CommandResultDto(status = "succeeded", result = report)
        }

    private fun storageReport(): JsonObject {
        val data = StatFs(Environment.getDataDirectory().path)
        return buildJsonObject {
            put("data_total_bytes", JsonPrimitive(data.totalBytes))
            put("data_free_bytes", JsonPrimitive(data.availableBytes))
            put("agent_cache_bytes", JsonPrimitive(directorySize(context.cacheDir)))
            put("agent_files_bytes", JsonPrimitive(directorySize(context.filesDir)))
        }
    }

    private fun memoryReport(): JsonObject {
        val activity = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val info = ActivityManager.MemoryInfo()
        activity.getMemoryInfo(info)
        val runtime = Runtime.getRuntime()
        return buildJsonObject {
            put("system_available_bytes", JsonPrimitive(info.availMem))
            put("system_total_bytes", JsonPrimitive(info.totalMem))
            put("system_low_memory", JsonPrimitive(info.lowMemory))
            put("agent_heap_used_bytes", JsonPrimitive(runtime.totalMemory() - runtime.freeMemory()))
            put("agent_heap_max_bytes", JsonPrimitive(runtime.maxMemory()))
        }
    }

    private fun directorySize(dir: java.io.File?): Long {
        if (dir == null || !dir.exists()) return 0
        return dir.walkBottomUp().filter { it.isFile }.sumOf { it.length() }
    }

    // ------------------------------------------------------------ службове -

    private fun isInstalled(packageName: String): Boolean = runCatching {
        context.packageManager.getPackageInfo(packageName, 0)
        true
    }.getOrDefault(false)

    @Suppress("DEPRECATION")
    private fun isRunning(packageName: String): Boolean = runCatching {
        val activity = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        activity.runningAppProcesses?.any { it.processName == packageName } == true
    }.getOrDefault(false)

    private fun failure(code: String, message: String) = CommandResultDto(
        status = "failed",
        error = "$code: $message",
        result = buildJsonObject { put("code", JsonPrimitive(code)) },
    )

    private companion object {
        const val TAG = "FleetDeviceCommand"
        const val RESTART_DELAY_MS = 500L
        const val DEFAULT_LOG_LINES = 200
    }
}
