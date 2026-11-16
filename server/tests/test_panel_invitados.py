"""Invitados de sólo lectura en el panel (§13, §14): entran sólo por el dominio, con Cloudflare Access y sin
la contraseña del panel; GET y HEAD de una lista blanca de rutas, y sólo las clases de sus materias. La
página Invitados (del dueño) escribe `panel.invitados` en config.yaml.

Todo con valores inventados: las claves RSA de prueba y el JWKS falso de test_panel_dominio."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from classmer import panel as panel_mod
from classmer import sesiones
from classmer.panel import acceso as acceso_mod
from classmer.config import Config, ConfigError, config_desde_texto
from classmer.panel import RUTAS_INVITADO
from classmer.panel.cloudflare import AccesoInvalido, VerificadorAccess, identidad
from classmer.util import ahora_iso

from test_panel import adjuntar, armar_clase
from test_panel_acceso import CONTRASENA, de_la_red, entrar, poner
from test_panel_dominio import (AUD, COOKIE_DOMINIO, DOMINIO, EQUIPO, IP_DE_AFUERA, MAIL, URL_DOMINIO, ClavesFalsas,
                                firmar, por_el_dominio)

INVITADO = "fulano@ejemplo.com"
OTRO_INVITADO = "otra@ejemplo.com"
# Clases: dos de sus materias (macro, e historia que está inactiva), una de otra materia, una de prueba y una Auto.
S_MACRO = "0192f7c2-aaaa-7000-8000-00000000aaaa"
S_HISTORIA = "0192f7c2-bbbb-7000-8000-00000000bbbb"
S_OTRA = "0192f7c2-cccc-7000-8000-00000000cccc"
S_PRUEBA = "0192f7c2-dddd-7000-8000-00000000dddd"
S_AUTO = "0192f7c2-eeee-7000-8000-00000000eeee"
DE_OTROS = (S_OTRA, S_PRUEBA, S_AUTO)
NOTA = "Nota secreta de la app"
ERROR_JOB = "el job falló en C:\\Users\\alguien\\Classmer-data"
COMENTARIO = "# los invitados: sólo lectura, sólo por el dominio"


def panel_yaml(invitados: list[dict] | None = None) -> dict:
    datos = {"dominio_publico": DOMINIO, "cloudflare_access": {"equipo": EQUIPO, "aud": AUD, "emails": [MAIL]}}
    if invitados is not None:
        datos["invitados"] = invitados
    return datos


def escribir(home: Path, client: TestClient, panel: dict, materias_extra: bool = True) -> None:
    """config.yaml con las materias de los tests (macro y una inactiva) y la sección panel; la config
    viva la aplica, como en producción."""
    ruta = home / "config.yaml"
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    ids = {m["id"] for m in datos["materias"]}
    if materias_extra and "macro" not in ids:
        datos["materias"] += [{"id": "macro", "nombre": "Macroeconomía", "revision": "apagada"},
                              {"id": "historia", "nombre": "Historia", "activa": False, "revision": "apagada"}]
    datos["panel"] = panel
    ruta.write_text(yaml.safe_dump(datos, allow_unicode=True, sort_keys=False), encoding="utf-8")
    assert client.app.state.config_viva.recargar(forzar=True), client.app.state.config_viva.error


@pytest.fixture(autouse=True)
def scrypt_rapido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acceso_mod, "SCRYPT_N", 2**10)


@pytest.fixture
def claves(client: TestClient) -> ClavesFalsas:
    falsas = ClavesFalsas()
    client.app.state.verificador_access._bajar = falsas
    return falsas


@pytest.fixture
def panel(client: TestClient) -> TestClient:
    """La PC del server (el dueño, sin contraseña)."""
    return TestClient(client.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50123),
                      follow_redirects=False)


@pytest.fixture
def con_invitado(home, cfg: Config, client: TestClient, claves, panel) -> Config:
    escribir(home, client, panel_yaml([{"email": INVITADO, "materias": ["macro", "historia"]}]))
    assert [i.email for i in cfg.panel.invitados] == [INVITADO]
    poner(panel)  # la contraseña del dueño (el invitado no la usa)
    return cfg


def invitado(client: TestClient, email: str = INVITADO, **opciones) -> TestClient:
    return por_el_dominio(client, token=firmar(email=email), **opciones)


def clase_completa(app, sid: str = S_MACRO, materia_id: str | None = "macro") -> Path:
    """Una clase con todo: dos versiones del apunte (con costo), transcripts, audio y audio del celu,
    fotos, un adjunto, una nota de la app, pendientes, y lo de cómo se procesó (jobs, log, error, dónde
    se publicó)."""
    carpeta = armar_clase(app, sid, materia_id=materia_id)
    cfg, base = app.state.cfg, app.state.base
    publicada = str(cfg.home.parent / "materia-prueba" / "Clases-Classmer" / "Clase 1")
    (carpeta / "apunte" / "v1" / "apunte.tex").write_text("\\section{Gradiente v1}\n", encoding="utf-8")
    (carpeta / "apunte" / "v2").mkdir(parents=True)
    (carpeta / "apunte" / "v2" / "apunte.pdf").write_bytes(b"%PDF-1.5 v2")
    (carpeta / "apunte" / "v2" / "apunte.tex").write_text("\\section{Gradiente v2}\n", encoding="utf-8")
    for nombre, texto in (("transcript.md", "# Transcript\n[00:00:01] PROFE: Hoy vemos el gradiente\n"),
                          ("transcript.crudo.md", "# Crudo\n"), ("transcript.celu.md", "# Celu\n")):
        (carpeta / nombre).write_text(texto, encoding="utf-8")
    (carpeta / "revision").mkdir()
    (carpeta / "revision" / "cambios.md").write_text("# Cambios\n", encoding="utf-8")
    with (carpeta / "timeline.md").open("a", encoding="utf-8") as f:
        f.write(f'[00:01:40] NOTA "{NOTA}"\n')
    with base.conexion() as conn:
        conn.execute("UPDATE apuntes SET actual = 0 WHERE sesion_id = ?", (sid,))
        conn.execute("INSERT INTO apuntes (sesion_id, version, actual, motor, modelo, ruta_tex, ruta_pdf, compilo, titulo, "
                     "costo, facturacion, creado) VALUES (?, 2, 1, 'claude-agent', 'claude-opus-5-5', "
                     "'apunte/v2/apunte.tex', 'apunte/v2/apunte.pdf', 1, 'Gradiente', 1.2345, 'api', ?)",
                     (sid, ahora_iso()))
        conn.execute("UPDATE sesiones SET ruta_publicada = ?, error = ? WHERE id = ?",
                     (publicada, "Un error de cuando se procesó", sid))
        conn.execute("INSERT INTO jobs (tipo, sesion_id, estado, error, log, costo, facturacion, creado, actualizado) "
                     "VALUES ('apunte', ?, 'hecho', ?, 'log del job', 0.4321, 'api', ?, ?)",
                     (sid, ERROR_JOB, time.time(), time.time()))
        conn.execute("INSERT INTO publicaciones (sesion_id, ruta, version, compilo, estado, creado, actualizado) "
                     "VALUES (?, ?, 2, 1, 'vigente', ?, ?)", (sid, publicada, ahora_iso(), ahora_iso()))
        conn.execute("INSERT INTO assets (id, sesion_id, tipo, offset_ms, datos, creado) VALUES (?, ?, 'nota', 100000, ?, ?)",
                     (f"n-{sid}", sid, json.dumps({"texto": NOTA}), ahora_iso()))
        sesiones._asegurar_asset_celu(cfg, conn, sid)
        sesiones.guardar_datos_celu(conn, sid, {"cerrada": True, "desfase_ms": 1500, "duracion_ms": 120000,
                                                "origen": "panel", "nombre_original": "grabacion-celu.m4a"})
    (carpeta / sesiones.AUDIO_CELU).write_bytes(b"OggS" + bytes(range(256)) * 10)
    adjuntar(app, sid, nombre="Slides clase 5.pdf", offset_ms=30_000)
    return carpeta


@pytest.fixture
def clases(con_invitado, client: TestClient) -> dict[str, Path]:
    carpetas = {S_MACRO: clase_completa(client.app)}
    carpetas[S_HISTORIA] = armar_clase(client.app, S_HISTORIA, materia_id="historia")
    for sid, materia in ((S_OTRA, "otra"), (S_PRUEBA, "prueba"), (S_AUTO, None)):
        carpetas[sid] = clase_completa(client.app, sid, materia)
    return carpetas


def nombre_adjunto(app, sid: str) -> str:
    with app.state.base.conexion() as conn:
        return conn.execute("SELECT ruta FROM assets WHERE sesion_id = ? AND tipo = 'adjunto'", (sid,)).fetchone()[0] \
            .split("/", 1)[1]


def rutas_de_la_clase(app, sid: str) -> list[str]:
    """Cada ruta de la lista blanca para una clase (y las de toda la lista, sin clase)."""
    base = f"/panel/clases/{sid}"
    return [base, f"{base}/apunte.pdf", f"{base}/apunte.pdf?descargar=1", f"{base}/apunte.tex",
            f"{base}/apunte.tex?descargar=1", f"{base}/apunte/v1.pdf", f"{base}/apunte/v1.tex?descargar=1",
            f"{base}/audio", f"{base}/audio?descargar=1", f"{base}/audio-celu", f"{base}/audio-celu?descargar=1",
            f"{base}/archivo/fotos/f-{sid}.jpg", f"{base}/archivo/fotos/f-{sid}.jpg?descargar=1",
            f"{base}/adjuntos/{nombre_adjunto(app, sid)}", f"{base}/adjuntos/{nombre_adjunto(app, sid)}?descargar=1",
            *(f"{base}/transcript/{cual}" for cual in panel_mod.TRANSCRIPTS),
            *(f"{base}/transcript/{cual}?descargar=1" for cual in panel_mod.TRANSCRIPTS)]


# --- la config ------------------------------------------------------------------------------------

BASE_YAML = ("materias:\n  - {id: macro, nombre: Macro}\n  - {id: historia, nombre: Historia, activa: false}\n"
             f"panel:\n  dominio_publico: {DOMINIO}\n  cloudflare_access:\n    equipo: {EQUIPO}\n    aud: {AUD}\n"
             f"    emails: [{MAIL}]\n")


def test_la_config_de_invitados(home):
    cfg = config_desde_texto(home, BASE_YAML + f"  invitados:\n    - email: {INVITADO}\n      materias: [macro, historia]\n")
    assert [(i.email, i.materias) for i in cfg.panel.invitados] == [(INVITADO, ["macro", "historia"])]
    assert config_desde_texto(home, BASE_YAML + "  invitados:\n").panel.invitados == []
    assert config_desde_texto(home, BASE_YAML + f"  invitados:\n    - email: {INVITADO}\n      materias: []\n") \
        .panel.invitado(INVITADO.upper()).materias == []
    malos = {
        "en minúscula": f"    - email: Fulano@Ejemplo.com\n      materias: [macro]\n",
        "no es un mail": "    - email: fulano\n      materias: [macro]\n",
        "repetido": f"    - email: {INVITADO}\n      materias: [macro]\n    - email: {INVITADO}\n      materias: []\n",
        "dueño e invitado": f"    - email: {MAIL}\n      materias: [macro]\n",
        "no hay materia": f"    - email: {INVITADO}\n      materias: [macro, inventada]\n",
        "materia repetida": f"    - email: {INVITADO}\n      materias: [macro, macro]\n",
        "materias": f"    - email: {INVITADO}\n",                       # sin la lista: no hay "todas"
        "lista de ids": f"    - email: {INVITADO}\n      materias: todas\n",
    }
    for motivo, texto in malos.items():
        with pytest.raises(ConfigError, match=motivo):
            config_desde_texto(home, BASE_YAML + "  invitados:\n" + texto)
    with pytest.raises(ConfigError, match="tiene que ser una lista"):
        config_desde_texto(home, BASE_YAML + "  invitados: fulano@ejemplo.com\n")


def test_el_rol_se_decide_en_un_solo_lugar(home):
    cfg = config_desde_texto(home, BASE_YAML + f"  invitados:\n    - email: {INVITADO}\n      materias: [macro]\n")
    assert identidad(MAIL.upper(), cfg.panel).rol == "dueno"
    quien = identidad(f" {INVITADO.upper()} ", cfg.panel)
    assert quien.es_invitado and quien.email == INVITADO and quien.materias == frozenset({"macro"})
    assert identidad("nadie@ejemplo.com", cfg.panel) is None
    v = VerificadorAccess(bajar=ClavesFalsas())
    assert v.verificar(firmar(email=INVITADO), cfg.panel).es_invitado
    assert not v.verificar(firmar(), cfg.panel).es_invitado
    with pytest.raises(AccesoInvalido, match="ni invitado"):
        v.verificar(firmar(email="nadie@ejemplo.com"), cfg.panel)


# --- lo que puede pedir -----------------------------------------------------------------------------

def test_cada_ruta_de_la_lista_da_200_con_una_clase_de_sus_materias(clases, client):
    nav = invitado(client)
    r = nav.get("/panel")
    assert r.status_code == 303 and r.headers["location"] == "/panel/clases"
    for ruta in ("/panel/clases", "/panel/pendientes", "/panel/pendientes?materia=macro",
                 *rutas_de_la_clase(client.app, S_MACRO), f"/panel/clases/{S_HISTORIA}"):
        assert nav.get(ruta).status_code == 200, ruta
        assert nav.head(ruta.split("?")[0]).status_code == 200, ruta


def test_otra_materia_prueba_y_auto_dan_404(clases, client, panel):
    nav = invitado(client)
    for sid in DE_OTROS:
        for ruta in rutas_de_la_clase(client.app, sid):
            assert nav.get(ruta).status_code == 404, ruta
            assert panel.get(ruta).status_code == 200, ruta  # el dueño sí
    assert nav.get("/panel/clases/no-existe").status_code == 404


def _concreta(ruta: str) -> str:
    valores = {"sesion_id": S_MACRO, "archivo": "v1.pdf", "ruta": f"fotos/f-{S_MACRO}.jpg", "cual": "clase",
               "item_id": "1", "materia_id": "macro", "token": "a" * 32, "nombre": "x.pdf"}
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: valores.get(m.group(1), "x"), ruta)


def test_recorriendo_el_router_lo_que_no_esta_en_la_lista_da_403(clases, client):
    """Todas las rutas del panel, con todos los métodos: para el invitado, sólo GET y HEAD de la lista."""
    nav = invitado(client)
    rutas = [p for p, _ in panel_mod.rutas_del_router(client.app.router) if panel_mod.es_ruta_panel(p)]
    assert len(rutas) > 30 and set(rutas) >= RUTAS_INVITADO  # la lista no nombra rutas que no existen
    for plantilla in rutas:
        url = _concreta(plantilla)
        for metodo in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            respuesta = nav.request(metodo, url, headers={"Origin": URL_DOMINIO})
            if plantilla == "/panel/entrar" and metodo in ("GET", "HEAD"):
                assert respuesta.status_code == 303 and respuesta.headers["location"] == "/panel/clases"
            elif plantilla in RUTAS_INVITADO and metodo in ("GET", "HEAD"):
                assert respuesta.status_code in (200, 303, 404), (metodo, url, respuesta.status_code)
            else:
                assert respuesta.status_code == 403, (metodo, url, respuesta.status_code)
    # Y lo que no es una ruta: también 403 (no 404 ni 405).
    for url in ("/panel/no-existe", f"/panel/clases/{S_MACRO}/borrar", f"/panel/clases/{S_MACRO}/",
                f"/panel/clases/{S_MACRO}/apunte/v1.pdf/x", "/panel/importar/analizar"):
        assert nav.get(url).status_code == 403, url


def test_una_ruta_nueva_no_se_escapa_aunque_tape_a_una_de_la_lista(clases, client, panel):
    """Una ruta nueva que el router atiende antes que /panel/clases/{sesion_id} no es de la lista."""
    nueva = APIRoute("/panel/clases/nueva", lambda: PlainTextResponse("página del dueño"), methods=["GET"])
    client.app.router.routes.insert(0, nueva)
    try:
        assert panel.get("/panel/clases/nueva").text == "página del dueño"
        assert invitado(client).get("/panel/clases/nueva").status_code == 403
    finally:
        client.app.router.routes.remove(nueva)


def test_si_el_middleware_se_equivocara_la_ruta_igual_da_403(clases, client, monkeypatch):
    """La segunda barrera: una dependencia del router del panel mira la ruta que se va a ejecutar."""
    monkeypatch.setattr(panel_mod, "plantilla_que_atiende", lambda _scope: "/panel/clases")
    nav = invitado(client)
    for ruta in ("/panel/materias", "/panel/estado", "/panel/invitados", "/panel/modelos"):
        assert nav.get(ruta).status_code == 403, ruta
    assert nav.get("/panel/clases").status_code == 200


# --- el rol --------------------------------------------------------------------------------------

def test_con_la_cookie_del_dueno_sigue_siendo_invitado(clases, client):
    valor = client.app.state.acceso.emitir()
    nav = invitado(client)
    nav.headers["Cookie"] = f"{COOKIE_DOMINIO}={valor}"
    r = nav.get("/panel/clases")
    assert r.status_code == 200 and f"Sólo lectura · {INVITADO}" in r.text
    for ruta in ("/panel/materias", "/panel/estado", "/panel/invitados", "/panel/importar", "/panel/acceso"):
        assert nav.get(ruta).status_code == 403, ruta
    assert nav.post("/panel/estilo", data={"estilo_global": "x"}, headers={"Origin": URL_DOMINIO}).status_code == 403


def test_entrar_no_lo_hace_dueno(clases, client):
    nav = invitado(client)
    r = nav.get("/panel/entrar?siguiente=/panel/materias")
    assert r.status_code == 303 and r.headers["location"] == "/panel/clases"
    r = nav.post("/panel/entrar", data={"contrasena": CONTRASENA, "siguiente": "/panel/materias"},
                 headers={"Origin": URL_DOMINIO})
    assert r.status_code == 403 and "set-cookie" not in r.headers
    # Sin contraseña ni cookie: entra igual (el invitado no la usa).
    assert nav.get("/panel/clases").status_code == 200 and "set-cookie" not in nav.get("/panel/clases").headers


def test_ni_dueno_ni_invitado_403(clases, client):
    nav = invitado(client, email="nadie@ejemplo.com")
    for ruta in ("/panel/clases", "/panel/entrar", f"/panel/clases/{S_MACRO}"):
        r = nav.get(ruta)
        assert r.status_code == 403 and "no pasó por Cloudflare Access" in r.text, ruta
    assert entrar(nav).status_code == 403


def test_un_mail_en_las_dos_listas_es_un_error_de_config(home, client, con_invitado, cfg):
    with pytest.raises(ConfigError):
        config_desde_texto(home, (home / "config.yaml").read_text(encoding="utf-8").replace(INVITADO, MAIL))
    # Escrito a mano: la config viva sigue con la de antes y lo muestra en Estado.
    ruta = home / "config.yaml"
    ruta.write_text(ruta.read_text(encoding="utf-8").replace(INVITADO, MAIL), encoding="utf-8")
    assert not client.app.state.config_viva.recargar(forzar=True)
    assert "dueño e invitado" in client.app.state.config_viva.error
    assert [i.email for i in cfg.panel.invitados] == [INVITADO]


def test_el_dueno_por_el_dominio_sigue_como_hoy(clases, client):
    nav = por_el_dominio(client)
    assert nav.get("/panel/clases").status_code == 303  # pide la contraseña
    assert entrar(nav).status_code == 303
    r = nav.get(f"/panel/clases/{S_OTRA}")
    assert r.status_code == 200 and "Procesamiento" in r.text and "Sólo lectura" not in r.text
    assert nav.get("/panel/invitados").status_code == 200


# --- la config viva -----------------------------------------------------------------------------------

def test_sacarlo_lo_deja_afuera_en_el_pedido_siguiente(clases, home, client):
    nav = invitado(client)
    assert nav.get("/panel/clases").status_code == 200
    escribir(home, client, panel_yaml([]))
    r = nav.get("/panel/clases")
    assert r.status_code == 403 and "no pasó por Cloudflare Access" in r.text


def test_cambiarle_las_materias_cambia_lo_que_ve(clases, home, client):
    nav = invitado(client)
    assert nav.get(f"/panel/clases/{S_OTRA}").status_code == 404
    escribir(home, client, panel_yaml([{"email": INVITADO, "materias": ["otra"]}]))
    assert nav.get(f"/panel/clases/{S_OTRA}").status_code == 200
    assert nav.get(f"/panel/clases/{S_MACRO}").status_code == 404
    html = nav.get("/panel/clases").text
    assert f"/panel/clases/{S_OTRA}" in html and f"/panel/clases/{S_MACRO}" not in html


# --- lo que ve ----------------------------------------------------------------------------------------

def _clases_en(html: str) -> set[str]:
    return set(re.findall(r'href="/panel/clases/([0-9a-f-]{36})"', html))


def test_clases_y_pendientes_solo_de_sus_materias(clases, client, panel):
    nav = invitado(client)
    assert _clases_en(panel.get("/panel/clases").text) == set(clases)
    for filtro in ("", "?materia=otra", "?materia=prueba", "?materia=auto", "?materia=macro", "?estado=lista"):
        html = nav.get(f"/panel/clases{filtro}").text
        esperadas = {"": {S_MACRO, S_HISTORIA}, "?materia=macro": {S_MACRO}, "?estado=lista": {S_MACRO, S_HISTORIA}}
        assert _clases_en(html) == esperadas.get(filtro, set()), filtro
    html = nav.get("/panel/clases").text
    # El filtro de materias, sólo con las suyas; sin Importar, sin Auto y sin recargarse sola.
    assert re.findall(r'<option value="([^"]*)"', html.split('name="estado"')[0]) == ["", "macro", "historia"]
    assert "/panel/importar" not in html and 'http-equiv="refresh"' not in html
    assert "Un error de cuando se procesó" in panel.get("/panel/clases").text
    assert "Un error de cuando se procesó" not in html
    assert "sin usar" not in html and "publicar" not in html and "Etapa" not in html  # cómo se procesó
    # Pendientes: el resumen y cada materia, sólo las suyas y sin botones para marcar.
    html = nav.get("/panel/pendientes").text
    assert "Macroeconomía" in html and "Otra Materia" not in html and "Materia de Prueba" not in html
    for materia in ("otra", "prueba", "no-existe"):
        assert nav.get(f"/panel/pendientes?materia={materia}").status_code == 404, materia
    html = nav.get("/panel/pendientes?materia=macro&ver=todos").text
    assert "Entra el gradiente" in html and "<form" not in html and "editar materia" not in html
    assert "/panel/items/" not in html
    assert "<form" in panel.get("/panel/pendientes?materia=macro").text


def test_la_clase_del_invitado_sin_formularios_log_costos_ni_rutas_de_la_pc(clases, client, panel, cfg, tmp_path):
    nav = invitado(client)
    html = nav.get(f"/panel/clases/{S_MACRO}").text
    duenio = panel.get(f"/panel/clases/{S_MACRO}").text
    # Lo que ve: el apunte (y la versión anterior), transcripts, audios, fotos, adjuntos, pendientes, timeline.
    for esperado in (f"/panel/clases/{S_MACRO}/apunte.pdf?v=", f"/panel/clases/{S_MACRO}/apunte.tex",
                     f"/panel/clases/{S_MACRO}/apunte/v1.pdf", f"/panel/clases/{S_MACRO}/audio",
                     f"/panel/clases/{S_MACRO}/audio-celu", f"/panel/clases/{S_MACRO}/archivo/fotos/f-{S_MACRO}.jpg",
                     f"/panel/clases/{S_MACRO}/adjuntos/", "Slides clase 5.pdf", "Entra el gradiente",
                     "Hoy vemos el gradiente", "00:01:01", "Cambios de la revisión",
                     "Crudo del celu", f"Sólo lectura · {INVITADO}", 'href="/cdn-cgi/access/logout"'):
        assert esperado in html, esperado
    # Lo que no ve, y el dueño sí.
    prohibidos = ("<form", "pipeline.log", "publicar: copiado", "Procesamiento", "US$", "facturación", "claude-opus",
                  "Reprocesar", "Borrar", "Adjuntar", "Agregar audio del celu", "Un error de cuando se procesó",
                  ERROR_JOB, "log del job", "Publicada en", "Publicación", "dispositivo", "grabacion-celu.m4a",
                  "medido por correlación", NOTA, "⭐ Importante", "MARCA", "Notas y marcas", "/panel/items/",
                  "lo usó el apunte", "/panel/materias", "/panel/estado", 'action="/panel/salir"')
    for texto in prohibidos:
        assert texto not in html, texto
    for texto in ("<form", "pipeline.log", "US$", "Publicada en", NOTA, "⭐ Importante", "Reprocesar"):
        assert texto in duenio, texto
    # Ninguna ruta de la PC: ni la carpeta de datos, ni la de las materias, ni nada con unidad de Windows.
    for ruta in (str(tmp_path), tmp_path.as_posix(), str(cfg.home), "Classmer-data", "Clases-Classmer"):
        assert ruta not in html, ruta
    assert re.search(r"\b[A-Za-z]:\\", html) is None
    assert str(tmp_path) in duenio  # (el dueño ve dónde se publicó)


def test_el_audio_con_head_y_con_range(clases, client):
    nav = invitado(client)
    for ruta in ("audio", "audio-celu"):
        url = f"/panel/clases/{S_MACRO}/{ruta}"
        r = nav.head(url)
        assert r.status_code == 200 and r.headers["accept-ranges"] == "bytes" and int(r.headers["content-length"]) > 0
        r = nav.get(url, headers={"Range": "bytes=4-9"})
        assert r.status_code == 206 and r.content == bytes(range(6)) and r.headers["content-type"].startswith("audio/ogg")
    assert nav.head(f"/panel/clases/{S_OTRA}/audio").status_code == 404


def test_las_notas_se_prenden_con_una_constante(clases, client, monkeypatch):
    monkeypatch.setattr(panel_mod, "INVITADO_VE_NOTAS", True)
    html = invitado(client).get(f"/panel/clases/{S_MACRO}").text
    assert NOTA in html and "⭐ Importante" in html and "Notas y marcas" in html


# --- descargar --------------------------------------------------------------------------------------

def _descarga(r) -> str:
    """El nombre con que se baja (attachment), del Content-Disposition."""
    tipo, _, resto = r.headers["content-disposition"].partition(";")
    assert tipo == "attachment", r.headers["content-disposition"]
    resto = resto.strip()
    return unquote(resto.removeprefix("filename*=utf-8''")) if resto.startswith("filename*=") \
        else resto.removeprefix("filename=").strip('"')


@pytest.mark.parametrize("quien", ["invitado", "dueno"])
def test_todo_se_descarga_con_un_nombre_legible(clases, client, panel, quien):
    nav = invitado(client) if quien == "invitado" else panel
    base = f"/panel/clases/{S_MACRO}"
    html = nav.get(base).text
    prefijo = "Macroeconomía 2026-10-15 - "
    esperados = {
        "apunte.pdf": "apunte.pdf", "apunte.tex": "apunte.tex", "apunte/v1.pdf": "apunte v1.pdf",
        "apunte/v1.tex": "apunte v1.tex", "audio": "audio.ogg", "audio-celu": f"audio del celu{Path(sesiones.AUDIO_CELU).suffix}",
        f"archivo/fotos/f-{S_MACRO}.jpg": "foto 00h01m01s.jpg", "transcript/clase": "transcript.md",
        "transcript/crudo": "transcript crudo.md", "transcript/celu": "transcript crudo del celu.md",
        "transcript/cambios": "cambios de la revisión.md",
    }
    for ruta, nombre in esperados.items():
        r = nav.get(f"{base}/{ruta}?descargar=1")
        assert r.status_code == 200 and _descarga(r) == prefijo + nombre, ruta
        assert f'{base}/{ruta}?descargar=1' in html or f"{base}/{ruta}?v=" in html, ruta  # el enlace está
    assert re.search(rf'href="{base}/apunte\.pdf\?v=\w+&amp;descargar=1"', html)
    # El adjunto, con su nombre original (como antes).
    r = nav.get(f"{base}/adjuntos/{nombre_adjunto(client.app, S_MACRO)}?descargar=1")
    assert _descarga(r) == "Slides clase 5.pdf"
    # Sin ?descargar, se abre como siempre.
    assert "content-disposition" not in nav.get(f"{base}/transcript/clase").headers
    assert nav.get(f"{base}/apunte.pdf").headers["content-disposition"].startswith("inline")


# --- el log -------------------------------------------------------------------------------------------

def test_el_log_anota_las_paginas_que_abre(caplog: pytest.LogCaptureFixture, clases, client):
    caplog.set_level("INFO", logger="classmer.panel")
    nav = invitado(client)
    nav.get(f"/panel/clases/{S_MACRO}", headers={"Sec-Fetch-Dest": "document"})
    nav.get(f"/panel/clases/{S_OTRA}")
    for _ in range(5):  # el reproductor, las fotos de la página y el PDF del iframe: no
        nav.get(f"/panel/clases/{S_MACRO}/audio", headers={"Range": "bytes=0-9"})
    nav.get(f"/panel/clases/{S_MACRO}/archivo/fotos/f-{S_MACRO}.jpg", headers={"Sec-Fetch-Dest": "image"})
    nav.get(f"/panel/clases/{S_MACRO}/apunte.pdf", headers={"Sec-Fetch-Dest": "iframe"})
    nav.get(f"/panel/clases/{S_MACRO}/apunte.pdf?descargar=1", headers={"Sec-Fetch-Dest": "document"})
    nav.post("/panel/estilo", data={"estilo_global": "x"}, headers={"Origin": URL_DOMINIO})
    lineas = [r.getMessage() for r in caplog.records if "el invitado" in r.getMessage()]
    abrio = [l for l in lineas if "abrió" in l]
    assert abrio == [f"panel: el invitado {INVITADO} abrió /panel/clases/{S_MACRO} ({IP_DE_AFUERA})",
                     f"panel: el invitado {INVITADO} abrió /panel/clases/{S_OTRA} ({IP_DE_AFUERA}) → 404",
                     f"panel: el invitado {INVITADO} abrió /panel/clases/{S_MACRO}/apunte.pdf?descargar=1 ({IP_DE_AFUERA})"]
    assert any("pidió POST /panel/estilo" in l and IP_DE_AFUERA in l for l in lineas)
    # Los rechazos de Access se loguean como siempre.
    invitado(client, email="nadie@ejemplo.com").get("/panel/clases")
    assert any("no pasó por Cloudflare Access" in r.getMessage() for r in caplog.records)


# --- la página Invitados ------------------------------------------------------------------------------

CONFIG_CON_COMENTARIOS = """# Config de prueba (los comentarios tienen que quedar)
zona_horaria: America/Argentina/Buenos_Aires   # la zona
worker:
  poll_s: 0.05

# --- el panel por internet ---
panel:
  dominio_publico: {dominio}
  cloudflare_access:
    equipo: {equipo}
    aud: {aud}
    emails: [{mail}]
  {comentario}
  invitados:
    - email: {invitado}   # el primero
      materias: [macro]

# --- materias ---
materias:
  - id: prueba
    nombre: Materia de Prueba
    carpeta: '{carpeta}'
    revision: apagada
  - id: macro
    nombre: Macroeconomía     # la de un amigo
    revision: apagada
  - id: historia
    nombre: Historia
    activa: false
    revision: apagada
"""


@pytest.fixture
def con_comentarios(home, client, cfg, claves, materia_dir) -> Path:
    ruta = home / "config.yaml"
    ruta.write_text(CONFIG_CON_COMENTARIOS.format(dominio=DOMINIO, equipo=EQUIPO, aud=AUD, mail=MAIL, invitado=INVITADO,
                                                  comentario=COMENTARIO, carpeta=materia_dir), encoding="utf-8")
    assert client.app.state.config_viva.recargar(forzar=True), client.app.state.config_viva.error
    return ruta


def mensaje(r) -> str:
    return parse_qs(urlsplit(r.headers["location"]).query).get("msg", [""])[0]


def test_la_pagina_invitados_guarda_sin_tocar_el_resto(con_comentarios, client, panel, cfg):
    antes = con_comentarios.read_text(encoding="utf-8")
    html = panel.get("/panel/invitados").text
    assert INVITADO in html and "policy de Cloudflare Access" in html and "Panel Classmer" in html
    assert "no entró desde el último reinicio" in html
    # Las activas primero; la inactiva, marcada.
    assert html.index('value="prueba"') < html.index('value="macro"') < html.index('value="historia"')
    assert '<span class="etiqueta">inactiva</span>' in html
    # Agregar.
    r = panel.post("/panel/invitados/agregar", data={"email": f" {OTRO_INVITADO.upper()} ", "materias": ["historia", "macro"]})
    assert r.status_code == 303 and "agregado" in mensaje(r), mensaje(r)
    texto = con_comentarios.read_text(encoding="utf-8")
    assert f"    - email: {OTRO_INVITADO}\n      materias: [macro, historia]\n" in texto
    assert [(i.email, i.materias) for i in cfg.panel.invitados] == [(INVITADO, ["macro"]),
                                                                       (OTRO_INVITADO, ["macro", "historia"])]
    # Cambiarle las materias a uno.
    r = panel.post("/panel/invitados/guardar", data={"email": INVITADO, "materias": ["macro", "historia"]})
    assert r.status_code == 303 and "Guardado" in mensaje(r)
    assert cfg.panel.invitado(INVITADO).materias == ["macro", "historia"]
    # Sacar el otro: el archivo queda como estaba, salvo las materias del primero.
    r = panel.post("/panel/invitados/sacar", data={"email": OTRO_INVITADO})
    assert r.status_code == 303 and "ya no entra" in mensaje(r)
    assert con_comentarios.read_text(encoding="utf-8") == antes.replace(
        "materias: [macro]\n\n# --- materias", "materias: [macro, historia]\n\n# --- materias")
    assert panel.post("/panel/invitados/sacar", data={"email": OTRO_INVITADO}).status_code == 303
    # Sacar al último: queda `invitados: []` y los comentarios siguen.
    panel.post("/panel/invitados/sacar", data={"email": INVITADO})
    texto = con_comentarios.read_text(encoding="utf-8")
    assert "  invitados: []\n\n# --- materias ---\n" in texto and COMENTARIO in texto and "# la de un amigo" in texto
    assert cfg.panel.invitados == []


@pytest.mark.parametrize("email, materias, motivo", [
    ("no-es-un-mail", ["macro"], "no es un mail"),
    ("dos@arrobas@ejemplo.com", ["macro"], "no es un mail"),
    (MAIL, ["macro"], "es un dueño"),
    (INVITADO, ["macro"], "ya es invitado"),
    ("nuevo@ejemplo.com", ["inventada"], "no hay materia"),
])
def test_la_pagina_invitados_valida(con_comentarios, panel, email, materias, motivo):
    antes = con_comentarios.read_text(encoding="utf-8")
    r = panel.post("/panel/invitados/agregar", data={"email": email, "materias": materias})
    assert r.status_code == 303 and motivo in mensaje(r) and parse_qs(urlsplit(r.headers["location"]).query)["tipo"] == ["error"]
    assert con_comentarios.read_text(encoding="utf-8") == antes
    r = panel.post("/panel/invitados/guardar", data={"email": "no-esta@ejemplo.com", "materias": ["macro"]})
    assert "ya no es invitado" in mensaje(r)
    assert con_comentarios.read_text(encoding="utf-8") == antes


def test_la_pagina_invitados_no_es_del_invitado_y_respeta_el_mismo_origen(con_comentarios, client, panel):
    poner(panel)
    antes = con_comentarios.read_text(encoding="utf-8")
    nav = invitado(client)
    assert nav.get("/panel/invitados").status_code == 403
    for accion in ("agregar", "guardar", "sacar"):
        r = nav.post(f"/panel/invitados/{accion}", data={"email": INVITADO, "materias": ["prueba"]},
                     headers={"Origin": URL_DOMINIO})
        assert r.status_code == 403, accion
        r = panel.post(f"/panel/invitados/{accion}", data={"email": "x@ejemplo.com", "materias": ["macro"]},
                       headers={"Origin": "https://malo.example"})
        assert r.status_code == 403 and "no viene del panel" in r.text, accion
    assert con_comentarios.read_text(encoding="utf-8") == antes
    # El dueño, desde la LAN (con la contraseña) y por el dominio (con Access y la contraseña).
    red = de_la_red(client)
    assert red.get("/panel/invitados").status_code == 303  # a entrar
    entrar(red)
    assert red.get("/panel/invitados").status_code == 200
    duenio = por_el_dominio(client)
    entrar(duenio)
    r = duenio.post("/panel/invitados/guardar", data={"email": INVITADO, "materias": ["prueba", "macro"]},
                    headers={"Origin": URL_DOMINIO})
    assert r.status_code == 303 and "Guardado" in mensaje(r)
    # Y en la barra del dueño está Invitados; en la del invitado, no.
    assert 'href="/panel/invitados"' in panel.get("/panel/clases").text
    assert 'href="/panel/invitados"' not in nav.get("/panel/clases").text


def test_la_ultima_vez_que_entro(con_comentarios, client, panel):
    invitado(client, ip_real="198.51.100.4").get("/panel/clases")
    html = panel.get("/panel/invitados").text
    assert "entró por última vez" in html and "198.51.100.4" in html


# --- lo que no cambia ----------------------------------------------------------------------------------

def test_la_api_y_el_mcp_no_se_abren_con_el_jwt_del_invitado(clases, client, panel):
    nav = invitado(client)
    assert nav.get("/api/materias").status_code == 401
    assert nav.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    # La PC y la LAN, como siempre (el invitado sólo existe por el dominio).
    assert panel.get(f"/panel/clases/{S_OTRA}").status_code == 200
    red = de_la_red(client)
    assert red.get("/panel/clases").status_code == 303
    entrar(red)
    assert red.get(f"/panel/clases/{S_PRUEBA}").status_code == 200
    # Un JWT de invitado por la LAN (sin el Host del dominio) no vale: 403, como cualquier encabezado de Cloudflare.
    assert invitado(client, ip_socket="192.168.1.20", base_url="http://192.168.1.10:8000").get(
        "/panel/clases").status_code == 403
