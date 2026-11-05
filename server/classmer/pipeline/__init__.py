"""Pipeline (§6): etapas re-ejecutables, cada una es un job que encola la siguiente.

Contrato de una etapa: un módulo `classmer.pipeline.<etapa>` con

    def ejecutar(ctx: Contexto, job: Job) -> Resultado

- Escribe sus salidas en la carpeta de la sesión (ctx.sesion_dir(id)) y se puede re-ejecutar
  sin efectos raros (pisa sus propias salidas, nunca las de otras etapas).
- Devuelve `Resultado(siguientes=[(tipo, args), ...])` con los jobs a encolar al terminar.
- Para fallar, lanza `ErrorEtapa` (reintentable o no), `CuotaAgotada` (vuelve a la cola sin
  contar el intento) o `EsperandoUsuario` (la sesión queda en esperando_usuario).
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from classmer.util import ahora_iso

if TYPE_CHECKING:
    from classmer.cola import Job
    from classmer.config import Config
    from classmer.db import Base

log = logging.getLogger("classmer.pipeline")

ETAPAS = [
    "verificar",
    "segmentar",
    "transcribir",
    "linea_de_tiempo",
    "clasificar",
    "revisar_transcript",
    "apunte",
    "publicar",
    "notificar",
]

# Peso de cada etapa en el progreso (0..1) que ve la app.
PESOS = {
    "verificar": 0.02,
    "segmentar": 0.04,
    "transcribir": 0.40,
    "linea_de_tiempo": 0.02,
    "clasificar": 0.02,
    "revisar_transcript": 0.06,
    "apunte": 0.40,
    "publicar": 0.04,
    "notificar": 0.0,
}

# Etapas que no cambian el estado visible de la sesión (corren con la sesión ya 'lista').
ETAPAS_SILENCIOSAS = {"notificar"}


class ErrorEtapa(Exception):
    """Falla de una etapa. Si es reintentable, el job vuelve a la cola con espera creciente."""

    def __init__(self, mensaje: str, *, reintentable: bool = True):
        super().__init__(mensaje)
        self.reintentable = reintentable


class CuotaAgotada(ErrorEtapa):
    """Cuota, rate limit o recurso externo no disponible: no cuenta como intento fallido."""

    def __init__(self, mensaje: str, *, espera_s: float = 300):
        super().__init__(mensaje, reintentable=True)
        self.espera_s = espera_s


ESPERAS = ("materia", "revision")


class EsperandoUsuario(Exception):
    """La sesión necesita una decisión del usuario: la materia (confianza baja) o las dudas de la revisión
    del transcript (`espera`, §15). Si la etapa ya gastó algo (una clasificación o una revisión con
    Claude), `motor`, `costo` y `facturacion` quedan en el job."""

    def __init__(self, mensaje: str, *, motor: str | None = None, costo: float | None = None,
                 facturacion: str | None = None, espera: str = "materia", log: str | None = None):
        super().__init__(mensaje)
        if espera not in ESPERAS:
            raise ValueError(f"espera desconocida: {espera!r}")
        self.motor = motor
        self.costo = costo
        self.facturacion = facturacion
        self.espera = espera
        self.log = log


@dataclass
class Resultado:
    siguientes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    log: str | None = None
    motor: str | None = None
    costo: float | None = None
    facturacion: str | None = None   # api | max: con qué se pagó lo que se le pidió a Claude


@dataclass
class Servicios:
    """Dependencias externas inyectables (los tests usan versiones falsas).

    - transcriptor(cfg) -> classmer.ia.transcriptor.Transcriptor
    - clasificador(cfg) -> objeto con .clasificar(...)
    - agente_apunte(cfg) -> objeto con .ejecutar(...)
    - revisor(cfg) -> objeto con .consultar(prompt, esquema) -> RespuestaJSON, .modelo y .esfuerzo
      (la revisión del transcript; también usa `transcriptor` para volver a escuchar las dudas)
    Si son None, cada etapa usa la implementación real.
    """

    transcriptor: Callable[..., Any] | None = None
    clasificador: Callable[..., Any] | None = None
    agente_apunte: Callable[..., Any] | None = None
    notificador: Callable[..., Any] | None = None
    revisor: Callable[..., Any] | None = None


@dataclass
class Contexto:
    cfg: Config
    base: Base
    servicios: Servicios = field(default_factory=Servicios)

    def sesion_dir(self, sesion_id: str) -> Path:
        return self.cfg.sesion_dir(sesion_id)

    def job_dir(self, sesion_id: str, nombre: str) -> Path:
        return self.cfg.jobs_dir / sesion_id / nombre

    def log_sesion(self, sesion_id: str, mensaje: str) -> None:
        """Agrega una línea a sesiones/<id>/pipeline.log (y al log general)."""
        log.info("[%s] %s", sesion_id[:8], mensaje)
        try:
            ruta = self.sesion_dir(sesion_id) / "pipeline.log"
            ruta.parent.mkdir(parents=True, exist_ok=True)
            with open(ruta, "a", encoding="utf-8") as f:
                f.write(f"{ahora_iso(self.cfg.zona_horaria)} {mensaje}\n")
        except OSError:
            log.exception("no pude escribir pipeline.log")


def etapa(tipo: str) -> Callable[[Contexto, Job], Resultado]:
    if tipo not in ETAPAS:
        raise KeyError(f"etapa desconocida: {tipo}")
    modulo = importlib.import_module(f"classmer.pipeline.{tipo}")
    return modulo.ejecutar


def siguiente_etapa(tipo: str) -> str | None:
    i = ETAPAS.index(tipo)
    return ETAPAS[i + 1] if i + 1 < len(ETAPAS) else None


# --- estado de la sesión -------------------------------------------------------------------

def actualizar_sesion(conn: sqlite3.Connection, sesion_id: str, zona: str, **campos: Any) -> None:
    """Cambia campos de la sesión. Un `estado` que no es esperando_usuario borra la `espera`."""
    if not campos:
        return
    if "estado" in campos and campos["estado"] != "esperando_usuario":
        campos.setdefault("espera", None)
    campos["actualizado"] = ahora_iso(zona)
    asignaciones = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(
        f"UPDATE sesiones SET {asignaciones} WHERE id = ?", [*campos.values(), sesion_id]
    )


def calcular_progreso(conn: sqlite3.Connection, sesion_id: str) -> float:
    """Progreso 0..1 según las etapas terminadas y los bloques ya transcriptos."""
    fila = conn.execute("SELECT estado, etapa FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
    if fila is None:
        return 0.0
    if fila["estado"] == "lista":
        return 1.0
    if fila["estado"] == "grabando":
        return 0.0
    # 'en_cola' recién finalizada no tiene nada hecho (da 0); re-encolada (asignar, reprocesar)
    # conserva lo que ya estaba.
    hechas = {
        f["tipo"]
        for f in conn.execute(
            "SELECT DISTINCT tipo FROM jobs WHERE sesion_id = ? AND estado = 'hecho'", (sesion_id,)
        )
    }
    total = 0.0
    for nombre in ETAPAS:
        if nombre == "transcribir":
            b = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN estado IN ('transcripto', 'dudoso') THEN 1 ELSE 0 END) AS ok
                FROM bloques WHERE sesion_id = ?
                """,
                (sesion_id,),
            ).fetchone()
            if b["n"]:
                total += PESOS[nombre] * (b["ok"] or 0) / b["n"]
        elif nombre in hechas:
            total += PESOS[nombre]
    return round(min(total, 0.99), 3)
