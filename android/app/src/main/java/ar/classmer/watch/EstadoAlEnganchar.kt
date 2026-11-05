package ar.classmer.watch

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.grabacion.EstadoGrabacion
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.TipoEvento
import ar.classmer.modelo.TipoMarca

/**
 * El estado de la pantalla de grabación al seguir una clase del reloj. Si el celu se engancha a una
 * clase que ya venía (se reinició, o empezó en el reloj), los contadores salen de `eventos.jsonl`.
 */
fun estadoAlEnganchar(almacen: AlmacenSesiones, sesion: SesionLocal, ui: EstadoWatchUi?): EstadoGrabacion {
    val eventos = almacen.leerEventos(sesion.id)
    return EstadoGrabacion(
        sesion = sesion,
        fotos = eventos.count { it.tipo == TipoEvento.FOTO },
        notas = eventos.count { it.tipo == TipoEvento.NOTA },
        importantes = eventos.count { it.tipo == TipoEvento.MARCA && it.marca == TipoMarca.IMPORTANTE },
        noEntendi = eventos.count { it.tipo == TipoEvento.MARCA && it.marca == TipoMarca.NO_ENTENDI },
        watch = ui,
    )
}
