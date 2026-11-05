package ar.classmer.watch

import ar.classmer.modelo.FuenteAudio
import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.EstadoWatch
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.OrigenGrabacion
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ElegirFuenteTest {
    private val ahora = 1_000_000L

    private fun estado(bateria: Int? = 80, cargando: Boolean = false, protocolo: Int = 1, error: String? = null, activa: Boolean = false) =
        EstadoWatch(
            protocolo = protocolo, ahora = 5, bateria = bateria, cargando = cargando, modelo = "SM-L500", error = error,
            grabaciones = if (activa) {
                listOf(
                    GrabacionWatch(
                        id = "0194a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b", estado = EstadoGrabacionWatch.GRABANDO,
                        origen = OrigenGrabacion.WATCH, inicio = "", bytes = 10, acumuladoMs = 0, desde = 1,
                    ),
                )
            } else {
                emptyList()
            },
        )

    private val conectado = InfoWatch(
        buscado = true, conectado = true, appInstalada = true, nombre = "Galaxy Watch8 Classic (3F2A)", nodo = "n1",
        estado = estado(), estadoEn = ahora - 5_000,
    )

    private fun decidir(info: InfoWatch, preferida: String = FuenteAudio.RELOJ) = ElegirFuente.decidir(preferida, info, ahora)

    @Test
    fun `con el telefono no importa el reloj`() {
        val d = decidir(InfoWatch(), FuenteAudio.TELEFONO)
        assertEquals(FuenteAudio.TELEFONO, d.fuente)
        assertNull(d.texto)
        assertFalse(d.ofrecerTelefono)
    }

    @Test
    fun `reloj conectado con bateria graba el reloj y muestra la bateria`() {
        val d = decidir(conectado)
        assertEquals(FuenteAudio.RELOJ, d.fuente)
        assertEquals(ElegirFuente.Nivel.OK, d.nivel)
        assertEquals("⌚ Galaxy Watch8 Classic (3F2A) · conectado · batería 80 %", d.texto)
        assertFalse(d.ofrecerTelefono)
    }

    @Test
    fun `sin reloj conectado o sin la app se ofrece el telefono`() {
        val sinReloj = decidir(InfoWatch(buscado = true))
        assertEquals(ElegirFuente.Nivel.PROBLEMA, sinReloj.nivel)
        assertTrue(sinReloj.ofrecerTelefono)
        assertTrue(sinReloj.texto!!.contains("no está conectado"))

        val sinApp = decidir(conectado.copy(appInstalada = false, estado = null))
        assertEquals(ElegirFuente.Nivel.PROBLEMA, sinApp.nivel)
        assertTrue(sinApp.ofrecerTelefono)
        assertTrue(sinApp.texto!!.contains("falta instalar Classmer"))
    }

    @Test
    fun `si la app del reloj no contesta se ofrece el telefono`() {
        val d = decidir(conectado.copy(estado = null, sinRespuesta = true))
        assertEquals(ElegirFuente.Nivel.PROBLEMA, d.nivel)
        assertTrue(d.ofrecerTelefono)
        // Un estado viejo cuenta como que no contestó.
        val viejo = decidir(conectado.copy(estadoEn = ahora - ElegirFuente.ESTADO_VIEJO_MS - 1, sinRespuesta = true))
        assertTrue(viejo.ofrecerTelefono)
        // Todavía no se le preguntó: no se alarma.
        val sinPreguntar = decidir(conectado.copy(estado = null))
        assertEquals(ElegirFuente.Nivel.OK, sinPreguntar.nivel)
        assertFalse(sinPreguntar.ofrecerTelefono)
    }

    @Test
    fun `con poca bateria avisa y ofrece el telefono, pero deja grabar con el reloj`() {
        val d = decidir(conectado.copy(estado = estado(bateria = 25)))
        assertEquals(FuenteAudio.RELOJ, d.fuente)
        assertEquals(ElegirFuente.Nivel.AVISO, d.nivel)
        assertTrue(d.texto!!.contains("batería 25 %"))
        assertTrue(d.ofrecerTelefono)
        // Cargando no es un problema.
        assertEquals(ElegirFuente.Nivel.OK, decidir(conectado.copy(estado = estado(bateria = 25, cargando = true))).nivel)
        // El umbral es 30 %.
        assertEquals(ElegirFuente.Nivel.OK, decidir(conectado.copy(estado = estado(bateria = 30))).nivel)
        assertEquals(ElegirFuente.Nivel.AVISO, decidir(conectado.copy(estado = estado(bateria = 29))).nivel)
    }

    @Test
    fun `otra version del protocolo pide actualizar la app del reloj`() {
        val d = decidir(conectado.copy(estado = estado(protocolo = 2)))
        assertEquals(ElegirFuente.Nivel.PROBLEMA, d.nivel)
        assertTrue(d.texto!!.contains("actualizá"))
        assertTrue(d.ofrecerTelefono)
    }

    @Test
    fun `si el reloj ya esta grabando, Grabar sigue esa clase`() {
        val d = decidir(conectado.copy(estado = estado(activa = true)))
        assertEquals("0194a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b", d.grabacionEnCurso)
        assertEquals(FuenteAudio.RELOJ, d.fuente)
    }

    @Test
    fun `un problema del reloj (por ejemplo, sin permiso de avisos) se muestra`() {
        val d = decidir(conectado.copy(estado = estado(error = "El reloj no tiene permiso para mostrar avisos")))
        assertEquals(ElegirFuente.Nivel.AVISO, d.nivel)
        assertTrue(d.texto!!.contains("permiso para mostrar avisos"))
    }

    @Test
    fun `el dispositivo que va al server dice que grabo el reloj`() {
        assertEquals("galaxy-watch8-classic", SincronizacionWatch.nombreDispositivo("SM-L500"))
        assertEquals("galaxy-watch8-classic", SincronizacionWatch.nombreDispositivo("SM-L505F"))
        assertEquals("galaxy-watch8", SincronizacionWatch.nombreDispositivo("SM-L330"))
        assertEquals("reloj-pixel-watch-3", SincronizacionWatch.nombreDispositivo("Pixel Watch 3"))
        assertEquals("reloj", SincronizacionWatch.nombreDispositivo(""))
    }

    // ---- Ambos ----

    @Test
    fun `con Ambos se decide como con el reloj, pero graba los dos`() {
        val d = decidir(conectado, FuenteAudio.AMBOS)
        assertEquals(FuenteAudio.AMBOS, d.fuente)
        assertEquals(ElegirFuente.Nivel.OK, d.nivel)
        assertEquals("⌚ Galaxy Watch8 Classic (3F2A) · conectado · batería 80 %", d.texto)
        assertFalse(d.ofrecerTelefono)
    }

    @Test
    fun `con Ambos un problema del reloj es un aviso porque el celu graba igual`() {
        val sinReloj = decidir(InfoWatch(buscado = true), FuenteAudio.AMBOS)
        assertEquals(FuenteAudio.AMBOS, sinReloj.fuente)
        assertEquals(ElegirFuente.Nivel.AVISO, sinReloj.nivel)
        assertTrue(sinReloj.texto!!.contains("no está conectado"))
        assertTrue(sinReloj.texto!!.contains("Graba el celu"))
        assertTrue(sinReloj.ofrecerTelefono)
        // Poca batería sigue siendo un aviso.
        assertEquals(ElegirFuente.Nivel.AVISO, decidir(conectado.copy(estado = estado(bateria = 20)), FuenteAudio.AMBOS).nivel)
    }

    @Test
    fun `con Ambos y el reloj ya grabando, Grabar la sigue y el celu graba tambien`() {
        val d = decidir(conectado.copy(estado = estado(activa = true)), FuenteAudio.AMBOS)
        assertEquals("0194a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b", d.grabacionEnCurso)
        assertEquals(FuenteAudio.AMBOS, d.fuente)
        assertTrue(d.texto!!.endsWith("y el celu graba también"))
    }

    @Test
    fun `con Ambos y una clase que ya tiene la pista del celu, avisa que sigue solo el reloj`() {
        val d = ElegirFuente.decidir(FuenteAudio.AMBOS, conectado.copy(estado = estado(activa = true)), ahora, celuYaGrabo = true)
        assertTrue(d.texto!!.contains("sigue sólo el reloj"))
        assertFalse(d.texto!!.contains("el celu graba también"))
    }
}
