package ar.classmer

import android.Manifest
import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.grabacion.Recuperacion
import ar.classmer.grabacion.ServicioGrabacion
import ar.classmer.importacion.PlanificadorImportacion
import ar.classmer.modelo.FuenteAudio
import ar.classmer.subida.PlanificadorSubida
import ar.classmer.util.Tiempo
import ar.classmer.watch.Ambos
import ar.classmer.watch.SincronizacionWatch

class ClassmerApp : Application() {

    override fun onCreate() {
        super.onCreate()
        crearCanales()
        cerrarSesionesInterrumpidas()
        getSystemService(ConnectivityManager::class.java)?.registerDefaultNetworkCallback(
            object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) = PlanificadorSubida.patear(this@ClassmerApp)
            },
        )
        PlanificadorSubida.patear(this)
        // Una clase que está grabando (o grabó) el reloj: se retoma lo pendiente.
        SincronizacionWatch.ponerseAlDiaEnFondo(this)
        // Una clase importada que se estaba copiando cuando se cerró la app.
        PlanificadorImportacion.retomar(this)
    }

    /**
     * Si el proceso arranca y hay una sesión que figura "grabando", la app se cayó o la cerraron
     * mientras grababa: la grabación de ese proceso ya no existe. Se cierra y se avisa enseguida
     * (el servicio es START_STICKY, así que Android vuelve a levantar el proceso tras un cierre).
     */
    private fun cerrarSesionesInterrumpidas() {
        // Si el proceso murió con "¿Sigue la clase?" abierta, sus botones ya no hacen nada.
        runCatching { getSystemService(NotificationManager::class.java).cancel(ServicioGrabacion.ID_PREGUNTA) }
        for ((_, duracion) in Recuperacion.cerrarHuerfanas(AlmacenSesiones.de(this), activa = null, inactividadMs = 0)) {
            avisarCorte(this, duracion)
        }
        // Ambos: la pista del celu que quedó abierta se cierra; si el reloj nunca grabó, el celu era lo único
        // que grababa y también se avisa.
        for ((_, duracion) in runCatching { Ambos.cerrarPistasCortadas(AlmacenSesiones.de(this), activa = null) }.getOrDefault(emptyList())) {
            avisarCorte(this, duracion)
        }
    }

    private fun crearCanales() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CANAL_GRABACION, "Grabación en curso", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Notificación fija mientras se graba una clase, con marcas y pausa."
                setShowBadge(false)
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(CANAL_AVISOS, "Avisos", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Problemas con una grabación o con la subida."
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(CANAL_TRANSFERENCIAS, "Clases importadas", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Progreso mientras se copia al celu y se sube una clase importada."
                setShowBadge(false)
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(CANAL_CORTE, "¿Sigue la clase?", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Pregunta al pasar el fin de la clase. Si nadie responde en 10 minutos, la grabación termina sola."
                enableVibration(true)
            },
        )
    }

    companion object {
        const val CANAL_GRABACION = "grabacion"
        const val CANAL_AVISOS = "avisos"
        const val CANAL_CORTE = "corte"
        const val CANAL_TRANSFERENCIAS = "transferencias"
        private var siguienteAviso = 100

        fun avisarCorte(context: Context, grabadoMs: Long) = avisar(
            context,
            "La grabación se cortó",
            "Se guardó y se va a subir lo grabado hasta ${Tiempo.duracion(grabadoMs)}. Si la clase sigue, abrí Classmer y tocá Grabar.",
        )

        fun avisar(context: Context, titulo: String, texto: String) {
            if (Build.VERSION.SDK_INT >= 33 &&
                ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
            ) {
                return
            }
            val abrir = PendingIntent.getActivity(
                context, 0, Intent(context, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )
            val n = NotificationCompat.Builder(context, CANAL_AVISOS)
                .setSmallIcon(R.drawable.ic_notificacion)
                .setContentTitle(titulo)
                .setContentText(texto)
                .setStyle(NotificationCompat.BigTextStyle().bigText(texto))
                .setContentIntent(abrir)
                .setAutoCancel(true)
                // Si graba el reloj, lo tiene el profe: los avisos de Classmer no se copian allá.
                .setLocalOnly(ControlGrabacion.estado.value.sesion?.fuenteAudio == FuenteAudio.RELOJ)
                .build()
            runCatching { context.getSystemService(NotificationManager::class.java).notify(siguienteAviso++, n) }
        }
    }
}
