package ua.fleet.agent.enroll

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import ua.fleet.agent.crypto.DeviceKeyStore
import ua.fleet.agent.data.AgentStore
import ua.fleet.agent.device.DeviceInfoCollector
import ua.fleet.agent.net.ApiClient
import ua.fleet.agent.net.ServerUrlGuard
import ua.fleet.agent.net.dto.ChallengeRequest
import ua.fleet.agent.net.dto.DeviceMeResponse
import ua.fleet.agent.net.dto.DeviceTokenRequest
import ua.fleet.agent.net.dto.EnrollRequest
import ua.fleet.agent.net.dto.EnrollResponse
import ua.fleet.agent.net.dto.EnrollmentQrPayload
import ua.fleet.agent.net.dto.KeyRotationRequest
import java.security.MessageDigest

/**
 * Реєстрація пристрою та автентифікація агента.
 *
 * Схема: одноразовий токен → пара ключів у Keystore → device_id від сервера →
 * підпис nonce → короткоживучий device-токен.
 */
class EnrollmentRepository(context: Context) {

    private val appContext = context.applicationContext
    private val store = AgentStore(appContext)
    private val collector = DeviceInfoCollector(appContext)
    private val tokenMutex = Mutex()

    private var cachedToken: String? = null
    private var cachedTokenExpiresAt: Long = 0L
    private val rotationMutex = Mutex()

    val state: AgentStore get() = store

    init {
        // Псевдонім ключа в Keystore версіонується поколінням — сховище має
        // знати поточне ще до першого підпису.
        DeviceKeyStore.bindGeneration(store.keyGeneration)
        ServerUrlGuard.bind(store.serverUrl)
    }

    /** Реєструє пристрій. Повертає відповідь сервера й зберігає стан. */
    suspend fun enroll(serverUrl: String, rawToken: String): EnrollResponse =
        withContext(Dispatchers.IO) {
            val base = ApiClient.normalizeBaseUrl(serverUrl)
            val token = normalizeToken(rawToken)
            val publicKey = DeviceKeyStore.ensureKeyPair()

            val api = ApiClient.create(serverUrl)
            val response = api.enroll(
                EnrollRequest(
                    token = token,
                    publicKey = publicKey,
                    device = collector.collect(),
                )
            )

            store.serverUrl = base.removeSuffix("/api/v1/")
            ServerUrlGuard.bind(store.serverUrl)
            store.deviceId = response.deviceId
            store.deviceName = response.deviceName
            store.websocketUrl = response.websocketUrl
            store.managementMode = response.managementMode
            store.heartbeatIntervalSeconds = response.heartbeatIntervalSeconds
            store.transparencyNotice = response.transparencyNotice
            store.lastContactAtMillis = System.currentTimeMillis()

            Log.i(TAG, "Пристрій зареєстровано: ${response.deviceId}")
            response
        }

    /** Повертає дійсний device-токен, за потреби проходячи challenge–response. */
    suspend fun accessToken(): String = tokenMutex.withLock {
        val now = System.currentTimeMillis()
        cachedToken?.let { token ->
            if (now < cachedTokenExpiresAt) return@withLock token
        }
        val fresh = requestToken()
        cachedToken = fresh.first
        // Оновлюємо трохи раніше за фактичне протермінування.
        cachedTokenExpiresAt = now + (fresh.second - SKEW_SECONDS).coerceAtLeast(30) * 1000
        fresh.first
    }

    private suspend fun requestToken(): Pair<String, Long> = withContext(Dispatchers.IO) {
        val serverUrl = store.serverUrl ?: error("Пристрій не зареєстровано")
        val deviceId = store.deviceId ?: error("Пристрій не зареєстровано")
        val api = ApiClient.create(serverUrl)

        val challenge = api.challenge(ChallengeRequest(deviceId))
        val message = challenge.messageTemplate
            .replace("{device_id}", deviceId)
            .replace("{nonce}", challenge.nonce)
        val signature = DeviceKeyStore.sign(message.toByteArray(Charsets.UTF_8))

        val response = api.deviceToken(
            DeviceTokenRequest(deviceId = deviceId, nonce = challenge.nonce, signature = signature)
        )
        // Сервер сам каже, коли ключ час перевипустити — агент не вигадує
        // політику, а лише виконує її.
        store.keyRotationDue = response.keyRotationDue
        response.accessToken to response.expiresIn
    }

    // ------------------------------------------------ ротація ключа --------

    /**
     * Перевипускає ключ пристрою, якщо сервер цього просить.
     *
     * Викликається агентом при підключенні; безпечна до повторів і до
     * обриву мережі — незавершена заготовка просто прибирається.
     *
     * @return true, якщо ключ справді змінено
     */
    suspend fun rotateKeyIfRequested(force: Boolean = false): Boolean =
        rotationMutex.withLock {
            if (!force && !store.keyRotationDue) return@withLock false
            withContext(Dispatchers.IO) { performRotation() }
        }

    private suspend fun performRotation(): Boolean {
        val serverUrl = store.serverUrl ?: error("Пристрій не зареєстровано")
        val deviceId = store.deviceId ?: error("Пристрій не зареєстровано")
        val api = ApiClient.create(serverUrl)
        val bearer = "Bearer ${accessToken()}"

        val (newPublicKey, candidate) = DeviceKeyStore.prepareNextGeneration()
        try {
            val challenge = api.keyRotationChallenge(bearer)
            val digest = sha256Hex(newPublicKey)
            val message = challenge.messageTemplate
                .replace("{device_id}", deviceId)
                .replace("{sha256(new_public_key)}", digest)
                .replace("{nonce}", challenge.nonce)

            // Підпис саме СТАРИМ ключем: це і є доказ, що ротацію просить
            // той самий пристрій, а не хтось із вкраденим токеном.
            val signature = DeviceKeyStore.sign(message.toByteArray(Charsets.UTF_8))

            val response = api.rotateKey(
                bearer,
                KeyRotationRequest(
                    newPublicKey = newPublicKey,
                    nonce = challenge.nonce,
                    signature = signature,
                    reason = "scheduled",
                ),
            )

            DeviceKeyStore.commitGeneration(candidate)
            store.keyGeneration = candidate
            store.keyRotatedAtMillis = System.currentTimeMillis()
            store.keyRotationDue = false
            // Старий токен сервер анулював — наступний запит візьме новий.
            invalidateToken()
            Log.i(TAG, "Ключ пристрою перевипущено: покоління ${response.keyGeneration}")
            return true
        } catch (e: Exception) {
            // Заготовку прибираємо, поточний ключ лишається робочим.
            DeviceKeyStore.discardGeneration(candidate)
            Log.w(TAG, "Ротація ключа не вдалася — працюємо старим ключем", e)
            return false
        }
    }

    /** Скидає кеш токена: після ротації чи відкликання він уже недійсний. */
    fun invalidateToken() {
        cachedToken = null
        cachedTokenExpiresAt = 0L
    }

    private fun sha256Hex(value: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    /** Перевірка звʼязку: що сервер знає про цей пристрій. */
    suspend fun ping(): DeviceMeResponse = withContext(Dispatchers.IO) {
        val serverUrl = store.serverUrl ?: error("Пристрій не зареєстровано")
        val api = ApiClient.create(serverUrl)
        val response = api.me("Bearer ${accessToken()}")
        store.deviceName = response.name
        store.managementMode = response.managementMode
        store.transparencyNotice = response.transparencyNotice
        store.lastContactAtMillis = System.currentTimeMillis()
        response
    }

    /** Знімає пристрій з обліку локально (ключ і збережений стан видаляються). */
    fun reset() {
        invalidateToken()
        store.clear()
        DeviceKeyStore.deleteKey()
        DeviceKeyStore.bindGeneration(1)
        ServerUrlGuard.bind(null)
    }

    companion object {
        private const val TAG = "FleetEnrollment"
        private const val SKEW_SECONDS = 60L
        private const val TOKEN_BODY_LENGTH = 20

        /** Розбирає QR-код панелі: `{"v":1,"server":...,"token":...}`. */
        fun parseQrPayload(raw: String): EnrollmentQrPayload =
            ApiClient.json.decodeFromString(EnrollmentQrPayload.serializer(), raw)

        /** Приводить введений вручну токен до канонічного вигляду. */
        fun normalizeToken(raw: String): String {
            val cleaned = raw.trim().uppercase()
                .filter { it.isLetterOrDigit() }
                .removePrefix("FLEET")
            require(cleaned.length == TOKEN_BODY_LENGTH) {
                "Токен має містити $TOKEN_BODY_LENGTH символів після префікса FLEET-"
            }
            return "FLEET-" + cleaned.chunked(4).joinToString("-")
        }

        fun isTokenPlausible(raw: String): Boolean = try {
            normalizeToken(raw); true
        } catch (_: IllegalArgumentException) {
            false
        }
    }
}
