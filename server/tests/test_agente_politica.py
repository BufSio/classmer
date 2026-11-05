"""Política de herramientas del agente de apuntes (`decidir`) y el runner con un cliente falso.

No se llama a Claude: el cliente falso reproduce los mensajes del SDK y usa el hook de verdad.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (AssistantMessage, RateLimitEvent, RateLimitInfo, ResultMessage, SystemMessage,
                              TextBlock, ToolResultBlock, ToolUseBlock, UserMessage)

from classmer import latex
from classmer.config import cargar_config
from classmer.ia import agente_apunte as ag
from classmer.ia.agente_apunte import (AgenteClaude, EstadoPolitica, Registro, TrabajoApunte, clasificar_error,
                                       clasificar_falla, decidir, espera_cuota, resolver_ruta, resumen_compilacion,
                                       validar_comando)
from classmer.ia.facturacion import SinCreditos
from classmer.pipeline import CuotaAgotada

COMPILAR = "cd output && pdflatex -interaction=nonstopmode -halt-on-error -file-line-error apunte.tex"


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def trabajo_dir(raiz: Path) -> Path:
    w = raiz / "trabajo"
    for d in ("input/fotos", "output", "plantilla"):
        (w / d).mkdir(parents=True)
    (w / "input" / "timeline.md").write_text("# Clase\n", encoding="utf-8")
    (w / "output" / "classmer.cls").write_text("% cls\n", encoding="utf-8")
    return w


@pytest.fixture
def materia(raiz: Path) -> Path:
    m = raiz / "Análisis Matemático II"
    m.mkdir()
    (m / "PROMPT-AGENTE.md").write_text("perfil", encoding="utf-8")
    return m


@pytest.fixture
def afuera(raiz: Path) -> Path:
    a = raiz / "afuera"
    a.mkdir()
    (a / "secreto.txt").write_text("no", encoding="utf-8")
    # Carpetas con el mismo prefijo que las permitidas: no cuentan como adentro.
    (raiz / "trabajo2").mkdir()
    (raiz / "Análisis Matemático II 2").mkdir()
    return a


class Reloj:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def reloj() -> Reloj:
    return Reloj()


@pytest.fixture
def estado(trabajo_dir: Path, materia: Path, afuera: Path, reloj: Reloj) -> EstadoPolitica:
    return EstadoPolitica(trabajo_dir, materia, max_intentos=5, tope_s=600, reloj=reloj)


def permite(estado: EstadoPolitica, herramienta: str, **entrada: Any) -> bool:
    ok, motivo = decidir(herramienta, entrada, estado)
    assert isinstance(motivo, str) and motivo
    return ok


def variantes(p: Path) -> list[str]:
    """La misma ruta escrita de varias formas (barras, mayúsculas)."""
    s = str(p)
    return [s, s.replace("\\", "/"), s.upper(), s.lower(), s.replace("\\", "/").swapcase()]


# --- lectura -------------------------------------------------------------------------------------

def test_read_dentro_del_trabajo_y_la_materia(estado, trabajo_dir, materia):
    for ruta in variantes(trabajo_dir / "input" / "timeline.md"):
        assert permite(estado, "Read", file_path=ruta), ruta
    assert permite(estado, "Read", file_path="input/timeline.md")              # relativa al cwd
    assert permite(estado, "Read", file_path="./input/../output/classmer.cls")  # .. que queda adentro
    assert permite(estado, "Read", file_path="plantilla\\ejemplo.tex")
    assert permite(estado, "Read", file_path=str(trabajo_dir))
    for ruta in variantes(materia / "PROMPT-AGENTE.md"):
        assert permite(estado, "Read", file_path=ruta), ruta
    assert permite(estado, "Read", file_path=str(materia / "sub" / ".." / "PROMPT-AGENTE.md"))


def test_read_git_bash(estado, trabajo_dir):
    s = str(trabajo_dir).replace("\\", "/")
    if os.name != "nt" or s[1] != ":":
        pytest.skip("sólo Windows")
    assert permite(estado, "Read", file_path=f"/{s[0].lower()}{s[2:]}/input/timeline.md")
    assert not permite(estado, "Read", file_path="/tmp/secreto.txt")


def test_read_fuera_se_deniega(estado, trabajo_dir, materia, afuera, raiz):
    prohibidas = [
        str(afuera / "secreto.txt"),
        str(raiz),
        "../afuera/secreto.txt",
        "input/../../afuera/secreto.txt",
        str(trabajo_dir / ".." / "afuera" / "secreto.txt"),
        str(trabajo_dir / "output" / ".." / ".." / "afuera" / "secreto.txt").upper(),
        str(raiz / "trabajo2" / "x.txt"),                    # mismo prefijo, otra carpeta
        str(raiz / "Análisis Matemático II 2" / "x.txt"),
        str(materia / ".." / "afuera" / "secreto.txt"),
        "C:/Windows/System32/drivers/etc/hosts",
        "\\\\servidor\\compartida\\x.txt",
        "//servidor/compartida/x.txt",
        "\\\\?\\" + str(trabajo_dir / "input" / "timeline.md"),
        str(trabajo_dir / "input" / "timeline.md") + ":flujo",
        "~/.claude/.credentials.json",
        "", "   ", None, 42, "input/\x00timeline.md",
    ]
    for ruta in prohibidas:
        assert not permite(estado, "Read", file_path=ruta), ruta
    assert not permite(estado, "Read")  # sin file_path


def test_glob_y_grep(estado, trabajo_dir, materia, afuera):
    assert permite(estado, "Glob", pattern="**/*.tex")                      # sin path = cwd
    assert permite(estado, "Glob", pattern="input/fotos/*.jpg", path=str(trabajo_dir))
    assert permite(estado, "Glob", pattern="**/*.pdf", path=str(materia))
    assert permite(estado, "Glob", pattern=str(materia).replace("\\", "/") + "/**/*.md")  # absoluto adentro
    assert permite(estado, "Glob", pattern="*.md", path="input")
    assert not permite(estado, "Glob", pattern="*", path=str(afuera))
    assert not permite(estado, "Glob", pattern="../**/*")
    assert not permite(estado, "Glob", pattern="input/../../afuera/*")
    assert not permite(estado, "Glob", pattern="..\\*")
    assert not permite(estado, "Glob", pattern=str(afuera).replace("\\", "/") + "/*.txt")
    assert not permite(estado, "Glob", pattern="C:/**/*.txt")
    assert not permite(estado, "Glob", pattern="~/*")
    assert not permite(estado, "Glob", pattern="*", path="..")

    assert permite(estado, "Grep", pattern="derivada")
    assert permite(estado, "Grep", pattern="gradiente", path=str(materia), glob="*.md")
    assert permite(estado, "Grep", pattern="x", path=str(trabajo_dir / "input" / "timeline.md"))
    assert not permite(estado, "Grep", pattern="x", path=str(afuera))
    assert not permite(estado, "Grep", pattern="x", path="../afuera")
    assert not permite(estado, "Grep", pattern="x", glob="../../**")


def test_salidas_largas_de_la_sesion_del_cli(estado, raiz):
    """Claude Code guarda las salidas largas (p. ej. la de pdflatex) en <sesión>/tool-results/: se
    pueden leer, pero sólo las de esta sesión y nunca escribir ahí."""
    proyecto = raiz / "home" / ".claude" / "projects" / "C--trabajo"
    resultados = proyecto / "sesion-1" / "tool-results"
    resultados.mkdir(parents=True)
    (resultados / "b6l7.txt").write_text("This is pdfTeX", encoding="utf-8")
    assert not permite(estado, "Grep", pattern="Output written", path=str(resultados / "b6l7.txt"))
    estado.sesion_cli(str(proyecto / "sesion-1.jsonl"))
    estado.sesion_cli(str(proyecto / "otra-sesion.jsonl"))  # la primera queda fija
    assert permite(estado, "Grep", pattern="Output written|^!", path=str(resultados / "b6l7.txt"))
    assert permite(estado, "Read", file_path=str(resultados / "b6l7.txt").upper())
    assert not permite(estado, "Read", file_path=str(proyecto / "sesion-1.jsonl"))       # el transcript no
    assert not permite(estado, "Read", file_path=str(proyecto / "otra-sesion" / "tool-results" / "x.txt"))
    assert not permite(estado, "Write", file_path=str(resultados / "b6l7.txt"))
    estado2 = EstadoPolitica(estado.workdir)
    estado2.sesion_cli("no-es-un-transcript.txt")
    estado2.sesion_cli(None)
    assert estado2.resultados is None


def test_sin_carpeta_de_materia(trabajo_dir, materia):
    estado = EstadoPolitica(trabajo_dir, None)
    assert permite(estado, "Read", file_path="input/timeline.md")
    assert not permite(estado, "Read", file_path=str(materia / "PROMPT-AGENTE.md"))


# --- escritura -----------------------------------------------------------------------------------

def test_write_y_edit_solo_en_output(estado, trabajo_dir, materia, afuera):
    for herramienta in ("Write", "Edit"):
        for ruta in variantes(trabajo_dir / "output" / "apunte.tex"):
            assert permite(estado, herramienta, file_path=ruta), (herramienta, ruta)
        assert permite(estado, herramienta, file_path="output/meta.json")
        assert permite(estado, herramienta, file_path="output\\figuras\\grafico.tikz")
        assert permite(estado, herramienta, file_path="input/../output/apunte.tex")
        prohibidas = [
            "input/timeline.md", "apunte.tex", "plantilla/classmer.cls", "output", str(trabajo_dir / "output"),
            "output/../input/x.tex", "output/../../afuera/x.tex", str(afuera / "x.tex"),
            str(materia / "PROMPT-AGENTE.md"), str(materia / "nuevo.tex"),
            "output/classmer.cls", "OUTPUT/Classmer.CLS", str(trabajo_dir / "output" / "classmer.cls").upper(),
            "output/apunte.tex:flujo", "\\\\servidor\\c\\output\\apunte.tex", "", None,
        ]
        for ruta in prohibidas:
            assert not permite(estado, herramienta, file_path=ruta), (herramienta, ruta)


# --- Bash ----------------------------------------------------------------------------------------

def test_bash_compilaciones_validas(trabajo_dir, materia):
    salida = trabajo_dir / "output"
    validos = [
        COMPILAR,
        f"{COMPILAR} && pdflatex -interaction=nonstopmode -halt-on-error -file-line-error apunte.tex",
        f"{COMPILAR}; pdflatex -interaction=nonstopmode -halt-on-error -file-line-error apunte.tex",
        "cd output && lualatex -interaction=nonstopmode apunte.tex",
        "cd output&&pdflatex apunte.tex",
        "  cd ./output/  &&  pdflatex  -file-line-error -halt-on-error  apunte.tex  ",
        "cd output ; pdflatex -synctex=0 -no-shell-escape apunte.tex",
        "cd OUTPUT && pdflatex apunte.tex",
        f'cd "{salida}" && pdflatex apunte.tex',
        f"cd '{salida}' && pdflatex apunte.tex",
        f"cd {str(salida).replace(chr(92), '/')} && pdflatex apunte.tex",
        "cd input/../output && pdflatex apunte.tex",
    ]
    if os.name == "nt":
        s = str(salida).replace("\\", "/")
        validos.append(f"cd /{s[0].lower()}{s[2:]} && pdflatex apunte.tex")
    for comando in validos:
        estado = EstadoPolitica(trabajo_dir, materia, max_intentos=99)
        ok, motivo = decidir("Bash", {"command": comando, "description": "compilar"}, estado)
        assert ok, (comando, motivo)
        assert estado.intentos == 1


def test_bash_comandos_prohibidos(estado):
    prohibidos = [
        "ls", "ls -la", "dir", "cat input/timeline.md", "pwd", "echo hola", "rm -rf output",
        "pdflatex -interaction=nonstopmode apunte.tex",              # sin cd: arranca en la carpeta de trabajo
        "cd input && pdflatex apunte.tex",
        "cd .. && pdflatex apunte.tex",
        "cd output/.. && pdflatex apunte.tex",
        "cd && pdflatex apunte.tex",
        "cd output plantilla && pdflatex apunte.tex",
        "cd output",
        "cd output && cd output && pdflatex apunte.tex",
        f"{COMPILAR} | tee salida.txt",
        f"{COMPILAR} > salida.txt",
        f"{COMPILAR} 2>&1",
        f"{COMPILAR} &",
        f"{COMPILAR} & rm x",
        f"{COMPILAR} || rm x",
        f"{COMPILAR}\nrm x",
        f"{COMPILAR}\r\nrm x",
        f"{COMPILAR} && rm apunte.log",
        f"{COMPILAR} && cd .. && pdflatex apunte.tex",
        f"{COMPILAR} && {COMPILAR[13:]} && {COMPILAR[13:]}",        # tres pasadas
        f"{COMPILAR};",
        f"{COMPILAR} ;; pdflatex apunte.tex",
        "cd output && pdflatex $(echo apunte.tex)",
        "cd output && pdflatex `whoami`.tex",
        "cd output && pdflatex ${HOME}/apunte.tex",
        "cd output && pdflatex -shell-escape apunte.tex",
        "cd output && pdflatex --shell-escape apunte.tex",
        "cd output && pdflatex -enable-write18 apunte.tex",
        "cd output && pdflatex -output-directory=.. apunte.tex",
        "cd output && pdflatex -interaction=batchmode apunte.tex",
        "cd output && pdflatex -halt-on-error -halt-on-error apunte.tex",
        "cd output && pdflatex otro.tex",
        "cd output && pdflatex ../input/timeline.md",
        "cd output && pdflatex apunte.tex extra",
        "cd output && pdflatex",
        "cd output && pdflatex 'apunte.tex'",
        "cd output && pdflatex \"apunte.tex\"",
        "cd output && pdflatex apunte\\.tex",
        "cd output && xelatex apunte.tex",
        "cd output && latexmk -pdf apunte.tex",
        "cd output && bibtex apunte",
        "cd output && python -c 'print(1)'",
        "cd output && powershell -c ls",
        "cd output && cmd /c dir",
        "cd output && /c/texlive/pdflatex apunte.tex",
        "cd output && pdflatex.exe apunte.tex",
        "cd output && pdflatex apunte.tex # comentario",
        "cd output && pdflatex ~/apunte.tex",
        "cd output && pdflatex *.tex",
        "cd output && pdflatex apunte.tex %TEMP%",
        "cd output && pdflatex apunte.tex !",
        "cd output && (pdflatex apunte.tex)",
        "cd \"output && pdflatex apunte.tex",
        "", "   ",
    ]
    for comando in prohibidos:
        ok, motivo = decidir("Bash", {"command": comando}, estado)
        assert not ok, comando
        assert "compilar" in motivo or "Bash" in motivo, motivo
    assert not permite(estado, "Bash")
    assert not permite(estado, "Bash", command=["pdflatex", "apunte.tex"])
    assert not permite(estado, "Bash", command=COMPILAR, run_in_background=True)
    assert estado.intentos == 0  # lo denegado no cuenta como intento


def test_bash_sin_cd_explica_que_falta(estado):
    ok, motivo = decidir("Bash", {"command": "pdflatex apunte.tex"}, estado)
    assert not ok and "cd output &&" in motivo


def test_tope_de_intentos(estado, reloj):
    for k in range(1, 6):
        ok, motivo = decidir("Bash", {"command": COMPILAR}, estado)
        assert ok and motivo == f"compilación {k} de 5"
        reloj.t += 30
    assert decidir("Bash", {"command": "ls"}, estado)[0] is False   # inválido: no cambia nada
    ok, motivo = decidir("Bash", {"command": COMPILAR}, estado)
    assert not ok
    assert motivo == "tope de 5 intentos de compilación alcanzado: dejá apunte.tex y meta.json como están y terminá"
    assert estado.intentos == 5 and estado.restantes == 0
    # Lo demás sigue andando: puede terminar de ajustar meta.json.
    assert permite(estado, "Edit", file_path="output/meta.json")


def test_tope_de_tiempo_desde_el_primer_intento(estado, reloj):
    reloj.t += 3600                                   # escribir el apunte no cuenta
    assert decidir("Bash", {"command": COMPILAR}, estado)[0]
    reloj.t += 599
    assert decidir("Bash", {"command": COMPILAR}, estado)[0]
    reloj.t += 2
    ok, motivo = decidir("Bash", {"command": COMPILAR}, estado)
    assert not ok and "pasaron más de 10 min" in motivo and "terminá" in motivo
    assert estado.intentos == 2


def test_comando_del_prompt_y_del_server_sin_shell_escape(trabajo_dir):
    # La política acepta -no-shell-escape (no lo exige) y el comando que se le sugiere al agente lo trae.
    assert "-no-shell-escape" in ag.COMANDO_EJEMPLO and ag.COMANDO_EJEMPLO in ag._AYUDA_BASH
    for comando in (ag.COMANDO_EJEMPLO, f"{ag.COMANDO_EJEMPLO} && {ag.COMANDO_EJEMPLO[13:]}",
                    "cd output && " + " ".join(latex.comando("pdflatex", "apunte.tex")),
                    "cd output && " + " ".join(latex.comando("lualatex", "apunte.tex")), COMPILAR):
        assert validar_comando(comando, EstadoPolitica(trabajo_dir)) == (True, ""), comando


def test_bash_no_compila_si_el_tex_lee_archivos_de_afuera(estado, trabajo_dir):
    salida = trabajo_dir / "output"
    tex = salida / "apunte.tex"
    malos = [
        "\\newread\\f\\openin\\f=../../../../.env \\read\\f to\\x \\typeout{\\x}",
        "\\input{C:/Users/usuario/Documents/Classmer-data/.env}",
        "\\input ../../.env",
        "\\includegraphics[width=\\linewidth]{C:/Users/usuario/Documents/Classmer-data/token-qr.png}",
        "\\pizarra{../../../otra/pizarra_01.jpg}{00:01:00}{x}",
        "\\lstinputlisting{/etc/hosts}", "\\directlua{io.open('x')}", "^^5copenin\\f=x",
    ]
    for cuerpo in malos:
        tex.write_text(f"\\documentclass{{classmer}}\n\\begin{{document}}\n{cuerpo}\n\\end{{document}}\n",
                       encoding="utf-8")
        ok, motivo = decidir("Bash", {"command": COMPILAR}, estado)
        assert not ok and "no puede leer archivos" in motivo and "no cuenta como intento" in motivo, cuerpo
    assert estado.intentos == 0 and estado.primer_intento is None  # no se gastó nada

    # Otro archivo de output/ que el .tex incluye también se revisa.
    tex.write_text("\\documentclass{classmer}\n\\begin{document}\n\\input{parte2}\n\\end{document}\n", encoding="utf-8")
    (salida / "parte2.tex").write_text("\\read16 to \\x\n", encoding="utf-8")
    ok, motivo = decidir("Bash", {"command": COMPILAR}, estado)
    assert not ok and "parte2.tex: \\read" in motivo

    # También comentado (un '%' no siempre es comentario: \url{…%20…}); el rechazo tampoco gasta intento.
    (salida / "parte2.tex").write_text("Texto. % aca no va \\openin\n", encoding="utf-8")
    ok, motivo = decidir("Bash", {"command": COMPILAR}, estado)
    assert not ok and "parte2.tex: \\openin" in motivo and "también en comentarios" in motivo, motivo
    assert estado.intentos == 0

    # Lo normal compila: comentarios, links con %20, fotos por nombre, \input relativo, el .log de TeX
    # (trae \read3).
    (salida / "parte2.tex").write_text("Texto. % un comentario\n\\url{https://campus.ejemplo.edu/a%20b.pdf}\n",
                                       encoding="utf-8")
    (salida / "apunte.log").write_text("\\tcb@inputstream=\\read3\n", encoding="utf-8")
    tex.write_text("\\documentclass{classmer}\n\\begin{document}\n\\input{parte2}\n"
                   "\\pizarra{pizarra_01.jpg}{00:01:00}{La pizarra} $f:\\R\\to\\R$, 100\\%.\n"
                   "\\includegraphics[width=3cm]{fotos/pizarra_02.jpg}\n\\end{document}\n", encoding="utf-8")
    ok, motivo = decidir("Bash", {"command": COMPILAR}, estado)
    assert ok and estado.intentos == 1, motivo


def test_otras_herramientas_denegadas(estado):
    for herramienta in ("WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "TodoWrite", "MultiEdit",
                        "mcp__bot__enviar_mensaje", "Skill", "PowerShell", "read", "bash", ""):
        ok, motivo = decidir(herramienta, {"file_path": "output/apunte.tex", "command": COMPILAR}, estado)
        assert not ok and "no está habilitada" in motivo, herramienta
    assert decidir("Read", "no es un dict", estado)[0] is False  # type: ignore[arg-type]
    assert estado.intentos == 0


def test_resolver_ruta(trabajo_dir):
    assert resolver_ruta("input/timeline.md", trabajo_dir) == (trabajo_dir / "input" / "timeline.md").resolve()
    assert resolver_ruta("C:foo:bar", trabajo_dir) is None
    assert resolver_ruta("\\\\?\\C:\\x", trabajo_dir) is None


# --- hook y opciones del SDK ---------------------------------------------------------------------

def trabajo_de(trabajo_dir: Path, materia: Path | None, validar=lambda: [], **extra: Any) -> TrabajoApunte:
    datos = dict(workdir=trabajo_dir, carpeta_materia=materia, prompt_sistema="Sistema de prueba",
                 prompt="Hacé el apunte", modelo="sonnet", motor_latex="pdflatex", max_intentos_compilacion=5,
                 tope_correccion_s=600, timeout_s=60, validar_meta=validar, duracion_ms=600_000)
    datos.update(extra)
    return TrabajoApunte(**datos)


def test_opciones_y_hook(trabajo_dir, materia, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-falsa")
    estado = EstadoPolitica(trabajo_dir, materia)
    registro = Registro(None)
    op = AgenteClaude(None)._opciones(trabajo_de(trabajo_dir, materia), estado, registro)
    assert op.tools == ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
    assert {"WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"} <= set(op.disallowed_tools)
    assert op.allowed_tools == [] and op.permission_mode == "dontAsk"
    assert op.setting_sources == [] and op.skills == [] and op.strict_mcp_config is True
    assert op.cwd == str(trabajo_dir.resolve()) and op.add_dirs == [str(materia.resolve())]
    assert op.model == "sonnet" and op.max_turns == ag.MAX_TURNOS and op.verbatim_prompts is True
    assert op.env["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"] == "1"
    assert "ANTHROPIC_API_KEY" not in op.env and op.system_prompt == "Sistema de prueba"
    # Una foto leída con Read vuelve en base64 dentro de un mensaje: el tope del SDK (1 MiB por
    # defecto) rompía con fotos reales del S23. Tiene que ser 1 GiB.
    assert op.max_buffer_size == ag.MAX_BUFFER_SDK == 1024 ** 3

    hook = op.hooks["PreToolUse"][0].hooks[0]
    assert op.hooks["PreToolUse"][0].matcher is None  # todas las herramientas pasan por el hook

    def llamar(nombre: str, entrada: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(hook({"hook_event_name": "PreToolUse", "tool_name": nombre, "tool_input": entrada,
                                 "tool_use_id": "t1", "session_id": "s", "transcript_path": "", "cwd": ""},
                                "t1", {"signal": None}))["hookSpecificOutput"]

    r = llamar("Read", {"file_path": str(trabajo_dir / "input" / "timeline.md")})
    assert r["hookEventName"] == "PreToolUse" and r["permissionDecision"] == "allow"
    r = llamar("Bash", {"command": "ls"})
    assert r["permissionDecision"] == "deny" and "compilar" in r["permissionDecisionReason"]
    r = llamar("WebFetch", {"url": "https://example.com"})
    assert r["permissionDecision"] == "deny"
    assert registro.usos == {"Read": 1, "Bash": 1, "WebFetch": 1} and len(registro.denegadas) == 2
    # El hook toma de cada evento la carpeta tool-results de la sesión del CLI.
    transcript = trabajo_dir.parent / "proyectos" / "sesion-9.jsonl"
    asyncio.run(hook({"tool_name": "Read", "tool_input": {"file_path": "input/timeline.md"},
                      "transcript_path": str(transcript)}, "t2", {"signal": None}))
    assert estado.resultados == (trabajo_dir.parent / "proyectos" / "sesion-9" / "tool-results").resolve()


def test_system_prompt_largo_va_a_un_archivo(trabajo_dir):
    largo = "x" * (ag.LARGO_PROMPT_ARGV + 10)
    op = AgenteClaude(None)._opciones(trabajo_de(trabajo_dir, None, prompt_sistema=largo),
                                      EstadoPolitica(trabajo_dir), Registro(None))
    assert op.system_prompt["type"] == "file"
    ruta = Path(op.system_prompt["path"])
    assert ruta.read_text(encoding="utf-8") == largo and trabajo_dir.resolve() not in ruta.parents


def test_espera_cuota():
    ahora = 1_800_000_000.0
    assert espera_cuota("Claude AI usage limit reached|1800003600", None, ahora) == 3660
    assert espera_cuota("sin dato", ahora + 120, ahora) == 180
    assert espera_cuota("sin dato", None, ahora) == ag.ESPERA_CUOTA_S
    assert espera_cuota("x", ahora + 10 * 24 * 3600, ahora) == ag.ESPERA_CUOTA_MAX_S


def test_resumen_compilacion():
    assert resumen_compilacion("Output written on apunte.pdf (7 pages, 1234 bytes).", False) == "ok, 7 páginas"
    assert resumen_compilacion("./apunte.tex:12: Undefined control sequence.\nl.12 \\foo", True) == \
        "falló: ./apunte.tex:12: Undefined control sequence."
    assert resumen_compilacion("algo raro\nExit code 1", True) == "falló: Exit code 1"


def test_resultado_de_la_compilacion_sale_del_log(trabajo_dir):
    salida = trabajo_dir / "output"
    desde = time.time()
    assert ag.resultado_en_disco(salida, desde) is None                      # todavía no hay log
    (salida / "apunte.log").write_text("bla\nOutput written on apunte.pdf (2 pages, 99 bytes).\n", encoding="utf-8")
    (salida / "apunte.pdf").write_bytes(b"%PDF")
    assert ag.resultado_en_disco(salida, desde) == "ok, 2 páginas"
    assert ag.resultado_en_disco(salida, desde + 3600) is None               # log viejo: no es de este intento
    (salida / "apunte.log").write_text("./apunte.tex:40: Missing $ inserted.\n", encoding="utf-8")
    assert ag.resultado_en_disco(salida, desde) == "falló: ./apunte.tex:40: Missing $ inserted."

    # El registro usa el log aunque la salida de la herramienta venga recortada.
    registro = Registro(None, salida)
    registro.herramienta("Bash", {"command": COMPILAR}, True, "compilación 1 de 5", "t1")
    (salida / "apunte.log").write_text("Output written on apunte.pdf (3 pages).\n", encoding="utf-8")
    (salida / "apunte.pdf").write_bytes(b"%PDF")
    registro.resultado_herramienta("t1", "Output too large. Full output saved to C:/.../tool-results/x.txt", False)
    assert registro.compilaciones[0]["resultado"] == "ok, 3 páginas"


# --- runner con un cliente falso -----------------------------------------------------------------

def resultado(turnos: int = 4, costo: float = 0.25, error: bool = False, texto: str = "Listo.",
              subtipo: str = "success", **extra: Any) -> ResultMessage:
    return ResultMessage(subtype=subtipo, duration_ms=10, duration_api_ms=8, is_error=error, num_turns=turnos,
                         session_id="s", total_cost_usd=costo, result=texto, **extra)


class ClienteFalso:
    """Imita ClaudeSDKClient: cada query() corre el siguiente paso del guion (que usa el hook real)."""

    def __init__(self, opciones: Any, guion: list[Any]):
        self.opciones = opciones
        self.guion = guion
        self.consultas: list[str] = []
        self.entorno: dict[str, str] = {}
        self.interrumpido = asyncio.Event()
        self.desconectado = False

    async def connect(self) -> None:
        self.entorno = dict(os.environ)

    async def query(self, prompt: str) -> None:
        self.consultas.append(prompt)

    async def herramienta(self, nombre: str, entrada: dict[str, Any]) -> bool:
        hook = self.opciones.hooks["PreToolUse"][0].hooks[0]
        r = await hook({"tool_name": nombre, "tool_input": entrada}, f"t{len(self.consultas)}{nombre}", {})
        return r["hookSpecificOutput"]["permissionDecision"] == "allow"

    async def receive_response(self):
        paso = self.guion[len(self.consultas) - 1]
        async for m in paso(self):
            yield m

    async def interrupt(self) -> None:
        self.interrumpido.set()

    async def disconnect(self) -> None:
        self.desconectado = True


def escribir_salida(trabajo_dir: Path, meta: Any, *, pdf: bool = True) -> None:
    """apunte.tex + meta.json y, si `pdf`, el apunte.pdf de una compilación que anduvo."""
    salida = trabajo_dir / "output"
    (salida / "apunte.tex").write_text("\\documentclass{classmer}\n", encoding="utf-8")
    (salida / "meta.json").write_text(meta if isinstance(meta, str) else json.dumps(meta), encoding="utf-8")
    if pdf:
        (salida / "apunte.pdf").write_bytes(b"%PDF")
        t = (salida / "apunte.tex").stat().st_mtime
        os.utime(salida / "apunte.pdf", (t + 1, t + 1))
    else:
        (salida / "apunte.pdf").unlink(missing_ok=True)


def correr(trabajo: TrabajoApunte, guion: list[Any], cfg: Any = None, **kw: Any) -> tuple[Any, ClienteFalso]:
    creados: list[ClienteFalso] = []

    def fabrica(opciones: Any) -> ClienteFalso:
        creados.append(ClienteFalso(opciones, guion))
        return creados[0]

    r = AgenteClaude(cfg, cliente=fabrica, **kw).ejecutar(trabajo)
    return r, creados[0]


def test_runner_camino_feliz(trabajo_dir, materia, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-falsa")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sesion-padre")

    async def paso(c: ClienteFalso):
        yield SystemMessage(subtype="init", data={"model": "claude-sonnet-x", "permissionMode": "dontAsk",
                                                  "tools": ag.HERRAMIENTAS, "apiKeySource": "none"})
        assert await c.herramienta("Read", {"file_path": str(trabajo_dir / "input" / "timeline.md")})
        assert not await c.herramienta("Bash", {"command": "ls"})
        assert await c.herramienta("Write", {"file_path": "output/apunte.tex", "content": "x"})
        escribir_salida(trabajo_dir, {"ok": True})
        assert await c.herramienta("Bash", {"command": COMPILAR})
        yield AssistantMessage(content=[TextBlock("Compilo."), ToolUseBlock("t1Bash", "Bash", {"command": COMPILAR})],
                               model="claude-sonnet-x")
        yield UserMessage(content=[ToolResultBlock("t1Bash", "Output written on apunte.pdf (3 pages).", False)])
        yield resultado(turnos=6, costo=0.31)

    r, c = correr(trabajo_de(trabajo_dir, materia), [paso])
    assert r.ok and r.error is None
    assert r.modelo == "claude-sonnet-x" and r.costo_usd == 0.31 and r.turnos == 6 and r.intentos_compilacion == 1
    assert c.consultas == ["Hacé el apunte"] and c.desconectado
    assert "ANTHROPIC_API_KEY" not in c.entorno and "CLAUDE_CODE_SESSION_ID" not in c.entorno
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-falsa"  # se restaura después de lanzar
    assert "Compilaciones del agente: 1 de 5" in r.log and "1. ok, 3 páginas" in r.log
    assert "Read ×1" in r.log and "denegadas: 1" in r.log and "clave de API" in r.log
    assert "sk-ant-falsa" not in r.log
    log = (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")
    assert "DENEGADO Bash `ls`" in log and "fin." in log


def test_runner_seguimiento_por_meta_invalido(trabajo_dir):
    meta = trabajo_dir / "output" / "meta.json"

    def validar() -> list[str]:
        return [] if meta.is_file() and "bien" in meta.read_text(encoding="utf-8") else ["meta.json: falta el campo 'items'"]

    async def primero(c: ClienteFalso):
        escribir_salida(trabajo_dir, "{\"mal\": 1}")
        yield resultado(turnos=10, costo=0.5)

    async def segundo(c: ClienteFalso):
        assert "falta el campo 'items'" in c.consultas[-1] and "te quedan 5 intentos" in c.consultas[-1]
        assert await c.herramienta("Edit", {"file_path": str(meta)})
        escribir_salida(trabajo_dir, "{\"bien\": 1}")
        yield resultado(turnos=2, costo=0.6)

    r, c = correr(trabajo_de(trabajo_dir, None, validar=validar), [primero, segundo])
    assert r.ok and len(c.consultas) == 2
    assert r.turnos == 12 and r.costo_usd == 0.6  # el costo del CLI es acumulado
    assert "Seguimientos por la salida: 1" in r.log


def test_runner_seguimientos_tienen_tope(trabajo_dir):
    async def paso(c: ClienteFalso):
        yield resultado()

    r, c = correr(trabajo_de(trabajo_dir, None, validar=lambda: ["no existe output/apunte.tex"]), [paso] * 5)
    assert not r.ok and len(c.consultas) == 1 + ag.MAX_SEGUIMIENTOS
    assert "no existe output/apunte.tex" in r.log


def test_runner_cuota_lanza_cuota_agotada(trabajo_dir):
    vuelve = int(time.time()) + 7200

    async def paso(c: ClienteFalso):
        yield RateLimitEvent(RateLimitInfo(status="rejected", resets_at=vuelve, rate_limit_type="five_hour"), "u", "s")
        yield resultado(error=True, texto=f"Claude AI usage limit reached|{vuelve}")

    with pytest.raises(CuotaAgotada) as e:
        correr(trabajo_de(trabajo_dir, None), [paso])
    assert 7000 < e.value.espera_s <= 7300
    assert "cuota" in str(e.value)
    # Es el cupo del plan Max: la etapa cambia a la API si el modo lo permite (y se recuerda hasta resets_at).
    assert isinstance(e.value, SinCreditos) and (e.value.medio, e.value.estado) == ("max", "sin_cupo")
    assert e.value.hasta == vuelve


def test_runner_rate_limit_del_modelo(trabajo_dir):
    async def paso(c: ClienteFalso):
        yield AssistantMessage(content=[TextBlock("API Error")], model="m", error="rate_limit")
        yield resultado(error=True, texto="API Error: 429")

    with pytest.raises(CuotaAgotada) as e:
        correr(trabajo_de(trabajo_dir, None), [paso])
    assert e.value.espera_s == ag.ESPERA_CUOTA_S
    assert not isinstance(e.value, SinCreditos)  # un 429 suelto es pasajero: no cambia de medio


API_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'


@pytest.mark.parametrize("mensajes", [
    # el modelo marca server_error y el resultado trae el status
    [AssistantMessage(content=[TextBlock(API_500)], model="m", error="server_error"),
     resultado(error=True, texto=API_500, api_error_status=500)],
    # sólo el status (5xx) en el resultado
    [resultado(error=True, texto="API Error: 503 upstream", api_error_status=503)],
    # sin red: el CLI ya reintentó y se rindió
    [resultado(error=True, texto="API Error: Connection error.")],
    [resultado(error=True, texto="API Error: Request timed out.")],
], ids=["server_error", "status_503", "connection_error", "timeout"])
def test_runner_api_caida_es_sobrecarga(trabajo_dir, mensajes):
    async def paso(c: ClienteFalso):
        escribir_salida(trabajo_dir, {"a medias": True}, pdf=False)  # lo que quedó no se compila ni publica
        for m in mensajes:
            yield m

    with pytest.raises(CuotaAgotada) as e:
        correr(trabajo_de(trabajo_dir, None), [paso])
    assert e.value.espera_s == ag.ESPERA_SOBRECARGA_S and "sobrecargada" in str(e.value)
    assert not isinstance(e.value, SinCreditos)


def test_clasificar_falla_de_red_y_5xx():
    for texto in ("API Error: Connection error.", "read ECONNRESET", "connect ETIMEDOUT 1.2.3.4:443",
                  "API Error: Request timed out.", API_500, "Overloaded", "API Error: 529"):
        assert clasificar_falla(texto) == ("sobrecarga", ag.ESPERA_SOBRECARGA_S), texto
    assert clasificar_falla("apunte.tex:12: Undefined control sequence") is None
    assert clasificar_falla("error_max_turns: 80") is None


def test_runner_seguimiento_si_la_ultima_compilacion_fallo(trabajo_dir):
    async def primero(c: ClienteFalso):
        escribir_salida(trabajo_dir, {"ok": True}, pdf=False)
        assert await c.herramienta("Bash", {"command": COMPILAR})  # falló: no dejó apunte.pdf
        yield resultado(texto="Listo, el apunte está.")

    async def segundo(c: ClienteFalso):
        assert "no dejó un apunte.pdf al día" in c.consultas[-1] and "output/apunte.log" in c.consultas[-1]
        assert "te quedan 4 intentos" in c.consultas[-1]
        assert await c.herramienta("Bash", {"command": COMPILAR})
        escribir_salida(trabajo_dir, {"ok": True})
        yield resultado()

    r, c = correr(trabajo_de(trabajo_dir, None), [primero, segundo])
    assert r.ok and len(c.consultas) == 2 and r.intentos_compilacion == 2
    assert "Seguimientos por la salida: 1" in r.log


def test_runner_pdf_viejo_tambien_pide_compilar(trabajo_dir):
    async def primero(c: ClienteFalso):
        escribir_salida(trabajo_dir, {"ok": True})
        tex = trabajo_dir / "output" / "apunte.tex"
        t = (trabajo_dir / "output" / "apunte.pdf").stat().st_mtime
        os.utime(tex, (t + 10, t + 10))  # editó el .tex después de la última compilación buena
        yield resultado()

    async def segundo(c: ClienteFalso):
        assert "apunte.pdf al día" in c.consultas[-1]
        escribir_salida(trabajo_dir, {"ok": True})
        yield resultado()

    r, c = correr(trabajo_de(trabajo_dir, None), [primero, segundo])
    assert r.ok and len(c.consultas) == 2


@pytest.mark.parametrize("extra", [{"max_intentos_compilacion": 1}, {"tope_correccion_s": -1}],
                         ids=["sin_intentos", "tope_vencido"])
def test_runner_sin_margen_no_pide_compilar(trabajo_dir, extra):
    async def paso(c: ClienteFalso):
        escribir_salida(trabajo_dir, {"ok": True}, pdf=False)
        assert await c.herramienta("Bash", {"command": COMPILAR})
        yield resultado()

    r, c = correr(trabajo_de(trabajo_dir, None, **extra), [paso])
    assert len(c.consultas) == 1 and "Seguimientos por la salida: 0" in r.log


def test_pdf_atrasado_y_puede_compilar(trabajo_dir, reloj):
    estado = EstadoPolitica(trabajo_dir, None, max_intentos=2, tope_s=600, reloj=reloj)
    salida = trabajo_dir / "output"
    assert not estado.pdf_atrasado()                      # todavía no hay apunte.tex
    (salida / "apunte.tex").write_text("x", encoding="utf-8")
    assert estado.pdf_atrasado() and estado.puede_compilar
    (salida / "apunte.pdf").write_bytes(b"%PDF")
    t = (salida / "apunte.tex").stat().st_mtime_ns
    os.utime(salida / "apunte.pdf", ns=(t, t))
    assert not estado.pdf_atrasado()                      # mismo instante: al día
    os.utime(salida / "apunte.pdf", ns=(t - 5 * 10**9, t - 5 * 10**9))
    assert estado.pdf_atrasado()
    assert decidir("Bash", {"command": COMPILAR}, estado)[0]
    reloj.t += 600
    assert estado.puede_compilar
    reloj.t += 1
    assert not estado.puede_compilar                      # venció el tope desde la primera compilación
    estado.primer_intento, estado.intentos = None, 2
    assert not estado.puede_compilar                      # sin intentos


def test_runner_error_comun_no_es_cuota(trabajo_dir):
    async def paso(c: ClienteFalso):
        yield resultado(error=True, subtipo="error_max_turns", texto="")

    r, _ = correr(trabajo_de(trabajo_dir, None), [paso])
    assert not r.ok and "error_max_turns" in r.error


def test_runner_timeout_interrumpe(trabajo_dir):
    async def paso(c: ClienteFalso):
        await c.interrumpido.wait()
        yield resultado(error=True, subtipo="error_during_execution", texto="interrumpido")

    t0 = time.monotonic()
    r, c = correr(trabajo_de(trabajo_dir, None, timeout_s=0.2), [paso])
    assert time.monotonic() - t0 < 10
    assert not r.ok and "tope" in r.error and c.interrumpido.is_set() and c.desconectado


def test_runner_falla_al_conectar(trabajo_dir):
    class NoConecta(ClienteFalso):
        async def connect(self) -> None:
            raise RuntimeError("no encuentro claude.exe")

    r = AgenteClaude(None, cliente=lambda op: NoConecta(op, [])).ejecutar(trabajo_de(trabajo_dir, None))
    assert not r.ok and "no encuentro claude.exe" in r.error


def test_runner_sin_login_es_cuota(trabajo_dir):
    class SinLogin(ClienteFalso):
        async def connect(self) -> None:
            raise RuntimeError("Invalid API key · Please run /login")

    with pytest.raises(CuotaAgotada) as e:
        AgenteClaude(None, cliente=lambda op: SinLogin(op, [])).ejecutar(trabajo_de(trabajo_dir, None))
    assert "login" in str(e.value)
    assert isinstance(e.value, SinCreditos) and (e.value.medio, e.value.estado) == ("max", "sin_login")


def test_error_de_buffer_del_sdk_queda_legible():
    from claude_agent_sdk import CLIJSONDecodeError

    # Como lo arma el SDK (subprocess_cli.py): el texto va en `line`, el detalle en original_error.
    e = CLIJSONDecodeError("JSON message exceeded maximum buffer size of 1048576 bytes",
                           ValueError("Buffer size 1400000 exceeds limit 1048576"))
    texto = ag.describir_error_sdk(e)
    assert texto.startswith("un mensaje del agente superó el tope del SDK")
    assert "foto" in texto and "CLIJSONDecodeError" in texto
    assert ag.describir_error_sdk(RuntimeError("otra cosa")) == "RuntimeError: otra cosa"


# --- esfuerzo y facturación (API key o plan Max) -------------------------------------------------

CLAVE = "sk-ant-falsa-123"
DEL_ENTORNO = "sk-ant-del-entorno-456"


@pytest.fixture
def cfg_con_clave(home: Path):
    """Config con ANTHROPIC_API_KEY en el .env de la carpeta de datos (de prueba, nunca la real)."""
    with open(home / ".env", "a", encoding="utf-8") as f:
        f.write(f"ANTHROPIC_API_KEY={CLAVE}\n")
    cfg = cargar_config(home)
    assert cfg.anthropic_api_key == CLAVE and CLAVE not in repr(cfg)
    return cfg


def comando_del_sdk(op: Any) -> list[str]:
    """La línea de comando con la que el SDK lanzaría el CLI (sin lanzar nada)."""
    from dataclasses import replace

    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    return SubprocessCLITransport(prompt="x", options=replace(op, cli_path="claude"))._build_command()


def entorno_del_cli(c: ClienteFalso) -> dict[str, str]:
    """El entorno que arma el SDK para el CLI: el del proceso al conectar (sin CLAUDECODE) + opciones.env."""
    return {**{k: v for k, v in c.entorno.items() if k != "CLAUDECODE"}, **c.opciones.env}


def init(fuente: str = "none", modelo: str = "claude-opus-5-5") -> SystemMessage:
    return SystemMessage(subtype="init", data={"model": modelo, "permissionMode": "dontAsk",
                                               "tools": ag.HERRAMIENTAS, "apiKeySource": fuente})


def paso_ok(trabajo_dir: Path, fuente: str = "none"):
    async def paso(c: ClienteFalso):
        yield init(fuente)
        escribir_salida(trabajo_dir, {"ok": True})
        yield resultado(turnos=3, costo=0.42)
    return paso


@pytest.mark.parametrize("esfuerzo", ["low", "medium", "high", "xhigh", "max"])
def test_esfuerzo_llega_al_comando_del_cli(trabajo_dir, esfuerzo):
    t = trabajo_de(trabajo_dir, None, modelo="claude-opus-5-5", esfuerzo=esfuerzo)
    op = AgenteClaude(None)._opciones(t, EstadoPolitica(trabajo_dir), Registro(None))
    assert op.effort == esfuerzo
    cmd = comando_del_sdk(op)
    assert cmd[cmd.index("--effort") + 1] == esfuerzo and cmd[cmd.index("--model") + 1] == "claude-opus-5-5"


def test_sin_esfuerzo_no_se_pasa(trabajo_dir):
    op = AgenteClaude(None)._opciones(trabajo_de(trabajo_dir, None), EstadoPolitica(trabajo_dir), Registro(None))
    assert op.effort is None and "--effort" not in comando_del_sdk(op)


def test_esfuerzo_y_facturacion_en_agente_log(trabajo_dir):
    r, _ = correr(trabajo_de(trabajo_dir, None, esfuerzo="xhigh"), [paso_ok(trabajo_dir)])
    log = (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")
    assert "inicia: modelo sonnet, esfuerzo xhigh, facturación max," in log
    assert r.log.startswith("Agente: claude-opus-5-5 · esfuerzo xhigh · facturación plan Max ·")
    assert "costo US$ 0.42 equivalente API (plan Max" in r.log and "AVISO" not in log


def test_trabajo_por_defecto_es_plan_max(trabajo_dir):
    t = trabajo_de(trabajo_dir, None)
    assert t.facturacion == "max" and t.esfuerzo is None


def test_modo_max_la_clave_nunca_llega_al_cli(trabajo_dir, cfg_con_clave, monkeypatch):
    """Con el plan Max, ni la clave del .env ni la del entorno del proceso llegan al CLI."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", DEL_ENTORNO)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-del-entorno")
    r, c = correr(trabajo_de(trabajo_dir, None, esfuerzo="high", facturacion="max"), [paso_ok(trabajo_dir)],
                  cfg=cfg_con_clave)
    assert r.ok
    assert "ANTHROPIC_API_KEY" not in c.opciones.env and "ANTHROPIC_AUTH_TOKEN" not in c.opciones.env
    assert "ANTHROPIC_API_KEY" not in c.entorno and "ANTHROPIC_AUTH_TOKEN" not in c.entorno
    cli = entorno_del_cli(c)
    assert "ANTHROPIC_API_KEY" not in cli and "ANTHROPIC_AUTH_TOKEN" not in cli
    assert not any(CLAVE in v or DEL_ENTORNO in v for v in cli.values())
    assert not any(CLAVE in x for x in comando_del_sdk(c.opciones))
    # El entorno del proceso vuelve a quedar como estaba (no es nuestro) y nunca recibe la del .env.
    assert os.environ["ANTHROPIC_API_KEY"] == DEL_ENTORNO
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "token-del-entorno"
    log = (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")
    assert "para usar el login de Claude Code" in log
    assert CLAVE not in log + r.log and DEL_ENTORNO not in log + r.log


def test_modo_api_la_clave_va_solo_en_la_corrida(trabajo_dir, cfg_con_clave, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    vistas: list[str | None] = []
    creados: list[ClienteFalso] = []

    class Espia(ClienteFalso):
        async def connect(self) -> None:
            vistas.append(os.environ.get("ANTHROPIC_API_KEY"))
            await super().connect()

    def fabrica(op: Any) -> ClienteFalso:
        creados.append(Espia(op, [paso_ok(trabajo_dir, "ANTHROPIC_API_KEY")]))
        return creados[-1]

    t = trabajo_de(trabajo_dir, None, esfuerzo="max", facturacion="api")
    r = AgenteClaude(cfg_con_clave, cliente=fabrica).ejecutar(t)
    (c,) = creados
    assert r.ok and vistas == [None]
    assert c.opciones.env["ANTHROPIC_API_KEY"] == CLAVE       # sólo en las opciones de esta corrida
    assert entorno_del_cli(c)["ANTHROPIC_API_KEY"] == CLAVE
    assert "ANTHROPIC_API_KEY" not in c.entorno and "ANTHROPIC_API_KEY" not in os.environ  # ni antes ni después
    assert not any(CLAVE in x for x in comando_del_sdk(c.opciones))                       # ni en la línea de comando
    assert c.opciones.effort == "max"
    log = (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")
    assert "esfuerzo max, facturación api" in log and "AVISO" not in log
    assert "costo US$ 0.42 (API)" in r.log
    assert CLAVE not in log and CLAVE not in r.log


def test_modo_api_con_otra_clave_en_el_entorno(trabajo_dir, cfg_con_clave, monkeypatch):
    """La clave del entorno del proceso no se usa: va la del .env, y el entorno se restaura."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", DEL_ENTORNO)
    r, c = correr(trabajo_de(trabajo_dir, None, facturacion="api"), [paso_ok(trabajo_dir, "ANTHROPIC_API_KEY")],
                  cfg=cfg_con_clave)
    assert r.ok and c.opciones.env["ANTHROPIC_API_KEY"] == CLAVE and "ANTHROPIC_API_KEY" not in c.entorno
    assert entorno_del_cli(c)["ANTHROPIC_API_KEY"] == CLAVE
    assert os.environ["ANTHROPIC_API_KEY"] == DEL_ENTORNO
    assert "Classmer-data\\.env" in (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")


def test_modo_api_sin_clave_no_lanza_el_cli(trabajo_dir):
    creados: list[Any] = []
    with pytest.raises(SinCreditos) as e:
        AgenteClaude(None, cliente=lambda op: creados.append(op)).ejecutar(
            trabajo_de(trabajo_dir, None, facturacion="api"))
    assert (e.value.medio, e.value.estado) == ("api", "clave_invalida") and "ANTHROPIC_API_KEY" in str(e.value)
    assert creados == []


def test_clave_explicita_y_rara_tampoco_queda_en_los_logs(trabajo_dir):
    clave = "clave-rara-XYZ987"  # no tiene el formato sk-ant-…: se oculta igual porque es la de la corrida

    async def paso(c: ClienteFalso):
        yield init("ANTHROPIC_API_KEY")
        yield resultado(error=True, texto=f"API Error: 401 invalid x-api-key {clave}", api_error_status=401)

    with pytest.raises(SinCreditos) as e:
        correr(trabajo_de(trabajo_dir, None, facturacion="api"), [paso], clave_api=clave)
    assert (e.value.medio, e.value.estado) == ("api", "clave_invalida")
    assert clave not in str(e.value) and clave not in (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")


@pytest.mark.parametrize(("facturacion", "fuente", "aviso"), [
    ("max", "ANTHROPIC_API_KEY", "el CLI usa una API key"),
    ("max", "none", None),
    ("api", "none", "el CLI no la usa"),
    ("api", "ANTHROPIC_API_KEY", None),
])
def test_aviso_si_el_cli_no_factura_como_se_pidio(trabajo_dir, cfg_con_clave, facturacion, fuente, aviso):
    r, _ = correr(trabajo_de(trabajo_dir, None, facturacion=facturacion), [paso_ok(trabajo_dir, fuente)],
                  cfg=cfg_con_clave)
    log = (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")
    if aviso:
        assert "AVISO: la corrida es con" in log and aviso in log and "Avisos:" in r.log
    else:
        assert "AVISO" not in log and "Avisos:" not in r.log


CREDITO_400 = ('API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"Your credit '
               'balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or '
               'purchase credits."}}')
CLAVE_401 = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'
RATE_429 = ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"This request would '
            'exceed the rate limit for your organization of 50 requests per minute"}}')


@pytest.mark.parametrize(("medio", "texto", "extra", "tipo"), [
    # plan Max: cupo agotado o sin login → cambia de medio
    ("max", "Claude AI usage limit reached|1800003600", {}, "sin_cupo"),
    ("max", "You've hit your limit · resets 5pm (America/Buenos_Aires)", {}, "sin_cupo"),
    ("max", "You're out of extra usage · resets 3am", {}, "sin_cupo"),
    ("max", "Weekly limit reached ∙ resets Oct 20", {}, "sin_cupo"),
    ("max", "5-hour limit reached ∙ resets 3pm", {}, "sin_cupo"),
    ("max", "API Error", {"error": "billing_error"}, "sin_cupo"),
    ("max", "API Error: 429", {"error": "rate_limit", "rechazo": True}, "sin_cupo"),
    ("max", "Invalid API key · Please run /login", {}, "sin_login"),
    ("max", "Not logged in · Please run /login", {}, "sin_login"),
    ("max", "API Error", {"error": "authentication_failed"}, "sin_login"),
    # API: sin créditos o clave rechazada → cambia de medio
    ("api", CREDITO_400, {}, "sin_creditos"),
    ("api", "Credit balance is too low", {}, "sin_creditos"),
    ("api", "API Error", {"error": "billing_error"}, "sin_creditos"),
    ("api", "error", {"status": 402}, "sin_creditos"),
    ("api", "You have reached your specified API usage limits. You will regain access on 2026-11-01.", {},
     "sin_creditos"),
    ("api", CLAVE_401, {"status": 401}, "clave_invalida"),
    ("api", "Invalid API key · Please run /login", {}, "clave_invalida"),
    ("api", "API Error", {"error": "authentication_failed"}, "clave_invalida"),
    # pasajeros: no cambian de medio
    ("api", RATE_429, {"status": 429}, "rate_limit"),
    ("api", "API Error: 429", {"error": "rate_limit", "rechazo": True}, "rate_limit"),
    ("max", RATE_429, {"status": 429}, "rate_limit"),
    ("max", "API Error", {"error": "rate_limit"}, "rate_limit"),
    ("max", API_500, {"error": "server_error"}, "sobrecarga"),
    ("api", API_500, {"status": 500}, "sobrecarga"),
    ("api", "API Error: 529 Overloaded", {}, "sobrecarga"),
    ("max", "API Error: Connection error.", {}, "sobrecarga"),
    ("api", "API Error: Request timed out.", {}, "sobrecarga"),
    ("max", "error", {"status": 503}, "sobrecarga"),
    # errores comunes del agente
    ("max", "apunte.tex:12: Undefined control sequence", {}, None),
    ("api", "error_max_turns: 80", {}, None),
    ("api", "API Error", {"error": "invalid_request"}, None),
])
def test_clasificar_error_segun_el_medio(medio, texto, extra, tipo):
    assert clasificar_error(texto, medio, **extra) == tipo


@pytest.mark.parametrize(("tipo", "medio", "estado"), [
    ("sin_cupo", "max", "sin_cupo"), ("sin_login", "max", "sin_login"),
    ("sin_creditos", "api", "sin_creditos"), ("clave_invalida", "api", "clave_invalida"),
])
def test_excepcion_de_facturacion(tipo, medio, estado):
    e = ag.excepcion_falla(tipo, f"algo {CLAVE}", medio)
    assert isinstance(e, SinCreditos) and isinstance(e, CuotaAgotada)
    assert (e.medio, e.estado) == (medio, estado) and CLAVE not in str(e)


def test_excepcion_pasajera_no_es_sin_creditos():
    for tipo, espera in (("rate_limit", ag.ESPERA_CUOTA_S), ("sobrecarga", ag.ESPERA_SOBRECARGA_S)):
        e = ag.excepcion_falla(tipo, "API Error: 429", "api")
        assert type(e) is CuotaAgotada and e.espera_s == espera


def test_sin_cupo_toma_el_reset_del_texto():
    ahora = 1_800_000_000.0
    e = ag.excepcion_falla("sin_cupo", "Claude AI usage limit reached|1800003600", "max", ahora=ahora)
    assert e.hasta == 1_800_003_600 and e.espera_s == 3660 and "cuota" in str(e)
    assert ag.excepcion_falla("sin_cupo", "limit reached|1700000000", "max", ahora=ahora).hasta is None  # ya pasó


def test_runner_api_sin_creditos(trabajo_dir, cfg_con_clave):
    async def paso(c: ClienteFalso):
        yield init("ANTHROPIC_API_KEY")
        escribir_salida(trabajo_dir, {"a medias": True}, pdf=False)
        yield AssistantMessage(content=[TextBlock("Credit balance is too low")], model="m", error="billing_error")
        yield resultado(error=True, texto=CREDITO_400, api_error_status=400)

    with pytest.raises(SinCreditos) as e:
        correr(trabajo_de(trabajo_dir, None, facturacion="api"), [paso], cfg=cfg_con_clave)
    assert (e.value.medio, e.value.estado) == ("api", "sin_creditos") and "créditos" in str(e.value)
    log = (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")
    assert "API sin créditos" in log and "otro medio" in log


def test_runner_api_clave_invalida(trabajo_dir, cfg_con_clave):
    async def paso(c: ClienteFalso):
        yield AssistantMessage(content=[TextBlock("Invalid API key · Fix external API key")], model="<synthetic>",
                               error="authentication_failed")
        yield resultado(error=True, texto=CLAVE_401, api_error_status=401)

    with pytest.raises(SinCreditos) as e:
        correr(trabajo_de(trabajo_dir, None, facturacion="api"), [paso], cfg=cfg_con_clave)
    assert (e.value.medio, e.value.estado) == ("api", "clave_invalida")


def test_runner_api_429_es_pasajero(trabajo_dir, cfg_con_clave):
    async def paso(c: ClienteFalso):
        yield AssistantMessage(content=[TextBlock(RATE_429)], model="m", error="rate_limit")
        yield resultado(error=True, texto=RATE_429, api_error_status=429)

    with pytest.raises(CuotaAgotada) as e:
        correr(trabajo_de(trabajo_dir, None, facturacion="api"), [paso], cfg=cfg_con_clave)
    assert not isinstance(e.value, SinCreditos) and e.value.espera_s == ag.ESPERA_CUOTA_S
    assert "rate limit" in str(e.value)


def test_runner_max_rechazo_del_cupo_con_429(trabajo_dir):
    """Un RateLimitEvent 'rejected' del plan + un 429 genérico: es el cupo de Max, no algo pasajero."""
    vuelve = int(time.time()) + 3600

    async def paso(c: ClienteFalso):
        yield RateLimitEvent(RateLimitInfo(status="rejected", resets_at=vuelve, rate_limit_type="seven_day"), "u", "s")
        yield AssistantMessage(content=[TextBlock("API Error: 429")], model="m", error="rate_limit")
        yield resultado(error=True, texto="API Error: 429", api_error_status=429)

    with pytest.raises(SinCreditos) as e:
        correr(trabajo_de(trabajo_dir, None), [paso])
    assert (e.value.medio, e.value.estado) == ("max", "sin_cupo") and e.value.hasta == vuelve


def test_runner_pasajero_y_despues_cupo_manda_el_cupo(trabajo_dir):
    async def paso(c: ClienteFalso):
        yield AssistantMessage(content=[TextBlock("API Error: 429")], model="m", error="rate_limit")
        yield resultado(error=True, texto="You've hit your limit · resets 5pm")

    with pytest.raises(SinCreditos) as e:
        correr(trabajo_de(trabajo_dir, None), [paso])
    assert e.value.estado == "sin_cupo"


def test_runner_sin_login_al_conectar_con_api_es_clave_invalida(trabajo_dir, cfg_con_clave):
    class SinLogin(ClienteFalso):
        async def connect(self) -> None:
            raise RuntimeError("Invalid API key · Please run /login")

    with pytest.raises(SinCreditos) as e:
        AgenteClaude(cfg_con_clave, cliente=lambda op: SinLogin(op, [])).ejecutar(
            trabajo_de(trabajo_dir, None, facturacion="api"))
    assert (e.value.medio, e.value.estado) == ("api", "clave_invalida")


# --- revisión: el entorno real que arma el SDK, avisos y claves en los errores ---------------------

class ClienteTransporte:
    """El transporte real del SDK (SubprocessCLITransport.connect arma el entorno del CLI de verdad);
    sólo se reemplaza el lanzamiento del proceso, que captura `env` y falla."""

    def __init__(self, opciones: Any):
        import sys
        from dataclasses import replace

        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

        self.opciones = opciones
        self.t = SubprocessCLITransport(prompt="x", options=replace(opciones, cli_path=sys.executable))

    async def connect(self) -> None:
        await self.t.connect()

    async def disconnect(self) -> None:
        await self.t.close()


@pytest.fixture
def lanzamientos(monkeypatch) -> list[dict[str, Any]]:
    from claude_agent_sdk._internal.transport import subprocess_cli

    vistos: list[dict[str, Any]] = []

    async def open_process(cmd: Any, **kw: Any) -> Any:
        vistos.append({"cmd": list(cmd), "env": dict(kw["env"])})
        raise RuntimeError("no lanzo nada en los tests")

    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    monkeypatch.setattr(subprocess_cli.anyio, "open_process", open_process)
    return vistos


@pytest.mark.parametrize("facturacion", ["max", "api"])
def test_entorno_real_del_cli_sin_claves_ni_url_ajenas(trabajo_dir, cfg_con_clave, lanzamientos, monkeypatch,
                                                      facturacion):
    """Con el transporte real: en max ninguna clave llega al CLI; en api llega sólo la del .env. En los
    dos, la ANTHROPIC_BASE_URL que exporta una sesión de Claude Code (su proxy local) no pasa: la clave
    del .env iría a ese proxy y no a api.anthropic.com."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", DEL_ENTORNO)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-del-entorno")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/proxy-de-otra-sesion")
    r = AgenteClaude(cfg_con_clave, cliente=ClienteTransporte).ejecutar(
        trabajo_de(trabajo_dir, None, facturacion=facturacion, esfuerzo="high"))
    assert not r.ok and "no lanzo nada" in (r.error or "")
    ((lanzado),) = lanzamientos
    env, cmd = lanzado["env"], lanzado["cmd"]
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert not any(DEL_ENTORNO in v for v in env.values())
    if facturacion == "api":
        assert env["ANTHROPIC_API_KEY"] == CLAVE
    else:
        assert "ANTHROPIC_API_KEY" not in env and not any(CLAVE in v for v in env.values())
    assert cmd[cmd.index("--effort") + 1] == "high" and not any(CLAVE in x for x in cmd)
    # El entorno del proceso queda como estaba.
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9/proxy-de-otra-sesion"
    assert os.environ["ANTHROPIC_API_KEY"] == DEL_ENTORNO


def test_aviso_si_con_la_api_el_cli_usa_otra_clave(trabajo_dir, cfg_con_clave):
    """'/login managed key' es la clave de la consola guardada en Claude Code, no la del .env."""
    assert ag.aviso_clave("api", "/login managed key") is not None
    assert ag.aviso_clave("api", "ANTHROPIC_API_KEY") is None and ag.aviso_clave("max", "none") is None
    r, _ = correr(trabajo_de(trabajo_dir, None, facturacion="api"), [paso_ok(trabajo_dir, "/login managed key")],
                  cfg=cfg_con_clave)
    log = (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")
    assert "AVISO: la corrida es con la API key" in log and "/login managed key" in log and "Avisos:" in r.log


def test_la_clave_del_env_no_queda_en_el_error_aunque_la_corrida_sea_max(trabajo_dir):
    """Un error común (no de facturación) que trae la clave del .env: ni en r.error ni en los logs."""
    clave = "clave-rara-XYZ987"

    async def paso(c: ClienteFalso):
        yield init()
        yield resultado(error=True, subtipo="error_during_execution", texto=f"algo raro con {clave}")

    r, _ = correr(trabajo_de(trabajo_dir, None, facturacion="max"), [paso], clave_api=clave)
    assert not r.ok and r.error and "algo raro" in r.error
    assert clave not in r.error and clave not in r.log
    assert clave not in (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")


def test_lo_gastado_por_un_intento_descartado_viaja_en_la_excepcion(trabajo_dir, cfg_con_clave):
    async def paso(c: ClienteFalso):
        yield init("ANTHROPIC_API_KEY")
        yield AssistantMessage(content=[TextBlock("Credit balance is too low")], model="m", error="billing_error")
        yield resultado(error=True, texto=CREDITO_400, costo=1.25)

    with pytest.raises(SinCreditos) as e:
        correr(trabajo_de(trabajo_dir, None, facturacion="api"), [paso], cfg=cfg_con_clave)
    assert e.value.costo_usd == 1.25
    assert "este intento ya gastó US$ 1.25 (API)" in (trabajo_dir / ag.NOMBRE_LOG).read_text(encoding="utf-8")


@pytest.mark.parametrize("texto", ["You've hit your session limit · resets 3pm", "You've hit your limit",
                                   "Opus limit reached", "Claude AI usage limit reached|1800000000"])
def test_cupo_del_plan_max_por_el_texto(texto):
    from classmer.ia import clasificador as cl

    assert ag.clasificar_error(texto, "max", error="rate_limit") == "sin_cupo"
    assert cl._CUPO_RE.search(texto)
    # Un 429 común (sin señal de cupo agotado) sigue siendo pasajero.
    assert ag.clasificar_error("API Error: 429 rate_limit_error", "max", error="rate_limit") == "rate_limit"
