package ar.classmer.subida

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Evento
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.TipoEvento
import ar.classmer.red.ClienteApi
import ar.classmer.red.ErrorApi
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import kotlin.random.Random

/** El Subidor de siempre con una clase importada (contra [ServidorDePrueba]). */
class SubidorImportadaTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private lateinit var http: MockWebServer
    private lateinit var servidor: ServidorDePrueba
    private lateinit var almacen: AlmacenSesiones
    private val id = "019a1b2c-3d4e-7f50-8a6b-7c8d9e0f1a2b"

    @Before
    fun preparar() {
        servidor = ServidorDePrueba()
        http = MockWebServer().apply { dispatcher = servidor; start() }
        almacen = AlmacenSesiones(tmp.newFolder("sesiones"))
    }

    @After
    fun cerrar() = http.shutdown()

    /** Con los tramos de siempre: 1 MiB para las grabadas y 4 MiB para las importadas. */
    private fun subidor() = Subidor(almacen, ClienteApi(http.url("/").toString(), "tok"))

    private fun importada(audio: ByteArray, duracionMs: Long = 0, estado: String = EstadoLocal.TERMINADA): SesionLocal {
        val s = SesionLocal(
            id = id, materiaId = "macro", origenMateria = "calendario", tipoClase = "teorica",
            inicio = "2026-10-22T14:00:00-03:00", dispositivo = "s23-ultra", numeroClase = 7,
            estadoLocal = estado, duracionMs = duracionMs, importada = true,
            audioNombre = "Clase 5 (Macro).m4a", audioBytes = audio.size.toLong(), archivoAudio = "audio.m4a",
        )
        almacen.crear(s)
        almacen.archivoAudio(s).writeBytes(audio)
        return s
    }

    private fun fotoSinMinuto(eventoId: String, ext: String, n: Int) {
        File(almacen.dirFotos(id), "$eventoId.$ext").writeBytes(Random.nextBytes(n))
        almacen.agregarEvento(id, Evento(eventoId, TipoEvento.FOTO, null, "2026-10-24T18:00:00-03:00", archivo = "fotos/$eventoId.$ext"))
    }

    @Test
    fun `sube el audio de la sesion en tramos de 4 MiB y la crea como importada`() = runTest {
        val audio = Random.nextBytes((4 shl 20) + 1000)
        importada(audio, duracionMs = 5_400_000)

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))

        val s = servidor.sesiones.getValue(id)
        assertArrayEquals(audio, s.audio.toByteArray())
        assertEquals(listOf(4 shl 20, 1000), s.tramos)
        assertFalse("no se inventa un audio.ogg", almacen.archivoAudio(id).exists())
        val crear = s.cuerpoCrear!!
        assertTrue(crear, crear.contains("\"importada\":true"))
        assertTrue(crear.contains("\"audio_nombre\":\"Clase 5 (Macro).m4a\""))
        assertTrue(crear.contains("\"audio_bytes\":${audio.size}"))
        assertTrue(crear.contains("\"numero_clase\":7"))
        assertTrue(s.cuerpoFinalizar!!.contains("\"duracion_ms\":5400000"))
        assertTrue(s.finalizada)
    }

    @Test
    fun `una grabada sigue en tramos de 1 MiB y sin los campos nuevos`() = runTest {
        val s = SesionLocal(id, "macro", "calendario", "teorica", "2026-10-22T14:00:00-03:00", "s23-ultra", estadoLocal = EstadoLocal.TERMINADA, duracionMs = 1000)
        almacen.crear(s)
        val audio = Random.nextBytes((1 shl 20) + 10)
        almacen.archivoAudio(id).writeBytes(audio)

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val r = servidor.sesiones.getValue(id)
        assertEquals(listOf(1 shl 20, 10), r.tramos)
        listOf("importada", "audio_nombre", "audio_bytes").forEach { assertFalse(it, r.cuerpoCrear!!.contains(it)) }
        assertTrue(r.cuerpoFinalizar!!.contains("\"duracion_ms\":1000"))
    }

    @Test
    fun `las fotos van sin offset_ms, con su nombre y su tipo`() = runTest {
        importada(Random.nextBytes(2000))
        fotoSinMinuto("019a1b2c-0000-7000-8000-0000000000f1", "jpg", 4321)
        fotoSinMinuto("019a1b2c-0000-7000-8000-0000000000f2", "png", 999)

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))

        val s = servidor.sesiones.getValue(id)
        assertEquals(mapOf("019a1b2c-0000-7000-8000-0000000000f1" to null, "019a1b2c-0000-7000-8000-0000000000f2" to null), s.offsets)
        assertEquals(4321, s.fotos["019a1b2c-0000-7000-8000-0000000000f1"])
        assertEquals("019a1b2c-0000-7000-8000-0000000000f1.jpg" to "image/jpeg", s.archivosFotos["019a1b2c-0000-7000-8000-0000000000f1"])
        assertEquals("019a1b2c-0000-7000-8000-0000000000f2.png" to "image/png", s.archivosFotos["019a1b2c-0000-7000-8000-0000000000f2"])
        assertTrue(s.cuerpoFinalizar!!.contains("\"eventos_totales\":2"))
    }

    @Test
    fun `sin duracion medida, finalizar la manda null`() = runTest {
        importada(Random.nextBytes(500), duracionMs = 0)
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val fin = servidor.sesiones.getValue(id).cuerpoFinalizar!!
        assertTrue(fin, fin.contains("\"duracion_ms\":null"))
    }

    @Test
    fun `un 413 del server (audio de mas de 2 GB) es permanente`() = runTest {
        servidor.maxAudioImportada = 1000
        importada(Random.nextBytes(1500))
        try {
            subidor().sincronizar(id)
            fail("tenía que fallar")
        } catch (e: ErrorApi) {
            assertEquals(413, e.codigo)
            assertTrue(e.permanente)
        }
        // No se probó "sin número" ni "como Auto": no es un problema de los datos de la clase.
        assertEquals(1, servidor.cuerposCrear.size)
        assertFalse(almacen.leerSubida(id).sesionCreada)
    }

    @Test
    fun `un 413 al pasar el tope mientras sube tambien es permanente`() = runTest {
        // El selector informó menos de lo que pesa: el server lo frena en el PUT.
        servidor.maxAudioImportada = 3 shl 20
        val s = importada(Random.nextBytes(5 shl 20))
        almacen.guardarSesion(s.copy(audioBytes = 1000))
        try {
            subidor().sincronizar(id)
            fail("tenía que fallar")
        } catch (e: ErrorApi) {
            assertEquals(413, e.codigo)
            assertTrue(e.permanente)
        }
        assertFalse(servidor.sesiones.getValue(id).finalizada)
    }

    @Test
    fun `mientras se copia al celu no va al server`() = runTest {
        importada(Random.nextBytes(500), estado = EstadoLocal.IMPORTANDO)
        assertEquals(Subidor.Resultado.ESPERANDO, subidor().sincronizar(id))
        assertTrue(servidor.pedidos.isEmpty())
        assertNull(servidor.sesiones[id])
    }
}
