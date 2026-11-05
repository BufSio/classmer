package ar.classmer.subida

import android.util.Log
import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.EstadoSubidaAdjuntos
import ar.classmer.modelo.SoporteAdjuntos
import ar.classmer.modelo.SubidaAdjunto
import ar.classmer.red.ClienteApi
import ar.classmer.red.ErrorApi
import ar.classmer.red.ErrorAutenticacion
import ar.classmer.red.ResultadoAdjunto
import ar.classmer.util.Tiempo
import ar.classmer.watch.Ambos
import java.io.File
import java.io.IOException

/**
 * Sube los adjuntos de las clases (`POST /api/sesiones/{id}/adjuntos`), aparte de la sesión: también
 * los de clases ya finalizadas o que sólo existen en el server. Cada adjunto se manda una vez que la
 * clase existe en el server; el id lo hace idempotente si se perdió la respuesta.
 *
 * Antes de mandar un archivo (hasta 100 MB) se pregunta con un pedido de unos bytes, sin el archivo
 * ([ClienteApi.consultarAdjunto]): así nunca se manda entero a un server que no lo va a guardar.
 * - 422 ("falta el archivo"): el server lo acepta y no lo tiene → se manda el archivo.
 * - 200: ya lo tenía (se había perdido la respuesta) → subido, sin mandarlo de nuevo.
 * - 404 "sesión no encontrada": se reintenta con espera creciente; pasados unos intentos, la clase se
 *   da por perdida y sólo se pregunta cada tanto (o con "Reintentar").
 * - 404 "Not Found" o 405 (server viejo): queda guardado en `adjuntos/soporte.json` (vale para todos)
 *   y se vuelve a preguntar cada [ESPERA_SIN_SOPORTE_MS], sin pedirle reintentos a WorkManager.
 *
 * Y al mandar el archivo:
 * - 200/201: subido, no se vuelve a mandar.
 * - Sin red, 5xx, 408, 429: se reintenta, pero ese archivo espera cada vez más antes de volver a
 *   mandarse (el servicio de grabación pide una pasada cada ~30 s).
 * - 409, 413, 415, 422…: rechazado con el motivo; sólo se vuelve a mandar con "Reintentar".
 * - 401/403: [ErrorAutenticacion], como el resto de la subida.
 *
 * Sólo esta clase escribe `adjuntos/<sesion>/subida.json` y `adjuntos/soporte.json`.
 *
 * @param servidor la URL del server, para saber si `soporte.json` habla de éste.
 * @param alSubir avisa qué adjunto se está mandando (`null` al terminar), para mostrar "Subiendo…".
 */
class SubidorAdjuntos(
    private val adjuntos: AlmacenAdjuntos,
    private val sesiones: AlmacenSesiones,
    private val api: ClienteApi,
    private val servidor: String = "",
    private val reloj: () -> Long = System::currentTimeMillis,
    private val alSubir: (String?) -> Unit = {},
) {
    enum class Resultado {
        /** No queda nada que reintentar solo (puede haber adjuntos esperando la clase, rechazados o sin soporte). */
        LISTO,

        /** Falló la red o el server, o la clase todavía no aparece: hay que reintentar más tarde. */
        REINTENTAR,
    }

    private enum class Paso { LISTO, REINTENTAR, CORTAR, SIN_SOPORTE }

    /** Lo que falta subir de una clase. [forzados]: los que se pidieron con "Reintentar" (no esperan). */
    private class Pendientes(val sesionId: String, val lista: List<AdjuntoLocal>, val forzados: Set<String>)

    /** Lo que dice `soporte.json` (tal cual está en el disco). */
    private var soporteGuardado = SoporteAdjuntos()

    /** Una pasada por todas las clases con adjuntos, la más vieja primero. */
    suspend fun sincronizarTodo(): Resultado {
        val porClase = adjuntos.sesiones().mapNotNull(::preparar)
        if (porClase.isEmpty()) return Resultado.LISTO

        // Un server sin actualizar: se le vuelve a preguntar cada tanto, no en cada pasada. Si se
        // cambió de server en Ajustes, o se tocó "Reintentar", se pregunta ya.
        soporteGuardado = adjuntos.leerSoporte()
        val soporte = soporteGuardado.takeIf { it.servidor == servidor } ?: SoporteAdjuntos()
        if (soporte.sinSoporte && porClase.none { it.forzados.isNotEmpty() } &&
            esperando(soporte.ultimoSondeoMs?.plus(ESPERA_SIN_SOPORTE_MS))
        ) {
            return Resultado.LISTO
        }

        var reintentar = false
        for (p in porClase) {
            when (sincronizarSesion(p)) {
                Paso.LISTO -> Unit
                Paso.REINTENTAR -> reintentar = true
                // Sin red o el server no puede: las otras clases darían lo mismo.
                Paso.CORTAR -> return Resultado.REINTENTAR
                // Un server sin adjuntos: tampoco tiene sentido seguir (y no se pide reintento).
                Paso.SIN_SOPORTE -> return if (reintentar) Resultado.REINTENTAR else Resultado.LISTO
            }
        }
        return if (reintentar) Resultado.REINTENTAR else Resultado.LISTO
    }

    /**
     * Consume los pedidos de la clase ("Reintentar" de la hoja, reenvío porque el server volvió a
     * crear la clase) y devuelve lo que falta subir, o `null` si no falta nada.
     */
    private fun preparar(sesionId: String): Pendientes? {
        val lista = adjuntos.listar(sesionId)
        if (lista.isEmpty()) return null
        val antes = adjuntos.leerSubida(sesionId)
        var estado = antes

        // El server había perdido la clase y Subidor la volvió a crear: lo confirmado se vuelve a mandar.
        val reenvio = archivoPedidoReenvio(sesiones, sesionId)
        val hayReenvio = reenvio.exists()
        if (hayReenvio) {
            estado = estado.copy(
                adjuntos = estado.adjuntos.mapValues { (_, s) -> s.copy(subido = false, fallos = 0, esperarHastaMs = null) },
                noEncontrada = 0, esperarHastaMs = null, ultimoError = null,
            )
        }

        // "Reintentar": se borran el rechazo y las esperas, y se prueba ya.
        val pedidos = lista.map { adjuntos.archivoPedidoReintento(sesionId, it.id) to it.id }.filter { it.first.exists() }
        val forzados = mutableSetOf<String>()
        for ((_, id) in pedidos) {
            val s = estado.de(id)
            if (s.subido) continue
            estado = estado.con(id, s.copy(rechazo = null, esperarHastaMs = null)).copy(esperarHastaMs = null)
            forzados += id
        }

        if (estado != antes) adjuntos.guardarSubida(sesionId, estado)
        // Los pedidos se borran después de guardar: si la app se cierra en el medio, se repiten.
        if (hayReenvio) reenvio.delete()
        pedidos.forEach { it.first.delete() }

        val pendientes = lista.filter { estado.de(it.id).let { s -> !s.subido && s.rechazo == null } }
        return if (pendientes.isEmpty()) null else Pendientes(sesionId, pendientes, forzados)
    }

    private suspend fun sincronizarSesion(p: Pendientes): Paso {
        val sesionId = p.sesionId
        // Hasta que la clase llegue al server no hay a dónde subirlos; la subida de la clase ya reintenta.
        if (!claseEnServidor(sesiones, sesionId)) return Paso.LISTO

        var estado = adjuntos.leerSubida(sesionId)
        fun guardar(nuevo: EstadoSubidaAdjuntos) {
            estado = nuevo
            adjuntos.guardarSubida(sesionId, nuevo)
        }
        fun anotar(id: String, cambio: (SubidaAdjunto) -> SubidaAdjunto) = guardar(estado.con(id, cambio(estado.de(id))))
        fun claseEncontrada() {
            if (estado.noEncontrada != 0 || estado.esperarHastaMs != null || estado.ultimoError != null) {
                guardar(estado.copy(noEncontrada = 0, esperarHastaMs = null, ultimoError = null))
            }
        }
        /** Falló al mandar el archivo: espera creciente antes de volver a mandarlo. */
        fun fallo(id: String, momento: String, mensaje: String?) = anotar(id) {
            it.copy(fallos = it.fallos + 1, esperarHastaMs = reloj() + espera(it.fallos + 1), ultimoIntento = momento, ultimoError = mensaje)
        }

        // El server no encontraba la clase: se le vuelve a preguntar recién cuando pasa la espera.
        if (estado.noEncontrada > 0 && p.forzados.isEmpty() && esperando(estado.esperarHastaMs)) {
            return if (estado.claseNoEncontrada) Paso.LISTO else Paso.REINTENTAR
        }

        var paso = Paso.LISTO
        for (a in p.lista) {
            if (a.id !in p.forzados && esperando(estado.de(a.id).esperarHastaMs)) {
                // Falló hace poco: el archivo espera antes de volver a mandarse.
                paso = Paso.REINTENTAR
                continue
            }
            val archivo = adjuntos.archivo(a)
            if (!archivo.isFile) {
                anotar(a.id) { it.copy(rechazo = "El archivo ya no está en el celu") }
                continue
            }
            // Con Ambos, un minuto contado con la pista del celu va en el audio principal; si todavía no se
            // sabe el desfase, el adjunto espera (como los eventos).
            val paraSubir = sesiones.leerSesion(sesionId)
                ?.let { s -> Ambos.adjuntoParaSubir(a, s, Ambos.desfase(sesiones, s)) } ?: a.takeIf { it.pista == null }
            if (paraSubir == null) {
                paso = Paso.REINTENTAR
                continue
            }
            val momento = Tiempo.ahoraIso()

            // 1. La pregunta de unos bytes, sin el archivo.
            val consulta = try {
                api.consultarAdjunto(sesionId, a.id)
            } catch (e: ErrorAutenticacion) {
                anotar(a.id) { it.copy(ultimoIntento = momento, ultimoError = e.message) }
                throw e
            } catch (e: IOException) {
                anotar(a.id) { it.copy(ultimoIntento = momento, ultimoError = sinConexion(e)) }
                return Paso.CORTAR
            } catch (e: ErrorApi) {
                // 5xx, 408, 429: el server no puede ahora; con los otros pasaría lo mismo.
                anotar(a.id) { it.copy(ultimoIntento = momento, ultimoError = e.message) }
                return Paso.CORTAR
            }

            // 2. Lo normal: 422 "falta el archivo" (no lo tiene). Ahora sí, el archivo.
            val respuesta = if (consulta !is ResultadoAdjunto.Rechazado) consulta else {
                servidorAcepta()
                claseEncontrada()
                try {
                    alSubir(a.id)
                    api.subirAdjunto(sesionId, paraSubir, archivo)
                } catch (e: ErrorAutenticacion) {
                    anotar(a.id) { it.copy(ultimoIntento = momento, ultimoError = e.message) }
                    throw e
                } catch (e: IOException) {
                    fallo(a.id, momento, sinConexion(e))
                    return Paso.CORTAR
                } catch (e: ErrorApi) {
                    // El server no pudo ahora: se sigue con los otros y éste espera.
                    fallo(a.id, momento, e.message)
                    paso = Paso.REINTENTAR
                    continue
                } finally {
                    alSubir(null)
                }
            }

            when (respuesta) {
                is ResultadoAdjunto.Subido -> {
                    servidorAcepta()
                    claseEncontrada()
                    anotar(a.id) {
                        it.copy(
                            subido = true, servidor = respuesta.adjunto ?: it.servidor, rechazo = null, fallos = 0,
                            esperarHastaMs = null, ultimoError = null, ultimoIntento = momento,
                        )
                    }
                    respuesta.sesion?.let { s -> guardar(estado.copy(sesion = s)) }
                }
                is ResultadoAdjunto.SesionNoEncontrada -> {
                    servidorAcepta()
                    val n = estado.noEncontrada + 1
                    val espera = if (n > EstadoSubidaAdjuntos.MAX_NO_ENCONTRADA) ESPERA_CLASE_PERDIDA_MS else espera(n)
                    guardar(
                        estado.copy(
                            noEncontrada = n, esperarHastaMs = reloj() + espera,
                            ultimoError = "El server no encuentra esta clase (${respuesta.detalle})",
                        ),
                    )
                    // Los otros adjuntos de esta clase darían lo mismo. Si el server la perdió de verdad,
                    // pasados unos intentos se deja de pedir reintento (se pregunta cada tanto).
                    return if (estado.claseNoEncontrada) paso else Paso.REINTENTAR
                }
                ResultadoAdjunto.SinSoporte -> {
                    soporteGuardado = SoporteAdjuntos(sinSoporte = true, servidor = servidor, ultimoSondeoMs = reloj())
                    adjuntos.guardarSoporte(soporteGuardado)
                    return Paso.SIN_SOPORTE
                }
                is ResultadoAdjunto.Rechazado -> {
                    Log.w(TAG, "Adjunto ${a.id} rechazado (HTTP ${respuesta.codigo}): ${respuesta.detalle}")
                    anotar(a.id) {
                        it.copy(
                            rechazo = respuesta.detalle.ifBlank { "HTTP ${respuesta.codigo}" }, fallos = 0,
                            esperarHastaMs = null, ultimoIntento = momento, ultimoError = null,
                        )
                    }
                }
            }
        }
        return paso
    }

    /** El server contestó algo de adjuntos: si `soporte.json` decía que no los acepta, ya no vale. */
    private fun servidorAcepta() {
        if (!soporteGuardado.sinSoporte) return
        soporteGuardado = SoporteAdjuntos(servidor = servidor, ultimoSondeoMs = reloj())
        adjuntos.guardarSoporte(soporteGuardado)
    }

    /** Todavía no llegó [hasta]. Una espera más larga que la máxima es un reloj que cambió: no se respeta. */
    private fun esperando(hasta: Long?): Boolean {
        if (hasta == null) return false
        val falta = hasta - reloj()
        return falta in 1..ESPERA_CLASE_PERDIDA_MS
    }

    private fun sinConexion(e: IOException) = "Sin conexión con el server (${e.message ?: e.javaClass.simpleName})"

    companion object {
        private const val TAG = "ClassmerSubida"

        /** Espera antes de volver a mandar un archivo que falló: 30 s, 1 min, 2 min… hasta [ESPERA_MAXIMA_MS]. */
        const val ESPERA_INICIAL_MS = 30_000L
        const val ESPERA_MAXIMA_MS = 30 * 60_000L

        /** Una clase que el server ya no tiene: se le vuelve a preguntar cada hora (con un pedido de unos bytes). */
        const val ESPERA_CLASE_PERDIDA_MS = 60 * 60_000L

        /** Un server sin la ruta de adjuntos: se le vuelve a preguntar cada 10 min. */
        const val ESPERA_SIN_SOPORTE_MS = 10 * 60_000L

        fun espera(intentos: Int): Long = (ESPERA_INICIAL_MS shl (intentos - 1).coerceIn(0, 10)).coerceAtMost(ESPERA_MAXIMA_MS)

        /**
         * La clase ya existe en el server: si es del celu, lo dice su `subida.json`; si no hay carpeta
         * en `sesiones/`, se adjuntó desde una fila que sólo está en el server.
         */
        fun claseEnServidor(sesiones: AlmacenSesiones, sesionId: String): Boolean =
            !sesiones.dir(sesionId).isDirectory || sesiones.leerSubida(sesionId).sesionCreada

        /**
         * Lo crea [Subidor] cuando el server no tenía la clase y la volvió a crear (se borraron sus datos o
         * se cambió de server): los adjuntos que ya había confirmado se vuelven a mandar. Lo consume esta clase.
         */
        fun archivoPedidoReenvio(sesiones: AlmacenSesiones, sesionId: String) = File(sesiones.dir(sesionId), "reenviar-adjuntos")

        fun pedirReenvio(sesiones: AlmacenSesiones, sesionId: String) {
            runCatching { archivoPedidoReenvio(sesiones, sesionId).createNewFile() }
        }
    }
}
