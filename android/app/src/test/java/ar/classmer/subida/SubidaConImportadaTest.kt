package ar.classmer.subida

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.grabacion.EstadoGrabacion
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.SesionLocal
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.random.Random

/**
 * [SincronizacionSubida] entera (contra [ServidorDePrueba]) con una importada grande subiendo: la
 * clase que se está grabando sube igual, mientras graba y al terminar.
 */
class SubidaConImportadaTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private lateinit var http: MockWebServer
    private val servidor = ServidorDePrueba()
    private val raiz by lazy { tmp.newFolder("files") }
    private val almacen by lazy { AlmacenSesiones(File(raiz, "sesiones")) }
    private val contexto by lazy { ContextoDePrueba(raiz) }

    private val importada = "019a1b2c-3d4e-7f50-8a6b-0000000000a1"
    private val grabada = "019a1b2c-3d4e-7f50-8a6b-0000000000a2"

    /** El primer PUT del audio de la importada queda colgado hasta [soltar]: la importada "se está subiendo". */
    private val importadaSubiendo = CountDownLatch(1)
    private val soltar = CountDownLatch(1)
    private val putsImportada = AtomicInteger(0)

    @Before
    fun preparar() {
        http = MockWebServer().apply {
            dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    if (request.method == "PUT" && request.path!!.startsWith("/api/sesiones/$importada/audio")) {
                        putsImportada.incrementAndGet()
                        importadaSubiendo.countDown()
                        soltar.await(30, TimeUnit.SECONDS)
                    }
                    return servidor.dispatch(request)
                }
            }
            start()
        }
        Ajustes(contexto).apply {
            urlServidor = http.url("/").toString()
            token = "tok"
        }
    }

    @After
    fun cerrar() {
        soltar.countDown()
        ControlGrabacion.estadoMutable.value = EstadoGrabacion()
        http.shutdown()
    }

    private fun crear(s: SesionLocal, bytes: Int) {
        almacen.crear(s)
        almacen.archivoAudio(s).writeBytes(Random.nextBytes(bytes))
    }

    @Test
    fun `mientras sube el audio de una importada, la clase que se graba sube igual`() = runBlocking {
        // Una importada ya copiada al celu, de 3 tramos de 4 MiB.
        crear(
            SesionLocal(
                importada, "macro", "calendario", "teorica", "2026-10-20T14:00:00-03:00", "s23-ultra", numeroClase = 7,
                estadoLocal = EstadoLocal.TERMINADA, duracionMs = 0, importada = true, audioNombre = "clase.m4a",
                audioBytes = 9L shl 20, archivoAudio = "audio.m4a",
            ),
            9 shl 20,
        )
        // Y la clase que se está grabando.
        val enCurso = SesionLocal(grabada, "macro", "calendario", "teorica", "2026-10-24T14:00:00-03:00", "s23-ultra")
        crear(enCurso, 100_000)
        ControlGrabacion.estadoMutable.value = EstadoGrabacion(sesion = enCurso)

        // WorkManager (por ejemplo, al abrir la app para sacar una foto) sube lo que hay y se mete con la importada.
        val trabajo = async(Dispatchers.IO) { SincronizacionSubida.ejecutar(contexto) }
        assertTrue("la importada empezó a subir", withContext(Dispatchers.IO) { importadaSubiendo.await(10, TimeUnit.SECONDS) })
        assertEquals(100_000, servidor.sesiones.getValue(grabada).audio.size())

        // Sigue grabando: el servicio sube lo nuevo (cada ~30 s) aunque la importada siga subiendo.
        almacen.archivoAudio(grabada).appendBytes(Random.nextBytes(50_000))
        assertNotNull("el servicio no quedó afuera", withTimeout(10_000) { SincronizacionSubida.intentarSiEstaLibre(contexto) })
        assertEquals(150_000, servidor.sesiones.getValue(grabada).audio.size())

        // Termina la clase: la subida final del servicio la finaliza sin esperar a la importada.
        ControlGrabacion.estadoMutable.value = EstadoGrabacion()
        almacen.guardarSesion(enCurso.copy(estadoLocal = EstadoLocal.TERMINADA, duracionMs = 60_000))
        assertEquals(SincronizacionSubida.Resultado.LISTO, withTimeout(10_000) { SincronizacionSubida.ejecutar(contexto, conImportadas = false) })
        assertTrue(servidor.sesiones.getValue(grabada).finalizada)
        assertFalse("la importada sigue subiendo", trabajo.isCompleted)
        assertFalse(servidor.sesiones.getValue(importada).finalizada)

        soltar.countDown()
        assertEquals(SincronizacionSubida.Resultado.LISTO, withTimeout(30_000) { trabajo.await() })
        val s = servidor.sesiones.getValue(importada)
        assertTrue(s.finalizada)
        assertEquals(listOf(4 shl 20, 4 shl 20, 1 shl 20), s.tramos)
    }

    @Test
    fun `dos subidas de importadas no corren a la vez, y lo que las acompana corre con su candado`() = runBlocking {
        crear(
            SesionLocal(
                importada, null, "auto", "teorica", "2026-10-20T14:00:00-03:00", "s23-ultra",
                estadoLocal = EstadoLocal.TERMINADA, importada = true, archivoAudio = "audio.m4a",
            ),
            5 shl 20,
        )
        val acompanando = CountDownLatch(1)
        val primera = async(Dispatchers.IO) { SincronizacionSubida.ejecutar(contexto, acompanar = { acompanando.countDown() }) }
        assertTrue(withContext(Dispatchers.IO) { importadaSubiendo.await(10, TimeUnit.SECONDS) })
        assertTrue("acompaña mientras sube la importada", withContext(Dispatchers.IO) { acompanando.await(10, TimeUnit.SECONDS) })

        // Otra (subida-siguiente, o la copia que termina) espera su turno: no manda el mismo audio a la vez.
        val segunda = async(Dispatchers.IO) { SincronizacionSubida.ejecutar(contexto) }
        Thread.sleep(300)
        assertFalse(segunda.isCompleted)
        assertEquals(1, putsImportada.get())

        soltar.countDown()
        assertEquals(SincronizacionSubida.Resultado.LISTO, withTimeout(30_000) { primera.await() })
        assertEquals(SincronizacionSubida.Resultado.LISTO, withTimeout(30_000) { segunda.await() })
        assertEquals(listOf(4 shl 20, 1 shl 20), servidor.sesiones.getValue(importada).tramos)
        assertEquals(2, putsImportada.get())
        assertTrue(servidor.sesiones.getValue(importada).finalizada)
    }

    @Test
    fun `si lo que acompana a las importadas tira, la pasada se corta ahi`() = runBlocking {
        crear(
            SesionLocal(
                importada, null, "auto", "teorica", "2026-10-20T14:00:00-03:00", "s23-ultra",
                estadoLocal = EstadoLocal.TERMINADA, importada = true, archivoAudio = "audio.m4a",
            ),
            5 shl 20,
        )
        class Tope : Exception()
        val r = async(Dispatchers.IO) {
            runCatching {
                SincronizacionSubida.ejecutar(contexto, acompanar = {
                    withContext(Dispatchers.IO) { importadaSubiendo.await(10, TimeUnit.SECONDS) }
                    throw Tope()
                })
            }
        }
        assertTrue(withTimeout(30_000) { r.await() }.exceptionOrNull() is Tope)
        assertFalse(servidor.sesiones.getValue(importada).finalizada)
        // El candado quedó libre: la próxima pasada la termina.
        soltar.countDown()
        assertEquals(SincronizacionSubida.Resultado.LISTO, withTimeout(30_000) { SincronizacionSubida.ejecutar(contexto) })
        assertTrue(servidor.sesiones.getValue(importada).finalizada)
    }
}
