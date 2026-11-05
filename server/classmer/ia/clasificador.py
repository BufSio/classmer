"""Clasificar la materia de una clase (§6 etapa 5): con Claude (por defecto) o con Gemini Flash.

Sólo se usa cuando la sesión quedó en Auto (sin materia). Recibe extractos del transcript y la
lista de materias activas, y devuelve {materia_id | None, confianza 0..1, motivo} (JSON con esquema).

- `ClasificadorClaude` (clasificar.motor: claude; por defecto Sonnet 5.5): el medio lo decide la
  facturación (§7, `ia/facturacion.py`). Con la API, el SDK `anthropic` con salida JSON estructurada
  (la clave va explícita al cliente, nunca a os.environ); con el plan Max, una consulta de un turno
  por el Agent SDK (el plan sólo se alcanza con el login de Claude Code), sin herramientas y con el
  mismo esquema. Si la API se queda sin créditos o la clave no sirve, rehace la consulta con el plan
  Max (y al revés en max_luego_api); lo pasajero (429, 5xx, sin red) se pospone sin cambiar de medio.
- `ClasificadorGemini` (clasificar.motor: gemini): Gemini Flash con response_schema.

Los dos usan el mismo prompt (prompts/clasificar.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field

from classmer.config import MOTORES_CLASIFICAR
from classmer.db import Base
from classmer.ia import gemini
from classmer.ia.facturacion import API, MAX, Facturacion, SinCreditos, costo_api
from classmer.ia.modelos import esfuerzo_para
from classmer.pipeline import CuotaAgotada, ErrorEtapa
from classmer.util import ms_a_hhmmss

if TYPE_CHECKING:
    from classmer.config import Config

LIMITE_TRANSCRIPT = 30_000
LIMITE_PERFIL = 1_500
PRIMEROS_MS = 15 * 60 * 1000

# Claude por la API.
URL_API = "https://api.anthropic.com"
TIMEOUT_API_S = 120
REINTENTOS_API = 2           # los del SDK (429, 5xx, red); después decide la cola de jobs
MAX_TOKENS = 8000
ESPERA_RATE_LIMIT_S = 120    # 429 sin retry-after
ESPERA_SOBRECARGA_S = 180    # 5xx / 529 overloaded
ESPERA_SIN_RED_S = 120
# Claude por el plan Max (Agent SDK).
TIMEOUT_SDK_S = 300
MAX_TURNOS_SDK = 3           # la respuesta estructurada llega por la herramienta StructuredOutput
HERRAMIENTA_SALIDA = "StructuredOutput"  # la agrega el CLI con --json-schema; no es una herramienta de verdad
CARPETA_SDK = "_clasificar"  # cwd del CLI (vacío), dentro de jobs/
ENTORNO_SDK = {"DISABLE_AUTOUPDATER": "1"}   # nunca lleva ANTHROPIC_API_KEY: el plan va con el login
SISTEMA_SDK = ("Clasificás clases universitarias grabadas en una de las materias de la lista que te dan. "
               "Respondé sólo con el JSON pedido.")

T = TypeVar("T")


@dataclass
class MateriaCandidata:
    id: str
    nombre: str
    perfil: str | None = None


@dataclass
class ResultadoClasificacion:
    materia_id: str | None
    confianza: float
    motivo: str
    modelo: str
    uso: dict[str, int] = field(default_factory=dict)
    costo: float | None = None              # US$: real con la API, "equivalente API" con el plan Max
    facturacion: str | None = None          # api | max (sólo Claude)
    avisos: list[str] = field(default_factory=list)  # para pipeline.log (cambio de medio, etc.)


@runtime_checkable
class Clasificador(Protocol):
    def clasificar(self, transcript: str, materias: list[MateriaCandidata], *,
                   duracion_ms: int | None = None) -> ResultadoClasificacion:
        ...


def armar_prompt(transcript: str, materias: list[MateriaCandidata], duracion_ms: int | None = None) -> str:
    """prompts/clasificar.md con las materias, los ids válidos, el transcript y la duración."""
    return gemini.cargar_prompt(
        "clasificar",
        materias=formatear_materias(materias),
        ids=", ".join(m.id for m in materias),
        transcript=transcript.strip() or "(vacío)",
        duracion=ms_a_hhmmss(duracion_ms) if duracion_ms else "desconocida",
    )


def crear_clasificador(cfg: Config) -> Clasificador:
    """El clasificador de `clasificar.motor` (lo usa la etapa si no hay uno inyectado)."""
    motor = cfg.clasificar.motor
    if motor == "claude":
        return ClasificadorClaude(cfg)
    if motor == "gemini":
        return ClasificadorGemini(cfg, modelo=cfg.clasificar.modelo)
    raise ErrorEtapa(f"clasificar.motor desconocido: {motor!r} (opciones: {', '.join(MOTORES_CLASIFICAR)})",
                     reintentable=False)


# --- Gemini --------------------------------------------------------------------------------------

class RespuestaClasificacion(BaseModel):
    """Esquema de salida (response_schema)."""

    materia_id: str | None = Field(description="id de la materia de la lista, o null")
    confianza: float = Field(description="Confianza entre 0 y 1")
    motivo: str = Field(description="Evidencia concreta en una o dos oraciones")


class ClasificadorGemini:
    """Gemini Flash con salida JSON. `cliente` se inyecta en los tests."""

    PROMPT = "clasificar"
    motor = "gemini"
    esfuerzo = None

    def __init__(self, cfg: Config, *, modelo: str | None = None, cliente: Any = None):
        self.cfg = cfg
        self.modelo = modelo or cfg.clasificar.modelo
        self._cliente = cliente

    def _cliente_listo(self) -> Any:
        if self._cliente is None:
            self._cliente = gemini.crear_cliente(self.cfg, timeout_s=gemini.TIMEOUT_CLASIFICAR_S)
        return self._cliente

    def prompt(self, transcript: str, materias: list[MateriaCandidata],
               duracion_ms: int | None = None) -> str:
        return armar_prompt(transcript, materias, duracion_ms)

    def clasificar(self, transcript: str, materias: list[MateriaCandidata], *,
                   duracion_ms: int | None = None) -> ResultadoClasificacion:
        from google.genai import types

        cliente = self._cliente_listo()
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RespuestaClasificacion,
        )
        respuesta = gemini.llamar(cliente, self.modelo, self.prompt(transcript, materias, duracion_ms),
                                  config, clave=self.cfg.gemini_api_key)
        return interpretar(gemini.texto_respuesta(respuesta), self.modelo, gemini.uso(respuesta))


# --- Claude --------------------------------------------------------------------------------------

def esquema(ids: list[str]) -> dict[str, Any]:
    """Esquema JSON de la respuesta de Claude (output_config.format de la API y --json-schema del CLI):
    materia_id es una de las ids activas o null. Sin minimum/maximum (la API no los admite):
    `interpretar` acota la confianza."""
    return {
        "type": "object",
        "properties": {
            "materia_id": {"anyOf": [{"type": "string", "enum": list(ids)}, {"type": "null"}],
                           "description": "id de la materia de la lista, o null"},
            "confianza": {"type": "number", "description": "Confianza entre 0 y 1"},
            "motivo": {"type": "string", "description": "Evidencia concreta en una o dos oraciones"},
        },
        "required": ["materia_id", "confianza", "motivo"],
        "additionalProperties": False,
    }


@dataclass
class _RespuestaSdk:
    """Lo que dejó la consulta por el Agent SDK (para el resultado o para entender la falla)."""

    resultado: Any = None                   # ResultMessage
    clave: str | None = None                # apiKeySource del init del CLI ("none" con el login)
    error_modelo: str | None = None         # AssistantMessage.error (rate_limit, billing_error, …)
    rechazado: bool = False                 # RateLimitEvent "rejected": cupo del plan agotado
    resets_at: float | None = None
    stderr: deque[str] = field(default_factory=lambda: deque(maxlen=20))


class ClienteClaude:
    """Lo común de los pedidos de un turno a Claude por el plan Max (Agent SDK): la consulta y cómo se
    traduce su falla. Lo usan el clasificador y la revisión del transcript (ia/claude_json.py); cada
    uno pone `cfg`, `modelo` y `_cliente_sdk`."""

    cfg: Config
    modelo: str
    _cliente_sdk: Callable[[Any], Any] | None = None

    async def _consultar(self, opciones: Any, prompt: str, r: _RespuestaSdk) -> None:
        from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, SystemMessage

        if self._cliente_sdk is not None:
            cliente = self._cliente_sdk(opciones)
        else:
            from claude_agent_sdk import ClaudeSDKClient

            cliente = ClaudeSDKClient(opciones)
        await conectar_sin_claves(cliente)
        try:
            await cliente.query(prompt)
            async for m in cliente.receive_response():
                if isinstance(m, SystemMessage):
                    if m.subtype == "init":
                        r.clave = str(m.data.get("apiKeySource") or "") or None
                elif isinstance(m, AssistantMessage):
                    if m.error:
                        r.error_modelo = m.error
                elif isinstance(m, RateLimitEvent):
                    info = m.rate_limit_info
                    if info.status == "rejected":
                        r.rechazado = True
                        if info.resets_at:
                            r.resets_at = float(info.resets_at)
                elif isinstance(m, ResultMessage):
                    r.resultado = m
        finally:
            with contextlib.suppress(Exception):
                await cliente.disconnect()

    def _falla_sdk(self, r: _RespuestaSdk, excepcion: BaseException | None = None) -> ErrorEtapa:
        """La falla del plan Max: cupo agotado o sin login cambian de medio (SinCreditos); lo pasajero
        se pospone (CuotaAgotada); lo demás es un error de la etapa."""
        from classmer.ia.agente_apunte import ESPERA_LOGIN_S, espera_cuota

        res = r.resultado
        partes: list[str] = []
        if excepcion is not None:
            partes.append(f"{excepcion.__class__.__name__}: {excepcion}")
        if res is not None:
            partes += [*(res.errors or []), res.result or "", res.subtype or ""]
        if r.error_modelo:
            partes.append(f"error del modelo: {r.error_modelo}")
        if r.stderr:
            partes.append("stderr: " + " | ".join(list(r.stderr)[-5:]))
        texto = sin_secretos(" ".join(" ".join(p.split()) for p in partes if p), self.cfg.anthropic_api_key)
        motivo = texto[:300] or "el CLI no devolvió resultado"
        estado = (res.api_error_status if res is not None else None) or 0
        prefijo = f"Claude por el plan Max ({self.modelo})"
        caida = r.error_modelo == "server_error" or estado >= 500 or bool(_CAIDA_RE.search(texto))

        def sin_cupo() -> SinCreditos:
            hasta = r.resets_at or _epoch_en(texto)
            return SinCreditos(MAX, "sin_cupo", motivo, espera_s=espera_cuota(texto, r.resets_at), hasta=hasta)

        if r.error_modelo == "billing_error" or estado == 402 or _CUPO_RE.search(texto):
            return sin_cupo()
        if r.error_modelo == "authentication_failed" or estado == 401 or _LOGIN_RE.search(texto):
            return SinCreditos(MAX, "sin_login", motivo, espera_s=ESPERA_LOGIN_S)
        # Un RateLimitEvent "rejected" es el cupo del plan, salvo que la falla sea una caída de la API
        # (529, 5xx, sin red): eso es pasajero y no tiene que dejar el plan Max marcado.
        if r.rechazado and not caida:
            return sin_cupo()
        if r.error_modelo == "rate_limit" or estado == 429 or _RATE_RE.search(texto):
            return CuotaAgotada(f"{prefijo}: rate limit ({motivo})", espera_s=ESPERA_RATE_LIMIT_S)
        if caida:
            return CuotaAgotada(f"{prefijo}: la API está sobrecargada o no responde ({motivo})",
                                espera_s=ESPERA_SOBRECARGA_S)
        return ErrorEtapa(f"{prefijo}: {motivo}", reintentable=True)


class ClasificadorClaude(ClienteClaude):
    """Claude con salida JSON estructurada, por la API o por el plan Max según la facturación.

    Para los tests: `cliente_api` (objeto con `.messages.create(**kw)`), `cliente_sdk` (fábrica
    `opciones -> cliente` con la interfaz de ClaudeSDKClient) y `facturacion` (si no, se abre la base
    de la config)."""

    motor = "claude"

    def __init__(self, cfg: Config, *, modelo: str | None = None, esfuerzo: str | None = None,
                 cliente_api: Any = None, cliente_sdk: Callable[[Any], Any] | None = None,
                 facturacion: Facturacion | None = None):
        self.cfg = cfg
        self.modelo = modelo or cfg.clasificar.modelo
        self.esfuerzo = esfuerzo if esfuerzo is not None else cfg.clasificar.esfuerzo
        self._cliente_api = cliente_api
        self._cliente_sdk = cliente_sdk
        self._facturacion = facturacion

    def prompt(self, transcript: str, materias: list[MateriaCandidata],
               duracion_ms: int | None = None) -> str:
        return armar_prompt(transcript, materias, duracion_ms)

    def clasificar(self, transcript: str, materias: list[MateriaCandidata], *,
                   duracion_ms: int | None = None) -> ResultadoClasificacion:
        ids = [m.id for m in materias]
        if not ids:
            return ResultadoClasificacion(None, 0.0, "no hay materias para elegir", self.modelo)
        texto = self.prompt(transcript, materias, duracion_ms)
        avisos: list[str] = []
        facturacion = self._facturacion or Facturacion(self.cfg)
        # Un esfuerzo que el modelo no acepta (según el catálogo) daría un 400: se baja o se omite.
        base = getattr(facturacion, "base", None) or Base(self.cfg.db_path)
        esfuerzo, aviso = esfuerzo_para(base, self.modelo, self.esfuerzo)
        if aviso:
            avisos.append(aviso)

        def correr(medio: str) -> ResultadoClasificacion:
            if medio == API:
                return self._por_api(texto, ids, esfuerzo)
            return self._por_sdk(texto, ids, avisos, esfuerzo)

        try:
            r, medio = facturacion.ejecutar("clasificar", correr, log_tarea=avisos.append)
        except Exception as e:
            # La etapa los deja en pipeline.log aunque la clasificación falle.
            with contextlib.suppress(AttributeError):
                setattr(e, "avisos", list(avisos))  # noqa: B010
            raise
        r.facturacion = medio
        r.avisos = list(avisos)
        return r

    # --- API ---
    def _por_api(self, prompt: str, ids: list[str], esfuerzo: str | None) -> ResultadoClasificacion:
        clave = self.cfg.anthropic_api_key
        if not clave:
            raise SinCreditos(API, "clave_invalida", "no hay ANTHROPIC_API_KEY en Classmer-data\\.env")
        cliente = self._cliente_api if self._cliente_api is not None else crear_cliente_api(clave)
        config: dict[str, Any] = {"format": {"type": "json_schema", "schema": esquema(ids)}}
        if esfuerzo:
            config["effort"] = esfuerzo
        try:
            respuesta = cliente.messages.create(
                model=self.modelo, max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}], output_config=config)
        except Exception as e:  # noqa: BLE001 - se traduce todo lo que venga del SDK
            raise error_api(e, self.modelo, clave=clave) from None
        uso = uso_claude(getattr(respuesta, "usage", None))
        if getattr(respuesta, "stop_reason", None) == "refusal":
            detalles = getattr(respuesta, "stop_details", None)
            categoria = getattr(detalles, "category", None) if detalles is not None else None
            r = ResultadoClasificacion(
                None, 0.0, "el modelo no quiso clasificar la clase" + (f" ({categoria})" if categoria else ""),
                self.modelo, uso)
        else:
            texto = next((b.text for b in getattr(respuesta, "content", None) or []
                          if getattr(b, "type", None) == "text"), "")
            r = interpretar(texto, self.modelo, uso)
        r.costo = costo_api(self.modelo, uso)
        return r

    # --- plan Max (Agent SDK) ---
    def opciones_sdk(self, ids: list[str], r: _RespuestaSdk | None = None, esfuerzo: str | None = ...) -> Any:
        """Una consulta de un turno, sin herramientas, sin la config del usuario (ni CLAUDE.md, ni MCP,
        ni skills) y sin la API key en el entorno de la corrida."""
        from claude_agent_sdk import ClaudeAgentOptions

        from classmer.ia.agente_apunte import PROHIBIDAS

        cwd = self.cfg.jobs_dir / CARPETA_SDK
        cwd.mkdir(parents=True, exist_ok=True)
        clave = self.cfg.anthropic_api_key

        def stderr(linea: str) -> None:
            if r is not None:
                r.stderr.append(sin_secretos(linea, clave)[:300])

        return ClaudeAgentOptions(
            tools=[],
            # Sólo la herramienta de la respuesta estructurada: con dontAsk, lo no aprobado se deniega.
            allowed_tools=[HERRAMIENTA_SALIDA],
            disallowed_tools=list(PROHIBIDAS),
            system_prompt=SISTEMA_SDK,
            permission_mode="dontAsk",
            cwd=str(cwd),
            setting_sources=[],
            skills=[],
            strict_mcp_config=True,
            model=self.modelo,
            effort=self.esfuerzo if esfuerzo is ... else esfuerzo,
            max_turns=MAX_TURNOS_SDK,
            output_format={"type": "json_schema", "schema": esquema(ids)},
            verbatim_prompts=True,       # el transcript no puede disparar @archivo ni /comandos
            env=dict(ENTORNO_SDK),
            stderr=stderr,
        )

    def _por_sdk(self, prompt: str, ids: list[str], avisos: list[str],
                 esfuerzo: str | None = ...) -> ResultadoClasificacion:
        r = _RespuestaSdk()
        opciones = self.opciones_sdk(ids, r, esfuerzo)
        try:
            _correr(asyncio.wait_for(self._consultar(opciones, prompt, r), TIMEOUT_SDK_S))
        except TimeoutError:
            raise ErrorEtapa(f"Claude por el plan Max ({self.modelo}) no respondió en "
                             f"{TIMEOUT_SDK_S // 60} min", reintentable=True) from None
        except Exception as e:  # noqa: BLE001 - el CLI puede fallar de muchas formas
            raise self._falla_sdk(r, e) from None
        if r.clave and r.clave.lower() != "none":
            avisos.append(f"el CLI de Claude Code dice que usó una API key ({r.clave}) aunque la "
                          "facturación es el plan Max")
        res = r.resultado
        if res is None or res.is_error or res.subtype != "success":
            raise self._falla_sdk(r)
        uso = uso_claude(res.usage)
        datos = res.structured_output
        if isinstance(datos, dict):
            texto = json.dumps(datos, ensure_ascii=False)
        elif isinstance(datos, str) and datos.strip():   # por si el CLI lo manda como texto JSON
            texto = datos
        else:
            texto = res.result or ""
        resultado = interpretar(texto, self.modelo, uso)
        resultado.costo = res.total_cost_usd
        return resultado


# API sin créditos: "Your credit balance is too low…" (400), billing_error (402) y el tope de gasto del
# workspace ("You have reached your specified API usage limits…", 400): ninguno se arregla esperando
# un rato, así que cambian de medio (como en agente_apunte).
_CREDITOS_API_RE = re.compile(r"credit balance|billing[_ ]error|insufficient (?:credit|fund)|payment required|"
                              r"api usage limits?", re.I)
# Textos de error del CLI (plan Max): ver también agente_apunte.
_CUPO_RE = re.compile(r"usage limit|limit reached|hit your (?:\w+ )?limit|out of (?:extra )?usage|"
                      r"(?:weekly|session|opus|sonnet|fable) limit|quota|"
                      r"credit balance|billing|insufficient (?:credit|fund)|payment required|"
                      r"\bAPI Error:\s*402\b", re.I)
_LOGIN_RE = re.compile(r"not logged in|/login\b|invalid api key|invalid x-api-key|authentication|oauth token|"
                       r"unauthorized|\bAPI Error:\s*401\b", re.I)
_RATE_RE = re.compile(r"rate[ _-]?limit|too many requests|\b429\b", re.I)
_CAIDA_RE = re.compile(r"overloaded|\b529\b|API Error:\s*5\d\d\b|connection error|ECONNRESET|ETIMEDOUT|"
                       r"timed out|socket hang up", re.I)
_CLAVE_RE = re.compile(r"sk-ant-[\w-]+")


def _epoch_en(texto: str) -> float | None:
    """El '|<epoch>' de 'Claude AI usage limit reached|1760000000'."""
    m = re.search(r"\|(\d{10})\b", texto)
    return float(m.group(1)) if m else None


async def conectar_sin_claves(cliente: Any) -> None:
    """Lanza el CLI sin claves de API ni variables de una sesión de Claude Code padre en el entorno
    heredado (como el apunte en modo Max): el plan Max va con el login de Claude Code."""
    from classmer.ia.agente_apunte import VARIABLES_FUERA, _entorno_lock

    with _entorno_lock:
        sacadas = {k: os.environ.pop(k) for k in VARIABLES_FUERA if k in os.environ}
        try:
            await cliente.connect()
        finally:
            os.environ.update(sacadas)


def _correr(coro: Coroutine[Any, Any, T]) -> T:
    """El SDK necesita un loop propio; en Windows, Proactor (subprocess)."""
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
            return runner.run(coro)
    with asyncio.Runner() as runner:
        return runner.run(coro)


def crear_cliente_api(clave: str) -> Any:
    """Cliente de la API de Anthropic con la clave explícita: así el SDK no mira ANTHROPIC_API_KEY,
    ANTHROPIC_AUTH_TOKEN ni ANTHROPIC_BASE_URL del entorno."""
    import anthropic

    return anthropic.Anthropic(api_key=clave, base_url=URL_API, timeout=TIMEOUT_API_S,
                               max_retries=REINTENTOS_API)


def error_api(e: BaseException, modelo: str, *, clave: str | None = None) -> ErrorEtapa:
    """Excepción del SDK `anthropic` → excepción del pipeline (§7).

    - 401 → SinCreditos clave_invalida; 402, billing_error, "credit balance" o el tope de gasto del
      workspace ("API usage limits") → SinCreditos sin_creditos (los dos cambian de medio).
    - 429 → CuotaAgotada (retry-after o 2 min); 5xx/529 → CuotaAgotada de 3 min; sin red, timeout, 408 o
      409 → CuotaAgotada de 2 min. No cambian de medio.
    - 404 (modelo inexistente), 400 y el resto de 4xx → ErrorEtapa no reintentable.
    Nunca deja la clave en el mensaje."""
    if isinstance(e, ErrorEtapa):
        return e
    import anthropic

    if isinstance(e, anthropic.APIStatusError):
        estado = int(getattr(e, "status_code", 0) or 0)
        tipo = str(getattr(e, "type", None) or "")
        mensaje = " ".join(sin_secretos(_mensaje_api(e), clave).split())[:300]
        detalle = f"Claude API {estado}{f' {tipo}' if tipo else ''} ({modelo}): {mensaje}"
        if isinstance(e, anthropic.AuthenticationError) or estado == 401:
            return SinCreditos(API, "clave_invalida", detalle)
        if estado == 402 or tipo == "billing_error" or _CREDITOS_API_RE.search(mensaje):
            return SinCreditos(API, "sin_creditos", detalle)
        if isinstance(e, anthropic.RateLimitError) or estado == 429:
            return CuotaAgotada(detalle, espera_s=_retry_after(e))
        if estado >= 500 or tipo == "overloaded_error":
            return CuotaAgotada(detalle, espera_s=ESPERA_SOBRECARGA_S)
        if estado in (408, 409):   # timeout / conflicto del lado de la API: el SDK ya reintentó
            return CuotaAgotada(detalle, espera_s=ESPERA_SIN_RED_S)
        return ErrorEtapa(detalle, reintentable=False)
    if isinstance(e, anthropic.APIConnectionError):  # incluye APITimeoutError
        que = "no respondió a tiempo" if isinstance(e, anthropic.APITimeoutError) else "sin conexión"
        return CuotaAgotada(f"Claude API ({modelo}): {que} ({e.__class__.__name__})", espera_s=ESPERA_SIN_RED_S)
    texto = " ".join(sin_secretos(str(e) or e.__class__.__name__, clave).split())[:300]
    return ErrorEtapa(f"Claude API ({modelo}): {e.__class__.__name__}: {texto}", reintentable=True)


def _mensaje_api(e: Any) -> str:
    """El error.message del cuerpo (más legible que el repr que arma el SDK)."""
    cuerpo = getattr(e, "body", None)
    if isinstance(cuerpo, dict):
        error = cuerpo.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return str(getattr(e, "message", "") or e)


def _retry_after(e: Any) -> float:
    try:
        valor = float(e.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return float(ESPERA_RATE_LIMIT_S)
    return float(min(max(valor, 10.0), 3600.0))


def uso_claude(usage: Any) -> dict[str, int]:
    """Tokens de `usage` (objeto de la API o dict del Agent SDK) con los nombres de `costo_api`."""
    if usage is None:
        return {}
    campos = {"entrada": "input_tokens", "salida": "output_tokens",
              "cache_escritura": "cache_creation_input_tokens", "cache_lectura": "cache_read_input_tokens"}
    salida: dict[str, int] = {}
    for nombre, attr in campos.items():
        v = usage.get(attr) if isinstance(usage, dict) else getattr(usage, attr, None)
        if isinstance(v, int) and not isinstance(v, bool) and (v or nombre in ("entrada", "salida")):
            salida[nombre] = v
    return salida


def sin_secretos(texto: str, clave: str | None = None) -> str:
    """Por las dudas: ninguna clave en un mensaje que va a la base, al log o al panel."""
    if clave:
        texto = texto.replace(clave, "[oculto]")
    return _CLAVE_RE.sub("[oculto]", texto)


# --- comunes -------------------------------------------------------------------------------------

def interpretar(texto: str, modelo: str, uso: dict[str, int] | None = None) -> ResultadoClasificacion:
    """JSON del modelo → ResultadoClasificacion (tolerante: lo que no se entiende es confianza 0)."""
    datos = gemini.leer_json_objeto(texto or "")
    if datos is None:
        return ResultadoClasificacion(None, 0.0, "respuesta del modelo ilegible", modelo, uso or {})
    materia_id = datos.get("materia_id")
    if isinstance(materia_id, str):
        materia_id = materia_id.strip() or None
        if materia_id and materia_id.lower() in ("null", "none", "ninguna"):
            materia_id = None
    else:
        materia_id = None
    try:
        confianza = float(datos.get("confianza") or 0.0)
    except (TypeError, ValueError):
        confianza = 0.0
    if confianza > 1 and confianza <= 100:  # por si responde en porcentaje
        confianza /= 100
    confianza = min(max(confianza, 0.0), 1.0)
    motivo = str(datos.get("motivo") or "").strip() or "sin motivo"
    return ResultadoClasificacion(materia_id, round(confianza, 3), motivo, modelo, uso or {})


def formatear_materias(materias: list[MateriaCandidata]) -> str:
    bloques = []
    for m in materias:
        perfil = (m.perfil or "").strip() or "(sin perfil: usá sólo el nombre)"
        bloques.append(f"### id: {m.id}\nNombre: {m.nombre}\n\n{perfil}")
    return "\n\n".join(bloques)


def candidatas(cfg: Config) -> list[MateriaCandidata]:
    """Materias activas de la config con el comienzo de su perfil (~1500 caracteres, priorizando
    programa). Las inactivas no se proponen; a mano se pueden seguir asignando."""
    salida = []
    for m in cfg.materias:
        if not m.activa:
            continue
        perfil = gemini.leer_perfil(m.ruta_perfil())
        salida.append(MateriaCandidata(
            id=m.id, nombre=m.nombre,
            perfil=gemini.extracto_perfil(perfil, LIMITE_PERFIL, gemini.CLAVES_CLASIFICAR),
        ))
    return salida


def extracto_transcript(segmentos: list[dict[str, Any]], limite: int = LIMITE_TRANSCRIPT) -> str:
    """Primeros ~15 min y algo del medio, como líneas '[hh:mm:ss] texto', en ≤ `limite` caracteres."""
    lineas = [(int(s.get("inicio_ms") or 0), f"[{ms_a_hhmmss(s.get('inicio_ms') or 0)}] "
                                              f"{str(s.get('texto') or '').strip()}")
              for s in segmentos if str(s.get("texto") or "").strip()]
    lineas.sort(key=lambda x: x[0])
    if not lineas:
        return ""
    completo = "\n".join(t for _, t in lineas)
    if len(completo) <= limite:
        return completo
    cupo_inicio = int(limite * 2 / 3)
    primeras: list[str] = []
    usado = 0
    idx_fin = 0
    for i, (ms, t) in enumerate(lineas):
        if ms >= PRIMEROS_MS or usado + len(t) + 1 > cupo_inicio:
            break
        primeras.append(t)
        usado += len(t) + 1
        idx_fin = i + 1
    restante = limite - usado - 10
    fin_total = lineas[-1][0]
    medio_ms = fin_total // 2
    inicio_medio = next((i for i, (ms, _) in enumerate(lineas) if ms >= medio_ms), len(lineas))
    inicio_medio = max(inicio_medio, idx_fin)
    medio: list[str] = []
    for _, t in lineas[inicio_medio:]:
        if len(t) + 1 > restante:
            break
        medio.append(t)
        restante -= len(t) + 1
    partes = ["\n".join(primeras)]
    if medio:
        partes.append("[…]\n" + "\n".join(medio))
    return "\n".join(p for p in partes if p)[:limite]
