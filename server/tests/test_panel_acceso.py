"""Panel desde la red (§13, §14): contraseña puesta desde la PC, cookie firmada de 90 días, tope de
intentos y chequeos de Host y Origin. La PC del server (loopback) sigue entrando sin nada."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from classmer import panel as panel_mod
from classmer.panel import acceso as acceso_mod
from classmer.panel.acceso import Acceso, Intentos, host_de_red_valido, siguiente_seguro

from test_panel import SID, armar_clase

LOCAL = "http://127.0.0.1:8000"
RED = "http://192.168.1.10:8000"
IP_OTRA_PC = "192.168.1.20"
CONTRASENA = "una-clave-larga"
COOKIE = "classmer_panel_8000"


@pytest.fixture(autouse=True)
def scrypt_rapido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los parámetros quedan en el archivo: con N chico los tests no tardan y verificar usa los mismos."""
    monkeypatch.setattr(acceso_mod, "SCRYPT_N", 2**10)


@pytest.fixture
def panel(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url=LOCAL, client=("127.0.0.1", 50123), follow_redirects=False)


def de_la_red(client: TestClient, ip: str = IP_OTRA_PC, base_url: str = RED) -> TestClient:
    return TestClient(client.app, base_url=base_url, client=(ip, 40000), follow_redirects=False)


@pytest.fixture
def red(client: TestClient) -> TestClient:
    return de_la_red(client)


def poner(panel: TestClient, contrasena: str = CONTRASENA, repetida: str | None = None):
    return panel.post("/panel/acceso/contrasena",
                      data={"contrasena": contrasena, "repetida": contrasena if repetida is None else repetida})


def entrar(red: TestClient, contrasena: str = CONTRASENA, siguiente: str = "/panel/clases"):
    return red.post("/panel/entrar", data={"contrasena": contrasena, "siguiente": siguiente})


def galleta(valor: str | None, nombre: str = COOKIE) -> dict[str, str]:
    """La cookie a mano, en el encabezado (sin pasar por el frasco del cliente)."""
    return {"Cookie": f"{nombre}={valor}"}


def mensaje(r) -> str:
    return parse_qs(urlsplit(r.headers["location"]).query).get("msg", [""])[0]


# --- sin contraseña ----------------------------------------------------------------------------

def test_red_sin_contrasena_403(red: TestClient):
    for ruta in ("/panel", "/panel/clases", "/panel/entrar", "/"):
        r = red.get(ruta)
        assert r.status_code == 403, ruta
        assert "todavía no hay una" in r.text and "http://127.0.0.1:8000/panel/acceso" in r.text
    assert entrar(red).status_code == 403
    assert red.post("/panel/estilo", data={"estilo_global": "x"}).status_code == 403


def test_la_pc_entra_sin_nada_y_ve_acceso(panel: TestClient):
    r = panel.get("/panel/clases")
    assert r.status_code == 200
    assert 'href="/panel/acceso"' in r.text and "/panel/salir" not in r.text
    r = panel.get("/panel/acceso")
    assert r.status_code == 200 and "cerrado" in r.text and "Poner una contraseña" in r.text
    # /panel/entrar y /panel/salir desde la PC no hacen nada: vuelven al panel.
    assert panel.get("/panel/entrar?siguiente=/panel/estado").headers["location"] == "/panel/estado"
    assert panel.post("/panel/salir").headers["location"] == "/panel/clases"


# --- poner, cambiar y quitar la contraseña (desde la PC) ----------------------------------------

def test_poner_contrasena_valida_y_guarda_solo_el_hash(home, panel: TestClient):
    r = poner(panel, "corta")
    assert r.status_code == 303 and "al menos 8" in mensaje(r)
    r = poner(panel, CONTRASENA, "otra-distinta")
    assert "no coinciden" in mensaje(r)
    assert not (home / "panel-acceso.json").exists()

    r = poner(panel)
    assert r.status_code == 303 and "ya se abre desde las otras PCs" in mensaje(r)
    texto = (home / "panel-acceso.json").read_text(encoding="utf-8")
    assert CONTRASENA not in texto
    datos = json.loads(texto)
    assert datos["hash"]["algoritmo"] == "scrypt" and len(bytes.fromhex(datos["hash"]["sal"])) == 16
    assert len(bytes.fromhex(datos["secreto"])) == 32
    r = panel.get("/panel/acceso")
    assert "abierto" in r.text and "Cambiar la contraseña" in r.text and "Cerrar el acceso desde la red" in r.text
    assert CONTRASENA not in r.text


def test_la_contrasena_no_va_al_log(caplog: pytest.LogCaptureFixture, panel: TestClient, red: TestClient):
    caplog.set_level("DEBUG")
    poner(panel)
    entrar(red, "mal-mal-mal")
    entrar(red)
    assert caplog.records and not any(CONTRASENA in r.getMessage() or "mal-mal-mal" in r.getMessage()
                                      for r in caplog.records)


def test_la_url_para_las_otras_pcs_sale_de_las_ips(monkeypatch: pytest.MonkeyPatch, panel: TestClient):
    monkeypatch.setattr(panel_mod, "ips_de_la_pc", lambda: ["192.168.1.10", "10.8.0.2"])
    poner(panel)
    r = panel.get("/panel/acceso")
    assert '<a href="http://192.168.1.10:8000/panel">' in r.text
    assert "<code>http://10.8.0.2:8000/panel</code>" in r.text


def test_ips_de_la_pc_lan_primero(monkeypatch: pytest.MonkeyPatch):
    def getaddrinfo(*_a, **_k):
        return [(2, 1, 0, "", (ip, 0)) for ip in ("10.8.0.2", "127.0.0.1", "169.254.3.4", "192.168.1.10",
                                                    "8.8.8.8", "172.20.1.1")]

    class SinRuta:
        def __init__(self, *_a):
            raise OSError("sin red")

    monkeypatch.setattr(acceso_mod.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(acceso_mod.socket, "socket", SinRuta)
    assert acceso_mod.ips_de_la_pc() == ["192.168.1.10", "172.20.1.1", "10.8.0.2"]


# --- entrar desde la red ------------------------------------------------------------------------

def test_con_contrasena_y_sin_cookie_va_a_entrar(panel: TestClient, red: TestClient):
    poner(panel)
    r = red.get("/panel/clases?materia=prueba")
    assert r.status_code == 303
    destino = urlsplit(r.headers["location"])
    assert destino.path == "/panel/entrar"
    assert parse_qs(destino.query)["siguiente"] == ["/panel/clases?materia=prueba"]
    r = red.get(r.headers["location"])
    assert r.status_code == 200 and 'name="contrasena"' in r.text
    assert 'value="/panel/clases?materia=prueba"' in r.text and "/panel/salir" not in r.text
    # Los métodos que no son seguros, sin sesión: 403 (no se redirige un POST).
    r = red.post("/panel/estilo", data={"estilo_global": "x"})
    assert r.status_code == 403 and "entrá de nuevo" in r.text


def test_contrasena_mal_no_entra_y_cuenta(client: TestClient, panel: TestClient, red: TestClient):
    poner(panel)
    r = entrar(red, "no-es-esta-clave")
    assert r.status_code == 401 and "Contraseña incorrecta" in r.text
    assert COOKIE not in r.headers.get("set-cookie", "")
    assert client.app.state.acceso.intentos.fallidos() == 1
    assert red.get("/panel/clases").status_code == 303


def test_contrasena_bien_da_cookie_y_entra(client: TestClient, panel: TestClient, red: TestClient):
    poner(panel)
    entrar(red, "no-es-esta-clave")
    r = entrar(red, siguiente="/panel/estado")
    assert r.status_code == 303 and r.headers["location"] == "/panel/estado"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    atributos = {a.strip().split("=")[0].lower(): a for a in cookie.split(";")}
    assert "httponly" in atributos and "secure" not in atributos
    assert atributos["samesite"].strip().lower() == "samesite=lax"
    assert atributos["max-age"].strip().lower() == f"max-age={90 * 24 * 3600}"
    assert atributos["path"].strip() == "Path=/"
    # Entrar bien olvida los fallidos de esa IP.
    assert client.app.state.acceso.intentos.fallidos() == 0
    r = red.get("/panel/estado")
    assert r.status_code == 200
    # Desde la red: Salir sí, Acceso no.
    assert 'action="/panel/salir"' in r.text and 'href="/panel/acceso"' not in r.text
    # Con sesión, /panel/entrar vuelve al panel.
    assert red.get("/panel/entrar").headers["location"] == "/panel/clases"


def test_el_panel_anda_con_la_sesion(client: TestClient, panel: TestClient, red: TestClient):
    """Una clase con audio, el PDF (con Range en el audio) y un POST con el Origin del panel."""
    armar_clase(client.app)
    poner(panel)
    entrar(red)
    r = red.get(f"/panel/clases/{SID}")
    assert r.status_code == 200 and "Gradiente y curvas de nivel" in r.text
    r = red.get(f"/panel/clases/{SID}/audio", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == b"OggS" + bytes(range(6))
    r = red.get(f"/panel/clases/{SID}/apunte.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    r = red.post("/panel/estilo", data={"estilo_global": "Desde la red."}, headers={"Origin": RED})
    assert r.status_code == 303 and "Estilo guardado" in mensaje(r)
    assert client.app.state.cfg.estilo_global.strip() == "Desde la red."


def test_cookie_vencida_o_adulterada_va_a_entrar(client: TestClient, panel: TestClient, red: TestClient):
    poner(panel)
    acceso: Acceso = client.app.state.acceso
    vieja = acceso.emitir(ahora=time.time() - 91 * 24 * 3600)
    casi = acceso.emitir(ahora=time.time() - 89 * 24 * 3600)
    assert acceso.sesion_valida(casi) and not acceso.sesion_valida(vieja)
    cuerpo, _, firma = acceso.emitir().rpartition(".")
    version, emitida, nonce = cuerpo.split(".")
    adulteradas = [
        vieja,
        f"{version}.{int(emitida) + 10 * 24 * 3600}.{nonce}.{firma}",   # le estiraron la fecha
        f"{cuerpo}.{'A' * len(firma)}",                                # otra firma
        f"{cuerpo}.",                                                   # sin firma
        "basura",
    ]
    for valor in adulteradas:
        r = red.get("/panel/clases", headers=galleta(valor))
        assert r.status_code == 303 and r.headers["location"].startswith("/panel/entrar"), valor
    assert red.get("/panel/clases", headers=galleta(casi)).status_code == 200
    # La cookie es de este puerto: la de otro server (otro puerto) no abre este.
    assert red.get("/panel/clases", headers=galleta(casi, "classmer_panel_8777")).status_code == 303


def test_cookie_no_ascii_va_a_entrar(client: TestClient, panel: TestClient, red: TestClient):
    """Una cookie inventada con bytes latin-1 (p. ej. "²", que isdigit() acepta) va a Entrar, no da 500."""
    poner(panel)
    acceso: Acceso = client.app.state.acceso
    assert not acceso.sesion_valida("v1.²².abc.def") and not acceso.sesion_valida("v1.123.nñ.def")
    for crudo in ("v1.²².abc.def".encode("latin-1"), "v1.123.nñ.def".encode("latin-1")):
        r = red.get("/panel/clases", headers={"Cookie": (COOKIE + "=").encode("ascii") + crudo})
        assert r.status_code == 303 and r.headers["location"].startswith("/panel/entrar"), crudo


def test_archivo_con_n_invalido_cierra_la_red(home, panel: TestClient, red: TestClient):
    """Un n que no es potencia de 2 (archivo tocado a mano) haría tirar a scrypt: el archivo cuenta como dañado."""
    poner(panel)
    ruta = home / "panel-acceso.json"
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    datos["hash"]["n"] = 1000
    ruta.write_text(json.dumps(datos), encoding="utf-8")
    assert red.get("/panel/clases").status_code == 403
    assert entrar(red).status_code == 403
    assert "dañado" in panel.get("/panel/acceso").text


def test_tope_de_intentos_429(client: TestClient, panel: TestClient, red: TestClient):
    poner(panel)
    for _ in range(5):
        assert entrar(red, "no-es-esta-clave").status_code == 401
    r = entrar(red)  # ni con la buena: ya se pasó el tope
    assert r.status_code == 429 and "Demasiados intentos fallidos" in r.text
    assert int(r.headers["retry-after"]) > 0 and COOKIE not in r.headers.get("set-cookie", "")
    # Otra PC todavía puede.
    otra = de_la_red(client, "192.168.1.51")
    assert entrar(otra).status_code == 303


def test_tope_global(client: TestClient, panel: TestClient):
    poner(panel)
    client.app.state.acceso.intentos = Intentos(tope_ip=5, tope_global=3)
    for i in range(3):
        assert entrar(de_la_red(client, f"192.168.1.{60 + i}"), "no-es-esta-clave").status_code == 401
    assert entrar(de_la_red(client, "192.168.1.70")).status_code == 429
    # La PC sigue entrando sin nada.
    assert panel.get("/panel/clases").status_code == 200


def test_intentos_vencen_con_la_ventana():
    intentos = Intentos(tope_ip=2, tope_global=10, ventana_s=600)
    assert intentos.anotar("a", 0) and intentos.anotar("a", 1)
    assert not intentos.anotar("a", 2) and intentos.espera("a", 2) == pytest.approx(598)
    assert intentos.anotar("b", 2)
    assert intentos.anotar("a", 601)  # el primero ya salió de la ventana
    intentos.bien("a")
    assert intentos.fallidos(601) == 1


# --- /panel/acceso y lo que se chequea desde la red ------------------------------------------------

def test_acceso_desde_la_red_403_tambien_con_cookie(panel: TestClient, red: TestClient):
    poner(panel)
    rutas_post = ("/panel/acceso/contrasena", "/panel/acceso/cerrar-sesiones", "/panel/acceso/cerrar")
    assert red.get("/panel/acceso").status_code == 403
    entrar(red)
    assert red.get("/panel/clases").status_code == 200
    r = red.get("/panel/acceso")
    assert r.status_code == 403 and "sólo en la PC del server" in r.text
    for ruta in rutas_post:
        r = red.post(ruta, data={"contrasena": "otra-clave-x", "repetida": "otra-clave-x"}, headers={"Origin": RED})
        assert r.status_code == 403, ruta
    assert red.get("/panel/clases").status_code == 200  # nada cambió


def test_cambiar_contrasena_o_cerrar_sesiones_invalida_cookies(panel: TestClient, red: TestClient):
    poner(panel)
    entrar(red)
    assert red.get("/panel/clases").status_code == 200
    r = poner(panel, "otra-clave-nueva")
    assert "las otras PCs tienen que entrar de nuevo" in mensaje(r)
    assert red.get("/panel/clases").status_code == 303
    assert entrar(red).status_code == 401  # la vieja ya no
    assert entrar(red, "otra-clave-nueva").status_code == 303
    assert red.get("/panel/clases").status_code == 200

    r = panel.post("/panel/acceso/cerrar-sesiones")
    assert "Sesiones cerradas" in mensaje(r)
    assert red.get("/panel/clases").status_code == 303
    assert entrar(red, "otra-clave-nueva").status_code == 303  # la contraseña sigue
    assert red.get("/panel/clases").status_code == 200


def test_cerrar_el_acceso_quita_la_contrasena(home, panel: TestClient, red: TestClient):
    poner(panel)
    entrar(red)
    r = panel.post("/panel/acceso/cerrar")
    assert "vuelve a abrirse sólo en esta PC" in mensaje(r)
    assert not (home / "panel-acceso.json").exists()
    r = red.get("/panel/clases")
    assert r.status_code == 403 and "todavía no hay una" in r.text
    assert "ya estaba cerrado" in mensaje(panel.post("/panel/acceso/cerrar"))
    assert "no hay sesiones" in mensaje(panel.post("/panel/acceso/cerrar-sesiones"))


def test_archivo_danado_cierra_la_red(home, panel: TestClient, red: TestClient):
    poner(panel)
    (home / "panel-acceso.json").write_text("{no es json", encoding="utf-8")
    assert red.get("/panel/clases").status_code == 403
    r = panel.get("/panel/acceso")
    assert "dañado" in r.text
    poner(panel)  # se arregla poniendo la contraseña de nuevo
    assert entrar(red).status_code == 303


def test_salir(panel: TestClient, red: TestClient):
    poner(panel)
    entrar(red)
    r = red.post("/panel/salir", headers={"Origin": RED})
    assert r.status_code == 303 and urlsplit(r.headers["location"]).path == "/panel/entrar"
    assert f'{COOKIE}=""' in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    assert red.get("/panel/clases").status_code == 303


def test_host_de_dominio_publico_desde_la_red_403(client: TestClient, panel: TestClient):
    poner(panel)
    malo = de_la_red(client, base_url="http://malo.example:8000")
    assert malo.get("/panel/clases").status_code == 403
    assert entrar(malo).status_code == 403
    # Con una cookie válida, tampoco (DNS rebinding contra un navegador ya logueado).
    valor = client.app.state.acceso.emitir()
    assert malo.get("/panel/clases", headers=galleta(valor)).status_code == 403
    # Una IP o un nombre de la LAN, sí.
    for base in ("http://servidor:8000", "http://servidor.local:8000", "http://pc.home.arpa:8000",
                 "http://[fe80::1]:8000"):
        assert de_la_red(client, base_url=base).get("/panel/clases", headers=galleta(valor)).status_code == 200, base


def test_hosts_de_red():
    for bueno in ("192.168.1.10", "10.8.0.2", "fe80::1", "servidor", "pc-de-casa.local", "nas.lan",
                  "pc.home.arpa", "servidor."):
        assert host_de_red_valido(bueno), bueno
    for malo in ("", "malo.example", "classmer.com", "192.168.1.10.nip.io", "evil.local.example.com",
                 "x y", "ñandú.com"):
        assert not host_de_red_valido(malo), malo


def test_siguiente_de_afuera_no_redirige_afuera(panel: TestClient, red: TestClient):
    poner(panel)
    for siguiente in ("//malo.example/panel", "https://malo.example/panel", "http:/\\malo.example",
                      "/\\malo.example", "/panel\\..\\..\\x", "/panel//malo.example", "javascript:alert(1)",
                      "/panelx", "/api/materias", "/panel/entrar", "/panel/clases\r\nSet-Cookie: x=1", ""):
        r = entrar(red, siguiente=siguiente)
        assert r.status_code == 303 and r.headers["location"] == "/panel/clases", siguiente
        red.cookies.clear()
    assert siguiente_seguro("/panel/clases/abc?x=1#y") == "/panel/clases/abc?x=1"
    assert siguiente_seguro("/panel") == "/panel"


def test_post_con_origin_de_otro_sitio_desde_la_red_403(panel: TestClient, red: TestClient):
    poner(panel)
    entrar(red)
    r = red.post("/panel/estilo", data={"estilo_global": "x"}, headers={"Origin": "http://malo.example"})
    assert r.status_code == 403 and "no viene del panel" in r.text
    r = red.post("/panel/estilo", data={"estilo_global": "x"}, headers={"Referer": "http://malo.example/form"})
    assert r.status_code == 403
    # Tampoco el de la PC (otro origen que el de esta página).
    r = red.post("/panel/estilo", data={"estilo_global": "x"}, headers={"Origin": LOCAL})
    assert r.status_code == 403
    # Ni para entrar (login CSRF).
    red.cookies.clear()
    assert red.post("/panel/entrar", data={"contrasena": CONTRASENA},
                    headers={"Origin": "http://malo.example"}).status_code == 403


def test_la_api_y_el_mcp_no_cambian(client: TestClient, panel: TestClient, red: TestClient):
    poner(panel)
    entrar(red)
    # La cookie del panel no abre la API ni el MCP: siguen con su token.
    assert red.get("/api/materias").status_code == 401
    assert red.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    r = red.get("/api/materias", headers={"Authorization": client.headers["Authorization"]})
    assert r.status_code == 200
    # Y el token de la app no abre el panel.
    otra = de_la_red(client, "192.168.1.52")
    assert otra.get("/panel/clases", headers={"Authorization": client.headers["Authorization"]}).status_code == 303
