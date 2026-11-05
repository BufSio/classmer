"""Estado de la revisión del transcript de una clase (§15) y cómo se aplica.

Todo vive en `sesiones/<id>/revision/`:
- `revision.json`: las líneas revisadas (con su minuto, rol, texto, el crudo y el tramo del celu), las
  dudas (versiones, propuesta, lo que se volvió a escuchar, la respuesta del usuario) y el resumen (modo,
  modelo, costo, estado: esperando | aplicada | apagada).
- `conciliar.json` y `limpiar.json`: el registro de cada cambio, separado por paso (el minuto, las
  versiones, la elección, el motivo y la confianza). `escuchar.json`: lo que Gemini volvió a escuchar
  y cómo se resolvió cada duda. `cambios.md`: lo mismo para leer.

Aplicar (`aplicar`) escribe el revisado como el transcript de la clase: transcript.json, transcript.md,
timeline.md (sus líneas de habla, lo demás queda) y la tabla `segmentos` (búsqueda y MCP). Los crudos
quedan en transcript.crudo.*, timeline.crudo.md y transcript.celu.*. El profe va con su nombre (el
`profesor` de la materia al aplicar, o "Profe") y los alumnos como "Alumno".
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from classmer import cola
from classmer.config import NOMBRE_ALUMNO, NOMBRE_PROFE_DEFAULT
from classmer.db import transaccion
from classmer.pipeline import actualizar_sesion
from classmer.util import ahora_iso, escribir_atomico, escribir_json, leer_json, ms_a_hhmmss

if TYPE_CHECKING:
    from classmer.config import Config, Materia
    from classmer.pipeline import Contexto

CARPETA = "revision"
ARCHIVO = "revision.json"
CRUDOS = ("transcript.json", "transcript.md", "timeline.md")
# Cómo se aplicó (`aplicada.como`), para leer en cambios.md. "sin_dudas" no lleva línea.
COMO_APLICADA = {"automatica": "automáticamente", "listo": "con \"Listo\"", "sin_revisar": "sin revisar"}


class NoSePuede(Exception):
    """La clase no está esperando la revisión (→ 409 / mensaje en el panel)."""


def carpeta(cfg: Config, sid: str) -> Path:
    return cfg.sesion_dir(sid) / CARPETA


def cargar(cfg: Config, sid: str) -> dict[str, Any] | None:
    datos = leer_json(carpeta(cfg, sid) / ARCHIVO)
    return datos if isinstance(datos, dict) and isinstance(datos.get("lineas"), list) else None


def guardar(cfg: Config, sid: str, rev: dict[str, Any]) -> None:
    escribir_json(carpeta(cfg, sid) / ARCHIVO, rev)


def pendientes(rev: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not rev:
        return []
    return [d for d in rev.get("dudas") or [] if d.get("estado") == "pendiente"]


def dudas_pendientes(cfg: Config, sid: str) -> int:
    """Cuántas dudas esperan al usuario (GET /api/sesiones/{id}, `dudas`)."""
    rev = cargar(cfg, sid)
    if not rev or rev.get("estado") != "esperando":
        return 0
    return len(pendientes(rev))


# --- crudos ----------------------------------------------------------------------------------------

def ruta_crudo(cfg: Config, sid: str, nombre: str) -> Path:
    """transcript.json → transcript.crudo.json (y así)."""
    base, _, extension = nombre.rpartition(".")
    return cfg.sesion_dir(sid) / f"{base}.crudo.{extension}"


def asegurar_crudos(cfg: Config, sid: str) -> None:
    """Las clases de antes de la revisión no tienen las copias crudas: son los archivos de siempre (que
    nunca se revisaron)."""
    for nombre in CRUDOS:
        origen = cfg.sesion_dir(sid) / nombre
        destino = ruta_crudo(cfg, sid, nombre)
        if not destino.is_file() and origen.is_file():
            shutil.copyfile(origen, destino)


def restaurar_crudo(ctx: Contexto, sid: str) -> bool:
    """Con la revisión apagada: si quedó aplicada una revisión vieja, vuelve el crudo (archivos y tabla).
    Devuelve True si cambió algo."""
    cfg = ctx.cfg
    asegurar_crudos(cfg, sid)
    cambio = False
    for nombre in CRUDOS:
        actual, crudo = cfg.sesion_dir(sid) / nombre, ruta_crudo(cfg, sid, nombre)
        if crudo.is_file() and (not actual.is_file() or actual.read_bytes() != crudo.read_bytes()):
            shutil.copyfile(crudo, actual)
            cambio = True
    if cambio:
        segmentos = leer_json(ruta_crudo(cfg, sid, "transcript.json"), []) or []
        with ctx.base.transaccion() as conn:
            _reemplazar_segmentos(conn, sid, [s for s in segmentos if isinstance(s, dict)])
        rev = cargar(cfg, sid)
        if rev is not None and rev.get("estado") != "apagada":
            rev["estado"] = "apagada"
            guardar(cfg, sid, rev)
    return cambio


# --- aplicar ---------------------------------------------------------------------------------------

def nombre_de(rol: str | None, materia: Materia | None) -> str:
    if rol == "alumno":
        return NOMBRE_ALUMNO
    return materia.nombre_profe if materia is not None else NOMBRE_PROFE_DEFAULT


def segmentos_revisados(rev: dict[str, Any], materia: Materia | None) -> list[dict[str, Any]]:
    """Las líneas revisadas como segmentos del transcript (sin las vacías)."""
    salida = []
    for ln in rev["lineas"]:
        texto = " ".join(str(ln.get("texto") or "").split())
        if ln.get("eliminada") or not texto:
            continue
        s: dict[str, Any] = {"bloque": ln.get("bloque") or 0, "inicio_ms": int(ln["inicio_ms"]),
                             "fin_ms": int(ln.get("fin_ms") or ln["inicio_ms"]),
                             "hablante": nombre_de(ln.get("rol"), materia), "rol": ln.get("rol") or "profe",
                             "texto": texto}
        if ln.get("origen") == "celu":
            s["origen"] = "celu"
        salida.append(s)
    salida.sort(key=lambda s: s["inicio_ms"])
    return salida


def aplicar(ctx: Contexto, sid: str, rev: dict[str, Any], materia: Materia | None) -> list[dict[str, Any]]:
    """Escribe el revisado como el transcript de la clase (archivos y tabla `segmentos`)."""
    from classmer.pipeline.linea_de_tiempo import _linea_habla, rehacer_timeline

    cfg = ctx.cfg
    sdir = cfg.sesion_dir(sid)
    asegurar_crudos(cfg, sid)
    crudos = leer_json(ruta_crudo(cfg, sid, "transcript.json"), []) or []
    segmentos = segmentos_revisados(rev, materia)
    escribir_json(sdir / "transcript.json", segmentos)
    try:
        cabeza = ruta_crudo(cfg, sid, "transcript.md").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        cabeza = "# Transcript"
    escribir_atomico(sdir / "transcript.md", "\n".join([cabeza, *(_linea_habla(s) for s in segmentos)]) + "\n")
    timeline_crudo = ruta_crudo(cfg, sid, "timeline.md")
    if timeline_crudo.is_file():
        texto = timeline_crudo.read_text(encoding="utf-8")
        escribir_atomico(sdir / "timeline.md", rehacer_timeline(texto, crudos, segmentos))
    with ctx.base.transaccion() as conn:
        _reemplazar_segmentos(conn, sid, segmentos)
    return segmentos


def _reemplazar_segmentos(conn: sqlite3.Connection, sid: str, segmentos: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM segmentos WHERE sesion_id = ?", (sid,))
    conn.executemany(
        "INSERT INTO segmentos (sesion_id, bloque, inicio_ms, fin_ms, hablante, texto) VALUES (?, ?, ?, ?, ?, ?)",
        [(sid, int(s.get("bloque") or 0), int(s["inicio_ms"]), int(s.get("fin_ms") or s["inicio_ms"]),
          s.get("hablante"), s["texto"]) for s in segmentos])


# --- las dudas desde el panel ----------------------------------------------------------------------

def _duda(rev: dict[str, Any], duda_id: str) -> dict[str, Any]:
    for d in rev.get("dudas") or []:
        if d.get("id") == duda_id:
            return d
    raise KeyError(duda_id)


def _linea(rev: dict[str, Any], linea_id: str) -> dict[str, Any] | None:
    return next((ln for ln in rev["lineas"] if ln.get("id") == linea_id), None)


def poner_texto(ln: dict[str, Any], texto: str) -> None:
    """El texto elegido para la línea de una duda. Si limpiar la había vaciado (eliminada), con texto
    vuelve al transcript."""
    ln["texto"] = texto
    if texto.strip():
        ln["eliminada"] = False


def responder(cfg: Config, sid: str, respuestas: dict[str, dict[str, Any]]) -> int:
    """Guarda lo que eligió el usuario: {duda: {"opcion": i}} (una de las versiones) o {duda: {"texto": "…"}}
    (editada). Una respuesta vacía la vuelve a dejar pendiente. Devuelve cuántas guardó."""
    rev = cargar(cfg, sid)
    if rev is None:
        raise NoSePuede("esta clase no tiene revisión del transcript")
    guardadas = 0
    for duda_id, r in respuestas.items():
        try:
            d = _duda(rev, duda_id)
        except KeyError:
            continue
        texto, fuente = None, None
        if r.get("texto") is not None and " ".join(str(r["texto"]).split()):
            texto, fuente = " ".join(str(r["texto"]).split()), "usuario"
            opciones = [o["texto"] for o in d["opciones"]]
            if texto in opciones:
                fuente = d["opciones"][opciones.index(texto)]["fuente"]
        elif r.get("opcion") is not None:
            try:
                o = d["opciones"][int(r["opcion"])]
            except (ValueError, IndexError, TypeError):
                continue
            texto, fuente = o["texto"], o["fuente"]
        if texto is None:
            if d.get("estado") == "respondida":
                d["estado"], d["respuesta"] = "pendiente", None
                guardadas += 1
            continue
        d["respuesta"] = {"texto": texto, "fuente": fuente, "momento": ahora_iso(cfg.zona_horaria)}
        d["estado"] = "respondida"
        ln = _linea(rev, d["linea"])
        if ln is not None:
            poner_texto(ln, texto)
        guardadas += 1
    guardar(cfg, sid, rev)
    return guardadas


def terminar(ctx: Contexto, sid: str, *, sin_revisar: bool = False) -> int:
    """"Listo" o "Generar sin revisar": aplica la revisión y encola el apunte. Las dudas que el usuario
    respondió quedan como las respondió; las demás, con la propuesta del modelo (con "Generar sin
    revisar", todas las que no tocó). Devuelve cuántas quedaron con la propuesta del modelo."""
    cfg = ctx.cfg
    rev = cargar(cfg, sid)
    with ctx.base.conexion() as conn:
        fila = conn.execute("SELECT estado, espera, materia_id FROM sesiones WHERE id = ?", (sid,)).fetchone()
    if fila is None:
        raise NoSePuede("no existe la clase")
    if fila["estado"] != "esperando_usuario" or (fila["espera"] or "materia") != "revision" or rev is None:
        raise NoSePuede("la clase no está esperando la revisión del transcript")
    sin_responder = usar_propuestas(rev)
    materia = cfg.materia(fila["materia_id"])
    aplicar(ctx, sid, rev, materia)
    rev["estado"] = "aplicada"
    rev["aplicada"] = {"momento": ahora_iso(cfg.zona_horaria), "como": "sin_revisar" if sin_revisar else "listo",
                       "con_propuesta": sin_responder}
    guardar(cfg, sid, rev)
    escribir_cambios_md(cfg, sid, rev)
    with ctx.base.conexion() as conn, transaccion(conn):
        actual = conn.execute("SELECT estado, espera FROM sesiones WHERE id = ?", (sid,)).fetchone()
        if actual is None or actual["estado"] != "esperando_usuario" or actual["espera"] != "revision":
            raise NoSePuede("la clase cambió de estado mientras se aplicaba la revisión")
        actualizar_sesion(conn, sid, cfg.zona_horaria, estado="en_cola", etapa="apunte", error=None)
        cola.encolar(conn, "apunte", sid)
    ctx.log_sesion(sid, f"revisar_transcript: {'generar sin revisar' if sin_revisar else 'listo'} "
                        f"({sin_responder} duda(s) con la propuesta del modelo); el apunte queda en cola")
    return sin_responder


def usar_propuestas(rev: dict[str, Any]) -> int:
    """Cada duda sin responder toma la propuesta del modelo (en `rev`, sin aplicar: "Listo" y la revisión
    automática). Devuelve cuántas."""
    sin_responder = pendientes(rev)
    for d in sin_responder:
        ln = _linea(rev, d["linea"])
        if ln is not None:
            poner_texto(ln, d["opciones"][d["propuesta"]]["texto"])
    return len(sin_responder)


# --- registro para leer ----------------------------------------------------------------------------

def escribir_cambios_md(cfg: Config, sid: str, rev: dict[str, Any]) -> None:
    """revision/cambios.md: el resumen, las dudas y los cambios de cada paso, para leer."""
    c = carpeta(cfg, sid)
    lineas = [f"# Revisión del transcript — {rev.get('modo')}", ""]
    lineas.append(f"Modelo {rev.get('modelo')} (esfuerzo {rev.get('esfuerzo') or 'por defecto'}), "
                  f"{rev.get('pedidos', 0)} pedido(s), costo {rev.get('costo')!s} US$ ({rev.get('facturacion') or '?'}).")
    aplicada = rev.get("aplicada") or {}
    if rev.get("estado") == "aplicada" and aplicada.get("como") in COMO_APLICADA:
        lineas.append(f"Aplicada {COMO_APLICADA[aplicada['como']]}: {aplicada.get('con_propuesta', 0)} duda(s) "
                      "con la propuesta del modelo.")
    for aviso in rev.get("avisos") or []:
        lineas.append(f"- Aviso: {aviso}")
    lineas += ["", "## Dudas", ""]
    for d in rev.get("dudas") or []:
        lineas.append(f"- **{d['id']}** [{ms_a_hhmmss(d['inicio_ms'])}] ({d['paso']}, {d.get('estado')}) {d.get('motivo') or ''}")
        for k, o in enumerate(d["opciones"]):
            marca = " ← propuesta" if k == d.get("propuesta") else ""
            lineas.append(f"  - {o['fuente']}: {o['texto']}{marca}")
        for fuente, texto in (d.get("escuchado") or {}).items():
            lineas.append(f"  - escuchado ({fuente}): {texto}")
        if d.get("resolucion"):
            r = d["resolucion"]
            lineas.append(f"  - resolución ({r.get('confianza')}): {r.get('motivo')}")
        if d.get("respuesta"):
            lineas.append(f"  - Usuario: {d['respuesta']['texto']}")
    for paso in ("conciliar", "limpiar"):
        registro = leer_json(c / f"{paso}.json")
        if not isinstance(registro, dict):
            continue
        lineas += ["", f"## {paso.capitalize()}", ""]
        for x in registro.get("cambios") or []:
            if paso == "conciliar":
                lineas.append(f"- [{ms_a_hhmmss(x['inicio_ms'])}] {x['linea']} ({x['eleccion']}, {x['confianza']}): "
                              f"reloj «{x['reloj']}» / celu «{x.get('celu') or '—'}» → «{x['texto']}». {x['motivo']}")
            else:
                for cambio in x.get("cambios") or []:
                    lineas.append(f"- [{ms_a_hhmmss(x['inicio_ms'])}] {x['linea']} {cambio['tipo']} ({cambio['confianza']}): "
                                  f"«{cambio['antes']}» → «{cambio['despues']}». {cambio['motivo']}")
        for x in registro.get("solo_celu") or []:
            lineas.append(f"- [{ms_a_hhmmss(x['inicio_ms'])}] sólo en el celu ({'entra' if x['incluir'] else 'no entra'}): "
                          f"«{x['texto']}». {x['motivo']}")
    escribir_atomico(c / "cambios.md", "\n".join(lineas) + "\n")


def resumen_panel(rev: dict[str, Any] | None) -> dict[str, Any] | None:
    """Lo que muestra la página de la clase."""
    if rev is None:
        return None
    dudas = rev.get("dudas") or []
    return {
        "estado": rev.get("estado"), "modo": rev.get("modo"), "modelo": rev.get("modelo"),
        "costo": rev.get("costo"), "facturacion": rev.get("facturacion"),
        "pendientes": [d for d in dudas if d.get("estado") in ("pendiente", "respondida")],
        "resueltas": [d for d in dudas if d.get("estado") == "resuelta"],
        "cantidad": {"pendientes": len([d for d in dudas if d.get("estado") == "pendiente"]),
                     "respondidas": len([d for d in dudas if d.get("estado") == "respondida"]),
                     "resueltas": len([d for d in dudas if d.get("estado") == "resuelta"])},
        "desfase_ms": rev.get("desfase_ms"), "aplicada": rev.get("aplicada"), "avisos": rev.get("avisos") or [],
        "lineas": len(rev.get("lineas") or []),
    }


def como_json(rev: dict[str, Any]) -> str:
    return json.dumps(rev, ensure_ascii=False, indent=2)
