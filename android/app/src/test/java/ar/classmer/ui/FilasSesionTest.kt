package ar.classmer.ui

import ar.classmer.modelo.EstadoSesionServidor
import ar.classmer.util.JsonClassmer
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class FilasSesionTest {
    private val sid = "0192a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"

    private val base = FilaSesion(
        id = sid,
        materia = "Macro",
        tipoClase = "teorica",
        fecha = "mié 14/10 11:30",
        duracion = "1:30:00",
        estado = "",
        detalle = "Detalle local",
        progreso = null,
        tono = Tono.NORMAL,
        pdf = null,
        numeroClase = 7,
    )

    private fun remota(
        estado: String?,
        error: String? = null,
        etapa: String? = null,
        progreso: Double? = null,
        pdf: String? = null,
        numeroClase: Int? = null,
        espera: String? = null,
        dudas: Int? = null,
    ) = EstadoSesionServidor(
        id = sid,
        estado = estado,
        etapa = etapa,
        progreso = progreso,
        pdf = pdf,
        error = error?.let { JsonPrimitive(it) },
        numeroClase = numeroClase,
        espera = espera,
        dudas = dudas,
    )

    private val conSugerida =
        "No pude determinar la materia (confianza 0.30: habla de inflación y del BCRA). Sugerida: macro. " +
            "Asignala con: uv run classmer asignar $sid --materia <materia> (opciones: analisis, macro)"

    // ---- esperando_usuario ----

    @Test
    fun `esperando_usuario pide elegir la materia desde el celu`() {
        val f = FilasSesion.aplicarEstadoRemoto(base, remota("esperando_usuario", conSugerida))
        assertEquals("Elegí la materia", f.estado)
        assertTrue(f.elegirMateria)
        assertEquals(Tono.ACTIVO, f.tono)
        assertEquals("macro", f.materiaSugerida)
        // El motivo del server primero; el detalle local (por ejemplo, "Terminó sola…") se conserva abajo.
        assertEquals("No pude determinar la materia (confianza 0.30: habla de inflación y del BCRA).\nDetalle local", f.detalle)
        val sinLocal = FilasSesion.aplicarEstadoRemoto(base.copy(detalle = null), remota("esperando_usuario", conSugerida))
        assertEquals("No pude determinar la materia (confianza 0.30: habla de inflación y del BCRA).", sinLocal.detalle)
    }

    @Test
    fun `el motivo no lleva las instrucciones para la PC ni la sugerida`() {
        val motivo = FilasSesion.motivoEsperando(conSugerida)
        assertFalse(motivo.contains("Asignala con:"))
        assertFalse(motivo.contains("uv run"))
        assertFalse(motivo.contains("opciones"))
        assertFalse(motivo.contains("Sugerida"))
        assertEquals("No pude determinar la materia (confianza 0.30: habla de inflación y del BCRA).", motivo)
    }

    @Test
    fun `sin sugerida el motivo queda igual de limpio y no hay materia sugerida`() {
        val error = "No pude determinar la materia (confianza 0.20: no se entiende). " +
            "Asignala con: uv run classmer asignar $sid --materia <materia> (opciones: macro)"
        assertEquals("No pude determinar la materia (confianza 0.20: no se entiende).", FilasSesion.motivoEsperando(error))
        assertNull(FilasSesion.materiaSugerida(error))
        val vacio = "No pude determinar la materia: el transcript está vacío. Asignala con: uv run classmer asignar $sid --materia <materia>"
        assertEquals("No pude determinar la materia: el transcript está vacío.", FilasSesion.motivoEsperando(vacio))
        assertNull(FilasSesion.materiaSugerida(vacio))
    }

    @Test
    fun `sin materias en la config el mensaje queda limpio`() {
        val error = "No hay materias en config.yaml para clasificar. Agregalas y asigná con: uv run classmer asignar $sid --materia <materia>"
        assertEquals("No hay materias en config.yaml para clasificar.", FilasSesion.motivoEsperando(error))
        assertNull(FilasSesion.materiaSugerida(error))
        val f = FilasSesion.aplicarEstadoRemoto(base.copy(detalle = null), remota("esperando_usuario", error))
        assertEquals("No hay materias en config.yaml para clasificar.", f.detalle)
        assertTrue(f.elegirMateria)
        assertNull(f.materiaSugerida)
    }

    @Test
    fun `sin error o con error vacio se usa el motivo por defecto`() {
        val porDefecto = "El server no pudo saber de qué materia es."
        assertEquals(porDefecto, FilasSesion.motivoEsperando(null))
        assertEquals(porDefecto, FilasSesion.motivoEsperando(""))
        assertEquals(porDefecto, FilasSesion.motivoEsperando("   "))
        // Si el server sólo manda las instrucciones para la PC, tampoco queda nada que mostrar.
        assertEquals(porDefecto, FilasSesion.motivoEsperando("Asignala con: uv run classmer asignar $sid --materia <materia>"))
        assertNull(FilasSesion.materiaSugerida(null))
        assertNull(FilasSesion.materiaSugerida(""))

        val f = FilasSesion.aplicarEstadoRemoto(base.copy(detalle = null), remota("esperando_usuario"))
        assertEquals(porDefecto, f.detalle)
        assertTrue(f.elegirMateria)
        assertNull(f.materiaSugerida)
        // Un error JSON null cuenta como sin error.
        val conNull = FilasSesion.aplicarEstadoRemoto(base, remota("esperando_usuario").copy(error = JsonNull))
        assertEquals("$porDefecto\nDetalle local", conNull.detalle)
    }

    @Test
    fun `la materia sugerida admite guiones y guiones bajos`() {
        assertEquals("analisis", FilasSesion.materiaSugerida("No pude. Sugerida: analisis. Asignala con: x"))
        assertEquals("teoria_medida2", FilasSesion.materiaSugerida("No pude. Sugerida: teoria_medida2. Asignala con: x"))
    }

    // ---- número de clase ----

    @Test
    fun `el numero de clase del server pisa al local`() {
        val f = FilasSesion.aplicarEstadoRemoto(base, remota("en_cola", numeroClase = 9))
        assertEquals(9, f.numeroClase)
    }

    @Test
    fun `si el server no manda numero se conserva el local`() {
        for (estado in listOf("en_cola", "procesando", "lista", "error", "esperando_usuario", "grabando", "otra_cosa")) {
            val f = FilasSesion.aplicarEstadoRemoto(base, remota(estado, numeroClase = null))
            assertEquals(estado, 7, f.numeroClase)
        }
        val sinLocal = FilasSesion.aplicarEstadoRemoto(base.copy(numeroClase = null), remota("lista"))
        assertNull(sinLocal.numeroClase)
    }

    @Test
    fun `textoNumero muestra Clase N solo con numeros positivos`() {
        assertEquals("Clase 1", FilasSesion.textoNumero(1))
        assertEquals("Clase 12", FilasSesion.textoNumero(12))
        assertNull(FilasSesion.textoNumero(null))
        assertNull(FilasSesion.textoNumero(0))
        assertNull(FilasSesion.textoNumero(-3))
    }

    // ---- los demás estados, como antes ----

    @Test
    fun `lista muestra el pdf del server o la ruta por defecto`() {
        val conPdf = FilasSesion.aplicarEstadoRemoto(base, remota("lista", pdf = "/api/sesiones/$sid/otro.pdf"))
        assertEquals("Lista", conPdf.estado)
        assertEquals(Tono.OK, conPdf.tono)
        assertEquals("/api/sesiones/$sid/otro.pdf", conPdf.pdf)
        assertFalse(conPdf.elegirMateria)
        val sinPdf = FilasSesion.aplicarEstadoRemoto(base, remota("lista"))
        assertEquals("/api/sesiones/$sid/apunte.pdf", sinPdf.pdf)
    }

    @Test
    fun `error muestra el texto del server o conserva el detalle`() {
        val conTexto = FilasSesion.aplicarEstadoRemoto(base, remota("error", "Falló la transcripción"))
        assertEquals("Error", conTexto.estado)
        assertEquals(Tono.ERROR, conTexto.tono)
        assertEquals("Falló la transcripción", conTexto.detalle)
        assertFalse(conTexto.elegirMateria)
        val sinTexto = FilasSesion.aplicarEstadoRemoto(base, remota("error"))
        assertEquals("Detalle local", sinTexto.detalle)
        // En un error no se recortan las instrucciones: es otro mensaje.
        val conInstrucciones = FilasSesion.aplicarEstadoRemoto(base, remota("error", "Algo. Asignala con: uv run x"))
        assertEquals("Algo. Asignala con: uv run x", conInstrucciones.detalle)
    }

    @Test
    fun `procesando muestra la etapa y el progreso acotado`() {
        val f = FilasSesion.aplicarEstadoRemoto(base, remota("procesando", etapa = "linea_de_tiempo", progreso = 0.42))
        assertEquals("Procesando · linea de tiempo", f.estado)
        assertEquals(0.42f, f.progreso!!, 1e-6f)
        assertEquals(Tono.NORMAL, f.tono)
        assertEquals("Detalle local", f.detalle)
        val sinEtapa = FilasSesion.aplicarEstadoRemoto(base, remota("procesando", progreso = 1.7))
        assertEquals("Procesando", sinEtapa.estado)
        assertEquals(1f, sinEtapa.progreso!!, 0f)
        val negativo = FilasSesion.aplicarEstadoRemoto(base, remota("procesando", progreso = -0.5))
        assertEquals(0f, negativo.progreso!!, 0f)
        assertNull(FilasSesion.aplicarEstadoRemoto(base, remota("procesando")).progreso)
    }

    @Test
    fun `en_cola, grabando y estados desconocidos`() {
        val enCola = FilasSesion.aplicarEstadoRemoto(base, remota("en_cola"))
        assertEquals("En cola", enCola.estado)
        assertEquals(Tono.NORMAL, enCola.tono)
        assertFalse(enCola.elegirMateria)
        val grabando = FilasSesion.aplicarEstadoRemoto(base, remota("grabando"))
        assertEquals("Recibiendo audio", grabando.estado)
        assertEquals(Tono.NORMAL, grabando.tono)
        val desconocido = FilasSesion.aplicarEstadoRemoto(base, remota("archivada"))
        assertEquals("archivada", desconocido.estado)
        assertEquals(Tono.OK, desconocido.tono)
        val sinEstado = FilasSesion.aplicarEstadoRemoto(base, remota(null))
        assertEquals("Subida", sinEstado.estado)
        assertEquals(Tono.OK, sinEstado.tono)
    }

    // ---- Clases recientes ----

    @Test
    fun `una importada vieja que todavia necesita algo se ve aunque haya 20 clases mas nuevas`() {
        val dia = 24 * 3_600_000L
        val nuevas = (1..25).map { i -> base.copy(id = "nueva-$i", inicioMs = 100 * dia + i * dia) }
        // Del 8/10, importada hoy: se está copiando (o falló, o espera la materia).
        val importada = base.copy(id = "importada", inicioMs = 10 * dia, importada = true, importacionFallida = true)
        val vieja = base.copy(id = "vieja", inicioMs = 5 * dia)

        val filas = FilasSesion.recientes(nuevas + vieja + importada, max = 20, siempre = setOf("importada"))

        assertEquals(21, filas.size)
        assertEquals((25 downTo 6).map { "nueva-$it" } + "importada", filas.map { it.id })
        // Sin nada pendiente, las 20 más recientes, como siempre.
        assertEquals((25 downTo 6).map { "nueva-$it" }, FilasSesion.recientes(nuevas + vieja + importada, 20, emptySet()).map { it.id })
    }

    @Test
    fun `una sesion que deja de estar esperando_usuario ya no ofrece elegir la materia`() {
        val esperando = FilasSesion.aplicarEstadoRemoto(base, remota("esperando_usuario", conSugerida))
        assertTrue(esperando.elegirMateria)
        // La fila se arma de nuevo desde la base en cada refresco: el estado nuevo no arrastra el selector.
        val enCola = FilasSesion.aplicarEstadoRemoto(base, remota("en_cola"))
        assertFalse(enCola.elegirMateria)
        assertNull(enCola.materiaSugerida)
    }

    // ---- La revisión del transcript (§15) ----

    @Test
    fun `esperando la revision muestra cuantas dudas hay para revisar en el panel, sin el selector de materia`() {
        val f = FilasSesion.aplicarEstadoRemoto(base, remota("esperando_usuario", espera = "revision", dudas = 3))
        assertEquals("Revisá el transcript", f.estado)
        assertEquals("Hay 3 dudas para revisar en el panel.\nDetalle local", f.detalle)
        assertTrue(f.revisarEnPanel)
        assertFalse(f.elegirMateria)
        assertNull(f.materiaSugerida)
        assertEquals(Tono.ACTIVO, f.tono)
        assertEquals("Hay 1 duda para revisar en el panel.", FilasSesion.textoDudas(1))
        assertEquals("El transcript espera tu revisión en el panel.", FilasSesion.textoDudas(null))
        assertEquals("El transcript espera tu revisión en el panel.", FilasSesion.textoDudas(0))
    }

    @Test
    fun `esperando la materia, o sin espera (server viejo), es la espera de siempre`() {
        for (espera in listOf("materia", null)) {
            val f = FilasSesion.aplicarEstadoRemoto(base, remota("esperando_usuario", conSugerida, espera = espera))
            assertEquals("Elegí la materia", f.estado)
            assertTrue(f.elegirMateria)
            assertFalse(f.revisarEnPanel)
        }
    }

    @Test
    fun `espera y dudas llegan del server y un server viejo no los manda`() {
        val conRevision = JsonClassmer.decodeFromString(
            EstadoSesionServidor.serializer(),
            """{"id":"$sid","estado":"esperando_usuario","espera":"revision","dudas":2}""",
        )
        assertEquals("revision", conRevision.espera)
        assertEquals(2, conRevision.dudas)
        val viejo = JsonClassmer.decodeFromString(EstadoSesionServidor.serializer(), """{"id":"$sid","estado":"esperando_usuario"}""")
        assertNull(viejo.espera)
        assertNull(viejo.dudas)
    }
}
