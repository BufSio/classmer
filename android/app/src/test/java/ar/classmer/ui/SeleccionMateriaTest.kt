package ar.classmer.ui

import ar.classmer.calendario.EventoCalendario
import ar.classmer.calendario.Sugerencia
import ar.classmer.modelo.Materia
import ar.classmer.modelo.OrigenMateria
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SeleccionMateriaTest {
    private val analisis = Materia("analisis", "Análisis Matemático")
    private val macro = Materia("macro", "Macro")
    private val materias = listOf(analisis, macro)

    private fun sug(m: Materia, titulo: String, inicio: Long, tipo: String? = null) =
        Sugerencia(m, EventoCalendario(titulo, inicio, inicio + 90 * 60_000, "Facultad"), tipo)

    private val sugAnalisis = sug(analisis, "Análisis Matemático", 1_000)
    private val sugMacro = sug(macro, "Macro - Práctica", 9_000, "practica")

    @Test
    fun `sin eleccion a mano sigue al calendario`() {
        val s = SeleccionMateria.aplicarCalendario(Seleccion(), sugMacro, materias)
        assertEquals("macro", s.materiaId)
        assertEquals(OrigenMateria.CALENDARIO, s.origen)
        assertEquals("practica", s.tipoClase)
        val sinEvento = SeleccionMateria.aplicarCalendario(s, null, materias)
        assertNull(sinEvento.materiaId)
        assertEquals(OrigenMateria.AUTO, sinEvento.origen)
    }

    @Test
    fun `tocar el tipo no congela la materia del calendario`() {
        var s = SeleccionMateria.aplicarCalendario(Seleccion(), sugAnalisis, materias)
        s = SeleccionMateria.elegirTipo(s, "practica", sugAnalisis)
        // Mismo evento: se respeta el tipo elegido.
        s = SeleccionMateria.aplicarCalendario(s, sugAnalisis, materias)
        assertEquals("analisis", s.materiaId)
        assertEquals("practica", s.tipoClase)
        // Empieza otra clase: lo elegido a mano vence y todo sigue al calendario.
        s = SeleccionMateria.aplicarCalendario(s, sugMacro, materias)
        assertEquals("macro", s.materiaId)
        assertEquals(OrigenMateria.CALENDARIO, s.origen)
        assertEquals("practica", s.tipoClase)
    }

    @Test
    fun `elegir Auto a mano se respeta mientras el calendario sugiera el mismo evento`() {
        var s = SeleccionMateria.aplicarCalendario(Seleccion(), sugAnalisis, materias)
        s = SeleccionMateria.elegirMateria(s, null, materias, sugAnalisis)
        s = SeleccionMateria.aplicarCalendario(s, sugAnalisis, materias)
        assertNull(s.materiaId)
        assertEquals(OrigenMateria.AUTO, s.origen)
        assertTrue(s.materiaAMano)
        s = SeleccionMateria.aplicarCalendario(s, sugMacro, materias)
        assertEquals("macro", s.materiaId)
    }

    @Test
    fun `elegir a mano otra materia es manual y elegir la sugerida es calendario`() {
        val base = SeleccionMateria.aplicarCalendario(Seleccion(), sugAnalisis, materias)
        assertEquals(OrigenMateria.MANUAL, SeleccionMateria.elegirMateria(base, "macro", materias, sugAnalisis).origen)
        val misma = SeleccionMateria.elegirMateria(base, "analisis", materias, sugAnalisis)
        assertEquals(OrigenMateria.CALENDARIO, misma.origen)
        assertEquals("Análisis Matemático", misma.eventoCalendario)
    }

    // ---- Fin del evento (corte automático) ----

    @Test
    fun `el fin del evento viene de la sugerencia del calendario`() {
        val s = SeleccionMateria.aplicarCalendario(Seleccion(), sugMacro, materias)
        assertEquals(sugMacro.evento.finMs, s.finEventoMs)
        assertEquals(9_000 + 90 * 60_000L, s.finEventoMs)
        // Empieza otra clase: el fin es el del evento nuevo.
        val otra = SeleccionMateria.aplicarCalendario(s, sugAnalisis, materias)
        assertEquals(sugAnalisis.evento.finMs, otra.finEventoMs)
    }

    @Test
    fun `en Auto no hay fin de evento`() {
        val s = SeleccionMateria.aplicarCalendario(Seleccion(), sugMacro, materias)
        // El calendario deja de sugerir: Auto.
        val sinEvento = SeleccionMateria.aplicarCalendario(s, null, materias)
        assertNull(sinEvento.finEventoMs)
        assertNull(sinEvento.eventoCalendario)
        // Auto elegido a mano con el evento todavía sugerido.
        val auto = SeleccionMateria.elegirMateria(s, null, materias, sugMacro)
        assertNull(auto.finEventoMs)
        assertNull(SeleccionMateria.aplicarCalendario(auto, sugMacro, materias).finEventoMs)
    }

    @Test
    fun `elegir a mano la sugerida conserva el fin del evento y otra materia lo borra`() {
        val base = SeleccionMateria.aplicarCalendario(Seleccion(), sugAnalisis, materias)
        val misma = SeleccionMateria.elegirMateria(base, "analisis", materias, sugAnalisis)
        assertEquals(sugAnalisis.evento.finMs, misma.finEventoMs)
        val otra = SeleccionMateria.elegirMateria(base, "macro", materias, sugAnalisis)
        assertEquals(OrigenMateria.MANUAL, otra.origen)
        assertNull(otra.finEventoMs)
        // Mientras el calendario siga sugiriendo el mismo evento, la manual sigue sin fin de evento.
        assertNull(SeleccionMateria.aplicarCalendario(otra, sugAnalisis, materias).finEventoMs)
        // Volver a la sugerida lo recupera.
        val vuelta = SeleccionMateria.elegirMateria(otra, "analisis", materias, sugAnalisis)
        assertEquals(sugAnalisis.evento.finMs, vuelta.finEventoMs)
        // Sin sugerencia del calendario, ninguna materia elegida a mano tiene fin de evento.
        assertNull(SeleccionMateria.elegirMateria(Seleccion(), "analisis", materias, null).finEventoMs)
    }

    // ---- Materias que desaparecen (inactivas) ----

    @Test
    fun `una materia elegida a mano que desaparece vuelve al calendario`() {
        var s = SeleccionMateria.aplicarCalendario(Seleccion(), sugAnalisis, materias)
        s = SeleccionMateria.elegirMateria(s, "macro", materias, sugAnalisis)
        s = SeleccionMateria.elegirTipo(s, "practica", sugAnalisis)
        assertEquals("macro", s.materiaId)
        // El server deja de mandar Macro (la desactivaron), con el mismo evento en el calendario.
        s = SeleccionMateria.aplicarCalendario(s, sugAnalisis, listOf(analisis))
        assertEquals("analisis", s.materiaId)
        assertEquals(OrigenMateria.CALENDARIO, s.origen)
        assertEquals(sugAnalisis.evento.finMs, s.finEventoMs)
        assertFalse(s.materiaAMano)
        assertFalse(s.tipoAMano)
        assertEquals("teorica", s.tipoClase)
    }

    @Test
    fun `una materia elegida a mano que desaparece sin calendario vuelve a Auto`() {
        var s = SeleccionMateria.elegirMateria(Seleccion(), "macro", materias, null)
        assertEquals("macro", s.materiaId)
        assertEquals(OrigenMateria.MANUAL, s.origen)
        // Mientras siga en la lista, se respeta.
        assertEquals("macro", SeleccionMateria.aplicarCalendario(s, null, materias).materiaId)
        s = SeleccionMateria.aplicarCalendario(s, null, listOf(analisis))
        assertNull(s.materiaId)
        assertEquals(OrigenMateria.AUTO, s.origen)
        assertFalse(s.materiaAMano)
        assertNull(s.finEventoMs)
    }

    @Test
    fun `Auto elegido a mano no cuenta como materia desaparecida`() {
        var s = SeleccionMateria.aplicarCalendario(Seleccion(), sugAnalisis, materias)
        s = SeleccionMateria.elegirMateria(s, null, materias, sugAnalisis)
        s = SeleccionMateria.aplicarCalendario(s, sugAnalisis, listOf(analisis))
        assertNull(s.materiaId)
        assertTrue(s.materiaAMano)
    }

    // ---- Número de clase ----

    @Test
    fun `numeroClase es el proximo numero de la materia elegida`() {
        val conNumero = listOf(analisis.copy(proximoNumero = 5), macro, Materia("riesgo", "Riesgo", proximoNumero = 0))
        assertNull(SeleccionMateria.numeroClase(Seleccion(), conNumero))
        assertNull(SeleccionMateria.numeroClase(Seleccion(materiaId = "macro"), conNumero))
        assertEquals(5, SeleccionMateria.numeroClase(Seleccion(materiaId = "analisis"), conNumero))
        // Un número que no sirve o una materia que ya no está tampoco se mandan.
        assertNull(SeleccionMateria.numeroClase(Seleccion(materiaId = "riesgo"), conNumero))
        assertNull(SeleccionMateria.numeroClase(Seleccion(materiaId = "no-existe"), conNumero))
    }

    @Test
    fun `contarClase suma uno solo a esa materia y no toca las que no tienen numero`() {
        val riesgo = Materia("riesgo", "Riesgo", proximoNumero = 2)
        val lista = listOf(analisis.copy(proximoNumero = 5), macro, riesgo)
        val contada = SeleccionMateria.contarClase(lista, "analisis")
        assertEquals(listOf(6, null, 2), contada.map { it.proximoNumero })
        assertEquals(lista.map { it.id }, contada.map { it.id })
        // Una materia sin número sigue sin número.
        assertEquals(lista, SeleccionMateria.contarClase(lista, "macro"))
        // Auto o una materia que no está: no cambia nada.
        assertEquals(lista, SeleccionMateria.contarClase(lista, null))
        assertEquals(lista, SeleccionMateria.contarClase(lista, "no-existe"))
        // Dos clases seguidas sin conexión no repiten número.
        assertEquals(7, SeleccionMateria.contarClase(contada, "analisis").first().proximoNumero)
    }

    @Test
    fun `reemplazar cambia la materia y conserva el orden`() {
        val riesgo = Materia("riesgo", "Riesgo", proximoNumero = 2)
        val lista = listOf(analisis, macro, riesgo)
        val nueva = macro.copy(nombre = "Macroeconomía", proximoNumero = 11)
        val r = SeleccionMateria.reemplazar(lista, nueva)
        assertEquals(listOf("analisis", "macro", "riesgo"), r.map { it.id })
        assertEquals(nueva, r[1])
        assertEquals(analisis, r[0])
        assertEquals(riesgo, r[2])
        // Una materia que no estaba no se agrega.
        assertEquals(lista, SeleccionMateria.reemplazar(lista, Materia("nueva", "Nueva", proximoNumero = 1)))
    }
}
