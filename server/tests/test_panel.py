"""Panel web (§13): sólo desde la PC, cada pantalla, ediciones sin reiniciar y acciones."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from classmer import adjuntos, cola, panel as panel_mod, sesiones
from classmer.ia import modelos
from classmer.ia.facturacion import huella
from classmer.pipeline import actualizar_sesion
from classmer.pipeline.publicar import AVISO_EN_USO
from classmer.util import ahora_iso

SID = "0192f7c2-1111-7000-8000-00000000abcd"
LOCAL = "http://127.0.0.1:8000"
CLAVE_ANTHROPIC = "sk-ant-falsa-123"
CLAVE_GEMINI = "gemini-falsa-456"


@pytest.fixture
def panel(client: TestClient) -> TestClient:
    """El mismo server, visto desde la PC (sin token)."""
    return TestClient(client.app, base_url=LOCAL, client=("127.0.0.1", 50123), follow_redirects=False)


def armar_clase(app, sid: str = SID, *, materia_id: str | None = "prueba", estado: str = "lista") -> Path:
    cfg, base = app.state.cfg, app.state.base
    with base.conexion() as conn:
        sesiones.crear(cfg, conn, sesion_id=sid, materia_id=materia_id, origen_materia="calendario",
                       tipo_clase="teorica", inicio="2026-10-15T11:30:00-03:00", dispositivo="s23")
        actualizar_sesion(conn, sid, cfg.zona_horaria, estado=estado, etapa="publicar", duracion_ms=185_000,
                          titulo="Gradiente y curvas de nivel")
        conn.execute("INSERT INTO assets (id, sesion_id, tipo, offset_ms, ruta, datos, creado) "
                     "VALUES (?, ?, 'foto', 61000, ?, '{}', ?)", (f"f-{sid}", sid, f"fotos/f-{sid}.jpg", ahora_iso()))
        conn.execute("INSERT INTO assets (id, sesion_id, tipo, offset_ms, datos, creado) VALUES (?, ?, 'marca', 90000, ?, ?)",
                     (f"m-{sid}", sid, json.dumps({"marca": "importante"}), ahora_iso()))
        aid = conn.execute(
            "INSERT INTO apuntes (sesion_id, version, actual, motor, modelo, ruta_tex, ruta_pdf, compilo, titulo, creado) "
            "VALUES (?, 1, 1, 'claude-agent', 'opus', 'apunte/v1/apunte.tex', 'apunte/v1/apunte.pdf', 1, ?, ?)",
            (sid, "Gradiente y curvas de nivel", ahora_iso())).lastrowid
        for tipo, texto, offset in (("parcial", "Entra el gradiente", 90000), ("tarea", "Práctica 3", 120000)):
            conn.execute("INSERT INTO items (materia_id, sesion_id, apunte_id, tipo, texto, offset_ms, estado, creado) "
                         "VALUES (?, ?, ?, ?, ?, ?, 'abierto', ?)", (materia_id, sid, aid, tipo, texto, offset, ahora_iso()))
    carpeta = cfg.sesion_dir(sid)
    (carpeta / "fotos").mkdir(parents=True, exist_ok=True)
    (carpeta / "fotos" / f"f-{sid}.jpg").write_bytes(b"\xff\xd8\xff jpeg falso")
    (carpeta / "audio.ogg").write_bytes(b"OggS" + bytes(range(256)) * 40)
    (carpeta / "apunte" / "v1").mkdir(parents=True)
    (carpeta / "apunte" / "v1" / "apunte.pdf").write_bytes(b"%PDF-1.5 falso")
    (carpeta / "transcript.json").write_text(json.dumps([
        {"bloque": 1, "inicio_ms": 1400, "fin_ms": 8300, "hablante": "PROFE", "texto": "Hoy vemos el gradiente"},
        {"bloque": 1, "inicio_ms": 61000, "fin_ms": 64000, "hablante": "PROFE", "texto": "Esto lo tomo"},
    ]), encoding="utf-8")
    (carpeta / "timeline.md").write_text(
        "# Clase: Materia de Prueba — 2026-10-15 — teórica\n[00:00:01] PROFE: Hoy vemos el gradiente\n"
        "[00:01:01] FOTO fotos/pizarra_01.jpg\n[00:01:30] MARCA importante\n", encoding="utf-8")
    (carpeta / "pipeline.log").write_text("2026-10-15T13:00:00-03:00 publicar: copiado\n", encoding="utf-8")
    return carpeta


# --- sólo desde la PC ----------------------------------------------------------------------------

def test_desde_la_lan_403(client: TestClient):
    lan = TestClient(client.app, base_url="http://192.168.1.10:8000", client=("192.168.1.20", 40000))
    for ruta in ("/panel", "/panel/clases", "/panel/materias", "/panel/estado", "/"):
        r = lan.get(ruta)
        assert r.status_code == 403, ruta
    assert lan.post("/panel/estilo", data={"estilo_global": "x"}).status_code == 403
    # El token de la app no abre el panel.
    assert lan.get("/panel/clases", headers={"Authorization": client.headers["Authorization"]}).status_code == 403


def test_host_y_origen(client: TestClient, panel: TestClient):
    rebinding = TestClient(client.app, base_url="http://malo.example:8000", client=("127.0.0.1", 1))
    assert rebinding.get("/panel/clases").status_code == 403
    r = panel.post("/panel/estilo", data={"estilo_global": "x"}, headers={"Origin": "http://malo.example"})
    assert r.status_code == 403
    r = panel.post("/panel/estilo", data={"estilo_global": "Nuevo"}, headers={"Origin": LOCAL})
    assert r.status_code == 303


def test_ipv6_local(client: TestClient):
    c = TestClient(client.app, base_url="http://[::1]:8000", client=("::1", 5))
    assert c.get("/panel/estado").status_code == 200


def test_la_api_sigue_pidiendo_token(panel: TestClient):
    assert panel.get("/api/materias").status_code == 401


# --- pantallas -----------------------------------------------------------------------------------

def test_inicio_redirige(panel: TestClient):
    r = panel.get("/panel")
    assert r.status_code == 303 and r.headers["location"] == "/panel/clases"


def test_lista_de_clases(client: TestClient, panel: TestClient):
    armar_clase(client.app)
    with client.app.state.base.conexion() as conn:
        sesiones.crear(client.app.state.cfg, conn, sesion_id="esperando-1", materia_id=None, origen_materia="auto",
                       tipo_clase=None, inicio="2026-10-16T09:00:00-03:00", dispositivo="s23")
        actualizar_sesion(conn, "esperando-1", "America/Argentina/Buenos_Aires", estado="esperando_usuario",
                          error="No pude determinar la materia (confianza 0.40). Sugerida: prueba.")
    r = panel.get("/panel/clases")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "Gradiente y curvas de nivel" in html and "2026-10-15 11:30" in html
    assert "Materia de Prueba" in html and "Auto (sin materia)" in html
    assert "No pude determinar la materia" in html and "esperando_usuario" in html
    assert panel.get("/panel/clases?materia=auto").text.count("/panel/clases/esperando-1\"") >= 1
    assert "Gradiente" not in panel.get("/panel/clases?estado=error").text


def test_detalle_de_clase(client: TestClient, panel: TestClient):
    armar_clase(client.app)
    r = panel.get(f"/panel/clases/{SID}")
    assert r.status_code == 200
    html = r.text
    for esperado in ("Gradiente y curvas de nivel", "Clase 1", f"/panel/clases/{SID}/apunte.pdf",
                     f"/panel/clases/{SID}/audio", f"/panel/clases/{SID}/archivo/fotos/f-{SID}.jpg",
                     "Hoy vemos el gradiente", "Entra el gradiente", "⭐ Importante",
                     "publicar: copiado", "Reprocesar", ">Borrar<"):
        assert esperado in html, esperado
    assert panel.get("/panel/clases/no-existe").status_code == 404


def test_clase_sin_botones_de_minuto(client: TestClient, panel: TestClient):
    # Para moverse en el audio está la barra del reproductor: ya no hay una fila 0′ 1′ … N′.
    armar_clase(client.app)
    html = panel.get(f"/panel/clases/{SID}").text
    assert 'class="minutos"' not in html and "′" not in html
    assert re.search(r"<button[^>]*data-ms", html) is None
    # Los demás saltos al audio siguen, y son los únicos data-ms: ítems (90000 y 120000), foto (61000),
    # marca (90000), transcript (1400 y 61000) y línea de tiempo (1000, 61000 y 90000). Antes la
    # fila agregaba 0, 60000, 120000 y 180000.
    assert sorted(int(ms) for ms in re.findall(r'data-ms="(\d+)"', html)) == sorted(
        [90000, 120000, 61000, 90000, 1400, 61000, 1000, 61000, 90000])
    assert 'href="#" class="t" data-ms="120000">00:02:00</a>' in html  # el ítem, no la fila
    assert "function saltar(reproductor, segundos, otro)" in html and ".get('t')" in html  # el ?t= de Pendientes sigue


def test_archivos_de_la_clase(client: TestClient, panel: TestClient):
    armar_clase(client.app)
    r = panel.get(f"/panel/clases/{SID}/apunte.pdf")
    assert r.status_code == 200 and r.content == b"%PDF-1.5 falso"
    assert r.headers["content-type"] == "application/pdf"
    r = panel.get(f"/panel/clases/{SID}/audio", headers={"Range": "bytes=4-9"})
    assert r.status_code == 206 and r.content == bytes(range(6))  # el reproductor puede saltar
    assert r.headers["content-type"].startswith("audio/ogg")
    assert panel.get(f"/panel/clases/{SID}/archivo/fotos/f-{SID}.jpg").status_code == 200
    for malo in ("audio.ogg", "../../config.yaml", "fotos/../sesion.json", "fotos/no-existe.jpg"):
        assert panel.get(f"/panel/clases/{SID}/archivo/{malo}").status_code == 404, malo


def test_materias_y_estilo_y_estado(client: TestClient, panel: TestClient):
    armar_clase(client.app)
    html = panel.get("/panel/materias").text
    assert "Materia de Prueba" in html and "Otra Materia" in html and "2 abiertos" in html
    html = panel.get("/panel/materias/prueba").text
    assert 'name="proximo_numero"' in html and 'value="2"' in html and 'value="Prueba"' in html
    assert panel.get("/panel/materias/nueva").status_code == 200
    assert panel.get("/panel/materias/no-existe").status_code == 404
    html = panel.get("/panel/estilo").text
    assert "Estilo global" in html and "Contrato fijo" in html
    with client.app.state.base.conexion() as conn:
        cola.encolar(conn, "segmentar", SID)
    (client.app.state.cfg.logs_dir / "classmer.log").write_text("línea 1\nlínea final del log\n", encoding="utf-8")
    html = panel.get("/panel/estado").text
    assert "segmentar" in html and "línea final del log" in html and "apagado" in html


def test_pendientes(client: TestClient, panel: TestClient):
    armar_clase(client.app)
    html = panel.get("/panel/pendientes").text
    assert "Materia de Prueba" in html and "1 parcial" in html and "1 tarea" in html
    html = panel.get("/panel/pendientes?materia=prueba").text
    assert "Entra el gradiente" in html and "Práctica 3" in html and "Clase 1" in html
    assert f"/panel/clases/{SID}?t=90000" in html and "00:01:30" in html
    assert "Gradiente y curvas de nivel" in html  # clases recientes
    assert panel.get("/panel/pendientes?materia=no-existe").status_code == 404


def test_marcar_item(client: TestClient, panel: TestClient):
    armar_clase(client.app)
    with client.app.state.base.conexion() as conn:
        item = conn.execute("SELECT id FROM items WHERE tipo = 'tarea'").fetchone()[0]
    r = panel.post(f"/panel/items/{item}", data={"estado": "hecho", "volver": "/panel/pendientes?materia=prueba"})
    assert r.status_code == 303 and r.headers["location"].startswith("/panel/pendientes?materia=prueba&msg=")
    html = panel.get("/panel/pendientes?materia=prueba").text
    assert "Práctica 3" not in html and "Entra el gradiente" in html
    assert "Práctica 3" in panel.get("/panel/pendientes?materia=prueba&ver=todos").text
    assert panel.post(f"/panel/items/{item}", data={"estado": "abierto"}).status_code == 303
    assert "Práctica 3" in panel.get("/panel/pendientes?materia=prueba").text
    assert panel.post("/panel/items/9999", data={"estado": "hecho"}).status_code == 404
    r = panel.post(f"/panel/items/{item}", data={"estado": "hecho", "volver": "https://malo.example/"})
    assert r.headers["location"].startswith("/panel/pendientes?")


# --- ediciones sin reiniciar ---------------------------------------------------------------------

def test_editar_materia_desde_el_panel_sin_reiniciar(home: Path, client: TestClient, panel: TestClient):
    armar_clase(client.app)
    r = panel.post("/panel/materias/prueba", data={
        "nombre": "Prueba (Classmer)", "activa": "on", "carpeta": str(client.app.state.cfg.materia("prueba").carpeta),
        "carpeta_clases": "Clases-Classmer", "perfil": "", "titulo_regex": "^Prueba\\b", "calendario": "",
        "tipos_clase": "teorica, practica", "numero_clase_inicial": "1", "proximo_numero": "7",
        "proximo_original": "2", "estilo": "Siempre con un ejemplo.",
    })
    assert r.status_code == 303, r.text
    api = client.get("/api/materias").json()[0]
    assert api["nombre"] == "Prueba (Classmer)" and api["proximo_numero"] == 7
    assert api["calendario"]["titulo_regex"] == "^Prueba\\b"
    guardada = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["materias"][0]
    assert guardada["numero_clase_inicial"] == 6 and guardada["estilo"] == "Siempre con un ejemplo.\n"
    assert "perfil" not in guardada  # un campo vacío que no estaba no agrega la clave

    # Desactivarla la saca de la app.
    datos = {"nombre": "Otra Materia", "carpeta": "", "carpeta_clases": "", "perfil": "", "titulo_regex": "",
             "calendario": "", "tipos_clase": "teorica,practica", "numero_clase_inicial": "1",
             "proximo_numero": "1", "proximo_original": "1", "estilo": ""}
    assert panel.post("/panel/materias/otra", data=datos).status_code == 303
    assert [m["id"] for m in client.get("/api/materias").json()] == ["prueba"]


def test_editar_materia_invalida_muestra_el_error(home: Path, panel: TestClient):
    antes = (home / "config.yaml").read_bytes()
    r = panel.post("/panel/materias/prueba", data={
        "nombre": "Materia de Prueba", "activa": "on", "titulo_regex": "([mal", "tipos_clase": "teorica",
        "numero_clase_inicial": "1", "proximo_numero": "1", "proximo_original": "1"})
    assert r.status_code == 422 and "regex" in r.text and 'value="([mal"' in r.text
    assert (home / "config.yaml").read_bytes() == antes


def test_agregar_materia_desde_el_panel(client: TestClient, panel: TestClient):
    r = panel.post("/panel/materias/nueva", data={
        "id": "historia", "nombre": "Historia Contemporánea", "activa": "on", "carpeta": "",
        "carpeta_clases": "", "perfil": "", "titulo_regex": "^Historia\\b", "calendario": "",
        "tipos_clase": "teorica, practica", "numero_clase_inicial": "3", "estilo": ""})
    assert r.status_code == 303 and r.headers["location"].startswith("/panel/materias/historia")
    api = {m["id"]: m for m in client.get("/api/materias").json()}
    assert api["historia"]["proximo_numero"] == 3
    r = panel.post("/panel/materias/nueva", data={"id": "historia", "nombre": "Repetida", "tipos_clase": "teorica"})
    assert r.status_code == 422 and "ya existe" in r.text


def test_editar_estilo_global(client: TestClient, panel: TestClient):
    r = panel.post("/panel/estilo", data={"estilo_global": "Incluí gráficos.\r\nY ejemplos."})
    assert r.status_code == 303
    assert client.app.state.cfg.estilo_global == "Incluí gráficos.\nY ejemplos.\n"
    assert "Incluí gráficos." in panel.get("/panel/estilo").text


# --- acciones sobre una clase --------------------------------------------------------------------

def test_asignar_materia_desde_el_panel(client: TestClient, panel: TestClient):
    with client.app.state.base.conexion() as conn:
        sesiones.crear(client.app.state.cfg, conn, sesion_id="esp-1", materia_id=None, origen_materia="auto",
                       tipo_clase=None, inicio="2026-10-16T09:00:00-03:00", dispositivo="s23")
        actualizar_sesion(conn, "esp-1", "America/Argentina/Buenos_Aires", estado="esperando_usuario",
                          error="No pude determinar la materia")
    assert 'action="/panel/clases/esp-1/materia"' in panel.get("/panel/clases/esp-1").text
    r = panel.post("/panel/clases/esp-1/materia", data={"materia_id": "otra"})
    assert r.status_code == 303 and "sigue+la+revisi" in r.headers["location"]
    s = client.get("/api/sesiones/esp-1").json()
    assert s["materia_id"] == "otra" and s["estado"] == "en_cola" and s["numero_clase"] == 1
    r = panel.post("/panel/clases/esp-1/materia", data={"materia_id": "no-existe"})
    assert "tipo=error" in r.headers["location"]


def test_reprocesar_desde_el_panel(client: TestClient, panel: TestClient):
    armar_clase(client.app)
    r = panel.post(f"/panel/clases/{SID}/reprocesar", data={"desde": "apunte"})
    assert r.status_code == 303 and "tipo=ok" in r.headers["location"]
    assert client.get(f"/api/sesiones/{SID}").json()["estado"] == "en_cola"
    r = panel.post(f"/panel/clases/{SID}/reprocesar", data={"desde": "cualquiera"})
    assert "tipo=error" in r.headers["location"]


def test_borrar_sesion_de_prueba(client: TestClient, panel: TestClient):
    cfg = client.app.state.cfg
    carpeta = armar_clase(client.app)
    publicada = cfg.materia("prueba").ruta_clases() / "2026-10-15 Clase 01 - Gradiente"
    publicada.mkdir(parents=True)
    (publicada / "apunte.pdf").write_bytes(b"pdf")
    with client.app.state.base.conexion() as conn:
        actualizar_sesion(conn, SID, cfg.zona_horaria, ruta_publicada=str(publicada))
        cola.encolar(conn, "notificar", SID)
        conn.execute("INSERT INTO busqueda (texto, materia, tipo, ref, inicio_ms) VALUES (?, ?, ?, ?, ?)",
                     ("gradiente", "prueba", "transcript", SID, 0))
    (cfg.jobs_dir / SID / "apunte").mkdir(parents=True)

    sin_confirmar = panel.post(f"/panel/clases/{SID}/borrar", data={})
    assert "tipo=error" in sin_confirmar.headers["location"] and carpeta.exists()
    r = panel.post(f"/panel/clases/{SID}/borrar", data={"confirmar": "si"})
    assert r.status_code == 303 and r.headers["location"].startswith("/panel/clases?")
    assert not carpeta.exists() and not (cfg.jobs_dir / SID).exists() and not publicada.exists()
    assert client.get(f"/api/sesiones/{SID}").status_code == 404
    with client.app.state.base.conexion() as conn:
        for tabla in ("assets", "apuntes", "items", "jobs"):
            assert conn.execute(f"SELECT COUNT(*) FROM {tabla} WHERE sesion_id = ?", (SID,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM busqueda WHERE ref = ?", (SID,)).fetchone()[0] == 0


def test_no_se_borran_clases_reales(client: TestClient, panel: TestClient):
    carpeta = armar_clase(client.app, materia_id="otra")
    assert ">Borrar<" not in panel.get(f"/panel/clases/{SID}").text
    r = panel.post(f"/panel/clases/{SID}/borrar", data={"confirmar": "si"})
    assert "tipo=error" in r.headers["location"]
    assert carpeta.exists() and client.get(f"/api/sesiones/{SID}").status_code == 200


# --- modelos y facturación -----------------------------------------------------------------------

def poner_claves(app, home: Path, *, anthropic_: bool = True, gemini: bool = True) -> None:
    """Claves falsas en el .env temporal (nunca las reales) y la config viva recargada."""
    lineas = [l for l in (home / ".env").read_text(encoding="utf-8").splitlines()
              if not l.startswith(("ANTHROPIC_API_KEY=", "GEMINI_API_KEY="))]
    if anthropic_:
        lineas.append(f"ANTHROPIC_API_KEY={CLAVE_ANTHROPIC}")
    if gemini:
        lineas.append(f"GEMINI_API_KEY={CLAVE_GEMINI}")
    (home / ".env").write_text("\n".join(lineas) + "\n", encoding="utf-8")
    app.state.config_viva.recargar(forzar=True)


def select_de(html: str, nombre: str) -> str:
    inicio = html.index(f'<select name="{nombre}"')
    return html[inicio:html.index("</select>", inicio)]


def mensaje(r) -> tuple[str, str]:
    """(msg, tipo) del redirect de un POST del panel."""
    consulta = parse_qs(urlsplit(r.headers["location"]).query)
    return consulta["msg"][0], consulta["tipo"][0]


def config_guardada(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def posponer(app, tipo: str, sid: str = SID, *, en_s: float = 3600) -> int:
    with app.state.base.conexion() as conn:
        return cola.encolar(conn, tipo, sid, run_after=time.time() + en_s)


def run_after(app, job_id: int) -> float:
    with app.state.base.conexion() as conn:
        return conn.execute("SELECT run_after FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def test_pagina_modelos(panel: TestClient):
    html = panel.get("/panel/modelos").text
    assert 'href="/panel/modelos" class="activo"' in html
    # Facturación: los cuatro modos (por defecto api_luego_max) y el aviso de que no hay clave.
    for texto in ("Sólo API key", "Sólo plan Max", "API y, si se queda sin créditos, plan Max (por defecto)",
                  "Plan Max y, si se agota su cupo, API"):
        assert texto in html, texto
    assert '<option value="api_luego_max" selected>' in html and 'value="6"' in html
    assert ("No hay ANTHROPIC_API_KEY en Classmer-data\\.env: los modos con API funcionan como plan Max"
            in html)
    assert "sin clave" in html and "Reintentar" not in html
    # Selects con los modelos del catálogo, cada uno en su tarea.
    transcribir, clasificar, apunte = (select_de(html, t) for t in ("transcribir", "clasificar", "apunte"))
    assert ('<option value="gemini-3.5-transcribe" selected>Gemini 3.5 Transcribe (gemini-3.5-transcribe)'
            in transcribir)
    assert "claude" not in transcribir
    assert '<optgroup label="Claude (Anthropic)">' in clasificar and '<optgroup label="Gemini (Google)">' in clasificar
    assert '<option value="claude-sonnet-5-5" selected>Claude Sonnet 5.5 (claude-sonnet-5-5)' in clasificar
    assert 'value="gemini-flash-latest"' in clasificar and "transcribe" not in clasificar
    assert '<option value="claude-opus-5-5" selected>' in apunte and "gemini" not in apunte
    for modelo in ("claude-haiku-5-5", "claude-fable-5-1"):
        assert f'value="{modelo}"' in apunte and f'value="{modelo}"' in clasificar
    assert '<option value="medium" selected>' in select_de(html, "clasificar_esfuerzo")
    assert '<option value="high" selected>' in select_de(html, "apunte_esfuerzo")
    # Nunca se escribe a mano: no hay campos de texto para el modelo.
    assert 'type="text"' not in html
    # Catálogo: sin descubrimientos todavía, falta de claves y dónde está elegido cada uno.
    assert "Redescubrir modelos" in html and "falta ANTHROPIC_API_KEY" in html and "falta GEMINI_API_KEY" in html
    assert "apunte.modelo (global) (esfuerzo high)" in html and "clasificar.modelo, motor claude" in html
    assert "Ninguna: todas usan el modelo global" in html


def test_estado_de_la_facturacion_y_botones(client: TestClient, panel: TestClient, home: Path):
    poner_claves(client.app, home)
    ahora = time.time()
    with client.app.state.base.conexion() as conn:
        conn.execute("INSERT INTO facturacion (medio, estado, motivo, desde, hasta, huella_clave) "
                     "VALUES ('api', 'sin_creditos', 'credit balance is too low', ?, NULL, ?)",
                     (ahora - 600, huella(CLAVE_ANTHROPIC)))
        conn.execute("INSERT INTO facturacion (medio, estado, motivo, desde, hasta) "
                     "VALUES ('max', 'sin_cupo', 'usage limit reached', ?, ?)", (ahora, ahora + 3600))
    html = panel.get("/panel/modelos").text
    assert "API sin créditos desde" in html and "plan Max sin cupo desde" in html
    assert "se vuelve a probar a las" in html and "credit balance is too low" in html
    assert ">Reintentar API</button>" in html and ">Reintentar plan Max</button>" in html
    assert "No hay ANTHROPIC_API_KEY" not in html
    # El aviso de la clave sólo si el modo usa la API.
    poner_claves(client.app, home, anthropic_=False)
    client.app.state.config_viva.editar_modelos({"claude": {"facturacion": "max"}})
    html = panel.get("/panel/modelos").text
    assert "No hay ANTHROPIC_API_KEY" not in html and "sin clave" in html
    assert ">Reintentar API</button>" not in html and ">Reintentar plan Max</button>" in html


def test_guardar_modelos_sin_reiniciar(home: Path, client: TestClient, panel: TestClient):
    cfg = client.app.state.cfg
    r = panel.post("/panel/modelos", data={
        "transcribir": "gemini-3.5-transcribe", "clasificar": "claude-haiku-5-5", "clasificar_esfuerzo": "low",
        "apunte": "claude-sonnet-5-5", "apunte_esfuerzo": "max"})
    assert r.status_code == 303 and mensaje(r) == ("Guardado: el próximo job ya usa esta configuración.", "ok")
    guardada = config_guardada(home)
    assert "transcribir" not in guardada and "claude" not in guardada  # sólo las claves que cambian
    assert guardada["clasificar"] == {"modelo": "claude-haiku-5-5", "esfuerzo": "low"}
    assert guardada["apunte"] == {"modelo": "claude-sonnet-5-5", "esfuerzo": "max"}
    # El cfg vivo (el mismo objeto que usa el worker) ya lo ve.
    assert (cfg.clasificar.motor, cfg.clasificar.modelo, cfg.clasificar.esfuerzo) == ("claude", "claude-haiku-5-5", "low")
    assert (cfg.apunte.modelo, cfg.apunte.esfuerzo) == ("claude-sonnet-5-5", "max")
    assert cfg.modelo_apunte(cfg.materia("prueba")) == ("claude-sonnet-5-5", "max", "global")

    # La facturación va en su propio formulario y no toca los modelos.
    r = panel.post("/panel/modelos", data={"facturacion": "max_luego_api", "reintentar_api_h": "1,5"})
    assert mensaje(r)[1] == "ok"
    guardada = config_guardada(home)
    assert guardada["claude"] == {"facturacion": "max_luego_api", "reintentar_api_h": 1.5}
    assert guardada["apunte"] == {"modelo": "claude-sonnet-5-5", "esfuerzo": "max"}
    assert (cfg.claude.facturacion, cfg.claude.reintentar_api_h) == ("max_luego_api", 1.5)
    html = panel.get("/panel/modelos").text
    assert '<option value="max_luego_api" selected>' in html and 'value="1.5"' in html
    assert '<option value="claude-sonnet-5-5" selected>' in select_de(html, "apunte")

    # Esfuerzo vacío = el default del modelo (null en el yaml).
    panel.post("/panel/modelos", data={"apunte": "claude-sonnet-5-5", "apunte_esfuerzo": ""})
    assert config_guardada(home)["apunte"]["esfuerzo"] is None and cfg.apunte.esfuerzo is None


@pytest.mark.parametrize("datos, error", [
    ({"apunte": "claude-inventado-9"}, "no está en la lista"),
    ({"apunte": ""}, "no está en la lista"),
    ({"apunte": "gemini-flash-latest"}, "no está en la lista"),           # no le sirve al apunte
    ({"clasificar": "gemini-3.5-transcribe"}, "no está en la lista"),     # transcribe, no clasifica
    ({"transcribir": "claude-opus-5-5"}, "no está en la lista"),
    ({"apunte": "claude-opus-5-5", "apunte_esfuerzo": "altísimo"}, "esfuerzo inválido"),
    ({"facturacion": "gratis"}, "modo de facturación inválido"),
    ({"reintentar_api_h": "0"}, "mayor que 0"),
    ({"reintentar_api_h": "-2"}, "mayor que 0"),
    ({"reintentar_api_h": "seis"}, "mayor que 0"),
    (None, "no llegó nada"),
])
def test_modelos_invalidos_no_escriben(home: Path, client: TestClient, panel: TestClient, datos, error):
    # Un modelo bueno y uno malo en el mismo envío: no se escribe ninguno.
    antes = (home / "config.yaml").read_bytes()
    r = panel.post("/panel/modelos", data={"clasificar": "claude-haiku-5-5", **datos} if datos is not None else {})
    msg, tipo = mensaje(r)
    assert r.status_code == 303 and tipo == "error" and error in msg, msg
    assert (home / "config.yaml").read_bytes() == antes
    assert client.app.state.cfg.clasificar.modelo == "claude-sonnet-5-5"


def test_clasificar_con_gemini_guarda_el_motor(home: Path, client: TestClient, panel: TestClient):
    cfg = client.app.state.cfg
    r = panel.post("/panel/modelos", data={"clasificar": "gemini-flash-latest", "clasificar_esfuerzo": ""})
    assert mensaje(r)[1] == "ok"
    assert config_guardada(home)["clasificar"] == {"motor": "gemini", "modelo": "gemini-flash-latest", "esfuerzo": None}
    assert (cfg.clasificar.motor, cfg.clasificar.modelo) == ("gemini", "gemini-flash-latest")
    html = panel.get("/panel/modelos").text
    assert '<option value="gemini-flash-latest" selected>' in select_de(html, "clasificar")
    # Y de vuelta a Claude: el motor también vuelve.
    panel.post("/panel/modelos", data={"clasificar": "claude-sonnet-5-5", "clasificar_esfuerzo": "medium"})
    assert (cfg.clasificar.motor, cfg.clasificar.modelo, cfg.clasificar.esfuerzo) == ("claude", "claude-sonnet-5-5", "medium")


def test_reintentar_api_y_plan_max(home: Path, client: TestClient, panel: TestClient):
    app = client.app
    poner_claves(app, home)
    ahora = time.time()
    with app.state.base.conexion() as conn:
        conn.execute("INSERT INTO facturacion (medio, estado, motivo, desde, huella_clave) "
                     "VALUES ('api', 'sin_creditos', 'credit balance is too low', ?, ?)",
                     (ahora, huella(CLAVE_ANTHROPIC)))
        conn.execute("INSERT INTO facturacion (medio, estado, motivo, desde, hasta) "
                     "VALUES ('max', 'sin_cupo', 'usage limit reached', ?, ?)", (ahora, ahora + 7200))
    apunte = posponer(app, "apunte")
    otro = posponer(app, "transcribir")  # no es de Claude: no se toca

    r = panel.post("/panel/modelos/reintentar", data={"medio": "api"})
    msg, tipo = mensaje(r)
    assert tipo == "ok" and msg.startswith("Se vuelve a probar la API") and "1 job(s)" in msg
    with app.state.base.conexion() as conn:
        medios = [f[0] for f in conn.execute("SELECT medio FROM facturacion")]
    assert medios == ["max"]
    assert run_after(app, apunte) <= time.time() and run_after(app, otro) > time.time() + 3000
    html = panel.get("/panel/modelos").text
    assert ">Reintentar API</button>" not in html and ">Reintentar plan Max</button>" in html

    clasificar = posponer(app, "clasificar")
    r = panel.post("/panel/modelos/reintentar", data={"medio": "max"})
    msg, tipo = mensaje(r)
    assert tipo == "ok" and msg.startswith("Se vuelve a probar el plan Max") and "1 job(s)" in msg
    with app.state.base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM facturacion").fetchone()[0] == 0
    assert run_after(app, clasificar) <= time.time()
    assert "no había jobs esperando" in mensaje(panel.post("/panel/modelos/reintentar", data={"medio": "api"}))[0]
    assert mensaje(panel.post("/panel/modelos/reintentar", data={"medio": "gemini"}))[1] == "error"


def test_cambiar_el_modo_despierta_los_jobs_pospuestos(client: TestClient, panel: TestClient):
    app = client.app
    job = posponer(app, "apunte")
    r = panel.post("/panel/modelos", data={"facturacion": "api_luego_max", "reintentar_api_h": "6"})
    assert "reintentan" not in mensaje(r)[0] and run_after(app, job) > time.time()  # el modo no cambió
    r = panel.post("/panel/modelos", data={"facturacion": "max", "reintentar_api_h": "6"})
    assert "se reintentan ya 1 job(s)" in mensaje(r)[0]
    assert run_after(app, job) <= time.time()


def test_redescubrir_sin_claves_no_toca_nada(client: TestClient, panel: TestClient, monkeypatch):
    def prohibido(_cfg):
        pytest.fail("no tenía que crear un cliente real de la API")

    monkeypatch.setattr(modelos, "_cliente_anthropic", prohibido)
    monkeypatch.setattr(modelos, "_cliente_google", prohibido)
    r = panel.post("/panel/modelos/redescubrir")
    msg, tipo = mensaje(r)
    assert tipo == "aviso" and "Anthropic: no actualicé el catálogo (falta ANTHROPIC_API_KEY" in msg
    assert "Google: no actualicé el catálogo (falta GEMINI_API_KEY" in msg
    html = panel.get("/panel/modelos").text
    assert "No se actualizó: falta ANTHROPIC_API_KEY" in html and "Se mantuvo lo que había" in html
    assert '<option value="claude-opus-5-5" selected>' in select_de(html, "apunte")  # el catálogo sigue igual


def test_redescubrir_muestra_el_resumen(client: TestClient, panel: TestClient, monkeypatch):
    llamadas = []

    def falso(cfg, base):
        llamadas.append(cfg)
        r_anthropic = modelos.DescubrimientoProveedor(modelos.ANTHROPIC, ok=True, listados=9, cantidad=5,
                                                      nuevos=["claude-nuevo-6"], desaparecidos=["claude-fable-5-1"])
        r_google = modelos.DescubrimientoProveedor(modelos.GOOGLE, ok=False,
                                                   detalle="falta GEMINI_API_KEY en Classmer-data\\.env")
        return modelos.ResumenDescubrimiento("2026-10-16T10:00:00-03:00",
                                             {modelos.ANTHROPIC: r_anthropic, modelos.GOOGLE: r_google})

    monkeypatch.setattr(modelos, "redescubrir", falso)
    r = panel.post("/panel/modelos/redescubrir")
    msg, tipo = mensaje(r)
    assert llamadas == [client.app.state.cfg] and tipo == "aviso"
    assert "Anthropic: 5 modelos para Classmer (de 9 que lista la API); nuevos: claude-nuevo-6" in msg
    assert "no aparecieron: claude-fable-5-1" in msg and "Google: no actualicé el catálogo" in msg
    html = panel.get(r.headers["location"]).text
    assert 'class="msg aviso"' in html and "claude-nuevo-6" in html

    # Una sola corrida a la vez.
    assert panel_mod.REDESCUBRIENDO.acquire(blocking=False)
    try:
        assert "disabled" in panel.get("/panel/modelos").text
        msg, tipo = mensaje(panel.post("/panel/modelos/redescubrir"))
        assert "ya se está redescubriendo" in msg.lower() and len(llamadas) == 1
    finally:
        panel_mod.REDESCUBRIENDO.release()
    panel.post("/panel/modelos/redescubrir")
    assert len(llamadas) == 2 and not panel_mod.REDESCUBRIENDO.locked()


def test_modelo_elegido_que_no_aparecio_queda_marcado(client: TestClient, panel: TestClient):
    with client.app.state.base.conexion() as conn:
        conn.execute("UPDATE modelos SET presente = 0 WHERE id = 'claude-opus-5-5'")
    # Uno que ni está en el catálogo (escrito a mano en config.yaml): sigue elegido.
    client.app.state.config_viva.editar_modelos({"transcribir": {"modelo": "gemini-viejo-transcribe"}})
    html = panel.get("/panel/modelos").text
    apunte = select_de(html, "apunte")
    assert ('<option value="claude-opus-5-5" selected>Claude Opus 5.5 (claude-opus-5-5) · '
            "no apareció en el último descubrimiento</option>") in apunte
    assert "Ojo: el elegido (claude-opus-5-5) no apareció en el último descubrimiento." in html
    # En clasificar no está elegido: se marca en la opción, sin el aviso al lado.
    assert "Claude Opus 5.5 (claude-opus-5-5) · no apareció" in select_de(html, "clasificar")
    transcribir = select_de(html, "transcribir")
    assert ('<option value="gemini-viejo-transcribe" selected>gemini-viejo-transcribe · no está en el catálogo'
            in transcribir)
    assert "Ojo: el elegido (gemini-viejo-transcribe) no está en el catálogo." in html
    assert "transcribir.modelo" in html  # en la tabla del catálogo, con dónde está elegido
    # El valor actual vale aunque no esté en el catálogo (no se cambia solo).
    r = panel.post("/panel/modelos", data={"transcribir": "gemini-viejo-transcribe", "apunte": "claude-opus-5-5"})
    assert mensaje(r)[1] == "ok"


# --- modelo del apunte por materia ---------------------------------------------------------------

def form_prueba(app, **extra) -> dict[str, str]:
    return {"nombre": "Materia de Prueba", "activa": "on", "carpeta": str(app.state.cfg.materia("prueba").carpeta),
            "carpeta_clases": "Clases-Classmer", "perfil": "", "titulo_regex": "Prueba", "calendario": "",
            "tipos_clase": "teorica, practica", "numero_clase_inicial": "1", "proximo_numero": "1",
            "proximo_original": "1", "estilo": "", **extra}


def test_modelo_del_apunte_por_materia(home: Path, client: TestClient, panel: TestClient):
    app, cfg = client.app, client.app.state.cfg
    html = panel.get("/panel/materias/prueba").text
    assert 'value="heredar" id="apunte_heredar" checked' in html
    assert "Heredar del global (Claude Opus 5.5 (claude-opus-5-5), esfuerzo high)" in html
    assert '<option value="claude-opus-5-5" selected>' in select_de(html, "apunte_modelo")
    assert "gemini" not in select_de(html, "apunte_modelo")

    # Propio, con esfuerzo.
    r = panel.post("/panel/materias/prueba", data=form_prueba(
        app, apunte_origen="propio", apunte_modelo="claude-sonnet-5-5", apunte_esfuerzo="max"))
    assert r.status_code == 303, r.text
    assert config_guardada(home)["materias"][0]["apunte"] == {"modelo": "claude-sonnet-5-5", "esfuerzo": "max"}
    assert cfg.modelo_apunte(cfg.materia("prueba")) == ("claude-sonnet-5-5", "max", "materia")
    html = panel.get("/panel/materias/prueba").text
    assert 'value="propio" id="apunte_propio" checked' in html
    assert '<option value="claude-sonnet-5-5" selected>' in select_de(html, "apunte_modelo")
    assert '<option value="max" selected>' in select_de(html, "apunte_esfuerzo")
    html = panel.get("/panel/modelos").text
    assert 'href="/panel/materias/prueba">Materia de Prueba</a>' in html and "materia prueba (esfuerzo max)" in html

    # Un formulario sin la sección (otro cliente) no la toca.
    assert panel.post("/panel/materias/prueba", data=form_prueba(app)).status_code == 303
    assert config_guardada(home)["materias"][0]["apunte"]["modelo"] == "claude-sonnet-5-5"

    # Propio sin esfuerzo: hereda el esfuerzo global.
    panel.post("/panel/materias/prueba", data=form_prueba(
        app, apunte_origen="propio", apunte_modelo="claude-haiku-5-5", apunte_esfuerzo=""))
    assert config_guardada(home)["materias"][0]["apunte"] == {"modelo": "claude-haiku-5-5"}
    assert cfg.modelo_apunte(cfg.materia("prueba")) == ("claude-haiku-5-5", "high", "materia")

    # De vuelta a heredar: la clave `apunte` desaparece de la materia.
    panel.post("/panel/materias/prueba", data=form_prueba(
        app, apunte_origen="heredar", apunte_modelo="claude-haiku-5-5", apunte_esfuerzo=""))
    assert "apunte" not in config_guardada(home)["materias"][0]
    assert cfg.modelo_apunte(cfg.materia("prueba")) == ("claude-opus-5-5", "high", "global")


def test_modelo_del_apunte_invalido_conserva_lo_elegido(home: Path, client: TestClient, panel: TestClient):
    antes = (home / "config.yaml").read_bytes()
    r = panel.post("/panel/materias/prueba", data=form_prueba(
        client.app, apunte_origen="propio", apunte_modelo="gpt-5", apunte_esfuerzo="low"))
    assert r.status_code == 422 and "no está en la lista" in r.text
    assert (home / "config.yaml").read_bytes() == antes
    assert 'value="propio" id="apunte_propio" checked' in r.text
    assert '<option value="gpt-5" selected>' in select_de(r.text, "apunte_modelo")
    assert '<option value="low" selected>' in select_de(r.text, "apunte_esfuerzo")
    # Otro error (regex) con un modelo válido: se conserva el modelo elegido.
    r = panel.post("/panel/materias/prueba", data=form_prueba(
        client.app, titulo_regex="([mal", apunte_origen="propio", apunte_modelo="claude-fable-5-1"))
    assert r.status_code == 422 and '<option value="claude-fable-5-1" selected>' in select_de(r.text, "apunte_modelo")
    assert (home / "config.yaml").read_bytes() == antes


def test_materia_nueva_con_modelo_propio(home: Path, client: TestClient, panel: TestClient):
    assert 'value="heredar" id="apunte_heredar" checked' in panel.get("/panel/materias/nueva").text
    r = panel.post("/panel/materias/nueva", data={
        "id": "historia", "nombre": "Historia", "activa": "on", "tipos_clase": "teorica",
        "apunte_origen": "propio", "apunte_modelo": "claude-sonnet-5-5", "apunte_esfuerzo": "medium"})
    assert r.status_code == 303, r.text
    nueva = config_guardada(home)["materias"][-1]
    assert nueva["id"] == "historia" and nueva["apunte"] == {"modelo": "claude-sonnet-5-5", "esfuerzo": "medium"}
    r = panel.post("/panel/materias/nueva", data={
        "id": "otra2", "nombre": "Otra", "tipos_clase": "teorica", "apunte_origen": "propio",
        "apunte_modelo": "gemini-flash-latest", "apunte_esfuerzo": ""})
    assert r.status_code == 422 and "otra2" not in (home / "config.yaml").read_text(encoding="utf-8")


# --- versiones, publicación y costos en la clase -------------------------------------------------

def armar_versiones(app, sid: str = SID) -> Path:
    """v1 y v2 compilaron (v2 sigue publicada), v3 (la vigente) no compiló; una carpeta archivada y
    otra que no se pudo archivar; jobs pagados con la API, con el plan Max y uno de Gemini."""
    cfg, base = app.state.cfg, app.state.base
    carpeta = armar_clase(app, sid)
    (carpeta / "apunte" / "v1" / "apunte.tex").write_text("\\section{Gradiente v1}\n", encoding="utf-8")
    for n, pdf in ((2, True), (3, False)):
        (carpeta / "apunte" / f"v{n}").mkdir()
        (carpeta / "apunte" / f"v{n}" / "apunte.tex").write_text(f"\\section{{Gradiente v{n} — ñandú}}\n",
                                                                 encoding="utf-8")
        if pdf:
            (carpeta / "apunte" / f"v{n}" / "apunte.pdf").write_bytes(b"%PDF v2")
    raiz = cfg.materia("prueba").ruta_clases()
    vigente = raiz / "2026-10-15 Clase 01 - Gradiente y curvas de nivel"
    trabada = raiz / "2026-10-15 Clase 01 - Gradiente (v2)"
    archivo = cfg.datos / "apuntes-anteriores" / "prueba" / "2026-10-15 19.00.00 2026-10-15 Clase 01 - Gradiente"
    momento = ahora_iso()
    with base.conexion() as conn:
        conn.execute("UPDATE apuntes SET actual = 0, esfuerzo = 'high', facturacion = 'max', costo = 0.9, "
                     "prompt_version = 'aaa111bbb222' WHERE sesion_id = ?", (sid,))
        for n, actual, modelo, esfuerzo, fact, compilo, costo in (
                (2, 0, "claude-opus-5-5", "high", "max", 1, 1.8),
                (3, 1, "claude-sonnet-5-5", "max", "api", 0, 0.0123)):
            conn.execute(
                "INSERT INTO apuntes (sesion_id, version, actual, motor, modelo, esfuerzo, facturacion, "
                "prompt_version, ruta_tex, ruta_pdf, compilo, titulo, costo, creado) "
                "VALUES (?, ?, ?, 'claude-agent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, n, actual, modelo, esfuerzo, fact, f"prompt{n}abcdef", f"apunte/v{n}/apunte.tex",
                 f"apunte/v{n}/apunte.pdf" if compilo else None, compilo, f"Gradiente v{n}", costo, momento))
        for ruta, version, estado, arch, aviso in (
                (raiz / "2026-10-15 Clase 01 - Gradiente", 1, "archivada", str(archivo), None),
                (trabada, 1, "sin_archivar", None, AVISO_EN_USO),
                (vigente, 2, "vigente", None, None)):
            conn.execute("INSERT INTO publicaciones (sesion_id, ruta, version, compilo, estado, archivo, aviso, "
                         "creado, actualizado) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                         (sid, str(ruta), version, estado, arch, aviso, momento, momento))
        actualizar_sesion(conn, sid, cfg.zona_horaria, ruta_publicada=str(vigente), estado="error",
                          error="el apunte no compiló")
        for tipo, fact, costo in (("clasificar", "api", 0.0042), ("apunte", "max", 1.8), ("transcribir", None, 0.25)):
            job = cola.encolar(conn, tipo, sid)
            cola.completar(conn, job, log=f"{tipo} hecho", costo=costo, facturacion=fact)
    return carpeta


def test_clase_versiones_publicacion_y_costos(client: TestClient, panel: TestClient):
    armar_versiones(client.app)
    html = panel.get(f"/panel/clases/{SID}").text
    for esperado in (
            "Versión vigente: v3", f'href="/panel/clases/{SID}/apunte.tex"', f'href="/panel/clases/{SID}/apunte.pdf?v=',
            "claude-sonnet-5-5 · esfuerzo max · facturación: API · costo: US$ 0.0123 (API)", "prompt prompt3abc",
            "La versión v3 no compiló: en la carpeta de la materia sigue la v2.",
            "Versiones anteriores", f"/panel/clases/{SID}/apunte/v2.pdf", f"/panel/clases/{SID}/apunte/v2.tex",
            f"/panel/clases/{SID}/apunte/v1.pdf", f"/panel/clases/{SID}/apunte/v1.tex",
            "US$ 1.80 equivalente API (plan Max, no se cobra aparte)", "aaa111bbb2",
            # publicación: la vigente, la archivada (con dónde quedó) y la que no se pudo archivar
            "Publicada en", "Gradiente y curvas de nivel", "Archivada en", "apuntes-anteriores",
            "sin archivar", "Una carpeta publicada antes quedó sin archivar: " + AVISO_EN_USO, "Gradiente (v2)",
            # jobs: pago y costo
            "<th>Pago</th>", "<th>Costo</th>", "US$ 0.0042 (API)", "US$ 0.25<"):
        assert esperado in html, esperado
    assert "/apunte/v3.pdf" not in html  # v3 no tiene PDF (y la vigente no va en "anteriores")


def test_archivos_de_las_versiones(client: TestClient, panel: TestClient):
    armar_versiones(client.app)
    r = panel.get(f"/panel/clases/{SID}/apunte.pdf")  # la vigente no compiló: el último PDF que sí
    assert r.status_code == 200 and r.content == b"%PDF v2"
    r = panel.get(f"/panel/clases/{SID}/apunte.tex")
    assert r.status_code == 200 and r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.text == "\\section{Gradiente v3 — ñandú}\n"
    r = panel.get(f"/panel/clases/{SID}/apunte/v1.pdf")
    assert r.status_code == 200 and r.content == b"%PDF-1.5 falso" and r.headers["content-type"] == "application/pdf"
    r = panel.get(f"/panel/clases/{SID}/apunte/v2.tex")
    assert r.status_code == 200 and "Gradiente v2 — ñandú" in r.text
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    for malo in ("v3.pdf", "v9.pdf", "v9.tex", "vx.pdf", "v-1.pdf", "v1.exe", "v1.pdf.bak", "V1.pdf", "v1",
                 "apunte.pdf", "..%2F..%2Fconfig.yaml", "v1.pdf%2F..%2F..%2F..%2Fconfig.yaml",
                 "..%5C..%5Cconfig.yaml", "v1%2F..%2F..%2Ftranscript.json", "v99999999.pdf"):
        assert panel.get(f"/panel/clases/{SID}/apunte/{malo}").status_code == 404, malo
    assert panel.get("/panel/clases/no-existe/apunte/v1.pdf").status_code == 404
    assert panel.get("/panel/clases/no-existe/apunte.tex").status_code == 404


def test_clase_sin_versiones_anteriores(client: TestClient, panel: TestClient):
    armar_clase(client.app)
    html = panel.get(f"/panel/clases/{SID}").text
    assert "Versión vigente: v1" in html and "Versiones anteriores" not in html
    assert "no compiló" not in html and "Ver .tex" not in html  # armar_clase no deja el .tex de v1
    assert panel.get(f"/panel/clases/{SID}/apunte.tex").status_code == 404


def test_estado_muestra_facturacion_avisos_y_costos(client: TestClient, panel: TestClient):
    armar_versiones(client.app)
    html = panel.get("/panel/estado").text
    for esperado in ("Facturación de Claude", "API y, si se queda sin créditos, plan Max", 'href="/panel/modelos"',
                     "No hay ANTHROPIC_API_KEY", "Carpetas publicadas que no se pudieron archivar",
                     f'<a href="/panel/clases/{SID}">Gradiente y curvas de nivel</a>: {AVISO_EN_USO}',
                     "Últimos trabajos con Claude", "US$ 0.0042 (API)",
                     "US$ 1.80 equivalente API (plan Max, no se cobra aparte)"):
        assert esperado in html, esperado
    # El job de Gemini (sin facturación de Claude) no va en "Últimos trabajos con Claude".
    assert "US$ 0.25" not in html


# --- secretos ------------------------------------------------------------------------------------

def test_ninguna_pagina_muestra_las_claves(home: Path, client: TestClient, panel: TestClient, monkeypatch):
    app = client.app
    poner_claves(app, home)
    armar_versiones(app)
    # Aunque una clave se cuele en un error guardado en la base, la página no la muestra.
    ahora = time.time()
    with app.state.base.conexion() as conn:
        conn.execute("INSERT INTO facturacion (medio, estado, motivo, desde, huella_clave) VALUES "
                     "('api', 'clave_invalida', ?, ?, ?)",
                     (f"invalid x-api-key {CLAVE_ANTHROPIC}", ahora, huella(CLAVE_ANTHROPIC)))
        conn.execute("INSERT INTO descubrimientos (proveedor, momento, ok, detalle) VALUES ('google', ?, 0, ?)",
                     (ahora_iso(), f"APIError 400: key={CLAVE_GEMINI} inválida"))
        job = cola.encolar(conn, "segmentar", SID)
        conn.execute("UPDATE jobs SET error = ? WHERE id = ?", (f"falló con {CLAVE_ANTHROPIC}", job))
    paginas = ["/panel/clases", f"/panel/clases/{SID}", "/panel/materias", "/panel/materias/prueba",
               "/panel/materias/nueva", "/panel/pendientes", "/panel/pendientes?materia=prueba", "/panel/estilo",
               "/panel/modelos", "/panel/estado", "/panel/importar", f"/panel/clases?msg={CLAVE_ANTHROPIC}"]
    for ruta in paginas:
        r = panel.get(ruta)
        assert r.status_code == 200, ruta
        assert CLAVE_ANTHROPIC not in r.text and CLAVE_GEMINI not in r.text, ruta
    modelos_html = panel.get("/panel/modelos").text
    assert "[clave oculta]" in modelos_html and "la API key no es válida desde" in modelos_html

    def con_clave(cfg, base):
        raise RuntimeError(f"algo raro con {CLAVE_ANTHROPIC}")

    monkeypatch.setattr(modelos, "redescubrir", con_clave)
    r = panel.post("/panel/modelos/redescubrir")
    assert CLAVE_ANTHROPIC not in r.headers["location"] and mensaje(r)[1] == "error"
    assert mensaje(r)[0] == "No se pudo redescubrir: RuntimeError: algo raro con [clave oculta]"


# --- casos de borde (revisión) -------------------------------------------------------------------

def cambiar_config(app, home: Path, **secciones) -> None:
    """Escribe secciones en el config.yaml temporal (como si las hubiera editado el usuario a mano)."""
    datos = config_guardada(home)
    datos.update(secciones)
    (home / "config.yaml").write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")
    app.state.config_viva.recargar(forzar=True)


def test_apunte_sin_modelo_fijo_no_elige_el_primero(home: Path, client: TestClient, panel: TestClient):
    """`apunte.modelo: null` (el default del Agent SDK): el select no puede quedar sin opción
    elegida, porque el navegador mandaría la primera de la lista al guardar otra cosa."""
    app, cfg = client.app, client.app.state.cfg
    cambiar_config(app, home, apunte={"modelo": None, "esfuerzo": "high"})
    assert cfg.apunte.modelo is None
    html = panel.get("/panel/modelos").text
    apunte = select_de(html, "apunte")
    assert '<option value="" selected>(ninguno fijo: el default)</option>' in apunte
    assert apunte.count(" selected") == 1
    assert '<option value=""' not in select_de(html, "transcribir")  # los demás sí tienen uno elegido
    # Guardar la sección con la opción vacía cambia el esfuerzo y deja el modelo en null.
    r = panel.post("/panel/modelos", data={"transcribir": "gemini-3.5-transcribe", "clasificar": "claude-sonnet-5-5",
                                           "clasificar_esfuerzo": "medium", "apunte": "", "apunte_esfuerzo": "low"})
    assert mensaje(r) == ("Guardado: el próximo job ya usa esta configuración.", "ok")
    assert (cfg.apunte.modelo, cfg.apunte.esfuerzo) == (None, "low")
    assert config_guardada(home)["apunte"] == {"modelo": None, "esfuerzo": "low"}
    # La materia que hereda tampoco elige uno por su cuenta.
    html = panel.get("/panel/materias/prueba").text
    assert "Heredar del global (el modelo por defecto, esfuerzo low)" in html
    assert '<option value="" selected>' in select_de(html, "apunte_modelo")
    # Ya con un modelo elegido, la opción vacía no vale.
    assert mensaje(panel.post("/panel/modelos", data={"apunte": "claude-sonnet-5-5"}))[1] == "ok"
    r = panel.post("/panel/modelos", data={"apunte": ""})
    assert mensaje(r)[1] == "error" and cfg.apunte.modelo == "claude-sonnet-5-5"


def test_alias_viejo_fuera_del_catalogo(home: Path, client: TestClient, panel: TestClient):
    """Un alias escrito a mano ("opus") sigue elegido y marcado, se puede volver a guardar sin
    cambiar el motor de clasificar, y una materia lo puede heredar o fijar."""
    app, cfg = client.app, client.app.state.cfg
    cambiar_config(app, home, clasificar={"motor": "claude", "modelo": "opus"}, apunte={"modelo": "opus"})
    html = panel.get("/panel/modelos").text
    clasificar = select_de(html, "clasificar")
    assert '<optgroup label="Otros"><option value="opus" selected>opus · no está en el catálogo</option>' in clasificar
    assert clasificar.count(" selected") == 1
    assert "Ojo: el elegido (opus) no está en el catálogo." in html
    antes = (home / "config.yaml").read_bytes()
    r = panel.post("/panel/modelos", data={"clasificar": "opus", "clasificar_esfuerzo": "medium",
                                           "apunte": "opus", "apunte_esfuerzo": "high"})
    assert mensaje(r)[1] == "ok"
    assert (cfg.clasificar.motor, cfg.clasificar.modelo, cfg.apunte.modelo) == ("claude", "opus", "opus")
    assert (home / "config.yaml").read_bytes() == antes  # nada cambió: no se reescribe
    # Otro alias inventado no entra por más que se parezca.
    r = panel.post("/panel/modelos", data={"clasificar": "sonnet"})
    assert mensaje(r)[1] == "error" and cfg.clasificar.modelo == "opus"
    # La materia hereda "opus" y lo puede fijar como propio (es el elegido actual).
    r = panel.post("/panel/materias/prueba", data=form_prueba(app, apunte_origen="propio", apunte_modelo="opus"))
    assert r.status_code == 303, r.text
    assert config_guardada(home)["materias"][0]["apunte"] == {"modelo": "opus"}


def test_nombres_de_version_ambiguos_dan_404(client: TestClient, panel: TestClient):
    armar_versiones(client.app)
    assert panel.get(f"/panel/clases/{SID}/apunte/v1.pdf").status_code == 200
    # Dígitos no ASCII ("v١" = v1 en arábigo) y ceros adelante no son otro nombre de la v1.
    for malo in ("v%D9%A1.pdf", "v01.pdf", "v0.pdf", "v%EF%BC%91.tex", "v1.pdf%00", "v1.PDF"):
        assert panel.get(f"/panel/clases/{SID}/apunte/{malo}").status_code == 404, malo


def test_el_tex_no_muestra_las_claves(home: Path, client: TestClient, panel: TestClient):
    app = client.app
    poner_claves(app, home)
    carpeta = armar_versiones(app)
    (carpeta / "apunte" / "v3" / "apunte.tex").write_text(f"% {CLAVE_ANTHROPIC}\n", encoding="utf-8")
    (carpeta / "apunte" / "v2" / "apunte.tex").write_text(f"% {CLAVE_GEMINI}\n", encoding="utf-8")
    for ruta in (f"/panel/clases/{SID}/apunte.tex", f"/panel/clases/{SID}/apunte/v2.tex"):
        r = panel.get(ruta)
        assert r.status_code == 200 and "[clave oculta]" in r.text, ruta
        assert CLAVE_ANTHROPIC not in r.text and CLAVE_GEMINI not in r.text, ruta


def test_clase_la_mas_nueva_no_compilo_y_la_publicada_sigue_vigente(client: TestClient, panel: TestClient):
    # Así lo deja publicar: la v3 no compiló y la v2 publicada vuelve a ser la actual.
    armar_versiones(client.app)
    with client.app.state.base.conexion() as conn:
        conn.execute("UPDATE apuntes SET actual = (version = 2) WHERE sesion_id = ?", (SID,))
    html = panel.get(f"/panel/clases/{SID}").text
    for esperado in ("Versión vigente: v2", "La versión v3 no compiló: en la carpeta de la materia sigue la v2, "
                     "que es la vigente.", "Otras versiones", f"/panel/clases/{SID}/apunte/v3.tex"):
        assert esperado in html, esperado
    assert "Versiones anteriores" not in html
    r = panel.get(f"/panel/clases/{SID}/apunte.pdf")
    assert r.status_code == 200 and r.content == b"%PDF v2"


# --- el PDF vigente, siempre el de ahora ---------------------------------------------------------

def _urls_del_pdf(panel: TestClient) -> set[str]:
    """Las URLs del PDF vigente en las cuatro partes del panel que lo muestran: el iframe y "Abrir
    PDF" de la clase, la lista de clases y las clases recientes de Pendientes."""
    clase = panel.get(f"/panel/clases/{SID}").text
    iframe = re.search(r'<iframe class="pdf" src="([^"]+)"', clase).group(1)
    assert iframe.endswith("#view=FitH"), iframe  # la marca va antes del fragmento
    abrir = re.search(r'<a class="boton" href="([^"]+)" target="_blank">Abrir PDF</a>', clase).group(1)
    del_pdf = rf'href="(/panel/clases/{SID}/apunte\.pdf[^"]*)"'
    lista = re.findall(del_pdf, panel.get("/panel/clases").text)
    pendientes = re.findall(del_pdf, panel.get("/panel/pendientes?materia=prueba").text)
    assert len(lista) == len(pendientes) == 1
    return {iframe.removesuffix("#view=FitH"), abrir, *lista, *pendientes}


def test_la_url_del_pdf_cambia_con_cada_version(client: TestClient, panel: TestClient):
    # La URL sola es la misma para todas las versiones: con la marca, el navegador no puede mostrar
    # una vieja de su caché (pasó con una v2 en el uso real).
    app = client.app
    carpeta = armar_clase(app)
    urls = _urls_del_pdf(panel)
    assert len(urls) == 1  # la misma en todos lados
    (v1,) = urls
    assert re.fullmatch(rf"/panel/clases/{SID}/apunte\.pdf\?v=[0-9a-f]{{12}}", v1), v1
    assert panel.get(v1).content == b"%PDF-1.5 falso"
    assert _urls_del_pdf(panel) == {v1}  # no cambia mientras no cambie el archivo

    # Aparece la v2, como la deja el agente: una fila en apuntes y el PDF en apunte/v2/.
    (carpeta / "apunte" / "v2").mkdir()
    (carpeta / "apunte" / "v2" / "apunte.pdf").write_bytes(b"%PDF v2")
    with app.state.base.conexion() as conn:
        conn.execute("UPDATE apuntes SET actual = 0 WHERE sesion_id = ?", (SID,))
        conn.execute("INSERT INTO apuntes (sesion_id, version, actual, ruta_tex, ruta_pdf, compilo, creado) "
                     "VALUES (?, 2, 1, 'apunte/v2/apunte.tex', 'apunte/v2/apunte.pdf', 1, ?)", (SID, ahora_iso()))
    (v2,) = _urls_del_pdf(panel)
    assert v2 != v1 and panel.get(v2).content == b"%PDF v2"
    assert panel.get(v1).content == b"%PDF v2"  # un link viejo abre la vigente, no un 404

    # La misma v2 recompilada o republicada: mismo número y mismo tamaño, otro archivo.
    pdf = carpeta / "apunte" / "v2" / "apunte.pdf"
    antes = pdf.stat().st_mtime_ns
    pdf.write_bytes(b"%PDF V2")
    os.utime(pdf, ns=(antes + 2_000_000_000, antes + 2_000_000_000))
    (v2_bis,) = _urls_del_pdf(panel)
    assert v2_bis not in (v1, v2) and panel.get(v2_bis).content == b"%PDF V2"


def test_la_url_del_pdf_sin_pdf_y_con_la_vigente_sin_compilar(client: TestClient, panel: TestClient):
    armar_versiones(client.app)  # la v3 (vigente) no compiló: se ve el PDF de la v2
    (url,) = _urls_del_pdf(panel)
    assert url.startswith(f"/panel/clases/{SID}/apunte.pdf?v=") and panel.get(url).content == b"%PDF v2"
    with client.app.state.base.conexion() as conn:
        conn.execute("UPDATE apuntes SET compilo = 0 WHERE sesion_id = ?", (SID,))
    html = panel.get(f"/panel/clases/{SID}").text
    assert "<iframe" not in html and "Abrir PDF" not in html and "Todavía no hay un PDF que haya compilado." in html
    assert f"/panel/clases/{SID}/apunte.pdf" not in panel.get("/panel/clases").text


def test_el_pdf_y_el_tex_vigentes_se_revalidan_siempre(client: TestClient, panel: TestClient):
    carpeta = armar_clase(client.app)
    (carpeta / "apunte" / "v1" / "apunte.tex").write_text("\\section{Gradiente}\n", encoding="utf-8")
    (url,) = _urls_del_pdf(panel)
    for ruta in (f"/panel/clases/{SID}/apunte.pdf", url, f"/panel/clases/{SID}/apunte.tex",
                 f"/panel/clases/{SID}/apunte/v1.pdf", f"/panel/clases/{SID}/apunte/v1.tex"):
        r = panel.get(ruta)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", ruta
    assert panel.get(f"/panel/clases/{SID}/apunte.pdf").headers["content-disposition"] == 'inline; filename="apunte.pdf"'
    # También por rangos, como lo pide el visor de PDF del navegador.
    r = panel.get(url, headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == b"%PDF" and r.headers["cache-control"] == "no-cache"


# --- adjuntos ------------------------------------------------------------------------------------

SID2 = "0192f7c2-2222-7000-8000-00000000abcd"
AVISO = 'class="msg aviso aviso-adjuntos"'  # el aviso de adjuntos sin usar (la clase CSS sola está en el <style>)


def pdf_de(paginas: int = 2) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(paginas):
        w.add_blank_page(width=200, height=150)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def imagen_de(formato: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 30, 30)).save(buf, formato)
    return buf.getvalue()


def adjuntar(app, sid: str = SID, contenido: bytes | None = None, nombre: str = "Slides clase 5.pdf", *,
             offset_ms: int | None = None, origen: str = "app") -> dict:
    """Un adjunto como lo deja la API (el mismo adjuntos.guardar)."""
    _, info = adjuntos.guardar(app.state.cfg, app.state.base, sid, adjunto_id=str(uuid.uuid4()),
                               archivo=io.BytesIO(pdf_de() if contenido is None else contenido),
                               nombre_original=nombre, offset_ms=offset_ms, origen=origen)
    return info


def usar_en_el_apunte(app, *ids: str, sid: str = SID) -> None:
    """Como si el apunte vigente los hubiera usado (apuntes.adjuntos)."""
    with app.state.base.conexion() as conn:
        conn.execute("UPDATE apuntes SET adjuntos = ? WHERE sesion_id = ? AND actual = 1", (json.dumps(list(ids)), sid))


def filas_adjuntos(app, sid: str = SID) -> list:
    with app.state.base.conexion() as conn:
        return conn.execute("SELECT * FROM assets WHERE sesion_id = ? AND tipo = 'adjunto' ORDER BY creado, rowid",
                            (sid,)).fetchall()


def seccion(html: str, desde: str, hasta: str) -> str:
    inicio = html.index(desde)
    return html[inicio:html.index(hasta, inicio)]


def test_tamano_legible():
    assert [panel_mod._tamano(n) for n in (None, 0, 840, 1536, 312 * 1024, int(4.2 * 1024 * 1024), 100 * 1024 ** 2,
                                           3 * 1024 ** 3)] == ["", "0 B", "840 B", "1,5 KB", "312 KB", "4,2 MB",
                                                               "100 MB", "3,0 GB"]


def test_clase_lista_los_adjuntos(client: TestClient, panel: TestClient):
    app = client.app
    armar_clase(app)
    contenido = pdf_de(3)
    slides = adjuntar(app, contenido=contenido, offset_ms=2_040_000)
    adjuntar(app, contenido=imagen_de(), nombre="Pizarrón final.png", origen="panel")
    adjuntar(app, contenido=pdf_de(1), nombre='<script>alert("x")</script>.pdf')
    usar_en_el_apunte(app, slides["id"])  # las otras dos llegaron después del apunte vigente
    html = panel.get(f"/panel/clases/{SID}").text
    lista = seccion(html, "<h2>Adjuntos (3)</h2>", "<h2>Fotos (1)</h2>")
    # Abrir (en otra pestaña) y bajar, por el nombre que armó el server.
    assert (f'<a href="/panel/clases/{SID}/adjuntos/Slides_clase_5.pdf" target="_blank">Slides clase 5.pdf</a>'
            in lista)
    assert f'<a href="/panel/clases/{SID}/adjuntos/Slides_clase_5.pdf?descargar=1">Descargar</a>' in lista
    assert f'href="/panel/clases/{SID}/adjuntos/Pizarron_final.png" target="_blank">Pizarrón final.png</a>' in lista
    assert lista.count(">Descargar</a>") == 3
    # Tipo (con páginas), tamaño, minuto que salta el audio, origen y estado con su explicación.
    assert "PDF, 3 páginas" in lista and "imagen PNG" in lista and "PDF, 1 página<" in lista
    assert f">{panel_mod._tamano(len(contenido))}<" in lista
    assert 'href="#" class="t" data-ms="2040000">00:34:00</a>' in lista and lista.count("sin minuto") == 2
    assert "(desde el panel)" in lista
    assert '<span class="estado lista">usado</span> <span class="chico">lo usó el apunte vigente</span>' in lista
    assert lista.count('<span class="estado parcial">sin usar</span>') == 2
    assert "el apunte vigente no lo usa: regeneralo para incluirlo" in lista
    # El nombre original va escapado: nunca como HTML.
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;.pdf" in lista
    # Y el formulario para sumar más, con el mismo formulario que sin adjuntos.
    assert f'action="/panel/clases/{SID}/adjuntos" enctype="multipart/form-data"' in html
    assert 'type="file" name="archivos" multiple required accept="application/pdf,image/jpeg' in html


def test_clase_sin_adjuntos_muestra_solo_el_formulario(client: TestClient, panel: TestClient):
    armar_clase(client.app)
    html = panel.get(f"/panel/clases/{SID}").text
    assert "<h2>Adjuntos</h2>" in html and 'class="adjuntos"' not in html and "Descargar" not in html
    assert f'action="/panel/clases/{SID}/adjuntos"' in html and "PDF, JPEG, PNG, WebP o HEIC, hasta 100 MB" in html
    assert "que el apunte vigente no usa" not in html


def test_adjunto_pendiente_sin_apunte(client: TestClient, panel: TestClient):
    with client.app.state.base.conexion() as conn:  # grabando todavía: lo va a usar el apunte
        sesiones.crear(client.app.state.cfg, conn, sesion_id=SID2, materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio="2026-10-16T09:00:00-03:00", dispositivo="s23")
    adjuntar(client.app, SID2, offset_ms=61_000)
    html = panel.get(f"/panel/clases/{SID2}").text
    assert ('<span class="estado procesando">pendiente</span> <span class="chico">lo usa el apunte en curso o el '
            'próximo<') in html
    assert "que el apunte vigente no usa" not in html


def test_aviso_de_adjuntos_sin_usar(client: TestClient, panel: TestClient):
    app, cfg = client.app, client.app.state.cfg
    armar_clase(app)
    assert AVISO not in panel.get(f"/panel/clases/{SID}").text
    uno = adjuntar(app)
    html = panel.get(f"/panel/clases/{SID}").text
    aviso = seccion(html, AVISO, "</div>")
    assert "Hay 1 adjunto que el apunte vigente no usa." in aviso
    # Junto a Reprocesar (en la misma tarjeta) y antes del reproductor.
    assert (html.index(f'action="/panel/clases/{SID}/reprocesar"') < html.index(AVISO)
            < html.index('class="tarjeta reproductor"'))
    assert f'action="/panel/clases/{SID}/reprocesar"' in aviso
    assert '<input type="hidden" name="desde" value="apunte">' in aviso
    assert "return confirm(" in aviso and "cuesta lo mismo que un apunte nuevo" in aviso
    assert ">Regenerar el apunte con los adjuntos</button>" in aviso
    dos = adjuntar(app, nombre="Notas del profe.pdf")
    assert "Hay 2 adjuntos que el apunte vigente no usa." in panel.get(f"/panel/clases/{SID}").text
    # Si el apunte vigente los usó, no hay aviso.
    usar_en_el_apunte(app, uno["id"], dos["id"])
    html = panel.get(f"/panel/clases/{SID}").text
    assert AVISO not in html and html.count('<span class="estado lista">usado</span>') == 2
    usar_en_el_apunte(app, uno["id"])
    assert "Hay 1 adjunto que el apunte vigente no usa." in panel.get(f"/panel/clases/{SID}").text

    # Con el apunte corriendo (llegó tarde para esa corrida): el aviso, pero todavía sin el botón.
    with app.state.base.conexion() as conn:
        job = cola.encolar(conn, "apunte", SID)
        conn.execute("UPDATE jobs SET estado = 'corriendo' WHERE id = ?", (job,))
        actualizar_sesion(conn, SID, cfg.zona_horaria, estado="procesando", etapa="apunte")
    adjuntos.escribir_manifiesto(cfg.jobs_dir / SID / "apunte", job, [uno["id"]])
    aviso = seccion(panel.get(f"/panel/clases/{SID}").text, AVISO, "</div>")
    assert "Hay 1 adjunto que el apunte vigente no usa." in aviso
    assert "<button" not in aviso and "Cuando termine lo que está en curso" in aviso
    with app.state.base.conexion() as conn:
        cola.completar(conn, job)
        actualizar_sesion(conn, SID, cfg.zona_horaria, estado="lista", etapa="publicar")

    # El botón reprocesa desde apunte: queda en cola y el adjunto pasa a pendiente.
    r = panel.post(f"/panel/clases/{SID}/reprocesar", data={"desde": "apunte"})
    assert r.status_code == 303 and mensaje(r) == ("Reprocesando desde apunte.", "ok")
    s = client.get(f"/api/sesiones/{SID}").json()
    assert s["estado"] == "en_cola" and s["adjuntos_sin_usar"] == 0
    html = panel.get(f"/panel/clases/{SID}").text
    assert AVISO not in html and '<span class="estado procesando">pendiente</span>' in html


def test_subir_un_adjunto_desde_el_panel(client: TestClient, panel: TestClient):
    app, cfg = client.app, client.app.state.cfg
    armar_clase(app)
    contenido = pdf_de(2)
    r = panel.post(f"/panel/clases/{SID}/adjuntos",
                   files={"archivos": ("Slides clase 5.pdf", contenido, "application/pdf")})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/panel/clases/{SID}?")
    assert mensaje(r) == ("Adjunto agregado: Slides clase 5.pdf. El apunte vigente no lo usa: regeneralo para "
                          "incluirlo.", "ok")
    # El mismo almacenamiento que la API, con un id nuevo y origen "panel".
    (fila,) = filas_adjuntos(app)
    assert str(uuid.UUID(fila["id"])) == fila["id"] and fila["ruta"] == "adjuntos/Slides_clase_5.pdf"
    datos = json.loads(fila["datos"])
    assert datos["origen"] == "panel" and datos["nombre_original"] == "Slides clase 5.pdf" and datos["paginas"] == 2
    assert (cfg.sesion_dir(SID) / "adjuntos" / "Slides_clase_5.pdf").read_bytes() == contenido
    registro = (cfg.sesion_dir(SID) / "adjuntos.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["origen"] for x in registro] == ["panel"]
    api = client.get(f"/api/sesiones/{SID}").json()
    assert [a["nombre"] for a in api["adjuntos"]] == ["Slides_clase_5.pdf"] and api["adjuntos_sin_usar"] == 1
    html = panel.get(r.headers["location"]).text
    assert "Adjunto agregado: Slides clase 5.pdf" in html and "(desde el panel)" in html


def test_subir_varios_adjuntos_desde_el_panel(client: TestClient, panel: TestClient):
    app = client.app
    with app.state.base.conexion() as conn:
        sesiones.crear(app.state.cfg, conn, sesion_id=SID2, materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio="2026-10-16T09:00:00-03:00", dispositivo="s23")
    r = panel.post(f"/panel/clases/{SID2}/adjuntos", files=[
        ("archivos", ("Slides.pdf", pdf_de(), "application/pdf")),
        ("archivos", ("pizarra.jpg", imagen_de("JPEG"), "image/jpeg")),
        ("archivos", ("Slides.pdf", pdf_de(1), "application/pdf")),  # el mismo nombre: otro adjunto
    ])
    assert mensaje(r) == ("3 adjuntos agregados. Los va a usar el próximo apunte.", "ok")
    filas = filas_adjuntos(app, SID2)
    assert [f["ruta"] for f in filas] == ["adjuntos/Slides.pdf", "adjuntos/pizarra.jpg", "adjuntos/Slides-2.pdf"]
    assert {json.loads(f["datos"])["origen"] for f in filas} == {"panel"} and len({f["id"] for f in filas}) == 3


def test_subir_adjuntos_errores(client: TestClient, panel: TestClient, monkeypatch):
    app, cfg = client.app, client.app.state.cfg
    armar_clase(app)
    url = f"/panel/clases/{SID}/adjuntos"
    carpeta = cfg.sesion_dir(SID) / "adjuntos"

    # Un .txt (415 en la API): no entra, con el motivo.
    msg, tipo = mensaje(panel.post(url, files={"archivos": ("notas.txt", b"hola\n" * 20, "text/plain")}))
    assert tipo == "error" and msg.startswith("No se pudo adjuntar: notas.txt (tipo de archivo no soportado: ")
    assert "PDF, JPEG, PNG, WebP o HEIC" in msg
    assert not filas_adjuntos(app) and (not carpeta.exists() or not any(carpeta.iterdir()))

    # Uno sí y otro no: se dice cuál entró y cuál no.
    msg, tipo = mensaje(panel.post(url, files=[("archivos", ("Slides.pdf", pdf_de(), "application/pdf")),
                                               ("archivos", ("notas.txt", b"hola\n" * 20, "text/plain"))]))
    assert tipo == "aviso" and msg.startswith("Se agregó 1 de 2 adjuntos. No entraron: notas.txt (tipo de archivo")
    assert [f["ruta"] for f in filas_adjuntos(app)] == ["adjuntos/Slides.pdf"]

    # Más grande que el tope (413 en la API).
    monkeypatch.setattr(adjuntos, "MAX_ADJUNTO", 1000)
    msg, tipo = mensaje(panel.post(url, files={"archivos": ("Grande.pdf", b"%PDF-1.4\n" + b"x" * 2000,
                                                             "application/pdf")}))
    assert tipo == "error" and msg.startswith("No se pudo adjuntar: Grande.pdf (el adjunto pesa más de")
    monkeypatch.undo()

    # Más archivos de los que acepta un envío.
    monkeypatch.setattr(panel_mod, "MAX_ADJUNTOS_FORM", 2)
    msg, tipo = mensaje(panel.post(url, files=[("archivos", (f"{i}.pdf", pdf_de(1), "application/pdf"))
                                               for i in range(3)]))
    assert tipo == "error" and "hasta 2 por vez" in msg
    monkeypatch.undo()

    # Sin archivos (el navegador manda una parte vacía si no se eligió ninguno).
    for archivos in ({"archivos": ("", b"", "application/octet-stream")}, None):
        msg, tipo = mensaje(panel.post(url, files=archivos, data={"x": "1"}))
        assert (msg, tipo) == ("Elegí uno o más archivos para adjuntar.", "error")

    # La clase en error (por ejemplo, un apunte que no compiló) también acepta material: se suma al
    # regenerar.
    with app.state.base.conexion() as conn:
        actualizar_sesion(conn, SID, cfg.zona_horaria, estado="error", error="el apunte no compiló")
    msg, tipo = mensaje(panel.post(url, files=[("archivos", ("a.pdf", pdf_de(1), "application/pdf")),
                                               ("archivos", ("b.pdf", pdf_de(1), "application/pdf"))]))
    assert tipo == "ok" and msg.startswith("2 adjuntos agregados."), msg
    assert [f["ruta"] for f in filas_adjuntos(app)] == ["adjuntos/Slides.pdf", "adjuntos/a.pdf", "adjuntos/b.pdf"]

    # Una sesión que no existe, y desde otra página (otro Origin).
    archivo = {"archivos": ("a.pdf", pdf_de(1), "application/pdf")}
    assert panel.post("/panel/clases/no-existe/adjuntos", files=archivo).status_code == 404
    r = panel.post(url, files=archivo, headers={"Origin": "http://malo.example"})
    assert r.status_code == 403


def test_abrir_y_bajar_un_adjunto(client: TestClient, panel: TestClient):
    app = client.app
    armar_clase(app)
    contenido = pdf_de(1)
    slides = adjuntar(app, contenido=contenido, nombre="Slides clase 5 – Límites.pdf")
    url = f"/panel/clases/{SID}/adjuntos/{slides['nombre']}"
    # Por defecto, inline: el navegador lo abre. Con el tipo que se detectó y sin que adivine otro.
    r = panel.get(url)
    assert r.status_code == 200 and r.content == contenido
    assert r.headers["content-type"] == "application/pdf" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == ("inline; filename*=utf-8''"
                                                "Slides%20clase%205%20%E2%80%93%20L%C3%ADmites.pdf")
    # ?descargar=1: para bajarlo, con el nombre original.
    r = panel.get(url + "?descargar=1")
    assert r.status_code == 200 and r.content == contenido
    assert r.headers["content-disposition"].startswith("attachment; filename*=utf-8''Slides%20clase%205")
    assert panel.get(url + "?descargar=0").headers["content-disposition"].startswith("inline;")
    # Un nombre ASCII simple va tal cual; uno sin extensión la toma del tipo.
    foto = adjuntar(app, contenido=imagen_de(), nombre="pizarra.png")
    r = panel.get(f"/panel/clases/{SID}/adjuntos/{foto['nombre']}?descargar=1")
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"] == 'attachment; filename="pizarra.png"'
    sin_extension = adjuntar(app, nombre="notas del profe")
    r = panel.get(f"/panel/clases/{SID}/adjuntos/{sin_extension['nombre']}?descargar=1")
    assert sin_extension["nombre"] == "notas_del_profe.pdf"
    assert r.headers["content-disposition"] == "attachment; filename*=utf-8''notas%20del%20profe.pdf"
    # Comillas, saltos de línea y carpetas en el nombre original no rompen el encabezado.
    raro = adjuntar(app, nombre='C:\\Users\\x\\Nota "final"\r\nX-Malo: 1.pdf')
    r = panel.get(f"/panel/clases/{SID}/adjuntos/{raro['nombre']}?descargar=1")
    disposicion = r.headers["content-disposition"]
    assert r.status_code == 200 and "x-malo" not in r.headers
    assert disposicion.startswith("attachment; filename*=utf-8''") and not set('"\r\n\\') & set(disposicion)
    assert unquote(disposicion.split("''", 1)[1]) == 'Nota "final"\r\nX-Malo: 1.pdf'
    # Baja siempre con una extensión de su tipo: un .hta que empieza con %PDF- se guardó como PDF y no
    # puede volver como .hta (Windows lo ejecutaría con doble clic). Una extensión del tipo queda como estaba.
    for original, contenido, esperado in (
            ("notas.hta", b"<!-- %PDF-1.4 -->\r\n<script>alert(1)</script>\r\n", "notas.pdf"),
            ("slides.pdf.exe", pdf_de(1), "slides.pdf.pdf"),
            ("captura.jpg", imagen_de("PNG"), "captura.png"),
            ("Foto.JPEG", imagen_de("JPEG"), "Foto.JPEG")):
        adjunto = adjuntar(app, contenido=contenido, nombre=original)
        r = panel.get(f"/panel/clases/{SID}/adjuntos/{adjunto['nombre']}?descargar=1")
        assert r.status_code == 200, original
        assert r.headers["content-disposition"] == f'attachment; filename="{esperado}"', original


def test_adjunto_que_no_es_de_la_sesion_da_404(client: TestClient, panel: TestClient):
    app, cfg = client.app, client.app.state.cfg
    armar_clase(app)
    armar_clase(app, SID2)
    slides = adjuntar(app)
    adjuntar(app, SID2, nombre="De otra clase.pdf")
    assert panel.get(f"/panel/clases/{SID2}/adjuntos/De_otra_clase.pdf").status_code == 200
    for malo in ("De_otra_clase.pdf", "no-existe.pdf", slides["nombre"].upper(), slides["nombre"] + "%00",
                 "..%2F..%2Fconfig.yaml", "..%5C..%5Cconfig.yaml", "..%5Ctranscript.json", "%2E%2E%2Ftranscript.json",
                 "%2E%2E", "adjuntos%2FSlides_clase_5.pdf", "C%3A%5CWindows%5Cwin.ini", "%2Fetc%2Fpasswd",
                 "..%2F..%2F..%2F..%2Fclassmer.db"):
        assert panel.get(f"/panel/clases/{SID}/adjuntos/{malo}").status_code == 404, malo
    assert panel.get(f"/panel/clases/no-existe/adjuntos/{slides['nombre']}").status_code == 404
    # Aunque la base tuviera una ruta que se sale de adjuntos/, no se sirve (se revalida en disco).
    with app.state.base.conexion() as conn:
        conn.execute("INSERT INTO assets (id, sesion_id, tipo, ruta, datos, creado) "
                     "VALUES (?, ?, 'adjunto', ?, '{}', ?)", (str(uuid.uuid4()), SID, "adjuntos/..\\transcript.json", ahora_iso()))
    assert (cfg.sesion_dir(SID) / "transcript.json").is_file()
    assert panel.get(f"/panel/clases/{SID}/adjuntos/..%5Ctranscript.json").status_code == 404
    # Si falta el archivo, 404 (no un 500).
    (cfg.sesion_dir(SID) / "adjuntos" / slides["nombre"]).unlink()
    assert panel.get(f"/panel/clases/{SID}/adjuntos/{slides['nombre']}").status_code == 404


def test_notas_y_marcas_no_muestran_adjuntos(client: TestClient, panel: TestClient):
    app = client.app
    armar_clase(app)
    adjuntar(app, offset_ms=95_000, nombre="Slides.pdf")
    html = panel.get(f"/panel/clases/{SID}").text
    notas = seccion(html, "<h2>Notas y marcas</h2>", "</table>")
    assert notas.count("<tr>") == 2  # el encabezado y la marca
    assert "⭐ Importante" in notas and "adjunto" not in notas.lower() and "Slides" not in notas
    assert "Fotos (1)" in html  # las fotos siguen
    # Si lo único que hay son adjuntos, la sección ni aparece.
    with app.state.base.conexion() as conn:
        conn.execute("DELETE FROM assets WHERE tipo = 'marca'")
    html = panel.get(f"/panel/clases/{SID}").text
    assert "Notas y marcas" not in html and "Adjuntos (1)" in html
    # Ni la API los cuenta como eventos de la grabación.
    assert client.get(f"/api/sesiones/{SID}/eventos").json()["ids"] == [f"f-{SID}"]


def test_lista_de_clases_marca_los_adjuntos(client: TestClient, panel: TestClient):
    app = client.app
    armar_clase(app)
    assert "📎" not in panel.get("/panel/clases").text
    uno, dos = adjuntar(app), adjuntar(app, nombre="Notas.pdf")
    html = panel.get("/panel/clases").text
    assert "📎 2" in html and "(2 sin usar)" in html and 'title="2 adjuntos, 2 sin usar en el apunte vigente"' in html
    usar_en_el_apunte(app, uno["id"], dos["id"])
    html = panel.get("/panel/clases").text
    assert "📎 2" in html and "sin usar" not in html and 'title="2 adjuntos"' in html


def test_borrar_sesion_de_prueba_con_adjuntos(client: TestClient, panel: TestClient):
    app, cfg = client.app, client.app.state.cfg
    carpeta = armar_clase(app)
    r = panel.post(f"/panel/clases/{SID}/adjuntos", files=[
        ("archivos", ("Slides.pdf", pdf_de(), "application/pdf")),
        ("archivos", ("pizarra.png", imagen_de(), "image/png"))])
    assert mensaje(r)[1] == "ok"
    # Publicada con su carpeta adjuntos/ (como la deja publicar) y la carpeta de trabajo del agente.
    publicada = cfg.materia("prueba").ruta_clases() / "2026-10-15 Clase 01 - Gradiente"
    (publicada / "adjuntos").mkdir(parents=True)
    with app.state.base.conexion() as conn:
        lista = adjuntos.listar(conn, SID)
    for a in lista:
        shutil.copyfile(carpeta / a["ruta"], publicada / "adjuntos" / adjuntos.nombre_latex(a))
    (publicada / "apunte.pdf").write_bytes(b"pdf")
    with app.state.base.conexion() as conn:
        actualizar_sesion(conn, SID, cfg.zona_horaria, ruta_publicada=str(publicada))
    trabajo = cfg.jobs_dir / SID / "apunte"
    (trabajo / "input" / "adjuntos").mkdir(parents=True)
    (trabajo / "input" / "adjuntos" / "Slides.pdf").write_bytes(b"%PDF")
    adjuntos.escribir_manifiesto(trabajo, 7, [])
    assert (carpeta / "adjuntos" / "Slides.pdf").is_file() and (carpeta / "adjuntos.jsonl").is_file()

    r = panel.post(f"/panel/clases/{SID}/borrar", data={"confirmar": "si"})
    assert r.status_code == 303 and mensaje(r) == ("Sesión borrada.", "ok")
    assert not carpeta.exists() and not publicada.exists() and not (cfg.jobs_dir / SID).exists()
    assert not filas_adjuntos(app)
    with app.state.base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE sesion_id = ?", (SID,)).fetchone()[0] == 0
    assert panel.get(f"/panel/clases/{SID}/adjuntos/Slides.pdf").status_code == 404
