package ar.classmer.watchlink

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.EOFException
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.io.RandomAccessFile
import java.util.zip.CRC32

/**
 * Formato del canal de audio (reloj → celu). Cada trama lleva su posición en el archivo, así el
 * celu sabe exactamente dónde va cada byte aunque el canal se corte o llegue algo repetido:
 *
 * ```
 * trama := MAGIA (int32) | offset (int64) | largo (int32) | datos[largo] | crc32(offset + datos) (int32)
 * fin   := MAGIA (int32) | offset (int64) = hasta dónde se mandó | FIN (int32)
 * ```
 *
 * Sin la trama de fin, el envío se cortó (el canal se cierra si se corta la conexión).
 */
object Tramas {
    const val MAGIA = 0x434D4131 // "CMA1"
    const val FIN = -1

    /** 64 KiB: unos 12 s de audio a 32 kbps. Si se corta, se pierde poco. */
    const val MAX_DATOS = 64 * 1024

    fun crc(offset: Long, datos: ByteArray, n: Int): Int {
        val c = CRC32()
        for (k in 7 downTo 0) c.update((offset ushr (8 * k)).toInt() and 0xFF)
        c.update(datos, 0, n)
        return c.value.toInt()
    }
}

/** Lado del reloj: manda los bytes `[desde, hasta)` del archivo en tramas y cierra con la trama de fin. */
object EmisorAudio {
    /** Devuelve cuántos bytes mandó. Un error de escritura (canal cerrado) se propaga. */
    fun enviar(archivo: File, desde: Long, hasta: Long, salida: OutputStream, tamTrama: Int = Tramas.MAX_DATOS): Long {
        require(desde in 0..hasta) { "Rango inválido: [$desde, $hasta)" }
        require(tamTrama in 1..Tramas.MAX_DATOS)
        val out = DataOutputStream(BufferedOutputStream(salida, tamTrama + 32))
        RandomAccessFile(archivo, "r").use { raf ->
            raf.seek(desde)
            val buffer = ByteArray(tamTrama)
            var pos = desde
            while (pos < hasta) {
                val n = raf.read(buffer, 0, minOf(tamTrama.toLong(), hasta - pos).toInt())
                if (n < 0) throw IOException("El audio tiene menos de $hasta bytes")
                out.writeInt(Tramas.MAGIA)
                out.writeLong(pos)
                out.writeInt(n)
                out.write(buffer, 0, n)
                out.writeInt(Tramas.crc(pos, buffer, n))
                pos += n
            }
            out.writeInt(Tramas.MAGIA)
            out.writeLong(pos)
            out.writeInt(Tramas.FIN)
            out.flush()
        }
        return hasta - desde
    }
}

/** Cómo terminó una recepción. */
enum class FinRecepcion {
    /** Llegó la trama de fin: se recibió todo lo que el reloj tenía para mandar. */
    COMPLETA,

    /** El canal se cerró antes del fin (se cortó la conexión): se vuelve a pedir desde lo que hay. */
    CORTADA,

    /** Una trama llegó dañada (CRC): no se guardó y se vuelve a pedir desde ahí. */
    DANADA,

    /** El reloj mandó algo que no sigue el formato, o con un hueco: se vuelve a pedir desde lo que hay. */
    DESFASADA,

    /** Un tramo repetido no coincide con lo que ya tiene el celu: la copia local no sirve (hay que bajarla entera). */
    DISTINTA,
}

data class Recepcion(
    val fin: FinRecepcion,
    /** Bytes agregados al archivo local en esta recepción. */
    val agregados: Long,
    /** Largo del archivo local al terminar. */
    val largo: Long,
    /** Bytes que llegaron repetidos (ya estaban) y se descartaron. */
    val repetidos: Long = 0,
    val detalle: String? = null,
)

/**
 * Lado del celu: agrega al final de [destino] lo que llega del reloj, trama por trama. Una trama sólo
 * se guarda si está entera y su CRC coincide, y siempre en su posición: lo repetido se descarta
 * (después de comprobar que coincide con lo que ya está), y un hueco corta la recepción.
 *
 * Nunca reescribe lo que ya está en el archivo, así el que lo sube al server puede leerlo mientras
 * crece (igual que el Ogg que escribe MediaRecorder).
 */
class ReceptorAudio(private val destino: File, private val syncCada: Long = 1L shl 20) {

    fun recibir(entrada: InputStream): Recepcion {
        val din = DataInputStream(BufferedInputStream(entrada, Tramas.MAX_DATOS + 32))
        return FileOutputStream(destino, true).use { out -> recibirEn(din, out) }
    }

    private fun recibirEn(din: DataInputStream, out: FileOutputStream): Recepcion {
        val inicial = out.channel.size()
        var largo = inicial
        var repetidos = 0L
        var sinSync = 0L
        fun resultado(fin: FinRecepcion, detalle: String? = null): Recepcion {
            if (sinSync > 0) out.fd.sync()
            return Recepcion(fin, largo - inicial, largo, repetidos, detalle)
        }
        val buffer = ByteArray(Tramas.MAX_DATOS)
        while (true) {
            val offset: Long
            val n: Int
            try {
                val magia = din.readInt()
                if (magia != Tramas.MAGIA) return resultado(FinRecepcion.DESFASADA, "marca de trama inválida")
                offset = din.readLong()
                n = din.readInt()
                if (n == Tramas.FIN) return resultado(FinRecepcion.COMPLETA)
                if (n !in 1..Tramas.MAX_DATOS) return resultado(FinRecepcion.DESFASADA, "largo de trama inválido: $n")
                din.readFully(buffer, 0, n)
                if (din.readInt() != Tramas.crc(offset, buffer, n)) return resultado(FinRecepcion.DANADA, "CRC en $offset")
            } catch (e: EOFException) {
                return resultado(FinRecepcion.CORTADA)
            } catch (e: IOException) {
                return resultado(FinRecepcion.CORTADA, e.message ?: e.javaClass.simpleName)
            }
            if (offset > largo) return resultado(FinRecepcion.DESFASADA, "hueco: llegó $offset y hay $largo")
            // Lo que ya estaba tiene que coincidir: si no, la copia del celu y la del reloj difieren.
            val ya = minOf(n.toLong(), largo - offset).toInt()
            if (ya > 0 && !coincide(offset, buffer, ya)) return resultado(FinRecepcion.DISTINTA, "difiere en [$offset, ${offset + ya})")
            repetidos += ya
            if (ya < n) {
                out.write(buffer, ya, n - ya)
                largo += n - ya
                sinSync += n - ya
                if (sinSync >= syncCada) {
                    out.fd.sync()
                    sinSync = 0
                }
            }
        }
    }

    private fun coincide(offset: Long, datos: ByteArray, n: Int): Boolean {
        val local = ByteArray(n)
        RandomAccessFile(destino, "r").use { raf ->
            raf.seek(offset)
            raf.readFully(local)
        }
        for (i in 0 until n) if (local[i] != datos[i]) return false
        return true
    }
}
