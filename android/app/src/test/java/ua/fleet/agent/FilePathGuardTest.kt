package ua.fleet.agent

import java.io.File
import java.nio.file.Files
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import ua.fleet.agent.files.FilePathGuard

class FilePathGuardTest {

    private lateinit var base: File
    private lateinit var allowed: File
    private lateinit var secret: File
    private lateinit var roots: List<FilePathGuard.Root>

    @Before
    fun setUp() {
        base = Files.createTempDirectory("fleet-files").toFile().canonicalFile
        allowed = File(base, "allowed").apply { mkdirs() }
        secret = File(base, "secret").apply { mkdirs() }
        File(secret, "passwords.txt").writeText("не для агента")

        roots = listOf(
            FilePathGuard.Root(
                path = allowed.absolutePath,
                writable = true,
                allowPrivileged = false,
                maxFileSizeMb = null,
            )
        )
    }

    @After
    fun tearDown() {
        base.deleteRecursively()
    }

    private fun resolve(path: String, needWrite: Boolean = false) =
        FilePathGuard.resolve(path, roots, needWrite)

    @Test
    fun `шлях усередині дозволеного каталогу приймається`() {
        val file = File(allowed, "report.txt")
        val (resolved, root) = resolve(file.absolutePath)
        assertEquals(file.canonicalPath, resolved.absolutePath)
        assertEquals(allowed.absolutePath, root.path)
    }

    @Test(expected = FilePathGuard.Violation::class)
    fun `порожній allowlist забороняє все`() {
        FilePathGuard.resolve(allowed.absolutePath, emptyList(), needWrite = false)
    }

    @Test(expected = FilePathGuard.Violation::class)
    fun `відносний шлях відхиляється`() {
        resolve("allowed/report.txt")
    }

    @Test(expected = FilePathGuard.Violation::class)
    fun `перехід через дві крапки відхиляється`() {
        resolve(allowed.absolutePath + "/../secret/passwords.txt")
    }

    @Test(expected = FilePathGuard.Violation::class)
    fun `каталог поза allowlist недоступний`() {
        resolve(File(secret, "passwords.txt").absolutePath)
    }

    @Test(expected = FilePathGuard.Violation::class)
    fun `каталог зі схожим префіксом недоступний`() {
        val lookalike = File(base, "allowed-private").apply { mkdirs() }
        resolve(File(lookalike, "a.txt").absolutePath)
    }

    @Test(expected = FilePathGuard.Violation::class)
    fun `запис у каталог лише для читання заборонено`() {
        val readOnly = listOf(
            FilePathGuard.Root(allowed.absolutePath, writable = false, allowPrivileged = false, maxFileSizeMb = null)
        )
        FilePathGuard.resolve(File(allowed, "a.txt").absolutePath, readOnly, needWrite = true)
    }

    @Test
    fun `симлінк за межі allowlist не дає доступу`() {
        val link = File(allowed, "escape")
        val created = runCatching {
            Files.createSymbolicLink(link.toPath(), secret.toPath())
        }.isSuccess
        // На Windows без відповідних прав символьні посилання недоступні.
        assumeTrue("Символьні посилання недоступні в цьому оточенні", created)

        val violation = runCatching {
            resolve(File(link, "passwords.txt").absolutePath)
        }.exceptionOrNull()
        assertTrue(violation is FilePathGuard.Violation)
    }

    @Test
    fun `межі каталогу перевіряються рядково коректно`() {
        assertTrue(FilePathGuard.isWithin("/sdcard/Fleet", "/sdcard/Fleet"))
        assertTrue(FilePathGuard.isWithin("/sdcard/Fleet/", "/sdcard/Fleet/logs/a.txt"))
        assertFalse(FilePathGuard.isWithin("/sdcard/Fleet", "/sdcard/FleetSecret/a.txt"))
        assertFalse(FilePathGuard.isWithin("/sdcard/Fleet", "/sdcard"))
    }

    @Test
    fun `імена файлів валідуються`() {
        assertEquals("report.txt", FilePathGuard.validateName(" report.txt "))
        for (bad in listOf("", ".", "..", "a/b", "a\\b", "a".repeat(256))) {
            val error = runCatching { FilePathGuard.validateName(bad) }.exceptionOrNull()
            assertTrue("Мало бути відхилено: $bad", error is FilePathGuard.Violation)
        }
    }

    // ------------------------------------------- ін'єкція команд оболонки --

    @Test
    fun `шлях із метасимволами оболонки відхиляється`() {
        val payloads = listOf(
            "/data/local/x\"; reboot; \"",
            "/data/local/\$(id)",
            "/data/local/`id`",
            "/data/local/a|b",
            "/data/local/a&b",
            "/data/local/a;b",
            "/data/local/a>b",
        )
        for (payload in payloads) {
            var blocked = false
            try {
                FilePathGuard.resolve(payload, roots, needWrite = true)
            } catch (e: FilePathGuard.Violation) {
                blocked = true
            }
            assertTrue("Мав бути відхилений: $payload", blocked)
        }
    }

    @Test(expected = FilePathGuard.Violation::class)
    fun `імя з метасимволами оболонки відхиляється`() {
        FilePathGuard.validateName("report\";rm -rf /\"")
    }

    @Test
    fun `екранування робить рядок безпечним для оболонки`() {
        assertEquals("'/tmp/file 1.txt'", FilePathGuard.shellQuote("/tmp/file 1.txt"))

        // Одинарна лапка всередині рядка закривається, додається літералом
        // через зворотний слеш і відкривається знову: a'b → 'a'\''b'.
        // Очікуване значення збираємо з символів, щоб не плутатись у тому,
        // скільки слешів «з'їдає» літерал Kotlin.
        val quote = '\''
        val backslash = '\\'
        val expected = "" + quote + "a" + quote + backslash + quote + quote + "b" + quote
        assertEquals(expected, FilePathGuard.shellQuote("a'b"))

        // Підстановка команди лишається звичайним текстом.
        assertEquals("'\$(id)'", FilePathGuard.shellQuote("\$(id)"))
    }

    @Test
    fun `sha256 приймається лише як шістнадцятковий`() {
        val valid = "a".repeat(64)
        assertEquals(valid, FilePathGuard.requireSha256(valid.uppercase()))

        val invalid = listOf("", "zz", "../../etc/passwd", "a".repeat(63), "a\"; id; \"")
        for (candidate in invalid) {
            var blocked = false
            try {
                FilePathGuard.requireSha256(candidate)
            } catch (e: FilePathGuard.Violation) {
                blocked = true
            }
            assertTrue("Мав бути відхилений: $candidate", blocked)
        }
    }
}
