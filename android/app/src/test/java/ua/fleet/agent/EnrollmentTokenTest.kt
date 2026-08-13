package ua.fleet.agent

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import ua.fleet.agent.enroll.EnrollmentRepository
import ua.fleet.agent.net.ApiClient

class EnrollmentTokenTest {

    @Test
    fun `нормалізує токен, введений вручну`() {
        val expected = "FLEET-ABCD-EFGH-JKLM-NPQR-STVW"
        assertEquals(expected, EnrollmentRepository.normalizeToken("fleet abcd efgh jklm npqr stvw"))
        assertEquals(expected, EnrollmentRepository.normalizeToken("ABCDEFGHJKLMNPQRSTVW"))
        assertEquals(expected, EnrollmentRepository.normalizeToken(" FLEET-ABCD-EFGH-JKLM-NPQR-STVW "))
    }

    @Test
    fun `відхиляє токен неправильної довжини`() {
        assertFalse(EnrollmentRepository.isTokenPlausible("FLEET-ABCD"))
        assertFalse(EnrollmentRepository.isTokenPlausible(""))
        assertTrue(EnrollmentRepository.isTokenPlausible("ABCDEFGHJKLMNPQRSTVW"))
    }

    @Test
    fun `будує базовий URL API`() {
        assertEquals("https://mdm.example.com/api/v1/", ApiClient.normalizeBaseUrl("https://mdm.example.com"))
        assertEquals("https://mdm.example.com/api/v1/", ApiClient.normalizeBaseUrl("https://mdm.example.com/"))
        assertEquals("https://mdm.example.com/api/v1/", ApiClient.normalizeBaseUrl("https://mdm.example.com/api/v1"))
    }

    @Test(expected = IllegalArgumentException::class)
    fun `відхиляє адресу без схеми`() {
        ApiClient.normalizeBaseUrl("mdm.example.com")
    }

    @Test
    fun `розбирає QR-payload панелі`() {
        val raw = """{"v":1,"server":"https://mdm.example.com",""" +
            """"enroll_url":"https://mdm.example.com/api/v1/enroll",""" +
            """"token":"FLEET-ABCD-EFGH-JKLM-NPQR-STVW"}"""
        val payload = EnrollmentRepository.parseQrPayload(raw)
        assertEquals("https://mdm.example.com", payload.server)
        assertEquals("FLEET-ABCD-EFGH-JKLM-NPQR-STVW", payload.token)
    }
}
