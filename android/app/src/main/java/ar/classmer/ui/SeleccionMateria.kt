package ar.classmer.ui

import ar.classmer.calendario.Sugerencia
import ar.classmer.modelo.Materia
import ar.classmer.modelo.OrigenMateria
import ar.classmer.modelo.TIPOS_CLASE_POR_DEFECTO

data class Seleccion(
    /** `null` = Auto (IA): el server clasifica la materia. */
    val materiaId: String? = null,
    val origen: String = OrigenMateria.AUTO,
    val tipoClase: String = TIPOS_CLASE_POR_DEFECTO.first(),
    val eventoCalendario: String? = null,
    /** Fin del evento del calendario (ms desde 1970) si la materia salió de ahí: para el corte automático. */
    val finEventoMs: Long? = null,
    /** La materia (o Auto) la eligió el usuario: el calendario no la cambia. */
    val materiaAMano: Boolean = false,
    /** El tipo lo eligió el usuario: el calendario no lo cambia. */
    val tipoAMano: Boolean = false,
    /**
     * Qué evento sugería el calendario cuando se eligió a mano. Si después sugiere otro (empezó otra
     * clase), lo elegido a mano vence y se vuelve a autocompletar.
     */
    val claveEvento: String? = null,
)

/** Cómo se combina lo que sugiere el calendario con lo que elige el usuario en Inicio. */
object SeleccionMateria {

    fun clave(s: Sugerencia?): String? = s?.let { "${it.evento.titulo}|${it.evento.inicioMs}" }

    fun tiposDe(materias: List<Materia>, materiaId: String?): List<String> =
        materias.firstOrNull { it.id == materiaId }?.tiposClase?.ifEmpty { null } ?: TIPOS_CLASE_POR_DEFECTO

    fun aplicarCalendario(actual: Seleccion, sugerencia: Sugerencia?, materias: List<Materia>): Seleccion {
        val clave = clave(sugerencia)
        // Una materia que el server ya no manda (por ejemplo, la desactivaron) no se puede seguir eligiendo.
        val desaparecio = actual.materiaId != null && materias.none { it.id == actual.materiaId }
        val vencida = (actual.materiaAMano || actual.tipoAMano) && actual.claveEvento != clave
        val vigente = if (vencida || desaparecio) Seleccion() else actual
        val conMateria = when {
            vigente.materiaAMano -> vigente
            sugerencia != null -> vigente.copy(
                materiaId = sugerencia.materia.id,
                origen = OrigenMateria.CALENDARIO,
                eventoCalendario = sugerencia.evento.titulo,
                finEventoMs = sugerencia.evento.finMs,
            )
            else -> vigente.copy(materiaId = null, origen = OrigenMateria.AUTO, eventoCalendario = null, finEventoMs = null)
        }
        val tipos = tiposDe(materias, conMateria.materiaId)
        val tipo = when {
            conMateria.tipoAMano && conMateria.tipoClase in tipos -> conMateria.tipoClase
            !conMateria.tipoAMano && sugerencia != null && conMateria.materiaId == sugerencia.materia.id ->
                sugerencia.tipoClase?.takeIf { it in tipos } ?: tipos.first()
            conMateria.tipoClase in tipos -> conMateria.tipoClase
            else -> tipos.first()
        }
        return conMateria.copy(tipoClase = tipo)
    }

    /** `id == null` = Auto (IA). */
    fun elegirMateria(actual: Seleccion, id: String?, materias: List<Materia>, sugerencia: Sugerencia?): Seleccion {
        val materia = materias.firstOrNull { it.id == id }
        val tipos = tiposDe(materias, materia?.id)
        val desdeCalendario = materia != null && sugerencia != null && sugerencia.materia.id == materia.id
        return actual.copy(
            materiaId = materia?.id,
            origen = when {
                materia == null -> OrigenMateria.AUTO
                desdeCalendario -> OrigenMateria.CALENDARIO
                else -> OrigenMateria.MANUAL
            },
            tipoClase = if (actual.tipoClase in tipos) actual.tipoClase else tipos.first(),
            eventoCalendario = if (desdeCalendario) sugerencia?.evento?.titulo else null,
            finEventoMs = if (desdeCalendario) sugerencia?.evento?.finMs else null,
            materiaAMano = true,
            claveEvento = clave(sugerencia),
        )
    }

    fun elegirTipo(actual: Seleccion, tipo: String, sugerencia: Sugerencia?): Seleccion =
        actual.copy(tipoClase = tipo, tipoAMano = true, claveEvento = clave(sugerencia))

    /** El número con el que se graba la próxima clase: `null` en Auto o si el server no lo informa. */
    fun numeroClase(seleccion: Seleccion, materias: List<Materia>): Int? =
        seleccion.materiaId?.let { id -> materias.firstOrNull { it.id == id }?.proximoNumero }?.takeIf { it > 0 }

    /**
     * Después de empezar a grabar una clase de [materiaId], el server cuenta una clase más. Se suma
     * en la cache hasta el próximo refresco, así dos clases seguidas sin conexión no repiten número.
     */
    fun contarClase(materias: List<Materia>, materiaId: String?): List<Materia> =
        materias.map { m -> if (m.id == materiaId && m.proximoNumero != null) m.copy(proximoNumero = m.proximoNumero + 1) else m }

    /**
     * Las clases que el celu ya numeró y el server todavía no cuenta (una importada que se está
     * copiando): cada una corre el próximo número de su materia, como [contarClase].
     */
    fun contarPendientes(materias: List<Materia>, materiaIds: List<String>): List<Materia> {
        if (materiaIds.isEmpty()) return materias
        val porMateria = materiaIds.groupingBy { it }.eachCount()
        return materias.map { m ->
            val n = porMateria[m.id] ?: 0
            if (n > 0 && m.proximoNumero != null) m.copy(proximoNumero = m.proximoNumero + n) else m
        }
    }

    /** Reemplaza una materia por la que devolvió el server, sin cambiar el orden. */
    fun reemplazar(materias: List<Materia>, nueva: Materia): List<Materia> =
        materias.map { if (it.id == nueva.id) nueva else it }
}
