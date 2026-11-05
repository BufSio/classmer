package ar.classmer.modelo

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.util.JsonClassmer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Lo que va y viene del server con una clase importada; una grabada manda lo mismo que antes. */
class ContratoImportacionTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val grabada = SesionLocal(
        id = "0192f7c2-0000-7000-8000-000000000001", materiaId = "analisis", origenMateria = "calendario",
        tipoClase = "teorica", inicio = "2026-10-15T14:00:05-03:00", dispositivo = "s23-ultra", numeroClase = 8,
        estadoLocal = EstadoLocal.TERMINADA, duracionMs = 5_400_000, nombreMateria = "Análisis Matemático",
    )

    private fun crear(s: SesionLocal) = JsonClassmer.encodeToString(CrearSesion.serializer(), s.paraCrear())

    @Test
    fun `una clase grabada manda el mismo JSON de siempre`() {
        assertEquals(
            """{"id":"0192f7c2-0000-7000-8000-000000000001","materia_id":"analisis","origen_materia":"calendario",""" +
                """"tipo_clase":"teorica","inicio":"2026-10-15T14:00:05-03:00","dispositivo":"s23-ultra","numero_clase":8}""",
            crear(grabada),
        )
        assertEquals(
            """{"id":"0192f7c2-0000-7000-8000-000000000001","materia_id":null,"origen_materia":"auto",""" +
                """"tipo_clase":"teorica","inicio":"2026-10-15T14:00:05-03:00","dispositivo":"s23-ultra"}""",
            crear(grabada.copy(materiaId = null, origenMateria = "auto", numeroClase = null)),
        )
        // Los campos de importación no se cuelan aunque estén cargados en una que no es importada.
        assertEquals(crear(grabada), crear(grabada.copy(audioNombre = "x.m4a", audioBytes = 10, archivoAudio = "audio.m4a")))
    }

    @Test
    fun `una importada manda importada, audio_nombre y audio_bytes`() {
        val importada = grabada.copy(importada = true, audioNombre = "PTT-20261022-WA0003.opus", audioBytes = 31_457_280, archivoAudio = "audio.opus")
        assertEquals(
            """{"id":"0192f7c2-0000-7000-8000-000000000001","materia_id":"analisis","origen_materia":"calendario",""" +
                """"tipo_clase":"teorica","inicio":"2026-10-15T14:00:05-03:00","dispositivo":"s23-ultra","numero_clase":8,""" +
                """"importada":true,"audio_nombre":"PTT-20261022-WA0003.opus","audio_bytes":31457280}""",
            crear(importada),
        )
    }

    @Test
    fun `el sesion json de una grabada no lleva los campos de importacion y una importada los conserva`() {
        val almacen = AlmacenSesiones(tmp.root)
        almacen.crear(grabada)
        val texto = almacen.archivoSesion(grabada.id).readText()
        listOf("importada", "audio_nombre", "audio_bytes", "archivo_audio").forEach { assertFalse(it, texto.contains(it)) }
        assertEquals("audio.ogg", almacen.archivoAudio(grabada).name)

        val importada = grabada.copy(id = "0192f7c2-0000-7000-8000-000000000002", importada = true, audioNombre = "a.m4a", audioBytes = 10, archivoAudio = "audio.m4a")
        almacen.crear(importada)
        assertEquals(importada, almacen.leerSesion(importada.id))
        assertEquals("audio.m4a", almacen.archivoAudio(importada).name)
    }

    @Test
    fun `finalizar admite la duracion null`() {
        assertEquals(
            """{"sha256":"ab","bytes_totales":10,"duracion_ms":null,"eventos_totales":2}""",
            JsonClassmer.encodeToString(Finalizar.serializer(), Finalizar("ab", 10, null, 2)),
        )
        assertEquals(
            """{"sha256":"ab","bytes_totales":10,"duracion_ms":5400000,"eventos_totales":2}""",
            JsonClassmer.encodeToString(Finalizar.serializer(), Finalizar("ab", 10, 5_400_000, 2)),
        )
    }

    @Test
    fun `una foto sin minuto se guarda y se lee, y un evento de siempre no cambia`() {
        val foto = Evento("f1", TipoEvento.FOTO, null, "2026-10-24T18:00:00-03:00", archivo = "fotos/f1.jpg")
        val linea = JsonClassmer.encodeToString(Evento.serializer(), foto)
        assertFalse(linea, linea.contains("offset_ms"))
        assertNull(JsonClassmer.decodeFromString(Evento.serializer(), linea).offsetMs)
        val marca = Evento("m1", TipoEvento.MARCA, 1234, "x", marca = "importante")
        assertEquals(
            """{"id":"m1","tipo":"marca","offset_ms":1234,"creado":"x","texto":null,"marca":"importante","archivo":null}""",
            JsonClassmer.encodeToString(Evento.serializer(), marca),
        )
    }

    @Test
    fun `importada en las respuestas es opcional (un server viejo no la manda)`() {
        assertFalse(JsonClassmer.decodeFromString(EstadoSesionServidor.serializer(), """{"id":"a","estado":"lista"}""").importada)
        assertTrue(JsonClassmer.decodeFromString(EstadoSesionServidor.serializer(), """{"id":"a","importada":true}""").importada)
        assertFalse(JsonClassmer.decodeFromString(EstadoSesionServidor.serializer(), """{"id":"a","importada":null}""").importada)
    }
}
