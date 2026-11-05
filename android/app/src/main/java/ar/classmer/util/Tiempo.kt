package ar.classmer.util

import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import java.util.Locale

object Tiempo {
    private val formatoFecha = DateTimeFormatter.ofPattern("EEE d/M HH:mm", Locale.forLanguageTag("es-AR"))
    private val formatoHora = DateTimeFormatter.ofPattern("HH:mm")

    /** Fecha y hora actual en ISO 8601 con zona, por ejemplo `2026-10-15T14:00:05-03:00`. */
    fun ahoraIso(): String =
        OffsetDateTime.now(ZoneId.systemDefault())
            .truncatedTo(ChronoUnit.SECONDS)
            .format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)

    /** Un instante (ms desde 1970) en ISO 8601 con la zona del celu, sin milisegundos. */
    fun iso(epochMs: Long, zona: ZoneId = ZoneId.systemDefault()): String =
        Instant.ofEpochMilli(epochMs).atZone(zona).toOffsetDateTime()
            .truncatedTo(ChronoUnit.SECONDS)
            .format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)

    /** `mar 13/10 14:00` de un instante (ms desde 1970). */
    fun fechaCorta(epochMs: Long, zona: ZoneId = ZoneId.systemDefault()): String =
        Instant.ofEpochMilli(epochMs).atZone(zona).format(formatoFecha)

    /** `14:35`: la hora local de un instante (ms desde 1970). */
    fun hora(epochMs: Long, zona: ZoneId = ZoneId.systemDefault()): String =
        Instant.ofEpochMilli(epochMs).atZone(zona).format(formatoHora)

    /** `1:02:03` o `02:03`. */
    fun duracion(ms: Long): String {
        val totalSeg = (ms.coerceAtLeast(0)) / 1000
        val h = totalSeg / 3600
        val m = (totalSeg % 3600) / 60
        val s = totalSeg % 60
        return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%02d:%02d".format(m, s)
    }

    /** Milisegundos desde 1970 de un ISO 8601 con zona, o 0 si no se puede leer. */
    fun epochMs(iso: String?): Long =
        iso?.let { runCatching { OffsetDateTime.parse(it).toInstant().toEpochMilli() }.getOrNull() } ?: 0L

    /** `mar 13/10 14:00` a partir de un ISO 8601; si no se puede leer, devuelve el texto tal cual. */
    fun fechaCorta(iso: String?): String {
        if (iso == null) return ""
        return runCatching {
            OffsetDateTime.parse(iso).atZoneSameInstant(ZoneId.systemDefault()).format(formatoFecha)
        }.getOrDefault(iso)
    }
}
