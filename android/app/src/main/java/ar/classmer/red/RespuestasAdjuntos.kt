package ar.classmer.red

import ar.classmer.modelo.AdjuntoServidor
import ar.classmer.modelo.EstadoSesionServidor

/** Respuesta de `POST /api/sesiones/{id}/adjuntos`. 401/403 son [ErrorAutenticacion]; 5xx, 408 y 429, [ErrorApi]. */
sealed interface ResultadoAdjunto {
    /**
     * 201 (nuevo) o 200 (ese id ya estaba: se había perdido la respuesta). [adjunto] y [sesion] son lo
     * que informa el server (`null` si no se pudo leer).
     */
    data class Subido(val nuevo: Boolean, val adjunto: AdjuntoServidor?, val sesion: EstadoSesionServidor?) : ResultadoAdjunto

    /** 404 de una sesión que el server no tiene (todavía): se reintenta más tarde. */
    data class SesionNoEncontrada(val detalle: String) : ResultadoAdjunto

    /** 404 "Not Found" (FastAPI sin la ruta) o 405: el server todavía no acepta adjuntos. */
    data object SinSoporte : ResultadoAdjunto

    /** 409 (la clase está en error), 413, 415, 422…: no se arregla reintentando solo. */
    data class Rechazado(val codigo: Int, val detalle: String) : ResultadoAdjunto
}

/** Respuesta de `POST /api/sesiones/{id}/regenerar`. */
sealed interface ResultadoRegenerar {
    /** 200: la clase quedó en cola desde el apunte. [sesion] es su estado nuevo (`null` si no se pudo leer). */
    data class Ok(val sesion: EstadoSesionServidor?) : ResultadoRegenerar

    /** 409: no está lista ni en error, o ya se está procesando. */
    data class Rechazado(val detalle: String) : ResultadoRegenerar

    /** 404 de una sesión que el server no tiene. */
    data class NoEncontrada(val detalle: String) : ResultadoRegenerar

    /** 404 "Not Found" o 405: el server todavía no tiene esta función. */
    data object SinSoporte : ResultadoRegenerar
}

/** Los 4xx de `POST /adjuntos` que se leen como resultado (401/403 igual son [ErrorAutenticacion]; 408, 425 y 429 se reintentan). */
internal val CODIGOS_RECHAZO_ADJUNTO: Set<Int> = (400..499).toSet() - setOf(408, 425, 429)

/** El server no tiene esa ruta: FastAPI responde `{"detail": "Not Found"}`, o 405 si la ruta existe con otro método. */
fun esRutaInexistente(codigo: Int, cuerpo: String): Boolean =
    codigo == 405 || (codigo == 404 && detalleDe(cuerpo) == "Not Found")
