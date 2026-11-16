"""El panel por el dominio público (§13, §14): Cloudflare Tunnel adelante y Cloudflare Access.

- `cloudflared` corre en la misma PC y le pasa al server los pedidos de `https://<dominio>/panel…`
  por 127.0.0.1. Un pedido "por el dominio" se reconoce por el Host o porque trae encabezados de
  Cloudflare (`trae_encabezados_cloudflare`); lo decide `AccesoPanel`.
- Cada pedido por el dominio trae el JWT de Access en `Cf-Access-Jwt-Assertion`. `VerificadorAccess`
  lo valida: RS256 con las claves del equipo (`https://<equipo>/cdn-cgi/access/certs`, en caché y
  vueltas a pedir si llega un `kid` nuevo), `aud`, `iss`, `exp`/`nbf` con un margen chico, y después
  decide el rol del `email` (`identidad`, el único lugar donde se decide): dueño si está en
  `cloudflare_access.emails`, invitado de sólo lectura si está en `panel.invitados`; si no, 403. Es
  bloqueante (puede bajar las claves): llamarlo en el threadpool.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from starlette.datastructures import Headers

from classmer.config import CloudflareAccess, Invitado, Panel
from classmer.util import ahora_iso

log = logging.getLogger("classmer.panel")

ENCABEZADO_JWT = "cf-access-jwt-assertion"
ENCABEZADOS_CLOUDFLARE = ("cf-connecting-ip", "cf-ray", ENCABEZADO_JWT)
ALGORITMO = "RS256"
MARGEN_S = 30                 # exp/nbf: diferencia de reloj tolerada con Cloudflare
VIGENCIA_CLAVES_S = 3600      # cada cuánto se vuelven a pedir las claves
GRACIA_CLAVES_S = 24 * 3600   # si no se pueden bajar, se siguen usando las de antes hasta este tope
ENTRE_PEDIDOS_S = 30          # un kid desconocido no hace pedir las claves más de una vez cada tanto
TIMEOUT_S = 10
MAX_JWT = 16 * 1024


DUENO = "dueno"
INVITADO = "invitado"


@dataclass(frozen=True)
class Identidad:
    """Quién es, según el mail del JWT de Access (se decide en cada pedido, con la config de ese momento)."""

    email: str
    rol: str                                # DUENO | INVITADO
    materias: frozenset[str] = frozenset()  # las que ve un invitado (al dueño no se le aplica)

    @property
    def es_invitado(self) -> bool:
        return self.rol == INVITADO


def identidad(email: str, panel: Panel) -> Identidad | None:
    """El rol de un mail (ya validado el JWT): dueño si está en `cloudflare_access.emails`; invitado si
    está en `panel.invitados`, con sus materias; None si no está en ninguna (403). La config no deja que
    un mail esté en las dos listas; si igual pasara, gana el rol con menos permisos."""
    email = email.strip().lower()
    invitado: Invitado | None = panel.invitado(email)
    if invitado is not None:
        return Identidad(email, INVITADO, frozenset(invitado.materias))
    if email in panel.cloudflare_access.emails:
        return Identidad(email, DUENO)
    return None


class AccesoInvalido(Exception):
    """Sin JWT de Access o con uno que no vale: 403. El mensaje es para el log, no para el navegador."""


class ClavesNoDisponibles(Exception):
    """No se pudieron bajar las claves de Access (y no hay unas de antes que sirvan): 503, cerrado."""


def trae_encabezados_cloudflare(encabezados: Headers) -> bool:
    """`Cf-Connecting-IP`, `Cf-Ray`, `Cf-Access-Jwt-Assertion` o `Cdn-Loop` con `cloudflare`."""
    if any(nombre in encabezados for nombre in ENCABEZADOS_CLOUDFLARE):
        return True
    return any("cloudflare" in valor.lower() for valor in encabezados.getlist("cdn-loop"))


def bajar_claves(url: str) -> dict[str, Any]:
    """El JWKS del equipo de Access (sin proxies del entorno ni redirecciones)."""
    r = httpx.get(url, timeout=TIMEOUT_S, follow_redirects=False, trust_env=False,
                  headers={"Accept": "application/json"})
    r.raise_for_status()
    datos = r.json()
    if not isinstance(datos, dict):
        raise ValueError("la respuesta no es un objeto JSON")
    return datos


@dataclass(frozen=True)
class Pedido:
    """La última vez que se pidieron las claves (para la página Acceso)."""

    cuando: str
    ok: bool
    detalle: str


@dataclass
class _Claves:
    claves: dict[str, Any] = field(default_factory=dict)   # kid → PyJWK
    bajadas: float | None = None                           # reloj de la última bajada buena
    intento: float | None = None                           # reloj del último pedido (bueno o malo)


class VerificadorAccess:
    """Valida el JWT de Access de un pedido por el dominio. Seguro entre hilos."""

    def __init__(self, bajar: Callable[[str], dict[str, Any]] | None = None,
                 reloj: Callable[[], float] = time.monotonic):
        self._bajar = bajar
        self._reloj = reloj
        self._lock = threading.Lock()
        self._cache: dict[str, _Claves] = {}
        self.ultimo: Pedido | None = None

    def verificar(self, token: str | None, panel: Panel) -> Identidad:
        """Quién es (dueño o invitado) si el JWT vale y su mail está en alguna de las dos listas.
        AccesoInvalido (403) o ClavesNoDisponibles (503) si no."""
        email = self.validar_jwt(token, panel.cloudflare_access)
        quien = identidad(email, panel)
        if quien is None:
            raise AccesoInvalido("el mail del JWT no es dueño (panel.cloudflare_access.emails) ni invitado "
                                 "(panel.invitados)")
        return quien

    def validar_jwt(self, token: str | None, ajustes: CloudflareAccess) -> str:
        """El mail del JWT (en minúscula) si el JWT vale: firma, `aud`, `iss`, `exp` y `nbf`. No mira las
        listas: eso es `verificar`."""
        if not ajustes.completa:
            raise AccesoInvalido("cloudflare_access incompleto")
        if not token:
            raise AccesoInvalido("sin Cf-Access-Jwt-Assertion")
        token = token.strip()
        if len(token) > MAX_JWT or not token.isascii():
            raise AccesoInvalido("JWT ilegible")
        try:
            cabecera = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as e:
            raise AccesoInvalido(f"JWT ilegible ({e.__class__.__name__})") from e
        if cabecera.get("alg") != ALGORITMO:
            raise AccesoInvalido(f"algoritmo {cabecera.get('alg')!r}")
        kid = cabecera.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AccesoInvalido("JWT sin kid")
        clave = self._clave(ajustes.url_claves, kid)
        try:
            datos = jwt.decode(token, clave.key, algorithms=[ALGORITMO], audience=ajustes.aud,
                               issuer=ajustes.emisor, leeway=MARGEN_S,
                               options={"require": ["exp", "aud", "iss"]})
        except jwt.InvalidTokenError as e:
            raise AccesoInvalido(f"JWT inválido ({e.__class__.__name__})") from e
        email = datos.get("email")
        if not isinstance(email, str) or not email.strip():
            raise AccesoInvalido("el JWT no trae mail")
        return email.strip().lower()

    # --- claves ---
    def _clave(self, url: str, kid: str) -> Any:
        with self._lock:
            ahora = self._reloj()
            c = self._cache.get(url)
            if c is not None and kid in c.claves and c.bajadas is not None and ahora - c.bajadas < VIGENCIA_CLAVES_S:
                return c.claves[kid]
            if c is not None and c.intento is not None and ahora - c.intento < ENTRE_PEDIDOS_S:
                # Se pidieron hace nada: no se vuelve a pedir por cada pedido con un kid raro.
                return self._de_antes(c, kid, ahora, "se pidieron hace menos de "
                                      f"{ENTRE_PEDIDOS_S} s")
            if c is None:
                c = self._cache.setdefault(url, _Claves())
            c.intento = ahora
            try:
                claves = self._bajar_y_leer(url)
            except Exception as e:  # noqa: BLE001 - red, HTTP, JSON o claves que no se pueden leer
                detalle = f"{e.__class__.__name__}: {e}"[:300]
                self.ultimo = Pedido(ahora_iso(), False, detalle)
                log.warning("panel: no pude bajar las claves de Cloudflare Access (%s): %s", url, detalle)
                return self._de_antes(c, kid, ahora, detalle)
            c.claves, c.bajadas = claves, ahora
            self.ultimo = Pedido(ahora_iso(), True, f"{len(claves)} clave{'s' if len(claves) != 1 else ''}")
            log.info("panel: claves de Cloudflare Access bajadas (%d)", len(claves))
            if kid not in claves:
                raise AccesoInvalido(f"kid desconocido {kid[:40]!r}")
            return claves[kid]

    def _de_antes(self, c: _Claves, kid: str, ahora: float, detalle: str) -> Any:
        """Sin poder pedirlas ahora: las de antes, si no son demasiado viejas."""
        if c.bajadas is None or ahora - c.bajadas >= GRACIA_CLAVES_S:
            raise ClavesNoDisponibles(detalle)
        if kid not in c.claves:
            raise AccesoInvalido(f"kid desconocido {kid[:40]!r}")
        return c.claves[kid]

    def _bajar_y_leer(self, url: str) -> dict[str, Any]:
        datos = (self._bajar or bajar_claves)(url)
        conjunto = jwt.PyJWKSet.from_dict(datos)
        claves = {k.key_id: k for k in conjunto.keys
                  if k.key_id and k.key_type == "RSA" and k.algorithm_name in (ALGORITMO, None)}
        if not claves:
            raise ValueError("el JWKS no trae claves RSA con kid")
        return claves
