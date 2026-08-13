package ua.fleet.agent.crypto

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import android.util.Base64
import android.util.Log
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * Криптографічна ідентичність пристрою.
 *
 * Приватний ключ EC P-256 генерується всередині Android Keystore і **ніколи**
 * не залишає пристрій: назовні віддається лише публічна частина (SPKI/DER).
 * Автентифікація на сервері — підпис одноразового nonce (SHA256withECDSA).
 *
 * ## Ротація
 *
 * Ключі версіонуються псевдонімом `fleet_device_identity_v<покоління>`.
 * Перевипуск виглядає так:
 *
 * 1. [prepareNextGeneration] створює нову пару під наступним псевдонімом —
 *    стара при цьому лишається робочою, тож обрив мережі нічого не ламає;
 * 2. новий публічний ключ підписується **старим** ключем ([sign]) — це і є
 *    доказ для сервера, що ротацію просить той самий пристрій;
 * 3. після підтвердження сервером [commitGeneration] робить нову пару
 *    поточною й видаляє стару;
 * 4. якщо сервер відмовив — [discardGeneration] прибирає заготовку.
 *
 * Приватної частини не бачить ніхто, включно з сервером, на жодному кроці.
 */
object DeviceKeyStore {

    private const val TAG = "FleetKeyStore"
    private const val PROVIDER = "AndroidKeyStore"
    private const val ALIAS_PREFIX = "fleet_device_identity_v"
    const val SIGNATURE_ALGORITHM = "SHA256withECDSA"

    /** Скільки поколінь тому може лишатися ключ (страховка від обриву). */
    private const val CLEANUP_DEPTH = 5

    private val keyStore: KeyStore by lazy {
        KeyStore.getInstance(PROVIDER).apply { load(null) }
    }

    @Volatile
    private var generation: Int = 1

    private fun alias(forGeneration: Int = generation) = "$ALIAS_PREFIX$forGeneration"

    /** Прив'язує сховище до поточного покоління ключа (виклик при старті). */
    fun bindGeneration(current: Int) {
        generation = current.coerceAtLeast(1)
    }

    fun currentGeneration(): Int = generation

    fun hasKey(): Boolean = keyStore.containsAlias(alias())

    /** Створює ключ поточного покоління, якщо його ще немає. */
    fun ensureKeyPair(): String {
        if (!hasKey()) {
            generate(alias(), preferStrongBox = true)
        }
        return publicKeyBase64()
    }

    fun publicKeyBase64(forGeneration: Int = generation): String {
        val certificate = keyStore.getCertificate(alias(forGeneration))
            ?: error("Ключ пристрою відсутній — спершу викличте ensureKeyPair()")
        val spki = certificate.publicKey.encoded // X.509 SubjectPublicKeyInfo (DER)
        return Base64.encodeToString(spki, Base64.NO_WRAP)
    }

    /** Підписує повідомлення ключем із Keystore; повертає DER-підпис у base64. */
    fun sign(message: ByteArray, forGeneration: Int = generation): String {
        val entry = keyStore.getEntry(alias(forGeneration), null) as? KeyStore.PrivateKeyEntry
            ?: error("Ключ пристрою відсутній")
        val privateKey: PrivateKey = entry.privateKey
        val signature = Signature.getInstance(SIGNATURE_ALGORITHM).apply {
            initSign(privateKey)
            update(message)
        }.sign()
        return Base64.encodeToString(signature, Base64.NO_WRAP)
    }

    // ------------------------------------------------------------ ротація --

    /**
     * Готує пару наступного покоління, не чіпаючи поточну.
     *
     * @return публічний ключ заготовки у base64 та її покоління
     */
    fun prepareNextGeneration(): Pair<String, Int> {
        val next = generation + 1
        // Залишок невдалої попередньої спроби прибираємо, щоб не підписати
        // ключ, від якого вже немає приватної частини в узгодженому стані.
        if (keyStore.containsAlias(alias(next))) {
            keyStore.deleteEntry(alias(next))
        }
        generate(alias(next), preferStrongBox = true)
        return publicKeyBase64(next) to next
    }

    /** Робить підготовлену пару поточною й прибирає попередні покоління. */
    fun commitGeneration(next: Int) {
        require(keyStore.containsAlias(alias(next))) { "Ключ покоління $next відсутній" }
        val previous = generation
        generation = next
        if (previous != next) {
            runCatching { keyStore.deleteEntry(alias(previous)) }
                .onFailure { Log.w(TAG, "Не вдалося видалити старий ключ", it) }
        }
        cleanupStaleGenerations()
    }

    /** Прибирає заготовку, якщо сервер ротацію не прийняв. */
    fun discardGeneration(candidate: Int) {
        if (candidate == generation) return
        runCatching { keyStore.deleteEntry(alias(candidate)) }
            .onFailure { Log.w(TAG, "Не вдалося прибрати заготовку ключа", it) }
    }

    private fun cleanupStaleGenerations() {
        val lowest = (generation - CLEANUP_DEPTH).coerceAtLeast(1)
        for (old in lowest until generation) {
            if (keyStore.containsAlias(alias(old))) {
                runCatching { keyStore.deleteEntry(alias(old)) }
            }
        }
    }

    /** Чи зберігається ключ в апаратному сховищі (StrongBox / TEE). */
    fun isHardwareBacked(): Boolean = try {
        val entry = keyStore.getEntry(alias(), null) as? KeyStore.PrivateKeyEntry
        if (entry == null) {
            false
        } else {
            val factory = java.security.KeyFactory.getInstance(entry.privateKey.algorithm, PROVIDER)
            val info = factory.getKeySpec(
                entry.privateKey,
                android.security.keystore.KeyInfo::class.java,
            )
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                info.securityLevel != android.security.keystore.KeyProperties.SECURITY_LEVEL_SOFTWARE
            } else {
                @Suppress("DEPRECATION")
                info.isInsideSecureHardware
            }
        }
    } catch (e: Exception) {
        Log.w(TAG, "Не вдалося визначити тип сховища ключа", e)
        false
    }

    /** Видаляє всі ключі пристрою — при знятті з обліку. */
    fun deleteKey() {
        for (candidate in 1..(generation + 1)) {
            if (keyStore.containsAlias(alias(candidate))) {
                runCatching { keyStore.deleteEntry(alias(candidate)) }
            }
        }
        generation = 1
    }

    private fun generate(withAlias: String, preferStrongBox: Boolean) {
        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, PROVIDER)
        try {
            generator.initialize(buildSpec(withAlias, useStrongBox = preferStrongBox))
            generator.generateKeyPair()
        } catch (e: StrongBoxUnavailableException) {
            Log.i(TAG, "StrongBox недоступний, використовуємо TEE", e)
            generator.initialize(buildSpec(withAlias, useStrongBox = false))
            generator.generateKeyPair()
        }
    }

    private fun buildSpec(withAlias: String, useStrongBox: Boolean): KeyGenParameterSpec {
        val builder = KeyGenParameterSpec.Builder(withAlias, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)
            // Ключ доступний без розблокування — агент має відповідати серверу
            // у фоні; сам ключ при цьому лишається невивантажуваним.
            .setUserAuthenticationRequired(false)

        if (useStrongBox && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            builder.setIsStrongBoxBacked(true)
        }
        return builder.build()
    }
}
