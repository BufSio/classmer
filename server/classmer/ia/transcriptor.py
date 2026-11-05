"""Transcripción de un bloque de audio (§6 etapa 3, §7).

- `TranscriptorGeminiASR`: modelo ASR dedicado (gemini-*-transcribe). Devuelve palabras con
  tiempos y hablante; acá se agrupan en frases. No acepta JSON mode ni system_instruction.
- `TranscriptorGeminiLLM`: respaldo con un modelo general (gemini-pro-latest) y salida JSON
  [{inicio, fin, hablante, texto}] en MM:SS, con el prompt de prompts/transcribir.md.
- `TranscriptorWhisper`: lugar reservado para Whisper local (v0.2).

Todos los tiempos que devuelven son en ms **relativos al bloque**; la etapa los pasa a absolutos.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from classmer.ia import gemini
from classmer.pipeline import ErrorEtapa

if TYPE_CHECKING:
    from classmer.config import Config

HABLANTES = ("profe", "alumno", "desconocido")

# Agrupación de palabras del ASR en segmentos.
PAUSA_CORTE_MS = 1200
TOPE_SEGMENTO_MS = 30_000
CORTE_SUAVE_MS = 20_000  # pasado esto, también se corta en coma / punto y coma
FIN_ORACION = (".", "?", "!", "…")
PAUSA_SUAVE = (",", ";", ":")

# Validación.
HUECO_MS = 45_000
TOLERANCIA_FIN_MS = 5_000
REPETICIONES_LOOP = 4
HABLA_MINIMA_MS = 3_000

TEXTO_SIN_TRANSCRIPCION = "[inaudible: no se pudo transcribir este bloque]"


# --- datos -----------------------------------------------------------------------------------

@dataclass
class ContextoTranscripcion:
    """Lo que se sabe del bloque antes de transcribirlo. `habla` en ms relativos al bloque."""

    materia: str | None = None
    perfil: str | None = None
    glosario: list[str] = field(default_factory=list)
    previo: str | None = None
    duracion_ms: int = 0
    habla: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class SegmentoRel:
    inicio_ms: int
    fin_ms: int
    hablante: str
    texto: str


@dataclass
class ResultadoTranscripcion:
    segmentos: list[SegmentoRel]
    motor: str
    modelo: str
    costo: float | None = None
    avisos: list[str] = field(default_factory=list)
    uso: dict[str, int] = field(default_factory=dict)

    @property
    def vacio(self) -> bool:
        return not any(s.texto.strip() for s in self.segmentos)


@runtime_checkable
class Transcriptor(Protocol):
    nombre: str

    def transcribir(self, audio: Path, contexto: ContextoTranscripcion) -> ResultadoTranscripcion:
        ...


# --- fábricas --------------------------------------------------------------------------------

def crear_transcriptor(cfg: Config) -> Transcriptor:
    """El transcriptor principal según cfg.transcribir (motor/modelo)."""
    t = cfg.transcribir
    if t.motor == "whisper":
        return TranscriptorWhisper(cfg)
    if t.motor != "gemini":
        raise ErrorEtapa(f"motor de transcripción desconocido: {t.motor!r}", reintentable=False)
    return _gemini_para(cfg, t.modelo)


def crear_transcriptores(cfg: Config) -> list[Transcriptor]:
    """Cadena principal → respaldos: [principal, LLM (modelo_respaldo), Whisper (si respaldo=whisper)]."""
    cadena: list[Transcriptor] = [crear_transcriptor(cfg)]
    t = cfg.transcribir
    if t.motor == "gemini" and t.modelo_respaldo and t.modelo_respaldo != t.modelo:
        cadena.append(_gemini_para(cfg, t.modelo_respaldo))
    if t.respaldo == "whisper" and t.motor != "whisper":
        cadena.append(TranscriptorWhisper(cfg))
    return cadena


def _gemini_para(cfg: Config, modelo: str) -> Transcriptor:
    if "transcribe" in modelo.lower():
        return TranscriptorGeminiASR(cfg, modelo=modelo)
    return TranscriptorGeminiLLM(cfg, modelo=modelo)


def _leer_audio(audio: Path) -> bytes:
    try:
        datos = audio.read_bytes()
    except FileNotFoundError as e:
        raise ErrorEtapa(f"no existe el audio del bloque: {audio.name}", reintentable=False) from e
    if len(datos) > gemini.MAX_INLINE_BYTES:
        raise ErrorEtapa(
            f"{audio.name} pesa {len(datos) // (1024 * 1024)} MB: es demasiado para mandarlo inline "
            "(bajá segmentar.bloque_s)", reintentable=False)
    return datos


# --- Gemini ASR ------------------------------------------------------------------------------

class TranscriptorGeminiASR:
    """gemini-*-transcribe: palabras con tiempos y diarización, agrupadas en frases."""

    nombre = "gemini-asr"

    def __init__(self, cfg: Config, *, modelo: str | None = None, cliente: Any = None):
        self.cfg = cfg
        self.modelo = modelo or cfg.transcribir.modelo
        self._cliente = cliente

    def _cliente_listo(self) -> Any:
        if self._cliente is None:
            self._cliente = gemini.crear_cliente(self.cfg, timeout_s=gemini.TIMEOUT_TRANSCRIBIR_S)
        return self._cliente

    def configuracion(self) -> Any:
        from google.genai import types

        return types.GenerateContentConfig(
            audio_transcription_config=types.AudioTranscriptionConfig(
                language_codes=[self.cfg.transcribir.idioma],
                word_timestamp=True,
                diarization=True,
                mode="VERBATIM",
            )
        )

    def transcribir(self, audio: Path, contexto: ContextoTranscripcion) -> ResultadoTranscripcion:
        from google.genai import types

        cliente = self._cliente_listo()
        datos = _leer_audio(audio)
        respuesta = gemini.llamar(
            cliente, self.modelo,
            [types.Part.from_bytes(data=datos, mime_type=gemini.mime_audio(audio))],
            self.configuracion(), clave=self.cfg.gemini_api_key,
        )
        segmentos, avisos = segmentos_asr(respuesta, contexto.duracion_ms)
        razon = gemini.motivo_fin(respuesta)
        if razon and razon not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            avisos.append(f"el modelo terminó con {razon}")
        return ResultadoTranscripcion(segmentos=segmentos, motor=self.nombre, modelo=self.modelo,
                                      avisos=avisos, uso=gemini.uso(respuesta))


@dataclass
class _Palabra:
    texto: str
    inicio_ms: int
    fin_ms: int
    etiqueta: str | None
    entera: bool = False


def offset_a_ms(valor: Any) -> int | None:
    """'1.250s' (Duration de protobuf en JSON), '1.25', 1.25 o {'seconds', 'nanos'} → ms."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return int(round(float(valor) * 1000))
    if isinstance(valor, dict):
        seg = float(valor.get("seconds", 0) or 0)
        nanos = float(valor.get("nanos", 0) or 0)
        return int(round(seg * 1000 + nanos / 1e6))
    texto = str(valor).strip().lower()
    m = re.match(r"^(-?[\d.]+)\s*(ms|s)?$", texto)
    if not m:
        return None
    try:
        numero = float(m.group(1))
    except ValueError:
        return None
    return int(round(numero if m.group(2) == "ms" else numero * 1000))


def segmentos_asr(respuesta: Any, duracion_ms: int = 0) -> tuple[list[SegmentoRel], list[str]]:
    """Respuesta del ASR → frases (cortes por hablante, fin de oración, pausa y tope de 30 s)."""
    avisos: list[str] = []
    ultimo_fin = 0
    sin_tiempos = 0
    transcripciones = [getattr(p, "audio_transcription", None) for p in gemini.partes_respuesta(respuesta)]
    transcripciones = [t for t in transcripciones if t is not None]
    if not transcripciones:
        texto = gemini.texto_respuesta(respuesta).strip()
        if texto:
            avisos.append("el ASR devolvió texto sin tiempos")
            fin = duracion_ms or _estimar_ms(texto)
            return [SegmentoRel(0, fin, "desconocido", _normalizar_espacios(texto))], avisos
        return [], avisos

    palabras: list[_Palabra] = []
    for t in transcripciones:
        etiqueta = getattr(t, "speaker_label", None) or None
        texto_parte = (getattr(t, "text", None) or "").strip()
        words = [w for w in (getattr(t, "words", None) or []) if (getattr(w, "word", None) or "").strip()]
        if not words:
            if not texto_parte:
                continue
            inicio = offset_a_ms(getattr(t, "start_offset", None))
            fin = offset_a_ms(getattr(t, "end_offset", None))
            if inicio is None:
                inicio = ultimo_fin
                sin_tiempos += 1
            if fin is None or fin < inicio:
                fin = inicio + _estimar_ms(texto_parte)
            # Parte sin palabras: va entera, como un segmento propio.
            palabras.append(_Palabra(_normalizar_espacios(texto_parte), inicio, fin, etiqueta, entera=True))
            ultimo_fin = fin
            continue
        # Si el texto de la parte tiene la misma cantidad de tokens que las palabras, se usan sus
        # tokens (traen la puntuación y las mayúsculas).
        tokens = texto_parte.split()
        usar_tokens = len(tokens) == len(words)
        for i, w in enumerate(words):
            inicio = offset_a_ms(getattr(w, "start_offset", None))
            fin = offset_a_ms(getattr(w, "end_offset", None))
            if inicio is None:
                inicio = ultimo_fin
                sin_tiempos += 1
            if fin is None or fin < inicio:
                fin = inicio + 300
            texto = tokens[i] if usar_tokens else str(w.word).strip()
            palabras.append(_Palabra(texto, inicio, fin, etiqueta))
            ultimo_fin = fin
    if sin_tiempos:
        avisos.append(f"{sin_tiempos} palabra(s) sin tiempo")

    hablantes = _mapa_hablantes(palabras)
    segmentos: list[SegmentoRel] = []
    actual: list[_Palabra] = []

    def cerrar() -> None:
        if not actual:
            return
        texto = _normalizar_espacios(" ".join(p.texto for p in actual))
        if texto:
            segmentos.append(SegmentoRel(actual[0].inicio_ms, max(p.fin_ms for p in actual),
                                         hablantes.get(actual[0].etiqueta, "desconocido"), texto))
        actual.clear()

    for p in palabras:
        if p.entera:
            cerrar()
            actual.append(p)
            cerrar()
            continue
        if actual:
            previa = actual[-1]
            duracion = p.fin_ms - actual[0].inicio_ms
            fin_previa = previa.texto.rstrip("\"'»”)]")
            if (p.etiqueta != previa.etiqueta
                    or fin_previa.endswith(FIN_ORACION)
                    or p.inicio_ms - previa.fin_ms > PAUSA_CORTE_MS
                    or duracion > TOPE_SEGMENTO_MS
                    or (p.inicio_ms - actual[0].inicio_ms > CORTE_SUAVE_MS
                        and fin_previa.endswith(PAUSA_SUAVE))):
                cerrar()
        actual.append(p)
    cerrar()
    return segmentos, avisos


def _mapa_hablantes(palabras: list[_Palabra]) -> dict[str | None, str]:
    """La etiqueta con más tiempo de habla es el profe; las otras, alumnos; sin etiqueta, desconocido."""
    tiempos: Counter[str] = Counter()
    for p in palabras:
        if p.etiqueta:
            tiempos[p.etiqueta] += max(1, p.fin_ms - p.inicio_ms)
    mapa: dict[str | None, str] = {None: "desconocido"}
    if tiempos:
        profe = tiempos.most_common(1)[0][0]
        for etiqueta in tiempos:
            mapa[etiqueta] = "profe" if etiqueta == profe else "alumno"
    return mapa


def _estimar_ms(texto: str) -> int:
    """Duración aproximada de un texto hablado (~2,5 palabras por segundo)."""
    return max(1000, int(len(texto.split()) / 2.5 * 1000))


def _normalizar_espacios(texto: str) -> str:
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = re.sub(r"\s+([,.;:!?…)\]»”])", r"\1", texto)
    texto = re.sub(r"([¿¡(\[«“])\s+", r"\1", texto)
    return texto


# --- Gemini LLM (respaldo) -------------------------------------------------------------------

class SegmentoLLM(BaseModel):
    """Esquema de salida del LLM (response_schema)."""

    inicio: str = Field(description="Inicio de la frase, MM:SS desde el comienzo de este audio")
    fin: str = Field(description="Fin de la frase, MM:SS desde el comienzo de este audio")
    hablante: str = Field(description="profe, alumno o desconocido")
    texto: str = Field(description="Transcripción literal de la frase")


class TranscriptorGeminiLLM:
    """Modelo general de Gemini con salida JSON: respaldo cuando el ASR no valida."""

    nombre = "gemini-llm"
    PROMPT = "transcribir"

    def __init__(self, cfg: Config, *, modelo: str | None = None, cliente: Any = None):
        self.cfg = cfg
        self.modelo = modelo or cfg.transcribir.modelo_respaldo or "gemini-pro-latest"
        self._cliente = cliente

    def _cliente_listo(self) -> Any:
        if self._cliente is None:
            self._cliente = gemini.crear_cliente(self.cfg, timeout_s=gemini.TIMEOUT_TRANSCRIBIR_S)
        return self._cliente

    def prompt(self, contexto: ContextoTranscripcion) -> str:
        return gemini.cargar_prompt(
            self.PROMPT,
            materia=contexto.materia or "(no se sabe todavía)",
            duracion=_mmss(contexto.duracion_ms),
            perfil=(contexto.perfil or "").strip() or "(no hay perfil de la materia)",
            glosario=", ".join(contexto.glosario) if contexto.glosario else "(no hay)",
            previo=(contexto.previo or "").strip() or "(es el comienzo de la clase o no está disponible)",
        )

    def configuracion(self, contexto: ContextoTranscripcion) -> Any:
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=self.prompt(contexto),
            response_mime_type="application/json",
            response_schema=list[SegmentoLLM],
            # Transcribir no necesita razonar mucho: "low" probado con gemini-pro-latest
            # (2026-10-13): 28 s en vez de 46 s y ~70 % menos tokens de razonamiento.
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        )

    def transcribir(self, audio: Path, contexto: ContextoTranscripcion) -> ResultadoTranscripcion:
        from google.genai import types

        cliente = self._cliente_listo()
        datos = _leer_audio(audio)
        contents = [
            types.Part.from_bytes(data=datos, mime_type=gemini.mime_audio(audio)),
            "Transcribí este audio completo siguiendo las instrucciones. Respondé sólo con la lista JSON.",
        ]
        respuesta = gemini.llamar(cliente, self.modelo, contents, self.configuracion(contexto),
                                  clave=self.cfg.gemini_api_key)
        segmentos, avisos = segmentos_llm(respuesta)
        razon = gemini.motivo_fin(respuesta)
        if razon and razon not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            avisos.append(f"el modelo terminó con {razon}")
        return ResultadoTranscripcion(segmentos=segmentos, motor=self.nombre, modelo=self.modelo,
                                      avisos=avisos, uso=gemini.uso(respuesta))


def segmentos_llm(respuesta: Any) -> tuple[list[SegmentoRel], list[str]]:
    """Respuesta JSON del LLM → segmentos (ms relativos). Descarta los items mal formados."""
    avisos: list[str] = []
    texto = gemini.texto_respuesta(respuesta)
    if not texto.strip():
        return [], avisos
    items, completa = gemini.leer_json_lista(texto)
    if not completa:
        avisos.append(f"respuesta JSON cortada: se rescataron {len(items)} segmento(s)")
    segmentos: list[SegmentoRel] = []
    malos = 0
    for item in items:
        if not isinstance(item, dict):
            malos += 1
            continue
        contenido = _normalizar_espacios(str(item.get("texto") or ""))
        try:
            inicio = tiempo_llm_a_ms(item.get("inicio"))
            fin = tiempo_llm_a_ms(item.get("fin"))
        except ValueError:
            malos += 1
            continue
        if not contenido:
            continue
        segmentos.append(SegmentoRel(inicio, fin, normalizar_hablante(item.get("hablante")), contenido))
    if malos:
        avisos.append(f"{malos} segmento(s) con formato inválido descartados")
    return segmentos, avisos


def tiempo_llm_a_ms(valor: Any) -> int:
    """'MM:SS', 'H:MM:SS', 'SS', 'MM:SS.mmm', 'MM:SS,mmm' o 'MM:SS:mmm' → ms."""
    if valor is None:
        raise ValueError("tiempo vacío")
    if isinstance(valor, (int, float)):
        if valor < 0:
            raise ValueError("tiempo negativo")
        return int(round(float(valor) * 1000))
    texto = str(valor).strip().replace(",", ".")
    if not texto:
        raise ValueError("tiempo vacío")
    partes = texto.split(":")
    if len(partes) == 3 and re.fullmatch(r"\d{3}", partes[2]) and int(partes[1]) <= 59:
        # 'MM:SS:mmm' (milisegundos con dos puntos)
        partes = [partes[0], f"{partes[1]}.{partes[2]}"]
    if not 1 <= len(partes) <= 3:
        raise ValueError(f"tiempo inválido: {valor!r}")
    segundos = 0.0
    for p in partes:
        p = p.strip()
        if not re.fullmatch(r"\d+(\.\d+)?", p):
            raise ValueError(f"tiempo inválido: {valor!r}")
        segundos = segundos * 60 + float(p)
    return int(round(segundos * 1000))


def normalizar_hablante(valor: Any) -> str:
    texto = _sin_tildes(str(valor or "")).lower().strip()
    if re.search(r"profe|docente|ayudante|maestr|catedr", texto):
        return "profe"
    if re.search(r"alumn|estudiante|companer|oyente|publico", texto):
        return "alumno"
    return "desconocido"


def _mmss(ms: int) -> str:
    total = max(0, int(ms // 1000))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


# --- Whisper local (v0.2) --------------------------------------------------------------------

class TranscriptorWhisper:
    """Whisper local como respaldo (v0.2). La GPU es AMD (sin CUDA), así que va a ser:

    - faster-whisper en CPU: WhisperModel("large-v3-turbo", device="cpu", compute_type="int8"),
      transcribe(audio, language="es", vad_filter=True, word_timestamps=True,
      initial_prompt=", ".join(contexto.glosario)); o
    - whisper.cpp compilado con Vulkan (subprocess), con -l es, VAD y --prompt con el glosario.

    Los segmentos de Whisper ya traen inicio/fin; el hablante queda "desconocido".
    """

    nombre = "whisper"

    def __init__(self, cfg: Config, *, modelo: str = "large-v3-turbo"):
        self.cfg = cfg
        self.modelo = modelo

    def initial_prompt(self, contexto: ContextoTranscripcion) -> str:
        partes = [contexto.materia or "", ", ".join(contexto.glosario)]
        return ". ".join(p for p in partes if p)[:800]

    def transcribir(self, audio: Path, contexto: ContextoTranscripcion) -> ResultadoTranscripcion:
        raise ErrorEtapa("Whisper local llega en v0.2", reintentable=False)


# --- validación ------------------------------------------------------------------------------

def validar(resultado: ResultadoTranscripcion, contexto: ContextoTranscripcion) -> list[str]:
    """Problemas de una transcripción (lista vacía = válida).

    - vacía cuando el detector de voz dice que hubo habla;
    - huecos: más de ~45 s de habla seguida sin ningún segmento que la toque;
    - texto en loop: la misma frase ≥ 4 veces seguidas o un n-grama repetido dentro de un segmento;
    - tiempos fuera de [0, duración + 5 s] o con fin < inicio;
    - segmentos de más de 2 × TOPE_SEGMENTO_MS (texto sin tiempos);
    - salida cortada: MAX_TOKENS, JSON cortado o items descartados (avisos del transcriptor).
    """
    problemas: list[str] = [a for a in resultado.avisos if _aviso_de_salida_incompleta(a)]
    segmentos = [s for s in resultado.segmentos if s.texto.strip()]
    duracion = contexto.duracion_ms
    habla = _recortar(contexto.habla, duracion)
    total_habla = sum(f - i for i, f in habla)

    if not segmentos:
        if total_habla >= HABLA_MINIMA_MS:
            problemas.append(f"vacía: el detector de voz marcó {total_habla / 1000:.0f} s de habla")
        return problemas

    malos = [s for s in segmentos
             if s.inicio_ms < 0 or s.fin_ms < s.inicio_ms
             or (duracion and s.fin_ms > duracion + TOLERANCIA_FIN_MS)]
    if malos:
        ejemplo = malos[0]
        problemas.append(
            f"tiempos inválidos en {len(malos)} segmento(s) (p. ej. {ejemplo.inicio_ms}–{ejemplo.fin_ms} ms "
            f"con un bloque de {duracion} ms)")

    largos = [s for s in segmentos if s.fin_ms - s.inicio_ms > 2 * TOPE_SEGMENTO_MS]
    if largos:
        problemas.append(f"{len(largos)} segmento(s) de más de {2 * TOPE_SEGMENTO_MS // 1000} s "
                         "(texto sin tiempos)")

    for inicio, fin in huecos(segmentos, habla):
        problemas.append(f"hueco de {(fin - inicio) / 1000:.0f} s con habla y sin texto "
                         f"({_mmss(inicio)}–{_mmss(fin)})")

    loop = detectar_loop(segmentos)
    if loop:
        problemas.append(loop)
    return problemas


def _aviso_de_salida_incompleta(aviso: str) -> bool:
    """Avisos de los transcriptores que indican que falta texto (no sólo un detalle)."""
    return (aviso.startswith("respuesta JSON cortada")
            or (aviso.startswith("el modelo terminó con") and "MAX_TOKENS" in aviso)
            or "descartados" in aviso)


def _recortar(intervalos: list[tuple[int, int]], duracion: int) -> list[tuple[int, int]]:
    salida = []
    for i, f in sorted(intervalos):
        i = max(0, int(i))
        f = int(f) if not duracion else min(int(f), duracion)
        if f > i:
            salida.append((i, f))
    return salida


def huecos(segmentos: list[SegmentoRel], habla: list[tuple[int, int]],
           minimo_ms: int = HUECO_MS) -> list[tuple[int, int]]:
    """Tramos de habla sin segmentos: entre segmentos (y antes del primero / después del último),
    desde el primer tramo de habla hasta el último, si duran ≥ minimo_ms y al menos la mitad es habla."""
    if not habla:
        return []
    cubiertos = sorted((min(s.inicio_ms, s.fin_ms), max(s.inicio_ms, s.fin_ms)) for s in segmentos)
    union: list[list[int]] = []
    for i, f in cubiertos:
        if union and i <= union[-1][1]:
            union[-1][1] = max(union[-1][1], f)
        else:
            union.append([i, f])
    limites = [-1, *[x for par in union for x in par], 10**12]
    resultado = []
    for k in range(0, len(limites), 2):
        desde, hasta = limites[k], limites[k + 1]
        dentro = [(max(i, desde), min(f, hasta)) for i, f in habla if f > desde and i < hasta]
        dentro = [(i, f) for i, f in dentro if f > i]
        if not dentro:
            continue
        tramo_i, tramo_f = dentro[0][0], dentro[-1][1]
        hablado = sum(f - i for i, f in dentro)
        if tramo_f - tramo_i >= minimo_ms and hablado >= minimo_ms / 2:
            resultado.append((tramo_i, tramo_f))
    return resultado


def _palabras_norm(texto: str) -> list[str]:
    return re.findall(r"\w+", _sin_tildes(texto).lower())


def detectar_loop(segmentos: list[SegmentoRel]) -> str | None:
    """Texto repetido en loop (típica alucinación). Devuelve la descripción o None."""
    # 1) La misma frase en ≥ 4 segmentos seguidos.
    previo, racha = None, 0
    for s in segmentos:
        clave = " ".join(_palabras_norm(s.texto))
        if not clave or clave == "inaudible":
            previo, racha = None, 0
            continue
        racha = racha + 1 if clave == previo else 1
        previo = clave
        if racha >= REPETICIONES_LOOP:
            return f"texto en loop: «{s.texto[:60]}» repetido {racha} veces seguidas"
    # 2) Dentro de un segmento: un n-grama repetido seguido muchas veces.
    for s in segmentos:
        palabras = _palabras_norm(s.texto)
        if len(palabras) < 12:
            continue
        for n in range(1, 9):
            minimo = max(REPETICIONES_LOOP, -(-12 // n))  # ≥ 4 veces y ≥ 12 palabras en total
            reps = _max_repeticiones(palabras, n)
            if reps >= minimo:
                frase = " ".join(palabras[:n]) if n < 4 else "una frase"
                return f"texto en loop dentro de un segmento ({_mmss(s.inicio_ms)}): «{frase}…» ×{reps}"
        # n-grama de 4 palabras que aparece muchas veces aunque no sea seguido (loop con variantes)
        cuatro = Counter(tuple(palabras[i:i + 4]) for i in range(len(palabras) - 3))
        if cuatro:
            ngrama, veces = cuatro.most_common(1)[0]
            if veces >= 8 and veces * 4 >= len(palabras) * 0.6:
                return (f"texto en loop dentro de un segmento ({_mmss(s.inicio_ms)}): "
                        f"«{' '.join(ngrama)}» ×{veces}")
    # 3) En todo el bloque: una frase larga (9–20 palabras) repetida seguida, aunque los cortes
    #    de segmento no coincidan con la frase.
    todas = [w for s in segmentos for w in _palabras_norm(s.texto)]
    for n in range(9, 21):
        if len(todas) < n * REPETICIONES_LOOP:
            break
        reps = _max_repeticiones(todas, n)
        if reps >= REPETICIONES_LOOP:
            return f"texto en loop en el bloque: una frase de {n} palabras ×{reps}"
    return None


def _max_repeticiones(palabras: list[str], n: int) -> int:
    """Máxima cantidad de veces seguidas que se repite un mismo n-grama."""
    mejor = 1
    for inicio in range(n):
        racha, previo = 0, None
        for i in range(inicio, len(palabras) - n + 1, n):
            ngrama = tuple(palabras[i:i + n])
            racha = racha + 1 if ngrama == previo else 1
            previo = ngrama
            mejor = max(mejor, racha)
    return mejor


def puntaje(resultado: ResultadoTranscripcion, problemas: list[str]) -> tuple[int, int, int]:
    """Para elegir el mejor resultado no válido: menos problemas, más tiempo cubierto, más texto."""
    cubierto = sum(max(0, s.fin_ms - s.inicio_ms) for s in resultado.segmentos)
    texto = sum(len(s.texto) for s in resultado.segmentos)
    return (-len(problemas), cubierto, texto)


def texto_final(segmentos: list[SegmentoRel], maximo: int = 600) -> str | None:
    """Las últimas frases de un bloque (contexto 'previo' del siguiente)."""
    partes: list[str] = []
    largo = 0
    for s in reversed(segmentos):
        t = s.texto.strip()
        if not t:
            continue
        if largo + len(t) > maximo and partes:
            break
        partes.append(t)
        largo += len(t) + 1
        if len(partes) >= 4:
            break
    if not partes:
        return None
    return " ".join(reversed(partes))[-maximo:]


__all__ = [
    "ContextoTranscripcion",
    "HABLANTES",
    "ResultadoTranscripcion",
    "SegmentoRel",
    "TEXTO_SIN_TRANSCRIPCION",
    "Transcriptor",
    "TranscriptorGeminiASR",
    "TranscriptorGeminiLLM",
    "TranscriptorWhisper",
    "crear_transcriptor",
    "crear_transcriptores",
    "detectar_loop",
    "huecos",
    "normalizar_hablante",
    "offset_a_ms",
    "puntaje",
    "segmentos_asr",
    "segmentos_llm",
    "texto_final",
    "tiempo_llm_a_ms",
    "validar",
]
