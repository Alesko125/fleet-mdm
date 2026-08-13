package ua.fleet.agent.net

import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Streaming
import retrofit2.http.Url
import ua.fleet.agent.net.dto.AppInventoryReportDto
import ua.fleet.agent.net.dto.ChallengeRequest
import ua.fleet.agent.net.dto.ChunkAcceptedDto
import ua.fleet.agent.net.dto.ChallengeResponse
import ua.fleet.agent.net.dto.CommandProgressDto
import ua.fleet.agent.net.dto.CommsBatchAcceptedDto
import ua.fleet.agent.net.dto.CommsBatchDto
import ua.fleet.agent.net.dto.CommsConfigDto
import ua.fleet.agent.net.dto.CommsPermissionDto
import ua.fleet.agent.net.dto.CommandResultDto
import ua.fleet.agent.net.dto.DeviceMeResponse
import ua.fleet.agent.net.dto.DeviceTokenRequest
import ua.fleet.agent.net.dto.DeviceTokenResponse
import ua.fleet.agent.net.dto.KeyRotationChallengeResponse
import ua.fleet.agent.net.dto.KeyRotationRequest
import ua.fleet.agent.net.dto.KeyRotationResponse
import ua.fleet.agent.net.dto.EnrollRequest
import ua.fleet.agent.net.dto.EnrollResponse
import ua.fleet.agent.net.dto.FileRootDto
import ua.fleet.agent.net.dto.LocationBatchAcceptedDto
import ua.fleet.agent.net.dto.LocationBatchDto
import ua.fleet.agent.net.dto.LocationConfigDto
import ua.fleet.agent.net.dto.LocationPermissionDto
import ua.fleet.agent.net.dto.MessageDto
import ua.fleet.agent.net.dto.PendingCommandDto
import ua.fleet.agent.net.dto.TransferCompleteDto
import ua.fleet.agent.net.dto.TransferFailDto
import ua.fleet.agent.net.dto.TransferProgressDto
import ua.fleet.agent.net.dto.SessionConsentDto
import ua.fleet.agent.net.dto.SessionEndDto
import ua.fleet.agent.net.dto.SupportSessionDto
import ua.fleet.agent.net.dto.TelemetryAcceptedDto
import ua.fleet.agent.net.dto.TelemetryDto
import ua.fleet.agent.net.dto.TransferStatusDto

/** REST-контракт агента (базовий URL — `<server>/api/v1/`). */
interface FleetApi {

    // --- реєстрація та автентифікація ---

    @POST("enroll")
    suspend fun enroll(@Body body: EnrollRequest): EnrollResponse

    @POST("device/auth/challenge")
    suspend fun challenge(@Body body: ChallengeRequest): ChallengeResponse

    @POST("device/auth/token")
    suspend fun deviceToken(@Body body: DeviceTokenRequest): DeviceTokenResponse

    @POST("device/key/challenge")
    suspend fun keyRotationChallenge(
        @Header("Authorization") bearer: String,
    ): KeyRotationChallengeResponse

    @POST("device/key/rotate")
    suspend fun rotateKey(
        @Header("Authorization") bearer: String,
        @Body body: KeyRotationRequest,
    ): KeyRotationResponse

    @GET("device/me")
    suspend fun me(@Header("Authorization") bearer: String): DeviceMeResponse

    // --- командний канал ---

    @GET("device/commands/pending")
    suspend fun pendingCommands(@Header("Authorization") bearer: String): List<PendingCommandDto>

    @POST("device/commands/{id}/ack")
    suspend fun ackCommand(
        @Header("Authorization") bearer: String,
        @Path("id") commandId: String,
    ): MessageDto

    @POST("device/commands/{id}/progress")
    suspend fun reportProgress(
        @Header("Authorization") bearer: String,
        @Path("id") commandId: String,
        @Body body: CommandProgressDto,
    ): MessageDto

    @POST("device/commands/{id}/result")
    suspend fun reportResult(
        @Header("Authorization") bearer: String,
        @Path("id") commandId: String,
        @Body body: CommandResultDto,
    ): MessageDto

    // --- застосунки ---

    @POST("device/apps")
    suspend fun reportApps(
        @Header("Authorization") bearer: String,
        @Body body: AppInventoryReportDto,
    ): MessageDto

    // --- віддалена підтримка ---

    @POST("device/support-sessions/{id}/consent")
    suspend fun supportConsent(
        @Header("Authorization") bearer: String,
        @Path("id") sessionId: String,
        @Body body: SessionConsentDto,
    ): SupportSessionDto

    @POST("device/support-sessions/{id}/end")
    suspend fun endSupportSession(
        @Header("Authorization") bearer: String,
        @Path("id") sessionId: String,
        @Body body: SessionEndDto,
    ): SupportSessionDto

    // --- телеметрія ---

    @POST("device/telemetry")
    suspend fun reportTelemetry(
        @Header("Authorization") bearer: String,
        @Body body: TelemetryDto,
    ): TelemetryAcceptedDto

    // --- геолокація ---

    @GET("device/location/config")
    suspend fun locationConfig(@Header("Authorization") bearer: String): LocationConfigDto

    @POST("device/location/permissions")
    suspend fun reportLocationPermissions(
        @Header("Authorization") bearer: String,
        @Body body: LocationPermissionDto,
    ): LocationConfigDto

    @POST("device/location")
    suspend fun uploadLocation(
        @Header("Authorization") bearer: String,
        @Body body: LocationBatchDto,
    ): LocationBatchAcceptedDto

    // --- комунікаційний модуль ---

    @GET("device/comms/config")
    suspend fun commsConfig(@Header("Authorization") bearer: String): CommsConfigDto

    @POST("device/comms/permissions")
    suspend fun reportCommsPermissions(
        @Header("Authorization") bearer: String,
        @Body body: CommsPermissionDto,
    ): CommsConfigDto

    @POST("device/comms/data")
    suspend fun uploadCommsData(
        @Header("Authorization") bearer: String,
        @Body body: CommsBatchDto,
    ): CommsBatchAcceptedDto

    // --- файли ---

    @GET("device/files/roots")
    suspend fun fileRoots(@Header("Authorization") bearer: String): List<FileRootDto>

    @GET
    suspend fun transferStatus(
        @Url url: String,
        @Header("Authorization") bearer: String,
    ): TransferStatusDto

    @POST
    suspend fun uploadChunk(
        @Url url: String,
        @Header("Authorization") bearer: String,
        @Header("X-Chunk-Offset") offset: Long,
        @Body body: RequestBody,
    ): ChunkAcceptedDto

    @Streaming
    @GET
    suspend fun downloadRange(
        @Url url: String,
        @Header("Authorization") bearer: String,
        @Header("Range") range: String?,
    ): ResponseBody

    @POST
    suspend fun transferProgress(
        @Url url: String,
        @Header("Authorization") bearer: String,
        @Body body: TransferProgressDto,
    ): MessageDto

    @POST
    suspend fun transferComplete(
        @Url url: String,
        @Header("Authorization") bearer: String,
        @Body body: TransferCompleteDto,
    ): TransferStatusDto

    @POST
    suspend fun transferFail(
        @Url url: String,
        @Header("Authorization") bearer: String,
        @Body body: TransferFailDto,
    ): MessageDto

    /** Завантаження APK. URL приходить у payload команди. */
    @Streaming
    @GET
    suspend fun downloadApk(
        @Url url: String,
        @Header("Authorization") bearer: String,
    ): ResponseBody
}
