"""Lógica de sesiones (sin HTTP): crear, subir audio por offset, eventos idempotentes y finalizar.

La API (classmer.api.app) y el importador usan estas funciones. Todo lo que toca el audio de
una sesión pasa por un lock por sesión, así dos PUT simultáneos o un PUT y un finalizar no se
pisan.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from classmer import audio as audio_mod
from classmer import cola
from classmer.config import Config
from classmer.db import Base, transaccion
from classmer.materias import proximo_numero
from classmer.pipeline import actualizar_sesion, calcular_progreso
from classmer.util import (
    ahora_iso,
    escribir_atomico,
    id_sesion_valido,
    parsear_iso,
    sha256_archivo,
    zona,
)

MAX_AUDIO_REQUEST = 8 * 1024 * 1024
MAX_FOTO = 50 * 1024 * 1024
# Audio de una clase importada (§5.1, "Importar clase"): el de un amigo por WhatsApp, un video…
MAX_AUDIO_IMPORTADO = 2 * 1024 ** 3
# Copia en Opus del audio importado que Chrome no reproduce (amr, wma…), para el reproductor del panel.
AUDIO_PANEL = "audio.panel.ogg"
TIPOS_EVENTO = ("foto", "nota", "marca", "pausa", "reanudar")
MARCAS = ("importante", "no_entendi")
ORIGENES = ("calendario", "manual", "auto")
TIPOS_CLASE = ("teorica", "practica")
ESTADOS = ("grabando", "en_cola", "procesando", "lista", "error", "esperando_usuario")
# Los eventos de la grabación en `assets`: ni el audio ni los adjuntos (§5.1: los adjuntos son una
# cola aparte y no cuentan en GET /eventos, `eventos_totales` ni el chequeo de /finalizar).
SOLO_EVENTOS = "tipo NOT IN ('audio', 'adjunto')"


class ErrorDatos(ValueError):
    """Datos inválidos (→ 422)."""


class NoExiste(LookupError):
    """La sesión no existe (→ 404)."""


class Conflicto(Exception):
    """La sesión no está en un estado que permita el cambio (→ 409)."""


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_sesion(sesion_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(sesion_id)
        if lock is None:
            lock = _locks[sesion_id] = threading.Lock()
        return lock


PISTA_CELU = "celu"
PISTAS = ("principal", PISTA_CELU)
# La segunda pista (§15, "Grabar con los dos"): el audio del celu cuando el reloj es el principal, o uno
# grabado con otra app que se agrega desde el panel. Va en audio.celu.<ext> (de la app, audio.celu.ogg).
AUDIO_CELU = "audio.celu.ogg"


def pista_valida(pista: str | None) -> str | None:
    """None para el principal (sin `pista` o "principal"), "celu" para la segunda; si no, ErrorDatos."""
    if pista in (None, "", "principal"):
        return None
    if pista == PISTA_CELU:
        return PISTA_CELU
    raise ErrorDatos(f"pista inválida: {pista!r} (opciones: celu, o sin pista para el principal)")


def id_audio(sesion_id: str, pista: str | None = None) -> str:
    return f"audio-celu:{sesion_id}" if pista == PISTA_CELU else f"audio:{sesion_id}"


# --- lectura -----------------------------------------------------------------------------------

def fila_sesion(conn: sqlite3.Connection, sesion_id: str) -> sqlite3.Row:
    fila = conn.execute("SELECT * FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
    if fila is None:
        raise NoExiste(sesion_id)
    return fila


def ruta_audio(cfg: Config, conn: sqlite3.Connection, sesion_id: str, pista: str | None = None) -> Path:
    fila = conn.execute(
        "SELECT ruta FROM assets WHERE id = ?", (id_audio(sesion_id, pista),)
    ).fetchone()
    nombre = fila["ruta"] if fila and fila["ruta"] else (AUDIO_CELU if pista == PISTA_CELU else "audio.ogg")
    return cfg.sesion_dir(sesion_id) / nombre


def datos_celu(conn: sqlite3.Connection, sesion_id: str) -> dict[str, Any] | None:
    """Los datos de la segunda pista (sha256, bytes_totales, duracion_ms, desfase_ms, origen…) si la
    sesión la tiene cerrada: de `finalizar` (la app) o de "Agregar audio del celu" (el panel)."""
    fila = conn.execute("SELECT ruta, datos FROM assets WHERE id = ?", (id_audio(sesion_id, PISTA_CELU),)).fetchone()
    if fila is None:
        return None
    try:
        datos = json.loads(fila["datos"] or "{}")
    except ValueError:
        return None
    if not isinstance(datos, dict) or not datos.get("cerrada"):
        return None
    return {**datos, "ruta": fila["ruta"] or AUDIO_CELU}


def guardar_datos_celu(conn: sqlite3.Connection, sesion_id: str, cambios: dict[str, Any]) -> None:
    """Suma `cambios` a los datos de la segunda pista (p. ej. el desfase medido por correlación)."""
    fila = conn.execute("SELECT datos FROM assets WHERE id = ?", (id_audio(sesion_id, PISTA_CELU),)).fetchone()
    datos = json.loads(fila["datos"] or "{}") if fila is not None else {}
    datos.update(cambios)
    conn.execute("UPDATE assets SET datos = ? WHERE id = ?",
                 (json.dumps(datos, ensure_ascii=False), id_audio(sesion_id, PISTA_CELU)))


def ruta_audio_panel(cfg: Config, conn: sqlite3.Connection, sesion_id: str) -> Path:
    """Lo que reproduce el panel: la copia en Opus (audio.panel.ogg) si verificar la armó porque el
    navegador no reproduce el original; si no, el original."""
    copia = cfg.sesion_dir(sesion_id) / AUDIO_PANEL
    return copia if copia.is_file() else ruta_audio(cfg, conn, sesion_id)


def mensaje_audio_grande(total: int | None = None) -> str:
    """'el audio pesa 2,3 GB: el máximo para importar es 2 GB' (sin el total: 'más de 2 GB')."""
    tope = MAX_AUDIO_IMPORTADO / 1024 ** 3
    if total is None:
        peso = f"más de {tope:g} GB"
    else:
        peso = f"{math.ceil(total / 1024 ** 3 * 10) / 10:.1f}".replace(".", ",") + " GB"
    return f"el audio pesa {peso}: el máximo para importar es {tope:g} GB"


def nombre_audio_importado(nombre_original: str | None) -> str:
    """`audio.<ext>` con la extensión del nombre original si está en la lista blanca; si no, `audio.bin`
    (verificar lo renombra según el formato que detecta ffprobe)."""
    return "audio" + (audio_mod.extension_valida(nombre_original) or ".bin")


def tamano(ruta: Path) -> int:
    try:
        return ruta.stat().st_size
    except FileNotFoundError:
        return 0


def ruta_pdf_actual(cfg: Config, conn: sqlite3.Connection, sesion_id: str) -> Path | None:
    """PDF de la versión actual; si esa no compiló, el último que sí compiló (mejor que nada)."""
    filas = conn.execute(
        "SELECT ruta_pdf FROM apuntes WHERE sesion_id = ? AND compilo = 1 AND ruta_pdf IS NOT NULL "
        "ORDER BY actual DESC, version DESC",
        (sesion_id,),
    ).fetchall()
    for fila in filas:
        ruta = Path(fila["ruta_pdf"])
        if not ruta.is_absolute():
            ruta = cfg.sesion_dir(sesion_id) / ruta
        if ruta.is_file():
            return ruta
    return None


def a_api(cfg: Config, conn: sqlite3.Connection, fila: sqlite3.Row) -> dict[str, Any]:
    """Objeto de GET /api/sesiones/{id} (§5.1): los 8 campos del contrato + los informativos
    (inicio, tipo_clase, duracion_ms, numero_clase, titulo, adjuntos, adjuntos_sin_usar e importada).
    Los clientes ignoran los campos que no conocen."""
    from classmer.adjuntos import resumen_api

    from classmer.revision import dudas_pendientes

    sid = fila["id"]
    pdf = ruta_pdf_actual(cfg, conn, sid)
    adjuntos, sin_usar = resumen_api(cfg, conn, sid)
    espera = (fila["espera"] or "materia") if fila["estado"] == "esperando_usuario" else None
    return {
        "id": sid,
        "materia_id": fila["materia_id"],
        "estado": fila["estado"],
        "etapa": fila["etapa"],
        "progreso": calcular_progreso(conn, sid),
        "bytes_recibidos": tamano(ruta_audio(cfg, conn, sid)),
        "pdf": f"/api/sesiones/{sid}/apunte.pdf" if pdf else None,
        "error": fila["error"],
        "inicio": fila["inicio"],
        "tipo_clase": fila["tipo_clase"],
        "duracion_ms": fila["duracion_ms"],
        "numero_clase": fila["numero_clase"],
        "titulo": fila["titulo"],
        "adjuntos": adjuntos,
        "adjuntos_sin_usar": sin_usar,
        "importada": bool(fila["importada"]),
        # §15: en esperando_usuario, qué se espera (la materia o la revisión del transcript) y, en la
        # revisión, cuántas dudas quedan para revisar en el panel.
        "espera": espera,
        "dudas": dudas_pendientes(cfg, sid) if espera == "revision" else 0,
    }


def listar(cfg: Config, conn: sqlite3.Connection, limite: int = 20) -> list[dict[str, Any]]:
    filas = conn.execute(
        "SELECT * FROM sesiones ORDER BY inicio DESC, creado DESC, rowid DESC LIMIT ?", (limite,)
    ).fetchall()
    return [a_api(cfg, conn, f) for f in filas]


def ids_eventos(conn: sqlite3.Connection, sesion_id: str) -> list[str]:
    return [
        f["id"]
        for f in conn.execute(
            f"SELECT id FROM assets WHERE sesion_id = ? AND {SOLO_EVENTOS} ORDER BY offset_ms, creado",
            (sesion_id,),
        )
    ]


# --- crear -------------------------------------------------------------------------------------

def normalizar_inicio(cfg: Config, valor: str | datetime) -> str:
    """ISO 8601 en la zona de Classmer (así el orden de texto coincide con el cronológico)."""
    try:
        dt = parsear_iso(valor, cfg.zona_horaria) if isinstance(valor, str) else valor
    except ValueError as e:
        raise ErrorDatos(f"inicio inválido: {valor!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zona(cfg.zona_horaria))
    return dt.astimezone(zona(cfg.zona_horaria)).isoformat(timespec="seconds")


def validar_materia_y_tipo(cfg: Config, materia_id: str | None, tipo_clase: str | None) -> None:
    if materia_id is not None:
        materia = cfg.materia(materia_id)
        if materia is None:
            raise ErrorDatos(f"materia_id inexistente: {materia_id!r}")
        permitidos = materia.tipos_clase
    else:
        permitidos = list(TIPOS_CLASE)
    if tipo_clase is not None and tipo_clase not in permitidos:
        raise ErrorDatos(f"tipo_clase inválido: {tipo_clase!r} (opciones: {', '.join(permitidos)})")


def crear(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    sesion_id: str,
    materia_id: str | None,
    origen_materia: str | None,
    tipo_clase: str | None,
    inicio: str | datetime,
    dispositivo: str | None,
    audio_nombre: str = "audio.ogg",
    numero_clase: int | None = None,
    importada: bool = False,
    audio_original: str | None = None,
    audio_bytes: int | None = None,
) -> tuple[bool, sqlite3.Row]:
    """Crea la sesión si no existe (idempotente por id). Devuelve (creada, fila).

    El número de clase queda fijo acá: `numero_clase` si viene; si no, el `proximo_numero` de la
    materia (en Auto, null hasta que se asigne la materia).

    `audio_nombre` es el archivo en la carpeta de la sesión. En una importada, `audio_original` (el
    nombre que tenía) y `audio_bytes` (el tamaño declarado) quedan en los datos del asset del audio, y
    un `audio_bytes` de más de MAX_AUDIO_IMPORTADO da ArchivoGrande (413) sin crear nada. Si la sesión
    ya existe no se revalida nada."""
    if not id_sesion_valido(sesion_id):
        raise ErrorDatos(f"id de sesión inválido: {sesion_id!r}")
    existente = conn.execute("SELECT * FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
    if existente is not None:
        return False, existente
    if importada and audio_bytes is not None and audio_bytes > MAX_AUDIO_IMPORTADO:
        raise ArchivoGrande(mensaje_audio_grande(audio_bytes))
    if materia_id == "":
        materia_id = None
    validar_materia_y_tipo(cfg, materia_id, tipo_clase)
    if origen_materia is None:
        origen_materia = "auto" if materia_id is None else "manual"
    if origen_materia not in ORIGENES:
        raise ErrorDatos(f"origen_materia inválido: {origen_materia!r}")
    inicio_iso = normalizar_inicio(cfg, inicio)
    momento = ahora_iso(cfg.zona_horaria)
    carpeta = cfg.sesion_dir(sesion_id)
    (carpeta / "fotos").mkdir(parents=True, exist_ok=True)
    if numero_clase is not None and numero_clase < 0:
        raise ErrorDatos("numero_clase no puede ser negativo")
    datos_audio = None
    if importada:
        original = re.split(r"[\\/]", audio_original or "")[-1].strip()[:255]
        datos_audio = json.dumps({k: v for k, v in {"nombre_original": original or None,
                                                     "bytes_declarados": audio_bytes}.items()
                                  if v is not None}, ensure_ascii=False)
    with transaccion(conn):
        if numero_clase is None and materia_id is not None:
            numero_clase = proximo_numero(conn, cfg.materia(materia_id))  # type: ignore[arg-type]
        cur = conn.execute(
            """
            INSERT INTO sesiones (id, materia_id, tipo_clase, inicio, origen_materia, estado,
                                  dispositivo, numero_clase, importada, creado, actualizado)
            VALUES (?, ?, ?, ?, ?, 'grabando', ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (sesion_id, materia_id, tipo_clase, inicio_iso, origen_materia, dispositivo, numero_clase,
             1 if importada else 0, momento, momento),
        )
        if cur.rowcount:
            conn.execute(
                """
                INSERT INTO assets (id, sesion_id, tipo, offset_ms, ruta, bytes_recibidos, datos, creado)
                VALUES (?, ?, 'audio', 0, ?, 0, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (id_audio(sesion_id), sesion_id, audio_nombre, datos_audio, momento),
            )
    fila = fila_sesion(conn, sesion_id)
    if cur.rowcount:
        escribir_atomico(carpeta / "sesion.json", json.dumps(dict(fila), ensure_ascii=False, indent=2) + "\n")
    return bool(cur.rowcount), fila


# --- materia -----------------------------------------------------------------------------------

ETAPAS_DEL_APUNTE = ("apunte", "publicar", "notificar")


def asignar_materia(cfg: Config, base: Base, sesion_id: str, materia_id: str, *,
                    permitir_terminada: bool = False) -> str:
    """POST /api/sesiones/{id}/materia, `classmer asignar` y el panel. Devuelve el estado previo.

    - `esperando_usuario` por la materia: asigna y encola la revisión del transcript (clasificar ya
      terminó; con la revisión apagada en la materia, pasa derecho al apunte).
    - `esperando_usuario` por la revisión (§15): sólo cambia la materia y la clase sigue esperando las
      dudas (el apunte arranca con "Listo" o "Generar sin revisar" en el panel). El nombre del profe
      se pone al aplicar la revisión, así que sale el de la materia nueva.
    - `grabando`, `en_cola` o procesando antes del apunte: sólo la materia; clasificar la ve.
    - Si el apunte ya empezó, o la sesión está lista o en error: Conflicto (la CLI permite cambiarla
      en una sesión terminada con `permitir_terminada`; después se reprocesa).
    Si la materia cambia, el número de clase pasa a ser el próximo de la materia nueva (salvo que
    la sesión no tuviera materia y ya trajera un número).
    """
    materia = cfg.materia(materia_id)
    if materia is None:
        raise ErrorDatos(f"materia_id inexistente: {materia_id!r}")
    with base.transaccion() as conn:
        s = fila_sesion(conn, sesion_id)
        estado = s["estado"]
        if estado in ("lista", "error") and not permitir_terminada:
            raise Conflicto(f"la sesión ya terminó (estado {estado}): para cambiarle la materia, "
                            f"`uv run classmer asignar {sesion_id} --materia <id>` y reprocesala")
        empezado = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE sesion_id = ? AND estado = 'corriendo' "
            f"AND tipo IN ({','.join('?' * len(ETAPAS_DEL_APUNTE))})",
            (sesion_id, *ETAPAS_DEL_APUNTE),
        ).fetchone()[0]
        if empezado or (estado == "procesando" and s["etapa"] in ETAPAS_DEL_APUNTE):
            raise Conflicto("el apunte ya empezó: la materia no se puede cambiar")
        campos: dict[str, Any] = {"materia_id": materia_id, "origen_materia": "manual", "confianza": None}
        if materia_id != s["materia_id"] and (s["materia_id"] is not None or s["numero_clase"] is None):
            campos["numero_clase"] = proximo_numero(conn, materia)
        if estado == "esperando_usuario" and (s["espera"] or "materia") == "materia":
            campos.update(estado="en_cola", etapa="revisar_transcript", error=None)
            cola.encolar(conn, "revisar_transcript", sesion_id)
        actualizar_sesion(conn, sesion_id, cfg.zona_horaria, **campos)
    return estado


# --- audio -------------------------------------------------------------------------------------

@dataclass
class ResultadoAudio:
    ok: bool
    bytes_recibidos: int


def agregar_audio(cfg: Config, base: Base, sesion_id: str, offset: int, datos: bytes,
                  pista: str | None = None) -> ResultadoAudio:
    """PUT /audio?offset=N: si N coincide con lo recibido, agrega; si no, devuelve lo que hay.

    En una sesión importada, si offset + len(datos) pasa MAX_AUDIO_IMPORTADO: ArchivoGrande (413) sin
    escribir nada. Con `pista="celu"` (§15) es el mismo esquema para audio.celu.ogg, con el mismo tope."""
    with lock_sesion(sesion_id):
        with base.conexion() as conn:
            fila = fila_sesion(conn, sesion_id)
            if pista == PISTA_CELU:
                _asegurar_asset_celu(cfg, conn, sesion_id)
            ruta = ruta_audio(cfg, conn, sesion_id, pista)
        if datos and (fila["importada"] or pista == PISTA_CELU) and offset + len(datos) > MAX_AUDIO_IMPORTADO:
            raise ArchivoGrande(mensaje_audio_grande())
        actual = tamano(ruta)
        if offset != actual:
            return ResultadoAudio(False, actual)
        if fila["estado"] != "grabando" and datos:
            # La grabación ya se cerró: no se agregan bytes (el celu ve que ya está todo).
            return ResultadoAudio(False, actual)
        if datos:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            with open(ruta, "ab") as f:
                f.write(datos)
                f.flush()
                os.fsync(f.fileno())
        nuevo = tamano(ruta)
        with base.conexion() as conn:
            conn.execute(
                "UPDATE assets SET bytes_recibidos = ? WHERE id = ?", (nuevo, id_audio(sesion_id, pista))
            )
        return ResultadoAudio(True, nuevo)


def _asegurar_asset_celu(cfg: Config, conn: sqlite3.Connection, sesion_id: str, *, ruta: str = AUDIO_CELU,
                         origen: str = "app") -> None:
    """La fila de la segunda pista en `assets` (tipo audio: no cuenta como evento)."""
    conn.execute(
        """
        INSERT INTO assets (id, sesion_id, tipo, offset_ms, ruta, bytes_recibidos, datos, creado)
        VALUES (?, ?, 'audio', 0, ?, 0, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (id_audio(sesion_id, PISTA_CELU), sesion_id, ruta,
         json.dumps({"pista": PISTA_CELU, "origen": origen}), ahora_iso(cfg.zona_horaria)),
    )


# --- eventos -----------------------------------------------------------------------------------

def registrar_evento(
    cfg: Config,
    base: Base,
    sesion_id: str,
    *,
    evento_id: str,
    tipo: str,
    offset_ms: int | str | None,
    creado: str | None = None,
    texto: str | None = None,
    marca: str | None = None,
    archivo: Any = None,  # objeto con .read(n) (UploadFile.file) o Path
    nombre_archivo: str | None = None,
    datos_extra: dict[str, Any] | None = None,
) -> bool:
    """Registra un evento idempotente por id. Devuelve True si lo creó, False si ya existía."""
    if not id_sesion_valido(evento_id or ""):
        raise ErrorDatos(f"id de evento inválido: {evento_id!r}")
    if tipo not in TIPOS_EVENTO:
        raise ErrorDatos(f"tipo inválido: {tipo!r} (opciones: {', '.join(TIPOS_EVENTO)})")
    try:
        offset = None if offset_ms in (None, "") else int(offset_ms)
    except (TypeError, ValueError) as e:
        raise ErrorDatos(f"offset_ms inválido: {offset_ms!r}") from e
    if offset is not None and offset < 0:
        raise ErrorDatos("offset_ms no puede ser negativo")
    if offset is None and tipo != "foto":
        raise ErrorDatos("falta offset_ms")
    if tipo == "foto" and archivo is None:
        raise ErrorDatos("una foto necesita el campo archivo")

    with lock_sesion(sesion_id):
        with base.conexion() as conn:
            fila_sesion(conn, sesion_id)
            existente = conn.execute("SELECT sesion_id, tipo FROM assets WHERE id = ?", (evento_id,)).fetchone()
        if existente is not None:
            # Un 200 acá dejaría el evento sin guardar y el finalizar trabado en faltan_eventos (un
            # adjunto no cuenta como evento: SOLO_EVENTOS).
            if existente["sesion_id"] != sesion_id:
                raise ErrorDatos(f"el id de evento {evento_id!r} ya pertenece a otra sesión")
            if existente["tipo"] == "adjunto":
                raise ErrorDatos(f"el id de evento {evento_id!r} ya es de un adjunto de esta sesión")
            return False

        carpeta = cfg.sesion_dir(sesion_id)
        ruta_rel: str | None = None
        sha: str | None = None
        if tipo == "foto":
            extension = Path(nombre_archivo or "").suffix.lower()
            if extension not in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
                extension = ".jpg"
            ruta_rel = f"fotos/{evento_id}{extension}"
            sha = _guardar_archivo(archivo, carpeta / ruta_rel, MAX_FOTO)

        datos = {k: v for k, v in {"texto": texto, "marca": marca, "creado": creado,
                                   "nombre_original": nombre_archivo}.items() if v not in (None, "")}
        if datos_extra:
            datos.update(datos_extra)
        momento = ahora_iso(cfg.zona_horaria)
        with base.conexion() as conn:
            cur = conn.execute(
                """
                INSERT INTO assets (id, sesion_id, tipo, offset_ms, ruta, sha256, datos, creado)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (evento_id, sesion_id, tipo, offset, ruta_rel, sha,
                 json.dumps(datos, ensure_ascii=False), momento),
            )
        if cur.rowcount:
            linea = {"id": evento_id, "tipo": tipo, "offset_ms": offset, **datos}
            if ruta_rel:
                linea["ruta"] = ruta_rel
            with open(carpeta / "eventos.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(linea, ensure_ascii=False) + "\n")
        return bool(cur.rowcount)


def _guardar_archivo(origen: Any, destino: Path, maximo: int) -> str:
    """Copia a destino (temporal + renombrar) con tope de tamaño. Devuelve el sha256."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(f".{destino.name}.tmp")
    h = hashlib.sha256()
    total = 0
    fuente = open(origen, "rb") if isinstance(origen, (str, Path)) else origen
    try:
        with open(tmp, "wb") as f:
            while chunk := fuente.read(1024 * 1024):
                total += len(chunk)
                if total > maximo:
                    raise ArchivoGrande(f"archivo de más de {maximo // (1024 * 1024)} MB")
                h.update(chunk)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, destino)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if isinstance(origen, (str, Path)):
            fuente.close()
    return h.hexdigest()


class ArchivoGrande(ValueError):
    """Archivo más grande que el permitido (→ 413)."""


# --- audio del celu agregado desde el panel (§15) ----------------------------------------------------

ESTADOS_AGREGAR_CELU = ("lista", "error")


def agregar_audio_celu(cfg: Config, base: Base, sesion_id: str, archivo: Any, nombre_original: str | None
                       ) -> dict[str, Any]:
    """"Agregar audio del celu" (panel): un audio de la misma clase grabado con otra app pasa a ser la
    segunda pista (audio.celu.<ext>). El desfase lo mide segmentar por correlación. Sólo en una clase
    terminada (lista o en error); la anterior, si había, se aparta como audio.celu.descartado-….
    Devuelve los datos de la pista. ErrorDatos si no es un audio; ArchivoGrande si pasa los 2 GiB."""
    extension = audio_mod.extension_valida(nombre_original) or ".bin"
    with lock_sesion(sesion_id):
        with base.conexion() as conn:
            fila = fila_sesion(conn, sesion_id)
            if fila["estado"] not in ESTADOS_AGREGAR_CELU:
                raise Conflicto("el audio del celu se agrega a una clase terminada (lista o en error)")
            anterior = ruta_audio(cfg, conn, sesion_id, PISTA_CELU)
        carpeta = cfg.sesion_dir(sesion_id)
        tmp = carpeta / f".audio.celu.subiendo{extension}"
        sha = _guardar_archivo(archivo, tmp, MAX_AUDIO_IMPORTADO)
        try:
            info = audio_mod.sondear(tmp)
            if not audio_mod.hay_pista_de_audio(info):
                raise ErrorDatos("el archivo no tiene una pista de audio")
            if extension == ".bin":
                extension = audio_mod.extension_por_formato(info) or ".bin"
            duracion = audio_mod.duracion_ms(tmp)
        except audio_mod.ErrorAudio as e:
            tmp.unlink(missing_ok=True)
            raise ErrorDatos(f"no es un audio que se pueda leer: {e}") from e
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if anterior.exists():
            _descartar_audio(cfg, base, sesion_id, anterior, PISTA_CELU)
        destino = carpeta / f"audio.celu{extension}"
        os.replace(tmp, destino)
        datos = {"pista": PISTA_CELU, "origen": "panel", "cerrada": True, "sha256": sha,
                 "bytes_totales": destino.stat().st_size, "duracion_ms": duracion, "desfase_ms": None,
                 "nombre_original": re.split(r"[\\/]", nombre_original or "")[-1][:255] or None}
        with base.conexion() as conn:
            conn.execute(
                """
                INSERT INTO assets (id, sesion_id, tipo, offset_ms, ruta, sha256, bytes_recibidos, datos, creado)
                VALUES (?, ?, 'audio', 0, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET ruta = excluded.ruta, sha256 = excluded.sha256,
                    bytes_recibidos = excluded.bytes_recibidos, datos = excluded.datos
                """,
                (id_audio(sesion_id, PISTA_CELU), sesion_id, destino.name, sha, datos["bytes_totales"],
                 json.dumps(datos, ensure_ascii=False), ahora_iso(cfg.zona_horaria)),
            )
        return datos


# --- finalizar ---------------------------------------------------------------------------------

@dataclass
class ResultadoFinalizar:
    ok: bool
    cuerpo: dict[str, Any]


def finalizar(
    cfg: Config,
    base: Base,
    sesion_id: str,
    *,
    sha256: str,
    bytes_totales: int,
    duracion_ms: int | None,
    eventos_totales: int,
    celu: dict[str, Any] | None = None,
) -> ResultadoFinalizar:
    """POST /finalizar: comprueba bytes, eventos y hash; si todo cierra, encola el pipeline.

    Con `celu` ({"sha256", "bytes_totales", "duracion_ms", "desfase_ms"}, §15) también cierra la
    segunda pista. Orden de chequeo: faltan_bytes del principal → del celu → faltan_eventos →
    hash_distinto del principal → del celu. Los 409 de la segunda pista llevan `"pista": "celu"`.
    """
    with lock_sesion(sesion_id):
        with base.conexion() as conn:
            fila = fila_sesion(conn, sesion_id)
            ruta = ruta_audio(cfg, conn, sesion_id)
            ruta_celu = ruta_audio(cfg, conn, sesion_id, PISTA_CELU)
            if fila["estado"] != "grabando":
                # Ya finalizada (p. ej., se perdió la respuesta): idempotente.
                return ResultadoFinalizar(True, {"estado": fila["estado"]})
            recibidos = tamano(ruta)
            if recibidos < bytes_totales:
                return ResultadoFinalizar(False, {"motivo": "faltan_bytes", "bytes_recibidos": recibidos})
            con_celu = celu is not None and int(celu.get("bytes_totales") or 0) > 0
            recibidos_celu = tamano(ruta_celu) if con_celu else 0
            if con_celu and recibidos_celu < int(celu["bytes_totales"]):  # type: ignore[index]
                return ResultadoFinalizar(False, {"motivo": "faltan_bytes", "pista": PISTA_CELU,
                                                  "bytes_recibidos": recibidos_celu})
            eventos = conn.execute(
                f"SELECT COUNT(*) FROM assets WHERE sesion_id = ? AND {SOLO_EVENTOS}", (sesion_id,)
            ).fetchone()[0]
            if eventos < eventos_totales:
                return ResultadoFinalizar(False, {"motivo": "faltan_eventos", "eventos_recibidos": eventos})

        # Sin bytes, el hash es el de un archivo vacío (si no, un audio de 0 bytes daría
        # hash_distinto para siempre).
        hash_local = sha256_archivo(ruta) if recibidos else hashlib.sha256(b"").hexdigest()
        if recibidos != bytes_totales or hash_local.lower() != sha256.strip().lower():
            _descartar_audio(cfg, base, sesion_id, ruta)
            return ResultadoFinalizar(False, {"motivo": "hash_distinto", "bytes_recibidos": 0})
        hash_celu = None
        if con_celu:
            hash_celu = sha256_archivo(ruta_celu)
            if (recibidos_celu != int(celu["bytes_totales"])  # type: ignore[index]
                    or hash_celu.lower() != str(celu.get("sha256") or "").strip().lower()):  # type: ignore[union-attr]
                _descartar_audio(cfg, base, sesion_id, ruta_celu, PISTA_CELU)
                return ResultadoFinalizar(False, {"motivo": "hash_distinto", "pista": PISTA_CELU,
                                                  "bytes_recibidos": 0})

        with base.conexion() as conn, transaccion(conn):
            actualizar_sesion(
                conn, sesion_id, cfg.zona_horaria,
                estado="en_cola", etapa=None, error=None, sha256=hash_local,
                bytes_totales=bytes_totales, duracion_ms=duracion_ms,
                eventos_totales=eventos_totales, finalizado=ahora_iso(cfg.zona_horaria),
            )
            conn.execute(
                "UPDATE assets SET sha256 = ?, bytes_recibidos = ? WHERE id = ?",
                (hash_local, recibidos, id_audio(sesion_id)),
            )
            if con_celu:
                conn.execute("UPDATE assets SET sha256 = ?, bytes_recibidos = ? WHERE id = ?",
                             (hash_celu, recibidos_celu, id_audio(sesion_id, PISTA_CELU)))
                guardar_datos_celu(conn, sesion_id, {
                    "cerrada": True, "sha256": hash_celu, "bytes_totales": recibidos_celu,
                    "duracion_ms": celu.get("duracion_ms"),  # type: ignore[union-attr]
                    "desfase_ms": celu.get("desfase_ms"),  # type: ignore[union-attr]
                    "desfase_origen": "app" if celu.get("desfase_ms") is not None else None,  # type: ignore[union-attr]
                })
            cola.encolar(conn, "verificar", sesion_id)
        return ResultadoFinalizar(True, {"estado": "en_cola"})


def _descartar_audio(cfg: Config, base: Base, sesion_id: str, ruta: Path, pista: str | None = None) -> None:
    """Aparta la copia del server como audio.descartado-<timestamp>.<ext> (la del celu,
    audio.celu.descartado-<timestamp>.ogg) y vuelve a 0 bytes."""
    if ruta.exists():
        marca = datetime.now(zona(cfg.zona_horaria)).strftime("%Y%m%dT%H%M%S")
        destino = ruta.with_name(f"{ruta.stem}.descartado-{marca}{ruta.suffix}")
        n = 1
        while destino.exists():
            n += 1
            destino = ruta.with_name(f"{ruta.stem}.descartado-{marca}-{n}{ruta.suffix}")
        shutil.move(str(ruta), str(destino))
    with base.conexion() as conn:
        conn.execute("UPDATE assets SET bytes_recibidos = 0 WHERE id = ?", (id_audio(sesion_id, pista),))
