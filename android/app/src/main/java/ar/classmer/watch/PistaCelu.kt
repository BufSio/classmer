package ar.classmer.watch

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.grabacion.OggDuracion
import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Evento
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.Pista
import ar.classmer.modelo.SesionLocal
import ar.classmer.util.JsonClassmer
import ar.classmer.util.Tiempo
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.io.File
import kotlin.math.abs

/**
 * `sesiones/<id>/celu.json`: la pista del celu de una clase grabada con Ambos (`audio.celu.ogg`). La
 * escribe el servicio de grabación mientras graba, y lo que cierra la clase si la app se cayó.
 */
@Serializable
data class PistaCelu(
    /** Hora del celu (ms desde 1970) en que empezó a grabar: da un desfase de respaldo si no se pudo medir. */
    @SerialName("inicio_ms") val inicioMs: Long,
    /**
     * El momento del audio del reloj en que empieza el del celu (`desfase_ms` de §15): negativo si el celu
     * empezó antes. `null` mientras no grabaron los dos a la vez.
     */
    @SerialName("desfase_ms") val desfaseMs: Long? = null,
    /** El error posible de [desfaseMs]: el de la medición del desfase entre los relojes. */
    @SerialName("desfase_error_ms") val desfaseErrorMs: Long? = null,
    /**
     * Ya hubo una pausa con los dos grabando: el desfase queda el de antes de la pausa. Las dos pistas se
     * pausan juntas, pero no en el mismo milisegundo.
     */
    @SerialName("desfase_fijo") val desfaseFijo: Boolean = false,
    /** Lo más que se corrió la alineación después de una pausa (diagnóstico). */
    @SerialName("corrimiento_max_ms") val corrimientoMaxMs: Long = 0,
    /** Audio grabado sin las pausas (se guarda cada ~30 s; al cerrarla, el definitivo). */
    @SerialName("duracion_ms") val duracionMs: Long = 0,
    /** La grabadora del celu ya se cerró: el archivo no cambia más. */
    val terminada: Boolean = false,
    /** Se cortó antes que la clase (la app se cayó o falló la grabadora del celu). */
    val interrumpida: Boolean = false,
)

class PistasCelu(private val almacen: AlmacenSesiones) {
    fun archivo(id: String) = File(almacen.dir(id), "celu.json")
    fun audio(id: String) = File(almacen.dir(id), Pista.ARCHIVO_CELU)

    fun leer(id: String): PistaCelu? {
        val f = archivo(id)
        if (!f.isFile) return null
        return runCatching { JsonClassmer.decodeFromString(PistaCelu.serializer(), f.readText(Charsets.UTF_8)) }.getOrNull()
    }

    fun guardar(id: String, p: PistaCelu) {
        synchronized(candado) {
            if (!almacen.dir(id).isDirectory) return
            AlmacenSesiones.escribirAtomico(archivo(id), JsonClassmer.encodeToString(PistaCelu.serializer(), p))
        }
    }

    /** Sólo escribe si cambió algo (el servicio la mide cada 5 s). */
    fun cambiar(id: String, cambio: (PistaCelu) -> PistaCelu): PistaCelu? = synchronized(candado) {
        val actual = leer(id) ?: return null
        cambio(actual).also { if (it != actual) guardar(id, it) }
    }

    /**
     * Una pista que quedó abierta porque la app se cayó grabando: se cierra con lo que tiene el archivo
     * (vale hasta la última página Ogg completa). Si ya estaba cerrada, no la toca. Si hay audio pero se
     * perdió `celu.json`, se arma de nuevo con lo que dice el Ogg (la hora de inicio, aproximada).
     */
    fun cerrarSiQuedoAbierta(id: String): PistaCelu? = synchronized(candado) {
        val audio = audio(id)
        val actual = leer(id) ?: run {
            if (audio.length() == 0L) return null
            val duracion = OggDuracion.leerMs(audio) ?: 0L
            return PistaCelu(inicioMs = audio.lastModified() - duracion, duracionMs = duracion, terminada = true, interrumpida = true)
                .also { guardar(id, it) }
        }
        cambiar(id) { p -> if (p.terminada) p else p.copy(terminada = true, interrumpida = true, duracionMs = OggDuracion.leerMs(audio) ?: p.duracionMs) }
            ?: actual
    }

    private companion object {
        val candado = Any()
    }
}

/**
 * Las cuentas de Ambos, sin Android: el desfase entre las dos pistas, el minuto de cada evento en el
 * audio que va al server y qué pasa con la clase si el reloj no grabó.
 */
object Ambos {
    /**
     * Con lo grabado por el reloj y por el celu en el mismo instante (y el error posible del desfase entre
     * los relojes): el desfase de la pista. Antes de la primera pausa se queda con la medición de menos
     * error; después sólo anota cuánto se corrió. Un salto más grande que los errores de las dos mediciones
     * no es una medición mejor sino un corrimiento (por ejemplo, el reloj pausó lejos del celu y el celu
     * siguió grabando): tampoco reemplaza al desfase. La excepción es una mucho más precisa: la primera sale
     * de una estimación sin ping, cuyo error real puede ser más que el declarado (la demora del Bluetooth).
     */
    fun medir(p: PistaCelu, grabadoReloj: Long, errorMs: Long, grabadoCelu: Long): PistaCelu {
        val medido = grabadoReloj - grabadoCelu
        val actual = p.desfaseMs ?: return p.copy(desfaseMs = medido, desfaseErrorMs = errorMs)
        val errorActual = p.desfaseErrorMs ?: Long.MAX_VALUE
        val salto = abs(medido - actual)
        val esLaMisma = salto <= (p.desfaseErrorMs ?: 0L) + errorMs + TOLERANCIA_SALTO_MS
        // Sólo contra una estimación sin ping, y con un salto del orden de la demora del Bluetooth (no una
        // pausa que el celu no vio, que son minutos).
        val muchoMasPrecisa = errorMs * FACTOR_MUCHO_MAS_PRECISA < errorActual && salto <= MAX_SALTO_ESTIMACION_MS
        return when {
            !p.desfaseFijo && (esLaMisma || muchoMasPrecisa) ->
                if (errorMs < errorActual) p.copy(desfaseMs = medido, desfaseErrorMs = errorMs) else p
            salto > p.corrimientoMaxMs -> p.copy(corrimientoMaxMs = salto)
            else -> p
        }
    }

    /** Una medición con esta fracción del error de la anterior la reemplaza aunque salte (ver [medir]). */
    private const val FACTOR_MUCHO_MAS_PRECISA = 4

    /** Lo más que puede estar corrida una estimación sin ping (la demora de un mensaje por Bluetooth). */
    private const val MAX_SALTO_ESTIMACION_MS = 5_000L

    /** Lo que puede moverse una medición del desfase sin que sea un corrimiento, además de los errores. */
    const val TOLERANCIA_SALTO_MS = 250L

    /**
     * El desfase que va al server: el medido o, si nunca grabaron los dos a la vez con el celu siguiéndolos
     * (por ejemplo, la app se cayó antes de que empezara el reloj), el de las horas de los dos equipos al
     * empezar, que tienen la hora de la red (error de alrededor de un segundo).
     */
    fun desfaseFinal(p: PistaCelu, inicioRelojMs: Long?): Long? =
        p.desfaseMs ?: inicioRelojMs?.takeIf { it > 0 }?.let { p.inicioMs - it }

    /** La hora (ms desde 1970) en que empezó el audio del reloj, según lo último que informó. */
    fun inicioReloj(almacen: AlmacenSesiones, s: SesionLocal): Long? =
        (RegistrosWatch(almacen).leer(s.id).grabacion?.inicio ?: s.inicio.takeIf { s.pistaCelu })
            ?.let { Tiempo.epochMs(it) }?.takeIf { it > 0 }

    /**
     * El desfase de una clase de Ambos. Mientras no está cerrada, sólo el medido: los eventos que lo
     * necesitan esperan a que se mida, que es a los pocos segundos de que graben los dos. Cerrada, también
     * el de las horas ([desfaseFinal]).
     */
    fun desfase(almacen: AlmacenSesiones, s: SesionLocal): Long? {
        if (!s.pistaCelu && !s.principalEsCelu) return null
        val p = PistasCelu(almacen).leer(s.id) ?: return null
        return if (s.terminada) desfaseFinal(p, inicioReloj(almacen, s)) else p.desfaseMs
    }

    /**
     * El evento como va al server, con el minuto en el audio principal. En una de Ambos, los minutos
     * contados con la pista del celu ([Evento.pista]) se pasan al audio del reloj sumando el desfase; si la
     * clase quedó como del teléfono, al revés. Devuelve `null` si hace falta el desfase y todavía no se
     * sabe (se sube más tarde); con la clase cerrada, va como está.
     */
    fun paraSubir(e: Evento, s: SesionLocal, desfase: Long?): Evento? {
        val minuto = e.offsetMs ?: return e.copy(pista = null)
        val corrimiento = corrimiento(e.pista, s, desfase) ?: return null
        return e.copy(offsetMs = (minuto + corrimiento).coerceAtLeast(0), pista = null)
    }

    /** Lo mismo para el minuto de un adjunto tomado grabando. */
    fun adjuntoParaSubir(a: AdjuntoLocal, s: SesionLocal, desfase: Long?): AdjuntoLocal? {
        val minuto = a.offsetMs ?: return a.copy(pista = null)
        val corrimiento = corrimiento(a.pista, s, desfase) ?: return null
        return a.copy(offsetMs = (minuto + corrimiento).coerceAtLeast(0), pista = null)
    }

    /**
     * Cuánto hay que correr un minuto contado en [pista] para que quede en el audio principal de [s], o
     * `null` si hace falta el desfase y todavía no se sabe (con la clase cerrada, 0: va como está).
     */
    private fun corrimiento(pista: String?, s: SesionLocal, desfase: Long?): Long? {
        val c = when {
            s.pistaCelu && pista == Pista.CELU -> desfase
            s.principalEsCelu && pista == null -> desfase?.let { -it }
            else -> 0L
        }
        return c ?: if (s.terminada) 0L else null
    }

    /**
     * La clase queda como si se hubiera grabado con el teléfono: el reloj no llegó a grabar (o no llegó
     * nada que sirva) y el audio principal pasa a ser la pista del celu.
     */
    fun comoTelefono(s: SesionLocal, dispositivo: String, pista: PistaCelu?): SesionLocal = s.copy(
        fuenteAudio = FuenteAudio.TELEFONO,
        dispositivo = dispositivo,
        archivoAudio = Pista.ARCHIVO_CELU,
        pistaCelu = false,
        // La clase empieza con la pista del celu (si el reloj llegó a empezar, la sesión tenía su hora).
        inicio = pista?.let { Tiempo.iso(it.inicioMs) } ?: s.inicio,
        duracionMs = pista?.duracionMs?.takeIf { it > 0 } ?: s.duracionMs,
    )

    /** El audio del reloj que llegó no sirve como principal: nada, o ni una página Ogg con audio. */
    fun relojSinAudio(almacen: AlmacenSesiones, id: String): Boolean {
        val audio = almacen.archivoAudio(id)
        return audio.length() == 0L || (OggDuracion.leerMs(audio) ?: 0L) <= 0L
    }

    /**
     * Una de Ambos cuyo audio del reloj no sirve (aunque haya llegado entero y verificado: el reloj se cortó
     * apenas empezó) y la pista del celu sí tiene audio: se cierra con [cerrarSinReloj], como del teléfono.
     */
    fun relojVacio(almacen: AlmacenSesiones, s: SesionLocal): Boolean =
        s.pistaCelu && relojSinAudio(almacen, s.id) && PistasCelu(almacen).audio(s.id).length() > 0

    /**
     * Al arrancar la app: las clases de Ambos que quedaron "grabando" con la pista del celu abierta (la app se
     * cayó) se cierran con lo que tiene su Ogg. Devuelve las que no tenían nada del reloj (ni lo vio grabar
     * ni llegó audio): ahí el celu era lo único que grababa, así que hay que avisar que se cortó. Las cierra
     * del todo SincronizacionWatch (o "Cerrar con lo recibido"), cuando se sabe si el reloj la tiene.
     */
    fun cerrarPistasCortadas(almacen: AlmacenSesiones, activa: String?): List<Pair<SesionLocal, Long>> {
        val pistas = PistasCelu(almacen)
        val registros = RegistrosWatch(almacen)
        val soloCelu = mutableListOf<Pair<SesionLocal, Long>>()
        for (s in almacen.listar()) {
            if (!s.delReloj || !s.pistaCelu || s.estadoLocal != EstadoLocal.GRABANDO || s.id == activa) continue
            val p = pistas.leer(s.id)
            val abierta = (p != null && !p.terminada) || (p == null && pistas.audio(s.id).length() > 0)
            if (!abierta) continue
            val cerrada = runCatching { pistas.cerrarSiQuedoAbierta(s.id) }.getOrNull() ?: continue
            // El reloj nunca grabó, o su grabación ya había terminado (se cortó y seguía sólo el celu).
            val r = registros.leer(s.id)
            if ((!r.vistaEnWatch && relojSinAudio(almacen, s.id)) || r.grabacion?.terminada == true) soloCelu += s to cerrada.duracionMs
        }
        return soloCelu
    }

    /**
     * Una de Ambos que figura "grabando" sin nadie que la siga, en la que el reloj nunca se vio grabar y el
     * celu sí grabó: se ofrece "Cerrar con lo recibido" (queda como del teléfono). Si el reloj la está
     * grabando, el celu lo ve en cuanto lo encuentra y la sigue solo.
     */
    fun cerrableSinReloj(almacen: AlmacenSesiones, s: SesionLocal): Boolean =
        s.delReloj && s.pistaCelu && s.estadoLocal == EstadoLocal.GRABANDO &&
            !RegistrosWatch(almacen).leer(s.id).vistaEnWatch && relojSinAudio(almacen, s.id) &&
            PistasCelu(almacen).audio(s.id).length() > 0

    /**
     * Cierra una clase de Ambos a la que el reloj no le va a mandar más audio (nunca empezó, o se cerró con
     * lo recibido). Si del reloj llegó algo que sirve, queda como de Ambos con eso; si no, y la pista del
     * celu tiene audio, queda como del teléfono. Devuelve la sesión cerrada, o `null` si no es de Ambos o
     * su pista del celu está vacía (la cierra quien llama, como siempre).
     */
    fun cerrarSinReloj(almacen: AlmacenSesiones, s: SesionLocal, dispositivoCelu: String, interrumpida: Boolean): SesionLocal? {
        if (!s.pistaCelu) return null
        val pistas = PistasCelu(almacen)
        val pista = pistas.cerrarSiQuedoAbierta(s.id)
        if (pistas.audio(s.id).length() == 0L) return null
        val cerrada = if (relojSinAudio(almacen, s.id)) {
            comoTelefono(s, dispositivoCelu, pista).copy(interrumpida = s.interrumpida || pista?.interrumpida == true)
        } else {
            val duracion = OggDuracion.leerMs(almacen.archivoAudio(s.id)) ?: s.duracionMs
            s.copy(duracionMs = duracion, interrumpida = s.interrumpida || interrumpida)
        }
        val final = cerrada.copy(estadoLocal = EstadoLocal.TERMINADA, fin = s.fin ?: Tiempo.ahoraIso())
        almacen.guardarSesion(final)
        return final
    }
}
