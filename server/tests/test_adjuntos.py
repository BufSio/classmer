"""Adjuntos de una clase (§5.1, §10): API, tipos por contenido, nombres, estados, migración y la
preparación para LaTeX y la línea de tiempo."""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import struct
import uuid
import zlib
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from classmer import adjuntos, cola
from classmer.adjuntos import (
    PENDIENTE,
    SIN_USAR,
    USADO,
    detectar_tipo,
    elegir_nombre,
    estados,
    listar,
    preparar_en,
    raiz_nombre,
    refrescar_timeline,
)
from classmer.db import Base, inicializar

INICIO = "2026-10-15T14:00:05-03:00"
NOMBRE_SEGURO = re.compile(r"^[A-Za-z0-9_-]+\.(pdf|jpg|png|webp|heic)$")


# --- archivos de prueba ---------------------------------------------------------------------------

def pdf(paginas: int = 3, *, clave_usuario: str | None = None, clave_duenio: str | None = None) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(paginas):
        w.add_blank_page(width=200, height=150)
    if clave_usuario is not None:
        w.encrypt(user_password=clave_usuario, owner_password=clave_duenio)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def imagen(formato: str, tamano: tuple[int, int] = (40, 30), *, orientacion: int | None = None) -> bytes:
    img = Image.new("RGB", tamano, (200, 30, 30))
    buf = io.BytesIO()
    if orientacion is not None:
        exif = Image.Exif()
        exif[0x0112] = orientacion
        img.save(buf, formato, exif=exif)
    else:
        img.save(buf, formato)
    return buf.getvalue()


def heic() -> bytes:
    pytest.importorskip("pillow_heif")
    return imagen("HEIF")


def png_gigante(ancho: int, alto: int) -> bytes:
    """Un PNG RGBA de ancho×alto que pesa unos bytes (una "bomba"): la cabecera dice el tamaño y los
    datos ni están, así que decodificarlo daría error en vez de pedir GB de RAM."""
    def chunk(tipo: bytes, datos: bytes) -> bytes:
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * 1000)) + chunk(b"IEND", b""))


# --- helpers de la API ----------------------------------------------------------------------------

def nuevo_id() -> str:
    return str(uuid.uuid4())


def crear(client: TestClient) -> str:
    sid = nuevo_id()
    r = client.post("/api/sesiones", json={"id": sid, "materia_id": "prueba", "origen_materia": "manual",
                                          "tipo_clase": "teorica", "inicio": INICIO, "dispositivo": "s23"})
    assert r.status_code == 201
    return sid


def subir(client: TestClient, sid: str, contenido: bytes, nombre: str = "Slides clase 5.pdf", *,
          aid: str | None = None, mime: str = "application/pdf", **campos):
    datos = {"id": aid or nuevo_id(), **{k: str(v) for k, v in campos.items()}}
    return client.post(f"/api/sesiones/{sid}/adjuntos", data=datos, files={"archivo": (nombre, contenido, mime)})


def sql(cfg, consulta: str, *params):
    with Base(cfg.db_path).conexion() as conn:
        return conn.execute(consulta, params).fetchall()


def estado_sesion(cfg, sid: str, estado: str, etapa: str | None = None) -> None:
    sql(cfg, "UPDATE sesiones SET estado = ?, etapa = ? WHERE id = ?", estado, etapa, sid)


def apunte(cfg, sid: str, version: int = 1, usados: list[str] | None = None, *, actual: bool = True) -> None:
    if actual:
        sql(cfg, "UPDATE apuntes SET actual = 0 WHERE sesion_id = ?", sid)
    sql(cfg, "INSERT INTO apuntes (sesion_id, version, actual, compilo, creado, adjuntos) VALUES (?, ?, ?, 1, ?, ?)",
        sid, version, int(actual), INICIO, json.dumps(usados) if usados is not None else None)


def sin_temporales(cfg, sid: str) -> bool:
    carpeta = cfg.sesion_dir(sid) / "adjuntos"
    return not carpeta.is_dir() or not any(p.name.startswith(".") for p in carpeta.iterdir())


# --- POST /api/sesiones/{id}/adjuntos -------------------------------------------------------------

def test_subir_pdf_201_y_reenvio_200(client: TestClient, cfg):
    sid = crear(client)
    aid = nuevo_id()
    contenido = pdf(3)
    r = subir(client, sid, contenido, "Slides clase 5 - Límites.pdf", aid=aid)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert {k: cuerpo[k] for k in ("id", "nombre", "nombre_original", "tipo", "bytes", "offset_ms", "estado")} == {
        "id": aid, "nombre": "Slides_clase_5_-_Limites.pdf", "nombre_original": "Slides clase 5 - Límites.pdf",
        "tipo": "pdf", "bytes": len(contenido), "offset_ms": None, "estado": PENDIENTE}
    assert cuerpo["sesion"]["id"] == sid
    assert [a["id"] for a in cuerpo["sesion"]["adjuntos"]] == [aid]
    assert cuerpo["sesion"]["adjuntos_sin_usar"] == 0
    guardado = cfg.sesion_dir(sid) / "adjuntos" / "Slides_clase_5_-_Limites.pdf"
    assert guardado.read_bytes() == contenido

    (f,) = sql(cfg, "SELECT * FROM assets WHERE id = ?", aid)
    assert f["tipo"] == "adjunto" and f["sesion_id"] == sid
    assert f["ruta"] == "adjuntos/Slides_clase_5_-_Limites.pdf"
    assert f["sha256"] == hashlib.sha256(contenido).hexdigest() and f["bytes_recibidos"] == len(contenido)
    datos = json.loads(f["datos"])
    assert datos["nombre_original"] == "Slides clase 5 - Límites.pdf" and datos["origen"] == "app"
    assert datos["paginas"] == 3 and datos["estado_sesion"] == "grabando" and datos["mime"] == "application/pdf"
    registro = (cfg.sesion_dir(sid) / "adjuntos.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(registro) == 1 and json.loads(registro[0])["id"] == aid

    # El mismo id otra vez (se perdió la respuesta), aunque traiga otro contenido: 200 y nada nuevo.
    r2 = subir(client, sid, pdf(1), "otro.pdf", aid=aid)
    assert r2.status_code == 200 and r2.json()["nombre"] == "Slides_clase_5_-_Limites.pdf"
    assert guardado.read_bytes() == contenido
    assert len(list(guardado.parent.iterdir())) == 1
    assert len(sql(cfg, "SELECT * FROM assets WHERE tipo = 'adjunto'")) == 1
    assert sin_temporales(cfg, sid)


def test_offset_y_creado(client: TestClient, cfg):
    sid = crear(client)
    r = subir(client, sid, pdf(), offset_ms=2040000, creado="2026-10-15T14:34:05-03:00")
    assert r.status_code == 201
    assert r.json()["offset_ms"] == 2040000 and r.json()["creado"] == "2026-10-15T14:34:05-03:00"
    (f,) = sql(cfg, "SELECT offset_ms FROM assets WHERE tipo = 'adjunto'")
    assert f["offset_ms"] == 2040000


@pytest.mark.parametrize("nombre, contenido, tipo, extension", [
    ("notas.bin", pdf(1), "pdf", ".pdf"),          # la extensión no importa: manda el contenido
    ("foto.png", imagen("JPEG"), "jpeg", ".jpg"),
    ("captura.png", imagen("PNG"), "png", ".png"),
    ("slide.webp", imagen("WEBP"), "webp", ".webp"),
    ("sin extension", pdf(1), "pdf", ".pdf"),
])
def test_tipo_por_contenido(client: TestClient, cfg, nombre, contenido, tipo, extension):
    sid = crear(client)
    r = subir(client, sid, contenido, nombre, mime="application/octet-stream")
    assert r.status_code == 201, r.text
    assert r.json()["tipo"] == tipo and r.json()["nombre"].endswith(extension)


def test_heic_se_acepta(client: TestClient, cfg):
    sid = crear(client)
    r = subir(client, sid, heic(), "IMG_0001.HEIC", mime="image/heic")
    assert r.status_code == 201, r.text
    assert r.json()["tipo"] == "heic" and r.json()["nombre"] == "IMG_0001.heic"


@pytest.mark.parametrize("nombre, contenido", [
    ("slides.pdf", b"esto no es un pdf\n" * 10),
    ("foto.jpg", b"GIF89a" + b"\x00" * 50),
    ("virus.pdf.exe", b"MZ\x90\x00" + b"\x00" * 100),
])
def test_tipo_no_soportado_415(client: TestClient, cfg, nombre, contenido):
    sid = crear(client)
    r = subir(client, sid, contenido, nombre)
    assert r.status_code == 415
    assert "PDF, JPEG, PNG, WebP o HEIC" in r.json()["detail"]
    assert not sql(cfg, "SELECT * FROM assets WHERE tipo = 'adjunto'")
    carpeta = cfg.sesion_dir(sid) / "adjuntos"
    assert not carpeta.exists() or not any(carpeta.iterdir())


def test_tope_413(client: TestClient, cfg, monkeypatch):
    sid = crear(client)
    monkeypatch.setattr(adjuntos, "MAX_ADJUNTO", 1000)
    r = subir(client, sid, b"%PDF-1.4\n" + b"x" * 2000)
    assert r.status_code == 413 and "MB" in r.json()["detail"]
    assert not sql(cfg, "SELECT * FROM assets WHERE tipo = 'adjunto'")
    assert sin_temporales(cfg, sid)


def test_tope_413_por_content_length(client: TestClient, cfg, monkeypatch):
    from classmer.api import app as modulo_app

    sid = crear(client)
    monkeypatch.setattr(adjuntos, "MAX_ADJUNTO", 1000)
    monkeypatch.setattr(modulo_app, "MARGEN_MULTIPART", 0)
    r = subir(client, sid, b"%PDF-1.4\n" + b"x" * 5000)
    assert r.status_code == 413
    assert not (cfg.sesion_dir(sid) / "adjuntos").exists()


@pytest.mark.parametrize("nombre", [
    "../../../evil.pdf", "..\\..\\evil.pdf", "C:\\Windows\\System32\\evil.pdf", "/etc/passwd.pdf",
    "....pdf", "CON.pdf", "a:b*c?.pdf", "‮gpj.pdf", "名前.pdf", " .pdf", "x" * 300 + ".pdf",
])
def test_nombre_saneado_sin_path_traversal(client: TestClient, cfg, nombre):
    sid = crear(client)
    r = subir(client, sid, pdf(1), nombre)
    assert r.status_code == 201, r.text
    guardado = r.json()["nombre"]
    assert NOMBRE_SEGURO.match(guardado) and len(guardado) <= 64
    assert guardado.split(".")[0].casefold() not in {"con", "prn", "aux", "nul"}
    carpeta = (cfg.sesion_dir(sid) / "adjuntos").resolve()
    (f,) = sql(cfg, "SELECT ruta FROM assets WHERE tipo = 'adjunto'")
    assert (cfg.sesion_dir(sid) / f["ruta"]).resolve().parent == carpeta


def test_nombres_repetidos_no_chocan_ni_en_la_raiz(client: TestClient, cfg):
    sid = crear(client)
    nombres = [subir(client, sid, contenido, nombre).json()["nombre"] for nombre, contenido in (
        ("Slides.pdf", pdf(1)), ("Slides.pdf", pdf(2)), ("slides.jpg", imagen("JPEG")), ("SLIDES.png", imagen("PNG")))]
    assert nombres == ["Slides.pdf", "Slides-2.pdf", "slides-3.jpg", "SLIDES-4.png"]
    # Los nombres para LaTeX (raíz + .pdf/.jpg) tampoco chocan.
    with Base(cfg.db_path).conexion() as conn:
        latex = [adjuntos.nombre_latex(a) for a in listar(conn, sid)]
    assert len({n.casefold() for n in latex}) == 4


def test_404_sesion_inexistente(client: TestClient, cfg):
    sid = nuevo_id()
    r = subir(client, sid, pdf())
    assert r.status_code == 404 and r.json() == {"detail": "sesión no encontrada"}
    assert not cfg.sesion_dir(sid).exists()


@pytest.mark.parametrize("campos, archivo, detalle", [
    ({"id": ""}, True, "id de adjunto inválido"),
    ({"id": "../../x"}, True, "id de adjunto inválido"),
    ({"id": "con espacio"}, True, "id de adjunto inválido"),
    ({"id": "abc"}, True, "id de adjunto inválido"),                                   # no es un UUID
    ({"id": "{" + str(uuid.UUID(int=1)) + "}"}, True, "id de adjunto inválido"),
    ({"offset_ms": "-1"}, True, "negativo"),
    ({"offset_ms": "abc"}, True, "offset_ms inválido"),
    ({"offset_ms": str(10**20)}, True, "fuera de rango"),      # antes: OverflowError de SQLite (500)
    ({}, False, "falta el archivo"),
])
def test_422_datos_invalidos(client: TestClient, cfg, campos, archivo, detalle):
    sid = crear(client)
    datos = {"id": nuevo_id(), **campos}
    archivos = {"archivo": ("a.pdf", pdf(), "application/pdf")} if archivo else None
    r = client.post(f"/api/sesiones/{sid}/adjuntos", data=datos, files=archivos)
    assert r.status_code == 422, r.text
    assert detalle in r.json()["detail"]
    assert not sql(cfg, "SELECT * FROM assets WHERE tipo = 'adjunto'")
    carpeta = cfg.sesion_dir(sid) / "adjuntos"
    assert not carpeta.exists() or not any(carpeta.iterdir())


def test_id_en_mayusculas_es_el_mismo_adjunto(client: TestClient, cfg):
    sid = crear(client)
    aid = nuevo_id()
    r = subir(client, sid, pdf(), "Slides.pdf", aid=aid.upper())
    assert r.status_code == 201 and r.json()["id"] == aid
    r = subir(client, sid, pdf(), "Slides.pdf", aid=aid)
    assert r.status_code == 200 and r.json()["nombre"] == "Slides.pdf"
    assert [f["id"] for f in sql(cfg, "SELECT id FROM assets WHERE tipo = 'adjunto'")] == [aid]
    assert [p.name for p in (cfg.sesion_dir(sid) / "adjuntos").iterdir()] == ["Slides.pdf"]


def test_si_falla_el_insert_no_queda_el_archivo(client: TestClient, cfg):
    """El archivo se mueve a su nombre antes del INSERT: si el INSERT falla (base bloqueada más de 30 s,
    disco), no puede quedar huérfano, sin fila y ocupando la raíz para siempre."""
    sid = crear(client)

    class Conexion:
        def __init__(self, conn: sqlite3.Connection):
            self._conn = conn

        def execute(self, consulta: str, *params):
            if "INSERT INTO assets" in consulta:
                raise sqlite3.OperationalError("database is locked")
            return self._conn.execute(consulta, *params)

        def __getattr__(self, nombre: str):
            return getattr(self._conn, nombre)

    class BaseBloqueada(Base):
        @contextmanager
        def conexion(self):
            with super().conexion() as conn:
                yield Conexion(conn)

    aid = nuevo_id()
    with pytest.raises(sqlite3.OperationalError):
        adjuntos.guardar(cfg, BaseBloqueada(cfg.db_path), sid, adjunto_id=aid, archivo=io.BytesIO(pdf()),
                         nombre_original="Slides.pdf")
    carpeta = cfg.sesion_dir(sid) / "adjuntos"
    assert not any(carpeta.iterdir())
    # El reintento (la app reenvía ante un 5xx) lo guarda con su nombre, no como Slides-2.pdf.
    r = subir(client, sid, pdf(), "Slides.pdf", aid=aid)
    assert r.status_code == 201 and r.json()["nombre"] == "Slides.pdf"
    assert [p.name for p in carpeta.iterdir()] == ["Slides.pdf"]


def test_422_archivo_vacio(client: TestClient, cfg):
    sid = crear(client)
    r = subir(client, sid, b"")
    assert r.status_code == 422 and "vacío" in r.json()["detail"]
    assert sin_temporales(cfg, sid)


def test_consulta_sin_archivo(client: TestClient, cfg):
    """La app pregunta con sólo el id antes de mandar el archivo (así no le manda 100 MB a un server
    que no los va a guardar): 200 si ya lo tiene, 422 "falta el archivo" si no (también con la clase
    en error) y 404 si la clase no existe."""
    sid = crear(client)
    aid = nuevo_id()

    def consultar(sesion: str, adjunto: str):
        return client.post(f"/api/sesiones/{sesion}/adjuntos", data={"id": adjunto})

    r = consultar(sid, aid)
    assert r.status_code == 422 and "falta el archivo" in r.json()["detail"]
    assert subir(client, sid, pdf(), aid=aid).status_code == 201
    r = consultar(sid, aid)
    assert r.status_code == 200 and r.json()["id"] == aid and r.json()["sesion"]["id"] == sid
    estado_sesion(cfg, sid, "error")
    assert consultar(sid, aid).status_code == 200
    otro = nuevo_id()
    assert consultar(sid, otro).status_code == 422
    assert subir(client, sid, pdf(), aid=otro).status_code == 201
    assert consultar(nuevo_id(), aid).status_code == 404
    assert len(sql(cfg, "SELECT * FROM assets WHERE tipo = 'adjunto'")) == 2


def test_422_id_de_un_evento_o_de_otra_sesion(client: TestClient, cfg):
    sid, otra = crear(client), crear(client)
    evento = nuevo_id()
    assert client.post(f"/api/sesiones/{sid}/eventos",
                       data={"id": evento, "tipo": "nota", "offset_ms": "1000", "texto": "x"}).status_code == 201
    r = subir(client, sid, pdf(), aid=evento)
    assert r.status_code == 422 and "evento" in r.json()["detail"]
    aid = nuevo_id()
    assert subir(client, otra, pdf(), aid=aid).status_code == 201
    r = subir(client, sid, pdf(), aid=aid)
    assert r.status_code == 422 and "otra sesión" in r.json()["detail"]


def test_evento_con_el_id_de_un_adjunto_422(client: TestClient):
    # Al revés: un 200 diría "ya estaba" sin guardarlo, y el finalizar quedaría en faltan_eventos.
    sid = crear(client)
    aid = subir(client, sid, pdf()).json()["id"]
    r = client.post(f"/api/sesiones/{sid}/eventos", data={"id": aid, "tipo": "nota", "offset_ms": "1000", "texto": "x"})
    assert r.status_code == 422 and "adjunto" in r.json()["detail"]
    assert client.get(f"/api/sesiones/{sid}/eventos").json() == {"ids": []}


def test_mas_de_un_archivo_422(client: TestClient):
    sid = crear(client)
    r = client.post(f"/api/sesiones/{sid}/adjuntos", data={"id": nuevo_id()},
                    files=[("archivo", ("a.pdf", pdf(), "application/pdf")),
                           ("archivo", ("b.pdf", pdf(), "application/pdf"))])
    assert r.status_code == 422


# --- estados de la sesión al adjuntar --------------------------------------------------------------

def test_sesion_en_error_acepta_adjuntos_para_regenerar(client: TestClient, cfg):
    """Una clase en error (por ejemplo, un apunte que no compiló) acepta material nuevo: queda sin
    usar y se suma al regenerar (POST /regenerar acepta lista y error)."""
    sid = crear(client)
    aid = nuevo_id()
    assert subir(client, sid, pdf(), aid=aid).status_code == 201
    estado_sesion(cfg, sid, "error")
    apunte(cfg, sid, 1, [aid])  # la versión que no compiló usó el primero
    (cfg.sesion_dir(sid) / "timeline.md").write_text("# Clase: x\n", encoding="utf-8")
    r = subir(client, sid, pdf(), "nuevo.pdf")
    assert r.status_code == 201, r.text
    assert r.json()["estado"] == SIN_USAR and r.json()["sesion"]["adjuntos_sin_usar"] == 1
    (f,) = sql(cfg, "SELECT datos FROM assets WHERE id = ?", r.json()["id"])
    assert json.loads(f["datos"])["estado_sesion"] == "error"
    assert subir(client, sid, pdf(), aid=aid).status_code == 200  # reenvío: sigue idempotente
    assert len(list((cfg.sesion_dir(sid) / "adjuntos").iterdir())) == 2
    r = client.post(f"/api/sesiones/{sid}/regenerar")
    assert r.status_code == 200 and r.json()["adjuntos_sin_usar"] == 0
    assert {a["estado"] for a in r.json()["adjuntos"]} == {PENDIENTE}


@pytest.mark.parametrize("estado", ["grabando", "en_cola", "procesando", "lista", "esperando_usuario", "error"])
def test_se_acepta_en_cualquier_estado(client: TestClient, cfg, estado):
    sid = crear(client)
    estado_sesion(cfg, sid, estado)
    assert subir(client, sid, pdf()).status_code == 201


def test_lista_con_apunte_queda_sin_usar(client: TestClient, cfg):
    sid = crear(client)
    estado_sesion(cfg, sid, "lista")
    apunte(cfg, sid)
    r = subir(client, sid, pdf())
    assert r.json()["estado"] == SIN_USAR
    assert r.json()["sesion"]["adjuntos_sin_usar"] == 1
    assert client.get(f"/api/sesiones/{sid}").json()["adjuntos_sin_usar"] == 1


# --- eventos, finalizar y objeto de sesión ---------------------------------------------------------

def test_los_adjuntos_no_son_eventos(client: TestClient, cfg):
    sid = crear(client)
    evento = nuevo_id()
    client.post(f"/api/sesiones/{sid}/eventos", data={"id": evento, "tipo": "marca", "offset_ms": "5", "marca": "importante"})
    subir(client, sid, pdf(), offset_ms=10)
    assert client.get(f"/api/sesiones/{sid}/eventos").json() == {"ids": [evento]}
    audio = b"OggS" + b"\x00" * 100
    assert client.put(f"/api/sesiones/{sid}/audio?offset=0", content=audio).status_code == 200
    sha = hashlib.sha256(audio).hexdigest()
    r = client.post(f"/api/sesiones/{sid}/finalizar",
                    json={"sha256": sha, "bytes_totales": len(audio), "duracion_ms": 1000, "eventos_totales": 2})
    assert r.status_code == 409 and r.json() == {"motivo": "faltan_eventos", "eventos_recibidos": 1}
    r = client.post(f"/api/sesiones/{sid}/finalizar",
                    json={"sha256": sha, "bytes_totales": len(audio), "duracion_ms": 1000, "eventos_totales": 1})
    assert r.status_code == 200


def test_objeto_de_sesion_trae_adjuntos(client: TestClient):
    sid = crear(client)
    vacia = client.get(f"/api/sesiones/{sid}").json()
    assert vacia["adjuntos"] == [] and vacia["adjuntos_sin_usar"] == 0
    a = subir(client, sid, pdf(), "a.pdf", offset_ms=1000).json()
    b = subir(client, sid, imagen("PNG"), "b.png").json()
    lista = client.get("/api/sesiones").json()
    (s,) = [x for x in lista if x["id"] == sid]
    assert [x["id"] for x in s["adjuntos"]] == [a["id"], b["id"]]
    assert set(s["adjuntos"][0]) == {"id", "nombre", "nombre_original", "tipo", "bytes", "offset_ms", "creado", "estado"}


# --- POST /api/sesiones/{id}/regenerar ------------------------------------------------------------

def _lista_con_timeline(client: TestClient, cfg) -> str:
    sid = crear(client)
    estado_sesion(cfg, sid, "lista")
    apunte(cfg, sid)
    (cfg.sesion_dir(sid) / "timeline.md").write_text("# Clase: x\n", encoding="utf-8")
    return sid


def test_regenerar_lista(client: TestClient, cfg):
    sid = _lista_con_timeline(client, cfg)
    assert subir(client, sid, pdf()).json()["estado"] == SIN_USAR
    r = client.post(f"/api/sesiones/{sid}/regenerar")
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == "en_cola" and cuerpo["etapa"] == "apunte"
    assert cuerpo["adjuntos_sin_usar"] == 0 and cuerpo["adjuntos"][0]["estado"] == PENDIENTE
    assert [j["tipo"] for j in sql(cfg, "SELECT tipo FROM jobs WHERE sesion_id = ? AND estado = 'pendiente'", sid)] \
        == ["apunte"]


def test_regenerar_en_error_con_cuerpo_vacio(client: TestClient, cfg):
    sid = _lista_con_timeline(client, cfg)
    estado_sesion(cfg, sid, "error")
    assert client.post(f"/api/sesiones/{sid}/regenerar", json={}).status_code == 200


@pytest.mark.parametrize("estado, texto", [
    ("en_cola", "ya se está procesando"), ("procesando", "ya se está procesando"),
    ("grabando", "grabando"), ("esperando_usuario", "esperando_usuario"),
])
def test_regenerar_409_si_no_termino(client: TestClient, cfg, estado, texto):
    sid = _lista_con_timeline(client, cfg)
    estado_sesion(cfg, sid, estado)
    r = client.post(f"/api/sesiones/{sid}/regenerar")
    assert r.status_code == 409 and texto in r.json()["detail"]
    assert not sql(cfg, "SELECT * FROM jobs WHERE sesion_id = ?", sid)


def test_regenerar_409_sin_timeline_o_con_job_corriendo(client: TestClient, cfg, base):
    sid = _lista_con_timeline(client, cfg)
    (cfg.sesion_dir(sid) / "timeline.md").unlink()
    r = client.post(f"/api/sesiones/{sid}/regenerar")
    assert r.status_code == 409 and "timeline.md" in r.json()["detail"]
    (cfg.sesion_dir(sid) / "timeline.md").write_text("# Clase: x\n", encoding="utf-8")
    with base.conexion() as conn:
        cola.encolar(conn, "notificar", sid)
        conn.execute("UPDATE jobs SET estado = 'corriendo' WHERE sesion_id = ?", (sid,))
    r = client.post(f"/api/sesiones/{sid}/regenerar")
    assert r.status_code == 409 and "corriendo" in r.json()["detail"]


def test_regenerar_404(client: TestClient):
    r = client.post(f"/api/sesiones/{nuevo_id()}/regenerar")
    assert r.status_code == 404 and r.json() == {"detail": "sesión no encontrada"}


# --- estados de cada adjunto ----------------------------------------------------------------------

def _con_adjuntos(client: TestClient, n: int = 2) -> tuple[str, list[str]]:
    sid = crear(client)
    ids = [subir(client, sid, pdf(), f"a{i}.pdf").json()["id"] for i in range(n)]
    return sid, ids


def _estados(cfg, sid: str) -> dict[str, str]:
    with Base(cfg.db_path).conexion() as conn:
        return estados(cfg, conn, sid, listar(conn, sid))


def test_estado_usado_y_sin_usar(client: TestClient, cfg):
    sid, (a, b) = _con_adjuntos(client)
    estado_sesion(cfg, sid, "lista")
    apunte(cfg, sid, 1, [a])
    assert _estados(cfg, sid) == {a: USADO, b: SIN_USAR}
    # Si la vigente vuelve a ser una versión vieja (la nueva no compiló), manda la vigente.
    apunte(cfg, sid, 2, [a, b], actual=False)
    assert _estados(cfg, sid) == {a: USADO, b: SIN_USAR}


def test_estado_sin_apunte_es_pendiente(client: TestClient, cfg):
    sid, (a, b) = _con_adjuntos(client)
    for estado in ("grabando", "esperando_usuario", "error"):
        estado_sesion(cfg, sid, estado)
        assert set(_estados(cfg, sid).values()) == {PENDIENTE}


def test_estado_con_etapas_en_cola_es_pendiente(client: TestClient, cfg, base):
    sid, (a, b) = _con_adjuntos(client)
    estado_sesion(cfg, sid, "procesando", "transcribir")
    apunte(cfg, sid, 1, [a])
    with base.conexion() as conn:
        cola.encolar(conn, "linea_de_tiempo", sid)
    assert _estados(cfg, sid) == {a: PENDIENTE, b: PENDIENTE}
    # Corriendo y sin nada en cola (la siguiente se encola recién al terminar): sigue viniendo un apunte.
    with base.conexion() as conn:
        assert cola.tomar(conn, sesion_id=sid).tipo == "linea_de_tiempo"
    assert _estados(cfg, sid) == {a: PENDIENTE, b: PENDIENTE}
    sesion = client.get(f"/api/sesiones/{sid}").json()
    assert sesion["adjuntos_sin_usar"] == 0 and {x["estado"] for x in sesion["adjuntos"]} == {PENDIENTE}
    # Lo mismo con el último bloque de transcribir corriendo solo.
    sql(cfg, "DELETE FROM jobs WHERE sesion_id = ?", sid)
    with base.conexion() as conn:
        cola.encolar(conn, "transcribir", sid, {"n": 3})
        assert cola.tomar(conn, sesion_id=sid).tipo == "transcribir"
    assert _estados(cfg, sid) == {a: PENDIENTE, b: PENDIENTE}
    # Sólo publicar en cola o corriendo: el apunte no se rehace.
    for corre in (False, True):
        sql(cfg, "DELETE FROM jobs WHERE sesion_id = ?", sid)
        with base.conexion() as conn:
            cola.encolar(conn, "publicar", sid)
            if corre:
                cola.tomar(conn, sesion_id=sid)
        assert _estados(cfg, sid) == {a: USADO, b: SIN_USAR}


def test_estado_con_el_apunte_corriendo(client: TestClient, cfg, base):
    sid, (a, b) = _con_adjuntos(client)
    estado_sesion(cfg, sid, "procesando", "apunte")
    with base.conexion() as conn:
        job = cola.encolar(conn, "apunte", sid)
        conn.execute("UPDATE jobs SET estado = 'corriendo' WHERE sesion_id = ?", (sid,))
    job_id = sql(cfg, "SELECT id FROM jobs WHERE sesion_id = ?", sid)[0]["id"]
    assert job is not None
    # Todavía prepara la carpeta: no eligió nada.
    assert _estados(cfg, sid) == {a: PENDIENTE, b: PENDIENTE}
    workdir = cfg.jobs_dir / sid / "apunte"
    adjuntos.escribir_manifiesto(workdir, job_id, [a])
    assert _estados(cfg, sid) == {a: PENDIENTE, b: SIN_USAR}
    # Un manifiesto de otra corrida no cuenta.
    adjuntos.escribir_manifiesto(workdir, job_id + 1000, [a])
    assert _estados(cfg, sid) == {a: PENDIENTE, b: PENDIENTE}


# --- migración ------------------------------------------------------------------------------------

def test_migracion_agrega_apuntes_adjuntos(tmp_path: Path):
    """Una base de antes (esquema 2, sin apuntes.adjuntos) se migra sola al abrirla."""
    from classmer import db

    linea = next(ln for ln in db.ESQUEMA.splitlines(keepends=True) if ln.strip().startswith("adjuntos "))
    viejo = db.ESQUEMA.replace(linea, "")
    assert viejo != db.ESQUEMA
    ruta = tmp_path / "vieja.db"
    conn = sqlite3.connect(ruta)
    conn.executescript(viejo)
    conn.execute("INSERT INTO sesiones (id, inicio, estado, creado, actualizado) VALUES ('s1', 'x', 'lista', 'x', 'x')")
    conn.execute("INSERT INTO apuntes (sesion_id, version, actual, compilo, titulo, creado) VALUES ('s1', 1, 1, 1, 'T', 'x')")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    assert "adjuntos" not in {c[1] for c in conn.execute("PRAGMA table_info(apuntes)")}
    conn.close()
    inicializar(ruta)
    inicializar(ruta)  # idempotente
    conn = sqlite3.connect(ruta)
    assert "adjuntos" in {c[1] for c in conn.execute("PRAGMA table_info(apuntes)")}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION_ESQUEMA
    assert conn.execute("SELECT titulo, adjuntos FROM apuntes").fetchone() == ("T", None)
    conn.close()


# --- tipos y nombres ------------------------------------------------------------------------------

def _ftyp(principal: bytes, *compatibles: bytes) -> bytes:
    cuerpo = principal + b"\x00\x00\x00\x00" + b"".join(compatibles)
    return (8 + len(cuerpo)).to_bytes(4, "big") + b"ftyp" + cuerpo + b"\x00" * 16


@pytest.mark.parametrize("cabecera, tipo", [
    (b"%PDF-1.7\n", "pdf"),
    (b"\xef\xbb\xbf  basura\r\n%PDF-1.4", "pdf"),
    (b"x" * 1100 + b"%PDF-1.4", None),
    (b"\xff\xd8\xff\xe0\x00\x10JFIF", "jpeg"),
    (b"\x89PNG\r\n\x1a\n\x00\x00", "png"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", None),
    (_ftyp(b"heic", b"mif1", b"heic"), "heic"),
    (_ftyp(b"mif1", b"heic"), "heic"),
    (_ftyp(b"msf1", b"hevc"), "heic"),
    (_ftyp(b"avif", b"mif1", b"miaf"), None),
    (_ftyp(b"isom", b"mp41"), None),
    (b"GIF89a", None),
    (b"", None),
])
def test_detectar_tipo(cabecera, tipo):
    assert detectar_tipo(cabecera) == tipo


@pytest.mark.parametrize("original, raiz", [
    ("Slides clase 5 - Límites.pdf", "Slides_clase_5_-_Limites"),
    ("C:\\fakepath\\Teórica 3.PDF", "Teorica_3"),
    ("../../etc/passwd", "passwd"),
    ("archivo.tar.gz", "archivo_tar"),
    (".bashrc", "bashrc"),
    ("名前.pdf", "adjunto"),
    ("", "adjunto"),
    (None, "adjunto"),
    ("nul.pdf", "nul_"),
    ("Ñandú (versión final).pdf", "Nandu_version_final"),
])
def test_raiz_nombre(original, raiz):
    assert raiz_nombre(original) == raiz


def test_elegir_nombre():
    assert elegir_nombre("a", ".pdf", []) == "a.pdf"
    assert elegir_nombre("a", ".pdf", ["A", "a-2"]) == "a-3.pdf"
    largo = "x" * 60
    assert elegir_nombre(largo, ".pdf", [largo]) == "x" * 58 + "-2.pdf"


def test_info_pdf(tmp_path: Path):
    ruta = tmp_path / "a.pdf"
    ruta.write_bytes(pdf(4))
    assert adjuntos.info_pdf(ruta) == {"paginas": 4}
    ruta.write_bytes(pdf(2, clave_usuario="", clave_duenio="duenio"))
    assert adjuntos.info_pdf(ruta) == {"cifrado": True, "paginas": 2}
    ruta.write_bytes(pdf(2, clave_usuario="secreta", clave_duenio="duenio"))
    assert adjuntos.info_pdf(ruta) == {"cifrado": True}
    ruta.write_bytes(b"%PDF-1.4\nroto")
    assert adjuntos.info_pdf(ruta) == {}


# --- preparar para LaTeX ---------------------------------------------------------------------------

def _guardados(client: TestClient, cfg, archivos: list[tuple[str, bytes]]) -> tuple[str, list[dict]]:
    sid = crear(client)
    for nombre, contenido in archivos:
        assert subir(client, sid, contenido, nombre).status_code == 201
    with Base(cfg.db_path).conexion() as conn:
        return sid, listar(conn, sid)


def test_preparar_pdf_tal_cual_e_imagenes_como_jpg(client: TestClient, cfg, tmp_path: Path):
    jpg = imagen("JPEG")
    sid, lista = _guardados(client, cfg, [
        ("slides.pdf", pdf(2)), ("foto.jpg", jpg), ("cap.png", imagen("PNG")), ("w.webp", imagen("WEBP")),
        ("acostada.jpg", imagen("JPEG", orientacion=6))])
    destino = tmp_path / "input" / "adjuntos"
    preparados = preparar_en(cfg, sid, lista, destino)
    assert [(p.nombre, p.ok, p.tratamiento) for p in preparados] == [
        ("slides.pdf", True, "pdf"), ("foto.jpg", True, "original"), ("cap.jpg", True, "convertida"),
        ("w.jpg", True, "convertida"), ("acostada.jpg", True, "rotada")]
    assert (destino / "slides.pdf").read_bytes() == (cfg.sesion_dir(sid) / "adjuntos" / "slides.pdf").read_bytes()
    assert (destino / "foto.jpg").read_bytes() == jpg
    with Image.open(destino / "cap.jpg") as img:
        assert img.format == "JPEG"
    with Image.open(destino / "acostada.jpg") as img:
        assert img.size == (30, 40)  # rotada de verdad
    assert sorted(p.name for p in destino.iterdir()) == ["acostada.jpg", "cap.jpg", "foto.jpg", "slides.pdf", "w.jpg"]


def test_preparar_heic(client: TestClient, cfg, tmp_path: Path):
    sid, lista = _guardados(client, cfg, [("IMG.heic", heic())])
    (p,) = preparar_en(cfg, sid, lista, tmp_path)
    assert (p.nombre, p.ok, p.tratamiento) == ("IMG.jpg", True, "convertida")


def test_preparar_exclusivo_no_pisa_y_avisa_faltantes(client: TestClient, cfg, tmp_path: Path):
    sid, lista = _guardados(client, cfg, [("a.pdf", pdf(1)), ("b.jpg", imagen("JPEG")), ("c.pdf", pdf(1))])
    (tmp_path / "a.pdf").write_bytes(b"del usuario")
    (tmp_path / "b.jpg").write_bytes(b"del usuario")
    (cfg.sesion_dir(sid) / "adjuntos" / "c.pdf").unlink()
    preparados = preparar_en(cfg, sid, lista, tmp_path, exclusivo=True)
    assert [p.ok for p in preparados] == [False, False, False]
    assert "no lo piso" in preparados[0].aviso and "no lo piso" in preparados[1].aviso
    assert "falta el archivo" in preparados[2].aviso
    assert (tmp_path / "a.pdf").read_bytes() == b"del usuario" and (tmp_path / "b.jpg").read_bytes() == b"del usuario"
    assert not any(p.name.startswith(".") for p in tmp_path.iterdir())


def test_preparar_imagen_rota(client: TestClient, cfg, tmp_path: Path):
    sid, lista = _guardados(client, cfg, [("rota.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64)])
    destino = tmp_path / "destino"
    (p,) = preparar_en(cfg, sid, lista, destino)
    assert not p.ok and "no se pudo abrir" in p.aviso
    assert not any(destino.iterdir())


def test_preparar_imagen_gigante_no_la_decodifica(client: TestClient, cfg, tmp_path: Path):
    # 144 MP: Pillow ni avisa (el tope es 300 MP), pero un PNG se decodifica entero.
    sid, lista = _guardados(client, cfg, [("bomba.png", png_gigante(12_000, 12_000)), ("ok.png", imagen("PNG"))])
    destino = tmp_path / "destino"
    bomba, ok = preparar_en(cfg, sid, lista, destino)
    assert not bomba.ok and "demasiado grande (12000×12000 px" in bomba.aviso
    assert ok.ok and [p.name for p in destino.iterdir()] == ["ok.jpg"]


def test_procesar_foto_corta_en_el_tope_de_pixeles(tmp_path: Path):
    # Entre 300 y 600 MP, Pillow sólo avisa: procesar_foto corta antes de decodificar.
    from classmer.pipeline.linea_de_tiempo import procesar_foto

    origen = tmp_path / "bomba.png"
    origen.write_bytes(png_gigante(20_000, 20_000))
    with pytest.warns(Image.DecompressionBombWarning), pytest.raises(Image.DecompressionBombError, match="400 MP"):
        procesar_foto(origen, tmp_path / "p.jpg")
    assert not (tmp_path / "p.jpg").exists()


# --- línea de tiempo ------------------------------------------------------------------------------

def _adj(aid: str, nombre: str, offset: int | None, tipo: str = "pdf", **extra) -> dict:
    return {"id": aid, "nombre": nombre, "nombre_original": extra.pop("original", nombre), "tipo": tipo,
            "offset_ms": offset, **extra}


BASE_TL = """# Clase: Materia — 2026-10-15 — teórica
[00:00:01] PROFE: hola
[00:00:10] PROFE: diez
[00:00:10] FOTO fotos/pizarra_01.jpg
[00:00:20] PROFE: veinte
# Fotos sin hora
FOTO fotos/pizarra_02.jpg
"""


def test_refrescar_timeline_ubica_y_es_idempotente():
    lista = [_adj("a", "Slides.pdf", 10_500, paginas=23, original='Slides "clase" 5.pdf'),
             _adj("b", "foto.png", 0, tipo="png"),
             _adj("c", "Apunte_profe.pdf", None, paginas=1),
             _adj("d", "z.pdf", 99_000, cifrado=True)]
    texto = refrescar_timeline(BASE_TL, lista, {"c": "no se pudo abrir"})
    assert texto.splitlines() == [
        "# Clase: Materia — 2026-10-15 — teórica",
        "[00:00:00] ADJUNTO adjuntos/foto.jpg (imagen PNG): \"foto.png\"",
        "[00:00:01] PROFE: hola",
        "[00:00:10] PROFE: diez",
        "[00:00:10] FOTO fotos/pizarra_01.jpg",
        "[00:00:10] ADJUNTO adjuntos/Slides.pdf (PDF, 23 páginas): \"Slides 'clase' 5.pdf\"",
        "[00:00:20] PROFE: veinte",
        "[00:01:39] ADJUNTO adjuntos/z.pdf (PDF cifrado): \"z.pdf\"",
        "# Fotos sin hora",
        "FOTO fotos/pizarra_02.jpg",
        "# Material adjunto",
        "ADJUNTO adjuntos/Apunte_profe.pdf (PDF, 1 página): \"Apunte_profe.pdf\" [no se pudo abrir: no está en adjuntos/]",
    ]
    assert refrescar_timeline(texto, lista, {"c": "no se pudo abrir"}) == texto
    # Sin adjuntos vuelve a la original; con otros, saca los viejos.
    assert refrescar_timeline(texto, []) == BASE_TL
    otro = refrescar_timeline(texto, [_adj("e", "nuevo.pdf", None)])
    assert "Slides.pdf" not in otro and otro.endswith("# Material adjunto\nADJUNTO adjuntos/nuevo.pdf (PDF): \"nuevo.pdf\"\n")
