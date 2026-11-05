"""Importador (plan B): offsets de fotos por EXIF, inferencia del inicio y POST /api/importar."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from classmer import sesiones
from classmer.db import Base
from classmer.importador import (
    ErrorImportacion,
    importar_archivos,
    parsear_fecha_exif,
    ubicar_offset,
)
from classmer.util import leer_json, sha256_archivo
from conftest import requiere

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
INICIO = datetime(2026, 10, 15, 14, 0, 0, tzinfo=TZ)
DUR_WAV_MS = 120_000
DUR_M4A_MS = 900_000
CREATION_TIME = "2026-10-15T17:15:00Z"   # 14:15:00 en Buenos Aires


# --- helpers ---------------------------------------------------------------------------------

def _ffmpeg(*args: str) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True,
                   creationflags=flags)


def jpeg_bytes(fecha: str | None = None, offset: str | None = None, color: str = "darkgreen") -> bytes:
    img = Image.new("RGB", (320, 240), color)
    exif = Image.Exif()
    if fecha is not None:
        sub = {36867: fecha}
        if offset is not None:
            sub[36881] = offset
        exif[0x8769] = sub
    b = io.BytesIO()
    img.save(b, "JPEG", exif=exif, quality=80)
    return b.getvalue()


def jpeg(ruta: Path, fecha: str | None = None, offset: str | None = None) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(jpeg_bytes(fecha, offset))
    return ruta


def fotos_de(base: Base, sid: str) -> dict[str, dict]:
    """nombre original → {offset_ms, datos, ruta}."""
    with base.conexion() as conn:
        filas = conn.execute(
            "SELECT offset_ms, ruta, datos FROM assets WHERE sesion_id = ? AND tipo = 'foto'", (sid,)
        ).fetchall()
    res = {}
    for f in filas:
        datos = json.loads(f["datos"])
        res[datos["nombre_original"]] = {"offset_ms": f["offset_ms"], "datos": datos, "ruta": f["ruta"]}
    return res


def sesion_de(base: Base, sid: str):
    with base.conexion() as conn:
        return conn.execute("SELECT * FROM sesiones WHERE id = ?", (sid,)).fetchone()


@pytest.fixture(scope="module")
def audios(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if shutil.which("ffmpeg") is None:
        pytest.skip("falta ffmpeg")
    d = tmp_path_factory.mktemp("audios")
    wav = d / "clase.wav"
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=8000:duration={DUR_WAV_MS // 1000}",
            "-ac", "1", "-c:a", "pcm_s16le", str(wav))
    m4a = d / "Grabación 001.m4a"
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=8000:duration={DUR_M4A_MS // 1000}",
            "-ac", "1", "-c:a", "aac", "-b:a", "16k", "-metadata", f"creation_time={CREATION_TIME}", str(m4a))
    return {"wav": wav, "m4a": m4a}


@pytest.fixture
def wav(audios: dict[str, Path], tmp_path: Path) -> Path:
    """Copia propia del wav (algunos tests le cambian el mtime)."""
    destino = tmp_path / "entrada" / "clase.wav"
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(audios["wav"], destino)
    return destino


# --- unidades sin ffmpeg ---------------------------------------------------------------------

def test_parsear_fecha_exif():
    assert parsear_fecha_exif("2026:10:15 14:00:30") == datetime(2026, 10, 15, 14, 0, 30, tzinfo=TZ)
    dt = parsear_fecha_exif("2026:10:15 17:00:30", "+00:00", "25")
    assert dt == datetime(2026, 10, 15, 17, 0, 30, 250000, tzinfo=timezone.utc)
    assert dt.astimezone(TZ).hour == 14
    assert parsear_fecha_exif(b"2026:10:15 14:00:30\x00", "-0300") == datetime(2026, 10, 15, 14, 0, 30, tzinfo=TZ)
    assert parsear_fecha_exif("    :  :     :  :  ") is None
    assert parsear_fecha_exif(None) is None
    assert parsear_fecha_exif("2026:13:45 99:00:00") is None


def test_ubicar_offset():
    dur = 60_000
    assert ubicar_offset(INICIO + timedelta(seconds=30), INICIO, dur) == (30_000, {})
    # dentro del margen de 5 min: se recorta sin marcar
    assert ubicar_offset(INICIO - timedelta(minutes=4), INICIO, dur) == (0, {})
    assert ubicar_offset(INICIO + timedelta(minutes=5), INICIO, dur) == (dur, {})
    # fuera de rango: se recorta y se marca
    assert ubicar_offset(INICIO - timedelta(minutes=6), INICIO, dur) == (
        0, {"fuera_de_rango": True, "offset_original_ms": -360_000})
    assert ubicar_offset(INICIO + timedelta(hours=2), INICIO, dur) == (
        dur, {"fuera_de_rango": True, "offset_original_ms": 7_200_000})


def test_materia_invalida(ctx, tmp_path):
    audio = tmp_path / "clase.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(ErrorImportacion, match="materia_id inexistente"):
        importar_archivos(ctx, audio, materia_id="no-existe", inicio=INICIO)
    with pytest.raises(ErrorImportacion, match="tipo_clase"):
        importar_archivos(ctx, audio, materia_id="prueba", tipo_clase="laboratorio", inicio=INICIO)
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 0


# --- importar con ffmpeg ---------------------------------------------------------------------

@pytest.mark.ffmpeg
@requiere("ffprobe")
def test_audio_ilegible(ctx, tmp_path):
    falso = tmp_path / "clase.ogg"
    falso.write_text("esto no es un audio", encoding="utf-8")
    with pytest.raises(ErrorImportacion, match="no es un audio legible"):
        importar_archivos(ctx, falso, materia_id="prueba", inicio=INICIO)
    foto = jpeg(tmp_path / "foto.jpg", "2026:10:15 14:00:30")
    with pytest.raises(ErrorImportacion, match="no es un audio legible"):
        importar_archivos(ctx, foto, materia_id="prueba", inicio=INICIO)
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 0


@pytest.mark.ffmpeg
def test_offsets_por_exif_y_sesion_en_cola(ctx, cfg, wav, tmp_path):
    d = tmp_path / "fotos"
    a = jpeg(d / "a.jpg", "2026:10:15 14:00:30")                 # hora local, sin offset
    b = jpeg(d / "b.jpg", "2026:10:15 17:01:30", "+00:00")       # UTC con OffsetTimeOriginal
    c = jpeg(d / "c.jpg")                                         # sin EXIF: usa el mtime
    t = (INICIO + timedelta(seconds=60)).timestamp()
    os.utime(c, (t, t))
    lejos = jpeg(d / "lejos.jpg", "2026:10:15 16:00:00")         # 2 h después: fuera de rango
    antes = jpeg(d / "antes.jpg", "2026:10:15 13:59:00")         # 1 min antes: se recorta a 0
    rota = d / "rota.jpg"
    rota.write_bytes(b"\xff\xd8\xff\xe0 basura que no es un jpeg")

    sid = importar_archivos(ctx, wav, materia_id="prueba", fotos=[a, b, c, lejos, antes, rota],
                            inicio=INICIO, tipo_clase="teorica")

    fotos = fotos_de(ctx.base, sid)
    assert set(fotos) == {"a.jpg", "b.jpg", "c.jpg", "lejos.jpg", "antes.jpg"}
    assert fotos["a.jpg"]["offset_ms"] == 30_000
    assert fotos["a.jpg"]["datos"]["origen_hora"] == "exif"
    assert fotos["b.jpg"]["offset_ms"] == 90_000
    assert fotos["c.jpg"]["offset_ms"] == 60_000
    assert fotos["c.jpg"]["datos"]["origen_hora"] == "mtime"
    assert fotos["lejos.jpg"]["offset_ms"] == DUR_WAV_MS
    assert fotos["lejos.jpg"]["datos"]["fuera_de_rango"] is True
    assert fotos["lejos.jpg"]["datos"]["offset_original_ms"] == 7_200_000
    assert fotos["antes.jpg"]["offset_ms"] == 0
    assert "fuera_de_rango" not in fotos["antes.jpg"]["datos"]
    carpeta = cfg.sesion_dir(sid)
    for f in fotos.values():
        assert (carpeta / f["ruta"]).is_file()
    assert (carpeta / fotos["a.jpg"]["ruta"]).read_bytes() == a.read_bytes()

    s = sesion_de(ctx.base, sid)
    assert s["estado"] == "en_cola"
    assert s["materia_id"] == "prueba" and s["origen_materia"] == "manual"
    assert s["tipo_clase"] == "teorica" and s["dispositivo"] == "importador"
    assert s["inicio"] == "2026-10-15T14:00:00-03:00"
    assert s["duracion_ms"] == DUR_WAV_MS and s["eventos_totales"] == 5
    with ctx.base.conexion() as conn:
        ruta = sesiones.ruta_audio(cfg, conn, sid)
        jobs = conn.execute("SELECT tipo, estado FROM jobs WHERE sesion_id = ?", (sid,)).fetchall()
        audio_datos = json.loads(conn.execute("SELECT datos FROM assets WHERE id = ?",
                                              (sesiones.id_audio(sid),)).fetchone()["datos"])
    assert ruta.name == "audio.wav"
    assert sha256_archivo(ruta) == sha256_archivo(wav) == s["sha256"]
    assert s["bytes_totales"] == wav.stat().st_size
    assert [tuple(j) for j in jobs] == [("verificar", "pendiente")]
    assert audio_datos["origen_inicio"] == "parametro"

    lineas = (carpeta / "eventos.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 5
    resumen = leer_json(carpeta / "importacion.json")
    assert resumen["origen_inicio"] == "parametro"
    assert [f["nombre"] for f in resumen["fotos_ilegibles"]] == ["rota.jpg"]
    log = (carpeta / "pipeline.log").read_text(encoding="utf-8")
    assert "rota.jpg" in log and "parametro" in log


@pytest.mark.ffmpeg
def test_fotos_sin_hora_si_no_se_usa_mtime(ctx, wav, tmp_path):
    c = jpeg(tmp_path / "c.jpg")
    a = jpeg(tmp_path / "a.jpg", "2026:10:15 14:01:00")
    sid = importar_archivos(ctx, wav, materia_id=None, fotos=[c, a], inicio=INICIO,
                            usar_mtime_fotos=False)
    fotos = fotos_de(ctx.base, sid)
    assert fotos["c.jpg"]["offset_ms"] is None
    assert fotos["c.jpg"]["datos"]["origen_hora"] is None
    assert fotos["a.jpg"]["offset_ms"] == 60_000
    s = sesion_de(ctx.base, sid)
    assert s["materia_id"] is None and s["origen_materia"] == "auto"
    assert s["estado"] == "en_cola" and s["eventos_totales"] == 2


@pytest.mark.ffmpeg
def test_inicio_por_mtime_del_audio(ctx, cfg, wav):
    fin = (INICIO + timedelta(milliseconds=DUR_WAV_MS)).timestamp()
    os.utime(wav, (fin, fin))
    sid = importar_archivos(ctx, wav, materia_id="prueba")
    s = sesion_de(ctx.base, sid)
    assert s["inicio"] == "2026-10-15T14:00:00-03:00"
    assert leer_json(cfg.sesion_dir(sid) / "importacion.json")["origen_inicio"] == "mtime_audio"


@pytest.mark.ffmpeg
def test_inicio_por_creation_time(ctx, cfg, audios):
    sid = importar_archivos(ctx, audios["m4a"], materia_id="prueba", usar_mtime_audio=False)
    s = sesion_de(ctx.base, sid)
    assert s["inicio"] == "2026-10-15T14:15:00-03:00"
    with ctx.base.conexion() as conn:
        assert sesiones.ruta_audio(cfg, conn, sid).name == "audio.m4a"
    resumen = leer_json(cfg.sesion_dir(sid) / "importacion.json")
    assert resumen["origen_inicio"] == "creation_time"
    assert resumen["audio"]["nombre_original"] == "Grabación 001.m4a"


@pytest.mark.ffmpeg
def test_creation_time_escrito_al_final(ctx, cfg, audios, tmp_path):
    # Foto 10 min antes del creation_time: sólo cierra si creation_time es el fin de la grabación.
    foto = jpeg(tmp_path / "pizarra.jpg", "2026:10:15 14:05:00")
    sid = importar_archivos(ctx, audios["m4a"], materia_id="prueba", fotos=[foto],
                            usar_mtime_audio=False, usar_mtime_fotos=False)
    s = sesion_de(ctx.base, sid)
    assert s["inicio"] == "2026-10-15T14:00:00-03:00"
    assert fotos_de(ctx.base, sid)["pizarra.jpg"]["offset_ms"] == 300_000
    assert leer_json(cfg.sesion_dir(sid) / "importacion.json")["origen_inicio"] == "creation_time_como_fin"


@pytest.mark.ffmpeg
def test_sin_hora_del_audio_el_inicio_sale_de_las_fotos(ctx, cfg, wav, tmp_path):
    # Como la API: sin inicio, sin mtime y el wav no trae creation_time. Antes: ahora − duración y
    # todas las fotos apiladas al final.
    fotos = [jpeg(tmp_path / "f1.jpg", "2026:10:15 14:00:30"), jpeg(tmp_path / "f2.jpg", "2026:10:15 14:01:00")]
    sid = importar_archivos(ctx, wav, materia_id="prueba", fotos=fotos, usar_mtime_audio=False,
                            usar_mtime_fotos=False)
    assert sesion_de(ctx.base, sid)["inicio"] == "2026-10-15T13:59:30-03:00"
    offsets = {k: v["offset_ms"] for k, v in fotos_de(ctx.base, sid).items()}
    assert offsets == {"f1.jpg": 60_000, "f2.jpg": 90_000}
    resumen = leer_json(cfg.sesion_dir(sid) / "importacion.json")
    assert resumen["origen_inicio"] == "exif_fotos" and resumen["candidatos_inicio"] == []
    assert any("primera foto" in a for a in resumen["avisos"])

    # Con un mtime que no es la hora real (copiado por Drive): ningún candidato deja las fotos adentro.
    t = (INICIO + timedelta(hours=5)).timestamp()
    os.utime(wav, (t, t))
    sid = importar_archivos(ctx, wav, materia_id="prueba", fotos=fotos, usar_mtime_fotos=False)
    assert sesion_de(ctx.base, sid)["inicio"] == "2026-10-15T13:59:30-03:00"
    resumen = leer_json(cfg.sesion_dir(sid) / "importacion.json")
    assert resumen["origen_inicio"] == "exif_fotos"
    assert [(c["origen"], c["fotos_dentro"]) for c in resumen["candidatos_inicio"]] == [("mtime_audio", 0)]


@pytest.mark.ffmpeg
def test_con_fotos_gana_el_candidato_que_las_deja_adentro(ctx, cfg, audios, tmp_path):
    # m4a con creation_time 14:15 pero mtime de otro día: antes ganaba el mtime sin mirar las fotos.
    m4a = tmp_path / "Voz 002.m4a"
    shutil.copyfile(audios["m4a"], m4a)
    t = datetime(2026, 10, 13, 23, 0, tzinfo=TZ).timestamp()
    os.utime(m4a, (t, t))
    foto = jpeg(tmp_path / "pizarra.jpg", "2026:10:15 14:20:00")
    sid = importar_archivos(ctx, m4a, materia_id="prueba", fotos=[foto], usar_mtime_fotos=False)
    assert sesion_de(ctx.base, sid)["inicio"] == "2026-10-15T14:15:00-03:00"
    assert fotos_de(ctx.base, sid)["pizarra.jpg"]["offset_ms"] == 300_000
    resumen = leer_json(cfg.sesion_dir(sid) / "importacion.json")
    assert resumen["origen_inicio"] == "creation_time"
    assert [(c["origen"], c["fotos_dentro"]) for c in resumen["candidatos_inicio"]] == [
        ("mtime_audio", 0), ("creation_time", 1), ("creation_time_como_fin", 1)]
    assert resumen["avisos"] == []


def test_inicio_por_fotos_ignora_una_foto_suelta():
    from classmer.importador import _inicio_por_fotos

    horas = [datetime(2026, 10, 14, 9, 0, tzinfo=TZ),            # del día anterior, por error
             INICIO + timedelta(seconds=30), INICIO + timedelta(seconds=90)]
    assert _inicio_por_fotos(horas, DUR_WAV_MS) == INICIO - timedelta(seconds=30)
    assert _inicio_por_fotos(horas[1:], DUR_WAV_MS) == INICIO - timedelta(seconds=30)


@pytest.mark.ffmpeg
def test_extension_desde_el_nombre_original(ctx, cfg, wav, tmp_path):
    sin_ext = tmp_path / "subida"
    shutil.copyfile(wav, sin_ext)
    sid = importar_archivos(ctx, sin_ext, materia_id="prueba", inicio=INICIO, nombre_original="Clase.WAV")
    with ctx.base.conexion() as conn:
        assert sesiones.ruta_audio(cfg, conn, sid).name == "audio.wav"
    sid2 = importar_archivos(ctx, sin_ext, materia_id="prueba", inicio=INICIO)
    with ctx.base.conexion() as conn:
        assert sesiones.ruta_audio(cfg, conn, sid2).name == "audio.wav"   # por el formato de ffprobe


# --- POST /api/importar ----------------------------------------------------------------------

@pytest.mark.ffmpeg
def test_api_importar(client, cfg, audios):
    archivos = [
        ("audio", ("clase.wav", audios["wav"].read_bytes(), "audio/wav")),
        ("fotos[]", ("p1.jpg", jpeg_bytes("2026:10:15 14:00:45"), "image/jpeg")),
        ("fotos[]", ("p2.jpg", jpeg_bytes("2026:10:15 17:01:40", "+00:00"), "image/jpeg")),
        ("fotos[]", ("p3.jpg", jpeg_bytes(), "image/jpeg")),
    ]
    r = client.post("/api/importar", files=archivos,
                    data={"materia_id": "prueba", "inicio": "2026-10-15T14:00:00-03:00",
                          "tipo_clase": "practica"})
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert set(cuerpo) == {"id", "estado"} and cuerpo["estado"] == "en_cola"
    sid = cuerpo["id"]

    base = Base(cfg.db_path)
    offsets = {k: v["offset_ms"] for k, v in fotos_de(base, sid).items()}
    assert sorted(offsets.values(), key=lambda x: (x is None, x or 0)) == [45_000, 100_000, None]
    s = sesion_de(base, sid)
    assert s["estado"] == "en_cola" and s["tipo_clase"] == "practica"
    assert s["inicio"] == "2026-10-15T14:00:00-03:00" and s["eventos_totales"] == 3
    api = client.get(f"/api/sesiones/{sid}").json()
    assert api["estado"] == "en_cola" and api["bytes_recibidos"] == audios["wav"].stat().st_size
    # La carpeta temporal de la subida se borra.
    assert not any(p.name.startswith("importar-") for p in cfg.datos.iterdir())


@pytest.mark.ffmpeg
def test_api_importar_materia_auto(client, cfg, audios):
    r = client.post("/api/importar", files={"audio": ("clase.wav", audios["wav"].read_bytes(), "audio/wav")},
                    data={"materia_id": "auto", "inicio": "2026-10-15T14:00:00-03:00"})
    assert r.status_code == 201, r.text
    s = sesion_de(Base(cfg.db_path), r.json()["id"])
    assert s["materia_id"] is None and s["origen_materia"] == "auto"


def test_api_importar_materia_inexistente(client, cfg):
    r = client.post("/api/importar", files={"audio": ("clase.wav", b"RIFF0000WAVE", "audio/wav")},
                    data={"materia_id": "no-existe"})
    assert r.status_code == 422
    assert "materia_id inexistente" in r.json()["detail"]
    r = client.post("/api/importar", data={"materia_id": "prueba"})
    assert r.status_code == 422
    with Base(cfg.db_path).conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 0


# --- scripts de soporte ----------------------------------------------------------------------

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def cargar_script(nombre: str):
    import importlib.util

    import sys

    if nombre in sys.modules:
        return sys.modules[nombre]
    spec = importlib.util.spec_from_file_location(nombre, SCRIPTS / f"{nombre}.py")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo   # las dataclasses lo necesitan registrado
    spec.loader.exec_module(modulo)
    return modulo


@pytest.mark.ffmpeg
def test_generar_fotos_prueba_e_importar(ctx, wav, tmp_path):
    gen = cargar_script("generar_fotos_prueba")
    fotos = gen.generar(tmp_path / "fotos", INICIO, [30_000, 95_000])
    assert [p.name for p in fotos] == ["20261015_140030.jpg", "20261015_140135.jpg"]
    sid = importar_archivos(ctx, wav, materia_id="prueba", fotos=fotos, inicio=INICIO, usar_mtime_fotos=False)
    offsets = {k: v["offset_ms"] for k, v in fotos_de(ctx.base, sid).items()}
    assert offsets == {"20261015_140030.jpg": 30_000, "20261015_140135.jpg": 95_000}


@pytest.mark.ffmpeg
def test_simular_celu_subida_y_finalizar(client, cfg, audios, tmp_path):
    simular_celu = cargar_script("simular_celu")
    wav = audios["wav"]
    fotos = [jpeg(tmp_path / "f1.jpg", "2026:10:15 14:00:30"), jpeg(tmp_path / "f2.jpg")]
    lineas: list[str] = []
    sid = simular_celu.simular(client, wav, fotos=fotos, materia="prueba", tipo_clase="teorica",
                               chunk=256 * 1024, duracion_ms=DUR_WAV_MS, offsets_fotos=[30_000],
                               inicio=INICIO, esperar=False, probar_409=True, imprimir=lineas.append)
    assert any("409" in linea for linea in lineas)
    s = client.get(f"/api/sesiones/{sid}").json()
    assert s["estado"] == "en_cola" and s["bytes_recibidos"] == wav.stat().st_size
    base = Base(cfg.db_path)
    with base.conexion() as conn:
        tipos = sorted(f["tipo"] for f in conn.execute(
            "SELECT tipo FROM assets WHERE sesion_id = ? AND tipo != 'audio'", (sid,)))
        jobs = [tuple(j) for j in conn.execute("SELECT tipo, estado FROM jobs WHERE sesion_id = ?", (sid,))]
        ruta = sesiones.ruta_audio(cfg, conn, sid)
    assert tipos == ["foto", "foto", "marca", "marca", "nota"]
    assert jobs == [("verificar", "pendiente")]
    assert sha256_archivo(ruta) == sha256_archivo(wav)
    # Como el celu, manda las fotos como <uuid>.jpg; la segunda va repartida en la duración.
    assert sorted(v["offset_ms"] for v in fotos_de(base, sid).values()) == [30_000, DUR_WAV_MS * 2 // 3]
    # Volver a correrlo con la misma sesión no duplica nada.
    simular_celu.simular(client, wav, fotos=fotos, materia="prueba", sesion_id=sid, duracion_ms=DUR_WAV_MS,
                         esperar=False, imprimir=lineas.append)
    assert len(client.get(f"/api/sesiones/{sid}/eventos").json()["ids"]) == 5


@pytest.mark.ffmpeg
def test_simular_celu_hash_distinto(client, cfg, audios):
    simular_celu = cargar_script("simular_celu")
    wav = audios["wav"]
    sid = "0192f7c2-0000-7000-8000-00000000abcd"
    r = client.post("/api/sesiones", json={"id": sid, "materia_id": "prueba", "inicio": "2026-10-15T14:00:00-03:00"})
    assert r.status_code == 201
    basura = b"\x00" * wav.stat().st_size      # mismo largo, otro contenido
    r = client.put(f"/api/sesiones/{sid}/audio?offset=0", content=basura)
    assert r.status_code == 200
    lineas: list[str] = []
    simular_celu.simular(client, wav, materia="prueba", sesion_id=sid, duracion_ms=DUR_WAV_MS,
                         chunk=512 * 1024, esperar=False, imprimir=lineas.append)
    assert any("hash_distinto" in linea for linea in lineas)
    assert client.get(f"/api/sesiones/{sid}").json()["estado"] == "en_cola"
    with Base(cfg.db_path).conexion() as conn:
        ruta = sesiones.ruta_audio(cfg, conn, sid)
    assert sha256_archivo(ruta) == sha256_archivo(wav)
    assert any(".descartado-" in p.name for p in ruta.parent.iterdir())
