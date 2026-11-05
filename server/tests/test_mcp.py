"""Servidor MCP (§11) en /mcp: protocolo, token, las 8 herramientas y el índice que arma publicar.

Las clases se publican con la etapa real `publicar` (en la carpeta temporal de la materia), así
también se prueba que publicar llena el índice de búsqueda.
"""

from __future__ import annotations

import io
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import TOKEN
from PIL import Image

from classmer import adjuntos, busqueda, sesiones
from classmer.cli import main
from classmer.cola import Job
from classmer.mcp import herramientas
from classmer.mcp.servidor import VERSIONES
from classmer.pipeline import actualizar_sesion
from classmer.pipeline import publicar as etapa_publicar
from classmer.util import ahora_iso, escribir_json

S1 = "0192f7c2-0000-7000-8000-000000000001"
S2 = "0192f7c3-0000-7000-8000-000000000002"
S3 = "0192f7c4-0000-7000-8000-000000000003"

TEX = r"""\documentclass{classmer}
\materia{Materia de Prueba}
\numeroclase{7}
\titulo{Derivada direccional y gradiente}
\begin{document}
\section{Derivada direccional \ts{00:00:10}}
La \textbf{derivada direccional} se define como un límite \ts{00:00:20}.

\section{Gradiente \ts{00:01:00}}
El gradiente es perpendicular a las curvas de nivel \ts{00:01:10}.
\end{document}
"""

META = {
    "titulo": "Derivada direccional y gradiente",
    "resumen": "Se definió la derivada direccional y se vio el gradiente.",
    "secciones": [
        {"titulo": "Derivada direccional", "inicio_ms": 10_000, "fin_ms": 60_000,
         "temas": ["derivada direccional", "límite"]},
        {"titulo": "Gradiente", "inicio_ms": 60_000, "fin_ms": 120_000,
         "temas": ["gradiente", "curvas de nivel"]},
    ],
    "items": [
        {"tipo": "parcial", "texto": "Entra la regla de la cadena", "offset_ms": 70_000, "fecha_limite": None},
        {"tipo": "tarea", "texto": "Ejercicios 1 a 5 de la guía 3", "offset_ms": 100_000,
         "fecha_limite": "2026-10-22"},
    ],
}

SEGMENTOS = [
    (10_000, 15_000, "PROFE", "Hoy vemos la derivada direccional."),
    (20_000, 28_000, "PROFE", "Se define como un límite cuando t tiende a cero."),
    (30_000, 35_000, "ALUMNO", "¿Y si el vector no es unitario?"),
    (61_000, 68_000, "PROFE", "El gradiente es perpendicular a las curvas de nivel."),
    (70_000, 76_000, "PROFE", "Esto lo tomo en el parcial: la regla de la cadena."),
    (100_000, 105_000, "PROFE", "Para la semana que viene, ejercicios uno a cinco."),
]


def publicar_clase(ctx, sid: str, *, materia_id: str = "prueba", inicio: str = "2026-10-15T14:00:05-03:00",
                   numero: int = 7, tex: str = TEX, meta: dict[str, Any] | None = None,
                   segmentos: list[tuple[int, int, str, str]] | None = None) -> Path:
    """Sesión con transcript y apunte v1 (tex, meta.json, PDF falso), publicada con la etapa real."""
    meta = META if meta is None else meta
    segmentos = SEGMENTOS if segmentos is None else segmentos
    cfg = ctx.cfg
    with ctx.base.conexion() as conn:
        sesiones.crear(cfg, conn, sesion_id=sid, materia_id=materia_id, origen_materia="manual",
                       tipo_clase="teorica", inicio=inicio, dispositivo="test")
        actualizar_sesion(conn, sid, cfg.zona_horaria, estado="procesando", etapa="publicar",
                          numero_clase=numero, duracion_ms=130_000)
        conn.executemany(
            "INSERT INTO segmentos (sesion_id, bloque, inicio_ms, fin_ms, hablante, texto) VALUES (?, 1, ?, ?, ?, ?)",
            [(sid, *s) for s in segmentos])
    carpeta = ctx.sesion_dir(sid)
    (carpeta / "transcript.md").write_text(
        "# Transcript: x\n" + "".join(f"[{s[0] // 1000:02d}] {s[2]}: {s[3]}\n" for s in segmentos), encoding="utf-8")
    v1 = carpeta / "apunte" / "v1"
    v1.mkdir(parents=True)
    (v1 / "apunte.tex").write_text(tex, encoding="utf-8")
    (v1 / "apunte.pdf").write_bytes(b"%PDF-1.5 falso")
    (v1 / "classmer.cls").write_text("% plantilla\n", encoding="utf-8")
    escribir_json(v1 / "meta.json", meta)
    momento = ahora_iso()
    with ctx.base.transaccion() as conn:
        apunte_id = conn.execute(
            "INSERT INTO apuntes (sesion_id, version, actual, motor, modelo, ruta_tex, ruta_pdf, compilo, titulo, "
            "resumen, creado) VALUES (?, 1, 1, 'fake', 'fake', 'apunte/v1/apunte.tex', 'apunte/v1/apunte.pdf', 1, "
            "?, ?, ?)", (sid, meta.get("titulo"), meta.get("resumen"), momento)).lastrowid
        for orden, s in enumerate(meta.get("secciones", []), 1):
            conn.execute("INSERT INTO secciones (apunte_id, orden, titulo, inicio_ms, fin_ms) VALUES (?, ?, ?, ?, ?)",
                         (apunte_id, orden, s["titulo"], s["inicio_ms"], s["fin_ms"]))
        for it in meta.get("items", []):
            conn.execute("INSERT INTO items (materia_id, sesion_id, apunte_id, tipo, texto, fecha_limite, offset_ms, "
                         "estado, creado) VALUES (?, ?, ?, ?, ?, ?, ?, 'abierto', ?)",
                         (materia_id, sid, apunte_id, it["tipo"], it["texto"], it["fecha_limite"],
                          it["offset_ms"], momento))
    etapa_publicar.ejecutar(ctx, Job(id=1, tipo="publicar", sesion_id=sid))
    return carpeta


@pytest.fixture
def clases(ctx):
    """S1: clase 7 de 'prueba' (publicada); S2: clase 1 de 'otra' (su carpeta no existe: no se publica
    afuera); S3: clase de 'prueba' todavía procesando (sin apunte)."""
    publicar_clase(ctx, S1)
    publicar_clase(
        ctx, S2, materia_id="otra", inicio="2026-10-14T10:00:00-03:00", numero=1,
        tex="\\documentclass{classmer}\\begin{document}\\section{Repaso}Repasamos derivadas parciales.\\end{document}",
        meta={"titulo": "Repaso", "resumen": "Repaso de derivadas parciales.",
              "secciones": [{"titulo": "Repaso", "inicio_ms": 0, "fin_ms": 50_000, "temas": ["Gradiente"]}],
              "items": [{"tipo": "duda", "texto": "No entendí el teorema", "offset_ms": 5_000, "fecha_limite": None}]},
        segmentos=[(1_000, 4_000, "PROFE", "Repasamos derivadas parciales y el gradiente.")],
    )
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=S3, materia_id="prueba", origen_materia="manual",
                       tipo_clase="practica", inicio="2026-10-16T14:00:00-03:00", dispositivo="test")
        actualizar_sesion(conn, S3, ctx.cfg.zona_horaria, estado="procesando", etapa="transcribir")
    return ctx


# --- helpers JSON-RPC ------------------------------------------------------------------------------

def rpc(client, metodo: str, params: dict[str, Any] | None = None, id_: int = 1) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": metodo}
    if params is not None:
        cuerpo["params"] = params
    r = client.post("/mcp", json=cuerpo, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    respuesta = r.json()
    assert respuesta["jsonrpc"] == "2.0" and respuesta["id"] == id_
    return respuesta


def llamar(client, nombre: str, **args: Any) -> dict[str, Any]:
    return rpc(client, "tools/call", {"name": nombre, "arguments": args})["result"]


def datos(client, nombre: str, **args: Any) -> dict[str, Any]:
    res = llamar(client, nombre, **args)
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


def error(client, nombre: str, **args: Any) -> str:
    res = llamar(client, nombre, **args)
    assert res["isError"] is True, res
    return res["content"][0]["text"]


def sin_bytes(archivos: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in archivos.items() if k != "bytes"}


def tamanos(archivos: dict[str, Any]) -> dict[str, Any]:
    """Lo que tiene que dar `archivos.bytes`: el tamaño de cada archivo que está (None si no)."""
    def tam(ruta: str | None) -> int | None:
        return Path(ruta).stat().st_size if ruta else None
    return {**{k: tam(archivos[k]) for k in ("pdf", "tex", "transcript", "audio", "audio_panel", "audio_celu")},
            "adjuntos": [tam(r) for r in archivos["adjuntos"]]}


# --- protocolo -------------------------------------------------------------------------------------

def test_initialize_negocia_la_version(client):
    res = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1"}})["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"]["name"] == "classmer"
    assert "cita" in res["instructions"]
    otra = rpc(client, "initialize", {"protocolVersion": "2099-01-01"})["result"]
    assert otra["protocolVersion"] == VERSIONES[0]
    # La notificación de vuelta no lleva respuesta.
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    assert rpc(client, "ping")["result"] == {}


def test_tools_list(client):
    tools = rpc(client, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["listar_materias", "listar_clases", "leer_apunte", "transcript",
                                          "buscar", "temas", "pendientes", "marcar_item"]
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["description"]
        assert t["annotations"]["readOnlyHint"] is (t["name"] != "marcar_item")
    buscar = next(t for t in tools if t["name"] == "buscar")
    assert buscar["inputSchema"]["required"] == ["texto"]


def test_pide_el_token(client):
    cuerpo = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer otro"}, {"Authorization": TOKEN}):
        r = client.post("/mcp", json=cuerpo, headers=headers)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/mcp", headers={"Authorization": "Bearer otro"}).status_code == 401


def test_origen(client):
    cuerpo = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert client.post("/mcp", json=cuerpo, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/mcp", json=cuerpo, headers={"Origin": "http://localhost:6274"}).status_code == 200


def test_get_y_delete_dan_405(client):
    for metodo in ("GET", "DELETE"):
        r = client.request(metodo, "/mcp")
        assert r.status_code == 405 and r.headers["allow"] == "POST"


def test_barra_final(client):
    r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert r.status_code == 200 and r.json()["result"] == {}


def test_errores_jsonrpc(client):
    r = client.post("/mcp", content=b"{no es json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    assert client.post("/mcp", json=[]).status_code == 400
    assert rpc(client, "no/existe")["error"]["code"] == -32601
    r = client.post("/mcp", json={"jsonrpc": "1.0", "id": 9, "method": "ping"})
    assert r.json() == {"jsonrpc": "2.0", "id": 9, "error": {"code": -32600, "message": "pedido JSON-RPC 2.0 inválido"}}
    res = rpc(client, "tools/call", {"name": "borrar_todo", "arguments": {}})
    assert res["error"]["code"] == -32602
    # Una respuesta del cliente (sin method) se ignora.
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "result": {}}).status_code == 202


def test_lote(client):
    r = client.post("/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert r.status_code == 200
    assert [m["id"] for m in r.json()] == [1, 2]


def test_argumentos_invalidos_vuelven_como_error_de_herramienta(client):
    assert "argumentos inválidos" in error(client, "marcar_item", id=1, estado="borrado")
    assert "argumentos inválidos" in error(client, "listar_materias", extra=1)
    assert "required" in error(client, "leer_apunte")
    res = rpc(client, "tools/call", {"name": "listar_materias", "arguments": [1]})["result"]
    assert res["isError"] is True


def test_un_error_interno_no_tira_el_server(client, monkeypatch):
    def explota(_e):
        raise RuntimeError("se rompió")

    monkeypatch.setattr(herramientas.HERRAMIENTAS["listar_materias"], "funcion", explota)
    assert "RuntimeError: se rompió" in error(client, "listar_materias")
    assert rpc(client, "ping")["result"] == {}


# --- herramientas ----------------------------------------------------------------------------------

def test_listar_materias(client, clases, materia_dir):
    d = datos(client, "listar_materias")
    por_id = {m["id"]: m for m in d["materias"]}
    assert set(por_id) == {"prueba", "otra"}
    prueba = por_id["prueba"]
    assert prueba["nombre"] == "Materia de Prueba" and prueba["activa"] is True
    assert prueba["clases"] == {"total": 2, "listas": 1, "en_curso": 1, "esperando_usuario": 0, "con_error": 0}
    assert prueba["pendientes_abiertos"] == 2
    assert prueba["carpeta_clases"] == str(materia_dir / "Clases-Classmer")
    assert prueba["ultima_clase"]["sesion"] == S3 and prueba["ultima_clase"]["estado"] == "procesando"
    assert por_id["otra"]["clases"]["listas"] == 1


def test_listar_clases(client, clases, materia_dir):
    d = datos(client, "listar_clases", materia="materia de PRUEBA")  # por nombre, sin importar mayúsculas
    assert d["materia"] == "prueba" and d["total"] == 2
    c7, c8 = d["clases"]  # orden cronológico
    assert c7["sesion"] == S1 and c8["sesion"] == S3
    assert c7["numero"] == 7 and c7["fecha"] == "2026-10-15" and c7["titulo"] == META["titulo"]
    assert c7["estado"] == "lista" and "etapa" not in c7
    assert c7["resumen"] == META["resumen"]
    assert c7["temas"] == ["derivada direccional", "límite", "gradiente", "curvas de nivel"]
    assert c7["pendientes_abiertos"] == 2
    assert c7["cita"] == "Materia de Prueba, clase 7 (2026-10-15)"
    carpeta = materia_dir / "Clases-Classmer" / "2026-10-15 Clase 07 - Derivada direccional y gradiente"
    assert sin_bytes(c7["archivos"]) == {
        "publicado": True, "carpeta": str(carpeta), "pdf": str(carpeta / "apunte.pdf"),
        "tex": str(carpeta / "apunte.tex"), "transcript": str(carpeta / "transcript.md"), "adjuntos": [],
        "audio": None, "audio_panel": None, "audio_celu": None}
    assert c7["archivos"]["bytes"] == tamanos(c7["archivos"])
    assert all(Path(c7["archivos"][k]).is_absolute() for k in ("pdf", "tex", "transcript"))
    assert c8["estado"] == "procesando" and c8["etapa"] == "transcribir" and c8["archivos"]["pdf"] is None

    # Filtros por fecha y todas las materias.
    assert [c["sesion"] for c in datos(client, "listar_clases", desde="2026-10-16")["clases"]] == [S3]
    assert [c["sesion"] for c in datos(client, "listar_clases", hasta="2026-10-14")["clases"]] == [S2]
    todas = datos(client, "listar_clases", limite=2)
    assert todas["total"] == 3 and [c["sesion"] for c in todas["clases"]] == [S1, S3] and "aviso" in todas
    assert "AAAA-MM-DD" in error(client, "listar_clases", desde="15/10")
    assert "no existe la materia" in error(client, "listar_clases", materia="quimica")


def test_listar_clases_sin_publicar_afuera_da_las_rutas_de_classmer_data(client, clases, cfg):
    (c,) = datos(client, "listar_clases", materia="otra")["clases"]
    v1 = cfg.sesion_dir(S2) / "apunte" / "v1"
    assert sin_bytes(c["archivos"]) == {
        "publicado": False, "carpeta": None, "pdf": str(v1 / "apunte.pdf"), "tex": str(v1 / "apunte.tex"),
        "transcript": str(cfg.sesion_dir(S2) / "transcript.md"), "adjuntos": [],
        "audio": None, "audio_panel": None, "audio_celu": None}
    assert c["archivos"]["bytes"] == tamanos(c["archivos"])


def test_archivos_trae_los_audios_y_los_tamanos(client, clases, cfg):
    """Para que un bot de mensajería mande el audio (§12): los audios viven siempre en Classmer-data, y `bytes` da el
    tamaño de cada archivo sin abrirlo."""
    s1, s2 = cfg.sesion_dir(S1), cfg.sesion_dir(S2)
    (s1 / "audio.ogg").write_bytes(b"r" * 1000)
    (s1 / "audio.panel.ogg").write_bytes(b"p" * 300)
    (s1 / "audio.celu.ogg").write_bytes(b"c" * 700)
    # El original de una importada se llama como su asset.
    with clases.base.conexion() as conn:
        conn.execute("UPDATE assets SET ruta = 'audio.m4a' WHERE id = ?", (f"audio:{S2}",))
    (s2 / "audio.m4a").write_bytes(b"m" * 50)
    (s2 / "audio.ogg").write_bytes(b"no es el de la clase")
    por_sesion = {c["sesion"]: c["archivos"] for c in datos(client, "listar_clases")["clases"]}
    a1, a2 = por_sesion[S1], por_sesion[S2]
    assert (a1["audio"], a1["audio_panel"], a1["audio_celu"]) == (
        str(s1 / "audio.ogg"), str(s1 / "audio.panel.ogg"), str(s1 / "audio.celu.ogg"))
    assert (a1["bytes"]["audio"], a1["bytes"]["audio_panel"], a1["bytes"]["audio_celu"]) == (1000, 300, 700)
    assert a2["audio"] == str(s2 / "audio.m4a") and a2["bytes"]["audio"] == 50
    assert a2["audio_panel"] is None and a2["bytes"]["audio_panel"] is None
    # Lo mismo en leer_apunte.
    meta = json.loads(llamar(client, "leer_apunte", sesion=S1)["content"][0]["text"])
    assert meta["archivos"]["audio"] == str(s1 / "audio.ogg") and meta["archivos"]["bytes"]["audio"] == 1000


def test_leer_apunte(client, clases):
    res = llamar(client, "leer_apunte", sesion=S1[:8])  # alcanza un prefijo único
    assert res["isError"] is False and len(res["content"]) == 2
    meta = json.loads(res["content"][0]["text"])
    assert res["content"][1]["text"] == TEX
    assert meta["sesion"] == S1 and meta["materia"] == "prueba" and meta["numero"] == 7
    assert meta["apunte"]["version"] == 1 and meta["apunte"]["compilo"] is True
    gradiente = meta["meta"]["secciones"][1]
    assert gradiente["titulo"] == "Gradiente" and gradiente["minuto"] == "00:01:00"
    assert gradiente["cita"] == "Materia de Prueba, clase 7 (2026-10-15), min 00:01:00"
    assert meta["meta"]["items"][0]["minuto"] == "00:01:10"
    assert meta["archivos"]["tex"].endswith("apunte.tex")

    assert "todavía no tiene apunte" in error(client, "leer_apunte", sesion=S3)
    assert "no existe la sesión" in error(client, "leer_apunte", sesion="nada")
    assert "varias sesiones" in error(client, "leer_apunte", sesion="0192f7c")


def test_transcript(client, clases):
    res = llamar(client, "transcript", sesion=S1, desde=0.3, hasta="1:05")  # 18 s y mm:ss
    d = json.loads(res["content"][0]["text"])
    assert res["content"][1]["text"].splitlines() == [
        "[00:00:20] PROFE: Se define como un límite cuando t tiende a cero.",
        "[00:00:30] ALUMNO: ¿Y si el vector no es unitario?",
        "[00:01:01] PROFE: El gradiente es perpendicular a las curvas de nivel.",
    ]
    assert d["desde_ms"] == 20_000 and d["hasta_ms"] == 68_000 and d["segmentos"] == 3
    assert d["cita"] == "Materia de Prueba, clase 7 (2026-10-15), min 00:00:20"
    assert d["archivos"]["transcript"].endswith("transcript.md") and "truncado" not in d
    # Sin rango: la clase entera.
    entero = llamar(client, "transcript", sesion=S1)["content"][1]["text"].splitlines()
    assert len(entero) == len(SEGMENTOS)
    # Un tramo sin habla.
    assert "no hay habla" in llamar(client, "transcript", sesion=S1, desde="00:01:50", hasta=3)["content"][1]["text"]
    assert "hasta tiene que ser mayor" in error(client, "transcript", sesion=S1, desde=5, hasta=2)
    assert "desde inválido" in error(client, "transcript", sesion=S1, desde="ayer")
    assert "todavía no tiene transcript" in error(client, "transcript", sesion=S3)


def test_transcript_largo_se_corta(client, clases, monkeypatch):
    monkeypatch.setattr(herramientas, "MAX_TRANSCRIPT_CHARS", 120)
    res = llamar(client, "transcript", sesion=S1)
    d = json.loads(res["content"][0]["text"])
    assert d["truncado"] is True and d["seguir_desde"] == "00:00:30"
    assert len(res["content"][1]["text"].splitlines()) == 2


def test_publicar_indexa_la_clase(clases):
    with clases.base.conexion() as conn:
        filas = conn.execute("SELECT tipo, materia, inicio_ms FROM busqueda WHERE ref = ? ORDER BY tipo, inicio_ms",
                             (S1,)).fetchall()
    tipos = {f["tipo"] for f in filas}
    assert tipos == {"apunte", "transcript"}
    assert {f["materia"] for f in filas} == {"prueba"}
    assert (S3,) not in [(f["materia"],) for f in filas]  # sin apunte no se indexa


def test_buscar(client, clases):
    d = datos(client, "buscar", texto="limite tiende")  # sin tilde
    (r,) = d["resultados"]
    assert r["origen"] == "transcript" and r["sesion"] == S1 and r["materia"] == "prueba" and r["numero"] == 7
    assert r["inicio_ms"] == 10_000 and r["minuto"] == "00:00:10"
    assert r["seccion"] == "Derivada direccional"
    assert r["cita"] == "Materia de Prueba, clase 7 (2026-10-15), min 00:00:10"
    assert "«límite»" in r["fragmento"]
    assert r["archivos"]["pdf"].endswith("apunte.pdf")

    # Apunte y transcript; acentos y plurales.
    d = datos(client, "buscar", texto="perpendiculares")
    assert {(x["origen"], x["sesion"]) for x in d["resultados"]} == {("apunte", S1), ("transcript", S1)}

    # Filtro por materia.
    d = datos(client, "buscar", texto="gradiente", materia="otra")
    assert {x["sesion"] for x in d["resultados"]} == {S2}

    # Coincidencias parciales con aviso; sin resultados.
    d = datos(client, "buscar", texto="gradiente fubini")
    assert d["resultados"] and "parciales" in d["aviso"]
    d = datos(client, "buscar", texto="fubini")
    assert d["resultados"] == [] and "sin resultados" in d["aviso"]

    # Frase exacta.
    assert datos(client, "buscar", texto='"regla de la cadena"')["resultados"]
    assert not datos(client, "buscar", texto='"cadena de la regla"')["resultados"]
    assert "falta el texto" in error(client, "buscar", texto="  ")


def test_buscar_por_tema(client, clases):
    # "gradiente" está en las dos clases; con tema=límite sólo cuenta la primera sección de S1.
    d = datos(client, "buscar", texto="derivada", tema="LIMITE")
    assert d["resultados"] and all(x["sesion"] == S1 and x["inicio_ms"] < 60_000 for x in d["resultados"])
    d = datos(client, "buscar", texto="gradiente", tema="gradiente")
    assert {x["sesion"] for x in d["resultados"]} == {S1, S2}
    d = datos(client, "buscar", texto="gradiente", tema="topología")
    assert d["resultados"] == [] and "ninguna sección" in d["aviso"]


def test_buscar_arma_el_indice_si_esta_vacio(client, clases):
    with clases.base.conexion() as conn:
        conn.execute("DELETE FROM busqueda")
    assert datos(client, "buscar", texto="perpendicular")["resultados"]


def test_buscar_ignora_sesiones_borradas(client, clases):
    with clases.base.conexion() as conn:
        conn.execute("INSERT INTO busqueda (texto, materia, tipo, ref, inicio_ms) "
                     "VALUES ('perpendicular huérfana', 'prueba', 'transcript', 'borrada', 0)")
    assert {x["sesion"] for x in datos(client, "buscar", texto="perpendicular")["resultados"]} == {S1}


def test_temas(client, clases):
    d = datos(client, "temas", materia="prueba")
    assert d["fuente"] == "meta.json" and "aviso" in d
    assert [t["tema"] for t in d["temas"]] == ["derivada direccional", "límite", "gradiente", "curvas de nivel"]
    gradiente = d["temas"][2]["clases"][0]
    assert gradiente["sesion"] == S1 and gradiente["seccion"] == "Gradiente" and gradiente["minuto"] == "00:01:00"
    assert gradiente["cita"] == "Materia de Prueba, clase 7 (2026-10-15), min 00:01:00"
    assert "required" in error(client, "temas")


def test_temas_con_programa(client, clases):
    with clases.base.conexion() as conn:
        conn.execute("INSERT INTO temas (id, materia_id, unidad, nombre, descripcion, sinonimos, estado) "
                     "VALUES ('u1-grad', 'prueba', 'Unidad 1', 'Vector gradiente', NULL, ?, 'aprobado')",
                     (json.dumps(["gradiente"]),))
        conn.execute("INSERT INTO temas (id, materia_id, unidad, nombre, estado) "
                     "VALUES ('u2-int', 'prueba', 'Unidad 2', 'Integral doble', 'aprobado')")
    d = datos(client, "temas", materia="prueba")
    assert d["fuente"] == "programa"
    grad, integral = d["temas"]
    assert grad["tema"] == "Vector gradiente" and [c["sesion"] for c in grad["clases"]] == [S1]
    assert integral["clases"] == []
    assert [t["tema"] for t in d["fuera_del_programa"]] == ["derivada direccional", "límite", "curvas de nivel"]


def test_pendientes_y_marcar_item(client, clases):
    d = datos(client, "pendientes", materia="prueba")
    assert d["total"] == 2 and d["por_tipo"] == {"tarea": 1, "parcial": 1}
    tarea, parcial = d["items"]  # primero los que tienen fecha límite
    assert tarea["tipo"] == "tarea" and tarea["fecha_limite"] == "2026-10-22"
    assert parcial["texto"] == "Entra la regla de la cadena" and parcial["minuto"] == "00:01:10"
    assert parcial["sesion"] == S1 and parcial["numero"] == 7
    assert parcial["cita"] == "Materia de Prueba, clase 7 (2026-10-15), min 00:01:10"
    assert [i["tipo"] for i in datos(client, "pendientes")["items"]] == ["tarea", "parcial", "duda"]
    assert [i["tipo"] for i in datos(client, "pendientes", tipo="duda")["items"]] == ["duda"]

    m = datos(client, "marcar_item", id=str(parcial["id"]), estado="hecho")
    assert m["estado_anterior"] == "abierto" and m["item"]["estado"] == "hecho" and m["item"]["sesion"] == S1
    assert [i["id"] for i in datos(client, "pendientes", materia="prueba")["items"]] == [tarea["id"]]
    assert [i["id"] for i in datos(client, "pendientes", materia="prueba", estado="hecho")["items"]] == [parcial["id"]]
    assert datos(client, "pendientes", materia="prueba", estado="todos")["total"] == 2
    assert datos(client, "marcar_item", id=parcial["id"], estado="abierto")["item"]["estado"] == "abierto"

    assert "no existe el item" in error(client, "marcar_item", id=999, estado="hecho")
    assert "id de item inválido" in error(client, "marcar_item", id="abc", estado="hecho")


# --- reindexar y publicar --------------------------------------------------------------------------

def test_cli_reindexar(clases, capsys):
    with clases.base.conexion() as conn:
        conn.execute("DELETE FROM busqueda")
        conn.execute("INSERT INTO busqueda (texto, materia, tipo, ref, inicio_ms) "
                     "VALUES ('vieja', 'prueba', 'transcript', 'borrada', 0)")
    assert main(["reindexar"]) == 0
    assert "2 clases" in capsys.readouterr().out
    with clases.base.conexion() as conn:
        refs = {f[0] for f in conn.execute("SELECT DISTINCT ref FROM busqueda")}
    assert refs == {S1, S2}


def test_publicar_sigue_si_falla_el_indice(ctx, monkeypatch):
    def falla(*_a, **_k):
        raise RuntimeError("disco lleno")

    monkeypatch.setattr(busqueda, "indexar_sesion", falla)
    carpeta = publicar_clase(ctx, S1)
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT estado FROM sesiones WHERE id = ?", (S1,)).fetchone()[0] == "lista"
    assert "no pude indexarla" in (carpeta / "pipeline.log").read_text(encoding="utf-8")


def test_republicar_reemplaza_las_filas(clases):
    with clases.base.conexion() as conn:
        antes = conn.execute("SELECT COUNT(*) FROM busqueda WHERE ref = ?", (S1,)).fetchone()[0]
        busqueda.indexar_sesion(clases.cfg, conn, S1)
        despues = conn.execute("SELECT COUNT(*) FROM busqueda WHERE ref = ?", (S1,)).fetchone()[0]
    assert antes == despues > 0


# --- adjuntos --------------------------------------------------------------------------------------

def adjuntar(ctx, sid: str, nombre: str, contenido: bytes) -> dict[str, Any]:
    _, info = adjuntos.guardar(ctx.cfg, ctx.base, sid, adjunto_id=str(uuid.uuid4()), archivo=io.BytesIO(contenido),
                               nombre_original=nombre)
    return info


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 10), (0, 0, 200)).save(buf, "PNG")
    return buf.getvalue()


def test_archivos_trae_los_adjuntos(client, clases, cfg, materia_dir, monkeypatch):
    """Los publicados (adjuntos/ en la carpeta de la clase) si están; si no, los originales de
    Classmer-data; [] si la clase no tiene."""
    adjuntar(clases, S1, "Slides clase 7.pdf", b"%PDF-1.4 slides")
    adjuntar(clases, S1, "Pizarrón.png", png())  # llegó después de publicar: no está en la carpeta
    adjuntar(clases, S2, "Notas del profe.pdf", b"%PDF-1.4 notas")  # 'otra' no se publica afuera
    publicada = materia_dir / "Clases-Classmer" / "2026-10-15 Clase 07 - Derivada direccional y gradiente"
    copia = publicada / "adjuntos" / "Slides_clase_7.pdf"  # el nombre para LaTeX, como lo copia publicar
    copia.parent.mkdir(exist_ok=True)
    shutil.copyfile(cfg.sesion_dir(S1) / "adjuntos" / "Slides_clase_7.pdf", copia)
    de_s1 = [str(copia), str(cfg.sesion_dir(S1) / "adjuntos" / "Pizarron.png")]
    de_s2 = [str(cfg.sesion_dir(S2) / "adjuntos" / "Notas_del_profe.pdf")]
    assert all(Path(r).is_absolute() and Path(r).is_file() for r in de_s1 + de_s2)

    # listar_clases: una sola consulta de adjuntos para toda la página, no una por clase.
    consultas = []
    original = herramientas.adjuntos_de

    def espia(conn, ids):
        ids = list(ids)
        consultas.append(ids)
        return original(conn, ids)

    monkeypatch.setattr(herramientas, "adjuntos_de", espia)
    d = datos(client, "listar_clases")
    assert {c["sesion"]: c["archivos"]["adjuntos"] for c in d["clases"]} == {S1: de_s1, S2: de_s2, S3: []}
    assert len(consultas) == 1 and set(consultas[0]) == {S1, S2, S3}

    # leer_apunte y buscar traen lo mismo; transcript, sólo el transcript.
    meta = json.loads(llamar(client, "leer_apunte", sesion=S1)["content"][0]["text"])
    assert meta["archivos"]["adjuntos"] == de_s1 and meta["archivos"]["publicado"] is True
    assert json.loads(llamar(client, "leer_apunte", sesion=S2)["content"][0]["text"])["archivos"]["adjuntos"] == de_s2
    consultas.clear()
    resultados = datos(client, "buscar", texto="gradiente")["resultados"]
    assert {r["sesion"] for r in resultados} == {S1, S2} and len(consultas) == 1
    assert all(r["archivos"]["adjuntos"] == (de_s1 if r["sesion"] == S1 else de_s2) for r in resultados)
    assert list(json.loads(llamar(client, "transcript", sesion=S1)["content"][0]["text"])["archivos"]) == ["transcript"]

    # Si se borra la copia publicada, vuelve el original; si falta también el original, no va.
    copia.unlink()
    (cfg.sesion_dir(S1) / "adjuntos" / "Pizarron.png").unlink()
    assert datos(client, "listar_clases", materia="prueba")["clases"][0]["archivos"]["adjuntos"] == [
        str(cfg.sesion_dir(S1) / "adjuntos" / "Slides_clase_7.pdf")]


def test_herramientas_mencionan_los_adjuntos(client):
    tools = {t["name"]: t for t in rpc(client, "tools/list")["result"]["tools"]}
    assert "adjuntos" in tools["leer_apunte"]["description"] and "adjuntos" in tools["listar_clases"]["description"]
    assert "adjuntos" in rpc(client, "initialize", {"protocolVersion": VERSIONES[0]})["result"]["instructions"]
