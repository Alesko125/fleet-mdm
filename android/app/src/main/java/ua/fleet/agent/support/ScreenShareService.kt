package ua.fleet.agent.support

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import java.io.ByteArrayOutputStream
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import ua.fleet.agent.FleetAgentApplication
import ua.fleet.agent.R
import ua.fleet.agent.net.ApiClient

/**
 * Трансляція екрана під час сеансу підтримки.
 *
 * * працює лише як **foreground service** з постійним сповіщенням, у якому є
 *   кнопка «Зупинити» — користувач може обірвати сеанс будь-якої миті;
 * * використовує `MediaProjection`, тож Android додатково показує власний
 *   індикатор трансляції; обійти його ми не намагаємось;
 * * частота, масштаб і якість кадрів підлаштовуються під канал за підказками
 *   сервера.
 */
class ScreenShareService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private var socket: SupportSocket? = null
    private var captureJob: Job? = null
    private var watchdogJob: Job? = null

    @Volatile private var maxFps = 4
    @Volatile private var scalePercent = 50
    @Volatile private var jpegQuality = 60

    private var request: SupportSessionRequest? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSession("stopped_by_user")
            return START_NOT_STICKY
        }

        val payload = intent?.getStringExtra(EXTRA_REQUEST)
        val parsed = payload?.let {
            runCatching {
                ApiClient.json.decodeFromString(SupportSessionRequest.serializer(), it)
            }.getOrNull()
        }
        val resultCode = intent?.getIntExtra(EXTRA_RESULT_CODE, 0) ?: 0
        val data: Intent? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent?.getParcelableExtra(EXTRA_RESULT_DATA, Intent::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent?.getParcelableExtra(EXTRA_RESULT_DATA)
        }

        if (parsed == null || data == null) {
            stopSelf()
            return START_NOT_STICKY
        }

        request = parsed
        startForeground(NOTIFICATION_ID, buildNotification(parsed))

        val manager = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        projection = manager.getMediaProjection(resultCode, data)?.also { media ->
            media.registerCallback(
                object : MediaProjection.Callback() {
                    override fun onStop() {
                        // Користувач зупинив трансляцію системним індикатором.
                        stopSession("stopped_by_user")
                    }
                },
                null,
            )
        }

        if (projection == null) {
            stopSelf()
            return START_NOT_STICKY
        }

        SupportSessionState.begin(parsed)
        startStreaming(parsed)
        return START_STICKY
    }

    // ------------------------------------------------------------ потік ---

    private fun startStreaming(session: SupportSessionRequest) {
        val app = application as FleetAgentApplication
        val repository = app.enrollmentRepository
        val metrics = displayMetrics()

        socket = SupportSocket(
            url = session.websocketUrl,
            tokenProvider = { repository.accessToken() },
            onQuality = { fps, scale, quality ->
                maxFps = fps.coerceIn(1, 30)
                scalePercent = scale.coerceIn(10, 100)
                jpegQuality = quality.coerceIn(10, 95)
                Log.i(TAG, "Нова якість: $maxFps к/с, $scalePercent%, JPEG $jpegQuality")
            },
            onCommand = { message -> RemoteInputService.dispatch(this, message) },
            onClosed = { stopSession("stream_closed") },
            scope = scope,
            onReady = { channel -> channel.sendText(metricsPayload(metrics)) },
        ).also { it.connect() }

        val width = metrics.widthPixels * scalePercent / 100
        val height = metrics.heightPixels * scalePercent / 100

        imageReader = ImageReader.newInstance(
            metrics.widthPixels, metrics.heightPixels, PixelFormat.RGBA_8888, 2
        )
        virtualDisplay = projection?.createVirtualDisplay(
            "fleet-support",
            metrics.widthPixels,
            metrics.heightPixels,
            metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            imageReader?.surface,
            null,
            null,
        )

        SupportSessionState.markStreaming(true)
        captureJob = scope.launch { captureLoop(width, height) }
        watchdogJob = scope.launch { watchdog() }
    }

    /**
     * Страховка на випадок, якщо сервер недоступний.
     *
     * Сеанс має закінчитися й тоді, коли підтвердження ззовні не прийшло:
     * трансляція екрана не повинна тривати довше за дозволений час лише
     * через те, що обірвався звʼязок.
     */
    private suspend fun watchdog() {
        while (scope.isActive) {
            delay(WATCHDOG_INTERVAL_MS)
            val elapsed = SupportSessionState.elapsedSeconds()
            if (elapsed >= MAX_SESSION_SECONDS) {
                Log.i(TAG, "Досягнуто максимальної тривалості сеансу — зупиняємось")
                stopSession("max_duration_reached")
                return
            }
        }
    }

    private suspend fun captureLoop(targetWidth: Int, targetHeight: Int) {
        var width = targetWidth
        var height = targetHeight

        while (scope.isActive) {
            val frameDelay = 1000L / maxFps.coerceAtLeast(1)
            val reader = imageReader

            if (reader != null) {
                val bytes = runCatching { grabFrame(reader, width, height) }.getOrNull()
                if (bytes != null) socket?.sendFrame(bytes)
            }

            // Масштаб міг змінитися підказкою сервера — перераховуємо.
            val metrics = displayMetrics()
            width = metrics.widthPixels * scalePercent / 100
            height = metrics.heightPixels * scalePercent / 100

            delay(frameDelay)
        }
    }

    private fun grabFrame(reader: ImageReader, width: Int, height: Int): ByteArray? {
        val image = reader.acquireLatestImage() ?: return null
        return try {
            val plane = image.planes[0]
            val rowPadding = plane.rowStride - plane.pixelStride * image.width
            val bitmap = Bitmap.createBitmap(
                image.width + rowPadding / plane.pixelStride,
                image.height,
                Bitmap.Config.ARGB_8888,
            )
            bitmap.copyPixelsFromBuffer(plane.buffer)

            val cropped = Bitmap.createBitmap(bitmap, 0, 0, image.width, image.height)
            val scaled = if (width > 0 && height > 0 && width < image.width) {
                Bitmap.createScaledBitmap(cropped, width, height, true)
            } else {
                cropped
            }

            ByteArrayOutputStream().use { output ->
                scaled.compress(Bitmap.CompressFormat.JPEG, jpegQuality, output)
                output.toByteArray()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Кадр не захоплено", e)
            null
        } finally {
            image.close()
        }
    }

    /**
     * Роздільність екрана для сервера.
     *
     * Кадри масштабуються під канал, тож панель не може вивести координати
     * з розміру картинки — вона має знати справжні пікселі пристрою, інакше
     * дотик оператора потрапив би не туди. Надсилається при кожному
     * відкритті каналу, зокрема після перепідключення.
     */
    private fun metricsPayload(metrics: DisplayMetrics): String = buildString {
        append("{\"type\":\"metrics\",")
        append("\"width\":").append(metrics.widthPixels).append(',')
        append("\"height\":").append(metrics.heightPixels).append(',')
        append("\"density_dpi\":").append(metrics.densityDpi)
        append('}')
    }

    private fun displayMetrics(): DisplayMetrics {
        val metrics = DisplayMetrics()
        val window = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        @Suppress("DEPRECATION")
        window.defaultDisplay.getRealMetrics(metrics)
        return metrics
    }

    // ---------------------------------------------------------- зупинка ---

    private fun stopSession(reason: String) {
        val session = request
        val app = application as FleetAgentApplication

        scope.launch {
            runCatching {
                val serverUrl = app.agentStore.serverUrl ?: return@runCatching
                val bearer = "Bearer " + app.enrollmentRepository.accessToken()
                if (session != null) {
                    ApiClient.create(serverUrl).endSupportSession(
                        bearer,
                        session.sessionId,
                        ua.fleet.agent.net.dto.SessionEndDto(reason),
                    )
                }
            }.onFailure { Log.w(TAG, "Не вдалося повідомити сервер про зупинку", it) }
            stopSelf()
        }
    }

    override fun onDestroy() {
        SupportSessionState.markStreaming(false)
        SupportSessionState.clear()
        captureJob?.cancel()
        watchdogJob?.cancel()
        socket?.close()
        virtualDisplay?.release()
        imageReader?.close()
        projection?.stop()
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // -------------------------------------------------------- сповіщення --

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.support_channel_name),
            NotificationManager.IMPORTANCE_HIGH,
        ).apply { description = getString(R.string.support_channel_description) }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(session: SupportSessionRequest): Notification {
        val stopIntent = Intent(this, ScreenShareService::class.java).setAction(ACTION_STOP)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0
        val stopPending = PendingIntent.getService(this, 1, stopIntent, flags)

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle(getString(R.string.support_notification_title))
            .setContentText(getString(R.string.support_notification_text, session.requestedBy))
            .setSmallIcon(android.R.drawable.presence_video_online)
            .setOngoing(true)
            .addAction(
                Notification.Action.Builder(
                    null,
                    getString(R.string.support_stop),
                    stopPending,
                ).build()
            )
            .setExtras(Bundle())
            .build()
    }

    companion object {
        private const val TAG = "FleetScreenShare"
        private const val CHANNEL_ID = "fleet_support_session"
        private const val NOTIFICATION_ID = 4243
        private const val WATCHDOG_INTERVAL_MS = 15_000L
        /** Стеля тривалості на боці пристрою (сервер має власну, коротшу). */
        private const val MAX_SESSION_SECONDS = 4 * 60 * 60L
        const val ACTION_STOP = "ua.fleet.agent.SUPPORT_STOP"
        const val EXTRA_REQUEST = "session_request"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"

        fun start(
            context: Context,
            request: SupportSessionRequest,
            resultCode: Int,
            data: Intent,
        ) {
            val payload = ApiClient.json.encodeToString(
                SupportSessionRequest.serializer(), request
            )
            val intent = Intent(context, ScreenShareService::class.java)
                .putExtra(EXTRA_REQUEST, payload)
                .putExtra(EXTRA_RESULT_CODE, resultCode)
                .putExtra(EXTRA_RESULT_DATA, data)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, ScreenShareService::class.java).setAction(ACTION_STOP)
            )
        }
    }
}
