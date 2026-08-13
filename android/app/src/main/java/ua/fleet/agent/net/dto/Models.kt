package ua.fleet.agent.net.dto

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/** Дані пристрою, які передаються під час реєстрації. */
@Serializable
data class DeviceEnrollInfo(
    val manufacturer: String? = null,
    val model: String? = null,
    val product: String? = null,
    @SerialName("android_version") val androidVersion: String? = null,
    @SerialName("sdk_int") val sdkInt: Int? = null,
    @SerialName("security_patch") val securityPatch: String? = null,
    @SerialName("build_id") val buildId: String? = null,
    @SerialName("kernel_version") val kernelVersion: String? = null,
    @SerialName("android_id") val androidId: String? = null,
    @SerialName("serial_number") val serialNumber: String? = null,
    @SerialName("management_mode") val managementMode: String = "unmanaged",
    @SerialName("agent_version") val agentVersion: String? = null,
    @SerialName("display_name") val displayName: String? = null,
)

@Serializable
data class EnrollRequest(
    val token: String,
    @SerialName("public_key") val publicKey: String,
    val device: DeviceEnrollInfo,
)

@Serializable
data class EnrollResponse(
    @SerialName("device_id") val deviceId: String,
    @SerialName("device_name") val deviceName: String,
    @SerialName("server_time") val serverTime: String,
    @SerialName("api_base_url") val apiBaseUrl: String,
    @SerialName("websocket_url") val websocketUrl: String,
    @SerialName("heartbeat_interval_seconds") val heartbeatIntervalSeconds: Int,
    @SerialName("management_mode") val managementMode: String,
    @SerialName("transparency_notice") val transparencyNotice: String,
)

@Serializable
data class ChallengeRequest(@SerialName("device_id") val deviceId: String)

@Serializable
data class ChallengeResponse(
    val nonce: String,
    @SerialName("expires_at") val expiresAt: String,
    @SerialName("message_template") val messageTemplate: String,
    val algorithm: String,
)

@Serializable
data class DeviceTokenRequest(
    @SerialName("device_id") val deviceId: String,
    val nonce: String,
    val signature: String,
)

@Serializable
data class DeviceTokenResponse(
    @SerialName("access_token") val accessToken: String,
    @SerialName("token_type") val tokenType: String = "bearer",
    @SerialName("expires_in") val expiresIn: Long,
    /** Сервер каже, що ключ застарів за політикою — час перевипустити. */
    @SerialName("key_rotation_due") val keyRotationDue: Boolean = false,
    @SerialName("key_age_days") val keyAgeDays: Int = 0,
)

@Serializable
data class DeviceMeResponse(
    @SerialName("device_id") val deviceId: String,
    val name: String,
    val status: String,
    @SerialName("management_mode") val managementMode: String,
    @SerialName("enrolled_at") val enrolledAt: String,
    @SerialName("transparency_notice") val transparencyNotice: String,
)

// ------------------------------------------------------- команди ----------

@Serializable
data class PendingCommandDto(
    val id: String,
    val type: String,
    val payload: JsonObject? = null,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
)

@Serializable
data class CommandProgressDto(
    val percent: Int,
    val message: String? = null,
)

@Serializable
data class CommandResultDto(
    val status: String,
    val result: JsonObject? = null,
    val error: String? = null,
)

@Serializable
data class MessageDto(val message: String)

// ------------------------------------------------ інвентар застосунків ----

@Serializable
data class AppInventoryItemDto(
    @SerialName("package_name") val packageName: String,
    @SerialName("app_name") val appName: String? = null,
    @SerialName("version_name") val versionName: String? = null,
    @SerialName("version_code") val versionCode: Long? = null,
    @SerialName("is_system") val isSystem: Boolean = false,
    @SerialName("is_enabled") val isEnabled: Boolean = true,
    @SerialName("installer_package") val installerPackage: String? = null,
)

@Serializable
data class AppInventoryReportDto(val apps: List<AppInventoryItemDto>)

// ---------------------------------------------------------- геолокація ----

@Serializable
data class LocationFixDto(
    val latitude: Double,
    val longitude: Double,
    @SerialName("accuracy_m") val accuracyM: Float? = null,
    @SerialName("altitude_m") val altitudeM: Double? = null,
    @SerialName("speed_mps") val speedMps: Float? = null,
    @SerialName("bearing_deg") val bearingDeg: Float? = null,
    val provider: String? = null,
    val source: String = "periodic",
    @SerialName("is_mock") val isMock: Boolean = false,
    @SerialName("battery_level") val batteryLevel: Int? = null,
    /** ISO-8601 у UTC. */
    @SerialName("recorded_at") val recordedAt: String,
)

@Serializable
data class LocationBatchDto(val fixes: List<LocationFixDto>)

@Serializable
data class LocationConfigDto(
    val enabled: Boolean,
    @SerialName("interval_seconds") val intervalSeconds: Int,
    @SerialName("min_distance_meters") val minDistanceMeters: Int,
    @SerialName("high_accuracy") val highAccuracy: Boolean,
    @SerialName("retention_days") val retentionDays: Int = 90,
    @SerialName("server_time") val serverTime: String? = null,
)

@Serializable
data class LocationBatchAcceptedDto(
    val accepted: Int,
    val rejected: Int,
    val config: LocationConfigDto,
)

@Serializable
data class LocationPermissionDto(
    @SerialName("permission_state") val permissionState: String,
    @SerialName("background_permission") val backgroundPermission: Boolean,
)

// -------------------------------------------------------------- файли ----

@Serializable
data class FileRootDto(
    val id: String,
    val path: String,
    val label: String? = null,
    val writable: Boolean = false,
    @SerialName("max_file_size_mb") val maxFileSizeMb: Int? = null,
    @SerialName("allow_privileged") val allowPrivileged: Boolean = false,
)

@Serializable
data class FileEntryDto(
    val name: String,
    val path: String,
    @SerialName("is_dir") val isDir: Boolean = false,
    @SerialName("size_bytes") val sizeBytes: Long? = null,
    @SerialName("modified_at") val modifiedAt: String? = null,
    val permissions: String? = null,
    @SerialName("is_symlink") val isSymlink: Boolean = false,
    @SerialName("mime_type") val mimeType: String? = null,
    val readable: Boolean = true,
    val writable: Boolean = false,
)

@Serializable
data class TransferStatusDto(
    val id: String,
    val direction: String,
    @SerialName("remote_path") val remotePath: String,
    val filename: String,
    val status: String,
    @SerialName("size_bytes") val sizeBytes: Long? = null,
    @SerialName("received_bytes") val receivedBytes: Long = 0,
    @SerialName("chunk_size") val chunkSize: Int = 1_048_576,
    val sha256: String? = null,
)

@Serializable
data class ChunkAcceptedDto(
    @SerialName("received_bytes") val receivedBytes: Long,
    @SerialName("chunk_size") val chunkSize: Int,
)

@Serializable
data class TransferProgressDto(
    @SerialName("received_bytes") val receivedBytes: Long,
)

@Serializable
data class TransferCompleteDto(
    val sha256: String? = null,
    @SerialName("size_bytes") val sizeBytes: Long? = null,
)

@Serializable
data class TransferFailDto(val error: String)

// --------------------------------------------------------- телеметрія ----

@Serializable
data class TelemetryDto(
    val manufacturer: String? = null,
    val model: String? = null,
    val product: String? = null,
    @SerialName("android_version") val androidVersion: String? = null,
    @SerialName("sdk_int") val sdkInt: Int? = null,
    @SerialName("security_patch") val securityPatch: String? = null,
    @SerialName("build_id") val buildId: String? = null,
    @SerialName("kernel_version") val kernelVersion: String? = null,
    @SerialName("agent_version") val agentVersion: String? = null,
    @SerialName("uptime_seconds") val uptimeSeconds: Long? = null,
    @SerialName("battery_level") val batteryLevel: Int? = null,
    @SerialName("battery_status") val batteryStatus: String? = null,
    @SerialName("battery_health") val batteryHealth: String? = null,
    @SerialName("battery_temperature_c") val batteryTemperatureC: Float? = null,
    val charging: Boolean? = null,
    @SerialName("ram_total_bytes") val ramTotalBytes: Long? = null,
    @SerialName("ram_available_bytes") val ramAvailableBytes: Long? = null,
    @SerialName("storage_total_bytes") val storageTotalBytes: Long? = null,
    @SerialName("storage_free_bytes") val storageFreeBytes: Long? = null,
    @SerialName("cpu_cores") val cpuCores: Int? = null,
    @SerialName("cpu_load_percent") val cpuLoadPercent: Float? = null,
    @SerialName("cpu_abi") val cpuAbi: String? = null,
    @SerialName("network_type") val networkType: String? = null,
    @SerialName("collected_at") val collectedAt: String? = null,
)

@Serializable
data class TelemetryAcceptedDto(
    val accepted: Boolean = true,
    @SerialName("next_report_seconds") val nextReportSeconds: Int,
    @SerialName("alerts_raised") val alertsRaised: Int = 0,
)

// -------------------------------------------------- віддалена підтримка ---

@Serializable
data class SessionConsentDto(
    val granted: Boolean,
    @SerialName("input_available") val inputAvailable: Boolean = false,
    val reason: String? = null,
)

@Serializable
data class SessionEndDto(val reason: String = "stopped")

@Serializable
data class SupportSessionDto(
    val id: String,
    val state: String,
    @SerialName("input_enabled") val inputEnabled: Boolean = false,
)

/** Формат помилки бекенду: {"error": {"code", "message", "details"}}. */
@Serializable
data class ApiErrorBody(val error: ApiErrorDetail)

@Serializable
data class ApiErrorDetail(
    val code: String,
    val message: String,
)

/** Вміст QR-коду, який показує панель адміністратора. */
@Serializable
data class EnrollmentQrPayload(
    val v: Int = 1,
    val server: String,
    @SerialName("enroll_url") val enrollUrl: String? = null,
    val token: String,
)


// ------------------------------------------------ ротація ключа пристрою ---

@Serializable
data class KeyRotationChallengeResponse(
    val nonce: String,
    @SerialName("expires_at") val expiresAt: String? = null,
    @SerialName("message_template") val messageTemplate: String =
        "fleet-device-key-rotation:{device_id}:{sha256(new_public_key)}:{nonce}",
    val algorithm: String = "SHA256withECDSA",
)

@Serializable
data class KeyRotationRequest(
    @SerialName("new_public_key") val newPublicKey: String,
    val nonce: String,
    val signature: String,
    val reason: String = "scheduled",
)

@Serializable
data class KeyRotationResponse(
    @SerialName("device_id") val deviceId: String,
    @SerialName("key_fingerprint") val keyFingerprint: String,
    @SerialName("key_generation") val keyGeneration: Int,
    @SerialName("rotated_at") val rotatedAt: String? = null,
    @SerialName("reauthentication_required") val reauthenticationRequired: Boolean = true,
)


// ---------------------------------------- комунікаційний модуль (SMS/дзвінки)

@Serializable
data class CommsConfigDto(
    val enabled: Boolean = false,
    @SerialName("collect_sms") val collectSms: Boolean = false,
    @SerialName("collect_call_log") val collectCallLog: Boolean = false,
    @SerialName("metadata_only") val metadataOnly: Boolean = false,
    @SerialName("history_days") val historyDays: Int = 30,
    @SerialName("max_batch") val maxBatch: Int = 2000,
    val notice: String = "",
)

@Serializable
data class CommsPermissionDto(
    @SerialName("sms_permission") val smsPermission: String,
    @SerialName("call_log_permission") val callLogPermission: String,
)

@Serializable
data class SmsItemDto(
    @SerialName("provider_id") val providerId: String? = null,
    val direction: String = "other",
    val address: String? = null,
    val body: String? = null,
    @SerialName("sent_at") val sentAt: String? = null,
)

@Serializable
data class CallItemDto(
    @SerialName("provider_id") val providerId: String? = null,
    val direction: String = "other",
    val number: String? = null,
    val contact: String? = null,
    @SerialName("duration_seconds") val durationSeconds: Int? = null,
    @SerialName("occurred_at") val occurredAt: String? = null,
)

@Serializable
data class CommsBatchDto(
    val sms: List<SmsItemDto> = emptyList(),
    val calls: List<CallItemDto> = emptyList(),
)

@Serializable
data class CommsBatchAcceptedDto(
    @SerialName("sms_stored") val smsStored: Int = 0,
    @SerialName("calls_stored") val callsStored: Int = 0,
    val rejected: Int = 0,
    val config: CommsConfigDto,
)
