package ar.classmer.subida

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.watch.SincronizacionWatch
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** El trabajo de WorkManager: corre una [SincronizacionSubida] y pide reintento si falló. */
class TrabajadorSubida(contexto: Context, params: WorkerParameters) : CoroutineWorker(contexto, params) {

    override suspend fun doWork(): Result {
        val primerPlano = PrimerPlano()
        // Lo que quedó en el reloj (por ejemplo, si se alejó al terminar la clase) se trae antes de subir.
        runCatching { SincronizacionWatch.ponerseAlDia(applicationContext) }
        val r = try {
            subir(primerPlano)
        } catch (e: TopeSinPrimerPlano) {
            // Se terminó la ventana del trabajo: lo que falta va en el próximo intento.
            return Result.retry()
        }
        // Lo que el server acaba de aceptar ya se puede borrar del reloj.
        runCatching { SincronizacionWatch.ponerseAlDia(applicationContext) }
        return r
    }

    /** Si Android negó el primer plano, corta en el tope de [PrimerPlano] (tira [TopeSinPrimerPlano]). */
    private suspend fun subir(primerPlano: PrimerPlano): Result = coroutineScope {
        val tope = launch {
            while (primerPlano.dentroDelTope) delay(2_000)
            throw TopeSinPrimerPlano()
        }
        try {
            // Con una importada pendiente, en primer plano mientras sube (ver AvisoSubida).
            when (SincronizacionSubida.ejecutar(applicationContext, acompanar = { acompanarImportadas(primerPlano) })) {
                SincronizacionSubida.Resultado.LISTO -> Result.success()
                SincronizacionSubida.Resultado.REINTENTAR -> Result.retry()
                SincronizacionSubida.Resultado.TOKEN_INVALIDO -> Result.failure()
            }
        } finally {
            tope.cancel()
        }
    }

    /**
     * Mientras se suben las importadas: pasa a primer plano si hay una que subir y actualiza el porcentaje
     * cada 2 s. Si Android no deja (desde segundo plano, Android 12+, sin la exención de batería), la
     * subida sigue sin notificación, como un trabajo común.
     */
    private suspend fun acompanarImportadas(primerPlano: PrimerPlano) {
        val almacen = AlmacenSesiones.de(applicationContext)
        while (!primerPlano.negado) {
            val p = withContext(Dispatchers.IO) { runCatching { AvisoSubida.pendiente(almacen) }.getOrNull() }
            when {
                // No hay ninguna que subir (o ya subieron): la pasada va como siempre.
                p == null -> Unit
                !primerPlano.activo -> primerPlano.pedir { setForeground(AvisoSubida.info(applicationContext, p)) }
                else -> AvisoSubida.notificar(applicationContext, p)
            }
            delay(2_000)
        }
    }
}
