package ar.classmer.ui

import android.Manifest
import android.content.ActivityNotFoundException
import android.widget.Toast
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TimePicker
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.material3.rememberTimePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import ar.classmer.calendario.ClaseDelCalendario
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.importacion.Importador
import ar.classmer.modelo.SesionLocal
import ar.classmer.util.Tiempo
import kotlinx.coroutines.launch
import java.time.Instant
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * "Importar clase": cargar el audio de una clase que no se grabó (típicamente, el que mandó un amigo
 * por WhatsApp), con fotos y material opcionales, y que siga el camino de siempre (ver [ar.classmer.importacion.Importador]).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ImportarScreen(onVolver: () -> Unit, onImportada: (SesionLocal) -> Unit, vm: ImportarViewModel = viewModel()) {
    val estado by vm.estado.collectAsStateWithLifecycle()
    val grabacion by ControlGrabacion.estado.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    // De cero al entrar, pero no al volver a armar la pantalla (girar el celu, volver del selector).
    var empezada by rememberSaveable { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        if (!empezada) {
            vm.empezar()
            empezada = true
        }
    }
    fun volver() {
        vm.salir()
        onVolver()
    }
    BackHandler(onBack = ::volver)
    // Se importó (también si se giró el celu mientras se guardaba): a Inicio.
    LaunchedEffect(estado.importada) {
        estado.importada?.let { sesion ->
            vm.olvidarImportada()
            Toast.makeText(context, "Importando la clase: se copia al celu y después se sube.", Toast.LENGTH_LONG).show()
            onImportada(sesion)
        }
    }

    val selectorAudio = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri -> uri?.let(vm::elegirAudio) }
    // Photo Picker: devuelve el archivo original, con su EXIF (Android sólo le saca la ubicación), que
    // es lo que usa el server para ubicar cada foto en la clase (ver android/README.md).
    val selectorFotos = rememberLauncherForActivityResult(ActivityResultContracts.PickMultipleVisualMedia()) { vm.agregarFotos(it) }
    val selectorMaterial = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { vm.agregarMaterial(it) }
    val permisoCalendario = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { vm.leerCalendario() }
    fun abrir(lanzar: () -> Unit) {
        try {
            lanzar()
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(context, "No hay ninguna app para elegir archivos", Toast.LENGTH_LONG).show()
        }
    }

    var elegirMateria by remember { mutableStateOf(false) }
    var elegirFecha by remember { mutableStateOf(false) }
    var fechaElegida by remember { mutableStateOf<LocalDate?>(null) }
    var irAlDia by remember { mutableStateOf(false) }
    var confirmar by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Importar clase") },
                navigationIcon = {
                    IconButton(onClick = ::volver) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Volver") }
                },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(padding).imePadding().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(
                "Una clase que no grabaste (por ejemplo, el audio que te mandaron por WhatsApp). Sigue el camino de siempre: " +
                    "se copia al celu, se sube al server y se arma el apunte.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            // ---- Audio ----
            Seccion("Audio de la clase") {
                val audio = estado.audio
                when {
                    estado.leyendoAudio -> Row(verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(8.dp))
                        Text("Leyendo el audio…", style = MaterialTheme.typography.bodyMedium)
                    }
                    audio != null -> {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text(audio.nombre ?: "Audio", style = MaterialTheme.typography.bodyLarge, maxLines = 2, overflow = TextOverflow.Ellipsis)
                                Text(
                                    listOfNotNull(
                                        audio.bytes?.let(Importador::mb),
                                        audio.duracionMs?.let { Tiempo.duracion(it) } ?: "duración desconocida (la mide el server)",
                                    ).joinToString(" · "),
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                                audio.fechaMs?.let {
                                    Text(
                                        "Fecha en el archivo: ${Tiempo.fechaCorta(it)} (puede ser la hora en que se mandó)",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                            }
                            IconButton(onClick = vm::quitarAudio) { Icon(Icons.Default.Close, contentDescription = "Sacar el audio") }
                        }
                        TextButton(onClick = { abrir { selectorAudio.launch(Importador.TIPOS_AUDIO) } }) { Text("Elegir otro") }
                    }
                    else -> FilledTonalButton(onClick = { abrir { selectorAudio.launch(Importador.TIPOS_AUDIO) } }, modifier = Modifier.fillMaxWidth()) {
                        Text("Elegir el audio")
                    }
                }
                estado.problemaAudio?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
                Text(
                    "Audio o video (.opus, .ogg, .m4a, .mp4, .mp3…), hasta 2 GB.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            // ---- Fotos ----
            Seccion("Fotos (opcional)") {
                estado.fotos.forEach { f -> FilaArchivo(f) { vm.quitarFoto(f.uri) } }
                OutlinedButton(
                    onClick = {
                        abrir { selectorFotos.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)) }
                    },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("Agregar fotos de la galería") }
                Text(
                    "Se ubican en la clase por la hora en que se sacaron (EXIF). Las que no la tienen (por ejemplo, las que " +
                        "pasaron por WhatsApp) quedan sin hora. Hasta 50 MB cada una.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            // ---- Material ----
            Seccion("Material (opcional)") {
                estado.material.forEach { m -> FilaArchivo(m) { vm.quitarMaterial(m.uri) } }
                OutlinedButton(onClick = { abrir { selectorMaterial.launch(CopiaAdjuntos.TIPOS) } }, modifier = Modifier.fillMaxWidth()) {
                    Text("📎 Agregar PDF o imagen")
                }
                Text(
                    "Las slides o las notas del profe: van como adjuntos de la clase. Hasta 100 MB cada uno.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            estado.avisos.takeIf { it.isNotEmpty() }?.let { avisos ->
                Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        avisos.forEach { Text(it, color = MaterialTheme.colorScheme.onErrorContainer) }
                        TextButton(onClick = vm::olvidarAvisos, modifier = Modifier.align(Alignment.End)) { Text("Entendido") }
                    }
                }
            }

            // ---- ¿Qué clase es? ----
            Seccion("¿Qué clase es?") {
                val e = estado.eleccion
                Row(verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = vm::diaAnterior) { Text("‹", style = MaterialTheme.typography.titleLarge) }
                    Text(
                        formatoDia(e.dia),
                        style = MaterialTheme.typography.titleSmall,
                        modifier = Modifier.weight(1f).clickable { irAlDia = true },
                    )
                    TextButton(onClick = vm::diaSiguiente) { Text("›", style = MaterialTheme.typography.titleLarge) }
                }
                if (!estado.calendarioLeido) {
                    Text(
                        "Sin acceso al calendario: elegí a mano.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    TextButton(onClick = { permisoCalendario.launch(Manifest.permission.READ_CALENDAR) }) { Text("Dar acceso al calendario") }
                } else if (estado.clases.isEmpty()) {
                    Text(
                        "No hay clases en el calendario del celu ese día (puede tener sólo las semanas cercanas). Elegí a mano.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                estado.clases.forEach { c -> OpcionClase(c, elegida = e.clave == c.clave) { vm.elegirClase(c) } }
                OpcionMateria("Elegir a mano", "Materia (o Auto), tipo, fecha y hora", e.aMano, vm::elegirAMano)

                if (e.aMano) {
                    OutlinedCard(onClick = { elegirMateria = true }, modifier = Modifier.fillMaxWidth()) {
                        Row(modifier = Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text("Materia", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                                Text(estado.materia?.nombre ?: "Auto (IA)", style = MaterialTheme.typography.bodyLarge)
                            }
                            Icon(Icons.Default.ArrowDropDown, contentDescription = "Elegir materia")
                        }
                    }
                }
                if (e.elegida) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        estado.tiposClase.forEach { tipo ->
                            FilterChip(selected = e.tipoClase == tipo, onClick = { vm.elegirTipo(tipo) }, label = { Text(nombreTipo(tipo)) })
                        }
                    }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text("Inicio", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            Text(
                                e.inicioMs?.let { Tiempo.fechaCorta(it) } ?: "Falta elegir la hora",
                                style = MaterialTheme.typography.bodyLarge,
                                color = if (e.inicioMs == null) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
                            )
                        }
                        TextButton(onClick = { elegirFecha = true }) { Text("Cambiar") }
                    }
                    if (e.materiaId != null) {
                        OutlinedTextField(
                            value = e.numero?.toString() ?: "",
                            onValueChange = { t -> vm.cambiarNumero(t.filter(Char::isDigit).take(3).toIntOrNull()) },
                            label = { Text("Número de clase") },
                            supportingText = { Text(if (e.numero == null) "Sin número: lo pone el server" else "El próximo de la materia; cambialo si es otra") },
                            singleLine = true,
                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                }
            }

            estado.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            if (grabacion.grabando) {
                Text(
                    "Hay una grabación en curso: importá la clase cuando termine.",
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            Button(
                onClick = { confirmar = true },
                enabled = estado.lista && !grabacion.grabando,
                modifier = Modifier.fillMaxWidth().height(56.dp),
            ) {
                if (estado.importando) {
                    CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
                    Spacer(Modifier.width(8.dp))
                }
                Text("Importar y procesar", style = MaterialTheme.typography.titleMedium)
            }
        }
    }

    if (elegirMateria) {
        AlertDialog(
            onDismissRequest = { elegirMateria = false },
            title = { Text("Materia") },
            text = {
                Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                    OpcionMateria("Auto (IA)", "La materia la decide el server", estado.eleccion.materiaId == null) {
                        vm.elegirMateria(null)
                        elegirMateria = false
                    }
                    estado.materias.forEach { m ->
                        OpcionMateria(m.nombre, null, estado.eleccion.materiaId == m.id) {
                            vm.elegirMateria(m.id)
                            elegirMateria = false
                        }
                    }
                    if (estado.materias.isEmpty()) {
                        Text("No hay materias: el server todavía no respondió.", style = MaterialTheme.typography.bodySmall)
                    }
                }
            },
            confirmButton = { TextButton(onClick = { elegirMateria = false }) { Text("Cerrar") } },
        )
    }

    // Ir a un día de la lista sin tocar ‹ › muchas veces.
    if (irAlDia) {
        DialogoFecha(estado.eleccion.dia, onCerrar = { irAlDia = false }) { vm.cambiarDia(it) }
    }

    // El inicio: primero la fecha y después la hora.
    if (elegirFecha) {
        val inicio = estado.eleccion.inicioMs
        val dia = inicio?.let { Instant.ofEpochMilli(it).atZone(ZoneId.systemDefault()).toLocalDate() } ?: estado.eleccion.dia
        DialogoFecha(dia, onCerrar = { elegirFecha = false }) { fechaElegida = it }
    }
    fechaElegida?.let { fecha ->
        val zona = ZoneId.systemDefault()
        // La hora con que arranca: la del inicio, si no la de los datos del audio, si no la de ahora.
        val base = estado.eleccion.inicioMs ?: estado.audio?.fechaMs ?: System.currentTimeMillis()
        val hora = Instant.ofEpochMilli(base).atZone(zona).toLocalTime()
        DialogoHora(hora, onCerrar = { fechaElegida = null }) { h ->
            vm.cambiarInicio(fecha.atTime(h).atZone(zona).toInstant().toEpochMilli())
        }
    }

    if (confirmar) {
        val fotos = estado.fotos.size
        val material = estado.material.size
        AlertDialog(
            onDismissRequest = { if (!estado.importando) confirmar = false },
            title = { Text("¿Importar y procesar?") },
            text = {
                Text(
                    buildString {
                        append("Se copia al celu el audio")
                        estado.audio?.bytes?.let { append(" (${Importador.mb(it)})") }
                        if (fotos > 0) append(" y ${ResumenAdjuntos.plural(fotos, "foto", "fotos")}")
                        if (material > 0) append(", con ${ResumenAdjuntos.plural(material, "adjunto", "adjuntos")} de material,")
                        append(" se sube al server y se arma un apunte nuevo. Cuesta lo mismo que una clase grabada ")
                        append("(con la API, alrededor de US$ 1–2).")
                    },
                )
            },
            confirmButton = {
                TextButton(
                    enabled = !estado.importando,
                    onClick = {
                        // El error queda en el estado; si sale bien, vuelve a Inicio (ver estado.importada).
                        scope.launch {
                            runCatching { vm.importar() }
                            confirmar = false
                        }
                    },
                ) { Text("Importar y procesar") }
            },
            dismissButton = { TextButton(onClick = { confirmar = false }, enabled = !estado.importando) { Text("Cancelar") } },
        )
    }
}

@Composable
private fun Seccion(titulo: String, contenido: @Composable () -> Unit) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(titulo, style = MaterialTheme.typography.titleMedium)
            contenido()
        }
    }
}

@Composable
private fun FilaArchivo(a: ArchivoElegido, onQuitar: () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(modifier = Modifier.weight(1f)) {
            Text(a.nombre ?: "Archivo", style = MaterialTheme.typography.bodyMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
            a.bytes?.let {
                Text(Importador.mb(it), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        IconButton(onClick = onQuitar) { Icon(Icons.Default.Close, contentDescription = "Sacar") }
    }
}

/** Una clase del calendario: horario, materia, tipo y el título del evento. */
@Composable
private fun OpcionClase(c: ClaseDelCalendario, elegida: Boolean, onClick: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(selected = elegida, onClick = onClick)
        Column {
            Text("${Tiempo.hora(c.evento.inicioMs)}–${Tiempo.hora(c.evento.finMs)} · ${c.materia.nombre}")
            Text(
                listOfNotNull(c.tipoClase?.let(::nombreTipo), c.evento.titulo).joinToString(" · "),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DialogoFecha(inicial: LocalDate, onCerrar: () -> Unit, onElegir: (LocalDate) -> Unit) {
    // El DatePicker trabaja con la medianoche UTC de cada día.
    val estado = rememberDatePickerState(initialSelectedDateMillis = inicial.atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli())
    DatePickerDialog(
        onDismissRequest = onCerrar,
        confirmButton = {
            TextButton(onClick = {
                estado.selectedDateMillis?.let { onElegir(Instant.ofEpochMilli(it).atZone(ZoneOffset.UTC).toLocalDate()) }
                onCerrar()
            }) { Text("Listo") }
        },
        dismissButton = { TextButton(onClick = onCerrar) { Text("Cancelar") } },
    ) {
        DatePicker(state = estado)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DialogoHora(inicial: LocalTime, onCerrar: () -> Unit, onElegir: (LocalTime) -> Unit) {
    val estado = rememberTimePickerState(initialHour = inicial.hour, initialMinute = inicial.minute, is24Hour = true)
    AlertDialog(
        onDismissRequest = onCerrar,
        title = { Text("Hora de inicio") },
        text = { TimePicker(state = estado) },
        confirmButton = {
            TextButton(onClick = {
                onElegir(LocalTime.of(estado.hour, estado.minute))
                onCerrar()
            }) { Text("Listo") }
        },
        dismissButton = { TextButton(onClick = onCerrar) { Text("Cancelar") } },
    )
}

private val formatoDiaLargo = DateTimeFormatter.ofPattern("EEEE d/M", Locale.forLanguageTag("es-AR"))

private fun formatoDia(dia: LocalDate): String {
    val hoy = LocalDate.now()
    val nombre = dia.format(formatoDiaLargo)
    return when (dia) {
        hoy -> "Hoy, $nombre"
        hoy.minusDays(1) -> "Ayer, $nombre"
        else -> nombre.replaceFirstChar { it.uppercase() }
    }
}
