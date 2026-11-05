"""Adjuntos en el pipeline (§6, §7, §9): linea_de_tiempo, la carpeta de trabajo y el prompt del apunte,
apuntes.adjuntos (también con uno que llega con el agente andando y con el cambio de facturación),
publicar y la compilación real de páginas de un PDF adjunto (marca latex). El agente es FALSO."""

from __future__ import annotations

import io
import json
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from classmer import adjuntos, cola, latex, sesiones
from classmer.adjuntos import PENDIENTE, SIN_USAR, USADO
from classmer.cola import Job
from classmer.config import cargar_config
from classmer.ia.agente_apunte import ResultadoAgente, TrabajoApunte
from classmer.ia.facturacion import SinCreditos
from classmer.pipeline import Servicios, actualizar_sesion, linea_de_tiempo, publicar
from classmer.pipeline import apunte as etapa
from classmer.util import ahora_iso
from conftest import requiere

SID = "adj-0001"
INICIO = "2026-10-15T14:00:05-03:00"
TIMELINE = (
    "# Clase: Materia de Prueba — 2026-10-15 — teórica\n"
    "[00:00:05] PROFE: Repasemos lo de la clase pasada.\n"
    "[00:00:30] FOTO fotos/pizarra_01.jpg\n"
    "[00:01:00] PROFE: Miren la slide del gradiente.\n"
)
PREAMBULO = ("\\documentclass{classmer}\n\\materia{Materia de Prueba}\n\\fechaclase{15 de octubre de 2026}\n"
             "\\numeroclase{7}\n\\titulo{Gradiente}\n")
TEX = PREAMBULO + "\\begin{document}\n\\section{Gradiente \\ts{00:01:00}}\nEl gradiente.\n\\end{document}\n"
META = {"titulo": "Gradiente", "resumen": "Se vio el gradiente.",
        "secciones": [{"titulo": "Gradiente", "inicio_ms": 60000, "fin_ms": 120000, "temas": ["gradiente"]}],
        "items": []}


# --- archivos de prueba ---------------------------------------------------------------------------

def pdf_dibujado(paginas: int = 3) -> bytes:
    """Un PDF (pypdf) con un rectángulo de otro color y otro ancho en cada página."""
    from pypdf import PdfWriter
    from pypdf.generic import ContentStream

    w = PdfWriter()
    for i in range(paginas):
        pagina = w.add_blank_page(width=400, height=300)
        contenido = ContentStream(None, w)
        contenido.set_data(f"0.{i + 2} 0.4 0.8 rg 40 40 {100 + 80 * i} 150 re f".encode())
        pagina.replace_contents(contenido)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def imagen(formato: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (48, 36), (30, 120, 200)).save(buf, formato)
    return buf.getvalue()


IMAGEN_ROTA = b"\xff\xd8\xff\xe0" + b"\x00" * 64   # dice JPEG, pero Pillow no la abre


def adjuntar(ctx, sid: str, contenido: bytes, nombre: str, offset_ms: int | None = None) -> str:
    aid = str(uuid.uuid4())
    adjuntos.guardar(ctx.cfg, ctx.base, sid, adjunto_id=aid, archivo=io.BytesIO(contenido),
                     nombre_original=nombre, offset_ms=offset_ms)
    return aid


# --- sesiones y agente falso ----------------------------------------------------------------------

def crear(ctx, sid: str = SID, *, estado: str = "procesando", etapa_: str = "apunte",
          numero: int | None = None) -> Path:
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio=INICIO, dispositivo="test")
        actualizar_sesion(conn, sid, ctx.cfg.zona_horaria, estado=estado, etapa=etapa_, duracion_ms=120_000,
                          numero_clase=numero)
    carpeta = ctx.sesion_dir(sid)
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def para_apunte(ctx, sid: str = SID) -> Path:
    """Sesión lista para la etapa apunte: timeline.md de linea_de_tiempo (sin adjuntos) y transcript."""
    carpeta = crear(ctx, sid)
    (carpeta / "timeline.md").write_text(TIMELINE, encoding="utf-8")
    (carpeta / "transcript.json").write_text("[]", encoding="utf-8")
    return carpeta


class AgenteFalso:
    """Escribe un apunte mínimo. `durante(n)` corre adentro de la corrida n (0, 1…), con el agente
    andando; `fallas`: las primeras corridas lanzan esas excepciones (una por corrida)."""

    def __init__(self, durante: Callable[[int], None] | None = None, fallas: list[Exception] | None = None,
                 tex: str = TEX):
        self.durante = durante
        self.fallas = list(fallas or [])
        self.tex = tex
        self.trabajos: list[TrabajoApunte] = []
        self.manifiestos: list[dict[str, Any]] = []

    def ejecutar(self, trabajo: TrabajoApunte) -> ResultadoAgente:
        self.trabajos.append(trabajo)
        w = Path(trabajo.workdir)
        self.manifiestos.append(json.loads((w / "input" / "adjuntos.json").read_text(encoding="utf-8")))
        if self.durante is not None:
            self.durante(len(self.trabajos) - 1)
        if self.fallas:
            raise self.fallas.pop(0)
        (w / "output" / "apunte.tex").write_text(self.tex, encoding="utf-8")
        (w / "output" / "meta.json").write_text(json.dumps(META), encoding="utf-8")
        return ResultadoAgente(ok=True, modelo="claude-falso", costo_usd=0.1, intentos_compilacion=1, turnos=3,
                               log="Agente: claude-falso · 3 turnos", error=None)


def usar(ctx, agente: AgenteFalso) -> AgenteFalso:
    ctx.servicios = Servicios(agente_apunte=lambda cfg: agente)
    return agente


@pytest.fixture
def compilar_falso(monkeypatch):
    """La compilación del server, sin MiKTeX."""
    def falso(carpeta: Path, archivo: str = "apunte.tex", **_kw: Any) -> latex.ResultadoCompilacion:
        pdf = Path(carpeta) / "apunte.pdf"
        pdf.write_bytes(b"%PDF-1.5 falso")
        return latex.ResultadoCompilacion(ok=True, pdf=pdf, log="pdflatex compiló bien (falso).")

    monkeypatch.setattr(latex, "compilar", falso)


def usados_del_actual(ctx, sid: str = SID) -> list[str]:
    with ctx.base.conexion() as conn:
        fila = conn.execute("SELECT adjuntos FROM apuntes WHERE sesion_id = ? AND actual = 1", (sid,)).fetchone()
    return json.loads(fila["adjuntos"])


def estados(ctx, sid: str = SID) -> dict[str, str]:
    with ctx.base.conexion() as conn:
        return adjuntos.estados(ctx.cfg, conn, sid, adjuntos.listar(conn, sid))


def leer_log(ctx, sid: str = SID) -> str:
    return (ctx.sesion_dir(sid) / "pipeline.log").read_text(encoding="utf-8")


# --- linea_de_tiempo ------------------------------------------------------------------------------

def test_linea_de_tiempo_con_adjuntos_con_y_sin_minuto(ctx):
    carpeta = crear(ctx, estado="procesando", etapa_="linea_de_tiempo")
    with ctx.base.conexion() as conn:
        conn.execute("INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta, estado) "
                     "VALUES (?, 1, 0, 60000, 'bloques/bloque_01.mp3', 'transcripto')", (SID,))
    (carpeta / "transcript").mkdir()
    segmentos = [{"inicio_ms": 1_000, "fin_ms": 5_000, "hablante": "PROFE", "texto": "Hoy vemos derivadas."},
                 {"inicio_ms": 31_000, "fin_ms": 35_000, "hablante": "PROFE", "texto": "Miren la slide."}]
    (carpeta / "transcript" / "bloque_01.json").write_text(json.dumps(
        {"n": 1, "inicio_ms": 0, "fin_ms": 60_000, "estado": "transcripto", "avisos": [], "segmentos": segmentos}),
        encoding="utf-8")
    sesiones.registrar_evento(ctx.cfg, ctx.base, SID, evento_id="m-1", tipo="marca", offset_ms=31_000,
                              marca="importante")
    slides = adjuntar(ctx, SID, pdf_dibujado(3), "Slides clase 5.pdf", offset_ms=30_500)
    notas = adjuntar(ctx, SID, imagen("PNG"), "notas del profe.png")

    r = linea_de_tiempo.ejecutar(ctx, Job(id=1, tipo="linea_de_tiempo", sesion_id=SID))

    assert (carpeta / "timeline.md").read_text(encoding="utf-8").splitlines() == [
        "# Clase: Materia de Prueba — 2026-10-15 — teórica",
        "[00:00:01] PROFE: Hoy vemos derivadas.",
        '[00:00:30] ADJUNTO adjuntos/Slides_clase_5.pdf (PDF, 3 páginas): "Slides clase 5.pdf"',
        "[00:00:31] PROFE: Miren la slide.",
        "[00:00:31] MARCA importante",
        "# Material adjunto",
        'ADJUNTO adjuntos/notas_del_profe.jpg (imagen PNG): "notas del profe.png"',
    ]
    tl = json.loads((carpeta / "timeline.json").read_text(encoding="utf-8"))
    assert tl["adjuntos"] == [
        {"id": slides, "nombre": "Slides_clase_5.pdf", "nombre_original": "Slides clase 5.pdf", "tipo": "pdf",
         "offset_ms": 30_500, "paginas": 3},
        {"id": notas, "nombre": "notas_del_profe.jpg", "nombre_original": "notas del profe.png", "tipo": "png",
         "offset_ms": None, "paginas": None},
    ]
    assert [e["tipo"] for e in tl["eventos"]] == ["marca"]  # los adjuntos no son eventos
    assert "2 adjunto(s)" in r.log
    # transcript.md sigue siendo sólo el habla.
    assert "ADJUNTO" not in (carpeta / "transcript.md").read_text(encoding="utf-8")


def test_linea_de_tiempo_sin_adjuntos_no_cambia(ctx):
    carpeta = crear(ctx, estado="procesando", etapa_="linea_de_tiempo")
    sesiones.registrar_evento(ctx.cfg, ctx.base, SID, evento_id="n-1", tipo="nota", offset_ms=3_000, texto="algo")
    r = linea_de_tiempo.ejecutar(ctx, Job(id=1, tipo="linea_de_tiempo", sesion_id=SID))
    assert (carpeta / "timeline.md").read_text(encoding="utf-8").splitlines() == [
        "# Clase: Materia de Prueba — 2026-10-15 — teórica", '[00:00:03] NOTA "algo"']
    assert json.loads((carpeta / "timeline.json").read_text(encoding="utf-8"))["adjuntos"] == []
    assert "adjunto" not in r.log


# --- carpeta de trabajo y prompt del apunte -------------------------------------------------------

def _contexto(ctx) -> dict[str, Any]:
    return {"materia_id": "prueba", "materia": "Materia de Prueba", "fecha": "2026-10-15",
            "fecha_larga": "15 de octubre de 2026", "numero_clase": 7, "tipo_clase": "teorica",
            "duracion_ms": 120_000, "inicio": INICIO}


def test_preparar_carpeta_copia_adjuntos_manifiesto_y_timeline(ctx):
    carpeta = para_apunte(ctx)  # timeline.md se escribió antes de que llegaran los adjuntos
    slides_pdf = pdf_dibujado(3)
    slides = adjuntar(ctx, SID, slides_pdf, "Slides clase 5.pdf", offset_ms=45_000)
    notas = adjuntar(ctx, SID, imagen("PNG"), "notas del profe.png")
    rota = adjuntar(ctx, SID, IMAGEN_ROTA, "rota.jpg")
    materia = ctx.cfg.materia("prueba")
    workdir = ctx.job_dir(SID, "apunte")

    fotos, material = etapa.preparar_carpeta(ctx.cfg, workdir, carpeta, materia, _contexto(ctx), "Primera clase.",
                                             base=ctx.base, sesion_id=SID, job_id=42)

    assert fotos == []
    assert [(p.adjunto["id"], p.nombre, p.ok) for p in material] == [
        (slides, "Slides_clase_5.pdf", True), (notas, "notas_del_profe.jpg", True), (rota, "rota.jpg", False)]
    entrada = workdir / "input"
    assert sorted(p.name for p in (entrada / "adjuntos").iterdir()) == ["Slides_clase_5.pdf", "notas_del_profe.jpg"]
    assert (entrada / "adjuntos" / "Slides_clase_5.pdf").read_bytes() == slides_pdf
    with Image.open(entrada / "adjuntos" / "notas_del_profe.jpg") as img:
        assert img.format == "JPEG"
    # Todos los considerados, también el que no se pudo preparar (regenerar no lo arreglaría).
    assert json.loads((entrada / "adjuntos.json").read_text(encoding="utf-8")) == {
        "job": 42, "adjuntos": [slides, notas, rota]}
    # input/timeline.md: encabezado de siempre y las líneas ADJUNTO rehechas con lo que hay ahora.
    base_tl = TIMELINE.splitlines()
    assert (entrada / "timeline.md").read_text(encoding="utf-8").splitlines() == base_tl[:3] + [
        '[00:00:45] ADJUNTO adjuntos/Slides_clase_5.pdf (PDF, 3 páginas): "Slides clase 5.pdf"',
        *base_tl[3:],
        "# Material adjunto",
        'ADJUNTO adjuntos/notas_del_profe.jpg (imagen PNG): "notas del profe.png"',
        'ADJUNTO adjuntos/rota.jpg (imagen JPEG): "rota.jpg" [no se pudo abrir: no está en adjuntos/]',
    ]
    assert (carpeta / "timeline.md").read_text(encoding="utf-8") == TIMELINE  # el de la sesión no se toca

    # El sistema dice cómo usarlos; el mensaje los nombra ({adjuntos}) y lleva el timeline refrescado.
    sistema, mensaje, _, _ = etapa.armar_prompt(ctx.cfg, materia, _contexto(ctx), workdir, fotos, material=material)
    assert "# Material adjunto" in sistema and "`input/adjuntos/`" in sistema
    assert "fuente principal" in sistema and "parámetro `pages`, hasta 20 páginas" in sistema
    assert "\\includegraphics[page=N,width=\\linewidth]{adjuntos/Slides_clase_5.pdf}" in sistema
    assert "nunca `../input/…`" in sistema
    assert ("- Material adjunto: 2 en input/adjuntos/ (Slides_clase_5.pdf: PDF, 3 páginas; notas_del_profe.jpg: "
            "imagen PNG) · no disponible (no lo uses): rota.jpg, no se pudo abrir.") in mensaje
    assert '[00:00:45] ADJUNTO adjuntos/Slides_clase_5.pdf (PDF, 3 páginas): "Slides clase 5.pdf"' in mensaje
    assert "{adjuntos}" not in mensaje and "Classmer-data" not in mensaje


def test_preparar_carpeta_rehace_y_saca_lo_viejo(ctx):
    # Una corrida anterior dejó input/adjuntos/ y otro manifiesto: la carpeta se arma desde cero.
    carpeta = para_apunte(ctx)
    materia = ctx.cfg.materia("prueba")
    workdir = ctx.job_dir(SID, "apunte")
    (workdir / "input" / "adjuntos").mkdir(parents=True)
    (workdir / "input" / "adjuntos" / "viejo.pdf").write_bytes(b"%PDF viejo")
    adjuntos.escribir_manifiesto(workdir, 1, ["otro"])
    _, material = etapa.preparar_carpeta(ctx.cfg, workdir, carpeta, materia, _contexto(ctx), "x",
                                         base=ctx.base, sesion_id=SID, job_id=2)
    assert material == [] and not (workdir / "input" / "adjuntos").exists()
    assert json.loads((workdir / "input" / "adjuntos.json").read_text(encoding="utf-8")) == {"job": 2, "adjuntos": []}
    assert (workdir / "input" / "timeline.md").read_text(encoding="utf-8") == TIMELINE


def test_texto_de_adjuntos_en_el_mensaje():
    def preparado(nombre: str, tipo: str = "pdf", ok: bool = True, **extra) -> adjuntos.Preparado:
        a = {"id": nombre, "nombre": nombre, "tipo": tipo, **extra}
        return adjuntos.Preparado(a, adjuntos.nombre_latex(a), ok, aviso=None if ok else "falta el archivo en "
                                  "Classmer-data")

    assert etapa.texto_adjuntos([]) == "no hay material adjunto"
    assert etapa.texto_adjuntos([preparado("a.pdf", paginas=1), preparado("b.webp", "webp")]) == (
        "2 en input/adjuntos/ (a.pdf: PDF, 1 página; b.jpg: imagen WebP)")
    assert etapa.texto_adjuntos([preparado("c.pdf", cifrado=True)]) == "1 en input/adjuntos/ (c.pdf: PDF cifrado)"
    assert etapa.texto_adjuntos([preparado("x.pdf", ok=False)]) == (
        "ninguno en input/adjuntos/ · no disponible (no lo uses): x.pdf, falta el archivo en Classmer-data")
    muchos = etapa.texto_adjuntos([preparado(f"s{i:02d}.pdf") for i in range(15)])
    assert muchos.startswith("15 en input/adjuntos/ (s00.pdf: PDF; ") and "s11.pdf" in muchos
    assert "s12.pdf" not in muchos and muchos.endswith("… y 3 más (ver input/timeline.md))")


# --- la etapa apunte y apuntes.adjuntos -----------------------------------------------------------

def test_etapa_guarda_los_adjuntos_que_uso(ctx, compilar_falso):
    para_apunte(ctx)
    a = adjuntar(ctx, SID, pdf_dibujado(2), "Slides.pdf", offset_ms=10_000)
    b = adjuntar(ctx, SID, IMAGEN_ROTA, "rota.jpg")
    agente = usar(ctx, AgenteFalso())

    r = etapa.ejecutar(ctx, Job(id=7, tipo="apunte", sesion_id=SID))

    assert agente.manifiestos == [{"job": 7, "adjuntos": [a, b]}]
    assert usados_del_actual(ctx) == [a, b]
    assert "· 2 adjunto(s)" in r.log.split("\n")[0]
    assert "- Material adjunto: 1 en input/adjuntos/ (Slides.pdf: PDF, 2 páginas)" in agente.trabajos[0].prompt
    log = leer_log(ctx)
    assert "apunte: clase 1, carpeta de trabajo lista (0 foto(s), 2 adjunto(s)), prompt apunte@" in log
    assert "apunte: aviso: el adjunto 'rota.jpg' no está en input/adjuntos/: no se pudo abrir (" in log
    # Con el apunte guardado y nada en curso, los dos quedan "usado" (también el que no se pudo preparar).
    assert estados(ctx) == {a: USADO, b: USADO}
    # Una versión nueva sin adjuntos nuevos registra los mismos.
    etapa.ejecutar(ctx, Job(id=8, tipo="apunte", sesion_id=SID))
    with ctx.base.conexion() as conn:
        assert [(f["version"], json.loads(f["adjuntos"])) for f in conn.execute(
            "SELECT version, adjuntos FROM apuntes ORDER BY version")] == [(1, [a, b]), (2, [a, b])]


def test_adjunto_que_llega_con_el_agente_andando_queda_sin_usar(ctx, compilar_falso):
    from classmer.worker import Worker

    para_apunte(ctx)
    a = adjuntar(ctx, SID, pdf_dibujado(2), "Slides.pdf")
    nuevos: list[str] = []
    vistos: list[dict[str, str]] = []

    def durante(_n: int) -> None:
        nuevos.append(adjuntar(ctx, SID, imagen("JPEG"), "foto del pizarron.jpg"))
        vistos.append(estados(ctx))

    usar(ctx, AgenteFalso(durante))
    w = Worker(ctx)
    try:
        with ctx.base.conexion() as conn:
            cola.encolar(conn, "apunte", SID)
            j = cola.tomar(conn, ["apunte"])
        w.ejecutar_job(j)
    finally:
        w.detener()

    (b,) = nuevos
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT estado FROM jobs WHERE id = ?", (j.id,)).fetchone()["estado"] == "hecho"
    # Con el agente andando: lo que tomó la corrida (su manifiesto) está pendiente; lo que llegó, sin_usar.
    assert vistos == [{a: PENDIENTE, b: SIN_USAR}]
    assert usados_del_actual(ctx) == [a]
    assert estados(ctx) == {a: USADO, b: SIN_USAR}
    with ctx.base.conexion() as conn:
        lista, sin_usar = adjuntos.resumen_api(ctx.cfg, conn, SID)
    assert sin_usar == 1 and [x["estado"] for x in lista] == [USADO, SIN_USAR]


def test_cambio_de_facturacion_usa_los_adjuntos_del_intento_que_quedo(ctx, home, compilar_falso):
    with open(home / ".env", "a", encoding="utf-8") as f:
        f.write("ANTHROPIC_API_KEY=sk-ant-falsa-123\n")
    ctx.cfg = cargar_config(home)  # api_luego_max: primero la API
    para_apunte(ctx)
    a = adjuntar(ctx, SID, pdf_dibujado(2), "Slides.pdf")
    nuevos: list[str] = []

    def durante(n: int) -> None:
        if n == 0:  # llega con el intento por la API andando; ese intento se descarta
            nuevos.append(adjuntar(ctx, SID, imagen("JPEG"), "foto.jpg"))

    agente = usar(ctx, AgenteFalso(durante, fallas=[SinCreditos("api", "sin_creditos", "Credit balance is too low")]))
    r = etapa.ejecutar(ctx, Job(id=9, tipo="apunte", sesion_id=SID))

    (b,) = nuevos
    assert [t.facturacion for t in agente.trabajos] == ["api", "max"] and r.facturacion == "max"
    assert agente.manifiestos == [{"job": 9, "adjuntos": [a]}, {"job": 9, "adjuntos": [a, b]}]
    assert usados_del_actual(ctx) == [a, b]
    assert estados(ctx) == {a: USADO, b: USADO}


# --- publicar -------------------------------------------------------------------------------------

def para_publicar(ctx, sid: str = SID, numero: int = 7) -> Path:
    """Sesión con su apunte v1 (que compiló) armado a mano, como en test_publicar."""
    carpeta = crear(ctx, sid, etapa_="publicar", numero=numero)
    v1 = carpeta / "apunte" / "v1"
    v1.mkdir(parents=True)
    (v1 / "apunte.tex").write_text(TEX, encoding="utf-8")
    (v1 / "classmer.cls").write_text("% plantilla\n", encoding="utf-8")
    (v1 / "apunte.pdf").write_bytes(b"%PDF-1.5 falso")
    (carpeta / "transcript.md").write_text("# Transcript\n", encoding="utf-8")
    with ctx.base.conexion() as conn:
        conn.execute(
            "INSERT INTO apuntes (sesion_id, version, actual, motor, modelo, ruta_tex, ruta_pdf, compilo, titulo, "
            "resumen, creado) VALUES (?, 1, 1, 'fake', 'fake', 'apunte/v1/apunte.tex', 'apunte/v1/apunte.pdf', 1, "
            "'Gradiente', 'Se vio el gradiente.', ?)", (sid, ahora_iso()))
    return carpeta


def test_publicar_copia_los_adjuntos(ctx, materia_dir):
    para_publicar(ctx)
    slides = pdf_dibujado(3)
    adjuntar(ctx, SID, slides, "Slides clase 5.pdf", offset_ms=1_000)
    adjuntar(ctx, SID, imagen("PNG"), "notas del profe.png")
    adjuntar(ctx, SID, IMAGEN_ROTA, "rota.jpg")

    publicar.ejecutar(ctx, Job(id=2, tipo="publicar", sesion_id=SID))

    (destino,) = (materia_dir / "Clases-Classmer").glob("2026-10-15 Clase 07 - *")
    assert sorted(p.name for p in (destino / "adjuntos").iterdir()) == ["Slides_clase_5.pdf", "notas_del_profe.jpg"]
    assert (destino / "adjuntos" / "Slides_clase_5.pdf").read_bytes() == slides
    with Image.open(destino / "adjuntos" / "notas_del_profe.jpg") as img:
        assert img.format == "JPEG"
    assert (destino / "apunte.pdf").is_file() and (destino / "transcript.md").is_file()
    assert "publicar: aviso: no copié el adjunto 'rota.jpg' a adjuntos/: no se pudo abrir" in leer_log(ctx)
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT estado FROM sesiones WHERE id = ?", (SID,)).fetchone()["estado"] == "lista"


def test_publicar_sin_adjuntos_no_crea_la_carpeta(ctx, materia_dir):
    para_publicar(ctx)
    publicar.ejecutar(ctx, Job(id=2, tipo="publicar", sesion_id=SID))
    (destino,) = (materia_dir / "Clases-Classmer").glob("2026-10-15 Clase 07 - *")
    assert not (destino / "adjuntos").exists()

    # Si ninguno se pudo copiar, tampoco queda una carpeta vacía.
    otra = "adj-0002"
    para_publicar(ctx, otra, numero=8)
    adjuntar(ctx, otra, IMAGEN_ROTA, "rota.jpg")
    publicar.ejecutar(ctx, Job(id=3, tipo="publicar", sesion_id=otra))
    (destino,) = (materia_dir / "Clases-Classmer").glob("2026-10-15 Clase 08 - *")
    assert not (destino / "adjuntos").exists()
    assert "no copié el adjunto 'rota.jpg'" in leer_log(ctx, otra)


# --- compilación real -----------------------------------------------------------------------------

CUERPO_SLIDES = ("\\begin{document}\n"
                 "\\section{Slides}\n"
                 "\\includegraphics[page=2,width=.5\\linewidth]{adjuntos/slides.pdf}\n\n"
                 "\\includegraphics[page=1,width=.3\\linewidth]{slides.pdf}\n"
                 "\\end{document}\n")


def _paginas_incluidas(pdf: Path) -> int:
    """Cuántas imágenes o páginas de otro PDF (XObject) quedaron embebidas en el PDF."""
    from pypdf import PdfReader

    total = 0
    for pagina in PdfReader(pdf).pages:
        recursos = pagina["/Resources"]
        total += len(recursos["/XObject"]) if "/XObject" in recursos else 0
    return total


@pytest.mark.latex
@requiere("pdflatex")
def test_paginas_de_un_pdf_adjunto_compilan_en_trabajo_y_publicada(tmp_path):
    if not str(tmp_path).isascii():
        pytest.skip("la carpeta temporal tiene caracteres no ASCII")
    slides = pdf_dibujado(3)
    # Carpeta de trabajo: se compila en output/ y el adjunto está en ../input/adjuntos/.
    trabajo = tmp_path / "trabajo"
    (trabajo / "input" / "adjuntos").mkdir(parents=True)
    (trabajo / "input" / "adjuntos" / "slides.pdf").write_bytes(slides)
    salida = trabajo / "output"
    latex.copiar_plantilla(salida)
    (salida / "apunte.tex").write_text(PREAMBULO + CUERPO_SLIDES, encoding="utf-8")
    assert latex.lecturas_externas_en(salida) == []  # el filtro no la rechaza
    r = latex.compilar(salida, pasadas=1)
    assert r.ok, r.log
    assert r.errores == [] and r.paginas == 1
    assert _paginas_incluidas(r.pdf) == 2  # las dos páginas de las slides quedaron embebidas

    # Carpeta publicada: el mismo .tex, con adjuntos/ al lado.
    publicada = tmp_path / "publicada"
    (publicada / "adjuntos").mkdir(parents=True)
    (publicada / "adjuntos" / "slides.pdf").write_bytes(slides)
    latex.copiar_plantilla(publicada)
    shutil.copyfile(salida / "apunte.tex", publicada / "apunte.tex")
    assert latex.lecturas_externas_en(publicada) == []
    r = latex.compilar(publicada, pasadas=1)
    assert r.ok, r.log
    assert _paginas_incluidas(r.pdf) == 2


@pytest.mark.latex
@requiere("pdflatex")
def test_etapa_compila_con_paginas_del_adjunto_y_publicada_tambien(ctx, materia_dir):
    """De punta a punta: el agente (falso) incluye una página de las slides, el server compila de
    verdad en la carpeta de trabajo y el .tex publicado compila al lado de su adjuntos/."""
    para_apunte(ctx)
    # Nombres como los que arma el server: con '_' y '-' (sin tildes, espacios ni puntos internos).
    adjuntar(ctx, SID, pdf_dibujado(3), "Slides clase 5 - Límites.pdf", offset_ms=60_000)
    adjuntar(ctx, SID, imagen("PNG"), "notas del profe.png")
    tex = TEX.replace("El gradiente.",
                      "\\includegraphics[page=3,width=\\linewidth]{adjuntos/Slides_clase_5_-_Limites.pdf}\n"
                      "\\pizarra{adjuntos/notas_del_profe.jpg}{}{Notas del profe}")
    usar(ctx, AgenteFalso(tex=tex))
    r = etapa.ejecutar(ctx, Job(id=5, tipo="apunte", sesion_id=SID))
    assert "v1 compiló" in r.log, r.log
    v1 = ctx.sesion_dir(SID) / "apunte" / "v1"
    assert "Foto no encontrada" not in (v1 / "compilacion.log").read_text(encoding="utf-8", errors="replace")
    assert _paginas_incluidas(v1 / "apunte.pdf") == 2  # la página de las slides y la imagen

    publicar.ejecutar(ctx, Job(id=6, tipo="publicar", sesion_id=SID))
    (destino,) = (materia_dir / "Clases-Classmer").glob("2026-10-15 Clase 01 - *")
    assert sorted(p.name for p in (destino / "adjuntos").iterdir()) == [
        "Slides_clase_5_-_Limites.pdf", "notas_del_profe.jpg"]
    copia = ctx.cfg.datos / "recompilar"  # sin tocar la carpeta publicada
    shutil.copytree(destino, copia)
    (copia / "apunte.pdf").unlink()
    assert latex.lecturas_externas_en(copia) == []
    r = latex.compilar(copia, pasadas=1)
    assert r.ok, r.log
    assert not [a for a in r.avisos if "Foto no encontrada" in a]
    assert _paginas_incluidas(r.pdf) == 2
