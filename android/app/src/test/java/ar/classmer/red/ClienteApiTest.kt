package ar.classmer.red

import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.AdjuntoServidor
import ar.classmer.modelo.CrearSesion
import ar.classmer.modelo.EstadoSubida
import ar.classmer.modelo.Evento
import ar.classmer.modelo.Finalizar
import ar.classmer.modelo.FinalizarCelu
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.TIPOS_CLASE_POR_DEFECTO
import ar.classmer.modelo.paraCrear
import ar.classmer.util.JsonClassmer
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

class ClienteApiTest {
    private lateinit var http: MockWebServer
    private lateinit var api: ClienteApi

    @Before
    fun preparar() {
        http = MockWebServer().apply { start() }
        api = ClienteApi(http.url("/").toString(), "tok")
    }

    @After
    fun cerrar() = http.shutdown()

    private fun responder(codigo: Int, cuerpo: String = "") = http.enqueue(
        MockResponse().setResponseCode(codigo).setHeader("Content-Type", "application/json").setBody(cuerpo),
    )

    private fun pedido(): RecordedRequest = http.takeRequest(5, TimeUnit.SECONDS) ?: error("no llegó ningún pedido")

    private suspend inline fun <reified T : Throwable> esperarError(crossinline bloque: suspend () -> Unit): T {
        try {
            bloque()
        } catch (e: Throwable) {
            if (e is T) return e
            throw AssertionError("se esperaba ${T::class.simpleName} y llegó ${e.javaClass.simpleName}: ${e.message}", e)
        }
        fail("tenía que fallar con ${T::class.simpleName}")
        throw AssertionError()
    }

    private fun sesionLocal(numeroClase: Int?, materiaId: String? = "analisis", origen: String = "calendario") =
        SesionLocal(
            id = "01a137d8-617d-718c-a700-3328dc9a1fec", materiaId = materiaId, origenMateria = origen,
            tipoClase = "teorica", inicio = "2026-10-15T14:00:05-03:00", dispositivo = "s23-ultra",
            numeroClase = numeroClase,
        )

    // ---- GET /api/materias ----

    @Test
    fun `materias lee proximo_numero e ignora las claves que no conoce`() = runTest {
        responder(
            200,
            """
            [{"id": "analisis", "nombre": "Análisis Matemático II",
              "calendario": {"calendario": null, "titulo_regex": "^An[aá]lisis Matem[aá]tico\\b"},
              "tipos_clase": ["teorica", "practica"], "proximo_numero": 8,
              "activa": true, "otro": {"anidado": [1, 2, {"x": null}]}, "numero_clase_inicial": 1},
             {"id": "prueba", "nombre": "Prueba (Classmer)", "activa": false},
             {"id": "macro", "nombre": "Macro", "tipos_clase": ["teorica"], "proximo_numero": null}]
            """.trimIndent(),
        )

        val materias = api.materias()

        assertEquals(listOf("analisis", "prueba", "macro"), materias.map { it.id })
        assertEquals(8, materias[0].proximoNumero)
        assertEquals("^An[aá]lisis Matem[aá]tico\\b", materias[0].calendario?.tituloRegex)
        assertNull("sin proximo_numero queda null", materias[1].proximoNumero)
        assertEquals(TIPOS_CLASE_POR_DEFECTO, materias[1].tiposClase)
        assertNull(materias[2].proximoNumero)
        assertEquals(listOf("teorica"), materias[2].tiposClase)
        val p = pedido()
        assertEquals("GET", p.method)
        assertEquals("/api/materias", p.path)
        assertEquals("Bearer tok", p.getHeader("Authorization"))
    }

    // ---- PATCH /api/materias/{id} ----

    @Test
    fun `cambiarProximoNumero manda PATCH con el numero y devuelve la materia`() = runTest {
        responder(
            200,
            """{"id": "analisis", "nombre": "Análisis Matemático II", "tipos_clase": ["teorica"],
               "proximo_numero": 12, "numero_clase_inicial": 5, "activa": true}""",
        )

        val materia = api.cambiarProximoNumero("analisis", 12)

        assertNotNull(materia)
        assertEquals("analisis", materia!!.id)
        assertEquals(12, materia.proximoNumero)
        assertEquals(listOf("teorica"), materia.tiposClase)
        val p = pedido()
        assertEquals("PATCH", p.method)
        assertEquals("/api/materias/analisis", p.path)
        assertEquals("""{"proximo_numero":12}""", p.body.readUtf8())
        assertEquals("Bearer tok", p.getHeader("Authorization"))
        assertTrue(p.getHeader("Content-Type")!!.startsWith("application/json"))
    }

    @Test
    fun `cambiarProximoNumero codifica el id de la materia en la ruta`() = runTest {
        responder(200, """{"id": "a b", "nombre": "A B", "proximo_numero": 3}""")

        assertEquals(3, api.cambiarProximoNumero("a b", 3)?.proximoNumero)
        assertEquals("/api/materias/a%20b", pedido().path)
    }

    @Test
    fun `cambiarProximoNumero con una respuesta que no se puede leer devuelve null`() = runTest {
        responder(200, "listo")
        responder(200, """{"ok": true}""")
        responder(200, "[]")
        responder(204)

        assertNull("no es JSON", api.cambiarProximoNumero("analisis", 4))
        assertNull("JSON sin los campos de la materia", api.cambiarProximoNumero("analisis", 4))
        assertNull("una lista", api.cambiarProximoNumero("analisis", 4))
        assertNull("sin cuerpo", api.cambiarProximoNumero("analisis", 4))
        repeat(4) { assertEquals("""{"proximo_numero":4}""", pedido().body.readUtf8()) }
    }

    @Test
    fun `cambiarProximoNumero 404 y 422 tiran ErrorApi con ese codigo`() = runTest {
        responder(404, """{"detail": "materia desconocida"}""")
        responder(422, """{"detail": [{"loc": ["body", "proximo_numero"], "msg": "debe ser mayor que 0", "type": "value_error"}]}""")

        val noExiste = esperarError<ErrorApi> { api.cambiarProximoNumero("vieja", 3) }
        assertEquals(404, noExiste.codigo)
        assertFalse(noExiste is ErrorAutenticacion)
        assertTrue(noExiste.message!!.contains("materia desconocida"))

        val invalido = esperarError<ErrorApi> { api.cambiarProximoNumero("analisis", 0) }
        assertEquals(422, invalido.codigo)
        assertFalse(invalido is ErrorAutenticacion)
        assertTrue(invalido.permanente)
        assertEquals("proximo_numero: debe ser mayor que 0", detalleDe(invalido.cuerpo))
    }

    @Test
    fun `cambiarProximoNumero 401 tira ErrorAutenticacion`() = runTest {
        responder(401, """{"detail": "token"}""")

        val e = esperarError<ErrorAutenticacion> { api.cambiarProximoNumero("analisis", 9) }
        assertEquals(401, e.codigo)
    }

    // ---- POST /api/sesiones/{id}/materia ----

    @Test
    fun `asignarMateria manda POST y con 200 devuelve la sesion`() = runTest {
        responder(
            200,
            """{"id": "s1", "materia_id": "analisis", "estado": "en_cola", "etapa": null,
               "numero_clase": 9, "titulo": null, "campo_nuevo": [1, 2]}""",
        )

        val r = api.asignarMateria("s1", "analisis")

        assertTrue(r is ResultadoAsignar.Ok)
        val sesion = (r as ResultadoAsignar.Ok).sesion
        assertNotNull(sesion)
        assertEquals("s1", sesion!!.id)
        assertEquals("analisis", sesion.materiaId)
        assertEquals("en_cola", sesion.estado)
        assertEquals(9, sesion.numeroClase)
        val p = pedido()
        assertEquals("POST", p.method)
        assertEquals("/api/sesiones/s1/materia", p.path)
        assertEquals("""{"materia_id":"analisis"}""", p.body.readUtf8())
        assertEquals("Bearer tok", p.getHeader("Authorization"))
        assertTrue(p.getHeader("Content-Type")!!.startsWith("application/json"))
    }

    @Test
    fun `asignarMateria con 200 y un cuerpo raro devuelve Ok sin sesion`() = runTest {
        responder(200, "hecho")
        responder(200, """{"estado": "en_cola"}""")
        responder(200, "[]")
        responder(200)

        repeat(4) { assertEquals(ResultadoAsignar.Ok(null), api.asignarMateria("s1", "macro")) }
    }

    @Test
    fun `asignarMateria 409 es YaEmpezo con el detail`() = runTest {
        responder(409, """{"detail": "el apunte ya empezó"}""")

        assertEquals(ResultadoAsignar.YaEmpezo("el apunte ya empezó"), api.asignarMateria("s1", "macro"))
    }

    @Test
    fun `asignarMateria 422 es MateriaInvalida, con detail string o lista de FastAPI`() = runTest {
        responder(422, """{"detail": "materia desconocida: vieja"}""")
        responder(422, """{"detail": [{"loc": ["body", "materia_id"], "msg": "no existe", "type": "value_error"}]}""")

        assertEquals(ResultadoAsignar.MateriaInvalida("materia desconocida: vieja"), api.asignarMateria("s1", "vieja"))
        assertEquals(ResultadoAsignar.MateriaInvalida("materia_id: no existe"), api.asignarMateria("s1", "vieja"))
    }

    @Test
    fun `asignarMateria 404, 500 y 401 son errores`() = runTest {
        responder(404, """{"detail": "sesión desconocida"}""")
        responder(500, "Internal Server Error")
        responder(401, """{"detail": "token"}""")

        val noExiste = esperarError<ErrorApi> { api.asignarMateria("otra", "macro") }
        assertEquals(404, noExiste.codigo)
        assertFalse(noExiste is ErrorAutenticacion)
        assertEquals(500, esperarError<ErrorApi> { api.asignarMateria("s1", "macro") }.codigo)
        assertEquals(401, esperarError<ErrorAutenticacion> { api.asignarMateria("s1", "macro") }.codigo)
    }

    // ---- Estado de las sesiones ----

    @Test
    fun `el estado de la sesion trae numero_clase`() = runTest {
        responder(200, """{"id": "s1", "estado": "lista", "numero_clase": 7, "titulo": "Clase 7", "otro": 1}""")
        responder(
            200,
            """{"sesiones": [{"id": "s1", "estado": "lista", "numero_clase": 7},
                             {"id": "s2", "estado": "esperando_usuario", "numero_clase": null},
                             {"id": "s3", "estado": "grabando"}]}""",
        )

        val una = api.sesion("s1")
        assertEquals(7, una.numeroClase)
        assertEquals("Clase 7", una.titulo)

        val lista = api.sesiones()
        assertEquals(listOf(7, null, null), lista.map { it.numeroClase })
        assertEquals("esperando_usuario", lista[1].estado)
    }

    // ---- POST /api/sesiones ----

    @Test
    fun `CrearSesion sin numero no lleva la clave numero_clase`() = runTest {
        val auto = sesionLocal(numeroClase = null, materiaId = null, origen = "auto").paraCrear()
        val json = JsonClassmer.encodeToString(CrearSesion.serializer(), auto)
        assertFalse(json, json.contains("numero_clase"))
        assertTrue("materia_id null sigue yendo explícito", json.contains("\"materia_id\":null"))

        responder(201, """{"id": "x", "estado": "grabando"}""")
        assertTrue(api.crearSesion(auto))
        val cuerpo = pedido().body.readUtf8()
        assertFalse(cuerpo, cuerpo.contains("numero_clase"))
        assertTrue(cuerpo.contains("\"origen_materia\":\"auto\""))
    }

    @Test
    fun `CrearSesion con numero lo manda`() = runTest {
        val conNumero = sesionLocal(numeroClase = 8).paraCrear()
        val json = JsonClassmer.encodeToString(CrearSesion.serializer(), conNumero)
        assertTrue(json, json.contains("\"numero_clase\":8"))
        assertFalse("los campos locales no van", json.contains("estado_local"))

        responder(200, """{"id": "x", "estado": "grabando"}""")
        assertFalse("200 = ya existía", api.crearSesion(conNumero))
        val cuerpo = pedido().body.readUtf8()
        assertTrue(cuerpo, cuerpo.contains("\"numero_clase\":8"))
        assertTrue(cuerpo.contains("\"materia_id\":\"analisis\""))
    }

    @Test
    fun `una SesionLocal de v0_1 se lee con los valores por defecto`() {
        val vieja = """
            {"id": "s1", "materia_id": "analisis", "origen_materia": "calendario", "tipo_clase": "teorica",
             "inicio": "2026-10-08T14:00:05-03:00", "dispositivo": "s23-ultra", "estado_local": "terminada",
             "duracion_ms": 5400000, "fin": "2026-10-08T15:30:05-03:00", "nombre_materia": "Análisis Matemático",
             "evento_calendario": "Análisis Matemático", "interrumpida": false, "continua_de": null}
        """.trimIndent()

        val s = JsonClassmer.decodeFromString(SesionLocal.serializer(), vieja)

        assertNull(s.numeroClase)
        assertNull(s.finEventoMs)
        assertNull(s.preguntarCorteMs)
        assertFalse(s.corteAutomatico)
        assertTrue(s.terminada)
        assertFalse(JsonClassmer.encodeToString(CrearSesion.serializer(), s.paraCrear()).contains("numero_clase"))

        // subida.json de v0.1: sin crear_sin_numero.
        val subida = JsonClassmer.decodeFromString(
            EstadoSubida.serializer(),
            """{"sesion_creada": true, "bytes_confirmados": 10, "crear_como_auto": false}""",
        )
        assertFalse(subida.crearSinNumero)
        assertTrue(subida.sesionCreada)
    }

    @Test
    fun `SesionLocal con los campos nuevos ida y vuelta`() {
        val s = sesionLocal(numeroClase = 8).copy(finEventoMs = 1_760_545_800_000, preguntarCorteMs = 1_760_546_700_000, corteAutomatico = true)
        val leida = JsonClassmer.decodeFromString(SesionLocal.serializer(), JsonClassmer.encodeToString(SesionLocal.serializer(), s))
        assertEquals(s, leida)
    }

    // ---- Adjuntos ----

    @Test
    fun `el estado de la sesion se lee con y sin los campos de adjuntos`() = runTest {
        responder(200, """{"id": "s1", "estado": "lista", "numero_clase": 7}""")
        responder(
            200,
            """{"id": "s1", "estado": "lista", "adjuntos_sin_usar": 1, "otro_campo_nuevo": {"x": 1},
               "adjuntos": [
                 {"id": "a1", "nombre": "Slides_clase_5.pdf", "nombre_original": "Slides clase 5.pdf", "tipo": "pdf",
                  "bytes": 1234567, "offset_ms": 2040000, "creado": "2026-10-22T15:03:10-03:00", "estado": "usado",
                  "paginas": 23},
                 {"id": "a2", "estado": "sin_usar"},
                 {"id": "a3", "nombre": "pizarron.jpg", "offset_ms": null, "estado": null}]}""",
        )
        responder(200, """{"id": "s1", "estado": "lista", "adjuntos": null, "adjuntos_sin_usar": null}""")

        val vieja = api.sesion("s1")
        assertEquals(emptyList<AdjuntoServidor>(), vieja.adjuntos)
        assertEquals(0, vieja.adjuntosSinUsar)

        val nueva = api.sesion("s1")
        assertEquals(1, nueva.adjuntosSinUsar)
        assertEquals(listOf("a1", "a2", "a3"), nueva.adjuntos.map { it.id })
        val a1 = nueva.adjuntos[0]
        assertEquals("Slides_clase_5.pdf", a1.nombre)
        assertEquals("Slides clase 5.pdf", a1.nombreOriginal)
        assertEquals("pdf", a1.tipo)
        assertEquals(1234567L, a1.bytes)
        assertEquals(2040000L, a1.offsetMs)
        assertEquals("usado", a1.estado)
        assertEquals(AdjuntoServidor("a2", estado = "sin_usar"), nueva.adjuntos[1])
        assertNull(nueva.adjuntos[2].offsetMs)

        val conNulls = api.sesion("s1")
        assertEquals("null en una lista se toma como vacía", emptyList<AdjuntoServidor>(), conNulls.adjuntos)
        assertEquals(0, conNulls.adjuntosSinUsar)
    }

    @Test
    fun `regenerar manda POST y con 200 devuelve la sesion en cola`() = runTest {
        responder(200, """{"id": "s1", "estado": "en_cola", "etapa": "apunte", "adjuntos_sin_usar": 0, "adjuntos": [{"id": "a1", "estado": "pendiente"}]}""")

        val r = api.regenerar("s1")

        assertTrue(r is ResultadoRegenerar.Ok)
        val sesion = (r as ResultadoRegenerar.Ok).sesion!!
        assertEquals("en_cola", sesion.estado)
        assertEquals("pendiente", sesion.adjuntos.single().estado)
        val p = pedido()
        assertEquals("POST", p.method)
        assertEquals("/api/sesiones/s1/regenerar", p.path)
        assertEquals("{}", p.body.readUtf8())
        assertEquals("Bearer tok", p.getHeader("Authorization"))
    }

    @Test
    fun `regenerar con 200 y un cuerpo raro igual es Ok`() = runTest {
        responder(200, "listo")
        assertEquals(ResultadoRegenerar.Ok(null), api.regenerar("s1"))
    }

    @Test
    fun `regenerar 409 trae el detail`() = runTest {
        responder(409, """{"detail": "ya se está procesando"}""")
        assertEquals(ResultadoRegenerar.Rechazado("ya se está procesando"), api.regenerar("s1"))
    }

    @Test
    fun `regenerar en un server sin la ruta es SinSoporte, y una sesion que no existe NoEncontrada`() = runTest {
        responder(404, """{"detail": "Not Found"}""")
        responder(405, """{"detail": "Method Not Allowed"}""")
        responder(404, """{"detail": "sesión no encontrada"}""")

        assertEquals(ResultadoRegenerar.SinSoporte, api.regenerar("s1"))
        assertEquals(ResultadoRegenerar.SinSoporte, api.regenerar("s1"))
        assertEquals(ResultadoRegenerar.NoEncontrada("sesión no encontrada"), api.regenerar("otra"))
    }

    @Test
    fun `regenerar 401 y 500 son errores`() = runTest {
        responder(401, """{"detail": "token inválido"}""")
        responder(500, "Internal Server Error")

        assertEquals(401, esperarError<ErrorAutenticacion> { api.regenerar("s1") }.codigo)
        val e = esperarError<ErrorApi> { api.regenerar("s1") }
        assertEquals(500, e.codigo)
        assertFalse(e is ErrorAutenticacion)
    }

    private fun adjuntoLocal(dir: java.io.File, offsetMs: Long? = 5000): Pair<AdjuntoLocal, java.io.File> {
        val archivo = java.io.File(dir, "a1.pdf").apply { writeBytes("%PDF-1.4 hola".toByteArray()) }
        return AdjuntoLocal("a1", "s1", "Clase 5 (versión final).pdf", "application/pdf", archivo.length(), offsetMs, "2026-10-22T15:03:10-03:00", "a1.pdf") to archivo
    }

    @Test
    fun `subirAdjunto lee el adjunto y la sesion, y clasifica las respuestas`() = runTest {
        val dir = kotlin.io.path.createTempDirectory("adjunto").toFile()
        val (adjunto, archivo) = adjuntoLocal(dir)
        responder(
            201,
            """{"id": "a1", "nombre": "Clase_5_version_final.pdf", "nombre_original": "Clase 5 (versión final).pdf",
               "tipo": "pdf", "bytes": 13, "offset_ms": 5000, "estado": "pendiente",
               "sesion": {"id": "s1", "estado": "grabando", "adjuntos": [{"id": "a1"}], "adjuntos_sin_usar": 0}}""",
        )
        responder(200, """{"id": "a1"}""")
        responder(404, """{"detail": "sesión no encontrada"}""")
        responder(404, """{"detail": "Not Found"}""")
        responder(405, """{"detail": "Method Not Allowed"}""")
        responder(409, """{"detail": "la clase está en error"}""")
        responder(413, """{"detail": "archivo de más de 100 MB"}""")
        responder(415, """{"detail": "el archivo no es PDF, JPEG, PNG, WebP ni HEIC"}""")
        responder(422, """{"detail": [{"loc": ["body", "id"], "msg": "no es un UUID", "type": "value_error"}]}""")

        val subido = api.subirAdjunto("s1", adjunto, archivo) as ResultadoAdjunto.Subido
        assertTrue(subido.nuevo)
        assertEquals("Clase_5_version_final.pdf", subido.adjunto?.nombre)
        assertEquals("pendiente", subido.adjunto?.estado)
        assertEquals(listOf("a1"), subido.sesion?.adjuntos?.map { it.id })
        val p = pedido()
        assertEquals("POST", p.method)
        assertEquals("/api/sesiones/s1/adjuntos", p.path)
        assertTrue(p.getHeader("Content-Type")!!.startsWith("multipart/form-data"))
        val cuerpo = p.body.readUtf8()
        assertTrue(cuerpo, cuerpo.contains("name=\"archivo\"; filename=\"Clase 5 (versión final).pdf\""))
        assertTrue(cuerpo.contains("Content-Type: application/pdf"))
        assertTrue(cuerpo.contains("name=\"offset_ms\""))

        assertEquals(ResultadoAdjunto.Subido(false, AdjuntoServidor("a1"), null), api.subirAdjunto("s1", adjunto, archivo))
        assertEquals(ResultadoAdjunto.SesionNoEncontrada("sesión no encontrada"), api.subirAdjunto("s1", adjunto, archivo))
        assertEquals(ResultadoAdjunto.SinSoporte, api.subirAdjunto("s1", adjunto, archivo))
        assertEquals(ResultadoAdjunto.SinSoporte, api.subirAdjunto("s1", adjunto, archivo))
        assertEquals(ResultadoAdjunto.Rechazado(409, "la clase está en error"), api.subirAdjunto("s1", adjunto, archivo))
        assertEquals(ResultadoAdjunto.Rechazado(413, "archivo de más de 100 MB"), api.subirAdjunto("s1", adjunto, archivo))
        assertEquals(ResultadoAdjunto.Rechazado(415, "el archivo no es PDF, JPEG, PNG, WebP ni HEIC"), api.subirAdjunto("s1", adjunto, archivo))
        assertEquals(ResultadoAdjunto.Rechazado(422, "id: no es un UUID"), api.subirAdjunto("s1", adjunto, archivo))
        dir.deleteRecursively()
    }

    @Test
    fun `subirAdjunto sin minuto no manda offset_ms, y 503, 429 y 401 son errores`() = runTest {
        val dir = kotlin.io.path.createTempDirectory("adjunto").toFile()
        val (adjunto, archivo) = adjuntoLocal(dir, offsetMs = null)
        responder(503, """{"detail": "ocupado"}""")
        responder(429, """{"detail": "más despacio"}""")
        responder(401, """{"detail": "token inválido"}""")

        assertEquals(503, esperarError<ErrorApi> { api.subirAdjunto("s1", adjunto, archivo) }.codigo)
        val cuerpo = pedido().body.readUtf8()
        assertFalse(cuerpo, cuerpo.contains("offset_ms"))
        assertTrue(cuerpo.contains("name=\"creado\""))
        assertEquals(429, esperarError<ErrorApi> { api.subirAdjunto("s1", adjunto, archivo) }.codigo)
        assertEquals(401, esperarError<ErrorAutenticacion> { api.subirAdjunto("s1", adjunto, archivo) }.codigo)
        dir.deleteRecursively()
    }

    @Test
    fun `consultarAdjunto manda solo el id, sin el archivo, y clasifica igual`() = runTest {
        responder(422, """{"detail": "falta el archivo (campo archivo)"}""")
        responder(200, """{"id": "a1", "estado": "usado", "sesion": {"id": "s1", "estado": "lista"}}""")
        responder(404, """{"detail": "sesión no encontrada"}""")
        responder(404, """{"detail": "Not Found"}""")
        responder(503, """{"detail": "ocupado"}""")

        assertEquals(ResultadoAdjunto.Rechazado(422, "falta el archivo (campo archivo)"), api.consultarAdjunto("s1", "a1"))
        val p = pedido()
        assertEquals("POST", p.method)
        assertEquals("/api/sesiones/s1/adjuntos", p.path)
        assertTrue(p.getHeader("Content-Type")!!.startsWith("multipart/form-data"))
        val cuerpo = p.body.readUtf8()
        assertTrue(cuerpo, cuerpo.contains("name=\"id\""))
        assertFalse("sin el archivo", cuerpo.contains("name=\"archivo\""))
        assertTrue("unos pocos bytes", cuerpo.length < 400)

        val ya = api.consultarAdjunto("s1", "a1") as ResultadoAdjunto.Subido
        assertFalse(ya.nuevo)
        assertEquals("usado", ya.adjunto?.estado)
        assertEquals("lista", ya.sesion?.estado)
        assertEquals(ResultadoAdjunto.SesionNoEncontrada("sesión no encontrada"), api.consultarAdjunto("s1", "a1"))
        assertEquals(ResultadoAdjunto.SinSoporte, api.consultarAdjunto("s1", "a1"))
        assertEquals(503, esperarError<ErrorApi> { api.consultarAdjunto("s1", "a1") }.codigo)
    }

    // ---- Ambos: la pista del celu (§15) ----

    @Test
    fun `la pista del celu va como parametro pista en el GET y el PUT del audio`() = runTest {
        responder(200, """{"bytes_recibidos": 0}""")
        assertEquals(EstadoAudio(0, null), api.estadoAudio("s1", "celu"))
        assertEquals("/api/sesiones/s1/audio?pista=celu", pedido().path)

        responder(200, """{"bytes_recibidos": 10, "pista": "celu"}""")
        assertEquals(EstadoAudio(10, "celu"), api.estadoAudio("s1", "celu"))
        pedido()

        val f = java.io.File.createTempFile("celu", ".ogg").apply { writeBytes(ByteArray(30) { it.toByte() }); deleteOnExit() }
        responder(200, """{"bytes_recibidos": 40}""")
        assertEquals(ResultadoAudio.Ok(40), api.subirAudio("s1", f, 10, 20, "celu"))
        val put = pedido()
        assertEquals("/api/sesiones/s1/audio?offset=10&pista=celu", put.path)
        assertEquals(20L, put.bodySize)

        // Sin pista, como siempre.
        responder(200, """{"bytes_recibidos": 5}""")
        api.subirAudio("s1", f, 0, 5)
        assertEquals("/api/sesiones/s1/audio?offset=0", pedido().path)
    }

    @Test
    fun `un 409 del PUT de la pista sin bytes_recibidos pregunta por esa pista`() = runTest {
        val f = java.io.File.createTempFile("celu", ".ogg").apply { writeBytes(ByteArray(10)); deleteOnExit() }
        responder(409, "{}")
        responder(200, """{"bytes_recibidos": 7}""")
        assertEquals(ResultadoAudio.Conflicto(7), api.subirAudio("s1", f, 0, 10, "celu"))
        pedido()
        assertEquals("/api/sesiones/s1/audio?pista=celu", pedido().path)
    }

    @Test
    fun `finalizar manda el bloque celu solo si lo hay y lee la pista de un 409`() = runTest {
        val sin = Finalizar("a".repeat(64), 100, 1000, 0)
        assertFalse(JsonClassmer.encodeToString(Finalizar.serializer(), sin).contains("celu"))
        val con = sin.copy(celu = FinalizarCelu("b".repeat(64), 200, 2000, -20_000))
        assertTrue(
            JsonClassmer.encodeToString(Finalizar.serializer(), con)
                .endsWith(""""celu":{"sha256":"${"b".repeat(64)}","bytes_totales":200,"duracion_ms":2000,"desfase_ms":-20000}}"""),
        )

        responder(409, """{"motivo": "faltan_bytes", "bytes_recibidos": 120, "pista": "celu"}""")
        assertEquals(ResultadoFinalizar.Conflicto("faltan_bytes", 120, "celu"), api.finalizar("s1", con))
        responder(409, """{"motivo": "faltan_bytes", "bytes_recibidos": 50}""")
        assertEquals(ResultadoFinalizar.Conflicto("faltan_bytes", 50, null), api.finalizar("s1", con))
    }

    @Test
    fun `sesion_json y eventos de siempre no cambian con Ambos`() {
        val s = sesionLocal(4)
        val json = JsonClassmer.encodeToString(SesionLocal.serializer(), s)
        assertFalse(json.contains("pista_celu"))
        assertTrue(JsonClassmer.encodeToString(SesionLocal.serializer(), s.copy(pistaCelu = true)).contains("\"pista_celu\":true"))
        val evento = Evento("e", "marca", 10, "x", marca = "importante")
        assertFalse(JsonClassmer.encodeToString(Evento.serializer(), evento).contains("pista"))
        assertTrue(JsonClassmer.encodeToString(Evento.serializer(), evento.copy(pista = "celu")).contains("\"pista\":\"celu\""))
        // Lo que va al server al crear la sesión no lleva nada de Ambos.
        assertFalse(JsonClassmer.encodeToString(CrearSesion.serializer(), s.copy(pistaCelu = true).paraCrear()).contains("pista"))
    }
}
