"""Worker (§10) con etapas falsas: encadenamiento, concurrencia, estados de la sesión y apagado."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable

import pytest

from classmer import cola, sesiones
from classmer import worker as worker_mod
from classmer.pipeline import (
    ETAPAS,
    Contexto,
    CuotaAgotada,
    ErrorEtapa,
    EsperandoUsuario,
    Resultado,
    actualizar_sesion,
    calcular_progreso,
)
from classmer.worker import Worker

INICIO = "2026-10-15T14:00:05-03:00"


# --- utilidades ----------------------------------------------------------------------------------

def esperar(condicion: Callable[[], bool], timeout: float = 10.0, mensaje: str = "") -> None:
    limite = time.time() + timeout
    while time.time() < limite:
        if condicion():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout esperando: {mensaje or condicion}")


def nueva_sesion(ctx: Contexto, sid: str, estado: str = "en_cola", materia_id: str | None = "prueba") -> str:
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id=materia_id, origen_materia=None,
                       tipo_clase=None, inicio=INICIO, dispositivo=None)
        actualizar_sesion(conn, sid, ctx.cfg.zona_horaria, estado=estado)
    return sid


def sesion(ctx: Contexto, sid: str) -> dict:
    with ctx.base.conexion() as conn:
        return dict(conn.execute("SELECT * FROM sesiones WHERE id = ?", (sid,)).fetchone())


def jobs(ctx: Contexto, sid: str | None = None) -> list[cola.Job]:
    with ctx.base.conexion() as conn:
        if sid is None:
            filas = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            return [cola.Job.desde_fila(f) for f in filas]
        return cola.jobs_de_sesion(conn, sid)


def job(ctx: Contexto, job_id: int) -> cola.Job:
    with ctx.base.conexion() as conn:
        return cola.Job.desde_fila(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def encolar(ctx: Contexto, tipo: str, sid: str | None, args: dict | None = None, **kw) -> int:
    with ctx.base.conexion() as conn:
        jid = cola.encolar(conn, tipo, sid, args, **kw)
    assert jid is not None
    return jid


def sin_activos(ctx: Contexto) -> bool:
    with ctx.base.conexion() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE estado IN ('pendiente', 'corriendo')"
        ).fetchone()[0] == 0


class Concurrencia:
    """Cuenta cuántas corridas de cada grupo hay a la vez.

    Cada corrida espera (hasta `paciencia` s) a que el grupo llegue a `objetivo` antes de seguir:
    así el máximo se alcanza aunque la máquina esté cargada, y si el worker dejara correr más
    de lo permitido, el máximo lo delataría igual.
    """

    def __init__(self, paciencia: float = 0.5):
        self.cond = threading.Condition()
        self.paciencia = paciencia
        self.ahora: dict[str, int] = defaultdict(int)
        self.maximo: dict[str, int] = defaultdict(int)
        self.total: dict[str, int] = defaultdict(int)

    def correr(self, grupo: str, segundos: float, objetivo: int = 1) -> None:
        with self.cond:
            self.ahora[grupo] += 1
            self.total[grupo] += 1
            self.maximo[grupo] = max(self.maximo[grupo], self.ahora[grupo])
            self.cond.notify_all()
            self.cond.wait_for(lambda: self.ahora[grupo] >= objetivo, timeout=self.paciencia)
        try:
            time.sleep(segundos)
        finally:
            with self.cond:
                self.ahora[grupo] -= 1


@pytest.fixture
def etapas(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable]:
    """Registro de etapas falsas: el worker busca acá en vez de importar classmer.pipeline.<etapa>."""
    registro: dict[str, Callable] = {}

    def buscar(tipo: str):
        if tipo not in registro:
            raise KeyError(f"etapa falsa no registrada: {tipo}")
        return registro[tipo]

    monkeypatch.setattr(worker_mod, "etapa", buscar)
    return registro


@pytest.fixture
def sin_espera(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reintentos inmediatos (la espera real arranca en 30 s)."""
    monkeypatch.setattr(cola, "espera_reintento", lambda intentos: 0.0)


@pytest.fixture
def cli_sin_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los comandos de la CLI configuran el logging global; en los tests, no."""
    from classmer import cli

    monkeypatch.setattr(cli, "configurar_logging", lambda *a, **k: None)


@pytest.fixture
def eventos_a_soltar():
    """Eventos que el test bloquea; se sueltan al final aunque el test falle."""
    lista: list[threading.Event] = []
    yield lista
    for e in lista:
        e.set()


@pytest.fixture
def worker(ctx: Contexto, eventos_a_soltar):
    creados: list[Worker] = []

    def crear() -> Worker:
        w = Worker(ctx)
        creados.append(w)
        return w

    yield crear
    for e in eventos_a_soltar:
        e.set()
    for w in creados:
        w.detener(espera_s=2)


# --- encadenamiento ------------------------------------------------------------------------------

def test_encadenamiento_completo(ctx: Contexto, etapas, worker):
    sid = nueva_sesion(ctx, "s-cadena")
    vistos: list[tuple[str, str, str | None]] = []
    lock = threading.Lock()
    bloques_hechos: set[int] = set()
    n_bloques = 4

    def registrar(tipo: str, j) -> None:
        s = sesion(ctx, j.sesion_id)
        with lock:
            vistos.append((tipo, s["estado"], s["etapa"]))

    def simple(tipo: str, siguientes: list[tuple[str, dict]], **extra):
        def ejecutar(c, j):
            assert c is ctx and j.tipo == tipo
            registrar(tipo, j)
            return Resultado(siguientes=list(siguientes), **extra)
        return ejecutar

    def transcribir(c, j):
        registrar("transcribir", j)
        time.sleep(0.05)
        with lock:
            bloques_hechos.add(j.args["n"])
            todos = len(bloques_hechos) == n_bloques
        return Resultado(siguientes=[("linea_de_tiempo", {})] if todos else [])

    def publicar(c, j):
        registrar("publicar", j)
        with c.base.conexion() as conn:
            actualizar_sesion(conn, j.sesion_id, c.cfg.zona_horaria, estado="lista")
        return Resultado(siguientes=[("notificar", {})])

    etapas.update({
        "verificar": simple("verificar", [("segmentar", {})]),
        "segmentar": simple("segmentar", [("transcribir", {"n": k}) for k in range(1, n_bloques + 1)]),
        "transcribir": transcribir,
        "linea_de_tiempo": simple("linea_de_tiempo", [("clasificar", {})]),
        "clasificar": simple("clasificar", [("revisar_transcript", {})]),
        "revisar_transcript": simple("revisar_transcript", [("apunte", {})]),
        "apunte": simple("apunte", [("publicar", {})], log="agente ok", motor="claude-agent", costo=1.25),
        "publicar": publicar,
        "notificar": simple("notificar", []),
    })
    encolar(ctx, "verificar", sid)
    w = worker()
    w.iniciar()
    esperar(lambda: sin_activos(ctx) and any(t == "notificar" for t, _, _ in vistos), mensaje="pipeline")

    tipos = [t for t, _, _ in vistos]
    sin_bloques = [t for t in tipos if t != "transcribir"]
    assert sin_bloques == [e for e in ETAPAS if e != "transcribir"]
    assert tipos.count("transcribir") == n_bloques
    assert tipos.index("segmentar") < tipos.index("transcribir")
    assert max(i for i, t in enumerate(tipos) if t == "transcribir") < tipos.index("linea_de_tiempo")

    # Mientras corre cada etapa visible, la sesión está en 'procesando' con esa etapa.
    for t, estado, etapa_vista in vistos:
        if t == "notificar":
            assert (estado, etapa_vista) == ("lista", "publicar")  # silenciosa: no toca la sesión
        else:
            assert (estado, etapa_vista) == ("procesando", t)

    todos = jobs(ctx, sid)
    assert all(j.estado == "hecho" and j.intentos == 1 for j in todos)
    assert sorted(j.args.get("n") for j in todos if j.tipo == "transcribir") == [1, 2, 3, 4]
    assert [j.tipo for j in todos].count("linea_de_tiempo") == 1
    apunte = next(j for j in todos if j.tipo == "apunte")
    assert apunte.motor == "claude-agent" and apunte.log == "agente ok"
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT costo FROM jobs WHERE id = ?", (apunte.id,)).fetchone()[0] == 1.25
        assert calcular_progreso(conn, sid) == 1.0
    s = sesion(ctx, sid)
    assert s["estado"] == "lista" and s["error"] is None
    log = (ctx.sesion_dir(sid) / "pipeline.log").read_text(encoding="utf-8")
    assert "inicia verificar" in log and "termina apunte" in log and "bloque 3" in log


def test_siguientes_duplicados_se_deduplican(ctx: Contexto, etapas, worker):
    sid = nueva_sesion(ctx, "s-dup")
    etapas["verificar"] = lambda c, j: Resultado(siguientes=[("segmentar", {}), ("segmentar", {})])
    llamado = threading.Event()
    etapas["segmentar"] = lambda c, j: (llamado.set(), Resultado())[1]
    encolar(ctx, "verificar", sid)
    worker().iniciar()
    esperar(lambda: llamado.is_set() and sin_activos(ctx))
    assert [j.tipo for j in jobs(ctx, sid)] == ["verificar", "segmentar"]


def test_job_sin_sesion(ctx: Contexto, etapas, worker):
    hecho = threading.Event()
    etapas["verificar"] = lambda c, j: (hecho.set(), Resultado(siguientes=[]))[1]
    jid = encolar(ctx, "verificar", None)
    worker().iniciar()
    esperar(lambda: job(ctx, jid).estado == "hecho")


# --- concurrencia -------------------------------------------------------------------------------

def test_limites_de_concurrencia(ctx: Contexto, etapas, worker):
    conc = Concurrencia()
    etapas["transcribir"] = lambda c, j: (conc.correr("transcribir", 0.05, objetivo=3), Resultado())[1]
    etapas["apunte"] = lambda c, j: (conc.correr("apunte", 0.1, objetivo=2), Resultado())[1]
    etapas["verificar"] = lambda c, j: (conc.correr("general", 0.05, objetivo=2), Resultado())[1]
    s1 = nueva_sesion(ctx, "s-conc-1")
    for n in range(1, 10):
        encolar(ctx, "transcribir", s1, {"n": n})
    for k in range(3):
        encolar(ctx, "apunte", nueva_sesion(ctx, f"s-ap-{k}"))
    for k in range(5):
        encolar(ctx, "verificar", nueva_sesion(ctx, f"s-ve-{k}"))
    worker().iniciar()
    esperar(lambda: sin_activos(ctx), timeout=20)
    assert conc.total == {"transcribir": 9, "apunte": 3, "general": 5}
    assert conc.maximo["transcribir"] == 3        # hasta 3 transcripciones en paralelo
    assert conc.maximo["apunte"] == 1             # los apuntes, de a uno
    assert conc.maximo["general"] == ctx.cfg.worker.paralelo_general == 2


def test_paralelo_de_transcribir_sale_de_la_config(ctx: Contexto, etapas, worker):
    ctx.cfg.transcribir.paralelo = 2
    conc = Concurrencia()
    etapas["transcribir"] = lambda c, j: (conc.correr("t", 0.05, objetivo=3), Resultado())[1]
    sid = nueva_sesion(ctx, "s-par")
    for n in range(1, 7):
        encolar(ctx, "transcribir", sid, {"n": n})
    worker().iniciar()
    esperar(lambda: sin_activos(ctx), timeout=20)
    assert conc.maximo["t"] == 2 and conc.total["t"] == 6


def test_pools_no_se_bloquean_entre_si(ctx: Contexto, etapas, worker, eventos_a_soltar):
    # Un apunte largo no frena las transcripciones ni el pool general.
    soltar = threading.Event()
    eventos_a_soltar.append(soltar)
    empezo = threading.Event()

    def apunte(c, j):
        empezo.set()
        soltar.wait(10)
        return Resultado()

    etapas["apunte"] = apunte
    etapas["transcribir"] = lambda c, j: Resultado()
    etapas["verificar"] = lambda c, j: Resultado()
    ap = encolar(ctx, "apunte", nueva_sesion(ctx, "s-largo"))
    otra = nueva_sesion(ctx, "s-otra")
    t = encolar(ctx, "transcribir", otra, {"n": 1})
    v = encolar(ctx, "verificar", otra)
    worker().iniciar()
    esperar(lambda: empezo.is_set())
    esperar(lambda: job(ctx, t).estado == "hecho" and job(ctx, v).estado == "hecho")
    assert job(ctx, ap).estado == "corriendo"
    soltar.set()
    esperar(lambda: job(ctx, ap).estado == "hecho")


# --- errores y estados de la sesión ----------------------------------------------------------------

def test_error_al_agotar_intentos(ctx: Contexto, etapas, worker, sin_espera):
    sid = nueva_sesion(ctx, "s-err")
    llamadas: list[tuple[int, str, str | None]] = []

    def segmentar(c, j):
        s = sesion(ctx, sid)
        llamadas.append((j.intentos, s["estado"], s["error"]))
        raise ErrorEtapa("ffmpeg explotó")

    etapas["segmentar"] = segmentar
    jid = encolar(ctx, "segmentar", sid)
    worker().iniciar()
    esperar(lambda: job(ctx, jid).estado == "error")
    j = job(ctx, jid)
    assert j.intentos == j.max_intentos == 3 and j.error == "ffmpeg explotó"
    assert [x[0] for x in llamadas] == [1, 2, 3]
    assert all(x[1] == "procesando" and x[2] is None for x in llamadas)  # al arrancar se limpia
    s = sesion(ctx, sid)
    assert s["estado"] == "error" and s["etapa"] == "segmentar"
    assert s["error"] == "segmentar: ffmpeg explotó"
    log = (ctx.sesion_dir(sid) / "pipeline.log").read_text(encoding="utf-8")
    assert "reintenta" in log and "sin más intentos" in log


def test_error_reintentable_deja_aviso_en_la_sesion(ctx: Contexto, etapas, worker):
    # Con la espera real (30 s) el job queda pendiente y la sesión sigue procesando con el aviso.
    sid = nueva_sesion(ctx, "s-aviso")
    llamado = threading.Event()

    def segmentar(c, j):
        llamado.set()
        raise ErrorEtapa("se cortó la red")

    etapas["segmentar"] = segmentar
    jid = encolar(ctx, "segmentar", sid)
    worker().iniciar()
    esperar(lambda: llamado.is_set() and job(ctx, jid).estado == "pendiente")
    j = job(ctx, jid)
    assert j.intentos == 1 and j.run_after > time.time() + 20
    s = sesion(ctx, sid)
    assert s["estado"] == "procesando"
    assert s["error"] == "segmentar: se cortó la red (reintenta)"


def test_error_no_reintentable(ctx: Contexto, etapas, worker):
    sid = nueva_sesion(ctx, "s-noreint")
    llamadas = []
    etapas["verificar"] = lambda c, j: (llamadas.append(1), (_ for _ in ()).throw(
        ErrorEtapa("el audio no es Ogg", reintentable=False)))
    jid = encolar(ctx, "verificar", sid)
    worker().iniciar()
    esperar(lambda: job(ctx, jid).estado == "error")
    assert len(llamadas) == 1 and job(ctx, jid).intentos == 1
    assert sesion(ctx, sid)["error"] == "verificar: el audio no es Ogg"
    assert sesion(ctx, sid)["estado"] == "error"


def test_excepcion_cualquiera_cuenta_como_reintentable(ctx: Contexto, etapas, worker, sin_espera):
    sid = nueva_sesion(ctx, "s-exc")
    llamadas = []

    def apunte(c, j):
        llamadas.append(j.intentos)
        raise ValueError("meta.json inválido")

    etapas["apunte"] = apunte
    jid = encolar(ctx, "apunte", sid)
    worker().iniciar()
    esperar(lambda: job(ctx, jid).estado == "error")
    assert llamadas == [1, 2]  # max_intentos de apunte = 2
    assert job(ctx, jid).error == "ValueError: meta.json inválido"
    assert sesion(ctx, sid)["error"] == "apunte: ValueError: meta.json inválido"


def test_falla_y_despues_anda(ctx: Contexto, etapas, worker, sin_espera):
    sid = nueva_sesion(ctx, "s-recupera")
    intentos = []

    def segmentar(c, j):
        intentos.append(j.intentos)
        if j.intentos == 1:
            raise ErrorEtapa("primera vez falla")
        return Resultado(siguientes=[("transcribir", {"n": 1})])

    hecho = threading.Event()
    etapas["segmentar"] = segmentar
    etapas["transcribir"] = lambda c, j: (hecho.set(), Resultado())[1]
    jid = encolar(ctx, "segmentar", sid)
    worker().iniciar()
    esperar(lambda: hecho.is_set() and sin_activos(ctx))
    assert intentos == [1, 2]
    j = job(ctx, jid)
    assert j.estado == "hecho" and j.intentos == 2 and j.error is None
    s = sesion(ctx, sid)
    assert s["estado"] == "procesando" and s["error"] is None and s["etapa"] == "transcribir"


def test_esperando_usuario(ctx: Contexto, etapas, worker):
    sid = nueva_sesion(ctx, "s-espera", materia_id=None)

    def clasificar(c, j):
        raise EsperandoUsuario("no sé si es Macro o Riesgo (confianza 0.4)")

    etapas["clasificar"] = clasificar
    jid = encolar(ctx, "clasificar", sid)
    worker().iniciar()
    esperar(lambda: job(ctx, jid).estado == "hecho")
    s = sesion(ctx, sid)
    assert s["estado"] == "esperando_usuario"
    assert s["error"] == "no sé si es Macro o Riesgo (confianza 0.4)"
    assert [j.tipo for j in jobs(ctx, sid)] == ["clasificar"]  # no sigue solo
    assert job(ctx, jid).intentos == 1


def test_cuota_agotada_pospone_sin_contar_intento(ctx: Contexto, etapas, worker):
    sid = nueva_sesion(ctx, "s-cuota")
    llamadas: list[tuple[float, int]] = []
    avisos: list[str | None] = []

    def apunte(c, j):
        llamadas.append((time.time(), j.intentos))
        if len(llamadas) > 1:
            avisos.append(sesion(ctx, sid)["error"])
        if len(llamadas) <= 3:
            raise CuotaAgotada("429: límite del plan", espera_s=0.2)
        return Resultado()

    etapas["apunte"] = apunte
    jid = encolar(ctx, "apunte", sid)  # max_intentos = 2: tres cuotas no lo agotan
    worker().iniciar()
    esperar(lambda: job(ctx, jid).estado == "hecho", timeout=15)
    assert [i for _, i in llamadas] == [1, 1, 1, 1]
    esperas = [b - a for (a, _), (b, _) in zip(llamadas, llamadas[1:])]
    assert all(e >= 0.18 for e in esperas), esperas  # respetó run_after
    assert job(ctx, jid).intentos == 1
    s = sesion(ctx, sid)
    assert s["estado"] == "procesando" and s["error"] is None


def test_cuota_agotada_larga_queda_pendiente(ctx: Contexto, etapas, worker):
    sid = nueva_sesion(ctx, "s-cuota-larga")
    llamado = threading.Event()

    def transcribir(c, j):
        llamado.set()
        raise CuotaAgotada("falta GEMINI_API_KEY", espera_s=3600)

    etapas["transcribir"] = transcribir
    jid = encolar(ctx, "transcribir", sid, {"n": 1})
    worker().iniciar()
    esperar(lambda: llamado.is_set() and job(ctx, jid).estado == "pendiente")
    j = job(ctx, jid)
    assert j.intentos == 0 and j.error == "falta GEMINI_API_KEY"
    assert j.run_after > time.time() + 3500
    s = sesion(ctx, sid)
    assert s["estado"] == "procesando"
    assert s["error"] == "transcribir: falta GEMINI_API_KEY (reintenta solo)"


def test_sesion_en_error_no_vuelve_a_procesando(ctx: Contexto, etapas, worker, sin_espera):
    # Un bloque sin más intentos deja la sesión en error; los bloques hermanos que siguen
    # no la "reviven" ni pisan el mensaje.
    ctx.cfg.transcribir.paralelo = 1
    sid = nueva_sesion(ctx, "s-hermanos")
    vistos: dict[int, list[str]] = defaultdict(list)
    fallas_b2 = []

    def transcribir(c, j):
        n = j.args["n"]
        vistos[n].append(sesion(ctx, sid)["estado"])
        if n == 1:
            raise ErrorEtapa("bloque 1 ilegible", reintentable=False)
        if n == 2 and not fallas_b2:
            fallas_b2.append(1)
            raise ErrorEtapa("bloque 2 timeout")
        return Resultado()

    etapas["transcribir"] = transcribir
    for n in (1, 2, 3):
        encolar(ctx, "transcribir", sid, {"n": n})
    worker().iniciar()
    esperar(lambda: sin_activos(ctx))
    assert vistos[1] == ["procesando"]
    assert vistos[2] == ["error", "error"] and vistos[3] == ["error"]
    s = sesion(ctx, sid)
    assert s["estado"] == "error" and s["error"] == "transcribir: bloque 1 ilegible"


def test_etapa_silenciosa_no_toca_la_sesion(ctx: Contexto, etapas, worker):
    sid = nueva_sesion(ctx, "s-lista", estado="lista")

    def notificar(c, j):
        raise ErrorEtapa("el bot no responde", reintentable=False)

    etapas["notificar"] = notificar
    jid = encolar(ctx, "notificar", sid)
    worker().iniciar()
    esperar(lambda: job(ctx, jid).estado == "error")
    s = sesion(ctx, sid)
    assert s["estado"] == "lista" and s["error"] is None


# --- lease ------------------------------------------------------------------------------------------

def test_mantenimiento_recupera_lo_vencido(ctx: Contexto, etapas, worker):
    # Jobs que quedaron 'corriendo' de un server que se cayó.
    s_ok = nueva_sesion(ctx, "s-caido-ok", estado="procesando")
    s_mal = nueva_sesion(ctx, "s-caido-mal", estado="procesando")
    recuperable = encolar(ctx, "segmentar", s_ok)
    agotado = encolar(ctx, "verificar", s_mal, max_intentos=1)
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE jobs SET estado = 'corriendo', intentos = 1, lease_hasta = ? WHERE id IN (?, ?)",
                     (time.time() - 10, recuperable, agotado))
    corrio = threading.Event()
    etapas["segmentar"] = lambda c, j: (corrio.set(), Resultado())[1]
    etapas["verificar"] = lambda c, j: pytest.fail("no tenía que volver a correr")
    worker().iniciar()
    esperar(lambda: corrio.is_set() and job(ctx, recuperable).estado == "hecho")
    assert job(ctx, recuperable).intentos == 2
    esperar(lambda: job(ctx, agotado).estado == "error")
    assert "sin más intentos" in job(ctx, agotado).error
    s = sesion(ctx, s_mal)
    assert s["estado"] == "error" and s["error"].startswith("verificar:")


def test_renueva_el_lease_de_lo_que_corre(ctx: Contexto, etapas, worker, eventos_a_soltar):
    ctx.cfg.worker.lease_s = 1  # mantenimiento cada 0,5 s
    sid = nueva_sesion(ctx, "s-lease")
    soltar = threading.Event()
    eventos_a_soltar.append(soltar)
    llamadas = []

    def apunte(c, j):
        llamadas.append(j.intentos)
        soltar.wait(10)
        return Resultado()

    etapas["apunte"] = apunte
    jid = encolar(ctx, "apunte", sid)
    w = worker()
    w.iniciar()
    esperar(lambda: llamadas)
    lease_inicial = job(ctx, jid).lease_hasta
    time.sleep(2.2)  # más que el lease: sin renovar, el mantenimiento lo recuperaría
    j = job(ctx, jid)
    assert j.estado == "corriendo" and j.lease_hasta > lease_inicial
    assert llamadas == [1]
    soltar.set()
    esperar(lambda: job(ctx, jid).estado == "hecho")
    assert llamadas == [1]


# --- detener ---------------------------------------------------------------------------------------

def test_detener_libera_los_jobs_que_corrian(ctx: Contexto, etapas, worker, eventos_a_soltar):
    sid = nueva_sesion(ctx, "s-stop")
    empezaron = threading.Semaphore(0)
    soltar = threading.Event()
    eventos_a_soltar.append(soltar)

    def transcribir(c, j):
        empezaron.release()
        soltar.wait(10)
        return Resultado(siguientes=[("linea_de_tiempo", {})])

    etapas["transcribir"] = transcribir
    ids = [encolar(ctx, "transcribir", sid, {"n": n}) for n in (1, 2, 3, 4)]
    w = worker()
    w.iniciar()
    for _ in range(3):
        assert empezaron.acquire(timeout=10)
    esperar(lambda: sum(job(ctx, i).estado == "corriendo" for i in ids) == 3)
    w.detener(espera_s=2)
    for i in ids:
        j = job(ctx, i)
        assert j.estado == "pendiente" and j.intentos == 0 and j.lease_hasta is None, j
    # Si una etapa liberada termina bien y nadie la retomó, el trabajo no se tira.
    soltar.set()
    esperar(lambda: sum(job(ctx, i).estado == "hecho" for i in ids) == 3)
    assert [job(ctx, i).estado for i in ids].count("pendiente") == 1  # la 4.ª nunca arrancó
    assert [j.tipo for j in jobs(ctx, sid)].count("linea_de_tiempo") == 1


def test_detener_y_fallo_tardio_no_cuenta(ctx: Contexto, etapas, worker, eventos_a_soltar):
    sid = nueva_sesion(ctx, "s-stop-falla")
    empezo = threading.Event()
    soltar = threading.Event()
    eventos_a_soltar.append(soltar)

    def apunte(c, j):
        empezo.set()
        soltar.wait(10)
        raise ErrorEtapa("se cortó por el apagado")

    etapas["apunte"] = apunte
    jid = encolar(ctx, "apunte", sid)
    w = worker()
    w.iniciar()
    assert empezo.wait(10)
    w.detener(espera_s=2)
    soltar.set()
    esperar(lambda: not w._liberados, mensaje="que termine la corrida liberada")
    j = job(ctx, jid)
    assert j.estado == "pendiente" and j.intentos == 0 and j.error is None
    assert sesion(ctx, sid)["estado"] == "procesando"


def test_resultado_tardio_no_pisa_una_corrida_nueva(ctx: Contexto, etapas, worker, eventos_a_soltar):
    sid = nueva_sesion(ctx, "s-retoma")
    primera, segunda = threading.Event(), threading.Event()
    eventos_a_soltar.extend([primera, segunda])
    llamadas: list[int] = []
    arranco = [threading.Event(), threading.Event()]

    def segmentar(c, j):
        k = len(llamadas)
        llamadas.append(k)
        arranco[k].set()
        (primera if k == 0 else segunda).wait(10)
        return Resultado(siguientes=[("transcribir", {"n": 1})])

    etapas["segmentar"] = segmentar
    etapas["transcribir"] = lambda c, j: Resultado()
    jid = encolar(ctx, "segmentar", sid)
    w1 = worker()
    w1.iniciar()
    assert arranco[0].wait(10)
    w1.detener(espera_s=2)
    assert job(ctx, jid).estado == "pendiente"
    w2 = worker()
    w2.iniciar()
    assert arranco[1].wait(10)
    assert job(ctx, jid).estado == "corriendo"
    primera.set()  # la corrida liberada termina mientras w2 tiene el job
    esperar(lambda: not w1._liberados, mensaje="que termine la corrida liberada")
    assert job(ctx, jid).estado == "corriendo"
    assert [j.tipo for j in jobs(ctx, sid)] == ["segmentar"]
    segunda.set()
    esperar(lambda: sin_activos(ctx))
    assert job(ctx, jid).estado == "hecho"
    assert [j.tipo for j in jobs(ctx, sid)] == ["segmentar", "transcribir"]


def test_iniciar_despues_de_detener(ctx: Contexto, etapas, worker):
    sid = nueva_sesion(ctx, "s-reinicio")
    etapas["verificar"] = lambda c, j: Resultado()
    w = worker()
    w.iniciar()
    w.detener(espera_s=2)
    jid = encolar(ctx, "verificar", sid)
    w.iniciar()
    w.despertar()
    esperar(lambda: job(ctx, jid).estado == "hecho")


def test_detener_sin_iniciar(ctx: Contexto):
    Worker(ctx).detener(espera_s=0.1)


# --- uso directo y CLI -------------------------------------------------------------------------------

def test_ejecutar_job_directo(ctx: Contexto, etapas):
    sid = nueva_sesion(ctx, "s-directo")
    etapas["verificar"] = lambda c, j: Resultado(siguientes=[("segmentar", {})], log="ok")
    encolar(ctx, "verificar", sid)
    with ctx.base.conexion() as conn:
        j = cola.tomar(conn)
    w = Worker(ctx)
    w.ejecutar_job(j)
    assert job(ctx, j.id).estado == "hecho" and job(ctx, j.id).log == "ok"
    assert [(x.tipo, x.estado) for x in jobs(ctx, sid)] == [("verificar", "hecho"), ("segmentar", "pendiente")]
    assert sesion(ctx, sid)["estado"] == "procesando"


def test_procesar_hasta(ctx: Contexto, etapas):
    sid = nueva_sesion(ctx, "s-cli")
    etapas["verificar"] = lambda c, j: Resultado(siguientes=[("publicar", {})])

    def publicar(c, j):
        with c.base.conexion() as conn:
            actualizar_sesion(conn, j.sesion_id, c.cfg.zona_horaria, estado="lista")
        return Resultado()

    etapas["publicar"] = publicar
    encolar(ctx, "verificar", sid)
    assert Worker(ctx).procesar_hasta(sid, timeout_s=20) == "lista"


def test_cli_asignar_retoma_desde_la_revision(ctx: Contexto, capsys, cli_sin_logging):
    from classmer import cli

    sid = nueva_sesion(ctx, "s-asignar", estado="esperando_usuario", materia_id=None)
    assert cli.main(["asignar", sid, "--materia", "prueba"]) == 0
    s = sesion(ctx, sid)
    assert (s["materia_id"], s["origen_materia"], s["estado"], s["error"]) == ("prueba", "manual", "en_cola", None)
    assert [(j.tipo, j.estado) for j in jobs(ctx, sid)] == [("revisar_transcript", "pendiente")]
    assert "sigue la revisión del transcript y el apunte" in capsys.readouterr().out


def test_cli_asignar_con_el_pipeline_en_curso_no_adelanta_el_apunte(ctx: Contexto, capsys, cli_sin_logging):
    from classmer import cli

    sid = nueva_sesion(ctx, "s-asignar-2", estado="procesando", materia_id=None)
    encolar(ctx, "transcribir", sid, {"n": 1})
    assert cli.main(["asignar", sid, "--materia", "otra"]) == 0
    s = sesion(ctx, sid)
    assert s["materia_id"] == "otra" and s["estado"] == "procesando"
    assert [j.tipo for j in jobs(ctx, sid)] == ["transcribir"]
    assert cli.main(["asignar", sid, "--materia", "no-existe"]) == 2
    assert cli.main(["asignar", "no-hay-tal-sesion", "--materia", "prueba"]) == 1


def test_procesar_hasta_devuelve_error(ctx: Contexto, etapas):
    sid = nueva_sesion(ctx, "s-cli-error")

    def verificar(c, j):
        raise ErrorEtapa("no hay audio", reintentable=False)

    etapas["verificar"] = verificar
    encolar(ctx, "verificar", sid)
    assert Worker(ctx).procesar_hasta(sid, timeout_s=20) == "error"


def test_procesar_hasta_solo_toma_jobs_de_su_sesion(ctx: Contexto, etapas):
    # `importar --procesar` no se lleva (ni después suelta) los jobs de otra sesión.
    ajena = nueva_sesion(ctx, "s-app")
    propia = nueva_sesion(ctx, "s-imp")
    corridas: list[tuple[str, str]] = []

    def verificar(c, j):
        corridas.append(("verificar", j.sesion_id))
        if j.sesion_id == propia:
            with c.base.conexion() as conn:
                actualizar_sesion(conn, propia, c.cfg.zona_horaria, estado="lista")
        return Resultado()

    etapas["verificar"] = verificar
    etapas["transcribir"] = lambda c, j: (corridas.append(("transcribir", j.sesion_id)), Resultado())[1]
    etapas["apunte"] = lambda c, j: (corridas.append(("apunte", j.sesion_id)), Resultado())[1]
    ajenos = [encolar(ctx, "verificar", ajena), encolar(ctx, "transcribir", ajena, {"n": 1}),
              encolar(ctx, "apunte", ajena)]  # encolados antes: sin filtro saldrían primero
    encolar(ctx, "verificar", propia)
    avisos: list[str] = []
    w = Worker(ctx, solo_sesion=propia)
    assert w.procesar_hasta(propia, timeout_s=20, avisar=avisos.append) == "lista"
    assert corridas == [("verificar", propia)]
    for jid in ajenos:
        j = job(ctx, jid)
        assert j.estado == "pendiente" and j.intentos == 0, j
    assert avisos and avisos[0].startswith("Procesando acá mismo")


# --- errores fuera de la etapa -------------------------------------------------------------------

@pytest.fixture
def cierre_sin_espera(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_mod, "ESPERA_CIERRE_S", 0.0)


def test_cierre_reintenta_si_la_base_esta_bloqueada(ctx: Contexto, etapas, worker, cierre_sin_espera,
                                                    monkeypatch: pytest.MonkeyPatch):
    # Un 'database is locked' pasajero al cerrar no pierde una etapa que ya terminó (p. ej.,
    # publicar): no queda 'corriendo' 15 min para después repetirse.
    sid = nueva_sesion(ctx, "s-bloqueada")
    corridas: list[int] = []
    etapas["publicar"] = lambda c, j: (corridas.append(j.intentos), Resultado(siguientes=[("notificar", {})]))[1]
    etapas["notificar"] = lambda c, j: Resultado()
    original = cola.completar
    fallas: list[int] = []

    def completar(conn, job_id, **kw):
        if not fallas:
            fallas.append(job_id)
            raise sqlite3.OperationalError("database is locked")
        return original(conn, job_id, **kw)

    monkeypatch.setattr(cola, "completar", completar)
    jid = encolar(ctx, "publicar", sid)
    worker().iniciar()
    esperar(lambda: sin_activos(ctx), mensaje="que cierre publicar y notificar")
    assert fallas == [jid] and corridas == [1]
    assert [(j.tipo, j.estado, j.intentos) for j in jobs(ctx, sid)] == [("publicar", "hecho", 1),
                                                                        ("notificar", "hecho", 1)]


def test_error_al_cerrar_queda_en_el_log(ctx: Contexto, etapas, worker, cierre_sin_espera, caplog,
                                         monkeypatch: pytest.MonkeyPatch):
    def completar(conn, job_id, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cola, "completar", completar)
    sid = nueva_sesion(ctx, "s-mudo")
    etapas["verificar"] = lambda c, j: Resultado()
    jid = encolar(ctx, "verificar", sid)
    w = worker()
    w.iniciar()
    esperar(lambda: any("error fuera de la etapa" in r.getMessage() for r in caplog.records),
            mensaje="el log del error al cerrar")
    errores = [r for r in caplog.records if "error fuera de la etapa" in r.getMessage()]
    assert len(errores) == 1 and errores[0].exc_info and "disk I/O error" in str(errores[0].exc_info[1])
    reintentos = [r for r in caplog.records if "no pude cerrarlo" in r.getMessage()]
    assert len(reintentos) == worker_mod.INTENTOS_CIERRE - 1
    assert job(ctx, jid).estado == "corriendo"  # vuelve a la cola cuando venza el lease
    esperar(lambda: not w._corridas)


def test_falla_al_marcar_procesando_no_aborta_la_etapa(ctx: Contexto, etapas, worker, caplog,
                                                       monkeypatch: pytest.MonkeyPatch):
    original = worker_mod.actualizar_sesion

    def actualizar(conn, sid, zona, **campos):
        if campos.get("estado") == "procesando":
            raise sqlite3.OperationalError("database is locked")
        return original(conn, sid, zona, **campos)

    monkeypatch.setattr(worker_mod, "actualizar_sesion", actualizar)
    sid = nueva_sesion(ctx, "s-cosmetico")
    corridas: list[int] = []
    etapas["segmentar"] = lambda c, j: (corridas.append(j.intentos), Resultado())[1]
    jid = encolar(ctx, "segmentar", sid)
    worker().iniciar()
    esperar(lambda: job(ctx, jid).estado == "hecho")
    assert corridas == [1]
    assert any("procesando" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


# --- progreso ----------------------------------------------------------------------------------------

def test_progreso_de_una_sesion_reencolada_conserva_lo_hecho(ctx: Contexto, cli_sin_logging):
    from classmer import cli

    sid = nueva_sesion(ctx, "s-prog", estado="esperando_usuario", materia_id=None)
    for tipo in ("verificar", "segmentar", "linea_de_tiempo", "clasificar"):
        jid = encolar(ctx, tipo, sid)
        with ctx.base.conexion() as conn:
            cola.completar(conn, jid)
    with ctx.base.conexion() as conn:
        antes = calcular_progreso(conn, sid)
    assert antes == pytest.approx(0.1)
    assert cli.main(["asignar", sid, "--materia", "prueba"]) == 0
    assert sesion(ctx, sid)["estado"] == "en_cola"
    with ctx.base.conexion() as conn:
        assert calcular_progreso(conn, sid) == antes  # no vuelve a 0 mientras espera el apunte
    # Recién finalizada (en_cola sin nada hecho) y grabando siguen en 0.
    nueva = nueva_sesion(ctx, "s-prog-nueva", estado="en_cola")
    encolar(ctx, "verificar", nueva)
    grabando = nueva_sesion(ctx, "s-prog-grabando", estado="grabando")
    with ctx.base.conexion() as conn:
        assert calcular_progreso(conn, nueva) == 0.0
        assert calcular_progreso(conn, grabando) == 0.0
