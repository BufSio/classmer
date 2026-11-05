"""Subida resumible del audio (§4 Subida y §5.1 PUT/GET /audio)."""

from __future__ import annotations

import hashlib
import os
import random
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from classmer import sesiones

INICIO = "2026-10-15T14:00:05-03:00"
MB = 1024 * 1024


def crear(client: TestClient) -> str:
    sid = str(uuid.uuid4())
    r = client.post("/api/sesiones", json={"id": sid, "materia_id": "prueba", "origen_materia": "manual",
                                           "tipo_clase": "teorica", "inicio": INICIO})
    assert r.status_code == 201, r.text
    return sid


def put(client: TestClient, sid: str, offset: int, datos: bytes, **kw):
    headers = {"Content-Type": "application/octet-stream", **kw.pop("headers", {})}
    return client.put(f"/api/sesiones/{sid}/audio?offset={offset}", content=datos, headers=headers, **kw)


def audio(cfg, sid: str) -> bytes:
    ruta = cfg.sesion_dir(sid) / "audio.ogg"
    return ruta.read_bytes() if ruta.exists() else b""


def bytes_aleatorios(n: int, semilla: int = 7) -> bytes:
    return random.Random(semilla).randbytes(n)


# --- offset correcto / incorrecto -----------------------------------------------------------------

def test_put_en_partes(client: TestClient, cfg):
    sid = crear(client)
    datos = bytes_aleatorios(10_000)
    offset = 0
    for largo in (1, 999, 4000, 3, 4997):
        r = put(client, sid, offset, datos[offset:offset + largo])
        assert r.status_code == 200, r.text
        assert r.json() == {"bytes_recibidos": offset + largo}
        offset += largo
    assert offset == len(datos)
    assert audio(cfg, sid) == datos
    assert client.get(f"/api/sesiones/{sid}/audio").json() == {"bytes_recibidos": len(datos)}
    assert client.get(f"/api/sesiones/{sid}").json()["bytes_recibidos"] == len(datos)


@pytest.mark.parametrize("offset_malo", [1, 499, 501, 10_000])
def test_put_offset_distinto_409_y_retoma(client: TestClient, cfg, offset_malo: int):
    sid = crear(client)
    datos = bytes_aleatorios(1000)
    assert put(client, sid, 0, datos[:500]).json() == {"bytes_recibidos": 500}
    r = put(client, sid, offset_malo, datos[offset_malo:] or b"x")
    assert r.status_code == 409
    assert r.json() == {"bytes_recibidos": 500}
    assert audio(cfg, sid) == datos[:500]  # no tocó el archivo
    # El celu retoma desde lo que dice el server.
    m = r.json()["bytes_recibidos"]
    r = put(client, sid, m, datos[m:])
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": 1000}
    assert audio(cfg, sid) == datos


def test_put_repetido_no_duplica(client: TestClient, cfg):
    # La respuesta se perdió y el celu reenvía el mismo pedazo: 409 con lo que ya hay.
    sid = crear(client)
    trozo = b"OggS" + bytes_aleatorios(300)
    assert put(client, sid, 0, trozo).status_code == 200
    r = put(client, sid, 0, trozo)
    assert r.status_code == 409 and r.json() == {"bytes_recibidos": len(trozo)}
    assert audio(cfg, sid) == trozo


def test_put_cero_bytes(client: TestClient, cfg):
    sid = crear(client)
    r = put(client, sid, 0, b"")
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": 0}
    put(client, sid, 0, b"abc")
    r = put(client, sid, 3, b"")
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": 3}
    r = put(client, sid, 0, b"")
    assert r.status_code == 409 and r.json() == {"bytes_recibidos": 3}
    assert audio(cfg, sid) == b"abc"


def test_put_corta_a_mitad_de_pagina_ogg(client: TestClient, cfg):
    # El server sólo concatena: cortes arbitrarios, incluso dentro de "OggS".
    sid = crear(client)
    datos = (b"OggS" + bytes_aleatorios(50, 1)) * 20
    cortes = [0, 2, 3, 57, 58, 500, len(datos)]
    for a, b in zip(cortes, cortes[1:]):
        assert put(client, sid, a, datos[a:b]).status_code == 200
    assert audio(cfg, sid) == datos


@pytest.mark.parametrize("query", ["", "?offset=", "?offset=-1", "?offset=abc", "?offset=1.5"])
def test_put_offset_invalido_422(client: TestClient, query: str):
    sid = crear(client)
    r = client.put(f"/api/sesiones/{sid}/audio{query}", content=b"abc")
    assert r.status_code == 422


def test_put_sesion_inexistente_404(client: TestClient):
    r = put(client, str(uuid.uuid4()), 0, b"abc")
    assert r.status_code == 404


def test_get_audio(client: TestClient):
    sid = crear(client)
    assert client.get(f"/api/sesiones/{sid}/audio").json() == {"bytes_recibidos": 0}
    put(client, sid, 0, b"x" * 1234)
    r = client.get(f"/api/sesiones/{sid}/audio")
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": 1234}


# --- límite de 8 MB -------------------------------------------------------------------------------

def test_limite_8mb_con_content_length(client: TestClient, cfg):
    assert sesiones.MAX_AUDIO_REQUEST == 8 * MB
    sid = crear(client)
    r = put(client, sid, 0, b"\x01" * (8 * MB + 1))
    assert r.status_code == 413
    assert audio(cfg, sid) == b""
    r = put(client, sid, 0, b"\x02" * (8 * MB))  # justo 8 MB entra
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": 8 * MB}


def test_limite_8mb_sin_content_length(client: TestClient, cfg):
    sid = crear(client)
    put(client, sid, 0, b"abc")

    def trozos():
        for _ in range(9):
            yield b"\x03" * MB

    # Un generador va con Transfer-Encoding: chunked, sin Content-Length.
    r = client.put(f"/api/sesiones/{sid}/audio?offset=3", content=trozos(),
                   headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413
    assert audio(cfg, sid) == b"abc"

    def pocos():
        yield b"de"
        yield b"f"

    r = client.put(f"/api/sesiones/{sid}/audio?offset=3", content=pocos())
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": 6}
    assert audio(cfg, sid) == b"abcdef"


def test_limite_con_content_length_mentiroso(client: TestClient, cfg, monkeypatch: pytest.MonkeyPatch):
    # Aunque el encabezado diga poco, se cuenta lo que llega de verdad.
    monkeypatch.setattr(sesiones, "MAX_AUDIO_REQUEST", 100)
    sid = crear(client)

    def trozos():
        yield b"a" * 60
        yield b"b" * 60

    r = client.put(f"/api/sesiones/{sid}/audio?offset=0", content=trozos(), headers={"Content-Length": "10"})
    assert r.status_code == 413
    assert audio(cfg, sid) == b""


# --- concurrencia ---------------------------------------------------------------------------------

def _subir_como_celu(client: TestClient, sid: str, datos: bytes, trozo: int, errores: list) -> None:
    """Lógica del celu: pregunta cuánto hay, manda lo que sigue y respeta los 409."""
    try:
        for _ in range(10_000):
            m = client.get(f"/api/sesiones/{sid}/audio").json()["bytes_recibidos"]
            if m >= len(datos):
                return
            r = put(client, sid, m, datos[m:m + trozo])
            if r.status_code not in (200, 409):
                errores.append(r.status_code)
                return
        errores.append("no terminó")
    except Exception as e:  # pragma: no cover - lo reporta el assert
        errores.append(repr(e))


def test_put_concurrentes_por_http(client: TestClient, cfg):
    sid = crear(client)
    datos = bytes_aleatorios(200_000, 3)
    errores: list = []
    hilos = [threading.Thread(target=_subir_como_celu, args=(client, sid, datos, 7_001 + i * 13, errores))
             for i in range(6)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    assert not errores
    assert audio(cfg, sid) == datos
    assert client.get(f"/api/sesiones/{sid}/audio").json() == {"bytes_recibidos": len(datos)}


def test_agregar_audio_concurrente_mismo_offset(cfg, base):
    # Muchos hilos mandan el mismo pedazo a la vez: entra uno solo.
    with base.conexion() as conn:
        sesiones.crear(cfg, conn, sesion_id="s-conc", materia_id=None, origen_materia=None,
                       tipo_clase=None, inicio=INICIO, dispositivo=None)
    trozo = bytes_aleatorios(5000, 9)
    barrera = threading.Barrier(16)
    resultados: list = []

    def subir():
        barrera.wait()
        resultados.append(sesiones.agregar_audio(cfg, base, "s-conc", 0, trozo))

    hilos = [threading.Thread(target=subir) for _ in range(16)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)
    assert sum(r.ok for r in resultados) == 1
    assert all(r.bytes_recibidos == len(trozo) for r in resultados if r.ok)
    assert (cfg.sesion_dir("s-conc") / "audio.ogg").read_bytes() == trozo
    with base.conexion() as conn:
        f = conn.execute("SELECT bytes_recibidos FROM assets WHERE id = 'audio:s-conc'").fetchone()
    assert f["bytes_recibidos"] == len(trozo)


def test_agregar_audio_concurrente_secuencia(cfg, base):
    # Cada hilo intenta todos los pedazos en orden; el archivo queda exacto.
    with base.conexion() as conn:
        sesiones.crear(cfg, conn, sesion_id="s-seq", materia_id=None, origen_materia=None,
                       tipo_clase=None, inicio=INICIO, dispositivo=None)
    datos = bytes_aleatorios(64 * 1000, 11)
    pedazos = [(i, datos[i:i + 1000]) for i in range(0, len(datos), 1000)]

    def subir():
        for off, p in pedazos:
            sesiones.agregar_audio(cfg, base, "s-seq", off, p)

    hilos = [threading.Thread(target=subir) for _ in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    assert (cfg.sesion_dir("s-seq") / "audio.ogg").read_bytes() == datos


# --- después de finalizar -------------------------------------------------------------------------

def test_put_despues_de_finalizar_no_agrega(client: TestClient, cfg):
    sid = crear(client)
    datos = b"OggS" + bytes_aleatorios(2000, 5)
    put(client, sid, 0, datos)
    r = client.post(f"/api/sesiones/{sid}/finalizar", json={
        "sha256": hashlib.sha256(datos).hexdigest(), "bytes_totales": len(datos),
        "duracion_ms": 60_000, "eventos_totales": 0})
    assert r.status_code == 200
    r = put(client, sid, len(datos), b"mas bytes")
    assert r.status_code == 409 and r.json() == {"bytes_recibidos": len(datos)}
    r = put(client, sid, 0, datos)
    assert r.status_code == 409 and r.json() == {"bytes_recibidos": len(datos)}
    # Un PUT vacío en el offset correcto es inocuo (el celu pregunta "¿cuánto tenés?").
    r = put(client, sid, len(datos), b"")
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": len(datos)}
    assert audio(cfg, sid) == datos
    assert client.get(f"/api/sesiones/{sid}/audio").json() == {"bytes_recibidos": len(datos)}


def test_tamano_lo_da_el_archivo(client: TestClient, cfg):
    # bytes_recibidos sale del archivo en disco (fuente de verdad), no de la base.
    sid = crear(client)
    put(client, sid, 0, b"12345")
    with open(cfg.sesion_dir(sid) / "audio.ogg", "ab") as f:
        f.write(b"67")
        f.flush()
        os.fsync(f.fileno())
    assert client.get(f"/api/sesiones/{sid}/audio").json() == {"bytes_recibidos": 7}
    assert put(client, sid, 5, b"x").status_code == 409
