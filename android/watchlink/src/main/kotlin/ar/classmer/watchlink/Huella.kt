package ar.classmer.watchlink

import java.io.File
import java.security.MessageDigest
import java.security.SecureRandom

/** SHA-256 en hex: la verificación final del audio (la misma huella que va al `/finalizar` del server). */
object Huella {
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

/**
 * Id de una grabación que empieza en el reloj: UUID versión 7 (RFC 9562), como los de las sesiones
 * del celu (los primeros 48 bits son la hora, así quedan ordenados por fecha). El celu usa ese
 * mismo id para la sesión y el server también.
 */
object IdGrabacion {
    private val random = SecureRandom()

    fun nuevo(ahoraMs: Long = System.currentTimeMillis()): String {
        val bytes = ByteArray(16)
        random.nextBytes(bytes)
        for (i in 0 until 6) {
            bytes[i] = (ahoraMs ushr (8 * (5 - i))).toByte()
        }
        bytes[6] = ((bytes[6].toInt() and 0x0F) or 0x70).toByte()
        bytes[8] = ((bytes[8].toInt() and 0x3F) or 0x80).toByte()
        val hex = bytes.joinToString("") { "%02x".format(it) }
        return "${hex.substring(0, 8)}-${hex.substring(8, 12)}-${hex.substring(12, 16)}-" +
            "${hex.substring(16, 20)}-${hex.substring(20)}"
    }

    private val FORMA = Regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    /** Un id que llegó por el Data Layer se usa como nombre de carpeta: sólo se aceptan UUID. */
    fun valido(id: String?): Boolean = id != null && FORMA.matches(id)
}
