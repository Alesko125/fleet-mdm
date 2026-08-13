package ua.fleet.agent.net

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.HttpException
import retrofit2.Retrofit
import ua.fleet.agent.BuildConfig
import ua.fleet.agent.net.dto.ApiErrorBody

/** Створює клієнт Retrofit для конкретного сервера. */
object ApiClient {

    val json: Json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
        explicitNulls = false
    }

    private val logging = HttpLoggingInterceptor().apply {
        // Тіла запитів містять токени — у релізі не логуємо нічого зайвого.
        level = if (BuildConfig.DEBUG) {
            HttpLoggingInterceptor.Level.BASIC
        } else {
            HttpLoggingInterceptor.Level.NONE
        }
    }

    private val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .addInterceptor { chain ->
            val request = chain.request().newBuilder()
                .header("User-Agent", "FleetAgent/${BuildConfig.AGENT_VERSION} (Android)")
                .build()
            chain.proceed(request)
        }
        .addInterceptor(logging)
        .build()

    /** @param serverUrl базова адреса сервера, напр. `https://mdm.example.com` */
    fun create(serverUrl: String): FleetApi {
        val base = normalizeBaseUrl(serverUrl)
        return Retrofit.Builder()
            .baseUrl(base)
            .client(httpClient)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(FleetApi::class.java)
    }

    /**
     * Клієнт для запитів за абсолютним URL (`@Url`), напр. завантаження APK
     * за посиланням із payload команди. Базова адреса тут — заглушка.
     */
    fun createRaw(): FleetApi =
        Retrofit.Builder()
            .baseUrl("http://localhost/")
            .client(httpClient)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(FleetApi::class.java)

    /**
     * `https://host` → `https://host/api/v1/` (Retrofit вимагає слеш у кінці).
     *
     * У релізній збірці приймається лише `https://`: агент носить токен
     * пристрою, і віддавати його відкритим каналом не можна. Виняток для
     * `http://` лишається тільки в debug-збірці — і навіть там мережева
     * політика Android пропускає його виключно на loopback/емулятор
     * (`res/xml/network_security_config.xml`).
     */
    fun normalizeBaseUrl(serverUrl: String): String {
        var url = serverUrl.trim().trimEnd('/')
        require(url.startsWith("http://") || url.startsWith("https://")) {
            "Адреса має починатися з http:// або https://"
        }
        require(url.startsWith("https://") || BuildConfig.DEBUG) {
            "Дозволено лише https:// — незашифроване з'єднання з сервером керування неприпустиме"
        }
        if (!url.endsWith("/api/v1")) {
            url += "/api/v1"
        }
        return "$url/"
    }

    /** Дістає людяне повідомлення з відповіді бекенду. */
    fun describeError(throwable: Throwable): String = when (throwable) {
        is HttpException -> parseErrorBody(throwable) ?: "Помилка сервера (${throwable.code()})"
        is java.net.UnknownHostException -> "Сервер недоступний: перевірте адресу й мережу"
        is javax.net.ssl.SSLException -> "Помилка TLS: сертифікат сервера не довірений"
        is java.io.IOException -> "Немає зʼєднання з сервером"
        else -> throwable.message ?: "Невідома помилка"
    }

    private fun parseErrorBody(exception: HttpException): String? = try {
        exception.response()?.errorBody()?.string()?.let { raw ->
            json.decodeFromString(ApiErrorBody.serializer(), raw).error.message
        }
    } catch (_: Exception) {
        null
    }
}
