"""Importador ("Importar clase", plan B y §5.1 POST /api/importar): audio, fotos y material sueltos.

Dos caminos, una sola lógica:
- **Archivos sueltos** (`classmer importar`, `POST /api/importar` y el panel): `importar_clase` arma la
  sesión como si la hubiera subido el celu (audio copiado, fotos ubicadas por su hora EXIF respecto
  del inicio, material como adjuntos) y la finaliza, así el pipeline arranca igual que con la app.
  `analizar_archivos` es el paso "Analizar" del panel: propone el inicio sin crear nada.
- **App** (una sesión creada con `importada`): sube el audio por `PUT /audio` y las fotos sin minuto;
  `verificar` llama a `completar_importada`, que ubica esas fotos por EXIF, renombra un audio.bin,
  arma la copia para el reproductor del panel y escribe importacion.json.

Acá vive también el área temporal del panel (`dir_temporal`, `limpiar_temporales`…).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from classmer import adjuntos  # también registra el abridor de HEIC (pillow-heif)
from classmer import audio as audio_mod
from classmer import sesiones
from classmer.audio import ErrorAudio
from classmer.pipeline import ErrorEtapa, actualizar_sesion
from classmer.sesiones import MAX_AUDIO_IMPORTADO, ArchivoGrande
from classmer.util import ahora_iso, escribir_json, nombre_seguro, parsear_iso, sha256_archivo, zona

if TYPE_CHECKING:
    from classmer.config import Config
    from classmer.pipeline import Contexto

log = logging.getLogger("classmer.importador")

# Una foto hasta 5 min antes del inicio o después del final se recorta al rango sin marcarla.
MARGEN_MS = 5 * 60 * 1000

# Tags EXIF (IFD0 y sub-IFD Exif 0x8769).
IFD_EXIF = 0x8769
TAG_FECHA = 306                  # DateTime (IFD0)
TAG_FECHA_ORIGINAL = 36867       # DateTimeOriginal
TAG_FECHA_DIGITALIZADA = 36868   # DateTimeDigitized
TAG_OFFSET = 36880               # OffsetTime
TAG_OFFSET_ORIGINAL = 36881      # OffsetTimeOriginal
TAG_OFFSET_DIGITALIZADA = 36882  # OffsetTimeDigitized
TAG_SUBSEG_ORIGINAL = 37521      # SubsecTimeOriginal
TAG_SUBSEG_DIGITALIZADA = 37522  # SubsecTimeDigitized

_FECHA_EXIF_RE = re.compile(r"^\s*(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_OFFSET_EXIF_RE = re.compile(r"^\s*([+-])(\d{2}):?(\d{2})\s*$")
# Años creíbles para la hora de una foto o el creation_time de un audio (como el lastModified y el inicio
# del panel). Un reloj sin configurar o un EXIF editado da 0001 o 9999: con eso no se puede ni restar un
# minuto (OverflowError), así que cuenta como "sin hora".
ANIOS_CREIBLES = range(2000, 2101)

# Área temporal del panel ("Analizar", condición 3 del diseño): Classmer-data/importaciones/<token>/.
DIR_TEMPORAL = "importaciones"
VENCIMIENTO_TEMPORAL_H = 24
TOPES_TEMPORAL = {"audio": MAX_AUDIO_IMPORTADO, "fotos": sesiones.MAX_FOTO, "material": adjuntos.MAX_ADJUNTO}
_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_AUDIO_RENOMBRADO_RE = re.compile(r"audio\.[a-z0-9]{1,5}")   # lo que deja _renombrar_bin
_RESERVADOS_WIN = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

# Candidatos de inicio (condición 4 del diseño): de dónde sale cada uno, como lo muestra el panel.
ORIGENES_AUDIO = ("mtime_audio", "creation_time", "creation_time_como_fin")
ETIQUETAS_INICIO = {
    "exif_fotos": "Según las fotos (EXIF): 1 min antes de la primera",
    "mtime_audio": "Según la fecha del archivo, menos la duración",
    "creation_time": "Según los metadatos del audio",
    "creation_time_como_fin": "Según los metadatos del audio, tomados como el final de la grabación",
    "ahora_menos_duracion": "Ahora menos la duración: no hay ningún otro dato",
}
AVISO_ORIGEN = {
    "mtime_audio": "ojo, puede ser la hora en que se mandó o se bajó el archivo",
    "creation_time": "ojo, en WhatsApp suele ser la hora del envío",
    "creation_time_como_fin": "ojo, en WhatsApp suele ser la hora del envío",
}


class ErrorImportacion(ValueError):
    """No se puede importar (audio ilegible, materia inexistente, …). La API lo devuelve como 422."""


class AudioGrande(ErrorImportacion, ArchivoGrande):
    """El audio pasa MAX_AUDIO_IMPORTADO (2 GiB). La API lo devuelve como 413."""


@dataclass
class _Foto:
    ruta: Path
    indice: int = 0                          # la posición en la lista que se pasó (con las ilegibles)
    hora: datetime | None = None
    origen_hora: str | None = None          # exif | exif_digitalizada | exif_fecha | mtime | None
    offset_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Importacion:
    """Lo que devuelve importar_clase."""
    sesion_id: str
    material: list[dict[str, Any]] = field(default_factory=list)          # los adjuntos que entraron
    material_rechazado: list[dict[str, str]] = field(default_factory=list)  # [{"nombre", "motivo"}]
    avisos: list[str] = field(default_factory=list)


# --- API pública ---------------------------------------------------------------------------------

def importar_archivos(
    ctx: Contexto,
    audio: Path,
    *,
    materia_id: str | None,
    fotos: Sequence[Path] = (),
    inicio: datetime | None = None,
    tipo_clase: str | None = None,
    usar_mtime_fotos: bool = True,
    usar_mtime_audio: bool = True,
    nombre_original: str | None = None,
    numero_clase: int | None = None,
    material: Sequence[tuple[Path, str]] = (),
    dispositivo: str = "importador",
    mtime_audio: datetime | None = None,
    origen_material: str = "importador",
) -> str:
    """Como `importar_clase`, pero devuelve sólo el id de la sesión (queda en_cola). El material
    rechazado queda en importacion.json (`material_rechazado`)."""
    return importar_clase(
        ctx, audio, materia_id=materia_id, fotos=fotos, inicio=inicio, tipo_clase=tipo_clase,
        usar_mtime_fotos=usar_mtime_fotos, usar_mtime_audio=usar_mtime_audio,
        nombre_original=nombre_original, numero_clase=numero_clase, material=material,
        dispositivo=dispositivo, mtime_audio=mtime_audio, origen_material=origen_material,
    ).sesion_id


def importar_clase(
    ctx: Contexto,
    audio: Path,
    *,
    materia_id: str | None,
    fotos: Sequence[Path] = (),
    inicio: datetime | None = None,
    tipo_clase: str | None = None,
    usar_mtime_fotos: bool = True,
    usar_mtime_audio: bool = True,
    nombre_original: str | None = None,
    numero_clase: int | None = None,
    material: Sequence[tuple[Path, str]] = (),
    dispositivo: str = "importador",
    mtime_audio: datetime | None = None,
    origen_material: str = "importador",
    inicio_elegido: str | None = None,
    candidatos_panel: Sequence[dict[str, Any]] = (),
    mover_audio: bool = False,
) -> Importacion:
    """Crea una sesión importada con el audio, las fotos y el material, y la finaliza (queda en_cola).

    - `materia_id` None = Auto (la etapa clasificar decide). `numero_clase` fija el número; si no
      viene, el próximo de la materia (como POST /api/sesiones).
    - `inicio`: si no viene, se infiere (ver _inferir_inicio): la fecha del archivo − duración
      (`mtime_audio`, el lastModified que manda el navegador; si no, el mtime del archivo cuando
      `usar_mtime_audio`), el creation_time del contenedor o las fotos con hora EXIF.
      importacion.json guarda los candidatos y avisos. Con `inicio`, el panel dice además de dónde
      lo sacó el usuario (`inicio_elegido`: el `origen` del candidato que tocó, o "manual") y qué candidatos
      le mostró el paso Analizar (`candidatos_panel`): van a importacion.json.
    - Fotos: hora EXIF (DateTimeOriginal + OffsetTimeOriginal); sin EXIF, el mtime si
      `usar_mtime_fotos`; si no, quedan sin hora.
    - `material`: (ruta, nombre original) de PDFs o imágenes. Entran como adjuntos (origen
      `origen_material`) antes de finalizar. Uno rechazado (tipo, tamaño, vacío) no frena la
      importación: queda en `material_rechazado`, en importacion.json y en pipeline.log.
    - Un audio de más de MAX_AUDIO_IMPORTADO (2 GiB) da AudioGrande, una ErrorImportacion.
    - `mover_audio` (el panel: el audio está en el área temporal, en el mismo disco): se mueve a la
      carpeta de la sesión en vez de copiarse, así no hace falta el doble de lugar. Si no se puede
      mover (otro disco), se copia.
    - Si algo falla antes de finalizar (un OSError al copiar, un disco lleno…), la sesión a medio crear
      se borra (_deshacer): no queda una clase en error sin audio, ni se gasta su número, y un audio
      movido vuelve a su lugar (el análisis del panel sigue sirviendo para reintentar).
    """
    cfg = ctx.cfg
    audio = Path(audio)
    nombre_audio = nombre_original or audio.name
    if materia_id == "":
        materia_id = None
    if materia_id is not None and cfg.materia(materia_id) is None:
        raise ErrorImportacion(f"materia_id inexistente: {materia_id!r}")
    try:
        sesiones.validar_materia_y_tipo(cfg, materia_id, tipo_clase)
    except sesiones.ErrorDatos as e:
        raise ErrorImportacion(str(e)) from e
    if numero_clase is not None and numero_clase < 0:
        raise ErrorImportacion("numero_clase no puede ser negativo")
    if origen_material not in adjuntos.ORIGENES:
        raise ErrorImportacion(f"origen del material inválido: {origen_material!r}")
    if not audio.is_file():
        raise ErrorImportacion(f"no existe el audio: {audio}")
    total_audio = _chequear_tamano(audio)

    info, dur_ms = _leer_audio(audio, nombre_audio)
    tz = zona(cfg.zona_horaria)

    leidas, ilegibles = _leer_fotos([Path(f) for f in fotos], cfg.zona_horaria, usar_mtime_fotos)
    horas_exif = [f.hora for f in leidas if f.hora is not None and f.origen_hora != "mtime"]
    inicio_dt, origen_inicio, candidatos_inicio = _inferir_inicio(
        cfg.zona_horaria, audio, dur_ms, inicio, usar_mtime_audio, horas_exif,
        mtime_audio=mtime_audio, info=info,
    )
    inicio_dt = inicio_dt.astimezone(tz).replace(microsecond=0)

    for f in leidas:
        if f.hora is not None:
            f.offset_ms, f.extra = ubicar_offset(f.hora, inicio_dt, dur_ms)
    leidas.sort(key=lambda f: (f.offset_ms is None, f.offset_ms or 0, f.ruta.name))

    sid = str(uuid.uuid4())
    extension = _extension_audio(audio, nombre_original, info)
    with ctx.base.conexion() as conn:
        try:
            sesiones.crear(
                cfg, conn,
                sesion_id=sid, materia_id=materia_id,
                origen_materia="manual" if materia_id else "auto",
                tipo_clase=tipo_clase, inicio=inicio_dt, dispositivo=dispositivo,
                audio_nombre="audio" + extension, numero_clase=numero_clase,
                importada=True, audio_original=nombre_audio, audio_bytes=total_audio,
            )
        except sesiones.ErrorDatos as e:
            raise ErrorImportacion(str(e)) from e
        destino = sesiones.ruta_audio(cfg, conn, sid)

    movido = False
    try:
        movido = _traer_audio(audio, destino, mover_audio)
        sha = sha256_archivo(destino)
        total = destino.stat().st_size

        registradas: list[dict[str, Any]] = []
        for f in leidas:
            evento_id = str(uuid.uuid4())
            hora = f.hora.isoformat(timespec="seconds") if f.hora else None
            try:
                sesiones.registrar_evento(
                    cfg, ctx.base, sid,
                    evento_id=evento_id, tipo="foto", offset_ms=f.offset_ms, creado=hora,
                    archivo=f.ruta, nombre_archivo=f.ruta.name,
                    datos_extra={"origen_hora": f.origen_hora, "hora": hora, **f.extra},
                )
            except (sesiones.ArchivoGrande, OSError) as e:
                ilegibles.append({"nombre": f.ruta.name, "motivo": str(e)})
                continue
            registradas.append({
                "id": evento_id, "nombre_original": f.ruta.name, "hora": hora,
                "origen_hora": f.origen_hora, "offset_ms": f.offset_ms, **f.extra,
            })

        entro, rechazado = _guardar_material(ctx, sid, material, origen_material)
        avisos = _avisos_inicio(origen_inicio, registradas)
        if rechazado:
            avisos.append(f"{len(rechazado)} archivo(s) de material no entraron: "
                          + ", ".join(m["nombre"] for m in rechazado))
        resumen = {
            "audio": {"nombre_original": nombre_audio, "archivo": destino.name, "bytes": total,
                      "sha256": sha, "duracion_ms": dur_ms},
            "inicio": inicio_dt.isoformat(timespec="seconds"),
            "origen_inicio": origen_inicio,
            # Lo que se miró para elegir el inicio (con fotos: cuántas quedaban dentro con cada uno).
            "candidatos_inicio": candidatos_inicio,
            "fotos": registradas,
            "fotos_ilegibles": ilegibles,
            "material": entro,
            "material_rechazado": rechazado,
            "avisos": avisos,
            "importado": ahora_iso(cfg.zona_horaria),
            "via": "importador",
        }
        if origen_inicio == "parametro" and (inicio_elegido or candidatos_panel):
            resumen["inicio_elegido"] = inicio_elegido
            resumen["candidatos_inicio"] = list(candidatos_panel)
        escribir_json(cfg.sesion_dir(sid) / "importacion.json", resumen)
        with ctx.base.conexion() as conn:
            conn.execute(
                "UPDATE assets SET datos = ? WHERE id = ?",
                (json.dumps({"nombre_original": nombre_audio, "origen_inicio": origen_inicio,
                             "importado": True}, ensure_ascii=False),
                 sesiones.id_audio(sid)),
            )
        _registrar_en_log(ctx, sid, resumen)

        res = sesiones.finalizar(
            cfg, ctx.base, sid,
            sha256=sha, bytes_totales=total, duracion_ms=dur_ms, eventos_totales=len(registradas),
        )
        if not res.ok:
            raise ErrorImportacion(f"no se pudo cerrar la sesión importada: {res.cuerpo}")
    except BaseException as e:
        _deshacer(ctx, sid, str(e) or type(e).__name__, devolver=(destino, audio) if movido else None)
        raise
    return Importacion(sesion_id=sid, material=entro, material_rechazado=rechazado, avisos=avisos)


def analizar_archivos(
    cfg: Config,
    audio: Path,
    fotos: Sequence[Path] = (),
    *,
    nombre_original: str | None = None,
    mtime_audio: datetime | None = None,
) -> dict[str, Any]:
    """Paso "Analizar" del panel: lo que importar_clase haría con estos archivos, sin crear nada.

    Mide el audio, lee la hora EXIF de las fotos y propone el inicio con la misma lógica
    (_inferir_inicio, sin mtimes del server: sólo `mtime_audio`, el lastModified del navegador).
    Devuelve el audio, el inicio propuesto y su origen, los candidatos en orden de prioridad (con su
    etiqueta), las fotos ubicadas respecto del inicio propuesto, las ilegibles y los avisos. Errores:
    ErrorImportacion (audio ilegible) y AudioGrande (más de 2 GiB)."""
    audio = Path(audio)
    nombre_audio = nombre_original or audio.name
    if not audio.is_file():
        raise ErrorImportacion(f"no existe el audio: {audio}")
    total = _chequear_tamano(audio)
    info, dur_ms = _leer_audio(audio, nombre_audio)
    tz = zona(cfg.zona_horaria)

    leidas, ilegibles = _leer_fotos([Path(f) for f in fotos], cfg.zona_horaria, usar_mtime=False)
    horas = [f.hora for f in leidas if f.hora is not None]
    inicio_dt, origen, _ = _inferir_inicio(cfg.zona_horaria, audio, dur_ms, None, False, horas,
                                           mtime_audio=mtime_audio, info=info)
    inicio_dt = inicio_dt.astimezone(tz).replace(microsecond=0)
    audio_cands = _candidatos_audio(cfg.zona_horaria, audio, dur_ms, False, mtime_audio, info)
    candidatos = candidatos_inicio(cfg.zona_horaria, audio_cands, horas, dur_ms, elegido=origen)
    if origen == "ahora_menos_duracion":
        candidatos.append(_candidato(tz, inicio_dt, origen, None, 0, 0, elegido=True))

    for f in leidas:
        if f.hora is not None:
            f.offset_ms, f.extra = ubicar_offset(f.hora, inicio_dt, dur_ms)
    ordenadas = sorted(leidas, key=lambda f: (f.offset_ms is None, f.offset_ms or 0, f.ruta.name))
    fotos_api = []
    for f in ordenadas:
        foto: dict[str, Any] = {
            # El de `fotos` (contando las ilegibles): con eso el panel le pone el nombre original.
            "indice": f.indice, "nombre": f.ruta.name,
            "hora": f.hora.isoformat(timespec="seconds") if f.hora else None,
            "origen_hora": f.origen_hora, "offset_ms": f.offset_ms,
            "fuera_de_rango": bool(f.extra.get("fuera_de_rango")),
        }
        if "offset_original_ms" in f.extra:
            foto["offset_original_ms"] = f.extra["offset_original_ms"]
        fotos_api.append(foto)
    extension = _extension_audio(audio, nombre_original, info)
    return {
        "audio": {
            "nombre": nombre_audio, "bytes": total, "duracion_ms": dur_ms,
            "formato": audio_mod.formato(info), "codec": audio_mod.codec_audio(info),
            "extension": extension,
            "reproducible": audio_mod.reproducible_en_navegador(Path("audio" + extension), info),
        },
        "inicio": inicio_dt.isoformat(timespec="seconds"),
        "origen_inicio": origen,
        "candidatos_inicio": candidatos,
        "fotos": fotos_api,
        "fotos_ilegibles": ilegibles,
        "avisos": _avisos_analisis(origen, bool(horas), fotos_api),
    }


def ubicar_offset(hora: datetime, inicio: datetime, duracion_ms: int) -> tuple[int, dict[str, Any]]:
    """offset_ms de una foto respecto del inicio, recortado a [0, duración].

    Si cae a más de 5 min del rango, devuelve además {"fuera_de_rango": True, "offset_original_ms"}.
    """
    bruto = int(round((hora - inicio).total_seconds() * 1000))
    extra: dict[str, Any] = {}
    if bruto < -MARGEN_MS or bruto > duracion_ms + MARGEN_MS:
        extra = {"fuera_de_rango": True, "offset_original_ms": bruto}
    return min(max(bruto, 0), duracion_ms), extra


def hora_exif(img: Image.Image, nombre_zona: str) -> tuple[datetime | None, str | None]:
    """Hora de la foto según EXIF (con zona) y de qué tag salió. Sin offset se asume la zona local."""
    exif = img.getexif()
    sub = exif.get_ifd(IFD_EXIF)
    candidatos = (
        (sub.get(TAG_FECHA_ORIGINAL), sub.get(TAG_OFFSET_ORIGINAL), sub.get(TAG_SUBSEG_ORIGINAL), "exif"),
        (sub.get(TAG_FECHA_DIGITALIZADA), sub.get(TAG_OFFSET_DIGITALIZADA),
         sub.get(TAG_SUBSEG_DIGITALIZADA), "exif_digitalizada"),
        (exif.get(TAG_FECHA), sub.get(TAG_OFFSET), None, "exif_fecha"),
    )
    for fecha, offset, subseg, origen in candidatos:
        dt = parsear_fecha_exif(fecha, offset, subseg, nombre_zona)
        if dt is not None:
            return dt, origen
    return None, None


def parsear_fecha_exif(fecha: Any, offset: Any = None, subseg: Any = None,
                       nombre_zona: str = "America/Argentina/Buenos_Aires") -> datetime | None:
    """'AAAA:MM:DD HH:MM:SS' (+ '-03:00' y subsegundos opcionales) → datetime con zona.

    None si la fecha no se entiende o el año no es creíble (ANIOS_CREIBLES). Un offset imposible
    ('+25:00') se ignora: queda la zona local, como sin offset."""
    texto = _texto_exif(fecha)
    m = _FECHA_EXIF_RE.match(texto or "")
    if not m:
        return None
    try:
        dt = datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None
    if dt.year not in ANIOS_CREIBLES:
        return None
    sub = re.sub(r"\D", "", _texto_exif(subseg) or "")
    if sub:
        dt = dt.replace(microsecond=int((sub + "000000")[:6]))
    m_off = _OFFSET_EXIF_RE.match(_texto_exif(offset) or "")
    if m_off and int(m_off.group(2)) < 24 and int(m_off.group(3)) < 60:
        signo = -1 if m_off.group(1) == "-" else 1
        delta = timedelta(hours=int(m_off.group(2)), minutes=int(m_off.group(3)))
        return dt.replace(tzinfo=timezone(signo * delta))
    return dt.replace(tzinfo=zona(nombre_zona))


# --- sesión importada desde la app (lo llama verificar) -----------------------------------------

def duracion_declarada(ctx: Contexto, sesion_id: str, declarada: int | None) -> int | None:
    """La duración que mandó el cliente al finalizar una importada (puede ser null).

    verificar pisa sesiones.duracion_ms con la medida: la primera vez guarda la declarada en los datos
    del audio (`duracion_declarada_ms`), así un reproceso desde verificar la sigue informando."""
    with ctx.base.conexion() as conn:
        fila = conn.execute("SELECT datos FROM assets WHERE id = ?", (sesiones.id_audio(sesion_id),)).fetchone()
        if fila is None:
            return declarada
        datos = _datos_json(fila["datos"])
        if "duracion_declarada_ms" in datos:
            return datos["duracion_declarada_ms"]
        datos["duracion_declarada_ms"] = declarada
        conn.execute("UPDATE assets SET datos = ? WHERE id = ?",
                     (json.dumps(datos, ensure_ascii=False), sesiones.id_audio(sesion_id)))
    return declarada


def completar_importada(ctx: Contexto, sesion_id: str, *, duracion_ms: int) -> dict[str, Any]:
    """Lo que verificar suma en una sesión importada, después de medir el audio. Idempotente.

    - Sin pista de audio (un video sin sonido: ffprobe igual le mide la duración) falla acá, sin
      reintentar, como en el importador (_leer_audio), y no en segmentar con la salida de ffmpeg.
    - audio.bin → audio.<ext> según el formato que detecta ffprobe (y `assets.ruta`).
    - Las fotos sin offset_ms que todavía no se miraron (sus datos no tienen `origen_hora`): la hora
      EXIF respecto del inicio de la sesión y de `duracion_ms` (la medida); ubicar_offset recorta y
      marca fuera_de_rango. Sin EXIF (o una HEIC que Pillow no abre) quedan sin hora y marcadas como
      miradas (`origen_hora: null`; la que no se pudo abrir, con el motivo en `ilegible`). El mtime no
      se usa: en el server es el de la subida.
    - audio.panel.ogg (Opus mono, 32 kbps), una vez, si Chrome no reproduce el original. Si ffmpeg
      falla, sólo queda el aviso.
    - importacion.json, si no existe (el que escriben la CLI y el panel no se pisa).

    Los avisos de las fotos (y la línea del log) salen de lo que quedó en la base, no de lo que miró
    esta corrida: así un verificar que se cortó después de ubicarlas los vuelve a dar al reintentar.

    Devuelve {"audio": Path (el final), "avisos": [...], "fotos_miradas": n, "copia_panel": True (está),
    False (falló) o None (no hacía falta)}."""
    cfg = ctx.cfg
    carpeta = cfg.sesion_dir(sesion_id)
    avisos: list[str] = []
    with ctx.base.conexion() as conn:
        s = sesiones.fila_sesion(conn, sesion_id)
        ruta = sesiones.ruta_audio(cfg, conn, sesion_id)
        fila_audio = conn.execute("SELECT datos FROM assets WHERE id = ?", (sesiones.id_audio(sesion_id),)).fetchone()
    datos_audio = _datos_json(fila_audio["datos"] if fila_audio else None)
    try:
        info = audio_mod.sondear(ruta)
    except ErrorAudio as e:
        info = {}
        log.warning("no pude sondear %s: %s", ruta, e)
    if info and not audio_mod.hay_pista_de_audio(info):
        nombre = datos_audio.get("nombre_original") or ruta.name
        raise ErrorEtapa(f"audio ilegible: {nombre} no tiene pista de audio (¿un video sin sonido?)",
                         reintentable=False)

    ruta = _renombrar_bin(ctx, sesion_id, ruta, info, avisos)
    inicio = parsear_iso(s["inicio"], cfg.zona_horaria)
    miradas = _ubicar_fotos_sin_hora(ctx, sesion_id, inicio, duracion_ms)
    copia = _copia_panel(cfg, sesion_id, ruta, info, avisos)

    with ctx.base.conexion() as conn:
        fotos = _fotos_de_sesion(conn, sesion_id)
        ilegibles = _fotos_ilegibles(conn, sesion_id)
    destino_json = carpeta / "importacion.json"
    primera = not destino_json.exists()   # o la anterior se cortó antes de escribirlo
    avisos_fotos: list[str] = []
    if fotos and (miradas or primera):
        fuera = sum(1 for f in fotos if f.get("fuera_de_rango"))
        sin_hora = sum(1 for f in fotos if f["offset_ms"] is None)
        if fuera:
            avisos_fotos.append(f"{fuera} de {len(fotos)} fotos quedaron fuera de la grabación: revisá el inicio")
        if sin_hora:
            avisos_fotos.append(f"{sin_hora} de {len(fotos)} fotos no tienen hora EXIF: van al final, sin minuto")
        if ilegibles:
            avisos_fotos.append(f"{len(ilegibles)} foto(s) no se pudieron leer: quedan sin hora")
        ubicadas = sum(1 for f in fotos if f["offset_ms"] is not None and str(f.get("origen_hora") or "").startswith("exif"))
        ctx.log_sesion(sesion_id, f"importada: {ubicadas} foto(s) ubicadas por EXIF, {sin_hora} sin hora, "
                                  f"{fuera} fuera de rango (inicio {s['inicio']})")
    avisos = avisos_fotos + avisos

    if primera:
        horas = [parsear_iso(f["hora"], cfg.zona_horaria) for f in fotos
                 if f.get("hora") and str(f.get("origen_hora") or "").startswith("exif")]
        candidatos = candidatos_inicio(
            cfg.zona_horaria, _candidatos_audio(cfg.zona_horaria, ruta, duracion_ms, False, None, info),
            horas, duracion_ms)
        escribir_json(destino_json, {
            "audio": {"nombre_original": datos_audio.get("nombre_original") or ruta.name, "archivo": ruta.name,
                      "bytes": sesiones.tamano(ruta), "sha256": s["sha256"], "duracion_ms": duracion_ms,
                      "duracion_declarada_ms": datos_audio.get("duracion_declarada_ms"),
                      "formato": audio_mod.formato(info), "codec": audio_mod.codec_audio(info)},
            "inicio": s["inicio"],
            # El inicio lo dio el cliente: el evento del calendario o lo que se eligió a mano.
            "origen_inicio": "calendario" if s["origen_materia"] == "calendario" else "manual",
            "candidatos_inicio": candidatos,  # sólo informativos
            "fotos": fotos,
            "fotos_ilegibles": ilegibles,
            "avisos": avisos,
            "importado": ahora_iso(cfg.zona_horaria),
            "via": "app",
        })
    return {"audio": ruta, "avisos": avisos, "fotos_miradas": miradas, "copia_panel": copia}


def reconciliar_audio(ctx: Contexto, sid: str) -> Path:
    """El audio de una importada, con `assets.ruta` corregida si la base y el disco no coinciden.

    _renombrar_bin cambia la base y después renombra el archivo: si el server muere entre las dos (o un
    corte de luz pierde el commit y el rename no), la base dice audio.m4a y en disco está audio.bin, o
    al revés. Si el de la base no está y el otro sí (uno solo), la base pasa a decir el que está y
    verificar sigue (_renombrar_bin lo vuelve a intentar). Lo llama verificar antes de mirar el audio."""
    cfg = ctx.cfg
    with sesiones.lock_sesion(sid), ctx.base.conexion() as conn:
        ruta = sesiones.ruta_audio(cfg, conn, sid)
        if ruta.is_file():
            return ruta
        if ruta.suffix.lower() == ".bin":
            # audio.<ext> como lo arma _renombrar_bin: ni audio.panel.ogg ni un audio.descartado-….
            otros = [p for p in ruta.parent.glob("audio.*")
                     if _AUDIO_RENOMBRADO_RE.fullmatch(p.name.lower()) and p.is_file()]
        else:
            otros = [p for p in (ruta.with_name("audio.bin"),) if p.is_file()]
        if len(otros) != 1:
            return ruta
        real = otros[0]
        conn.execute("UPDATE assets SET ruta = ? WHERE id = ?", (real.name, sesiones.id_audio(sid)))
    ctx.log_sesion(sid, f"importada: la base decía {ruta.name} pero el audio está como {real.name}: corregido")
    return real


def _renombrar_bin(ctx: Contexto, sid: str, ruta: Path, info: dict[str, Any], avisos: list[str]) -> Path:
    """audio.bin → audio.<ext> del formato que detecta ffprobe (si no lo sabe, queda .bin). Si se corta
    entre la base y el disco, reconciliar_audio lo arregla en el próximo verificar."""
    if ruta.suffix.lower() != ".bin":
        return ruta
    extension = audio_mod.extension_por_formato(info)
    if not extension:
        avisos.append("no sé de qué formato es el audio: queda como audio.bin")
        return ruta
    destino = ruta.with_name("audio" + extension)
    with sesiones.lock_sesion(sid):
        if destino.exists():
            avisos.append(f"ya hay un {destino.name} en la carpeta de la sesión: el audio queda como {ruta.name}")
            return ruta
        with ctx.base.conexion() as conn:
            conn.execute("UPDATE assets SET ruta = ? WHERE id = ?", (destino.name, sesiones.id_audio(sid)))
            try:
                os.replace(ruta, destino)
            except OSError:
                conn.execute("UPDATE assets SET ruta = ? WHERE id = ?", (ruta.name, sesiones.id_audio(sid)))
                raise
    ctx.log_sesion(sid, f"importada: el audio era {audio_mod.formato(info)}: {ruta.name} → {destino.name}")
    return destino


def _ubicar_fotos_sin_hora(ctx: Contexto, sid: str, inicio: datetime, duracion_ms: int) -> int:
    """Ubica por EXIF las fotos sin offset que todavía no se miraron. Devuelve cuántas miró. La que no
    se puede abrir queda sin hora, con el motivo en `ilegible` (lo lee _fotos_ilegibles)."""
    cfg = ctx.cfg
    carpeta = cfg.sesion_dir(sid)
    with ctx.base.conexion() as conn:
        filas = conn.execute(
            "SELECT id, ruta, datos FROM assets WHERE sesion_id = ? AND tipo = 'foto' AND offset_ms IS NULL "
            "ORDER BY creado, id", (sid,)).fetchall()
    miradas = 0
    for f in filas:
        datos = _datos_json(f["datos"])
        if "origen_hora" in datos:
            continue
        ruta = Path(f["ruta"] or "")
        hora, origen = None, None
        try:
            with Image.open(carpeta / ruta) as img:
                hora, origen = hora_exif(img, cfg.zona_horaria)
        except Exception as e:  # HEIC sin soporte, archivo roto o que falta: queda sin hora
            datos["ilegible"] = _motivo_foto(ruta, e)
        offset, extra = (None, {}) if hora is None else ubicar_offset(hora, inicio, duracion_ms)
        datos.update({"origen_hora": origen, "hora": hora.isoformat(timespec="seconds") if hora else None, **extra})
        with ctx.base.conexion() as conn:
            conn.execute("UPDATE assets SET offset_ms = ?, datos = ? WHERE id = ? AND offset_ms IS NULL",
                         (offset, json.dumps(datos, ensure_ascii=False), f["id"]))
        miradas += 1
    return miradas


def _fotos_ilegibles(conn: Any, sid: str) -> list[dict[str, str]]:
    """Las fotos que verificar no pudo abrir (las marcó _ubicar_fotos_sin_hora), como en importacion.json."""
    ilegibles = []
    for f in conn.execute("SELECT id, ruta, datos FROM assets WHERE sesion_id = ? AND tipo = 'foto' "
                          "ORDER BY creado, id", (sid,)):
        datos = _datos_json(f["datos"])
        if datos.get("ilegible"):
            ilegibles.append({"nombre": datos.get("nombre_original") or Path(f["ruta"] or "").name or f["id"],
                              "motivo": str(datos["ilegible"])})
    return ilegibles


def _fotos_de_sesion(conn: Any, sid: str) -> list[dict[str, Any]]:
    """Las fotos de la sesión como en importacion.json (las que mandó el cliente con minuto: 'cliente')."""
    filas = conn.execute(
        "SELECT id, offset_ms, datos FROM assets WHERE sesion_id = ? AND tipo = 'foto' "
        "ORDER BY offset_ms IS NULL, offset_ms, creado, id", (sid,)).fetchall()
    fotos = []
    for f in filas:
        datos = _datos_json(f["datos"])
        foto = {"id": f["id"], "nombre_original": datos.get("nombre_original"), "hora": datos.get("hora"),
                "origen_hora": datos["origen_hora"] if "origen_hora" in datos
                else ("cliente" if f["offset_ms"] is not None else None),
                "offset_ms": f["offset_ms"]}
        for clave in ("fuera_de_rango", "offset_original_ms"):
            if clave in datos:
                foto[clave] = datos[clave]
        fotos.append(foto)
    return fotos


def _copia_panel(cfg: Config, sid: str, ruta: Path, info: dict[str, Any], avisos: list[str]) -> bool | None:
    """audio.panel.ogg si el navegador no reproduce el original (se arma una sola vez)."""
    if audio_mod.reproducible_en_navegador(ruta, info):
        return None
    copia = cfg.sesion_dir(sid) / sesiones.AUDIO_PANEL
    if copia.is_file():
        return True
    try:
        audio_mod.convertir_opus(ruta, copia)
    except ErrorAudio as e:
        log.warning("no pude armar %s para %s: %s", copia.name, sid[:8], e)
        avisos.append(f"no pude armar la copia del audio para el reproductor del panel ({e})")
        return False
    return True


# --- audio e inicio ------------------------------------------------------------------------------

def _chequear_tamano(audio: Path) -> int:
    total = audio.stat().st_size
    if total > MAX_AUDIO_IMPORTADO:
        raise AudioGrande(sesiones.mensaje_audio_grande(total))
    return total


def _leer_audio(audio: Path, nombre: str) -> tuple[dict[str, Any], int]:
    try:
        info = audio_mod.sondear(audio)
        if not audio_mod.hay_pista_de_audio(info):
            raise ErrorAudio("no tiene pista de audio")
        dur_ms = audio_mod.duracion_ms(audio)
    except ErrorAudio as e:
        raise ErrorImportacion(f"{nombre} no es un audio legible ({e})") from e
    if dur_ms <= 0:
        raise ErrorImportacion(f"{nombre} no es un audio legible (duración 0)")
    return info, dur_ms


def _candidatos_audio(
    nombre_zona: str,
    audio: Path,
    dur_ms: int,
    usar_mtime_audio: bool,
    mtime_audio: datetime | None = None,
    info: dict[str, Any] | None = None,
) -> list[tuple[datetime, str]]:
    """Lo que dice el audio sobre su inicio, en orden: la fecha del archivo − duración (`mtime_audio`
    o, con `usar_mtime_audio`, el mtime del archivo: la grabadora de Samsung escribe al terminar),
    creation_time y creation_time − duración (MediaRecorder lo escribe al cerrar)."""
    tz = zona(nombre_zona)
    duracion = timedelta(milliseconds=dur_ms)
    candidatos: list[tuple[datetime, str]] = []
    if mtime_audio is not None:
        fin = mtime_audio if mtime_audio.tzinfo else mtime_audio.replace(tzinfo=tz)
        candidatos.append((fin - duracion, "mtime_audio"))
    elif usar_mtime_audio:
        try:
            candidatos.append((datetime.fromtimestamp(audio.stat().st_mtime, tz) - duracion, "mtime_audio"))
        except (OSError, OverflowError, ValueError):
            pass
    creacion = audio_mod.fecha_creacion(audio, info)
    if creacion:
        try:
            dt = parsear_iso(creacion, nombre_zona)
        except ValueError:
            log.warning("creation_time ilegible en %s: %r", audio.name, creacion)
        else:
            if dt.year in ANIOS_CREIBLES:
                candidatos += [(dt, "creation_time"), (dt - duracion, "creation_time_como_fin")]
    return candidatos


def _inferir_inicio(
    nombre_zona: str,
    audio: Path,
    dur_ms: int,
    inicio: datetime | str | None,
    usar_mtime_audio: bool,
    horas_fotos: list[datetime],
    *,
    mtime_audio: datetime | None = None,
    info: dict[str, Any] | None = None,
) -> tuple[datetime, str, list[dict[str, Any]]]:
    """Inicio de la grabación, de dónde salió y los candidatos que se miraron.

    Orígenes: parametro | mtime_audio | creation_time | creation_time_como_fin | exif_fotos |
    ahora_menos_duracion. Los candidatos del audio salen de _candidatos_audio. Sin fotos con hora
    EXIF gana el primero que haya (salvo el "como fin"). Con fotos, el que deja más fotos dentro de la
    grabación; si ninguno deja adentro a la mayoría, el inicio sale de las fotos (1 min antes de la
    primera). Es la prioridad de la condición 4 del diseño: lo que da el usuario, después las fotos (o un
    metadato del audio que las fotos confirman) y por último los metadatos del audio solos.
    """
    tz = zona(nombre_zona)
    duracion = timedelta(milliseconds=dur_ms)
    if inicio is not None:
        if isinstance(inicio, str):
            try:
                inicio = parsear_iso(inicio, nombre_zona)
            except ValueError as e:
                raise ErrorImportacion(f"inicio inválido: {inicio!r}") from e
        return (inicio if inicio.tzinfo else inicio.replace(tzinfo=tz)), "parametro", []
    candidatos = _candidatos_audio(nombre_zona, audio, dur_ms, usar_mtime_audio, mtime_audio, info)

    if horas_fotos:
        contados = [(dt, origen, _fotos_dentro(horas_fotos, dt, dur_ms)) for dt, origen in candidatos]
        detalle = [{"origen": o, "inicio": dt.astimezone(tz).isoformat(timespec="seconds"), "fotos_dentro": k}
                   for dt, o, k in contados]
        mejor = max(contados, key=lambda c: c[2], default=None)  # ante empate, el primero
        if mejor is not None and 2 * mejor[2] > len(horas_fotos):
            return mejor[0], mejor[1], detalle
        return _inicio_por_fotos(horas_fotos, dur_ms), "exif_fotos", detalle
    detalle = [{"origen": o, "inicio": dt.astimezone(tz).isoformat(timespec="seconds")} for dt, o in candidatos]
    for dt, origen in candidatos:
        if origen != "creation_time_como_fin":
            return dt, origen, detalle
    log.warning("no sé cuándo empezó %s: uso ahora − duración", audio.name)
    return datetime.now(tz) - duracion, "ahora_menos_duracion", detalle


def candidatos_inicio(
    nombre_zona: str,
    candidatos_audio: list[tuple[datetime, str]],
    horas_fotos: list[datetime],
    dur_ms: int,
    *,
    elegido: str | None = None,
) -> list[dict[str, Any]]:
    """Los candidatos de inicio para mostrar, en el orden de prioridad de la condición 4 del diseño.

    Primero lo que sale de las fotos: el EXIF (1 min antes de la primera) y los metadatos del audio que
    las fotos confirman (dejan adentro a la mayoría). Después los metadatos del audio solos, con la
    advertencia. El `elegido` (el origen que propone _inferir_inicio) va primero. Cada uno:
    {"origen", "inicio" (ISO), "etiqueta", "elegido", "fotos_dentro" (si hay fotos con EXIF)}."""
    tz = zona(nombre_zona)
    n = len(horas_fotos)
    confirmados: list[dict[str, Any]] = []
    solos: list[dict[str, Any]] = []
    for dt, origen in candidatos_audio:
        k = _fotos_dentro(horas_fotos, dt, dur_ms) if n else 0
        confirmado = bool(n) and 2 * k > n
        (confirmados if confirmado else solos).append(
            _candidato(tz, dt, origen, confirmado, k, n, elegido=origen == elegido))
    if n:
        dt = _inicio_por_fotos(horas_fotos, dur_ms)
        c = _candidato(tz, dt, "exif_fotos", True, _fotos_dentro(horas_fotos, dt, dur_ms), n,
                       elegido=elegido == "exif_fotos")
        if dt + timedelta(minutes=1) != min(horas_fotos):
            c["etiqueta"] = "Según las fotos (EXIF): 1 min antes de la primera de las que caen juntas (hay alguna suelta)"
        confirmados.append(c)
    todos = confirmados + solos
    return sorted(todos, key=lambda c: not c["elegido"])  # sorted es estable: el resto queda en su orden


def _candidato(tz: Any, dt: datetime, origen: str, confirmado: bool | None, k: int, n: int, *,
               elegido: bool) -> dict[str, Any]:
    etiqueta = ETIQUETAS_INICIO[origen]
    if origen in ORIGENES_AUDIO:
        if confirmado:
            etiqueta += f" (lo confirman {k} de {n} fotos)"
        elif n:
            etiqueta += f": deja {k} de {n} fotos adentro; {AVISO_ORIGEN[origen]}"
        else:
            etiqueta += f": {AVISO_ORIGEN[origen]}"
    c: dict[str, Any] = {"origen": origen, "inicio": dt.astimezone(tz).isoformat(timespec="seconds"),
                         "etiqueta": etiqueta, "elegido": elegido}
    if n:
        c["fotos_dentro"] = k
    return c


def _inicio_por_fotos(horas: list[datetime], dur_ms: int) -> datetime:
    """1 min antes de la primera foto; si hay alguna suelta lejos (otro día), 1 min antes de la
    primera desde la que entran más fotos en la grabación."""
    opciones = [h - timedelta(minutes=1) for h in sorted(horas)]
    return max(opciones, key=lambda dt: _fotos_dentro(horas, dt, dur_ms))


def _fotos_dentro(horas: list[datetime], inicio: datetime, dur_ms: int) -> int:
    return sum(1 for h in horas if not ubicar_offset(h, inicio, dur_ms)[1])


def _extension_audio(audio: Path, nombre_original: str | None, info: dict[str, Any]) -> str:
    """La extensión del nombre (el original o el del archivo) si está en la lista blanca; si no, la del
    formato que detecta ffprobe; si no, .bin."""
    for candidato in (nombre_original, audio.name):
        extension = audio_mod.extension_valida(candidato)
        if extension:
            return extension
    return audio_mod.extension_por_formato(info) or ".bin"


# --- fotos ---------------------------------------------------------------------------------------

def _leer_fotos(rutas: list[Path], nombre_zona: str, usar_mtime: bool) -> tuple[list[_Foto], list[dict[str, str]]]:
    """Hora de cada foto. Las que Pillow no puede abrir se avisan y se saltean."""
    tz = zona(nombre_zona)
    leidas: list[_Foto] = []
    ilegibles: list[dict[str, str]] = []
    for indice, ruta in enumerate(rutas):
        try:
            with Image.open(ruta) as img:
                hora, origen = hora_exif(img, nombre_zona)
                img.draft("RGB", (max(1, img.width // 8), max(1, img.height // 8)))
                img.load()  # detecta archivos cortados o que no son imágenes
        except Exception as e:  # UnidentifiedImageError, OSError, DecompressionBombError, …
            motivo = _motivo_foto(ruta, e)
            log.warning("foto ilegible, la salteo: %s (%s)", ruta.name, motivo)
            ilegibles.append({"nombre": ruta.name, "motivo": motivo})
            continue
        if hora is None and usar_mtime:
            try:
                hora, origen = datetime.fromtimestamp(ruta.stat().st_mtime, tz), "mtime"
            except (OSError, OverflowError, ValueError):
                hora, origen = None, None
        leidas.append(_Foto(ruta=ruta, indice=indice, hora=hora, origen_hora=origen))
    return leidas, ilegibles


def _motivo_foto(ruta: Path, e: Exception) -> str:
    return "formato no soportado (¿HEIC?)" if ruta.suffix.lower() == ".heic" else str(e) or type(e).__name__


def _texto_exif(valor: Any) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, bytes):
        valor = valor.decode("ascii", errors="ignore")
    return str(valor).replace("\x00", "").strip() or None


# --- material ------------------------------------------------------------------------------------

def _guardar_material(ctx: Contexto, sid: str, material: Sequence[tuple[Path, str]], origen: str
                      ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Los PDFs e imágenes como adjuntos de la sesión. Lo que no entra (tipo, tamaño, vacío) se informa."""
    entro: list[dict[str, Any]] = []
    rechazado: list[dict[str, str]] = []
    for ruta, nombre in material:
        nombre = (nombre or "").strip() or Path(ruta).name
        try:
            _, info = adjuntos.guardar(ctx.cfg, ctx.base, sid, adjunto_id=str(uuid.uuid4()), archivo=Path(ruta),
                                       nombre_original=nombre, origen=origen)
        except (ArchivoGrande, adjuntos.TipoNoSoportado, sesiones.ErrorDatos, OSError) as e:
            motivo = (e.strerror or str(e)) if isinstance(e, OSError) else str(e)
            log.warning("material salteado: %s (%s)", nombre, motivo)
            rechazado.append({"nombre": nombre, "motivo": motivo or type(e).__name__})
            continue
        entro.append({k: info[k] for k in ("id", "nombre", "nombre_original", "tipo", "bytes")})
    return entro, rechazado


# --- área temporal del panel ---------------------------------------------------------------------

def dir_temporal(cfg: Config) -> Path:
    """Classmer-data/importaciones/: lo que el panel sube para analizar antes de importar."""
    carpeta = cfg.datos / DIR_TEMPORAL
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def crear_temporal(cfg: Config) -> tuple[str, Path]:
    """Una carpeta nueva en el área temporal: (token, ruta). Al navegador sólo viaja el token."""
    token = secrets.token_hex(16)
    carpeta = dir_temporal(cfg) / token
    carpeta.mkdir()
    return token, carpeta


def token_valido(token: Any) -> bool:
    """¿Tiene forma de token del área temporal (32 hex en minúsculas)? Nada de '..' ni barras."""
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


def ruta_temporal(cfg: Config, token: str) -> Path:
    """La carpeta de un token. ErrorImportacion si el token no es válido o ya no está (venció o se borró)."""
    if not token_valido(token):
        raise ErrorImportacion("análisis inválido")
    carpeta = dir_temporal(cfg) / token
    if not carpeta.is_dir():
        raise ErrorImportacion("el análisis ya no está (venció o se canceló): volvé a subir los archivos")
    return carpeta


class ArchivoTemporal:
    """Un archivo que entra al área temporal, en <token>/<tipo>/NNN/<nombre saneado>, escrito de a
    pedazos con el tope de su tipo: audio 2 GiB (AudioGrande), fotos 50 MB y material 100 MB
    (ArchivoGrande). Lo que pasa el tope no queda en disco. Lo usan guardar_temporal y el panel, que
    escribe el multipart mientras llega (así nada pasa por la carpeta temporal del sistema)."""

    def __init__(self, cfg: Config, token: str, tipo: str, nombre: str | None):
        if tipo not in TOPES_TEMPORAL:
            raise ValueError(f"tipo inválido: {tipo!r}")
        carpeta = ruta_temporal(cfg, token) / tipo
        carpeta.mkdir(exist_ok=True)
        n = sum(1 for _ in carpeta.iterdir())
        while True:
            try:
                (carpeta / f"{n:03d}").mkdir()
                break
            except FileExistsError:
                n += 1
        self.tipo = tipo
        self.ruta = carpeta / f"{n:03d}" / _nombre_temporal(nombre, n)
        self.maximo = TOPES_TEMPORAL[tipo]
        self.bytes = 0
        try:
            self._f = open(self.ruta, "xb")
        except BaseException:
            shutil.rmtree(self.ruta.parent, ignore_errors=True)
            raise

    def escribir(self, datos: bytes) -> None:
        self.bytes += len(datos)
        if self.bytes > self.maximo:
            self.descartar()
            raise (AudioGrande(sesiones.mensaje_audio_grande()) if self.tipo == "audio" else
                   ArchivoGrande(f"{self.ruta.name} pesa más de {self.maximo // (1024 * 1024)} MB"))
        self._f.write(datos)

    def cerrar(self) -> Path:
        self._f.close()
        return self.ruta

    def descartar(self) -> None:
        """Lo borra (también a medio escribir). Se puede llamar más de una vez."""
        self._f.close()
        shutil.rmtree(self.ruta.parent, ignore_errors=True)


def guardar_temporal(cfg: Config, token: str, tipo: str, archivo: Any, nombre: str | None) -> Path:
    """Guarda un archivo subido (un stream con .read(n) o un Path) en <token>/<tipo>/NNN/<nombre saneado>.

    `tipo`: audio (tope 2 GiB: AudioGrande), fotos (50 MB) o material (100 MB): ArchivoGrande. Lo que
    pasa el tope no queda en disco."""
    fuente = open(archivo, "rb") if isinstance(archivo, (str, Path)) else archivo
    try:
        destino = ArchivoTemporal(cfg, token, tipo, nombre)
        try:
            while chunk := fuente.read(1024 * 1024):
                destino.escribir(chunk)
        except BaseException:
            destino.descartar()
            raise
    finally:
        if isinstance(archivo, (str, Path)):
            fuente.close()
    return destino.cerrar()


def borrar_temporal(cfg: Config, token: str) -> bool:
    """Borra un análisis (al cancelar o después de importar). False si el token no es válido o no estaba."""
    try:
        carpeta = ruta_temporal(cfg, token)
    except ErrorImportacion:
        return False
    shutil.rmtree(carpeta, ignore_errors=True)
    return True


def limpiar_temporales(cfg: Config, vencimiento_h: float = VENCIMIENTO_TEMPORAL_H) -> int:
    """Borra lo abandonado del área temporal: lo que no se tocó en `vencimiento_h` horas, y las carpetas
    importar-* que haya dejado un POST /api/importar cortado (de antes del área temporal). Lo llaman el
    arranque del server y el mantenimiento del worker. Devuelve cuántas entradas borró."""
    limite = time.time() - vencimiento_h * 3600
    entradas: list[Path] = []
    raiz = cfg.datos / DIR_TEMPORAL
    if raiz.is_dir():
        entradas += list(raiz.iterdir())
    if cfg.datos.is_dir():
        entradas += [p for p in cfg.datos.glob("importar-*") if p.is_dir()]
    borradas = 0
    for p in entradas:
        try:
            if _ultima_modificacion(p) >= limite:
                continue
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
        except OSError as e:
            log.warning("no pude borrar %s del área temporal: %s", p.name, e)
            continue
        borradas += 1
    if borradas:
        log.info("área temporal: borré %d importación(es) abandonada(s)", borradas)
    return borradas


def _ultima_modificacion(ruta: Path) -> float:
    ultima = ruta.lstat().st_mtime
    if ruta.is_dir() and not ruta.is_symlink():
        for raiz, carpetas, archivos in os.walk(ruta):
            for nombre in (*carpetas, *archivos):
                try:
                    ultima = max(ultima, os.lstat(os.path.join(raiz, nombre)).st_mtime)
                except OSError:
                    pass
    return ultima


def _nombre_temporal(nombre: str | None, n: int) -> str:
    """El nombre original válido en Windows (sin carpetas, sin <>:"/\\|?*), con su extensión."""
    original = re.split(r"[\\/]", nombre or "")[-1]
    extension = Path(original).suffix
    if not extension or nombre_seguro(extension) != extension or len(extension) > 10:
        extension = ""
    raiz = Path(original).stem if extension else original
    limpio = nombre_seguro(raiz, 100) if raiz.strip(" .") else ""
    if not limpio or limpio == "Sin titulo":
        limpio = f"archivo_{n:03d}"
    if limpio.split(".")[0].casefold() in _RESERVADOS_WIN:
        limpio += "_"
    return limpio + extension


# --- varios --------------------------------------------------------------------------------------

def _datos_json(texto: str | None) -> dict[str, Any]:
    try:
        datos = json.loads(texto) if texto else {}
    except ValueError:
        return {}
    return datos if isinstance(datos, dict) else {}


def _traer_audio(origen: Path, destino: Path, mover: bool) -> bool:
    """El audio a la carpeta de la sesión: con `mover`, os.replace (mismo disco: no ocupa el doble ni
    tarda); si no se puede, o sin `mover`, una copia atómica. Devuelve si lo movió."""
    if mover:
        destino.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(origen, destino)
            return True
        except OSError as e:
            log.info("no pude mover %s a la sesión (%s): lo copio", origen.name, e)
    _copiar_atomico(origen, destino)
    return False


def _copiar_atomico(origen: Path, destino: Path) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(f".{destino.name}.tmp")
    try:
        shutil.copyfile(origen, tmp)
        os.replace(tmp, destino)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _avisos_inicio(origen: str, fotos: list[dict[str, Any]]) -> list[str]:
    avisos = []
    if origen == "exif_fotos":
        avisos.append("el audio no dice cuándo empezó (o no coincide con las fotos): "
                      "el inicio es 1 min antes de la primera foto")
    elif origen == "ahora_menos_duracion":
        avisos.append("no sé cuándo empezó la grabación: el inicio es ahora − duración")
    fuera = sum(1 for f in fotos if f.get("fuera_de_rango"))
    if fuera:
        avisos.append(f"{fuera} de {len(fotos)} fotos quedaron fuera de la grabación")
    if avisos and origen != "parametro":
        avisos.append("si está mal, reimportá con --inicio")
    return avisos


def _avisos_analisis(origen: str, hay_exif: bool, fotos: list[dict[str, Any]]) -> list[str]:
    """Los avisos del paso "Analizar" (el panel deja corregir el inicio antes de importar)."""
    avisos = []
    if origen == "exif_fotos":
        avisos.append("el audio no dice cuándo empezó (o no coincide con las fotos): "
                      "el inicio propuesto es 1 min antes de la primera foto")
    elif origen in ORIGENES_AUDIO and not hay_exif:
        avisos.append("el inicio sale sólo de los metadatos del audio: si vino por WhatsApp, "
                      "suele ser la hora del envío; revisalo")
    elif origen == "ahora_menos_duracion":
        avisos.append("no sé cuándo empezó la grabación: poné el inicio a mano")
    fuera = sum(1 for f in fotos if f["fuera_de_rango"])
    if fuera:
        avisos.append(f"{fuera} de {len(fotos)} fotos quedan fuera de la grabación: revisá el inicio")
    sin_hora = sum(1 for f in fotos if f["hora"] is None)
    if sin_hora:
        avisos.append(f"{sin_hora} de {len(fotos)} fotos no tienen hora EXIF: van al final, sin minuto")
    return avisos


def _registrar_en_log(ctx: Contexto, sid: str, r: dict[str, Any]) -> None:
    fotos = r["fotos"]
    fuera = sum(1 for f in fotos if f.get("fuera_de_rango"))
    sin_hora = sum(1 for f in fotos if f["offset_ms"] is None)
    a = r["audio"]
    ctx.log_sesion(
        sid,
        f"importado: {a['nombre_original']} ({a['duracion_ms'] // 1000} s, {a['bytes']} bytes); "
        f"inicio {r['inicio']} (sale de: {r['origen_inicio']}); {len(fotos)} fotos "
        f"({fuera} fuera de rango, {sin_hora} sin hora, {len(r['fotos_ilegibles'])} ilegibles)",
    )
    for f in r["fotos_ilegibles"]:
        ctx.log_sesion(sid, f"foto salteada: {f['nombre']} ({f['motivo']})")
    if r.get("material") or r.get("material_rechazado"):
        ctx.log_sesion(sid, f"importado: material: {len(r['material'])} adjunto(s), "
                            f"{len(r['material_rechazado'])} rechazado(s)")
    for m in r.get("material_rechazado") or []:
        ctx.log_sesion(sid, f"material salteado: {m['nombre']} ({m['motivo']})")
    if r.get("avisos"):
        ctx.log_sesion(sid, "importado: aviso: " + "; ".join(r["avisos"]))
    if fuera:
        log.warning("%d de %d fotos quedaron fuera de la grabación (inicio %s, sale de %s): "
                    "si está mal, reimportá con --inicio", fuera, len(fotos), r["inicio"], r["origen_inicio"])


def _deshacer(ctx: Contexto, sid: str, mensaje: str, *, devolver: tuple[Path, Path] | None = None) -> None:
    """La importación se cortó antes de finalizar: borra la sesión a medio crear (sus filas y su carpeta).

    Así no queda una clase en error sin audio que el panel no deja borrar (fuera de la materia de
    prueba) ni se gasta su número, y reintentar no suma otra. `devolver` = (destino, origen): el audio
    que se movió vuelve a su lugar antes de borrar la carpeta. Una sesión que ya salió de `grabando` no
    se toca; si no se puede borrar, queda en error (_marcar_error), como antes."""
    try:
        with ctx.base.transaccion() as conn:
            fila = conn.execute("SELECT estado FROM sesiones WHERE id = ?", (sid,)).fetchone()
            if fila is None or fila["estado"] != "grabando":
                return
            # Antes de finalizar sólo hay assets (audio, fotos, material): ni jobs ni apuntes.
            conn.execute("DELETE FROM assets WHERE sesion_id = ?", (sid,))
            conn.execute("DELETE FROM jobs WHERE sesion_id = ?", (sid,))
            conn.execute("DELETE FROM sesiones WHERE id = ?", (sid,))
        borrada = True
    except Exception:
        log.exception("no pude borrar la sesión a medio importar %s: queda en error", sid)
        borrada = False
    if devolver is not None:
        try:
            os.replace(*devolver)
        except OSError as e:
            log.warning("no pude devolver el audio de la importación fallida %s: %s", sid[:8], e)
    if not borrada:
        _marcar_error(ctx, sid, mensaje)
        return
    shutil.rmtree(ctx.cfg.sesion_dir(sid), ignore_errors=True)
    log.warning("importación fallida (%s): borré la sesión a medio crear %s", mensaje, sid[:8])


def _marcar_error(ctx: Contexto, sid: str, mensaje: str) -> None:
    """Si la importación se corta a la mitad y la sesión no se puede borrar, queda en error (no colgada
    en grabando)."""
    try:
        with ctx.base.conexion() as conn:
            fila = conn.execute("SELECT estado FROM sesiones WHERE id = ?", (sid,)).fetchone()
            if fila is not None and fila["estado"] == "grabando":
                actualizar_sesion(conn, sid, ctx.cfg.zona_horaria, estado="error",
                                  error=f"importación: {mensaje}"[:500])
        ctx.log_sesion(sid, f"importación fallida: {mensaje}")
    except Exception:
        log.exception("no pude marcar la sesión %s como error", sid)
