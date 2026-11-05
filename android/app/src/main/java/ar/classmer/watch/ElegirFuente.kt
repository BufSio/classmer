package ar.classmer.watch

import ar.classmer.modelo.FuenteAudio
import ar.classmer.watchlink.VERSION_PROTOCOLO

/**
 * Qué pasa al tocar Grabar según lo que eligió el usuario (Teléfono / Reloj / Ambos) y lo que se sabe del
 * reloj. Sólo decide: Inicio lo muestra debajo del selector.
 */
object ElegirFuente {
    enum class Nivel { OK, AVISO, PROBLEMA }

    data class Decision(
        /** [FuenteAudio] con la que se graba al tocar Grabar. */
        val fuente: String,
        /** Una línea sobre el reloj (`null` si se graba con el teléfono). */
        val texto: String? = null,
        val nivel: Nivel = Nivel.OK,
        /** Conviene ofrecer grabar con el teléfono: el reloj no está, no responde o tiene poca batería. */
        val ofrecerTelefono: Boolean = false,
        /** El reloj ya está grabando una clase (se empezó allá): Grabar se engancha a esa. */
        val grabacionEnCurso: String? = null,
    )

    /** Un estado más viejo que esto no dice si el reloj sigue ahí. */
    const val ESTADO_VIEJO_MS = 2 * 60_000L

    /**
     * @param celuYaGrabo la clase que el reloj ya está grabando tiene su pista del celu (la app se cayó y se
     *   la vuelve a seguir): una clase lleva una sola, así que el celu no graba otra.
     */
    fun decidir(preferida: String, info: InfoWatch, ahoraCelu: Long, celuYaGrabo: Boolean = false): Decision = when (preferida) {
        FuenteAudio.RELOJ -> decidirReloj(info, ahoraCelu)
        FuenteAudio.AMBOS -> conCelu(decidirReloj(info, ahoraCelu), celuYaGrabo)
        else -> Decision(FuenteAudio.TELEFONO)
    }

    /**
     * Ambos: el celu graba desde que se toca Grabar, así que un problema del reloj no deja la clase sin audio
     * (es un aviso, no un problema). Igual se ofrece grabar sólo con el teléfono.
     */
    private fun conCelu(d: Decision, celuYaGrabo: Boolean): Decision = d.copy(
        fuente = FuenteAudio.AMBOS,
        texto = d.texto?.let { t ->
            when {
                d.grabacionEnCurso != null && celuYaGrabo -> "$t (el celu ya grabó su audio de esta clase: sigue sólo el reloj)"
                d.grabacionEnCurso != null -> "$t, y el celu graba también"
                d.nivel == Nivel.PROBLEMA -> "$t. Graba el celu y el reloj se suma si aparece"
                else -> t
            }
        },
        nivel = if (d.nivel == Nivel.PROBLEMA) Nivel.AVISO else d.nivel,
    )

    private fun decidirReloj(info: InfoWatch, ahoraCelu: Long): Decision {
        val reloj = FuenteAudio.RELOJ
        val nombre = info.nombre?.let { "⌚ $it" } ?: "⌚ Reloj"
        val estado = info.estado?.takeIf { ahoraCelu - info.estadoEn <= ESTADO_VIEJO_MS }
        val bateria = estado?.bateria
        return when {
            !info.buscado -> Decision(reloj, "⌚ Buscando el reloj…")
            !info.conectado -> Decision(reloj, "⌚ El reloj no está conectado al celu (Bluetooth)", Nivel.PROBLEMA, ofrecerTelefono = true)
            !info.appInstalada -> Decision(reloj, "$nombre: falta instalar Classmer en el reloj", Nivel.PROBLEMA, ofrecerTelefono = true)
            estado == null && info.sinRespuesta ->
                Decision(reloj, "$nombre: Classmer no responde en el reloj", Nivel.PROBLEMA, ofrecerTelefono = true)
            estado == null -> Decision(reloj, "$nombre · conectado")
            estado.protocolo != VERSION_PROTOCOLO ->
                Decision(reloj, "$nombre: actualizá Classmer en el reloj (es de otra versión)", Nivel.PROBLEMA, ofrecerTelefono = true)
            estado.activa != null -> Decision(
                reloj,
                "$nombre ya está grabando una clase: Grabar la sigue en el celu",
                grabacionEnCurso = estado.activa?.id,
            )
            bateria != null && bateria < SesionWatch.BATERIA_BAJA && !estado.cargando -> Decision(
                reloj,
                "$nombre · ⚠ batería $bateria %: puede no alcanzar para toda la clase",
                Nivel.AVISO,
                ofrecerTelefono = true,
            )
            estado.error != null -> Decision(reloj, "$nombre · ⚠ ${estado.error}", Nivel.AVISO)
            else -> Decision(reloj, "$nombre · conectado" + (estado.bateria?.let { " · batería $it %" } ?: ""))
        }
    }
}
