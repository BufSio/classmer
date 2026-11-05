"""Un pedido de un turno a Claude con salida JSON estructurada (§7), por la API o por el plan Max.

Lo usa la revisión del transcript (§15), con la facturación de siempre (`claude.facturacion`,
ia/facturacion.py): con la API, el SDK `anthropic` en streaming (las salidas son largas) con
`output_config.format`; con el plan Max, una consulta de un turno por el Agent SDK, sin herramientas y
con el mismo esquema (como clasificar). El costo es el real con la API y el "equivalente API" con el
plan Max.

Para los tests: `cliente_api` (objeto con `.messages.stream(**kw)` como context manager con
`get_final_message()`), `cliente_sdk` (fábrica `opciones -> cliente`) y `facturacion`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from classmer.db import Base
from classmer.ia import gemini
from classmer.ia.clasificador import (
    ENTORNO_SDK,
    HERRAMIENTA_SALIDA,
    ClienteClaude,
    _correr,
    _RespuestaSdk,
    crear_cliente_api,
    error_api,
    sin_secretos,
    uso_claude,
)
from classmer.ia.facturacion import API, Facturacion, SinCreditos, costo_api
from classmer.ia.modelos import esfuerzo_para
from classmer.pipeline import ErrorEtapa

if TYPE_CHECKING:
    from classmer.config import Config

MAX_TOKENS = 64_000          # salidas largas (un tramo de transcript entero): siempre en streaming
TIMEOUT_API_S = 900
TIMEOUT_SDK_S = 900
MAX_TURNOS_SDK = 3


class RespuestaInvalida(ErrorEtapa):
    """El modelo no devolvió el JSON pedido (cortado por max_tokens, rechazo, ilegible). No se reintenta
    igual: quien llama decide (achicar el pedido o seguir sin ese tramo)."""

    def __init__(self, mensaje: str, *, uso: dict[str, int] | None = None, costo: float | None = None,
                 cortada: bool = False):
        super().__init__(mensaje, reintentable=False)
        self.uso = uso or {}
        self.costo = costo
        self.cortada = cortada


@dataclass
class RespuestaJSON:
    datos: dict[str, Any]
    modelo: str
    facturacion: str | None = None           # api | max
    costo: float | None = None
    uso: dict[str, int] = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)


class ConsultaClaude(ClienteClaude):
    motor = "claude"

    def __init__(self, cfg: Config, *, tarea: str, modelo: str, esfuerzo: str | None = None, sistema: str = "",
                 cliente_api: Any = None, cliente_sdk: Callable[[Any], Any] | None = None,
                 facturacion: Facturacion | None = None, max_tokens: int = MAX_TOKENS):
        self.cfg = cfg
        self.tarea = tarea
        self.modelo = modelo
        self.esfuerzo = esfuerzo
        self.sistema = sistema
        self.max_tokens = max_tokens
        self._cliente_api = cliente_api
        self._cliente_sdk = cliente_sdk
        self._facturacion = facturacion

    def consultar(self, prompt: str, esquema: dict[str, Any]) -> RespuestaJSON:
        """Un pedido con el primer medio de facturación disponible; si se queda sin créditos o sin cupo,
        lo rehace con el otro. Lanza CuotaAgotada (pospone), ErrorEtapa o RespuestaInvalida."""
        avisos: list[str] = []
        facturacion = self._facturacion or Facturacion(self.cfg)
        base = getattr(facturacion, "base", None) or Base(self.cfg.db_path)
        esfuerzo, aviso = esfuerzo_para(base, self.modelo, self.esfuerzo)
        if aviso:
            avisos.append(aviso)

        def correr(medio: str) -> RespuestaJSON:
            if medio == API:
                return self._por_api(prompt, esquema, esfuerzo)
            return self._por_sdk(prompt, esquema, avisos, esfuerzo)

        r, medio = facturacion.ejecutar(self.tarea, correr, log_tarea=avisos.append)
        r.facturacion = medio
        r.avisos = avisos + r.avisos
        return r

    # --- API ---
    def _por_api(self, prompt: str, esquema: dict[str, Any], esfuerzo: str | None) -> RespuestaJSON:
        clave = self.cfg.anthropic_api_key
        if not clave:
            raise SinCreditos(API, "clave_invalida", "no hay ANTHROPIC_API_KEY en Classmer-data\\.env")
        cliente = self._cliente_api if self._cliente_api is not None else crear_cliente_api(clave)
        config: dict[str, Any] = {"format": {"type": "json_schema", "schema": esquema}}
        if esfuerzo:
            config["effort"] = esfuerzo
        pedido: dict[str, Any] = {"model": self.modelo, "max_tokens": self.max_tokens,
                                  "messages": [{"role": "user", "content": prompt}], "output_config": config}
        if self.sistema:
            pedido["system"] = self.sistema
        try:
            if self._cliente_api is None:
                cliente = cliente.with_options(timeout=TIMEOUT_API_S)
            with cliente.messages.stream(**pedido) as stream:
                respuesta = stream.get_final_message()
        except Exception as e:  # noqa: BLE001 - se traduce todo lo que venga del SDK
            raise error_api(e, self.modelo, clave=clave) from None
        uso = uso_claude(getattr(respuesta, "usage", None))
        costo = costo_api(self.modelo, uso)
        motivo = getattr(respuesta, "stop_reason", None)
        if motivo == "refusal":
            detalles = getattr(respuesta, "stop_details", None)
            categoria = getattr(detalles, "category", None) if detalles is not None else None
            raise RespuestaInvalida(f"el modelo no quiso hacerlo{f' ({categoria})' if categoria else ''}",
                                    uso=uso, costo=costo)
        texto = "".join(b.text for b in getattr(respuesta, "content", None) or []
                        if getattr(b, "type", None) == "text")
        return self._interpretar(texto, uso, costo, cortada=motivo == "max_tokens")

    # --- plan Max (Agent SDK) ---
    def opciones_sdk(self, esquema: dict[str, Any], r: _RespuestaSdk | None = None,
                     esfuerzo: str | None = None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        from classmer.ia.agente_apunte import PROHIBIDAS

        cwd = self.cfg.jobs_dir / f"_{self.tarea}"
        cwd.mkdir(parents=True, exist_ok=True)
        clave = self.cfg.anthropic_api_key

        def stderr(linea: str) -> None:
            if r is not None:
                r.stderr.append(sin_secretos(linea, clave)[:300])

        return ClaudeAgentOptions(
            tools=[],
            allowed_tools=[HERRAMIENTA_SALIDA],
            disallowed_tools=list(PROHIBIDAS),
            system_prompt=self.sistema or None,
            permission_mode="dontAsk",
            cwd=str(cwd),
            setting_sources=[],
            skills=[],
            strict_mcp_config=True,
            model=self.modelo,
            effort=esfuerzo,
            max_turns=MAX_TURNOS_SDK,
            output_format={"type": "json_schema", "schema": esquema},
            verbatim_prompts=True,       # el transcript no puede disparar @archivo ni /comandos
            env=dict(ENTORNO_SDK),
            stderr=stderr,
            max_buffer_size=64 * 1024 * 1024,
        )

    def _por_sdk(self, prompt: str, esquema: dict[str, Any], avisos: list[str],
                 esfuerzo: str | None) -> RespuestaJSON:
        r = _RespuestaSdk()
        opciones = self.opciones_sdk(esquema, r, esfuerzo)
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
        elif isinstance(datos, str) and datos.strip():
            texto = datos
        else:
            texto = res.result or ""
        return self._interpretar(texto, uso, res.total_cost_usd)

    def _interpretar(self, texto: str, uso: dict[str, int], costo: float | None, *,
                     cortada: bool = False) -> RespuestaJSON:
        datos = gemini.leer_json_objeto(texto or "")
        if datos is None:
            que = "se cortó por el tope de tokens" if cortada else "no es el JSON pedido"
            raise RespuestaInvalida(f"la respuesta de {self.modelo} {que}", uso=uso, costo=costo, cortada=cortada)
        return RespuestaJSON(datos=datos, modelo=self.modelo, costo=costo, uso=uso)
