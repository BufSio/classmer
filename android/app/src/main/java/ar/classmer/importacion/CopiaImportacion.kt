package ar.classmer.importacion

import android.util.Log
import ar.classmer.datos.AlmacenSesiones
import ar.classmer.modelo.ArchivoImportado
import ar.classmer.modelo.EstadoLocal
import ar.classmer.modelo.Evento
import ar.classmer.modelo.Importacion
import ar.classmer.modelo.TipoEvento
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.RandomAccessFile
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * Copia al celu el audio y las fotos de una clase importada, según su `importacion.json`:
 *
 * 1. El audio va a `audio.<ext>.parcial`, que se renombra a `audio.<ext>` al final. Cada
 *    [confirmarCada] bytes hace `fsync` y anota hasta dónde llegó: si se corta (Android frena el
 *    trabajo, se cierra la app, se apaga el celu), se retoma desde ahí sin repetir lo ya copiado.
 * 2. Cada foto va entera a `fotos/<id>.<ext>` (temporal y renombrar). Una que no se puede leer o que
 *    pesa más de 50 MB queda afuera, con el motivo, y no frena la clase.
 * 3. Las fotos se anotan como eventos `foto` sin minuto (el server las ubica por la hora de su EXIF),
 *    se mide la duración y la sesión pasa a `terminada`: desde ahí la sube el Subidor de siempre.
 *
 * Si el audio no se puede leer (lo borraron, se perdió el permiso) o no hay lugar, el motivo queda en
 * `importacion.json` y la clase lo muestra, con "Reintentar" y "Descartar": no se reintenta sola. Sólo
 * esta clase escribe `importacion.json` (después de [Importador.crear]).
 *
 * @param abrir abre el documento desde ese byte; tira una excepción si ya no se puede leer.
 * @param medirDuracion la duración del audio copiado, o `null` si no se puede medir.
 * @param espacioLibre el espacio libre en una carpeta (0 = no se sabe), reemplazable en los tests.
 * @param alAvanzar lo copiado y el total que informó el selector, para la notificación.
 * @param seguir devuelve false cuando hay que parar (WorkManager frenó el trabajo).
 */
class CopiaImportacion(
    private val almacen: AlmacenSesiones,
    private val abrir: (uri: String, desde: Long) -> InputStream,
    private val medirDuracion: (File) -> Long? = { null },
    private val espacioLibre: (File) -> Long = { 0L },
    private val confirmarCada: Long = CONFIRMAR_CADA,
    private val alAvanzar: (copiados: Long, total: Long) -> Unit = { _, _ -> },
    private val seguir: () -> Boolean = { true },
) {
    enum class Resultado {
        /** Se copió todo y la clase quedó `terminada`: hay que patear la subida. */
        TERMINADA,

        /** El audio no se pudo copiar: el motivo quedó en `importacion.json`. */
        ERROR,

        /** Se frenó en el medio: lo copiado quedó anotado y el próximo intento sigue desde ahí. */
        CORTADA,

        /** No había nada que hacer: no es una importación en curso, o ya había fallado y no se pidió reintentar. */
        NADA,
    }

    /** Hay que parar ([seguir] dio false). */
    private class Frenada : Exception()

    /** El audio no se puede copiar: [message] es lo que muestra la clase. */
    private class FallaCopia(mensaje: String) : Exception(mensaje)

    /** Falló leer el documento (no escribir en el celu). */
    private class FallaLectura(causa: Throwable) : IOException(causa)

    /** Una pasada sobre una clase: [imp] es lo último guardado en `importacion.json`. */
    private inner class Pasada(val sesionId: String, var imp: Importacion) {
        val dir: File = almacen.dir(sesionId)

        fun guardar(nueva: Importacion) {
            imp = nueva
            almacen.guardarImportacion(sesionId, nueva)
        }

        fun guardarFoto(i: Int, foto: ArchivoImportado) = guardar(imp.copy(fotos = imp.fotos.toMutableList().also { it[i] = foto }))

        fun avisar(parcial: Long) = alAvanzar(Importador.copiado(imp, parcial), imp.bytesTotales)
    }

    /**
     * Copia lo que falta de la clase [sesionId]. Bloquea: se llama fuera del hilo principal.
     * @param reintento se tocó "Reintentar": una importación que había fallado se vuelve a probar.
     */
    fun copiar(sesionId: String, reintento: Boolean = false): Resultado {
        val sesion = almacen.leerSesion(sesionId) ?: return Resultado.NADA
        if (!sesion.importada || sesion.estadoLocal != EstadoLocal.IMPORTANDO) return Resultado.NADA
        // Sin importacion.json no hay qué copiar: la clase se muestra rota y se puede descartar.
        val p = Pasada(sesionId, almacen.leerImportacion(sesionId) ?: return Resultado.NADA)
        if (p.imp.error != null) {
            if (!reintento) return Resultado.NADA
            p.guardar(p.imp.copy(error = null))
        }
        try {
            if (!p.imp.audio.copiado) copiarAudio(p, otraVez = true)
            for (i in p.imp.fotos.indices) {
                val f = p.imp.fotos[i]
                if (!f.copiado && f.problema == null) copiarFoto(p, i)
            }
        } catch (e: Frenada) {
            return Resultado.CORTADA
        } catch (e: FallaCopia) {
            Log.w(TAG, "Importación $sesionId: ${e.message}")
            p.guardar(p.imp.copy(error = e.message))
            return Resultado.ERROR
        }

        // Las fotos, sin minuto: el server las ubica por su EXIF. Sin repetir las que ya se anotaron.
        if (!p.imp.fotosRegistradas) {
            val ya = almacen.leerEventos(sesionId).map { it.id }.toSet()
            for (f in p.imp.fotos) {
                val id = f.id ?: continue
                if (!f.copiado || id in ya) continue
                almacen.agregarEvento(sesionId, Evento(id, TipoEvento.FOTO, null, p.imp.creado, archivo = f.destino))
            }
            p.guardar(p.imp.copy(fotosRegistradas = true))
        }

        // Se descartó mientras tanto: no queda nada que cerrar.
        val actual = almacen.leerSesion(sesionId)?.takeIf { it.estadoLocal == EstadoLocal.IMPORTANDO } ?: return Resultado.NADA
        // La duración medida en el celu; si no se puede, la que se midió al elegirlo; si tampoco, 0 (va null).
        val medida = runCatching { medirDuracion(almacen.archivoAudio(actual)) }.getOrNull()?.takeIf { it > 0 }
        almacen.guardarSesion(
            actual.copy(
                estadoLocal = EstadoLocal.TERMINADA,
                duracionMs = medida ?: p.imp.duracionElegidaMs ?: 0,
                audioBytes = p.imp.audio.bytes ?: actual.audioBytes,
            ),
        )
        p.avisar(0)
        Log.i(TAG, "Importación $sesionId copiada: ${p.imp.audio.bytes} B de audio, ${p.imp.fotos.count { it.copiado }} fotos")
        return Resultado.TERMINADA
    }

    /**
     * El audio, de a tramos, al `.parcial`. Sólo vale lo confirmado: lo que se escribió después pudo no
     * llegar al disco (un apagón), así que se corta ahí y se sigue desde ese byte.
     *
     * @param otraVez si se retomó sobre un documento que cambió de tamaño, se puede empezar de cero una vez.
     */
    private fun copiarAudio(p: Pasada, otraVez: Boolean) {
        val a = p.imp.audio
        val destino = File(p.dir, a.destino)
        val parcial = File(p.dir, a.destino + EXT_PARCIAL)
        var desde = a.confirmados.coerceAtLeast(0)
        if (!parcial.isFile || parcial.length() < desde) desde = 0
        try {
            RandomAccessFile(parcial, "rw").use { it.setLength(desde) }
        } catch (e: IOException) {
            throw FallaCopia(noSeGuarda(e))
        }
        val pendienteFotos = p.imp.fotos.filter { !it.copiado && it.problema == null }.sumOf { it.bytes ?: 0 }
        Importador.problemaEspacio(espacioLibre(p.dir), ((a.bytes ?: 0) - desde).coerceAtLeast(0) + pendienteFotos)
            ?.let { throw FallaCopia(it) }

        var total = desde
        try {
            leer(a.uri, desde).use { entrada ->
                FileOutputStream(parcial, true).use { salida ->
                    val buffer = ByteArray(BUFFER)
                    var sinConfirmar = 0L
                    fun confirmar() {
                        salida.flush()
                        salida.fd.sync()
                        p.guardar(p.imp.copy(audio = p.imp.audio.copy(confirmados = total)))
                        sinConfirmar = 0
                    }
                    while (true) {
                        if (!seguir()) {
                            confirmar()
                            throw Frenada()
                        }
                        val n = try {
                            entrada.read(buffer)
                        } catch (e: Exception) {
                            throw FallaLectura(e)
                        }
                        if (n < 0) break
                        total += n
                        if (total > Importador.MAX_AUDIO) throw FallaCopia("El audio pesa más de 2 GB: no se puede importar.")
                        salida.write(buffer, 0, n)
                        sinConfirmar += n
                        if (sinConfirmar >= confirmarCada) {
                            confirmar()
                            // El selector puede no informar el tamaño (o mentirlo): se mira el espacio en el camino.
                            val libre = espacioLibre(p.dir)
                            if (libre in 1 until Importador.MARGEN_LIBRE) throw FallaCopia(Importador.problemaEspacio(libre, 0) ?: "Falta lugar")
                            p.avisar(total)
                        }
                    }
                    confirmar()
                }
            }
        } catch (e: Frenada) {
            throw e
        } catch (e: FallaCopia) {
            throw e
        } catch (e: FallaLectura) {
            throw FallaCopia(audioIlegible(a.nombre, e.cause ?: e))
        } catch (e: IOException) {
            throw FallaCopia(noSeGuarda(e))
        }

        if (a.bytes != null && total != a.bytes && desde > 0 && otraVez) {
            // Se retomó y el tamaño no coincide: el documento cambió entre un intento y otro (o el selector
            // informó mal). Lo copiado puede ser de otro archivo: se copia entero de nuevo, una sola vez.
            Log.w(TAG, "El audio de ${p.sesionId} tiene $total B y el selector dijo ${a.bytes}: se copia de nuevo")
            parcial.delete()
            p.guardar(p.imp.copy(audio = p.imp.audio.copy(confirmados = 0)))
            return copiarAudio(p, otraVez = false)
        }
        if (total == 0L) throw FallaCopia("El audio elegido está vacío: no hay nada que importar.")
        try {
            Files.move(parcial.toPath(), destino.toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        } catch (e: IOException) {
            throw FallaCopia(noSeGuarda(e))
        }
        p.guardar(p.imp.copy(audio = p.imp.audio.copy(copiado = true, confirmados = total, bytes = total)))
        p.avisar(0)
    }

    /** Una foto, entera. Si no se puede leer o pesa demasiado, queda afuera (con el motivo) y la clase sigue. */
    private fun copiarFoto(p: Pasada, i: Int) {
        val f = p.imp.fotos[i]
        val destino = File(p.dir, f.destino)
        // Se copió pero se cortó antes de anotarlo.
        if (destino.isFile && destino.length() > 0) {
            p.guardarFoto(i, f.copy(copiado = true, bytes = destino.length()))
            return
        }
        Importador.problemaFoto(f.nombre, f.bytes)?.let {
            p.guardarFoto(i, f.copy(problema = it))
            return
        }
        destino.parentFile?.mkdirs()
        val tmp = File(destino.path + ".tmp")
        var total = 0L
        try {
            leer(f.uri, 0).use { entrada ->
                FileOutputStream(tmp).use { salida ->
                    val buffer = ByteArray(BUFFER)
                    while (true) {
                        if (!seguir()) throw Frenada()
                        val n = try {
                            entrada.read(buffer)
                        } catch (e: Exception) {
                            throw FallaLectura(e)
                        }
                        if (n < 0) break
                        total += n
                        if (total > Importador.MAX_FOTO) break
                        salida.write(buffer, 0, n)
                    }
                    salida.fd.sync()
                }
            }
            val problema = when {
                total > Importador.MAX_FOTO -> Importador.problemaFoto(f.nombre, total)
                total == 0L -> "${f.nombre ?: "Una foto"} está vacía: no se importa."
                else -> null
            }
            if (problema != null) {
                tmp.delete()
                p.guardarFoto(i, f.copy(problema = problema))
                return
            }
            Files.move(tmp.toPath(), destino.toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        } catch (e: Frenada) {
            tmp.delete()
            throw e
        } catch (e: FallaLectura) {
            tmp.delete()
            Log.w(TAG, "No se puede leer la foto ${f.nombre ?: f.uri}", e.cause ?: e)
            p.guardarFoto(i, f.copy(problema = "${f.nombre ?: "Una foto"} no se pudo leer: quedó afuera de la clase."))
            return
        } catch (e: IOException) {
            // Escribir en el celu falló (por ejemplo, sin lugar): eso sí frena la importación.
            tmp.delete()
            throw FallaCopia(noSeGuarda(e))
        }
        p.guardarFoto(i, f.copy(copiado = true, bytes = total))
        p.avisar(0)
    }

    /** Abre el documento; cualquier falla al abrirlo es de lectura (se borró, se perdió el permiso…). */
    private fun leer(uri: String, desde: Long): InputStream = try {
        abrir(uri, desde)
    } catch (e: Exception) {
        throw FallaLectura(e)
    }

    /** Lo que muestra la clase (corto: la fila tiene tres líneas); el detalle técnico queda en el log. */
    private fun audioIlegible(nombre: String?, causa: Throwable): String {
        Log.w(TAG, "No se puede leer el audio ${nombre ?: ""}", causa)
        val cual = nombre?.let { "el audio ($it)" } ?: "el audio"
        return "No se puede leer $cual: puede que lo hayan borrado o movido, o que Android ya no deje leerlo. " +
            "Si sigue en el celu, tocá Reintentar; si no, Descartar."
    }

    private fun noSeGuarda(e: IOException) =
        "No se pudo guardar la copia en el celu (${e.message ?: e.javaClass.simpleName}). Revisá el espacio libre y tocá Reintentar."

    companion object {
        private const val TAG = "ClassmerImportacion"
        const val EXT_PARCIAL = ".parcial"

        /** Cada 8 MiB copiados se hace `fsync` y se anota: si se corta, se pierde a lo sumo eso. */
        const val CONFIRMAR_CADA = 8L * 1024 * 1024
        private const val BUFFER = 256 * 1024
    }
}
