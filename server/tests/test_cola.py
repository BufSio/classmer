"""Cola de jobs en SQLite (§10): toma atómica, prioridades, run_after, lease, reintentos y claves."""

from __future__ import annotations

import threading

import pytest

from classmer import cola
from classmer.db import Base

T0 = 1_800_000_000.0  # un "ahora" fijo para que los tiempos sean exactos


@pytest.fixture
def conn(base: Base):
    with base.conexion() as c:
        yield c


def estado(conn, job_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


# --- encolar y claves ------------------------------------------------------------------------------

def test_encolar_valores_por_defecto(conn):
    jid = cola.encolar(conn, "transcribir", "s1", {"n": 3}, ahora=T0)
    f = estado(conn, jid)
    assert f["tipo"] == "transcribir" and f["sesion_id"] == "s1" and f["estado"] == "pendiente"
    assert f["prioridad"] == cola.PRIORIDADES["transcribir"]
    assert f["max_intentos"] == cola.MAX_INTENTOS["transcribir"]
    assert f["intentos"] == 0 and f["run_after"] == 0 and f["lease_hasta"] is None
    assert f["clave"] == "transcribir:s1:3" and f["creado"] == T0
    j = cola.Job.desde_fila(conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone())
    assert j.args == {"n": 3}
    assert estado(conn, cola.encolar(conn, "verificar", "s1"))["max_intentos"] == 3
    assert estado(conn, cola.encolar(conn, "apunte", "s1"))["max_intentos"] == 2
    assert estado(conn, cola.encolar(conn, "otra-cosa", "s1"))["prioridad"] == 100


def test_prioridades_siguen_el_orden_del_pipeline():
    from classmer.pipeline import ETAPAS

    valores = [cola.PRIORIDADES[e] for e in ETAPAS]
    assert valores == sorted(valores) and len(set(valores)) == len(valores)


def test_clave_evita_duplicados_activos(conn):
    a = cola.encolar(conn, "apunte", "s1", ahora=T0)
    assert a is not None
    assert cola.encolar(conn, "apunte", "s1", ahora=T0 + 1) is None          # pendiente
    j = cola.tomar(conn, ahora=T0 + 2)
    assert j.id == a
    assert cola.encolar(conn, "apunte", "s1", ahora=T0 + 3) is None          # corriendo
    cola.completar(conn, a, ahora=T0 + 4)
    b = cola.encolar(conn, "apunte", "s1", ahora=T0 + 5)                     # hecho: se puede re-encolar
    assert b is not None and b != a
    assert cola.encolar(conn, "apunte", "s2", ahora=T0) is not None          # otra sesión
    assert cola.encolar(conn, "publicar", "s1", ahora=T0) is not None        # otra etapa
    n = conn.execute("SELECT COUNT(*) FROM jobs WHERE clave = 'apunte:s1'").fetchone()[0]
    assert n == 2


def test_clave_por_bloque(conn):
    assert cola.encolar(conn, "transcribir", "s1", {"n": 1}) is not None
    assert cola.encolar(conn, "transcribir", "s1", {"n": 2}) is not None
    assert cola.encolar(conn, "transcribir", "s1", {"n": 1}) is None
    assert cola.clave_default("transcribir", "s1", {"n": 7}) == "transcribir:s1:7"
    assert cola.clave_default("apunte", "s1", {}) == "apunte:s1"
    assert cola.clave_default("mantenimiento", None, None) is None


@pytest.mark.parametrize("final", ["error", "cancelado"])
def test_clave_libre_despues_de_error_o_cancelado(conn, final: str):
    a = cola.encolar(conn, "segmentar", "s1")
    conn.execute("UPDATE jobs SET estado = ? WHERE id = ?", (final, a))
    assert cola.encolar(conn, "segmentar", "s1") is not None


def test_sin_clave_no_deduplica(conn):
    assert cola.encolar(conn, "mantenimiento") is not None
    assert cola.encolar(conn, "mantenimiento") is not None
    assert cola.encolar(conn, "apunte", "s1", clave=None) is not None
    assert cola.encolar(conn, "apunte", "s1", clave=None) is not None


def test_clave_explicita(conn):
    assert cola.encolar(conn, "apunte", "s1", clave="propia") is not None
    assert cola.encolar(conn, "publicar", "s2", clave="propia") is None


# --- tomar -----------------------------------------------------------------------------------------

def test_tomar_respeta_prioridad_y_creado(conn):
    ids = {
        "apunte": cola.encolar(conn, "apunte", "s1", ahora=T0),
        "verificar-viejo": cola.encolar(conn, "verificar", "s2", ahora=T0 + 1),
        "verificar-nuevo": cola.encolar(conn, "verificar", "s3", ahora=T0 + 2),
        "transcribir": cola.encolar(conn, "transcribir", "s1", {"n": 1}, ahora=T0 - 100),
        "urgente": cola.encolar(conn, "notificar", "s9", prioridad=1, ahora=T0 + 50),
    }
    orden = [cola.tomar(conn, ahora=T0 + 100).id for _ in range(5)]
    assert orden == [ids["urgente"], ids["verificar-viejo"], ids["verificar-nuevo"],
                     ids["transcribir"], ids["apunte"]]
    assert cola.tomar(conn, ahora=T0 + 100) is None


def test_tomar_desempata_por_id(conn):
    a = cola.encolar(conn, "verificar", "s1", ahora=T0)
    b = cola.encolar(conn, "verificar", "s2", ahora=T0)
    assert [cola.tomar(conn, ahora=T0).id, cola.tomar(conn, ahora=T0).id] == [a, b]


def test_tomar_respeta_run_after(conn):
    tarde = cola.encolar(conn, "verificar", "s1", run_after=T0 + 60, ahora=T0)
    ya = cola.encolar(conn, "apunte", "s2", ahora=T0)
    assert cola.tomar(conn, ahora=T0).id == ya       # aunque verificar tenga más prioridad
    assert cola.tomar(conn, ahora=T0 + 59.9) is None
    assert cola.tomar(conn, ahora=T0 + 60).id == tarde  # run_after <= ahora


def test_tomar_marca_corriendo_con_lease(conn):
    jid = cola.encolar(conn, "segmentar", "s1", ahora=T0)
    j = cola.tomar(conn, ahora=T0 + 5, lease_s=120)
    assert j.id == jid and j.estado == "corriendo" and j.intentos == 1
    assert j.lease_hasta == T0 + 125
    f = estado(conn, jid)
    assert f["estado"] == "corriendo" and f["intentos"] == 1 and f["lease_hasta"] == T0 + 125
    assert f["actualizado"] == T0 + 5
    assert cola.tomar(conn, ahora=T0 + 6) is None  # no se toma dos veces


def test_tomar_lease_por_defecto(conn):
    cola.encolar(conn, "segmentar", "s1")
    j = cola.tomar(conn, ahora=T0)
    assert j.lease_hasta == T0 + cola.LEASE_S == T0 + 900


def test_tomar_filtra_tipos_y_excluir(conn):
    t1 = cola.encolar(conn, "transcribir", "s1", {"n": 1}, ahora=T0)
    t2 = cola.encolar(conn, "transcribir", "s1", {"n": 2}, ahora=T0)
    ap = cola.encolar(conn, "apunte", "s2", ahora=T0)
    ve = cola.encolar(conn, "verificar", "s3", ahora=T0)
    pu = cola.encolar(conn, "publicar", "s4", ahora=T0)
    assert cola.tomar(conn, ["apunte"], ahora=T0).id == ap
    assert cola.tomar(conn, ["apunte"], ahora=T0) is None
    assert cola.tomar(conn, ["transcribir"], ahora=T0).id == t1
    general = [cola.tomar(conn, excluir=["transcribir", "apunte"], ahora=T0) for _ in range(3)]
    assert [j.id if j else None for j in general] == [ve, pu, None]
    assert cola.tomar(conn, ["transcribir", "publicar"], ahora=T0).id == t2
    assert cola.tomar(conn, ahora=T0) is None


def test_tomar_filtra_por_sesion(conn):
    # La CLI (`importar --procesar`) sólo toma lo de su sesión, nunca jobs de otras.
    otra = cola.encolar(conn, "verificar", "s-otra", ahora=T0 - 10)  # más vieja: saldría primero
    propia = cola.encolar(conn, "verificar", "s-mia", ahora=T0)
    sin_sesion = cola.encolar(conn, "mantenimiento", None, ahora=T0)
    t = cola.encolar(conn, "transcribir", "s-mia", {"n": 1}, ahora=T0)
    assert cola.tomar(conn, ["transcribir"], sesion_id="s-otra", ahora=T0) is None
    assert cola.tomar(conn, sesion_id="s-mia", ahora=T0).id == propia
    assert cola.tomar(conn, ["transcribir"], sesion_id="s-mia", ahora=T0).id == t
    assert cola.tomar(conn, excluir=["transcribir"], sesion_id="s-mia", ahora=T0) is None
    assert {cola.tomar(conn, ahora=T0).id, cola.tomar(conn, ahora=T0).id} == {otra, sin_sesion}


def test_tomar_ignora_estados_no_pendientes(conn):
    for est in ("corriendo", "hecho", "error", "cancelado"):
        jid = cola.encolar(conn, "verificar", f"s-{est}")
        conn.execute("UPDATE jobs SET estado = ? WHERE id = ?", (est, jid))
    assert cola.tomar(conn, ahora=T0) is None


def test_tomar_atomico_con_muchos_hilos(base: Base):
    total = 300
    with base.conexion() as c:
        for i in range(total):
            cola.encolar(c, "verificar", f"s{i}", ahora=T0 + i)
    tomados: list[int] = []
    errores: list[str] = []
    lock = threading.Lock()
    barrera = threading.Barrier(16)

    def trabajar():
        try:
            with base.conexion() as c:
                barrera.wait()
                while (j := cola.tomar(c, ahora=T0 + total)) is not None:
                    with lock:
                        tomados.append(j.id)
        except Exception as e:  # pragma: no cover - lo reporta el assert
            errores.append(repr(e))

    hilos = [threading.Thread(target=trabajar) for _ in range(16)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    assert not errores
    assert len(tomados) == total and len(set(tomados)) == total
    with base.conexion() as c:
        fila = c.execute("SELECT COUNT(*), MIN(intentos), MAX(intentos) FROM jobs WHERE estado = 'corriendo'").fetchone()
    assert tuple(fila) == (total, 1, 1)


def test_tomar_y_encolar_concurrentes(base: Base):
    # Productores y consumidores a la vez: cada job encolado se toma exactamente una vez.
    encolados: list[int] = []
    tomados: list[int] = []
    lock = threading.Lock()
    fin = threading.Event()

    def productor(k: int):
        with base.conexion() as c:
            for i in range(50):
                jid = cola.encolar(c, "verificar", f"p{k}-{i}")
                with lock:
                    encolados.append(jid)

    def consumidor():
        with base.conexion() as c:
            while True:
                terminaron = fin.is_set()  # antes de tomar: si ya terminaron y no hay nada, listo
                j = cola.tomar(c)
                if j is not None:
                    with lock:
                        tomados.append(j.id)
                elif terminaron:
                    return
                else:
                    threading.Event().wait(0.001)

    cons = [threading.Thread(target=consumidor) for _ in range(6)]
    prods = [threading.Thread(target=productor, args=(k,)) for k in range(4)]
    for h in cons + prods:
        h.start()
    for h in prods:
        h.join(timeout=60)
    fin.set()
    for h in cons:
        h.join(timeout=60)
    assert sorted(tomados) == sorted(encolados) and len(set(tomados)) == 200


# --- lease vencido ---------------------------------------------------------------------------------

def test_lease_vencido_vuelve_a_pendiente(conn):
    jid = cola.encolar(conn, "segmentar", "s1", ahora=T0)
    cola.tomar(conn, ahora=T0, lease_s=100)
    assert cola.recuperar_vencidos(conn, ahora=T0 + 100) == 0   # todavía no venció (lease_hasta < ahora)
    assert estado(conn, jid)["estado"] == "corriendo"
    assert cola.recuperar_vencidos(conn, ahora=T0 + 101) == 1
    f = estado(conn, jid)
    assert f["estado"] == "pendiente" and f["lease_hasta"] is None and f["error"] == "lease vencido"
    assert f["run_after"] == T0 + 101 and f["intentos"] == 1  # el intento cortado cuenta
    j = cola.tomar(conn, ahora=T0 + 102)
    assert j.id == jid and j.intentos == 2


def test_lease_vencido_sin_intentos_va_a_error(conn):
    jid = cola.encolar(conn, "segmentar", "s1", max_intentos=2, ahora=T0)
    for i in range(2):
        cola.tomar(conn, ahora=T0 + i * 1000, lease_s=10)
        cola.recuperar_vencidos(conn, ahora=T0 + i * 1000 + 11)
    f = estado(conn, jid)
    assert f["estado"] == "error" and f["intentos"] == 2
    assert "sin más intentos" in f["error"]
    assert cola.tomar(conn, ahora=T0 + 10_000) is None


def test_recuperar_no_toca_jobs_vigentes_ni_otros_estados(conn):
    vig = cola.encolar(conn, "verificar", "s1")
    cola.tomar(conn, ahora=T0, lease_s=1000)
    pend = cola.encolar(conn, "verificar", "s2")
    assert cola.recuperar_vencidos(conn, ahora=T0 + 500) == 0
    assert estado(conn, vig)["estado"] == "corriendo" and estado(conn, pend)["estado"] == "pendiente"


def test_renovar_extiende_el_lease(conn):
    jid = cola.encolar(conn, "apunte", "s1")
    cola.tomar(conn, ahora=T0, lease_s=100)
    cola.renovar(conn, jid, ahora=T0 + 90, lease_s=100)
    assert estado(conn, jid)["lease_hasta"] == T0 + 190
    assert cola.recuperar_vencidos(conn, ahora=T0 + 150) == 0
    cola.completar(conn, jid, ahora=T0 + 160)
    cola.renovar(conn, jid, ahora=T0 + 170)  # ya no corre: no hace nada
    assert estado(conn, jid)["lease_hasta"] is None


# --- completar / fallar / posponer / liberar ------------------------------------------------------

def test_completar(conn):
    jid = cola.encolar(conn, "apunte", "s1")
    cola.tomar(conn, ahora=T0)
    conn.execute("UPDATE jobs SET error = 'algo viejo', log = 'log viejo' WHERE id = ?", (jid,))
    cola.completar(conn, jid, motor="claude-agent", costo=0.5, ahora=T0 + 10)
    f = estado(conn, jid)
    assert f["estado"] == "hecho" and f["lease_hasta"] is None and f["error"] is None
    assert f["motor"] == "claude-agent" and f["costo"] == 0.5 and f["log"] == "log viejo"
    cola.completar(conn, jid, log="nuevo")
    assert estado(conn, jid)["log"] == "nuevo" and estado(conn, jid)["motor"] == "claude-agent"


def test_espera_reintento_creciente_con_tope():
    esperas = [cola.espera_reintento(i) for i in range(0, 8)]
    assert esperas[:5] == [30.0, 30.0, 120.0, 480.0, 1920.0]
    assert esperas[5:] == [3600.0, 3600.0, 3600.0]
    assert all(a <= b for a, b in zip(esperas, esperas[1:]))


def test_fallar_con_espera_creciente_y_tope(conn):
    jid = cola.encolar(conn, "transcribir", "s1", {"n": 1}, max_intentos=4, ahora=T0)
    ahora = T0
    run_afters = []
    for intento in range(1, 5):
        j = cola.tomar(conn, ahora=ahora)
        assert j is not None and j.id == jid and j.intentos == intento
        vuelve = cola.fallar(conn, j, f"falla {intento}", ahora=ahora)
        f = estado(conn, jid)
        if intento < 4:
            assert vuelve is True
            assert f["estado"] == "pendiente" and f["lease_hasta"] is None
            assert f["run_after"] == ahora + cola.espera_reintento(intento)
            run_afters.append(f["run_after"] - ahora)
            assert cola.tomar(conn, ahora=f["run_after"] - 0.001) is None  # respeta la espera
            ahora = f["run_after"]
        else:
            assert vuelve is False
            assert f["estado"] == "error"
        assert f["error"] == f"falla {intento}"
    assert run_afters == [30.0, 120.0, 480.0]
    assert cola.tomar(conn, ahora=ahora + 10_000) is None


def test_fallar_no_reintentable_va_directo_a_error(conn):
    jid = cola.encolar(conn, "verificar", "s1")
    j = cola.tomar(conn, ahora=T0)
    assert cola.fallar(conn, j, "audio ilegible", reintentable=False, ahora=T0) is False
    f = estado(conn, jid)
    assert f["estado"] == "error" and f["intentos"] == 1 and f["error"] == "audio ilegible"


def test_fallar_usa_los_intentos_de_la_base(conn):
    # El objeto Job puede estar viejo: manda lo que dice la base.
    jid = cola.encolar(conn, "verificar", "s1", max_intentos=3)
    j = cola.tomar(conn, ahora=T0)
    conn.execute("UPDATE jobs SET intentos = 3 WHERE id = ?", (jid,))
    assert cola.fallar(conn, j, "x", ahora=T0) is False


def test_posponer_no_cuenta_intento(conn):
    jid = cola.encolar(conn, "transcribir", "s1", {"n": 1}, max_intentos=2, ahora=T0)
    for i in range(5):  # muchas cuotas agotadas seguidas no agotan los intentos
        j = cola.tomar(conn, ahora=T0 + i * 1000)
        assert j is not None and j.intentos == 1
        cola.posponer(conn, j, 300, "429 rate limit", ahora=T0 + i * 1000)
        f = estado(conn, jid)
        assert f["estado"] == "pendiente" and f["intentos"] == 0
        assert f["run_after"] == T0 + i * 1000 + 300 and f["error"] == "429 rate limit"
        assert f["lease_hasta"] is None
        assert cola.tomar(conn, ahora=T0 + i * 1000 + 299) is None
    j = cola.tomar(conn, ahora=T0 + 10_000)
    assert cola.fallar(conn, j, "falla real", ahora=T0 + 10_000) is True  # sigue teniendo intentos


def test_posponer_espera_negativa_es_cero(conn):
    jid = cola.encolar(conn, "verificar", "s1")
    j = cola.tomar(conn, ahora=T0)
    cola.posponer(conn, j, -10, "x", ahora=T0)
    assert estado(conn, jid)["run_after"] == T0


def test_liberar(conn):
    jid = cola.encolar(conn, "apunte", "s1", ahora=T0)
    cola.tomar(conn, ahora=T0)
    cola.liberar(conn, jid, ahora=T0 + 5)
    f = estado(conn, jid)
    assert f["estado"] == "pendiente" and f["intentos"] == 0 and f["lease_hasta"] is None
    assert f["run_after"] == T0 + 5
    cola.liberar(conn, jid, ahora=T0 + 6)  # no está corriendo: no cambia nada (ni resta intentos)
    assert estado(conn, jid)["intentos"] == 0 and estado(conn, jid)["run_after"] == T0 + 5
    cola.tomar(conn, ahora=T0 + 7)
    cola.completar(conn, jid)
    cola.liberar(conn, jid)
    assert estado(conn, jid)["estado"] == "hecho"


def test_cancelar_de_sesion(conn):
    a = cola.encolar(conn, "verificar", "s1")
    b = cola.encolar(conn, "transcribir", "s1", {"n": 1})
    c = cola.encolar(conn, "verificar", "s2")
    cola.tomar(conn, ["transcribir"], ahora=T0)
    assert cola.cancelar_de_sesion(conn, "s1") == 1  # sólo pendientes
    assert estado(conn, a)["estado"] == "cancelado"
    assert estado(conn, b)["estado"] == "corriendo"
    assert estado(conn, c)["estado"] == "pendiente"
    assert [j.id for j in cola.jobs_de_sesion(conn, "s1")] == [a, b]
