package ar.classmer.util

import java.time.ZoneId
import java.time.ZoneOffset
import java.time.ZonedDateTime
import org.junit.Assert.assertEquals
import org.junit.Test

class TiempoTest {
    private val buenosAires = ZoneId.of("America/Argentina/Buenos_Aires")

    private fun ms(iso: String): Long = ZonedDateTime.parse(iso).toInstant().toEpochMilli()

    @Test
    fun `hora muestra la hora local de la zona pedida`() {
        val instante = ms("2026-10-15T14:35:00Z")
        assertEquals("11:35", Tiempo.hora(instante, buenosAires))
        assertEquals("14:35", Tiempo.hora(instante, ZoneOffset.UTC))
        assertEquals("16:35", Tiempo.hora(instante, ZoneId.of("Europe/Madrid")))
    }

    @Test
    fun `hora usa 24 h con dos digitos y descarta los segundos`() {
        assertEquals("00:05", Tiempo.hora(ms("2026-10-15T03:05:59Z"), buenosAires))
        assertEquals("23:59", Tiempo.hora(ms("2026-10-16T02:59:59.999Z"), buenosAires))
        assertEquals("09:00", Tiempo.hora(ms("2026-10-15T12:00:00Z"), buenosAires))
        assertEquals("15:30", Tiempo.hora(ms("2026-10-15T18:30:00Z"), buenosAires))
    }

    @Test
    fun `hora de la epoca`() {
        assertEquals("00:00", Tiempo.hora(0, ZoneOffset.UTC))
        assertEquals("21:00", Tiempo.hora(0, ZoneOffset.ofHours(-3)))
    }
}
