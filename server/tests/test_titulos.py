"""Títulos con LaTeX: limpiar_titulo, cortar_en_palabra y el nombre de la carpeta publicada."""

from __future__ import annotations

import pytest

from classmer.pipeline.publicar import LARGO_TITULO, SIN_TITULO, nombre_carpeta, titulo_carpeta
from classmer.util import cortar_en_palabra, limpiar_titulo


@pytest.mark.parametrize("crudo,limpio", [
    # Un caso real: "--" es un guion, no dos.
    ("Opciones: camino a Black--Scholes", "Opciones: camino a Black-Scholes"),
    ("Teoría --- práctica", "Teoría — práctica"),
    ("Black---Scholes", "Black—Scholes"),
    (r"$\sigma$-álgebras", "σ-álgebras"),
    (r"Gradiente y $\nabla f$", "Gradiente y ∇ f"),
    (r"\textbf{Gradiente} y \emph{más}", "Gradiente y más"),
    (r"Ganancia del 50\%", "Ganancia del 50%"),
    (r"A \& B", "A & B"),
    (r"Costo en \$", "Costo en $"),
    # Acentos: los de verdad pasan tal cual; los escritos a la LaTeX se convierten.
    ("Integración por partes: área bajo la curva", "Integración por partes: área bajo la curva"),
    (r"Funci\'on de \~nandu", "Función de ñandu"),
    (r"Funci\'{o}n e \'{\i}ndice", "Función e índice"),
    (r"``citado''", "“citado”"),
    ("  muchos    espacios \n y saltos ", "muchos espacios y saltos"),
])
def test_limpiar_titulo(crudo, limpio):
    assert limpiar_titulo(crudo) == limpio


@pytest.mark.parametrize("crudo", ["", None, "$$", "{}", r"\emph{}", "  ", r"$\,$"])
def test_limpiar_titulo_vacio(crudo):
    assert limpiar_titulo(crudo) == ""


@pytest.mark.parametrize("texto,largo,cortado", [
    ("hola mundo", 10, "hola mundo"),  # entra: no se toca
    ("uno dos tres", 7, "uno dos"),
    ("uno dos tres", 8, "uno dos"),
    ("uno dos, tres", 8, "uno dos"),  # sin la coma colgando
    ("Opciones financieras y el camino a Black-Scholes con volatilidad", 40,
     "Opciones financieras y el camino a"),
    # Una sola palabra más larga que el límite: no hay dónde cortar mejor.
    ("Supercalifragilisticoespialidoso corto", 10, "Supercalif"),
])
def test_cortar_en_palabra(texto, largo, cortado):
    assert cortar_en_palabra(texto, largo) == cortado


def test_cortar_en_palabra_nunca_deja_media_palabra():
    texto = "Valuación de opciones árboles binomiales el modelo continuo y el camino a Black-Scholes"
    palabras = texto.split()
    for largo in range(30, len(texto) + 1):
        corto = cortar_en_palabra(texto, largo)
        assert len(corto) <= largo
        assert corto.split() == palabras[:len(corto.split())], (largo, corto)


def test_titulo_carpeta_limpia_y_corta_en_palabra():
    crudo = r"Valuación de opciones: árboles binomiales, el modelo continuo y el camino a Black--Scholes"
    t = titulo_carpeta(crudo)
    assert len(t) <= LARGO_TITULO
    assert t == "Valuación de opciones árboles binomiales, el modelo continuo"
    assert "Schole" not in t and ":" not in t


def test_titulo_carpeta_valido_en_windows_y_sin_titulo():
    assert titulo_carpeta('Qué es "f(x)"? <a|b> / c *') == "Qué es f(x) ab c"
    assert titulo_carpeta("") == SIN_TITULO
    assert titulo_carpeta(None) == SIN_TITULO
    assert titulo_carpeta("$$") == SIN_TITULO
    assert titulo_carpeta('???') == SIN_TITULO  # nada válido en Windows: con tilde, como el resto
    assert titulo_carpeta('"<>"') == SIN_TITULO
    assert titulo_carpeta("Fin con punto.") == "Fin con punto"  # Windows no acepta el punto final


def test_nombre_carpeta():
    assert nombre_carpeta("2026-10-15", 7, r"Gradiente y $\nabla f$") == "2026-10-15 Clase 07 - Gradiente y ∇ f"
    assert nombre_carpeta("2026-10-15", 12, "Black--Scholes", compilo=False) == (
        "2026-10-15 Clase 12 - Black-Scholes (no compiló)")
    assert nombre_carpeta("2026-10-15", None, "") == "2026-10-15 Clase 00 - Sin título"


def test_subindices_y_operadores_en_modo_matematico():
    assert limpiar_titulo(r"Límite~$\lim_{x \to 0}$ y $\sup A$") == "Límite lim x → 0 y sup A"
    assert limpiar_titulo(r"archivo\_final y $x_1$") == "archivo_final y x 1"   # el \_ escapado se queda


def test_corta_en_palabra_aunque_el_unico_espacio_este_al_principio():
    assert cortar_en_palabra("Hacia Black-Scholes", 15) == "Hacia"
    assert cortar_en_palabra("Unapalabramuylarga x", 10) == "Unapalabra"   # sin espacios antes: corte duro


@pytest.mark.parametrize("crudo", [
    r"Bonos a \$100 y el conjunto \{x\}", r"Black--Scholes y $\sigma$", r"\textbf{Teorema} de Fubini --- versión",
    "Costos de $5 y $10", r"Límite~$\lim_{x \to 0}$", "Ya está limpio: σ-álgebras — parte 2",
])
def test_limpiar_dos_veces_da_lo_mismo(crudo):
    una = limpiar_titulo(crudo)
    assert limpiar_titulo(una) == una


def test_los_literales_escapados_sobreviven_a_la_segunda_limpieza():
    assert limpiar_titulo(limpiar_titulo(r"Bonos a \$100 y el conjunto \{x\}")) == "Bonos a $100 y el conjunto {x}"
    assert limpiar_titulo("Costos de $5 y $10") == "Costos de $5 y $10"
