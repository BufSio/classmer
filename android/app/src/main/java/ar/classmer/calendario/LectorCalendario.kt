package ar.classmer.calendario

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CalendarContract
import android.util.Log
import androidx.core.content.ContextCompat

/** Lee del calendario del propio celu (sin OAuth) los eventos alrededor de ahora, o los de un día. */
object LectorCalendario {
    private const val TAG = "ClassmerCalendario"

    fun tienePermiso(context: Context) =
        ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED

    fun eventosCercanos(context: Context, ahoraMs: Long = System.currentTimeMillis()): List<EventoCalendario> =
        eventosEntre(context, ahoraMs - ReglasCalendario.MARGEN_MS, ahoraMs + ReglasCalendario.MARGEN_MS)

    /** Los eventos que se cruzan con `[desdeMs, hastaMs]` (por ejemplo, un día entero para "Importar clase"). */
    fun eventosEntre(context: Context, desdeMs: Long, hastaMs: Long): List<EventoCalendario> {
        if (!tienePermiso(context)) return emptyList()
        val uri = CalendarContract.Instances.CONTENT_URI.buildUpon().also {
            ContentUris.appendId(it, desdeMs)
            ContentUris.appendId(it, hastaMs)
        }.build()
        val columnas = arrayOf(
            CalendarContract.Instances.TITLE,
            CalendarContract.Instances.BEGIN,
            CalendarContract.Instances.END,
            CalendarContract.Instances.ALL_DAY,
            CalendarContract.Instances.CALENDAR_DISPLAY_NAME,
            CalendarContract.Instances.STATUS,
            CalendarContract.Instances.SELF_ATTENDEE_STATUS,
        )
        val eventos = mutableListOf<EventoCalendario>()
        try {
            context.contentResolver.query(uri, columnas, null, null, CalendarContract.Instances.BEGIN)?.use { c ->
                while (c.moveToNext()) {
                    if (c.getInt(5) == CalendarContract.Events.STATUS_CANCELED) continue
                    if (c.getInt(6) == CalendarContract.Attendees.ATTENDEE_STATUS_DECLINED) continue
                    eventos += EventoCalendario(
                        titulo = c.getString(0) ?: "",
                        inicioMs = c.getLong(1),
                        finMs = c.getLong(2),
                        todoElDia = c.getInt(3) != 0,
                        calendario = c.getString(4) ?: "",
                    )
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "No se pudo leer el calendario", e)
        }
        return eventos
    }
}
