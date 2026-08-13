package ua.fleet.agent.command

import android.content.Context
import android.util.Log
import java.io.File
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import ua.fleet.agent.apps.ApkDownloader
import ua.fleet.agent.apps.ApkInstaller
import ua.fleet.agent.apps.AppInventoryCollector
import ua.fleet.agent.comms.CommsReporter
import ua.fleet.agent.data.AgentStore
import ua.fleet.agent.enroll.EnrollmentRepository
import ua.fleet.agent.files.FileCommandExecutor
import ua.fleet.agent.files.FileManager
import ua.fleet.agent.files.FileTransferClient
import ua.fleet.agent.location.LocationReporter
import ua.fleet.agent.support.RemoteInputService
import ua.fleet.agent.support.ScreenShareService
import ua.fleet.agent.support.SupportConsentActivity
import ua.fleet.agent.support.SupportSessionRequest
import ua.fleet.agent.support.SupportSessionState
import ua.fleet.agent.telemetry.TelemetryCollector
import ua.fleet.agent.net.ApiClient
import ua.fleet.agent.net.dto.AppInventoryReportDto
import ua.fleet.agent.net.dto.CommandProgressDto
import ua.fleet.agent.net.dto.CommandResultDto
import ua.fleet.agent.net.dto.PendingCommandDto

/**
 * Виконує команди, отримані від сервера, і звітує про хід виконання.
 *
 * Кожна команда: `ack` → кілька `progress` → `result`. Будь-який збій
 * повертається серверу як `failed` із кодом помилки — операція ніколи не
 * «зникає» мовчки.
 */
class CommandProcessor(
    context: Context,
    private val repository: EnrollmentRepository,
) {

    private val appContext = context.applicationContext
    private val store: AgentStore = repository.state
    private val inventory = AppInventoryCollector(appContext)
    private val downloader = ApkDownloader(appContext)
    private val installer = ApkInstaller(appContext)
    val locationReporter = LocationReporter(appContext, repository)
    private val telemetryCollector = TelemetryCollector(appContext)
    private val fileManager = FileManager { store.allowRootInstall }
    private val transferClient = FileTransferClient(appContext, fileManager)
    private val fileCommands =
        FileCommandExecutor(store, repository, fileManager, transferClient)
    private val deviceCommands = DeviceCommandExecutor(appContext, store)
    val commsReporter = CommsReporter(appContext, repository)

    /** Забирає й виконує всі команди, що очікують. Повертає їхню кількість. */
    suspend fun runPending(): Int {
        if (!store.isEnrolled) return 0
        val serverUrl = store.serverUrl ?: return 0
        val api = ApiClient.create(serverUrl)
        val bearer = "Bearer ${repository.accessToken()}"

        val commands = api.pendingCommands(bearer)
        for (command in commands) {
            runCatching { execute(command) }
                .onFailure { error ->
                    Log.e(TAG, "Команду ${command.id} не виконано", error)
                    runCatching {
                        api.reportResult(
                            bearer,
                            command.id,
                            CommandResultDto(
                                status = "failed",
                                error = ApiClient.describeError(error).take(2000),
                            ),
                        )
                    }
                }
        }
        store.lastContactAtMillis = System.currentTimeMillis()
        return commands.size
    }

    private suspend fun execute(command: PendingCommandDto) {
        val serverUrl = store.serverUrl ?: return
        val api = ApiClient.create(serverUrl)
        val bearer = "Bearer ${repository.accessToken()}"
        api.ackCommand(bearer, command.id)

        suspend fun progress(percent: Int, message: String) {
            runCatching {
                api.reportProgress(bearer, command.id, CommandProgressDto(percent, message))
            }
        }

        AgentLog.info(TAG, "Виконуємо ${command.type} (${command.id})")
        val result = when (command.type) {
            "APP_INSTALL", "APP_UPDATE" -> installFlow(command, bearer, ::progress)
            "APP_UNINSTALL" -> uninstallFlow(command, ::progress)
            "APP_LAUNCH" -> launchFlow(command)
            "APP_STOP" -> stopAppFlow(command, ::progress)
            "APP_LIST" -> inventoryFlow(api, bearer, ::progress)
            "FILE_LIST" -> fileCommands.list(command)
            "FILE_MKDIR" -> fileCommands.mkdir(command)
            "FILE_RENAME" -> fileCommands.rename(command)
            "FILE_DELETE" -> fileCommands.delete(command)
            "FILE_DOWNLOAD" -> fileCommands.uploadToServer(command, bearer, ::progress)
            "FILE_UPLOAD" -> fileCommands.downloadToDevice(command, bearer, ::progress)
            "REFRESH_TELEMETRY" -> telemetryFlow(::progress)
            "SYNC_CONFIG" -> syncConfigFlow(::progress)
            "REBOOT" -> rebootFlow(command, ::progress)
            "AGENT_RESTART" -> restartAgentFlow(::progress)
            "COLLECT_DIAGNOSTICS" -> diagnosticsFlow(command, ::progress)
            "COMMS_SYNC" -> commsSyncFlow(::progress)
            "COMMS_COLLECT" -> commsCollectFlow(::progress)
            "REMOTE_SUPPORT_REQUEST" -> supportRequestFlow(command)
            "REMOTE_SUPPORT_END" -> supportEndFlow()
            "LOCATION_CONFIG" -> locationConfigFlow()
            "LOCATION_REQUEST" -> locationRequestFlow(::progress)
            else -> CommandResultDto(
                status = "failed",
                error = "Агент не підтримує команду ${command.type}",
            )
        }
        api.reportResult(bearer, command.id, result)
    }

    // ------------------------------------------------------ встановлення --

    private suspend fun installFlow(
        command: PendingCommandDto,
        bearer: String,
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        val payload = command.payload
            ?: return failure("BAD_PAYLOAD", "Команда без параметрів")
        val packageName = payload.str("package_name")
            ?: return failure("BAD_PAYLOAD", "Не вказано package_name")
        val url = payload.str("download_url")
            ?: return failure("BAD_PAYLOAD", "Не вказано download_url")
        val sha256 = payload.str("sha256")
            ?: return failure("BAD_PAYLOAD", "Не вказано контрольну суму")
        val expectedVersionCode = payload.str("version_code")?.toLongOrNull()
        val size = payload.str("size_bytes")?.toLongOrNull()

        progress(5, "Підготовка до встановлення")

        var file: File? = null
        try {
            file = downloader.download(
                url = url,
                bearerToken = bearer.removePrefix("Bearer "),
                expectedSha256 = sha256,
                expectedSize = size,
            ) { percent ->
                // 0..100 завантаження → 10..70 загального прогресу
                progress(10 + percent * 60 / 100, "Завантаження APK: $percent%")
            }

            progress(75, "Перевірка вмісту пакета")
            val archive = installer.inspect(file)
                ?: return failure("INVALID_ARCHIVE", "Не вдалося прочитати APK")
            if (archive.packageName != packageName) {
                return failure(
                    "PACKAGE_MISMATCH",
                    "У файлі пакет ${archive.packageName}, очікували $packageName",
                )
            }
            if (expectedVersionCode != null && archive.versionCode != expectedVersionCode) {
                return failure(
                    "VERSION_MISMATCH",
                    "versionCode ${archive.versionCode} не збігається з очікуваним $expectedVersionCode",
                )
            }

            progress(80, "Встановлення")
            val outcome = installer.install(
                file = file,
                expectedPackage = packageName,
                allowRootFallback = store.allowRootInstall,
            )

            return when (outcome) {
                is ApkInstaller.Outcome.Success -> {
                    progress(95, "Перевірка результату")
                    val installed = inventory.installedVersion(packageName)
                    CommandResultDto(
                        status = "succeeded",
                        result = buildJsonObject {
                            put("mechanism", JsonPrimitive(outcome.mechanism))
                            put("sha256", JsonPrimitive(sha256))
                            put(
                                "installed",
                                buildJsonObject {
                                    put("package_name", JsonPrimitive(packageName))
                                    put("version_code", JsonPrimitive(installed?.versionCode))
                                    put("version_name", JsonPrimitive(installed?.versionName))
                                    put("app_name", JsonPrimitive(installed?.appName))
                                },
                            )
                        },
                    )
                }

                is ApkInstaller.Outcome.Failure -> CommandResultDto(
                    status = "failed",
                    error = "${outcome.code}: ${outcome.message}",
                    result = buildJsonObject { put("code", JsonPrimitive(outcome.code)) },
                )

                ApkInstaller.Outcome.AwaitingUserConfirmation -> CommandResultDto(
                    status = "failed",
                    error = "Користувач не підтвердив встановлення",
                )
            }
        } catch (e: ApkDownloader.ChecksumMismatchException) {
            return failure("CHECKSUM_MISMATCH", e.message ?: "Контрольна сума не збіглася")
        } finally {
            file?.delete()
        }
    }

    // ---------------------------------------------------------- видалення --

    private suspend fun uninstallFlow(
        command: PendingCommandDto,
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        val packageName = command.payload?.str("package_name")
            ?: return failure("BAD_PAYLOAD", "Не вказано package_name")

        if (!inventory.isInstalled(packageName)) {
            return CommandResultDto(
                status = "succeeded",
                result = buildJsonObject {
                    put("mechanism", JsonPrimitive("already_absent"))
                    put("package_name", JsonPrimitive(packageName))
                },
            )
        }

        progress(40, "Видалення $packageName")
        return when (val outcome = installer.uninstall(packageName)) {
            is ApkInstaller.Outcome.Success -> CommandResultDto(
                status = "succeeded",
                result = buildJsonObject {
                    put("mechanism", JsonPrimitive(outcome.mechanism))
                    put("package_name", JsonPrimitive(packageName))
                },
            )

            is ApkInstaller.Outcome.Failure ->
                failure(outcome.code, outcome.message)

            ApkInstaller.Outcome.AwaitingUserConfirmation ->
                failure("USER_CANCELLED", "Користувач не підтвердив видалення")
        }
    }

    // ------------------------------------------------------------ запуск --

    private fun launchFlow(command: PendingCommandDto): CommandResultDto {
        val packageName = command.payload?.str("package_name")
            ?: return failure("BAD_PAYLOAD", "Не вказано package_name")
        return when (val outcome = installer.launch(packageName)) {
            is ApkInstaller.Outcome.Success -> CommandResultDto(
                status = "succeeded",
                result = buildJsonObject {
                    put("mechanism", JsonPrimitive(outcome.mechanism))
                    put("package_name", JsonPrimitive(packageName))
                },
            )

            is ApkInstaller.Outcome.Failure -> failure(outcome.code, outcome.message)
            ApkInstaller.Outcome.AwaitingUserConfirmation ->
                failure("UNEXPECTED", "Непередбачений стан")
        }
    }

    // ---------------------------------------------------------- інвентар --

    suspend fun reportInventory(): Int {
        val serverUrl = store.serverUrl ?: return 0
        val api = ApiClient.create(serverUrl)
        val bearer = "Bearer ${repository.accessToken()}"
        val apps = inventory.collect()
        api.reportApps(bearer, AppInventoryReportDto(apps))
        store.lastContactAtMillis = System.currentTimeMillis()
        return apps.size
    }

    private suspend fun inventoryFlow(
        api: ua.fleet.agent.net.FleetApi,
        bearer: String,
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        progress(30, "Збір переліку застосунків")
        val apps = inventory.collect()
        api.reportApps(bearer, AppInventoryReportDto(apps))
        return CommandResultDto(
            status = "succeeded",
            result = buildJsonObject { put("count", JsonPrimitive(apps.size)) },
        )
    }

    // ------------------------------------------------------- геолокація --

    /** Адміністратор змінив конфігурацію — забираємо її з сервера. */
    private suspend fun locationConfigFlow(): CommandResultDto {
        val config = locationReporter.syncConfig()
            ?: return failure("NO_SERVER", "Сервер недоступний")
        locationReporter.syncPermissions(force = true)
        return CommandResultDto(
            status = "succeeded",
            result = buildJsonObject {
                put("enabled", JsonPrimitive(config.enabled))
                put("interval_seconds", JsonPrimitive(config.intervalSeconds))
                put("high_accuracy", JsonPrimitive(config.highAccuracy))
                put("permission_state", JsonPrimitive(locationReporter.status().permissionState))
            },
        )
    }

    /** Разовий вимір позиції на запит адміністратора. */
    private suspend fun locationRequestFlow(
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        val status = locationReporter.status()
        if (!status.enabled) {
            return failure("LOCATION_DISABLED", "Модуль геолокації вимкнено")
        }
        if (status.permissionState !in setOf("granted_fine", "granted_coarse")) {
            return failure(
                "PERMISSION_DENIED",
                "Користувач не надав дозвіл на геолокацію (${status.permissionState})",
            )
        }
        if (!status.servicesEnabled) {
            return failure("LOCATION_OFF", "Геолокацію вимкнено в налаштуваннях системи")
        }

        progress(40, "Визначення позиції")
        val collected = locationReporter.collectOnDemand()
        return if (collected) {
            CommandResultDto(
                status = "succeeded",
                result = buildJsonObject { put("source", JsonPrimitive("on_demand")) },
            )
        } else {
            failure("NO_FIX", "Не вдалося отримати координати за відведений час")
        }
    }

    // ------------------------------------------------------- телеметрія --

    /** Надсилає зріз стану пристрою; повертає інтервал наступного звіту. */
    suspend fun reportTelemetry(): Int {
        val serverUrl = store.serverUrl ?: return 0
        val api = ApiClient.create(serverUrl)
        val bearer = "Bearer " + repository.accessToken()
        val response = api.reportTelemetry(bearer, telemetryCollector.collect())
        store.lastContactAtMillis = System.currentTimeMillis()
        store.telemetryIntervalSeconds = response.nextReportSeconds
        store.lastTelemetryAtMillis = System.currentTimeMillis()
        return response.nextReportSeconds
    }

    private suspend fun telemetryFlow(
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        progress(40, "Збір показників стану")
        val interval = reportTelemetry()
        return CommandResultDto(
            status = "succeeded",
            result = buildJsonObject {
                put("next_report_seconds", JsonPrimitive(interval))
            },
        )
    }

    // --------------------------------------------- віддалена підтримка ---

    /**
     * Показує користувачу екран згоди. Сеанс не почнеться, доки він не
     * натисне «Дозволити» і не підтвердить системний діалог MediaProjection.
     */
    private fun supportRequestFlow(command: PendingCommandDto): CommandResultDto {
        val payload = command.payload ?: return failure("BAD_PAYLOAD", "Команда без параметрів")
        val sessionId = payload.str("session_id")
            ?: return failure("BAD_PAYLOAD", "Не вказано session_id")
        val websocketUrl = payload.str("websocket_url")
            ?: return failure("BAD_PAYLOAD", "Не вказано websocket_url")

        val request = SupportSessionRequest(
            sessionId = sessionId,
            reason = payload.str("reason") ?: "",
            requestedBy = payload.str("requested_by") ?: "адміністратор",
            inputRequested = payload.str("input_requested")?.toBoolean() ?: false,
            joinSecret = payload.str("join_secret"),
            websocketUrl = websocketUrl,
        )
        SupportConsentActivity.launch(appContext, request)

        return CommandResultDto(
            status = "succeeded",
            result = buildJsonObject {
                put("session_id", JsonPrimitive(sessionId))
                put("consent_prompt_shown", JsonPrimitive(true))
                put("input_available", JsonPrimitive(RemoteInputService.isEnabled(appContext)))
            },
        )
    }

    private fun supportEndFlow(): CommandResultDto {
        ScreenShareService.stop(appContext)
        SupportSessionState.clear()
        return CommandResultDto(
            status = "succeeded",
            result = buildJsonObject { put("stopped", JsonPrimitive(true)) },
        )
    }

    // --------------------------------------------------- дії над пристроєм --

    /**
     * Перезавантаження пристрою.
     *
     * Результат повертається **до** самого перезавантаження: після виклику
     * система вимикає пристрій, і надіслати відповідь буде вже нікому.
     */
    private suspend fun rebootFlow(
        command: PendingCommandDto,
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        val reason = command.payload?.str("reason").orEmpty()
        progress(30, "Підготовка до перезавантаження")

        if (!deviceCommands.isDeviceOwner()) {
            return failure(
                "DEVICE_OWNER_REQUIRED",
                "Перезавантаження доступне лише в режимі Device Owner",
            )
        }

        progress(80, "Передаємо запит системі")
        return deviceCommands.reboot(reason)
    }

    /** Перечитує конфігурацію агента з сервера. */
    private suspend fun syncConfigFlow(
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        progress(20, "Отримання конфігурації геолокації")
        val location = locationReporter.syncConfig()
        locationReporter.syncPermissions(force = true)

        progress(60, "Оновлення дозволених каталогів")
        val roots = runCatching { fileCommands.refreshRoots() }.getOrDefault(-1)

        progress(90, "Готово")
        return CommandResultDto(
            status = "succeeded",
            result = buildJsonObject {
                put("location_enabled", JsonPrimitive(location?.enabled ?: store.locationEnabled))
                put(
                    "telemetry_interval_seconds",
                    JsonPrimitive(store.telemetryIntervalSeconds),
                )
                put("heartbeat_interval_seconds", JsonPrimitive(store.heartbeatIntervalSeconds))
                put("file_roots", JsonPrimitive(roots))
                put("key_rotation_due", JsonPrimitive(store.keyRotationDue))
            },
        )
    }

    private suspend fun restartAgentFlow(
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        // Звітуємо до перезапуску: після зупинки сервісу поточна корутина
        // може не дожити до відправлення результату.
        progress(70, "Перезапуск фонового сервісу")
        return deviceCommands.restartAgent()
    }

    private suspend fun diagnosticsFlow(
        command: PendingCommandDto,
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        progress(40, "Збір стану агента")
        return deviceCommands.collectDiagnostics(command.payload)
    }

    private suspend fun commsSyncFlow(
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        progress(40, "Синхронізація комунікаційного модуля")
        val config = commsReporter.sync()
            ?: return failure("NO_SERVER", "Сервер недоступний")
        return CommandResultDto(
            status = "succeeded",
            result = buildJsonObject {
                put("enabled", JsonPrimitive(config.enabled))
                put("collect_sms", JsonPrimitive(config.collectSms))
                put("collect_call_log", JsonPrimitive(config.collectCallLog))
                put("sms_permission", JsonPrimitive(commsReporter.currentStatus().smsPermission))
                put(
                    "call_log_permission",
                    JsonPrimitive(commsReporter.currentStatus().callLogPermission),
                )
            },
        )
    }

    private suspend fun commsCollectFlow(
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        progress(30, "Перевірка конфігурації модуля")
        val status = commsReporter.currentStatus()
        if (!status.enabled) {
            return CommandResultDto(
                status = "succeeded",
                result = buildJsonObject { put("collected", JsonPrimitive(0)) },
            )
        }
        progress(60, "Збір повідомлень і дзвінків")
        val sent = commsReporter.collectAndUpload()
        return CommandResultDto(
            status = "succeeded",
            result = buildJsonObject { put("collected", JsonPrimitive(sent)) },
        )
    }

    private suspend fun stopAppFlow(
        command: PendingCommandDto,
        progress: suspend (Int, String) -> Unit,
    ): CommandResultDto {
        val payload = command.payload
            ?: return failure("BAD_PAYLOAD", "Команда без параметрів")
        val packageName = payload.str("package_name")
            ?: return failure("BAD_PAYLOAD", "Не вказано package_name")
        val clearCache = payload.str("clear_cache")?.toBoolean() ?: false

        progress(50, "Зупинка застосунку")
        return deviceCommands.stopApp(packageName, clearCache)
    }

    private fun failure(code: String, message: String) = CommandResultDto(
        status = "failed",
        error = "$code: $message",
        result = buildJsonObject { put("code", JsonPrimitive(code)) },
    )

    private fun JsonObject.str(key: String): String? =
        this[key]?.let { element ->
            runCatching { element.jsonPrimitive.contentOrNullSafe() }.getOrNull()
        }

    private fun JsonPrimitive.contentOrNullSafe(): String? =
        if (this.content == "null") null else this.content

    private companion object {
        const val TAG = "FleetCommandProcessor"
    }
}
