"""Adjuntos de una clase (§5.1, §6, §10): PDF de slides o de notas del profe, e imágenes.

El celu (o el panel) los sube a una sesión; el agente del apunte los usa como material de
referencia y `publicar` los copia a la carpeta de la clase.

- **Archivo:** `sesiones/<id>/adjuntos/<nombre>`. El nombre lo arma el server: el original pasado a
  ASCII (sin tildes, espacios ni símbolos) más la extensión del tipo detectado por el contenido. La
  raíz es única por sesión (sin distinguir mayúsculas), así el nombre para LaTeX no choca nunca.
- **Base:** una fila en `assets` con `tipo = 'adjunto'` (no cuenta como evento: ni en `GET /eventos` ni
  en `/finalizar`) y una línea en `sesiones/<id>/adjuntos.jsonl`.
- **Qué usó cada apunte:** `apuntes.adjuntos` (JSON con los ids) y, mientras corre la etapa `apunte`,
  `jobs/<id>/apunte/input/adjuntos.json` con el id del job. De ahí sale el estado de cada adjunto:
  `pendiente` (lo va a usar el próximo apunte), `usado` (lo usó el vigente) o `sin_usar`.
- **Para LaTeX** (carpeta de trabajo del agente y carpeta publicada): los PDF tal cual (`<raíz>.pdf`);
  las imágenes como `<raíz>.jpg` con la regla de las fotos (§7): el original si es un JPEG derecho y
  dentro de los límites de la API; si no, rotada, reducida o convertida.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from classmer.db import Base
from classmer.sesiones import (
    ArchivoGrande,
    ErrorDatos,
    fila_sesion,
    lock_sesion,
)
from classmer.util import ahora_iso, escribir_json, leer_json, ms_a_hhmmss

if TYPE_CHECKING:
    from classmer.config import Config

log = logging.getLogger("classmer.adjuntos")

# HEIC/HEIF (fotos del celu en "alta eficiencia"): Pillow los abre con pillow-heif. Sin el paquete,
# se aceptan igual pero no se pueden convertir para el agente ni para el PDF (queda el aviso).
try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - depende del entorno
    HEIF_DISPONIBLE = False
else:
    register_heif_opener()
    HEIF_DISPONIBLE = True

MAX_ADJUNTO = 100 * 1024 * 1024
MAX_OFFSET_MS = 7 * 24 * 3600 * 1000  # una semana: más que cualquier grabación
# Una imagen adjunta que no es JPEG se decodifica entera (sólo JPEG se achica al leerla) y con alfa hace
# varias copias: un PNG liso de pocos MB puede pedir GB de RAM. Más que esto no se prepara (queda el aviso).
MAX_PIXELES_IMAGEN = 100_000_000
CARPETA = "adjuntos"
TIPO_ASSET = "adjunto"
REGISTRO = "adjuntos.jsonl"        # en la carpeta de la sesión: una línea por adjunto recibido
MANIFIESTO = "adjuntos.json"       # en input/ de la carpeta de trabajo del apunte
LARGO_RAIZ = 60
RAIZ_POR_DEFECTO = "adjunto"
ORIGENES = ("app", "panel", "importador")   # importador: el material de classmer importar y POST /api/importar

PENDIENTE, USADO, SIN_USAR = "pendiente", "usado", "sin_usar"

# tipo → (etiqueta, extensión guardada, mime)
TIPOS: dict[str, tuple[str, str, str]] = {
    "pdf": ("PDF", ".pdf", "application/pdf"),
    "jpeg": ("JPEG", ".jpg", "image/jpeg"),
    "png": ("PNG", ".png", "image/png"),
    "webp": ("WebP", ".webp", "image/webp"),
    "heic": ("HEIC", ".heic", "image/heic"),
}
TEXTO_TIPOS = "PDF, JPEG, PNG, WebP o HEIC"

# Marcas ISO-BMFF de HEIF/HEIC ("ftyp"). AVIF también es mif1, pero no es lo que se pidió.
_MARCAS_HEIC = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx"}
_MARCAS_HEIF = {b"mif1", b"msf1"}
_MARCAS_AVIF = {b"avif", b"avis"}

# El id lo genera el celu (o el panel): un UUID con guiones (sin llaves ni "urn:uuid:", que uuid.UUID acepta).
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Nombres que Windows no deja usar como archivo (con cualquier extensión).
_RESERVADOS_WIN = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class TipoNoSoportado(ValueError):
    """El contenido no es PDF, JPEG, PNG, WebP ni HEIC (→ 415)."""


# --- tipo y nombre -------------------------------------------------------------------------------

def detectar_tipo(cabecera: bytes) -> str | None:
    """El tipo por los bytes mágicos (no por la extensión): pdf, jpeg, png, webp, heic o None."""
    if b"%PDF-" in cabecera[:1024]:
        return "pdf"
    if cabecera.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if cabecera.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(cabecera) >= 12 and cabecera[:4] == b"RIFF" and cabecera[8:12] == b"WEBP":
        return "webp"
    if len(cabecera) >= 16 and cabecera[4:8] == b"ftyp":
        largo = int.from_bytes(cabecera[:4], "big")
        fin = min(len(cabecera), largo if 16 <= largo <= 4096 else 16)
        principal = cabecera[8:12]
        compatibles = {cabecera[i:i + 4] for i in range(16, fin - 3, 4)}
        if principal in _MARCAS_HEIC:
            return "heic"
        if principal not in _MARCAS_AVIF and ({principal} | compatibles) & (_MARCAS_HEIC | _MARCAS_HEIF):
            return "heic"
    return None


def raiz_nombre(nombre_original: str | None) -> str:
    """La raíz del nombre (sin extensión) en ASCII: letras, números, '-' y '_'. Nunca vacía ni con
    rutas: 'C:\\x\\Clase 5 - Límites.pdf' → 'Clase_5_-_Limites'."""
    nombre = re.split(r"[\\/]", nombre_original or "")[-1].strip()
    raiz = nombre.rsplit(".", 1)[0] if "." in nombre.lstrip(".") else nombre
    ascii_ = unicodedata.normalize("NFKD", raiz).encode("ascii", "ignore").decode("ascii")
    limpio = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_)
    limpio = re.sub(r"_{2,}", "_", limpio).strip("_-")
    if len(limpio) > LARGO_RAIZ:
        limpio = limpio[:LARGO_RAIZ].rstrip("_-")
    if not limpio:
        return RAIZ_POR_DEFECTO
    if limpio.casefold() in _RESERVADOS_WIN:
        limpio += "_"
    return limpio


def elegir_nombre(raiz: str, extension: str, ocupadas: Iterable[str]) -> str:
    """`raiz + extension`, o `raiz-2`, `raiz-3`… si esa raíz ya la usa otro adjunto de la sesión."""
    usadas = {r.casefold() for r in ocupadas}
    candidata, k = raiz, 2
    while candidata.casefold() in usadas:
        sufijo = f"-{k}"
        candidata = raiz[:LARGO_RAIZ - len(sufijo)].rstrip("_-") + sufijo
        k += 1
    return candidata + extension


def nombre_latex(adjunto: dict[str, Any]) -> str:
    """El nombre en input/adjuntos/ y en la carpeta publicada: `<raíz>.pdf` o `<raíz>.jpg`."""
    raiz = Path(adjunto["nombre"]).stem
    return raiz + (".pdf" if adjunto["tipo"] == "pdf" else ".jpg")


def info_pdf(ruta: Path) -> dict[str, Any]:
    """{'paginas': N, 'cifrado': bool} de un PDF; lo que no se pueda saber queda afuera."""
    try:
        from pypdf import PdfReader

        lector = PdfReader(str(ruta), strict=False)
        datos: dict[str, Any] = {}
        if lector.is_encrypted:
            datos["cifrado"] = True
            try:
                if not lector.decrypt(""):  # con contraseña para abrirlo: no se pueden contar las páginas
                    return datos
            except Exception:  # noqa: BLE001 — cifrado que pypdf no sabe abrir
                return datos
        datos["paginas"] = len(lector.pages)
        return datos
    except Exception as e:  # noqa: BLE001 — un PDF raro no frena la subida
        log.info("no pude leer el PDF %s: %s", ruta.name, e)
        return {}


# --- lectura -------------------------------------------------------------------------------------

def _datos(fila: sqlite3.Row) -> dict[str, Any]:
    try:
        datos = json.loads(fila["datos"]) if fila["datos"] else {}
    except ValueError:
        return {}
    return datos if isinstance(datos, dict) else {}


def desde_fila(fila: sqlite3.Row) -> dict[str, Any]:
    datos = _datos(fila)
    ruta = fila["ruta"] or ""
    tipo = datos.get("tipo") or "pdf"
    return {
        "id": fila["id"],
        "ruta": ruta,
        "nombre": Path(ruta).name,
        "nombre_original": datos.get("nombre_original") or Path(ruta).name,
        "tipo": tipo,
        "mime": datos.get("mime") or TIPOS.get(tipo, ("", "", "application/octet-stream"))[2],
        "bytes": fila["bytes_recibidos"],
        "offset_ms": fila["offset_ms"],
        "creado": datos.get("creado") or fila["creado"],
        "recibido": fila["creado"],
        "sha256": fila["sha256"],
        "paginas": datos.get("paginas"),
        "cifrado": bool(datos.get("cifrado")),
        "origen": datos.get("origen"),
    }


def listar(conn: sqlite3.Connection, sesion_id: str) -> list[dict[str, Any]]:
    """Los adjuntos de la sesión, en orden de llegada."""
    filas = conn.execute(
        "SELECT * FROM assets WHERE sesion_id = ? AND tipo = ? ORDER BY creado, rowid",
        (sesion_id, TIPO_ASSET),
    ).fetchall()
    return [desde_fila(f) for f in filas]


def ids_usados(fila_apunte: sqlite3.Row | None) -> set[str]:
    """Los adjuntos que usó una versión del apunte (columna `apuntes.adjuntos`; NULL = ninguno)."""
    if fila_apunte is None:
        return set()
    try:
        valor = json.loads(fila_apunte["adjuntos"] or "[]")
    except (ValueError, IndexError, KeyError):
        return set()
    return {str(x) for x in valor} if isinstance(valor, list) else set()


def ruta_manifiesto(cfg: Config, sesion_id: str) -> Path:
    return cfg.jobs_dir / sesion_id / "apunte" / "input" / MANIFIESTO


def escribir_manifiesto(workdir: Path, job_id: int | None, ids: list[str]) -> None:
    """input/adjuntos.json: qué adjuntos usa la corrida en curso del apunte (y de qué job es)."""
    escribir_json(workdir / "input" / MANIFIESTO, {"job": job_id, "adjuntos": ids})


def _en_curso(cfg: Config, sesion_id: str, job_id: int) -> set[str] | None:
    """Los adjuntos que tomó el job de apunte que está corriendo; None si todavía no los eligió."""
    try:
        datos = leer_json(ruta_manifiesto(cfg, sesion_id))
    except (OSError, ValueError):
        return None
    if not isinstance(datos, dict) or datos.get("job") != job_id or not isinstance(datos.get("adjuntos"), list):
        return None
    return {str(x) for x in datos["adjuntos"]}


def estados(cfg: Config, conn: sqlite3.Connection, sesion_id: str,
            adjuntos: list[dict[str, Any]]) -> dict[str, str]:
    """id → pendiente | usado | sin_usar.

    - Si corre el apunte: lo que tomó esa corrida es `pendiente` (lo está usando); lo que llegó
      después, `sin_usar`. Mientras prepara la carpeta (todavía no eligió), todo `pendiente`.
    - Si falta grabar, elegir la materia o hay etapas en cola o corriendo hasta el apunte: `pendiente`
      (cada etapa encola la siguiente recién al terminar: mientras corre, no hay nada en cola).
    - Si no: `usado` si lo usó el apunte vigente; si no hay apunte, `pendiente`; si no, `sin_usar`.
    """
    if not adjuntos:
        return {}
    from classmer.pipeline import ETAPAS

    hasta_apunte = ETAPAS[:ETAPAS.index("apunte") + 1]
    s = conn.execute("SELECT estado FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
    jobs = conn.execute(
        "SELECT id, tipo, estado FROM jobs WHERE sesion_id = ? AND estado IN ('pendiente', 'corriendo')",
        (sesion_id,),
    ).fetchall()
    corriendo = next((j for j in jobs if j["tipo"] == "apunte" and j["estado"] == "corriendo"), None)
    # `jobs` ya son los pendientes y los que corren; el apunte corriendo lo resuelve `corriendo`.
    viene_apunte = (s is not None and s["estado"] in ("grabando", "esperando_usuario")) or any(
        j["tipo"] in hasta_apunte for j in jobs)
    actual = conn.execute(
        "SELECT adjuntos FROM apuntes WHERE sesion_id = ? AND actual = 1 ORDER BY version DESC LIMIT 1",
        (sesion_id,),
    ).fetchone()
    usados = ids_usados(actual)
    en_curso = _en_curso(cfg, sesion_id, corriendo["id"]) if corriendo is not None else None

    resultado: dict[str, str] = {}
    for a in adjuntos:
        if corriendo is not None:
            resultado[a["id"]] = PENDIENTE if en_curso is None or a["id"] in en_curso else SIN_USAR
        elif viene_apunte:
            resultado[a["id"]] = PENDIENTE
        elif a["id"] in usados:
            resultado[a["id"]] = USADO
        elif actual is None:
            resultado[a["id"]] = PENDIENTE
        else:
            resultado[a["id"]] = SIN_USAR
    return resultado


def a_api(adjunto: dict[str, Any], estado: str | None) -> dict[str, Any]:
    """Un adjunto como lo da la API (§5.1)."""
    return {
        "id": adjunto["id"],
        "nombre": adjunto["nombre"],
        "nombre_original": adjunto["nombre_original"],
        "tipo": adjunto["tipo"],
        "bytes": adjunto["bytes"],
        "offset_ms": adjunto["offset_ms"],
        "creado": adjunto["creado"],
        "estado": estado,
    }


def resumen_api(cfg: Config, conn: sqlite3.Connection, sesion_id: str) -> tuple[list[dict[str, Any]], int]:
    """(adjuntos como los da la API, cuántos están sin_usar) para el objeto de sesión."""
    adjuntos = listar(conn, sesion_id)
    if not adjuntos:
        return [], 0
    por_id = estados(cfg, conn, sesion_id, adjuntos)
    lista = [a_api(a, por_id.get(a["id"])) for a in adjuntos]
    return lista, sum(1 for a in lista if a["estado"] == SIN_USAR)


# --- guardar -------------------------------------------------------------------------------------

def _offset(valor: Any) -> int | None:
    if valor in (None, ""):
        return None
    try:
        offset = int(valor)
    except (TypeError, ValueError) as e:
        raise ErrorDatos(f"offset_ms inválido: {valor!r}") from e
    if offset < 0:
        raise ErrorDatos("offset_ms no puede ser negativo")
    if offset > MAX_OFFSET_MS:  # si no, SQLite no lo guarda (OverflowError → 500)
        raise ErrorDatos(f"offset_ms fuera de rango: {offset} (más de una semana de grabación)")
    return offset


def _existente(conn: sqlite3.Connection, sesion_id: str, adjunto_id: str) -> sqlite3.Row | None:
    """La fila del adjunto si ya está en esta sesión. Un id de otra sesión o de un evento: 422."""
    fila = conn.execute("SELECT * FROM assets WHERE id = ?", (adjunto_id,)).fetchone()
    if fila is None:
        return None
    if fila["sesion_id"] != sesion_id:
        raise ErrorDatos(f"el id {adjunto_id!r} ya pertenece a otra sesión")
    if fila["tipo"] != TIPO_ASSET:
        raise ErrorDatos(f"el id {adjunto_id!r} ya es de un evento ({fila['tipo']}) de esta sesión")
    return fila


def _copiar_con_tope(origen: Any, destino: Path, maximo: int) -> tuple[str, int, bytes]:
    """Copia un stream (o un archivo) a `destino` con tope de tamaño. Devuelve (sha256, bytes, cabecera)."""
    h = hashlib.sha256()
    total = 0
    cabecera = b""
    fuente = open(origen, "rb") if isinstance(origen, (str, Path)) else origen
    try:
        with open(destino, "xb") as f:
            while chunk := fuente.read(1024 * 1024):
                total += len(chunk)
                if total > maximo:
                    raise ArchivoGrande(f"el adjunto pesa más de {maximo // (1024 * 1024)} MB")
                if len(cabecera) < 4096:
                    cabecera += chunk[:4096 - len(cabecera)]
                h.update(chunk)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
    finally:
        if isinstance(origen, (str, Path)):
            fuente.close()
    return h.hexdigest(), total, cabecera


def _describir(cfg: Config, conn: sqlite3.Connection, sesion_id: str, fila: sqlite3.Row) -> dict[str, Any]:
    adjunto = desde_fila(fila)
    return a_api(adjunto, estados(cfg, conn, sesion_id, [adjunto]).get(adjunto["id"]))


def guardar(
    cfg: Config,
    base: Base,
    sesion_id: str,
    *,
    adjunto_id: str,
    archivo: Any,  # objeto con .read(n) (UploadFile.file) o Path
    nombre_original: str | None,
    offset_ms: int | str | None = None,
    creado: str | None = None,
    origen: str = "app",
) -> tuple[bool, dict[str, Any]]:
    """Guarda un adjunto, idempotente por id. Devuelve (creado, adjunto como lo da la API).

    Se acepta en cualquier estado de la sesión, también en error: así se suma material antes de
    regenerar (POST /regenerar acepta lista y error). Errores: NoExiste (404), ArchivoGrande (413),
    TipoNoSoportado (415) y ErrorDatos (422). Un id que ya está en esta sesión devuelve lo guardado
    sin leer el archivo (es un reenvío)."""
    if not _UUID.fullmatch(adjunto_id or ""):
        raise ErrorDatos(f"id de adjunto inválido: {adjunto_id!r} (tiene que ser un UUID)")
    adjunto_id = adjunto_id.lower()  # la misma UUID en mayúsculas es el mismo adjunto (idempotencia)
    offset = _offset(offset_ms)
    creado = (creado or "").strip() or None
    if creado is not None and len(creado) > 64:
        raise ErrorDatos("creado inválido: demasiado largo")
    if origen not in ORIGENES:
        raise ErrorDatos(f"origen inválido: {origen!r}")
    with base.conexion() as conn:
        fila_sesion(conn, sesion_id)
        previo = _existente(conn, sesion_id, adjunto_id)
        if previo is not None:
            return False, _describir(cfg, conn, sesion_id, previo)
    if archivo is None:
        raise ErrorDatos("falta el archivo (campo archivo)")

    carpeta = cfg.sesion_dir(sesion_id) / CARPETA
    carpeta.mkdir(parents=True, exist_ok=True)
    tmp = carpeta / f".{adjunto_id}.{secrets.token_hex(4)}.subiendo"
    final: Path | None = None
    try:
        sha, total, cabecera = _copiar_con_tope(archivo, tmp, MAX_ADJUNTO)
        if total == 0:
            raise ErrorDatos("el archivo está vacío")
        tipo = detectar_tipo(cabecera)
        if tipo is None:
            raise TipoNoSoportado(f"tipo de archivo no soportado: sólo {TEXTO_TIPOS} (se mira el contenido, "
                                  "no la extensión)")
        etiqueta, extension, mime = TIPOS[tipo]
        datos: dict[str, Any] = {"nombre_original": (nombre_original or "").strip() or None, "tipo": tipo,
                                 "mime": mime, "creado": creado, "origen": origen}
        if tipo == "pdf":
            datos.update(info_pdf(tmp))

        momento = ahora_iso(cfg.zona_horaria)
        with lock_sesion(sesion_id):
            with base.conexion() as conn:
                s = fila_sesion(conn, sesion_id)
                previo = _existente(conn, sesion_id, adjunto_id)
                if previo is not None:  # llegó el mismo id por otro lado mientras se copiaba
                    return False, _describir(cfg, conn, sesion_id, previo)
                ocupadas = [Path(f["ruta"] or "").stem for f in conn.execute(
                    "SELECT ruta FROM assets WHERE sesion_id = ? AND tipo = ?", (sesion_id, TIPO_ASSET))]
                ocupadas += [p.stem for p in carpeta.iterdir() if not p.name.startswith(".")]
                nombre = elegir_nombre(raiz_nombre(nombre_original), extension, ocupadas)
                final = carpeta / nombre
                datos["estado_sesion"] = s["estado"]
                guardados = {k: v for k, v in datos.items() if v is not None}
                os.replace(tmp, final)
                try:
                    cur = conn.execute(
                        """
                        INSERT INTO assets (id, sesion_id, tipo, offset_ms, ruta, sha256, bytes_recibidos, datos,
                                            creado)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO NOTHING
                        """,
                        (adjunto_id, sesion_id, TIPO_ASSET, offset, f"{CARPETA}/{nombre}", sha, total,
                         json.dumps(guardados, ensure_ascii=False), momento),
                    )
                except BaseException:
                    # Sin fila, el archivo quedaría huérfano para siempre (y su raíz, tomada: el reintento
                    # sería Slides-2.pdf). Después del INSERT ya no se borra: en autocommit la fila quedó.
                    final.unlink(missing_ok=True)
                    raise
                if not cur.rowcount:  # el mismo id entró en otra sesión a la vez
                    final.unlink(missing_ok=True)
                    final = None
                    _existente(conn, sesion_id, adjunto_id)  # lanza ErrorDatos
                    raise ErrorDatos(f"el id {adjunto_id!r} ya está en uso")
                fila = conn.execute("SELECT * FROM assets WHERE id = ?", (adjunto_id,)).fetchone()
                info = _describir(cfg, conn, sesion_id, fila)
            linea = {"id": adjunto_id, "tipo": tipo, "ruta": f"{CARPETA}/{nombre}", "offset_ms": offset,
                     "bytes": total, "sha256": sha, "recibido": momento, **guardados}
            with open(cfg.sesion_dir(sesion_id) / REGISTRO, "a", encoding="utf-8") as f:
                f.write(json.dumps(linea, ensure_ascii=False) + "\n")
        log.info("adjunto %s en %s: %s (%s, %d bytes)", adjunto_id[:8], sesion_id[:8], nombre, etiqueta, total)
        return True, info
    finally:
        tmp.unlink(missing_ok=True)


# --- para LaTeX: carpeta de trabajo del agente y carpeta publicada -------------------------------

@dataclass
class Preparado:
    adjunto: dict[str, Any]
    nombre: str                    # nombre en la carpeta de destino (= nombre_latex)
    ok: bool
    tratamiento: str | None = None  # pdf | original | rotada | reducida | convertida
    aviso: str | None = None


def preparar_en(cfg: Config, sesion_id: str, adjuntos: list[dict[str, Any]], destino: Path, *,
                exclusivo: bool = False) -> list[Preparado]:
    """Deja cada adjunto en `destino` con su nombre para LaTeX: el PDF tal cual y las imágenes como
    JPEG con la regla de las fotos (§7). Con `exclusivo`, nunca pisa un archivo (publicar). Lo que no se
    pudo preparar (falta el archivo, una imagen que no se abre) vuelve con ok=False y el aviso."""
    from classmer.pipeline.linea_de_tiempo import procesar_foto

    carpeta = cfg.sesion_dir(sesion_id)
    preparados: list[Preparado] = []
    if adjuntos:
        destino.mkdir(parents=True, exist_ok=True)
    for a in adjuntos:
        nombre = nombre_latex(a)
        origen = carpeta / a["ruta"] if a["ruta"] else None
        if origen is None or not origen.is_file():
            preparados.append(Preparado(a, nombre, False, aviso="falta el archivo en Classmer-data"))
            continue
        final = destino / nombre
        try:
            if a["tipo"] == "pdf":
                _copiar(origen, final, exclusivo)
                preparados.append(Preparado(a, nombre, True, "pdf"))
                continue
            grande = _demasiado_grande(origen)
            if grande:
                preparados.append(Preparado(a, nombre, False, aviso=grande))
                continue
            tmp = destino / f".{nombre}.{secrets.token_hex(4)}.tmp"
            try:
                _, _, accion = procesar_foto(origen, tmp)
                if exclusivo:
                    _copiar(tmp, final, True)
                else:
                    os.replace(tmp, final)
            finally:
                tmp.unlink(missing_ok=True)
            preparados.append(Preparado(a, nombre, True, accion))
        except FileExistsError:
            preparados.append(Preparado(a, nombre, False, aviso=f"ya existía {nombre}, no lo piso"))
        except Exception as e:  # noqa: BLE001 — una imagen que Pillow no abre (p. ej. HEIC sin pillow-heif)
            detalle = "no hay soporte de HEIC (falta pillow-heif)" if a["tipo"] == "heic" and not HEIF_DISPONIBLE \
                else f"{e.__class__.__name__}: {e}"
            preparados.append(Preparado(a, nombre, False, aviso=f"no se pudo abrir ({detalle})"))
    return preparados


def _demasiado_grande(ruta: Path) -> str | None:
    """El aviso si es una imagen que no es JPEG de más de MAX_PIXELES_IMAGEN (mira sólo la cabecera)."""
    from PIL import Image

    with Image.open(ruta) as img:
        ancho, alto = img.size
        if img.format == "JPEG" or ancho * alto <= MAX_PIXELES_IMAGEN:
            return None
    return f"imagen demasiado grande ({ancho}×{alto} px, más de {MAX_PIXELES_IMAGEN // 1_000_000} MP)"


def _copiar(origen: Path, destino: Path, exclusivo: bool) -> None:
    if not exclusivo:
        shutil.copyfile(origen, destino)
        return
    with open(origen, "rb") as fin, open(destino, "xb") as fout:
        try:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
        except BaseException:
            fout.close()
            destino.unlink(missing_ok=True)
            raise


# --- línea de tiempo -----------------------------------------------------------------------------

SECCION_TIMELINE = "# Material adjunto"
_LINEA_CON_HORA = re.compile(r"^\[(\d{2,}):(\d{2}):(\d{2})\] ")
_LINEA_ADJUNTO = re.compile(r"^(?:\[\d{2,}:\d{2}:\d{2}\] )?ADJUNTO ")


def describir_tipo(adjunto: dict[str, Any]) -> str:
    """'PDF, 23 páginas', 'PDF cifrado', 'imagen JPEG'…"""
    if adjunto["tipo"] != "pdf":
        return f"imagen {TIPOS.get(adjunto['tipo'], (adjunto['tipo'],))[0]}"
    texto = "PDF cifrado" if adjunto.get("cifrado") else "PDF"
    paginas = adjunto.get("paginas")
    if paginas:
        texto += f", {paginas} página" + ("" if paginas == 1 else "s")
    return texto


def linea_timeline(adjunto: dict[str, Any], aviso: str | None = None) -> str:
    """`[hh:mm:ss] ADJUNTO adjuntos/<nombre> (PDF, 23 páginas): "<nombre original>"` (sin la hora si
    se adjuntó fuera de la grabación)."""
    original = " ".join(str(adjunto.get("nombre_original") or adjunto["nombre"]).split()).replace('"', "'")
    linea = f'ADJUNTO {CARPETA}/{nombre_latex(adjunto)} ({describir_tipo(adjunto)}): "{original}"'
    if aviso:
        linea += f" [{aviso}: no está en {CARPETA}/]"
    if adjunto.get("offset_ms") is not None:
        linea = f"[{ms_a_hhmmss(adjunto['offset_ms'])}] {linea}"
    return linea


def _segundos(linea: str) -> int | None:
    m = _LINEA_CON_HORA.match(linea)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def refrescar_timeline(texto: str, adjuntos: list[dict[str, Any]],
                       avisos: dict[str, str] | None = None) -> str:
    """timeline.md con las líneas ADJUNTO rehechas para `adjuntos` (pudo llegar uno después de
    linea_de_tiempo): saca las que había y la sección "# Material adjunto", mete las que tienen hora en
    su lugar (después de lo que pasó en ese mismo segundo) y las demás en esa sección, al final."""
    avisos = avisos or {}
    lineas = [ln for ln in texto.splitlines() if not _LINEA_ADJUNTO.match(ln)]
    if SECCION_TIMELINE in lineas:  # la sección (sin sus líneas, ya sacadas) va al final de nuevo
        i = lineas.index(SECCION_TIMELINE)
        fin = next((j for j in range(i + 1, len(lineas)) if lineas[j].startswith("# ")), len(lineas))
        del lineas[i:fin]
    while lineas and not lineas[-1].strip():
        lineas.pop()
    # Lo que tiene hora va antes de las secciones del final ("# Fotos sin hora"); la línea 0 es el título.
    fin_horas = next((j for j in range(1, len(lineas)) if lineas[j].startswith("# ")), len(lineas))
    con_hora = sorted((a for a in adjuntos if a.get("offset_ms") is not None), key=lambda a: a["offset_ms"])
    for a in con_hora:
        seg = int(a["offset_ms"]) // 1000
        k = next((j for j in range(1, fin_horas) if (s := _segundos(lineas[j])) is not None and s > seg), fin_horas)
        lineas.insert(k, linea_timeline(a, avisos.get(a["id"])))
        fin_horas += 1
    sin_hora = [a for a in adjuntos if a.get("offset_ms") is None]
    if sin_hora:
        lineas.append(SECCION_TIMELINE)
        lineas += [linea_timeline(a, avisos.get(a["id"])) for a in sin_hora]
    return "\n".join(lineas) + "\n"
