"""Búsqueda FTS (§10): texto plano del apunte, tramos, armado de consultas y búsqueda en el índice.

La integración (publicar indexa, `classmer reindexar`, la herramienta buscar) está en test_mcp.py.
"""

from __future__ import annotations

import sqlite3

import pytest

from classmer import busqueda
from classmer.busqueda import (
    consultas,
    latex_a_texto,
    tramos_apunte,
    tramos_secciones,
    tramos_transcript,
)

TEX = r"""\documentclass{classmer}
\materia{Materia de Prueba}
\titulo{Derivada direccional}
\usepackage{tikz}
\begin{document}
Intro sin sección \ts{00:00:05}.

\section{Derivada direccional \ts{00:34:10}}
La \textbf{derivada direccional} se define como un límite \ts{00:34:30}. % comentario: secreto

\begin{definicion}[Derivada direccional]
Para $v$ unitario, $D_v f(a) = \lim_{t\to 0} \frac{f(a+tv)-f(a)}{t}$ \ts{00:35:00}.
\end{definicion}

\begin{tikzpicture}
\draw (0,0) -- (1,1) node {nodo dibujado};
\end{tikzpicture}

\section{Gradiente}
El gradiente es perpendicular a las curvas de nivel.

\begin{importante}[clase][Para el parcial]
Entra la regla de la cadena \ts{00:52:00}.
\end{importante}
\end{document}
"""

META_SECCIONES = [
    {"titulo": "Derivada direccional", "inicio_ms": 2_040_000, "fin_ms": 2_400_000, "temas": ["derivada direccional"]},
    {"titulo": "Gradiente", "inicio_ms": 2_400_000, "fin_ms": 3_300_000, "temas": ["gradiente", "curvas de nivel"]},
]


def test_latex_a_texto_saca_comandos_comentarios_y_dibujos():
    texto = latex_a_texto(
        r"La \textbf{derivada} \emph{direccional} \ts{00:01:02} % comentario" "\n"
        r"\begin{tikzpicture}\draw (0,0) node {dibujo};\end{tikzpicture}"
        r"\begin{importante}[ia][Título] 50\% y \#A\end{importante} ``cita'' a~b \pizarra{fotos/x.jpg}{00:02:00}{La pizarra}"
    )
    assert texto == "La derivada direccional Título 50% y #A “cita” a b La pizarra"


def test_tramos_apunte_con_minutos_y_secciones():
    tramos = tramos_apunte(TEX, META_SECCIONES)
    textos = [t.texto for t in tramos]
    # El preámbulo y los dibujos no se indexan; los comentarios tampoco.
    assert not any("Materia de Prueba" in t or "nodo dibujado" in t or "secreto" in t for t in textos)
    assert tramos[0].texto == "Intro sin sección ." and tramos[0].inicio_ms == 5_000
    # Una sección con \ts en el título arranca en ese minuto, con su título al principio.
    derivada = tramos[1]
    assert derivada.texto.startswith("Derivada direccional La derivada direccional se define como un límite")
    assert derivada.inicio_ms == 34 * 60_000 + 10_000
    assert "Para v unitario" in derivada.texto
    # Sin \ts en el título: el inicio de la sección según meta.json; nunca se mezclan secciones.
    gradiente = [t for t in tramos if t.texto.startswith("Gradiente")]
    assert len(gradiente) == 1 and gradiente[0].inicio_ms == 2_400_000
    assert "Derivada" not in gradiente[0].texto
    assert "Para el parcial Entra la regla de la cadena" in gradiente[0].texto


def test_tramos_apunte_parte_lo_largo_y_toma_el_minuto_de_cada_tramo(monkeypatch):
    monkeypatch.setattr(busqueda, "TRAMO_APUNTE_CHARS", 60)
    tex = ("\\begin{document}\\section{Larga}\n"
           + "\n\n".join(f"Párrafo {i} con bastante texto para llenar el tramo \\ts{{00:0{i}:00}}." for i in range(1, 5))
           + "\n\nÚltimo sin minuto propio.\n\\end{document}")
    tramos = tramos_apunte(tex)
    assert [t.inicio_ms for t in tramos] == [60_000, 120_000, 180_000, 240_000, 240_000]
    assert tramos[-1].texto == "Último sin minuto propio."


def test_tramos_apunte_sin_document_ni_secciones():
    assert tramos_apunte("") == []
    assert tramos_apunte("Texto suelto \\ts{00:00:09}")[0].inicio_ms == 9_000


def test_tramos_secciones_lleva_titulo_y_temas():
    tramos = tramos_secciones(META_SECCIONES + [{"titulo": "", "temas": []}])
    assert [(t.texto, t.inicio_ms) for t in tramos] == [
        ("Sección: Derivada direccional. Temas: derivada direccional.", 2_040_000),
        ("Sección: Gradiente. Temas: gradiente, curvas de nivel.", 2_400_000),
    ]


def test_tramos_transcript_une_segmentos_seguidos():
    segmentos = [{"inicio_ms": i * 10_000, "fin_ms": i * 10_000 + 9_000, "texto": f"frase {i}"} for i in range(10)]
    segmentos.insert(3, {"inicio_ms": 30_500, "fin_ms": 31_000, "texto": "   "})  # vacío: se ignora
    tramos = tramos_transcript(segmentos)
    assert [(t.inicio_ms, t.texto) for t in tramos] == [
        (0, "frase 0 frase 1 frase 2 frase 3 frase 4"),
        (50_000, "frase 5 frase 6 frase 7 frase 8 frase 9"),
    ]
    largos = [{"inicio_ms": i * 1000, "texto": "x" * 300} for i in range(3)]
    assert len(tramos_transcript(largos)) == 3  # tope de caracteres


@pytest.mark.parametrize("texto, esperado", [
    ("límite", [("todas", 'texto : ("limite"*)')]),
    ("qué dijo el profe sobre las derivadas", [
        ("todas", 'texto : ("profe"* AND "derivada"*)'),
        ("alguna", 'texto : ("profe"* OR "derivada"*)'),
    ]),
    ('"regla de la cadena" funciones', [
        ("todas", 'texto : ("regla de la cadena" AND "funcion"*)'),
        ("alguna", 'texto : ("regla de la cadena" OR "funcion"*)'),
    ]),
    ("de la", [("todas", 'texto : ("de" AND "la")'), ("alguna", 'texto : ("de" OR "la")')]),
    ("f(x) = 2", [("todas", 'texto : ("f" AND "x" AND "2")'), ("alguna", 'texto : ("f" OR "x" OR "2")')]),
    ("   ", []),
    ('"" AND OR (', [("todas", 'texto : ("and" AND "or")'), ("alguna", 'texto : ("and" OR "or")')]),
])
def test_consultas(texto, esperado):
    assert consultas(texto) == esperado


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    busqueda.asegurar_tabla(c)
    filas = [
        ("La derivada direccional se define como un límite", "prueba", "apunte", "s1", 2_050_000),
        ("El gradiente es perpendicular a las curvas de nivel", "prueba", "transcript", "s1", 2_500_000),
        ("Repasamos derivadas parciales", "otra", "transcript", "s2", 60_000),
        ("Clase de prueba sin nada", "prueba", "transcript", "s3", 0),
    ]
    c.executemany("INSERT INTO busqueda (texto, materia, tipo, ref, inicio_ms) VALUES (?, ?, ?, ?, ?)", filas)
    yield c
    c.close()


def test_buscar_todas_las_palabras_sin_tildes(conn):
    res, modo = busqueda.buscar(conn, "LIMITE direccional")
    assert modo == "todas"
    assert [(r.sesion_id, r.tipo, r.inicio_ms) for r in res] == [("s1", "apunte", 2_050_000)]
    assert "«límite»" in res[0].fragmento and "«direccional»" in res[0].fragmento


def test_buscar_plural_y_materia(conn):
    res, _ = busqueda.buscar(conn, "derivadas")
    assert {r.sesion_id for r in res} == {"s1", "s2"}
    res, _ = busqueda.buscar(conn, "derivadas", materia="otra")
    assert [r.sesion_id for r in res] == ["s2"]


def test_buscar_cae_a_coincidencias_parciales(conn):
    res, modo = busqueda.buscar(conn, "gradiente integral")
    assert modo == "alguna"
    assert [r.sesion_id for r in res] == ["s1"]
    assert busqueda.buscar(conn, "inexistente") == ([], None)


def test_buscar_solo_en_el_texto(conn):
    # "prueba" es una materia y "apunte" un tipo: sólo cuentan si están en el texto.
    res, _ = busqueda.buscar(conn, "prueba")
    assert [r.sesion_id for r in res] == ["s3"]
    assert busqueda.buscar(conn, "apunte") == ([], None)


def test_buscar_con_rangos(conn):
    res, _ = busqueda.buscar(conn, "derivada OR gradiente", rangos={"s1": [(2_400_000, 3_000_000)]})
    assert [(r.sesion_id, r.inicio_ms) for r in res] == [("s1", 2_500_000)]
