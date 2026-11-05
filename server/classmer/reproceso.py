"""Re-ejecutar el pipeline de una sesión desde una etapa (CLI y POST /reprocesar)."""

from __future__ import annotations

from collections.abc import Collection

from classmer import cola
from classmer.db import transaccion
from classmer.pipeline import ETAPAS, Contexto, actualizar_sesion

# "Regenerar el apunte con los adjuntos" (app, POST /regenerar): sólo una clase que ya terminó.
# Reprocesar una en cola o procesando cortaría su pipeline a la mitad.
ESTADOS_REGENERAR = ("lista", "error")


def reprocesar(ctx: Contexto, sesion_id: str, desde: str, *, estados: Collection[str] | None = None) -> None:
    """Cancela lo que haya en cola de la sesión y encola `desde`. Con `estados`, sólo si la sesión
    está en uno de ellos (se chequea en la misma transacción). Lanza LookupError si no se puede."""
    if desde not in ETAPAS:
        raise LookupError(f"etapa inválida: {desde}")
    with ctx.base.conexion() as conn, transaccion(conn):
        s = conn.execute("SELECT estado FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
        if s is None:
            raise LookupError(f"no existe la sesión {sesion_id}")
        if s["estado"] == "grabando":
            raise LookupError("la sesión todavía se está grabando (falta finalizar)")
        if estados is not None and s["estado"] not in estados:
            if s["estado"] in ("en_cola", "procesando"):
                raise LookupError("la clase ya se está procesando: esperá a que termine")
            raise LookupError(f"la clase está en {s['estado']}: sólo se regenera una clase lista o en error")
        corriendo = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE sesion_id = ? AND estado = 'corriendo'", (sesion_id,)
        ).fetchone()[0]
        if corriendo:
            raise LookupError("la sesión tiene un job corriendo; esperá a que termine")
        if desde == "apunte" and not (ctx.sesion_dir(sesion_id) / "timeline.md").is_file():
            # Antes de cancelar nada: el pipeline que esté en cola sigue como estaba.
            raise LookupError("falta timeline.md: reprocesá desde linea_de_tiempo")
        cola.cancelar_de_sesion(conn, sesion_id)
        if desde == "transcribir":
            bloques = [f["n"] for f in conn.execute(
                "SELECT n FROM bloques WHERE sesion_id = ? ORDER BY n", (sesion_id,))]
            if not bloques:
                raise LookupError("no hay bloques: reprocesá desde segmentar")
            conn.execute("UPDATE bloques SET estado = 'pendiente' WHERE sesion_id = ?", (sesion_id,))
            for n in bloques:
                cola.encolar(conn, "transcribir", sesion_id, {"n": n})
        else:
            cola.encolar(conn, desde, sesion_id)
        actualizar_sesion(conn, sesion_id, ctx.cfg.zona_horaria, estado="en_cola", etapa=desde, error=None)


def reprocesar_celu(ctx: Contexto, sesion_id: str) -> None:
    """Después de "Agregar audio del celu": segmenta y transcribe sólo esa pista (el desfase sale por
    correlación) y sigue el pipeline (línea de tiempo, revisión, apunte y publicación). Sólo una clase
    terminada y sin jobs corriendo. Lanza LookupError si no se puede."""
    from classmer.sesiones import datos_celu

    with ctx.base.conexion() as conn, transaccion(conn):
        s = conn.execute("SELECT estado FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
        if s is None:
            raise LookupError(f"no existe la sesión {sesion_id}")
        if s["estado"] not in ESTADOS_REGENERAR:
            raise LookupError("la clase todavía se está procesando: esperá a que termine")
        if conn.execute("SELECT COUNT(*) FROM jobs WHERE sesion_id = ? AND estado = 'corriendo'",
                        (sesion_id,)).fetchone()[0]:
            raise LookupError("la sesión tiene un job corriendo; esperá a que termine")
        if datos_celu(conn, sesion_id) is None:
            raise LookupError("la clase no tiene audio del celu")
        cola.cancelar_de_sesion(conn, sesion_id)
        cola.encolar(conn, "segmentar", sesion_id, {"pista": "celu"})
        actualizar_sesion(conn, sesion_id, ctx.cfg.zona_horaria, estado="en_cola", etapa="segmentar", error=None)
