package ar.classmer.ui

import ar.classmer.datos.AlmacenAdjuntos
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.AdjuntoLocal
import ar.classmer.modelo.AdjuntoServidor
import ar.classmer.modelo.EstadoAdjunto
import ar.classmer.modelo.EstadoSesionServidor
import ar.classmer.modelo.EstadoSubidaAdjuntos
import ar.classmer.modelo.SoporteAdjuntos
import ar.classmer.modelo.SubidaAdjunto
import ar.classmer.subida.SubidorAdjuntos
import ar.classmer.util.Tiempo
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/** Los adjuntos de una clase, para su fila en Inicio y para la hoja de adjuntos. */
data class ResumenAdjuntos(
    /** Los del celu más los que informa el server, sin repetir. */
    val total: Int = 0,
    /** Del celu y todavía sin subir (esperando la clase, pendientes, o con un server sin soporte). */
    val sinSubir: Int = 0,
    val rechazados: Int = 0,
    /** El server todavía no acepta adjuntos (404 "Not Found" o 405) y hay alguno sin subir. */
    val sinSoporte: Boolean = false,
    /** La clase todavía no llegó al server: los adjuntos esperan. */
    val esperandoClase: Boolean = false,
    /** El server no encuentra la clase hace rato (por ejemplo, se borró desde el panel) y hay alguno sin subir. */
    val claseNoEncontrada: Boolean = false,
    /** Lo que informa el server de los adjuntos de la clase. */
    val servidor: List<AdjuntoServidor> = emptyList(),
    val sinUsar: Int = 0,
    /** El estado de la clase en el server (`lista`, `error`…), si se sabe. */
    val estadoClase: String? = null,
) {
    /** Hay adjuntos que el apunte vigente no usa y la clase se puede regenerar (está lista o en error). */
    val regenerable: Boolean get() = sinUsar > 0 && estadoClase in ESTADOS_REGENERABLES

    /** Una línea para la fila de la clase, o `null` si no hay nada que avisar. */
    val texto: String?
        get() = when {
            total == 0 -> null
            sinSoporte -> "El server todavía no acepta adjuntos: quedan guardados en el celu"
            rechazados > 0 -> plural(rechazados, "adjunto rechazado", "adjuntos rechazados") + " por el server"
            claseNoEncontrada -> "El server no encuentra esta clase: " + plural(sinSubir, "adjunto", "adjuntos") + " sin subir"
            esperandoClase && sinSubir > 0 -> plural(sinSubir, "adjunto espera", "adjuntos esperan") + " que la clase llegue al server"
            sinSubir > 0 -> plural(sinSubir, "adjunto", "adjuntos") + " sin subir"
            else -> null
        }

    /** Hay algo que no anda (rechazos, un server sin soporte, una clase que el server no tiene): se muestra en rojo. */
    val conProblemas: Boolean get() = sinSoporte || rechazados > 0 || claseNoEncontrada

    val etiquetaBoton: String get() = if (total == 0) "📎 Adjuntos" else "📎 Adjuntos ($total)"

    companion object {
        val ESTADOS_REGENERABLES = setOf("lista", "error")

        /**
         * @param remota lo último que informó el server de la clase; si no hay, lo que devolvió al subir
         *   el último adjunto.
         * @param soporte si el server acepta adjuntos (`adjuntos/soporte.json`).
         */
        fun de(
            locales: List<AdjuntoLocal>,
            subida: EstadoSubidaAdjuntos,
            remota: EstadoSesionServidor?,
            claseEnServidor: Boolean,
            soporte: SoporteAdjuntos = SoporteAdjuntos(),
        ): ResumenAdjuntos {
            val fuente = remota ?: subida.sesion
            val enServidor = fuente?.adjuntos.orEmpty()
            val idsServidor = enServidor.map { it.id }.toSet()
            val sinSubirLocal = locales.filter { a -> !subida.de(a.id).subido && a.id !in idsServidor }
            val rechazados = sinSubirLocal.count { subida.de(it.id).rechazo != null }
            val sinSubir = sinSubirLocal.size - rechazados
            return ResumenAdjuntos(
                total = (locales.map { it.id } + idsServidor).toSet().size,
                sinSubir = sinSubir,
                rechazados = rechazados,
                sinSoporte = soporte.sinSoporte && sinSubir > 0,
                esperandoClase = !claseEnServidor && sinSubir > 0,
                claseNoEncontrada = claseEnServidor && subida.claseNoEncontrada && sinSubir > 0,
                servidor = enServidor,
                sinUsar = fuente?.adjuntosSinUsar ?: 0,
                estadoClase = fuente?.estado,
            )
        }

        /** Lee lo que hay en el celu y le suma lo que informa el server. Hace I/O: fuera del hilo principal. */
        fun completar(fila: FilaSesion, adjuntos: AlmacenAdjuntos, sesiones: AlmacenSesiones, remota: EstadoSesionServidor?): FilaSesion {
            val locales = adjuntos.listar(fila.id)
            val subida = adjuntos.leerSubida(fila.id)
            val enServidor = locales.isEmpty() || SubidorAdjuntos.claseEnServidor(sesiones, fila.id)
            val soporte = if (locales.isEmpty()) SoporteAdjuntos() else adjuntos.leerSoporte()
            return fila.copy(adjuntos = de(locales, subida, remota, enServidor, soporte))
        }

        fun plural(n: Int, uno: String, varios: String) = if (n == 1) "1 $uno" else "$n $varios"

        private val formatoDia = DateTimeFormatter.ofPattern("EEE d/M", Locale.forLanguageTag("es-AR"))

        /** "mié 22/10": el día en que se creó una clase, sacado de su id (UUIDv7: los primeros 48 bits son la hora). */
        fun diaDeId(id: String): String? = runCatching {
            val ms = id.replace("-", "").take(12).toLong(16)
            Instant.ofEpochMilli(ms).atZone(ZoneId.systemDefault()).format(formatoDia)
        }.getOrNull()
    }
}

/** Un adjunto como se muestra en la hoja. */
data class VistaAdjunto(
    val id: String,
    val nombre: String,
    val esPdf: Boolean,
    /** Tipo, tamaño y minuto: "PDF · 2,3 MB · en 34:52". */
    val detalle: String,
    val estado: String,
    val explicacion: String? = null,
    val tono: Tono = Tono.NORMAL,
    /** Se está copiando o mandando ahora (con ruedita). */
    val enCurso: Boolean = false,
    val reintentable: Boolean = false,
    /** Está en el celu y todavía no se subió: se puede borrar con "Descartar". */
    val descartable: Boolean = false,
)

object VistasAdjuntos {

    /**
     * Los del celu en orden de llegada y después los que sólo están en el server (por ejemplo, subidos desde la PC).
     *
     * @param subiendo el adjunto cuyo archivo se está mandando ahora, si hay uno.
     */
    fun lista(
        locales: List<AdjuntoLocal>,
        subida: EstadoSubidaAdjuntos,
        servidor: List<AdjuntoServidor>,
        claseEnServidor: Boolean,
        pedidosReintento: Set<String> = emptySet(),
        soporte: SoporteAdjuntos = SoporteAdjuntos(),
        subiendo: String? = null,
    ): List<VistaAdjunto> {
        val porId = servidor.associateBy { it.id }
        val idsLocales = locales.map { it.id }.toSet()
        return locales.map {
            de(it, subida.de(it.id), porId[it.id], claseEnServidor, it.id in pedidosReintento, subida, soporte.sinSoporte, it.id == subiendo)
        } + servidor.filter { it.id !in idsLocales }.map(::soloServidor)
    }

    /**
     * @param clase el estado de subida de toda la clase (si el server la encuentra).
     * @param sinSoporte el server todavía no acepta adjuntos.
     * @param subiendo su archivo se está mandando ahora.
     */
    fun de(
        a: AdjuntoLocal,
        subida: SubidaAdjunto,
        servidor: AdjuntoServidor?,
        claseEnServidor: Boolean,
        pedidoReintento: Boolean = false,
        clase: EstadoSubidaAdjuntos = EstadoSubidaAdjuntos(),
        sinSoporte: Boolean = false,
        subiendo: Boolean = false,
    ): VistaAdjunto {
        val base = VistaAdjunto(
            id = a.id,
            nombre = a.nombreOriginal,
            esPdf = a.esPdf,
            detalle = detalle(a.esPdf, a.bytes, a.offsetMs),
            estado = "",
        )
        val pendiente = base.copy(estado = "Pendiente de subir", descartable = true)
        return when {
            servidor != null || subida.subido -> subido(base, servidor?.estado ?: subida.servidor?.estado)
            subiendo -> base.copy(estado = "Subiendo…", enCurso = true)
            pedidoReintento -> base.copy(estado = "Reintentando", enCurso = true)
            subida.rechazo != null ->
                base.copy(estado = "Rechazado", explicacion = subida.rechazo, tono = Tono.ERROR, reintentable = true, descartable = true)
            sinSoporte -> pendiente.copy(
                estado = "Server sin soporte",
                explicacion = "El server todavía no acepta adjuntos (falta actualizarlo): queda guardado en el celu y se sube solo cuando lo acepte.",
                tono = Tono.ERROR,
                reintentable = true,
            )
            !claseEnServidor -> pendiente.copy(estado = "Esperando que la clase llegue al server")
            clase.claseNoEncontrada -> pendiente.copy(
                estado = "La clase no está en el server",
                explicacion = "El server no la encuentra (¿se borró desde el panel?). Se le vuelve a preguntar cada hora; " +
                    "podés reintentar ahora o descartarlo.",
                tono = Tono.ERROR,
                reintentable = true,
            )
            clase.noEncontrada > 0 -> pendiente.copy(explicacion = "${clase.ultimoError ?: "El server no encuentra esta clase"}: se reintenta", tono = Tono.ERROR)
            subida.ultimoError != null -> pendiente.copy(explicacion = subida.ultimoError, tono = Tono.ERROR, reintentable = subida.fallos > 0)
            else -> pendiente
        }
    }

    /** Un adjunto que el server tiene y el celu no (subido desde el panel de la PC o desde otro celu). */
    fun soloServidor(a: AdjuntoServidor): VistaAdjunto {
        val esPdf = a.tipo == "pdf" || a.nombre?.endsWith(".pdf", ignoreCase = true) == true
        val base = VistaAdjunto(
            id = a.id,
            nombre = a.nombreOriginal ?: a.nombre ?: "Adjunto",
            esPdf = esPdf,
            detalle = detalle(esPdf, a.bytes, a.offsetMs) + " · no está en el celu",
            estado = "",
        )
        return subido(base, a.estado)
    }

    /** "Copiando…": el archivo elegido todavía se está copiando al celu. */
    fun copiando(clave: String, nombre: String) =
        VistaAdjunto(id = clave, nombre = nombre, esPdf = nombre.endsWith(".pdf", ignoreCase = true), detalle = "", estado = "Copiando…", enCurso = true)

    private fun subido(base: VistaAdjunto, estadoServidor: String?): VistaAdjunto = when (estadoServidor) {
        EstadoAdjunto.PENDIENTE -> base.copy(estado = "Subido", explicacion = "Lo va a usar el próximo apunte", tono = Tono.OK)
        EstadoAdjunto.USADO -> base.copy(estado = "Subido", explicacion = "Lo usó el apunte vigente", tono = Tono.OK)
        EstadoAdjunto.SIN_USAR -> base.copy(estado = "Subido", explicacion = "El apunte vigente no lo usa", tono = Tono.ACTIVO)
        else -> base.copy(estado = "Subido", tono = Tono.OK)
    }

    fun detalle(esPdf: Boolean, bytes: Long?, offsetMs: Long?): String =
        listOfNotNull(
            if (esPdf) "PDF" else "Imagen",
            bytes?.let(::tamanio),
            offsetMs?.let { "en ${Tiempo.duracion(it)}" },
        ).joinToString(" · ")

    /** "850 KB", "2,3 MB". */
    fun tamanio(bytes: Long): String = when {
        bytes < 1024 * 1024 -> "${(bytes + 1023) / 1024} KB"
        else -> "%.1f MB".format(Locale.forLanguageTag("es-AR"), bytes / 1_048_576.0)
    }
}
