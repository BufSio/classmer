"""Plantilla LaTeX (classmer.cls, ejemplo.tex) y helper de compilación (classmer.latex).

Los tests marcados `latex` compilan de verdad con MiKTeX; se saltean si no está pdflatex (o lualatex).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from classmer import latex
from conftest import requiere

PREAMBULO = (
    "\\documentclass{classmer}\n"
    "\\materia{Análisis Matemático II}\n"
    "\\fechaclase{15 de octubre de 2026}\n"
    "\\numeroclase{7}\n"
    "\\tipoclase{Teórica}\n"
    "\\titulo{Derivada direccional y gradiente}\n"
)


def con_latex(motor: str = "pdflatex"):
    def deco(f):
        return pytest.mark.latex(requiere(motor)(f))
    return deco


@pytest.fixture
def carpeta(tmp_path: Path) -> Path:
    if not str(tmp_path).isascii():
        pytest.skip("la carpeta temporal tiene caracteres no ASCII")
    d = tmp_path / "apunte"
    latex.copiar_plantilla(d)
    return d


def escribir(carpeta: Path, texto: str, nombre: str = "apunte.tex") -> Path:
    ruta = carpeta / nombre
    ruta.write_text(texto, encoding="utf-8")
    return ruta


def pdfinfo(pdf: Path) -> str:
    if shutil.which("pdfinfo") is None:
        pytest.skip("falta pdfinfo")
    r = subprocess.run(["pdfinfo", "-enc", "UTF-8", str(pdf)], capture_output=True, encoding="utf-8",
                       errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    return r.stdout


# --- sin LaTeX ---

def test_comando():
    assert latex.comando("pdflatex", "apunte.tex") == [
        "pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-no-shell-escape",
        "apunte.tex"]
    assert latex.comando("lualatex", "x.tex")[0] == "lualatex"


def test_lecturas_externas():
    malos = {
        r"\newread\f \openin\f=../../../../.env \read\f to\x": [r"\openin", r"\read"],
        r"\readline\f to\x": [r"\readline"],
        r"\directlua{io.open('C:/x')} \latelua{} \luaexec{} \scantokens{x}":
            [r"\directlua", r"\latelua", r"\luaexec", r"\scantokens"],
        r"\pdffiledump{x} \filedump{x} \pdfmdfivesum{x}": [r"\pdffiledump", r"\filedump", r"\pdfmdfivesum"],
        r"\begin{luacode} io.open('x') \end{luacode}": [r"\begin{luacode}"],
        r"^^5copenin\f=x": [r"\openin"],                             # disfrazado con ^^
        r"100\% \openin\f=x": [r"\openin"],                          # \% no es comentario
        r"\input{C:/Users/usuario/x.tex}": [r"\input{C:/Users/usuario/x.tex}"],
        r"\input C:\Users\usuario\x.tex": [r"\input{C:\Users\usuario\x.tex}"],   # sin llaves
        r"\input ../../.env": [r"\input{../../.env}"],
        r"\input{/etc/hosts}": [r"\input{/etc/hosts}"],
        r"\include{../secreto}": [r"\include{../secreto}"],
        r"\InputIfFileExists{\ruta}{}{}": [r"\InputIfFileExists{\ruta}"],
        r"\lstinputlisting[language=Python]{C:/x.py}": [r"\lstinputlisting{C:/x.py}"],
        r"\verbatiminput{../x} \VerbatimInput[frame=single]{/x}": [r"\verbatiminput{../x}", r"\VerbatimInput{/x}"],
        r"\includepdf[pages=-]{C:/x.pdf}": [r"\includepdf{C:/x.pdf}"],
        r"\includegraphics[width=\linewidth]{C:/Classmer-data/token-qr.png}":
            [r"\includegraphics{C:/Classmer-data/token-qr.png}"],
        r"\includegraphics*{../../x.png}": [r"\includegraphics{../../x.png}"],
        r"\pizarra{../../pizarra_01.jpg}{00:01:00}{Foto}": [r"\pizarra{../../pizarra_01.jpg}"],
        r"\graphicspath{{C:/Users/}}": [r"\graphicspath{{C:/Users/}}"],
        r"\makeatletter\def\input@path{{C:/x/}}": [r"\input@path{{C:/x/}}"],
        r"\input|kpsewhich": [r"\input{|kpsewhich}"],
        # Conservador: también comentado (un '%' no siempre es comentario, ver ..._con_porcentajes).
        "% \\openin comentado y \\input{C:/x} también": [r"\openin", r"\input{C:/x}"],
    }
    for texto, esperado in malos.items():
        assert latex.lecturas_externas(texto) == esperado, texto
    buenos = [
        "% un comentario cualquiera, 100\\% y \\input{parte2} comentado",
        "\\input{parte2} \\include{capitulo} \\input apendice",
        "\\pizarra{pizarra_07.jpg}{00:34:52}{Epígrafe con 12:30 y C:}",
        "\\includegraphics[width=0.5\\linewidth]{fotos/pizarra_01.jpg}",
        "\\inputencoding{utf8} \\includeonly{a} \\readdata \\openinfile",
        "\\makeatletter\\let\\input@path\\Ginput@path\\makeatother",
        "$f:\\R\\to\\R$, $\\{x : x>0\\}$, \\ts{00:01:00}, $a..b$, \\csname foo\\endcsname, \\catcode`\\@=11",
    ]
    for texto in buenos:
        assert latex.lecturas_externas(texto) == [], texto
    # Lo que trae la plantilla de ejemplo (la referencia del agente) pasa.
    assert latex.lecturas_externas(latex.RUTA_EJEMPLO.read_text(encoding="utf-8")) == []


def test_lecturas_externas_adjuntos_y_opciones():
    # Material adjunto (§7): páginas de un PDF con ruta relativa adjuntos/… pasan, con cualquier opción.
    buenos = [
        r"\includegraphics[page=2,width=\linewidth]{adjuntos/Slides_clase_5.pdf}",
        r"\includepdf[pages=2-3]{adjuntos/Slides_clase_5.pdf}",
        r"\includepdf[pages={1,3-5}, pagecommand={\thispagestyle{empty}}]{adjuntos/x.pdf}",
        r"\includegraphics[trim={1cm 2cm 0 0}, clip, page=3]{adjuntos/x.pdf}",
        r"\includegraphics[page=1,width=.3\linewidth]{slides.pdf}",
        r"\pizarra{adjuntos/captura.jpg}{00:34:52}{Captura}",
        r"\pizarra{adjuntos/captura.jpg}{}{Sin minuto}",
        r"\includegraphics[alt={Figura [1]: la curva}]{adjuntos/x.pdf}",   # antes: falso positivo
        "\\includegraphics\n  [page=2]\n  {adjuntos/x.pdf}",
        r"\includegraphics *[page=2]{adjuntos/x.pdf}",
        # Un '[' sin cerrar en un opcional (intervalo semiabierto): xparse no lo lee más allá del párrafo,
        # así que el ']' de un '(a;b]' de más abajo no lo cierra (antes: '\includegraphics{$.}').
        "\\includegraphics[page=2,width=\\linewidth,alt=Intervalo [0;1)]{adjuntos/slides.pdf}\n\nSea $x\\in(a;b]$.",
        "\\includegraphics[alt=Intervalo [0;1)]{adjuntos/x.pdf} y \\par Sea $x\\in(a;b]$.",
        "\\includegraphics[page=1, % el ancho ]\n  width=3cm]{adjuntos/x.pdf}",
    ]
    for texto in buenos:
        assert latex.lecturas_externas(texto) == [], texto
    malos = {
        r"\includegraphics[page=2,width=\linewidth]{../input/adjuntos/x.pdf}":
            [r"\includegraphics{../input/adjuntos/x.pdf}"],
        r"\includepdf[pages=2-3]{C:/Classmer-data/sesiones/s/adjuntos/x.pdf}":
            [r"\includepdf{C:/Classmer-data/sesiones/s/adjuntos/x.pdf}"],
        r"\includegraphics[page=1]{/adjuntos/x.pdf}": [r"\includegraphics{/adjuntos/x.pdf}"],
        r"\includegraphics[page=1]{$HOME/x.pdf}": [r"\includegraphics{$HOME/x.pdf}"],
        r"\includegraphics[page=1]{~/x.pdf}": [r"\includegraphics{~/x.pdf}"],
        "\\includegraphics\n  [page=2]\n  {../x.pdf}": [r"\includegraphics{../x.pdf}"],
        r"\includegraphics[width=3cm][x]{C:/y.png}": [r"\includegraphics{C:/y.png}"],
        # Huecos de antes: un ']' entre llaves o escapado no cierra el opcional para TeX.
        r"\includegraphics[alt={a]b}]{/etc/passwd}": [r"\includegraphics{/etc/passwd}"],
        r"\includegraphics[trim={0 0 0 0}, alt={x]}]{C:/x.png}": [r"\includegraphics{C:/x.png}"],
        r"\includegraphics[alt=\]]{../x.png}": [r"\includegraphics{../x.png}"],
        # Corchetes anidados: xparse los balancea (LaTeX2e no): se miran las dos lecturas.
        r"\includepdf[pages=1,a[b]c]{../x.pdf}": [r"\includepdf{../x.pdf}"],
        r"\includegraphics[a[b]{../x.pdf}]{ok.pdf}": [r"\includegraphics{../x.pdf}"],
        # El párrafo sólo corta la lectura de xparse: la de LaTeX2e cierra en el primer ']' y mira el archivo.
        "\\includegraphics[page=2,width=\\linewidth,alt=Intervalo [0;1)]{C:/x.pdf}\n\nSea $x\\in(a;b]$.":
            [r"\includegraphics{C:/x.pdf}"],
        # TeX saltea espacios, saltos y comentarios antes del '*' (\@ifstar lo ve igual).
        r"\includegraphics *{C:/Users/usuario/secreto.png}": [r"\includegraphics{C:/Users/usuario/secreto.png}"],
        "\\includegraphics\n*{C:/x.png}": [r"\includegraphics{C:/x.png}"],
        "\\includegraphics%\n*{C:/x.png}": [r"\includegraphics{C:/x.png}"],
        r"\includegraphics *[page=1]{/abs/x.pdf}": [r"\includegraphics{/abs/x.pdf}"],
    }
    for texto, esperado in malos.items():
        assert latex.lecturas_externas(texto) == esperado, texto
    # Un opcional que no cierra no deja argumento (TeX daría error) y no se cuelga.
    assert latex.lecturas_externas(r"\includegraphics[page=2" + " x" * 50_000) == []


def test_lecturas_externas_con_porcentajes():
    # Un '%' no siempre es comentario: en \url, \href o \verb (links del campus con %20, que traen las slides)
    # lo que sigue en la línea TeX lo ejecuta igual. Antes, todo esto daba [].
    malos = {
        "Ver \\url{https://campus.ejemplo.edu/slides%20clase.pdf} y \\includegraphics[width=1cm]{C:/x/secreto.png}":
            [r"\includegraphics{C:/x/secreto.png}"],
        r"\verb|%| \includegraphics{C:/x.png}": [r"\includegraphics{C:/x.png}"],
        r"\href{https://a/b%20c}{link} \input{C:/x.tex}": [r"\input{C:/x.tex}"],
        r"\begin{verbatim}%\end{verbatim}\openin\f=x": [r"\openin"],
        # Y después del comando, un '%' sí es comentario (TeX se lo saltea), aunque antes haya un %20.
        "\\url{a%20b} \\includegraphics%\n{C:/x.png}": [r"\includegraphics{C:/x.png}"],
        "\\url{a%20b} \\includegraphics%\n *[page=1 % ]\n]{C:/x.pdf}": [r"\includegraphics{C:/x.pdf}"],
        # Un comentario dentro del nombre se lleva el fin de línea: TeX lee '../secreto'.
        "\\input{.%\n  ./secreto}": [r"\input{../secreto}"],
        "\\input .%\n./secreto": [r"\input{../secreto}"],
    }
    for texto, esperado in malos.items():
        assert latex.lecturas_externas(texto) == esperado, texto
    buenos = [
        "Ver \\url{https://campus.ejemplo.edu/slides%20clase.pdf} y \\includegraphics[width=1cm]{adjuntos/x.pdf}",
        "\\href{https://campus.ejemplo.edu/a%20b.pdf}{las slides}, 100\\% \\input{parte2}% y nada más",
        "\\input parte2% comentario\n\\section{Intro}",
    ]
    for texto in buenos:
        assert latex.lecturas_externas(texto) == [], texto


# Links como los que trae la búsqueda en internet del agente: %, #, _, & y ~ en el texto, en una caja y
# (escapados) dentro del argumento de otro comando.
LINKS_WEB = (
    "Según \\href{https://www.indec.gob.ar/uploads/informesdeprensa/ipc_11_26.pdf#page=3}{el INDEC} y\n"
    "\\url{https://www.ambito.com/economia/nota_con-guion?id=12&utm=x%20y#comentarios}.\n\n"
    "\\begin{agregadoIA}[Lo que dice la web]\n"
    "Fuente: \\url{https://es.wikipedia.org/wiki/Inflaci%C3%B3n_en_Argentina#Historia} y\n"
    "\\href{https://example.org/~usuario/a_b.html?x=1&y=2%2C3}{otra nota} (10/11/2026).\n"
    "\\end{agregadoIA}\n\n"
    "Al pie\\footnote{Ver \\href{https://example.org/a\\%20b\\#c}{la nota} o \\url{https://example.org/~x_y}.}\n"
    "\\section{Con link en el título \\texorpdfstring{\\href{https://example.org/a\\%20b}{(fuente)}}{}}\n"
)


def test_links_de_la_web_no_son_lecturas_de_afuera(tmp_path: Path):
    assert latex.lecturas_externas(LINKS_WEB) == []
    escribir(tmp_path, PREAMBULO + "\\begin{document}\n" + LINKS_WEB + "\\end{document}\n")
    assert latex.lecturas_externas_en(tmp_path) == []


@con_latex()
def test_links_de_la_web_compilan(carpeta: Path):
    escribir(carpeta, PREAMBULO + "\\begin{document}\n" + LINKS_WEB + "\\end{document}\n")
    r = latex.compilar(carpeta)
    assert r.ok, r.log
    assert r.errores == []


def test_lecturas_externas_en_la_carpeta(tmp_path: Path):
    latex.copiar_plantilla(tmp_path)  # classmer.cls usa \graphicspath con '..': es del server, no cuenta
    escribir(tmp_path, "\\documentclass{classmer}\\begin{document}\\input{parte2}\\end{document}\n")
    (tmp_path / "apunte.log").write_text("\\tcb@inputstream=\\read3\n", encoding="utf-8")  # lo escribe TeX
    assert latex.lecturas_externas_en(tmp_path) == []
    (tmp_path / "figuras").mkdir()
    (tmp_path / "figuras" / "parte2.tex").write_text("\\openin\\f=../.env\n", encoding="utf-8")
    (tmp_path / "apunte.aux").write_text("\\relax\n\\input{C:/x}\n", encoding="utf-8")
    escribir(tmp_path, "\\documentclass{classmer}\\begin{document}\\input{/etc/hosts}\\end{document}\n")
    assert latex.lecturas_externas_en(tmp_path) == [
        "\\input{/etc/hosts}", "apunte.aux: \\input{C:/x}", "figuras/parte2.tex: \\openin"]


def test_copiar_plantilla(tmp_path: Path):
    ruta = latex.copiar_plantilla(tmp_path / "nueva" / "plantilla", ejemplo=True)
    assert ruta == tmp_path / "nueva" / "plantilla" / "classmer.cls"
    assert ruta.read_bytes() == latex.RUTA_CLS.read_bytes()
    assert (ruta.parent / "ejemplo.tex").read_bytes() == latex.RUTA_EJEMPLO.read_bytes()
    assert latex.copiar_plantilla(tmp_path) == tmp_path / "classmer.cls"
    assert not (tmp_path / "ejemplo.tex").exists()


def test_ruta_con_tildes_da_error_claro(tmp_path: Path):
    d = tmp_path / "Análisis"
    d.mkdir()
    (d / "apunte.tex").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="sin tildes"):
        latex.compilar(d)


def test_argumentos_invalidos(tmp_path: Path):
    with pytest.raises(ValueError, match="motor"):
        latex.compilar(tmp_path, motor="word")
    with pytest.raises(ValueError):
        latex.compilar(tmp_path, pasadas=0)


def test_falta_el_tex(tmp_path: Path):
    r = latex.compilar(tmp_path, "no_esta.tex")
    assert not r.ok and r.pdf is None and r.pasadas == 0
    assert "no_esta.tex" in r.log


def test_lectura_del_log_cortado_a_79():
    ruta = "C:/Users/usuario/AppData/Local/Programs/MiKTeX/tex/latex/tcolorbox/tcbbreakable.code.tex"
    linea = f"{ruta}:123: Undefined control sequence."
    log = "\n".join([
        "(./apunte.aux)",
        linea[:79], linea[79:158], linea[158:],
        "l.123 \\foo",
        "",
        "./apunte.tex:12: LaTeX Error: Environment teoremita undefined.",
        "",
        "See the LaTeX manual or LaTeX Companion for explanation.",
        "Type  H <return>  for immediate help.",
        " ...                                              ",
        "l.12 \\begin{teoremita}",
        "",
        "./apunte.tex:12:  ==> Fatal error occurred, no output PDF file produced!",
        "Package hyperref Warning: Token not allowed in a PDF string (Unicode):",
        "(hyperref)                removing `\\ts' on input line 20.",
        "Class classmer Warning: Foto no encontrada: pizarra_09.jpg on input line 30.",
    ])
    lineas = latex._desenvolver(log)
    errores = latex._errores(lineas)
    assert [e.texto for e in errores] == [
        f"{ruta}:123: Undefined control sequence.",
        "apunte.tex:12: LaTeX Error: Environment teoremita undefined.",
    ]
    assert errores[1].contexto == ["l.12 \\begin{teoremita}"]
    avisos = latex._avisos(lineas)
    assert avisos[0].startswith("Class classmer Warning: Foto no encontrada: pizarra_09.jpg")
    assert "removing `\\ts'" in avisos[1]


# --- con LaTeX ---

@con_latex()
def test_ejemplo_compila_con_pdflatex(carpeta: Path):
    latex.copiar_plantilla(carpeta, ejemplo=True)
    r = latex.compilar(carpeta, "ejemplo.tex", pasadas=2, timeout_s=300)
    assert r.ok, r.log
    assert r.pdf == carpeta / "ejemplo.pdf" and r.pdf.read_bytes().startswith(b"%PDF")
    assert r.pasadas == 2 and r.errores == []
    assert r.paginas is not None and r.paginas >= 2
    assert r.ruta_log == carpeta / "ejemplo.log"
    # La foto del ejemplo no existe: sale el recuadro y un aviso, pero compila.
    assert any("Foto no encontrada: pizarra_07.jpg" in a for a in r.avisos)
    assert "compiló bien" in r.log and len(r.log) <= latex.MAX_RESUMEN
    info = pdfinfo(r.pdf)
    assert "Derivada direccional y gradiente" in info and "Análisis Matemático II" in info


@con_latex()
def test_minimo_solo_preambulo(carpeta: Path):
    escribir(carpeta, PREAMBULO + "\\begin{document}\nHola.\n\\end{document}\n")
    r = latex.compilar(carpeta)
    assert r.ok, r.log
    assert r.pasadas == 2 and r.paginas == 1
    # Sin shell-escape: MiKTeX, por defecto, dejaría "restricted \write18 enabled."
    assert "write18 enabled" not in r.ruta_log.read_text(encoding="utf-8", errors="replace")


@con_latex()
def test_sin_datos_de_la_clase(carpeta: Path):
    escribir(carpeta, "\\documentclass{classmer}\n\\begin{document}\nSólo texto.\n\\end{document}\n")
    r = latex.compilar(carpeta, pasadas=1)
    assert r.ok, r.log


@con_latex()
def test_datos_de_la_clase_normalizados(carpeta: Path):
    escribir(carpeta, "\\documentclass[sinencabezado]{classmer}\n"
                      "\\fechaclase{2026-10-05}\n\\tipoclase{practica}\n"
                      "\\makeatletter\\typeout{FECHA=[\\classmer@fecha]}\\makeatother\n"
                      "\\begin{document}\nSin datos de materia ni título.\n\\end{document}\n")
    r = latex.compilar(carpeta, pasadas=1)
    assert r.ok, r.log
    assert "FECHA=[5 de octubre de 2026]" in r.ruta_log.read_text(encoding="utf-8", errors="replace")


@con_latex()
def test_error_con_numero_de_linea(carpeta: Path):
    escribir(carpeta, PREAMBULO + "\\begin{document}\nHola.\n\nAcá hay un \\comandoquenoexiste{} mal.\n"
                                  "\\end{document}\n")
    linea = PREAMBULO.count("\n") + 4
    r = latex.compilar(carpeta)
    assert not r.ok and r.pdf is None
    assert r.pasadas == 1  # si falla la primera, no corre la segunda
    assert r.errores and r.errores[0] == f"apunte.tex:{linea}: Undefined control sequence."
    assert f"apunte.tex:{linea}:" in r.log and "comandoquenoexiste" in r.log
    assert len(r.log) <= latex.MAX_RESUMEN


@con_latex()
def test_pizarra_con_foto_real_y_faltante(tmp_path: Path):
    if not str(tmp_path).isascii():
        pytest.skip("la carpeta temporal tiene caracteres no ASCII")
    # Carpeta de trabajo del agente: se compila en output/ y las fotos están en ../input/fotos/.
    fotos = tmp_path / "trabajo" / "input" / "fotos"
    fotos.mkdir(parents=True)
    Image.new("RGB", (1600, 1200), (210, 225, 240)).save(fotos / "pizarra_01.jpg", quality=80)
    salida = tmp_path / "trabajo" / "output"
    latex.copiar_plantilla(salida)
    cuerpo = (PREAMBULO + "\\begin{document}\n"
              "\\pizarra{pizarra_01.jpg}{00:34:52}{Foto real}\n"
              "\\pizarra{fotos/pizarra_01.jpg}{00:35:00}{Con la carpeta en el nombre}\n"
              "\\pizarra{pizarra_01}{00:36:00}{Sin extensión}\n"
              "\\begin{ejemplo}\\pizarra{no_existe.jpg}{00:40:00}{Falta}\\end{ejemplo}\n"
              "\\pizarra{otra_que_falta.png}{}{}\n"
              "\\end{document}\n")
    escribir(salida, cuerpo)
    r = latex.compilar(salida, pasadas=1)
    assert r.ok, r.log
    faltantes = [a for a in r.avisos if "Foto no encontrada" in a]
    assert len(faltantes) == 2, r.avisos
    assert "no_existe.jpg" in faltantes[0] and "otra_que_falta.png" in faltantes[1]
    assert b"/DCTDecode" in r.pdf.read_bytes()  # el JPG quedó embebido

    # Carpeta publicada: fotos/ al lado del .tex.
    publicada = tmp_path / "publicada"
    (publicada / "fotos").mkdir(parents=True)
    shutil.copy(fotos / "pizarra_01.jpg", publicada / "fotos" / "pizarra_01.jpg")
    latex.copiar_plantilla(publicada)
    escribir(publicada, PREAMBULO + "\\begin{document}\n\\pizarra{pizarra_01.jpg}{00:34:52}{Foto}\n\\end{document}\n")
    r = latex.compilar(publicada, pasadas=1)
    assert r.ok, r.log
    assert not [a for a in r.avisos if "Foto no encontrada" in a]


@con_latex()
def test_timeout_corta_y_avisa(carpeta: Path):
    escribir(carpeta, PREAMBULO + "\\begin{document}\n\\def\\bucle{\\bucle}\\bucle\n\\end{document}\n")
    r = latex.compilar(carpeta, timeout_s=4)
    assert not r.ok and r.pdf is None and r.pasadas == 1
    assert "tardó más de 4 s" in r.log
    assert r.duracion_s < 30


@con_latex()
def test_aux_roto_se_borra_y_reintenta(carpeta: Path):
    escribir(carpeta, PREAMBULO + "\\begin{document}\n\\section{Uno}\\label{s}Ver \\ref{s}.\n\\end{document}\n")
    (carpeta / "apunte.aux").write_text("\\relax\n\\macroquenoexiste{\n", encoding="utf-8")
    r = latex.compilar(carpeta)
    assert r.ok, r.log
    assert r.pasadas == 2


@con_latex()
def test_libertad_paquetes_y_opciones_del_agente(carpeta: Path):
    # Lo que un agente suele escribir por costumbre no tiene que romper la compilación.
    preambulo = PREAMBULO.replace("{classmer}", "[11pt,a4paper]{classmer}") + "\n".join([
        "\\usepackage[utf8]{inputenc}",
        "\\usepackage[T1]{fontenc}",
        "\\usepackage[spanish]{babel}",
        "\\usepackage{amsmath,amssymb,amsthm,mathtools}",
        "\\usepackage[dvipsnames,table]{xcolor}",
        "\\usepackage[margin=2.5cm]{geometry}",
        "\\usepackage[colorlinks=true,linkcolor=blue]{hyperref}",
        "\\usepackage[skins,theorems]{tcolorbox}",
        "\\usepackage[shortlabels]{enumitem}",
        "\\usepackage{pgfplots}",
        "\\usepackage{circuitikz}",
        "\\usepackage{siunitx,booktabs,float,multicol,cleveref}",
        "\\newcommand{\\R}{\\mathbb{R}}",
        "\\newtheorem{proposicion}{Proposición}",
        "",
    ])
    cuerpo = r"""\begin{document}
\maketitle
\section{Uno \ts{00:01:00}}\label{sec:uno}
Ver \cref{sec:uno}: $\R$, $\Q$, $\lim_{x\to 0}\frac{\sin x}{x}=1$, $\max f$, $\sen x$,
\SI{3,5}{\percent}, {\color{ForestGreen}verde}, \nota{inaudible}.
Unicode suelto: si x ∈ ℝ y α ≤ β, f(x) → ∞; $‖v‖ ≥ 0$, $A ⊆ B ∪ C$.
\begin{proposicion} Algo. \end{proposicion}
\begin{enumerate}[(a)] \item uno \item dos \end{enumerate}
\section{Dos \nota{inaudible}}
\begin{definicion}[Con lista] \begin{itemize}\item a\end{itemize} \end{definicion}
\begin{teorema}[En $\R^n$ \ts{00:03:00}] $x^2 + \nota{ilegible}$ \end{teorema}
\nuevacaja{lema}{Lema}{teorema}
\begin{lema} l \end{lema}
\begin{tcolorbox}[ficha, colback=fondoazul, colframe=azul, colbacktitle=azul, title={Caja de ficha}] f \end{tcolorbox}
\begin{ejemplo} \begin{intuicion}[Anidada] adentro \end{intuicion} \end{ejemplo}
\begin{parcial} p \end{parcial}
\begin{agregadoIA}[Gráfico]
\begin{tikzpicture}\begin{axis}[width=5cm]\addplot {x^2};\end{axis}\end{tikzpicture}
\end{agregadoIA}
\begin{duda} \begin{tikzpicture}\draw[->] (0,0) -- (1,0) node[right]{\ts{00:02:00}};\end{tikzpicture} \end{duda}
\begin{circuitikz}\draw (0,0) to[R=$R_1$] (2,0);\end{circuitikz}
\begin{tabular}{lr}\toprule a & b\\ \midrule \rowcolor{gray!20} 1 & 2\\ \bottomrule\end{tabular}
\end{document}
"""
    escribir(carpeta, preambulo + cuerpo)
    r = latex.compilar(carpeta, pasadas=1)
    assert r.ok, r.log


@con_latex("lualatex")
def test_ejemplo_compila_con_lualatex(carpeta: Path):
    latex.copiar_plantilla(carpeta, ejemplo=True)
    # La primera vez lualatex arma la caché de fuentes: timeout generoso.
    r = latex.compilar(carpeta, "ejemplo.tex", motor="lualatex", pasadas=1, timeout_s=900)
    assert r.ok, r.log
    assert r.paginas is not None and r.paginas >= 2


def pdftexto(pdf: Path) -> str:
    if shutil.which("pdftotext") is None:
        pytest.skip("falta pdftotext")
    r = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"], capture_output=True, encoding="utf-8",
                       errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    return " ".join(r.stdout.split())


@con_latex()
def test_cajas_con_origen_importante_y_parcial(carpeta: Path):
    # §8: lo importante muestra su origen con un ícono chico y sin texto (pedido del usuario del
    # 14/10): estrellita si lo marcó en clase, destellito si lo detectó Classmer. El uso viejo de
    # parcial (primer opcional = título) sigue andando.
    escribir(carpeta, PREAMBULO + r"""\begin{document}
\begin{importante}[clase]
Marca uno.
\end{importante}
\begin{importante}[ia][Regla de la cadena]
Detectado dos.
\end{importante}
\begin{importante}
Sin origen.
\end{importante}
\begin{parcial}[clase][Entra hasta gradiente]
Aviso tres.
\end{parcial}
\begin{parcial}[Consulta]
Uso viejo.
\end{parcial}
\begin{importante}[clase]
{}[a,b] con corchete.
\end{importante}
\end{document}
""")
    r = latex.compilar(carpeta, pasadas=1)
    assert r.ok, r.log
    texto = pdftexto(r.pdf)
    assert "marcado en clase" not in texto and "detectado por IA" not in texto
    assert texto.count("⋆") == 3                       # la estrellita ($\bigstar$) de [clase]
    assert "Importante · Regla de la cadena" in texto
    assert "Para el parcial · Entra hasta gradiente" in texto
    assert "Para el parcial · Consulta" in texto
    assert "[a,b] con corchete" in texto


@con_latex()
def test_minutos_invisibles_por_defecto_y_visibles_con_la_opcion(carpeta: Path):
    # Pedido del usuario del 14/10: los \ts no se ven en el PDF (quedan en el .tex, meta.json y el
    # transcript para ubicar cada parte en el audio). Con [minutos] vuelven a verse.
    cuerpo = ("\\begin{document}\n"
              "\\section{Cardinalidad \\ts{00:01:04}}\n"
              "Texto con minuto \\ts{00:12:34}. Sigue \\ts{00:12:40} igual.\n"
              "\\begin{agregadoIA}[Detalle] Algo extra. \\end{agregadoIA}\n"
              "\\pizarra{no-existe.jpg}{00:34:52}{Epígrafe de la foto}\n"
              "\\end{document}\n")
    escribir(carpeta, PREAMBULO + cuerpo)
    r = latex.compilar(carpeta, pasadas=1)
    assert r.ok, r.log
    texto = pdftexto(r.pdf)
    assert "00:12:34" not in texto and "00:01:04" not in texto and "00:34:52" not in texto
    assert "Texto con minuto. Sigue igual." in " ".join(texto.split())
    assert "Agregado de Classmer · Detalle" in texto and "Agregado por IA" not in texto
    assert "Epígrafe de la foto" in texto

    escribir(carpeta, PREAMBULO.replace("\\documentclass{classmer}",
                                        "\\documentclass[minutos]{classmer}") + cuerpo)
    r = latex.compilar(carpeta, pasadas=1)
    assert r.ok, r.log
    texto = pdftexto(r.pdf)
    assert "00:12:34" in texto and "00:34:52" in texto


@con_latex()
def test_estrellas_y_tildes_sueltas_compilan(carpeta: Path):
    # Prueba real 2026-10-14: el agente escribió "⭐" tal cual y pdflatex cortó con
    # "Unicode character ⭐ (U+2B50)", gastando un intento de compilación.
    escribir(carpeta, PREAMBULO + "\\begin{document}\n"
             "Marcado ⭐ en clase, ★ y ☆, bien ✓ ✔ y mal ✗. En fórmula: $a ⭐ b$.\n"
             "\\end{document}\n")
    r = latex.compilar(carpeta, pasadas=1)
    assert r.ok, r.log


def test_rutas_con_variables_de_entorno_se_rechazan():
    # TeX puede expandir $VARIABLE al buscar el archivo: leería afuera o mostraría la clave en un error.
    for texto in (r"\input{$ANTHROPIC_API_KEY}", r"\includegraphics{$HOME/foto.jpg}",
                  r"\pizarra{${USERPROFILE}/x.jpg}{00:01:00}{x}", r"\include{a$b}"):
        assert latex.lecturas_externas(texto), texto
    assert latex.lecturas_externas(r"Cuesta \$100: \input{parte2}") == []
