"""Búsqueda de texto (§10): índice FTS5 `busqueda` sobre los apuntes y el transcript.

Cada fila de `busqueda` (la tabla está en el esquema de db.py) es un tramo de una clase:
- `texto`: lo que se busca. Un tramo del apunte en texto plano (párrafos seguidos de una misma
  sección), el título y los temas de una sección del `meta.json`, o unos segmentos seguidos del
  transcript (hasta ~45 s).
- `materia`: id de la materia; `ref`: id de la sesión.
- `tipo`: `apunte` | `transcript`.
- `inicio_ms`: dónde empieza el tramo en la grabación (para citar el minuto).

Se llena al publicar (`indexar_publicada`) y se rearma entero con `classmer reindexar`. Las
consultas buscan sólo en la columna `texto`: FTS5 indexa también las otras y, sin el filtro de
columna, "prueba" encontraría todas las clases de la materia `prueba`.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from classmer.db import transaccion
from classmer.util import leer_json, tiempo_a_ms

if TYPE_CHECKING:
    from classmer.config import Config
    from classmer.db import Base
    from classmer.pipeline import Contexto

log = logging.getLogger("classmer.busqueda")

TIPO_APUNTE = "apunte"
TIPO_TRANSCRIPT = "transcript"

# El mismo DDL que db.py: por si la base es de antes de que existiera la tabla.
DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS busqueda USING fts5(
    texto, materia, tipo, ref, inicio_ms UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""

TRAMO_TRANSCRIPT_MS = 45_000
TRAMO_TRANSCRIPT_CHARS = 500
TRAMO_APUNTE_CHARS = 700


@dataclass
class Tramo:
    texto: str
    inicio_ms: int | None


@dataclass
class Reindexado:
    sesiones: int
    filas: int


# --- texto plano ---------------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes (para comparar nombres de materias, temas y palabras vacías)."""
    sin_tildes = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return " ".join(sin_tildes.casefold().split())


_COMENTARIO = re.compile(r"(?<!\\)%.*")
_DIBUJOS = re.compile(r"\\begin\{(tikzpicture|axis|circuitikz|pgfpicture)\}.*?\\end\{\1\}", re.DOTALL)
_TS = re.compile(r"\\ts\s*\{([^{}]*)\}")
_SECCION = re.compile(r"\\section\*?\s*(?:\[[^\]]*\]\s*)?\{")
_ENTORNO = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}(?:\s*\[(?:clase|ia)\])?")
_PIZARRA = re.compile(r"\\pizarra\s*\{[^{}]*\}\s*\{[^{}]*\}")  # deja el epígrafe
_COMANDO = re.compile(r"\\[A-Za-z@]+\*?")
_SIMBOLO = re.compile(r"\\[\\,;:!> ]")
_ESCAPADO = re.compile(r"\\([#&%_])")


def _ms_de_ts(valor: str) -> int | None:
    try:
        return tiempo_a_ms(valor)
    except ValueError:
        return None


def latex_a_texto(fragmento: str) -> str:
    """Texto plano aproximado de un pedazo de LaTeX: sin comandos, entornos ni dibujos (TikZ)."""
    t = _COMENTARIO.sub("", fragmento)
    t = _DIBUJOS.sub(" ", t)
    t = _TS.sub(" ", t)
    t = _PIZARRA.sub(" ", t)
    t = _ENTORNO.sub(" ", t)
    t = _SIMBOLO.sub(" ", t)
    t = _ESCAPADO.sub(lambda m: m.group(1), t)
    t = _COMANDO.sub(" ", t)
    t = t.replace("``", "“").replace("''", "”").replace("~", " ")
    t = re.sub(r"[{}$\[\]]", " ", t)
    return " ".join(t.split())


def _argumento(texto: str, inicio: int) -> tuple[str, int]:
    """Contenido entre llaves balanceadas desde `inicio` (justo después de '{')."""
    nivel = 1
    i = inicio
    while i < len(texto) and nivel:
        c = texto[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            nivel += 1
        elif c == "}":
            nivel -= 1
        i += 1
    return texto[inicio:i - 1 if nivel == 0 else i], i


def _cuerpo(tex: str) -> str:
    sin_comentarios = "\n".join(_COMENTARIO.sub("", linea) for linea in tex.splitlines())
    _, sep, despues = sin_comentarios.partition("\\begin{document}")
    cuerpo = despues if sep else sin_comentarios
    return cuerpo.split("\\end{document}", 1)[0]


def _inicio_seccion(titulo: str, k: int, total: int, secciones: list[Mapping[str, Any]]) -> int | None:
    """Minuto de la k-ésima \\section según meta.json: por título o, si coinciden en cantidad, por orden."""
    clave = normalizar(titulo)
    for s in secciones:
        if normalizar(str(s.get("titulo") or "")) == clave and s.get("inicio_ms") is not None:
            return int(s["inicio_ms"])
    if total == len(secciones) and 0 <= k < len(secciones) and secciones[k].get("inicio_ms") is not None:
        return int(secciones[k]["inicio_ms"])
    return None


def tramos_apunte(tex: str, secciones: list[Mapping[str, Any]] | None = None) -> list[Tramo]:
    """El apunte en tramos de texto plano (párrafos seguidos, sin cruzar secciones).

    El minuto de cada tramo es el primer `\\ts{}` que tiene; si no tiene, el último anterior de su
    sección; si tampoco, el inicio de la sección en meta.json.
    """
    secciones = list(secciones or [])
    cuerpo = _cuerpo(tex)
    partes: list[tuple[str | None, str]] = []  # (título de la sección, LaTeX)
    pos = 0
    previo: str | None = None
    for m in _SECCION.finditer(cuerpo):
        if m.start() < pos:
            continue
        partes.append((previo, cuerpo[pos:m.start()]))
        previo, pos = _argumento(cuerpo, m.end())
        previo = previo.strip()
    partes.append((previo, cuerpo[pos:]))
    total = sum(1 for titulo, _ in partes if titulo is not None)

    tramos: list[Tramo] = []
    k = -1
    for titulo, latex in partes:
        if titulo is not None:
            k += 1
        ts_titulo = [_ms_de_ts(v) for v in _TS.findall(titulo or "")]
        actual = next((ms for ms in ts_titulo if ms is not None), None)
        if actual is None and titulo is not None:
            actual = _inicio_seccion(latex_a_texto(titulo), k, total, secciones)
        texto_titulo = latex_a_texto(titulo) if titulo else ""
        acumulado: list[str] = [texto_titulo] if texto_titulo else []
        inicio_tramo: int | None = actual

        def cerrar() -> None:
            texto = " ".join(acumulado).strip()
            if texto:
                tramos.append(Tramo(texto, inicio_tramo))

        for parrafo in re.split(r"\n\s*\n", latex):
            marcas = [ms for ms in (_ms_de_ts(v) for v in _TS.findall(parrafo)) if ms is not None]
            texto = latex_a_texto(parrafo)
            if not texto:
                if marcas:
                    actual = marcas[-1]
                continue
            if acumulado and sum(len(a) for a in acumulado) + len(texto) > TRAMO_APUNTE_CHARS:
                cerrar()
                acumulado = []
                inicio_tramo = None
            if not acumulado or inicio_tramo is None:
                inicio_tramo = marcas[0] if marcas else (inicio_tramo if acumulado else actual)
            acumulado.append(texto)
            if marcas:
                actual = marcas[-1]
        cerrar()
    return tramos


def tramos_secciones(secciones: list[Mapping[str, Any]]) -> list[Tramo]:
    """Una fila por sección de meta.json: título y temas (sirve para buscar por tema)."""
    tramos = []
    for s in secciones:
        titulo = " ".join(str(s.get("titulo") or "").split())
        temas = [" ".join(str(t).split()) for t in s.get("temas") or [] if str(t).strip()]
        if not titulo and not temas:
            continue
        texto = f"Sección: {titulo}." + (f" Temas: {', '.join(temas)}." if temas else "")
        inicio = s.get("inicio_ms")
        tramos.append(Tramo(texto, int(inicio) if inicio is not None else None))
    return tramos


def tramos_transcript(segmentos: Iterable[Mapping[str, Any]]) -> list[Tramo]:
    """Segmentos seguidos unidos en tramos de hasta ~45 s o ~500 caracteres."""
    tramos: list[Tramo] = []
    textos: list[str] = []
    inicio: int | None = None
    largo = 0
    for s in segmentos:
        texto = " ".join(str(s.get("texto") or "").split())
        if not texto:
            continue
        ms = int(s.get("inicio_ms") or 0)
        if textos and (ms - (inicio or 0) >= TRAMO_TRANSCRIPT_MS or largo + len(texto) > TRAMO_TRANSCRIPT_CHARS):
            tramos.append(Tramo(" ".join(textos), inicio))
            textos, largo = [], 0
        if not textos:
            inicio = ms
        textos.append(texto)
        largo += len(texto) + 1
    if textos:
        tramos.append(Tramo(" ".join(textos), inicio))
    return tramos


# --- datos de una clase --------------------------------------------------------------------------

def apunte_actual(conn: sqlite3.Connection, sesion_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM apuntes WHERE sesion_id = ? AND actual = 1 ORDER BY version DESC LIMIT 1",
        (sesion_id,),
    ).fetchone()


def carpeta_apunte(cfg: Config, sesion_id: str, apunte: Mapping[str, Any]) -> Path:
    """sesiones/<id>/apunte/vN/ del apunte (la fuente de verdad, no la copia publicada)."""
    ruta = apunte["ruta_tex"]
    if ruta:
        p = Path(ruta)
        return (p if p.is_absolute() else cfg.sesion_dir(sesion_id) / p).parent
    return cfg.sesion_dir(sesion_id) / "apunte" / f"v{apunte['version']}"


def leer_meta(cfg: Config, sesion_id: str, apunte: Mapping[str, Any] | None) -> dict[str, Any]:
    """meta.json del apunte (vacío si no está o no es un objeto)."""
    if apunte is None:
        return {}
    meta = leer_json(carpeta_apunte(cfg, sesion_id, apunte) / "meta.json", default={})
    return meta if isinstance(meta, dict) else {}


def secciones_meta(meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    secciones = meta.get("secciones")
    return [s for s in secciones if isinstance(s, dict)] if isinstance(secciones, list) else []


def segmentos_sesion(cfg: Config, conn: sqlite3.Connection, sesion_id: str) -> list[dict[str, Any]]:
    """Segmentos del transcript ordenados: de la base y, si no hay, de transcript.json."""
    filas = conn.execute(
        "SELECT inicio_ms, fin_ms, hablante, texto FROM segmentos WHERE sesion_id = ? ORDER BY inicio_ms, id",
        (sesion_id,),
    ).fetchall()
    if filas:
        return [dict(f) for f in filas]
    datos = leer_json(cfg.sesion_dir(sesion_id) / "transcript.json", default=[])
    if not isinstance(datos, list):
        return []
    segmentos = [
        {"inicio_ms": int(s.get("inicio_ms") or 0), "fin_ms": int(s.get("fin_ms") or s.get("inicio_ms") or 0),
         "hablante": s.get("hablante"), "texto": str(s.get("texto") or "")}
        for s in datos if isinstance(s, dict)
    ]
    return sorted(segmentos, key=lambda s: s["inicio_ms"])


def _filas_sesion(cfg: Config, conn: sqlite3.Connection, sesion_id: str) -> list[tuple[Any, ...]]:
    sesion = conn.execute("SELECT materia_id FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
    if sesion is None:
        return []
    materia = sesion["materia_id"] or ""
    filas: list[tuple[Any, ...]] = []
    apunte = apunte_actual(conn, sesion_id)
    if apunte is not None:
        meta = leer_meta(cfg, sesion_id, apunte)
        secciones = secciones_meta(meta)
        tex = carpeta_apunte(cfg, sesion_id, apunte) / "apunte.tex"
        try:
            latex = tex.read_text(encoding="utf-8", errors="replace")
        except OSError:
            latex = ""
        for t in tramos_secciones(secciones) + tramos_apunte(latex, secciones):
            filas.append((t.texto, materia, TIPO_APUNTE, sesion_id, t.inicio_ms))
    for t in tramos_transcript(segmentos_sesion(cfg, conn, sesion_id)):
        filas.append((t.texto, materia, TIPO_TRANSCRIPT, sesion_id, t.inicio_ms))
    return filas


_INSERTAR = "INSERT INTO busqueda (texto, materia, tipo, ref, inicio_ms) VALUES (?, ?, ?, ?, ?)"


def asegurar_tabla(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)


def indexar_sesion(cfg: Config, conn: sqlite3.Connection, sesion_id: str) -> int:
    """Reemplaza las filas de la sesión en el índice. Devuelve cuántas quedaron."""
    asegurar_tabla(conn)
    filas = _filas_sesion(cfg, conn, sesion_id)
    with transaccion(conn):
        conn.execute("DELETE FROM busqueda WHERE ref = ?", (sesion_id,))
        conn.executemany(_INSERTAR, filas)
    return len(filas)


def borrar_sesion(conn: sqlite3.Connection, sesion_id: str) -> None:
    """Saca una sesión del índice (p. ej., al borrarla)."""
    asegurar_tabla(conn)
    conn.execute("DELETE FROM busqueda WHERE ref = ?", (sesion_id,))


def indexar_publicada(ctx: Contexto, sesion_id: str) -> None:
    """Lo llama `publicar`. Un error acá no frena la publicación: queda en pipeline.log."""
    try:
        with ctx.base.conexion() as conn:
            n = indexar_sesion(ctx.cfg, conn, sesion_id)
        log.info("[%s] búsqueda: %d fragmentos indexados", sesion_id[:8], n)
    except Exception as e:  # noqa: BLE001
        log.exception("[%s] no pude indexar la clase", sesion_id[:8])
        ctx.log_sesion(sesion_id, f"publicar: aviso: no pude indexarla para la búsqueda "
                                  f"({e.__class__.__name__}: {e}); corré `classmer reindexar`")


def sesiones_indexables(conn: sqlite3.Connection) -> list[str]:
    """Las sesiones con apunte: las que pasaron por publicar (o lo van a hacer)."""
    return [f[0] for f in conn.execute(
        "SELECT DISTINCT s.id FROM sesiones s JOIN apuntes a ON a.sesion_id = s.id AND a.actual = 1 "
        "ORDER BY s.inicio, s.id")]


def reindexar(cfg: Config, base: Base) -> Reindexado:
    """Rearma el índice entero desde la base y los archivos de cada sesión."""
    with base.conexion() as conn:
        asegurar_tabla(conn)
        ids = sesiones_indexables(conn)
        filas = [f for sid in ids for f in _filas_sesion(cfg, conn, sid)]
        with transaccion(conn):
            conn.execute("DELETE FROM busqueda")
            conn.executemany(_INSERTAR, filas)
    return Reindexado(sesiones=len(ids), filas=len(filas))


def indice_vacio(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM busqueda LIMIT 1").fetchone() is None


# --- consultas -----------------------------------------------------------------------------------

PALABRAS_VACIAS = frozenset(normalizar(p) for p in """
a al algo algun alguna algunas alguno algunos ante antes aqui asi aun cada como con contra cual
cuales cuando de del desde donde dos e el ella ellas ello ellos en entre era eran es esa esas ese
eso esos esta estaba estan estas este esto estos fue fueron ha habia han hasta hay la las le les
lo los mas me mi mis mucho muy nada ni no nos o otra otras otro otros para pero poco por porque
que quien quienes se ser si sin sobre son su sus tambien tan tanto te tiene tienen todo todos tu
tus un una unas uno unos y ya yo dijo dice dijeron decia hablo hablamos explico explicaron vimos
vio
""".split())

_PALABRA = re.compile(r"\w+", re.UNICODE)
_FRASE = re.compile(r'"([^"]+)"')


def _termino(palabra: str) -> str:
    """Una palabra para FTS5: entre comillas y, si es larga, como prefijo sin el plural."""
    p = normalizar(palabra)
    if len(p) >= 6 and p.endswith("es"):
        p = p[:-2]
    elif len(p) >= 5 and p.endswith("s"):
        p = p[:-1]
    return f'"{p}"*' if len(p) >= 4 else f'"{p}"'


def consultas(texto: str) -> list[tuple[str, str]]:
    """[(modo, consulta FTS5)]: primero todas las palabras, después alguna (si hay más de una).

    "entre comillas" busca la frase exacta. Se ignoran las palabras vacías ("qué dijo sobre…"),
    salvo que la consulta sea sólo eso.
    """
    frases = [" ".join(_PALABRA.findall(normalizar(f))) for f in _FRASE.findall(texto)]
    frases = [f'"{f}"' for f in frases if f]
    palabras = _PALABRA.findall(_FRASE.sub(" ", texto))
    utiles = [p for p in palabras if normalizar(p) not in PALABRAS_VACIAS] or palabras
    terminos = list(dict.fromkeys(frases + [_termino(p) for p in utiles]))
    if not terminos:
        return []
    salida = [("todas", "texto : (" + " AND ".join(terminos) + ")")]
    if len(terminos) > 1:
        salida.append(("alguna", "texto : (" + " OR ".join(terminos) + ")"))
    return salida


@dataclass
class Coincidencia:
    sesion_id: str
    materia: str
    tipo: str
    inicio_ms: int | None
    fragmento: str
    puntaje: float


def buscar(conn: sqlite3.Connection, texto: str, *, materia: str | None = None,
           rangos: Mapping[str, list[tuple[int, int]]] | None = None,
           limite: int = 10) -> tuple[list[Coincidencia], str | None]:
    """Coincidencias ordenadas por relevancia y el modo que encontró algo ('todas' o 'alguna').

    `rangos` (sesión → [(inicio_ms, fin_ms)]) restringe a esos tramos de esas clases (filtro por tema).
    """
    sql = ("SELECT ref, materia, tipo, inicio_ms, snippet(busqueda, 0, '«', '»', '…', 24) AS fragmento, "
           "bm25(busqueda) AS puntaje FROM busqueda WHERE busqueda MATCH ?")
    params: list[Any] = []
    if materia is not None:
        sql += " AND materia = ?"
        params.append(materia)
    sql += " ORDER BY puntaje LIMIT ?"
    tope = 2000 if rangos is not None else limite
    for modo, consulta in consultas(texto):
        filas = conn.execute(sql, [consulta, *params, tope]).fetchall()
        encontradas = [
            Coincidencia(f["ref"], f["materia"], f["tipo"],
                         int(f["inicio_ms"]) if f["inicio_ms"] is not None else None,
                         f["fragmento"], float(f["puntaje"]))
            for f in filas if rangos is None or _en_rangos(rangos, f["ref"], f["inicio_ms"])
        ][:limite]
        if encontradas:
            return encontradas, modo
    return [], None


def _en_rangos(rangos: Mapping[str, list[tuple[int, int]]], sesion_id: str, inicio_ms: Any) -> bool:
    tramos = rangos.get(sesion_id)
    if not tramos:
        return False
    if inicio_ms is None:
        return False
    ms = int(inicio_ms)
    return any(ini <= ms < fin for ini, fin in tramos)


def iterar_secciones(cfg: Config, conn: sqlite3.Connection,
                     materia: str | None = None) -> Iterator[tuple[sqlite3.Row, dict[str, Any]]]:
    """(sesión, sección de meta.json) de las clases con apunte, en orden de fecha y minuto."""
    sql = ("SELECT s.* FROM sesiones s WHERE EXISTS (SELECT 1 FROM apuntes a WHERE a.sesion_id = s.id "
           "AND a.actual = 1)")
    params: list[Any] = []
    if materia is not None:
        sql += " AND s.materia_id = ?"
        params.append(materia)
    for sesion in conn.execute(sql + " ORDER BY s.inicio, s.id", params).fetchall():
        meta = leer_meta(cfg, sesion["id"], apunte_actual(conn, sesion["id"]))
        for s in sorted(secciones_meta(meta), key=lambda s: s.get("inicio_ms") or 0):
            yield sesion, s
