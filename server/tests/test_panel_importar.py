"""Panel, "Importar clase" (§13): el formulario, Analizar (el área temporal), Importar y procesar,
Cancelar y lo vencido, la marca "importada" en la lista y en la clase, y el reproductor con la copia."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from classmer import importador, sesiones
from classmer.panel import importar as panel_importar
from classmer.pipeline import actualizar_sesion
from classmer.util import leer_json
from test_importador import _ffmpeg, jpeg_bytes
from test_panel import CLAVE_ANTHROPIC, LOCAL, pdf_de, poner_claves

DUR_MS = 180_000
# Un audio de WhatsApp: el creation_time es la hora del envío (14:45) y la fecha del archivo, la de
# cuando se bajó (15:30). La clase empezó a las 14:00: lo dicen las fotos.
CREATION_TIME = "2026-10-20T17:45:00Z"
MODIFICADO_MS = 1_792_521_000_000            # 2026-10-20T15:30:00-03:00 (File.lastModified)
NOMBRE_M4A = "Audio de WhatsApp 2026-10-20 a las 14.45.m4a"
FOTO_1 = jpeg_bytes("2026:10:20 14:01:00", "-03:00")
FOTO_2 = jpeg_bytes("2026:10:20 14:02:30", "-03:00", color="navy")
FOTO_OTRO_DIA = jpeg_bytes("2026:10:19 10:00:00", "-03:00", color="gray")
FOTO_SIN_EXIF = jpeg_bytes(color="white")


@pytest.fixture
def panel(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url=LOCAL, client=("127.0.0.1", 50124), follow_redirects=False)


@pytest.fixture(scope="module")
def audios(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("falta ffmpeg/ffprobe")
    d = tmp_path_factory.mktemp("audios-panel")
    seno = f"sine=frequency=440:sample_rate=16000:duration={DUR_MS // 1000}"
    m4a = d / "whatsapp.m4a"
    _ffmpeg("-f", "lavfi", "-i", seno, "-ac", "1", "-c:a", "aac", "-b:a", "16k",
            "-metadata", f"creation_time={CREATION_TIME}", str(m4a))
    opus = d / "whatsapp.opus"
    _ffmpeg("-f", "lavfi", "-i", seno, "-ac", "1", "-c:a", "libopus", "-b:a", "16k", str(opus))
    return {"m4a": m4a.read_bytes(), "opus": opus.read_bytes()}


def analizar(panel: TestClient, audio: bytes | None = None, *, nombre: str = NOMBRE_M4A, fotos=(),
             modificado: int | None = MODIFICADO_MS, token: str | None = None, **kwargs):
    """Como lo manda el JS: el token (si hay) antes que los archivos, el audio y las fotos."""
    data = {}
    if token is not None:
        data["token"] = token
    if audio is not None and modificado is not None:
        data["audio_modificado"] = str(modificado)
    files = []
    if audio is not None:
        files.append(("audio", (nombre, audio, "audio/mp4")))
    files += [("fotos", (n, c, "image/jpeg")) for n, c in fotos]
    if not files:  # un input sin archivo elegido: el navegador manda la parte vacía (y así es multipart)
        files.append(("fotos", ("", b"", "application/octet-stream")))
    return panel.post("/panel/importar/analizar", data=data, files=files, **kwargs)


def importar(panel: TestClient, token: str | None, *, material=(), **campos):
    data = {"token": token} if token is not None else {}
    data.update({"materia_id": "prueba", "tipo_clase": "teorica", "numero_clase": "3",
                 "inicio": "2026-10-20T14:00:00"})
    data.update(campos)
    data = {k: v for k, v in data.items() if v is not None}
    files = [("material", (n, c, t)) for n, c, t in material] or [("material", ("", b"", "application/octet-stream"))]
    return panel.post("/panel/importar", data=data, files=files)


def temporales(cfg) -> list[str]:
    raiz = cfg.datos / importador.DIR_TEMPORAL
    return sorted(p.name for p in raiz.iterdir()) if raiz.is_dir() else []


def contar_sesiones(base) -> int:
    with base.conexion() as conn:
        return conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0]


def flash(url: str) -> tuple[str, str]:
    consulta = parse_qs(urlsplit(url).query)
    return consulta["msg"][0], consulta["tipo"][0]


def datos_js(html: str) -> dict:
    return json.loads(re.search(r"const DATOS = (.*);\n", html).group(1))


# --- el formulario -------------------------------------------------------------------------------

def test_boton_en_clases_y_formulario(client: TestClient, panel: TestClient):
    html = panel.get("/panel/clases").text
    assert '<a class="boton primario" href="/panel/importar">Importar clase</a>' in html
    r = panel.get("/panel/importar")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert 'href="/panel/clases" class="activo"' in html  # es parte de Clases
    # Audio: lo que lee ffmpeg; fotos: imágenes; material: lo de los adjuntos.
    acepta = re.search(r'id="audio" accept="([^"]+)"', html).group(1).split(",")
    for tipo in ("audio/*", "video/mp4", ".opus", ".ogg", ".m4a", ".mp4", ".mp3", ".wav", ".aac", ".amr", ".3gp",
                 ".wma", ".flac", ".webm"):
        assert tipo in acepta, tipo
    assert 'id="fotos" multiple accept="image/*"' in html
    assert f'id="material" multiple accept="{panel_importar.ACEPTA_AUDIO}"' not in html
    assert 'id="material" multiple accept="application/pdf,image/jpeg' in html
    # Materia: Auto y las activas; el número por defecto es el próximo de cada una.
    assert '<option value="auto">Auto (IA)</option>' in html
    assert '<option value="prueba">Materia de Prueba</option>' in html and '<option value="otra">Otra Materia</option>' in html
    assert 'type="datetime-local" id="inicio" step="1"' in html
    datos = datos_js(html)
    assert [(m["id"], m["proximo"]) for m in datos["materias"]] == [("prueba", 1), ("otra", 1)]
    assert datos["materias"][0]["tipos"] == [{"valor": "teorica", "texto": "Teórica"},
                                             {"valor": "practica", "texto": "Práctica"}]
    assert [t["valor"] for t in datos["tipos_auto"]] == ["teorica", "practica"]
    assert datos["max_audio"] == 2 * 1024 ** 3 and datos["max_foto"] == sesiones.MAX_FOTO
    # El JS: subida con progreso, Analizar, Importar, Cancelar y el confirm con el costo.
    for esperado in ("xhr.upload.onprogress", "'/panel/importar/analizar'", "'/panel/importar'",
                     "'/cancelar'", "confirm(DATOS.confirmacion)", "sendBeacon", "numero.disabled = !m"):
        assert esperado in html, esperado


def test_confirmacion_con_el_costo(home: Path, client: TestClient, panel: TestClient):
    # Sin apuntes con costo y sin clave (va por el plan Max).
    texto = datos_js(panel.get("/panel/importar").text)["confirmacion"]
    assert texto.startswith("Se va a generar un apunte nuevo de esta clase con Claude.")
    assert "Facturación: API y, si se queda sin créditos, plan Max." in texto
    assert "Ahora va por el plan Max: no se cobra aparte (serían alrededor de US$ 1–2 con la API)." in texto
    assert texto.endswith("¿Importar y procesar?")
    # Con el costo de los últimos apuntes (los que lo tienen) y la API.
    with client.app.state.base.conexion() as conn:
        sesiones.crear(client.app.state.cfg, conn, sesion_id="con-costo", materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio="2026-10-15T11:30:00-03:00", dispositivo="s23")
        for version, costo in ((1, 1.0), (2, None), (3, 2.0), (4, 0.0)):
            conn.execute("INSERT INTO apuntes (sesion_id, version, costo, creado) VALUES ('con-costo', ?, ?, '')",
                         (version, costo))
    poner_claves(client.app, home)
    texto = datos_js(panel.get("/panel/importar").text)["confirmacion"]
    assert "Ahora va por la API: unos US$ 1.50 con la API (el promedio de los últimos 2 apuntes)." in texto
    assert CLAVE_ANTHROPIC not in panel.get("/panel/importar").text


# --- Analizar --------------------------------------------------------------------------------------

@pytest.mark.ffmpeg
def test_analizar(client: TestClient, panel: TestClient, audios, monkeypatch: pytest.MonkeyPatch):
    cfg = client.app.state.cfg
    # Los archivos se escriben directo en el área temporal: nada pasa por el parser de Starlette (que
    # los manda a la carpeta temporal del sistema).
    import starlette.formparsers

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", None)
    r = analizar(panel, audios["m4a"], fotos=[("IMG_1.jpg", FOTO_1), ("pizarrón sin exif.jpg", FOTO_SIN_EXIF)])
    assert r.status_code == 200, r.text
    d = r.json()
    token = d["token"]
    assert re.fullmatch(r"[0-9a-f]{32}", token) and temporales(cfg) == [token]
    a = d["audio"]
    assert a["nombre"] == NOMBRE_M4A and a["bytes"] == len(audios["m4a"]) and abs(a["duracion_ms"] - DUR_MS) < 1500
    assert a["extension"] == ".m4a" and a["codec"] == "aac" and a["reproducible"] is True
    # El inicio sale de las fotos (el audio dice 14:45, la hora del envío): 1 min antes de la primera.
    assert d["origen_inicio"] == "exif_fotos" and d["inicio"] == "2026-10-20T14:00:00-03:00"
    assert d["inicio_local"] == "2026-10-20T14:00:00" and d["offset_zona"] == "-03:00"
    candidatos = {c["origen"]: c for c in d["candidatos_inicio"]}
    assert d["candidatos_inicio"][0]["origen"] == "exif_fotos" and d["candidatos_inicio"][0]["elegido"] is True
    assert candidatos["exif_fotos"]["texto"] == "mar 20/10/2026 14:00:00"
    assert candidatos["mtime_audio"]["inicio_local"] == "2026-10-20T15:27:00"  # lastModified − duración
    assert candidatos["creation_time"]["inicio_local"] == "2026-10-20T14:45:00"
    assert "ojo, en WhatsApp suele ser la hora del envío" in candidatos["creation_time"]["etiqueta"]
    assert "ojo, puede ser la hora en que se mandó o se bajó el archivo" in candidatos["mtime_audio"]["etiqueta"]
    # Las fotos, con el nombre original: la de EXIF en su minuto, la otra sin hora.
    (con, sin) = d["fotos"]
    assert (con["nombre"], con["hora"], con["minuto"], con["offset_ms"]) == (
        "IMG_1.jpg", "2026-10-20T14:01:00-03:00", "00:01:00", 60_000)
    assert (sin["nombre"], sin["hora"], sin["minuto"]) == ("pizarrón sin exif.jpg", None, None)
    assert d["resumen_fotos"] == {"total": 2, "con_hora": 1, "sin_hora": 1, "fuera_de_rango": 0, "ilegibles": 0}
    assert any("no tienen hora EXIF" in x for x in d["avisos"])
    # Quedó en Classmer-data/importaciones/<token>/, con nombres saneados; no se creó ninguna sesión.
    carpeta = cfg.datos / "importaciones" / token
    assert (carpeta / "audio" / "000" / NOMBRE_M4A).read_bytes() == audios["m4a"]
    assert (carpeta / "fotos" / "000" / "IMG_1.jpg").read_bytes() == FOTO_1
    assert (carpeta / "fotos" / "001" / "pizarrón sin exif.jpg").is_file()
    guardado = leer_json(carpeta / "analisis.json")
    assert guardado["audio"] == {"archivo": f"audio/000/{NOMBRE_M4A}", "nombre": NOMBRE_M4A,
                                 "modificado_ms": MODIFICADO_MS}
    assert [f["nombre"] for f in guardado["fotos"]] == ["IMG_1.jpg", "pizarrón sin exif.jpg"]
    assert guardado["resultado"]["token"] == token
    assert contar_sesiones(client.app.state.base) == 0


@pytest.mark.ffmpeg
def test_analizar_opus_sin_fotos(client: TestClient, panel: TestClient, audios):
    r = analizar(panel, audios["opus"], nombre="Audio de WhatsApp 2026-10-20 a las 14.45.opus")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["audio"]["extension"] == ".opus" and d["audio"]["codec"] == "opus" and d["audio"]["reproducible"]
    # Sin fotos ni metadatos: la fecha del archivo menos la duración (la del opus mide unos ms de más), con el aviso.
    assert d["origen_inicio"] == "mtime_audio" and d["inicio_local"] in ("2026-10-20T15:26:59", "2026-10-20T15:27:00")
    assert d["resumen_fotos"]["total"] == 0 and d["fotos"] == []
    assert any("WhatsApp" in a for a in d["avisos"])


@pytest.mark.ffmpeg
def test_analizar_audio_ilegible(client: TestClient, panel: TestClient):
    r = analizar(panel, b"esto no es un audio" * 100, nombre="clase.m4a", fotos=[("IMG_1.jpg", FOTO_1)])
    assert r.status_code == 422
    assert r.json() == {"detail": "No se puede importar: clase.m4a no es un audio legible (ffmpeg no lo reconoce). "
                                  "¿Es el archivo que era?", "campo": "audio"}  # sin la salida de ffprobe ni rutas
    assert temporales(client.app.state.cfg) == []  # no queda nada


def test_analizar_sin_audio(client: TestClient, panel: TestClient):
    cfg = client.app.state.cfg
    for r in (analizar(panel, fotos=[("IMG_1.jpg", FOTO_1)]), analizar(panel),
              panel.post("/panel/importar/analizar", files=[("audio", ("", b"", "application/octet-stream"))])):
        assert r.status_code == 422 and r.json() == {"detail": "Elegí el audio de la clase.", "campo": "audio"}
    r = panel.post("/panel/importar/analizar", json={"audio": "x"})
    assert r.status_code == 422 and "multipart/form-data" in r.json()["detail"]
    assert temporales(cfg) == []


def test_analizar_mas_de_2_gib(client: TestClient, panel: TestClient, monkeypatch: pytest.MonkeyPatch):
    # 2 GiB simulados: el tope se aplica mientras llega, antes de que lo mire ffprobe.
    monkeypatch.setitem(importador.TOPES_TEMPORAL, "audio", 1000)
    r = analizar(panel, b"\x00" * 5000, fotos=[("IMG_1.jpg", FOTO_1)])
    assert r.status_code == 413
    assert r.json() == {"detail": "El audio pesa más de 2 GB: el máximo para importar es 2 GB.", "campo": "audio"}
    assert temporales(client.app.state.cfg) == []


@pytest.mark.ffmpeg
def test_analizar_fotos_con_y_sin_exif(client: TestClient, panel: TestClient, audios, monkeypatch: pytest.MonkeyPatch):
    normales = (FOTO_1, FOTO_2, FOTO_OTRO_DIA, FOTO_SIN_EXIF)
    monkeypatch.setitem(importador.TOPES_TEMPORAL, "fotos", max(len(f) for f in normales) + 100)
    grande = FOTO_2 + b"\x00" * 5000  # 50 MB simulados
    r = analizar(panel, audios["m4a"], fotos=[
        ("IMG_2.jpg", FOTO_2), ("IMG_1.jpg", FOTO_1), ("ayer.jpg", FOTO_OTRO_DIA), ("sin_exif.jpg", FOTO_SIN_EXIF),
        ("roto.jpg", b"no es una imagen"), ("grande.jpg", grande)])
    assert r.status_code == 200, r.text
    d = r.json()
    # El inicio sale de las dos que caen juntas; la del otro día queda fuera de la grabación.
    assert d["origen_inicio"] == "exif_fotos" and d["inicio_local"] == "2026-10-20T14:00:00"
    fotos = {f["nombre"]: f for f in d["fotos"]}
    assert [f["nombre"] for f in d["fotos"]] == ["ayer.jpg", "IMG_1.jpg", "IMG_2.jpg", "sin_exif.jpg"]
    assert fotos["IMG_1.jpg"]["minuto"] == "00:01:00" and fotos["IMG_2.jpg"]["minuto"] == "00:02:30"
    assert fotos["ayer.jpg"]["fuera_de_rango"] is True and fotos["ayer.jpg"]["hora_texto"] == "19/10 10:00:00"
    assert fotos["sin_exif.jpg"]["hora"] is None and fotos["sin_exif.jpg"]["indice"] == 3
    ilegibles = {f["nombre"]: f["motivo"] for f in d["fotos_ilegibles"]}
    assert set(ilegibles) == {"roto.jpg", "grande.jpg"} and "pesa más de" in ilegibles["grande.jpg"]
    assert d["resumen_fotos"] == {"total": 6, "con_hora": 3, "sin_hora": 1, "fuera_de_rango": 1, "ilegibles": 2}
    # La que pasó el tope no quedó en disco.
    carpeta = client.app.state.cfg.datos / "importaciones" / d["token"]
    assert sorted(p.name for p in (carpeta / "fotos").rglob("*.jpg")) == [
        "IMG_1.jpg", "IMG_2.jpg", "ayer.jpg", "roto.jpg", "sin_exif.jpg"]


@pytest.mark.ffmpeg
def test_volver_a_analizar_con_otras_fotos(client: TestClient, panel: TestClient, audios):
    cfg = client.app.state.cfg
    token = analizar(panel, audios["m4a"], fotos=[("IMG_1.jpg", FOTO_1)]).json()["token"]
    carpeta = cfg.datos / "importaciones" / token
    # Cambiaron las fotos: sólo van el token y las fotos nuevas (el audio no se vuelve a subir).
    r = analizar(panel, token=token, fotos=[("IMG_2.jpg", FOTO_2), ("sin_exif.jpg", FOTO_SIN_EXIF)])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["token"] == token and temporales(cfg) == [token] and d["audio"]["nombre"] == NOMBRE_M4A
    assert [f["nombre"] for f in d["fotos"]] == ["IMG_2.jpg", "sin_exif.jpg"]
    assert d["inicio_local"] == "2026-10-20T14:01:30"  # 1 min antes de IMG_2
    assert not (carpeta / "fotos" / "000" / "IMG_1.jpg").exists()
    assert "mtime_audio" in {c["origen"] for c in d["candidatos_inicio"]}  # el lastModified de la primera vez
    # Sin fotos: se sacaron todas.
    d = analizar(panel, token=token).json()
    assert d["fotos"] == [] and d["origen_inicio"] == "mtime_audio" and not (carpeta / "fotos").exists()
    assert (carpeta / "audio" / "000" / NOMBRE_M4A).is_file()


# --- Importar y procesar ----------------------------------------------------------------------------

@pytest.mark.ffmpeg
def test_importar(client: TestClient, panel: TestClient, audios):
    app, cfg = client.app, client.app.state.cfg
    d = analizar(panel, audios["m4a"], fotos=[("IMG_1.jpg", FOTO_1), ("sin_exif.jpg", FOTO_SIN_EXIF)]).json()
    slides = pdf_de(2)
    r = importar(panel, d["token"], materia_id="prueba", tipo_clase="practica", numero_clase="7",
                 inicio="2026-10-20T13:58:30",  # corregido a mano
                 material=[("Slides clase 7.pdf", slides, "application/pdf"), ("notas.txt", b"hola\n", "text/plain")])
    assert r.status_code == 201, r.text
    j = r.json()
    sid = j["id"]
    assert str(uuid.UUID(sid)) == sid and j["estado"] == "en_cola"
    assert j["url"].startswith(f"/panel/clases/{sid}?")
    msg, tipo = flash(j["url"])
    assert tipo == "aviso" and msg.startswith("Clase importada: quedó en cola para procesar. Material: 1 adjunto.")
    assert "No entraron: notas.txt (tipo de archivo no soportado" in msg
    assert [m["nombre"] for m in j["material_rechazado"]] == ["notas.txt"]
    assert [m["nombre_original"] for m in j["material"]] == ["Slides clase 7.pdf"]

    # La sesión importada, con su número, materia, tipo y el inicio del formulario.
    with app.state.base.conexion() as conn:
        s = sesiones.fila_sesion(conn, sid)
        adj = conn.execute("SELECT * FROM assets WHERE sesion_id = ? AND tipo = 'adjunto'", (sid,)).fetchall()
        fotos = {json.loads(f["datos"])["nombre_original"]: f["offset_ms"] for f in conn.execute(
            "SELECT * FROM assets WHERE sesion_id = ? AND tipo = 'foto'", (sid,))}
    assert (s["importada"], s["dispositivo"], s["numero_clase"], s["materia_id"], s["tipo_clase"], s["estado"]) == (
        1, "panel", 7, "prueba", "practica", "en_cola")
    assert s["inicio"] == "2026-10-20T13:58:30-03:00" and s["origen_materia"] == "manual"
    assert fotos == {"IMG_1.jpg": 150_000, "sin_exif.jpg": None}
    # El material, como adjunto de la clase (origen panel).
    (a,) = adj
    assert json.loads(a["datos"])["origen"] == "panel" and a["ruta"] == "adjuntos/Slides_clase_7.pdf"
    assert (cfg.sesion_dir(sid) / "adjuntos" / "Slides_clase_7.pdf").read_bytes() == slides
    # importacion.json: inicio puesto a mano, con los candidatos que se mostraron.
    imp = leer_json(cfg.sesion_dir(sid) / "importacion.json")
    assert imp["origen_inicio"] == "parametro" and imp["inicio_elegido"] == "manual"
    assert imp["candidatos_inicio"][0]["origen"] == "exif_fotos" and "inicio_local" not in imp["candidatos_inicio"][0]
    assert imp["audio"]["nombre_original"] == NOMBRE_M4A and imp["audio"]["archivo"] == "audio.m4a"
    assert [m["nombre"] for m in imp["material_rechazado"]] == ["notas.txt"]
    with app.state.base.conexion() as conn:
        datos_audio = json.loads(conn.execute("SELECT datos FROM assets WHERE id = ?",
                                              (sesiones.id_audio(sid),)).fetchone()["datos"])
    assert datos_audio["nombre_original"] == NOMBRE_M4A
    # El análisis se borró.
    assert temporales(cfg) == []

    # La página de la clase: el mensaje, la marca, el resumen y el reproductor (m4a, con Range).
    html = panel.get(j["url"]).text
    assert "Clase importada: quedó en cola para procesar." in html and "notas.txt" in html
    assert 'class="etiqueta"' in html and ">importada</span>" in html
    resumen = html[html.index('class="tarjeta importacion"'):html.index('class="tarjeta reproductor"')]
    assert "desde el panel" in resumen and NOMBRE_M4A in resumen
    assert "puesto a mano en el panel (el server proponía otro: según las fotos (EXIF): 1 min antes" in resumen
    assert "1 ubicada por su hora · 1 sin hora (al final, sin minuto) · 0 fuera de la grabación" in resumen
    assert "Material: 1 adjunto · no entraron: notas.txt (tipo de archivo no soportado" in resumen
    assert "archivo(s) de material no entraron" not in resumen  # el aviso del importador, ya dicho con el motivo
    assert "Slides clase 7.pdf" in html
    r = panel.get(f"/panel/clases/{sid}/audio", headers={"Range": "bytes=1000-1999"})
    assert r.status_code == 206 and r.headers["content-type"] == "audio/mp4"
    assert r.content == audios["m4a"][1000:2000]


@pytest.mark.ffmpeg
def test_importar_en_auto_con_un_candidato(client: TestClient, panel: TestClient, audios):
    app, cfg = client.app, client.app.state.cfg
    d = analizar(panel, audios["m4a"]).json()
    creacion = next(c for c in d["candidatos_inicio"] if c["origen"] == "creation_time")
    # En Auto el número no cuenta (el input está deshabilitado; si llega igual, se ignora).
    r = importar(panel, d["token"], materia_id="auto", tipo_clase="teorica", numero_clase="5",
                 inicio=creacion["inicio_local"])
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert flash(r.json()["url"]) == ("Clase importada: quedó en cola para procesar.", "ok")
    with app.state.base.conexion() as conn:
        s = sesiones.fila_sesion(conn, sid)
    assert (s["materia_id"], s["numero_clase"], s["origen_materia"], s["tipo_clase"]) == (None, None, "auto", "teorica")
    assert s["inicio"] == "2026-10-20T14:45:00-03:00"
    imp = leer_json(cfg.sesion_dir(sid) / "importacion.json")
    assert imp["inicio_elegido"] == "creation_time" and imp["material"] == []
    html = panel.get(f"/panel/clases/{sid}").text
    assert "elegido en el panel: según los metadatos del audio: ojo, en WhatsApp suele ser la hora del envío" in html
    assert "Fotos: ninguna." in html


@pytest.mark.ffmpeg
def test_importar_errores_sin_perder_el_analisis(client: TestClient, panel: TestClient, audios):
    app, cfg = client.app, client.app.state.cfg
    token = analizar(panel, audios["m4a"]).json()["token"]
    carpeta = cfg.datos / "importaciones" / token
    casos = [
        (dict(token=None), "token", "Falta el análisis: elegí el audio y esperá a que se analice."),
        (dict(token="0" * 32), "token", "El análisis ya no está (venció o se canceló): volvé a subir los archivos."),
        (dict(token="../../config.yaml"), "token", "Análisis inválido."),
        (dict(materia_id="no-existe"), "materia_id", "La materia 'no-existe' no existe."),
        (dict(tipo_clase="laboratorio"), "tipo_clase",
         "Tipo de clase inválido: 'laboratorio' (opciones: teorica, practica)."),
        (dict(inicio=""), "inicio", "Poné el inicio de la clase (fecha y hora)."),
        (dict(inicio="mañana a las 3"), "inicio", "Inicio inválido: 'mañana a las 3'."),
        (dict(numero_clase="siete"), "numero_clase", "Número de clase inválido: 'siete'."),
        (dict(numero_clase="-1"), "numero_clase", "El número de clase no puede ser negativo."),
    ]
    for campos, campo, detalle in casos:
        campos = {"token": token, **campos}
        r = importar(panel, campos.pop("token"), material=[("Slides.pdf", pdf_de(1), "application/pdf")], **campos)
        assert r.status_code == 422, (campos, r.text)
        assert r.json() == {"detail": detalle, "campo": campo}, campos
    # Nada se creó, el análisis sigue (para corregir y volver a mandar) y el material del intento no queda.
    assert contar_sesiones(app.state.base) == 0
    assert (carpeta / "analisis.json").is_file() and not (carpeta / "material").exists()
    assert (cfg.home / "config.yaml").is_file()
    r = importar(panel, token, numero_clase="")  # vacío: el próximo de la materia
    assert r.status_code == 201, r.text
    with app.state.base.conexion() as conn:
        assert sesiones.fila_sesion(conn, r.json()["id"])["numero_clase"] == 1


# --- Cancelar, lo vencido y los tokens --------------------------------------------------------------

def test_cancelar_y_los_vencidos(client: TestClient, panel: TestClient):
    cfg = client.app.state.cfg
    token, carpeta = importador.crear_temporal(cfg)
    (carpeta / "audio").mkdir()
    (carpeta / "audio" / "clase.m4a").write_bytes(b"x")
    r = panel.post(f"/panel/importar/{token}/cancelar")
    assert r.status_code == 200 and r.json() == {"ok": True, "borrado": True} and not carpeta.exists()
    assert panel.post(f"/panel/importar/{token}/cancelar").json() == {"ok": True, "borrado": False}
    # Lo abandonado hace más de 24 h se borra (también al abrir el formulario, por si no hay worker).
    viejo_token, viejo = importador.crear_temporal(cfg)
    (viejo / "foto.jpg").write_bytes(b"x")
    nuevo_token, _ = importador.crear_temporal(cfg)
    hace = time.time() - 25 * 3600
    for p in (viejo / "foto.jpg", viejo):
        os.utime(p, (hace, hace))
    assert panel.get("/panel/importar").status_code == 200
    assert temporales(cfg) == [nuevo_token]


def test_tokens_invalidos_y_path_traversal(client: TestClient, panel: TestClient):
    cfg = client.app.state.cfg
    token, carpeta = importador.crear_temporal(cfg)
    for malo in ("ZZZ", token.upper(), "a" * 31, "..", "%2E%2E", "..%2F..%2Fconfig.yaml", "..%5C..%5Cconfig.yaml"):
        r = panel.post(f"/panel/importar/{malo}/cancelar")
        assert r.status_code == 404, malo
    assert carpeta.is_dir() and (cfg.home / "config.yaml").is_file() and (cfg.home / "classmer.db").is_file()
    # En Analizar, un token que no es del área temporal no abre nada afuera.
    for malo in ("../../sesiones", "..\\..\\config.yaml", token[:-1] + "/"):
        r = analizar(panel, token=malo, fotos=[("IMG_1.jpg", FOTO_1)])
        assert r.status_code == 422 and r.json()["campo"] == "token", malo
        assert r.json()["detail"] == "Análisis inválido."
    r = analizar(panel, token="f" * 32, fotos=[("IMG_1.jpg", FOTO_1)])  # bien formado, pero no está
    assert r.status_code == 422 and "ya no está" in r.json()["detail"]
    assert temporales(cfg) == [token] and not any(cfg.datos.glob("**/IMG_1.jpg"))


def test_rutas_nuevas_solo_desde_la_pc(client: TestClient, panel: TestClient):
    token, _ = importador.crear_temporal(client.app.state.cfg)
    lan = TestClient(client.app, base_url="http://192.168.1.10:8000", client=("192.168.1.20", 40000))
    assert lan.get("/panel/importar").status_code == 403
    for ruta in ("/panel/importar/analizar", "/panel/importar", f"/panel/importar/{token}/cancelar"):
        assert lan.post(ruta, files={"audio": ("a.m4a", b"x")}).status_code == 403, ruta
        # Desde la PC, pero mandado por otra página abierta en el navegador.
        r = panel.post(ruta, files={"audio": ("a.m4a", b"x")}, headers={"Origin": "http://malo.example"})
        assert r.status_code == 403, ruta
    assert temporales(client.app.state.cfg) == [token]
    rebinding = TestClient(client.app, base_url="http://malo.example:8000", client=("127.0.0.1", 1))
    assert rebinding.get("/panel/importar").status_code == 403


# --- la marca en la lista y en la clase; el reproductor ----------------------------------------------

def crear_sesion(app, sid: str, *, importada: bool, audio_nombre: str = "audio.ogg", dispositivo: str = "s23"):
    with app.state.base.conexion() as conn:
        sesiones.crear(app.state.cfg, conn, sesion_id=sid, materia_id="prueba", origen_materia="calendario",
                       tipo_clase="teorica", inicio="2026-10-20T14:00:00-03:00", dispositivo=dispositivo,
                       audio_nombre=audio_nombre, importada=importada, audio_original="Audio de WhatsApp.amr")
    return app.state.cfg.sesion_dir(sid)


def test_marca_importada_en_la_lista_y_en_la_clase(client: TestClient, panel: TestClient):
    app = client.app
    crear_sesion(app, "importada-1", importada=True)      # la app la está subiendo
    crear_sesion(app, "normal-1", importada=False)        # una grabación en curso
    html = panel.get("/panel/clases").text

    def fila(sid: str) -> str:
        return html[html.index(f'href="/panel/clases/{sid}"'):].split("</tr>")[0]

    assert ">importada</span>" in fila("importada-1") and ">subiendo</span>" in fila("importada-1")
    assert ">importada</span>" not in fila("normal-1") and ">grabando</span>" in fila("normal-1")
    html = panel.get("/panel/clases/importada-1").text
    assert ">importada</span>" in html and ">subiendo</span>" in html and ">grabando</span>" not in html
    assert "La app está subiendo el audio de esta clase importada." in html
    assert "Sin jobs todavía (la app todavía está subiendo el audio)." in html
    assert 'class="tarjeta importacion"' not in html  # todavía no hay importacion.json
    html = panel.get("/panel/clases/normal-1").text
    assert ">importada</span>" not in html and ">grabando</span>" in html

    # Ya verificada: importacion.json (como lo escribe verificar para la app).
    with app.state.base.conexion() as conn:
        actualizar_sesion(conn, "importada-1", app.state.cfg.zona_horaria, estado="procesando", etapa="segmentar")
    (app.state.cfg.sesion_dir("importada-1") / "importacion.json").write_text(json.dumps({
        "audio": {"nombre_original": "Audio de WhatsApp.amr", "archivo": "audio.amr", "bytes": 1_234_567,
                  "formato": "amr", "codec": "amr_nb"},
        "inicio": "2026-10-20T14:00:00-03:00", "origen_inicio": "calendario",
        "fotos": [{"id": "a", "offset_ms": 60_000, "origen_hora": "exif"},
                  {"id": "b", "offset_ms": 0, "origen_hora": "exif", "fuera_de_rango": True},
                  {"id": "c", "offset_ms": 90_000, "origen_hora": "exif"},
                  {"id": "d", "offset_ms": None, "origen_hora": None}],
        "fotos_ilegibles": [{"nombre": "pizarra.heic", "motivo": "formato no soportado (¿HEIC?)"}],
        "avisos": ["1 de 4 fotos quedaron fuera de la grabación: revisá el inicio"],
        "importado": "2026-10-20T16:00:00-03:00", "via": "app", "otra_clave": {"se": "ignora"},
    }), encoding="utf-8")
    html = panel.get("/panel/clases/importada-1").text
    resumen = html[html.index('class="tarjeta importacion"'):html.index('class="tarjeta reproductor"')]
    for esperado in ("Clase importada", "desde la app", "Audio de WhatsApp.amr", "(1,2 MB, amr)",
                     "Inicio: 2026-10-20 14:00, el evento del calendario elegido en la app.",
                     "2 ubicadas por su hora · 1 sin hora (al final, sin minuto) · 1 fuera de la grabación",
                     "1 ilegible: pizarra.heic", "1 de 4 fotos quedaron fuera de la grabación: revisá el inicio"):
        assert esperado in resumen, esperado
    assert ">procesando</span>" in html
    # Un importacion.json roto no rompe la página.
    (app.state.cfg.sesion_dir("importada-1") / "importacion.json").write_text("{roto", encoding="utf-8")
    assert panel.get("/panel/clases/importada-1").status_code == 200


def test_reproductor_sirve_la_copia(client: TestClient, panel: TestClient):
    app = client.app
    carpeta = crear_sesion(app, "amr-1", importada=True, audio_nombre="audio.amr")
    original = b"#!AMR\n" + bytes(range(256)) * 20
    copia = b"OggS" + bytes(reversed(range(256))) * 30
    (carpeta / "audio.amr").write_bytes(original)
    # Sin la copia todavía: el original (que Chrome no reproduce) y el aviso.
    r = panel.get("/panel/clases/amr-1/audio")
    assert r.status_code == 200 and r.content == original and r.headers["content-type"] == "application/octet-stream"
    assert "El navegador no reproduce este formato" in panel.get("/panel/clases/amr-1").text
    # Con audio.panel.ogg (la arma verificar): esa, como audio/ogg y con Range para saltar.
    (carpeta / sesiones.AUDIO_PANEL).write_bytes(copia)
    r = panel.get("/panel/clases/amr-1/audio")
    assert r.status_code == 200 and r.content == copia and r.headers["content-type"].startswith("audio/ogg")
    r = panel.get("/panel/clases/amr-1/audio", headers={"Range": "bytes=4-99"})
    assert r.status_code == 206 and r.content == copia[4:100] and r.headers["accept-ranges"] == "bytes"
    assert "El navegador no reproduce este formato" not in panel.get("/panel/clases/amr-1").text
    # Un m4a se sirve tal cual, como audio/mp4 y con Range.
    carpeta = crear_sesion(app, "m4a-1", importada=True, audio_nombre="audio.m4a")
    m4a = b"\x00\x00\x00\x18ftypM4A " + bytes(range(256)) * 20
    (carpeta / "audio.m4a").write_bytes(m4a)
    r = panel.get("/panel/clases/m4a-1/audio", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == m4a[100:200] and r.headers["content-type"] == "audio/mp4"


# --- el área temporal, de a pedazos ------------------------------------------------------------------

def test_archivo_temporal_de_a_pedazos(cfg, monkeypatch: pytest.MonkeyPatch):
    token, carpeta = importador.crear_temporal(cfg)
    a = importador.ArchivoTemporal(cfg, token, "audio", "Audio: de WhatsApp?.opus")
    a.escribir(b"x" * 10)
    a.escribir(b"y" * 10)
    assert a.cerrar() == carpeta / "audio" / "000" / "Audio de WhatsApp.opus" and a.ruta.read_bytes() == b"x" * 10 + b"y" * 10
    monkeypatch.setitem(importador.TOPES_TEMPORAL, "fotos", 15)
    f = importador.ArchivoTemporal(cfg, token, "fotos", "IMG_1.jpg")
    f.escribir(b"z" * 10)
    with pytest.raises(sesiones.ArchivoGrande, match="IMG_1.jpg pesa más de 0 MB"):
        f.escribir(b"z" * 10)
    assert not f.ruta.parent.exists()   # lo que pasó el tope no queda
    f.descartar()                       # y descartar de nuevo no falla
    with pytest.raises(ValueError):
        importador.ArchivoTemporal(cfg, token, "otra", "x")
    assert importador.token_valido(token) and not importador.token_valido("../" + token[3:])


# --- lo que encontró la revisión ---------------------------------------------------------------------

LIMITE = "limite-de-prueba"


def multipart(partes, *, cierre: bool = True, crlf_final: bool = True) -> tuple[bytes, dict[str, str]]:
    """Un multipart armado a mano: (campo, valor) o (campo, nombre de archivo, contenido)."""
    cuerpo = b""
    for p in partes:
        disposicion = f'Content-Disposition: form-data; name="{p[0]}"'
        if len(p) == 2:
            cuerpo += f"--{LIMITE}\r\n{disposicion}\r\n\r\n{p[1]}\r\n".encode()
        else:
            cuerpo += (f'--{LIMITE}\r\n{disposicion}; filename="{p[1]}"\r\n'
                       "Content-Type: application/octet-stream\r\n\r\n").encode() + p[2] + b"\r\n"
    if cierre:
        cuerpo += f"--{LIMITE}--".encode() + (b"\r\n" if crlf_final else b"")
    return cuerpo, {"Content-Type": f"multipart/form-data; boundary={LIMITE}"}


def mandar_y_cortar(app, ruta: str, cuerpo: bytes, encabezados: dict[str, str], corte: int) -> None:
    """Manda los primeros `corte` bytes del pedido de a pedazos y se corta la conexión (un xhr que se cae
    a mitad, el server que se reinicia). Directo al ASGI: el TestClient manda el cuerpo entero de una."""
    import anyio
    from starlette.requests import ClientDisconnect

    pedazos = [cuerpo[i:min(i + 1024, corte)] for i in range(0, corte, 1024)]

    async def receive():
        if pedazos:
            return {"type": "http.request", "body": pedazos.pop(0), "more_body": True}
        return {"type": "http.disconnect"}

    async def send(_mensaje):
        pass

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": ruta, "raw_path": ruta.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"127.0.0.1:8000"), (b"content-length", str(len(cuerpo)).encode()),
                         *((k.lower().encode(), v.encode()) for k, v in encabezados.items())],
             "client": ("127.0.0.1", 50124), "server": ("127.0.0.1", 8000)}
    try:
        anyio.run(app, scope, receive, send)
    except ClientDisconnect:
        pass


@pytest.mark.ffmpeg
def test_analizar_con_una_ilegible_primero(client: TestClient, panel: TestClient, audios):
    # Antes, el índice de cada foto no contaba la ilegible: cada una salía con el nombre de la anterior.
    r = analizar(panel, audios["m4a"], fotos=[("roto.jpg", b"no es una imagen"), ("IMG_1.jpg", FOTO_1),
                                              ("IMG_2.jpg", FOTO_2)])
    assert r.status_code == 200, r.text
    d = r.json()
    assert [(f["nombre"], f["minuto"]) for f in d["fotos"]] == [("IMG_1.jpg", "00:01:00"), ("IMG_2.jpg", "00:02:30")]
    assert [f["nombre"] for f in d["fotos_ilegibles"]] == ["roto.jpg"]
    guardado = leer_json(client.app.state.cfg.datos / "importaciones" / d["token"] / "analisis.json")
    assert [f["nombre"] for f in guardado["resultado"]["fotos"]] == ["IMG_1.jpg", "IMG_2.jpg"]


@pytest.mark.ffmpeg
def test_analizar_con_el_maximo_de_fotos(client: TestClient, panel: TestClient, audios):
    # El JS manda hasta max_fotos (300) fotos además del audio: el audio no cuenta para ese máximo.
    maximo = datos_js(panel.get("/panel/importar").text)["max_fotos"]
    assert maximo == panel_importar.MAX_FOTOS_FORM == 300
    chica = jpeg_bytes("2026:10:20 14:01:00", "-03:00")
    fotos = [(f"IMG_{i:03d}.jpg", chica) for i in range(maximo)]
    r = analizar(panel, audios["m4a"], fotos=fotos)
    assert r.status_code == 200, r.text   # antes: 422 "demasiados archivos: hasta 300 por vez"
    assert r.json()["resumen_fotos"]["total"] == maximo
    r = analizar(panel, audios["m4a"], fotos=fotos + [("IMG_300.jpg", chica)])
    assert r.status_code == 422 and "demasiados archivos" in r.json()["detail"]


def test_formulario_sin_cierre(client: TestClient, panel: TestClient):
    cfg = client.app.state.cfg
    # Completo según el Content-Length, pero sin el --límite-- final: el último archivo quedaría a medias.
    cuerpo, encabezados = multipart([("audio", "clase.m4a", b"x" * 1000)], cierre=False)
    r = panel.post("/panel/importar/analizar", content=cuerpo, headers=encabezados)
    assert r.status_code == 422
    assert r.json() == {"detail": "No se pudieron recibir los archivos: el formulario llegó cortado.", "campo": None}
    assert temporales(cfg) == []   # ni el token nuevo ni el audio a medias
    # Con el cierre (con o sin el CRLF del final) se lee entero.
    for crlf_final in (True, False):
        cuerpo, encabezados = multipart([("fotos", "", b"")], crlf_final=crlf_final)
        r = panel.post("/panel/importar/analizar", content=cuerpo, headers=encabezados)
        assert r.json() == {"detail": "Elegí el audio de la clase.", "campo": "audio"}, crlf_final


@pytest.mark.ffmpeg
def test_importar_material_sin_cierre(client: TestClient, panel: TestClient, audios):
    app, cfg = client.app, client.app.state.cfg
    token = analizar(panel, audios["m4a"]).json()["token"]
    cuerpo, encabezados = multipart([("token", token), ("materia_id", "prueba"), ("tipo_clase", "teorica"),
                                     ("inicio", "2026-10-20T14:00:00"), ("material", "slides.pdf", pdf_de(1))],
                                    cierre=False)
    r = panel.post("/panel/importar", content=cuerpo, headers=encabezados)
    assert r.status_code == 422, r.text   # antes: 201 sin el PDF y sin decir nada
    assert r.json()["detail"] == "No se pudo recibir el formulario: el formulario llegó cortado."
    assert contar_sesiones(app.state.base) == 0
    carpeta = cfg.datos / "importaciones" / token
    assert (carpeta / "analisis.json").is_file()
    assert not any(p.is_file() for p in carpeta.glob("material/**/*"))


@pytest.mark.ffmpeg
def test_reanalisis_cortado_no_importa_con_menos_fotos(client: TestClient, panel: TestClient, audios):
    app, cfg = client.app, client.app.state.cfg
    token = analizar(panel, audios["m4a"], fotos=[("IMG_1.jpg", FOTO_1), ("IMG_2.jpg", FOTO_2)]).json()["token"]
    carpeta = cfg.datos / "importaciones" / token
    # Se agrega una foto (el JS vuelve a mandar las tres) y el pedido se corta a mitad de IMG_2: IMG_1 ya
    # reemplazó a las de antes, pero analisis.json todavía lista las dos.
    cuerpo, encabezados = multipart([("token", token), ("fotos", "IMG_1.jpg", FOTO_1),
                                     ("fotos", "IMG_2.jpg", FOTO_2), ("fotos", "IMG_3.jpg", FOTO_SIN_EXIF)])
    corte = cuerpo.index(FOTO_2) + len(FOTO_2) // 2
    mandar_y_cortar(app, "/panel/importar/analizar", cuerpo, encabezados, corte)
    assert temporales(cfg) == [token]
    assert sorted(p.relative_to(carpeta).as_posix() for p in carpeta.glob("fotos/**/*.jpg")) == ["fotos/000/IMG_1.jpg"]

    r = importar(panel, token)
    assert r.status_code == 422, r.text   # antes: 201 sólo con IMG_1, y el mensaje "ok"
    assert r.json() == {"detail": "El último análisis de las fotos no terminó (¿se cortó la subida?): "
                                  "hay que volver a analizarlas.", "campo": "fotos"}
    assert contar_sesiones(app.state.base) == 0
    # Lo que hace el JS con ese error: vuelve a analizar las fotos, y entonces sí.
    d = analizar(panel, token=token, fotos=[("IMG_1.jpg", FOTO_1), ("IMG_2.jpg", FOTO_2),
                                            ("IMG_3.jpg", FOTO_SIN_EXIF)]).json()
    assert len(d["fotos"]) == 3 and panel_importar.REANALIZANDO not in leer_json(carpeta / "analisis.json")
    r = importar(panel, token)
    assert r.status_code == 201, r.text
    with app.state.base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE sesion_id = ? AND tipo = 'foto'",
                            (r.json()["id"],)).fetchone()[0] == 3


@pytest.mark.ffmpeg
def test_importar_sin_una_foto_del_analisis(client: TestClient, panel: TestClient, audios):
    cfg = client.app.state.cfg
    token = analizar(panel, audios["m4a"], fotos=[("IMG_1.jpg", FOTO_1), ("IMG_2.jpg", FOTO_2)]).json()["token"]
    (cfg.datos / "importaciones" / token / "fotos" / "001" / "IMG_2.jpg").unlink()
    r = importar(panel, token)
    assert r.status_code == 422
    assert r.json() == {"detail": "1 foto(s) del análisis ya no están: hay que volver a analizarlas.",
                        "campo": "fotos"}
    assert contar_sesiones(client.app.state.base) == 0


@pytest.mark.ffmpeg
def test_importar_con_el_disco_lleno(client: TestClient, panel: TestClient, audios, monkeypatch: pytest.MonkeyPatch):
    app, cfg = client.app, client.app.state.cfg
    token = analizar(panel, audios["m4a"]).json()["token"]
    carpeta = cfg.datos / "importaciones" / token

    def lleno(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(importador, "sha256_archivo", lleno)
    r = importar(panel, token, material=[("Slides.pdf", pdf_de(1), "application/pdf")])
    # Un error que se lee (antes: 500 sin JSON), sin una clase en error que no se puede borrar…
    assert r.status_code == 503
    assert r.json() == {"detail": "No se pudo importar: no hay lugar en el disco de la PC: liberá espacio y "
                                  "volvé a intentar.", "campo": None}
    assert contar_sesiones(app.state.base) == 0 and not any(cfg.sesiones_dir.iterdir())
    # …y el análisis sigue (el audio, que se había movido a la sesión, volvió): se puede reintentar.
    assert (carpeta / "audio" / "000" / NOMBRE_M4A).read_bytes() == audios["m4a"]
    monkeypatch.undo()
    r = importar(panel, token)
    assert r.status_code == 201, r.text
    assert (cfg.sesion_dir(r.json()["id"]) / "audio.m4a").read_bytes() == audios["m4a"]
    assert temporales(cfg) == []


def test_cancelar_no_se_ofrece_cuando_ya_no_cancela(panel: TestClient):
    # Cuando el server ya tiene el pedido de importar, cortar el xhr no frena nada: la clase se crea
    # igual. Cancelar se apaga en ese momento (y enseguida si no hay material que subir).
    html = panel.get("/panel/importar").text
    js = html[html.index("form.addEventListener('submit'"):html.index("// --- Cancelar")]
    assert "if (!archivos.length) cancelar.disabled = true;" in js
    assert "cancelar.disabled = true;\n        progreso(true, null, 'Importando… (ya no se puede cancelar)');" in js
    assert "cancelar.disabled = false;" in js   # si el server responde con un error, vuelve
    # Un análisis de las fotos que se cortó: el JS las vuelve a analizar.
    assert "if (r.campo === 'fotos' && token && audio.files[0])" in js and "analizar(false);" in js
