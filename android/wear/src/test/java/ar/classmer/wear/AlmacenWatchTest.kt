package ar.classmer.wear

import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.IdGrabacion
import ar.classmer.watchlink.OrigenGrabacion
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class AlmacenWatchTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun almacen() = AlmacenWatch(File(tmp.root, "grabaciones"))

    private fun grabacion(id: String = IdGrabacion.nuevo()) = GrabacionWatch(
        id = id, estado = EstadoGrabacionWatch.GRABANDO, origen = OrigenGrabacion.WATCH,
        inicio = "2026-10-22T14:00:00-03:00", bytes = 0, acumuladoMs = 0, desde = 10,
    )

    @Test
    fun `un pedido del celu vence a los 20 minutos y no se puede usar para una clase de otro momento`() {
        val a = almacen()
        val ahora = 1_000_000_000L
        val p = PedidoInicio(id = IdGrabacion.nuevo(), materia = "Macro", recibido = ahora)
        a.guardarPedido(p)
        assertEquals(p, a.leerPedido(ahora + 60_000))
        assertEquals(p, a.leerPedido(ahora + AlmacenWatch.VIGENCIA_PEDIDO_MS))
        assertNull(a.leerPedido(ahora + AlmacenWatch.VIGENCIA_PEDIDO_MS + 1))
        // Vencido, se borra: ni siquiera con la hora de antes vuelve.
        assertNull(a.leerPedido(ahora))
    }

    @Test
    fun `un pedido con la hora corrida hacia adelante tampoco vale`() {
        val a = almacen()
        a.guardarPedido(PedidoInicio(id = IdGrabacion.nuevo(), recibido = 5_000_000L))
        assertNull(a.leerPedido(5_000_000L - 2 * 60_000))
    }

    @Test
    fun `las grabaciones se listan de la mas vieja a la mas nueva y se borran por id`() {
        val a = almacen()
        val vieja = grabacion(IdGrabacion.nuevo(1_000_000))
        val nueva = grabacion(IdGrabacion.nuevo(2_000_000))
        a.crear(nueva)
        a.crear(vieja)
        a.archivoAudio(vieja.id).writeBytes(ByteArray(100))
        assertEquals(listOf(vieja.id, nueva.id), a.listar().map { it.id })
        assertEquals(100, a.bytesFirmes(vieja.id))
        assertTrue(a.borrar(vieja.id))
        assertEquals(listOf(nueva.id), a.listar().map { it.id })
    }

    @Test
    fun `un id que llega del celu y no es un UUID no toca el disco`() {
        val a = almacen()
        a.crear(grabacion())
        assertNull(a.leer("../pedido"))
        assertFalse(a.borrar(".."))
        assertTrue(File(tmp.root, "grabaciones").isDirectory)
    }
}
