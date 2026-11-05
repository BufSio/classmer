package ar.classmer.ui

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.SystemClock
import android.provider.OpenableColumns
import android.util.Log
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import ar.classmer.datos.AdjuntoInvalido
import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.importacion.Documentos
import ar.classmer.modelo.CopiaPendiente
import ar.classmer.subida.PlanificadorSubida
import ar.classmer.util.Tiempo
import ar.classmer.util.Uuid7
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Copia al celu los archivos elegidos con el selector, uno por uno y fuera del hilo principal. Sigue
 * aunque se cierre la hoja o se vuelva a Inicio. Al terminar patea la subida.
 *
 * Cada archivo queda anotado en disco (`<id>.copiando`) antes de copiarlo, con permiso persistente
 * sobre su URI: si Android cierra la app en el medio, [retomar] lo vuelve a copiar al abrirla (o avisa
 * que hay que adjuntarlo de nuevo, si ya no se puede leer).
 */
object CopiaAdjuntos {
    private const val TAG = "ClassmerAdjuntos"

    /** Un archivo que se está copiando ("Copiando…"). [clave] es el id que va a tener el adjunto. */
    data class Copia(val clave: String, val sesionId: String, val nombre: String)

    /** Lo que no se pudo adjuntar en la última tanda de una clase (pesa demasiado, no es PDF ni imagen…). */
    data class Problemas(val sesionId: String, val mensajes: List<String>)

    /** Lo que ofrece el selector del sistema. El tipo se vuelve a mirar en el contenido al copiar. */
    val TIPOS = arrayOf("application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")

    private val alcance = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private val copiasMutable = MutableStateFlow<List<Copia>>(emptyList())
    val copias: StateFlow<List<Copia>> = copiasMutable.asStateFlow()

    private val problemasMutable = MutableStateFlow<Problemas?>(null)
    val problemas: StateFlow<Problemas?> = problemasMutable.asStateFlow()

    private val retomado = AtomicBoolean(false)

    fun olvidarProblemas() {
        problemasMutable.value = null
    }

    /**
     * @param offsetMs el minuto del audio si se adjunta grabando (el del momento en que se tocó el botón).
     * @param creado la hora en que se tocó el botón.
     * @param pista con Ambos, la pista en que se contó [offsetMs] ([ControlGrabacion.pistaActual]).
     */
    fun adjuntar(context: Context, sesionId: String, uris: List<Uri>, offsetMs: Long?, creado: String, pista: String? = null) {
        if (uris.isEmpty()) return
        val app = context.applicationContext
        val pedidos = uris.map { CopiaPendiente(Uuid7.nuevo(), sesionId, it.toString(), offsetMs = offsetMs, creado = creado, pista = pista) }
        // "Copiando…" ya, aunque todavía no se sepa el nombre.
        copiasMutable.update { actual -> actual + pedidos.map { Copia(it.id, sesionId, "Archivo") } }
        alcance.launch {
            val almacen = AlmacenAdjuntos.de(app)
            // Primero se anotan todos (con su nombre y tamaño): si la app se cierra copiando el primero, no se pierde el resto.
            val anotados = pedidos.map { p ->
                val uri = Uri.parse(p.uri)
                runCatching { app.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
                val (nombre, tamanio) = datosDe(app, uri)
                val conDatos = p.copy(nombre = nombre, tamanio = tamanio)
                runCatching { almacen.anotarCopia(conDatos) }.onFailure { Log.w(TAG, "No se pudo anotar la copia ${p.id}", it) }
                copiasMutable.update { actual -> actual.map { if (it.clave == p.id) it.copy(nombre = nombre ?: "Archivo") else it } }
                conDatos
            }
            copiar(app, almacen, sesionId, anotados, offsetMs, retomando = false)
        }
    }

    /**
     * Las copias que quedaron anotadas porque Android cerró la app en el medio: se vuelven a copiar.
     * Corre una vez por proceso (se llama desde las pantallas de adjuntos). También tira los restos viejos.
     */
    fun retomar(context: Context) {
        if (!retomado.compareAndSet(false, true)) return
        val app = context.applicationContext
        alcance.launch {
            val almacen = AlmacenAdjuntos.de(app)
            almacen.sesiones().forEach { runCatching { almacen.limpiar(it) } }
            // Las anotadas primero y después las que están en curso: una copia nueva entra en memoria antes de anotarse.
            val anotadas = almacen.copiasPendientes()
            val enCurso = copiasMutable.value.map { it.clave }.toSet()
            val cortadas = anotadas.filter { it.id !in enCurso }
            for ((sesionId, deLaClase) in cortadas.groupBy { it.sesionId }) {
                copiasMutable.update { actual -> actual + deLaClase.map { Copia(it.id, sesionId, it.nombre ?: "Archivo") } }
                copiar(app, almacen, sesionId, deLaClase, deLaClase.first().offsetMs, retomando = true)
            }
        }
    }

    private suspend fun copiar(
        app: Context,
        almacen: AlmacenAdjuntos,
        sesionId: String,
        pedidos: List<CopiaPendiente>,
        offsetMs: Long?,
        retomando: Boolean,
    ) {
        val hechos = mutableListOf<String>()
        val problemas = mutableListOf<String>()
        for (p in pedidos) {
            val uri = Uri.parse(p.uri)
            val nombre = p.nombre ?: "Archivo"
            try {
                // Ya había terminado (la app se cerró justo después): sólo falta olvidar el pedido.
                if (almacen.archivoDatos(sesionId, p.id).exists()) continue
                // Dos veces el mismo archivo (por ejemplo, un doble toque): no se duplica.
                val repetido = p.tamanio != null && almacen.listar(sesionId).any { it.nombreOriginal == p.nombre?.trim() && it.bytes == p.tamanio }
                if (repetido) {
                    problemas += "$nombre ya estaba adjuntado a esta clase."
                    continue
                }
                val entrada = app.contentResolver.openInputStream(uri) ?: throw IOException("el selector no lo dejó abrir")
                val adjunto = entrada.use { almacen.copiar(sesionId, it, p.nombre, p.offsetMs, p.creado, p.tamanio, id = p.id, pista = p.pista) }
                hechos += adjunto.nombreOriginal
            } catch (e: CancellationException) {
                throw e
            } catch (e: AdjuntoInvalido) {
                problemas += e.message ?: "No se pudo adjuntar $nombre."
            } catch (e: Exception) {
                Log.w(TAG, "No se pudo copiar $uri", e)
                problemas += if (retomando) {
                    "No se terminó de copiar $nombre (la app se cerró en el medio): adjuntalo de nuevo."
                } else {
                    "No se pudo copiar $nombre: ${e.message ?: e.javaClass.simpleName}"
                }
            } finally {
                almacen.olvidarCopia(sesionId, p.id)
                // Salvo que el mismo documento siga en uso (una importación que lo está copiando, por ejemplo).
                Documentos.soltarPermisos(app, listOf(p.uri))
                copiasMutable.update { actual -> actual.filterNot { it.clave == p.id } }
            }
        }
        if (hechos.isNotEmpty()) PlanificadorSubida.patear(app)
        problemasMutable.value = if (problemas.isEmpty()) null else Problemas(sesionId, problemas)
        avisar(app, hechos, problemas, offsetMs)
    }

    private suspend fun avisar(context: Context, hechos: List<String>, problemas: List<String>, offsetMs: Long?) {
        val minuto = offsetMs?.let { " en ${Tiempo.duracion(it)}" } ?: ""
        val texto = buildList {
            when (hechos.size) {
                0 -> Unit
                1 -> add("📎 Adjuntado$minuto: ${hechos[0]}")
                else -> add("📎 ${hechos.size} archivos adjuntados$minuto")
            }
            // El detalle de los problemas queda en pantalla (la hoja y la pantalla de grabación).
            when (problemas.size) {
                0 -> Unit
                1 -> add(problemas[0])
                else -> add("${problemas.size} archivos no se adjuntaron")
            }
        }.joinToString("\n")
        if (texto.isEmpty()) return
        withContext(Dispatchers.Main) {
            Toast.makeText(context, texto, if (problemas.isEmpty()) Toast.LENGTH_SHORT else Toast.LENGTH_LONG).show()
        }
    }

    /** Nombre y tamaño que informa el selector (cualquiera de los dos puede faltar). */
    private fun datosDe(context: Context, uri: Uri): Pair<String?, Long?> = runCatching {
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
            if (!c.moveToFirst()) return@use null
            val iNombre = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            val iTamanio = c.getColumnIndex(OpenableColumns.SIZE)
            val nombre = if (iNombre >= 0 && !c.isNull(iNombre)) c.getString(iNombre) else null
            val tamanio = if (iTamanio >= 0 && !c.isNull(iTamanio)) c.getLong(iTamanio).takeIf { it > 0 } else null
            nombre to tamanio
        }
    }.getOrNull() ?: (null to null)
}

/**
 * El selector de documentos del sistema (Storage Access Framework): no pide permisos de
 * almacenamiento. Devuelve la función que lo abre: el minuto (`null` = sin minuto) y la hora se
 * toman al tocar el botón, antes de abrir el selector, y si se eligen varios archivos todos van con
 * ese minuto.
 */
@Composable
fun rememberSelectorAdjuntos(sesionId: String): (offsetMs: Long?) -> Unit {
    val context = LocalContext.current
    // Sobreviven a que Android recree la pantalla mientras el selector está abierto.
    var pedidoSesion by rememberSaveable { mutableStateOf<String?>(null) }
    var pedidoOffset by rememberSaveable { mutableLongStateOf(-1L) }
    var pedidoCreado by rememberSaveable { mutableStateOf<String?>(null) }
    var pedidoDesde by rememberSaveable { mutableLongStateOf(0L) }
    var pedidoPista by rememberSaveable { mutableStateOf<String?>(null) }
    val selector = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        val sesion = pedidoSesion ?: return@rememberLauncherForActivityResult
        CopiaAdjuntos.adjuntar(context, sesion, uris, pedidoOffset.takeIf { it >= 0 }, pedidoCreado ?: Tiempo.ahoraIso(), pedidoPista)
        pedidoSesion = null
    }
    return { offsetMs ->
        // Un doble toque no abre dos selectores (lo elegido en el segundo se perdería), salvo que el
        // anterior haya quedado colgado.
        if (pedidoSesion == null || SystemClock.elapsedRealtime() - pedidoDesde >= 2 * 60_000L) {
            pedidoSesion = sesionId
            pedidoOffset = offsetMs ?: -1L
            // Con Ambos, el minuto puede estar contado con la pista del celu: va con él.
            pedidoPista = if (offsetMs != null) ControlGrabacion.pistaActual() else null
            pedidoCreado = Tiempo.ahoraIso()
            pedidoDesde = SystemClock.elapsedRealtime()
            try {
                selector.launch(CopiaAdjuntos.TIPOS)
            } catch (e: ActivityNotFoundException) {
                pedidoSesion = null
                Toast.makeText(context, "No hay ninguna app para elegir archivos", Toast.LENGTH_LONG).show()
            }
        }
    }
}
