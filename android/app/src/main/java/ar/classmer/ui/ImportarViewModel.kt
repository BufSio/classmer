package ar.classmer.ui

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import ar.classmer.calendario.ClaseDelCalendario
import ar.classmer.calendario.LectorCalendario
import ar.classmer.calendario.ReglasCalendario
import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.importacion.DocumentoElegido
import ar.classmer.importacion.Documentos
import ar.classmer.importacion.ImportacionInvalida
import ar.classmer.importacion.Importador
import ar.classmer.importacion.MetadatosAudio
import ar.classmer.importacion.PedidoImportacion
import ar.classmer.importacion.PlanificadorImportacion
import ar.classmer.modelo.Materia
import ar.classmer.modelo.SesionLocal
import ar.classmer.util.Tiempo
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.LocalDate

/** El audio elegido, con lo que dijo el selector y lo que se leyó de sus metadatos. */
data class AudioElegido(
    val uri: String,
    val nombre: String?,
    val bytes: Long?,
    val mime: String?,
    val duracionMs: Long?,
    /** La fecha de sus metadatos (`creation_time`): en un audio de WhatsApp suele ser la del envío. */
    val fechaMs: Long?,
    val modificadoMs: Long?,
)

/** Una foto o un archivo de material elegido. */
data class ArchivoElegido(val uri: String, val nombre: String?, val bytes: Long?, val mime: String?)

data class EstadoImportar(
    val materias: List<Materia> = emptyList(),
    val audio: AudioElegido? = null,
    val leyendoAudio: Boolean = false,
    /** El audio elegido no sirve (por ejemplo, pesa más de 2 GB): no quedó elegido. */
    val problemaAudio: String? = null,
    val fotos: List<ArchivoElegido> = emptyList(),
    val material: List<ArchivoElegido> = emptyList(),
    /** Lo que no se agregó (una foto de más de 50 MB, un PDF de más de 100 MB…). */
    val avisos: List<String> = emptyList(),
    val eleccion: EleccionImportacion = EleccionImportacion(LocalDate.now()),
    /** Las clases del calendario del día elegido. */
    val clases: List<ClaseDelCalendario> = emptyList(),
    val calendarioLeido: Boolean = false,
    val importando: Boolean = false,
    val error: String? = null,
    /** Se acaba de importar esta clase: la pantalla vuelve a Inicio (también si se giró el celu mientras se guardaba). */
    val importada: SesionLocal? = null,
) {
    val materia: Materia? get() = materias.firstOrNull { it.id == eleccion.materiaId }
    val tiposClase: List<String> get() = SeleccionMateria.tiposDe(materias, eleccion.materiaId)

    /** Lo que se va a copiar al celu (lo que informó el selector). */
    val bytesACopiar: Long get() = (audio?.bytes ?: 0) + fotos.sumOf { it.bytes ?: 0 }

    val lista: Boolean get() = audio != null && eleccion.completa && !leyendoAudio && !importando
}

/**
 * La pantalla "Importar clase". Vive con la actividad (sobrevive a girar el celu con el selector
 * abierto); [empezar] y [salir] la dejan de cero al entrar y al irse. Los documentos elegidos se
 * quedan con permiso persistente hasta importar (pasan a la copia) o hasta sacarlos. Lo que se saca
 * se saca primero del estado y después se suelta su permiso ([Documentos.soltarPermisos] mira lo que
 * sigue elegido acá).
 */
class ImportarViewModel(app: Application) : AndroidViewModel(app) {
    private val ajustes = Ajustes(app)
    private val almacen = AlmacenSesiones.de(app)
    private val _estado = MutableStateFlow(EstadoImportar(materias = ajustes.materiasCache))
    val estado: StateFlow<EstadoImportar> = _estado.asStateFlow()

    private var lecturaCalendario: Job? = null

    /** "Importar y procesar": en el alcance del ViewModel, de a una. */
    private val guardado = TareaUnica<SesionLocal>(viewModelScope)

    /** Lo elegido acá: una importación que termina (o se descarta) no le suelta el permiso. */
    private val elegidosAca: () -> Set<String> = { elegidos(_estado.value) }

    init {
        Documentos.enPantalla = elegidosAca
        // Si Android cerró la app con esta pantalla abierta, vuelve acá sin pasar por empezar().
        leerCalendario()
    }

    /** Al entrar a la pantalla: todo de cero (lo que quedó elegido de antes se suelta). */
    fun empezar() {
        val antes = elegidos(_estado.value)
        _estado.value = EstadoImportar(materias = ajustes.materiasCache, eleccion = EleccionImportacion(LocalDate.now()))
        soltar(antes)
        leerCalendario()
    }

    /** Al irse sin importar. */
    fun salir() {
        val antes = elegidos(_estado.value)
        _estado.value = EstadoImportar()
        soltar(antes)
    }

    override fun onCleared() {
        if (Documentos.enPantalla === elegidosAca) Documentos.enPantalla = { emptySet() }
        soltar(elegidos(_estado.value))
    }

    fun olvidarAvisos() = _estado.update { it.copy(avisos = emptyList(), error = null) }

    /** La pantalla ya volvió a Inicio con la clase importada. */
    fun olvidarImportada() = _estado.update { it.copy(importada = null) }

    // ---- Audio ----

    fun elegirAudio(uri: Uri) {
        val app = getApplication<Application>()
        _estado.update { it.copy(leyendoAudio = true, problemaAudio = null, error = null) }
        viewModelScope.launch {
            val (datos, meta) = withContext(Dispatchers.IO) {
                Documentos.tomarPermiso(app, uri)
                Documentos.datos(app, uri) to MetadatosAudio.leer(app, uri)
            }
            // El audio anterior se reemplaza: su permiso se suelta (después de sacarlo del estado).
            val anterior = _estado.value.audio?.uri?.takeIf { it != uri.toString() }
            Importador.problemaAudio(datos.bytes)?.let { problema ->
                _estado.update { it.copy(audio = null, leyendoAudio = false, problemaAudio = problema) }
                soltar(listOfNotNull(uri.toString(), anterior))
                return@launch
            }
            val audio = AudioElegido(uri.toString(), datos.nombre, datos.bytes, datos.mime, meta.duracionMs, meta.fechaMs, datos.modificadoMs)
            // La lista del calendario arranca en el día del audio (o en hoy), salvo que ya se haya elegido qué clase es.
            val dia = ElegirClaseImportada.diaInicial(meta.fechaMs, datos.modificadoMs, System.currentTimeMillis())
            _estado.update { e -> e.copy(audio = audio, leyendoAudio = false, eleccion = ElegirClaseImportada.alElegirAudio(e.eleccion, dia)) }
            soltar(listOfNotNull(anterior))
            leerCalendario()
        }
    }

    fun quitarAudio() {
        val audio = _estado.value.audio ?: return
        _estado.update { it.copy(audio = null, problemaAudio = null) }
        soltar(listOf(audio.uri))
    }

    // ---- Fotos y material ----

    fun agregarFotos(uris: List<Uri>) = agregar(uris, material = false)

    fun agregarMaterial(uris: List<Uri>) = agregar(uris, material = true)

    fun quitarFoto(uri: String) {
        _estado.update { e -> e.copy(fotos = e.fotos.filterNot { it.uri == uri }) }
        soltar(listOf(uri))
    }

    fun quitarMaterial(uri: String) {
        _estado.update { e -> e.copy(material = e.material.filterNot { it.uri == uri }) }
        soltar(listOf(uri))
    }

    private fun agregar(uris: List<Uri>, material: Boolean) {
        if (uris.isEmpty()) return
        val app = getApplication<Application>()
        viewModelScope.launch {
            val ya = _estado.value.let { e -> (e.fotos + e.material).map { it.uri }.toSet() + listOfNotNull(e.audio?.uri) }
            val nuevos = withContext(Dispatchers.IO) {
                uris.map { it.toString() }.distinct().filter { it !in ya }.map { u ->
                    val uri = Uri.parse(u)
                    Documentos.tomarPermiso(app, uri)
                    val d = Documentos.datos(app, uri)
                    ArchivoElegido(u, d.nombre, d.bytes, d.mime)
                }
            }
            val problemas = nuevos.associateWith { a ->
                if (material) {
                    a.bytes?.takeIf { it > AlmacenAdjuntos.MAX_ADJUNTO }?.let { "${a.nombre ?: "Un archivo"} pesa más de 100 MB: no se adjunta." }
                } else {
                    Importador.problemaFoto(a.nombre, a.bytes)
                }
            }
            val aceptados = nuevos.filter { problemas[it] == null }
            // Los que no se agregan nunca estuvieron en el estado.
            soltar(nuevos.filter { problemas[it] != null }.map { it.uri })
            val repetidos = uris.size - nuevos.size
            _estado.update { e ->
                e.copy(
                    fotos = if (material) e.fotos else e.fotos + aceptados,
                    material = if (material) e.material + aceptados else e.material,
                    avisos = problemas.values.filterNotNull() +
                        listOfNotNull(if (repetidos > 0) ResumenAdjuntos.plural(repetidos, "archivo ya estaba elegido", "archivos ya estaban elegidos") + "." else null),
                )
            }
        }
    }

    // ---- ¿Qué clase es? ----

    fun diaAnterior() = cambiarDia(_estado.value.eleccion.dia.minusDays(1))

    fun diaSiguiente() = cambiarDia(_estado.value.eleccion.dia.plusDays(1))

    fun cambiarDia(dia: LocalDate) {
        _estado.update { e -> e.copy(eleccion = ElegirClaseImportada.cambiarDia(e.eleccion, dia), clases = emptyList()) }
        leerCalendario()
    }

    fun elegirClase(clase: ClaseDelCalendario) =
        _estado.update { e -> e.copy(eleccion = ElegirClaseImportada.elegirClase(e.eleccion, clase)) }

    fun elegirAMano() = _estado.update { e -> e.copy(eleccion = ElegirClaseImportada.elegirAMano(e.eleccion)) }

    /** `null` = Auto (IA). */
    fun elegirMateria(id: String?) =
        _estado.update { e -> e.copy(eleccion = ElegirClaseImportada.elegirMateria(e.eleccion, id, e.materias)) }

    fun elegirTipo(tipo: String) = _estado.update { e -> e.copy(eleccion = ElegirClaseImportada.elegirTipo(e.eleccion, tipo)) }

    fun cambiarInicio(inicioMs: Long) {
        val antes = _estado.value.eleccion.dia
        _estado.update { e -> e.copy(eleccion = ElegirClaseImportada.cambiarInicio(e.eleccion, inicioMs)) }
        if (_estado.value.eleccion.dia != antes) leerCalendario()
    }

    fun cambiarNumero(numero: Int?) = _estado.update { e -> e.copy(eleccion = ElegirClaseImportada.cambiarNumero(e.eleccion, numero)) }

    /** Las clases del calendario del celu del día elegido (puede tener sólo las semanas cercanas). */
    fun leerCalendario() {
        val app = getApplication<Application>()
        lecturaCalendario?.cancel()
        lecturaCalendario = viewModelScope.launch {
            val e = _estado.value
            val dia = e.eleccion.dia
            val (desde, hasta) = ElegirClaseImportada.limitesDelDia(dia)
            val clases = withContext(Dispatchers.IO) {
                ReglasCalendario.clasesDelDia(e.materias, LectorCalendario.eventosEntre(app, desde, hasta), desde, hasta)
            }
            _estado.update { actual ->
                if (actual.eleccion.dia != dia) actual else actual.copy(clases = clases, calendarioLeido = LectorCalendario.tienePermiso(app))
            }
        }
    }

    // ---- Importar ----

    /**
     * "Importar y procesar" (ya confirmado): guarda la clase en el celu (`importando`), encola la copia
     * y copia el material a sus adjuntos. Devuelve la sesión creada (y la deja en [EstadoImportar.importada]),
     * o tira con el motivo para mostrar.
     *
     * Corre en el alcance del ViewModel, no en el de quien la llama: si se gira el celu mientras se
     * guarda, la pantalla se rehace (y su alcance se cancela), pero la clase se termina de guardar y de
     * encolar igual. Si no, quedaba guardada sin su copia, con el formulario lleno, y otro toque creaba
     * otra clase. Un segundo pedido mientras tanto espera el mismo.
     */
    suspend fun importar(): SesionLocal {
        val e = _estado.value
        val audio = e.audio ?: throw ImportacionInvalida("Elegí el audio de la clase.")
        val inicio = e.eleccion.inicioMs?.takeIf { e.eleccion.elegida } ?: throw ImportacionInvalida("Elegí qué clase es y cuándo empezó.")
        if (ControlGrabacion.estado.value.grabando) throw ImportacionInvalida("Hay una grabación en curso: importá la clase cuando termine.")
        return guardado.correr { guardar(e, audio, inicio) }
    }

    private suspend fun guardar(e: EstadoImportar, audio: AudioElegido, inicio: Long): SesionLocal {
        val app = getApplication<Application>()
        _estado.update { it.copy(importando = true, error = null) }
        try {
            val sesion = withContext(Dispatchers.IO) {
                Importador.crear(
                    almacen,
                    PedidoImportacion(
                        audio = DocumentoElegido(audio.uri, audio.nombre, audio.bytes, audio.mime),
                        duracionMs = audio.duracionMs,
                        fotos = e.fotos.map { DocumentoElegido(it.uri, it.nombre, it.bytes, it.mime) },
                        materiaId = e.eleccion.materiaId,
                        nombreMateria = e.materia?.nombre,
                        origenMateria = e.eleccion.origen,
                        tipoClase = e.eleccion.tipoClase,
                        inicio = Tiempo.iso(inicio),
                        numeroClase = e.eleccion.numero,
                        eventoCalendario = e.eleccion.eventoCalendario,
                        dispositivo = ajustes.dispositivo,
                    ),
                )
            }
            PlanificadorImportacion.encolar(app, sesion.id)
            // El material entra a la cola de adjuntos de siempre (sube cuando la clase ya está en el server).
            if (e.material.isNotEmpty()) {
                CopiaAdjuntos.adjuntar(app, sesion.id, e.material.map { Uri.parse(it.uri) }, offsetMs = null, creado = Tiempo.ahoraIso())
            }
            // Los permisos ahora son de la copia (y de la de adjuntos): no se sueltan acá.
            _estado.value = EstadoImportar(importada = sesion)
            return sesion
        } catch (ex: CancellationException) {
            // Se cerró la actividad: lo guardado (si llegó) lo retoma PlanificadorImportacion al abrir la app.
            _estado.update { it.copy(importando = false) }
            throw ex
        } catch (ex: ImportacionInvalida) {
            _estado.update { it.copy(importando = false, error = ex.message) }
            throw ex
        } catch (ex: Exception) {
            val mensaje = "No se pudo guardar la clase en el celu: ${ex.message ?: ex.javaClass.simpleName}"
            _estado.update { it.copy(importando = false, error = mensaje) }
            throw ImportacionInvalida(mensaje)
        }
    }

    // ---- Permisos ----

    private fun elegidos(e: EstadoImportar): Set<String> =
        (listOfNotNull(e.audio?.uri) + e.fotos.map { it.uri } + e.material.map { it.uri }).toSet()

    /** Suelta el permiso de documentos que ya no están elegidos, salvo que sigan en uso en otro lado. */
    private fun soltar(uris: Collection<String>) = Documentos.soltarPermisos(getApplication(), uris)
}

/**
 * Una tarea que corre de a una en [alcance] (el del ViewModel): si se cancela quien la espera (por
 * ejemplo, la pantalla que se rehace al girar el celu), la tarea sigue; un pedido que llega mientras
 * corre espera esa misma en vez de arrancar otra.
 */
internal class TareaUnica<T>(private val alcance: CoroutineScope) {
    private var enCurso: Deferred<T>? = null

    suspend fun correr(tarea: suspend () -> T): T {
        val d = synchronized(this) {
            enCurso?.takeIf { it.isActive } ?: alcance.async { tarea() }.also { enCurso = it }
        }
        return d.await()
    }
}
