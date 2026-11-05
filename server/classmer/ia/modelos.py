"""Catálogo de modelos elegibles (§7) y "Redescubrir modelos".

El panel y la CLI eligen el modelo de cada tarea de una lista: nunca se escribe a mano. La lista sale
de la tabla `modelos`, que arranca con los modelos conocidos (semillas, al abrir la base) y se
actualiza con las APIs de modelos de Anthropic (ANTHROPIC_API_KEY) y de Google (GEMINI_API_KEY).

Qué modelo le sirve a qué tarea (decidido mirando lo que informa cada API):

- Anthropic (`GET /v1/models`, con `capabilities`): `clasificar` y `revisar_transcript` si soporta
  salida estructurada; `apunte` si acepta imágenes y soporta `effort`. Sin capacidades informadas, las dos.
- Google (`models.list`, con `supported_actions`): `transcribir` si el id dice "transcribe" y tiene
  `generateContent` (el "-transcribe-live" sólo tiene bidiGenerateContent: queda afuera);
  `clasificar` si es un "gemini-…" de texto con `generateContent` (sin TTS, imagen, embeddings,
  live, audio nativo, robótica, etc.).

Un modelo que deja de aparecer no se borra: queda con `presente = 0` y, si estaba elegido, sigue
elegido con el aviso "no apareció en el último descubrimiento". Sin la clave de un proveedor (o si
su API falla) no se toca nada de ese proveedor y queda anotado en `descubrimientos`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from classmer.config import ESFUERZOS
from classmer.db import transaccion
from classmer.util import ahora_iso

if TYPE_CHECKING:
    from classmer.config import Config
    from classmer.db import Base

log = logging.getLogger("classmer.modelos")

TAREAS = ("transcribir", "clasificar", "revisar_transcript", "apunte")
ANTHROPIC, GOOGLE = "anthropic", "google"
PROVEEDORES = (ANTHROPIC, GOOGLE)
CLAVE_PROVEEDOR = {ANTHROPIC: "ANTHROPIC_API_KEY", GOOGLE: "GEMINI_API_KEY"}
NOMBRE_PROVEEDOR = {ANTHROPIC: "Anthropic", GOOGLE: "Google"}

URL_ANTHROPIC = "https://api.anthropic.com"
TIMEOUT_S = 60
POR_PAGINA = 100
MAX_PAGINAS = 50   # por las dudas: una API que nunca dice "no hay más" no cuelga el descubrimiento

AVISO_AUSENTE = "no apareció en el último descubrimiento"
AVISO_FUERA = "no está en el catálogo"

# Modelos conocidos con los que arranca el catálogo (INSERT OR IGNORE: lo que ya hay no se toca).
SEMILLAS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (ANTHROPIC, "claude-opus-5-5", "Claude Opus 5.5", ("clasificar", "revisar_transcript", "apunte")),
    (ANTHROPIC, "claude-sonnet-5-5", "Claude Sonnet 5.5", ("clasificar", "revisar_transcript", "apunte")),
    (ANTHROPIC, "claude-haiku-5-5", "Claude Haiku 5.5", ("clasificar", "revisar_transcript", "apunte")),
    (ANTHROPIC, "claude-fable-5-1", "Claude Fable 5.1", ("clasificar", "revisar_transcript", "apunte")),
    (GOOGLE, "gemini-3.5-transcribe", "Gemini 3.5 Transcribe", ("transcribir",)),
    (GOOGLE, "gemini-flash-latest", "Gemini Flash Latest", ("clasificar",)),
)

# Gemini que no son de texto (o no sirven para un pedido de un turno): fuera de `clasificar`.
GEMINI_NO_TEXTO_RE = re.compile(
    r"tts|image|embedding|live|native-audio|transcribe|robotics|computer-use|banana|omni|"
    r"customtools|deep-research|translate"
)


def _ordenar_tareas(tareas: Iterable[str]) -> list[str]:
    conjunto = set(tareas)
    return [t for t in TAREAS if t in conjunto]


# --- semillas y lectura --------------------------------------------------------------------------

def sembrar(conn: sqlite3.Connection, zona: str) -> None:
    """Agrega los modelos conocidos que falten (se llama al abrir la base)."""
    momento = ahora_iso(zona)
    with transaccion(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO modelos (proveedor, id, nombre, tareas, detalles, descubierto, visto, "
            "presente, origen) VALUES (?, ?, ?, ?, '{}', ?, NULL, 1, 'semilla')",
            [(p, i, n, json.dumps(_ordenar_tareas(t)), momento) for p, i, n, t in SEMILLAS],
        )
        # La revisión del transcript (§15) llegó después: los Claude con salida estructurada que ya
        # estaban en el catálogo (sirven para clasificar) también sirven para revisar.
        for f in conn.execute("SELECT id, tareas FROM modelos WHERE proveedor = ?", (ANTHROPIC,)).fetchall():
            tareas = _json(f["tareas"], [])
            if "clasificar" in tareas and "revisar_transcript" not in tareas:
                conn.execute("UPDATE modelos SET tareas = ? WHERE proveedor = ? AND id = ?",
                             (json.dumps(_ordenar_tareas([*tareas, "revisar_transcript"])), ANTHROPIC, f["id"]))


def _json(texto: str | None, vacio: Any) -> Any:
    try:
        valor = json.loads(texto) if texto else vacio
    except ValueError:
        return vacio
    return valor if isinstance(valor, type(vacio)) else vacio


def _como_dict(f: sqlite3.Row) -> dict[str, Any]:
    return {
        "proveedor": f["proveedor"],
        "id": f["id"],
        "nombre": f["nombre"] or f["id"],
        "tareas": _json(f["tareas"], []),
        "detalles": _json(f["detalles"], {}),
        "descubierto": f["descubierto"],
        "visto": f["visto"],
        "presente": bool(f["presente"]),
        "origen": f["origen"],
    }


def catalogo(conn: sqlite3.Connection, tarea: str | None = None,
             proveedor: str | None = None) -> list[dict[str, Any]]:
    """Los modelos del catálogo (de una tarea y/o un proveedor): presentes primero, por proveedor y nombre."""
    sql, params = "SELECT * FROM modelos", []
    if proveedor:
        sql += " WHERE proveedor = ?"
        params.append(proveedor)
    sql += " ORDER BY presente DESC, proveedor, lower(coalesce(nombre, id)), id"
    modelos = [_como_dict(f) for f in conn.execute(sql, params)]
    if tarea:
        modelos = [m for m in modelos if tarea in m["tareas"]]
    return modelos


def opciones_tarea(conn: sqlite3.Connection, tarea: str, elegido: str | Iterable[str] | None = None, *,
                   proveedor: str | None = None, incluir_ausentes: bool = True) -> list[dict[str, Any]]:
    """Lo que muestra el select de una tarea: los modelos del catálogo para esa tarea, cada uno con
    `elegido`, `en_catalogo` y `aviso`. Si un elegido no está entre ellos, va primero una entrada
    extra (`en_catalogo: False`) para que siga elegido: un modelo nunca se cambia solo."""
    elegidos = [elegido] if isinstance(elegido, str) else list(elegido or [])
    elegidos = [e for e in dict.fromkeys(e.strip() for e in elegidos if e) if e]
    opciones = []
    for m in catalogo(conn, tarea, proveedor):
        if not m["presente"] and not incluir_ausentes and m["id"] not in elegidos:
            continue
        opciones.append({**m, "elegido": m["id"] in elegidos, "en_catalogo": True,
                         "aviso": None if m["presente"] else AVISO_AUSENTE})
    ids = {o["id"] for o in opciones}
    extras = []
    for e in elegidos:
        if e in ids:
            continue
        fila = conn.execute("SELECT * FROM modelos WHERE id = ? ORDER BY presente DESC LIMIT 1", (e,)).fetchone()
        if fila is not None:
            extra = _como_dict(fila)
            if tarea in extra["tareas"]:   # quedó afuera por el filtro de proveedor
                extra["aviso"] = f"es un modelo de {NOMBRE_PROVEEDOR.get(extra['proveedor'], extra['proveedor'])}"
            else:
                extra["aviso"] = f"{AVISO_FUERA} para {tarea}"
        else:
            extra = {"proveedor": proveedor_de(e), "id": e, "nombre": e, "tareas": [], "detalles": {},
                     "descubierto": None, "visto": None, "presente": False, "origen": None,
                     "aviso": AVISO_FUERA}
        extras.append({**extra, "elegido": True, "en_catalogo": False})
    return extras + opciones


def proveedor_de(modelo: str) -> str | None:
    """Proveedor por el nombre (para un modelo que no está en el catálogo)."""
    if modelo.startswith("claude-"):
        return ANTHROPIC
    if modelo.startswith(("gemini-", "models/gemini-")):
        return GOOGLE
    return None


def ultimo_descubrimiento(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """{proveedor: {momento, ok, detalle, cantidad, ultimo_ok}} del último intento de cada proveedor.
    `ultimo_ok`: el último descubrimiento que anduvo (el `visto` más nuevo de sus modelos)."""
    resultado: dict[str, dict[str, Any]] = {}
    for f in conn.execute("SELECT * FROM descubrimientos ORDER BY proveedor"):
        ultimo_ok = conn.execute("SELECT MAX(visto) FROM modelos WHERE proveedor = ?",
                                 (f["proveedor"],)).fetchone()[0]
        resultado[f["proveedor"]] = {"momento": f["momento"], "ok": bool(f["ok"]), "detalle": f["detalle"],
                                     "cantidad": f["cantidad"], "ultimo_ok": ultimo_ok}
    return resultado


def esfuerzo_para(base: Base, modelo: str | None, esfuerzo: str | None) -> tuple[str | None, str | None]:
    """(esfuerzo a pedir, aviso o None) para ese modelo según lo que informó la API de modelos: el
    pedido si lo soporta o no se sabe; si no, el nivel soportado más alto por debajo (o ninguno si el
    modelo no acepta esfuerzo: mandarlo daría un 400)."""
    if not esfuerzo or not modelo:
        return esfuerzo, None
    try:
        with base.conexion() as conn:
            fila = conn.execute("SELECT detalles FROM modelos WHERE proveedor = ? AND id = ?",
                                (ANTHROPIC, modelo)).fetchone()
    except sqlite3.Error:
        return esfuerzo, None
    soportados = _json(fila["detalles"], {}).get("esfuerzos") if fila is not None else None
    if not isinstance(soportados, list) or esfuerzo in soportados:
        return esfuerzo, None
    if esfuerzo not in ESFUERZOS:
        return None, f"esfuerzo {esfuerzo!r} desconocido: no lo pido"
    menores = [n for n in ESFUERZOS[:ESFUERZOS.index(esfuerzo)] if n in soportados]
    if menores:
        return menores[-1], f"{modelo} no acepta el esfuerzo {esfuerzo}: uso {menores[-1]}"
    return None, f"{modelo} no acepta un nivel de esfuerzo: lo pido sin esfuerzo"


def elegidos_config(cfg: Config) -> dict[str, dict[str, list[str]]]:
    """{tarea: {modelo: [dónde está elegido]}} según config.yaml (global y por materia)."""
    def con_esfuerzo(texto: str, esfuerzo: str | None) -> str:
        return f"{texto} (esfuerzo {esfuerzo})" if esfuerzo else texto

    elegidos: dict[str, dict[str, list[str]]] = {t: {} for t in TAREAS}
    if cfg.transcribir.modelo:
        elegidos["transcribir"].setdefault(cfg.transcribir.modelo, []).append("transcribir.modelo")
    if cfg.clasificar.modelo:
        etiqueta = f"clasificar.modelo, motor {cfg.clasificar.motor}"
        esfuerzo = cfg.clasificar.esfuerzo if cfg.clasificar.motor == "claude" else None
        elegidos["clasificar"].setdefault(cfg.clasificar.modelo, []).append(con_esfuerzo(etiqueta, esfuerzo))
    if cfg.revisar_transcript.modelo:
        elegidos["revisar_transcript"].setdefault(cfg.revisar_transcript.modelo, []).append(
            con_esfuerzo("revisar_transcript.modelo", cfg.revisar_transcript.esfuerzo))
    if cfg.apunte.modelo:
        elegidos["apunte"].setdefault(cfg.apunte.modelo, []).append(
            con_esfuerzo("apunte.modelo (global)", cfg.apunte.esfuerzo))
    for m in cfg.materias:
        if m.apunte is not None:
            modelo, esfuerzo, _origen = cfg.modelo_apunte(m)
            if modelo:
                elegidos["apunte"].setdefault(modelo, []).append(con_esfuerzo(f"materia {m.id}", esfuerzo))
    return elegidos


# --- filtros por tarea ---------------------------------------------------------------------------

@dataclass
class ModeloDescubierto:
    id: str
    nombre: str
    tareas: list[str]
    detalles: dict[str, Any]


def _campo(obj: Any, nombre: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(nombre)
    return getattr(obj, nombre, None)


def _soporta(capacidades: Any, *ruta: str) -> bool | None:
    """capabilities.<ruta>.supported: True/False, o None si la API no lo informa."""
    obj = capacidades
    for nombre in ruta:
        obj = _campo(obj, nombre)
        if obj is None:
            return None
    valor = _campo(obj, "supported")
    return None if valor is None else bool(valor)


def tareas_anthropic(capacidades: Any) -> list[str]:
    """Tareas de Classmer que puede hacer un modelo de Claude según sus `capabilities` (objeto del SDK,
    dict o None). Lo que la API no informa cuenta como soportado."""
    tareas = []
    if _soporta(capacidades, "structured_outputs") is not False:
        tareas += ["clasificar", "revisar_transcript"]
    if (_soporta(capacidades, "image_input") is not False
            and _soporta(capacidades, "effort") is not False):
        tareas.append("apunte")
    return tareas


def modelo_anthropic(m: Any) -> ModeloDescubierto:
    """Un ModelInfo de `GET /v1/models` → modelo del catálogo (con sus tareas y detalles).
    Un modelo retirado (`lifecycle: retired`) ya no se puede usar: no le sirve a ninguna tarea."""
    capacidades = _campo(m, "capabilities")
    esfuerzo = _soporta(capacidades, "effort")
    esfuerzos = None
    if esfuerzo is False:
        esfuerzos = []
    elif esfuerzo:
        efecto = _campo(capacidades, "effort")
        niveles = {n: _soporta(efecto, n) for n in ESFUERZOS}
        # Si la API no informa ningún nivel, no se sabe cuáles (None), no "ninguno" ([]).
        if any(v is not None for v in niveles.values()):
            esfuerzos = [n for n, v in niveles.items() if v]
    detalles = {
        "esfuerzos": esfuerzos,
        "imagenes": _soporta(capacidades, "image_input"),
        "salida_estructurada": _soporta(capacidades, "structured_outputs"),
        "max_entrada": _campo(m, "max_input_tokens"),
        "max_salida": _campo(m, "max_tokens"),
    }
    ciclo = _campo(m, "lifecycle")
    if ciclo:
        detalles["ciclo"] = str(ciclo)
    id_ = str(_campo(m, "id") or "")
    tareas = [] if ciclo == "retired" or not id_ else tareas_anthropic(capacidades)
    return ModeloDescubierto(id_, str(_campo(m, "display_name") or id_), tareas, detalles)


def tareas_google(id_modelo: str, acciones: Iterable[str] | None) -> list[str]:
    """Tareas de Classmer que puede hacer un modelo de Gemini según su id y `supported_actions`."""
    id_ = id_modelo.removeprefix("models/").lower()
    genera = "generateContent" in set(acciones or [])
    tareas = []
    if genera and "transcribe" in id_:
        tareas.append("transcribir")
    if genera and id_.startswith("gemini-") and not GEMINI_NO_TEXTO_RE.search(id_):
        tareas.append("clasificar")
    return tareas


def modelo_google(m: Any) -> ModeloDescubierto:
    """Un types.Model de `models.list` → modelo del catálogo. El id va sin "models/"."""
    id_ = str(_campo(m, "name") or "").removeprefix("models/")
    acciones = list(_campo(m, "supported_actions") or [])
    detalles = {
        "acciones": acciones,
        "max_entrada": _campo(m, "input_token_limit"),
        "max_salida": _campo(m, "output_token_limit"),
    }
    return ModeloDescubierto(id_, str(_campo(m, "display_name") or id_), tareas_google(id_, acciones), detalles)


# --- listar en cada API ------------------------------------------------------------------------

def _cliente_anthropic(cfg: Config) -> Any:
    """Cliente de la API de Anthropic con la clave explícita (así el SDK no mira el entorno)."""
    import anthropic

    return anthropic.Anthropic(api_key=cfg.anthropic_api_key, base_url=URL_ANTHROPIC, timeout=TIMEOUT_S)


def _cliente_google(cfg: Config) -> Any:
    from classmer.ia.gemini import crear_cliente

    return crear_cliente(cfg, timeout_s=TIMEOUT_S)


def listar_anthropic(cliente: Any) -> list[ModeloDescubierto]:
    """Todos los modelos de la API de Anthropic, paginando con after_id mientras `has_more`."""
    modelos: list[ModeloDescubierto] = []
    siguiente: str | None = None
    for _ in range(MAX_PAGINAS):
        pagina = (cliente.models.list(limit=POR_PAGINA, after_id=siguiente) if siguiente
                  else cliente.models.list(limit=POR_PAGINA))
        modelos.extend(modelo_anthropic(m) for m in (_campo(pagina, "data") or []))
        if not _campo(pagina, "has_more"):
            return modelos
        siguiente = _campo(pagina, "last_id")
        if not siguiente:
            # Una lista a medias no se puede tomar por completa: marcaría como ausentes a los que faltan.
            raise RuntimeError("la API de modelos dice que hay más páginas pero no da last_id")
    raise RuntimeError(f"la API de modelos sigue paginando después de {MAX_PAGINAS} páginas")


def listar_google(cliente: Any) -> list[ModeloDescubierto]:
    """Todos los modelos de la API de Gemini (el Pager del SDK pide las páginas siguientes solo)."""
    return [modelo_google(m) for m in cliente.models.list(config={"page_size": POR_PAGINA})]


# --- redescubrir ---------------------------------------------------------------------------------

@dataclass
class DescubrimientoProveedor:
    proveedor: str
    ok: bool
    detalle: str | None = None
    listados: int = 0       # todos los modelos que devolvió la API
    cantidad: int = 0       # los que le sirven a Classmer (quedaron presentes en el catálogo)
    nuevos: list[str] = field(default_factory=list)
    desaparecidos: list[str] = field(default_factory=list)
    volvieron: list[str] = field(default_factory=list)

    def texto(self) -> str:
        nombre = NOMBRE_PROVEEDOR.get(self.proveedor, self.proveedor)
        if not self.ok:
            return f"{nombre}: no actualicé el catálogo ({self.detalle}); queda lo que había"
        partes = [f"{self.cantidad} modelos para Classmer (de {self.listados} que lista la API)"]
        if self.nuevos:
            partes.append(f"nuevos: {', '.join(self.nuevos)}")
        if self.volvieron:
            partes.append(f"volvieron: {', '.join(self.volvieron)}")
        if self.desaparecidos:
            partes.append(f"no aparecieron: {', '.join(self.desaparecidos)}")
        return f"{nombre}: " + "; ".join(partes)


@dataclass
class ResumenDescubrimiento:
    momento: str
    proveedores: dict[str, DescubrimientoProveedor]

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.proveedores.values())

    def __getitem__(self, proveedor: str) -> DescubrimientoProveedor:
        return self.proveedores[proveedor]

    def lineas(self) -> list[str]:
        return [p.texto() for p in self.proveedores.values()]


def _sin_claves(texto: str, cfg: Config) -> str:
    """Nunca dejar una clave en un mensaje que va a la base, al log o a la pantalla."""
    for clave in (cfg.anthropic_api_key, cfg.gemini_api_key):
        if clave:
            texto = texto.replace(clave, "***")
    return texto


def _describir_error(e: BaseException, cfg: Config) -> str:
    codigo = getattr(e, "status_code", None) or getattr(e, "code", None)
    texto = " ".join((str(e) or "").split())
    detalle = e.__class__.__name__ + (f" {codigo}" if codigo else "") + (f": {texto}" if texto else "")
    return _sin_claves(detalle, cfg)[:300]


def _anotar(conn: sqlite3.Connection, r: DescubrimientoProveedor, momento: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO descubrimientos (proveedor, momento, ok, detalle, cantidad) VALUES (?, ?, ?, ?, ?)",
        (r.proveedor, momento, 1 if r.ok else 0, r.detalle, r.cantidad if r.ok else None),
    )


def _guardar(conn: sqlite3.Connection, proveedor: str, modelos: list[ModeloDescubierto],
             momento: str) -> DescubrimientoProveedor:
    """Upsert de lo que devolvió la API y `presente = 0` para lo que no apareció (o ya no sirve)."""
    r = DescubrimientoProveedor(proveedor, ok=True, listados=len(modelos))
    utiles = {m.id: m for m in modelos if m.tareas}
    existentes = {f["id"]: bool(f["presente"])
                  for f in conn.execute("SELECT id, presente FROM modelos WHERE proveedor = ?", (proveedor,))}
    for m in utiles.values():
        valores = (m.nombre, json.dumps(_ordenar_tareas(m.tareas)), json.dumps(m.detalles, ensure_ascii=False))
        if m.id in existentes:
            # `descubierto` y `origen` se conservan: es cuándo entró al catálogo y de dónde.
            conn.execute("UPDATE modelos SET nombre = ?, tareas = ?, detalles = ?, visto = ?, presente = 1 "
                         "WHERE proveedor = ? AND id = ?", (*valores, momento, proveedor, m.id))
            if not existentes[m.id]:
                r.volvieron.append(m.id)
        else:
            conn.execute("INSERT INTO modelos (proveedor, id, nombre, tareas, detalles, descubierto, visto, "
                         "presente, origen) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'api')",
                         (proveedor, m.id, *valores, momento, momento))
            r.nuevos.append(m.id)
    for id_, presente in existentes.items():
        if id_ not in utiles:
            conn.execute("UPDATE modelos SET presente = 0 WHERE proveedor = ? AND id = ?", (proveedor, id_))
            if presente:
                r.desaparecidos.append(id_)
    r.cantidad = len(utiles)
    r.nuevos.sort()
    r.desaparecidos.sort()
    r.volvieron.sort()
    return r


def _descubrir(cfg: Config, base: Base, proveedor: str, clave: str | None, cliente: Any,
               crear: Callable[[Config], Any], listar: Callable[[Any], list[ModeloDescubierto]],
               momento: str) -> DescubrimientoProveedor:
    if not clave:
        r = DescubrimientoProveedor(proveedor, ok=False,
                                    detalle=f"falta {CLAVE_PROVEEDOR[proveedor]} en Classmer-data\\.env")
    else:
        propio = cliente is None
        try:
            if propio:
                cliente = crear(cfg)
            modelos = listar(cliente)
        except Exception as e:  # noqa: BLE001 - red, API o SDK: se mantiene lo que había
            r = DescubrimientoProveedor(proveedor, ok=False, detalle=_describir_error(e, cfg))
        else:
            if modelos:
                with base.conexion() as conn, transaccion(conn):
                    r = _guardar(conn, proveedor, modelos, momento)
                    _anotar(conn, r, momento)
                return r
            r = DescubrimientoProveedor(proveedor, ok=False,
                                        detalle="la API no devolvió ningún modelo: no toqué el catálogo")
        finally:
            if propio and cliente is not None and callable(getattr(cliente, "close", None)):
                try:
                    cliente.close()
                except Exception:  # noqa: BLE001
                    pass
    with base.conexion() as conn:
        _anotar(conn, r, momento)
    return r


def redescubrir(cfg: Config, base: Base, *, cliente_anthropic: Any = None,
                cliente_google: Any = None) -> ResumenDescubrimiento:
    """Consulta las APIs de modelos de Anthropic y Google y actualiza el catálogo.

    Cada proveedor por separado: sin su clave o si su API falla, no se toca nada suyo y queda anotado
    en `descubrimientos` (ok = 0, con el motivo, sin la clave). Los clientes se inyectan en los tests.
    """
    momento = ahora_iso(cfg.zona_horaria)
    resultados = {
        ANTHROPIC: _descubrir(cfg, base, ANTHROPIC, cfg.anthropic_api_key, cliente_anthropic,
                              _cliente_anthropic, listar_anthropic, momento),
        GOOGLE: _descubrir(cfg, base, GOOGLE, cfg.gemini_api_key, cliente_google,
                           _cliente_google, listar_google, momento),
    }
    for r in resultados.values():
        (log.info if r.ok else log.warning)("redescubrir modelos: %s", r.texto())
    return ResumenDescubrimiento(momento, resultados)
