package ar.classmer.subida

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.work.ForegroundInfo
import ar.classmer.ClassmerApp
import ar.classmer.R
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.importacion.TrabajadorImportacion
import ar.classmer.modelo.SesionLocal

/**
 * Mientras sube una clase importada (puede pesar cientos de MB), el trabajo de subida va en primer
 * plano (`dataSync`) con "Subiendo <materia> · 45 %": así Android no lo frena a los 10 minutos ni lo
 * congela con la pantalla bloqueada. La subida que sigue a la copia, en el primer plano de la copia
 * ([TrabajadorImportacion]), usa el mismo texto en la notificación de la copia. Android sólo deja
 * arrancar el primer plano desde segundo plano con la exención de batería: si no, la importada sube
 * como un trabajo común ([PrimerPlano]). Una clase grabada sube como siempre, sin esta notificación.
 */
object AvisoSubida {
    private const val ID_NOTIFICACION = 30

    /** Una importada que falta subir y cuánto lleva (bytes confirmados sobre el total, `null` si no se sabe). */
    data class Pendiente(val sesion: SesionLocal, val porcentaje: Int?) {
        val texto: String get() = "Subiendo ${sesion.nombreMateria ?: "Auto (IA)"}" + (porcentaje?.let { " · $it %" } ?: "")
    }

    /** La primera importada ya copiada que el server todavía no aceptó (sin un rechazo que espere "Reintentar"). */
    fun pendiente(almacen: AlmacenSesiones): Pendiente? {
        for (s in SincronizacionSubida.ordenDeSubida(almacen.listar())) {
            if (!s.importada || !s.terminada) continue
            val subida = almacen.leerSubida(s.id)
            if (subida.finalizada || subida.errorPermanente != null) continue
            val total = almacen.archivoAudio(s).length()
            val porcentaje = if (total > 0) (subida.bytesConfirmados * 100 / total).toInt().coerceIn(0, 100) else null
            return Pendiente(s, porcentaje)
        }
        return null
    }

    fun info(context: Context, p: Pendiente): ForegroundInfo {
        val n = construir(context, p)
        return if (Build.VERSION.SDK_INT >= 29) {
            ForegroundInfo(ID_NOTIFICACION, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            ForegroundInfo(ID_NOTIFICACION, n)
        }
    }

    /** Actualiza la notificación del primer plano (el mismo id). */
    fun notificar(context: Context, p: Pendiente) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        runCatching { context.getSystemService(NotificationManager::class.java).notify(ID_NOTIFICACION, construir(context, p)) }
    }

    fun construir(context: Context, p: Pendiente): Notification =
        NotificationCompat.Builder(context, ClassmerApp.CANAL_TRANSFERENCIAS)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setContentTitle(p.texto)
            .setContentText("Clase importada: se manda al server de la PC")
            .setProgress(100, p.porcentaje ?: 0, p.porcentaje == null)
            .setOngoing(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setContentIntent(TrabajadorImportacion.abrirApp(context))
            .build()
}
