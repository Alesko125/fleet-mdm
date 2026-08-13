package ua.fleet.agent.comms

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.database.Cursor
import android.net.Uri
import android.provider.CallLog
import android.provider.Telephony
import android.util.Log
import androidx.core.content.ContextCompat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import ua.fleet.agent.net.dto.CallItemDto
import ua.fleet.agent.net.dto.SmsItemDto

/**
 * Читання SMS та журналу дзвінків **із системних content provider'ів**.
 *
 * Це та сама інформація, яку бачать штатні застосунки «Повідомлення» й
 * «Телефон»: агент не перехоплює трафік, не читає чужі застосунки, не
 * слухає сповіщення й не робить нічого прихованого. Джерела — лише
 * [Telephony.Sms] і [CallLog.Calls], доступні за рантайм-дозволами
 * READ_SMS / READ_CALL_LOG.
 *
 * Клас нічого не вирішує сам: він читає **тільки** те, що дозволив сервер
 * (модуль увімкнено) і що дозволила система (дозвіл наданий). Якщо дозволу
 * немає — повертає порожньо й повідомляє стан `denied`/`not_requested`, а
 * не намагається обійти обмеження.
 */
class CommsCollector(context: Context) {

    private val appContext = context.applicationContext

    enum class PermState(val wire: String) {
        GRANTED("granted"),
        DENIED("denied"),
        NOT_REQUESTED("not_requested"),
        UNAVAILABLE("unavailable"),
    }

    /** Чи є на пристрої телефонія — без неї SMS/дзвінки недоступні в принципі. */
    private fun hasTelephony(): Boolean =
        appContext.packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY)

    fun smsPermission(): PermState = permissionFor(Manifest.permission.READ_SMS)

    fun callLogPermission(): PermState = permissionFor(Manifest.permission.READ_CALL_LOG)

    private fun permissionFor(permission: String): PermState {
        if (!hasTelephony()) return PermState.UNAVAILABLE
        val granted = ContextCompat.checkSelfPermission(appContext, permission) ==
            PackageManager.PERMISSION_GRANTED
        return if (granted) PermState.GRANTED else PermState.DENIED
    }

    // --------------------------------------------------------------- SMS ---

    /**
     * Читає останні SMS.
     *
     * @param metadataOnly якщо true — текст не читається взагалі (лише номер,
     *        напрямок і час). Це поважає режим «тільки метадані» з сервера.
     */
    suspend fun collectSms(sinceMillis: Long, limit: Int, metadataOnly: Boolean): List<SmsItemDto> =
        withContext(Dispatchers.IO) {
            if (smsPermission() != PermState.GRANTED) return@withContext emptyList()

            val projection = mutableListOf(
                Telephony.Sms._ID,
                Telephony.Sms.ADDRESS,
                Telephony.Sms.DATE,
                Telephony.Sms.TYPE,
            )
            if (!metadataOnly) projection.add(Telephony.Sms.BODY)

            query(
                Telephony.Sms.CONTENT_URI,
                projection.toTypedArray(),
                "${Telephony.Sms.DATE} >= ?",
                arrayOf(sinceMillis.toString()),
                "${Telephony.Sms.DATE} DESC",
                limit,
            ) { cursor ->
                val date = cursor.longOrNull(Telephony.Sms.DATE)
                SmsItemDto(
                    providerId = cursor.stringOrNull(Telephony.Sms._ID),
                    direction = smsDirection(cursor.intOrNull(Telephony.Sms.TYPE)),
                    address = cursor.stringOrNull(Telephony.Sms.ADDRESS),
                    body = if (metadataOnly) null else cursor.stringOrNull(Telephony.Sms.BODY),
                    sentAt = date?.let(::iso8601),
                )
            }
        }

    // ----------------------------------------------------- журнал дзвінків --

    suspend fun collectCalls(sinceMillis: Long, limit: Int): List<CallItemDto> =
        withContext(Dispatchers.IO) {
            if (callLogPermission() != PermState.GRANTED) return@withContext emptyList()

            query(
                CallLog.Calls.CONTENT_URI,
                arrayOf(
                    CallLog.Calls._ID,
                    CallLog.Calls.NUMBER,
                    CallLog.Calls.CACHED_NAME,
                    CallLog.Calls.DATE,
                    CallLog.Calls.DURATION,
                    CallLog.Calls.TYPE,
                ),
                "${CallLog.Calls.DATE} >= ?",
                arrayOf(sinceMillis.toString()),
                "${CallLog.Calls.DATE} DESC",
                limit,
            ) { cursor ->
                CallItemDto(
                    providerId = cursor.stringOrNull(CallLog.Calls._ID),
                    direction = callDirection(cursor.intOrNull(CallLog.Calls.TYPE)),
                    number = cursor.stringOrNull(CallLog.Calls.NUMBER),
                    contact = cursor.stringOrNull(CallLog.Calls.CACHED_NAME),
                    durationSeconds = cursor.intOrNull(CallLog.Calls.DURATION),
                    occurredAt = cursor.longOrNull(CallLog.Calls.DATE)?.let(::iso8601),
                )
            }
        }

    // ------------------------------------------------------------ службове -

    private inline fun <T> query(
        uri: Uri,
        projection: Array<String>,
        selection: String,
        selectionArgs: Array<String>,
        sortOrder: String,
        limit: Int,
        map: (Cursor) -> T,
    ): List<T> {
        val result = ArrayList<T>()
        try {
            appContext.contentResolver.query(uri, projection, selection, selectionArgs, sortOrder)
                ?.use { cursor ->
                    while (cursor.moveToNext() && result.size < limit) {
                        runCatching { map(cursor) }.getOrNull()?.let(result::add)
                    }
                }
        } catch (e: SecurityException) {
            // Дозвіл відкликали між перевіркою й читанням — просто нічого не
            // повертаємо. Обходити відмову ми не намагаємось.
            Log.w(TAG, "Немає доступу до $uri", e)
        } catch (e: Exception) {
            Log.w(TAG, "Помилка читання $uri", e)
        }
        return result
    }

    private fun Cursor.stringOrNull(column: String): String? {
        val idx = getColumnIndex(column)
        return if (idx >= 0 && !isNull(idx)) getString(idx) else null
    }

    private fun Cursor.longOrNull(column: String): Long? {
        val idx = getColumnIndex(column)
        return if (idx >= 0 && !isNull(idx)) getLong(idx) else null
    }

    private fun Cursor.intOrNull(column: String): Int? {
        val idx = getColumnIndex(column)
        return if (idx >= 0 && !isNull(idx)) getInt(idx) else null
    }

    private fun smsDirection(type: Int?): String = when (type) {
        Telephony.Sms.MESSAGE_TYPE_INBOX -> "inbox"
        Telephony.Sms.MESSAGE_TYPE_SENT -> "sent"
        Telephony.Sms.MESSAGE_TYPE_DRAFT -> "draft"
        Telephony.Sms.MESSAGE_TYPE_OUTBOX -> "outbox"
        else -> "other"
    }

    private fun callDirection(type: Int?): String = when (type) {
        CallLog.Calls.INCOMING_TYPE -> "incoming"
        CallLog.Calls.OUTGOING_TYPE -> "outgoing"
        CallLog.Calls.MISSED_TYPE -> "missed"
        else -> "other"
    }

    private fun iso8601(epochMillis: Long): String {
        val df = java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
        df.timeZone = TimeZone.getTimeZone("UTC")
        return df.format(Date(epochMillis))
    }

    private companion object {
        const val TAG = "FleetCommsCollector"
    }
}
