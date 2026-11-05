"""Etapa segmentar: plan de cortes (puro) y corrida real con audios sintéticos de ffmpeg (lavfi).

Config de conftest: bloque_s=60, ventana_s=15, silencio_largo_s=8 (silencio_db=-35, min 0,7 s).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import requiere

from classmer import audio, sesiones
from classmer.cola import Job
from classmer.pipeline import ErrorEtapa
from classmer.pipeline import segmentar as seg
from classmer.util import leer_json

TOL_MS = 150  # silencedetect sobre Opus/MP3 corre los bordes unas decenas de ms

# Audio principal (~3:48): (tipo, segundos). Silencios en 30, 58.8–60.3, 100, 118.5–119.5,
# 122–124, 135, 150–160 (largo), 180 y 215.
PARTES = [
    ("voz", 30), ("sil", 1), ("voz", 27.8), ("sil", 1.5), ("voz", 39.7), ("sil", 1),
    ("voz", 17.5), ("sil", 1), ("voz", 2.5), ("sil", 2), ("voz", 11), ("sil", 1),
    ("voz", 14), ("sil", 10), ("voz", 20), ("sil", 1), ("voz", 34), ("sil", 1), ("voz", 12),
]
SILENCIOS_ESPERADOS = [
    (30000, 31000), (58800, 60300), (100000, 101000), (118500, 119500), (122000, 124000),
    (135000, 136000), (150000, 160000), (180000, 181000), (215000, 216000),
]
# Tramo 1 [0, 150.25]: objetivo 60 → silencio 58.8–60.3 (medio 59.55); objetivo 119.55 → 118.5–119.5
# (medio 119.0, más cerca que 122–124); quedan 31.25 s. Silencio largo 150–160 excluido (margen 250 ms).
# Tramo 2 [159.75, 228]: 68.25 s ≤ 75 → un solo bloque (no se corta en 215.5: quedaría uno de 12.5 s).
BLOQUES_ESPERADOS = [(0, 59550), (59550, 119000), (119000, 150250), (159750, 228000)]


# --- helpers -----------------------------------------------------------------------------------

def generar_audio(ruta: Path, partes: list[tuple[str, float]], codec: list[str]) -> None:
    """Concatena tonos (voz) y silencio digital (sil) con lavfi y codifica a `ruta`."""
    entradas: list[str] = []
    for i, (tipo, dur) in enumerate(partes):
        fuente = (f"sine=f={300 + 40 * i}:d={dur}:sample_rate=48000" if tipo == "voz"
                  else f"anullsrc=r=48000:cl=mono:d={dur}")
        entradas += ["-f", "lavfi", "-i", fuente]
    filtro = "".join(f"[{i}:a]" for i in range(len(partes))) + f"concat=n={len(partes)}:v=0:a=1[out]"
    r = audio.ejecutar(["ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-y", *entradas,
                        "-filter_complex", filtro, "-map", "[out]", "-ac", "1", *codec, str(ruta)],
                       timeout=120)
    assert r.returncode == 0, r.stderr


OPUS = ["-c:a", "libopus", "-b:a", "32k"]   # como graba el celu
AAC = ["-c:a", "aac", "-b:a", "64k"]        # grabadora de Samsung (importado)


def preparar(ctx, sid: str, origen: Path, nombre: str = "audio.ogg") -> Job:
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio="2026-10-15T14:00:00-03:00",
                       dispositivo="pytest", audio_nombre=nombre)
    shutil.copyfile(origen, ctx.sesion_dir(sid) / nombre)
    return Job(id=1, tipo="segmentar", sesion_id=sid)


def filas_bloques(ctx, sid: str) -> list[dict]:
    with ctx.base.conexion() as conn:
        return [dict(f) for f in conn.execute(
            "SELECT n, inicio_ms, fin_ms, ruta, estado FROM bloques WHERE sesion_id = ? ORDER BY n", (sid,))]


def cerca(a: int, b: int, tol: int = TOL_MS) -> bool:
    return abs(a - b) <= tol


def unir(tramos: list[tuple[int, int]]) -> list[tuple[int, int]]:
    unidos: list[tuple[int, int]] = []
    for a, b in sorted(tramos):
        if unidos and a <= unidos[-1][1]:
            unidos[-1] = (unidos[-1][0], max(unidos[-1][1], b))
        else:
            unidos.append((a, b))
    return unidos


@pytest.fixture(scope="module")
def audio_clase(tmp_path_factory) -> Path:
    ruta = tmp_path_factory.mktemp("audios") / "clase.ogg"
    generar_audio(ruta, PARTES, OPUS)
    return ruta


# --- plan de cortes (sin ffmpeg) ---------------------------------------------------------------

PARAMS = {"bloque_ms": 60000, "ventana_ms": 15000, "largo_ms": 8000}


def test_corte_duro_sin_silencios():
    s = seg.planificar(150000, [], **PARAMS)
    assert s.bloques == [(0, 60000), (60000, 120000), (120000, 150000)]
    assert s.cortes_duros == 2
    assert s.habla == [(0, 150000)] and s.descartados == []


def test_elige_el_silencio_mas_cercano_y_desempata_por_largo():
    # Medios: 59.9 s (0.8 s de largo), 61.0 s (1 s, empata: a menos de 1 s de la mejor distancia)
    # y 69 s (6 s, lejos). Gana el de 61.0 por ser más largo que el de 59.9.
    silencios = [(59500, 60300), (60500, 61500), (66000, 72000)]
    s = seg.planificar(150000, silencios, **PARAMS)
    assert s.bloques[0] == (0, 61000)
    # Sin empate (el largo queda a más de 1 s), gana el más cercano.
    s = seg.planificar(150000, [(59500, 60300), (61500, 62500)], **PARAMS)
    assert s.bloques[0] == (0, 59900)


def test_silencio_fuera_de_ventana_no_sirve():
    s = seg.planificar(150000, [(40000, 41000)], **PARAMS)  # medio 40.5 s: fuera de 60 ± 15
    assert s.bloques[0] == (0, 60000) and s.cortes_duros == 2


def test_ultimo_bloque_corto_se_une_al_anterior():
    s = seg.planificar(130000, [(59000, 60000), (120000, 121000)], **PARAMS)
    assert s.bloques == [(0, 59500), (59500, 130000)]
    assert all(b - a >= 15000 for a, b in s.bloques)


def test_silencios_largos_al_principio_y_al_final():
    s = seg.planificar(100000, [(0, 20000), (90000, 100000)], **PARAMS)
    assert s.descartados == [(0, 20000), (90000, 100000)]
    assert s.bloques == [(19750, 90250)]
    assert s.habla == [(20000, 90000)]


def test_tramo_sin_voz_entre_silencios_largos_no_crea_bloque():
    # Entre 20.0 y 20.3 s hay un ruidito de 300 ms: no alcanza para un bloque.
    s = seg.planificar(60000, [(10000, 20000), (20300, 30000)], **PARAMS)
    assert s.bloques == [(0, 10250), (29750, 60000)]


def test_audio_todo_silencio_no_tiene_bloques():
    assert seg.planificar(5000, [(0, 5000)], **PARAMS).bloques == []
    assert seg.planificar(30000, [(0, 30000)], **PARAMS).bloques == []
    assert seg.planificar(0, [], **PARAMS).bloques == []


def test_silencio_largo_sin_confirmar_con_umbral_estricto_no_se_descarta():
    # Voz baja: 50–70 s es silencio a -35 dB, pero a -47 dB sólo hay una pausa de 1 s.
    s = seg.planificar(150000, [(50000, 70000)], estrictos=[(55000, 56000), (120000, 120500)], **PARAMS)
    assert s.descartados == [] and s.rescatado_ms == 20000
    assert s.silencios == [(55000, 56000)]          # la pausa queda como lugar donde cortar
    assert s.habla == [(0, 55000), (56000, 150000)]
    assert s.bloques == [(0, 55500), (55500, 115500), (115500, 150000)]
    # Sin la detección estricta (como antes) el tramo se perdía.
    assert seg.planificar(150000, [(50000, 70000)], **PARAMS).descartados == [(50000, 70000)]


def test_silencio_largo_confirmado_se_descarta_entero_o_en_parte():
    # Confirmado: los bordes corridos unos ms entre umbrales se pegan.
    s = seg.planificar(140000, [(50000, 70000)], estrictos=[(50003, 69990)], **PARAMS)
    assert s.descartados == [(50000, 70000)] and s.rescatado_ms == 0
    assert s.bloques == [(0, 50250), (69750, 140000)]
    # Confirmado sólo al final: se descarta esa parte; lo anterior es habla (posible voz baja).
    s = seg.planificar(150000, [(40000, 100000)], estrictos=[(70000, 100000)], **PARAMS)
    assert s.descartados == [(70000, 100000)] and s.rescatado_ms == 30000
    assert s.habla == [(0, 70000), (100000, 150000)]
    assert s.bloques == [(0, 70250), (99750, 150000)]


def test_normaliza_silencios_pisados_y_fuera_de_rango():
    s = seg.planificar(50000, [(30000, 31000), (-50, 1000), (30500, 32000), (49000, 51000)], **PARAMS)
    assert s.silencios == [(0, 1000), (30000, 32000), (49000, 50000)]
    assert s.habla == [(1000, 30000), (32000, 49000)]


def test_parsear_silencedetect_con_y_sin_silence_end_final():
    salida = (
        "[Parsed_silencedetect_0 @ 0x1] silence_start: 0\n"
        "[Parsed_silencedetect_0 @ 0x1] silence_end: 2.000083 | silence_duration: 2.000083\n"
        "[Parsed_silencedetect_0 @ 0x1] silence_start: 5.999958\n"
        "size=N/A time=00:00:09.00 bitrate=N/A speed=1.54e+03x\n"
    )
    silencios, decodificado = audio.parsear_silencedetect(salida)
    assert silencios == [(0, 2000), (6000, 9000)] and decodificado == 9000
    # Sin la línea time=, el silencio abierto se cierra con la duración que se pasa.
    sin_tiempo = "\n".join(salida.splitlines()[:3])
    assert audio.parsear_silencedetect(sin_tiempo, 9500) == ([(0, 2000), (6000, 9500)], None)
    assert audio.parsear_silencedetect(sin_tiempo) == ([(0, 2000)], None)


def test_falta_el_audio_es_error_no_reintentable(ctx):
    sid = "seg-sin-audio"
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id="prueba", origen_materia="manual",
                       tipo_clase=None, inicio="2026-10-15T14:00:00-03:00", dispositivo="pytest")
    with pytest.raises(ErrorEtapa) as e:
        seg.ejecutar(ctx, Job(id=1, tipo="segmentar", sesion_id=sid))
    assert not e.value.reintentable


def test_error_de_ffmpeg_transitorio_o_no():
    invalido = audio.ErrorAudio("ffmpeg no pudo analizar x: Invalid data found")
    assert not seg._error_ffmpeg(invalido).reintentable
    assert seg._error_ffmpeg(audio.ErrorAudio("ffmpeg no pudo cortar x: Permission denied")).reintentable
    try:
        try:
            raise subprocess.TimeoutExpired("ffmpeg", 1)
        except subprocess.TimeoutExpired as t:
            raise audio.ErrorAudio("ffmpeg tardó demasiado") from t
    except audio.ErrorAudio as e:
        assert seg._error_ffmpeg(e).reintentable


# --- corrida real con ffmpeg -------------------------------------------------------------------

@pytest.mark.ffmpeg
@requiere("ffmpeg")
def test_segmenta_clase_sintetica(ctx, audio_clase):
    sid = "seg-clase-1"
    job = preparar(ctx, sid, audio_clase)
    carpeta = ctx.sesion_dir(sid)

    r = seg.ejecutar(ctx, job)

    assert r.siguientes == [("transcribir", {"n": n}) for n in (1, 2, 3, 4)]
    assert r.log and "4 bloque(s)" in r.log

    # silencios.json: duración, silencios, el largo descartado y el habla (complemento).
    sil = leer_json(carpeta / "silencios.json")
    assert set(sil) == {"duracion_ms", "silencios", "descartados", "habla"}
    assert cerca(sil["duracion_ms"], 228000, 50)
    detectados = [(s["inicio_ms"], s["fin_ms"]) for s in sil["silencios"]]
    assert len(detectados) == len(SILENCIOS_ESPERADOS)
    for (a, b), (ea, eb) in zip(detectados, SILENCIOS_ESPERADOS):
        assert cerca(a, ea) and cerca(b, eb), (a, b, ea, eb)
    assert len(sil["descartados"]) == 1
    largo = (sil["descartados"][0]["inicio_ms"], sil["descartados"][0]["fin_ms"])
    assert cerca(largo[0], 150000) and cerca(largo[1], 160000)
    habla = [(h["inicio_ms"], h["fin_ms"]) for h in sil["habla"]]
    assert len(habla) == len(SILENCIOS_ESPERADOS) + 1

    # bloques.json: cortes en silencios cerca de cada minuto, offsets absolutos.
    bloques = leer_json(carpeta / "bloques.json")
    assert [b["n"] for b in bloques] == [1, 2, 3, 4]
    assert [b["ruta"] for b in bloques] == [f"bloques/bloque_{n:02d}.mp3" for n in (1, 2, 3, 4)]
    tramos = [(b["inicio_ms"], b["fin_ms"]) for b in bloques]
    for (a, b), (ea, eb) in zip(tramos, BLOQUES_ESPERADOS):
        assert cerca(a, ea) and cerca(b, eb), (tramos, BLOQUES_ESPERADOS)
    assert tramos[0][0] == 0 and tramos[-1][1] == sil["duracion_ms"]
    assert tramos[0][1] == tramos[1][0] and tramos[1][1] == tramos[2][0]  # contiguos dentro del tramo

    # El silencio largo nunca queda dentro de un bloque (sólo el margen de 250 ms en cada punta).
    for a, b in tramos:
        assert b <= largo[0] + seg.MARGEN_MS or a >= largo[1] - seg.MARGEN_MS
    # El último bloque corto quedó unido: ninguno mide menos de 1/4 del objetivo.
    assert all(b - a >= 15000 for a, b in tramos)

    # Los bloques cubren todo el habla.
    cubierto = unir(tramos)
    for ha, hb in habla:
        assert any(ca <= ha and hb <= cb for ca, cb in cubierto), (ha, hb, cubierto)

    # MP3 mono 16 kHz 48 kbps, con la duración del tramo.
    for b in bloques:
        mp3 = carpeta / b["ruta"]
        assert mp3.is_file()
        info = audio.sondear(mp3)
        pista = next(s for s in info["streams"] if s["codec_type"] == "audio")
        assert pista["codec_name"] == "mp3"
        assert pista["channels"] == 1 and pista["sample_rate"] == "16000"
        assert abs(int(info["format"]["bit_rate"]) - 48000) < 2000
        assert cerca(audio.duracion_ms(mp3), b["fin_ms"] - b["inicio_ms"], 100)

    # Offsets conservados: los silencios internos de cada bloque, medidos en el MP3 y corridos por
    # inicio_ms, caen donde estaban en el original.
    for b in bloques:
        internos = [(a, f) for a, f in SILENCIOS_ESPERADOS
                    if a >= b["inicio_ms"] + 500 and f <= b["fin_ms"] - 500]
        assert internos, b
        medidos, _ = audio.detectar_silencios(carpeta / b["ruta"], umbral_db=-35, minimo_s=0.7)
        absolutos = [(a + b["inicio_ms"], f + b["inicio_ms"]) for a, f in medidos]
        for ea, eb in internos:
            assert any(cerca(a, ea) and cerca(f, eb) for a, f in absolutos), (b, absolutos, ea, eb)

    # Filas en la base, iguales a bloques.json y pendientes.
    assert filas_bloques(ctx, sid) == [
        {"n": b["n"], "inicio_ms": b["inicio_ms"], "fin_ms": b["fin_ms"], "ruta": b["ruta"],
         "estado": "pendiente"} for b in bloques
    ]
    assert "segmentar: 4 bloque(s)" in (carpeta / "pipeline.log").read_text(encoding="utf-8")


@pytest.mark.ffmpeg
@requiere("ffmpeg")
def test_reejecutar_es_idempotente(ctx, audio_clase):
    sid = "seg-clase-2"
    job = preparar(ctx, sid, audio_clase)
    carpeta = ctx.sesion_dir(sid)
    seg.ejecutar(ctx, job)
    antes_json = leer_json(carpeta / "bloques.json")
    antes_filas = filas_bloques(ctx, sid)

    # Basura de una corrida anterior: un bloque que ya no existe y una fila ya transcripta.
    (carpeta / "bloques" / "bloque_09.mp3").write_bytes(b"viejo")
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE bloques SET estado = 'transcripto' WHERE sesion_id = ? AND n = 1", (sid,))
        conn.execute("INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta, estado) "
                     "VALUES (?, 9, 0, 1, 'bloques/bloque_09.mp3', 'pendiente')", (sid,))

    r = seg.ejecutar(ctx, job)

    assert len(r.siguientes) == 4
    assert leer_json(carpeta / "bloques.json") == antes_json
    assert filas_bloques(ctx, sid) == antes_filas
    assert sorted(p.name for p in (carpeta / "bloques").iterdir()) == [
        f"bloque_{n:02d}.mp3" for n in (1, 2, 3, 4)
    ]


@pytest.mark.ffmpeg
@requiere("ffmpeg")
def test_ogg_cortado_a_mitad_de_pagina(ctx, audio_clase, tmp_path):
    # Como queda el .ogg del celu si se cortó la subida: termina en una página Ogg incompleta.
    datos = audio_clase.read_bytes()
    cortado = tmp_path / "cortado.ogg"
    cortado.write_bytes(datos[: int(len(datos) * 0.55) + 37])
    sid = "seg-cortado"
    job = preparar(ctx, sid, cortado)
    carpeta = ctx.sesion_dir(sid)

    r = seg.ejecutar(ctx, job)

    sil = leer_json(carpeta / "silencios.json")
    assert 100000 < sil["duracion_ms"] < 150000
    bloques = leer_json(carpeta / "bloques.json")
    assert len(r.siguientes) == len(bloques) >= 2
    assert bloques[-1]["fin_ms"] == sil["duracion_ms"]
    for b in bloques:
        assert cerca(audio.duracion_ms(carpeta / b["ruta"]), b["fin_ms"] - b["inicio_ms"], 100)


@pytest.mark.ffmpeg
@requiere("ffmpeg")
def test_audio_importado_m4a(ctx, tmp_path):
    # Arranca con 3 s de silencio (corto: queda en el bloque) y tiene un único silencio en 50 s.
    origen = tmp_path / "grabadora.m4a"
    generar_audio(origen, [("sil", 3), ("voz", 47), ("sil", 1), ("voz", 25)], AAC)
    sid = "seg-importado"
    job = preparar(ctx, sid, origen, nombre="audio.m4a")
    carpeta = ctx.sesion_dir(sid)

    r = seg.ejecutar(ctx, job)

    assert r.siguientes == [("transcribir", {"n": 1}), ("transcribir", {"n": 2})]
    tramos = [(b["inicio_ms"], b["fin_ms"]) for b in leer_json(carpeta / "bloques.json")]
    assert tramos[0][0] == 0 and cerca(tramos[0][1], 50500) and tramos[0][1] == tramos[1][0]
    assert cerca(tramos[1][1], 76000, 100)
    assert all((carpeta / f"bloques/bloque_{n:02d}.mp3").is_file() for n in (1, 2))


@pytest.mark.ffmpeg
@requiere("ffmpeg")
def test_audio_sin_voz_es_error_no_reintentable(ctx, tmp_path):
    origen = tmp_path / "mudo.ogg"
    generar_audio(origen, [("sil", 12)], OPUS)
    sid = "seg-mudo"
    job = preparar(ctx, sid, origen)

    with pytest.raises(ErrorEtapa) as e:
        seg.ejecutar(ctx, job)

    assert str(e.value) == "el audio no tiene voz" and not e.value.reintentable
    assert filas_bloques(ctx, sid) == []
    assert leer_json(ctx.sesion_dir(sid) / "bloques.json") == []


def generar_expr(ruta: Path, expr: str, dur: float) -> None:
    """Audio sintético a partir de una expresión de aevalsrc (las comas van escapadas)."""
    r = audio.ejecutar(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"aevalsrc={expr}:s=48000:d={dur}", "-ac", "1", *OPUS, str(ruta)], timeout=120)
    assert r.returncode == 0, r.stderr


@pytest.mark.ffmpeg
@requiere("ffmpeg")
def test_grabacion_baja_reintenta_con_umbral_mas_estricto(ctx, tmp_path):
    # Voz 20 dB más baja de lo normal (~-41 dB RMS): a -35 dB todo parece silencio largo y no se
    # transcribiría nada; con el umbral más estricto (-47 dB) aparece la voz.
    normal = tmp_path / "normal.wav"
    generar_audio(normal, [("voz", 70), ("sil", 1), ("voz", 79)], ["-c:a", "pcm_s16le"])
    origen = tmp_path / "bajo.ogg"
    r = audio.ejecutar(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(normal),
                        "-af", "volume=-20dB", *OPUS, str(origen)], timeout=120)
    assert r.returncode == 0, r.stderr
    sid = "seg-bajo"
    job = preparar(ctx, sid, origen)

    res = seg.ejecutar(ctx, job)

    assert len(res.siguientes) >= 2
    silencios = leer_json(ctx.sesion_dir(sid) / "silencios.json")
    voz = sum(b - a for a, b in ((h["inicio_ms"], h["fin_ms"]) for h in silencios["habla"]))
    assert voz > 140_000
    assert silencios["descartados"] == []
    log = (ctx.sesion_dir(sid) / "pipeline.log").read_text(encoding="utf-8")
    assert "puede ser voz baja" in log


@pytest.mark.ffmpeg
@requiere("ffmpeg")
def test_tramo_de_voz_baja_no_se_descarta(ctx, tmp_path):
    # "Voz" con pausas de 1,2 s cada 8 s; entre 40 y 60 s baja a -38 dBFS de pico (bajo -35 dB:
    # parece un silencio largo de 20 s). A -47 dB sólo son silencio las pausas: no se descarta.
    voz = "sin(2*PI*440*t)*gt(mod(t\\,8)\\,1.2)"
    origen = tmp_path / "voz_baja.ogg"
    generar_expr(origen, f"if(between(t\\,40\\,60)\\,0.012*{voz}\\,0.3*{voz})+0.001*(random(0)-0.5)", 100)
    sid = "seg-voz-baja"
    job = preparar(ctx, sid, origen)

    res = seg.ejecutar(ctx, job)

    carpeta = ctx.sesion_dir(sid)
    sil = leer_json(carpeta / "silencios.json")
    assert sil["descartados"] == []
    habla = [(h["inicio_ms"], h["fin_ms"]) for h in sil["habla"]]
    assert sum(min(b, 60000) - max(a, 40000) for a, b in habla if b > 40000 and a < 60000) > 15000
    tramos = [(b["inicio_ms"], b["fin_ms"]) for b in leer_json(carpeta / "bloques.json")]
    assert len(res.siguientes) == len(tramos) == 2
    assert unir(tramos) == [(0, sil["duracion_ms"])]   # el tramo bajo va a algún bloque
    assert 48000 < tramos[0][1] < 58000                 # se corta en una pausa, no en medio
    assert "puede ser voz baja" in (carpeta / "pipeline.log").read_text(encoding="utf-8")


@pytest.mark.ffmpeg
@requiere("ffmpeg")
def test_poca_voz_en_silencios_cortos_usa_umbral_estricto_en_todo(ctx, tmp_path):
    # 4 s bajo -35 dB y 1 s fuerte, todo el tiempo: a -35 dB son silencios cortos (no largos) y
    # queda 20 % de voz; con el umbral estricto es todo voz.
    origen = tmp_path / "bajo_cortado.ogg"
    generar_expr(origen, "if(lt(mod(t\\,5)\\,4)\\,0.012\\,0.3)*sin(2*PI*440*t)", 130)
    sid = "seg-poca-voz"
    job = preparar(ctx, sid, origen)

    res = seg.ejecutar(ctx, job)

    assert len(res.siguientes) == 2
    sil = leer_json(ctx.sesion_dir(sid) / "silencios.json")
    assert sum(h["fin_ms"] - h["inicio_ms"] for h in sil["habla"]) > 120_000
    log = (ctx.sesion_dir(sid) / "pipeline.log").read_text(encoding="utf-8")
    assert "poca voz" in log and "puede ser voz baja" not in log
