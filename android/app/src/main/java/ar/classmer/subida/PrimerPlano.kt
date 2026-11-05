package ar.classmer.subida

import android.os.SystemClock
import android.util.Log
import kotlinx.coroutines.CancellationException

/**
 * El primer plano (`dataSync`) de un trabajo de WorkManager que puede tardar: la copia de una importada
 * y su subida. Desde segundo plano, Android 12+ no deja arrancarlo (salvo con la exención de batería), y
 * el trabajo sigue como uno común.
 *
 * Pero WorkManager 2.12 anota el trabajo como de primer plano antes de arrancar el servicio
 * (`Processor.startForeground`) y no lo deshace si Android lo niega. Desde ahí ignora el aviso de
 * JobScheduler de que se terminó la ventana del trabajo (`Processor.stopWork`): `isStopped` nunca se
 * pone en true y el trabajo seguiría corriendo afuera de su ventana, sin servicio ni trabajo activo,
 * hasta que Android congele o mate el proceso. Por eso, si Android lo negó, el corte de los 10 minutos
 * lo pone el trabajo: a los [TOPE_MS] de empezar, [dentroDelTope] da false, y el trabajo para y pide
 * reintento (lo hecho queda anotado).
 *
 * @param ahora el reloj (reemplazable en los tests); se crea al empezar el trabajo.
 */
class PrimerPlano(private val ahora: () -> Long = SystemClock::elapsedRealtime) {
    private val inicio = ahora()

    @Volatile
    var activo = false
        private set

    /** Android no lo dejó: el trabajo sigue sin primer plano, con [TOPE_MS]. */
    @Volatile
    var negado = false
        private set

    /** Pasa a primer plano con [pasar] (el `setForeground` del trabajo), una sola vez. Devuelve si quedó. */
    suspend fun pedir(pasar: suspend () -> Unit): Boolean {
        if (activo || negado) return activo
        try {
            pasar()
            activo = true
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // ForegroundServiceStartNotAllowedException (Android 12+, desde segundo plano) o similar.
            Log.w(TAG, "Sigue sin primer plano: ${e.message}")
            negado = true
        }
        return activo
    }

    /** false cuando hay que parar: Android negó el primer plano y ya pasó [TOPE_MS] desde que empezó el trabajo. */
    val dentroDelTope: Boolean get() = !negado || ahora() - inicio < TOPE_MS

    companion object {
        private const val TAG = "ClassmerPrimerPlano"

        /** Antes de los 10 minutos de la ventana de un trabajo común de JobScheduler. */
        const val TOPE_MS = 9 * 60_000L
    }
}

/** Se llegó al [PrimerPlano.TOPE_MS] sin primer plano: el trabajo pide reintento. */
class TopeSinPrimerPlano : Exception("Sin primer plano: se corta antes de que termine la ventana del trabajo")
