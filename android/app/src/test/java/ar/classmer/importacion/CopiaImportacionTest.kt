package ar.classmer.importacion

import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.CopiaPendiente
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.TipoEvento
import ar.classmer.modelo.paraCrear
import ar.classmer.subida.PrimerPlano
import ar.classmer.ui.FilaSesion
import ar.classmer.ui.FilasImportacion
import ar.classmer.ui.Tono
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FileNotFoundException
import java.io.InputStream
import kotlin.random.Random

/** La cola de importación: la copia al celu, retomar después de un corte, las fotos sin minuto y el paso a terminada. */
class CopiaImportacionTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val almacen by lazy { AlmacenSesiones(tmp.newFolder("sesiones")) }
    private val id = "019a1b2c-3d4e-7f50-8a6b-7c8d9e0f1a2b"
    private val creado = "2026-10-24T18:00:00-03:00"

    /** Los "documentos" del selector: lo que devuelve abrir() para cada URI. */
    private val documentos = mutableMapOf<String, ByteArray>()

    /** Cada apertura: la URI y desde qué byte. */
    private val aperturas = mutableListOf<Pair<String, Long>>()

    /** Las URIs que "ya no se pueden leer" (se borraron o se perdió el permiso). */
    private val ilegibles = mutableSetOf<String>()

    private fun abrir(uri: String, desde: Long): InputStream {
        aperturas += uri to desde
        if (uri in ilegibles) throw SecurityException("Permission Denial: reading $uri")
        val datos = documentos[uri] ?: throw FileNotFoundException(uri)
        return ByteArrayInputStream(datos).also { it.skip(desde) }
    }

    private fun copia(
        seguir: () -> Boolean = { true },
        espacioLibre: (File) -> Long = { 0L },
        duracion: Long? = 5_400_000,
        confirmarCada: Long = 64 * 1024,
    ) = CopiaImportacion(
        almacen, ::abrir, medirDuracion = { duracion }, espacioLibre = espacioLibre,
        confirmarCada = confirmarCada, seguir = seguir,
    )

    private fun doc(uri: String, n: Int): ByteArray = Random.nextBytes(n).also { documentos[uri] = it }

    private fun pedido(
        audioBytes: Long?,
        fotos: List<DocumentoElegido> = emptyList(),
        materiaId: String? = "macro",
        numero: Int? = 7,
    ) = PedidoImportacion(
        audio = DocumentoElegido("content://audio", "PTT-20261022-WA0003.opus", audioBytes, "audio/ogg"),
        duracionMs = 5_000_000,
        fotos = fotos,
        materiaId = materiaId,
        nombreMateria = materiaId?.let { "Macro" },
        origenMateria = if (materiaId == null) "auto" else "calendario",
        tipoClase = "teorica",
        inicio = "2026-10-22T14:00:00-03:00",
        numeroClase = numero,
        eventoCalendario = "Macro (Teórica)",
        dispositivo = "s23-ultra",
    )

    private fun crear(p: PedidoImportacion): SesionLocal = Importador.crear(almacen, p, id = id, ahora = creado, espacioLibre = { 0L })

    // ---- Importador.crear ----

    @Test
    fun `crear deja la sesion importando, marcada importada y sin corte automatico`() {
        val s = crear(pedido(audioBytes = 1000))
        assertEquals(EstadoLocal.IMPORTANDO, s.estadoLocal)
        assertTrue(s.importada)
        assertEquals("audio.opus", s.archivoAudio)
        assertEquals("PTT-20261022-WA0003.opus", s.audioNombre)
        assertEquals(1000L, s.audioBytes)
        assertEquals(7, s.numeroClase)
        assertNull(s.preguntarCorteMs)
        assertNull(s.finEventoMs)
        assertEquals(s, almacen.leerSesion(id))
        val imp = almacen.leerImportacion(id)!!
        assertEquals("content://audio", imp.audio.uri)
        assertEquals("audio.opus", imp.audio.destino)
        assertEquals(creado, imp.creado)
        assertFalse(imp.audio.copiado)
        assertTrue(almacen.archivoEventos(id).exists())
    }

    @Test
    fun `en Auto no va el numero`() {
        val s = crear(pedido(audioBytes = 1000, materiaId = null, numero = 7))
        assertNull(s.materiaId)
        assertNull(s.numeroClase)
        assertNull(s.paraCrear().numeroClase)
    }

    @Test
    fun `mas de 2 GiB o sin lugar no se crea nada`() {
        try {
            Importador.crear(almacen, pedido(audioBytes = Importador.MAX_AUDIO + 1), id = id)
            fail("tenía que rechazarlo")
        } catch (e: ImportacionInvalida) {
            assertTrue(e.message, e.message!!.contains("2 GB"))
        }
        try {
            Importador.crear(almacen, pedido(audioBytes = 500L * 1024 * 1024), id = id, espacioLibre = { 600L * 1024 * 1024 })
            fail("tenía que rechazarlo")
        } catch (e: ImportacionInvalida) {
            assertTrue(e.message, e.message!!.contains("lugar"))
        }
        assertFalse(almacen.dir(id).exists())
    }

    @Test
    fun `la extension del audio sale del nombre, si no del tipo, y si no es bin`() {
        assertEquals("opus", Importador.extensionAudio("PTT-20261022-WA0003.opus", "audio/ogg"))
        assertEquals("m4a", Importador.extensionAudio("Clase 5.M4A", null))
        assertEquals("mp4", Importador.extensionAudio("VID-20261022-WA0001.mp4", "video/mp4"))
        assertEquals("ogg", Importador.extensionAudio(null, "audio/ogg; codecs=opus"))
        assertEquals("m4a", Importador.extensionAudio("grabacion", "audio/mp4"))
        assertEquals("bin", Importador.extensionAudio("algo.xyz", null))
        assertEquals("jpg", Importador.extensionFoto("IMG_1234.JPEG", null))
        assertEquals("heic", Importador.extensionFoto("IMG_1234.heif", null))
        assertEquals("png", Importador.extensionFoto(null, "image/png"))
        assertEquals("jpg", Importador.extensionFoto("foto", null))
    }

    // ---- La copia ----

    @Test
    fun `copia el audio y las fotos, registra las fotos sin minuto y pasa a terminada`() {
        val audio = doc("content://audio", 300_000)
        val f1 = doc("content://foto1", 4321)
        val f2 = doc("content://foto2", 1234)
        crear(
            pedido(
                audioBytes = audio.size.toLong(),
                fotos = listOf(
                    DocumentoElegido("content://foto1", "IMG_20261022_141500.jpg", f1.size.toLong(), "image/jpeg"),
                    DocumentoElegido("content://foto2", "pizarron.png", f2.size.toLong(), "image/png"),
                ),
            ),
        )

        assertEquals(CopiaImportacion.Resultado.TERMINADA, copia(duracion = 5_400_123).copiar(id))

        val s = almacen.leerSesion(id)!!
        assertEquals(EstadoLocal.TERMINADA, s.estadoLocal)
        assertEquals(5_400_123L, s.duracionMs)
        assertEquals(audio.size.toLong(), s.audioBytes)
        assertArrayEquals(audio, almacen.archivoAudio(s).readBytes())
        assertEquals("audio.opus", almacen.archivoAudio(s).name)
        assertFalse(File(almacen.dir(id), "audio.opus.parcial").exists())

        val imp = almacen.leerImportacion(id)!!
        assertTrue(imp.audio.copiado)
        assertTrue(imp.fotosRegistradas)
        val eventos = almacen.leerEventos(id)
        assertEquals(2, eventos.size)
        assertTrue(eventos.all { it.tipo == TipoEvento.FOTO && it.offsetMs == null && it.creado == creado })
        assertEquals(imp.fotos.map { it.id }, eventos.map { it.id })
        assertEquals(listOf("jpg", "png"), eventos.map { it.archivo!!.substringAfterLast('.') })
        assertArrayEquals(f1, File(almacen.dir(id), eventos[0].archivo!!).readBytes())
        assertArrayEquals(f2, File(almacen.dir(id), eventos[1].archivo!!).readBytes())
        // En eventos.jsonl la foto queda sin offset_ms.
        assertFalse(almacen.archivoEventos(id).readText().contains("offset_ms"))

        // Otra pasada no hace nada (ni duplica eventos).
        assertEquals(CopiaImportacion.Resultado.NADA, copia().copiar(id))
        assertEquals(2, almacen.leerEventos(id).size)
    }

    @Test
    fun `sin duracion medida queda la de cuando se eligio, y si tampoco hay, 0`() {
        doc("content://audio", 1000)
        crear(pedido(audioBytes = 1000))
        assertEquals(CopiaImportacion.Resultado.TERMINADA, copia(duracion = null).copiar(id))
        assertEquals(5_000_000L, almacen.leerSesion(id)!!.duracionMs)

        val otro = "019a1b2c-3d4e-7f50-8a6b-000000000002"
        Importador.crear(almacen, pedido(audioBytes = 1000).copy(duracionMs = null), id = otro, espacioLibre = { 0L })
        assertEquals(CopiaImportacion.Resultado.TERMINADA, copia(duracion = null).copiar(otro))
        assertEquals(0L, almacen.leerSesion(otro)!!.duracionMs)
    }

    @Test
    fun `si se corta, retoma desde lo confirmado sin repetir lo ya copiado`() {
        val audio = doc("content://audio", 1_000_000)
        crear(pedido(audioBytes = audio.size.toLong()))
        var lecturas = 0
        // Frena después de ~12 lecturas de 256 KiB... con tramos de confirmación de 64 KiB.
        val r = copia(seguir = { ++lecturas < 3 }).copiar(id)
        assertEquals(CopiaImportacion.Resultado.CORTADA, r)
        val imp = almacen.leerImportacion(id)!!
        assertFalse(imp.audio.copiado)
        assertTrue(imp.audio.confirmados > 0)
        val parcial = File(almacen.dir(id), "audio.opus.parcial")
        assertEquals(imp.audio.confirmados, parcial.length())
        assertEquals(EstadoLocal.IMPORTANDO, almacen.leerSesion(id)!!.estadoLocal)

        // Lo que se escribió después de la última confirmación pudo no llegar al disco: no cuenta.
        parcial.appendBytes(ByteArray(5000) { 7 })
        aperturas.clear()
        assertEquals(CopiaImportacion.Resultado.TERMINADA, copia().copiar(id))
        assertEquals(listOf("content://audio" to imp.audio.confirmados), aperturas)
        assertArrayEquals(audio, almacen.archivoAudio(almacen.leerSesion(id)!!).readBytes())
    }

    @Test
    fun `sin primer plano, la copia corta sola en el tope y sigue en el proximo intento`() = runTest {
        val audio = doc("content://audio", 1_000_000)
        crear(pedido(audioBytes = audio.size.toLong()))
        var ahora = 0L
        val primerPlano = PrimerPlano { ahora }
        assertFalse(primerPlano.pedir { throw IllegalStateException("startForegroundService() not allowed") })
        // WorkManager nunca avisa el fin de la ventana (isStopped sigue en false); cada lectura tarda 4 minutos.
        val seguir = {
            val sigue = primerPlano.dentroDelTope
            ahora += 4 * 60_000
            sigue
        }
        assertEquals(CopiaImportacion.Resultado.CORTADA, copia(seguir = seguir).copiar(id))
        val confirmados = almacen.leerImportacion(id)!!.audio.confirmados
        assertEquals(3L * 256 * 1024, confirmados)

        aperturas.clear()
        assertEquals(CopiaImportacion.Resultado.TERMINADA, copia().copiar(id))
        assertEquals(listOf("content://audio" to confirmados), aperturas)
        assertArrayEquals(audio, almacen.archivoAudio(almacen.leerSesion(id)!!).readBytes())
    }

    @Test
    fun `si al retomar el documento cambio de tamano, se copia entero de nuevo`() {
        val original = doc("content://audio", 400_000)
        crear(pedido(audioBytes = original.size.toLong()))
        var lecturas = 0
        assertEquals(CopiaImportacion.Resultado.CORTADA, copia(seguir = { ++lecturas < 2 }).copiar(id))
        // Entre un intento y otro, el documento pasó a ser otro (más largo).
        val nuevo = doc("content://audio", 500_000)
        assertEquals(CopiaImportacion.Resultado.TERMINADA, copia().copiar(id))
        assertArrayEquals(nuevo, almacen.archivoAudio(almacen.leerSesion(id)!!).readBytes())
        assertEquals(500_000L, almacen.leerSesion(id)!!.audioBytes)
    }

    @Test
    fun `un audio ilegible deja el error y la clase no pasa a terminada ni se reintenta sola`() {
        crear(pedido(audioBytes = 1000))
        ilegibles += "content://audio"

        assertEquals(CopiaImportacion.Resultado.ERROR, copia().copiar(id))
        val imp = almacen.leerImportacion(id)!!
        val error = imp.error
        assertNotNull(error)
        assertTrue(error, error!!.contains("No se puede leer el audio"))
        assertEquals(EstadoLocal.IMPORTANDO, almacen.leerSesion(id)!!.estadoLocal)
        assertTrue(almacen.leerEventos(id).isEmpty())

        // Sin "Reintentar", otra pasada (por ejemplo, al abrir la app) no lo vuelve a probar.
        aperturas.clear()
        assertEquals(CopiaImportacion.Resultado.NADA, copia().copiar(id))
        assertTrue(aperturas.isEmpty())

        // Con "Reintentar" (por ejemplo, el documento volvió a estar), se copia.
        ilegibles.clear()
        doc("content://audio", 1000)
        assertEquals(CopiaImportacion.Resultado.TERMINADA, copia().copiar(id, reintento = true))
        assertNull(almacen.leerImportacion(id)!!.error)
    }

    @Test
    fun `una foto ilegible o de mas de 50 MB queda afuera y la clase sigue`() {
        doc("content://audio", 2000)
        doc("content://foto-ok", 300)
        crear(
            pedido(
                audioBytes = 2000,
                fotos = listOf(
                    DocumentoElegido("content://foto-borrada", "borrada.jpg", 100, "image/jpeg"),
                    DocumentoElegido("content://foto-grande", "grande.jpg", Importador.MAX_FOTO + 1, "image/jpeg"),
                    DocumentoElegido("content://foto-ok", "ok.jpg", 300, "image/jpeg"),
                ),
            ),
        )
        ilegibles += "content://foto-borrada"

        assertEquals(CopiaImportacion.Resultado.TERMINADA, copia().copiar(id))
        val imp = almacen.leerImportacion(id)!!
        assertEquals(listOf(false, false, true), imp.fotos.map { it.copiado })
        assertEquals(2, imp.problemasFotos.size)
        assertTrue(imp.problemasFotos[1], imp.problemasFotos[1].contains("50 MB"))
        assertEquals(listOf(imp.fotos[2].id), almacen.leerEventos(id).map { it.id })
        // La grande no se llegó a abrir.
        assertTrue(aperturas.none { it.first == "content://foto-grande" })
    }

    @Test
    fun `sin lugar en el celu queda el error antes de copiar`() {
        doc("content://audio", 1000)
        crear(pedido(audioBytes = 400L * 1024 * 1024))
        val r = copia(espacioLibre = { 500L * 1024 * 1024 }).copiar(id)
        assertEquals(CopiaImportacion.Resultado.ERROR, r)
        assertTrue(almacen.leerImportacion(id)!!.error!!.contains("lugar"))
        assertTrue(aperturas.isEmpty())
    }

    @Test
    fun `una clase grabada o terminada no se toca`() {
        val grabada = SesionLocal("019a1b2c-3d4e-7f50-8a6b-000000000009", null, "auto", "teorica", "2026-10-22T14:00:00-03:00", "s23-ultra")
        almacen.crear(grabada)
        assertEquals(CopiaImportacion.Resultado.NADA, copia().copiar(grabada.id))
        assertEquals(grabada, almacen.leerSesion(grabada.id))
    }

    // ---- La fila de Inicio ----

    private val base = FilaSesion(
        id = id, materia = "Macro", tipoClase = "teorica", fecha = "mié 22/10 14:00", duracion = null,
        estado = "", detalle = null, progreso = null, tono = Tono.NORMAL, pdf = null,
    )

    @Test
    fun `la fila muestra Copiando con el porcentaje, y el error con Reintentar y Descartar`() {
        crear(pedido(audioBytes = 1000))
        val s = almacen.leerSesion(id)!!
        val imp = almacen.leerImportacion(id)!!
        val copiando = FilasImportacion.copiando(base, s, imp, largoParcial = 400)!!
        assertEquals("Copiando… 40 %", copiando.estado)
        assertEquals(0.4f, copiando.progreso!!, 1e-6f)
        assertTrue(copiando.importada)
        assertFalse(copiando.importacionFallida)

        val fallida = FilasImportacion.copiando(base, s, imp.copy(error = "No se puede leer el audio"), 0)!!
        assertEquals(FilasImportacion.NO_SE_PUDO, fallida.estado)
        assertEquals("No se puede leer el audio", fallida.detalle)
        assertEquals(Tono.ERROR, fallida.tono)
        assertTrue(fallida.importacionFallida)

        // Terminada, ya no es una fila de la copia (la sigue la de la subida).
        assertNull(FilasImportacion.copiando(base, s.copy(estadoLocal = EstadoLocal.TERMINADA), imp, 0))
        assertEquals("Subiendo 23 %", FilasImportacion.subiendo(0.234f))
    }

    // ---- Descartar ----

    @Test
    fun `descartar borra solo las copias de una importacion fallida que no llego al server`() {
        doc("content://audio", 1000)
        val adjuntos = AlmacenAdjuntos(tmp.newFolder("adjuntos"))
        crear(pedido(audioBytes = 1000))
        adjuntos.dir(id).mkdirs()
        File(adjuntos.dir(id), "algo.pdf").writeBytes(ByteArray(10))

        // Mientras se copia (sin error) no se descarta.
        assertNull(Importador.descartar(almacen, adjuntos, id))
        assertTrue(almacen.dir(id).isDirectory)

        ilegibles += "content://audio"
        copia().copiar(id)
        assertEquals(listOf("content://audio"), Importador.descartar(almacen, adjuntos, id))
        assertFalse(almacen.dir(id).exists())
        assertFalse(adjuntos.dir(id).exists())
    }

    // ---- Permisos de los documentos ----

    @Test
    fun `un documento que usa otra importacion, o una copia de adjuntos, sigue en uso`() {
        val adjuntos = AlmacenAdjuntos(tmp.newFolder("adjuntos"))
        val foto = DocumentoElegido("content://foto", "foto.jpg", 100, "image/jpeg")
        val otraFoto = DocumentoElegido("content://otra-foto", "otra.jpg", 100, "image/jpeg")
        doc("content://audio", 1000)
        doc("content://foto", 100)
        doc("content://otra-foto", 100)
        // A falló ("No hay lugar", por ejemplo) y el usuario importó la misma clase de nuevo (B), con una foto más.
        crear(pedido(audioBytes = 1000, fotos = listOf(foto)))
        ilegibles += "content://audio"
        assertEquals(CopiaImportacion.Resultado.ERROR, copia().copiar(id))
        ilegibles.clear()
        val b = "019a1b2c-3d4e-7f50-8a6b-7c8d9e0f1a2c"
        Importador.crear(almacen, pedido(audioBytes = 1000, fotos = listOf(foto, otraFoto)), id = b, espacioLibre = { 0L })
        assertEquals(setOf("content://audio", "content://foto", "content://otra-foto"), Importador.urisEnUso(almacen, adjuntos))

        // Descartar A no le saca a B el audio ni la foto.
        val deA = Importador.descartar(almacen, adjuntos, id)!!
        assertEquals(listOf("content://audio", "content://foto"), deA)
        assertTrue(Importador.urisEnUso(almacen, adjuntos).containsAll(deA))

        // B terminó de copiarse: ya nadie los usa.
        assertEquals(CopiaImportacion.Resultado.TERMINADA, copia().copiar(b))
        assertTrue(Importador.urisEnUso(almacen, adjuntos).isEmpty())

        // Una copia de adjuntos que quedó pendiente (la app se cerró en el medio) también usa su documento.
        adjuntos.anotarCopia(CopiaPendiente(id = "019a1b2c-3d4e-7f50-8a6b-7c8d9e0f1a2d", sesionId = b, uri = "content://slides", creado = creado))
        assertEquals(setOf("content://slides"), Importador.urisEnUso(almacen, adjuntos))
    }

    @Test
    fun `una importacion que ya llego al server, o una clase grabada, no se descartan`() {
        val adjuntos = AlmacenAdjuntos(tmp.newFolder("adjuntos"))
        crear(pedido(audioBytes = 1000))
        ilegibles += "content://audio"
        copia().copiar(id)
        almacen.guardarSubida(id, almacen.leerSubida(id).copy(sesionCreada = true))
        assertNull(Importador.descartar(almacen, adjuntos, id))
        assertTrue(almacen.archivoSesion(id).exists())

        val grabada = SesionLocal("019a1b2c-3d4e-7f50-8a6b-000000000009", null, "auto", "teorica", "2026-10-22T14:00:00-03:00", "s23-ultra")
        almacen.crear(grabada)
        assertNull(Importador.descartar(almacen, adjuntos, grabada.id))
        assertTrue(almacen.dir(grabada.id).isDirectory)
    }
}
