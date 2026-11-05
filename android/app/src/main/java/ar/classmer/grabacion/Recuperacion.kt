package ar.classmer.grabacion

import android.util.Log
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.SesionLocal
import ar.classmer.util.Tiempo

/**
 * Cierra las sesiones que figuran "grabando" pero que ya no se están grabando: la app se cayó o la
 * cerraron en medio de la clase, o no se pudo guardar el cierre. El audio que quedó es válido hasta
 * la última página Ogg completa; se cierra con su duración real para que se suba y se procese.
 */
object Recuperacion {
    private const val TAG = "ClassmerRecuperacion"

    /**
     * @param activa la sesión que se está grabando ahora en este proceso (no se toca).
     * @param inactividadMs sólo se cierran las que no tocaron sus archivos en este tiempo (0 = todas).
     * @return las sesiones cerradas, con la duración con la que quedaron.
     */
    fun cerrarHuerfanas(
        almacen: AlmacenSesiones,
        activa: String?,
        inactividadMs: Long,
        ahoraMs: Long = System.currentTimeMillis(),
    ): List<Pair<SesionLocal, Long>> {
        val cerradas = mutableListOf<Pair<SesionLocal, Long>>()
        for (s in almacen.listar()) {
            if (s.estadoLocal != EstadoLocal.GRABANDO || s.id == activa) continue
            // Una importada no se grabó: nunca se cierra como interrumpida (ni aunque figure "grabando").
            if (s.importada) continue
            // Si graba el reloj, la grabación sigue allá aunque el celu se haya caído: la retoma SincronizacionWatch.
            if (s.delReloj) continue
            val ultimaActividad = maxOf(almacen.archivoAudio(s).lastModified(), almacen.archivoSesion(s.id).lastModified())
            if (ahoraMs - ultimaActividad < inactividadMs) continue
            val duracion = runCatching { OggDuracion.leerMs(almacen.archivoAudio(s)) }.getOrNull() ?: s.duracionMs
            Log.w(TAG, "Sesión ${s.id} interrumpida; se cierra con $duracion ms")
            val cerrada = runCatching {
                almacen.guardarSesion(
                    s.copy(estadoLocal = EstadoLocal.TERMINADA, duracionMs = duracion, fin = Tiempo.ahoraIso(), interrumpida = true),
                )
            }.isSuccess
            if (cerrada) cerradas += s to duracion
        }
        return cerradas
    }
}
