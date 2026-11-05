package ar.classmer.modelo

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

// ---- Importar una clase: el audio que no grabó el celu (por ejemplo, el que mandó un amigo) ----

/** Un documento elegido para importar (el audio o una foto) y cómo va su copia al celu. */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class ArchivoImportado(
    /** La URI que dio el selector, con permiso persistente mientras se copia. */
    val uri: String,
    /** Nombre y tamaño que informó el selector (cualquiera de los dos puede faltar). */
    val nombre: String? = null,
    val bytes: Long? = null,
    /** Dónde queda la copia, relativo a la carpeta de la clase: `audio.m4a` o `fotos/<id>.jpg`. */
    val destino: String,
    /** El id del evento `foto` (el audio no tiene). */
    @EncodeDefault(EncodeDefault.Mode.NEVER) val id: String? = null,
    /** La copia quedó entera en [destino]. */
    val copiado: Boolean = false,
    /**
     * Sólo el audio, que se copia a un `.parcial`: los bytes que ya están en el disco (con `fsync`).
     * Si la copia se corta, se retoma desde acá.
     */
    val confirmados: Long = 0,
    /** Una foto que no se pudo copiar (no se puede leer, pesa demasiado): queda afuera de la clase. */
    @EncodeDefault(EncodeDefault.Mode.NEVER) val problema: String? = null,
)

/**
 * `sesiones/<id>/importacion.json`: lo que falta copiar de una clase importada. Se escribe antes que
 * `sesion.json` (una clase importada sin este archivo está rota) y después lo escribe sólo la copia
 * ([ar.classmer.importacion.CopiaImportacion]). Al terminar queda, como registro de lo que se importó.
 */
@OptIn(ExperimentalSerializationApi::class)
@Serializable
data class Importacion(
    val audio: ArchivoImportado,
    val fotos: List<ArchivoImportado> = emptyList(),
    /** Cuándo se confirmó: es el `creado` de los eventos de las fotos. */
    val creado: String,
    /** La duración que midió el celu al elegir el audio (por si la copia no se puede medir). */
    @EncodeDefault(EncodeDefault.Mode.NEVER)
    @SerialName("duracion_elegida_ms") val duracionElegidaMs: Long? = null,
    /**
     * La copia del audio falló (se borró el archivo, se perdió el permiso, no hay lugar): la clase
     * muestra el motivo con "Reintentar" y "Descartar". No se reintenta sola.
     */
    @EncodeDefault(EncodeDefault.Mode.NEVER) val error: String? = null,
    /** Las fotos copiadas ya se anotaron en `eventos.jsonl` (sin minuto). */
    @SerialName("fotos_registradas") val fotosRegistradas: Boolean = false,
) {
    /** Lo que hay que copiar en total (lo que informó el selector; 0 si no informó nada). */
    val bytesTotales: Long get() = (audio.bytes ?: 0) + fotos.sumOf { it.bytes ?: 0 }

    /** Las fotos que quedaron afuera, con el motivo. */
    val problemasFotos: List<String> get() = fotos.mapNotNull { it.problema }
}
