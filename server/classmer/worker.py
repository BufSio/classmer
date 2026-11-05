"""Worker de jobs dentro del mismo proceso (§10).

Un hilo despachador toma jobs de la cola y los corre en pools con límite de concurrencia:
hasta N transcripciones en paralelo (config: transcribir.paralelo), apuntes de a uno y el
resto de las etapas en un pool general. Renueva el lease de lo que está corriendo y, al
terminar cada job, encola los siguientes en la misma transacción.

Cada corrida se identifica por el objeto `Job` que devolvió `cola.tomar`. Al cerrar un job
(hecho, fallo, pospuesto) se comprueba que esa corrida siga siendo la dueña: si `detener()` lo
liberó y otro lo retomó, el resultado tardío se descarta en vez de pisar la corrida nueva.

Un solo proceso por carpeta de datos toma jobs: el lock `worker.lock` es del proceso (no de
cada Worker) y no se suelta en `detener()`, porque los hilos de las etapas liberadas siguen
vivos hasta que sale el proceso. Así una segunda `classmer serve` o un `importar --procesar`
con el server andando no corren lo mismo dos veces.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from classmer import cola
from classmer.cola import Job
from classmer.db import transaccion
from classmer.pipeline import (
    ETAPAS_SILENCIOSAS,
    Contexto,
    CuotaAgotada,
    ErrorEtapa,
    EsperandoUsuario,
    actualizar_sesion,
    etapa,
)
from classmer.util import ahora as _ahora

log = logging.getLogger("classmer.worker")

ESTADOS_FINALES = {"lista", "error", "esperando_usuario"}
MANTENIMIENTO_S = 30.0
LIMPIEZA_TEMPORALES_S = 3600.0   # el área temporal de "Importar clase" (lo abandonado hace más de 24 h)
INTENTOS_CIERRE = 3      # un lock pasajero de la base no tiene que perder el cierre de una etapa
ESPERA_CIERRE_S = 2.0
LOCK_ARCHIVO = "worker.lock"


# --- un solo worker por carpeta de datos ----------------------------------------------------------

_locks_worker: dict[str, int] = {}   # carpeta de datos → fd con el lock (hasta que sale el proceso)
_locks_worker_guard = threading.Lock()


def tomar_lock_worker(datos: Path) -> bool:
    """Toma el lock de worker de `datos` (o ya lo tiene este proceso). False si lo tiene otro."""
    ruta = os.path.abspath(datos)
    clave = os.path.normcase(ruta)
    with _locks_worker_guard:
        if clave in _locks_worker:
            return True
        os.makedirs(ruta, exist_ok=True)
        fd = os.open(os.path.join(ruta, LOCK_ARCHIVO), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        _locks_worker[clave] = fd
        return True


def _imprimir(mensaje: str) -> None:
    print(mensaje, flush=True)


class _Pool:
    def __init__(self, nombre: str, tipos: list[str] | None, limite: int):
        self.nombre = nombre
        self.tipos = tipos  # None = todos los que no tienen pool propio
        self.limite = max(1, limite)
        self.ejecutor = self._nuevo_ejecutor()
        self.cerrado = False
        self.corriendo: dict[int, Future] = {}

    def _nuevo_ejecutor(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=self.limite, thread_name_prefix=f"job-{self.nombre}")

    def abrir(self) -> None:
        if self.cerrado:
            self.ejecutor = self._nuevo_ejecutor()
            self.cerrado = False

    def cerrar(self) -> None:
        self.cerrado = True
        self.ejecutor.shutdown(wait=False, cancel_futures=True)

    def libres(self) -> int:
        return self.limite - len(self.corriendo)


class Worker:
    def __init__(self, ctx: Contexto, solo_sesion: str | None = None):
        self.ctx = ctx
        self.solo_sesion = solo_sesion  # la CLI sólo toma los jobs de la sesión que importó
        self._esperando_lock = False
        cfg = ctx.cfg
        self.lease_s = cfg.worker.lease_s
        self.poll_s = cfg.worker.poll_s
        self.pools = [
            _Pool("transcribir", ["transcribir"], cfg.transcribir.paralelo),
            _Pool("apunte", ["apunte"], 1),
            _Pool("general", None, cfg.worker.paralelo_general),
        ]
        self._propios = [t for p in self.pools if p.tipos for t in p.tipos]
        self._parar = threading.Event()
        self._despertar = threading.Event()
        self._hilo: threading.Thread | None = None
        self._lock = threading.Lock()
        self._corridas: dict[int, Job] = {}    # job id → corrida vigente de este worker
        self._liberados: dict[int, Job] = {}   # corridas que detener() liberó y siguen en curso

    # --- ciclo de vida ---
    def iniciar(self) -> None:
        if self._hilo and self._hilo.is_alive():
            return
        for p in self.pools:
            p.abrir()
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, name="classmer-worker", daemon=True)
        self._hilo.start()
        log.info("worker iniciado")

    def detener(self, espera_s: float = 5.0) -> None:
        """Deja de tomar jobs; lo que sigue corriendo se libera (vuelve a pendiente).

        Si una etapa liberada termina bien mientras el proceso sigue vivo y nadie la retomó, su
        resultado se guarda igual (no se tira el trabajo hecho).
        """
        self._parar.set()
        self._despertar.set()
        if self._hilo:
            self._hilo.join(timeout=espera_s)
        for p in self.pools:
            p.cerrar()  # cancela lo que estaba en la fila del ejecutor y no arrancó
        with self._lock:
            en_curso = [jid for p in self.pools for jid, f in p.corriendo.items()
                        if f.cancelled() or not f.done()]
            # Bajo el lock: así el cierre de una corrida no se cruza con su liberación.
            for jid in en_curso:
                job = self._corridas.pop(jid, None)
                if job is not None:
                    self._liberados[jid] = job
                try:
                    with self.ctx.base.conexion() as conn:
                        cola.liberar(conn, jid)
                except sqlite3.Error:
                    log.exception("no pude liberar el job %s", jid)
        log.info("worker detenido (%d job(s) liberados)", len(en_curso))

    def despertar(self) -> None:
        """Avisar que hay jobs nuevos (p. ej., al finalizar una sesión)."""
        self._despertar.set()

    # --- bucle principal ---
    def _bucle(self) -> None:
        ultimo_mantenimiento = 0.0
        ultima_limpieza = time.time()  # al arrancar ya limpió el lifespan del server
        intervalo = min(MANTENIMIENTO_S, max(0.5, self.lease_s / 3))
        while not self._parar.is_set():
            try:
                tomados = 0
                if self._tengo_lock():  # si otro proceso es el worker, no toco la cola
                    ahora = time.time()
                    if ahora - ultimo_mantenimiento >= intervalo:
                        self._mantenimiento()
                        ultimo_mantenimiento = ahora
                    if ahora - ultima_limpieza >= LIMPIEZA_TEMPORALES_S:
                        ultima_limpieza = ahora
                        self._limpiar_temporales()
                    tomados = self.despachar()
            except Exception:
                log.exception("error en el bucle del worker")
                tomados = 0
            if not tomados:
                self._despertar.wait(self.poll_s)
                self._despertar.clear()

    def _tengo_lock(self) -> bool:
        """Lock de worker de la carpeta de datos; avisa una sola vez mientras lo tenga otro."""
        datos = self.ctx.cfg.datos
        if tomar_lock_worker(datos):
            if self._esperando_lock:
                self._esperando_lock = False
                log.info("el worker de %s quedó libre: empiezo a tomar jobs", datos)
            return True
        if not self._esperando_lock:
            self._esperando_lock = True
            log.warning("otro proceso tiene el worker de %s (¿ya hay otro classmer serve?): "
                        "no tomo jobs y reintento", datos)
        return False

    def _mantenimiento(self) -> None:
        """Renueva el lease de lo propio y después recupera lo vencido (en ese orden)."""
        zona = self.ctx.cfg.zona_horaria
        with self.ctx.base.conexion() as conn:
            with self._lock:
                ids = list(self._corridas)
            for jid in ids:
                cola.renovar(conn, jid, lease_s=self.lease_s)
            momento = _ahora()
            with transaccion(conn):
                agotados = conn.execute(
                    "SELECT tipo, sesion_id FROM jobs WHERE estado = 'corriendo' AND lease_hasta < ? "
                    "AND intentos >= max_intentos",
                    (momento,),
                ).fetchall()
                n = cola.recuperar_vencidos(conn, ahora=momento)
                for f in agotados:
                    if f["sesion_id"] and f["tipo"] not in ETAPAS_SILENCIOSAS:
                        actualizar_sesion(conn, f["sesion_id"], zona, estado="error",
                                          error=f"{f['tipo']}: se cortó a mitad y no quedan intentos")
        if n:
            log.warning("%d job(s) con lease vencido volvieron a la cola", n)
        if agotados:
            log.warning("%d job(s) con lease vencido quedaron en error (sin intentos)", len(agotados))

    def _limpiar_temporales(self) -> None:
        from classmer.importador import limpiar_temporales

        try:
            limpiar_temporales(self.ctx.cfg)
        except Exception:
            log.exception("no pude limpiar el área temporal de importaciones")

    def despachar(self) -> int:
        """Toma jobs mientras haya lugar en los pools. Devuelve cuántos tomó."""
        tomados = 0
        if not self._tengo_lock():
            return 0
        with self._lock:
            for p in self.pools:
                p.corriendo = {j: f for j, f in p.corriendo.items() if not f.done()}
        for p in self.pools:
            while not self._parar.is_set():
                with self._lock:
                    if p.libres() <= 0:
                        break
                with self.ctx.base.conexion() as conn:
                    if p.tipos:
                        job = cola.tomar(conn, p.tipos, sesion_id=self.solo_sesion, lease_s=self.lease_s)
                    else:
                        job = cola.tomar(conn, excluir=self._propios, sesion_id=self.solo_sesion,
                                         lease_s=self.lease_s)
                if job is None:
                    break
                with self._lock:
                    self._corridas[job.id] = job
                    self._liberados.pop(job.id, None)
                try:
                    fut = p.ejecutor.submit(self.ejecutar_job, job)
                except RuntimeError:
                    # El ejecutor ya se cerró (detener en curso): el job vuelve a la cola.
                    with self._lock:
                        self._corridas.pop(job.id, None)
                    with self.ctx.base.conexion() as conn:
                        cola.liberar(conn, job.id)
                    return tomados
                with self._lock:
                    p.corriendo[job.id] = fut
                fut.add_done_callback(lambda _f: self._despertar.set())
                tomados += 1
        return tomados

    # --- cierre de una corrida ---
    def _cerrar(self, job: Job, *, exito: bool, accion: Callable[[sqlite3.Connection], None]) -> bool:
        """Aplica `accion` (en una transacción) si esta corrida todavía puede cerrar el job.

        - Corrida vigente: si el job sigue 'corriendo' (o, si terminó bien, 'pendiente' porque
          se recuperó por lease).
        - Corrida liberada por detener(): sólo si terminó bien y el job sigue 'pendiente'
          (nadie lo retomó). Un fallo de una corrida liberada no se registra: ya está en la cola.

        Si la base da OperationalError (p. ej., 'database is locked'), reintenta: la transacción
        se deshizo entera, así que `accion` se puede repetir.
        """
        intento = 1
        while True:
            try:
                return self._cerrar_una_vez(job, exito=exito, accion=accion)
            except sqlite3.OperationalError:
                if intento >= INTENTOS_CIERRE:
                    raise
                log.warning("job %s (%s): no pude cerrarlo (intento %d de %d); reintento en %.0f s",
                            job.id, job.tipo, intento, INTENTOS_CIERRE, ESPERA_CIERRE_S, exc_info=True)
                intento += 1
                time.sleep(ESPERA_CIERRE_S)

    def _cerrar_una_vez(self, job: Job, *, exito: bool, accion: Callable[[sqlite3.Connection], None]) -> bool:
        with self._lock:
            propia = self._corridas.get(job.id) is job
            liberada = self._liberados.get(job.id) is job
            if not (propia or liberada):
                return False
            with self.ctx.base.conexion() as conn, transaccion(conn):
                fila = conn.execute("SELECT estado FROM jobs WHERE id = ?", (job.id,)).fetchone()
                estado = fila["estado"] if fila else None
                if propia:
                    ok = estado == "corriendo" or (exito and estado == "pendiente")
                else:
                    ok = exito and estado == "pendiente"
                if ok:
                    accion(conn)
            return ok

    def _soltar(self, job: Job) -> None:
        with self._lock:
            if self._corridas.get(job.id) is job:
                del self._corridas[job.id]
            if self._liberados.get(job.id) is job:
                del self._liberados[job.id]

    @staticmethod
    def _estado_sesion(conn: sqlite3.Connection, sesion_id: str) -> str | None:
        fila = conn.execute("SELECT estado FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
        return fila["estado"] if fila else None

    # --- ejecución de un job ---
    def ejecutar_job(self, job: Job) -> None:
        """Corre un job ya tomado con `cola.tomar` (lo llama el pool; también sirve directo)."""
        with self._lock:
            if self._corridas.get(job.id) is not job and self._liberados.get(job.id) is not job:
                self._corridas[job.id] = job  # llamada directa, sin pasar por despachar()
        try:
            self._ejecutar(job)
        except Exception:
            # Falló algo fuera de la etapa (p. ej., cerrar el job): que no quede mudo en el Future.
            # El job sigue 'corriendo' y vuelve a la cola cuando venza el lease.
            log.exception("job %s (%s): error fuera de la etapa", job.id, job.tipo)
        finally:
            self._soltar(job)

    def _ejecutar(self, job: Job) -> None:
        ctx = self.ctx
        zona = ctx.cfg.zona_horaria
        sid = job.sesion_id
        visible = bool(sid) and job.tipo not in ETAPAS_SILENCIOSAS
        etiqueta = f"{job.tipo}" + (f" bloque {job.args['n']}" if "n" in job.args else "")

        if self._parar.is_set():
            # Se tomó justo antes de detener: vuelve a la cola sin correr.
            self._cerrar(job, exito=False, accion=lambda conn: cola.liberar(conn, job.id))
            return

        if visible:
            try:
                with ctx.base.conexion() as conn, transaccion(conn):
                    # Una sesión en error (p. ej., otro bloque sin más intentos) no vuelve a
                    # 'procesando' por un job hermano; sólo reprocesar/asignar la sacan de ahí.
                    if self._estado_sesion(conn, sid) != "error":
                        actualizar_sesion(conn, sid, zona, estado="procesando", etapa=job.tipo, error=None)
            except sqlite3.Error:
                # Es sólo lo que ve la app: no vale abortar la etapa por esto.
                log.exception("job %s (%s): no pude marcar la sesión en 'procesando'; sigo igual",
                              job.id, job.tipo)
        if sid:
            ctx.log_sesion(sid, f"inicia {etiqueta} (intento {job.intentos})")
        t0 = time.time()
        try:
            resultado = etapa(job.tipo)(ctx, job)
        except CuotaAgotada as e:
            def posponer(conn: sqlite3.Connection) -> None:
                cola.posponer(conn, job, e.espera_s, str(e))
                if visible and self._estado_sesion(conn, sid) != "error":
                    actualizar_sesion(conn, sid, zona, error=f"{job.tipo}: {e} (reintenta solo)")

            if self._cerrar(job, exito=False, accion=posponer) and sid:
                ctx.log_sesion(sid, f"{etiqueta} pospuesto {e.espera_s:.0f}s: {e}")
        except EsperandoUsuario as e:
            def esperar(conn: sqlite3.Connection) -> None:
                cola.completar(conn, job.id, log=getattr(e, "log", None) or str(e), motor=getattr(e, "motor", None),
                               costo=getattr(e, "costo", None), facturacion=getattr(e, "facturacion", None))
                if sid:
                    actualizar_sesion(conn, sid, zona, estado="esperando_usuario", error=str(e),
                                      espera=getattr(e, "espera", "materia"))

            if self._cerrar(job, exito=False, accion=esperar) and sid:
                ctx.log_sesion(sid, f"{etiqueta}: esperando usuario: {e}")
        except Exception as e:  # ErrorEtapa o cualquier otra cosa
            reintentable = e.reintentable if isinstance(e, ErrorEtapa) else True
            mensaje = str(e) or e.__class__.__name__
            if not isinstance(e, ErrorEtapa):
                mensaje = f"{e.__class__.__name__}: {mensaje}"
                log.error("job %s (%s) falló:\n%s", job.id, job.tipo, traceback.format_exc())
            reintenta = False

            def fallar(conn: sqlite3.Connection) -> None:
                nonlocal reintenta
                reintenta = cola.fallar(conn, job, mensaje, reintentable=reintentable)
                if not visible:
                    return
                if not reintenta:
                    actualizar_sesion(conn, sid, zona, estado="error", error=f"{job.tipo}: {mensaje}")
                elif self._estado_sesion(conn, sid) != "error":
                    actualizar_sesion(conn, sid, zona, error=f"{job.tipo}: {mensaje} (reintenta)")

            if self._cerrar(job, exito=False, accion=fallar) and sid:
                ctx.log_sesion(sid, f"{etiqueta} falló ({'reintenta' if reintenta else 'sin más intentos'}): {mensaje}")
        else:
            def completar(conn: sqlite3.Connection) -> None:
                cola.completar(conn, job.id, log=resultado.log, motor=resultado.motor, costo=resultado.costo,
                              facturacion=resultado.facturacion)
                for tipo, args in resultado.siguientes:
                    cola.encolar(conn, tipo, sid, args)

            if self._cerrar(job, exito=True, accion=completar):
                if sid:
                    ctx.log_sesion(sid, f"termina {etiqueta} en {time.time() - t0:.1f}s")
                self._despertar.set()
            else:
                log.warning("job %s (%s): ya no es de esta corrida (liberado o retomado); "
                            "descarto el resultado", job.id, job.tipo)

    # --- uso desde la CLI ---
    def procesar_hasta(self, sesion_id: str, timeout_s: float | None = None,
                       avisar: Callable[[str], None] = _imprimir) -> str:
        """Corre el worker hasta que la sesión llegue a un estado final. Devuelve ese estado.

        Si el worker de la carpeta de datos lo tiene otro proceso (el server), no arranca el
        propio: sólo espera mirando el estado, así cortar (Ctrl+C o timeout) no libera nada que
        esté corriendo allá. Si ese proceso termina, sigue acá mismo.
        """
        datos = self.ctx.cfg.datos
        propio = tomar_lock_worker(datos)
        if propio:
            avisar("Procesando acá mismo (Ctrl+C para cortar; el server retoma lo que falte)…")
            self.iniciar()
        else:
            avisar("El server está procesando la sesión; espero a que termine (Ctrl+C sólo deja de esperar)…")
        limite = time.time() + timeout_s if timeout_s else None
        try:
            while True:
                with self.ctx.base.conexion() as conn:
                    fila = conn.execute("SELECT estado FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
                    activos = conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE sesion_id = ? AND estado IN ('pendiente','corriendo')",
                        (sesion_id,),
                    ).fetchone()[0]
                estado = fila["estado"] if fila else "error"
                if estado in ESTADOS_FINALES and not activos:
                    return estado
                if limite and time.time() > limite:
                    return estado
                if not propio and tomar_lock_worker(datos):
                    propio = True
                    avisar("El server dejó de procesar: sigo acá mismo…")
                    self.iniciar()
                time.sleep(1)
        finally:
            if propio:
                self.detener()
