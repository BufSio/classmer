package ar.classmer.wear

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.media.AudioAttributes
import android.util.Log
import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.Huella
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class ClassmerWatchApp : Application() {

    override fun onCreate() {
        super.onCreate()
        crearCanales()
        cerrarInterrumpidas()
    }

    /**
     * Si el proceso arranca y hay una grabación que figura activa, la app se cayó (o la cerró el
     * sistema) mientras grababa: MediaRecorder murió con el proceso. El audio vale hasta la última
     * página Ogg completa: se cierra como interrumpida, se calcula su huella y se le avisa al celu,
     * que la sube igual. También se completan las huellas que quedaron sin calcular.
     */
    private fun cerrarInterrumpidas() {
        val almacen = AlmacenWatch.de(this)
        val pendientes = almacen.listar().filter { it.activa || (it.terminada && it.sha256 == null) }
        if (pendientes.isEmpty()) return
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
            for (g in pendientes) {
                val cerrada = if (g.activa) {
                    Log.w(TAG, "Grabación ${g.id} interrumpida: se cierra con lo grabado")
                    g.copy(estado = EstadoGrabacionWatch.TERMINADA, desde = null, duracionMs = null, interrumpida = true)
                } else {
                    g
                }
                val bytes = almacen.bytesFirmes(g.id)
                runCatching { almacen.guardar(cerrada.copy(bytes = bytes, sha256 = Huella.sha256(almacen.archivoAudio(g.id)))) }
                    .onFailure { Log.e(TAG, "No se pudo cerrar ${g.id}", it) }
            }
            EnlaceCelu.avisar(this@ClassmerWatchApp)
        }
    }

    private fun crearCanales() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CANAL_GRABACION, "Grabación en curso", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Fija mientras el reloj graba la clase."
                setShowBadge(false)
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(CANAL_PEDIDO, "Tocá para grabar", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "El celu pidió que el reloj grabe la clase: hay que tocar el aviso para empezar."
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 400, 200, 400)
                setSound(null, AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION).build())
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(CANAL_AVISOS, "Avisos", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Problemas con una grabación."
            },
        )
    }

    companion object {
        private const val TAG = "ClassmerWatch"
        const val CANAL_GRABACION = "grabacion"
        const val CANAL_PEDIDO = "pedido"
        const val CANAL_AVISOS = "avisos"
    }
}
