"""Contrato de la API v1 (§5.1 de ARCHITECTURE.md), ruta por ruta, con TestClient."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from classmer import sesiones
from conftest import TOKEN

INICIO = "2026-10-15T14:00:05-03:00"
CLAVES_CONTRATO = {"id", "materia_id", "estado", "etapa", "progreso", "bytes_recibidos", "pdf", "error"}
# Campos informativos acordados con la app (§5.1; los clientes ignoran los desconocidos).
CLAVES_INFORMATIVAS = {"inicio", "tipo_clase", "duracion_ms", "numero_clase", "titulo", "adjuntos",
                       "adjuntos_sin_usar", "importada", "espera", "dudas"}
CLAVES_SESION = CLAVES_CONTRATO | CLAVES_INFORMATIVAS


def nuevo_id() -> str:
    return str(uuid.uuid4())


def crear(client: TestClient, sid: str | None = None, **campos) -> tuple[str, object]:
    sid = nuevo_id() if sid is None else sid
    cuerpo = {"id": sid, "materia_id": "prueba", "origen_materia": "calendario",
              "tipo_clase": "teorica", "inicio": INICIO, "dispositivo": "s23-ultra"}
    cuerpo.update(campos)
    return sid, client.post("/api/sesiones", json=cuerpo)


def fila(cfg, sid: str) -> dict:
    from classmer.db import Base

    with Base(cfg.db_path).conexion() as conn:
        f = conn.execute("SELECT * FROM sesiones WHERE id = ?", (sid,)).fetchone()
        return dict(f) if f else {}


# --- 401 en todas las rutas ----------------------------------------------------------------------

SID_X = "0192f7c2-0000-7000-8000-00000000abcd"
RUTAS = [
    ("GET", "/api/health"),
    ("GET", "/api/materias"),
    ("GET", "/api/sesiones"),
    ("GET", "/api/sesiones?limite=5"),
    ("POST", "/api/sesiones"),
    ("GET", f"/api/sesiones/{SID_X}"),
    ("GET", f"/api/sesiones/{SID_X}/audio"),
    ("PUT", f"/api/sesiones/{SID_X}/audio?offset=0"),
    ("POST", f"/api/sesiones/{SID_X}/eventos"),
    ("GET", f"/api/sesiones/{SID_X}/eventos"),
    ("POST", f"/api/sesiones/{SID_X}/finalizar"),
    ("GET", f"/api/sesiones/{SID_X}/apunte.pdf"),
    ("POST", "/api/importar"),
    ("POST", f"/api/sesiones/{SID_X}/reprocesar"),
    ("PATCH", "/api/materias/prueba"),
    ("POST", f"/api/sesiones/{SID_X}/materia"),
    ("POST", f"/api/sesiones/{SID_X}/adjuntos"),
    ("POST", f"/api/sesiones/{SID_X}/regenerar"),
]
ENCABEZADOS_MALOS = {
    "sin_encabezado": None,
    "vacio": "",
    "token_malo": "Bearer otro-token",
    "sin_bearer": TOKEN,
    "basic": f"Basic {TOKEN}",
    "bearer_sin_token": "Bearer",
    "bearer_espacio": "Bearer ",
    "token_con_prefijo": f"Bearer x{TOKEN}",
    "token_cortado": f"Bearer {TOKEN[:-1]}",
}


@pytest.fixture
def anonimo(client: TestClient) -> TestClient:
    """Cliente sobre la misma app pero sin el encabezado de autorización."""
    c = TestClient(client.app)
    return c


@pytest.mark.parametrize("metodo,ruta", RUTAS)
@pytest.mark.parametrize("caso", list(ENCABEZADOS_MALOS))
def test_401_en_todas_las_rutas(anonimo: TestClient, metodo: str, ruta: str, caso: str):
    valor = ENCABEZADOS_MALOS[caso]
    headers = {} if valor is None else {"Authorization": valor}
    r = anonimo.request(metodo, ruta, headers=headers)
    assert r.status_code == 401, (metodo, ruta, caso, r.text)
    assert r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("metodo,ruta", RUTAS)
def test_401_gana_aunque_el_cuerpo_sea_invalido(anonimo: TestClient, metodo: str, ruta: str):
    # FastAPI valida el JSON antes que las dependencias: el middleware tiene que cortar antes.
    r = anonimo.request(metodo, ruta, content=b"{esto no es json",
                        headers={"Authorization": "Bearer malo", "Content-Type": "application/json"})
    assert r.status_code == 401, (metodo, ruta, r.text)


def test_401_en_ruta_inexistente_bajo_api(anonimo: TestClient):
    assert anonimo.get("/api/no-existe").status_code == 401


def test_token_valido_acepta_bearer_sin_importar_mayusculas(anonimo: TestClient):
    for prefijo in ("Bearer", "bearer", "BEARER"):
        r = anonimo.get("/api/health", headers={"Authorization": f"{prefijo} {TOKEN}"})
        assert r.status_code == 200, prefijo


def test_sin_token_configurado_todo_es_401(cfg):
    from classmer.api.app import crear_app

    cfg.token = None
    with TestClient(crear_app(cfg, iniciar_worker=False)) as c:
        assert c.get("/api/health", headers={"Authorization": "Bearer "}).status_code == 401
        assert c.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401


# --- health y materias -------------------------------------------------------------------------

def test_health(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "0.1.0"}
    assert r.headers["content-type"].startswith("application/json")


def test_materias_forma_exacta(client: TestClient):
    r = client.get("/api/materias")
    assert r.status_code == 200
    assert r.json() == [
        {"id": "prueba", "nombre": "Materia de Prueba",
         "calendario": {"calendario": None, "titulo_regex": "Prueba"},
         "tipos_clase": ["teorica", "practica"], "proximo_numero": 1},
        {"id": "otra", "nombre": "Otra Materia",
         "calendario": {"calendario": None, "titulo_regex": None},
         "tipos_clase": ["teorica", "practica"], "proximo_numero": 1},
    ]


def test_materias_utf8_y_reglas_de_calendario(home: Path, tmp_path: Path):
    import yaml

    from classmer.api.app import crear_app
    from classmer.config import cargar_config

    config = {"materias": [{"id": "analisis", "nombre": "Análisis Matemático II",
                            "carpeta": str(tmp_path / "x"),
                            "calendario": {"calendario": "Facu", "titulo_regex": "An[aá]lisis Matem[aá]tico"},
                            "tipos_clase": ["teorica"]}]}
    (home / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    with TestClient(crear_app(cargar_config(home), iniciar_worker=False)) as c:
        r = c.get("/api/materias", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")  # JSON es UTF-8 por definición
    assert "Matemático".encode("utf-8") in r.content
    assert json.loads(r.content.decode("utf-8")) == [
        {"id": "analisis", "nombre": "Análisis Matemático II",
         "calendario": {"calendario": "Facu", "titulo_regex": "An[aá]lisis Matem[aá]tico"},
         "tipos_clase": ["teorica"], "proximo_numero": 1}]


# --- POST /api/sesiones ------------------------------------------------------------------------

def test_crear_201_y_repetido_200(client: TestClient, cfg):
    sid, r = crear(client)
    assert r.status_code == 201, r.text
    assert r.json() == {"id": sid, "estado": "grabando"}
    _, r = crear(client, sid)
    assert r.status_code == 200
    assert r.json() == {"id": sid, "estado": "grabando"}
    # Repetido con otros datos: no pisa lo guardado.
    _, r = crear(client, sid, materia_id="otra", tipo_clase="practica")
    assert r.status_code == 200
    f = fila(cfg, sid)
    assert f["materia_id"] == "prueba" and f["tipo_clase"] == "teorica"
    assert f["origen_materia"] == "calendario" and f["dispositivo"] == "s23-ultra"
    # Carpeta, sesion.json y el asset del audio.
    carpeta = cfg.sesion_dir(sid)
    assert (carpeta / "fotos").is_dir()
    guardado = json.loads((carpeta / "sesion.json").read_text(encoding="utf-8"))
    assert guardado["id"] == sid and guardado["estado"] == "grabando"
    from classmer.db import Base

    with Base(cfg.db_path).conexion() as conn:
        a = conn.execute("SELECT * FROM assets WHERE id = ?", (f"audio:{sid}",)).fetchone()
    assert a["tipo"] == "audio" and a["ruta"] == "audio.ogg" and a["bytes_recibidos"] == 0


def test_crear_repetido_con_datos_invalidos_igual_da_200(client: TestClient):
    sid, r = crear(client)
    assert r.status_code == 201
    _, r = crear(client, sid, materia_id="no-existe")
    assert r.status_code == 200


def test_crear_materia_inexistente_422(client: TestClient, cfg):
    sid, r = crear(client, materia_id="no-existe")
    assert r.status_code == 422
    assert fila(cfg, sid) == {}
    assert not cfg.sesion_dir(sid).exists()


def test_crear_materia_null_auto(client: TestClient, cfg):
    sid, r = crear(client, materia_id=None, origen_materia="auto", tipo_clase=None)
    assert r.status_code == 201, r.text
    f = fila(cfg, sid)
    assert f["materia_id"] is None and f["origen_materia"] == "auto" and f["tipo_clase"] is None
    assert client.get(f"/api/sesiones/{sid}").json()["materia_id"] is None


def test_crear_materia_null_sin_origen_queda_auto(client: TestClient, cfg):
    sid = nuevo_id()
    r = client.post("/api/sesiones", json={"id": sid, "materia_id": None, "inicio": INICIO})
    assert r.status_code == 201, r.text
    f = fila(cfg, sid)
    assert f["origen_materia"] == "auto" and f["dispositivo"] is None


def test_crear_campos_minimos(client: TestClient, cfg):
    sid = nuevo_id()
    r = client.post("/api/sesiones", json={"id": sid, "inicio": INICIO})
    assert r.status_code == 201, r.text
    assert fila(cfg, sid)["materia_id"] is None


@pytest.mark.parametrize("tipo", ["teorica", "practica"])
def test_crear_tipos_validos(client: TestClient, tipo: str):
    _, r = crear(client, tipo_clase=tipo)
    assert r.status_code == 201


@pytest.mark.parametrize("tipo", ["teórica", "Teorica", "laboratorio", ""])
def test_crear_tipo_clase_invalido_422(client: TestClient, tipo: str):
    _, r = crear(client, tipo_clase=tipo)
    assert r.status_code == 422, r.text


def test_crear_tipo_clase_limitado_a_la_materia(home: Path, tmp_path: Path):
    import yaml

    from classmer.api.app import crear_app
    from classmer.config import cargar_config

    config = {"materias": [{"id": "solo-teo", "nombre": "Solo teóricas", "tipos_clase": ["teorica"]}]}
    (home / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    with TestClient(crear_app(cargar_config(home), iniciar_worker=False)) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        _, r = crear(c, materia_id="solo-teo", tipo_clase="practica")
        assert r.status_code == 422
        _, r = crear(c, materia_id="solo-teo", tipo_clase="teorica")
        assert r.status_code == 201


@pytest.mark.parametrize("origen", ["calendario", "manual", "auto"])
def test_crear_origenes_validos(client: TestClient, origen: str):
    _, r = crear(client, origen_materia=origen)
    assert r.status_code == 201


def test_crear_origen_invalido_422(client: TestClient):
    _, r = crear(client, origen_materia="gps")
    assert r.status_code == 422


@pytest.mark.parametrize("inicio,esperado", [
    ("2026-10-15T14:00:05-03:00", "2026-10-15T14:00:05-03:00"),
    ("2026-10-15T17:00:05Z", "2026-10-15T14:00:05-03:00"),
    ("2026-10-15T17:00:05+00:00", "2026-10-15T14:00:05-03:00"),
    ("2026-10-15T14:00:05.789-03:00", "2026-10-15T14:00:05-03:00"),
    # Sin zona: se asume la de Classmer (America/Argentina/Buenos_Aires).
    ("2026-10-15T14:00:05", "2026-10-15T14:00:05-03:00"),
])
def test_crear_inicio_con_y_sin_zona(client: TestClient, cfg, inicio: str, esperado: str):
    sid, r = crear(client, inicio=inicio)
    assert r.status_code == 201, r.text
    assert fila(cfg, sid)["inicio"] == esperado


@pytest.mark.parametrize("inicio", ["", "ayer", "2026-13-40T99:00:00", "15/10/2026 14:00"])
def test_crear_inicio_invalido_422(client: TestClient, cfg, inicio: str):
    sid, r = crear(client, inicio=inicio)
    assert r.status_code == 422
    assert fila(cfg, sid) == {}


@pytest.mark.parametrize("sid", ["", "a/b", "..", "a b", "x" * 65, "abc\n", "-empieza-con-guion", "ñandú"])
def test_crear_id_invalido_422(client: TestClient, sid: str):
    _, r = crear(client, sid)
    assert r.status_code == 422, (sid, r.text)


def test_crear_faltan_campos_422(client: TestClient):
    assert client.post("/api/sesiones", json={"id": nuevo_id()}).status_code == 422
    assert client.post("/api/sesiones", json={"inicio": INICIO}).status_code == 422
    assert client.post("/api/sesiones", content=b"{roto", headers={"Content-Type": "application/json"}).status_code == 422


# --- GET /api/sesiones/{id} ---------------------------------------------------------------------

def test_ver_sesion_claves_exactas(client: TestClient):
    sid, _ = crear(client)
    r = client.get(f"/api/sesiones/{sid}")
    assert r.status_code == 200
    cuerpo = r.json()
    assert {k: cuerpo[k] for k in CLAVES_CONTRATO} == {
        "id": sid, "materia_id": "prueba", "estado": "grabando", "etapa": None,
        "progreso": 0.0, "bytes_recibidos": 0, "pdf": None, "error": None}
    assert set(cuerpo) == CLAVES_SESION
    assert cuerpo["inicio"].endswith("-03:00")
    assert cuerpo["duracion_ms"] is None and cuerpo["titulo"] is None


def test_ver_sesion_refleja_bytes(client: TestClient):
    sid, _ = crear(client)
    client.put(f"/api/sesiones/{sid}/audio?offset=0", content=b"OggS" + b"\x00" * 96)
    assert client.get(f"/api/sesiones/{sid}").json()["bytes_recibidos"] == 100


@pytest.mark.parametrize("metodo,sufijo", [
    ("GET", ""), ("GET", "/audio"), ("PUT", "/audio?offset=0"), ("GET", "/eventos"),
    ("GET", "/apunte.pdf"),
])
def test_404_sesion_inexistente(client: TestClient, metodo: str, sufijo: str):
    r = client.request(metodo, f"/api/sesiones/{nuevo_id()}{sufijo}", content=b"abc" if metodo == "PUT" else None)
    assert r.status_code == 404, r.text


def test_404_sesion_inexistente_eventos_y_finalizar(client: TestClient):
    sid = nuevo_id()
    r = client.post(f"/api/sesiones/{sid}/eventos", data={"id": nuevo_id(), "tipo": "nota", "offset_ms": "1"})
    assert r.status_code == 404
    r = client.post(f"/api/sesiones/{sid}/finalizar",
                    json={"sha256": "0" * 64, "bytes_totales": 0, "duracion_ms": 0, "eventos_totales": 0})
    assert r.status_code == 404


# --- GET /api/sesiones?limite=N -------------------------------------------------------------------

def test_listar_orden_y_limite(client: TestClient):
    # Se crean desordenadas; el orden es por inicio, de la más nueva a la más vieja.
    inicios = {
        "s-martes": "2026-10-13T14:00:00-03:00",
        "s-jueves": "2026-10-15T17:00:00Z",          # 14:00 -03:00
        "s-lunes": "2026-10-12T09:00:00-03:00",
        "s-jueves-tarde": "2026-10-15T18:30:00-03:00",
        "s-miercoles": "2026-10-14T10:00:00",       # sin zona
    }
    for sid, inicio in inicios.items():
        _, r = crear(client, sid, inicio=inicio)
        assert r.status_code == 201
    orden = ["s-jueves-tarde", "s-jueves", "s-miercoles", "s-martes", "s-lunes"]

    r = client.get("/api/sesiones")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == orden

    r = client.get("/api/sesiones?limite=2")
    assert [s["id"] for s in r.json()] == orden[:2]
    assert [s["id"] for s in client.get("/api/sesiones?limite=1").json()] == orden[:1]

    # Objetos iguales a los de GET /api/sesiones/{id}.
    for s in client.get("/api/sesiones?limite=20").json():
        assert set(s) == CLAVES_SESION
        assert s == client.get(f"/api/sesiones/{s['id']}").json()


def test_listar_vacio_y_limites_invalidos(client: TestClient):
    assert client.get("/api/sesiones").json() == []
    assert client.get("/api/sesiones?limite=0").status_code == 422
    assert client.get("/api/sesiones?limite=-1").status_code == 422
    assert client.get("/api/sesiones?limite=abc").status_code == 422


def test_listar_limite_por_defecto_20(client: TestClient):
    for i in range(23):
        crear(client, f"s-{i:02d}", inicio=f"2026-10-01T{i:02d}:00:00-03:00")
    ids = [s["id"] for s in client.get("/api/sesiones").json()]
    assert len(ids) == 20
    assert ids[0] == "s-22" and ids[-1] == "s-03"


# --- eventos -----------------------------------------------------------------------------------

def _evento(client: TestClient, sid: str, **campos):
    archivo = campos.pop("archivo", None)
    files = {"archivo": archivo} if archivo is not None else None
    return client.post(f"/api/sesiones/{sid}/eventos", data=campos, files=files)


def test_eventos_todos_los_tipos_201_y_200(client: TestClient, cfg):
    sid, _ = crear(client)
    foto = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 10
    ids = {k: nuevo_id() for k in ("nota", "imp", "noent", "pausa", "reanudar", "foto")}
    pedidos = [
        dict(id=ids["nota"], tipo="nota", offset_ms="1000", creado="2026-10-15T14:00:06-03:00", texto="ver ñ y tildes"),
        dict(id=ids["imp"], tipo="marca", offset_ms="2000", marca="importante"),
        dict(id=ids["noent"], tipo="marca", offset_ms="3000", marca="no_entendi"),
        dict(id=ids["pausa"], tipo="pausa", offset_ms="4000"),
        dict(id=ids["reanudar"], tipo="reanudar", offset_ms="4000"),
        dict(id=ids["foto"], tipo="foto", offset_ms="5000",
             archivo=(f"{ids['foto']}.jpg", foto, "image/jpeg")),
    ]
    for p in pedidos:
        r = _evento(client, sid, **dict(p))
        assert r.status_code == 201, (p["tipo"], r.text)
    for p in pedidos:
        r = _evento(client, sid, **dict(p))
        assert r.status_code == 200, (p["tipo"], r.text)

    carpeta = cfg.sesion_dir(sid)
    assert (carpeta / "fotos" / f"{ids['foto']}.jpg").read_bytes() == foto
    lineas = [json.loads(x) for x in (carpeta / "eventos.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["id"] for x in lineas] == [p["id"] for p in pedidos]  # sin duplicados
    assert lineas[0]["texto"] == "ver ñ y tildes" and lineas[0]["offset_ms"] == 1000
    assert lineas[1]["marca"] == "importante" and lineas[2]["marca"] == "no_entendi"
    assert lineas[5]["ruta"] == f"fotos/{ids['foto']}.jpg"

    from classmer.db import Base

    with Base(cfg.db_path).conexion() as conn:
        filas = {f["id"]: f for f in conn.execute("SELECT * FROM assets WHERE sesion_id = ?", (sid,))}
    assert filas[ids["foto"]]["sha256"] == hashlib.sha256(foto).hexdigest()
    assert filas[ids["foto"]]["tipo"] == "foto" and filas[ids["foto"]]["offset_ms"] == 5000
    assert json.loads(filas[ids["nota"]]["datos"])["creado"] == "2026-10-15T14:00:06-03:00"
    assert filas[ids["pausa"]]["tipo"] == "pausa" and filas[ids["reanudar"]]["tipo"] == "reanudar"

    r = client.get(f"/api/sesiones/{sid}/eventos")
    assert r.status_code == 200
    assert set(r.json()) == {"ids"}
    assert sorted(r.json()["ids"]) == sorted(ids.values())


def test_eventos_get_vacio(client: TestClient):
    sid, _ = crear(client)
    assert client.get(f"/api/sesiones/{sid}/eventos").json() == {"ids": []}


def test_eventos_get_no_incluye_el_audio_ni_otras_sesiones(client: TestClient):
    a, _ = crear(client)
    b, _ = crear(client)
    client.put(f"/api/sesiones/{a}/audio?offset=0", content=b"OggS")
    e = nuevo_id()
    assert _evento(client, b, id=e, tipo="nota", offset_ms="1").status_code == 201
    assert client.get(f"/api/sesiones/{a}/eventos").json() == {"ids": []}
    assert client.get(f"/api/sesiones/{b}/eventos").json() == {"ids": [e]}


def test_foto_sin_archivo_422(client: TestClient, cfg):
    sid, _ = crear(client)
    e = nuevo_id()
    r = _evento(client, sid, id=e, tipo="foto", offset_ms="1000")
    assert r.status_code == 422
    # Campo "archivo" como texto (sin archivo): tampoco.
    r = client.post(f"/api/sesiones/{sid}/eventos", data={"id": e, "tipo": "foto", "offset_ms": "1", "archivo": "x"})
    assert r.status_code == 422
    assert client.get(f"/api/sesiones/{sid}/eventos").json() == {"ids": []}


def test_foto_sin_offset_se_acepta(client: TestClient):
    # Fotos sin hora (p. ej., importadas) van al final de la línea de tiempo.
    sid, _ = crear(client)
    r = _evento(client, sid, id=nuevo_id(), tipo="foto", archivo=("a.jpg", b"\xff\xd8\xff", "image/jpeg"))
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("campos", [
    {"tipo": "video", "offset_ms": "1"},
    {"tipo": "", "offset_ms": "1"},
    {"tipo": "NOTA", "offset_ms": "1"},
    {"offset_ms": "1"},                                  # sin tipo
    {"tipo": "nota"},                                    # sin offset_ms
    {"tipo": "nota", "offset_ms": "abc"},
    {"tipo": "nota", "offset_ms": "-5"},
    {"tipo": "nota", "offset_ms": "1.5"},
])
def test_eventos_datos_invalidos_422(client: TestClient, campos: dict):
    sid, _ = crear(client)
    r = _evento(client, sid, id=nuevo_id(), **campos)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("eid", ["", "a/b", "abc\n", "x" * 65])
def test_eventos_id_invalido_422(client: TestClient, eid: str):
    sid, _ = crear(client)
    r = _evento(client, sid, id=eid, tipo="nota", offset_ms="1")
    assert r.status_code == 422


def test_eventos_sin_id_422(client: TestClient):
    sid, _ = crear(client)
    r = client.post(f"/api/sesiones/{sid}/eventos", data={"tipo": "nota", "offset_ms": "1"})
    assert r.status_code == 422


def test_foto_grande_413(client: TestClient, cfg, monkeypatch: pytest.MonkeyPatch):
    assert sesiones.MAX_FOTO == 50 * 1024 * 1024  # el del contrato
    monkeypatch.setattr(sesiones, "MAX_FOTO", 4096)
    sid, _ = crear(client)
    e = nuevo_id()
    r = _evento(client, sid, id=e, tipo="foto", offset_ms="1", archivo=(f"{e}.jpg", b"\xff" * 4097, "image/jpeg"))
    assert r.status_code == 413, r.text
    fotos = cfg.sesion_dir(sid) / "fotos"
    assert list(fotos.iterdir()) == []  # ni la foto ni el temporal
    assert client.get(f"/api/sesiones/{sid}/eventos").json() == {"ids": []}
    # Justo en el límite entra, y el mismo id se puede reintentar.
    r = _evento(client, sid, id=e, tipo="foto", offset_ms="1", archivo=(f"{e}.jpg", b"\xff" * 4096, "image/jpeg"))
    assert r.status_code == 201
    assert (fotos / f"{e}.jpg").stat().st_size == 4096


def test_foto_extension_rara_se_guarda_como_jpg(client: TestClient, cfg):
    sid, _ = crear(client)
    e = nuevo_id()
    r = _evento(client, sid, id=e, tipo="foto", offset_ms="1", archivo=("..\\..\\malo.exe", b"\xff\xd8", "image/jpeg"))
    assert r.status_code == 201
    assert [p.name for p in (cfg.sesion_dir(sid) / "fotos").iterdir()] == [f"{e}.jpg"]


def test_evento_con_id_de_otra_sesion_422(client: TestClient):
    # Un 200 sin guardarlo dejaría a B trabada en faltan_eventos; con 422 la app lo descarta.
    a, _ = crear(client)
    b, _ = crear(client)
    e = nuevo_id()
    assert _evento(client, a, id=e, tipo="nota", offset_ms="1").status_code == 201
    r = _evento(client, b, id=e, tipo="nota", offset_ms="1")
    assert r.status_code == 422 and "otra sesión" in r.json()["detail"]
    assert _evento(client, a, id=e, tipo="nota", offset_ms="1").status_code == 200  # misma sesión: idempotente
    assert client.get(f"/api/sesiones/{b}/eventos").json() == {"ids": []}
    assert client.get(f"/api/sesiones/{a}/eventos").json() == {"ids": [e]}


# --- errores del multipart -----------------------------------------------------------------------

def test_multipart_mal_armado_422(client: TestClient):
    sid, _ = crear(client)
    r = client.post(f"/api/sesiones/{sid}/eventos", content=b"--x\r\nbasura",
                    headers={"Content-Type": "multipart/form-data"})  # sin boundary
    assert r.status_code == 422 and r.json()["detail"].startswith("multipart inválido")


def test_multipart_error_de_disco_503_reintentable(client: TestClient, cfg, monkeypatch: pytest.MonkeyPatch):
    # Una foto de más de 1 MB pasa a un temporal en disco; si eso falla (disco lleno) no es un
    # error de los datos: 422 haría que la app tire la foto para siempre.
    import errno
    import tempfile

    from starlette import formparsers

    class SinEspacio(tempfile.SpooledTemporaryFile):
        def write(self, s):  # type: ignore[override]
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", SinEspacio)
    sid, _ = crear(client)
    e = nuevo_id()
    r = _evento(client, sid, id=e, tipo="foto", offset_ms="1", archivo=(f"{e}.jpg", b"\xff" * 4096, "image/jpeg"))
    assert r.status_code == 503 and r.json() == {"detail": "error de disco en el server"}
    monkeypatch.undo()
    r = _evento(client, sid, id=e, tipo="foto", offset_ms="1", archivo=(f"{e}.jpg", b"\xff" * 4096, "image/jpeg"))
    assert r.status_code == 201  # el reintento entra


def test_multipart_cliente_cortado_no_es_422(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from starlette.requests import ClientDisconnect, Request

    async def cortado(self, **_kw):
        raise ClientDisconnect()

    monkeypatch.setattr(Request, "_get_form", cortado)
    sid, _ = crear(client)
    r = _evento(client, sid, id=nuevo_id(), tipo="nota", offset_ms="1")
    assert r.status_code == 408


# --- POST /api/importar: nombres de fotos ----------------------------------------------------------

@pytest.fixture
def importar_falso(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[str, bytes]]]:
    """Reemplaza importar_archivos (sin ffmpeg): anota nombre y contenido de las fotos que recibe."""
    from classmer import importador

    llamadas: list[list[tuple[str, bytes]]] = []

    def falso(ctx, audio, *, fotos, **_kw):
        assert audio.is_file()
        llamadas.append([(f.name, f.read_bytes()) for f in fotos])
        return "sesion-importada"

    monkeypatch.setattr(importador, "importar_archivos", falso)
    return llamadas


def test_importar_fotos_con_nombres_invalidos_en_windows(client: TestClient, importar_falso):
    nombres = ["a?b.jpg", "x|y.JPG", "..", "...", "normal.jpg", "sin_extension", "g<a>t.jp?g"]
    archivos = [("audio", ("clase.wav", b"RIFF0000WAVE", "audio/wav"))]
    archivos += [("fotos[]", (n, f"foto {i}".encode(), "image/jpeg")) for i, n in enumerate(nombres)]
    r = client.post("/api/importar", files=archivos, data={"materia_id": "prueba"})
    assert r.status_code == 201, r.text
    [fotos] = importar_falso
    assert [n for n, _ in fotos] == ["ab.jpg", "xy.JPG", "foto_002.jpg", "foto_003.jpg",
                                     "normal.jpg", "sin_extension.jpg", "gat.jpg"]
    assert [c for _, c in fotos] == [f"foto {i}".encode() for i in range(len(nombres))]


def test_importar_foto_que_no_se_puede_copiar_422(client: TestClient, importar_falso,
                                                  monkeypatch: pytest.MonkeyPatch):
    from classmer.api import app as app_mod

    original = app_mod._copiar

    def copiar(origen, destino):
        if destino.parent.parent.name == "fotos":
            raise OSError(22, "Invalid argument")
        original(origen, destino)

    monkeypatch.setattr(app_mod, "_copiar", copiar)
    archivos = [("audio", ("clase.wav", b"RIFF0000WAVE", "audio/wav")),
                ("fotos[]", ("rara.jpg", b"\xff\xd8", "image/jpeg"))]
    r = client.post("/api/importar", files=archivos, data={"materia_id": "prueba"})
    assert r.status_code == 422 and "rara.jpg" in r.json()["detail"]
    assert importar_falso == []


# --- apunte.pdf --------------------------------------------------------------------------------

def _apunte(cfg, sid: str, *, version: int = 1, actual: int = 1, compilo: int = 1,
            ruta: str | None = None, contenido: bytes | None = b"%PDF-1.5\n%prueba\n") -> Path:
    from classmer.db import Base

    ruta = ruta or f"apunte/v{version}/apunte.pdf"
    destino = Path(ruta) if Path(ruta).is_absolute() else cfg.sesion_dir(sid) / ruta
    if contenido is not None:
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(contenido)
    with Base(cfg.db_path).conexion() as conn:
        conn.execute(
            "INSERT INTO apuntes (sesion_id, version, actual, ruta_pdf, compilo, creado) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, version, actual, ruta, compilo, "2026-10-15T16:00:00-03:00"),
        )
    return destino


def test_apunte_pdf_404_sin_apunte(client: TestClient):
    sid, _ = crear(client)
    r = client.get(f"/api/sesiones/{sid}/apunte.pdf")
    assert r.status_code == 404
    assert client.get(f"/api/sesiones/{sid}").json()["pdf"] is None


def test_apunte_pdf_200(client: TestClient, cfg):
    sid, _ = crear(client)
    _apunte(cfg, sid, contenido=b"%PDF-1.5 version 1")
    r = client.get(f"/api/sesiones/{sid}/apunte.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == b"%PDF-1.5 version 1"
    assert client.get(f"/api/sesiones/{sid}").json()["pdf"] == f"/api/sesiones/{sid}/apunte.pdf"


def test_apunte_pdf_se_revalida_siempre(client: TestClient, cfg):
    # La URL es la misma para todas las versiones: sin caché heurística, el cliente no se queda con
    # una vieja. El contrato no cambia: `pdf` sigue siendo la misma URL, sin marca.
    sid, _ = crear(client)
    _apunte(cfg, sid, version=1, contenido=b"%PDF v1")
    r = client.get(f"/api/sesiones/{sid}/apunte.pdf")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert r.headers["content-disposition"] == 'inline; filename="apunte.pdf"'
    _apunte(cfg, sid, version=2, contenido=b"%PDF v2")
    from classmer.db import Base

    with Base(cfg.db_path).conexion() as conn:
        conn.execute("UPDATE apuntes SET actual = (version = 2) WHERE sesion_id = ?", (sid,))
    r = client.get(f"/api/sesiones/{sid}/apunte.pdf")
    assert r.content == b"%PDF v2" and r.headers["cache-control"] == "no-cache"
    assert client.get(f"/api/sesiones/{sid}").json()["pdf"] == f"/api/sesiones/{sid}/apunte.pdf"


def test_apunte_pdf_sirve_la_version_actual(client: TestClient, cfg):
    sid, _ = crear(client)
    _apunte(cfg, sid, version=1, actual=0, contenido=b"%PDF v1")
    _apunte(cfg, sid, version=2, actual=1, contenido=b"%PDF v2")
    assert client.get(f"/api/sesiones/{sid}/apunte.pdf").content == b"%PDF v2"


def test_apunte_pdf_ruta_absoluta(client: TestClient, cfg, tmp_path: Path):
    sid, _ = crear(client)
    _apunte(cfg, sid, ruta=str(tmp_path / "fuera" / "apunte.pdf"), contenido=b"%PDF abs")
    assert client.get(f"/api/sesiones/{sid}/apunte.pdf").content == b"%PDF abs"


@pytest.mark.parametrize("actual,compilo,contenido", [
    (1, 0, b"%PDF"),      # no compiló
    (1, 1, None),         # la fila está pero falta el archivo
])
def test_apunte_pdf_404_si_no_esta_listo(client: TestClient, cfg, actual: int, compilo: int, contenido):
    sid, _ = crear(client)
    _apunte(cfg, sid, actual=actual, compilo=compilo, contenido=contenido)
    assert client.get(f"/api/sesiones/{sid}/apunte.pdf").status_code == 404
    assert client.get(f"/api/sesiones/{sid}").json()["pdf"] is None


def test_apunte_pdf_si_la_version_nueva_no_compilo_sirve_la_anterior(client: TestClient, cfg):
    # Reprocesar el apunte y que v2 no compile no tiene que dejar a la app sin el PDF de v1.
    sid, _ = crear(client)
    _apunte(cfg, sid, version=1, actual=0, compilo=1, contenido=b"%PDF v1")
    _apunte(cfg, sid, version=2, actual=1, compilo=0, ruta="apunte/v2/apunte.pdf", contenido=None)
    r = client.get(f"/api/sesiones/{sid}/apunte.pdf")
    assert r.status_code == 200 and r.content == b"%PDF v1"
    assert client.get(f"/api/sesiones/{sid}").json()["pdf"] == f"/api/sesiones/{sid}/apunte.pdf"
    # Si v3 compila, vuelve a ganar la actual.
    _apunte(cfg, sid, version=3, actual=0, compilo=1, contenido=b"%PDF v3")
    from classmer.db import Base

    with Base(cfg.db_path).conexion() as conn:
        conn.execute("UPDATE apuntes SET actual = (version = 3) WHERE sesion_id = ?", (sid,))
    assert client.get(f"/api/sesiones/{sid}/apunte.pdf").content == b"%PDF v3"


# --- reprocesar (v0.2, pero la ruta ya existe) -------------------------------------------------

def test_reprocesar_sesion_grabando_409(client: TestClient):
    sid, _ = crear(client)
    assert client.post(f"/api/sesiones/{sid}/reprocesar", json={"desde": "apunte"}).status_code == 409
    assert client.post(f"/api/sesiones/{nuevo_id()}/reprocesar", json={"desde": "apunte"}).status_code == 404


def _finalizada(client: TestClient) -> str:
    sid, _ = crear(client)
    datos = b"OggS" + b"1" * 50
    client.put(f"/api/sesiones/{sid}/audio?offset=0", content=datos)
    r = client.post(f"/api/sesiones/{sid}/finalizar", json={
        "sha256": hashlib.sha256(datos).hexdigest(), "bytes_totales": len(datos),
        "duracion_ms": 1000, "eventos_totales": 0})
    assert r.status_code == 200
    return sid


def _jobs(cfg, sid: str) -> set[tuple[str, str]]:
    from classmer import cola
    from classmer.db import Base

    with Base(cfg.db_path).conexion() as conn:
        return {(j.tipo, j.estado) for j in cola.jobs_de_sesion(conn, sid)}


def test_reprocesar_etapas_del_modelo_son_las_del_pipeline():
    from typing import get_args

    from classmer.api.app import EtapaReproceso
    from classmer.pipeline import ETAPAS

    assert list(get_args(EtapaReproceso)) == ETAPAS


@pytest.mark.parametrize("cuerpo", [{"desde": "etapa-que-no-existe"}, {"desde": ""}, {}, None])
def test_reprocesar_etapa_invalida_o_sin_cuerpo_422(client: TestClient, cfg, cuerpo):
    # 422 (datos inválidos) y sin tocar la cola; el 409 queda para "hay un job corriendo".
    sid = _finalizada(client)
    r = client.post(f"/api/sesiones/{sid}/reprocesar", json=cuerpo) if cuerpo is not None \
        else client.post(f"/api/sesiones/{sid}/reprocesar")
    assert r.status_code == 422, r.text
    assert _jobs(cfg, sid) == {("verificar", "pendiente")}


def test_reprocesar_apunte_sin_timeline_409_sin_cancelar_nada(client: TestClient, cfg):
    sid = _finalizada(client)
    r = client.post(f"/api/sesiones/{sid}/reprocesar", json={"desde": "apunte"})
    assert r.status_code == 409 and "timeline.md" in r.json()["detail"]
    assert _jobs(cfg, sid) == {("verificar", "pendiente")}
    assert fila(cfg, sid)["estado"] == "en_cola"


def test_reprocesar_encola_desde_la_etapa(client: TestClient, cfg):
    sid = _finalizada(client)
    (cfg.sesion_dir(sid) / "timeline.md").write_text("# línea de tiempo\n", encoding="utf-8")
    r = client.post(f"/api/sesiones/{sid}/reprocesar", json={"desde": "apunte"})
    assert r.status_code == 200 and r.json() == {"id": sid, "estado": "en_cola"}
    assert _jobs(cfg, sid) == {("verificar", "cancelado"), ("apunte", "pendiente")}
    r = client.post(f"/api/sesiones/{sid}/reprocesar", json={"desde": "verificar"})
    assert r.status_code == 200
    assert _jobs(cfg, sid) == {("verificar", "cancelado"), ("apunte", "cancelado"), ("verificar", "pendiente")}
