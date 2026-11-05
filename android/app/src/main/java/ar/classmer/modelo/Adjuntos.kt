package ar.classmer.modelo

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

// ---- Adjuntos de una clase: PDF de slides o notas del profe, o imágenes ----

/**
 * Un adjunto como lo informa el server: en `adjuntos` del objeto de sesión y en la respuesta de
 * `POST /api/sesiones/{id}/adjuntos`. Todo es opcional salvo el id.
 */
@Serializable
data class AdjuntoServidor(
    val id: String,
    /** El nombre que le puso el server (ASCII, con la extensión del tipo detectado). */
    val nombre: String? = null,
    @SerialName("nombre_original") val nombreOriginal: String? = null,
    /** `pdf`, `jpeg`, `png`, `webp` o `heic`. */
    val tipo: String? = null,
    val bytes: Long? = null,
    @SerialName("offset_ms") val offsetMs: Long? = null,
    val creado: String? = null,
    /** [EstadoAdjunto]: si el apunte vigente lo usó. */
    val estado: String? = null,
)

/** Los valores de `estado` de un adjunto en el server. */
object EstadoAdjunto {
    /** El próximo apunte lo va a usar (todavía no hay apunte, o hay uno en cola o corriendo que lo incluye). */
    const val PENDIENTE = "pendiente"

    /** Lo usó el apunte vigente. */
    const val USADO = "usado"

    /** El apunte vigente no lo usa (llegó después): se arregla regenerando. */
    const val SIN_USAR = "sin_usar"
}

/**
 * `adjuntos/<sesion>/<id>.json`: un adjunto copiado al celu. El archivo está al lado, en [archivo]
 * (`<id>.<ext>`). Se escribe una sola vez, después de que la copia quedó entera.
 */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class AdjuntoLocal(
    val id: String,
    @SerialName("sesion_id") val sesionId: String,
    @SerialName("nombre_original") val nombreOriginal: String,
    /** El tipo detectado por el contenido (no el que dice el selector). */
    val mime: String,
    val bytes: Long,
    /** Minuto del audio si se adjuntó grabando; si no, no va. */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("offset_ms") val offsetMs: Long? = null,
    val creado: String,
    val archivo: String,
    /** Con Ambos, la pista en que se contó [offsetMs] (como [Evento.pista]): la subida lo pasa al audio principal. */
    @EncodeDefault(EncodeDefault.Mode.NEVER) val pista: String? = null,
) {
    val esPdf: Boolean get() = mime == "application/pdf"
}

/**
 * `adjuntos/<sesion>/<id>.copiando`: un archivo elegido con el selector que todavía se está copiando.
 * Se anota antes de copiar: si Android cierra la app en el medio, la copia se retoma (o se avisa que
 * hay que volver a adjuntarlo). Al terminar se borra. El [id] es el que va a tener el adjunto.
 */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class CopiaPendiente(
    val id: String,
    @SerialName("sesion_id") val sesionId: String,
    /** La URI que dio el selector (con permiso persistente, si Android lo dio). */
    val uri: String,
    /** Nombre y tamaño que informó el selector (cualquiera de los dos puede faltar). */
    val nombre: String? = null,
    val tamanio: Long? = null,
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("offset_ms") val offsetMs: Long? = null,
    val creado: String,
    /** La pista de [offsetMs] (Ambos). */
    @EncodeDefault(EncodeDefault.Mode.NEVER) val pista: String? = null,
)

/** Cómo va la subida de un adjunto. */
@Serializable
data class SubidaAdjunto(
    /** El server lo confirmó (200/201): no se vuelve a subir. */
    val subido: Boolean = false,
    /** Lo que devolvió el server al subirlo (nombre y estado en ese momento). */
    val servidor: AdjuntoServidor? = null,
    /** El server lo rechazó (409, 413, 415, 422…): no se reintenta solo, sólo con "Reintentar". */
    val rechazo: String? = null,
    /** Fallas seguidas de la red o del server al mandar el archivo: definen la espera antes de volver a mandarlo. */
    val fallos: Int = 0,
    /** Antes de este momento (ms desde 1970) no se vuelve a mandar solo: la espera creciente tras una falla. */
    @SerialName("esperar_hasta_ms") val esperarHastaMs: Long? = null,
    @SerialName("ultimo_error") val ultimoError: String? = null,
    @SerialName("ultimo_intento") val ultimoIntento: String? = null,
)

/** `adjuntos/<sesion>/subida.json`: la subida de los adjuntos de una clase. Lo escribe sólo la subida. */
@Serializable
data class EstadoSubidaAdjuntos(
    val adjuntos: Map<String, SubidaAdjunto> = emptyMap(),
    /** El último objeto de sesión que devolvió el server al subir un adjunto. */
    val sesion: EstadoSesionServidor? = null,
    /**
     * Respuestas 404 "sesión no encontrada" seguidas: es de la clase, no de un adjunto. Pasado
     * [MAX_NO_ENCONTRADA], la clase se da por perdida (por ejemplo, se borró desde el panel): no se le
     * pide más reintentos a WorkManager y sólo se vuelve a preguntar cada tanto o con "Reintentar".
     */
    @SerialName("no_encontrada") val noEncontrada: Int = 0,
    /** Antes de este momento (ms desde 1970) no se le vuelve a preguntar al server por la clase. */
    @SerialName("esperar_hasta_ms") val esperarHastaMs: Long? = null,
    /** El último problema de la clase en sí (por ejemplo, "el server no encuentra esta clase"). */
    @SerialName("ultimo_error") val ultimoError: String? = null,
) {
    fun de(id: String): SubidaAdjunto = adjuntos[id] ?: SubidaAdjunto()

    fun con(id: String, subida: SubidaAdjunto) = copy(adjuntos = adjuntos + (id to subida))

    /** El server no encuentra la clase hace rato: se dejó de insistir. */
    val claseNoEncontrada: Boolean get() = noEncontrada > MAX_NO_ENCONTRADA

    companion object {
        /** Respuestas 404 "sesión no encontrada" seguidas con las que se sigue pidiendo reintento. */
        const val MAX_NO_ENCONTRADA = 5
    }
}

/**
 * `adjuntos/soporte.json`: si el server acepta adjuntos. Es de todo el server, no de una clase: así,
 * con un server sin actualizar, todos los adjuntos lo muestran y no se le pregunta en cada pasada.
 * Lo escribe sólo la subida.
 */
@Serializable
data class SoporteAdjuntos(
    /** El server no tiene la ruta de adjuntos (404 "Not Found" o 405): todavía no se actualizó. */
    @SerialName("sin_soporte") val sinSoporte: Boolean = false,
    /** La URL del server que contestó eso: si se cambia en Ajustes, se vuelve a probar enseguida. */
    val servidor: String? = null,
    /** La última vez que se le preguntó (ms desde 1970). */
    @SerialName("ultimo_sondeo_ms") val ultimoSondeoMs: Long? = null,
)
