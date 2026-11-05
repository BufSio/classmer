package ar.classmer.subida

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.RecordedRequest
import java.io.ByteArrayOutputStream
import java.security.MessageDigest

/**
 * Implementación mínima del contrato de la API v1 para los tests del [Subidor], con fallas
 * inyectables. Guarda el audio de cada sesión y los ids de sus eventos.
 */
class ServidorDePrueba(private val token: String = "tok") : Dispatcher() {
    class Sesion {
        val audio = ByteArrayOutputStream()
        val eventos = linkedMapOf<String, String>()
        val fotos = mutableMapOf<String, Int>()
        var finalizada = false
        var cuerpoCrear: String? = null

        /** El `offset_ms` de cada evento como llegó (`null` si no vino el campo). */
        val offsets = mutableMapOf<String, String?>()

        /** Nombre y tipo del archivo de cada foto. */
        val archivosFotos = mutableMapOf<String, Pair<String, String?>>()

        /** El largo de cada PUT de audio que se guardó, en orden. */
        val tramos = mutableListOf<Int>()

        /** El cuerpo del último `/finalizar`. */
        var cuerpoFinalizar: String? = null

        /** La pista del celu (Ambos, `?pista=celu`) y el largo de cada PUT que se guardó. */
        val audioCelu = ByteArrayOutputStream()
        val tramosCelu = mutableListOf<Int>()

        val importada: Boolean get() = cuerpoCrear?.contains("\"importada\":true") == true
    }

    val sesiones = mutableMapOf<String, Sesion>()
    val pedidos = mutableListOf<String>()

    /** Los próximos N pedidos responden 503. */
    var fallarProximos = 0

    /** Responde 503 a los pedidos (`"PUT /api/..."`) para los que devuelve true. */
    var fallarSi: (String) -> Boolean = { false }

    /** El próximo PUT de audio responde 409 diciendo que tiene estos bytes (sin guardar nada). */
    var conflictoFalso: Long? = null

    /** El próximo PUT de audio guarda sólo la mitad del cuerpo y responde 409 (corte a mitad). */
    var guardarMitadProximoPut = false

    /** Al finalizar, devuelve hash_distinto una vez (y vacía el audio si [resetearEnHashDistinto]). */
    var hashDistintoUnaVez = false
    var resetearEnHashDistinto = true

    /** Al finalizar, olvida los eventos una vez y responde faltan_eventos. */
    var olvidarEventosUnaVez = false

    /** Al finalizar, olvida sólo este evento una vez. */
    var olvidarEvento: String? = null

    /** El tope del audio de una sesión importada (como los 2 GiB del server): 413 al crearla o al pasarlo. */
    var maxAudioImportada: Long? = null

    /** false = un server de antes de Ambos: ignora `pista` (la pista del celu se pegaría al principal) y el bloque `celu`. */
    var separaPistas = true

    /** El `GET /audio?pista=celu` repite `"pista": "celu"` (el contrato no lo pide). */
    var ecoPista = false

    /** Al finalizar, la pista del celu da hash_distinto una vez (y se vacía, como el server). */
    var hashCeluDistintoUnaVez = false

    /** Al finalizar, el server "pierde" la mitad de la pista del celu una vez y responde faltan_bytes. */
    var perderCeluUnaVez = false

    /** Lo mismo, cada vez: la pista del celu nunca cierra. */
    var perderCeluSiempre = false

    /** Si no es null, todo pedido con `?pista=celu` responde este código (un server que la rechaza). */
    var rechazarPista: Int? = null

    @Synchronized
    override fun dispatch(request: RecordedRequest): MockResponse {
        val ruta = request.requestUrl!!.encodedPath
        pedidos += "${request.method} ${request.path}"
        if (request.getHeader("Authorization") != "Bearer $token") return json(401, """{"detail":"token"}""")
        if (fallarProximos > 0 || fallarSi("${request.method} ${request.path}")) {
            if (fallarProximos > 0) fallarProximos--
            request.body.clear()
            return json(503, """{"detail":"falla simulada"}""")
        }
        val partes = ruta.trim('/').split('/')
        return when {
            request.method == "POST" && ruta == "/api/sesiones" -> crear(request)
            request.method == "POST" && partes.size == 4 && partes[0] == "api" && partes[3] == "adjuntos" -> adjunto(partes[2], request)
            partes.size >= 3 && partes[0] == "api" && partes[1] == "sesiones" -> {
                val s = sesiones[partes[2]] ?: return json(404, """{"detail":"no existe"}""")
                // Un server viejo ignora `pista`: todo va al audio principal.
                val pista = request.requestUrl!!.queryParameter("pista")?.takeIf { separaPistas }
                rechazarPista?.let { codigo ->
                    if (pista != null) {
                        request.body.clear()
                        return json(codigo, """{"detail":"pista rechazada"}""")
                    }
                }
                when {
                    partes.size == 4 && partes[3] == "audio" && request.method == "GET" -> {
                        val eco = if (ecoPista && pista != null) """, "pista": "$pista"""" else ""
                        json(200, """{"bytes_recibidos": ${s.pista(pista).size()}$eco}""")
                    }
                    partes.size == 4 && partes[3] == "audio" && request.method == "PUT" -> audio(s, request, pista)
                    partes.size == 4 && partes[3] == "eventos" && request.method == "GET" -> {
                        val ids = s.eventos.keys.joinToString(",") { "\"$it\"" }
                        json(200, """{"ids": [$ids]}""")
                    }
                    partes.size == 4 && partes[3] == "eventos" -> evento(s, request)
                    partes.size == 4 && partes[3] == "finalizar" -> finalizar(s, request)
                    else -> json(404, "{}")
                }
            }
            else -> json(404, "{}")
        }
    }

    /** Responde 422 (formato FastAPI) a este evento. */
    var rechazarEvento: String? = null

    /** Responde 400 a las sesiones con materia (como un server con otra config de materias). */
    var rechazarMaterias = false

    /** Responde 422 (formato FastAPI) a las sesiones que traen `numero_clase` (un server que no lo acepta). */
    var rechazarNumero = false

    /** Todos los cuerpos de `POST /api/sesiones` que llegaron a procesarse, aceptados o rechazados, en orden. */
    val cuerposCrear = mutableListOf<String>()

    private fun crear(request: RecordedRequest): MockResponse {
        val cuerpo = request.body.readUtf8()
        cuerposCrear += cuerpo
        if (rechazarNumero && cuerpo.contains("\"numero_clase\"")) {
            return json(422, """{"detail":[{"loc":["body","numero_clase"],"msg":"numero_clase no permitido","type":"value_error"}]}""")
        }
        if (rechazarMaterias && !cuerpo.contains("\"materia_id\":null")) {
            return json(400, """{"detail":"materia_id desconocida"}""")
        }
        val id = Regex("\"id\":\"([^\"]+)\"").find(cuerpo)!!.groupValues[1]
        val declarado = Regex("\"audio_bytes\":(\\d+)").find(cuerpo)?.groupValues?.get(1)?.toLong()
        maxAudioImportada?.let { max ->
            if (cuerpo.contains("\"importada\":true") && declarado != null && declarado > max) {
                return json(413, """{"detail":"el audio pesa más de 2 GB: $declarado bytes"}""")
            }
        }
        val nueva = id !in sesiones
        sesiones.getOrPut(id) { Sesion() }.cuerpoCrear = cuerpo
        return json(if (nueva) 201 else 200, """{"id":"$id","estado":"grabando"}""")
    }

    private fun Sesion.pista(pista: String?) = if (pista == "celu") audioCelu else audio

    private fun audio(s: Sesion, request: RecordedRequest, pista: String?): MockResponse {
        val offset = request.requestUrl!!.queryParameter("offset")!!.toLong()
        val bytes = request.body.readByteArray()
        if (pista != null) {
            val celu = s.audioCelu
            if (s.finalizada || offset != celu.size().toLong()) return json(409, """{"bytes_recibidos": ${celu.size()}}""")
            celu.write(bytes)
            s.tramosCelu += bytes.size
            return json(200, """{"bytes_recibidos": ${celu.size()}}""")
        }
        conflictoFalso?.let {
            conflictoFalso = null
            return json(409, """{"bytes_recibidos": $it}""")
        }
        if (s.finalizada || offset != s.audio.size().toLong()) {
            return json(409, """{"bytes_recibidos": ${s.audio.size()}}""")
        }
        maxAudioImportada?.let { max ->
            if (s.importada && offset + bytes.size > max) return json(413, """{"detail":"el audio pesa más de 2 GB"}""")
        }
        if (guardarMitadProximoPut) {
            guardarMitadProximoPut = false
            s.audio.write(bytes, 0, bytes.size / 2)
            return json(409, """{"bytes_recibidos": ${s.audio.size()}}""")
        }
        s.audio.write(bytes)
        s.tramos += bytes.size
        return json(200, """{"bytes_recibidos": ${s.audio.size()}}""")
    }

    private fun evento(s: Sesion, request: RecordedRequest): MockResponse {
        val cuerpo = request.body.readByteArray()
        val texto = String(cuerpo, Charsets.ISO_8859_1)
        val id = campo(texto, "id")!!
        val tipo = campo(texto, "tipo")!!
        if (id == rechazarEvento) {
            return json(422, """{"detail":[{"loc":["body","archivo"],"msg":"falta el archivo de la foto","type":"missing"}]}""")
        }
        s.offsets[id] = campo(texto, "offset_ms")
        if (tipo == "foto") {
            val inicio = texto.indexOf("name=\"archivo\"")
            require(inicio >= 0) { "foto sin archivo" }
            val cabecera = Regex("name=\"archivo\"; filename=\"([^\"]*)\"\r\n((?:[^\r\n]+\r\n)*)").find(texto, maxOf(0, inicio - 40))
            s.archivosFotos[id] = (cabecera?.groupValues?.get(1) ?: "") to
                cabecera?.let { Regex("Content-Type: ([^\r\n]+)", RegexOption.IGNORE_CASE).find(it.groupValues[2])?.groupValues?.get(1) }
            val datos = texto.indexOf("\r\n\r\n", inicio) + 4
            val fin = texto.indexOf("\r\n--", datos)
            s.fotos[id] = fin - datos
        }
        val nuevo = id !in s.eventos
        s.eventos[id] = tipo
        return json(if (nuevo) 201 else 200, """{"id":"$id","nuevo":$nuevo}""")
    }

    private fun finalizar(s: Sesion, request: RecordedRequest): MockResponse {
        val cuerpo = request.body.readUtf8()
        s.cuerpoFinalizar = cuerpo
        val sha = Regex("\"sha256\":\"([0-9a-f]+)\"").find(cuerpo)!!.groupValues[1]
        val bytes = Regex("\"bytes_totales\":(\\d+)").find(cuerpo)!!.groupValues[1].toLong()
        val eventos = Regex("\"eventos_totales\":(\\d+)").find(cuerpo)!!.groupValues[1].toInt()
        if (olvidarEventosUnaVez) {
            olvidarEventosUnaVez = false
            s.eventos.clear()
        }
        olvidarEvento?.let {
            olvidarEvento = null
            s.eventos.remove(it)
        }
        // El bloque de la pista del celu (Ambos): un server viejo lo ignora.
        val celu = Regex("\"celu\":\\{([^}]*)\\}").find(cuerpo)?.groupValues?.get(1)?.takeIf { separaPistas }
        if (bytes != s.audio.size().toLong()) return json(409, """{"motivo":"faltan_bytes"}""")
        if (celu != null) {
            if (perderCeluUnaVez || perderCeluSiempre) {
                perderCeluUnaVez = false
                val antes = s.audioCelu.toByteArray()
                s.audioCelu.reset()
                s.audioCelu.write(antes, 0, antes.size / 2)
            }
            val bytesCelu = Regex("\"bytes_totales\":(\\d+)").find(celu)!!.groupValues[1].toLong()
            if (bytesCelu != s.audioCelu.size().toLong()) {
                return json(409, """{"motivo":"faltan_bytes","bytes_recibidos":${s.audioCelu.size()},"pista":"celu"}""")
            }
        }
        if (eventos != s.eventos.size) return json(409, """{"motivo":"faltan_eventos"}""")
        if (hashDistintoUnaVez || sha != sha256(s.audio.toByteArray())) {
            hashDistintoUnaVez = false
            if (resetearEnHashDistinto) s.audio.reset()
            return json(409, """{"motivo":"hash_distinto"}""")
        }
        if (celu != null) {
            val shaCelu = Regex("\"sha256\":\"([0-9a-f]+)\"").find(celu)!!.groupValues[1]
            if (hashCeluDistintoUnaVez || shaCelu != sha256(s.audioCelu.toByteArray())) {
                hashCeluDistintoUnaVez = false
                s.audioCelu.reset()
                return json(409, """{"motivo":"hash_distinto","bytes_recibidos":0,"pista":"celu"}""")
            }
        }
        s.finalizada = true
        return json(200, """{"estado":"en_cola"}""")
    }

    // ---- Adjuntos ----

    /** Un adjunto como llegó: los campos del multipart y el tamaño del archivo. */
    data class AdjuntoRecibido(
        val id: String,
        val nombreOriginal: String?,
        val tipo: String?,
        val bytes: Int,
        val offsetMs: String?,
        val creado: String?,
    )

    /** Adjuntos recibidos por sesión, en orden de llegada. */
    val adjuntos = mutableMapOf<String, LinkedHashMap<String, AdjuntoRecibido>>()

    /** Sesiones que el server tiene sin que las haya subido este celu (por ejemplo, grabadas en otro). */
    val sesionesSoloServidor = mutableSetOf<String>()

    /**
     * Si devuelve algo, se responde eso al adjunto con ese id sin guardarlo (404, 405, 409, 413, 415, 422…).
     * `conArchivo` es false en la consulta de unos bytes que se hace antes de mandar el archivo.
     */
    var responderAdjunto: (id: String, conArchivo: Boolean) -> MockResponse? = { _, _ -> null }

    /** Las próximas N veces guarda el adjunto pero responde 503: la respuesta "se pierde". */
    var perderRespuestasAdjunto = 0

    /** El `estado` y el `adjuntos_sin_usar` que informa al recibir un adjunto. */
    var estadoAdjunto = "pendiente"
    var adjuntosSinUsar = 0

    /** Los pedidos de adjuntos que trajeron el archivo (los demás son consultas sin el archivo). */
    var archivosRecibidos = 0

    private fun adjunto(sesionId: String, request: RecordedRequest): MockResponse {
        val texto = String(request.body.readByteArray(), Charsets.ISO_8859_1)
        val id = campo(texto, "id")!!
        val cabecera = Regex("name=\"archivo\"; filename=\"([^\"]*)\"\r\n((?:[^\r\n]+\r\n)*)\r\n").find(texto)
        if (cabecera != null) archivosRecibidos++
        responderAdjunto(id, cabecera != null)?.let { return it }
        if (sesionId !in sesiones && sesionId !in sesionesSoloServidor) return json(404, """{"detail":"sesión no encontrada"}""")
        if (cabecera == null) {
            // La consulta sin el archivo, como el server real: 200 si ya tiene ese id, 422 si no.
            if (adjuntos[sesionId]?.containsKey(id) != true) return json(422, """{"detail":"falta el archivo (campo archivo)"}""")
            return respuestaAdjunto(sesionId, id, nuevo = false)
        }
        val datos = cabecera.range.last + 1
        // El archivo es la última parte: termina donde empieza el cierre del multipart.
        val fin = texto.lastIndexOf("\r\n--")
        val recibido = AdjuntoRecibido(
            id = id,
            // Los encabezados del multipart van en UTF-8.
            nombreOriginal = String(cabecera.groupValues[1].toByteArray(Charsets.ISO_8859_1), Charsets.UTF_8),
            tipo = Regex("Content-Type: ([^\r\n]+)", RegexOption.IGNORE_CASE).find(cabecera.groupValues[2])?.groupValues?.get(1),
            bytes = fin - datos,
            offsetMs = campo(texto, "offset_ms"),
            creado = campo(texto, "creado"),
        )
        val deLaSesion = adjuntos.getOrPut(sesionId) { linkedMapOf() }
        val nuevo = id !in deLaSesion
        if (nuevo) deLaSesion[id] = recibido
        if (perderRespuestasAdjunto > 0) {
            perderRespuestasAdjunto--
            return json(503, """{"detail":"respuesta perdida"}""")
        }
        return respuestaAdjunto(sesionId, id, nuevo)
    }

    private fun respuestaAdjunto(sesionId: String, id: String, nuevo: Boolean): MockResponse {
        val deLaSesion = adjuntos.getValue(sesionId)
        val objeto = { a: AdjuntoRecibido ->
            buildJsonObject {
                put("id", a.id)
                put("nombre", "adjunto-${a.id.take(4)}.pdf")
                put("nombre_original", a.nombreOriginal)
                put("bytes", a.bytes)
                put("offset_ms", a.offsetMs?.toLong())
                put("creado", a.creado)
                put("estado", estadoAdjunto)
            }
        }
        val respuesta = buildJsonObject {
            objeto(deLaSesion.getValue(id)).forEach { (k, v) -> put(k, v) }
            put(
                "sesion",
                buildJsonObject {
                    put("id", sesionId)
                    put("estado", if (sesiones[sesionId]?.finalizada == true) "en_cola" else "grabando")
                    put("adjuntos", JsonArray(deLaSesion.values.map(objeto)))
                    put("adjuntos_sin_usar", adjuntosSinUsar)
                },
            )
        }
        return json(if (nuevo) 201 else 200, respuesta.toString())
    }

    private fun campo(multipart: String, nombre: String): String? =
        Regex("name=\"$nombre\"\r\n(?:[^\r\n]+\r\n)*\r\n([^\r]*)\r\n").find(multipart)?.groupValues?.get(1)

    private fun json(codigo: Int, cuerpo: String) =
        MockResponse().setResponseCode(codigo).setHeader("Content-Type", "application/json").setBody(cuerpo)

    companion object {
        fun sha256(b: ByteArray) = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }
    }
}
