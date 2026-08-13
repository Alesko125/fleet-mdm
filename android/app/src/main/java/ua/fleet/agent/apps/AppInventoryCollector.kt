package ua.fleet.agent.apps

import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import ua.fleet.agent.net.dto.AppInventoryItemDto

/**
 * Перелік встановлених застосунків через `PackageManager`.
 *
 * Збираються лише метадані пакета (імʼя, версія, системний/користувацький,
 * хто встановив). Вміст застосунків, їхні дані чи трафік не читаються.
 */
class AppInventoryCollector(private val context: Context) {

    fun collect(includeSystem: Boolean = true): List<AppInventoryItemDto> {
        val pm = context.packageManager
        val packages = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                pm.getInstalledPackages(PackageManager.PackageInfoFlags.of(0L))
            } else {
                @Suppress("DEPRECATION")
                pm.getInstalledPackages(0)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Не вдалося отримати перелік застосунків", e)
            emptyList()
        }

        return packages.mapNotNull { info ->
            val appInfo = info.applicationInfo ?: return@mapNotNull null
            val isSystem = (appInfo.flags and ApplicationInfo.FLAG_SYSTEM) != 0
            if (isSystem && !includeSystem) return@mapNotNull null

            AppInventoryItemDto(
                packageName = info.packageName,
                appName = runCatching { pm.getApplicationLabel(appInfo).toString() }.getOrNull(),
                versionName = info.versionName,
                versionCode = versionCodeOf(info),
                isSystem = isSystem,
                isEnabled = appInfo.enabled,
                installerPackage = installerOf(info.packageName),
            )
        }.sortedBy { it.packageName }
    }

    fun installedVersion(packageName: String): AppInventoryItemDto? = try {
        val pm = context.packageManager
        val info = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            pm.getPackageInfo(packageName, PackageManager.PackageInfoFlags.of(0L))
        } else {
            @Suppress("DEPRECATION")
            pm.getPackageInfo(packageName, 0)
        }
        AppInventoryItemDto(
            packageName = info.packageName,
            appName = info.applicationInfo?.let {
                runCatching { pm.getApplicationLabel(it).toString() }.getOrNull()
            },
            versionName = info.versionName,
            versionCode = versionCodeOf(info),
            isSystem = info.applicationInfo?.let {
                (it.flags and ApplicationInfo.FLAG_SYSTEM) != 0
            } ?: false,
            isEnabled = info.applicationInfo?.enabled ?: true,
            installerPackage = installerOf(packageName),
        )
    } catch (_: PackageManager.NameNotFoundException) {
        null
    }

    fun isInstalled(packageName: String): Boolean = installedVersion(packageName) != null

    private fun versionCodeOf(info: PackageInfo): Long =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            @Suppress("DEPRECATION")
            info.versionCode.toLong()
        }

    private fun installerOf(packageName: String): String? = try {
        val pm = context.packageManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            pm.getInstallSourceInfo(packageName).installingPackageName
        } else {
            @Suppress("DEPRECATION")
            pm.getInstallerPackageName(packageName)
        }
    } catch (_: Exception) {
        null
    }

    private companion object {
        const val TAG = "FleetAppInventory"
    }
}
