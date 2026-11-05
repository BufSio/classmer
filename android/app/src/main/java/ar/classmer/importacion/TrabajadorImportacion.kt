package ar.classmer.importacion

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.work.BackoffPolicy
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import ar.classmer.ClassmerApp
import ar.classmer.MainActivity
import ar.classmer.R
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.grabacion.OggDuracion
import ar.classmer.modelo.EstadoLocal
import ar.classmer.subida.AvisoSubida
import ar.classmer.subida.PlanificadorSubida
import ar.classmer.subida.PrimerPlano
import ar.classmer.subida.SincronizacionSubida
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * El trabajo de WorkManager que copia al celu una clase importada ([CopiaImportacion]), uno por
 * clase. Sobrevive a que se cierre la app: si Android lo frena, el próximo intento sigue desde lo
 * copiado. Cuando se puede, va en primer plano (`dataSync`) con el progreso en una notificación, y al
 * terminar la copia sigue con la subida en ese mismo primer plano. Si Android no deja arrancar el
 * primer plano desde segundo plano, sigue como trabajo común (con el tope de [PrimerPlano]) y la
 * subida queda para [ar.classmer.subida.TrabajadorSubida].
 */
class TrabajadorImportacion(contexto: Context, params: WorkerParameters) : CoroutineWorker(contexto, params) {

    private val sesionId: String? get() = inputData.getString(CLAVE_SESION)

    /** Para el trabajo urgente en Android 11 o anterior, que arranca en primer plano. */
    override suspend fun getForegroundInfo(): ForegroundInfo = info(applicationContext, sesionId.orEmpty(), nombreMateria(), null)

    override suspend fun doWork(): Result {
        val id = sesionId ?: return Result.failure()
        val ctx = applicationContext
        val almacen = AlmacenSesiones.de(ctx)
        val sesion = almacen.leerSesion(id) ?: return Result.success()
        if (!sesion.importada || sesion.estadoLocal != EstadoLocal.IMPORTANDO) return Result.success()
        val materia = sesion.nombreMateria ?: "Auto (IA)"

        val primerPlano = PrimerPlano()
        val enPrimerPlano = primerPlano.pedir { setForeground(info(ctx, id, materia, null)) }
        var ultimoAviso = 0L
        var ultimoPorcentaje = -1
        val copia = CopiaImportacion(
            almacen = almacen,
            abrir = { uri, desde -> Documentos.abrir(ctx, uri, desde) },
            medirDuracion = { archivo ->
                MetadatosAudio.duracionMs(archivo)
                    ?: if (archivo.extension in setOf("ogg", "opus", "oga")) runCatching { OggDuracion.leerMs(archivo) }.getOrNull() else null
            },
            espacioLibre = Importador::espacioLibre,
            alAvanzar = { copiados, total ->
                val porcentaje = if (total > 0) (copiados * 100 / total).toInt().coerceIn(0, 100) else -1
                val ahora = SystemClock.elapsedRealtime()
                if (enPrimerPlano && porcentaje != ultimoPorcentaje && ahora - ultimoAviso >= 1_000) {
                    ultimoAviso = ahora
                    ultimoPorcentaje = porcentaje
                    notificar(ctx, id, construir(ctx, materia, porcentaje.takeIf { it >= 0 }))
                }
            },
            // Sin primer plano, WorkManager no avisa el fin de la ventana del trabajo: corta el tope.
            seguir = { !isStopped && primerPlano.dentroDelTope },
        )
        val r = try {
            withContext(Dispatchers.IO) { copia.copiar(id, reintento = inputData.getBoolean(CLAVE_REINTENTO, false)) }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Algo inesperado (por ejemplo, el disco falló al anotar): lo copiado quedó anotado, se reintenta.
            Log.w(TAG, "La copia de $id falló", e)
            return Result.retry()
        }
        when (r) {
            CopiaImportacion.Resultado.TERMINADA -> {
                // La copia ya está en el celu: los documentos originales no hacen falta más (salvo que otra
                // importación use los mismos).
                almacen.leerImportacion(id)?.let { imp -> Documentos.soltarPermisos(ctx, Importador.uris(imp)) }
                try {
                    if (enPrimerPlano) subirEnPrimerPlano(ctx, almacen, id)
                } finally {
                    // Lo que no se pudo subir acá (o todo, sin primer plano) lo sube WorkManager.
                    PlanificadorSubida.patear(ctx)
                }
            }
            CopiaImportacion.Resultado.ERROR -> ClassmerApp.avisar(
                ctx,
                "No se pudo importar la clase",
                almacen.leerImportacion(id)?.error ?: "Abrí Classmer para ver qué pasó.",
            )
            CopiaImportacion.Resultado.CORTADA -> return Result.retry()
            CopiaImportacion.Resultado.NADA -> Unit
        }
        return Result.success()
    }

    /**
     * Todavía en primer plano, la subida empieza acá mismo, como la subida final del servicio de grabación.
     * Si quedara sólo para [ar.classmer.subida.TrabajadorSubida], ése tendría que pedir el primer plano
     * desde segundo plano (por ejemplo, si el usuario salió de la app mientras se copiaba), y Android 12+ no lo
     * deja sin la exención de batería: la clase subiría como un trabajo común, de a 10 minutos y frenada
     * en Doze. La notificación de la copia pasa a ser la de la subida ("Subiendo Macro · 45 %").
     */
    private suspend fun subirEnPrimerPlano(ctx: Context, almacen: AlmacenSesiones, id: String) {
        try {
            SincronizacionSubida.ejecutar(ctx, acompanar = {
                while (true) {
                    withContext(Dispatchers.IO) { runCatching { AvisoSubida.pendiente(almacen) }.getOrNull() }
                        ?.let { notificar(ctx, id, AvisoSubida.construir(ctx, it)) }
                    delay(2_000)
                }
            })
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.w(TAG, "La subida de $id desde la copia falló: sigue WorkManager", e)
        }
    }

    private fun nombreMateria(): String =
        sesionId?.let { AlmacenSesiones.de(applicationContext).leerSesion(it)?.nombreMateria } ?: "Auto (IA)"

    companion object {
        private const val TAG = "ClassmerImportacion"
        const val CLAVE_SESION = "sesion_id"
        const val CLAVE_REINTENTO = "reintento"

        /** Una notificación por clase (se pueden importar dos a la vez). */
        private fun idNotificacion(sesionId: String) = 2000 + (sesionId.hashCode() and 0x3FF)

        fun info(context: Context, sesionId: String, materia: String, porcentaje: Int?): ForegroundInfo {
            val n = construir(context, materia, porcentaje)
            return if (Build.VERSION.SDK_INT >= 29) {
                ForegroundInfo(idNotificacion(sesionId), n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            } else {
                ForegroundInfo(idNotificacion(sesionId), n)
            }
        }

        private fun construir(context: Context, materia: String, porcentaje: Int?): Notification =
            NotificationCompat.Builder(context, ClassmerApp.CANAL_TRANSFERENCIAS)
                .setSmallIcon(R.drawable.ic_notificacion)
                .setContentTitle("Importando $materia")
                .setContentText(porcentaje?.let { "Copiando al celu · $it %" } ?: "Copiando al celu…")
                .setProgress(100, porcentaje ?: 0, porcentaje == null)
                .setOngoing(true)
                .setSilent(true)
                .setCategory(NotificationCompat.CATEGORY_PROGRESS)
                .setContentIntent(abrirApp(context))
                .build()

        private fun notificar(context: Context, sesionId: String, n: Notification) {
            if (Build.VERSION.SDK_INT >= 33 &&
                ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
            ) {
                return
            }
            runCatching { context.getSystemService(NotificationManager::class.java).notify(idNotificacion(sesionId), n) }
        }

        fun abrirApp(context: Context): PendingIntent = PendingIntent.getActivity(
            context, 0, Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }
}

/** Cuándo corre [TrabajadorImportacion]: al confirmar, con "Reintentar" y, por las dudas, al abrir la app. */
object PlanificadorImportacion {
    private const val TAG = "ClassmerImportacion"
    private val ejecutor = Executors.newSingleThreadExecutor()

    fun nombre(sesionId: String) = "importar-$sesionId"

    /** Uno por clase: si ya está encolado o corriendo, no se encola otro (salvo "Reintentar", que lo reemplaza). */
    fun encolar(context: Context, sesionId: String, reintento: Boolean = false) {
        val app = context.applicationContext
        val pedido = OneTimeWorkRequestBuilder<TrabajadorImportacion>()
            .setInputData(workDataOf(TrabajadorImportacion.CLAVE_SESION to sesionId, TrabajadorImportacion.CLAVE_REINTENTO to reintento))
            // Urgente: con la app a la vista arranca enseguida (y puede pasar a primer plano).
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .setBackoffCriteria(BackoffPolicy.LINEAR, 30, TimeUnit.SECONDS)
            .build()
        runCatching {
            WorkManager.getInstance(app).enqueueUniqueWork(
                nombre(sesionId),
                if (reintento) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP,
                pedido,
            )
        }.onFailure { Log.w(TAG, "No se pudo encolar la copia de $sesionId", it) }
    }

    fun cancelar(context: Context, sesionId: String) {
        runCatching { WorkManager.getInstance(context.applicationContext).cancelUniqueWork(nombre(sesionId)) }
    }

    /**
     * Al abrir la app: las importaciones que siguen copiándose (sin error) vuelven a encolarse. WorkManager
     * ya las retoma solo; esto cubre que se haya perdido su trabajo (por ejemplo, al actualizar la app).
     */
    fun retomar(context: Context) {
        val app = context.applicationContext
        ejecutor.execute {
            runCatching {
                val almacen = AlmacenSesiones.de(app)
                for (s in almacen.listar()) {
                    if (!s.importada || s.estadoLocal != EstadoLocal.IMPORTANDO) continue
                    val imp = almacen.leerImportacion(s.id) ?: continue
                    if (imp.error == null) encolar(app, s.id)
                }
            }.onFailure { Log.w(TAG, "No se pudieron retomar las importaciones", it) }
        }
    }
}
