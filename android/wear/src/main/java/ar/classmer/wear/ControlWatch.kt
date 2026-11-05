package ar.classmer.wear

import android.content.Context
import android.content.Intent
import android.media.MediaRecorder
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import ar.classmer.watchlink.GrabacionWatch
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/** Lo que muestra la pantalla del reloj. */
data class EstadoPantalla(
    /** La grabación en curso (`null` = no se graba). */
    val grabacion: GrabacionWatch? = null,
    /** El celu pidió grabar y se espera el toque. */
    val pedido: PedidoInicio? = null,
    /** Grabaciones terminadas que el celu todavía no confirmó. */
    val porPasar: Int = 0,
    /** Algo salió mal (por ejemplo, no se pudo abrir el micrófono). También le llega al celu. */
    val error: String? = null,
)

/**
 * Punto de entrada a la grabación para la pantalla y el Data Layer. El estado vive en
 * [ServicioGrabacionWatch]; acá sólo se publica y se mandan las órdenes como intents.
 */
object ControlWatch {
    internal val estadoMutable = MutableStateFlow(EstadoPantalla())
    val estado: StateFlow<EstadoPantalla> = estadoMutable.asStateFlow()

    @Volatile
    internal var cronometro: Cronometro? = null

    /** [ServicioGrabacionWatch] está vivo (grabando, o esperando que el celu confirme lo último). */
    @Volatile
    internal var servicioActivo = false

    /** Audio grabado hasta ahora en la grabación en curso (sin las pausas). */
    fun offsetActualMs(): Long = cronometro?.offsetMs() ?: 0

    /**
     * Relee del disco el pedido y las grabaciones por pasar (las cambia el Data Layer). Se escribe con
     * `update` (atómico): la pantalla, el servicio y el Data Layer publican desde hilos distintos.
     */
    fun refrescar(context: Context) {
        val almacen = AlmacenWatch.de(context)
        val pedido = almacen.leerPedido()
        val porPasar = almacen.listar().count { it.terminada }
        // Un pedido vencido ya no se puede usar: tampoco su aviso.
        if (pedido == null) Avisos.cancelarPedido(context)
        estadoMutable.update { it.copy(pedido = pedido, porPasar = porPasar) }
    }

    fun mostrarError(error: String?) = estadoMutable.update { it.copy(error = error) }

    fun limpiarError() = mostrarError(null)

    /**
     * Empieza a grabar. Sólo desde la pantalla del reloj (visible) o desde un toque en el aviso:
     * Android no deja prender el micrófono desde segundo plano.
     */
    fun iniciar(context: Context, pedido: PedidoInicio?) {
        val intent = Intent(context, ServicioGrabacionWatch::class.java).setAction(ServicioGrabacionWatch.ACCION_INICIAR)
        pedido?.let {
            intent.putExtra(ServicioGrabacionWatch.EXTRA_ID, it.id)
                .putExtra(ServicioGrabacionWatch.EXTRA_MATERIA, it.materia)
                .putExtra(ServicioGrabacionWatch.EXTRA_TIPO_CLASE, it.tipoClase)
        }
        ContextCompat.startForegroundService(context, intent)
    }

    fun terminar(context: Context) = enviar(context, ServicioGrabacionWatch.ACCION_TERMINAR)

    /** El celu está trayendo audio: si el servicio espera que confirme, le da más tiempo. */
    fun avisarEnvio(context: Context) {
        if (!servicioActivo) return
        runCatching { context.startService(Intent(context, ServicioGrabacionWatch::class.java).setAction(ServicioGrabacionWatch.ACCION_ENVIANDO)) }
    }

    /** El celu tiene la grabación [id] entera: el servicio ya no necesita quedarse por ella. */
    fun confirmarRecibida(context: Context, id: String) {
        if (!servicioActivo) return
        val intent = Intent(context, ServicioGrabacionWatch::class.java)
            .setAction(ServicioGrabacionWatch.ACCION_RECIBIDA)
            .putExtra(ServicioGrabacionWatch.EXTRA_ID, id)
        runCatching { context.startService(intent) }
    }

    /** Una orden al servicio que está grabando; [respuestaA] es la orden del celu a contestar. */
    fun enviar(context: Context, accion: String, respuestaA: Long? = null, nodo: String? = null): Boolean {
        if (estado.value.grabacion == null) return false
        val intent = Intent(context, ServicioGrabacionWatch::class.java).setAction(accion)
        respuestaA?.let { intent.putExtra(ServicioGrabacionWatch.EXTRA_RESPUESTA_A, it) }
        nodo?.let { intent.putExtra(ServicioGrabacionWatch.EXTRA_NODO, it) }
        // Mientras graba, el servicio está en primer plano: Android deja mandarle órdenes desde atrás.
        return runCatching { context.startService(intent) }.isSuccess
    }
}

/**
 * Cómo graba el reloj. Ogg/Opus mono a 48 kHz (lo mismo que el celu, así el server no se entera).
 * La fuente y el bitrate salieron de una prueba corta en el Watch 8 (ver android/README.md): MIC le
 * saca unos 13 dB de voz sobre ruido a VOICE_RECOGNITION, sin bombear en los silencios, y a 32 kbps
 * Opus ya no limita al ASR. Se pueden cambiar por adb para volver a probar, sin pantalla, igual que
 * `prueba_corte_seg` en el celu.
 */
class AjustesWatch(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("ajustes", Context.MODE_PRIVATE)

    val fuente: Int get() = prefs.getInt(FUENTE, FUENTE_POR_DEFECTO)
    val bitrate: Int get() = prefs.getInt(BITRATE, BITRATE_POR_DEFECTO).coerceIn(6_000, 510_000)

    /** Para la prueba de fuentes: deja la fuente y el bitrate elegidos para las próximas grabaciones. */
    fun cambiar(fuente: Int?, bitrate: Int?) = prefs.edit {
        if (fuente == null) remove(FUENTE) else putInt(FUENTE, fuente)
        if (bitrate == null) remove(BITRATE) else putInt(BITRATE, bitrate)
    }

    companion object {
        private const val FUENTE = "fuente_audio"
        private const val BITRATE = "bitrate"
        const val FUENTE_POR_DEFECTO = MediaRecorder.AudioSource.MIC
        const val BITRATE_POR_DEFECTO = 32_000

        fun nombreFuente(f: Int): String = when (f) {
            MediaRecorder.AudioSource.MIC -> "MIC"
            MediaRecorder.AudioSource.VOICE_RECOGNITION -> "VOICE_RECOGNITION"
            MediaRecorder.AudioSource.UNPROCESSED -> "UNPROCESSED"
            MediaRecorder.AudioSource.CAMCORDER -> "CAMCORDER"
            MediaRecorder.AudioSource.VOICE_COMMUNICATION -> "VOICE_COMMUNICATION"
            else -> "fuente $f"
        }
    }
}
