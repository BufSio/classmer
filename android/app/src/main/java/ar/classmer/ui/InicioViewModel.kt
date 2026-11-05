package ar.classmer.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import ar.classmer.calendario.LectorCalendario
import ar.classmer.calendario.ReglasCalendario
import ar.classmer.calendario.Sugerencia
import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.importacion.Documentos
import ar.classmer.importacion.Importador
import ar.classmer.importacion.PlanificadorImportacion
import ar.classmer.modelo.EstadoSesionServidor
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.Materia
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.TIPOS_CLASE_POR_DEFECTO
import ar.classmer.red.ClienteApi
import ar.classmer.red.ErrorApi
import ar.classmer.red.ErrorAutenticacion
import ar.classmer.red.ResultadoAsignar
import ar.classmer.red.detalleDe
import ar.classmer.subida.PlanificadorSubida
import ar.classmer.util.Tiempo
import ar.classmer.watch.EnlaceWatch
import ar.classmer.watch.FilasWatch
import ar.classmer.watch.SincronizacionWatch
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException

enum class Tono { NORMAL, ACTIVO, OK, ERROR }

data class FilaSesion(
    val id: String,
    val materia: String,
    val tipoClase: String?,
    val fecha: String,
    val duracion: String?,
    val estado: String,
    val detalle: String?,
    val progreso: Float?,
    val tono: Tono,
    /** Ruta del PDF cuando el apunte está listo. */
    val pdf: String?,
    /** Para ordenar la lista: inicio de la clase en ms (0 si no se sabe). */
    val inicioMs: Long = 0,
    /** El server rechazó la clase: se muestra el botón "Reintentar". */
    val reintentable: Boolean = false,
    /** Número de clase (el del server si ya lo informó, si no el con que se grabó). */
    val numeroClase: Int? = null,
    /** La sesión está en `esperando_usuario`: se puede elegir la materia desde el celu. */
    val elegirMateria: Boolean = false,
    /** La materia que sugiere el server para esta sesión, si la hay. */
    val materiaSugerida: String? = null,
    /** El server espera que se revisen las dudas del transcript en el panel (`espera: revision`). */
    val revisarEnPanel: Boolean = false,
    /** Grabó el reloj: el botón para seguirla en el celu o cerrarla con lo recibido ([FilasWatch]). */
    val accionWatch: String? = null,
    /** Adjuntos de la clase (PDF, imágenes): los del celu y los que informa el server. */
    val adjuntos: ResumenAdjuntos = ResumenAdjuntos(),
    /** Clase importada (no la grabó el celu): lleva la marca "Importada". */
    val importada: Boolean = false,
    /** La copia de una importada falló: "Reintentar" y "Descartar" ([FilasImportacion]). */
    val importacionFallida: Boolean = false,
)

data class EstadoInicio(
    val materias: List<Materia> = emptyList(),
    val materiasDesdeCache: Boolean = false,
    val errorMaterias: String? = null,
    val seleccion: Seleccion = Seleccion(),
    val sugerencia: Sugerencia? = null,
    val calendarioLeido: Boolean = false,
    val sesiones: List<FilaSesion> = emptyList(),
    val configurado: Boolean = false,
    val errorAutenticacion: String? = null,
    val errorServidor: String? = null,
    val descargandoPdf: String? = null,
    /** Sesión a la que se le está mandando la materia elegida. */
    val asignando: String? = null,
) {
    val materiaSeleccionada: Materia? get() = materias.firstOrNull { it.id == seleccion.materiaId }
    val tiposClase: List<String> get() = materiaSeleccionada?.tiposClase?.ifEmpty { null } ?: TIPOS_CLASE_POR_DEFECTO

    /** Número de la próxima clase de la materia elegida (`null` en Auto o si el server no lo informa). */
    val numeroClase: Int? get() = SeleccionMateria.numeroClase(seleccion, materias)
}

class InicioViewModel(app: Application) : AndroidViewModel(app) {
    private val ajustes = Ajustes(app)
    private val almacen = AlmacenSesiones.de(app)
    private val adjuntos = AlmacenAdjuntos.de(app)
    private val _estado = MutableStateFlow(EstadoInicio(materias = ajustes.materiasCache, materiasDesdeCache = true))
    val estado: StateFlow<EstadoInicio> = _estado.asStateFlow()

    private val candadoRefresco = Mutex()

    /** Sube con cada número confirmado por el server: un refresco de materias que empezó antes se descarta. */
    @Volatile
    private var cambiosDeNumero = 0
    private var ultimaPatada = 0L

    /** Sesiones por las que el server respondió 404 (por ejemplo, de otro server): no se vuelve a preguntar. */
    private val noEncontradas = mutableSetOf<String>()

    /** Se tocó Grabar: cuando la grabación arranque de verdad, lo elegido a mano se descarta. */
    private var reiniciarAlGrabar = false

    init {
        refrescarMaterias()
        aplicarCalendario()
        viewModelScope.launch {
            ControlGrabacion.estado.map { it.sesion }.distinctUntilChanged { a, b -> a?.id == b?.id }.collect { sesion ->
                if (sesion != null && reiniciarAlGrabar) {
                    reiniciarAlGrabar = false
                    if (sesion.numeroClase != null) contarClase(sesion.materiaId)
                    // Lo elegido a mano vale para esta clase: la próxima vuelve a autocompletarse.
                    _estado.update { it.copy(seleccion = Seleccion()) }
                    aplicarCalendario()
                }
            }
        }
    }

    fun refrescarMaterias() {
        viewModelScope.launch {
            _estado.update { it.copy(configurado = ajustes.configurado, errorAutenticacion = ajustes.errorAutenticacion) }
            val api = api() ?: return@launch
            val cambiosAlEmpezar = cambiosDeNumero
            try {
                val materias = withContext(Dispatchers.IO) {
                    // Las importadas que el server todavía no tiene (se están copiando) ya usaron su número.
                    SeleccionMateria.contarPendientes(api.materias(), importadasSinCrear())
                }
                // Mientras tanto se cambió un número con el lápiz: esta lista puede traer el anterior.
                if (cambiosDeNumero != cambiosAlEmpezar) return@launch
                ajustes.materiasCache = materias
                ajustes.materiasActualizadas = Tiempo.ahoraIso()
                limpiarErrorAutenticacion()
                _estado.update { it.copy(materias = materias, materiasDesdeCache = false, errorMaterias = null) }
                aplicarCalendario()
            } catch (e: ErrorAutenticacion) {
                ajustes.errorAutenticacion = e.message
                _estado.update { it.copy(errorAutenticacion = e.message) }
            } catch (e: Exception) {
                _estado.update { it.copy(errorMaterias = mensaje(e)) }
            }
        }
    }

    /** Autocompleta materia y tipo desde el calendario, salvo que se haya elegido a mano. */
    fun aplicarCalendario() {
        viewModelScope.launch {
            val materias = _estado.value.materias
            val sugerencia = withContext(Dispatchers.IO) {
                ReglasCalendario.sugerir(materias, LectorCalendario.eventosCercanos(getApplication()), System.currentTimeMillis())
            }
            _estado.update { e ->
                e.copy(
                    seleccion = SeleccionMateria.aplicarCalendario(e.seleccion, sugerencia, e.materias),
                    sugerencia = sugerencia,
                    calendarioLeido = LectorCalendario.tienePermiso(getApplication()),
                )
            }
        }
    }

    /** `null` = Auto (IA). */
    fun elegirMateria(id: String?) {
        _estado.update { e -> e.copy(seleccion = SeleccionMateria.elegirMateria(e.seleccion, id, e.materias, e.sugerencia)) }
    }

    fun elegirTipo(tipo: String) {
        _estado.update { e -> e.copy(seleccion = SeleccionMateria.elegirTipo(e.seleccion, tipo, e.sugerencia)) }
    }

    fun grabar() {
        val e = _estado.value
        val fuente = ajustes.fuenteAudio
        // El reloj ya está grabando una clase (se empezó allá): Grabar la sigue en el celu (con Ambos, el celu
        // graba también desde ahora). Sólo con un estado reciente y si esa clase sigue abierta en el celu (o el
        // celu todavía no la conoce).
        val conReloj = fuente == FuenteAudio.RELOJ || fuente == FuenteAudio.AMBOS
        val enCurso = if (conReloj) SincronizacionWatch.grabacionParaSeguir(getApplication()) else null
        if (enCurso != null) {
            SincronizacionWatch.seguirEnCurso(getApplication(), enCurso.id, conCelu = fuente == FuenteAudio.AMBOS)
            return
        }
        ControlGrabacion.iniciar(
            getApplication(),
            materiaId = e.seleccion.materiaId,
            nombreMateria = e.materiaSeleccionada?.nombre,
            origenMateria = e.seleccion.origen,
            tipoClase = e.seleccion.tipoClase,
            eventoCalendario = e.seleccion.eventoCalendario,
            numeroClase = e.numeroClase,
            finEventoMs = e.seleccion.finEventoMs,
            fuenteAudio = fuente,
        )
        reiniciarAlGrabar = true
    }

    /** Las materias de las importadas con número que todavía no llegaron al server (una por clase). */
    private fun importadasSinCrear(): List<String> = almacen.listar().mapNotNull { s ->
        s.materiaId?.takeIf { s.importada && s.numeroClase != null && !almacen.leerSubida(s.id).sesionCreada }
    }

    /** Se importó una clase: como después de grabar, el próximo número de su materia se corre uno. */
    fun alImportar(sesion: SesionLocal) {
        if (sesion.numeroClase != null) contarClase(sesion.materiaId)
        refrescarSesiones()
    }

    /** "Reintentar" de una importación que falló (por ejemplo, después de liberar espacio). */
    fun reintentarImportacion(id: String) {
        PlanificadorImportacion.encolar(getApplication(), id, reintento = true)
        refrescarSesiones()
    }

    /**
     * "Descartar" una importación que falló: se borran sólo las copias que hizo la app (los archivos
     * originales no se tocan) y sólo si la clase nunca llegó al server. Devuelve el error, o `null`.
     */
    suspend fun descartarImportacion(id: String): String? {
        val app = getApplication<Application>()
        PlanificadorImportacion.cancelar(app, id)
        val uris = withContext(Dispatchers.IO) { runCatching { Importador.descartar(almacen, adjuntos, id) }.getOrNull() }
        refrescarSesiones()
        if (uris == null) return "No se pudo descartar: la clase ya no está en ese estado."
        // Salvo los que use otra importación (por ejemplo, la misma clase importada de nuevo).
        Documentos.soltarPermisos(app, uris)
        return null
    }

    /** Ya empezó a grabarse una clase de esta materia: su próximo número sube uno hasta que responda el server. */
    private fun contarClase(materiaId: String?) {
        _estado.update { e -> e.copy(materias = SeleccionMateria.contarClase(e.materias, materiaId)) }
        val materias = _estado.value.materias
        viewModelScope.launch(Dispatchers.IO) { ajustes.materiasCache = materias }
    }

    /**
     * El lápiz de "Clase N": `PATCH /api/materias/{id}`. Devuelve el error para mostrar, o `null` si
     * el server tomó el número.
     */
    suspend fun cambiarProximoNumero(materiaId: String, numero: Int): String? {
        val api = api() ?: return "Configurá el server en Ajustes."
        return try {
            val respuesta = withContext(Dispatchers.IO) { api.cambiarProximoNumero(materiaId, numero) }
            // Sólo se toma el número: la materia que ya está en el celu conserva sus reglas de calendario.
            val confirmado = respuesta?.proximoNumero ?: numero
            cambiosDeNumero++
            _estado.update { e -> e.copy(materias = e.materias.map { if (it.id == materiaId) it.copy(proximoNumero = confirmado) else it }) }
            val materias = _estado.value.materias
            withContext(Dispatchers.IO) { ajustes.materiasCache = materias }
            null
        } catch (e: ErrorAutenticacion) {
            ajustes.errorAutenticacion = e.message
            _estado.update { it.copy(errorAutenticacion = e.message) }
            "El server rechazó el token. Revisalo en Ajustes."
        } catch (e: ErrorApi) {
            when {
                rutaInexistente(e) -> "El server todavía no permite cambiar el número desde la app (falta actualizarlo)."
                e.codigo == 404 -> "El server ya no tiene esta materia. Tocá Actualizar."
                else -> "El server no aceptó el número: ${detalleDe(e.cuerpo)}"
            }
        } catch (e: IOException) {
            "No hay conexión con el server: el número no se cambió."
        }
    }

    /**
     * Elige la materia de una clase en `esperando_usuario` (`POST /api/sesiones/{id}/materia`).
     * Devuelve el error para mostrar, o `null` si el server la tomó.
     */
    suspend fun elegirMateriaSesion(sesionId: String, materiaId: String): String? {
        val api = api() ?: return "Configurá el server en Ajustes."
        if (_estado.value.asignando != null) return "Todavía se está mandando la materia de otra clase: esperá un momento."
        _estado.update { it.copy(asignando = sesionId) }
        try {
            val resultado = withContext(Dispatchers.IO) { api.asignarMateria(sesionId, materiaId) }
            return when (resultado) {
                is ResultadoAsignar.Ok -> {
                    resultado.sesion?.let { s -> withContext(Dispatchers.IO) { runCatching { almacen.guardarEstadoServidor(s) } } }
                    null
                }
                // El detail del server dice lo mismo ("el apunte ya empezó…"): no se repite.
                is ResultadoAsignar.YaEmpezo -> "El server ya empezó el apunte: la materia no se puede cambiar desde el celu."
                is ResultadoAsignar.MateriaInvalida -> {
                    refrescarMaterias()
                    "El server no tiene esa materia (${resultado.detalle}). Elegí otra."
                }
            }
        } catch (e: ErrorAutenticacion) {
            ajustes.errorAutenticacion = e.message
            _estado.update { it.copy(errorAutenticacion = e.message) }
            return "El server rechazó el token. Revisalo en Ajustes."
        } catch (e: ErrorApi) {
            return when {
                rutaInexistente(e) -> "El server todavía no permite elegir la materia desde la app (falta actualizarlo)."
                e.codigo == 404 -> "El server no encontró esta clase."
                else -> e.message ?: "HTTP ${e.codigo}"
            }
        } catch (e: IOException) {
            return "No hay conexión con el server: la materia no se cambió."
        } finally {
            _estado.update { it.copy(asignando = null) }
            refrescarSesiones()
        }
    }

    /** Lee las sesiones locales y, si hay server, su estado allá. Se llama cada ~10 s en Inicio. */
    fun refrescarSesiones() {
        viewModelScope.launch {
            candadoRefresco.withLock { refrescarSesionesAhora() }
        }
    }

    private suspend fun refrescarSesionesAhora() {
        _estado.update { it.copy(configurado = ajustes.configurado, errorAutenticacion = ajustes.errorAutenticacion) }
        val locales = withContext(Dispatchers.IO) { almacen.listar().take(MAX_SESIONES) }
        mostrar(locales, emptyMap())

        val hayPendientes = withContext(Dispatchers.IO) { locales.any { !almacen.leerSubida(it.id).finalizada } }
        val ahora = System.currentTimeMillis()
        if (hayPendientes && ahora - ultimaPatada > 30_000) {
            ultimaPatada = ahora
            PlanificadorSubida.patear(getApplication())
        }

        val api = api() ?: return
        try {
            val remotas = withContext(Dispatchers.IO) {
                val lista = api.sesiones(MAX_SESIONES).associateBy { it.id }.toMutableMap()
                for (s in locales) {
                    if (s.id in lista || s.id in noEncontradas || !almacen.leerSubida(s.id).finalizada) continue
                    try {
                        lista[s.id] = api.sesion(s.id)
                    } catch (e: ErrorAutenticacion) {
                        throw e
                    } catch (e: ErrorApi) {
                        if (e.codigo == 404) noEncontradas += s.id
                    } catch (e: IOException) {
                        // Sin conexión: se vuelve a intentar en el próximo refresco.
                    }
                }
                lista.values.forEach { runCatching { almacen.guardarEstadoServidor(it) } }
                lista
            }
            limpiarErrorAutenticacion()
            _estado.update { it.copy(errorServidor = null) }
            mostrar(locales, remotas)
        } catch (e: ErrorAutenticacion) {
            ajustes.errorAutenticacion = e.message
            _estado.update { it.copy(errorAutenticacion = e.message) }
        } catch (e: Exception) {
            _estado.update { it.copy(errorServidor = mensaje(e)) }
        }
    }

    private suspend fun mostrar(locales: List<SesionLocal>, remotas: Map<String, EstadoSesionServidor>) {
        val materias = _estado.value.materias.associateBy { it.id }
        val activa = ControlGrabacion.estado.value.sesion?.id
        val filas = withContext(Dispatchers.IO) {
            val idsLocales = locales.map { it.id }.toSet()
            val delCelu = locales.map { s -> filaLocal(s, remotas[s.id] ?: almacen.leerEstadoServidor(s.id), materias, s.id == activa) }
            val soloServer = remotas.values.filter { it.id !in idsLocales }.map { filaRemota(it, materias) }
            // Las del celu que todavía no terminaron de subir (una importada copiándose, o que falló) o que
            // esperan la materia o la revisión se ven aunque sean de hace semanas.
            val pendientes = locales.filter { !almacen.leerSubida(it.id).finalizada }.map { it.id } +
                delCelu.filter { it.elegirMateria || it.revisarEnPanel }.map { it.id }
            FilasSesion.recientes(delCelu + soloServer, MAX_SESIONES, pendientes.toSet())
                // Adjuntos de cada clase: lo que hay en el celu y lo que informa el server.
                .map { ResumenAdjuntos.completar(it, adjuntos, almacen, remotas[it.id] ?: almacen.leerEstadoServidor(it.id)) }
        }
        _estado.update { it.copy(sesiones = filas) }
    }

    private fun filaLocal(s: SesionLocal, remota: EstadoSesionServidor?, materias: Map<String, Materia>, activa: Boolean): FilaSesion {
        // Si se grabó en Auto, el server la clasifica: se muestra la materia que eligió.
        // El server manda: si la clasificó (Auto) o la reasignó, se muestra la materia que tiene él.
        val materiaId = remota?.materiaId ?: s.materiaId
        val nombre = materiaId?.let { materias[it]?.nombre ?: s.nombreMateria ?: it } ?: "Auto (IA)"
        val importacion = if (s.importada) almacen.leerImportacion(s.id) else null
        val subida = almacen.leerSubida(s.id)
        val base = FilaSesion(
            id = s.id,
            materia = nombre,
            tipoClase = s.tipoClase,
            fecha = Tiempo.fechaCorta(s.inicio),
            inicioMs = Tiempo.epochMs(s.inicio),
            duracion = when {
                // Una importada sin duración medida (0) muestra la del server, si ya la midió.
                s.importada -> (s.duracionMs.takeIf { it > 0 } ?: remota?.duracionMs)?.let { Tiempo.duracion(it) }
                s.terminada -> Tiempo.duracion(s.duracionMs)
                else -> null
            },
            estado = "",
            detalle = listOfNotNull(
                when {
                    s.interrumpida -> "La grabación se cortó; se guardó lo grabado"
                    s.corteAutomatico -> "Terminó sola: nadie respondió «¿Sigue la clase?»"
                    s.importada -> FilasImportacion.detalle(importacion)
                    else -> null
                },
                // Ambos: el server no tomó el audio del celu (server viejo, o lo rechazó).
                FilasWatch.TEXTO_CELU_SIN_SUBIR.takeIf { subida.celuSinSubir },
            ).joinToString("\n").ifEmpty { null },
            progreso = null,
            tono = Tono.NORMAL,
            pdf = null,
            numeroClase = s.numeroClase,
            importada = s.importada,
        )
        // Una importada que se está copiando al celu (o cuya copia falló).
        val parcial = importacion?.let { Importador.archivoParcial(almacen, s.id, it).length() } ?: 0L
        FilasImportacion.copiando(base, s, importacion, parcial)?.let { return it }
        FilasWatch.fila(base, s, almacen, activa)?.let { return it }
        if (activa || !s.terminada) return base.copy(estado = "Grabando", tono = Tono.ACTIVO)

        if (subida.errorPermanente != null && !subida.finalizada) {
            val pedido = almacen.archivoPedidoReintento(s.id).exists()
            return base.copy(
                estado = if (pedido) "Reintentando" else "El server la rechazó",
                detalle = subida.errorPermanente,
                tono = Tono.ERROR,
                reintentable = !pedido,
            )
        }
        if (!subida.finalizada) {
            val total = almacen.archivoAudio(s).length().coerceAtLeast(1)
            val avance = (subida.bytesConfirmados.toFloat() / total).coerceIn(0f, 1f)
            return if (subida.ultimoError != null) {
                base.copy(estado = "Pendiente de subir", detalle = subida.ultimoError, progreso = avance, tono = Tono.ERROR)
            } else {
                base.copy(estado = if (s.importada) FilasImportacion.subiendo(avance) else "Subiendo", progreso = avance)
            }
        }
        return if (remota != null) aplicarRemota(base, remota) else base.copy(estado = "Subida", tono = Tono.OK)
    }

    private fun filaRemota(r: EstadoSesionServidor, materias: Map<String, Materia>): FilaSesion {
        val base = FilaSesion(
            id = r.id,
            materia = r.titulo ?: r.materiaId?.let { materias[it]?.nombre ?: it } ?: "Auto (IA)",
            tipoClase = r.tipoClase,
            fecha = Tiempo.fechaCorta(r.inicio),
            inicioMs = Tiempo.epochMs(r.inicio),
            duracion = r.duracionMs?.let { Tiempo.duracion(it) },
            estado = "",
            detalle = null,
            progreso = null,
            tono = Tono.NORMAL,
            pdf = null,
            numeroClase = r.numeroClase,
            importada = r.importada,
        )
        return aplicarRemota(base, r)
    }

    private fun aplicarRemota(base: FilaSesion, r: EstadoSesionServidor): FilaSesion =
        FilasSesion.aplicarEstadoRemoto(base.copy(detalle = base.detalle ?: r.titulo), r)

    private fun limpiarErrorAutenticacion() {
        if (ajustes.errorAutenticacion != null) ajustes.errorAutenticacion = null
        _estado.update { it.copy(errorAutenticacion = null) }
    }

    /** El botón de una clase que grabó el reloj: seguirla en el celu, o cerrarla con lo que llegó. */
    fun accionWatch(fila: FilaSesion) {
        when (fila.accionWatch) {
            FilasWatch.SEGUIR -> ControlGrabacion.enganchar(getApplication(), fila.id)
            FilasWatch.CERRAR -> SincronizacionWatch.cerrarConLoRecibido(getApplication(), fila.id)
        }
        refrescarSesiones()
    }

    /** "Reintentar" de una clase que el server rechazó (por ejemplo, después de corregir su config). */
    fun reintentar(id: String) {
        viewModelScope.launch {
            withContext(Dispatchers.IO) { runCatching { almacen.archivoPedidoReintento(id).createNewFile() } }
            PlanificadorSubida.patear(getApplication())
            refrescarSesiones()
        }
    }

    /** Baja el PDF al cache de la app y devuelve el archivo para abrirlo con otra app. */
    suspend fun descargarPdf(fila: FilaSesion): File {
        val api = api() ?: throw IllegalStateException("Configurá el server en Ajustes")
        _estado.update { it.copy(descargandoPdf = fila.id) }
        try {
            return withContext(Dispatchers.IO) {
                val dir = File(getApplication<Application>().cacheDir, "pdf").apply { mkdirs() }
                val destino = File(dir, "apunte-${fila.id}.pdf")
                api.descargarPdf(fila.id, fila.pdf, destino)
                destino
            }
        } finally {
            _estado.update { it.copy(descargandoPdf = null) }
        }
    }

    private fun api(): ClienteApi? {
        if (!ajustes.configurado) return null
        return runCatching { ClienteApi(ajustes.urlServidor, ajustes.token) }.getOrElse {
            _estado.update { it.copy(errorServidor = "La URL del server no es válida: revisala en Ajustes") }
            null
        }
    }

    companion object {
        private const val MAX_SESIONES = 20

        /** El server todavía no tiene esa ruta (FastAPI responde `{"detail": "Not Found"}` o 405). */
        fun rutaInexistente(e: ErrorApi): Boolean =
            e.codigo == 405 || (e.codigo == 404 && detalleDe(e.cuerpo) == "Not Found")

        fun mensaje(e: Exception): String = when (e) {
            is IOException -> "No hay conexión con el server (${e.message ?: e.javaClass.simpleName})"
            else -> e.message ?: e.javaClass.simpleName
        }
    }
}
