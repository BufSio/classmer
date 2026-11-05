"""Facturación de Claude (§7): API key o plan Max, con respaldo del otro y memoria del estado.

Vale para todo lo que Classmer le pide a Claude (clasificar, apunte y lo que se agregue). El modo
sale de `claude.facturacion` en config.yaml:

- `api`: sólo la API key (ANTHROPIC_API_KEY de Classmer-data\\.env).
- `max`: sólo el plan Max (el login de Claude Code; lo que se hacía siempre).
- `api_luego_max` (por defecto): la API; si se queda sin créditos o la clave es inválida, el plan Max.
- `max_luego_api`: el plan Max; si se agota su cupo (o no hay login), la API.

Sin clave, los modos con API funcionan como `max` (y el panel lo avisa).

Sólo se cambia de medio por falta de créditos o de cupo, o por una clave (o un login) ausente o
inválido: eso es `SinCreditos`. Lo pasajero (429, 5xx, sobrecarga, sin red) es `CuotaAgotada` y
sigue la política de posponer de siempre, sin cambiar de medio. El cambio rehace el paso entero
(`ejecutar` vuelve a llamar a `correr` con el otro medio).

Memoria (tabla `facturacion`): después de un error de créditos la API queda "sin créditos" con la
hora, y las tareas siguientes van directo al otro medio. Se vuelve a probar pasadas
`claude.reintentar_api_h` horas, si cambia la clave del .env (se guarda una huella: los primeros
8 hex del SHA-256, nunca caracteres de la clave) o con "Reintentar API" en el panel. El cupo agotado
del plan Max se recuerda hasta que se libera (resets_at del CLI, o 30 min).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, TypeVar

from classmer.pipeline import CuotaAgotada
from classmer.util import zona

if TYPE_CHECKING:
    from classmer.config import Config
    from classmer.db import Base

log = logging.getLogger("classmer.facturacion")

API, MAX = "api", "max"
MEDIOS = (API, MAX)
NOMBRE_MEDIO = {API: "la API", MAX: "el plan Max"}
A_MEDIO = {API: "a la API", MAX: "al plan Max"}     # "voy directo al plan Max"
ETIQUETA_MEDIO = {API: "API", MAX: "plan Max"}
# Estados persistidos (la falta de fila es "ok"); "sin_clave" no se guarda: sale de la config.
ESTADOS_API = ("sin_creditos", "clave_invalida")
ESTADOS_MAX = ("sin_cupo", "sin_login")
TEXTO_ESTADO = {
    "ok": "ok",
    "sin_clave": "sin clave",
    "sin_creditos": "API sin créditos",
    "clave_invalida": "la API key no es válida",
    "sin_cupo": "plan Max sin cupo",
    "sin_login": "Claude Code sin sesión iniciada (plan Max)",
}
ESPERA_MAX_DEFAULT_S = 1800      # cupo del plan sin dato de cuándo se libera
ESPERA_MIN_S = 60
ESPERA_TOPE_S = 6 * 3600
MARGEN_MAX_S = 60                # después del resets_at del plan, como agente_apunte.espera_cuota
TAREAS_CLAUDE = ("apunte", "clasificar", "revisar_transcript")   # jobs que se despiertan con "Reintentar API"

T = TypeVar("T")


class SinCreditos(CuotaAgotada):
    """El medio de facturación no sirve ahora: sin créditos, clave inválida, cupo del plan agotado o
    sin login. Es una CuotaAgotada: si nadie la maneja, el job se pospone (como antes)."""

    def __init__(self, medio: str, estado: str, motivo: str, *, espera_s: float = ESPERA_MAX_DEFAULT_S,
                 hasta: float | None = None):
        if medio not in MEDIOS:
            raise ValueError(f"medio desconocido: {medio!r}")
        super().__init__(f"{TEXTO_ESTADO.get(estado, estado)}: {motivo}", espera_s=espera_s)
        self.medio = medio
        self.estado = estado
        self.motivo = motivo
        self.hasta = hasta


def huella(clave: str | None) -> str | None:
    """Los primeros 8 hex del SHA-256 de la clave: sirve para saber si cambió, no para recuperarla."""
    if not clave:
        return None
    return hashlib.sha256(clave.encode("utf-8")).hexdigest()[:8]


@dataclass
class EstadoMedio:
    medio: str
    estado: str                 # ok | sin_clave | sin_creditos | clave_invalida | sin_cupo | sin_login
    motivo: str | None = None
    desde: float | None = None  # epoch del error
    hasta: float | None = None  # epoch desde el que se vuelve a probar
    disponible: bool = True     # ¿entra en el plan ahora?
    nota: str | None = None     # p. ej. "la clave cambió: se vuelve a probar"

    @property
    def ok(self) -> bool:
        return self.estado == "ok"

    @property
    def texto(self) -> str:
        return TEXTO_ESTADO.get(self.estado, self.estado)


def _hora(epoch: float | None, nombre_zona: str, ahora: float | None = None) -> str:
    if not epoch:
        return "?"
    dt = datetime.fromtimestamp(epoch, zona(nombre_zona))
    hoy = datetime.fromtimestamp(time.time() if ahora is None else ahora, zona(nombre_zona)).date()
    return dt.strftime("%H:%M") if dt.date() == hoy else dt.strftime("%d/%m %H:%M")


def _minutos(segundos: float) -> str:
    minutos = max(1, round(segundos / 60))
    return f"{minutos} min" if minutos < 120 else f"{minutos / 60:.1f} h".replace(".0 h", " h")


class Facturacion:
    """Decide con qué medio se le pide algo a Claude y recuerda los que no andan.

    `base` se puede omitir (se abre la de la config): el clasificador sólo recibe la config.
    """

    def __init__(self, cfg: Config, base: Base | None = None, *, reloj: Callable[[], float] = time.time):
        self.cfg = cfg
        if base is None:
            from classmer.db import Base as _Base

            base = _Base(cfg.db_path)
        self.base = base
        self.reloj = reloj

    # --- lectura ---
    @property
    def modo(self) -> str:
        return self.cfg.claude.facturacion

    @property
    def hay_clave(self) -> bool:
        return bool(self.cfg.anthropic_api_key)

    def medios_del_modo(self) -> list[str]:
        """Los medios del modo en orden, sin mirar la memoria. Sin clave, sólo el plan Max."""
        orden = {"api": [API], "max": [MAX], "api_luego_max": [API, MAX], "max_luego_api": [MAX, API]}
        medios = list(orden.get(self.modo, [API, MAX]))
        if not self.hay_clave:
            medios = [m for m in medios if m != API] or [MAX]
        return medios

    def _fila(self, medio: str) -> sqlite3.Row | None:
        try:
            with self.base.conexion() as conn:
                return conn.execute("SELECT * FROM facturacion WHERE medio = ?", (medio,)).fetchone()
        except sqlite3.OperationalError:  # base sin la tabla (no pasó por abrir): nada recordado
            return None

    def estado(self, medio: str) -> EstadoMedio:
        ahora = self.reloj()
        if medio == API and not self.hay_clave:
            return EstadoMedio(API, "sin_clave", "no hay ANTHROPIC_API_KEY en Classmer-data\\.env",
                               disponible=False)
        fila = self._fila(medio)
        if fila is None:
            return EstadoMedio(medio, "ok")
        e = EstadoMedio(medio, fila["estado"], fila["motivo"], fila["desde"], fila["hasta"])
        if medio == API:
            e.hasta = (fila["desde"] or ahora) + self.cfg.claude.reintentar_api_h * 3600
            # Sin huella guardada = falló sin clave (p. ej. la sacaron entre el plan y la corrida):
            # ahora que hay una, también es "otra clave".
            if fila["huella_clave"] != huella(self.cfg.anthropic_api_key):
                e.disponible, e.nota = True, "la clave cambió desde el error: se vuelve a probar"
                return e
        elif e.hasta is None:  # sin dato de cuándo se libera: no queda trabado para siempre
            e.hasta = (fila["desde"] or ahora) + ESPERA_MAX_DEFAULT_S
        if ahora >= e.hasta:
            e.disponible, e.nota = True, "ya pasó el tiempo de espera: se vuelve a probar"
        else:
            e.disponible = False
        return e

    def estados(self) -> dict[str, EstadoMedio]:
        return {m: self.estado(m) for m in MEDIOS}

    def describir(self, e: EstadoMedio) -> str:
        """'API sin créditos desde 14:05 (se vuelve a probar a las 20:05)' y similares (panel y logs)."""
        z, ahora = self.cfg.zona_horaria, self.reloj()
        if e.estado in ("ok", "sin_clave"):
            return e.texto
        texto = f"{e.texto} desde {_hora(e.desde, z, ahora)}"
        if e.nota:
            return f"{texto} ({e.nota})"
        if e.hasta:
            texto += f" (se vuelve a probar a las {_hora(e.hasta, z, ahora)})"
        return texto

    def plan(self) -> tuple[list[str], list[str]]:
        """(medios a probar ahora, en orden; avisos para el log). Saltea los que la memoria dice que
        no andan. Puede quedar vacío: ver `espera`."""
        medios = self.medios_del_modo()
        avisos: list[str] = []
        if not self.hay_clave and self.modo != "max":
            avisos.append("no hay ANTHROPIC_API_KEY en Classmer-data\\.env: uso el plan Max")
        estados = {m: self.estado(m) for m in medios}
        disponibles = [m for m in medios if estados[m].disponible]
        for m in medios:
            if estados[m].disponible:
                if not estados[m].ok:
                    avisos.append(f"{self.describir(estados[m])}")
                continue
            siguiente = next((d for d in disponibles if medios.index(d) > medios.index(m)), None)
            if siguiente is not None:
                avisos.append(f"{self.describir(estados[m])}: voy directo {A_MEDIO[siguiente]}")
        return disponibles, avisos

    def espera(self, medios: list[str] | None = None) -> float:
        """Segundos hasta que alguno de los medios del modo se pueda volver a probar (entre 1 min y 6 h)."""
        ahora = self.reloj()
        candidatos = []
        for m in medios or self.medios_del_modo():
            e = self.estado(m)
            if e.disponible:
                return float(ESPERA_MIN_S)
            if e.hasta is not None:
                candidatos.append(e.hasta - ahora)
        espera = min(candidatos) if candidatos else ESPERA_MAX_DEFAULT_S
        return float(min(ESPERA_TOPE_S, max(ESPERA_MIN_S, espera)))

    def _ocultar_clave(self, texto: str) -> str:
        """Por las dudas: la clave nunca va a la base, al log ni al panel aunque venga en un error."""
        clave = self.cfg.anthropic_api_key
        return texto.replace(clave, "[ANTHROPIC_API_KEY]") if clave else texto

    # --- escritura ---
    def marcar(self, e: SinCreditos, *, huella_clave: str | None = None) -> None:
        """Recuerda que el medio no anda (con la API, la huella de la clave que falló: por defecto, la
        de ahora). Si la base no deja escribir, sigue sin memoria (la tarea no se cae por eso)."""
        ahora = self.reloj()
        hasta = None
        if e.medio == MAX:  # con margen: justo en el resets_at el CLI puede no haberlo liberado todavía
            hasta = (e.hasta + MARGEN_MAX_S if e.hasta and e.hasta > ahora
                     else ahora + max(ESPERA_MIN_S, e.espera_s))
        motivo = self._ocultar_clave(e.motivo)
        try:
            with self.base.conexion() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO facturacion (medio, estado, motivo, desde, hasta, huella_clave) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (e.medio, e.estado, motivo[:500], ahora, hasta,
                     (huella_clave or huella(self.cfg.anthropic_api_key)) if e.medio == API else None),
                )
        except sqlite3.OperationalError as error:
            log.error("facturación: no pude guardar el estado de %s: %s", ETIQUETA_MEDIO[e.medio], error)
        log.warning("facturación: %s queda %s (%s)", ETIQUETA_MEDIO[e.medio], e.estado, motivo[:200])

    def marcar_ok(self, medio: str) -> None:
        try:
            with self.base.conexion() as conn:
                cur = conn.execute("DELETE FROM facturacion WHERE medio = ?", (medio,))
        except sqlite3.OperationalError:
            return
        if cur.rowcount:
            log.info("facturación: %s volvió a andar", ETIQUETA_MEDIO[medio])

    def reintentar(self, medio: str) -> int:
        """Botones "Reintentar API" / "Reintentar plan Max": olvida el error de ese medio y adelanta
        los jobs de Claude pospuestos. Devuelve cuántos jobs despertó."""
        if medio not in MEDIOS:
            raise ValueError(f"medio desconocido: {medio!r}")
        ahora = self.reloj()
        marcas = ",".join("?" for _ in TAREAS_CLAUDE)
        with self.base.conexion() as conn:
            conn.execute("DELETE FROM facturacion WHERE medio = ?", (medio,))
            cur = conn.execute(
                f"UPDATE jobs SET run_after = ?, actualizado = ? WHERE estado = 'pendiente' "
                f"AND tipo IN ({marcas}) AND run_after > ?",
                (ahora, ahora, *TAREAS_CLAUDE, ahora))
        log.info("facturación: %s se vuelve a probar (a mano); %d job(s) adelantados", ETIQUETA_MEDIO[medio],
                 cur.rowcount)
        return cur.rowcount

    def reintentar_api(self) -> int:
        return self.reintentar(API)

    # --- correr una tarea ---
    def ejecutar(self, tarea: str, correr: Callable[[str], T], *,
                 log_tarea: Callable[[str], None] | None = None) -> tuple[T, str]:
        """Corre `correr(medio)` con el primer medio disponible; si lanza SinCreditos, lo recuerda y
        rehace todo con el siguiente. Devuelve (resultado, medio). Si no queda ninguno, CuotaAgotada
        con la espera hasta que alguno se pueda volver a probar."""
        avisar = log_tarea or (lambda _m: None)
        medios, avisos = self.plan()
        for a in avisos:
            avisar(a)
        if not medios:
            estados = [self.describir(self.estado(m)) for m in self.medios_del_modo()]
            espera = self.espera()
            raise CuotaAgotada(f"{tarea}: ningún medio de facturación de Claude disponible ({'; '.join(estados)}); "
                               f"reintento en {_minutos(espera)}", espera_s=espera)
        for i, medio in enumerate(medios):
            # La clave con la que corre este intento: si el .env cambia mientras tanto, lo que se
            # recuerda es que falló ésa (la nueva se vuelve a probar).
            huella_intento = huella(self.cfg.anthropic_api_key)
            try:
                resultado = correr(medio)
            except SinCreditos as e:
                self.marcar(e, huella_clave=huella_intento)
                resto = medios[i + 1:]
                if resto:
                    avisar(f"{TEXTO_ESTADO.get(e.estado, e.estado)} ({self._ocultar_clave(e.motivo)[:200]}): "
                           f"sigo con {NOMBRE_MEDIO[resto[0]]}")
                    continue
                espera = self.espera()
                detalle = self._ocultar_clave(str(e))
                if len(self.medios_del_modo()) > 1:
                    detalle += " y no queda otro medio"
                raise CuotaAgotada(f"{detalle}; reintento en {_minutos(espera)}", espera_s=espera) from e
            self.marcar_ok(medio)
            return resultado, medio
        raise AssertionError("inalcanzable")  # pragma: no cover


def despertar_pospuestos(base: Base, ahora: float | None = None) -> int:
    """Adelanta los jobs de Claude que estaban esperando (p. ej., después de cambiar el modo)."""
    momento = time.time() if ahora is None else ahora
    marcas = ",".join("?" for _ in TAREAS_CLAUDE)
    with base.conexion() as conn:
        cur = conn.execute(
            f"UPDATE jobs SET run_after = ?, actualizado = ? WHERE estado = 'pendiente' "
            f"AND tipo IN ({marcas}) AND run_after > ?", (momento, momento, *TAREAS_CLAUDE, momento))
    return cur.rowcount


# --- costo de la API ---------------------------------------------------------------------------

# US$ por millón de tokens (entrada, salida), de la skill claude-api (tabla "Current Models", cacheada
# el 2026-10-06). Haiku 5.5 cuesta 0,50/2,50 si el prompt pasa los 100K tokens.
PRECIOS_USD_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-mythos-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}
PRECIO_HAIKU_5_5_LARGO = (0.50, 2.50)
CACHE_ESCRITURA = 1.25   # multiplicador del precio de entrada (caché de 5 min)
CACHE_LECTURA = 0.10     # multiplicador de la lectura de caché, salvo los modelos de abajo
# Lectura de caché con precio propio (US$/MTok): Opus 5.5 lee a 0,20 (0,05x) y Fable/Mythos 5.1 a
# 0,25 (0,025x), no al 0,1x de la entrada.
CACHE_LECTURA_USD_MTOK: dict[str, float] = {
    "claude-opus-5-5": 0.20,
    "claude-fable-5-1": 0.25,
    "claude-mythos-5-1": 0.25,
}


def costo_api(modelo: str | None, uso: dict[str, int]) -> float | None:
    """Costo en US$ de una llamada a la API con `uso` = {"entrada", "salida", "cache_escritura",
    "cache_lectura"} (tokens). None si el modelo no está en la tabla (se muestran sólo los tokens)."""
    if not modelo or modelo not in PRECIOS_USD_MTOK:
        return None
    entrada = int(uso.get("entrada") or 0)
    escritura = int(uso.get("cache_escritura") or 0)
    lectura = int(uso.get("cache_lectura") or 0)
    salida = int(uso.get("salida") or 0)
    p_in, p_out = PRECIOS_USD_MTOK[modelo]
    if modelo == "claude-haiku-5-5" and entrada + escritura + lectura > 100_000:
        p_in, p_out = PRECIO_HAIKU_5_5_LARGO
    p_lectura = CACHE_LECTURA_USD_MTOK.get(modelo, p_in * CACHE_LECTURA)
    total = (entrada + escritura * CACHE_ESCRITURA) * p_in + lectura * p_lectura + salida * p_out
    return round(total / 1_000_000, 6)


def texto_costo(costo: float | None, facturacion: str | None) -> str:
    """'US$ 0.0123 (API)' o 'US$ 1.80 equivalente API (plan Max)' para logs y panel."""
    if costo is None:
        return f"sin costo informado ({ETIQUETA_MEDIO.get(facturacion or '', facturacion or '?')})"
    cifra = f"US$ {costo:.4f}" if costo < 0.1 else f"US$ {costo:.2f}"
    if facturacion == MAX:
        return f"{cifra} equivalente API (plan Max, no se cobra aparte)"
    return f"{cifra} ({ETIQUETA_MEDIO.get(facturacion or '', facturacion or '?')})"
