"""Etapa 8 — notificar (§6, §12): avisa y manda el PDF por un bot de mensajería.

En v0.1 es un stub: si hay un `ctx.servicios.notificador` lo llama con (cfg, sesion_id,
ruta_pdf); si no, sólo lo anota. Nunca hace fallar la sesión: cualquier error queda en el log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from classmer.pipeline import Contexto, Resultado
from classmer.sesiones import ruta_pdf_actual

if TYPE_CHECKING:
    from classmer.cola import Job


def ejecutar(ctx: Contexto, job: Job) -> Resultado:
    sid = job.sesion_id or ""
    try:
        notificador = ctx.servicios.notificador
        if notificador is None:
            ctx.log_sesion(sid, "notificar: sin bot de avisos configurado")
            return Resultado(log="sin notificador (no hay bot de avisos)")
        with ctx.base.conexion() as conn:
            ruta_pdf = ruta_pdf_actual(ctx.cfg, conn, sid)
        notificador(ctx.cfg, sid, ruta_pdf)
        ctx.log_sesion(sid, "notificar: aviso enviado" + ("" if ruta_pdf else " (sin PDF)"))
        return Resultado(log="aviso enviado", motor="bot")
    except Exception as e:  # noqa: BLE001 — notificar nunca falla la sesión
        mensaje = f"notificar: no pude avisar ({e.__class__.__name__}: {e})"
        try:
            ctx.log_sesion(sid, mensaje)
        except Exception:  # noqa: BLE001
            pass
        return Resultado(log=mensaje)
