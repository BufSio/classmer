"""Etapa 2 (§6): cortar el audio en bloques de ~10 min en silencios, sin los silencios largos.

Salidas en la carpeta de la sesión (formato en docs/internos.md):
- `silencios.json`: duración, silencios detectados, silencios largos descartados y tramos con habla.
- `bloques/bloque_NN.mp3`: mono, 16 kHz, MP3 48 kbps.
- `bloques.json` y las filas de `bloques` (estado 'pendiente'), con inicio_ms/fin_ms absolutos.

Silencio largo: ≥ silencio_largo_s a silencio_db y TAMBIÉN con un umbral 12 dB más estricto (si
no, puede ser voz baja: cuenta como habla y se transcribe). Sólo se descarta la parte confirmada.

Cómo corta: los silencios largos separan el audio en tramos y nunca quedan
dentro de un bloque. Cada tramo se parte desde su inicio: el objetivo es inicio + bloque_s y el
corte va al punto medio del silencio más cercano al objetivo dentro de ±ventana_s (si empatan,
el más largo); si no hay ninguno, corte duro en el objetivo. El último bloque de un tramo nunca
queda más corto que 1/4 del objetivo: se une al anterior. Un bloque sin voz no se crea.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from classmer import audio
from classmer.pipeline import Contexto, ErrorEtapa, Resultado
from classmer.sesiones import PISTA_CELU, datos_celu, guardar_datos_celu, ruta_audio
from classmer.util import escribir_json, leer_json, ms_a_hhmmss

if TYPE_CHECKING:
    from classmer.cola import Job

Tramo = tuple[int, int]  # (inicio_ms, fin_ms), absolutos en el audio original

MARGEN_MS = 250        # parte de un silencio largo que queda pegada al bloque (ataques y colas suaves)
VOZ_MIN_MS = 1000      # un bloque con menos habla que esto no se crea
EMPATE_MS = 1000       # silencios a menos de esto de la mejor distancia al objetivo cuentan como empate
TOLERANCIA_DURACION_MS = 1000  # si ffprobe y la decodificación difieren más, manda la decodificación
FRECUENCIA_HZ = 16000
KBPS = 48
VOZ_MINIMA_RELATIVA = 0.5  # con menos voz que esto, se usa el umbral estricto en todo el audio
REBAJA_UMBRAL_DB = 12      # umbral estricto = silencio_db − esto (confirma los silencios largos)
BORDE_MS = 1000            # puntas de un silencio largo sin confirmar más cortas que esto: siguen siendo silencio
# Segunda pista (§15): sus bloques van numerados desde 101 (la clave de `bloques` es (sesión, n)).
PRIMER_BLOQUE_CELU = 101
CARPETA_BLOQUES_CELU = "bloques_celu"

# Errores de ffmpeg que vale la pena reintentar (archivo tomado, disco lleno, timeout...).
_TRANSITORIOS = (
    "tardó demasiado",
    "permission denied",
    "resource temporarily unavailable",
    "no space left",
    "device or resource busy",
    "used by another process",
    "input/output error",
    "cannot allocate memory",
    "out of memory",
)


@dataclass
class Segmentacion:
    duracion_ms: int
    silencios: list[Tramo]
    descartados: list[Tramo]
    habla: list[Tramo]
    bloques: list[Tramo]
    cortes_duros: int = 0
    rescatado_ms: int = 0  # silencio largo al umbral normal que el estricto no confirma (posible voz baja)

    @property
    def voz_ms(self) -> int:
        return sum(b - a for a, b in self.habla)


# --- etapa -------------------------------------------------------------------------------------

def ejecutar(ctx: Contexto, job: Job) -> Resultado:
    sid = job.sesion_id
    if not sid:
        raise ErrorEtapa("segmentar necesita una sesión", reintentable=False)
    if job.args.get("pista") == PISTA_CELU:
        # "Agregar audio del celu" (panel): sólo la segunda pista; el principal ya está transcripto.
        filas_celu, resumen_celu = segmentar_celu(ctx, sid)
        # Sin bloques del celu (p. ej. revisión apagada y el principal con audio en toda la clase) el
        # pipeline sigue igual: linea_de_tiempo lo encadena como lo haría el último bloque.
        siguientes = [("transcribir", {"n": f["n"]}) for f in filas_celu] or [("linea_de_tiempo", {})]
        return Resultado(siguientes=siguientes, log=f"celu: {resumen_celu}")
    cfg = ctx.cfg
    with ctx.base.conexion() as conn:
        ruta = ruta_audio(cfg, conn, sid)
    if not ruta.is_file():
        raise ErrorEtapa(f"no encuentro el audio de la sesión ({ruta.name})", reintentable=False)

    seg = _segmentar_audio(ctx, sid, ruta, "segmentar")
    carpeta = ctx.sesion_dir(sid)
    escribir_json(carpeta / "silencios.json", {
        "duracion_ms": seg.duracion_ms,
        "silencios": _a_json(seg.silencios),
        "descartados": _a_json(seg.descartados),
        "habla": _a_json(seg.habla),
    })

    _borrar_bloques_viejos(carpeta / "bloques")
    filas: list[dict[str, int | str]] = []
    for n, (inicio, fin) in enumerate(seg.bloques, start=1):
        rel = f"bloques/bloque_{n:02d}.mp3"
        try:
            audio.extraer_mp3(ruta, carpeta / rel, inicio, fin, frecuencia_hz=FRECUENCIA_HZ, kbps=KBPS)
        except audio.ErrorAudio as e:
            raise _error_ffmpeg(e) from e
        filas.append({"n": n, "inicio_ms": inicio, "fin_ms": fin, "ruta": rel})
    escribir_json(carpeta / "bloques.json", filas)

    with ctx.base.transaccion() as conn:
        conn.execute("DELETE FROM bloques WHERE sesion_id = ? AND pista IS NULL", (sid,))
        conn.executemany(
            "INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta, estado) "
            "VALUES (?, ?, ?, ?, ?, 'pendiente')",
            [(sid, f["n"], f["inicio_ms"], f["fin_ms"], f["ruta"]) for f in filas],
        )

    if not filas:
        ctx.log_sesion(sid, f"segmentar: no encontré voz en {ms_a_hhmmss(seg.duracion_ms)} de audio")
        raise ErrorEtapa("el audio no tiene voz", reintentable=False)

    resumen = (
        f"{len(filas)} bloque(s); voz {ms_a_hhmmss(seg.voz_ms)} de {ms_a_hhmmss(seg.duracion_ms)}; "
        f"{len(seg.descartados)} silencio(s) largo(s) descartado(s)"
    )
    if seg.cortes_duros:
        resumen += f"; {seg.cortes_duros} corte(s) sin silencio cerca"
    ctx.log_sesion(sid, f"segmentar: {resumen}")
    siguientes = [("transcribir", {"n": f["n"]}) for f in filas]
    with ctx.base.conexion() as conn:
        hay_celu = datos_celu(conn, sid) is not None
    if hay_celu:
        try:
            filas_celu, resumen_celu = segmentar_celu(ctx, sid, duracion_principal_ms=seg.duracion_ms)
        except ErrorEtapa as e:
            if e.reintentable:
                raise
            # Un audio del celu roto no traba la clase: sigue con el principal solo.
            ctx.log_sesion(sid, f"segmentar celu: aviso: lo salteo ({e}); la clase sigue con el principal")
            resumen += f" · celu salteado: {e}"
        else:
            siguientes += [("transcribir", {"n": f["n"]}) for f in filas_celu]
            resumen += f" · celu: {resumen_celu}"
    return Resultado(siguientes=siguientes, log=resumen)


def _segmentar_audio(ctx: Contexto, sid: str, ruta: Path, etiqueta: str) -> Segmentacion:
    """Duración, silencios y plan de bloques de un audio (el principal o el del celu)."""
    p = ctx.cfg.segmentar
    try:
        duracion = audio.duracion_ms(ruta)
        silencios, decodificado = audio.detectar_silencios(
            ruta, umbral_db=p.silencio_db, minimo_s=p.silencio_min_s, duracion_ms=duracion
        )
    except audio.ErrorAudio as e:
        raise _error_ffmpeg(e) from e
    if decodificado is not None and abs(decodificado - duracion) > TOLERANCIA_DURACION_MS:
        ctx.log_sesion(
            sid,
            f"{etiqueta}: ffprobe dice {ms_a_hhmmss(duracion)} pero se decodifican "
            f"{ms_a_hhmmss(decodificado)}; uso lo decodificado",
        )
        duracion = decodificado

    # Voz baja (celu lejos o tapado): puede quedar bajo el umbral y parecer silencio largo, que no
    # se transcribe. Un silencio largo sólo se descarta si también lo es con el umbral estricto.
    umbral = p.silencio_db - REBAJA_UMBRAL_DB
    try:
        estrictos, _ = audio.detectar_silencios(
            ruta, umbral_db=umbral, minimo_s=p.silencio_min_s, duracion_ms=duracion
        )
    except audio.ErrorAudio as e:
        raise _error_ffmpeg(e) from e
    params = {"bloque_ms": _ms(p.bloque_s), "ventana_ms": _ms(p.ventana_s), "largo_ms": _ms(p.silencio_largo_s)}
    try:
        seg = planificar(duracion, silencios, estrictos=estrictos, **params)
    except ValueError as e:
        raise ErrorEtapa(f"configuración de segmentar inválida: {e}", reintentable=False) from e
    if seg.rescatado_ms >= VOZ_MIN_MS:
        ctx.log_sesion(
            sid,
            f"{etiqueta}: {ms_a_hhmmss(seg.rescatado_ms)} de silencio largo a {p.silencio_db:g} dB no lo es "
            f"a {umbral:g} dB (puede ser voz baja): no se descarta",
        )

    # Grabación baja en general: si igual queda poca voz, se usa el umbral estricto en todo el audio.
    if seg.duracion_ms > 120_000 and seg.voz_ms < VOZ_MINIMA_RELATIVA * seg.duracion_ms:
        seg2 = planificar(duracion, estrictos, **params)
        if seg2.voz_ms > seg.voz_ms:
            ctx.log_sesion(
                sid,
                f"{etiqueta}: poca voz a {p.silencio_db:g} dB ({ms_a_hhmmss(seg.voz_ms)}); "
                f"uso {umbral:g} dB ({ms_a_hhmmss(seg2.voz_ms)} de voz)",
            )
            seg = seg2
    return seg


# --- segunda pista (§15) -----------------------------------------------------------------------

def tramos_sin_principal(desfase_ms: int, duracion_celu_ms: int, duracion_principal_ms: int) -> list[Tramo]:
    """Partes del audio del celu (en su tiempo) donde el principal no tiene audio: antes de que
    empiece (desfase negativo) y después de que termine (se cortó o se quedó sin batería)."""
    tramos: list[Tramo] = []
    if desfase_ms < 0:
        tramos.append((0, min(duracion_celu_ms, -desfase_ms)))
    fin_principal = duracion_principal_ms - desfase_ms   # el final del principal, en tiempo del celu
    if fin_principal < duracion_celu_ms:
        tramos.append((max(0, fin_principal), duracion_celu_ms))
    return [(a, b) for a, b in tramos if b - a >= VOZ_MIN_MS]


def recortar_a_tramos(bloques: list[Tramo], tramos: list[Tramo], margen_ms: int = 2000) -> list[Tramo]:
    """Los bloques cortados a los tramos (con un margen para no perder la primera palabra)."""
    salida: list[Tramo] = []
    for a, b in bloques:
        for x, y in tramos:
            i, f = max(a, x - margen_ms), min(b, y + margen_ms)
            if f - i >= VOZ_MIN_MS:
                salida.append((i, f))
    return salida


def segmentar_celu(ctx: Contexto, sid: str, *, duracion_principal_ms: int | None = None
                   ) -> tuple[list[dict[str, int | str]], str]:
    """Segmenta el audio del celu en bloques_celu/ (bloques n ≥ 101, pista 'celu').

    Si falta el desfase (un audio agregado desde el panel), lo mide por correlación y lo guarda. Con la
    revisión apagada en la materia sólo corta los tramos donde el principal no tiene audio (el
    respaldo); con la revisión prendida, o sin materia todavía, todo el audio."""
    from classmer import correlacion

    cfg = ctx.cfg
    carpeta = ctx.sesion_dir(sid)
    with ctx.base.conexion() as conn:
        datos = datos_celu(conn, sid)
        ruta = ruta_audio(cfg, conn, sid, PISTA_CELU)
        principal = ruta_audio(cfg, conn, sid)
        materia_id = conn.execute("SELECT materia_id FROM sesiones WHERE id = ?", (sid,)).fetchone()["materia_id"]
    if datos is None or not ruta.is_file() or ruta.stat().st_size == 0:
        raise ErrorEtapa(f"no está el audio del celu ({ruta.name})", reintentable=False)
    if duracion_principal_ms is None:
        silencios = leer_json(carpeta / "silencios.json")
        duracion_principal_ms = (silencios.get("duracion_ms") if isinstance(silencios, dict) else None) or 0
        if not duracion_principal_ms:
            try:
                duracion_principal_ms = audio.duracion_ms(principal)
            except audio.ErrorAudio as e:
                raise _error_ffmpeg(e) from e

    desfase = datos.get("desfase_ms")
    if desfase is None:
        try:
            medido = correlacion.medir(principal, ruta)
        except correlacion.ErrorCorrelacion as e:
            raise ErrorEtapa(f"no pude medir el desfase del audio del celu: {e}", reintentable=False) from e
        desfase = medido.desfase_ms
        with ctx.base.conexion() as conn:
            guardar_datos_celu(conn, sid, {"desfase_ms": desfase, "desfase_origen": "correlacion",
                                           "correlacion": medido.como_dict()})
        ctx.log_sesion(sid, f"segmentar celu: desfase {desfase} ms por correlación "
                            f"(confianza {medido.confianza:.2f}, pico {medido.pico:.2f})")

    seg = _segmentar_audio(ctx, sid, ruta, "segmentar celu")
    materia = cfg.materia(materia_id)
    revisar = materia is None or materia.revisar_transcript
    bloques = seg.bloques
    tramos = tramos_sin_principal(int(desfase), seg.duracion_ms, int(duracion_principal_ms))
    if not revisar:
        bloques = recortar_a_tramos(bloques, tramos)
    escribir_json(carpeta / "silencios.celu.json", {
        "duracion_ms": seg.duracion_ms,
        "silencios": _a_json(seg.silencios),
        "descartados": _a_json(seg.descartados),
        "habla": _a_json(seg.habla),
    })
    _borrar_bloques_viejos(carpeta / CARPETA_BLOQUES_CELU)
    filas: list[dict[str, int | str]] = []
    for k, (inicio, fin) in enumerate(bloques, start=1):
        n = PRIMER_BLOQUE_CELU + k - 1
        rel = f"{CARPETA_BLOQUES_CELU}/bloque_{n}.mp3"
        try:
            audio.extraer_mp3(ruta, carpeta / rel, inicio, fin, frecuencia_hz=FRECUENCIA_HZ, kbps=KBPS)
        except audio.ErrorAudio as e:
            raise _error_ffmpeg(e) from e
        filas.append({"n": n, "inicio_ms": inicio, "fin_ms": fin, "ruta": rel})
    escribir_json(carpeta / "bloques.celu.json", filas)
    with ctx.base.transaccion() as conn:
        conn.execute("DELETE FROM bloques WHERE sesion_id = ? AND pista = ?", (sid, PISTA_CELU))
        conn.executemany(
            "INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta, estado, pista) "
            "VALUES (?, ?, ?, ?, ?, 'pendiente', ?)",
            [(sid, f["n"], f["inicio_ms"], f["fin_ms"], f["ruta"], PISTA_CELU) for f in filas],
        )
        guardar_datos_celu(conn, sid, {"duracion_medida_ms": seg.duracion_ms})
    if revisar:
        que = "todo el audio"
    elif tramos:
        que = f"sólo donde el principal no tiene audio ({len(tramos)} tramo(s))"
    else:
        que = "nada: el principal tiene audio en toda la clase"
    resumen = (f"{len(filas)} bloque(s) del celu ({que}); desfase {desfase} ms; voz {ms_a_hhmmss(seg.voz_ms)} "
               f"de {ms_a_hhmmss(seg.duracion_ms)}")
    ctx.log_sesion(sid, f"segmentar celu: {resumen}")
    return filas, resumen


# --- plan de cortes (puro, sin ffmpeg) ---------------------------------------------------------

def planificar(
    duracion_ms: int,
    silencios: list[Tramo],
    *,
    bloque_ms: int,
    ventana_ms: int,
    largo_ms: int,
    estrictos: list[Tramo] | None = None,
    margen_ms: int = MARGEN_MS,
    voz_min_ms: int = VOZ_MIN_MS,
) -> Segmentacion:
    """Decide los bloques a partir de la duración y los silencios detectados (todo en ms).

    Con `estrictos` (silencios con el umbral más bajo), cada silencio largo se reemplaza por lo
    que también es silencio estricto: sólo se descarta lo que siga midiendo ≥ largo_ms; lo demás
    cuenta como habla y sus silencios estrictos más cortos quedan como lugares donde cortar.
    """
    if bloque_ms <= 0:
        raise ValueError("bloque_s tiene que ser mayor que 0")
    duracion_ms = max(0, int(duracion_ms))
    silencios = _normalizar(silencios, duracion_ms)
    largos_ms = sum(b - a for a, b in silencios if b - a >= largo_ms)
    if estrictos is not None:
        silencios = _confirmar_largos(silencios, _normalizar(estrictos, duracion_ms), largo_ms)
    descartados = [s for s in silencios if s[1] - s[0] >= largo_ms]
    largos = set(descartados)
    habla = _complemento(silencios, duracion_ms)
    minimo = bloque_ms // 4

    bloques: list[Tramo] = []
    duros = 0
    for t0, t1 in _tramos(duracion_ms, descartados, margen_ms):
        internos = [s for s in silencios if s not in largos and t0 <= s[0] and s[1] <= t1]
        inicio = t0
        while t1 - inicio > bloque_ms + minimo:
            objetivo = inicio + bloque_ms
            corte = _elegir_corte(
                internos, objetivo,
                desde=max(objetivo - ventana_ms, inicio + minimo),
                hasta=min(objetivo + ventana_ms, t1 - minimo),
            )
            if corte is None:
                corte = objetivo
                duros += 1
            bloques.append((inicio, corte))
            inicio = corte
        bloques.append((inicio, t1))

    bloques = [b for b in bloques if _solapamiento(b, habla) >= voz_min_ms]
    rescatado = largos_ms - sum(b - a for a, b in descartados)
    return Segmentacion(duracion_ms, silencios, descartados, habla, bloques, duros, max(0, rescatado))


def _confirmar_largos(silencios: list[Tramo], estrictos: list[Tramo], largo_ms: int) -> list[Tramo]:
    """Cada silencio largo pasa a ser sólo sus partes que también son silencio estricto.

    Las puntas sin confirmar de menos de BORDE_MS se pegan a la pieza vecina: el detector corre
    los bordes unos ms entre un umbral y otro. Los silencios cortos quedan como están.
    """
    salida: list[Tramo] = []
    for a, b in silencios:
        if b - a < largo_ms:
            salida.append((a, b))
            continue
        piezas = [(max(a, x), min(b, y)) for x, y in estrictos if min(b, y) > max(a, x)]
        if piezas and piezas[0][0] - a < BORDE_MS:
            piezas[0] = (a, piezas[0][1])
        if piezas and b - piezas[-1][1] < BORDE_MS:
            piezas[-1] = (piezas[-1][0], b)
        salida += piezas
    return salida


def _elegir_corte(silencios: list[Tramo], objetivo: int, *, desde: int, hasta: int) -> int | None:
    """Medio del silencio más cercano al objetivo en [desde, hasta]; si empatan, el más largo."""
    candidatos = [
        (abs(medio - objetivo), b - a, medio)
        for a, b in silencios
        if desde <= (medio := (a + b) // 2) <= hasta
    ]
    if not candidatos:
        return None
    mejor = min(c[0] for c in candidatos)
    empatados = [c for c in candidatos if c[0] - mejor <= EMPATE_MS]
    return min(empatados, key=lambda c: (-c[1], c[0]))[2]


def _normalizar(silencios: list[Tramo], duracion_ms: int) -> list[Tramo]:
    """Recorta a [0, duración], ordena y une silencios que se pisan o se tocan."""
    limpios = sorted(
        (max(0, int(a)), min(duracion_ms, int(b))) for a, b in silencios
    )
    unidos: list[Tramo] = []
    for a, b in limpios:
        if b <= a:
            continue
        if unidos and a <= unidos[-1][1]:
            unidos[-1] = (unidos[-1][0], max(unidos[-1][1], b))
        else:
            unidos.append((a, b))
    return unidos


def _complemento(silencios: list[Tramo], duracion_ms: int) -> list[Tramo]:
    """Tramos con habla: lo que no es silencio (los silencios ya vienen normalizados)."""
    habla: list[Tramo] = []
    cursor = 0
    for a, b in silencios:
        if a > cursor:
            habla.append((cursor, a))
        cursor = max(cursor, b)
    if duracion_ms > cursor:
        habla.append((cursor, duracion_ms))
    return habla


def _tramos(duracion_ms: int, descartados: list[Tramo], margen_ms: int) -> list[Tramo]:
    """Partes del audio entre silencios largos (con un margen chico adentro de cada silencio)."""
    tramos: list[Tramo] = []
    inicio = 0
    for a, b in descartados:
        m = max(0, min(margen_ms, (b - a) // 2))
        fin = a + m if a > 0 else 0
        if fin > inicio:
            tramos.append((inicio, fin))
        inicio = b - m if b < duracion_ms else duracion_ms
    if duracion_ms > inicio:
        tramos.append((inicio, duracion_ms))
    return tramos


def _solapamiento(tramo: Tramo, otros: list[Tramo]) -> int:
    a, b = tramo
    return sum(max(0, min(b, y) - max(a, x)) for x, y in otros)


# --- helpers -----------------------------------------------------------------------------------

def _ms(segundos: float) -> int:
    return int(round(float(segundos) * 1000))


def _a_json(tramos: list[Tramo]) -> list[dict[str, int]]:
    return [{"inicio_ms": a, "fin_ms": b} for a, b in tramos]


def _borrar_bloques_viejos(carpeta: Path) -> None:
    """La carpeta bloques/ es sólo de esta etapa: se vacía antes de cortar de nuevo."""
    try:
        if carpeta.exists():
            shutil.rmtree(carpeta)
        carpeta.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ErrorEtapa(f"no pude borrar los bloques viejos: {e}") from e


def _error_ffmpeg(e: audio.ErrorAudio) -> ErrorEtapa:
    """ErrorAudio → ErrorEtapa; reintentable sólo si parece transitorio."""
    texto = str(e)
    transitorio = isinstance(e.__cause__, subprocess.TimeoutExpired) or any(
        t in texto.lower() for t in _TRANSITORIOS
    )
    return ErrorEtapa(texto, reintentable=transitorio)
