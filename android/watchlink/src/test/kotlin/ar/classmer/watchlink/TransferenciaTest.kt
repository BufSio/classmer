package ar.classmer.watchlink

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import kotlin.random.Random

class TransferenciaTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val rnd = Random(20261022)

    /** El Ogg del reloj (crece mientras graba) y la copia del celu. */
    private lateinit var watch: File
    private lateinit var celu: File

    private fun preparar(bytes: Int = 0): ByteArray {
        watch = tmp.newFile("watch.ogg")
        celu = File(tmp.root, "celu.ogg")
        return grabar(bytes)
    }

    private fun grabar(n: Int): ByteArray {
        val nuevos = rnd.nextBytes(n)
        FileOutputStream(watch, true).use { it.write(nuevos) }
        return nuevos
    }

    /** Lo que manda el reloj para un pedido desde [desde]: los bytes del canal, enteros. */
    private fun canal(desde: Long, hasta: Long = watch.length(), tamTrama: Int = 1000): ByteArray {
        val out = ByteArrayOutputStream()
        EmisorAudio.enviar(watch, desde, hasta, out, tamTrama)
        return out.toByteArray()
    }

    private fun recibir(bytes: ByteArray) = ReceptorAudio(celu).recibir(ByteArrayInputStream(bytes))

    private fun igual() = assertArrayEquals(watch.readBytes(), celu.readBytes())

    @Test
    fun `una pasada entera llega completa y sin repetidos`() {
        preparar(10_500)
        val r = recibir(canal(0))
        assertEquals(FinRecepcion.COMPLETA, r.fin)
        assertEquals(10_500, r.agregados)
        assertEquals(0, r.repetidos)
        igual()
    }

    @Test
    fun `si el canal se corta a mitad de una trama se guarda hasta la ultima trama entera`() {
        preparar(10_000)
        val entero = canal(0)
        // 3 tramas de 1000 bytes más la mitad de la cuarta (cada trama tiene 20 bytes de encabezado y CRC).
        val r = recibir(entero.copyOf(3 * 1020 + 500))
        assertEquals(FinRecepcion.CORTADA, r.fin)
        assertEquals(3000, celu.length())
        // Se retoma desde lo que tiene el celu.
        val r2 = recibir(canal(celu.length()))
        assertEquals(FinRecepcion.COMPLETA, r2.fin)
        assertEquals(7000, r2.agregados)
        igual()
    }

    @Test
    fun `un canal que tira IOException cuenta como corte`() {
        preparar(5_000)
        val datos = canal(0)
        val rota = object : InputStream() {
            var i = 0
            override fun read(): Int {
                if (i >= 2_500) throw IOException("Channel closed")
                return datos[i++].toInt() and 0xFF
            }
        }
        val r = ReceptorAudio(celu).recibir(rota)
        assertEquals(FinRecepcion.CORTADA, r.fin)
        assertEquals("Channel closed", r.detalle)
        assertEquals(2000, celu.length())
    }

    @Test
    fun `lo repetido se descarta y se agrega solo lo nuevo`() {
        preparar(4_000)
        recibir(canal(0, 2_500))
        // El reloj vuelve a mandar desde antes (por ejemplo, dos pedidos que se cruzaron).
        val r = recibir(canal(1_000))
        assertEquals(FinRecepcion.COMPLETA, r.fin)
        assertEquals(1_500, r.repetidos)
        assertEquals(1_500, r.agregados)
        igual()
        // Todo repetido: no cambia nada.
        val r3 = recibir(canal(0))
        assertEquals(0, r3.agregados)
        assertEquals(4_000, r3.repetidos)
        igual()
    }

    @Test
    fun `una trama danada no se guarda y se vuelve a pedir desde ahi`() {
        preparar(5_000)
        val datos = canal(0)
        // Un byte cambiado en los datos de la segunda trama.
        datos[1020 + 16 + 10] = (datos[1020 + 16 + 10].toInt() xor 0x01).toByte()
        val r = recibir(datos)
        assertEquals(FinRecepcion.DANADA, r.fin)
        assertEquals(1000, celu.length())
        assertEquals(FinRecepcion.COMPLETA, recibir(canal(celu.length())).fin)
        igual()
    }

    @Test
    fun `un hueco corta la recepcion sin escribir nada`() {
        preparar(5_000)
        recibir(canal(0, 1_000))
        val r = recibir(canal(2_000))
        assertEquals(FinRecepcion.DESFASADA, r.fin)
        assertEquals(1_000, celu.length())
    }

    @Test
    fun `basura en vez de tramas corta la recepcion`() {
        preparar(1_000)
        val r = recibir("no es una trama".toByteArray())
        assertEquals(FinRecepcion.DESFASADA, r.fin)
        assertEquals(0, celu.length())
    }

    @Test
    fun `si lo repetido no coincide con la copia del celu se avisa que difiere`() {
        preparar(3_000)
        recibir(canal(0, 2_000))
        // La copia del celu se dañó en el disco.
        val local = celu.readBytes()
        local[1_500] = (local[1_500].toInt() xor 0xFF).toByte()
        celu.writeBytes(local)
        val r = recibir(canal(0))
        assertEquals(FinRecepcion.DISTINTA, r.fin)
        assertEquals(2_000, celu.length())
    }

    @Test
    fun `mientras graba se va trayendo lo nuevo y al terminar se verifica la huella`() {
        preparar(0)
        var g = grabacion(EstadoGrabacionWatch.GRABANDO)
        repeat(20) {
            grabar(rnd.nextInt(500, 6_000))
            g = g.copy(bytes = watch.length())
            when (val paso = PlanTransferencia.decidir(celu.length(), g)) {
                is PlanTransferencia.Paso.Pedir -> recibir(canal(paso.desde, g.bytes))
                else -> error("esperaba Pedir y salió $paso")
            }
        }
        assertEquals(PlanTransferencia.Paso.Nada, PlanTransferencia.decidir(celu.length(), g))
        // Terminó: el reloj informa largo y huella.
        g = g.copy(estado = EstadoGrabacionWatch.TERMINADA, bytes = watch.length(), sha256 = Huella.sha256(watch))
        assertEquals(PlanTransferencia.Paso.Verificar, PlanTransferencia.decidir(celu.length(), g))
        assertTrue(PlanTransferencia.verificada(celu.length(), Huella.sha256(celu), g))
    }

    @Test
    fun `con cortes, repetidos y tramas danadas al azar el audio llega igual y la huella coincide`() {
        preparar(0)
        var g = grabacion(EstadoGrabacionWatch.GRABANDO)
        var pedidos = 0
        var cortes = 0
        var danadas = 0
        fun traer() {
            while (true) {
                val paso = PlanTransferencia.decidir(celu.length(), g)
                if (paso !is PlanTransferencia.Paso.Pedir) return
                pedidos++
                // A veces el reloj manda desde antes de lo pedido (repetidos).
                val desde = if (rnd.nextInt(4) == 0) (paso.desde - rnd.nextLong(0, 5_000)).coerceAtLeast(0) else paso.desde
                var datos = canal(desde, g.bytes, tamTrama = rnd.nextInt(200, 4_000))
                when (rnd.nextInt(5)) {
                    0 -> {
                        datos = datos.copyOf(rnd.nextInt(datos.size))
                        cortes++
                    }
                    1 -> if (datos.size > 40) {
                        val i = rnd.nextInt(16, datos.size - 4)
                        datos[i] = (datos[i].toInt() xor 0x40).toByte()
                        danadas++
                    }
                }
                val r = recibir(datos)
                assertFalse("nunca debería diferir: ${r.detalle}", r.fin == FinRecepcion.DISTINTA)
                // Lo guardado es siempre un prefijo exacto del audio del reloj.
                val local = celu.readBytes()
                assertArrayEquals(watch.readBytes().copyOf(local.size), local)
            }
        }
        repeat(40) {
            grabar(rnd.nextInt(100, 20_000))
            g = g.copy(bytes = watch.length())
            traer()
        }
        g = g.copy(estado = EstadoGrabacionWatch.TERMINADA, bytes = watch.length(), sha256 = Huella.sha256(watch))
        traer()
        assertEquals(PlanTransferencia.Paso.Verificar, PlanTransferencia.decidir(celu.length(), g))
        assertTrue(PlanTransferencia.verificada(celu.length(), Huella.sha256(celu), g))
        igual()
        assertTrue("la prueba tiene que haber cortado y dañado algo ($cortes, $danadas)", cortes > 3 && danadas > 3)
        assertTrue(pedidos > 40)
    }

    @Test
    fun `si la huella final no coincide hay que bajarla de nuevo`() {
        preparar(2_000)
        recibir(canal(0))
        val g = grabacion(EstadoGrabacionWatch.TERMINADA).copy(bytes = 2_000, sha256 = "0".repeat(64))
        assertEquals(PlanTransferencia.Paso.Verificar, PlanTransferencia.decidir(celu.length(), g))
        assertFalse(PlanTransferencia.verificada(celu.length(), Huella.sha256(celu), g))
        // Si el celu tiene más que el archivo final del reloj, su copia tampoco sirve.
        val corta = g.copy(bytes = 1_500)
        assertEquals(PlanTransferencia.Paso.Rebajar, PlanTransferencia.decidir(celu.length(), corta))
    }

    @Test
    fun `terminada sin huella todavia espera`() {
        preparar(1_000)
        recibir(canal(0))
        val g = grabacion(EstadoGrabacionWatch.TERMINADA).copy(bytes = 1_000, sha256 = null)
        assertEquals(PlanTransferencia.Paso.Nada, PlanTransferencia.decidir(celu.length(), g))
    }

    @Test
    fun `la ruta del canal lleva el id y desde donde`() {
        val id = IdGrabacion.nuevo()
        assertEquals(id to 12_345L, Rutas.leerAudio(Rutas.audio(id, 12_345)))
        assertEquals(null, Rutas.leerAudio("/classmer/audio/$id"))
        assertEquals(null, Rutas.leerAudio("/classmer/audio/$id/-3"))
        assertEquals(null, Rutas.leerAudio("/classmer/estado"))
        assertTrue(IdGrabacion.valido(id))
        assertFalse(IdGrabacion.valido("../../x"))
    }

    private fun grabacion(estado: String) = GrabacionWatch(
        id = "01a15a3a-0000-7000-8000-000000000000", estado = estado, origen = OrigenGrabacion.CELU,
        inicio = "2026-10-22T14:00:00-03:00", bytes = 0, acumuladoMs = 0,
    )
}
