package ar.classmer.datos

import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.EstadoSubidaAdjuntos
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.SubidaAdjunto
import ar.classmer.util.JsonClassmer
import ar.classmer.util.Uuid7
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import kotlin.random.Random

class AlmacenAdjuntosTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val sesion = "01a137d8-617d-718c-a700-3328dc9a1fec"
    private val creado = "2026-10-22T15:03:10-03:00"

    private fun almacen(maximo: Long = AlmacenAdjuntos.MAX_ADJUNTO) = AlmacenAdjuntos(File(tmp.root, "adjuntos"), maximo)

    private fun pdf(n: Int = 5000) = "%PDF-1.7\n%âãÏÓ\n".toByteArray(Charsets.ISO_8859_1) + Random.nextBytes(n)

    private fun bytes(vararg b: Int) = ByteArray(b.size) { b[it].toByte() }

    /** Lo que queda en la carpeta de la clase. */
    private fun archivos(a: AlmacenAdjuntos) = a.dir(sesion).list()?.sorted() ?: emptyList()

    private fun esperarInvalido(bloque: () -> Unit): AdjuntoInvalido {
        try {
            bloque()
        } catch (e: AdjuntoInvalido) {
            return e
        }
        fail("tenía que rechazarlo")
        throw AssertionError()
    }

    @Test
    fun `copia el archivo entero con sus datos y sin temporales`() {
        val a = almacen()
        val datos = pdf()

        val adjunto = a.copiar(sesion, ByteArrayInputStream(datos), "Slides clase 5.pdf", 2_040_000, creado)

        assertEquals(sesion, adjunto.sesionId)
        assertEquals("Slides clase 5.pdf", adjunto.nombreOriginal)
        assertEquals("application/pdf", adjunto.mime)
        assertEquals(datos.size.toLong(), adjunto.bytes)
        assertEquals(2_040_000L, adjunto.offsetMs)
        assertEquals(creado, adjunto.creado)
        assertEquals("${adjunto.id}.pdf", adjunto.archivo)
        assertArrayEquals(datos, a.archivo(adjunto).readBytes())
        assertEquals(listOf("${adjunto.id}.json", "${adjunto.id}.pdf"), archivos(a))
        assertEquals(listOf(adjunto), a.listar(sesion))

        val json = a.archivoDatos(sesion, adjunto.id).readText()
        assertTrue(json, json.contains("\"sesion_id\":\"$sesion\""))
        assertTrue(json.contains("\"nombre_original\":\"Slides clase 5.pdf\""))
        assertTrue(json.contains("\"offset_ms\":2040000"))
    }

    @Test
    fun `sin minuto, offset_ms no va en el json`() {
        val a = almacen()
        val adjunto = a.copiar(sesion, ByteArrayInputStream(pdf()), "Notas.pdf", null, creado)
        val json = a.archivoDatos(sesion, adjunto.id).readText()
        assertFalse(json, json.contains("offset_ms"))
        assertNull(a.listar(sesion).single().offsetMs)
    }

    @Test
    fun `el id es un UUIDv7 nuevo`() {
        val a = almacen()
        val uno = a.copiar(sesion, ByteArrayInputStream(pdf()), "a.pdf", null, creado)
        val otro = a.copiar(sesion, ByteArrayInputStream(pdf()), "a.pdf", null, creado)
        assertTrue(uno.id != otro.id)
        listOf(uno, otro).forEach {
            assertTrue(it.id, Regex("[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}").matches(it.id))
        }
    }

    @Test
    fun `reconoce el tipo por el contenido, no por el nombre`() {
        val a = almacen()
        val casos = listOf(
            Triple("foto.pdf", bytes(0xFF, 0xD8, 0xFF, 0xE0) + Random.nextBytes(100), "image/jpeg" to "jpg"),
            Triple("captura", bytes(0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A) + Random.nextBytes(100), "image/png" to "png"),
            Triple("x.webp", "RIFF".toByteArray() + bytes(1, 2, 3, 4) + "WEBPVP8 ".toByteArray() + Random.nextBytes(100), "image/webp" to "webp"),
            Triple("IMG_1234.HEIC", bytes(0, 0, 0, 0x18) + "ftypheic".toByteArray() + bytes(0, 0, 0, 0) + "mif1heic".toByteArray(), "image/heic" to "heic"),
            Triple("x.heif", bytes(0, 0, 0, 0x18) + "ftypmif1".toByteArray() + bytes(0, 0, 0, 0) + "mif1heic".toByteArray(), "image/heif" to "heic"),
            // El "%PDF-" puede venir después de basura, mientras esté en los primeros 1024 bytes.
            Triple("raro.bin", ByteArray(1000) { ' '.code.toByte() } + "%PDF-1.4".toByteArray() + Random.nextBytes(100), "application/pdf" to "pdf"),
        )
        for ((nombre, datos, esperado) in casos) {
            val adjunto = a.copiar(sesion, ByteArrayInputStream(datos), nombre, null, creado)
            assertEquals(nombre, esperado.first, adjunto.mime)
            assertTrue(nombre, adjunto.archivo.endsWith(".${esperado.second}"))
            assertEquals("el nombre original queda como dato", nombre, adjunto.nombreOriginal)
        }
        assertEquals(casos.size, a.listar(sesion).size)
    }

    @Test
    fun `rechaza lo que no es PDF ni imagen sin dejar nada`() {
        val a = almacen()
        val malos = mapOf(
            "notas.txt" to "Hola, esto es texto".toByteArray(),
            "tarde.pdf" to ByteArray(1020) { ' '.code.toByte() } + "%PDF-1.4".toByteArray(),
            "foto.avif" to bytes(0, 0, 0, 0x1C) + "ftypavif".toByteArray() + bytes(0, 0, 0, 0) + "avifmif1miaf".toByteArray(),
            "corto.jpg" to bytes(0xFF, 0xD8),
        )
        for ((nombre, datos) in malos) {
            val e = esperarInvalido { a.copiar(sesion, ByteArrayInputStream(datos), nombre, null, creado) }
            assertTrue(e.message, e.message!!.contains(nombre) && e.message!!.contains("no es un PDF"))
        }
        val vacio = esperarInvalido { a.copiar(sesion, ByteArrayInputStream(ByteArray(0)), "vacío.pdf", null, creado) }
        assertTrue(vacio.message!!.contains("vacío"))
        assertEquals(emptyList<String>(), archivos(a))
        assertEquals(emptyList<AdjuntoLocal>(), a.listar(sesion))
    }

    @Test
    fun `mas del tope declarado se rechaza sin leer nada`() {
        val a = almacen(maximo = 10_000)
        var leyo = false
        val origen = object : InputStream() {
            override fun read(): Int {
                leyo = true
                return -1
            }
        }
        val e = esperarInvalido { a.copiar(sesion, origen, "Libro.pdf", null, creado, tamanioDeclarado = 10_001) }
        assertTrue(e.message, e.message!!.contains("pesa más de"))
        assertFalse(leyo)
        assertEquals(emptyList<String>(), archivos(a))
    }

    @Test
    fun `si el archivo es mas grande de lo que dijo el selector, corta la copia`() {
        val a = almacen(maximo = 10_000)
        val e = esperarInvalido { a.copiar(sesion, ByteArrayInputStream(pdf(20_000)), "Libro.pdf", null, creado, tamanioDeclarado = 900) }
        assertTrue(e.message!!.contains("Libro.pdf pesa más de"))
        assertEquals("no queda ni el temporal", emptyList<String>(), archivos(a))

        // Justo en el tope sí entra.
        val justo = "%PDF-".toByteArray() + Random.nextBytes(10_000 - 5)
        val adjunto = a.copiar(sesion, ByteArrayInputStream(justo), "Justo.pdf", null, creado)
        assertEquals(10_000L, adjunto.bytes)
    }

    @Test
    fun `una copia que falla a mitad no deja un adjunto a medias`() {
        val a = almacen()
        val datos = pdf(600_000)
        val roto = object : InputStream() {
            var i = 0
            override fun read(): Int = if (i < 300_000) datos[i++].toInt() and 0xFF else throw IOException("se desconectó la tarjeta")
            override fun read(b: ByteArray, off: Int, len: Int): Int {
                if (i >= 300_000) throw IOException("se desconectó la tarjeta")
                val n = minOf(len, 300_000 - i)
                System.arraycopy(datos, i, b, off, n)
                i += n
                return n
            }
        }
        try {
            a.copiar(sesion, roto, "Slides.pdf", null, creado)
            fail("tenía que fallar")
        } catch (e: IOException) {
            assertEquals("se desconectó la tarjeta", e.message)
        }
        assertEquals(emptyList<String>(), archivos(a))
        assertEquals(emptyList<AdjuntoLocal>(), a.listar(sesion))
    }

    @Test
    fun `listar ignora los datos rotos, los que no tienen archivo y los temporales`() {
        val a = almacen()
        val viejo = a.copiar(sesion, ByteArrayInputStream(pdf()), "Primero.pdf", null, creado, id = Uuid7.nuevo(1_000_000))
        val nuevo = a.copiar(sesion, ByteArrayInputStream(pdf()), "Segundo.pdf", null, creado, id = Uuid7.nuevo(2_000_000))
        File(a.dir(sesion), "roto.json").writeText("{no es json")
        // Datos sin su archivo (no debería pasar: el json se escribe último).
        val huerfano = nuevo.copy(id = Uuid7.nuevo(3_000_000), archivo = "falta.pdf")
        a.archivoDatos(sesion, huerfano.id).writeText(JsonClassmer.encodeToString(AdjuntoLocal.serializer(), huerfano))
        File(a.dir(sesion), "x.pdf.tmp").writeBytes(pdf())
        a.guardarSubida(sesion, EstadoSubidaAdjuntos().con(viejo.id, SubidaAdjunto(subido = true)))

        assertEquals(listOf(viejo.id, nuevo.id), a.listar(sesion).map { it.id })
        assertEquals(emptyList<AdjuntoLocal>(), a.listar("otra-clase"))
    }

    @Test
    fun `subida json se escribe atomico y se relee`() {
        val a = almacen()
        assertEquals(EstadoSubidaAdjuntos(), a.leerSubida(sesion))
        a.guardarSubida(sesion, EstadoSubidaAdjuntos(noEncontrada = 2).con("x", SubidaAdjunto(fallos = 3, esperarHastaMs = 1234)))
        a.guardarSubida(sesion, a.leerSubida(sesion).con("y", SubidaAdjunto(subido = true)))
        val leida = a.leerSubida(sesion)
        assertEquals(3, leida.de("x").fallos)
        assertEquals(1234L, leida.de("x").esperarHastaMs)
        assertEquals(2, leida.noEncontrada)
        assertTrue(leida.de("y").subido)
        assertFalse(leida.de("z").subido)
        assertFalse(File(a.dir(sesion), "subida.json.tmp").exists())
    }

    @Test
    fun `los adjuntos no se mezclan con las sesiones`() {
        val raiz = tmp.root
        val sesiones = AlmacenSesiones(File(raiz, "sesiones"))
        val a = AlmacenAdjuntos(File(raiz, "adjuntos"))
        sesiones.crear(SesionLocal(sesion, null, "auto", "teorica", "2026-10-22T14:00:05-03:00", "s23-ultra"))
        a.copiar(sesion, ByteArrayInputStream(pdf()), "Slides.pdf", null, creado)
        a.copiar("solo-en-el-server", ByteArrayInputStream(pdf()), "Slides.pdf", null, creado)

        assertEquals(listOf(sesion), sesiones.listar().map { it.id })
        assertFalse(File(sesiones.dir(sesion), "adjuntos").exists())
        assertEquals(listOf(sesion, "solo-en-el-server").sorted(), a.sesiones())
    }

    // ---- Espacio libre ----

    private val mb = 1024L * 1024

    @Test
    fun `sin lugar en el celu no se copia nada`() {
        val a = AlmacenAdjuntos(File(tmp.root, "adjuntos"), espacioLibre = { 350 * mb })
        var leyo = 0
        val origen = object : InputStream() {
            val datos = pdf(100)
            override fun read(): Int = if (leyo < datos.size) datos[leyo++].toInt() and 0xFF else -1
        }
        // 350 MB libres - 60 MB < 300 MB de margen (lo que necesita la grabación).
        val e = esperarInvalido { a.copiar(sesion, origen, "Libro.pdf", null, creado, tamanioDeclarado = 60 * mb) }
        assertTrue(e.message, e.message!!.contains("No hay lugar en el celu para Libro.pdf"))
        assertTrue(e.message!!.contains("300 MB"))
        assertEquals(emptyList<String>(), archivos(a))

        // Uno chico sí entra.
        val chico = a.copiar(sesion, ByteArrayInputStream(pdf()), "Chico.pdf", null, creado, tamanioDeclarado = 5_000)
        assertEquals(listOf(chico.id), a.listar(sesion).map { it.id })
    }

    @Test
    fun `si el espacio se acaba copiando, corta la copia sin dejar nada`() {
        var libre = 2_000 * mb
        val a = AlmacenAdjuntos(File(tmp.root, "adjuntos"), espacioLibre = { libre })
        // El selector no informó el tamaño: se mira el espacio mientras copia.
        val origen = object : InputStream() {
            val cabecera = pdf(100)
            var leidos = 0L
            override fun read(): Int = throw UnsupportedOperationException()
            override fun read(b: ByteArray, off: Int, len: Int): Int {
                val n = minOf(len, 64 * 1024)
                for (i in 0 until n) b[off + i] = if (leidos + i < cabecera.size) cabecera[(leidos + i).toInt()] else 0
                leidos += n
                // Otra app (o la grabación) llena el disco a mitad de la copia.
                if (leidos > 4 * mb) libre = 250 * mb
                return n
            }
        }
        val e = esperarInvalido { a.copiar(sesion, origen, "Grande.pdf", null, creado) }
        assertTrue(e.message!!.contains("No hay lugar"))
        assertTrue("cortó antes del tope", origen.leidos < 20 * mb)
        assertEquals(emptyList<String>(), archivos(a))
    }

    @Test
    fun `si no se sabe el espacio libre, copia igual`() {
        val a = AlmacenAdjuntos(File(tmp.root, "adjuntos"), espacioLibre = { 0L })
        val adjunto = a.copiar(sesion, ByteArrayInputStream(pdf()), "Slides.pdf", null, creado, tamanioDeclarado = 50 * mb)
        assertEquals(listOf(adjunto.id), a.listar(sesion).map { it.id })
    }

    // ---- Restos de copias cortadas, copias pendientes, descartar ----

    @Test
    fun `limpia los restos viejos de una copia cortada y nada mas`() {
        val a = almacen()
        val bien = a.copiar(sesion, ByteArrayInputStream(pdf()), "Bien.pdf", null, creado)
        val d = a.dir(sesion)
        val hora = 60 * 60_000L
        val ahora = System.currentTimeMillis()
        fun viejo(nombre: String) = File(d, nombre).apply { writeBytes(pdf(10)); setLastModified(ahora - 2 * hora) }
        val tmpViejo = viejo("x.pdf.tmp")
        val sinDatos = viejo("01a137d8-1111-718c-a700-3328dc9a1fec.pdf")
        // Uno que tiene la copia anotada: lo retoma la copia, no se borra.
        val conCopia = viejo("01a137d8-2222-718c-a700-3328dc9a1fec.pdf")
        File(d, "01a137d8-2222-718c-a700-3328dc9a1fec.copiando").writeText("{}")
        val reciente = File(d, "01a137d8-3333-718c-a700-3328dc9a1fec.pdf").apply { writeBytes(pdf(10)) }
        File(a.archivo(bien).path).setLastModified(ahora - 2 * hora)
        a.archivoDatos(sesion, bien.id).setLastModified(ahora - 2 * hora)
        a.guardarSubida(sesion, EstadoSubidaAdjuntos())
        a.archivoSubida(sesion).setLastModified(ahora - 2 * hora)

        a.limpiar(sesion)

        assertFalse(tmpViejo.exists())
        assertFalse("archivo viejo sin su .json", sinDatos.exists())
        assertTrue(conCopia.exists())
        assertTrue("puede estar terminando de copiarse", reciente.exists())
        assertTrue(a.archivo(bien).exists())
        assertTrue(a.archivoDatos(sesion, bien.id).exists())
        assertTrue(a.archivoSubida(sesion).exists())
        assertEquals(listOf(bien.id), a.listar(sesion).map { it.id })
    }

    @Test
    fun `las copias pendientes se anotan, se listan y se olvidan`() {
        val a = almacen()
        val copia = ar.classmer.modelo.CopiaPendiente(
            id = Uuid7.nuevo(), sesionId = sesion, uri = "content://proveedor/doc/1",
            nombre = "Slides clase 5.pdf", tamanio = 1234, offsetMs = 60_000, creado = creado,
        )
        val otra = copia.copy(id = Uuid7.nuevo(), sesionId = "otra-clase", offsetMs = null)
        a.anotarCopia(copia)
        a.anotarCopia(otra)

        assertEquals(setOf(copia, otra), a.copiasPendientes().toSet())
        assertEquals("no es un adjunto todavía", emptyList<AdjuntoLocal>(), a.listar(sesion))

        a.olvidarCopia(sesion, copia.id)
        assertEquals(listOf(otra), a.copiasPendientes())
    }

    @Test
    fun `descartar borra el adjunto, su archivo y su pedido de reintento`() {
        val a = almacen()
        val queda = a.copiar(sesion, ByteArrayInputStream(pdf()), "Queda.pdf", null, creado)
        val sale = a.copiar(sesion, ByteArrayInputStream(pdf()), "Sale.pdf", null, creado)
        a.archivoPedidoReintento(sesion, sale.id).createNewFile()

        a.descartar(sesion, sale.id)

        assertEquals(listOf(queda.id), a.listar(sesion).map { it.id })
        assertFalse(a.archivo(sale).exists())
        assertFalse(a.archivoDatos(sesion, sale.id).exists())
        assertFalse(a.archivoPedidoReintento(sesion, sale.id).exists())
        assertTrue(a.archivo(queda).exists())
    }

    @Test
    fun `el soporte del server se guarda en la raiz y no es una clase`() {
        val a = almacen()
        assertFalse(a.leerSoporte().sinSoporte)
        a.guardarSoporte(ar.classmer.modelo.SoporteAdjuntos(sinSoporte = true, servidor = "http://pc:8000/", ultimoSondeoMs = 42))
        val leido = a.leerSoporte()
        assertTrue(leido.sinSoporte)
        assertEquals("http://pc:8000/", leido.servidor)
        assertEquals(42L, leido.ultimoSondeoMs)
        assertEquals(emptyList<String>(), a.sesiones())
    }
}
