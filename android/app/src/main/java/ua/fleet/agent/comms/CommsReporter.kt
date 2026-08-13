package ua.fleet.agent.comms

import android.content.Context
import ua.fleet.agent.command.AgentLog
import ua.fleet.agent.enroll.EnrollmentRepository
import ua.fleet.agent.net.ApiClient
import ua.fleet.agent.net.dto.CommsBatchDto
import ua.fleet.agent.net.dto.CommsConfigDto
import ua.fleet.agent.net.dto.CommsPermissionDto

/**
 * Оркестрація комунікаційного модуля на пристрої.
 *
 * Порядок дій жорсткий і не залишає місця для «тихого» збору:
 *
 * 1. запитуємо в сервера конфігурацію (`enabled`, що саме збирати);
 * 2. повідомляємо серверу фактичний стан дозволів — сервер не гадає;
 * 3. збираємо **лише** дозволене (сервером *і* системою) і надсилаємо.
 *
 * Якщо модуль вимкнено (`enabled=false`) або дозвіл не наданий — нічого не
 * збирається й не надсилається. Недоступну можливість позначаємо як таку,
 * а не обходимо.
 */
class CommsReporter(
    context: Context,
    private val repository: EnrollmentRepository,
) {

    private val appContext = context.applicationContext
    private val collector = CommsCollector(appContext)
    private val store = repository.state

    /** Останній відомий стан — для картки на екрані агента. */
    data class Status(
        val enabled: Boolean,
        val collectSms: Boolean,
        val collectCallLog: Boolean,
        val metadataOnly: Boolean,
        val smsPermission: String,
        val callLogPermission: String,
        val notice: String,
        val lastSyncAtMillis: Long,
    )

    @Volatile
    var lastStatus: Status = Status(
        enabled = false, collectSms = false, collectCallLog = false,
        metadataOnly = false, smsPermission = "unknown", callLogPermission = "unknown",
        notice = "", lastSyncAtMillis = 0L,
    )
        private set

    /** Синхронізує конфігурацію та стан дозволів. Повертає чинну конфігурацію. */
    suspend fun sync(): CommsConfigDto? {
        val serverUrl = store.serverUrl ?: return null
        val api = ApiClient.create(serverUrl)
        val bearer = "Bearer ${repository.accessToken()}"

        val config = runCatching { api.commsConfig(bearer) }.getOrNull() ?: return null
        store.commsEnabled = config.enabled
        store.commsCollectSms = config.collectSms
        store.commsCollectCallLog = config.collectCallLog
        store.commsMetadataOnly = config.metadataOnly
        store.commsNotice = config.notice

        // Завжди повідомляємо серверу фактичний стан дозволів — навіть коли
        // модуль вимкнено, щоб панель бачила реальну картину.
        val sms = collector.smsPermission()
        val calls = collector.callLogPermission()
        runCatching {
            api.reportCommsPermissions(
                bearer, CommsPermissionDto(sms.wire, calls.wire),
            )
        }

        updateStatus(config, sms.wire, calls.wire)
        return config
    }

    /**
     * Збирає й надсилає дані згідно з конфігурацією.
     *
     * @return скільки записів надіслано (sms + calls), або 0
     */
    suspend fun collectAndUpload(): Int {
        val config = sync() ?: return 0
        if (!config.enabled) return 0
        if (!config.collectSms && !config.collectCallLog) return 0

        val serverUrl = store.serverUrl ?: return 0
        val api = ApiClient.create(serverUrl)
        val bearer = "Bearer ${repository.accessToken()}"

        val sinceMillis = System.currentTimeMillis() -
            config.historyDays.coerceIn(1, 365) * DAY_MS

        val sms = if (config.collectSms) {
            collector.collectSms(sinceMillis, config.maxBatch, config.metadataOnly)
        } else {
            emptyList()
        }
        val calls = if (config.collectCallLog) {
            collector.collectCalls(sinceMillis, config.maxBatch)
        } else {
            emptyList()
        }

        if (sms.isEmpty() && calls.isEmpty()) return 0

        val accepted = runCatching {
            api.uploadCommsData(bearer, CommsBatchDto(sms = sms, calls = calls))
        }.getOrElse {
            AgentLog.warn(TAG, "Не вдалося надіслати комунікаційні дані", it)
            return 0
        }

        store.commsLastSyncAtMillis = System.currentTimeMillis()
        AgentLog.info(
            TAG,
            "Надіслано: SMS ${accepted.smsStored}, дзвінків ${accepted.callsStored}",
        )
        updateStatus(accepted.config, lastStatus.smsPermission, lastStatus.callLogPermission)
        return accepted.smsStored + accepted.callsStored
    }

    /** Чи потрібні (і ще не надані) рантайм-дозволи — для запиту з UI. */
    fun neededPermissions(): List<String> {
        val needed = mutableListOf<String>()
        if (store.commsCollectSms &&
            collector.smsPermission() == CommsCollector.PermState.DENIED
        ) {
            needed.add(android.Manifest.permission.READ_SMS)
        }
        if (store.commsCollectCallLog &&
            collector.callLogPermission() == CommsCollector.PermState.DENIED
        ) {
            needed.add(android.Manifest.permission.READ_CALL_LOG)
        }
        return needed
    }

    fun currentStatus(): Status {
        val sms = collector.smsPermission().wire
        val calls = collector.callLogPermission().wire
        val current = lastStatus.copy(
            enabled = store.commsEnabled,
            collectSms = store.commsCollectSms,
            collectCallLog = store.commsCollectCallLog,
            metadataOnly = store.commsMetadataOnly,
            smsPermission = sms,
            callLogPermission = calls,
            notice = store.commsNotice.orEmpty(),
            lastSyncAtMillis = store.commsLastSyncAtMillis,
        )
        lastStatus = current
        return current
    }

    private fun updateStatus(config: CommsConfigDto, sms: String, calls: String) {
        lastStatus = Status(
            enabled = config.enabled,
            collectSms = config.collectSms,
            collectCallLog = config.collectCallLog,
            metadataOnly = config.metadataOnly,
            smsPermission = sms,
            callLogPermission = calls,
            notice = config.notice,
            lastSyncAtMillis = store.commsLastSyncAtMillis,
        )
    }

    private companion object {
        const val TAG = "FleetCommsReporter"
        const val DAY_MS = 24L * 60 * 60 * 1000
    }
}
