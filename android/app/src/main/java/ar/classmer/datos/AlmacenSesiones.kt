package ar.classmer.datos

import android.content.Context
import ar.classmer.modelo.EstadoSesionServidor
import ar.classmer.modelo.EstadoSubida
import ar.classmer.modelo.Evento
import ar.classmer.modelo.FotoPendiente
import ar.classmer.modelo.Importacion
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.TipoEvento
import ar.classmer.util.JsonClassmer
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.RandomAccessFile
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * Las sesiones viven en archivos, una carpeta por clase:
 *
 * ```
 * sesiones/<id>/
 *   sesion.json      datos de la clase (lo escribe la grabación)
 *   audio.ogg        Ogg/Opus continuo (lo escribe MediaRecorder)
 *   eventos.jsonl    fotos, notas, marcas y pausas; sólo se agregan líneas
 *   fotos/<id>.jpg
 *   subida.json      hasta dónde confirmó el server (lo escribe sólo la subida)
 *   servidor.json    último estado que informó el server (lo escribe sólo la pantalla de inicio)
 * ```
 *
 * Una clase importada (un audio que no grabó el celu) tiene además `importacion.json` (lo que falta
 * copiar, lo escribe sólo la copia), el audio en `audio.<ext>` (mientras se copia, `audio.<ext>.parcial`)
 * y las fotos elegidas en `fotos/<id>.<ext>`.
 *
 * Nada de esto se borra desde la app. La única excepción es "Descartar" una importación que falló y
 * nunca llegó al server: son copias que hizo la app de archivos elegidos, y los originales no se tocan
 * ([ar.classmer.importacion.Importador.descartar]).
 */
class AlmacenSesiones(val raiz: File) {

    fun dir(id: String) = File(raiz, id)
    fun archivoSesion(id: String) = File(dir(id), "sesion.json")
    fun archivoAudio(id: String) = File(dir(id), NOMBRE_AUDIO)

    /** El audio de la sesión: `audio.ogg`, o el `audio.<ext>` de una importada. */
    fun archivoAudio(sesion: SesionLocal) = File(dir(sesion.id), sesion.archivoAudio ?: NOMBRE_AUDIO)
    fun archivoEventos(id: String) = File(dir(id), "eventos.jsonl")
    fun dirFotos(id: String) = File(dir(id), DIR_FOTOS)
    fun archivoSubida(id: String) = File(dir(id), "subida.json")
    fun archivoEstadoServidor(id: String) = File(dir(id), "servidor.json")

    fun archivoImportacion(id: String) = File(dir(id), "importacion.json")

    /** Lo crea la pantalla ("Reintentar") y lo consume la subida: así sólo la subida escribe subida.json. */
    fun archivoPedidoReintento(id: String) = File(dir(id), "reintentar")

    fun crear(sesion: SesionLocal) {
        val d = dir(sesion.id)
        if (!d.mkdirs() && !d.isDirectory) throw IOException("No se pudo crear $d")
        dirFotos(sesion.id).mkdirs()
        archivoEventos(sesion.id).createNewFile()
        guardarSesion(sesion)
    }

    fun guardarSesion(sesion: SesionLocal) =
        escribirAtomico(archivoSesion(sesion.id), JsonClassmer.encodeToString(SesionLocal.serializer(), sesion))

    fun leerSesion(id: String): SesionLocal? = leerJson(archivoSesion(id)) {
        JsonClassmer.decodeFromString(SesionLocal.serializer(), it)
    }

    /** Todas las sesiones locales, la más nueva primero (los ids son UUIDv7, ordenados por fecha). */
    fun listar(): List<SesionLocal> =
        (raiz.listFiles { f -> f.isDirectory } ?: emptyArray())
            .sortedByDescending { it.name }
            .mapNotNull { leerSesion(it.name) }

    fun leerSubida(id: String): EstadoSubida =
        leerJson(archivoSubida(id)) { JsonClassmer.decodeFromString(EstadoSubida.serializer(), it) }
            ?: EstadoSubida()

    fun guardarSubida(id: String, estado: EstadoSubida) =
        escribirAtomico(archivoSubida(id), JsonClassmer.encodeToString(EstadoSubida.serializer(), estado))

    fun leerImportacion(id: String): Importacion? =
        leerJson(archivoImportacion(id)) { JsonClassmer.decodeFromString(Importacion.serializer(), it) }

    fun guardarImportacion(id: String, importacion: Importacion) =
        escribirAtomico(archivoImportacion(id), JsonClassmer.encodeToString(Importacion.serializer(), importacion))

    fun leerEstadoServidor(id: String): EstadoSesionServidor? =
        leerJson(archivoEstadoServidor(id)) { JsonClassmer.decodeFromString(EstadoSesionServidor.serializer(), it) }

    fun guardarEstadoServidor(estado: EstadoSesionServidor) {
        if (!dir(estado.id).isDirectory) return
        escribirAtomico(
            archivoEstadoServidor(estado.id),
            JsonClassmer.encodeToString(EstadoSesionServidor.serializer(), estado),
        )
    }

    /**
     * Agrega un evento al final de `eventos.jsonl` con una sola escritura y `fsync`, así un corte
     * de luz o un cierre forzado no deja el archivo a medias (a lo sumo, la última línea incompleta).
     */
    @Synchronized
    fun agregarEvento(sesionId: String, evento: Evento) {
        val archivo = archivoEventos(sesionId)
        // Si una escritura anterior falló a mitad (disco lleno), se empieza en una línea nueva para
        // no pegar este evento al pedazo roto.
        val prefijo = if (terminaSinSaltoDeLinea(archivo)) "\n" else ""
        val linea = prefijo + JsonClassmer.encodeToString(Evento.serializer(), evento) + "\n"
        FileOutputStream(archivo, true).use { out ->
            out.write(linea.toByteArray(Charsets.UTF_8))
            out.fd.sync()
        }
    }

    private fun terminaSinSaltoDeLinea(archivo: File): Boolean {
        if (!archivo.exists() || archivo.length() == 0L) return false
        return RandomAccessFile(archivo, "r").use { raf ->
            raf.seek(raf.length() - 1)
            raf.read() != '\n'.code
        }
    }

    // ---- Fotos en curso ----
    //
    // Antes de abrir la cámara se deja `fotos/<id>.pendiente.json` con el minuto de la foto. Si la
    // respuesta de la cámara no llega (la app se cerró, la grabación terminó mientras tanto), la foto
    // igual se registra después con su minuto original, en vez de quedar en el celu sin evento.

    fun archivoFotoPendiente(sesionId: String, eventoId: String) = File(dirFotos(sesionId), "$eventoId.pendiente.json")

    fun marcarFotoPendiente(sesionId: String, foto: FotoPendiente) {
        dirFotos(sesionId).mkdirs()
        escribirAtomico(
            archivoFotoPendiente(sesionId, foto.id),
            JsonClassmer.encodeToString(FotoPendiente.serializer(), foto),
        )
    }

    fun olvidarFotoPendiente(sesionId: String, eventoId: String) {
        archivoFotoPendiente(sesionId, eventoId).delete()
    }

    /**
     * Registra las fotos que quedaron sin evento. Devuelve true si alguna todavía puede estar
     * guardándose (la cámara sigue abierta): en ese caso conviene esperar antes de `/finalizar`.
     */
    @Synchronized
    fun resolverFotosPendientes(sesionId: String, ahoraMs: Long = System.currentTimeMillis()): Boolean {
        val pendientes = dirFotos(sesionId).listFiles { f -> f.name.endsWith(".pendiente.json") } ?: return false
        if (pendientes.isEmpty()) return false
        val registrados = leerEventos(sesionId).map { it.id }.toSet()
        var esperando = false
        for (marca in pendientes) {
            val foto = leerJson(marca) { JsonClassmer.decodeFromString(FotoPendiente.serializer(), it) }
            if (foto == null) {
                marca.delete()
                continue
            }
            val jpg = File(dirFotos(sesionId), "${foto.id}.jpg")
            when {
                foto.id in registrados -> marca.delete()
                jpg.isFile && jpg.length() > 0 -> {
                    agregarEvento(
                        sesionId,
                        Evento(foto.id, TipoEvento.FOTO, foto.offsetMs, foto.creado, archivo = "$DIR_FOTOS/${jpg.name}", pista = foto.pista),
                    )
                    marca.delete()
                }
                ahoraMs - marca.lastModified() > ESPERA_FOTO_MS -> {
                    // La cámara nunca guardó nada: no hay foto que registrar.
                    if (jpg.isFile && jpg.length() == 0L) jpg.delete()
                    marca.delete()
                }
                else -> esperando = true
            }
        }
        return esperando
    }

    /** Los eventos completos (las líneas que terminan en `\n` y se pueden leer). */
    fun leerEventos(sesionId: String): List<Evento> {
        val archivo = archivoEventos(sesionId)
        if (!archivo.exists()) return emptyList()
        val texto = archivo.readText(Charsets.UTF_8)
        val completo = texto.substring(0, texto.lastIndexOf('\n') + 1)
        return completo.lineSequence()
            .filter { it.isNotBlank() }
            .mapNotNull { linea ->
                runCatching { JsonClassmer.decodeFromString(Evento.serializer(), linea) }.getOrNull()
            }
            .distinctBy { it.id }
            .toList()
    }

    private fun <T> leerJson(archivo: File, decodificar: (String) -> T): T? {
        if (!archivo.exists()) return null
        return runCatching { decodificar(archivo.readText(Charsets.UTF_8)) }.getOrNull()
    }

    companion object {
        const val NOMBRE_AUDIO = "audio.ogg"
        const val DIR_FOTOS = "fotos"

        /** Cuánto se espera a que la cámara guarde una foto antes de darla por perdida. */
        const val ESPERA_FOTO_MS = 10 * 60 * 1000L

        /**
         * En el almacenamiento externo propio de la app (`Android/data/ar.classmer/files`), que se
         * puede copiar por USB con `adb pull` si alguna vez hace falta rescatar una clase a mano.
         */
        fun de(context: Context): AlmacenSesiones {
            val base = context.getExternalFilesDir(null) ?: context.filesDir
            return AlmacenSesiones(File(base, "sesiones"))
        }

        /** Escribe en un temporal, hace `fsync` y lo renombra: el archivo nunca queda a medias. */
        fun escribirAtomico(destino: File, contenido: String) {
            val tmp = File(destino.parentFile, destino.name + ".tmp")
            FileOutputStream(tmp).use { out ->
                out.write(contenido.toByteArray(Charsets.UTF_8))
                out.fd.sync()
            }
            Files.move(tmp.toPath(), destino.toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        }
    }
}
