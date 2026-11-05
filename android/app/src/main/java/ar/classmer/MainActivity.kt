package ar.classmer

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.subida.PlanificadorSubida
import ar.classmer.ui.AjustesScreen
import ar.classmer.ui.GrabandoScreen
import ar.classmer.ui.ImportarScreen
import ar.classmer.ui.InicioScreen
import ar.classmer.ui.InicioViewModel
import ar.classmer.ui.TemaClassmer
import ar.classmer.watch.SincronizacionWatch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent { TemaClassmer { AppClassmer() } }
    }

    override fun onStart() {
        super.onStart()
        PlanificadorSubida.patear(this)
        // Con la app a la vista, Android deja enganchar el servicio a una clase que esté grabando el reloj.
        SincronizacionWatch.ponerseAlDiaEnFondo(this)
        // Con Ambos: una clase del reloj que el celu sigue sin su pista (se enganchó en segundo plano) la suma ahora.
        ControlGrabacion.sumarPistaCeluSiFalta(this)
    }
}

private enum class Pantalla { INICIO, AJUSTES, IMPORTAR }

@Composable
private fun AppClassmer() {
    val grabacion by ControlGrabacion.estado.collectAsStateWithLifecycle()
    var pantalla by rememberSaveable { mutableStateOf(Pantalla.INICIO) }
    var ocultarGrabacion by rememberSaveable { mutableStateOf(false) }
    // El mismo de siempre (vive con la actividad): al importar una clase, corre el número de su materia.
    val inicio: InicioViewModel = viewModel()

    // Cuando arranca una grabación nueva, se muestra la pantalla de grabación.
    LaunchedEffect(grabacion.sesion?.id) {
        if (grabacion.sesion != null) ocultarGrabacion = false
    }

    when {
        pantalla == Pantalla.AJUSTES -> AjustesScreen(onVolver = { pantalla = Pantalla.INICIO })
        // Si mientras tanto empieza una grabación (por ejemplo, desde el reloj), lo elegido no se pierde:
        // al volver se ve la pantalla de grabación.
        pantalla == Pantalla.IMPORTAR -> ImportarScreen(
            onVolver = { pantalla = Pantalla.INICIO },
            onImportada = {
                inicio.alImportar(it)
                pantalla = Pantalla.INICIO
            },
        )
        grabacion.grabando && !ocultarGrabacion -> GrabandoScreen(onIrAInicio = { ocultarGrabacion = true })
        else -> InicioScreen(
            onAjustes = { pantalla = Pantalla.AJUSTES },
            onVerGrabacion = { ocultarGrabacion = false },
            onImportar = { pantalla = Pantalla.IMPORTAR },
            vm = inicio,
        )
    }
}
