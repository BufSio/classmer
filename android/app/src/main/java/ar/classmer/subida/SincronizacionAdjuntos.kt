package ar.classmer.subida

import android.content.Context
import android.util.Log
import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.red.ClienteApi
import ar.classmer.red.ErrorAutenticacion
import ar.classmer.subida.SincronizacionSubida.Resultado
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Las pasadas de adjuntos ([SubidorAdjuntos]), aparte de las de las clases: [SincronizacionSubida]
 * sube primero las clases (con su candado) y después pide una pasada de adjuntos acá. Así la subida
 * de una clase, y la subida final al terminar, nunca esperan detrás de un PDF de 100 MB.
 *
 * Las pasadas corren de a una en un alcance propio, no en el de quien las pide: si se cancela quien
 * espera (el tope de la subida final, WorkManager que corta un trabajo a los 10 min), el archivo se
 * sigue mandando mientras el proceso viva, y el próximo que pida se suma a esa misma pasada.
 */
object SincronizacionAdjuntos {
    private const val TAG = "ClassmerSubida"

    private val cola = ColaDePasadas(CoroutineScope(SupervisorJob() + Dispatchers.IO))

    private val subiendoMutable = MutableStateFlow<String?>(null)

    /** El adjunto cuyo archivo se está mandando ahora, si hay uno (para mostrar "Subiendo…"). */
    val subiendo: StateFlow<String?> = subiendoMutable.asStateFlow()

    /**
     * Después de subir las clases: pide una pasada de adjuntos. Con [esperar] (WorkManager), devuelve
     * el resultado de las dos cosas juntas; si no (el servicio mientras graba), no espera.
     */
    suspend fun despuesDeLasClases(context: Context, clases: Resultado, esperar: Boolean): Resultado {
        val app = context.applicationContext
        return cola.despuesDe(clases, esperar) { pasada(app) }
    }

    private suspend fun pasada(context: Context): Resultado {
        val ajustes = Ajustes(context)
        if (!ajustes.configurado) return Resultado.LISTO
        val token = ajustes.token
        val api = try {
            ClienteApi(ajustes.urlServidor, token)
        } catch (e: IllegalArgumentException) {
            return Resultado.LISTO
        }
        val subidor = SubidorAdjuntos(
            AlmacenAdjuntos.de(context), AlmacenSesiones.de(context), api,
            servidor = ajustes.urlServidor,
            alSubir = { subiendoMutable.value = it },
        )
        // Si mientras tanto se cambió el token en Ajustes, este error ya no corresponde.
        return pasada(subidor) { if (ajustes.token == token) ajustes.errorAutenticacion = it }
    }

    /** Una pasada, con los errores traducidos al resultado de la subida (sin Context, para los tests). */
    internal suspend fun pasada(subidor: SubidorAdjuntos, alRechazarToken: (String?) -> Unit): Resultado = try {
        when (subidor.sincronizarTodo()) {
            SubidorAdjuntos.Resultado.LISTO -> Resultado.LISTO
            SubidorAdjuntos.Resultado.REINTENTAR -> Resultado.REINTENTAR
        }
    } catch (e: CancellationException) {
        throw e
    } catch (e: ErrorAutenticacion) {
        alRechazarToken(e.message)
        Resultado.TOKEN_INVALIDO
    } catch (e: Exception) {
        Log.w(TAG, "Fallo subiendo adjuntos", e)
        Resultado.REINTENTAR
    }
}

/**
 * Corre de a una pasada por vez, en [alcance]. Un pedido que llega con una en curso no arranca otra:
 * hace que la que corre dé una vuelta más al terminar (así no se pierde un adjunto que llegó en el
 * medio) y espera ese resultado.
 */
internal class ColaDePasadas(private val alcance: CoroutineScope, private val maxVueltas: Int = 5) {
    private val lock = Any()
    private var enCurso: CompletableDeferred<Resultado>? = null
    private var otraVuelta = false

    fun pedir(pasada: suspend () -> Resultado): Deferred<Resultado> {
        synchronized(lock) {
            enCurso?.let {
                otraVuelta = true
                return it
            }
            val nueva = CompletableDeferred<Resultado>()
            enCurso = nueva
            alcance.launch { correr(nueva, pasada) }
            return nueva
        }
    }

    /** Las clases primero: con el token rechazado, los adjuntos ni se intentan. */
    suspend fun despuesDe(clases: Resultado, esperar: Boolean, pasada: suspend () -> Resultado): Resultado {
        if (clases == Resultado.TOKEN_INVALIDO) return clases
        val adjuntos = pedir(pasada)
        if (!esperar) return clases
        return combinar(clases, adjuntos.await())
    }

    private suspend fun correr(resultado: CompletableDeferred<Resultado>, pasada: suspend () -> Resultado) {
        var vueltas = 0
        try {
            while (true) {
                synchronized(lock) { otraVuelta = false }
                val r = pasada()
                vueltas++
                synchronized(lock) {
                    if (!otraVuelta || r != Resultado.LISTO || vueltas >= maxVueltas) {
                        enCurso = null
                        // Quedó un pedido sin atender (demasiadas vueltas seguidas): que se reintente.
                        resultado.complete(if (otraVuelta && r == Resultado.LISTO) Resultado.REINTENTAR else r)
                        return
                    }
                }
            }
        } catch (e: Throwable) {
            synchronized(lock) { enCurso = null }
            resultado.completeExceptionally(e)
            if (e is CancellationException) throw e
        }
    }

    companion object {
        /** El peor de los dos: el token rechazado manda, después reintentar. */
        fun combinar(a: Resultado, b: Resultado): Resultado = when {
            a == Resultado.TOKEN_INVALIDO || b == Resultado.TOKEN_INVALIDO -> Resultado.TOKEN_INVALIDO
            a == Resultado.REINTENTAR || b == Resultado.REINTENTAR -> Resultado.REINTENTAR
            else -> Resultado.LISTO
        }
    }
}
