package ar.classmer.wear

import android.content.Context
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.IdGrabacion
import ar.classmer.watchlink.JsonWatch
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.RandomAccessFile
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/** Un inicio que pidió el celu y espera el toque en el reloj ("tocá para grabar"). */
@Serializable
data class PedidoInicio(
    val id: String,
    val materia: String? = null,
    @SerialName("tipo_clase") val tipoClase: String? = null,
    /** Hora del reloj (ms desde 1970) en que llegó el pedido. */
    val recibido: Long,
)

/**
 * Las grabaciones viven en el almacenamiento interno del reloj, una carpeta por clase:
 *
 * ```
 * grabaciones/<id>/
 *   audio.ogg         Ogg/Opus continuo (lo escribe MediaRecorder; sólo crece)
 *   grabacion.json    estado ([GrabacionWatch]): lo que se le informa al celu
 * pedido.json         el inicio que pidió el celu, mientras espera el toque
 * ```
 *
 * Una grabación se borra sólo cuando el celu avisa que tiene la copia verificada y que el server
 * la aceptó ([ar.classmer.watchlink.Rutas.BORRAR]).
 */
class AlmacenWatch(val raiz: File) {

    fun dir(id: String) = File(raiz, id)
    fun archivoAudio(id: String) = File(dir(id), NOMBRE_AUDIO)
    fun archivoGrabacion(id: String) = File(dir(id), "grabacion.json")
    private val archivoPedido get() = File(raiz.parentFile, "pedido.json")

    fun crear(g: GrabacionWatch) {
        require(IdGrabacion.valido(g.id)) { "Id inválido: ${g.id}" }
        val d = dir(g.id)
        if (!d.mkdirs() && !d.isDirectory) throw IOException("No se pudo crear $d")
        guardar(g)
    }

    @Synchronized
    fun guardar(g: GrabacionWatch) {
        if (!dir(g.id).isDirectory) return
        escribirAtomico(archivoGrabacion(g.id), JsonWatch.encodeToString(GrabacionWatch.serializer(), g))
    }

    fun leer(id: String): GrabacionWatch? {
        if (!IdGrabacion.valido(id)) return null
        val f = archivoGrabacion(id)
        if (!f.isFile) return null
        return runCatching { JsonWatch.decodeFromString(GrabacionWatch.serializer(), f.readText(Charsets.UTF_8)) }.getOrNull()
    }

    /** La más vieja primero (los ids son UUIDv7, ordenados por fecha). */
    fun listar(): List<GrabacionWatch> =
        (raiz.listFiles { f -> f.isDirectory } ?: emptyArray())
            .sortedBy { it.name }
            .mapNotNull { leer(it.name) }

    @Synchronized
    fun borrar(id: String): Boolean = IdGrabacion.valido(id) && dir(id).deleteRecursively()

    /**
     * Largo del audio ya asentado en el disco: se lee el largo y después se hace `fsync` (MediaRecorder
     * no lo hace). Así el celu nunca recibe bytes que un apagón del reloj podría borrar.
     */
    fun bytesFirmes(id: String): Long {
        val f = archivoAudio(id)
        if (!f.exists()) return 0
        val largo = f.length()
        runCatching { RandomAccessFile(f, "r").use { it.fd.sync() } }
        return largo
    }

    /**
     * El pedido vigente del celu. Vence a los [VIGENCIA_PEDIDO_MS]: después, tocar Grabar en el reloj
     * empieza una grabación nueva (con su propio id) en vez de usar el id de una clase de otro momento.
     */
    fun leerPedido(ahoraMs: Long = System.currentTimeMillis()): PedidoInicio? {
        val f = archivoPedido
        if (!f.isFile) return null
        val p = runCatching { JsonWatch.decodeFromString(PedidoInicio.serializer(), f.readText(Charsets.UTF_8)) }.getOrNull()
        if (p == null || ahoraMs - p.recibido !in -60_000L..VIGENCIA_PEDIDO_MS) {
            f.delete()
            return null
        }
        return p
    }

    fun guardarPedido(p: PedidoInicio?) {
        if (p == null) {
            archivoPedido.delete()
        } else {
            raiz.parentFile?.mkdirs()
            escribirAtomico(archivoPedido, JsonWatch.encodeToString(PedidoInicio.serializer(), p))
        }
    }

    companion object {
        const val NOMBRE_AUDIO = "audio.ogg"

        /** Un pedido de "tocá para grabar" vale esto: lo que tarda el usuario en llevarle el reloj al profe. */
        const val VIGENCIA_PEDIDO_MS = 20 * 60_000L

        fun de(context: Context) = AlmacenWatch(File(context.filesDir, "grabaciones"))

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
