package ar.classmer.ui

import ar.classmer.modelo.Espera
import ar.classmer.modelo.EstadoSesionServidor

/** Cómo se muestra en la lista de clases recientes lo que informa el server de una sesión. */
object FilasSesion {

    fun aplicarEstadoRemoto(base: FilaSesion, r: EstadoSesionServidor): FilaSesion {
        // El número que vale es el del server (se puede cambiar desde el panel o al elegir la materia).
        val conNumero = base.copy(numeroClase = r.numeroClase ?: base.numeroClase)
        return when (r.estado) {
            "en_cola" -> conNumero.copy(estado = "En cola")
            "procesando" -> conNumero.copy(
                estado = "Procesando" + (r.etapa?.let { " · ${it.replace('_', ' ')}" } ?: ""),
                progreso = r.progreso?.toFloat()?.coerceIn(0f, 1f),
            )
            "lista" -> conNumero.copy(estado = "Lista", tono = Tono.OK, pdf = r.pdf ?: "/api/sesiones/${r.id}/apunte.pdf")
            "error" -> conNumero.copy(estado = "Error", detalle = r.textoError ?: conNumero.detalle, tono = Tono.ERROR)
            // El apunte espera que el usuario revise las dudas del transcript en el panel (§15): acá sólo se avisa.
            // Sin `espera` (un server viejo) o con `materia`, es la espera de siempre.
            "esperando_usuario" -> if (r.espera == Espera.REVISION) {
                conNumero.copy(
                    estado = "Revisá el transcript",
                    detalle = listOfNotNull(textoDudas(r.dudas), base.detalle?.takeIf { it != r.titulo }).joinToString("\n"),
                    tono = Tono.ACTIVO,
                    revisarEnPanel = true,
                )
            } else {
                conNumero.copy(
                    estado = "Elegí la materia",
                    detalle = listOfNotNull(motivoEsperando(r.textoError), base.detalle?.takeIf { it != r.titulo })
                        .joinToString("\n"),
                    tono = Tono.ACTIVO,
                    elegirMateria = true,
                    materiaSugerida = materiaSugerida(r.textoError),
                )
            }
            "grabando" -> conNumero.copy(estado = "Recibiendo audio")
            else -> conNumero.copy(estado = r.estado ?: "Subida", tono = Tono.OK)
        }
    }

    /**
     * El motivo que da el server, sin las instrucciones para la PC ("Asignala con: uv run classmer
     * asignar…") ni el id de la materia sugerida, que se muestra en el selector.
     */
    fun motivoEsperando(error: String?): String {
        val limpio = error.orEmpty()
            .replace(INSTRUCCIONES_PC, "")
            .replace(SUGERIDA, "")
            .trim()
        return limpio.ifEmpty { "El server no pudo saber de qué materia es." }
    }

    /** El id de la materia que sugiere el server ("… Sugerida: macro. …"), si la hay. */
    fun materiaSugerida(error: String?): String? = error?.let { SUGERIDA.find(it)?.groupValues?.get(1) }

    fun textoNumero(numero: Int?): String? = numero?.takeIf { it > 0 }?.let { "Clase $it" }

    /** "Hay 3 dudas para revisar en el panel"; sin el número, que el transcript espera la revisión. */
    fun textoDudas(dudas: Int?): String = when {
        dudas == null || dudas <= 0 -> "El transcript espera tu revisión en el panel."
        dudas == 1 -> "Hay 1 duda para revisar en el panel."
        else -> "Hay $dudas dudas para revisar en el panel."
    }

    /**
     * "Clases recientes": las [max] de inicio más reciente, más las del celu que todavía necesitan algo
     * aunque sean de antes ([siempre]). Con Importar, la última clase creada ya no es siempre la de inicio
     * más reciente: una clase de hace semanas que se está copiando, que falló (con Reintentar y Descartar)
     * o que espera la materia no puede quedar afuera, porque su fila es la única forma de manejarla.
     */
    fun recientes(filas: List<FilaSesion>, max: Int, siempre: Set<String>): List<FilaSesion> {
        val orden = filas.sortedWith(compareByDescending<FilaSesion> { it.inicioMs }.thenByDescending { it.id })
        val primeras = orden.take(max).map { it.id }.toSet()
        return orden.filter { it.id in primeras || it.id in siempre }
    }

    private val INSTRUCCIONES_PC = Regex("""\s*(?:Asignala|Agregalas y asigná) con:.*$""", RegexOption.DOT_MATCHES_ALL)
    private val SUGERIDA = Regex("""\s*Sugerida: ([\w-]+)\.""")
}
