package ar.classmer.grabacion

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioDeviceInfo
import android.media.MediaRecorder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import ar.classmer.ClassmerApp
import ar.classmer.MainActivity
import ar.classmer.R
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Evento
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.OrigenMateria
import ar.classmer.modelo.Pista
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.TipoEvento
import ar.classmer.modelo.TipoMarca
import ar.classmer.subida.PlanificadorSubida
import ar.classmer.subida.SincronizacionSubida
import ar.classmer.util.Tiempo
import ar.classmer.util.Uuid7
import ar.classmer.watch.Ambos
import ar.classmer.watch.EnlaceWatch
import ar.classmer.watch.PistaCelu
import ar.classmer.watch.PistasCelu
import ar.classmer.watch.SesionWatch
import ar.classmer.watch.SincronizacionWatch
import ar.classmer.watch.estadoAlEnganchar
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File

/**
 * Servicio en primer plano de tipo `microphone`: graba la clase entera en un único Ogg/Opus
 * (mono, 32 kbps) y anota fotos, notas, marcas y pausas en `eventos.jsonl` con su `offset_ms`.
 * Sigue grabando con la pantalla bloqueada.
 *
 * Todas las órdenes llegan como intents (desde la pantalla o desde la notificación) y se atienden
 * en el hilo principal, así que no hay dos cambios de estado a la vez.
 */
class ServicioGrabacion : Service() {

    private val handler = Handler(Looper.getMainLooper())
    private lateinit var almacen: AlmacenSesiones

    private var sesion: SesionLocal? = null
    private var recorder: MediaRecorder? = null
    private var reloj: RelojGrabacion? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var ticks = 0L
    private var avisos = 0L

    /** Subidas directas mientras se graba (ver [SincronizacionSubida]). */
    private val alcance = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var subidaEnCurso: Job? = null

    /** La subida de lo último y el `/finalizar` al terminar, todavía en primer plano. */
    private var subidaFinal: Job? = null

    /** El último start recibido: stopSelf(ultimoStartId) no corta una orden que llegó después. */
    private var ultimoStartId = 0

    /** Cuándo se reinició la grabadora tras un error (para no reintentar sin fin). */
    private val reinicios = mutableListOf<Long>()

    /** Tipo del dispositivo de entrada que está grabando (para avisar si no es el micrófono del celu). */
    private var ultimaEntrada: Int? = null

    /** Para detectar que el audio dejó de crecer (disco lleno, micrófono caído). */
    private var ultimoLargoAudio = -1L
    private var ticksSinCrecer = 0
    private var avisoSinCrecer = false

    /** Desde cuándo se pregunta "¿Sigue la clase?" (`elapsedRealtime`); `null` = no hay pregunta abierta. */
    private var preguntaDesde: Long? = null

    /** La pregunta se pudo mostrar como notificación: sólo entonces la grabación puede terminar sola. */
    private var preguntaVisible = false

    /** Tiempos cortos del corte automático para probarlo en el celu (ver [Ajustes.pruebaCorteSeg]); `null` = los reales. */
    private var pruebaCorteMs: Long? = null

    /** Graba el reloj (Galaxy Watch): el audio llega por Bluetooth en vez de MediaRecorder ([SesionWatch]). */
    private var watch: SesionWatch? = null

    /** El reloj espejo del audio del Watch (lo último que mandó [SesionWatch]). */
    private var espejo: RelojGrabacion? = null

    /** Ambos: la grabadora de la pista del celu (`audio.celu.ogg`, ver [PistaCelu]) mientras graba también el reloj. */
    private var grabadoraCelu: MediaRecorder? = null

    /** Ambos: lo grabado en la pista del celu, sin las pausas. */
    private var relojCelu: RelojGrabacion? = null

    /** Ambos: la grabación del reloj se cortó y la clase sigue con la pista del celu. */
    private var relojCortado = false

    /** El servicio está en primer plano con el micrófono (tipo `microphone`): puede grabar con el celu. */
    private var conMicrofono = false

    override fun onCreate() {
        super.onCreate()
        almacen = AlmacenSesiones.de(this)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ultimoStartId = startId
        when (intent?.action) {
            ACCION_INICIAR -> iniciar(intent)
            ACCION_MARCA -> marcar(intent.getStringExtra(EXTRA_MARCA))
            ACCION_NOTA -> anotar(intent.getStringExtra(EXTRA_TEXTO), intent.getLongExtra(EXTRA_OFFSET, -1), pistaDe(intent))
            ACCION_FOTO -> registrarFoto(
                intent.getStringExtra(EXTRA_EVENTO_ID),
                intent.getLongExtra(EXTRA_OFFSET, -1),
                intent.getStringExtra(EXTRA_ARCHIVO),
                pistaDe(intent),
            )
            ACCION_PAUSAR -> pausar()
            ACCION_REANUDAR -> reanudar()
            ACCION_TERMINAR -> terminar()
            ACCION_SEGUIR -> seguirClase()
            ACCION_ENGANCHAR -> enganchar(intent.getStringExtra(EXTRA_SESION_ID), intent.getBooleanExtra(EXTRA_PISTA_CELU, false))
            ACCION_USAR_TELEFONO -> usarTelefono()
        }
        // Una orden vieja (de una notificación que quedó) sin grabación activa: no hay nada que hacer.
        // También pasa cuando Android vuelve a levantar el servicio tras matar el proceso (START_STICKY):
        // ClassmerApp ya cerró la sesión cortada y avisó; el micrófono no se puede reabrir desde atrás.
        if (sesion == null && subidaFinal?.isActive != true) stopSelf(startId)
        return if (sesion != null) START_STICKY else START_NOT_STICKY
    }

    override fun onDestroy() {
        // Sólo pasa si el sistema detiene el servicio mientras graba: se cierra la sesión y se avisa.
        val grabado = ControlGrabacion.offsetActualMs()
        val w = watch
        if (w != null && grabadoraCelu != null && !w.empezo()) {
            // Ambos y el reloj todavía no empezó: el celu era lo único que grababa. Se cierra como una del
            // teléfono (o, si el pedido le llegó al reloj, esperando que confirme si la tiene).
            w.soltar()
            cerrarSesionActual(interrumpida = true)
            publicar(EstadoGrabacion(error = "La grabación se cortó. Lo grabado quedó guardado."))
            ClassmerApp.avisarCorte(this, grabado)
            PlanificadorSubida.patear(this)
        } else if (watch != null && relojCortado) {
            // Ambos con el reloj ya cortado: el celu era lo único que grababa. Su pista vale hasta acá; la
            // clase la cierra SincronizacionWatch cuando termina de llegar lo que grabó el reloj.
            cerrarPistaCelu(interrumpida = true)
            soltarWatch()
            publicar(EstadoGrabacion(error = "La grabación se cortó. Lo grabado quedó guardado."))
            ClassmerApp.avisarCorte(this, grabado)
        } else if (watch != null) {
            // El reloj sigue grabando: la sesión queda "grabando" y se vuelve a enganchar al abrir Classmer.
            // La pista del celu (Ambos) se cierra acá: vale lo grabado hasta ahora.
            cerrarPistaCelu(interrumpida = true)
            soltarWatch()
            publicar(EstadoGrabacion(error = "El celu dejó de seguir al reloj, que sigue grabando. Abrí Classmer para seguir la clase."))
        } else if (cerrarSesionActual(interrumpida = true) != null) {
            publicar(EstadoGrabacion(error = "La grabación se cortó. Lo grabado quedó guardado."))
            ClassmerApp.avisarCorte(this, grabado)
            PlanificadorSubida.patear(this)
        }
        liberarWakeLock()
        alcance.cancel()
        super.onDestroy()
    }

    // ---- Órdenes ----

    private fun iniciar(intent: Intent) {
        if (sesion != null) {
            actualizarNotificacion()
            return
        }
        val materiaId = intent.getStringExtra(EXTRA_MATERIA_ID)
        val origen = intent.getStringExtra(EXTRA_ORIGEN) ?: OrigenMateria.AUTO
        pruebaCorteMs = Ajustes(this).tomarPruebaCorteSeg().takeIf { it > 0 }?.let { it * 1000L }
        pruebaCorteMs?.let { Log.w(TAG, "Corte automático en modo prueba: $it ms") }
        // El fin del evento sólo cuenta si la materia salió del calendario (corte automático, §4).
        val finEvento = intent.getLongExtra(EXTRA_FIN_EVENTO, -1).takeIf { it > 0 && origen == OrigenMateria.CALENDARIO }
        val pedida = intent.getStringExtra(EXTRA_FUENTE) ?: FuenteAudio.TELEFONO
        // Con Ambos la clase es del reloj (el principal) y el celu graba además su pista.
        val ambos = pedida == FuenteAudio.AMBOS
        val fuente = if (ambos) FuenteAudio.RELOJ else pedida
        val nueva = SesionLocal(
            id = Uuid7.nuevo(),
            materiaId = materiaId,
            origenMateria = origen,
            tipoClase = intent.getStringExtra(EXTRA_TIPO_CLASE) ?: "teorica",
            inicio = Tiempo.ahoraIso(),
            dispositivo = if (fuente == FuenteAudio.RELOJ) {
                SincronizacionWatch.nombreDispositivo(EnlaceWatch.info.value.estado?.modelo.orEmpty())
            } else {
                Ajustes(this).dispositivo
            },
            numeroClase = intent.getIntExtra(EXTRA_NUMERO_CLASE, -1).takeIf { it > 0 && materiaId != null },
            nombreMateria = intent.getStringExtra(EXTRA_NOMBRE_MATERIA),
            eventoCalendario = intent.getStringExtra(EXTRA_EVENTO_CALENDARIO),
            finEventoMs = finEvento,
            preguntarCorteMs = pruebaCorteMs?.let { System.currentTimeMillis() + it }
                ?: CorteAutomatico.primeraPregunta(System.currentTimeMillis(), finEvento),
            fuenteAudio = fuente,
            pistaCelu = ambos,
        )
        cerrarPreguntaCorte()
        sesion = nueva

        // startForegroundService() obliga a llamar startForeground() enseguida, aunque después falle algo.
        val microfono = fuente != FuenteAudio.RELOJ || ambos
        try {
            ServiceCompat.startForeground(this, ID_NOTIFICACION, construirNotificacion(), tipoServicio(microfono, reloj = fuente == FuenteAudio.RELOJ))
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo pasar a primer plano", e)
            fallarInicio("Android no dejó empezar a grabar en segundo plano (${e.message}). Probá de nuevo con la app abierta.")
            return
        }
        conMicrofono = microfono
        if (fuente == FuenteAudio.RELOJ) {
            arrancarWatch(nueva, enganchar = false)
            // El celu empieza enseguida; el reloj, cuando se toque Grabar allá.
            if (ambos && sesion?.id == nueva.id) arrancarPistaCelu(nueva)
        } else {
            arrancarGrabadora(nueva)
        }
    }

    // ---- Cuando graba el reloj (ver ar.classmer.watch) ----

    /**
     * Graba el reloj: la sesión se arma igual que siempre, pero el audio lo trae [SesionWatch]. Al seguir una
     * clase que ya graba el reloj, con [conCelu] (Grabar con Ambos) el celu suma su pista.
     */
    private fun arrancarWatch(nueva: SesionLocal, enganchar: Boolean, conCelu: Boolean = false) {
        sesion = nueva
        try {
            if (!enganchar) almacen.crear(nueva)
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo crear la sesión", e)
            fallarInicio("No se pudo guardar la clase: ${e.message ?: e.javaClass.simpleName}")
            return
        }
        soltarWatch()
        val id = nueva.id
        val w = SesionWatch(this, id, alcance) { cambioWatch(id, it) }
        watch = w
        // Al volver a seguir una clase, hasta que conteste el reloj, las marcas usan lo grabado según el
        // último guardado de la sesión (no 0:00). Al pedir una clase nueva, el reloj todavía no empezó.
        val enPausa = enganchar && SesionWatch.estabaEnPausa(almacen, nueva.id)
        val provisorio = if (enganchar) {
            RelojGrabacion(acumuladoInicialMs = SesionWatch.offsetProvisional(almacen, nueva, enPausa)).also { if (!enPausa) it.iniciar() }
        } else {
            null
        }
        espejo = provisorio
        relojCortado = false
        usarReloj(provisorio, null)
        if (enganchar) w.enganchar() else w.iniciar(nueva.nombreMateria, nueva.tipoClase)
        adquirirWakeLock()
        publicar(estadoAlEnganchar(almacen, nueva, w.ui).copy(pausada = enPausa))
        actualizarNotificacion()
        ticks = 0
        handler.removeCallbacks(tick)
        handler.postDelayed(tick, INTERVALO_TICK_MS)
        if (enganchar) pistaCeluAlEnganchar(nueva, conCelu)
        subirAhora()
    }

    // ---- Ambos: la pista del celu (ver PistaCelu) ----

    /**
     * Al seguir una clase del reloj que ya venía: una pista del celu que quedó de antes (la app se cayó) se
     * cierra con lo que tiene, porque una clase lleva una sola. Si no tiene, el celu empieza una si puede
     * usar el micrófono: Grabar con Ambos con la app a la vista, o la clase que sigue a una falla del reloj.
     */
    private fun pistaCeluAlEnganchar(s: SesionLocal, conCelu: Boolean) {
        val pistas = PistasCelu(almacen)
        when {
            pistas.leer(s.id) != null || pistas.audio(s.id).length() > 0 -> pistas.cerrarSiQuedoAbierta(s.id)
            (s.pistaCelu || conCelu) && conMicrofono -> {
                val conPista = s.copy(pistaCelu = true)
                sesion = conPista
                runCatching { almacen.guardarSesion(conPista) }
                arrancarPistaCelu(conPista)
            }
            s.pistaCelu -> sinPistaCelu(s, null)
        }
    }

    /**
     * Empieza la pista del celu de [s] (el servicio ya tiene el micrófono). Mientras el reloj no empieza, los
     * minutos se cuentan en esta pista. Si no arranca, la clase sigue sólo con el reloj.
     */
    private fun arrancarPistaCelu(s: SesionLocal): Boolean {
        val pistas = PistasCelu(almacen)
        val archivo = pistas.audio(s.id)
        // MediaRecorder vacía el archivo al prepararse: una pista que ya tiene audio nunca se pisa.
        if (archivo.length() > 0) {
            Log.w(TAG, "La pista del celu de ${s.id} ya tiene audio: no se graba otra")
            pistas.cerrarSiQuedoAbierta(s.id)
            return false
        }
        var r: MediaRecorder? = null
        try {
            r = crearRecorder(archivo)
            r.prepare()
            // celu.json antes de empezar: sin él, la pista no se sube.
            pistas.guardar(s.id, PistaCelu(inicioMs = System.currentTimeMillis()))
            r.start()
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo grabar la pista del celu", e)
            r?.release()
            // MediaRecorder no llegó a escribir audio: no queda una pista vacía.
            if (archivo.length() == 0L) {
                archivo.delete()
                pistas.archivo(s.id).delete()
            }
            sinPistaCelu(s, "⚠ El celu no pudo grabar su audio (${e.message ?: e.javaClass.simpleName}): graba sólo el reloj")
            return false
        }
        val rc = RelojGrabacion().also { it.iniciar() }
        grabadoraCelu = r
        relojCelu = rc
        runCatching { pistas.cambiar(s.id) { it.copy(inicioMs = System.currentTimeMillis()) } }
        if (watch?.empezo() != true) usarReloj(rc, Pista.CELU)
        // Se suma a una clase del reloj que está en pausa (por ejemplo, el recreo): empieza en pausa.
        else if (ControlGrabacion.estado.value.pausada) seguirPausaDelReloj(true)
        publicar(ControlGrabacion.estado.value)
        return true
    }

    /** La clase sigue sin pista del celu (no arrancó, o no se puede usar el micrófono). */
    private fun sinPistaCelu(s: SesionLocal, aviso: String?) {
        val sin = s.copy(pistaCelu = false)
        if (sesion?.id == s.id) sesion = sin
        runCatching { almacen.guardarSesion(sin) }
        publicar(ControlGrabacion.estado.value.copy(sesion = sesion), aviso = aviso)
    }

    /** Cierra la grabadora de la pista del celu y deja `celu.json` con lo definitivo. */
    private fun cerrarPistaCelu(interrumpida: Boolean) {
        val r = grabadoraCelu ?: return
        val id = sesion?.id
        runCatching { r.stop() }.onFailure { Log.w(TAG, "stop() de la pista del celu falló", it) }
        r.release()
        grabadoraCelu = null
        val duracion = relojCelu?.pausar() ?: 0
        if (id != null) {
            runCatching { PistasCelu(almacen).cambiar(id) { it.copy(terminada = true, interrumpida = interrumpida, duracionMs = duracion) } }
                .onFailure { Log.e(TAG, "No se pudo cerrar celu.json", it) }
        }
    }

    /**
     * Con los dos grabando, la pista del celu sigue las pausas del reloj (así el desfase vale para toda la
     * clase). Después de la primera, el desfase queda fijo.
     */
    private fun seguirPausaDelReloj(pausada: Boolean) {
        val r = grabadoraCelu ?: return
        val rc = relojCelu ?: return
        if (relojCortado || pausada == !rc.corriendo) return
        try {
            if (pausada) r.pause() else r.resume()
        } catch (e: Exception) {
            Log.w(TAG, "La pista del celu no pudo ${if (pausada) "pausar" else "reanudar"}", e)
            return
        }
        if (pausada) {
            rc.pausar()
            fijarDesfaseCelu()
        } else {
            rc.iniciar()
        }
    }

    /** Hubo una pausa: el desfase queda el de antes (ver [Ambos.medir]). */
    private fun fijarDesfaseCelu() {
        val s = sesion?.takeIf { it.pistaCelu } ?: return
        runCatching { PistasCelu(almacen).cambiar(s.id) { if (it.desfaseMs != null) it.copy(desfaseFijo = true) else it } }
    }

    /** Con los dos grabando, lo grabado por cada uno en este instante da el desfase de la pista ([Ambos.medir]). */
    private fun medirDesfaseCelu() {
        val s = sesion ?: return
        val w = watch ?: return
        val rc = relojCelu ?: return
        if (relojCortado || grabadoraCelu == null || !rc.corriendo) return
        val (grabadoReloj, error) = w.grabadoEn(SystemClock.elapsedRealtime()) ?: return
        val grabadoCelu = rc.offsetMs()
        runCatching { PistasCelu(almacen).cambiar(s.id) { Ambos.medir(it, grabadoReloj, error, grabadoCelu) } }
    }

    /** La grabadora de la pista del celu falló: la clase sigue con el reloj (si se había cortado, termina). */
    private fun errorPistaCelu(what: Int, extra: Int) {
        Log.e(TAG, "MediaRecorder de la pista del celu: error what=$what extra=$extra")
        val cortado = relojCortado
        cerrarPistaCelu(interrumpida = true)
        relojCelu = null
        if (cortado) {
            publicar(ControlGrabacion.estado.value, aviso = "⚠ El celu dejó de grabar y el reloj ya se había cortado")
            ClassmerApp.avisar(this, "La grabación se cortó", "El reloj se había cortado y ahora falló el micrófono del celu. Lo grabado quedó guardado.")
            terminar()
            return
        }
        if (ControlGrabacion.pistaDelReloj == Pista.CELU) usarReloj(espejo, null)
        publicar(ControlGrabacion.estado.value, aviso = "⚠ El celu dejó de grabar su audio: sigue el reloj")
    }

    /** El reloj que da los minutos de las fotos, notas y marcas, y en qué audio se cuentan. */
    private fun usarReloj(r: RelojGrabacion?, pista: String?) {
        reloj = r
        ControlGrabacion.reloj = r
        ControlGrabacion.pistaDelReloj = pista
    }

    /** Lo que dice la pantalla de grabación de la pista del celu. */
    private fun textoCelu(): String? = when {
        sesion?.pistaCelu != true -> null
        grabadoraCelu == null -> "📱 El celu dejó de grabar su audio: sigue el reloj"
        relojCortado && relojCelu?.corriendo == false -> "📱 El reloj se cortó en la pausa: Reanudar sigue con el celu"
        relojCortado -> "📱 El reloj se cortó: la clase sigue con el audio del celu"
        relojCelu?.corriendo == false -> "📱 El celu también graba (en pausa)"
        else -> "📱 El celu también graba, de respaldo"
    }

    /** Lo que avisa [SesionWatch], en el hilo principal. Sólo cuenta para la clase que se está siguiendo. */
    private fun cambioWatch(id: String, c: SesionWatch.Cambio) {
        val s = sesion?.takeIf { it.id == id && watch?.sesionId == id } ?: return
        val e = ControlGrabacion.estado.value
        when (c) {
            is SesionWatch.Cambio.Estado -> publicar(e.copy(watch = c.ui))
            // La clase empieza cuando empieza el audio del reloj, no cuando se tocó Grabar en el celu.
            is SesionWatch.Cambio.Empezo -> if (c.inicio.isNotBlank() && c.inicio != s.inicio) {
                sesion = s.copy(inicio = c.inicio)
                runCatching { almacen.guardarSesion(s.copy(inicio = c.inicio)) }
            }
            is SesionWatch.Cambio.Aviso -> publicar(e, aviso = c.texto)
            is SesionWatch.Cambio.Reloj -> {
                espejo = c.reloj
                // Ambos con el reloj cortado: los minutos y las pausas son los de la pista del celu.
                if (relojCortado) return
                usarReloj(c.reloj, null)
                seguirPausaDelReloj(c.pausada)
                medirDesfaseCelu()
                publicar(e.copy(pausada = c.pausada))
            }
            is SesionWatch.Cambio.Pausa -> {
                fijarDesfaseCelu()
                agregar(s, Evento(Uuid7.nuevo(), TipoEvento.PAUSA, c.offsetMs, Tiempo.ahoraIso()))
                publicar(e.copy(pausada = true), aviso = "⏸ En pausa en ${Tiempo.duracion(c.offsetMs)}")
            }
            is SesionWatch.Cambio.Reanudacion -> {
                fijarDesfaseCelu()
                agregar(s, Evento(Uuid7.nuevo(), TipoEvento.REANUDAR, c.offsetMs, Tiempo.ahoraIso()))
                publicar(e.copy(pausada = false), aviso = "▶ Grabando de nuevo")
            }
            // Ambos: si la grabación del reloj se cortó (no la terminaron), la clase sigue con el celu. Su audio
            // hasta el corte sigue llegando; los minutos de acá en adelante se cuentan en la pista del celu.
            is SesionWatch.Cambio.TerminoEnWatch -> if (c.interrumpida && grabadoraCelu != null && relojCelu != null && !relojCortado) {
                relojCortado = true
                usarReloj(relojCelu, Pista.CELU)
                val enPausa = relojCelu?.corriendo == false
                publicar(
                    e.copy(pausada = enPausa),
                    aviso = if (enPausa) "⚠ El reloj se cortó en la pausa: tocá Reanudar para seguir con el celu" else "⚠ La grabación del reloj se cortó: sigue grabando el celu",
                )
                ClassmerApp.avisar(
                    this,
                    "La grabación del reloj se cortó",
                    if (enPausa) {
                        "Se cortó durante la pausa. Para seguir la clase con el audio del celu, tocá Reanudar. Lo que grabó el reloj se pasa al celu y se sube."
                    } else {
                        "La clase sigue con el audio del celu. Lo que grabó el reloj hasta el corte se pasa al celu y se sube."
                    },
                )
            } else {
                ClassmerApp.avisar(
                    this,
                    if (c.interrumpida) "La grabación del reloj se cortó" else "El reloj terminó la grabación",
                    "La clase terminó también en el celu. El audio del reloj se pasa al celu y se sube.",
                )
                terminar()
            }
            // La grabadora del reloj falló y siguió en una grabación nueva: esta parte se cierra (su audio
            // sigue llegando) y la clase sigue enseguida en la sesión nueva, como cuando falla la del celu.
            is SesionWatch.Cambio.Continuacion -> {
                terminar()
                val nueva = almacen.leerSesion(c.g.id)
                    ?: SincronizacionWatch.continuacion(s, c.g).also { runCatching { almacen.crear(it) } }
                if (sesion == null && nueva.estadoLocal == EstadoLocal.GRABANDO) {
                    arrancarWatch(nueva, enganchar = true)
                    publicar(ControlGrabacion.estado.value, aviso = "⚠ La grabación del reloj se cortó y siguió en una sesión nueva")
                }
            }
        }
    }

    /**
     * Seguir en el celu una clase que ya está grabando el reloj (empezó allá, o el celu se reinició). Con
     * [conCelu] (se tocó Grabar con Ambos) el celu intenta sumar su pista: Android sólo le da el micrófono
     * si la app está a la vista; si no, la sigue sin pista.
     */
    private fun enganchar(id: String?, conCelu: Boolean) {
        val actual = sesion
        if (actual != null) {
            // Ya la sigue (por ejemplo, se enganchó sola sin el micrófono): con Ambos y la app a la vista,
            // suma la pista del celu.
            if (conCelu && actual.id == id && watch != null && grabadoraCelu == null) sumarPistaCelu(actual)
            return
        }
        // Llegó con startForegroundService(): hay que pasar a primer plano enseguida, aunque después no haya nada.
        var microfono = conCelu
        try {
            try {
                ServiceCompat.startForeground(this, ID_NOTIFICACION, construirNotificacion(), tipoServicio(microfono, reloj = true))
            } catch (e: Exception) {
                if (!microfono) throw e
                Log.w(TAG, "Sin micrófono para la pista del celu: se sigue al reloj sin ella", e)
                microfono = false
                ServiceCompat.startForeground(this, ID_NOTIFICACION, construirNotificacion(), tipoServicio(microfono = false, reloj = true))
            }
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo pasar a primer plano para seguir al reloj", e)
            if (subidaFinal?.isActive != true) stopSelf(ultimoStartId)
            return
        }
        conMicrofono = microfono
        val s = paraEnganchar(id?.let { almacen.leerSesion(it) })
        if (s == null) {
            if (subidaFinal?.isActive == true) mostrarNotificacionSubiendo() else detenerServicio()
            return
        }
        arrancarWatch(s, enganchar = true, conCelu = microfono)
    }

    /** Grabar con Ambos sobre una clase que el celu ya seguía sin su pista: se pide el micrófono y se empieza. */
    private fun sumarPistaCelu(s: SesionLocal) {
        if (!conMicrofono) {
            try {
                ServiceCompat.startForeground(this, ID_NOTIFICACION, construirNotificacion(), tipoServicio(microfono = true, reloj = true))
            } catch (e: Exception) {
                Log.w(TAG, "Sin micrófono para sumar la pista del celu", e)
                publicar(ControlGrabacion.estado.value, aviso = "⚠ Android no dejó usar el micrófono: graba sólo el reloj")
                return
            }
            conMicrofono = true
        }
        pistaCeluAlEnganchar(s, conCelu = true)
    }

    /** El reloj no empezó a grabar (no responde, o el usuario lo prefiere): la clase se graba con el celu. */
    private fun usarTelefono() {
        val s = sesion ?: return
        val w = watch ?: return
        if (!w.ui.puedeUsarTelefono) return
        w.cancelar()
        watch = null
        val pistaCelu = grabadoraCelu
        if (pistaCelu != null) {
            // Ambos: el celu ya graba desde que se tocó Grabar. Su pista pasa a ser el audio de la clase, sin
            // cortar nada; los minutos ya se contaban en ella.
            recorder = pistaCelu
            grabadoraCelu = null
            usarReloj(relojCelu, Pista.CELU)
            relojCelu = null
            espejo = null
            val nueva = Ambos.comoTelefono(s, Ajustes(this).dispositivo, PistasCelu(almacen).leer(s.id))
            sesion = nueva
            runCatching { almacen.guardarSesion(nueva) }.onFailure { Log.e(TAG, "No se pudo guardar el cambio al teléfono", it) }
            ultimoLargoAudio = -1
            ticksSinCrecer = 0
            publicar(estadoAlEnganchar(almacen, nueva, null).copy(pausada = ControlGrabacion.estado.value.pausada))
            return
        }
        val nueva = s.copy(fuenteAudio = FuenteAudio.TELEFONO, dispositivo = Ajustes(this).dispositivo)
        sesion = nueva
        try {
            // El usuario tocó el botón con la app a la vista: Android deja sumar el micrófono.
            ServiceCompat.startForeground(this, ID_NOTIFICACION, construirNotificacion(), tipoServicio(microfono = true, reloj = false))
            conMicrofono = true
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo pasar al micrófono del celu", e)
            cerrarSinAudio(nueva)
            fallarInicio("Android no dejó usar el micrófono (${e.message}). Volvé a tocar Grabar con el teléfono.")
            return
        }
        // Las fotos, notas y marcas de mientras se esperaba al reloj siguen contando.
        val empieza = System.currentTimeMillis()
        if (arrancarGrabadora(nueva)) {
            // Ambos y el micrófono del celu había fallado antes: lo que grabó va de segunda pista. Su desfase
            // sale de la hora del propio celu (las dos grabaciones son de acá), así los minutos que se contaron
            // con ella caen bien en el audio nuevo.
            if (nueva.pistaCelu) {
                runCatching {
                    PistasCelu(almacen).cambiar(nueva.id) {
                        if (it.desfaseMs != null) it else it.copy(desfaseMs = it.inicioMs - empieza, desfaseErrorMs = ERROR_MISMO_CELU_MS, desfaseFijo = true)
                    }
                }
            }
            publicar(estadoAlEnganchar(almacen, nueva, null).copy(pausada = ControlGrabacion.estado.value.pausada))
        }
    }

    private fun soltarWatch() {
        watch?.soltar()
        watch = null
    }

    /**
     * Una clase que no llegó a tener audio (no arrancó la grabadora, o el reloj nunca empezó): si tiene
     * fotos o notas se guarda cerrada, para no perderlas; si no tiene nada, se borra.
     */
    private fun cerrarSinAudio(s: SesionLocal) {
        if (SincronizacionWatch.descartarSiVacia(almacen, s)) return
        runCatching { almacen.guardarSesion(s.copy(estadoLocal = EstadoLocal.TERMINADA, fin = Tiempo.ahoraIso(), interrumpida = true)) }
    }

    /**
     * Crea la carpeta de la sesión y arranca MediaRecorder. El servicio ya tiene que estar en
     * primer plano. Si falla, no deja ninguna sesión a medias y avisa.
     */
    private fun arrancarGrabadora(nueva: SesionLocal, enPausa: Boolean = false): Boolean {
        sesion = nueva
        val nuevoReloj = RelojGrabacion()
        var nuevoRecorder: MediaRecorder? = null
        try {
            almacen.crear(nueva)
            nuevoRecorder = crearRecorder(almacen.archivoAudio(nueva))
            nuevoRecorder.prepare()
            nuevoRecorder.start()
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo iniciar MediaRecorder", e)
            nuevoRecorder?.release()
            // Todavía no hay nada grabado: se borra la carpeta vacía para que no quede una sesión fantasma
            // (salvo que tenga fotos o notas, por ejemplo de mientras se esperaba al reloj).
            cerrarSinAudio(nueva)
            fallarInicio("No se pudo usar el micrófono: ${e.message ?: e.javaClass.simpleName}")
            return false
        }
        val grabadora = checkNotNull(nuevoRecorder)
        recorder = grabadora
        nuevoReloj.iniciar()
        usarReloj(nuevoReloj, null)
        // Una continuación de una clase que estaba en pausa arranca en pausa (no se graba lo que se pausó).
        val pausada = enPausa && runCatching { grabadora.pause() }.isSuccess
        if (pausada) {
            val offset = nuevoReloj.pausar()
            agregar(nueva, Evento(Uuid7.nuevo(), TipoEvento.PAUSA, offset, Tiempo.ahoraIso()))
        }
        adquirirWakeLock()
        publicar(EstadoGrabacion(sesion = nueva, pausada = pausada))
        actualizarNotificacion()
        ticks = 0
        ultimoLargoAudio = -1
        ticksSinCrecer = 0
        avisoSinCrecer = false
        ultimaEntrada = null
        handler.removeCallbacks(tick)
        handler.postDelayed(tick, INTERVALO_TICK_MS)
        avisarSiQuedaPocoEspacio(almacen.dir(nueva.id))
        subirAhora()
        return true
    }

    private fun fallarInicio(mensaje: String) {
        recorder?.release()
        recorder = null
        grabadoraCelu?.release()
        grabadoraCelu = null
        relojCelu = null
        espejo = null
        relojCortado = false
        sesion = null
        usarReloj(null, null)
        publicar(EstadoGrabacion(error = mensaje))
        if (subidaFinal?.isActive == true) {
            // Hay una subida final de la clase anterior: el servicio sigue hasta que termine.
            handler.removeCallbacks(tick)
            mostrarNotificacionSubiendo()
        } else {
            detenerServicio()
        }
    }

    private fun detenerServicio() {
        handler.removeCallbacks(tick)
        liberarWakeLock()
        conMicrofono = false
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf(ultimoStartId)
    }

    private fun marcar(marca: String?) {
        val s = sesion ?: return
        if (marca != TipoMarca.IMPORTANTE && marca != TipoMarca.NO_ENTENDI) return
        val offset = ControlGrabacion.offsetActualMs()
        val evento = Evento(Uuid7.nuevo(), TipoEvento.MARCA, offset, Tiempo.ahoraIso(), marca = marca, pista = ControlGrabacion.pistaActual())
        if (!agregar(s, evento)) return
        val e = ControlGrabacion.estado.value
        val etiqueta = if (marca == TipoMarca.IMPORTANTE) "⭐ Importante" else "❓ No entendí"
        publicar(
            if (marca == TipoMarca.IMPORTANTE) e.copy(importantes = e.importantes + 1) else e.copy(noEntendi = e.noEntendi + 1),
            aviso = "$etiqueta en ${Tiempo.duracion(offset)}",
        )
    }

    private fun anotar(texto: String?, offsetPedido: Long, pistaPedida: String? = ControlGrabacion.pistaActual()) {
        val s = sesion ?: return
        val limpio = texto?.trim().orEmpty()
        if (limpio.isEmpty()) return
        val (offset, pista) = if (offsetPedido >= 0) offsetPedido to pistaPedida else ControlGrabacion.offsetActualMs() to ControlGrabacion.pistaActual()
        if (!agregar(s, Evento(Uuid7.nuevo(), TipoEvento.NOTA, offset, Tiempo.ahoraIso(), texto = limpio, pista = pista))) return
        val e = ControlGrabacion.estado.value
        publicar(e.copy(notas = e.notas + 1), aviso = "📝 Nota en ${Tiempo.duracion(offset)}")
    }

    private fun registrarFoto(eventoId: String?, offsetPedido: Long, archivo: String?, pistaPedida: String?) {
        val s = sesion ?: return
        if (eventoId == null || archivo == null) return
        val f = File(almacen.dir(s.id), archivo)
        if (!f.isFile || f.length() == 0L) {
            publicar(ControlGrabacion.estado.value, aviso = "La foto no se guardó")
            return
        }
        val (offset, pista) = if (offsetPedido >= 0) offsetPedido to pistaPedida else ControlGrabacion.offsetActualMs() to ControlGrabacion.pistaActual()
        val evento = Evento(eventoId, TipoEvento.FOTO, offset, Tiempo.ahoraIso(), archivo = archivo, pista = pista)
        if (!agregar(s, evento)) return
        almacen.olvidarFotoPendiente(s.id, eventoId)
        val e = ControlGrabacion.estado.value
        publicar(e.copy(fotos = e.fotos + 1), aviso = "📷 Foto en ${Tiempo.duracion(offset)}")
    }

    private fun pausar() {
        val s = sesion ?: return
        // Con el reloj, se pausa el reloj; el evento se anota cuando confirma (con su offset exacto) y la
        // pista del celu (Ambos) lo sigue.
        val (r, rc) = grabadoraQuePausa() ?: return run { watch?.pausar() }
        if (ControlGrabacion.estado.value.pausada) return
        try {
            r.pause()
        } catch (e: Exception) {
            Log.w(TAG, "pause() falló", e)
            publicar(ControlGrabacion.estado.value, aviso = "No se pudo pausar: sigue grabando")
            return
        }
        val offset = rc.pausar()
        agregar(s, Evento(Uuid7.nuevo(), TipoEvento.PAUSA, offset, Tiempo.ahoraIso(), pista = ControlGrabacion.pistaActual()))
        guardarDuracion()
        publicar(ControlGrabacion.estado.value.copy(pausada = true), aviso = "⏸ En pausa en ${Tiempo.duracion(offset)}")
    }

    private fun reanudar() {
        val s = sesion ?: return
        val (r, rc) = grabadoraQuePausa() ?: return run { watch?.reanudar() }
        if (!ControlGrabacion.estado.value.pausada) return
        try {
            r.resume()
        } catch (e: Exception) {
            Log.w(TAG, "resume() falló", e)
            publicar(ControlGrabacion.estado.value, aviso = "No se pudo reanudar: probá de nuevo")
            return
        }
        rc.iniciar()
        val offset = ControlGrabacion.offsetActualMs()
        agregar(s, Evento(Uuid7.nuevo(), TipoEvento.REANUDAR, offset, Tiempo.ahoraIso(), pista = ControlGrabacion.pistaActual()))
        publicar(ControlGrabacion.estado.value.copy(pausada = false), aviso = "▶ Grabando de nuevo")
    }

    /**
     * La grabadora que pausa el celu: la del teléfono o, con Ambos, la de la pista del celu mientras el reloj
     * no graba (todavía no empezó, o se cortó). `null` = la pausa se le pide al reloj.
     */
    private fun grabadoraQuePausa(): Pair<MediaRecorder, RelojGrabacion>? {
        recorder?.let { r -> return reloj?.let { r to it } }
        val r = grabadoraCelu ?: return null
        val rc = relojCelu ?: return null
        val w = watch
        return if (w == null || !w.empezo() || relojCortado) r to rc else null
    }

    /**
     * Terminar clase: cierra la grabación y, todavía en primer plano (con red aunque la pantalla se
     * bloquee), sube lo último y llama a `/finalizar`. WorkManager queda de respaldo.
     */
    private fun terminar() {
        // Una nota que quedó a medio escribir no se pierde: se guarda con su minuto.
        ControlGrabacion.borradorNota?.let { b ->
            if (b.sesionId == sesion?.id && b.texto.isNotBlank()) anotar(b.texto, b.offsetMs, b.pista)
        }
        ControlGrabacion.borradorNota = null
        // Con el reloj: se le pide que termine y la sesión espera que llegue todo su audio. Si el reloj
        // nunca empezó, se le saca el "tocá para grabar" y la clase queda sin audio (o se descarta vacía).
        val enWatch = watch?.takeIf { it.empezo() }?.also {
            it.anotarCorte(ControlGrabacion.offsetActualMs())
            it.terminar()
        }
        // Si el pedido le llegó, el reloj pudo empezar sin que el celu lo viera: la clase espera su audio
        // (o que confirme que no la tiene) en vez de cerrarse sin audio. El CANCELAR la frena si está grabando.
        val sinEmpezar = watch?.takeIf { !it.empezo() && !it.pudoEmpezar() }
        watch?.takeIf { !it.empezo() }?.cancelar()
        val cerrada = cerrarSesionActual(interrumpida = false)
        if (cerrada == null) {
            if (subidaFinal?.isActive != true) stopSelf(ultimoStartId)
            return
        }
        // Con Ambos el celu grabó la clase: ya quedó como del teléfono (ver cerrarSesionActual).
        if (sinEmpezar != null && !cerrada.principalEsCelu) cerrarSinAudio(cerrada)
        publicar(EstadoGrabacion())
        mostrarNotificacionSubiendo(reloj = enWatch != null)
        PlanificadorSubida.patear(this)
        // La de una clase anterior (por ejemplo, esperando el audio del reloj) sigue: el servicio se
        // detiene recién cuando terminan todas.
        val anterior = subidaFinal
        subidaFinal = alcance.launch {
            val esta = coroutineContext[Job]
            runCatching { enWatch?.esperarAudioCompleto(MAX_RECEPCION_RELOJ_MS) }.onFailure { Log.w(TAG, "Audio del reloj", it) }
            withTimeoutOrNull(MAX_SUBIDA_FINAL_MS) {
                // Sin las importadas: este primer plano es para la clase que terminó (las sube WorkManager).
                runCatching { SincronizacionSubida.ejecutar(this@ServicioGrabacion, conImportadas = false) }
                    .onFailure { Log.w(TAG, "Subida final falló", it) }
            }
            anterior?.join()
            withContext(Dispatchers.Main) {
                enWatch?.soltar()
                // Si mientras tanto empezó otra clase, o terminó otra y su subida final sigue, no se corta.
                if (sesion == null && subidaFinal === esta) detenerServicio()
            }
        }
    }

    /** Frena el reloj y MediaRecorder y guarda la sesión como terminada. Devuelve la sesión cerrada. */
    private fun cerrarSesionActual(interrumpida: Boolean): SesionLocal? {
        val s = sesion ?: return null
        handler.removeCallbacks(tick)
        cerrarPreguntaCorte()
        val duracion = reloj?.pausar() ?: 0
        recorder?.let { r ->
            // stop() tira RuntimeException si no llegó audio válido (por ejemplo, si se paró enseguida).
            runCatching { r.stop() }.onFailure { Log.w(TAG, "stop() falló", it) }
            r.release()
        }
        recorder = null
        cerrarPistaCelu(interrumpida)
        // Con el reloj, la clase terminó pero su audio puede seguir llegando (la cierra SincronizacionWatch).
        val w = watch
        val esperaAlReloj = w?.let { it.empezo() || it.pudoEmpezar() } == true
        var final = s.copy(
            estadoLocal = if (esperaAlReloj) EstadoLocal.RECIBIENDO else EstadoLocal.TERMINADA,
            duracionMs = duracion,
            fin = Tiempo.ahoraIso(),
            interrumpida = interrumpida,
        )
        // Ambos y el reloj nunca empezó: el celu grabó la clase, que queda como del teléfono. Se cambia antes de
        // guardarla, así la subida nunca la ve cerrada como del reloj y sin audio.
        val pistas = PistasCelu(almacen)
        if (w != null && !esperaAlReloj && s.pistaCelu && pistas.audio(s.id).length() > 0) {
            final = Ambos.comoTelefono(final, Ajustes(this).dispositivo, pistas.leer(s.id))
        }
        // Si esto falla (disco lleno), la sesión queda "grabando" en disco y la cierra Recuperacion.
        runCatching { almacen.guardarSesion(final) }.onFailure { Log.e(TAG, "No se pudo guardar el cierre", it) }
        sesion = null
        relojCelu = null
        espejo = null
        relojCortado = false
        usarReloj(null, null)
        watch = null
        return final
    }

    /**
     * MediaRecorder murió en medio de la clase (por ejemplo, se reinició el servidor de audio). Se
     * cierra lo grabado y, como el servicio sigue en primer plano con acceso al micrófono, se sigue
     * grabando enseguida en una sesión nueva de la misma materia, en vez de perder el resto de la clase.
     */
    private fun errorDeGrabadora(what: Int, extra: Int) {
        val anterior = sesion ?: return
        Log.e(TAG, "MediaRecorder error what=$what extra=$extra")
        val grabado = ControlGrabacion.offsetActualMs()
        val estabaEnPausa = ControlGrabacion.estado.value.pausada
        cerrarSesionActual(interrumpida = true)
        PlanificadorSubida.patear(this)

        val ahora = SystemClock.elapsedRealtime()
        reinicios.removeAll { ahora - it > VENTANA_REINICIOS_MS }
        if (reinicios.size < MAX_REINICIOS) {
            reinicios += ahora
            val continuacion = anterior.copy(
                id = Uuid7.nuevo(),
                inicio = Tiempo.ahoraIso(),
                estadoLocal = EstadoLocal.GRABANDO,
                duracionMs = 0,
                fin = null,
                interrumpida = false,
                continuaDe = anterior.id,
                // Una de Ambos que quedó como del teléfono grababa en audio.celu.ogg: la que sigue, en audio.ogg.
                archivoAudio = null,
                pistaCelu = false,
            )
            if (arrancarGrabadora(continuacion, enPausa = estabaEnPausa)) {
                publicar(ControlGrabacion.estado.value, aviso = "⚠ La grabación se cortó y siguió en una sesión nueva")
                ClassmerApp.avisar(
                    this,
                    "La grabación se cortó y siguió",
                    "Se guardó lo grabado hasta ${Tiempo.duracion(grabado)} y se sigue grabando en una sesión nueva.",
                )
                return
            }
            // arrancarGrabadora ya avisó y detuvo el servicio.
        } else {
            publicar(EstadoGrabacion(error = "La grabación se cortó (error $what/$extra). Lo grabado quedó guardado."))
            detenerServicio()
        }
        ClassmerApp.avisarCorte(this, grabado)
    }

    // ---- Corte automático (§4) ----

    private fun revisarCorte(s: SesionLocal) {
        val abierta = preguntaDesde?.let { SystemClock.elapsedRealtime() - it }
        val espera = pruebaCorteMs ?: CorteAutomatico.ESPERA_RESPUESTA_MS
        when (CorteAutomatico.evaluar(s.preguntarCorteMs, System.currentTimeMillis(), abierta, espera)) {
            CorteAutomatico.Accion.NADA -> Unit
            CorteAutomatico.Accion.PREGUNTAR -> preguntarSiSigue()
            // Si la pregunta no se pudo mostrar (notificaciones apagadas), no se corta: se vuelve a intentar.
            CorteAutomatico.Accion.TERMINAR -> if (preguntaVisible) terminarPorCorte() else preguntarSiSigue()
        }
    }

    private fun preguntarSiSigue() {
        preguntaDesde = SystemClock.elapsedRealtime()
        val hasta = System.currentTimeMillis() + (pruebaCorteMs ?: CorteAutomatico.ESPERA_RESPUESTA_MS)
        preguntaVisible = mostrarPreguntaCorte(hasta)
        if (!preguntaVisible) Log.w(TAG, "No se pudo mostrar \"¿Sigue la clase?\": la grabación no va a terminar sola")
        publicar(
            ControlGrabacion.estado.value.copy(preguntaCorteHastaMs = hasta, preguntaCorteTerminaSola = preguntaVisible),
            aviso = "¿Sigue la clase?",
        )
    }

    /** "Seguir": se vuelve a preguntar en 30 min. */
    private fun seguirClase() {
        val s = sesion ?: return
        if (preguntaDesde == null) return
        val proxima = CorteAutomatico.seguir(System.currentTimeMillis(), pruebaCorteMs ?: CorteAutomatico.VOLVER_A_PREGUNTAR_MS)
        val actual = s.copy(preguntarCorteMs = proxima, duracionMs = ControlGrabacion.offsetActualMs())
        sesion = actual
        runCatching { almacen.guardarSesion(actual) }
        cerrarPreguntaCorte()
        publicar(
            ControlGrabacion.estado.value.copy(preguntaCorteHastaMs = null),
            aviso = "▶ Sigue la clase: se vuelve a preguntar a las ${Tiempo.hora(proxima)}",
        )
    }

    /** Nadie respondió en 10 min: la clase termina como si se hubiera tocado Terminar. */
    private fun terminarPorCorte() {
        val s = sesion ?: return
        val grabado = ControlGrabacion.offsetActualMs()
        Log.i(TAG, "Nadie respondió \"¿Sigue la clase?\": la grabación termina sola")
        sesion = s.copy(corteAutomatico = true)
        terminar()
        ClassmerApp.avisar(
            this,
            "La grabación terminó sola",
            "Nadie respondió «¿Sigue la clase?» en 10 minutos. Se guardó lo grabado (${Tiempo.duracion(grabado)}) y se sube. " +
                "Si la clase sigue, abrí Classmer y tocá Grabar.",
        )
    }

    private fun cerrarPreguntaCorte() {
        preguntaDesde = null
        preguntaVisible = false
        runCatching { getSystemService(NotificationManager::class.java).cancel(ID_PREGUNTA) }
    }

    /** Notificación con Seguir / Terminar y cuenta regresiva. Devuelve false si Android no la deja mostrar. */
    private fun mostrarPreguntaCorte(hastaMs: Long): Boolean {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            return false
        }
        val nm = getSystemService(NotificationManager::class.java)
        if (!NotificationManagerCompat.from(this).areNotificationsEnabled()) return false
        if (nm.getNotificationChannel(ClassmerApp.CANAL_CORTE)?.importance == NotificationManager.IMPORTANCE_NONE) return false
        val n = NotificationCompat.Builder(this, ClassmerApp.CANAL_CORTE)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setContentTitle("¿Sigue la clase?")
            .setContentText("Si no respondés, la grabación termina sola a las ${Tiempo.hora(hastaMs)}.")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setWhen(hastaMs)
            .setShowWhen(true)
            .setUsesChronometer(true)
            .setChronometerCountDown(true)
            .setContentIntent(
                PendingIntent.getActivity(
                    this, 1,
                    Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                ),
            )
            .addAction(0, "Seguir", accion(4, ACCION_SEGUIR))
            .addAction(0, "Terminar", accion(5, ACCION_TERMINAR))
            // Descartarla (deslizar o "Borrar todo") es como tocar Seguir: alguien la vio y la clase no se corta.
            .setDeleteIntent(accion(6, ACCION_SEGUIR))
            // Si graba el reloj, lo tiene el profe: la pregunta no se copia allá.
            .setLocalOnly(watch != null)
            .build()
        return runCatching { nm.notify(ID_PREGUNTA, n) }.isSuccess
    }

    // ---- Auxiliares ----

    /** Agrega el evento a `eventos.jsonl`. Si el disco falla, avisa y devuelve false. */
    private fun agregar(s: SesionLocal, evento: Evento): Boolean = try {
        almacen.agregarEvento(s.id, evento)
        true
    } catch (e: Exception) {
        Log.e(TAG, "No se pudo guardar el evento", e)
        publicar(ControlGrabacion.estado.value, aviso = "No se pudo guardar: ${e.message}")
        false
    }

    private fun guardarDuracion() {
        val s = sesion ?: return
        val actual = s.copy(duracionMs = ControlGrabacion.offsetActualMs())
        sesion = actual
        runCatching { almacen.guardarSesion(actual) }
        // Si la app se cae, la pista del celu se cierra con lo que diga su Ogg (o con esto).
        relojCelu?.takeIf { grabadoraCelu != null }?.let { rc ->
            runCatching { PistasCelu(almacen).cambiar(s.id) { it.copy(duracionMs = rc.offsetMs()) } }
        }
    }

    private val tick = object : Runnable {
        override fun run() {
            val s = sesion ?: return
            ticks++
            revisarMicrofono()
            // Con el reloj, el audio llega a tirones (y nada si se corta el Bluetooth): eso lo muestra SesionWatch.
            // Con Ambos se mira la pista del celu.
            if (watch == null) {
                revisarCrecimiento(almacen.archivoAudio(s), ControlGrabacion.estado.value.pausada)
            } else if (grabadoraCelu != null) {
                revisarCrecimiento(PistasCelu(almacen).audio(s.id), relojCelu?.corriendo != true)
            }
            medirDesfaseCelu()
            revisarCorte(s)
            // revisarCorte puede haber terminado la clase.
            if (sesion == null) return
            if (ticks % TICKS_POR_SUBIDA == 0L) {
                guardarDuracion()
                adquirirWakeLock()
                // Desde Android 14 la notificación se puede descartar: se vuelve a poner.
                actualizarNotificacion()
                subirAhora()
            }
            if (ticks % TICKS_POR_REVISION_ESPACIO == 0L) avisarSiQuedaPocoEspacio(almacen.dir(s.id))
            handler.postDelayed(this, INTERVALO_TICK_MS)
        }
    }

    /**
     * Sube lo nuevo desde acá mismo: con la pantalla bloqueada Android 16 frena los trabajos de
     * WorkManager (Doze), pero un servicio en primer plano conserva la red.
     */
    private fun subirAhora() {
        if (subidaEnCurso?.isActive == true) return
        subidaEnCurso = alcance.launch {
            runCatching { SincronizacionSubida.intentarSiEstaLibre(this@ServicioGrabacion) }
                .onFailure { Log.w(TAG, "Subida durante la grabación falló", it) }
        }
    }

    /** OggWriter ignora los errores de escritura: si el archivo deja de crecer mientras graba, se avisa. */
    private fun revisarCrecimiento(audio: File, pausada: Boolean) {
        val largo = audio.length()
        ticksSinCrecer = if (!pausada && largo == ultimoLargoAudio) ticksSinCrecer + 1 else 0
        ultimoLargoAudio = largo
        if (ticksSinCrecer >= 3 && !avisoSinCrecer) {
            avisoSinCrecer = true
            Log.e(TAG, "El audio no crece desde hace ${ticksSinCrecer * INTERVALO_TICK_MS / 1000} s ($largo bytes)")
            publicar(ControlGrabacion.estado.value, aviso = "⚠ El audio dejó de crecer: revisá el espacio libre")
            ClassmerApp.avisar(this, "La grabación puede estar fallando", "El archivo de audio no crece desde hace unos segundos.")
        } else if (ticksSinCrecer == 0) {
            avisoSinCrecer = false
        }
    }

    private fun avisarSiQuedaPocoEspacio(dir: File) {
        val libre = dir.usableSpace
        if (libre in 1 until MIN_ESPACIO_LIBRE) {
            val mb = libre / (1024 * 1024)
            publicar(ControlGrabacion.estado.value, aviso = "⚠ Quedan $mb MB libres en el celu")
            ClassmerApp.avisar(this, "Poco espacio libre", "Quedan $mb MB. Una hora de clase ocupa unos 20 MB más las fotos.")
        }
    }

    private fun revisarMicrofono() {
        val config = runCatching { (recorder ?: grabadoraCelu)?.activeRecordingConfiguration }.getOrNull()
        val silenciado = config?.isClientSilenced ?: false
        val e = ControlGrabacion.estado.value
        if (silenciado != e.microfonoSilenciado) {
            publicar(
                e.copy(microfonoSilenciado = silenciado),
                aviso = if (silenciado) "⚠ Otra app está usando el micrófono" else "🎙 El micrófono volvió",
            )
        }
        // Si se conectan auriculares con micrófono, Android puede pasar a grabar desde ahí.
        val entrada = config?.audioDevice ?: return
        if (entrada.type == ultimaEntrada) return
        val antes = ultimaEntrada
        ultimaEntrada = entrada.type
        if (entrada.type != AudioDeviceInfo.TYPE_BUILTIN_MIC) {
            val nombre = entrada.productName?.toString()?.takeIf { it.isNotBlank() } ?: "otro dispositivo"
            publicar(ControlGrabacion.estado.value, aviso = "🎧 Se está grabando desde $nombre, no desde el celu")
            ClassmerApp.avisar(this, "Cambió el micrófono", "Se está grabando desde $nombre. Desconectalo para volver al micrófono del celu.")
        } else if (antes != null) {
            publicar(ControlGrabacion.estado.value, aviso = "🎙 Se graba de nuevo con el micrófono del celu")
        }
    }

    private fun publicar(estado: EstadoGrabacion, aviso: String? = null) {
        val conCelu = estado.copy(celu = if (estado.sesion != null) textoCelu() else null)
        ControlGrabacion.estadoMutable.value =
            if (aviso != null) conCelu.copy(aviso = Aviso(aviso, ++avisos)) else conCelu
        if (sesion != null) actualizarNotificacion()
    }

    private fun crearRecorder(archivo: File): MediaRecorder {
        val r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(this) else @Suppress("DEPRECATION") MediaRecorder()
        r.setAudioSource(MediaRecorder.AudioSource.MIC)
        r.setOutputFormat(MediaRecorder.OutputFormat.OGG)
        r.setAudioEncoder(MediaRecorder.AudioEncoder.OPUS)
        r.setAudioChannels(1)
        r.setAudioSamplingRate(48_000)
        r.setAudioEncodingBitRate(32_000)
        r.setOutputFile(archivo)
        // Sólo cuenta un error de una grabadora actual (no uno atrasado de la que ya se reemplazó). La de la
        // pista del celu puede pasar a ser la de la clase (Ambos sin el reloj): se mira al llegar el error.
        r.setOnErrorListener { mr, what, extra ->
            handler.post {
                when {
                    mr === recorder -> errorDeGrabadora(what, extra)
                    mr === grabadoraCelu -> errorPistaCelu(what, extra)
                }
            }
        }
        return r
    }

    /**
     * Con el reloj, el celu no usa el micrófono: `connectedDevice`, que se puede arrancar desde segundo plano.
     * Con Ambos, los dos tipos.
     */
    private fun tipoServicio(microfono: Boolean, reloj: Boolean): Int = when {
        Build.VERSION.SDK_INT < 30 -> 0
        microfono && reloj -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
        reloj -> ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
        else -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
    }

    private fun adquirirWakeLock() {
        val wl = wakeLock ?: (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "classmer:grabacion")
            .also {
                it.setReferenceCounted(false)
                wakeLock = it
            }
        // Con plazo, por las dudas; se renueva en cada tick mientras se graba.
        wl.acquire(10 * 60 * 1000L)
    }

    private fun liberarWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    private fun actualizarNotificacion() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        runCatching {
            getSystemService(NotificationManager::class.java).notify(ID_NOTIFICACION, construirNotificacion())
        }
    }

    private fun construirNotificacion(): Notification {
        val e = ControlGrabacion.estado.value
        val offset = ControlGrabacion.offsetActualMs()
        val materia = sesion?.nombreMateria ?: "Auto (IA)"
        val contadores = buildString {
            append("⭐ ${e.importantes}  ❓ ${e.noEntendi}  📷 ${e.fotos}  📝 ${e.notas}")
            if (e.microfonoSilenciado) append("  ⚠ micrófono ocupado")
        }
        val b = NotificationCompat.Builder(this, ClassmerApp.CANAL_GRABACION)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setContentTitle((if (e.watch != null) (if (e.celu != null) "⌚📱 " else "⌚ ") else "") + if (e.pausada) "En pausa · $materia" else "Grabando · $materia")
            .setContentText(contadores)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(
                PendingIntent.getActivity(
                    this, 0,
                    Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                ),
            )
            .addAction(0, "⭐ Importante", accion(1, ACCION_MARCA) { putExtra(EXTRA_MARCA, TipoMarca.IMPORTANTE) })
            .addAction(0, "❓ No entendí", accion(2, ACCION_MARCA) { putExtra(EXTRA_MARCA, TipoMarca.NO_ENTENDI) })
            .addAction(
                0,
                if (e.pausada) "▶ Reanudar" else "⏸ Pausa",
                accion(3, if (e.pausada) ACCION_REANUDAR else ACCION_PAUSAR),
            )
        e.watch?.let { b.setStyle(NotificationCompat.BigTextStyle().bigText("$contadores\n${it.texto}" + (e.celu?.let { c -> "\n$c" } ?: ""))) }
        if (e.watch?.esperandoInicio == true) {
            b.setShowWhen(false)
        } else if (e.pausada) {
            b.setShowWhen(false).setSubText("Pausa en ${Tiempo.duracion(offset)}")
        } else {
            b.setUsesChronometer(true).setShowWhen(true).setWhen(System.currentTimeMillis() - offset)
        }
        return b.build()
    }

    /** Mientras se sube lo último de la clase recién terminada (con el reloj, también mientras llega su audio). */
    private fun mostrarNotificacionSubiendo(reloj: Boolean = false) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        val n = NotificationCompat.Builder(this, ClassmerApp.CANAL_GRABACION)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setContentTitle("Subiendo la clase")
            .setContentText(if (reloj) "Llega el audio del reloj y se manda al server" else "Se manda lo último al server")
            .setOngoing(true)
            .setSilent(true)
            .setProgress(0, 0, true)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
        runCatching { getSystemService(NotificationManager::class.java).notify(ID_NOTIFICACION, n) }
    }

    /** La pista del minuto que mandó la pantalla con una foto o una nota (sin el extra, la del reloj actual). */
    private fun pistaDe(intent: Intent): String? =
        if (intent.hasExtra(EXTRA_PISTA)) intent.getStringExtra(EXTRA_PISTA)?.takeIf { it.isNotEmpty() } else ControlGrabacion.pistaActual()

    private fun accion(codigo: Int, accion: String, extras: Intent.() -> Unit = {}): PendingIntent =
        PendingIntent.getService(
            this, codigo,
            Intent(this, ServicioGrabacion::class.java).setAction(accion).apply(extras),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    companion object {
        private const val TAG = "ClassmerGrabacion"

        /**
         * La sesión a la que se puede enganchar el servicio: una del reloj que sigue grabando. Nunca una
         * importada (no la graba nadie), aunque algo le cambie la fuente o el estado.
         */
        fun paraEnganchar(s: SesionLocal?): SesionLocal? = s?.takeIf { it.delReloj && it.estadoLocal == EstadoLocal.GRABANDO }
        const val ID_NOTIFICACION = 1
        /** La notificación "¿Sigue la clase?". */
        const val ID_PREGUNTA = 2

        private const val INTERVALO_TICK_MS = 5_000L

        /** Cada 6 ticks (~30 s): guarda la duración y sube lo nuevo. */
        private const val TICKS_POR_SUBIDA = 6L

        /** Cada ~5 min se mira el espacio libre. */
        private const val TICKS_POR_REVISION_ESPACIO = 60L
        private const val MIN_ESPACIO_LIBRE = 300L * 1024 * 1024

        /** Tope para subir lo último al terminar antes de soltar el primer plano. */
        private const val MAX_SUBIDA_FINAL_MS = 60_000L

        /**
         * Con el reloj, cuánto se espera (en primer plano) a que llegue su audio al terminar. Si el reloj
         * quedó lejos, el resto se trae después (al volver la conexión, o con la subida de WorkManager).
         */
        private const val MAX_RECEPCION_RELOJ_MS = 15 * 60_000L

        /** El error de un desfase medido con la hora del celu entre dos grabaciones del mismo celu (lo que tarda en arrancar). */
        private const val ERROR_MISMO_CELU_MS = 100L

        /** Si MediaRecorder falla, se reintenta seguir grabando hasta 3 veces cada 10 minutos. */
        private const val MAX_REINICIOS = 3
        private const val VENTANA_REINICIOS_MS = 10 * 60 * 1000L

        const val ACCION_INICIAR = "ar.classmer.INICIAR"
        const val ACCION_MARCA = "ar.classmer.MARCA"
        const val ACCION_NOTA = "ar.classmer.NOTA"
        const val ACCION_FOTO = "ar.classmer.FOTO"
        const val ACCION_PAUSAR = "ar.classmer.PAUSAR"
        const val ACCION_REANUDAR = "ar.classmer.REANUDAR"
        const val ACCION_TERMINAR = "ar.classmer.TERMINAR"
        const val ACCION_SEGUIR = "ar.classmer.SEGUIR"
        const val ACCION_ENGANCHAR = "ar.classmer.ENGANCHAR"
        const val ACCION_USAR_TELEFONO = "ar.classmer.USAR_TELEFONO"

        const val EXTRA_MATERIA_ID = "materia_id"
        const val EXTRA_NOMBRE_MATERIA = "nombre_materia"
        const val EXTRA_ORIGEN = "origen_materia"
        const val EXTRA_TIPO_CLASE = "tipo_clase"
        const val EXTRA_EVENTO_CALENDARIO = "evento_calendario"
        const val EXTRA_NUMERO_CLASE = "numero_clase"
        const val EXTRA_FIN_EVENTO = "fin_evento_ms"
        const val EXTRA_MARCA = "marca"
        const val EXTRA_TEXTO = "texto"
        const val EXTRA_OFFSET = "offset_ms"
        const val EXTRA_EVENTO_ID = "evento_id"
        const val EXTRA_ARCHIVO = "archivo"
        const val EXTRA_FUENTE = "fuente_audio"
        const val EXTRA_SESION_ID = "sesion_id"

        /** El audio en que se contó el minuto de una foto o una nota ([ControlGrabacion.pistaActual]); vacío = el principal. */
        const val EXTRA_PISTA = "pista"

        /** Al seguir una clase del reloj: el celu graba también su pista (Ambos). */
        const val EXTRA_PISTA_CELU = "pista_celu"
    }
}
