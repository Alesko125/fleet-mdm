package ua.fleet.agent.command

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import ua.fleet.agent.files.FilePathGuard
import ua.fleet.agent.net.dto.CommandResultDto
import ua.fleet.agent.net.dto.FileEntryDto
import ua.fleet.agent.net.dto.FileRootDto

/**
 * Перетворення DTO дозволених каталогів на структуру, з якою працює
 * [FilePathGuard]. Агент завжди звіряє шлях із власним переліком, отриманим
 * із сервера, а не лише з тим, що прийшло в payload команди.
 */
internal fun List<FileRootDto>.toGuardRoots(): List<FilePathGuard.Root> = map { root ->
    FilePathGuard.Root(
        path = root.path,
        writable = root.writable,
        allowPrivileged = root.allowPrivileged,
        maxFileSizeMb = root.maxFileSizeMb,
    )
}

internal fun FileEntryDto.toJson() = buildJsonObject {
    put("name", JsonPrimitive(name))
    put("path", JsonPrimitive(path))
    put("is_dir", JsonPrimitive(isDir))
    put("size_bytes", JsonPrimitive(sizeBytes))
    put("modified_at", JsonPrimitive(modifiedAt))
    put("permissions", JsonPrimitive(permissions))
    put("is_symlink", JsonPrimitive(isSymlink))
    put("readable", JsonPrimitive(readable))
    put("writable", JsonPrimitive(writable))
}

internal fun entriesResult(entries: List<FileEntryDto>, path: String) = CommandResultDto(
    status = "succeeded",
    result = buildJsonObject {
        put("path", JsonPrimitive(path))
        put("count", JsonPrimitive(entries.size))
        put("entries", JsonArray(entries.map { it.toJson() }))
    },
)
