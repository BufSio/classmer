"""Puente stdio → HTTP para clientes MCP que sólo saben lanzar un proceso (Claude Desktop).

Uso, con cualquier Python ≥ 3.10 y sin dependencias (no hace falta el entorno de uv):

    python <Classmer>\\server\\classmer\\mcp\\puente.py [--url http://127.0.0.1:8000/mcp]

Lee de stdin un mensaje JSON-RPC por línea, lo manda por POST al MCP de Classmer y escribe la
respuesta en stdout, también una por línea. El token sale de `CLASSMER_TOKEN` o del `.env` de la
carpeta de datos (`CLASSMER_HOME` o `%USERPROFILE%\\Documents\\Classmer-data`): así no queda
escrito en la configuración del cliente. Si el server no responde, cada pedido vuelve con un error
que lo explica (y el cliente sigue andando).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

URL_DEFAULT = "http://127.0.0.1:8000/mcp"
TIMEOUT_S = 120


def carpeta_datos() -> Path:
    env = os.environ.get("CLASSMER_HOME")
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    return Path.home() / "Documents" / "Classmer-data"


def leer_token() -> str | None:
    """CLASSMER_TOKEN del entorno o del .env de la carpeta de datos."""
    if os.environ.get("CLASSMER_TOKEN"):
        return os.environ["CLASSMER_TOKEN"].strip()
    ruta = carpeta_datos() / ".env"
    try:
        lineas = ruta.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for linea in lineas:
        clave, sep, valor = linea.partition("=")
        if sep and clave.strip() == "CLASSMER_TOKEN" and valor.strip():
            return valor.strip().strip('"').strip("'")
    return None


def _log(mensaje: str) -> None:
    print(f"classmer-puente: {mensaje}", file=sys.stderr, flush=True)


class Puente:
    def __init__(self, url: str, token: str | None, salida: Any = None):
        self.url = url
        self.token = token
        self.salida = salida or sys.stdout.buffer
        self.lock = threading.Lock()
        # Sin proxies: el server está en la misma PC (una VPN o un proxy del sistema no deben meterse).
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def escribir(self, mensaje: Any) -> None:
        datos = json.dumps(mensaje, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self.lock:
            self.salida.write(datos)
            self.salida.flush()

    def _error(self, mensaje: Any, texto: str) -> None:
        """Responde con un error a cada pedido (los que tienen id) del mensaje original."""
        pedidos = mensaje if isinstance(mensaje, list) else [mensaje]
        for m in pedidos:
            if isinstance(m, dict) and "method" in m and "id" in m:
                self.escribir({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32000, "message": texto}})
        _log(texto)

    def atender(self, linea: bytes) -> None:
        try:
            mensaje = json.loads(linea)
        except ValueError:
            self.escribir({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON inválido"}})
            return
        encabezados = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            encabezados["Authorization"] = f"Bearer {self.token}"
        pedido = urllib.request.Request(self.url, data=json.dumps(mensaje).encode("utf-8"),
                                        headers=encabezados, method="POST")
        try:
            with self.opener.open(pedido, timeout=TIMEOUT_S) as r:
                tipo = r.headers.get("Content-Type", "")
                cuerpo = r.read()
        except urllib.error.HTTPError as e:
            detalle = "token inválido: revisá CLASSMER_TOKEN en Classmer-data\\.env" if e.code == 401 else e.reason
            self._error(mensaje, f"Classmer respondió {e.code} ({detalle})")
            return
        except (urllib.error.URLError, OSError) as e:
            razon = getattr(e, "reason", e)
            self._error(mensaje, f"Classmer no responde en {self.url} ({razon}): ¿está corriendo el server?")
            return
        if not cuerpo.strip():
            return  # 202: era una notificación
        for respuesta in self._respuestas(tipo, cuerpo):
            self.escribir(respuesta)

    @staticmethod
    def _respuestas(tipo: str, cuerpo: bytes) -> list[Any]:
        if tipo.startswith("text/event-stream"):
            datos = [linea[5:].strip() for linea in cuerpo.decode("utf-8").splitlines() if linea.startswith("data:")]
            salida = [json.loads(d) for d in datos if d]
        else:
            salida = [json.loads(cuerpo)]
        # stdio no lleva lotes: cada respuesta va en su línea.
        return [r for item in salida for r in (item if isinstance(item, list) else [item])]

    def correr(self, entrada: Any = None) -> None:
        entrada = entrada or sys.stdin.buffer
        with ThreadPoolExecutor(max_workers=4) as pool:
            for linea in entrada:
                if linea.strip():
                    pool.submit(self._atender_seguro, linea)

    def _atender_seguro(self, linea: bytes) -> None:
        try:
            self.atender(linea)
        except Exception as e:  # noqa: BLE001 — un mensaje raro no corta el puente
            _log(f"error inesperado: {e.__class__.__name__}: {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puente stdio → HTTP para el MCP de Classmer")
    parser.add_argument("--url", default=os.environ.get("CLASSMER_MCP_URL", URL_DEFAULT))
    args = parser.parse_args(argv)
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    token = leer_token()
    if not token:
        _log(f"no encontré CLASSMER_TOKEN (ni en el entorno ni en {carpeta_datos() / '.env'})")
    Puente(args.url, token).correr()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
