"""Agente de apuntes con el Claude Agent SDK (§7, §8): escribe apunte.tex + meta.json y compila.

Facturación por corrida (`TrabajoApunte.facturacion`, la decide `ia.facturacion`):
- `max`: el login de Claude Code del usuario (plan Max). El CLI no recibe ninguna API key: si hay
  ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN en el entorno del proceso, se sacan mientras arranca.
- `api`: la ANTHROPIC_API_KEY de Classmer-data\\.env va SÓLO en el `env` de las opciones de esa
  corrida (nunca en os.environ); lo del entorno del proceso se saca igual.
Los errores se separan en pasajeros (429, 5xx, sin red: CuotaAgotada, no cambia de medio) y de
facturación (sin créditos, clave inválida, cupo del plan agotado, sin login: SinCreditos, la etapa
rehace todo con el otro medio si el modo lo permite).

No carga la config del usuario (setting_sources=[]): ni CLAUDE.md, ni MCP, ni hooks, ni plugins.

Política de herramientas (hook PreToolUse que decide TODO, ver `decidir`):
- Read/Glob/Grep: sólo dentro de la carpeta de trabajo o de la carpeta de la materia.
- Write/Edit: sólo dentro de <trabajo>/output (y nunca classmer.cls).
- Bash: sólo compilar `apunte.tex` con pdflatex/lualatex en output/, con tope de intentos y tiempo,
  y sólo si lo que hay en output/ no lee archivos de afuera (\\openin, \\input{C:/…}, …: ver
  latex.lecturas_externas; el rechazo no gasta intento).
- Cualquier otra cosa: denegada.

El SDK necesita un loop asyncio propio: en Windows, Proactor (subprocess).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, NoReturn

from classmer import latex
from classmer.ia.facturacion import API, ETIQUETA_MEDIO, MAX, SinCreditos, texto_costo
from classmer.pipeline import CuotaAgotada

if TYPE_CHECKING:
    from classmer.config import Config

HERRAMIENTAS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
# Todo lo demás que trae Claude Code: se saca del contexto del modelo (y el hook lo deniega igual).
PROHIBIDAS = [
    "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "NotebookRead", "TodoWrite", "MultiEdit",
    "BashOutput", "KillShell", "KillBash", "Skill", "SlashCommand", "ExitPlanMode", "EnterPlanMode",
    "AskUserQuestion", "ListMcpResourcesTool", "ReadMcpResourceTool", "Monitor", "PowerShell",
    "CronCreate", "CronDelete", "CronList", "RemoteTrigger", "SendMessage", "ToolSearch",
    "EnterWorktree", "ExitWorktree", "TaskOutput", "TaskStop", "LSP",
]
MOTORES_BASH = ("pdflatex", "lualatex")
FLAGS_BASH = ("-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-synctex=0", "-no-shell-escape")
ARCHIVO_TEX = "apunte.tex"
PLANTILLA = "classmer.cls"
CARPETA_SALIDA = "output"
MAX_TURNOS = 80
MAX_SEGUIMIENTOS = 2
ESPERA_CUOTA_S = 1800       # sin dato de cuándo se libera: 30 min
ESPERA_CUOTA_MAX_S = 6 * 3600
ESPERA_SOBRECARGA_S = 300   # API sobrecargada (529): reintentar en 5 min
ESPERA_LOGIN_S = 1800
LARGO_PROMPT_ARGV = 24_000  # Windows corta la línea de comando en ~32k caracteres
NOMBRE_LOG = "agente.log"
MAX_BUFFER_SDK = 1024 ** 3  # tope por mensaje del CLI (una foto leída va en base64 adentro)

# Variables del entorno del proceso que no se le pasan al CLI: claves de API (con el plan Max
# facturaría por API; con la API, la clave es la del .env y va en las opciones de la corrida), la URL
# de la API (una sesión de Claude Code exporta ANTHROPIC_BASE_URL a su proxy local: la clave del .env
# iría ahí y no a api.anthropic.com) y las que deja una sesión de Claude Code que lanzó el server (el
# agente no es una sesión hija).
VARIABLES_FUERA = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES",
    "CLAUDE_CODE_REPORT_FINDINGS", "CLAUDE_CODE_TERMINAL_MCP_TOOLS", "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "CLAUDE_CODE_EAGER_FLUSH", "CLAUDE_CODE_DISABLE_CRON",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "CLAUDE_CODE_DESKTOP_APP_VERSION", "CLAUDE_PREVIEW_CLASSIFIER_FLOOR",
    "CLAUDE_AGENT_SDK_MCP_NO_PREFIX", "CLAUDE_EFFORT", "CLAUDE_CODE_OAUTH_SCOPES", "CLAUDE_CODE_ACCOUNT_UUID",
    "CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_ORGANIZATION_UUID",
)
ENTORNO_AGENTE = {
    "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",  # cada Bash arranca en la carpeta de trabajo
    "BASH_DEFAULT_TIMEOUT_MS": "600000",              # MiKTeX puede tardar bajando paquetes
    "BASH_MAX_TIMEOUT_MS": "900000",
    "DISABLE_AUTOUPDATER": "1",
}
_entorno_lock = threading.Lock()


# --- contrato con la etapa -----------------------------------------------------------------------

@dataclass
class TrabajoApunte:
    workdir: Path                       # carpeta de trabajo (ASCII, sin espacios): input/ plantilla/ output/
    carpeta_materia: Path | None        # se monta como directorio extra de sólo lectura
    prompt_sistema: str
    prompt: str                         # primer mensaje (contexto de la clase)
    modelo: str | None
    motor_latex: str
    max_intentos_compilacion: int
    tope_correccion_s: float            # desde el primer intento de compilación
    timeout_s: float                    # tope total del agente
    validar_meta: Callable[[], list[str]]  # errores de output/ (meta.json y apunte.tex); [] si está bien
    duracion_ms: int | None
    esfuerzo: str | None = None         # low … max (`--effort` del CLI); None: el default del modelo
    facturacion: str = MAX              # api | max: con qué se paga esta corrida


@dataclass
class ResultadoAgente:
    ok: bool
    modelo: str | None
    costo_usd: float | None
    intentos_compilacion: int
    turnos: int
    log: str
    error: str | None


# --- política de herramientas --------------------------------------------------------------------

@dataclass
class EstadoPolitica:
    """Lo que la política necesita saber (y lo que cuenta: intentos de compilación)."""

    workdir: Path
    carpeta_materia: Path | None = None
    max_intentos: int = 5
    tope_s: float = 600
    reloj: Callable[[], float] = time.monotonic
    intentos: int = 0
    primer_intento: float | None = None
    # Donde Claude Code guarda las salidas largas de las herramientas de esta sesión
    # (~/.claude/projects/<proyecto>/<sesión>/tool-results/): el agente las tiene que poder leer.
    resultados: Path | None = None

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir).resolve()
        if self.carpeta_materia is not None:
            self.carpeta_materia = Path(self.carpeta_materia).resolve()

    @property
    def salida(self) -> Path:
        return self.workdir / CARPETA_SALIDA

    @property
    def raices_lectura(self) -> list[Path]:
        raices = [self.workdir]
        if self.carpeta_materia:
            raices.append(self.carpeta_materia)
        if self.resultados:
            raices.append(self.resultados)
        return raices

    def sesion_cli(self, transcript_path: Any) -> None:
        """Registra la carpeta tool-results de la sesión del CLI (viene en cada evento de hook)."""
        if self.resultados is None and isinstance(transcript_path, str) and transcript_path.endswith(".jsonl"):
            ruta = resolver_ruta(transcript_path, self.workdir)
            if ruta is not None:
                self.resultados = ruta.with_suffix("") / "tool-results"

    @property
    def restantes(self) -> int:
        return max(0, self.max_intentos - self.intentos)

    @property
    def puede_compilar(self) -> bool:
        """¿Quedan intentos y no venció el tope de tiempo desde la primera compilación?"""
        return self.restantes > 0 and (self.primer_intento is None
                                       or self.reloj() - self.primer_intento <= self.tope_s)

    def pdf_atrasado(self) -> bool:
        """¿Hay apunte.tex sin un apunte.pdf más nuevo (la última compilación falló o no la hubo)?"""
        tex = self.salida / ARCHIVO_TEX
        pdf = tex.with_suffix(".pdf")
        try:
            return tex.is_file() and (not pdf.is_file() or pdf.stat().st_mtime < tex.stat().st_mtime)
        except OSError:
            return False


COMANDO_EJEMPLO = (f"cd {CARPETA_SALIDA} && pdflatex -interaction=nonstopmode -halt-on-error -file-line-error "
                   f"-no-shell-escape {ARCHIVO_TEX}")
_AYUDA_BASH = (f"Bash sólo sirve para compilar: `{COMANDO_EJEMPLO}` (podés encadenar las dos pasadas con && "
               "o usar lualatex). Nada de pipes, redirecciones, otros programas ni otras carpetas.")
_CHARS_BASH = set("|<>`$\r\n(){}*?~!#^%\x00")


def decidir(tool_name: str, tool_input: dict[str, Any], estado: EstadoPolitica) -> tuple[bool, str]:
    """¿Se permite esta herramienta con esta entrada? Devuelve (permitir, motivo). Sin efectos salvo
    el conteo de intentos de compilación (cada Bash permitido suma uno); para Bash lee output/."""
    datos = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in ("Read", "Glob", "Grep"):
        return _decidir_lectura(tool_name, datos, estado)
    if tool_name in ("Write", "Edit"):
        return _decidir_escritura(tool_name, datos, estado)
    if tool_name == "Bash":
        return _decidir_bash(datos, estado)
    return False, (f"La herramienta {tool_name or '(sin nombre)'} no está habilitada: sólo Read, Glob, Grep, "
                   f"Write/Edit en {CARPETA_SALIDA}/ y Bash para compilar.")


def _decidir_lectura(nombre: str, datos: dict[str, Any], estado: EstadoPolitica) -> tuple[bool, str]:
    fuera = (f"{nombre}: sólo se puede leer dentro de la carpeta de trabajo ({estado.workdir})"
             + (f" o de la carpeta de la materia ({estado.carpeta_materia})" if estado.carpeta_materia else ""))
    if nombre == "Read":
        ruta = resolver_ruta(datos.get("file_path"), estado.workdir)
        if ruta is None:
            return False, f"Read: ruta inválida ({datos.get('file_path')!r})"
        return (True, "lectura permitida") if _en_alguna(ruta, estado.raices_lectura) else (False, fuera)

    base_txt = datos.get("path")
    base = resolver_ruta(base_txt, estado.workdir) if base_txt not in (None, "") else estado.workdir
    if base is None:
        return False, f"{nombre}: ruta inválida ({base_txt!r})"
    if not _en_alguna(base, estado.raices_lectura):
        return False, fuera
    patron = datos.get("pattern") if nombre == "Glob" else datos.get("glob")
    if patron not in (None, ""):
        ok, motivo = _patron_seguro(str(patron), base, estado)
        if not ok:
            return False, f"{nombre}: {motivo}"
    return True, "búsqueda permitida"


def _patron_seguro(patron: str, base: Path, estado: EstadoPolitica) -> tuple[bool, str]:
    """Un patrón glob no puede salir de la carpeta: sin '..', sin '~' y, si es absoluto, adentro."""
    partes = re.split(r"[\\/]", patron)
    if ".." in partes or patron.startswith("~"):
        return False, "el patrón no puede salir de la carpeta ('..' o '~')"
    if re.match(r"^(?:[A-Za-z]:|[\\/])", patron):
        fijo = []
        for p in partes:
            if re.search(r"[*?\[\]{}]", p):
                break
            fijo.append(p)
        literal = "/".join(fijo) or "/"
        raiz = resolver_ruta(literal + "/" if re.fullmatch(r"[A-Za-z]:", literal) else literal, base)
        if raiz is None or not _en_alguna(raiz, estado.raices_lectura):
            return False, "el patrón apunta fuera de las carpetas permitidas"
    return True, ""


def _decidir_escritura(nombre: str, datos: dict[str, Any], estado: EstadoPolitica) -> tuple[bool, str]:
    ruta = resolver_ruta(datos.get("file_path"), estado.workdir)
    if ruta is None:
        return False, f"{nombre}: ruta inválida ({datos.get('file_path')!r})"
    if not _dentro(ruta, estado.salida) or _clave(ruta) == _clave(estado.salida):
        return False, f"{nombre}: sólo se puede escribir dentro de {estado.salida} (apunte.tex y meta.json)"
    if _clave(ruta) == _clave(estado.salida / PLANTILLA):
        return False, f"{nombre}: {PLANTILLA} es la plantilla fija del look: no se toca"
    return True, "escritura permitida"


def _decidir_bash(datos: dict[str, Any], estado: EstadoPolitica) -> tuple[bool, str]:
    comando = datos.get("command")
    if not isinstance(comando, str) or not comando.strip():
        return False, _AYUDA_BASH
    if datos.get("run_in_background"):
        return False, "Bash: no se permite correr en segundo plano. " + _AYUDA_BASH
    ok, motivo = validar_comando(comando, estado)
    if not ok:
        return False, f"{motivo}. {_AYUDA_BASH}"
    ahora = estado.reloj()
    if estado.intentos >= estado.max_intentos:
        return False, (f"tope de {estado.max_intentos} intentos de compilación alcanzado: dejá apunte.tex y "
                       "meta.json como están y terminá")
    if estado.primer_intento is not None and ahora - estado.primer_intento > estado.tope_s:
        return False, (f"pasaron más de {estado.tope_s / 60:g} min desde la primera compilación: dejá apunte.tex "
                       "y meta.json como están y terminá")
    lecturas = latex.lecturas_externas_en(estado.salida, ARCHIVO_TEX)
    if lecturas:
        return False, mensaje_lecturas(lecturas)
    estado.intentos += 1
    if estado.primer_intento is None:
        estado.primer_intento = ahora
    return True, f"compilación {estado.intentos} de {estado.max_intentos}"


def mensaje_lecturas(lecturas: list[str]) -> str:
    return (f"{ARCHIVO_TEX} no puede leer archivos de fuera de {CARPETA_SALIDA}/ y encontré: "
            + ", ".join(lecturas[:6]) + (" …" if len(lecturas) > 6 else "")
            + ". Sacá \\openin, \\read, \\directlua y similares, y en \\input, \\includegraphics y \\pizarra "
            "usá sólo nombres relativos, sin rutas absolutas ni '..' (las fotos: \\pizarra{pizarra_NN.jpg}; "
            "los adjuntos: adjuntos/<archivo>, nunca ../input/adjuntos/). Esto vale también en comentarios: "
            "un % no siempre es comentario (\\url{…%20…}). "
            "Esta compilación no se corrió y no cuenta como intento.")


def validar_comando(comando: str, estado: EstadoPolitica) -> tuple[bool, str]:
    """¿Es `[cd output &&] pdflatex [flags] apunte.tex [(&& | ;) pdflatex [flags] apunte.tex]`?"""
    texto = comando.strip()
    raros = sorted(set(texto) & _CHARS_BASH)
    if raros:
        return False, f"caracteres no permitidos en el comando: {' '.join(repr(c) for c in raros)}"
    marcado = texto.replace("&&", "\x01")
    if "&" in marcado:
        return False, "no se permite '&' (segundo plano)"
    segmentos = re.split(r"\s*[\x01;]\s*", marcado)
    if any(not s.strip() for s in segmentos):
        return False, "comando vacío entre separadores"
    cwd = estado.workdir
    if segmentos[0].split()[0] == "cd":
        try:
            partes = shlex.split(segmentos[0], posix=True)
        except ValueError:
            return False, "comillas sin cerrar"
        if len(partes) != 2:
            return False, "cd lleva exactamente una carpeta"
        destino = resolver_ruta(partes[1], cwd)
        if destino is None or _clave(destino) != _clave(estado.salida):
            return False, f"sólo se puede hacer cd a {CARPETA_SALIDA} ({estado.salida})"
        cwd = destino
        segmentos = segmentos[1:]
    if _clave(cwd) != _clave(estado.salida):
        return False, f"falta `cd {CARPETA_SALIDA} &&` al principio (cada comando arranca en {estado.workdir})"
    if not 1 <= len(segmentos) <= 2:
        return False, "se permiten una o dos pasadas de compilación"
    for seg in segmentos:
        if any(c in seg for c in "'\"\\"):
            return False, "la compilación no lleva comillas ni barras invertidas"
        tokens = seg.split()
        if tokens[0] not in MOTORES_BASH:
            return False, f"programa no permitido: {tokens[0]!r} (sólo {' o '.join(MOTORES_BASH)})"
        if len(tokens) < 2 or tokens[-1] != ARCHIVO_TEX:
            return False, f"lo único que se compila es {ARCHIVO_TEX} (tiene que ser el último argumento)"
        flags = tokens[1:-1]
        malos = [f for f in flags if f not in FLAGS_BASH]
        if malos:
            return False, f"opciones no permitidas: {' '.join(malos)} (sirven: {' '.join(FLAGS_BASH)})"
        if len(set(flags)) != len(flags):
            return False, "opciones repetidas"
    return True, ""


def resolver_ruta(ruta: Any, base: Path) -> Path | None:
    """Ruta (absoluta, relativa a `base`, con / o \\, estilo Git Bash /c/...) → Path absoluta resuelta.
    None si es inválida o rara (UNC, \\\\?\\, flujos alternativos 'archivo:flujo', bytes nulos)."""
    if not isinstance(ruta, str) or not ruta.strip() or "\x00" in ruta:
        return None
    texto = ruta.strip()
    if texto.startswith(("\\\\", "//")) or "%" in texto or "$" in texto:
        return None  # UNC / \\?\ y variables (%USERPROFILE%, $HOME): no se adivina qué haría la herramienta
    if texto.startswith("~"):
        texto = os.path.expanduser(texto)  # como la herramienta: ~ es la carpeta del usuario (afuera)
    m = re.match(r"^/([A-Za-z])(?:/(.*))?$", texto)
    if m and os.name == "nt":
        texto = f"{m.group(1)}:/{m.group(2) or ''}"
    if re.match(r"^[A-Za-z]:(?![\\/])", texto):
        return None  # 'C:' o 'C:foo': relativa a la carpeta actual de esa unidad
    sin_unidad = texto[2:] if re.match(r"^[A-Za-z]:", texto) else texto
    if ":" in sin_unidad:
        return None
    p = Path(texto)
    if not p.is_absolute():
        p = Path(base) / p
    try:
        return p.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _clave(p: Path) -> tuple[str, ...]:
    """Partes normalizadas para comparar rutas sin importar mayúsculas ni el tipo de barra."""
    partes = PureWindowsPath(str(p)).parts if os.name == "nt" else Path(p).parts
    return tuple(x.replace("/", "\\").rstrip("\\").casefold() or "\\" for x in partes)


def _dentro(ruta: Path, raiz: Path) -> bool:
    k, r = _clave(ruta), _clave(raiz)
    return len(k) >= len(r) and k[:len(r)] == r


def _en_alguna(ruta: Path, raices: list[Path]) -> bool:
    return any(_dentro(ruta, r) for r in raices)


# --- registro (agente.log) -----------------------------------------------------------------------

class Registro:
    """Log legible de lo que hizo el agente: herramientas, compilaciones, costo. Se escribe a medida."""

    def __init__(self, ruta: Path | None = None, salida: Path | None = None):
        self.ruta = ruta
        self.salida = salida  # output/: el resultado real de cada compilación se lee de apunte.log
        self.t0 = time.monotonic()
        self.lineas: list[str] = []
        self.usos: Counter[str] = Counter()
        self.denegadas: list[str] = []
        self.compilaciones: list[dict[str, Any]] = []
        self._bash: dict[str, dict[str, Any]] = {}
        self.stderr: deque[str] = deque(maxlen=40)
        self.secretos: list[str] = []  # valores que nunca se escriben (la API key de la corrida)

    def ocultar(self, texto: str) -> str:
        texto = _sin_secretos(texto)
        for s in self.secretos:
            if s:
                texto = texto.replace(s, "[oculto]")
        return texto

    def __call__(self, texto: str) -> None:
        seg = int(time.monotonic() - self.t0)
        linea = f"[{seg // 60:02d}:{seg % 60:02d}] {self.ocultar(texto)}"
        self.lineas.append(linea)
        if self.ruta is not None:
            try:
                with open(self.ruta, "a", encoding="utf-8") as f:
                    f.write(linea + "\n")
            except OSError:
                pass

    def herramienta(self, nombre: str, datos: dict[str, Any], permitir: bool, motivo: str,
                    tool_use_id: str | None) -> None:
        self.usos[nombre] += 1
        resumen = resumen_entrada(nombre, datos)
        if permitir:
            self(f"{nombre} {resumen}" + (f" ({motivo})" if nombre == "Bash" else ""))
            if nombre == "Bash" and tool_use_id:
                comp = {"n": len(self.compilaciones) + 1, "comando": resumen, "resultado": "sin resultado",
                        "desde": time.time()}
                self.compilaciones.append(comp)
                self._bash[tool_use_id] = comp
        else:
            self.denegadas.append(f"{nombre} {resumen}: {motivo}")
            self(f"DENEGADO {nombre} {resumen}: {motivo}")

    def resultado_herramienta(self, tool_use_id: str, contenido: Any, es_error: bool | None) -> None:
        comp = self._bash.pop(tool_use_id, None)
        texto = _texto_de(contenido)
        if comp is None:
            if es_error:
                self(f"  → error de la herramienta: {' '.join(texto.split())[:300]}")
            return
        # La salida de la herramienta puede venir recortada (Claude Code guarda las largas en un
        # archivo aparte): la verdad está en output/apunte.log y apunte.pdf.
        comp["resultado"] = resultado_en_disco(self.salida, comp["desde"]) or resumen_compilacion(texto, es_error)
        self(f"compilación {comp['n']}: {comp['resultado']}")

    def resumen(self, *, modelo: str | None, turnos: int, costo: float | None, seguimientos: int,
                errores_salida: list[str], error: str | None, estado: EstadoPolitica,
                esfuerzo: str | None = None, facturacion: str = MAX, avisos: list[str] | None = None) -> str:
        dur = int(time.monotonic() - self.t0)
        costo_txt = texto_costo(costo, facturacion) if costo is not None else "—"
        lineas = [
            f"Agente: {modelo or '?'} · esfuerzo {esfuerzo or 'por defecto'} · facturación "
            f"{ETIQUETA_MEDIO.get(facturacion, facturacion)} · {turnos} turnos · {dur // 60}m{dur % 60:02d}s · "
            f"costo {costo_txt}",
            "Herramientas: " + (", ".join(f"{k} ×{v}" for k, v in sorted(self.usos.items())) or "ninguna")
            + (f" (denegadas: {len(self.denegadas)})" if self.denegadas else ""),
            f"Compilaciones del agente: {estado.intentos} de {estado.max_intentos}"
            + ("".join(f"\n  {c['n']}. {c['resultado']}" for c in self.compilaciones)),
        ]
        if avisos:
            lineas.append("Avisos:" + "".join(f"\n  - {a}" for a in avisos))
        if self.denegadas:
            lineas.append("Denegadas:" + "".join(f"\n  - {d[:300]}" for d in self.denegadas[:15]))
        lineas.append(f"Seguimientos por la salida: {seguimientos}")
        lineas.append("Salida: ok" if not errores_salida else
                      "Salida con problemas:" + "".join(f"\n  - {e}" for e in errores_salida[:10]))
        if error:
            lineas.append(f"Error: {error}")
            if self.stderr:
                lineas.append("stderr del CLI (últimas líneas):" + "".join(f"\n  {s}" for s in list(self.stderr)[-15:]))
        return self.ocultar("\n".join(lineas))


def resumen_entrada(nombre: str, datos: dict[str, Any]) -> str:
    if nombre == "Bash":
        return f"`{str(datos.get('command', ''))[:200]}`"
    if nombre in ("Read", "Write", "Edit"):
        extra = ""
        if nombre == "Write" and isinstance(datos.get("content"), str):
            extra = f" ({len(datos['content'])} caracteres)"
        return f"{datos.get('file_path', '?')}{extra}"
    if nombre in ("Glob", "Grep"):
        return f"{datos.get('pattern', '')!r} en {datos.get('path') or '.'}"
    return json.dumps(datos, ensure_ascii=False)[:200]


def resumen_compilacion(texto: str, es_error: bool | None) -> str:
    """'ok, 7 páginas' o 'falló: apunte.tex:120: Undefined control sequence.'"""
    paginas = re.findall(r"Output written on .*?\((\d+) pages?", texto)
    error = re.search(r"^[^\n]*?\.tex:\d+: [^\n]+", texto, re.M) or re.search(r"^! [^\n]+", texto, re.M)
    if paginas and not error and not es_error:
        return f"ok, {paginas[-1]} páginas"
    if error:
        return f"falló: {error.group(0).strip()[:200]}"
    if es_error:
        ultima = [l for l in texto.strip().splitlines() if l.strip()][-1:] or ["sin salida"]
        return f"falló: {ultima[0][:200]}"
    return "terminó sin PDF" if "Output written" not in texto else "ok"


def resultado_en_disco(salida: Path | None, desde: float) -> str | None:
    """Resumen de la compilación leyendo output/apunte.log (si se escribió después de `desde`)."""
    if salida is None:
        return None
    log, pdf = salida / "apunte.log", salida / "apunte.pdf"
    try:
        if log.stat().st_mtime < desde - 1:
            return None
        texto = log.read_text(encoding="utf-8", errors="replace")
        pdf_nuevo = pdf.is_file() and pdf.stat().st_mtime >= desde - 1
    except OSError:
        return None
    return resumen_compilacion(texto, not pdf_nuevo)


def _texto_de(contenido: Any) -> str:
    if isinstance(contenido, str):
        return contenido
    if isinstance(contenido, list):
        return "\n".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in contenido)
    return "" if contenido is None else str(contenido)


def _sin_secretos(texto: str) -> str:
    return re.sub(r"(?i)\b(sk-ant-[\w-]+|(?:api[_-]?key|token|authorization)\s*[=:]\s*\S+)", "[oculto]", texto)


# --- cuota y rate limit --------------------------------------------------------------------------

_CUOTA_RE = re.compile(r"usage limit|rate[ _-]?limit|limit reached|hit your limit|too many requests|\b429\b|"
                       r"quota|out of (?:extra )?usage|weekly limit", re.I)
# Sobrecarga o caída de la API / de la red (el CLI ya reintentó adentro): se espera sin gastar intento.
_SOBRECARGA_RE = re.compile(r"overloaded|\b529\b|\bAPI Error:\s*5\d\d\b|connection error|ECONNRESET|ETIMEDOUT|"
                            r"request timed out", re.I)
_LOGIN_RE = re.compile(r"not logged in|please run /login|invalid api key|authentication|oauth token", re.I)


def espera_cuota(texto: str | None, resets_at: float | None, ahora: float | None = None) -> float:
    """Segundos a esperar: lo que diga el CLI (resets_at o 'limit reached|<epoch>') o 30 min."""
    ahora = time.time() if ahora is None else ahora
    candidato = resets_at
    if candidato is None and texto:
        m = re.search(r"\|(\d{10})\b", texto)
        if m:
            candidato = float(m.group(1))
    if candidato is not None and candidato > ahora:
        return float(min(ESPERA_CUOTA_MAX_S, max(60.0, candidato - ahora + 60)))
    return float(ESPERA_CUOTA_S)


def clasificar_falla(texto: str) -> tuple[str, float] | None:
    """('cuota'|'sobrecarga'|'login', espera_s) si el texto es de un error que no cuenta como intento.
    No distingue el medio de facturación: para eso, `clasificar_error`."""
    if _CUOTA_RE.search(texto):
        return "cuota", ESPERA_CUOTA_S
    if _SOBRECARGA_RE.search(texto):
        return "sobrecarga", ESPERA_SOBRECARGA_S
    if _LOGIN_RE.search(texto):
        return "login", ESPERA_LOGIN_S
    return None


# Según el medio de la corrida (§7). Cambian de medio (SinCreditos): el cupo del plan Max agotado o
# sin login (max), sin créditos o la clave rechazada (api). Lo demás es pasajero (CuotaAgotada).
# "You've hit your session limit", "Opus limit reached", "Claude AI usage limit reached|<epoch>"…
_CUPO_MAX_RE = re.compile(r"usage limit|limit reached|hit your (?:\w+ )?limit|out of (?:extra )?usage|"
                          r"(?:weekly|session|opus|sonnet|fable) limit|quota", re.I)
_SIN_LOGIN_RE = re.compile(r"not logged in|please run /login|oauth token|invalid api key|authentication|"
                           r"\bAPI Error:\s*401\b|unauthorized", re.I)
# La API sin créditos responde "Your credit balance is too low…" (400) o billing_error (402); el tope de
# gasto del workspace ("reached your specified API usage limits") tampoco se arregla esperando un rato.
_CREDITOS_RE = re.compile(r"credit balance|billing[_ ]error|insufficient (?:credit|fund)|payment required|"
                          r"\bAPI Error:\s*402\b|api usage limits?", re.I)
_CLAVE_RE = re.compile(r"invalid (?:x-)?api[ -]?key|x-api-key|authentication|\bAPI Error:\s*401\b|unauthorized|"
                       r"not logged in|please run /login", re.I)
_RATE_RE = re.compile(r"rate[ _-]?limit|too many requests|\b429\b", re.I)
TIPOS_FACTURACION = ("sin_cupo", "sin_login", "sin_creditos", "clave_invalida")  # → SinCreditos
TIPOS_PASAJEROS = ("rate_limit", "sobrecarga")                                 # → CuotaAgotada


def clasificar_error(texto: str, medio: str = MAX, *, error: str | None = None, status: int | None = None,
                     rechazo: bool = False) -> str | None:
    """Tipo de una falla de Claude según el medio de la corrida, o None si es un error común (del
    agente, no de cuota, red ni facturación). `error` es AssistantMessage.error, `status` el HTTP
    que informa el CLI y `rechazo` si llegó un RateLimitEvent 'rejected' (cupo del plan).

    - api: 'sin_creditos' | 'clave_invalida' (cambian de medio).
    - max: 'sin_cupo' | 'sin_login' (cambian de medio).
    - los dos: 'rate_limit' (429 sin señal de cupo agotado) | 'sobrecarga' (5xx, 529, sin red).
    """
    t = texto or ""
    if medio == API:
        if error == "billing_error" or status == 402 or _CREDITOS_RE.search(t):
            return "sin_creditos"
        if error == "authentication_failed" or status == 401 or _CLAVE_RE.search(t):
            return "clave_invalida"
    else:
        if error == "billing_error" or status == 402 or _CUPO_MAX_RE.search(t) or _CREDITOS_RE.search(t):
            return "sin_cupo"
        if error == "authentication_failed" or status == 401 or _SIN_LOGIN_RE.search(t):
            return "sin_login"
        if rechazo and (error == "rate_limit" or status == 429 or _RATE_RE.search(t)):
            return "sin_cupo"
    if error == "rate_limit" or status == 429 or _RATE_RE.search(t) or _CUPO_MAX_RE.search(t):
        return "rate_limit"
    if error == "server_error" or (status or 0) >= 500 or _SOBRECARGA_RE.search(t):
        return "sobrecarga"
    return None


def _peor(actual: str | None, nuevo: str | None) -> str | None:
    """Entre dos fallas de la misma ronda manda la de facturación (cambia de medio) sobre la pasajera."""
    if actual is None or (actual in TIPOS_PASAJEROS and nuevo in TIPOS_FACTURACION):
        return nuevo or actual
    return actual


def _epoch_en(texto: str | None) -> float | None:
    m = re.search(r"\|(\d{10})\b", texto or "")
    return float(m.group(1)) if m else None


def excepcion_falla(tipo: str, texto: str, medio: str = MAX, *, resets_at: float | None = None,
                    ahora: float | None = None) -> CuotaAgotada:
    """La excepción de una falla clasificada: SinCreditos (cambia de medio) o CuotaAgotada (pasajera)."""
    detalle = _sin_secretos(" ".join(str(texto or "sin detalle").split()))[:300]
    ahora = time.time() if ahora is None else ahora
    if tipo == "sin_cupo":
        hasta = resets_at or _epoch_en(texto)
        return SinCreditos(MAX, "sin_cupo", f"se agotó la cuota del plan ({detalle})",
                           espera_s=espera_cuota(texto, resets_at, ahora),
                           hasta=hasta if hasta and hasta > ahora else None)
    if tipo == "sin_login":
        return SinCreditos(MAX, "sin_login", f"Claude Code no tiene sesión iniciada en esta PC (corré `claude` y "
                           f"hacé /login) ({detalle})", espera_s=ESPERA_LOGIN_S)
    if tipo == "sin_creditos":
        return SinCreditos(API, "sin_creditos", f"la cuenta de la API de Anthropic no tiene créditos ({detalle})",
                           espera_s=ESPERA_CUOTA_S)
    if tipo == "clave_invalida":
        return SinCreditos(API, "clave_invalida", f"la API de Anthropic rechazó la ANTHROPIC_API_KEY ({detalle})",
                           espera_s=ESPERA_CUOTA_S)
    if tipo == "sobrecarga":
        return CuotaAgotada(f"Claude: la API está sobrecargada o no responde: {detalle[:200]}",
                            espera_s=ESPERA_SOBRECARGA_S)
    return CuotaAgotada(f"Claude: hay rate limit (demasiados pedidos seguidos): {detalle[:200]}",
                        espera_s=ESPERA_CUOTA_S)


def aviso_clave(medio: str, fuente: Any) -> str | None:
    """Aviso si el CLI no factura como se pidió (apiKeySource del init; 'none' es el login de Claude Code)."""
    if not isinstance(fuente, str) or not fuente.strip():
        return None
    fuente = fuente.strip()
    if medio == MAX and fuente.lower() != "none":
        return (f"la corrida es con el plan Max pero el CLI usa una API key (apiKeySource {fuente}): "
                "se estaría cobrando por la API")
    # Con la API, la única fuente que es la clave de Classmer-data\.env es ANTHROPIC_API_KEY ("/login
    # managed key" es la de la consola guardada en Claude Code; "none", el plan Max).
    if medio == API and fuente.upper() != "ANTHROPIC_API_KEY":
        return (f"la corrida es con la API key de Classmer-data\\.env pero el CLI no la usa (apiKeySource "
                f"{fuente}): se paga con otra cosa")
    return None


def describir_error_sdk(e: BaseException) -> str:
    """Error del SDK/CLI en criollo (queda en sesiones.error); el detalle técnico va entre paréntesis."""
    texto = f"{e.__class__.__name__}: {e}"
    detalle = f"{texto} {getattr(e, 'original_error', '') or ''}"
    if "maximum buffer size" in detalle or "exceeds limit" in detalle:
        return ("un mensaje del agente superó el tope del SDK (suele ser una foto muy pesada leída con "
                f"Read; revisá MAX_BUFFER_SDK y el tamaño de las fotos) ({texto[:300]})")
    return texto


# --- runner --------------------------------------------------------------------------------------

@dataclass
class _Ronda:
    terminada: bool = False  # llegó el ResultMessage
    texto_final: str = ""
    es_error: bool = False
    error: str | None = None
    turnos: int = 0
    costo: float | None = None
    falla: str | None = None   # tipo de clasificar_error (pasajera o de facturación)
    texto_falla: str = ""

    @property
    def bien(self) -> bool:
        return self.terminada and not self.es_error


@dataclass
class _Corrida:
    medio: str = MAX
    modelo: str | None = None
    turnos: int = 0
    costo: float | None = None
    resets_at: float | None = None
    rechazo: bool = False      # llegó un RateLimitEvent 'rejected': el cupo del plan se agotó
    rondas: list[_Ronda] = field(default_factory=list)
    seguimientos: int = 0
    timeout: bool = False
    errores_salida: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)


class AgenteClaude:
    """Ejecutor del apunte con el Claude Agent SDK. `cliente` se puede reemplazar en tests.
    La API key (sólo para las corridas con facturación api) sale de `clave_api` o de la config."""

    motor = "claude-agent"

    def __init__(self, cfg: Config | None = None, *, cliente: Callable[..., Any] | None = None,
                 clave_api: str | None = None):
        self.cfg = cfg
        self._cliente = cliente
        self._clave_api = clave_api

    def _clave(self) -> str | None:
        return self._clave_api or (getattr(self.cfg, "anthropic_api_key", None) if self.cfg is not None else None)

    def ejecutar(self, trabajo: TrabajoApunte) -> ResultadoAgente:
        """Corre el agente hasta que termine (o venza trabajo.timeout_s). Si el medio de facturación
        no sirve (sin créditos o clave inválida con la API; cupo agotado o sin login con el plan Max)
        lanza SinCreditos; si es algo pasajero (rate limit, API sobrecargada o caída, sin red),
        CuotaAgotada. Así la etapa no compila ni publica lo que quedó a medias."""
        workdir = Path(trabajo.workdir)
        ruta_log = workdir / NOMBRE_LOG
        with contextlib.suppress(OSError):
            ruta_log.unlink(missing_ok=True)
        registro = Registro(ruta_log, workdir / CARPETA_SALIDA)
        medio = API if trabajo.facturacion == API else MAX
        clave_cfg = self._clave()
        clave = clave_cfg if medio == API else None
        if clave_cfg:  # en los dos medios: la clave del .env nunca se escribe, aunque aparezca
            registro.secretos.append(clave_cfg)
        materia = Path(trabajo.carpeta_materia) if trabajo.carpeta_materia else None
        if materia is not None and not materia.is_dir():
            registro(f"la carpeta de la materia no existe ({materia}): no se monta")
            materia = None
        estado = EstadoPolitica(workdir, materia, max_intentos=max(1, trabajo.max_intentos_compilacion),
                                tope_s=trabajo.tope_correccion_s)
        corrida = _Corrida(medio=medio)
        registro(f"inicia: modelo {trabajo.modelo or '(default)'}, esfuerzo {trabajo.esfuerzo or '(default)'}, "
                 f"facturación {medio}, cwd {estado.workdir}, materia {estado.carpeta_materia or '(no)'}, "
                 f"tope {trabajo.timeout_s / 60:g} min")
        if medio == API and not clave:
            e = SinCreditos(API, "clave_invalida", "no hay ANTHROPIC_API_KEY en Classmer-data\\.env",
                            espera_s=ESPERA_CUOTA_S)
            registro(f"{e}: no lanzo el CLI")
            raise e
        error: str | None = None
        try:
            if sys.platform == "win32":
                with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
                    runner.run(self._sesion(trabajo, estado, registro, corrida))
            else:
                with asyncio.Runner() as runner:
                    runner.run(self._sesion(trabajo, estado, registro, corrida))
        except CuotaAgotada:
            raise
        except Exception as e:  # noqa: BLE001 — el CLI puede fallar de muchas formas
            error = registro.ocultar(describir_error_sdk(e))
            registro(f"error: {error[:2000]}")
            tipo = clasificar_error(str(e), medio, rechazo=corrida.rechazo)
            if tipo and not (corrida.rondas and corrida.rondas[0].bien):
                self._fallar(tipo, str(e), corrida, registro)

        principal = corrida.rondas[0] if corrida.rondas else None
        # Falla de cuota, red o facturación en la ronda principal: se reintenta más tarde (o con el
        # otro medio). En un seguimiento, se usa lo que hay.
        if principal is not None and principal.falla and not principal.bien:
            self._fallar(principal.falla, principal.error or principal.texto_final or principal.texto_falla
                         or "sin detalle", corrida, registro)
        if error is None:
            if corrida.timeout:
                error = f"el agente pasó el tope de {trabajo.timeout_s / 60:g} min y se cortó"
            elif principal is None:
                error = "el agente no devolvió resultado"
            elif principal.es_error:
                error = f"el agente terminó con error: {(principal.error or principal.texto_final)[:500]}"
            elif not principal.terminada:
                error = "el agente cortó sin terminar la ronda"
            if error is not None:
                error = registro.ocultar(error)  # va a sesiones.error y al panel
        try:
            corrida.errores_salida = list(trabajo.validar_meta())
        except Exception as e:  # noqa: BLE001
            corrida.errores_salida = [f"no pude validar la salida: {e}"]
        ok = error is None and not corrida.errores_salida
        resumen = registro.resumen(modelo=corrida.modelo or trabajo.modelo, turnos=corrida.turnos,
                                   costo=corrida.costo, seguimientos=corrida.seguimientos,
                                   errores_salida=corrida.errores_salida, error=error, estado=estado,
                                   esfuerzo=trabajo.esfuerzo, facturacion=medio, avisos=corrida.avisos)
        registro("fin.\n" + resumen)
        log = resumen + "\n\n--- detalle ---\n" + "\n".join(registro.lineas)
        return ResultadoAgente(ok=ok, modelo=corrida.modelo or trabajo.modelo, costo_usd=corrida.costo,
                               intentos_compilacion=estado.intentos, turnos=corrida.turnos, log=log, error=error)

    @staticmethod
    def _fallar(tipo: str, texto: str, corrida: _Corrida, registro: Registro) -> NoReturn:
        e = excepcion_falla(tipo, registro.ocultar(texto), corrida.medio, resets_at=corrida.resets_at)
        # Lo que ya gastó este intento (con la API es plata real): la etapa lo deja en pipeline.log.
        e.costo_usd = corrida.costo  # type: ignore[attr-defined]
        if corrida.costo:
            registro(f"este intento ya gastó {texto_costo(corrida.costo, corrida.medio)}")
        if isinstance(e, SinCreditos):
            registro(f"{e} (facturación {corrida.medio}): la etapa sigue con el otro medio si el modo lo "
                     f"permite; si no, reintento en {e.espera_s / 60:.0f} min")
        else:
            registro(f"{e}; reintento en {e.espera_s / 60:.0f} min")
        raise e

    # --- sesión asyncio ---
    def _opciones(self, trabajo: TrabajoApunte, estado: EstadoPolitica, registro: Registro) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        async def pre_tool_use(entrada: Any, tool_use_id: str | None, _contexto: Any) -> dict[str, Any]:
            nombre, datos = "", {}
            try:
                nombre = str(entrada.get("tool_name") or "")
                datos = entrada.get("tool_input") or {}
                estado.sesion_cli(entrada.get("transcript_path"))
                permitir, motivo = decidir(nombre, datos, estado)
            except Exception as e:  # noqa: BLE001 — ante la duda, se deniega
                permitir, motivo = False, f"error interno de la política ({e.__class__.__name__})"
            registro.herramienta(nombre, datos if isinstance(datos, dict) else {}, permitir, motivo, tool_use_id)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if permitir else "deny",
                "permissionDecisionReason": motivo,
            }}

        sistema: Any = trabajo.prompt_sistema
        if len(sistema) > LARGO_PROMPT_ARGV:
            ruta = estado.workdir.parent / f"{estado.workdir.name}.sistema.md"
            ruta.write_text(sistema, encoding="utf-8")
            sistema = {"type": "file", "path": str(ruta)}

        def stderr(linea: str) -> None:
            registro.stderr.append(registro.ocultar(linea)[:500])

        # El entorno del CLI es el del proceso (sin VARIABLES_FUERA, ver _conectar) + este `env`. Con
        # facturación api, la clave va SÓLO acá: es de esta corrida y nunca toca os.environ.
        entorno = dict(ENTORNO_AGENTE)
        if trabajo.facturacion == API:
            clave = self._clave()
            if not clave:
                raise SinCreditos(API, "clave_invalida", "no hay ANTHROPIC_API_KEY en Classmer-data\\.env",
                                  espera_s=ESPERA_CUOTA_S)
            entorno["ANTHROPIC_API_KEY"] = clave

        return ClaudeAgentOptions(
            tools=list(HERRAMIENTAS),
            allowed_tools=[],
            disallowed_tools=list(PROHIBIDAS),
            system_prompt=sistema,
            permission_mode="dontAsk",   # lo que el hook no permite explícitamente, se deniega
            cwd=str(estado.workdir),
            add_dirs=[str(estado.carpeta_materia)] if estado.carpeta_materia else [],
            setting_sources=[],
            skills=[],
            strict_mcp_config=True,
            model=trabajo.modelo or None,
            effort=trabajo.esfuerzo or None,  # el SDK lo pasa como `--effort <nivel>` al CLI
            max_turns=MAX_TURNOS,
            verbatim_prompts=True,       # el transcript no puede disparar @archivo ni /comandos
            env=entorno,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use], timeout=60)]},
            stderr=stderr,
            # Un Read de una foto vuelve en base64 dentro de UN mensaje del CLI: con el tope por
            # defecto del SDK (1 MiB) una foto real del celu lo rompía. Es sólo un tope.
            max_buffer_size=MAX_BUFFER_SDK,
        )

    async def _sesion(self, trabajo: TrabajoApunte, estado: EstadoPolitica, registro: Registro,
                      corrida: _Corrida) -> None:
        loop = asyncio.get_running_loop()
        limite = loop.time() + trabajo.timeout_s
        opciones = self._opciones(trabajo, estado, registro)
        if self._cliente is not None:
            cliente = self._cliente(opciones)
        else:
            from claude_agent_sdk import ClaudeSDKClient

            cliente = ClaudeSDKClient(opciones)
        await self._conectar(cliente, registro, corrida.medio)
        try:
            tarea = asyncio.create_task(self._conversar(cliente, trabajo, estado, registro, corrida))
            hechas, _ = await asyncio.wait({tarea}, timeout=max(1.0, limite - loop.time()))
            if not hechas:
                corrida.timeout = True
                registro(f"tope de {trabajo.timeout_s / 60:g} min: interrumpo al agente")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(cliente.interrupt(), 15)
                hechas, _ = await asyncio.wait({tarea}, timeout=30)
                if not hechas:
                    tarea.cancel()
                    with contextlib.suppress(BaseException):
                        await tarea
            else:
                tarea.result()  # propaga errores de la conversación
        finally:
            try:
                await cliente.disconnect()
            except Exception as e:  # noqa: BLE001
                registro(f"aviso al cerrar el CLI: {e.__class__.__name__}: {e}")

    @staticmethod
    async def _conectar(cliente: Any, registro: Registro, medio: str = MAX) -> None:
        """Lanza el CLI sin las claves de API del entorno del proceso ni variables de una sesión de
        Claude Code padre (en los dos medios: con la API, la clave es la de las opciones)."""
        with _entorno_lock:
            sacadas = {k: os.environ.pop(k) for k in VARIABLES_FUERA if k in os.environ}
            if "ANTHROPIC_API_KEY" in sacadas or "ANTHROPIC_AUTH_TOKEN" in sacadas:
                registro("había una clave de API de Anthropic en el entorno del proceso: la saqué "
                         + ("(la corrida usa la ANTHROPIC_API_KEY de Classmer-data\\.env)" if medio == API
                            else "para usar el login de Claude Code"))
            otras = [k for k in sacadas if not k.startswith("ANTHROPIC_")]
            if otras:
                registro(f"saqué {len(otras)} variable(s) de una sesión de Claude Code padre")
            try:
                await cliente.connect()
            finally:
                os.environ.update(sacadas)

    async def _conversar(self, cliente: Any, trabajo: TrabajoApunte, estado: EstadoPolitica,
                         registro: Registro, corrida: _Corrida) -> None:
        await cliente.query(trabajo.prompt)
        ronda = await self._recibir(cliente, registro, corrida)
        if not ronda.bien or ronda.falla or corrida.timeout:
            return
        for k in range(1, MAX_SEGUIMIENTOS + 1):
            try:
                errores = list(trabajo.validar_meta())
            except Exception as e:  # noqa: BLE001
                errores = [f"no pude validar la salida: {e}"]
            if estado.puede_compilar and estado.pdf_atrasado():
                errores.append(f"la última compilación no dejó un apunte.pdf al día: buscá el error en "
                               f"{CARPETA_SALIDA}/apunte.log, corregí y compilá")
            if not errores:
                return
            corrida.seguimientos = k
            registro(f"seguimiento {k}: la salida tiene {len(errores)} problema(s)")
            await cliente.query(mensaje_seguimiento(errores, estado))
            ronda = await self._recibir(cliente, registro, corrida)
            if not ronda.bien or ronda.falla or corrida.timeout:
                return

    @staticmethod
    async def _recibir(cliente: Any, registro: Registro, corrida: _Corrida) -> _Ronda:
        from claude_agent_sdk import (AssistantMessage, RateLimitEvent, ResultMessage, SystemMessage,
                                      TextBlock, ToolResultBlock, UserMessage)

        ronda = _Ronda()
        corrida.rondas.append(ronda)
        async for m in cliente.receive_response():
            if isinstance(m, SystemMessage):
                if m.subtype == "init":
                    corrida.modelo = m.data.get("model") or corrida.modelo
                    fuente = m.data.get("apiKeySource")
                    registro(f"CLI listo: modelo {m.data.get('model')}, permisos {m.data.get('permissionMode')}, "
                             f"herramientas {m.data.get('tools')}, clave {fuente}")
                    aviso = aviso_clave(corrida.medio, fuente)
                    if aviso and aviso not in corrida.avisos:
                        corrida.avisos.append(aviso)
                        registro(f"AVISO: {aviso}")
            elif isinstance(m, AssistantMessage):
                corrida.modelo = corrida.modelo or m.model
                textos = [b.text for b in m.content if isinstance(b, TextBlock) and b.text.strip()]
                for t in textos:
                    registro(f"agente: {' '.join(t.split())[:400]}")
                if m.error:  # p. ej. 5xx: lo que quedó a medias no se compila
                    registro(f"error del modelo: {m.error}")
                    texto = " ".join(textos)
                    tipo = clasificar_error(texto, corrida.medio, error=m.error, rechazo=corrida.rechazo)
                    if _peor(ronda.falla, tipo) != ronda.falla:
                        ronda.falla, ronda.texto_falla = tipo, texto or str(m.error)
            elif isinstance(m, UserMessage) and isinstance(m.content, list):
                for b in m.content:
                    if isinstance(b, ToolResultBlock):
                        registro.resultado_herramienta(b.tool_use_id, b.content, b.is_error)
            elif isinstance(m, RateLimitEvent):
                info = m.rate_limit_info
                if info.status != "allowed":
                    registro(f"rate limit: {info.status} ({info.rate_limit_type}, se libera {info.resets_at})")
                if info.status == "rejected":
                    corrida.rechazo = True
                    if info.resets_at:
                        corrida.resets_at = float(info.resets_at)
            elif isinstance(m, ResultMessage):
                ronda.terminada = True
                ronda.turnos = m.num_turns or 0
                ronda.costo = m.total_cost_usd
                ronda.texto_final = m.result or ""
                ronda.es_error = bool(m.is_error) or m.subtype != "success"
                corrida.turnos += ronda.turnos
                if m.total_cost_usd is not None:  # el CLI informa el acumulado de la sesión
                    corrida.costo = max(corrida.costo or 0.0, float(m.total_cost_usd))
                if ronda.es_error:
                    detalle = "; ".join(m.errors or []) or ronda.texto_final or m.subtype
                    ronda.error = f"{m.subtype}: {detalle}"[:1000]
                    registro(f"resultado con error: {ronda.error}")
                    if m.subtype not in ("error_max_turns",):
                        tipo = clasificar_error(detalle, corrida.medio, status=m.api_error_status,
                                                rechazo=corrida.rechazo)
                        if _peor(ronda.falla, tipo) != ronda.falla:
                            ronda.falla, ronda.texto_falla = tipo, detalle
                else:
                    registro(f"terminó la ronda: {ronda.turnos} turnos, costo acumulado {m.total_cost_usd}")
        return ronda


def mensaje_seguimiento(errores: list[str], estado: EstadoPolitica) -> str:
    lista = "\n".join(f"- {e}" for e in errores[:20])
    compilar = (f"Si cambiás apunte.tex, volvé a compilar como antes (te quedan {estado.restantes} intentos)."
                if estado.restantes else "No quedan intentos de compilación: no compiles más.")
    return (f"Revisé lo que quedó en output/ y hay problemas:\n{lista}\n\n"
            f"Corregilos editando output/meta.json y/o output/apunte.tex. {compilar} "
            "Cuando esté, terminá con un resumen de una línea.")
