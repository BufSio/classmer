"""Genera fotos "de pizarrón" de prueba con EXIF DateTimeOriginal = inicio + offset.

Sirve para probar el importador y la línea de tiempo sin ir a clase. Las fotos siguen la clase
del guion (derivada direccional y gradiente): definición, gradiente, el ejemplo con números y un
dibujo de curvas de nivel. Se van alternando pizarrón verde y pizarra blanca.

Uso (desde server/):
    uv run python scripts/generar_fotos_prueba.py --salida X --inicio 2026-10-15T14:00:00-03:00 --offsets 60,180,300

Los offsets van en segundos desde el inicio (también acepta MM:SS o HH:MM:SS). El mtime de cada
archivo queda igual a la hora de la foto, como si la hubiera escrito la cámara.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from classmer.util import parsear_iso, tiempo_a_ms

ANCHO, ALTO = 1600, 1200
FUENTES_WIN = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
# Letra "a mano" primero; Cambria y Segoe UI Symbol tienen ∇, ‖, ⊥, ℝ, ∈…
CANDIDATAS = [("segoeprb.ttf", 0), ("segoepr.ttf", 0), ("cambria.ttc", 0), ("seguisym.ttf", 0), ("arial.ttf", 0)]
ESCALA_RESPALDO = 1.12   # Cambria y Segoe UI Symbol son más chicas que Segoe Print

PALETAS = {
    "verde": {"fondo": (34, 66, 48), "marco": (120, 86, 52), "tiza": (238, 240, 228),
              "acento": (250, 214, 110), "rojo": (240, 140, 130), "borron": (255, 255, 255, 20)},
    "blanco": {"fondo": (246, 246, 240), "marco": (170, 172, 176), "tiza": (28, 52, 130),
               "acento": (25, 25, 25), "rojo": (190, 35, 30), "borron": (120, 140, 180, 14)},
}


# --- texto con fallback de fuentes y mini-markup (_{sub}, ^{sup}) --------------------------------
# Todas las coordenadas y de texto son la línea base (anchor "ls"): así se alinean fuentes distintas.

class Fuentes:
    def __init__(self) -> None:
        self._cache: dict[tuple[int, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
        self._glifos: dict[tuple[int, str], bool] = {}
        self.disponibles = [(FUENTES_WIN / n, i) for n, i in CANDIDATAS if (FUENTES_WIN / n).is_file()]

    def _fuente(self, k: int, tamano: int):
        clave = (k, tamano)
        if clave not in self._cache:
            if self.disponibles:
                ruta, indice = self.disponibles[k]
                self._cache[clave] = ImageFont.truetype(str(ruta), tamano, index=indice)
            else:
                self._cache[clave] = ImageFont.load_default(size=tamano)
        return self._cache[clave]

    def para(self, c: str, tamano: int):
        """Primera fuente que tiene el glifo (comparando contra el .notdef)."""
        if not self.disponibles:
            return self._fuente(0, tamano)
        for k in range(len(self.disponibles)):
            f = self._fuente(k, tamano if k == 0 else round(tamano * ESCALA_RESPALDO))
            if c.isspace() or self._tiene(k, f, c):
                return f
        return self._fuente(0, tamano)

    def _tiene(self, k: int, fuente, c: str) -> bool:
        clave = (k, c)
        if clave not in self._glifos:
            self._glifos[clave] = _render(fuente, c) != _render(fuente, "\uffff")
        return self._glifos[clave]


def _render(fuente, c: str) -> bytes:
    im = Image.new("L", (160, 160))
    ImageDraw.Draw(im).text((16, 120), c, font=fuente, fill=255, anchor="ls")
    return im.tobytes()


def _tramos(texto: str) -> list[tuple[str, str]]:
    """'x^{2} + f_{x}' → [('n','x'), ('sup','2'), ('n',' + f'), ('sub','x')]."""
    res, i, actual = [], 0, ""
    while i < len(texto):
        if texto[i] in "_^" and texto[i + 1:i + 2] == "{":
            fin = texto.find("}", i)
            if fin > 0:
                if actual:
                    res.append(("n", actual))
                    actual = ""
                res.append(("sub" if texto[i] == "_" else "sup", texto[i + 2:fin]))
                i = fin + 1
                continue
        actual += texto[i]
        i += 1
    if actual:
        res.append(("n", actual))
    return res


def _partes(texto: str, tamano: int):
    for tipo, parte in _tramos(texto):
        if tipo == "n":
            yield parte, tamano, 0.0
        else:
            yield parte, int(tamano * 0.62), (0.2 if tipo == "sub" else -0.45) * tamano


def escribir(draw: ImageDraw.ImageDraw, x: float, base: float, texto: str, fuentes: Fuentes,
             color, tamano: int, temblor: float = 1.5) -> float:
    """Dibuja letra por letra (con temblor de tiza) sobre la línea base; devuelve el ancho."""
    x0 = x
    for parte, t, dy in _partes(texto, tamano):
        for c in parte:
            f = fuentes.para(c, t)
            draw.text((x, base + dy + random.uniform(-temblor, temblor)), c, font=f, fill=color, anchor="ls")
            x += f.getlength(c)
    return x - x0


def ancho(texto: str, fuentes: Fuentes, tamano: int) -> float:
    return sum(fuentes.para(c, t).getlength(c) for parte, t, _ in _partes(texto, tamano) for c in parte)


def fraccion(draw, x: float, base: float, num: str, den: str, fuentes: Fuentes, color, tamano: int) -> float:
    """Fracción con raya centrada en el eje matemático de la línea. Devuelve el ancho."""
    an = max(ancho(num, fuentes, tamano), ancho(den, fuentes, tamano)) + 24
    eje = base - 0.33 * tamano
    escribir(draw, x + (an - ancho(num, fuentes, tamano)) / 2, eje - 0.2 * tamano, num, fuentes, color, tamano)
    draw.line([(x, eje), (x + an, eje)], fill=color, width=4)
    escribir(draw, x + (an - ancho(den, fuentes, tamano)) / 2, eje + 0.95 * tamano, den, fuentes, color, tamano)
    return an


def flecha(draw, a: tuple[float, float], b: tuple[float, float], color, ancho_linea: int = 6) -> None:
    draw.line([a, b], fill=color, width=ancho_linea)
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    for s in (+1, -1):
        p = (b[0] - 34 * math.cos(ang + s * 0.42), b[1] - 34 * math.sin(ang + s * 0.42))
        draw.line([b, p], fill=color, width=ancho_linea)


def titulo(d, texto: str, f: Fuentes, p, base: float = 190) -> None:
    an = escribir(d, 150, base, texto, f, p["acento"], 72)
    d.line([(145, base + 22), (160 + an, base + 26)], fill=p["acento"], width=4)


# --- páginas del pizarrón (y = línea base) -----------------------------------------------------

def pagina_definicion(d, f: Fuentes, p) -> None:
    titulo(d, "Derivada direccional", f, p)
    escribir(d, 150, 340, "Sea f: ℝ^{2} → ℝ,  p ∈ ℝ^{2},  u con ‖u‖ = 1", f, p["tiza"], 54)
    base = 590
    x = 150 + escribir(d, 150, base, "D_{u}f(p) = ", f, p["tiza"], 66)
    an_lim = escribir(d, x, base, "lím", f, p["tiza"], 66)
    escribir(d, x + an_lim / 2 - ancho("t→0", f, 40) / 2, base + 62, "t→0", f, p["tiza"], 40)
    fraccion(d, x + an_lim + 30, base, "f(p + t·u) − f(p)", "t", f, p["tiza"], 62)
    d.rounded_rectangle([(140, 840), (1180, 980)], radius=24, outline=p["rojo"], width=5)
    escribir(d, 175, 935, "¡Ojo! u tiene que ser unitario", f, p["rojo"], 56)


def pagina_gradiente(d, f: Fuentes, p) -> None:
    titulo(d, "Gradiente", f, p)
    escribir(d, 150, 370, "∇f(p) = ( f_{x}(p) , f_{y}(p) )", f, p["tiza"], 64)
    escribir(d, 150, 520, "Si f es diferenciable en p:", f, p["tiza"], 54)
    escribir(d, 240, 665, "D_{u}f(p) = ∇f(p) · u", f, p["tiza"], 66)
    d.rounded_rectangle([(200, 580), (1000, 710)], radius=20, outline=p["rojo"], width=5)
    escribir(d, 150, 850, "máx D_{u}f(p) = ‖∇f(p)‖   (u en la dirección de ∇f)", f, p["tiza"], 50)
    escribir(d, 150, 1000, "esto no lo demuestro (ver apunte)", f, p["rojo"], 46)


def pagina_ejemplo(d, f: Fuentes, p) -> None:
    titulo(d, "Ejemplo", f, p)
    escribir(d, 150, 320, "f(x, y) = x^{2} + 3xy", f, p["tiza"], 58)
    escribir(d, 900, 320, "p = (1, 2)", f, p["tiza"], 58)
    base = 470
    x = 150 + escribir(d, 150, base, "u = (", f, p["tiza"], 58)
    x += fraccion(d, x, base, "3", "5", f, p["tiza"], 50)
    x += escribir(d, x, base, " , ", f, p["tiza"], 58)
    x += fraccion(d, x, base, "4", "5", f, p["tiza"], 50)
    escribir(d, x, base, ")", f, p["tiza"], 58)
    for base, izq, der in ((630, "f_{x} = 2x + 3y", "f_{x}(1, 2) = 8"), (740, "f_{y} = 3x", "f_{y}(1, 2) = 3")):
        escribir(d, 150, base, izq, f, p["tiza"], 56)
        escribir(d, 640, base, "→", f, p["tiza"], 56)
        escribir(d, 760, base, der, f, p["tiza"], 56)
    escribir(d, 150, 860, "∇f(1, 2) = (8, 3)", f, p["tiza"], 60)
    escribir(d, 170, 1010, "D_{u}f(1, 2) = 8 · 3/5 + 3 · 4/5 = 36/5", f, p["acento"], 60)
    d.rounded_rectangle([(130, 935), (1340, 1060)], radius=22, outline=p["rojo"], width=5)


def pagina_dibujo(d, f: Fuentes, p) -> None:
    titulo(d, "Curvas de nivel y ∇f", f, p)
    cx, cy = 560, 690
    for k, (a, b) in enumerate([(150, 95), (260, 165), (370, 235), (480, 305)]):
        d.ellipse([(cx - a, cy - b), (cx + a, cy + b)], outline=p["tiza"], width=4)
        escribir(d, cx + a * 0.71 + 8, cy + b * 0.71 + 40, f"c_{{{k + 1}}}", f, p["tiza"], 38)
    a, b, th = 370, 235, math.radians(35)
    px, py = cx + a * math.cos(th), cy - b * math.sin(th)
    nx, ny = math.cos(th) / a, math.sin(th) / b          # normal hacia afuera (coordenadas matemáticas)
    largo = math.hypot(nx, ny)
    nx, ny = nx / largo, ny / largo
    fin = (px + 230 * nx, py - 230 * ny)
    flecha(d, (px, py), fin, p["rojo"])
    tx, ty = -ny, -nx                                    # tangente, ya en coordenadas de pantalla
    for s in range(-6, 6, 2):
        d.line([(px + s * 40 * tx, py + s * 40 * ty), (px + (s + 1) * 40 * tx, py + (s + 1) * 40 * ty)],
               fill=p["tiza"], width=3)
    d.ellipse([(px - 10, py - 10), (px + 10, py + 10)], fill=p["acento"])
    escribir(d, px - 55, py + 55, "p", f, p["acento"], 50)
    escribir(d, fin[0] + 15, fin[1] + 10, "∇f(p)", f, p["rojo"], 54)
    escribir(d, 1080, 640, "∇f ⊥ curva de nivel", f, p["tiza"], 46)
    d.rounded_rectangle([(1050, 880), (1520, 1060)], radius=20, outline=p["rojo"], width=6)
    escribir(d, 1080, 960, "PARCIAL 5/11", f, p["rojo"], 50)
    escribir(d, 1080, 1030, "entra todo esto", f, p["rojo"], 42)


PAGINAS: list[tuple[Callable, str]] = [
    (pagina_definicion, "verde"),
    (pagina_gradiente, "blanco"),
    (pagina_ejemplo, "verde"),
    (pagina_dibujo, "blanco"),
]


def dibujar_pizarron(n: int, fondo: str | None = None, semilla: int = 0) -> Image.Image:
    random.seed(semilla * 1000 + n)
    pagina, fondo_default = PAGINAS[n % len(PAGINAS)]
    p = PALETAS[fondo or fondo_default]
    img = Image.new("RGB", (ANCHO, ALTO), (205, 200, 190))       # pared
    d = ImageDraw.Draw(img)
    d.rectangle([(40, 40), (ANCHO - 40, ALTO - 40)], fill=p["marco"])
    d.rectangle([(70, 70), (ANCHO - 70, ALTO - 70)], fill=p["fondo"])
    # borrones de pasadas anteriores
    capa = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dc = ImageDraw.Draw(capa)
    for _ in range(9):
        x, y = random.randint(100, ANCHO - 300), random.randint(100, ALTO - 250)
        dc.ellipse([(x, y), (x + random.randint(180, 520), y + random.randint(60, 160))], fill=p["borron"])
    img = Image.alpha_composite(img.convert("RGBA"), capa).convert("RGB")
    pagina(ImageDraw.Draw(img), Fuentes(), p)
    # grano de foto y una inclinación leve
    ruido = Image.effect_noise(img.size, 28).convert("RGB")
    img = Image.blend(img, ruido, 0.07)
    return img.rotate(random.uniform(-2.2, 2.2), resample=Image.Resampling.BICUBIC, fillcolor=(205, 200, 190))


# --- EXIF y main -------------------------------------------------------------------------------

def exif_para(hora: datetime, con_zona: bool) -> Image.Exif:
    texto = hora.strftime("%Y:%m:%d %H:%M:%S")
    exif = Image.Exif()
    exif[0x010F] = "Classmer"                 # Make
    exif[0x0110] = "generar_fotos_prueba"     # Model
    exif[0x0112] = 1                          # Orientation
    exif[0x0132] = texto                      # DateTime
    sub: dict[int, str] = {36867: texto, 36868: texto}
    if con_zona:
        off = hora.utcoffset() or timedelta(0)
        minutos = int(off.total_seconds() // 60)
        signo = "-" if minutos < 0 else "+"
        zona_txt = f"{signo}{abs(minutos) // 60:02d}:{abs(minutos) % 60:02d}"
        sub.update({36880: zona_txt, 36881: zona_txt, 36882: zona_txt})
    exif[0x8769] = sub
    return exif


def generar(salida: Path, inicio: datetime, offsets_ms: list[int], *, con_exif: bool = True,
            con_zona: bool = True, fondo: str | None = None) -> list[Path]:
    salida.mkdir(parents=True, exist_ok=True)
    rutas: list[Path] = []
    for n, off in enumerate(offsets_ms):
        hora = (inicio + timedelta(milliseconds=off)).astimezone(inicio.tzinfo)
        ruta = salida / hora.strftime("%Y%m%d_%H%M%S.jpg")
        k = 1
        while ruta.exists() or ruta in rutas:
            ruta = salida / hora.strftime(f"%Y%m%d_%H%M%S_{k}.jpg")
            k += 1
        img = dibujar_pizarron(n, fondo)
        extra = {"exif": exif_para(hora, con_zona)} if con_exif else {}
        img.save(ruta, "JPEG", quality=85, **extra)
        os.utime(ruta, (hora.timestamp(), hora.timestamp()))
        rutas.append(ruta)
        print(f"{ruta.name}  offset {off / 1000:>7.1f} s  hora {hora.isoformat(timespec='seconds')}")
    return rutas


def main(argv: list[str] | None = None) -> int:
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Fotos de pizarrón de prueba con hora EXIF")
    ap.add_argument("--salida", required=True, help="carpeta donde dejar las fotos")
    ap.add_argument("--inicio", required=True,
                    help="inicio de la grabación, ISO 8601 (p. ej. 2026-10-15T14:00:00-03:00)")
    ap.add_argument("--offsets", required=True, help="segundos desde el inicio, separados por coma (60,180,300)")
    ap.add_argument("--zona", default="America/Argentina/Buenos_Aires", help="zona si --inicio viene sin offset")
    ap.add_argument("--sin-zona", action="store_true", help="no escribir OffsetTimeOriginal (hora local pelada)")
    ap.add_argument("--sin-exif", action="store_true", help="sin EXIF: la hora queda sólo en el mtime")
    ap.add_argument("--fondo", choices=sorted(PALETAS), help="forzar pizarrón verde o pizarra blanca")
    args = ap.parse_args(argv)
    try:
        inicio = parsear_iso(args.inicio, args.zona)
        offsets = [tiempo_a_ms(x) for x in args.offsets.split(",") if x.strip()]
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    generar(Path(args.salida), inicio, offsets, con_exif=not args.sin_exif,
            con_zona=not args.sin_zona, fondo=args.fondo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
