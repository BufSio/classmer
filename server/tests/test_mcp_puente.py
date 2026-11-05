"""Puente stdio → HTTP (classmer/mcp/puente.py) contra un server real en un puerto libre.

Se corre como lo lanzaría Claude Desktop: un proceso aparte con el Python del sistema (`-I`, sin
el paquete classmer), leyendo el token del .env de CLASSMER_HOME.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from classmer.mcp import puente

PUENTE = Path(puente.__file__)


@pytest.fixture
def url(cfg):
    from classmer.api.app import crear_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    puerto = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(crear_app(cfg, iniciar_worker=False), log_config=None, access_log=False))
    hilo = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    hilo.start()
    limite = time.monotonic() + 10
    while not server.started and time.monotonic() < limite:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{puerto}/mcp"
    server.should_exit = True
    hilo.join(10)
    sock.close()


def correr(mensajes: list[dict], url: str, home: Path, **env: str) -> tuple[list[dict], str]:
    entorno = {k: v for k, v in os.environ.items() if k not in ("CLASSMER_TOKEN", "CLASSMER_HOME")}
    entorno.update(CLASSMER_HOME=str(home), **env)
    entrada = "".join(json.dumps(m) + "\n" for m in mensajes).encode("utf-8")
    r = subprocess.run([sys.executable, "-I", str(PUENTE), "--url", url], input=entrada, capture_output=True,
                       env=entorno, timeout=60)
    assert r.returncode == 0, r.stderr
    salida = [json.loads(linea) for linea in r.stdout.decode("utf-8").splitlines() if linea.strip()]
    return sorted(salida, key=lambda m: str(m.get("id"))), r.stderr.decode("utf-8", "replace")


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
LISTA = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def test_reenvia_con_el_token_del_env(url, home):
    salida, _ = correr([INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"}, LISTA,
                        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "listar_materias", "arguments": {}}}], url, home)
    assert [m["id"] for m in salida] == [1, 2, 3]  # la notificación no tiene respuesta
    assert salida[0]["result"]["serverInfo"]["name"] == "classmer"
    assert len(salida[1]["result"]["tools"]) == 8
    materias = json.loads(salida[2]["result"]["content"][0]["text"])["materias"]
    assert {m["id"] for m in materias} == {"prueba", "otra"}


def test_token_malo(url, home):
    salida, err = correr([LISTA], url, home, CLASSMER_TOKEN="otro")
    assert salida == [{"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": salida[0]["error"]["message"]}}]
    assert "401" in salida[0]["error"]["message"] and "401" in err


def test_server_caido(home):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto = s.getsockname()[1]  # libre: nadie escucha ahí
    salida, _ = correr([LISTA, {"jsonrpc": "2.0", "method": "notifications/initialized"}],
                       f"http://127.0.0.1:{puerto}/mcp", home)
    assert len(salida) == 1 and "no responde" in salida[0]["error"]["message"]


def test_linea_que_no_es_json(url, home):
    entorno = {**os.environ, "CLASSMER_HOME": str(home)}
    entorno.pop("CLASSMER_TOKEN", None)
    r = subprocess.run([sys.executable, "-I", str(PUENTE), "--url", url], input=b"{roto\n\n", capture_output=True,
                       env=entorno, timeout=60)
    assert json.loads(r.stdout) == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON inválido"}}


def test_leer_token(tmp_path, monkeypatch):
    monkeypatch.delenv("CLASSMER_TOKEN", raising=False)
    monkeypatch.setenv("CLASSMER_HOME", str(tmp_path))
    assert puente.leer_token() is None
    (tmp_path / ".env").write_text('GEMINI_API_KEY=x\nCLASSMER_TOKEN="abc"\n', encoding="utf-8")
    assert puente.leer_token() == "abc"
    monkeypatch.setenv("CLASSMER_TOKEN", "del-entorno")
    assert puente.leer_token() == "del-entorno"


def test_respuestas_sse_y_lotes():
    sse = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
    assert puente.Puente._respuestas("text/event-stream", sse) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    lote = json.dumps([{"id": 1}, {"id": 2}]).encode()
    assert puente.Puente._respuestas("application/json", lote) == [{"id": 1}, {"id": 2}]
