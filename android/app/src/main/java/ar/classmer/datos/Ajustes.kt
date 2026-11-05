package ar.classmer.datos

import android.content.Context
import android.os.Build
import androidx.core.content.edit
import ar.classmer.modelo.FuenteAudio
import ar.classmer.modelo.Materia
import ar.classmer.util.JsonClassmer
import kotlinx.serialization.builtins.ListSerializer
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** Ajustes y caches chicos, en SharedPreferences (quedan sólo en el celu). */
class Ajustes(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("ajustes", Context.MODE_PRIVATE)

    var urlServidor: String
        get() = prefs.getString(URL, "") ?: ""
        set(valor) = prefs.edit { putString(URL, normalizarUrl(valor)) }

    var token: String
        get() = prefs.getString(TOKEN, "") ?: ""
        set(valor) = prefs.edit { putString(TOKEN, valor.trim()) }

    var dispositivo: String
        get() = prefs.getString(DISPOSITIVO, null)?.takeIf { it.isNotBlank() } ?: dispositivoPorDefecto()
        set(valor) = prefs.edit { putString(DISPOSITIVO, valor.trim()) }

    val configurado: Boolean get() = urlServidor.isNotBlank() && token.isNotBlank()

    /** El server respondió 401/403: el token está mal. Se limpia al guardar los ajustes. */
    var errorAutenticacion: String?
        get() = prefs.getString(ERROR_AUTH, null)
        set(valor) = prefs.edit { putString(ERROR_AUTH, valor) }

    var materiasCache: List<Materia>
        get() = prefs.getString(MATERIAS, null)?.let {
            runCatching { JsonClassmer.decodeFromString(ListSerializer(Materia.serializer()), it) }.getOrNull()
        } ?: emptyList()
        set(valor) = prefs.edit {
            putString(MATERIAS, JsonClassmer.encodeToString(ListSerializer(Materia.serializer()), valor))
        }

    /** Ya se pidió el permiso de calendario una vez (no se insiste). */
    var calendarioPedido: Boolean
        get() = prefs.getBoolean(CALENDARIO_PEDIDO, false)
        set(valor) = prefs.edit { putBoolean(CALENDARIO_PEDIDO, valor) }

    /** Ya se pidió el permiso de notificaciones una vez (no se insiste). */
    var notificacionesPedidas: Boolean
        get() = prefs.getBoolean(NOTIFICACIONES_PEDIDAS, false)
        set(valor) = prefs.edit { putBoolean(NOTIFICACIONES_PEDIDAS, valor) }

    /**
     * Sólo para probar el corte automático en el celu: si es mayor que 0, "¿Sigue la clase?" aparece a
     * esos segundos de empezar, se espera la respuesta esos segundos y "Seguir" vuelve a preguntar a
     * esos segundos. No hay pantalla: se pone por adb antes de grabar y vale para UNA sola grabación
     * (se borra al leerlo), así nunca queda puesto para una clase de verdad.
     */
    fun tomarPruebaCorteSeg(): Int {
        if (!prefs.contains(PRUEBA_CORTE)) return 0
        val segundos = runCatching { prefs.getInt(PRUEBA_CORTE, 0) }.getOrDefault(0)
        prefs.edit(commit = true) { remove(PRUEBA_CORTE) }
        return segundos
    }

    var materiasActualizadas: String?
        get() = prefs.getString(MATERIAS_FECHA, null)
        set(valor) = prefs.edit { putString(MATERIAS_FECHA, valor) }

    /** Quién graba ([FuenteAudio]): se recuerda la última elección. */
    var fuenteAudio: String
        get() = prefs.getString(FUENTE_AUDIO, null) ?: FuenteAudio.TELEFONO
        set(valor) = prefs.edit { putString(FUENTE_AUDIO, valor) }

    companion object {
        private const val URL = "url_servidor"
        private const val TOKEN = "token"
        private const val DISPOSITIVO = "dispositivo"
        private const val ERROR_AUTH = "error_autenticacion"
        private const val MATERIAS = "materias_cache"
        private const val MATERIAS_FECHA = "materias_actualizadas"
        private const val CALENDARIO_PEDIDO = "calendario_pedido"
        private const val NOTIFICACIONES_PEDIDAS = "notificaciones_pedidas"
        private const val PRUEBA_CORTE = "prueba_corte_seg"
        private const val FUENTE_AUDIO = "fuente_audio"

        /** Saca espacios y la barra final; si falta el esquema, asume `http://` (server en la LAN). */
        fun normalizarUrl(url: String): String {
            val limpia = url.trim().trimEnd('/')
            if (limpia.isEmpty()) return ""
            return if (limpia.startsWith("http://") || limpia.startsWith("https://")) limpia else "http://$limpia"
        }

        /** La URL ya normalizada es usable (host válido, puerto en rango). */
        fun urlValida(url: String): Boolean =
            url.isNotBlank() && (url.trimEnd('/') + "/").toHttpUrlOrNull()?.host?.isNotBlank() == true

        fun dispositivoPorDefecto(): String {
            val modelo = Build.MODEL ?: "android"
            if (modelo.startsWith("SM-S918")) return "s23-ultra"
            return modelo.lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').ifEmpty { "android" }
        }
    }
}
