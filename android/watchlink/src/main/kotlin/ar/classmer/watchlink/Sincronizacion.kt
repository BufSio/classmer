package ar.classmer.watchlink

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Una ida y vuelta de [Rutas.PING]: `t1` y `t4` con el reloj del celu (al mandar y al recibir la
 * respuesta), `t2` y `t3` con el del Watch (al recibir el ping y al contestar). Todos son
 * `elapsedRealtime` en ms.
 */
data class Muestra(val t1: Long, val t2: Long, val t3: Long, val t4: Long) {
    /** Lo que tardaron los mensajes en ir y volver, sin lo que tardó el Watch en contestar. */
    val idaYVuelta: Long get() = (t4 - t1) - (t3 - t2)

    /** Watch − celu, suponiendo que la ida y la vuelta tardan lo mismo. */
    val desfase: Long get() = Math.floorDiv((t2 - t1) + (t3 - t4), 2L)

    val valida: Boolean get() = t4 >= t1 && t3 >= t2 && idaYVuelta >= 0
}

/**
 * Diferencia entre los relojes: `watch = celu + ms`. Al medirlo, el valor real está a menos de
 * [errorMs]: con una ida de `a` y una vuelta de `b` (a + b = ida y vuelta), la estimación se corre
 * `(a − b) / 2`. [medidoEn] es el `elapsedRealtime` del celu al medirlo.
 */
@Serializable
data class Desfase(
    val ms: Long,
    @SerialName("error_ms") val errorMs: Long,
    @SerialName("ida_y_vuelta_ms") val idaYVueltaMs: Long,
    @SerialName("medido_en") val medidoEn: Long,
) {
    /** El error posible en [ahoraCelu]: el de la medición más lo que pudieron separarse los relojes desde entonces. */
    fun errorEn(ahoraCelu: Long): Long = errorMs + ((ahoraCelu - medidoEn).coerceAtLeast(0) * Sincronizacion.DERIVA_PPM) / 1_000_000
}

/**
 * La cuenta del desfase (tipo NTP): de varias idas y vueltas se queda con la más rápida, que es la
 * que menos error puede tener. Así los offsets de las fotos y marcas del celu se cuentan desde el
 * inicio de la grabación del Watch.
 */
object Sincronizacion {
    /** Cuántas idas y vueltas se hacen por medición. */
    const val MUESTRAS = 5

    /** Error objetivo: si la mejor medición queda por encima, se vuelve a medir. */
    const val ERROR_OBJETIVO_MS = 500L

    /**
     * Cuánto pueden separarse los relojes (`elapsedRealtime` corre con el cristal de cada equipo):
     * 50 ppm es un tope holgado, 0,18 s en una hora.
     */
    const val DERIVA_PPM = 50L

    /** `medidoEn` = cuándo terminó la medición (reloj del celu). */
    fun estimar(muestras: List<Muestra>, medidoEn: Long = muestras.maxOfOrNull { it.t4 } ?: 0): Desfase? =
        muestras.filter { it.valida }.minByOrNull { it.idaYVuelta }?.let {
            Desfase(ms = it.desfase, errorMs = (it.idaYVuelta + 1) / 2, idaYVueltaMs = it.idaYVuelta, medidoEn = medidoEn)
        }

    /** Entre la medición anterior y la nueva, la que tenga menos error posible ahora. */
    fun mejor(anterior: Desfase?, nueva: Desfase?, ahoraCelu: Long): Desfase? = when {
        anterior == null -> nueva
        nueva == null -> anterior
        nueva.errorEn(ahoraCelu) <= anterior.errorEn(ahoraCelu) -> nueva
        else -> anterior
    }

    fun aWatch(tCelu: Long, d: Desfase): Long = tCelu + d.ms
    fun aCelu(tWatch: Long, d: Desfase): Long = tWatch - d.ms
}
