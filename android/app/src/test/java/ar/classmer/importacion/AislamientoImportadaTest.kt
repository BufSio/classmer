package ar.classmer.importacion

import android.content.Context
import android.content.ContextWrapper
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.grabacion.CorteAutomatico
import ar.classmer.grabacion.Recuperacion
import ar.classmer.grabacion.ServicioGrabacion
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.EstadoSubida
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.SesionLocal
import ar.classmer.subida.SincronizacionSubida
import ar.classmer.ui.FilaSesion
import ar.classmer.ui.Tono
import ar.classmer.watch.FilasWatch
import ar.classmer.watch.RegistrosWatch
import ar.classmer.watch.SincronizacionWatch
import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.EstadoWatch
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.OrigenGrabacion
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * Condición 1 del diseño: nada de la grabación toca una clase importada, ni mientras se copia
 * (`importando`) ni ya `terminada` mientras sube. Ni la recuperación de grabaciones cortadas, ni el
 * corte automático, ni el reloj (su sincronización, sus filas y `watch.json`), ni el servicio de grabación.
 */
class AislamientoImportadaTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val raiz by lazy { tmp.newFolder("files") }
    private val almacen by lazy { AlmacenSesiones(File(raiz, "sesiones")) }
    private val hace10Min = System.currentTimeMillis() - 10 * 60_000

    private val copiando = "019a1b2c-3d4e-7f50-8a6b-000000000001"
    private val subiendo = "019a1b2c-3d4e-7f50-8a6b-000000000002"

    private fun importada(id: String, estado: String, fuente: String = FuenteAudio.TELEFONO): SesionLocal {
        val pedido = PedidoImportacion(
            audio = DocumentoElegido("content://audio/$id", "clase.m4a", 1000, "audio/mp4"),
            duracionMs = 60_000, fotos = emptyList(), materiaId = "macro", nombreMateria = "Macro",
            origenMateria = "calendario", tipoClase = "teorica", inicio = "2026-10-22T14:00:00-03:00",
            numeroClase = 7, eventoCalendario = "Macro", dispositivo = "s23-ultra",
        )
        val s = Importador.crear(almacen, pedido, id = id, espacioLibre = { 0L })
        val final = s.copy(estadoLocal = estado, fuenteAudio = fuente)
        almacen.guardarSesion(final)
        almacen.archivoAudio(final).writeBytes(ByteArray(1000))
        // Sin actividad hace rato: una grabación así ya se habría cerrado como interrumpida.
        almacen.dir(id).listFiles()!!.forEach { it.setLastModified(hace10Min) }
        return final
    }

    private fun lasDos(): List<SesionLocal> = listOf(importada(copiando, EstadoLocal.IMPORTANDO), importada(subiendo, EstadoLocal.TERMINADA))

    /** Todo lo que hay en la carpeta de cada clase, para ver que nada cambió. */
    private fun foto(ids: List<String>): Map<String, String> =
        ids.flatMap { id -> almacen.dir(id).walkTopDown().filter { it.isFile }.map { "$id/${it.relativeTo(almacen.dir(id))}" to it.readBytes().contentHashCode().toString() + "@" + it.length() } }.toMap()

    @Test
    fun `la recuperacion no cierra una importada como interrumpida`() {
        val importadas = lasDos()
        // Aunque una importada figurara "grabando" (no debería pasar nunca), tampoco se cierra.
        val rara = importada("019a1b2c-3d4e-7f50-8a6b-000000000003", EstadoLocal.GRABANDO)
        // Y una grabación cortada de verdad sí se cierra: la recuperación sigue andando.
        val cortada = SesionLocal("019a1b2c-3d4e-7f50-8a6b-000000000004", null, "auto", "teorica", "2026-10-22T10:00:00-03:00", "s23-ultra")
        almacen.crear(cortada)
        almacen.archivoAudio(cortada.id).writeBytes(ByteArray(10))
        almacen.dir(cortada.id).listFiles()!!.forEach { it.setLastModified(hace10Min) }
        val ids = importadas.map { it.id } + rara.id
        val antes = foto(ids)

        val cerradas = Recuperacion.cerrarHuerfanas(almacen, activa = null, inactividadMs = 0)

        assertEquals(listOf(cortada.id), cerradas.map { it.first.id })
        assertEquals(antes, foto(ids))
        assertEquals(EstadoLocal.IMPORTANDO, almacen.leerSesion(copiando)!!.estadoLocal)
        assertFalse(almacen.leerSesion(subiendo)!!.interrumpida)
        assertEquals(EstadoLocal.GRABANDO, almacen.leerSesion(rara.id)!!.estadoLocal)
    }

    @Test
    fun `el corte automatico nunca pregunta por una importada`() {
        for (s in lasDos()) {
            assertNull(s.preguntarCorteMs)
            assertNull(s.finEventoMs)
            val muchoDespues = System.currentTimeMillis() + 24 * 60 * 60_000L
            assertEquals(CorteAutomatico.Accion.NADA, CorteAutomatico.evaluar(s.preguntarCorteMs, muchoDespues, preguntaAbiertaMs = null))
        }
    }

    @Test
    fun `el servicio de grabacion no se engancha a una importada, aunque figure del reloj y grabando`() {
        val rara = importada("019a1b2c-3d4e-7f50-8a6b-000000000005", EstadoLocal.GRABANDO, fuente = FuenteAudio.RELOJ)
        assertFalse(rara.delReloj)
        assertNull(ServicioGrabacion.paraEnganchar(rara))
        // Una del reloj de verdad, sí.
        assertNotNull(ServicioGrabacion.paraEnganchar(rara.copy(importada = false)))
        for (s in lasDos()) assertNull(ServicioGrabacion.paraEnganchar(s))
    }

    @Test
    fun `el reloj no ve las importadas ni les escribe watch json`() {
        val importadas = lasDos() + importada("019a1b2c-3d4e-7f50-8a6b-000000000006", EstadoLocal.TERMINADA, fuente = FuenteAudio.RELOJ)
        assertFalse(SincronizacionWatch.hayPendientes(almacen))
        val base = FilaSesion(
            id = "x", materia = "Macro", tipoClase = "teorica", fecha = "", duracion = null, estado = "", detalle = null,
            progreso = null, tono = Tono.NORMAL, pdf = null,
        )
        for (s in importadas) {
            assertNull(FilasWatch.fila(base.copy(id = s.id), s, almacen, activa = false))
            assertFalse(RegistrosWatch(almacen).archivo(s.id).exists())
        }
    }

    @Test
    fun `la sincronizacion del reloj no toca una importada aunque el reloj informe una grabacion con su id`() = runTest {
        val importadas = lasDos()
        val ids = importadas.map { it.id }
        val antes = foto(ids)
        val estado = EstadoWatch(
            ahora = 1,
            grabaciones = importadas.map { s ->
                GrabacionWatch(
                    id = s.id, estado = EstadoGrabacionWatch.TERMINADA, origen = OrigenGrabacion.WATCH,
                    inicio = s.inicio, bytes = 5000, acumuladoMs = 60_000,
                )
            },
        )

        val contexto = ContextoDePrueba(raiz)
        // La sincronización lee las mismas clases que el test.
        assertEquals(almacen.raiz, AlmacenSesiones.de(contexto).raiz)
        SincronizacionWatch.sincronizar(contexto, estado, nodo = "reloj-1")

        assertEquals(antes, foto(ids))
        for (id in ids) assertFalse(RegistrosWatch(almacen).archivo(id).exists())
    }

    @Test
    fun `la subida no manda una importada mientras se copia y deja las importadas para el final`() {
        val (enCopia, terminada) = lasDos()
        assertTrue(SincronizacionSubida.todaviaNo(enCopia, EstadoSubida(), largoAudio = 1000))
        assertFalse(SincronizacionSubida.todaviaNo(terminada, EstadoSubida(), largoAudio = 1000))
        val grabada = SesionLocal("019a1b2c-3d4e-7f50-8a6b-000000000009", null, "auto", "teorica", "2026-10-24T10:00:00-03:00", "s23-ultra")
        // listar() devuelve la más nueva primero; la subida va de la más vieja a la más nueva, importadas al final.
        val orden = SincronizacionSubida.ordenDeSubida(listOf(grabada, terminada, enCopia))
        assertEquals(listOf(grabada.id, enCopia.id, terminada.id), orden.map { it.id })
    }

    /** Lo mínimo de un Context para [AlmacenSesiones.de]: la carpeta de la app. */
    private class ContextoDePrueba(private val raiz: File) : ContextWrapper(null) {
        override fun getExternalFilesDir(type: String?): File = raiz
        override fun getFilesDir(): File = raiz
        override fun getApplicationContext(): Context = this
    }
}
