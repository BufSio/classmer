"""Etapas publicar y notificar: estructura, no pisar nada ajeno (v2), carpeta de materia ausente,
INDICE-CLASES.md, el caso "no compiló" y el reemplazo de versiones (la vieja se archiva en
apuntes-anteriores; fallas, bloqueos y sesiones de antes de la tabla publicaciones)."""

from __future__ import annotations

import errno
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from classmer import cola, sesiones
from classmer.borrado import borrar_sesion
from classmer.cola import Job
from classmer.pipeline import ErrorEtapa, actualizar_sesion
from classmer.pipeline import notificar as etapa_notificar
from classmer.pipeline import publicar
from classmer.pipeline.publicar import (
    AVISO_EN_USO,
    CARPETA_ARCHIVO,
    ERROR_NO_COMPILO,
    INDICE,
    MARCA_INDICE,
    avisos_publicacion,
    carpeta_archivo,
    ejecutar,
    publicaciones_de,
)
from classmer.util import ahora_iso, zona

SID = "pub-0001"
TITULO = "Derivada direccional: gradiente"
CARPETA = "2026-10-15 Clase 07 - Derivada direccional gradiente"  # sin ':' (Windows)
RESUMEN = ("Se definió la derivada direccional y se vio que el gradiente es perpendicular a las curvas de "
           "nivel. Después se hicieron ejemplos con funciones de dos variables y quedó una tarea.")


def armar(ctx, sid: str = SID, *, materia_id: str = "prueba", compilo: bool = True, numero: int | None = 7,
          titulo: str | None = TITULO, inicio: str = "2026-10-15T14:00:05-03:00", version: int = 1,
          resumen: str = RESUMEN) -> Path:
    """Sesión 'procesando' con apunte/vN/ armado a mano, fotos_pdf/ y transcript.md."""
    with ctx.base.conexion() as conn:
        if conn.execute("SELECT 1 FROM sesiones WHERE id = ?", (sid,)).fetchone() is None:
            sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id=materia_id, origen_materia="manual",
                           tipo_clase="teorica", inicio=inicio, dispositivo="test")
            actualizar_sesion(conn, sid, ctx.cfg.zona_horaria, estado="procesando", etapa="publicar",
                              numero_clase=numero)
    carpeta = ctx.sesion_dir(sid)
    v = carpeta / "apunte" / f"v{version}"
    v.mkdir(parents=True, exist_ok=True)
    (v / "apunte.tex").write_text(f"\\documentclass{{classmer}}\n% v{version}\n", encoding="utf-8")
    (v / "classmer.cls").write_text("% plantilla\n", encoding="utf-8")
    (v / "compilacion.log").write_text("! Undefined control sequence.\n", encoding="utf-8")
    (v / "meta.json").write_text("{}", encoding="utf-8")
    if compilo:
        (v / "apunte.pdf").write_bytes(b"%PDF-1.5 falso " + sid.encode())
    fotos = carpeta / "fotos_pdf"
    fotos.mkdir(exist_ok=True)
    (fotos / "pizarra_01.jpg").write_bytes(b"foto1")
    (fotos / "pizarra_02.jpg").write_bytes(b"foto2")
    (carpeta / "transcript.md").write_text("# Transcript\n[00:00:01] PROFE: hola\n", encoding="utf-8")
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE apuntes SET actual = 0 WHERE sesion_id = ?", (sid,))
        conn.execute(
            """
            INSERT INTO apuntes (sesion_id, version, actual, motor, modelo, ruta_tex, ruta_pdf, compilo,
                                 titulo, resumen, creado)
            VALUES (?, ?, 1, 'fake', 'fake', ?, ?, ?, ?, ?, ?)
            """,
            (sid, version, f"apunte/v{version}/apunte.tex",
             f"apunte/v{version}/apunte.pdf" if compilo else None, int(compilo), titulo, resumen,
             ahora_iso()),
        )
    return carpeta


def job(sid: str = SID, tipo: str = "publicar") -> Job:
    return Job(id=1, tipo=tipo, sesion_id=sid)


def fila(ctx, sid: str = SID):
    with ctx.base.conexion() as conn:
        return conn.execute("SELECT * FROM sesiones WHERE id = ?", (sid,)).fetchone()


def contenido(carpeta: Path) -> dict[str, bytes]:
    return {p.relative_to(carpeta).as_posix(): p.read_bytes() for p in sorted(carpeta.rglob("*")) if p.is_file()}


ARCHIVADA_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2} (?P<nombre>.+)$")


def archivadas(ctx, materia: str = "prueba") -> list[tuple[str, Path]]:
    """(nombre de la carpeta publicada, carpeta en apuntes-anteriores/<materia>/) de cada versión archivada."""
    carpeta = ctx.cfg.datos / CARPETA_ARCHIVO / materia
    if not carpeta.is_dir():
        return []
    salida = []
    for p in sorted(carpeta.iterdir()):
        m = ARCHIVADA_RE.match(p.name)
        assert m, p.name
        salida.append((re.sub(r" \(\d+\)$", "", m["nombre"]), p))  # " (2)": dos en el mismo segundo
    return salida


def pubs(ctx, sid: str = SID) -> list[dict]:
    with ctx.base.conexion() as conn:
        return [dict(f) for f in publicaciones_de(conn, sid)]


def leer_log(ctx, sid: str = SID) -> str:
    return (ctx.sesion_dir(sid) / "pipeline.log").read_text(encoding="utf-8")


def nombres(carpeta: Path) -> list[str]:
    return sorted(p.name for p in carpeta.iterdir())


def filas_indice(clases: Path) -> list[str]:
    return [l for l in (clases / INDICE).read_text(encoding="utf-8").splitlines() if l.startswith("| 2026")]


def test_publica_estructura_e_indice(ctx, materia_dir):
    carpeta = armar(ctx)
    r = ejecutar(ctx, job())
    assert r.siguientes == [("notificar", {})]
    destino = materia_dir / "Clases-Classmer" / CARPETA
    assert contenido(destino) == {
        "apunte.pdf": b"%PDF-1.5 falso " + SID.encode(),
        "apunte.tex": (carpeta / "apunte/v1/apunte.tex").read_bytes(),
        "classmer.cls": (carpeta / "apunte/v1/classmer.cls").read_bytes(),
        "fotos/pizarra_01.jpg": b"foto1",
        "fotos/pizarra_02.jpg": b"foto2",
        "transcript.md": (carpeta / "transcript.md").read_bytes(),
    }
    s = fila(ctx)
    assert s["estado"] == "lista" and s["error"] is None
    assert s["ruta_publicada"] == str(destino)
    assert s["titulo"] == TITULO

    indice = (materia_dir / "Clases-Classmer" / INDICE).read_text(encoding="utf-8").splitlines()
    assert indice[0] == MARCA_INDICE
    assert "# Clases de Materia de Prueba" in indice
    assert "| Fecha | Clase | Título | Carpeta | Resumen |" in indice
    fila_md = [l for l in indice if l.startswith("| 2026-10-15")]
    assert len(fila_md) == 1
    assert fila_md[0].startswith(f"| 2026-10-15 | 07 | {TITULO} | [{CARPETA}](<{CARPETA}/>) | Se definió")
    assert fila_md[0].endswith("… |")  # resumen corto
    # Nada más se creó en la carpeta de la materia.
    assert sorted(p.name for p in materia_dir.iterdir()) == ["Clases-Classmer", "PROMPT-AGENTE.md"]


def test_transcript_publicado_lleva_la_materia_asignada_despues(ctx, materia_dir):
    # En Auto, linea_de_tiempo escribe transcript.md antes de que clasificar elija la materia.
    carpeta = armar(ctx)
    cuerpo = "[00:00:01] PROFE: hola\n[00:00:05] ALUMNO: ¿y esto?\n"
    (carpeta / "transcript.md").write_bytes(
        ("# Transcript: materia sin asignar — 2026-10-15 — teórica\n" + cuerpo).encode("utf-8"))
    ejecutar(ctx, job())
    publicado = (materia_dir / "Clases-Classmer" / CARPETA / "transcript.md").read_bytes().decode("utf-8")
    assert publicado == "# Transcript: Materia de Prueba — 2026-10-15 — teórica\n" + cuerpo
    # El de la sesión no se toca.
    assert (carpeta / "transcript.md").read_bytes().decode("utf-8").startswith("# Transcript: materia sin asignar")


def test_no_pisa_lo_ajeno_usa_v2_y_republicar_archiva(ctx, materia_dir):
    armar(ctx)
    clases = materia_dir / "Clases-Classmer"
    ajena = clases / CARPETA
    ajena.mkdir(parents=True)
    (ajena / "apunte.tex").write_text("lo editó el usuario", encoding="utf-8")
    antes = contenido(ajena)

    ejecutar(ctx, job())
    v2 = clases / f"{CARPETA} (v2)"
    assert contenido(ajena) == antes
    assert (v2 / "apunte.pdf").is_file() and (v2 / "fotos" / "pizarra_02.jpg").is_file()
    assert fila(ctx)["ruta_publicada"] == str(v2)

    # Re-publicar (p. ej., reprocesar con un apunte v2): la "(v2)" es de esta sesión, así que se
    # archiva y la nueva vuelve a quedar en "(v2)" (el nombre normal sigue ocupado por la ajena).
    armar(ctx, version=2, titulo=TITULO)
    copia_v2 = contenido(v2)
    ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, f"{CARPETA} (v2)", INDICE]
    assert contenido(ajena) == antes
    assert (v2 / "apunte.tex").read_text(encoding="utf-8").endswith("% v2\n")
    [(nombre, archivada)] = archivadas(ctx)
    assert nombre == f"{CARPETA} (v2)" and contenido(archivada) == copia_v2
    assert fila(ctx)["ruta_publicada"] == str(v2)
    indice = (clases / INDICE).read_text(encoding="utf-8")
    assert indice.count("| 2026-10-15 |") == 1 and f"<{CARPETA} (v2)/>" in indice
    assert [(p["estado"], p["version"]) for p in pubs(ctx)] == [("vigente", 2), ("archivada", 1)]


def test_no_crea_la_carpeta_de_la_materia(ctx, tmp_path):
    carpeta = armar(ctx, materia_id="otra")
    assert not (tmp_path / "otra-materia").exists()
    r = ejecutar(ctx, job())
    assert r.siguientes == [("notificar", {})]
    assert not (tmp_path / "otra-materia").exists()
    s = fila(ctx)
    assert s["estado"] == "lista" and s["ruta_publicada"] is None and s["titulo"] == TITULO
    log = (carpeta / "pipeline.log").read_text(encoding="utf-8")
    assert "no existe la carpeta de la materia" in log


def test_no_compilo_publica_tex_y_deja_error(ctx, materia_dir):
    armar(ctx, compilo=False)
    r = ejecutar(ctx, job())
    assert r.siguientes == [("notificar", {})]
    destino = materia_dir / "Clases-Classmer" / f"{CARPETA} (no compiló)"
    archivos = set(contenido(destino))
    assert {"apunte.tex", "transcript.md", "compilacion.log", "classmer.cls"} <= archivos
    assert "apunte.pdf" not in archivos
    s = fila(ctx)
    assert s["estado"] == "error"
    assert s["error"] == ("el apunte no compiló: se guardaron el .tex y el log; "
                          f"reintentá con classmer reprocesar {SID} --desde apunte")
    assert s["error"] == ERROR_NO_COMPILO.format(id=SID)
    assert s["ruta_publicada"] == str(destino)


def test_sin_apunte_no_reintenta(ctx):
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=SID, materia_id="prueba", origen_materia="manual",
                       tipo_clase=None, inicio="2026-10-15T14:00:05-03:00", dispositivo="test")
    with pytest.raises(ErrorEtapa) as e:
        ejecutar(ctx, job())
    assert e.value.reintentable is False


def test_usa_la_version_actual_mas_alta(ctx, materia_dir):
    armar(ctx, titulo="Viejo")
    armar(ctx, version=2, titulo="Nuevo")
    ejecutar(ctx, job())
    destino = materia_dir / "Clases-Classmer" / "2026-10-15 Clase 07 - Nuevo"
    assert (destino / "apunte.tex").read_text(encoding="utf-8").endswith("% v2\n")
    assert fila(ctx)["titulo"] == "Nuevo"


def test_indice_ajeno_no_se_toca(ctx, materia_dir):
    armar(ctx)
    clases = materia_dir / "Clases-Classmer"
    clases.mkdir()
    (clases / INDICE).write_text("mi índice a mano\n", encoding="utf-8")
    ejecutar(ctx, job())
    assert (clases / INDICE).read_text(encoding="utf-8") == "mi índice a mano\n"
    assert (clases / CARPETA / "apunte.pdf").is_file()
    assert "no lo escribió Classmer" in (ctx.sesion_dir(SID) / "pipeline.log").read_text(encoding="utf-8")


def test_indice_con_varias_clases_y_numero_calculado(ctx, materia_dir):
    armar(ctx, "pub-a", numero=None, titulo="Primera | intro", inicio="2026-10-08T14:00:00-03:00")
    armar(ctx, "pub-b", numero=None, titulo="Segunda", inicio="2026-10-15T14:00:00-03:00")
    ejecutar(ctx, job("pub-b"))
    ejecutar(ctx, job("pub-a"))
    assert fila(ctx, "pub-a")["numero_clase"] == 1 and fila(ctx, "pub-b")["numero_clase"] == 2
    clases = materia_dir / "Clases-Classmer"
    assert (clases / "2026-10-08 Clase 01 - Primera intro").is_dir()
    assert (clases / "2026-10-15 Clase 02 - Segunda").is_dir()
    filas = [l for l in (clases / INDICE).read_text(encoding="utf-8").splitlines() if l.startswith("| 2026")]
    assert [l.split(" | ")[:3] for l in filas] == [
        ["| 2026-10-08", "01", "Primera \\| intro"],
        ["| 2026-10-15", "02", "Segunda"],
    ]


def test_con_worker_no_compilo_queda_en_error_y_encola_notificar(ctx):
    from classmer.worker import Worker

    armar(ctx, compilo=False)
    w = Worker(ctx)
    try:
        with ctx.base.conexion() as conn:
            cola.encolar(conn, "publicar", SID)
            j = cola.tomar(conn, ["publicar"])
        w.ejecutar_job(j)
        s = fila(ctx)
        assert s["estado"] == "error" and s["error"].startswith("el apunte no compiló")
        with ctx.base.conexion() as conn:
            j2 = cola.tomar(conn, ["notificar"])
        assert j2 is not None
        w.ejecutar_job(j2)  # notificar es silenciosa: no cambia el estado
        assert fila(ctx)["estado"] == "error"
    finally:
        w.detener()


# --- versiones: la nueva reemplaza a la vieja --------------------------------------------------

def test_republicar_archiva_la_anterior_y_publica_sin_v2(ctx, materia_dir):
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    vieja = clases / CARPETA
    (vieja / "mis-notas.txt").write_text("lo agregó el usuario", encoding="utf-8")
    antes = contenido(vieja)

    armar(ctx, version=2)
    r = ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, INDICE]  # sin "(v2)"
    assert (vieja / "apunte.tex").read_text(encoding="utf-8").endswith("% v2\n")
    assert not (vieja / "mis-notas.txt").exists()
    # La anterior se movió entera (con lo que agregó el usuario) a apuntes-anteriores/<materia>/.
    [(nombre, archivada)] = archivadas(ctx)
    assert nombre == CARPETA and contenido(archivada) == antes
    assert archivada.parent == carpeta_archivo(ctx.cfg) / "prueba"
    assert carpeta_archivo(ctx.cfg) == ctx.cfg.datos / "apuntes-anteriores"

    s = fila(ctx)
    assert s["estado"] == "lista" and s["ruta_publicada"] == str(vieja)
    p = pubs(ctx)
    assert [(x["estado"], x["version"], x["compilo"], x["ruta"]) for x in p] == [
        ("vigente", 2, 1, str(vieja)), ("archivada", 1, 1, str(vieja))]
    assert p[0]["archivo"] is None and p[1]["archivo"] == str(archivada)
    assert "archivé la versión anterior" in leer_log(ctx) and str(archivada) in r.log
    [fila_md] = filas_indice(clases)
    assert f"[{CARPETA}](<{CARPETA}/>)" in fila_md


def test_titulo_nuevo_publica_con_el_nombre_nuevo_y_archiva_la_vieja(ctx, materia_dir):
    armar(ctx, titulo="Viejo")
    ejecutar(ctx, job())
    armar(ctx, version=2, titulo="Nuevo")
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    nueva = "2026-10-15 Clase 07 - Nuevo"
    assert nombres(clases) == [nueva, INDICE]
    assert [n for n, _ in archivadas(ctx)] == ["2026-10-15 Clase 07 - Viejo"]
    assert fila(ctx)["ruta_publicada"] == str(clases / nueva) and fila(ctx)["titulo"] == "Nuevo"
    [fila_md] = filas_indice(clases)
    assert f"| Nuevo | [{nueva}](<{nueva}/>) |" in fila_md


def test_carpeta_anterior_borrada_a_mano_publica_normal(ctx, materia_dir):
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    shutil.rmtree(clases / CARPETA)

    armar(ctx, version=2)
    ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, INDICE]
    assert (clases / CARPETA / "apunte.tex").read_text(encoding="utf-8").endswith("% v2\n")
    assert archivadas(ctx) == []
    assert [(x["estado"], x["version"]) for x in pubs(ctx)] == [("vigente", 2), ("no_esta", 1)]
    assert "ya no está: no archivo nada" in leer_log(ctx)


@pytest.mark.parametrize("titulo_nuevo", [TITULO, "Otro título"])
def test_falla_en_la_copia_restaura_la_anterior(ctx, materia_dir, monkeypatch, titulo_nuevo):
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    vieja = clases / CARPETA
    (vieja / "mis-notas.txt").write_text("lo agregó el usuario", encoding="utf-8")
    antes, indice_antes, pubs_antes = contenido(vieja), (clases / INDICE).read_bytes(), pubs(ctx)

    original = publicar.copiar_exclusivo

    def copiar(origen: Path, destino: Path) -> None:
        if destino.name == "apunte.tex":  # el PDF ya se copió: la nueva queda a medias
            raise OSError(errno.ENOSPC, "disco lleno")
        original(origen, destino)

    monkeypatch.setattr(publicar, "copiar_exclusivo", copiar)
    armar(ctx, version=2, titulo=titulo_nuevo)
    with pytest.raises(OSError, match="disco lleno"):
        ejecutar(ctx, job())

    assert nombres(clases) == [CARPETA, INDICE]  # ni la nueva a medias ni nada de más
    assert contenido(vieja) == antes
    assert archivadas(ctx) == []
    assert (clases / INDICE).read_bytes() == indice_antes
    assert fila(ctx)["ruta_publicada"] == str(vieja) and fila(ctx)["titulo"] == TITULO
    assert pubs(ctx) == pubs_antes
    log = leer_log(ctx)
    assert "falló la publicación nueva" in log and "borré la copia a medias" in log
    assert f"restauré {vieja}" in log


def test_falla_en_la_base_restaura_la_anterior(ctx, materia_dir, monkeypatch):
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    vieja = clases / CARPETA
    antes, pubs_antes = contenido(vieja), pubs(ctx)

    def actualizar(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(publicar, "actualizar_sesion", actualizar)
    armar(ctx, version=2, titulo="Otro título")
    with pytest.raises(sqlite3.OperationalError):
        ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, INDICE]
    assert contenido(vieja) == antes and archivadas(ctx) == []
    assert fila(ctx)["ruta_publicada"] == str(vieja) and pubs(ctx) == pubs_antes
    assert f"restauré {vieja}" in leer_log(ctx)


def test_no_reemplaza_una_compilada_por_una_que_no_compilo(ctx, materia_dir, monkeypatch):
    indexadas = []
    monkeypatch.setattr(publicar, "indexar_publicada", lambda _ctx, sid: indexadas.append(sid))
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    vieja = clases / CARPETA
    antes, pubs_antes = contenido(vieja), pubs(ctx)

    armar(ctx, version=2, compilo=False)
    r = ejecutar(ctx, job())
    assert r.siguientes == [("notificar", {})]
    assert nombres(clases) == [CARPETA, INDICE]  # no hay "(no compiló)"
    assert contenido(vieja) == antes and archivadas(ctx) == []
    s = fila(ctx)
    assert s["estado"] == "error" and s["error"] == ERROR_NO_COMPILO.format(id=SID)
    assert s["ruta_publicada"] == str(vieja)
    assert pubs(ctx) == pubs_antes
    assert "no compiló y la versión publicada sí" in leer_log(ctx)
    assert "queda publicada la versión anterior" in r.log
    assert indexadas == [SID]  # la búsqueda sigue con la publicada (la vigente)
    [fila_md] = filas_indice(clases)
    assert f"<{CARPETA}/>" in fila_md


def test_si_ninguna_compilo_se_reemplaza_y_una_compilada_reemplaza_a_la_que_no(ctx, materia_dir):
    clases = materia_dir / "Clases-Classmer"
    no_compilo = f"{CARPETA} (no compiló)"
    armar(ctx, compilo=False)
    ejecutar(ctx, job())
    armar(ctx, version=2, compilo=False)
    ejecutar(ctx, job())
    assert nombres(clases) == [no_compilo, INDICE]
    assert (clases / no_compilo / "apunte.tex").read_text(encoding="utf-8").endswith("% v2\n")
    assert [n for n, _ in archivadas(ctx)] == [no_compilo]
    assert fila(ctx)["estado"] == "error"

    armar(ctx, version=3)
    ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, INDICE]
    assert sorted(n for n, _ in archivadas(ctx)) == [no_compilo, no_compilo]
    assert fila(ctx)["estado"] == "lista" and fila(ctx)["ruta_publicada"] == str(clases / CARPETA)
    assert [(p["estado"], p["version"], p["compilo"]) for p in pubs(ctx)] == [
        ("vigente", 3, 1), ("archivada", 2, 0), ("archivada", 1, 0)]


def test_carpeta_en_uso_publica_al_lado_y_la_archiva_en_la_proxima(ctx, materia_dir, monkeypatch):
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    vieja = clases / CARPETA
    antes = contenido(vieja)
    esperas: list[float] = []
    monkeypatch.setattr(publicar, "dormir", esperas.append)
    original = publicar._renombrar

    def renombrar(origen, destino):
        if Path(origen) == vieja:  # como Windows con el PDF abierto
            raise PermissionError(errno.EACCES, "Acceso denegado", str(origen))
        return original(origen, destino)

    monkeypatch.setattr(publicar, "_renombrar", renombrar)
    armar(ctx, version=2)
    ejecutar(ctx, job())
    assert esperas == list(publicar.ESPERAS_ARCHIVAR) and 9 <= sum(esperas) <= 11
    al_lado = clases / f"{CARPETA} (v2)"
    assert nombres(clases) == [CARPETA, f"{CARPETA} (v2)", INDICE]
    assert contenido(vieja) == antes
    assert (al_lado / "apunte.tex").read_text(encoding="utf-8").endswith("% v2\n")
    assert fila(ctx)["ruta_publicada"] == str(al_lado)
    p = pubs(ctx)
    assert [(x["estado"], x["ruta"]) for x in p] == [("vigente", str(al_lado)), ("sin_archivar", str(vieja))]
    assert p[1]["aviso"] == AVISO_EN_USO
    with ctx.base.conexion() as conn:
        assert [a["ruta"] for a in avisos_publicacion(conn, SID)] == [str(vieja)]
        assert [a["ruta"] for a in avisos_publicacion(conn)] == [str(vieja)]
        assert avisos_publicacion(conn, "otra-sesion") == []
    assert "en uso" in leer_log(ctx)
    [fila_md] = filas_indice(clases)
    assert f"<{CARPETA} (v2)/>" in fila_md

    # El usuario cerró el PDF: la próxima publicación archiva las dos y vuelve al nombre normal.
    monkeypatch.setattr(publicar, "_renombrar", original)
    esperas.clear()
    armar(ctx, version=3)
    ejecutar(ctx, job())
    assert esperas == []
    assert nombres(clases) == [CARPETA, INDICE]
    assert (vieja / "apunte.tex").read_text(encoding="utf-8").endswith("% v3\n")
    assert sorted(n for n, _ in archivadas(ctx)) == [CARPETA, f"{CARPETA} (v2)"]
    assert [(x["estado"], x["version"]) for x in pubs(ctx)] == [
        ("vigente", 3), ("archivada", 2), ("archivada", 1)]
    with ctx.base.conexion() as conn:
        assert avisos_publicacion(conn) == []
    assert fila(ctx)["ruta_publicada"] == str(vieja)


def test_otro_error_al_archivar_no_reintenta(ctx, materia_dir, monkeypatch):
    armar(ctx)
    ejecutar(ctx, job())
    vieja = materia_dir / "Clases-Classmer" / CARPETA
    esperas: list[float] = []
    monkeypatch.setattr(publicar, "dormir", esperas.append)
    original = publicar._renombrar

    def renombrar(origen, destino):
        if Path(origen) == vieja:
            raise OSError(errno.EIO, "error de E/S")
        return original(origen, destino)

    monkeypatch.setattr(publicar, "_renombrar", renombrar)
    armar(ctx, version=2)
    ejecutar(ctx, job())
    assert esperas == []
    p = pubs(ctx)
    assert p[1]["estado"] == "sin_archivar" and "error de E/S" in p[1]["aviso"]
    assert vieja.is_dir() and fila(ctx)["ruta_publicada"] == str(vieja.parent / f"{CARPETA} (v2)")


@pytest.mark.skipif(sys.platform != "win32", reason="el bloqueo por un archivo abierto es de Windows")
def test_carpeta_en_uso_de_verdad_en_windows(ctx, materia_dir, monkeypatch):
    monkeypatch.setattr(publicar, "ESPERAS_ARCHIVAR", (0.01,))
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    armar(ctx, version=2)
    with open(clases / CARPETA / "apunte.pdf", "rb"):  # el PDF abierto en el visor
        ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, f"{CARPETA} (v2)", INDICE]
    assert pubs(ctx)[1]["estado"] == "sin_archivar"
    armar(ctx, version=3)
    ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, INDICE] and len(archivadas(ctx)) == 2


def test_sesion_vieja_sin_filas_en_publicaciones(ctx, materia_dir):
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    antes = contenido(clases / CARPETA)
    with ctx.base.conexion() as conn:  # como una sesión publicada antes de la tabla publicaciones
        conn.execute("DELETE FROM publicaciones")

    armar(ctx, version=2)
    ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, INDICE]
    [(nombre, archivada)] = archivadas(ctx)
    assert nombre == CARPETA and contenido(archivada) == antes
    assert [(p["estado"], p["version"], p["ruta"], p["archivo"]) for p in pubs(ctx)] == [
        ("vigente", 2, str(clases / CARPETA), None),
        ("archivada", None, str(clases / CARPETA), str(archivada))]


def test_sesion_vieja_sin_filas_tampoco_reemplaza_la_compilada(ctx, materia_dir):
    armar(ctx)
    ejecutar(ctx, job())
    with ctx.base.conexion() as conn:
        conn.execute("DELETE FROM publicaciones")
    armar(ctx, version=2, compilo=False)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    assert nombres(clases) == [CARPETA, INDICE] and archivadas(ctx) == []
    assert fila(ctx)["ruta_publicada"] == str(clases / CARPETA)


@pytest.mark.parametrize("caso", ["afuera", "raiz", "archivo"])
def test_nunca_mueve_lo_que_no_es_una_carpeta_publicada_en_la_materia(ctx, materia_dir, tmp_path, caso):
    clases = materia_dir / "Clases-Classmer"
    clases.mkdir()
    (clases / "del usuario.txt").write_text("mío", encoding="utf-8")
    if caso == "afuera":
        rara = tmp_path / "afuera"
        rara.mkdir()
        (rara / "algo.txt").write_text("no es de Classmer", encoding="utf-8")
    elif caso == "raiz":
        rara = clases
    else:
        rara = clases / "un archivo.pdf"
        rara.write_bytes(b"%PDF")
    armar(ctx)
    with ctx.base.conexion() as conn:
        actualizar_sesion(conn, SID, ctx.cfg.zona_horaria, ruta_publicada=str(rara))
    antes = contenido(rara) if caso == "afuera" else rara.read_bytes() if caso == "archivo" else None

    ejecutar(ctx, job())
    if antes is not None:
        assert (contenido(rara) if caso == "afuera" else rara.read_bytes()) == antes
    assert (clases / "del usuario.txt").read_text(encoding="utf-8") == "mío"
    assert (clases / CARPETA / "apunte.pdf").is_file() and archivadas(ctx) == []
    assert fila(ctx)["ruta_publicada"] == str(clases / CARPETA)
    anterior = pubs(ctx)[1]
    assert anterior["ruta"] == str(rara) and anterior["archivo"] is None
    assert anterior["estado"] == ("no_esta" if caso == "archivo" else "sin_archivar")


def test_cambio_de_materia_archiva_en_la_materia_de_la_carpeta(ctx, materia_dir, tmp_path):
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    (tmp_path / "otra-materia").mkdir()
    with ctx.base.conexion() as conn:
        actualizar_sesion(conn, SID, ctx.cfg.zona_horaria, materia_id="otra")
    armar(ctx, version=2)
    ejecutar(ctx, job())
    otra = tmp_path / "otra-materia" / "Clases-Classmer"
    assert nombres(otra) == [CARPETA, INDICE]
    assert nombres(clases) == [INDICE] and filas_indice(clases) == []  # el índice de prueba ya no la lista
    assert [n for n, _ in archivadas(ctx, "prueba")] == [CARPETA] and archivadas(ctx, "otra") == []


def test_nombre_ocupado_en_el_archivo_usa_sufijo(ctx, materia_dir, monkeypatch):
    fijo = datetime(2026, 10, 16, 9, 5, 7, tzinfo=zona(ctx.cfg.zona_horaria))
    monkeypatch.setattr(publicar, "ahora_local", lambda _cfg: fijo)
    ocupada = carpeta_archivo(ctx.cfg) / "prueba" / f"2026-10-16 09.05.07 {CARPETA}"
    ocupada.mkdir(parents=True)
    (ocupada / "nota.txt").write_text("ya estaba", encoding="utf-8")
    armar(ctx)
    ejecutar(ctx, job())
    armar(ctx, version=2)
    ejecutar(ctx, job())
    assert (ocupada / "nota.txt").read_text(encoding="utf-8") == "ya estaba"
    assert sorted(p.name for p in ocupada.parent.iterdir()) == [ocupada.name, f"{ocupada.name} (2)"]
    assert pubs(ctx)[1]["archivo"] == str(ocupada.parent / f"{ocupada.name} (2)")


def test_indice_y_ruta_publicada_siguen_a_la_vigente(ctx, materia_dir, monkeypatch):
    indexadas = []
    original = publicar.indexar_publicada
    monkeypatch.setattr(publicar, "indexar_publicada",
                        lambda c, sid: (indexadas.append(sid), original(c, sid))[1])
    clases = materia_dir / "Clases-Classmer"
    for version, titulo in ((1, "Primera"), (2, "Segunda"), (3, "Tercera")):
        armar(ctx, version=version, titulo=titulo)
        ejecutar(ctx, job())
        nombre = f"2026-10-15 Clase 07 - {titulo}"
        assert nombres(clases) == [nombre, INDICE]
        assert fila(ctx)["ruta_publicada"] == str(clases / nombre)
        [fila_md] = filas_indice(clases)
        assert f"| {titulo} | [{nombre}](<{nombre}/>) |" in fila_md
    assert indexadas == [SID] * 3  # la búsqueda se rehace con cada versión nueva
    assert len(archivadas(ctx)) == 2


def test_mover_carpeta_a_otro_disco(tmp_path, monkeypatch):
    origen = tmp_path / "materia" / "Clase 01"
    (origen / "fotos").mkdir(parents=True)
    (origen / "apunte.pdf").write_bytes(b"%PDF")
    (origen / "fotos" / "pizarra_01.jpg").write_bytes(b"foto")
    antes = contenido(origen)
    destino = tmp_path / "datos" / "apuntes-anteriores" / "prueba" / "x Clase 01"
    original = publicar._renombrar

    def renombrar(o, d):
        if Path(o).parent != Path(d).parent:
            raise OSError(errno.EXDEV, "no es el mismo disco")
        return original(o, d)

    monkeypatch.setattr(publicar, "_renombrar", renombrar)
    assert publicar.mover_carpeta(origen, destino) is None
    assert not origen.exists() and contenido(destino) == antes
    assert nombres(origen.parent) == []  # no quedó la copia apartada

    # Si la copia falla, la original vuelve a su lugar y no queda nada a medias.
    def copytree_roto(*_a, **_k):
        raise OSError(errno.ENOSPC, "disco lleno")

    monkeypatch.setattr(shutil, "copytree", copytree_roto)
    with pytest.raises(OSError, match="disco lleno"):
        publicar.mover_carpeta(destino, origen)
    assert contenido(destino) == antes and not origen.exists()
    assert nombres(destino.parent) == [destino.name]
    with pytest.raises(FileExistsError):
        publicar.mover_carpeta(destino, destino)


def test_titulo_con_latex_limpio_en_carpeta_base_e_indice(ctx, materia_dir):
    crudo = r"Opciones: \textbf{árboles} binomiales y el camino a Black--Scholes con $\sigma$ constante"
    limpio = "Opciones: árboles binomiales y el camino a Black-Scholes con σ constante"
    armar(ctx, titulo=crudo)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    [carpeta] = [p for p in clases.iterdir() if p.is_dir()]
    titulo_carpeta = carpeta.name.removeprefix("2026-10-15 Clase 07 - ")
    assert len(titulo_carpeta) <= publicar.LARGO_TITULO
    # Cortado en un límite de palabra (no "Black-Schole").
    assert limpio.replace(":", "").startswith(titulo_carpeta + " ")
    assert titulo_carpeta == "Opciones árboles binomiales y el camino a Black-Scholes con"
    assert fila(ctx)["titulo"] == limpio
    [fila_md] = filas_indice(clases)
    assert f"| {limpio} |" in fila_md

    # El índice limpia también un título con LaTeX que venga de la base.
    with ctx.base.conexion() as conn:
        actualizar_sesion(conn, SID, ctx.cfg.zona_horaria, titulo=r"\emph{Gradiente}---direcciones")
    publicar.regenerar_indice(ctx, ctx.cfg.materia("prueba"))
    [fila_md] = filas_indice(clases)
    assert "| Gradiente—direcciones |" in fila_md


def test_titulo_que_queda_vacio_es_sin_titulo(ctx, materia_dir):
    armar(ctx, titulo="$\\,$ {}")
    ejecutar(ctx, job())
    assert (materia_dir / "Clases-Classmer" / "2026-10-15 Clase 07 - Sin título").is_dir()
    assert fila(ctx)["titulo"] == "Sin título"


@pytest.mark.parametrize("compilo", [True, False])
def test_carpeta_que_ahora_es_de_otra_sesion_no_se_mueve_ni_se_borra(ctx, materia_dir, compilo):
    # La carpeta de A se borró a mano y otra clase (B) publicó después con el mismo nombre: ya no es de A.
    clases = materia_dir / "Clases-Classmer"
    armar(ctx, "pub-a")
    ejecutar(ctx, job("pub-a"))
    shutil.rmtree(clases / CARPETA)
    armar(ctx, "pub-b")
    ejecutar(ctx, job("pub-b"))
    de_b = contenido(clases / CARPETA)

    armar(ctx, "pub-a", version=2, compilo=compilo)
    ejecutar(ctx, job("pub-a"))
    nueva = f"{CARPETA} (v2)" if compilo else f"{CARPETA} (no compiló)"
    assert nombres(clases) == sorted([CARPETA, nueva, INDICE])
    assert contenido(clases / CARPETA) == de_b and archivadas(ctx) == []
    assert fila(ctx, "pub-a")["ruta_publicada"] == str(clases / nueva)  # no se "conservó" la de B
    assert fila(ctx, "pub-b")["ruta_publicada"] == str(clases / CARPETA)
    assert [(p["estado"], p["ruta"]) for p in pubs(ctx, "pub-a")] == [
        ("vigente", str(clases / nueva)), ("no_esta", str(clases / CARPETA))]
    assert "otra clase" in leer_log(ctx, "pub-a")

    # Borrar A (es de prueba) tampoco toca la carpeta de B.
    avisos = borrar_sesion(ctx, "pub-a")
    assert nombres(clases) == [CARPETA, INDICE] and contenido(clases / CARPETA) == de_b
    assert avisos == []


def test_reclamo_viejo_de_otra_sesion_no_frena_el_archivo(ctx, materia_dir):
    # Al revés: B publicó primero, su carpeta se borró a mano y después A publicó con ese nombre. B
    # sigue "reclamando" la ruta (dato viejo), pero la carpeta es de A: A la archiva y B no la toca.
    clases = materia_dir / "Clases-Classmer"
    armar(ctx, "pub-b")
    ejecutar(ctx, job("pub-b"))
    shutil.rmtree(clases / CARPETA)
    armar(ctx, "pub-a")
    ejecutar(ctx, job("pub-a"))
    de_a = contenido(clases / CARPETA)

    armar(ctx, "pub-a", version=2)
    ejecutar(ctx, job("pub-a"))
    assert nombres(clases) == [CARPETA, INDICE]  # sin "(v2)"
    [(_, archivada)] = archivadas(ctx)
    assert contenido(archivada) == de_a

    [aviso] = borrar_sesion(ctx, "pub-b")  # B no borra la de A
    assert "otra clase" in aviso
    assert (clases / CARPETA / "apunte.tex").read_text(encoding="utf-8").endswith("% v2\n")
    borrar_sesion(ctx, "pub-a")
    assert nombres(clases) == [INDICE] and archivadas(ctx) == []


def test_no_compilo_y_se_conserva_la_publicada_el_titulo_sigue_siendo_el_de_la_publicada(ctx, materia_dir):
    armar(ctx, titulo="Viejo")
    ejecutar(ctx, job())
    armar(ctx, version=2, titulo="Nuevo", compilo=False)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    vieja = "2026-10-15 Clase 07 - Viejo"
    assert nombres(clases) == [vieja, INDICE]
    assert fila(ctx)["titulo"] == "Viejo" and fila(ctx)["estado"] == "error"
    [fila_md] = filas_indice(clases)
    assert f"| Viejo | [{vieja}](<{vieja}/>) |" in fila_md


def test_dos_filas_de_la_misma_carpeta_manda_la_vigente(ctx, materia_dir):
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    with ctx.base.conexion() as conn:  # una fila vieja sin_archivar y la vigente, misma ruta
        conn.execute("UPDATE publicaciones SET estado = 'sin_archivar'")
        conn.execute("INSERT INTO publicaciones (sesion_id, ruta, version, compilo, estado, creado, actualizado) "
                     "VALUES (?, ?, 1, 1, 'vigente', 'x', 'x')", (SID, str(clases / CARPETA)))
    armar(ctx, version=2, compilo=False)
    ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, INDICE] and archivadas(ctx) == []  # se conservó la compilada


def test_falla_y_la_copia_a_medias_queda_en_uso_un_rato(ctx, materia_dir, monkeypatch):
    # El antivirus tiene abierto un archivo recién copiado: se reintenta y después se restaura todo.
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    vieja = clases / CARPETA
    antes, pubs_antes = contenido(vieja), pubs(ctx)
    esperas: list[float] = []
    monkeypatch.setattr(publicar, "dormir", esperas.append)
    original_copiar, original_borrar = publicar.copiar_exclusivo, publicar.borrar_carpeta

    def copiar(origen: Path, destino: Path) -> None:
        if destino.name == "apunte.tex":
            raise OSError(errno.ENOSPC, "disco lleno")
        original_copiar(origen, destino)

    def borrar(carpeta: Path) -> None:
        if not esperas:
            raise PermissionError(errno.EACCES, "en uso", str(carpeta))
        original_borrar(carpeta)

    monkeypatch.setattr(publicar, "copiar_exclusivo", copiar)
    monkeypatch.setattr(publicar, "borrar_carpeta", borrar)
    armar(ctx, version=2)
    with pytest.raises(OSError, match="disco lleno"):
        ejecutar(ctx, job())
    assert esperas == [publicar.ESPERAS_ARCHIVAR[0]]
    assert nombres(clases) == [CARPETA, INDICE] and contenido(vieja) == antes
    assert archivadas(ctx) == [] and pubs(ctx) == pubs_antes


def test_falla_y_no_se_puede_restaurar_queda_registrado(ctx, materia_dir, monkeypatch):
    # Doble falla: la copia a medias no se puede borrar (ocupa el nombre) y la vieja no puede volver.
    # No se pierde nada y la base lo dice: la vieja quedó archivada y la copia a medias sin_archivar.
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    vieja = clases / CARPETA
    (vieja / "mis-notas.txt").write_text("lo agregó el usuario", encoding="utf-8")
    antes = contenido(vieja)
    monkeypatch.setattr(publicar, "dormir", lambda _s: None)
    original_copiar = publicar.copiar_exclusivo

    def copiar(origen: Path, destino: Path) -> None:
        if destino.name == "apunte.tex":
            raise OSError(errno.ENOSPC, "disco lleno")
        original_copiar(origen, destino)

    def borrar(carpeta: Path) -> None:
        raise PermissionError(errno.EACCES, "en uso", str(carpeta))

    monkeypatch.setattr(publicar, "copiar_exclusivo", copiar)
    monkeypatch.setattr(publicar, "borrar_carpeta", borrar)
    armar(ctx, version=2)
    with pytest.raises(OSError, match="disco lleno"):
        ejecutar(ctx, job())
    [(nombre, archivada)] = archivadas(ctx)
    assert nombre == CARPETA and contenido(archivada) == antes
    assert set(contenido(vieja)) == {"apunte.pdf"}  # la copia a medias
    p = pubs(ctx)
    assert [(x["estado"], x["ruta"], x["archivo"]) for x in p] == [
        ("sin_archivar", str(vieja), None), ("archivada", str(vieja), str(archivada))]
    assert "copia a medias" in p[0]["aviso"]
    log = leer_log(ctx)
    assert "no pude borrar la copia a medias" in log and "no pude restaurar" in log

    # La próxima publicación archiva la copia a medias (es nuestra) y publica normal.
    monkeypatch.setattr(publicar, "copiar_exclusivo", original_copiar)
    monkeypatch.setattr(publicar, "borrar_carpeta", publicar.shutil.rmtree)
    armar(ctx, version=3)
    ejecutar(ctx, job())
    assert nombres(clases) == [CARPETA, INDICE]
    assert (vieja / "apunte.tex").read_text(encoding="utf-8").endswith("% v3\n")
    assert len(archivadas(ctx)) == 2
    assert [x["estado"] for x in pubs(ctx)] == ["vigente", "archivada", "archivada"]


# --- borrar una sesión de prueba ---------------------------------------------------------------

def test_borrar_sesion_borra_lo_publicado_lo_sin_archivar_y_lo_archivado(ctx, materia_dir, tmp_path,
                                                                        monkeypatch):
    clases = materia_dir / "Clases-Classmer"
    armar(ctx)
    ejecutar(ctx, job())
    armar(ctx, version=2)
    ejecutar(ctx, job())  # v1 → apuntes-anteriores
    original = publicar._renombrar

    def renombrar(origen, destino):
        if Path(origen) == clases / CARPETA:
            raise PermissionError(errno.EACCES, "en uso", str(origen))
        return original(origen, destino)

    monkeypatch.setattr(publicar, "dormir", lambda _s: None)
    monkeypatch.setattr(publicar, "_renombrar", renombrar)
    armar(ctx, version=3)
    ejecutar(ctx, job())  # v2 queda sin_archivar; v3 en "(v2)"
    monkeypatch.setattr(publicar, "_renombrar", original)
    assert nombres(clases) == [CARPETA, f"{CARPETA} (v2)", INDICE]
    assert len(archivadas(ctx)) == 1

    # Lo que no es de esta sesión no se toca.
    (clases / "Carpeta del usuario").mkdir()
    ajena_archivada = carpeta_archivo(ctx.cfg) / "prueba" / "2026-01-01 00.00.00 de otra sesión"
    ajena_archivada.mkdir()
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    with ctx.base.conexion() as conn:  # una fila archivada que apunta fuera de apuntes-anteriores
        conn.execute("INSERT INTO publicaciones (sesion_id, ruta, estado, archivo, creado, actualizado) "
                     "VALUES (?, ?, 'archivada', ?, 'x', 'x')", (SID, str(clases / "z"), str(afuera)))

    avisos = borrar_sesion(ctx, SID)
    assert nombres(clases) == ["Carpeta del usuario", INDICE]
    assert [p.name for p in (carpeta_archivo(ctx.cfg) / "prueba").iterdir()] == [ajena_archivada.name]
    assert afuera.is_dir() and any("fuera de" in a for a in avisos) and len(avisos) == 1
    assert pubs(ctx) == [] and filas_indice(clases) == []


# --- notificar ---------------------------------------------------------------------------------

def test_notificar_sin_bot(ctx):
    carpeta = armar(ctx)
    r = etapa_notificar.ejecutar(ctx, job(tipo="notificar"))
    assert r.siguientes == []
    assert "notificar: sin bot de avisos configurado" in (carpeta / "pipeline.log").read_text(encoding="utf-8")


def test_notificar_llama_al_notificador_con_el_pdf(ctx):
    carpeta = armar(ctx)
    llamadas = []
    ctx.servicios.notificador = lambda cfg, sid, pdf: llamadas.append((cfg, sid, pdf))
    r = etapa_notificar.ejecutar(ctx, job(tipo="notificar"))
    assert r.siguientes == []
    assert llamadas == [(ctx.cfg, SID, carpeta / "apunte" / "v1" / "apunte.pdf")]


def test_notificar_nunca_falla(ctx):
    carpeta = armar(ctx)

    def roto(*_a):
        raise RuntimeError("bot caído")

    ctx.servicios.notificador = roto
    r = etapa_notificar.ejecutar(ctx, job(tipo="notificar"))
    assert r.siguientes == [] and "bot caído" in r.log
    assert "no pude avisar" in (carpeta / "pipeline.log").read_text(encoding="utf-8")


def test_se_conserva_la_publicada_y_vuelve_a_ser_la_actual(ctx, materia_dir):
    # La v2 no compiló: la v1 publicada sigue mandando en el índice, la búsqueda y el MCP.
    from classmer import busqueda
    from classmer.mcp.herramientas import resolver_clase

    armar(ctx, titulo="Viejo", resumen="Resumen de la v1 que compiló.")
    ejecutar(ctx, job())
    armar(ctx, version=2, titulo="Nuevo", compilo=False, resumen="RESUMEN NUEVO roto")
    ejecutar(ctx, job())
    with ctx.base.conexion() as conn:
        actuales = [f["version"] for f in conn.execute(
            "SELECT version FROM apuntes WHERE sesion_id = ? AND actual = 1", (SID,))]
        assert actuales == [1]
        assert busqueda.apunte_actual(conn, SID)["version"] == 1
        assert resolver_clase(conn, SID)["apunte_version"] == 1
    [fila_md] = filas_indice(materia_dir / "Clases-Classmer")
    assert "Resumen de la v1" in fila_md and "RESUMEN NUEVO" not in fila_md
    s = fila(ctx)
    assert s["estado"] == "error" and s["error"] == ERROR_NO_COMPILO.format(id=SID)
    log = (ctx.sesion_dir(SID) / "pipeline.log").read_text(encoding="utf-8")
    assert "la vigente sigue siendo la v1" in log

    # La próxima que compila vuelve a ser la actual y reemplaza a la publicada.
    armar(ctx, version=3, titulo="Arreglado")
    ejecutar(ctx, job())
    with ctx.base.conexion() as conn:
        assert [f["version"] for f in conn.execute(
            "SELECT version FROM apuntes WHERE sesion_id = ? AND actual = 1", (SID,))] == [3]
    assert fila(ctx)["estado"] == "lista"


def test_cambio_de_materia_y_la_nueva_no_compilo_no_se_queda_en_la_materia_vieja(ctx, materia_dir, tmp_path):
    armar(ctx)
    ejecutar(ctx, job())
    clases = materia_dir / "Clases-Classmer"
    (tmp_path / "otra-materia").mkdir()
    with ctx.base.conexion() as conn:
        actualizar_sesion(conn, SID, ctx.cfg.zona_horaria, materia_id="otra")
    armar(ctx, version=2, compilo=False)
    ejecutar(ctx, job())
    otra = tmp_path / "otra-materia" / "Clases-Classmer"
    assert nombres(otra) == [f"{CARPETA} (no compiló)", INDICE]
    assert nombres(clases) == [INDICE] and filas_indice(clases) == []
    assert [n for n, _ in archivadas(ctx, "prueba")] == [CARPETA]
    assert fila(ctx)["ruta_publicada"] == str(otra / f"{CARPETA} (no compiló)")
