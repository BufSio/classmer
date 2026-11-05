package ar.classmer.subida

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Evento
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.Pista
import ar.classmer.modelo.SesionLocal
import ar.classmer.red.ClienteApi
import ar.classmer.util.Tiempo
import ar.classmer.watch.Ambos
import ar.classmer.watch.PistaCelu
import ar.classmer.watch.PistasCelu
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.FileOutputStream
import kotlin.random.Random

/** La subida de una clase grabada con Ambos (§15): la pista del celu, el bloque `celu` y un server viejo. */
class SubidorAmbosTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private lateinit var http: MockWebServer
    private lateinit var servidor: ServidorDePrueba
    private lateinit var almacen: AlmacenSesiones
    private val pistas by lazy { PistasCelu(almacen) }
    private val id = "0194b2c3-d4e5-7f60-8a7b-8c9d0e1f2a3b"
    private val rnd = Random(26)
    private val inicioCelu = Tiempo.epochMs("2026-10-26T14:00:00-03:00")

    @Before
    fun preparar() {
        servidor = ServidorDePrueba()
        http = MockWebServer().apply { dispatcher = servidor; start() }
        almacen = AlmacenSesiones(tmp.newFolder("sesiones"))
    }

    @After
    fun cerrar() = http.shutdown()

    private fun subidor() = Subidor(almacen, ClienteApi(http.url("/").toString(), "tok"), tamanioTramo = 1000)

    private fun nuevaSesion(): SesionLocal {
        val s = SesionLocal(
            id = id, materiaId = "macro", origenMateria = "calendario", tipoClase = "teorica",
            inicio = "2026-10-26T14:00:20-03:00", dispositivo = "galaxy-watch8-classic", numeroClase = 4,
            fuenteAudio = FuenteAudio.RELOJ, pistaCelu = true, estadoLocal = EstadoLocal.RECIBIENDO,
        )
        almacen.crear(s)
        pistas.guardar(id, PistaCelu(inicioMs = inicioCelu))
        return s
    }

    private fun agregar(archivo: java.io.File, n: Int): ByteArray =
        rnd.nextBytes(n).also { b -> FileOutputStream(archivo, true).use { it.write(b) } }

    private fun principal(n: Int) = agregar(almacen.archivoAudio(id), n)
    private fun celu(n: Int) = agregar(pistas.audio(id), n)

    private fun terminar(desfase: Long? = -20_000) {
        pistas.cambiar(id) { it.copy(terminada = true, duracionMs = 61_000, desfaseMs = desfase, desfaseErrorMs = desfase?.let { 23 }) }
        almacen.guardarSesion(almacen.leerSesion(id)!!.copy(estadoLocal = EstadoLocal.TERMINADA, duracionMs = 60_000))
    }

    private fun putsCelu() = servidor.pedidos.filter { it.startsWith("PUT") && it.contains("pista=celu") }

    @Test
    fun `sube la pista del celu aparte y la declara en finalizar`() = runTest {
        nuevaSesion()
        principal(2500)
        celu(3200)
        assertEquals(Subidor.Resultado.PARCIAL, subidor().sincronizar(id))
        val s = servidor.sesiones.getValue(id)
        assertArrayEquals(almacen.archivoAudio(id).readBytes(), s.audio.toByteArray())
        assertArrayEquals(pistas.audio(id).readBytes(), s.audioCelu.toByteArray())
        assertTrue("antes de subirla, pregunta qué tiene de esa pista", servidor.pedidos.any { it.startsWith("GET") && it.contains("audio?pista=celu") })
        assertEquals(4, putsCelu().size)

        celu(500)
        terminar()
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertTrue(s.finalizada)
        assertArrayEquals(pistas.audio(id).readBytes(), s.audioCelu.toByteArray())
        val sha = ServidorDePrueba.sha256(pistas.audio(id).readBytes())
        assertTrue(
            s.cuerpoFinalizar!!,
            s.cuerpoFinalizar!!.contains("\"celu\":{\"sha256\":\"$sha\",\"bytes_totales\":3700,\"duracion_ms\":61000,\"desfase_ms\":-20000}"),
        )
        assertEquals(http.url("/").toString(), almacen.leerSubida(id).celuSoportadaEn)
    }

    @Test
    fun `no sube la pista mientras el principal no tiene bytes en el server`() = runTest {
        // Sin bytes del principal, un server viejo y uno nuevo contestan lo mismo (0) al GET de la pista.
        nuevaSesion()
        celu(3000)
        subidor().sincronizar(id)
        assertTrue(putsCelu().isEmpty())
        assertNull(almacen.leerSubida(id).celuSoportadaEn)
        principal(10)
        subidor().sincronizar(id)
        assertEquals(3, putsCelu().size)
    }

    @Test
    fun `si el server repite la pista, la sube aunque el principal no tenga bytes`() = runTest {
        servidor.ecoPista = true
        nuevaSesion()
        celu(1500)
        subidor().sincronizar(id)
        assertEquals(2, putsCelu().size)
        assertEquals(0, servidor.sesiones.getValue(id).audio.size())
    }

    @Test
    fun `con un server viejo nunca manda la pista y finaliza sin el bloque`() = runTest {
        servidor.separaPistas = false
        nuevaSesion()
        principal(2500)
        celu(3200)
        subidor().sincronizar(id)
        terminar()
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val s = servidor.sesiones.getValue(id)
        assertTrue(putsCelu().isEmpty())
        // El principal quedó intacto: nada de la pista del celu se le pegó.
        assertArrayEquals(almacen.archivoAudio(id).readBytes(), s.audio.toByteArray())
        assertFalse(s.cuerpoFinalizar!!.contains("celu"))
        assertTrue(almacen.leerSubida(id).celuSinSubir)
        // La pista queda en el celu.
        assertEquals(3200L, pistas.audio(id).length())
    }

    @Test
    fun `un 409 faltan_bytes de la pista del celu sigue desde lo que tiene el server`() = runTest {
        nuevaSesion()
        principal(2000)
        celu(3000)
        terminar()
        servidor.perderCeluUnaVez = true
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val s = servidor.sesiones.getValue(id)
        assertArrayEquals(pistas.audio(id).readBytes(), s.audioCelu.toByteArray())
        assertTrue(putsCelu().any { it.contains("offset=1500") })
        assertEquals(2, servidor.pedidos.count { it.startsWith("PUT") && !it.contains("pista") })
    }

    @Test
    fun `hash_distinto de la pista del celu la sube entera sin tocar el principal`() = runTest {
        nuevaSesion()
        principal(2000)
        celu(2500)
        terminar()
        servidor.hashCeluDistintoUnaVez = true
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val s = servidor.sesiones.getValue(id)
        assertArrayEquals(pistas.audio(id).readBytes(), s.audioCelu.toByteArray())
        assertEquals(2, putsCelu().count { it.contains("offset=0") })
        assertEquals(2, servidor.pedidos.count { it.startsWith("PUT") && !it.contains("pista") })
    }

    @Test
    fun `los minutos contados con la pista del celu llegan en el audio del reloj`() = runTest {
        nuevaSesion()
        principal(500)
        almacen.agregarEvento(id, Evento("antes", "marca", 5_000, "x", marca = "importante", pista = Pista.CELU))
        almacen.agregarEvento(id, Evento("despues", "marca", 50_000, "x", marca = "importante", pista = Pista.CELU))
        almacen.agregarEvento(id, Evento("reloj", "nota", 10_000, "x", texto = "del reloj"))
        // Sin el desfase todavía, los de la pista del celu esperan.
        subidor().sincronizar(id)
        val s = servidor.sesiones.getValue(id)
        assertEquals(setOf("reloj"), s.eventos.keys)

        pistas.cambiar(id) { it.copy(desfaseMs = -20_000, desfaseErrorMs = 23) }
        subidor().sincronizar(id)
        assertEquals("0", s.offsets["antes"])
        assertEquals("30000", s.offsets["despues"])
        assertEquals("10000", s.offsets["reloj"])
        terminar()
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
    }

    @Test
    fun `una que quedo como del telefono sube audio_celu_ogg como su audio, sin segunda pista`() = runTest {
        val s0 = nuevaSesion()
        celu(2200)
        pistas.cambiar(id) { it.copy(terminada = true, duracionMs = 61_000) }
        val t = Ambos.comoTelefono(s0, "s23-ultra", pistas.leer(id)).copy(estadoLocal = EstadoLocal.TERMINADA)
        almacen.guardarSesion(t)
        almacen.agregarEvento(id, Evento("e", "marca", 7_000, "x", marca = "importante", pista = Pista.CELU))

        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val s = servidor.sesiones.getValue(id)
        assertArrayEquals(pistas.audio(id).readBytes(), s.audio.toByteArray())
        assertEquals(0, s.audioCelu.size())
        assertTrue(putsCelu().isEmpty())
        assertFalse(s.cuerpoFinalizar!!.contains("celu"))
        assertTrue(s.cuerpoFinalizar!!.contains("\"duracion_ms\":61000"))
        assertEquals("7000", s.offsets["e"])
        assertTrue(s.cuerpoCrear!!.contains("\"dispositivo\":\"s23-ultra\""))
    }

    @Test
    fun `una pista vacia no va al server`() = runTest {
        nuevaSesion()
        principal(1000)
        terminar()
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertFalse(servidor.sesiones.getValue(id).cuerpoFinalizar!!.contains("celu"))
        assertTrue(putsCelu().isEmpty())
    }

    @Test
    fun `sin el desfase medido va el de las horas de los dos equipos`() = runTest {
        nuevaSesion()
        principal(1000)
        celu(1000)
        terminar(desfase = null)
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertTrue(servidor.sesiones.getValue(id).cuerpoFinalizar!!.contains("\"desfase_ms\":-20000"))
    }

    @Test
    fun `si el server vuelve a crear la sesion, pregunta de nuevo si separa las pistas`() = runTest {
        nuevaSesion()
        principal(1000)
        celu(1000)
        subidor().sincronizar(id)
        assertNotNull(almacen.leerSubida(id).celuSoportadaEn)
        // Otro server (o se borraron sus datos): ahora es uno viejo.
        servidor.sesiones.clear()
        servidor.separaPistas = false
        val antes = putsCelu().size
        subidor().sincronizar(id)
        assertEquals(antes, putsCelu().size)
        assertNull(almacen.leerSubida(id).celuSoportadaEn)
        assertEquals(0L, almacen.leerSubida(id).celuBytesConfirmados)
        assertNull(servidor.sesiones.getValue(id).cuerpoFinalizar)
    }

    // ---- Lo que salió de la revisión ----

    @Test
    fun `un server viejo que volvio el principal a cero no se confunde con uno que separa las pistas`() = runTest {
        // Se perdió la respuesta de un hash_distinto: el server tiene 0 y el celu cree que tiene todo. Con lo
        // anotado (2000) el GET de la pista (0) parecería otra pista; con el principal en el momento, no.
        servidor.separaPistas = false
        nuevaSesion()
        principal(2000)
        celu(1500)
        subidor().sincronizar(id)
        servidor.sesiones.getValue(id).audio.reset()
        subidor().sincronizar(id)
        assertTrue(putsCelu().isEmpty())
        assertNull(almacen.leerSubida(id).celuSoportadaEn)
    }

    @Test
    fun `lo que confirmo un server no vale para otro`() = runTest {
        servidor.separaPistas = false
        nuevaSesion()
        principal(2000)
        celu(1500)
        subidor().sincronizar(id)
        // Lo había confirmado otro server (el falso, por ejemplo) y ahora se volvió a uno viejo que ya tenía la clase.
        almacen.guardarSubida(id, almacen.leerSubida(id).copy(celuSoportadaEn = "http://otro-server:8765/", celuBytesConfirmados = 1000))
        subidor().sincronizar(id)
        assertTrue(putsCelu().isEmpty())
        assertArrayEquals(almacen.archivoAudio(id).readBytes(), servidor.sesiones.getValue(id).audio.toByteArray())
    }

    @Test
    fun `si el server rechaza la pista, la clase se finaliza sin ella`() = runTest {
        servidor.rechazarPista = 422
        nuevaSesion()
        principal(2000)
        celu(1500)
        assertEquals(Subidor.Resultado.PARCIAL, subidor().sincronizar(id))
        terminar()
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        val estado = almacen.leerSubida(id)
        assertNotNull(estado.celuRechazada)
        assertTrue(estado.celuSinSubir)
        assertNull(estado.errorPermanente)
        assertFalse(servidor.sesiones.getValue(id).cuerpoFinalizar!!.contains("celu"))
        assertArrayEquals(almacen.archivoAudio(id).readBytes(), servidor.sesiones.getValue(id).audio.toByteArray())
    }

    @Test
    fun `si la pista nunca cierra en finalizar, la clase se finaliza sin ella`() = runTest {
        nuevaSesion()
        principal(2000)
        celu(3000)
        terminar()
        servidor.perderCeluSiempre = true
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertTrue(almacen.leerSubida(id).celuSinSubir)
        assertNotNull(almacen.leerSubida(id).celuRechazada)
        assertFalse(servidor.sesiones.getValue(id).cuerpoFinalizar!!.contains("celu"))
    }

    // ---- Segunda revisión ----

    @Test
    fun `si en la misma URL vuelve un server viejo, se deja de subir la pista`() = runTest {
        nuevaSesion()
        principal(2000)
        celu(1500)
        subidor().sincronizar(id)
        assertEquals(2, putsCelu().size)
        // El server se reinicia con una versión vieja, en la misma URL y con los mismos datos.
        servidor.separaPistas = false
        principal(500)
        celu(1500)
        subidor().sincronizar(id)
        assertEquals(2, putsCelu().size)
        assertNull(almacen.leerSubida(id).celuSoportadaEn)
        assertArrayEquals(almacen.archivoAudio(id).readBytes(), servidor.sesiones.getValue(id).audio.toByteArray())
    }

    @Test
    fun `con la sesion creada de nuevo se olvida que la pista se rechazo`() = runTest {
        nuevaSesion()
        principal(1000)
        celu(1000)
        subidor().sincronizar(id)
        almacen.guardarSubida(id, almacen.leerSubida(id).copy(celuRechazada = "422 en el otro server", celuSinSubir = true))
        servidor.sesiones.clear()
        subidor().sincronizar(id)
        val estado = almacen.leerSubida(id)
        assertNull(estado.celuRechazada)
        assertFalse(estado.celuSinSubir)
        assertArrayEquals(pistas.audio(id).readBytes(), servidor.sesiones.getValue(id).audioCelu.toByteArray())
    }

    @Test
    fun `sin subir se anota recien con el finalizar aceptado`() = runTest {
        // Server viejo y el finalizar falla: todavía no se sabe si va sin la pista.
        servidor.separaPistas = false
        var fallas = 1
        servidor.fallarSi = { it.endsWith("/finalizar") && fallas-- > 0 }
        nuevaSesion()
        principal(1000)
        celu(1000)
        terminar()
        assertFalse(runCatching { subidor().sincronizar(id) }.isSuccess)
        assertFalse(almacen.leerSubida(id).celuSinSubir)
        // Mientras tanto se actualizó el server: va con la pista y no queda el aviso.
        servidor.separaPistas = true
        assertEquals(Subidor.Resultado.COMPLETA, subidor().sincronizar(id))
        assertFalse(almacen.leerSubida(id).celuSinSubir)
        assertTrue(servidor.sesiones.getValue(id).cuerpoFinalizar!!.contains("\"celu\""))
    }
}
