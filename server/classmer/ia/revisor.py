"""La revisión del transcript por dentro (§15): conciliar, limpiar, volver a escuchar y resolver dudas.

1. **Alinear** (sin IA): cada línea del principal con el tramo del celu que le corresponde, palabra por
   palabra (difflib, por partes de ~5 min: los dos transcripts ya están en el mismo tiempo). Lo que el
   celu tiene y el reloj no (un segmento casi sin palabras en común) queda como línea "sólo en el celu".
2. **Conciliar** (sólo con dos audios): Claude elige, en las líneas que difieren, lo que más
   probablemente se dijo, y decide si suma las líneas que sólo están en el celu. Puede marcar dudas.
3. **Limpiar** (siempre): Claude limpia todas las líneas (muletillas, puntuación, lo que el ASR
   entendió mal, hablante, notación, corchetes) y anota los cambios que no son de forma. Puede marcar
   dudas (con un solo audio: lo que cambia el sentido y no está claro).
4. **Volver a escuchar**: Gemini transcribe de nuevo esos segundos en los dos audios.
5. **Resolver**: Claude decide cada duda con lo que se volvió a escuchar; lo que no se resuelve queda
   para el usuario.

Los pedidos a Claude van por tramos de la clase (en paralelo) y cada respuesta se guarda en
`jobs/<sesion>/revisar/cache/` con la huella del pedido: si el job se pospone o se reintenta, lo ya
pagado no se vuelve a pedir.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from classmer.ia import gemini
from classmer.ia.claude_json import RespuestaInvalida, RespuestaJSON
from classmer.pipeline import CuotaAgotada, ErrorEtapa
from classmer.util import escribir_json, leer_json, ms_a_hhmmss

log = logging.getLogger("classmer.revisor")

PARTE_ALINEAR_MS = 300_000       # partes de ~5 min para alinear
LINEAS_CONCILIAR = 60            # líneas del reloj por pedido de conciliar
LINEAS_LIMPIAR = 70              # líneas por pedido de limpiar
CONTEXTO_LIMPIAR = 4             # líneas limpias del tramo anterior que van como contexto
PARALELO = 4                     # pedidos a Claude a la vez
LIMITE_PERFIL = 6000
SOLO_CELU_MAX_COINCIDENCIA = 0.25   # un segmento del celu con menos palabras en común es "sólo celu"
SOLO_CELU_MIN_PALABRAS = 2
MARGEN_TRAMO_MS = 2000           # el tramo del celu de una línea llega hasta su fin más esto
MARGEN_ESCUCHA_MS = 3000
ESCUCHA_MAX_MS = 40_000
# Volver a escuchar va agrupado: varios tramos por pedido a Gemini, separados por un silencio.
ESCUCHA_POR_PEDIDO = 12
ESCUCHA_MAX_PEDIDO_MS = 300_000
SILENCIO_ESCUCHA_MS = 2500
CONFIANZAS = ("alta", "media", "baja")
TIPOS_CAMBIO = ("palabra", "termino", "notacion", "corchetes", "inaudible", "hablante", "otro")
ROLES = ("profe", "alumno")
# Lo que se saca sin que cambie el sentido (para decidir si dos versiones "difieren").
RELLENO = {"eh", "ehh", "em", "emm", "mm", "mmm", "este", "esteee", "ah"}


# --- normalización ---------------------------------------------------------------------------------

def norm(palabra: str) -> str:
    """Minúsculas, sin tildes ni puntuación (para comparar palabras)."""
    p = unicodedata.normalize("NFD", palabra.lower())
    p = "".join(c for c in p if unicodedata.category(c) != "Mn")
    return re.sub(r"[^\w]", "", p)


def palabras(texto: str) -> list[str]:
    return [n for n in (norm(w) for w in str(texto or "").split()) if n]


def sin_relleno(tokens: Iterable[str]) -> list[str]:
    """Sin muletillas ni repeticiones inmediatas: lo que importa para el sentido."""
    salida: list[str] = []
    for t in tokens:
        if t in RELLENO or (salida and salida[-1] == t):
            continue
        salida.append(t)
    return salida


def difieren(a: str, b: str) -> bool:
    return sin_relleno(palabras(a)) != sin_relleno(palabras(b))


def similitud(a: str, b: str) -> float:
    ta, tb = palabras(a), palabras(b)
    if not ta and not tb:
        return 1.0
    return round(difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio(), 3)


def rol_de(hablante: str | None) -> str:
    """El rol que adivinó el ASR (para el prompt): PROFE, ALUMNO o DESCONOCIDO."""
    h = (hablante or "").strip().lower()
    if h.startswith(("prof", "docen")):
        return "PROFE"
    if h.startswith(("alum", "estud")):
        return "ALUMNO"
    return "DESCONOCIDO"


# --- alinear ---------------------------------------------------------------------------------------

def alinear(principal: list[dict[str, Any]], celu: list[dict[str, Any]],
            parte_ms: int = PARTE_ALINEAR_MS) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Empareja cada línea del principal con su tramo del celu (los dos en el tiempo del principal).

    Devuelve (lineas, solo_celu). Cada línea: {"i", "celu": texto o None, "similitud"}. Cada "sólo celu":
    {"inicio_ms", "fin_ms", "hablante", "texto"}."""
    lineas: list[dict[str, Any]] = [{"i": i, "celu": None, "similitud": None} for i in range(len(principal))]
    solo: list[dict[str, Any]] = []
    if not principal or not celu:
        return lineas, [dict(s) for s in celu] if not principal else []

    # Partes: cortes en el inicio de una línea del principal cada ~parte_ms.
    cortes = [0]
    for i, s in enumerate(principal):
        if s["inicio_ms"] - principal[cortes[-1]]["inicio_ms"] >= parte_ms:
            cortes.append(i)
    cortes.append(len(principal))
    limites = [principal[c]["inicio_ms"] for c in cortes[:-1]] + [float("inf")]

    def parte_de(s: dict[str, Any]) -> int:
        medio = (s["inicio_ms"] + s["fin_ms"]) / 2
        for k in range(len(limites) - 1):
            if medio < limites[k + 1]:
                return k
        return len(limites) - 2

    celu_por_parte: list[list[dict[str, Any]]] = [[] for _ in range(len(cortes) - 1)]
    for s in celu:
        celu_por_parte[parte_de(s)].append(s)

    for k in range(len(cortes) - 1):
        desde, hasta = cortes[k], cortes[k + 1]
        tok_p: list[tuple[str, int]] = []
        for i in range(desde, hasta):
            tok_p += [(n, i) for n in palabras(principal[i]["texto"])]
        tok_c: list[tuple[str, str, int, float]] = []     # (normalizada, original, segmento, minuto aprox.)
        segs = celu_por_parte[k]
        for j, s in enumerate(segs):
            ws = [w for w in str(s.get("texto") or "").split() if norm(w)]
            dur = max(0, int(s["fin_ms"]) - int(s["inicio_ms"]))
            tok_c += [(norm(w), w, j, s["inicio_ms"] + dur * (x + 0.5) / len(ws)) for x, w in enumerate(ws)]
        sm = difflib.SequenceMatcher(None, [t[0] for t in tok_p], [t[0] for t in tok_c], autojunk=False)
        p_a_c: dict[int, int] = {}
        for a, b, n in sm.get_matching_blocks():
            for x in range(n):
                p_a_c[a + x] = b + x
        emparejadas_seg = [0] * len(segs)
        for c in p_a_c.values():
            emparejadas_seg[tok_c[c][2]] += 1
        total_seg = [0] * len(segs)
        for t in tok_c:
            total_seg[t[2]] += 1
        es_solo = [total_seg[j] >= SOLO_CELU_MIN_PALABRAS
                   and emparejadas_seg[j] / total_seg[j] < SOLO_CELU_MAX_COINCIDENCIA for j in range(len(segs))]
        emparejados = set(p_a_c.values())
        # Un segmento del celu casi sin palabras en común es habla que el reloj no tiene: lo que no se
        # emparejó de él va como línea "sólo en el celu" (lo emparejado queda en su línea del reloj).
        sueltos = [c for c in range(len(tok_c)) if es_solo[tok_c[c][2]] and c not in emparejados]
        for grupo in _seguidos(sueltos):
            if len(grupo) >= SOLO_CELU_MIN_PALABRAS:
                s = segs[tok_c[grupo[0]][2]]
                solo.append({"inicio_ms": int(tok_c[grupo[0]][3]) if len(grupo) < len(tok_c) else s["inicio_ms"],
                             "fin_ms": int(tok_c[grupo[-1]][3]) + 300, "hablante": s.get("hablante"),
                             "texto": " ".join(tok_c[c][1] for c in grupo)})
        # Primer token del celu emparejado de cada línea.
        primero: dict[int, int] = {}
        for a in sorted(p_a_c):
            linea = tok_p[a][1]
            primero.setdefault(linea, p_a_c[a])
        con_par = sorted(primero)
        for n, linea in enumerate(con_par):
            inicio = primero[linea]
            fin = primero[con_par[n + 1]] if n + 1 < len(con_par) else len(tok_c)
            fin = max(fin, inicio)
            # El tramo termina con la línea (más un margen): lo que el celu tiene después y no se
            # emparejó es habla que el reloj no tiene en esta línea (otra línea o una que perdió).
            corte = principal[linea]["fin_ms"] + MARGEN_TRAMO_MS
            propios, sobra = [], []
            for c in range(inicio, fin):
                if c not in emparejados and es_solo[tok_c[c][2]]:
                    continue
                if c not in emparejados and tok_c[c][3] > corte:
                    sobra.append(c)
                else:
                    propios.append(c)
            texto = " ".join(tok_c[c][1] for c in propios)
            lineas[linea]["celu"] = texto
            lineas[linea]["similitud"] = similitud(principal[linea]["texto"], texto)
            for grupo in _seguidos(sobra):
                if len(grupo) >= SOLO_CELU_MIN_PALABRAS:
                    solo.append({"inicio_ms": int(tok_c[grupo[0]][3]), "fin_ms": int(tok_c[grupo[-1]][3]) + 300,
                                 "hablante": segs[tok_c[grupo[0]][2]].get("hablante"),
                                 "texto": " ".join(tok_c[c][1] for c in grupo)})
    solo.sort(key=lambda s: s["inicio_ms"])
    return lineas, solo


def _seguidos(indices: list[int]) -> list[list[int]]:
    grupos: list[list[int]] = []
    for i in indices:
        if grupos and i == grupos[-1][-1] + 1:
            grupos[-1].append(i)
        else:
            grupos.append([i])
    return grupos


# --- tramos ----------------------------------------------------------------------------------------

def tramos(n: int, tamano: int) -> list[tuple[int, int]]:
    """Cortes parejos de range(n) en tramos de ~tamano (el último no queda chiquito)."""
    if n <= 0:
        return []
    cantidad = max(1, round(n / tamano))
    paso = n / cantidad
    bordes = [round(paso * k) for k in range(cantidad)] + [n]
    return [(bordes[k], bordes[k + 1]) for k in range(cantidad) if bordes[k + 1] > bordes[k]]


# --- esquemas --------------------------------------------------------------------------------------

def _texto(descripcion: str) -> dict[str, Any]:
    return {"type": "string", "description": descripcion}


def _enum(opciones: Iterable[str], descripcion: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(opciones), "description": descripcion}


def _objeto(propiedades: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": propiedades, "required": list(propiedades),
            "additionalProperties": False}


ESQUEMA_CONCILIAR = _objeto({
    "lineas": {"type": "array", "items": _objeto({
        "id": _texto("id de la línea (L…)"),
        "texto": _texto("lo que más probablemente se dijo, sin limpiar"),
        "eleccion": _enum(("reloj", "celu", "mezcla"), "de dónde sale"),
        "motivo": _texto("una oración corta"),
        "confianza": _enum(CONFIANZAS, "qué tan seguro"),
        "duda": {"type": "boolean", "description": "true si cambia el sentido y no se puede saber cuál se dijo"},
    })},
    "solo_celu": {"type": "array", "items": _objeto({
        "id": _texto("id de la línea (C…)"),
        "incluir": {"type": "boolean"},
        "hablante": _enum(ROLES, "quién habla"),
        "texto": _texto("lo que se dijo, sin limpiar"),
        "motivo": _texto("una oración corta"),
    })},
})

_CAMBIO = _objeto({
    "antes": _texto("lo que decía"),
    "despues": _texto("lo que queda"),
    "tipo": _enum(TIPOS_CAMBIO, "qué clase de cambio"),
    "motivo": _texto("una oración corta"),
    "confianza": _enum(CONFIANZAS, "qué tan seguro"),
})

ESQUEMA_LIMPIAR = _objeto({
    "lineas": {"type": "array", "items": _objeto({
        "id": _texto("id de la línea"),
        "hablante": _enum(ROLES, "quién habla"),
        "texto": _texto("la línea limpia (vacía si no queda nada)"),
        "cambios": {"type": "array", "items": _CAMBIO},
        "duda": {"anyOf": [{"type": "null"}, _objeto({
            "alternativa": _texto("lo que pudo haberse dicho, limpio"),
            "motivo": _texto("qué está en juego"),
        })]},
        "alternativas": {"type": "array", "items": _objeto({
            "fuente": _texto("la fuente de esa versión (reloj, celu)"),
            "texto": _texto("esa versión, limpia"),
        })},
    })},
})

ESQUEMA_RESOLVER = _objeto({
    "dudas": {"type": "array", "items": _objeto({
        "id": _texto("id de la duda (D…)"),
        "resuelta": {"type": "boolean"},
        "texto": _texto("la línea final, limpia (si no se resolvió, la más probable)"),
        "eleccion": _enum(("reloj", "celu", "audio", "propuesta", "otra"), "de dónde sale"),
        "motivo": _texto("una oración concreta"),
        "confianza": _enum(CONFIANZAS, "qué tan seguro"),
    })},
})

SISTEMA = ("Revisás transcripts de clases universitarias grabadas para que queden fieles y bien escritos. "
           "Respondé sólo con el JSON pedido.")


# --- llamadas con caché ----------------------------------------------------------------------------

@dataclass
class Gasto:
    """Lo que gastó la revisión en Claude (para jobs.costo y el registro)."""
    costo: float = 0.0
    con_costo: bool = False
    uso: dict[str, int] = field(default_factory=dict)
    medios: set[str] = field(default_factory=set)
    pedidos: int = 0
    en_cache: int = 0

    def sumar(self, costo: float | None, uso: dict[str, int] | None, medio: str | None) -> None:
        if costo is not None:
            self.costo += costo
            self.con_costo = True
        for k, v in (uso or {}).items():
            self.uso[k] = self.uso.get(k, 0) + int(v)
        if medio:
            self.medios.add(medio)

    @property
    def facturacion(self) -> str | None:
        if not self.medios:
            return None
        return "max" if self.medios == {"max"} else "api"


class Pedidor:
    """Hace los pedidos a Claude con caché en disco (por huella del pedido) y suma el gasto."""

    def __init__(self, consulta: Any, cache: Path, gasto: Gasto, avisar: Callable[[str], None]):
        self.consulta = consulta
        self.cache = cache
        self.gasto = gasto
        self.avisar = avisar

    def huella(self, paso: str, prompt: str) -> str:
        datos = json.dumps([paso, self.consulta.modelo, self.consulta.esfuerzo, prompt], ensure_ascii=False)
        return hashlib.sha256(datos.encode("utf-8")).hexdigest()[:24]

    def pedir(self, paso: str, prompt: str, esquema: dict[str, Any]) -> dict[str, Any]:
        ruta = self.cache / f"{paso}-{self.huella(paso, prompt)}.json"
        guardado = leer_json(ruta)
        if isinstance(guardado, dict) and isinstance(guardado.get("datos"), dict):
            self.gasto.en_cache += 1
            return guardado["datos"]
        r: RespuestaJSON = self.consulta.consultar(prompt, esquema)
        self.gasto.pedidos += 1
        self.gasto.sumar(r.costo, r.uso, r.facturacion)
        for aviso in r.avisos:
            self.avisar(aviso)
        escribir_json(ruta, {"paso": paso, "modelo": r.modelo, "facturacion": r.facturacion, "costo": r.costo,
                             "uso": r.uso, "datos": r.datos})
        return r.datos


def en_paralelo(funcion: Callable[[Any], Any], items: list[Any], hilos: int = PARALELO) -> list[Any]:
    """Corre `funcion` sobre los items; si alguno lanza, espera a los demás (lo pagado queda en caché) y
    relanza el primero (una CuotaAgotada antes que otro error)."""
    if len(items) <= 1 or hilos <= 1:
        return [funcion(x) for x in items]
    resultados: list[Any] = [None] * len(items)
    errores: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=min(hilos, len(items)), thread_name_prefix="revisar") as ej:
        futuros = {ej.submit(funcion, x): k for k, x in enumerate(items)}
        for f, k in futuros.items():
            try:
                resultados[k] = f.result()
            except BaseException as e:  # noqa: BLE001 - se relanza abajo
                errores.append(e)
    if errores:
        cuota = [e for e in errores if isinstance(e, CuotaAgotada)]
        raise (cuota[0] if cuota else errores[0])
    return resultados


# --- prompts ---------------------------------------------------------------------------------------

def _linea_prompt(linea: dict[str, Any], texto: str | None = None) -> str:
    return f"{linea['id']} [{ms_a_hhmmss(linea['inicio_ms'])}] {rol_de(linea.get('hablante'))}: " \
           f"{' '.join(str(texto if texto is not None else linea['texto']).split())}"


def prompt_conciliar(lineas: list[dict[str, Any]], solo: list[dict[str, Any]], pedidas: list[str], *,
                     materia: str, profesor: str, perfil: str) -> str:
    items: list[tuple[int, int, str]] = []
    for k, ln in enumerate(lineas):
        if ln["id"] in pedidas:
            celu = ln.get("celu")
            texto = (f"{ln['id']} [{ms_a_hhmmss(ln['inicio_ms'])}] {rol_de(ln.get('hablante'))}\n"
                     f"  reloj: {' '.join(str(ln['texto']).split())}\n"
                     f"  celu:  {' '.join(celu.split()) if celu else '(el celu no tiene esta línea)'}")
        else:
            texto = _linea_prompt(ln)
        items.append((ln["inicio_ms"], k, texto))
    for k, s in enumerate(solo):
        items.append((s["inicio_ms"], len(lineas) + k,
                      f"{s['id']} [{ms_a_hhmmss(s['inicio_ms'])}] sólo en el celu, {rol_de(s.get('hablante'))}: "
                      f"{' '.join(str(s['texto']).split())}"))
    items.sort()
    return gemini.cargar_prompt("revisar_conciliar", materia=materia, profesor=profesor, perfil=perfil,
                                lineas="\n".join(t for *_, t in items), pedidas=", ".join(pedidas) or "(ninguna)")


def prompt_limpiar(lineas: list[dict[str, Any]], contexto: list[str], *, materia: str, profesor: str,
                   perfil: str) -> str:
    textos = []
    for ln in lineas:
        textos.append(_linea_prompt(ln))
        duda = ln.get("duda_conciliar")
        if duda:
            versiones = " / ".join(f"{o['fuente']} «{' '.join(o['texto'].split())}»" for o in duda["opciones"])
            textos.append(f"   (duda de la conciliación: {versiones})")
    return gemini.cargar_prompt("revisar_limpiar", materia=materia, profesor=profesor, perfil=perfil,
                                contexto="\n".join(contexto) or "(es el comienzo de la clase)",
                                lineas="\n".join(textos))


def prompt_resolver(dudas: list[dict[str, Any]], *, materia: str, profesor: str) -> str:
    bloques = []
    for d in dudas:
        partes = [f"{d['id']} [{ms_a_hhmmss(d['inicio_ms'])}] (de la {'conciliación' if d['paso'] == 'conciliar' else 'limpieza'}) "
                  f"{d.get('motivo') or ''}".rstrip()]
        if d.get("antes"):
            partes.append(f"  antes:  {d['antes']}")
        partes.append("  versiones:")
        partes += [f"    {o['fuente']}: {o['texto']}" for o in d["opciones"]]
        partes.append(f"  propuesta: {d['opciones'][d['propuesta']]['fuente']}")
        escuchado = d.get("escuchado") or {}
        for fuente, nombre in (("reloj", "el reloj"), ("celu", "el celu")):
            if fuente in escuchado:
                partes.append(f"  escuchado en {nombre}: {escuchado[fuente] or '(no se oyó nada)'}")
        if not escuchado:
            partes.append("  (no se pudo volver a escuchar)")
        if d.get("despues"):
            partes.append(f"  después: {d['despues']}")
        bloques.append("\n".join(partes))
    return gemini.cargar_prompt("revisar_resolver", materia=materia, profesor=profesor, dudas="\n\n".join(bloques))


# --- interpretar respuestas ------------------------------------------------------------------------

def _confianza(valor: Any) -> str:
    return valor if valor in CONFIANZAS else "media"


def interpretar_conciliar(datos: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    lineas = {}
    for x in datos.get("lineas") or []:
        if isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("texto"), str):
            lineas[x["id"]] = {"texto": " ".join(x["texto"].split()),
                               "eleccion": x.get("eleccion") if x.get("eleccion") in ("reloj", "celu", "mezcla") else "mezcla",
                               "motivo": str(x.get("motivo") or ""), "confianza": _confianza(x.get("confianza")),
                               "duda": bool(x.get("duda"))}
    solo = {}
    for x in datos.get("solo_celu") or []:
        if isinstance(x, dict) and isinstance(x.get("id"), str):
            solo[x["id"]] = {"incluir": bool(x.get("incluir")), "hablante": x.get("hablante") if x.get("hablante") in ROLES else "alumno",
                             "texto": " ".join(str(x.get("texto") or "").split()), "motivo": str(x.get("motivo") or "")}
    return lineas, solo


def interpretar_limpiar(datos: dict[str, Any]) -> dict[str, dict[str, Any]]:
    salida = {}
    for x in datos.get("lineas") or []:
        if not (isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("texto"), str)):
            continue
        cambios = []
        for c in x.get("cambios") or []:
            if isinstance(c, dict):
                cambios.append({"antes": str(c.get("antes") or ""), "despues": str(c.get("despues") or ""),
                                "tipo": c.get("tipo") if c.get("tipo") in TIPOS_CAMBIO else "otro",
                                "motivo": str(c.get("motivo") or ""), "confianza": _confianza(c.get("confianza"))})
        duda = x.get("duda") if isinstance(x.get("duda"), dict) else None
        alternativas = [{"fuente": str(a.get("fuente") or "?"), "texto": " ".join(str(a.get("texto") or "").split())}
                        for a in x.get("alternativas") or [] if isinstance(a, dict)]
        salida[x["id"]] = {
            "hablante": x.get("hablante") if x.get("hablante") in ROLES else None,
            "texto": " ".join(x["texto"].split()),
            "cambios": cambios,
            "duda": {"alternativa": " ".join(str(duda.get("alternativa") or "").split()),
                     "motivo": str(duda.get("motivo") or "")} if duda and str(duda.get("alternativa") or "").strip() else None,
            "alternativas": [a for a in alternativas if a["texto"]],
        }
    return salida


def interpretar_resolver(datos: dict[str, Any]) -> dict[str, dict[str, Any]]:
    salida = {}
    for x in datos.get("dudas") or []:
        if isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("texto"), str):
            salida[x["id"]] = {"resuelta": bool(x.get("resuelta")), "texto": " ".join(x["texto"].split()),
                               "eleccion": str(x.get("eleccion") or "otra"), "motivo": str(x.get("motivo") or ""),
                               "confianza": _confianza(x.get("confianza"))}
    return salida


def pedir_tramo(pedidor: Pedidor, paso: str, prompt_de: Callable[[list[Any]], str], items: list[Any],
                esquema: dict[str, Any], *, minimo: int = 12) -> list[tuple[list[Any], dict[str, Any] | None]]:
    """Un pedido por tramo; si la respuesta se cortó por el tope de tokens, lo parte en dos. Si no se
    puede (o el modelo no quiso), devuelve el tramo sin datos (None): sigue sin revisar."""
    try:
        return [(items, pedidor.pedir(paso, prompt_de(items), esquema))]
    except RespuestaInvalida as e:
        pedidor.gasto.sumar(e.costo, e.uso, None)
        if e.cortada and len(items) >= 2 * minimo:
            mitad = len(items) // 2
            return (pedir_tramo(pedidor, paso, prompt_de, items[:mitad], esquema, minimo=minimo)
                    + pedir_tramo(pedidor, paso, prompt_de, items[mitad:], esquema, minimo=minimo))
        pedidor.avisar(f"{paso}: un tramo de {len(items)} línea(s) queda sin revisar ({e})")
        return [(items, None)]


def perfil_para(texto: str | None) -> str:
    return gemini.extracto_perfil(texto, LIMITE_PERFIL) or "(sin perfil: usá el nombre de la materia)"


def recortes_escucha(inicio_ms: int, fin_ms: int) -> tuple[int, int]:
    """El tramo que se vuelve a escuchar (con margen, hasta ESCUCHA_MAX_MS)."""
    a = max(0, inicio_ms - MARGEN_ESCUCHA_MS)
    b = fin_ms + MARGEN_ESCUCHA_MS
    if b - a > ESCUCHA_MAX_MS:
        b = a + ESCUCHA_MAX_MS
    return a, b


def lotes_escucha(tramos: list[Any], por_pedido: int = ESCUCHA_POR_PEDIDO,
                  max_ms: int = ESCUCHA_MAX_PEDIDO_MS) -> list[list[Any]]:
    """Los tramos [(duda, inicio_ms, fin_ms)] en grupos de hasta `por_pedido` y `max_ms` de audio."""
    lotes: list[list[Any]] = []
    largo = 0
    for t in tramos:
        dur = t[2] - t[1]
        if not lotes or len(lotes[-1]) >= por_pedido or largo + dur > max_ms:
            lotes.append([])
            largo = 0
        lotes[-1].append(t)
        largo += dur + SILENCIO_ESCUCHA_MS
    return lotes


def repartir_escucha(segmentos: list[Any], inicios: list[int], duraciones: list[int]) -> list[str]:
    """El texto de cada tramo de un pedido agrupado: cada frase va al tramo donde cae su mitad (los
    silencios del medio se reparten entre los dos vecinos)."""
    textos: list[list[str]] = [[] for _ in inicios]
    for s in segmentos:
        texto = " ".join(str(getattr(s, "texto", "") or "").split())
        if not texto:
            continue
        medio = (int(s.inicio_ms) + int(s.fin_ms)) / 2
        k = 0
        for i in range(len(inicios)):
            fin_i = inicios[i] + duraciones[i]
            limite = (fin_i + inicios[i + 1]) / 2 if i + 1 < len(inicios) else float("inf")
            if medio < limite:
                k = i
                break
        textos[k].append(texto)
    return [" ".join(x) for x in textos]


def error_corta(e: BaseException) -> str:
    texto = str(e) or e.__class__.__name__
    return texto[:200]


def es_pasajero(e: BaseException) -> bool:
    return isinstance(e, CuotaAgotada) or (isinstance(e, ErrorEtapa) and e.reintentable)
