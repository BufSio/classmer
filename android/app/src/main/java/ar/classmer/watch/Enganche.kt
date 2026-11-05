package ar.classmer.watch

import android.Manifest
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import ar.classmer.ClassmerApp
import ar.classmer.R
import ar.classmer.grabacion.ServicioGrabacion

/**
 * Engancha el servicio de grabación del celu a una clase que está grabando el reloj. Desde segundo
 * plano (un estado que llegó del reloj con la app cerrada), Android sólo deja arrancar el servicio
 * si Classmer no tiene restricciones de batería; si no deja, queda un aviso que lo arranca con un toque.
 */
object Enganche {
    private const val TAG = "ClassmerWatch"
    private const val ID_AVISO = 3

    fun pedir(context: Context, sesionId: String, conCelu: Boolean = false) {
        val intent = intent(context, sesionId).putExtra(ServicioGrabacion.EXTRA_PISTA_CELU, conCelu)
        try {
            ContextCompat.startForegroundService(context, intent)
            cancelarAviso(context)
        } catch (e: Exception) {
            // ForegroundServiceStartNotAllowedException (Android 12+) o IllegalStateException.
            Log.w(TAG, "No se pudo enganchar la grabación del reloj desde segundo plano: ${e.message}")
            avisar(context, sesionId)
        }
    }

    private fun intent(context: Context, sesionId: String) =
        Intent(context, ServicioGrabacion::class.java)
            .setAction(ServicioGrabacion.ACCION_ENGANCHAR)
            .putExtra(ServicioGrabacion.EXTRA_SESION_ID, sesionId)

    /** Tocar el aviso cuenta como interacción del usuario: Android deja arrancar el servicio. */
    private fun avisar(context: Context, sesionId: String) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val arrancar = PendingIntent.getForegroundService(
            context, 7, intent(context, sesionId),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n = NotificationCompat.Builder(context, ClassmerApp.CANAL_AVISOS)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setContentTitle("El reloj está grabando una clase")
            .setContentText("Tocá para seguirla en el celu (fotos, notas y marcas).")
            .setContentIntent(arrancar)
            .setAutoCancel(true)
            // El reloj lo tiene el profe: este aviso es sólo para el celu.
            .setLocalOnly(true)
            .build()
        runCatching { context.getSystemService(NotificationManager::class.java).notify(ID_AVISO, n) }
    }

    fun cancelarAviso(context: Context) {
        runCatching { context.getSystemService(NotificationManager::class.java).cancel(ID_AVISO) }
    }
}
