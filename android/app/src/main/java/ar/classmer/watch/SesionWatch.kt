package ar.classmer.watch

import android.content.Context
import android.os.SystemClock
import android.util.Log
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.grabacion.RelojGrabacion
import ar.classmer.modelo.SesionLocal
import ar.classmer.watchlink.Desfase
import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.EstadoWatch
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.Orden
import ar.classmer.watchlink.Rutas
import ar.classmer.watchlink.Sincronizacion
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.distinctUntilChangedBy
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.util.concurrent.atomic.AtomicBoolean

/** Lo que la pantalla de grabación (y la notificación) muestran del reloj. */
data class EstadoWatchUi(
    /** Se le pidió al reloj que grabe y todavía no se tocó "Grabar" allá. */
    val esperandoInicio: Boolean = true,
    /** Hace rato que el reloj no contesta (el audio sigue quedando en el reloj). */
    val sinContacto: Boolean = false,
    /** Una línea de estado, por ejemplo "⌚ Graba el reloj · audio al día". */
    val texto: String = "⌚ Buscando el reloj…",
    val bateria: Int? = null,
    /** El reloj no empezó: todavía se puede grabar con el celu en vez del reloj. */
    val puedeUsarTelefono: Boolean = false,
    /** Lo que el reloj ya grabó y el celu todavía no tiene. */
    val faltanBytes: Long = 0,
)

/**
 * La clase en curso cuando graba el reloj: lo que [ar.classmer.grabacion.ServicioGrabacion] le
 * delega en vez de usar MediaRecorder. Las fotos, notas y marcas las sigue anotando el servicio con
 * `ControlGrabacion.offsetActualMs()`, que acá se vuelve un reloj espejo del audio del Watch: el
 * mismo tiempo grabado (sin pausas), pasado al reloj del celu con el [Desfase] medido.
 *
 * Todo lo que cambia se avisa con [alCambiar], siempre en el hilo principal.
 */
class SesionWatch(
    private val context: Context,
    val sesionId: String,
    private val alcance: CoroutineScope,
    private val alCambiar: (Cambio) -> Unit,
) {
    sealed interface Cambio {
        /** El reloj empezó a grabar a la hora [inicio] (ISO 8601): es la hora de la clase. */
        data class Empezo(val inicio: String) : Cambio

        /**
         * El reloj espejo para los offsets del celu (`null` = el reloj todavía no empezó). Si la grabación del
         * reloj terminó, [pausada] dice si estaba en pausa al terminar (la pista del celu de Ambos la sigue).
         */
        data class Reloj(val reloj: RelojGrabacion?, val pausada: Boolean) : Cambio

        /** El reloj pausó o reanudó: el offset exacto del Watch para el evento. */
        data class Pausa(val offsetMs: Long) : Cambio
        data class Reanudacion(val offsetMs: Long) : Cambio

        /** El reloj terminó la grabación por su cuenta (botón del reloj, o se cortó allá). */
        data class TerminoEnWatch(val interrumpida: Boolean) : Cambio

        /** La grabadora del reloj falló y siguió en la grabación [g]: la clase sigue en esa sesión nueva. */
        data class Continuacion(val g: GrabacionWatch) : Cambio

        data class Estado(val ui: EstadoWatchUi) : Cambio
        data class Aviso(val texto: String) : Cambio
    }

    private val almacen = AlmacenSesiones.de(context)
    private val registros = RegistrosWatch(almacen)
    private val candado = Mutex()
    private var trabajo: Job? = null

    /** Lo último que se aplicó al reloj espejo. */
    @Volatile
    private var aplicada: GrabacionWatch? = null

    /**
     * Se mide cada vez que se sigue una clase, aunque `watch.json` tenga uno: `elapsedRealtime` vuelve a
     * cero si el celu se reinicia, y un desfase viejo correría todos los offsets.
     */
    @Volatile
    private var desfase: Desfase? = null

    @Volatile
    private var desfaseAplicado: Desfase? = null
    private val midiendo = AtomicBoolean(false)
    private val sincronizando = AtomicBoolean(false)

    /** Se pidió que grabe (en vez de engancharse a una grabación que ya existe en el reloj). */
    @Volatile
    private var pedirInicio = false

    /** El reloj recibió el pedido de grabar (contestó, o informó el pedido): puede haber empezado sin que el celu lo vea. */
    @Volatile
    private var pedidoEntregado = false
    private var materia: String? = null
    private var tipoClase: String? = null
    private var ultimoPedido = 0L
    private var terminoAvisado = false
    private var continuacionAvisada = false

    /** `ahora` (reloj del Watch) del último estado aplicado: uno más viejo que llega tarde se descarta. */
    private var ultimoAhora = Long.MIN_VALUE

    @Volatile
    var ui = EstadoWatchUi()
        private set

    /** El usuario tocó Grabar con el reloj: se manda la orden y se espera el toque en el reloj. */
    fun iniciar(materia: String?, tipoClase: String?) {
        pedirInicio = true
        this.materia = materia
        this.tipoClase = tipoClase
        mostrar(EstadoWatchUi(texto = "⌚ Pidiéndole al reloj que grabe…", puedeUsarTelefono = true))
        pedir()
        arrancar()
    }

    /** La grabación ya existe en el reloj (se empezó allá, o el celu se reinició): sólo se sigue. */
    fun enganchar() {
        pedirInicio = false
        mostrar(EstadoWatchUi(esperandoInicio = false, texto = "⌚ Conectando con el reloj…"))
        arrancar()
    }

    fun pausar() = orden(Rutas.PAUSAR, "No se pudo pausar: el reloj no respondió")
    fun reanudar() = orden(Rutas.REANUDAR, "No se pudo reanudar: el reloj no respondió")

    /** Terminar en el celu: se le pide al reloj; si no contesta, la sincronización insiste. */
    fun terminar() = orden(Rutas.TERMINAR, null)

    /** El celu se pasa a grabar él: el reloj saca el aviso "tocá para grabar". */
    fun cancelar() {
        alcance.launch { EnlaceWatch.ordenar(context, Rutas.CANCELAR) { Orden(n = it, id = sesionId) } }
        soltar()
    }

    fun soltar() {
        trabajo?.cancel()
        trabajo = null
    }

    /** El reloj está grabando (o grabó) esta clase según lo que vio el celu; al seguir una que ya existía, siempre. */
    fun empezo(): Boolean = !pedirInicio || aplicada != null

    /**
     * El celu no vio que el reloj empezara, pero el pedido le llegó: pudo haber empezado sin contacto. Al
     * terminar, la clase no se da por "sin audio" hasta que el reloj confirme que no la tiene.
     */
    fun pudoEmpezar(): Boolean = !empezo() && pedidoEntregado

    /**
     * Lo grabado por el reloj en el instante [ahoraCelu] del celu y el error posible (el del desfase entre
     * los relojes), mientras graba (no en pausa ni terminada): con eso se alinea la pista del celu (Ambos).
     */
    fun grabadoEn(ahoraCelu: Long): Pair<Long, Long>? {
        val g = aplicada?.takeIf { it.estado == EstadoGrabacionWatch.GRABANDO && it.desde != null } ?: return null
        // La mejor medición, aunque el reloj espejo no se haya rehecho con ella (difiere en menos de 50 ms).
        val d = desfase ?: desfaseAplicado ?: return null
        return g.offsetEn(ahoraCelu, d) to d.errorEn(ahoraCelu)
    }

    /** Al terminar en el celu: hasta dónde iba la clase (para saber cuánto grabó el reloj de más). */
    fun anotarCorte(offsetMs: Long) {
        registros.cambiar(sesionId) { it.copy(corteMs = offsetMs) }
    }

    /**
     * Después de Terminar: espera (con el servicio todavía en primer plano) a que llegue y se verifique
     * todo el audio del reloj, hasta [maxMs]. Devuelve true si la sesión quedó cerrada.
     */
    suspend fun esperarAudioCompleto(maxMs: Long): Boolean = withContext(Dispatchers.IO) {
        withTimeoutOrNull(maxMs) {
            while (almacen.leerSesion(sesionId)?.terminada != true) {
                EnlaceWatch.consultar(context)?.let { e ->
                    procesar(e)
                    EnlaceWatch.info.value.nodo?.let { SincronizacionWatch.sincronizar(context, e, it) }
                }
                if (almacen.leerSesion(sesionId)?.terminada == true) break
                delay(ESPERA_ENTRE_CONSULTAS_FINAL_MS)
            }
            true
        } ?: false
    }

    private fun arrancar() {
        trabajo?.cancel()
        trabajo = alcance.launch {
            // Cada estado que llega del reloj (contestado o mandado solo) se aplica enseguida.
            launch {
                EnlaceWatch.info.map { it.estado to it.estadoEn }.distinctUntilChangedBy { it.second }
                    .map { it.first }.filterNotNull().collect { procesar(it) }
            }
            while (isActive) {
                val e = EnlaceWatch.consultar(context)
                if (e == null) sinRespuesta()
                delay(if (ui.esperandoInicio) CONSULTA_ESPERANDO_MS else CONSULTA_MS)
            }
        }
    }

    private suspend fun procesar(e: EstadoWatch) = candado.withLock {
        // Una respuesta armada antes de un cambio puede llegar después del estado con el cambio. Si el
        // reloj se reinició, su `ahora` vuelve a empezar: ahí se acepta.
        if (e.ahora < ultimoAhora && ultimoAhora - e.ahora < VENTANA_REINICIO_MS) return@withLock
        ultimoAhora = e.ahora
        val g = e.grabacion(sesionId)
        when {
            g != null -> seguir(e, g)
            e.esperando == sesionId -> {
                pedidoEntregado = true
                mostrar(
                    EstadoWatchUi(
                        texto = "⌚ Tocá el aviso «tocá para grabar» en el reloj para empezar",
                        bateria = e.bateria,
                        puedeUsarTelefono = true,
                    ),
                )
            }
            pedirInicio -> {
                val otra = e.activa
                if (otra != null) {
                    mostrar(EstadoWatchUi(texto = "⌚ El reloj ya está grabando otra clase", bateria = e.bateria, puedeUsarTelefono = true))
                } else {
                    // El pedido no llegó (o la app del reloj se reinició): se vuelve a mandar.
                    pedir()
                    mostrar(
                        EstadoWatchUi(
                            texto = e.error?.let { "⌚ $it" } ?: "⌚ Pidiéndole al reloj que grabe…",
                            bateria = e.bateria,
                            puedeUsarTelefono = true,
                        ),
                    )
                }
            }
            else -> mostrar(ui.copy(texto = "⌚ El reloj no tiene esta grabación", sinContacto = false))
        }
    }

    private fun pedir() {
        val ahora = SystemClock.elapsedRealtime()
        if (ahora - ultimoPedido < REPETIR_PEDIDO_MS) return
        ultimoPedido = ahora
        alcance.launch {
            val e = EnlaceWatch.ordenar(context, Rutas.INICIAR) { Orden(n = it, id = sesionId, materia = materia, tipoClase = tipoClase) }
            if (e != null) pedidoEntregado = true
        }
    }

    /** La grabación existe en el reloj: reloj espejo, pausas, fin y transferencia. */
    private fun seguir(e: EstadoWatch, g: GrabacionWatch) {
        pedirInicio = false
        if (aplicada == null) avisar(Cambio.Empezo(g.inicio))
        val d = desfase ?: estimarSinPing(e)
        if (desfase == null || desfase!!.errorEn(SystemClock.elapsedRealtime()) > Sincronizacion.ERROR_OBJETIVO_MS) medir()
        aplicar(g, d)

        // Si la grabadora del reloj falló y siguió en otra grabación, la clase sigue ahí: no terminó.
        val nueva = e.grabaciones.firstOrNull { it.continuaDe == sesionId && it.activa }
        if (nueva != null && !continuacionAvisada) {
            continuacionAvisada = true
            terminoAvisado = true
            avisar(Cambio.Continuacion(nueva))
        } else if (g.terminada && !terminoAvisado) {
            terminoAvisado = true
            avisar(Cambio.TerminoEnWatch(g.interrumpida))
        }

        val local = almacen.archivoAudio(sesionId).length()
        val faltan = (g.bytes - local).coerceAtLeast(0)
        val estado = when {
            g.terminada -> if (faltan > 0) "⌚ El reloj terminó: faltan ${mb(faltan)} por pasar" else "⌚ El reloj terminó: verificando el audio"
            g.estado == EstadoGrabacionWatch.PAUSADA -> "⌚ El reloj está en pausa"
            faltan > AL_DIA_BYTES -> "⌚ Graba el reloj · faltan ${mb(faltan)} por pasar"
            else -> "⌚ Graba el reloj · audio al día"
        }
        val bateria = e.bateria?.let { if (it < BATERIA_BAJA) " · ⚠ batería $it %" else " · batería $it %" } ?: ""
        mostrar(
            EstadoWatchUi(
                esperandoInicio = false,
                texto = estado + bateria + (e.error?.let { "\n⚠ $it" } ?: ""),
                bateria = e.bateria,
                faltanBytes = faltan,
            ),
        )
        sincronizarEnFondo(e)
    }

    /**
     * Arma el reloj espejo con el estado del Watch. Sólo cambia cuando el Watch arranca, pausa o
     * reanuda, o cuando el desfase medido se corre más de [TOLERANCIA_DESFASE_MS]: así el cronómetro
     * del celu no salta con cada estado.
     */
    private fun aplicar(g: GrabacionWatch, d: Desfase) {
        val anterior = aplicada
        val cambioEstado = anterior == null || anterior.estado != g.estado || anterior.acumuladoMs != g.acumuladoMs || anterior.desde != g.desde
        val cambioDesfase = desfaseAplicado?.let { kotlin.math.abs(it.ms - d.ms) > TOLERANCIA_DESFASE_MS } ?: true
        if (!cambioEstado && !cambioDesfase) return
        aplicada = g
        desfaseAplicado = d
        val corriendo = g.estado == EstadoGrabacionWatch.GRABANDO
        val reloj = RelojGrabacion(acumuladoInicialMs = g.offsetEn(SystemClock.elapsedRealtime(), d)).also { if (corriendo) it.iniciar() }
        val antes = anterior?.estado
        when {
            antes == EstadoGrabacionWatch.GRABANDO && g.estado == EstadoGrabacionWatch.PAUSADA -> avisar(Cambio.Pausa(g.acumuladoMs))
            antes == EstadoGrabacionWatch.PAUSADA && g.estado == EstadoGrabacionWatch.GRABANDO -> avisar(Cambio.Reanudacion(g.acumuladoMs))
            // Se perdieron los dos estados de una pausa y su reanudación (o al revés): el acumulado cambió igual.
            antes == EstadoGrabacionWatch.GRABANDO && g.estado == EstadoGrabacionWatch.GRABANDO && anterior.acumuladoMs != g.acumuladoMs -> {
                avisar(Cambio.Pausa(g.acumuladoMs))
                avisar(Cambio.Reanudacion(g.acumuladoMs))
            }
            antes == EstadoGrabacionWatch.PAUSADA && g.estado == EstadoGrabacionWatch.PAUSADA && anterior.acumuladoMs != g.acumuladoMs -> {
                avisar(Cambio.Reanudacion(anterior.acumuladoMs))
                avisar(Cambio.Pausa(g.acumuladoMs))
            }
            // Terminó después de una reanudación que el celu no vio (sin contacto): la clase siguió.
            antes == EstadoGrabacionWatch.PAUSADA && g.terminada && siguioGrabando(anterior, g) ->
                avisar(Cambio.Reanudacion(anterior.acumuladoMs))
        }
        // Una grabación que terminó en pausa (sin grabar nada más) sigue "en pausa" para la clase.
        val pausada = g.estado == EstadoGrabacionWatch.PAUSADA ||
            (g.terminada && antes == EstadoGrabacionWatch.PAUSADA && !siguioGrabando(anterior, g))
        avisar(Cambio.Reloj(reloj, pausada))
    }

    /**
     * Una grabación que estaba en pausa ([antes]) siguió grabando antes de terminar ([g]). Si la app del reloj
     * se cayó, el acumulado que queda es el de la última pausa: lo dice el archivo, que siguió creciendo.
     */
    private fun siguioGrabando(antes: GrabacionWatch, g: GrabacionWatch): Boolean =
        g.acumuladoMs > antes.acumuladoMs || g.bytes > antes.bytes + CRECIMIENTO_EN_PAUSA_BYTES

    /**
     * Mientras no hay una medición con ping: el estado trae la hora del Watch al mandarlo. Sin contar
     * lo que tardó el mensaje (en general menos de medio segundo), da una primera aproximación.
     */
    private fun estimarSinPing(e: EstadoWatch): Desfase {
        val ahora = EnlaceWatch.info.value.estadoEn.takeIf { it > 0 } ?: SystemClock.elapsedRealtime()
        return Desfase(ms = e.ahora - ahora, errorMs = ERROR_SIN_PING_MS, idaYVueltaMs = -1, medidoEn = ahora)
    }

    private fun medir() {
        if (!midiendo.compareAndSet(false, true)) return
        alcance.launch {
            try {
                val nueva = EnlaceWatch.medirDesfase(context) ?: return@launch
                val elegida = Sincronizacion.mejor(desfase, nueva, SystemClock.elapsedRealtime()) ?: return@launch
                if (elegida != desfase) {
                    desfase = elegida
                    registros.cambiar(sesionId) { it.copy(desfase = elegida) }
                    Log.i(TAG, "Desfase con el reloj para $sesionId: ${elegida.ms} ms ± ${elegida.errorMs} ms")
                    aplicada?.let { g -> candado.withLock { aplicar(g, elegida) } }
                }
            } finally {
                midiendo.set(false)
            }
        }
    }

    private fun sincronizarEnFondo(e: EstadoWatch) {
        val nodo = EnlaceWatch.info.value.nodo ?: return
        if (!sincronizando.compareAndSet(false, true)) return
        alcance.launch {
            try {
                SincronizacionWatch.sincronizar(context, e, nodo)
            } catch (ex: Exception) {
                if (ex is kotlinx.coroutines.CancellationException) throw ex
                Log.w(TAG, "Sincronización con el reloj", ex)
            } finally {
                sincronizando.set(false)
            }
        }
    }

    private fun sinRespuesta() {
        val desde = EnlaceWatch.info.value.estadoEn
        val hace = if (desde > 0) SystemClock.elapsedRealtime() - desde else Long.MAX_VALUE
        if (hace < SIN_CONTACTO_MS && !ui.esperandoInicio) return
        val info = EnlaceWatch.info.value
        val texto = when {
            ui.esperandoInicio && !info.conectado -> "⌚ El reloj no está conectado al celu"
            ui.esperandoInicio && !info.appInstalada -> "⌚ El reloj no tiene Classmer instalada"
            ui.esperandoInicio -> "⌚ El reloj no responde"
            desde <= 0 -> "⌚ Sin contacto con el reloj · sigue grabando y el audio llega después"
            else -> "⌚ Sin contacto con el reloj hace ${hace / 60_000} min · sigue grabando y el audio llega después"
        }
        mostrar(ui.copy(texto = texto, sinContacto = true))
    }

    private fun orden(ruta: String, siFalla: String?) {
        alcance.launch {
            val e = EnlaceWatch.ordenar(context, ruta) { Orden(n = it, id = sesionId) }
            if (e == null && siFalla != null) avisar(Cambio.Aviso(siFalla))
        }
    }

    private fun mostrar(nuevo: EstadoWatchUi) {
        if (nuevo == ui) return
        ui = nuevo
        avisar(Cambio.Estado(nuevo))
    }

    private fun avisar(c: Cambio) {
        alcance.launch(Dispatchers.Main) { alCambiar(c) }
    }

    companion object {
        private const val TAG = "ClassmerWatch"
        private const val VENTANA_REINICIO_MS = 10 * 60_000L
        private const val CONSULTA_MS = 30_000L
        private const val CONSULTA_ESPERANDO_MS = 5_000L
        private const val ESPERA_ENTRE_CONSULTAS_FINAL_MS = 3_000L
        private const val REPETIR_PEDIDO_MS = 15_000L
        private const val SIN_CONTACTO_MS = 90_000L
        private const val TOLERANCIA_DESFASE_MS = 50L
        private const val ERROR_SIN_PING_MS = 1_000L

        /** Menos que esto pendiente (unos 15 s de audio) cuenta como "al día". */
        private const val AL_DIA_BYTES = 80L * 1024

        /** Lo que puede crecer el audio del reloj en pausa al cerrarlo (la última página): ~4 s de audio. */
        private const val CRECIMIENTO_EN_PAUSA_BYTES = 16L * 1024

        /** Por debajo de esto, Inicio y la pantalla de grabación avisan que la batería del reloj está baja. */
        const val BATERIA_BAJA = 30

        fun mb(bytes: Long) = "%.1f MB".format(bytes / 1_048_576.0)

        /**
         * Al volver a seguir una clase (el celu se reinició) y hasta que conteste el reloj: lo grabado
         * según el último guardado de la sesión (el servicio lo guarda cada ~30 s), más lo que pasó desde
         * entonces. Así una marca en ese rato no queda en 0:00.
         */
        fun offsetProvisional(almacen: AlmacenSesiones, s: SesionLocal, enPausa: Boolean = false, ahoraMs: Long = System.currentTimeMillis()): Long {
            if (enPausa) return RegistrosWatch(almacen).leer(s.id).grabacion?.acumuladoMs ?: s.duracionMs
            return s.duracionMs + (ahoraMs - almacen.archivoSesion(s.id).lastModified()).coerceIn(0, 6 * 60 * 60_000L)
        }

        /** Lo último que se supo del reloj es que estaba en pausa. */
        fun estabaEnPausa(almacen: AlmacenSesiones, id: String): Boolean =
            RegistrosWatch(almacen).leer(id).grabacion?.estado == EstadoGrabacionWatch.PAUSADA
    }
}
