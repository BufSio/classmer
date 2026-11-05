"""Un solo worker por carpeta de datos (§10): el lock `worker.lock` es del proceso.

Con dos procesos de verdad: una segunda `classmer serve` (o `importar --procesar` con el server
andando) no toma jobs, así no corre dos veces lo mismo ni suelta lo que corre el otro.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from classmer import cola, sesiones
from classmer import worker as worker_mod
from classmer.pipeline import Contexto, Resultado, actualizar_sesion
from classmer.worker import Worker, tomar_lock_worker

SERVER = Path(__file__).resolve().parents[1]
INICIO = "2026-10-15T14:00:05-03:00"

# Otro proceso que toma el lock y (con "esperar") lo mantiene hasta que lo maten.
TOMAR_LOCK = """
import sys, time
from pathlib import Path
from classmer.worker import tomar_lock_worker
print("tomado" if tomar_lock_worker(Path(sys.argv[1])) else "ocupado", flush=True)
if sys.argv[2] == "esperar":
    time.sleep(120)
"""


def _python(datos: Path, modo: str) -> list[str]:
    return [sys.executable, "-c", TOMAR_LOCK, str(datos), modo]


class OtroProceso:
    def __init__(self, datos: Path):
        self.p = subprocess.Popen(_python(datos, "esperar"), cwd=SERVER, stdout=subprocess.PIPE, text=True)
        linea = self.p.stdout.readline().strip()
        assert linea == "tomado", linea

    def soltar(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait(30)
        if self.p.stdout:
            self.p.stdout.close()


@pytest.fixture
def otro_proceso(ctx: Contexto):
    procesos: list[OtroProceso] = []

    def lanzar() -> OtroProceso:
        p = OtroProceso(ctx.cfg.datos)
        procesos.append(p)
        return p

    yield lanzar
    for p in procesos:
        p.soltar()


@pytest.fixture
def etapas(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable]:
    registro: dict[str, Callable] = {}

    def buscar(tipo: str):
        if tipo not in registro:
            raise KeyError(f"etapa falsa no registrada: {tipo}")
        return registro[tipo]

    monkeypatch.setattr(worker_mod, "etapa", buscar)
    return registro


@pytest.fixture
def workers():
    creados: list[Worker] = []
    yield creados
    for w in creados:
        w.detener(espera_s=2)


def esperar(condicion: Callable[[], bool], timeout: float = 15.0, mensaje: str = "") -> None:
    limite = time.time() + timeout
    while time.time() < limite:
        if condicion():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout esperando: {mensaje or condicion}")


def nueva_sesion(ctx: Contexto, sid: str, estado: str = "en_cola") -> str:
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id="prueba", origen_materia=None,
                       tipo_clase=None, inicio=INICIO, dispositivo=None)
        actualizar_sesion(conn, sid, ctx.cfg.zona_horaria, estado=estado)
    return sid


def encolar(ctx: Contexto, tipo: str, sid: str, args: dict | None = None) -> int:
    with ctx.base.conexion() as conn:
        jid = cola.encolar(conn, tipo, sid, args)
    assert jid is not None
    return jid


def job(ctx: Contexto, jid: int) -> cola.Job:
    with ctx.base.conexion() as conn:
        return cola.Job.desde_fila(conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone())


# --- el lock --------------------------------------------------------------------------------------

def test_lock_es_del_proceso_y_reentrante(cfg, tmp_path: Path):
    assert tomar_lock_worker(cfg.datos)
    assert tomar_lock_worker(cfg.datos)  # varios Worker en el mismo proceso (tests, CLI)
    assert (cfg.datos / worker_mod.LOCK_ARCHIVO).is_file()
    r = subprocess.run(_python(cfg.datos, "salir"), cwd=SERVER, capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "ocupado", r.stderr
    # Es por carpeta de datos: otra carpeta, otro lock.
    r = subprocess.run(_python(tmp_path / "otros-datos", "salir"), cwd=SERVER, capture_output=True,
                       text=True, timeout=60)
    assert r.stdout.strip() == "tomado", r.stderr


def test_no_toma_jobs_si_otro_proceso_es_el_worker(ctx: Contexto, etapas, workers, otro_proceso, caplog):
    caplog.set_level(logging.INFO, logger="classmer.worker")
    otro = otro_proceso()
    sid = nueva_sesion(ctx, "s-segunda")
    corrio = threading.Event()
    etapas["verificar"] = lambda c, j: (corrio.set(), Resultado())[1]
    jid = encolar(ctx, "verificar", sid)
    # Un job del otro proceso con el lease vencido: tampoco lo toca (el dueño lo mantiene).
    ajeno = encolar(ctx, "segmentar", nueva_sesion(ctx, "s-ajena", estado="procesando"))
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE jobs SET estado = 'corriendo', intentos = 1, lease_hasta = ? WHERE id = ?",
                     (time.time() - 10, ajeno))
    w = Worker(ctx)
    workers.append(w)
    w.iniciar()
    time.sleep(0.6)  # muchos polls (poll_s = 0.05)
    assert not corrio.is_set()
    j = job(ctx, jid)
    assert j.estado == "pendiente" and j.intentos == 0
    assert job(ctx, ajeno).estado == "corriendo"
    avisos = [r for r in caplog.records if "otro proceso tiene el worker" in r.getMessage()]
    assert len(avisos) == 1  # una sola vez, no en cada poll

    otro.soltar()  # el otro proceso terminó: este toma el lugar
    esperar(lambda: job(ctx, jid).estado == "hecho", mensaje="que tome el job al quedar libre")
    assert corrio.is_set()
    assert any("quedó libre" in r.getMessage() for r in caplog.records)


def test_procesar_hasta_con_el_server_andando_solo_espera(ctx: Contexto, etapas, otro_proceso):
    # El "server" (otro proceso) tiene el worker y ya tomó un bloque; la CLI no arranca su worker,
    # no corre nada y al cortarse (timeout o Ctrl+C) no libera lo que corre allá.
    otro_proceso()
    sid = nueva_sesion(ctx, "s-imp", estado="procesando")
    corridas: list[int] = []
    etapas["transcribir"] = lambda c, j: (corridas.append(j.args["n"]), Resultado())[1]
    del_server = encolar(ctx, "transcribir", sid, {"n": 1})
    pendiente = encolar(ctx, "transcribir", sid, {"n": 2})
    with ctx.base.conexion() as conn:
        tomado = cola.tomar(conn, ["transcribir"])
    assert tomado.id == del_server
    avisos: list[str] = []
    w = Worker(ctx, solo_sesion=sid)
    assert w.procesar_hasta(sid, timeout_s=1.5, avisar=avisos.append) == "procesando"
    assert w._hilo is None and corridas == []
    j = job(ctx, del_server)
    assert j.estado == "corriendo" and j.intentos == 1 and j.lease_hasta == tomado.lease_hasta
    j = job(ctx, pendiente)
    assert j.estado == "pendiente" and j.intentos == 0
    assert len(avisos) == 1 and "el server está procesando la sesión; espero" in avisos[0].lower()


def test_procesar_hasta_sigue_solo_si_el_server_termina(ctx: Contexto, etapas, otro_proceso):
    otro = otro_proceso()
    sid = nueva_sesion(ctx, "s-imp-2")

    def verificar(c, j):
        with c.base.conexion() as conn:
            actualizar_sesion(conn, j.sesion_id, c.cfg.zona_horaria, estado="lista")
        return Resultado()

    etapas["verificar"] = verificar
    jid = encolar(ctx, "verificar", sid)
    avisos: list[str] = []
    resultado: dict[str, str] = {}
    w = Worker(ctx, solo_sesion=sid)
    hilo = threading.Thread(target=lambda: resultado.update(
        estado=w.procesar_hasta(sid, timeout_s=30, avisar=avisos.append)))
    hilo.start()
    try:
        esperar(lambda: avisos, mensaje="el aviso de espera")
        time.sleep(0.3)
        assert job(ctx, jid).estado == "pendiente"
        otro.soltar()
        hilo.join(30)
    finally:
        otro.soltar()
        hilo.join(30)
    assert resultado == {"estado": "lista"}
    assert job(ctx, jid).estado == "hecho"
    assert "dejó de procesar" in avisos[1]


# --- serve no abre la base de otro server (27/10) --------------------------------------------------

def test_serve_no_abre_la_base_si_otro_server_tiene_la_carpeta(cfg, otro_proceso, monkeypatch, capsys):
    """Un server de prueba con CLASSMER_HOME mal puesto apuntaba a la carpeta de producción: abrir() la
    migró. Ahora `serve` toma el lock de la carpeta de datos antes de abrir la base; si lo tiene otro
    proceso, sale sin tocar nada."""
    from classmer import cli, db

    otro_proceso()
    abiertas: list[object] = []
    monkeypatch.setattr(db, "abrir", lambda *a, **k: abiertas.append(a) or pytest.fail("abrió la base"))
    monkeypatch.setattr(cli, "_puerto_ocupado", lambda host, port: None)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("arrancó el server"))
    assert cli.main(["serve", "--port", "8799"]) == 5
    err = capsys.readouterr().err
    assert "Ya hay otro classmer serve" in err and str(cfg.datos) in err
    assert abiertas == []
