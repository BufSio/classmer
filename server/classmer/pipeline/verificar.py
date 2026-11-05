"""Etapa 1 — verificar (§6): hash y duración del audio, fotos y eventos recibidos.

Falla (sin reintentar) si el audio no está, si el hash no coincide con el de finalizar o si
ffprobe no lo puede leer. Lo demás (duración muy distinta de la declarada, fotos que faltan,
menos eventos que los declarados) sólo se avisa. Escribe `verificacion.json`: su `duracion_ms`
es siempre la medida (la del celu va en `duracion_declarada_ms`); apunte la usa para meta.json.

En una sesión importada ("Importar clase"), además (importador.completar_importada): la duración de
la sesión pasa a ser la medida (la declarada puede venir null o distinta: nunca traba), un archivo sin
pista de audio (un video sin sonido) falla sin reintentar, un audio.bin toma la extensión de su
formato (si ese rename se cortó a mitad, reconciliar_audio corrige la base), las fotos sin minuto se
ubican por su hora EXIF, se arma la copia para el reproductor del panel si hace falta y se escribe
importacion.json si no estaba.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from classmer import audio, importador
from classmer.db import transaccion
from classmer.pipeline import Contexto, ErrorEtapa, Resultado, actualizar_sesion
from classmer.sesiones import SOLO_EVENTOS, NoExiste, fila_sesion, id_audio, ruta_audio, tamano
from classmer.util import escribir_json, ms_a_hhmmss, sha256_archivo

if TYPE_CHECKING:
    from classmer.cola import Job

# La duración medida "difiere mucho" de la declarada si la diferencia supera esto.
TOLERANCIA_DURACION_MS = 10_000
TOLERANCIA_DURACION_FRACCION = 0.03


def difiere_mucho(medida_ms: int, declarada_ms: int) -> bool:
    tolerancia = max(TOLERANCIA_DURACION_MS, TOLERANCIA_DURACION_FRACCION * declarada_ms)
    return abs(medida_ms - declarada_ms) > tolerancia


def ejecutar(ctx: Contexto, job: Job) -> Resultado:
    sid = job.sesion_id
    if not sid:
        raise ErrorEtapa("verificar necesita una sesión", reintentable=False)
    cfg = ctx.cfg
    carpeta = ctx.sesion_dir(sid)
    with ctx.base.conexion() as conn:
        try:
            sesion = fila_sesion(conn, sid)
        except NoExiste as e:
            raise ErrorEtapa(f"no existe la sesión {sid}", reintentable=False) from e
        ruta = ruta_audio(cfg, conn, sid)
        fotos = conn.execute(
            "SELECT id, ruta FROM assets WHERE sesion_id = ? AND tipo = 'foto' ORDER BY offset_ms, creado",
            (sid,),
        ).fetchall()
        eventos = conn.execute(
            f"SELECT COUNT(*) FROM assets WHERE sesion_id = ? AND {SOLO_EVENTOS}", (sid,)
        ).fetchone()[0]

    importada = bool(sesion["importada"])
    declarada = sesion["duracion_ms"]
    if importada:
        declarada = importador.duracion_declarada(ctx, sid, declarada)
        ruta = importador.reconciliar_audio(ctx, sid)  # un rename de audio.bin que se cortó a mitad
    bytes_audio = tamano(ruta)
    if not ruta.is_file() or bytes_audio == 0:
        raise ErrorEtapa(f"no está el audio de la sesión ({ruta.name})", reintentable=False)

    # --- hash ---
    sha = sha256_archivo(ruta)
    esperado = (sesion["sha256"] or "").strip().lower()
    informe: dict[str, Any] = {
        "sha256": sha,
        "bytes": bytes_audio,
        "duracion_ms": None,
        "duracion_declarada_ms": declarada,
        "fotos_faltantes": [],
        "eventos": eventos,
        "eventos_totales": sesion["eventos_totales"],
        "avisos": [],
    }
    if esperado and sha != esperado:
        informe["error"] = "hash distinto"
        informe["sha256_esperado"] = esperado
        escribir_json(carpeta / "verificacion.json", informe)
        raise ErrorEtapa(
            "el hash del audio no coincide con el que mandó el celu al finalizar",
            reintentable=False,
        )

    # --- duración ---
    if shutil.which("ffprobe") is None:
        raise ErrorEtapa("no encuentro ffprobe en el PATH")
    try:
        medida = audio.duracion_ms(ruta)
    except audio.ErrorAudio as e:
        if "tardó demasiado" in str(e) or str(e).startswith("no encuentro"):
            raise ErrorEtapa(f"no pude medir {ruta.name}: {e}") from e
        raise ErrorEtapa(f"audio ilegible: {e}", reintentable=False) from e
    if medida <= 0:
        raise ErrorEtapa(f"audio ilegible: {ruta.name} dura 0 ms", reintentable=False)
    informe["duracion_ms"] = medida

    # --- avisos ---
    avisos: list[str] = informe["avisos"]
    if declarada is not None and difiere_mucho(medida, declarada):
        avisos.append(
            f"la duración medida ({ms_a_hhmmss(medida)}) difiere mucho de la declarada "
            f"({ms_a_hhmmss(declarada)})" + (": se usa la medida" if importada else "")
        )
    for f in fotos:
        if not f["ruta"] or not (carpeta / f["ruta"]).is_file():
            informe["fotos_faltantes"].append(f["ruta"] or f["id"])
    if informe["fotos_faltantes"]:
        avisos.append(f"faltan {len(informe['fotos_faltantes'])} archivo(s) de fotos")
    if sesion["eventos_totales"] is not None and eventos < sesion["eventos_totales"]:
        avisos.append(f"llegaron {eventos} eventos de {sesion['eventos_totales']} declarados")

    # --- sesión importada: fotos por EXIF, audio.bin, copia para el panel, importacion.json ---
    if importada:
        extra = importador.completar_importada(ctx, sid, duracion_ms=medida)
        avisos += extra["avisos"]

    # --- completar lo que falta (importadas, o sin duración declarada) ---
    campos: dict[str, Any] = {}
    if not esperado:
        campos["sha256"] = sha
    if importada or declarada is None:
        campos["duracion_ms"] = medida
    if sesion["bytes_totales"] is None:
        campos["bytes_totales"] = bytes_audio
    with ctx.base.conexion() as conn, transaccion(conn):
        actualizar_sesion(conn, sid, cfg.zona_horaria, **campos)
        conn.execute(
            "UPDATE assets SET sha256 = COALESCE(sha256, ?), bytes_recibidos = ? WHERE id = ?",
            (sha, bytes_audio, id_audio(sid)),
        )

    escribir_json(carpeta / "verificacion.json", informe)
    for aviso in avisos:
        ctx.log_sesion(sid, f"verificar: aviso: {aviso}")
    resumen = (
        f"audio ok ({ms_a_hhmmss(medida)}, {bytes_audio} bytes), {eventos} eventos"
        + (f", {len(avisos)} aviso(s)" if avisos else "")
    )
    return Resultado(siguientes=[("segmentar", {})], log=resumen)
