package ar.classmer.subida

import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.SesionLocal
import ar.classmer.red.ClienteApi
import ar.classmer.subida.SincronizacionSubida.Resultado
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.random.Random

/** Cómo se encadenan los adjuntos con la subida de las clases ([ColaDePasadas], [SincronizacionAdjuntos]). */
class SincronizacionAdjuntosTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val alcance = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    @After
    fun cerrar() = alcance.cancel()

    private suspend fun esperarQue(condicion: () -> Boolean) = withTimeout(5_000) {
        while (!condicion()) delay(5)
    }

    @Test
    fun `una pasada a la vez, y un pedido en el medio le hace dar otra vuelta`() = runBlocking {
        val cola = ColaDePasadas(alcance)
        val seguir = CompletableDeferred<Unit>()
        val corridas = AtomicInteger(0)
        val activas = AtomicInteger(0)
        var maxActivas = 0
        val pasada: suspend () -> Resultado = {
            maxActivas = maxOf(maxActivas, activas.incrementAndGet())
            if (corridas.incrementAndGet() == 1) seguir.await()
            activas.decrementAndGet()
            Resultado.LISTO
        }

        val primera = cola.pedir(pasada)
        esperarQue { corridas.get() == 1 }
        // Llega un adjunto nuevo mientras la primera vuelta está mandando un archivo.
        val segunda = cola.pedir(pasada)
        assertSame("no arranca otra pasada: se suma a la que corre", primera, segunda)
        seguir.complete(Unit)

        assertEquals(Resultado.LISTO, primera.await())
        assertEquals("dio una vuelta más por el pedido del medio", 2, corridas.get())
        assertEquals("nunca dos a la vez", 1, maxActivas)

        // Terminada, un pedido nuevo arranca otra.
        assertEquals(Resultado.LISTO, cola.pedir(pasada).await())
        assertEquals(3, corridas.get())
    }

    @Test
    fun `si se cancela quien espera, la pasada sigue y el siguiente se suma`() = runBlocking {
        val cola = ColaDePasadas(alcance)
        val seguir = CompletableDeferred<Unit>()
        val corridas = AtomicInteger(0)
        val terminadas = AtomicInteger(0)
        val pasada: suspend () -> Resultado = {
            if (corridas.incrementAndGet() == 1) seguir.await()
            terminadas.incrementAndGet()
            Resultado.REINTENTAR
        }

        // Como la subida final al terminar la clase (tope de 60 s) o WorkManager que corta a los 10 min.
        val quienEspera = launch { cola.despuesDe(Resultado.LISTO, esperar = true, pasada) }
        esperarQue { corridas.get() == 1 }
        quienEspera.cancelAndJoin()

        // El que pide después se suma a la misma pasada (el archivo no se vuelve a empezar).
        val siguiente = cola.pedir(pasada)
        seguir.complete(Unit)
        assertEquals(Resultado.REINTENTAR, siguiente.await())
        assertEquals("la pasada cortada terminó igual", 1, terminadas.get())
        assertEquals(1, corridas.get())
    }

    @Test
    fun `sin esperar devuelve lo de las clases enseguida y la pasada corre igual`() = runBlocking {
        val cola = ColaDePasadas(alcance)
        val seguir = CompletableDeferred<Unit>()
        val corrio = AtomicBoolean(false)
        // El servicio mientras graba: la subida del audio no espera detrás de un PDF.
        val r = cola.despuesDe(Resultado.REINTENTAR, esperar = false) {
            seguir.await()
            corrio.set(true)
            Resultado.LISTO
        }
        assertEquals(Resultado.REINTENTAR, r)
        seguir.complete(Unit)
        esperarQue { corrio.get() }
    }

    @Test
    fun `con el token rechazado no se intentan los adjuntos, y se combina lo peor`() = runBlocking {
        val cola = ColaDePasadas(alcance)
        assertEquals(Resultado.TOKEN_INVALIDO, cola.despuesDe(Resultado.TOKEN_INVALIDO, esperar = true) { fail("no tenía que correr"); Resultado.LISTO })
        assertEquals(Resultado.REINTENTAR, cola.despuesDe(Resultado.LISTO, esperar = true) { Resultado.REINTENTAR })
        assertEquals(Resultado.REINTENTAR, cola.despuesDe(Resultado.REINTENTAR, esperar = true) { Resultado.LISTO })
        assertEquals(Resultado.TOKEN_INVALIDO, cola.despuesDe(Resultado.REINTENTAR, esperar = true) { Resultado.TOKEN_INVALIDO })
        assertEquals(Resultado.LISTO, cola.despuesDe(Resultado.LISTO, esperar = true) { Resultado.LISTO })
    }

    @Test
    fun `una pasada que falla no traba la cola`() = runBlocking {
        val cola = ColaDePasadas(alcance)
        try {
            cola.pedir { throw IllegalStateException("roto") }.await()
            fail("tenía que fallar")
        } catch (e: IllegalStateException) {
            assertEquals("roto", e.message)
        }
        assertEquals(Resultado.LISTO, cola.pedir { Resultado.LISTO }.await())
    }

    // ---- Con el server de prueba ----

    private lateinit var http: MockWebServer
    private lateinit var servidor: ServidorDePrueba
    private lateinit var sesiones: AlmacenSesiones
    private lateinit var adjuntos: AlmacenAdjuntos
    private val id = "01a137d8-617d-718c-a700-3328dc9a1fec"

    @Before
    fun preparar() {
        servidor = ServidorDePrueba()
        http = MockWebServer().apply { dispatcher = servidor; start() }
        val raiz = tmp.newFolder("files")
        sesiones = AlmacenSesiones(File(raiz, "sesiones").apply { mkdirs() })
        adjuntos = AlmacenAdjuntos(File(raiz, "adjuntos"))
    }

    @After
    fun cerrarServidor() = http.shutdown()

    private fun api(token: String = "tok") = ClienteApi(http.url("/").toString(), token)

    private fun claseTerminadaConAdjunto() {
        sesiones.crear(
            SesionLocal(
                id = id, materiaId = "analisis", origenMateria = "calendario", tipoClase = "teorica",
                inicio = "2026-10-22T14:00:05-03:00", dispositivo = "s23-ultra",
                estadoLocal = EstadoLocal.TERMINADA, duracionMs = 5_000,
            ),
        )
        FileOutputStream(sesiones.archivoAudio(id), true).use { it.write(Random.nextBytes(500)) }
        adjuntos.copiar(id, ("%PDF-1.7\n".toByteArray() + Random.nextBytes(2000)).inputStream(), "Slides.pdf", 60_000, "2026-10-22T15:03:10-03:00")
    }

    /** Lo mismo que [SincronizacionSubida.ejecutar], sin Context: la clase y después sus adjuntos. */
    private suspend fun ejecutar(token: String = "tok", alRechazarToken: (String?) -> Unit = {}): Resultado {
        val clases = try {
            Subidor(sesiones, api(token)).sincronizar(id)
            Resultado.LISTO
        } catch (e: ar.classmer.red.ErrorAutenticacion) {
            Resultado.TOKEN_INVALIDO
        }
        return ColaDePasadas(alcance).despuesDe(clases, esperar = true) {
            SincronizacionAdjuntos.pasada(SubidorAdjuntos(adjuntos, sesiones, api(token)), alRechazarToken)
        }
    }

    @Test
    fun `los adjuntos van despues de finalizar la clase`() = runBlocking {
        claseTerminadaConAdjunto()

        assertEquals(Resultado.LISTO, ejecutar())

        val finalizar = servidor.pedidos.indexOfFirst { it.endsWith("/finalizar") }
        val primerAdjunto = servidor.pedidos.indexOfFirst { it.endsWith("/adjuntos") }
        assertTrue(servidor.pedidos.toString(), finalizar in 0 until primerAdjunto)
        assertTrue(sesiones.leerSubida(id).finalizada)
        assertTrue(adjuntos.leerSubida(id).de(adjuntos.listar(id).single().id).subido)
    }

    @Test
    fun `un 401 en los adjuntos es TOKEN_INVALIDO y lo anota`() = runBlocking {
        claseTerminadaConAdjunto()
        Subidor(sesiones, api()).sincronizar(id)
        var anotado: String? = null

        // La clase ya está finalizada (no pide nada); el token lo rechaza la pasada de adjuntos.
        val r = ColaDePasadas(alcance).despuesDe(Resultado.LISTO, esperar = true) {
            SincronizacionAdjuntos.pasada(SubidorAdjuntos(adjuntos, sesiones, api("otro"))) { anotado = it }
        }

        assertEquals(Resultado.TOKEN_INVALIDO, r)
        assertEquals("Token rechazado (HTTP 401)", anotado)
    }
}
