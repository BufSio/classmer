"""POST /api/sesiones/{id}/finalizar (§5.1): los tres 409, el 200 y la idempotencia."""

from __future__ import annotations

import hashlib
import re
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from classmer import cola, sesiones
from classmer.db import Base

INICIO = "2026-10-15T14:00:05-03:00"
DESCARTADO_RE = re.compile(r"^audio\.descartado-\d{8}T\d{6}(-\d+)?\.ogg$")


def crear(client: TestClient) -> str:
    sid = str(uuid.uuid4())
    r = client.post("/api/sesiones", json={"id": sid, "materia_id": "prueba", "origen_materia": "calendario",
                                           "tipo_clase": "teorica", "inicio": INICIO})
    assert r.status_code == 201, r.text
    return sid


def subir(client: TestClient, sid: str, datos: bytes, offset: int = 0) -> None:
    r = client.put(f"/api/sesiones/{sid}/audio?offset={offset}", content=datos,
                   headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text


def evento(client: TestClient, sid: str, tipo: str, offset_ms: int = 1000, **campos):
    datos = {"id": str(uuid.uuid4()), "tipo": tipo, "offset_ms": str(offset_ms), **campos}
    files = None
    if tipo == "foto":
        files = {"archivo": (f"{datos['id']}.jpg", b"\xff\xd8\xff\xe0foto", "image/jpeg")}
    r = client.post(f"/api/sesiones/{sid}/eventos", data=datos, files=files)
    assert r.status_code == 201, r.text
    return datos["id"]


def finalizar(client: TestClient, sid: str, datos: bytes | None = None, **campos):
    cuerpo = {"sha256": hashlib.sha256(datos or b"").hexdigest(), "bytes_totales": len(datos or b""),
              "duracion_ms": 5_400_000, "eventos_totales": 0}
    cuerpo.update(campos)
    return client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo)


def fila(cfg, sid: str) -> dict:
    with Base(cfg.db_path).conexion() as conn:
        return dict(conn.execute("SELECT * FROM sesiones WHERE id = ?", (sid,)).fetchone())


def jobs(cfg, sid: str) -> list[cola.Job]:
    with Base(cfg.db_path).conexion() as conn:
        return cola.jobs_de_sesion(conn, sid)


AUDIO = b"OggS" + bytes(range(256)) * 40


# --- 409 faltan_bytes ------------------------------------------------------------------------------

def test_faltan_bytes(client: TestClient, cfg):
    sid = crear(client)
    subir(client, sid, AUDIO[:1000])
    r = finalizar(client, sid, AUDIO)
    assert r.status_code == 409
    assert r.json() == {"motivo": "faltan_bytes", "bytes_recibidos": 1000}
    assert fila(cfg, sid)["estado"] == "grabando"
    assert jobs(cfg, sid) == []
    # El celu sigue desde M y vuelve a finalizar.
    subir(client, sid, AUDIO[1000:], offset=1000)
    r = finalizar(client, sid, AUDIO)
    assert r.status_code == 200 and r.json() == {"estado": "en_cola"}


def test_faltan_bytes_sin_nada_subido(client: TestClient):
    sid = crear(client)
    r = finalizar(client, sid, AUDIO)
    assert r.status_code == 409 and r.json() == {"motivo": "faltan_bytes", "bytes_recibidos": 0}


def test_faltan_bytes_gana_sobre_faltan_eventos(client: TestClient):
    sid = crear(client)
    subir(client, sid, AUDIO[:10])
    r = finalizar(client, sid, AUDIO, eventos_totales=5)
    assert r.json() == {"motivo": "faltan_bytes", "bytes_recibidos": 10}


# --- 409 faltan_eventos ----------------------------------------------------------------------------

def test_faltan_eventos(client: TestClient, cfg):
    sid = crear(client)
    subir(client, sid, AUDIO)
    evento(client, sid, "nota", texto="hola")
    evento(client, sid, "foto")
    r = finalizar(client, sid, AUDIO, eventos_totales=4)
    assert r.status_code == 409
    assert r.json() == {"motivo": "faltan_eventos", "eventos_recibidos": 2}
    assert fila(cfg, sid)["estado"] == "grabando"
    # El celu pide GET /eventos, reenvía lo que falta y vuelve a finalizar.
    recibidos = set(client.get(f"/api/sesiones/{sid}/eventos").json()["ids"])
    assert len(recibidos) == 2
    evento(client, sid, "marca", marca="importante")
    evento(client, sid, "marca", marca="no_entendi")
    r = finalizar(client, sid, AUDIO, eventos_totales=4)
    assert r.status_code == 200 and r.json() == {"estado": "en_cola"}


def test_pausa_y_reanudar_cuentan_en_eventos_totales(client: TestClient):
    sid = crear(client)
    subir(client, sid, AUDIO)
    evento(client, sid, "nota", texto="a")
    evento(client, sid, "pausa", offset_ms=2000)
    r = finalizar(client, sid, AUDIO, eventos_totales=3)
    assert r.json() == {"motivo": "faltan_eventos", "eventos_recibidos": 2}
    evento(client, sid, "reanudar", offset_ms=2000)
    r = finalizar(client, sid, AUDIO, eventos_totales=3)
    assert r.status_code == 200, r.text


def test_mas_eventos_de_los_declarados_no_es_error(client: TestClient):
    sid = crear(client)
    subir(client, sid, AUDIO)
    evento(client, sid, "nota", texto="a")
    evento(client, sid, "nota", texto="b")
    assert finalizar(client, sid, AUDIO, eventos_totales=1).status_code == 200


def test_eventos_repetidos_no_suman(client: TestClient):
    sid = crear(client)
    subir(client, sid, AUDIO)
    eid = str(uuid.uuid4())
    for _ in range(3):
        client.post(f"/api/sesiones/{sid}/eventos", data={"id": eid, "tipo": "nota", "offset_ms": "1"})
    r = finalizar(client, sid, AUDIO, eventos_totales=2)
    assert r.json() == {"motivo": "faltan_eventos", "eventos_recibidos": 1}


# --- 409 hash_distinto -----------------------------------------------------------------------------

def test_hash_distinto_aparta_y_vuelve_a_cero(client: TestClient, cfg):
    sid = crear(client)
    corrupto = bytearray(AUDIO)
    corrupto[500] ^= 0xFF
    subir(client, sid, bytes(corrupto))
    r = finalizar(client, sid, AUDIO)
    assert r.status_code == 409
    assert r.json() == {"motivo": "hash_distinto", "bytes_recibidos": 0}

    carpeta = cfg.sesion_dir(sid)
    assert not (carpeta / "audio.ogg").exists()
    descartados = [p for p in carpeta.iterdir() if p.name.startswith("audio.descartado-")]
    assert len(descartados) == 1 and DESCARTADO_RE.match(descartados[0].name), descartados
    assert descartados[0].read_bytes() == bytes(corrupto)  # se aparta, no se borra
    assert client.get(f"/api/sesiones/{sid}/audio").json() == {"bytes_recibidos": 0}
    assert client.get(f"/api/sesiones/{sid}").json()["bytes_recibidos"] == 0
    with Base(cfg.db_path).conexion() as conn:
        a = conn.execute("SELECT bytes_recibidos FROM assets WHERE id = ?", (f"audio:{sid}",)).fetchone()
    assert a["bytes_recibidos"] == 0
    assert fila(cfg, sid)["estado"] == "grabando"
    assert jobs(cfg, sid) == []

    # Un PUT con el offset viejo ahora da 409 con 0.
    r = client.put(f"/api/sesiones/{sid}/audio?offset={len(AUDIO)}", content=b"x")
    assert r.status_code == 409 and r.json() == {"bytes_recibidos": 0}
    # Re-subida completa desde 0 y finalizar ok.
    subir(client, sid, AUDIO[:3000])
    subir(client, sid, AUDIO[3000:], offset=3000)
    r = finalizar(client, sid, AUDIO)
    assert r.status_code == 200 and r.json() == {"estado": "en_cola"}
    assert (carpeta / "audio.ogg").read_bytes() == AUDIO
    assert descartados[0].exists()


def test_hash_distinto_dos_veces_no_pisa_el_descarte(client: TestClient, cfg):
    sid = crear(client)
    for relleno in (b"A", b"B"):
        subir(client, sid, relleno * len(AUDIO))
        r = finalizar(client, sid, AUDIO)
        assert r.json() == {"motivo": "hash_distinto", "bytes_recibidos": 0}
    descartados = sorted(p for p in cfg.sesion_dir(sid).iterdir() if p.name.startswith("audio.descartado-"))
    assert len(descartados) == 2
    assert all(DESCARTADO_RE.match(p.name) for p in descartados)
    assert {p.read_bytes()[:1] for p in descartados} == {b"A", b"B"}


def test_mas_bytes_de_los_declarados_es_hash_distinto(client: TestClient, cfg):
    sid = crear(client)
    subir(client, sid, AUDIO + b"sobra")
    r = finalizar(client, sid, AUDIO)
    assert r.status_code == 409 and r.json() == {"motivo": "hash_distinto", "bytes_recibidos": 0}


def test_hash_sin_importar_mayusculas_ni_espacios(client: TestClient):
    sid = crear(client)
    subir(client, sid, AUDIO)
    r = finalizar(client, sid, AUDIO, sha256=f" {hashlib.sha256(AUDIO).hexdigest().upper()} ")
    assert r.status_code == 200, r.text


# --- 200 en_cola -----------------------------------------------------------------------------------

def test_finalizar_ok_guarda_campos_y_encola_verificar(client: TestClient, cfg):
    sid = crear(client)
    subir(client, sid, AUDIO)
    for tipo in ("nota", "marca", "foto", "pausa", "reanudar"):
        evento(client, sid, tipo, texto="t" if tipo == "nota" else None,
               marca="importante" if tipo == "marca" else None)
    r = finalizar(client, sid, AUDIO, duracion_ms=5_400_000, eventos_totales=5)
    assert r.status_code == 200
    assert r.json() == {"estado": "en_cola"}

    f = fila(cfg, sid)
    assert f["estado"] == "en_cola" and f["etapa"] is None and f["error"] is None
    assert f["sha256"] == hashlib.sha256(AUDIO).hexdigest()
    assert f["bytes_totales"] == len(AUDIO)
    assert f["duracion_ms"] == 5_400_000
    assert f["eventos_totales"] == 5
    assert f["finalizado"] and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$", f["finalizado"])
    with Base(cfg.db_path).conexion() as conn:
        a = conn.execute("SELECT * FROM assets WHERE id = ?", (f"audio:{sid}",)).fetchone()
    assert a["sha256"] == f["sha256"] and a["bytes_recibidos"] == len(AUDIO)

    js = jobs(cfg, sid)
    assert [(j.tipo, j.estado, j.intentos) for j in js] == [("verificar", "pendiente", 0)]
    assert js[0].clave == f"verificar:{sid}"
    s = client.get(f"/api/sesiones/{sid}").json()
    assert s["estado"] == "en_cola" and s["progreso"] == 0.0 and s["bytes_recibidos"] == len(AUDIO)


def test_finalizar_sin_duracion_ni_eventos(client: TestClient, cfg):
    sid = crear(client)
    subir(client, sid, AUDIO)
    r = client.post(f"/api/sesiones/{sid}/finalizar",
                    json={"sha256": hashlib.sha256(AUDIO).hexdigest(), "bytes_totales": len(AUDIO)})
    assert r.status_code == 200
    assert fila(cfg, sid)["duracion_ms"] is None


def test_finalizar_repetido_es_idempotente(client: TestClient, cfg):
    sid = crear(client)
    subir(client, sid, AUDIO)
    assert finalizar(client, sid, AUDIO).json() == {"estado": "en_cola"}
    # Se perdió la respuesta: el celu reintenta (incluso con otros datos) y no se duplica nada.
    for cuerpo in ({}, {"eventos_totales": 99}, {"bytes_totales": 1, "sha256": "f" * 64}):
        r = finalizar(client, sid, AUDIO, **cuerpo)
        assert r.status_code == 200 and r.json() == {"estado": "en_cola"}
    assert [j.tipo for j in jobs(cfg, sid)] == ["verificar"]
    assert (cfg.sesion_dir(sid) / "audio.ogg").read_bytes() == AUDIO


def test_finalizar_repetido_con_el_pipeline_andando(client: TestClient, cfg):
    sid = crear(client)
    subir(client, sid, AUDIO)
    finalizar(client, sid, AUDIO)
    with Base(cfg.db_path).conexion() as conn:
        conn.execute("UPDATE sesiones SET estado = 'procesando', etapa = 'transcribir' WHERE id = ?", (sid,))
    r = finalizar(client, sid, AUDIO)
    assert r.status_code == 200 and r.json() == {"estado": "procesando"}
    assert len(jobs(cfg, sid)) == 1


def test_finalizar_audio_vacio(client: TestClient, cfg):
    # 0 bytes con el hash de un archivo vacío: no puede quedar en hash_distinto para siempre.
    sid = crear(client)
    r = finalizar(client, sid, b"")
    assert r.status_code == 200, r.text
    assert r.json() == {"estado": "en_cola"}


def test_finalizar_audio_vacio_con_hash_malo(client: TestClient):
    sid = crear(client)
    r = finalizar(client, sid, b"", sha256="0" * 64)
    assert r.status_code == 409 and r.json() == {"motivo": "hash_distinto", "bytes_recibidos": 0}


@pytest.mark.parametrize("cuerpo", [
    {},
    {"bytes_totales": 10},
    {"sha256": "a" * 64},
    {"sha256": "a" * 64, "bytes_totales": -1},
    {"sha256": "a" * 64, "bytes_totales": 10, "duracion_ms": -5},
    {"sha256": "a" * 64, "bytes_totales": 10, "eventos_totales": -1},
    {"sha256": "a" * 64, "bytes_totales": "muchos"},
    {"sha256": "a" * 63, "bytes_totales": 10},
    {"sha256": "g" * 64, "bytes_totales": 10},
    {"sha256": "n4bQgYhMfWWaL+qgxVrQFaO/TxsrC4Is0V1sFbDwCgg=", "bytes_totales": 10},  # base64
])
def test_finalizar_cuerpo_invalido_422(client: TestClient, cfg, cuerpo: dict):
    sid = crear(client)
    subir(client, sid, AUDIO[:10])
    r = client.post(f"/api/sesiones/{sid}/finalizar", json=cuerpo)
    assert r.status_code == 422
    # No se tocó el audio ni el estado.
    assert (cfg.sesion_dir(sid) / "audio.ogg").read_bytes() == AUDIO[:10]
    assert fila(cfg, sid)["estado"] == "grabando"


def test_finalizar_concurrente_encola_una_vez(cfg, base):
    with base.conexion() as conn:
        sesiones.crear(cfg, conn, sesion_id="s-fin", materia_id=None, origen_materia=None,
                       tipo_clase=None, inicio=INICIO, dispositivo=None)
    assert sesiones.agregar_audio(cfg, base, "s-fin", 0, AUDIO).ok
    sha = hashlib.sha256(AUDIO).hexdigest()
    barrera = threading.Barrier(10)
    res: list = []

    def fin():
        barrera.wait()
        res.append(sesiones.finalizar(cfg, base, "s-fin", sha256=sha, bytes_totales=len(AUDIO),
                                      duracion_ms=1000, eventos_totales=0))

    hilos = [threading.Thread(target=fin) for _ in range(10)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)
    assert len(res) == 10 and all(r.ok and r.cuerpo == {"estado": "en_cola"} for r in res)
    with base.conexion() as conn:
        assert [j.tipo for j in cola.jobs_de_sesion(conn, "s-fin")] == ["verificar"]


def test_finalizar_mientras_se_sube(cfg, base):
    # PUT y finalizar comparten el lock de la sesión: finalizar ve el archivo entero o el parcial,
    # nunca uno a medio escribir.
    with base.conexion() as conn:
        sesiones.crear(cfg, conn, sesion_id="s-carrera", materia_id=None, origen_materia=None,
                       tipo_clase=None, inicio=INICIO, dispositivo=None)
    datos = AUDIO * 50
    sha = hashlib.sha256(datos).hexdigest()
    mitad = len(datos) // 2
    assert sesiones.agregar_audio(cfg, base, "s-carrera", 0, datos[:mitad]).ok
    resultados: list = []
    t = threading.Thread(target=lambda: resultados.append(
        sesiones.agregar_audio(cfg, base, "s-carrera", mitad, datos[mitad:])))
    t.start()
    r = sesiones.finalizar(cfg, base, "s-carrera", sha256=sha, bytes_totales=len(datos),
                           duracion_ms=1, eventos_totales=0)
    t.join(timeout=30)
    assert resultados and resultados[0].ok
    if r.ok:
        assert r.cuerpo == {"estado": "en_cola"}
    else:
        assert r.cuerpo == {"motivo": "faltan_bytes", "bytes_recibidos": mitad}
        r = sesiones.finalizar(cfg, base, "s-carrera", sha256=sha, bytes_totales=len(datos),
                               duracion_ms=1, eventos_totales=0)
        assert r.ok
    assert (cfg.sesion_dir("s-carrera") / "audio.ogg").read_bytes() == datos
