package ar.classmer.watch

import android.os.SystemClock
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.repeatOnLifecycle
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.modelo.FuenteAudio
import kotlinx.coroutines.delay

/**
 * "Graba: 📱 Teléfono / ⌚ Reloj / ⌚📱 Ambos" en la tarjeta de Nueva clase. Recuerda la última elección y,
 * con el reloj (o los dos), muestra si está conectado, si tiene Classmer y su batería.
 */
@Composable
fun SelectorFuente(habilitado: Boolean) {
    val context = LocalContext.current
    val ajustes = remember { Ajustes(context) }
    var fuente by remember { mutableStateOf(ajustes.fuenteAudio) }
    val info by EnlaceWatch.info.collectAsStateWithLifecycle()
    var ahora by remember { mutableLongStateOf(SystemClock.elapsedRealtime()) }

    // Con el reloj elegido (solo o con el celu), se lo consulta cada ~10 s mientras se ve la pantalla.
    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(fuente, lifecycleOwner) {
        if (fuente != FuenteAudio.RELOJ && fuente != FuenteAudio.AMBOS) return@LaunchedEffect
        lifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) {
                EnlaceWatch.buscar(context)
                if (EnlaceWatch.info.value.appInstalada) EnlaceWatch.consultar(context)
                ahora = SystemClock.elapsedRealtime()
                delay(10_000)
            }
        }
    }

    fun elegir(f: String) {
        fuente = f
        ajustes.fuenteAudio = f
    }

    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("Graba:", style = MaterialTheme.typography.labelLarge)
        // Con los tres no entra "Graba:" en la misma fila del S23.
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            for ((f, etiqueta) in OPCIONES) {
                FilterChip(
                    selected = fuente == f,
                    onClick = { elegir(f) },
                    enabled = habilitado,
                    label = { Text(etiqueta) },
                )
            }
        }
        // La clase que ya graba el reloj puede tener su pista del celu (se vuelve a seguir después de un corte).
        val enCurso = info.estado?.activa?.id
        val sigueAca = ControlGrabacion.estado.value.sesion?.id == enCurso
        val celuYaGrabo = remember(enCurso, ahora, sigueAca) {
            enCurso != null && !sigueAca && PistasCelu(AlmacenSesiones.de(context)).audio(enCurso).length() > 0
        }
        val decision = ElegirFuente.decidir(fuente, info, maxOf(ahora, info.estadoEn), celuYaGrabo)
        decision.texto?.let { texto ->
            Text(
                texto,
                style = MaterialTheme.typography.bodySmall,
                color = when (decision.nivel) {
                    ElegirFuente.Nivel.OK -> MaterialTheme.colorScheme.onSurfaceVariant
                    ElegirFuente.Nivel.AVISO -> MaterialTheme.colorScheme.tertiary
                    ElegirFuente.Nivel.PROBLEMA -> MaterialTheme.colorScheme.error
                },
            )
        }
        if (decision.ofrecerTelefono && habilitado) {
            TextButton(onClick = { elegir(FuenteAudio.TELEFONO) }) {
                Text(if (fuente == FuenteAudio.AMBOS) "Grabar sólo con el teléfono" else "Grabar con el teléfono")
            }
        }
    }
}

private val OPCIONES = listOf(
    FuenteAudio.TELEFONO to "📱 Teléfono",
    FuenteAudio.RELOJ to "⌚ Reloj",
    FuenteAudio.AMBOS to "⌚📱 Ambos",
)

/**
 * En la pantalla de grabación, cuando graba el reloj: si el audio va llegando y si hay algo pendiente. Con
 * Ambos, [celu] es una línea sobre la pista del celu.
 */
@Composable
fun TarjetaWatch(estado: EstadoWatchUi, celu: String? = null, onUsarTelefono: () -> Unit) {
    val problema = estado.sinContacto || (estado.bateria != null && estado.bateria < SesionWatch.BATERIA_BAJA)
    Card(
        colors = CardDefaults.cardColors(
            containerColor = if (problema) MaterialTheme.colorScheme.errorContainer else MaterialTheme.colorScheme.secondaryContainer,
        ),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(
                estado.texto,
                color = if (problema) MaterialTheme.colorScheme.onErrorContainer else MaterialTheme.colorScheme.onSecondaryContainer,
            )
            celu?.let {
                Text(it, color = if (problema) MaterialTheme.colorScheme.onErrorContainer else MaterialTheme.colorScheme.onSecondaryContainer)
            }
            if (estado.puedeUsarTelefono) {
                OutlinedButton(onClick = onUsarTelefono, modifier = Modifier.fillMaxWidth()) {
                    // Con Ambos el celu ya graba: la clase sigue con su audio, sin cortar.
                    Text(if (celu != null) "📱 Seguir sólo con el teléfono" else "📱 Grabar con el teléfono")
                }
            }
        }
    }
}
