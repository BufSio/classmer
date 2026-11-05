package ar.classmer.grabacion

import java.io.File
import java.io.RandomAccessFile

/**
 * Lee la duración de un Ogg/Opus a partir del `granulepos` de la última página completa.
 * Sirve para cerrar una sesión que quedó a medias (la app se cayó grabando): el audio hasta el
 * corte es válido aunque la última página haya quedado incompleta.
 */
object OggDuracion {
    private val CAPTURA = byteArrayOf('O'.code.toByte(), 'g'.code.toByte(), 'g'.code.toByte(), 'S'.code.toByte())
    private const val OPUS_HZ = 48_000L

    /** Duración en ms, o `null` si el archivo no tiene páginas Ogg/Opus legibles. */
    fun leerMs(archivo: File): Long? {
        if (!archivo.isFile || archivo.length() < 47) return null
        RandomAccessFile(archivo, "r").use { raf ->
            val preSkip = leerPreSkip(raf) ?: return null
            val largo = raf.length()
            // Se busca hacia atrás la última página cuyo cuerpo esté entero en el archivo.
            val ventana = minOf(largo, 256L * 1024)
            val buffer = ByteArray(ventana.toInt())
            raf.seek(largo - ventana)
            raf.readFully(buffer)
            var i = buffer.size - 27
            while (i >= 0) {
                if (esCaptura(buffer, i)) {
                    val granule = leerLong(buffer, i + 6)
                    val segmentos = buffer[i + 26].toInt() and 0xFF
                    if (i + 27 + segmentos <= buffer.size && granule >= 0) {
                        var cuerpo = 0
                        for (s in 0 until segmentos) cuerpo += buffer[i + 27 + s].toInt() and 0xFF
                        if (i + 27 + segmentos + cuerpo <= buffer.size) {
                            return ((granule - preSkip).coerceAtLeast(0) * 1000) / OPUS_HZ
                        }
                    }
                }
                i--
            }
        }
        return null
    }

    /** El pre-skip está en el encabezado `OpusHead` (bytes 10-11 del primer paquete). */
    private fun leerPreSkip(raf: RandomAccessFile): Long? {
        val inicio = ByteArray(minOf(raf.length(), 512L).toInt())
        raf.seek(0)
        raf.readFully(inicio)
        if (!esCaptura(inicio, 0)) return null
        val segmentos = inicio[26].toInt() and 0xFF
        val p = 27 + segmentos
        if (p + 12 > inicio.size) return null
        val firma = String(inicio, p, 8, Charsets.US_ASCII)
        if (firma != "OpusHead") return null
        return ((inicio[p + 10].toInt() and 0xFF) or ((inicio[p + 11].toInt() and 0xFF) shl 8)).toLong()
    }

    private fun esCaptura(b: ByteArray, i: Int) =
        i + 4 <= b.size && b[i] == CAPTURA[0] && b[i + 1] == CAPTURA[1] && b[i + 2] == CAPTURA[2] && b[i + 3] == CAPTURA[3]

    private fun leerLong(b: ByteArray, i: Int): Long {
        var v = 0L
        for (k in 7 downTo 0) v = (v shl 8) or (b[i + k].toLong() and 0xFF)
        return v
    }
}
