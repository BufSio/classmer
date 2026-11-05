package ar.classmer.grabacion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayOutputStream
import java.io.File

class OggDuracionTest {
    @get:Rule
    val tmp = TemporaryFolder()

    /** Una página Ogg con un solo paquete, como las que escribe el OggWriter de Android. */
    private fun pagina(granule: Long, cuerpo: ByteArray): ByteArray {
        val out = ByteArrayOutputStream()
        out.write("OggS".toByteArray())
        out.write(0) // versión
        out.write(0) // flags
        for (i in 0 until 8) out.write(((granule ushr (8 * i)) and 0xFF).toInt())
        repeat(4) { out.write(0x11) } // serial
        repeat(4) { out.write(0) } // secuencia
        repeat(4) { out.write(0) } // CRC (no se verifica)
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

    private fun archivo(vararg partes: ByteArray): File {
        val f = tmp.newFile()
        f.writeBytes(partes.fold(ByteArray(0)) { acc, p -> acc + p })
        return f
    }

    @Test
    fun `duracion desde el granulepos de la ultima pagina menos el pre-skip`() {
        val paginas = (1..250).map { pagina(960L * it, ByteArray(80) { 7 }) }.toTypedArray()
        val f = archivo(pagina(0, opusHead(312)), pagina(0, "OpusTags".toByteArray()), *paginas)
        // 250 paquetes de 20 ms = 5 s, menos 312 muestras de pre-skip (6,5 ms).
        assertEquals((240_000L - 312) * 1000 / 48_000, OggDuracion.leerMs(f))
    }

    @Test
    fun `ignora una ultima pagina cortada a la mitad`() {
        val completa = pagina(960L * 100, ByteArray(80))
        val cortada = pagina(960L * 101, ByteArray(80)).copyOf(40)
        val f = archivo(pagina(0, opusHead(0)), completa, cortada)
        assertEquals(2_000L, OggDuracion.leerMs(f))
    }

    @Test
    fun `archivo vacio o que no es Ogg Opus`() {
        assertNull(OggDuracion.leerMs(tmp.newFile()))
        assertNull(OggDuracion.leerMs(archivo(ByteArray(100) { 1 })))
        assertNull(OggDuracion.leerMs(File(tmp.root, "no-existe.ogg")))
    }
}
