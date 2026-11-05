"""Facturación de Claude (§7): orden de medios por modo, memoria del estado (sin créditos, clave
cambiada, cupo del plan Max), `ejecutar` con respaldo, costos, la migración de la base v1 y la
ANTHROPIC_API_KEY que sale sólo del .env de la carpeta de datos.

Todo con relojes falsos y carpetas temporales: ninguna API real, ninguna clave real.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from classmer import cola
from classmer.config import (
    ESFUERZOS,
    FACTURACIONES,
    ConfigError,
    cargar_config,
    config_desde_texto,
    leer_env,
)
from classmer.db import VERSION_ESQUEMA, Base, abrir, inicializar
from classmer.ia import facturacion as fac
from classmer.ia.facturacion import (
    API,
    MAX,
    Facturacion,
    SinCreditos,
    costo_api,
    despertar_pospuestos,
    huella,
    texto_costo,
)
from classmer.pipeline import CuotaAgotada, ErrorEtapa

CLAVE = "sk-ant-falsa-123"
OTRA_CLAVE = "sk-ant-falsa-456"
T0 = 1_800_000_000.0   # 2027-01-15 05:00 en Buenos Aires (08:00 UTC): +6 h sigue siendo el mismo día
H = 3600.0
ZONA = "America/Argentina/Buenos_Aires"


class Reloj:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def avanzar(self, segundos: float) -> None:
        self.t += segundos


def configurar(home: Path, *, modo: str | None = None, clave: str | None = CLAVE,
               reintentar_h: float | None = None) -> object:
    """Reescribe la sección `claude:` y la ANTHROPIC_API_KEY del .env temporal y recarga la config."""
    datos = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    claude: dict = {}
    if modo is not None:
        claude["facturacion"] = modo
    if reintentar_h is not None:
        claude["reintentar_api_h"] = reintentar_h
    datos.pop("claude", None)
    if claude:
        datos["claude"] = claude
    (home / "config.yaml").write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")
    lineas = [l for l in (home / ".env").read_text(encoding="utf-8").splitlines()
              if not l.startswith("ANTHROPIC_API_KEY")]
    if clave:
        lineas.append(f"ANTHROPIC_API_KEY={clave}")
    (home / ".env").write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return cargar_config(home)


@pytest.fixture
def armar(home: Path, base: Base):
    """Fábrica de (Facturacion, reloj) con el modo y la clave pedidos, sobre la base temporal. Pasar
    el mismo reloj simula "la próxima tarea" (otro objeto Facturacion, misma base)."""

    def _armar(modo: str = "api_luego_max", clave: str | None = CLAVE, *,
               reintentar_h: float | None = None, reloj: Reloj | None = None):
        cfg = configurar(home, modo=modo, clave=clave, reintentar_h=reintentar_h)
        r = reloj or Reloj()
        return Facturacion(cfg, base, reloj=r), r

    return _armar


def filas_facturacion(base: Base) -> dict[str, dict]:
    with base.conexion() as conn:
        return {f["medio"]: dict(f) for f in conn.execute("SELECT * FROM facturacion")}


def sin_creditos(motivo: str = "credit balance is too low") -> SinCreditos:
    return SinCreditos(API, "sin_creditos", motivo)


class Correr:
    """`correr(medio)` falso: por medio, una lista de resultados o excepciones que va consumiendo."""

    def __init__(self, **guion):
        self.guion = {m: list(v) for m, v in guion.items()}
        self.llamadas: list[str] = []

    def __call__(self, medio: str):
        self.llamadas.append(medio)
        paso = self.guion[medio].pop(0) if self.guion.get(medio) else f"ok con {medio}"
        if isinstance(paso, BaseException):
            raise paso
        return paso


# --- orden de medios por modo ----------------------------------------------------------------------

@pytest.mark.parametrize(("modo", "con_clave", "esperado"), [
    ("api", True, [API]),
    ("max", True, [MAX]),
    ("api_luego_max", True, [API, MAX]),
    ("max_luego_api", True, [MAX, API]),
    # Sin clave, los modos con API funcionan como `max`.
    ("api", False, [MAX]),
    ("max", False, [MAX]),
    ("api_luego_max", False, [MAX]),
    ("max_luego_api", False, [MAX]),
])
def test_medios_del_modo(armar, modo, con_clave, esperado):
    f, _ = armar(modo, CLAVE if con_clave else None)
    assert f.modo == modo and f.hay_clave is con_clave
    assert f.medios_del_modo() == esperado
    medios, avisos = f.plan()
    assert medios == esperado


def test_todos_los_modos_de_la_config_tienen_orden(armar):
    for modo in FACTURACIONES:
        f, _ = armar(modo)
        assert set(f.medios_del_modo()) <= {API, MAX} and f.medios_del_modo()


def test_sin_clave_avisa_y_usa_el_plan_max(armar):
    f, _ = armar("api", None)
    estado = f.estado(API)
    assert estado.estado == "sin_clave" and not estado.disponible and not estado.ok
    assert f.describir(estado) == "sin clave"
    medios, avisos = f.plan()
    assert medios == [MAX]
    assert any("no hay ANTHROPIC_API_KEY" in a and "plan Max" in a for a in avisos)

    correr = Correr()
    resultado, medio = f.ejecutar("clasificar", correr)
    assert (resultado, medio) == ("ok con max", MAX) and correr.llamadas == [MAX]


def test_modo_max_sin_clave_no_avisa(armar):
    f, _ = armar("max", None)
    assert f.plan() == ([MAX], [])


def test_estados_por_defecto_ok(armar):
    f, _ = armar()
    estados = f.estados()
    assert set(estados) == {API, MAX}
    assert all(e.ok and e.disponible for e in estados.values())
    assert f.describir(estados[API]) == "ok"
    assert f.plan() == ([API, MAX], [])


# --- memoria: la API sin créditos ------------------------------------------------------------------

def test_sin_creditos_recordado_va_directo_al_otro_medio(armar, base):
    f, reloj = armar()
    f.marcar(sin_creditos())
    fila = filas_facturacion(base)[API]
    assert fila["estado"] == "sin_creditos" and fila["desde"] == T0 and fila["hasta"] is None
    assert fila["huella_clave"] == huella(CLAVE)

    # Otra tarea (otro objeto, misma base): va directo al plan Max y lo dice bien.
    g, _ = armar(reloj=reloj)
    e = g.estado(API)
    assert not e.disponible and e.hasta == T0 + 6 * H
    medios, avisos = g.plan()
    assert medios == [MAX]
    assert avisos == [
        "API sin créditos desde 05:00 (se vuelve a probar a las 11:00): voy directo al plan Max"]


def test_vuelve_a_probar_la_api_pasadas_las_horas(armar):
    f, reloj = armar()
    f.marcar(sin_creditos())
    reloj.avanzar(6 * H - 1)
    assert f.plan()[0] == [MAX]
    reloj.avanzar(1)
    medios, avisos = f.plan()
    assert medios == [API, MAX]
    assert any("ya pasó el tiempo de espera" in a for a in avisos)
    e = f.estado(API)
    assert e.disponible and e.estado == "sin_creditos"   # se vuelve a probar, pero sigue anotado


def test_reintentar_api_h_es_configurable(armar):
    f, reloj = armar(reintentar_h=1.5)
    f.marcar(sin_creditos())
    assert f.estado(API).hasta == T0 + 1.5 * H
    reloj.avanzar(1.5 * H - 1)
    assert f.plan()[0] == [MAX]
    reloj.avanzar(1)
    assert f.plan()[0] == [API, MAX]


def test_vuelve_a_probar_si_cambio_la_clave(armar, base):
    f, reloj = armar()
    f.marcar(SinCreditos(API, "clave_invalida", "invalid x-api-key"))
    assert armar(reloj=reloj)[0].plan()[0] == [MAX]

    # El usuario cambia la clave del .env: la huella guardada ya no coincide.
    g, _ = armar(clave=OTRA_CLAVE, reloj=reloj)
    e = g.estado(API)
    assert e.disponible and "la clave cambió" in (e.nota or "")
    medios, avisos = g.plan()
    assert medios == [API, MAX]
    assert any("la clave cambió" in a for a in avisos)

    # Si con la clave nueva vuelve a fallar, se recuerda con la huella nueva.
    g.marcar(sin_creditos())
    assert filas_facturacion(base)[API]["huella_clave"] == huella(OTRA_CLAVE)
    assert armar(clave=OTRA_CLAVE, reloj=reloj)[0].plan()[0] == [MAX]
    # Volver a la clave vieja también es "otra clave" respecto del último error.
    assert armar(clave=CLAVE, reloj=reloj)[0].plan()[0] == [API, MAX]


def test_error_sin_clave_no_traba_la_clave_nueva(armar, base):
    """Si la corrida por API falló sin clave (la sacaron entre el plan y la corrida), la fila queda sin
    huella: cuando el usuario pone una clave, se vuelve a probar enseguida, sin esperar las 6 h."""
    f, reloj = armar(clave=None)
    f.marcar(SinCreditos(API, "clave_invalida", "no hay ANTHROPIC_API_KEY en Classmer-data\\.env"))
    assert filas_facturacion(base)[API]["huella_clave"] is None
    g, _ = armar(clave=CLAVE, reloj=reloj)
    e = g.estado(API)
    assert e.disponible and "la clave cambió" in (e.nota or "")
    assert g.plan()[0] == [API, MAX]


def test_sacar_la_clave_despues_del_error_es_sin_clave(armar):
    f, reloj = armar()
    f.marcar(sin_creditos())
    g, _ = armar(clave=None, reloj=reloj)
    assert g.estado(API).estado == "sin_clave"
    assert g.plan()[0] == [MAX]


def test_la_base_no_guarda_la_clave(armar, base):
    f, _ = armar()
    f.marcar(SinCreditos(API, "clave_invalida", f"401 invalid x-api-key: {CLAVE}"))
    fila = filas_facturacion(base)[API]
    assert all(CLAVE not in str(v) for v in fila.values())
    assert "[ANTHROPIC_API_KEY]" in fila["motivo"]


# --- memoria: el plan Max sin cupo -----------------------------------------------------------------

def test_max_sin_cupo_recordado_hasta_que_se_libera(armar, base):
    f, reloj = armar("max_luego_api")
    f.marcar(SinCreditos(MAX, "sin_cupo", "usage limit reached", hasta=T0 + 2 * H))
    fila = filas_facturacion(base)[MAX]
    assert fila["hasta"] == T0 + 2 * H + fac.MARGEN_MAX_S and fila["huella_clave"] is None

    medios, avisos = f.plan()
    assert medios == [API]
    assert avisos == ["plan Max sin cupo desde 05:00 (se vuelve a probar a las 07:01): voy directo a la API"]
    reloj.avanzar(2 * H + fac.MARGEN_MAX_S - 1)
    assert f.plan()[0] == [API]
    reloj.avanzar(1)
    assert f.plan()[0] == [MAX, API]


@pytest.mark.parametrize(("hasta", "espera_s", "esperado"), [
    (None, 900, T0 + 900),          # sin dato de cuándo se libera: la espera del error
    (T0 - 10, 900, T0 + 900),       # un resets_at que ya pasó no sirve
    (None, 5, T0 + fac.ESPERA_MIN_S),   # nunca menos de un minuto
    (T0 + 3 * 86400, 900, T0 + 3 * 86400 + fac.MARGEN_MAX_S),  # límite semanal: el resets_at (+ margen)
])
def test_max_hasta_cuando(armar, base, hasta, espera_s, esperado):
    f, _ = armar("max")
    f.marcar(SinCreditos(MAX, "sin_login", "Not logged in", espera_s=espera_s, hasta=hasta))
    assert filas_facturacion(base)[MAX]["hasta"] == esperado
    assert not f.estado(MAX).disponible


def test_max_sin_hasta_en_la_base_no_queda_trabado(armar, base):
    f, reloj = armar("max")
    with base.conexion() as conn:
        conn.execute("INSERT INTO facturacion (medio, estado, motivo, desde, hasta) "
                     "VALUES ('max', 'sin_cupo', 'x', ?, NULL)", (T0,))
    assert not f.estado(MAX).disponible
    reloj.avanzar(fac.ESPERA_MAX_DEFAULT_S)
    assert f.estado(MAX).disponible


# --- Reintentar API --------------------------------------------------------------------------------

def _jobs_pospuestos(base: Base) -> dict[str, int]:
    """Un job por caso; devuelve {nombre: id}."""
    ids = {}
    with base.conexion() as conn:
        ids["apunte_pospuesto"] = cola.encolar(conn, "apunte", "s1", run_after=T0 + 5 * H, ahora=T0 - H)
        ids["clasificar_pospuesto"] = cola.encolar(conn, "clasificar", "s2", run_after=T0 + H, ahora=T0 - H)
        ids["transcribir_pospuesto"] = cola.encolar(conn, "transcribir", "s3", {"n": 1},
                                                    run_after=T0 + H, ahora=T0 - H)
        ids["apunte_listo"] = cola.encolar(conn, "apunte", "s4", run_after=T0 - 10, ahora=T0 - H)
        ids["apunte_con_error"] = cola.encolar(conn, "apunte", "s5", run_after=T0 + H, ahora=T0 - H)
        ids["clasificar_corriendo"] = cola.encolar(conn, "clasificar", "s6", run_after=T0 + H, ahora=T0 - H)
        conn.execute("UPDATE jobs SET estado = 'error' WHERE id = ?", (ids["apunte_con_error"],))
        conn.execute("UPDATE jobs SET estado = 'corriendo' WHERE id = ?", (ids["clasificar_corriendo"],))
    return ids


def _run_after(base: Base) -> dict[int, tuple[float, float]]:
    with base.conexion() as conn:
        return {f["id"]: (f["run_after"], f["actualizado"])
                for f in conn.execute("SELECT id, run_after, actualizado FROM jobs")}


def test_reintentar_api_borra_el_estado_y_adelanta_los_jobs_de_claude(armar, base):
    f, _ = armar()
    f.marcar(sin_creditos())
    f.marcar(SinCreditos(MAX, "sin_cupo", "usage limit reached", hasta=T0 + H))
    ids = _jobs_pospuestos(base)
    antes = _run_after(base)

    assert f.reintentar_api() == 2
    despues = _run_after(base)
    for nombre in ("apunte_pospuesto", "clasificar_pospuesto"):
        assert despues[ids[nombre]] == (T0, T0)
    for nombre in ("transcribir_pospuesto", "apunte_listo", "apunte_con_error", "clasificar_corriendo"):
        assert despues[ids[nombre]] == antes[ids[nombre]]

    filas = filas_facturacion(base)
    assert API not in filas and MAX in filas   # sólo olvida la API
    assert f.estado(API).ok and f.plan()[0] == [API]


def test_reintentar_api_sin_estado_igual_despierta(armar, base):
    f, _ = armar()
    _jobs_pospuestos(base)
    assert f.reintentar_api() == 2
    assert f.reintentar_api() == 0   # ya quedaron listos


def test_despertar_pospuestos(base):
    ids = _jobs_pospuestos(base)
    assert despertar_pospuestos(base, ahora=T0) == 2
    despues = _run_after(base)
    assert despues[ids["apunte_pospuesto"]][0] == T0 and despues[ids["clasificar_pospuesto"]][0] == T0
    assert despues[ids["transcribir_pospuesto"]][0] == T0 + H


# --- ejecutar ---------------------------------------------------------------------------------------

def test_ejecutar_cambia_de_medio_ante_sin_creditos_y_lo_recuerda(armar, base):
    f, reloj = armar()
    avisos: list[str] = []
    correr = Correr(api=[sin_creditos("Your credit balance is too low")], max=["apunte hecho"])
    resultado, medio = f.ejecutar("apunte", correr, log_tarea=avisos.append)
    assert (resultado, medio) == ("apunte hecho", MAX)
    assert correr.llamadas == [API, MAX]   # rehace el paso entero con el otro medio
    assert avisos == ["API sin créditos (Your credit balance is too low): sigo con el plan Max"]
    assert filas_facturacion(base)[API]["estado"] == "sin_creditos"

    # La tarea siguiente va directo al plan Max, sin llamar con la API.
    g, _ = armar(reloj=reloj)
    avisos.clear()
    correr = Correr()
    assert g.ejecutar("clasificar", correr, log_tarea=avisos.append) == ("ok con max", MAX)
    assert correr.llamadas == [MAX]
    assert len(avisos) == 1 and avisos[0].endswith("voy directo al plan Max")
    assert API in filas_facturacion(base)   # el éxito con Max no borra el estado de la API


def test_ejecutar_clave_invalida_tambien_cambia(armar, base):
    f, _ = armar()
    avisos: list[str] = []
    correr = Correr(api=[SinCreditos(API, "clave_invalida", "invalid x-api-key")])
    assert f.ejecutar("clasificar", correr, log_tarea=avisos.append) == ("ok con max", MAX)
    assert avisos == ["la API key no es válida (invalid x-api-key): sigo con el plan Max"]
    assert filas_facturacion(base)[API]["estado"] == "clave_invalida"


def test_ejecutar_max_luego_api(armar, base):
    f, _ = armar("max_luego_api")
    avisos: list[str] = []
    correr = Correr(max=[SinCreditos(MAX, "sin_cupo", "usage limit reached", hasta=T0 + H)])
    assert f.ejecutar("apunte", correr, log_tarea=avisos.append) == ("ok con api", API)
    assert correr.llamadas == [MAX, API]
    assert avisos == ["plan Max sin cupo (usage limit reached): sigo con la API"]
    assert filas_facturacion(base)[MAX]["hasta"] == T0 + H + fac.MARGEN_MAX_S


@pytest.mark.parametrize("error", [
    CuotaAgotada("429 rate_limit_error: too many requests", espera_s=120),
    CuotaAgotada("529 overloaded_error", espera_s=180),
])
def test_ejecutar_no_cambia_ante_un_error_pasajero(armar, base, error):
    f, _ = armar()
    correr = Correr(api=[error])
    with pytest.raises(CuotaAgotada) as exc:
        f.ejecutar("clasificar", correr)
    assert exc.value is error and not isinstance(exc.value, SinCreditos)
    assert correr.llamadas == [API]          # no probó con el plan Max
    assert filas_facturacion(base) == {}     # ni marcó nada


def test_ejecutar_otros_errores_se_propagan_sin_marcar(armar, base):
    f, _ = armar()
    error = ErrorEtapa("el modelo no existe", reintentable=False)
    correr = Correr(api=[error])
    with pytest.raises(ErrorEtapa) as exc:
        f.ejecutar("clasificar", correr)
    assert exc.value is error and correr.llamadas == [API]
    assert filas_facturacion(base) == {}


def test_ejecutar_exito_borra_el_estado(armar, base):
    f, reloj = armar()
    f.marcar(sin_creditos())
    reloj.avanzar(6 * H)
    avisos: list[str] = []
    correr = Correr()
    assert f.ejecutar("apunte", correr, log_tarea=avisos.append) == ("ok con api", API)
    assert correr.llamadas == [API]
    assert any("se vuelve a probar" in a for a in avisos)
    assert filas_facturacion(base) == {}
    assert f.estado(API).ok


def test_ejecutar_vuelve_a_fallar_despues_de_reintentar(armar, base):
    f, reloj = armar()
    f.marcar(sin_creditos())
    reloj.avanzar(7 * H)
    correr = Correr(api=[sin_creditos()])
    assert f.ejecutar("apunte", correr)[1] == MAX
    assert filas_facturacion(base)[API]["desde"] == T0 + 7 * H   # el reloj de espera arranca de nuevo
    assert f.estado(API).hasta == T0 + 13 * H


def test_ejecutar_sin_medios_disponibles(armar):
    f, reloj = armar("api")
    f.marcar(sin_creditos())
    reloj.avanzar(H)
    correr = Correr()
    with pytest.raises(CuotaAgotada) as exc:
        f.ejecutar("clasificar", correr)
    assert correr.llamadas == []
    assert not isinstance(exc.value, SinCreditos)
    assert exc.value.espera_s == pytest.approx(5 * H)   # hasta que se vuelve a probar la API
    assert "ningún medio" in str(exc.value) and "API sin créditos" in str(exc.value)


def test_ejecutar_falla_el_ultimo_medio(armar, base):
    f, _ = armar()
    correr = Correr(api=[sin_creditos()],
                    max=[SinCreditos(MAX, "sin_cupo", "usage limit reached", hasta=T0 + 1200)])
    with pytest.raises(CuotaAgotada) as exc:
        f.ejecutar("apunte", correr)
    assert correr.llamadas == [API, MAX]
    assert not isinstance(exc.value, SinCreditos)
    assert exc.value.espera_s == pytest.approx(1200 + fac.MARGEN_MAX_S)   # lo primero que se libera: el cupo de Max
    assert "no queda otro medio" in str(exc.value)
    assert set(filas_facturacion(base)) == {API, MAX}


def test_ejecutar_un_solo_medio_que_falla(armar):
    f, _ = armar("max")
    with pytest.raises(CuotaAgotada) as exc:
        f.ejecutar("apunte", Correr(max=[SinCreditos(MAX, "sin_login", "Not logged in", espera_s=900)]))
    assert "no queda otro medio" not in str(exc.value)
    assert "sin sesión iniciada" in str(exc.value)
    assert exc.value.espera_s == pytest.approx(900)


@pytest.mark.parametrize(("hasta", "esperado"), [
    (T0 + 3 * 86400, fac.ESPERA_TOPE_S),   # nunca más de 6 h de una vez
    (T0 + 10, 10 + fac.MARGEN_MAX_S),      # un reset cercano: hasta el reset y el margen
])
def test_espera_acotada(armar, hasta, esperado):
    f, _ = armar("max")
    f.marcar(SinCreditos(MAX, "sin_cupo", "weekly limit", hasta=hasta))
    with pytest.raises(CuotaAgotada) as exc:
        f.ejecutar("apunte", Correr())
    assert exc.value.espera_s == esperado


def test_espera_con_un_medio_disponible_es_corta(armar):
    f, _ = armar()
    f.marcar(sin_creditos())
    assert f.espera() == fac.ESPERA_MIN_S   # el plan Max está libre


def test_ejecutar_nunca_deja_la_clave_en_los_avisos(armar):
    f, _ = armar()
    avisos: list[str] = []
    correr = Correr(api=[SinCreditos(API, "clave_invalida", f"invalid x-api-key {CLAVE}")])
    f.ejecutar("clasificar", correr, log_tarea=avisos.append)
    assert avisos and all(CLAVE not in a for a in avisos)

    g, _ = armar("api")
    with pytest.raises(CuotaAgotada) as exc:
        g.ejecutar("clasificar", Correr(api=[SinCreditos(API, "clave_invalida", f"bad key {CLAVE}")]))
    assert CLAVE not in str(exc.value)


# --- sin base inicializada ---------------------------------------------------------------------------

def test_sin_tabla_no_rompe(tmp_path):
    """El clasificador puede crear Facturacion(cfg) sin base: si la tabla no existe, no hay memoria."""
    home = tmp_path / "sin-base"
    home.mkdir()
    (home / ".env").write_text(f"ANTHROPIC_API_KEY={CLAVE}\n", encoding="utf-8")
    cfg = config_desde_texto(home, "")
    f = Facturacion(cfg, reloj=Reloj())
    assert f.estado(API).ok and f.plan() == ([API, MAX], [])
    f.marcar_ok(API)
    f.marcar(sin_creditos())   # no puede recordar, pero no corta la tarea
    correr = Correr(api=[sin_creditos()])
    assert f.ejecutar("clasificar", correr) == ("ok con max", MAX)


def test_sin_base_explicita_usa_la_de_la_config(armar, home, base):
    f, reloj = armar()
    f.marcar(sin_creditos())
    g = Facturacion(cargar_config(home), reloj=reloj)
    assert g.base.ruta == base.ruta
    assert g.plan()[0] == [MAX]


# --- SinCreditos, huella, describir ----------------------------------------------------------------

def test_sin_creditos_es_una_cuota_agotada():
    e = SinCreditos(API, "sin_creditos", "credit balance is too low", espera_s=60)
    assert isinstance(e, CuotaAgotada) and isinstance(e, ErrorEtapa) and e.reintentable
    assert (e.medio, e.estado, e.motivo, e.espera_s, e.hasta) == (API, "sin_creditos",
                                                                  "credit balance is too low", 60, None)
    assert str(e) == "API sin créditos: credit balance is too low"
    assert SinCreditos(MAX, "sin_cupo", "x").espera_s == fac.ESPERA_MAX_DEFAULT_S
    with pytest.raises(ValueError):
        SinCreditos("gratis", "sin_creditos", "x")


def test_huella():
    h = huella(CLAVE)
    assert re.fullmatch(r"[0-9a-f]{8}", h)
    assert h == hashlib.sha256(CLAVE.encode()).hexdigest()[:8]
    assert huella(CLAVE) == h and huella(OTRA_CLAVE) != h
    assert h not in CLAVE and CLAVE not in h
    assert huella(None) is None and huella("") is None


def test_describir(armar):
    f, reloj = armar()
    assert f.describir(f.estado(API)) == "ok"
    f.marcar(sin_creditos())
    assert f.describir(f.estado(API)) == "API sin créditos desde 05:00 (se vuelve a probar a las 11:00)"
    # Otro día: con fecha.
    reloj.avanzar(2 * 86400)
    texto = f.describir(f.estado(API))
    assert texto.startswith("API sin créditos desde 15/01 05:00")
    assert "se vuelve a probar" in texto


def test_hora_local_de_la_config():
    dt = datetime.fromtimestamp(T0, ZoneInfo(ZONA))
    assert (dt.hour, dt.minute) == (5, 0)
    assert fac._hora(T0, ZONA, T0) == "05:00"
    assert fac._hora(T0, ZONA, T0 + 86400) == "15/01 05:00"
    assert fac._hora(None, ZONA) == "?"


# --- costo de la API --------------------------------------------------------------------------------

MTOK = 1_000_000


@pytest.mark.parametrize(("modelo", "entrada", "salida"), [
    # Precios de la skill claude-api (tabla "Current Models", cacheada el 2026-10-06).
    ("claude-opus-5-5", 4.00, 20.00),
    ("claude-sonnet-5-5", 2.00, 10.00),
    ("claude-haiku-5-5", 0.10, 0.50),
    ("claude-fable-5-1", 10.00, 50.00),
    ("claude-opus-4-8", 5.00, 25.00),
    ("claude-sonnet-4-6", 3.00, 15.00),
    ("claude-haiku-4-5", 1.00, 5.00),
])
def test_costo_api_precios(modelo, entrada, salida):
    # 100K tokens de entrada: no pasa el umbral de prompt largo de Haiku 5.5.
    assert costo_api(modelo, {"entrada": 100_000}) == pytest.approx(entrada / 10)
    assert costo_api(modelo, {"salida": MTOK}) == pytest.approx(salida)


def test_costo_api_usa_la_tabla():
    for modelo, (p_in, p_out) in fac.PRECIOS_USD_MTOK.items():
        assert costo_api(modelo, {"entrada": 50_000, "salida": MTOK}) == pytest.approx(p_in / 20 + p_out)


def test_costo_api_clasificacion_tipica():
    # Sonnet 5.5: 12.345 tokens de entrada y 87 de salida.
    assert costo_api("claude-sonnet-5-5", {"entrada": 12_345, "salida": 87}) == pytest.approx(
        (12_345 * 2.00 + 87 * 10.00) / MTOK)


def test_costo_api_cache():
    # Escritura (5 min) a 1,25x la entrada; lectura a 0,1x, salvo Opus 5.5 (US$ 0,20) y Fable 5.1 (US$ 0,25).
    assert costo_api("claude-sonnet-5-5", {"cache_escritura": MTOK}) == pytest.approx(2.50)
    assert costo_api("claude-sonnet-5-5", {"cache_lectura": MTOK}) == pytest.approx(0.20)
    assert costo_api("claude-opus-4-8", {"cache_lectura": MTOK}) == pytest.approx(0.50)
    assert costo_api("claude-opus-5-5", {"cache_lectura": MTOK}) == pytest.approx(0.20)
    assert costo_api("claude-fable-5-1", {"cache_lectura": MTOK}) == pytest.approx(0.25)
    assert costo_api("claude-opus-5-5", {"cache_escritura": MTOK}) == pytest.approx(5.00)
    uso = {"entrada": 1_000, "salida": 2_000, "cache_escritura": 3_000, "cache_lectura": 4_000}
    assert costo_api("claude-sonnet-5-5", uso) == pytest.approx(
        (1_000 * 2 + 3_000 * 2.5 + 4_000 * 0.2 + 2_000 * 10) / MTOK)


def test_costo_api_haiku_largo():
    # Hasta 100K tokens de prompt: 0,10/0,50; más: 0,50/2,50 (cuenta también la caché).
    assert costo_api("claude-haiku-5-5", {"entrada": 100_000}) == pytest.approx(0.01)
    assert costo_api("claude-haiku-5-5", {"entrada": 150_000, "salida": 10_000}) == pytest.approx(
        (150_000 * 0.50 + 10_000 * 2.50) / MTOK)
    assert costo_api("claude-haiku-5-5", {"entrada": 50_000, "cache_lectura": 60_000}) == pytest.approx(
        (50_000 * 0.50 + 60_000 * 0.05) / MTOK)
    assert costo_api("claude-haiku-5-5", {"entrada": 50_000, "cache_lectura": 50_000}) == pytest.approx(
        (50_000 * 0.10 + 50_000 * 0.01) / MTOK)


def test_costo_api_casos_borde():
    assert costo_api("claude-desconocido-9", {"entrada": MTOK}) is None
    assert costo_api("gemini-flash-latest", {"entrada": MTOK}) is None
    assert costo_api(None, {"entrada": MTOK}) is None
    assert costo_api("claude-sonnet-5-5", {}) == 0.0
    assert costo_api("claude-sonnet-5-5", {"entrada": None, "salida": 1_000}) == pytest.approx(0.01)


@pytest.mark.parametrize(("costo", "medio", "esperado"), [
    (0.0123, "api", "US$ 0.0123 (API)"),
    (0.1, "api", "US$ 0.10 (API)"),
    (1.8, "max", "US$ 1.80 equivalente API (plan Max, no se cobra aparte)"),
    (0.004, "max", "US$ 0.0040 equivalente API (plan Max, no se cobra aparte)"),
    (None, "api", "sin costo informado (API)"),
    (None, "max", "sin costo informado (plan Max)"),
    (None, None, "sin costo informado (?)"),
    (0.5, None, "US$ 0.50 (?)"),
])
def test_texto_costo(costo, medio, esperado):
    assert texto_costo(costo, medio) == esperado


# --- migración de una base v1 ----------------------------------------------------------------------

# Las tablas que cambiaron en la v2, tal como eran en la v1 (más sesiones y materias, por las claves
# foráneas). Las demás tablas de la v1 son iguales en la v2.
ESQUEMA_V1 = """
CREATE TABLE materias (
    id TEXT PRIMARY KEY, nombre TEXT NOT NULL, carpeta TEXT, perfil TEXT, carpeta_clases TEXT,
    regla_calendario TEXT, tipos_clase TEXT, estilo TEXT, activa INTEGER NOT NULL DEFAULT 1,
    actualizado TEXT NOT NULL
);
CREATE TABLE sesiones (
    id TEXT PRIMARY KEY, materia_id TEXT REFERENCES materias(id), tipo_clase TEXT, inicio TEXT NOT NULL,
    duracion_ms INTEGER, origen_materia TEXT, confianza REAL, estado TEXT NOT NULL DEFAULT 'grabando',
    etapa TEXT, error TEXT, dispositivo TEXT, sha256 TEXT, bytes_totales INTEGER, eventos_totales INTEGER,
    numero_clase INTEGER, titulo TEXT, ruta_publicada TEXT, creado TEXT NOT NULL, actualizado TEXT NOT NULL,
    finalizado TEXT
);
CREATE TABLE apuntes (
    id INTEGER PRIMARY KEY, sesion_id TEXT NOT NULL REFERENCES sesiones(id), version INTEGER NOT NULL,
    actual INTEGER NOT NULL DEFAULT 0, motor TEXT, modelo TEXT, prompt_version TEXT, ruta_tex TEXT,
    ruta_pdf TEXT, compilo INTEGER NOT NULL DEFAULT 0, titulo TEXT, resumen TEXT, costo REAL,
    creado TEXT NOT NULL, UNIQUE (sesion_id, version)
);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY, tipo TEXT NOT NULL, sesion_id TEXT, args TEXT, clave TEXT,
    estado TEXT NOT NULL DEFAULT 'pendiente', prioridad INTEGER NOT NULL DEFAULT 100,
    intentos INTEGER NOT NULL DEFAULT 0, max_intentos INTEGER NOT NULL DEFAULT 3,
    run_after REAL NOT NULL DEFAULT 0, lease_hasta REAL, motor TEXT, error TEXT, log TEXT, costo REAL,
    creado REAL NOT NULL, actualizado REAL NOT NULL
);
CREATE INDEX jobs_listos ON jobs(estado, run_after, prioridad, creado);
CREATE INDEX jobs_por_sesion ON jobs(sesion_id, tipo);
CREATE UNIQUE INDEX jobs_clave_activa
    ON jobs(clave) WHERE clave IS NOT NULL AND estado IN ('pendiente', 'corriendo');
INSERT INTO materias VALUES ('rf', 'RF', NULL, NULL, 'Clases-Classmer', '{}', '[]', NULL, 1, 'x');
INSERT INTO sesiones (id, materia_id, inicio, estado, creado, actualizado)
    VALUES ('01a13fff', 'rf', '2026-10-15T09:00:00-03:00', 'lista', 'x', 'x');
INSERT INTO apuntes (sesion_id, version, actual, motor, modelo, compilo, titulo, costo, creado)
    VALUES ('01a13fff', 1, 0, 'claude-agent', 'claude-opus-5-5', 1, 'Clase 1', 1.25, 'x'),
           ('01a13fff', 2, 1, 'claude-agent', 'claude-opus-5-5', 1, 'Clase 1', 1.50, 'x');
INSERT INTO jobs (tipo, sesion_id, estado, costo, creado, actualizado)
    VALUES ('apunte', '01a13fff', 'hecho', 1.50, 1, 1), ('publicar', '01a13fff', 'pendiente', NULL, 1, 1);
PRAGMA user_version = 1;
"""


def _base_v1(ruta: Path) -> None:
    conn = sqlite3.connect(ruta)
    try:
        conn.executescript(ESQUEMA_V1)
    finally:
        conn.close()


def _columnas(conn: sqlite3.Connection, tabla: str) -> set[str]:
    return {c[1] for c in conn.execute(f'PRAGMA table_info("{tabla}")')}


def _tablas(conn: sqlite3.Connection) -> set[str]:
    return {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_migracion_de_una_base_v1(tmp_path):
    ruta = tmp_path / "classmer.db"
    _base_v1(ruta)
    with sqlite3.connect(ruta) as conn:
        assert "esfuerzo" not in _columnas(conn, "apuntes") and "facturacion" not in _columnas(conn, "jobs")

    inicializar(ruta)
    inicializar(ruta)   # idempotente

    conn = sqlite3.connect(ruta)
    conn.row_factory = sqlite3.Row
    try:
        assert {"esfuerzo", "facturacion"} <= _columnas(conn, "apuntes")
        assert "facturacion" in _columnas(conn, "jobs")
        assert {"publicaciones", "facturacion", "modelos", "descubrimientos"} <= _tablas(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == VERSION_ESQUEMA  # sin número: hay más migraciones
        assert "adjuntos" in _columnas(conn, "apuntes")  # la de la v3 también, desde una v1
        # Las filas viejas siguen ahí, con las columnas nuevas vacías.
        apuntes = conn.execute(
            "SELECT version, actual, costo, esfuerzo, facturacion FROM apuntes ORDER BY version").fetchall()
        assert [tuple(a) for a in apuntes] == [(1, 0, 1.25, None, None), (2, 1, 1.50, None, None)]
        jobs = conn.execute("SELECT tipo, estado, costo, facturacion FROM jobs ORDER BY id").fetchall()
        assert [tuple(j) for j in jobs] == [
            ("apunte", "hecho", 1.50, None), ("publicar", "pendiente", None, None)]
        assert conn.execute("SELECT titulo FROM apuntes WHERE actual = 1").fetchone()[0] == "Clase 1"
    finally:
        conn.close()

    # Y la base migrada sirve para lo nuevo.
    base = Base(ruta)
    with base.conexion() as c:
        jid = cola.encolar(c, "clasificar", "01a13fff", ahora=T0)
        cola.completar(c, jid, costo=0.01, facturacion="api", ahora=T0)
        c.execute("UPDATE apuntes SET esfuerzo = 'high', facturacion = 'max' WHERE version = 2")
        assert c.execute("SELECT facturacion FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == "api"
        assert tuple(c.execute("SELECT esfuerzo, facturacion FROM apuntes WHERE version = 2").fetchone()) == (
            "high", "max")


def test_abrir_migra_la_base_vieja(home):
    cfg = cargar_config(home)
    cfg.datos.mkdir(parents=True, exist_ok=True)
    _base_v1(cfg.db_path)
    base = abrir(cfg)
    with base.conexion() as conn:
        assert "facturacion" in _columnas(conn, "jobs") and "esfuerzo" in _columnas(conn, "apuntes")
        assert conn.execute("SELECT COUNT(*) FROM apuntes").fetchone()[0] == 2
        # 'rf' no está en la config de prueba: queda inactiva, pero no se borra.
        assert conn.execute("SELECT activa FROM materias WHERE id = 'rf'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM materias WHERE id = 'prueba'").fetchone()[0] == 1
    cfg2 = configurar(home)
    f = Facturacion(cfg2, base, reloj=Reloj())
    f.marcar(sin_creditos())
    assert f.plan()[0] == [MAX]


# --- la clave: sólo del .env de la carpeta de datos ------------------------------------------------

def _env(home: Path, texto: str) -> None:
    (home / ".env").write_bytes(texto.encode("utf-8"))


def test_la_clave_sale_del_env(home, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _env(home, f"CLASSMER_TOKEN=x\nANTHROPIC_API_KEY={CLAVE}\n")
    cfg = cargar_config(home)
    assert cfg.anthropic_api_key == CLAVE
    assert leer_env(home)["ANTHROPIC_API_KEY"] == CLAVE
    assert "ANTHROPIC_API_KEY" not in os.environ   # nunca se exporta


def test_la_clave_no_sale_del_entorno_del_proceso(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", OTRA_CLAVE)   # p. ej. la de una sesión de Claude Code
    _env(home, "CLASSMER_TOKEN=x\n")
    assert cargar_config(home).anthropic_api_key is None
    assert "ANTHROPIC_API_KEY" not in leer_env(home)
    _env(home, f"CLASSMER_TOKEN=x\nANTHROPIC_API_KEY={CLAVE}\n")
    assert cargar_config(home).anthropic_api_key == CLAVE   # gana el .env, no el entorno
    assert os.environ["ANTHROPIC_API_KEY"] == OTRA_CLAVE    # y el entorno no se toca


def test_token_y_gemini_si_salen_del_entorno(home, monkeypatch):
    _env(home, "CLASSMER_TOKEN=del-env\nGEMINI_API_KEY=gemini-del-env\n")
    monkeypatch.setenv("CLASSMER_TOKEN", "del-proceso")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-del-proceso")
    cfg = cargar_config(home)
    assert cfg.token == "del-proceso" and cfg.gemini_api_key == "gemini-del-proceso"


@pytest.mark.parametrize("linea", [
    f"ANTHROPIC_API_KEY={CLAVE}",
    f"ANTHROPIC_API_KEY = {CLAVE}   ",
    f'ANTHROPIC_API_KEY="{CLAVE}"',
    f"ANTHROPIC_API_KEY='{CLAVE}'",
    f"export ANTHROPIC_API_KEY={CLAVE}",
    f"ANTHROPIC_API_KEY={CLAVE} # la de Classmer",
])
@pytest.mark.parametrize("fin", ["\n", "\r\n"])
def test_formatos_del_env(home, monkeypatch, linea, fin):
    """La clave en la línea 11 de un .env con otras cosas, como lo dejó el usuario (con o sin CRLF/BOM)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    otras = ["# Classmer", "CLASSMER_TOKEN=x", "GEMINI_API_KEY=g", ""] + [f"# nota {i}" for i in range(6)]
    texto = fin.join([*otras, linea, "OTRA=1"]) + fin
    assert texto.split(fin)[10] == linea
    _env(home, "﻿" + texto)
    assert cargar_config(home).anthropic_api_key == CLAVE


def test_clave_vacia_es_sin_clave(home):
    _env(home, "ANTHROPIC_API_KEY=\n")
    assert cargar_config(home).anthropic_api_key is None
    _env(home, "ANTHROPIC_API_KEY=   \n")
    assert cargar_config(home).anthropic_api_key is None


def test_repr_no_muestra_las_claves(home):
    _env(home, f"CLASSMER_TOKEN=token-secreto\nGEMINI_API_KEY=gemini-secreta\nANTHROPIC_API_KEY={CLAVE}\n")
    cfg = cargar_config(home)
    for texto in (repr(cfg), str(cfg)):
        assert CLAVE not in texto and "gemini-secreta" not in texto and "token-secreto" not in texto
    assert "anthropic_api_key" not in repr(cfg)


# --- validación de la config -----------------------------------------------------------------------

def _yaml(home: Path, **secciones) -> str:
    datos = {"materias": [{"id": "prueba", "nombre": "Prueba", "carpeta": str(home / "m")}]}
    datos.update(secciones)
    return yaml.safe_dump(datos, allow_unicode=True)


def test_valores_por_defecto(home):
    cfg = config_desde_texto(home, _yaml(home))
    assert (cfg.clasificar.motor, cfg.clasificar.modelo, cfg.clasificar.esfuerzo) == (
        "claude", "claude-sonnet-5-5", "medium")
    assert cfg.clasificar.umbral_confianza == 0.75
    assert (cfg.apunte.modelo, cfg.apunte.esfuerzo) == ("claude-opus-5-5", "high")
    assert (cfg.claude.facturacion, cfg.claude.reintentar_api_h) == ("api_luego_max", 6)
    assert cfg.materias[0].apunte is None


@pytest.mark.parametrize("secciones", [
    {"clasificar": {"esfuerzo": "altisimo"}},
    {"clasificar": {"motor": "openai"}},
    {"apunte": {"esfuerzo": "ultra"}},
    {"apunte": {"esfuerzo": "HIGH"}},
    {"claude": {"facturacion": "gratis"}},
    {"claude": {"facturacion": "api-luego-max"}},
    {"claude": {"reintentar_api_h": 0}},
    {"claude": {"reintentar_api_h": -1}},
])
def test_valores_invalidos(home, secciones):
    with pytest.raises(ConfigError):
        config_desde_texto(home, _yaml(home, **secciones))


@pytest.mark.parametrize("apunte", [
    {"modelo": "claude-opus-5-5", "esfuerzo": "maximo"},
    {"modelo": "", "esfuerzo": "max"},
    {"modelo": "   "},
    {"esfuerzo": "max"},
])
def test_apunte_de_materia_invalido(home, apunte):
    datos = yaml.safe_load(_yaml(home))
    datos["materias"][0]["apunte"] = apunte
    with pytest.raises(ConfigError):
        config_desde_texto(home, yaml.safe_dump(datos))


def test_mensajes_de_validacion(home):
    with pytest.raises(ConfigError, match="claude.facturacion"):
        config_desde_texto(home, _yaml(home, claude={"facturacion": "gratis"}))
    with pytest.raises(ConfigError, match="esfuerzo inválido"):
        config_desde_texto(home, _yaml(home, apunte={"esfuerzo": "ultra"}))
    with pytest.raises(ConfigError, match="clasificar.motor"):
        config_desde_texto(home, _yaml(home, clasificar={"motor": "openai"}))


def test_valores_validos(home):
    def cargar(**secciones):
        return config_desde_texto(home, _yaml(home, **secciones))

    for esfuerzo in ESFUERZOS:
        cfg = cargar(apunte={"esfuerzo": esfuerzo}, clasificar={"esfuerzo": esfuerzo})
        assert cfg.apunte.esfuerzo == esfuerzo and cfg.clasificar.esfuerzo == esfuerzo
    for modo in FACTURACIONES:
        assert cargar(claude={"facturacion": modo}).claude.facturacion == modo
    for vacio in ("", None):
        cfg = cargar(apunte={"esfuerzo": vacio}, clasificar={"esfuerzo": vacio})
        assert cfg.apunte.esfuerzo is None and cfg.clasificar.esfuerzo is None
    cfg = cargar(clasificar={"motor": "gemini", "modelo": "gemini-flash-latest"},
                 claude={"reintentar_api_h": 0.5})
    assert cfg.clasificar.motor == "gemini" and cfg.claude.reintentar_api_h == 0.5


# --- modelo del apunte por materia -----------------------------------------------------------------

def _cfg_materias(home: Path, **global_apunte):
    datos = {
        "apunte": global_apunte or {"modelo": "claude-opus-5-5", "esfuerzo": "high"},
        "materias": [
            {"id": "real", "nombre": "Análisis Matemático", "carpeta": str(home / "real"),
             "apunte": {"modelo": "claude-opus-5-5", "esfuerzo": "max"}},
            {"id": "historia", "nombre": "Historia", "carpeta": str(home / "historia"),
             "apunte": {"modelo": "claude-sonnet-5-5"}},
            {"id": "rf", "nombre": "RF", "carpeta": str(home / "rf")},
            {"id": "nula", "nombre": "Nula", "carpeta": str(home / "nula"), "apunte": None},
            {"id": "vacio", "nombre": "Vacío", "carpeta": str(home / "vacio"),
             "apunte": {"modelo": " claude-haiku-5-5 ", "esfuerzo": ""}},
        ],
    }
    return config_desde_texto(home, yaml.safe_dump(datos, allow_unicode=True))


def test_modelo_apunte_por_materia(home):
    cfg = _cfg_materias(home)
    assert cfg.modelo_apunte(cfg.materia("real")) == ("claude-opus-5-5", "max", "materia")
    # Esfuerzo propio vacío: hereda el global.
    assert cfg.modelo_apunte(cfg.materia("historia")) == ("claude-sonnet-5-5", "high", "materia")
    assert cfg.modelo_apunte(cfg.materia("vacio")) == ("claude-haiku-5-5", "high", "materia")
    # Sin `apunte` (o null): el global.
    assert cfg.modelo_apunte(cfg.materia("rf")) == ("claude-opus-5-5", "high", "global")
    assert cfg.modelo_apunte(cfg.materia("nula")) == ("claude-opus-5-5", "high", "global")
    assert cfg.modelo_apunte(None) == ("claude-opus-5-5", "high", "global")


def test_modelo_apunte_sigue_al_global(home):
    cfg = _cfg_materias(home, modelo="claude-fable-5-1", esfuerzo="xhigh")
    assert cfg.modelo_apunte(cfg.materia("rf")) == ("claude-fable-5-1", "xhigh", "global")
    assert cfg.modelo_apunte(cfg.materia("historia")) == ("claude-sonnet-5-5", "xhigh", "materia")
    assert cfg.modelo_apunte(cfg.materia("real")) == ("claude-opus-5-5", "max", "materia")


def test_env_en_utf16_da_un_error_claro(home):
    (home / ".env").write_bytes(f"CLASSMER_TOKEN=x\nANTHROPIC_API_KEY={CLAVE}\n".encode("utf-16"))
    with pytest.raises(ConfigError) as info:
        cargar_config(home)
    assert "UTF-8" in str(info.value) and CLAVE not in str(info.value)


def test_se_recuerda_la_clave_que_fallo_no_la_que_hay_despues(armar, base):
    # La corrida arranca con CLAVE; mientras corre, el usuario pone OTRA_CLAVE en el .env (la config viva
    # la aplica sobre el mismo objeto). Falla por créditos: lo que queda recordado es CLAVE.
    f, _ = armar("api")

    def correr(medio):
        f.cfg.anthropic_api_key = OTRA_CLAVE
        raise sin_creditos()

    with pytest.raises(CuotaAgotada):
        f.ejecutar("apunte", correr)
    assert filas_facturacion(base)[API]["huella_clave"] == huella(CLAVE)
    assert f.estado(API).disponible   # la clave nueva nunca falló: se prueba
    assert f.plan()[0] == [API]


def test_cambiar_la_clave_en_el_env_adelanta_los_jobs_pospuestos(home, base, monkeypatch):
    from classmer import cola
    from classmer.config_vivo import ConfigViva

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _env(home, f"CLASSMER_TOKEN=x\nANTHROPIC_API_KEY={CLAVE}\n")
    cfg = cargar_config(home)
    viva = ConfigViva(cfg, base)
    lejos = 4_000_000_000.0
    with base.conexion() as conn:
        apunte = cola.encolar(conn, "apunte", None)
        transcribir = cola.encolar(conn, "transcribir", None, {"n": 1})
        conn.execute("UPDATE jobs SET run_after = ?", (lejos,))
    _env(home, f"CLASSMER_TOKEN=x\nANTHROPIC_API_KEY={OTRA_CLAVE}\n")
    assert viva.recargar(forzar=True)
    assert cfg.anthropic_api_key == OTRA_CLAVE
    with base.conexion() as conn:
        run_after = {f["id"]: f["run_after"] for f in conn.execute("SELECT id, run_after FROM jobs")}
    assert run_after[apunte] < lejos and run_after[transcribir] == lejos   # sólo los de Claude
    # Recargar sin cambiar la clave no adelanta nada.
    with base.conexion() as conn:
        conn.execute("UPDATE jobs SET run_after = ?", (lejos,))
    assert viva.recargar(forzar=True)
    with base.conexion() as conn:
        assert {f["run_after"] for f in conn.execute("SELECT run_after FROM jobs")} == {lejos}
