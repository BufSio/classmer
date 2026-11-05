package ar.classmer.wear

import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.os.SystemClock
import android.util.Log
import ar.classmer.watchlink.Capacidades
import ar.classmer.watchlink.EstadoWatch
import ar.classmer.watchlink.Rutas
import ar.classmer.watchlink.codificar
import com.google.android.gms.wearable.CapabilityClient
import com.google.android.gms.wearable.Wearable
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withTimeoutOrNull

/**
 * El lado del reloj del Data Layer: arma el [EstadoWatch] y se lo manda al celu. El reloj nunca
 * espera nada del celu para seguir grabando: si no hay conexión, el mensaje se pierde y el celu se
 * pone al día con el próximo (lo pide él cada 30 s mientras graba).
 */
object EnlaceCelu {
    private const val TAG = "ClassmerWatchEnlace"
    private val alcance = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    fun estado(context: Context, respuestaA: Long? = null): EstadoWatch {
        // Se toma antes de leer las grabaciones: el celu descarta un estado con `ahora` más viejo que
        // el último que vio, así que uno armado antes de un cambio nunca pisa al de después.
        val ahora = SystemClock.elapsedRealtime()
        val almacen = AlmacenWatch.de(context)
        val grabaciones = almacen.listar().map { g -> if (g.activa) g.copy(bytes = almacen.bytesFirmes(g.id)) else g }
        val bm = context.getSystemService(BatteryManager::class.java)
        return EstadoWatch(
            respuestaA = respuestaA,
            ahora = ahora,
            bateria = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)?.takeIf { it in 0..100 },
            cargando = bm?.isCharging == true,
            cargaUah = bm?.getLongProperty(BatteryManager.BATTERY_PROPERTY_CHARGE_COUNTER)?.takeIf { it > 0 && it != Long.MIN_VALUE },
            modelo = Build.MODEL ?: "",
            versionApp = BuildConfig.VERSION_NAME,
            esperando = almacen.leerPedido()?.id,
            grabaciones = grabaciones,
            error = ControlWatch.estado.value.error,
        )
    }

    /** Manda el estado al celu sin esperar (desde cualquier hilo). [nodo] `null` = a los celus conectados. */
    fun avisar(context: Context, respuestaA: Long? = null, nodo: String? = null) {
        val app = context.applicationContext
        alcance.launch {
            runCatching { mandar(app, Rutas.ESTADO, codificar(EstadoWatch.serializer(), estado(app, respuestaA)), nodo) }
                .onFailure { Log.i(TAG, "No se pudo mandar el estado: ${it.message}") }
        }
    }

    suspend fun mandar(context: Context, ruta: String, datos: ByteArray, nodo: String? = null) {
        val cliente = Wearable.getMessageClient(context)
        for (n in nodo?.let { listOf(it) } ?: nodosCelu(context)) {
            runCatching { withTimeoutOrNull(ESPERA_MS) { cliente.sendMessage(n, ruta, datos).await() } }
                .onFailure { Log.i(TAG, "$ruta a $n: ${it.message}") }
        }
    }

    /** Los celus con Classmer; si la capacidad no aparece (versión vieja), todos los conectados. */
    private suspend fun nodosCelu(context: Context): List<String> {
        val conCapacidad = runCatching {
            Wearable.getCapabilityClient(context).getCapability(Capacidades.CELU, CapabilityClient.FILTER_REACHABLE).await()
                .nodes.map { it.id }
        }.getOrDefault(emptyList())
        if (conCapacidad.isNotEmpty()) return conCapacidad
        return runCatching { Wearable.getNodeClient(context).connectedNodes.await().map { it.id } }.getOrDefault(emptyList())
    }

    private const val ESPERA_MS = 10_000L
}
