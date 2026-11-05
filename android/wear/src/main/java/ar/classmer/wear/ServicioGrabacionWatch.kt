package ar.classmer.wear

import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.MediaRecorder
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.app.ServiceCompat
import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.Huella
import ar.classmer.watchlink.IdGrabacion
import ar.classmer.watchlink.OrigenGrabacion
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/**
 * Servicio en primer plano de tipo `microphone` del reloj: graba la clase en un único Ogg/Opus
 * (mono, 48 kHz) y sigue con la pantalla apagada. Muestra una Ongoing Activity (el indicador de
 * "grabando" en la esfera).
 *
 * El audio queda en el reloj hasta que el celu confirma que tiene la copia: si se corta la conexión,
 * se sigue grabando igual y el celu se pone al día después. Las órdenes llegan como intents (de la
 * pantalla o del celu, vía [ServicioDatos]) y se atienden en el hilo principal.
 */
class ServicioGrabacionWatch : Service() {

    private val handler = Handler(Looper.getMainLooper())
    private lateinit var almacen: AlmacenWatch
    private val alcance = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private var grabacion: GrabacionWatch? = null
    private var recorder: MediaRecorder? = null
    private var cronometro: Cronometro? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var ticks = 0L
    private var ultimoStartId = 0

    /** Huellas que se están calculando: el servicio sigue en primer plano hasta terminarlas. */
    private var cierresPendientes = 0

    /**
     * Grabaciones terminadas que el celu todavía no confirmó ([ar.classmer.watchlink.Rutas.RECIBIDA]):
     * el servicio sigue en primer plano un rato para que Android no congele el proceso a mitad del envío.
     */
    private val porConfirmar = mutableSetOf<String>()

    /** Cuándo se reinició la grabadora tras un error (para no reintentar sin fin). */
    private val reinicios = mutableListOf<Long>()

    /** El sistema ya destruyó el servicio: lo que termina después (la huella) no toca la notificación. */
    private var destruido = false

    /** `elapsedRealtime` al empezar la grabación en curso (para el tope de duración). */
    private var iniciadaEn = 0L

    /** Para detectar que el audio dejó de crecer (disco lleno, micrófono caído). */
    private var ultimoLargo = -1L
    private var ticksSinCrecer = 0

    /** Android le está dando silencio a la grabación (otra captura tomó el micrófono). */
    private var silenciada = false

    override fun onCreate() {
        super.onCreate()
        almacen = AlmacenWatch.de(this)
        ControlWatch.servicioActivo = true
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ultimoStartId = startId
        val respuestaA = intent?.getLongExtra(EXTRA_RESPUESTA_A, -1)?.takeIf { it >= 0 }
        val nodo = intent?.getStringExtra(EXTRA_NODO)
        when (intent?.action) {
            ACCION_INICIAR -> iniciar(intent)
            ACCION_PAUSAR -> pausar()
            ACCION_REANUDAR -> reanudar()
            ACCION_TERMINAR -> terminar()
            ACCION_RECIBIDA -> recibida(intent.getStringExtra(EXTRA_ID))
            ACCION_ENVIANDO -> postergarEspera()
        }
        // Se contesta la orden del celu con el estado ya cambiado.
        if (respuestaA != null) EnlaceCelu.avisar(this, respuestaA, nodo)
        // Una orden vieja sin grabación activa (o Android que vuelve a levantar el servicio tras matar
        // el proceso): no hay nada que hacer; el micrófono no se puede reabrir desde atrás.
        if (grabacion == null && cierresPendientes == 0 && porConfirmar.isEmpty()) stopSelf(startId)
        return if (grabacion != null) START_STICKY else START_NOT_STICKY
    }

    override fun onDestroy() {
        destruido = true
        // Sólo pasa si el sistema detiene el servicio mientras graba: se cierra lo grabado y se avisa.
        grabacion?.let { cerrar(it, interrumpida = true) }
        porConfirmar.clear()
        liberarWakeLock()
        handler.removeCallbacks(tick)
        handler.removeCallbacks(soltarEspera)
        ControlWatch.servicioActivo = false
        super.onDestroy()
        // Las huellas pendientes siguen en el alcance: se calculan aunque el servicio ya no esté.
    }

    // ---- Órdenes ----

    private fun iniciar(intent: Intent) {
        if (grabacion != null) return
        val pedido = intent.getStringExtra(EXTRA_ID)?.takeIf { IdGrabacion.valido(it) }
        val nueva = GrabacionWatch(
            id = pedido ?: IdGrabacion.nuevo(),
            estado = EstadoGrabacionWatch.GRABANDO,
            origen = if (pedido != null) OrigenGrabacion.CELU else OrigenGrabacion.WATCH,
            inicio = ahoraIso(),
            bytes = 0,
            acumuladoMs = 0,
            materia = intent.getStringExtra(EXTRA_MATERIA),
            tipoClase = intent.getStringExtra(EXTRA_TIPO_CLASE),
        )
        // startForegroundService() obliga a llamar startForeground() enseguida, aunque después falle algo.
        try {
            ServiceCompat.startForeground(
                this, ID_NOTIFICACION, Avisos.grabando(this, nueva, 0), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
            )
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo pasar a primer plano", e)
            fallar("Android no dejó prender el micrófono (${e.message}). Abrí Classmer en el reloj y tocá Grabar.")
            return
        }
        if (pedido != null && almacen.leer(pedido) != null) {
            // Dos toques al mismo aviso: esa grabación ya existe.
            olvidarPedido(pedido)
            detenerSiTermino()
            return
        }
        if (arrancar(nueva)) olvidarPedido(nueva.id)
    }

    /** Crea la carpeta y arranca MediaRecorder. El servicio ya tiene que estar en primer plano. */
    private fun arrancar(nueva: GrabacionWatch, enPausa: Boolean = false): Boolean {
        val crono = Cronometro()
        var r: MediaRecorder? = null
        try {
            almacen.crear(nueva)
            r = crearRecorder(almacen.archivoAudio(nueva.id))
            r.prepare()
            r.start()
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo iniciar MediaRecorder", e)
            r?.release()
            // Todavía no hay nada grabado: se borra la carpeta para que no quede una grabación fantasma.
            almacen.borrar(nueva.id)
            fallar("No se pudo usar el micrófono: ${e.message ?: e.javaClass.simpleName}")
            return false
        }
        recorder = r
        cronometro = crono
        ControlWatch.cronometro = crono
        crono.iniciar()
        iniciadaEn = SystemClock.elapsedRealtime()
        silenciada = false
        var g = nueva.copy(desde = crono.desde)
        // Una continuación de una grabación que estaba en pausa arranca en pausa.
        if (enPausa && runCatching { r.pause() }.isSuccess) {
            g = g.copy(estado = EstadoGrabacionWatch.PAUSADA, acumuladoMs = crono.pausar(), desde = null)
        }
        grabacion = g
        guardar(g)
        adquirirWakeLock()
        publicar(error = null)
        ticks = 0
        ultimoLargo = -1
        ticksSinCrecer = 0
        handler.removeCallbacks(tick)
        handler.postDelayed(tick, INTERVALO_TICK_MS)
        EnlaceCelu.avisar(this)
        return true
    }

    private fun pausar() {
        val g = grabacion ?: return
        val r = recorder ?: return
        if (g.estado != EstadoGrabacionWatch.GRABANDO) return
        try {
            r.pause()
        } catch (e: Exception) {
            Log.w(TAG, "pause() falló", e)
            return
        }
        val acumulado = cronometro?.pausar() ?: g.acumuladoMs
        cambiar(g.copy(estado = EstadoGrabacionWatch.PAUSADA, acumuladoMs = acumulado, desde = null))
    }

    private fun reanudar() {
        val g = grabacion ?: return
        val r = recorder ?: return
        if (g.estado != EstadoGrabacionWatch.PAUSADA) return
        try {
            r.resume()
        } catch (e: Exception) {
            Log.w(TAG, "resume() falló", e)
            return
        }
        val crono = cronometro ?: return
        crono.iniciar()
        cambiar(g.copy(estado = EstadoGrabacionWatch.GRABANDO, acumuladoMs = crono.acumuladoMs, desde = crono.desde))
    }

    private fun terminar() {
        val g = grabacion ?: return
        cerrar(g, interrumpida = false)
        detenerSiTermino()
    }

    /** El celu confirmó que tiene la grabación entera: ya no hace falta seguir en primer plano por ella. */
    private fun recibida(id: String?) {
        if (id != null && porConfirmar.remove(id)) detenerSiTermino()
    }

    /** El celu está trayendo audio de una grabación terminada: se le da más tiempo antes de soltar el primer plano. */
    private fun postergarEspera() {
        if (porConfirmar.isEmpty()) return
        handler.removeCallbacks(soltarEspera)
        handler.postDelayed(soltarEspera, MAX_ESPERA_RECIBIDA_MS)
    }

    /** Si el celu no confirma (quedó lejos o apagado), igual se suelta el primer plano: el audio queda en el reloj. */
    private val soltarEspera = Runnable {
        porConfirmar.clear()
        detenerSiTermino()
    }

    /**
     * Frena MediaRecorder y deja la grabación terminada. La huella (SHA-256 del archivo entero) se
     * calcula aparte: hasta que esté, el celu ve la grabación terminada sin huella y espera.
     */
    private fun cerrar(g: GrabacionWatch, interrumpida: Boolean, avisarCelu: Boolean = true): GrabacionWatch {
        handler.removeCallbacks(tick)
        val acumulado = cronometro?.pausar() ?: g.acumuladoMs
        recorder?.let { r ->
            // stop() tira RuntimeException si no llegó audio válido (por ejemplo, si se paró enseguida).
            runCatching { r.stop() }.onFailure { Log.w(TAG, "stop() falló", it) }
            r.release()
        }
        recorder = null
        cronometro = null
        ControlWatch.cronometro = null
        grabacion = null
        val terminada = g.copy(
            estado = EstadoGrabacionWatch.TERMINADA,
            acumuladoMs = acumulado,
            desde = null,
            duracionMs = acumulado,
            interrumpida = interrumpida,
            bytes = almacen.bytesFirmes(g.id),
        )
        guardar(terminada)
        publicar()
        if (avisarCelu) EnlaceCelu.avisar(this)
        porConfirmar += g.id
        handler.removeCallbacks(soltarEspera)
        handler.postDelayed(soltarEspera, MAX_ESPERA_RECIBIDA_MS)
        cierresPendientes++
        alcance.launch {
            runCatching {
                val sha = Huella.sha256(almacen.archivoAudio(g.id))
                guardar(terminada.copy(sha256 = sha, bytes = almacen.bytesFirmes(g.id)))
            }.onFailure { Log.e(TAG, "No se pudo calcular la huella de ${g.id}", it) }
            EnlaceCelu.avisar(this@ServicioGrabacionWatch)
            withContext(Dispatchers.Main) {
                cierresPendientes--
                if (!destruido) detenerSiTermino()
            }
        }
        return terminada
    }

    private fun detenerSiTermino() {
        if (grabacion != null || cierresPendientes > 0 || porConfirmar.isNotEmpty()) {
            if (grabacion == null) Avisos.mostrarCerrando(this)
            return
        }
        handler.removeCallbacks(soltarEspera)
        liberarWakeLock()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf(ultimoStartId)
    }

    /**
     * MediaRecorder murió en medio de la clase. Se cierra lo grabado y, como el servicio sigue en
     * primer plano con el micrófono, se sigue enseguida en una grabación nueva que el celu sube como
     * continuación (igual que en el celu).
     */
    private fun errorDeGrabadora(what: Int, extra: Int) {
        val anterior = grabacion ?: return
        Log.e(TAG, "MediaRecorder error what=$what extra=$extra")
        val estabaEnPausa = anterior.estado == EstadoGrabacionWatch.PAUSADA
        // Se le avisa al celu recién con la grabación nueva armada: si viera A terminada sin B, daría
        // la clase por terminada en vez de seguir en la continuación.
        cerrar(anterior, interrumpida = true, avisarCelu = false)
        val ahora = SystemClock.elapsedRealtime()
        reinicios.removeAll { ahora - it > VENTANA_REINICIOS_MS }
        if (reinicios.size < MAX_REINICIOS) {
            reinicios += ahora
            val continuacion = anterior.copy(
                id = IdGrabacion.nuevo(),
                estado = EstadoGrabacionWatch.GRABANDO,
                inicio = ahoraIso(),
                bytes = 0,
                acumuladoMs = 0,
                desde = null,
                duracionMs = null,
                sha256 = null,
                interrumpida = false,
                continuaDe = anterior.id,
            )
            if (arrancar(continuacion, enPausa = estabaEnPausa)) return
        } else {
            fallar("La grabación se cortó (error $what/$extra). Lo grabado quedó guardado.")
        }
        EnlaceCelu.avisar(this)
        detenerSiTermino()
    }

    // ---- Auxiliares ----

    private fun cambiar(g: GrabacionWatch) {
        grabacion = g
        guardar(g)
        publicar()
        EnlaceCelu.avisar(this)
    }

    private fun guardar(g: GrabacionWatch) {
        runCatching { almacen.guardar(g) }.onFailure { Log.e(TAG, "No se pudo guardar ${g.id}", it) }
    }

    private fun olvidarPedido(id: String) {
        if (almacen.leerPedido()?.id == id) almacen.guardarPedido(null)
        getSystemService(NotificationManager::class.java).cancel(Avisos.ID_PEDIDO)
        ControlWatch.refrescar(this)
        EnlaceCelu.avisar(this)
    }

    private fun fallar(mensaje: String) {
        recorder?.release()
        recorder = null
        grabacion = null
        cronometro = null
        ControlWatch.cronometro = null
        publicar(error = mensaje)
        EnlaceCelu.avisar(this)
        detenerSiTermino()
    }

    private fun publicar(error: String? = ControlWatch.estado.value.error) {
        val g = grabacion
        ControlWatch.estadoMutable.update { it.copy(grabacion = g, error = error) }
        ControlWatch.refrescar(this)
        grabacion?.let { g -> runCatching { Avisos.actualizarGrabando(this, g, ControlWatch.offsetActualMs()) } }
    }

    private val tick = object : Runnable {
        override fun run() {
            val g = grabacion ?: return
            ticks++
            // Si nadie la termina (el celu quedó lejos y nadie tocó el reloj), no graba para siempre.
            if (SystemClock.elapsedRealtime() - iniciadaEn > MAX_GRABACION_MS) {
                Log.w(TAG, "La grabación ${g.id} llegó al tope de duración: se termina sola")
                terminar()
                return
            }
            revisarCrecimiento(g)
            revisarMicrofono()
            if (ticks % TICKS_POR_LATIDO == 0L) {
                adquirirWakeLock()
                // Desde Android 14 la notificación se puede descartar: se vuelve a poner.
                runCatching { Avisos.actualizarGrabando(this@ServicioGrabacionWatch, g, ControlWatch.offsetActualMs()) }
                // Latido: el celu se entera de cuánto audio hay aunque no pregunte.
                EnlaceCelu.avisar(this@ServicioGrabacionWatch)
            }
            handler.postDelayed(this, INTERVALO_TICK_MS)
        }
    }

    /** OggWriter ignora los errores de escritura: si el archivo deja de crecer mientras graba, se avisa. */
    private fun revisarCrecimiento(g: GrabacionWatch) {
        val largo = almacen.archivoAudio(g.id).length()
        val grabando = g.estado == EstadoGrabacionWatch.GRABANDO
        ticksSinCrecer = if (grabando && largo == ultimoLargo) ticksSinCrecer + 1 else 0
        ultimoLargo = largo
        if (ticksSinCrecer == 3) {
            Log.e(TAG, "El audio no crece desde hace ${ticksSinCrecer * INTERVALO_TICK_MS / 1000} s ($largo bytes)")
            publicar(error = "El audio dejó de crecer: puede que el reloj no tenga espacio o que el micrófono esté ocupado.")
            EnlaceCelu.avisar(this)
        } else if (ticksSinCrecer == 0 && ControlWatch.estado.value.error?.startsWith("El audio dejó de crecer") == true) {
            publicar(error = null)
        }
    }

    /** Si otra captura le saca el micrófono, Android le da silencio: se avisa en el reloj y al celu. */
    private fun revisarMicrofono() {
        val ahora = runCatching { recorder?.activeRecordingConfiguration?.isClientSilenced }.getOrNull() ?: false
        if (ahora == silenciada) return
        silenciada = ahora
        Log.w(TAG, if (ahora) "El micrófono quedó silenciado" else "El micrófono volvió")
        publicar(error = if (ahora) MICROFONO_OCUPADO else ControlWatch.estado.value.error?.takeIf { it != MICROFONO_OCUPADO })
        EnlaceCelu.avisar(this)
    }

    private fun crearRecorder(archivo: File): MediaRecorder {
        val ajustes = AjustesWatch(this)
        val r = MediaRecorder(this)
        r.setAudioSource(ajustes.fuente)
        // Otra app (o el Asistente) no puede dejar la clase en silencio por usar el micrófono. Va justo
        // después de la fuente: después de setOutputFormat tira IllegalStateException.
        r.setPrivacySensitive(true)
        r.setOutputFormat(MediaRecorder.OutputFormat.OGG)
        r.setAudioEncoder(MediaRecorder.AudioEncoder.OPUS)
        r.setAudioChannels(1)
        r.setAudioSamplingRate(48_000)
        r.setAudioEncodingBitRate(ajustes.bitrate)
        r.setOutputFile(archivo)
        // Sólo cuenta un error de la grabadora actual (no uno atrasado de la que ya se reemplazó).
        r.setOnErrorListener { mr, what, extra -> handler.post { if (mr === recorder) errorDeGrabadora(what, extra) } }
        Log.i(TAG, "Grabando con ${AjustesWatch.nombreFuente(ajustes.fuente)} a ${ajustes.bitrate} bps")
        return r
    }

    private fun adquirirWakeLock() {
        val wl = wakeLock ?: (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "classmer:grabacion-reloj")
            .also {
                it.setReferenceCounted(false)
                wakeLock = it
            }
        // Con plazo, por las dudas; se renueva en cada latido mientras se graba.
        wl.acquire(10 * 60 * 1000L)
    }

    private fun liberarWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    companion object {
        private const val TAG = "ClassmerWatchGrabacion"
        const val ID_NOTIFICACION = 1

        private const val INTERVALO_TICK_MS = 5_000L

        /** Cada 12 ticks (~1 min): latido al celu y se renueva la notificación. */
        private const val TICKS_POR_LATIDO = 12L

        /** Tope de una grabación (hora real, con las pausas): más que cualquier clase. */
        private const val MAX_GRABACION_MS = 5 * 60 * 60 * 1000L

        /** Cuánto se espera, después de terminar, que el celu confirme que recibió todo. */
        private const val MAX_ESPERA_RECIBIDA_MS = 10 * 60 * 1000L

        /** Si MediaRecorder falla, se reintenta seguir grabando hasta 3 veces cada 10 minutos. */
        private const val MAX_REINICIOS = 3
        private const val VENTANA_REINICIOS_MS = 10 * 60 * 1000L

        private const val MICROFONO_OCUPADO = "Otra app está usando el micrófono del reloj: se graba silencio hasta que lo suelte."

        const val ACCION_INICIAR = "ar.classmer.wear.INICIAR"
        const val ACCION_PAUSAR = "ar.classmer.wear.PAUSAR"
        const val ACCION_REANUDAR = "ar.classmer.wear.REANUDAR"
        const val ACCION_TERMINAR = "ar.classmer.wear.TERMINAR"
        const val ACCION_RECIBIDA = "ar.classmer.wear.RECIBIDA"
        const val ACCION_ENVIANDO = "ar.classmer.wear.ENVIANDO"

        const val EXTRA_ID = "id"
        const val EXTRA_MATERIA = "materia"
        const val EXTRA_TIPO_CLASE = "tipo_clase"
        const val EXTRA_RESPUESTA_A = "respuesta_a"
        const val EXTRA_NODO = "nodo"

        fun ahoraIso(): String =
            OffsetDateTime.now(ZoneId.systemDefault()).truncatedTo(ChronoUnit.SECONDS).format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)
    }
}
