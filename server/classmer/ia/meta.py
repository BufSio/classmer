"""`meta.json` del apunte (§8): esquema JSON, validación con errores legibles y reparación.

Lo escribe el agente en output/meta.json y es lo que se indexa (secciones e items). Si viene
roto, `reparar_meta` arma uno válido con lo que se pueda salvar y lo que se lee del .tex: el
índice nunca tiene que romper la publicación.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from classmer.util import ms_a_hhmmss, tiempo_a_ms

TIPOS_ITEM = ("parcial", "tarea", "fecha", "duda", "aviso")
TOLERANCIA_MS = 60_000  # los tiempos pueden pasarse un poco de la duración (redondeos del agente)
MAX_ERRORES = 20
RESUMEN_DEFAULT = "Apunte de la clase (el índice se armó a partir del .tex)."
TITULO_DEFAULT = "Clase sin título"

_TEXTO = {"type": "string", "minLength": 1, "pattern": r"\S"}
_MS = {"type": "integer", "minimum": 0}

ESQUEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://classmer.local/schemas/meta.json",
    "title": "meta.json de un apunte de Classmer",
    "type": "object",
    "required": ["titulo", "resumen", "secciones", "items"],
    "properties": {
        "titulo": _TEXTO,
        "resumen": _TEXTO,
        "secciones": {"type": "array", "items": {"$ref": "#/$defs/seccion"}},
        "items": {"type": "array", "items": {"$ref": "#/$defs/item"}},
    },
    # En la raíz el modelo puede agregar lo que quiera (p. ej. "bibliografia"); adentro, no.
    "additionalProperties": True,
    "$defs": {
        "seccion": {
            "type": "object",
            "required": ["titulo", "inicio_ms", "fin_ms", "temas"],
            "properties": {
                "titulo": _TEXTO,
                "inicio_ms": _MS,
                "fin_ms": _MS,
                "temas": {"type": "array", "items": {"type": "string"}},
                "texto_plano": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "item": {
            "type": "object",
            "required": ["tipo", "texto", "offset_ms", "fecha_limite"],
            "properties": {
                "tipo": {"enum": list(TIPOS_ITEM)},
                "texto": _TEXTO,
                "offset_ms": {"type": ["integer", "null"], "minimum": 0},
                "fecha_limite": {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            },
            "additionalProperties": False,
        },
    },
}

_VALIDADOR = Draft202012Validator(ESQUEMA)
_CAMPOS = {
    "seccion": list(ESQUEMA["$defs"]["seccion"]["properties"]),
    "item": list(ESQUEMA["$defs"]["item"]["properties"]),
}
_TIPOS_JSON = {"string": "un texto", "integer": "un entero", "array": "una lista", "object": "un objeto",
               "null": "null", "number": "un número", "boolean": "true/false"}


# --- validación --------------------------------------------------------------------------------

def leer_meta(ruta: Path) -> tuple[Any, str | None]:
    """(datos, None) o (None, error legible) si no existe o no es JSON."""
    try:
        texto = Path(ruta).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None, f"no existe {Path(ruta).name}"
    except (OSError, UnicodeDecodeError) as e:
        return None, f"no pude leer {Path(ruta).name}: {e}"
    if not texto.strip():
        return None, f"{Path(ruta).name} está vacío"
    try:
        return json.loads(texto), None
    except json.JSONDecodeError as e:
        return None, f"{Path(ruta).name} no es JSON válido (línea {e.lineno}, columna {e.colno}: {e.msg})"


def validar_meta(ruta: Path, duracion_ms: int | None = None) -> list[str]:
    """Errores legibles (en español) de output/meta.json; lista vacía si está bien."""
    datos, error = leer_meta(ruta)
    if error:
        return [error]
    return validar_datos(datos, duracion_ms)


def validar_datos(datos: Any, duracion_ms: int | None = None) -> list[str]:
    """Esquema + reglas que el esquema no expresa (fin ≥ inicio, tiempos dentro de la clase, fechas reales)."""
    if not isinstance(datos, dict):
        return [f"meta.json tiene que ser un objeto JSON {{...}} (vino {_tipo_de(datos)})"]
    errores = list(dict.fromkeys(  # sin repetidos (minLength y pattern dicen lo mismo)
        _traducir(e) for e in sorted(_VALIDADOR.iter_errors(datos), key=lambda e: [str(p) for p in e.absolute_path])))
    tope = None if duracion_ms is None else int(duracion_ms) + TOLERANCIA_MS
    for i, s in enumerate(datos.get("secciones") or []):
        if not isinstance(s, dict):
            continue
        ini, fin = s.get("inicio_ms"), s.get("fin_ms")
        if _es_entero(ini) and _es_entero(fin) and fin < ini:
            errores.append(f"secciones[{i}]: fin_ms ({fin}) es menor que inicio_ms ({ini})")
        for campo, valor in (("inicio_ms", ini), ("fin_ms", fin)):
            if tope is not None and _es_entero(valor) and valor > tope:
                errores.append(f"secciones[{i}].{campo}: {valor} ms ({ms_a_hhmmss(valor)}) se pasa de la "
                               f"duración de la clase ({ms_a_hhmmss(duracion_ms)})")
    for i, it in enumerate(datos.get("items") or []):
        if not isinstance(it, dict):
            continue
        off = it.get("offset_ms")
        if tope is not None and _es_entero(off) and off > tope:
            errores.append(f"items[{i}].offset_ms: {off} ms ({ms_a_hhmmss(off)}) se pasa de la duración de "
                           f"la clase ({ms_a_hhmmss(duracion_ms)})")
        fecha = it.get("fecha_limite")
        if isinstance(fecha, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha) and _fecha(fecha) is None:
            errores.append(f"items[{i}].fecha_limite: {fecha!r} no es una fecha real")
    if len(errores) > MAX_ERRORES:
        errores = errores[:MAX_ERRORES] + [f"… y {len(errores) - MAX_ERRORES} errores más"]
    return errores


def _traducir(e: Any) -> str:
    """Un error de jsonschema → 'secciones[2].fin_ms: tiene que ser un entero ≥ 0 (vino "12:30")'."""
    lugar = _ruta(e.absolute_path) or "meta.json"
    v = e.validator
    if v == "required":
        faltan = [c for c in e.validator_value if isinstance(e.instance, dict) and c not in e.instance]
        return f"{lugar}: falta el campo {', '.join(repr(c) for c in faltan) or e.message}"
    if v == "additionalProperties":
        conocidos = _CAMPOS["seccion" if "secciones" in list(e.absolute_path) else "item"]
        extra = sorted(set(e.instance) - set(conocidos)) if isinstance(e.instance, dict) else []
        return (f"{lugar}: campo desconocido {', '.join(repr(c) for c in extra)} "
                f"(los válidos son: {', '.join(conocidos)})")
    if v == "type":
        tipos = e.validator_value if isinstance(e.validator_value, list) else [e.validator_value]
        esperado = " o ".join(_TIPOS_JSON.get(t, t) for t in tipos)
        return f"{lugar}: tiene que ser {esperado} (vino {_muestra(e.instance)})"
    if v == "minimum":
        return f"{lugar}: tiene que ser ≥ {e.validator_value} (vino {_muestra(e.instance)})"
    if v == "enum":
        return f"{lugar}: tiene que ser uno de {', '.join(e.validator_value)} (vino {_muestra(e.instance)})"
    if v in ("minLength", "pattern") and str(e.validator_value) in ("1", r"\S"):
        return f"{lugar}: no puede estar vacío"
    if v == "pattern":
        return f"{lugar}: tiene que ser una fecha AAAA-MM-DD o null (vino {_muestra(e.instance)})"
    return f"{lugar}: {e.message}"


def _ruta(partes: Any) -> str:
    texto = ""
    for p in partes:
        texto += f"[{p}]" if isinstance(p, int) else (f".{p}" if texto else str(p))
    return texto


def _muestra(valor: Any) -> str:
    texto = json.dumps(valor, ensure_ascii=False)
    return texto if len(texto) <= 60 else texto[:57] + "…"


def _tipo_de(valor: Any) -> str:
    if isinstance(valor, list):
        return "una lista"
    if valor is None:
        return "null"
    return f"un {type(valor).__name__}"


def _es_entero(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _fecha(texto: str) -> date | None:
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


# --- reparación --------------------------------------------------------------------------------

def reparar_meta(datos: Any, tex: str, duracion_ms: int | None = None) -> dict[str, Any]:
    """meta.json válido con lo que se pueda salvar de `datos` y, lo que falte, sacado del .tex.

    - titulo/resumen: los del agente si son válidos; si no, \\titulo{…} del .tex.
    - secciones/items: los válidos del agente (sin campos desconocidos, con tiempos acotados);
      si no queda ninguna sección, salen de los \\section del .tex (con su \\ts), y los items
      de los entornos parcial y duda.
    """
    base = meta_desde_tex(tex, duracion_ms)
    if not isinstance(datos, dict):
        return base
    tope = None if duracion_ms is None else int(duracion_ms) + TOLERANCIA_MS
    meta = {k: v for k, v in datos.items() if k not in ("titulo", "resumen", "secciones", "items")}
    meta["titulo"] = _texto_valido(datos.get("titulo")) or base["titulo"]
    meta["resumen"] = _texto_valido(datos.get("resumen")) or base["resumen"]

    secciones = []
    for s in datos.get("secciones") if isinstance(datos.get("secciones"), list) else []:
        if not isinstance(s, dict) or not _texto_valido(s.get("titulo")):
            continue
        ini, fin = _ms(s.get("inicio_ms"), tope), _ms(s.get("fin_ms"), tope)
        if ini is None:
            continue
        nueva = {"titulo": s["titulo"].strip(), "inicio_ms": ini, "fin_ms": max(ini, fin if fin is not None else ini),
                 "temas": [str(t) for t in s.get("temas") or [] if isinstance(t, (str, int, float)) and str(t).strip()]
                 if isinstance(s.get("temas"), list) else []}
        if isinstance(s.get("texto_plano"), str):
            nueva["texto_plano"] = s["texto_plano"]
        secciones.append(nueva)
    meta["secciones"] = secciones or base["secciones"]

    items = []
    lista_items = datos.get("items") if isinstance(datos.get("items"), list) else None
    for it in lista_items or []:
        if not isinstance(it, dict) or it.get("tipo") not in TIPOS_ITEM or not _texto_valido(it.get("texto")):
            continue
        fecha = it.get("fecha_limite")
        fecha = fecha if isinstance(fecha, str) and _fecha(fecha) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha) else None
        items.append({"tipo": it["tipo"], "texto": it["texto"].strip(),
                      "offset_ms": _ms(it.get("offset_ms"), tope), "fecha_limite": fecha})
    meta["items"] = items if lista_items is not None else base["items"]
    return meta


def _texto_valido(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _ms(v: Any, tope: int | None) -> int | None:
    """Entero ≥ 0 (acepta 12.0 o "00:12:30"), recortado a la duración; None si no se puede."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        n = int(v)
    elif isinstance(v, str):
        try:
            n = int(v) if v.strip().isdigit() else tiempo_a_ms(v)
        except ValueError:
            return None
    else:
        return None
    if n < 0:
        return None
    return min(n, tope) if tope is not None else n


# --- meta mínimo desde el .tex -------------------------------------------------------------------

_TS_RE = re.compile(r"\\ts\{\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\}")
_SECCION_RE = re.compile(r"\\section\*?\s*(?:\[[^\]]*\])?\s*\{")
_ENTORNO_RE = re.compile(r"\\begin\{(parcial|duda)\}")


def meta_desde_tex(tex: str, duracion_ms: int | None = None) -> dict[str, Any]:
    """meta.json mínimo y válido leyendo \\titulo, \\section (con su \\ts) y los entornos parcial/duda."""
    cuerpo = _sin_comentarios(tex)
    titulo = latex_a_texto(_argumento(cuerpo, r"\titulo") or _argumento(cuerpo, r"\title") or "") or TITULO_DEFAULT
    fin_clase = int(duracion_ms) if duracion_ms is not None else None

    marcas = []  # (posición en el .tex, título)
    for m in _SECCION_RE.finditer(cuerpo):
        arg = _llaves(cuerpo, m.end() - 1)
        if arg is not None:
            marcas.append((m.start(), arg))
    secciones: list[dict[str, Any]] = []
    for k, (pos, arg) in enumerate(marcas):
        hasta = marcas[k + 1][0] if k + 1 < len(marcas) else len(cuerpo)
        ts = _TS_RE.search(cuerpo, pos, hasta)
        inicio = tiempo_a_ms(ts.group(1)) if ts else None
        nombre = latex_a_texto(arg) or f"Sección {k + 1}"
        secciones.append({"titulo": nombre, "inicio_ms": inicio, "fin_ms": None, "temas": []})
    # Sin \ts: hereda el inicio de la anterior. El fin es el inicio de la siguiente (o la duración).
    tope = fin_clase + TOLERANCIA_MS if fin_clase is not None else None
    previo = 0
    for s in secciones:
        inicio = s["inicio_ms"] if s["inicio_ms"] is not None else previo
        s["inicio_ms"] = previo = min(inicio, tope) if tope is not None else inicio
    for k, s in enumerate(secciones):
        siguiente = secciones[k + 1]["inicio_ms"] if k + 1 < len(secciones) else fin_clase
        s["fin_ms"] = max(s["inicio_ms"], siguiente if siguiente is not None else s["inicio_ms"])
    if not secciones:
        secciones = [{"titulo": titulo, "inicio_ms": 0, "fin_ms": fin_clase or 0, "temas": []}]

    items: list[dict[str, Any]] = []
    for m in _ENTORNO_RE.finditer(cuerpo):
        tipo = m.group(1)
        fin_entorno = cuerpo.find(f"\\end{{{tipo}}}", m.end())
        contenido = cuerpo[m.end(): fin_entorno if fin_entorno >= 0 else len(cuerpo)]
        opcional = re.match(r"\s*\[([^\]]*)\]", contenido)
        ts = _TS_RE.search(contenido)
        texto = latex_a_texto(contenido[opcional.end():] if opcional else contenido)
        if opcional and latex_a_texto(opcional.group(1)):
            texto = f"{latex_a_texto(opcional.group(1))}: {texto}"
        texto = " ".join(texto.split())[:300].strip()
        if texto:
            off = tiempo_a_ms(ts.group(1)) if ts else None
            if off is not None and fin_clase is not None:
                off = min(off, fin_clase + TOLERANCIA_MS)
            items.append({"tipo": tipo, "texto": texto, "offset_ms": off, "fecha_limite": None})

    nombres = [s["titulo"] for s in secciones if s["titulo"] != titulo]
    resumen = (f"{RESUMEN_DEFAULT[:-1]}: {'; '.join(nombres)}." if nombres else RESUMEN_DEFAULT)
    return {"titulo": titulo, "resumen": resumen, "secciones": secciones, "items": items}


def _sin_comentarios(tex: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", tex)


def _argumento(tex: str, comando: str) -> str | None:
    """Argumento entre llaves de la primera aparición de `\\comando{…}` (con llaves anidadas)."""
    m = re.search(re.escape(comando) + r"\s*\{", tex)
    return _llaves(tex, m.end() - 1) if m else None


def _llaves(tex: str, abre: int) -> str | None:
    """Contenido de las llaves que abren en `abre` (respeta anidamiento y \\{ \\})."""
    nivel, i = 0, abre
    while i < len(tex):
        c = tex[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            nivel += 1
        elif c == "}":
            nivel -= 1
            if nivel == 0:
                return tex[abre + 1:i]
        i += 1
    return None


def latex_a_texto(tex: str) -> str:
    """LaTeX → texto plano aproximado (para títulos e items del índice)."""
    t = _TS_RE.sub("", tex)
    t = re.sub(r"\\nota\{([^{}]*)\}", r"[\1]", t)
    t = re.sub(r"\\(?:label|ref|cite|pizarra|includegraphics)\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})+", "", t)
    t = re.sub(r"\\(?:begin|end)\{[^{}]*\}(?:\[[^\]]*\])?", " ", t)
    t = t.replace("\\\\", " ").replace("~", " ")
    t = re.sub(r"\\([%&#_$])", r"\1", t)
    t = re.sub(r"\\[A-Za-z@]+\*?", "", t)
    t = re.sub(r"[{}$]", "", t)
    return re.sub(r"\s+([.,;:])", r"\1", " ".join(t.split()))
