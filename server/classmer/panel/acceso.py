"""Acceso al panel desde la red (§13, §14): la contraseña, la cookie de sesión y el tope de intentos.

- La contraseña la pone el usuario en la PC del server (/panel/acceso). Se guarda un hash scrypt con sal en
  `Classmer-data\\panel-acceso.json`, nunca el texto, junto con un secreto al azar para firmar las
  cookies. Cambiar la contraseña o "Cerrar las sesiones de las otras PCs" regenera el secreto: todas
  las cookies que había dejan de valer. Quitar la contraseña borra el archivo.
- La sesión es una cookie firmada con HMAC-SHA256 que dura 90 días: no hay tabla de sesiones.
- Contra la fuerza bruta, un tope de intentos fallidos por IP y otro global, en memoria.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from classmer.util import ahora_iso, escribir_json

log = logging.getLogger("classmer.panel")

ARCHIVO = "panel-acceso.json"
MIN_CONTRASENA = 8
MAX_CONTRASENA = 1024          # scrypt con un texto enorme es trabajo gratis para el que lo manda
DURACION_SESION_S = 90 * 24 * 3600
COOKIE = "classmer_panel"      # + "_<puerto>": las cookies no separan puertos (8000 y uno de prueba)
VERSION_COOKIE = "v1"
# scrypt: 16 MiB de memoria y unas décimas de segundo por intento. Los parámetros quedan en el archivo,
# así un cambio de acá no invalida una contraseña ya puesta.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
# Tope de intentos fallidos: por IP y entre todas las IPs, en una ventana.
VENTANA_S = 10 * 60
TOPE_POR_IP = 5
TOPE_GLOBAL = 20


class ErrorContrasena(ValueError):
    """La contraseña nueva no sirve (corta, larga o no coincide): el mensaje es para mostrar."""


@dataclass(frozen=True)
class Estado:
    """Lo que hay en panel-acceso.json. `abierto`: hay contraseña y la red puede entrar."""

    abierto: bool
    hash: dict[str, Any] | None = None
    secreto: bytes | None = None
    puesta: str | None = None          # cuándo se puso o cambió la contraseña
    sesiones_desde: str | None = None  # cuándo se regeneró el secreto por última vez
    error: str | None = None           # el archivo no se pudo leer: la red queda cerrada


CERRADO = Estado(abierto=False)


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _hash(contrasena: str, sal: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(contrasena.encode("utf-8"), salt=sal, n=n, r=r, p=p,
                          maxmem=256 * r * n + 1024 * 1024, dklen=32)


def validar_nueva(contrasena: str, repetida: str) -> None:
    if len(contrasena) < MIN_CONTRASENA:
        raise ErrorContrasena(f"La contraseña tiene que tener al menos {MIN_CONTRASENA} caracteres.")
    if len(contrasena) > MAX_CONTRASENA:
        raise ErrorContrasena(f"La contraseña puede tener hasta {MAX_CONTRASENA} caracteres.")
    if contrasena != repetida:
        raise ErrorContrasena("Las dos contraseñas no coinciden.")


class Acceso:
    """El archivo de acceso, con su caché: se relee si cambia (también a mano, p. ej. si se borra)."""

    def __init__(self, home: Path):
        self.ruta = home / ARCHIVO
        self._lock = threading.Lock()
        self._marca: tuple[int, int] | None = None
        self._estado = CERRADO
        self.intentos = Intentos()

    # --- leer ---
    def estado(self) -> Estado:
        try:
            st = self.ruta.stat()
        except FileNotFoundError:
            self._marca, self._estado = None, CERRADO
            return CERRADO
        except OSError as e:
            return Estado(abierto=False, error=f"no se puede leer {self.ruta.name}: {e}")
        marca = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if marca != self._marca:
                self._estado = self._leer()
                self._marca = marca
            return self._estado

    def _leer(self) -> Estado:
        try:
            datos = json.loads(self.ruta.read_text(encoding="utf-8"))
            h = datos["hash"]
            if h.get("algoritmo") != "scrypt":
                raise ValueError(f"algoritmo desconocido {h.get('algoritmo')!r}")
            for clave in ("n", "r", "p"):
                if not isinstance(h[clave], int) or h[clave] < 1:
                    raise ValueError(f"parámetro {clave} inválido")
            if h["n"] < 2 or h["n"] & (h["n"] - 1):  # scrypt pide una potencia de 2 mayor que 1
                raise ValueError("parámetro n inválido")
            bytes.fromhex(h["sal"])
            bytes.fromhex(h["hash"])
            secreto = bytes.fromhex(datos["secreto"])
            if len(secreto) < 32:
                raise ValueError("secreto demasiado corto")
        except FileNotFoundError:
            return CERRADO
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.error("panel: %s ilegible, el acceso desde la red queda cerrado: %s", self.ruta.name,
                      e.__class__.__name__)
            return Estado(abierto=False, error=f"{self.ruta.name} está dañado: poné la contraseña de nuevo")
        return Estado(abierto=True, hash=h, secreto=secreto, puesta=datos.get("puesta"),
                      sesiones_desde=datos.get("sesiones_desde"))

    # --- escribir (sólo desde la PC) ---
    def poner(self, contrasena: str, repetida: str) -> None:
        """Pone o cambia la contraseña (y regenera el secreto: las sesiones de antes dejan de valer)."""
        validar_nueva(contrasena, repetida)
        sal = secrets.token_bytes(16)
        h = {"algoritmo": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "sal": sal.hex(),
             "hash": _hash(contrasena, sal, SCRYPT_N, SCRYPT_R, SCRYPT_P).hex()}
        ahora = ahora_iso()
        self._escribir({"version": 1, "hash": h, "secreto": secrets.token_bytes(32).hex(),
                        "puesta": ahora, "sesiones_desde": ahora})
        log.info("panel: contraseña del acceso desde la red puesta o cambiada")

    def cerrar_sesiones(self) -> bool:
        """Regenera el secreto: las cookies de las otras PCs dejan de valer. False si no hay contraseña."""
        e = self.estado()
        if not e.abierto:
            return False
        self._escribir({"version": 1, "hash": e.hash, "secreto": secrets.token_bytes(32).hex(),
                        "puesta": e.puesta, "sesiones_desde": ahora_iso()})
        log.info("panel: sesiones de las otras PCs cerradas")
        return True

    def quitar(self) -> bool:
        """Cierra el acceso desde la red: borra la contraseña (y el secreto). False si no había."""
        with self._lock:
            try:
                self.ruta.unlink()
            except FileNotFoundError:
                return False
            self._marca, self._estado = None, CERRADO
        log.info("panel: acceso desde la red cerrado (contraseña quitada)")
        return True

    def _escribir(self, datos: dict[str, Any]) -> None:
        with self._lock:
            escribir_json(self.ruta, datos)
            self._marca = None  # se relee en el próximo estado()

    # --- verificar ---
    def verificar(self, contrasena: str) -> bool:
        """¿Es la contraseña? Tarda (scrypt): llamarla en el threadpool."""
        e = self.estado()
        if not e.abierto or e.hash is None or len(contrasena) > MAX_CONTRASENA:
            return False
        h = e.hash
        calculado = _hash(contrasena, bytes.fromhex(h["sal"]), h["n"], h["r"], h["p"])
        return hmac.compare_digest(calculado, bytes.fromhex(h["hash"]))

    # --- cookie ---
    @staticmethod
    def nombre_cookie(puerto: int | None) -> str:
        return f"{COOKIE}_{puerto}" if puerto else COOKIE

    def emitir(self, ahora: float | None = None) -> str | None:
        """El valor de una cookie nueva: v1.<emitida>.<nonce>.<firma>. None si no hay contraseña."""
        e = self.estado()
        if not e.abierto or e.secreto is None:
            return None
        cuerpo = f"{VERSION_COOKIE}.{int(time.time() if ahora is None else ahora)}.{secrets.token_urlsafe(12)}"
        return f"{cuerpo}.{_firma(e.secreto, cuerpo)}"

    def sesion_valida(self, valor: str | None, ahora: float | None = None) -> bool:
        """La cookie está firmada con el secreto de ahora y no venció (90 días desde que se emitió)."""
        # Sólo ASCII: los encabezados llegan en latin-1 y "²".isdigit() es True; sin esto, una cookie
        # inventada reventaba la firma (UnicodeEncodeError, 500) en lugar de ir a Entrar.
        if not valor or not valor.isascii():
            return False
        e = self.estado()
        if not e.abierto or e.secreto is None:
            return False
        cuerpo, _, firma = valor.rpartition(".")
        partes = cuerpo.split(".")
        if len(partes) != 3 or partes[0] != VERSION_COOKIE or not partes[1].isdigit():
            return False
        if not hmac.compare_digest(firma.encode("ascii", "replace"), _firma(e.secreto, cuerpo).encode("ascii")):
            return False
        emitida, ahora = int(partes[1]), time.time() if ahora is None else ahora
        return emitida - 300 <= ahora < emitida + DURACION_SESION_S  # 5 min de margen para el reloj


def _firma(secreto: bytes, cuerpo: str) -> str:
    return _b64(hmac.new(secreto, b"classmer-panel|" + cuerpo.encode("ascii"), hashlib.sha256).digest())


# --- tope de intentos ----------------------------------------------------------------------------

class Intentos:
    """Intentos de entrar que no salieron bien, por IP y en total, en una ventana de tiempo. Un
    intento se anota antes de verificar (así, cien pedidos a la vez no pasan el tope) y se borra si
    la contraseña era la correcta."""

    def __init__(self, tope_ip: int = TOPE_POR_IP, tope_global: int = TOPE_GLOBAL, ventana_s: float = VENTANA_S):
        self.tope_ip, self.tope_global, self.ventana_s = tope_ip, tope_global, ventana_s
        self._lock = threading.Lock()
        self._todos: deque[tuple[float, str]] = deque()

    def _podar(self, ahora: float) -> None:
        while self._todos and self._todos[0][0] <= ahora - self.ventana_s:
            self._todos.popleft()

    def espera(self, ip: str, ahora: float | None = None) -> float:
        """Segundos que faltan para poder probar de nuevo desde esa IP (0: se puede)."""
        ahora = time.monotonic() if ahora is None else ahora
        with self._lock:
            self._podar(ahora)
            de_ip = [t for t, i in self._todos if i == ip]
            esperas = [0.0]
            if len(de_ip) >= self.tope_ip:
                esperas.append(de_ip[-self.tope_ip] + self.ventana_s - ahora)
            if len(self._todos) >= self.tope_global:
                esperas.append(self._todos[-self.tope_global][0] + self.ventana_s - ahora)
            return max(esperas)

    def anotar(self, ip: str, ahora: float | None = None) -> bool:
        """Anota un intento, si no se pasa el tope (False si se pasa: no se puede probar)."""
        ahora = time.monotonic() if ahora is None else ahora
        with self._lock:
            self._podar(ahora)
            de_ip = sum(1 for _, i in self._todos if i == ip)
            if de_ip >= self.tope_ip or len(self._todos) >= self.tope_global:
                return False
            self._todos.append((ahora, ip))
        return True

    def bien(self, ip: str) -> None:
        """La contraseña era la correcta: se olvidan los intentos de esa IP."""
        with self._lock:
            self._todos = deque((t, i) for t, i in self._todos if i != ip)

    def fallidos(self, ahora: float | None = None) -> int:
        ahora = time.monotonic() if ahora is None else ahora
        with self._lock:
            self._podar(ahora)
            return len(self._todos)


# --- redirección después de entrar ---------------------------------------------------------------

def siguiente_seguro(valor: str | None, default: str = "/panel/clases") -> str:
    """Adónde volver después de entrar: sólo una ruta del panel de este mismo server (nada de
    `//otro.sitio`, `\\`, esquemas ni saltos de línea: no hay open redirect)."""
    valor = (valor or "").strip()
    if not valor or any(c in valor for c in "\\\r\n\t") or not valor.isascii() or not valor.isprintable():
        return default
    ruta = valor.split("?", 1)[0].split("#", 1)[0]
    if not (ruta == "/panel" or ruta.startswith("/panel/")) or "//" in ruta:
        return default
    if ruta in ("/panel/entrar", "/panel/salir"):
        return default
    return valor.split("#", 1)[0]


# --- IPs de la PC (la URL para las otras PCs) ----------------------------------------------------

def _rango(ip: ipaddress.IPv4Address) -> int:
    """Orden de preferencia: primero la LAN de casa (192.168), después 172.16/12 y al final 10/8
    (ahí suelen caer las VPN, como 10.8.0.2)."""
    if ip in ipaddress.ip_network("192.168.0.0/16"):
        return 0
    if ip in ipaddress.ip_network("172.16.0.0/12"):
        return 1
    if ip in ipaddress.ip_network("10.0.0.0/8"):
        return 2
    return 3


def ips_de_la_pc() -> list[str]:
    """Las IPv4 privadas de esta PC (sin loopback ni link-local), la de la LAN primero."""
    vistas: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            vistas.add(info[4][0])
    except OSError:
        pass
    # La IP por la que sale el tráfico (no manda nada: un connect UDP sólo elige la interfaz).
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 9))
            vistas.add(s.getsockname()[0])
    except OSError:
        pass
    ips = []
    for texto in vistas:
        try:
            ip = ipaddress.IPv4Address(texto)
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified:
            ips.append(ip)
    return [str(ip) for ip in sorted(ips, key=lambda i: (_rango(i), int(i)))]


def es_cliente_local(host: str) -> bool:
    return host in ("127.0.0.1", "::1") or host.startswith("::ffff:127.")


def host_de_red_valido(host: str) -> bool:
    """Un Host que puede mandar un navegador de la red: una IP literal o un nombre de la LAN (sin
    puntos, .local, .lan, .home.arpa). Un dominio público no: contra DNS rebinding."""
    host = host.rstrip(".").lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if not all(c.isalnum() or c in "-." for c in host) or not host.isascii():
        return False
    return "." not in host or host.endswith((".local", ".lan", ".home.arpa"))

