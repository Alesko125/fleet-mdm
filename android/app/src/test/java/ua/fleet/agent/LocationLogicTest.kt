package ua.fleet.agent

import java.io.File
import java.nio.file.Files
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import ua.fleet.agent.location.BatteryPolicy
import ua.fleet.agent.location.LocationQueue
import ua.fleet.agent.net.dto.LocationFixDto

class BatteryPolicyTest {

    @Test
    fun `на зарядці працюємо у штатному режимі`() {
        assertTrue(BatteryPolicy.shouldCollect(batteryPercent = 5, charging = true))
        assertEquals(900, BatteryPolicy.effectiveIntervalSeconds(900, 5, charging = true))
        assertTrue(BatteryPolicy.useHighAccuracy(requested = true, batteryPercent = 5, charging = true))
    }

    @Test
    fun `на критичному заряді збір зупиняється`() {
        assertFalse(BatteryPolicy.shouldCollect(batteryPercent = 8, charging = false))
        assertTrue(BatteryPolicy.shouldCollect(batteryPercent = 11, charging = false))
    }

    @Test
    fun `інтервал зростає з падінням заряду`() {
        assertEquals(600, BatteryPolicy.effectiveIntervalSeconds(600, 80, charging = false))
        assertEquals(1200, BatteryPolicy.effectiveIntervalSeconds(600, 30, charging = false))
        assertEquals(1800, BatteryPolicy.effectiveIntervalSeconds(600, 15, charging = false))
    }

    @Test
    fun `GPS вимикається на низькому заряді`() {
        assertTrue(BatteryPolicy.useHighAccuracy(true, batteryPercent = 50, charging = false))
        assertFalse(BatteryPolicy.useHighAccuracy(true, batteryPercent = 15, charging = false))
        // Якщо висока точність не запитана — вона не вмикається сама.
        assertFalse(BatteryPolicy.useHighAccuracy(false, batteryPercent = 90, charging = false))
    }
}

class LocationQueueTest {

    private lateinit var dir: File
    private lateinit var queue: LocationQueue

    private fun fix(lat: Double) = LocationFixDto(
        latitude = lat,
        longitude = 30.0,
        recordedAt = "2026-08-12T10:00:00Z",
    )

    @Before
    fun setUp() {
        dir = Files.createTempDirectory("fleet-queue").toFile()
        queue = LocationQueue(File(dir, "queue.jsonl"), maxSize = 5)
    }

    @After
    fun tearDown() {
        dir.deleteRecursively()
    }

    @Test
    fun `порожня черга нічого не віддає`() {
        assertEquals(0, queue.size)
        assertTrue(queue.peek(10).isEmpty())
    }

    @Test
    fun `координати зберігаються і читаються у порядку додавання`() {
        queue.add(fix(50.1))
        queue.add(fix(50.2))
        queue.add(fix(50.3))

        assertEquals(3, queue.size)
        val batch = queue.peek(2)
        assertEquals(2, batch.size)
        assertEquals(50.1, batch[0].latitude, 0.0001)
        assertEquals(50.2, batch[1].latitude, 0.0001)
    }

    @Test
    fun `після успішної відправки записи видаляються`() {
        queue.addAll(listOf(fix(50.1), fix(50.2), fix(50.3)))
        queue.drop(2)

        assertEquals(1, queue.size)
        assertEquals(50.3, queue.peek(1).first().latitude, 0.0001)
    }

    @Test
    fun `переповнена черга відкидає найстаріші записи`() {
        // Широти 50.0, 51.0 … 57.0 — так одразу видно, які записи лишились.
        repeat(8) { index -> queue.add(fix(50.0 + index)) }

        assertEquals(5, queue.size)
        // Лишилися останні пʼять: 53.0 … 57.0
        assertEquals(53.0, queue.peek(1).first().latitude, 0.0001)
        assertEquals(57.0, queue.peek(5).last().latitude, 0.0001)
    }

    @Test
    fun `очищення прибирає все`() {
        queue.addAll(listOf(fix(50.1), fix(50.2)))
        queue.clear()
        assertEquals(0, queue.size)
    }

    @Test
    fun `пошкоджений рядок не ламає читання`() {
        queue.add(fix(50.1))
        File(dir, "queue.jsonl").appendText("це не json\n")
        queue.add(fix(50.2))

        val batch = queue.peek(10)
        assertEquals(2, batch.size)
    }
}
