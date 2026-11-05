package ar.classmer.wear

import android.os.SystemClock

/**
 * Mide el audio grabado en el reloj: corre mientras graba y se frena en las pausas (como el reloj de
 * la grabación del celu). [desde] y [acumuladoMs] son justo lo que va en el estado que se le manda
 * al celu, que con el desfase entre relojes calcula el offset de sus fotos y marcas.
 */
class Cronometro(
    private val ahora: () -> Long = SystemClock::elapsedRealtime,
    acumuladoInicialMs: Long = 0,
) {
    @get:Synchronized
    var acumuladoMs: Long = acumuladoInicialMs
        private set

    /** `elapsedRealtime` desde el que corre; `null` en pausa. */
    @get:Synchronized
    var desde: Long? = null
        private set

    @Synchronized
    fun iniciar() {
        if (desde == null) desde = ahora()
    }

    /** Frena el cronómetro y devuelve el total grabado. */
    @Synchronized
    fun pausar(): Long {
        desde?.let { acumuladoMs += ahora() - it }
        desde = null
        return acumuladoMs
    }

    @Synchronized
    fun offsetMs(): Long = acumuladoMs + (desde?.let { ahora() - it } ?: 0)
}
