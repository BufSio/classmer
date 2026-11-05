package ar.classmer.watch

import android.content.Context
import android.os.SystemClock
import android.util.Log
import ar.classmer.ClassmerApp
import ar.classmer.calendario.LectorCalendario
import ar.classmer.calendario.ReglasCalendario
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.grabacion.CorteAutomatico
import ar.classmer.grabacion.OggDuracion
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.OrigenMateria
import ar.classmer.modelo.SesionLocal
import ar.classmer.subida.PlanificadorSubida
import ar.classmer.ui.Seleccion
import ar.classmer.ui.SeleccionMateria
import ar.classmer.util.Tiempo
import ar.classmer.watchlink.EstadoWatch
import ar.classmer.watchlink.FinRecepcion
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.Huella
import ar.classmer.watchlink.IdGrabacion
import ar.classmer.watchlink.Orden
import ar.classmer.watchlink.PlanTransferencia
import ar.classmer.watchlink.Rutas
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * Pone al día el celu con las grabaciones del reloj, con cada estado que llega:
 *
 * 1. Una grabación que el celu no conoce (se empezó en el reloj) se vuelve una sesión nueva, con la
 *    materia del calendario, y el servicio de grabación del celu se engancha.
 * 2. El audio se trae por el canal desde lo que ya tiene el celu ([PlanTransferencia]) y se agrega al
 *    mismo `audio.ogg` que sube el [ar.classmer.subida.Subidor]: el server no se entera.
 * 3. Cuando el reloj terminó y el celu tiene el mismo largo, se compara la huella. Si coincide, la
 *    sesión queda terminada y la subida la finaliza como siempre; si no, se baja entera de nuevo.
 * 4. Cuando el server aceptó la clase, se le pide al reloj que borre su copia.
 *
 * Un candado hace que nunca haya dos de estas pasadas a la vez (un solo canal por vez).
 */
object SincronizacionWatch {
    private const val TAG = "ClassmerWatchSync"
    private val candado = Mutex()
    private val alcance = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** Cuándo se le pidió por última vez al reloj que termine cada clase (para no insistir en cada estado). */
    private val terminarPedido = ConcurrentHashMap<String, Long>()

    /** Cuándo se intentó por última vez enganchar el servicio del celu a cada grabación del reloj. */
    private val enganchePedido = ConcurrentHashMap<String, Long>()

    /**
     * Grabar con Ambos sobre una grabación del reloj (cuándo se tocó): el enganche que haga la sincronización
     * mientras tanto pide también la pista del celu, con la app todavía a la vista.
     */
    private val conCeluPedido = ConcurrentHashMap<String, Long>()
    private const val CON_CELU_VIGENTE_MS = 60_000L

    /** Cuándo se le pidió por última vez al reloj que saque un pedido "tocá para grabar" que ya no va. */
    private val cancelarPedido = ConcurrentHashMap<String, Long>()

    /** Tope de pedidos de audio por pasada (de a 1 MiB, cada uno sigue desde donde cortó el anterior). */
    private const val MAX_PEDIDOS = 80

    /** Bajadas enteras con huella distinta antes de dejar de insistir (y ofrecer cerrar con lo recibido). */
    const val MAX_REBAJADAS = 3
    private const val REPETIR_TERMINAR_MS = 15_000L
    private const val REPETIR_ENGANCHE_MS = 5 * 60_000L
    private const val REPETIR_CANCELAR_MS = 60_000L
    private const val ESTADO_RECIENTE_MS = 60_000L

    /** Una sesión del reloj que nadie graba (ni el reloj la tiene, ni espera el toque) se cierra pasado esto. */
    private const val MARGEN_HUERFANA_MS = 10 * 60_000L



    /** El último estado que llegó sin que nadie lo esperara, todavía sin atender. */
    private val ultimo = AtomicReference<Pair<EstadoWatch, String>?>(null)
    private val atendiendo = AtomicBoolean(false)

    /**
     * Llega un estado del reloj: se pone al día en segundo plano, sin demorar a quien lo recibió. Si
     * llegan varios mientras se atiende uno (por ejemplo, durante una transferencia larga), sólo se
     * atiende el último.
     */
    fun alRecibirEstado(context: Context, estado: EstadoWatch, nodo: String) {
        ultimo.set(estado to nodo)
        val app = context.applicationContext
        if (!atendiendo.compareAndSet(false, true)) return
        alcance.launch {
            do {
                try {
                    while (true) {
                        val (e, n) = ultimo.getAndSet(null) ?: break
                        runCatching { sincronizar(app, e, n) }.onFailure { Log.w(TAG, "No se pudo sincronizar con el reloj", it) }
                    }
                } finally {
                    atendiendo.set(false)
                }
                // Uno que llegó justo al terminar: se sigue, salvo que ya lo esté atendiendo otro.
            } while (ultimo.get() != null && atendiendo.compareAndSet(false, true))
        }
    }

    /** Hay algo que traer del reloj o que confirmarle. */
    fun hayPendientes(context: Context): Boolean = hayPendientes(AlmacenSesiones.de(context))

    /** Lo mismo, sin Context (para los tests): sólo cuentan las clases del reloj, nunca una importada. */
    fun hayPendientes(almacen: AlmacenSesiones): Boolean {
        val registros = RegistrosWatch(almacen)
        return almacen.listar().any { s ->
            when {
                s.delReloj -> registros.leer(s.id).let { r -> !r.cerradaSinWatch && (s.estadoLocal != EstadoLocal.TERMINADA || !r.borradaEnWatch) }
                // Ambos que quedó del teléfono con una copia del reloj sin sonido: falta que el reloj la borre.
                s.principalEsCelu -> registros.leer(s.id).let { r -> r.verificada && !r.borradaEnWatch }
                else -> false
            }
        }
    }

    /**
     * Pregunta al reloj y se pone al día (desde la subida, al abrir la app o cuando vuelve la conexión).
     * Sin [siempre], no molesta al reloj si el celu no espera nada de él.
     */
    suspend fun ponerseAlDia(context: Context, siempre: Boolean = false) {
        if (!siempre && !hayPendientes(context)) return
        val estado = EnlaceWatch.consultar(context) ?: return
        sincronizar(context, estado, EnlaceWatch.info.value.nodo ?: return)
    }

    fun ponerseAlDiaEnFondo(context: Context, siempre: Boolean = false) {
        val app = context.applicationContext
        alcance.launch { runCatching { ponerseAlDia(app, siempre) }.onFailure { Log.w(TAG, "No se pudo poner al día con el reloj", it) } }
    }

    /**
     * La grabación que el reloj está haciendo y que Grabar tiene que seguir en vez de pedir otra: según
     * un estado de hace menos de [ESTADO_RECIENTE_MS] y sólo si esa clase sigue abierta en el celu (o el
     * celu todavía no la adoptó).
     */
    fun grabacionParaSeguir(context: Context, ahoraCelu: Long = SystemClock.elapsedRealtime()): GrabacionWatch? {
        val info = EnlaceWatch.info.value
        if (ahoraCelu - info.estadoEn > ESTADO_RECIENTE_MS) return null
        val g = info.estado?.activa ?: return null
        val s = AlmacenSesiones.de(context).leerSesion(g.id) ?: return g
        return g.takeIf { s.delReloj && s.estadoLocal == EstadoLocal.GRABANDO }
    }

    /**
     * Grabar con el reloj cuando el reloj ya está grabando una clase (se empezó allá): el celu la sigue. Con
     * Ambos ([conCelu]), el celu graba también su pista desde ahora.
     */
    fun seguirEnCurso(context: Context, id: String, conCelu: Boolean = false) {
        val app = context.applicationContext
        if (conCelu) conCeluPedido[id] = SystemClock.elapsedRealtime()
        alcance.launch {
            runCatching { ponerseAlDia(app, siempre = true) }
            val s = AlmacenSesiones.de(app).leerSesion(id)
            // Con Ambos se manda aunque ya la siga: si se enganchó sin el micrófono, el servicio suma la pista.
            if (s != null && s.estadoLocal == EstadoLocal.GRABANDO && (conCelu || !esActiva(id))) ControlGrabacion.enganchar(app, id, conCelu)
        }
    }

    /**
     * "Cerrar con lo recibido": el reloj no aparece (se quedó sin batería, se perdió) y el usuario prefiere
     * subir la clase con el audio que ya llegó. Si después aparece el resto, queda en el reloj.
     */
    fun cerrarConLoRecibido(context: Context, id: String) {
        val app = context.applicationContext
        alcance.launch {
            candado.withLock {
                val almacen = AlmacenSesiones.de(app)
                val s = almacen.leerSesion(id) ?: return@withLock
                // También una de Ambos que figura grabando sin que nadie la siga y en la que el reloj nunca grabó.
                val cerrable = s.estadoLocal == EstadoLocal.RECIBIENDO || Ambos.cerrableSinReloj(almacen, s)
                if (s.delReloj && cerrable && !esActiva(id)) {
                    Log.w(TAG, "Sesión $id cerrada a mano con lo recibido del reloj")
                    // Desde acá no se le agrega nada (ya va al server así) y el reloj conserva su copia entera.
                    RegistrosWatch(almacen).cambiar(id) { it.copy(cerradaSinWatch = true) }
                    cerrar(app, almacen, s, null, interrumpida = true)
                }
            }
        }
    }

    suspend fun sincronizar(context: Context, estado: EstadoWatch, nodo: String) = candado.withLock {
        val almacen = AlmacenSesiones.de(context)
        val registros = RegistrosWatch(almacen)
        // Un "tocá para grabar" que el celu ya no espera (terminó o se pasó al teléfono, y el CANCELAR
        // no llegó): se saca, para que nadie grabe una clase nueva con ese id.
        estado.esperando?.let { id ->
            val ahora = System.currentTimeMillis()
            // Una clase del reloj que sigue "grabando" (el celu se reinició esperando el toque) todavía lo espera.
            val sigueEsperando = almacen.leerSesion(id)?.let { it.delReloj && it.estadoLocal == EstadoLocal.GRABANDO } == true
            if (!esActiva(id) && !sigueEsperando && ahora - (cancelarPedido[id] ?: 0) > REPETIR_CANCELAR_MS) {
                cancelarPedido[id] = ahora
                EnlaceWatch.ordenar(context, Rutas.CANCELAR) { Orden(n = it, id = id) }
            }
        }
        // Las que se están grabando primero: así el celu se engancha antes de traer audio viejo.
        for (g in estado.grabaciones.sortedByDescending { it.activa }) {
            if (!IdGrabacion.valido(g.id)) continue
            try {
                atender(context, almacen, registros, g, nodo, estado)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.w(TAG, "Grabación ${g.id} del reloj", e)
            }
        }
        // Sesiones del reloj que el reloj ya no tiene.
        for (s in almacen.listar()) {
            // (También una de Ambos que quedó del teléfono: si el reloj ya no tiene su copia, está borrada.)
            if ((!s.delReloj && !s.principalEsCelu) || estado.grabacion(s.id) != null) continue
            val r = registros.leer(s.id)
            if ((r.nodo != null && r.nodo != nodo) || r.cerradaSinWatch) continue
            when {
                r.verificada && !r.borradaEnWatch -> registros.guardar(s.id, r.copy(borradaEnWatch = true))
                // La tenía y ya no (por ejemplo, se borraron los datos de la app del reloj): se cierra con lo recibido.
                !r.verificada && r.vistaEnWatch && s.estadoLocal == EstadoLocal.RECIBIENDO && !esActiva(s.id) -> {
                    Log.w(TAG, "El reloj ya no tiene ${s.id}: se cierra con lo recibido")
                    cerrar(context, almacen, s, null, interrumpida = true)
                }
                // Se terminó en el celu sin haber visto al reloj grabar, y el reloj confirma que no la tiene ni
                // espera el toque: nunca empezó.
                !r.verificada && !r.vistaEnWatch && s.estadoLocal == EstadoLocal.RECIBIENDO && !esActiva(s.id) && estado.esperando != s.id -> {
                    Log.i(TAG, "El reloj nunca empezó ${s.id}: se cierra sin su audio")
                    if (!descartarSiVacia(almacen, s)) cerrar(context, almacen, s, null, interrumpida = true)
                }
                // Nadie la graba: ni el celu la sigue, ni el reloj la tiene o espera el toque (por ejemplo,
                // el celu se reinició esperando el toque y el pedido venció).
                !r.verificada && s.estadoLocal == EstadoLocal.GRABANDO && !esActiva(s.id) && estado.esperando != s.id &&
                    (r.vistaEnWatch || System.currentTimeMillis() - Tiempo.epochMs(s.inicio) > MARGEN_HUERFANA_MS) -> {
                    Log.w(TAG, "Nadie graba ${s.id}: se cierra con lo recibido")
                    if (!descartarSiVacia(almacen, s)) cerrar(context, almacen, s, null, interrumpida = true)
                }
            }
        }
    }

    private suspend fun atender(
        context: Context,
        almacen: AlmacenSesiones,
        registros: RegistrosWatch,
        g: GrabacionWatch,
        nodo: String,
        estado: EstadoWatch,
    ) {
        val modelo = estado.modelo
        var s = almacen.leerSesion(g.id) ?: adoptar(context, almacen, g, modelo) ?: return
        if (!s.delReloj) {
            // Ambos que quedó del teléfono porque el audio del reloj no tenía sonido: el celu tiene esa copia
            // verificada (nunca se borra) y la clase ya está en el server, así que el reloj puede borrar la suya.
            if (s.principalEsCelu && borrarCopiaSinUso(context, almacen, registros, g)) return
            // El celu se pasó a grabar él justo cuando se tocó el reloj: se frena la grabación del reloj. No
            // se borra: puede tener minutos que el celu no grabó. Queda en el reloj (se rescata a mano).
            // Una importada tampoco se toca (ni su audio ni su watch.json).
            apartar(context, g, if (s.importada) "esa clase es importada" else "el celu grabó esa clase con el teléfono")
            return
        }
        if (registros.leer(g.id).cerradaSinWatch) {
            // Se cerró a mano con lo recibido: no se le agrega nada más. El reloj deja de grabar y conserva su copia.
            apartar(context, g, "la clase se cerró con lo recibido")
            return
        }
        if (s.terminada && !registros.leer(g.id).verificada) {
            // La clase ya se cerró en el celu sin el audio del reloj (por ejemplo, se terminó antes de que el
            // reloj empezara y su pedido no se pudo cancelar): ese audio no se pega a una clase ya cerrada.
            apartar(context, g, "la clase ya se había cerrado sin su audio")
            return
        }
        val ahora = System.currentTimeMillis()
        // Mientras graba (sin cargar), la batería: así cada clase deja medido cuánto gastó el reloj.
        val lectura = if (g.activa && !estado.cargando && estado.bateria != null) LecturaBateria(estado.bateria, estado.cargaUah, ahora) else null
        var r = registros.cambiar(g.id) {
            it.copy(
                nodo = nodo, modelo = modelo, vistaEnWatch = true, grabacion = g, ultimoContacto = ahora,
                bateriaInicio = it.bateriaInicio ?: lectura,
                bateriaUltima = lectura ?: it.bateriaUltima,
            )
        }

        // Una sesión "grabando" sin el servicio del celu que la atienda (el celu se reinició, o empezó en el reloj).
        if (s.estadoLocal == EstadoLocal.GRABANDO && !esActiva(s.id)) {
            if (g.activa) {
                val ahora = System.currentTimeMillis()
                if (ahora - (enganchePedido[g.id] ?: 0) > REPETIR_ENGANCHE_MS) {
                    enganchePedido[g.id] = ahora
                    // Con Ambos elegido (o recién tocado Grabar) se pide también la pista del celu. Android sólo da
                    // el micrófono con la app a la vista; si no, el servicio la sigue sin ella.
                    val pedida = conCeluPedido.remove(g.id)?.let { SystemClock.elapsedRealtime() - it < CON_CELU_VIGENTE_MS } == true
                    ControlGrabacion.enganchar(context, s.id, pedida || Ajustes(context).fuenteAudio == FuenteAudio.AMBOS)
                }
            } else {
                s = s.copy(estadoLocal = EstadoLocal.RECIBIENDO, fin = s.fin ?: Tiempo.ahoraIso())
                almacen.guardarSesion(s)
            }
        }
        // La clase terminó en el celu pero al reloj no le llegó: se le vuelve a pedir.
        if (s.estadoLocal == EstadoLocal.RECIBIENDO && g.activa) {
            val ahora = System.currentTimeMillis()
            if (ahora - (terminarPedido[g.id] ?: 0) > REPETIR_TERMINAR_MS) {
                terminarPedido[g.id] = ahora
                EnlaceWatch.ordenar(context, Rutas.TERMINAR) { Orden(n = it, id = g.id) }
            }
        }

        if (!r.verificada) traerAudio(context, almacen, registros, g, nodo)
        r = registros.leer(g.id)
        s = almacen.leerSesion(g.id) ?: return

        if (r.verificada && s.estadoLocal == EstadoLocal.RECIBIENDO && !esActiva(s.id)) cerrar(context, almacen, s, g, g.interrumpida)
        if (r.verificada && !r.recibidaAvisada) {
            if (EnlaceWatch.ordenar(context, Rutas.RECIBIDA) { Orden(n = it, id = g.id, sha256 = r.sha256) } != null) {
                registros.cambiar(g.id) { it.copy(recibidaAvisada = true) }
            }
        }
        // El server ya tiene la clase y el celu la copia verificada: el reloj puede borrar la suya.
        val subida = almacen.leerSubida(g.id)
        if (r.verificada && s.terminada && subida.finalizada && subida.sha256.equals(r.sha256, ignoreCase = true) && subida.sha256Bytes == g.bytes) {
            val respuesta = EnlaceWatch.ordenar(context, Rutas.BORRAR) { Orden(n = it, id = g.id, sha256 = r.sha256) }
            if (respuesta != null && respuesta.grabacion(g.id) == null) registros.cambiar(g.id) { it.copy(borradaEnWatch = true) }
        }
    }

    /**
     * La grabación del reloj de una clase de Ambos que quedó del teléfono: si el celu tiene su copia
     * verificada (misma huella) y la clase ya se finalizó en el server, se le pide al reloj que la borre, como
     * con cualquier clase subida. Devuelve false si todavía no corresponde.
     */
    private suspend fun borrarCopiaSinUso(context: Context, almacen: AlmacenSesiones, registros: RegistrosWatch, g: GrabacionWatch): Boolean {
        val r = registros.leer(g.id)
        if (!g.terminada || !r.verificada || r.sha256 == null) return false
        // Como con cualquier clase: el reloj sabe enseguida que el celu la tiene, y puede soltar su primer plano.
        if (!r.recibidaAvisada && EnlaceWatch.ordenar(context, Rutas.RECIBIDA) { Orden(n = it, id = g.id, sha256 = r.sha256) } != null) {
            registros.cambiar(g.id) { it.copy(recibidaAvisada = true) }
        }
        if (!almacen.leerSubida(g.id).finalizada) return false
        if (r.borradaEnWatch) return true
        val respuesta = EnlaceWatch.ordenar(context, Rutas.BORRAR) { Orden(n = it, id = g.id, sha256 = r.sha256) }
        if (respuesta != null && respuesta.grabacion(g.id) == null) registros.cambiar(g.id) { it.copy(borradaEnWatch = true) }
        return true
    }

    /** Trae lo que falte del audio; si el reloj terminó, verifica la huella. */
    private suspend fun traerAudio(context: Context, almacen: AlmacenSesiones, registros: RegistrosWatch, g: GrabacionWatch, nodo: String) {
        // Ya se bajó entera varias veces y no coincide: no se insiste (en la lista se ofrece cerrar con lo recibido).
        if (registros.leer(g.id).rebajadas >= MAX_REBAJADAS) return
        val audio = almacen.archivoAudio(g.id)
        repeat(MAX_PEDIDOS) {
            // Si mientras tanto el reloj empezó otra clase que el celu todavía no sigue, se suelta el candado
            // entre tramo y tramo: la próxima pasada la engancha primero y después sigue con ésta.
            if (!g.activa && claseNuevaEsperando(context, g.id)) return
            when (val paso = PlanTransferencia.decidir(if (audio.exists()) audio.length() else 0, g)) {
                is PlanTransferencia.Paso.Pedir -> {
                    val r = EnlaceWatch.pedirAudio(context, nodo, g.id, paso.desde, audio)
                    if (r.agregados > 0) registros.cambiar(g.id) { it.copy(ultimaRecepcion = System.currentTimeMillis()) }
                    Log.i(TAG, "Audio ${g.id}: ${r.fin} +${r.agregados} B (repetidos ${r.repetidos}) = ${r.largo}/${g.bytes} ${r.detalle ?: ""}")
                    when (r.fin) {
                        FinRecepcion.DISTINTA -> {
                            rebajar(context, almacen, registros, g, nodo)
                            return
                        }
                        FinRecepcion.COMPLETA -> Unit
                        // Sin avance (se cortó el canal enseguida): se sigue con el próximo estado.
                        else -> if (r.agregados == 0L) return
                    }
                }
                PlanTransferencia.Paso.Verificar -> {
                    val sha = Huella.sha256(audio)
                    if (PlanTransferencia.verificada(audio.length(), sha, g)) {
                        Log.i(TAG, "Audio ${g.id} verificado: ${audio.length()} B, $sha")
                        registros.cambiar(g.id) { it.copy(verificada = true, sha256 = sha) }
                    } else {
                        Log.w(TAG, "La huella de ${g.id} no coincide ($sha vs ${g.sha256}): se baja entera")
                        rebajar(context, almacen, registros, g, nodo)
                    }
                    return
                }
                PlanTransferencia.Paso.Rebajar -> {
                    rebajar(context, almacen, registros, g, nodo)
                    return
                }
                PlanTransferencia.Paso.Nada -> return
            }
        }
    }

    /**
     * La copia del celu no coincide con la del reloj (algo raro: el canal tiene CRC por trama). Se baja
     * entera a un archivo aparte y, si su huella coincide (o el reloj sigue grabando), reemplaza a la del
     * celu de una vez. Si el server ya tenía bytes de la copia mala, el `/finalizar` da `hash_distinto`
     * y la subida manda el archivo entero de nuevo (§5.1).
     */
    private suspend fun rebajar(context: Context, almacen: AlmacenSesiones, registros: RegistrosWatch, g: GrabacionWatch, nodo: String) {
        val r = registros.leer(g.id)
        if (r.rebajadas >= MAX_REBAJADAS) {
            Log.e(TAG, "Ya se bajó ${g.id} entera ${r.rebajadas} veces y sigue sin coincidir: se deja como está")
            return
        }
        // Si una bajada anterior se cortó, se sigue desde donde quedó (el receptor comprueba lo repetido).
        val tmp = File(almacen.dir(g.id), "audio.watch.tmp")
        if (tmp.length() > g.bytes) tmp.delete()
        repeat(MAX_PEDIDOS) {
            val largo = if (tmp.exists()) tmp.length() else 0L
            if (largo >= g.bytes) return@repeat
            val rec = EnlaceWatch.pedirAudio(context, nodo, g.id, largo, tmp)
            if (rec.fin != FinRecepcion.COMPLETA && rec.agregados == 0L) return
        }
        if (tmp.length() < g.bytes) return
        val sha = Huella.sha256(tmp)
        if (g.terminada && !PlanTransferencia.verificada(tmp.length(), sha, g)) {
            Log.e(TAG, "La copia nueva de ${g.id} tampoco coincide con la huella del reloj")
            registros.cambiar(g.id) { it.copy(rebajadas = it.rebajadas + 1) }
            tmp.delete()
            return
        }
        Files.move(tmp.toPath(), almacen.archivoAudio(g.id).toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        if (g.terminada) registros.cambiar(g.id) { it.copy(verificada = true, sha256 = sha) }
        Log.i(TAG, "Audio ${g.id} reemplazado por la copia del reloj (${g.bytes} B)")
    }

    /**
     * Una grabación del reloj que no va a ninguna clase del celu: si sigue grabando se la frena, y nunca se
     * borra (en el reloj figura "por pasar" y se puede rescatar con adb, ver android/README.md).
     */
    private suspend fun apartar(context: Context, g: GrabacionWatch, motivo: String) {
        if (g.activa) {
            val ahora = System.currentTimeMillis()
            if (ahora - (terminarPedido[g.id] ?: 0) > REPETIR_TERMINAR_MS) {
                terminarPedido[g.id] = ahora
                EnlaceWatch.ordenar(context, Rutas.TERMINAR) { Orden(n = it, id = g.id) }
            }
        }
        Log.w(TAG, "Grabación ${g.id} del reloj apartada: $motivo")
    }

    /** El reloj está grabando una clase que el celu todavía no sigue (y no es [actual]). */
    private fun claseNuevaEsperando(context: Context, actual: String): Boolean {
        val g = EnlaceWatch.info.value.estado?.activa ?: return false
        if (g.id == actual || esActiva(g.id)) return false
        val s = AlmacenSesiones.de(context).leerSesion(g.id) ?: return true
        return s.delReloj && s.estadoLocal == EstadoLocal.GRABANDO
    }

    /**
     * La sesión del celu queda terminada con el audio del reloj: la subida la finaliza como siempre. Sin [g]
     * (con lo recibido, o el reloj nunca empezó), una de Ambos sigue con la pista del celu, y si del reloj no
     * llegó nada que sirva, queda como del teléfono ([Ambos.cerrarSinReloj]).
     */
    private fun cerrar(context: Context, almacen: AlmacenSesiones, s: SesionLocal, g: GrabacionWatch?, interrumpida: Boolean) {
        // Sin el audio entero del reloj, o con uno que no sirve (se cortó apenas empezó): con Ambos sigue la
        // pista del celu.
        if (g == null || Ambos.relojVacio(almacen, s)) {
            Ambos.cerrarSinReloj(almacen, s, Ajustes(context).dispositivo, interrumpida)?.let { c ->
                Log.i(TAG, "Sesión ${s.id} de Ambos cerrada sin más audio del reloj (${if (c.principalEsCelu) "como del teléfono" else "con lo recibido"})")
                PlanificadorSubida.patear(context)
                return
            }
        }
        // Si la app se cayó con la pista del celu abierta, se cierra con lo que tiene.
        if (s.pistaCelu) PistasCelu(almacen).cerrarSiQuedoAbierta(s.id)
        val audio = almacen.archivoAudio(s.id)
        // La duración del reloj; si se cortó allá, la que dice el propio Ogg.
        val duracion = g?.duracionMs?.takeIf { !interrumpida } ?: runCatching { OggDuracion.leerMs(audio) }.getOrNull() ?: s.duracionMs
        almacen.guardarSesion(
            s.copy(estadoLocal = EstadoLocal.TERMINADA, duracionMs = duracion, fin = s.fin ?: Tiempo.ahoraIso(), interrumpida = s.interrumpida || interrumpida),
        )
        Log.i(TAG, "Sesión ${s.id} cerrada con el audio del reloj (${audio.length()} B, $duracion ms)")
        PlanificadorSubida.patear(context)
    }

    /**
     * Una grabación que empezó en el reloj: el celu arma la sesión con lo que sabe (la materia del
     * calendario, o la de la clase que continúa si la grabadora del reloj falló) y se engancha.
     */
    private fun adoptar(context: Context, almacen: AlmacenSesiones, g: GrabacionWatch, modelo: String): SesionLocal? {
        val ajustes = Ajustes(context)
        val inicioMs = Tiempo.epochMs(g.inicio).takeIf { it > 0 } ?: System.currentTimeMillis()
        val anterior = g.continuaDe?.let { almacen.leerSesion(it) }?.takeIf { it.delReloj }
        val estadoLocal = if (g.activa) EstadoLocal.GRABANDO else EstadoLocal.RECIBIENDO
        val sesion = if (anterior != null) {
            continuacion(anterior, g)
        } else {
            val materias = ajustes.materiasCache
            val sugerencia = runCatching {
                ReglasCalendario.sugerir(materias, LectorCalendario.eventosCercanos(context, inicioMs), inicioMs)
            }.getOrNull()
            val sel = SeleccionMateria.aplicarCalendario(Seleccion(), sugerencia, materias)
            val numero = SeleccionMateria.numeroClase(sel, materias)
            if (numero != null) ajustes.materiasCache = SeleccionMateria.contarClase(materias, sel.materiaId)
            val finEvento = sel.finEventoMs.takeIf { sel.origen == OrigenMateria.CALENDARIO }
            SesionLocal(
                id = g.id,
                materiaId = sel.materiaId,
                origenMateria = sel.origen,
                tipoClase = g.tipoClase ?: sel.tipoClase,
                inicio = g.inicio,
                dispositivo = nombreDispositivo(modelo),
                numeroClase = numero,
                estadoLocal = estadoLocal,
                nombreMateria = materias.firstOrNull { it.id == sel.materiaId }?.nombre,
                eventoCalendario = sel.eventoCalendario,
                finEventoMs = finEvento,
                preguntarCorteMs = CorteAutomatico.primeraPregunta(inicioMs, finEvento),
                fuenteAudio = FuenteAudio.RELOJ,
            )
        }
        almacen.crear(sesion)
        Log.i(TAG, "Grabación ${g.id} empezada en el reloj: sesión nueva (${sesion.nombreMateria ?: "Auto"})")
        if (g.activa) {
            ClassmerApp.avisar(
                context,
                "El reloj está grabando",
                "Se empezó a grabar desde el reloj (${sesion.nombreMateria ?: "Auto (IA)"}). Las fotos y marcas del celu van a esta clase.",
            )
        }
        return sesion
    }

    /** La sesión que sigue a [anterior] cuando la grabadora del reloj falló y siguió en la grabación [g]. */
    fun continuacion(anterior: SesionLocal, g: GrabacionWatch): SesionLocal = anterior.copy(
        id = g.id, inicio = g.inicio, estadoLocal = if (g.activa) EstadoLocal.GRABANDO else EstadoLocal.RECIBIENDO,
        duracionMs = 0, fin = null, interrumpida = false, continuaDe = anterior.id, corteAutomatico = false,
    )

    /**
     * Una clase sin audio: si tampoco tiene fotos, notas ni marcas, se borra y devuelve true; si tiene
     * algo, no se toca (la cierra quien llama, así no se pierde). La pista del celu (Ambos) cuenta como audio.
     */
    fun descartarSiVacia(almacen: AlmacenSesiones, s: SesionLocal): Boolean {
        val sinAudio = listOf(almacen.archivoAudio(s.id), almacen.archivoAudio(s), PistasCelu(almacen).audio(s.id)).all { it.length() == 0L }
        val vacia = sinAudio && almacen.leerEventos(s.id).isEmpty() &&
            almacen.dirFotos(s.id).list().isNullOrEmpty()
        if (vacia) {
            Log.i(TAG, "Sesión ${s.id} sin audio ni eventos: se descarta")
            almacen.dir(s.id).deleteRecursively()
        }
        return vacia
    }

    /** El `dispositivo` que va al server: dice que grabó el reloj (por ejemplo, `galaxy-watch8-classic`). */
    fun nombreDispositivo(modelo: String): String {
        val m = modelo.uppercase()
        return when {
            m.startsWith("SM-L50") -> "galaxy-watch8-classic"
            m.startsWith("SM-L3") -> "galaxy-watch8"
            m.isBlank() -> "reloj"
            else -> "reloj-" + modelo.lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-')
        }
    }

    private fun esActiva(id: String) = ControlGrabacion.estado.value.sesion?.id == id
}
