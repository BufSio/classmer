package ar.classmer.watch

import java.io.ByteArrayOutputStream

/** Un Ogg/Opus mínimo que [ar.classmer.grabacion.OggDuracion] puede medir (páginas de 20 ms, sin CRC). */
object OggDePrueba {
    fun bytes(paquetes: Int, preSkip: Int = 0): ByteArray {
        val out = ByteArrayOutputStream()
        out.write(pagina(0, opusHead(preSkip)))
        out.write(pagina(0, "OpusTags".toByteArray()))
        for (i in 1..paquetes) out.write(pagina(960L * i, ByteArray(80) { i.toByte() }))
        return out.toByteArray()
    }

    /** Lo que dura [bytes] con [paquetes] de 20 ms y sin pre-skip. */
    fun duracionMs(paquetes: Int): Long = paquetes * 20L

    private fun pagina(granule: Long, cuerpo: ByteArray): ByteArray {
        val out = ByteArrayOutputStream()
        out.write("OggS".toByteArray())
        out.write(0)
        out.write(0)
        for (i in 0 until 8) out.write(((granule ushr (8 * i)) and 0xFF).toInt())
        repeat(4) { out.write(0x11) }
        repeat(8) { out.write(0) }
        val lacing = mutableListOf<Int>()
        var resto = cuerpo.size
        while (resto >= 255) {
            lacing += 255
            resto -= 255
        }
        lacing += resto
        out.write(lacing.size)
        lacing.forEach { out.write(it) }
        out.write(cuerpo)
        return out.toByteArray()
    }

    private fun opusHead(preSkip: Int): ByteArray {
        val b = ByteArray(19)
        "OpusHead".toByteArray().copyInto(b)
        b[8] = 1
        b[9] = 1
        b[10] = (preSkip and 0xFF).toByte()
        b[11] = (preSkip shr 8).toByte()
        return b
    }
}
