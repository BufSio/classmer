package ar.classmer.calendario

import ar.classmer.modelo.Materia
import ar.classmer.modelo.ReglaCalendario
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.regex.Pattern

class ReglasCalendarioTest {
    private val min = 60_000L
    private val ahora = 1_000_000_000_000L

    private val analisis = Materia("analisis", "Análisis Matemático II", ReglaCalendario(tituloRegex = "^(?!Clase particular).*An[aá]lisis Matem[aá]tico"))
    private val medida = Materia("teoria-juegos", "Teoría de Juegos", ReglaCalendario(calendario = "Facultad"))
    private val macro = Materia("macro", "Macro", ReglaCalendario(calendario = "Facultad", tituloRegex = "Macro"))
    private val sinRegla = Materia("riesgo", "Riesgo", null)
    private val materias = listOf(analisis, medida, macro, sinRegla)

    private fun ev(titulo: String, desdeMin: Long, hastaMin: Long, cal: String = "Personal", todoElDia: Boolean = false) =
        EventoCalendario(titulo, ahora + desdeMin * min, ahora + hastaMin * min, cal, todoElDia)

    @Test
    fun `la regex se busca en el titulo sin distinguir mayusculas ni en letras con tilde`() {
        assertTrue(ReglasCalendario.coincide(analisis.calendario, ev("Teórica Análisis Matemático", -5, 60)))
        assertTrue(ReglasCalendario.coincide(analisis.calendario, ev("Analisis Matematico - práctica", -5, 60)))
        assertTrue(ReglasCalendario.coincide(analisis.calendario, ev("análisis matemático", -5, 60)))
        assertTrue(ReglasCalendario.coincide(ReglaCalendario(tituloRegex = "análisis matemático"), ev("ANÁLISIS MATEMÁTICO", 0, 1)))
        assertFalse(ReglasCalendario.coincide(analisis.calendario, ev("Álgebra", -5, 60)))
    }

    @Test
    fun `las clases particulares quedan excluidas por la regla`() {
        val particular = ev("Clase particular - Ana (Análisis Matemático)", -5, 55)
        assertFalse(ReglasCalendario.coincide(analisis.calendario, particular))
        assertNull(ReglasCalendario.sugerir(materias, listOf(particular), ahora))
    }

    @Test
    fun `calendario dedicado sin importar mayusculas y las dos condiciones juntas`() {
        assertTrue(ReglasCalendario.coincide(medida.calendario, ev("Clase 7", 0, 90, cal = "facultad")))
        assertFalse(ReglasCalendario.coincide(medida.calendario, ev("Clase 7", 0, 90, cal = "Personal")))
        assertTrue(ReglasCalendario.coincide(macro.calendario, ev("Macro II", 0, 90, cal = "Facultad")))
        assertFalse(ReglasCalendario.coincide(macro.calendario, ev("Macro II", 0, 90, cal = "Personal")))
    }

    @Test
    fun `regla vacia, nula o con regex invalida no coincide`() {
        assertFalse(ReglasCalendario.coincide(null, ev("Riesgo", 0, 90)))
        assertFalse(ReglasCalendario.coincide(ReglaCalendario(), ev("Riesgo", 0, 90)))
        assertFalse(ReglasCalendario.coincide(ReglaCalendario(calendario = " ", tituloRegex = ""), ev("Riesgo", 0, 90)))
        assertFalse(ReglasCalendario.coincide(ReglaCalendario(tituloRegex = "(sin cerrar"), ev("(sin cerrar", 0, 90)))
    }

    /** Como el Pattern.compile de Android (ICU): UNICODE_CHARACTER_CLASS no está soportada. */
    private val compilarComoAndroid: (String, Int) -> Pattern = { regex, banderas ->
        require(banderas and Pattern.UNICODE_CHARACTER_CLASS == 0) { "Unsupported flags: ${Pattern.UNICODE_CHARACTER_CLASS}" }
        Pattern.compile(regex, banderas)
    }

    @Test
    fun `sin UNICODE_CHARACTER_CLASS, como en Android, las reglas del usuario igual coinciden`() {
        // Reglas como las de una config real, contra títulos como los del calendario del celu.
        val historia = ReglasCalendario.compilar("^Historia\\b", compilarComoAndroid)!!
        assertTrue(historia.containsMatchIn("Historia · Teórica"))
        assertFalse(historia.containsMatchIn("Historiadores invitados"))
        assertTrue(ReglasCalendario.compilar("^An[aá]lisis Matem[aá]tico\\b", compilarComoAndroid)!!.containsMatchIn("Análisis Matemático · Teórica"))
        assertTrue(ReglasCalendario.compilar("^RF\\b", compilarComoAndroid)!!.containsMatchIn("rf · Práctica"))
        assertFalse(ReglasCalendario.compilar("^RF\\b", compilarComoAndroid)!!.containsMatchIn("PARCIAL · RF"))
        assertTrue(ReglasCalendario.compilar("análisis matemático", compilarComoAndroid)!!.containsMatchIn("ANÁLISIS MATEMÁTICO"))
        // Una regex inválida sigue sin coincidir (no se confunde con la bandera que falta).
        assertNull(ReglasCalendario.compilar("(sin cerrar", compilarComoAndroid))
    }

    @Test
    fun `margen de 15 minutos antes y despues`() {
        assertEquals(analisis, ReglasCalendario.sugerir(materias, listOf(ev("Análisis Matemático", 14, 104)), ahora)?.materia)
        assertNull(ReglasCalendario.sugerir(materias, listOf(ev("Análisis Matemático", 16, 106)), ahora))
        assertEquals(analisis, ReglasCalendario.sugerir(materias, listOf(ev("Análisis Matemático", -100, -14)), ahora)?.materia)
        assertNull(ReglasCalendario.sugerir(materias, listOf(ev("Análisis Matemático", -100, -16)), ahora))
        assertNull(ReglasCalendario.sugerir(materias, listOf(ev("Análisis Matemático", -60, 600, todoElDia = true)), ahora))
    }

    @Test
    fun `prefiere el evento en curso sobre el que empieza o el que termino`() {
        val termino = ev("Análisis Matemático", -100, -5)
        val enCurso = ev("Macro", -3, 90, cal = "Facultad")
        val empieza = ev("Análisis Matemático", 10, 100)
        val s = ReglasCalendario.sugerir(materias, listOf(termino, empieza, enCurso), ahora)
        assertEquals(macro, s?.materia)
        assertEquals(analisis, ReglasCalendario.sugerir(materias, listOf(termino, empieza), ahora)?.materia)
        assertEquals(empieza, ReglasCalendario.sugerir(materias, listOf(termino, empieza), ahora)?.evento)
    }

    @Test
    fun `con clases seguidas gana la que empieza si la otra esta por terminar`() {
        val terminando = ev("Juegos", -85, 5, cal = "Facultad") // termina en 5 min
        val empieza = ev("Análisis Matemático", 5, 95)
        assertEquals(analisis, ReglasCalendario.sugerir(materias, listOf(terminando, empieza), ahora)?.materia)
        // Si todavía le falta más de 15 min, gana la que está en curso.
        val enCurso = ev("Juegos", -60, 30, cal = "Facultad")
        assertEquals(medida, ReglasCalendario.sugerir(materias, listOf(enCurso, empieza), ahora)?.materia)
    }

    @Test
    fun `el tipo de clase sale del titulo`() {
        val tipos = listOf("teorica", "practica")
        assertEquals("practica", ReglasCalendario.tipoDesdeTitulo("Práctica Análisis Matemático", tipos))
        assertEquals("teorica", ReglasCalendario.tipoDesdeTitulo("Análisis Matemático (Teórico)", tipos))
        assertEquals("teorica", ReglasCalendario.tipoDesdeTitulo("TEORICA", tipos))
        assertNull(ReglasCalendario.tipoDesdeTitulo("Análisis Matemático", tipos))
        assertEquals(
            "practica",
            ReglasCalendario.sugerir(materias, listOf(ev("Análisis Matemático - Práctica", -1, 60)), ahora)?.tipoClase,
        )
    }
}
