"""El panel por el dominio público (§13, §14): Cloudflare Tunnel + Access adelante, y en el server los
tres candados: el JWT de Access (validado en cada pedido), la contraseña y la cookie con Secure.

Todo con valores inventados: un par de claves RSA de prueba y un JWKS falso (nunca el Access real)."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from classmer.config import CloudflareAccess, Config, ConfigError, Panel, config_desde_texto
from classmer.panel import acceso as acceso_mod
from classmer.panel import cloudflare as cf_mod
from classmer.panel.cloudflare import AccesoInvalido, ClavesNoDisponibles, VerificadorAccess

from test_panel import SID, armar_clase
from test_panel_acceso import CONTRASENA, IP_OTRA_PC, LOCAL, RED, de_la_red, entrar, poner

DOMINIO = "panel.ejemplo.com"
URL_DOMINIO = f"https://{DOMINIO}"
EQUIPO = "equipo-de-prueba.cloudflareaccess.com"
EMISOR = f"https://{EQUIPO}"
AUD = "aud-de-prueba-0123456789abcdef"
MAIL = "alguien@ejemplo.com"
IP_DE_AFUERA = "203.0.113.7"
COOKIE_DOMINIO = "classmer_panel_443"   # TestClient: el "puerto del server" sale de la URL base (https)


def _par() -> tuple[rsa.RSAPrivateKey, dict]:
    privada = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return privada, json.loads(RSAAlgorithm.to_jwk(privada.public_key()))


PRIVADA, PUBLICA = _par()
PRIVADA_NUEVA, PUBLICA_NUEVA = _par()
PRIVADA_AJENA, _ = _par()


def jwks(*claves: tuple[str, dict]) -> dict:
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"} for kid, jwk in claves]}


class ClavesFalsas:
    """Lo que devolvería https://<equipo>/cdn-cgi/access/certs; cuenta los pedidos."""

    def __init__(self) -> None:
        self.datos: dict = jwks(("k1", PUBLICA))
        self.falla = False
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        if self.falla:
            raise OSError("sin conexión")
        return self.datos


def firmar(privada=PRIVADA, kid: str = "k1", **cambios) -> str:
    ahora = int(time.time())
    datos = {"aud": [AUD], "iss": EMISOR, "email": MAIL, "iat": ahora, "nbf": ahora, "exp": ahora + 3600,
             "type": "app", "sub": "id-de-prueba", **cambios}
    datos = {k: v for k, v in datos.items() if v is not None}
    return jwt.encode(datos, privada, algorithm="RS256", headers={"kid": kid})


def ajustes(**cambios) -> CloudflareAccess:
    return CloudflareAccess(**{"equipo": EQUIPO, "aud": AUD, "emails": [MAIL], **cambios})


def verificar(v: VerificadorAccess, token: str, access: CloudflareAccess) -> str:
    """El mail del dueño que verifica `v` (los invitados: test_panel_invitados)."""
    quien = v.verificar(token, Panel(cloudflare_access=access))
    assert not quien.es_invitado
    return quien.email


@pytest.fixture(autouse=True)
def scrypt_rapido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acceso_mod, "SCRYPT_N", 2**10)


@pytest.fixture
def claves(client: TestClient) -> ClavesFalsas:
    falsas = ClavesFalsas()
    client.app.state.verificador_access._bajar = falsas
    return falsas


def escribir_panel(home, client: TestClient, panel: dict | None) -> None:
    """La sección `panel` en config.yaml, y la config viva la aplica (como en producción)."""
    ruta = home / "config.yaml"
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    if panel is None:
        datos.pop("panel", None)
    else:
        datos["panel"] = panel
    ruta.write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")
    assert client.app.state.config_viva.recargar(forzar=True)
    assert client.app.state.config_viva.pendientes_reinicio == []


PANEL_YAML = {"dominio_publico": DOMINIO, "cloudflare_access": {"equipo": EQUIPO, "aud": AUD, "emails": [MAIL]}}


@pytest.fixture
def con_dominio(home, cfg: Config, client: TestClient, claves: ClavesFalsas) -> Config:
    """La config del dominio en config.yaml (un POST del panel que guarda la config la vuelve a leer)."""
    escribir_panel(home, client, PANEL_YAML)
    assert cfg.panel.por_el_dominio
    return cfg


@pytest.fixture
def panel(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url=LOCAL, client=("127.0.0.1", 50123), follow_redirects=False)


def por_el_dominio(client: TestClient, token: str | None = "firmar", ip_socket: str = "127.0.0.1",
                   ip_real: str | None = IP_DE_AFUERA, base_url: str = URL_DOMINIO) -> TestClient:
    """Un navegador por el túnel: llega por 127.0.0.1 con el Host del dominio y lo que pone Cloudflare."""
    encabezados = {"Cf-Ray": "8f0000000000-EZE", "Cdn-Loop": "cloudflare", "X-Forwarded-Proto": "https"}
    if ip_real:
        encabezados["Cf-Connecting-IP"] = ip_real
    if token:
        encabezados["Cf-Access-Jwt-Assertion"] = firmar() if token == "firmar" else token
    c = TestClient(client.app, base_url=base_url, client=(ip_socket, 40001), follow_redirects=False)
    c.headers.update(encabezados)
    return c


# --- el JWT de Access -----------------------------------------------------------------------------

def test_jwt_valido_pasa_a_pedir_la_contrasena_y_con_la_cookie_entra(con_dominio, panel, client, claves):
    poner(panel)
    nav = por_el_dominio(client)
    r = nav.get("/panel/clases")
    assert r.status_code == 303 and urlsplit(r.headers["location"]).path == "/panel/entrar"
    r = nav.get(r.headers["location"])
    assert r.status_code == 200 and 'name="contrasena"' in r.text and "Por internet" in r.text
    r = entrar(nav)
    assert r.status_code == 303 and r.headers["location"] == "/panel/clases"
    atributos = {a.strip().split("=")[0].lower() for a in r.headers["set-cookie"].split(";")}
    assert {"secure", "httponly"} <= atributos
    r = nav.get("/panel/clases")
    assert r.status_code == 200
    # No es el dueño: Salir sí, Acceso no.
    assert 'action="/panel/salir"' in r.text and 'href="/panel/acceso"' not in r.text
    assert claves.urls == [f"{EMISOR}/cdn-cgi/access/certs"]


def test_sin_jwt_403(con_dominio, panel, client):
    poner(panel)
    nav = por_el_dominio(client, token=None)
    r = nav.get("/panel/clases")
    assert r.status_code == 403 and "Cloudflare Access" in r.text
    assert entrar(nav).status_code == 403
    # Ni con una cookie válida: el JWT va antes que la cookie.
    valor = client.app.state.acceso.emitir()
    assert nav.get("/panel/clases", headers={"Cookie": f"{COOKIE_DOMINIO}={valor}"}).status_code == 403


@pytest.mark.parametrize("token", [
    pytest.param(lambda: firmar(aud=["otro-aud"]), id="aud"),
    pytest.param(lambda: firmar(iss="https://otro-equipo.cloudflareaccess.com"), id="iss"),
    pytest.param(lambda: firmar(email="otra@ejemplo.com"), id="email"),
    pytest.param(lambda: firmar(email=None), id="sin-email"),
    pytest.param(lambda: firmar(exp=int(time.time()) - 120), id="vencido"),
    pytest.param(lambda: firmar(exp=None), id="sin-exp"),
    pytest.param(lambda: firmar(nbf=int(time.time()) + 600), id="todavia-no"),
    pytest.param(lambda: firmar(PRIVADA_AJENA), id="otra-clave"),
    pytest.param(lambda: firmar(PRIVADA_AJENA, kid="desconocido"), id="kid-desconocido"),
    pytest.param(lambda: jwt.encode({"aud": AUD, "iss": EMISOR, "email": MAIL, "exp": int(time.time()) + 60},
                                    "secreto-de-un-hmac-bastante-largo", algorithm="HS256",
                                    headers={"kid": "k1"}), id="hs256"),
    pytest.param(lambda: "no.es.un-jwt", id="basura"),
])
def test_jwt_invalido_403(con_dominio, panel, client, token):
    poner(panel)
    nav = por_el_dominio(client, token=token())
    r = nav.get("/panel/entrar")
    assert r.status_code == 403 and "no pasó por Cloudflare Access" in r.text
    assert entrar(nav).status_code == 403


def test_el_mail_sin_mirar_mayusculas_y_el_margen_del_reloj():
    v = VerificadorAccess(bajar=ClavesFalsas())
    assert verificar(v, firmar(email="Alguien@Ejemplo.COM"), ajustes(emails=["ALGUIEN@ejemplo.com"])) == MAIL
    # Vencido hace 10 s: entra en el margen (30 s).
    assert verificar(v, firmar(exp=int(time.time()) - 10), ajustes()) == MAIL


def test_las_claves_no_se_pueden_bajar_503(con_dominio, panel, client, claves):
    poner(panel)
    claves.falla = True
    nav = por_el_dominio(client)
    r = nav.get("/panel/clases")
    assert r.status_code == 503 and "claves de Cloudflare Access" in r.text and r.headers["retry-after"]
    assert entrar(nav).status_code == 503
    # Acceso, en la PC, muestra que no se pudieron bajar.
    r = panel.get("/panel/acceso")
    assert "no se pudieron bajar" in r.text and "sin conexión" in r.text


def test_kid_nuevo_vuelve_a_pedir_las_claves():
    claves = ClavesFalsas()
    reloj = [1000.0]
    v = VerificadorAccess(bajar=claves, reloj=lambda: reloj[0])
    assert verificar(v, firmar(), ajustes()) == MAIL
    assert verificar(v, firmar(), ajustes()) == MAIL
    assert len(claves.urls) == 1  # en caché
    # Cloudflare rota las claves: llega un kid nuevo y se vuelven a pedir.
    claves.datos = jwks(("k1", PUBLICA), ("k2", PUBLICA_NUEVA))
    reloj[0] += cf_mod.ENTRE_PEDIDOS_S + 1
    assert verificar(v, firmar(PRIVADA_NUEVA, kid="k2"), ajustes()) == MAIL
    assert len(claves.urls) == 2
    # Un kid que no existe no hace pedir las claves en cada pedido.
    for _ in range(5):
        with pytest.raises(AccesoInvalido):
            verificar(v, firmar(PRIVADA_AJENA, kid="k9"), ajustes())
    assert len(claves.urls) == 2
    # Vencida la vigencia, se piden de nuevo.
    reloj[0] += cf_mod.VIGENCIA_CLAVES_S
    assert verificar(v, firmar(), ajustes()) == MAIL
    assert len(claves.urls) == 3


def test_si_no_se_pueden_bajar_siguen_las_de_antes_un_rato():
    claves = ClavesFalsas()
    reloj = [0.0]
    v = VerificadorAccess(bajar=claves, reloj=lambda: reloj[0])
    assert verificar(v, firmar(), ajustes()) == MAIL
    claves.falla = True
    reloj[0] += cf_mod.VIGENCIA_CLAVES_S + 1
    assert verificar(v, firmar(), ajustes()) == MAIL  # Cloudflare no contesta: las de hace una hora sirven
    assert v.ultimo is not None and not v.ultimo.ok
    reloj[0] += cf_mod.GRACIA_CLAVES_S
    with pytest.raises(ClavesNoDisponibles):
        verificar(v, firmar(), ajustes())
    # Sin claves de antes y sin poder bajarlas: 503, y no se reintenta en cada pedido.
    v2 = VerificadorAccess(bajar=claves, reloj=lambda: reloj[0])
    for _ in range(3):
        with pytest.raises(ClavesNoDisponibles):
            verificar(v2, firmar(), ajustes())
    assert len(claves.urls) == 4  # la buena, dos que fallaron y una sola del verificador nuevo


def test_un_jwks_sin_claves_rsa_es_como_no_poder_bajarlas():
    claves = ClavesFalsas()
    claves.datos = {"keys": []}
    with pytest.raises(ClavesNoDisponibles):
        verificar(VerificadorAccess(bajar=claves), firmar(), ajustes())


# --- Host, encabezados de Cloudflare y quién es el dueño --------------------------------------------

def test_host_del_dominio_desde_loopback(con_dominio, panel, client):
    poner(panel)
    # Sin JWT ni encabezados de Cloudflare, con el Host del dominio: también es por el dominio.
    pelado = TestClient(client.app, base_url=URL_DOMINIO, client=("127.0.0.1", 40002), follow_redirects=False)
    assert pelado.get("/panel/clases").status_code == 403
    nav = por_el_dominio(client)
    assert nav.get("/panel/clases").status_code == 303           # con JWT y sin cookie → Entrar
    entrar(nav)
    assert nav.get("/panel/clases").status_code == 200           # con cookie → entra
    # Sin mirar mayúsculas ni puerto.
    for base in ("https://PANEL.Ejemplo.com", "https://panel.ejemplo.com:443"):
        otra = por_el_dominio(client, base_url=base)
        otra.cookies = nav.cookies
        assert otra.get("/panel/clases").status_code == 200, base


def test_acceso_por_el_dominio_403_tambien_con_jwt_y_cookie(con_dominio, panel, client):
    poner(panel)
    nav = por_el_dominio(client)
    entrar(nav)
    assert nav.get("/panel/clases").status_code == 200
    r = nav.get("/panel/acceso")
    assert r.status_code == 403 and "sólo en la PC del server" in r.text
    for ruta in ("/panel/acceso/contrasena", "/panel/acceso/cerrar-sesiones", "/panel/acceso/cerrar"):
        r = nav.post(ruta, data={"contrasena": "otra-clave-x", "repetida": "otra-clave-x"},
                     headers={"Origin": URL_DOMINIO})
        assert r.status_code == 403, ruta
    assert nav.get("/panel/clases").status_code == 200


def test_encabezados_de_cloudflare_con_otro_host_403(con_dominio, panel, client):
    """La aplicación de Access es la misma para otros dominios (mismo aud): el JWT vale sólo con el Host exacto."""
    poner(panel)
    valor = client.app.state.acceso.emitir()
    otro = por_el_dominio(client, base_url="https://bot.ejemplo.com")
    r = otro.get("/panel/clases")
    assert r.status_code == 403 and "sólo en su dominio" in r.text
    assert entrar(otro).status_code == 403
    assert otro.get("/panel/clases", headers={"Cookie": f"{COOKIE_DOMINIO}={valor}"}).status_code == 403
    # Cf-Connecting-IP con Host 127.0.0.1 desde loopback: 403, no es el dueño (ni la red).
    r = panel.get("/panel/clases", headers={"Cf-Connecting-IP": IP_DE_AFUERA})
    assert r.status_code == 403
    for encabezado in ({"Cf-Ray": "x"}, {"Cdn-Loop": "cloudflare; loops=1"}, {"Cf-Access-Jwt-Assertion": firmar()}):
        assert panel.get("/panel/clases", headers=encabezado).status_code == 403, encabezado
        assert panel.get("/panel/acceso", headers=encabezado).status_code == 403, encabezado
    # Con una IP de la LAN como Host, tampoco.
    lan = por_el_dominio(client, ip_socket=IP_OTRA_PC, base_url=RED)
    assert lan.get("/panel/clases", headers={"Cookie": f"classmer_panel_8000={valor}"}).status_code == 403
    # Un Cdn-Loop de otro CDN no es Cloudflare: la PC sigue entrando.
    assert panel.get("/panel/clases", headers={"Cdn-Loop": "otro-cdn"}).status_code == 200


def test_otro_dominio_publico_403(con_dominio, panel, client):
    poner(panel)
    valor = client.app.state.acceso.emitir()
    otro = TestClient(client.app, base_url="https://malo.example", client=(IP_OTRA_PC, 40003), follow_redirects=False)
    assert otro.get("/panel/clases", headers={"Cookie": f"classmer_panel_443={valor}"}).status_code == 403
    sub = TestClient(client.app, base_url=f"https://x.{DOMINIO}", client=("127.0.0.1", 40004), follow_redirects=False)
    assert sub.get("/panel/clases").status_code == 403


def test_sin_dominio_o_sin_access_completo_el_dominio_da_403(cfg: Config, panel, client, claves):
    poner(panel)
    for config in (Panel(), Panel(cloudflare_access=ajustes()), Panel(dominio_publico=DOMINIO),
                   Panel(dominio_publico=DOMINIO, cloudflare_access=ajustes(aud=None)),
                   Panel(dominio_publico=DOMINIO, cloudflare_access=ajustes(emails=[]))):
        cfg.panel = config
        nav = por_el_dominio(client)
        assert nav.get("/panel/clases").status_code == 403, config
        assert entrar(nav).status_code == 403, config
    assert claves.urls == []
    # La PC y la red siguen como siempre.
    assert panel.get("/panel/clases").status_code == 200
    red = de_la_red(client)
    assert entrar(red).status_code == 303 and red.get("/panel/clases").status_code == 200


def test_la_config_del_dominio_se_lee_viva(home, cfg: Config, panel, client, claves):
    poner(panel)
    nav = por_el_dominio(client)
    assert nav.get("/panel/clases").status_code == 403
    escribir_panel(home, client, PANEL_YAML)
    assert nav.get("/panel/clases").status_code == 303
    escribir_panel(home, client, {**PANEL_YAML, "cloudflare_access": {"equipo": EQUIPO, "emails": [MAIL]}})
    assert nav.get("/panel/clases").status_code == 403
    escribir_panel(home, client, PANEL_YAML)
    # Guardar algo desde el panel (Estilo, en la PC) conserva la sección panel de config.yaml.
    assert panel.post("/panel/estilo", data={"estilo_global": "Algo."}).status_code == 303
    assert "dominio_publico: panel.ejemplo.com" in (home / "config.yaml").read_text(encoding="utf-8")
    assert client.app.state.config_viva.recargar(forzar=True)
    assert cfg.panel.por_el_dominio and nav.get("/panel/clases").status_code == 303
    escribir_panel(home, client, None)
    assert nav.get("/panel/clases").status_code == 403


def test_config_del_panel_valida(home):
    cfg = config_desde_texto(home, "panel:\n")
    assert cfg.panel.dominio_publico is None and not cfg.panel.por_el_dominio
    cfg = config_desde_texto(home, "panel:\n  dominio_publico: Panel.Ejemplo.com.\n  cloudflare_access:\n"
                                   f"    equipo: https://{EQUIPO.upper()}/\n    aud: ' {AUD} '\n"
                                   "    emails: Alguien@Ejemplo.com\n")
    assert cfg.panel.dominio_publico == DOMINIO and cfg.panel.por_el_dominio
    assert cfg.panel.cloudflare_access.equipo == EQUIPO and cfg.panel.cloudflare_access.aud == AUD
    assert cfg.panel.cloudflare_access.emails == [MAIL]
    assert cfg.panel.cloudflare_access.url_claves == f"https://{EQUIPO}/cdn-cgi/access/certs"
    for malo in ("https://panel.ejemplo.com", "panel.ejemplo.com:443", "panel.ejemplo.com/panel", "localhost",
                 "192.168.1.10", "servidor", "pc.local", "ñandú.com", "-x.ejemplo.com"):
        with pytest.raises(ConfigError):
            config_desde_texto(home, f"panel:\n  dominio_publico: '{malo}'\n")
    with pytest.raises(ConfigError):
        config_desde_texto(home, "panel:\n  cloudflare_access:\n    emails: [no-es-un-mail]\n")


# --- la IP del tope de intentos, la cookie y el mismo origen ----------------------------------------

def test_tope_de_intentos_por_cf_connecting_ip_desde_loopback(con_dominio, panel, client):
    poner(panel)
    nav = por_el_dominio(client, ip_real="203.0.113.10")
    for _ in range(5):
        assert entrar(nav, "no-es-esta-clave").status_code == 401
    assert entrar(nav).status_code == 429
    # Otra IP real (otro navegador por el túnel) todavía puede.
    assert entrar(por_el_dominio(client, ip_real="203.0.113.11")).status_code == 303


def test_desde_la_lan_el_cf_connecting_ip_no_cuenta(con_dominio, panel, client):
    poner(panel)
    # Por el dominio pero desde una IP de la LAN (no es cloudflared): cuenta la IP del socket.
    for i in range(5):
        nav = por_el_dominio(client, ip_socket=IP_OTRA_PC, ip_real=f"203.0.113.{20 + i}")
        assert entrar(nav, "no-es-esta-clave").status_code == 401
    assert entrar(por_el_dominio(client, ip_socket=IP_OTRA_PC, ip_real="203.0.113.99")).status_code == 429


def test_la_cookie_secure_por_el_dominio_y_sin_secure_en_la_lan(con_dominio, panel, client):
    poner(panel)
    r = entrar(por_el_dominio(client))
    assert "secure" in {a.strip().lower() for a in r.headers["set-cookie"].split(";")}
    r = entrar(de_la_red(client))
    assert "secure" not in {a.strip().lower() for a in r.headers["set-cookie"].split(";")}
    nav = por_el_dominio(client)
    entrar(nav)
    r = nav.post("/panel/salir", headers={"Origin": URL_DOMINIO})
    assert r.status_code == 303 and "Max-Age=0" in r.headers["set-cookie"] and "Secure" in r.headers["set-cookie"]


def test_mismo_origen_por_el_dominio(con_dominio, panel, client, cfg):
    armar_clase(client.app)
    poner(panel)
    nav = por_el_dominio(client)
    # Entrar con el Origin del dominio pasa; con otro, 403 (login CSRF).
    r = nav.post("/panel/entrar", data={"contrasena": CONTRASENA}, headers={"Origin": "https://malo.example"})
    assert r.status_code == 403
    r = nav.post("/panel/entrar", data={"contrasena": CONTRASENA}, headers={"Origin": URL_DOMINIO})
    assert r.status_code == 303
    r = nav.post("/panel/estilo", data={"estilo_global": "Por el dominio."}, headers={"Origin": URL_DOMINIO})
    assert r.status_code == 303 and cfg.estilo_global.strip() == "Por el dominio."
    for origen in ("https://malo.example", f"http://{DOMINIO}.malo.example", LOCAL, RED):
        r = nav.post("/panel/estilo", data={"estilo_global": "x"}, headers={"Origin": origen})
        assert r.status_code == 403 and "no viene del panel" in r.text, origen
    # Los fetch de Importar (cancelar un análisis) con el Origin del dominio pasan el filtro.
    r = nav.post("/panel/importar/no-existe/cancelar", headers={"Origin": URL_DOMINIO})
    assert r.status_code != 403
    r = nav.post("/panel/importar/no-existe/cancelar", headers={"Origin": "https://malo.example"})
    assert r.status_code == 403
    # Y la clase, con su PDF.
    assert nav.get(f"/panel/clases/{SID}").status_code == 200
    assert nav.get(f"/panel/clases/{SID}/apunte.pdf").content.startswith(b"%PDF")


def test_el_aviso_de_100_mb_solo_por_el_dominio(con_dominio, panel, client):
    armar_clase(client.app)
    poner(panel)
    nav = por_el_dominio(client)
    entrar(nav)
    for ruta in ("/panel/importar", f"/panel/clases/{SID}"):
        assert "Por el dominio, hasta 100 MB" in nav.get(ruta).text, ruta
        assert "Por el dominio, hasta 100 MB" not in panel.get(ruta).text, ruta
    assert '"tope_dominio": 100000000' in nav.get("/panel/importar").text
    assert '"tope_dominio": null' in panel.get("/panel/importar").text


def test_acceso_muestra_la_url_publica(cfg: Config, panel, client, claves):
    poner(panel)
    assert "No hay dominio público configurado" in panel.get("/panel/acceso").text
    cfg.panel = Panel(dominio_publico=DOMINIO)
    r = panel.get("/panel/acceso")
    assert "falta <code>panel.cloudflare_access</code>" in r.text and f"https://{DOMINIO}/panel" not in r.text
    cfg.panel = Panel(dominio_publico=DOMINIO, cloudflare_access=ajustes())
    r = panel.get("/panel/acceso")
    assert f'<a href="https://{DOMINIO}/panel">' in r.text and "se bajan con el primer pedido" in r.text
    nav = por_el_dominio(client)
    nav.get("/panel/entrar")
    assert "bajadas el" in panel.get("/panel/acceso").text


def test_el_log_dice_por_el_dominio(caplog: pytest.LogCaptureFixture, con_dominio, panel, client):
    caplog.set_level("INFO", logger="classmer.panel")
    poner(panel)
    entrar(por_el_dominio(client))
    assert any("por el dominio" in r.getMessage() and IP_DE_AFUERA in r.getMessage() and MAIL in r.getMessage()
               for r in caplog.records)
    entrar(por_el_dominio(client, token=None, ip_real="203.0.113.50"))
    assert any("no pasó por Cloudflare Access" in r.getMessage() and "203.0.113.50" in r.getMessage()
               for r in caplog.records)


# --- lo de siempre no cambia -------------------------------------------------------------------------

def test_la_pc_la_lan_la_api_y_el_mcp_siguen_igual(con_dominio, panel, client, claves):
    assert panel.get("/panel/clases").status_code == 200
    assert panel.get("/panel/acceso").status_code == 200
    red = de_la_red(client)
    assert red.get("/panel/clases").status_code == 403   # sin contraseña, como siempre
    poner(panel)
    assert entrar(red).status_code == 303 and red.get("/panel/clases").status_code == 200
    assert red.get("/panel/acceso").status_code == 403
    # La API y el MCP siguen con su token, también con encabezados de Cloudflare (no pasan por el filtro).
    nav = por_el_dominio(client)
    assert nav.get("/api/materias").status_code == 401
    assert nav.get("/api/materias", headers={"Authorization": client.headers["Authorization"]}).status_code == 200
    assert nav.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    assert claves.urls == []  # nada de eso pidió las claves
