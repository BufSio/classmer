package ar.classmer.subida

import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.EstadoSubidaAdjuntos
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.Pista
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.SoporteAdjuntos
import ar.classmer.red.ClienteApi
import ar.classmer.red.ErrorAutenticacion
import ar.classmer.watch.PistaCelu
import ar.classmer.watch.PistasCelu
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.FileOutputStream
import kotlin.random.Random

class SubidorAdjuntosTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private lateinit var http: MockWebServer
    private lateinit var servidor: ServidorDePrueba
    private lateinit var sesiones: AlmacenSesiones
    private lateinit var adjuntos: AlmacenAdjuntos
    private val id = "01a137d8-617d-718c-a700-3328dc9a1fec"

    /** Una clase grabada en otro celu: existe sólo en el server. */
    private val otra = "01a137d8-0000-718c-a700-3328dc9a1fed"

    /** El reloj de la subida (las esperas se miden con éste). */
    private var ahora = 1_800_000_000_000L

    /** Lo que avisó la subida con `alSubir` ("Subiendo…"). */
    private val avisos = mutableListOf<String?>()

    @Before
    fun preparar() {
        servidor = ServidorDePrueba()
        http = MockWebServer().apply { dispatcher = servidor; start() }
        val raiz = tmp.newFolder("files")
        sesiones = AlmacenSesiones(File(raiz, "sesiones").apply { mkdirs() })
        adjuntos = AlmacenAdjuntos(File(raiz, "adjuntos"))
    }

    @After
    fun cerrar() = http.shutdown()

    private fun url() = http.url("/").toString()

    private fun api(token: String = "tok") = ClienteApi(url(), token)

    private fun subidorAdjuntos(token: String = "tok", servidorUrl: String = url()) =
        SubidorAdjuntos(adjuntos, sesiones, api(token), servidor = servidorUrl, reloj = { ahora }, alSubir = { avisos += it })

    private fun nuevaSesion() {
        sesiones.crear(
            SesionLocal(
                id = id, materiaId = "analisis", origenMateria = "calendario", tipoClase = "teorica",
                inicio = "2026-10-22T14:00:05-03:00", dispositivo = "s23-ultra",
            ),
        )
        FileOutputStream(sesiones.archivoAudio(id), true).use { it.write(Random.nextBytes(500)) }
    }

    /** La clase llega al server (y se crea `subida.json` con `sesion_creada`). */
    private suspend fun subirClase(terminar: Boolean = false) {
        if (terminar) {
            sesiones.guardarSesion(sesiones.leerSesion(id)!!.copy(estadoLocal = EstadoLocal.TERMINADA, duracionMs = 5_000))
        }
        Subidor(sesiones, api(), tamanioTramo = 1000).sincronizar(id)
    }

    private fun pdf(n: Int = 3000) = "%PDF-1.7\n".toByteArray() + Random.nextBytes(n)

    private val png = byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A) + Random.nextBytes(800)

    private fun adjuntar(
        nombre: String = "Slides clase 5.pdf",
        datos: ByteArray = pdf(),
        offsetMs: Long? = null,
        sesionId: String = id,
    ): AdjuntoLocal = adjuntos.copiar(sesionId, datos.inputStream(), nombre, offsetMs, "2026-10-22T15:03:10-03:00")

    /** Todos los POST a /adjuntos: las consultas de unos bytes y los que llevan el archivo. */
    private fun pedidosAdjuntos() = servidor.pedidos.count { it.startsWith("POST") && it.endsWith("/adjuntos") }

    /** Los POST a /adjuntos que llevaron el archivo. */
    private fun archivos() = servidor.archivosRecibidos

    private fun subida(a: AdjuntoLocal) = adjuntos.leerSubida(a.sesionId).de(a.id)

    private fun json(codigo: Int, cuerpo: String) =
        MockResponse().setResponseCode(codigo).setHeader("Content-Type", "application/json").setBody(cuerpo)

    private fun pasar(ms: Long) {
        ahora += ms
    }

    @Test
    fun `espera a que la clase este en el server`() = runTest {
        nuevaSesion()
        val a = adjuntar()

        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertEquals("sin sesion_creada no se manda nada", 0, pedidosAdjuntos())
        assertFalse(subida(a).subido)

        subirClase()
        assertTrue(sesiones.leerSubida(id).sesionCreada)
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertEquals(setOf(a.id), servidor.adjuntos.getValue(id).keys)
        assertTrue(subida(a).subido)
    }

    @Test
    fun `antes del archivo pregunta con unos bytes, y avisa cual se esta mandando`() = runTest {
        servidor.sesionesSoloServidor += otra
        val a = adjuntar(sesionId = otra)

        subidorAdjuntos().sincronizarTodo()

        assertEquals("la consulta (422: no lo tiene) y después el archivo", 2, pedidosAdjuntos())
        assertEquals(1, archivos())
        assertEquals(listOf(a.id, null), avisos)
    }

    @Test
    fun `una clase ya finalizada igual sube sus adjuntos`() = runTest {
        nuevaSesion()
        subirClase(terminar = true)
        assertTrue(sesiones.leerSubida(id).finalizada)
        // El material llega días después.
        val a = adjuntar()

        assertEquals(Subidor.Resultado.COMPLETA, Subidor(sesiones, api()).sincronizar(id))
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
        assertEquals(1, servidor.adjuntos.getValue(id).size)
    }

    @Test
    fun `una clase que solo esta en el server sube sin sesion local`() = runTest {
        servidor.sesionesSoloServidor += otra
        val a = adjuntar(sesionId = otra)

        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
        assertEquals(setOf(a.id), servidor.adjuntos.getValue(otra).keys)
        assertFalse("no crea una carpeta de sesión", sesiones.dir(otra).exists())
    }

    @Test
    fun `si se perdio la respuesta, la consulta lo encuentra y el archivo no se vuelve a mandar`() = runTest {
        servidor.sesionesSoloServidor += otra
        val a = adjuntar(sesionId = otra)
        servidor.perderRespuestasAdjunto = 1

        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertFalse(subida(a).subido)
        assertNotNull(subida(a).ultimoError)
        assertEquals(1, subida(a).fallos)

        // Enseguida (el servicio pide una pasada cada ~30 s): espera, no manda nada.
        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertEquals(2, pedidosAdjuntos())

        pasar(SubidorAdjuntos.ESPERA_INICIAL_MS + 1)
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
        assertNull(subida(a).ultimoError)
        assertEquals(0, subida(a).fallos)
        assertEquals("la consulta contestó 200: el archivo se mandó una sola vez", 1, archivos())
        assertEquals(1, servidor.adjuntos.getValue(otra).size)
        assertEquals(3, pedidosAdjuntos())
    }

    @Test
    fun `el multipart lleva el nombre original, el tipo, el minuto si hay y creado`() = runTest {
        servidor.sesionesSoloServidor += otra
        val conMinuto = adjuntar(nombre = "Slides clase 5 (versión final).pdf", datos = pdf(4321), offsetMs = 2_040_000, sesionId = otra)
        val sinMinuto = adjuntar(nombre = "pizarrón.png", datos = png, sesionId = otra)

        subidorAdjuntos().sincronizarTodo()

        val recibidos = servidor.adjuntos.getValue(otra)
        val r1 = recibidos.getValue(conMinuto.id)
        assertEquals("Slides clase 5 (versión final).pdf", r1.nombreOriginal)
        assertEquals("application/pdf", r1.tipo)
        assertEquals(conMinuto.bytes.toInt(), r1.bytes)
        assertEquals("2040000", r1.offsetMs)
        assertEquals("2026-10-22T15:03:10-03:00", r1.creado)
        val r2 = recibidos.getValue(sinMinuto.id)
        assertEquals("pizarrón.png", r2.nombreOriginal)
        assertEquals("image/png", r2.tipo)
        assertEquals(png.size, r2.bytes)
        assertNull("sin minuto, offset_ms no va", r2.offsetMs)
        assertEquals("en orden de llegada", listOf(conMinuto.id, sinMinuto.id), recibidos.keys.toList())
    }

    @Test
    fun `guarda lo que devuelve el server, con el objeto de la sesion`() = runTest {
        servidor.sesionesSoloServidor += otra
        servidor.estadoAdjunto = "sin_usar"
        servidor.adjuntosSinUsar = 1
        val a = adjuntar(sesionId = otra)

        subidorAdjuntos().sincronizarTodo()

        assertEquals("sin_usar", subida(a).servidor?.estado)
        assertEquals("Slides clase 5.pdf", subida(a).servidor?.nombreOriginal)
        val sesion = adjuntos.leerSubida(otra).sesion
        assertEquals(otra, sesion?.id)
        assertEquals(1, sesion?.adjuntosSinUsar)
        assertEquals(listOf(a.id), sesion?.adjuntos?.map { it.id })
    }

    @Test
    fun `404 sesion no encontrada espera, se reintenta y despues sube, sin mandar el archivo de mas`() = runTest {
        val a = adjuntar(sesionId = otra)

        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertFalse(subida(a).subido)
        assertNull(subida(a).rechazo)
        val clase = adjuntos.leerSubida(otra)
        assertEquals(1, clase.noEncontrada)
        assertTrue(clase.ultimoError!!.contains("no encuentra"))
        assertEquals("sólo la consulta: el archivo no se manda", 0, archivos())

        // Antes de que pase la espera no se pregunta de nuevo.
        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertEquals(1, pedidosAdjuntos())

        servidor.sesionesSoloServidor += otra
        pasar(SubidorAdjuntos.espera(1) + 1)
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
        assertEquals(0, adjuntos.leerSubida(otra).noEncontrada)
        assertNull(adjuntos.leerSubida(otra).ultimoError)
        assertEquals(1, archivos())
    }

    @Test
    fun `una clase que el server perdio deja de pedir reintento, nunca manda el archivo y pregunta cada hora`() = runTest {
        val a = adjuntar(sesionId = otra)
        adjuntar(sesionId = otra, nombre = "Notas.pdf")

        for (n in 1..EstadoSubidaAdjuntos.MAX_NO_ENCONTRADA) {
            assertEquals("intento $n", SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
            pasar(SubidorAdjuntos.espera(n) + 1)
        }
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(adjuntos.leerSubida(otra).claseNoEncontrada)
        assertEquals("una consulta por intento, y los otros de la clase no se preguntan", EstadoSubidaAdjuntos.MAX_NO_ENCONTRADA + 1, pedidosAdjuntos())

        // Las pasadas siguientes (cada ~30 s mientras se graba otra clase) no le mandan nada.
        repeat(20) {
            pasar(30_000)
            assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        }
        assertEquals(EstadoSubidaAdjuntos.MAX_NO_ENCONTRADA + 1, pedidosAdjuntos())

        // Pasada la hora, se le pregunta con unos bytes.
        pasar(SubidorAdjuntos.ESPERA_CLASE_PERDIDA_MS)
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertEquals(EstadoSubidaAdjuntos.MAX_NO_ENCONTRADA + 2, pedidosAdjuntos())

        // "Reintentar" pregunta ya; si la clase apareció (por ejemplo, se restauró), sube.
        servidor.sesionesSoloServidor += otra
        adjuntos.archivoPedidoReintento(otra, a.id).createNewFile()
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
        assertFalse(adjuntos.leerSubida(otra).claseNoEncontrada)
        assertEquals(0, adjuntos.leerSubida(otra).noEncontrada)
        assertEquals(2, archivos())
    }

    @Test
    fun `server sin la ruta (404 Not Found o 405) es sin soporte para todos, sin mandar archivos y preguntando cada 10 minutos`() = runTest {
        for ((codigo, cuerpo) in listOf(404 to """{"detail":"Not Found"}""", 405 to """{"detail":"Method Not Allowed"}""")) {
            servidor.pedidos.clear()
            servidor.archivosRecibidos = 0
            adjuntos.guardarSoporte(SoporteAdjuntos())
            val claseA = "01a137d8-0000-718c-a700-000000000$codigo"
            val claseB = "01a137d8-0001-718c-a700-000000000$codigo"
            servidor.sesionesSoloServidor += claseA
            servidor.sesionesSoloServidor += claseB
            val a = adjuntar(sesionId = claseA)
            val b = adjuntar(sesionId = claseA, nombre = "Notas.pdf")
            val c = adjuntar(sesionId = claseB, nombre = "Guía.pdf")
            servidor.responderAdjunto = { _, _ -> json(codigo, cuerpo) }

            assertEquals("HTTP $codigo", SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
            assertEquals("corta en la primera consulta: los demás darían lo mismo", 1, pedidosAdjuntos())
            assertEquals("el archivo nunca se manda a un server viejo", 0, archivos())
            val soporte = adjuntos.leerSoporte()
            assertTrue("vale para todos los adjuntos, de todas las clases", soporte.sinSoporte)
            assertEquals(url(), soporte.servidor)
            assertFalse(subida(a).subido)
            assertNull("no es un rechazo: queda guardado", subida(a).rechazo)

            // Mientras se graba, una pasada cada ~30 s: no se le pregunta en cada una.
            repeat(19) {
                pasar(30_000)
                assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
            }
            assertEquals(1, pedidosAdjuntos())

            // A los 10 minutos, se le vuelve a preguntar (unos bytes).
            pasar(SubidorAdjuntos.ESPERA_SIN_SOPORTE_MS)
            assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
            assertEquals(2, pedidosAdjuntos())

            // "Reintentar" pregunta ya.
            adjuntos.archivoPedidoReintento(claseA, b.id).createNewFile()
            assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
            assertEquals(3, pedidosAdjuntos())
            assertEquals(0, archivos())

            // Se actualizó el server: a la próxima pregunta sube todo y se borra el aviso.
            servidor.responderAdjunto = { _, _ -> null }
            pasar(SubidorAdjuntos.ESPERA_SIN_SOPORTE_MS + 1)
            assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
            assertTrue(subida(a).subido)
            assertTrue(subida(b).subido)
            assertTrue(subida(c).subido)
            assertFalse(adjuntos.leerSoporte().sinSoporte)
            assertEquals(3, archivos())
        }
    }

    @Test
    fun `si se cambio de server en Ajustes, se le pregunta enseguida`() = runTest {
        servidor.sesionesSoloServidor += otra
        val a = adjuntar(sesionId = otra)
        adjuntos.guardarSoporte(SoporteAdjuntos(sinSoporte = true, servidor = "http://server-viejo:8000/", ultimoSondeoMs = ahora))

        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
        assertFalse(adjuntos.leerSoporte().sinSoporte)

        // Con el mismo server, en cambio, se respeta la espera.
        val b = adjuntar(sesionId = otra, nombre = "Notas.pdf")
        adjuntos.guardarSoporte(SoporteAdjuntos(sinSoporte = true, servidor = url(), ultimoSondeoMs = ahora))
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertFalse(subida(b).subido)
    }

    @Test
    fun `409, 413, 415 y 422 son rechazos con el motivo, sin reintento solo`() = runTest {
        servidor.sesionesSoloServidor += otra
        val casos = mapOf(
            409 to """{"detail":"la clase está en error"}""",
            413 to """{"detail":"archivo de más de 100 MB"}""",
            415 to """{"detail":"el archivo no es PDF, JPEG, PNG, WebP ni HEIC"}""",
            422 to """{"detail":[{"loc":["body","offset_ms"],"msg":"tiene que ser un entero >= 0","type":"value_error"}]}""",
        )
        val porCodigo = casos.keys.associateWith { adjuntar(sesionId = otra, nombre = "adjunto-$it.pdf") }
        val codigoDe = porCodigo.entries.associate { (codigo, a) -> a.id to codigo }
        // Como el server real: la consulta sin archivo da 422 "falta el archivo"; el rechazo llega con el archivo.
        servidor.responderAdjunto = { aid, conArchivo -> if (conArchivo) codigoDe[aid]?.let { json(it, casos.getValue(it)) } else null }

        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertEquals(4, archivos())
        assertEquals("la clase está en error", subida(porCodigo.getValue(409)).rechazo)
        assertEquals("archivo de más de 100 MB", subida(porCodigo.getValue(413)).rechazo)
        assertEquals("el archivo no es PDF, JPEG, PNG, WebP ni HEIC", subida(porCodigo.getValue(415)).rechazo)
        assertEquals("offset_ms: tiene que ser un entero >= 0", subida(porCodigo.getValue(422)).rechazo)
        porCodigo.values.forEach { assertFalse(subida(it).subido) }

        // No se reintentan solos (ni se pregunta por ellos).
        val antes = pedidosAdjuntos()
        pasar(SubidorAdjuntos.ESPERA_CLASE_PERDIDA_MS)
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertEquals(antes, pedidosAdjuntos())

        // "Reintentar" de uno: se vuelve a mandar sólo ése.
        servidor.responderAdjunto = { _, _ -> null }
        val elegido = porCodigo.getValue(409)
        adjuntos.archivoPedidoReintento(otra, elegido.id).createNewFile()
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertEquals(5, archivos())
        assertTrue(subida(elegido).subido)
        assertNull(subida(elegido).rechazo)
        assertFalse(adjuntos.archivoPedidoReintento(otra, elegido.id).exists())
        assertNotNull(subida(porCodigo.getValue(415)).rechazo)
    }

    @Test
    fun `token rechazado es ErrorAutenticacion`() = runTest {
        servidor.sesionesSoloServidor += otra
        val a = adjuntar(sesionId = otra)
        try {
            subidorAdjuntos(token = "otro").sincronizarTodo()
            fail("tenía que fallar")
        } catch (e: ErrorAutenticacion) {
            assertEquals(401, e.codigo)
        }
        assertFalse(subida(a).subido)
        assertNull(subida(a).rechazo)
        assertEquals(0, archivos())
    }

    @Test
    fun `sin server reintenta y despues sube`() = runTest {
        servidor.sesionesSoloServidor += otra
        val a = adjuntar(sesionId = otra)
        val puerto = http.port
        http.shutdown()

        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).ultimoError!!.startsWith("Sin conexión"))
        assertFalse(subida(a).subido)
        assertEquals("falló la consulta, no el archivo: no hay espera", 0, subida(a).fallos)

        http = MockWebServer().apply { dispatcher = servidor; start(puerto) }
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
    }

    @Test
    fun `un 503 al mandar el archivo espera cada vez mas y no frena a los otros`() = runTest {
        servidor.sesionesSoloServidor += otra
        val a = adjuntar(sesionId = otra)
        val b = adjuntar(sesionId = otra, nombre = "Notas.pdf")
        var fallas = 2
        servidor.responderAdjunto = { aid, conArchivo ->
            if (conArchivo && aid == a.id && fallas > 0) json(503, """{"detail":"ocupado"}""").also { fallas-- } else null
        }

        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertFalse(subida(a).subido)
        assertTrue(subida(b).subido)
        assertEquals(ahora + SubidorAdjuntos.espera(1), subida(a).esperarHastaMs)

        // Durante la espera no se manda (ni se pregunta).
        val antes = pedidosAdjuntos()
        pasar(SubidorAdjuntos.ESPERA_INICIAL_MS - 1)
        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertEquals(antes, pedidosAdjuntos())

        pasar(2)
        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertEquals("la segunda falla espera el doble", ahora + SubidorAdjuntos.espera(2), subida(a).esperarHastaMs)
        assertEquals(2 * SubidorAdjuntos.ESPERA_INICIAL_MS, SubidorAdjuntos.espera(2))

        pasar(SubidorAdjuntos.espera(2) + 1)
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
        assertEquals(0, subida(a).fallos)
        assertNull(subida(a).esperarHastaMs)
    }

    @Test
    fun `un 503 en la consulta corta la pasada sin mandar archivos`() = runTest {
        servidor.sesionesSoloServidor += otra
        val a = adjuntar(sesionId = otra)
        adjuntar(sesionId = otra, nombre = "Notas.pdf")
        servidor.responderAdjunto = { _, conArchivo -> if (!conArchivo) json(503, """{"detail":"ocupado"}""") else null }

        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertEquals(1, pedidosAdjuntos())
        assertEquals(0, archivos())
        assertTrue(subida(a).ultimoError!!.contains("503"))
    }

    @Test
    fun `las esperas no se respetan si el reloj del celu cambio`() = runTest {
        servidor.sesionesSoloServidor += otra
        val a = adjuntar(sesionId = otra)
        // Una espera de días: el reloj estaba adelantado cuando se anotó.
        adjuntos.guardarSubida(otra, EstadoSubidaAdjuntos().con(a.id, adjuntos.leerSubida(otra).de(a.id).copy(fallos = 1, esperarHastaMs = ahora + 5 * 86_400_000L)))

        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
    }

    @Test
    fun `lo subido no se vuelve a subir`() = runTest {
        servidor.sesionesSoloServidor += otra
        adjuntar(sesionId = otra)
        adjuntar(sesionId = otra, nombre = "pizarrón.png", datos = png)
        subidorAdjuntos().sincronizarTodo()
        assertEquals(2, archivos())
        val antes = pedidosAdjuntos()

        repeat(3) { assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo()) }
        assertEquals(antes, pedidosAdjuntos())

        // Uno nuevo: se manda sólo ése.
        adjuntar(sesionId = otra, nombre = "Práctica 3.pdf")
        subidorAdjuntos().sincronizarTodo()
        assertEquals(3, archivos())
        assertEquals(3, servidor.adjuntos.getValue(otra).size)
    }

    @Test
    fun `si el server volvio a crear la clase, los adjuntos se vuelven a mandar`() = runTest {
        nuevaSesion()
        subirClase()
        val a = adjuntar()
        subidorAdjuntos().sincronizarTodo()
        assertTrue(subida(a).subido)

        // El server pierde todo (se borraron sus datos, o se cambió de server en Ajustes).
        servidor.sesiones.clear()
        servidor.adjuntos.clear()
        subirClase()
        assertTrue("Subidor la volvió a crear y lo avisa", SubidorAdjuntos.archivoPedidoReenvio(sesiones, id).exists())

        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertTrue(subida(a).subido)
        assertEquals(setOf(a.id), servidor.adjuntos.getValue(id).keys)
        assertFalse("el pedido se consume", SubidorAdjuntos.archivoPedidoReenvio(sesiones, id).exists())
        assertEquals(2, archivos())
    }

    @Test
    fun `sin adjuntos no molesta al server`() = runTest {
        nuevaSesion()
        subirClase()
        val antes = servidor.pedidos.size
        assertEquals(SubidorAdjuntos.Resultado.LISTO, subidorAdjuntos().sincronizarTodo())
        assertEquals(antes, servidor.pedidos.size)
    }

    @Test
    fun `con Ambos, un adjunto contado con la pista del celu espera el desfase y va en el audio del reloj`() = runTest {
        nuevaSesion()
        sesiones.guardarSesion(sesiones.leerSesion(id)!!.copy(fuenteAudio = FuenteAudio.RELOJ, pistaCelu = true))
        PistasCelu(sesiones).guardar(id, PistaCelu(inicioMs = 0))
        subirClase()
        val a = adjuntos.copiar(id, pdf().inputStream(), "Slides.pdf", 90_000, "2026-10-22T15:03:10-03:00", pista = Pista.CELU)

        assertEquals(SubidorAdjuntos.Resultado.REINTENTAR, subidorAdjuntos().sincronizarTodo())
        assertFalse(subida(a).subido)

        PistasCelu(sesiones).cambiar(id) { it.copy(desfaseMs = -30_000, desfaseErrorMs = 23) }
        subidorAdjuntos().sincronizarTodo()
        assertTrue(subida(a).subido)
        assertEquals("60000", servidor.adjuntos.getValue(id).getValue(a.id).offsetMs)
    }
}
