package ar.classmer.grabacion

import android.os.SystemClock

/**
 * Mide el tiempo de audio grabado: corre mientras se graba y se frena en las pausas. Es la base
 * de `offset_ms` (ARCHITECTURE.md §4): no depende de la hora del sistema.
 *
 * Usa `elapsedRealtime`, que es monotónico y sigue contando con el celu bloqueado.
 */
class RelojGrabacion(
    private val ahora: () -> Long = SystemClock::elapsedRealtime,
    acumuladoInicialMs: Long = 0,
) {
    private var acumuladoMs = acumuladoInicialMs
    private var desde: Long? = null

    val corriendo: Boolean @Synchronized get() = desde != null

    @Synchronized
    fun iniciar() {
        if (desde == null) desde = ahora()
    }

    /** Frena el reloj y devuelve el total grabado. */
    @Synchronized
    fun pausar(): Long {
        desde?.let { acumuladoMs += ahora() - it }
        desde = null
        return acumuladoMs
    }

    @Synchronized
    fun offsetMs(): Long = acumuladoMs + (desde?.let { ahora() - it } ?: 0)
}
