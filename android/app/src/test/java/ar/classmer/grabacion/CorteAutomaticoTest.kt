package ar.classmer.grabacion

import ar.classmer.grabacion.CorteAutomatico.Accion
import org.junit.Assert.assertEquals
import org.junit.Test

class CorteAutomaticoTest {
    private val min = 60_000L
    private val inicio = 1_800_000_000_000L

    @Test
    fun `los plazos son los de la arquitectura`() {
        assertEquals(15 * min, CorteAutomatico.MARGEN_FIN_EVENTO_MS)
        assertEquals(210 * min, CorteAutomatico.SIN_CALENDARIO_MS)
        assertEquals(10 * min, CorteAutomatico.ESPERA_RESPUESTA_MS)
        assertEquals(30 * min, CorteAutomatico.VOLVER_A_PREGUNTAR_MS)
    }

    @Test
    fun `con un evento que termina despues de empezar se pregunta al fin mas 15 min`() {
        val fin = inicio + 90 * min
        assertEquals(fin + 15 * min, CorteAutomatico.primeraPregunta(inicio, fin))
        // Aunque el evento sea más largo que la regla sin calendario, vale el fin del evento.
        val finLargo = inicio + 5 * 60 * min
        assertEquals(finLargo + 15 * min, CorteAutomatico.primeraPregunta(inicio, finLargo))
        // Un evento que termina un instante después de empezar a grabar todavía cuenta.
        assertEquals(inicio + 1 + 15 * min, CorteAutomatico.primeraPregunta(inicio, inicio + 1))
    }

    @Test
    fun `un evento que termina justo al empezar o que ya termino usa las 3 h 30`() {
        assertEquals(inicio + 210 * min, CorteAutomatico.primeraPregunta(inicio, inicio))
        assertEquals(inicio + 210 * min, CorteAutomatico.primeraPregunta(inicio, inicio - 1))
        assertEquals(inicio + 210 * min, CorteAutomatico.primeraPregunta(inicio, inicio - 2 * 60 * min))
    }

    @Test
    fun `sin evento del calendario se pregunta a las 3 h 30 de empezada`() {
        assertEquals(inicio + 210 * min, CorteAutomatico.primeraPregunta(inicio, null))
    }

    @Test
    fun `linea de tiempo completa con calendario`() {
        val preguntarEn = CorteAutomatico.primeraPregunta(inicio, inicio + 90 * min)
        assertEquals(inicio + 105 * min, preguntarEn)

        // Antes del umbral no pasa nada.
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(preguntarEn, inicio, null))
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(preguntarEn, preguntarEn - 1, null))
        // En el umbral (y después, si el tick llegó tarde) se pregunta.
        assertEquals(Accion.PREGUNTAR, CorteAutomatico.evaluar(preguntarEn, preguntarEn, null))
        assertEquals(Accion.PREGUNTAR, CorteAutomatico.evaluar(preguntarEn, preguntarEn + 3 * min, null))

        // Con la pregunta abierta se espera la respuesta: no se vuelve a preguntar.
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(preguntarEn, preguntarEn + 5_000, 0))
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(preguntarEn, preguntarEn + 5 * min, 5 * min))
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(preguntarEn, preguntarEn + 10 * min - 1_000, 10 * min - 1_000))
        // A los 10 min sin respuesta termina sola.
        assertEquals(Accion.TERMINAR, CorteAutomatico.evaluar(preguntarEn, preguntarEn + 10 * min, 10 * min))
        assertEquals(Accion.TERMINAR, CorteAutomatico.evaluar(preguntarEn, preguntarEn + 12 * min, 12 * min))
    }

    @Test
    fun `la espera de la respuesta se mide con el reloj monotonico, no con la hora del celu`() {
        val preguntarEn = inicio + 210 * min
        // La hora del celu saltó para atrás (antes del umbral) pero la pregunta lleva 10 min abierta.
        assertEquals(Accion.TERMINAR, CorteAutomatico.evaluar(preguntarEn, inicio, 10 * min))
        // La hora del celu saltó para adelante pero la pregunta recién se abrió.
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(preguntarEn, preguntarEn + 60 * min, 1_000))
    }

    @Test
    fun `sin hora para preguntar nunca hace nada`() {
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(null, inicio, null))
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(null, inicio + 24 * 60 * min, null))
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(null, inicio, 10 * min))
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(null, inicio, 60 * min))
    }

    @Test
    fun `seguir vuelve a preguntar 30 min despues y recien ahi`() {
        val preguntarEn = CorteAutomatico.primeraPregunta(inicio, null)
        assertEquals(Accion.PREGUNTAR, CorteAutomatico.evaluar(preguntarEn, preguntarEn, null))

        // Se responde "Seguir" a los 4 min de abierta la pregunta.
        val respuesta = preguntarEn + 4 * min
        val proxima = CorteAutomatico.seguir(respuesta)
        assertEquals(respuesta + 30 * min, proxima)

        // La pregunta se cerró (preguntaAbierta = null): nada hasta la próxima hora.
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(proxima, respuesta, null))
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(proxima, respuesta + 10 * min, null))
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(proxima, proxima - 1, null))
        assertEquals(Accion.PREGUNTAR, CorteAutomatico.evaluar(proxima, proxima, null))

        // La segunda pregunta tiene otros 10 min para responderse.
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(proxima, proxima + 9 * min, 9 * min))
        assertEquals(Accion.TERMINAR, CorteAutomatico.evaluar(proxima, proxima + 10 * min, 10 * min))

        // Seguir de nuevo: otra vez 30 min desde la respuesta.
        val tercera = CorteAutomatico.seguir(proxima + min)
        assertEquals(proxima + 31 * min, tercera)
        assertEquals(Accion.NADA, CorteAutomatico.evaluar(tercera, proxima + 20 * min, null))
        assertEquals(Accion.PREGUNTAR, CorteAutomatico.evaluar(tercera, tercera, null))
    }
}
