"""Segunda pista, el audio del celu (§15, "Grabar con los dos"): API, finalizar, segmentar, desfase por
correlación, respaldo y "Agregar audio del celu" desde el panel."""

from __future__ import annotations

import hashlib
import json
import random
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from classmer import audio, cola, correlacion, sesiones
from classmer.cola import Job
from classmer.db import Base
from classmer.pipeline import segmentar
from classmer.util import leer_json
from conftest import requiere

INICIO = "2026-10-26T14:00:05-03:00"


def crear(client: TestClient) -> str:
    sid = str(uuid.uuid4())
    r = client.post("/api/sesiones", json={"id": sid, "materia_id": "prueba", "origen_materia": "calendario",
                                           "tipo_clase": "teorica", "inicio": INICIO, "dispositivo": "galaxy-watch8"})
    assert r.status_code == 201, r.text
    return sid


def subir(client: TestClient, sid: str, datos: bytes, offset: int = 0, pista: str | None = None):
    q = f"?offset={offset}" + (f"&pista={pista}" if pista else "")
    return client.put(f"/api/sesiones/{sid}/audio{q}", content=datos,
                      headers={"Content-Type": "application/octet-stream"})


def cuerpo(principal: bytes, celu: bytes | None = None, *, desfase: int | None = -1500, **extra):
    c = {"sha256": hashlib.sha256(principal).hexdigest(), "bytes_totales": len(principal),
         "duracion_ms": 60_000, "eventos_totales": 0}
    if celu is not None:
        c["celu"] = {"sha256": hashlib.sha256(celu).hexdigest(), "bytes_totales": len(celu),
                     "duracion_ms": 61_000, "desfase_ms": desfase}
    c.update(extra)
    return c


def fila(cfg, sid: str) -> dict:
    with Base(cfg.db_path).conexion() as conn:
        return dict(conn.execute("SELECT * FROM sesiones WHERE id = ?", (sid,)).fetchone())


# --- API ---------------------------------------------------------------------------------------------

def test_subir_y_preguntar_por_la_pista_del_celu(client: TestClient, cfg):
    sid = crear(client)
    # El GET de la pista la repite: la app distingue un server nuevo aunque no haya bytes en ninguna.
    assert client.get(f"/api/sesiones/{sid}/audio?pista=celu").json() == {"bytes_recibidos": 0, "pista": "celu"}
    assert subir(client, sid, b"celu-1", pista="celu").json() == {"bytes_recibidos": 6}
    r = subir(client, sid, b"x", offset=2, pista="celu")
    assert r.status_code == 409 and r.json() == {"bytes_recibidos": 6}
    assert subir(client, sid, b"celu-2", offset=6, pista="celu").json() == {"bytes_recibidos": 12}
    # El principal sigue por su lado (sin pista, o pista=principal).
    assert subir(client, sid, b"reloj").json() == {"bytes_recibidos": 5}
    assert client.get(f"/api/sesiones/{sid}/audio").json() == {"bytes_recibidos": 5}
    assert client.get(f"/api/sesiones/{sid}/audio?pista=principal").json() == {"bytes_recibidos": 5}
    assert (cfg.sesion_dir(sid) / "audio.celu.ogg").read_bytes() == b"celu-1celu-2"
    assert subir(client, sid, b"x", pista="otra").status_code == 422
    assert client.get(f"/api/sesiones/{sid}/audio?pista=otra").status_code == 422
    # La pista del celu no es un evento: GET /eventos sigue vacío.
    assert client.get(f"/api/sesiones/{sid}/eventos").json() == {"ids": []}


def test_finalizar_con_el_celu(client: TestClient, cfg):
    sid = crear(client)
    reloj, celu = b"reloj" * 10, b"celu" * 10
    subir(client, sid, reloj)
    subir(client, sid, celu[:15], pista="celu")
    # Orden: faltan bytes del principal → del celu → eventos → hashes.
    r = client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo(reloj + b"mas", celu))
    assert r.status_code == 409 and r.json() == {"motivo": "faltan_bytes", "bytes_recibidos": 50}
    r = client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo(reloj, celu))
    assert r.status_code == 409 and r.json() == {"motivo": "faltan_bytes", "pista": "celu", "bytes_recibidos": 15}
    subir(client, sid, celu[15:], offset=15, pista="celu")
    r = client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo(reloj, celu, eventos_totales=1))
    assert r.status_code == 409 and r.json()["motivo"] == "faltan_eventos"
    malo = cuerpo(reloj, celu)
    malo["celu"]["sha256"] = "0" * 64
    r = client.post(f"/api/sesiones/{sid}/finalizar", json=malo)
    assert r.status_code == 409 and r.json() == {"motivo": "hash_distinto", "pista": "celu", "bytes_recibidos": 0}
    assert re.fullmatch(r"audio\.celu\.descartado-\d{8}T\d{6}\.ogg",
                        next(p.name for p in cfg.sesion_dir(sid).iterdir() if "descartado" in p.name))
    assert client.get(f"/api/sesiones/{sid}/audio?pista=celu").json() == {"bytes_recibidos": 0, "pista": "celu"}
    assert fila(cfg, sid)["estado"] == "grabando"   # el principal no se tocó
    subir(client, sid, celu, pista="celu")
    r = client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo(reloj, celu, desfase=-1500))
    assert r.status_code == 200 and r.json() == {"estado": "en_cola"}
    with Base(cfg.db_path).conexion() as conn:
        datos = sesiones.datos_celu(conn, sid)
        assert [j.tipo for j in cola.jobs_de_sesion(conn, sid)] == ["verificar"]
    assert datos["desfase_ms"] == -1500 and datos["desfase_origen"] == "app" and datos["bytes_totales"] == 40
    assert datos["ruta"] == "audio.celu.ogg" and datos["sha256"] == hashlib.sha256(celu).hexdigest()
    # Idempotente, y después de finalizar el celu tampoco acepta bytes.
    assert client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo(reloj, celu)).json() == {"estado": "en_cola"}
    assert subir(client, sid, b"x", offset=40, pista="celu").status_code == 409


def test_finalizar_sin_celu_es_como_siempre(client: TestClient, cfg):
    sid = crear(client)
    subir(client, sid, b"reloj")
    subir(client, sid, b"celu sin cerrar", pista="celu")
    assert client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo(b"reloj")).status_code == 200
    with Base(cfg.db_path).conexion() as conn:
        assert sesiones.datos_celu(conn, sid) is None   # sin el bloque `celu`, la pista no cuenta
    # Un celu de 0 bytes tampoco.
    sid = crear(client)
    subir(client, sid, b"reloj")
    assert client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo(b"reloj", b"")).status_code == 200
    with Base(cfg.db_path).conexion() as conn:
        assert sesiones.datos_celu(conn, sid) is None


# --- segmentar: tramos sin el principal ---------------------------------------------------------------

def test_tramos_donde_el_principal_no_tiene_audio():
    # El celu empieza 5 s antes y el principal se cortó a los 40 s (el celu sigue hasta los 60 s).
    assert segmentar.tramos_sin_principal(-5000, 60_000, 40_000) == [(0, 5000), (45_000, 60_000)]
    # El celu empieza después y termina antes: nada.
    assert segmentar.tramos_sin_principal(2000, 30_000, 40_000) == []
    # Menos de un segundo no cuenta.
    assert segmentar.tramos_sin_principal(-500, 40_000, 41_000) == []
    assert segmentar.recortar_a_tramos([(0, 30_000), (30_000, 60_000)], [(45_000, 60_000)]) == [(43_000, 60_000)]


def generar(ruta: Path, partes: list[tuple[str, float]]) -> None:
    """Tonos que cambian (voz) y silencio, en Opus."""
    entradas: list[str] = []
    for i, (tipo, dur) in enumerate(partes):
        fuente = (f"sine=f={300 + 70 * i}:d={dur}:sample_rate=48000" if tipo == "voz"
                  else f"anullsrc=r=48000:cl=mono:d={dur}")
        entradas += ["-f", "lavfi", "-i", fuente]
    filtro = "".join(f"[{i}:a]" for i in range(len(partes))) + f"concat=n={len(partes)}:v=0:a=1[out]"
    r = audio.ejecutar(["ffmpeg", "-hide_banner", "-v", "error", "-y", *entradas, "-filter_complex", filtro,
                        "-map", "[out]", "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(ruta)], timeout=120)
    assert r.returncode == 0, r.stderr


PARTES = [("voz", 7), ("sil", 2), ("voz", 4), ("sil", 3), ("voz", 9), ("sil", 1), ("voz", 5), ("sil", 4),
          ("voz", 6), ("sil", 2), ("voz", 8)]   # 51 s con un patrón de silencios irregular


@requiere("ffmpeg")
def test_desfase_por_correlacion(tmp_path):
    principal = tmp_path / "reloj.ogg"
    generar(principal, PARTES)
    # El celu empezó 3,4 s después que el reloj (y termina antes).
    otro = tmp_path / "celu.ogg"
    r = audio.ejecutar(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(principal), "-ss", "3.4",
                        "-t", "40", "-c:a", "libopus", str(otro)], timeout=60)
    assert r.returncode == 0
    d = correlacion.medir_envolventes(correlacion.normalizar(correlacion.envolvente(principal)),
                                      correlacion.normalizar(correlacion.envolvente(otro)))
    assert abs(d.desfase_ms - 3400) <= 30 and d.confianza > 1.15


def test_correlacion_sin_ffmpeg_y_sin_pico_claro():
    rng = random.Random(1)
    base: list[float] = []                              # 2 min a 100 Hz: tramos de energía, como el habla
    while len(base) < 12_000:
        base += [rng.random() ** 3] * rng.randint(20, 80)
    base = base[:12_000]
    otro = [x * 0.3 for x in base[2500:11_000]]         # empieza 25 s después y se oye más bajo
    d = correlacion.medir_envolventes(correlacion.normalizar(base), correlacion.normalizar(otro))
    assert d.desfase_ms == 25_000 and d.confianza > 1.15
    # El otro empieza antes que el principal: desfase negativo.
    d = correlacion.medir_envolventes(correlacion.normalizar(base[1000:]), correlacion.normalizar(base))
    assert d.desfase_ms == -10_000
    # Muy cortos para decidir, o sin nada en común.
    with pytest.raises(correlacion.ErrorCorrelacion):
        correlacion.medir_envolventes(base[:500], base[:500])
    otra_clase: list[float] = []
    while len(otra_clase) < 9000:
        otra_clase += [rng.random() ** 3] * rng.randint(20, 80)
    with pytest.raises(correlacion.ErrorCorrelacion):
        correlacion.medir_envolventes(correlacion.normalizar(base), correlacion.normalizar(otra_clase[:9000]))


@requiere("ffmpeg")
def test_segmentar_con_celu_y_respaldo(ctx, tmp_path):
    sid = "celu-seg-1"
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio=INICIO, dispositivo="reloj")
    carpeta = ctx.sesion_dir(sid)
    generar(carpeta / "audio.ogg", PARTES[:7])                     # el reloj se cortó a los 31 s
    generar(carpeta / "audio.celu.ogg", PARTES)                   # el celu siguió hasta el final
    with ctx.base.conexion() as conn:
        sesiones._asegurar_asset_celu(ctx.cfg, conn, sid)
        sesiones.guardar_datos_celu(conn, sid, {"cerrada": True, "desfase_ms": 0})
    # Revisión apagada (la de los tests): el celu sólo se corta donde el principal no tiene audio.
    r = segmentar.ejecutar(ctx, Job(id=1, tipo="segmentar", sesion_id=sid))
    with ctx.base.conexion() as conn:
        filas = [dict(f) for f in conn.execute("SELECT n, inicio_ms, fin_ms, ruta, pista FROM bloques "
                                               "WHERE sesion_id = ? ORDER BY n", (sid,))]
    principales = [f for f in filas if f["pista"] is None]
    del_celu = [f for f in filas if f["pista"] == "celu"]
    assert principales and all(f["n"] < 101 for f in principales)
    assert [f["n"] for f in del_celu] == [101] and del_celu[0]["inicio_ms"] >= 29_000
    assert del_celu[0]["ruta"] == "bloques_celu/bloque_101.mp3" and (carpeta / del_celu[0]["ruta"]).is_file()
    assert ("transcribir", {"n": 101}) in r.siguientes and "celu:" in r.log
    assert (carpeta / "silencios.celu.json").is_file() and leer_json(carpeta / "bloques.celu.json")[0]["n"] == 101
    # Con la revisión automática o asistida, todo el audio del celu.
    for tipo in ("automatica", "asistida"):
        ctx.cfg.materia("prueba").revision = tipo
        filas_celu, _ = segmentar.segmentar_celu(ctx, sid)
        assert filas_celu[0]["inicio_ms"] == 0 and filas_celu[-1]["fin_ms"] > 50_000, tipo
    # Apagada otra vez, recortado.
    ctx.cfg.materia("prueba").revision = "apagada"
    filas_celu, _ = segmentar.segmentar_celu(ctx, sid)
    assert [f["n"] for f in filas_celu] == [101] and filas_celu[0]["inicio_ms"] >= 29_000
    # Volver a segmentar el principal no borra los bloques del celu.
    segmentar.ejecutar(ctx, Job(id=2, tipo="segmentar", sesion_id=sid))
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bloques WHERE sesion_id = ? AND pista = 'celu'",
                            (sid,)).fetchone()[0] == 1


def test_un_celu_roto_no_traba_la_clase(ctx, monkeypatch):
    sid = "celu-seg-2"
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio=INICIO, dispositivo="reloj")
        sesiones._asegurar_asset_celu(ctx.cfg, conn, sid)
        sesiones.guardar_datos_celu(conn, sid, {"cerrada": True, "desfase_ms": 0})
    (ctx.sesion_dir(sid) / "audio.ogg").write_bytes(b"ogg")
    plan = segmentar.Segmentacion(60_000, [], [], [(0, 60_000)], [(0, 60_000)])
    monkeypatch.setattr(segmentar, "_segmentar_audio", lambda *a, **k: plan)
    monkeypatch.setattr(segmentar.audio, "extraer_mp3", lambda *a, **k: None)
    # No hay audio.celu.ogg: se avisa y sigue con el principal.
    r = segmentar.ejecutar(ctx, Job(id=1, tipo="segmentar", sesion_id=sid))
    assert r.siguientes == [("transcribir", {"n": 1})] and "celu salteado" in r.log


# --- agregar el audio del celu desde el panel ---------------------------------------------------------

@pytest.fixture
def panel(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50123),
                      follow_redirects=False)


@requiere("ffmpeg")
def test_agregar_audio_del_celu_desde_el_panel(ctx, client: TestClient, panel: TestClient, tmp_path):
    sid = crear(client)
    generar(tmp_path / "reloj.ogg", PARTES)
    datos = (tmp_path / "reloj.ogg").read_bytes()
    subir(client, sid, datos)
    # Grabando: todavía no.
    r = panel.post(f"/panel/clases/{sid}/celu", files={"audio": ("Voz 1.ogg", b"x", "audio/ogg")})
    assert "No+se+pudo" in r.headers["location"]
    assert client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo(datos)).status_code == 200
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE sesiones SET estado = 'lista' WHERE id = ?", (sid,))
        conn.execute("UPDATE jobs SET estado = 'hecho' WHERE sesion_id = ?", (sid,))
    # Algo que no es audio.
    r = panel.post(f"/panel/clases/{sid}/celu", files={"audio": ("nota.txt", b"hola", "text/plain")})
    assert "No+se+pudo" in r.headers["location"]
    with ctx.base.conexion() as conn:
        assert sesiones.datos_celu(conn, sid) is None
    # Un audio de verdad (m4a de la grabadora, por ejemplo).
    celu = tmp_path / "Voz (1).m4a"
    r0 = audio.ejecutar(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(tmp_path / "reloj.ogg"),
                         "-ss", "2", "-c:a", "aac", str(celu)], timeout=60)
    assert r0.returncode == 0
    r = panel.post(f"/panel/clases/{sid}/celu", files={"audio": (celu.name, celu.read_bytes(), "audio/mp4")})
    assert r.status_code == 303 and "Audio+del+celu+agregado" in r.headers["location"], r.headers["location"]
    with ctx.base.conexion() as conn:
        datos_celu = sesiones.datos_celu(conn, sid)
        trabajos = [(j.tipo, j.args, j.estado) for j in cola.jobs_de_sesion(conn, sid) if j.estado == "pendiente"]
    assert datos_celu["ruta"] == "audio.celu.m4a" and datos_celu["origen"] == "panel"
    assert datos_celu["desfase_ms"] is None and datos_celu["nombre_original"] == "Voz (1).m4a"
    assert trabajos == [("segmentar", {"pista": "celu"}, "pendiente")]
    assert fila(ctx.cfg, sid)["estado"] == "en_cola"
    html = panel.get(f"/panel/clases/{sid}").text
    assert 'id="audio-celu"' in html and "desfase por medir" in html
    assert panel.get(f"/panel/clases/{sid}/audio-celu").status_code == 200
    # El job: mide el desfase por correlación y corta el celu.
    r = segmentar.ejecutar(ctx, Job(id=9, tipo="segmentar", sesion_id=sid, args={"pista": "celu"}))
    with ctx.base.conexion() as conn:
        datos_celu = sesiones.datos_celu(conn, sid)
    assert abs(datos_celu["desfase_ms"] - 2000) <= 40 and datos_celu["desfase_origen"] == "correlacion"
    assert json.dumps(datos_celu["correlacion"])
    # Con la revisión apagada y el reloj con audio en toda la clase no hay nada que transcribir del celu:
    # el pipeline sigue igual (linea_de_tiempo).
    assert r.siguientes == [("linea_de_tiempo", {})] and "nada" in r.log
    ctx.cfg.materia("prueba").revision = "automatica"
    r = segmentar.ejecutar(ctx, Job(id=10, tipo="segmentar", sesion_id=sid, args={"pista": "celu"}))
    assert r.siguientes and all(a["n"] >= 101 for _, a in r.siguientes)


def test_el_boton_del_celu_pide_confirmacion(ctx, client: TestClient, panel: TestClient, tmp_path):
    """El confirm() con el costo tiene que quedar entero en el atributo: con comillas dobles, el JSON lo
    cortaba en `confirm(`, el navegador descartaba el handler y el audio se subía sin preguntar."""
    from html.parser import HTMLParser

    sid = crear(client)
    generar(tmp_path / "reloj.ogg", PARTES)
    datos = (tmp_path / "reloj.ogg").read_bytes()
    subir(client, sid, datos)
    assert client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo(datos)).status_code == 200
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE sesiones SET estado = 'lista' WHERE id = ?", (sid,))

    class Formularios(HTMLParser):
        onsubmit: str | None = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form" and (a.get("action") or "").endswith("/celu"):
                self.onsubmit = a.get("onsubmit")

    p = Formularios()
    p.feed(panel.get(f"/panel/clases/{sid}").text)
    assert p.onsubmit is not None
    assert p.onsubmit.startswith("return confirm(") and p.onsubmit.endswith(");")
    texto = json.loads(p.onsubmit[len("return confirm("):-len(");")])
    assert texto.startswith("Se va a transcribir el audio del celu") and texto.endswith("¿Seguir?")
