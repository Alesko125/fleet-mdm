package ua.fleet.agent.support

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import ua.fleet.agent.FleetAgentApplication
import ua.fleet.agent.R
import ua.fleet.agent.net.ApiClient
import ua.fleet.agent.net.dto.SessionConsentDto

/**
 * Екран згоди на показ екрана.
 *
 * Показується поверх усього, називає адміністратора й причину. Далі, якщо
 * користувач погодився, спрацьовує **системний** діалог `MediaProjection` —
 * агент його не обходить і не може отримати доступ до екрана без нього.
 */
class SupportConsentActivity : ComponentActivity() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private lateinit var request: SupportSessionRequest

    private val projectionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            sendConsent(granted = true) {
                ScreenShareService.start(this, request, result.resultCode, result.data!!)
                finish()
            }
        } else {
            // Користувач скасував системний діалог — це теж відмова.
            sendConsent(granted = false, reason = "Скасовано в системному діалозі") { finish() }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val raw = intent.getStringExtra(EXTRA_REQUEST)
        val parsed = raw?.let {
            runCatching {
                ApiClient.json.decodeFromString(SupportSessionRequest.serializer(), it)
            }.getOrNull()
        }
        if (parsed == null) {
            finish()
            return
        }
        request = parsed
        SupportSessionState.begin(parsed)

        setContent {
            MaterialTheme {
                ConsentScreen(
                    request = parsed,
                    onAllow = {
                        val manager =
                            getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
                        projectionLauncher.launch(manager.createScreenCaptureIntent())
                    },
                    onDeny = {
                        sendConsent(granted = false, reason = "Користувач відхилив запит") {
                            SupportSessionState.clear()
                            finish()
                        }
                    },
                )
            }
        }
    }

    private fun sendConsent(
        granted: Boolean,
        reason: String? = null,
        onDone: () -> Unit,
    ) {
        val app = application as FleetAgentApplication
        val repository = app.enrollmentRepository
        val serverUrl = repository.state.serverUrl

        scope.launch {
            runCatching {
                if (serverUrl == null) return@runCatching
                ApiClient.create(serverUrl).supportConsent(
                    "Bearer " + repository.accessToken(),
                    request.sessionId,
                    SessionConsentDto(
                        granted = granted,
                        inputAvailable = granted && RemoteInputService.isEnabled(this@SupportConsentActivity),
                        reason = reason,
                    ),
                )
            }.onFailure { Log.w(TAG, "Не вдалося надіслати відповідь про згоду", it) }
            onDone()
        }
    }

    companion object {
        private const val TAG = "FleetSupportConsent"
        const val EXTRA_REQUEST = "session_request"

        fun launch(context: Context, request: SupportSessionRequest) {
            val payload = ApiClient.json.encodeToString(
                SupportSessionRequest.serializer(), request
            )
            val intent = Intent(context, SupportConsentActivity::class.java)
                .putExtra(EXTRA_REQUEST, payload)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            context.startActivity(intent)
        }
    }
}

@Composable
private fun ConsentScreen(
    request: SupportSessionRequest,
    onAllow: () -> Unit,
    onDeny: () -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
    ) {
        Text(
            stringResource(R.string.support_consent_title),
            style = MaterialTheme.typography.headlineSmall,
        )
        Text(
            stringResource(R.string.support_consent_admin, request.requestedBy),
            style = MaterialTheme.typography.bodyLarge,
        )
        Text(
            stringResource(R.string.support_consent_reason, request.reason),
            style = MaterialTheme.typography.bodyMedium,
        )
        if (request.inputRequested) {
            Text(
                stringResource(R.string.support_consent_input),
                style = MaterialTheme.typography.bodyMedium,
            )
        }
        Text(
            stringResource(R.string.support_consent_note),
            style = MaterialTheme.typography.bodySmall,
        )

        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(onClick = onAllow) { Text(stringResource(R.string.support_allow)) }
            OutlinedButton(onClick = onDeny) { Text(stringResource(R.string.support_deny)) }
        }
    }
}
