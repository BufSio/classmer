package ar.classmer.ui

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.SystemClock
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import ar.classmer.calendario.LectorCalendario
import ar.classmer.datos.Ajustes
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.modelo.Materia
import ar.classmer.modelo.OrigenMateria
import ar.classmer.watch.SelectorFuente
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InicioScreen(
    onAjustes: () -> Unit,
    onVerGrabacion: () -> Unit,
    onImportar: () -> Unit = {},
    vm: InicioViewModel = viewModel(),
) {
    val estado by vm.estado.collectAsStateWithLifecycle()
    val grabacion by ControlGrabacion.estado.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var elegirMateria by remember { mutableStateOf(false) }
    // Materia cuyo próximo número se está editando con el lápiz.
    var editarNumeroDe by remember { mutableStateOf<String?>(null) }
    // Clase en esperando_usuario a la que se le está eligiendo la materia.
    var materiaDeSesion by remember { mutableStateOf<FilaSesion?>(null) }
    // Clase cuya hoja de adjuntos está abierta (sobrevive a que Android recree la pantalla con el selector abierto).
    var adjuntosDe by rememberSaveable { mutableStateOf<String?>(null) }
    // Importación fallida que se está por descartar (pide confirmación).
    var descartarImportacionDe by remember { mutableStateOf<FilaSesion?>(null) }

    // Refresco periódico mientras la pantalla está visible.
    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(lifecycleOwner) {
        lifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
            vm.refrescarMaterias()
            var vueltas = 0
            while (true) {
                // El calendario también se vuelve a mirar: una clase puede empezar con la app abierta.
                vm.aplicarCalendario()
                vm.refrescarSesiones()
                delay(10_000)
                // Las materias, cada ~60 s: el número de clase se puede cambiar desde el panel de la PC.
                if (++vueltas % 6 == 0) vm.refrescarMaterias()
            }
        }
    }

    // El calendario se pide una sola vez (queda anotado en Ajustes); si se niega, la materia queda en Auto.
    val ajustes = remember { Ajustes(context) }
    val permisoCalendario = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
        vm.aplicarCalendario()
    }
    LaunchedEffect(Unit) {
        if (!ajustes.calendarioPedido && !LectorCalendario.tienePermiso(context)) {
            ajustes.calendarioPedido = true
            permisoCalendario.launch(Manifest.permission.READ_CALENDAR)
        }
    }

    // Grabar sólo necesita el micrófono. Las notificaciones se piden una vez, pero si se niegan
    // se graba igual (la notificación del servicio queda en el administrador de tareas).
    fun tieneMicrofono() =
        ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
    // Momento en que se pidió el micrófono para grabar (0 = no hay pedido en curso).
    var pedidoMicrofonoDesde by rememberSaveable { mutableLongStateOf(0L) }
    val permisosGrabar = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { r ->
        // Un resultado vacío es un pedido duplicado o cancelado por Android: se espera el verdadero.
        if (pedidoMicrofonoDesde == 0L || r.isEmpty()) return@rememberLauncherForActivityResult
        pedidoMicrofonoDesde = 0L
        if (tieneMicrofono()) {
            vm.grabar()
        } else {
            Toast.makeText(
                context,
                "Sin permiso de micrófono no se puede grabar. Si no aparece el pedido, activalo en Ajustes de Android → Apps → Classmer → Permisos.",
                Toast.LENGTH_LONG,
            ).show()
        }
    }
    fun pedirYGrabar() {
        val pedirNotificaciones = Build.VERSION.SDK_INT >= 33 && !ajustes.notificacionesPedidas &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        val pedir = buildList {
            if (!tieneMicrofono()) add(Manifest.permission.RECORD_AUDIO)
            if (pedirNotificaciones) add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (pedirNotificaciones) ajustes.notificacionesPedidas = true
        when {
            pedir.isEmpty() -> vm.grabar()
            tieneMicrofono() -> {
                // Se empieza a grabar ya; el pedido de notificaciones no lo frena.
                vm.grabar()
                permisosGrabar.launch(pedir.toTypedArray())
            }
            else -> {
                // Doble toque mientras Android muestra el pedido: no se pide dos veces.
                if (SystemClock.elapsedRealtime() - pedidoMicrofonoDesde < 10_000) return
                pedidoMicrofonoDesde = SystemClock.elapsedRealtime()
                permisosGrabar.launch(pedir.toTypedArray())
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Classmer") },
                actions = {
                    IconButton(onClick = { vm.refrescarMaterias(); vm.refrescarSesiones() }) {
                        Icon(Icons.Default.Refresh, contentDescription = "Actualizar")
                    }
                    IconButton(onClick = onAjustes) { Icon(Icons.Default.Settings, contentDescription = "Ajustes") }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(padding),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            if (grabacion.grabando) {
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer),
                        modifier = Modifier.fillMaxWidth().clickable(onClick = onVerGrabacion),
                    ) {
                        Text(
                            if (grabacion.preguntaCorteHastaMs != null) {
                                "● ¿Sigue la clase? Tocá para responder (si no, termina sola)"
                            } else {
                                "● Grabación en curso — tocá para volver"
                            },
                            modifier = Modifier.padding(16.dp),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                }
            }
            grabacion.error?.let { error ->
                item { Aviso(error, accion = "Entendido") { ControlGrabacion.limpiarError() } }
            }
            if (!estado.configurado) {
                item { Aviso("Falta configurar el server. Podés grabar igual: se sube cuando lo configures.", "Ajustes", onAjustes) }
            }
            estado.errorAutenticacion?.let {
                item { Aviso("El server rechazó el token ($it). Revisalo en Ajustes.", "Ajustes", onAjustes) }
            }

            item {
                TarjetaNuevaClase(
                    estado = estado,
                    grabando = grabacion.grabando,
                    onElegirMateria = { elegirMateria = true },
                    onEditarNumero = { editarNumeroDe = estado.materiaSeleccionada?.id },
                    onElegirTipo = vm::elegirTipo,
                    onGrabar = ::pedirYGrabar,
                    onImportar = onImportar,
                )
            }

            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Clases recientes", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                    estado.errorServidor?.let {
                        Text("sin conexión", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.error)
                    }
                }
            }
            if (estado.sesiones.isEmpty()) {
                item { Text("Todavía no hay clases grabadas.", color = MaterialTheme.colorScheme.onSurfaceVariant) }
            }
            items(estado.sesiones, key = { it.id }) { fila ->
                FilaClase(
                    fila,
                    descargando = estado.descargandoPdf == fila.id,
                    onReintentar = { vm.reintentar(fila.id) },
                    onElegirMateria = { materiaDeSesion = fila },
                    onAccionWatch = { vm.accionWatch(fila) },
                    onAdjuntos = { adjuntosDe = fila.id },
                    onReintentarImportacion = { vm.reintentarImportacion(fila.id) },
                    onDescartarImportacion = { descartarImportacionDe = fila },
                ) {
                    scope.launch {
                        try {
                            val pdf = vm.descargarPdf(fila)
                            val uri = FileProvider.getUriForFile(context, "${context.packageName}.archivos", pdf)
                            val ver = Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/pdf")
                                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            context.startActivity(Intent.createChooser(ver, "Abrir apunte"))
                        } catch (e: ActivityNotFoundException) {
                            Toast.makeText(context, "No hay ninguna app para abrir PDFs", Toast.LENGTH_LONG).show()
                        } catch (e: Exception) {
                            Toast.makeText(context, "No se pudo bajar el PDF: ${e.message}", Toast.LENGTH_LONG).show()
                        }
                    }
                }
            }
            // Adjuntos de clases que el server ya no tiene y no están en la lista (si no hay, no muestra nada).
            item { AvisoAdjuntosSinClase(estado.sesiones) { adjuntosDe = it } }
        }
    }

    if (elegirMateria) {
        DialogoMateria(
            estado = estado,
            onElegir = {
                vm.elegirMateria(it)
                elegirMateria = false
            },
            onCerrar = { elegirMateria = false },
        )
    }

    // Se edita la materia que estaba elegida al tocar el lápiz, aunque el calendario cambie la selección.
    val materiaNumero = editarNumeroDe?.let { id -> estado.materias.firstOrNull { it.id == id } }
    val numeroActual = materiaNumero?.proximoNumero
    if (materiaNumero != null && numeroActual != null) {
        DialogoNumero(
            materia = materiaNumero,
            actual = numeroActual,
            onGuardar = { vm.cambiarProximoNumero(materiaNumero.id, it) },
            onCerrar = { editarNumeroDe = null },
        )
    } else if (editarNumeroDe != null) {
        // La materia desapareció (o el server dejó de informar el número): no queda un diálogo pendiente.
        LaunchedEffect(Unit) { editarNumeroDe = null }
    }

    materiaDeSesion?.let { fila ->
        DialogoMateriaDeSesion(
            fila = fila,
            materias = estado.materias,
            enviando = estado.asignando == fila.id,
            onElegir = { vm.elegirMateriaSesion(fila.id, it) },
            onCerrar = { materiaDeSesion = null },
        )
    }

    descartarImportacionDe?.let { fila ->
        AlertDialog(
            onDismissRequest = { descartarImportacionDe = null },
            title = { Text("¿Descartar la clase importada?") },
            text = {
                Text(
                    "Se borran del celu las copias que hizo Classmer (el audio a medias, las fotos y el material de esta clase). " +
                        "Los archivos originales no se tocan. La clase nunca llegó al server.",
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    descartarImportacionDe = null
                    scope.launch {
                        vm.descartarImportacion(fila.id)?.let { Toast.makeText(context, it, Toast.LENGTH_LONG).show() }
                    }
                }) { Text("Descartar") }
            },
            dismissButton = { TextButton(onClick = { descartarImportacionDe = null }) { Text("Cancelar") } },
        )
    }

    // Adjuntos de una clase (PDF, imágenes): también de las que sólo están en el server.
    adjuntosDe?.let { id ->
        HojaAdjuntos(id, estado.sesiones.firstOrNull { it.id == id }, onRefrescar = vm::refrescarSesiones, onCerrar = { adjuntosDe = null })
    }
}

@Composable
private fun TarjetaNuevaClase(
    estado: EstadoInicio,
    grabando: Boolean,
    onElegirMateria: () -> Unit,
    onEditarNumero: () -> Unit,
    onElegirTipo: (String) -> Unit,
    onGrabar: () -> Unit,
    onImportar: () -> Unit,
) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("Nueva clase", style = MaterialTheme.typography.titleMedium)
            OutlinedCard(onClick = onElegirMateria, modifier = Modifier.fillMaxWidth()) {
                Row(modifier = Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text("Materia", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(
                            estado.materiaSeleccionada?.nombre ?: "Auto (IA)",
                            style = MaterialTheme.typography.bodyLarge,
                            maxLines = 2,
                            overflow = TextOverflow.Ellipsis,
                        )
                        val sel = estado.seleccion
                        val origen = when {
                            sel.origen == OrigenMateria.CALENDARIO -> "Del calendario: ${sel.eventoCalendario ?: ""}"
                            sel.materiaAMano -> "Elegida a mano"
                            estado.calendarioLeido -> "Ningún evento del calendario coincide ahora"
                            else -> "Sin acceso al calendario"
                        }
                        Text(origen, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Icon(Icons.Default.ArrowDropDown, contentDescription = "Elegir materia")
                }
            }
            // Número de la clase que se va a grabar: sólo con materia y si el server lo informa.
            estado.numeroClase?.let { numero ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Clase $numero", style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
                    IconButton(onClick = onEditarNumero) {
                        Icon(Icons.Default.Edit, contentDescription = "Cambiar el número de clase")
                    }
                }
            }
            if (estado.materiasDesdeCache && estado.errorMaterias != null) {
                Text(
                    "Materias guardadas en el celu (el server no respondió).",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            SelectorFuente(habilitado = !grabando)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                estado.tiposClase.forEach { tipo ->
                    FilterChip(
                        selected = estado.seleccion.tipoClase == tipo,
                        onClick = { onElegirTipo(tipo) },
                        label = { Text(nombreTipo(tipo)) },
                    )
                }
            }
            Button(
                onClick = onGrabar,
                enabled = !grabando,
                colors = ButtonDefaults.buttonColors(containerColor = RojoGrabando, contentColor = Color.White),
                modifier = Modifier.fillMaxWidth().height(64.dp),
            ) {
                Text("●  Grabar clase", style = MaterialTheme.typography.titleMedium)
            }
            // Una clase que no se grabó (por ejemplo, el audio que mandó un amigo): no mientras se graba.
            OutlinedButton(onClick = onImportar, enabled = !grabando, modifier = Modifier.fillMaxWidth()) {
                Text("Importar clase")
            }
        }
    }
}

@Composable
private fun DialogoMateria(estado: EstadoInicio, onElegir: (String?) -> Unit, onCerrar: () -> Unit) {
    AlertDialog(
        onDismissRequest = onCerrar,
        title = { Text("Materia") },
        text = {
            Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                OpcionMateria("Auto (IA)", "La materia la decide el server", estado.seleccion.materiaId == null) { onElegir(null) }
                estado.materias.forEach { m ->
                    val sugerida = estado.sugerencia?.materia?.id == m.id
                    OpcionMateria(m.nombre, if (sugerida) "Sugerida por el calendario" else null, estado.seleccion.materiaId == m.id) {
                        onElegir(m.id)
                    }
                }
                if (estado.materias.isEmpty()) {
                    Text(
                        "No hay materias: el server todavía no respondió.",
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
            }
        },
        confirmButton = { TextButton(onClick = onCerrar) { Text("Cerrar") } },
    )
}

@Composable
internal fun OpcionMateria(nombre: String, detalle: String?, elegida: Boolean, onClick: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(selected = elegida, onClick = onClick)
        Column {
            Text(nombre)
            detalle?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
    }
}

@Composable
private fun DialogoNumero(materia: Materia, actual: Int, onGuardar: suspend (Int) -> String?, onCerrar: () -> Unit) {
    val scope = rememberCoroutineScope()
    var texto by rememberSaveable { mutableStateOf(actual.toString()) }
    var error by remember { mutableStateOf<String?>(null) }
    var enviando by remember { mutableStateOf(false) }
    val numero = texto.trim().toIntOrNull()?.takeIf { it in 1..MAX_NUMERO_CLASE }
    AlertDialog(
        onDismissRequest = { if (!enviando) onCerrar() },
        title = { Text("Número de clase") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    "La próxima clase de ${materia.nombre} va a ser la número…",
                    style = MaterialTheme.typography.bodyMedium,
                )
                OutlinedTextField(
                    value = texto,
                    onValueChange = { nuevo ->
                        texto = nuevo.filter(Char::isDigit).take(3)
                        error = null
                    },
                    singleLine = true,
                    enabled = !enviando,
                    isError = numero == null,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    "Se guarda en el server y vale también para las clases siguientes.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
            }
        },
        confirmButton = {
            TextButton(
                enabled = numero != null && !enviando,
                onClick = {
                    val n = numero ?: return@TextButton
                    enviando = true
                    scope.launch {
                        val problema = onGuardar(n)
                        enviando = false
                        if (problema == null) onCerrar() else error = problema
                    }
                },
            ) {
                if (enviando) {
                    CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(8.dp))
                }
                Text("Guardar")
            }
        },
        dismissButton = { TextButton(onClick = onCerrar, enabled = !enviando) { Text("Cancelar") } },
    )
}

/** Elegir la materia de una clase que el server no pudo clasificar (`esperando_usuario`). */
@Composable
private fun DialogoMateriaDeSesion(
    fila: FilaSesion,
    materias: List<Materia>,
    enviando: Boolean,
    onElegir: suspend (String) -> String?,
    onCerrar: () -> Unit,
) {
    val scope = rememberCoroutineScope()
    var error by remember { mutableStateOf<String?>(null) }
    var elegida by remember { mutableStateOf<String?>(null) }
    AlertDialog(
        onDismissRequest = { if (!enviando) onCerrar() },
        title = { Text("¿De qué materia es?") },
        text = {
            Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                Text(
                    listOfNotNull(fila.fecha, fila.tipoClase?.let(::nombreTipo), fila.duracion).joinToString(" · "),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                materias.forEach { m ->
                    OpcionMateria(
                        m.nombre,
                        if (fila.materiaSugerida == m.id) "Sugerida por el server" else null,
                        elegida == m.id,
                    ) {
                        if (enviando) return@OpcionMateria
                        elegida = m.id
                        error = null
                        scope.launch {
                            val problema = onElegir(m.id)
                            if (problema == null) onCerrar() else error = problema
                        }
                    }
                }
                if (materias.isEmpty()) {
                    Text(
                        "No hay materias: el server todavía no respondió.",
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
                if (enviando) {
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 8.dp)) {
                        CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(8.dp))
                        Text("Mandando al server…", style = MaterialTheme.typography.bodySmall)
                    }
                }
                error?.let {
                    Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 8.dp))
                }
            }
        },
        confirmButton = { TextButton(onClick = onCerrar, enabled = !enviando) { Text("Cerrar") } },
    )
}

@Composable
private fun FilaClase(
    fila: FilaSesion,
    descargando: Boolean,
    onReintentar: () -> Unit,
    onElegirMateria: () -> Unit,
    onAccionWatch: () -> Unit,
    onAdjuntos: () -> Unit = {},
    onReintentarImportacion: () -> Unit = {},
    onDescartarImportacion: () -> Unit = {},
    onAbrirPdf: () -> Unit,
) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(fila.materia, style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Text(
                        listOfNotNull(
                            FilasSesion.textoNumero(fila.numeroClase),
                            fila.fecha,
                            fila.tipoClase?.let(::nombreTipo),
                            fila.duracion,
                            "Importada".takeIf { fila.importada },
                        ).joinToString(" · "),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Text(
                    fila.estado,
                    style = MaterialTheme.typography.labelLarge,
                    color = when (fila.tono) {
                        Tono.ACTIVO -> RojoGrabando
                        Tono.OK -> MaterialTheme.colorScheme.primary
                        Tono.ERROR -> MaterialTheme.colorScheme.error
                        Tono.NORMAL -> MaterialTheme.colorScheme.onSurface
                    },
                )
            }
            fila.progreso?.let { LinearProgressIndicator(progress = { it }, modifier = Modifier.fillMaxWidth()) }
            fila.detalle?.let {
                Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 3, overflow = TextOverflow.Ellipsis)
            }
            if (fila.reintentable) {
                TextButton(onClick = onReintentar) { Text("Reintentar") }
            }
            if (fila.importacionFallida) {
                Row {
                    TextButton(onClick = onReintentarImportacion) { Text("Reintentar") }
                    TextButton(onClick = onDescartarImportacion) { Text("Descartar") }
                }
            }
            if (fila.elegirMateria) {
                Button(onClick = onElegirMateria) { Text("Elegir materia") }
            }
            fila.accionWatch?.let { TextButton(onClick = onAccionWatch) { Text(it) } }
            AdjuntosDeFila(fila, onAdjuntos)
            if (fila.pdf != null) {
                HorizontalDivider(modifier = Modifier.padding(vertical = 4.dp))
                TextButton(onClick = onAbrirPdf, enabled = !descargando) {
                    if (descargando) {
                        CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(8.dp))
                    }
                    Text("Abrir PDF")
                }
            }
        }
    }
}

@Composable
private fun Aviso(texto: String, accion: String, onAccion: () -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(texto, color = MaterialTheme.colorScheme.onSecondaryContainer)
            TextButton(onClick = onAccion, modifier = Modifier.align(Alignment.End)) { Text(accion) }
        }
    }
}

/** Tope del número de clase que se puede escribir con el lápiz. */
private const val MAX_NUMERO_CLASE = 999

fun nombreTipo(tipo: String): String = when (tipo) {
    "teorica" -> "Teórica"
    "practica" -> "Práctica"
    else -> tipo.replaceFirstChar { it.uppercase() }
}
