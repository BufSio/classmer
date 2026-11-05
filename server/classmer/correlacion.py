"""Desfase entre dos grabaciones de la misma clase por correlación de los audios (§15).

Sirve para "Agregar audio del celu" desde el panel: un audio grabado con otra app no trae el desfase
que mide la app con Ambos. `desfase_ms` es el momento del audio principal en que empieza el otro
(puede ser negativo): un instante t del otro audio es t + desfase_ms en el principal.

Cómo:
1. ffmpeg arma la envolvente de energía de cada audio a 100 Hz (mono, el cuadrado de la señal
   remuestreado: el filtro del remuestreo hace de promedio).
2. Se pasa a logaritmo y se normaliza (media 0, desvío 1): así pesan igual la voz cerca y lejos.
3. Correlación cruzada a 10 Hz con una FFT (en Python puro: sin dependencias nuevas), sobre todos los
   desfases posibles.
4. Se refina a 100 Hz (10 ms) cerca del pico, sólo con la parte que se solapa.

`confianza` compara el pico con el segundo mejor lejos de él (≥ 1,5 suele ser claro) y `pico` es la
correlación normalizada ahí. Con poca superposición, un pico chato o bajo, lanza ErrorCorrelacion.
"""

from __future__ import annotations

import cmath
import math
import os
import tempfile
from array import array
from dataclasses import dataclass
from pathlib import Path

from classmer import audio

HZ = 100                 # envolvente fina
DIEZMADO = 10            # 100 Hz → 10 Hz para la búsqueda gruesa
REFINAR_S = 1.0          # alrededor del pico grueso, ± esto a 100 Hz
EXCLUSION_S = 5.0        # el segundo pico se busca a más de esto del primero
SOLAPE_MIN_S = 60.0      # menos superposición que esto no alcanza para decidir (o la mitad del más corto)
SOLAPE_PISO_S = 10.0
CONFIANZA_MIN = 1.15
PICO_MIN = 0.2           # correlación normalizada mínima en el pico (una clase real dio 0,84)
PISO = 1e-7              # energía mínima (silencio digital) antes del logaritmo


class ErrorCorrelacion(Exception):
    """No se pudo medir el desfase (audio ilegible, muy corto o sin un pico claro)."""


@dataclass
class Desfase:
    desfase_ms: int
    confianza: float
    pico: float              # correlación normalizada en el pico (−1..1)
    solape_s: float

    def como_dict(self) -> dict[str, float | int]:
        return {"desfase_ms": self.desfase_ms, "confianza": round(self.confianza, 3),
                "pico": round(self.pico, 4), "solape_s": round(self.solape_s, 1)}


# --- envolvente ----------------------------------------------------------------------------------

def envolvente(ruta: Path, *, timeout: float | None = 1800) -> list[float]:
    """Energía del audio a HZ muestras por segundo (lista de floats)."""
    fd, tmp = tempfile.mkstemp(suffix=".f32")
    os.close(fd)
    try:
        r = audio.ejecutar(["ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-y", "-i", str(ruta),
                            "-map", "0:a:0", "-ac", "1", "-af", f"aresample=8000,aeval=val(0)*val(0),aresample={HZ}",
                            "-f", "f32le", tmp], timeout=timeout)
        if r.returncode != 0:
            raise ErrorCorrelacion(f"ffmpeg no pudo leer {ruta.name}: {r.stderr.strip()[-300:]}")
        datos = array("f")
        with open(tmp, "rb") as f:
            datos.frombytes(f.read())
        return list(datos)
    except audio.ErrorAudio as e:
        raise ErrorCorrelacion(str(e)) from e
    finally:
        Path(tmp).unlink(missing_ok=True)


def normalizar(energia: list[float]) -> list[float]:
    """log10 de la energía, con media 0 y desvío 1."""
    if not energia:
        return []
    valores = [math.log10(max(x, 0.0) + PISO) for x in energia]
    media = sum(valores) / len(valores)
    desvio = math.sqrt(sum((v - media) ** 2 for v in valores) / len(valores)) or 1.0
    return [(v - media) / desvio for v in valores]


def diezmar(x: list[float], factor: int) -> list[float]:
    return [sum(x[i:i + factor]) / len(x[i:i + factor]) for i in range(0, len(x), factor)]


# --- FFT (radix 2, iterativa) --------------------------------------------------------------------

def _fft(a: list[complex], inversa: bool = False) -> list[complex]:
    n = len(a)
    a = list(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    largo = 2
    signo = 1 if inversa else -1
    while largo <= n:
        w_largo = cmath.exp(signo * 2j * math.pi / largo)
        mitad = largo // 2
        ws = [1 + 0j] * mitad
        for k in range(1, mitad):
            ws[k] = ws[k - 1] * w_largo
        for inicio in range(0, n, largo):
            for k in range(mitad):
                u = a[inicio + k]
                v = a[inicio + k + mitad] * ws[k]
                a[inicio + k] = u + v
                a[inicio + k + mitad] = u - v
        largo <<= 1
    if inversa:
        a = [x / n for x in a]
    return a


def correlacion_cruzada(principal: list[float], otro: list[float]) -> dict[int, float]:
    """{lag: Σ principal[i + lag] · otro[i]} para todos los lags (en muestras), con una FFT."""
    total = len(principal) + len(otro)
    n = 1
    while n < total:
        n <<= 1
    fa = _fft([complex(x) for x in principal] + [0j] * (n - len(principal)))
    fb = _fft([complex(x) for x in otro] + [0j] * (n - len(otro)))
    producto = [x * y.conjugate() for x, y in zip(fa, fb, strict=True)]
    r = _fft(producto, inversa=True)
    salida: dict[int, float] = {}
    for lag in range(-(len(otro) - 1), len(principal)):
        salida[lag] = r[lag % n].real
    return salida


def _solape(lag: int, n_principal: int, n_otro: int) -> int:
    """Cuántas muestras se superponen con el otro corrido `lag` muestras en el principal."""
    return max(0, min(n_principal, lag + n_otro) - max(0, lag))


def _normalizada(valor: float, lag: int, n_principal: int, n_otro: int) -> float:
    solape = _solape(lag, n_principal, n_otro)
    return valor / solape if solape else 0.0


def _producto(principal: list[float], otro: list[float], lag: int) -> tuple[float, int]:
    desde = max(0, -lag)
    hasta = min(len(otro), len(principal) - lag)
    if hasta <= desde:
        return 0.0, 0
    return sum(principal[i + lag] * otro[i] for i in range(desde, hasta)), hasta - desde


# --- medir -----------------------------------------------------------------------------------------

def medir_envolventes(principal: list[float], otro: list[float]) -> Desfase:
    """El desfase entre dos envolventes ya normalizadas (a HZ)."""
    a, b = diezmar(principal, DIEZMADO), diezmar(otro, DIEZMADO)
    piso = int(SOLAPE_PISO_S * HZ / DIEZMADO)
    solape_min = max(piso, min(int(SOLAPE_MIN_S * HZ / DIEZMADO), min(len(a), len(b)) // 2))
    if min(len(a), len(b)) < piso:
        raise ErrorCorrelacion("los audios son demasiado cortos para medir el desfase")
    corr = correlacion_cruzada(a, b)
    puntajes = {lag: _normalizada(v, lag, len(a), len(b)) for lag, v in corr.items()
                if _solape(lag, len(a), len(b)) >= solape_min}
    if not puntajes:
        raise ErrorCorrelacion("los audios casi no se superponen")
    mejor = max(puntajes, key=puntajes.__getitem__)
    exclusion = int(EXCLUSION_S * HZ / DIEZMADO)
    segundo = max((v for lag, v in puntajes.items() if abs(lag - mejor) > exclusion), default=0.0)
    confianza = puntajes[mejor] / segundo if segundo > 0 else float("inf")

    # Refinar a HZ alrededor del pico grueso.
    centro = mejor * DIEZMADO
    radio = int(REFINAR_S * HZ) + DIEZMADO
    fino, valor_fino, solape = centro, -math.inf, 0
    for lag in range(centro - radio, centro + radio + 1):
        suma, n = _producto(principal, otro, lag)
        if n and suma / n > valor_fino:
            fino, valor_fino, solape = lag, suma / n, n
    if confianza < CONFIANZA_MIN or valor_fino < PICO_MIN:
        raise ErrorCorrelacion(f"no hay un desfase claro (confianza {confianza:.2f}, pico {valor_fino:.2f}): "
                               "¿son la misma clase?")
    return Desfase(desfase_ms=int(round(fino * 1000 / HZ)), confianza=min(confianza, 99.0),
                   pico=valor_fino, solape_s=solape / HZ)


def medir(principal: Path, otro: Path) -> Desfase:
    """El desfase de `otro` respecto de `principal` (ms; un instante t de `otro` es t + desfase_ms en
    el principal)."""
    return medir_envolventes(normalizar(envolvente(principal)), normalizar(envolvente(otro)))
