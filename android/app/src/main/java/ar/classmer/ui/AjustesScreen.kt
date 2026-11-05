package ar.classmer.ui

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import ar.classmer.BuildConfig
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.red.ClienteApi
import ar.classmer.subida.PlanificadorSubida
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AjustesScreen(onVolver: () -> Unit) {
    val context = LocalContext.current
    val ajustes = remember { Ajustes(context) }
    val scope = rememberCoroutineScope()

    var url by rememberSaveable { mutableStateOf(ajustes.urlServidor) }
    var token by rememberSaveable { mutableStateOf(ajustes.token) }
    var dispositivo by rememberSaveable { mutableStateOf(ajustes.dispositivo) }
    var verToken by rememberSaveable { mutableStateOf(false) }
    var resultado by remember { mutableStateOf<String?>(null) }
    var probando by remember { mutableStateOf(false) }

    // Se vuelve a mirar al volver de la pantalla de Android.
    var sinRestriccionBateria by remember { mutableStateOf(true) }
    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(lifecycleOwner) {
        lifecycleOwner.repeatOnLifecycle(Lifecycle.State.RESUMED) {
            sinRestriccionBateria = context.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(context.packageName)
        }
    }

    /** Devuelve false (y no guarda nada) si la URL no se puede usar. */
    fun guardar(): Boolean {
        val normalizada = Ajustes.normalizarUrl(url)
        if (normalizada.isNotEmpty() && !Ajustes.urlValida(normalizada)) return false
        ajustes.urlServidor = url
        ajustes.token = token
        ajustes.dispositivo = dispositivo
        ajustes.errorAutenticacion = null
        url = ajustes.urlServidor
        PlanificadorSubida.patear(context)
        return true
    }

    BackHandler(onBack = onVolver)

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Ajustes") },
                navigationIcon = {
                    IconButton(onClick = onVolver) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Volver") }
                },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(padding).imePadding().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Server", style = MaterialTheme.typography.titleMedium)
            OutlinedTextField(
                value = url,
                onValueChange = { url = it },
                label = { Text("URL del server") },
                placeholder = { Text("http://192.168.1.10:8000") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                label = { Text("Token del dispositivo") },
                singleLine = true,
                visualTransformation = if (verToken) VisualTransformation.None else PasswordVisualTransformation(),
                trailingIcon = { TextButton(onClick = { verToken = !verToken }) { Text(if (verToken) "Ocultar" else "Ver") } },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = dispositivo,
                onValueChange = { dispositivo = it },
                label = { Text("Nombre de este celu") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(onClick = {
                    resultado = if (guardar()) "Guardado." else "✗ La URL no es válida. Ejemplo: http://192.168.1.10:8000"
                }) { Text("Guardar") }
                OutlinedButton(
                    enabled = !probando && url.isNotBlank(),
                    onClick = {
                        probando = true
                        resultado = "Probando…"
                        scope.launch {
                            resultado = probarConexion(Ajustes.normalizarUrl(url), token)
                            probando = false
                        }
                    },
                ) { Text("Probar conexión") }
            }
            resultado?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
            Text(
                "Con el server en la PC: http://<IP de la PC en la LAN>:<puerto>. Desde afuera de casa, la misma IP con el WireGuard del router prendido.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            HorizontalDivider()
            Text("Batería", style = MaterialTheme.typography.titleMedium)
            if (sinRestriccionBateria) {
                Text("✓ Classmer puede funcionar sin restricciones de batería.")
            } else {
                Card {
                    Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(
                            "Para que la subida siga con la pantalla bloqueada y después de la clase, permití que Classmer " +
                                "funcione sin restricciones de batería.",
                        )
                        Button(onClick = { pedirSinRestriccion(context) }) { Text("Permitir") }
                    }
                }
            }
            TextButton(onClick = { abrirAppsQueNuncaDuermen(context) }) { Text("Samsung: agregar a \"Apps que nunca se suspenden\"") }

            HorizontalDivider()
            Text("Datos", style = MaterialTheme.typography.titleMedium)
            Text(
                "Las clases quedan en el celu y no se borran:\n${AlmacenSesiones.de(context).raiz.absolutePath}",
                style = MaterialTheme.typography.bodySmall,
            )
            Text("Classmer ${BuildConfig.VERSION_NAME}", style = MaterialTheme.typography.bodySmall)
        }
    }
}

private suspend fun probarConexion(url: String, token: String): String = withContext(Dispatchers.IO) {
    if (!Ajustes.urlValida(url)) return@withContext "✗ La URL no es válida. Ejemplo: http://192.168.1.10:8000"
    try {
        val api = ClienteApi(url, token)
        api.health()
        val materias = api.materias()
        "✓ Conectado. El server tiene ${materias.size} materias."
    } catch (e: Exception) {
        val m = e.message ?: e.javaClass.simpleName
        if (m.contains("CLEARTEXT", ignoreCase = true)) {
            "✗ Android bloquea HTTP sin TLS a esa dirección. Agregala en android/app/src/main/res/xml/network_security_config.xml y reinstalá la app."
        } else {
            "✗ $m"
        }
    }
}

@SuppressLint("BatteryLife")
private fun pedirSinRestriccion(context: android.content.Context) {
    val pedido = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${context.packageName}"))
    try {
        context.startActivity(pedido)
    } catch (e: ActivityNotFoundException) {
        context.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
    }
}

/** Pantalla de One UI con las apps que nunca se suspenden (si no existe, abre los ajustes de la app). */
private fun abrirAppsQueNuncaDuermen(context: android.content.Context) {
    val samsung = Intent("com.samsung.android.sm.ACTION_OPEN_CHECKABLE_LISTACTIVITY")
        .setPackage("com.samsung.android.lool")
        .putExtra("activity_type", 2)
    try {
        context.startActivity(samsung)
    } catch (e: Exception) {
        context.startActivity(
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}")),
        )
    }
}
