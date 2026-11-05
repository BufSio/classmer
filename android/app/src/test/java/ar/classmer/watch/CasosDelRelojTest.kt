package ar.classmer.watch

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Evento
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.SesionLocal
import ar.classmer.ui.FilaSesion
import ar.classmer.ui.Tono
import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.OrigenGrabacion
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Casos que salieron de la revisión: clases sin audio, continuaciones, offsets al volver a seguir. */
class CasosDelRelojTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val almacen by lazy { AlmacenSesiones(tmp.newFolder("sesiones")) }

    private fun sesion(id: String) = SesionLocal(
        id = id, materiaId = "macro", origenMateria = "calendario", tipoClase = "teorica",
        inicio = "2026-10-22T14:00:05-03:00", dispositivo = "galaxy-watch8-classic", numeroClase = 7,
        nombreMateria = "Macro", fuenteAudio = FuenteAudio.RELOJ,
    )

    @Test
    fun `una clase que el reloj nunca empezo se descarta si no tiene nada, y se conserva si hay fotos o notas`() {
        val vacia = sesion("vacia").also { almacen.crear(it) }
        assertTrue(SincronizacionWatch.descartarSiVacia(almacen, vacia))
        assertFalse(almacen.dir("vacia").exists())

        val conNota = sesion("con-nota").also { almacen.crear(it) }
        almacen.agregarEvento(conNota.id, Evento("e1", "nota", 0, "x", texto = "tema del parcial"))
        assertFalse(SincronizacionWatch.descartarSiVacia(almacen, conNota))
        assertTrue(almacen.dir("con-nota").isDirectory)

        val conFoto = sesion("con-foto").also { almacen.crear(it) }
        almacen.dirFotos(conFoto.id).resolve("f.jpg").writeBytes(ByteArray(10))
        assertFalse(SincronizacionWatch.descartarSiVacia(almacen, conFoto))
    }

    @Test
    fun `la continuacion de una falla del reloj sigue en la misma materia y numero, con su propio id`() {
        val a = sesion("a").copy(duracionMs = 1_800_000, interrumpida = true)
        val g = GrabacionWatch(
            id = "b", estado = EstadoGrabacionWatch.GRABANDO, origen = OrigenGrabacion.CELU, inicio = "2026-10-22T14:30:10-03:00",
            bytes = 0, acumuladoMs = 0, desde = 1, continuaDe = "a",
        )
        val b = SincronizacionWatch.continuacion(a, g)
        assertEquals("b", b.id)
        assertEquals("a", b.continuaDe)
        assertEquals("macro", b.materiaId)
        assertEquals(7, b.numeroClase)
        assertEquals(EstadoLocal.GRABANDO, b.estadoLocal)
        assertEquals(0, b.duracionMs)
        assertFalse(b.interrumpida)
        assertEquals(FuenteAudio.RELOJ, b.fuenteAudio)
        assertEquals(EstadoLocal.RECIBIENDO, SincronizacionWatch.continuacion(a, g.copy(estado = EstadoGrabacionWatch.TERMINADA)).estadoLocal)
    }

    @Test
    fun `al volver a seguir una clase, las marcas no quedan en cero mientras contesta el reloj`() {
        val s = sesion("s").copy(duracionMs = 600_000)
        almacen.crear(s)
        val guardada = almacen.archivoSesion("s").lastModified()
        // Se guardó hace 25 s con 10 min grabados: van unos 10 min 25 s.
        assertEquals(625_000, SesionWatch.offsetProvisional(almacen, s, ahoraMs = guardada + 25_000))
        // Un reloj del celu corrido hacia atrás no da negativo.
        assertEquals(600_000, SesionWatch.offsetProvisional(almacen, s, ahoraMs = guardada - 5_000))
    }

    @Test
    fun `si la huella no coincide ni bajandola entera, se puede cerrar con lo recibido aunque el reloj este cerca`() {
        val s = sesion("r").copy(estadoLocal = EstadoLocal.RECIBIENDO).also { almacen.crear(it) }
        val ahora = 50_000_000L
        val registros = RegistrosWatch(almacen)
        registros.guardar("r", RegistroWatch(vistaEnWatch = true, ultimoContacto = ahora - 10_000))
        val base = FilaSesion(
            id = "r", materia = "Macro", tipoClase = null, fecha = "", duracion = null, estado = "", detalle = null,
            progreso = null, tono = Tono.NORMAL, pdf = null,
        )
        assertNull(FilasWatch.fila(base, s, almacen, activa = false, ahoraMs = ahora)!!.accionWatch)
        registros.cambiar("r") { it.copy(rebajadas = SincronizacionWatch.MAX_REBAJADAS) }
        assertEquals(FilasWatch.CERRAR, FilasWatch.fila(base, s, almacen, activa = false, ahoraMs = ahora)!!.accionWatch)
    }
}
