package ua.fleet.agent.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.res.stringResource
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import ua.fleet.agent.R

/**
 * Сканер QR-коду enrollment-токена.
 *
 * Камера використовується лише в момент сканування, за явною дією користувача;
 * жодного фонового доступу до камери агент не має.
 */
@Composable
fun rememberQrScanner(onResult: (String) -> Unit): () -> Unit {
    val prompt = stringResource(R.string.enroll_scan_prompt)
    val launcher = rememberLauncherForActivityResult(ScanContract()) { result ->
        result.contents?.takeIf { it.isNotBlank() }?.let(onResult)
    }
    return remember(launcher, prompt) {
        {
            launcher.launch(
                ScanOptions()
                    .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                    .setPrompt(prompt)
                    .setBeepEnabled(false)
                    .setOrientationLocked(false)
            )
        }
    }
}
