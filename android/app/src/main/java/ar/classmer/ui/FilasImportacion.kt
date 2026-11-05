package ar.classmer.ui

import ar.classmer.importacion.Importador
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Importacion
import ar.classmer.modelo.SesionLocal

/**
 * Cómo se ve en "Clases recientes" una clase importada: "Copiando… 40 %" mientras se copia al celu,
 * "Subiendo 23 %" mientras sube y después los estados del server, como cualquier otra.
 */
object FilasImportacion {
    const val NO_SE_PUDO = "No se pudo importar"

    /**
     * Una importada que todavía se está copiando, o cuya copia falló (con "Reintentar" y "Descartar").
     * `null` si no es una importada en `importando`.
     *
     * @param largoParcial lo que ya tiene el `.parcial` del audio.
     */
    fun copiando(base: FilaSesion, s: SesionLocal, importacion: Importacion?, largoParcial: Long): FilaSesion? {
        if (!s.importada || s.estadoLocal != EstadoLocal.IMPORTANDO) return null
        val fila = base.copy(importada = true)
        if (importacion == null) {
            return fila.copy(
                estado = NO_SE_PUDO,
                detalle = "Se perdieron los datos de la copia (importacion.json). Descartala e importala de nuevo.",
                tono = Tono.ERROR,
                importacionFallida = true,
            )
        }
        importacion.error?.let { return fila.copy(estado = NO_SE_PUDO, detalle = it, tono = Tono.ERROR, importacionFallida = true) }
        val avance = Importador.progreso(importacion, largoParcial)
        return fila.copy(
            estado = "Copiando…" + (avance?.let { " ${porcentaje(it)} %" } ?: ""),
            detalle = "Se copia al celu y después se sube.",
            progreso = avance,
        )
    }

    /** El estado mientras sube: bytes confirmados por el server sobre el total. */
    fun subiendo(avance: Float): String = "Subiendo ${porcentaje(avance)} %"

    /** Las fotos que quedaron afuera de la importación, para el detalle de la fila. */
    fun detalle(importacion: Importacion?): String? {
        val problemas = importacion?.problemasFotos.orEmpty()
        return when (problemas.size) {
            0 -> null
            1 -> problemas[0]
            else -> "${problemas.size} fotos no se pudieron importar."
        }
    }

    private fun porcentaje(avance: Float) = (avance.coerceIn(0f, 1f) * 100).toInt()
}
