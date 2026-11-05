"""Cliente de Gemini (google-genai) y helpers comunes: errores → excepciones del pipeline, prompts.

Lo usan el transcriptor (ASR y LLM) y el clasificador. La clave sale de `cfg.gemini_api_key`
(Classmer-data\\.env) y nunca se imprime ni se escribe en logs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from classmer.config import PROMPTS_DIR
from classmer.pipeline import CuotaAgotada, ErrorEtapa

if TYPE_CHECKING:
    from classmer.config import Config

TIMEOUT_TRANSCRIBIR_S = 600
TIMEOUT_CLASIFICAR_S = 120
ESPERA_SIN_CLAVE_S = 600
ESPERA_CUOTA_S = 120
ESPERA_CUOTA_DIARIA_S = 3600
ESPERA_SIN_RED_S = 120
ESPERA_SOBRECARGA_S = 180
# La API acepta ~20 MB por request y los bytes inline van en base64 (×4/3): 14 MiB → ~18,7 MB,
# con margen para el prompt.
MAX_INLINE_BYTES = 14 * 1024 * 1024

MIME_AUDIO = {
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".m4a": "audio/aac",
    ".aiff": "audio/aiff",
}


# --- cliente ---------------------------------------------------------------------------------

def crear_cliente(cfg: Config, *, timeout_s: float) -> Any:
    """genai.Client con timeout. Sin clave → CuotaAgotada (el job espera sin gastar intentos).

    Sin reintentos internos del SDK (también repetiría los timeouts, que son largos): reintentan
    la etapa (principal → respaldo) y la cola de jobs.
    """
    if not cfg.gemini_api_key:
        raise CuotaAgotada("falta GEMINI_API_KEY en Classmer-data\\.env", espera_s=ESPERA_SIN_CLAVE_S)
    from google import genai
    from google.genai import types

    opciones = types.HttpOptions(timeout=int(timeout_s * 1000))
    return genai.Client(api_key=cfg.gemini_api_key, http_options=opciones)


def llamar(cliente: Any, modelo: str, contents: Any, config: Any, *, clave: str | None = None) -> Any:
    """client.models.generate_content con los errores traducidos a ErrorEtapa/CuotaAgotada."""
    try:
        return cliente.models.generate_content(model=modelo, contents=contents, config=config)
    except Exception as e:  # noqa: BLE001 - se traduce todo lo que venga del SDK o de httpx
        raise traducir_error(e, modelo, clave=clave) from e


# --- errores ---------------------------------------------------------------------------------

def traducir_error(e: BaseException, modelo: str, *, clave: str | None = None) -> ErrorEtapa:
    """Error del SDK/HTTP → excepción del pipeline (§7: la cuota no cuenta como intento).

    - 429 / RESOURCE_EXHAUSTED → CuotaAgotada (espera = retryDelay si viene; si no, ~2 min;
      cuota diaria → 1 h).
    - 503 / UNAVAILABLE / "overloaded" (modelo sobrecargado) → CuotaAgotada de 3 min: es pasajero
      y no debe gastar los intentos del job.
    - El resto de 5xx (500, 502, 504), 408, 409, 499 y timeouts → ErrorEtapa reintentable.
    - Sin conexión → CuotaAgotada (no gasta intentos mientras no haya internet).
    - 400/401/403/404 y el resto de 4xx → ErrorEtapa no reintentable.
    """
    if isinstance(e, ErrorEtapa):
        return e
    from google.genai import errors

    if isinstance(e, errors.APIError):
        codigo = int(getattr(e, "code", 0) or 0)
        estado = str(getattr(e, "status", "") or "")
        mensaje = _limpiar(str(getattr(e, "message", "") or e), clave)[:300]
        cuotas = cuotas_tocadas(getattr(e, "details", None))
        # Qué límite se tocó, antes del mensaje (que se corta y la métrica queda afuera).
        detalle = (f"Gemini {codigo} {estado} ({modelo})" + (f" [cuota: {', '.join(cuotas)}]" if cuotas else "")
                   + f": {mensaje}").strip()
        if codigo == 429 or estado == "RESOURCE_EXHAUSTED":
            return CuotaAgotada(detalle, espera_s=espera_cuota(getattr(e, "details", None)))
        if (codigo == 503 or estado == "UNAVAILABLE"
                or (not 400 <= codigo < 500 and "overload" in mensaje.lower())):
            return CuotaAgotada(detalle, espera_s=ESPERA_SOBRECARGA_S)
        if codigo >= 500 or codigo in (408, 409, 499):
            return ErrorEtapa(detalle, reintentable=True)
        return ErrorEtapa(detalle, reintentable=False)
    if isinstance(e, httpx.TimeoutException):
        return ErrorEtapa(f"Gemini ({modelo}) no respondió a tiempo", reintentable=True)
    if isinstance(e, (httpx.ConnectError, httpx.NetworkError)):
        return CuotaAgotada(f"sin conexión con Gemini ({modelo}): {e.__class__.__name__}",
                            espera_s=ESPERA_SIN_RED_S)
    texto = _limpiar(str(e) or e.__class__.__name__, clave)[:300]
    return ErrorEtapa(f"Gemini ({modelo}): {e.__class__.__name__}: {texto}", reintentable=True)


def cuotas_tocadas(detalles: Any) -> list[str]:
    """Los quotaId de las violaciones de un 429 (p. ej. GenerateContentPaidTierInputTokensPerModelPerMinute)."""
    vistos: list[str] = []

    def recorrer(obj: Any) -> None:
        if isinstance(obj, dict):
            valor = obj.get("quotaId")
            if isinstance(valor, str) and valor and valor not in vistos:
                vistos.append(valor)
            for v in obj.values():
                recorrer(v)
        elif isinstance(obj, list):
            for v in obj:
                recorrer(v)

    recorrer(detalles)
    return vistos


def espera_cuota(detalles: Any) -> float:
    """Segundos a esperar según el cuerpo de un 429 (google.rpc.RetryInfo.retryDelay).

    La cuota diaria manda aunque venga un retryDelay corto: reintentar cada ~20 s todo el día
    sólo llena el log.
    """
    texto = json.dumps(detalles, ensure_ascii=False) if detalles is not None else ""
    if "PerDay" in texto or "per day" in texto.lower():
        return float(ESPERA_CUOTA_DIARIA_S)
    demora = _buscar_clave(detalles, "retryDelay")
    if isinstance(demora, str):
        m = re.match(r"^\s*([\d.]+)\s*s?\s*$", demora)
        if m:
            return float(min(max(float(m.group(1)) + 5, 10), ESPERA_CUOTA_DIARIA_S))
    return float(ESPERA_CUOTA_S)


def _buscar_clave(obj: Any, clave: str) -> Any:
    if isinstance(obj, dict):
        if clave in obj:
            return obj[clave]
        valores = obj.values()
    elif isinstance(obj, list):
        valores = obj
    else:
        return None
    for v in valores:
        r = _buscar_clave(v, clave)
        if r is not None:
            return r
    return None


def _limpiar(texto: str, clave: str | None) -> str:
    """Por las dudas: nunca dejar la clave en un mensaje que va a la base o al log."""
    if clave:
        texto = texto.replace(clave, "***")
    return texto


# --- respuestas ------------------------------------------------------------------------------

def uso(respuesta: Any) -> dict[str, int]:
    """Tokens de usage_metadata (para el log del job)."""
    meta = getattr(respuesta, "usage_metadata", None)
    if meta is None:
        return {}
    campos = {
        "entrada": "prompt_token_count",
        "salida": "candidates_token_count",
        "razonamiento": "thoughts_token_count",
        "total": "total_token_count",
    }
    salida: dict[str, int] = {}
    for nombre, attr in campos.items():
        v = getattr(meta, attr, None)
        if isinstance(v, int):
            salida[nombre] = v
    return salida


def texto_respuesta(respuesta: Any) -> str:
    """Texto de las parts de texto del primer candidato (sin pasar por .text, que avisa si hay otras)."""
    partes = []
    for p in partes_respuesta(respuesta):
        t = getattr(p, "text", None)
        if t and not getattr(p, "thought", False):
            partes.append(t)
    return "".join(partes)


def partes_respuesta(respuesta: Any) -> list[Any]:
    candidatos = getattr(respuesta, "candidates", None) or []
    if not candidatos:
        return []
    contenido = getattr(candidatos[0], "content", None)
    return list(getattr(contenido, "parts", None) or [])


def motivo_fin(respuesta: Any) -> str | None:
    """finish_reason del primer candidato (o el bloqueo del prompt), como texto."""
    feedback = getattr(respuesta, "prompt_feedback", None)
    bloqueo = getattr(feedback, "block_reason", None) if feedback is not None else None
    if bloqueo:
        return f"BLOQUEADO:{getattr(bloqueo, 'value', bloqueo)}"
    candidatos = getattr(respuesta, "candidates", None) or []
    if not candidatos:
        return None
    razon = getattr(candidatos[0], "finish_reason", None)
    if razon is None:
        return None
    return str(getattr(razon, "value", razon))


def _sin_cercas(texto: str) -> str:
    limpio = texto.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", limpio, re.DOTALL)
    return m.group(1).strip() if m else limpio


def leer_json_objeto(texto: str) -> dict[str, Any] | None:
    """Objeto JSON de la respuesta (tolera ```json … ``` y texto alrededor), o None."""
    limpio = _sin_cercas(texto)
    try:
        datos = json.loads(limpio)
    except json.JSONDecodeError:
        i = limpio.find("{")
        if i < 0:
            return None
        try:
            datos, _ = json.JSONDecoder().raw_decode(limpio, i)
        except json.JSONDecodeError:
            return None
    if isinstance(datos, list) and datos and isinstance(datos[0], dict):
        datos = datos[0]
    return datos if isinstance(datos, dict) else None


def leer_json_lista(texto: str) -> tuple[list[Any], bool]:
    """Lista JSON de la respuesta. Si vino cortada, rescata los objetos completos.

    Devuelve (items, completa). Tolera ```json … ``` alrededor.
    """
    limpio = _sin_cercas(texto)
    try:
        datos = json.loads(limpio)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(datos, list):
            return datos, True
        if isinstance(datos, dict):
            for v in datos.values():
                if isinstance(v, list):
                    return v, True
            return [datos], True
        return [], True
    # Rescate: objetos completos dentro de una lista cortada.
    decodificador = json.JSONDecoder()
    i = limpio.find("[")
    items: list[Any] = []
    if i < 0:
        return items, False
    i += 1
    while i < len(limpio):
        while i < len(limpio) and limpio[i] in " \t\r\n,":
            i += 1
        if i >= len(limpio) or limpio[i] != "{":
            break
        try:
            obj, fin = decodificador.raw_decode(limpio, i)
        except json.JSONDecodeError:
            break
        items.append(obj)
        i = fin
    return items, False


def mime_audio(ruta: Path) -> str:
    return MIME_AUDIO.get(ruta.suffix.lower(), "audio/mp3")


# --- prompts ---------------------------------------------------------------------------------

_COMENTARIO = re.compile(r"<!--.*?-->\s*", re.DOTALL)


def cargar_prompt(nombre: str, *, carpeta: Path | None = None, **variables: Any) -> str:
    """Lee prompts/<nombre>.md, saca los comentarios <!-- --> y reemplaza {variable}.

    Sólo se reemplazan las variables pasadas: otras llaves del texto (ejemplos JSON, LaTeX)
    quedan como están, y los valores no se vuelven a interpretar.
    """
    ruta = (carpeta or PROMPTS_DIR) / f"{nombre}.md"
    plantilla = _COMENTARIO.sub("", ruta.read_text(encoding="utf-8"))
    return rellenar(plantilla, **variables)


def rellenar(plantilla: str, **variables: Any) -> str:
    def reemplazo(m: re.Match[str]) -> str:
        clave = m.group(1)
        if clave in variables:
            v = variables[clave]
            return "" if v is None else str(v)
        return m.group(0)

    return re.sub(r"\{([a-z_][a-z0-9_]*)\}", reemplazo, plantilla)


# --- perfil de la materia --------------------------------------------------------------------

CLAVES_TRANSCRIBIR = (
    "notaci", "glosario", "vocabulario", "termino", "término", "sigla", "abreviatura",
    "programa", "temario", "contenido", "unidad", "parte", "objetivo", "catedra", "cátedra",
    "profesor", "docente",
)
CLAVES_CLASIFICAR = (
    "programa", "temario", "contenido", "unidad", "parte", "objetivo", "catedra", "cátedra",
    "rol", "materia",
)
_TITULO = re.compile(r"^(#{1,6})\s+(.*)$")


def leer_perfil(ruta: Path | None) -> str | None:
    if ruta is None:
        return None
    try:
        texto = ruta.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return texto.strip() or None


def extracto_perfil(texto: str | None, limite: int = 8000,
                    claves: tuple[str, ...] = CLAVES_TRANSCRIBIR) -> str | None:
    """Recorta el perfil a ~`limite` caracteres priorizando las secciones útiles.

    Siempre va el comienzo (título y presentación); después, las secciones cuyo título menciona
    alguna de `claves` (notación, glosario, programa…), en el orden original; si sobra lugar,
    el resto en orden.
    """
    if not texto:
        return None
    texto = texto.strip()
    if len(texto) <= limite:
        return texto
    secciones = _secciones(texto)
    if len(secciones) <= 1:
        return _cortar(texto, limite)
    elegidas: list[int] = [0]
    usado = min(len(secciones[0][1]), limite // 4)
    prioritarias = [i for i, (titulo, _) in enumerate(secciones)
                    if i > 0 and any(c in titulo.lower() for c in claves)]
    resto = [i for i in range(1, len(secciones)) if i not in prioritarias]
    for i in prioritarias + resto:
        largo = len(secciones[i][1])
        if usado + largo <= limite:
            elegidas.append(i)
            usado += largo
    partes = []
    for i in sorted(set(elegidas)):
        cuerpo = secciones[i][1]
        if i == 0:
            cuerpo = _cortar(cuerpo, limite // 4)
        partes.append(cuerpo.strip())
    salida = "\n\n".join(p for p in partes if p)
    return _cortar(salida, limite)


def _secciones(texto: str) -> list[tuple[str, str]]:
    """[(titulo, texto_con_titulo)] cortando en cada título markdown (fuera de bloques de código)."""
    secciones: list[tuple[str, list[str]]] = [("", [])]
    en_codigo = False
    for linea in texto.splitlines():
        if linea.strip().startswith("```"):
            en_codigo = not en_codigo
        m = None if en_codigo else _TITULO.match(linea)
        if m:
            secciones.append((m.group(2).strip(), [linea]))
        else:
            secciones[-1][1].append(linea)
    return [(t, "\n".join(ls)) for t, ls in secciones if "\n".join(ls).strip()]


def _cortar(texto: str, limite: int) -> str:
    if len(texto) <= limite:
        return texto
    corte = texto.rfind("\n", 0, limite)
    if corte < limite // 2:
        corte = limite
    return texto[:corte].rstrip() + "\n[…]"


def extraer_glosario(perfil: str | None, maximo: int = 150) -> list[str]:
    """Términos de las secciones de glosario/vocabulario/términos/siglas del perfil.

    Toma el término de cada viñeta («- **término**: …», «- término — …»). Sirve para el prompt
    del LLM y, en v0.2, como initial_prompt de Whisper.
    """
    if not perfil:
        return []
    terminos: list[str] = []
    vistos: set[str] = set()
    for titulo, cuerpo in _secciones(perfil):
        if not re.search(r"glosario|vocabulario|t[eé]rminos|siglas|abreviaturas", titulo, re.I):
            continue
        for linea in cuerpo.splitlines()[1:]:
            m = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", linea)
            if not m:
                continue
            item = m.group(1)
            negrita = re.match(r"^\*\*(.+?)\*\*", item)
            termino = negrita.group(1) if negrita else re.split(r"\s+[—–-]\s+|:\s", item, maxsplit=1)[0]
            termino = termino.strip(" *`_.:;")
            if 1 < len(termino) <= 60 and termino.lower() not in vistos:
                vistos.add(termino.lower())
                terminos.append(termino)
                if len(terminos) >= maximo:
                    return terminos
    return terminos
