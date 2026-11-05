package ar.classmer.ui

import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.AdjuntoServidor
import ar.classmer.modelo.EstadoSesionServidor
import ar.classmer.modelo.EstadoSubidaAdjuntos
import ar.classmer.modelo.SoporteAdjuntos
import ar.classmer.modelo.SubidaAdjunto
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ResumenAdjuntosTest {

    private fun local(id: String, nombre: String = "$id.pdf", mime: String = "application/pdf", offsetMs: Long? = null) =
        AdjuntoLocal(id, "s1", nombre, mime, 2_400_000, offsetMs, "2026-10-22T15:03:10-03:00", "$id.pdf")

    private fun remota(estado: String, sinUsar: Int = 0, vararg adjuntos: AdjuntoServidor) =
        EstadoSesionServidor(id = "s1", estado = estado, adjuntos = adjuntos.toList(), adjuntosSinUsar = sinUsar)

    @Test
    fun `sin adjuntos no hay nada que mostrar`() {
        val r = ResumenAdjuntos.de(emptyList(), EstadoSubidaAdjuntos(), remota("lista"), claseEnServidor = true)
        assertEquals(0, r.total)
        assertNull(r.texto)
        assertFalse(r.regenerable)
        assertEquals("📎 Adjuntos", r.etiquetaBoton)
    }

    @Test
    fun `cuenta los del celu y los del server sin repetir`() {
        val subida = EstadoSubidaAdjuntos().con("a1", SubidaAdjunto(subido = true))
        val r = ResumenAdjuntos.de(
            listOf(local("a1"), local("a2")),
            subida,
            remota("lista", 0, AdjuntoServidor("a1", estado = "usado"), AdjuntoServidor("panel", estado = "usado")),
            claseEnServidor = true,
        )
        assertEquals(3, r.total)
        assertEquals(1, r.sinSubir)
        assertEquals("1 adjunto sin subir", r.texto)
        assertEquals("📎 Adjuntos (3)", r.etiquetaBoton)
    }

    @Test
    fun `regenerable con adjuntos sin usar y la clase lista o en error`() {
        val sinUsar = AdjuntoServidor("a1", estado = "sin_usar")
        assertTrue(ResumenAdjuntos.de(emptyList(), EstadoSubidaAdjuntos(), remota("lista", 1, sinUsar), true).regenerable)
        assertTrue(ResumenAdjuntos.de(emptyList(), EstadoSubidaAdjuntos(), remota("error", 1, sinUsar), true).regenerable)
        assertFalse(ResumenAdjuntos.de(emptyList(), EstadoSubidaAdjuntos(), remota("procesando", 1, sinUsar), true).regenerable)
        assertFalse(ResumenAdjuntos.de(emptyList(), EstadoSubidaAdjuntos(), remota("lista", 0, sinUsar), true).regenerable)
    }

    @Test
    fun `sin lo ultimo del server usa lo que devolvio al subir`() {
        val subida = EstadoSubidaAdjuntos(sesion = remota("lista", 1, AdjuntoServidor("a1", estado = "sin_usar")))
            .con("a1", SubidaAdjunto(subido = true))
        val r = ResumenAdjuntos.de(listOf(local("a1")), subida, remota = null, claseEnServidor = true)
        assertTrue(r.regenerable)
        assertEquals(0, r.sinSubir)
    }

    @Test
    fun `el texto de la fila avisa lo mas importante primero`() {
        val locales = listOf(local("a1"), local("a2"), local("a3"))
        val base = EstadoSubidaAdjuntos()
        assertEquals(
            "3 adjuntos esperan que la clase llegue al server",
            ResumenAdjuntos.de(locales, base, null, claseEnServidor = false).texto,
        )
        val rechazado = base.con("a2", SubidaAdjunto(rechazo = "la clase está en error"))
        assertEquals("1 adjunto rechazado por el server", ResumenAdjuntos.de(locales, rechazado, null, true).texto)
        // El server sin soporte vale para todos los que faltan subir (está en soporte.json, no en cada uno).
        val r = ResumenAdjuntos.de(locales, rechazado, null, true, SoporteAdjuntos(sinSoporte = true))
        assertEquals("El server todavía no acepta adjuntos: quedan guardados en el celu", r.texto)
        assertTrue(r.conProblemas)
        val perdida = EstadoSubidaAdjuntos(noEncontrada = EstadoSubidaAdjuntos.MAX_NO_ENCONTRADA + 1)
        ResumenAdjuntos.de(locales, perdida, null, true).let {
            assertEquals("El server no encuentra esta clase: 3 adjuntos sin subir", it.texto)
            assertTrue(it.claseNoEncontrada)
            assertTrue(it.conProblemas)
        }
        val todos = listOf("a1", "a2", "a3").fold(base) { e, id -> e.con(id, SubidaAdjunto(subido = true)) }
        assertNull(ResumenAdjuntos.de(locales, todos, null, true).texto)
        assertFalse("con todo subido, el server viejo ya no importa", ResumenAdjuntos.de(locales, todos, null, true, SoporteAdjuntos(sinSoporte = true)).sinSoporte)
    }

    @Test
    fun `cada estado de un adjunto del celu`() {
        val a = local("a1", offsetMs = 2_092_000)
        fun vista(
            s: SubidaAdjunto,
            servidor: AdjuntoServidor? = null,
            enServidor: Boolean = true,
            pedido: Boolean = false,
            clase: EstadoSubidaAdjuntos = EstadoSubidaAdjuntos(),
            sinSoporte: Boolean = false,
            subiendo: Boolean = false,
        ) = VistasAdjuntos.de(a, s, servidor, enServidor, pedido, clase, sinSoporte, subiendo)

        assertEquals("PDF · 2,3 MB · en 34:52", vista(SubidaAdjunto()).detalle)
        vista(SubidaAdjunto(), enServidor = false).let {
            assertEquals("Esperando que la clase llegue al server", it.estado)
            assertTrue(it.descartable)
        }
        vista(SubidaAdjunto()).let {
            assertEquals("Pendiente de subir", it.estado)
            assertFalse(it.enCurso)
            assertTrue(it.descartable)
        }
        vista(SubidaAdjunto(), subiendo = true).let {
            assertEquals("Subiendo…", it.estado)
            assertTrue(it.enCurso)
            assertFalse("no se descarta lo que se está mandando", it.descartable)
        }
        vista(SubidaAdjunto(ultimoError = "Sin conexión con el server")).let {
            assertEquals("Pendiente de subir", it.estado)
            assertEquals("Sin conexión con el server", it.explicacion)
            assertEquals(Tono.ERROR, it.tono)
            assertFalse(it.reintentable)
        }
        assertTrue("tras una falla al mandarlo, se puede saltear la espera", vista(SubidaAdjunto(fallos = 1, ultimoError = "HTTP 503")).reintentable)
        vista(SubidaAdjunto(), sinSoporte = true).let {
            assertEquals("Server sin soporte", it.estado)
            assertTrue(it.reintentable)
            assertTrue(it.descartable)
        }
        vista(SubidaAdjunto(), clase = EstadoSubidaAdjuntos(noEncontrada = 2, ultimoError = "El server no encuentra esta clase (sesión no encontrada)")).let {
            assertEquals("Pendiente de subir", it.estado)
            assertEquals("El server no encuentra esta clase (sesión no encontrada): se reintenta", it.explicacion)
        }
        vista(SubidaAdjunto(), clase = EstadoSubidaAdjuntos(noEncontrada = EstadoSubidaAdjuntos.MAX_NO_ENCONTRADA + 1)).let {
            assertEquals("La clase no está en el server", it.estado)
            assertTrue(it.reintentable)
            assertTrue(it.descartable)
            assertEquals(Tono.ERROR, it.tono)
        }
        vista(SubidaAdjunto(rechazo = "el archivo no es PDF, JPEG, PNG, WebP ni HEIC")).let {
            assertEquals("Rechazado", it.estado)
            assertEquals("el archivo no es PDF, JPEG, PNG, WebP ni HEIC", it.explicacion)
            assertTrue(it.reintentable)
            assertTrue(it.descartable)
        }
        assertEquals("Reintentando", vista(SubidaAdjunto(rechazo = "x"), pedido = true).estado)
        assertEquals("Reintentando", vista(SubidaAdjunto(), pedido = true, sinSoporte = true).estado)
        vista(SubidaAdjunto(subido = true)).let {
            assertEquals("Subido", it.estado)
            assertEquals(Tono.OK, it.tono)
            assertFalse(it.descartable)
        }
        // El estado que informa el server manda sobre el que devolvió al subirlo.
        val alSubir = SubidaAdjunto(subido = true, servidor = AdjuntoServidor("a1", estado = "pendiente"))
        assertEquals("Lo va a usar el próximo apunte", vista(alSubir).explicacion)
        assertEquals("Lo usó el apunte vigente", vista(alSubir, AdjuntoServidor("a1", estado = "usado")).explicacion)
        vista(alSubir, AdjuntoServidor("a1", estado = "sin_usar")).let {
            assertEquals("El apunte vigente no lo usa", it.explicacion)
            assertEquals(Tono.ACTIVO, it.tono)
        }
        // Si el server ya lo tiene (se perdió la respuesta), cuenta como subido.
        assertEquals("Subido", vista(SubidaAdjunto(ultimoError = "Sin conexión"), AdjuntoServidor("a1", estado = "pendiente")).estado)
    }

    @Test
    fun `la lista aplica el server sin soporte a todos y marca el que se esta mandando`() {
        val vistas = VistasAdjuntos.lista(
            listOf(local("a1"), local("a2"), local("a3")),
            EstadoSubidaAdjuntos().con("a3", SubidaAdjunto(subido = true)),
            emptyList(),
            claseEnServidor = true,
            soporte = SoporteAdjuntos(sinSoporte = true),
        )
        assertEquals(listOf("Server sin soporte", "Server sin soporte", "Subido"), vistas.map { it.estado })

        val subiendo = VistasAdjuntos.lista(listOf(local("a1"), local("a2")), EstadoSubidaAdjuntos(), emptyList(), true, subiendo = "a2")
        assertEquals(listOf("Pendiente de subir", "Subiendo…"), subiendo.map { it.estado })
    }

    @Test
    fun `el dia de una clase sale de su id`() {
        val ms = java.time.OffsetDateTime.parse("2026-10-22T15:03:10-03:00").toInstant().toEpochMilli()
        val id = ar.classmer.util.Uuid7.nuevo(ms)
        val esperado = java.time.Instant.ofEpochMilli(ms).atZone(java.time.ZoneId.systemDefault())
            .format(java.time.format.DateTimeFormatter.ofPattern("EEE d/M", java.util.Locale.forLanguageTag("es-AR")))
        assertEquals(esperado, ResumenAdjuntos.diaDeId(id))
        assertNull(ResumenAdjuntos.diaDeId("no-es-un-id"))
    }

    @Test
    fun `la lista suma los que el server tiene y el celu no`() {
        val vistas = VistasAdjuntos.lista(
            listOf(local("a1", nombre = "Slides.pdf"), local("a2", nombre = "pizarrón.jpg", mime = "image/jpeg")),
            EstadoSubidaAdjuntos(),
            listOf(
                AdjuntoServidor("a1", estado = "usado"),
                AdjuntoServidor("panel", nombre = "Guia_3.pdf", nombreOriginal = "Guía 3.pdf", tipo = "pdf", bytes = 900_000, estado = "sin_usar"),
            ),
            claseEnServidor = true,
        )
        assertEquals(listOf("a1", "a2", "panel"), vistas.map { it.id })
        assertEquals(listOf(true, false, true), vistas.map { it.esPdf })
        assertEquals("Lo usó el apunte vigente", vistas[0].explicacion)
        assertEquals("Pendiente de subir", vistas[1].estado)
        assertEquals("Guía 3.pdf", vistas[2].nombre)
        assertEquals("PDF · 879 KB · no está en el celu", vistas[2].detalle)
        assertEquals("El apunte vigente no lo usa", vistas[2].explicacion)
    }

    @Test
    fun `tamanios legibles`() {
        assertEquals("1 KB", VistasAdjuntos.tamanio(10))
        assertEquals("1024 KB", VistasAdjuntos.tamanio(1024 * 1024 - 1))
        assertEquals("1,0 MB", VistasAdjuntos.tamanio(1024 * 1024))
        assertEquals("100,0 MB", VistasAdjuntos.tamanio(100L * 1024 * 1024))
    }
}
