"""Etapa 4 — línea de tiempo (§6): une transcript, fotos, notas, marcas y adjuntos en `timeline.md`.

Salidas (en la carpeta de la sesión, ver docs/internos.md):
- `transcript.json` y `transcript.md`: los segmentos de todos los bloques, en orden.
- `fotos_pdf/pizarra_NN.jpg`: las fotos ordenadas por offset_ms, derechas (rotación EXIF),
  con el lado mayor ≤ 2400 px. Las fotos sin hora van al final.
- `timeline.json` y `timeline.md`: lo que lee el agente del apunte, una línea por cosa.

Además del habla y los eventos, timeline.md marca lo que no se transcribió, en su lugar:
- `[hh:mm:ss] SILENCIO hasta hh:mm:ss (no se transcribió)`: un silencio largo descartado
  (silencios.json "descartados").
- `[hh:mm:ss] AVISO: tramo con habla sin transcribir hasta hh:mm:ss`: en un bloque 'dudoso', un
  hueco con habla (VAD) y sin texto (transcriptor.huecos).

Y el material adjunto (PDF de slides, imágenes; classmer.adjuntos): `[hh:mm:ss] ADJUNTO adjuntos/…`
en su lugar si se adjuntó grabando; si no, en "# Material adjunto", al final. La regla es una sola
(`adjuntos.refrescar_timeline`): el apunte la vuelve a aplicar por si llegó otro después.

Segunda pista (§15, el audio del celu cuando el reloj es el principal):
- `transcript.celu.json` / `transcript.celu.md`: el transcript del celu, ya en el tiempo del principal
  (+ desfase_ms). Lo usa la revisión para conciliar.
- Respaldo: los segmentos del celu donde el principal no tiene audio (antes de que empiece o después
  de que se cortó) entran al transcript de la clase con `"origen": "celu"`, con la revisión prendida o
  apagada. Lo que cae antes del inicio del principal queda en el minuto 0.

Las copias `transcript.crudo.json`, `transcript.crudo.md` y `timeline.crudo.md` son lo que escribió
esta etapa: la revisión las lee (transcript.json, transcript.md y timeline.md pasan a ser los
revisados) y se pueden mirar desde el panel.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageOps

from classmer import adjuntos
from classmer.ia.transcriptor import SegmentoRel, huecos
from classmer.pipeline import Contexto, ErrorEtapa, Resultado
from classmer.pipeline.segmentar import PRIMER_BLOQUE_CELU
from classmer.pipeline.transcribir import habla_del_bloque
from classmer.sesiones import PISTA_CELU, NoExiste, datos_celu, fila_sesion
from classmer.util import (
    escribir_atomico,
    escribir_json,
    leer_json,
    ms_a_hhmmss,
    parsear_iso,
    tiempo_a_ms,
    zona,
)

if TYPE_CHECKING:
    from classmer.config import Config
    from classmer.cola import Job

# Fotos nativas (§7): el agente y el PDF usan la foto ORIGINAL. Sólo se toca si hay que rotarla
# (orientación EXIF ≠ 1), si supera los límites de la API de Claude o si no es JPEG.
LADO_MAX_API = 8000          # px: la API no acepta imágenes con un lado más grande
BYTES_MAX_API = 7_500_000    # ~10 MB en base64, el tope por imagen de la API
LADO_REDUCIDO = 4000         # a esto se lleva una foto que supera los límites (p. ej. 200 MP)
CALIDAD_JPEG = 95            # una sola recompresión, casi sin pérdida
CARPETA_FOTOS = "fotos_pdf"
SIN_MATERIA = "materia sin asignar"
SIN_TIPO = "tipo sin asignar"
TIPOS_LEGIBLES = {"teorica": "teórica", "practica": "práctica"}
TIPOS_EVENTO = ("foto", "nota", "marca", "pausa", "reanudar")
# A igual offset y sin hora del celu, la pausa va antes que el reanudar.
_ORDEN_TIPO = {"reanudar": 1}

# Las fotos son del propio celu (el S23 Ultra saca hasta 200 MP): subo el tope anti "bomba". Pillow
# sólo avisa hasta el doble (DecompressionBombWarning): procesar_foto corta en este tope.
LIMITE_PIXELES = 300_000_000
if Image.MAX_IMAGE_PIXELS is not None and Image.MAX_IMAGE_PIXELS < LIMITE_PIXELES:
    Image.MAX_IMAGE_PIXELS = LIMITE_PIXELES


# --- helpers públicos (también los usa publicar) -----------------------------------------------

def fecha_local(cfg: Config, inicio: str) -> str:
    """Fecha AAAA-MM-DD de `inicio` en la zona de Classmer."""
    try:
        return parsear_iso(inicio, cfg.zona_horaria).astimezone(zona(cfg.zona_horaria)).date().isoformat()
    except (ValueError, TypeError):
        return str(inicio)[:10]


def nombre_materia(cfg: Config, conn: sqlite3.Connection, materia_id: str | None) -> str | None:
    """Nombre visible de la materia: config, si no la tabla materias, si no el id."""
    if materia_id is None:
        return None
    materia = cfg.materia(materia_id)
    if materia is not None:
        return materia.nombre
    fila = conn.execute("SELECT nombre FROM materias WHERE id = ?", (materia_id,)).fetchone()
    return fila["nombre"] if fila else materia_id


def tipo_legible(tipo_clase: str | None) -> str:
    if not tipo_clase:
        return SIN_TIPO
    return TIPOS_LEGIBLES.get(tipo_clase, tipo_clase)


def encabezado(materia: str | None, fecha: str, tipo_clase: str | None, *, titulo: str = "Clase") -> str:
    """'# Clase: <materia> — <AAAA-MM-DD> — <tipo>' (primera línea de timeline.md)."""
    return f"# {titulo}: {materia or SIN_MATERIA} — {fecha} — {tipo_legible(tipo_clase)}"


def normalizar_hablante(hablante: str | None) -> str:
    """'profe', 'Profesora', 'docente' → PROFE; 'alumna', 'estudiante' → ALUMNO; vacío → DESCONOCIDO."""
    texto = " ".join((hablante or "").split())
    if not texto:
        return "DESCONOCIDO"
    bajo = texto.lower()
    if bajo.startswith(("prof", "docen")):
        return "PROFE"
    if bajo.startswith(("alum", "estud")):
        return "ALUMNO"
    if bajo.startswith(("descon", "unknown", "?")):
        return "DESCONOCIDO"
    return texto.upper()


def procesar_foto(origen: Path, destino: Path) -> tuple[int, int, str]:
    """Foto para el agente y el PDF (§7): la ORIGINAL, byte a byte. Sólo se recodifica (JPEG
    calidad 95, una vez) si tiene orientación EXIF ≠ 1 (Claude y \\includegraphics ignoran el tag:
    saldría acostada), si supera los límites de la API (se lleva a LADO_REDUCIDO px) o si no es JPEG.
    Devuelve (ancho, alto, acción) con acción ∈ {original, rotada, reducida, convertida}."""
    with Image.open(origen) as img:
        formato = img.format
        ancho, alto = img.size
        # Antes de getexif, que en un PNG decodifica la imagen entera. Pillow sólo avisa hasta el doble.
        if ancho * alto > LIMITE_PIXELES:
            raise Image.DecompressionBombError(
                f"imagen de {ancho * alto / 1e6:.0f} MP: supera el tope de {LIMITE_PIXELES / 1e6:.0f} MP")
        orientacion = img.getexif().get(0x0112, 1) or 1
    grande = max(ancho, alto) > LADO_MAX_API or origen.stat().st_size > BYTES_MAX_API
    if formato == "JPEG" and orientacion == 1 and not grande:
        escribir_atomico(destino, origen.read_bytes())
        return ancho, alto, "original"

    with Image.open(origen) as original:
        if grande and original.format == "JPEG":
            original.draft("RGB", (LADO_REDUCIDO, LADO_REDUCIDO))  # decodifica ya achicada
        img = ImageOps.exif_transpose(original)
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        fondo = Image.new("RGB", rgba.size, (255, 255, 255))
        fondo.paste(rgba, mask=rgba.getchannel("A"))
        img = fondo
    elif img.mode != "RGB":
        img = img.convert("RGB")
    if grande:
        img.thumbnail((LADO_REDUCIDO, LADO_REDUCIDO), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=CALIDAD_JPEG, optimize=True)
    escribir_atomico(destino, buf.getvalue())
    accion = "reducida" if grande else ("rotada" if orientacion != 1 else "convertida")
    return img.size[0], img.size[1], accion


# --- etapa -------------------------------------------------------------------------------------

def ejecutar(ctx: Contexto, job: Job) -> Resultado:
    sid = job.sesion_id
    if not sid:
        raise ErrorEtapa("linea_de_tiempo necesita una sesión", reintentable=False)
    cfg = ctx.cfg
    carpeta = ctx.sesion_dir(sid)
    avisos: list[str] = []
    with ctx.base.conexion() as conn:
        try:
            sesion = fila_sesion(conn, sid)
        except NoExiste as e:
            raise ErrorEtapa(f"no existe la sesión {sid}", reintentable=False) from e
        materia = nombre_materia(cfg, conn, sesion["materia_id"])
        bloques = _numeros_de_bloque(conn, sid, carpeta)
        bloques_celu = [int(f["n"]) for f in conn.execute(
            "SELECT n FROM bloques WHERE sesion_id = ? AND pista = ? ORDER BY n", (sid, PISTA_CELU))]
        assets = conn.execute(
            f"SELECT * FROM assets WHERE sesion_id = ? AND tipo IN ({','.join('?' * len(TIPOS_EVENTO))})",
            (sid, *TIPOS_EVENTO),
        ).fetchall()
        material = adjuntos.listar(conn, sid)

    fecha = fecha_local(cfg, sesion["inicio"])
    silencios = leer_json(carpeta / "silencios.json")
    sin_texto: list[tuple[int, int]] = []
    segmentos = _juntar_transcript(carpeta, bloques, avisos, silencios, sin_texto)
    respaldo = _transcript_celu(ctx, sid, carpeta, bloques_celu, materia, fecha, sesion["tipo_clase"],
                                silencios, sesion["duracion_ms"], avisos)
    if respaldo:
        segmentos = sorted(segmentos + respaldo, key=lambda s: (s["inicio_ms"], s["bloque"]))
    fotos, eventos = _ordenar_assets(assets, avisos)
    fotos_ok = _preparar_fotos(carpeta, fotos, avisos)
    nombre_de = {f["id"]: f["nombre"] for f in fotos_ok}

    # --- transcript ---
    escribir_json(carpeta / "transcript.json", segmentos)
    lineas_tr = [encabezado(materia, fecha, sesion["tipo_clase"], titulo="Transcript")]
    lineas_tr += [_linea_habla(s) for s in segmentos]
    escribir_atomico(carpeta / "transcript.md", "\n".join(lineas_tr) + "\n")
    escribir_json(carpeta / "transcript.crudo.json", segmentos)
    escribir_atomico(carpeta / "transcript.crudo.md", "\n".join(lineas_tr) + "\n")
    with ctx.base.transaccion() as conn:
        # Los del respaldo también son el transcript de la clase (búsqueda y MCP).
        conn.execute("DELETE FROM segmentos WHERE sesion_id = ? AND bloque >= ?", (sid, PRIMER_BLOQUE_CELU))
        conn.executemany(
            "INSERT INTO segmentos (sesion_id, bloque, inicio_ms, fin_ms, hablante, texto) VALUES (?, ?, ?, ?, ?, ?)",
            [(sid, s["bloque"], s["inicio_ms"], s["fin_ms"], s["hablante"], s["texto"]) for s in respaldo])

    # --- línea de tiempo ---
    items: list[tuple[tuple[int, int, int, int], str]] = []
    for i, s in enumerate(segmentos):
        items.append(((s["inicio_ms"] // 1000, 0, s["inicio_ms"], i), _linea_habla(s)))
    marcas = [(a, f"[{ms_a_hhmmss(a)}] SILENCIO hasta {ms_a_hhmmss(b)} (no se transcribió)")
              for a, b in _descartados(silencios)]
    marcas += [(a, f"[{ms_a_hhmmss(a)}] AVISO: tramo con habla sin transcribir hasta {ms_a_hhmmss(b)}")
               for a, b in sin_texto]
    for k, (inicio, linea) in enumerate(marcas):
        items.append(((inicio // 1000, 0, inicio, len(segmentos) + k), linea))
    eventos_json: list[dict[str, Any]] = []
    for j, e in enumerate(eventos):
        if e["tipo"] == "foto" and e["id"] not in nombre_de:
            continue  # no se pudo abrir: ya se avisó
        linea, evento = _evento(e, nombre_de)
        items.append(((e["offset_ms"] // 1000, 1, e["offset_ms"], j), linea))
        eventos_json.append(evento)
    items.sort(key=lambda x: x[0])

    lineas_tl = [encabezado(materia, fecha, sesion["tipo_clase"])]
    lineas_tl += [linea for _, linea in items]
    sin_hora = [f for f in fotos_ok if f["offset_ms"] is None]
    if sin_hora:
        lineas_tl.append("# Fotos sin hora")
        lineas_tl += [f"FOTO fotos/{f['nombre']}" for f in sin_hora]
    # Los adjuntos, con la misma regla que usa el apunte (con hora en su lugar; sin hora, al final).
    timeline_md = adjuntos.refrescar_timeline("\n".join(lineas_tl) + "\n", material)
    escribir_atomico(carpeta / "timeline.md", timeline_md)
    escribir_atomico(carpeta / "timeline.crudo.md", timeline_md)
    escribir_json(carpeta / "timeline.json", {"fotos": fotos_ok, "eventos": eventos_json,
                                              "adjuntos": [_adjunto_json(a) for a in material], "avisos": avisos})

    for aviso in avisos:
        ctx.log_sesion(sid, f"linea_de_tiempo: aviso: {aviso}")
    resumen = (f"{len(segmentos)} segmentos de {len(bloques)} bloque(s), {len(fotos_ok)} foto(s), "
               f"{len(eventos_json)} evento(s)" + (f", {len(material)} adjunto(s)" if material else "")
               + (f", {len(respaldo)} del celu donde el principal no tiene audio" if respaldo else "")
               + (f", {len(avisos)} aviso(s)" if avisos else ""))
    return Resultado(siguientes=[("clasificar", {})], log=resumen)


# --- segunda pista (§15) -------------------------------------------------------------------------

def _transcript_celu(ctx: Contexto, sid: str, carpeta: Path, bloques_celu: list[int], materia: str | None,
                     fecha: str, tipo_clase: str | None, silencios: Any, duracion_ms: int | None,
                     avisos: list[str]) -> list[dict[str, Any]]:
    """Escribe transcript.celu.json/.md (el celu en el tiempo del principal) y devuelve los segmentos
    de respaldo: los del celu que caen donde el principal no tiene audio. Sin celu, borra los viejos."""
    for viejo in ("transcript.celu.json", "transcript.celu.md"):
        (carpeta / viejo).unlink(missing_ok=True)
    if not bloques_celu:
        return []
    with ctx.base.conexion() as conn:
        datos = datos_celu(conn, sid)
    desfase = int((datos or {}).get("desfase_ms") or 0)
    propios: list[str] = []
    segmentos = _juntar_transcript(carpeta, bloques_celu, propios, leer_json(carpeta / "silencios.celu.json"))
    avisos += [f"celu: {a}" for a in propios]
    celu = []
    for s in segmentos:
        celu.append({**s, "inicio_ms": s["inicio_ms"] + desfase, "fin_ms": s["fin_ms"] + desfase, "origen": "celu"})
    escribir_json(carpeta / "transcript.celu.json", celu)
    lineas = [encabezado(materia, fecha, tipo_clase, titulo="Transcript del celu")]
    lineas += [_linea_habla({**s, "inicio_ms": max(0, s["inicio_ms"])}) for s in celu]
    escribir_atomico(carpeta / "transcript.celu.md", "\n".join(lineas) + "\n")

    fin_principal = (silencios.get("duracion_ms") if isinstance(silencios, dict) else None) or duracion_ms
    respaldo = []
    for s in celu:
        medio = (s["inicio_ms"] + s["fin_ms"]) // 2
        if medio < 0 or (fin_principal and medio > fin_principal):
            respaldo.append({**s, "inicio_ms": max(0, s["inicio_ms"]), "fin_ms": max(0, s["fin_ms"])})
    return respaldo


def segmentos_del_timeline(segmentos: list[dict[str, Any]]) -> set[str]:
    """Las líneas de habla que escribió esta etapa para esos segmentos (para separarlas del resto)."""
    return {_linea_habla(s) for s in segmentos}


def rehacer_timeline(texto: str, crudos: list[dict[str, Any]], nuevos: list[dict[str, Any]]) -> str:
    """timeline.md con las líneas de habla de `crudos` reemplazadas por las de `nuevos` (la revisión).

    Lo demás (fotos, notas, marcas, silencios, avisos, adjuntos y las secciones del final) queda igual
    y en su lugar; cada línea nueva va antes de lo que pasó en un segundo posterior (a igual segundo,
    el habla antes de los eventos, como en la línea de tiempo original)."""
    habla = segmentos_del_timeline(crudos)
    lineas = texto.splitlines()
    cabeza: list[str] = []
    cuerpo: list[tuple[int, str]] = []   # (segundo, línea) de lo que no es habla
    cola: list[str] = []
    for i, linea in enumerate(lineas):
        if i == 0 and linea.startswith("# "):
            cabeza.append(linea)
            continue
        if cola or (linea.startswith("# ") and i > 0):
            cola.append(linea)       # "# Fotos sin hora", "# Material adjunto" y lo que sigue
            continue
        if linea in habla:
            continue
        m = _MINUTO_RE.match(linea)
        cuerpo.append((tiempo_a_ms(m.group(1)) // 1000 if m else -1, linea))
    nuevas = sorted(((s["inicio_ms"] // 1000, s["inicio_ms"], k, _linea_habla(s)) for k, s in enumerate(nuevos)))
    salida = list(cabeza)
    j = 0
    for segundo, linea in cuerpo:
        while j < len(nuevas) and segundo >= 0 and nuevas[j][0] <= segundo and not _antes(linea):
            salida.append(nuevas[j][3])
            j += 1
        while j < len(nuevas) and segundo >= 0 and nuevas[j][0] < segundo:
            salida.append(nuevas[j][3])
            j += 1
        salida.append(linea)
    salida += [n[3] for n in nuevas[j:]]
    salida += cola
    return "\n".join(salida) + "\n"


_MINUTO_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] ")


def _antes(linea: str) -> bool:
    """SILENCIO y AVISO van con el habla del mismo segundo, en su orden: el habla no se adelanta."""
    resto = linea[11:] if len(linea) > 11 else ""
    return resto.startswith(("SILENCIO ", "AVISO:"))


def _adjunto_json(a: dict[str, Any]) -> dict[str, Any]:
    """Un adjunto en timeline.json: `nombre` es el de LaTeX (input/adjuntos/ y la carpeta publicada)."""
    return {"id": a["id"], "nombre": adjuntos.nombre_latex(a), "nombre_original": a["nombre_original"],
            "tipo": a["tipo"], "offset_ms": a["offset_ms"], "paginas": a.get("paginas")}


# --- transcript --------------------------------------------------------------------------------

def _numeros_de_bloque(conn: sqlite3.Connection, sid: str, carpeta: Path) -> list[int]:
    """Bloques del audio principal: la tabla bloques; si está vacía, bloques.json; si no, lo que haya
    (sin los del celu, que van desde el 101)."""
    filas = conn.execute("SELECT n FROM bloques WHERE sesion_id = ? AND pista IS NULL ORDER BY n", (sid,)).fetchall()
    if filas:
        return [int(f["n"]) for f in filas]
    datos = leer_json(carpeta / "bloques.json")
    if isinstance(datos, list) and datos:
        return sorted(int(b["n"]) for b in datos if isinstance(b, dict) and "n" in b)
    numeros = []
    for ruta in (carpeta / "transcript").glob("bloque_*.json"):
        try:
            numeros.append(int(ruta.stem.split("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return sorted(n for n in numeros if n < PRIMER_BLOQUE_CELU)


def _juntar_transcript(carpeta: Path, bloques: list[int], avisos: list[str], silencios: Any = None,
                       sin_texto: list[tuple[int, int]] | None = None) -> list[dict[str, Any]]:
    """Segmentos de todos los bloques en orden. En `sin_texto` deja los huecos (ms absolutos) de
    los bloques 'dudoso': tramos con habla según silencios.json y sin segmentos."""
    if not bloques:
        avisos.append("no hay bloques transcriptos: la clase queda sin transcript")
    segmentos: list[tuple[int, int, int, dict[str, Any]]] = []
    for n in bloques:
        ruta = carpeta / "transcript" / f"bloque_{n:02d}.json"
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise ErrorEtapa(f"falta el transcript del bloque {n}: reprocesá desde transcribir",
                             reintentable=False) from e
        except (OSError, ValueError) as e:
            raise ErrorEtapa(f"el transcript del bloque {n} está roto ({e}): reprocesá desde transcribir",
                             reintentable=False) from e
        if not isinstance(datos, dict):
            raise ErrorEtapa(f"el transcript del bloque {n} no tiene la forma esperada", reintentable=False)
        if datos.get("estado") == "dudoso":
            detalle = "; ".join(str(a) for a in datos.get("avisos") or [])
            avisos.append(f"el bloque {n} quedó dudoso" + (f": {detalle}" if detalle else ""))
            if sin_texto is not None:
                sin_texto += _huecos_del_bloque(datos, silencios)
        for i, s in enumerate(datos.get("segmentos") or []):
            try:
                inicio = int(s["inicio_ms"])
                fin = int(s.get("fin_ms") if s.get("fin_ms") is not None else inicio)
            except (KeyError, TypeError, ValueError):
                avisos.append(f"bloque {n}: segmento {i + 1} sin tiempos válidos, lo salteo")
                continue
            texto = " ".join(str(s.get("texto") or "").split())
            if not texto:
                continue
            segmentos.append((inicio, n, i, {
                "bloque": n,
                "inicio_ms": max(0, inicio),
                "fin_ms": max(inicio, fin),
                "hablante": normalizar_hablante(s.get("hablante")),
                "texto": texto,
            }))
    segmentos.sort(key=lambda x: x[:3])
    return [s for *_, s in segmentos]


def _huecos_del_bloque(datos: dict[str, Any], silencios: Any) -> list[tuple[int, int]]:
    """Huecos con habla y sin texto de un bloque (la misma regla que la validación), absolutos."""
    try:
        inicio, fin = int(datos["inicio_ms"]), int(datos["fin_ms"])
    except (KeyError, TypeError, ValueError):
        return []
    relativos = []
    for s in datos.get("segmentos") or []:
        try:
            a = int(s["inicio_ms"])
            b = int(s.get("fin_ms") if s.get("fin_ms") is not None else a)
        except (KeyError, TypeError, ValueError):
            continue
        relativos.append(SegmentoRel(a - inicio, b - inicio, "", ""))
    return [(inicio + a, inicio + b) for a, b in huecos(relativos, habla_del_bloque(silencios, inicio, fin))]


def _descartados(silencios: Any) -> list[tuple[int, int]]:
    """Silencios largos que segmentar no mandó a transcribir (silencios.json "descartados")."""
    if not isinstance(silencios, dict) or not isinstance(silencios.get("descartados"), list):
        return []
    tramos = []
    for d in silencios["descartados"]:
        try:
            a, b = int(d["inicio_ms"]), int(d["fin_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if b > a:
            tramos.append((max(0, a), b))
    return sorted(tramos)


def _linea_habla(s: Mapping[str, Any]) -> str:
    return f"[{ms_a_hhmmss(s['inicio_ms'])}] {s['hablante']}: {s['texto']}"


# --- eventos y fotos ---------------------------------------------------------------------------

def _datos(fila: sqlite3.Row) -> dict[str, Any]:
    try:
        datos = json.loads(fila["datos"]) if fila["datos"] else {}
    except ValueError:
        return {}
    return datos if isinstance(datos, dict) else {}


def _ordenar_assets(assets: list[sqlite3.Row], avisos: list[str]
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Devuelve (fotos en orden de pizarra, eventos con hora en orden de tiempo)."""
    con_hora: list[dict[str, Any]] = []
    fotos_sin_hora: list[dict[str, Any]] = []
    for fila in assets:
        e = {"id": fila["id"], "tipo": fila["tipo"], "offset_ms": fila["offset_ms"],
             "ruta": fila["ruta"], "creado": fila["creado"] or "", "datos": _datos(fila)}
        if e["offset_ms"] is not None:
            e["offset_ms"] = max(0, int(e["offset_ms"]))
            con_hora.append(e)
        elif e["tipo"] == "foto":
            fotos_sin_hora.append(e)
        else:
            avisos.append(f"evento {e['tipo']} {e['id']} sin offset_ms, lo salteo")
    con_hora.sort(key=lambda e: (e["offset_ms"], str(e["datos"].get("creado") or ""),
                                 _ORDEN_TIPO.get(e["tipo"], 0), e["creado"], e["id"]))
    fotos_sin_hora.sort(key=lambda e: (str(e["datos"].get("creado") or ""), e["creado"], e["id"]))
    fotos = [e for e in con_hora if e["tipo"] == "foto"] + fotos_sin_hora
    return fotos, con_hora


def _preparar_fotos(carpeta: Path, fotos: list[dict[str, Any]], avisos: list[str]) -> list[dict[str, Any]]:
    """Genera fotos_pdf/pizarra_NN.jpg (numeradas sin huecos) y borra las que sobran de antes."""
    destino_dir = carpeta / CARPETA_FOTOS
    listas: list[dict[str, Any]] = []
    for f in fotos:
        origen = carpeta / f["ruta"] if f["ruta"] else None
        if origen is None or not origen.is_file():
            avisos.append(f"falta el archivo de la foto {f['id']}, la salteo")
            continue
        nombre = f"pizarra_{len(listas) + 1:02d}.jpg"
        try:
            ancho, alto, accion = procesar_foto(origen, destino_dir / nombre)
        except Exception as e:  # cualquier cosa que Pillow no pueda abrir
            avisos.append(f"no pude abrir la foto {f['ruta']} ({e.__class__.__name__}), la salteo")
            continue
        listas.append({"id": f["id"], "original": f["ruta"], "nombre": nombre, "offset_ms": f["offset_ms"],
                       "tamano": [ancho, alto], "tratamiento": accion})
    vigentes = {f["nombre"] for f in listas}
    if destino_dir.is_dir():
        for vieja in destino_dir.glob("pizarra_*.jpg"):
            if vieja.name not in vigentes:
                vieja.unlink(missing_ok=True)
    return listas


def _evento(e: dict[str, Any], nombre_de: dict[str, str]) -> tuple[str, dict[str, Any]]:
    t = ms_a_hhmmss(e["offset_ms"])
    tipo = e["tipo"]
    evento: dict[str, Any] = {"id": e["id"], "tipo": tipo, "offset_ms": e["offset_ms"]}
    if tipo == "foto":
        evento["foto"] = nombre_de[e["id"]]
        return f"[{t}] FOTO fotos/{evento['foto']}", evento
    if tipo == "nota":
        evento["texto"] = " ".join(str(e["datos"].get("texto") or "").split())
        return f'[{t}] NOTA "{evento["texto"]}"', evento
    if tipo == "marca":
        marca = str(e["datos"].get("marca") or "").strip()
        if marca:
            evento["marca"] = marca
        return f"[{t}] MARCA {marca}".rstrip(), evento
    return f"[{t}] {tipo.upper()}", evento
