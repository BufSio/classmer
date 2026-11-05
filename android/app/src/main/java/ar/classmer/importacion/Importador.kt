package ar.classmer.importacion

import android.os.StatFs
import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.ArchivoImportado
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Importacion
import ar.classmer.modelo.SesionLocal
import ar.classmer.util.Tiempo
import ar.classmer.util.Uuid7
import java.io.File
import java.io.IOException
import java.util.Locale

/** No se puede importar: el audio pesa demasiado o no entra en el celu. */
class ImportacionInvalida(mensaje: String) : Exception(mensaje)

/** Un documento elegido en la pantalla, con lo que informó el selector (cualquier dato puede faltar). */
data class DocumentoElegido(val uri: String, val nombre: String?, val bytes: Long?, val mime: String?)

/** Lo que se eligió en "Importar clase", listo para guardar. */
data class PedidoImportacion(
    val audio: DocumentoElegido,
    /** La que midió el celu al elegir el audio (`null` si no pudo). */
    val duracionMs: Long?,
    val fotos: List<DocumentoElegido>,
    /** `null` = Auto (IA). */
    val materiaId: String?,
    val nombreMateria: String?,
    val origenMateria: String,
    val tipoClase: String,
    /** ISO 8601 con zona. */
    val inicio: String,
    /** En Auto no va. */
    val numeroClase: Int?,
    val eventoCalendario: String?,
    val dispositivo: String,
)

/**
 * Importar una clase que el celu no grabó (por ejemplo, el audio que mandó un amigo por WhatsApp).
 * Va por el mismo camino que una grabada: la sesión se guarda en el celu (`sesiones/<id>/`), en el
 * estado local `importando` mientras [CopiaImportacion] copia el audio y las fotos, y después la sube
 * el [ar.classmer.subida.Subidor] de siempre, marcada `importada`.
 */
object Importador {
    /** El tope del server para el audio de una importada (2 GiB). */
    const val MAX_AUDIO = 2L * 1024 * 1024 * 1024

    /** El tope del server para una foto (como las de la grabación). */
    const val MAX_FOTO = 50L * 1024 * 1024

    /** Lo que tiene que quedar libre en el celu después de copiar: lo mismo que pide la grabación. */
    const val MARGEN_LIBRE = AlmacenAdjuntos.MARGEN_LIBRE

    /** Extensiones de audio (y video con audio) que se conservan en la copia; las demás quedan `.bin`. */
    val EXTENSIONES_AUDIO = setOf(
        "ogg", "oga", "opus", "m4a", "mp4", "mp3", "wav", "webm", "aac", "flac", "amr", "3gp", "3gpp", "wma",
        "mka", "mkv", "mov",
    )

    /** Si el nombre no trae una extensión conocida, sale del tipo que informa el selector. */
    private val EXTENSION_POR_MIME = mapOf(
        "audio/ogg" to "ogg", "application/ogg" to "ogg", "audio/opus" to "opus", "audio/mp4" to "m4a",
        "audio/x-m4a" to "m4a", "audio/m4a" to "m4a", "audio/aac" to "aac", "audio/mpeg" to "mp3", "audio/mp3" to "mp3",
        "audio/wav" to "wav", "audio/x-wav" to "wav", "audio/webm" to "webm", "video/webm" to "webm",
        "audio/flac" to "flac", "audio/x-flac" to "flac", "audio/amr" to "amr", "audio/3gpp" to "3gp",
        "video/3gpp" to "3gp", "video/mp4" to "mp4", "audio/x-ms-wma" to "wma", "video/quicktime" to "mov",
        "video/x-matroska" to "mkv", "audio/x-matroska" to "mka",
    )

    private val FOTO_POR_MIME = mapOf("image/jpeg" to "jpg", "image/png" to "png", "image/webp" to "webp", "image/heic" to "heic", "image/heif" to "heic")

    /** Lo que ofrece el selector del audio: WhatsApp manda `.opus` (audio/ogg) y audios en `.m4a` o `.mp4`. */
    val TIPOS_AUDIO = arrayOf(
        "audio/*", "application/ogg", "video/mp4", "video/3gpp", "video/3gpp2", "video/webm", "video/x-matroska",
        "video/quicktime",
    )

    /** La extensión de `audio.<ext>`: la del nombre si es conocida, si no la del tipo, y si no `bin`. */
    fun extensionAudio(nombre: String?, mime: String?): String {
        extensionDe(nombre)?.takeIf { it in EXTENSIONES_AUDIO }?.let { return it }
        val tipo = mime?.substringBefore(';')?.trim()?.lowercase(Locale.ROOT)
        return EXTENSION_POR_MIME[tipo] ?: "bin"
    }

    /** La extensión de `fotos/<id>.<ext>`: una de las que acepta el server (si no, `jpg`, como hace él). */
    fun extensionFoto(nombre: String?, mime: String?): String = when (val ext = extensionDe(nombre)) {
        "jpg", "png", "webp", "heic" -> ext
        "jpeg" -> "jpg"
        "heif" -> "heic"
        else -> FOTO_POR_MIME[mime?.substringBefore(';')?.trim()?.lowercase(Locale.ROOT)] ?: "jpg"
    }

    private fun extensionDe(nombre: String?): String? =
        nombre?.trim()?.substringAfterLast('.', "")?.lowercase(Locale.ROOT)?.takeIf { it.isNotEmpty() && it.length <= 5 }

    /** El audio no se puede importar por su tamaño (el que informó el selector). */
    fun problemaAudio(bytes: Long?): String? =
        if (bytes != null && bytes > MAX_AUDIO) "El audio pesa ${gb(bytes)}: el tope es 2 GB. No se puede importar." else null

    fun problemaFoto(nombre: String?, bytes: Long?): String? =
        if (bytes != null && bytes > MAX_FOTO) "${nombre ?: "Una foto"} pesa más de 50 MB: no se importa." else null

    /** Con [libre] bytes libres (0 = no se sabe), ¿entran [necesita] dejando el margen de la grabación? */
    fun problemaEspacio(libre: Long, necesita: Long): String? {
        if (libre <= 0 || libre - necesita >= MARGEN_LIBRE) return null
        return "No hay lugar en el celu para copiar la clase (${mb(necesita)}): quedarían menos de " +
            "${MARGEN_LIBRE / (1024 * 1024)} MB libres, que hacen falta para grabar. Liberá espacio y probá de nuevo."
    }

    /** El espacio libre con `StatFs` (0 si no se puede saber). */
    fun espacioLibre(dir: File): Long = runCatching {
        if (!dir.exists()) dir.mkdirs()
        StatFs(dir.path).availableBytes
    }.getOrDefault(0L)

    /**
     * Guarda la clase en el celu, en `importando`: `importacion.json` primero (con lo que hay que
     * copiar) y después `sesion.json`. Mira el tamaño del audio y el espacio libre antes de escribir
     * nada. La copia la hace después [CopiaImportacion].
     *
     * @throws ImportacionInvalida si el audio pasa los 2 GiB o no entra en el celu.
     */
    fun crear(
        almacen: AlmacenSesiones,
        pedido: PedidoImportacion,
        id: String = Uuid7.nuevo(),
        ahora: String = Tiempo.ahoraIso(),
        espacioLibre: (File) -> Long = ::espacioLibre,
    ): SesionLocal {
        problemaAudio(pedido.audio.bytes)?.let { throw ImportacionInvalida(it) }
        if (!almacen.raiz.mkdirs() && !almacen.raiz.isDirectory) throw IOException("No se pudo crear ${almacen.raiz}")
        val necesita = (pedido.audio.bytes ?: 0) + pedido.fotos.sumOf { it.bytes ?: 0 }
        problemaEspacio(espacioLibre(almacen.raiz), necesita)?.let { throw ImportacionInvalida(it) }

        val archivoAudio = "audio." + extensionAudio(pedido.audio.nombre, pedido.audio.mime)
        val nombreAudio = pedido.audio.nombre?.trim()?.take(MAX_NOMBRE)?.ifEmpty { null }
        val fotos = pedido.fotos.map { f ->
            val fotoId = Uuid7.nuevo()
            ArchivoImportado(
                uri = f.uri, nombre = f.nombre, bytes = f.bytes,
                destino = "${AlmacenSesiones.DIR_FOTOS}/$fotoId.${extensionFoto(f.nombre, f.mime)}", id = fotoId,
            )
        }
        val importacion = Importacion(
            audio = ArchivoImportado(pedido.audio.uri, nombreAudio, pedido.audio.bytes, archivoAudio),
            fotos = fotos,
            creado = ahora,
            duracionElegidaMs = pedido.duracionMs?.takeIf { it > 0 },
        )
        val sesion = SesionLocal(
            id = id,
            materiaId = pedido.materiaId,
            origenMateria = pedido.origenMateria,
            tipoClase = pedido.tipoClase,
            inicio = pedido.inicio,
            dispositivo = pedido.dispositivo,
            numeroClase = pedido.numeroClase?.takeIf { it > 0 && pedido.materiaId != null },
            estadoLocal = EstadoLocal.IMPORTANDO,
            duracionMs = pedido.duracionMs?.coerceAtLeast(0) ?: 0,
            nombreMateria = pedido.nombreMateria,
            eventoCalendario = pedido.eventoCalendario,
            // Nada del corte automático: no hay grabación que cortar.
            finEventoMs = null,
            preguntarCorteMs = null,
            importada = true,
            audioNombre = nombreAudio ?: archivoAudio,
            audioBytes = pedido.audio.bytes,
            archivoAudio = archivoAudio,
        )
        val d = almacen.dir(id)
        if (!d.mkdirs() && !d.isDirectory) throw IOException("No se pudo crear $d")
        almacen.dirFotos(id).mkdirs()
        almacen.archivoEventos(id).createNewFile()
        almacen.guardarImportacion(id, importacion)
        almacen.guardarSesion(sesion)
        return sesion
    }

    /** Lo copiado hasta ahora: el `.parcial` del audio (o el audio entero) y las fotos que ya están. */
    fun copiado(importacion: Importacion, largoParcial: Long): Long =
        (if (importacion.audio.copiado) importacion.audio.bytes ?: 0 else largoParcial) +
            importacion.fotos.filter { it.copiado }.sumOf { it.bytes ?: 0 }

    /** De 0 a 1, o `null` si el selector no informó el tamaño del audio. */
    fun progreso(importacion: Importacion, largoParcial: Long): Float? {
        if (importacion.audio.bytes == null && !importacion.audio.copiado) return null
        val total = importacion.bytesTotales.takeIf { it > 0 } ?: return null
        return (copiado(importacion, largoParcial).toFloat() / total).coerceIn(0f, 1f)
    }

    fun archivoParcial(almacen: AlmacenSesiones, sesionId: String, importacion: Importacion) =
        File(almacen.dir(sesionId), importacion.audio.destino + CopiaImportacion.EXT_PARCIAL)

    /** Las URIs de los documentos elegidos (para soltar sus permisos al terminar o al descartar). */
    fun uris(importacion: Importacion): List<String> = listOf(importacion.audio.uri) + importacion.fotos.map { it.uri }

    /**
     * Los documentos que todavía hacen falta: los de las importaciones que se están copiando (o cuya
     * copia falló, que se pueden reintentar) y los de las copias de adjuntos pendientes. Sus permisos no
     * se sueltan ([Documentos.soltarPermisos]).
     */
    fun urisEnUso(almacen: AlmacenSesiones, adjuntos: AlmacenAdjuntos): Set<String> {
        val importando = almacen.listar()
            .filter { it.importada && it.estadoLocal == EstadoLocal.IMPORTANDO }
            .flatMap { s -> almacen.leerImportacion(s.id)?.let(::uris).orEmpty() }
        return (importando + adjuntos.copiasPendientes().map { it.uri }).toSet()
    }

    /**
     * Se puede descartar una importación que falló (o que quedó rota, sin `importacion.json`), que
     * todavía no terminó de copiarse y que nunca llegó al server.
     */
    fun descartable(almacen: AlmacenSesiones, sesion: SesionLocal): Boolean {
        if (!sesion.importada || sesion.estadoLocal != EstadoLocal.IMPORTANDO) return false
        if (almacen.leerSubida(sesion.id).sesionCreada) return false
        val importacion = almacen.leerImportacion(sesion.id)
        return importacion == null || importacion.error != null
    }

    /**
     * "Descartar" una importación que falló: borra del celu las copias que hizo la app (la carpeta de
     * la clase, con el audio a medias y las fotos copiadas, y su material, que tampoco se subió). Los
     * archivos originales no se tocan. Una clase grabada, o una importada que ya está en el server, no
     * se borra nunca. Primero `sesion.json`: desde ahí la clase ya no existe, aunque el resto falle.
     *
     * @return las URIs cuyo permiso hay que soltar, o `null` si no se descartó.
     */
    fun descartar(almacen: AlmacenSesiones, adjuntos: AlmacenAdjuntos, sesionId: String): List<String>? {
        val sesion = almacen.leerSesion(sesionId) ?: return null
        if (!descartable(almacen, sesion)) return null
        val uris = almacen.leerImportacion(sesionId)?.let(::uris).orEmpty()
        if (!almacen.archivoSesion(sesionId).delete()) return null
        almacen.dir(sesionId).deleteRecursively()
        adjuntos.dir(sesionId).deleteRecursively()
        return uris
    }

    fun mb(bytes: Long): String = "%.1f MB".format(Locale.forLanguageTag("es-AR"), bytes / (1024.0 * 1024.0))

    private fun gb(bytes: Long): String = "%.1f GB".format(Locale.forLanguageTag("es-AR"), bytes / (1024.0 * 1024.0 * 1024.0))

    private const val MAX_NOMBRE = 200
}
