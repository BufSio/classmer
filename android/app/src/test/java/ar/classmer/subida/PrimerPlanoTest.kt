package ar.classmer.subida

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/** El tope de un trabajo al que Android le negó el primer plano (WorkManager ya no lo frena). */
class PrimerPlanoTest {
    private var ahora = 1_000L

    @Test
    fun `si Android lo niega, el trabajo corta a los 9 minutos de empezar`() = runTest {
        val p = PrimerPlano { ahora }
        ahora += 30_000
        assertFalse(p.pedir { throw IllegalStateException("startForegroundService() not allowed") })
        assertTrue(p.negado)
        // No se vuelve a pedir en el mismo intento.
        var pedidos = 0
        assertFalse(p.pedir { pedidos++ })
        assertEquals(0, pedidos)

        ahora = 1_000L + PrimerPlano.TOPE_MS - 1
        assertTrue(p.dentroDelTope)
        ahora = 1_000L + PrimerPlano.TOPE_MS
        assertFalse(p.dentroDelTope)
    }

    @Test
    fun `en primer plano, o sin pedirlo nunca, no hay tope`() = runTest {
        val enPrimerPlano = PrimerPlano { ahora }
        assertTrue(enPrimerPlano.pedir { })
        assertTrue(enPrimerPlano.activo)
        val comun = PrimerPlano { ahora }
        ahora += 60 * 60_000
        assertTrue(enPrimerPlano.dentroDelTope)
        // Un trabajo común de verdad (no hubo importada que subir): lo frena JobScheduler, como siempre.
        assertTrue(comun.dentroDelTope)
    }

    @Test
    fun `una cancelacion no cuenta como negado`() = runTest {
        val p = PrimerPlano { ahora }
        try {
            p.pedir { throw CancellationException("se canceló el trabajo") }
            fail("tenía que seguir la cancelación")
        } catch (e: CancellationException) {
            // ok
        }
        assertFalse(p.negado)
        assertFalse(p.activo)
    }
}
