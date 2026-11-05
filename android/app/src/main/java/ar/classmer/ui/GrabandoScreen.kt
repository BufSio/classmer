package ar.classmer.ui

import android.content.ActivityNotFoundException
import android.os.SystemClock
import android.view.HapticFeedbackConstants
import android.widget.Toast
import androidx.activity.compose.LocalActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.FileProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.grabacion.BorradorNota
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.modelo.EstadoSubida
import ar.classmer.modelo.FotoPendiente
import ar.classmer.modelo.TipoMarca
import ar.classmer.subida.PlanificadorSubida
import ar.classmer.util.Tiempo
import ar.classmer.util.Uuid7
import ar.classmer.watch.TarjetaWatch
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GrabandoScreen(onIrAInicio: () -> Unit) {
    val estado by ControlGrabacion.estado.collectAsStateWithLifecycle()
    val sesion = estado.sesion ?: return
    val context = LocalContext.current
    val view = LocalView.current
    val almacen = remember { AlmacenSesiones.de(context) }
    val scope = rememberCoroutineScope()
    val snackbar = remember { SnackbarHostState() }

    // Mientras se ve esta pantalla, aparece sobre el bloqueo: en clase alcanza con apretar el botón
    // de encendido para marcar ⭐/❓ sin desbloquear. Al salir de acá vuelve a pedir desbloqueo.
    val actividad = LocalActivity.current
    DisposableEffect(actividad) {
        actividad?.setShowWhenLocked(true)
        onDispose { actividad?.setShowWhenLocked(false) }
    }

    // Cronómetro: tiempo de audio grabado, sin contar las pausas.
    var offset by remember { mutableLongStateOf(ControlGrabacion.offsetActualMs()) }
    // Hora del celu, para la cuenta regresiva de "¿Sigue la clase?".
    var ahora by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            offset = ControlGrabacion.offsetActualMs()
            ahora = System.currentTimeMillis()
            delay(200)
        }
    }

    // Avance de la subida de esta clase.
    var subida by remember { mutableStateOf<EstadoSubida?>(null) }
    var bytesLocales by remember { mutableLongStateOf(0L) }
    // También por el archivo: una de Ambos que pasa al teléfono sigue en audio.celu.ogg.
    LaunchedEffect(sesion.id, sesion.archivoAudio) {
        while (true) {
            withContext(Dispatchers.IO) {
                subida = almacen.leerSubida(sesion.id)
                bytesLocales = almacen.archivoAudio(sesion).length()
            }
            delay(3_000)
        }
    }

    LaunchedEffect(estado.aviso?.n) {
        estado.aviso?.let { snackbar.showSnackbar(it.texto) }
    }

    // Foto: la cámara del sistema guarda directo en la carpeta de la sesión. El offset es el del
    // momento en que se tocó "Foto". Antes de abrir la cámara se deja anotada la foto pendiente:
    // si la respuesta de la cámara no llega, la foto se registra igual al subir la clase.
    var fotoPendienteId by rememberSaveable { mutableStateOf<String?>(null) }
    var fotoPendienteSesion by rememberSaveable { mutableStateOf<String?>(null) }
    var fotoPendienteOffset by rememberSaveable { mutableLongStateOf(0L) }
    var fotoPendientePista by rememberSaveable { mutableStateOf<String?>(null) }
    var fotoPendienteDesde by rememberSaveable { mutableLongStateOf(0L) }
    val camara = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { ok ->
        val id = fotoPendienteId ?: return@rememberLauncherForActivityResult
        val sesionFoto = fotoPendienteSesion ?: sesion.id
        val relativo = "${AlmacenSesiones.DIR_FOTOS}/$id.jpg"
        val archivo = File(almacen.dir(sesionFoto), relativo)
        if (ok && archivo.length() > 0) {
            if (ControlGrabacion.estado.value.sesion?.id == sesionFoto) {
                ControlGrabacion.foto(context, id, fotoPendienteOffset, relativo, fotoPendientePista)
            } else {
                // La grabación terminó con la cámara abierta: se registra igual con su minuto.
                scope.launch(Dispatchers.IO) {
                    runCatching { almacen.resolverFotosPendientes(sesionFoto) }
                    PlanificadorSubida.patear(context)
                }
            }
        } else {
            archivo.delete()
            almacen.olvidarFotoPendiente(sesionFoto, id)
            // Si la grabación ya terminó, la clase puede estar esperando esta foto para finalizar.
            PlanificadorSubida.patear(context)
        }
        fotoPendienteId = null
    }
    fun sacarFoto() {
        // Evita abrir dos cámaras con un doble toque (salvo que la anterior haya quedado colgada).
        if (fotoPendienteId != null && SystemClock.elapsedRealtime() - fotoPendienteDesde < 2 * 60_000L) return
        val id = Uuid7.nuevo()
        val offset = ControlGrabacion.offsetActualMs()
        val pista = ControlGrabacion.pistaActual()
        val dir = almacen.dirFotos(sesion.id).apply { mkdirs() }
        val archivo = File(dir, "$id.jpg")
        runCatching { almacen.marcarFotoPendiente(sesion.id, FotoPendiente(id, offset, Tiempo.ahoraIso(), pista)) }
        fotoPendienteId = id
        fotoPendienteSesion = sesion.id
        fotoPendienteOffset = offset
        fotoPendientePista = pista
        fotoPendienteDesde = SystemClock.elapsedRealtime()
        try {
            camara.launch(FileProvider.getUriForFile(context, "${context.packageName}.archivos", archivo))
        } catch (e: ActivityNotFoundException) {
            fotoPendienteId = null
            almacen.olvidarFotoPendiente(sesion.id, id)
            Toast.makeText(context, "No hay app de cámara", Toast.LENGTH_LONG).show()
        }
    }

    // Nota: el offset es el del momento en que se abre el diálogo.
    // El borrador vive en ControlGrabacion: no se pierde al ir a Inicio y volver. Si la grabación
    // siguió en una sesión nueva (falla de la grabadora), el minuto pasa a ser el de la sesión nueva.
    var notaAbierta by rememberSaveable { mutableStateOf(false) }
    var borrador by remember(sesion.id) {
        mutableStateOf(
            ControlGrabacion.borradorNota?.let {
                if (it.sesionId == sesion.id) it else it.copy(sesionId = sesion.id, offsetMs = ControlGrabacion.offsetActualMs(), pista = ControlGrabacion.pistaActual())
            },
        )
    }
    fun guardarBorrador(b: BorradorNota?) {
        borrador = b
        ControlGrabacion.borradorNota = b
    }
    var confirmarFin by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(sesion.nombreMateria ?: "Auto (IA)", maxLines = 1) },
                navigationIcon = {
                    IconButton(onClick = onIrAInicio) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Ir al inicio (sigue grabando)")
                    }
                },
            )
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(padding).padding(16.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                if (estado.pausada) "⏸ En pausa" else "● Grabando",
                color = if (estado.pausada) MaterialTheme.colorScheme.onSurfaceVariant else RojoGrabando,
                style = MaterialTheme.typography.titleMedium,
            )
            Text(
                Tiempo.duracion(offset),
                fontSize = 64.sp,
                fontFamily = FontFamily.Monospace,
                fontWeight = FontWeight.Light,
            )
            Text(
                "${nombreTipo(sesion.tipoClase)} · ⭐ ${estado.importantes}  ❓ ${estado.noEntendi}  📷 ${estado.fotos}  📝 ${estado.notas}",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            estado.watch?.let { TarjetaWatch(it, celu = estado.celu, onUsarTelefono = { ControlGrabacion.usarTelefono(context) }) }
            if (estado.microfonoSilenciado) {
                Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
                    Text(
                        "⚠ Otra app (por ejemplo, una llamada) está usando el micrófono: se graba silencio hasta que lo suelte.",
                        modifier = Modifier.padding(12.dp),
                        color = MaterialTheme.colorScheme.onErrorContainer,
                    )
                }
            }

            Spacer(Modifier.height(8.dp))
            FilaBotones {
                BotonGrande("📷 Foto", Modifier.weight(1f), onClick = ::sacarFoto)
                BotonGrande("📝 Nota", Modifier.weight(1f)) {
                    // Si quedó un borrador (se cerró sin querer), se retoma con su minuto original.
                    if (borrador?.texto.isNullOrBlank()) {
                        guardarBorrador(BorradorNota(sesion.id, "", ControlGrabacion.offsetActualMs(), ControlGrabacion.pistaActual()))
                    }
                    notaAbierta = true
                }
            }
            FilaBotones {
                BotonGrande("⭐ Importante", Modifier.weight(1f)) {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    ControlGrabacion.marcar(context, TipoMarca.IMPORTANTE)
                }
                BotonGrande("❓ No entendí", Modifier.weight(1f)) {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    ControlGrabacion.marcar(context, TipoMarca.NO_ENTENDI)
                }
            }
            FilaBotones {
                OutlinedButton(
                    onClick = { if (estado.pausada) ControlGrabacion.reanudar(context) else ControlGrabacion.pausar(context) },
                    modifier = Modifier.weight(1f).height(64.dp),
                ) { Text(if (estado.pausada) "▶ Reanudar" else "⏸ Pausa") }
                Button(
                    onClick = { confirmarFin = true },
                    colors = ButtonDefaults.buttonColors(containerColor = RojoGrabando, contentColor = Color.White),
                    modifier = Modifier.weight(1f).height(64.dp),
                ) { Text("■ Terminar clase") }
            }
            // PDF de las slides o imágenes, con el minuto del momento en que se toca.
            BotonAdjuntarGrabando(sesion.id)

            Spacer(Modifier.height(8.dp))
            Text(
                textoSubida(subida, bytesLocales),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
            Text(
                "Podés bloquear la pantalla: sigue grabando. Las marcas también están en la notificación.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
        }
    }

    val nota = borrador
    if (notaAbierta && nota != null) {
        AlertDialog(
            // Tocar afuera no la cierra; Atrás la cierra pero guarda el borrador.
            onDismissRequest = { notaAbierta = false },
            properties = DialogProperties(dismissOnClickOutside = false),
            title = { Text("Nota en ${Tiempo.duracion(nota.offsetMs)}") },
            text = {
                OutlinedTextField(
                    value = nota.texto,
                    onValueChange = { guardarBorrador(nota.copy(texto = it)) },
                    placeholder = { Text("Ej.: preguntar por qué el gradiente es perpendicular") },
                    minLines = 3,
                    modifier = Modifier.fillMaxWidth(),
                )
            },
            confirmButton = {
                TextButton(
                    enabled = nota.texto.isNotBlank(),
                    onClick = {
                        ControlGrabacion.nota(context, nota.texto, nota.offsetMs, nota.pista)
                        guardarBorrador(null)
                        notaAbierta = false
                    },
                ) { Text("Guardar") }
            },
            dismissButton = {
                TextButton(onClick = {
                    guardarBorrador(null)
                    notaAbierta = false
                }) { Text("Descartar") }
            },
        )
    }

    // Corte automático: la misma pregunta que la notificación, por si la app está abierta.
    estado.preguntaCorteHastaMs?.let { hasta ->
        AlertDialog(
            onDismissRequest = {},
            properties = DialogProperties(dismissOnClickOutside = false, dismissOnBackPress = false),
            title = { Text("¿Sigue la clase?") },
            text = {
                Text(
                    if (estado.preguntaCorteTerminaSola) {
                        "Si nadie responde, la grabación termina sola en ${Tiempo.duracion(hasta - ahora)} " +
                            "(a las ${Tiempo.hora(hasta)}). Lo grabado se guarda y se sube."
                    } else {
                        "Las notificaciones de Classmer están apagadas, así que la grabación no va a terminar sola: " +
                            "si la clase ya terminó, tocá Terminar."
                    },
                )
            },
            confirmButton = { TextButton(onClick = { ControlGrabacion.seguirClase(context) }) { Text("Seguir grabando") } },
            dismissButton = { TextButton(onClick = { ControlGrabacion.terminar(context) }) { Text("Terminar") } },
        )
    }

    if (confirmarFin) {
        AlertDialog(
            onDismissRequest = { confirmarFin = false },
            title = { Text("¿Terminar la clase?") },
            text = { Text("Se cierra la grabación (${Tiempo.duracion(offset)}) y se sube al server para armar el apunte.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmarFin = false
                    ControlGrabacion.terminar(context)
                }) { Text("Terminar") }
            },
            dismissButton = { TextButton(onClick = { confirmarFin = false }) { Text("Seguir grabando") } },
        )
    }
}

@Composable
private fun FilaBotones(contenido: @Composable RowScope.() -> Unit) {
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp), content = contenido)
}

@Composable
private fun BotonGrande(texto: String, modifier: Modifier, onClick: () -> Unit) {
    FilledTonalButton(onClick = onClick, modifier = modifier.height(72.dp)) {
        Text(texto, style = MaterialTheme.typography.titleMedium)
    }
}

private fun textoSubida(subida: EstadoSubida?, bytesLocales: Long): String {
    val local = mb(bytesLocales)
    if (subida == null) return "Audio: $local en el celu"
    val subido = mb(subida.bytesConfirmados)
    return when {
        subida.ultimoError != null -> "Audio: $local en el celu · subido $subido\n${subida.ultimoError}"
        else -> "Audio: $local en el celu · subido $subido"
    }
}

private fun mb(bytes: Long) = "%.1f MB".format(bytes / 1_048_576.0)
