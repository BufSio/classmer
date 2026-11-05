"""Etapa apunte con un agente FALSO (no se llama a Claude): carpeta de trabajo, prompt, compilación
real con pdflatex (marca latex), filas en la base, versiones, meta.json reparado y "no compiló"."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock
from PIL import Image

from classmer import cola, latex, sesiones
from classmer.cola import Job
from classmer.config import ModeloApunte, cargar_config
from classmer.ia.agente_apunte import ESPERA_SOBRECARGA_S, AgenteClaude, ResultadoAgente, TrabajoApunte
from classmer.ia.facturacion import SinCreditos
from classmer.pipeline import CuotaAgotada, ErrorEtapa, Servicios, actualizar_sesion
from classmer.pipeline import apunte as etapa
from classmer.pipeline import publicar
from classmer.util import ahora_iso
from conftest import requiere

SID = "apu-0001"
INICIO = "2026-10-15T14:00:05-03:00"
TIMELINE = (
    "# Clase: Materia de Prueba — 2026-10-15 — teórica\n"
    "[00:00:05] PROFE: Repasemos lo de la clase pasada.\n"
    "[00:00:30] FOTO fotos/pizarra_01.jpg\n"
    "[00:01:00] PROFE: El gradiente apunta hacia donde la función crece más rápido.\n"
    "[00:01:05] MARCA importante\n"
    "[00:01:30] PROFE: Para el parcial entra hasta gradiente.\n"
)
TEX = r"""\documentclass{classmer}
\materia{Materia de Prueba}
\fechaclase{15 de octubre de 2026}
\numeroclase{1}
\tipoclase{Teórica}
\titulo{Derivada direccional y gradiente}
\begin{document}
\section{Repaso \ts{00:00:05}}
Se repasó la clase anterior \ts{00:00:10}.
\pizarra{pizarra_01.jpg}{00:00:30}{La pizarra al empezar}
\section{Gradiente \ts{00:01:00}}
\begin{intuicion}[Para recordar]
El gradiente apunta hacia donde $f$ crece más rápido.
\end{intuicion}
\begin{parcial}[Primer parcial]
Entra hasta gradiente \ts{00:01:30}.
\end{parcial}
\end{document}
"""
META = {
    "titulo": "Derivada direccional y gradiente",
    "resumen": "Se repasó la clase anterior y se vio el gradiente.",
    "secciones": [
        {"titulo": "Repaso", "inicio_ms": 5000, "fin_ms": 60000, "temas": ["repaso"]},
        {"titulo": "Gradiente", "inicio_ms": 60000, "fin_ms": 120000, "temas": ["gradiente"],
         "texto_plano": "El gradiente apunta hacia donde f crece más rápido."},
    ],
    "items": [
        {"tipo": "parcial", "texto": "Entra hasta gradiente", "offset_ms": 90000, "fecha_limite": None},
        {"tipo": "tarea", "texto": "Ejercicios 1 a 3", "offset_ms": 100000, "fecha_limite": "2026-10-22"},
    ],
}


class AgenteFalso:
    """Escribe output/apunte.tex y output/meta.json como lo haría el agente. Con `fallas`, las
    primeras corridas dejan algo a medias en output/ y lanzan esas excepciones (una por corrida)."""

    def __init__(self, cfg: Any, *, tex: str | None = TEX, meta: Any = META, error: Exception | None = None,
                 fallas: list[Exception] | None = None):
        self.cfg = cfg
        self.tex = tex
        self.meta = meta
        self.error = error
        self.fallas = list(fallas or [])
        self.trabajos: list[TrabajoApunte] = []
        self.archivos: list[set[str]] = []
        self.errores_antes: list[str] = []

    @property
    def medios(self) -> list[str]:
        return [t.facturacion for t in self.trabajos]

    def ejecutar(self, trabajo: TrabajoApunte) -> ResultadoAgente:
        self.trabajos.append(trabajo)
        w = Path(trabajo.workdir)
        self.archivos.append({p.relative_to(w).as_posix() for p in w.rglob("*") if p.is_file()})
        self.errores_antes = trabajo.validar_meta()
        if self.error:
            raise self.error
        salida = w / "output"
        if self.fallas:
            (salida / "a_medias.tex").write_text("\\documentclass{classmer}\n% a medias\n", encoding="utf-8")
            raise self.fallas.pop(0)
        if self.tex is not None:
            (salida / "apunte.tex").write_text(self.tex, encoding="utf-8")
        if self.meta is not None:
            texto = self.meta if isinstance(self.meta, str) else json.dumps(self.meta, ensure_ascii=False)
            (salida / "meta.json").write_text(texto, encoding="utf-8")
        return ResultadoAgente(ok=True, modelo="claude-falso", costo_usd=0.12, intentos_compilacion=1, turnos=7,
                               log="Agente: claude-falso · 7 turnos\nCompilaciones: 1 de 5\n\n--- detalle ---\nnada",
                               error=None)


def usar(ctx, agente: AgenteFalso) -> AgenteFalso:
    ctx.servicios = Servicios(agente_apunte=lambda cfg: agente)
    return agente


def armar(ctx, sid: str = SID, *, inicio: str = INICIO, materia_id: str | None = "prueba",
          estado: str = "procesando", fotos: bool = True) -> Path:
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id=materia_id, origen_materia="manual",
                       tipo_clase="teorica", inicio=inicio, dispositivo="test")
        # Sin número fijo (como una sesión en Auto o anterior a v0.2): lo calcula la etapa.
        actualizar_sesion(conn, sid, ctx.cfg.zona_horaria, estado=estado, etapa="apunte", duracion_ms=120_000,
                          numero_clase=None)
    carpeta = ctx.sesion_dir(sid)
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "timeline.md").write_text(TIMELINE, encoding="utf-8")
    (carpeta / "transcript.json").write_text(json.dumps([
        {"bloque": 1, "inicio_ms": 5000, "fin_ms": 9000, "hablante": "PROFE", "texto": "Repasemos"}]), encoding="utf-8")
    if fotos:
        (carpeta / "fotos_pdf").mkdir(exist_ok=True)
        Image.new("RGB", (64, 48), (200, 220, 255)).save(carpeta / "fotos_pdf" / "pizarra_01.jpg", "JPEG")
    return carpeta


def job(sid: str = SID) -> Job:
    return Job(id=1, tipo="apunte", sesion_id=sid)


def filas(ctx, sql: str, *args: Any) -> list[dict[str, Any]]:
    with ctx.base.conexion() as conn:
        return [dict(f) for f in conn.execute(sql, args).fetchall()]


@pytest.fixture
def compilar_falso(monkeypatch):
    """Sustituye la compilación del server (para los tests que no necesitan MiKTeX)."""
    llamadas: list[Path] = []

    def falso(carpeta: Path, archivo: str = "apunte.tex", **_kw: Any) -> latex.ResultadoCompilacion:
        llamadas.append(Path(carpeta))
        pdf = Path(carpeta) / "apunte.pdf"
        pdf.write_bytes(b"%PDF-1.5 falso")
        log = Path(carpeta) / "apunte.log"
        log.write_text("This is pdfTeX (falso)\nOutput written on apunte.pdf (1 page).\n", encoding="utf-8")
        return latex.ResultadoCompilacion(ok=True, pdf=pdf, log="pdflatex compiló bien (falso).", ruta_log=log)

    monkeypatch.setattr(latex, "compilar", falso)
    return llamadas


# --- carpeta de trabajo y prompt -----------------------------------------------------------------

def test_carpeta_de_trabajo_y_prompt(ctx, materia_dir, compilar_falso):
    ctx.cfg.estilo_global = "Gráficos siempre que se pueda."
    ctx.cfg.materias[0].estilo = "Vectores en negrita, como en la cátedra."
    carpeta = armar(ctx)
    workdir = ctx.job_dir(SID, "apunte")
    (workdir / "output").mkdir(parents=True)
    (workdir / "output" / "viejo.txt").write_text("de una corrida anterior", encoding="utf-8")
    agente = usar(ctx, AgenteFalso(ctx.cfg))

    r = etapa.ejecutar(ctx, job())
    assert r.siguientes == [("publicar", {})]
    assert r.costo == 0.12 and r.motor == "claude-agent/claude-falso"

    assert agente.archivos[0] == {
        "input/timeline.md", "input/transcript.json", "input/fotos/pizarra_01.jpg", "input/perfil.md",
        "input/estilo.md", "input/clase_anterior.md", "input/contexto.json", "input/adjuntos.json",
        "plantilla/classmer.cls", "plantilla/ejemplo.tex", "output/classmer.cls",
    }
    entrada = workdir / "input"
    assert (entrada / "timeline.md").read_text(encoding="utf-8") == TIMELINE
    # Sin adjuntos: el manifiesto igual dice que esta corrida no tomó ninguno (y no hay input/adjuntos/).
    assert json.loads((entrada / "adjuntos.json").read_text(encoding="utf-8")) == {"job": 1, "adjuntos": []}
    assert not (entrada / "adjuntos").exists()
    assert (entrada / "fotos" / "pizarra_01.jpg").read_bytes() == (carpeta / "fotos_pdf" / "pizarra_01.jpg").read_bytes()
    assert (entrada / "perfil.md").read_text(encoding="utf-8") == (materia_dir / "PROMPT-AGENTE.md").read_text(encoding="utf-8")
    estilo = (entrada / "estilo.md").read_text(encoding="utf-8")
    assert "Gráficos siempre" in estilo and "Vectores en negrita" in estilo
    assert "primera clase" in (entrada / "clase_anterior.md").read_text(encoding="utf-8")
    assert json.loads((entrada / "contexto.json").read_text(encoding="utf-8")) == {
        "materia_id": "prueba", "materia": "Materia de Prueba", "fecha": "2026-10-15",
        "fecha_larga": "15 de octubre de 2026", "numero_clase": 1, "tipo_clase": "teorica",
        "duracion_ms": 120_000, "inicio": INICIO,
    }
    assert (workdir / "output" / "classmer.cls").read_bytes() == latex.RUTA_CLS.read_bytes()
    assert not (workdir / "output" / "viejo.txt").exists()  # la carpeta se rearma desde cero

    t = agente.trabajos[0]
    assert t.workdir == workdir and t.carpeta_materia == materia_dir
    # Modelo y esfuerzo globales por defecto; sin ANTHROPIC_API_KEY, se paga con el plan Max.
    assert t.modelo == "claude-opus-5-5" and t.esfuerzo == "high" and t.facturacion == "max"
    assert t.motor_latex == "pdflatex" and t.max_intentos_compilacion == 5
    assert t.tope_correccion_s == 600 and t.timeout_s == 45 * 60 and t.duracion_ms == 120_000
    # Sistema: contrato + estilos. Mensaje: el contexto de la clase.
    assert "agregadoIA" in t.prompt_sistema and "\\nota{inaudible}" in t.prompt_sistema
    assert ("cd output && pdflatex -interaction=nonstopmode -halt-on-error -file-line-error -no-shell-escape "
            "apunte.tex") in t.prompt_sistema
    # Las líneas de huecos del audio que trae timeline.md, y qué hacer con cada una.
    assert "SILENCIO hasta hh:mm:ss (no se transcribió)" in t.prompt_sistema
    assert "AVISO: tramo con habla sin transcribir hasta hh:mm:ss" in t.prompt_sistema
    assert "sin inventar lo que falta" in t.prompt_sistema
    assert "\\openin" in t.prompt_sistema  # el apunte no lee archivos de afuera
    assert "Gráficos siempre" in t.prompt_sistema and "Vectores en negrita" in t.prompt_sistema
    assert "{estilo_global}" not in t.prompt_sistema and "<!--" not in t.prompt_sistema
    assert "\\materia{Materia de Prueba}" in t.prompt and "\\numeroclase{1}" in t.prompt
    assert "\\tipoclase{Teórica}" in t.prompt and "\\fechaclase{15 de octubre de 2026}" in t.prompt
    assert "[00:01:05] MARCA importante" in t.prompt and "Notación: vectores en negrita" in t.prompt
    assert "1 en input/fotos/ (pizarra_01.jpg)" in t.prompt
    assert "- Material adjunto: no hay material adjunto." in t.prompt
    assert "# Material adjunto" in t.prompt_sistema and "{adjuntos}" not in t.prompt
    assert "no existe output/apunte.tex" in agente.errores_antes
    assert "meta.json: no existe meta.json" in agente.errores_antes
    assert t.validar_meta() == []  # después de escribir, la salida está bien

    prompts = filas(ctx, "SELECT * FROM prompts")
    assert len(prompts) == 1 and prompts[0]["tarea"] == "apunte" and prompts[0]["alcance"] == "global"
    assert prompts[0]["version"].startswith("apunte@") and len(prompts[0]["version"]) == len("apunte@") + 8
    assert "Vectores en negrita" in prompts[0]["contenido"]
    assert filas(ctx, "SELECT prompt_version FROM apuntes")[0]["prompt_version"] == prompts[0]["version"]
    assert filas(ctx, "SELECT adjuntos FROM apuntes")[0]["adjuntos"] == "[]"
    assert filas(ctx, "SELECT numero_clase FROM sesiones WHERE id = ?", SID)[0]["numero_clase"] == 1
    assert compilar_falso == [workdir / "output"]


def test_registra_apunte_secciones_items(ctx, compilar_falso):
    carpeta = armar(ctx)
    usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())

    v1 = carpeta / "apunte" / "v1"
    assert sorted(p.name for p in v1.iterdir()) == [
        "agente.log", "apunte.pdf", "apunte.tex", "classmer.cls", "compilacion.log", "meta.json"]
    assert json.loads((v1 / "meta.json").read_text(encoding="utf-8")) == META
    assert "Output written on" in (v1 / "compilacion.log").read_text(encoding="utf-8")
    assert "claude-falso" in (v1 / "agente.log").read_text(encoding="utf-8")

    (a,) = filas(ctx, "SELECT * FROM apuntes")
    assert (a["sesion_id"], a["version"], a["actual"], a["compilo"]) == (SID, 1, 1, 1)
    assert a["motor"] == "claude-agent" and a["modelo"] == "claude-falso" and a["costo"] == 0.12
    assert a["ruta_tex"] == "apunte/v1/apunte.tex" and a["ruta_pdf"] == "apunte/v1/apunte.pdf"
    assert a["titulo"] == META["titulo"] and a["resumen"] == META["resumen"]
    secs = filas(ctx, "SELECT orden, titulo, inicio_ms, fin_ms, texto_plano FROM secciones WHERE apunte_id = ? ORDER BY orden", a["id"])
    assert secs == [
        {"orden": 1, "titulo": "Repaso", "inicio_ms": 5000, "fin_ms": 60000, "texto_plano": None},
        {"orden": 2, "titulo": "Gradiente", "inicio_ms": 60000, "fin_ms": 120000,
         "texto_plano": "El gradiente apunta hacia donde f crece más rápido."},
    ]
    items = filas(ctx, "SELECT materia_id, sesion_id, apunte_id, tipo, texto, fecha_limite, offset_ms, estado FROM items ORDER BY id")
    assert items == [
        {"materia_id": "prueba", "sesion_id": SID, "apunte_id": a["id"], "tipo": "parcial", "texto": "Entra hasta gradiente",
         "fecha_limite": None, "offset_ms": 90000, "estado": "abierto"},
        {"materia_id": "prueba", "sesion_id": SID, "apunte_id": a["id"], "tipo": "tarea", "texto": "Ejercicios 1 a 3",
         "fecha_limite": "2026-10-22", "offset_ms": 100000, "estado": "abierto"},
    ]
    (s,) = filas(ctx, "SELECT titulo, numero_clase FROM sesiones")
    assert s == {"titulo": META["titulo"], "numero_clase": 1}
    log = (carpeta / "pipeline.log").read_text(encoding="utf-8")
    assert "apunte: guardado v1 (compiló)" in log and "prompt apunte@" in log


def test_reejecutar_crea_v2_y_conserva_items_marcados(ctx, compilar_falso):
    armar(ctx)
    agente = usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())
    with ctx.base.conexion() as conn:  # el usuario marcó la tarea como hecha
        conn.execute("UPDATE items SET estado = 'hecho' WHERE tipo = 'tarea'")
    agente.meta = {**META, "titulo": "Gradiente (v2)",
                   "items": META["items"] + [{"tipo": "aviso", "texto": "No hay clase el lunes", "offset_ms": None,
                                              "fecha_limite": None}]}
    etapa.ejecutar(ctx, job())

    aps = filas(ctx, "SELECT id, version, actual, titulo FROM apuntes ORDER BY version")
    assert [(x["version"], x["actual"], x["titulo"]) for x in aps] == [(1, 0, META["titulo"]), (2, 1, "Gradiente (v2)")]
    v2 = aps[1]["id"]
    assert (ctx.sesion_dir(SID) / "apunte" / "v1" / "apunte.tex").is_file()
    assert (ctx.sesion_dir(SID) / "apunte" / "v2" / "apunte.tex").is_file()
    items = filas(ctx, "SELECT tipo, texto, estado, apunte_id FROM items ORDER BY tipo")
    assert [(i["tipo"], i["estado"], i["apunte_id"]) for i in items] == [
        ("aviso", "abierto", v2), ("parcial", "abierto", v2), ("tarea", "hecho", v2)]
    assert len(filas(ctx, "SELECT id FROM secciones WHERE apunte_id = ?", v2)) == 2
    assert filas(ctx, "SELECT titulo FROM sesiones")[0]["titulo"] == "Gradiente (v2)"
    assert len(filas(ctx, "SELECT id FROM prompts")) == 1  # misma versión de prompt


# --- meta.json inválido --------------------------------------------------------------------------

def test_meta_roto_se_reemplaza_por_uno_minimo(ctx, compilar_falso):
    carpeta = armar(ctx)
    usar(ctx, AgenteFalso(ctx.cfg, meta="{ esto no es json"))
    etapa.ejecutar(ctx, job())
    meta = json.loads((carpeta / "apunte" / "v1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["titulo"] == "Derivada direccional y gradiente"
    assert [(s["titulo"], s["inicio_ms"], s["fin_ms"]) for s in meta["secciones"]] == [
        ("Repaso", 5000, 60000), ("Gradiente", 60000, 120000)]
    assert meta["items"] == [{"tipo": "parcial", "texto": "Primer parcial: Entra hasta gradiente.",
                              "offset_ms": 90000, "fecha_limite": None}]
    assert meta["resumen"]
    assert not (carpeta / "apunte" / "v1" / "meta.agente.json").exists()
    (a,) = filas(ctx, "SELECT titulo, compilo FROM apuntes")
    assert a == {"titulo": "Derivada direccional y gradiente", "compilo": 1}
    assert len(filas(ctx, "SELECT id FROM secciones")) == 2
    log = (carpeta / "pipeline.log").read_text(encoding="utf-8")
    assert "aviso: meta.json inválido" in log and "se armó uno mínimo" in log


def test_meta_falta_se_arma_desde_el_tex(ctx, compilar_falso):
    carpeta = armar(ctx)
    usar(ctx, AgenteFalso(ctx.cfg, meta=None))
    etapa.ejecutar(ctx, job())
    meta = json.loads((carpeta / "apunte" / "v1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["titulo"] == "Derivada direccional y gradiente" and len(meta["secciones"]) == 2


def test_meta_parcialmente_invalido_se_salva_lo_bueno(ctx, compilar_falso):
    carpeta = armar(ctx)
    malo = {
        "titulo": "Gradiente",
        "resumen": "",                                              # vacío → del .tex
        "secciones": [
            {"titulo": "Repaso", "inicio_ms": 5000, "fin_ms": 60000, "temas": ["repaso"], "nivel": 1},
            {"titulo": "Gradiente", "inicio_ms": "00:01:00", "fin_ms": 999_999_999, "temas": []},
        ],
        "items": [
            {"tipo": "parcial", "texto": "Entra gradiente", "offset_ms": 90000.0},   # sin fecha_limite
            {"tipo": "examen", "texto": "tipo inválido", "offset_ms": 1, "fecha_limite": None},
            {"tipo": "fecha", "texto": "Entrega", "offset_ms": None, "fecha_limite": "2026-02-30"},
        ],
        "extra": {"lo": "conserva"},
    }
    usar(ctx, AgenteFalso(ctx.cfg, meta=malo))
    etapa.ejecutar(ctx, job())
    v1 = carpeta / "apunte" / "v1"
    meta = json.loads((v1 / "meta.json").read_text(encoding="utf-8"))
    assert meta["titulo"] == "Gradiente" and meta["resumen"] and meta["extra"] == {"lo": "conserva"}
    assert meta["secciones"] == [
        {"titulo": "Repaso", "inicio_ms": 5000, "fin_ms": 60000, "temas": ["repaso"]},
        {"titulo": "Gradiente", "inicio_ms": 60000, "fin_ms": 180_000, "temas": []},   # recortado a la duración
    ]
    assert meta["items"] == [
        {"tipo": "parcial", "texto": "Entra gradiente", "offset_ms": 90000, "fecha_limite": None},
        {"tipo": "fecha", "texto": "Entrega", "offset_ms": None, "fecha_limite": None},
    ]
    assert json.loads((v1 / "meta.agente.json").read_text(encoding="utf-8")) == malo
    assert len(filas(ctx, "SELECT id FROM items")) == 2


# --- número de clase y clase anterior ------------------------------------------------------------

def test_numero_de_clase_y_clase_anterior(ctx, compilar_falso):
    ctx.cfg.materias[0].numero_clase_inicial = 3
    armar(ctx, "previa-1", inicio="2026-10-01T14:00:00-03:00")
    armar(ctx, "previa-2", inicio="2026-10-08T14:00:00-03:00")
    armar(ctx, "grabando", inicio="2026-10-10T14:00:00-03:00", estado="grabando")   # no cuenta
    armar(ctx, "otra-mat", inicio="2026-10-09T14:00:00-03:00", materia_id="otra")   # otra materia
    armar(ctx, "posterior", inicio="2026-10-20T14:00:00-03:00")                     # después: no cuenta
    armar(ctx)
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE sesiones SET numero_clase = 4 WHERE id = 'previa-2'")
        aid = conn.execute(
            "INSERT INTO apuntes (sesion_id, version, actual, titulo, resumen, compilo, creado) "
            "VALUES ('previa-2', 1, 1, 'Derivadas parciales', 'Se vieron las parciales.', 1, ?)", (ahora_iso(),),
        ).lastrowid
        conn.execute("INSERT INTO secciones (apunte_id, orden, titulo, inicio_ms, fin_ms) VALUES (?, 1, 'Parciales', 0, 600000)", (aid,))
        conn.execute("INSERT INTO items (materia_id, sesion_id, apunte_id, tipo, texto, fecha_limite, estado, creado) "
                     "VALUES ('prueba', 'previa-2', ?, 'tarea', 'Práctica 3', '2026-10-15', 'abierto', ?)", (aid, ahora_iso()))
        # El apunte de la clase posterior no es "la anterior".
        conn.execute("INSERT INTO apuntes (sesion_id, version, actual, titulo, compilo, creado) "
                     "VALUES ('posterior', 1, 1, 'Del futuro', 1, ?)", (ahora_iso(),))
    agente = usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())

    assert filas(ctx, "SELECT numero_clase FROM sesiones WHERE id = ?", SID)[0]["numero_clase"] == 5  # 3 − 1 + 3
    anterior = (ctx.job_dir(SID, "apunte") / "input" / "clase_anterior.md").read_text(encoding="utf-8")
    assert anterior.startswith("# Clase 4 — 2026-10-08 — Derivadas parciales")
    assert "Se vieron las parciales." in anterior and "[00:00:00–00:10:00] Parciales" in anterior
    assert "- tarea: Práctica 3 (para el 2026-10-15)" in anterior and "Del futuro" not in anterior
    assert "Derivadas parciales" in agente.trabajos[0].prompt and "\\numeroclase{5}" in agente.trabajos[0].prompt


def test_numero_de_clase_sin_sesiones_en_error(ctx, compilar_falso):
    # Una prueba que falló (sin apunte) o una clase cuyo apunte nunca compiló no son clases.
    armar(ctx, "prueba-fallida", inicio="2026-10-14T10:00:00-03:00", estado="error")
    armar(ctx, "no-compilo", inicio="2026-10-14T12:00:00-03:00", estado="error")
    armar(ctx, "compilo-antes", inicio="2026-10-13T12:00:00-03:00", estado="error")   # v1 compiló: cuenta
    armar(ctx)
    with ctx.base.conexion() as conn:
        for sid, version, actual, titulo, compilo in (("no-compilo", 1, 1, "A medias", 0),
                                                      ("compilo-antes", 1, 0, "Clase vieja", 1),
                                                      ("compilo-antes", 2, 1, "Clase vieja v2", 0)):
            conn.execute("INSERT INTO apuntes (sesion_id, version, actual, titulo, compilo, creado) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (sid, version, actual, titulo, compilo, ahora_iso()))
    agente = usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())

    assert filas(ctx, "SELECT numero_clase FROM sesiones WHERE id = ?", SID)[0]["numero_clase"] == 2
    anterior = (ctx.job_dir(SID, "apunte") / "input" / "clase_anterior.md").read_text(encoding="utf-8")
    assert "Clase vieja v2" in anterior and "A medias" not in anterior
    assert "\\numeroclase{2}" in agente.trabajos[0].prompt


def test_numero_de_clase_publicado_se_conserva(ctx, compilar_falso):
    armar(ctx)
    with ctx.base.conexion() as conn:
        actualizar_sesion(conn, SID, ctx.cfg.zona_horaria, numero_clase=7,
                          ruta_publicada="C:/Materia/Clases-Classmer/2026-10-15 Clase 07 - Gradiente")
    armar(ctx, "importada-despues", inicio="2026-10-01T14:00:00-03:00")  # anterior, pero llegó después
    agente = usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())
    assert filas(ctx, "SELECT numero_clase FROM sesiones WHERE id = ?", SID)[0]["numero_clase"] == 7
    assert "\\numeroclase{7}" in agente.trabajos[0].prompt


def test_duracion_medida_manda_sobre_la_declarada(ctx, compilar_falso):
    carpeta = armar(ctx)  # el celu declaró 2 min; el audio mide 5
    (carpeta / "verificacion.json").write_text(
        json.dumps({"duracion_ms": 300_000, "duracion_declarada_ms": 120_000}), encoding="utf-8")
    meta = {**META, "secciones": [META["secciones"][0], {**META["secciones"][1], "fin_ms": 290_000}]}
    agente = usar(ctx, AgenteFalso(ctx.cfg, meta=meta))
    etapa.ejecutar(ctx, job())

    t = agente.trabajos[0]
    assert t.duracion_ms == 300_000 and "Duración: 00:05:00" in t.prompt
    contexto = json.loads((ctx.job_dir(SID, "apunte") / "input" / "contexto.json").read_text(encoding="utf-8"))
    assert contexto["duracion_ms"] == 300_000
    assert t.validar_meta() == []  # 4:50 no se pasa de la clase
    v1 = carpeta / "apunte" / "v1"
    assert json.loads((v1 / "meta.json").read_text(encoding="utf-8"))["secciones"][1]["fin_ms"] == 290_000
    assert not (v1 / "meta.agente.json").exists()  # no hubo que repararlo


def test_sin_perfil_usa_el_nombre_de_la_materia(ctx, materia_dir, compilar_falso):
    (materia_dir / "PROMPT-AGENTE.md").unlink()
    armar(ctx)
    agente = usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())
    perfil = (ctx.job_dir(SID, "apunte") / "input" / "perfil.md").read_text(encoding="utf-8")
    assert perfil.startswith("# Materia de Prueba") and str(materia_dir) in perfil
    assert "No hay un perfil" in agente.trabajos[0].prompt


# --- errores -------------------------------------------------------------------------------------

def test_sin_materia_no_reintenta(ctx):
    armar(ctx, materia_id=None)
    usar(ctx, AgenteFalso(ctx.cfg))
    with pytest.raises(ErrorEtapa) as e:
        etapa.ejecutar(ctx, job())
    assert not e.value.reintentable and "falta la materia" in str(e.value)


def test_sin_timeline_no_reintenta(ctx):
    carpeta = armar(ctx)
    (carpeta / "timeline.md").unlink()
    usar(ctx, AgenteFalso(ctx.cfg))
    with pytest.raises(ErrorEtapa) as e:
        etapa.ejecutar(ctx, job())
    assert not e.value.reintentable and "timeline.md" in str(e.value)


def test_sin_apunte_tex_es_reintentable(ctx, compilar_falso):
    armar(ctx)
    usar(ctx, AgenteFalso(ctx.cfg, tex=None))
    with pytest.raises(ErrorEtapa) as e:
        etapa.ejecutar(ctx, job())
    assert e.value.reintentable and "apunte.tex" in str(e.value)
    assert filas(ctx, "SELECT id FROM apuntes") == [] and compilar_falso == []


def test_cuota_del_agente_se_propaga(ctx):
    carpeta = armar(ctx)
    usar(ctx, AgenteFalso(ctx.cfg, error=CuotaAgotada("Claude: se agotó la cuota", espera_s=1800)))
    with pytest.raises(CuotaAgotada) as e:
        etapa.ejecutar(ctx, job())
    assert e.value.espera_s == 1800
    assert "se agotó la cuota" in (carpeta / "pipeline.log").read_text(encoding="utf-8")


class ClienteCaido:
    """Imita ClaudeSDKClient: el agente escribe medio apunte y la API de Claude se cae (500)."""

    def __init__(self, opciones: Any):
        self.salida = Path(opciones.cwd) / "output"

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def interrupt(self) -> None: ...
    async def query(self, prompt: str) -> None: ...

    async def receive_response(self):
        (self.salida / "apunte.tex").write_text(TEX.split("\\section{Gradiente")[0], encoding="utf-8")
        (self.salida / "meta.json").write_text(json.dumps(META), encoding="utf-8")
        texto = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
        yield AssistantMessage(content=[TextBlock(texto)], model="claude-x", error="server_error")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=4,
                            session_id="s", total_cost_usd=0.5, result=texto, api_error_status=500)


def test_api_caida_a_mitad_no_compila_ni_guarda(ctx, compilar_falso):
    carpeta = armar(ctx)
    ctx.servicios = Servicios(agente_apunte=lambda cfg: AgenteClaude(cfg, cliente=ClienteCaido))
    with pytest.raises(CuotaAgotada) as e:  # se reintenta más tarde sin gastar intento
        etapa.ejecutar(ctx, job())
    assert e.value.espera_s == ESPERA_SOBRECARGA_S
    assert compilar_falso == [] and filas(ctx, "SELECT id FROM apuntes") == []
    assert not (carpeta / "apunte").exists()
    assert "sobrecargada o no responde" in (carpeta / "pipeline.log").read_text(encoding="utf-8")


def test_tex_que_lee_archivos_de_afuera_no_se_compila(ctx, compilar_falso, tmp_path):
    carpeta = armar(ctx)
    secreto = (tmp_path / "Classmer-data" / ".env").as_posix()
    malo = TEX.replace("\\begin{document}", "\\begin{document}\n\\input{" + secreto + "}")
    usar(ctx, AgenteFalso(ctx.cfg, tex=malo))
    r = etapa.ejecutar(ctx, job())

    assert compilar_falso == []  # el server no lo compila
    assert r.siguientes == [("publicar", {})] and "no compiló" in r.log
    v1 = carpeta / "apunte" / "v1"
    assert (v1 / "apunte.tex").read_text(encoding="utf-8") == malo and not (v1 / "apunte.pdf").exists()
    log_comp = (v1 / "compilacion.log").read_text(encoding="utf-8")
    assert "lee archivos de fuera" in log_comp and f"\\input{{{secreto}}}" in log_comp
    (a,) = filas(ctx, "SELECT compilo, ruta_pdf FROM apuntes")
    assert a == {"compilo": 0, "ruta_pdf": None}
    log = (carpeta / "pipeline.log").read_text(encoding="utf-8")
    assert "apunte: no lo compilo" in log and "apunte: guardado v1 (no compiló)" in log


def test_carpeta_de_trabajo_con_tildes(ctx, tmp_path, monkeypatch):
    armar(ctx)
    usar(ctx, AgenteFalso(ctx.cfg))
    monkeypatch.setattr(ctx, "job_dir", lambda sid, nombre: tmp_path / "Clásicos" / sid / nombre)
    with pytest.raises(ErrorEtapa) as e:
        etapa.ejecutar(ctx, job())
    assert not e.value.reintentable and "ASCII" in str(e.value)


def test_helpers():
    assert etapa.fecha_larga("2026-10-05") == "5 de octubre de 2026"
    assert etapa.escapar_latex("Riesgo & Finanzas 100% #1 a_b") == r"Riesgo \& Finanzas 100\% \#1 a\_b"
    pre = etapa.preambulo({"materia": "Álgebra & Co", "fecha_larga": "1 de marzo de 2026", "numero_clase": 2,
                           "tipo_clase": None})
    assert "\\materia{Álgebra \\& Co}" in pre and "tipoclase" not in pre


# --- título en texto plano -----------------------------------------------------------------------

def test_titulo_con_latex_queda_en_texto_plano(ctx, compilar_falso):
    carpeta = armar(ctx)
    usar(ctx, AgenteFalso(ctx.cfg, meta={**META, "titulo": r"Del CAPM a  Black--Scholes: $\sigma$ y \textbf{volatilidad}"}))
    etapa.ejecutar(ctx, job())
    limpio = "Del CAPM a Black-Scholes: σ y volatilidad"
    v1 = carpeta / "apunte" / "v1"
    assert json.loads((v1 / "meta.json").read_text(encoding="utf-8"))["titulo"] == limpio
    assert filas(ctx, "SELECT titulo FROM apuntes")[0]["titulo"] == limpio
    assert filas(ctx, "SELECT titulo FROM sesiones")[0]["titulo"] == limpio
    assert (v1 / "apunte.tex").read_text(encoding="utf-8") == TEX  # el .tex no se toca
    assert f"apunte: guardado v1 (compiló): {limpio} ·" in (carpeta / "pipeline.log").read_text(encoding="utf-8")


def test_normalizar_titulo():
    assert etapa._normalizar({**META, "titulo": "Martingalas --- parte\n 2"})["titulo"] == "Martingalas — parte 2"
    # Si limpiar no deja nada, queda el original (sin espacios de más): nunca un título vacío.
    assert etapa._normalizar({**META, "titulo": "  \\textbf{}  "})["titulo"] == "\\textbf{}"


# --- modelo, esfuerzo y facturación --------------------------------------------------------------

CLAVE = "sk-ant-falsa-123"


@pytest.fixture
def con_clave(ctx, home):
    """El ctx con ANTHROPIC_API_KEY (de prueba) en el .env de la carpeta de datos."""
    with open(home / ".env", "a", encoding="utf-8") as f:
        f.write(f"ANTHROPIC_API_KEY={CLAVE}\n")
    ctx.cfg = cargar_config(home)
    assert ctx.cfg.anthropic_api_key == CLAVE and ctx.cfg.claude.facturacion == "api_luego_max"
    return ctx


def pipeline_log(ctx, sid: str = SID) -> str:
    return (ctx.sesion_dir(sid) / "pipeline.log").read_text(encoding="utf-8")


def test_modelo_esfuerzo_y_facturacion_globales(ctx, compilar_falso):
    armar(ctx)
    agente = usar(ctx, AgenteFalso(ctx.cfg))
    r = etapa.ejecutar(ctx, job())

    t = agente.trabajos[0]
    assert (t.modelo, t.esfuerzo, t.facturacion) == ("claude-opus-5-5", "high", "max")
    (a,) = filas(ctx, "SELECT modelo, esfuerzo, facturacion FROM apuntes")
    assert a == {"modelo": "claude-falso", "esfuerzo": "high", "facturacion": "max"}
    assert r.facturacion == "max"
    assert "agente claude-falso, esfuerzo high, facturación plan Max, costo US$ 0.12 equivalente API" in r.log
    log = pipeline_log(ctx)
    assert "no hay ANTHROPIC_API_KEY en Classmer-data\\.env: uso el plan Max" in log  # modo con API sin clave
    assert "apunte: intento con claude-opus-5-5, esfuerzo high (global), facturación plan Max" in log
    assert ("apunte: guardado v1 (compiló): Derivada direccional y gradiente · claude-falso, esfuerzo high, "
            "facturación plan Max, costo US$ 0.12 equivalente API (plan Max") in log


@pytest.mark.parametrize(("propio", "modelo", "esfuerzo"), [
    ({"modelo": "claude-sonnet-5-5", "esfuerzo": "max"}, "claude-sonnet-5-5", "max"),
    ({"modelo": "claude-haiku-5-5"}, "claude-haiku-5-5", "high"),   # sin esfuerzo propio: hereda el global
])
def test_modelo_propio_de_la_materia(ctx, compilar_falso, propio, modelo, esfuerzo):
    ctx.cfg.materias[0].apunte = ModeloApunte(**propio)
    armar(ctx)
    agente = usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())
    t = agente.trabajos[0]
    assert (t.modelo, t.esfuerzo) == (modelo, esfuerzo)
    assert filas(ctx, "SELECT esfuerzo FROM apuntes")[0]["esfuerzo"] == esfuerzo
    assert f"apunte: intento con {modelo}, esfuerzo {esfuerzo} (de la materia), facturación plan Max" in pipeline_log(ctx)


def test_modelo_global_cambiado_y_sin_esfuerzo(ctx, compilar_falso):
    # La otra materia tiene modelo propio: no afecta a ésta.
    ctx.cfg.materias[1].apunte = ModeloApunte(modelo="claude-haiku-5-5", esfuerzo="low")
    ctx.cfg.apunte.modelo, ctx.cfg.apunte.esfuerzo = "claude-fable-5-1", None
    armar(ctx)
    agente = usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())
    assert (agente.trabajos[0].modelo, agente.trabajos[0].esfuerzo) == ("claude-fable-5-1", None)
    assert filas(ctx, "SELECT esfuerzo FROM apuntes")[0]["esfuerzo"] is None
    assert "intento con claude-fable-5-1, esfuerzo por defecto (global)" in pipeline_log(ctx)


def test_api_sin_creditos_rehace_todo_con_max_y_lo_recuerda(con_clave, compilar_falso):
    ctx = con_clave
    armar(ctx)
    agente = usar(ctx, AgenteFalso(ctx.cfg, fallas=[SinCreditos("api", "sin_creditos", "Credit balance is too low")]))
    r = etapa.ejecutar(ctx, job())

    assert agente.medios == ["api", "max"]
    assert "output/a_medias.tex" not in agente.archivos[1]       # el segundo intento arranca con output/ limpio
    assert agente.archivos[1] == agente.archivos[0]
    assert not (ctx.job_dir(SID, "apunte") / "output" / "a_medias.tex").exists()
    assert r.facturacion == "max" and "facturación plan Max" in r.log
    assert filas(ctx, "SELECT facturacion FROM apuntes") == [{"facturacion": "max"}]
    assert len(filas(ctx, "SELECT id FROM prompts")) == 1           # el prompt se registra una sola vez
    assert filas(ctx, "SELECT medio, estado FROM facturacion") == [{"medio": "api", "estado": "sin_creditos"}]
    log = pipeline_log(ctx)
    assert "apunte: intento con claude-opus-5-5, esfuerzo high (global), facturación API" in log
    assert "apunte: API sin créditos (Credit balance is too low): sigo con el plan Max" in log
    assert log.index("sigo con el plan Max") < log.index("facturación plan Max")
    assert CLAVE not in log

    # La próxima tarea va directo al plan Max, sin volver a probar la API.
    otra = "apu-0002"
    armar(ctx, otra, inicio="2026-10-16T14:00:05-03:00")
    r2 = etapa.ejecutar(ctx, job(otra))
    assert agente.medios == ["api", "max", "max"] and r2.facturacion == "max"
    log2 = pipeline_log(ctx, otra)
    assert "API sin créditos desde" in log2 and "voy directo" in log2 and "facturación API" not in log2


@pytest.mark.parametrize(("modo", "falla", "medios"), [
    ("api_luego_max", SinCreditos("api", "clave_invalida", "invalid x-api-key"), ["api", "max"]),
    ("max_luego_api", SinCreditos("max", "sin_cupo", "usage limit reached", espera_s=3600), ["max", "api"]),
    ("max_luego_api", SinCreditos("max", "sin_login", "Please run /login"), ["max", "api"]),
], ids=["clave_invalida", "max_sin_cupo", "max_sin_login"])
def test_cambio_de_medio_segun_el_modo(con_clave, compilar_falso, modo, falla, medios):
    ctx = con_clave
    ctx.cfg.claude.facturacion = modo
    armar(ctx)
    agente = usar(ctx, AgenteFalso(ctx.cfg, fallas=[falla]))
    r = etapa.ejecutar(ctx, job())
    assert agente.medios == medios and r.facturacion == medios[-1]
    assert filas(ctx, "SELECT facturacion FROM apuntes") == [{"facturacion": medios[-1]}]
    assert filas(ctx, "SELECT medio, estado FROM facturacion") == [{"medio": falla.medio, "estado": falla.estado}]


def test_429_con_la_api_no_cambia_a_max(con_clave, compilar_falso):
    ctx = con_clave
    armar(ctx)
    pasajero = CuotaAgotada("Claude: hay rate limit (demasiados pedidos seguidos): API Error: 429", espera_s=1800)
    agente = usar(ctx, AgenteFalso(ctx.cfg, fallas=[pasajero]))
    with pytest.raises(CuotaAgotada) as e:
        etapa.ejecutar(ctx, job())
    assert e.value is pasajero and not isinstance(e.value, SinCreditos)
    assert agente.medios == ["api"]
    assert filas(ctx, "SELECT * FROM facturacion") == [] and filas(ctx, "SELECT id FROM apuntes") == []
    assert compilar_falso == [] and "hay rate limit" in pipeline_log(ctx)


def test_modo_api_sin_otro_medio_se_pospone(con_clave, compilar_falso):
    ctx = con_clave
    ctx.cfg.claude.facturacion = "api"
    armar(ctx)
    agente = usar(ctx, AgenteFalso(ctx.cfg, fallas=[SinCreditos("api", "sin_creditos", "Credit balance is too low")]))
    with pytest.raises(CuotaAgotada) as e:
        etapa.ejecutar(ctx, job())
    assert agente.medios == ["api"] and e.value.espera_s >= 3600   # hasta que se vuelva a probar la API
    assert "API sin créditos" in pipeline_log(ctx)
    # Mientras tanto, ni se lanza el agente.
    with pytest.raises(CuotaAgotada):
        etapa.ejecutar(ctx, job())
    assert agente.medios == ["api"] and filas(ctx, "SELECT id FROM apuntes") == []


def test_modo_max_no_usa_la_api_aunque_haya_clave(con_clave, compilar_falso):
    ctx = con_clave
    ctx.cfg.claude.facturacion = "max"
    armar(ctx)
    agente = usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())
    assert agente.medios == ["max"]


def test_el_job_guarda_con_que_se_pago(ctx, compilar_falso):
    from classmer.worker import Worker

    armar(ctx)
    usar(ctx, AgenteFalso(ctx.cfg))
    w = Worker(ctx)
    try:
        with ctx.base.conexion() as conn:
            cola.encolar(conn, "apunte", SID)
            j = cola.tomar(conn, ["apunte"])
        w.ejecutar_job(j)
    finally:
        w.detener()
    (fila,) = filas(ctx, "SELECT estado, facturacion, costo, motor FROM jobs WHERE tipo = 'apunte'")
    assert fila == {"estado": "hecho", "facturacion": "max", "costo": 0.12, "motor": "claude-agent/claude-falso"}


class ClienteFacturacion:
    """Imita ClaudeSDKClient: si la corrida trae la API key, la API responde "sin créditos" (después
    de dejar algo a medias en output/); con el plan Max, el agente escribe el apunte entero."""

    def __init__(self, opciones: Any):
        self.opciones = opciones
        self.salida = Path(opciones.cwd) / "output"
        self.entorno: dict[str, str] = {}

    async def connect(self) -> None:
        self.entorno = dict(os.environ)

    async def disconnect(self) -> None: ...
    async def interrupt(self) -> None: ...
    async def query(self, prompt: str) -> None: ...

    async def receive_response(self):
        api = "ANTHROPIC_API_KEY" in self.opciones.env
        yield SystemMessage(subtype="init", data={"model": "claude-opus-5-5",
                                                  "apiKeySource": "ANTHROPIC_API_KEY" if api else "none"})
        if api:
            (self.salida / "a_medias.tex").write_text("\\documentclass{classmer}\n", encoding="utf-8")
            texto = "Credit balance is too low"
            yield AssistantMessage(content=[TextBlock(texto)], model="<synthetic>", error="billing_error")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1,
                                session_id="s", total_cost_usd=0.0, result=texto, api_error_status=400)
            return
        (self.salida / "apunte.tex").write_text(TEX, encoding="utf-8")
        (self.salida / "meta.json").write_text(json.dumps(META), encoding="utf-8")
        (self.salida / "apunte.pdf").write_bytes(b"%PDF")       # compiló: no hace falta seguimiento
        t = (self.salida / "apunte.tex").stat().st_mtime
        os.utime(self.salida / "apunte.pdf", (t + 1, t + 1))
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=9,
                            session_id="s", total_cost_usd=0.9, result="Listo.")


def test_etapa_con_el_agente_real_pasa_de_api_a_max(con_clave, compilar_falso, monkeypatch):
    """De punta a punta con AgenteClaude y un SDK falso: la API sin créditos → se rehace con el plan
    Max, y en esa corrida la clave no llega al CLI (ni la del .env ni la del entorno del proceso)."""
    ctx = con_clave
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-del-entorno-456")
    carpeta = armar(ctx)
    clientes: list[ClienteFacturacion] = []

    def fabrica(opciones: Any) -> ClienteFacturacion:
        clientes.append(ClienteFacturacion(opciones))
        return clientes[-1]

    ctx.servicios = Servicios(agente_apunte=lambda cfg: AgenteClaude(cfg, cliente=fabrica))
    r = etapa.ejecutar(ctx, job())

    con_api, con_max = clientes
    assert con_api.opciones.env["ANTHROPIC_API_KEY"] == CLAVE and con_api.opciones.effort == "high"
    assert "ANTHROPIC_API_KEY" not in con_max.opciones.env and con_max.opciones.effort == "high"
    assert all("ANTHROPIC_API_KEY" not in c.entorno for c in clientes)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-del-entorno-456"
    assert not (ctx.job_dir(SID, "apunte") / "output" / "a_medias.tex").exists()
    assert r.facturacion == "max" and "costo US$ 0.90 equivalente API" in r.log
    assert filas(ctx, "SELECT facturacion, esfuerzo FROM apuntes") == [{"facturacion": "max", "esfuerzo": "high"}]
    log = pipeline_log(ctx)
    assert "API sin créditos" in log and "sigo con el plan Max" in log
    agente_log = (carpeta / "apunte" / "v1" / "agente.log").read_text(encoding="utf-8")
    assert "esfuerzo high, facturación max" in agente_log and "facturación plan Max" in agente_log
    for texto in (log, agente_log, r.log):
        assert CLAVE not in texto and "sk-ant-del-entorno-456" not in texto


def test_intento_descartado_deja_lo_gastado_en_pipeline_log(con_clave, compilar_falso):
    ctx = con_clave
    armar(ctx)
    falla = SinCreditos("api", "sin_creditos", "Credit balance is too low")
    falla.costo_usd = 1.25  # lo que AgenteClaude anota de un intento que ya había gastado
    usar(ctx, AgenteFalso(ctx.cfg, fallas=[falla]))
    r = etapa.ejecutar(ctx, job())
    assert r.facturacion == "max"
    log = pipeline_log(ctx)
    assert "apunte: el intento con API se descarta; ya había gastado US$ 1.25 (API)" in log
    assert log.index("ya había gastado") < log.index("sigo con el plan Max")
    # El job lleva el costo del intento que quedó y, en su resumen, lo que se pagó de verdad con la API.
    assert r.costo == 0.12 and "además gastó un intento descartado: US$ 1.25 (API)" in r.log.split("\n")[0]


def test_carpeta_tomada_un_momento_se_reintenta(ctx, compilar_falso, monkeypatch):
    """Al cambiar de medio la carpeta se borra apenas cerró el CLI: si Windows la tiene tomada un
    momento, se reintenta; si sigue tomada, error reintentable (no se pierde nada)."""
    armar(ctx)
    workdir = ctx.job_dir(SID, "apunte")
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "viejo.txt").write_text("x", encoding="utf-8")
    borrar_real, intentos, esperas = etapa._borrar, [], []

    def borrar(carpeta):
        intentos.append(carpeta)
        if len(intentos) <= 2:
            raise PermissionError(13, "El proceso no tiene acceso al archivo porque está siendo utilizado")
        borrar_real(carpeta)

    monkeypatch.setattr(etapa, "_borrar", borrar)
    monkeypatch.setattr(etapa, "_dormir", esperas.append)
    usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())
    assert len(intentos) == 3 and esperas == [0.5, 1.0] and not (workdir / "viejo.txt").exists()

    def siempre_tomada(carpeta):
        raise PermissionError(13, "en uso")

    monkeypatch.setattr(etapa, "_borrar", siempre_tomada)
    esperas.clear()
    with pytest.raises(ErrorEtapa) as e:
        etapa.ejecutar(ctx, job())
    assert e.value.reintentable and "carpeta de trabajo" in str(e.value) and esperas == list(etapa.ESPERAS_BORRAR)


# --- compilación real ----------------------------------------------------------------------------

con_latex = pytest.mark.latex


@con_latex
@requiere("pdflatex")
def test_compila_de_verdad_y_publica(ctx, materia_dir):
    carpeta = armar(ctx)
    usar(ctx, AgenteFalso(ctx.cfg))
    r = etapa.ejecutar(ctx, job())
    assert r.siguientes == [("publicar", {})]
    v1 = carpeta / "apunte" / "v1"
    assert (v1 / "apunte.pdf").read_bytes().startswith(b"%PDF")
    log = (v1 / "compilacion.log").read_text(encoding="utf-8", errors="replace")
    assert "Output written on apunte.pdf" in log and "Foto no encontrada" not in log  # la foto sale de ../input/fotos
    (a,) = filas(ctx, "SELECT compilo, ruta_pdf FROM apuntes")
    assert a == {"compilo": 1, "ruta_pdf": "apunte/v1/apunte.pdf"}

    # publicar toma el apunte actual tal cual.
    publicar.ejecutar(ctx, Job(id=2, tipo="publicar", sesion_id=SID))
    (destino,) = (materia_dir / "Clases-Classmer").glob("2026-10-15 Clase 01 - *")
    assert (destino / "apunte.pdf").read_bytes() == (v1 / "apunte.pdf").read_bytes()
    assert (destino / "fotos" / "pizarra_01.jpg").is_file()
    assert filas(ctx, "SELECT estado FROM sesiones")[0]["estado"] == "lista"


@con_latex
@requiere("pdflatex")
def test_no_compilo_sigue_a_publicar(ctx):
    carpeta = armar(ctx)
    roto = TEX.replace("Se repasó la clase anterior", "Se repasó \\comandoquenoexiste la clase anterior")
    usar(ctx, AgenteFalso(ctx.cfg, tex=roto))
    r = etapa.ejecutar(ctx, job())
    assert r.siguientes == [("publicar", {})] and "no compiló" in r.log
    v1 = carpeta / "apunte" / "v1"
    assert not (v1 / "apunte.pdf").exists()
    assert "Undefined control sequence" in (v1 / "compilacion.log").read_text(encoding="utf-8", errors="replace")
    (a,) = filas(ctx, "SELECT compilo, ruta_pdf, actual FROM apuntes")
    assert a == {"compilo": 0, "ruta_pdf": None, "actual": 1}
    assert len(filas(ctx, "SELECT id FROM items")) == 2  # el índice se arma igual
    assert "apunte: guardado v1 (no compiló)" in (carpeta / "pipeline.log").read_text(encoding="utf-8")


@con_latex
@requiere("pdflatex")
def test_foto_real_de_varios_mb_llega_intacta_al_agente_y_al_pdf(ctx, tmp_path):
    # §7, fotos nativas: una foto del S23 (4000x3000, varios MB) pasa por linea_de_tiempo sin
    # achicarse ni recomprimirse, el agente la ve tal cual en input/fotos y el PDF la embebe entera.
    from classmer.pipeline.linea_de_tiempo import procesar_foto

    carpeta = armar(ctx, fotos=False)
    original = tmp_path / "s23.jpg"
    ruido = Image.effect_noise((4000, 3000), 60).convert("RGB")
    Image.blend(Image.new("RGB", (4000, 3000), (40, 90, 60)), ruido, 0.5).save(original, "JPEG", quality=90)
    assert original.stat().st_size > 2_000_000
    (carpeta / "fotos_pdf").mkdir()
    assert procesar_foto(original, carpeta / "fotos_pdf" / "pizarra_01.jpg")[2] == "original"

    agente = usar(ctx, AgenteFalso(ctx.cfg))
    etapa.ejecutar(ctx, job())
    workdir = Path(agente.trabajos[0].workdir)
    assert (workdir / "input" / "fotos" / "pizarra_01.jpg").read_bytes() == original.read_bytes()
    pdf = carpeta / "apunte" / "v1" / "apunte.pdf"
    assert pdf.is_file() and pdf.stat().st_size >= original.stat().st_size  # JPEG embebido sin recomprimir
