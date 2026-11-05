package ar.classmer.ui

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

val RojoGrabando = Color(0xFFD32F2F)

@Composable
fun TemaClassmer(contenido: @Composable () -> Unit) {
    val oscuro = isSystemInDarkTheme()
    val colores = when {
        Build.VERSION.SDK_INT >= 31 -> {
            val ctx = LocalContext.current
            if (oscuro) dynamicDarkColorScheme(ctx) else dynamicLightColorScheme(ctx)
        }
        oscuro -> darkColorScheme()
        else -> lightColorScheme()
    }
    MaterialTheme(colorScheme = colores, content = contenido)
}
