"""Helpers de ffmpeg/ffprobe (los usan segmentar, verificar y el importador)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


class ErrorAudio(RuntimeError):
    """ffmpeg/ffprobe falló o el archivo no es un audio legible."""


# --- formatos (audio importado) -------------------------------------------------------------

# Extensiones que se aceptan del nombre original de un audio importado (§5.1). Con otra, el server
# lo guarda como audio.bin y verificar lo renombra según el formato que detecta ffprobe.
EXTENSIONES_AUDIO = (
    ".ogg", ".opus", ".oga", ".m4a", ".mp4", ".m4b", ".aac", ".mp3", ".wav", ".flac", ".webm", ".weba",
    ".mka", ".mkv", ".amr", ".awb", ".3gp", ".3ga", ".wma", ".aif", ".aiff", ".caf",
)

# format_name de ffprobe → extensión (un audio.bin, o un archivo que llegó sin extensión).
EXTENSION_POR_FORMATO = {
    "ogg": ".ogg",
    "mov,mp4,m4a,3gp,3g2,mj2": ".m4a",
    "wav": ".wav",
    "mp3": ".mp3",
    "flac": ".flac",
    "matroska,webm": ".webm",
    "aac": ".aac",
    "amr": ".amr",
    "asf": ".wma",
    "aiff": ".aiff",
    "caf": ".caf",
}

# Lo que Chrome y Edge reproducen en <audio>: el contenedor por la extensión (con el tipo con que se
# sirve) y el códec que informa ffprobe. amr, wma, alac y similares no: el panel usa una copia en Opus.
MIME_AUDIO = {".ogg": "audio/ogg", ".opus": "audio/ogg", ".oga": "audio/ogg", ".m4a": "audio/mp4",
              ".mp4": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".webm": "audio/webm",
              ".aac": "audio/aac", ".flac": "audio/flac"}
CODECS_NAVEGADOR = ("opus", "vorbis", "aac", "mp3", "flac")

_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,5}$")


def extension_valida(nombre: str | None) -> str | None:
    """La extensión del nombre (en minúsculas) si está en EXTENSIONES_AUDIO; si no, None."""
    extension = Path(re.split(r"[\\/]", nombre or "")[-1]).suffix.lower()
    return extension if extension in EXTENSIONES_AUDIO else None


def formato(info: dict[str, Any]) -> str | None:
    return (info.get("format") or {}).get("format_name") or None


def codec_audio(info: dict[str, Any]) -> str | None:
    """codec_name de la primera pista de audio, según ffprobe."""
    for s in info.get("streams") or []:
        if s.get("codec_type") == "audio":
            return s.get("codec_name") or None
    return None


def extension_por_formato(info: dict[str, Any]) -> str | None:
    """La extensión que corresponde al contenedor que detectó ffprobe (None si no se sabe)."""
    nombre = str(formato(info) or "")
    if nombre in EXTENSION_POR_FORMATO:
        return EXTENSION_POR_FORMATO[nombre]
    primero = "." + nombre.split(",")[0].lower() if nombre else ""
    return primero if _EXTENSION_RE.match(primero) and primero != ".bin" else None


def reproducible_en_navegador(ruta: Path, info: dict[str, Any]) -> bool:
    """Si el reproductor del panel (Chrome o Edge) puede reproducir el archivo tal cual."""
    if ruta.suffix.lower() not in MIME_AUDIO:
        return False
    codec = codec_audio(info) or ""
    return codec in CODECS_NAVEGADOR or codec.startswith("pcm_")


def _programa(nombre: str) -> str:
    ruta = shutil.which(nombre)
    if ruta is None:
        raise ErrorAudio(f"no encuentro {nombre} en el PATH")
    return ruta


def ejecutar(args: list[str], *, timeout: float | None = 3600) -> subprocess.CompletedProcess[str]:
    """Corre ffmpeg/ffprobe sin consola y decodificando la salida como UTF-8."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            [_programa(args[0]), *args[1:]],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=flags, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ErrorAudio(f"{args[0]} tardó demasiado") from e


def sondear(ruta: Path) -> dict[str, Any]:
    """`ffprobe -show_format -show_streams` como dict."""
    r = ejecutar(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(ruta)],
                 timeout=120)
    if r.returncode != 0:
        raise ErrorAudio(f"ffprobe no pudo leer {ruta.name}: {r.stderr.strip()[-500:]}")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise ErrorAudio(f"ffprobe devolvió algo raro para {ruta.name}") from e


def duracion_ms(ruta: Path) -> int:
    """Duración del audio en ms. Si el contenedor no la declara (Ogg cortado), la mide decodificando."""
    info = sondear(ruta)
    dur = (info.get("format") or {}).get("duration")
    if dur in (None, "N/A"):
        for s in info.get("streams") or []:
            if s.get("codec_type") == "audio" and s.get("duration") not in (None, "N/A"):
                dur = s["duration"]
                break
    if dur not in (None, "N/A"):
        try:
            return int(round(float(dur) * 1000))
        except ValueError:
            pass
    return duracion_decodificando_ms(ruta)


def duracion_decodificando_ms(ruta: Path) -> int:
    r = ejecutar(["ffmpeg", "-hide_banner", "-nostats", "-i", str(ruta), "-f", "null", "-"])
    ultimo = None
    for linea in r.stderr.splitlines():
        if "time=" in linea:
            ultimo = linea.split("time=")[-1].split()[0]
    if ultimo is None:
        raise ErrorAudio(f"no pude medir la duración de {ruta.name}")
    from classmer.util import tiempo_a_ms

    return tiempo_a_ms(ultimo)


def hay_pista_de_audio(info: dict[str, Any]) -> bool:
    """Si lo que devolvió `sondear` tiene alguna pista de audio (un video sin sonido no la tiene)."""
    return any(s.get("codec_type") == "audio" for s in info.get("streams") or [])


def tiene_audio(ruta: Path) -> bool:
    try:
        return hay_pista_de_audio(sondear(ruta))
    except ErrorAudio:
        return False


def fecha_creacion(ruta: Path, info: dict[str, Any] | None = None) -> str | None:
    """Tag creation_time del contenedor (m4a/mp4 suelen tenerlo), si existe. Con `info` (lo que ya
    devolvió `sondear`) no vuelve a llamar a ffprobe."""
    if info is None:
        try:
            info = sondear(ruta)
        except ErrorAudio:
            return None
    tags = (info.get("format") or {}).get("tags") or {}
    for clave in ("creation_time", "date"):
        for k, v in tags.items():
            if k.lower() == clave and v:
                return str(v)
    return None


# --- segmentar: silencios y cortes precisos -------------------------------------------------

_SILENCIO_INICIO_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?(?:e[-+]?\d+)?)")
_SILENCIO_FIN_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?(?:e[-+]?\d+)?)")
_TIEMPO_RE = re.compile(r"time=\s*(\d+:\d{2}:\d{2}(?:\.\d+)?)")


def parsear_silencedetect(
    salida: str, duracion_ms: int | None = None
) -> tuple[list[tuple[int, int]], int | None]:
    """stderr de silencedetect → ([(inicio_ms, fin_ms)], duración decodificada en ms o None).

    Si el audio termina en silencio puede faltar el último silence_end (ffmpeg < 9): se cierra
    con la duración decodificada o, si no está, con `duracion_ms`. Sin ninguna de las dos, se
    descarta ese silencio abierto.
    """
    from classmer.util import tiempo_a_ms

    pares: list[tuple[int, int]] = []
    abierto: int | None = None
    decodificado: int | None = None
    for linea in salida.splitlines():
        if m := _SILENCIO_INICIO_RE.search(linea):
            abierto = max(0, int(round(float(m.group(1)) * 1000)))
        elif m := _SILENCIO_FIN_RE.search(linea):
            fin = max(0, int(round(float(m.group(1)) * 1000)))
            pares.append((abierto if abierto is not None else 0, fin))
            abierto = None
        elif "time=" in linea and (m := _TIEMPO_RE.search(linea)):
            decodificado = tiempo_a_ms(m.group(1))
    cierre = decodificado if decodificado is not None else duracion_ms
    if abierto is not None and cierre is not None:
        pares.append((abierto, cierre))
    return [(a, b) for a, b in pares if b > a], decodificado


def detectar_silencios(
    ruta: Path, *, umbral_db: float, minimo_s: float, duracion_ms: int | None = None,
    timeout: float | None = 3600,
) -> tuple[list[tuple[int, int]], int | None]:
    """`ffmpeg -af silencedetect`: silencios ≥ minimo_s por debajo de umbral_db (en ms absolutos)."""
    filtro = f"silencedetect=noise={umbral_db:g}dB:d={minimo_s:g}"
    r = ejecutar(["ffmpeg", "-hide_banner", "-nostats", "-i", str(ruta), "-map", "0:a:0",
                  "-af", filtro, "-f", "null", "-"], timeout=timeout)
    if r.returncode != 0:
        raise ErrorAudio(f"ffmpeg no pudo analizar {ruta.name}: {r.stderr.strip()[-500:]}")
    return parsear_silencedetect(r.stderr, duracion_ms)


def extraer_mp3(
    origen: Path, destino: Path, inicio_ms: int, fin_ms: int, *, frecuencia_hz: int = 16000,
    kbps: int = 48, timeout: float | None = 1800,
) -> None:
    """Re-codifica [inicio_ms, fin_ms) a MP3 mono (libmp3lame), con escritura atómica.

    `-ss`/`-to` van después de `-i`: ffmpeg decodifica desde el principio y corta exacto por
    timestamp (con -ss antes de -i, un MP3 VBR importado puede caer corrido).
    """
    if fin_ms <= inicio_ms:
        raise ErrorAudio(f"tramo vacío: {inicio_ms}–{fin_ms} ms")
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(f".{destino.name}.tmp")
    args = [
        "ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-y", "-i", str(origen),
        "-ss", f"{inicio_ms / 1000:.3f}", "-to", f"{fin_ms / 1000:.3f}",
        "-map", "0:a:0", "-ac", "1", "-ar", str(frecuencia_hz),
        "-c:a", "libmp3lame", "-b:a", f"{kbps}k", "-map_metadata", "-1", "-f", "mp3", str(tmp),
    ]
    try:
        r = ejecutar(args, timeout=timeout)
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            raise ErrorAudio(
                f"ffmpeg no pudo cortar {origen.name} ({inicio_ms}–{fin_ms} ms): {r.stderr.strip()[-500:]}"
            )
        os.replace(tmp, destino)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def unir_tramos(tramos: list[tuple[Path, int, int]], destino: Path, *, silencio_ms: int = 2000,
                frecuencia_hz: int = 16000, kbps: int = 48, timeout: float | None = 900) -> list[int]:
    """Varios tramos [(audio, inicio_ms, fin_ms)] en un solo MP3 mono, separados por `silencio_ms` de
    silencio, con escritura atómica. Devuelve dónde empieza cada tramo en el archivo nuevo (ms)."""
    if not tramos:
        raise ErrorAudio("no hay tramos para unir")
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(f".{destino.name}.tmp")
    entradas: list[str] = []
    filtros: list[str] = []
    piezas: list[str] = []
    inicios: list[int] = []
    cursor = 0
    for i, (ruta, a, b) in enumerate(tramos):
        if b <= a:
            raise ErrorAudio(f"tramo vacío: {a}–{b} ms")
        entradas += ["-i", str(ruta)]
        filtros.append(f"[{i}:a:0]atrim=start={a / 1000:.3f}:end={b / 1000:.3f},asetpts=PTS-STARTPTS,"
                       f"aresample={frecuencia_hz},aformat=channel_layouts=mono[p{i}]")
        if i:
            piezas.append(f"[s{i}]")
            cursor += silencio_ms
        piezas.append(f"[p{i}]")
        inicios.append(cursor)
        cursor += b - a
    n = len(tramos)
    if n > 1:
        entradas += ["-f", "lavfi", "-i", f"anullsrc=r={frecuencia_hz}:cl=mono:d={silencio_ms / 1000:.3f}"]
        filtros.append(f"[{n}:a]asplit={n - 1}" + "".join(f"[s{i}]" for i in range(1, n)))
    filtros.append("".join(piezas) + f"concat=n={len(piezas)}:v=0:a=1[out]")
    args = ["ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-y", *entradas,
            "-filter_complex", ";".join(filtros), "-map", "[out]", "-c:a", "libmp3lame", "-b:a", f"{kbps}k",
            "-map_metadata", "-1", "-f", "mp3", str(tmp)]
    try:
        r = ejecutar(args, timeout=timeout)
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            raise ErrorAudio(f"ffmpeg no pudo unir {n} tramo(s): {r.stderr.strip()[-500:]}")
        os.replace(tmp, destino)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return inicios


def convertir_opus(origen: Path, destino: Path, *, kbps: int = 32, timeout: float | None = 1800) -> None:
    """Copia en Opus mono (Ogg) para el reproductor del panel, con escritura atómica."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(f".{destino.name}.tmp")
    args = [
        "ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-y", "-i", str(origen),
        "-map", "0:a:0", "-vn", "-ac", "1", "-c:a", "libopus", "-b:a", f"{kbps}k",
        "-map_metadata", "-1", "-f", "ogg", str(tmp),
    ]
    try:
        r = ejecutar(args, timeout=timeout)
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            raise ErrorAudio(f"ffmpeg no pudo convertir {origen.name} a Opus: {r.stderr.strip()[-500:]}")
        os.replace(tmp, destino)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
