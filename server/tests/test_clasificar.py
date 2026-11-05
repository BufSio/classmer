"""Clasificar materia (§6 etapa 5): no-op con materia, asignación por confianza, Gemini y Claude con fakes.

Claude: API falsa (objeto con .messages.create y excepciones reales del SDK `anthropic`) y Agent SDK
falso (la interfaz de ClaudeSDKClient). Nunca se llama a una API real y la clave es falsa.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from claude_agent_sdk import AssistantMessage, RateLimitEvent, RateLimitInfo, ResultMessage, SystemMessage, TextBlock
from google.genai import errors, types

from classmer import cola, sesiones
from classmer.cola import Job
from classmer.config import cargar_config
from classmer.ia import clasificador as cl
from classmer.ia.clasificador import (
    Clasificador,
    ClasificadorClaude,
    ClasificadorGemini,
    MateriaCandidata,
    ResultadoClasificacion,
    candidatas,
    crear_clasificador,
    error_api,
    esquema,
    extracto_transcript,
    interpretar,
)
from classmer.ia.facturacion import MARGEN_MAX_S, Facturacion, SinCreditos, costo_api
from classmer.pipeline import Contexto, CuotaAgotada, ErrorEtapa, EsperandoUsuario, Servicios
from classmer.pipeline import clasificar as etapa
from classmer.worker import Worker

SID = "0192f7c2-0000-7000-8000-00000000bbbb"
JOB = Job(id=1, tipo="clasificar", sesion_id=SID)
TRANSCRIPT = [
    {"bloque": 1, "inicio_ms": 0, "fin_ms": 4000, "hablante": "profe", "texto": "Hoy vemos vectores en negrita."},
    {"bloque": 1, "inicio_ms": 4000, "fin_ms": 9000, "hablante": "profe", "texto": "Y el producto escalar."},
]


class ClasificadorFalso:
    def __init__(self, resultado: ResultadoClasificacion):
        self.resultado = resultado
        self.llamadas: list[tuple[str, list[MateriaCandidata], int | None]] = []

    def clasificar(self, transcript, materias, *, duracion_ms=None):
        self.llamadas.append((transcript, materias, duracion_ms))
        return self.resultado


class ClienteFalso:
    def __init__(self, respuesta):
        self.respuesta = respuesta
        self.llamadas: list[dict] = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.llamadas.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.respuesta, BaseException):
            raise self.respuesta
        return self.respuesta


def respuesta_texto(texto: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=texto)]), finish_reason="STOP")])


def crear_sesion(ctx, materia: str | None = None, transcript: list | None = TRANSCRIPT) -> None:
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=SID, materia_id=materia, origen_materia=None,
                       tipo_clase=None, inicio="2026-10-15T14:00:00-03:00", dispositivo="test")
    if transcript is not None:
        (ctx.sesion_dir(SID) / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")


def con_clasificador(ctx, falso: ClasificadorFalso) -> None:
    ctx.servicios = Servicios(clasificador=lambda cfg: falso)


def fila(ctx):
    with ctx.base.conexion() as conn:
        return conn.execute("SELECT materia_id, confianza, origen_materia FROM sesiones WHERE id = ?",
                            (SID,)).fetchone()


# --- etapa -----------------------------------------------------------------------------------

def test_con_materia_no_llama_a_nada(ctx):
    crear_sesion(ctx, materia="prueba")

    def no_llamar(cfg):
        raise AssertionError("no debería clasificar")

    ctx.servicios = Servicios(clasificador=no_llamar)
    r = etapa.ejecutar(ctx, JOB)
    assert r.siguientes == [("revisar_transcript", {})]
    assert not (ctx.sesion_dir(SID) / "clasificacion.json").exists()


def test_confianza_alta_asigna_y_sigue(ctx):
    crear_sesion(ctx)
    assert fila(ctx)["origen_materia"] == "auto"
    falso = ClasificadorFalso(ResultadoClasificacion("prueba", 0.9, "Habla de vectores en negrita.", "flash"))
    con_clasificador(ctx, falso)
    r = etapa.ejecutar(ctx, JOB)
    assert r.siguientes == [("revisar_transcript", {})]
    f = fila(ctx)
    assert (f["materia_id"], f["confianza"], f["origen_materia"]) == ("prueba", 0.9, "auto")
    datos = json.loads((ctx.sesion_dir(SID) / "clasificacion.json").read_text(encoding="utf-8"))
    # Un clasificador falso (sin motor) y un resultado sin costo/facturación: igual se escriben las claves.
    assert datos == {"materia_id": "prueba", "confianza": 0.9, "motivo": "Habla de vectores en negrita.",
                     "modelo": "flash", "motor": None, "facturacion": None, "costo": None, "uso": {}}
    assert r.facturacion is None and r.costo is None and r.motor == "flash"
    transcript, materias, duracion = falso.llamadas[0]
    assert "[00:00:00] Hoy vemos vectores en negrita." in transcript
    assert [m.id for m in materias] == ["prueba", "otra"]
    assert "Notación" in (materias[0].perfil or "") and materias[1].perfil is None
    assert duracion == 9000


def test_confianza_baja_espera_al_usuario(ctx):
    crear_sesion(ctx)
    con_clasificador(ctx, ClasificadorFalso(ResultadoClasificacion("prueba", 0.42, "Hay poco texto.", "flash")))
    with pytest.raises(EsperandoUsuario) as info:
        etapa.ejecutar(ctx, JOB)
    mensaje = str(info.value)
    assert "confianza 0.42: Hay poco texto." in mensaje
    assert f"uv run classmer asignar {SID} --materia" in mensaje
    f = fila(ctx)
    assert f["materia_id"] is None and f["confianza"] == 0.42


class ClasificadorConAsignacionManual(ClasificadorFalso):
    """Mientras 'llama a Gemini', el usuario asigna la materia a mano (como `classmer asignar`)."""

    def __init__(self, ctx, resultado: ResultadoClasificacion):
        super().__init__(resultado)
        self.ctx = ctx

    def clasificar(self, transcript, materias, *, duracion_ms=None):
        with self.ctx.base.conexion() as conn:
            conn.execute("UPDATE sesiones SET materia_id = 'otra', origen_materia = 'manual', confianza = NULL "
                         "WHERE id = ?", (SID,))
        return super().clasificar(transcript, materias, duracion_ms=duracion_ms)


def test_asignar_durante_clasificar_no_se_pisa(ctx):
    crear_sesion(ctx)
    con_clasificador(ctx, ClasificadorConAsignacionManual(
        ctx, ResultadoClasificacion("prueba", 0.9, "Vectores.", "flash")))
    r = etapa.ejecutar(ctx, JOB)
    assert r.siguientes == [("revisar_transcript", {})]
    f = fila(ctx)
    assert (f["materia_id"], f["confianza"], f["origen_materia"]) == ("otra", None, "manual")


def test_asignar_durante_clasificar_con_confianza_baja_sigue_al_apunte(ctx):
    crear_sesion(ctx)
    con_clasificador(ctx, ClasificadorConAsignacionManual(
        ctx, ResultadoClasificacion("prueba", 0.3, "Poco texto.", "flash")))
    r = etapa.ejecutar(ctx, JOB)                                   # no queda esperando al usuario
    assert r.siguientes == [("revisar_transcript", {})]
    f = fila(ctx)
    assert (f["materia_id"], f["confianza"], f["origen_materia"]) == ("otra", None, "manual")


def test_materia_inexistente_no_se_asigna(ctx):
    crear_sesion(ctx)
    con_clasificador(ctx, ClasificadorFalso(ResultadoClasificacion("fisica", 0.95, "Es física.", "flash")))
    with pytest.raises(EsperandoUsuario):
        etapa.ejecutar(ctx, JOB)
    assert fila(ctx)["materia_id"] is None
    datos = json.loads((ctx.sesion_dir(SID) / "clasificacion.json").read_text(encoding="utf-8"))
    assert datos["materia_id"] is None and "fisica" in datos["motivo"]


def test_sin_transcript_espera_sin_llamar(ctx):
    crear_sesion(ctx, transcript=None)
    falso = ClasificadorFalso(ResultadoClasificacion("prueba", 1.0, "x", "flash"))
    con_clasificador(ctx, falso)
    with pytest.raises(EsperandoUsuario) as info:
        etapa.ejecutar(ctx, JOB)
    assert "vacío" in str(info.value) and falso.llamadas == []


def test_usa_los_segmentos_de_la_base_si_falta_transcript_json(ctx):
    crear_sesion(ctx, transcript=None)
    with ctx.base.conexion() as conn:
        conn.execute("INSERT INTO segmentos (sesion_id, bloque, inicio_ms, fin_ms, hablante, texto) "
                     "VALUES (?, 1, 0, 5000, 'profe', 'Teorema de la proyección ortogonal.')", (SID,))
    falso = ClasificadorFalso(ResultadoClasificacion("prueba", 0.8, "Proyección.", "flash"))
    con_clasificador(ctx, falso)
    assert etapa.ejecutar(ctx, JOB).siguientes == [("revisar_transcript", {})]
    assert "proyección ortogonal" in falso.llamadas[0][0]


# --- Gemini (cliente falso) ------------------------------------------------------------------

def test_clasificador_gemini_prompt_y_config(cfg):
    # El default ahora es Claude: Gemini Flash es una opción elegible (motor gemini + su modelo).
    cfg.clasificar = cfg.clasificar.model_copy(update={"motor": "gemini", "modelo": "gemini-flash-latest"})
    cfg.gemini_api_key = "clave-falsa"
    cliente = ClienteFalso(respuesta_texto(
        '{"materia_id": "prueba", "confianza": 0.83, "motivo": "Habla de vectores."}'))
    materias = candidatas(cfg)
    clasificador = crear_clasificador(cfg)
    assert isinstance(clasificador, ClasificadorGemini)
    clasificador._cliente = cliente
    r = clasificador.clasificar("[00:00:00] Hoy vemos vectores.", materias, duracion_ms=5_400_000)
    assert (r.materia_id, r.confianza, r.motivo, r.modelo) == ("prueba", 0.83, "Habla de vectores.",
                                                              "gemini-flash-latest")
    llamada = cliente.llamadas[0]
    assert llamada["model"] == "gemini-flash-latest"
    assert llamada["config"].response_mime_type == "application/json"
    prompt = llamada["contents"]
    for esperado in ("### id: prueba", "Materia de Prueba", "### id: otra", "Notación: vectores en negrita.",
                     "[00:00:00] Hoy vemos vectores.", "01:30:00", "prueba, otra"):
        assert esperado in prompt
    assert "{materias}" not in prompt and "<!--" not in prompt


def test_candidatas_sin_materias_inactivas(cfg):
    assert [m.id for m in candidatas(cfg)] == ["prueba", "otra"]
    cfg.materias = [m.model_copy(update={"activa": False}) if m.id == "otra" else m for m in cfg.materias]
    assert [m.id for m in candidatas(cfg)] == ["prueba"]


def test_clasificador_gemini_errores(cfg):
    with pytest.raises(CuotaAgotada) as info:                      # sin clave
        ClasificadorGemini(cfg).clasificar("x", [MateriaCandidata("prueba", "Prueba")])
    assert "GEMINI_API_KEY" in str(info.value)
    cfg.gemini_api_key = "clave-falsa"
    cuota = errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "q"}})
    with pytest.raises(CuotaAgotada):
        ClasificadorGemini(cfg, cliente=ClienteFalso(cuota)).clasificar("x", [MateriaCandidata("prueba", "P")])


def test_interpretar_es_tolerante():
    r = interpretar('```json\n{"materia_id": "macro", "confianza": 85, "motivo": "IS-LM"}\n```', "m")
    assert (r.materia_id, r.confianza) == ("macro", 0.85)
    assert interpretar('{"materia_id": "null", "confianza": 0.3, "motivo": ""}', "m").materia_id is None
    assert interpretar('{"materia_id": null, "confianza": "x"}', "m").confianza == 0.0
    malo = interpretar("no es json", "m")
    assert malo.materia_id is None and malo.confianza == 0.0


# --- extracto del transcript -----------------------------------------------------------------

def test_extracto_transcript_corto_va_entero():
    assert extracto_transcript(TRANSCRIPT) == ("[00:00:00] Hoy vemos vectores en negrita.\n"
                                               "[00:00:04] Y el producto escalar.")


def test_extracto_transcript_largo_toma_comienzo_y_medio():
    texto = "palabra " * 12
    segs = [{"inicio_ms": i * 10_000, "fin_ms": i * 10_000 + 9000, "texto": f"{texto}{i}"}
            for i in range(720)]                                   # 2 horas, ~80k caracteres
    extracto = extracto_transcript(segs)
    assert len(extracto) <= 30_000
    assert extracto.startswith("[00:00:00]")
    assert "[…]" in extracto and "[01:00:00]" in extracto           # algo del medio
    assert "[01:59:50]" not in extracto


# --- Claude: API y Agent SDK falsos ----------------------------------------------------------

CLAVE = "sk-ant-falsa-123"
URL_MENSAJES = "https://api.anthropic.com/v1/messages"
RESPUESTA = '{"materia_id": "prueba", "confianza": 0.91, "motivo": "Habla de vectores en negrita."}'
ESTRUCTURADA = {"materia_id": "otra", "confianza": 0.88, "motivo": "Temas de la otra materia."}
MATERIAS = [MateriaCandidata("prueba", "Materia de Prueba", "Vectores"), MateriaCandidata("otra", "Otra Materia")]
VUELVE = int(time.time()) + 7200


def cfg_claude(home, modo: str = "api_luego_max", *, clave: str | None = CLAVE):
    """Config recargada con la clave en home/.env (como en producción) y el modo de facturación."""
    if clave:
        env = home / ".env"
        env.write_text(env.read_text(encoding="utf-8") + f"ANTHROPIC_API_KEY={clave}\n", encoding="utf-8")
    cfg = cargar_config(home)
    cfg.claude = cfg.claude.model_copy(update={"facturacion": modo})
    return cfg


def mensaje(texto: str = RESPUESTA, *, stop_reason: str = "end_turn", stop_details: dict | None = None,
            **usage: int) -> Message:
    return Message.model_validate({
        "id": "msg_falso", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": texto}] if texto else [],
        "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
        "usage": {"input_tokens": 1200, "output_tokens": 80, **usage},
    })


def excepcion_api(clase: type, estado: int, tipo: str, texto: str, headers: dict[str, str] | None = None):
    """Una excepción real del SDK `anthropic` (necesita la respuesta HTTP y el cuerpo)."""
    cuerpo = {"type": "error", "error": {"type": tipo, "message": texto}}
    respuesta = httpx2.Response(estado, request=httpx2.Request("POST", URL_MENSAJES), json=cuerpo,
                                headers=headers or {})
    return clase(f"Error code: {estado} - {cuerpo}", response=respuesta, body=cuerpo)


def sin_creditos_400():
    return excepcion_api(anthropic.BadRequestError, 400, "invalid_request_error",
                         "Your credit balance is too low to access the Anthropic API. "
                         "Please go to Plans & Billing to upgrade or purchase credits.")


def sin_creditos_402():
    return excepcion_api(anthropic.APIStatusError, 402, "billing_error", "Billing issue with your account.")


def clave_invalida():
    return excepcion_api(anthropic.AuthenticationError, 401, "authentication_error", "invalid x-api-key")


class ApiFalsa:
    """`anthropic.Anthropic` falso: .messages.create devuelve (o lanza) lo que se le dio, en orden."""

    def __init__(self, *respuestas: Any):
        self.respuestas = list(respuestas)
        self.llamadas: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kw: Any) -> Any:
        self.llamadas.append(kw)
        r = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(r, BaseException):
            raise r
        return r


class ApiProhibida:
    """La API no se tiene que tocar."""

    @property
    def messages(self):
        raise AssertionError("no debería llamar a la API")


class SdkFalso:
    """ClaudeSDKClient falso: guarda el entorno con el que se lanzaría el CLI y devuelve un guion."""

    def __init__(self, opciones: Any, mensajes: list[Any]):
        self.opciones = opciones
        self.mensajes = mensajes
        self.consultas: list[str] = []
        self.entorno: dict[str, str] = {}
        self.desconectado = False

    async def connect(self) -> None:
        self.entorno = dict(os.environ)

    async def query(self, prompt: str) -> None:
        self.consultas.append(prompt)

    async def receive_response(self):
        for m in self.mensajes:
            yield m

    async def disconnect(self) -> None:
        self.desconectado = True


class FabricaSdk:
    """La fábrica `opciones -> cliente` que recibe ClasificadorClaude; cada cliente corre el mismo guion."""

    def __init__(self, *mensajes: Any):
        self.mensajes = list(mensajes)
        self.creados: list[SdkFalso] = []

    def __call__(self, opciones: Any) -> SdkFalso:
        c = SdkFalso(opciones, list(self.mensajes))
        self.creados.append(c)
        return c


class MedioFijo:
    """Facturación que corre siempre con un medio: para ver la excepción propia de cada camino."""

    def __init__(self, medio: str):
        self.medio = medio

    def ejecutar(self, tarea, correr, *, log_tarea=None):
        return correr(self.medio), self.medio


def init_sdk(clave: str = "none") -> SystemMessage:
    return SystemMessage(subtype="init", data={"model": "claude-sonnet-5-5", "apiKeySource": clave})


def resultado_sdk(datos: Any = ESTRUCTURADA, *, error: bool = False, texto: str = "", subtipo: str = "success",
                  costo: float = 0.0123, **extra: Any) -> ResultMessage:
    return ResultMessage(subtype=subtipo, duration_ms=10, duration_api_ms=8, is_error=error, num_turns=2,
                         session_id="s", total_cost_usd=costo, result=texto, structured_output=datos,
                         usage={"input_tokens": 900, "output_tokens": 60, "cache_read_input_tokens": 300}, **extra)


def _texto_error(texto: str, **extra: Any) -> ResultMessage:
    return resultado_sdk(None, error=True, texto=texto, subtipo="error_during_execution", **extra)


def cupo_agotado() -> list[Any]:
    return [RateLimitEvent(RateLimitInfo(status="rejected", resets_at=VUELVE, rate_limit_type="five_hour"), "u", "s"),
            _texto_error(f"Claude AI usage limit reached|{VUELVE}")]


@pytest.fixture(autouse=True)
def sin_claude_real(monkeypatch):
    """Red de seguridad: ningún test de este archivo puede lanzar el CLI real (plan Max del usuario) ni
    armar un cliente real de la API, aunque se olvide de inyectar los falsos."""
    import claude_agent_sdk

    def prohibido(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("un test intentó usar Claude de verdad")

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", prohibido)
    monkeypatch.setattr(cl, "crear_cliente_api", prohibido)


def estado_facturacion(base) -> dict[str, str]:
    with base.conexion() as conn:
        return {f["medio"]: f["estado"] for f in conn.execute("SELECT medio, estado FROM facturacion")}


def test_crear_clasificador_segun_el_motor(cfg):
    c = crear_clasificador(cfg)                                    # por defecto: Claude Sonnet 5.5
    assert isinstance(c, ClasificadorClaude) and isinstance(c, Clasificador)
    assert (c.motor, c.modelo, c.esfuerzo) == ("claude", "claude-sonnet-5-5", "medium")
    cfg.clasificar = cfg.clasificar.model_copy(update={"motor": "gemini", "modelo": "gemini-flash-latest"})
    g = crear_clasificador(cfg)
    assert isinstance(g, ClasificadorGemini) and (g.motor, g.modelo) == ("gemini", "gemini-flash-latest")
    cfg.clasificar = cfg.clasificar.model_copy(update={"motor": "otro"})
    with pytest.raises(ErrorEtapa) as info:
        crear_clasificador(cfg)
    assert not info.value.reintentable and "otro" in str(info.value)


def test_claude_api_pide_json_estructurado(home, base):
    cfg = cfg_claude(home)
    api = ApiFalsa(mensaje(cache_read_input_tokens=500))
    materias = candidatas(cfg)
    c = ClasificadorClaude(cfg, cliente_api=api, cliente_sdk=FabricaSdk(), facturacion=Facturacion(cfg, base))
    r = c.clasificar("[00:00:00] Hoy vemos vectores.", materias, duracion_ms=5_400_000)
    assert (r.materia_id, r.confianza, r.motivo, r.modelo) == ("prueba", 0.91, "Habla de vectores en negrita.",
                                                              "claude-sonnet-5-5")
    assert r.facturacion == "api" and r.avisos == []
    assert r.uso == {"entrada": 1200, "salida": 80, "cache_lectura": 500}
    assert r.costo == costo_api("claude-sonnet-5-5", r.uso) and r.costo > 0
    (llamada,) = api.llamadas
    assert llamada["model"] == "claude-sonnet-5-5" and llamada["max_tokens"] == 8000
    assert "thinking" not in llamada and "temperature" not in llamada
    (pedido,) = llamada["messages"]
    assert pedido["role"] == "user"
    assert pedido["content"] == c.prompt("[00:00:00] Hoy vemos vectores.", materias, 5_400_000)
    for esperado in ("### id: prueba", "### id: otra", "Notación: vectores en negrita.",
                     "[00:00:00] Hoy vemos vectores.", "01:30:00", "prueba, otra"):
        assert esperado in pedido["content"]
    assert "<!--" not in pedido["content"]
    config = llamada["output_config"]
    assert config["effort"] == "medium"
    assert config["format"] == {"type": "json_schema", "schema": esquema(["prueba", "otra"])}
    schema = config["format"]["schema"]
    assert schema["properties"]["materia_id"]["anyOf"] == [{"type": "string", "enum": ["prueba", "otra"]},
                                                           {"type": "null"}]
    assert schema["required"] == ["materia_id", "confianza", "motivo"] and schema["additionalProperties"] is False
    assert "minimum" not in json.dumps(schema) and "maximum" not in json.dumps(schema)
    assert estado_facturacion(base) == {}


def test_claude_api_sin_esfuerzo_y_solo_materias_activas(home, base):
    cfg = cfg_claude(home)
    cfg.clasificar = cfg.clasificar.model_copy(update={"esfuerzo": None})
    cfg.materias = [m.model_copy(update={"activa": False}) if m.id == "otra" else m for m in cfg.materias]
    api = ApiFalsa(mensaje())
    ClasificadorClaude(cfg, cliente_api=api, facturacion=Facturacion(cfg, base)).clasificar("x", candidatas(cfg))
    config = api.llamadas[0]["output_config"]
    assert "effort" not in config
    assert config["format"]["schema"]["properties"]["materia_id"]["anyOf"][0]["enum"] == ["prueba"]


def test_claude_api_rechazo_del_modelo(home, base):
    cfg = cfg_claude(home)
    api = ApiFalsa(mensaje("", stop_reason="refusal",
                           stop_details={"type": "refusal", "category": "cyber", "explanation": "x"}))
    r = ClasificadorClaude(cfg, cliente_api=api, facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert r.materia_id is None and r.confianza == 0.0
    assert "no quiso clasificar" in r.motivo and "cyber" in r.motivo
    assert r.facturacion == "api" and r.costo is not None


@pytest.mark.parametrize(("error", "estado"), [
    (sin_creditos_400, "sin_creditos"),
    (sin_creditos_402, "sin_creditos"),
    (clave_invalida, "clave_invalida"),
], ids=["credit_balance_400", "billing_error_402", "clave_invalida_401"])
def test_api_sin_creditos_pasa_al_plan_max_y_queda_recordado(home, base, monkeypatch, error, estado):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-del-entorno")
    cfg = cfg_claude(home)
    sdk = FabricaSdk(init_sdk(), resultado_sdk())
    api = ApiFalsa(error())
    r = ClasificadorClaude(cfg, cliente_api=api, cliente_sdk=sdk,
                           facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert (r.materia_id, r.confianza, r.motivo, r.facturacion) == ("otra", 0.88, "Temas de la otra materia.", "max")
    assert r.costo == 0.0123 and r.uso == {"entrada": 900, "salida": 60, "cache_lectura": 300}
    assert len(api.llamadas) == 1 and len(sdk.creados) == 1
    assert any("sigo con el plan Max" in a for a in r.avisos), r.avisos
    assert estado_facturacion(base) == {"api": estado}
    # La clave nunca llega al CLI: ni en el env de las opciones ni en el entorno heredado.
    c = sdk.creados[0]
    assert "ANTHROPIC_API_KEY" not in c.opciones.env and CLAVE not in c.opciones.env.values()
    assert "ANTHROPIC_API_KEY" not in c.entorno and CLAVE not in c.entorno.values()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-del-entorno"     # se restaura después de lanzar
    assert c.desconectado and "### id: prueba" in c.consultas[0]

    # Queda recordado: la siguiente clasificación va directo al plan Max sin tocar la API.
    r2 = ClasificadorClaude(cfg, cliente_api=ApiProhibida(), cliente_sdk=sdk,
                            facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert r2.facturacion == "max" and len(sdk.creados) == 2
    assert any("directo" in a and "plan Max" in a for a in r2.avisos), r2.avisos


@pytest.mark.parametrize(("error", "espera"), [
    (lambda: excepcion_api(anthropic.RateLimitError, 429, "rate_limit_error", "rate limit exceeded",
                           {"retry-after": "30"}), 30),
    (lambda: excepcion_api(anthropic.APIStatusError, 529, "overloaded_error", "Overloaded"), cl.ESPERA_SOBRECARGA_S),
    (lambda: anthropic.APIConnectionError(request=httpx2.Request("POST", URL_MENSAJES)), cl.ESPERA_SIN_RED_S),
], ids=["429", "529", "sin_red"])
def test_lo_pasajero_no_cambia_de_medio(home, base, error, espera):
    cfg = cfg_claude(home)
    sdk = FabricaSdk(resultado_sdk())
    with pytest.raises(CuotaAgotada) as info:
        ClasificadorClaude(cfg, cliente_api=ApiFalsa(error()), cliente_sdk=sdk,
                           facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert not isinstance(info.value, SinCreditos) and info.value.espera_s == espera
    assert sdk.creados == [] and estado_facturacion(base) == {}
    assert info.value.avisos == []                                  # la etapa los loguea igual


def test_modo_api_sin_otro_medio_pospone(home, base):
    cfg = cfg_claude(home, "api")
    sdk = FabricaSdk(resultado_sdk())
    with pytest.raises(CuotaAgotada) as info:
        ClasificadorClaude(cfg, cliente_api=ApiFalsa(sin_creditos_400()), cliente_sdk=sdk,
                           facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert not isinstance(info.value, SinCreditos)
    assert "sin créditos" in str(info.value) and info.value.espera_s > 0
    assert sdk.creados == [] and estado_facturacion(base) == {"api": "sin_creditos"}


def test_modo_max_no_toca_la_api_y_la_clave_no_llega_al_cli(home, base, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-del-entorno")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-del-entorno")
    cfg = cfg_claude(home, "max")
    sdk = FabricaSdk(init_sdk(), resultado_sdk())
    r = ClasificadorClaude(cfg, cliente_api=ApiProhibida(), cliente_sdk=sdk,
                           facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert (r.materia_id, r.facturacion, r.avisos) == ("otra", "max", [])
    c = sdk.creados[0]
    for nombre in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        assert nombre not in c.entorno and nombre not in c.opciones.env
    assert CLAVE not in json.dumps(c.entorno) and CLAVE not in json.dumps(c.opciones.env)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-del-entorno"
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "token-del-entorno"


def test_sin_clave_va_al_plan_max(cfg, base):
    sdk = FabricaSdk(resultado_sdk())
    r = ClasificadorClaude(cfg, cliente_api=ApiProhibida(), cliente_sdk=sdk,
                           facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert r.facturacion == "max" and any("ANTHROPIC_API_KEY" in a for a in r.avisos)


def test_api_sin_clave_es_clave_invalida(cfg):
    with pytest.raises(SinCreditos) as info:
        ClasificadorClaude(cfg, cliente_api=ApiProhibida(), facturacion=MedioFijo("api")).clasificar("x", MATERIAS)
    assert (info.value.medio, info.value.estado) == ("api", "clave_invalida")
    assert "ANTHROPIC_API_KEY" in str(info.value)


def test_max_luego_api_pasa_a_la_api_si_se_agota_el_plan(home, base):
    cfg = cfg_claude(home, "max_luego_api")
    sdk = FabricaSdk(*cupo_agotado())
    api = ApiFalsa(mensaje())
    r = ClasificadorClaude(cfg, cliente_api=api, cliente_sdk=sdk,
                           facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert (r.materia_id, r.facturacion) == ("prueba", "api") and len(api.llamadas) == 1
    assert any("sigo con la API" in a for a in r.avisos), r.avisos
    assert estado_facturacion(base) == {"max": "sin_cupo"}
    with base.conexion() as conn:
        assert conn.execute("SELECT hasta FROM facturacion WHERE medio = 'max'").fetchone()["hasta"] == VUELVE + MARGEN_MAX_S


def test_plan_max_avisa_si_el_cli_usa_una_api_key(home, base):
    cfg = cfg_claude(home, "max")
    sdk = FabricaSdk(init_sdk("ANTHROPIC_API_KEY"), resultado_sdk())
    r = ClasificadorClaude(cfg, cliente_sdk=sdk, facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert any("API key" in a and "plan Max" in a for a in r.avisos), r.avisos


def test_plan_max_sin_structured_output_lee_el_result(cfg):
    texto = '```json\n{"materia_id": "prueba", "confianza": 0.7, "motivo": "Vectores."}\n```'
    sdk = FabricaSdk(resultado_sdk(None, texto=texto))
    r = ClasificadorClaude(cfg, cliente_sdk=sdk, facturacion=MedioFijo("max")).clasificar("x", MATERIAS)
    assert (r.materia_id, r.confianza, r.motivo, r.facturacion) == ("prueba", 0.7, "Vectores.", "max")
    # structured_output como texto JSON (no como dict) también se lee.
    sdk = FabricaSdk(resultado_sdk(json.dumps(ESTRUCTURADA), texto="listo"))
    r = ClasificadorClaude(cfg, cliente_sdk=sdk, facturacion=MedioFijo("max")).clasificar("x", MATERIAS)
    assert (r.materia_id, r.confianza) == ("otra", 0.88)


def test_opciones_del_sdk_y_comando_del_cli(cfg):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from classmer.ia.agente_apunte import PROHIBIDAS

    op = ClasificadorClaude(cfg).opciones_sdk(["prueba", "otra"])
    assert op.tools == [] and op.allowed_tools == [cl.HERRAMIENTA_SALIDA] and op.disallowed_tools == PROHIBIDAS
    assert op.setting_sources == [] and op.skills == [] and op.strict_mcp_config and op.verbatim_prompts
    assert (op.model, op.effort, op.max_turns, op.permission_mode) == ("claude-sonnet-5-5", "medium", 3, "dontAsk")
    assert op.output_format == {"type": "json_schema", "schema": esquema(["prueba", "otra"])}
    assert op.env == cl.ENTORNO_SDK and "ANTHROPIC_API_KEY" not in op.env
    cwd = cfg.jobs_dir / cl.CARPETA_SDK
    assert op.cwd == str(cwd) and cwd.is_dir() and not any(cwd.iterdir())
    # El esfuerzo y el esquema llegan a la línea de comando del CLI (sin lanzar nada).
    cmd = SubprocessCLITransport(prompt="x", options=dataclasses.replace(op, cli_path="claude"))._build_command()

    def valor(flag: str) -> str:
        return cmd[cmd.index(flag) + 1]

    assert valor("--effort") == "medium" and valor("--model") == "claude-sonnet-5-5"
    assert valor("--tools") == "" and valor("--permission-mode") == "dontAsk" and valor("--max-turns") == "3"
    assert json.loads(valor("--json-schema")) == esquema(["prueba", "otra"])
    assert "--setting-sources=" in cmd


@pytest.mark.parametrize(("mensajes", "esperado"), [
    (cupo_agotado(), "sin_cupo"),
    ([_texto_error("You've hit your limit · resets 5pm")], "sin_cupo"),
    ([AssistantMessage(content=[TextBlock("x")], model="m", error="billing_error"), _texto_error("API Error")],
     "sin_cupo"),
    ([_texto_error("Not logged in · Please run /login")], "sin_login"),
    ([AssistantMessage(content=[TextBlock("x")], model="m", error="authentication_failed"), _texto_error("x")],
     "sin_login"),
    ([AssistantMessage(content=[TextBlock("API Error")], model="m", error="rate_limit"),
      _texto_error("API Error: 429", api_error_status=429)], "pasajero"),
    ([_texto_error('API Error: 529 {"type":"overloaded_error"}', api_error_status=529)], "pasajero"),
    ([_texto_error("API Error: Connection error.")], "pasajero"),
    ([resultado_sdk(None, error=True, subtipo="error_max_turns")], "error"),
    ([], "error"),                                                  # el CLI cortó sin resultado
    ([_texto_error("API Error", api_error_status=402)], "sin_cupo"),
    ([_texto_error("API Error: 401 Unauthorized")], "sin_login"),
    # Un "rejected" con una caída de la API es pasajero: no deja el plan Max marcado sin cupo.
    ([RateLimitEvent(RateLimitInfo(status="rejected", resets_at=VUELVE), "u", "s"),
      _texto_error('API Error: 529 {"type":"overloaded_error"}', api_error_status=529)], "pasajero"),
], ids=["rate_limit_event", "hit_your_limit", "billing_error", "not_logged_in", "authentication_failed",
        "429", "529", "sin_red", "max_turns", "sin_resultado", "402", "401", "rechazado_con_529"])
def test_errores_del_plan_max(cfg, mensajes, esperado):
    with pytest.raises(ErrorEtapa) as info:
        ClasificadorClaude(cfg, cliente_sdk=FabricaSdk(*mensajes), facturacion=MedioFijo("max")).clasificar(
            "x", MATERIAS)
    e = info.value
    if esperado in ("sin_cupo", "sin_login"):
        assert isinstance(e, SinCreditos) and (e.medio, e.estado) == ("max", esperado)
    elif esperado == "pasajero":
        assert isinstance(e, CuotaAgotada) and not isinstance(e, SinCreditos)
    else:
        assert not isinstance(e, CuotaAgotada) and e.reintentable


def test_plan_max_cupo_agotado_dice_hasta_cuando(cfg):
    with pytest.raises(SinCreditos) as info:
        ClasificadorClaude(cfg, cliente_sdk=FabricaSdk(*cupo_agotado()), facturacion=MedioFijo("max")).clasificar(
            "x", MATERIAS)
    assert info.value.hasta == VUELVE and 7000 < info.value.espera_s <= 7300


def test_plan_max_falla_al_conectar(cfg):
    class NoConecta(SdkFalso):
        async def connect(self) -> None:
            raise RuntimeError("Invalid API key · Please run /login")

    with pytest.raises(SinCreditos) as info:
        ClasificadorClaude(cfg, cliente_sdk=lambda op: NoConecta(op, []), facturacion=MedioFijo("max")).clasificar(
            "x", MATERIAS)
    assert info.value.estado == "sin_login"


def test_error_api_traduce_las_excepciones_del_sdk():
    pedido = httpx2.Request("POST", URL_MENSAJES)
    tope_de_gasto = excepcion_api(anthropic.BadRequestError, 400, "invalid_request_error",
                                  "You have reached your specified API usage limits. You will regain access "
                                  "on 2026-11-01 at 00:00 UTC.")
    for e, estado in ((clave_invalida(), "clave_invalida"), (sin_creditos_402(), "sin_creditos"),
                      (sin_creditos_400(), "sin_creditos"), (tope_de_gasto, "sin_creditos")):
        r = error_api(e, "claude-sonnet-5-5", clave=CLAVE)
        assert isinstance(r, SinCreditos) and (r.medio, r.estado) == ("api", estado)
    pasajeros = [
        (excepcion_api(anthropic.RateLimitError, 429, "rate_limit_error", "lento", {"retry-after": "45"}), 45),
        (excepcion_api(anthropic.RateLimitError, 429, "rate_limit_error", "lento"), cl.ESPERA_RATE_LIMIT_S),
        (excepcion_api(anthropic.InternalServerError, 500, "api_error", "Internal server error"),
         cl.ESPERA_SOBRECARGA_S),
        (excepcion_api(anthropic.APIStatusError, 529, "overloaded_error", "Overloaded"), cl.ESPERA_SOBRECARGA_S),
        (anthropic.APIConnectionError(request=pedido), cl.ESPERA_SIN_RED_S),
        (anthropic.APITimeoutError(request=pedido), cl.ESPERA_SIN_RED_S),
        (excepcion_api(anthropic.APIStatusError, 408, "timeout_error", "Request timeout"), cl.ESPERA_SIN_RED_S),
    ]
    for e, espera in pasajeros:
        r = error_api(e, "m")
        assert type(r) is CuotaAgotada and r.espera_s == espera, e
    no_existe = excepcion_api(anthropic.NotFoundError, 404, "not_found_error", "model: claude-inexistente")
    for e in (no_existe, excepcion_api(anthropic.BadRequestError, 400, "invalid_request_error", "max_tokens: x"),
              excepcion_api(anthropic.PermissionDeniedError, 403, "permission_error", "no")):
        r = error_api(e, "m")
        assert type(r) is ErrorEtapa and not r.reintentable
    assert "model: claude-inexistente" in str(error_api(no_existe, "claude-inexistente"))
    # Nunca la clave en el mensaje (va a la base, al log y al panel).
    con_clave = excepcion_api(anthropic.BadRequestError, 400, "invalid_request_error", f"header raro {CLAVE}")
    assert CLAVE not in str(error_api(con_clave, "m", clave=CLAVE))
    assert "sk-ant-" not in str(error_api(RuntimeError("falló con sk-ant-otra-456"), "m"))


# --- etapa con Claude --------------------------------------------------------------------------

def ctx_claude(home, base, api: Any, sdk: Any, modo: str = "api_luego_max") -> Contexto:
    cfg = cfg_claude(home, modo)
    return Contexto(cfg=cfg, base=base, servicios=Servicios(clasificador=lambda c: ClasificadorClaude(
        c, cliente_api=api, cliente_sdk=sdk, facturacion=Facturacion(c, base))))


def test_etapa_con_claude_guarda_facturacion_y_costo(home, base):
    ctx = ctx_claude(home, base, ApiFalsa(sin_creditos_400()), FabricaSdk(init_sdk(), resultado_sdk()))
    crear_sesion(ctx)
    r = etapa.ejecutar(ctx, JOB)
    assert r.siguientes == [("revisar_transcript", {})]
    assert (r.facturacion, r.costo, r.motor) == ("max", 0.0123, "claude-sonnet-5-5")
    assert "equivalente API" in r.log
    assert fila(ctx)["materia_id"] == "otra"
    datos = json.loads((ctx.sesion_dir(SID) / "clasificacion.json").read_text(encoding="utf-8"))
    assert datos == {"materia_id": "otra", "confianza": 0.88, "motivo": "Temas de la otra materia.",
                     "modelo": "claude-sonnet-5-5", "motor": "claude", "esfuerzo": "medium", "facturacion": "max",
                     "costo": 0.0123, "uso": {"entrada": 900, "salida": 60, "cache_lectura": 300}}
    log = (ctx.sesion_dir(SID) / "pipeline.log").read_text(encoding="utf-8")
    assert "clasificar: API sin créditos" in log and "sigo con el plan Max" in log
    assert "clasificación: otra (confianza 0.88, claude-sonnet-5-5, US$ 0.0123 equivalente API" in log
    assert CLAVE not in log


def test_etapa_con_claude_por_api_queda_en_el_job(home, base):
    ctx = ctx_claude(home, base, ApiFalsa(mensaje()), FabricaSdk())
    crear_sesion(ctx)
    with base.conexion() as conn:
        cola.encolar(conn, "clasificar", SID)
        job = cola.tomar(conn, ["clasificar"])
    assert job is not None
    Worker(ctx).ejecutar_job(job)
    with base.conexion() as conn:
        f = conn.execute("SELECT estado, motor, costo, facturacion, log FROM jobs WHERE id = ?", (job.id,)).fetchone()
    costo = costo_api("claude-sonnet-5-5", {"entrada": 1200, "salida": 80})
    assert (f["estado"], f["motor"], f["facturacion"]) == ("hecho", "claude-sonnet-5-5", "api")
    assert f["costo"] == pytest.approx(costo) and "(API)" in f["log"]
    datos = json.loads((ctx.sesion_dir(SID) / "clasificacion.json").read_text(encoding="utf-8"))
    assert (datos["facturacion"], datos["costo"], datos["motor"]) == ("api", costo, "claude")


def test_etapa_deja_los_avisos_aunque_falle(home, base):
    sin_login = FabricaSdk(_texto_error("Not logged in · Please run /login"))
    ctx = ctx_claude(home, base, ApiFalsa(sin_creditos_400()), sin_login)
    crear_sesion(ctx)
    with pytest.raises(CuotaAgotada):
        etapa.ejecutar(ctx, JOB)
    log = (ctx.sesion_dir(SID) / "pipeline.log").read_text(encoding="utf-8")
    assert "clasificar: API sin créditos" in log and "sigo con el plan Max" in log
    assert estado_facturacion(base) == {"api": "sin_creditos", "max": "sin_login"}


def test_etapa_con_gemini_no_lleva_esfuerzo_ni_facturacion(ctx):
    ctx.cfg.gemini_api_key = "clave-falsa"
    cliente = ClienteFalso(respuesta_texto(RESPUESTA))
    ctx.servicios = Servicios(clasificador=lambda cfg: ClasificadorGemini(cfg, modelo="gemini-flash-latest",
                                                                          cliente=cliente))
    crear_sesion(ctx)
    r = etapa.ejecutar(ctx, JOB)
    assert r.facturacion is None and r.costo is None
    datos = json.loads((ctx.sesion_dir(SID) / "clasificacion.json").read_text(encoding="utf-8"))
    assert datos["motor"] == "gemini" and "esfuerzo" not in datos
    assert datos["facturacion"] is None and datos["costo"] is None
    assert "US$" not in (ctx.sesion_dir(SID) / "pipeline.log").read_text(encoding="utf-8")


def test_confianza_baja_con_el_worker_guarda_costo_y_facturacion_en_el_job(ctx):
    from classmer import cola
    from classmer.worker import Worker

    crear_sesion(ctx)
    r = ResultadoClasificacion("prueba", 0.3, "Poco texto.", "claude-sonnet-5-5", {"entrada": 900, "salida": 40},
                               costo=0.0022, facturacion="api")
    con_clasificador(ctx, ClasificadorFalso(r))
    w = Worker(ctx)
    try:
        with ctx.base.conexion() as conn:
            cola.encolar(conn, "clasificar", SID)
            j = cola.tomar(conn, ["clasificar"])
        w.ejecutar_job(j)
    finally:
        w.detener()
    with ctx.base.conexion() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (j.id,)).fetchone()
        estado = conn.execute("SELECT estado FROM sesiones WHERE id = ?", (SID,)).fetchone()["estado"]
    assert estado == "esperando_usuario"
    assert (job["estado"], job["costo"], job["facturacion"], job["motor"]) == (
        "hecho", 0.0022, "api", "claude-sonnet-5-5")


def _detalles_en_catalogo(base, modelo: str, esfuerzos) -> None:
    with base.conexion() as conn:
        conn.execute("INSERT OR REPLACE INTO modelos (proveedor, id, nombre, tareas, detalles, descubierto, presente, "
                     "origen) VALUES ('anthropic', ?, ?, '[\"clasificar\"]', ?, '2026-10-16T00:00:00-03:00', 1, 'api')",
                     (modelo, modelo, json.dumps({"esfuerzos": esfuerzos})))


@pytest.mark.parametrize(("esfuerzos", "pedido", "esperado", "aviso"), [
    (None, "medium", "medium", None),                                   # no se sabe: se pide igual
    (["low", "medium", "high", "max"], "xhigh", "high", "uso high"),    # p. ej. Sonnet 4.6: sin xhigh
    ([], "medium", None, "sin esfuerzo"),                               # p. ej. Haiku 4.5: no acepta esfuerzo
])
def test_claude_api_baja_el_esfuerzo_que_el_modelo_no_acepta(home, base, esfuerzos, pedido, esperado, aviso):
    cfg = cfg_claude(home)
    cfg.clasificar = cfg.clasificar.model_copy(update={"modelo": "claude-viejo-1", "esfuerzo": pedido})
    _detalles_en_catalogo(base, "claude-viejo-1", esfuerzos)
    api = ApiFalsa(mensaje())
    r = ClasificadorClaude(cfg, cliente_api=api, facturacion=Facturacion(cfg, base)).clasificar("x", MATERIAS)
    assert api.llamadas[0]["output_config"].get("effort") == esperado
    if aviso:
        assert any(aviso in a for a in r.avisos)
    else:
        assert r.avisos == []
