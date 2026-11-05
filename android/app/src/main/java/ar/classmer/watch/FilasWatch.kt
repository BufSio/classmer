package ar.classmer.watch

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.SesionLocal
import ar.classmer.ui.FilaSesion
import ar.classmer.ui.Tono

/** Cómo se ve en "Clases recientes" una clase que grabó el reloj mientras su audio no terminó de llegar. */
object FilasWatch {
    const val SEGUIR = "Seguir en el celu"
    const val CERRAR = "Cerrar con lo recibido"

    /** Sin contacto con el reloj por más que esto, se ofrece cerrar la clase con lo que llegó. */
    const val OFRECER_CERRAR_MS = 30 * 60_000L

    /** Con Ambos se ofrece antes: el celu grabó su audio, así que la clase no espera al reloj. */
    const val OFRECER_CERRAR_AMBOS_MS = 5 * 60_000L

    /** La clase se finalizó sin la pista del celu: el audio del celu quedó sólo en el celu. */
    const val TEXTO_CELU_SIN_SUBIR = "El audio del celu quedó en el celu: el server no lo aceptó"

    /** `null` si la clase no es del reloj o no está esperando nada de él. */
    fun fila(base: FilaSesion, s: SesionLocal, almacen: AlmacenSesiones, activa: Boolean, ahoraMs: Long = System.currentTimeMillis()): FilaSesion? {
        if (!s.delReloj || activa) return null
        val r = RegistrosWatch(almacen).leer(s.id)
        val contacto = r.ultimoContacto?.let { ahoraMs - it }
        return when (s.estadoLocal) {
            EstadoLocal.GRABANDO -> if (Ambos.cerrableSinReloj(almacen, s)) {
                // Ambos, y el reloj nunca se vio grabar: el celu era lo único que grababa y se cortó.
                base.copy(
                    estado = "Se cortó",
                    detalle = "El celu grabó su audio; el reloj nunca empezó. Si el reloj aparece grabando, se sigue solo.",
                    tono = Tono.ACTIVO,
                    accionWatch = CERRAR,
                )
            } else {
                // El reloj graba y el celu no la está siguiendo (se reinició, o empezó en el reloj).
                base.copy(
                    estado = "Graba el reloj",
                    detalle = "Las fotos y marcas del celu van a esta clase si la seguís en el celu.",
                    tono = Tono.ACTIVO,
                    accionWatch = SEGUIR,
                )
            }
            EstadoLocal.RECIBIENDO -> {
                val local = almacen.archivoAudio(s.id).length()
                val total = r.grabacion?.bytes ?: 0
                val faltan = (total - local).coerceAtLeast(0)
                val detalle = buildString {
                    append(if (faltan > 0) "Faltan ${SesionWatch.mb(faltan)} del reloj" else "Verificando el audio del reloj")
                    contacto?.let { if (it > 2 * 60_000L) append(" · sin contacto hace ${minutos(it)}") }
                    if (s.pistaCelu) append(" · el celu grabó su audio")
                }
                val ofrecerDesde = if (s.pistaCelu) OFRECER_CERRAR_AMBOS_MS else OFRECER_CERRAR_MS
                base.copy(
                    estado = "Recibiendo del reloj",
                    detalle = detalle,
                    progreso = if (total > 0) (local.toFloat() / total).coerceIn(0f, 1f) else null,
                    tono = Tono.ACTIVO,
                    // Sin contacto un rato, o la huella no coincide ni bajándola entera: se puede cerrar con lo que hay.
                    accionWatch = if (contacto == null || contacto > ofrecerDesde || r.rebajadas >= SincronizacionWatch.MAX_REBAJADAS) CERRAR else null,
                )
            }
            else -> null
        }
    }

    private fun minutos(ms: Long): String {
        val min = ms / 60_000
        return if (min < 120) "$min min" else "${min / 60} h"
    }
}
