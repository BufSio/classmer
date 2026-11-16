"""Panel web (§13): lo sirve el mismo server en /panel; desde la PC sin nada y desde la red con contraseña.

- `AccesoPanel` decide quién entra (ninguno usa el token de la app):
  - desde la PC (127.0.0.1 o ::1), sin contraseña, con un Host de loopback (contra DNS rebinding);
  - desde la red (la LAN o el WireGuard del router), sólo con la cookie de sesión que da
    /panel/entrar con la contraseña, y con un Host que sea una IP o un nombre de la LAN. Sin
    contraseña puesta, 403 como antes. /panel/acceso (poner, cambiar o quitar la contraseña y
    cerrar las sesiones) da 403 desde la red aunque haya sesión;
  - por el dominio público (`panel.dominio_publico`, con Cloudflare Tunnel y Access adelante): es
    "desde la red" aunque el socket sea 127.0.0.1, con un candado más, el JWT de Access, que se
    valida en cada pedido. Se reconoce por el Host o por los encabezados de Cloudflare, y esos
    encabezados sólo valen con el Host exacto del dominio (si no, 403). La cookie va con Secure;
    el tope de intentos usa `Cf-Connecting-IP` si el socket es loopback (`cloudflared` es local);
  - en los POST, en los tres casos, un Origin del mismo panel (contra otra página abierta en el
    navegador que mande formularios al server).
  Por el dominio, el mail del JWT decide el rol en cada pedido (`cloudflare.identidad`): el dueño sigue
  con la contraseña y la cookie; un invitado (`panel.invitados`) entra sin contraseña, sólo con GET y
  HEAD y sólo a las rutas de `RUTAS_INVITADO` (todo lo demás, 403), y ve sólo las clases de sus
  materias (las otras dan 404, en `_sesion_o_404`).
  La contraseña (hash scrypt), la cookie firmada y el tope de intentos están en `classmer.panel.acceso`;
  el JWT de Access, en `classmer.panel.cloudflare`.
- HTML generado en el server con Jinja2; formularios comunes (POST → redirect) y un poco de JS
  para el reproductor. Sin compilar nada.
- Los adjuntos de una clase se ven, se abren, se bajan y se suben desde la PC con el mismo
  almacenamiento que la API (`classmer.adjuntos`).
- "Importar clase" (/panel/importar): Analizar y después Importar y procesar, con el importador de
  siempre; la recepción de los archivos y lo que se muestra están en `classmer.panel.importar`.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import cookie_parser
from starlette.routing import compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from classmer import adjuntos, importador, materias, revision, sesiones
from classmer import audio as audio_mod
from classmer.borrado import borrar_sesion, se_puede_borrar
from classmer.config import ESFUERZOS, FACTURACIONES, PROMPTS_DIR, REVISION_DEFAULT, Config, ConfigError, Panel
from classmer.config_vivo import MateriaNoExiste, materia_cruda
from classmer.ia import modelos
from classmer.ia.facturacion import (API, ETIQUETA_MEDIO, MAX, NOMBRE_MEDIO, Facturacion, despertar_pospuestos,
                                     texto_costo)
from classmer.importador import AudioGrande, ErrorImportacion
from classmer.panel import importar as panel_importar
from classmer.panel.acceso import (DURACION_SESION_S, MIN_CONTRASENA, Acceso, ErrorContrasena, es_cliente_local,
                                   host_de_red_valido, ips_de_la_pc, siguiente_seguro)
from classmer.panel.cloudflare import (ENCABEZADO_JWT, AccesoInvalido, ClavesNoDisponibles, Identidad,
                                       VerificadorAccess, trae_encabezados_cloudflare)
from classmer.panel.importar import ErrorRecepcion
from classmer.pipeline import ETAPAS
from classmer.pipeline.linea_de_tiempo import CARPETA_FOTOS
from classmer.pipeline.publicar import VIGENTE, apunte_actual, avisos_publicacion, publicaciones_de
from classmer.reproceso import ESTADOS_REGENERAR, reprocesar, reprocesar_celu
from classmer.sesiones import ArchivoGrande, Conflicto, ErrorDatos, NoExiste
from classmer.util import ahora_iso, escribir_json, leer_json, ms_a_hhmmss, parsear_iso, tiempo_a_ms, zona

log = logging.getLogger("classmer.panel")

PLANTILLAS = Path(__file__).parent / "plantillas"
HOSTS_LOCALES = {"127.0.0.1", "::1", "localhost"}
METODOS_SEGUROS = {"GET", "HEAD", "OPTIONS"}
TIPOS_ITEM = ("parcial", "fecha", "tarea", "duda", "aviso")
ESTADOS_ITEM = ("abierto", "hecho", "descartado")
CARPETAS_FOTOS = ("fotos", CARPETA_FOTOS)
LOG_SERVER = "classmer.log"
SECCIONES = {"clases.html": "clases", "clase.html": "clases", "importar.html": "clases", "materias.html": "materias",
             "materia.html": "materias", "pendientes.html": "pendientes", "pendientes_resumen.html": "pendientes",
             "estilo.html": "estilo", "modelos.html": "modelos", "estado.html": "estado", "acceso.html": "acceso",
             "invitados.html": "invitados"}
_TIEMPO_RE = re.compile(r"^\[(\d{1,2}:\d{2}:\d{2})\]\s?(.*)$")
# Archivos de una versión interna del apunte: sesiones/<id>/apunte/vN/apunte.pdf|tex (N en dígitos
# ASCII y sin ceros adelante: "v١.pdf" o "v01.pdf" no son otro nombre de la v1).
_VERSION_RE = re.compile(r"v([1-9][0-9]{0,5})\.(pdf|tex)", re.ASCII)
# Los transcripts de una clase que se ven en el panel (§15): el de la clase (revisado, si se revisó),
# los crudos y el registro de cambios de la revisión.
TRANSCRIPTS = {
    "clase": ("transcript.md", "Transcript de la clase"),
    "crudo": ("transcript.crudo.md", "Crudo (principal)"),
    "celu": ("transcript.celu.md", "Crudo del celu"),
    "cambios": ("revision/cambios.md", "Cambios de la revisión"),
}
# Cómo se llama cada transcript al bajarlo ("Macro 2026-11-05 - transcript crudo.md").
NOMBRES_TRANSCRIPT = {"clase": "transcript", "crudo": "transcript crudo", "celu": "transcript crudo del celu",
                      "cambios": "cambios de la revisión"}
# Los archivos del apunte se revalidan siempre: sin Cache-Control, el navegador guardaba el PDF
# horas (caché heurística) y seguía mostrando una versión vieja.
SIN_CACHE = {"Cache-Control": "no-cache"}
# Adjuntos (§13): cuántos archivos por envío del formulario, qué ofrece el selector de archivos (el
# tipo de verdad lo decide el contenido, en adjuntos.guardar) y cómo se ve el estado de cada uno.
MAX_ADJUNTOS_FORM = 20
ACEPTA_ADJUNTOS = ("application/pdf,image/jpeg,image/png,image/webp,image/heic,image/heif,"
                   ".pdf,.jpg,.jpeg,.png,.webp,.heic,.heif")
ESTADOS_ADJUNTO = {  # estado → (etiqueta, clase CSS, explicación)
    adjuntos.USADO: ("usado", "lista", "lo usó el apunte vigente"),
    adjuntos.PENDIENTE: ("pendiente", "procesando", "lo usa el apunte en curso o el próximo"),
    adjuntos.SIN_USAR: ("sin usar", "parcial", "el apunte vigente no lo usa: regeneralo para incluirlo"),
}
# Al bajar un adjunto, el nombre original conserva su extensión sólo si es una de su tipo.
_EXTENSIONES_TIPO = {"pdf": {".pdf"}, "jpeg": {".jpg", ".jpeg", ".jfif"}, "png": {".png"}, "webp": {".webp"},
                     "heic": {".heic", ".heif"}}

# Facturación de Claude (§7): cómo se ve cada modo en el panel.
MODOS_FACTURACION = {
    "api": "Sólo API key",
    "max": "Sólo plan Max",
    "api_luego_max": "API y, si se queda sin créditos, plan Max",
    "max_luego_api": "Plan Max y, si se agota su cupo, API",
}
MODO_DEFAULT = "api_luego_max"
MODOS_CON_API = {m for m in FACTURACIONES if API in m.split("_")}
AVISO_SIN_CLAVE = "No hay ANTHROPIC_API_KEY en Classmer-data\\.env: los modos con API funcionan como plan Max"
# Clasificar: el motor sale del proveedor del modelo elegido.
MOTOR_DE_PROVEEDOR = {modelos.ANTHROPIC: "claude", modelos.GOOGLE: "gemini"}
GRUPO_PROVEEDOR = {modelos.ANTHROPIC: "Claude (Anthropic)", modelos.GOOGLE: "Gemini (Google)"}
# "Redescubrir modelos" consulta dos APIs: una corrida a la vez.
REDESCUBRIENDO = threading.Lock()


# --- quién entra: la PC sin nada, la red con contraseña ------------------------------------------

RUTAS_SIN_SESION = ("/panel/entrar", "/panel/salir")
EMAIL_ACCESS = "classmer.email_access"   # en el scope: el mail del JWT de Access (por el dominio)
# El plan gratis de Cloudflare corta las subidas de más de 100 MB: por el dominio se avisa.
TOPE_SUBIDA_DOMINIO = 100 * 1000 * 1000

# --- invitados de sólo lectura (por el dominio) ---
IDENTIDAD = "classmer.identidad"   # en el scope: quién es por el dominio (cloudflare.Identidad)
METODOS_INVITADO = ("GET", "HEAD")
# Lo único que puede pedir un invitado: la plantilla de la ruta del router que atiende el pedido tiene
# que estar acá (se busca como lo hace el router, así una ruta nueva que tape a una de estas tampoco
# pasa). Una ruta nueva queda prohibida para el invitado hasta que alguien la sume a propósito, después
# de revisar que no escribe nada ni dispara trabajo. Las de una clase pasan por `_sesion_o_404`.
RUTAS_INVITADO = frozenset({
    "/panel",
    "/panel/clases",
    "/panel/clases/{sesion_id}",
    "/panel/clases/{sesion_id}/apunte.pdf",
    "/panel/clases/{sesion_id}/apunte.tex",
    "/panel/clases/{sesion_id}/apunte/{archivo}",
    "/panel/clases/{sesion_id}/audio",
    "/panel/clases/{sesion_id}/audio-celu",
    "/panel/clases/{sesion_id}/archivo/{ruta:path}",
    "/panel/clases/{sesion_id}/adjuntos/{nombre}",
    "/panel/clases/{sesion_id}/transcript/{cual}",
    "/panel/pendientes",
})
# Las notas y las marcas que el usuario escribe en clase desde la app: el invitado no las ve (no se habló,
# 7/11). Con True las ve en la clase y en la línea de tiempo.
INVITADO_VE_NOTAS = False
_NOTA_O_MARCA_RE = re.compile(r"^(NOTA|MARCA)\b")
# Lo de la sesión que ve un invitado (nada de cómo se procesó: error, ruta publicada, dispositivo…).
CAMPOS_SESION_INVITADO = ("id", "materia_id", "titulo", "numero_clase", "inicio", "tipo_clase", "estado",
                          "importada", "duracion_ms")
# El log anota las páginas que abre un invitado, no lo que el navegador pide para mostrarlas (las fotos
# de la clase, el PDF del iframe, los pedazos del audio): Sec-Fetch-Dest de una página, o sin él (curl).
DESTINOS_PAGINA = ("", "document")


def rutas_del_router(router: Any, prefijo: str = "") -> list[tuple[str, Any]]:
    """(plantilla, ruta) de todas las rutas, en el orden en que el router las prueba. Entra en los
    routers incluidos (FastAPI 0.143 los anida en lugar de copiar sus rutas)."""
    salida: list[tuple[str, Any]] = []
    for ruta in getattr(router, "routes", ()):
        incluido = getattr(ruta, "original_router", None)
        if incluido is not None:
            extra = getattr(getattr(ruta, "include_context", None), "prefix", "") or ""
            salida += rutas_del_router(incluido, prefijo + extra)
        elif isinstance(getattr(ruta, "path", None), str):
            salida.append((prefijo + ruta.path, ruta))
    return salida


def plantilla_que_atiende(scope: Scope) -> str | None:
    """La plantilla de la ruta que va a atender el pedido: la primera que coincide en ruta y método,
    como el router (con la expresión que compiló el router para esa ruta), o None si no hay."""
    camino, metodo = scope.get("path", ""), scope.get("method", "")
    for plantilla, ruta in rutas_del_router(getattr(scope.get("app"), "router", None)):
        regex = getattr(ruta, "path_regex", None)
        if regex is None:
            continue
        if plantilla != ruta.path:  # un router incluido con prefijo
            regex = compile_path(plantilla)[0]
        if regex.match(camino) and metodo in (getattr(ruta, "methods", None) or ()):
            return plantilla
    return None


def es_pagina(scope: Scope) -> bool:
    encabezados = Headers(scope=scope)
    return (scope.get("method") == "GET" and "range" not in encabezados
            and encabezados.get("sec-fetch-dest", "").strip().lower() in DESTINOS_PAGINA)


class VisitasInvitados:
    """La última vez que se vio a cada invitado, con su IP. En memoria: desde el último reinicio."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ultima: dict[str, tuple[float, str]] = {}

    def anotar(self, email: str, ip: str, cuando: float | None = None) -> None:
        with self._lock:
            self._ultima[email] = (time.time() if cuando is None else cuando, ip)

    def ultima(self, email: str) -> tuple[float, str] | None:
        with self._lock:
            return self._ultima.get(email)


def es_ruta_panel(ruta: str) -> bool:
    return ruta in ("/", "/panel") or ruta.startswith("/panel/")


def es_ruta_acceso(ruta: str) -> bool:
    return ruta == "/panel/acceso" or ruta.startswith("/panel/acceso/")


def _host(valor: str) -> str:
    """'127.0.0.1:8000' → '127.0.0.1'; '[::1]:8000' → '::1'."""
    try:
        return (urlsplit(f"//{valor}").hostname or "").lower()
    except ValueError:  # p. ej. '[::1' o un puerto que no es número
        return ""


def puerto_de(scope: Scope) -> int | None:
    """El puerto en que escucha el server (para el nombre de la cookie y las URLs que se muestran)."""
    servidor = scope.get("server") or (None, None)
    return servidor[1] if len(servidor) > 1 else None


def url_acceso_local(scope: Scope) -> str:
    puerto = puerto_de(scope)
    return f"http://127.0.0.1{f':{puerto}' if puerto else ''}/panel/acceso"


def _ruta_pedida(scope: Scope) -> str:
    """La ruta tal como llegó (ya codificada), con su query: adonde volver después de entrar."""
    crudo = scope.get("raw_path")
    ruta = crudo.decode("latin-1") if isinstance(crudo, bytes) and crudo else quote(scope.get("path", "/"))
    query = scope.get("query_string", b"").decode("latin-1")
    return f"{ruta}?{query}" if query else ruta


def por_el_dominio(encabezados: Headers, panel: Panel) -> bool:
    """El pedido llega (o dice llegar) por el dominio público: Host del dominio o encabezados de Cloudflare."""
    if trae_encabezados_cloudflare(encabezados):
        return True
    return panel.dominio_publico is not None and _host(encabezados.get("host", "")).rstrip(".") == panel.dominio_publico


def _cliente(scope: Scope) -> str:
    return (scope.get("client") or ("", 0))[0] or ""


def es_de_la_pc(scope: Scope, panel: Panel) -> bool:
    """La PC del server (el dueño, sin contraseña): loopback y no por el dominio. El Host de loopback
    lo exige el middleware."""
    return es_cliente_local(_cliente(scope)) and not por_el_dominio(Headers(scope=scope), panel)


def ip_del_cliente(scope: Scope, panel: Panel) -> str:
    """La IP para el tope de intentos y los logs: por el dominio y desde loopback (`cloudflared` corre en
    la PC), la de `Cf-Connecting-IP`; si no, la del socket, aunque el pedido traiga el encabezado."""
    socket_ip = _cliente(scope)
    encabezados = Headers(scope=scope)
    if es_cliente_local(socket_ip) and por_el_dominio(encabezados, panel):
        try:
            return str(ipaddress.ip_address(encabezados.get("cf-connecting-ip", "").strip()))
        except ValueError:
            pass
    return socket_ip


class AccesoPanel:
    """Middleware ASGI: quién puede usar el panel (ver el docstring del módulo)."""

    def __init__(self, app: ASGIApp, acceso: Acceso, cfg: Config, verificador: VerificadorAccess,
                 visitas: VisitasInvitados | None = None):
        self.app = app
        self.acceso = acceso
        self.cfg = cfg
        self.verificador = verificador
        self.visitas = visitas or VisitasInvitados()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and es_ruta_panel(scope.get("path", "")):
            scope.pop(IDENTIDAD, None)  # lo pone sólo este filtro, en cada pedido
            respuesta = await self._filtro(scope)
            if respuesta is not None:
                await respuesta(scope, receive, send)
                return
            quien = scope.get(IDENTIDAD)
            if quien is not None and quien.es_invitado:
                await self._como_invitado(scope, receive, send, quien)
                return
        await self.app(scope, receive, send)

    async def _como_invitado(self, scope: Scope, receive: Receive, send: Send, quien: Identidad) -> None:
        """Un pedido permitido de un invitado: anota la visita y, si es una página, una línea en el log."""
        ip = ip_del_cliente(scope, self.cfg.panel)
        self.visitas.anotar(quien.email, ip)
        estado = {"codigo": 0}

        async def enviar(mensaje: Message) -> None:
            if mensaje["type"] == "http.response.start":
                estado["codigo"] = mensaje["status"]
            await send(mensaje)

        try:
            await self.app(scope, receive, enviar)
        finally:
            if es_pagina(scope):
                codigo = estado["codigo"]
                log.info("panel: el invitado %s abrió %s (%s)%s", quien.email, _ruta_pedida(scope)[:300], ip,
                         f" → {codigo}" if codigo >= 400 or not codigo else "")

    async def _filtro(self, scope: Scope) -> Response | None:
        encabezados = Headers(scope=scope)
        host = encabezados.get("host", "")
        metodo = scope.get("method", "GET")
        if por_el_dominio(encabezados, self.cfg.panel):
            return await self._por_el_dominio(scope, encabezados, host, metodo)
        if es_cliente_local(_cliente(scope)):
            if _host(host) not in HOSTS_LOCALES:
                return _rechazo("Abrí el panel como http://127.0.0.1:<puerto>/panel.")
            if metodo not in METODOS_SEGUROS and not _mismo_origen(encabezados, host):
                return _rechazo("Pedido rechazado: no viene del panel.")
            return None
        # Desde la red (la LAN o el WireGuard del router).
        if not host_de_red_valido(_host(host)):
            return _rechazo("Abrí el panel con la IP de la PC del server: la muestra la página Acceso, en esa PC.")
        return self._con_sesion(scope, encabezados, host, metodo)

    async def _por_el_dominio(self, scope: Scope, encabezados: Headers, host: str, metodo: str) -> Response | None:
        """Cloudflare Tunnel + Access: el Host exacto del dominio y un JWT de Access válido. Después, el
        dueño sigue como desde la red (contraseña y cookie) y el invitado, por la lista blanca."""
        panel = self.cfg.panel
        if panel.dominio_publico is None or _host(host).rstrip(".") != panel.dominio_publico:
            # Encabezados de Cloudflare con otro Host (otro dominio de la misma aplicación de Access,
            # 127.0.0.1, una IP de la LAN): no es el panel por su dominio.
            return _rechazo("Por internet, el panel se abre sólo en su dominio.")
        if not panel.cloudflare_access.completa:
            return _rechazo("El panel no se abre por el dominio: falta panel.cloudflare_access en config.yaml.")
        try:
            quien = await run_in_threadpool(self.verificador.verificar, encabezados.get(ENCABEZADO_JWT), panel)
        except AccesoInvalido as e:
            log.warning("panel: pedido por el dominio rechazado, no pasó por Cloudflare Access (%s, desde %s): %s",
                        scope.get("path", ""), ip_del_cliente(scope, panel), e)
            return _rechazo("Pedido rechazado: no pasó por Cloudflare Access.")
        except ClavesNoDisponibles:
            return PlainTextResponse("No pude bajar las claves de Cloudflare Access para validar el pedido: "
                                     "probá de nuevo en un rato.", status_code=503, headers={"Retry-After": "60"})
        scope[EMAIL_ACCESS] = quien.email
        scope[IDENTIDAD] = quien
        if quien.es_invitado:
            return self._filtro_invitado(scope, quien, metodo)
        return self._con_sesion(scope, encabezados, host, metodo)

    def _filtro_invitado(self, scope: Scope, quien: Identidad, metodo: str) -> Response | None:
        """Sólo lectura: GET o HEAD, y sólo las rutas de RUTAS_INVITADO. Sin contraseña ni cookie (una
        cookie de dueño no lo hace dueño). /panel/entrar lo manda a las clases."""
        ruta = scope.get("path", "")
        if metodo in METODOS_INVITADO and ruta == "/panel/entrar":
            return RedirectResponse("/panel/clases", status_code=303)
        if metodo in METODOS_INVITADO and plantilla_que_atiende(scope) in RUTAS_INVITADO:
            return None
        log.warning("panel: el invitado %s pidió %s %s (%s): no lo puede pedir (sólo lectura)", quien.email,
                    metodo, _ruta_pedida(scope)[:300], ip_del_cliente(scope, self.cfg.panel))
        return _rechazo("Tu acceso al panel de Classmer es de sólo lectura: podés ver las clases y los "
                        "pendientes de tus materias, nada más.")

    def _con_sesion(self, scope: Scope, encabezados: Headers, host: str, metodo: str) -> Response | None:
        """Desde la red o por el dominio: el mismo origen, nunca /panel/acceso, la contraseña y la cookie."""
        ruta = scope.get("path", "")
        if metodo not in METODOS_SEGUROS and not _mismo_origen(encabezados, host):
            return _rechazo("Pedido rechazado: no viene del panel.")
        if es_ruta_acceso(ruta):
            return _rechazo("La contraseña del panel se pone, se cambia y se quita sólo en la PC del server: "
                            f"{url_acceso_local(scope)}.")
        if not self.acceso.estado().abierto:
            return _rechazo("Desde otra PC, el panel de Classmer pide una contraseña y todavía no hay una. "
                            f"Ponela en la PC del server: {url_acceso_local(scope)}.")
        if ruta in RUTAS_SIN_SESION:
            return None
        cookies = cookie_parser(encabezados.get("cookie", ""))
        if self.acceso.sesion_valida(cookies.get(Acceso.nombre_cookie(puerto_de(scope)))):
            return None
        if metodo in METODOS_SEGUROS:
            return RedirectResponse(f"/panel/entrar?{urlencode({'siguiente': _ruta_pedida(scope)})}", status_code=303)
        return _rechazo("Tu sesión del panel venció o se cerró: entrá de nuevo en /panel/entrar.")


def _mismo_origen(encabezados: Headers, host: str) -> bool:
    origen = encabezados.get("origin") or encabezados.get("referer")
    if origen is None:
        return True
    try:
        return urlsplit(origen).netloc.lower() == host.lower()
    except ValueError:
        return False


def _rechazo(motivo: str) -> PlainTextResponse:
    return PlainTextResponse(motivo, status_code=403)


# --- helpers de vista --------------------------------------------------------------------------

def _hora(epoch: float | None, nombre_zona: str) -> str:
    if not epoch:
        return ""
    dt = datetime.fromtimestamp(epoch, zona(nombre_zona))
    hoy = datetime.now(zona(nombre_zona)).date()
    return dt.strftime("%H:%M:%S") if dt.date() == hoy else dt.strftime("%d/%m %H:%M")


def _fecha(iso: str | None, nombre_zona: str) -> str:
    if not iso:
        return ""
    try:
        dt = parsear_iso(iso, nombre_zona).astimezone(zona(nombre_zona))
    except ValueError:
        return iso
    return dt.strftime("%Y-%m-%d %H:%M")


def _duracion(ms: int | None) -> str:
    if not ms:
        return ""
    minutos = round(ms / 60000)
    return f"{minutos // 60} h {minutos % 60:02d} min" if minutos >= 60 else f"{minutos} min"


def _tamano(n: int | None) -> str:
    """Bytes legibles: '840 B', '312 KB', '4,2 MB'."""
    if n is None:
        return ""
    if n < 1024:
        return f"{n} B"
    valor = float(n)
    for unidad in ("KB", "MB", "GB"):
        valor /= 1024
        if valor < 1024 or unidad == "GB":
            break
    return (f"{valor:.1f}" if valor < 10 else f"{valor:.0f}").replace(".", ",") + f" {unidad}"


def _cola_archivo(ruta: Path, lineas: int, max_bytes: int = 256_000) -> list[str]:
    """Las últimas `lineas` de un archivo de texto (lee sólo el final)."""
    try:
        with open(ruta, "rb") as f:
            f.seek(0, 2)
            tam = f.tell()
            f.seek(max(0, tam - max_bytes))
            texto = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    filas = texto.splitlines()
    if tam > max_bytes and filas:
        filas = filas[1:]  # la primera puede estar cortada
    return filas[-lineas:]


def _lineas_con_tiempo(texto: str) -> list[dict[str, Any]]:
    """Líneas de timeline.md/transcript.md con su minuto (ms) si empiezan con [hh:mm:ss]."""
    salida = []
    for linea in texto.splitlines():
        m = _TIEMPO_RE.match(linea)
        if m:
            salida.append({"ms": tiempo_a_ms(m.group(1)), "tiempo": m.group(1), "texto": m.group(2)})
        elif linea.strip():
            salida.append({"ms": None, "tiempo": "", "texto": linea})
    return salida


def _leer_texto(ruta: Path) -> str | None:
    try:
        return ruta.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _texto_costo(costo: float | None, facturacion: str | None) -> str:
    """Costo de un job o un apunte: con la facturación de Claude si la hay ('' si no hay nada)."""
    if facturacion:
        return texto_costo(costo, facturacion)
    if costo is None:
        return ""
    return f"US$ {costo:.4f}" if costo < 0.1 else f"US$ {costo:.2f}"


def _grupos(opciones: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Las opciones de un select agrupadas por proveedor (<optgroup>), en el orden en que vienen."""
    grupos: dict[str, list[dict[str, Any]]] = {}
    for o in opciones:
        grupos.setdefault(o.get("proveedor") or "", []).append(o)
    return [(GRUPO_PROVEEDOR.get(p, p or "Otros"), lista) for p, lista in grupos.items()]


def _esfuerzo_form(texto: str | None, nombre: str) -> str | None:
    """Esfuerzo de un select: vacío = None (el default); si no, uno de la lista."""
    texto = (texto or "").strip()
    if not texto:
        return None
    if texto not in ESFUERZOS:
        raise ConfigError(f"{nombre}: esfuerzo inválido {texto!r} (opciones: {', '.join(ESFUERZOS)})")
    return texto


def _horas(texto: str | None) -> float:
    """Horas para volver a probar la API: un número mayor que 0 ("1,5" también vale)."""
    try:
        horas = float((texto or "").strip().replace(",", "."))
    except ValueError:
        horas = math.nan
    if not math.isfinite(horas) or horas <= 0:
        raise ConfigError(f"las horas para volver a probar la API tienen que ser un número mayor que 0 "
                          f"(llegó {texto!r})")
    return int(horas) if horas.is_integer() else horas


def _elegir(conn: sqlite3.Connection, tarea: str, valor: str | None, actual: str | None) -> dict[str, Any]:
    """La opción elegida en el select de una tarea: tiene que estar en su lista (el catálogo para esa
    tarea o el valor actual). El modelo nunca se escribe a mano: ConfigError si no está."""
    valor = (valor or "").strip()
    for o in modelos.opciones_tarea(conn, tarea, actual):
        if o["id"] == valor:
            return o
    raise ConfigError(f"{tarea}: {valor!r} no está en la lista de modelos (elegí uno del catálogo o "
                      "redescubrí los modelos)")


def _url_pdf(sesion_id: str, ruta: Path | None) -> str | None:
    """URL del PDF vigente en el panel (None si no hay), con una marca `?v=` que cambia cada vez que
    cambia el archivo servido: otra versión, o la misma recompilada o republicada. Sin la marca, la
    URL es la misma para todas las versiones y el navegador puede mostrar una vieja de su caché."""
    if ruta is None:
        return None
    url = f"/panel/clases/{sesion_id}/apunte.pdf"
    try:
        st = ruta.stat()
    except OSError:
        return url
    marca = hashlib.sha256(b"%s|%d|%d" % (os.fsencode(ruta), st.st_mtime_ns, st.st_size)).hexdigest()[:12]
    return f"{url}?v={marca}"


def _adjuntos_vista(sesion_id: str, lista: list[dict[str, Any]], de_api: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Los adjuntos para la página de la clase: con el estado que ya calculó `sesiones.a_api`, cómo se
    ve, el tipo para leer ("PDF, 23 páginas") y las URLs para abrirlo y para bajarlo."""
    estados = {a["id"]: a["estado"] for a in de_api}
    salida = []
    for a in lista:
        estado = estados.get(a["id"])
        etiqueta, css, ayuda = ESTADOS_ADJUNTO.get(estado, ("—", "", ""))
        url = f"/panel/clases/{sesion_id}/adjuntos/{quote(a['nombre'])}"
        salida.append({**a, "estado": estado, "estado_texto": etiqueta, "estado_css": css, "estado_ayuda": ayuda,
                       "descripcion": adjuntos.describir_tipo(a), "url": url, "url_descarga": f"{url}?descargar=1"})
    return salida


def _nombre_descarga(adjunto: dict[str, Any]) -> str:
    """El nombre original para bajar el adjunto: sin carpetas y con una extensión de su tipo (el que se
    detectó por el contenido). Si no, un 'notas.hta' que arranca con %PDF- se guarda como PDF pero bajaría
    como .hta, y en Windows el doble clic lo ejecuta: 'notas.hta' → 'notas.pdf', 'x.pdf.exe' → 'x.pdf.pdf'.
    FileResponse lo codifica en Content-Disposition (filename*=utf-8'' si tiene comillas, saltos de
    línea o algo que no sea ASCII)."""
    nombre = re.split(r"[\\/]", adjunto["nombre_original"] or "")[-1].strip() or adjunto["nombre"]
    extension = Path(adjunto["nombre"]).suffix  # la del tipo, la puso el server (.pdf, .jpg, …)
    sufijo = Path(nombre).suffix
    if sufijo.casefold() in _EXTENSIONES_TIPO.get(adjunto["tipo"], {extension}):
        return nombre
    return (nombre[:-len(sufijo)] if sufijo else nombre) + extension


def _corto(texto: str, largo: int = 80) -> str:
    return texto if len(texto) <= largo else texto[:largo - 1] + "…"


def _mensaje_adjuntos(guardados: list[dict[str, Any]], rechazos: dict[str, list[str]]) -> tuple[str, str]:
    """(mensaje, tipo) después de adjuntar desde el panel: qué entró, qué no y por qué. Los rechazos van
    agrupados por motivo (varios de un tipo no soportado dicen lo mismo)."""
    no_entraron = "; ".join(f"{', '.join(_corto(n) for n in nombres)} ({motivo})"
                            for motivo, nombres in rechazos.items())
    if not guardados:
        return f"No se pudo adjuntar: {no_entraron}.", "error"
    uno = len(guardados) == 1
    if rechazos:
        total = len(guardados) + sum(len(n) for n in rechazos.values())
        texto = f"Se agregó{'' if uno else 'ron'} {len(guardados)} de {total} adjuntos. No entraron: {no_entraron}."
    elif uno:
        texto = f"Adjunto agregado: {_corto(guardados[0]['nombre_original'] or guardados[0]['nombre'])}."
    else:
        texto = f"{len(guardados)} adjuntos agregados."
    estados = {g["estado"] for g in guardados}
    if adjuntos.SIN_USAR in estados:
        texto += (" El apunte vigente no lo usa: regeneralo para incluirlo." if uno
                  else " El apunte vigente no los usa: regeneralo para incluirlos.")
    elif estados == {adjuntos.PENDIENTE}:
        texto += " Lo va a usar el próximo apunte." if uno else " Los va a usar el próximo apunte."
    return texto, "aviso" if rechazos else "ok"


def _frase(texto: str) -> str:
    """'el audio pesa más de 2 GB: …' → 'El audio pesa más de 2 GB: ….' (un mensaje suelto del panel)."""
    texto = texto.strip()
    if not texto:
        return texto
    texto = texto[0].upper() + texto[1:]
    return texto if texto.endswith((".", "?", "!")) else texto + "."


def _estado_texto(estado: str | None, importada: Any = False) -> str:
    """El estado como se lee: una importada en `grabando` la está subiendo la app (no hay clase en curso)."""
    return "subiendo" if importada and estado == "grabando" else estado or ""


_NO_VA_EN_NOMBRE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')


def _descargar(valor: str) -> bool:
    """`?descargar=1` (cualquier cosa menos vacío o 0): Content-Disposition attachment."""
    return valor not in ("", "0")


def _disposicion(tipo: str, nombre: str) -> str:
    """Content-Disposition con el nombre, como lo arma FileResponse (filename* si no es ASCII simple)."""
    citado = quote(nombre)
    return f"{tipo}; filename*=utf-8''{citado}" if citado != nombre else f'{tipo}; filename="{nombre}"'


def _minuto_nombre(ms: int) -> str:
    """'00h01m01s': el minuto de una foto en un nombre de archivo (Windows no acepta ':')."""
    s = ms // 1000
    return f"{s // 3600:02d}h{s % 3600 // 60:02d}m{s % 60:02d}s"


def _archivo_en(raiz: Path, ruta: Path) -> Path | None:
    """`ruta` resuelta si es un archivo dentro de `raiz` (nunca afuera, ni con '..' ni con enlaces)."""
    try:
        destino = ruta.resolve()
        if destino.is_relative_to(raiz.resolve()) and destino.is_file():
            return destino
    except OSError:
        pass
    return None


# --- router ------------------------------------------------------------------------------------

def _lista_blanca_del_invitado(request: Request) -> None:
    """Por las dudas, en cada ruta del panel: la que se está por ejecutar tiene que estar en
    RUTAS_INVITADO si el pedido es de un invitado (el filtro de verdad es el middleware, antes de rutear)."""
    quien = request.scope.get(IDENTIDAD)
    if isinstance(quien, Identidad) and quien.es_invitado:
        ruta = getattr(request.scope.get("route"), "path", None)
        if ruta not in RUTAS_INVITADO or request.method not in METODOS_INVITADO:
            raise HTTPException(403, "sólo lectura")


def crear_router(cfg: Config, acceso: Acceso | None = None, verificador: VerificadorAccess | None = None,
                 visitas: VisitasInvitados | None = None) -> APIRouter:
    router = APIRouter(dependencies=[Depends(_lista_blanca_del_invitado)])
    acceso = acceso or Acceso(cfg.home)
    verificador = verificador or VerificadorAccess()
    visitas = visitas or VisitasInvitados()

    def lectura(ruta: str, **opciones: Any):
        """Una ruta que se lee (GET y HEAD: el reproductor y curl -I preguntan con HEAD)."""
        return router.api_route(ruta, methods=["GET", "HEAD"], **opciones)
    plantillas = Jinja2Templates(directory=str(PLANTILLAS))
    plantillas.env.filters["hhmmss"] = lambda ms: ms_a_hhmmss(ms or 0)
    plantillas.env.filters["fecha"] = lambda iso: _fecha(iso, cfg.zona_horaria)
    plantillas.env.filters["hora"] = lambda epoch: _hora(epoch, cfg.zona_horaria)
    plantillas.env.filters["duracion"] = _duracion
    plantillas.env.filters["porcentaje"] = lambda x: f"{round((x or 0) * 100)}%"
    plantillas.env.filters["pago"] = lambda medio: ETIQUETA_MEDIO.get(medio, medio) if medio else ""
    plantillas.env.filters["costo"] = _texto_costo
    plantillas.env.filters["tamano"] = _tamano
    plantillas.env.filters["estado_texto"] = _estado_texto

    def ctx_de(request: Request):
        return request.app.state.ctx

    def claves() -> list[str]:
        """Los secretos de la config (se leen cada vez: la config viva los puede cambiar)."""
        return [c for c in (cfg.anthropic_api_key, cfg.gemini_api_key, cfg.token) if c and len(c) >= 8]

    def sin_claves(texto: str) -> str:
        for clave in claves():
            texto = texto.replace(clave, "[clave oculta]")
        return texto

    def vista(request: Request, nombre: str, status_code: int = 200, **datos: Any) -> HTMLResponse:
        datos.setdefault("msg", request.query_params.get("msg"))
        datos.setdefault("tipo_msg", request.query_params.get("tipo", "ok"))
        datos["cfg"] = cfg
        datos["nombre_materia"] = nombre_materia
        datos["seccion"] = SECCIONES.get(nombre, "")
        datos["invitado"] = invitado_de(request)
        datos["desde_la_pc"] = desde_la_pc(request)
        datos["por_el_dominio"] = dominio(request)
        datos["tope_dominio_mb"] = TOPE_SUBIDA_DOMINIO // (1000 * 1000)
        respuesta = plantillas.TemplateResponse(request, nombre, datos, status_code=status_code)
        # Por las dudas: una clave nunca sale en una página, aunque se cuele en un error o un log.
        if any(c.encode("utf-8") in respuesta.body for c in claves()):
            return HTMLResponse(sin_claves(respuesta.body.decode("utf-8")), status_code=status_code)
        return respuesta

    def con_msg(url: str, msg: str, tipo: str = "ok") -> str:
        """La URL con el mensaje que muestra la página (?msg=…&tipo=…)."""
        separador = "&" if "?" in url else "?"
        return f"{url}{separador}{urlencode({'msg': sin_claves(msg), 'tipo': tipo})}"

    def volver(url: str, msg: str, tipo: str = "ok") -> RedirectResponse:
        return RedirectResponse(con_msg(url, msg, tipo), status_code=303)

    def nombre_materia(materia_id: str | None) -> str:
        if materia_id is None:
            return "Auto (sin materia)"
        m = cfg.materia(materia_id)
        return m.nombre if m else materia_id

    async def formulario(request: Request) -> dict[str, Any]:
        form = await request.form()
        datos: dict[str, Any] = {}
        for clave, valor in form.multi_items():
            if isinstance(valor, str):
                datos[clave] = valor
        return datos

    def con_url_pdf(conn: sqlite3.Connection, fila: sqlite3.Row) -> dict[str, Any]:
        """La sesión como la da la API, más `url_pdf`: el PDF vigente en el panel, con su marca."""
        return {**sesiones.a_api(cfg, conn, fila),
                "url_pdf": _url_pdf(fila["id"], sesiones.ruta_pdf_actual(cfg, conn, fila["id"]))}

    def desde_la_pc(request: Request) -> bool:
        return es_de_la_pc(request.scope, cfg.panel)

    def dominio(request: Request) -> bool:
        """Por el dominio público (https del lado del navegador): el middleware ya validó el JWT."""
        return por_el_dominio(request.headers, cfg.panel)

    def invitado_de(request: Request) -> Identidad | None:
        """El invitado de sólo lectura de este pedido (lo decidió el middleware con el JWT), o None."""
        quien = request.scope.get(IDENTIDAD)
        return quien if isinstance(quien, Identidad) and quien.es_invitado else None

    def nombre_descarga(s: Any, cosa: str, extension: str) -> str:
        """Un nombre legible para bajar algo de una clase: 'Macro 2026-11-05 - apunte.pdf'."""
        fecha = _fecha(s["inicio"], cfg.zona_horaria)[:10]
        base = " ".join(p for p in (nombre_materia(s["materia_id"]), fecha) if p)
        nombre = _NO_VA_EN_NOMBRE.sub("-", f"{base} - {cosa}").strip(" .")
        return nombre + extension

    # --- entrar y salir (desde la red) ---
    def nombre_cookie(request: Request) -> str:
        return Acceso.nombre_cookie(puerto_de(request.scope))

    def pagina_entrar(request: Request, siguiente: str, error: str | None = None,
                      status_code: int = 200) -> HTMLResponse:
        respuesta = vista(request, "entrar.html", status_code=status_code, siguiente=siguiente, error=error,
                          sin_nav=True, dias=DURACION_SESION_S // 86400, url_acceso=url_acceso_local(request.scope))
        respuesta.headers["Cache-Control"] = "no-store"
        return respuesta

    @router.get("/panel/entrar", response_class=HTMLResponse)
    def entrar(request: Request, siguiente: str = "") -> Response:
        destino = siguiente_seguro(siguiente)
        if desde_la_pc(request) or acceso.sesion_valida(request.cookies.get(nombre_cookie(request))):
            return RedirectResponse(destino, status_code=303)
        return pagina_entrar(request, destino)

    @router.post("/panel/entrar")
    async def entrar_post(request: Request) -> Response:
        """Verifica la contraseña (scrypt, en el threadpool) con el tope de intentos por IP y global."""
        datos = await formulario(request)
        destino = siguiente_seguro(datos.get("siguiente"))
        if desde_la_pc(request):
            return RedirectResponse(destino, status_code=303)
        ip = ip_del_cliente(request.scope, cfg.panel)
        if not acceso.intentos.anotar(ip):
            minutos = max(1, math.ceil(acceso.intentos.espera(ip) / 60))
            log.warning("panel: tope de intentos de entrar (desde %s)", ip)
            respuesta = pagina_entrar(
                request, destino, status_code=429,
                error=f"Demasiados intentos fallidos. Esperá {minutos} minuto{'s' if minutos != 1 else ''} y "
                      "probá de nuevo (o entrá desde la PC del server).")
            respuesta.headers["Retry-After"] = str(minutos * 60)
            return respuesta
        if not await run_in_threadpool(acceso.verificar, datos.get("contrasena", "")):
            log.warning("panel: contraseña incorrecta (desde %s)", ip)
            return pagina_entrar(request, destino, error="Contraseña incorrecta.", status_code=401)
        acceso.intentos.bien(ip)
        valor = acceso.emitir()
        if valor is None:  # la quitaron mientras se verificaba
            return _rechazo("El acceso desde la red se cerró.")
        if dominio(request):
            log.info("panel: entró un navegador por el dominio (%s, %s)", ip, request.scope.get(EMAIL_ACCESS))
        else:
            log.info("panel: entró una PC de la red (%s)", ip)
        respuesta = RedirectResponse(destino, status_code=303)
        # Por el dominio el navegador ve https: la cookie con Secure. En la LAN es http, como siempre.
        respuesta.set_cookie(nombre_cookie(request), valor, max_age=DURACION_SESION_S, path="/",
                             httponly=True, samesite="lax", secure=dominio(request))
        return respuesta

    @router.post("/panel/salir")
    def salir(request: Request) -> RedirectResponse:
        if desde_la_pc(request):
            return RedirectResponse("/panel/clases", status_code=303)
        respuesta = RedirectResponse(con_msg("/panel/entrar", "Saliste del panel en esta PC."), status_code=303)
        respuesta.delete_cookie(nombre_cookie(request), path="/", httponly=True, samesite="lax",
                                secure=dominio(request))
        return respuesta

    # --- acceso desde la red (sólo desde la PC) ---
    def solo_desde_la_pc(request: Request) -> None:
        """El middleware ya da 403 desde la red; esto es por las dudas."""
        if not desde_la_pc(request):
            raise HTTPException(403, "la contraseña del panel se cambia sólo desde la PC del server")

    @router.get("/panel/acceso", response_class=HTMLResponse)
    def acceso_ver(request: Request) -> HTMLResponse:
        solo_desde_la_pc(request)
        estado = acceso.estado()
        puerto = puerto_de(request.scope) or cfg.servidor.port
        urls = [f"http://{ip}:{puerto}/panel" for ip in ips_de_la_pc()]
        panel = cfg.panel
        return vista(request, "acceso.html", estado=estado, urls=urls, puerto=puerto, min_contrasena=MIN_CONTRASENA,
                     fallidos=acceso.intentos.fallidos(), dias=DURACION_SESION_S // 86400,
                     dominio_publico=panel.dominio_publico, access_completa=panel.cloudflare_access.completa,
                     url_publica=f"https://{panel.dominio_publico}/panel" if panel.por_el_dominio else None,
                     pedido_claves=verificador.ultimo)

    @router.post("/panel/acceso/contrasena")
    async def acceso_contrasena(request: Request) -> RedirectResponse:
        solo_desde_la_pc(request)
        datos = await formulario(request)
        habia = acceso.estado().abierto
        try:
            await run_in_threadpool(acceso.poner, datos.get("contrasena", ""), datos.get("repetida", ""))
        except ErrorContrasena as e:
            return volver("/panel/acceso", str(e), "error")
        if habia:
            return volver("/panel/acceso", "Contraseña cambiada: las otras PCs tienen que entrar de nuevo.")
        return volver("/panel/acceso", "Listo: el panel ya se abre desde las otras PCs de la red, con la contraseña.")

    @router.post("/panel/acceso/cerrar-sesiones")
    def acceso_cerrar_sesiones(request: Request) -> RedirectResponse:
        solo_desde_la_pc(request)
        if not acceso.cerrar_sesiones():
            return volver("/panel/acceso", "No hay contraseña puesta: no hay sesiones para cerrar.", "aviso")
        return volver("/panel/acceso", "Sesiones cerradas: las otras PCs tienen que volver a poner la contraseña.")

    @router.post("/panel/acceso/cerrar")
    def acceso_cerrar(request: Request) -> RedirectResponse:
        solo_desde_la_pc(request)
        if not acceso.quitar():
            return volver("/panel/acceso", "El acceso desde la red ya estaba cerrado.", "aviso")
        return volver("/panel/acceso", "Acceso desde la red cerrado: el panel vuelve a abrirse sólo en esta PC.")

    # --- inicio ---
    @router.get("/", include_in_schema=False)
    @lectura("/panel", include_in_schema=False)
    def inicio() -> RedirectResponse:
        return RedirectResponse("/panel/clases", status_code=303)

    def materias_de(request: Request) -> list[Any]:
        """Las materias que puede ver este pedido: todas, o las del invitado (en el orden de config.yaml)."""
        quien = invitado_de(request)
        return [m for m in cfg.materias if quien is None or m.id in quien.materias]

    # --- clases ---
    @lectura("/panel/clases", response_class=HTMLResponse)
    def clases(request: Request, materia: str | None = None, estado: str | None = None,
               limite: int = 100) -> HTMLResponse:
        filtros, params = [], []
        quien = invitado_de(request)
        if quien is not None:  # sólo sus materias, también con un ?materia= de otra (o "auto")
            suyas = sorted(quien.materias)
            filtros.append(f"materia_id IN ({', '.join('?' * len(suyas))})" if suyas else "0")
            params.extend(suyas)
        if materia:
            filtros.append("materia_id = ?" if materia != "auto" else "materia_id IS NULL")
            if materia != "auto":
                params.append(materia)
        if estado:
            filtros.append("estado = ?")
            params.append(estado)
        donde = f"WHERE {' AND '.join(filtros)}" if filtros else ""
        ahora = datetime.now().timestamp()
        with ctx_de(request).base.conexion() as conn:
            filas = conn.execute(
                f"SELECT * FROM sesiones {donde} ORDER BY inicio DESC, creado DESC LIMIT ?",
                (*params, max(1, min(limite, 1000))),
            ).fetchall()
            lista = []
            for f in filas:
                api = con_url_pdf(conn, f)
                if quien is not None:  # ni el error ni los jobs en espera
                    lista.append({**api, "error": None, "etapa": None, "adjuntos_sin_usar": 0,
                                  "materia_nombre": nombre_materia(f["materia_id"]), "espera": None})
                    continue
                espera = conn.execute(
                    "SELECT error, run_after FROM jobs WHERE sesion_id = ? AND estado = 'pendiente' "
                    "AND run_after > ? ORDER BY run_after LIMIT 1", (f["id"], ahora)).fetchone()
                lista.append({**api, "materia_nombre": nombre_materia(f["materia_id"]),
                              "espera": dict(espera) if espera else None})
        return vista(request, "clases.html", clases=lista, filtro_materia=materia or "",
                     filtro_estado=estado or "", estados=sesiones.ESTADOS, materias_filtro=materias_de(request))

    def _sesion_o_404(request: Request, conn: sqlite3.Connection, sesion_id: str) -> sqlite3.Row:
        """La sesión, o 404. Para un invitado, también 404 si no es de una de sus materias (ni si es Auto):
        no se revela que existe. Todas las rutas de una clase pasan por acá."""
        try:
            s = sesiones.fila_sesion(conn, sesion_id)
        except NoExiste as e:
            raise HTTPException(404, "sesión no encontrada") from e
        quien = invitado_de(request)
        if quien is not None and s["materia_id"] not in quien.materias:
            raise HTTPException(404, "sesión no encontrada")
        return s

    @lectura("/panel/clases/{sesion_id}", response_class=HTMLResponse)
    def clase(request: Request, sesion_id: str) -> HTMLResponse:
        if invitado_de(request) is not None:
            return clase_solo_lectura(request, sesion_id)
        carpeta = cfg.sesion_dir(sesion_id)
        with ctx_de(request).base.conexion() as conn:
            s = _sesion_o_404(request, conn, sesion_id)
            api = sesiones.a_api(cfg, conn, s)
            audio = sesiones.ruta_audio(cfg, conn, sesion_id)
            audio_panel = sesiones.ruta_audio_panel(cfg, conn, sesion_id)
            # Fotos, notas y marcas: ni el audio ni los adjuntos (esos van en su propia sección). Las
            # fotos sin minuto (las de una importada sin hora EXIF) van al final.
            eventos = [dict(e) | {"datos": json.loads(e["datos"] or "{}")} for e in conn.execute(
                f"SELECT * FROM assets WHERE sesion_id = ? AND {sesiones.SOLO_EVENTOS} "
                "ORDER BY offset_ms IS NULL, offset_ms, creado", (sesion_id,))]
            lista_adjuntos = adjuntos.listar(conn, sesion_id)
            jobs = conn.execute("SELECT * FROM jobs WHERE sesion_id = ? ORDER BY id", (sesion_id,)).fetchall()
            items = conn.execute("SELECT * FROM items WHERE sesion_id = ? ORDER BY offset_ms, id",
                                 (sesion_id,)).fetchall()
            apuntes = conn.execute("SELECT * FROM apuntes WHERE sesion_id = ? ORDER BY version DESC",
                                   (sesion_id,)).fetchall()
            actual = apunte_actual(conn, sesion_id)
            ruta_pdf = sesiones.ruta_pdf_actual(cfg, conn, sesion_id)
            publicaciones = publicaciones_de(conn, sesion_id)
            avisos_pub = avisos_publicacion(conn, sesion_id)
            celu = sesiones.datos_celu(conn, sesion_id)
            audio_celu = sesiones.ruta_audio(cfg, conn, sesion_id, sesiones.PISTA_CELU)
            confirmacion_celu = texto_confirmacion_celu(conn, ctx_de(request).base)
        transcript = leer_json(carpeta / "transcript.json", None) or []
        timeline_txt = _leer_texto(carpeta / "timeline.md")
        duracion_ms = s["duracion_ms"] or max((seg.get("fin_ms") or 0 for seg in transcript), default=0)
        fotos = [e for e in eventos if e["tipo"] == "foto" and e["ruta"]]
        importacion = None
        if s["importada"]:
            try:
                importacion = panel_importar.resumen_importacion(leer_json(carpeta / "importacion.json"),
                                                                 s["dispositivo"])
            except (OSError, ValueError) as e:
                log.warning("importacion.json ilegible en %s: %s", sesion_id[:8], e)
        return vista(
            request, "clase.html", s=s, api=api, materia_nombre=nombre_materia(s["materia_id"]),
            hay_audio=audio.is_file() and audio.stat().st_size > 0, eventos=eventos, fotos=fotos,
            # Sin copia para el panel, un amr o un wma no se escuchan hasta que verificar la arma.
            audio_reproducible=audio_panel.suffix.lower() in audio_mod.MIME_AUDIO, importacion=importacion,
            transcript=transcript, timeline=_lineas_con_tiempo(timeline_txt) if timeline_txt else None,
            duracion_ms=duracion_ms, jobs=jobs, items=items, etapas=ETAPAS, url_pdf=_url_pdf(sesion_id, ruta_pdf),
            log=_cola_archivo(carpeta / "pipeline.log", 60),
            materias_activas=cfg.materias_activas, puede_borrar=se_puede_borrar(s["materia_id"]),
            puede_asignar=s["estado"] in ("esperando_usuario", "grabando", "en_cola")
            or (s["estado"] == "procesando" and s["etapa"] not in sesiones.ETAPAS_DEL_APUNTE),
            **_versiones(sesion_id, apuntes, actual, ruta_pdf, publicaciones),
            publicaciones=publicaciones, avisos_pub=avisos_pub,
            adjuntos_clase=_adjuntos_vista(sesion_id, lista_adjuntos, api["adjuntos"]),
            puede_regenerar=s["estado"] in ESTADOS_REGENERAR, acepta_adjuntos=ACEPTA_ADJUNTOS,
            tipos_adjunto=adjuntos.TEXTO_TIPOS, max_adjunto_mb=adjuntos.MAX_ADJUNTO // (1024 * 1024),
            rev=revision.resumen_panel(revision.cargar(cfg, sesion_id)),
            esperando_revision=s["estado"] == "esperando_usuario" and s["espera"] == "revision",
            celu=celu, hay_audio_celu=celu is not None and audio_celu.is_file(),
            audio_celu_reproducible=audio_celu.suffix.lower() in audio_mod.MIME_AUDIO,
            puede_agregar_celu=s["estado"] in sesiones.ESTADOS_AGREGAR_CELU, confirmacion_celu=confirmacion_celu,
            transcripts=_transcripts_disponibles(carpeta),
        )

    def clase_solo_lectura(request: Request, sesion_id: str) -> HTMLResponse:
        """La clase para un invitado: el apunte (y sus versiones), los transcripts, el audio, las fotos,
        los adjuntos, sus pendientes y la línea de tiempo. Nada de cómo se procesó (jobs, costos, log,
        errores, dónde se publica, la importación, la revisión) ni las notas y marcas de la app: la
        plantilla no lo muestra y acá ni se le pasa (si una plantilla se equivoca, no hay qué mostrar)."""
        carpeta = cfg.sesion_dir(sesion_id)
        with ctx_de(request).base.conexion() as conn:
            fila = _sesion_o_404(request, conn, sesion_id)
            s = {k: fila[k] for k in CAMPOS_SESION_INVITADO}
            progreso = sesiones.calcular_progreso(conn, sesion_id)
            audio = sesiones.ruta_audio(cfg, conn, sesion_id)
            audio_panel = sesiones.ruta_audio_panel(cfg, conn, sesion_id)
            eventos = [dict(e) | {"datos": json.loads(e["datos"] or "{}")} for e in conn.execute(
                f"SELECT * FROM assets WHERE sesion_id = ? AND {sesiones.SOLO_EVENTOS} "
                "ORDER BY offset_ms IS NULL, offset_ms, creado", (sesion_id,))]
            lista_adjuntos = adjuntos.listar(conn, sesion_id)
            items = conn.execute("SELECT id, tipo, texto, offset_ms, estado, fecha_limite FROM items "
                                 "WHERE sesion_id = ? ORDER BY offset_ms, id", (sesion_id,)).fetchall()
            apuntes = conn.execute("SELECT * FROM apuntes WHERE sesion_id = ? ORDER BY version DESC",
                                   (sesion_id,)).fetchall()
            actual = apunte_actual(conn, sesion_id)
            ruta_pdf = sesiones.ruta_pdf_actual(cfg, conn, sesion_id)
            celu = sesiones.datos_celu(conn, sesion_id)
            audio_celu = sesiones.ruta_audio(cfg, conn, sesion_id, sesiones.PISTA_CELU)
        transcript = leer_json(carpeta / "transcript.json", None) or []
        timeline_txt = _leer_texto(carpeta / "timeline.md")
        timeline = _lineas_con_tiempo(timeline_txt) if timeline_txt else None
        if timeline is not None and not INVITADO_VE_NOTAS:
            timeline = [l for l in timeline if not _NOTA_O_MARCA_RE.match(l["texto"])]
        if not INVITADO_VE_NOTAS:
            eventos = [e for e in eventos if e["tipo"] == "foto"]
        versiones = _versiones(sesion_id, apuntes, actual, ruta_pdf, [])
        vigente = versiones["actual"]
        anteriores = [{"a": {"version": x["a"]["version"], "creado": x["a"]["creado"], "titulo": x["a"]["titulo"]},
                       "pdf": x["pdf"], "tex": x["tex"]} for x in versiones["anteriores"]]
        adjuntos_clase = [{k: a[k] for k in ("nombre_original", "descripcion", "bytes", "offset_ms", "url",
                                             "url_descarga")}
                          for a in _adjuntos_vista(sesion_id, lista_adjuntos, [])]
        return vista(
            request, "clase.html", s=s, api={"progreso": progreso}, materia_nombre=nombre_materia(s["materia_id"]),
            hay_audio=audio.is_file() and audio.stat().st_size > 0, eventos=eventos,
            fotos=[e for e in eventos if e["tipo"] == "foto" and e["ruta"]],
            audio_reproducible=audio_panel.suffix.lower() in audio_mod.MIME_AUDIO,
            transcript=transcript, timeline=timeline, items=items, url_pdf=_url_pdf(sesion_id, ruta_pdf),
            duracion_ms=s["duracion_ms"] or max((seg.get("fin_ms") or 0 for seg in transcript), default=0),
            actual={"version": vigente["version"], "creado": vigente["creado"]} if vigente is not None else None,
            anteriores=anteriores, version_pdf=versiones["version_pdf"], hay_tex=versiones["hay_tex"],
            hay_mas_nuevas=versiones["hay_mas_nuevas"], adjuntos_clase=adjuntos_clase,
            celu={"desfase_ms": celu.get("desfase_ms")} if celu is not None else None,
            hay_audio_celu=celu is not None and audio_celu.is_file(),
            audio_celu_reproducible=audio_celu.suffix.lower() in audio_mod.MIME_AUDIO,
            transcripts=_transcripts_disponibles(carpeta), ve_notas=INVITADO_VE_NOTAS,
        )

    def _versiones(sesion_id: str, apuntes: list[sqlite3.Row], actual: sqlite3.Row | None,
                   ruta_pdf: Path | None, publicaciones: list[sqlite3.Row]) -> dict[str, Any]:
        """La versión vigente del apunte (la principal), las anteriores con sus archivos y, si la
        vigente no compiló, qué versión es la que se ve o la que sigue publicada."""
        carpeta = cfg.sesion_dir(sesion_id)
        anteriores = []
        for a in apuntes:
            if actual is not None and a["id"] == actual["id"]:
                continue
            vdir = carpeta / "apunte" / f"v{a['version']}"
            anteriores.append({"a": a, "pdf": (vdir / "apunte.pdf").is_file(), "tex": (vdir / "apunte.tex").is_file()})
        version_pdf = None
        if ruta_pdf is not None:
            for a in apuntes:
                if a["ruta_pdf"] and _archivo_en(carpeta, carpeta / a["ruta_pdf"]) == ruta_pdf.resolve():
                    version_pdf = a["version"]
                    break
        aviso = None
        if actual is not None and not actual["compilo"]:
            vigente = next((p for p in publicaciones if p["estado"] == VIGENTE), None)
            if vigente is not None and vigente["version"] and vigente["version"] != actual["version"]:
                aviso = (f"La versión v{actual['version']} no compiló: en la carpeta de la materia sigue "
                         f"la v{vigente['version']}.")
            elif version_pdf is not None and version_pdf != actual["version"]:
                aviso = (f"La versión v{actual['version']} no compiló: el PDF de abajo es el de la "
                         f"v{version_pdf}, la última que compiló.")
            else:
                aviso = f"La versión v{actual['version']} no compiló y no hay otra versión con PDF."
        # Una más nueva que no compiló no reemplaza a la publicada que sí (publicar la deja como actual).
        mas_nuevas = [a["version"] for a in apuntes if actual is not None and a["version"] > actual["version"]]
        if aviso is None and mas_nuevas:
            aviso = (f"La versión v{max(mas_nuevas)} no compiló: en la carpeta de la materia sigue la "
                     f"v{actual['version']}, que es la vigente.")
        hay_tex = actual is not None and _tex_actual(sesion_id, actual) is not None
        return {"actual": actual, "anteriores": anteriores, "version_pdf": version_pdf,
                "aviso_version": aviso, "hay_tex": hay_tex, "hay_mas_nuevas": bool(mas_nuevas)}

    def _tex_actual(sesion_id: str, actual: sqlite3.Row) -> Path | None:
        carpeta = cfg.sesion_dir(sesion_id)
        relativa = actual["ruta_tex"] or f"apunte/v{actual['version']}/apunte.tex"
        return _archivo_en(carpeta, carpeta / relativa)

    def _texto_plano(ruta: Path, descarga: str | None = None) -> PlainTextResponse:
        """Un .tex o un transcript como texto; con `descarga`, para bajar con ese nombre."""
        encabezados = dict(SIN_CACHE)
        if descarga is not None:
            encabezados["Content-Disposition"] = _disposicion("attachment", descarga)
        return PlainTextResponse(sin_claves(ruta.read_text(encoding="utf-8", errors="replace")),
                                 media_type="text/plain; charset=utf-8", headers=encabezados)

    def _archivo(ruta: Path, media_type: str | None, nombre: str, descargar: str, *,
                 siempre_nombre: bool = False, encabezados: dict[str, str] | None = None) -> FileResponse:
        """Un archivo de la clase: con ?descargar=1, attachment con un nombre legible. Si no, inline (con
        ese nombre si `siempre_nombre`: el PDF, para cuando lo guardan desde el visor)."""
        if _descargar(descargar):
            return FileResponse(ruta, media_type=media_type, filename=nombre, content_disposition_type="attachment",
                                headers=encabezados)
        if siempre_nombre:
            return FileResponse(ruta, media_type=media_type, filename=nombre, content_disposition_type="inline",
                                headers=encabezados)
        return FileResponse(ruta, media_type=media_type, headers=encabezados)

    # Las rutas de lectura de una clase (también las del invitado: ver RUTAS_INVITADO). Ninguna escribe
    # nada ni dispara trabajo. Con ?descargar=1 bajan el archivo con un nombre legible.
    @lectura("/panel/clases/{sesion_id}/apunte.pdf")
    def clase_pdf(request: Request, sesion_id: str, descargar: str = "") -> FileResponse:
        """El PDF vigente. Las páginas lo piden con la marca de _url_pdf (?v=…), que acá se ignora."""
        with ctx_de(request).base.conexion() as conn:
            s = _sesion_o_404(request, conn, sesion_id)
            ruta = sesiones.ruta_pdf_actual(cfg, conn, sesion_id)
        if ruta is None:
            raise HTTPException(404, "el apunte todavía no está listo")
        return _archivo(ruta, "application/pdf", nombre_descarga(s, "apunte", ".pdf"), descargar,
                        siempre_nombre=True, encabezados=SIN_CACHE)

    @lectura("/panel/clases/{sesion_id}/apunte.tex")
    def clase_tex(request: Request, sesion_id: str, descargar: str = "") -> PlainTextResponse:
        """El .tex de la versión vigente (compile o no)."""
        with ctx_de(request).base.conexion() as conn:
            s = _sesion_o_404(request, conn, sesion_id)
            actual = apunte_actual(conn, sesion_id)
        ruta = _tex_actual(sesion_id, actual) if actual is not None else None
        if ruta is None:
            raise HTTPException(404, "todavía no hay apunte")
        return _texto_plano(ruta, nombre_descarga(s, "apunte", ".tex") if _descargar(descargar) else None)

    @lectura("/panel/clases/{sesion_id}/apunte/{archivo}")
    def clase_version(request: Request, sesion_id: str, archivo: str, descargar: str = ""):
        """PDF o .tex de una versión interna: sólo sesiones/<id>/apunte/vN/apunte.pdf|tex."""
        m = _VERSION_RE.fullmatch(archivo)
        if m is None:
            raise HTTPException(404, "archivo no encontrado")
        n, extension = int(m.group(1)), m.group(2)
        with ctx_de(request).base.conexion() as conn:
            s = _sesion_o_404(request, conn, sesion_id)
        carpeta = cfg.sesion_dir(sesion_id)
        ruta = _archivo_en(carpeta, carpeta / "apunte" / f"v{n}" / f"apunte.{extension}")
        if ruta is None:
            raise HTTPException(404, "archivo no encontrado")
        nombre = nombre_descarga(s, f"apunte v{n}", f".{extension}")
        if extension == "tex":
            return _texto_plano(ruta, nombre if _descargar(descargar) else None)
        return _archivo(ruta, "application/pdf", nombre, descargar, siempre_nombre=True, encabezados=SIN_CACHE)

    def _audio(ruta: Path, s: sqlite3.Row, cosa: str, descargar: str) -> FileResponse:
        tipo = audio_mod.MIME_AUDIO.get(ruta.suffix.lower(), "application/octet-stream")
        return _archivo(ruta, tipo, nombre_descarga(s, cosa, ruta.suffix.lower()), descargar)

    @lectura("/panel/clases/{sesion_id}/audio")
    def clase_audio(request: Request, sesion_id: str, descargar: str = "") -> FileResponse:
        """Lo que reproduce el panel: la copia en Opus (audio.panel.ogg) de un importado que el navegador
        no reproduce, si verificar la armó; si no, el original. Con Range: el reproductor salta. Para
        bajar, el mismo archivo."""
        with ctx_de(request).base.conexion() as conn:
            s = _sesion_o_404(request, conn, sesion_id)
            ruta = sesiones.ruta_audio_panel(cfg, conn, sesion_id)
        if not ruta.is_file():
            raise HTTPException(404, "no hay audio")
        return _audio(ruta, s, "audio", descargar)

    @lectura("/panel/clases/{sesion_id}/archivo/{ruta:path}")
    def clase_archivo(request: Request, sesion_id: str, ruta: str, descargar: str = "") -> FileResponse:
        """Una foto de la clase (las de la app y las preparadas para el apunte), nunca otro archivo."""
        with ctx_de(request).base.conexion() as conn:
            s = _sesion_o_404(request, conn, sesion_id)
            carpeta = cfg.sesion_dir(sesion_id).resolve()
            destino = (carpeta / ruta).resolve()
            try:
                relativa = destino.relative_to(carpeta)
            except ValueError as e:
                raise HTTPException(404, "archivo no encontrado") from e
            if not relativa.parts or relativa.parts[0] not in CARPETAS_FOTOS or not destino.is_file():
                raise HTTPException(404, "archivo no encontrado")
            foto = conn.execute("SELECT offset_ms FROM assets WHERE sesion_id = ? AND tipo = 'foto' AND ruta = ?",
                                (sesion_id, relativa.as_posix())).fetchone()
        cosa = (f"foto {_minuto_nombre(foto['offset_ms'])}" if foto is not None and foto["offset_ms"] is not None
                else f"foto {destino.stem}")
        return _archivo(destino, None, nombre_descarga(s, cosa, destino.suffix.lower()), descargar)

    @lectura("/panel/clases/{sesion_id}/adjuntos/{nombre}")
    def clase_adjunto(request: Request, sesion_id: str, nombre: str, descargar: str = "") -> FileResponse:
        """Un adjunto: el navegador lo abre (inline) o, con ?descargar=1, lo baja con el nombre original.
        Se busca en la base por la ruta que guardó el server (nunca se arma la ruta con lo que llega en
        la URL) y se revalida que el archivo esté dentro de sesiones/<id>/adjuntos."""
        with ctx_de(request).base.conexion() as conn:
            _sesion_o_404(request, conn, sesion_id)
            fila = conn.execute("SELECT * FROM assets WHERE sesion_id = ? AND tipo = ? AND ruta = ?",
                                (sesion_id, adjuntos.TIPO_ASSET, f"{adjuntos.CARPETA}/{nombre}")).fetchone()
        if fila is None:
            raise HTTPException(404, "adjunto no encontrado")
        adjunto = adjuntos.desde_fila(fila)
        carpeta = cfg.sesion_dir(sesion_id)
        ruta = _archivo_en(carpeta / adjuntos.CARPETA, carpeta / adjunto["ruta"])
        if ruta is None:
            raise HTTPException(404, "no está el archivo del adjunto")
        # nosniff: el navegador respeta el tipo (el que se detectó por el contenido) y no adivina otro.
        return FileResponse(ruta, media_type=adjunto["mime"], filename=_nombre_descarga(adjunto),
                            content_disposition_type="attachment" if _descargar(descargar) else "inline",
                            headers={"X-Content-Type-Options": "nosniff"})

    @router.post("/panel/clases/{sesion_id}/adjuntos")
    async def clase_subir_adjuntos(request: Request, sesion_id: str) -> RedirectResponse:
        """Adjuntar desde la PC uno o varios archivos: el mismo almacenamiento que la API, cada uno con un
        id nuevo y origen "panel", en cualquier estado de la clase (también en error, para sumar material
        antes de regenerar). Lo que no entra (tipo, tamaño) vuelve en el mensaje."""
        url = f"/panel/clases/{sesion_id}"
        base = ctx_de(request).base
        with base.conexion() as conn:  # 404 antes de recibir los archivos
            _sesion_o_404(request, conn, sesion_id)
        try:
            form = await request.form(max_files=MAX_ADJUNTOS_FORM, max_fields=20)
        except (StarletteHTTPException, MultiPartException, OSError) as e:
            detalle = getattr(e, "detail", None) or getattr(e, "message", None) or str(e)
            return volver(url, f"No se pudieron recibir los archivos (hasta {MAX_ADJUNTOS_FORM} por vez): "
                               f"{detalle}", "error")
        guardados: list[dict[str, Any]] = []
        rechazos: dict[str, list[str]] = {}  # motivo → nombres
        try:
            subidas = [v for v in form.getlist("archivos") if isinstance(v, UploadFile) and v.filename]
            for subida in subidas:
                try:
                    _, info = await run_in_threadpool(
                        adjuntos.guardar, cfg, base, sesion_id, adjunto_id=str(uuid.uuid4()), archivo=subida.file,
                        nombre_original=subida.filename, origen="panel")
                except NoExiste as e:  # se borró mientras llegaban los archivos
                    raise HTTPException(404, "sesión no encontrada") from e
                except (ArchivoGrande, adjuntos.TipoNoSoportado, ErrorDatos) as e:
                    rechazos.setdefault(str(e), []).append(subida.filename or "")
                else:
                    guardados.append(info)
        finally:
            await form.close()
        if not guardados and not rechazos:
            return volver(url, "Elegí uno o más archivos para adjuntar.", "error")
        return volver(url, *_mensaje_adjuntos(guardados, rechazos))

    # --- revisión del transcript y audio del celu (§15) ---
    def texto_confirmacion_celu(conn: sqlite3.Connection, base: Any) -> str:
        """El confirm() de "Agregar audio del celu": qué se rehace y cuánto cuesta, con la facturación de
        ahora y el promedio de las últimas revisiones y apuntes."""
        medios, _ = Facturacion(cfg, base).plan()
        partes = []
        for tipo, que in (("revisar_transcript", "la revisión"), ("apunte", "el apunte")):
            costos = [f["costo"] for f in conn.execute(
                "SELECT costo FROM jobs WHERE tipo = ? AND costo > 0 ORDER BY id DESC LIMIT 5", (tipo,))]
            if costos:
                partes.append(f"{que}, unos US$ {sum(costos) / len(costos):.2f}")
        cifras = (" (" + "; ".join(partes) + ", con la API)") if partes else ""
        medio = {MAX: "Ahora va por el plan Max: no se cobra aparte", API: "Ahora va por la API"}.get(
            medios[0] if medios else "", "Ahora ni la API ni el plan Max están disponibles")
        return ("Se va a transcribir el audio del celu (unos centavos de Gemini), volver a revisar el transcript "
                f"con las dos grabaciones y generar un apunte nuevo de esta clase.\n\n{medio}{cifras}.\n\n¿Seguir?")

    def _transcripts_disponibles(carpeta: Path) -> list[dict[str, str]]:
        salida = []
        for cual, (archivo, nombre) in TRANSCRIPTS.items():
            if (carpeta / archivo).is_file():
                salida.append({"cual": cual, "nombre": nombre})
        return salida

    @lectura("/panel/clases/{sesion_id}/transcript/{cual}")
    def clase_transcript(request: Request, sesion_id: str, cual: str, descargar: str = "") -> PlainTextResponse:
        """El transcript revisado, los crudos (el principal y el del celu) o el registro de cambios."""
        if cual not in TRANSCRIPTS:
            raise HTTPException(404, "archivo no encontrado")
        with ctx_de(request).base.conexion() as conn:
            s = _sesion_o_404(request, conn, sesion_id)
        carpeta = cfg.sesion_dir(sesion_id)
        ruta = _archivo_en(carpeta, carpeta / TRANSCRIPTS[cual][0])
        if ruta is None:
            raise HTTPException(404, "todavía no está")
        return _texto_plano(ruta, nombre_descarga(s, NOMBRES_TRANSCRIPT[cual], ".md") if _descargar(descargar) else None)

    @lectura("/panel/clases/{sesion_id}/audio-celu")
    def clase_audio_celu(request: Request, sesion_id: str, descargar: str = "") -> FileResponse:
        """El audio del celu (la segunda pista), con Range para saltar."""
        with ctx_de(request).base.conexion() as conn:
            s = _sesion_o_404(request, conn, sesion_id)
            ruta = sesiones.ruta_audio(cfg, conn, sesion_id, sesiones.PISTA_CELU)
        if not ruta.is_file():
            raise HTTPException(404, "no hay audio del celu")
        return _audio(ruta, s, "audio del celu", descargar)

    @router.post("/panel/clases/{sesion_id}/revision")
    async def clase_revision(request: Request, sesion_id: str) -> RedirectResponse:
        """Las dudas de la revisión: guardar lo elegido, "Listo" o "Generar sin revisar"."""
        datos = await formulario(request)
        url = f"/panel/clases/{sesion_id}"
        ctx = ctx_de(request)
        with ctx.base.conexion() as conn:
            _sesion_o_404(request, conn, sesion_id)
        accion = datos.get("accion", "guardar")
        if accion not in ("guardar", "listo", "sin_revisar"):
            return volver(url, f"Acción inválida: {accion!r}", "error")
        respuestas: dict[str, dict[str, Any]] = {}
        if accion != "sin_revisar":
            for clave, valor in datos.items():
                if not clave.startswith("eleccion_"):
                    continue
                duda = clave.removeprefix("eleccion_")
                if valor == "editar":
                    respuestas[duda] = {"texto": datos.get(f"texto_{duda}", "")}
                elif valor.isdigit():
                    respuestas[duda] = {"opcion": int(valor)}
        try:
            if respuestas:
                await run_in_threadpool(revision.responder, cfg, sesion_id, respuestas)
            if accion == "guardar":
                return volver(url, f"Guardado ({len(respuestas)} duda{'s' if len(respuestas) != 1 else ''}). "
                                   "El apunte arranca con \"Listo\".")
            con_propuesta = await run_in_threadpool(lambda: revision.terminar(ctx, sesion_id,
                                                                              sin_revisar=accion == "sin_revisar"))
        except revision.NoSePuede as e:
            return volver(url, f"No se pudo: {e}", "error")
        _despertar(request)
        detalle = (f" {con_propuesta} duda{'s quedaron' if con_propuesta != 1 else ' quedó'} con la propuesta del modelo."
                   if con_propuesta else "")
        return volver(url, "Revisión aplicada: el apunte quedó en cola." + detalle)

    @router.post("/panel/clases/{sesion_id}/celu")
    async def clase_agregar_celu(request: Request, sesion_id: str) -> RedirectResponse:
        """"Agregar audio del celu": un audio de la misma clase grabado con otra app. Se mide el desfase por
        correlación y se reprocesa lo necesario (segunda pista, revisión, apunte)."""
        url = f"/panel/clases/{sesion_id}"
        ctx = ctx_de(request)
        with ctx.base.conexion() as conn:  # 404 antes de recibir el audio
            _sesion_o_404(request, conn, sesion_id)
        try:
            form = await request.form(max_files=1, max_fields=10)
        except (StarletteHTTPException, MultiPartException, OSError) as e:
            detalle = getattr(e, "detail", None) or getattr(e, "message", None) or str(e)
            return volver(url, f"No se pudo recibir el audio: {detalle}", "error")
        try:
            subida = form.get("audio")
            if not isinstance(subida, UploadFile) or not subida.filename:
                return volver(url, "Elegí el audio del celu.", "error")
            try:
                datos = await run_in_threadpool(sesiones.agregar_audio_celu, cfg, ctx.base, sesion_id, subida.file,
                                                subida.filename)
                await run_in_threadpool(reprocesar_celu, ctx, sesion_id)
            except (Conflicto, ErrorDatos, ArchivoGrande, LookupError) as e:
                return volver(url, f"No se pudo agregar el audio del celu: {e}", "error")
        finally:
            await form.close()
        _despertar(request)
        return volver(url, f"Audio del celu agregado ({_duracion(datos.get('duracion_ms'))}): se mide el desfase, "
                           "se transcribe y se vuelve a revisar el transcript.")

    @router.post("/panel/clases/{sesion_id}/reprocesar")
    async def clase_reprocesar(request: Request, sesion_id: str) -> RedirectResponse:
        datos = await formulario(request)
        url = f"/panel/clases/{sesion_id}"
        desde = datos.get("desde", "")
        if desde not in ETAPAS:
            return volver(url, f"Etapa inválida: {desde!r}", "error")
        try:
            reprocesar(ctx_de(request), sesion_id, desde)
        except LookupError as e:
            return volver(url, f"No se pudo reprocesar: {e}", "error")
        _despertar(request)
        return volver(url, f"Reprocesando desde {desde}.")

    @router.post("/panel/clases/{sesion_id}/materia")
    async def clase_materia(request: Request, sesion_id: str) -> RedirectResponse:
        datos = await formulario(request)
        url = f"/panel/clases/{sesion_id}"
        try:
            estado = sesiones.asignar_materia(cfg, ctx_de(request).base, sesion_id, datos.get("materia_id", ""))
        except NoExiste as e:
            raise HTTPException(404, "sesión no encontrada") from e
        except (Conflicto, ErrorDatos) as e:
            return volver(url, f"No se pudo asignar la materia: {e}", "error")
        if estado == "esperando_usuario":
            _despertar(request)
            return volver(url, "Materia asignada: sigue la revisión del transcript y el apunte.")
        return volver(url, "Materia asignada.")

    @router.post("/panel/clases/{sesion_id}/borrar")
    async def clase_borrar(request: Request, sesion_id: str) -> RedirectResponse:
        datos = await formulario(request)
        url = f"/panel/clases/{sesion_id}"
        if datos.get("confirmar") != "si":
            return volver(url, "Para borrar hay que confirmar.", "error")
        try:
            avisos = borrar_sesion(ctx_de(request), sesion_id)
        except NoExiste as e:
            raise HTTPException(404, "sesión no encontrada") from e
        except Conflicto as e:
            return volver(url, f"No se pudo borrar: {e}", "error")
        msg = "Sesión borrada." + (" Avisos: " + "; ".join(avisos) if avisos else "")
        return volver("/panel/clases", msg, "error" if avisos else "ok")

    def _despertar(request: Request) -> None:
        worker = getattr(request.app.state, "worker", None)
        if worker is not None:
            worker.despertar()

    # --- importar clase ---
    # Un pedido a la vez por análisis: volver a analizar (otras fotos), importar y cancelar esperan al
    # que está en curso, así ninguno ve las fotos o el material a medio reemplazar.
    locks_importar: dict[str, asyncio.Lock] = {}

    async def tomar_analisis(pila: AsyncExitStack, token: str) -> None:
        await pila.enter_async_context(locks_importar.setdefault(token, asyncio.Lock()))

    def error_importar(status: int, detalle: str, campo: str | None = None) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": sin_claves(detalle), "campo": campo})

    def _sin(carpeta: Path) -> None:
        shutil.rmtree(carpeta, ignore_errors=True)

    @router.get("/panel/importar", response_class=HTMLResponse)
    def importar_formulario(request: Request) -> HTMLResponse:
        base = ctx_de(request).base
        try:  # lo abandonado (24 h): también lo hacen el arranque y el worker, esto es por si no hay worker
            importador.limpiar_temporales(cfg)
        except Exception:
            log.exception("no pude limpiar el área temporal de importaciones")
        def tipos(lista: list[str] | tuple[str, ...]) -> list[dict[str, str]]:
            return [{"valor": t, "texto": panel_importar.TEXTO_TIPO.get(t, t)} for t in lista]

        with base.conexion() as conn:
            lista = [{"id": m.id, "nombre": m.nombre, "proximo": materias.proximo_numero(conn, m),
                      "tipos": tipos(m.tipos_clase)} for m in cfg.materias_activas]
            confirmacion = panel_importar.texto_confirmacion(
                cfg, base, conn, MODOS_FACTURACION.get(cfg.claude.facturacion, cfg.claude.facturacion))
        datos_js = {"materias": lista, "tipos_auto": tipos(sesiones.TIPOS_CLASE), "confirmacion": confirmacion,
                    "max_audio": sesiones.MAX_AUDIO_IMPORTADO, "max_foto": sesiones.MAX_FOTO,
                    "max_material": adjuntos.MAX_ADJUNTO, "max_fotos": panel_importar.MAX_FOTOS_FORM,
                    "max_materiales": panel_importar.MAX_MATERIAL_FORM,
                    "tope_dominio": TOPE_SUBIDA_DOMINIO if dominio(request) else None}
        return vista(request, "importar.html", materias_form=lista, datos_js=datos_js,
                     acepta_audio=panel_importar.ACEPTA_AUDIO, acepta_adjuntos=ACEPTA_ADJUNTOS,
                     tipos_adjunto=adjuntos.TEXTO_TIPOS, max_adjunto_mb=adjuntos.MAX_ADJUNTO // (1024 * 1024),
                     max_foto_mb=sesiones.MAX_FOTO // (1024 * 1024))

    @router.post("/panel/importar/analizar")
    async def importar_analizar(request: Request) -> JSONResponse:
        """Paso "Analizar": el audio (`audio`, con `audio_modificado` = File.lastModified en ms) y las fotos
        (`fotos`) quedan en el área temporal y vuelve lo de importador.analizar_archivos, con un `token`.
        Con `token` (antes que los archivos) y sin audio, analiza el mismo audio con las fotos que vengan,
        que reemplazan a las anteriores (también si no viene ninguna); hasta que termina, analisis.json
        queda marcado (panel_importar.marcar_reanalisis). Errores: {detail, campo}."""
        estado: dict[str, Any] = {"token": None, "nuevo": False}
        async with AsyncExitStack() as pila:
            async def token_de(campos: dict[str, str]) -> str:
                token = campos.get("token", "").strip()
                if token:
                    carpeta = importador.ruta_temporal(cfg, token)  # ErrorImportacion si no vale o ya no está
                    await tomar_analisis(pila, token)
                    # Las fotos de antes se van a reemplazar: si esto se corta, Importar lo sabe.
                    await run_in_threadpool(panel_importar.marcar_reanalisis, carpeta)
                else:
                    token, _ = await run_in_threadpool(importador.crear_temporal, cfg)
                    estado["nuevo"] = True
                estado["token"] = token
                return token

            async def descartar(siempre: bool = False) -> None:
                """Un análisis nuevo que falló no queda; uno que ya no sirve (audio ilegible), tampoco."""
                if estado["token"] and (siempre or estado["nuevo"]):
                    await run_in_threadpool(importador.borrar_temporal, cfg, estado["token"])

            try:
                # Hasta max_fotos fotos (lo que manda el JS) más el audio.
                recibido = await panel_importar.recibir(request, cfg, archivos=panel_importar.ARCHIVOS_ANALIZAR,
                                                        token_de=token_de, reemplazar=("audio", "fotos"),
                                                        max_archivos=panel_importar.MAX_FOTOS_FORM + 1)
            except AudioGrande as e:
                await descartar()
                return error_importar(413, _frase(str(e)), "audio")
            except ErrorImportacion as e:
                await descartar()
                return error_importar(422, _frase(str(e)), "token")
            except ErrorRecepcion as e:
                await descartar()
                return error_importar(422, f"No se pudieron recibir los archivos: {e}.")
            except OSError as e:  # el disco lleno con un audio de 2 GB, por ejemplo
                log.warning("importar desde el panel: no pude guardar lo que llegó: %s", e)
                await descartar()
                return error_importar(503, _frase(f"No se pudieron recibir los archivos: "
                                                  f"{panel_importar.texto_error_disco(e)}"))
            except BaseException:  # ClientDisconnect (se canceló la subida) y lo inesperado
                await descartar()
                raise

            token = estado["token"]
            if token is None:
                return error_importar(422, "Elegí el audio de la clase.", "audio")
            carpeta = importador.dir_temporal(cfg) / token
            audios = recibido.archivos.get("audio", [])
            if len(audios) > 1:
                await descartar()
                return error_importar(422, "Mandá un solo audio.", "audio")
            elegido = panel_importar.audio_a_analizar(carpeta, audios[0] if audios else None,
                                                      recibido.campos.get("audio_modificado"))
            if elegido is None:
                await descartar()
                return error_importar(422, "Elegí el audio de la clase.", "audio")
            ruta_audio, nombre_audio, modificado_ms = elegido
            fotos = recibido.archivos.get("fotos", [])
            if not estado["nuevo"] and not fotos:  # se sacaron todas las fotos
                await run_in_threadpool(_sin, carpeta / "fotos")
            try:
                resultado = await run_in_threadpool(lambda: importador.analizar_archivos(
                    cfg, ruta_audio, [r for r, _ in fotos], nombre_original=nombre_audio,
                    mtime_audio=panel_importar.modificado(modificado_ms, cfg.zona_horaria)))
            except AudioGrande as e:
                await descartar(siempre=True)
                return error_importar(413, _frase(str(e)), "audio")
            except ErrorImportacion as e:
                await descartar(siempre=True)
                if "no es un audio legible" in str(e):  # sin la salida de ffprobe ni la ruta del área temporal
                    log.info("importar desde el panel: %s", e)
                    return error_importar(422, f"No se puede importar: {nombre_audio} no es un audio legible (ffmpeg "
                                               "no lo reconoce). ¿Es el archivo que era?", "audio")
                return error_importar(422, f"No se puede importar: {e}.", "audio")
            respuesta = panel_importar.vista_analisis(cfg.zona_horaria, token, resultado, fotos, recibido.rechazados)

            def relativa(ruta: Path) -> str:
                return ruta.resolve().relative_to(carpeta.resolve()).as_posix()

            try:  # sin la marca de reanalisis: este análisis es el que vale
                await run_in_threadpool(escribir_json, carpeta / panel_importar.ANALISIS, {
                    "audio": {"archivo": relativa(ruta_audio), "nombre": nombre_audio, "modificado_ms": modificado_ms},
                    "fotos": [{"archivo": relativa(r), "nombre": n} for r, n in fotos],
                    "resultado": respuesta,
                    "analizado": ahora_iso(cfg.zona_horaria),
                })
            except OSError as e:
                log.warning("importar desde el panel: no pude guardar el análisis: %s", e)
                await descartar()
                return error_importar(503, _frase(f"No se pudo guardar el análisis: "
                                                  f"{panel_importar.texto_error_disco(e)}"))
        return JSONResponse(respuesta)

    @router.post("/panel/importar")
    async def importar_crear(request: Request) -> JSONResponse:
        """"Importar y procesar": `token` (el del análisis, antes que los archivos), `materia_id` ("auto" o
        vacío = Auto), `tipo_clase`, `numero_clase` (en Auto no cuenta), `inicio` (datetime-local, en la
        zona de Classmer) y `material` (PDF o imágenes: adjuntos de la clase). Importa con el importador
        de siempre (dispositivo "panel"), borra el análisis y responde 201 {id, url, …}; un error vuelve
        como {detail, campo} y el análisis queda para corregir y volver a mandar."""
        estado: dict[str, Any] = {"token": None}
        async with AsyncExitStack() as pila:
            async def token_de(campos: dict[str, str]) -> str:
                token = campos.get("token", "").strip()
                if not token:
                    raise ErrorImportacion(panel_importar.FALTA_ANALISIS)
                importador.ruta_temporal(cfg, token)
                await tomar_analisis(pila, token)
                estado["token"] = token
                return token

            try:
                recibido = await panel_importar.recibir(
                    request, cfg, archivos=panel_importar.ARCHIVOS_IMPORTAR, token_de=token_de,
                    reemplazar=("material",), max_archivos=panel_importar.MAX_MATERIAL_FORM)
            except ErrorImportacion as e:
                return error_importar(422, _frase(str(e)), "token")
            except ErrorRecepcion as e:
                return error_importar(422, f"No se pudo recibir el formulario: {e}.")
            except OSError as e:
                log.warning("importar desde el panel: no pude guardar el material: %s", e)
                return error_importar(503, _frase(f"No se pudo recibir el material: "
                                                  f"{panel_importar.texto_error_disco(e)}"), "material")
            token = estado["token"]
            if token is None:
                return error_importar(422, _frase(panel_importar.FALTA_ANALISIS), "token")
            carpeta = importador.dir_temporal(cfg) / token
            material = recibido.archivos.get("material", [])
            try:
                datos = panel_importar.datos_importacion(cfg, recibido.campos, carpeta)
                # El audio se mueve del área temporal a la sesión (mismo disco: no ocupa el doble); si la
                # importación falla, el importador lo devuelve y borra la sesión a medio crear.
                imp = await run_in_threadpool(lambda: importador.importar_clase(
                    ctx_de(request), datos["audio"], materia_id=datos["materia_id"], fotos=datos["fotos"],
                    inicio=datos["inicio"], tipo_clase=datos["tipo_clase"], numero_clase=datos["numero_clase"],
                    material=material, dispositivo="panel", usar_mtime_fotos=False, usar_mtime_audio=False,
                    mtime_audio=datos["mtime_audio"], nombre_original=datos["nombre_original"],
                    origen_material="panel", inicio_elegido=datos["inicio_elegido"],
                    candidatos_panel=datos["candidatos_panel"], mover_audio=True))
            except panel_importar.CampoInvalido as e:
                await run_in_threadpool(_sin, carpeta / "material")  # el próximo intento lo vuelve a mandar
                return error_importar(422, str(e), e.campo)
            except AudioGrande as e:
                await run_in_threadpool(_sin, carpeta / "material")
                return error_importar(413, _frase(str(e)), "audio")
            except ErrorImportacion as e:
                await run_in_threadpool(_sin, carpeta / "material")
                return error_importar(422, f"No se pudo importar: {e}.")
            except OSError as e:  # el disco lleno, un archivo trabado…: el análisis queda para reintentar
                log.warning("importar desde el panel: %s", e)
                await run_in_threadpool(_sin, carpeta / "material")
                return error_importar(503, _frase(f"No se pudo importar: {panel_importar.texto_error_disco(e)}"))
            await run_in_threadpool(importador.borrar_temporal, cfg, token)
            locks_importar.pop(token, None)
        _despertar(request)

        rechazado = imp.material_rechazado + [{"nombre": r["nombre"], "motivo": r["motivo"]}
                                              for r in recibido.rechazados]
        avisos = panel_importar.sin_aviso_material(imp.avisos)
        msg = "Clase importada: quedó en cola para procesar."
        if imp.material:
            msg += f" Material: {len(imp.material)} adjunto{'s' if len(imp.material) != 1 else ''}."
        if rechazado:
            msg += " No entraron: " + "; ".join(f"{_corto(r['nombre'])} ({r['motivo']})" for r in rechazado) + "."
        if avisos:
            msg += " Avisos: " + "; ".join(avisos) + "."
        tipo = "aviso" if rechazado or avisos else "ok"
        return JSONResponse(status_code=201, content={
            "id": imp.sesion_id, "url": con_msg(f"/panel/clases/{imp.sesion_id}", msg, tipo), "estado": "en_cola",
            "material": imp.material, "material_rechazado": rechazado, "avisos": avisos})

    @router.post("/panel/importar/{token}/cancelar")
    async def importar_cancelar(token: str) -> JSONResponse:
        """"Cancelar" (y el beacon al irse de la página sin importar): borra el análisis. Idempotente."""
        if not importador.token_valido(token):
            return error_importar(404, "Análisis inválido.")
        async with locks_importar.setdefault(token, asyncio.Lock()):
            borrado = await run_in_threadpool(importador.borrar_temporal, cfg, token)
        locks_importar.pop(token, None)
        return JSONResponse({"ok": True, "borrado": borrado})

    # --- materias ---
    def _resumen_materias(conn, lista: list[Any] | None = None) -> list[dict[str, Any]]:
        salida = []
        for m in cfg.materias if lista is None else lista:
            clases_total = conn.execute("SELECT COUNT(*) FROM sesiones WHERE materia_id = ?", (m.id,)).fetchone()[0]
            abiertos = conn.execute("SELECT COUNT(*) FROM items WHERE materia_id = ? AND estado = 'abierto'",
                                    (m.id,)).fetchone()[0]
            perfil = m.ruta_perfil()
            salida.append({
                "m": m, "proximo": materias.proximo_numero(conn, m),
                "contadas": materias.clases_contadas(conn, m.id), "total": clases_total,
                "abiertos": abiertos, "carpeta_ok": bool(m.carpeta and m.carpeta.is_dir()),
                "perfil": perfil.name if perfil else None,
            })
        return salida

    @router.get("/panel/materias", response_class=HTMLResponse)
    def materias_lista(request: Request) -> HTMLResponse:
        with ctx_de(request).base.conexion() as conn:
            resumen = _resumen_materias(conn)
        return vista(request, "materias.html", materias=resumen,
                     config_viva=request.app.state.config_viva)

    def _valores_form(datos: dict[str, Any]) -> dict[str, Any]:
        """Del formulario a los cambios de config_vivo (vacío = null)."""
        tipos = [t for t in re.split(r"[,\s]+", datos.get("tipos_clase", "")) if t]
        valores = {
            "nombre": datos.get("nombre", ""),
            "carpeta": datos.get("carpeta", ""),
            "perfil": datos.get("perfil", ""),
            "carpeta_clases": datos.get("carpeta_clases", ""),
            "calendario": {"calendario": datos.get("calendario", ""), "titulo_regex": datos.get("titulo_regex", "")},
            "tipos_clase": tipos,
            "estilo": datos.get("estilo", ""),
            "activa": datos.get("activa") == "on",
        }
        # Tipo de revisión del transcript (§15). Un formulario sin la sección (otro cliente) no lo toca.
        if "revision" in datos:
            valores["revision"] = datos.get("revision", "")
        if "profesor" in datos:
            valores["profesor"] = datos.get("profesor", "")
        # Modelo del apunte: None hereda el global; si no, el propio de la materia. Un formulario
        # sin la sección (otro cliente) no lo toca.
        if "apunte_origen" in datos:
            valores["apunte"] = None
            if datos["apunte_origen"] == "propio":
                valores["apunte"] = {"modelo": datos.get("apunte_modelo", "").strip(),
                                     "esfuerzo": datos.get("apunte_esfuerzo", "").strip() or None}
        return valores

    def _validar_apunte(conn: sqlite3.Connection, cambios: dict[str, Any], materia_id: str | None) -> None:
        """El modelo propio sale de la lista de "apunte" (el catálogo o el que ya tiene la materia)."""
        propio = cambios.get("apunte")
        if propio is None:
            return
        materia = cfg.materia(materia_id) if materia_id else None
        actual = materia.apunte.modelo if materia is not None and materia.apunte is not None else cfg.apunte.modelo
        _elegir(conn, "apunte", propio["modelo"], actual)
        _esfuerzo_form(propio["esfuerzo"], "apunte")

    def _form_apunte(request: Request, propio: dict[str, Any] | None) -> dict[str, Any]:
        """Lo que necesita la sección "Modelo del apunte" del formulario de una materia."""
        elegido = (propio or {}).get("modelo") or cfg.apunte.modelo
        with ctx_de(request).base.conexion() as conn:
            opciones = modelos.opciones_tarea(conn, "apunte", elegido)
            fila = conn.execute("SELECT nombre FROM modelos WHERE id = ? AND nombre IS NOT NULL LIMIT 1",
                                (cfg.apunte.modelo,)).fetchone() if cfg.apunte.modelo else None
        nombre_global = cfg.apunte.modelo or "el modelo por defecto"
        if fila is not None and fila["nombre"] != cfg.apunte.modelo:
            nombre_global = f"{fila['nombre']} ({cfg.apunte.modelo})"
        return {"opciones_apunte": opciones, "apunte_global": nombre_global,
                "esfuerzo_global": cfg.apunte.esfuerzo, "esfuerzos": ESFUERZOS}

    def _entero(texto: str | None) -> int | None:
        texto = (texto or "").strip()
        if not texto:
            return None
        try:
            return int(texto)
        except ValueError as e:
            raise ConfigError(f"número inválido: {texto!r}") from e

    @router.get("/panel/materias/nueva", response_class=HTMLResponse)
    def materia_nueva(request: Request) -> HTMLResponse:
        valores = {"id": "", "nombre": "", "carpeta": "", "perfil": "", "carpeta_clases": "",
                   "calendario": {"calendario": "", "titulo_regex": ""}, "tipos_clase": ["teorica", "practica"],
                   "estilo": "", "numero_clase_inicial": 1, "activa": True, "apunte": None,
                   "revision": REVISION_DEFAULT, "profesor": ""}
        return vista(request, "materia.html", nueva=True, v=valores, proximo=1, contadas=0,
                     **_form_apunte(request, None))

    @router.post("/panel/materias/nueva")
    async def materia_crear(request: Request):
        datos = await formulario(request)
        cambios = _valores_form(datos)
        cambios["id"] = datos.get("id", "").strip()
        try:
            inicial = _entero(datos.get("numero_clase_inicial"))
            if inicial is not None:
                cambios["numero_clase_inicial"] = inicial
            with ctx_de(request).base.conexion() as conn:
                _validar_apunte(conn, cambios, None)
            request.app.state.config_viva.agregar_materia(cambios)
        except ConfigError as e:
            valores = {"apunte": None, **cambios, "numero_clase_inicial": datos.get("numero_clase_inicial", 1)}
            return vista(request, "materia.html", status_code=422, nueva=True, v=valores, proximo=None,
                         contadas=0, msg=str(e), tipo_msg="error", **_form_apunte(request, valores["apunte"]))
        return volver(f"/panel/materias/{cambios['id']}", "Materia agregada. Ya la ve la app.")

    @router.get("/panel/materias/{materia_id}", response_class=HTMLResponse)
    def materia_editar(request: Request, materia_id: str) -> HTMLResponse:
        valores = materia_cruda(cfg.home, materia_id)
        materia = cfg.materia(materia_id)
        if valores is None or materia is None:
            raise HTTPException(404, "materia no encontrada")
        with ctx_de(request).base.conexion() as conn:
            proximo = materias.proximo_numero(conn, materia)
            contadas = materias.clases_contadas(conn, materia_id)
        return vista(request, "materia.html", nueva=False, v=valores, proximo=proximo, contadas=contadas,
                     **_form_apunte(request, valores.get("apunte")))

    @router.post("/panel/materias/{materia_id}")
    async def materia_guardar(request: Request, materia_id: str):
        datos = await formulario(request)
        materia = cfg.materia(materia_id)
        if materia is None:
            raise HTTPException(404, "materia no encontrada")
        cambios = _valores_form(datos)
        base = ctx_de(request).base
        try:
            proximo = _entero(datos.get("proximo_numero"))
            inicial = _entero(datos.get("numero_clase_inicial"))
            if proximo is not None and str(proximo) != datos.get("proximo_original", "").strip():
                if proximo < 1:
                    raise ConfigError("el próximo número tiene que ser 1 o más")
                with base.conexion() as conn:
                    cambios["numero_clase_inicial"] = materias.inicial_para(conn, materia, proximo)
            elif inicial is not None:
                cambios["numero_clase_inicial"] = inicial
            with base.conexion() as conn:
                _validar_apunte(conn, cambios, materia_id)
            request.app.state.config_viva.editar_materia(materia_id, cambios)
        except MateriaNoExiste as e:
            raise HTTPException(404, "materia no encontrada") from e
        except ConfigError as e:
            with base.conexion() as conn:
                contadas = materias.clases_contadas(conn, materia_id)
            propio = materia.apunte.model_dump() if materia.apunte is not None else None
            valores = {"apunte": propio, "revision": materia.revision, **cambios, "id": materia_id,
                       "numero_clase_inicial": datos.get("numero_clase_inicial", materia.numero_clase_inicial)}
            return vista(request, "materia.html", status_code=422, nueva=False, v=valores,
                         proximo=datos.get("proximo_numero"), contadas=contadas, msg=str(e), tipo_msg="error",
                         **_form_apunte(request, valores["apunte"]))
        return volver(f"/panel/materias/{materia_id}", "Cambios guardados en config.yaml y aplicados.")

    # --- pendientes (dashboard por materia) ---
    @lectura("/panel/pendientes", response_class=HTMLResponse)
    def pendientes(request: Request, materia: str | None = None, ver: str = "abiertos") -> HTMLResponse:
        visibles = materias_de(request)  # un invitado, sólo las suyas
        with ctx_de(request).base.conexion() as conn:
            if not materia:
                return vista(request, "pendientes_resumen.html", materias=_resumen_materias(conn, visibles),
                             por_tipo={m.id: dict(conn.execute(
                                 "SELECT tipo, COUNT(*) FROM items WHERE materia_id = ? AND estado = 'abierto' "
                                 "GROUP BY tipo", (m.id,)).fetchall()) for m in visibles})
            m = cfg.materia(materia)
            if m is None or m not in visibles:
                raise HTTPException(404, "materia no encontrada")
            filtro = "" if ver == "todos" else "AND i.estado = 'abierto'"
            # Un ítem de una clase que ahora es de otra materia no se le muestra a un invitado.
            de_la_materia = "AND (i.sesion_id IS NULL OR s.materia_id = i.materia_id)" if invitado_de(request) else ""
            filas = conn.execute(
                f"""
                SELECT i.*, s.inicio, s.numero_clase, s.titulo AS clase_titulo
                FROM items i LEFT JOIN sesiones s ON s.id = i.sesion_id
                WHERE i.materia_id = ? {filtro} {de_la_materia}
                ORDER BY s.inicio DESC, i.offset_ms, i.id
                """, (materia,)).fetchall()
            recientes = [con_url_pdf(conn, f) for f in conn.execute(
                "SELECT * FROM sesiones WHERE materia_id = ? ORDER BY inicio DESC, creado DESC LIMIT 10",
                (materia,))]
            proximo = materias.proximo_numero(conn, m)
        grupos = {t: [f for f in filas if f["tipo"] == t] for t in TIPOS_ITEM}
        otros = [f for f in filas if f["tipo"] not in TIPOS_ITEM]
        if otros:
            grupos["otros"] = otros
        return vista(request, "pendientes.html", m=m, grupos=grupos, recientes=recientes, ver=ver,
                     proximo=proximo, total=len(filas))

    @router.post("/panel/items/{item_id}")
    async def item_marcar(request: Request, item_id: int) -> RedirectResponse:
        datos = await formulario(request)
        estado = datos.get("estado", "")
        volver_a = datos.get("volver", "/panel/pendientes")
        if not volver_a.startswith("/panel/"):
            volver_a = "/panel/pendientes"
        if estado not in ESTADOS_ITEM:
            return volver(volver_a, f"Estado inválido: {estado!r}", "error")
        with ctx_de(request).base.conexion() as conn:
            cur = conn.execute("UPDATE items SET estado = ? WHERE id = ?", (estado, item_id))
        if not cur.rowcount:
            raise HTTPException(404, "item no encontrado")
        textos = {"hecho": "Marcado como hecho.", "descartado": "Descartado.", "abierto": "Reabierto."}
        return volver(volver_a, textos[estado])

    # --- invitados (sólo el dueño: el invitado no llega acá, RUTAS_INVITADO) ---
    URL_INVITADOS = "/panel/invitados"

    def solo_el_dueno(request: Request) -> None:
        """El middleware ya le da 403 al invitado; esto es por las dudas."""
        if invitado_de(request) is not None:
            raise HTTPException(403, "sólo lectura")

    async def form_invitado(request: Request) -> tuple[str, list[str]]:
        form = await request.form()
        email = form.get("email")
        return (email if isinstance(email, str) else ""), [m for m in form.getlist("materias") if isinstance(m, str)]

    @router.get(URL_INVITADOS, response_class=HTMLResponse)
    def invitados(request: Request) -> HTMLResponse:
        solo_el_dueno(request)
        orden = [m for m in cfg.materias if m.activa] + [m for m in cfg.materias if not m.activa]
        lista = [{"email": i.email, "materias": set(i.materias), "ultima": visitas.ultima(i.email)}
                 for i in cfg.panel.invitados]
        panel = cfg.panel
        return vista(request, "invitados.html", invitados=lista, materias_orden=orden,
                     dominio_publico=panel.dominio_publico, access_completa=panel.cloudflare_access.completa,
                     duenos=panel.cloudflare_access.emails)

    @router.post(f"{URL_INVITADOS}/agregar")
    async def invitados_agregar(request: Request) -> RedirectResponse:
        solo_el_dueno(request)
        email, elegidas = await form_invitado(request)
        try:
            email = request.app.state.config_viva.poner_invitado(email, elegidas, nuevo=True)
        except ConfigError as e:
            return volver(URL_INVITADOS, f"No se pudo agregar: {e}", "error")
        aviso = "" if elegidas else " Todavía no ve ninguna materia: elegíselas en su fila."
        return volver(URL_INVITADOS, f"{email} agregado: ya puede entrar por el dominio si también está en la "
                                     f"policy de Cloudflare Access.{aviso}", "aviso" if aviso else "ok")

    @router.post(f"{URL_INVITADOS}/guardar")
    async def invitados_guardar(request: Request) -> RedirectResponse:
        solo_el_dueno(request)
        email, elegidas = await form_invitado(request)
        try:
            email = request.app.state.config_viva.poner_invitado(email, elegidas, nuevo=False)
        except ConfigError as e:
            return volver(URL_INVITADOS, f"No se pudo guardar: {e}", "error")
        return volver(URL_INVITADOS, f"Guardado: {email} ve {len(elegidas)} materia{'' if len(elegidas) == 1 else 's'}"
                                     " desde su próximo pedido.")

    @router.post(f"{URL_INVITADOS}/sacar")
    async def invitados_sacar(request: Request) -> RedirectResponse:
        solo_el_dueno(request)
        email, _ = await form_invitado(request)
        try:
            sacado = request.app.state.config_viva.sacar_invitado(email)
        except ConfigError as e:
            return volver(URL_INVITADOS, f"No se pudo sacar: {e}", "error")
        if not sacado:
            return volver(URL_INVITADOS, f"{email.strip() or 'Ese mail'} ya no era invitado.", "aviso")
        return volver(URL_INVITADOS, f"{email.strip().lower()} ya no entra (desde su próximo pedido). Si querés, "
                                     "sacalo también de la policy de Cloudflare Access.")

    # --- estilo ---
    @router.get("/panel/estilo", response_class=HTMLResponse)
    def estilo(request: Request) -> HTMLResponse:
        return vista(request, "estilo.html", estilo_global=cfg.estilo_global,
                     contrato=_leer_texto(PROMPTS_DIR / "apunte.md") or "(no encontré prompts/apunte.md)")

    @router.post("/panel/estilo")
    async def estilo_guardar(request: Request):
        datos = await formulario(request)
        try:
            request.app.state.config_viva.editar_estilo_global(datos.get("estilo_global", ""))
        except ConfigError as e:
            return volver("/panel/estilo", f"No se pudo guardar: {e}", "error")
        return volver("/panel/estilo", "Estilo guardado: se usa desde el próximo apunte.")

    # --- modelos y facturación ---
    def _facturacion(request: Request) -> dict[str, Any]:
        """Modo y estado de cada medio de facturación de Claude (Modelos y Estado)."""
        fact = Facturacion(cfg, ctx_de(request).base)
        medios = []
        for medio in (API, MAX):
            e = fact.estado(medio)
            medios.append({"medio": medio, "etiqueta": ETIQUETA_MEDIO[medio], "e": e, "texto": fact.describir(e),
                           "reintentable": not e.ok and e.estado != "sin_clave"})
        return {"modo": fact.modo, "modo_texto": MODOS_FACTURACION.get(fact.modo, fact.modo),
                "modos": [(m, MODOS_FACTURACION.get(m, m) + (" (por defecto)" if m == MODO_DEFAULT else ""))
                          for m in FACTURACIONES],
                "medios": medios, "hay_clave": fact.hay_clave, "horas": cfg.claude.reintentar_api_h,
                "aviso_sin_clave": AVISO_SIN_CLAVE if not fact.hay_clave and fact.modo in MODOS_CON_API else None}

    @router.get("/panel/modelos", response_class=HTMLResponse)
    def modelos_ver(request: Request) -> HTMLResponse:
        with ctx_de(request).base.conexion() as conn:
            opciones = {
                "transcribir": modelos.opciones_tarea(conn, "transcribir", cfg.transcribir.modelo),
                "clasificar": modelos.opciones_tarea(conn, "clasificar", cfg.clasificar.modelo),
                "revisar_transcript": modelos.opciones_tarea(conn, "revisar_transcript", cfg.revisar_transcript.modelo),
                "apunte": modelos.opciones_tarea(conn, "apunte", cfg.apunte.modelo),
            }
            lista = modelos.catalogo(conn)
            descubrimientos = modelos.ultimo_descubrimiento(conn)
        elegidos = modelos.elegidos_config(cfg)
        ids = {m["id"] for m in lista}
        for m in lista:
            m["donde"] = [lugar for t in modelos.TAREAS for lugar in elegidos[t].get(m["id"], [])]
        fuera = [{"id": id_, "tarea": t, "donde": lugares}
                 for t in modelos.TAREAS for id_, lugares in elegidos[t].items() if id_ not in ids]
        proveedores = [{
            "id": p, "nombre": modelos.NOMBRE_PROVEEDOR[p], "variable": modelos.CLAVE_PROVEEDOR[p],
            "hay_clave": bool(cfg.anthropic_api_key if p == modelos.ANTHROPIC else cfg.gemini_api_key),
            "d": descubrimientos.get(p),
        } for p in modelos.PROVEEDORES]
        propias = [{"m": m, "modelo": cfg.modelo_apunte(m)[0], "esfuerzo": cfg.modelo_apunte(m)[1]}
                   for m in cfg.materias if m.apunte is not None]
        return vista(request, "modelos.html", f=_facturacion(request), opciones=opciones,
                     grupos_clasificar=_grupos(opciones["clasificar"]), esfuerzos=ESFUERZOS,
                     catalogo=lista, fuera=fuera, proveedores=proveedores, propias=propias,
                     nombre_proveedor=modelos.NOMBRE_PROVEEDOR, aviso_ausente=modelos.AVISO_AUSENTE,
                     aviso_fuera=modelos.AVISO_FUERA, redescubriendo=REDESCUBRIENDO.locked())

    def _cambios_modelos(request: Request, datos: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Del formulario de Modelos a los cambios de `editar_modelos`. Cada modelo tiene que estar
        en la lista de su tarea; esfuerzo de la lista o vacío; modo de FACTURACIONES; horas > 0.
        Sólo van las secciones que vinieron en el formulario. ConfigError si algo no vale."""
        cambios: dict[str, dict[str, Any]] = {}
        with ctx_de(request).base.conexion() as conn:
            if "transcribir" in datos:
                cambios["transcribir"] = {
                    "modelo": _elegir(conn, "transcribir", datos["transcribir"], cfg.transcribir.modelo)["id"]}
            if "clasificar" in datos:
                o = _elegir(conn, "clasificar", datos["clasificar"], cfg.clasificar.modelo)
                proveedor = o.get("proveedor") or modelos.proveedor_de(o["id"])
                clasificar = {"motor": MOTOR_DE_PROVEEDOR.get(proveedor, cfg.clasificar.motor), "modelo": o["id"]}
                if "clasificar_esfuerzo" in datos:
                    clasificar["esfuerzo"] = _esfuerzo_form(datos["clasificar_esfuerzo"], "clasificar")
                cambios["clasificar"] = clasificar
            if "revisar_transcript" in datos:
                revisar: dict[str, Any] = {"modelo": _elegir(conn, "revisar_transcript", datos["revisar_transcript"],
                                                             cfg.revisar_transcript.modelo)["id"]}
                if "revisar_transcript_esfuerzo" in datos:
                    revisar["esfuerzo"] = _esfuerzo_form(datos["revisar_transcript_esfuerzo"], "revisar_transcript")
                cambios["revisar_transcript"] = revisar
            if "apunte" in datos:
                apunte: dict[str, Any] = {}
                if datos["apunte"].strip() or cfg.apunte.modelo:
                    apunte["modelo"] = _elegir(conn, "apunte", datos["apunte"], cfg.apunte.modelo)["id"]
                # else: `apunte.modelo: null` (el default del Agent SDK) y la opción vacía: queda así.
                if "apunte_esfuerzo" in datos:
                    apunte["esfuerzo"] = _esfuerzo_form(datos["apunte_esfuerzo"], "apunte")
                if "apunte_web_form" in datos:  # el casillero sin marcar no viaja: el oculto dice que estaba
                    apunte["web"] = datos.get("apunte_web") == "on"
                cambios["apunte"] = apunte
        claude: dict[str, Any] = {}
        if "facturacion" in datos:
            modo = datos["facturacion"].strip()
            if modo not in FACTURACIONES:
                raise ConfigError(f"modo de facturación inválido: {modo!r} (opciones: {', '.join(FACTURACIONES)})")
            claude["facturacion"] = modo
        if "reintentar_api_h" in datos:
            claude["reintentar_api_h"] = _horas(datos["reintentar_api_h"])
        if claude:
            cambios["claude"] = claude
        return cambios

    @router.post("/panel/modelos")
    async def modelos_guardar(request: Request) -> RedirectResponse:
        datos = await formulario(request)
        url = "/panel/modelos"
        modo_antes = cfg.claude.facturacion
        try:
            cambios = _cambios_modelos(request, datos)
            if not cambios:
                raise ConfigError("no llegó nada para guardar")
            request.app.state.config_viva.editar_modelos(cambios)
        except ConfigError as e:
            return volver(url, str(e), "error")
        msg = "Guardado: el próximo job ya usa esta configuración."
        if cfg.claude.facturacion != modo_antes:
            # Los jobs de Claude que esperaban por el modo anterior se prueban ya con el nuevo.
            adelantados = despertar_pospuestos(ctx_de(request).base)
            _despertar(request)
            if adelantados:
                msg += f" Con el modo nuevo se reintentan ya {adelantados} job(s) que esperaban."
        return volver(url, msg)

    @router.post("/panel/modelos/reintentar")
    async def modelos_reintentar(request: Request) -> RedirectResponse:
        """Botones "Reintentar API" y "Reintentar plan Max": olvida el error y adelanta los jobs."""
        datos = await formulario(request)
        url = "/panel/modelos"
        medio = datos.get("medio", "")
        if medio not in (API, MAX):
            return volver(url, f"Medio inválido: {medio!r}", "error")
        adelantados = Facturacion(cfg, ctx_de(request).base).reintentar(medio)
        _despertar(request)
        detalle = (f"{adelantados} job(s) que esperaban se reintentan ya" if adelantados
                   else "no había jobs esperando")
        return volver(url, f"Se vuelve a probar {NOMBRE_MEDIO[medio]} desde el próximo job: {detalle}.")

    @router.post("/panel/modelos/redescubrir")
    async def modelos_redescubrir(request: Request) -> RedirectResponse:
        """Consulta las APIs de modelos de Anthropic y Google (en un hilo: tarda unos segundos)."""
        url = "/panel/modelos"
        if not REDESCUBRIENDO.acquire(blocking=False):
            return volver(url, "Ya se está redescubriendo: esperá a que termine y recargá la página.", "aviso")
        try:
            resumen = await run_in_threadpool(modelos.redescubrir, cfg, ctx_de(request).base)
        except Exception as e:  # noqa: BLE001 — cada proveedor ya maneja sus errores: esto es otra cosa
            detalle = sin_claves(f"{e.__class__.__name__}: {e}")
            log.error("redescubrir modelos falló: %s", detalle)
            return volver(url, f"No se pudo redescubrir: {detalle}", "error")
        finally:
            REDESCUBRIENDO.release()
        return volver(url, "Redescubrir modelos: " + " · ".join(resumen.lineas()),
                      "ok" if resumen.ok else "aviso")

    # --- estado ---
    @router.get("/panel/estado", response_class=HTMLResponse)
    def estado(request: Request) -> HTMLResponse:
        with ctx_de(request).base.conexion() as conn:
            activos = conn.execute(
                "SELECT * FROM jobs WHERE estado IN ('pendiente', 'corriendo') "
                "ORDER BY estado = 'corriendo' DESC, run_after, prioridad, creado").fetchall()
            errores_jobs = conn.execute(
                "SELECT * FROM jobs WHERE estado = 'error' ORDER BY actualizado DESC LIMIT 20").fetchall()
            errores_sesiones = conn.execute(
                "SELECT * FROM sesiones WHERE estado = 'error' ORDER BY actualizado DESC LIMIT 20").fetchall()
            esperando = conn.execute(
                "SELECT * FROM sesiones WHERE estado = 'esperando_usuario' ORDER BY actualizado DESC").fetchall()
            con_claude = conn.execute(
                "SELECT * FROM jobs WHERE facturacion IS NOT NULL ORDER BY actualizado DESC LIMIT 15").fetchall()
            avisos_pub = []
            for a in avisos_publicacion(conn):
                s = conn.execute("SELECT titulo, inicio FROM sesiones WHERE id = ?", (a["sesion_id"],)).fetchone()
                avisos_pub.append({"a": a, "titulo": s["titulo"] if s else None, "inicio": s["inicio"] if s else None})
        return vista(request, "estado.html", activos=activos, errores_jobs=errores_jobs,
                     errores_sesiones=errores_sesiones, esperando=esperando, con_claude=con_claude,
                     avisos_pub=avisos_pub, f=_facturacion(request),
                     worker=getattr(request.app.state, "worker", None),
                     config_viva=request.app.state.config_viva, ahora=datetime.now().timestamp(),
                     log=_cola_archivo(cfg.logs_dir / LOG_SERVER, 80), log_ruta=cfg.logs_dir / LOG_SERVER)

    return router

