package ar.classmer.ui

import ar.classmer.calendario.ClaseDelCalendario
import ar.classmer.calendario.EventoCalendario
import ar.classmer.calendario.ReglasCalendario
import ar.classmer.importacion.MetadatosAudio
import ar.classmer.modelo.Materia
import ar.classmer.modelo.OrigenMateria
import ar.classmer.modelo.ReglaCalendario
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId

/** "¿Qué clase es?" al importar: las clases del calendario de un día y lo que completa cada elección. */
class EleccionImportacionTest {
    private val zona = ZoneId.of("America/Argentina/Buenos_Aires")
    private val dia = LocalDate.of(2026, 10, 22)

    private val macro = Materia("macro", "Macroeconomía", ReglaCalendario(calendario = "Facultad", tituloRegex = "Macro"), proximoNumero = 9)
    private val analisis = Materia(
        "analisis", "Análisis Matemático II", ReglaCalendario(tituloRegex = "^(?!Clase particular).*An[aá]lisis Matem[aá]tico"), proximoNumero = 4,
    )
    private val medida = Materia("teoria-juegos", "Teoría de Juegos", ReglaCalendario(calendario = "Facultad"), tiposClase = listOf("teorica"))
    private val materias = listOf(macro, analisis, medida)

    private fun ms(hora: Int, minuto: Int = 0, d: LocalDate = dia) = LocalDateTime.of(d, java.time.LocalTime.of(hora, minuto)).atZone(zona).toInstant().toEpochMilli()

    private fun ev(titulo: String, desde: Int, hasta: Int, cal: String = "Personal", todoElDia: Boolean = false, d: LocalDate = dia) =
        EventoCalendario(titulo, ms(desde, d = d), ms(hasta, d = d), cal, todoElDia)

    private fun clasesDe(eventos: List<EventoCalendario>, d: LocalDate = dia): List<ClaseDelCalendario> {
        val (desde, hasta) = ElegirClaseImportada.limitesDelDia(d, zona)
        return ReglasCalendario.clasesDelDia(materias, eventos, desde, hasta)
    }

    // ---- Las clases de un día ----

    @Test
    fun `las clases del dia, en orden, con su materia y el tipo por el titulo`() {
        val clases = clasesDe(
            listOf(
                ev("Análisis Matemático - Práctica", 16, 18),
                ev("Macro II (Teórica)", 9, 11, cal = "Facultad"),
                ev("Clase particular - Ana (Análisis Matemático)", 19, 20),
                ev("Dentista", 12, 13),
                ev("Parcial de Juegos", 0, 23, cal = "Facultad", todoElDia = true),
                ev("Clase 7", 14, 15, cal = "Facultad"),
            ),
        )
        assertEquals(listOf("macro", "teoria-juegos", "analisis"), clases.map { it.materia.id })
        assertEquals(listOf("teorica", null, "practica"), clases.map { it.tipoClase })
        assertEquals(listOf(9, 14, 16), clases.map { java.time.Instant.ofEpochMilli(it.evento.inicioMs).atZone(zona).hour })
    }

    @Test
    fun `un evento que coincide con varias materias va con la regla mas especifica`() {
        // "Macro" en el calendario Facultad coincide con macro (regex y calendario) y con medida (sólo calendario).
        val clases = clasesDe(listOf(ev("Macro", 9, 11, cal = "Facultad")))
        assertEquals(listOf("macro"), clases.map { it.materia.id })
    }

    @Test
    fun `un dia sin eventos, o con eventos de otros dias, no tiene clases`() {
        assertTrue(clasesDe(emptyList()).isEmpty())
        val ayer = dia.minusDays(1)
        assertTrue(clasesDe(listOf(ev("Macro", 9, 11, cal = "Facultad", d = ayer))).isEmpty())
        assertEquals(1, clasesDe(listOf(ev("Macro", 9, 11, cal = "Facultad", d = ayer)), d = ayer).size)
    }

    // ---- El día con que arranca ----

    @Test
    fun `arranca en el dia del audio, si no en la fecha del archivo, y si no hoy`() {
        val ahora = ms(20, d = LocalDate.of(2026, 10, 24))
        assertEquals(dia, ElegirClaseImportada.diaInicial(ms(15), ms(10, d = LocalDate.of(2026, 10, 23)), ahora, zona))
        assertEquals(LocalDate.of(2026, 10, 23), ElegirClaseImportada.diaInicial(null, ms(10, d = LocalDate.of(2026, 10, 23)), ahora, zona))
        assertEquals(LocalDate.of(2026, 10, 24), ElegirClaseImportada.diaInicial(null, null, ahora, zona))
        // Una fecha imposible (el cero de QuickTime, o del futuro) no vale.
        assertEquals(LocalDate.of(2026, 10, 24), ElegirClaseImportada.diaInicial(-2_082_844_800_000, ahora + 10L * 24 * 3_600_000, ahora, zona))
    }

    @Test
    fun `la fecha de los metadatos del audio se lee en UTC`() {
        val ms = MetadatosAudio.fechaDeMetadato("20261022T172959.000Z")!!
        assertEquals(LocalDateTime.of(2026, 10, 22, 14, 29, 59), java.time.Instant.ofEpochMilli(ms).atZone(zona).toLocalDateTime())
        assertEquals(ms, MetadatosAudio.fechaDeMetadato("20261022T172959Z"))
        assertNull(MetadatosAudio.fechaDeMetadato("19040101T000000.000Z"))
        assertNull(MetadatosAudio.fechaDeMetadato(""))
        assertNull(MetadatosAudio.fechaDeMetadato("ayer"))
        assertNull(MetadatosAudio.fechaDeMetadato(null))
    }

    // ---- Lo que completa cada elección ----

    @Test
    fun `tocar una clase completa materia, tipo, inicio y el proximo numero`() {
        val clase = clasesDe(listOf(ev("Macro II (Teórica)", 9, 11, cal = "Facultad"))).single()
        val e = ElegirClaseImportada.elegirClase(EleccionImportacion(dia), clase)
        assertEquals("macro", e.materiaId)
        assertEquals(OrigenMateria.CALENDARIO, e.origen)
        assertEquals("teorica", e.tipoClase)
        assertEquals(ms(9), e.inicioMs)
        assertEquals("Macro II (Teórica)", e.eventoCalendario)
        assertEquals(9, e.numero)
        assertTrue(e.completa)

        // El inicio queda editable (por ejemplo, empezó tarde) sin perder la clase elegida.
        val tarde = ElegirClaseImportada.cambiarInicio(e, ms(9, 20), zona)
        assertEquals(ms(9, 20), tarde.inicioMs)
        assertEquals(clase.clave, tarde.clave)
        // Otro día borra la elección del calendario (era de ese día).
        val otroDia = ElegirClaseImportada.cambiarDia(e, dia.plusDays(1), zona)
        assertNull(otroDia.clave)
        assertNull(otroDia.inicioMs)
        assertFalse(otroDia.completa)
    }

    @Test
    fun `sin tipo en el titulo va el primero de la materia`() {
        val clase = clasesDe(listOf(ev("Clase 7", 14, 15, cal = "Facultad"))).single()
        val e = ElegirClaseImportada.elegirClase(EleccionImportacion(dia), clase)
        assertEquals("teoria-juegos", e.materiaId)
        assertEquals("teorica", e.tipoClase)
        // La materia no informa el número: no va (lo pone el server).
        assertNull(e.numero)
    }

    @Test
    fun `a mano, con Auto no hay numero y el inicio sigue al dia elegido`() {
        var e = ElegirClaseImportada.elegirAMano(EleccionImportacion(dia))
        assertTrue(e.aMano)
        assertEquals(OrigenMateria.AUTO, e.origen)
        assertNull(e.materiaId)
        assertFalse("falta la hora", e.completa)

        e = ElegirClaseImportada.elegirMateria(e, "analisis", materias)
        assertEquals(OrigenMateria.MANUAL, e.origen)
        assertEquals(4, e.numero)
        e = ElegirClaseImportada.cambiarNumero(e, 3)
        assertEquals(3, e.numero)

        e = ElegirClaseImportada.cambiarInicio(e, ms(18, 30, d = dia.minusDays(2)), zona)
        assertEquals(dia.minusDays(2), e.dia)
        assertTrue(e.completa)
        // Con ‹ ›, a mano el inicio pasa a ese día con la misma hora.
        e = ElegirClaseImportada.cambiarDia(e, dia, zona)
        assertEquals(ms(18, 30), e.inicioMs)

        e = ElegirClaseImportada.elegirMateria(e, null, materias)
        assertEquals(OrigenMateria.AUTO, e.origen)
        assertNull(e.numero)
        assertNull(ElegirClaseImportada.cambiarNumero(e, 5).numero)
    }

    @Test
    fun `elegir a mano despues de una clase del calendario conserva la materia pero ya no es del calendario`() {
        val clase = clasesDe(listOf(ev("Macro II (Teórica)", 9, 11, cal = "Facultad"))).single()
        val e = ElegirClaseImportada.elegirAMano(ElegirClaseImportada.elegirClase(EleccionImportacion(dia), clase))
        assertEquals("macro", e.materiaId)
        assertEquals(OrigenMateria.MANUAL, e.origen)
        assertNull(e.clave)
        assertNull(e.eventoCalendario)
        assertEquals(ms(9), e.inicioMs)
    }

    // ---- Elegir (u "Elegir otro") el audio después de elegir la clase ----

    @Test
    fun `elegir el audio despues no pisa el inicio puesto a mano`() {
        val martes = dia.minusDays(2)
        var e = ElegirClaseImportada.elegirMateria(ElegirClaseImportada.elegirAMano(EleccionImportacion(dia)), "macro", materias)
        e = ElegirClaseImportada.cambiarInicio(e, ms(18, 30, d = martes), zona)
        // El audio de WhatsApp dice jueves 15:00 (la hora del envío).
        val conAudio = ElegirClaseImportada.alElegirAudio(e, dia, zona)
        assertEquals(e, conAudio)
        assertEquals(ms(18, 30, d = martes), conAudio.inicioMs)
        assertTrue(conAudio.completa)
    }

    @Test
    fun `elegir el audio despues no borra la clase del calendario elegida, sea del dia que sea`() {
        val clase = clasesDe(listOf(ev("Macro II (Teórica)", 9, 11, cal = "Facultad"))).single()
        val e = ElegirClaseImportada.elegirClase(EleccionImportacion(dia), clase)
        for (diaDelAudio in listOf(dia, dia.plusDays(1))) {
            val conAudio = ElegirClaseImportada.alElegirAudio(e, diaDelAudio, zona)
            assertEquals(clase.clave, conAudio.clave)
            assertEquals(ms(9), conAudio.inicioMs)
            assertTrue(conAudio.completa)
        }
        // Y volver al mismo día con ‹ › (o el selector de fecha) tampoco la borra.
        assertEquals(e, ElegirClaseImportada.cambiarDia(e, dia, zona))
    }

    @Test
    fun `sin nada elegido, la lista pasa al dia del audio`() {
        val e = ElegirClaseImportada.alElegirAudio(EleccionImportacion(dia), dia.minusDays(5), zona)
        assertEquals(dia.minusDays(5), e.dia)
        assertFalse(e.elegida)
    }

    // ---- El número de las importadas que todavía no llegaron al server ----

    @Test
    fun `cada importada sin crear en el server corre el numero de su materia`() {
        val r = SeleccionMateria.contarPendientes(materias, listOf("macro", "macro", "analisis", "no-existe"))
        assertEquals(listOf(11, 5, null), r.map { it.proximoNumero })
        assertEquals(materias, SeleccionMateria.contarPendientes(materias, emptyList()))
    }
}
