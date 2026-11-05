"""Compilación LaTeX con MiKTeX (§8): pdflatex o lualatex sin consola y con un resumen legible del log.

No hay latexmk (necesita Perl), así que las pasadas se corren a mano. La carpeta tiene que estar en
una ruta ASCII: LaTeX y algunas herramientas fallan con rutas como `Análisis Matemático…`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from classmer.config import PLANTILLAS_DIR
from classmer.util import escribir_atomico

MOTORES = ("pdflatex", "lualatex", "xelatex")
CLS = "classmer.cls"
EJEMPLO = "ejemplo.tex"
RUTA_CLS = PLANTILLAS_DIR / CLS
RUTA_EJEMPLO = PLANTILLAS_DIR / EJEMPLO

MAX_RESUMEN = 4000
_ANCHO_LOG = 79  # TeX corta las líneas del log a este ancho (max_print_line)
_EXT_AUX = (".aux", ".toc", ".out", ".lof", ".lot", ".nav", ".snm")

# "./apunte.tex:12: Undefined control sequence." o "C:/…/tcolorbox.sty:345: …" (-file-line-error).
_ERROR_RE = re.compile(r"^(?P<archivo>(?:[A-Za-z]:)?[^:\r\n]*?\.[A-Za-z0-9]{1,8}):(?P<linea>\d+): (?P<mensaje>.*)$")
_CONTEXTO_RE = re.compile(r"^l\.\d+(?:\s|$)")
_RUIDO_RE = re.compile(r"^(See the LaTeX manual|See the \S+ package documentation|Type {1,2}H <return>|\s*\.\.\.\s*$)")
_AVISO_RE = re.compile(r"^(?:LaTeX|LaTeX Font|Package [\w.@-]+|Class [\w.@-]+) Warning: ")
_CONTINUACION_RE = re.compile(r"^\([\w.@-]+\)\s+")
_SALIDA_RE = re.compile(r"Output written on .*?\((\d+) pages?")
_MEMORIA = "Here is how much of TeX's memory you used:"
# Renglones de la cola que no ayudan: registros, Info de paquetes, versiones de archivos.
_RUIDO_COLA_RE = re.compile(
    r"^(?:\\[\w@]+=\\(?:count|dimen|skip|muskip|toks|box|write|read|insert|language|marks)\d+"
    r"|(?:Package|Class) [\w.@-]+ Info:|LaTeX (?:Font )?Info:|File: |Package: |Document Class: |\s*\)+\s*$)")


@dataclass
class ResultadoCompilacion:
    ok: bool
    pdf: Path | None
    log: str  # resumen legible: errores, avisos y últimas líneas relevantes (≤ ~4000 caracteres)
    errores: list[str] = field(default_factory=list)  # "archivo:linea: mensaje" (de -file-line-error)
    ruta_log: Path | None = None
    avisos: list[str] = field(default_factory=list)  # "Foto no encontrada", referencias sin resolver, …
    pasadas: int = 0  # pasadas que se corrieron de verdad
    paginas: int | None = None
    duracion_s: float = 0.0


def comando(motor: str, archivo: str) -> list[str]:
    """Línea de comando de una pasada (la misma que puede correr el agente a mano). Sin shell-escape:
    MiKTeX deja por defecto un \\write18 restringido (kpsewhich, bibtex, …) que la plantilla no usa."""
    return [motor, "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-no-shell-escape", archivo]


def motor_disponible(motor: str = "pdflatex") -> bool:
    return shutil.which(motor) is not None


# --- lecturas de archivos de afuera (filtro léxico) ---
# MiKTeX deja que un .tex lea cualquier archivo de la PC (AllowUnsafeInputFiles): se rechaza lo que
# lee archivos por su cuenta y las rutas absolutas o con '..'. Es un filtro léxico, no un sandbox
# (\csname y \catcode no se tocan: los usa código LaTeX legítimo).
_LEE_SIEMPRE_RE = re.compile(
    r"\\(?:openin|readline|read|directlua|luadirect|luaexec|latelua|pdffiledump|filedump|pdfmdfivesum|"
    r"filemdfivesum|mdfivesum|scantokens)(?![A-Za-z])|\\begin\s*\{luacode\*?\}")
_LEE_ARCHIVO_RE = re.compile(
    r"\\(?P<cmd>input@path|Ginput@path|@@input|input|include|InputIfFileExists|lstinputlisting|verbatiminput|"
    r"VerbatimInput|includepdf|includegraphics|pizarra|graphicspath)(?![A-Za-z])")
_SOLO_LLAVES = {"input@path", "Ginput@path", "graphicspath"}  # sin {…} no leen nada (p. ej. \let)
_COMENTARIO_TEX = re.compile(r"(?<!\\)%.*")
# Dentro de un argumento: el comentario se lleva el fin de línea y TeX saltea los espacios de la línea
# siguiente (`{.%⏎./x}` es `../x`).
_COMENTARIO_EN_ARG = re.compile(r"(?<!\\)%[^\n]*\n?[ \t]*")
_SIN_LLAVES_RE = re.compile(r"(?:\\.|[^\s{}%\\]|%[^\n]*\n?[ \t]*)+")
# Un opcional de xparse (`o`) no admite \par: con una línea en blanco o \par antes de cerrarlo, TeX da error.
_FIN_PARRAFO = re.compile(r"\n[ \t]*\n|\\par(?![A-Za-z])")


def lecturas_externas(texto: str) -> list[str]:
    """Lo que en un .tex lee archivos fuera de la carpeta: ['\\openin', '\\input{C:/…}', …] o [].

    Se mira el texto sin comentarios y también entero: un `%` no siempre es comentario (`\\url{…%20…}`,
    `\\href`, `\\verb|%|`, verbatim) y lo que sigue en esa línea TeX lo ejecuta igual. Es conservador:
    una lectura de afuera comentada (`% \\input{C:/…}`) también cuenta."""
    texto = _sin_circunflejos(texto)
    hallazgos: list[str] = []
    for variante in (_COMENTARIO_TEX.sub("", texto), texto):
        hallazgos += _hallazgos(variante)
    return list(dict.fromkeys(h[:120] for h in hallazgos))


def _hallazgos(texto: str) -> list[str]:
    hallazgos = [m.group(0) for m in _LEE_SIEMPRE_RE.finditer(texto)]
    for m in _LEE_ARCHIVO_RE.finditer(texto):
        cmd = m.group("cmd")
        # TeX saltea espacios, saltos y comentarios después del nombre: `\includegraphics *{…}` es la forma con *.
        inicio = _saltar_blancos(texto, m.end())
        if texto.startswith("*", inicio):
            inicio += 1
        # Los opcionales se saltean de las dos formas en que los lee TeX: se mira el archivo de cada una.
        for fin in sorted({_fin_opcionales(texto, inicio, anidados=False),
                           _fin_opcionales(texto, inicio, anidados=True)}):
            if cmd in _SOLO_LLAVES and not texto.startswith("{", fin):
                continue
            arg = _COMENTARIO_EN_ARG.sub("", _argumento(texto, fin))
            ruta = arg.strip().strip('"').strip()
            # `$`: la búsqueda de archivos de TeX puede expandir variables de entorno ($HOME, $ANTHROPIC_API_KEY):
            # leería afuera o dejaría el valor en un mensaje de error.
            if ":" in ruta or ".." in ruta or "$" in ruta or ruta.startswith(("/", "\\", "|", "~")):
                hallazgos.append(f"\\{cmd}{{{' '.join(arg.split())}}}")
    return hallazgos


def lecturas_externas_en(carpeta: Path, principal: str = "apunte.tex") -> list[str]:
    """lecturas_externas del .tex principal y de todo lo demás que haya en la carpeta (un
    \\input{parte2} o un .aux escrito a mano pasan por TeX igual), salvo classmer.cls (la pone el
    server) y el .log del principal (lo reescribe TeX en cada pasada y trae líneas "\\…=\\read3").
    Los de otros archivos van con su ruta: 'parte2.tex: \\read'."""
    carpeta = Path(carpeta)
    fijos = {CLS.casefold(), Path(principal).with_suffix(".log").name.casefold()}
    hallazgos: list[str] = []
    archivos = sorted((p for p in carpeta.rglob("*") if p.is_file()),
                      key=lambda p: (p.name.casefold() != principal.casefold(), str(p)))
    for p in archivos:
        rel = p.relative_to(carpeta).as_posix()
        if rel.casefold() in fijos:
            continue
        try:
            texto = p.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        prefijo = "" if rel.casefold() == principal.casefold() else f"{rel}: "
        hallazgos += [prefijo + h for h in lecturas_externas(texto)]
    return hallazgos


def _sin_circunflejos(texto: str) -> str:
    """^^5c → \\, ^^M → retorno de carro…, como lee TeX: que no sirvan para disfrazar un comando."""
    for _ in range(4):
        nuevo = re.sub(r"\^\^\^\^([0-9a-f]{4})", lambda m: chr(int(m.group(1), 16)), texto)
        nuevo = re.sub(r"\^\^([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), nuevo)
        nuevo = re.sub(r"\^\^([\x00-\x7f])", lambda m: chr(ord(m.group(1)) ^ 64), nuevo)
        if nuevo == texto:
            break
        texto = nuevo
    return texto


def _saltar_blancos(texto: str, i: int) -> int:
    """Saltea espacios, saltos de línea y comentarios `%…` desde texto[i], como TeX después de un comando."""
    while i < len(texto):
        if texto[i].isspace():
            i += 1
        elif texto[i] == "%":
            fin = texto.find("\n", i)
            i = len(texto) if fin < 0 else fin + 1
        else:
            break
    return i


def _fin_opcionales(texto: str, i: int, *, anidados: bool) -> int:
    """Dónde empieza el argumento después de los opcionales `[…]` (y los espacios) que siguen a texto[i].

    Un `]` entre llaves no cierra el opcional (`[trim={0 0 1cm 0}, alt={a]b}]`). LaTeX2e
    (`\\includegraphics`) cierra en el primer `]` fuera de llaves; xparse (`\\NewDocumentCommand`)
    además respeta los corchetes anidados (`[a[b]c]`): el que llama prueba las dos lecturas. Un
    `\\x` (`\\]`, `\\{`) no cuenta, ni lo que está en un comentario `%…`. Si un opcional no cierra, no
    queda argumento (TeX daría error); con xparse tampoco si antes de cerrar hay una línea en blanco o un
    `\\par` (si no, un `[0;1)` sin cerrar barrería el documento hasta el `]` de un `(a;b]`)."""
    while True:
        i = _saltar_blancos(texto, i)
        if not texto.startswith("[", i):
            return i
        llaves = corchetes = 0
        j = i + 1
        while j < len(texto):
            c = texto[j]
            if anidados and c in "\\\n" and _FIN_PARRAFO.match(texto, j):
                return len(texto)
            if c == "\\":
                j += 2
                continue
            if c == "%":  # hasta el fin de línea (el salto lo mira la vuelta siguiente: puede ser un párrafo)
                fin = texto.find("\n", j)
                j = len(texto) if fin < 0 else fin
                continue
            if c == "{":
                llaves += 1
            elif c == "}":
                llaves = max(0, llaves - 1)  # una '}' de más es error de TeX: ante la duda, sigue cerrando
            elif llaves == 0 and c == "[" and anidados:
                corchetes += 1
            elif llaves == 0 and c == "]":
                if corchetes == 0:
                    break
                corchetes -= 1
            j += 1
        else:
            return len(texto)
        i = j + 1


def _argumento(texto: str, i: int) -> str:
    """El argumento que empieza en texto[i]: un grupo {…} balanceado o, sin llaves, hasta el espacio.
    Trae los comentarios `%…` tal cual (los saca el que llama), pero sus llaves no cuentan ni cortan."""
    if texto.startswith("{", i):
        nivel = 0
        j, tope = i, min(len(texto), i + 2000)
        while j < tope:
            c = texto[j]
            if c == "\\":
                j += 2
                continue
            if c == "%":
                fin = texto.find("\n", j, tope)
                j = tope if fin < 0 else fin
                continue
            if c == "{":
                nivel += 1
            elif c == "}":
                nivel -= 1
                if nivel == 0:
                    return texto[i + 1:j]
            j += 1
        return texto[i + 1:tope]
    m = _SIN_LLAVES_RE.match(texto[i:i + 500])
    return m.group(0) if m else ""


def copiar_plantilla(destino: Path, *, ejemplo: bool = False) -> Path:
    """Copia classmer.cls (y, si se pide, ejemplo.tex) a la carpeta `destino`. Devuelve la ruta del .cls."""
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    ruta = destino / CLS
    escribir_atomico(ruta, RUTA_CLS.read_bytes())
    if ejemplo:
        escribir_atomico(destino / EJEMPLO, RUTA_EJEMPLO.read_bytes())
    return ruta


def compilar(carpeta: Path, archivo: str = "apunte.tex", *, motor: str = "pdflatex", pasadas: int = 2,
             timeout_s: float = 600) -> ResultadoCompilacion:
    """Compila `carpeta/archivo` en `pasadas` pasadas; si una falla, no corre las siguientes.

    `timeout_s` es el total para todas las pasadas. ok = la última pasada terminó bien y dejó un PDF nuevo.
    Lanza ValueError si el motor no existe o la ruta no es ASCII.
    """
    carpeta = Path(carpeta)
    if motor not in MOTORES:
        raise ValueError(f"motor LaTeX desconocido: {motor!r} (opciones: {', '.join(MOTORES)})")
    if pasadas < 1:
        raise ValueError("pasadas tiene que ser 1 o más")
    if not str(carpeta.absolute()).isascii() or not archivo.isascii():
        raise ValueError(f"LaTeX necesita una ruta sin tildes ni caracteres especiales: {carpeta / archivo}")

    tex = carpeta / archivo
    base = tex.with_suffix("")
    ruta_pdf, ruta_log = base.with_suffix(".pdf"), base.with_suffix(".log")
    if not tex.is_file():
        return ResultadoCompilacion(ok=False, pdf=None, log=f"No existe {tex}.")
    if not motor_disponible(motor):
        return ResultadoCompilacion(ok=False, pdf=None, log=f"No encuentro {motor} en el PATH (¿está MiKTeX?).")

    inicio = time.time()
    limite = inicio + timeout_s
    corridas, rc, salida, motivo = 0, None, "", ""
    reintento_aux = False
    while corridas < pasadas:
        restante = limite - time.time()
        if restante <= 0:
            motivo = "timeout"
            break
        rc, salida = _correr(comando(motor, archivo), carpeta, restante)
        corridas += 1
        if rc is None:
            motivo = "timeout"
            break
        if rc != 0:
            # Un .aux roto de una compilación anterior hace fallar la primera pasada: se borra y se reintenta.
            if corridas == 1 and not reintento_aux and _fallo_por_auxiliar(ruta_log, base.name):
                reintento_aux = True
                _borrar_auxiliares(base)
                corridas = 0
                continue
            motivo = "error"
            break

    duracion = time.time() - inicio
    log_nuevo = ruta_log.is_file() and ruta_log.stat().st_mtime >= inicio - 1
    texto = _leer_log(ruta_log) if log_nuevo else salida
    lineas = _desenvolver(texto)
    errores = _errores(lineas)
    avisos = _avisos(lineas)
    paginas = _paginas(lineas)
    pdf_nuevo = ruta_pdf.is_file() and ruta_pdf.stat().st_mtime >= inicio - 1
    ok = not motivo and rc == 0 and pdf_nuevo

    if motivo == "timeout":
        cabecera = (f"{motor} tardó más de {timeout_s:.0f} s y se cortó (pasada {corridas} de {pasadas}). "
                    "¿Una macro que se llama a sí misma, o MiKTeX bajando paquetes?")
    elif motivo == "error":
        cabecera = f"{motor} falló en la pasada {corridas} de {pasadas} (código {rc}); no se corrieron las siguientes."
    elif not pdf_nuevo:
        cabecera = f"{motor} terminó sin errores pero no generó {ruta_pdf.name} (¿el documento está vacío?)."
    else:
        extra = f", {paginas} páginas" if paginas else ""
        cabecera = f"{motor} compiló bien en {corridas} pasadas ({duracion:.1f} s{extra})."

    return ResultadoCompilacion(
        ok=ok,
        pdf=ruta_pdf if ok else None,
        log=_resumen(cabecera, lineas, errores, avisos, con_cola=not ok),
        errores=[e.texto for e in errores],
        ruta_log=ruta_log if ruta_log.is_file() else None,
        avisos=avisos,
        pasadas=corridas,
        paginas=paginas,
        duracion_s=round(duracion, 2),
    )


# --- proceso ---

def _correr(cmd: list[str], carpeta: Path, timeout: float) -> tuple[int | None, str]:
    """Corre una pasada sin consola. Devuelve (código, salida); código None si se cortó por tiempo."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, cwd=carpeta, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=flags)
    except OSError as e:
        return 1, f"No pude ejecutar {cmd[0]}: {e}"
    try:
        datos, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _matar(proc)
        try:
            datos, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            datos = b""
        return None, (datos or b"").decode("utf-8", errors="replace")
    return proc.returncode, (datos or b"").decode("utf-8", errors="replace")


def _matar(proc: subprocess.Popen) -> None:
    """Mata el proceso y sus hijos (MiKTeX puede abrir procesos auxiliares, p. ej. para instalar paquetes)."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _fallo_por_auxiliar(ruta_log: Path, nombre: str) -> bool:
    """¿El error vino de leer un .aux/.toc/… del propio documento?"""
    if not ruta_log.is_file():
        return False
    for e in _errores(_desenvolver(_leer_log(ruta_log))):
        if e.archivo:
            p = Path(e.archivo)
            if p.suffix.lower() in _EXT_AUX and p.stem.lower() == nombre.lower():
                return True
    return False


def _borrar_auxiliares(base: Path) -> None:
    for ext in _EXT_AUX:
        try:
            base.with_suffix(ext).unlink(missing_ok=True)
        except OSError:
            pass


# --- lectura del log ---

@dataclass
class _Error:
    archivo: str | None
    linea: int | None
    mensaje: str
    indice: int  # renglón del log donde aparece
    contexto: list[str] = field(default_factory=list)

    @property
    def texto(self) -> str:
        if self.archivo is None:
            return self.mensaje
        return f"{self.archivo}:{self.linea}: {self.mensaje}"


def _leer_log(ruta: Path) -> str:
    datos = ruta.read_bytes()
    try:
        return datos.decode("utf-8")
    except UnicodeDecodeError:
        texto = datos.decode("utf-8", errors="replace")
        # Si casi todo lo que no es ASCII quedó roto, el log estaba en latin-1.
        rotos = texto.count("\ufffd")
        if rotos > max(3, sum(1 for c in texto if ord(c) > 127) // 2):
            return datos.decode("latin-1")
        return texto


def _desenvolver(texto: str) -> list[str]:
    """Une las líneas que TeX cortó al ancho del log (si no, se pierden rutas y mensajes largos)."""
    lineas: list[str] = []
    pegar = False
    for linea in texto.splitlines():
        if pegar and lineas:
            lineas[-1] += linea
        else:
            lineas.append(linea)
        pegar = len(linea.encode("utf-8", errors="replace")) == _ANCHO_LOG
    return lineas


def _limpiar_archivo(archivo: str) -> str:
    archivo = archivo.strip()
    return archivo[2:] if archivo.startswith(("./", ".\\")) else archivo


def _errores(lineas: list[str]) -> list[_Error]:
    """Errores del log con su contexto (la línea "l.NN …"). Si ninguno trae archivo, los "! …"."""
    errores: list[_Error] = []
    for i, linea in enumerate(lineas):
        m = _ERROR_RE.match(linea)
        if m:
            mensaje = m.group("mensaje").strip()
            if mensaje.startswith("==>"):  # "==> Fatal error occurred, no output PDF file produced!"
                continue
            error = _Error(_limpiar_archivo(m.group("archivo")), int(m.group("linea")), mensaje, i)
        elif linea.startswith("! ") and not linea.startswith("!  ==>"):
            error = _Error(None, None, linea[2:].strip(), i)
        else:
            continue
        for k in range(i + 1, min(i + 12, len(lineas))):
            sig = lineas[k]
            if _ERROR_RE.match(sig) or sig.startswith("!"):
                break
            if sig.strip() and not _RUIDO_RE.match(sig):
                error.contexto.append(sig.rstrip())
            if _CONTEXTO_RE.match(sig):
                if k + 1 < len(lineas) and lineas[k + 1].strip():
                    error.contexto.append(lineas[k + 1].rstrip())
                break
        error.contexto = error.contexto[:6]
        errores.append(error)
    con_archivo = [e for e in errores if e.archivo is not None]
    return con_archivo or errores


def _avisos(lineas: list[str]) -> list[str]:
    """Avisos útiles para corregir (los Overfull/Underfull se cuentan aparte, en el resumen)."""
    avisos: list[str] = []
    for i, linea in enumerate(lineas):
        if not _AVISO_RE.match(linea):
            continue
        texto = linea.strip()
        for sig in lineas[i + 1:i + 6]:
            if not _CONTINUACION_RE.match(sig):
                break
            texto += " " + _CONTINUACION_RE.sub("", sig).strip()
        if texto not in avisos:
            avisos.append(texto)
    # Primero los de la plantilla (fotos que faltan), después el resto.
    avisos.sort(key=lambda a: 0 if a.startswith("Class classmer") else 1)
    return avisos[:30]


def _paginas(lineas: list[str]) -> int | None:
    for linea in reversed(lineas):
        m = _SALIDA_RE.search(linea)
        if m:
            return int(m.group(1))
    return None


def _resumen(cabecera: str, lineas: list[str], errores: list[_Error], avisos: list[str], *, con_cola: bool) -> str:
    partes = [cabecera]
    if errores:
        partes.append("Errores:")
        for e in errores[:5]:
            partes.append(f"  {e.texto}")
            partes.extend(f"    {c}" for c in e.contexto)
    desbordes = sum(1 for linea in lineas if linea.startswith(("Overfull \\", "Underfull \\")))
    if avisos or desbordes:
        partes.append("Avisos:")
        partes.extend(f"  {a}" for a in avisos[:12])
        if len(avisos) > 12:
            partes.append(f"  … y {len(avisos) - 12} avisos más")
        if desbordes:
            partes.append(f"  {desbordes} renglones o cajas desbordados (Overfull/Underfull), en general inofensivos")
    texto = "\n".join(partes)
    if con_cola:
        cola = _cola(lineas, errores)
        lugar = MAX_RESUMEN - len(texto) - 30
        bloque: list[str] = []
        for linea in reversed(cola):
            linea = linea if len(linea) <= 300 else linea[:300] + "…"
            if sum(len(x) + 3 for x in bloque) + len(linea) + 3 > lugar:
                break
            bloque.insert(0, linea)
        if bloque:
            titulo = "Lo último antes del error:" if errores else "Últimas líneas del log:"
            texto += f"\n{titulo}\n" + "\n".join(f"  {x}" for x in bloque)
    return texto if len(texto) <= MAX_RESUMEN else texto[:MAX_RESUMEN - 1] + "…"


def _cola(lineas: list[str], errores: list[_Error]) -> list[str]:
    """Últimas líneas útiles: antes del primer error (si hay) y sin el ruido de carga de paquetes."""
    fin = len(lineas)
    if errores:
        fin = min(e.indice for e in errores)
    else:
        for i in range(len(lineas) - 1, -1, -1):
            if lineas[i].startswith(_MEMORIA):
                fin = i
                break
    utiles = [x.rstrip() for x in lineas[:fin] if x.strip() and not _RUIDO_COLA_RE.match(x)]
    return utiles[-(8 if errores else 25):]
