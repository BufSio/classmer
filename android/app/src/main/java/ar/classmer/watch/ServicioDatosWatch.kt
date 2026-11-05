package ar.classmer.watch

import ar.classmer.watchlink.Capacidades
import com.google.android.gms.wearable.CapabilityInfo
import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.WearableListenerService

/**
 * Lo que manda el Galaxy Watch (estado y pongs). Google Play services levanta este servicio aunque
 * Classmer no esté abierta: así el celu se entera de una grabación que empezó en el reloj.
 */
class ServicioDatosWatch : WearableListenerService() {

    override fun onMessageReceived(evento: MessageEvent) {
        EnlaceWatch.recibir(applicationContext, evento.path, evento.data, evento.sourceNodeId)
    }

    /** El reloj volvió a estar al alcance: se trae lo que haya quedado pendiente. */
    override fun onCapabilityChanged(info: CapabilityInfo) {
        if (info.name == Capacidades.WATCH && info.nodes.isNotEmpty()) SincronizacionWatch.ponerseAlDiaEnFondo(applicationContext)
    }
}
