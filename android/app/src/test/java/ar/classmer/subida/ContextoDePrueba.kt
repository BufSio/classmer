package ar.classmer.subida

import android.content.Context
import android.content.ContextWrapper
import android.content.SharedPreferences
import java.io.File

/**
 * Lo mínimo de un Context para correr [SincronizacionSubida] entera en un test: la carpeta de la app
 * ([ar.classmer.datos.AlmacenSesiones.de], [ar.classmer.datos.AlmacenAdjuntos.de]) y SharedPreferences
 * en memoria ([ar.classmer.datos.Ajustes]).
 */
class ContextoDePrueba(private val raiz: File) : ContextWrapper(null) {
    private val prefs = mutableMapOf<String, PrefsEnMemoria>()

    override fun getExternalFilesDir(type: String?): File = raiz
    override fun getFilesDir(): File = raiz
    override fun getApplicationContext(): Context = this
    override fun getSharedPreferences(name: String, mode: Int): SharedPreferences =
        synchronized(prefs) { prefs.getOrPut(name) { PrefsEnMemoria() } }
}

/** SharedPreferences en memoria: `apply` y `commit` escriben enseguida. */
class PrefsEnMemoria : SharedPreferences {
    private val valores = mutableMapOf<String, Any>()

    private inline fun <reified T> leer(clave: String, porDefecto: T): T = synchronized(valores) { valores[clave] as? T } ?: porDefecto

    override fun getAll(): Map<String, *> = synchronized(valores) { valores.toMap() }
    override fun getString(key: String, defValue: String?): String? = synchronized(valores) { valores[key] as? String } ?: defValue
    @Suppress("UNCHECKED_CAST")
    override fun getStringSet(key: String, defValues: Set<String>?): Set<String>? =
        synchronized(valores) { valores[key] as? Set<String> } ?: defValues
    override fun getInt(key: String, defValue: Int): Int = leer(key, defValue)
    override fun getLong(key: String, defValue: Long): Long = leer(key, defValue)
    override fun getFloat(key: String, defValue: Float): Float = leer(key, defValue)
    override fun getBoolean(key: String, defValue: Boolean): Boolean = leer(key, defValue)
    override fun contains(key: String): Boolean = synchronized(valores) { key in valores }
    override fun edit(): SharedPreferences.Editor = Editor()
    override fun registerOnSharedPreferenceChangeListener(listener: SharedPreferences.OnSharedPreferenceChangeListener?) = Unit
    override fun unregisterOnSharedPreferenceChangeListener(listener: SharedPreferences.OnSharedPreferenceChangeListener?) = Unit

    private inner class Editor : SharedPreferences.Editor {
        private val cambios = mutableMapOf<String, Any?>()
        private var limpiar = false

        private fun poner(clave: String, valor: Any?) = apply { cambios[clave] = valor }

        override fun putString(key: String, value: String?) = poner(key, value)
        override fun putStringSet(key: String, values: Set<String>?) = poner(key, values?.toSet())
        override fun putInt(key: String, value: Int) = poner(key, value)
        override fun putLong(key: String, value: Long) = poner(key, value)
        override fun putFloat(key: String, value: Float) = poner(key, value)
        override fun putBoolean(key: String, value: Boolean) = poner(key, value)
        override fun remove(key: String) = poner(key, null)
        override fun clear() = apply { limpiar = true }

        override fun commit(): Boolean {
            apply()
            return true
        }

        override fun apply() = synchronized(valores) {
            if (limpiar) valores.clear()
            for ((clave, valor) in cambios) if (valor == null) valores.remove(clave) else valores[clave] = valor
        }
    }
}
