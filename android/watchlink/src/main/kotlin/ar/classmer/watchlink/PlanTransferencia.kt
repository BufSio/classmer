package ar.classmer.watchlink

/**
 * Qué hace el celu con el audio de una grabación del reloj, según lo que tiene él ([largoLocal]) y
 * lo que informa el reloj. Es la misma idea que la subida al server: se pide desde lo que ya se tiene.
 */
object PlanTransferencia {
    sealed interface Paso {
        /** Pedir los bytes desde [desde] (lo que ya tiene el celu). */
        data class Pedir(val desde: Long) : Paso

        /** El reloj terminó y el celu tiene el mismo largo: falta comparar la huella. */
        data object Verificar : Paso

        /**
         * La copia del celu no coincide con la del reloj (más larga que la final, o con otra huella):
         * hay que bajarla entera aparte y reemplazarla.
         */
        data object Rebajar : Paso

        /** Está al día (y el reloj sigue grabando). */
        data object Nada : Paso
    }

    fun decidir(largoLocal: Long, g: GrabacionWatch): Paso = when {
        g.bytes > largoLocal -> Paso.Pedir(largoLocal)
        !g.terminada -> Paso.Nada
        largoLocal == g.bytes && g.sha256 != null -> Paso.Verificar
        largoLocal > g.bytes -> Paso.Rebajar
        // Terminada sin huella todavía (el reloj la está calculando): se espera el próximo estado.
        else -> Paso.Nada
    }

    /** Después de [Paso.Verificar]: el celu tiene el audio completo sólo si la huella coincide. */
    fun verificada(largoLocal: Long, shaLocal: String, g: GrabacionWatch): Boolean =
        g.terminada && largoLocal == g.bytes && g.sha256.equals(shaLocal, ignoreCase = true)
}
