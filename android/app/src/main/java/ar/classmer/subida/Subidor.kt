package ar.classmer.subida

import ar.classmer.datos.AlmacenSesiones
import ar.classmer.grabacion.OggDuracion
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.EstadoSubida
import ar.classmer.modelo.Finalizar
import ar.classmer.modelo.FinalizarCelu
import ar.classmer.modelo.OrigenMateria
import ar.classmer.modelo.Pista
import ar.classmer.modelo.SesionLocal
import ar.classmer.modelo.paraCrear
import ar.classmer.red.ClienteApi
import ar.classmer.red.ErrorApi
import ar.classmer.red.ErrorAutenticacion
import ar.classmer.red.ResultadoAudio
import ar.classmer.red.ResultadoFinalizar
import ar.classmer.util.Tiempo
import ar.classmer.watch.Ambos
import ar.classmer.watch.PistasCelu
import android.util.Log
import kotlinx.coroutines.CancellationException
import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest

/** El server contestó algo que no deja avanzar con esta sesión (se reintenta más tarde). */
class ErrorProtocolo(mensaje: String) : Exception(mensaje)

/**
 * Sube una sesión al server siguiendo el contrato de la API v1:
 *
 * 1. `POST /api/sesiones` (idempotente).
 * 2. Audio por append: `PUT /audio?offset=N` con los bytes nuevos; ante un 409 sigue desde el
 *    `bytes_recibidos` que informa el server.
 * 3. Eventos (`multipart`), idempotentes por UUID.
 * 4. Si la grabación terminó y el server tiene todo: `POST /finalizar` con SHA-256.
 *
 * Cada paso confirmado se anota en `subida.json`, así se retoma donde quedó. No borra nada.
 * Los errores de red se propagan (`IOException`) para que el trabajo reintente con espera creciente.
 *
 * Una clase importada va igual, con tres diferencias: el `POST` lleva `importada`, `audio_nombre` y
 * `audio_bytes`; se sube su `audio.<ext>` en tramos de [tamanioTramoImportada]; y si el celu no pudo
 * medir su duración, `/finalizar` la manda `null` (el server usa la que mide él).
 *
 * Una de Ambos (§15) sube además la pista del celu (`?pista=celu`, el mismo esquema reanudable) y la
 * declara en el bloque `celu` de `/finalizar`. Sólo si el server separa las pistas: un server viejo
 * ignora `pista` y la pegaría al audio principal ([soportaPistaCelu]). Con un server así, la clase se
 * finaliza sin la pista, que queda en el celu. Lo mismo si el server rechaza la pista: una falla de la
 * segunda pista nunca traba la clase.
 */
class Subidor(
    private val almacen: AlmacenSesiones,
    private val api: ClienteApi,
    private val tamanioTramo: Long = TAMANIO_TRAMO,
    private val tamanioTramoImportada: Long = TAMANIO_TRAMO_IMPORTADA,
) {
    enum class Resultado {
        /** El server confirmó todo (`/finalizar` aceptado). */
        COMPLETA,

        /** Se subió lo que había, pero la clase sigue grabándose. */
        PARCIAL,

        /** La clase terminó pero falta algo (una foto que la cámara todavía puede estar guardando). */
        ESPERANDO,
    }

    suspend fun sincronizar(id: String): Resultado {
        var estado = almacen.leerSubida(id)
        if (estado.finalizada) return Resultado.COMPLETA

        fun guardar(nuevo: EstadoSubida) {
            estado = nuevo
            almacen.guardarSubida(id, nuevo)
        }

        val inicial = almacen.leerSesion(id) ?: throw ErrorProtocolo("Falta sesion.json de $id")
        // Una importada que todavía se está copiando al celu no va al server: si la copia falla y se
        // descarta, no queda allá una clase sin audio que además corra la numeración.
        if (inicial.estadoLocal == EstadoLocal.IMPORTANDO) return Resultado.ESPERANDO
        // Se manda siempre (es idempotente): si el server no conocía la sesión (se borraron sus
        // datos, o se cambió de server en Ajustes), la crea de nuevo y se le sube todo desde cero.
        val creadaRecien = try {
            crearSesion(inicial, estado) { guardar(it) }
        } catch (e: ErrorApi) {
            // Si el server ya la tenía, un rechazo de este POST (por ejemplo, porque cambió su config de
            // materias) no frena el resto: una sesión perdida de verdad aparece como 404 más adelante.
            if (e is ErrorAutenticacion || !estado.sesionCreada) throw e
            false
        }
        if (creadaRecien && estado.sesionCreada) {
            // Puede ser otro server (se cambió en Ajustes): también se vuelve a preguntar si separa las pistas.
            guardar(
                estado.copy(
                    bytesConfirmados = 0, eventosSubidos = emptySet(),
                    celuBytesConfirmados = 0, celuSoportadaEn = null, celuRechazada = null, celuSinSubir = false,
                ),
            )
            // Los adjuntos que ya había confirmado también se vuelven a mandar (lo hace SubidorAdjuntos).
            SubidorAdjuntos.pedirReenvio(almacen, id)
        }
        if (!estado.sesionCreada) guardar(estado.copy(sesionCreada = true))

        var rechazosFinalizar = 0
        var rechazosCelu = 0
        // Que el server separa las pistas se vuelve a preguntar una vez por pasada antes de subir la del celu:
        // un server que vuelve en la misma URL con una versión vieja la pegaría al principal.
        var pistaVerificada = false
        while (true) {
            // Se lee el estado de la grabación ANTES que el tamaño del audio: si ya terminó,
            // el archivo está cerrado y su tamaño es el definitivo.
            val sesion = almacen.leerSesion(id) ?: throw ErrorProtocolo("Falta sesion.json de $id")
            val audio = almacen.archivoAudio(sesion)
            val largo = if (audio.exists()) audio.length() else 0L
            // MediaRecorder no hace fsync: se fuerza (después de leer el largo) antes de subir, así el
            // server nunca confirma bytes que un reinicio brusco del celu podría borrar.
            sincronizarDisco(audio)

            val tramo = if (sesion.importada) tamanioTramoImportada else tamanioTramo
            subirAudio(id, audio, largo, estado.bytesConfirmados, tramo) { b ->
                guardar(estado.copy(bytesConfirmados = b))
            }

            val eventos = almacen.leerEventos(id)
            // Con Ambos, algunos minutos se contaron en la otra pista: van al server en el audio principal.
            val desfase = Ambos.desfase(almacen, sesion)
            for (evento in eventos) {
                if (evento.id in estado.eventosSubidos || evento.id in estado.eventosRechazados) continue
                val foto = evento.archivo?.let { File(almacen.dir(id), it) }?.takeIf { it.isFile }
                // Si hace falta el desfase y todavía no se sabe, va en otra pasada (antes de finalizar, seguro).
                val paraSubir = Ambos.paraSubir(evento, sesion, desfase) ?: continue
                try {
                    api.subirEvento(id, paraSubir, foto)
                } catch (e: ErrorApi) {
                    if (e is ErrorAutenticacion || !e.permanente) throw e
                    // El server no acepta este evento (por ejemplo, una foto cuyo archivo ya no está):
                    // se deja afuera en vez de trabar toda la clase.
                    Log.w(TAG, "Evento ${evento.id} (${evento.tipo}) rechazado: ${e.message}")
                    guardar(estado.copy(eventosRechazados = estado.eventosRechazados + evento.id))
                    continue
                }
                guardar(estado.copy(eventosSubidos = estado.eventosSubidos + evento.id))
            }

            if (sesion.pistaCelu) pistaVerificada = subirPistaCelu(id, pistaVerificada, { estado }) { guardar(it) }

            if (!sesion.terminada) return Resultado.PARCIAL

            // Fotos que la cámara guardó sin que llegara su evento (la app se cerró, la clase terminó
            // con la cámara abierta): se registran ahora. Si alguna puede estar guardándose, se espera.
            val antes = eventos.size
            if (almacen.resolverFotosPendientes(id)) return Resultado.ESPERANDO
            if (almacen.leerEventos(id).size != antes) continue

            val sha = if (estado.sha256 != null && estado.sha256Bytes == largo) {
                estado.sha256!!
            } else {
                sha256(audio).also { guardar(estado.copy(sha256 = it, sha256Bytes = largo)) }
            }
            val bloque = bloqueCelu(id, sesion, { estado }) { guardar(it) }
            val celu = when (bloque) {
                BloqueCelu.Subir -> continue
                is BloqueCelu.Listo -> bloque.celu
                BloqueCelu.SinSubir, null -> null
            }
            val datos = Finalizar(
                sha256 = sha,
                bytesTotales = largo,
                // En una importada, 0 es que el celu no la pudo medir: va null (la mide el server).
                duracionMs = if (sesion.importada) sesion.duracionMs.takeIf { it > 0 } else sesion.duracionMs,
                eventosTotales = eventos.count { it.id !in estado.eventosRechazados },
                celu = celu,
            )
            when (val r = api.finalizar(id, datos)) {
                is ResultadoFinalizar.Ok -> {
                    guardar(
                        estado.copy(
                            finalizada = true, ultimoError = null, ultimoOk = Tiempo.ahoraIso(),
                            celuSinSubir = bloque == BloqueCelu.SinSubir,
                        ),
                    )
                    return Resultado.COMPLETA
                }
                is ResultadoFinalizar.Conflicto -> if (r.pista != null) {
                    // La pista del celu (Ambos): lo mismo que con el principal, en su propio estado. Si no se
                    // arregla, la clase se finaliza sin ella en vez de trabarse.
                    rechazosCelu++
                    if (rechazosCelu > MAX_RECHAZOS_FINALIZAR) {
                        rechazarPistaCelu(id, "el server rechazó /finalizar $rechazosCelu veces (${r.motivo})", { estado }) { guardar(it) }
                    } else {
                        conPistaCelu(id, { estado }, { guardar(it) }) { guardar(conflictoCelu(id, r, estado)) }
                    }
                } else {
                    rechazosFinalizar++
                    if (rechazosFinalizar > MAX_RECHAZOS_FINALIZAR) {
                        throw ErrorProtocolo("El server rechazó /finalizar ${rechazosFinalizar} veces (${r.motivo})")
                    }
                    when (r.motivo) {
                        "faltan_bytes" -> guardar(estado.copy(bytesConfirmados = r.bytesRecibidos ?: api.bytesAudio(id)))
                        "faltan_eventos" -> {
                            // Se reenvían sólo los que el server no tiene (o todos, si no lo informa).
                            val enServer = api.idsEventos(id) ?: emptySet()
                            guardar(estado.copy(eventosSubidos = estado.eventosSubidos intersect enServer))
                        }
                        "hash_distinto" -> {
                            // El server aparta su audio y vuelve a 0: se sube el archivo entero.
                            val enServer = r.bytesRecibidos ?: api.bytesAudio(id)
                            if (enServer != 0L) {
                                throw ErrorProtocolo(
                                    "El hash no coincide y el server conserva $enServer bytes: no se puede volver a subir el audio entero",
                                )
                            }
                            guardar(estado.copy(bytesConfirmados = 0))
                        }
                        else -> throw ErrorProtocolo("El server rechazó /finalizar: ${r.motivo}")
                    }
                }
            }
        }
    }

    /** Un 409 de `/finalizar` que dice `"pista": "celu"`: se sigue desde lo que tiene el server de esa pista. */
    private suspend fun conflictoCelu(id: String, r: ResultadoFinalizar.Conflicto, estado: EstadoSubida): EstadoSubida {
        if (r.pista != Pista.CELU) throw ErrorProtocolo("El server rechazó /finalizar: ${r.motivo} (pista ${r.pista})")
        return when (r.motivo) {
            "faltan_bytes" -> estado.copy(celuBytesConfirmados = r.bytesRecibidos ?: api.bytesAudio(id, Pista.CELU))
            "hash_distinto" -> {
                // Como con el principal: el server aparta su copia y vuelve a 0.
                val enServer = r.bytesRecibidos ?: api.bytesAudio(id, Pista.CELU)
                if (enServer != 0L) throw ErrorProtocolo("El hash de la pista del celu no coincide y el server conserva $enServer bytes")
                estado.copy(celuBytesConfirmados = 0)
            }
            else -> throw ErrorProtocolo("El server rechazó /finalizar: ${r.motivo} (pista ${r.pista})")
        }
    }

    /**
     * `POST /api/sesiones`. Devuelve true si el server la creó recién (201). Si el server rechaza los
     * datos (400/422), se prueba sin `numero_clase` (lo pone el server) y después como Auto (IA), por
     * ejemplo con una materia que ya no existe en su config: la clase nunca queda trabada.
     */
    private suspend fun crearSesion(sesion: SesionLocal, estado: EstadoSubida, guardar: (EstadoSubida) -> Unit): Boolean {
        val completa = sesion.paraCrear()
        val sinNumero = completa.copy(numeroClase = null)
        val comoAuto = sinNumero.copy(materiaId = null, origenMateria = OrigenMateria.AUTO)
        if (estado.crearComoAuto) return api.crearSesion(comoAuto)
        // Sin repetir: sin número o en Auto, algunos intentos son el mismo cuerpo.
        val intentos = listOfNotNull(completa.takeIf { !estado.crearSinNumero }, sinNumero, comoAuto).distinct()
        for ((i, cuerpo) in intentos.withIndex()) {
            try {
                val creada = api.crearSesion(cuerpo)
                when {
                    cuerpo.materiaId == null && completa.materiaId != null -> guardar(estado.copy(crearComoAuto = true))
                    cuerpo.numeroClase == null && completa.numeroClase != null && !estado.crearSinNumero ->
                        guardar(estado.copy(crearSinNumero = true))
                }
                return creada
            } catch (e: ErrorApi) {
                if (e is ErrorAutenticacion || e.codigo !in setOf(400, 422) || i == intentos.lastIndex) throw e
                Log.w(TAG, "El server rechazó la sesión (${e.message}); se prueba con menos datos")
            }
        }
        error("Sin intentos para crear la sesión")
    }

    /**
     * Ambos: sube lo nuevo de la pista del celu, pero recién cuando se sabe que el server separa las pistas
     * (antes, nada: un server viejo la pegaría al principal). Se pregunta otra vez en cada pasada en que hay
     * algo para subir ([yaVerificada]: ya se preguntó en ésta). Devuelve si quedó verificada en esta pasada.
     */
    private suspend fun subirPistaCelu(id: String, yaVerificada: Boolean, estado: () -> EstadoSubida, guardar: (EstadoSubida) -> Unit): Boolean {
        if (estado().celuRechazada != null) return yaVerificada
        val pistas = PistasCelu(almacen)
        // Como con el principal: primero si terminó, después el largo (cerrada, su largo es el definitivo).
        pistas.leer(id) ?: return yaVerificada
        val audio = pistas.audio(id)
        val largo = if (audio.exists()) audio.length() else 0L
        if (largo == 0L || (soportada(estado()) && estado().celuBytesConfirmados == largo)) return yaVerificada
        // MediaRecorder no hace fsync (tampoco al parar): el server nunca confirma bytes que un reinicio borraría.
        sincronizarDisco(audio)
        var verificada = yaVerificada
        conPistaCelu(id, estado, guardar) {
            if (!verificada || !soportada(estado())) {
                if (!soportaPistaCelu(id)) {
                    if (estado().celuSoportadaEn != null) {
                        Log.w(TAG, "El server ya no separa las pistas: la del celu de $id no se sube")
                        guardar(estado().copy(celuSoportadaEn = null))
                    }
                    return@conPistaCelu
                }
                if (!soportada(estado())) guardar(estado().copy(celuSoportadaEn = api.servidor))
                verificada = true
            }
            subirAudio(id, audio, largo, estado().celuBytesConfirmados, tamanioTramo, Pista.CELU) { b ->
                guardar(estado().copy(celuBytesConfirmados = b))
            }
        }
        return verificada
    }

    /** Lo confirmó este server: con otro (se cambió en Ajustes) se vuelve a preguntar. */
    private fun soportada(estado: EstadoSubida) = estado.celuSoportadaEn == api.servidor

    /**
     * El server separa las pistas: lo dice en la respuesta, o su `GET /audio?pista=celu` no da lo mismo que
     * el `GET /audio` del principal, que tiene que tener bytes. Un server viejo ignora `pista` y contesta lo
     * del principal. Se pregunta por el principal en el momento (no lo que se anotó: el server pudo haberlo
     * vuelto a 0 con una respuesta que se perdió).
     */
    private suspend fun soportaPistaCelu(id: String): Boolean {
        val celu = api.estadoAudio(id, Pista.CELU)
        if (celu.pista == Pista.CELU) return true
        val principal = api.bytesAudio(id)
        return principal > 0 && celu.bytesRecibidos != principal
    }

    /**
     * Lo de la pista del celu, aislado: un error que no se arregla reintentando (un 4xx, un 409 que no cierra)
     * la deja afuera y la clase sigue sin ella. Sin red o con el token mal, se reintenta como todo.
     */
    private suspend fun conPistaCelu(id: String, estado: () -> EstadoSubida, guardar: (EstadoSubida) -> Unit, bloque: suspend () -> Unit) {
        try {
            bloque()
        } catch (e: CancellationException) {
            throw e
        } catch (e: ErrorAutenticacion) {
            throw e
        } catch (e: ErrorApi) {
            if (!e.permanente) throw e
            rechazarPistaCelu(id, e.message ?: "HTTP ${e.codigo}", estado, guardar)
        } catch (e: ErrorProtocolo) {
            rechazarPistaCelu(id, e.message ?: "error de protocolo", estado, guardar)
        }
    }

    private fun rechazarPistaCelu(id: String, motivo: String, estado: () -> EstadoSubida, guardar: (EstadoSubida) -> Unit) {
        Log.w(TAG, "La pista del celu de $id queda afuera: $motivo")
        guardar(estado().copy(celuRechazada = motivo))
    }

    private sealed interface BloqueCelu {
        /** Falta subir la pista (se acaba de saber que el server la acepta): otra vuelta antes de finalizar. */
        data object Subir : BloqueCelu
        data class Listo(val celu: FinalizarCelu) : BloqueCelu

        /** Hay pista, pero el server no la toma (server viejo, o la rechazó): se finaliza sin ella. */
        data object SinSubir : BloqueCelu
    }

    /**
     * El bloque `celu` de `/finalizar`; `null` si la clase no lleva pista del celu (no es de Ambos, o la pista
     * está vacía), y [BloqueCelu.SinSubir] si la lleva pero el server no la toma: queda sólo en el celu.
     */
    private suspend fun bloqueCelu(id: String, sesion: SesionLocal, estado: () -> EstadoSubida, guardar: (EstadoSubida) -> Unit): BloqueCelu? {
        if (!sesion.pistaCelu) return null
        val pistas = PistasCelu(almacen)
        val pista = pistas.leer(id) ?: return null
        val audio = pistas.audio(id)
        val largo = if (audio.exists()) audio.length() else 0L
        if (largo == 0L) return null
        if (estado().celuRechazada != null) return BloqueCelu.SinSubir
        if (!soportada(estado())) {
            // El principal ya está entero en el server: es la última oportunidad de distinguirlo.
            var soporta = false
            conPistaCelu(id, estado, guardar) { soporta = soportaPistaCelu(id) }
            if (soporta) {
                guardar(estado().copy(celuSoportadaEn = api.servidor))
                return BloqueCelu.Subir
            }
            Log.w(TAG, "El server no separa las pistas: $id se finaliza sin la del celu, que queda en el celu")
            return BloqueCelu.SinSubir
        }
        if (estado().celuBytesConfirmados != largo) return BloqueCelu.Subir
        sincronizarDisco(audio)
        val sha = estado().celuSha256?.takeIf { estado().celuSha256Bytes == largo }
            ?: sha256(audio).also { guardar(estado().copy(celuSha256 = it, celuSha256Bytes = largo)) }
        val duracion = if (pista.terminada) pista.duracionMs else OggDuracion.leerMs(audio) ?: pista.duracionMs
        return BloqueCelu.Listo(
            FinalizarCelu(
                sha256 = sha,
                bytesTotales = largo,
                duracionMs = duracion,
                desfaseMs = Ambos.desfase(almacen, sesion) ?: 0,
            ),
        )
    }

    /** Sube `[desde, largo)` en tramos; ante un 409 sigue desde lo que tiene el server. */
    private suspend fun subirAudio(id: String, audio: File, largo: Long, desde: Long, tramo: Long, pista: String? = null, confirmar: (Long) -> Unit) {
        if (desde > largo) {
            throw ErrorProtocolo("El server confirmó $desde bytes de audio pero el celu tiene $largo")
        }
        var b = desde
        var conflictosSeguidos = 0
        while (b < largo) {
            val n = minOf(tramo, largo - b)
            when (val r = api.subirAudio(id, audio, b, n, pista)) {
                is ResultadoAudio.Ok -> {
                    conflictosSeguidos = 0
                    b = r.bytesRecibidos
                }
                is ResultadoAudio.Conflicto -> {
                    conflictosSeguidos++
                    if (conflictosSeguidos > MAX_CONFLICTOS_SEGUIDOS) {
                        throw ErrorProtocolo("El server rechaza el audio: offset $b, dice tener ${r.bytesRecibidos}")
                    }
                    b = r.bytesRecibidos
                }
            }
            if (b !in 0..largo) {
                throw ErrorProtocolo("El server dice tener $b bytes de audio pero el celu tiene $largo")
            }
            confirmar(b)
        }
    }

    companion object {
        private const val TAG = "ClassmerSubida"

        /** 1 MiB por request: ~4 min de audio a 32 kbps. Si se corta, se pierde poco. */
        const val TAMANIO_TRAMO = 1L shl 20

        /**
         * 4 MiB por request en una importada: el audio ya está entero (puede pesar cientos de MB) y
         * con tramos de 1 MiB se iría mucho en idas y vueltas. Sigue debajo de los 8 MB del contrato.
         */
        const val TAMANIO_TRAMO_IMPORTADA = 4L shl 20
        const val MAX_CONFLICTOS_SEGUIDOS = 5
        const val MAX_RECHAZOS_FINALIZAR = 3

        fun sincronizarDisco(archivo: File) {
            if (!archivo.exists()) return
            runCatching { RandomAccessFile(archivo, "r").use { it.fd.sync() } }
        }

        fun sha256(archivo: File): String {
            val digest = MessageDigest.getInstance("SHA-256")
            if (archivo.exists()) {
                archivo.inputStream().use { input ->
                    val buffer = ByteArray(256 * 1024)
                    while (true) {
                        val n = input.read(buffer)
                        if (n < 0) break
                        digest.update(buffer, 0, n)
                    }
                }
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }
    }
}
