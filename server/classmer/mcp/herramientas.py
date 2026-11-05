"""Herramientas del MCP (§11): consultas de lectura sobre la base y los archivos de cada clase.

Cada herramienta recibe un `Entorno` (config + base) y sus argumentos ya validados contra su
`inputSchema`, y devuelve una lista de bloques: un dict (va como JSON) o un texto (va tal cual,
p. ej. el LaTeX del apunte). Un error que el modelo puede corregir es `ErrorHerramienta`.

Para citar, todo lo que sale de una clase trae `sesion`, `materia`, `numero`, `fecha`, `titulo`,
el minuto (`inicio_ms` y `minuto` hh:mm:ss) y una `cita` lista para usar. Los resultados de una
clase traen las rutas absolutas del PDF, el .tex y el transcript publicados (o, si la clase no se
publicó en la carpeta de la materia, las de Classmer-data) y las de sus adjuntos (slides o notas del
profe, imágenes) para que un cliente en la misma PC los adjunte.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from classmer import adjuntos, busqueda, revision
from classmer.busqueda import normalizar
from classmer.pipeline.linea_de_tiempo import fecha_local
from classmer.sesiones import AUDIO_CELU, AUDIO_PANEL
from classmer.util import ms_a_hhmmss, tiempo_a_ms

if TYPE_CHECKING:
    from classmer.config import Config
    from classmer.db import Base

MAX_TRANSCRIPT_CHARS = 40_000
LIMITE_BUSCAR = 10
MAX_BUSCAR = 30
ESTADOS_ITEM = ("abierto", "hecho", "descartado")
TIPOS_ITEM = ("parcial", "tarea", "fecha", "duda", "aviso")
EN_CURSO = ("grabando", "en_cola", "procesando")


class ErrorHerramienta(Exception):
    """Error de uso (materia o clase inexistente, minuto inválido…): vuelve como isError."""


@dataclass
class Entorno:
    cfg: Config
    base: Base


Bloque = dict[str, Any] | str


@dataclass
class Herramienta:
    nombre: str
    titulo: str
    descripcion: str
    esquema: dict[str, Any]
    funcion: Callable[..., list[Bloque]]
    solo_lectura: bool = True
    anotaciones: dict[str, Any] = field(default_factory=dict)

    def definicion(self) -> dict[str, Any]:
        anotaciones = {"title": self.titulo, "readOnlyHint": self.solo_lectura,
                       "openWorldHint": False, **self.anotaciones}
        return {"name": self.nombre, "title": self.titulo, "description": self.descripcion,
                "inputSchema": self.esquema, "annotations": anotaciones}


# --- resolver materias y clases --------------------------------------------------------------------

def _materias(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM materias ORDER BY activa DESC, nombre").fetchall()


def resolver_materia(conn: sqlite3.Connection, valor: str) -> sqlite3.Row:
    """Por id o por nombre (sin importar tildes ni mayúsculas; vale un pedazo si es único)."""
    filas = _materias(conn)
    buscado = normalizar(str(valor))
    if not buscado:
        raise ErrorHerramienta("falta la materia")
    for f in filas:
        if f["id"] == valor or normalizar(f["id"]) == buscado or normalizar(f["nombre"]) == buscado:
            return f
    candidatas = [f for f in filas if buscado in normalizar(f["id"]) or buscado in normalizar(f["nombre"])]
    if len(candidatas) == 1:
        return candidatas[0]
    opciones = ", ".join(f"{f['id']} ({f['nombre']})" for f in (candidatas or filas))
    if candidatas:
        raise ErrorHerramienta(f"'{valor}' coincide con varias materias: {opciones}")
    raise ErrorHerramienta(f"no existe la materia '{valor}'. Materias: {opciones}")


# audio_ruta y audio_celu_ruta: los assets de sesiones.id_audio (el nombre del archivo en la carpeta).
_SQL_CLASE = """
SELECT s.*, m.nombre AS materia_nombre,
       a.id AS apunte_id, a.version AS apunte_version, a.compilo, a.ruta_tex, a.ruta_pdf,
       a.resumen, a.titulo AS apunte_titulo, a.motor, a.modelo, a.prompt_version,
       (SELECT ruta FROM assets WHERE id = 'audio:' || s.id) AS audio_ruta,
       (SELECT ruta FROM assets WHERE id = 'audio-celu:' || s.id) AS audio_celu_ruta
FROM sesiones s
LEFT JOIN materias m ON m.id = s.materia_id
LEFT JOIN apuntes a ON a.id = (SELECT id FROM apuntes WHERE sesion_id = s.id AND actual = 1
                               ORDER BY version DESC LIMIT 1)
"""


def resolver_clase(conn: sqlite3.Connection, valor: str) -> sqlite3.Row:
    """Por id de sesión completo o por un prefijo único (al menos 4 caracteres)."""
    sid = str(valor).strip()
    if not sid:
        raise ErrorHerramienta("falta la sesión (el id que devuelven listar_clases o buscar)")
    fila = conn.execute(_SQL_CLASE + " WHERE s.id = ?", (sid,)).fetchone()
    if fila is not None:
        return fila
    if len(sid) >= 4:
        filas = conn.execute(_SQL_CLASE + " WHERE s.id LIKE ? ESCAPE '\\' LIMIT 5",
                             (sid.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall()
        if len(filas) == 1:
            return filas[0]
        if len(filas) > 1:
            raise ErrorHerramienta(f"'{sid}' coincide con varias sesiones: " + ", ".join(f["id"] for f in filas))
    raise ErrorHerramienta(f"no existe la sesión '{sid}' (usá el id que devuelven listar_clases o buscar)")


def _clases_por_id(conn: sqlite3.Connection, ids: set[str]) -> dict[str, sqlite3.Row]:
    if not ids:
        return {}
    marcas = ",".join("?" * len(ids))
    return {f["id"]: f for f in conn.execute(_SQL_CLASE + f" WHERE s.id IN ({marcas})", list(ids))}


# --- forma de una clase y citas --------------------------------------------------------------------

def _nombre_materia(fila: Mapping[str, Any]) -> str:
    return fila["materia_nombre"] or fila["materia_id"] or "sin materia"


def cita(cfg: Config, fila: Mapping[str, Any], inicio_ms: int | None = None) -> str:
    """'Análisis Matemático II, clase 7 (2026-10-15), min 00:34:10'."""
    numero = f"clase {fila['numero_clase']}" if fila["numero_clase"] is not None else "clase sin número"
    texto = f"{_nombre_materia(fila)}, {numero} ({fecha_local(cfg, fila['inicio'])})"
    return texto + (f", min {ms_a_hhmmss(inicio_ms)}" if inicio_ms is not None else "")


def ref_clase(cfg: Config, fila: Mapping[str, Any]) -> dict[str, Any]:
    """Lo mínimo para identificar y citar una clase."""
    return {
        "sesion": fila["id"],
        "materia": fila["materia_id"],
        "materia_nombre": _nombre_materia(fila),
        "numero": fila["numero_clase"],
        "fecha": fecha_local(cfg, fila["inicio"]),
        "tipo_clase": fila["tipo_clase"],
        "titulo": fila["titulo"] or fila["apunte_titulo"],
    }


def _existe(ruta: Path) -> str | None:
    return str(ruta) if ruta.is_file() else None


def adjuntos_de(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Los adjuntos de varias clases con una sola consulta: sesión → adjuntos, en orden de llegada."""
    ids = sorted(set(ids))
    if not ids:
        return {}
    marcas = ",".join("?" * len(ids))
    por_sesion: dict[str, list[dict[str, Any]]] = {}
    for f in conn.execute(f"SELECT * FROM assets WHERE tipo = ? AND sesion_id IN ({marcas}) ORDER BY creado, rowid",
                          [adjuntos.TIPO_ASSET, *ids]):
        por_sesion.setdefault(f["sesion_id"], []).append(adjuntos.desde_fila(f))
    return por_sesion


def _rutas_adjuntos(cfg: Config, sesion_id: str, publicada: Path | None,
                    lista: list[dict[str, Any]]) -> list[str]:
    """La copia de cada adjunto en la carpeta publicada (adjuntos/<nombre para LaTeX>) si está; si no
    (no se publicó afuera, se publicó antes de que llegara, o un HEIC que no se pudo convertir), el
    original en Classmer-data. Uno que no está en ningún lado no va."""
    sesion = cfg.sesion_dir(sesion_id)
    rutas = []
    for a in lista:
        ruta = _existe(publicada / adjuntos.CARPETA / adjuntos.nombre_latex(a)) if publicada is not None else None
        ruta = ruta or (_existe(sesion / a["ruta"]) if a["ruta"] else None)
        if ruta is not None:
            rutas.append(ruta)
    return rutas


ARCHIVOS_SUELTOS = ("pdf", "tex", "transcript", "audio", "audio_panel", "audio_celu")


def _audios(cfg: Config, fila: Mapping[str, Any]) -> dict[str, str | None]:
    """Los audios no se publican afuera: siempre están en Classmer-data. El original (con el nombre de
    su asset), la copia en Opus que arma verificar si el navegador no reproduce el original, y la
    pista del celu de Ambos (§15)."""
    sesion = cfg.sesion_dir(fila["id"])
    claves = fila.keys()
    original = fila["audio_ruta"] if "audio_ruta" in claves else None
    celu = fila["audio_celu_ruta"] if "audio_celu_ruta" in claves else None
    return {"audio": _existe(sesion / (original or "audio.ogg")),
            "audio_panel": _existe(sesion / AUDIO_PANEL),
            "audio_celu": _existe(sesion / (celu or AUDIO_CELU))}


def _tamano(ruta: str | None) -> int | None:
    if ruta is None:
        return None
    try:
        return Path(ruta).stat().st_size
    except OSError:
        return None


def _con_tamanos(salida: dict[str, Any]) -> dict[str, Any]:
    """Suma `bytes`: el tamaño de cada archivo (None si no está) y el de cada adjunto, en el mismo
    orden. Sirve para decidir contra los topes de WhatsApp o Telegram sin abrir nada (para un bot de mensajería)."""
    salida["bytes"] = {**{k: _tamano(salida[k]) for k in ARCHIVOS_SUELTOS},
                       "adjuntos": [_tamano(r) for r in salida["adjuntos"]]}
    return salida


def archivos(cfg: Config, fila: Mapping[str, Any],
             lista_adjuntos: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Rutas absolutas de lo publicado; si no se publicó afuera, las copias de Classmer-data. Los
    `adjuntos` salen de `lista_adjuntos` (los de esa clase, de adjuntos_de o adjuntos.listar). Los
    audios y `bytes`, como en `_audios` y `_con_tamanos`."""
    publicada = Path(fila["ruta_publicada"]) if fila["ruta_publicada"] else None
    if publicada is not None and publicada.is_dir():
        return _con_tamanos({
            "publicado": True, "carpeta": str(publicada), "pdf": _existe(publicada / "apunte.pdf"),
            "tex": _existe(publicada / "apunte.tex"), "transcript": _existe(publicada / "transcript.md"),
            "adjuntos": _rutas_adjuntos(cfg, fila["id"], publicada, lista_adjuntos or []),
            **_audios(cfg, fila)})
    sesion = cfg.sesion_dir(fila["id"])
    salida: dict[str, Any] = {"publicado": False, "carpeta": None, "pdf": None, "tex": None,
                              "transcript": _existe(sesion / "transcript.md"),
                              "adjuntos": _rutas_adjuntos(cfg, fila["id"], None, lista_adjuntos or []),
                              **_audios(cfg, fila)}
    if fila["apunte_id"] is not None:
        apunte = busqueda.carpeta_apunte(cfg, fila["id"], {"ruta_tex": fila["ruta_tex"],
                                                            "version": fila["apunte_version"]})
        salida.update(pdf=_existe(apunte / "apunte.pdf"), tex=_existe(apunte / "apunte.tex"))
    return _con_tamanos(salida)


def _minuto(inicio_ms: int | None) -> str | None:
    return ms_a_hhmmss(inicio_ms) if inicio_ms is not None else None


def _seccion_en(secciones: list[dict[str, Any]], inicio_ms: int | None) -> dict[str, Any] | None:
    if inicio_ms is None:
        return None
    for s in secciones:
        ini, fin = s.get("inicio_ms"), s.get("fin_ms")
        if ini is not None and fin is not None and int(ini) <= inicio_ms < int(fin):
            return s
    return None


def _temas_de(meta: Mapping[str, Any]) -> list[str]:
    vistos: dict[str, str] = {}
    for s in busqueda.secciones_meta(meta):
        for t in s.get("temas") or []:
            texto = " ".join(str(t).split())
            if texto:
                vistos.setdefault(normalizar(texto), texto)
    return list(vistos.values())


# --- argumentos ------------------------------------------------------------------------------------

def _ms_de_minuto(valor: Any, nombre: str) -> int | None:
    """Minuto de la clase: número de minutos (34, 34.5) o 'hh:mm:ss' / 'mm:ss'."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    if isinstance(valor, bool):
        raise ErrorHerramienta(f"{nombre} inválido: {valor!r}")
    if isinstance(valor, (int, float)):
        if valor < 0:
            raise ErrorHerramienta(f"{nombre} no puede ser negativo")
        return int(round(float(valor) * 60_000))
    texto = str(valor).strip().lower().removesuffix("min").strip()
    try:
        if ":" in texto:
            return tiempo_a_ms(texto)
        return _ms_de_minuto(float(texto.replace(",", ".")), nombre)
    except ValueError as e:
        raise ErrorHerramienta(f"{nombre} inválido: {valor!r} (usá minutos, p. ej. 34, o 'hh:mm:ss')") from e


_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _fecha(valor: Any, nombre: str) -> str | None:
    if valor is None or not str(valor).strip():
        return None
    texto = str(valor).strip()[:10]
    if not _FECHA.match(texto):
        raise ErrorHerramienta(f"{nombre} inválido: {valor!r} (usá AAAA-MM-DD)")
    return texto


# --- herramientas ----------------------------------------------------------------------------------

def listar_materias(e: Entorno) -> list[Bloque]:
    with e.base.conexion() as conn:
        materias = _materias(conn)
        conteos: dict[str, dict[str, int]] = {}
        for f in conn.execute("SELECT materia_id, estado, COUNT(*) AS n FROM sesiones GROUP BY materia_id, estado"):
            conteos.setdefault(f["materia_id"], {})[f["estado"]] = f["n"]
        pendientes = {f["materia_id"]: f["n"] for f in conn.execute(
            "SELECT materia_id, COUNT(*) AS n FROM items WHERE estado = 'abierto' GROUP BY materia_id")}
        ultimas = {}
        for m in materias:
            u = conn.execute(_SQL_CLASE + " WHERE s.materia_id = ? ORDER BY s.inicio DESC, s.id DESC LIMIT 1",
                             (m["id"],)).fetchone()
            if u is not None:
                ultimas[m["id"]] = {**ref_clase(e.cfg, u), "estado": u["estado"]}
    salida = []
    for m in materias:
        c = conteos.get(m["id"], {})
        carpeta = Path(m["carpeta"]) if m["carpeta"] else None
        salida.append({
            "id": m["id"],
            "nombre": m["nombre"],
            "activa": bool(m["activa"]),
            "carpeta": str(carpeta) if carpeta else None,
            "carpeta_clases": str(carpeta / m["carpeta_clases"]) if carpeta and m["carpeta_clases"] else None,
            "clases": {
                "total": sum(c.values()),
                "listas": c.get("lista", 0),
                "en_curso": sum(c.get(k, 0) for k in EN_CURSO),
                "esperando_usuario": c.get("esperando_usuario", 0),
                "con_error": c.get("error", 0),
            },
            "pendientes_abiertos": pendientes.get(m["id"], 0),
            "ultima_clase": ultimas.get(m["id"]),
        })
    sin_materia = conteos.get(None, {})
    respuesta: dict[str, Any] = {"materias": salida}
    if sin_materia:
        respuesta["clases_sin_materia"] = sum(sin_materia.values())
    return [respuesta]


def listar_clases(e: Entorno, materia: str | None = None, desde: str | None = None,
                  hasta: str | None = None, limite: int = 50) -> list[Bloque]:
    fecha_desde, fecha_hasta = _fecha(desde, "desde"), _fecha(hasta, "hasta")
    with e.base.conexion() as conn:
        m = resolver_materia(conn, materia) if materia else None
        sql, params = _SQL_CLASE, []
        if m is not None:
            sql += " WHERE s.materia_id = ?"
            params.append(m["id"])
        filas = conn.execute(sql + " ORDER BY s.inicio DESC, s.id DESC", params).fetchall()
        filas = [f for f in filas
                 if (fecha_desde is None or fecha_local(e.cfg, f["inicio"]) >= fecha_desde)
                 and (fecha_hasta is None or fecha_local(e.cfg, f["inicio"]) <= fecha_hasta)]
        total = len(filas)
        filas = list(reversed(filas[:limite]))  # las más recientes, en orden cronológico
        pendientes = {f["sesion_id"]: f["n"] for f in conn.execute(
            "SELECT sesion_id, COUNT(*) AS n FROM items WHERE estado = 'abierto' GROUP BY sesion_id")}
        de_cada_clase = adjuntos_de(conn, (f["id"] for f in filas))
        clases = []
        for f in filas:
            meta = busqueda.leer_meta(e.cfg, f["id"], {"ruta_tex": f["ruta_tex"], "version": f["apunte_version"]}
                                      if f["apunte_id"] is not None else None)
            clase = {
                **ref_clase(e.cfg, f),
                "estado": f["estado"],
                "inicio": f["inicio"],
                "duracion": _minuto(f["duracion_ms"]),
                "duracion_ms": f["duracion_ms"],
                "resumen": f["resumen"],
                "temas": _temas_de(meta),
                "pendientes_abiertos": pendientes.get(f["id"], 0),
                "archivos": archivos(e.cfg, f, de_cada_clase.get(f["id"])),
                "cita": cita(e.cfg, f),
            }
            if f["estado"] != "lista":
                clase["etapa"] = f["etapa"]
                clase["error"] = f["error"]
            clases.append(clase)
    respuesta: dict[str, Any] = {"materia": m["id"] if m is not None else None, "total": total, "clases": clases}
    if total > len(clases):
        respuesta["aviso"] = f"se muestran las {len(clases)} más recientes; acotá con desde/hasta o subí el límite"
    return [respuesta]


def leer_apunte(e: Entorno, sesion: str) -> list[Bloque]:
    with e.base.conexion() as conn:
        f = resolver_clase(conn, sesion)
        lista_adjuntos = adjuntos.listar(conn, f["id"])
    if f["apunte_id"] is None:
        detalle = f"estado: {f['estado']}" + (f", etapa: {f['etapa']}" if f["etapa"] else "")
        raise ErrorHerramienta(f"la clase {f['id']} todavía no tiene apunte ({detalle})")
    apunte = {"ruta_tex": f["ruta_tex"], "version": f["apunte_version"]}
    carpeta = busqueda.carpeta_apunte(e.cfg, f["id"], apunte)
    try:
        latex = (carpeta / "apunte.tex").read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        raise ErrorHerramienta(f"no pude leer el .tex del apunte ({err.strerror or err})") from err
    meta = busqueda.leer_meta(e.cfg, f["id"], apunte)
    secciones = [
        {**s, "minuto": _minuto(s.get("inicio_ms")), "cita": cita(e.cfg, f, s.get("inicio_ms"))}
        for s in busqueda.secciones_meta(meta)
    ]
    items = [
        {**it, "minuto": _minuto(it.get("offset_ms")), "cita": cita(e.cfg, f, it.get("offset_ms"))}
        for it in (meta.get("items") or []) if isinstance(it, dict)
    ]
    datos = {
        **ref_clase(e.cfg, f),
        "estado": f["estado"],
        "cita": cita(e.cfg, f),
        "apunte": {"version": f["apunte_version"], "compilo": bool(f["compilo"]), "motor": f["motor"],
                   "modelo": f["modelo"], "prompt_version": f["prompt_version"]},
        "meta": {"titulo": meta.get("titulo") or f["apunte_titulo"], "resumen": meta.get("resumen") or f["resumen"],
                 "secciones": secciones, "items": items},
        "archivos": archivos(e.cfg, f, lista_adjuntos),
        "latex": "en el bloque siguiente (texto del apunte.tex)",
    }
    return [datos, latex]


def hablante_visible(hablante: str | None) -> str:
    """Los roles del ASR en mayúsculas (PROFE, ALUMNO, DESCONOCIDO, como siempre); un nombre (el del
    transcript revisado, §15: "Laura Inés Gómez", "Alumno"), tal cual."""
    texto = " ".join(str(hablante or "").split())
    if not texto or texto.lower() in ("profe", "alumno", "desconocido"):
        return (texto or "DESCONOCIDO").upper()
    return texto


def transcript(e: Entorno, sesion: str, desde: Any = None, hasta: Any = None) -> list[Bloque]:
    ms_desde, ms_hasta = _ms_de_minuto(desde, "desde"), _ms_de_minuto(hasta, "hasta")
    if ms_desde is not None and ms_hasta is not None and ms_hasta <= ms_desde:
        raise ErrorHerramienta("hasta tiene que ser mayor que desde")
    with e.base.conexion() as conn:
        f = resolver_clase(conn, sesion)
        segmentos = busqueda.segmentos_sesion(e.cfg, conn, f["id"])
    if not segmentos:
        raise ErrorHerramienta(f"la clase {f['id']} todavía no tiene transcript (estado: {f['estado']})")
    elegidos = [s for s in segmentos
                if (ms_desde is None or s["fin_ms"] > ms_desde) and (ms_hasta is None or s["inicio_ms"] < ms_hasta)]
    rev = revision.cargar(e.cfg, f["id"])
    revisado = bool(rev and rev.get("estado") in ("aplicada", "esperando"))
    lineas: list[str] = []
    largo = 0
    siguiente: int | None = None
    for s in elegidos:
        hablante = hablante_visible(s.get("hablante"))
        linea = f"[{ms_a_hhmmss(s['inicio_ms'])}] {hablante}: {' '.join(str(s['texto']).split())}"
        if lineas and largo + len(linea) > MAX_TRANSCRIPT_CHARS:
            siguiente = s["inicio_ms"]
            break
        lineas.append(linea)
        largo += len(linea) + 1
    mostrados = elegidos[:len(lineas)]
    datos: dict[str, Any] = {
        **ref_clase(e.cfg, f),
        "desde_ms": mostrados[0]["inicio_ms"] if mostrados else ms_desde,
        "hasta_ms": mostrados[-1]["fin_ms"] if mostrados else ms_hasta,
        "desde": _minuto(mostrados[0]["inicio_ms"]) if mostrados else _minuto(ms_desde),
        "hasta": _minuto(mostrados[-1]["fin_ms"]) if mostrados else _minuto(ms_hasta),
        "duracion": _minuto(f["duracion_ms"]),
        "segmentos": len(mostrados),
        "cita": cita(e.cfg, f, mostrados[0]["inicio_ms"] if mostrados else ms_desde),
        "archivos": {"transcript": archivos(e.cfg, f)["transcript"]},
        "formato": "una línea por segmento: [hh:mm:ss] hablante: texto (minuto desde el inicio de la grabación). "
                   "Revisado: el profe por su nombre y Alumno; sin revisar: PROFE, ALUMNO o DESCONOCIDO",
        "revisado": revisado,
    }
    if siguiente is not None:
        datos["truncado"] = True
        datos["seguir_desde"] = ms_a_hhmmss(siguiente)
    if not lineas:
        return [datos, "(no hay habla transcripta en ese tramo)"]
    return [datos, "\n".join(lineas)]


def _rangos_tema(e: Entorno, conn: sqlite3.Connection, tema: str,
                 materia: str | None) -> dict[str, list[tuple[int, int]]]:
    """Tramos (sesión → [(inicio, fin)]) de las secciones cuyo título o temas mencionan `tema`."""
    clave = normalizar(tema)
    rangos: dict[str, list[tuple[int, int]]] = {}
    for sesion, s in busqueda.iterar_secciones(e.cfg, conn, materia):
        textos = [str(s.get("titulo") or "")] + [str(t) for t in s.get("temas") or []]
        if any(clave in normalizar(t) for t in textos) and s.get("inicio_ms") is not None:
            fin = s.get("fin_ms") if s.get("fin_ms") is not None else s["inicio_ms"] + 1
            rangos.setdefault(sesion["id"], []).append((int(s["inicio_ms"]), int(fin)))
    return rangos


def buscar(e: Entorno, texto: str, materia: str | None = None, tema: str | None = None,
           limite: int = LIMITE_BUSCAR) -> list[Bloque]:
    if not str(texto).strip():
        raise ErrorHerramienta("falta el texto a buscar")
    limite = max(1, min(int(limite), MAX_BUSCAR))
    with e.base.conexion() as conn:
        m = resolver_materia(conn, materia) if materia else None
        if busqueda.indice_vacio(conn) and busqueda.sesiones_indexables(conn):
            busqueda.reindexar(e.cfg, e.base)  # base de antes del índice: se arma sola la primera vez
        rangos = _rangos_tema(e, conn, tema, m["id"] if m else None) if tema and str(tema).strip() else None
        if rangos is not None and not rangos:
            return [{"texto": texto, "tema": tema, "resultados": [],
                     "aviso": f"ninguna sección de los apuntes tiene el tema '{tema}' (mirá la herramienta temas)"}]
        coincidencias, modo = busqueda.buscar(conn, texto, materia=m["id"] if m else None,
                                              rangos=rangos, limite=limite)
        clases = _clases_por_id(conn, {c.sesion_id for c in coincidencias})
        de_cada_clase = adjuntos_de(conn, clases.keys())
    metas: dict[str, list[dict[str, Any]]] = {}
    resultados = []
    for c in coincidencias:
        f = clases.get(c.sesion_id)
        if f is None:
            continue  # sesión borrada que quedó en el índice
        if c.sesion_id not in metas:
            apunte = ({"ruta_tex": f["ruta_tex"], "version": f["apunte_version"]}
                      if f["apunte_id"] is not None else None)
            metas[c.sesion_id] = busqueda.secciones_meta(busqueda.leer_meta(e.cfg, c.sesion_id, apunte))
        seccion = _seccion_en(metas[c.sesion_id], c.inicio_ms)
        resultados.append({
            "cita": cita(e.cfg, f, c.inicio_ms),
            "origen": "apunte" if c.tipo == busqueda.TIPO_APUNTE else "transcript",
            "fragmento": c.fragmento,
            **ref_clase(e.cfg, f),
            "inicio_ms": c.inicio_ms,
            "minuto": _minuto(c.inicio_ms),
            "seccion": seccion.get("titulo") if seccion else None,
            "archivos": archivos(e.cfg, f, de_cada_clase.get(c.sesion_id)),
        })
    respuesta: dict[str, Any] = {"texto": texto, "materia": m["id"] if m else None, "tema": tema,
                                 "resultados": resultados}
    if modo == "alguna":
        respuesta["aviso"] = "ningún fragmento tiene todas las palabras: son coincidencias parciales"
    if not resultados:
        respuesta["aviso"] = "sin resultados; probá con otras palabras o sin filtros"
    return [respuesta]


def temas(e: Entorno, materia: str) -> list[Bloque]:
    with e.base.conexion() as conn:
        m = resolver_materia(conn, materia)
        cobertura: dict[str, dict[str, Any]] = {}
        clases: dict[str, sqlite3.Row] = {}
        for sesion, s in busqueda.iterar_secciones(e.cfg, conn, m["id"]):
            if sesion["id"] not in clases:
                clases[sesion["id"]] = conn.execute(_SQL_CLASE + " WHERE s.id = ?", (sesion["id"],)).fetchone()
            fila = clases[sesion["id"]]
            for t in s.get("temas") or []:
                nombre = " ".join(str(t).split())
                if not nombre:
                    continue
                entrada = cobertura.setdefault(normalizar(nombre), {"tema": nombre, "clases": []})
                entrada["clases"].append({
                    **ref_clase(e.cfg, fila),
                    "seccion": s.get("titulo"),
                    "inicio_ms": s.get("inicio_ms"),
                    "minuto": _minuto(s.get("inicio_ms")),
                    "cita": cita(e.cfg, fila, s.get("inicio_ms")),
                })
        programa = conn.execute(
            "SELECT id, unidad, nombre, descripcion, sinonimos, estado FROM temas WHERE materia_id = ? "
            "ORDER BY unidad, nombre", (m["id"],)).fetchall()
    if not programa:
        return [{"materia": m["id"], "materia_nombre": m["nombre"], "fuente": "meta.json",
                 "aviso": "todavía no hay programa (temas.yaml): son los temas que marcó cada apunte, "
                          "en orden de aparición",
                 "temas": list(cobertura.values())}]
    # Con programa: cada tema con las clases que lo tocaron (por nombre o sinónimo).
    usados: set[str] = set()
    salida = []
    for t in programa:
        nombres = {normalizar(t["nombre"])} | {normalizar(str(x)) for x in _lista_json(t["sinonimos"])}
        tocadas = []
        for clave, entrada in cobertura.items():
            if clave in nombres:
                tocadas += entrada["clases"]
                usados.add(clave)
        salida.append({"id": t["id"], "unidad": t["unidad"], "tema": t["nombre"], "descripcion": t["descripcion"],
                       "estado": t["estado"], "clases": tocadas})
    sin_programa = [v for k, v in cobertura.items() if k not in usados]
    return [{"materia": m["id"], "materia_nombre": m["nombre"], "fuente": "programa",
             "temas": salida, "fuera_del_programa": sin_programa}]


def _lista_json(valor: Any) -> list[Any]:
    if not valor:
        return []
    try:
        datos = json.loads(valor)
    except (TypeError, ValueError):
        return [valor]
    return datos if isinstance(datos, list) else [datos]


_SQL_ITEM = """
SELECT i.id AS item_id, i.tipo AS item_tipo, i.texto AS item_texto, i.estado AS item_estado,
       i.fecha_limite, i.offset_ms, i.materia_id AS item_materia, s.*, m.nombre AS materia_nombre,
       a.id AS apunte_id, a.titulo AS apunte_titulo
FROM items i
LEFT JOIN sesiones s ON s.id = i.sesion_id
LEFT JOIN materias m ON m.id = COALESCE(s.materia_id, i.materia_id)
LEFT JOIN apuntes a ON a.id = i.apunte_id
"""


def _item(cfg: Config, f: Mapping[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": f["item_id"],
        "tipo": f["item_tipo"],
        "texto": f["item_texto"],
        "estado": f["item_estado"],
        "fecha_limite": f["fecha_limite"],
        "inicio_ms": f["offset_ms"],
        "minuto": _minuto(f["offset_ms"]),
    }
    if f["id"] is not None:  # la sesión existe
        item.update(ref_clase(cfg, f))
        item["cita"] = cita(cfg, f, f["offset_ms"])
    else:
        item["materia"] = f["item_materia"]
    return item


def pendientes(e: Entorno, materia: str | None = None, tipo: str | None = None,
               estado: str = "abierto") -> list[Bloque]:
    with e.base.conexion() as conn:
        m = resolver_materia(conn, materia) if materia else None
        sql, params = _SQL_ITEM + " WHERE 1 = 1", []
        if m is not None:
            sql += " AND COALESCE(s.materia_id, i.materia_id) = ?"
            params.append(m["id"])
        if tipo:
            sql += " AND i.tipo = ?"
            params.append(tipo)
        if estado != "todos":
            sql += " AND i.estado = ?"
            params.append(estado)
        sql += " ORDER BY i.fecha_limite IS NULL, i.fecha_limite, s.inicio DESC, i.offset_ms, i.id"
        filas = conn.execute(sql, params).fetchall()
    items = [_item(e.cfg, f) for f in filas]
    por_tipo: dict[str, int] = {}
    for it in items:
        por_tipo[it["tipo"]] = por_tipo.get(it["tipo"], 0) + 1
    return [{"materia": m["id"] if m else None, "estado": estado, "total": len(items), "por_tipo": por_tipo,
             "items": items}]


def marcar_item(e: Entorno, id: Any, estado: str) -> list[Bloque]:
    try:
        item_id = int(str(id).strip())
    except ValueError as err:
        raise ErrorHerramienta(f"id de item inválido: {id!r} (usá el id que devuelve pendientes)") from err
    with e.base.transaccion() as conn:
        previo = conn.execute("SELECT estado FROM items WHERE id = ?", (item_id,)).fetchone()
        if previo is None:
            raise ErrorHerramienta(f"no existe el item {item_id} (usá el id que devuelve pendientes)")
        conn.execute("UPDATE items SET estado = ? WHERE id = ?", (estado, item_id))
        fila = conn.execute(_SQL_ITEM + " WHERE i.id = ?", (item_id,)).fetchone()
    return [{"item": _item(e.cfg, fila), "estado_anterior": previo["estado"]}]


# --- catálogo --------------------------------------------------------------------------------------

_MATERIA = {"type": "string", "description": "Id de la materia (p. ej. 'analisis') o su nombre; "
                                             "sin tildes ni mayúsculas también sirve."}
_SESION = {"type": "string", "description": "Id de la sesión (clase), tal como lo devuelven listar_clases, "
                                            "buscar o pendientes. Alcanza con un prefijo único."}
_MINUTO = {"type": ["number", "string"],
           "description": "Minuto de la grabación: número de minutos (34 o 34.5) o 'hh:mm:ss' / 'mm:ss'."}

HERRAMIENTAS: dict[str, Herramienta] = {h.nombre: h for h in [
    Herramienta(
        "listar_materias", "Materias",
        "Materias de Classmer con su estado: si está activa, cuántas clases hay (listas, en curso, con "
        "error), pendientes abiertos, la última clase y la carpeta donde se publican los apuntes.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        listar_materias,
    ),
    Herramienta(
        "listar_clases", "Clases",
        "Clases grabadas (de una materia o de todas), en orden cronológico: número, fecha, título, "
        "resumen, temas, estado y rutas absolutas del PDF, el .tex y el transcript publicados, de los "
        "adjuntos de la clase (slides o notas del profe, imágenes) y de sus audios, con el tamaño de "
        "cada archivo en `archivos.bytes`.",
        {"type": "object", "properties": {
            "materia": _MATERIA,
            "desde": {"type": "string", "description": "Fecha mínima AAAA-MM-DD (inclusive)."},
            "hasta": {"type": "string", "description": "Fecha máxima AAAA-MM-DD (inclusive)."},
            "limite": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50,
                       "description": "Cuántas clases como máximo (las más recientes)."},
        }, "additionalProperties": False},
        listar_clases,
    ),
    Herramienta(
        "leer_apunte", "Leer apunte",
        "El apunte completo de una clase: metadatos (meta.json: título, resumen, secciones con su minuto "
        "y temas, items) y, en un segundo bloque, el texto LaTeX del apunte.tex. Trae las rutas "
        "absolutas del PDF, el .tex, el transcript y los adjuntos de la clase (PDF de slides o notas "
        "del profe, imágenes).",
        {"type": "object", "properties": {"sesion": _SESION}, "required": ["sesion"],
         "additionalProperties": False},
        leer_apunte,
    ),
    Herramienta(
        "transcript", "Transcript",
        "Lo que se dijo en una clase entre dos minutos, una línea por frase con su minuto y quién habla. "
        "Si la clase pasó por la revisión (revisado: true), está limpio y fiel: sin muletillas, con el profe "
        "por su nombre, lo agregado entre corchetes y sin corregir los errores del profe. Sin desde/hasta, "
        "la clase entera (si es muy larga, se corta y avisa desde dónde seguir).",
        {"type": "object", "properties": {"sesion": _SESION, "desde": _MINUTO, "hasta": _MINUTO},
         "required": ["sesion"], "additionalProperties": False},
        transcript,
    ),
    Herramienta(
        "buscar", "Buscar",
        "Busca en los apuntes y en los transcripts de todas las clases (sin importar tildes ni "
        "mayúsculas). Cada resultado trae la clase, el minuto, la sección y una cita lista para usar. "
        "Primero exige todas las palabras; si no hay nada, devuelve coincidencias parciales. "
        "Usá \"comillas\" para una frase exacta.",
        {"type": "object", "properties": {
            "texto": {"type": "string", "description": "Qué buscar (palabras o \"frase exacta\")."},
            "materia": _MATERIA,
            "tema": {"type": "string", "description": "Sólo dentro de las secciones de los apuntes que "
                                                      "tocan este tema (ver la herramienta temas)."},
            "limite": {"type": "integer", "minimum": 1, "maximum": MAX_BUSCAR, "default": LIMITE_BUSCAR},
        }, "required": ["texto"], "additionalProperties": False},
        buscar,
    ),
    Herramienta(
        "temas", "Temas",
        "Temas de una materia con su cobertura: qué clases tocaron cada tema, en qué sección y minuto. "
        "Mientras no haya programa (temas.yaml), son los temas que marcó cada apunte.",
        {"type": "object", "properties": {"materia": _MATERIA}, "required": ["materia"],
         "additionalProperties": False},
        temas,
    ),
    Herramienta(
        "pendientes", "Pendientes",
        "Items detectados en clase (parcial, tarea, fecha, duda, aviso) con su clase y minuto. Por "
        "defecto, sólo los abiertos. Cada item trae el id que usa marcar_item.",
        {"type": "object", "properties": {
            "materia": _MATERIA,
            "tipo": {"type": "string", "enum": list(TIPOS_ITEM)},
            "estado": {"type": "string", "enum": [*ESTADOS_ITEM, "todos"], "default": "abierto"},
        }, "additionalProperties": False},
        pendientes,
    ),
    Herramienta(
        "marcar_item", "Marcar item",
        "Marca un item de pendientes como hecho o descartado (o lo vuelve a abrir).",
        {"type": "object", "properties": {
            "id": {"type": ["integer", "string"], "description": "Id del item (de pendientes)."},
            "estado": {"type": "string", "enum": list(ESTADOS_ITEM)},
        }, "required": ["id", "estado"], "additionalProperties": False},
        marcar_item,
        solo_lectura=False,
        anotaciones={"destructiveHint": False, "idempotentHint": True},
    ),
]}
