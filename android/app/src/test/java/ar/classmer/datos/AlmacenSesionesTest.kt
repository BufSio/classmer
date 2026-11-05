package ar.classmer.datos

import ar.classmer.modelo.EstadoSubida
import ar.classmer.modelo.Evento
import ar.classmer.modelo.SesionLocal
import ar.classmer.util.Uuid7
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class AlmacenSesionesTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun sesion(id: String) =
        SesionLocal(id, null, "auto", "teorica", "2026-10-15T14:00:05-03:00", "s23-ultra")

    @Test
    fun `eventos se agregan y se leen en orden, sin repetidos ni lineas rotas`() {
        val a = AlmacenSesiones(tmp.root)
        a.crear(sesion("s1"))
        a.agregarEvento("s1", Evento("e1", "marca", 10, "x", marca = "importante"))
        a.agregarEvento("s1", Evento("e2", "nota", 20, "x", texto = "con \"comillas\"\ny salto"))
        a.agregarEvento("s1", Evento("e1", "marca", 10, "x", marca = "importante"))
        a.archivoEventos("s1").appendText("basura\n{\"id\":\"e3\"")
        val eventos = a.leerEventos("s1")
        assertEquals(listOf("e1", "e2"), eventos.map { it.id })
        assertEquals("con \"comillas\"\ny salto", eventos[1].texto)
    }

    @Test
    fun `un evento despues de una escritura cortada no se pega al pedazo roto`() {
        val a = AlmacenSesiones(tmp.root)
        a.crear(sesion("s1"))
        a.archivoEventos("s1").appendText("{\"id\":\"roto\",\"ti")
        a.agregarEvento("s1", Evento("e1", "marca", 10, "x", marca = "importante"))
        assertEquals(listOf("e1"), a.leerEventos("s1").map { it.id })
    }

    @Test
    fun `cerrar huerfanas respeta la sesion activa y la que sigue escribiendo`() {
        val a = AlmacenSesiones(tmp.root)
        a.crear(sesion("vieja"))
        a.crear(sesion("activa"))
        a.crear(sesion("reciente"))
        val ahora = System.currentTimeMillis()
        listOf("vieja", "activa").forEach {
            a.archivoSesion(it).setLastModified(ahora - 10 * 60_000)
            a.archivoAudio(it).apply { writeBytes(ByteArray(10)) }.setLastModified(ahora - 10 * 60_000)
        }
        val cerradas = ar.classmer.grabacion.Recuperacion.cerrarHuerfanas(a, activa = "activa", inactividadMs = 2 * 60_000, ahoraMs = ahora)
        assertEquals(listOf("vieja"), cerradas.map { it.first.id })
        assertTrue(a.leerSesion("vieja")!!.let { it.terminada && it.interrumpida })
        assertFalse(a.leerSesion("activa")!!.terminada)
        assertFalse(a.leerSesion("reciente")!!.terminada)
    }

    @Test
    fun `listar devuelve la mas nueva primero y tolera carpetas rotas`() {
        val a = AlmacenSesiones(tmp.root)
        val vieja = Uuid7.nuevo(1_000_000)
        val nueva = Uuid7.nuevo(2_000_000)
        a.crear(sesion(vieja))
        a.crear(sesion(nueva))
        File(tmp.root, "carpeta-sin-sesion").mkdirs()
        File(tmp.root, "rota").mkdirs()
        File(tmp.root, "rota/sesion.json").writeText("{no es json")
        assertEquals(listOf(nueva, vieja), a.listar().map { it.id })
    }

    @Test
    fun `escritura atomica reemplaza el archivo y no deja temporales`() {
        val a = AlmacenSesiones(tmp.root)
        a.crear(sesion("s1"))
        a.guardarSubida("s1", EstadoSubida(bytesConfirmados = 10))
        a.guardarSubida("s1", EstadoSubida(bytesConfirmados = 20, eventosSubidos = setOf("e1")))
        assertEquals(20, a.leerSubida("s1").bytesConfirmados)
        assertEquals(setOf("e1"), a.leerSubida("s1").eventosSubidos)
        assertFalse(File(a.dir("s1"), "subida.json.tmp").exists())
        assertTrue(a.leerSubida("no-existe").let { !it.sesionCreada && it.bytesConfirmados == 0L })
    }

    @Test
    fun `el JSON de sesion lleva los nulls explicitos del contrato`() {
        val a = AlmacenSesiones(tmp.root)
        a.crear(sesion("s1"))
        val texto = a.archivoSesion("s1").readText()
        assertTrue(texto, texto.contains("\"materia_id\":null"))
        assertTrue(texto.contains("\"origen_materia\":\"auto\""))
    }
}
