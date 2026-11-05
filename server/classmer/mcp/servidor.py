"""Servidor MCP (§11) en `/mcp`, dentro del mismo proceso que la API.

Transporte "Streamable HTTP" del protocolo MCP, en su forma más simple: sin sesiones y con
respuestas JSON (sin SSE), que es lo que alcanza para un servidor de herramientas.
- `POST /mcp`: un mensaje JSON-RPC 2.0 (o un lote). Una petición vuelve con `200` y su
  respuesta; sólo notificaciones (p. ej. `notifications/initialized`) vuelven con `202` vacío.
- `GET` y `DELETE /mcp`: `405` (no hay stream de eventos ni sesión que cerrar).
- Pide el mismo `Authorization: Bearer <token>` que la API. Un `Origin` de otra máquina da `403`
  (protección contra DNS rebinding, como pide la especificación).

No usa el SDK oficial a propósito: son ~150 líneas, sin dependencias nuevas que sincronizar en
producción. Las herramientas están en `herramientas.py`.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlsplit

import jsonschema
from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from classmer import __version__
from classmer.api.app import token_valido
from classmer.config import Config
from classmer.mcp.herramientas import HERRAMIENTAS, Entorno, ErrorHerramienta

log = logging.getLogger("classmer.mcp")

# La primera es la que se ofrece si el cliente pide una que no conocemos.
VERSIONES = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
RUTAS = ("/mcp", "/mcp/")
HOSTS_LOCALES = {"localhost", "127.0.0.1", "::1"}

INSTRUCCIONES = """\
Classmer guarda las clases de la facultad del usuario: el apunte (LaTeX/PDF) y el transcript fiel de \
cada clase grabada, por materia.
- Para encontrar dónde se habló de algo, usá buscar; para ver qué hay, listar_materias y listar_clases.
- leer_apunte trae el apunte entero (LaTeX) y sus secciones con minuto; transcript, lo que se dijo \
entre dos minutos (sirve para chequear qué dijo exactamente el profe). Si la clase pasó por la revisión, \
el transcript está limpio (sin muletillas, puntuado, el profe por su nombre) pero dice lo que el profe \
dijo, también sus errores; lo que va entre corchetes lo agregó la revisión y […] es lo que no se entiende.
- Al responder, citá la clase y el minuto: cada resultado trae un campo "cita" listo para usar.
- Las rutas de "archivos" son absolutas en la PC del usuario (PDF, .tex y transcript publicados, y en \
"adjuntos" las slides o notas del profe e imágenes que el usuario le adjuntó a la clase).
- pendientes lista parciales, tareas, fechas, dudas y avisos detectados en clase; marcar_item los \
cierra (sólo si el usuario lo pide)."""


class ErrorRPC(Exception):
    def __init__(self, codigo: int, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


def _error(id_: Any, codigo: int, mensaje: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": mensaje}}


def _texto(bloque: Any) -> dict[str, str]:
    if isinstance(bloque, str):
        return {"type": "text", "text": bloque}
    return {"type": "text", "text": json.dumps(bloque, ensure_ascii=False, indent=2)}


def _resultado_error(mensaje: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": mensaje}], "isError": True}


def llamar_herramienta(entorno: Entorno, nombre: Any, argumentos: Any) -> dict[str, Any]:
    """Resultado de `tools/call`. Los errores de uso vuelven como `isError` para que el modelo los vea."""
    herramienta = HERRAMIENTAS.get(nombre) if isinstance(nombre, str) else None
    if herramienta is None:
        raise ErrorRPC(-32602, f"herramienta desconocida: {nombre!r}")
    argumentos = {} if argumentos is None else argumentos
    if not isinstance(argumentos, dict):
        return _resultado_error("los argumentos tienen que ser un objeto JSON")
    try:
        jsonschema.validate(argumentos, herramienta.esquema)
    except jsonschema.ValidationError as e:
        donde = ".".join(str(p) for p in e.absolute_path)
        return _resultado_error(f"argumentos inválidos{f' ({donde})' if donde else ''}: {e.message}")
    try:
        bloques = herramienta.funcion(entorno, **argumentos)
    except ErrorHerramienta as e:
        return _resultado_error(str(e))
    except Exception as e:  # noqa: BLE001 — el cliente recibe el error, el server sigue
        log.exception("MCP: falló la herramienta %s", nombre)
        return _resultado_error(f"error interno en {nombre}: {e.__class__.__name__}: {e}")
    return {"content": [_texto(b) for b in bloques], "isError": False}


def _inicializar(params: dict[str, Any]) -> dict[str, Any]:
    pedida = params.get("protocolVersion")
    version = pedida if pedida in VERSIONES else VERSIONES[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "classmer", "title": "Classmer", "version": __version__},
        "instructions": INSTRUCCIONES,
    }


def despachar(entorno: Entorno, metodo: str, params: dict[str, Any]) -> dict[str, Any]:
    if metodo == "initialize":
        return _inicializar(params)
    if metodo == "ping":
        return {}
    if metodo == "tools/list":
        return {"tools": [h.definicion() for h in HERRAMIENTAS.values()]}
    if metodo == "tools/call":
        return llamar_herramienta(entorno, params.get("name"), params.get("arguments"))
    # No los anunciamos, pero algunos clientes preguntan igual: listas vacías antes que un error.
    if metodo == "resources/list":
        return {"resources": []}
    if metodo == "resources/templates/list":
        return {"resourceTemplates": []}
    if metodo == "prompts/list":
        return {"prompts": []}
    if metodo == "logging/setLevel":
        return {}
    raise ErrorRPC(-32601, f"método no soportado: {metodo}")


def atender(entorno: Entorno, mensaje: Any) -> dict[str, Any] | None:
    """Respuesta a un mensaje JSON-RPC, o None si no lleva (notificaciones y respuestas)."""
    if not isinstance(mensaje, dict) or mensaje.get("jsonrpc") != "2.0":
        id_ = mensaje.get("id") if isinstance(mensaje, dict) else None
        return _error(id_, -32600, "pedido JSON-RPC 2.0 inválido")
    metodo = mensaje.get("method")
    if metodo is None:
        return None  # una respuesta del cliente: este server no le pide nada
    if "id" not in mensaje:
        return None  # notificación
    id_ = mensaje["id"]
    params = mensaje.get("params")
    if not isinstance(metodo, str) or (params is not None and not isinstance(params, dict)):
        return _error(id_, -32600, "pedido JSON-RPC 2.0 inválido")
    try:
        return {"jsonrpc": "2.0", "id": id_, "result": despachar(entorno, metodo, params or {})}
    except ErrorRPC as e:
        return _error(id_, e.codigo, e.mensaje)
    except Exception:  # noqa: BLE001
        log.exception("MCP: error atendiendo %s", metodo)
        return _error(id_, -32603, "error interno del server")


def _origen_permitido(origen: str | None) -> bool:
    if not origen or origen == "null":
        return True
    try:
        return urlsplit(origen).hostname in HOSTS_LOCALES
    except ValueError:
        return False


def crear_router(cfg: Config) -> APIRouter:
    """Router de FastAPI con `/mcp`. La app lo monta con `app.include_router(crear_router(cfg))`."""
    router = APIRouter()

    def entorno(request: Request) -> Entorno:
        estado = request.app.state
        return Entorno(cfg=getattr(estado, "cfg", None) or cfg, base=estado.base)

    def rechazo(request: Request) -> Response | None:
        actual = getattr(request.app.state, "cfg", None) or cfg
        if not token_valido(actual.token, request.headers.get("authorization", "")):
            return JSONResponse(status_code=401, content={"detail": "token inválido"},
                                headers={"WWW-Authenticate": "Bearer"})
        if not _origen_permitido(request.headers.get("origin")):
            return JSONResponse(status_code=403, content={"detail": "origen no permitido"})
        return None

    async def post(request: Request) -> Response:
        if (r := rechazo(request)) is not None:
            return r
        try:
            cuerpo = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content=_error(None, -32700, "JSON inválido"))
        env = entorno(request)
        lote = isinstance(cuerpo, list)
        mensajes = cuerpo if lote else [cuerpo]
        if lote and not mensajes:
            return JSONResponse(status_code=400, content=_error(None, -32600, "lote vacío"))
        respuestas = [r for r in [await run_in_threadpool(atender, env, m) for m in mensajes] if r is not None]
        if not respuestas:
            return Response(status_code=202)
        return JSONResponse(content=respuestas if lote else respuestas[0])

    async def otro(request: Request) -> Response:
        if (r := rechazo(request)) is not None:
            return r
        return Response(status_code=405, headers={"Allow": "POST"})

    for ruta in RUTAS:
        router.add_api_route(ruta, post, methods=["POST"], include_in_schema=False)
        router.add_api_route(ruta, otro, methods=["GET", "DELETE"], include_in_schema=False)
    return router
