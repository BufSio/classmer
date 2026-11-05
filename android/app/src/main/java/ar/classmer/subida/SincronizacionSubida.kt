package ar.classmer.subida

import android.content.Context
import android.util.Log
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.datos.Ajustes
import ar.classmer.grabacion.ControlGrabacion
import ar.classmer.grabacion.Recuperacion
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.EstadoSubida
import ar.classmer.modelo.SesionLocal
import ar.classmer.red.ClienteApi
import ar.classmer.red.ErrorApi
import ar.classmer.red.ErrorAutenticacion
import ar.classmer.util.Tiempo
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.IOException

/**
 * Sube todo lo pendiente de todas las sesiones, la más vieja primero. La usan tres caminos:
 *
 * - [TrabajadorSubida] (WorkManager): al terminar, al abrir la app, cuando vuelve la red, y los
 *   reintentos con espera creciente.
 * - El servicio de grabación, cada ~30 s mientras graba y al terminar. Android 16 frena los trabajos
 *   de WorkManager en Doze (pantalla bloqueada), pero un servicio en primer plano sigue teniendo red.
 * - La copia de una importada ([ar.classmer.importacion.TrabajadorImportacion]), que al terminar
 *   sigue con la subida en su primer plano.
 *
 * Dos candados: uno para las clases grabadas y otro para las importadas. Nunca corren dos subidas de
 * las mismas clases a la vez dentro del proceso, y una importada de cientos de MB (minutos subiendo)
 * nunca deja esperando a la clase que se está grabando.
 */
object SincronizacionSubida {
    private const val TAG = "ClassmerSubida"

    enum class Resultado {
        /** No quedó nada que reintentar (puede haber sesiones que siguen grabándose). */
        LISTO,

        /** Falló la red o el server: hay que reintentar más tarde. */
        REINTENTAR,

        /** El server rechazó el token: no sirve reintentar hasta que se cambie en Ajustes. */
        TOKEN_INVALIDO,
    }

    /** El de las grabadas: lo comparten el servicio de grabación y WorkManager. */
    private val candado = Mutex()

    /** El de las importadas: su audio puede tardar minutos en subir, y la clase que se graba no lo espera. */
    private val candadoImportadas = Mutex()

    /**
     * Las grabadas, después las importadas (con su candado) y después los adjuntos.
     *
     * @param conImportadas false en la subida final del servicio de grabación: su primer plano es para la
     *   clase que terminó, no para una importada de cientos de MB (esas las sube WorkManager).
     * @param acompanar corre mientras se suben las importadas, ya con su candado: el primer plano de
     *   [TrabajadorSubida] y el aviso con el porcentaje. Si tira, la pasada se corta y [ejecutar] tira eso.
     */
    suspend fun ejecutar(context: Context, conImportadas: Boolean = true, acompanar: (suspend () -> Unit)? = null): Resultado {
        val app = context.applicationContext
        val grabadas = candado.withLock { sincronizarTodo(app, importadas = false) }
        val clases = if (!conImportadas || grabadas == Resultado.TOKEN_INVALIDO) {
            grabadas
        } else {
            ColaDePasadas.combinar(grabadas, candadoImportadas.withLock { conAcompanante(acompanar) { sincronizarTodo(app, importadas = true) } })
        }
        // Adjuntos (PDF, imágenes): después y fuera de los candados, así una clase nunca espera detrás de un PDF.
        return SincronizacionAdjuntos.despuesDeLasClases(context, clases, esperar = true)
    }

    /**
     * Si ya hay una subida de grabadas corriendo, no espera: devuelve `null`. La usa el servicio mientras
     * graba, así que no sube las importadas (que pueden pesar cientos de MB): esas las sube WorkManager.
     */
    suspend fun intentarSiEstaLibre(context: Context): Resultado? {
        if (!candado.tryLock()) return null
        try {
            return sincronizarTodo(context.applicationContext, importadas = false)
                .also { SincronizacionAdjuntos.despuesDeLasClases(context, it, esperar = false) } // adjuntos: sin esperarlos
        } finally {
            candado.unlock()
        }
    }

    private suspend fun <T> conAcompanante(acompanar: (suspend () -> Unit)?, bloque: suspend () -> T): T {
        if (acompanar == null) return bloque()
        return coroutineScope {
            val a = launch { acompanar() }
            try {
                bloque()
            } finally {
                a.cancel()
            }
        }
    }

    /** Una pasada por las grabadas o, con [importadas], por las importadas (cada una con su candado). */
    private suspend fun sincronizarTodo(context: Context, importadas: Boolean): Resultado {
        val ajustes = Ajustes(context)
        val almacen = AlmacenSesiones.de(context)
        // Sesiones que quedaron "grabando" sin estar grabándose (no se pudo guardar el cierre).
        if (!importadas) Recuperacion.cerrarHuerfanas(almacen, ControlGrabacion.estado.value.sesion?.id, inactividadMs = 2 * 60_000L)
        if (!ajustes.configurado) return Resultado.LISTO

        val token = ajustes.token
        val api = try {
            ClienteApi(ajustes.urlServidor, token)
        } catch (e: IllegalArgumentException) {
            for (s in almacen.listar()) {
                if (s.importada != importadas) continue
                if (!almacen.leerSubida(s.id).finalizada) anotar(almacen, s.id, "La URL del server no es válida: revisala en Ajustes")
            }
            return Resultado.LISTO
        }
        val subidor = Subidor(almacen, api)
        val activa = ControlGrabacion.estado.value.sesion?.id
        var reintentar = false
        var pasadas = 0
        do {
            // El pedido de otra pasada lo atienden las grabadas: una importada nueva la patea su copia, y
            // esa patada encola otro trabajo si éste está corriendo.
            if (!importadas) PlanificadorSubida.pendiente.set(false)
            pasadas++
            for (sesion in ordenDeSubida(almacen.listar())) {
                val subida = almacen.leerSubida(sesion.id)
                if (subida.finalizada || sesion.importada != importadas) continue
                if (todaviaNo(sesion, subida, almacen.archivoAudio(sesion).length())) continue
                if (subida.errorPermanente != null) {
                    // El server la rechazó: no se reintenta en loop, sólo cuando se toca "Reintentar".
                    val pedido = almacen.archivoPedidoReintento(sesion.id)
                    if (!pedido.exists()) continue
                    almacen.guardarSubida(sesion.id, subida.copy(errorPermanente = null))
                    pedido.delete()
                }
                try {
                    val r = subidor.sincronizar(sesion.id)
                    // Una clase terminada que todavía no se pudo finalizar (o una "grabando" que no es la
                    // grabación actual) necesita otro intento más tarde.
                    if (r == Subidor.Resultado.ESPERANDO || (r == Subidor.Resultado.PARCIAL && sesion.id != activa)) {
                        reintentar = true
                    }
                    anotar(almacen, sesion.id, error = null)
                    if (ajustes.errorAutenticacion != null) ajustes.errorAutenticacion = null
                } catch (e: CancellationException) {
                    throw e
                } catch (e: ErrorAutenticacion) {
                    // Si mientras tanto se cambió el token en Ajustes, este error ya no corresponde.
                    if (ajustes.token == token) ajustes.errorAutenticacion = e.message
                    anotar(almacen, sesion.id, error = e.message)
                    return Resultado.TOKEN_INVALIDO
                } catch (e: IOException) {
                    // Sin red o el server no está: no tiene sentido probar con las otras sesiones.
                    anotar(almacen, sesion.id, error = "Sin conexión con el server (${e.message ?: e.javaClass.simpleName})")
                    reintentar = true
                    break
                } catch (e: ErrorApi) {
                    if (e.permanente) {
                        val estado = almacen.leerSubida(sesion.id)
                        almacen.guardarSubida(sesion.id, estado.copy(errorPermanente = e.message))
                        anotar(almacen, sesion.id, error = e.message)
                    } else {
                        anotar(almacen, sesion.id, error = e.message)
                        reintentar = true
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "Fallo subiendo ${sesion.id}", e)
                    anotar(almacen, sesion.id, error = e.message ?: e.javaClass.simpleName)
                    reintentar = true
                }
            }
        } while (!importadas && !reintentar && PlanificadorSubida.pendiente.get() && pasadas < 5)

        return if (reintentar) Resultado.REINTENTAR else Resultado.LISTO
    }

    /**
     * La más vieja primero, las importadas al final. Las importadas suben aparte, en su pasada y con su
     * candado ([ejecutar]); este orden también es el del aviso de [AvisoSubida].
     */
    fun ordenDeSubida(sesiones: List<SesionLocal>): List<SesionLocal> = sesiones.asReversed().sortedBy { it.importada }

    /** Una sesión que todavía no va al server (no hace falta reintentarla: la patea quien la destraba). */
    fun todaviaNo(sesion: SesionLocal, subida: EstadoSubida, largoAudio: Long): Boolean = when {
        // Una importada que se está copiando al celu: sube recién cuando la copia termina.
        sesion.estadoLocal == EstadoLocal.IMPORTANDO -> true
        // Una clase del reloj que todavía no tiene audio (el reloj no empezó) no va al server: si el
        // reloj nunca empieza, no queda ahí una clase fantasma que además corra la numeración.
        sesion.delReloj && !sesion.terminada && !subida.sesionCreada && largoAudio == 0L -> true
        else -> false
    }

    private fun anotar(almacen: AlmacenSesiones, id: String, error: String?) {
        val estado = almacen.leerSubida(id)
        val ahora = Tiempo.ahoraIso()
        almacen.guardarSubida(
            id,
            estado.copy(ultimoError = error, ultimoIntento = ahora, ultimoOk = if (error == null) ahora else estado.ultimoOk),
        )
        if (error != null) Log.i(TAG, "Sesión $id: $error")
    }
}
