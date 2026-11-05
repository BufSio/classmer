package ar.classmer.ui

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger

/** "Importar y procesar" en el alcance del ViewModel: girar el celu mientras se guarda no la corta ni la duplica. */
class TareaUnicaTest {
    private val alcance = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    @After
    fun cerrar() = alcance.cancel()

    @Test
    fun `si se cancela quien espera, la tarea sigue, y otro pedido espera la misma`() = runBlocking {
        val tarea = TareaUnica<Int>(alcance)
        val seguir = CompletableDeferred<Unit>()
        val corridas = AtomicInteger(0)
        val terminadas = AtomicInteger(0)
        val guardar: suspend () -> Int = {
            corridas.incrementAndGet()
            seguir.await()
            terminadas.incrementAndGet()
            7
        }

        // La pantalla que la pidió se rehace (se giró el celu) mientras se guarda.
        val pantalla = launch(Dispatchers.Default) { tarea.correr(guardar) }
        withTimeout(5_000) { while (corridas.get() == 0) delay(5) }
        pantalla.cancelAndJoin()

        // Otro toque (la pantalla nueva) no crea otra clase: espera la que sigue.
        val otra = async(Dispatchers.Default) { tarea.correr(guardar) }
        seguir.complete(Unit)
        assertEquals(7, withTimeout(5_000) { otra.await() })
        assertEquals(1, corridas.get())
        assertEquals("la tarea terminó aunque se canceló quien la pidió", 1, terminadas.get())

        // Terminada, un pedido nuevo corre otra vez.
        assertEquals(7, tarea.correr(guardar))
        assertEquals(2, corridas.get())
    }
}
