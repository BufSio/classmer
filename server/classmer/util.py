"""Utilidades chicas: tiempos, escritura atómica, hashes y nombres de archivo seguros."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ZONA_DEFAULT = "America/Argentina/Buenos_Aires"
ID_SESION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def ahora() -> float:
    """Tiempo actual en segundos epoch (lo que usa la cola de jobs)."""
    return time.time()


def zona(nombre: str = ZONA_DEFAULT) -> ZoneInfo:
    return ZoneInfo(nombre)


def ahora_iso(nombre_zona: str = ZONA_DEFAULT) -> str:
    """Fecha y hora actual en ISO 8601 con zona (§17)."""
    return datetime.now(zona(nombre_zona)).isoformat(timespec="seconds")


def parsear_iso(valor: str, nombre_zona: str = ZONA_DEFAULT) -> datetime:
    """ISO 8601 → datetime con zona. Si viene sin zona, se asume la zona local de Classmer."""
    texto = valor.strip()
    if texto.endswith("Z"):
        texto = texto[:-1] + "+00:00"
    dt = datetime.fromisoformat(texto)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zona(nombre_zona))
    return dt


def ms_a_hhmmss(ms: int | float) -> str:
    total = max(0, int(ms // 1000))
    h, resto = divmod(total, 3600)
    m, s = divmod(resto, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def tiempo_a_ms(texto: str) -> int:
    """'MM:SS', 'HH:MM:SS' o 'SS' (con decimales opcionales) → ms."""
    partes = texto.strip().split(":")
    if not 1 <= len(partes) <= 3:
        raise ValueError(f"tiempo inválido: {texto!r}")
    segundos = 0.0
    for p in partes:
        p = p.strip()
        if not p:
            raise ValueError(f"tiempo inválido: {texto!r}")
        segundos = segundos * 60 + float(p)
    if segundos < 0:
        raise ValueError(f"tiempo inválido: {texto!r}")
    return int(round(segundos * 1000))


def id_sesion_valido(valor: str) -> bool:
    # fullmatch: con match, el `$` acepta un "\n" final y el id termina en una ruta inválida.
    return isinstance(valor, str) and bool(ID_SESION_RE.fullmatch(valor))


def escribir_atomico(ruta: Path, contenido: str | bytes, *, encoding: str = "utf-8") -> None:
    """Escribe en un temporal de la misma carpeta y renombra (§10: escritura atómica)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    datos = contenido.encode(encoding) if isinstance(contenido, str) else contenido
    fd, tmp = tempfile.mkstemp(prefix=f".{ruta.name}.", suffix=".tmp", dir=ruta.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(datos)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ruta)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def escribir_json(ruta: Path, datos: Any) -> None:
    escribir_atomico(ruta, json.dumps(datos, ensure_ascii=False, indent=2) + "\n")


def leer_json(ruta: Path, default: Any = None) -> Any:
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def sha256_archivo(ruta: Path, bloque: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        while chunk := f.read(bloque):
            h.update(chunk)
    return h.hexdigest()


def sha256_texto(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


_PROHIBIDOS_WIN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def nombre_seguro(texto: str, largo_max: int = 80) -> str:
    """Nombre de carpeta válido en Windows (sin <>:"/\\|?*, sin punto ni espacio al final)."""
    limpio = _PROHIBIDOS_WIN.sub("", texto)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    limpio = limpio[:largo_max].rstrip(" .")
    return limpio or "Sin titulo"


# --- títulos (pueden traer LaTeX: "Black--Scholes", "$\\sigma$-álgebras") ----------------------

_GRIEGAS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_SIMBOLOS = {
    "infty": "∞", "to": "→", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔", "Rightarrow": "⇒",
    "iff": "⇔", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
    "in": "∈", "times": "×", "cdot": "·", "ldots": "…", "dots": "…", "cdots": "…", "pm": "±",
    "textendash": "–", "textemdash": "—", "S": "§", "partial": "∂", "nabla": "∇", "sum": "Σ", "int": "∫",
    # operadores que se leen como palabra
    **{op: op for op in ("lim", "liminf", "limsup", "sup", "inf", "max", "min", "log", "ln", "exp", "sin",
                         "cos", "tan", "det", "dim", "ker", "deg", "arg", "Pr")},
}
# Se\u00f1ales de LaTeX: un comando, guiones de TeX, ~, comillas `` '' o un $\u2026$ con contenido que no empieza
# con un n\u00famero ni un espacio ("$5 y $10" es texto). Lo que ya pas\u00f3 por limpiar_titulo no tiene ninguna.
_PARECE_LATEX = re.compile(r"\\|--|~|``|''|\$[^$\d\s][^$]*\$|\$\$|\{\s*\}")
_ACENTOS = {"'": "\u0301", "`": "\u0300", '"': "\u0308", "~": "\u0303", "^": "\u0302"}
_ESCAPADOS = {"&": "&", "%": "%", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}"}


def limpiar_titulo(texto: str | None) -> str:
    """Título en texto plano: `---` → raya, `--` → guion, sin comandos, llaves ni `$` (las letras
    griegas y algunos símbolos pasan a Unicode: `$\\sigma$` → σ). Vacío si no queda nada."""
    import unicodedata

    if not texto:
        return ""
    t = str(texto)
    if not _PARECE_LATEX.search(t):  # ya está limpio (p. ej. "Bonos a $100"): limpiarlo otra vez no cambia nada
        return re.sub(r"\s+", " ", t).strip()
    guardados: list[str] = []

    def guardar(c: str) -> str:
        guardados.append(c)
        return f"\x00{len(guardados) - 1}\x00"

    # Acentos escritos a la LaTeX: \'a, \'{a}, \'{\i}, \~n
    def acento(m: re.Match[str]) -> str:
        letra = "i" if m.group(2) in ("\\i", "i") else m.group(2)
        return unicodedata.normalize("NFC", letra + _ACENTOS[m.group(1)])

    t = re.sub(r"\\(['`\"~^])\s*\{?(\\i|[A-Za-z])\}?", acento, t)
    t = re.sub(r"\\([&%$#_{}])", lambda m: guardar(_ESCAPADOS[m.group(1)]), t)
    # En modo matemático, `_` es un subíndice: $\lim_{x \to 0}$ → "lim x → 0".
    t = re.sub(r"\$([^$]*)\$", lambda m: "$" + m.group(1).replace("_", " ") + "$", t)
    t = t.replace("---", "—").replace("--", "-")
    t = t.replace("``", "“").replace("''", "”")
    t = re.sub(r"\\\\|~|\\[,;:! ]", " ", t)

    def comando(m: re.Match[str]) -> str:
        simbolo = _GRIEGAS.get(m.group(1)) or _SIMBOLOS.get(m.group(1))
        return simbolo + m.group(2) if simbolo else ""

    t = re.sub(r"\\([A-Za-z]+)\*?(\s?)", comando, t)
    t = re.sub(r"[{}$\\]", "", t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: guardados[int(m.group(1))], t)
    return re.sub(r"\s+", " ", t).strip()


def cortar_en_palabra(texto: str, largo: int) -> str:
    """Corta en el último espacio antes de `largo` (si una palabra sola es más larga, corta ahí)."""
    texto = texto.strip()
    if len(texto) <= largo:
        return texto
    corte = texto[:largo + 1].rstrip()
    if len(corte) > largo:
        espacio = corte.rfind(" ")
        corte = corte[:espacio] if espacio > 0 else texto[:largo]
    return corte.rstrip(" .,;:-–—")
