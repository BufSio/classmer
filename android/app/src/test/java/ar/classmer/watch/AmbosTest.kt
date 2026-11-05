package ar.classmer.watch

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.grabacion.Recuperacion
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Evento
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.Pista
import ar.classmer.modelo.SesionLocal
import ar.classmer.ui.FilaSesion
import ar.classmer.ui.Tono
import ar.classmer.util.Tiempo
import ar.classmer.watchlink.EstadoGrabacionWatch
import ar.classmer.watchlink.GrabacionWatch
import ar.classmer.watchlink.OrigenGrabacion
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Grabar con los dos (§15): el desfase de la pista del celu, los minutos y qué pasa si el reloj no graba. */
class AmbosTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val almacen by lazy { AlmacenSesiones(tmp.newFolder("sesiones")) }
    private val pistas by lazy { PistasCelu(almacen) }

    /** El celu empezó a las 14:00:00 (al tocar Grabar); el reloj, 20 s después. */
    private val inicioCelu = Tiempo.epochMs("2026-10-22T14:00:00-03:00")
    private val inicioReloj = "2026-10-22T14:00:20-03:00"

    private fun sesion(id: String = "a") = SesionLocal(
        id = id, materiaId = "macro", origenMateria = "calendario", tipoClase = "teorica",
        inicio = inicioReloj, dispositivo = "galaxy-watch8-classic", numeroClase = 4,
        nombreMateria = "Macro", fuenteAudio = FuenteAudio.RELOJ, pistaCelu = true,
    )

    private fun evento(minuto: Long?, pista: String? = null) = Evento("e", "marca", minuto, "x", marca = "importante", pista = pista)

    // ---- El desfase ----

    @Test
    fun `el desfase es lo que grabo el reloj menos lo que grabo el celu en el mismo instante`() {
        // El celu lleva 80 s y el reloj 60 s: el audio del celu empieza 20 s antes que el del reloj.
        val p = Ambos.medir(PistaCelu(inicioMs = inicioCelu), grabadoReloj = 60_000, errorMs = 1_000, grabadoCelu = 80_000)
        assertEquals(-20_000L, p.desfaseMs)
        assertEquals(1_000L, p.desfaseErrorMs)
        // Si el celu empezó después (Grabar con Ambos cuando el reloj ya grababa), es positivo.
        assertEquals(15_000L, Ambos.medir(PistaCelu(inicioMs = 0), 75_000, 30, 60_000).desfaseMs)
    }

    @Test
    fun `antes de la primera pausa se queda con la medicion de menos error`() {
        val estimada = Ambos.medir(PistaCelu(inicioMs = inicioCelu), 60_000, 1_000, 80_400)
        // Llegó el ping: menos error, se toma.
        val conPing = Ambos.medir(estimada, 90_000, 23, 110_020)
        assertEquals(-20_020L, conPing.desfaseMs)
        assertEquals(23L, conPing.desfaseErrorMs)
        // Una con más error no la pisa.
        assertEquals(conPing, Ambos.medir(conPing, 120_000, 500, 140_000))
    }

    @Test
    fun `un salto mas grande que los errores no reemplaza al desfase aunque tenga menos error`() {
        // El reloj pausó 30 s lejos del celu, que siguió grabando: no es una medición mejor.
        val p = PistaCelu(inicioMs = inicioCelu, desfaseMs = -20_000, desfaseErrorMs = 40)
        val saltado = Ambos.medir(p, 600_000, 20, 650_000)
        assertEquals(-20_000L, saltado.desfaseMs)
        assertEquals(30_000L, saltado.corrimientoMaxMs)
    }

    @Test
    fun `despues de una pausa el desfase queda fijo y solo se anota cuanto se corrio`() {
        val fijo = PistaCelu(inicioMs = inicioCelu, desfaseMs = -20_000, desfaseErrorMs = 23, desfaseFijo = true)
        val corrido = Ambos.medir(fijo, 600_000, 10, 620_150)
        assertEquals(-20_000L, corrido.desfaseMs)
        assertEquals(150L, corrido.corrimientoMaxMs)
        // Se queda con el mayor.
        assertEquals(150L, Ambos.medir(corrido, 700_000, 10, 720_050).corrimientoMaxMs)
        assertEquals(400L, Ambos.medir(corrido, 700_000, 10, 719_600).corrimientoMaxMs)
    }

    @Test
    fun `sin medicion, el desfase sale de las horas de los dos equipos`() {
        val p = PistaCelu(inicioMs = inicioCelu)
        assertEquals(-20_000L, Ambos.desfaseFinal(p, Tiempo.epochMs(inicioReloj)))
        assertNull(Ambos.desfaseFinal(p, null))
        assertEquals(-19_980L, Ambos.desfaseFinal(p.copy(desfaseMs = -19_980), Tiempo.epochMs(inicioReloj)))
        // La hora del reloj sale de lo último que informó (watch.json).
        val s = sesion().also { almacen.crear(it) }
        pistas.guardar(s.id, p)
        RegistrosWatch(almacen).guardar(
            s.id,
            RegistroWatch(
                grabacion = GrabacionWatch(
                    id = s.id, estado = EstadoGrabacionWatch.GRABANDO, origen = OrigenGrabacion.CELU,
                    inicio = "2026-10-22T14:00:25-03:00", bytes = 0, acumuladoMs = 0,
                ),
            ),
        )
        // Mientras graba, sólo el medido (los eventos que lo necesitan esperan); cerrada, también el de las horas.
        assertNull(Ambos.desfase(almacen, s))
        val cerrada = s.copy(estadoLocal = EstadoLocal.TERMINADA)
        assertEquals(-25_000L, Ambos.desfase(almacen, cerrada))
        // Una que no es de Ambos no tiene desfase.
        assertNull(Ambos.desfase(almacen, cerrada.copy(pistaCelu = false)))
    }

    // ---- Los minutos de los eventos ----

    @Test
    fun `un minuto contado con la pista del celu pasa al audio del reloj con el desfase`() {
        val s = sesion()
        assertEquals(30_000L, Ambos.paraSubir(evento(50_000, Pista.CELU), s, -20_000)!!.offsetMs)
        // Antes de que empezara el reloj: queda en 0.
        assertEquals(0L, Ambos.paraSubir(evento(5_000, Pista.CELU), s, -20_000)!!.offsetMs)
        // Los del reloj quedan como están, y la pista no va al server.
        val delReloj = Ambos.paraSubir(evento(10_000), s, -20_000)!!
        assertEquals(10_000L, delReloj.offsetMs)
        assertNull(Ambos.paraSubir(evento(50_000, Pista.CELU), s, -20_000)!!.pista)
    }

    @Test
    fun `si todavia no se sabe el desfase el evento espera, y con la clase cerrada va como esta`() {
        assertNull(Ambos.paraSubir(evento(50_000, Pista.CELU), sesion(), null))
        val cerrada = sesion().copy(estadoLocal = EstadoLocal.TERMINADA)
        assertEquals(50_000L, Ambos.paraSubir(evento(50_000, Pista.CELU), cerrada, null)!!.offsetMs)
    }

    @Test
    fun `en una que quedo como del telefono, los minutos del reloj pasan a la pista del celu`() {
        val telefono = Ambos.comoTelefono(sesion(), "s23-ultra", PistaCelu(inicioMs = inicioCelu))
        assertEquals(30_000L, Ambos.paraSubir(evento(10_000), telefono, -20_000)!!.offsetMs)
        assertEquals(5_000L, Ambos.paraSubir(evento(5_000, Pista.CELU), telefono, -20_000)!!.offsetMs)
        // El reloj nunca grabó (no hay desfase): todos los minutos son de la pista del celu.
        assertEquals(10_000L, Ambos.paraSubir(evento(10_000), telefono.copy(estadoLocal = EstadoLocal.TERMINADA), null)!!.offsetMs)
    }

    @Test
    fun `una clase de siempre no cambia, ni una foto sin minuto`() {
        val telefono = sesion().copy(fuenteAudio = FuenteAudio.TELEFONO, pistaCelu = false)
        assertEquals(10_000L, Ambos.paraSubir(evento(10_000), telefono, null)!!.offsetMs)
        assertNull(Ambos.paraSubir(evento(null), sesion(), null)!!.offsetMs)
    }

    // ---- Si el reloj no graba ----

    @Test
    fun `como del telefono el audio es la pista del celu y la clase empieza con ella`() {
        val p = PistaCelu(inicioMs = inicioCelu, duracionMs = 5_400_000, terminada = true)
        val t = Ambos.comoTelefono(sesion(), "s23-ultra", p)
        assertEquals(FuenteAudio.TELEFONO, t.fuenteAudio)
        assertEquals("s23-ultra", t.dispositivo)
        assertEquals(Pista.ARCHIVO_CELU, t.archivoAudio)
        assertTrue(t.principalEsCelu)
        assertFalse(t.pistaCelu)
        assertFalse(t.delReloj)
        assertEquals(inicioCelu, Tiempo.epochMs(t.inicio))
        assertEquals(5_400_000L, t.duracionMs)
        assertEquals(almacen.dir(t.id).resolve(Pista.ARCHIVO_CELU), almacen.archivoAudio(t))
    }

    @Test
    fun `si el reloj nunca grabo la clase queda del telefono con el audio del celu`() {
        val s = sesion().copy(estadoLocal = EstadoLocal.RECIBIENDO).also { almacen.crear(it) }
        pistas.audio(s.id).writeBytes(OggDePrueba.bytes(500))
        pistas.guardar(s.id, PistaCelu(inicioMs = inicioCelu, duracionMs = 10_000, terminada = true))

        val c = Ambos.cerrarSinReloj(almacen, s, "s23-ultra", interrumpida = true)!!
        assertEquals(EstadoLocal.TERMINADA, c.estadoLocal)
        assertEquals(FuenteAudio.TELEFONO, c.fuenteAudio)
        assertTrue(c.principalEsCelu)
        // La pista del celu está entera: la clase no se cortó.
        assertFalse(c.interrumpida)
        assertEquals(c, almacen.leerSesion(s.id))
    }

    @Test
    fun `con algo del reloj, queda de Ambos con lo recibido`() {
        val s = sesion().copy(estadoLocal = EstadoLocal.RECIBIENDO).also { almacen.crear(it) }
        almacen.archivoAudio(s.id).writeBytes(OggDePrueba.bytes(300))
        pistas.audio(s.id).writeBytes(OggDePrueba.bytes(500))
        pistas.guardar(s.id, PistaCelu(inicioMs = inicioCelu, desfaseMs = -20_000, duracionMs = 10_000, terminada = true))

        val c = Ambos.cerrarSinReloj(almacen, s, "s23-ultra", interrumpida = true)!!
        assertEquals(EstadoLocal.TERMINADA, c.estadoLocal)
        assertEquals(FuenteAudio.RELOJ, c.fuenteAudio)
        assertTrue(c.pistaCelu)
        assertTrue(c.interrumpida)
        assertEquals(OggDePrueba.duracionMs(300), c.duracionMs)
    }

    @Test
    fun `una pista que quedo abierta (la app se cayo) se cierra con lo que tiene su Ogg`() {
        val s = sesion().copy(estadoLocal = EstadoLocal.GRABANDO).also { almacen.crear(it) }
        pistas.audio(s.id).writeBytes(OggDePrueba.bytes(250))
        pistas.guardar(s.id, PistaCelu(inicioMs = inicioCelu, duracionMs = 3_000))

        val c = Ambos.cerrarSinReloj(almacen, s, "s23-ultra", interrumpida = true)!!
        val p = pistas.leer(s.id)!!
        assertTrue(p.terminada)
        assertTrue(p.interrumpida)
        assertEquals(OggDePrueba.duracionMs(250), p.duracionMs)
        assertTrue(c.interrumpida)
        assertEquals(OggDePrueba.duracionMs(250), c.duracionMs)
    }

    @Test
    fun `sin audio del celu, o si no es de Ambos, la cierra quien llama como siempre`() {
        val s = sesion().also { almacen.crear(it) }
        pistas.guardar(s.id, PistaCelu(inicioMs = inicioCelu, terminada = true))
        assertNull(Ambos.cerrarSinReloj(almacen, s, "s23-ultra", interrumpida = true))
        assertNull(Ambos.cerrarSinReloj(almacen, s.copy(pistaCelu = false), "s23-ultra", interrumpida = true))
        assertEquals(EstadoLocal.GRABANDO, almacen.leerSesion(s.id)!!.estadoLocal)
    }

    @Test
    fun `una clase con la pista del celu nunca se descarta como vacia`() {
        val s = sesion().also { almacen.crear(it) }
        pistas.audio(s.id).writeBytes(OggDePrueba.bytes(10))
        assertFalse(SincronizacionWatch.descartarSiVacia(almacen, s))
        assertTrue(almacen.dir(s.id).isDirectory)
        // Tampoco una que ya quedó como del teléfono.
        val t = Ambos.comoTelefono(s, "s23-ultra", null)
        assertFalse(SincronizacionWatch.descartarSiVacia(almacen, t))
    }

    @Test
    fun `una del telefono que graba en audio_celu_ogg se recupera con ese archivo`() {
        val t = Ambos.comoTelefono(sesion(), "s23-ultra", PistaCelu(inicioMs = inicioCelu)).copy(estadoLocal = EstadoLocal.GRABANDO)
        almacen.crear(t)
        pistas.audio(t.id).writeBytes(OggDePrueba.bytes(400))
        val cerradas = Recuperacion.cerrarHuerfanas(almacen, activa = null, inactividadMs = 0)
        assertEquals(listOf(t.id to OggDePrueba.duracionMs(400)), cerradas.map { it.first.id to it.second })
        assertEquals(EstadoLocal.TERMINADA, almacen.leerSesion(t.id)!!.estadoLocal)
    }

    @Test
    fun `recibiendo del reloj, con Ambos se ofrece cerrar con lo recibido a los 5 minutos sin contacto`() {
        val s = sesion().copy(estadoLocal = EstadoLocal.RECIBIENDO).also { almacen.crear(it) }
        val ahora = 50_000_000L
        val registros = RegistrosWatch(almacen)
        val base = FilaSesion(
            id = s.id, materia = "Macro", tipoClase = null, fecha = "", duracion = null, estado = "", detalle = null,
            progreso = null, tono = Tono.NORMAL, pdf = null,
        )
        registros.guardar(s.id, RegistroWatch(vistaEnWatch = true, ultimoContacto = ahora - 3 * 60_000))
        val reciente = FilasWatch.fila(base, s, almacen, activa = false, ahoraMs = ahora)!!
        assertNull(reciente.accionWatch)
        assertTrue(reciente.detalle!!.contains("el celu grabó su audio"))
        registros.guardar(s.id, RegistroWatch(vistaEnWatch = true, ultimoContacto = ahora - 6 * 60_000))
        assertEquals(FilasWatch.CERRAR, FilasWatch.fila(base, s, almacen, activa = false, ahoraMs = ahora)!!.accionWatch)
        // Sólo con el reloj, todavía no.
        assertNull(FilasWatch.fila(base, s.copy(pistaCelu = false), almacen, activa = false, ahoraMs = ahora)!!.accionWatch)
    }

    @Test
    fun `si se perdio celu_json pero hay audio, se arma de nuevo con lo que dice el Ogg`() {
        val s = sesion().also { almacen.crear(it) }
        pistas.audio(s.id).writeBytes(OggDePrueba.bytes(150))
        val p = pistas.cerrarSiQuedoAbierta(s.id)!!
        assertTrue(p.terminada)
        assertTrue(p.interrumpida)
        assertEquals(OggDePrueba.duracionMs(150), p.duracionMs)
        assertEquals(p, pistas.leer(s.id))
        // Sin audio no hay pista que armar.
        val vacia = sesion("b").also { almacen.crear(it) }
        assertNull(pistas.cerrarSiQuedoAbierta(vacia.id))
    }

    // ---- Segunda revisión ----

    @Test
    fun `la medicion con ping reemplaza a la estimacion sin ping aunque salte`() {
        // El primer estado llegó 1,5 s tarde: la estimación sin ping quedó corrida más que su error declarado.
        val estimada = PistaCelu(inicioMs = inicioCelu, desfaseMs = -18_500, desfaseErrorMs = 1_000)
        val conPing = Ambos.medir(estimada, 90_000, 23, 110_000)
        assertEquals(-20_000L, conPing.desfaseMs)
        assertEquals(23L, conPing.desfaseErrorMs)
        // Después de una pausa, no.
        assertEquals(-18_500L, Ambos.medir(estimada.copy(desfaseFijo = true), 90_000, 23, 110_000).desfaseMs)
    }

    @Test
    fun `al arrancar la app se cierran las pistas que quedaron abiertas y se avisa si el celu era lo unico que grababa`() {
        // El reloj nunca grabó: el celu era lo único.
        val soloCelu = sesion("solo").also { almacen.crear(it) }
        pistas.audio(soloCelu.id).writeBytes(OggDePrueba.bytes(200))
        pistas.guardar(soloCelu.id, PistaCelu(inicioMs = inicioCelu))
        // El reloj grababa: se cierra la pista, pero el reloj sigue y no hay que avisar.
        val conReloj = sesion("reloj").also { almacen.crear(it) }
        pistas.audio(conReloj.id).writeBytes(OggDePrueba.bytes(200))
        pistas.guardar(conReloj.id, PistaCelu(inicioMs = inicioCelu))
        RegistrosWatch(almacen).guardar(conReloj.id, RegistroWatch(vistaEnWatch = true))
        // Ya cerrada: no se toca.
        val cerrada = sesion("cerrada").also { almacen.crear(it) }
        pistas.audio(cerrada.id).writeBytes(OggDePrueba.bytes(50))
        pistas.guardar(cerrada.id, PistaCelu(inicioMs = inicioCelu, terminada = true, duracionMs = 1))

        val avisar = Ambos.cerrarPistasCortadas(almacen, activa = null)
        assertEquals(listOf("solo" to OggDePrueba.duracionMs(200)), avisar.map { it.first.id to it.second })
        assertTrue(pistas.leer("solo")!!.interrumpida)
        assertTrue(pistas.leer("reloj")!!.terminada)
        assertEquals(1L, pistas.leer("cerrada")!!.duracionMs)
        // La que se está grabando no se toca.
        val activa = sesion("activa").also { almacen.crear(it) }
        pistas.audio(activa.id).writeBytes(OggDePrueba.bytes(20))
        pistas.guardar(activa.id, PistaCelu(inicioMs = inicioCelu))
        assertTrue(Ambos.cerrarPistasCortadas(almacen, activa = "activa").isEmpty())
        assertFalse(pistas.leer("activa")!!.terminada)
    }

    @Test
    fun `una que se corto sin que el reloj grabe ofrece cerrar con lo recibido, y queda del telefono`() {
        val s = sesion().also { almacen.crear(it) }
        pistas.audio(s.id).writeBytes(OggDePrueba.bytes(200))
        pistas.guardar(s.id, PistaCelu(inicioMs = inicioCelu, terminada = true, interrumpida = true))
        assertTrue(Ambos.cerrableSinReloj(almacen, s))
        val base = FilaSesion(
            id = s.id, materia = "Macro", tipoClase = null, fecha = "", duracion = null, estado = "", detalle = null,
            progreso = null, tono = Tono.NORMAL, pdf = null,
        )
        val fila = FilasWatch.fila(base, s, almacen, activa = false)!!
        assertEquals("Se cortó", fila.estado)
        assertEquals(FilasWatch.CERRAR, fila.accionWatch)
        // Si el reloj la grabó (o la está grabando), se sigue en el celu como siempre.
        RegistrosWatch(almacen).guardar(s.id, RegistroWatch(vistaEnWatch = true))
        assertFalse(Ambos.cerrableSinReloj(almacen, s))
        assertEquals(FilasWatch.SEGUIR, FilasWatch.fila(base, s, almacen, activa = false)!!.accionWatch)
    }

    @Test
    fun `un audio del reloj verificado pero sin sonido cuenta como vacio si el celu grabo`() {
        val s = sesion().copy(estadoLocal = EstadoLocal.RECIBIENDO).also { almacen.crear(it) }
        // Sólo los encabezados Ogg: ni un paquete de audio.
        almacen.archivoAudio(s.id).writeBytes(OggDePrueba.bytes(0))
        assertFalse(Ambos.relojVacio(almacen, s))
        pistas.audio(s.id).writeBytes(OggDePrueba.bytes(300))
        pistas.guardar(s.id, PistaCelu(inicioMs = inicioCelu, terminada = true, duracionMs = 6_000))
        assertTrue(Ambos.relojVacio(almacen, s))
        val c = Ambos.cerrarSinReloj(almacen, s, "s23-ultra", interrumpida = true)!!
        assertTrue(c.principalEsCelu)
        // Con audio del reloj de verdad, no.
        val otra = sesion("b").also { almacen.crear(it) }
        almacen.archivoAudio(otra.id).writeBytes(OggDePrueba.bytes(10))
        pistas.audio(otra.id).writeBytes(OggDePrueba.bytes(10))
        assertFalse(Ambos.relojVacio(almacen, otra))
    }

    // ---- Tercera revisión ----

    @Test
    fun `una mucho mas precisa no reemplaza si el salto es de una pausa que el celu no vio`() {
        val estimada = PistaCelu(inicioMs = inicioCelu, desfaseMs = -20_000, desfaseErrorMs = 1_000)
        // El reloj pausó 10 min lejos del celu: el salto son minutos, no la demora del Bluetooth.
        val saltada = Ambos.medir(estimada, 600_000, 23, 1_220_000)
        assertEquals(-20_000L, saltada.desfaseMs)
        assertEquals(600_000L, saltada.corrimientoMaxMs)
    }

    @Test
    fun `al arrancar tambien avisa si el reloj ya se habia cortado y seguia solo el celu`() {
        val s = sesion("cortado").also { almacen.crear(it) }
        almacen.archivoAudio(s.id).writeBytes(OggDePrueba.bytes(100))
        pistas.audio(s.id).writeBytes(OggDePrueba.bytes(300))
        pistas.guardar(s.id, PistaCelu(inicioMs = inicioCelu, desfaseMs = -20_000))
        RegistrosWatch(almacen).guardar(
            s.id,
            RegistroWatch(
                vistaEnWatch = true,
                grabacion = GrabacionWatch(
                    id = s.id, estado = EstadoGrabacionWatch.TERMINADA, origen = OrigenGrabacion.CELU,
                    inicio = inicioReloj, bytes = 100, acumuladoMs = 2_000, interrumpida = true,
                ),
            ),
        )
        assertEquals(listOf("cortado"), Ambos.cerrarPistasCortadas(almacen, activa = null).map { it.first.id })
    }

    @Test
    fun `el minuto de un adjunto contado con la pista del celu se pasa como el de los eventos`() {
        val a = AdjuntoLocal(
            id = "a", sesionId = "a", nombreOriginal = "Slides.pdf", mime = "application/pdf", bytes = 10,
            offsetMs = 50_000, creado = "x", archivo = "a.pdf", pista = Pista.CELU,
        )
        assertEquals(30_000L, Ambos.adjuntoParaSubir(a, sesion(), -20_000)!!.offsetMs)
        assertNull(Ambos.adjuntoParaSubir(a, sesion(), -20_000)!!.pista)
        // Sin el desfase espera; con la clase cerrada va como está.
        assertNull(Ambos.adjuntoParaSubir(a, sesion(), null))
        assertEquals(50_000L, Ambos.adjuntoParaSubir(a, sesion().copy(estadoLocal = EstadoLocal.TERMINADA), null)!!.offsetMs)
        // Si quedó del teléfono, el minuto de la pista del celu es el del audio principal.
        val telefono = Ambos.comoTelefono(sesion(), "s23-ultra", null)
        assertEquals(50_000L, Ambos.adjuntoParaSubir(a, telefono, -20_000)!!.offsetMs)
        // Uno sin minuto (adjuntado después de la clase) no cambia.
        assertNull(Ambos.adjuntoParaSubir(a.copy(offsetMs = null), sesion(), null)!!.offsetMs)
    }

    @Test
    fun `una que quedo del telefono con la copia del reloj verificada queda pendiente hasta que el reloj la borra`() {
        val t = Ambos.comoTelefono(sesion(), "s23-ultra", null).copy(estadoLocal = EstadoLocal.TERMINADA).also { almacen.crear(it) }
        assertFalse(SincronizacionWatch.hayPendientes(almacen))
        RegistrosWatch(almacen).guardar(t.id, RegistroWatch(vistaEnWatch = true, verificada = true, sha256 = "ab"))
        assertTrue(SincronizacionWatch.hayPendientes(almacen))
        RegistrosWatch(almacen).cambiar(t.id) { it.copy(borradaEnWatch = true) }
        assertFalse(SincronizacionWatch.hayPendientes(almacen))
    }
}
