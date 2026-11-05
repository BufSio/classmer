package ar.classmer.grabacion

import android.content.Context
import android.content.Intent
import ar.classmer.datos.Ajustes
import androidx.core.content.ContextCompat
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.SesionLocal
import ar.classmer.watch.Enganche
import ar.classmer.watch.EstadoWatchUi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Lo que la pantalla necesita saber de la grabación en curso. */
data class EstadoGrabacion(
    /** `null` = no se está grabando. */
    val sesion: SesionLocal? = null,
    val pausada: Boolean = false,
    val fotos: Int = 0,
    val notas: Int = 0,
    val importantes: Int = 0,
    val noEntendi: Int = 0,
    /** Otra app (una llamada, por ejemplo) tomó el micrófono: se está grabando silencio. */
    val microfonoSilenciado: Boolean = false,
    /** Último aviso para mostrar (por ejemplo, "⭐ Importante en 12:34"). */
    val aviso: Aviso? = null,
    /** La grabación no pudo arrancar o se cortó. */
    val error: String? = null,
    /** Se está preguntando "¿Sigue la clase?": si nadie responde, termina sola a esta hora (ms desde 1970). */
    val preguntaCorteHastaMs: Long? = null,
    /** La pregunta se mostró como notificación; si no, la grabación no termina sola. */
    val preguntaCorteTerminaSola: Boolean = true,
    /** Graba el reloj (Galaxy Watch): cómo viene su audio. `null` = graba el celu. */
    val watch: EstadoWatchUi? = null,
    /** Ambos: una línea sobre la pista del celu (`null` si no graban los dos). */
    val celu: String? = null,
) {
    val grabando: Boolean get() = sesion != null
}

data class Aviso(val texto: String, val n: Long)

/** Una nota a medio escribir: sobrevive a ir y volver de Inicio mientras se graba. [pista] es la de su minuto. */
data class BorradorNota(val sesionId: String, val texto: String, val offsetMs: Long, val pista: String? = null)

/**
 * Punto de entrada a la grabación para la UI y la notificación. El estado vive en
 * [ServicioGrabacion]; acá sólo se publica y se mandan las órdenes como intents.
 */
object ControlGrabacion {
    internal val estadoMutable = MutableStateFlow(EstadoGrabacion())
    val estado: StateFlow<EstadoGrabacion> = estadoMutable.asStateFlow()

    @Volatile
    internal var reloj: RelojGrabacion? = null

    /**
     * Con Ambos, [ar.classmer.modelo.Pista.CELU] mientras [reloj] es el de la pista del celu (el reloj
     * todavía no empezó, o se cortó): los minutos se cuentan en ese audio. `null` = el del audio principal.
     */
    @Volatile
    internal var pistaDelReloj: String? = null

    @Volatile
    var borradorNota: BorradorNota? = null

    /** Tiempo de audio grabado hasta ahora (sin contar las pausas). */
    fun offsetActualMs(): Long = reloj?.offsetMs() ?: 0

    /** En qué audio se cuenta [offsetActualMs] (ver [pistaDelReloj]): va con el minuto de una foto o una nota. */
    fun pistaActual(): String? = pistaDelReloj

    fun limpiarError() {
        estadoMutable.value = estadoMutable.value.copy(error = null)
    }

    fun iniciar(
        context: Context,
        materiaId: String?,
        nombreMateria: String?,
        origenMateria: String,
        tipoClase: String,
        eventoCalendario: String?,
        numeroClase: Int?,
        finEventoMs: Long?,
        fuenteAudio: String = FuenteAudio.TELEFONO,
    ) {
        val intent = Intent(context, ServicioGrabacion::class.java).setAction(ServicioGrabacion.ACCION_INICIAR)
            .putExtra(ServicioGrabacion.EXTRA_MATERIA_ID, materiaId)
            .putExtra(ServicioGrabacion.EXTRA_NOMBRE_MATERIA, nombreMateria)
            .putExtra(ServicioGrabacion.EXTRA_ORIGEN, origenMateria)
            .putExtra(ServicioGrabacion.EXTRA_TIPO_CLASE, tipoClase)
            .putExtra(ServicioGrabacion.EXTRA_EVENTO_CALENDARIO, eventoCalendario)
            .putExtra(ServicioGrabacion.EXTRA_FUENTE, fuenteAudio)
        numeroClase?.let { intent.putExtra(ServicioGrabacion.EXTRA_NUMERO_CLASE, it) }
        finEventoMs?.let { intent.putExtra(ServicioGrabacion.EXTRA_FIN_EVENTO, it) }
        ContextCompat.startForegroundService(context, intent)
    }

    fun marcar(context: Context, marca: String) =
        enviar(context, ServicioGrabacion.ACCION_MARCA) { putExtra(ServicioGrabacion.EXTRA_MARCA, marca) }

    fun nota(context: Context, texto: String, offsetMs: Long, pista: String?) =
        enviar(context, ServicioGrabacion.ACCION_NOTA) {
            putExtra(ServicioGrabacion.EXTRA_TEXTO, texto)
            putExtra(ServicioGrabacion.EXTRA_OFFSET, offsetMs)
            putExtra(ServicioGrabacion.EXTRA_PISTA, pista.orEmpty())
        }

    fun foto(context: Context, eventoId: String, offsetMs: Long, archivoRelativo: String, pista: String?) =
        enviar(context, ServicioGrabacion.ACCION_FOTO) {
            putExtra(ServicioGrabacion.EXTRA_EVENTO_ID, eventoId)
            putExtra(ServicioGrabacion.EXTRA_OFFSET, offsetMs)
            putExtra(ServicioGrabacion.EXTRA_ARCHIVO, archivoRelativo)
            putExtra(ServicioGrabacion.EXTRA_PISTA, pista.orEmpty())
        }

    fun pausar(context: Context) = enviar(context, ServicioGrabacion.ACCION_PAUSAR)
    fun reanudar(context: Context) = enviar(context, ServicioGrabacion.ACCION_REANUDAR)
    fun terminar(context: Context) = enviar(context, ServicioGrabacion.ACCION_TERMINAR)

    /** Respuesta "Seguir" a "¿Sigue la clase?". */
    fun seguirClase(context: Context) = enviar(context, ServicioGrabacion.ACCION_SEGUIR)

    /** El reloj no empezó a grabar: la clase se graba con el celu. */
    fun usarTelefono(context: Context) = enviar(context, ServicioGrabacion.ACCION_USAR_TELEFONO)

    /**
     * Seguir en el celu una clase que está grabando el reloj (ver [ar.classmer.watch.SincronizacionWatch]).
     * Con [conCelu] (Grabar con Ambos, con la app a la vista), el celu graba también su pista.
     */
    fun enganchar(context: Context, sesionId: String, conCelu: Boolean = false) = Enganche.pedir(context, sesionId, conCelu)

    /**
     * Al abrir la app con Ambos elegido: si la clase que se sigue es del reloj y todavía no tiene la pista del
     * celu (se enganchó en segundo plano, sin el micrófono), se le pide sumarla ahora que la app está a la vista.
     */
    fun sumarPistaCeluSiFalta(context: Context) {
        val s = estado.value.sesion ?: return
        if (!s.delReloj || s.pistaCelu || estado.value.watch == null) return
        if (Ajustes(context).fuenteAudio != FuenteAudio.AMBOS) return
        enganchar(context, s.id, conCelu = true)
    }

    private fun enviar(context: Context, accion: String, extras: Intent.() -> Unit = {}) {
        if (!estado.value.grabando) return
        val intent = Intent(context, ServicioGrabacion::class.java).setAction(accion).apply(extras)
        context.startService(intent)
    }
}
