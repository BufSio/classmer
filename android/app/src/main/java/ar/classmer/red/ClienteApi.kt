package ar.classmer.red

import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.AdjuntoServidor
import ar.classmer.modelo.AsignarMateria
import ar.classmer.modelo.CambiarNumero
import ar.classmer.modelo.CrearSesion
import ar.classmer.modelo.EstadoSesionServidor
import ar.classmer.modelo.Evento
import ar.classmer.modelo.Finalizar
import ar.classmer.modelo.Materia
import ar.classmer.util.JsonClassmer
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okio.BufferedSink
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** El server respondió algo distinto de lo esperado. El mensaje usa el `detail` de FastAPI si viene. */
open class ErrorApi(val codigo: Int, val cuerpo: String, mensaje: String = "HTTP $codigo: ${detalleDe(cuerpo)}") :
    Exception(mensaje) {
    /** Un 4xx que no se arregla reintentando (datos inválidos, cuerpo demasiado grande…). */
    val permanente: Boolean get() = codigo in 400..499 && codigo !in REINTENTABLES

    private companion object {
        val REINTENTABLES = setOf(404, 408, 409, 425, 429)
    }
}

/** El texto de `{"detail": ...}` (FastAPI): un string, o una lista de errores de validación. */
fun detalleDe(cuerpo: String): String = runCatching {
    when (val d = (JsonClassmer.parseToJsonElement(cuerpo) as JsonObject)["detail"]) {
        is JsonPrimitive -> d.content
        is JsonArray -> d.joinToString("; ") { e ->
            val o = e as? JsonObject
            val campo = (o?.get("loc") as? JsonArray)?.lastOrNull()?.jsonPrimitive?.contentOrNull
            val msg = o?.get("msg")?.jsonPrimitive?.contentOrNull ?: e.toString()
            if (campo != null) "$campo: $msg" else msg
        }
        else -> null
    }
}.getOrNull() ?: cuerpo.take(200)

/** 401/403: falta el token o está mal. No sirve reintentar hasta que se cambie. */
class ErrorAutenticacion(codigo: Int, cuerpo: String) : ErrorApi(codigo, cuerpo, "Token rechazado (HTTP $codigo)")

sealed interface ResultadoAudio {
    /** El server agregó los bytes: ahora tiene [bytesRecibidos]. */
    data class Ok(val bytesRecibidos: Long) : ResultadoAudio

    /** 409: el server tiene otro tamaño; hay que seguir desde [bytesRecibidos]. */
    data class Conflicto(val bytesRecibidos: Long) : ResultadoAudio
}

sealed interface ResultadoFinalizar {
    data class Ok(val estado: String?) : ResultadoFinalizar

    /**
     * 409 con `motivo`: `hash_distinto`, `faltan_bytes` o `faltan_eventos`. El contrato v1 agrega
     * `bytes_recibidos` (faltan_bytes, hash_distinto) y `eventos_recibidos` (faltan_eventos). Con Ambos,
     * [pista] dice a qué audio le pasa (`celu`); sin ella es el principal.
     */
    data class Conflicto(val motivo: String, val bytesRecibidos: Long? = null, val pista: String? = null) : ResultadoFinalizar
}

/** `GET /audio`: lo que tiene el server de un audio y, si lo dice, de qué pista es. */
data class EstadoAudio(val bytesRecibidos: Long, val pista: String? = null)

sealed interface ResultadoAsignar {
    /** 200: el server tomó la materia. [sesion] es su estado nuevo (`null` si no se pudo leer). */
    data class Ok(val sesion: EstadoSesionServidor?) : ResultadoAsignar

    /** 409: el apunte ya empezó y la materia no se puede cambiar desde el celu. */
    data class YaEmpezo(val detalle: String) : ResultadoAsignar

    /** 422: el server no tiene esa materia (por ejemplo, la desactivaron). */
    data class MateriaInvalida(val detalle: String) : ResultadoAsignar
}

/** Cliente del contrato de la API v1 (ARCHITECTURE.md §5.1). */
class ClienteApi(
    urlBase: String,
    private val token: String,
    private val http: OkHttpClient = httpCompartido,
) {
    private val base: HttpUrl = (urlBase.trimEnd('/') + "/").toHttpUrl()

    /** El server al que habla (normalizado): con él se anota qué server separa las pistas de Ambos. */
    val servidor: String get() = base.toString()

    suspend fun health(): String = ejecutar(get("api/health")) { it.cuerpo }

    suspend fun materias(): List<Materia> = ejecutar(get("api/materias")) {
        JsonClassmer.decodeFromString(ListSerializer(Materia.serializer()), it.cuerpo)
    }

    /**
     * `PATCH /api/materias/{id}` con `{"proximo_numero": N}`. Devuelve la materia actualizada, o
     * `null` si la respuesta no se pudo leer (el cambio igual quedó hecho).
     */
    suspend fun cambiarProximoNumero(materiaId: String, numero: Int): Materia? {
        val cuerpo = JsonClassmer.encodeToString(CambiarNumero.serializer(), CambiarNumero(numero)).toRequestBody(JSON)
        val url = base.newBuilder().addPathSegments("api/materias").addPathSegment(materiaId).build()
        return ejecutar(request(url).patch(cuerpo).build()) {
            runCatching { JsonClassmer.decodeFromString(Materia.serializer(), it.cuerpo) }.getOrNull()
        }
    }

    /** `POST /api/sesiones/{id}/materia`: elige la materia de una sesión en `esperando_usuario`. */
    suspend fun asignarMateria(sesionId: String, materiaId: String): ResultadoAsignar {
        val cuerpo = JsonClassmer.encodeToString(AsignarMateria.serializer(), AsignarMateria(materiaId)).toRequestBody(JSON)
        val url = base.newBuilder().addPathSegments("api/sesiones").addPathSegment(sesionId).addPathSegment("materia").build()
        return ejecutar(request(url).post(cuerpo).build(), aceptar = setOf(409, 422)) { r ->
            when (r.codigo) {
                409 -> ResultadoAsignar.YaEmpezo(detalleDe(r.cuerpo))
                422 -> ResultadoAsignar.MateriaInvalida(detalleDe(r.cuerpo))
                else -> ResultadoAsignar.Ok(
                    runCatching { JsonClassmer.decodeFromString(EstadoSesionServidor.serializer(), r.cuerpo) }.getOrNull(),
                )
            }
        }
    }

    /** Acepta una lista JSON o un objeto `{"sesiones": [...]}`. */
    suspend fun sesiones(limite: Int = 20): List<EstadoSesionServidor> =
        ejecutar(get("api/sesiones", "limite" to limite.toString())) { r ->
            val raiz = JsonClassmer.parseToJsonElement(r.cuerpo)
            val lista = when (raiz) {
                is JsonArray -> raiz
                is JsonObject -> raiz["sesiones"] as? JsonArray ?: JsonArray(emptyList())
                else -> JsonArray(emptyList())
            }
            lista.mapNotNull { elem ->
                runCatching { JsonClassmer.decodeFromJsonElement(EstadoSesionServidor.serializer(), elem) }.getOrNull()
            }
        }

    suspend fun sesion(id: String): EstadoSesionServidor = ejecutar(get("api/sesiones/$id")) {
        JsonClassmer.decodeFromString(EstadoSesionServidor.serializer(), it.cuerpo)
    }

    /** Idempotente por id: 201 (creada, devuelve true) o 200 (ya existía, devuelve false). */
    suspend fun crearSesion(sesion: CrearSesion): Boolean {
        val cuerpo = JsonClassmer.encodeToString(CrearSesion.serializer(), sesion).toRequestBody(JSON)
        return ejecutar(request("api/sesiones").post(cuerpo).build()) { it.codigo == 201 }
    }

    suspend fun bytesAudio(id: String, pista: String? = null): Long = estadoAudio(id, pista).bytesRecibidos

    /**
     * `GET /audio` (con [pista], `GET /audio?pista=celu`). Un server viejo ignora `pista` y contesta lo del
     * principal: por eso la pista del celu no se sube hasta saber que el server las separa.
     */
    suspend fun estadoAudio(id: String, pista: String? = null): EstadoAudio =
        ejecutar(get("api/sesiones/$id/audio", *conPista(pista))) { r ->
            val json = runCatching { JsonClassmer.parseToJsonElement(r.cuerpo) as? JsonObject }.getOrNull()
            EstadoAudio(
                bytesRecibidos = json?.get("bytes_recibidos")?.jsonPrimitive?.longOrNull
                    ?: throw ErrorApi(r.codigo, r.cuerpo, "Falta bytes_recibidos"),
                pista = json?.get("pista")?.jsonPrimitive?.contentOrNull,
            )
        }

    /** `PUT /audio?offset=N` (y `&pista=celu`) con los bytes `[offset, offset + largo)` del archivo. */
    suspend fun subirAudio(id: String, archivo: File, offset: Long, largo: Long, pista: String? = null): ResultadoAudio {
        val url = url("api/sesiones/$id/audio", "offset" to offset.toString(), *conPista(pista))
        val req = request(url).put(CuerpoArchivoParcial(archivo, offset, largo)).build()
        return ejecutar(req, aceptar = setOf(409)) { r ->
            if (r.codigo == 409) {
                ResultadoAudio.Conflicto(leerBytesRecibidos(r.cuerpo) ?: bytesAudio(id, pista))
            } else {
                ResultadoAudio.Ok(leerBytesRecibidos(r.cuerpo) ?: (offset + largo))
            }
        }
    }

    /**
     * `GET /api/sesiones/{id}/eventos` → `{"ids": [...]}`: los eventos que ya tiene el server.
     * Devuelve `null` si el server no tiene esa ruta (404/405), para reenviar todos.
     */
    suspend fun idsEventos(id: String): Set<String>? =
        ejecutar(get("api/sesiones/$id/eventos"), aceptar = setOf(404, 405)) { r ->
            if (r.codigo != 200) return@ejecutar null
            val ids = (JsonClassmer.parseToJsonElement(r.cuerpo) as? JsonObject)?.get("ids") as? JsonArray
            ids?.mapNotNull { it.jsonPrimitive.contentOrNull }?.toSet()
        }

    /**
     * Multipart, idempotente por el UUID del evento: 201 (nuevo) o 200 (ya existía). Una foto de una
     * clase importada va sin `offset_ms` (el server la ubica por su EXIF).
     */
    suspend fun subirEvento(id: String, evento: Evento, foto: File?) {
        val partes = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("id", evento.id)
            .addFormDataPart("tipo", evento.tipo)
        evento.offsetMs?.let { partes.addFormDataPart("offset_ms", it.toString()) }
        partes.addFormDataPart("creado", evento.creado)
        evento.texto?.let { partes.addFormDataPart("texto", it) }
        evento.marca?.let { partes.addFormDataPart("marca", it) }
        if (foto != null) {
            partes.addFormDataPart("archivo", foto.name, foto.asRequestBody(tipoFoto(foto.name)))
        }
        ejecutar(request("api/sesiones/$id/eventos").post(partes.build()).build()) { }
    }

    suspend fun finalizar(id: String, datos: Finalizar): ResultadoFinalizar {
        val cuerpo = JsonClassmer.encodeToString(Finalizar.serializer(), datos).toRequestBody(JSON)
        val req = request("api/sesiones/$id/finalizar").post(cuerpo).build()
        return ejecutar(req, aceptar = setOf(409)) { r ->
            val json = runCatching { JsonClassmer.parseToJsonElement(r.cuerpo) as? JsonObject }.getOrNull()
            if (r.codigo == 409) {
                ResultadoFinalizar.Conflicto(
                    motivo = json?.get("motivo")?.jsonPrimitive?.contentOrNull ?: "desconocido",
                    bytesRecibidos = json?.get("bytes_recibidos")?.jsonPrimitive?.longOrNull,
                    pista = json?.get("pista")?.jsonPrimitive?.contentOrNull,
                )
            } else {
                ResultadoFinalizar.Ok(json?.get("estado")?.jsonPrimitive?.contentOrNull)
            }
        }
    }

    /** Descarga el PDF (de la ruta que informa el server o la estándar) a [destino]. */
    suspend fun descargarPdf(id: String, ruta: String?, destino: File) {
        val url = ruta?.let { base.resolve(it.removePrefix("/")) } ?: url("api/sesiones/$id/apunte.pdf")
        val call = http.newCall(request(url).get().build())
        val respuesta = call.await()
        respuesta.use { r ->
            if (r.code == 401 || r.code == 403) throw ErrorAutenticacion(r.code, r.body.string())
            if (!r.isSuccessful) throw ErrorApi(r.code, r.body.string())
            val tmp = File(destino.parentFile, destino.name + ".tmp")
            tmp.outputStream().use { out -> r.body.byteStream().copyTo(out) }
            if (!tmp.renameTo(destino)) {
                destino.delete()
                if (!tmp.renameTo(destino)) throw IOException("No se pudo guardar el PDF")
            }
        }
    }

    // ---- Infraestructura ----

    private class Respuesta(val codigo: Int, val cuerpo: String)

    private fun url(ruta: String, vararg query: Pair<String, String>): HttpUrl {
        val b = base.newBuilder().addPathSegments(ruta)
        query.forEach { (k, v) -> b.addQueryParameter(k, v) }
        return b.build()
    }

    private fun request(ruta: String) = request(url(ruta))

    private fun request(url: HttpUrl) = Request.Builder().url(url).header("Authorization", "Bearer $token")

    private fun get(ruta: String, vararg query: Pair<String, String>) = request(url(ruta, *query)).get().build()

    private suspend fun <T> ejecutar(req: Request, aceptar: Set<Int> = emptySet(), leer: suspend (Respuesta) -> T): T {
        val r = http.newCall(req).await().use { Respuesta(it.code, it.body.string()) }
        if (r.codigo == 401 || r.codigo == 403) throw ErrorAutenticacion(r.codigo, r.cuerpo)
        if (r.codigo !in 200..299 && r.codigo !in aceptar) throw ErrorApi(r.codigo, r.cuerpo)
        return leer(r)
    }

    private fun conPista(pista: String?): Array<Pair<String, String>> = if (pista == null) emptyArray() else arrayOf("pista" to pista)

    private fun leerBytesRecibidos(cuerpo: String): Long? = runCatching {
        (JsonClassmer.parseToJsonElement(cuerpo) as JsonObject)["bytes_recibidos"]?.jsonPrimitive?.longOrNull
    }.getOrNull()

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()
        private val JPEG = "image/jpeg".toMediaType()
        private val OCTET = "application/octet-stream".toMediaType()

        /** Las de la cámara son `.jpg`; las importadas pueden ser PNG, WebP o HEIC (el server mira la extensión). */
        private fun tipoFoto(nombre: String) = when (nombre.substringAfterLast('.').lowercase()) {
            "png" -> "image/png".toMediaType()
            "webp" -> "image/webp".toMediaType()
            "heic", "heif" -> "image/heic".toMediaType()
            else -> JPEG
        }

        val httpCompartido: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(60, TimeUnit.SECONDS)
                .writeTimeout(60, TimeUnit.SECONDS)
                .build()
        }
    }

    /** Un tramo del archivo de audio, leído del disco al enviar (se puede reenviar si OkHttp reintenta). */
    private class CuerpoArchivoParcial(val archivo: File, val desde: Long, val largo: Long) : RequestBody() {
        override fun contentType() = OCTET
        override fun contentLength() = largo
        override fun writeTo(sink: BufferedSink) {
            RandomAccessFile(archivo, "r").use { raf ->
                raf.seek(desde)
                val buffer = ByteArray(64 * 1024)
                var restante = largo
                while (restante > 0) {
                    val n = raf.read(buffer, 0, minOf(buffer.size.toLong(), restante).toInt())
                    if (n < 0) throw IOException("El audio local es más corto de lo esperado")
                    sink.write(buffer, 0, n)
                    restante -= n
                }
            }
        }
    }

    // ---- Adjuntos de una clase ----

    /**
     * `POST /api/sesiones/{id}/adjuntos` (multipart), idempotente por el id del adjunto. El archivo va
     * con su nombre original y su tipo; `offset_ms` sólo si se adjuntó grabando.
     */
    suspend fun subirAdjunto(sesionId: String, adjunto: AdjuntoLocal, archivo: File): ResultadoAdjunto {
        val partes = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("id", adjunto.id)
            .addFormDataPart("creado", adjunto.creado)
        adjunto.offsetMs?.let { partes.addFormDataPart("offset_ms", it.toString()) }
        val tipo = runCatching { adjunto.mime.toMediaType() }.getOrDefault(OCTET)
        partes.addFormDataPart("archivo", adjunto.nombreOriginal, archivo.asRequestBody(tipo))
        return postAdjunto(sesionId, partes.build())
    }

    /**
     * El mismo `POST /adjuntos` pero sólo con el `id`, sin el archivo: un pedido de unos bytes que dice
     * si vale la pena mandar el archivo. El server contesta, antes de leer nada más:
     * - 404 "sesión no encontrada" si no tiene la clase, o 404 "Not Found"/405 si no tiene la ruta;
     * - 200 con el adjunto si ese id ya lo tiene (se había perdido la respuesta);
     * - 422 ("falta el archivo") si no lo tiene: entonces sí hay que mandarlo.
     */
    suspend fun consultarAdjunto(sesionId: String, adjuntoId: String): ResultadoAdjunto =
        postAdjunto(sesionId, MultipartBody.Builder().setType(MultipartBody.FORM).addFormDataPart("id", adjuntoId).build())

    private suspend fun postAdjunto(sesionId: String, cuerpo: MultipartBody): ResultadoAdjunto {
        val url = base.newBuilder().addPathSegments("api/sesiones").addPathSegment(sesionId).addPathSegment("adjuntos").build()
        return ejecutar(request(url).post(cuerpo).build(), aceptar = CODIGOS_RECHAZO_ADJUNTO) { r ->
            when {
                r.codigo in 200..299 -> {
                    val json = runCatching { JsonClassmer.parseToJsonElement(r.cuerpo) as? JsonObject }.getOrNull()
                    ResultadoAdjunto.Subido(
                        nuevo = r.codigo == 201,
                        adjunto = json?.let { runCatching { JsonClassmer.decodeFromJsonElement(AdjuntoServidor.serializer(), it) }.getOrNull() },
                        sesion = (json?.get("sesion") as? JsonObject)?.let {
                            runCatching { JsonClassmer.decodeFromJsonElement(EstadoSesionServidor.serializer(), it) }.getOrNull()
                        },
                    )
                }
                esRutaInexistente(r.codigo, r.cuerpo) -> ResultadoAdjunto.SinSoporte
                r.codigo == 404 -> ResultadoAdjunto.SesionNoEncontrada(detalleDe(r.cuerpo))
                else -> ResultadoAdjunto.Rechazado(r.codigo, detalleDe(r.cuerpo))
            }
        }
    }

    /** `POST /api/sesiones/{id}/regenerar`: vuelve a escribir el apunte entero, ahora con los adjuntos. */
    suspend fun regenerar(sesionId: String): ResultadoRegenerar {
        val url = base.newBuilder().addPathSegments("api/sesiones").addPathSegment(sesionId).addPathSegment("regenerar").build()
        return ejecutar(request(url).post("{}".toRequestBody(JSON)).build(), aceptar = setOf(404, 405, 409)) { r ->
            when {
                r.codigo == 409 -> ResultadoRegenerar.Rechazado(detalleDe(r.cuerpo))
                esRutaInexistente(r.codigo, r.cuerpo) -> ResultadoRegenerar.SinSoporte
                r.codigo == 404 -> ResultadoRegenerar.NoEncontrada(detalleDe(r.cuerpo))
                else -> ResultadoRegenerar.Ok(
                    runCatching { JsonClassmer.decodeFromString(EstadoSesionServidor.serializer(), r.cuerpo) }.getOrNull(),
                )
            }
        }
    }
}

/** Ejecuta la llamada sin bloquear un hilo y la cancela si se cancela la corrutina. */
suspend fun Call.await(): Response = suspendCancellableCoroutine { cont ->
    enqueue(object : Callback {
        override fun onResponse(call: Call, response: Response) = cont.resume(response) { _, r, _ -> r.close() }
        override fun onFailure(call: Call, e: IOException) = cont.resumeWithException(e)
    })
    cont.invokeOnCancellation { runCatching { cancel() } }
}
