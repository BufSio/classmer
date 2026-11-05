"""Config editable sin reiniciar (§13): escribir config.yaml conservando comentarios y recargarla.

- Las ediciones (panel y PATCH /api/materias) tocan sólo las claves que cambian, con ruamel.yaml
  en modo round-trip: comentarios, comillas y líneas en blanco quedan como estaban. Antes de
  escribir se valida el resultado entero; se escribe atómico (temporal + renombrar).
- `ConfigViva` vigila config.yaml y .env (cada ~2 s) y aplica los cambios *sobre el mismo objeto*
  Config que usan la API, el worker y las etapas: nadie guarda una copia vieja.
- Si una edición a mano deja el archivo inválido, se sigue con la config anterior y el error se
  ve en el panel (Estado).
- `home`, `datos` y `servidor` sólo cambian al reiniciar.
- Página Modelos del panel (`editar_modelos`): modelo, motor y esfuerzo por tarea y la facturación
  de Claude. El modelo propio del apunte de una materia va en `editar_materia(…, {"apunte": …})`.
"""

from __future__ import annotations

import io
import logging
import math
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.scalarstring import (
    DoubleQuotedScalarString,
    FoldedScalarString,
    LiteralScalarString,
    SingleQuotedScalarString,
)
from ruamel.yaml.tokens import CommentToken

from classmer.config import (
    CARPETA_CLASES_DEFAULT,
    ESFUERZOS,
    FACTURACIONES,
    ID_MATERIA_RE,
    MOTORES_CLASIFICAR,
    REVISION_DEFAULT,
    TIPOS_REVISION,
    Apunte,
    Clasificar,
    Claude,
    Config,
    ConfigError,
    Materia,
    ModeloApunte,
    RevisarTranscript,
    Transcribir,
    cargar_config,
    config_desde_texto,
    tipo_revision,
)
from classmer.util import ahora_iso, escribir_atomico

log = logging.getLogger("classmer.config")

CAMPOS_CON_REINICIO = ("home", "datos", "servidor")
# Lo que el panel puede cambiar de una materia (el id no: las sesiones apuntan a él).
CAMPOS_MATERIA = ("nombre", "carpeta", "perfil", "carpeta_clases", "calendario", "tipos_clase",
                  "estilo", "numero_clase_inicial", "activa", "apunte", "revision", "profesor")
VIEJA_REVISION = "revisar_transcript"   # el switch de antes del tipo de revisión (true/false)
DEFAULTS_MATERIA: dict[str, Any] = {"perfil": None, "carpeta_clases": CARPETA_CLASES_DEFAULT, "estilo": None,
                                    "numero_clase_inicial": 1, "activa": True, "revision": REVISION_DEFAULT,
                                    "profesor": None}
# Lo que se edita desde la página Modelos, por sección de config.yaml.
CAMPOS_MODELOS: dict[str, tuple[str, ...]] = {
    "transcribir": ("modelo",),
    "clasificar": ("motor", "modelo", "esfuerzo", "umbral_confianza"),
    "revisar_transcript": ("modelo", "esfuerzo"),
    "apunte": ("modelo", "esfuerzo"),
    "claude": ("facturacion", "reintentar_api_h"),
}
_CLASE_SECCION: dict[str, type[BaseModel]] = {
    "transcribir": Transcribir, "clasificar": Clasificar, "revisar_transcript": RevisarTranscript,
    "apunte": Apunte, "claude": Claude,
}

_lock_escritura = threading.RLock()


class MateriaNoExiste(LookupError):
    """No hay una materia con ese id en config.yaml (→ 404)."""


# --- YAML round-trip ---------------------------------------------------------------------------

def _yaml() -> YAML:
    y = YAML()  # round-trip: conserva comentarios, orden y comillas
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)  # el estilo de config.example.yaml ("  - id: …")
    y.representer.add_representer(
        type(None), lambda r, _d: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return y


def leer_documento(home: Path) -> tuple[Any, dict[str, Any]]:
    """(documento ruamel, formato original) de config.yaml. Sin archivo, un documento vacío."""
    ruta = home / "config.yaml"
    crudo = ruta.read_bytes().decode("utf-8") if ruta.is_file() else ""
    formato = {"bom": crudo.startswith("﻿"), "crlf": "\r\n" in crudo}
    texto = crudo.lstrip("﻿").replace("\r\n", "\n")
    doc = _yaml().load(texto) if texto.strip() else None
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise ConfigError("config.yaml tiene que ser un mapa")
    return doc, formato


def volcar(doc: Any, formato: dict[str, Any]) -> str:
    salida = io.StringIO()
    _yaml().dump(doc, salida)
    texto = salida.getvalue()
    if formato.get("crlf"):
        texto = texto.replace("\n", "\r\n")
    return ("﻿" if formato.get("bom") else "") + texto


def _con_estilo(anterior: Any, valor: Any) -> Any:
    """El valor nuevo con el estilo del anterior: comillas, bloque literal para textos largos."""
    if isinstance(valor, str):
        if "\n" in valor.strip("\n"):
            return LiteralScalarString(valor if valor.endswith("\n") else valor + "\n")
        if isinstance(anterior, SingleQuotedScalarString):
            return SingleQuotedScalarString(valor)
        if isinstance(anterior, DoubleQuotedScalarString):
            return DoubleQuotedScalarString(valor)
        if valor[:1] in "%'\"" or "\\" in valor:
            return SingleQuotedScalarString(valor)  # rutas de Windows: entre comillas simples
    return valor


def _es_numero(valor: Any) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _igual(actual: Any, valor: Any) -> bool:
    if isinstance(actual, str) and isinstance(valor, str):
        return str(actual).rstrip() == valor.rstrip()
    if _es_numero(actual) and _es_numero(valor):
        return actual == valor  # 0.75 leído (ScalarFloat) == 0.75
    return type(actual) is type(valor) and actual == valor


# --- comentarios al insertar o borrar claves ---------------------------------------------------
# ruamel guarda las líneas en blanco y los comentarios que van ARRIBA de una clave pegados a la
# clave anterior (a la última clave de su valor, si es un mapa bloque). Al insertar o borrar una
# clave hay que pasarlos de lugar para que sigan arriba de lo que comentaban.

def _portador(mapa: CommentedMap, clave: Any) -> tuple[Any, Any, int]:
    """(contenedor, clave o índice, lugar en `ca.items`) que guarda los comentarios que siguen a
    `clave`. Baja por mapas y listas en bloque (`tipos_clase:` con `- teorica` abajo): ahí ruamel
    los guarda en el último ítem, en el lugar 0 en vez del 2."""
    contenedor, clave, lugar = mapa, clave, 2
    valor = contenedor[clave]
    while isinstance(valor, (CommentedMap, CommentedSeq)) and len(valor) and not valor.fa.flow_style():
        if isinstance(valor, CommentedMap):
            contenedor, clave, lugar = valor, list(valor)[-1], 2
        else:
            contenedor, clave, lugar = valor, len(valor) - 1, 0
        valor = contenedor[clave]
    return contenedor, clave, lugar


def _es_bloque(valor: Any) -> bool:
    return isinstance(valor, (LiteralScalarString, FoldedScalarString))


def _partir(mapa: Any, clave: Any, lugar: int = 2) -> tuple[str, str]:
    """(comentario en la línea de la clave, lo que viene abajo: líneas en blanco y comentarios)."""
    items = mapa.ca.items.get(clave)
    token = items[lugar] if items and len(items) > lugar else None
    if not isinstance(token, CommentToken) or not isinstance(token.value, str):
        return "", ""
    texto = token.value
    if _es_bloque(mapa[clave]):
        # Un bloque | ya termina con su salto de línea: todo es de abajo. La primera línea se
        # escribe en la columna del token; al moverla, la sangría tiene que ir en el texto.
        if texto[:1] not in ("", "\n") and token.column:
            texto = " " * token.column + texto
        return "", texto
    corte = texto.find("\n") + 1
    if not corte:
        return texto, ""
    return texto[:corte], texto[corte:]


def _fijar(mapa: Any, clave: Any, lugar: int, linea: str, abajo: str) -> None:
    """Reemplaza los comentarios que siguen a `clave` (ver `_partir`)."""
    if abajo and not linea and not _es_bloque(mapa[clave]):
        linea = "\n"  # sin comentario en la línea: el token arranca con su salto de línea
    texto = linea + abajo
    items = mapa.ca.items.get(clave)
    token = items[lugar] if items and len(items) > lugar else None
    bloque = _es_bloque(mapa[clave])
    if not texto or (texto == "\n" and not bloque):
        if token is not None:
            items[lugar] = None
        return
    if isinstance(token, CommentToken) and not bloque:
        token.value = texto  # conserva la columna del comentario de la línea
        return
    # Token nuevo en la columna 0: después de un bloque | la sangría ya viene en el texto.
    items = mapa.ca.items.setdefault(clave, [None, None, None, None])
    items[lugar] = CommentToken(texto, CommentMark(0), None)


def _insertar(mapa: CommentedMap, pos: int, clave: str, valor: Any, *, separar: bool = False) -> None:
    """Inserta `clave` en `pos`; lo que estaba arriba de la clave siguiente sigue arriba de ella.
    `separar` (secciones de primer nivel): los comentarios con sangría, que son de la sección
    anterior, se quedan con ella, y queda una línea en blanco antes de la sección nueva."""
    abajo = ""
    if pos > 0:
        anterior = _portador(mapa, list(mapa)[pos - 1])
        linea, abajo = _partir(*anterior)
        if abajo:
            queda = ""
            if separar:
                lineas = abajo.splitlines(keepends=True)
                n = 0
                while n < len(lineas) and lineas[n][:1] in (" ", "\t") and lineas[n].lstrip().startswith("#"):
                    n += 1
                queda, abajo = "".join(lineas[:n]), "".join(lineas[n:])
                if abajo.startswith("\n"):
                    queda += "\n"
            _fijar(*anterior, linea, queda)
    mapa.insert(pos, clave, valor)
    if abajo:
        _fijar(*_portador(mapa, clave), "", abajo)


def _quitar(mapa: CommentedMap, clave: str) -> None:
    """Borra `clave`; lo que estaba arriba de la clave siguiente pasa a la anterior."""
    claves = list(mapa)
    pos = claves.index(clave)
    _, abajo = _partir(*_portador(mapa, clave))
    del mapa[clave]
    mapa.ca.items.pop(clave, None)
    if abajo and pos > 0:
        anterior = _portador(mapa, claves[pos - 1])
        linea, propio = _partir(*anterior)
        _fijar(*anterior, linea, propio + abajo)


def _posicion(mapa: CommentedMap, clave: str, orden: list[str]) -> int:
    """Dónde va una clave nueva: después de la última que la precede en `orden`; si no hay, antes
    de la primera que la sigue; si tampoco, al final."""
    claves = list(mapa)
    i = orden.index(clave) if clave in orden else len(orden)
    antes = [p for p, k in enumerate(claves) if k in orden[:i]]
    if antes:
        return antes[-1] + 1
    despues = [p for p, k in enumerate(claves) if k in orden[i + 1:]]
    return despues[0] if despues else len(claves)


def _poner(mapa: CommentedMap, clave: str, valor: Any, *, default: Any = ...) -> None:
    """Asigna sólo si cambia. Si la clave no está y el valor es vacío o el default, no la agrega."""
    if clave in mapa:
        actual = mapa[clave]
        if isinstance(actual, CommentedSeq) and isinstance(valor, list):
            if list(actual) != valor:
                actual[:] = valor  # en el lugar: conserva el estilo [a, b] y sus comentarios
            return
        if not _igual(actual, valor):
            mapa[clave] = _con_estilo(actual, valor)
        return
    if valor is None or (default is not ... and valor == default):
        return
    if isinstance(valor, list):
        seq = CommentedSeq(valor)
        seq.fa.set_flow_style()
        valor = seq
    _insertar(mapa, len(mapa), clave, _con_estilo(None, valor))


def _texto_opcional(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).replace("\r\n", "\n")
    return texto if texto.strip() else None


def _esfuerzo(valor: Any, nombre: str) -> str | None:
    """Un nivel de esfuerzo de la lista; vacío o None = null (hereda o sin esfuerzo)."""
    texto = str(valor).strip() if valor is not None else ""
    if not texto:
        return None
    if texto not in ESFUERZOS:
        raise ConfigError(f"{nombre} inválido: {texto!r} (opciones: {', '.join(ESFUERZOS)})")
    return texto


def _numero(valor: Any, nombre: str) -> int | float:
    """Un número del formulario ("0,6" también vale). Los enteros quedan enteros en el YAML."""
    if isinstance(valor, bool):
        raise ConfigError(f"{nombre}: número inválido: {valor!r}")
    try:
        numero = float(str(valor).strip().replace(",", ".")) if isinstance(valor, str) else float(valor)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{nombre}: número inválido: {valor!r}") from e
    if not math.isfinite(numero):
        raise ConfigError(f"{nombre}: número inválido: {valor!r}")
    return int(numero) if numero.is_integer() else numero


def _proveedor(modelo: str | None) -> str | None:
    """De quién es un id de modelo (por el prefijo: así los nombra cada API)."""
    texto = (modelo or "").strip().lower().removeprefix("models/")  # Gemini acepta "models/gemini-…"
    for proveedor in ("claude", "gemini"):
        if texto.startswith(proveedor):
            return proveedor
    return None


def _id_modelo(texto: str, nombre: str) -> str:
    """Un id de modelo: sin espacios ni saltos de línea (en el YAML quedaría un bloque `|`)."""
    if any(c.isspace() for c in texto):
        raise ConfigError(f"{nombre} inválido: {texto!r} (un id de modelo no lleva espacios)")
    return texto


def _apunte_materia(valor: Any) -> dict[str, Any] | None:
    """El modelo propio del apunte de una materia: None (hereda el global) o {modelo, esfuerzo}."""
    if valor is None:
        return None
    if isinstance(valor, ModeloApunte):
        valor = valor.model_dump()
    if not isinstance(valor, Mapping):
        raise ConfigError("apunte: se esperaba {modelo, esfuerzo} o nada (hereda el global)")
    desconocidos = set(valor) - {"modelo", "esfuerzo"}
    if desconocidos:
        raise ConfigError(f"apunte: campos desconocidos: {', '.join(sorted(map(str, desconocidos)))}")
    modelo = str(valor.get("modelo") or "").strip()
    if not modelo:
        return None
    _id_modelo(modelo, "apunte.modelo")
    if _proveedor(modelo) == "gemini":
        raise ConfigError(f"el apunte lo escribe Claude: {modelo!r} no sirve")
    return {"modelo": modelo, "esfuerzo": _esfuerzo(valor.get("esfuerzo"), "apunte.esfuerzo")}


def _mapa_apunte(valor: dict[str, Any]) -> CommentedMap:
    mapa = CommentedMap()  # bloque, como el resto de la materia
    mapa["modelo"] = _con_estilo(None, valor["modelo"])
    if valor["esfuerzo"] is not None:
        mapa["esfuerzo"] = valor["esfuerzo"]
    return mapa


def _poner_apunte(mapa: CommentedMap, valor: dict[str, Any] | None) -> None:
    """`apunte:` de una materia: None la borra (hereda el global); si no, {modelo, esfuerzo} (sin
    esfuerzo, la clave no va: hereda el esfuerzo global)."""
    actual = mapa.get("apunte")
    if valor is None:
        if "apunte" in mapa:
            _quitar(mapa, "apunte")
        return
    if isinstance(actual, CommentedMap):
        _poner(actual, "modelo", valor["modelo"])
        if valor["esfuerzo"] is not None:
            _poner(actual, "esfuerzo", valor["esfuerzo"])
        elif "esfuerzo" in actual:
            _quitar(actual, "esfuerzo")
        return
    pos = len(mapa)
    if "apunte" in mapa:  # `apunte: null` (u otra cosa) escrito a mano: va un mapa en su lugar
        pos = list(mapa).index("apunte")
        _quitar(mapa, "apunte")
    _insertar(mapa, pos, "apunte", _mapa_apunte(valor))


def _normalizar_modelos(cambios: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Valida lo que manda la página Modelos (antes de tocar el archivo): secciones y campos
    conocidos, esfuerzo/facturación/motor de su lista, números en rango, modelos no vacíos."""
    if not isinstance(cambios, Mapping):
        raise ConfigError("se esperaba un mapa de secciones")
    limpio: dict[str, dict[str, Any]] = {}
    for seccion, campos in cambios.items():
        if seccion not in CAMPOS_MODELOS:
            raise ConfigError(f"sección desconocida: {seccion!r} (se editan: {', '.join(CAMPOS_MODELOS)})")
        if not isinstance(campos, Mapping):
            raise ConfigError(f"{seccion}: se esperaba un mapa de campos")
        desconocidos = set(campos) - set(CAMPOS_MODELOS[seccion])
        if desconocidos:
            raise ConfigError(f"{seccion}: campos desconocidos: {', '.join(sorted(map(str, desconocidos)))}")
        valores: dict[str, Any] = {}
        for clave, valor in campos.items():
            nombre = f"{seccion}.{clave}"
            if clave == "esfuerzo":
                valores[clave] = _esfuerzo(valor, nombre)
            elif clave == "umbral_confianza":
                valores[clave] = _numero(valor, nombre)
                if not 0 <= valores[clave] <= 1:
                    raise ConfigError(f"{nombre} tiene que estar entre 0 y 1")
            elif clave == "reintentar_api_h":
                valores[clave] = _numero(valor, nombre)
                if valores[clave] <= 0:
                    raise ConfigError(f"{nombre} tiene que ser mayor que 0")
            else:
                texto = str(valor).strip() if valor is not None else ""
                if not texto:
                    raise ConfigError(f"falta {nombre}")
                opciones = {"motor": MOTORES_CLASIFICAR, "facturacion": FACTURACIONES}.get(clave)
                if opciones and texto not in opciones:
                    raise ConfigError(f"{nombre} inválido: {texto!r} (opciones: {', '.join(opciones)})")
                valores[clave] = _id_modelo(texto, nombre) if clave == "modelo" else texto
        limpio[seccion] = valores
    return limpio


def _aplicar_seccion(doc: CommentedMap, seccion: str, valores: dict[str, Any]) -> None:
    """Pone los valores en `seccion` tocando sólo lo que cambia. Una clave que no está y queda en
    su valor por defecto no se agrega; si no, va en su lugar lógico (esfuerzo después de modelo).
    La sección se crea (bloque, en su lugar entre las otras) sólo si hace falta."""
    clase = _CLASE_SECCION[seccion]
    campos = list(clase.model_fields)
    actual = doc.get(seccion)
    if actual is not None and not isinstance(actual, CommentedMap):
        raise ConfigError(f"`{seccion}` tiene que ser un mapa en config.yaml")
    mapa = actual if actual is not None else CommentedMap()
    for clave, valor in valores.items():
        if clave in mapa:
            if not _igual(mapa[clave], valor):
                mapa[clave] = _con_estilo(mapa[clave], valor)
        elif not _igual(clase.model_fields[clave].default, valor):
            _insertar(mapa, _posicion(mapa, clave, campos), clave, _con_estilo(None, valor))
    if actual is None and len(mapa):
        if seccion in doc:  # `seccion: null` escrito a mano
            pos = list(doc).index(seccion)
            _quitar(doc, seccion)
        else:
            pos = _posicion(doc, seccion, list(Config.model_fields))
        _insertar(doc, pos, seccion, mapa, separar=True)


def _verificar_modelos(cfg: Config, secciones: set[str]) -> None:
    """Que cada tarea quede con un modelo de su proveedor (un id de Gemini en el apunte fallaría en
    cada job). Sólo mira las secciones que se están editando."""
    if "transcribir" in secciones and _proveedor(cfg.transcribir.modelo) == "claude":
        raise ConfigError(f"transcribir usa Gemini: {cfg.transcribir.modelo!r} no sirve")
    if "clasificar" in secciones:
        c = cfg.clasificar
        proveedor = _proveedor(c.modelo)
        if proveedor is not None and proveedor != c.motor:
            raise ConfigError(f"clasificar: el motor {c.motor} no puede usar {c.modelo!r} (es de {proveedor}): "
                              "elegí motor y modelo del mismo proveedor")
    if "apunte" in secciones and _proveedor(cfg.apunte.modelo) == "gemini":
        raise ConfigError(f"el apunte lo escribe Claude: {cfg.apunte.modelo!r} no sirve")
    if "revisar_transcript" in secciones and _proveedor(cfg.revisar_transcript.modelo) == "gemini":
        raise ConfigError(f"la revisión del transcript la hace Claude: {cfg.revisar_transcript.modelo!r} no sirve")


def _normalizar_cambios(cambios: dict[str, Any]) -> dict[str, Any]:
    desconocidos = set(cambios) - set(CAMPOS_MATERIA) - {"id"}
    if desconocidos:
        raise ConfigError(f"campos desconocidos: {', '.join(sorted(desconocidos))}")
    limpio: dict[str, Any] = {}
    for clave, valor in cambios.items():
        if clave == "id":
            continue
        if clave in ("nombre", "carpeta", "carpeta_clases", "perfil"):
            valor = (str(valor).strip() if valor is not None else "") or None
        elif clave == "profesor":
            valor = " ".join(str(valor or "").split()) or None
        elif clave == "revision":
            valor = str(valor or "").strip().lower() or REVISION_DEFAULT
            if valor not in TIPOS_REVISION:
                raise ConfigError(f"tipo de revisión inválido: {valor!r} (opciones: {', '.join(TIPOS_REVISION)})")
        elif clave == "estilo":
            valor = _texto_opcional(valor)
            if valor is not None:
                valor = valor.rstrip() + "\n"
        elif clave == "calendario":
            valor = {k: ((str(v).strip() or None) if v is not None else None)
                     for k, v in dict(valor or {}).items() if k in ("calendario", "titulo_regex")}
        elif clave == "tipos_clase":
            valor = [str(t).strip() for t in (valor or []) if str(t).strip()]
            if not valor:
                raise ConfigError("tipos_clase no puede quedar vacío")
        elif clave == "numero_clase_inicial":
            valor = int(valor)
        elif clave == "activa":
            valor = bool(valor)
        elif clave == "apunte":
            valor = _apunte_materia(valor)
        limpio[clave] = valor
    if "nombre" in limpio and not limpio["nombre"]:
        raise ConfigError("la materia necesita un nombre")
    return limpio


def _aplicar_a_mapa(mapa: CommentedMap, cambios: dict[str, Any]) -> None:
    for clave, valor in cambios.items():
        if clave == "calendario":
            cal = mapa.get("calendario")
            if not isinstance(cal, CommentedMap):
                cal = CommentedMap()
                if "calendario" in mapa:
                    mapa["calendario"] = cal
                else:
                    _insertar(mapa, len(mapa), "calendario", cal)
            for k in ("calendario", "titulo_regex"):
                if k in valor:
                    _poner(cal, k, valor[k])
            continue
        if clave == "apunte":
            _poner_apunte(mapa, valor)
            continue
        if clave == "revision":
            _poner_revision(mapa, valor)
            continue
        _poner(mapa, clave, valor, default=DEFAULTS_MATERIA.get(clave, ...))


def _poner_revision(mapa: CommentedMap, valor: str) -> None:
    """`revision:` de una materia (sin escribir si es el default). El switch viejo `revisar_transcript`
    se va: si hay que escribir el tipo, en su lugar y con sus comentarios."""
    if VIEJA_REVISION in mapa and "revision" not in mapa and valor != REVISION_DEFAULT:
        pos = list(mapa).index(VIEJA_REVISION)
        comentarios = mapa.ca.items.pop(VIEJA_REVISION, None)
        del mapa[VIEJA_REVISION]
        mapa.insert(pos, "revision", valor)
        if comentarios is not None:
            mapa.ca.items["revision"] = comentarios
        return
    _poner(mapa, "revision", valor, default=REVISION_DEFAULT)
    if VIEJA_REVISION in mapa:
        _quitar(mapa, VIEJA_REVISION)


def _materias_doc(doc: CommentedMap) -> CommentedSeq:
    materias = doc.get("materias")
    if materias is None:
        materias = CommentedSeq()
        doc["materias"] = materias
    if not isinstance(materias, CommentedSeq):
        raise ConfigError("`materias` tiene que ser una lista")
    return materias


def materia_cruda(home: Path, materia_id: str) -> dict[str, Any] | None:
    """Los valores tal como están escritos (rutas con %USERPROFILE%), para los formularios."""
    doc, _ = leer_documento(home)
    for m in _materias_doc(doc):
        if isinstance(m, CommentedMap) and m.get("id") == materia_id:
            cal = m.get("calendario") or {}
            return {
                "id": materia_id,
                "nombre": m.get("nombre"),
                "carpeta": m.get("carpeta"),
                "perfil": m.get("perfil"),
                "carpeta_clases": m.get("carpeta_clases"),
                "calendario": {"calendario": cal.get("calendario"), "titulo_regex": cal.get("titulo_regex")},
                "tipos_clase": list(m.get("tipos_clase") or ["teorica", "practica"]),
                "estilo": m.get("estilo"),
                "numero_clase_inicial": m.get("numero_clase_inicial", 1),
                "activa": m.get("activa", True),
                "apunte": _apunte_crudo(m.get("apunte")),
                "revision": _revision_cruda(m),
                "profesor": m.get("profesor"),
            }
    return None


def _revision_cruda(m: Mapping[str, Any]) -> str | None:
    """El tipo de revisión escrito; con un switch viejo inválido, None (el formulario no marca ninguno y
    guardar sin elegir no lo toca)."""
    try:
        tipo = tipo_revision(m)
    except ValueError:
        return None
    return tipo if isinstance(tipo, str) else None


def _apunte_crudo(valor: Any) -> dict[str, Any] | None:
    """`apunte:` de una materia tal como está escrito: None (hereda el global) o {modelo, esfuerzo}."""
    if not isinstance(valor, Mapping) or not str(valor.get("modelo") or "").strip():
        return None
    return {"modelo": str(valor["modelo"]), "esfuerzo": valor.get("esfuerzo") or None}


# --- config viva -------------------------------------------------------------------------------

def _firma(home: Path) -> tuple[Any, ...]:
    partes: list[Any] = []
    for nombre in ("config.yaml", ".env"):
        try:
            st = (home / nombre).stat()
            partes.append((st.st_mtime_ns, st.st_size))
        except OSError:
            partes.append(None)
    return tuple(partes)


def aplicar(cfg: Config, nueva: Config) -> list[str]:
    """Copia `nueva` sobre `cfg` (el mismo objeto que usan todos). Devuelve los campos que no se
    aplicaron porque necesitan reiniciar."""
    pendientes = []
    for nombre in type(cfg).model_fields:
        if nombre in CAMPOS_CON_REINICIO:
            if getattr(cfg, nombre) != getattr(nueva, nombre):
                pendientes.append(nombre)
            continue
        setattr(cfg, nombre, getattr(nueva, nombre))
    return pendientes


class ConfigViva:
    """Mantiene `cfg` al día con config.yaml/.env y escribe las ediciones del panel y la API."""

    def __init__(self, cfg: Config, base: Any = None, *, intervalo_s: float = 2.0):
        self.cfg = cfg
        self.base = base
        self.intervalo_s = intervalo_s
        self.error: str | None = None
        self.pendientes_reinicio: list[str] = []
        self.recargada: str | None = None
        self._firma = _firma(cfg.home)
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    # --- recarga ---
    def recargar(self, *, forzar: bool = False) -> bool:
        """Relee config.yaml y .env si cambiaron. True si aplicó una config nueva."""
        with _lock_escritura:
            firma = _firma(self.cfg.home)
            if not forzar and firma == self._firma:
                return False
            self._firma = firma
            try:
                nueva = cargar_config(self.cfg.home)
            except ConfigError as e:
                self.error = str(e)
                log.warning("config.yaml no se pudo recargar; sigo con la anterior: %s", e)
                return False
            clave_antes = self.cfg.anthropic_api_key
            self.pendientes_reinicio = aplicar(self.cfg, nueva)
            self.error = None
            self.recargada = ahora_iso(self.cfg.zona_horaria)
            if self.pendientes_reinicio:
                log.warning("config recargada; para %s hay que reiniciar el server",
                            ", ".join(self.pendientes_reinicio))
            else:
                log.info("config recargada")
            self._sincronizar_base()
            if self.cfg.anthropic_api_key != clave_antes:
                self._clave_cambiada()
            return True

    def _clave_cambiada(self) -> None:
        """Con otra ANTHROPIC_API_KEY la API se vuelve a probar (ia.facturacion compara la huella): los
        jobs de Claude que esperaban por la clave vieja no tienen que esperar su run_after."""
        if self.base is None:
            return
        from classmer.ia.facturacion import despertar_pospuestos

        try:
            n = despertar_pospuestos(self.base)
        except Exception:  # noqa: BLE001 — la config ya se aplicó; los jobs se despiertan solos más tarde
            log.exception("no pude adelantar los jobs de Claude pospuestos")
            return
        log.info("cambió la ANTHROPIC_API_KEY: se vuelve a probar la API (%d job(s) adelantados)", n)

    def _sincronizar_base(self) -> None:
        if self.base is None:
            return
        from classmer.db import sincronizar_materias

        try:
            with self.base.conexion() as conn:
                sincronizar_materias(conn, self.cfg)
        except Exception:  # noqa: BLE001 — la tabla materias es un índice; la config ya se aplicó
            log.exception("no pude sincronizar las materias en la base")

    def iniciar(self) -> None:
        if self._hilo is not None:
            return
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, name="classmer-config", daemon=True)
        self._hilo.start()

    def detener(self) -> None:
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout=5)
            self._hilo = None

    def _bucle(self) -> None:
        while not self._parar.wait(self.intervalo_s):
            try:
                self.recargar()
            except Exception:  # noqa: BLE001 — el vigilante no se muere
                log.exception("error vigilando config.yaml")

    # --- escritura ---
    def _escribir(self, doc: Any, formato: dict[str, Any], *,
                  verificar: Callable[[Config], None] | None = None) -> Config:
        """Valida el documento entero (y `verificar`), lo escribe atómico y lo aplica. ConfigError si
        es inválido."""
        texto = volcar(doc, formato)
        nueva = config_desde_texto(self.cfg.home, texto.lstrip("﻿"))
        if verificar is not None:
            verificar(nueva)
        escribir_atomico(self.cfg.home / "config.yaml", texto.encode("utf-8"))
        self.recargar(forzar=True)
        return nueva

    def editar_modelos(self, cambios: Mapping[str, Mapping[str, Any]]) -> Config:
        """Página Modelos: `{"transcribir": {modelo}, "clasificar": {motor, modelo, esfuerzo,
        umbral_confianza}, "revisar_transcript": {modelo, esfuerzo}, "apunte": {modelo, esfuerzo},
        "claude": {facturacion, reintentar_api_h}}`
        (cualquier subconjunto). Esfuerzo vacío o None = null. Toca sólo las claves que cambian y
        crea la sección que falte; sin cambios no escribe. Devuelve la config viva (ya aplicada)."""
        limpio = _normalizar_modelos(cambios)
        with _lock_escritura:
            doc, formato = leer_documento(self.cfg.home)
            antes = volcar(doc, formato)
            for seccion, valores in limpio.items():
                _aplicar_seccion(doc, seccion, valores)
            if volcar(doc, formato) != antes:
                self._escribir(doc, formato, verificar=lambda nueva: _verificar_modelos(nueva, set(limpio)))
                log.info("modelos editados: %s", ", ".join(
                    f"{s}.{k}={v}" for s, valores in limpio.items() for k, v in valores.items()))
        return self.cfg

    def editar_materia(self, materia_id: str, cambios: dict[str, Any]) -> Materia:
        """Cambia los campos de `cambios` (ver CAMPOS_MATERIA). `apunte`: None (o modelo vacío) borra
        la clave y la materia hereda el modelo global; {modelo, esfuerzo} fija el propio."""
        with _lock_escritura:
            doc, formato = leer_documento(self.cfg.home)
            for mapa in _materias_doc(doc):
                if isinstance(mapa, CommentedMap) and mapa.get("id") == materia_id:
                    break
            else:
                raise MateriaNoExiste(materia_id)
            _aplicar_a_mapa(mapa, _normalizar_cambios(cambios))
            nueva = self._escribir(doc, formato)
        materia = nueva.materia(materia_id)
        assert materia is not None
        return self.cfg.materia(materia_id) or materia

    def agregar_materia(self, datos: dict[str, Any]) -> Materia:
        materia_id = str(datos.get("id") or "").strip()
        if not ID_MATERIA_RE.fullmatch(materia_id):
            raise ConfigError(f"id de materia inválido: {materia_id!r} (minúsculas, números, - y _)")
        with _lock_escritura:
            doc, formato = leer_documento(self.cfg.home)
            materias = _materias_doc(doc)
            if any(isinstance(m, CommentedMap) and m.get("id") == materia_id for m in materias):
                raise ConfigError(f"ya existe una materia con id {materia_id!r}")
            cambios = _normalizar_cambios({k: v for k, v in datos.items() if k != "id"})
            if not cambios.get("nombre"):
                raise ConfigError("la materia necesita un nombre")
            defaults = {**DEFAULTS_MATERIA, "carpeta": None}
            mapa = CommentedMap()
            mapa["id"] = materia_id
            for clave in ("nombre", "carpeta", "perfil", "carpeta_clases", "numero_clase_inicial"):
                if clave in cambios:
                    _poner(mapa, clave, cambios[clave], default=defaults.get(clave, ...))
            cal = cambios.get("calendario") or {}
            mapa["calendario"] = CommentedMap()  # con las dos claves, aunque sean null: así se ven
            for k in ("calendario", "titulo_regex"):
                mapa["calendario"][k] = _con_estilo(None, cal.get(k))
            _poner(mapa, "tipos_clase", cambios.get("tipos_clase") or ["teorica", "practica"])
            if cambios.get("apunte") is not None:
                mapa["apunte"] = _mapa_apunte(cambios["apunte"])
            for clave in ("estilo", "activa", "profesor", "revision"):
                if clave in cambios:
                    _poner(mapa, clave, cambios[clave], default=defaults[clave])
            materias.append(mapa)
            self._escribir(doc, formato)
        materia = self.cfg.materia(materia_id)
        assert materia is not None
        return materia

    def editar_estilo_global(self, texto: str) -> None:
        limpio = (texto or "").replace("\r\n", "\n").strip()
        with _lock_escritura:
            doc, formato = leer_documento(self.cfg.home)
            valor: Any = LiteralScalarString(limpio + "\n") if limpio else ""
            if str(doc.get("estilo_global") or "").strip() == limpio:
                return
            doc["estilo_global"] = valor
            self._escribir(doc, formato)

