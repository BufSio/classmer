"""Importar clase: sesiones importadas desde la app (POST /api/sesiones + verificar), el esquema 4,
importar_clase con número y material, analizar_archivos (paso "Analizar" del panel), POST
/api/importar con número y material y el área temporal."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from classmer import adjuntos, audio, importador, sesiones
from classmer.cola import Job
from classmer.db import Base
from classmer.importador import (
    AudioGrande,
    ErrorImportacion,
    analizar_archivos,
    candidatos_inicio,
    importar_archivos,
    importar_clase,
    parsear_fecha_exif,
)
from classmer.pipeline import ErrorEtapa
from classmer.pipeline.verificar import ejecutar as verificar
from classmer.util import leer_json
from test_importador import _ffmpeg, fotos_de, jpeg, jpeg_bytes, sesion_de

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
INICIO = datetime(2026, 10, 15, 14, 0, 0, tzinfo=TZ)
INICIO_ISO = "2026-10-15T14:00:00-03:00"
DUR_WAV_MS = 120_000
DUR_M4A_MS = 900_000
CREATION_TIME = "2026-10-15T17:15:00Z"   # 14:15:00 en Buenos Aires
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"
GIB = 1024 ** 3


# --- helpers ---------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def audios(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("falta ffmpeg/ffprobe")
    d = tmp_path_factory.mktemp("audios-importar")
    seno = "sine=frequency=440:sample_rate=8000:duration="
    wav = d / "clase.wav"
    _ffmpeg("-f", "lavfi", "-i", f"{seno}{DUR_WAV_MS // 1000}", "-ac", "1", "-c:a", "pcm_s16le", str(wav))
    m4a = d / "Audio de WhatsApp.m4a"
    _ffmpeg("-f", "lavfi", "-i", f"{seno}{DUR_M4A_MS // 1000}", "-ac", "1", "-c:a", "aac", "-b:a", "16k",
            "-metadata", f"creation_time={CREATION_TIME}", str(m4a))
    wma = d / "clase.wma"   # asf/wmav2: Chrome no lo reproduce
    _ffmpeg("-f", "lavfi", "-i", f"{seno}{DUR_WAV_MS // 1000}", "-ac", "1", "-c:a", "wmav2", str(wma))
    return {"wav": wav, "m4a": m4a, "wma": wma}


def crear_importada(client: TestClient, **campos) -> tuple[str, object]:
    sid = str(uuid.uuid4())
    cuerpo = {"id": sid, "materia_id": "prueba", "origen_materia": "calendario", "tipo_clase": "teorica",
              "inicio": INICIO_ISO, "dispositivo": "s23-ultra", "importada": True,
              "audio_nombre": "Audio de WhatsApp.m4a", "audio_bytes": 1234}
    cuerpo.update(campos)
    return sid, client.post("/api/sesiones", json=cuerpo)


def subir(client: TestClient, sid: str, datos: bytes, tramo: int = 64 * 1024) -> None:
    for offset in range(0, len(datos), tramo):
        r = client.put(f"/api/sesiones/{sid}/audio?offset={offset}", content=datos[offset:offset + tramo],
                       headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text


def foto_sin_minuto(client: TestClient, sid: str, contenido: bytes, nombre: str = "foto.jpg") -> str:
    eid = str(uuid.uuid4())
    r = client.post(f"/api/sesiones/{sid}/eventos", data={"id": eid, "tipo": "foto"},
                    files={"archivo": (nombre, contenido, "image/jpeg")})
    assert r.status_code == 201, r.text
    return eid


def finalizar(client: TestClient, sid: str, datos: bytes, duracion_ms: int | None, eventos: int) -> None:
    r = client.post(f"/api/sesiones/{sid}/finalizar", json={
        "sha256": hashlib.sha256(datos).hexdigest(), "bytes_totales": len(datos),
        "duracion_ms": duracion_ms, "eventos_totales": eventos})
    assert r.status_code == 200, r.text


def correr_verificar(ctx, sid: str):
    return verificar(ctx, Job(id=1, tipo="verificar", sesion_id=sid))


def fotos_por_id(base: Base, sid: str) -> dict[str, dict]:
    with base.conexion() as conn:
        filas = conn.execute("SELECT id, offset_ms, ruta, datos FROM assets WHERE sesion_id = ? AND tipo = 'foto'",
                             (sid,)).fetchall()
    return {f["id"]: {"offset_ms": f["offset_ms"], "ruta": f["ruta"], "datos": json.loads(f["datos"])} for f in filas}


def datos_audio(base: Base, sid: str) -> dict:
    with base.conexion() as conn:
        fila = conn.execute("SELECT ruta, datos FROM assets WHERE id = ?", (sesiones.id_audio(sid),)).fetchone()
    return {"ruta": fila["ruta"], **json.loads(fila["datos"] or "{}")}


# --- unidades --------------------------------------------------------------------------------

def test_nombre_audio_importado_y_mensaje():
    assert sesiones.nombre_audio_importado("Audio de WhatsApp 2026-10-20 a las 14.03.opus") == "audio.opus"
    assert sesiones.nombre_audio_importado("Grabación.M4A") == "audio.m4a"
    assert sesiones.nombre_audio_importado(r"C:\x\clase.amr") == "audio.amr"
    for raro in ("clase.xyz", "sin_extension", "", None, "clase.m4a.exe", "../../x.bin"):
        assert sesiones.nombre_audio_importado(raro) == "audio.bin"
    assert sesiones.MAX_AUDIO_IMPORTADO == 2 * GIB
    assert sesiones.mensaje_audio_grande(int(2.3 * GIB)) == "el audio pesa 2,3 GB: el máximo para importar es 2 GB"
    assert sesiones.mensaje_audio_grande(2 * GIB + 1) == "el audio pesa 2,1 GB: el máximo para importar es 2 GB"
    assert sesiones.mensaje_audio_grande() == "el audio pesa más de 2 GB: el máximo para importar es 2 GB"


@pytest.mark.parametrize("extension, codec, esperado", [
    (".ogg", "opus", True), (".opus", "opus", True), (".ogg", "vorbis", True), (".m4a", "aac", True),
    (".mp3", "mp3", True), (".wav", "pcm_s16le", True), (".flac", "flac", True), (".webm", "opus", True),
    (".amr", "amr_nb", False), (".wma", "wmav2", False), (".3gp", "aac", False), (".m4a", "alac", False),
    (".bin", "aac", False), (".ogg", None, False),
])
def test_reproducible_en_navegador(extension, codec, esperado):
    info = {"streams": [{"codec_type": "audio", "codec_name": codec}] if codec else []}
    assert audio.reproducible_en_navegador(Path("audio" + extension), info) is esperado


def test_extension_por_formato():
    assert audio.extension_por_formato({"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}) == ".m4a"
    assert audio.extension_por_formato({"format": {"format_name": "amr"}}) == ".amr"
    assert audio.extension_por_formato({"format": {"format_name": "asf"}}) == ".wma"
    assert audio.extension_por_formato({"format": {"format_name": "flv"}}) == ".flv"
    assert audio.extension_por_formato({}) is None


def test_candidatos_orden_y_etiquetas_sin_ffmpeg():
    dur = 900_000
    ct = INICIO + timedelta(minutes=15)
    cands = [(INICIO - timedelta(days=2), "mtime_audio"), (ct, "creation_time"),
             (ct - timedelta(milliseconds=dur), "creation_time_como_fin")]
    horas = [INICIO + timedelta(minutes=25)]   # sólo creation_time la deja adentro
    lista = candidatos_inicio("America/Argentina/Buenos_Aires", cands, horas, dur, elegido="creation_time")
    assert [c["origen"] for c in lista] == ["creation_time", "exif_fotos", "mtime_audio", "creation_time_como_fin"]
    assert [c["elegido"] for c in lista] == [True, False, False, False]
    assert [c["fotos_dentro"] for c in lista] == [1, 1, 0, 0]
    assert lista[0]["etiqueta"] == "Según los metadatos del audio (lo confirman 1 de 1 fotos)"
    assert lista[1]["etiqueta"] == "Según las fotos (EXIF): 1 min antes de la primera"
    assert lista[1]["inicio"] == "2026-10-15T14:24:00-03:00"
    assert lista[2]["etiqueta"].startswith("Según la fecha del archivo, menos la duración: deja 0 de 1")
    assert "WhatsApp" in lista[3]["etiqueta"]
    # Sin fotos: sólo los metadatos, con la advertencia, y sin fotos_dentro.
    lista = candidatos_inicio("America/Argentina/Buenos_Aires", cands[1:], [], dur, elegido="creation_time")
    assert [c["origen"] for c in lista] == ["creation_time", "creation_time_como_fin"]
    assert lista[0]["etiqueta"] == "Según los metadatos del audio: ojo, en WhatsApp suele ser la hora del envío"
    assert all("fotos_dentro" not in c for c in lista)


# --- POST /api/sesiones -----------------------------------------------------------------------

def test_sesion_normal_queda_igual(client: TestClient, cfg, base):
    sid = str(uuid.uuid4())
    r = client.post("/api/sesiones", json={"id": sid, "materia_id": "prueba", "inicio": INICIO_ISO})
    assert r.status_code == 201 and r.json() == {"id": sid, "estado": "grabando"}
    assert datos_audio(base, sid) == {"ruta": "audio.ogg"}
    assert client.get(f"/api/sesiones/{sid}").json()["importada"] is False
    # Los campos nuevos no cuentan sin importada: ni el nombre ni el tope.
    sid2 = str(uuid.uuid4())
    r = client.post("/api/sesiones", json={"id": sid2, "inicio": INICIO_ISO, "importada": False,
                                           "audio_nombre": "x.amr", "audio_bytes": 5 * GIB})
    assert r.status_code == 201
    assert datos_audio(base, sid2) == {"ruta": "audio.ogg"}
    assert sesion_de(base, sid2)["importada"] == 0


def test_crear_importada(client: TestClient, cfg, base):
    sid, r = crear_importada(client, audio_nombre=r"C:\Users\x\Audio de WhatsApp.M4A", audio_bytes=4_000_000)
    assert r.status_code == 201 and r.json() == {"id": sid, "estado": "grabando"}
    assert datos_audio(base, sid) == {"ruta": "audio.m4a", "nombre_original": "Audio de WhatsApp.M4A",
                                      "bytes_declarados": 4_000_000}
    s = client.get(f"/api/sesiones/{sid}").json()
    assert s["importada"] is True and s["estado"] == "grabando" and s["numero_clase"] == 1
    assert any(x["id"] == sid and x["importada"] is True for x in client.get("/api/sesiones").json())
    assert (cfg.sesion_dir(sid) / "fotos").is_dir()


@pytest.mark.parametrize("nombre", ["clase.xyz", "sin_extension", None])
def test_crear_importada_extension_desconocida_es_bin(client: TestClient, base, nombre):
    sid, r = crear_importada(client, audio_nombre=nombre)
    assert r.status_code == 201
    assert datos_audio(base, sid)["ruta"] == "audio.bin"


def test_crear_importada_413_sin_crear_nada(client: TestClient, cfg):
    sid, r = crear_importada(client, audio_bytes=int(2.3 * GIB))
    assert r.status_code == 413
    assert r.json() == {"detail": "el audio pesa 2,3 GB: el máximo para importar es 2 GB"}
    assert client.get(f"/api/sesiones/{sid}").status_code == 404
    assert not cfg.sesion_dir(sid).exists()
    # Justo 2 GiB entra.
    _, r = crear_importada(client, audio_bytes=2 * GIB)
    assert r.status_code == 201


def test_crear_importada_idempotente_sin_revalidar(client: TestClient, base):
    sid, r = crear_importada(client)
    assert r.status_code == 201
    r = client.post("/api/sesiones", json={"id": sid, "inicio": INICIO_ISO, "importada": True,
                                           "audio_nombre": "otro.amr", "audio_bytes": 9 * GIB})
    assert r.status_code == 200 and r.json() == {"id": sid, "estado": "grabando"}
    assert datos_audio(base, sid)["ruta"] == "audio.m4a"


def test_crear_importada_bytes_negativos_422(client: TestClient):
    _, r = crear_importada(client, audio_bytes=-1)
    assert r.status_code == 422


# --- PUT /audio: tope de las importadas -------------------------------------------------------

def test_put_acumulado_413_en_importada(client: TestClient, cfg, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sesiones, "MAX_AUDIO_IMPORTADO", 1000)
    sid, r = crear_importada(client, audio_bytes=None)
    assert r.status_code == 201
    assert client.put(f"/api/sesiones/{sid}/audio?offset=0", content=b"a" * 600).status_code == 200
    r = client.put(f"/api/sesiones/{sid}/audio?offset=600", content=b"b" * 401)
    assert r.status_code == 413 and "máximo para importar" in r.json()["detail"]
    assert (cfg.sesion_dir(sid) / "audio.m4a").read_bytes() == b"a" * 600   # no escribió nada
    assert client.get(f"/api/sesiones/{sid}/audio").json() == {"bytes_recibidos": 600}
    r = client.put(f"/api/sesiones/{sid}/audio?offset=600", content=b"b" * 400)   # justo el tope
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": 1000}
    # Un PUT vacío ("¿cuánto tenés?") nunca da 413.
    assert client.put(f"/api/sesiones/{sid}/audio?offset=1000", content=b"").status_code == 200

    # La grabación normal no tiene ese tope.
    normal = str(uuid.uuid4())
    client.post("/api/sesiones", json={"id": normal, "inicio": INICIO_ISO})
    r = client.put(f"/api/sesiones/{normal}/audio?offset=0", content=b"c" * 1500)
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": 1500}


# --- verificar de una importada ---------------------------------------------------------------

@pytest.mark.ffmpeg
def test_verificar_importada_ubica_fotos_y_escribe_importacion(client: TestClient, ctx, cfg, audios):
    datos = audios["wav"].read_bytes()
    sid, _ = crear_importada(client, audio_nombre="Grabación.wav", audio_bytes=len(datos))
    subir(client, sid, datos)
    dentro = foto_sin_minuto(client, sid, jpeg_bytes("2026:10:15 14:00:30"), "IMG_1.jpg")
    utc = foto_sin_minuto(client, sid, jpeg_bytes("2026:10:15 17:01:30", "+00:00"), "IMG_2.jpg")
    lejos = foto_sin_minuto(client, sid, jpeg_bytes("2026:10:15 16:00:00"), "IMG_3.jpg")
    sin_exif = foto_sin_minuto(client, sid, jpeg_bytes(), "captura.jpg")
    heic = foto_sin_minuto(client, sid, b"\x00\x00\x00\x18ftypheic basura que no abre", "IMG_4.heic")
    finalizar(client, sid, datos, duracion_ms=None, eventos=5)

    r = correr_verificar(ctx, sid)
    assert r.siguientes == [("segmentar", {})]
    fotos = fotos_por_id(ctx.base, sid)
    assert fotos[dentro]["offset_ms"] == 30_000
    assert fotos[dentro]["datos"]["origen_hora"] == "exif"
    assert fotos[dentro]["datos"]["hora"] == "2026-10-15T14:00:30-03:00"
    assert "fuera_de_rango" not in fotos[dentro]["datos"]
    assert fotos[utc]["offset_ms"] == 90_000
    assert fotos[lejos]["offset_ms"] == DUR_WAV_MS
    assert fotos[lejos]["datos"]["fuera_de_rango"] is True
    assert fotos[lejos]["datos"]["offset_original_ms"] == 7_200_000
    for sin_hora in (sin_exif, heic):
        assert fotos[sin_hora]["offset_ms"] is None
        assert fotos[sin_hora]["datos"]["origen_hora"] is None and fotos[sin_hora]["datos"]["hora"] is None

    s = sesion_de(ctx.base, sid)
    assert abs(s["duracion_ms"] - DUR_WAV_MS) <= 50   # la medida (la declarada vino null)
    informe = leer_json(cfg.sesion_dir(sid) / "verificacion.json")
    assert informe["duracion_declarada_ms"] is None and informe["duracion_ms"] == s["duracion_ms"]

    imp = leer_json(cfg.sesion_dir(sid) / "importacion.json")
    assert set(imp) >= {"audio", "inicio", "origen_inicio", "candidatos_inicio", "fotos", "fotos_ilegibles",
                        "avisos", "importado"}
    assert imp["via"] == "app" and imp["origen_inicio"] == "calendario" and imp["inicio"] == INICIO_ISO
    assert imp["audio"]["nombre_original"] == "Grabación.wav" and imp["audio"]["archivo"] == "audio.wav"
    assert imp["audio"]["bytes"] == len(datos) and imp["audio"]["sha256"] == hashlib.sha256(datos).hexdigest()
    assert [f["id"] for f in imp["fotos"]][:3] == [dentro, utc, lejos]
    assert [f["nombre"] for f in imp["fotos_ilegibles"]] == ["IMG_4.heic"]
    assert any("1 de 5 fotos quedaron fuera de la grabación: revisá el inicio" in a for a in imp["avisos"])
    assert any("2 de 5 fotos no tienen hora" in a for a in imp["avisos"])
    assert [c["origen"] for c in imp["candidatos_inicio"]] == ["exif_fotos"]
    assert any("revisá el inicio" in a for a in informe["avisos"])
    log = (cfg.sesion_dir(sid) / "pipeline.log").read_text(encoding="utf-8")
    assert "importada: 3 foto(s) ubicadas por EXIF" in log
    # wav (pcm) se reproduce en el navegador: no hace falta copia.
    assert not (cfg.sesion_dir(sid) / sesiones.AUDIO_PANEL).exists()
    with ctx.base.conexion() as conn:
        assert sesiones.ruta_audio_panel(cfg, conn, sid).name == "audio.wav"

    # Re-ejecutar es idempotente: no vuelve a mirar las fotos ni pisa importacion.json.
    antes = (cfg.sesion_dir(sid) / "importacion.json").read_bytes()
    correr_verificar(ctx, sid)
    assert fotos_por_id(ctx.base, sid) == fotos
    assert (cfg.sesion_dir(sid) / "importacion.json").read_bytes() == antes
    assert leer_json(cfg.sesion_dir(sid) / "verificacion.json")["duracion_declarada_ms"] is None


@pytest.mark.ffmpeg
def test_verificar_importada_duracion_declarada_distinta(client: TestClient, ctx, cfg, audios):
    datos = audios["wav"].read_bytes()
    sid, _ = crear_importada(client, origen_materia="manual", audio_nombre="clase.wav")
    subir(client, sid, datos)
    finalizar(client, sid, datos, duracion_ms=3_600_000, eventos=0)   # el celu midió mal
    correr_verificar(ctx, sid)
    s = sesion_de(ctx.base, sid)
    assert abs(s["duracion_ms"] - DUR_WAV_MS) <= 50 and s["estado"] == "en_cola"
    informe = leer_json(cfg.sesion_dir(sid) / "verificacion.json")
    assert informe["duracion_declarada_ms"] == 3_600_000
    assert any("difiere mucho" in a and "se usa la medida" in a for a in informe["avisos"])
    assert leer_json(cfg.sesion_dir(sid) / "importacion.json")["origen_inicio"] == "manual"
    # El reproceso sigue informando la declarada (aunque la sesión ya tenga la medida).
    correr_verificar(ctx, sid)
    assert leer_json(cfg.sesion_dir(sid) / "verificacion.json")["duracion_declarada_ms"] == 3_600_000
    assert datos_audio(ctx.base, sid)["duracion_declarada_ms"] == 3_600_000


@pytest.mark.ffmpeg
def test_verificar_renombra_audio_bin(client: TestClient, ctx, cfg, audios):
    datos = audios["m4a"].read_bytes()
    sid, _ = crear_importada(client, audio_nombre="audio.dat")
    assert datos_audio(ctx.base, sid)["ruta"] == "audio.bin"
    subir(client, sid, datos)
    finalizar(client, sid, datos, duracion_ms=DUR_M4A_MS, eventos=0)
    correr_verificar(ctx, sid)
    carpeta = cfg.sesion_dir(sid)
    assert datos_audio(ctx.base, sid)["ruta"] == "audio.m4a"
    assert (carpeta / "audio.m4a").read_bytes() == datos and not (carpeta / "audio.bin").exists()
    assert leer_json(carpeta / "importacion.json")["audio"]["archivo"] == "audio.m4a"
    assert client.get(f"/api/sesiones/{sid}").json()["bytes_recibidos"] == len(datos)
    # Con metadatos del audio pero sin fotos: el candidato (informativo) avisa lo de WhatsApp.
    cands = leer_json(carpeta / "importacion.json")["candidatos_inicio"]
    assert [c["origen"] for c in cands] == ["creation_time", "creation_time_como_fin"]
    assert all(c["elegido"] is False for c in cands)
    correr_verificar(ctx, sid)   # idempotente
    assert datos_audio(ctx.base, sid)["ruta"] == "audio.m4a" and (carpeta / "audio.m4a").is_file()


@pytest.mark.ffmpeg
def test_verificar_copia_para_el_panel(client: TestClient, ctx, cfg, audios):
    datos = audios["wma"].read_bytes()
    sid, _ = crear_importada(client, audio_nombre="clase.wma")
    subir(client, sid, datos)
    finalizar(client, sid, datos, duracion_ms=None, eventos=0)
    r = correr_verificar(ctx, sid)
    assert r.siguientes == [("segmentar", {})]
    copia = cfg.sesion_dir(sid) / sesiones.AUDIO_PANEL
    assert copia.is_file()
    info = audio.sondear(copia)
    assert audio.codec_audio(info) == "opus" and audio.formato(info) == "ogg"
    assert abs(audio.duracion_ms(copia) - DUR_WAV_MS) <= 200
    with ctx.base.conexion() as conn:
        assert sesiones.ruta_audio_panel(cfg, conn, sid) == copia
        assert sesiones.ruta_audio(cfg, conn, sid).name == "audio.wma"   # el pipeline usa el original
    # Una sola vez: un reproceso no la rehace.
    marca = copia.stat().st_mtime_ns
    correr_verificar(ctx, sid)
    assert copia.stat().st_mtime_ns == marca


@pytest.mark.ffmpeg
def test_verificar_copia_que_falla_es_solo_un_aviso(client: TestClient, ctx, cfg, audios,
                                                     monkeypatch: pytest.MonkeyPatch):
    def falla(*_a, **_k):
        raise audio.ErrorAudio("ffmpeg no pudo convertir")

    monkeypatch.setattr(audio, "convertir_opus", falla)
    datos = audios["wma"].read_bytes()
    sid, _ = crear_importada(client, audio_nombre="clase.wma")
    subir(client, sid, datos)
    finalizar(client, sid, datos, duracion_ms=None, eventos=0)
    assert correr_verificar(ctx, sid).siguientes == [("segmentar", {})]
    assert not (cfg.sesion_dir(sid) / sesiones.AUDIO_PANEL).exists()
    assert any("reproductor del panel" in a for a in leer_json(cfg.sesion_dir(sid) / "verificacion.json")["avisos"])
    with ctx.base.conexion() as conn:
        assert sesiones.ruta_audio_panel(cfg, conn, sid).name == "audio.wma"


@pytest.mark.ffmpeg
def test_verificar_no_pisa_importacion_json_existente(client: TestClient, ctx, cfg, audios):
    datos = audios["wav"].read_bytes()
    sid, _ = crear_importada(client, audio_nombre="clase.wav")
    subir(client, sid, datos)
    foto_sin_minuto(client, sid, jpeg_bytes("2026:10:15 14:01:00"))
    finalizar(client, sid, datos, duracion_ms=DUR_WAV_MS, eventos=1)
    propio = {"via": "panel", "inicio": INICIO_ISO}
    (cfg.sesion_dir(sid) / "importacion.json").write_text(json.dumps(propio), encoding="utf-8")
    correr_verificar(ctx, sid)
    assert leer_json(cfg.sesion_dir(sid) / "importacion.json") == propio
    assert [f["offset_ms"] for f in fotos_por_id(ctx.base, sid).values()] == [60_000]   # igual las ubica


@pytest.mark.ffmpeg
def test_verificar_sesion_normal_no_cambia(client: TestClient, ctx, cfg, audios):
    datos = audios["wav"].read_bytes()
    sid = str(uuid.uuid4())
    client.post("/api/sesiones", json={"id": sid, "materia_id": "prueba", "inicio": INICIO_ISO})
    subir(client, sid, datos)
    eid = foto_sin_minuto(client, sid, jpeg_bytes("2026:10:15 14:00:30"))
    finalizar(client, sid, datos, duracion_ms=60_000, eventos=1)
    correr_verificar(ctx, sid)
    foto = fotos_por_id(ctx.base, sid)[eid]
    assert foto["offset_ms"] is None and "origen_hora" not in foto["datos"]
    assert sesion_de(ctx.base, sid)["duracion_ms"] == 60_000   # la declarada no se pisa
    assert datos_audio(ctx.base, sid) == {"ruta": "audio.ogg"}
    carpeta = cfg.sesion_dir(sid)
    assert not (carpeta / "importacion.json").exists() and not (carpeta / sesiones.AUDIO_PANEL).exists()


# --- esquema 4 --------------------------------------------------------------------------------

def test_migracion_esquema_4(tmp_path: Path):
    from classmer import db

    linea = next(ln for ln in db.ESQUEMA.splitlines(keepends=True) if ln.strip().startswith("importada "))
    viejo = db.ESQUEMA.replace(linea, "")
    ruta = tmp_path / "vieja.db"
    conn = sqlite3.connect(ruta)
    conn.executescript(viejo)
    for sid, dispositivo in (("s-imp", "importador"), ("s-app", "s23-ultra"), ("s-nada", None)):
        conn.execute("INSERT INTO sesiones (id, inicio, estado, dispositivo, creado, actualizado) "
                     "VALUES (?, 'x', 'lista', ?, 'x', 'x')", (sid, dispositivo))
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    assert "importada" not in {c[1] for c in conn.execute("PRAGMA table_info(sesiones)")}
    conn.close()

    db.inicializar(ruta)
    db.inicializar(ruta)   # idempotente
    conn = sqlite3.connect(ruta)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION_ESQUEMA == 5
        assert dict(conn.execute("SELECT id, importada FROM sesiones").fetchall()) == {
            "s-imp": 1, "s-app": 0, "s-nada": 0}
        # La migración de datos corre una sola vez: lo que se cambie después no lo vuelve a tocar.
        conn.execute("UPDATE sesiones SET importada = 0 WHERE id = 's-imp'")
        conn.commit()
    finally:
        conn.close()
    db.inicializar(ruta)
    with sqlite3.connect(ruta) as conn:
        assert conn.execute("SELECT importada FROM sesiones WHERE id = 's-imp'").fetchone()[0] == 0


# --- importar_clase / importar_archivos -------------------------------------------------------

@pytest.mark.ffmpeg
def test_importar_con_numero_y_material(ctx, cfg, audios, tmp_path):
    pdf = tmp_path / "Slides clase 5.pdf"
    pdf.write_bytes(PDF)
    txt = tmp_path / "notas.txt"
    txt.write_text("esto no es un pdf", encoding="utf-8")
    vacio = tmp_path / "vacio.pdf"
    vacio.write_bytes(b"")
    foto = jpeg(tmp_path / "fotos" / "p.jpg", "2026:10:15 14:00:30")
    r = importar_clase(ctx, audios["wav"], materia_id="prueba", fotos=[foto], inicio=INICIO, numero_clase=7,
                       material=[(pdf, "Slides clase 5.pdf"), (txt, "notas.txt"), (vacio, "vacio.pdf"),
                                 (tmp_path / "no-esta.pdf", "no-esta.pdf")],
                       dispositivo="panel", origen_material="panel")
    s = sesion_de(ctx.base, r.sesion_id)
    assert s["importada"] == 1 and s["numero_clase"] == 7 and s["dispositivo"] == "panel"
    assert s["estado"] == "en_cola" and s["eventos_totales"] == 1
    assert [m["nombre_original"] for m in r.material] == ["Slides clase 5.pdf"]
    assert r.material[0]["nombre"] == "Slides_clase_5.pdf" and r.material[0]["tipo"] == "pdf"
    assert [m["nombre"] for m in r.material_rechazado] == ["notas.txt", "vacio.pdf", "no-esta.pdf"]
    assert "tipo de archivo no soportado" in r.material_rechazado[0]["motivo"]
    assert "vacío" in r.material_rechazado[1]["motivo"]
    assert any("3 archivo(s) de material no entraron" in a for a in r.avisos)
    with ctx.base.conexion() as conn:
        lista = adjuntos.listar(conn, r.sesion_id)
        api = sesiones.a_api(cfg, conn, sesiones.fila_sesion(conn, r.sesion_id))
    assert [(a["nombre"], a["origen"]) for a in lista] == [("Slides_clase_5.pdf", "panel")]
    assert api["importada"] is True and api["adjuntos"][0]["estado"] == "pendiente"
    carpeta = cfg.sesion_dir(r.sesion_id)
    resumen = leer_json(carpeta / "importacion.json")
    assert resumen["via"] == "importador"
    assert [m["nombre_original"] for m in resumen["material"]] == ["Slides clase 5.pdf"]
    assert [m["nombre"] for m in resumen["material_rechazado"]] == ["notas.txt", "vacio.pdf", "no-esta.pdf"]
    assert fotos_de(ctx.base, r.sesion_id)["p.jpg"]["datos"]["hora"] == "2026-10-15T14:00:30-03:00"
    log = (carpeta / "pipeline.log").read_text(encoding="utf-8")
    assert "material salteado: notas.txt" in log and "1 adjunto(s), 3 rechazado(s)" in log

    # importar_archivos sigue devolviendo sólo el id; el material por defecto es del importador.
    sid = importar_archivos(ctx, audios["wav"], materia_id="prueba", inicio=INICIO, material=[(pdf, "x.pdf")])
    with ctx.base.conexion() as conn:
        assert [a["origen"] for a in adjuntos.listar(conn, sid)] == ["importador"]
    # El número que sigue cuenta las clases (como siempre), no el más alto.
    assert sesion_de(ctx.base, sid)["numero_clase"] == 2 and sesion_de(ctx.base, sid)["dispositivo"] == "importador"


@pytest.mark.ffmpeg
def test_importar_material_grande_no_frena(ctx, audios, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(adjuntos, "MAX_ADJUNTO", 10)
    pdf = tmp_path / "grande.pdf"
    pdf.write_bytes(PDF)
    r = importar_clase(ctx, audios["wav"], materia_id="prueba", inicio=INICIO, material=[(pdf, "grande.pdf")])
    assert r.material == [] and "pesa más" in r.material_rechazado[0]["motivo"]
    assert sesion_de(ctx.base, r.sesion_id)["estado"] == "en_cola"


@pytest.mark.ffmpeg
def test_importar_mtime_del_navegador(ctx, cfg, audios):
    fin = INICIO + timedelta(milliseconds=DUR_WAV_MS)
    sid = importar_archivos(ctx, audios["wav"], materia_id="prueba", usar_mtime_audio=False, mtime_audio=fin)
    assert sesion_de(ctx.base, sid)["inicio"] == INICIO_ISO
    assert leer_json(cfg.sesion_dir(sid) / "importacion.json")["origen_inicio"] == "mtime_audio"


def test_importar_audio_grande(ctx, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(importador, "MAX_AUDIO_IMPORTADO", 10)
    grande = tmp_path / "clase.m4a"
    grande.write_bytes(b"x" * 11)
    with pytest.raises(AudioGrande, match="máximo para importar es 2 GB") as e:
        importar_archivos(ctx, grande, materia_id="prueba", inicio=INICIO)
    assert isinstance(e.value, ErrorImportacion) and isinstance(e.value, sesiones.ArchivoGrande)
    with pytest.raises(AudioGrande):
        analizar_archivos(ctx.cfg, grande)
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 0


def test_importar_numero_negativo(ctx, tmp_path):
    audio_falso = tmp_path / "clase.wav"
    audio_falso.write_bytes(b"RIFF")
    with pytest.raises(ErrorImportacion, match="negativo"):
        importar_archivos(ctx, audio_falso, materia_id="prueba", inicio=INICIO, numero_clase=-1)


# --- analizar_archivos ------------------------------------------------------------------------

@pytest.mark.ffmpeg
def test_analizar_con_fotos_que_confirman_el_audio(cfg, base, audios, tmp_path):
    fotos = [jpeg(tmp_path / "f" / "a.jpg", "2026:10:15 14:25:00"), jpeg(tmp_path / "f" / "b.jpg"),
             tmp_path / "f" / "rota.jpg"]
    fotos[2].write_bytes(b"\xff\xd8\xff basura")
    mtime = datetime(2026, 10, 13, 23, 0, tzinfo=TZ)   # la fecha del archivo es la de la descarga
    r = analizar_archivos(cfg, audios["m4a"], fotos, nombre_original="Audio de WhatsApp.m4a", mtime_audio=mtime)
    assert r["audio"] == {"nombre": "Audio de WhatsApp.m4a", "bytes": audios["m4a"].stat().st_size,
                          "duracion_ms": r["audio"]["duracion_ms"], "formato": "mov,mp4,m4a,3gp,3g2,mj2",
                          "codec": "aac", "extension": ".m4a", "reproducible": True}
    assert abs(r["audio"]["duracion_ms"] - DUR_M4A_MS) <= 100
    assert r["inicio"] == "2026-10-15T14:15:00-03:00" and r["origen_inicio"] == "creation_time"
    cands = r["candidatos_inicio"]
    assert [c["origen"] for c in cands] == ["creation_time", "exif_fotos", "mtime_audio", "creation_time_como_fin"]
    assert cands[0]["elegido"] is True and not any(c["elegido"] for c in cands[1:])
    assert cands[0]["etiqueta"] == "Según los metadatos del audio (lo confirman 1 de 1 fotos)"
    assert cands[1]["etiqueta"] == "Según las fotos (EXIF): 1 min antes de la primera"
    assert cands[2]["etiqueta"].startswith("Según la fecha del archivo, menos la duración")
    assert cands[2]["inicio"] == "2026-10-13T22:45:00-03:00"
    assert all(set(c) == {"origen", "inicio", "etiqueta", "elegido", "fotos_dentro"} for c in cands)
    assert r["fotos"] == [
        {"indice": 0, "nombre": "a.jpg", "hora": "2026-10-15T14:25:00-03:00", "origen_hora": "exif",
         "offset_ms": 600_000, "fuera_de_rango": False},
        {"indice": 1, "nombre": "b.jpg", "hora": None, "origen_hora": None, "offset_ms": None,
         "fuera_de_rango": False},
    ]
    assert [f["nombre"] for f in r["fotos_ilegibles"]] == ["rota.jpg"]
    assert r["avisos"] == ["1 de 2 fotos no tienen hora EXIF: van al final, sin minuto"]
    # No crea nada.
    with base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 0
    assert not any(cfg.sesiones_dir.iterdir())


@pytest.mark.ffmpeg
def test_analizar_solo_metadatos_avisa_whatsapp(cfg, audios):
    r = analizar_archivos(cfg, audios["m4a"], [])
    assert r["origen_inicio"] == "creation_time"
    assert [c["origen"] for c in r["candidatos_inicio"]] == ["creation_time", "creation_time_como_fin"]
    assert all("WhatsApp" in c["etiqueta"] and "fotos_dentro" not in c for c in r["candidatos_inicio"])
    assert any("metadatos del audio" in a and "WhatsApp" in a for a in r["avisos"])


@pytest.mark.ffmpeg
def test_analizar_fotos_que_no_coinciden_y_fuera_de_rango(cfg, audios, tmp_path):
    fotos = [jpeg(tmp_path / "a.jpg", "2026:10:16 09:00:30"), jpeg(tmp_path / "b.jpg", "2026:10:16 09:01:00"),
             jpeg(tmp_path / "c.jpg", "2026:10:14 18:00:00")]   # suelta, de otro día
    r = analizar_archivos(cfg, audios["m4a"], fotos)
    assert r["origen_inicio"] == "exif_fotos" and r["inicio"] == "2026-10-16T08:59:30-03:00"
    assert [c["origen"] for c in r["candidatos_inicio"]] == ["exif_fotos", "creation_time", "creation_time_como_fin"]
    assert "hay alguna suelta" in r["candidatos_inicio"][0]["etiqueta"]
    assert r["candidatos_inicio"][1]["etiqueta"].startswith("Según los metadatos del audio: deja 0 de 3 fotos")
    c = next(f for f in r["fotos"] if f["nombre"] == "c.jpg")
    assert c["fuera_de_rango"] is True and c["offset_ms"] == 0 and c["offset_original_ms"] < 0
    assert any("1 de 3 fotos quedan fuera de la grabación" in a for a in r["avisos"])
    assert any("1 min antes de la primera foto" in a for a in r["avisos"])


@pytest.mark.ffmpeg
def test_analizar_sin_ningun_dato(cfg, audios):
    r = analizar_archivos(cfg, audios["wav"], [])
    assert r["origen_inicio"] == "ahora_menos_duracion"
    assert [(c["origen"], c["elegido"]) for c in r["candidatos_inicio"]] == [("ahora_menos_duracion", True)]
    assert any("poné el inicio a mano" in a for a in r["avisos"])
    assert r["audio"]["codec"] == "pcm_s16le" and r["audio"]["reproducible"] is True


@pytest.mark.ffmpeg
def test_analizar_audio_ilegible(cfg, tmp_path):
    falso = tmp_path / "clase.m4a"
    falso.write_text("no es audio", encoding="utf-8")
    with pytest.raises(ErrorImportacion, match="no es un audio legible"):
        analizar_archivos(cfg, falso, [])


# --- POST /api/importar con número y material -------------------------------------------------

@pytest.mark.ffmpeg
def test_api_importar_con_numero_y_material(client: TestClient, cfg, audios):
    archivos = [
        ("audio", ("Audio de WhatsApp.wav", audios["wav"].read_bytes(), "audio/wav")),
        ("fotos[]", ("p1.jpg", jpeg_bytes("2026:10:15 14:00:45"), "image/jpeg")),
        ("material[]", ("Slides clase 5.pdf", PDF, "application/pdf")),
        ("material", ("notas.txt", b"texto plano", "text/plain")),
    ]
    r = client.post("/api/importar", files=archivos,
                    data={"materia_id": "prueba", "inicio": INICIO_ISO, "numero_clase": "12"})
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == "en_cola"
    assert [m["nombre"] for m in cuerpo["material_rechazado"]] == ["notas.txt"]
    sid = cuerpo["id"]
    s = client.get(f"/api/sesiones/{sid}").json()
    assert s["importada"] is True and s["numero_clase"] == 12
    assert [(a["nombre_original"], a["tipo"]) for a in s["adjuntos"]] == [("Slides clase 5.pdf", "pdf")]
    with Base(cfg.db_path).conexion() as conn:
        assert [a["origen"] for a in adjuntos.listar(conn, sid)] == ["importador"]
    # La carpeta temporal de la subida se borra (vive en el área temporal, dentro de Classmer-data).
    assert list((cfg.datos / importador.DIR_TEMPORAL).iterdir()) == []


def test_api_importar_numero_invalido_422(client: TestClient, cfg):
    for numero in ("abc", "-3", "1.5"):
        r = client.post("/api/importar", files={"audio": ("clase.wav", b"RIFF0000WAVE", "audio/wav")},
                        data={"materia_id": "prueba", "numero_clase": numero})
        assert r.status_code == 422, numero
        assert "numero_clase" in r.json()["detail"]


def test_api_importar_413(client: TestClient, cfg, monkeypatch: pytest.MonkeyPatch):
    # El tope del importador (el audio ya copiado)…
    monkeypatch.setattr(importador, "MAX_AUDIO_IMPORTADO", 5)
    r = client.post("/api/importar", files={"audio": ("clase.wav", b"RIFF0000WAVE", "audio/wav")},
                    data={"materia_id": "prueba"})
    assert r.status_code == 413 and "máximo para importar" in r.json()["detail"]
    # …y el de la API, antes de copiar nada.
    monkeypatch.setattr(sesiones, "MAX_AUDIO_IMPORTADO", 5)
    r = client.post("/api/importar", files={"audio": ("clase.wav", b"RIFF0000WAVE", "audio/wav")},
                    data={"materia_id": "prueba"})
    assert r.status_code == 413
    with Base(cfg.db_path).conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 0


@pytest.mark.ffmpeg
def test_cli_importar_numero_y_material(home, cfg, base, audios, tmp_path, capsys):
    from classmer import cli

    pdf = tmp_path / "apunte del profe.pdf"
    pdf.write_bytes(PDF)
    txt = tmp_path / "notas.txt"
    txt.write_text("x", encoding="utf-8")
    codigo = cli.main(["importar", str(audios["wav"]), "--materia", "prueba", "--inicio", INICIO_ISO,
                       "--numero", "4", "--material", str(pdf), "--material", str(txt)])
    assert codigo == 0
    salida = capsys.readouterr()
    assert "Material adjunto: apunte del profe.pdf" in salida.out
    assert "Material que no entró: notas.txt" in salida.err
    with base.conexion() as conn:
        s = conn.execute("SELECT * FROM sesiones").fetchone()
        assert s["numero_clase"] == 4 and s["importada"] == 1
        assert [a["nombre_original"] for a in adjuntos.listar(conn, s["id"])] == ["apunte del profe.pdf"]
    assert cli.main(["importar", str(audios["wav"]), "--materia", "prueba", "--material", str(tmp_path / "no")]) == 2


# --- área temporal del panel ------------------------------------------------------------------

def test_area_temporal(cfg, tmp_path, monkeypatch: pytest.MonkeyPatch):
    raiz = importador.dir_temporal(cfg)
    assert raiz == cfg.datos / "importaciones" and raiz.is_dir()
    token, carpeta = importador.crear_temporal(cfg)
    assert carpeta.parent == raiz and importador.ruta_temporal(cfg, token) == carpeta
    for malo in ("..", "../x", "a" * 32, token.upper(), ""):
        with pytest.raises(ErrorImportacion):
            importador.ruta_temporal(cfg, malo)

    origen = tmp_path / "o.bin"
    origen.write_bytes(b"x" * 100)
    a = importador.guardar_temporal(cfg, token, "audio", origen, r"C:\WhatsApp\Audio: 14.03?.opus")
    f1 = importador.guardar_temporal(cfg, token, "fotos", origen, "../../IMG_1.jpg")
    f2 = importador.guardar_temporal(cfg, token, "fotos", origen, "IMG_1.jpg")
    raro = importador.guardar_temporal(cfg, token, "material", origen, "CON.pdf")
    vacio = importador.guardar_temporal(cfg, token, "material", origen, "..")
    assert a.relative_to(carpeta).as_posix() == "audio/000/Audio 14.03.opus"
    assert f1.relative_to(carpeta).as_posix() == "fotos/000/IMG_1.jpg"
    assert f2.relative_to(carpeta).as_posix() == "fotos/001/IMG_1.jpg"
    assert raro.name == "CON_.pdf" and vacio.name == "archivo_001"
    assert a.read_bytes() == b"x" * 100

    monkeypatch.setitem(importador.TOPES_TEMPORAL, "audio", 50)
    monkeypatch.setitem(importador.TOPES_TEMPORAL, "fotos", 50)
    with pytest.raises(AudioGrande, match="máximo para importar"):
        importador.guardar_temporal(cfg, token, "audio", origen, "grande.m4a")
    with pytest.raises(sesiones.ArchivoGrande):
        importador.guardar_temporal(cfg, token, "fotos", origen, "grande.jpg")
    assert sorted(p.name for p in (carpeta / "audio").iterdir()) == ["000"]   # lo que pasó el tope no queda
    with pytest.raises(ValueError):
        importador.guardar_temporal(cfg, token, "otra", origen, "x")

    assert importador.borrar_temporal(cfg, token) is True and not carpeta.exists()
    assert importador.borrar_temporal(cfg, token) is False


def test_limpiar_temporales(cfg):
    viejo_token, viejo = importador.crear_temporal(cfg)
    (viejo / "audio").mkdir()
    (viejo / "audio" / "clase.m4a").write_bytes(b"x")
    nuevo_token, nuevo = importador.crear_temporal(cfg)
    (nuevo / "foto.jpg").write_bytes(b"x")
    a_medias_token, a_medias = importador.crear_temporal(cfg)   # vieja, pero con algo recién subido
    (a_medias / "fotos").mkdir()
    (a_medias / "fotos" / "nueva.jpg").write_bytes(b"x")
    api_vieja = cfg.datos / "importar-abc123"   # un POST /api/importar de antes, cortado
    api_vieja.mkdir()
    hace = time.time() - 25 * 3600
    for p in (viejo / "audio" / "clase.m4a", viejo / "audio", viejo, a_medias, api_vieja):
        os.utime(p, (hace, hace))

    assert importador.limpiar_temporales(cfg) == 2
    assert not viejo.exists() and not api_vieja.exists()
    assert nuevo.is_dir() and a_medias.is_dir()
    assert importador.limpiar_temporales(cfg) == 0
    assert importador.limpiar_temporales(cfg, vencimiento_h=0) == 2


def test_limpiar_temporales_al_arrancar_el_server(cfg):
    from classmer.api.app import crear_app

    _, viejo = importador.crear_temporal(cfg)
    hace = time.time() - 48 * 3600
    os.utime(viejo, (hace, hace))
    with TestClient(crear_app(cfg, iniciar_worker=False)):
        assert not viejo.exists()


def test_limpiar_temporales_desde_el_worker(ctx, cfg):
    from classmer.worker import Worker

    _, viejo = importador.crear_temporal(cfg)
    hace = time.time() - 48 * 3600
    os.utime(viejo, (hace, hace))
    Worker(ctx)._limpiar_temporales()
    assert not viejo.exists()


# --- lo que encontró la revisión ----------------------------------------------------------------

@pytest.mark.ffmpeg
def test_verificar_importada_sin_pista_de_audio(client: TestClient, ctx, cfg, audios, tmp_path):
    # Un video sin sonido: ffprobe le mide la duración, pero no hay nada que transcribir. Falla en
    # verificar, sin reintentar y con un motivo que se entiende (no en segmentar, con la salida de ffmpeg).
    video = tmp_path / "VID-20261015.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=5", "-c:v", "mpeg4", str(video))
    datos = video.read_bytes()
    sid, _ = crear_importada(client, audio_nombre=video.name, audio_bytes=len(datos))
    subir(client, sid, datos)
    finalizar(client, sid, datos, duracion_ms=None, eventos=0)
    with pytest.raises(ErrorEtapa) as e:
        correr_verificar(ctx, sid)
    assert e.value.reintentable is False
    assert str(e.value) == "audio ilegible: VID-20261015.mp4 no tiene pista de audio (¿un video sin sonido?)"
    assert not (cfg.sesion_dir(sid) / sesiones.AUDIO_PANEL).exists()   # ni intentó la copia


@pytest.mark.ffmpeg
def test_verificar_reconcilia_un_rename_de_audio_bin_cortado(client: TestClient, ctx, cfg, audios,
                                                             monkeypatch: pytest.MonkeyPatch):
    datos = audios["m4a"].read_bytes()

    def importada_bin() -> str:
        sid, _ = crear_importada(client, audio_nombre="audio.dat")
        subir(client, sid, datos)
        finalizar(client, sid, datos, duracion_ms=DUR_M4A_MS, eventos=0)
        return sid

    # El server muere entre el UPDATE y el rename: la base dice audio.m4a y en disco está audio.bin.
    sid = importada_bin()
    replace = os.replace

    def muere(origen, destino):
        if Path(origen).name == "audio.bin":
            raise SystemExit("se cortó la luz")
        replace(origen, destino)

    monkeypatch.setattr(importador.os, "replace", muere)
    with pytest.raises(SystemExit):
        correr_verificar(ctx, sid)
    monkeypatch.undo()
    carpeta = cfg.sesion_dir(sid)
    assert datos_audio(ctx.base, sid)["ruta"] == "audio.m4a" and (carpeta / "audio.bin").is_file()
    assert correr_verificar(ctx, sid).siguientes == [("segmentar", {})]
    assert datos_audio(ctx.base, sid)["ruta"] == "audio.m4a" and (carpeta / "audio.m4a").read_bytes() == datos
    assert not (carpeta / "audio.bin").exists()
    assert "la base decía audio.m4a pero el audio está como audio.bin" in (carpeta / "pipeline.log").read_text(
        encoding="utf-8")

    # Al revés (un corte de luz pierde el commit y el rename no): la base dice audio.bin.
    sid = importada_bin()
    carpeta = cfg.sesion_dir(sid)
    (carpeta / "audio.bin").rename(carpeta / "audio.m4a")
    assert correr_verificar(ctx, sid).siguientes == [("segmentar", {})]
    assert datos_audio(ctx.base, sid)["ruta"] == "audio.m4a"


def test_parsear_fecha_exif_valores_extremos():
    # Un offset imposible se ignora: la zona local, como sin offset (antes, la foto quedaba ilegible).
    assert parsear_fecha_exif("2026:10:20 14:01:00", "+25:00") == datetime(2026, 10, 20, 14, 1, tzinfo=TZ)
    assert parsear_fecha_exif("2026:10:20 14:01:00", "-03:75") == datetime(2026, 10, 20, 14, 1, tzinfo=TZ)
    # Un reloj sin configurar o un EXIF editado: sin hora (con 9999 o 0001 no se puede ni restar un minuto).
    for fecha, offset in (("9999:12:31 23:59:00", "-05:00"), ("0001:01:01 00:00:30", None),
                          ("1970:01:01 00:00:00", None), ("2101:01:01 00:00:00", None)):
        assert parsear_fecha_exif(fecha, offset) is None, fecha
    assert parsear_fecha_exif("2000:01:01 00:00:00") == datetime(2000, 1, 1, tzinfo=TZ)


@pytest.mark.ffmpeg
def test_fotos_con_exif_extremo(client: TestClient, ctx, cfg, audios, tmp_path):
    rara = jpeg(tmp_path / "rara.jpg", "2026:10:15 14:01:00", "+25:00")
    r = analizar_archivos(cfg, audios["wav"], [rara])
    assert r["fotos_ilegibles"] == [] and r["fotos"][0]["hora"] == "2026-10-15T14:01:00-03:00"
    reloj_roto = jpeg(tmp_path / "reloj.jpg", "9999:12:31 23:59:00", "-05:00")
    r = analizar_archivos(cfg, audios["wav"], [reloj_roto])   # antes: OverflowError (500 en el panel)
    assert r["fotos"][0]["hora"] is None and r["origen_inicio"] == "ahora_menos_duracion"

    # Desde la app: verificar pasa (antes daba OverflowError en cada reintento, para siempre).
    datos = audios["wav"].read_bytes()
    sid, _ = crear_importada(client, audio_nombre="clase.wav")
    subir(client, sid, datos)
    eid = foto_sin_minuto(client, sid, jpeg_bytes("9999:12:31 23:59:00", "-05:00"), "reloj.jpg")
    finalizar(client, sid, datos, duracion_ms=None, eventos=1)
    assert correr_verificar(ctx, sid).siguientes == [("segmentar", {})]
    foto = fotos_por_id(ctx.base, sid)[eid]
    assert foto["offset_ms"] is None and foto["datos"]["origen_hora"] is None


@pytest.mark.ffmpeg
def test_verificar_cortado_despues_de_las_fotos_no_pierde_los_avisos(client: TestClient, ctx, cfg, audios,
                                                                     monkeypatch: pytest.MonkeyPatch):
    datos = audios["wma"].read_bytes()   # Chrome no lo reproduce: verificar arma la copia para el panel
    sid, _ = crear_importada(client, audio_nombre="clase.wma")
    subir(client, sid, datos)
    foto_sin_minuto(client, sid, jpeg_bytes("2026:10:15 18:00:00"), "fuera.jpg")
    foto_sin_minuto(client, sid, b"no es una imagen", "rota.jpg")
    finalizar(client, sid, datos, duracion_ms=None, eventos=2)

    def trabado(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    # Se corta al armar la copia, con las fotos ya ubicadas (como un reinicio del server en ese momento).
    monkeypatch.setattr(audio, "convertir_opus", trabado)
    with pytest.raises(PermissionError):
        correr_verificar(ctx, sid)
    monkeypatch.undo()
    carpeta = cfg.sesion_dir(sid)
    assert not (carpeta / "importacion.json").exists()

    # El reintento ya no tiene fotos por mirar, pero los avisos salen de la base.
    correr_verificar(ctx, sid)
    imp = leer_json(carpeta / "importacion.json")
    assert [f["nombre"] for f in imp["fotos_ilegibles"]] == ["rota.jpg"] and imp["fotos_ilegibles"][0]["motivo"]
    assert "1 de 2 fotos quedaron fuera de la grabación: revisá el inicio" in imp["avisos"]
    assert "1 de 2 fotos no tienen hora EXIF: van al final, sin minuto" in imp["avisos"]
    assert "1 foto(s) no se pudieron leer: quedan sin hora" in imp["avisos"]
    log = (carpeta / "pipeline.log").read_text(encoding="utf-8")
    assert "importada: 1 foto(s) ubicadas por EXIF, 1 sin hora, 1 fuera de rango" in log
    # Un reproceso (ya con importacion.json) no repite los avisos de las fotos.
    correr_verificar(ctx, sid)
    assert not any("fotos" in a for a in leer_json(carpeta / "verificacion.json")["avisos"])


@pytest.mark.ffmpeg
def test_analizar_indice_con_una_ilegible_antes(cfg, audios, tmp_path):
    rota = tmp_path / "roto.jpg"
    rota.write_bytes(b"no es una imagen")
    fotos = [rota, jpeg(tmp_path / "IMG_1.jpg", "2026:10:15 14:00:30"),
             jpeg(tmp_path / "IMG_2.jpg", "2026:10:15 14:01:00")]
    r = analizar_archivos(cfg, audios["wav"], fotos)
    # El índice es el de la lista que se pasó (con la ilegible): con eso el panel pone el nombre original.
    assert [(f["indice"], f["nombre"]) for f in r["fotos"]] == [(1, "IMG_1.jpg"), (2, "IMG_2.jpg")]
    assert [f["nombre"] for f in r["fotos_ilegibles"]] == ["roto.jpg"]


@pytest.mark.ffmpeg
def test_importar_que_falla_no_deja_la_sesion(ctx, cfg, audios, tmp_path, monkeypatch: pytest.MonkeyPatch):
    def lleno(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    foto = jpeg(tmp_path / "f" / "p.jpg", "2026:10:15 14:00:30")
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(PDF)
    monkeypatch.setattr(importador.shutil, "copyfile", lleno)
    with pytest.raises(OSError):
        importar_clase(ctx, audios["wav"], materia_id="prueba", fotos=[foto], inicio=INICIO,
                       material=[(pdf, "slides.pdf")])
    monkeypatch.undo()
    # Ni una clase en error sin audio (que el panel no deja borrar fuera de prueba) ni su carpeta.
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert not any(cfg.sesiones_dir.iterdir())
    # El número no se gastó.
    r = importar_clase(ctx, audios["wav"], materia_id="prueba", fotos=[foto], inicio=INICIO)
    assert sesion_de(ctx.base, r.sesion_id)["numero_clase"] == 1


@pytest.mark.ffmpeg
def test_importar_moviendo_el_audio(ctx, cfg, audios, tmp_path, monkeypatch: pytest.MonkeyPatch):
    datos = audios["wav"].read_bytes()
    origen = tmp_path / "temporal" / "clase.wav"
    origen.parent.mkdir()
    origen.write_bytes(datos)
    r = importar_clase(ctx, origen, materia_id="prueba", inicio=INICIO, mover_audio=True)
    assert not origen.exists() and (cfg.sesion_dir(r.sesion_id) / "audio.wav").read_bytes() == datos
    assert sesion_de(ctx.base, r.sesion_id)["estado"] == "en_cola"

    # Si falla después de moverlo, el audio vuelve a su lugar (el análisis del panel sigue sirviendo).
    origen.write_bytes(datos)

    def falla(*_a, **_k):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(importador, "sha256_archivo", falla)
    with pytest.raises(OSError):
        importar_clase(ctx, origen, materia_id="prueba", inicio=INICIO, mover_audio=True)
    assert origen.read_bytes() == datos
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 1


def test_api_importar_413_mientras_llega(client: TestClient, cfg, monkeypatch: pytest.MonkeyPatch):
    import tempfile

    import starlette.formparsers

    escritos: list[int] = []

    class Grabadora(tempfile.SpooledTemporaryFile):   # lo que Starlette guarda en la carpeta temporal
        def write(self, s):
            escritos.append(len(s))
            return super().write(s)

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", Grabadora)
    monkeypatch.setattr(sesiones, "MAX_AUDIO_IMPORTADO", 1000)
    r = client.post("/api/importar", files={"audio": ("clase.wav", b"x" * 5000, "audio/wav")},
                    data={"materia_id": "prueba"})
    assert r.status_code == 413 and "máximo para importar" in r.json()["detail"]
    assert sum(escritos) <= 1000   # el audio no se guardó entero antes de responder
    # El tope es del audio: las fotos no cuentan.
    r = client.post("/api/importar", files=[("audio", ("clase.wav", b"x" * 900, "audio/wav")),
                                            ("fotos[]", ("p.jpg", b"y" * 5000, "image/jpeg"))],
                    data={"materia_id": "prueba"})
    assert r.status_code == 422 and "no es un audio legible" in r.json()["detail"]
    with Base(cfg.db_path).conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 0


@pytest.mark.parametrize("columnas_ya_creadas", [False, True])
def test_migracion_esquema_5(tmp_path: Path, columnas_ya_creadas: bool):
    """Esquema 5 (§15): `sesiones.espera` y `bloques.pista`. Dos puntos de partida: una base de main
    (esquema 4) y la de producción del 27/10, que quedó con las columnas creadas pero, al reiniciar con
    main, vuelve a `user_version = 4`. Las dos terminan igual y la migración es idempotente."""
    from classmer import db

    viejo = db.ESQUEMA
    if not columnas_ya_creadas:
        for col in ("espera ", "pista "):
            linea = next(ln for ln in viejo.splitlines(keepends=True) if ln.strip().startswith(col))
            viejo = viejo.replace(linea, "")
    ruta = tmp_path / "vieja.db"
    conn = sqlite3.connect(ruta)
    conn.executescript(viejo)
    for sid, estado in (("s-espera", "esperando_usuario"), ("s-lista", "lista")):
        conn.execute("INSERT INTO sesiones (id, inicio, estado, creado, actualizado) VALUES (?, 'x', ?, 'x', 'x')",
                     (sid, estado))
    conn.execute("INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta) VALUES ('s-lista', 1, 0, 1, 'b')")
    if columnas_ya_creadas:
        conn.execute("INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta, pista) "
                     "VALUES ('s-lista', 101, 0, 1, 'c', 'celu')")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    db.inicializar(ruta)
    db.inicializar(ruta)   # idempotente
    with sqlite3.connect(ruta) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION_ESQUEMA == 5
        assert dict(conn.execute("SELECT id, espera FROM sesiones").fetchall()) == {
            "s-espera": "materia", "s-lista": None}
        pistas = dict(conn.execute("SELECT n, pista FROM bloques").fetchall())
        assert pistas == ({1: None, 101: "celu"} if columnas_ya_creadas else {1: None})
        # Lo que cambió después de la migración no se vuelve a tocar.
        conn.execute("UPDATE sesiones SET espera = 'revision' WHERE id = 's-espera'")
    db.inicializar(ruta)
    with sqlite3.connect(ruta) as conn:
        assert conn.execute("SELECT espera FROM sesiones WHERE id = 's-espera'").fetchone()[0] == "revision"
