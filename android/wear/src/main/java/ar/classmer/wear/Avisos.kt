package ar.classmer.wear

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.wear.ongoing.OngoingActivity
import androidx.wear.ongoing.Status
import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.GrabacionWatch

/** Las notificaciones del reloj. */
object Avisos {
    const val ID_PEDIDO = 2

    /**
     * "Classmer: tocá para grabar". El toque abre la pantalla de Classmer y empieza a grabar: Android
     * deja prender el micrófono porque lo pidió el usuario con la app a la vista.
     */
    fun pedirToque(context: Context, pedido: PedidoInicio) {
        if (!puedeNotificar(context)) return
        val grabar = PendingIntent.getActivity(
            context, 10,
            Intent(context, MainActivity::class.java)
                .setAction(MainActivity.ACCION_GRABAR_PEDIDO)
                .putExtra(MainActivity.EXTRA_PEDIDO, pedido.id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n = NotificationCompat.Builder(context, ClassmerWatchApp.CANAL_PEDIDO)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setContentTitle("Classmer: tocá para grabar")
            .setContentText(pedido.materia?.let { "$it · lo pidió el celu" } ?: "Lo pidió el celu")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setContentIntent(grabar)
            .addAction(0, "● Grabar", grabar)
            .setAutoCancel(true)
            .build()
        runCatching { context.getSystemService(NotificationManager::class.java).notify(ID_PEDIDO, n) }
    }

    fun cancelarPedido(context: Context) {
        runCatching { context.getSystemService(NotificationManager::class.java).cancel(ID_PEDIDO) }
    }

    /** La notificación del servicio mientras graba, con la Ongoing Activity (el indicador en la esfera). */
    fun grabando(context: Context, g: GrabacionWatch, offsetMs: Long): Notification {
        val abrir = PendingIntent.getActivity(
            context, 0,
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val pausada = g.estado == EstadoGrabacionWatch.PAUSADA
        val titulo = if (pausada) "Classmer en pausa" else "Classmer grabando"
        val b = NotificationCompat.Builder(context, ClassmerWatchApp.CANAL_GRABACION)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setContentTitle(titulo)
            .setContentText(g.materia ?: "Grabando la clase")
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_STOPWATCH)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(abrir)
        // El cronómetro de la esfera cuenta el audio grabado (sin las pausas), igual que la pantalla.
        val estado = if (pausada) {
            Status.Builder().addTemplate("En pausa").build()
        } else {
            Status.Builder().addTemplate("#tiempo#").addPart("tiempo", Status.StopwatchPart(SystemClock.elapsedRealtime() - offsetMs)).build()
        }
        OngoingActivity.Builder(context, ServicioGrabacionWatch.ID_NOTIFICACION, b)
            .setStaticIcon(R.drawable.ic_notificacion)
            .setTouchIntent(abrir)
            .setStatus(estado)
            .build()
            .apply(context)
        return b.build()
    }

    fun actualizarGrabando(context: Context, g: GrabacionWatch, offsetMs: Long) {
        if (!puedeNotificar(context)) return
        context.getSystemService(NotificationManager::class.java).notify(ServicioGrabacionWatch.ID_NOTIFICACION, grabando(context, g, offsetMs))
    }

    /** Después de grabar, mientras se calcula la huella y se termina de pasar el audio al celu. */
    fun mostrarCerrando(context: Context) {
        if (!puedeNotificar(context)) return
        val n = NotificationCompat.Builder(context, ClassmerWatchApp.CANAL_GRABACION)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setContentTitle("Classmer")
            .setContentText("Pasando el audio al celu")
            .setOngoing(true)
            .setSilent(true)
            .setProgress(0, 0, true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
        runCatching { context.getSystemService(NotificationManager::class.java).notify(ServicioGrabacionWatch.ID_NOTIFICACION, n) }
    }

    private fun puedeNotificar(context: Context) =
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
}
