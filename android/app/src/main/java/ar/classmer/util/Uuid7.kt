package ar.classmer.util

import java.security.SecureRandom

/**
 * UUID versión 7 (RFC 9562): los primeros 48 bits son la hora en ms, así los ids de sesión
 * quedan ordenados por fecha de creación.
 */
object Uuid7 {
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
}
