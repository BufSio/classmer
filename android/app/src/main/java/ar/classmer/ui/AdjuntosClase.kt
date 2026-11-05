package ar.classmer.ui

import android.content.Context
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.EstadoSubidaAdjuntos
import ar.classmer.modelo.SoporteAdjuntos
import ar.classmer.red.ClienteApi
import ar.classmer.red.ErrorApi
import ar.classmer.red.ErrorAutenticacion
import ar.classmer.red.ResultadoRegenerar
import ar.classmer.red.detalleDe
import ar.classmer.subida.PlanificadorSubida
import ar.classmer.subida.SincronizacionAdjuntos
import ar.classmer.subida.SubidorAdjuntos
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException

/**
 * "📎 Adjuntar" de la pantalla de grabación: los archivos van con el minuto del momento en que se toca.
 * Debajo, lo que se está copiando, cuántos adjuntos tiene la clase y lo que no se pudo adjuntar.
 */
@Composable
fun BotonAdjuntarGrabando(sesionId: String) {
    val context = LocalContext.current
    val abrir = rememberSelectorAdjuntos(sesionId)
    val adjuntos = remember { AlmacenAdjuntos.de(context) }
    val copias by CopiaAdjuntos.copias.collectAsStateWithLifecycle()
    val problemas by CopiaAdjuntos.problemas.collectAsStateWithLifecycle()
    val copiando = copias.filter { it.sesionId == sesionId }
    var cantidad by remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) { CopiaAdjuntos.retomar(context) }
    LaunchedEffect(sesionId, copiando.size) {
        while (true) {
            cantidad = withContext(Dispatchers.IO) { adjuntos.listar(sesionId).size }
            delay(5_000)
        }
    }

    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        FilledTonalButton(
            onClick = { abrir(ControlGrabacion.offsetActualMs()) },
            modifier = Modifier.fillMaxWidth().height(56.dp),
        ) {
            Text("📎 Adjuntar PDF o imagen", style = MaterialTheme.typography.titleMedium)
        }
        if (copiando.isNotEmpty()) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
                Spacer(Modifier.width(6.dp))
                Text(
                    "Copiando " + copiando.joinToString(", ") { it.nombre } + "…",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        } else if (cantidad > 0) {
            Text(
                "📎 ${ResumenAdjuntos.plural(cantidad, "adjunto", "adjuntos")} en esta clase",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth(),
            )
        }
        problemas?.takeIf { it.sesionId == sesionId }?.let { TarjetaProblemas(it) }
    }
}

/** Lo que no se pudo adjuntar en la última tanda, hasta que se toque "Entendido". */
@Composable
private fun TarjetaProblemas(p: CopiaAdjuntos.Problemas) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
        Column(modifier = Modifier.padding(12.dp)) {
            p.mensajes.forEach { Text(it, color = MaterialTheme.colorScheme.onErrorContainer) }
            TextButton(onClick = CopiaAdjuntos::olvidarProblemas, modifier = Modifier.align(Alignment.End)) { Text("Entendido") }
        }
    }
}

/** En la fila de una clase: el aviso si el apunte no usa algún adjunto, un resumen y el botón "Adjuntos". */
@Composable
fun AdjuntosDeFila(fila: FilaSesion, onAbrir: () -> Unit) {
    val context = LocalContext.current
    // Copias que quedaron cortadas porque Android cerró la app (una vez por proceso).
    LaunchedEffect(Unit) { CopiaAdjuntos.retomar(context) }
    val r = fila.adjuntos
    if (r.regenerable) {
        Text(
            "⚠ Hay adjuntos que el apunte no usa",
            style = MaterialTheme.typography.bodySmall,
            color = RojoGrabando,
        )
    }
    r.texto?.let {
        Text(
            it,
            style = MaterialTheme.typography.bodySmall,
            color = if (r.conProblemas) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
    TextButton(onClick = onAbrir) { Text(r.etiquetaBoton) }
}

/**
 * Al final de la lista de Inicio: adjuntos de clases que el server no encuentra y que no tienen fila
 * (por ejemplo, una clase que estaba sólo en el server y se borró desde el panel). Desde acá se abre
 * su hoja para reintentarlos o descartarlos. Si no hay ninguno, no muestra nada.
 */
@Composable
fun AvisoAdjuntosSinClase(filas: List<FilaSesion>, onAbrir: (String) -> Unit) {
    val context = LocalContext.current
    val adjuntos = remember { AlmacenAdjuntos.de(context) }
    var perdidas by remember { mutableStateOf<List<Pair<String, Int>>>(emptyList()) }
    LaunchedEffect(Unit) {
        while (true) {
            perdidas = withContext(Dispatchers.IO) {
                adjuntos.sesiones().mapNotNull { s ->
                    val subida = adjuntos.leerSubida(s)
                    if (!subida.claseNoEncontrada) return@mapNotNull null
                    val n = adjuntos.listar(s).count { !subida.de(it.id).subido }
                    if (n == 0) null else s to n
                }
            }
            delay(10_000)
        }
    }
    val ids = filas.map { it.id }.toSet()
    val visibles = perdidas.filter { it.first !in ids }
    if (visibles.isEmpty()) return
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text("Adjuntos de clases que el server no encuentra", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onErrorContainer)
            visibles.forEach { (id, n) ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        ResumenAdjuntos.plural(n, "adjunto", "adjuntos") + (ResumenAdjuntos.diaDeId(id)?.let { " de la clase del $it" } ?: ""),
                        color = MaterialTheme.colorScheme.onErrorContainer,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = { onAbrir(id) }) { Text("Ver") }
                }
            }
        }
    }
}

/** Lo que hay en el celu de los adjuntos de una clase (se relee seguido mientras la hoja está abierta). */
private data class AdjuntosEnCelu(
    val locales: List<AdjuntoLocal> = emptyList(),
    val subida: EstadoSubidaAdjuntos = EstadoSubidaAdjuntos(),
    val claseEnServidor: Boolean = true,
    val pedidosReintento: Set<String> = emptySet(),
    val soporte: SoporteAdjuntos = SoporteAdjuntos(),
)

/**
 * La hoja de adjuntos de una clase (de Inicio; sirve también para las que sólo están en el server):
 * la lista con su estado, "Adjuntar" (sin minuto) y, si el apunte no usa alguno, "Regenerar".
 *
 * @param fila la fila de la clase en Inicio, si está (trae lo que informa el server).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HojaAdjuntos(sesionId: String, fila: FilaSesion?, onRefrescar: () -> Unit, onCerrar: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val adjuntos = remember { AlmacenAdjuntos.de(context) }
    val sesiones = remember { AlmacenSesiones.de(context) }
    val copias by CopiaAdjuntos.copias.collectAsStateWithLifecycle()
    val problemas by CopiaAdjuntos.problemas.collectAsStateWithLifecycle()
    val subiendo by SincronizacionAdjuntos.subiendo.collectAsStateWithLifecycle()
    val abrirSelector = rememberSelectorAdjuntos(sesionId)
    val copiandoAca = copias.filter { it.sesionId == sesionId }

    // Inicio refresca cada 10 s y, mientras pregunta al server, las clases que están sólo allá
    // desaparecen un momento de la lista: se sigue mostrando la última fila que hubo.
    var ultimaFila by remember(sesionId) { mutableStateOf(fila) }
    SideEffect { if (fila != null) ultimaFila = fila }
    val filaVista = fila ?: ultimaFila

    // Abrir la hoja es un buen momento para volver a probar lo pendiente.
    LaunchedEffect(sesionId) {
        CopiaAdjuntos.retomar(context)
        PlanificadorSubida.patear(context)
    }

    var enCelu by remember { mutableStateOf(AdjuntosEnCelu()) }
    // Se relee al cambiar las copias en curso (terminó una), al pedir un reintento o al descartar.
    var relecturas by remember { mutableIntStateOf(0) }
    LaunchedEffect(sesionId, copiandoAca.size, relecturas) {
        while (true) {
            enCelu = withContext(Dispatchers.IO) {
                val locales = adjuntos.listar(sesionId)
                AdjuntosEnCelu(
                    locales = locales,
                    subida = adjuntos.leerSubida(sesionId),
                    claseEnServidor = SubidorAdjuntos.claseEnServidor(sesiones, sesionId),
                    pedidosReintento = locales.filter { adjuntos.archivoPedidoReintento(sesionId, it.id).exists() }.map { it.id }.toSet(),
                    soporte = adjuntos.leerSoporte(),
                )
            }
            delay(2_000)
        }
    }

    // Lo del celu (recién leído) y, si está, lo que informó el server en el último refresco de Inicio.
    val local = ResumenAdjuntos.de(enCelu.locales, enCelu.subida, null, enCelu.claseEnServidor, enCelu.soporte)
    val resumen = filaVista?.adjuntos ?: local
    val vistas = copiandoAca.map { VistasAdjuntos.copiando(it.clave, it.nombre) } +
        VistasAdjuntos.lista(
            enCelu.locales, enCelu.subida, resumen.servidor, enCelu.claseEnServidor,
            enCelu.pedidosReintento, enCelu.soporte, subiendo,
        )

    var confirmarRegenerar by remember { mutableStateOf(false) }
    var regenerando by remember { mutableStateOf(false) }
    var mensaje by remember { mutableStateOf<Pair<Boolean, String>?>(null) }
    var descartarDe by remember { mutableStateOf<VistaAdjunto?>(null) }

    ModalBottomSheet(
        onDismissRequest = onCerrar,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
    ) {
        Column(
            modifier = Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp).padding(bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text("Adjuntos", style = MaterialTheme.typography.titleLarge)
            val encabezado = filaVista?.let { f ->
                listOfNotNull(f.materia, FilasSesion.textoNumero(f.numeroClase), f.fecha.ifEmpty { null }).joinToString(" · ")
            } ?: ResumenAdjuntos.diaDeId(sesionId)?.let { "Clase del $it" }
            encabezado?.let {
                Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Text(
                "PDF de las slides o de las notas del profe, o imágenes: el apunte los usa como material de referencia.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            if (resumen.regenerable) {
                Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)) {
                    Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(
                            "Hay ${ResumenAdjuntos.plural(resumen.sinUsar, "adjunto que el apunte vigente no usa", "adjuntos que el apunte vigente no usa")}.",
                            color = MaterialTheme.colorScheme.onSecondaryContainer,
                        )
                        Button(onClick = { confirmarRegenerar = true }, enabled = !regenerando) {
                            Text("Regenerar el apunte con los adjuntos")
                        }
                    }
                }
            }
            if (local.sinSoporte) {
                Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
                    Text(
                        "El server todavía no acepta adjuntos (falta actualizarlo). Quedan guardados en el celu y se suben solos " +
                            "cuando los acepte: se le vuelve a preguntar cada 10 minutos, o ya con \"Reintentar\".",
                        modifier = Modifier.padding(12.dp),
                        color = MaterialTheme.colorScheme.onErrorContainer,
                    )
                }
            }
            problemas?.takeIf { it.sesionId == sesionId }?.let { TarjetaProblemas(it) }
            mensaje?.let { (ok, texto) ->
                Text(
                    texto,
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (ok) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error,
                )
            }

            if (vistas.isEmpty()) {
                Text(
                    "Todavía no hay adjuntos. Se pueden agregar ahora o cuando llegue el material (por mail o por el campus).",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            vistas.forEachIndexed { i, v ->
                if (i > 0) HorizontalDivider()
                FilaAdjunto(
                    v,
                    onReintentar = {
                        scope.launch {
                            withContext(Dispatchers.IO) { runCatching { adjuntos.archivoPedidoReintento(sesionId, v.id).createNewFile() } }
                            PlanificadorSubida.patear(context)
                            relecturas++
                        }
                    },
                    onDescartar = { descartarDe = v },
                )
            }

            Spacer(Modifier.height(4.dp))
            FilledTonalButton(onClick = { abrirSelector(null) }, modifier = Modifier.fillMaxWidth().height(56.dp)) {
                Text("📎 Adjuntar PDF o imagen", style = MaterialTheme.typography.titleMedium)
            }
        }
    }

    descartarDe?.let { v ->
        AlertDialog(
            onDismissRequest = { descartarDe = null },
            title = { Text("¿Descartar el adjunto?") },
            text = { Text("${v.nombre} se borra del celu y no se sube. Si lo necesitás, lo podés volver a adjuntar.") },
            confirmButton = {
                TextButton(onClick = {
                    descartarDe = null
                    scope.launch {
                        withContext(Dispatchers.IO) { runCatching { adjuntos.descartar(sesionId, v.id) } }
                        relecturas++
                    }
                }) { Text("Descartar") }
            },
            dismissButton = { TextButton(onClick = { descartarDe = null }) { Text("Cancelar") } },
        )
    }

    if (confirmarRegenerar) {
        AlertDialog(
            onDismissRequest = { if (!regenerando) confirmarRegenerar = false },
            title = { Text("¿Regenerar el apunte?") },
            text = {
                Text(
                    "Se vuelve a escribir el apunte entero, ahora con los adjuntos. Cuesta lo mismo que un apunte nuevo " +
                        "y tarda lo mismo. Necesita conexión con el server.",
                )
            },
            confirmButton = {
                TextButton(
                    enabled = !regenerando,
                    onClick = {
                        regenerando = true
                        scope.launch {
                            mensaje = regenerar(context, sesionId)
                            regenerando = false
                            confirmarRegenerar = false
                            onRefrescar()
                        }
                    },
                ) {
                    if (regenerando) {
                        CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(8.dp))
                    }
                    Text("Regenerar")
                }
            },
            dismissButton = { TextButton(onClick = { confirmarRegenerar = false }, enabled = !regenerando) { Text("Cancelar") } },
        )
    }
}

@Composable
private fun FilaAdjunto(v: VistaAdjunto, onReintentar: () -> Unit, onDescartar: () -> Unit) {
    Row(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.Top) {
        Text(if (v.esPdf) "📄" else "🖼", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(end = 12.dp))
        Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(v.nombre, style = MaterialTheme.typography.bodyLarge, maxLines = 2, overflow = TextOverflow.Ellipsis)
            if (v.detalle.isNotEmpty()) {
                Text(v.detalle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (v.enCurso) {
                    CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(6.dp))
                }
                Text(v.estado, style = MaterialTheme.typography.labelLarge, color = colorDe(v.tono))
            }
            v.explicacion?.let {
                Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (v.reintentable || v.descartable) {
                Row {
                    if (v.reintentable) TextButton(onClick = onReintentar) { Text("Reintentar") }
                    if (v.descartable) TextButton(onClick = onDescartar) { Text("Descartar") }
                }
            }
        }
    }
}

@Composable
private fun colorDe(tono: Tono): Color = when (tono) {
    Tono.ACTIVO -> RojoGrabando
    Tono.OK -> MaterialTheme.colorScheme.primary
    Tono.ERROR -> MaterialTheme.colorScheme.error
    Tono.NORMAL -> MaterialTheme.colorScheme.onSurface
}

/**
 * `POST /api/sesiones/{id}/regenerar`. Necesita conexión: si falla, no queda nada pendiente.
 * Devuelve (salió bien, texto para mostrar).
 */
private suspend fun regenerar(context: Context, sesionId: String): Pair<Boolean, String> = withContext(Dispatchers.IO) {
    val ajustes = Ajustes(context)
    if (!ajustes.configurado) return@withContext false to "Configurá el server en Ajustes."
    val api = runCatching { ClienteApi(ajustes.urlServidor, ajustes.token) }.getOrNull()
        ?: return@withContext false to "La URL del server no es válida: revisala en Ajustes."
    try {
        when (val r = api.regenerar(sesionId)) {
            is ResultadoRegenerar.Ok -> {
                r.sesion?.let { s -> runCatching { AlmacenSesiones.de(context).guardarEstadoServidor(s) } }
                true to "Listo: la clase quedó en cola y el apunte se vuelve a escribir con los adjuntos."
            }
            is ResultadoRegenerar.Rechazado -> false to "El server no la regeneró: ${r.detalle}"
            is ResultadoRegenerar.NoEncontrada -> false to "El server no encontró esta clase."
            ResultadoRegenerar.SinSoporte -> false to "El server todavía no tiene esta función (falta actualizarlo)."
        }
    } catch (e: ErrorAutenticacion) {
        ajustes.errorAutenticacion = e.message
        false to "El server rechazó el token. Revisalo en Ajustes."
    } catch (e: ErrorApi) {
        false to "El server no la regeneró: ${detalleDe(e.cuerpo)}"
    } catch (e: IOException) {
        false to "No hay conexión con el server: no se regeneró nada."
    }
}
