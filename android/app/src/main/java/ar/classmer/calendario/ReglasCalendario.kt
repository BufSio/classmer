package ar.classmer.calendario

import ar.classmer.modelo.Materia
import ar.classmer.modelo.ReglaCalendario
import java.text.Normalizer
import java.util.regex.Pattern
import java.util.regex.PatternSyntaxException

data class EventoCalendario(
    val titulo: String,
    val inicioMs: Long,
    val finMs: Long,
    /** Nombre visible del calendario en el celu. */
    val calendario: String,
    val todoElDia: Boolean = false,
)

data class Sugerencia(val materia: Materia, val evento: EventoCalendario, val tipoClase: String?)

/** Una clase del calendario de un día ("Importar clase"): el evento, su materia y el tipo según el título. */
data class ClaseDelCalendario(val evento: EventoCalendario, val materia: Materia, val tipoClase: String?) {
    /** Para recordar cuál se eligió (sobrevive a que se vuelva a leer el calendario). */
    val clave: String get() = "${evento.inicioMs}|${evento.titulo}|${materia.id}"
}

/**
 * Elige la materia según el evento del calendario que está ocurriendo (ARCHITECTURE.md §4).
 *
 * Una regla coincide si se cumplen todas sus partes presentes:
 * - `calendario`: el nombre del calendario del evento (sin distinguir mayúsculas).
 * - `titulo_regex`: se busca en el título (no hace falta que coincida entero), sin distinguir
 *   mayúsculas, como `re.search(..., re.IGNORECASE)` de Python.
 * Si un evento coincide con varias materias, gana la regla más específica (ARCHITECTURE.md §5.1).
 * Una regla sin ninguna de las dos no coincide nunca (la materia no se autocompleta).
 */
object ReglasCalendario {
    const val MARGEN_MS = 15 * 60 * 1000L

    fun coincide(regla: ReglaCalendario?, evento: EventoCalendario): Boolean {
        if (regla == null) return false
        val calendario = regla.calendario?.takeIf { it.isNotBlank() }
        val regex = regla.tituloRegex?.takeIf { it.isNotBlank() }
        if (calendario == null && regex == null) return false
        if (calendario != null && !evento.calendario.trim().equals(calendario.trim(), ignoreCase = true)) return false
        if (regex != null) {
            val r = compilar(regex) ?: return false
            if (!r.containsMatchIn(evento.titulo)) return false
        }
        return true
    }

    /**
     * Contrato v1: búsqueda sin distinguir mayúsculas, con tildes incluidas (Á = á) y clases Unicode
     * (`\w`, `\b`), como `re` de Python. Una regex inválida da `null` (la regla no coincide).
     *
     * Android (ICU) no acepta UNICODE_CHARACTER_CLASS: `Pattern.compile` tira IllegalArgumentException y
     * ninguna regla coincidía nunca en el celu (ni al grabar ni en "Importar clase"), aunque los tests
     * de la JVM pasaban. Ahí se compila sin esa bandera: en ICU `\w` y `\b` ya son Unicode.
     */
    fun compilar(regex: String, compilarPattern: (String, Int) -> Pattern = Pattern::compile): Regex? {
        val base = Pattern.CASE_INSENSITIVE or Pattern.UNICODE_CASE
        return runCatching { compilarPattern(regex, base or Pattern.UNICODE_CHARACTER_CLASS) }
            .recoverCatching { e ->
                if (e is IllegalArgumentException && e !is PatternSyntaxException) compilarPattern(regex, base) else throw e
            }
            .getOrNull()?.toRegex()
    }

    /** Si un evento coincide con varias materias, gana la regla más específica (regex > calendario). */
    private fun especificidad(regla: ReglaCalendario?): Int =
        (if (!regla?.tituloRegex.isNullOrBlank()) 2 else 0) + (if (!regla?.calendario.isNullOrBlank()) 1 else 0)

    /**
     * El evento "en curso" con margen de ±15 min: empezado y no terminado, o que empieza dentro de
     * 15 min, o que terminó hace menos de 15 min. Prioridad: en curso (el que empezó último), después
     * el que empieza antes, después el que terminó más recién.
     */
    fun sugerir(materias: List<Materia>, eventos: List<EventoCalendario>, ahoraMs: Long): Sugerencia? {
        return eventos.asSequence()
            .filter { !it.todoElDia && it.inicioMs <= ahoraMs + MARGEN_MS && it.finMs >= ahoraMs - MARGEN_MS }
            .flatMap { ev -> materias.asSequence().filter { coincide(it.calendario, ev) }.map { ev to it } }
            .minWithOrNull(
                compareBy(
                    { prioridad(it.first, ahoraMs) },
                    { distancia(it.first, ahoraMs) },
                    { -especificidad(it.second.calendario) },
                ),
            )
            ?.let { (ev, materia) -> Sugerencia(materia, ev, tipoDesdeTitulo(ev.titulo, materia.tiposClase)) }
    }

    /**
     * Las clases de un día: los eventos que empiezan en `[desdeMs, hastaMs)`, sin los de todo el día,
     * que coinciden con alguna materia (si coinciden con varias, la regla más específica, como en
     * [sugerir]). Ordenadas por hora de inicio.
     */
    fun clasesDelDia(materias: List<Materia>, eventos: List<EventoCalendario>, desdeMs: Long, hastaMs: Long): List<ClaseDelCalendario> =
        eventos.asSequence()
            .filter { !it.todoElDia && it.inicioMs >= desdeMs && it.inicioMs < hastaMs }
            .mapNotNull { ev ->
                val materia = materias.filter { coincide(it.calendario, ev) }.maxByOrNull { especificidad(it.calendario) }
                    ?: return@mapNotNull null
                ClaseDelCalendario(ev, materia, tipoDesdeTitulo(ev.titulo, materia.tiposClase))
            }
            .distinctBy { it.clave }
            .sortedWith(compareBy({ it.evento.inicioMs }, { it.evento.finMs }, { it.evento.titulo }))
            .toList()

    /**
     * En curso (y no por terminar) > por empezar > en curso pero terminando en menos de 15 min > ya
     * terminado. Así, con clases seguidas, a las 15:25 gana la de las 15:30 y no la que termina.
     */
    private fun prioridad(ev: EventoCalendario, ahora: Long) = when {
        ev.inicioMs <= ahora && ahora < ev.finMs && ev.finMs - ahora > MARGEN_MS -> 0
        ev.inicioMs > ahora -> 1
        ev.inicioMs <= ahora && ahora < ev.finMs -> 2
        else -> 3
    }

    private fun distancia(ev: EventoCalendario, ahora: Long) = when {
        ev.inicioMs <= ahora && ahora < ev.finMs -> ahora - ev.inicioMs
        ev.inicioMs > ahora -> ev.inicioMs - ahora
        else -> ahora - ev.finMs
    }

    /**
     * El tipo de clase sale del título: "Práctica de Análisis Matemático" -> `practica`. Compara sin tildes
     * ni mayúsculas y acepta masculino/femenino ("Teórico"/"Teórica").
     */
    fun tipoDesdeTitulo(titulo: String, tipos: List<String>): String? {
        val t = normalizar(titulo)
        return tipos.firstOrNull { tipo ->
            val n = normalizar(tipo)
            val raiz = if (n.length > 4 && n.last() in "aeo") n.dropLast(1) else n
            raiz.isNotEmpty() && t.contains(raiz)
        }
    }

    fun normalizar(texto: String): String =
        Normalizer.normalize(texto, Normalizer.Form.NFD)
            .replace(Regex("\\p{M}+"), "")
            .lowercase()
}
