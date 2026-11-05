package ar.classmer.grabacion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RelojGrabacionTest {
    private var t = 10_000L
    private val reloj = RelojGrabacion(ahora = { t })

    @Test
    fun `cuenta solo el tiempo grabado, sin las pausas`() {
        assertEquals(0, reloj.offsetMs())
        reloj.iniciar()
        t += 5_000
        assertEquals(5_000, reloj.offsetMs())
        assertEquals(5_000, reloj.pausar())
        assertFalse(reloj.corriendo)
        t += 60_000
        assertEquals(5_000, reloj.offsetMs())
        reloj.iniciar()
        assertTrue(reloj.corriendo)
        t += 1_500
        assertEquals(6_500, reloj.offsetMs())
    }

    @Test
    fun `iniciar o pausar dos veces no cambia nada`() {
        reloj.iniciar()
        t += 1_000
        reloj.iniciar()
        t += 1_000
        assertEquals(2_000, reloj.offsetMs())
        reloj.pausar()
        t += 1_000
        assertEquals(2_000, reloj.pausar())
    }
}
