package ar.classmer.subida

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Evento
import ar.classmer.modelo.SesionLocal
import ar.classmer.red.ClienteApi
import ar.classmer.red.ErrorApi
import ar.classmer.red.ErrorAutenticacion
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.FileOutputStream
import kotlin.random.Random

class SubidorTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private lateinit var http: MockWebServer
    private lateinit var servidor: ServidorDePrueba
    private lateinit var almacen: AlmacenSesiones
    private val id = "01a137d8-617d-718c-a700-3328dc9a1fec"

    @Before
    fun preparar() {
        servidor = ServidorDePrueba()
        http = MockWebServer().apply { dispatcher = servidor; start() }
        almacen = AlmacenSesiones(tmp.newFolder("sesiones"))
    }

    @After
    fun cerrar() = http.shutdown()

    private fun subidor(token: String = "tok", tramo: Long = 1000) =
        Subidor(almacen, ClienteApi(http.url("/").toString(), token), tamanioTramo = tramo)

    private fun nuevaSesion(
        numeroClase: Int? = null,
        materiaId: String? = "analisis",
        origen: String = "calendario",
    ): SesionLocal {
        val s = SesionLocal(
            id = id, materiaId = materiaId, origenMateria = origen, tipoClase = "teorica",
            inicio = "2026-10-15T14:00:05-03:00", dispositivo = "s23-ultra", numeroClase = numeroClase,
        )
        almacen.crear(s)
        return s
    }

    private fun agregarAudio(n: Int): ByteArray {
        val bytes = Random.nextBytes(n)
        FileOutputStream(almacen.archivoAudio(id), true).use { it.write(bytes) }
        return bytes
    }

    private fun terminar(duracion: Long = 5_000) {
        val s = almacen.leerSesion(id)!!
        almacen.guardarSesion(s.copy(estadoLocal = EstadoLocal.TERMINADA, duracionMs = duracion))
    }

    private fun evento(i: Int, tipo: String = "marca") =
        Evento("ev-$i", tipo, i * 1000L, "2026-10-15T14:00:0$i-03:00", marca = if (tipo == "marca") "importante" else null)

    private fun audioLocal() = almacen.archivoAudio(id).readBytes()

    @Test
    fun `mientras graba sube audio y eventos pero no finaliza`() = runTest {
        nuevaSesion()
        agregarAudio(2500)
        almacen.agregarEvento(id, evento(1))

        assertEquals(Subidor.Resultado.PARCIAL, subidor().sincronizar(id))

        val s = servidor.sesiones.getValue(id)
        assertArrayEquals(audioLocal(), s.audio.toByteArray())
        assertEquals(setOf("ev-1"), s.eventos.keys)
        assertFalse(s.finalizada)
        assertTrue(servidor.pedidos.none { it.contains("finalizar") })
        assertTrue(servidor.cuerpoCrearContiene("\"materia_id\":\"analisis\""))
        assertFalse("los campos locales no van al server", servidor.cuerpoCrearContiene("estado_local"))
        // 2500 bytes en tramos de 1000: 3 PUT.
        assertEquals(3, servidor.pedidos.count { it.startsWith("PUT") })
    }

    @Test
    fun `retoma desde lo confirmado y finaliza con sha256`() = runTest {
        nuevaSesion()
        agregarAudio(1500)
        subidor().sincronizar(id)
        agregarAudio(1700)
        almacen.agregarEvento(id, evento(1))
        almacen.agregarEvento(id, evento(2))
        terminar()

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))

        val s = servidor.sesiones.getValue(id)
        assertTrue(s.finalizada)
        assertArrayEquals(audioLocal(), s.audio.toByteArray())
        assertTrue(servidor.pedidos.any { it.startsWith("PUT") && it.contains("offset=1500") })
        assertTrue(almacen.leerSubida(id).finalizada)
        // Una vez finalizada, no vuelve a molestar al server.
        val antes = servidor.pedidos.size
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertEquals(antes, servidor.pedidos.size)
    }

    @Test
    fun `ante un 409 sigue desde lo que tiene el server`() = runTest {
        nuevaSesion()
        agregarAudio(3000)
        servidor.guardarMitadProximoPut = true // guarda 500 de los primeros 1000 bytes
        terminar()

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertArrayEquals(audioLocal(), servidor.sesiones.getValue(id).audio.toByteArray())
        assertTrue(servidor.pedidos.any { it.contains("offset=500") })
    }

    @Test
    fun `una falla del server a mitad deja todo anotado y el reintento termina`() = runTest {
        nuevaSesion()
        agregarAudio(2500)
        almacen.agregarEvento(id, evento(1))
        terminar()
        var puts = 0
        servidor.fallarSi = { it.startsWith("PUT") && ++puts == 2 }
        try {
            subidor().sincronizar(id)
            fail("tenía que fallar")
        } catch (e: ErrorApi) {
            assertEquals(503, e.codigo)
        }
        val parcial = almacen.leerSubida(id)
        assertTrue(parcial.sesionCreada)
        assertEquals(1000, parcial.bytesConfirmados)
        assertFalse(parcial.finalizada)

        servidor.fallarSi = { false }
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertArrayEquals(audioLocal(), servidor.sesiones.getValue(id).audio.toByteArray())
        assertTrue(servidor.pedidos.any { it.contains("offset=1000") })
    }

    @Test
    fun `sin server tira IOException y despues retoma`() = runTest {
        nuevaSesion()
        agregarAudio(1500)
        terminar()
        val puerto = http.port
        http.shutdown()
        try {
            subidor().sincronizar(id)
            fail("tenía que fallar")
        } catch (e: java.io.IOException) {
            // esperado: el trabajo reintenta con espera creciente
        }
        assertFalse(almacen.leerSubida(id).sesionCreada)

        http = MockWebServer().apply { dispatcher = servidor; start(puerto) }
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertArrayEquals(audioLocal(), servidor.sesiones.getValue(id).audio.toByteArray())
    }

    @Test
    fun `faltan_eventos vuelve a subir todos los eventos`() = runTest {
        nuevaSesion()
        agregarAudio(100)
        repeat(3) { almacen.agregarEvento(id, evento(it)) }
        terminar()
        servidor.olvidarEventosUnaVez = true

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertEquals(3, servidor.sesiones.getValue(id).eventos.size)
        assertEquals(6, servidor.pedidos.count { it.startsWith("POST") && it.contains("/eventos") })
    }

    @Test
    fun `faltan_eventos reenvia solo los que el server no tiene`() = runTest {
        nuevaSesion()
        agregarAudio(100)
        repeat(4) { almacen.agregarEvento(id, evento(it)) }
        terminar()
        servidor.olvidarEvento = "ev-2"

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertEquals(4, servidor.sesiones.getValue(id).eventos.size)
        assertEquals(5, servidor.pedidos.count { it.startsWith("POST") && it.contains("/eventos") })
        assertEquals(1, servidor.pedidos.count { it.startsWith("GET") && it.contains("/eventos") })
    }

    @Test
    fun `hash_distinto con reinicio del server vuelve a subir el audio entero`() = runTest {
        nuevaSesion()
        agregarAudio(2200)
        terminar()
        servidor.hashDistintoUnaVez = true

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertArrayEquals(audioLocal(), servidor.sesiones.getValue(id).audio.toByteArray())
        assertEquals(2, servidor.pedidos.count { it.startsWith("PUT") && it.contains("offset=0") })
    }

    @Test
    fun `hash_distinto sin reinicio del server es un error, no un bucle`() = runTest {
        nuevaSesion()
        agregarAudio(200)
        terminar()
        servidor.hashDistintoUnaVez = true
        servidor.resetearEnHashDistinto = false

        try {
            subidor().sincronizar(id)
            fail("tenía que fallar")
        } catch (e: ErrorProtocolo) {
            assertTrue(e.message!!.contains("hash"))
        }
        assertFalse(almacen.leerSubida(id).finalizada)
    }

    @Test
    fun `la foto viaja en el multipart`() = runTest {
        nuevaSesion()
        val foto = File(almacen.dirFotos(id), "f1.jpg").apply { writeBytes(Random.nextBytes(4321)) }
        almacen.agregarEvento(id, Evento("f1", "foto", 1234, "2026-10-15T14:00:05-03:00", archivo = "fotos/${foto.name}"))
        almacen.agregarEvento(id, Evento("n1", "nota", 2000, "2026-10-15T14:00:06-03:00", texto = "¿por qué el gradiente?"))
        agregarAudio(10)
        terminar()

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertEquals(4321, servidor.sesiones.getValue(id).fotos["f1"])
    }

    @Test
    fun `token rechazado es ErrorAutenticacion`() = runTest {
        nuevaSesion()
        try {
            subidor(token = "otro").sincronizar(id)
            fail("tenía que fallar")
        } catch (e: ErrorAutenticacion) {
            assertEquals(401, e.codigo)
        }
    }

    @Test
    fun `una linea incompleta al final de eventos jsonl no se sube ni se cuenta`() = runTest {
        nuevaSesion()
        almacen.agregarEvento(id, evento(1))
        FileOutputStream(almacen.archivoEventos(id), true).use { it.write("{\"id\":\"ev-2\",\"ti".toByteArray()) }
        agregarAudio(10)
        terminar()

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertEquals(setOf("ev-1"), servidor.sesiones.getValue(id).eventos.keys)
    }

    @Test
    fun `si el server perdio la sesion la vuelve a crear y sube todo desde cero`() = runTest {
        nuevaSesion()
        agregarAudio(1800)
        almacen.agregarEvento(id, evento(1))
        subidor().sincronizar(id)
        // Se borran los datos del server (o se apunta a otro server).
        servidor.sesiones.clear()
        agregarAudio(700)
        terminar()

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val s = servidor.sesiones.getValue(id)
        assertTrue(s.finalizada)
        assertArrayEquals(audioLocal(), s.audio.toByteArray())
        assertEquals(setOf("ev-1"), s.eventos.keys)
    }

    @Test
    fun `si el server rechaza la materia se manda como Auto`() = runTest {
        nuevaSesion()
        agregarAudio(300)
        terminar()
        servidor.rechazarMaterias = true

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertTrue(servidor.cuerpoCrearContiene("\"materia_id\":null"))
        assertTrue(servidor.cuerpoCrearContiene("\"origen_materia\":\"auto\""))
        assertTrue(almacen.leerSubida(id).crearComoAuto)
    }

    @Test
    fun `una foto que quedo sin evento se registra antes de finalizar`() = runTest {
        nuevaSesion()
        agregarAudio(100)
        almacen.marcarFotoPendiente(id, ar.classmer.modelo.FotoPendiente("f9", 4567, "2026-10-15T14:01:00-03:00"))
        File(almacen.dirFotos(id), "f9.jpg").writeBytes(Random.nextBytes(900))
        terminar()

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val s = servidor.sesiones.getValue(id)
        assertEquals("foto", s.eventos["f9"])
        assertEquals(900, s.fotos["f9"])
        assertEquals(4567L, almacen.leerEventos(id).single { it.id == "f9" }.offsetMs)
        assertFalse(almacen.archivoFotoPendiente(id, "f9").exists())
    }

    @Test
    fun `con la camara todavia abierta espera antes de finalizar`() = runTest {
        nuevaSesion()
        agregarAudio(100)
        almacen.marcarFotoPendiente(id, ar.classmer.modelo.FotoPendiente("f8", 1000, "2026-10-15T14:01:00-03:00"))
        terminar()

        assertEquals(Subidor.Resultado.ESPERANDO, subidor().sincronizar(id))
        assertFalse(servidor.sesiones.getValue(id).finalizada)
    }

    @Test
    fun `si el server ya tenia la sesion, un rechazo del POST no frena la subida`() = runTest {
        nuevaSesion()
        agregarAudio(500)
        subidor().sincronizar(id)
        // El server cambió su config de materias: ahora rechaza el POST, pero la sesión ya existe.
        servidor.rechazarMaterias = true
        agregarAudio(300)
        terminar()
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertArrayEquals(audioLocal(), servidor.sesiones.getValue(id).audio.toByteArray())
    }

    @Test
    fun `un evento que el server rechaza con 422 no traba la clase`() = runTest {
        nuevaSesion()
        agregarAudio(100)
        repeat(3) { almacen.agregarEvento(id, evento(it)) }
        terminar()
        servidor.rechazarEvento = "ev-1"

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertEquals(setOf("ev-0", "ev-2"), servidor.sesiones.getValue(id).eventos.keys)
        assertEquals(setOf("ev-1"), almacen.leerSubida(id).eventosRechazados)
    }

    @Test
    fun `los errores de FastAPI se leen del detail`() {
        assertEquals("token inválido", ar.classmer.red.detalleDe("""{"detail":"token inválido"}"""))
        assertEquals(
            "archivo: falta el archivo de la foto",
            ar.classmer.red.detalleDe("""{"detail":[{"loc":["body","archivo"],"msg":"falta el archivo de la foto"}]}"""),
        )
        assertEquals("<html>error</html>", ar.classmer.red.detalleDe("<html>error</html>"))
        assertTrue(ErrorApi(422, "{}").permanente)
        assertFalse(ErrorApi(404, "{}").permanente)
        assertFalse(ErrorApi(503, "{}").permanente)
    }

    // ---- Número de clase (v0.2) ----

    @Test
    fun `la sesion con numero de clase lo manda al crearla`() = runTest {
        nuevaSesion(numeroClase = 8)
        agregarAudio(300)
        terminar()

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertTrue(servidor.cuerpoCrearContiene("\"numero_clase\":8"))
        assertTrue(servidor.cuerpoCrearContiene("\"materia_id\":\"analisis\""))
        assertEquals(1, postsCrear())
        val subida = almacen.leerSubida(id)
        assertFalse(subida.crearSinNumero)
        assertFalse(subida.crearComoAuto)
        assertTrue(servidor.sesiones.getValue(id).finalizada)
    }

    @Test
    fun `una sesion Auto no manda numero_clase`() = runTest {
        nuevaSesion(numeroClase = null, materiaId = null, origen = "auto")
        agregarAudio(300)
        terminar()

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertFalse("la clave no va (el server pone el número)", servidor.cuerpoCrearContiene("numero_clase"))
        assertTrue(servidor.cuerpoCrearContiene("\"materia_id\":null"))
        assertTrue(servidor.cuerpoCrearContiene("\"origen_materia\":\"auto\""))
        assertEquals(1, postsCrear())
        assertFalse(almacen.leerSubida(id).crearSinNumero)
    }

    @Test
    fun `una sesion con materia y sin numero no repite el mismo intento`() = runTest {
        nuevaSesion(numeroClase = null)
        agregarAudio(300)
        terminar()
        servidor.rechazarMaterias = true

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        // Con materia (400) y después Auto: sin número, "sin número" es el mismo cuerpo y no se repite.
        assertEquals(2, servidor.cuerposCrear.size)
        assertTrue(servidor.cuerposCrear.none { it.contains("numero_clase") })
        val subida = almacen.leerSubida(id)
        assertTrue(subida.crearComoAuto)
        assertFalse(subida.crearSinNumero)
    }

    @Test
    fun `si el server rechaza el numero se crea sin numero y no lo vuelve a mandar`() = runTest {
        nuevaSesion(numeroClase = 8)
        agregarAudio(1500)
        servidor.rechazarNumero = true

        assertEquals(Subidor.Resultado.PARCIAL, subidor().sincronizar(id))
        assertEquals(2, servidor.cuerposCrear.size)
        assertTrue(servidor.cuerposCrear[0].contains("\"numero_clase\":8"))
        assertFalse(servidor.cuerposCrear[1].contains("numero_clase"))
        assertTrue("la materia se conserva", servidor.cuerposCrear[1].contains("\"materia_id\":\"analisis\""))
        assertTrue(servidor.cuerposCrear[1].contains("\"origen_materia\":\"calendario\""))
        val subida = almacen.leerSubida(id)
        assertTrue(subida.sesionCreada)
        assertTrue(subida.crearSinNumero)
        assertFalse(subida.crearComoAuto)
        assertEquals(1500, subida.bytesConfirmados)

        // La próxima sincronización va directo sin número: no gasta el intento que ya se rechazó.
        agregarAudio(700)
        terminar()
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertEquals(3, servidor.cuerposCrear.size)
        assertFalse(servidor.cuerposCrear[2].contains("numero_clase"))
        assertTrue(servidor.cuerposCrear[2].contains("\"materia_id\":\"analisis\""))
        val s = servidor.sesiones.getValue(id)
        assertTrue(s.finalizada)
        assertArrayEquals(audioLocal(), s.audio.toByteArray())
        assertTrue(almacen.leerSubida(id).crearSinNumero)
    }

    @Test
    fun `si el server rechaza el numero y la materia se crea como Auto`() = runTest {
        nuevaSesion(numeroClase = 8)
        agregarAudio(1200)
        almacen.agregarEvento(id, evento(1))
        servidor.rechazarNumero = true
        servidor.rechazarMaterias = true

        assertEquals(Subidor.Resultado.PARCIAL, subidor().sincronizar(id))
        // Completa (422 por el número), sin número (400 por la materia) y Auto (201).
        assertEquals(3, servidor.cuerposCrear.size)
        assertTrue(servidor.cuerposCrear[0].contains("\"numero_clase\":8"))
        assertFalse(servidor.cuerposCrear[1].contains("numero_clase"))
        assertTrue(servidor.cuerposCrear[1].contains("\"materia_id\":\"analisis\""))
        assertFalse(servidor.cuerposCrear[2].contains("numero_clase"))
        assertTrue(servidor.cuerposCrear[2].contains("\"materia_id\":null"))
        assertTrue(servidor.cuerposCrear[2].contains("\"origen_materia\":\"auto\""))
        assertTrue(almacen.leerSubida(id).crearComoAuto)

        // La próxima va directo como Auto.
        terminar()
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertEquals(4, servidor.cuerposCrear.size)
        assertTrue(servidor.cuerposCrear[3].contains("\"materia_id\":null"))
        assertFalse(servidor.cuerposCrear[3].contains("numero_clase"))
        val s = servidor.sesiones.getValue(id)
        assertTrue(s.finalizada)
        assertArrayEquals(audioLocal(), s.audio.toByteArray())
        assertEquals(setOf("ev-1"), s.eventos.keys)
    }

    @Test
    fun `un 503 al crear no degrada la sesion`() = runTest {
        nuevaSesion(numeroClase = 8)
        agregarAudio(300)
        terminar()
        servidor.fallarSi = { it == "POST /api/sesiones" }

        try {
            subidor().sincronizar(id)
            fail("tenía que fallar")
        } catch (e: ErrorApi) {
            assertEquals(503, e.codigo)
        }
        assertEquals("no se prueba con menos datos", 1, postsCrear())
        val subida = almacen.leerSubida(id)
        assertFalse(subida.sesionCreada)
        assertFalse(subida.crearSinNumero)
        assertFalse(subida.crearComoAuto)

        servidor.fallarSi = { false }
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertTrue(servidor.cuerpoCrearContiene("\"numero_clase\":8"))
        assertTrue(servidor.cuerpoCrearContiene("\"materia_id\":\"analisis\""))
    }

    @Test
    fun `un 503 despues de rechazar el numero tampoco anota nada`() = runTest {
        nuevaSesion(numeroClase = 8)
        agregarAudio(300)
        terminar()
        servidor.rechazarNumero = true
        var posts = 0
        servidor.fallarSi = { it == "POST /api/sesiones" && ++posts == 2 }

        try {
            subidor().sincronizar(id)
            fail("tenía que fallar")
        } catch (e: ErrorApi) {
            assertEquals(503, e.codigo)
        }
        assertEquals(2, postsCrear())
        val parcial = almacen.leerSubida(id)
        assertFalse(parcial.sesionCreada)
        assertFalse(parcial.crearSinNumero)
        assertFalse(parcial.crearComoAuto)

        // El reintento vuelve a empezar por el cuerpo completo y recién ahí se anota.
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertEquals(4, postsCrear())
        assertTrue(servidor.cuerposCrear.last().let { !it.contains("numero_clase") && it.contains("\"materia_id\":\"analisis\"") })
        assertTrue(almacen.leerSubida(id).crearSinNumero)
        assertFalse(almacen.leerSubida(id).crearComoAuto)
        assertTrue(servidor.sesiones.getValue(id).finalizada)
    }

    @Test
    fun `un token rechazado al crear no degrada la sesion`() = runTest {
        nuevaSesion(numeroClase = 8)
        agregarAudio(100)
        terminar()

        try {
            subidor(token = "otro").sincronizar(id)
            fail("tenía que fallar")
        } catch (e: ErrorAutenticacion) {
            assertEquals(401, e.codigo)
        }
        assertEquals(1, postsCrear())
        val subida = almacen.leerSubida(id)
        assertFalse(subida.crearSinNumero)
        assertFalse(subida.crearComoAuto)

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertTrue(servidor.cuerpoCrearContiene("\"numero_clase\":8"))
    }

    @Test
    fun `sin red no se degrada la sesion con numero`() = runTest {
        nuevaSesion(numeroClase = 8)
        agregarAudio(500)
        terminar()
        val puerto = http.port
        http.shutdown()
        try {
            subidor().sincronizar(id)
            fail("tenía que fallar")
        } catch (e: java.io.IOException) {
            // esperado
        }
        val subida = almacen.leerSubida(id)
        assertFalse(subida.sesionCreada)
        assertFalse(subida.crearSinNumero)
        assertFalse(subida.crearComoAuto)

        http = MockWebServer().apply { dispatcher = servidor; start(puerto) }
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertTrue(servidor.cuerpoCrearContiene("\"numero_clase\":8"))
        assertArrayEquals(audioLocal(), servidor.sesiones.getValue(id).audio.toByteArray())
    }

    @Test
    fun `si el server ya tenia la sesion y rechaza el numero, la subida sigue sin numero`() = runTest {
        nuevaSesion(numeroClase = 8)
        agregarAudio(500)
        assertEquals(Subidor.Resultado.PARCIAL, subidor().sincronizar(id))
        assertTrue(servidor.cuerpoCrearContiene("\"numero_clase\":8"))
        // El server cambió y ahora no acepta el número: la sesión ya existe (200), no se re-sube nada.
        servidor.rechazarNumero = true
        agregarAudio(300)
        terminar()

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertTrue(almacen.leerSubida(id).crearSinNumero)
        assertFalse(almacen.leerSubida(id).crearComoAuto)
        assertArrayEquals(audioLocal(), servidor.sesiones.getValue(id).audio.toByteArray())
        // 500 + 300 bytes: no se volvió a subir desde 0.
        assertEquals(1, servidor.pedidos.count { it.startsWith("PUT") && it.contains("offset=0") })
    }

    private fun postsCrear() = servidor.pedidos.count { it == "POST /api/sesiones" }

    private fun ServidorDePrueba.cuerpoCrearContiene(texto: String) =
        sesiones.getValue(id).cuerpoCrear!!.contains(texto)
}
