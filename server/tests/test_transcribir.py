"""Transcripción (§6 etapa 3): parseo del ASR y del LLM, validación, errores y la etapa con fakes.

Nada llama a Gemini: los clientes y transcriptores son falsos.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from google.genai import errors, types

from classmer import sesiones
from classmer.cola import Job
from classmer.ia import gemini
from classmer.ia.transcriptor import (
    TEXTO_SIN_TRANSCRIPCION,
    ContextoTranscripcion,
    ResultadoTranscripcion,
    SegmentoRel,
    TranscriptorGeminiASR,
    TranscriptorGeminiLLM,
    TranscriptorWhisper,
    crear_transcriptor,
    crear_transcriptores,
    detectar_loop,
    normalizar_hablante,
    offset_a_ms,
    segmentos_asr,
    segmentos_llm,
    tiempo_llm_a_ms,
    validar,
)
from classmer.pipeline import CuotaAgotada, ErrorEtapa, Servicios
from classmer.pipeline import transcribir as etapa

SID = "0192f7c2-0000-7000-8000-00000000aaaa"


# --- helpers ---------------------------------------------------------------------------------

def palabra(texto: str, inicio_s: float, fin_s: float) -> types.WordInfo:
    return types.WordInfo(word=texto, start_offset=f"{inicio_s:.3f}s", end_offset=f"{fin_s:.3f}s")


def transcripcion(palabras: list[types.WordInfo], hablante: str | None = "spk:0",
                  texto: str | None = None) -> types.Transcription:
    if texto is None:
        texto = " ".join(p.word for p in palabras)
    return types.Transcription(text=texto, speaker_label=hablante, words=palabras)


def respuesta_asr(*partes: types.Transcription) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text=t.text, audio_transcription=t)
                                                       for t in partes]),
            finish_reason="STOP",
        )],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=700, candidates_token_count=90, total_token_count=790),
    )


def respuesta_texto(texto: str, razon: str = "STOP") -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=texto)]), finish_reason=razon)])


class ClienteFalso:
    """Imita client.models.generate_content: devuelve (o lanza) las respuestas en orden."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.llamadas: list[dict] = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.llamadas.append({"model": model, "contents": contents, "config": config})
        r = self.respuestas.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


class TranscriptorFalso:
    """Devuelve (o lanza) salidas en orden; la última se repite."""

    def __init__(self, nombre: str, *salidas, modelo: str = "falso-1"):
        self.nombre = nombre
        self.modelo = modelo
        self.salidas = list(salidas)
        self.contextos: list[ContextoTranscripcion] = []

    def transcribir(self, audio: Path, contexto: ContextoTranscripcion) -> ResultadoTranscripcion:
        self.contextos.append(contexto)
        salida = self.salidas.pop(0) if len(self.salidas) > 1 else self.salidas[0]
        if isinstance(salida, BaseException):
            raise salida
        return ResultadoTranscripcion(segmentos=[SegmentoRel(*s) for s in salida],
                                      motor=self.nombre, modelo=self.modelo, avisos=[])

    @property
    def llamadas(self) -> int:
        return len(self.contextos)


def error_api(codigo: int, status: str = "", mensaje: str = "error", detalles: list | None = None):
    cuerpo = {"error": {"code": codigo, "status": status, "message": mensaje, "details": detalles or []}}
    clase = errors.ClientError if codigo < 500 else errors.ServerError
    return clase(codigo, cuerpo)


def preparar(ctx, bloques: list[tuple[int, int]], *, materia: str | None = "prueba",
             habla: list[tuple[int, int]] | None = None) -> None:
    """Sesión con bloques (filas + mp3 de mentira) y silencios.json."""
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=SID, materia_id=materia, origen_materia=None,
                       tipo_clase=None, inicio="2026-10-15T14:00:00-03:00", dispositivo="test")
        for n, (i, f) in enumerate(bloques, start=1):
            ruta = f"bloques/bloque_{n:02d}.mp3"
            conn.execute("INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta) VALUES (?, ?, ?, ?, ?)",
                         (SID, n, i, f, ruta))
            archivo = ctx.sesion_dir(SID) / ruta
            archivo.parent.mkdir(parents=True, exist_ok=True)
            archivo.write_bytes(b"ID3 falso")
    if habla is not None:
        (ctx.sesion_dir(SID) / "silencios.json").write_text(json.dumps({
            "duracion_ms": bloques[-1][1], "silencios": [], "descartados": [],
            "habla": [{"inicio_ms": i, "fin_ms": f} for i, f in habla],
        }), encoding="utf-8")


def job(n: int) -> Job:
    return Job(id=n, tipo="transcribir", sesion_id=SID, args={"n": n})


def con_transcriptores(ctx, *transcriptores) -> None:
    ctx.servicios = Servicios(transcriptor=lambda cfg: list(transcriptores))


def filas_segmentos(ctx, n: int) -> list[tuple]:
    with ctx.base.conexion() as conn:
        return [tuple(f) for f in conn.execute(
            "SELECT inicio_ms, fin_ms, hablante, texto FROM segmentos WHERE sesion_id = ? AND bloque = ? "
            "ORDER BY inicio_ms", (SID, n))]


def bloque_db(ctx, n: int):
    with ctx.base.conexion() as conn:
        return conn.execute("SELECT * FROM bloques WHERE sesion_id = ? AND n = ?", (SID, n)).fetchone()


# --- ASR: parseo y agrupación ----------------------------------------------------------------

def test_offset_a_ms():
    assert offset_a_ms("0.100s") == 100
    assert offset_a_ms("3s") == 3000
    assert offset_a_ms("1.25") == 1250
    assert offset_a_ms("250ms") == 250
    assert offset_a_ms({"seconds": 2, "nanos": 500_000_000}) == 2500
    assert offset_a_ms(None) is None
    assert offset_a_ms("raro") is None


def test_asr_corta_por_oracion_pausa_y_hablante():
    r = respuesta_asr(
        transcripcion([palabra("Bueno,", 0.1, 0.7), palabra("arrancamos.", 0.8, 1.6),
                       palabra("Hoy", 2.4, 2.6), palabra("vemos", 2.6, 2.9), palabra("límites", 2.9, 3.5),
                       palabra("y", 5.0, 5.2), palabra("derivadas.", 5.2, 6.0)], "spk:0"),
        transcripcion([palabra("¿Y", 6.5, 6.7), palabra("eso", 6.7, 7.0), palabra("entra?", 7.0, 7.5)], "spk:1"),
        transcripcion([palabra("Sí.", 8.0, 8.3)], "spk:0"),
    )
    segmentos, avisos = segmentos_asr(r, 10_000)
    assert avisos == []
    assert [(s.inicio_ms, s.fin_ms, s.hablante, s.texto) for s in segmentos] == [
        (100, 1600, "profe", "Bueno, arrancamos."),
        (2400, 3500, "profe", "Hoy vemos límites"),       # pausa de 1,5 s
        (5000, 6000, "profe", "y derivadas."),
        (6500, 7500, "alumno", "¿Y eso entra?"),          # cambio de hablante
        (8000, 8300, "profe", "Sí."),
    ]


def test_asr_profe_es_quien_mas_habla():
    r = respuesta_asr(
        transcripcion([palabra("¿Profe?", 0.0, 0.5)], "spk:0"),
        transcripcion([palabra("Sí,", 1.0, 1.5), palabra("decime", 1.5, 3.0), palabra("qué", 3.0, 3.5),
                       palabra("pasa.", 3.5, 5.0)], "spk:1"),
    )
    segmentos, _ = segmentos_asr(r, 10_000)
    assert [s.hablante for s in segmentos] == ["alumno", "profe"]


def test_asr_tope_de_30_segundos_y_corte_suave_en_coma():
    palabras = [palabra(f"w{i}," if i == 22 else f"w{i}", i, i + 0.8) for i in range(40)]
    segmentos, _ = segmentos_asr(respuesta_asr(transcripcion(palabras)), 40_000)
    assert segmentos[0].fin_ms == 22_800              # cortó en la coma pasados los 20 s
    assert all(s.fin_ms - s.inicio_ms <= 30_000 for s in segmentos)
    sin_comas = [palabra(f"w{i}", i, i + 0.8) for i in range(70)]
    segmentos, _ = segmentos_asr(respuesta_asr(transcripcion(sin_comas)), 70_000)
    assert len(segmentos) >= 3
    assert all(s.fin_ms - s.inicio_ms <= 30_000 for s in segmentos)
    assert " ".join(s.texto for s in segmentos) == " ".join(f"w{i}" for i in range(70))


def test_asr_sin_diarizacion_es_desconocido():
    r = respuesta_asr(transcripcion([palabra("Hola.", 0, 1)], hablante=None))
    segmentos, _ = segmentos_asr(r, 5000)
    assert segmentos[0].hablante == "desconocido"


def test_asr_usa_la_puntuacion_del_texto_si_coincide():
    r = respuesta_asr(transcripcion([palabra("hola", 0, 0.5), palabra("que", 0.6, 0.8), palabra("tal", 0.8, 1.0),
                                     palabra("bien", 1.2, 1.5)], texto="Hola, ¿qué tal? Bien"))
    segmentos, _ = segmentos_asr(r, 5000)
    assert [s.texto for s in segmentos] == ["Hola, ¿qué tal?", "Bien"]


def test_asr_parte_sin_palabras_y_sin_tiempos():
    t1 = types.Transcription(text="Primera parte.", speaker_label="spk:0", start_offset="1s", end_offset="3s")
    t2 = types.Transcription(text="Segunda sin tiempos.", speaker_label="spk:0")
    segmentos, avisos = segmentos_asr(respuesta_asr(t1, t2), 10_000)
    assert [(s.inicio_ms, s.texto) for s in segmentos] == [(1000, "Primera parte."), (3000, "Segunda sin tiempos.")]
    assert segmentos[1].fin_ms > 3000
    assert any("sin tiempo" in a for a in avisos)


def test_asr_respuesta_vacia_o_solo_texto():
    assert segmentos_asr(types.GenerateContentResponse(candidates=[]), 1000) == ([], [])
    segmentos, avisos = segmentos_asr(respuesta_texto("Sólo texto"), 4000)
    assert segmentos[0].texto == "Sólo texto" and segmentos[0].fin_ms == 4000
    assert avisos


def test_asr_llamada_y_configuracion(cfg, tmp_path):
    cfg.gemini_api_key = "clave-falsa"
    audio = tmp_path / "bloque_01.mp3"
    audio.write_bytes(b"ID3")
    cliente = ClienteFalso(respuesta_asr(transcripcion([palabra("Hola.", 0, 1)])))
    r = TranscriptorGeminiASR(cfg, cliente=cliente).transcribir(audio, ContextoTranscripcion(duracion_ms=5000))
    assert r.motor == "gemini-asr" and r.modelo == "gemini-3.5-transcribe"
    assert r.uso == {"entrada": 700, "salida": 90, "total": 790}
    llamada = cliente.llamadas[0]
    assert llamada["model"] == "gemini-3.5-transcribe"
    c = llamada["config"]
    atc = c.audio_transcription_config
    assert atc.language_codes == ["es-AR"] and atc.word_timestamp and atc.diarization
    assert atc.mode == types.AudioTranscriptionConfigMode.VERBATIM
    assert atc.custom_vocabulary is None                     # incompatible con tiempos y diarización
    assert c.system_instruction is None and c.response_mime_type is None
    assert llamada["contents"][0].inline_data.mime_type == "audio/mp3"


# --- LLM: parseo y prompt --------------------------------------------------------------------

def test_tiempo_llm_a_ms():
    assert tiempo_llm_a_ms("01:05") == 65_000
    assert tiempo_llm_a_ms("1:02:03") == 3_723_000
    assert tiempo_llm_a_ms("75") == 75_000
    assert tiempo_llm_a_ms("00:05.5") == 5_500
    assert tiempo_llm_a_ms("00:05,250") == 5_250
    assert tiempo_llm_a_ms("00:05:250") == 5_250
    assert tiempo_llm_a_ms(12.5) == 12_500
    for malo in ("abc", "", None, "1:-2", "1:2:3:4"):
        with pytest.raises(ValueError):
            tiempo_llm_a_ms(malo)


def test_normalizar_hablante():
    assert normalizar_hablante("Profesora") == "profe"
    assert normalizar_hablante("PROFE") == "profe"
    assert normalizar_hablante("alumna") == "alumno"
    assert normalizar_hablante("Estudiante 2") == "alumno"
    assert normalizar_hablante("?") == "desconocido"
    assert normalizar_hablante(None) == "desconocido"


def test_llm_parseo_y_prompt(cfg, tmp_path):
    cfg.gemini_api_key = "clave-falsa"
    audio = tmp_path / "b.mp3"
    audio.write_bytes(b"ID3")
    salida = json.dumps([
        {"inicio": "00:00", "fin": "00:04", "hablante": "Profesor", "texto": "Bueno ,  arrancamos."},
        {"inicio": "0:00:05", "fin": "0:00:07", "hablante": "alumna", "texto": "¿Entra?"},
        {"inicio": "xx", "fin": "00:09", "hablante": "profe", "texto": "malo"},
        {"inicio": "00:10", "fin": "00:11", "hablante": "profe", "texto": "   "},
    ])
    cliente = ClienteFalso(respuesta_texto(salida))
    contexto = ContextoTranscripcion(materia="Análisis Matemático II", perfil="Notación: **v** en negrita.",
                                     glosario=["gradiente", "Cauchy"], previo="…y cerramos límites.",
                                     duracion_ms=600_000, habla=[(0, 7000)])
    t = TranscriptorGeminiLLM(cfg, cliente=cliente)
    r = t.transcribir(audio, contexto)
    assert r.motor == "gemini-llm" and r.modelo == "gemini-pro-latest"
    assert [(s.inicio_ms, s.fin_ms, s.hablante, s.texto) for s in r.segmentos] == [
        (0, 4000, "profe", "Bueno, arrancamos."),
        (5000, 7000, "alumno", "¿Entra?"),
    ]
    assert any("formato inválido" in a for a in r.avisos)
    c = cliente.llamadas[0]["config"]
    assert c.response_mime_type == "application/json"
    instruccion = c.system_instruction
    for esperado in ("Análisis Matemático II", "Notación: **v** en negrita.", "gradiente, Cauchy",
                     "…y cerramos límites.", "10:00", "[inaudible]"):
        assert esperado in instruccion
    assert "{materia}" not in instruccion and "<!--" not in instruccion
    assert '{"inicio": "00:00"' in instruccion                # el ejemplo JSON queda intacto


def test_llm_json_cortado_rescata_lo_completo():
    texto = ('```json\n[{"inicio":"00:00","fin":"00:05","hablante":"profe","texto":"Hola."},'
             '{"inicio":"00:05","fin":"00:0')
    segmentos, avisos = segmentos_llm(respuesta_texto(texto, "MAX_TOKENS"))
    assert [s.texto for s in segmentos] == ["Hola."]
    assert any("cortada" in a for a in avisos)


# --- validación ------------------------------------------------------------------------------

def resultado(*segs) -> ResultadoTranscripcion:
    return ResultadoTranscripcion(segmentos=[SegmentoRel(*s) for s in segs], motor="x", modelo="y")


def test_validar_vacio():
    con_habla = ContextoTranscripcion(duracion_ms=60_000, habla=[(0, 20_000)])
    sin_habla = ContextoTranscripcion(duracion_ms=60_000, habla=[])
    assert any("vacía" in p for p in validar(resultado(), con_habla))
    assert validar(resultado(), sin_habla) == []
    assert validar(resultado((0, 1000, "profe", "  ")), sin_habla) == []


def test_validar_huecos():
    ctx = ContextoTranscripcion(duracion_ms=80_000, habla=[(0, 80_000)])
    r = resultado((0, 10_000, "profe", "Al principio."), (70_000, 80_000, "profe", "Al final."))
    problemas = validar(r, ctx)
    assert len(problemas) == 1 and "hueco de 60 s" in problemas[0]
    # Sólo dos ruiditos en el medio: no es un hueco.
    ctx = ContextoTranscripcion(duracion_ms=80_000, habla=[(0, 10_000), (30_000, 31_000), (50_000, 51_000),
                                                           (70_000, 80_000)])
    assert validar(r, ctx) == []
    # Hueco al final (después del último segmento).
    ctx = ContextoTranscripcion(duracion_ms=80_000, habla=[(0, 80_000)])
    problemas = validar(resultado((0, 20_000, "profe", "Sólo el comienzo.")), ctx)
    assert any("hueco" in p for p in problemas)


def test_validar_loop():
    ctx = ContextoTranscripcion(duracion_ms=60_000, habla=[])
    cuatro = resultado(*[(i * 1000, i * 1000 + 900, "profe", "Gracias.") for i in range(4)])
    tres = resultado(*[(i * 1000, i * 1000 + 900, "profe", "Gracias.") for i in range(3)])
    assert any("loop" in p for p in validar(cuatro, ctx))
    assert validar(tres, ctx) == []
    adentro = resultado((0, 20_000, "profe", "y entonces " * 7))
    assert detectar_loop(adentro.segmentos)
    normal = resultado((0, 10_000, "profe", "No, no, no, no es así: la derivada direccional depende del vector."))
    assert detectar_loop(normal.segmentos) is None
    variantes = " ".join(f"y entonces el caso {x}" for x in "abcdefghij")
    assert detectar_loop(resultado((0, 25_000, "profe", variantes)).segmentos)
    dictado = "la derivada de x es uno, la derivada de y es dos, la derivada de z es tres, y listo"
    assert detectar_loop(resultado((0, 15_000, "profe", dictado)).segmentos) is None


def test_validar_loop_de_frase_larga_en_el_bloque():
    """Una frase de 11 palabras en loop, con cortes de segmento que no coinciden con la frase."""
    frase = "y entonces la derivada de f en el punto es cero"
    palabras = (frase + " ") * 30
    palabras = palabras.split()
    segs = [(i * 10_000, i * 10_000 + 9000, "profe", " ".join(palabras[j:j + 25]))
            for i, j in enumerate(range(0, len(palabras), 25))]
    problema = detectar_loop(resultado(*segs).segmentos)
    assert problema and "11 palabras" in problema
    # Control: texto variado (y frases que se repiten pocas veces) no es un loop.
    variado = [(i * 10_000, i * 10_000 + 9000, "profe",
                f"En el ejemplo {i} la función vale {i * i} y la derivada de f en el punto es cero")
               for i in range(30)]
    assert detectar_loop(resultado(*variado).segmentos) is None
    dos_veces = [(0, 9000, "profe", frase), (10_000, 19_000, "profe", frase),
                 (20_000, 29_000, "profe", "Y ahora pasamos a otra cosa.")]
    assert detectar_loop(resultado(*dos_veces).segmentos) is None


def test_validar_segmento_sin_tiempos_demasiado_largo():
    ctx = ContextoTranscripcion(duracion_ms=600_000, habla=[(0, 600_000)])
    texto = " ".join(f"palabra{i}" for i in range(1500))
    problemas = validar(resultado((0, 600_000, "desconocido", texto)), ctx)
    assert any("más de 60 s" in p for p in problemas)
    # Lo que devuelve el ASR cuando no trae tiempos tampoco valida.
    segmentos, _ = segmentos_asr(respuesta_texto(texto), 600_000)
    r = ResultadoTranscripcion(segmentos=segmentos, motor="gemini-asr", modelo="m")
    assert any("más de 60 s" in p for p in validar(r, ctx))
    # 60 s justos todavía valen.
    assert validar(resultado((0, 60_000, "profe", "Todo.")),
                   ContextoTranscripcion(duracion_ms=60_000, habla=[(0, 60_000)])) == []


def test_validar_salida_cortada_es_un_problema():
    ctx = ContextoTranscripcion(duracion_ms=60_000, habla=[(0, 60_000)])
    segs = [SegmentoRel(i * 20_000, i * 20_000 + 20_000, "profe", f"Parte {i}.") for i in range(3)]
    for aviso in ("respuesta JSON cortada: se rescataron 28 segmento(s)",
                  "el modelo terminó con MAX_TOKENS",
                  "2 segmento(s) con formato inválido descartados"):
        r = ResultadoTranscripcion(segmentos=segs, motor="gemini-llm", modelo="m", avisos=[aviso])
        assert validar(r, ctx) == [aviso]
    r = ResultadoTranscripcion(segmentos=segs, motor="gemini-asr", modelo="m", avisos=["3 palabra(s) sin tiempo"])
    assert validar(r, ctx) == []
    # Cortada y vacía: también es un problema aunque no haya habla.
    vacia = ResultadoTranscripcion(segmentos=[], motor="gemini-llm", modelo="m",
                                   avisos=["el modelo terminó con MAX_TOKENS"])
    assert validar(vacia, ContextoTranscripcion(duracion_ms=60_000, habla=[])) == ["el modelo terminó con MAX_TOKENS"]


def test_validar_tiempos():
    ctx = ContextoTranscripcion(duracion_ms=60_000, habla=[])
    assert validar(resultado((0, 30_000, "profe", "Casi"),
                             (30_000, 63_000, "profe", "al final.")), ctx) == []  # tolerancia de 5 s
    assert any("tiempos" in p for p in validar(resultado((0, 66_000, "profe", "Pasado.")), ctx))
    assert any("tiempos" in p for p in validar(resultado((5000, 4000, "profe", "Al revés.")), ctx))
    assert any("tiempos" in p for p in validar(resultado((-10, 4000, "profe", "Negativo.")), ctx))


# --- errores de Gemini -----------------------------------------------------------------------

def test_429_es_cuota_con_retry_delay():
    e = error_api(429, "RESOURCE_EXHAUSTED", "Quota exceeded", [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}])
    r = gemini.traducir_error(e, "gemini-3.5-transcribe")
    assert isinstance(r, CuotaAgotada) and r.espera_s == 42
    assert isinstance(gemini.traducir_error(error_api(429), "m"), CuotaAgotada)
    assert gemini.traducir_error(error_api(429), "m").espera_s == gemini.ESPERA_CUOTA_S
    diaria = error_api(429, "RESOURCE_EXHAUSTED", "Quota", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}])
    assert gemini.traducir_error(diaria, "m").espera_s == gemini.ESPERA_CUOTA_DIARIA_S


def test_cuota_diaria_manda_aunque_venga_retry_delay():
    diaria = error_api(429, "RESOURCE_EXHAUSTED", "Quota", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "13s"}])
    assert gemini.traducir_error(diaria, "m").espera_s == gemini.ESPERA_CUOTA_DIARIA_S


def test_el_error_dice_que_cuota_se_toco():
    # El mensaje de Google se corta a los 300 caracteres, antes de la métrica: el quotaId va adelante.
    largo = "You exceeded your current quota, please check your plan and billing details. " * 6
    e = error_api(429, "RESOURCE_EXHAUSTED", largo, [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaMetric": "generativelanguage.googleapis.com/generate_content_paid_tier_input_token_count",
             "quotaId": "GenerateContentPaidTierInputTokensPerModelPerMinute"},
            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "4s"}])
    r = gemini.traducir_error(e, "gemini-3.5-transcribe")
    assert str(r).startswith("Gemini 429 RESOURCE_EXHAUSTED (gemini-3.5-transcribe) [cuota: "
                             "GenerateContentPaidTierInputTokensPerModelPerMinute, "
                             "GenerateRequestsPerDayPerProjectPerModel]: You exceeded")
    assert "[cuota:" not in str(gemini.traducir_error(error_api(429), "m"))


def test_503_sobrecarga_es_cuota_y_no_gasta_intentos():
    for e in (error_api(503, "UNAVAILABLE", "The model is overloaded. Please try again later."),
              error_api(503),
              error_api(500, "UNAVAILABLE"),
              error_api(500, "INTERNAL", "The model is overloaded")):
        r = gemini.traducir_error(e, "gemini-3.5-transcribe")
        assert isinstance(r, CuotaAgotada) and r.espera_s == gemini.ESPERA_SOBRECARGA_S == 180
        assert "gemini-3.5-transcribe" in str(r)
    # Un 4xx que diga "overload" no espera para siempre.
    r = gemini.traducir_error(error_api(400, "INVALID_ARGUMENT", "overloaded request"), "m")
    assert type(r) is ErrorEtapa and not r.reintentable


def test_503_en_el_cliente_pospone(cfg, tmp_path):
    cfg.gemini_api_key = "clave-falsa"
    audio = tmp_path / "b.mp3"
    audio.write_bytes(b"ID3")
    for clase in (TranscriptorGeminiASR, TranscriptorGeminiLLM):
        t = clase(cfg, cliente=ClienteFalso(error_api(503, "UNAVAILABLE", "The model is overloaded")))
        with pytest.raises(CuotaAgotada):
            t.transcribir(audio, ContextoTranscripcion(duracion_ms=1000))


def test_max_inline_entra_en_base64():
    # Los bytes inline viajan en base64 (×4/3) y el request entero tiene que entrar en ~20 MB.
    assert 4 * -(-gemini.MAX_INLINE_BYTES // 3) + 200_000 < 20_000_000


def test_otros_errores():
    for codigo in (500, 502, 504):
        e5 = gemini.traducir_error(error_api(codigo, "INTERNAL", "error interno"), "m")
        assert type(e5) is ErrorEtapa and e5.reintentable
    for codigo in (400, 403, 404):
        e = gemini.traducir_error(error_api(codigo), "m")
        assert type(e) is ErrorEtapa and not e.reintentable
    timeout = gemini.traducir_error(httpx.ReadTimeout("lento"), "m")
    assert type(timeout) is ErrorEtapa and timeout.reintentable
    assert isinstance(gemini.traducir_error(httpx.ConnectError("sin red"), "m"), CuotaAgotada)


def test_el_mensaje_nunca_lleva_la_clave():
    e = gemini.traducir_error(error_api(400, "INVALID_ARGUMENT", "API key AIzaSECRETO not valid"), "m",
                              clave="AIzaSECRETO")
    assert "AIzaSECRETO" not in str(e) and "***" in str(e)


def test_errores_del_cliente_se_traducen(cfg, tmp_path):
    cfg.gemini_api_key = "clave-falsa"
    audio = tmp_path / "b.mp3"
    audio.write_bytes(b"ID3")
    t = TranscriptorGeminiASR(cfg, cliente=ClienteFalso(error_api(429, "RESOURCE_EXHAUSTED")))
    with pytest.raises(CuotaAgotada):
        t.transcribir(audio, ContextoTranscripcion(duracion_ms=1000))


def test_falta_la_clave(cfg, tmp_path):
    assert not cfg.gemini_api_key
    audio = tmp_path / "b.mp3"
    audio.write_bytes(b"ID3")
    for clase in (TranscriptorGeminiASR, TranscriptorGeminiLLM):
        with pytest.raises(CuotaAgotada) as info:
            clase(cfg).transcribir(audio, ContextoTranscripcion(duracion_ms=1000))
        assert "GEMINI_API_KEY" in str(info.value) and info.value.espera_s == 600


# --- fábricas --------------------------------------------------------------------------------

def test_fabricas(cfg):
    cadena = crear_transcriptores(cfg)
    assert [type(t) for t in cadena] == [TranscriptorGeminiASR, TranscriptorGeminiLLM]
    assert cadena[0].modelo == "gemini-3.5-transcribe" and cadena[1].modelo == "gemini-pro-latest"
    cfg.transcribir.modelo = "gemini-pro-latest"
    cfg.transcribir.modelo_respaldo = None
    assert [type(t) for t in crear_transcriptores(cfg)] == [TranscriptorGeminiLLM]
    cfg.transcribir.motor = "whisper"
    w = crear_transcriptor(cfg)
    assert isinstance(w, TranscriptorWhisper)
    with pytest.raises(ErrorEtapa) as info:
        w.transcribir(Path("x.mp3"), ContextoTranscripcion())
    assert "v0.2" in str(info.value) and not info.value.reintentable


# --- prompts y perfil ------------------------------------------------------------------------

def test_rellenar_respeta_otras_llaves():
    assert gemini.rellenar("{a} y {b} {\"x\": 1} {otra}", a="{b}", b=2) == "{b} y 2 {\"x\": 1} {otra}"


def test_extracto_perfil_prioriza_notacion():
    perfil = ("# Materia X\nIntro corta.\n\n## Estado al 2026-09-01\n" + "relleno " * 2000 +
              "\n\n## Notación de la cátedra\nLos vectores van en negrita.\n\n## Glosario\n"
              "- **Gradiente**: vector de derivadas\n- Jacobiano — matriz\n")
    extracto = gemini.extracto_perfil(perfil, 2000)
    assert len(extracto) <= 2010
    assert "Materia X" in extracto and "Los vectores van en negrita." in extracto
    assert "relleno relleno" not in extracto
    assert gemini.extraer_glosario(perfil) == ["Gradiente", "Jacobiano"]
    assert gemini.extracto_perfil("corto", 100) == "corto"
    assert gemini.extracto_perfil(None) is None


# --- etapa transcribir -----------------------------------------------------------------------

def test_etapa_tiempos_absolutos_archivo_filas_y_encadenamiento(ctx):
    preparar(ctx, [(0, 60_000), (60_000, 120_000)],
             habla=[(1000, 8000), (61_000, 66_000)])
    principal = TranscriptorFalso("falso",
                                  [(1000, 4000, "profe", "Arrancamos."), (4500, 8000, "alumno", "¿Qué entra?")],
                                  [(1000, 6000, "profe", "Sigue la clase.")])
    con_transcriptores(ctx, principal)

    r1 = etapa.ejecutar(ctx, job(1))
    assert r1.siguientes == []                       # falta el bloque 2
    assert r1.motor == "falso:falso-1"
    c1 = principal.contextos[0]
    assert c1.materia == "Materia de Prueba" and "Notación" in (c1.perfil or "")
    assert c1.duracion_ms == 60_000 and c1.habla == [(1000, 8000)] and c1.previo is None

    r2 = etapa.ejecutar(ctx, job(2))
    assert r2.siguientes == [("linea_de_tiempo", {})]
    c2 = principal.contextos[1]
    assert c2.habla == [(1000, 6000)]                # relativo al bloque 2
    assert c2.previo == "Arrancamos. ¿Qué entra?"

    datos = json.loads((ctx.sesion_dir(SID) / "transcript" / "bloque_02.json").read_text(encoding="utf-8"))
    assert datos == {
        "n": 2, "inicio_ms": 60_000, "fin_ms": 120_000, "motor": "falso", "modelo": "falso-1",
        "estado": "transcripto", "avisos": [],
        "segmentos": [{"inicio_ms": 61_000, "fin_ms": 66_000, "hablante": "profe", "texto": "Sigue la clase."}],
    }
    assert filas_segmentos(ctx, 1) == [(1000, 4000, "profe", "Arrancamos."),
                                       (4500, 8000, "alumno", "¿Qué entra?")]
    assert filas_segmentos(ctx, 2) == [(61_000, 66_000, "profe", "Sigue la clase.")]
    b = bloque_db(ctx, 2)
    assert b["estado"] == "transcripto" and b["motor"] == "falso:falso-1"


def test_etapa_reejecutar_reemplaza_filas(ctx):
    preparar(ctx, [(0, 30_000)], habla=[(0, 5000)])
    con_transcriptores(ctx, TranscriptorFalso("falso", [(0, 2000, "profe", "Uno."), (2000, 5000, "profe", "Dos.")]))
    assert etapa.ejecutar(ctx, job(1)).siguientes == [("linea_de_tiempo", {})]
    con_transcriptores(ctx, TranscriptorFalso("falso", [(0, 5000, "profe", "Otra versión.")]))
    etapa.ejecutar(ctx, job(1))
    assert filas_segmentos(ctx, 1) == [(0, 5000, "profe", "Otra versión.")]


def test_etapa_recorta_tiempos_al_bloque(ctx):
    preparar(ctx, [(10_000, 40_000)], habla=[(10_000, 40_000)])
    con_transcriptores(ctx, TranscriptorFalso("falso", [(0, 29_000, "profe", "Casi todo."),
                                                         (29_000, 33_000, "profe", "Se pasa un poco.")]))
    etapa.ejecutar(ctx, job(1))
    assert filas_segmentos(ctx, 1)[-1][:2] == (39_000, 40_000)


def test_etapa_usa_el_respaldo_si_el_principal_no_valida(ctx):
    preparar(ctx, [(0, 60_000)], habla=[(0, 30_000)])
    principal = TranscriptorFalso("asr", [])           # vacío con habla: no valida
    respaldo = TranscriptorFalso("llm", [(0, 30_000, "profe", "Bien transcripto.")], modelo="pro")
    con_transcriptores(ctx, principal, respaldo)
    r = etapa.ejecutar(ctx, job(1))
    assert principal.llamadas == ctx.cfg.transcribir.intentos_validacion == 2
    assert respaldo.llamadas == 1
    assert r.motor == "llm:pro"
    datos = json.loads((ctx.sesion_dir(SID) / "transcript" / "bloque_01.json").read_text(encoding="utf-8"))
    assert datos["estado"] == "transcripto" and datos["motor"] == "llm"
    assert "asr:falso-1" in datos["avisos"][0]
    assert bloque_db(ctx, 1)["estado"] == "transcripto"


def test_etapa_dudoso_con_el_mejor_resultado(ctx):
    preparar(ctx, [(0, 120_000)], habla=[(0, 120_000)])
    principal = TranscriptorFalso("asr", [(0, 5000, "profe", "Sólo el comienzo.")])  # hueco enorme
    respaldo = TranscriptorFalso("llm", [])
    con_transcriptores(ctx, principal, respaldo)
    r = etapa.ejecutar(ctx, job(1))
    assert r.siguientes == [("linea_de_tiempo", {})]   # dudoso también cuenta como listo
    datos = json.loads((ctx.sesion_dir(SID) / "transcript" / "bloque_01.json").read_text(encoding="utf-8"))
    assert datos["estado"] == "dudoso" and datos["motor"] == "asr"
    assert any("hueco" in a for a in datos["avisos"])
    assert filas_segmentos(ctx, 1) == [(0, 5000, "profe", "Sólo el comienzo.")]
    assert bloque_db(ctx, 1)["estado"] == "dudoso"


def test_etapa_sin_nada_deja_un_inaudible(ctx):
    preparar(ctx, [(0, 60_000), (60_000, 90_000)], habla=[(60_000, 90_000)])
    con_transcriptores(ctx, TranscriptorFalso("asr", []), TranscriptorFalso("llm", []))
    etapa.ejecutar(ctx, job(2))
    assert filas_segmentos(ctx, 2) == [(60_000, 90_000, "desconocido", TEXTO_SIN_TRANSCRIPCION)]
    assert bloque_db(ctx, 2)["estado"] == "dudoso"


def test_etapa_sin_habla_es_valido_y_vacio(ctx):
    preparar(ctx, [(0, 60_000)], habla=[])
    con_transcriptores(ctx, TranscriptorFalso("asr", []))
    r = etapa.ejecutar(ctx, job(1))
    assert r.siguientes == [("linea_de_tiempo", {})]
    assert filas_segmentos(ctx, 1) == []
    assert bloque_db(ctx, 1)["estado"] == "transcripto"


def test_etapa_sin_silencios_asume_habla(ctx):
    preparar(ctx, [(0, 60_000)], habla=None)
    principal = TranscriptorFalso("asr", [(0, 60_000, "profe", "Todo.")])
    con_transcriptores(ctx, principal)
    etapa.ejecutar(ctx, job(1))
    assert principal.contextos[0].habla == [(0, 60_000)]


def test_etapa_cuota_del_principal_pospone_sin_tocar_el_respaldo(ctx):
    # Prueba real del 14/10: 5 de 8 bloques dieron 429 en gemini-3.5-transcribe (límite por
    # minuto) y se fueron a gemini-pro-latest. El usuario quiere el ASR: sin cuota, se espera y se
    # vuelve a probar el principal; el respaldo no se llama.
    preparar(ctx, [(0, 60_000)], habla=[(0, 60_000)])
    principal = TranscriptorFalso("asr", CuotaAgotada("sin cuota", espera_s=90))
    respaldo = TranscriptorFalso("llm", [(0, 60_000, "profe", "Hola.")])
    con_transcriptores(ctx, principal, respaldo)
    with pytest.raises(CuotaAgotada) as info:
        etapa.ejecutar(ctx, job(1))
    assert info.value.espera_s == 90
    assert principal.llamadas == 1 and respaldo.llamadas == 0
    assert bloque_db(ctx, 1)["estado"] == "pendiente"


def test_etapa_error_no_reintentable_pasa_al_respaldo(ctx):
    preparar(ctx, [(0, 60_000)], habla=[(0, 20_000)])
    principal = TranscriptorFalso("asr", ErrorEtapa("Gemini 404 NOT_FOUND", reintentable=False))
    respaldo = TranscriptorFalso("llm", [(0, 20_000, "profe", "Ok.")])
    con_transcriptores(ctx, principal, respaldo)
    etapa.ejecutar(ctx, job(1))
    assert principal.llamadas == 1 and respaldo.llamadas == 1
    assert bloque_db(ctx, 1)["estado"] == "transcripto"


def test_etapa_errores_sin_resultados(ctx):
    preparar(ctx, [(0, 60_000)], habla=[(0, 20_000)])
    con_transcriptores(ctx, TranscriptorFalso("asr", ErrorEtapa("400 malo", reintentable=False)))
    with pytest.raises(ErrorEtapa) as info:
        etapa.ejecutar(ctx, job(1))
    assert not info.value.reintentable
    con_transcriptores(ctx, TranscriptorFalso("asr", ErrorEtapa("503", reintentable=True)),
                       TranscriptorFalso("llm", ErrorEtapa("503", reintentable=True)))
    with pytest.raises(ErrorEtapa) as info:
        etapa.ejecutar(ctx, job(1))
    assert info.value.reintentable and not isinstance(info.value, CuotaAgotada)
    # Un bug inesperado en el principal tampoco frena al respaldo.
    con_transcriptores(ctx, TranscriptorFalso("asr", KeyError("x")),
                       TranscriptorFalso("llm", [(0, 20_000, "profe", "Ok.")]))
    etapa.ejecutar(ctx, job(1))
    assert bloque_db(ctx, 1)["estado"] == "transcripto"


def test_etapa_bloque_inexistente(ctx):
    preparar(ctx, [(0, 60_000)])
    con_transcriptores(ctx, TranscriptorFalso("asr", []))
    with pytest.raises(ErrorEtapa) as info:
        etapa.ejecutar(ctx, job(5))
    assert not info.value.reintentable


def test_etapa_con_el_asr_real_y_cliente_falso(ctx):
    """El camino completo con TranscriptorGeminiASR (sin red): respuesta → segmentos → base."""
    ctx.cfg.gemini_api_key = "clave-falsa"
    preparar(ctx, [(600_000, 610_000)], habla=[(600_100, 602_000)])
    cliente = ClienteFalso(respuesta_asr(transcripcion([palabra("Bueno,", 0.1, 0.7),
                                                        palabra("arrancamos.", 0.8, 1.6)])))
    ctx.servicios = Servicios(transcriptor=lambda cfg: TranscriptorGeminiASR(cfg, cliente=cliente))
    r = etapa.ejecutar(ctx, job(1))
    assert r.motor == "gemini-asr:gemini-3.5-transcribe"
    assert filas_segmentos(ctx, 1) == [(600_100, 601_600, "profe", "Bueno, arrancamos.")]
    assert "tokens" in (r.log or "")


def test_etapa_un_error_pasa_directo_al_respaldo(ctx):
    preparar(ctx, [(0, 60_000)], habla=[(0, 20_000)])
    principal = TranscriptorFalso("asr", ErrorEtapa("Gemini 503 UNAVAILABLE", reintentable=True))
    respaldo = TranscriptorFalso("llm", [(0, 20_000, "profe", "Ok.")])
    con_transcriptores(ctx, principal, respaldo)
    etapa.ejecutar(ctx, job(1))
    assert principal.llamadas == 1 and respaldo.llamadas == 1


def job_intento(n: int, intentos: int, maximo: int = 4) -> Job:
    return Job(id=n, tipo="transcribir", sesion_id=SID, args={"n": n}, intentos=intentos, max_intentos=maximo)


def test_etapa_ultimo_intento_con_errores_pasajeros_deja_dudoso(ctx):
    """Si en el último intento todo sigue fallando por algo pasajero, el bloque queda 'dudoso'
    (inaudible) y se encola linea_de_tiempo: la sesión no termina en error sin apunte."""
    preparar(ctx, [(0, 60_000)], habla=[(0, 60_000)])
    con_transcriptores(ctx, TranscriptorFalso("asr", ErrorEtapa("Gemini 500 INTERNAL", reintentable=True)),
                       TranscriptorFalso("llm", ErrorEtapa("Gemini no respondió a tiempo", reintentable=True)))
    with pytest.raises(ErrorEtapa) as info:                        # todavía quedan intentos
        etapa.ejecutar(ctx, job_intento(1, 3))
    assert info.value.reintentable and bloque_db(ctx, 1)["estado"] == "pendiente"

    r = etapa.ejecutar(ctx, job_intento(1, 4))
    assert r.siguientes == [("linea_de_tiempo", {})]
    assert filas_segmentos(ctx, 1) == [(0, 60_000, "desconocido", TEXTO_SIN_TRANSCRIPCION)]
    assert bloque_db(ctx, 1)["estado"] == "dudoso"
    datos = json.loads((ctx.sesion_dir(SID) / "transcript" / "bloque_01.json").read_text(encoding="utf-8"))
    assert datos["estado"] == "dudoso" and "4 intentos" in datos["avisos"][0]


def test_etapa_ultimo_intento_no_tapa_cuota_ni_errores_permanentes(ctx):
    preparar(ctx, [(0, 60_000)], habla=[(0, 60_000)])
    con_transcriptores(ctx, TranscriptorFalso("asr", CuotaAgotada("sin cuota", espera_s=60)))
    with pytest.raises(CuotaAgotada):
        etapa.ejecutar(ctx, job_intento(1, 4))
    con_transcriptores(ctx, TranscriptorFalso("asr", ErrorEtapa("400 malo", reintentable=False)))
    with pytest.raises(ErrorEtapa) as info:
        etapa.ejecutar(ctx, job_intento(1, 4))
    assert not info.value.reintentable
    assert bloque_db(ctx, 1)["estado"] == "pendiente"


def test_con_el_worker_errores_pasajeros_no_dejan_la_sesion_en_error(ctx):
    from classmer import cola
    from classmer.worker import Worker

    preparar(ctx, [(0, 60_000)], habla=[(0, 60_000)])
    with ctx.base.conexion() as conn:
        cola.encolar(conn, "transcribir", SID, {"n": 1})
    asr = TranscriptorFalso("asr", ErrorEtapa("Gemini 502", reintentable=True))
    llm = TranscriptorFalso("llm", ErrorEtapa("Gemini 504", reintentable=True))
    con_transcriptores(ctx, asr, llm)
    worker = Worker(ctx)
    for _ in range(4):
        with ctx.base.conexion() as conn:
            tomado = cola.tomar(conn, ["transcribir"], ahora=10**12)
        assert tomado is not None
        worker.ejecutar_job(tomado)
    assert asr.llamadas == llm.llamadas == 4
    with ctx.base.conexion() as conn:
        tipos = [(f["tipo"], f["estado"]) for f in conn.execute("SELECT tipo, estado FROM jobs ORDER BY id")]
        sesion = conn.execute("SELECT estado FROM sesiones WHERE id = ?", (SID,)).fetchone()
    assert tipos == [("transcribir", "hecho"), ("linea_de_tiempo", "pendiente")]
    assert sesion["estado"] != "error"
    assert bloque_db(ctx, 1)["estado"] == "dudoso"


def test_etapa_principal_caido_y_respaldo_incompleto_reintenta(ctx):
    """Si el ASR no respondió por algo pasajero y el LLM no valida, se reintenta el job (el
    intento cuenta) en vez de quedarse con un 'dudoso' para siempre; en el último, se acepta."""
    preparar(ctx, [(0, 600_000)], habla=[(0, 600_000)])
    parcial = [(i * 20_000, i * 20_000 + 20_000, "profe", f"Frase número {i}.") for i in range(18)]  # hasta 06:00
    # Sin cuota (429) no se llega al respaldo: se pospone y se vuelve a probar el principal.
    principal = TranscriptorFalso("asr", CuotaAgotada("Gemini 429", espera_s=60))
    respaldo = TranscriptorFalso("llm", parcial, modelo="pro")
    con_transcriptores(ctx, principal, respaldo)
    with pytest.raises(CuotaAgotada):
        etapa.ejecutar(ctx, job_intento(1, 1))
    assert principal.llamadas == 1 and respaldo.llamadas == 0
    assert bloque_db(ctx, 1)["estado"] == "pendiente"

    principal = TranscriptorFalso("asr", ErrorEtapa("Gemini 500", reintentable=True))
    respaldo = TranscriptorFalso("llm", parcial, modelo="pro")
    con_transcriptores(ctx, principal, respaldo)
    with pytest.raises(ErrorEtapa) as info:
        etapa.ejecutar(ctx, job_intento(1, 1))
    assert info.value.reintentable and not isinstance(info.value, CuotaAgotada)
    assert "respaldo no validó" in str(info.value)
    assert bloque_db(ctx, 1)["estado"] == "pendiente"
    assert principal.llamadas == 1 and respaldo.llamadas == 1

    r = etapa.ejecutar(ctx, job_intento(1, 4))                     # último intento: se acepta
    assert r.motor == "llm:pro" and bloque_db(ctx, 1)["estado"] == "dudoso"
    # Con un error permanente del principal no tiene sentido reintentar.
    con_transcriptores(ctx, TranscriptorFalso("asr", ErrorEtapa("Gemini 404", reintentable=False)),
                       TranscriptorFalso("llm", parcial, modelo="pro"))
    etapa.ejecutar(ctx, job_intento(1, 1))
    assert bloque_db(ctx, 1)["estado"] == "dudoso"


def test_etapa_hueco_no_repite_el_principal_y_a_igualdad_gana_el_principal(ctx):
    """Un hueco (pizarrón, práctica) no se arregla repitiendo el ASR; si el LLM deja el mismo
    hueco, se queda el texto del ASR aunque el LLM cubra más tiempo."""
    preparar(ctx, [(0, 600_000)], habla=[(0, 600_000)])
    # ASR: frases de 15 s con pausas de 1 s; LLM: frases contiguas. Los dos sin nada entre 03:20 y 04:20.
    asr = [(i * 16_000, i * 16_000 + 15_000, "profe", f"Frase del asr {i}.") for i in range(37)
           if not 200_000 <= i * 16_000 < 260_000 and not 200_000 < i * 16_000 + 15_000 <= 260_000]
    llm = [(i * 20_000, i * 20_000 + 20_000, "profe", f"Frase del llm {i}.") for i in range(30)
           if not 200_000 <= i * 20_000 < 260_000]
    principal = TranscriptorFalso("asr", asr)
    respaldo = TranscriptorFalso("llm", llm, modelo="pro")
    con_transcriptores(ctx, principal, respaldo)
    r = etapa.ejecutar(ctx, job(1))
    assert principal.llamadas == 1 and respaldo.llamadas == 1
    assert r.motor == "asr:falso-1"
    datos = json.loads((ctx.sesion_dir(SID) / "transcript" / "bloque_01.json").read_text(encoding="utf-8"))
    assert datos["estado"] == "dudoso" and any("hueco" in a for a in datos["avisos"])
    # Un problema que no es hueco (vacía) sí repite el principal.
    principal = TranscriptorFalso("asr", [])
    con_transcriptores(ctx, principal, TranscriptorFalso("llm", llm, modelo="pro"))
    etapa.ejecutar(ctx, job(1))
    assert principal.llamadas == 2


# Clase de Macro del 27/10: el intento 1 no validó (texto en loop) y el 2 chocó con el límite por
# minuto; posponer volvía a empezar por el intento 1 sin gastar intentos, una vuelta por minuto.
EN_LOOP = [(i * 2000, i * 2000 + 2000, "profe", "Vale.") for i in range(4)] + [(8000, 60_000, "profe", "Sigue.")]


def test_etapa_sin_cuota_al_repetir_el_principal_sigue_con_lo_que_respondio(ctx):
    preparar(ctx, [(0, 60_000)], habla=[(0, 60_000)])
    principal = TranscriptorFalso("asr", EN_LOOP, CuotaAgotada("Gemini 429", espera_s=45))
    con_transcriptores(ctx, principal)
    r = etapa.ejecutar(ctx, job(1))
    assert principal.llamadas == 2
    assert r.siguientes == [("linea_de_tiempo", {})]
    datos = json.loads((ctx.sesion_dir(SID) / "transcript" / "bloque_01.json").read_text(encoding="utf-8"))
    assert datos["estado"] == "dudoso" and "loop" in datos["avisos"][0]
    assert datos["avisos"][-1] == "errores: Gemini 429"
    assert bloque_db(ctx, 1)["estado"] == "dudoso"
    # Con respaldo, se usa como si el principal hubiera agotado sus intentos.
    principal = TranscriptorFalso("asr", EN_LOOP, CuotaAgotada("Gemini 429", espera_s=45))
    respaldo = TranscriptorFalso("llm", [(0, 60_000, "profe", "Bien.")], modelo="pro")
    con_transcriptores(ctx, principal, respaldo)
    assert etapa.ejecutar(ctx, job(1)).motor == "llm:pro"
    assert principal.llamadas == 2 and respaldo.llamadas == 1


def test_etapa_vacio_y_sin_cuota_al_repetir_gasta_un_intento(ctx):
    """Vacío no hay con qué quedarse: el job se reintenta gastando un intento (CuotaAgotada no los
    gasta y lo repetiría para siempre); en el último, el bloque queda 'dudoso'."""
    preparar(ctx, [(0, 60_000)], habla=[(0, 60_000)])
    con_transcriptores(ctx, TranscriptorFalso("asr", [], CuotaAgotada("Gemini 429", espera_s=45)))
    with pytest.raises(ErrorEtapa) as info:
        etapa.ejecutar(ctx, job_intento(1, 1))
    assert info.value.reintentable and not isinstance(info.value, CuotaAgotada)
    assert bloque_db(ctx, 1)["estado"] == "pendiente"
    con_transcriptores(ctx, TranscriptorFalso("asr", [], CuotaAgotada("Gemini 429", espera_s=45)))
    r = etapa.ejecutar(ctx, job_intento(1, 4))
    assert r.siguientes == [("linea_de_tiempo", {})] and bloque_db(ctx, 1)["estado"] == "dudoso"


def test_con_el_worker_sin_cuota_al_repetir_no_queda_en_loop(ctx):
    from classmer import cola
    from classmer.worker import Worker

    preparar(ctx, [(0, 60_000)], habla=[(0, 60_000)])
    with ctx.base.conexion() as conn:
        cola.encolar(conn, "transcribir", SID, {"n": 1})
    con_transcriptores(ctx, TranscriptorFalso("asr", EN_LOOP, CuotaAgotada("Gemini 429", espera_s=45)))
    with ctx.base.conexion() as conn:
        tomado = cola.tomar(conn, ["transcribir"])
    Worker(ctx).ejecutar_job(tomado)
    with ctx.base.conexion() as conn:
        tipos = [(f["tipo"], f["estado"]) for f in conn.execute("SELECT tipo, estado FROM jobs ORDER BY id")]
    assert tipos == [("transcribir", "hecho"), ("linea_de_tiempo", "pendiente")]


def test_con_el_worker_encadena_y_pospone(ctx):
    """Los jobs reales de la cola: el último bloque encola linea_de_tiempo; la cuota pospone."""
    from classmer import cola
    from classmer.worker import Worker

    preparar(ctx, [(0, 60_000)], habla=[(0, 20_000)])
    with ctx.base.conexion() as conn:
        cola.encolar(conn, "transcribir", SID, {"n": 1})
    con_transcriptores(ctx, TranscriptorFalso("asr", CuotaAgotada("sin cuota", espera_s=60)))
    worker = Worker(ctx)
    with ctx.base.conexion() as conn:
        tomado = cola.tomar(conn, ["transcribir"])
    worker.ejecutar_job(tomado)
    with ctx.base.conexion() as conn:
        fila = conn.execute("SELECT estado, intentos, run_after FROM jobs WHERE id = ?", (tomado.id,)).fetchone()
    assert fila["estado"] == "pendiente" and fila["intentos"] == 0

    con_transcriptores(ctx, TranscriptorFalso("asr", [(0, 20_000, "profe", "Ok.")]))
    with ctx.base.conexion() as conn:
        tomado = cola.tomar(conn, ["transcribir"], ahora=fila["run_after"] + 1)
    worker.ejecutar_job(tomado)
    with ctx.base.conexion() as conn:
        tipos = [(f["tipo"], f["estado"]) for f in conn.execute("SELECT tipo, estado FROM jobs ORDER BY id")]
    assert tipos == [("transcribir", "hecho"), ("linea_de_tiempo", "pendiente")]
