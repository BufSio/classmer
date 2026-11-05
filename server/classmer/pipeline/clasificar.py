"""Etapa 5: clasificar la materia (sólo si la sesión quedó en Auto).

Con materia ya asignada no llama a nada y sigue a `revisar_transcript`. Si no, manda extractos del
transcript y las materias activas al clasificador de `clasificar.motor` (Claude por la API o por el
plan Max según la facturación, o Gemini Flash); con confianza ≥ clasificar.umbral_confianza asigna
la materia (origen_materia queda 'auto') y sigue; si no, la sesión queda esperando al usuario.

clasificacion.json y pipeline.log dicen con qué se clasificó: motor, modelo, esfuerzo, facturación
(api o max) y costo (real con la API, "equivalente API" con el plan Max).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from classmer.ia.clasificador import candidatas, crear_clasificador, extracto_transcript
from classmer.ia.facturacion import texto_costo
from classmer.pipeline import Contexto, ErrorEtapa, EsperandoUsuario, Resultado
from classmer.util import ahora_iso, escribir_json, leer_json

if TYPE_CHECKING:
    from classmer.cola import Job


def ejecutar(ctx: Contexto, job: Job) -> Resultado:
    sid = job.sesion_id
    if sid is None:
        raise ErrorEtapa("clasificar necesita sesion_id", reintentable=False)
    cfg = ctx.cfg
    with ctx.base.conexion() as conn:
        sesion = conn.execute("SELECT materia_id, duracion_ms FROM sesiones WHERE id = ?", (sid,)).fetchone()
    if sesion is None:
        raise ErrorEtapa("no existe la sesión", reintentable=False)
    if sesion["materia_id"]:
        return Resultado(siguientes=[("revisar_transcript", {})], log=f"materia ya asignada: {sesion['materia_id']}")

    comando = f"uv run classmer asignar {sid} --materia <materia>"
    materias = candidatas(cfg)
    if not materias:
        raise EsperandoUsuario(f"No hay materias en config.yaml para clasificar. Agregalas y asigná con: {comando}")
    segmentos = _segmentos(ctx, sid)
    texto = extracto_transcript(segmentos)
    if not texto.strip():
        raise EsperandoUsuario(f"No pude determinar la materia: el transcript está vacío. Asignala con: {comando}")
    duracion = sesion["duracion_ms"] or max((int(s.get("fin_ms") or 0) for s in segmentos), default=0)

    fabrica = ctx.servicios.clasificador or crear_clasificador
    clasificador = fabrica(cfg)
    try:
        r = clasificador.clasificar(texto, materias, duracion_ms=duracion or None)
    except Exception as e:
        _avisos(ctx, sid, getattr(e, "avisos", None))  # p. ej. "API sin créditos: sigo con el plan Max"
        raise
    _avisos(ctx, sid, getattr(r, "avisos", None))
    # Los clasificadores falsos de los tests pueden devolver el resultado viejo (sin costo/facturación).
    motor = getattr(clasificador, "motor", None)
    costo = getattr(r, "costo", None)
    facturacion = getattr(r, "facturacion", None)
    ids = {m.id for m in materias}
    valida = r.materia_id in ids
    datos: dict[str, Any] = {
        "materia_id": r.materia_id if valida else None,
        "confianza": r.confianza,
        "motivo": r.motivo if valida or r.materia_id is None
        else f"{r.motivo} (el modelo respondió una materia inexistente: {r.materia_id!r})",
        "modelo": r.modelo,
        "motor": motor,
    }
    if motor == "claude":
        datos["esfuerzo"] = getattr(clasificador, "esfuerzo", None)
    datos.update(facturacion=facturacion, costo=costo, uso=dict(r.uso or {}))
    escribir_json(ctx.sesion_dir(sid) / "clasificacion.json", datos)
    tokens = (" · tokens " + ", ".join(f"{k}={v}" for k, v in r.uso.items())) if r.uso else ""
    pago = f", {texto_costo(costo, facturacion)}" if facturacion or costo is not None else ""
    resumen = (f"clasificación: {r.materia_id or 'ninguna'} (confianza {r.confianza:.2f}, {r.modelo}{pago}): "
               f"{r.motivo}")
    ctx.log_sesion(sid, resumen + tokens)

    # El usuario puede asignar la materia a mano mientras corre la llamada (`classmer asignar`):
    # sólo se escribe si sigue sin materia, y la manual nunca se pisa.
    if valida and r.confianza >= cfg.clasificar.umbral_confianza:
        with ctx.base.conexion() as conn:
            cur = conn.execute(
                "UPDATE sesiones SET materia_id = ?, confianza = ?, actualizado = ? "
                "WHERE id = ? AND materia_id IS NULL",
                (r.materia_id, r.confianza, ahora_iso(cfg.zona_horaria), sid))
        if not cur.rowcount:
            resumen += " · no se asigna: la materia ya se asignó a mano"
            ctx.log_sesion(sid, "clasificación descartada: la materia se asignó a mano durante la llamada")
        return Resultado(siguientes=[("revisar_transcript", {})], log=resumen + tokens, motor=r.modelo, costo=costo,
                         facturacion=facturacion)

    with ctx.base.conexion() as conn:
        conn.execute("UPDATE sesiones SET confianza = ?, actualizado = ? WHERE id = ? AND materia_id IS NULL",
                     (r.confianza, ahora_iso(cfg.zona_horaria), sid))
        actual = conn.execute("SELECT materia_id FROM sesiones WHERE id = ?", (sid,)).fetchone()
    if actual is not None and actual["materia_id"]:
        aviso = f"materia asignada a mano durante la clasificación: {actual['materia_id']}"
        ctx.log_sesion(sid, aviso)
        return Resultado(siguientes=[("revisar_transcript", {})], log=f"{aviso} · {resumen}", motor=r.modelo, costo=costo,
                         facturacion=facturacion)
    sugerida = f" Sugerida: {r.materia_id}." if valida else ""
    raise EsperandoUsuario(
        f"No pude determinar la materia (confianza {r.confianza:.2f}: {r.motivo[:200]}).{sugerida} "
        f"Asignala con: uv run classmer asignar {sid} --materia <materia> "
        f"(opciones: {', '.join(sorted(ids))})",
        motor=r.modelo, costo=costo, facturacion=facturacion,
    )


def _avisos(ctx: Contexto, sid: str, avisos: Any) -> None:
    for aviso in avisos or []:
        ctx.log_sesion(sid, f"clasificar: {aviso}")


def _segmentos(ctx: Contexto, sid: str) -> list[dict[str, Any]]:
    """transcript.json (de linea_de_tiempo); si no está, las filas de `segmentos`."""
    datos = leer_json(ctx.sesion_dir(sid) / "transcript.json")
    if isinstance(datos, list) and datos:
        return [s for s in datos if isinstance(s, dict)]
    with ctx.base.conexion() as conn:
        filas = conn.execute(
            "SELECT inicio_ms, fin_ms, hablante, texto FROM segmentos WHERE sesion_id = ? ORDER BY inicio_ms",
            (sid,),
        ).fetchall()
    return [dict(f) for f in filas]
