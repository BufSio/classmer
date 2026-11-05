package ar.classmer.datos

import android.content.Context
import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.CopiaPendiente
import ar.classmer.modelo.EstadoSubidaAdjuntos
import ar.classmer.modelo.SoporteAdjuntos
import ar.classmer.util.JsonClassmer
import ar.classmer.util.Uuid7
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.IOException
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/** El archivo no se puede adjuntar: pesa demasiado, está vacío, no es de un tipo que acepte el server o no entra en el celu. */
class AdjuntoInvalido(mensaje: String) : Exception(mensaje)

/** Los tipos que acepta el server, reconocidos por los primeros bytes (no por la extensión ni el selector). */
enum class TipoAdjunto(val mime: String, val extension: String) {
    PDF("application/pdf", "pdf"),
    JPEG("image/jpeg", "jpg"),
    PNG("image/png", "png"),
    WEBP("image/webp", "webp"),
    HEIC("image/heic", "heic"),
    HEIF("image/heif", "heic"),
    ;

    companion object {
        /** Los bytes del principio que hacen falta: el `%PDF-` puede estar en cualquier lugar de los primeros 1024. */
        const val CABECERA = 1024

        private val PNG_FIRMA = byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)
        private val MARCAS_HEIC = setOf("heic", "heix", "heim", "heis", "hevc", "hevx")
        private val MARCAS_HEIF = setOf("mif1", "msf1")

        fun detectar(cabecera: ByteArray, largo: Int = cabecera.size): TipoAdjunto? {
            val n = minOf(largo, cabecera.size)
            fun ascii(desde: Int, hasta: Int) = if (n >= hasta) String(cabecera, desde, hasta - desde, Charsets.ISO_8859_1) else null
            return when {
                n >= 3 && cabecera[0] == 0xFF.toByte() && cabecera[1] == 0xD8.toByte() && cabecera[2] == 0xFF.toByte() -> JPEG
                n >= 8 && (0 until 8).all { cabecera[it] == PNG_FIRMA[it] } -> PNG
                ascii(0, 4) == "RIFF" && ascii(8, 12) == "WEBP" -> WEBP
                // HEIC/HEIF: caja `ftyp` con la marca principal (una AVIF, por ejemplo, no cuenta).
                ascii(4, 8) == "ftyp" && ascii(8, 12) in MARCAS_HEIC -> HEIC
                ascii(4, 8) == "ftyp" && ascii(8, 12) in MARCAS_HEIF -> HEIF
                contiene(cabecera, minOf(n, CABECERA), "%PDF-".toByteArray(Charsets.ISO_8859_1)) -> PDF
                else -> null
            }
        }

        private fun contiene(datos: ByteArray, largo: Int, buscado: ByteArray): Boolean =
            (0..largo - buscado.size).any { i -> buscado.indices.all { datos[i + it] == buscado[it] } }
    }
}

/**
 * Los adjuntos de las clases (PDF de slides o notas, imágenes), en una raíz aparte de las sesiones
 * para no mezclarse con [AlmacenSesiones.listar] ni con la recuperación de grabaciones cortadas:
 *
 * ```
 * adjuntos/
 *   soporte.json        si el server acepta adjuntos (lo escribe sólo la subida)
 *   <sesion>/
 *     <id>.<ext>        el archivo, copiado entero (temporal + fsync + renombrar)
 *     <id>.json         sus datos; se escribe último, así un adjunto sin .json no existe
 *     <id>.copiando     el pedido de copia, mientras se copia (se retoma si la app se cerró)
 *     <id>.reintentar   lo crea la pantalla ("Reintentar") y lo consume la subida
 *     subida.json       qué confirmó el server (lo escribe sólo la subida)
 * ```
 *
 * La clase puede existir sólo en el server (por ejemplo, grabada en otro celu): no hace falta que
 * haya una carpeta en `sesiones/`. Desde la app sólo se borra un adjunto que todavía no se subió,
 * con "Descartar".
 *
 * @param margenLibre lo que tiene que quedar libre en el celu después de copiar (para seguir grabando).
 * @param espacioLibre el espacio libre en una carpeta (0 = no se sabe), reemplazable en los tests.
 */
class AlmacenAdjuntos(
    val raiz: File,
    private val maximo: Long = MAX_ADJUNTO,
    private val margenLibre: Long = MARGEN_LIBRE,
    private val espacioLibre: (File) -> Long = { it.usableSpace },
) {

    fun dir(sesionId: String) = File(raiz, sesionId)
    fun archivoDatos(sesionId: String, id: String) = File(dir(sesionId), "$id.json")
    fun archivo(adjunto: AdjuntoLocal) = File(dir(adjunto.sesionId), adjunto.archivo)
    fun archivoSubida(sesionId: String) = File(dir(sesionId), NOMBRE_SUBIDA)
    fun archivoPedidoReintento(sesionId: String, id: String) = File(dir(sesionId), "$id.reintentar")
    fun archivoCopia(sesionId: String, id: String) = File(dir(sesionId), "$id$EXT_COPIA")
    fun archivoSoporte() = File(raiz, "soporte.json")

    /** Las clases que tienen adjuntos en el celu, la más vieja primero (los ids son UUIDv7). */
    fun sesiones(): List<String> =
        (raiz.listFiles { f -> f.isDirectory } ?: emptyArray()).map { it.name }.sorted()

    /** Los adjuntos de una clase en orden de llegada. Los que no terminaron de copiarse no aparecen. */
    fun listar(sesionId: String): List<AdjuntoLocal> =
        (dir(sesionId).listFiles { f -> f.isFile && f.name.endsWith(".json") && f.name != NOMBRE_SUBIDA } ?: emptyArray())
            .mapNotNull { f -> leerJson(f) { JsonClassmer.decodeFromString(AdjuntoLocal.serializer(), it) } }
            .filter { it.sesionId == sesionId && archivo(it).isFile }
            .sortedWith(compareBy<AdjuntoLocal> { it.id }.thenBy { it.creado })

    fun leerSubida(sesionId: String): EstadoSubidaAdjuntos =
        leerJson(archivoSubida(sesionId)) { JsonClassmer.decodeFromString(EstadoSubidaAdjuntos.serializer(), it) }
            ?: EstadoSubidaAdjuntos()

    fun guardarSubida(sesionId: String, estado: EstadoSubidaAdjuntos) {
        dir(sesionId).mkdirs()
        AlmacenSesiones.escribirAtomico(archivoSubida(sesionId), JsonClassmer.encodeToString(EstadoSubidaAdjuntos.serializer(), estado))
    }

    fun leerSoporte(): SoporteAdjuntos =
        leerJson(archivoSoporte()) { JsonClassmer.decodeFromString(SoporteAdjuntos.serializer(), it) } ?: SoporteAdjuntos()

    fun guardarSoporte(soporte: SoporteAdjuntos) {
        raiz.mkdirs()
        AlmacenSesiones.escribirAtomico(archivoSoporte(), JsonClassmer.encodeToString(SoporteAdjuntos.serializer(), soporte))
    }

    // ---- Copias pendientes (se retoman si la app se cerró en el medio) ----

    fun anotarCopia(copia: CopiaPendiente) {
        val d = dir(copia.sesionId)
        if (!d.mkdirs() && !d.isDirectory) throw IOException("No se pudo crear $d")
        AlmacenSesiones.escribirAtomico(archivoCopia(copia.sesionId, copia.id), JsonClassmer.encodeToString(CopiaPendiente.serializer(), copia))
    }

    fun olvidarCopia(sesionId: String, id: String) {
        archivoCopia(sesionId, id).delete()
    }

    /** Las copias que quedaron anotadas, de todas las clases. */
    fun copiasPendientes(): List<CopiaPendiente> =
        sesiones().flatMap { s ->
            (dir(s).listFiles { f -> f.isFile && f.name.endsWith(EXT_COPIA) } ?: emptyArray())
                .mapNotNull { f -> leerJson(f) { JsonClassmer.decodeFromString(CopiaPendiente.serializer(), it) } }
                .filter { it.sesionId == s }
        }

    /**
     * "Descartar": borra del celu un adjunto que todavía no se subió. Primero sus datos (desde ahí ya no
     * existe), después el archivo. `subida.json` no se toca (lo escribe sólo la subida).
     */
    fun descartar(sesionId: String, id: String) {
        val adjunto = listar(sesionId).firstOrNull { it.id == id }
        archivoDatos(sesionId, id).delete()
        adjunto?.let { archivo(it).delete() }
        archivoPedidoReintento(sesionId, id).delete()
    }

    /**
     * Copia un archivo elegido con el selector a la carpeta de la clase. Mira el tipo en los primeros
     * bytes, el tamaño y el espacio libre antes de escribir nada, y corta la copia si pasa el tope o si
     * el celu se queda sin lugar. El adjunto existe recién cuando está su `.json`: si algo falla en el
     * medio, no queda nada a medias.
     *
     * @param tamanioDeclarado el tamaño que informa el selector, si lo informa (se rechaza sin copiar).
     * @throws AdjuntoInvalido si pesa más del tope, está vacío, no es PDF, JPEG, PNG, WebP ni HEIC, o no
     *   entra en el celu dejando [margenLibre] libre.
     */
    fun copiar(
        sesionId: String,
        origen: InputStream,
        nombreOriginal: String?,
        offsetMs: Long?,
        creado: String,
        tamanioDeclarado: Long? = null,
        id: String = Uuid7.nuevo(),
        pista: String? = null,
    ): AdjuntoLocal {
        val nombre = nombreOriginal?.trim()?.take(MAX_NOMBRE)?.ifEmpty { null }
        val paraMostrar = nombre ?: "El archivo"
        if (tamanioDeclarado != null && tamanioDeclarado > maximo) throw AdjuntoInvalido(muyGrande(paraMostrar))

        // Los primeros bytes dicen el tipo: si el server no lo va a aceptar, no se copia nada.
        val cabecera = ByteArray(TipoAdjunto.CABECERA)
        val leidos = leerHasta(origen, cabecera)
        if (leidos == 0) throw AdjuntoInvalido("$paraMostrar está vacío: no se adjuntó.")
        val tipo = TipoAdjunto.detectar(cabecera, leidos)
            ?: throw AdjuntoInvalido("$paraMostrar no es un PDF ni una imagen (JPEG, PNG, WebP o HEIC): no se adjuntó.")

        val d = dir(sesionId)
        if (!d.mkdirs() && !d.isDirectory) throw IOException("No se pudo crear $d")
        limpiar(sesionId)
        // El audio de una grabación en curso va al mismo disco: un PDF grande no puede dejarlo sin lugar.
        val libre = espacioLibre(d)
        if (libre > 0 && libre - (tamanioDeclarado ?: leidos.toLong()) < margenLibre) throw AdjuntoInvalido(sinLugar(paraMostrar))
        val nombreArchivo = "$id.${tipo.extension}"
        val destino = File(d, nombreArchivo)
        val tmp = File(d, "$nombreArchivo.tmp")
        var total = leidos.toLong()
        try {
            FileOutputStream(tmp).use { out ->
                out.write(cabecera, 0, leidos)
                val buffer = ByteArray(256 * 1024)
                var desdeRevision = 0L
                while (true) {
                    val n = origen.read(buffer)
                    if (n < 0) break
                    total += n
                    if (total > maximo) throw AdjuntoInvalido(muyGrande(paraMostrar))
                    out.write(buffer, 0, n)
                    // El selector puede no informar el tamaño (o mentirlo): se mira el espacio cada tanto.
                    desdeRevision += n
                    if (desdeRevision >= REVISAR_ESPACIO_CADA) {
                        desdeRevision = 0
                        val queda = espacioLibre(d)
                        if (queda in 1 until margenLibre) throw AdjuntoInvalido(sinLugar(paraMostrar))
                    }
                }
                out.fd.sync()
            }
            Files.move(tmp.toPath(), destino.toPath(), StandardCopyOption.ATOMIC_MOVE)
        } catch (e: Throwable) {
            tmp.delete()
            throw e
        }

        val adjunto = AdjuntoLocal(
            id = id,
            sesionId = sesionId,
            nombreOriginal = nombre ?: "adjunto.${tipo.extension}",
            mime = tipo.mime,
            bytes = total,
            offsetMs = offsetMs,
            creado = creado,
            archivo = nombreArchivo,
            pista = pista.takeIf { offsetMs != null },
        )
        try {
            AlmacenSesiones.escribirAtomico(archivoDatos(sesionId, id), JsonClassmer.encodeToString(AdjuntoLocal.serializer(), adjunto))
        } catch (e: Throwable) {
            destino.delete()
            throw e
        }
        return adjunto
    }

    private fun muyGrande(nombre: String) =
        "$nombre pesa más de ${maximo / (1024 * 1024)} MB: no se adjuntó."

    private fun sinLugar(nombre: String) =
        "No hay lugar en el celu para $nombre: quedarían menos de ${margenLibre / (1024 * 1024)} MB libres, " +
            "que hacen falta para grabar. Liberá espacio y adjuntalo de nuevo."

    /** Lee hasta llenar [buffer] o hasta el final. Devuelve cuántos bytes leyó. */
    private fun leerHasta(origen: InputStream, buffer: ByteArray): Int {
        var n = 0
        while (n < buffer.size) {
            val r = origen.read(buffer, n, buffer.size - n)
            if (r < 0) break
            n += r
        }
        return n
    }

    /**
     * Restos de una copia que se cortó (la app se cerró en el medio), pasada una hora sin tocarse: el
     * temporal, o el archivo ya renombrado al que le faltó el `.json` (y que no tiene una copia anotada).
     */
    fun limpiar(sesionId: String, ahoraMs: Long = System.currentTimeMillis()) {
        val d = dir(sesionId)
        val archivos = d.listFiles { f -> f.isFile } ?: return
        val nombres = archivos.map { it.name }.toSet()
        for (f in archivos) {
            if (ahoraMs - f.lastModified() <= 60 * 60_000L) continue
            val n = f.name
            val raizNombre = n.substringBefore('.')
            val huerfano = when {
                n.endsWith(".tmp") -> true
                n == NOMBRE_SUBIDA || n.endsWith(".json") || n.endsWith(EXT_COPIA) || n.endsWith(".reintentar") -> false
                else -> "$raizNombre.json" !in nombres && "$raizNombre$EXT_COPIA" !in nombres
            }
            if (huerfano) f.delete()
        }
    }

    private fun <T> leerJson(archivo: File, decodificar: (String) -> T): T? {
        if (!archivo.exists()) return null
        return runCatching { decodificar(archivo.readText(Charsets.UTF_8)) }.getOrNull()
    }

    companion object {
        /** El tope del server para un adjunto (`MAX_ADJUNTO`). */
        const val MAX_ADJUNTO = 100L * 1024 * 1024

        /** Lo mismo que pide la grabación (`MIN_ESPACIO_LIBRE` del servicio). */
        const val MARGEN_LIBRE = 300L * 1024 * 1024
        private const val REVISAR_ESPACIO_CADA = 8L * 1024 * 1024
        private const val MAX_NOMBRE = 200
        private const val NOMBRE_SUBIDA = "subida.json"
        private const val EXT_COPIA = ".copiando"

        /** Al lado de `sesiones/`, en el almacenamiento externo propio de la app (se puede rescatar con `adb pull`). */
        fun de(context: Context): AlmacenAdjuntos {
            val base = context.getExternalFilesDir(null) ?: context.filesDir
            return AlmacenAdjuntos(File(base, "adjuntos"))
        }
    }
}
