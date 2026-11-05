package ar.classmer.subida

import android.content.Context
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Decide cuándo correr [TrabajadorSubida]. Se "patea" al terminar una clase, al abrir la app, al
 * guardar los ajustes y cuando vuelve la red (mientras se graba, sube el servicio directamente).
 *
 * - Si el trabajo está esperando un reintento, lo reemplaza para que corra ya.
 * - Si está corriendo, encola una pasada más en un trabajo aparte ([NOMBRE_SIGUIENTE]), así no se
 *   pierde lo que llegó mientras tanto (por ejemplo, el fin de la grabación) aunque el que corre falle.
 *
 * Sin restricción de red: el server está en la LAN y Android sólo considera "conectada" una red con
 * internet validado. Si no hay red, el intento falla rápido y se reintenta con espera creciente
 * lineal (30 s, 60 s, 90 s…), que no se dispara a horas como la exponencial.
 */
object PlanificadorSubida {
    const val NOMBRE = "subida"
    const val NOMBRE_SIGUIENTE = "subida-siguiente"
    private const val TAG = "ClassmerSubida"

    /** Hay algo nuevo para subir desde la última pasada del trabajo. */
    val pendiente = AtomicBoolean(false)

    private val ejecutor = Executors.newSingleThreadExecutor()

    fun patear(context: Context) {
        pendiente.set(true)
        val app = context.applicationContext
        ejecutor.execute {
            runCatching { encolar(app) }.onFailure { Log.w(TAG, "No se pudo encolar la subida", it) }
        }
    }

    private fun encolar(context: Context) {
        val wm = WorkManager.getInstance(context)
        val activos = wm.getWorkInfosForUniqueWork(NOMBRE).get().filter { !it.state.isFinished }
        when {
            activos.any { it.state == WorkInfo.State.RUNNING } ->
                wm.enqueueUniqueWork(NOMBRE_SIGUIENTE, ExistingWorkPolicy.KEEP, solicitud())
            activos.any { it.state == WorkInfo.State.ENQUEUED && it.runAttemptCount > 0 } ->
                wm.enqueueUniqueWork(NOMBRE, ExistingWorkPolicy.REPLACE, solicitud())
            else -> wm.enqueueUniqueWork(NOMBRE, ExistingWorkPolicy.KEEP, solicitud())
        }
    }

    private fun solicitud() = OneTimeWorkRequestBuilder<TrabajadorSubida>()
        .setBackoffCriteria(BackoffPolicy.LINEAR, 30, TimeUnit.SECONDS)
        .build()
}
