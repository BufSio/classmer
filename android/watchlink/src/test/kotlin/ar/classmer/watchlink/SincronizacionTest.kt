package ar.classmer.watchlink

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.random.Random

class SincronizacionTest {
    /** El reloj del Watch va [real] ms adelante del celu: watch = celu + real. */
    private val real = 7_312_455L

    /** Una ida y vuelta con [ida] ms de ida, [vuelta] de vuelta y [respuesta] ms que tarda el Watch en contestar. */
    private fun muestra(t1: Long, ida: Long, vuelta: Long, respuesta: Long = 3): Muestra {
        val t2 = t1 + ida + real
        val t3 = t2 + respuesta
        val t4 = t3 - real + vuelta
        return Muestra(t1, t2, t3, t4)
    }

    @Test
    fun `con ida y vuelta iguales el desfase es exacto`() {
        val d = Sincronizacion.estimar(listOf(muestra(1_000, ida = 40, vuelta = 40)))!!
        assertEquals(real, d.ms)
        assertEquals(80, d.idaYVueltaMs)
        assertEquals(40, d.errorMs)
    }

    @Test
    fun `con ida y vuelta distintas el error queda dentro de la cota`() {
        val d = Sincronizacion.estimar(listOf(muestra(1_000, ida = 900, vuelta = 100)))!!
        assertEquals(1_000, d.idaYVueltaMs)
        assertTrue(abs(d.ms - real) <= d.errorMs)
        assertEquals(400, abs(d.ms - real))
    }

    @Test
    fun `se queda con la ida y vuelta mas rapida`() {
        val muestras = listOf(
            muestra(1_000, ida = 600, vuelta = 50),
            muestra(2_000, ida = 30, vuelta = 45),
            muestra(3_000, ida = 300, vuelta = 300),
        )
        val d = Sincronizacion.estimar(muestras)!!
        assertEquals(75, d.idaYVueltaMs)
        assertTrue(abs(d.ms - real) <= 8)
        assertEquals(muestras.maxOf { it.t4 }, d.medidoEn)
    }

    @Test
    fun `al azar el error real nunca pasa la cota`() {
        val rnd = Random(15)
        repeat(1_000) {
            val muestras = List(Sincronizacion.MUESTRAS) { i ->
                muestra(10_000L * i, ida = rnd.nextLong(5, 1_500), vuelta = rnd.nextLong(5, 1_500), respuesta = rnd.nextLong(0, 50))
            }
            val d = Sincronizacion.estimar(muestras)!!
            assertTrue("error ${abs(d.ms - real)} > cota ${d.errorMs}", abs(d.ms - real) <= d.errorMs)
        }
    }

    @Test
    fun `las muestras imposibles se descartan`() {
        assertNull(Sincronizacion.estimar(emptyList()))
        // t4 antes que t1, o el Watch "contestó antes de recibir".
        assertNull(Sincronizacion.estimar(listOf(Muestra(100, 50, 60, 90), Muestra(100, 60, 50, 200))))
        val d = Sincronizacion.estimar(listOf(Muestra(100, 50, 60, 90), muestra(1_000, 20, 20)))!!
        assertEquals(real, d.ms)
    }

    @Test
    fun `una medicion vieja pierde contra una nueva un poco peor`() {
        val vieja = Desfase(ms = real, errorMs = 20, idaYVueltaMs = 40, medidoEn = 0)
        val nueva = Desfase(ms = real + 30, errorMs = 60, idaYVueltaMs = 120, medidoEn = 3_600_000)
        // A la hora, la vieja puede haberse corrido 180 ms (50 ppm): gana la nueva.
        assertEquals(nueva, Sincronizacion.mejor(vieja, nueva, ahoraCelu = 3_600_000))
        // Recién medida, gana la de menos error.
        val recien = nueva.copy(medidoEn = 0)
        assertEquals(vieja, Sincronizacion.mejor(vieja, recien, ahoraCelu = 0))
        assertEquals(vieja, Sincronizacion.mejor(vieja, null, ahoraCelu = 0))
        assertEquals(nueva, Sincronizacion.mejor(null, nueva, ahoraCelu = 0))
    }

    @Test
    fun `el offset del audio del Watch se cuenta en el celu sin las pausas`() {
        val d = Desfase(ms = real, errorMs = 30, idaYVueltaMs = 60, medidoEn = 0)
        // El Watch arrancó en su elapsedRealtime 9_000_000: en el celu, 9_000_000 − real.
        val arranque = 9_000_000L
        val g = GrabacionWatch(
            id = "x", estado = EstadoGrabacionWatch.GRABANDO, origen = OrigenGrabacion.CELU, inicio = "",
            bytes = 0, acumuladoMs = 0, desde = arranque,
        )
        val celuAlArrancar = Sincronizacion.aCelu(arranque, d)
        assertEquals(0, g.offsetEn(celuAlArrancar, d))
        assertEquals(65_000, g.offsetEn(celuAlArrancar + 65_000, d))
        // Antes de que arranque, el offset no es negativo.
        assertEquals(0, g.offsetEn(celuAlArrancar - 5_000, d))
        // En pausa con 65 s grabados: el offset no avanza.
        val pausada = g.copy(estado = EstadoGrabacionWatch.PAUSADA, acumuladoMs = 65_000, desde = null)
        assertEquals(65_000, pausada.offsetEn(celuAlArrancar + 600_000, d))
        // Reanudó 10 min después (en el Watch): sigue desde 65 s.
        val reanudada = pausada.copy(estado = EstadoGrabacionWatch.GRABANDO, desde = arranque + 665_000)
        assertEquals(65_000 + 12_000, reanudada.offsetEn(celuAlArrancar + 665_000 + 12_000, d))
    }

    @Test
    fun `el error crece con el tiempo por la deriva`() {
        val d = Desfase(ms = 0, errorMs = 10, idaYVueltaMs = 20, medidoEn = 1_000)
        assertEquals(10, d.errorEn(1_000))
        assertEquals(10 + 180, d.errorEn(1_000 + 3_600_000))
        assertEquals(10, d.errorEn(0))
    }
}
