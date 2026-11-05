package ar.classmer.wear

import android.media.AudioManager
import android.media.MediaRecorder
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.view.WindowManager
import android.widget.TextView
import androidx.activity.ComponentActivity
import java.io.File

/**
 * Sólo en debug: graba un clip corto con una fuente y un bitrate dados, para elegirlos con una prueba
 * barata (ver android/README.md). No toca las grabaciones de clase. Se lanza por adb, con el reloj
 * despierto:
 *
 * ```
 * adb shell am start -n ar.classmer/ar.classmer.wear.PruebaAudioActivity --es fuente VOICE_RECOGNITION --ei bitrate 32000 --ei segundos 30 --es nombre vr32
 * adb exec-out run-as ar.classmer cat files/pruebas/vr32.ogg > vr32.ogg
 * ```
 */
class PruebaAudioActivity : ComponentActivity() {
    private var recorder: MediaRecorder? = null
    private lateinit var texto: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        texto = TextView(this).apply { textSize = 14f; setPadding(24, 60, 24, 24) }
        setContentView(texto)

        val nombreFuente = intent.getStringExtra("fuente") ?: "MIC"
        val fuente = when (nombreFuente) {
            "VOICE_RECOGNITION" -> MediaRecorder.AudioSource.VOICE_RECOGNITION
            "UNPROCESSED" -> MediaRecorder.AudioSource.UNPROCESSED
            "CAMCORDER" -> MediaRecorder.AudioSource.CAMCORDER
            "VOICE_COMMUNICATION" -> MediaRecorder.AudioSource.VOICE_COMMUNICATION
            else -> MediaRecorder.AudioSource.MIC
        }
        val bitrate = intent.getIntExtra("bitrate", 32_000)
        val segundos = intent.getIntExtra("segundos", 30)
        val nombre = intent.getStringExtra("nombre")?.filter { it.isLetterOrDigit() || it == '-' }?.ifEmpty { null } ?: "prueba"
        val unprocessed = getSystemService(AudioManager::class.java).getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)
        Log.i(TAG, "UNPROCESSED soportado: $unprocessed")

        val archivo = File(File(filesDir, "pruebas").apply { mkdirs() }, "$nombre.ogg")
        archivo.delete()
        try {
            val r = MediaRecorder(this)
            r.setAudioSource(fuente)
            r.setOutputFormat(MediaRecorder.OutputFormat.OGG)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.OPUS)
            r.setAudioChannels(1)
            r.setAudioSamplingRate(48_000)
            r.setAudioEncodingBitRate(bitrate)
            r.setOutputFile(archivo)
            r.prepare()
            r.start()
            recorder = r
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo grabar con $nombreFuente", e)
            texto.text = "Error con $nombreFuente: ${e.message}"
            return
        }
        val desde = SystemClock.elapsedRealtime()
        texto.text = "Prueba $nombre\n$nombreFuente $bitrate bps\n$segundos s\nUNPROCESSED: $unprocessed"
        Log.i(TAG, "Grabando $nombre: $nombreFuente $bitrate bps, $segundos s")
        texto.postDelayed({
            detener()
            Log.i(TAG, "Listo $nombre: ${archivo.length()} bytes en ${SystemClock.elapsedRealtime() - desde} ms")
            texto.text = "Listo $nombre\n${archivo.length()} bytes"
        }, segundos * 1000L)
    }

    private fun detener() {
        recorder?.let { r ->
            runCatching { r.stop() }
            r.release()
        }
        recorder = null
    }

    override fun onDestroy() {
        detener()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ClassmerPruebaAudio"
    }
}
