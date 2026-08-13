package ua.fleet.agent.ui

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import java.text.DateFormat
import java.util.Date
import ua.fleet.agent.R
import ua.fleet.agent.location.LocationPermissions

class MainActivity : ComponentActivity() {

    private val viewModel: MainViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                AgentApp(viewModel)
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AgentApp(viewModel: MainViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()

    Scaffold(
        topBar = { TopAppBar(title = { Text(stringResource(R.string.app_name)) }) }
    ) { padding ->
        when {
            state.loading -> LoadingBox(padding)
            state.enrolled -> StatusScreen(state, viewModel, padding)
            else -> EnrollmentScreen(state, viewModel, padding)
        }
    }
}

@Composable
private fun LoadingBox(padding: PaddingValues) {
    Column(
        modifier = Modifier.fillMaxSize().padding(padding),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) { CircularProgressIndicator() }
}

// ---------------------------------------------------------------- реєстрація

@Composable
private fun EnrollmentScreen(
    state: AgentUiState,
    viewModel: MainViewModel,
    padding: PaddingValues,
) {
    val scanLauncher = rememberQrScanner { viewModel.onQrScanned(it) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(padding)
            .padding(20.dp)
            .verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text(
            stringResource(R.string.enroll_title),
            style = MaterialTheme.typography.headlineSmall,
        )
        Text(stringResource(R.string.enroll_intro), style = MaterialTheme.typography.bodyMedium)

        TransparencyCard()

        OutlinedTextField(
            value = state.serverInput,
            onValueChange = viewModel::onServerChanged,
            label = { Text(stringResource(R.string.enroll_server_label)) },
            singleLine = true,
            enabled = !state.busy,
            modifier = Modifier.fillMaxWidth(),
        )

        OutlinedTextField(
            value = state.tokenInput,
            onValueChange = viewModel::onTokenChanged,
            label = { Text(stringResource(R.string.enroll_token_label)) },
            placeholder = { Text(stringResource(R.string.enroll_token_hint)) },
            singleLine = true,
            enabled = !state.busy,
            modifier = Modifier.fillMaxWidth(),
        )

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedButton(
                onClick = { scanLauncher() },
                enabled = !state.busy,
            ) { Text(stringResource(R.string.enroll_scan)) }

            Button(
                onClick = viewModel::enroll,
                enabled = !state.busy && state.tokenInput.isNotBlank(),
            ) { Text(stringResource(R.string.enroll_submit)) }
        }

        if (state.busy) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                CircularProgressIndicator(modifier = Modifier.height(20.dp))
                Text(stringResource(R.string.enroll_in_progress))
            }
        }

        state.error?.let { MessageCard(it, isError = true) }
        state.info?.let { MessageCard(it, isError = false) }
    }
}

// -------------------------------------------------------------------- статус

@Composable
private fun StatusScreen(
    state: AgentUiState,
    viewModel: MainViewModel,
    padding: PaddingValues,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(padding)
            .padding(20.dp)
            .verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text(stringResource(R.string.status_title), style = MaterialTheme.typography.headlineSmall)

        TransparencyCard(state.transparencyNotice)

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                InfoRow(stringResource(R.string.status_device), state.deviceName ?: "—")
                InfoRow(stringResource(R.string.status_device_id), state.deviceId ?: "—")
                InfoRow(stringResource(R.string.status_server), state.serverUrl ?: "—")
                InfoRow(stringResource(R.string.status_mode), describeMode(state.managementMode))
                InfoRow(
                    stringResource(R.string.status_last_check),
                    formatTimestamp(state.lastContactAtMillis),
                )
                InfoRow(
                    "Ключ пристрою",
                    if (state.hardwareBackedKey) "апаратне сховище (TEE/StrongBox)" else "Keystore",
                )
            }
        }

        SupportCard(state, viewModel)

        CommsCard(state, viewModel)

        LocationCard(state, viewModel)

        AppManagementCard(state, viewModel)

        CollectedDataCard()

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Button(onClick = viewModel::checkConnection, enabled = !state.busy) {
                Text(stringResource(R.string.status_check_now))
            }
            TextButton(onClick = viewModel::unenroll, enabled = !state.busy) {
                Text("Зняти з обліку")
            }
        }

        if (state.busy) CircularProgressIndicator(modifier = Modifier.height(24.dp))
        state.error?.let { MessageCard(it, isError = true) }
        state.info?.let { MessageCard(it, isError = false) }
    }
}

// ------------------------------------------------------------- компоненти ---

@Composable
private fun TransparencyCard(serverNotice: String? = null) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.secondaryContainer,
        ),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                stringResource(R.string.transparency_title),
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            Text(
                serverNotice ?: stringResource(R.string.transparency_body),
                style = MaterialTheme.typography.bodyMedium,
            )
        }
    }
}

/**
 * Стан віддаленої підтримки на екрані агента.
 *
 * Картка показується завжди — і коли сеансу немає (щоб було видно, що
 * ніхто не дивиться), і коли він триває. Під час сеансу тут є кнопка
 * зупинки: користувач пристрою обриває показ екрана сам, без згоди
 * адміністратора.
 */
@Composable
private fun SupportCard(state: AgentUiState, viewModel: MainViewModel) {
    val support = state.support

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                stringResource(R.string.support_card_title),
                style = MaterialTheme.typography.titleMedium,
            )

            if (!support.active) {
                Text(
                    stringResource(R.string.support_card_idle),
                    style = MaterialTheme.typography.bodySmall,
                )
                return@Column
            }

            InfoRow(
                stringResource(R.string.support_card_state),
                when {
                    support.streaming -> stringResource(R.string.support_card_state_streaming)
                    support.connected -> stringResource(R.string.support_card_state_connected)
                    else -> stringResource(R.string.support_card_state_connecting)
                },
            )
            InfoRow(
                stringResource(R.string.support_card_admin),
                support.requestedBy ?: "—",
            )
            support.reason?.takeIf { it.isNotBlank() }?.let {
                InfoRow(stringResource(R.string.support_card_reason), it)
            }
            InfoRow(
                stringResource(R.string.support_card_duration),
                formatElapsed(support.elapsedSeconds),
            )
            if (support.reconnects > 0) {
                InfoRow(
                    stringResource(R.string.support_card_reconnects),
                    support.reconnects.toString(),
                )
            }
            InfoRow(
                stringResource(R.string.support_card_input),
                when {
                    !support.inputRequested -> stringResource(R.string.support_card_input_no)
                    support.inputServiceEnabled ->
                        stringResource(R.string.support_card_input_yes)
                    else -> stringResource(R.string.support_card_input_unavailable)
                },
            )

            Text(
                stringResource(R.string.support_card_hint),
                style = MaterialTheme.typography.bodySmall,
            )

            Button(onClick = viewModel::stopSupportSession) {
                Text(stringResource(R.string.support_stop))
            }
        }
    }
}

private fun formatElapsed(seconds: Long): String {
    val minutes = seconds / 60
    val rest = seconds % 60
    return if (minutes > 0) "$minutes хв ${rest.toString().padStart(2, '0')} с" else "$rest с"
}

/**
 * Стан комунікаційного модуля (SMS / журнал дзвінків) на екрані агента.
 *
 * Модуль опційний і вимкнений за замовчуванням. Картка показується лише
 * коли адміністратор його ввімкнув — і тоді власник пристрою чітко бачить,
 * що саме збирається. Прихованого збору немає.
 */
@Composable
private fun CommsCard(state: AgentUiState, viewModel: MainViewModel) {
    val comms = state.comms
    if (!comms.enabled) return

    val context = LocalContext.current
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { viewModel.refreshComms() }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.tertiaryContainer,
        ),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                stringResource(R.string.comms_card_title),
                style = MaterialTheme.typography.titleMedium,
            )
            Text(
                comms.notice.ifBlank { stringResource(R.string.comms_card_default_notice) },
                style = MaterialTheme.typography.bodySmall,
            )

            InfoRow(
                stringResource(R.string.comms_card_sms),
                describeComms(comms.collectSms, comms.smsPermission),
            )
            InfoRow(
                stringResource(R.string.comms_card_calls),
                describeComms(comms.collectCallLog, comms.callLogPermission),
            )
            if (comms.metadataOnly) {
                Text(
                    stringResource(R.string.comms_card_metadata_only),
                    style = MaterialTheme.typography.bodySmall,
                )
            }

            if (comms.needsPermissions) {
                Text(
                    stringResource(R.string.comms_card_permission_needed),
                    style = MaterialTheme.typography.bodySmall,
                )
                Button(onClick = {
                    val perms = viewModel.commsPermissionsToRequest()
                    if (perms.isNotEmpty()) permissionLauncher.launch(perms)
                }) {
                    Text(stringResource(R.string.comms_card_grant))
                }
            }
        }
    }
}

private fun describeComms(collect: Boolean, permission: String): String {
    if (!collect) return "не збирається"
    return when (permission) {
        "granted" -> "збирається (дозвіл надано)"
        "denied" -> "потрібен ваш дозвіл"
        "unavailable" -> "недоступно на цьому пристрої"
        else -> "очікує на дозвіл"
    }
}

@Composable
private fun LocationCard(state: AgentUiState, viewModel: MainViewModel) {
    val location = state.location ?: return
    val context = LocalContext.current

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { viewModel.onLocationPermissionResult() }

    val backgroundLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { viewModel.onLocationPermissionResult() }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = if (location.enabled) {
                MaterialTheme.colorScheme.tertiaryContainer
            } else {
                MaterialTheme.colorScheme.surfaceVariant
            },
        ),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(stringResource(R.string.location_title), style = MaterialTheme.typography.titleMedium)
            Text(
                stringResource(
                    if (location.enabled) {
                        R.string.location_enabled_note
                    } else {
                        R.string.location_disabled_note
                    }
                ),
                style = MaterialTheme.typography.bodySmall,
            )

            InfoRow(stringResource(R.string.location_permission), describePermission(location.permissionState))
            InfoRow(
                stringResource(R.string.location_background),
                if (location.backgroundGranted) "надано" else "не надано",
            )
            InfoRow(
                stringResource(R.string.location_services),
                if (location.servicesEnabled) "увімкнені" else "вимкнені в системі",
            )

            if (location.enabled) {
                InfoRow(
                    stringResource(R.string.location_interval),
                    "${location.intervalSeconds / 60} хв",
                )
                InfoRow(
                    stringResource(R.string.location_accuracy),
                    stringResource(
                        if (location.highAccuracy) {
                            R.string.location_accuracy_high
                        } else {
                            R.string.location_accuracy_low
                        }
                    ),
                )
                InfoRow(stringResource(R.string.location_queue), "${location.queuedFixes}")
                InfoRow(
                    stringResource(R.string.location_last_fix),
                    formatTimestamp(location.lastFixAtMillis),
                )
                InfoRow(stringResource(R.string.location_battery), location.batteryNote)
            }

            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                if (location.permissionState !in setOf("granted_fine", "granted_coarse")) {
                    Button(
                        onClick = {
                            permissionLauncher.launch(LocationPermissions.FOREGROUND_PERMISSIONS)
                        },
                        enabled = !state.busy,
                    ) { Text(stringResource(R.string.location_grant)) }
                } else if (!location.backgroundGranted &&
                    Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                ) {
                    OutlinedButton(
                        onClick = {
                            backgroundLauncher.launch(LocationPermissions.BACKGROUND_PERMISSION)
                        },
                        enabled = !state.busy,
                    ) { Text(stringResource(R.string.location_grant_background)) }
                }

                OutlinedButton(onClick = viewModel::syncLocationConfig, enabled = !state.busy) {
                    Text("Оновити стан")
                }

                TextButton(
                    onClick = {
                        context.startActivity(
                            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                                data = Uri.fromParts("package", context.packageName, null)
                                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                            }
                        )
                    },
                ) { Text(stringResource(R.string.location_open_settings)) }
            }

            Text(
                stringResource(R.string.location_revoke_hint),
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

private fun describePermission(state: String): String = when (state) {
    "granted_fine" -> "точна геолокація"
    "granted_coarse" -> "приблизна геолокація"
    "denied" -> "не надано"
    "restricted" -> "заблоковано політикою"
    else -> "невідомо"
}

@Composable
private fun AppManagementCard(state: AgentUiState, viewModel: MainViewModel) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text(
                stringResource(R.string.apps_title),
                style = MaterialTheme.typography.titleMedium,
            )
            Text(
                stringResource(R.string.apps_install_note),
                style = MaterialTheme.typography.bodySmall,
            )

            SwitchRow(
                label = stringResource(R.string.apps_service_toggle),
                checked = state.serviceEnabled,
                enabled = !state.busy,
                onCheckedChange = viewModel::toggleService,
            )

            SwitchRow(
                label = stringResource(R.string.apps_root_toggle),
                checked = state.allowRootInstall,
                enabled = !state.busy,
                onCheckedChange = viewModel::toggleRootFallback,
            )
            Text(
                stringResource(R.string.apps_root_hint),
                style = MaterialTheme.typography.bodySmall,
            )

            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(onClick = viewModel::checkCommands, enabled = !state.busy) {
                    Text(stringResource(R.string.apps_check_commands))
                }
                OutlinedButton(onClick = viewModel::sendInventory, enabled = !state.busy) {
                    Text(stringResource(R.string.apps_send_inventory))
                }
            }
        }
    }
}

@Composable
private fun SwitchRow(
    label: String,
    checked: Boolean,
    enabled: Boolean,
    onCheckedChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            label,
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.weight(1f),
        )
        Switch(checked = checked, onCheckedChange = onCheckedChange, enabled = enabled)
    }
}

@Composable
private fun CollectedDataCard() {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                stringResource(R.string.collected_title),
                style = MaterialTheme.typography.titleMedium,
            )
            Bullet(stringResource(R.string.collected_inventory))
            Bullet(stringResource(R.string.collected_apps))
            Bullet(stringResource(R.string.collected_location))
            Bullet(stringResource(R.string.collected_support))

            HorizontalDivider()

            Text(
                stringResource(R.string.not_collected_title),
                style = MaterialTheme.typography.titleMedium,
            )
            Text(
                stringResource(R.string.not_collected_body),
                style = MaterialTheme.typography.bodyMedium,
            )
        }
    }
}

@Composable
private fun Bullet(text: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        Text("•")
        Text(text, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun InfoRow(label: String, value: String) {
    Column {
        Text(label, style = MaterialTheme.typography.labelMedium)
        Text(value, style = MaterialTheme.typography.bodyLarge)
        Spacer(Modifier.height(2.dp))
    }
}

@Composable
private fun MessageCard(message: String, isError: Boolean) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = if (isError) {
                MaterialTheme.colorScheme.errorContainer
            } else {
                MaterialTheme.colorScheme.primaryContainer
            },
        ),
    ) {
        Text(message, modifier = Modifier.padding(14.dp))
    }
}

private fun describeMode(mode: String): String = when (mode) {
    "device_owner" -> "Device Owner (повне керування)"
    "profile_owner" -> "Profile Owner (робочий профіль)"
    else -> "Без прав адміністратора пристрою"
}

private fun formatTimestamp(millis: Long): String =
    if (millis <= 0L) {
        "ще не було"
    } else {
        DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT).format(Date(millis))
    }
