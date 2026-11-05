package ar.classmer.watchlink

import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * Lo que se dicen la app del celu y la del Galaxy Watch por el Wearable Data Layer. Las dos apps
 * tienen el mismo paquete (`ar.classmer`) y la misma firma: es lo que exige el Data Layer.
 *
 * - **Mensajes** (`MessageClient`, JSON en UTF-8): el celu manda [Orden]es y el reloj contesta cada
 *   una con su [EstadoWatch] (con `respuesta_a` = el `n` de la orden). El reloj también lo manda
 *   solo cuando cambia algo (empezó, pausó, terminó) y cada minuto mientras graba.
 * - **Audio** (`ChannelClient`): el celu abre un canal en [Rutas.audio] y el reloj le manda los bytes
 *   del Ogg desde `desde` en tramas ([Tramas]). Si el canal se corta, el celu vuelve a pedir desde
 *   lo que tiene: es el mismo esquema que la subida del celu al server (`PUT /audio?offset=N`).
 *
 * El reloj nunca habla con el server: todo pasa por el celu.
 */
object Rutas {
    const val PREFIJO = "/classmer"

    /** Celu → reloj: "El usuario tocó Grabar con el reloj": el reloj vibra y muestra "tocá para grabar". */
    const val INICIAR = "$PREFIJO/iniciar"

    /** Celu → reloj: el celu se pasó a grabar él (el reloj no había empezado): se saca el aviso. */
    const val CANCELAR = "$PREFIJO/cancelar"
    const val PAUSAR = "$PREFIJO/pausar"
    const val REANUDAR = "$PREFIJO/reanudar"
    const val TERMINAR = "$PREFIJO/terminar"

    /** Celu → reloj: pide el [EstadoWatch] (batería, grabaciones y cuántos bytes tiene cada una). */
    const val CONSULTAR = "$PREFIJO/consultar"

    /**
     * Celu → reloj: el celu tiene la copia completa y verificada. El reloj la conserva igual (hasta
     * [BORRAR]), pero ya puede soltar el primer plano que mantenía para terminar de mandarla.
     */
    const val RECIBIDA = "$PREFIJO/recibida"

    /** Celu → reloj: el celu tiene la copia verificada y el server la aceptó: el reloj borra la suya. */
    const val BORRAR = "$PREFIJO/borrar"

    /** Ida y vuelta para medir el desfase entre relojes ([Sincronizacion]). */
    const val PING = "$PREFIJO/ping"
    const val PONG = "$PREFIJO/pong"

    /** Reloj → celu. */
    const val ESTADO = "$PREFIJO/estado"

    /** Canal que abre el celu: `/classmer/audio/<id>/<desde>`. */
    const val AUDIO = "$PREFIJO/audio"

    fun audio(id: String, desde: Long) = "$AUDIO/$id/$desde"

    /** El id y el `desde` de la ruta de un canal de audio, o `null` si la ruta no es de audio. */
    fun leerAudio(ruta: String): Pair<String, Long>? {
        val partes = ruta.removePrefix("$AUDIO/").split('/')
        if (!ruta.startsWith("$AUDIO/") || partes.size != 2) return null
        val desde = partes[1].toLongOrNull()?.takeIf { it >= 0 } ?: return null
        return partes[0].takeIf { it.isNotBlank() }?.let { it to desde }
    }
}

/** Nombres de capacidad del Data Layer (`res/values/wear.xml`): así cada app sabe si la otra está instalada. */
object Capacidades {
    const val WATCH = "classmer_watch"
    const val CELU = "classmer_celu"
}

/** Sube si cambia algo incompatible: el celu avisa "actualizá la app del reloj" en vez de fallar raro. */
const val VERSION_PROTOCOLO = 1

/** Una orden del celu. [n] empareja la respuesta: el [EstadoWatch] vuelve con `respuesta_a = n`. */
@Serializable
data class Orden(
    val n: Long,
    /** Id de la grabación (el mismo id de la sesión en el celu y en el server). */
    val id: String? = null,
    /** Nombre de la materia, sólo para mostrar en el reloj. */
    val materia: String? = null,
    @SerialName("tipo_clase") val tipoClase: String? = null,
    /** En [Rutas.BORRAR] y [Rutas.RECIBIDA]: el reloj sólo la toma si su copia tiene esta huella. */
    val sha256: String? = null,
)

object EstadoGrabacionWatch {
    const val GRABANDO = "grabando"
    const val PAUSADA = "pausada"
    const val TERMINADA = "terminada"
}

object OrigenGrabacion {
    /** La pidió el celu ("tocá para grabar" en el reloj). */
    const val CELU = "celu"

    /** Se empezó desde la app del reloj: el celu se engancha cuando se entera. */
    const val WATCH = "reloj"
}

/**
 * Una grabación guardada en el reloj. Los tiempos `desde` son `elapsedRealtime` del reloj (ms,
 * monotónico, sigue contando con la pantalla apagada); para pasarlos al celu hace falta el
 * [Desfase].
 */
@Serializable
data class GrabacionWatch(
    val id: String,
    /** [EstadoGrabacionWatch]. */
    val estado: String,
    /** [OrigenGrabacion]. */
    val origen: String,
    /** Hora del reloj al empezar (ISO 8601 con zona). */
    val inicio: String,
    /** Bytes del Ogg que ya están en el disco del reloj (con `fsync`): lo que se puede pedir. */
    val bytes: Long,
    /** Audio grabado (sin pausas) hasta la última vez que arrancó o se pausó el reloj de la grabación. */
    @SerialName("acumulado_ms") val acumuladoMs: Long,
    /** Desde cuándo corre (`elapsedRealtime` del reloj); `null` en pausa o terminada. */
    val desde: Long? = null,
    /** Al terminar: el audio grabado sin pausas, medido con el reloj del Watch. */
    @SerialName("duracion_ms") val duracionMs: Long? = null,
    /** Al terminar: SHA-256 del archivo entero (en hex). */
    val sha256: String? = null,
    /** La app del reloj se cayó grabando: el archivo vale hasta la última página Ogg completa. */
    val interrumpida: Boolean = false,
    /**
     * La grabadora falló en medio de la clase y el reloj siguió grabando en esta grabación nueva:
     * el celu la sube como una sesión nueva de la misma materia (igual que cuando falla la del celu).
     */
    @SerialName("continua_de") val continuaDe: String? = null,
    val materia: String? = null,
    @SerialName("tipo_clase") val tipoClase: String? = null,
) {
    val activa: Boolean get() = estado == EstadoGrabacionWatch.GRABANDO || estado == EstadoGrabacionWatch.PAUSADA
    val terminada: Boolean get() = estado == EstadoGrabacionWatch.TERMINADA

    /** Offset de audio (ms grabados, sin pausas) en el instante [ahoraCelu] del celu. */
    fun offsetEn(ahoraCelu: Long, desfase: Desfase): Long =
        acumuladoMs + (desde?.let { (Sincronizacion.aWatch(ahoraCelu, desfase) - it).coerceAtLeast(0) } ?: 0)
}

/** Lo que el reloj le cuenta al celu. */
@Serializable
data class EstadoWatch(
    val protocolo: Int = VERSION_PROTOCOLO,
    /** El `n` de la [Orden] que se contesta; `null` si el reloj lo mandó solo. */
    @SerialName("respuesta_a") val respuestaA: Long? = null,
    /** `elapsedRealtime` del reloj al mandarlo. */
    val ahora: Long,
    /** Batería en %, o `null` si no se pudo leer. */
    val bateria: Int? = null,
    val cargando: Boolean = false,
    /** Carga en µAh (`BATTERY_PROPERTY_CHARGE_COUNTER`), si el reloj la informa: para medir el consumo. */
    @SerialName("carga_uah") val cargaUah: Long? = null,
    val modelo: String = "",
    @SerialName("version_app") val versionApp: String = "",
    /** Id de un inicio pedido por el celu que espera el toque en el reloj. */
    val esperando: String? = null,
    /** Todas las grabaciones que guarda el reloj (las activas y las que esperan que el celu confirme). */
    val grabaciones: List<GrabacionWatch> = emptyList(),
    /** Un problema que el celu muestra tal cual (por ejemplo, falta el permiso del micrófono). */
    val error: String? = null,
) {
    val activa: GrabacionWatch? get() = grabaciones.firstOrNull { it.activa }
    fun grabacion(id: String): GrabacionWatch? = grabaciones.firstOrNull { it.id == id }
}

/** `t1` es del celu (al mandar el ping); `t2` y `t3`, del reloj (al recibirlo y al contestar). */
@Serializable
data class Ping(val t1: Long)

@Serializable
data class Pong(val t1: Long, val t2: Long, val t3: Long)

/** JSON de los mensajes: tolera campos nuevos, para que una versión nueva de un lado no rompa al otro. */
val JsonWatch = Json {
    ignoreUnknownKeys = true
    encodeDefaults = true
    explicitNulls = false
}

fun <T> codificar(serializador: KSerializer<T>, valor: T): ByteArray =
    JsonWatch.encodeToString(serializador, valor).toByteArray(Charsets.UTF_8)

/** `null` si los bytes no son un mensaje válido de ese tipo. */
fun <T> decodificar(serializador: KSerializer<T>, bytes: ByteArray): T? =
    runCatching { JsonWatch.decodeFromString(serializador, bytes.toString(Charsets.UTF_8)) }.getOrNull()
