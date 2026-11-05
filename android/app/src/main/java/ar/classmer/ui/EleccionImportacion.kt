package ar.classmer.ui

import ar.classmer.calendario.ClaseDelCalendario
import ar.classmer.modelo.Materia
import ar.classmer.modelo.OrigenMateria
import ar.classmer.modelo.TIPOS_CLASE_POR_DEFECTO
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

/**
 * "¿Qué clase es?" de la pantalla Importar: una clase del calendario del día elegido, o a mano
 * (materia, también Auto (IA), tipo, fecha y hora). El inicio queda siempre editable.
 */
data class EleccionImportacion(
    /** El día cuya lista de clases del calendario se muestra. */
    val dia: LocalDate,
    /** La clase del calendario elegida ([ClaseDelCalendario.clave]), o `null`. */
    val clave: String? = null,
    /** Se eligió "Elegir a mano". */
    val aMano: Boolean = false,
    /** `null` = Auto (IA). */
    val materiaId: String? = null,
    val origen: String = OrigenMateria.AUTO,
    val tipoClase: String = TIPOS_CLASE_POR_DEFECTO.first(),
    /** El título del evento del calendario, si salió de ahí. */
    val eventoCalendario: String? = null,
    /** El inicio de la clase (ms desde 1970); `null` = todavía no se eligió. */
    val inicioMs: Long? = null,
    /** El número de la clase: el `proximo_numero` de la materia, editable. En Auto no va. */
    val numero: Int? = null,
) {
    val elegida: Boolean get() = clave != null || aMano

    /** Ya se puede importar: se sabe qué clase es y cuándo empezó. */
    val completa: Boolean get() = elegida && inicioMs != null
}

/** Cómo cambia [EleccionImportacion] con lo que se toca en la pantalla (sin Android, para los tests). */
object ElegirClaseImportada {
    private const val DIA_MS = 24 * 60 * 60_000L

    /** Antes de esto, una fecha de un audio no es creíble (un MP4 sin fecha trae 1904 o 1970). */
    private val MINIMO_MS = Instant.parse("2000-01-01T00:00:00Z").toEpochMilli()

    /**
     * El día con que arranca la lista: el de la fecha del audio (sus metadatos), si no la fecha de
     * modificación del documento, y si no hoy. Una fecha del futuro tampoco vale.
     */
    fun diaInicial(fechaAudioMs: Long?, modificadoMs: Long?, ahoraMs: Long, zona: ZoneId = ZoneId.systemDefault()): LocalDate {
        fun creible(ms: Long?) = ms?.takeIf { it in MINIMO_MS..(ahoraMs + DIA_MS) }
        val ms = creible(fechaAudioMs) ?: creible(modificadoMs) ?: ahoraMs
        return Instant.ofEpochMilli(ms).atZone(zona).toLocalDate()
    }

    /** `[desde, hasta)` en ms del día entero, para leer el calendario. */
    fun limitesDelDia(dia: LocalDate, zona: ZoneId = ZoneId.systemDefault()): Pair<Long, Long> =
        dia.atStartOfDay(zona).toInstant().toEpochMilli() to dia.plusDays(1).atStartOfDay(zona).toInstant().toEpochMilli()

    fun proximoNumero(materias: List<Materia>, materiaId: String?): Int? =
        materiaId?.let { id -> materias.firstOrNull { it.id == id }?.proximoNumero }?.takeIf { it > 0 }

    /** Tocar una clase del calendario completa la materia, el tipo (por el título) y el inicio (el del evento). */
    fun elegirClase(actual: EleccionImportacion, clase: ClaseDelCalendario): EleccionImportacion {
        val tipos = clase.materia.tiposClase.ifEmpty { TIPOS_CLASE_POR_DEFECTO }
        return actual.copy(
            clave = clase.clave,
            aMano = false,
            materiaId = clase.materia.id,
            origen = OrigenMateria.CALENDARIO,
            tipoClase = clase.tipoClase?.takeIf { it in tipos } ?: tipos.first(),
            eventoCalendario = clase.evento.titulo,
            inicioMs = clase.evento.inicioMs,
            numero = clase.materia.proximoNumero?.takeIf { it > 0 },
        )
    }

    /** "Elegir a mano": conserva lo que había (como punto de partida), pero ya no viene del calendario. */
    fun elegirAMano(actual: EleccionImportacion): EleccionImportacion = actual.copy(
        clave = null,
        aMano = true,
        origen = if (actual.materiaId == null) OrigenMateria.AUTO else OrigenMateria.MANUAL,
        eventoCalendario = null,
    )

    /** `id == null` = Auto (IA): sin número (lo pone el server al asignar la materia). */
    fun elegirMateria(actual: EleccionImportacion, id: String?, materias: List<Materia>): EleccionImportacion {
        val materia = materias.firstOrNull { it.id == id }
        val tipos = SeleccionMateria.tiposDe(materias, materia?.id)
        return elegirAMano(actual).copy(
            materiaId = materia?.id,
            origen = if (materia == null) OrigenMateria.AUTO else OrigenMateria.MANUAL,
            tipoClase = if (actual.tipoClase in tipos) actual.tipoClase else tipos.first(),
            numero = proximoNumero(materias, materia?.id),
        )
    }

    fun elegirTipo(actual: EleccionImportacion, tipo: String): EleccionImportacion = actual.copy(tipoClase = tipo)

    /**
     * Se eligió el audio (o se cambió por otro): la lista pasa al día de sus metadatos sólo si todavía no
     * se eligió qué clase es. Lo que eligió el usuario (una clase del calendario, o la materia y el inicio a
     * mano) le gana a la fecha del audio, que en uno de WhatsApp suele ser la del envío (condición 4).
     */
    fun alElegirAudio(actual: EleccionImportacion, diaDelAudio: LocalDate, zona: ZoneId = ZoneId.systemDefault()): EleccionImportacion =
        if (actual.elegida) actual else cambiarDia(actual, diaDelAudio, zona)

    /**
     * Otro día de la lista. A mano, el inicio pasa a ese día (con la misma hora); con una clase del
     * calendario, la elección se borra (era de otro día). El mismo día no cambia nada.
     */
    fun cambiarDia(actual: EleccionImportacion, dia: LocalDate, zona: ZoneId = ZoneId.systemDefault()): EleccionImportacion = when {
        dia == actual.dia -> actual
        actual.aMano -> actual.copy(
            dia = dia,
            inicioMs = actual.inicioMs?.let { ms ->
                val hora = Instant.ofEpochMilli(ms).atZone(zona).toLocalTime()
                dia.atTime(hora).atZone(zona).toInstant().toEpochMilli()
            },
        )
        actual.clave != null -> actual.copy(dia = dia, clave = null, inicioMs = null, eventoCalendario = null)
        else -> actual.copy(dia = dia)
    }

    /** El inicio editado (fecha y hora). A mano, la lista pasa a ese día. */
    fun cambiarInicio(actual: EleccionImportacion, inicioMs: Long, zona: ZoneId = ZoneId.systemDefault()): EleccionImportacion =
        actual.copy(
            inicioMs = inicioMs,
            dia = if (actual.aMano) Instant.ofEpochMilli(inicioMs).atZone(zona).toLocalDate() else actual.dia,
        )

    fun cambiarNumero(actual: EleccionImportacion, numero: Int?): EleccionImportacion =
        actual.copy(numero = numero?.takeIf { it > 0 && actual.materiaId != null })
}
