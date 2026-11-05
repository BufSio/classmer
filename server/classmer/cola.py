"""Cola de jobs en SQLite (§10): toma atómica con UPDATE … RETURNING, lease y run_after."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from classmer.util import ahora as _ahora

LEASE_S = 900

# Prioridad por etapa: menor = antes. Ordena dentro de lo que está listo para correr.
PRIORIDADES = {
    "verificar": 10,
    "segmentar": 20,
    "transcribir": 30,
    "linea_de_tiempo": 40,
    "clasificar": 45,
    "revisar_transcript": 47,
    "apunte": 50,
    "publicar": 60,
    "notificar": 70,
}

MAX_INTENTOS = {
    "transcribir": 4,
    "apunte": 2,
}


@dataclass
class Job:
    id: int
    tipo: str
    sesion_id: str | None
    args: dict[str, Any] = field(default_factory=dict)
    clave: str | None = None
    estado: str = "pendiente"
    prioridad: int = 100
    intentos: int = 0
    max_intentos: int = 3
    run_after: float = 0
    lease_hasta: float | None = None
    motor: str | None = None
    error: str | None = None
    log: str | None = None

    @classmethod
    def desde_fila(cls, fila: sqlite3.Row) -> Job:
        return cls(
            id=fila["id"],
            tipo=fila["tipo"],
            sesion_id=fila["sesion_id"],
            args=json.loads(fila["args"]) if fila["args"] else {},
            clave=fila["clave"],
            estado=fila["estado"],
            prioridad=fila["prioridad"],
            intentos=fila["intentos"],
            max_intentos=fila["max_intentos"],
            run_after=fila["run_after"],
            lease_hasta=fila["lease_hasta"],
            motor=fila["motor"],
            error=fila["error"],
            log=fila["log"],
        )


def clave_default(tipo: str, sesion_id: str | None, args: dict[str, Any] | None) -> str | None:
    """Un solo job activo por (tipo, sesión, bloque) y, en segmentar, por pista."""
    if sesion_id is None:
        return None
    clave = f"{tipo}:{sesion_id}"
    if args and "n" in args:
        clave += f":{args['n']}"
    if args and args.get("pista"):
        clave += f":{args['pista']}"
    return clave


def espera_reintento(intentos: int) -> float:
    """Espera creciente: 30 s, 2 min, 8 min, 32 min… con tope de 1 h."""
    return float(min(30 * 4 ** max(0, intentos - 1), 3600))


def encolar(
    conn: sqlite3.Connection,
    tipo: str,
    sesion_id: str | None = None,
    args: dict[str, Any] | None = None,
    *,
    prioridad: int | None = None,
    run_after: float = 0,
    clave: str | None = "auto",
    max_intentos: int | None = None,
    ahora: float | None = None,
) -> int | None:
    """Encola un job. Si ya hay uno activo con la misma clave, no duplica y devuelve None."""
    momento = _ahora() if ahora is None else ahora
    if clave == "auto":
        clave = clave_default(tipo, sesion_id, args)
    cur = conn.execute(
        """
        INSERT INTO jobs (tipo, sesion_id, args, clave, estado, prioridad, max_intentos,
                          run_after, creado, actualizado)
        VALUES (?, ?, ?, ?, 'pendiente', ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            tipo,
            sesion_id,
            json.dumps(args or {}, ensure_ascii=False),
            clave,
            PRIORIDADES.get(tipo, 100) if prioridad is None else prioridad,
            MAX_INTENTOS.get(tipo, 3) if max_intentos is None else max_intentos,
            run_after,
            momento,
            momento,
        ),
    )
    return cur.lastrowid if cur.rowcount else None


def tomar(
    conn: sqlite3.Connection,
    tipos: Sequence[str] | None = None,
    *,
    excluir: Sequence[str] | None = None,
    sesion_id: str | None = None,
    ahora: float | None = None,
    lease_s: int = LEASE_S,
) -> Job | None:
    """Toma atómicamente el próximo job listo (§10). `tipos`/`excluir` filtran por etapa y
    `sesion_id`, por sesión (la CLI sólo toma los de la sesión que importó)."""
    momento = _ahora() if ahora is None else ahora
    filtro, params = "", [momento]
    if tipos:
        filtro += f" AND tipo IN ({','.join('?' * len(tipos))})"
        params.extend(tipos)
    if excluir:
        filtro += f" AND tipo NOT IN ({','.join('?' * len(excluir))})"
        params.extend(excluir)
    if sesion_id is not None:
        filtro += " AND sesion_id = ?"
        params.append(sesion_id)
    fila = conn.execute(
        f"""
        UPDATE jobs
        SET estado = 'corriendo', intentos = intentos + 1, lease_hasta = ? + ?, actualizado = ?
        WHERE id = (SELECT id FROM jobs
                    WHERE estado = 'pendiente' AND run_after <= ?{filtro}
                    ORDER BY prioridad, creado, id LIMIT 1)
        RETURNING *
        """,
        [momento, lease_s, momento, *params],
    ).fetchone()
    return Job.desde_fila(fila) if fila else None


def renovar(conn: sqlite3.Connection, job_id: int, *, ahora: float | None = None,
            lease_s: int = LEASE_S) -> None:
    momento = _ahora() if ahora is None else ahora
    conn.execute(
        "UPDATE jobs SET lease_hasta = ?, actualizado = ? WHERE id = ? AND estado = 'corriendo'",
        (momento + lease_s, momento, job_id),
    )


def completar(conn: sqlite3.Connection, job_id: int, *, log: str | None = None,
              motor: str | None = None, costo: float | None = None,
              facturacion: str | None = None, ahora: float | None = None) -> None:
    momento = _ahora() if ahora is None else ahora
    conn.execute(
        """
        UPDATE jobs SET estado = 'hecho', lease_hasta = NULL, error = NULL, actualizado = ?,
                        log = COALESCE(?, log), motor = COALESCE(?, motor),
                        costo = COALESCE(?, costo), facturacion = COALESCE(?, facturacion)
        WHERE id = ?
        """,
        (momento, log, motor, costo, facturacion, job_id),
    )


def fallar(conn: sqlite3.Connection, job: Job, error: str, *, reintentable: bool = True,
           ahora: float | None = None) -> bool:
    """Registra un fallo. Devuelve True si el job vuelve a la cola (con espera creciente)."""
    momento = _ahora() if ahora is None else ahora
    fila = conn.execute("SELECT intentos, max_intentos FROM jobs WHERE id = ?", (job.id,)).fetchone()
    intentos, maximo = (fila["intentos"], fila["max_intentos"]) if fila else (job.intentos, job.max_intentos)
    if reintentable and intentos < maximo:
        conn.execute(
            """
            UPDATE jobs SET estado = 'pendiente', lease_hasta = NULL, error = ?, run_after = ?,
                            actualizado = ?
            WHERE id = ?
            """,
            (error, momento + espera_reintento(intentos), momento, job.id),
        )
        return True
    conn.execute(
        "UPDATE jobs SET estado = 'error', lease_hasta = NULL, error = ?, actualizado = ? WHERE id = ?",
        (error, momento, job.id),
    )
    return False


def posponer(conn: sqlite3.Connection, job: Job, segundos: float, motivo: str, *,
             ahora: float | None = None) -> None:
    """Cuota agotada, rate limit o falta un secreto: vuelve a la cola sin contar el intento."""
    momento = _ahora() if ahora is None else ahora
    conn.execute(
        """
        UPDATE jobs SET estado = 'pendiente', lease_hasta = NULL, error = ?, run_after = ?,
                        intentos = MAX(0, intentos - 1), actualizado = ?
        WHERE id = ?
        """,
        (motivo, momento + max(0.0, segundos), momento, job.id),
    )


def liberar(conn: sqlite3.Connection, job_id: int, *, ahora: float | None = None) -> None:
    """Apagado ordenado: el job vuelve a pendiente sin contar el intento."""
    momento = _ahora() if ahora is None else ahora
    conn.execute(
        """
        UPDATE jobs SET estado = 'pendiente', lease_hasta = NULL, run_after = ?,
                        intentos = MAX(0, intentos - 1), actualizado = ?
        WHERE id = ? AND estado = 'corriendo'
        """,
        (momento, momento, job_id),
    )


def recuperar_vencidos(conn: sqlite3.Connection, *, ahora: float | None = None) -> int:
    """Jobs 'corriendo' con lease vencido (el server se cayó): vuelven a pendiente o a error."""
    momento = _ahora() if ahora is None else ahora
    conn.execute(
        """
        UPDATE jobs SET estado = 'error', lease_hasta = NULL, actualizado = ?,
                        error = COALESCE(error, 'lease vencido') || ' (sin más intentos)'
        WHERE estado = 'corriendo' AND lease_hasta < ? AND intentos >= max_intentos
        """,
        (momento, momento),
    )
    cur = conn.execute(
        """
        UPDATE jobs SET estado = 'pendiente', lease_hasta = NULL, run_after = ?, actualizado = ?,
                        error = 'lease vencido'
        WHERE estado = 'corriendo' AND lease_hasta < ?
        """,
        (momento, momento, momento),
    )
    return cur.rowcount


def cancelar_de_sesion(conn: sqlite3.Connection, sesion_id: str, *, ahora: float | None = None) -> int:
    momento = _ahora() if ahora is None else ahora
    cur = conn.execute(
        """
        UPDATE jobs SET estado = 'cancelado', lease_hasta = NULL, actualizado = ?
        WHERE sesion_id = ? AND estado = 'pendiente'
        """,
        (momento, sesion_id),
    )
    return cur.rowcount


def jobs_de_sesion(conn: sqlite3.Connection, sesion_id: str) -> list[Job]:
    filas = conn.execute(
        "SELECT * FROM jobs WHERE sesion_id = ? ORDER BY id", (sesion_id,)
    ).fetchall()
    return [Job.desde_fila(f) for f in filas]
