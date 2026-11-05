package ar.classmer.importacion

import android.content.Context
import android.content.Intent
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import java.io.File
import java.io.FileNotFoundException
import java.io.FileInputStream
import java.io.IOException
import java.io.InputStream
import java.time.LocalDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors

/** Lo que se sabe de un documento elegido con el selector. */
data class DatosDocumento(
    val nombre: String?,
    val bytes: Long?,
    val mime: String?,
    /** Fecha de modificación (ms desde 1970), si el proveedor la informa. */
    val modificadoMs: Long?,
)

/**
 * Los documentos elegidos con el selector del sistema (audio, fotos, material): sus datos, su
 * permiso persistente (para copiarlos aunque la app se cierre) y leerlos desde un byte.
 */
object Documentos {

    fun datos(context: Context, uri: Uri): DatosDocumento {
        var nombre: String? = null
        var bytes: Long? = null
        var modificado: Long? = null
        runCatching {
            context.contentResolver.query(uri, null, null, null, null)?.use { c ->
                if (c.moveToFirst()) {
                    val iNombre = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    val iTamanio = c.getColumnIndex(OpenableColumns.SIZE)
                    val iModificado = c.getColumnIndex(DocumentsContract.Document.COLUMN_LAST_MODIFIED)
                    if (iNombre >= 0 && !c.isNull(iNombre)) nombre = c.getString(iNombre)
                    if (iTamanio >= 0 && !c.isNull(iTamanio)) bytes = c.getLong(iTamanio).takeIf { it > 0 }
                    if (iModificado >= 0 && !c.isNull(iModificado)) modificado = c.getLong(iModificado).takeIf { it > 0 }
                }
            }
        }
        val mime = runCatching { context.contentResolver.getType(uri) }.getOrNull()
        return DatosDocumento(nombre, bytes, mime, modificado)
    }

    /** Para leerlo después (la copia sigue aunque se cierre la app). Si el proveedor no lo da, vale mientras viva el proceso. */
    fun tomarPermiso(context: Context, uri: Uri) {
        runCatching { context.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
    }

    /**
     * Lo elegido en la pantalla Importar, que todavía no es de ninguna importación (lo pone
     * [ar.classmer.ui.ImportarViewModel]). Quien saca algo de la pantalla lo saca primero del estado y
     * después suelta su permiso.
     */
    @Volatile
    var enPantalla: () -> Set<String> = { emptySet() }

    private val ejecutor = Executors.newSingleThreadExecutor()

    /**
     * Suelta, en segundo plano, el permiso de los documentos que ya no usa nadie. El permiso es uno por
     * documento (no lleva la cuenta de quién lo usa): si dos importaciones comparten el audio (por
     * ejemplo, se importó de nuevo después de una falla), soltarlo en una se lo saca a la otra. Por eso
     * se queda el de los que siguen en uso ([Importador.urisEnUso]) o elegidos en la pantalla Importar
     * ([enPantalla]), mirado al momento de soltarlo.
     */
    fun soltarPermisos(context: Context, uris: Collection<String>) {
        if (uris.isEmpty()) return
        val app = context.applicationContext
        val cuales = uris.distinct()
        ejecutor.execute {
            // Ante la duda, no se suelta: un permiso de más no molesta, uno de menos frena una copia.
            val enUso = runCatching { Importador.urisEnUso(AlmacenSesiones.de(app), AlmacenAdjuntos.de(app)) + enPantalla() }
                .getOrElse { return@execute }
            for (uri in cuales) {
                if (uri in enUso) continue
                runCatching { app.contentResolver.releasePersistableUriPermission(Uri.parse(uri), Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            }
        }
    }

    /**
     * Abre el documento desde [desde]. Si es un archivo de verdad se salta directo a ese byte; si es
     * un flujo (por ejemplo, de un proveedor en la nube), se leen y descartan los primeros bytes.
     */
    fun abrir(context: Context, uri: String, desde: Long): InputStream {
        val pfd = context.contentResolver.openFileDescriptor(Uri.parse(uri), "r")
            ?: throw FileNotFoundException("el proveedor no lo dejó abrir")
        val entrada = ParcelFileDescriptor.AutoCloseInputStream(pfd)
        if (desde <= 0) return entrada
        try {
            if (pfd.statSize >= 0) {
                entrada.channel.position(desde)
            } else {
                saltear(entrada, desde)
            }
        } catch (e: IOException) {
            entrada.close()
            throw e
        }
        return entrada
    }

    private fun saltear(entrada: FileInputStream, n: Long) {
        val buffer = ByteArray(64 * 1024)
        var falta = n
        while (falta > 0) {
            val leidos = entrada.read(buffer, 0, minOf(buffer.size.toLong(), falta).toInt())
            if (leidos < 0) throw IOException("el documento es más corto de lo que ya se había copiado")
            falta -= leidos
        }
    }
}

/** Duración y fecha de un audio con `MediaMetadataRetriever` (sin permisos: sólo lee el documento). */
object MetadatosAudio {
    data class Datos(val duracionMs: Long?, val fechaMs: Long?)

    fun leer(context: Context, uri: Uri): Datos = conRetriever({ it.setDataSource(context, uri) }) { r ->
        Datos(
            duracionMs = r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull()?.takeIf { it > 0 },
            fechaMs = fechaDeMetadato(r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DATE)),
        )
    } ?: Datos(null, null)

    /** La duración de la copia en el celu (condición del diseño: si no se puede, `null` y el server la mide). */
    fun duracionMs(archivo: File): Long? = conRetriever({ it.setDataSource(archivo.path) }) { r ->
        r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull()?.takeIf { it > 0 }
    }

    private fun <T> conRetriever(abrir: (MediaMetadataRetriever) -> Unit, leer: (MediaMetadataRetriever) -> T): T? {
        val r = MediaMetadataRetriever()
        return try {
            abrir(r)
            leer(r)
        } catch (e: Exception) {
            null
        } finally {
            runCatching { r.release() }
        }
    }

    private val FORMATOS = listOf("yyyyMMdd'T'HHmmss.SSS'Z'", "yyyyMMdd'T'HHmmss'Z'", "yyyyMMdd'T'HHmmss")
        .map { DateTimeFormatter.ofPattern(it) }

    /**
     * `METADATA_KEY_DATE` viene como `20261024T143005.000Z` (UTC). Los años anteriores a 2000 se
     * descartan: un MP4 sin fecha trae 1904 (el cero de QuickTime) y otros, 1970.
     */
    fun fechaDeMetadato(texto: String?): Long? {
        val t = texto?.trim()?.takeIf { it.isNotEmpty() } ?: return null
        for (f in FORMATOS) {
            val fecha = runCatching { LocalDateTime.parse(t, f) }.getOrNull() ?: continue
            if (fecha.year < 2000) return null
            return fecha.toInstant(ZoneOffset.UTC).toEpochMilli()
        }
        return null
    }
}
