"""Borrar una sesión de la materia de prueba (panel). Las clases reales no se borran desde Classmer.

Borra sus filas de la base (cualquier tabla con `sesion_id`, las secciones y temas de sus apuntes y
sus fragmentos del índice de búsqueda), su carpeta en Classmer-data (sesiones/ y jobs/), las copias
publicadas (la vigente y las que no se pudieron archivar) si están dentro de la carpeta de clases de
la materia de prueba, y sus versiones archivadas en `apuntes-anteriores`. Después rehace el
INDICE-CLASES.md de esa materia.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from classmer import busqueda
from classmer.pipeline import Contexto
from classmer.sesiones import Conflicto, fila_sesion

MATERIA_PRUEBA = "prueba"


def se_puede_borrar(materia_id: str | None) -> bool:
    return materia_id == MATERIA_PRUEBA


def _tablas_con_sesion(conn) -> list[str]:
    tablas = []
    for (nombre,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'sesiones' "
            "AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%'"):
        columnas = {c[1] for c in conn.execute(f'PRAGMA table_info("{nombre}")')}
        if "sesion_id" in columnas:
            tablas.append(nombre)
    return tablas


def borrar_sesion(ctx: Contexto, sesion_id: str) -> list[str]:
    """Borra la sesión. Devuelve avisos (cosas que no se pudieron borrar del disco)."""
    from classmer.pipeline.publicar import (
        carpeta_archivo,
        dentro_de,
        es_de_otra_sesion,
        regenerar_indice,
        rutas_de_otras_sesiones,
    )

    cfg = ctx.cfg
    with ctx.base.transaccion() as conn:
        s = fila_sesion(conn, sesion_id)
        if not se_puede_borrar(s["materia_id"]):
            raise Conflicto(f"sólo se borran sesiones de la materia {MATERIA_PRUEBA!r}")
        corriendo = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE sesion_id = ? AND estado = 'corriendo'", (sesion_id,)
        ).fetchone()[0]
        if corriendo:
            raise Conflicto("la sesión tiene un job corriendo; esperá a que termine")
        # Antes de borrar las filas: qué carpetas publicó (en la materia) y qué archivó.
        publicadas: dict[str, tuple[Path, list[int]]] = {}  # carpeta → sus filas de publicaciones
        if s["ruta_publicada"]:
            publicadas[_clave(s["ruta_publicada"])] = (Path(s["ruta_publicada"]), [])
        archivadas: list[str] = []
        for p in conn.execute("SELECT id, ruta, estado, archivo FROM publicaciones WHERE sesion_id = ? "
                              "ORDER BY id", (sesion_id,)):
            if p["estado"] in ("vigente", "sin_archivar"):
                publicadas.setdefault(_clave(p["ruta"]), (Path(p["ruta"]), []))[1].append(p["id"])
            elif p["estado"] == "archivada" and p["archivo"]:
                archivadas.append(p["archivo"])
        # Si otra clase publicó después en la misma ruta (la nuestra se borró a mano), ya no es nuestra.
        otras = rutas_de_otras_sesiones(conn, sesion_id)
        de_otras = {clave for clave, (_, filas) in publicadas.items() if es_de_otra_sesion(otras, clave, filas)}
        conn.execute("PRAGMA defer_foreign_keys = ON")
        apuntes = "SELECT id FROM apuntes WHERE sesion_id = ?"
        conn.execute(f"DELETE FROM seccion_temas WHERE seccion_id IN "
                     f"(SELECT id FROM secciones WHERE apunte_id IN ({apuntes}))", (sesion_id,))
        conn.execute(f"DELETE FROM secciones WHERE apunte_id IN ({apuntes})", (sesion_id,))
        for tabla in _tablas_con_sesion(conn):
            conn.execute(f'DELETE FROM "{tabla}" WHERE sesion_id = ?', (sesion_id,))
        conn.execute("DELETE FROM sesiones WHERE id = ?", (sesion_id,))
        busqueda.borrar_sesion(conn, sesion_id)  # el índice FTS del MCP

    avisos: list[str] = []
    for carpeta in (cfg.sesion_dir(sesion_id), cfg.jobs_dir / sesion_id):
        _borrar_arbol(carpeta, avisos)
    archivo = carpeta_archivo(cfg)
    for ruta in _sin_repetir(archivadas):
        if dentro_de(ruta, archivo):
            _borrar_arbol(ruta, avisos)
        else:
            avisos.append(f"no borré la versión archivada: {ruta} está fuera de {archivo}")
    materia = cfg.materia(MATERIA_PRUEBA)
    raiz = materia.ruta_clases() if materia is not None else None
    if publicadas and materia is not None and raiz is not None:
        for clave, (ruta, _) in publicadas.items():
            if clave in de_otras:
                if ruta.exists():
                    avisos.append(f"no borré {ruta}: ahora es la carpeta publicada de otra clase")
            elif dentro_de(ruta, raiz):
                _borrar_arbol(ruta, avisos)
            elif ruta.exists():
                avisos.append(f"no borré la copia publicada: {ruta} está fuera de la carpeta de la materia")

        try:
            regenerar_indice(ctx, materia)
        except Exception as e:  # noqa: BLE001 — la sesión ya se borró
            avisos.append(f"no pude rehacer el índice: {e}")
    return avisos


def _clave(ruta: str) -> str:
    return os.path.normcase(os.path.normpath(ruta))


def _sin_repetir(rutas: list[str]) -> list[Path]:
    vistas: dict[str, Path] = {}
    for r in rutas:
        vistas.setdefault(_clave(r), Path(r))
    return list(vistas.values())


def _borrar_arbol(carpeta: Path, avisos: list[str]) -> None:
    if not carpeta.exists():
        return

    def _solo_lectura(funcion, ruta, _exc):  # archivos de sólo lectura (p. ej., copiados de la plantilla)
        os.chmod(ruta, 0o700)
        funcion(ruta)

    try:
        shutil.rmtree(carpeta, onexc=_solo_lectura)
    except OSError as e:
        avisos.append(f"no pude borrar {carpeta}: {e}")
