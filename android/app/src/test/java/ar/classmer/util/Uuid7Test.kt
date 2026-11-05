package ar.classmer.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.UUID

class Uuid7Test {
    @Test
    fun `formato, version 7, variante RFC y orden por tiempo`() {
        val a = Uuid7.nuevo(1_700_000_000_000)
        val b = Uuid7.nuevo(1_700_000_000_001)
        val u = UUID.fromString(a)
        assertEquals(7, u.version())
        assertEquals(2, u.variant())
        assertTrue(a < b)
        assertTrue(a.startsWith("018bcfe5-6800-7"))
        assertEquals(36, a.length)
    }
}
