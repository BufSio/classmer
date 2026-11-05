"""Materias vistas desde la base: número de clase (§5.1) y la forma de GET /api/materias.

- `proximo_numero` = `numero_clase_inicial` + clases de la materia que no terminaron en error.
- Cada sesión fija su número al crearse (el que manda la app o, si no viene, `proximo_numero`).
  En Auto queda en null hasta que se asigna la materia.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from classmer.config import Config, Materia

# Una sesión en error sin ningún apunte que compiló (prueba fallida, clase que no salió) no es una
# clase. Las que siguen grabando o en cola sí cuentan.
ES_CLASE = "(s.estado != 'error' OR EXISTS (SELECT 1 FROM apuntes c WHERE c.sesion_id = s.id AND c.compilo = 1))"


def clases_contadas(conn: sqlite3.Connection, materia_id: str) -> int:
    """Clases de la materia que no terminaron en error."""
    return int(conn.execute(
        f"SELECT COUNT(*) FROM sesiones s WHERE s.materia_id = ? AND {ES_CLASE}", (materia_id,)
    ).fetchone()[0])


def proximo_numero(conn: sqlite3.Connection, materia: Materia) -> int:
    return materia.numero_clase_inicial + clases_contadas(conn, materia.id)


def inicial_para(conn: sqlite3.Connection, materia: Materia, proximo: int) -> int:
    """El `numero_clase_inicial` que hace que `proximo_numero` valga `proximo`."""
    return proximo - clases_contadas(conn, materia.id)


def materia_api(conn: sqlite3.Connection, materia: Materia) -> dict[str, Any]:
    return materia.para_api(proximo_numero(conn, materia))


def materias_api(cfg: Config, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """GET /api/materias: sólo las activas."""
    return [materia_api(conn, m) for m in cfg.materias_activas]
