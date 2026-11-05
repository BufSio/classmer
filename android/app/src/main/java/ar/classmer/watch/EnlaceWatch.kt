package ar.classmer.watch

import android.content.Context
import android.os.SystemClock
import android.util.Log
import ar.classmer.watchlink.Capacidades
import ar.classmer.watchlink.Desfase
import ar.classmer.watchlink.EstadoWatch
import ar.classmer.watchlink.Muestra
import ar.classmer.watchlink.Orden
import ar.classmer.watchlink.Ping
import ar.classmer.watchlink.Pong
import ar.classmer.watchlink.ReceptorAudio
import ar.classmer.watchlink.Recepcion
import ar.classmer.watchlink.Rutas
import ar.classmer.watchlink.Sincronizacion
import ar.classmer.watchlink.codificar
import ar.classmer.watchlink.decodificar
import com.google.android.gms.wearable.CapabilityClient
import com.google.android.gms.wearable.Wearable
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.io.FilterInputStream
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong

/** Lo que el celu sabe del Galaxy Watch, para Inicio y para la pantalla de grabación. */
data class InfoWatch(
    /** Ya se buscó el reloj al menos una vez. */
    val buscado: Boolean = false,
    /** Hay un reloj conectado (con o sin Classmer). */
    val conectado: Boolean = false,
    /** El reloj conectado tiene Classmer (capacidad `classmer_watch`). */
    val appInstalada: Boolean = false,
    val nombre: String? = null,
    /** Nodo del Data Layer del reloj con Classmer. */
    val nodo: String? = null,
    /** El último estado que mandó el reloj. */
    val estado: EstadoWatch? = null,
    /** `elapsedRealtime` del celu cuando llegó [estado]. */
    val estadoEn: Long = 0,
    /** La última consulta al reloj quedó sin respuesta. */
    val sinRespuesta: Boolean = false,
)

/**
 * El lado del celu del Data Layer: busca el reloj, le manda órdenes (y espera el estado con que las
 * contesta), mide el desfase entre relojes y trae el audio por un canal. Lo que llega del reloj entra
 * por [ServicioDatosWatch] y se reparte desde [recibir].
 */
object EnlaceWatch {
    private const val TAG = "ClassmerWatch"

    private val infoMutable = MutableStateFlow(InfoWatch())
    val info: StateFlow<InfoWatch> = infoMutable.asStateFlow()

    /** Número de orden: arranca en la hora, así no se repite entre reinicios de la app. */
    private val siguiente = AtomicLong(System.currentTimeMillis())
    private val respuestas = ConcurrentHashMap<Long, CompletableDeferred<EstadoWatch>>()
    private val pongs = ConcurrentHashMap<Long, CompletableDeferred<Pair<Pong, Long>>>()

    /** Busca el reloj: si está conectado y si tiene Classmer. */
    suspend fun buscar(context: Context): InfoWatch {
        val conectados = runCatching { Wearable.getNodeClient(context).connectedNodes.await() }
            .onFailure { Log.i(TAG, "Sin nodos: ${it.message}") }
            .getOrDefault(emptyList())
        val conApp = runCatching {
            Wearable.getCapabilityClient(context).getCapability(Capacidades.WATCH, CapabilityClient.FILTER_REACHABLE).await().nodes
        }.getOrDefault(emptySet())
        val nodo = conApp.firstOrNull { it.isNearby } ?: conApp.firstOrNull()
        val alguno = nodo ?: conectados.firstOrNull { it.isNearby } ?: conectados.firstOrNull()
        infoMutable.update {
            it.copy(
                buscado = true,
                conectado = alguno != null,
                appInstalada = nodo != null,
                nombre = alguno?.displayName ?: it.nombre,
                nodo = nodo?.id,
            )
        }
        return info.value
    }

    /**
     * Manda una orden y espera el estado con que la contesta el reloj. `null` si no hay reloj o no
     * contestó a tiempo (el reloj sigue grabando igual: nunca depende del celu).
     */
    suspend fun ordenar(context: Context, ruta: String, esperaMs: Long = ESPERA_MS, orden: (Long) -> Orden): EstadoWatch? {
        val nodo = info.value.nodo ?: buscar(context).nodo ?: return null
        val n = siguiente.incrementAndGet()
        val respuesta = CompletableDeferred<EstadoWatch>()
        respuestas[n] = respuesta
        try {
            Wearable.getMessageClient(context).sendMessage(nodo, ruta, codificar(Orden.serializer(), orden(n))).await()
            return withTimeoutOrNull(esperaMs) { respuesta.await() }
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            // Típicamente TARGET_NODE_NOT_CONNECTED: se vuelve a buscar el reloj para la próxima.
            Log.i(TAG, "$ruta no llegó al reloj: ${e.message}")
            buscar(context)
            return null
        } finally {
            respuestas.remove(n)
        }
    }

    suspend fun consultar(context: Context): EstadoWatch? =
        ordenar(context, Rutas.CONSULTAR) { Orden(n = it) }.also { e -> infoMutable.update { it.copy(sinRespuesta = e == null) } }

    /** Varias idas y vueltas de ping; se queda con la más rápida ([Sincronizacion]). */
    suspend fun medirDesfase(context: Context): Desfase? {
        val nodo = info.value.nodo ?: buscar(context).nodo ?: return null
        val cliente = Wearable.getMessageClient(context)
        val muestras = mutableListOf<Muestra>()
        repeat(Sincronizacion.MUESTRAS) {
            val t1 = SystemClock.elapsedRealtime()
            val pong = CompletableDeferred<Pair<Pong, Long>>()
            pongs[t1] = pong
            try {
                cliente.sendMessage(nodo, Rutas.PING, codificar(Ping.serializer(), Ping(t1))).await()
                withTimeoutOrNull(ESPERA_PONG_MS) { pong.await() }?.let { (p, t4) -> muestras += Muestra(p.t1, p.t2, p.t3, t4) }
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                Log.i(TAG, "Ping al reloj: ${e.message}")
            } finally {
                pongs.remove(t1)
            }
            delay(ENTRE_PINGS_MS)
        }
        return Sincronizacion.estimar(muestras, SystemClock.elapsedRealtime()).also {
            Log.i(TAG, "Desfase con el reloj: $it (${muestras.size} muestras)")
        }
    }

    /**
     * Trae por un canal el audio de [id] desde [desde] y lo agrega al final de [destino]. Si el reloj deja
     * de mandar sin cerrar el canal (por ejemplo, su proceso quedó congelado), a los [SIN_DATOS_MS] se
     * cierra el canal desde acá: la lectura termina como un corte y se vuelve a pedir después.
     */
    suspend fun pedirAudio(context: Context, nodo: String, id: String, desde: Long, destino: File): Recepcion = withContext(Dispatchers.IO) {
        val cliente = Wearable.getChannelClient(context)
        val canal = cliente.openChannel(nodo, Rutas.audio(id, desde)).await() ?: throw IOException("No se pudo abrir el canal con el reloj")
        val ultimoDato = AtomicLong(SystemClock.elapsedRealtime())
        val vigia = launch {
            while (isActive) {
                delay(VIGIA_MS)
                if (SystemClock.elapsedRealtime() - ultimoDato.get() > SIN_DATOS_MS) {
                    Log.w(TAG, "El reloj dejó de mandar audio de $id: se cierra el canal")
                    runCatching { cliente.close(canal).await() }
                    break
                }
            }
        }
        try {
            cliente.getInputStream(canal).await().use { entrada ->
                val vigilada = object : FilterInputStream(entrada) {
                    override fun read(): Int = super.read().also { if (it >= 0) ultimoDato.set(SystemClock.elapsedRealtime()) }
                    override fun read(b: ByteArray, off: Int, len: Int): Int =
                        super.read(b, off, len).also { if (it > 0) ultimoDato.set(SystemClock.elapsedRealtime()) }
                }
                ReceptorAudio(destino).recibir(vigilada)
            }
        } finally {
            vigia.cancel()
            runCatching { cliente.close(canal).await() }
        }
    }

    /** Lo que llega del reloj (desde [ServicioDatosWatch], en un hilo de fondo). */
    fun recibir(context: Context, ruta: String, datos: ByteArray, nodo: String) {
        when (ruta) {
            Rutas.PONG -> {
                val t4 = SystemClock.elapsedRealtime()
                val p = decodificar(Pong.serializer(), datos) ?: return
                pongs[p.t1]?.complete(p to t4)
            }
            Rutas.ESTADO -> {
                val e = decodificar(EstadoWatch.serializer(), datos) ?: return
                // Uno armado antes que el último que llegó (respuestas cruzadas) no pisa al más nuevo. Si
                // el reloj se reinició, su `ahora` vuelve a empezar: ahí se acepta.
                val previo = info.value.estado
                val viejo = previo != null && info.value.nodo == nodo && e.ahora < previo.ahora && previo.ahora - e.ahora < VENTANA_REINICIO_MS
                Log.i(
                    TAG,
                    "Estado del reloj: batería ${e.bateria} % (${e.cargaUah ?: "?"} µAh${if (e.cargando) ", cargando" else ""}) " +
                        e.grabaciones.joinToString { "${it.id.takeLast(6)} ${it.estado} ${it.bytes} B" },
                )
                if (!viejo) {
                    infoMutable.update {
                        it.copy(buscado = true, conectado = true, appInstalada = true, nodo = nodo, estado = e, estadoEn = SystemClock.elapsedRealtime())
                    }
                }
                // Si alguien esperaba esta respuesta, la atiende él; si no (el reloj lo mandó solo, o llegó
                // tarde), se pone al día acá.
                val esperada = e.respuestaA?.let { respuestas[it] }
                if (esperada != null) esperada.complete(e) else if (!viejo) SincronizacionWatch.alRecibirEstado(context, e, nodo)
            }
        }
    }

    private const val ESPERA_MS = 8_000L
    private const val VIGIA_MS = 5_000L
    private const val SIN_DATOS_MS = 45_000L
    private const val VENTANA_REINICIO_MS = 10 * 60_000L
    private const val ESPERA_PONG_MS = 3_000L
    private const val ENTRE_PINGS_MS = 150L
}
