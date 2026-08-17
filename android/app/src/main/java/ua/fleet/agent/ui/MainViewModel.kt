package ua.fleet.agent.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import ua.fleet.agent.FleetAgentApplication
import ua.fleet.agent.command.AgentService
import ua.fleet.agent.command.StartOutcome
import ua.fleet.agent.command.CommandProcessor
import ua.fleet.agent.crypto.DeviceKeyStore
import ua.fleet.agent.device.DeviceInfoCollector
import ua.fleet.agent.enroll.EnrollmentRepository
import ua.fleet.agent.location.LocationReporter
import ua.fleet.agent.net.ApiClient
import ua.fleet.agent.support.RemoteInputService
import ua.fleet.agent.support.ScreenShareService
import ua.fleet.agent.support.SupportSessionState

data class AgentUiState(
    val loading: Boolean = true,
    val busy: Boolean = false,
    val enrolled: Boolean = false,
    val serverInput: String = "https://",
    val tokenInput: String = "",
    val error: String? = null,
    val info: String? = null,
    // Дані зареєстрованого пристрою
    val deviceName: String? = null,
    val deviceId: String? = null,
    val serverUrl: String? = null,
    val managementMode: String = DeviceInfoCollector.MODE_UNMANAGED,
    val transparencyNotice: String? = null,
    val lastContactAtMillis: Long = 0L,
    val hardwareBackedKey: Boolean = false,
    val serviceEnabled: Boolean = true,
    // Система заборонила постійне сповіщення — агент зупинено, і це видно.
    val foregroundBlocked: Boolean = false,
    val allowRootInstall: Boolean = false,
    val installedAppCount: Int = 0,
    val location: LocationReporter.Status? = null,
    // Стан віддаленої підтримки — навмисно видимий власнику пристрою.
    val support: SupportUiState = SupportUiState(),
    val comms: CommsUiState = CommsUiState(),
)

/**
 * Стан комунікаційного модуля для екрана агента.
 *
 * Модуль опційний і вимкнений за замовчуванням. Коли адміністратор його
 * вмикає, власник пристрою бачить це тут — прихованого збору немає.
 */
data class CommsUiState(
    val enabled: Boolean = false,
    val collectSms: Boolean = false,
    val collectCallLog: Boolean = false,
    val metadataOnly: Boolean = false,
    val smsPermission: String = "unknown",
    val callLogPermission: String = "unknown",
    val notice: String = "",
    val lastSyncAtMillis: Long = 0L,
    val needsPermissions: Boolean = false,
)

/**
 * Те, що бачить користувач про сеанс підтримки просто зараз.
 *
 * Дублює постійне сповіщення й системний індикатор Android: сеанс має бути
 * помітним щонайменше у трьох місцях, і жодне з них агент не приховує.
 */
data class SupportUiState(
    val active: Boolean = false,
    val requestedBy: String? = null,
    val reason: String? = null,
    val streaming: Boolean = false,
    val connected: Boolean = false,
    val elapsedSeconds: Long = 0,
    val reconnects: Int = 0,
    val inputRequested: Boolean = false,
    val inputServiceEnabled: Boolean = false,
)

class MainViewModel(application: Application) : AndroidViewModel(application) {

    private val repository: EnrollmentRepository =
        (application as FleetAgentApplication).enrollmentRepository

    private val processor = CommandProcessor(application, repository)

    private val _state = MutableStateFlow(AgentUiState())
    val state: StateFlow<AgentUiState> = _state.asStateFlow()

    /** Перемальовує екран, щойно змінюється стан сеансу підтримки. */
    private val supportListener: () -> Unit = { refreshSupport() }

    init {
        refreshFromStore()
        SupportSessionState.addListener(supportListener)
        // Лічильник тривалості сеансу має йти й без зовнішніх подій.
        viewModelScope.launch {
            while (true) {
                kotlinx.coroutines.delay(1000)
                if (SupportSessionState.current != null) refreshSupport()
            }
        }
    }

    override fun onCleared() {
        SupportSessionState.removeListener(supportListener)
        super.onCleared()
    }

    /** Збирає видимий стан сеансу підтримки. */
    /** Збирає стан комунікаційного модуля для UI. */
    private fun readComms(): CommsUiState {
        val status = runCatching { processor.commsReporter.currentStatus() }.getOrNull()
            ?: return CommsUiState()
        return CommsUiState(
            enabled = status.enabled,
            collectSms = status.collectSms,
            collectCallLog = status.collectCallLog,
            metadataOnly = status.metadataOnly,
            smsPermission = status.smsPermission,
            callLogPermission = status.callLogPermission,
            notice = status.notice,
            lastSyncAtMillis = status.lastSyncAtMillis,
            needsPermissions = processor.commsReporter.neededPermissions().isNotEmpty(),
        )
    }

    /** Синхронізує модуль із сервером (виклик після надання дозволів). */
    fun refreshComms() {
        viewModelScope.launch {
            runCatching { processor.commsReporter.sync() }
            _state.update { it.copy(comms = readComms()) }
        }
    }

    /** Дозволи, які треба запросити у користувача для комунікаційного модуля. */
    fun commsPermissionsToRequest(): Array<String> =
        processor.commsReporter.neededPermissions().toTypedArray()

    fun refreshSupport() {
        val session = SupportSessionState.current
        _state.update {
            it.copy(
                support = SupportUiState(
                    active = session != null,
                    requestedBy = session?.requestedBy,
                    reason = session?.reason,
                    streaming = SupportSessionState.streaming,
                    connected = SupportSessionState.connected,
                    elapsedSeconds = SupportSessionState.elapsedSeconds(),
                    reconnects = SupportSessionState.reconnects,
                    inputRequested = SupportSessionState.inputRequested,
                    inputServiceEnabled = RemoteInputService.isEnabled(getApplication()),
                )
            )
        }
    }

    /**
     * Зупиняє сеанс з боку користувача пристрою.
     *
     * Це той самий шлях, що й кнопка в сповіщенні: власник пристрою може
     * обірвати показ екрана будь-якої миті, не питаючи адміністратора.
     */
    fun stopSupportSession() {
        ScreenShareService.stop(getApplication())
        _state.update { it.copy(info = "Сеанс підтримки зупинено") }
    }

    private fun refreshFromStore() {
        val store = repository.state
        _state.update {
            it.copy(
                loading = false,
                enrolled = store.isEnrolled,
                deviceName = store.deviceName,
                deviceId = store.deviceId,
                serverUrl = store.serverUrl,
                managementMode = store.managementMode,
                transparencyNotice = store.transparencyNotice,
                lastContactAtMillis = store.lastContactAtMillis,
                hardwareBackedKey = DeviceKeyStore.hasKey() && DeviceKeyStore.isHardwareBacked(),
                serverInput = store.serverUrl ?: it.serverInput,
                serviceEnabled = store.serviceEnabled,
                allowRootInstall = store.allowRootInstall,
                location = runCatching { processor.locationReporter.status() }.getOrNull(),
                comms = readComms(),
            )
        }
        refreshSupport()
        if (store.isEnrolled && store.serviceEnabled) {
            // Тут застосунок на передньому плані — саме той момент, коли
            // система дозволяє підняти службу. Тому будь-яку попередню
            // відмову намагаємось відіграти назад щоразу при відкритті.
            val outcome = AgentService.start(getApplication())
            if (outcome == StartOutcome.NOT_ALLOWED) {
                store.foregroundBlockedAtMillis = System.currentTimeMillis()
            }
            _state.update { it.copy(foregroundBlocked = store.foregroundBlockedAtMillis > 0L) }
        }
    }

    fun onServerChanged(value: String) = _state.update { it.copy(serverInput = value, error = null) }

    fun onTokenChanged(value: String) = _state.update { it.copy(tokenInput = value, error = null) }

    /** Обробляє вміст QR-коду: заповнює адресу сервера й токен. */
    fun onQrScanned(raw: String) {
        val payload = runCatching { EnrollmentRepository.parseQrPayload(raw) }.getOrNull()
        if (payload == null) {
            // QR міг містити лише токен.
            if (EnrollmentRepository.isTokenPlausible(raw)) {
                _state.update { it.copy(tokenInput = raw.trim(), error = null) }
            } else {
                _state.update { it.copy(error = "QR-код не розпізнано") }
            }
            return
        }
        _state.update {
            it.copy(serverInput = payload.server, tokenInput = payload.token, error = null)
        }
    }

    fun enroll() {
        val current = _state.value
        if (current.busy) return

        if (!EnrollmentRepository.isTokenPlausible(current.tokenInput)) {
            _state.update { it.copy(error = "Некоректний формат токена") }
            return
        }

        _state.update { it.copy(busy = true, error = null, info = null) }
        viewModelScope.launch {
            runCatching { repository.enroll(current.serverInput, current.tokenInput) }
                .onSuccess { response ->
                    _state.update {
                        it.copy(
                            busy = false,
                            tokenInput = "",
                            info = "Пристрій «${response.deviceName}» зареєстровано",
                        )
                    }
                    refreshFromStore()
                }
                .onFailure { throwable ->
                    _state.update {
                        it.copy(busy = false, error = ApiClient.describeError(throwable))
                    }
                }
        }
    }

    fun checkConnection() {
        if (_state.value.busy) return
        _state.update { it.copy(busy = true, error = null, info = null) }
        viewModelScope.launch {
            runCatching { repository.ping() }
                .onSuccess { response ->
                    _state.update {
                        it.copy(busy = false, info = "Звʼязок із сервером є (${response.status})")
                    }
                    refreshFromStore()
                }
                .onFailure { throwable ->
                    _state.update {
                        it.copy(busy = false, error = ApiClient.describeError(throwable))
                    }
                }
        }
    }

    // ------------------------------------------------ керування агентом ---

    fun toggleService(enabled: Boolean) {
        repository.state.serviceEnabled = enabled
        var blocked = false
        if (enabled) {
            blocked = AgentService.start(getApplication()) == StartOutcome.NOT_ALLOWED
        } else {
            AgentService.stop(getApplication())
            repository.state.foregroundBlockedAtMillis = 0L
        }
        _state.update { it.copy(serviceEnabled = enabled, foregroundBlocked = blocked) }
    }

    fun toggleRootFallback(enabled: Boolean) {
        repository.state.allowRootInstall = enabled
        _state.update { it.copy(allowRootInstall = enabled) }
    }

    /** Надіслати серверу актуальний перелік застосунків. */
    fun sendInventory() {
        if (_state.value.busy) return
        _state.update { it.copy(busy = true, error = null, info = null) }
        viewModelScope.launch {
            runCatching { processor.reportInventory() }
                .onSuccess { count ->
                    repository.state.lastInventoryAtMillis = System.currentTimeMillis()
                    _state.update {
                        it.copy(
                            busy = false,
                            installedAppCount = count,
                            info = "Надіслано перелік із $count застосунків",
                        )
                    }
                    refreshFromStore()
                }
                .onFailure { throwable ->
                    _state.update {
                        it.copy(busy = false, error = ApiClient.describeError(throwable))
                    }
                }
        }
    }

    /** Забрати й виконати команди, що очікують на сервері. */
    fun checkCommands() {
        if (_state.value.busy) return
        _state.update { it.copy(busy = true, error = null, info = null) }
        viewModelScope.launch {
            runCatching { processor.runPending() }
                .onSuccess { handled ->
                    _state.update {
                        it.copy(
                            busy = false,
                            info = if (handled == 0) {
                                "Завдань немає"
                            } else {
                                "Оброблено завдань: $handled"
                            },
                        )
                    }
                    refreshFromStore()
                }
                .onFailure { throwable ->
                    _state.update {
                        it.copy(busy = false, error = ApiClient.describeError(throwable))
                    }
                }
        }
    }

    // ---------------------------------------------------- геолокація ------

    /** Після діалогу дозволів: одразу повідомляємо серверу новий стан. */
    fun onLocationPermissionResult() {
        viewModelScope.launch {
            runCatching { processor.locationReporter.syncPermissions(force = true) }
                .onFailure { throwable ->
                    _state.update { it.copy(error = ApiClient.describeError(throwable)) }
                }
            refreshFromStore()
        }
    }

    /** Ручна синхронізація конфігурації геолокації з сервером. */
    fun syncLocationConfig() {
        if (_state.value.busy) return
        _state.update { it.copy(busy = true, error = null, info = null) }
        viewModelScope.launch {
            runCatching {
                processor.locationReporter.syncPermissions(force = true)
                processor.locationReporter.syncConfig()
            }
                .onSuccess { config ->
                    _state.update {
                        it.copy(
                            busy = false,
                            info = if (config?.enabled == true) {
                                "Геолокація увімкнена адміністратором"
                            } else {
                                "Геолокація вимкнена"
                            },
                        )
                    }
                    refreshFromStore()
                }
                .onFailure { throwable ->
                    _state.update {
                        it.copy(busy = false, error = ApiClient.describeError(throwable))
                    }
                }
        }
    }

    /** Локальне зняття з обліку: видаляє ключ і збережений стан. */
    fun unenroll() {
        AgentService.stop(getApplication())
        repository.reset()
        _state.update {
            AgentUiState(loading = false, info = "Пристрій знято з обліку на цьому пристрої")
        }
    }
}
