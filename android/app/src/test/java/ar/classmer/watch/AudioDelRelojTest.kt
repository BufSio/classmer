package ar.classmer.watch

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.grabacion.Recuperacion
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.SesionLocal
import ar.classmer.red.ClienteApi
import ar.classmer.subida.ServidorDePrueba
import ar.classmer.subida.Subidor
import ar.classmer.ui.FilaSesion
import ar.classmer.ui.Tono
import ar.classmer.watchlink.EmisorAudio
import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.FinRecepcion
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.Huella
import ar.classmer.watchlink.OrigenGrabacion
import ar.classmer.watchlink.ReceptorAudio
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import kotlin.random.Random

/**
 * El audio del reloj entra al mismo `audio.ogg` que sube el [Subidor]: la subida al server no cambia.
 * Mientras llega, la sesión sube por partes y no se finaliza; recién verificada, se cierra y se finaliza.
 */
class AudioDelRelojTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private lateinit var http: MockWebServer
    private lateinit var servidor: ServidorDePrueba
    private lateinit var almacen: AlmacenSesiones
    private lateinit var watch: File
    private val id = "0194a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"
    private val rnd = Random(22)

    @Before
    fun preparar() {
        servidor = ServidorDePrueba()
        http = MockWebServer().apply { dispatcher = servidor; start() }
        almacen = AlmacenSesiones(tmp.newFolder("sesiones"))
        watch = tmp.newFile("watch.ogg")
    }

    @After
    fun cerrar() = http.shutdown()

    private fun cuerpoCrear() = servidor.sesiones.getValue(id).cuerpoCrear.orEmpty()

    private fun subidor() = Subidor(almacen, ClienteApi(http.url("/").toString(), "tok"), tamanioTramo = 1000)

    private fun sesion(estado: String = EstadoLocal.GRABANDO) = SesionLocal(
        id = id, materiaId = "prueba", origenMateria = "manual", tipoClase = "teorica",
        inicio = "2026-10-22T14:00:05-03:00", dispositivo = "galaxy-watch8-classic",
        estadoLocal = estado, fuenteAudio = FuenteAudio.RELOJ,
    )

    /** El reloj graba [n] bytes más y el celu los trae por el canal (desde lo que ya tiene). */
    private fun grabarYTraer(n: Int) {
        FileOutputStream(watch, true).use { it.write(rnd.nextBytes(n)) }
        val canal = ByteArrayOutputStream()
        val local = almacen.archivoAudio(id)
        EmisorAudio.enviar(watch, local.length(), watch.length(), canal)
        assertEquals(FinRecepcion.COMPLETA, ReceptorAudio(local).recibir(ByteArrayInputStream(canal.toByteArray())).fin)
    }

    @Test
    fun `mientras llega el audio del reloj se sube por partes y recien verificado se finaliza`() = runTest {
        almacen.crear(sesion())
        grabarYTraer(2_500)
        assertEquals(Subidor.Resultado.PARCIAL, subidor().sincronizar(id))
        grabarYTraer(3_100)

        // El usuario tocó Terminar en el celu: la clase terminó, pero falta audio del reloj.
        almacen.guardarSesion(almacen.leerSesion(id)!!.copy(estadoLocal = EstadoLocal.RECIBIENDO))
        assertEquals(Subidor.Resultado.PARCIAL, subidor().sincronizar(id))
        assertFalse(servidor.sesiones.getValue(id).finalizada)
        assertTrue(servidor.pedidos.none { it.contains("finalizar") })

        grabarYTraer(900)
        // Verificada contra la huella del reloj: la sesión se cierra y la subida la finaliza como siempre.
        assertEquals(Huella.sha256(watch), Huella.sha256(almacen.archivoAudio(id)))
        almacen.guardarSesion(almacen.leerSesion(id)!!.copy(estadoLocal = EstadoLocal.TERMINADA, duracionMs = 4_000))
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val s = servidor.sesiones.getValue(id)
        assertTrue(s.finalizada)
        assertArrayEquals(watch.readBytes(), s.audio.toByteArray())
        assertTrue("el server sabe que grabó el reloj", cuerpoCrear().contains("\"dispositivo\":\"galaxy-watch8-classic\""))
        assertFalse("la fuente es sólo local", cuerpoCrear().contains("fuente_audio"))
    }

    @Test
    fun `si el celu se cae, una clase del reloj no se cierra como interrumpida`() {
        almacen.crear(sesion())
        almacen.crear(sesion().copy(id = "otra", fuenteAudio = FuenteAudio.TELEFONO))
        val cerradas = Recuperacion.cerrarHuerfanas(almacen, activa = null, inactividadMs = 0)
        assertEquals(listOf("otra"), cerradas.map { it.first.id })
        assertEquals(EstadoLocal.GRABANDO, almacen.leerSesion(id)!!.estadoLocal)
    }

    private val base = FilaSesion(
        id = id, materia = "Prueba", tipoClase = "teorica", fecha = "", duracion = null, estado = "", detalle = null,
        progreso = null, tono = Tono.NORMAL, pdf = null,
    )

    @Test
    fun `en la lista se ve cuanto falta del reloj y, sin contacto un rato, se ofrece cerrar con lo recibido`() {
        almacen.crear(sesion(EstadoLocal.RECIBIENDO))
        grabarYTraer(1_000)
        val ahora = 10_000_000L
        val registros = RegistrosWatch(almacen)
        registros.guardar(
            id,
            RegistroWatch(
                vistaEnWatch = true, ultimoContacto = ahora - 5 * 60_000,
                grabacion = GrabacionWatch(
                    id = id, estado = EstadoGrabacionWatch.TERMINADA, origen = OrigenGrabacion.CELU, inicio = "",
                    bytes = 4 * 1_048_576L, acumuladoMs = 0,
                ),
            ),
        )
        val fila = FilasWatch.fila(base, almacen.leerSesion(id)!!, almacen, activa = false, ahoraMs = ahora)!!
        assertEquals("Recibiendo del reloj", fila.estado)
        val detalle = fila.detalle.orEmpty()
        assertTrue(detalle, detalle.startsWith("Faltan 4.0 MB del reloj") || detalle.startsWith("Faltan 4,0 MB del reloj"))
        assertTrue(detalle.contains("sin contacto hace 5 min"))
        assertNull("hace poco que no contesta: todavía no se ofrece cerrar", fila.accionWatch)

        registros.cambiar(id) { it.copy(ultimoContacto = ahora - FilasWatch.OFRECER_CERRAR_MS - 1) }
        assertEquals(FilasWatch.CERRAR, FilasWatch.fila(base, almacen.leerSesion(id)!!, almacen, false, ahora)!!.accionWatch)
    }

    @Test
    fun `una clase del reloj que el celu no sigue ofrece seguirla, y las del telefono no cambian`() {
        almacen.crear(sesion(EstadoLocal.GRABANDO))
        assertEquals(FilasWatch.SEGUIR, FilasWatch.fila(base, almacen.leerSesion(id)!!, almacen, activa = false)!!.accionWatch)
        assertNull(FilasWatch.fila(base, almacen.leerSesion(id)!!, almacen, activa = true))
        val delCelu = sesion().copy(id = "celu", fuenteAudio = FuenteAudio.TELEFONO)
        almacen.crear(delCelu)
        assertNull(FilasWatch.fila(base, delCelu, almacen, activa = false))
    }
}
