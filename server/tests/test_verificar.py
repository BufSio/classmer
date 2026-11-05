"""Etapa verificar: hash, duración con un wav real (ffmpeg lavfi), sesiones importadas y avisos."""

from __future__ import annotations

import io
import json
import shutil
import uuid
from pathlib import Path

import pytest

from classmer import adjuntos, audio, cola, sesiones
from classmer.cola import Job
from classmer.pipeline import ErrorEtapa, actualizar_sesion
from classmer.pipeline.verificar import difiere_mucho, ejecutar
from classmer.util import sha256_archivo

pytestmark = pytest.mark.ffmpeg

SID = "verif-0001"


@pytest.fixture(scope="module")
def wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("falta ffmpeg/ffprobe")
    ruta = tmp_path_factory.mktemp("audio") / "tono.wav"
    r = audio.ejecutar(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=3", "-ac", "1", "-ar", "16000", str(ruta)],
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return ruta


def armar(ctx, audio_origen: Path | bytes | None, **campos) -> Path:
    """Sesión 'en_cola' con el audio copiado como audio.wav y los campos dados."""
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=SID, materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio="2026-10-15T14:00:05-03:00",
                       dispositivo="test", audio_nombre="audio.wav")
        actualizar_sesion(conn, SID, ctx.cfg.zona_horaria, estado="en_cola", **campos)
    carpeta = ctx.sesion_dir(SID)
    if isinstance(audio_origen, Path):
        shutil.copy(audio_origen, carpeta / "audio.wav")
    elif isinstance(audio_origen, bytes):
        (carpeta / "audio.wav").write_bytes(audio_origen)
    return carpeta


def job() -> Job:
    return Job(id=1, tipo="verificar", sesion_id=SID)


def fila(ctx):
    with ctx.base.conexion() as conn:
        return conn.execute("SELECT * FROM sesiones WHERE id = ?", (SID,)).fetchone()


def test_hash_ok(ctx, wav):
    sha = sha256_archivo(wav)
    carpeta = armar(ctx, wav, sha256=sha, duracion_ms=3000, eventos_totales=0,
                    bytes_totales=wav.stat().st_size)
    r = ejecutar(ctx, job())
    assert r.siguientes == [("segmentar", {})]
    informe = json.loads((carpeta / "verificacion.json").read_text(encoding="utf-8"))
    assert informe["sha256"] == sha
    assert informe["bytes"] == wav.stat().st_size
    assert abs(informe["duracion_ms"] - 3000) <= 50
    assert informe["duracion_declarada_ms"] == 3000
    assert informe["fotos_faltantes"] == []
    assert informe["eventos"] == 0
    assert informe["avisos"] == []
    s = fila(ctx)
    assert s["sha256"] == sha and s["duracion_ms"] == 3000  # la declarada no se pisa


def test_hash_distinto_no_reintenta(ctx, wav):
    carpeta = armar(ctx, wav, sha256="0" * 64, duracion_ms=3000)
    with pytest.raises(ErrorEtapa) as e:
        ejecutar(ctx, job())
    assert e.value.reintentable is False
    assert "hash" in str(e.value)
    informe = json.loads((carpeta / "verificacion.json").read_text(encoding="utf-8"))
    assert informe["error"] == "hash distinto"
    assert fila(ctx)["sha256"] == "0" * 64


def test_importada_sin_sha_completa_hash_y_duracion(ctx, wav):
    armar(ctx, wav)  # como el importador: sin sha256, sin duración, sin bytes_totales
    r = ejecutar(ctx, job())
    assert r.siguientes == [("segmentar", {})]
    s = fila(ctx)
    assert s["sha256"] == sha256_archivo(wav)
    assert abs(s["duracion_ms"] - 3000) <= 50
    assert s["bytes_totales"] == wav.stat().st_size
    with ctx.base.conexion() as conn:
        a = conn.execute("SELECT sha256 FROM assets WHERE id = ?", (sesiones.id_audio(SID),)).fetchone()
    assert a["sha256"] == s["sha256"]


def test_avisos_sin_fallar(ctx, wav, tmp_path):
    carpeta = armar(ctx, wav, sha256=sha256_archivo(wav), duracion_ms=60_000, eventos_totales=3)
    foto = tmp_path / "f.jpg"
    foto.write_bytes(b"\xff\xd8\xff" + b"0" * 50)
    sesiones.registrar_evento(ctx.cfg, ctx.base, SID, evento_id="foto-1", tipo="foto", offset_ms=1000,
                              archivo=foto, nombre_archivo="f.jpg")
    (carpeta / "fotos" / "foto-1.jpg").unlink()
    r = ejecutar(ctx, job())
    assert r.siguientes == [("segmentar", {})]
    informe = json.loads((carpeta / "verificacion.json").read_text(encoding="utf-8"))
    # duracion_ms es la medida, no la declarada (apunte la usa para validar meta.json).
    assert abs(informe["duracion_ms"] - 3000) <= 50 and informe["duracion_declarada_ms"] == 60_000
    assert fila(ctx)["duracion_ms"] == 60_000
    assert informe["fotos_faltantes"] == ["fotos/foto-1.jpg"]
    assert informe["eventos"] == 1
    assert len(informe["avisos"]) == 3
    texto = " ".join(informe["avisos"])
    assert "difiere" in texto and "fotos" in texto and "1 eventos de 3" in texto
    log = (carpeta / "pipeline.log").read_text(encoding="utf-8")
    assert log.count("verificar: aviso:") == 3


def test_los_adjuntos_no_cuentan_como_eventos(ctx, wav):
    # Un adjunto no tapa un evento perdido (SOLO_EVENTOS): 1 nota + 1 adjunto de 2 declarados avisa.
    carpeta = armar(ctx, wav, sha256=sha256_archivo(wav), duracion_ms=3000, eventos_totales=2)
    sesiones.registrar_evento(ctx.cfg, ctx.base, SID, evento_id=str(uuid.uuid4()), tipo="nota", offset_ms=1000,
                              texto="x")
    adjuntos.guardar(ctx.cfg, ctx.base, SID, adjunto_id=str(uuid.uuid4()), archivo=io.BytesIO(b"%PDF-1.4\n%%EOF\n"),
                     nombre_original="slides.pdf", offset_ms=2000)
    ejecutar(ctx, job())
    informe = json.loads((carpeta / "verificacion.json").read_text(encoding="utf-8"))
    assert informe["eventos"] == 1
    assert any("llegaron 1 eventos de 2 declarados" in a for a in informe["avisos"])


def test_audio_ilegible(ctx):
    armar(ctx, b"esto no es un audio" * 200)
    with pytest.raises(ErrorEtapa) as e:
        ejecutar(ctx, job())
    assert e.value.reintentable is False
    assert str(e.value).startswith("audio ilegible")


def test_sin_audio(ctx):
    armar(ctx, None)
    with pytest.raises(ErrorEtapa) as e:
        ejecutar(ctx, job())
    assert e.value.reintentable is False


def test_difiere_mucho():
    assert not difiere_mucho(3_000, 3_500)
    assert difiere_mucho(3_000, 60_000)
    assert not difiere_mucho(5_400_000, 5_450_000)  # 50 s en hora y media: dentro del 3 %
    assert difiere_mucho(5_400_000, 5_000_000)


def test_con_worker_encola_segmentar_o_deja_error(ctx, wav):
    from classmer.worker import Worker

    armar(ctx, wav, sha256=sha256_archivo(wav))
    w = Worker(ctx)
    try:
        with ctx.base.conexion() as conn:
            cola.encolar(conn, "verificar", SID)
            j = cola.tomar(conn, ["verificar"])
        w.ejecutar_job(j)
        with ctx.base.conexion() as conn:
            tipos = [x.tipo for x in cola.jobs_de_sesion(conn, SID) if x.estado == "pendiente"]
        assert tipos == ["segmentar"]
        assert fila(ctx)["estado"] == "procesando"

        with ctx.base.conexion() as conn:
            actualizar_sesion(conn, SID, ctx.cfg.zona_horaria, sha256="f" * 64)
            cola.encolar(conn, "verificar", SID)
            j = cola.tomar(conn, ["verificar"])
        w.ejecutar_job(j)
        s = fila(ctx)
        assert s["estado"] == "error" and s["error"].startswith("verificar: el hash")
    finally:
        w.detener()
