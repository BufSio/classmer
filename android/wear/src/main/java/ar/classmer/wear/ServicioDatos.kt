package ar.classmer.wear

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.content.ContextCompat
import ar.classmer.watchlink.EmisorAudio
import ar.classmer.watchlink.IdGrabacion
import ar.classmer.watchlink.Orden
import ar.classmer.watchlink.Ping
import ar.classmer.watchlink.Pong
import ar.classmer.watchlink.Rutas
import ar.classmer.watchlink.codificar
import ar.classmer.watchlink.decodificar
import com.google.android.gms.tasks.Tasks
import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.Wearable
import com.google.android.gms.wearable.WearableListenerService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Lo que llega del celu por el Data Layer. Google Play services levanta este servicio aunque la app
 * no esté abierta. Cada orden se contesta con el estado del reloj; el audio se manda por un canal que
 * abre el celu ([Rutas.audio]), en un hilo aparte para no demorar los pings ni las órdenes.
 */
class ServicioDatos : WearableListenerService() {

    override fun onMessageReceived(evento: MessageEvent) {
        val recibido = SystemClock.elapsedRealtime()
        val nodo = evento.sourceNodeId
        if (evento.path == Rutas.PING) {
            val ping = decodificar(Ping.serializer(), evento.data) ?: return
            // t3 se toma justo antes de mandar: lo que tarda el reloj en contestar no cuenta como viaje.
            val pong = Pong(t1 = ping.t1, t2 = recibido, t3 = SystemClock.elapsedRealtime())
            Wearable.getMessageClient(this).sendMessage(nodo, Rutas.PONG, codificar(Pong.serializer(), pong))
            return
        }
        val orden = decodificar(Orden.serializer(), evento.data) ?: return
        Log.i(TAG, "${evento.path} ${orden.id ?: ""}")
        val almacen = AlmacenWatch.de(this)
        val id = orden.id?.takeIf { IdGrabacion.valido(it) }
        when (evento.path) {
            Rutas.INICIAR -> if (id != null) pedirToque(almacen, orden, id)
            Rutas.CANCELAR -> if (id != null) {
                if (almacen.leerPedido()?.id == id) {
                    almacen.guardarPedido(null)
                    Avisos.cancelarPedido(this)
                    ControlWatch.refrescar(this)
                } else if (ControlWatch.estado.value.grabacion?.id == id) {
                    // Se tocó en el reloj justo cuando el celu se pasó a grabar él: queda como una grabación aparte.
                    if (ControlWatch.enviar(this, ServicioGrabacionWatch.ACCION_TERMINAR, orden.n, nodo)) return
                }
            }
            Rutas.PAUSAR, Rutas.REANUDAR, Rutas.TERMINAR -> {
                val accion = when (evento.path) {
                    Rutas.PAUSAR -> ServicioGrabacionWatch.ACCION_PAUSAR
                    Rutas.REANUDAR -> ServicioGrabacionWatch.ACCION_REANUDAR
                    else -> ServicioGrabacionWatch.ACCION_TERMINAR
                }
                // El servicio contesta con el estado ya cambiado.
                if (id != null && ControlWatch.estado.value.grabacion?.id == id && ControlWatch.enviar(this, accion, orden.n, nodo)) return
            }
            Rutas.RECIBIDA -> if (id != null) {
                val g = almacen.leer(id)
                if (g != null && g.terminada && g.sha256.equals(orden.sha256, ignoreCase = true)) ControlWatch.confirmarRecibida(this, id)
            }
            Rutas.BORRAR -> if (id != null) {
                val g = almacen.leer(id)
                if (g != null && g.terminada && g.sha256 != null && g.sha256.equals(orden.sha256, ignoreCase = true)) {
                    Log.i(TAG, "El celu confirmó $id: se borra la copia del reloj")
                    almacen.borrar(id)
                    ControlWatch.refrescar(this)
                    ControlWatch.confirmarRecibida(this, id)
                }
            }
            Rutas.CONSULTAR -> Unit
            else -> return
        }
        EnlaceCelu.avisar(this, orden.n, nodo)
    }

    /** "Tocá para grabar": el micrófono sólo se puede prender con un toque en el reloj. */
    private fun pedirToque(almacen: AlmacenWatch, orden: Orden, id: String) {
        val activa = ControlWatch.estado.value.grabacion
        if (almacen.leer(id) != null || activa != null) return // ya existe, o se está grabando otra: el celu lo ve en el estado
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            ControlWatch.mostrarError("El reloj no tiene permiso para mostrar avisos: abrí Classmer en el reloj una vez.")
        }
        val pedido = PedidoInicio(id = id, materia = orden.materia, tipoClase = orden.tipoClase, recibido = System.currentTimeMillis())
        almacen.guardarPedido(pedido)
        ControlWatch.refrescar(this)
        Avisos.pedirToque(this, pedido)
    }

    override fun onChannelOpened(canal: ChannelClient.Channel) {
        val pedido = Rutas.leerAudio(canal.path)
        val cliente = Wearable.getChannelClient(this)
        if (pedido == null) {
            cliente.close(canal)
            return
        }
        val app = applicationContext
        val enviar = Runnable {
            val (id, desde) = pedido
            val almacen = AlmacenWatch.de(app)
            // Con la pantalla apagada y sin grabar, la CPU se podría dormir a mitad del envío.
            val wl = (app.getSystemService(Context.POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "classmer:envio-audio")
                .apply { acquire(ESPERA_ENVIO_MS) }
            ControlWatch.avisarEnvio(app)
            try {
                val g = almacen.leer(id)
                // De a tramos: el celu vuelve a pedir desde lo que tiene, y mientras tanto se atienden sus órdenes.
                val hasta = if (g == null) -1 else minOf(almacen.bytesFirmes(id), desde + MAX_POR_ENVIO)
                if (g == null || desde > hasta) {
                    Log.w(TAG, "Pedido de audio inválido: $id desde $desde (hay $hasta)")
                    return@Runnable
                }
                Tasks.await(cliente.getOutputStream(canal), ESPERA_S, TimeUnit.SECONDS).use { salida ->
                    val n = EmisorAudio.enviar(almacen.archivoAudio(id), desde, hasta, salida)
                    Log.i(TAG, "Audio $id: mandados $n bytes desde $desde")
                }
            } catch (e: Exception) {
                // El canal se cortó (se fue la conexión): el celu vuelve a pedir desde lo que tiene.
                Log.i(TAG, "Envío de audio $id cortado: ${e.message}")
            } finally {
                runCatching { cliente.close(canal) }
                if (wl.isHeld) wl.release()
            }
        }
        // Mientras graba (o espera que el celu confirme), el servicio en primer plano mantiene vivo el
        // proceso: el envío va aparte para no demorar los pings. Si no, va dentro del callback, que
        // mantiene el servicio enlazado mientras dura (y el proceso no se congela a mitad de camino).
        if (ControlWatch.servicioActivo) envios.execute(enviar) else enviar.run()
    }

    companion object {
        private const val TAG = "ClassmerWatchDatos"
        private const val ESPERA_S = 30L
        private const val ESPERA_ENVIO_MS = 20 * 60_000L

        /** Lo que se manda por canal: unos 3 min de audio. */
        private const val MAX_POR_ENVIO = 1L shl 20

        /** Un envío a la vez: el celu pide de a una grabación. */
        private val envios = Executors.newSingleThreadExecutor()
    }
}
