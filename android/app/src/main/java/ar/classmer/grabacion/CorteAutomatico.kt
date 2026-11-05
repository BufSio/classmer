package ar.classmer.grabacion

/**
 * Corte automático (ARCHITECTURE.md §4): por si la grabación quedó prendida después de la clase.
 *
 * - Si la materia vino del calendario, se pregunta "¿Sigue la clase?" al pasar el fin del evento
 *   + 15 min. Si el evento ya había terminado cuando se empezó a grabar, su horario no dice nada
 *   del fin de esta clase y se usa la regla sin calendario.
 * - Sin calendario, se pregunta a las 3 h 30 min de empezada la grabación.
 * - Sin respuesta en 10 min, la grabación termina sola. "Seguir" vuelve a preguntar 30 min después.
 *
 * Sólo decide; el servicio de grabación muestra la pregunta y termina la clase.
 */
object CorteAutomatico {
    const val MARGEN_FIN_EVENTO_MS = 15 * 60_000L
    const val SIN_CALENDARIO_MS = (3 * 60 + 30) * 60_000L
    const val ESPERA_RESPUESTA_MS = 10 * 60_000L
    const val VOLVER_A_PREGUNTAR_MS = 30 * 60_000L

    enum class Accion { NADA, PREGUNTAR, TERMINAR }

    /**
     * Cuándo preguntar por primera vez (ms desde 1970).
     * @param finEventoMs fin del evento del calendario si la materia salió de ahí; `null` si no.
     */
    fun primeraPregunta(inicioMs: Long, finEventoMs: Long?): Long =
        if (finEventoMs != null && finEventoMs > inicioMs) {
            finEventoMs + MARGEN_FIN_EVENTO_MS
        } else {
            inicioMs + SIN_CALENDARIO_MS
        }

    /**
     * @param preguntarEnMs cuándo toca preguntar (hora del celu); `null` = nunca.
     * @param ahoraMs hora del celu.
     * @param preguntaAbiertaMs hace cuánto se está preguntando (reloj monotónico); `null` = no hay pregunta abierta.
     * @param esperaMs cuánto se espera la respuesta (sólo cambia en las pruebas en el celu).
     */
    fun evaluar(
        preguntarEnMs: Long?,
        ahoraMs: Long,
        preguntaAbiertaMs: Long?,
        esperaMs: Long = ESPERA_RESPUESTA_MS,
    ): Accion = when {
        preguntarEnMs == null -> Accion.NADA
        preguntaAbiertaMs != null -> if (preguntaAbiertaMs >= esperaMs) Accion.TERMINAR else Accion.NADA
        ahoraMs >= preguntarEnMs -> Accion.PREGUNTAR
        else -> Accion.NADA
    }

    /** "Seguir": la próxima pregunta (ms desde 1970). */
    fun seguir(ahoraMs: Long, volverMs: Long = VOLVER_A_PREGUNTAR_MS): Long = ahoraMs + volverMs
}
