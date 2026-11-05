"""Catálogo de modelos: semillas, filtros por tarea, "Redescubrir modelos" con APIs falsas (paginación,
falta de una clave, errores) y el modelo elegido que desaparece. Nunca se llama a una API real."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import ModelInfo
from anthropic.types.capability_support import CapabilitySupport
from anthropic.types.effort_capability import EffortCapability
from anthropic.types.model_capabilities import ModelCapabilities
from google.genai import errors as genai_errors
from google.genai import pagers
from google.genai import types as genai_types

from classmer.config import ESFUERZOS, Config, cargar_config
from classmer.db import Base, abrir
from classmer.ia import modelos

CLAVE_ANTHROPIC = "sk-ant-falsa-123"
CLAVE_GEMINI = "gemini-falsa-456"
SEMILLAS_ANTHROPIC = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"}
SEMILLAS_GOOGLE = {"gemini-3.5-transcribe", "gemini-flash-latest"}

# Los originales, antes de que el fixture los reemplace (para probar cómo se arma el cliente real).
CLIENTE_ANTHROPIC_REAL = modelos._cliente_anthropic
CLIENTE_GOOGLE_REAL = modelos._cliente_google


@pytest.fixture(autouse=True)
def sin_clientes_reales(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test crea un cliente de verdad: si el código lo intenta, falla."""
    def prohibido(_cfg):
        pytest.fail("no tenía que crear un cliente real de la API")

    monkeypatch.setattr(modelos, "_cliente_anthropic", prohibido)
    monkeypatch.setattr(modelos, "_cliente_google", prohibido)


def _escribir_env(home: Path, *, anthropic_: bool = True, gemini: bool = True) -> None:
    lineas = ["CLASSMER_TOKEN=token-de-prueba-123"]
    if anthropic_:
        lineas.append(f"ANTHROPIC_API_KEY={CLAVE_ANTHROPIC}")
    if gemini:
        lineas.append(f"GEMINI_API_KEY={CLAVE_GEMINI}")
    (home / ".env").write_text("\n".join(lineas) + "\n", encoding="utf-8")


@pytest.fixture
def cfg_claves(home: Path) -> Config:
    _escribir_env(home)
    return cargar_config(home)


@pytest.fixture
def base_claves(cfg_claves: Config) -> Base:
    return abrir(cfg_claves)


# --- APIs falsas --------------------------------------------------------------------------------------

def info(id_: str, nombre: str, *, imagenes: bool = True, estructurada: bool = True, esfuerzo: bool = True,
         niveles: tuple[str, ...] = ESFUERZOS, capacidades: Any = "sdk") -> ModelInfo:
    """Un ModelInfo como los que arma el SDK (model_construct, sin validar: así hace con las respuestas)."""
    if capacidades == "sdk":
        si = lambda v: CapabilitySupport(supported=v)  # noqa: E731
        capacidades = ModelCapabilities.model_construct(
            image_input=si(imagenes),
            structured_outputs=si(estructurada),
            effort=EffortCapability(supported=esfuerzo, **{n: si(esfuerzo and n in niveles) for n in ESFUERZOS}),
        )
    return ModelInfo.model_construct(
        id=id_, display_name=nombre, capabilities=capacidades, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        max_input_tokens=1_000_000, max_tokens=128_000, lifecycle="active", type="model",
    )


def catalogo_anthropic() -> list[ModelInfo]:
    """Lo que devolvería la API de Anthropic (los más nuevos primero)."""
    return [
        info("claude-opus-5-5", "Claude Opus 5.5"),
        info("claude-sonnet-5-5", "Claude Sonnet 5.5"),
        info("claude-haiku-5-5", "Claude Haiku 5.5"),
        info("claude-fable-5-1", "Claude Fable 5.1"),
        info("claude-opus-5", "Claude Opus 5"),
        info("claude-opus-4-6", "Claude Opus 4.6", niveles=("low", "medium", "high", "max")),
        info("claude-haiku-4-5", "Claude Haiku 4.5", esfuerzo=False),          # sólo clasificar
        info("claude-3-haiku-20240307", "Claude Haiku 3", estructurada=False, esfuerzo=False),  # nada
    ]


class ApiAnthropicFalsa:
    """`client.models.list(limit=…, after_id=…)` con páginas de `por_pagina` (como SyncPage)."""

    def __init__(self, lista: list[Any], *, por_pagina: int = 3, error: BaseException | None = None):
        self.lista = lista
        self.por_pagina = por_pagina
        self.error = error
        self.llamadas: list[dict[str, Any]] = []
        self.cerrado = False
        self.models = self

    def list(self, *, limit: int = 20, after_id: str | None = None) -> SimpleNamespace:
        self.llamadas.append({"limit": limit, "after_id": after_id})
        if self.error is not None:
            raise self.error
        ids = [m.id for m in self.lista]
        inicio = 0 if after_id is None else ids.index(after_id) + 1
        trozo = self.lista[inicio:inicio + self.por_pagina]
        return SimpleNamespace(data=trozo, has_more=inicio + self.por_pagina < len(self.lista),
                               first_id=trozo[0].id if trozo else None,
                               last_id=trozo[-1].id if trozo else None)

    def close(self) -> None:
        self.cerrado = True


def gm(nombre: str, *acciones: str, display: str | None = None) -> genai_types.Model:
    return genai_types.Model(name=f"models/{nombre}", display_name=display or nombre,
                             supported_actions=list(acciones), input_token_limit=1_048_576,
                             output_token_limit=65_536)


GEN = "generateContent"
PAGINAS_GOOGLE = [
    [gm("gemini-3.5-transcribe", GEN, "countTokens", display="Gemini 3.5 Transcribe"),
     gm("gemini-3.5-transcribe-live", "bidiGenerateContent"),
     gm("gemini-flash-latest", GEN, "countTokens", display="Gemini Flash Latest")],
    [gm("gemini-pro-latest", GEN, display="Gemini Pro Latest"),
     gm("gemini-2.5-flash-preview-tts", GEN),
     gm("gemini-2.5-flash-image", GEN),
     gm("gemini-embedding-001", "embedContent")],
    [gm("gemini-3-flash-preview", GEN, display="Gemini 3 Flash Preview"),
     gm("gemini-2.5-flash-native-audio-latest", "bidiGenerateContent"),
     gm("gemini-robotics-er-1.5-preview", GEN),
     gm("gemini-2.5-computer-use-preview-10-2025", GEN),
     gm("nano-banana-pro-preview", GEN),
     gm("gemma-3-27b-it", GEN),
     gm("imagen-4.0-generate-001", "predict")],
]


class ApiGoogleFalsa:
    """`client.models.list(config=…)` que devuelve un Pager de verdad del SDK sobre páginas falsas."""

    def __init__(self, paginas: list[list[genai_types.Model]], *, error: BaseException | None = None):
        self.paginas = paginas
        self.error = error
        self.pedidas: list[int] = []
        self.configs: list[Any] = []
        self.models = self

    def _respuesta(self, i: int) -> SimpleNamespace:
        self.pedidas.append(i)
        token = str(i + 1) if i + 1 < len(self.paginas) else None
        return SimpleNamespace(models=self.paginas[i], next_page_token=token, sdk_http_response=None)

    def _pedir(self, *, config: dict[str, Any]) -> SimpleNamespace:
        return self._respuesta(int(config["page_token"]))

    def list(self, *, config: Any = None) -> pagers.Pager:
        self.configs.append(config)
        if self.error is not None:
            raise self.error
        return pagers.Pager("models", self._pedir, self._respuesta(0), config)


def _fila(base: Base, proveedor: str, id_: str) -> dict[str, Any] | None:
    with base.conexion() as conn:
        f = conn.execute("SELECT * FROM modelos WHERE proveedor = ? AND id = ?", (proveedor, id_)).fetchone()
    return None if f is None else dict(f)


def _ids(base: Base, **kw) -> set[str]:
    with base.conexion() as conn:
        return {m["id"] for m in modelos.catalogo(conn, **kw)}


# --- semillas y catálogo ------------------------------------------------------------------------------

def test_semillas_al_abrir_la_base(base: Base, cfg: Config):
    with base.conexion() as conn:
        todos = modelos.catalogo(conn)
    assert {m["id"] for m in todos} == SEMILLAS_ANTHROPIC | SEMILLAS_GOOGLE
    porid = {m["id"]: m for m in todos}
    assert porid["claude-opus-5-5"]["nombre"] == "Claude Opus 5.5"
    assert porid["claude-fable-5-1"]["nombre"] == "Claude Fable 5.1"
    assert porid["gemini-3.5-transcribe"]["nombre"] == "Gemini 3.5 Transcribe"
    for m in todos:
        assert m["origen"] == "semilla" and m["presente"] is True and m["visto"] is None
        assert m["descubierto"] and datetime.fromisoformat(m["descubierto"]).utcoffset() is not None
        if m["proveedor"] == "anthropic":
            assert set(m["tareas"]) == {"apunte", "clasificar", "revisar_transcript"}
    assert porid["gemini-3.5-transcribe"]["tareas"] == ["transcribir"]
    assert porid["gemini-flash-latest"]["tareas"] == ["clasificar"]
    assert porid["gemini-flash-latest"]["proveedor"] == "google"


def test_volver_a_abrir_no_pisa_lo_que_hay(base: Base, cfg: Config):
    with base.conexion() as conn:
        conn.execute("UPDATE modelos SET presente = 0, descubierto = '2026-01-01T00:00:00-03:00', "
                     "nombre = 'Otro nombre' WHERE id = 'claude-haiku-5-5'")
        conn.execute("DELETE FROM modelos WHERE id = 'gemini-flash-latest'")
    abrir(cfg)
    f = _fila(base, "anthropic", "claude-haiku-5-5")
    assert f["presente"] == 0 and f["descubierto"] == "2026-01-01T00:00:00-03:00" and f["nombre"] == "Otro nombre"
    assert _fila(base, "google", "gemini-flash-latest") is not None   # la que faltaba vuelve
    with base.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM modelos").fetchone()[0] == 6


def test_catalogo_filtra_por_tarea_y_proveedor_y_ordena(base: Base):
    with base.conexion() as conn:
        conn.execute("UPDATE modelos SET presente = 0 WHERE id = 'claude-fable-5-1'")
        apunte = [m["id"] for m in modelos.catalogo(conn, "apunte")]
        clasificar = modelos.catalogo(conn, "clasificar")
        transcribir = [m["id"] for m in modelos.catalogo(conn, "transcribir")]
        google = {m["id"] for m in modelos.catalogo(conn, proveedor="google")}
        clasificar_google = [m["id"] for m in modelos.catalogo(conn, "clasificar", "google")]
    # Presentes primero (por nombre); el que no apareció, al final.
    assert apunte == ["claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"]
    assert [m["proveedor"] for m in clasificar] == ["anthropic"] * 3 + ["google", "anthropic"]
    assert transcribir == ["gemini-3.5-transcribe"]
    assert google == SEMILLAS_GOOGLE
    assert clasificar_google == ["gemini-flash-latest"]


# --- filtros por tarea --------------------------------------------------------------------------------

def test_filtros_anthropic_por_capacidades():
    assert modelos.tareas_anthropic(info("a", "A").capabilities) == ["clasificar", "revisar_transcript", "apunte"]
    assert modelos.tareas_anthropic(info("a", "A", esfuerzo=False).capabilities) == ["clasificar", "revisar_transcript"]
    assert modelos.tareas_anthropic(info("a", "A", imagenes=False).capabilities) == ["clasificar", "revisar_transcript"]
    assert modelos.tareas_anthropic(info("a", "A", estructurada=False).capabilities) == ["apunte"]
    assert modelos.tareas_anthropic(
        info("a", "A", estructurada=False, esfuerzo=False).capabilities) == []
    # Capacidades desconocidas (o que la API no informa) cuentan como soportadas.
    assert modelos.tareas_anthropic(None) == ["clasificar", "revisar_transcript", "apunte"]
    assert modelos.tareas_anthropic({"image_input": {"supported": True}}) == ["clasificar", "revisar_transcript", "apunte"]
    # Como dict (la forma del JSON crudo) también anda.
    assert modelos.tareas_anthropic({"image_input": {"supported": True}, "effort": {"supported": False},
                                     "structured_outputs": {"supported": True}}) == ["clasificar", "revisar_transcript"]


def test_detalles_anthropic():
    m = modelos.modelo_anthropic(info("claude-opus-4-6", "Claude Opus 4.6", niveles=("low", "medium", "high", "max")))
    assert m.id == "claude-opus-4-6" and m.nombre == "Claude Opus 4.6"
    assert m.detalles == {"esfuerzos": ["low", "medium", "high", "max"], "imagenes": True,
                          "salida_estructurada": True, "max_entrada": 1_000_000, "max_salida": 128_000,
                          "ciclo": "active"}
    assert modelos.modelo_anthropic(info("x", "X", esfuerzo=False)).detalles["esfuerzos"] == []
    desconocido = modelos.modelo_anthropic(info("x", "X", capacidades=None))
    assert desconocido.tareas == ["clasificar", "revisar_transcript", "apunte"]
    assert desconocido.detalles["esfuerzos"] is None and desconocido.detalles["imagenes"] is None


@pytest.mark.parametrize(("nombre", "acciones", "tareas"), [
    ("models/gemini-3.5-transcribe", [GEN, "countTokens"], ["transcribir"]),
    ("models/gemini-3.5-transcribe-live", ["bidiGenerateContent"], []),
    ("models/gemini-flash-latest", [GEN, "countTokens", "createCachedContent"], ["clasificar"]),
    ("models/gemini-pro-latest", [GEN], ["clasificar"]),
    ("models/gemini-3-flash-preview", [GEN], ["clasificar"]),
    ("models/gemini-2.5-flash-preview-tts", [GEN], []),
    ("models/gemini-2.5-flash-image", [GEN], []),
    ("models/gemini-embedding-001", ["embedContent"], []),
    ("models/gemini-2.5-flash-native-audio-latest", ["bidiGenerateContent"], []),
    ("models/gemini-2.0-flash-live-001", [GEN, "bidiGenerateContent"], []),
    ("models/gemini-robotics-er-1.5-preview", [GEN], []),
    ("models/gemini-2.5-computer-use-preview-10-2025", [GEN], []),
    ("models/gemini-3-pro-image-preview", [GEN], []),
    ("models/nano-banana-pro-preview", [GEN], []),
    ("models/deep-research-pro-preview-12-2025", [GEN], []),
    ("models/gemma-3-27b-it", [GEN], []),
    ("models/imagen-4.0-generate-001", ["predict"], []),
    ("models/gemini-flash-latest", ["countTokens"], []),    # sin generateContent no sirve
])
def test_filtros_google(nombre: str, acciones: list[str], tareas: list[str]):
    assert modelos.tareas_google(nombre, acciones) == tareas
    m = modelos.modelo_google(gm(nombre.removeprefix("models/"), *acciones))
    assert m.id == nombre.removeprefix("models/") and m.tareas == tareas
    assert m.detalles == {"acciones": acciones, "max_entrada": 1_048_576, "max_salida": 65_536}


# --- redescubrir ----------------------------------------------------------------------------------------

def test_redescubrir_anthropic_pagina_y_actualiza(cfg_claves: Config, base_claves: Base):
    base = base_claves
    with base.conexion() as conn:   # descubierto viejo: tiene que conservarse
        conn.execute("UPDATE modelos SET descubierto = '2026-01-01T00:00:00-03:00' WHERE id = 'claude-opus-5-5'")
    api = ApiAnthropicFalsa(catalogo_anthropic(), por_pagina=3)
    google = ApiGoogleFalsa(PAGINAS_GOOGLE)
    r = modelos.redescubrir(cfg_claves, base, cliente_anthropic=api, cliente_google=google)

    # 8 modelos en páginas de 3: tres pedidos, cada uno con el after_id del último de la página anterior.
    assert [ll["after_id"] for ll in api.llamadas] == [None, "claude-haiku-5-5", "claude-opus-4-6"]
    assert all(ll["limit"] == modelos.POR_PAGINA for ll in api.llamadas)
    assert api.cerrado is False   # el cliente inyectado no se cierra
    ra = r["anthropic"]
    assert r.ok and ra.ok and ra.listados == 8 and ra.cantidad == 7
    assert sorted(ra.nuevos) == ["claude-haiku-4-5", "claude-opus-4-6", "claude-opus-5"]
    assert ra.desaparecidos == [] and ra.volvieron == []

    assert _fila(base, "anthropic", "claude-3-haiku-20240307") is None   # no le sirve a ninguna tarea
    opus = _fila(base, "anthropic", "claude-opus-5-5")
    assert opus["origen"] == "semilla" and opus["descubierto"] == "2026-01-01T00:00:00-03:00"
    assert opus["visto"] == r.momento and opus["presente"] == 1
    assert json.loads(opus["detalles"])["esfuerzos"] == list(ESFUERZOS)
    nuevo = _fila(base, "anthropic", "claude-opus-5")
    assert nuevo["origen"] == "api" and nuevo["descubierto"] == r.momento == nuevo["visto"]
    assert json.loads(nuevo["tareas"]) == ["clasificar", "revisar_transcript", "apunte"]
    haiku45 = _fila(base, "anthropic", "claude-haiku-4-5")
    assert json.loads(haiku45["tareas"]) == ["clasificar", "revisar_transcript"]
    assert "claude-haiku-4-5" not in _ids(base, tarea="apunte")
    assert "claude-haiku-4-5" in _ids(base, tarea="clasificar")
    # Las tareas de Google nunca incluyen apunte.
    assert _ids(base, tarea="apunte") <= {m.id for m in catalogo_anthropic()}

    with base.conexion() as conn:
        d = modelos.ultimo_descubrimiento(conn)
    assert d["anthropic"] == {"momento": r.momento, "ok": True, "detalle": None, "cantidad": 7,
                              "ultimo_ok": r.momento}


def test_redescubrir_google_pagina_y_filtra(cfg_claves: Config, base_claves: Base):
    base = base_claves
    google = ApiGoogleFalsa(PAGINAS_GOOGLE)
    r = modelos.redescubrir(cfg_claves, base, cliente_anthropic=ApiAnthropicFalsa(catalogo_anthropic()),
                            cliente_google=google)
    assert google.pedidas == [0, 1, 2]                          # recorrió las tres páginas
    assert google.configs == [{"page_size": modelos.POR_PAGINA}]
    rg = r["google"]
    assert rg.ok and rg.listados == 14 and rg.cantidad == 4
    assert sorted(rg.nuevos) == ["gemini-3-flash-preview", "gemini-pro-latest"]
    assert _ids(base, proveedor="google") == {"gemini-3.5-transcribe", "gemini-flash-latest",
                                              "gemini-pro-latest", "gemini-3-flash-preview"}
    assert _ids(base, tarea="transcribir") == {"gemini-3.5-transcribe"}
    assert _ids(base, tarea="clasificar", proveedor="google") == {
        "gemini-flash-latest", "gemini-pro-latest", "gemini-3-flash-preview"}
    pro = _fila(base, "google", "gemini-pro-latest")
    assert pro["nombre"] == "Gemini Pro Latest"
    assert json.loads(pro["detalles"]) == {"acciones": [GEN], "max_entrada": 1_048_576, "max_salida": 65_536}


def test_falta_una_clave_no_toca_ese_proveedor(home: Path, base: Base):
    _escribir_env(home, gemini=False)
    cfg = cargar_config(home)
    with base.conexion() as conn:
        conn.execute("UPDATE modelos SET nombre = 'Lo que había' WHERE id = 'gemini-flash-latest'")
    google = ApiGoogleFalsa(PAGINAS_GOOGLE)
    r = modelos.redescubrir(cfg, base, cliente_anthropic=ApiAnthropicFalsa(catalogo_anthropic()),
                            cliente_google=google)
    assert google.configs == []                       # sin clave, ni se le pregunta
    assert r["anthropic"].ok and not r["google"].ok and not r.ok
    assert "falta GEMINI_API_KEY" in r["google"].detalle
    assert "falta GEMINI_API_KEY" in r["google"].texto()
    assert _ids(base, proveedor="google") == SEMILLAS_GOOGLE
    f = _fila(base, "google", "gemini-flash-latest")
    assert f["nombre"] == "Lo que había" and f["presente"] == 1 and f["visto"] is None
    with base.conexion() as conn:
        d = modelos.ultimo_descubrimiento(conn)
    assert d["google"]["ok"] is False and d["google"]["cantidad"] is None
    assert "falta GEMINI_API_KEY" in d["google"]["detalle"] and d["google"]["ultimo_ok"] is None
    assert d["anthropic"]["ok"] is True


def test_sin_clave_de_anthropic_tampoco_crea_el_cliente(home: Path, base: Base):
    _escribir_env(home, anthropic_=False, gemini=False)
    cfg = cargar_config(home)
    r = modelos.redescubrir(cfg, base)    # los creadores de clientes fallan si se los llama
    assert not r["anthropic"].ok and "falta ANTHROPIC_API_KEY" in r["anthropic"].detalle
    assert not r["google"].ok
    assert _ids(base) == SEMILLAS_ANTHROPIC | SEMILLAS_GOOGLE


def test_error_de_la_api_mantiene_el_catalogo_y_no_filtra_la_clave(cfg_claves: Config, base_claves: Base,
                                                                   caplog: pytest.LogCaptureFixture):
    base = base_claves
    modelos.redescubrir(cfg_claves, base, cliente_anthropic=ApiAnthropicFalsa(catalogo_anthropic()),
                        cliente_google=ApiGoogleFalsa(PAGINAS_GOOGLE))
    with base.conexion() as conn:
        antes = [dict(f) for f in conn.execute("SELECT * FROM modelos ORDER BY proveedor, id")]

    respuesta = httpx2.Response(401, request=httpx2.Request("GET", "https://api.anthropic.com/v1/models"))
    error_anthropic = anthropic.AuthenticationError(f"invalid x-api-key {CLAVE_ANTHROPIC}", response=respuesta,
                                                    body={"type": "error", "error": {"type": "authentication_error"}})
    error_google = genai_errors.APIError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                         "message": f"API key not valid: {CLAVE_GEMINI}"}})
    with caplog.at_level(logging.INFO):
        r = modelos.redescubrir(cfg_claves, base, cliente_anthropic=ApiAnthropicFalsa([], error=error_anthropic),
                                cliente_google=ApiGoogleFalsa([], error=error_google))
    assert not r.ok and not r["anthropic"].ok and not r["google"].ok
    assert "AuthenticationError 401" in r["anthropic"].detalle and "***" in r["anthropic"].detalle
    assert "400" in r["google"].detalle and "***" in r["google"].detalle
    with base.conexion() as conn:
        despues = [dict(f) for f in conn.execute("SELECT * FROM modelos ORDER BY proveedor, id")]
        filas = [dict(f) for f in conn.execute("SELECT * FROM descubrimientos")]
        d = modelos.ultimo_descubrimiento(conn)
    assert despues == antes                                        # se mantiene lo que había
    assert all(f["ok"] == 0 for f in filas)
    assert d["anthropic"]["ultimo_ok"] is not None                 # el último que anduvo sigue a la vista
    textos = json.dumps(filas) + caplog.text + "\n".join(r.lineas())
    assert CLAVE_ANTHROPIC not in textos and CLAVE_GEMINI not in textos


def test_error_de_red_mantiene(cfg_claves: Config, base_claves: Base):
    error = anthropic.APIConnectionError(request=httpx2.Request("GET", "https://api.anthropic.com/v1/models"))
    r = modelos.redescubrir(cfg_claves, base_claves, cliente_anthropic=ApiAnthropicFalsa([], error=error),
                            cliente_google=ApiGoogleFalsa(PAGINAS_GOOGLE))
    assert not r["anthropic"].ok and "APIConnectionError" in r["anthropic"].detalle
    assert r["google"].ok
    assert _ids(base_claves, proveedor="anthropic") == SEMILLAS_ANTHROPIC


def test_api_sin_modelos_no_marca_todo_como_ausente(cfg_claves: Config, base_claves: Base):
    r = modelos.redescubrir(cfg_claves, base_claves, cliente_anthropic=ApiAnthropicFalsa([]),
                            cliente_google=ApiGoogleFalsa([[]]))
    assert not r["anthropic"].ok and "ningún modelo" in r["anthropic"].detalle
    assert not r["google"].ok
    with base_claves.conexion() as conn:
        assert conn.execute("SELECT COUNT(*) FROM modelos WHERE presente = 0").fetchone()[0] == 0


def test_modelo_elegido_que_desaparece_sigue_elegido_con_aviso(cfg_claves: Config, base_claves: Base):
    base = base_claves
    sin_fable = [m for m in catalogo_anthropic() if m.id != "claude-fable-5-1"]
    r = modelos.redescubrir(cfg_claves, base, cliente_anthropic=ApiAnthropicFalsa(sin_fable),
                            cliente_google=ApiGoogleFalsa(PAGINAS_GOOGLE))
    assert r["anthropic"].desaparecidos == ["claude-fable-5-1"]
    fable = _fila(base, "anthropic", "claude-fable-5-1")
    assert fable is not None and fable["presente"] == 0          # no se borra

    with base.conexion() as conn:
        opciones = modelos.opciones_tarea(conn, "apunte", "claude-fable-5-1")
        sin_ausentes = modelos.opciones_tarea(conn, "apunte", "claude-opus-5-5", incluir_ausentes=False)
        con_elegido_ausente = modelos.opciones_tarea(conn, "apunte", "claude-fable-5-1", incluir_ausentes=False)
    elegidas = [o for o in opciones if o["elegido"]]
    assert [o["id"] for o in elegidas] == ["claude-fable-5-1"]
    assert elegidas[0]["en_catalogo"] is True and elegidas[0]["presente"] is False
    assert elegidas[0]["aviso"] == "no apareció en el último descubrimiento"
    assert opciones[-1]["id"] == "claude-fable-5-1"               # los ausentes van al final
    assert all(o["aviso"] is None for o in opciones if o["id"] != "claude-fable-5-1")
    assert "claude-fable-5-1" not in {o["id"] for o in sin_ausentes}
    assert "claude-fable-5-1" in {o["id"] for o in con_elegido_ausente}

    # Si vuelve a aparecer, vuelve a estar presente (y se cuenta).
    r = modelos.redescubrir(cfg_claves, base, cliente_anthropic=ApiAnthropicFalsa(catalogo_anthropic()),
                            cliente_google=ApiGoogleFalsa(PAGINAS_GOOGLE))
    assert r["anthropic"].volvieron == ["claude-fable-5-1"]
    assert _fila(base, "anthropic", "claude-fable-5-1")["presente"] == 1


def test_modelo_que_ya_no_sirve_queda_ausente(cfg_claves: Config, base_claves: Base):
    # La API sigue listando el modelo pero ya no le sirve a ninguna tarea: no se guarda lo nuevo y el
    # que estaba queda presente = 0 (con sus tareas de antes, para avisar si estaba elegido).
    lista = [m for m in catalogo_anthropic() if m.id != "claude-haiku-5-5"]
    lista.append(info("claude-haiku-5-5", "Claude Haiku 5.5", estructurada=False, esfuerzo=False))
    r = modelos.redescubrir(cfg_claves, base_claves, cliente_anthropic=ApiAnthropicFalsa(lista),
                            cliente_google=ApiGoogleFalsa(PAGINAS_GOOGLE))
    assert "claude-haiku-5-5" in r["anthropic"].desaparecidos
    f = _fila(base_claves, "anthropic", "claude-haiku-5-5")
    assert f["presente"] == 0 and set(json.loads(f["tareas"])) == {"clasificar", "revisar_transcript", "apunte"}


def test_opciones_con_elegido_fuera_del_catalogo(base: Base):
    with base.conexion() as conn:
        desconocido = modelos.opciones_tarea(conn, "apunte", "claude-opus-9-9")
        otra_tarea = modelos.opciones_tarea(conn, "apunte", "gemini-3.5-transcribe")
        otro_proveedor = modelos.opciones_tarea(conn, "clasificar", "gemini-flash-latest", proveedor="anthropic")
        varios = modelos.opciones_tarea(conn, "apunte", ["claude-opus-5-5", "claude-sonnet-5-5", "", None])
        ninguno = modelos.opciones_tarea(conn, "transcribir")

    extra = desconocido[0]
    assert extra["id"] == "claude-opus-9-9" and extra["elegido"] is True and extra["en_catalogo"] is False
    assert extra["aviso"] == "no está en el catálogo" and extra["proveedor"] == "anthropic"
    assert extra["presente"] is False and extra["tareas"] == []
    assert len(desconocido) == 1 + len(SEMILLAS_ANTHROPIC)
    assert not any(o["elegido"] for o in desconocido[1:])

    assert otra_tarea[0]["id"] == "gemini-3.5-transcribe" and otra_tarea[0]["en_catalogo"] is False
    assert otra_tarea[0]["aviso"] == "no está en el catálogo para apunte"
    assert otro_proveedor[0]["aviso"] == "es un modelo de Google"

    assert {o["id"] for o in varios if o["elegido"]} == {"claude-opus-5-5", "claude-sonnet-5-5"}
    assert all(o["en_catalogo"] for o in varios)
    assert [o["id"] for o in ninguno] == ["gemini-3.5-transcribe"] and ninguno[0]["elegido"] is False


def test_elegidos_de_la_config(home: Path):
    import yaml

    ruta = home / "config.yaml"
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    datos["materias"][0]["apunte"] = {"modelo": "claude-sonnet-5-5", "esfuerzo": "max"}
    datos["materias"][1]["apunte"] = {"modelo": "claude-opus-5-5"}
    ruta.write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")
    cfg = cargar_config(home)
    e = modelos.elegidos_config(cfg)
    assert e["transcribir"] == {"gemini-3.5-transcribe": ["transcribir.modelo"]}
    assert e["clasificar"] == {"claude-sonnet-5-5": ["clasificar.modelo, motor claude (esfuerzo medium)"]}
    assert e["apunte"] == {
        "claude-opus-5-5": ["apunte.modelo (global) (esfuerzo high)", "materia otra (esfuerzo high)"],
        "claude-sonnet-5-5": ["materia prueba (esfuerzo max)"],
    }


def test_proveedor_de():
    assert modelos.proveedor_de("claude-opus-5-5") == "anthropic"
    assert modelos.proveedor_de("gemini-flash-latest") == "google"
    assert modelos.proveedor_de("gpt-x") is None


# --- clientes reales (sin red) ----------------------------------------------------------------------

def test_cliente_anthropic_usa_la_clave_de_la_config_y_no_el_entorno(cfg_claves: Config,
                                                                    monkeypatch: pytest.MonkeyPatch):
    # Con la clave explícita el SDK no mira ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN del entorno.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-del-entorno")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-del-entorno")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    cliente = CLIENTE_ANTHROPIC_REAL(cfg_claves)   # construir no hace ningún pedido
    try:
        assert cliente.api_key == CLAVE_ANTHROPIC
        assert cliente.auth_token is None
        assert str(cliente.base_url).rstrip("/") == "https://api.anthropic.com"
        assert cliente.timeout == modelos.TIMEOUT_S
    finally:
        cliente.close()


def test_cliente_google_usa_la_clave_de_la_config(cfg_claves: Config, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "clave-del-entorno")
    monkeypatch.setenv("GEMINI_API_KEY", "clave-del-entorno")
    cliente = CLIENTE_GOOGLE_REAL(cfg_claves)       # construir no hace ningún pedido
    try:
        assert cliente._api_client.api_key == CLAVE_GEMINI
    finally:
        cliente.close()


def test_redescubrir_cierra_los_clientes_que_crea(cfg_claves: Config, base_claves: Base,
                                                 monkeypatch: pytest.MonkeyPatch):
    api = ApiAnthropicFalsa(catalogo_anthropic())
    creados: list[str] = []

    def crear_anthropic(cfg):
        assert cfg.anthropic_api_key == CLAVE_ANTHROPIC
        creados.append("anthropic")
        return api

    def crear_google(cfg):
        creados.append("google")
        return ApiGoogleFalsa(PAGINAS_GOOGLE)

    monkeypatch.setattr(modelos, "_cliente_anthropic", crear_anthropic)
    monkeypatch.setattr(modelos, "_cliente_google", crear_google)
    r = modelos.redescubrir(cfg_claves, base_claves)
    assert r.ok and creados == ["anthropic", "google"]
    assert api.cerrado is True


# --- revisión: listas a medias, niveles desconocidos, modelos retirados --------------------------------

def test_esfuerzo_soportado_sin_niveles_informados_es_desconocido():
    # La API dice que soporta `effort` pero no informa los niveles: no se sabe cuáles (None), no "ninguno".
    sin_niveles = modelos.modelo_anthropic(info("x", "X", capacidades={
        "image_input": {"supported": True}, "structured_outputs": {"supported": True},
        "effort": {"supported": True}}))
    assert sin_niveles.detalles["esfuerzos"] is None
    assert sin_niveles.tareas == ["clasificar", "revisar_transcript", "apunte"]
    # Con algunos niveles informados, van sólo los soportados.
    parcial = modelos.modelo_anthropic(info("x", "X", capacidades={
        "effort": {"supported": True, "low": {"supported": True}, "max": {"supported": False}}}))
    assert parcial.detalles["esfuerzos"] == ["low"]


def test_modelo_retirado_o_sin_id_no_sirve():
    retirado = info("claude-viejo", "Claude Viejo")
    retirado.lifecycle = "retired"
    assert modelos.modelo_anthropic(retirado).tareas == []
    assert modelos.modelo_anthropic(info("claude-x", "X")).tareas == ["clasificar", "revisar_transcript", "apunte"]   # "active"
    deprecado = info("claude-y", "Y")
    deprecado.lifecycle = "deprecated"                       # deprecado todavía se puede usar
    assert modelos.modelo_anthropic(deprecado).tareas == ["clasificar", "revisar_transcript", "apunte"]
    assert modelos.modelo_anthropic(SimpleNamespace(id=None, display_name="?", capabilities=None)).tareas == []


def test_semilla_retirada_queda_ausente(cfg_claves: Config, base_claves: Base):
    lista = catalogo_anthropic()
    next(m for m in lista if m.id == "claude-haiku-5-5").lifecycle = "retired"
    r = modelos.redescubrir(cfg_claves, base_claves, cliente_anthropic=ApiAnthropicFalsa(lista),
                            cliente_google=ApiGoogleFalsa(PAGINAS_GOOGLE))
    assert r["anthropic"].ok and "claude-haiku-5-5" in r["anthropic"].desaparecidos
    assert _fila(base_claves, "anthropic", "claude-haiku-5-5")["presente"] == 0


class ApiAnthropicRara(ApiAnthropicFalsa):
    """Falla en el pedido `falla_en` (0 = el primero), o dice `has_more` sin `last_id`, o nunca termina."""

    def __init__(self, lista, *, falla_en: int | None = None, sin_last_id: bool = False,
                 sin_fin: bool = False, **kw):
        super().__init__(lista, **kw)
        self.falla_en, self.sin_last_id, self.sin_fin = falla_en, sin_last_id, sin_fin

    def list(self, *, limit: int = 20, after_id: str | None = None) -> SimpleNamespace:
        if self.falla_en is not None and len(self.llamadas) == self.falla_en:
            self.llamadas.append({"limit": limit, "after_id": after_id})
            raise anthropic.APIConnectionError(request=httpx2.Request("GET", "https://api.anthropic.com/v1/models"))
        if self.sin_fin:
            self.llamadas.append({"limit": limit, "after_id": after_id})
            return SimpleNamespace(data=self.lista[:1], has_more=True, last_id=f"x{len(self.llamadas)}")
        pagina = super().list(limit=limit, after_id=after_id)
        if self.sin_last_id:
            pagina.last_id = None
        return pagina


def _estado(base: Base) -> list[dict[str, Any]]:
    with base.conexion() as conn:
        return [dict(f) for f in conn.execute("SELECT * FROM modelos ORDER BY proveedor, id")]


@pytest.mark.parametrize("rara", [
    {"falla_en": 1},          # se corta en la segunda página
    {"sin_last_id": True},    # has_more sin last_id: la lista quedaría a medias
    {"sin_fin": True},        # nunca dice "no hay más"
], ids=["falla-en-pagina-2", "sin-last-id", "sin-fin"])
def test_anthropic_lista_a_medias_no_toca_el_catalogo(cfg_claves: Config, base_claves: Base,
                                                      rara: dict[str, Any]):
    antes = _estado(base_claves)
    api = ApiAnthropicRara(catalogo_anthropic(), por_pagina=3, **rara)
    r = modelos.redescubrir(cfg_claves, base_claves, cliente_anthropic=api,
                            cliente_google=ApiGoogleFalsa(PAGINAS_GOOGLE))
    assert not r["anthropic"].ok and r["google"].ok
    assert len(api.llamadas) == (1 if rara.get("sin_last_id") else 2 if rara.get("falla_en") else modelos.MAX_PAGINAS)
    assert [f for f in _estado(base_claves) if f["proveedor"] == "anthropic"] == \
        [f for f in antes if f["proveedor"] == "anthropic"]


def test_google_falla_en_la_segunda_pagina_no_toca_el_catalogo(cfg_claves: Config, base_claves: Base):
    antes = _estado(base_claves)
    google = ApiGoogleFalsa(PAGINAS_GOOGLE)

    def pedir(*, config):
        raise genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "x"}})

    google._pedir = pedir
    r = modelos.redescubrir(cfg_claves, base_claves, cliente_anthropic=ApiAnthropicFalsa(catalogo_anthropic()),
                            cliente_google=google)
    assert google.pedidas == [0]   # la primera página llegó, la segunda falló
    assert not r["google"].ok and "503" in r["google"].detalle
    assert [f for f in _estado(base_claves) if f["proveedor"] == "google"] == \
        [f for f in antes if f["proveedor"] == "google"]
