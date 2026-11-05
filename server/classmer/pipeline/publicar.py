"""Etapa 7 — publicar (§6, §9): copia el apunte a la carpeta de la materia y rehace el índice.

Destino: `<materia.carpeta>/<carpeta_clases>/AAAA-MM-DD Clase NN - Título/` con apunte.pdf (si
compiló), apunte.tex, classmer.cls, fotos/pizarra_NN.jpg, transcript.md y, si la clase tiene
material adjunto, adjuntos/ con todos los de la sesión y el mismo nombre para LaTeX que vio el agente
(los PDF tal cual, las imágenes como JPEG: classmer.adjuntos.preparar_en). Así el .tex publicado
compila ahí también (\\graphicspath de classmer.cls). Un adjunto que no se pudo copiar queda como
aviso en pipeline.log.

En la carpeta de la materia queda sólo la versión vigente de cada clase: si la sesión ya tenía
una carpeta publicada, se archiva entera en `<datos>/apuntes-anteriores/<materia>/<fecha hora>
<nombre>/` (tal cual, con lo que el usuario le haya agregado) y la nueva se publica con el nombre normal.
Cada publicación queda en la tabla `publicaciones` (vigente | sin_archivar | archivada | no_esta).

Reglas duras (la carpeta de la materia es del usuario):
- Nunca modifica ni pisa archivos existentes: cada publicación es una carpeta nueva y cada archivo
  se abre en modo exclusivo. Sólo mueve (al archivo) carpetas que publicó esta misma sesión y
  que están dentro de la carpeta de clases de una materia. Si el nombre lo ocupa una carpeta
  ajena (otra clase o una del usuario), usa "… (v2)", "… (v3)".
- Una falla a mitad no pierde nada: primero archiva la vieja; si la nueva falla, borra la copia a
  medias (es nuestra) y devuelve la vieja a su lugar.
- No reemplaza una versión publicada que compiló por una que no compiló.
- Si la carpeta de la materia no existe, no la crea: no publica afuera, lo anota y sigue.
- Sólo crea `carpeta_clases` y la carpeta de la clase. `INDICE-CLASES.md` es de Classmer y se
  reescribe entero (atómico), pero sólo si lo escribió Classmer (lleva una marca).

Es la única etapa que deja la sesión en 'lista' (o en 'error' si el apunte no compiló).
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from classmer import adjuntos
from classmer.busqueda import indexar_publicada
from classmer.config import PLANTILLAS_DIR
from classmer.db import transaccion
from classmer.pipeline import Contexto, ErrorEtapa, Resultado, actualizar_sesion
from classmer.pipeline.linea_de_tiempo import CARPETA_FOTOS, encabezado, fecha_local
from classmer.sesiones import NoExiste, fila_sesion
from classmer.util import ahora_iso, cortar_en_palabra, escribir_atomico, limpiar_titulo, nombre_seguro, zona

if TYPE_CHECKING:
    from classmer.cola import Job
    from classmer.config import Config, Materia

log = logging.getLogger("classmer.pipeline")

INDICE = "INDICE-CLASES.md"
MARCA_INDICE = "<!-- Generado por Classmer: se rehace solo en cada publicación, no lo edites. -->"
SUFIJO_NO_COMPILO = " (no compiló)"
LARGO_TITULO = 60
LARGO_RESUMEN = 160
MAX_VERSIONES = 99
SIN_TITULO = "Sin título"
ERROR_NO_COMPILO = (
    "el apunte no compiló: se guardaron el .tex y el log; "
    "reintentá con classmer reprocesar {id} --desde apunte"
)

CARPETA_ARCHIVO = "apuntes-anteriores"
# Esperas entre intentos de archivar una carpeta en uso (Windows no deja mover una carpeta con un
# archivo abierto, p. ej. el PDF): ~10 s en total. Los tests las cambian, igual que `dormir`.
ESPERAS_ARCHIVAR: tuple[float, ...] = (0.5, 1, 2, 3, 3.5)
dormir = time.sleep
AVISO_EN_USO = ("no pude archivarla: está en uso (¿un PDF abierto?): cerralo; "
                "se archiva en la próxima publicación")

# Estados de la tabla publicaciones.
VIGENTE, SIN_ARCHIVAR, ARCHIVADA, NO_ESTA = "vigente", "sin_archivar", "archivada", "no_esta"

_locks_indice: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_indice(carpeta: Path) -> threading.Lock:
    clave = os.path.normcase(str(carpeta))
    with _locks_guard:
        return _locks_indice.setdefault(clave, threading.Lock())


# --- helpers públicos --------------------------------------------------------------------------

def apunte_actual(conn: sqlite3.Connection, sesion_id: str) -> sqlite3.Row | None:
    """Fila del apunte vigente (actual = 1, la versión más alta)."""
    return conn.execute(
        "SELECT * FROM apuntes WHERE sesion_id = ? AND actual = 1 ORDER BY version DESC LIMIT 1",
        (sesion_id,),
    ).fetchone()


def calcular_numero_clase(cfg: Config, conn: sqlite3.Connection, sesion: sqlite3.Row) -> int | None:
    """Número de clase si la sesión no lo tiene: las clases anteriores de la materia + el inicial."""
    if sesion["numero_clase"] is not None:
        return int(sesion["numero_clase"])
    materia = cfg.materia(sesion["materia_id"])
    if materia is None:
        return None
    from classmer.pipeline.apunte import calcular_numero_clase as numero_de_apunte

    return numero_de_apunte(conn, materia, sesion)  # la misma regla que el PDF


def titulo_carpeta(titulo: str | None) -> str:
    """El título como va en el nombre de la carpeta: sin LaTeX, válido en Windows y, si es largo,
    cortado en un límite de palabra."""
    limpio = limpiar_titulo(titulo)
    seguro = nombre_seguro(limpio, 10_000) if limpio else ""  # sin cortar todavía
    if seguro == "Sin titulo" and limpio != seguro:  # no quedó nada válido en Windows (p. ej. "???")
        seguro = ""
    return cortar_en_palabra(seguro, LARGO_TITULO).rstrip(" .") or SIN_TITULO


def nombre_carpeta(fecha: str, numero: int | None, titulo: str, *, compilo: bool = True) -> str:
    """'AAAA-MM-DD Clase NN - Título' (válido en Windows), con ' (no compiló)' si hace falta."""
    nombre = f"{fecha} Clase {numero or 0:02d} - {titulo_carpeta(titulo)}"
    return nombre + ("" if compilo else SUFIJO_NO_COMPILO)


def carpeta_clases(materia: Materia | None) -> tuple[Path | None, str | None]:
    """(carpeta donde se publica, None) o (None, motivo por el que no se publica afuera)."""
    if materia is None:
        return None, "la sesión no tiene materia"
    if materia.carpeta is None:
        return None, f"la materia {materia.id} no tiene carpeta configurada"
    if not materia.carpeta.is_dir():
        return None, f"no existe la carpeta de la materia ({materia.carpeta})"
    raiz = materia.ruta_clases()
    assert raiz is not None
    try:
        raiz.resolve().relative_to(materia.carpeta.resolve())
    except ValueError:
        return None, f"carpeta_clases ({materia.carpeta_clases}) queda fuera de la carpeta de la materia"
    return raiz, None


def carpeta_archivo(cfg: Config) -> Path:
    """`<datos>/apuntes-anteriores`: donde van las versiones reemplazadas (el usuario la limpia a mano)."""
    return cfg.datos / CARPETA_ARCHIVO


def dentro_de(ruta: Path, raiz: Path) -> bool:
    """True si `ruta` está adentro de `raiz` (nunca la raíz misma), con las rutas resueltas."""
    try:
        r, base = ruta.resolve(), raiz.resolve()
        return r != base and r.is_relative_to(base)
    except OSError:
        return False


def materia_de_carpeta(cfg: Config, ruta: Path) -> Materia | None:
    """La materia en cuya carpeta de clases está `ruta` (activa o no), o None."""
    for m in cfg.materias:
        raiz = m.ruta_clases()
        if raiz is not None and dentro_de(ruta, raiz):
            return m
    return None


def publicaciones_de(conn: sqlite3.Connection, sesion_id: str) -> list[sqlite3.Row]:
    """Las publicaciones de una sesión, la más nueva primero."""
    return conn.execute(
        "SELECT * FROM publicaciones WHERE sesion_id = ? ORDER BY id DESC", (sesion_id,)
    ).fetchall()


def avisos_publicacion(conn: sqlite3.Connection, sesion_id: str | None = None) -> list[sqlite3.Row]:
    """Las carpetas que no se pudieron archivar (estado sin_archivar), de una sesión o de todas."""
    if sesion_id is None:
        return conn.execute(
            "SELECT * FROM publicaciones WHERE estado = ? ORDER BY id DESC", (SIN_ARCHIVAR,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM publicaciones WHERE sesion_id = ? AND estado = ? ORDER BY id DESC",
        (sesion_id, SIN_ARCHIVAR),
    ).fetchall()


# --- etapa -------------------------------------------------------------------------------------

@dataclass
class _Anterior:
    """Una carpeta que publicó antes esta sesión (vigente o sin_archivar) y qué pasó ahora con ella."""

    ruta: Path
    filas: list[int]                   # vacía: sesión vieja sin filas en publicaciones (ruta_publicada)
    estado: str
    version: int | None = None
    compilo: int | None = None
    nuevo_estado: str | None = None
    archivo: Path | None = None
    aviso: str | None = None
    materia_id: str | None = None
    restos: list[str] = field(default_factory=list)
    de_otra_sesion: bool = False       # otra sesión publicó después en esa misma ruta: no es nuestra


def ejecutar(ctx: Contexto, job: Job) -> Resultado:
    sid = job.sesion_id
    if not sid:
        raise ErrorEtapa("publicar necesita una sesión", reintentable=False)
    cfg = ctx.cfg
    carpeta = ctx.sesion_dir(sid)
    with ctx.base.conexion() as conn:
        try:
            sesion = fila_sesion(conn, sid)
        except NoExiste as e:
            raise ErrorEtapa(f"no existe la sesión {sid}", reintentable=False) from e
        apunte = apunte_actual(conn, sid)
        if apunte is None:
            raise ErrorEtapa("no hay apunte para publicar: reprocesá desde apunte", reintentable=False)
        numero = calcular_numero_clase(cfg, conn, sesion)
        anteriores = _anteriores(conn, sesion)
        material = adjuntos.listar(conn, sid)

    version = apunte["version"]
    tex = _en_sesion(carpeta, apunte["ruta_tex"]) or carpeta / "apunte" / f"v{version}" / "apunte.tex"
    if not tex.is_file():
        raise ErrorEtapa(f"falta el .tex del apunte v{version}: reprocesá desde apunte", reintentable=False)
    apunte_dir = tex.parent
    pdf = _en_sesion(carpeta, apunte["ruta_pdf"]) or apunte_dir / "apunte.pdf"
    compilo = bool(apunte["compilo"]) and pdf.is_file()
    avisos: list[str] = []
    if apunte["compilo"] and not compilo:
        avisos.append(f"el apunte v{version} figura como compilado pero no está el PDF")

    crudo = apunte["titulo"] or sesion["titulo"] or ""
    titulo = limpiar_titulo(crudo) or SIN_TITULO
    materia = cfg.materia(sesion["materia_id"])
    raiz, motivo = carpeta_clases(materia)

    campos: dict[str, Any] = {"titulo": titulo}
    if sesion["numero_clase"] is None and numero is not None:
        campos["numero_clase"] = numero
    if compilo:
        campos.update(estado="lista", error=None)
    else:
        campos.update(estado="error", error=ERROR_NO_COMPILO.format(id=sid))

    destino: Path | None = None
    conservada: _Anterior | None = None
    if raiz is None:
        ctx.log_sesion(sid, f"publicar: no publico en la carpeta de la materia: {motivo}; "
                            "el apunte queda sólo en Classmer-data")
        _guardar(ctx, sid, campos)
    elif not compilo and (conservada := _publicada_compilada(anteriores, raiz)) is not None:
        # No se reemplaza una versión que compiló por una que no: ruta_publicada sigue en la vieja y
        # esa versión vuelve a ser la `actual` (la que leen el índice, la búsqueda y el MCP).
        vigente = _version_publicada(ctx, sid, conservada)
        ctx.log_sesion(sid, f"publicar: el apunte v{version} no compiló y la versión publicada sí: "
                            f"la dejo donde está ({conservada.ruta}); la nueva queda sólo en "
                            f"Classmer-data (apunte/v{version})"
                            + (f"; la vigente sigue siendo la v{vigente}" if vigente else ""))
        if sesion["titulo"]:  # el título sigue siendo el de la publicada (el índice no los mezcla)
            campos.pop("titulo")
        _guardar(ctx, sid, campos, actual=vigente)
    else:
        fecha = fecha_local(cfg, sesion["inicio"])
        nombre = nombre_carpeta(fecha, numero, crudo, compilo=compilo)
        # En Auto, transcript.md se escribió antes de clasificar: el encabezado va con la materia de ahora.
        cabeza = encabezado(materia.nombre if materia else None, fecha, sesion["tipo_clase"], titulo="Transcript")

        def copiar(d: Path) -> None:
            _copiar_apunte(carpeta, apunte_dir, tex, pdf if compilo else None, d, avisos, cabeza)
            _copiar_adjuntos(cfg, sid, material, d, avisos)

        destino = _reemplazar(ctx, sid, raiz, nombre, anteriores, campos, avisos, copiar,
                              version=version, compilo=compilo)

    if conservada is None:
        indexar_publicada(ctx, sid)  # búsqueda FTS (§10); un error sólo queda como aviso
    # si se conservó la publicada, el índice de búsqueda sigue con esa (la vigente)

    if raiz is not None and materia is not None:
        try:
            if not regenerar_indice(ctx, materia):
                avisos.append(f"{INDICE} existe y no lo escribió Classmer: no lo toco")
        except Exception as e:  # noqa: BLE001 — ya se publicó: reintentar duplicaría la carpeta
            avisos.append(f"no pude escribir {INDICE}: {e.__class__.__name__}: {e}")
        # La carpeta anterior puede haber estado en otra materia (la sesión cambió de materia).
        for otra_id in sorted({a.materia_id for a in anteriores if a.materia_id} - {materia.id}):
            otra = cfg.materia(otra_id)
            try:
                if otra is not None:
                    regenerar_indice(ctx, otra)
            except Exception as e:  # noqa: BLE001
                avisos.append(f"no pude rehacer {INDICE} de {otra_id}: {e.__class__.__name__}: {e}")

    for aviso in avisos:
        ctx.log_sesion(sid, f"publicar: aviso: {aviso}")
    if destino is not None:
        ctx.log_sesion(sid, f"publicar: copiado a {destino}")
    if not compilo:
        ctx.log_sesion(sid, f"publicar: {campos['error']}")
    if destino is not None:
        resumen = f"publicado en {destino}"
        archivadas = [a for a in anteriores if a.nuevo_estado == ARCHIVADA]
        if archivadas:
            resumen += f"; la anterior quedó en {archivadas[-1].archivo}"
    elif conservada is not None:
        resumen = f"no compiló: queda publicada la versión anterior ({conservada.ruta})"
    else:
        resumen = f"sin publicar afuera: {motivo}"
    if not compilo and conservada is None:
        resumen += " (no compiló)"
    return Resultado(siguientes=[("notificar", {})], log=resumen)


def _anteriores(conn: sqlite3.Connection, sesion: sqlite3.Row) -> list[_Anterior]:
    """Las carpetas publicadas de la sesión que siguen en la materia (vigente y sin_archivar). Una
    sesión de antes de la tabla `publicaciones` tiene sólo `ruta_publicada`: es la vigente."""
    filas = conn.execute(
        "SELECT * FROM publicaciones WHERE sesion_id = ? ORDER BY id", (sesion["id"],)
    ).fetchall()
    por_ruta: dict[str, _Anterior] = {}
    for f in filas:
        if f["estado"] not in (VIGENTE, SIN_ARCHIVAR):
            continue
        clave = _clave(Path(f["ruta"]))
        if clave in por_ruta:
            a = por_ruta[clave]
            a.filas.append(f["id"])
            if f["estado"] == VIGENTE:  # la misma carpeta en dos filas: manda la vigente
                a.estado, a.version, a.compilo = VIGENTE, f["version"], f["compilo"]
        else:
            por_ruta[clave] = _Anterior(Path(f["ruta"]), [f["id"]], f["estado"], f["version"], f["compilo"])
    publicada = sesion["ruta_publicada"]
    if publicada and not any(_clave(Path(f["ruta"])) == _clave(Path(publicada)) for f in filas):
        por_ruta[_clave(Path(publicada))] = _Anterior(Path(publicada), [], VIGENTE)
    otras = rutas_de_otras_sesiones(conn, sesion["id"])
    for clave, a in por_ruta.items():
        a.de_otra_sesion = es_de_otra_sesion(otras, clave, a.filas)
    return list(por_ruta.values())


def rutas_de_otras_sesiones(conn: sqlite3.Connection, sesion_id: str) -> dict[str, int]:
    """Las carpetas (normalizadas) que reclaman otras sesiones → id de la fila de publicaciones más
    nueva que la reclama (0: `ruta_publicada` de una sesión de antes de la tabla, sin filas)."""
    otras: dict[str, int] = {}
    for (ruta,) in conn.execute(
            "SELECT ruta_publicada FROM sesiones WHERE id != ? AND ruta_publicada IS NOT NULL", (sesion_id,)):
        otras.setdefault(_clave(Path(ruta)), 0)
    for ruta, fila_id in conn.execute(
            "SELECT ruta, id FROM publicaciones WHERE sesion_id != ? AND estado IN (?, ?)",
            (sesion_id, VIGENTE, SIN_ARCHIVAR)):
        clave = _clave(Path(ruta))
        otras[clave] = max(otras.get(clave, 0), fila_id)
    return otras


def es_de_otra_sesion(otras: dict[str, int], clave: str, filas: list[int]) -> bool:
    """Si la carpeta de esta sesión se borró a mano y otra clase publicó después con el mismo nombre,
    esa carpeta ya no es nuestra: manda la publicación más nueva (ante la duda, no es nuestra)."""
    return clave in otras and otras[clave] >= max(filas, default=0)


def _clave(ruta: Path) -> str:
    return os.path.normcase(os.path.normpath(str(ruta)))


def _es_carpeta_real(ruta: Path) -> bool:
    """Un directorio de verdad (no un enlace ni una junction, que no son nuestros)."""
    return ruta.is_dir() and not ruta.is_symlink() and not os.path.isjunction(ruta)


def _publicada_compilada(anteriores: list[_Anterior], raiz: Path) -> _Anterior | None:
    """La vigente publicada si sigue en su lugar con su apunte.pdf, en la carpeta de clases de la
    materia de ahora (si la sesión cambió de materia, la de la otra materia se archiva igual)."""
    for a in anteriores:
        if (a.estado == VIGENTE and not a.de_otra_sesion and _es_carpeta_real(a.ruta)
                and dentro_de(a.ruta, raiz) and (a.ruta / "apunte.pdf").is_file()):
            return a
    return None


def _version_publicada(ctx: Contexto, sid: str, a: _Anterior) -> int | None:
    """La vN que está publicada en `a`: la que anotó la publicación o, en una sesión de antes de la
    tabla, la última que compiló."""
    with ctx.base.conexion() as conn:
        if a.version is not None and conn.execute(
                "SELECT 1 FROM apuntes WHERE sesion_id = ? AND version = ? AND compilo = 1",
                (sid, a.version)).fetchone():
            return int(a.version)
        fila = conn.execute("SELECT version FROM apuntes WHERE sesion_id = ? AND compilo = 1 "
                            "ORDER BY version DESC LIMIT 1", (sid,)).fetchone()
    return int(fila["version"]) if fila else None


def _reemplazar(ctx: Contexto, sid: str, raiz: Path, nombre: str, anteriores: list[_Anterior],
                campos: dict[str, Any], avisos: list[str], copiar, *, version: int, compilo: bool) -> Path:
    """Archiva las anteriores, publica la nueva y lo registra en la base. Si algo de la nueva falla,
    borra la copia a medias y devuelve las anteriores a su lugar (y relanza el error)."""
    destino: Path | None = None
    try:
        raiz.mkdir(parents=True, exist_ok=True)
        momento = ahora_local(ctx.cfg)
        for a in anteriores:
            _archivar(ctx, sid, a, momento)
        destino = _crear_carpeta_nueva(raiz, nombre)
        if destino.name != nombre:
            ctx.log_sesion(sid, f"publicar: ya hay una carpeta {nombre!r} que no es de esta publicación: "
                                f"publico en {destino.name!r}")
        copiar(destino)
        campos["ruta_publicada"] = str(destino)
        _guardar(ctx, sid, campos, anteriores, destino, version=version, compilo=compilo)
    except BaseException as e:
        ctx.log_sesion(sid, f"publicar: falló la publicación nueva ({e.__class__.__name__}: {e}); "
                            "deshago lo que hice")
        quedaron: list[_Anterior] = []
        if destino is not None:
            try:
                _borrar_con_reintentos(destino)  # es nuestra: la creamos recién
                ctx.log_sesion(sid, f"publicar: borré la copia a medias {destino}")
            except OSError as e2:
                ctx.log_sesion(sid, f"publicar: no pude borrar la copia a medias {destino}: {e2}")
                # Es nuestra: que quede registrada para archivarla en la próxima publicación.
                quedaron.append(_Anterior(destino, [], VIGENTE, version=version, compilo=int(compilo),
                                          nuevo_estado=SIN_ARCHIVAR,
                                          aviso=f"copia a medias de una publicación que falló ({e2})"))
        _restaurar(ctx, sid, anteriores)
        # Lo que no se pudo devolver a su lugar quedó entero en el archivo: que la base lo diga.
        quedaron += [a for a in anteriores if a.nuevo_estado == ARCHIVADA]
        if quedaron:
            try:
                _guardar_publicaciones(ctx, sid, quedaron)
            except Exception as e3:  # noqa: BLE001 — ya está en pipeline.log
                ctx.log_sesion(sid, f"publicar: no pude registrar en la base lo que quedó a medias: {e3}")
        raise
    for a in anteriores:
        avisos.extend(a.restos)
    return destino


def _archivar(ctx: Contexto, sid: str, a: _Anterior, momento: datetime) -> None:
    """Mueve una carpeta anterior al archivo. Deja en `a` el estado nuevo (archivada | no_esta |
    sin_archivar) y lo anota en pipeline.log. Nunca mueve algo que no sea una carpeta nuestra."""
    cfg = ctx.cfg
    if not a.ruta.exists() and not a.ruta.is_symlink():
        a.nuevo_estado, a.aviso = NO_ESTA, "ya no estaba (¿la borraron a mano?)"
        ctx.log_sesion(sid, f"publicar: la carpeta anterior {a.ruta} ya no está: no archivo nada")
        return
    if a.de_otra_sesion:
        a.nuevo_estado, a.aviso = NO_ESTA, "ahora es la carpeta publicada de otra clase: no la toco"
        ctx.log_sesion(sid, f"publicar: {a.ruta} ahora es la carpeta publicada de otra clase: no la toco")
        return
    if not _es_carpeta_real(a.ruta):
        a.nuevo_estado, a.aviso = NO_ESTA, "en su lugar hay otra cosa: no la toco"
        ctx.log_sesion(sid, f"publicar: en lugar de la carpeta anterior {a.ruta} hay otra cosa: no la toco")
        return
    materia = materia_de_carpeta(cfg, a.ruta)
    if materia is None:
        a.nuevo_estado = SIN_ARCHIVAR
        a.aviso = "no la archivo: está fuera de la carpeta de clases de las materias configuradas"
        ctx.log_sesion(sid, f"publicar: no archivo {a.ruta}: está fuera de la carpeta de clases de las "
                            "materias configuradas")
        return
    a.materia_id = materia.id
    destino = _libre(carpeta_archivo(cfg) / materia.id, f"{momento:%Y-%m-%d %H.%M.%S} {a.ruta.name}")
    try:
        resto = _mover_con_reintentos(a.ruta, destino)
    except OSError as e:
        a.nuevo_estado = SIN_ARCHIVAR
        a.aviso = AVISO_EN_USO if _en_uso(e) else (
            f"no pude archivarla ({e.__class__.__name__}: {e}); se archiva en la próxima publicación")
        ctx.log_sesion(sid, f"publicar: no pude archivar {a.ruta}: {a.aviso}")
        return
    a.nuevo_estado, a.archivo, a.aviso = ARCHIVADA, destino, None
    ctx.log_sesion(sid, f"publicar: archivé la versión anterior {a.ruta} en {destino}")
    if resto:
        a.restos.append(resto)


def _restaurar(ctx: Contexto, sid: str, anteriores: list[_Anterior]) -> None:
    """Devuelve a su lugar las carpetas que se archivaron en esta publicación (falló la nueva)."""
    for a in reversed(anteriores):
        if a.nuevo_estado != ARCHIVADA or a.archivo is None:
            continue
        try:
            resto = _mover_con_reintentos(a.archivo, a.ruta)
        except OSError as e:
            ctx.log_sesion(sid, f"publicar: no pude restaurar {a.ruta} ({e.__class__.__name__}: {e}): "
                                f"quedó entera en {a.archivo}")
        else:
            ctx.log_sesion(sid, f"publicar: restauré {a.ruta}" + (f" ({resto})" if resto else ""))
            a.nuevo_estado, a.archivo = None, None


def _guardar(ctx: Contexto, sid: str, campos: dict[str, Any], anteriores: list[_Anterior] | None = None,
             destino: Path | None = None, *, version: int | None = None, compilo: bool = False,
             actual: int | None = None) -> None:
    """La sesión y sus publicaciones, en una sola transacción. `actual`: la vN que vuelve a ser la
    vigente (cuando se conserva la publicada porque la nueva no compiló)."""
    momento = ahora_iso(ctx.cfg.zona_horaria)
    with ctx.base.conexion() as conn, transaccion(conn):
        actualizar_sesion(conn, sid, ctx.cfg.zona_horaria, **campos)
        if actual is not None:
            conn.execute("UPDATE apuntes SET actual = (version = ?) WHERE sesion_id = ?", (actual, sid))
        _escribir_anteriores(conn, sid, anteriores or [], momento)
        if destino is not None:
            conn.execute(
                "INSERT INTO publicaciones (sesion_id, ruta, version, compilo, estado, creado, actualizado) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, str(destino), version, int(compilo), VIGENTE, momento, momento),
            )


def _guardar_publicaciones(ctx: Contexto, sid: str, anteriores: list[_Anterior]) -> None:
    """Sólo las filas de publicaciones (falló la publicación nueva y algo quedó a medias)."""
    with ctx.base.conexion() as conn, transaccion(conn):
        _escribir_anteriores(conn, sid, anteriores, ahora_iso(ctx.cfg.zona_horaria))


def _escribir_anteriores(conn: sqlite3.Connection, sid: str, anteriores: list[_Anterior], momento: str) -> None:
    for a in anteriores:
        if a.nuevo_estado is None:
            continue
        archivo = str(a.archivo) if a.archivo is not None else None
        if not a.filas:  # sesión vieja: su ruta_publicada entra a la tabla
            conn.execute(
                "INSERT INTO publicaciones (sesion_id, ruta, version, compilo, estado, archivo, aviso, "
                "creado, actualizado) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, str(a.ruta), a.version, a.compilo, a.nuevo_estado, archivo, a.aviso, momento, momento),
            )
        for fila_id in a.filas:
            conn.execute(
                "UPDATE publicaciones SET estado = ?, archivo = ?, aviso = ?, actualizado = ? WHERE id = ?",
                (a.nuevo_estado, archivo, a.aviso, momento, fila_id),
            )


# --- mover al archivo --------------------------------------------------------------------------

def ahora_local(cfg: Config) -> datetime:
    """Fecha y hora del reemplazo (hora local de Classmer), para el nombre en el archivo."""
    return datetime.now(zona(cfg.zona_horaria))


def _libre(carpeta: Path, nombre: str) -> Path:
    """`carpeta/nombre`, o `nombre (2)`, `(3)`… si ya existe."""
    candidata = carpeta / nombre
    k = 2
    while candidata.exists() or candidata.is_symlink():
        candidata = carpeta / f"{nombre} ({k})"
        k += 1
    return candidata


def _en_uso(e: OSError) -> bool:
    """Windows no deja mover una carpeta con un archivo abierto: acceso denegado o en uso."""
    return isinstance(e, PermissionError) or getattr(e, "winerror", None) in (5, 32, 33)


def _otro_disco(e: OSError) -> bool:
    return e.errno == errno.EXDEV or getattr(e, "winerror", None) == 17  # ERROR_NOT_SAME_DEVICE


_renombrar = os.rename  # los tests lo reemplazan para simular bloqueos y otro disco


def mover_carpeta(origen: Path, destino: Path) -> str | None:
    """Mueve la carpeta entera a `destino` (que no tiene que existir). En el mismo disco es un rename;
    en otro, primero la aparta en su disco (si está en uso, falla ahí sin tocar nada), la copia y
    borra lo apartado. Devuelve un aviso si quedaron restos que no se pudieron borrar."""
    if destino.exists() or destino.is_symlink():
        raise FileExistsError(errno.EEXIST, "ya existe", str(destino))
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        _renombrar(origen, destino)
        return None
    except OSError as e:
        if not _otro_disco(e):
            raise
    apartada = _libre(origen.parent, f".{origen.name}.archivando")
    _renombrar(origen, apartada)
    try:
        shutil.copytree(apartada, destino, symlinks=True)
    except BaseException:
        shutil.rmtree(destino, ignore_errors=True)
        _renombrar(apartada, origen)
        raise
    try:
        borrar_carpeta(apartada)
    except OSError as e:
        return f"quedó una copia de más en {apartada} (ya está entera en {destino}): {e}"
    return None


def _mover_con_reintentos(origen: Path, destino: Path) -> str | None:
    esperas = list(ESPERAS_ARCHIVAR)
    while True:
        try:
            return mover_carpeta(origen, destino)
        except OSError as e:
            if not _en_uso(e) or not esperas:
                raise
            espera = esperas.pop(0)
            log.info("no pude mover %s (%s): reintento en %.1f s", origen, e, espera)
            dormir(espera)


def _borrar_con_reintentos(carpeta: Path) -> None:
    """borrar_carpeta, reintentando si algo la tiene abierta (en Windows, el antivirus o el indexador
    suelen abrir un rato los archivos recién copiados)."""
    esperas = list(ESPERAS_ARCHIVAR)
    while True:
        try:
            return borrar_carpeta(carpeta)
        except OSError as e:
            if not _en_uso(e) or not esperas:
                raise
            dormir(esperas.pop(0))


def borrar_carpeta(carpeta: Path) -> None:
    """rmtree que también borra archivos de sólo lectura (p. ej. copiados de la plantilla)."""
    def _solo_lectura(funcion, ruta, _exc):
        os.chmod(ruta, 0o700)
        funcion(ruta)

    shutil.rmtree(carpeta, onexc=_solo_lectura)


# --- copia -------------------------------------------------------------------------------------

def _en_sesion(carpeta: Path, ruta: str | None) -> Path | None:
    if not ruta:
        return None
    p = Path(ruta)
    return p if p.is_absolute() else carpeta / p


def _crear_carpeta_nueva(raiz: Path, nombre: str) -> Path:
    """Crea `nombre`, o `nombre (v2)`, `(v3)`… si ya existe. Nunca reutiliza una carpeta."""
    for k in range(1, MAX_VERSIONES + 1):
        candidata = raiz / (nombre if k == 1 else f"{nombre} (v{k})")
        try:
            candidata.mkdir()
        except FileExistsError:
            continue
        return candidata
    raise ErrorEtapa(f"ya hay {MAX_VERSIONES} versiones publicadas de {nombre!r}", reintentable=False)


def copiar_exclusivo(origen: Path, destino: Path) -> None:
    """Copia sin pisar nunca: abre el destino con 'xb' (FileExistsError si ya existe)."""
    with open(origen, "rb") as fin, open(destino, "xb") as fout:
        try:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
        except BaseException:
            fout.close()
            destino.unlink(missing_ok=True)  # es nuestra copia a medias
            raise


def escribir_exclusivo(destino: Path, datos: bytes) -> None:
    """Como copiar_exclusivo, pero con los bytes ya en memoria."""
    with open(destino, "xb") as fout:
        try:
            fout.write(datos)
            fout.flush()
            os.fsync(fout.fileno())
        except BaseException:
            fout.close()
            destino.unlink(missing_ok=True)
            raise


def _copiar_transcript(origen: Path, final: Path, cabeza: str | None) -> None:
    """Copia transcript.md con la primera línea ('# Transcript: …') cambiada por `cabeza`."""
    try:
        texto = origen.read_bytes().decode("utf-8")  # sin traducir saltos de línea
    except UnicodeDecodeError:
        texto = None
    if cabeza is None or texto is None or not texto.startswith("# Transcript:"):
        copiar_exclusivo(origen, final)
        return
    _, salto, resto = texto.partition("\n")
    escribir_exclusivo(final, (cabeza + (salto or "\n") + resto).encode("utf-8"))


def _copiar_apunte(sesion_dir: Path, apunte_dir: Path, tex: Path, pdf: Path | None,
                   destino: Path, avisos: list[str], cabeza_transcript: str | None = None) -> None:
    archivos: list[tuple[Path, Path]] = []
    if pdf is not None:
        archivos.append((pdf, destino / "apunte.pdf"))
    archivos.append((tex, destino / "apunte.tex"))
    cls = apunte_dir / "classmer.cls"
    if not cls.is_file():
        cls = PLANTILLAS_DIR / "classmer.cls"
    if cls.is_file():
        archivos.append((cls, destino / "classmer.cls"))
    else:
        avisos.append("no encontré classmer.cls para copiar")
    if pdf is None and (apunte_dir / "compilacion.log").is_file():
        archivos.append((apunte_dir / "compilacion.log", destino / "compilacion.log"))
    transcript = sesion_dir / "transcript.md"
    if transcript.is_file():
        archivos.append((transcript, destino / "transcript.md"))
    else:
        avisos.append("no está transcript.md")
    fotos = sorted((sesion_dir / CARPETA_FOTOS).glob("pizarra_*.jpg"))
    if fotos:
        (destino / "fotos").mkdir(exist_ok=True)
        archivos += [(f, destino / "fotos" / f.name) for f in fotos]

    for origen, final in archivos:
        try:
            if origen == transcript:
                _copiar_transcript(origen, final, cabeza_transcript)
            else:
                copiar_exclusivo(origen, final)
        except FileExistsError:
            avisos.append(f"ya existía {final}, no lo piso")


def _copiar_adjuntos(cfg: Config, sid: str, material: list[dict[str, Any]], destino: Path,
                     avisos: list[str]) -> None:
    """adjuntos/ de la carpeta publicada: todos los de la sesión, con el nombre para LaTeX y sin pisar
    nada. Sin adjuntos no hay carpeta; los que no se pudieron copiar van a los avisos."""
    if not material:
        return
    carpeta = destino / adjuntos.CARPETA
    preparados = adjuntos.preparar_en(cfg, sid, material, carpeta, exclusivo=True)
    for p in preparados:
        if not p.ok:
            avisos.append(f"no copié el adjunto {p.adjunto['nombre_original']!r} a {adjuntos.CARPETA}/: {p.aviso}")
    if not any(p.ok for p in preparados):
        try:
            carpeta.rmdir()  # la creó recién preparar_en y quedó vacía
        except OSError:
            pass


# --- índice ------------------------------------------------------------------------------------

def _celda(texto: Any) -> str:
    return " ".join(str(texto or "").split()).replace("|", "\\|")


def _corto(texto: str | None, largo: int = LARGO_RESUMEN) -> str:
    limpio = " ".join((texto or "").split())
    if len(limpio) <= largo:
        return limpio
    return limpio[:largo].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


def contenido_indice(cfg: Config, conn: sqlite3.Connection, materia: Materia, raiz: Path) -> str:
    """Markdown de INDICE-CLASES.md con las clases publicadas de la materia (leídas de la base)."""
    filas = conn.execute(
        """
        SELECT s.id, s.inicio, s.numero_clase, s.titulo, s.ruta_publicada,
               (SELECT a.resumen FROM apuntes a WHERE a.sesion_id = s.id AND a.actual = 1
                ORDER BY a.version DESC LIMIT 1) AS resumen
        FROM sesiones s
        WHERE s.materia_id = ? AND s.ruta_publicada IS NOT NULL
        ORDER BY s.inicio, s.creado
        """,
        (materia.id,),
    ).fetchall()
    lineas = [
        MARCA_INDICE,
        f"# Clases de {materia.nombre}",
        "",
        f"Apuntes que armó Classmer a partir de las grabaciones. Actualizado: {ahora_iso(cfg.zona_horaria)}.",
        "",
        "| Fecha | Clase | Título | Carpeta | Resumen |",
        "|---|---|---|---|---|",
    ]
    for f in filas:
        ruta = Path(f["ruta_publicada"])
        if not ruta.is_dir():
            continue  # la borraron a mano
        try:
            enlace = ruta.relative_to(raiz).as_posix()
        except ValueError:
            enlace = ruta.as_posix()
        numero = f"{f['numero_clase']:02d}" if f["numero_clase"] is not None else "—"
        titulo = limpiar_titulo(f["titulo"]) or SIN_TITULO
        lineas.append(
            f"| {fecha_local(cfg, f['inicio'])} | {numero} | {_celda(titulo)} "
            f"| [{_celda(ruta.name)}](<{enlace}/>) | {_celda(_corto(f['resumen']))} |"
        )
    return "\n".join(lineas) + "\n"


def regenerar_indice(ctx: Contexto, materia: Materia) -> bool:
    """Reescribe <carpeta_clases>/INDICE-CLASES.md. False si existe y no lo escribió Classmer."""
    raiz, _ = carpeta_clases(materia)
    if raiz is None or not raiz.is_dir():
        return True
    ruta = raiz / INDICE
    with _lock_indice(raiz):
        if ruta.exists():
            try:
                primera = ruta.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
            except OSError:
                primera = ""
            if primera.strip() != MARCA_INDICE:
                return False
        with ctx.base.conexion() as conn:
            texto = contenido_indice(ctx.cfg, conn, materia, raiz)
        escribir_atomico(ruta, texto)
    return True
