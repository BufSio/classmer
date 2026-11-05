package ar.classmer.modelo

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

// ---- Lo que manda el server (contrato de la API v1, ARCHITECTURE.md §5.1) ----

@Serializable
data class ReglaCalendario(
    val calendario: String? = null,
    @SerialName("titulo_regex") val tituloRegex: String? = null,
)

@Serializable
data class Materia(
    val id: String,
    val nombre: String,
    val calendario: ReglaCalendario? = null,
    @SerialName("tipos_clase") val tiposClase: List<String> = TIPOS_CLASE_POR_DEFECTO,
    /** Número de la próxima clase de esta materia (`null` si el server todavía no lo informa). */
    @SerialName("proximo_numero") val proximoNumero: Int? = null,
)

val TIPOS_CLASE_POR_DEFECTO = listOf("teorica", "practica")

/** Respuesta de `GET /api/sesiones/{id}` (y cada elemento de `GET /api/sesiones`). */
@Serializable
data class EstadoSesionServidor(
    val id: String,
    @SerialName("materia_id") val materiaId: String? = null,
    val estado: String? = null,
    val etapa: String? = null,
    val progreso: Double? = null,
    @SerialName("bytes_recibidos") val bytesRecibidos: Long? = null,
    val pdf: String? = null,
    val error: JsonElement? = null,
    // Campos opcionales que el server puede agregar en el listado.
    val inicio: String? = null,
    @SerialName("tipo_clase") val tipoClase: String? = null,
    @SerialName("duracion_ms") val duracionMs: Long? = null,
    @SerialName("numero_clase") val numeroClase: Int? = null,
    val titulo: String? = null,
    /** La clase se importó (no la grabó un celu): sólo para mostrarlo. Un server viejo no lo manda. */
    val importada: Boolean = false,
    // Adjuntos (PDF, imágenes): un server sin adjuntos no los manda.
    val adjuntos: List<AdjuntoServidor> = emptyList(),
    @SerialName("adjuntos_sin_usar") val adjuntosSinUsar: Int = 0,
    /**
     * En `esperando_usuario`, qué espera el server: [Espera.MATERIA] (la de siempre) o [Espera.REVISION]
     * (las dudas del transcript, que se revisan en el panel). Un server viejo no lo manda: es la materia.
     */
    val espera: String? = null,
    /** Con [Espera.REVISION]: cuántas dudas del transcript quedan para revisar. */
    val dudas: Int? = null,
) {
    val textoError: String?
        get() = when (val e = error) {
            null, JsonNull -> null
            is JsonPrimitive -> e.contentOrNull
            else -> e.toString()
        }
}

/** Lo que espera una sesión en `esperando_usuario` ([EstadoSesionServidor.espera]). */
object Espera {
    const val MATERIA = "materia"
    const val REVISION = "revision"
}

// ---- Lo que se guarda en el celu ----

object OrigenMateria {
    const val CALENDARIO = "calendario"
    const val MANUAL = "manual"
    const val AUTO = "auto"
}

object EstadoLocal {
    const val GRABANDO = "grabando"
    const val TERMINADA = "terminada"

    /** Grabó el reloj y la clase ya terminó, pero falta que llegue (y se verifique) todo su audio. */
    const val RECIBIENDO = "recibiendo"

    /**
     * Clase importada (un audio que no grabó el celu): se están copiando al celu el audio y las fotos
     * elegidos (`importacion.json`). No va al server hasta que la copia termina y pasa a [TERMINADA].
     */
    const val IMPORTANDO = "importando"
}

/** Quién graba el audio de la clase. */
object FuenteAudio {
    const val TELEFONO = "telefono"

    /** El Galaxy Watch (cerca del profe): el audio llega por Bluetooth al mismo `audio.ogg`. */
    const val RELOJ = "reloj"

    /**
     * Los dos: el reloj es el principal y el celu graba también su propio audio (`audio.celu.ogg`), de
     * respaldo y como segunda versión. Es una elección de Inicio: la sesión queda como del [RELOJ] con
     * [SesionLocal.pistaCelu].
     */
    const val AMBOS = "ambos"
}

/** Las pistas de audio de una sesión, como el parámetro `pista` de `/audio` (§15). */
object Pista {
    const val CELU = "celu"

    /** El archivo de la pista del celu en la carpeta de la sesión. */
    const val ARCHIVO_CELU = "audio.celu.ogg"
}

/** `sesion.json`: los datos de la clase. También es el cuerpo de `POST /api/sesiones`. */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class SesionLocal(
    val id: String,
    @SerialName("materia_id") val materiaId: String?,
    @SerialName("origen_materia") val origenMateria: String,
    @SerialName("tipo_clase") val tipoClase: String,
    val inicio: String,
    val dispositivo: String,
    /** Número de clase con el que se grabó (el `proximo_numero` de la materia); `null` en Auto. */
    @SerialName("numero_clase") val numeroClase: Int? = null,
    // De acá para abajo es sólo local (no va al server).
    @SerialName("estado_local") val estadoLocal: String = EstadoLocal.GRABANDO,
    @SerialName("duracion_ms") val duracionMs: Long = 0,
    val fin: String? = null,
    @SerialName("nombre_materia") val nombreMateria: String? = null,
    @SerialName("evento_calendario") val eventoCalendario: String? = null,
    /** La app se cerró o se cayó mientras grababa y la sesión se cerró al volver a abrirla. */
    val interrumpida: Boolean = false,
    /** La grabadora falló en medio de la clase y se siguió grabando en esta sesión nueva. */
    @SerialName("continua_de") val continuaDe: String? = null,
    /** Fin del evento del calendario del que salió la materia (ms desde 1970), para el corte automático. */
    @SerialName("fin_evento_ms") val finEventoMs: Long? = null,
    /** Cuándo preguntar "¿Sigue la clase?" (ms desde 1970). `null` = nunca (sesiones de v0.1). */
    @SerialName("preguntar_corte_ms") val preguntarCorteMs: Long? = null,
    /** Nadie respondió "¿Sigue la clase?" y la grabación terminó sola. */
    @SerialName("corte_automatico") val corteAutomatico: Boolean = false,
    /** [FuenteAudio]: con el reloj, el estado de la transferencia está en `watch.json`. */
    @SerialName("fuente_audio") val fuenteAudio: String = FuenteAudio.TELEFONO,
    // Clase importada (el audio que mandó un amigo, por ejemplo). Estos campos no van en el
    // sesion.json de una clase grabada.
    /**
     * No la grabó el celu: el audio (y las fotos) se copiaron de archivos elegidos. Nada de la
     * grabación la toca: ni la recuperación, ni el corte automático, ni el reloj.
     */
    @EncodeDefault(EncodeDefault.Mode.NEVER) val importada: Boolean = false,
    /** El nombre original del audio importado (va al server como `audio_nombre`). */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("audio_nombre") val audioNombre: String? = null,
    /** El tamaño del audio importado (va al server como `audio_bytes`): el de la copia, cuando termina. */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("audio_bytes") val audioBytes: Long? = null,
    /**
     * El archivo de audio de la carpeta si no es `audio.ogg`: en una importada, `audio.<ext>`; en una de
     * Ambos que quedó como del teléfono (el reloj nunca grabó), `audio.celu.ogg`.
     */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("archivo_audio") val archivoAudio: String? = null,
    /**
     * Ambos: además del audio del reloj (el principal), el celu graba el suyo en `audio.celu.ogg`
     * (`celu.json`) y se sube como segunda pista. No va en las demás.
     */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("pista_celu") val pistaCelu: Boolean = false,
) {
    val terminada: Boolean get() = estadoLocal == EstadoLocal.TERMINADA

    /** La graba (o la grabó) el reloj. Una importada nunca, aunque algo le cambie la fuente. */
    val delReloj: Boolean get() = fuenteAudio == FuenteAudio.RELOJ && !importada

    /** Se grabó con Ambos y el reloj no llegó a grabar: su audio principal es la pista del celu. */
    val principalEsCelu: Boolean get() = archivoAudio == Pista.ARCHIVO_CELU
}

/** Cuerpo de `POST /api/sesiones`. */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class CrearSesion(
    val id: String,
    @SerialName("materia_id") val materiaId: String?,
    @SerialName("origen_materia") val origenMateria: String,
    @SerialName("tipo_clase") val tipoClase: String,
    val inicio: String,
    val dispositivo: String,
    /** Opcional: si no va, el server usa el `proximo_numero` de la materia. */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("numero_clase") val numeroClase: Int? = null,
    // Sólo en una clase importada: en una grabada no van y el JSON queda como siempre. Un server
    // viejo los ignora.
    @EncodeDefault(EncodeDefault.Mode.NEVER) val importada: Boolean = false,
    /** El nombre original del audio: su extensión le dice al server cómo guardarlo. */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("audio_nombre") val audioNombre: String? = null,
    /** El tamaño total del audio: con más de 2 GiB, el server responde 413 antes de recibir nada. */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("audio_bytes") val audioBytes: Long? = null,
)

fun SesionLocal.paraCrear() = CrearSesion(
    id, materiaId, origenMateria, tipoClase, inicio, dispositivo, numeroClase,
    importada = importada,
    audioNombre = audioNombre.takeIf { importada },
    audioBytes = audioBytes.takeIf { importada },
)

/** Cuerpo de `POST /api/sesiones/{id}/materia`. */
@Serializable
data class AsignarMateria(@SerialName("materia_id") val materiaId: String)

/** Cuerpo de `PATCH /api/materias/{id}`. */
@Serializable
data class CambiarNumero(@SerialName("proximo_numero") val proximoNumero: Int)

object TipoEvento {
    const val FOTO = "foto"
    const val NOTA = "nota"
    const val MARCA = "marca"
    const val PAUSA = "pausa"
    const val REANUDAR = "reanudar"
}

object TipoMarca {
    const val IMPORTANTE = "importante"
    const val NO_ENTENDI = "no_entendi"
}

/** Una línea de `eventos.jsonl`. */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class Evento(
    val id: String,
    val tipo: String,
    /**
     * El minuto del audio. `null` (y la línea va sin `offset_ms`) sólo en las fotos de una clase
     * importada: el server las ubica por la hora de su EXIF (y si no la tienen, quedan "sin hora").
     */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("offset_ms") val offsetMs: Long? = null,
    val creado: String,
    val texto: String? = null,
    val marca: String? = null,
    /** Ruta relativa a la carpeta de la sesión (fotos), por ejemplo `fotos/<id>.jpg`. */
    val archivo: String? = null,
    /**
     * Con Ambos, [Pista.CELU] si el minuto se contó con el reloj de la pista del celu (antes de que
     * empezara el reloj, o después de que se cortó) y no con el del audio del reloj. La subida lo pasa
     * al audio principal con el desfase. No va en las demás.
     */
    @EncodeDefault(EncodeDefault.Mode.NEVER) val pista: String? = null,
)

/** `fotos/<id>.pendiente.json`: una foto pedida a la cámara que todavía no tiene evento. */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class FotoPendiente(
    val id: String,
    @SerialName("offset_ms") val offsetMs: Long,
    val creado: String,
    /** La [Evento.pista] de su minuto (Ambos). */
    @EncodeDefault(EncodeDefault.Mode.NEVER) val pista: String? = null,
)

/** `subida.json`: hasta dónde confirmó el server. Lo escribe sólo el trabajo de subida. */
@Serializable
data class EstadoSubida(
    @SerialName("sesion_creada") val sesionCreada: Boolean = false,
    @SerialName("bytes_confirmados") val bytesConfirmados: Long = 0,
    @SerialName("eventos_subidos") val eventosSubidos: Set<String> = emptySet(),
    /** El server aceptó `POST /finalizar`: ya tiene todo. */
    val finalizada: Boolean = false,
    val sha256: String? = null,
    @SerialName("sha256_bytes") val sha256Bytes: Long = -1,
    @SerialName("ultimo_error") val ultimoError: String? = null,
    @SerialName("ultimo_intento") val ultimoIntento: String? = null,
    @SerialName("ultimo_ok") val ultimoOk: String? = null,
    /** El server rechazó la materia (400/422): la sesión se manda como Auto (IA). */
    @SerialName("crear_como_auto") val crearComoAuto: Boolean = false,
    /** El server rechazó la sesión con `numero_clase` (400/422): se manda sin número (lo pone el server). */
    @SerialName("crear_sin_numero") val crearSinNumero: Boolean = false,
    /** Eventos que el server rechazó con un error permanente (por ejemplo, 422): no cuentan ni se reenvían. */
    @SerialName("eventos_rechazados") val eventosRechazados: Set<String> = emptySet(),
    /** El server rechazó la clase con un error que no se arregla reintentando: se espera "Reintentar". */
    @SerialName("error_permanente") val errorPermanente: String? = null,
    // La pista del celu (Ambos), con el mismo esquema que el audio principal.
    /**
     * La URL del server que demostró que separa las pistas (un `GET /audio?pista=celu` contestó otra cosa
     * que el principal). Hasta entonces, y con otro server, la pista no se sube: un server viejo ignora
     * `pista` y la pegaría al principal.
     */
    @SerialName("celu_soportada_en") val celuSoportadaEn: String? = null,
    @SerialName("celu_bytes_confirmados") val celuBytesConfirmados: Long = 0,
    @SerialName("celu_sha256") val celuSha256: String? = null,
    @SerialName("celu_sha256_bytes") val celuSha256Bytes: Long = -1,
    /**
     * Se dejó de mandar la pista del celu porque el server la rechazó de una forma que no se arregla
     * reintentando: la clase se finaliza sin ella. Una falla de la pista nunca traba la clase.
     */
    @SerialName("celu_rechazada") val celuRechazada: String? = null,
    /** Se finalizó sin la pista del celu (server viejo, o la rechazó): quedó sólo en el celu. Se anota con el `/finalizar` aceptado. */
    @SerialName("celu_sin_subir") val celuSinSubir: Boolean = false,
)

/** Cuerpo de `POST /api/sesiones/{id}/finalizar`. */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class Finalizar(
    val sha256: String,
    @SerialName("bytes_totales") val bytesTotales: Long,
    /** `null` en una importada cuya duración el celu no pudo medir: el server usa la que mide él. */
    @SerialName("duracion_ms") val duracionMs: Long?,
    @SerialName("eventos_totales") val eventosTotales: Int,
    /** Ambos: la pista del celu. Sin ella el cuerpo queda como siempre. */
    @EncodeDefault(EncodeDefault.Mode.NEVER) val celu: FinalizarCelu? = null,
)

/** El bloque `celu` de `/finalizar` (§15). */
@Serializable
data class FinalizarCelu(
    val sha256: String,
    @SerialName("bytes_totales") val bytesTotales: Long,
    @SerialName("duracion_ms") val duracionMs: Long,
    /** El momento del audio del reloj en que empieza el del celu: negativo si el celu empezó antes. */
    @SerialName("desfase_ms") val desfaseMs: Long,
)
