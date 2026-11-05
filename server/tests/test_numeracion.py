"""Número de clase, materias activas y elegir la materia (§5.1, v0.2): API y etapa apunte."""

from __future__ import annotations

import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

from classmer import cola, sesiones
from classmer.pipeline import actualizar_sesion
from classmer.pipeline.apunte import calcular_numero_clase
from classmer.util import ahora_iso

INICIO = "2026-10-15T14:00:05-03:00"


def crear(client: TestClient, **campos) -> str:
    sid = str(uuid.uuid4())
    cuerpo = {"id": sid, "materia_id": "prueba", "origen_materia": "calendario",
              "tipo_clase": "teorica", "inicio": INICIO, "dispositivo": "s23-ultra", **campos}
    r = client.post("/api/sesiones", json=cuerpo)
    assert r.status_code == 201, r.text
    return sid


def proximo(client: TestClient, materia_id: str = "prueba") -> int:
    return {m["id"]: m for m in client.get("/api/materias").json()}[materia_id]["proximo_numero"]


def poner(client: TestClient, sid: str, **campos) -> None:
    with client.app.state.base.conexion() as conn:
        actualizar_sesion(conn, sid, "America/Argentina/Buenos_Aires", **campos)


# --- proximo_numero y numero_clase ---------------------------------------------------------------

def test_numero_fijo_al_crear_y_proximo(client: TestClient):
    assert proximo(client) == 1
    a = crear(client)
    assert client.get(f"/api/sesiones/{a}").json()["numero_clase"] == 1
    assert proximo(client) == 2  # la que se está grabando ya cuenta
    b = crear(client, numero_clase=10)  # el override de la app manda
    assert client.get(f"/api/sesiones/{b}").json()["numero_clase"] == 10
    assert proximo(client) == 3  # inicial + clases, sin mirar el override
    assert [s["numero_clase"] for s in client.get("/api/sesiones").json()] == [10, 1]
    assert proximo(client, "otra") == 1


def test_las_clases_en_error_no_cuentan(client: TestClient):
    a, b, c = crear(client), crear(client), crear(client)
    poner(client, a, estado="error")  # prueba fallida, sin apunte: no cuenta
    poner(client, b, estado="error")
    with client.app.state.base.conexion() as conn:  # b tuvo un apunte que compiló: es una clase
        conn.execute("INSERT INTO apuntes (sesion_id, version, actual, compilo, creado) VALUES (?, 1, 1, 1, ?)",
                     (b, ahora_iso()))
    poner(client, c, estado="lista")
    assert proximo(client) == 3


def test_auto_sin_numero_hasta_tener_materia(client: TestClient):
    sid = crear(client, materia_id=None, origen_materia="auto")
    assert client.get(f"/api/sesiones/{sid}").json()["numero_clase"] is None
    assert proximo(client) == 1


def test_numero_clase_invalido(client: TestClient):
    r = client.post("/api/sesiones", json={"id": str(uuid.uuid4()), "materia_id": "prueba",
                                           "inicio": INICIO, "numero_clase": -1})
    assert r.status_code == 422


def test_etapa_apunte_respeta_el_numero_fijo(ctx):
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id="fija", materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio=INICIO, dispositivo="t", numero_clase=12)
        sesiones.crear(ctx.cfg, conn, sesion_id="anterior", materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio="2026-10-01T10:00:00-03:00", dispositivo="t")
        fila = sesiones.fila_sesion(conn, "fija")
        assert calcular_numero_clase(conn, ctx.cfg.materia("prueba"), fila) == 12


# --- materias activas ----------------------------------------------------------------------------

def test_materias_inactivas_no_se_listan_pero_se_aceptan(home, client: TestClient):
    datos = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    datos["materias"][1]["activa"] = False
    (home / "config.yaml").write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")
    assert client.app.state.config_viva.recargar()
    assert [m["id"] for m in client.get("/api/materias").json()] == ["prueba"]
    # Una cache vieja del celu con la materia inactiva no hace perder la clase.
    sid = crear(client, materia_id="otra")
    assert client.get(f"/api/sesiones/{sid}").json()["materia_id"] == "otra"
    with client.app.state.base.conexion() as conn:
        activas = dict(conn.execute("SELECT id, activa FROM materias").fetchall())
    assert activas == {"prueba": 1, "otra": 0}


# --- PATCH /api/materias/{id} --------------------------------------------------------------------

def test_patch_proximo_numero_sin_reiniciar(home, client: TestClient):
    crear(client)
    crear(client)
    r = client.patch("/api/materias/prueba", json={"proximo_numero": 8})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "prueba", "nombre": "Materia de Prueba",
                        "calendario": {"calendario": None, "titulo_regex": "Prueba"},
                        "tipos_clase": ["teorica", "practica"], "proximo_numero": 8}
    guardada = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert guardada["materias"][0]["numero_clase_inicial"] == 6  # 8 − 2 clases
    assert proximo(client) == 8
    sid = crear(client)
    assert client.get(f"/api/sesiones/{sid}").json()["numero_clase"] == 8
    assert proximo(client) == 9


def test_patch_numero_clase_inicial(client: TestClient):
    crear(client)
    r = client.patch("/api/materias/otra", json={"numero_clase_inicial": 5})
    assert r.status_code == 200
    assert r.json()["proximo_numero"] == 5
    r = client.patch("/api/materias/prueba", json={"numero_clase_inicial": 5})
    assert r.json()["proximo_numero"] == 6


@pytest.mark.parametrize("cuerpo", [{}, {"proximo_numero": 3, "numero_clase_inicial": 1},
                                    {"proximo_numero": 0}, {"proximo_numero": "x"}])
def test_patch_invalido_422(client: TestClient, cuerpo):
    assert client.patch("/api/materias/prueba", json=cuerpo).status_code == 422


def test_patch_materia_inexistente_404(client: TestClient):
    r = client.patch("/api/materias/no-existe", json={"proximo_numero": 3})
    assert r.status_code == 404
    assert r.json() == {"detail": "materia no encontrada"}


# --- POST /api/sesiones/{id}/materia -------------------------------------------------------------

def test_elegir_materia_en_esperando_usuario(client: TestClient):
    crear(client)  # una clase de prueba previa: la elegida pasa a ser la 2
    sid = crear(client, materia_id=None, origen_materia="auto")
    poner(client, sid, estado="esperando_usuario", etapa="clasificar", error="No pude determinar la materia")
    r = client.post(f"/api/sesiones/{sid}/materia", json={"materia_id": "prueba"})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["id"] == sid and cuerpo["materia_id"] == "prueba"
    assert cuerpo["estado"] == "en_cola" and cuerpo["etapa"] == "revisar_transcript" and cuerpo["error"] is None
    assert cuerpo["numero_clase"] == 2
    with client.app.state.base.conexion() as conn:
        assert [(j.tipo, j.estado) for j in cola.jobs_de_sesion(conn, sid)] == [("revisar_transcript", "pendiente")]
        assert sesiones.fila_sesion(conn, sid)["origen_materia"] == "manual"


def test_elegir_materia_mientras_graba(client: TestClient):
    sid = crear(client)  # prueba, clase 1
    crear(client, materia_id="otra")
    r = client.post(f"/api/sesiones/{sid}/materia", json={"materia_id": "otra"})
    assert r.status_code == 200
    assert r.json()["estado"] == "grabando"
    assert r.json()["numero_clase"] == 2  # renumerada en la materia nueva
    with client.app.state.base.conexion() as conn:
        assert cola.jobs_de_sesion(conn, sid) == []


@pytest.mark.parametrize("estado,etapa", [("lista", "publicar"), ("error", "apunte"), ("procesando", "apunte")])
def test_elegir_materia_409_si_el_apunte_ya_empezo(client: TestClient, estado, etapa):
    sid = crear(client)
    poner(client, sid, estado=estado, etapa=etapa)
    r = client.post(f"/api/sesiones/{sid}/materia", json={"materia_id": "otra"})
    assert r.status_code == 409
    assert "detail" in r.json()


def test_elegir_materia_409_con_job_de_apunte_corriendo(client: TestClient):
    sid = crear(client)
    poner(client, sid, estado="en_cola", etapa="apunte")
    with client.app.state.base.conexion() as conn:
        cola.encolar(conn, "apunte", sid)
        assert cola.tomar(conn) is not None
    assert client.post(f"/api/sesiones/{sid}/materia", json={"materia_id": "otra"}).status_code == 409


def test_elegir_materia_procesando_antes_del_apunte(client: TestClient):
    sid = crear(client, materia_id=None, origen_materia="auto")
    poner(client, sid, estado="procesando", etapa="transcribir")
    r = client.post(f"/api/sesiones/{sid}/materia", json={"materia_id": "prueba"})
    assert r.status_code == 200 and r.json()["estado"] == "procesando"


def test_elegir_materia_errores(client: TestClient):
    sid = crear(client)
    r = client.post(f"/api/sesiones/{sid}/materia", json={"materia_id": "no-existe"})
    assert r.status_code == 422
    r = client.post("/api/sesiones/0192f7c2-0000-7000-8000-00000000abcd/materia", json={"materia_id": "prueba"})
    assert r.status_code == 404 and r.json() == {"detail": "sesión no encontrada"}
    assert client.post(f"/api/sesiones/{sid}/materia", json={}).status_code == 422
