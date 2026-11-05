package ar.classmer.watch

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.util.JsonClassmer
import ar.classmer.watchlink.Desfase
import ar.classmer.watchlink.GrabacionWatch
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.io.File

/** Una lectura de la batería del reloj (para saber cuánto gastó cada clase). */
@Serializable
data class LecturaBateria(
    val porcentaje: Int? = null,
    /** Contador de carga en µAh, si el reloj lo informa (más fino que el porcentaje). */
    val uah: Long? = null,
    /** Hora del celu (ms desde 1970). */
    val en: Long,
)

/**
 * `sesiones/<id>/watch.json`: lo que el celu sabe de la grabación del reloj de esa clase. Lo escribe
 * sólo [SincronizacionWatch] (y [SesionWatch] el desfase), así como `subida.json` lo escribe sólo la
 * subida.
 */
@Serializable
data class RegistroWatch(
    /** Nodo del Data Layer del reloj que la grabó. */
    val nodo: String? = null,
    val modelo: String? = null,
    /** La última vez que el reloj la informó (en la lista de su estado). */
    @SerialName("vista_en_watch") val vistaEnWatch: Boolean = false,
    /** Lo último que informó el reloj de esta grabación. */
    val grabacion: GrabacionWatch? = null,
    /** El desfase entre relojes con el que se calcularon los offsets de las fotos y marcas. */
    val desfase: Desfase? = null,
    /**
     * Hasta dónde iba la clase (offset) cuando se terminó en el celu. Si el reloj estaba lejos y siguió
     * grabando, lo que pase de acá es de después de la clase (hoy se sube igual: ver ARCHITECTURE).
     */
    @SerialName("corte_ms") val corteMs: Long? = null,
    /** El celu tiene el audio entero y su huella coincide con la del reloj. */
    val verificada: Boolean = false,
    @SerialName("sha256") val sha256: String? = null,
    /** Se le avisó al reloj que el celu la tiene ([ar.classmer.watchlink.Rutas.RECIBIDA]). */
    @SerialName("recibida_avisada") val recibidaAvisada: Boolean = false,
    /** El reloj ya borró su copia. */
    @SerialName("borrada_en_watch") val borradaEnWatch: Boolean = false,
    /** Bajadas enteras del audio cuya huella tampoco coincidió con la del reloj. */
    val rebajadas: Int = 0,
    /**
     * El usuario la cerró a mano con lo recibido (el reloj no aparecía): ya fue al server así, no se le agrega
     * nada más y el reloj conserva su copia entera (se puede rescatar a mano).
     */
    @SerialName("cerrada_sin_watch") val cerradaSinWatch: Boolean = false,
    /** La batería del reloj la primera y la última vez que se lo vio grabando esta clase. */
    @SerialName("bateria_inicio") val bateriaInicio: LecturaBateria? = null,
    @SerialName("bateria_ultima") val bateriaUltima: LecturaBateria? = null,
    /** Hora del celu (ms desde 1970) de la última vez que llegaron bytes del reloj. */
    @SerialName("ultima_recepcion") val ultimaRecepcion: Long? = null,
    /** Hora del celu (ms desde 1970) del último estado del reloj que la incluyó. */
    @SerialName("ultimo_contacto") val ultimoContacto: Long? = null,
)

class RegistrosWatch(private val almacen: AlmacenSesiones) {
    fun archivo(id: String) = File(almacen.dir(id), "watch.json")

    fun leer(id: String): RegistroWatch {
        val f = archivo(id)
        if (!f.isFile) return RegistroWatch()
        return runCatching { JsonClassmer.decodeFromString(RegistroWatch.serializer(), f.readText(Charsets.UTF_8)) }.getOrNull() ?: RegistroWatch()
    }

    fun guardar(id: String, r: RegistroWatch) {
        synchronized(candado) {
            if (!almacen.dir(id).isDirectory) return
            AlmacenSesiones.escribirAtomico(archivo(id), JsonClassmer.encodeToString(RegistroWatch.serializer(), r))
        }
    }

    fun cambiar(id: String, cambio: (RegistroWatch) -> RegistroWatch): RegistroWatch = synchronized(candado) {
        val nuevo = cambio(leer(id))
        guardar(id, nuevo)
        nuevo
    }

    private companion object {
        /** Uno para todo el proceso: la sincronización y la sesión en curso escriben el mismo archivo. */
        val candado = Any()
    }
}
