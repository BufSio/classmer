package ar.classmer.wear

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.wear.compose.material3.AlertDialog
import androidx.wear.compose.material3.AlertDialogDefaults
import androidx.wear.compose.material3.AppScaffold
import androidx.wear.compose.material3.Button
import androidx.wear.compose.material3.ButtonDefaults
import androidx.wear.compose.material3.FilledTonalButton
import androidx.wear.compose.material3.MaterialTheme
import androidx.wear.compose.material3.ScreenScaffold
import androidx.wear.compose.material3.Text
import ar.classmer.watchlink.EstadoGrabacionWatch
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow

class MainActivity : ComponentActivity() {

    /** El aviso "tocá para grabar" abrió la pantalla: hay que empezar a grabar ese pedido. */
    private val pedidoAlAbrir = MutableStateFlow<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        atender(intent)
        setContent {
            MaterialTheme {
                val pedido by pedidoAlAbrir.collectAsStateWithLifecycle()
                PantallaWatch(pedidoAlAbrir = pedido, onPedidoAtendido = { pedidoAlAbrir.value = null })
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        atender(intent)
    }

    override fun onStart() {
        super.onStart()
        ControlWatch.refrescar(this)
    }

    private fun atender(intent: Intent?) {
        if (intent?.action == ACCION_GRABAR_PEDIDO) pedidoAlAbrir.value = intent.getStringExtra(EXTRA_PEDIDO)
    }

    companion object {
        const val ACCION_GRABAR_PEDIDO = "ar.classmer.wear.GRABAR_PEDIDO"
        const val EXTRA_PEDIDO = "pedido"
    }
}

private val RojoGrabando = Color(0xFFE53935)

@Composable
private fun PantallaWatch(pedidoAlAbrir: String?, onPedidoAtendido: () -> Unit) {
    val context = LocalContext.current
    val estado by ControlWatch.estado.collectAsStateWithLifecycle()

    fun tiene(p: String) = ContextCompat.checkSelfPermission(context, p) == PackageManager.PERMISSION_GRANTED
    var microfono by remember { mutableStateOf(tiene(Manifest.permission.RECORD_AUDIO)) }
    var avisos by remember { mutableStateOf(tiene(Manifest.permission.POST_NOTIFICATIONS)) }
    // Grabar apenas lleguen los permisos, si eso es lo que se estaba por hacer.
    var grabarAlPermitir by remember { mutableStateOf(false) }

    fun grabar() {
        val pedido = AlmacenWatch.de(context).leerPedido()
        ControlWatch.limpiarError()
        ControlWatch.iniciar(context, pedido)
    }

    val permisos = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        microfono = tiene(Manifest.permission.RECORD_AUDIO)
        avisos = tiene(Manifest.permission.POST_NOTIFICATIONS)
        if (grabarAlPermitir && microfono) grabar()
        grabarAlPermitir = false
    }
    fun pedirPermisos(yGrabar: Boolean) {
        grabarAlPermitir = yGrabar
        permisos.launch(arrayOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS))
    }

    // Se tocó "tocá para grabar": se empieza ya (si es el pedido que sigue en pie). Si no, la primera
    // vez se piden los permisos solos: sin avisos, el celu no puede pedir que se grabe. Un solo pedido
    // de permisos a la vez: dos seguidos pisan el primero y se perdería el "grabar al permitir".
    var permisosPedidos by remember { mutableStateOf(false) }
    LaunchedEffect(pedidoAlAbrir) {
        val id = pedidoAlAbrir
        if (id != null) {
            onPedidoAtendido()
            if (estado.grabacion == null && AlmacenWatch.de(context).leerPedido()?.id == id) {
                permisosPedidos = true
                if (microfono) grabar() else pedirPermisos(yGrabar = true)
                return@LaunchedEffect
            }
        }
        if (!permisosPedidos && (!microfono || !avisos)) {
            permisosPedidos = true
            pedirPermisos(yGrabar = false)
        }
    }

    var offset by remember { mutableLongStateOf(ControlWatch.offsetActualMs()) }
    LaunchedEffect(estado.grabacion?.id) {
        while (estado.grabacion != null) {
            offset = ControlWatch.offsetActualMs()
            delay(500)
        }
    }
    var confirmarFin by remember { mutableStateOf(false) }
    val g = estado.grabacion
    // Si la grabación terminó por otro lado (desde el celu), la pregunta no queda pendiente para la próxima.
    LaunchedEffect(g == null) { if (g == null) confirmarFin = false }

    AppScaffold {
        val scroll = rememberScrollState()
        ScreenScaffold(scrollState = scroll) { padding ->
            Column(
                modifier = Modifier.fillMaxSize().verticalScroll(scroll).padding(padding).padding(horizontal = 8.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(6.dp, Alignment.CenterVertically),
            ) {
                when {
                    g != null -> {
                        val pausada = g.estado == EstadoGrabacionWatch.PAUSADA
                        Text(
                            if (pausada) "⏸ En pausa" else "● Grabando",
                            color = if (pausada) MaterialTheme.colorScheme.onSurfaceVariant else RojoGrabando,
                            style = MaterialTheme.typography.titleMedium,
                        )
                        Text(duracion(offset), style = MaterialTheme.typography.numeralMedium)
                        g.materia?.let { Text(it, style = MaterialTheme.typography.bodySmall, textAlign = TextAlign.Center, maxLines = 2) }
                        Button(
                            onClick = { confirmarFin = true },
                            colors = ButtonDefaults.buttonColors(containerColor = RojoGrabando, contentColor = Color.White),
                            modifier = Modifier.fillMaxWidth(),
                        ) { Text("■ Terminar") }
                    }
                    !microfono -> {
                        Text("Classmer necesita el micrófono para grabar la clase.", textAlign = TextAlign.Center)
                        Button(onClick = { pedirPermisos(yGrabar = false) }, modifier = Modifier.fillMaxWidth()) { Text("Permitir") }
                    }
                    else -> {
                        val pedido = estado.pedido
                        Text(
                            if (pedido != null) "El celu pidió grabar" else "Classmer",
                            style = MaterialTheme.typography.titleMedium,
                            textAlign = TextAlign.Center,
                        )
                        pedido?.materia?.let { Text(it, style = MaterialTheme.typography.bodySmall, textAlign = TextAlign.Center, maxLines = 2) }
                        Button(
                            onClick = ::grabar,
                            colors = ButtonDefaults.buttonColors(containerColor = RojoGrabando, contentColor = Color.White),
                            modifier = Modifier.fillMaxWidth(),
                        ) { Text("● Grabar") }
                        if (!avisos) {
                            FilledTonalButton(onClick = { pedirPermisos(yGrabar = false) }, modifier = Modifier.fillMaxWidth()) {
                                Text("Permitir avisos")
                            }
                        }
                        if (estado.porPasar > 0) {
                            Text(
                                if (estado.porPasar == 1) "1 clase por pasar al celu" else "${estado.porPasar} clases por pasar al celu",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                textAlign = TextAlign.Center,
                            )
                        }
                    }
                }
                estado.error?.let {
                    Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall, textAlign = TextAlign.Center)
                }
            }
        }
    }

    AlertDialog(
        visible = confirmarFin && g != null,
        onDismissRequest = { confirmarFin = false },
        title = { Text("¿Terminar la grabación?") },
        text = { Text("El audio se pasa al celu, que lo sube.") },
        confirmButton = {
            AlertDialogDefaults.ConfirmButton(onClick = {
                confirmarFin = false
                ControlWatch.terminar(context)
            })
        },
        dismissButton = { AlertDialogDefaults.DismissButton(onClick = { confirmarFin = false }) },
    )
}

/** `1:02:03` o `02:03`. */
private fun duracion(ms: Long): String {
    val totalSeg = ms.coerceAtLeast(0) / 1000
    val h = totalSeg / 3600
    val m = (totalSeg % 3600) / 60
    val s = totalSeg % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%02d:%02d".format(m, s)
}
