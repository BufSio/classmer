package ar.classmer.watchlink

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocoloTest {
    @Test
    fun `el estado va y vuelve igual`() {
        val e = EstadoWatch(
            respuestaA = 7, ahora = 123_456, bateria = 81, cargaUah = 350_000, modelo = "SM-L505F", versionApp = "0.3.0",
            esperando = null,
            grabaciones = listOf(
                GrabacionWatch(
                    id = IdGrabacion.nuevo(), estado = EstadoGrabacionWatch.GRABANDO, origen = OrigenGrabacion.WATCH,
                    inicio = "2026-10-22T14:00:00-03:00", bytes = 4_096, acumuladoMs = 1_000, desde = 120_000,
                ),
            ),
        )
        assertEquals(e, decodificar(EstadoWatch.serializer(), codificar(EstadoWatch.serializer(), e)))
        assertEquals(e.grabaciones[0], e.activa)
    }

    @Test
    fun `los campos nuevos de otra version se ignoran y lo invalido da null`() {
        val json = """{"protocolo":2,"ahora":5,"campo_nuevo":{"x":1},"grabaciones":[]}"""
        val e = decodificar(EstadoWatch.serializer(), json.toByteArray())!!
        assertEquals(2, e.protocolo)
        assertNull(e.activa)
        assertNull(decodificar(EstadoWatch.serializer(), "no es json".toByteArray()))
        assertNull(decodificar(Orden.serializer(), "{}".toByteArray()))
    }

    @Test
    fun `los nombres van en snake_case como en el resto de Classmer`() {
        val texto = String(codificar(Orden.serializer(), Orden(n = 1, id = "a", tipoClase = "teorica")))
        assertTrue(texto, texto.contains("\"tipo_clase\":\"teorica\""))
        // Los nulos no viajan.
        assertTrue(texto, !texto.contains("sha256"))
    }
}
