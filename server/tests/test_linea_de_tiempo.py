"""Etapa linea_de_tiempo: formato exacto, orden, fotos renombradas y rotadas, fotos sin hora."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from classmer import sesiones
from classmer.cola import Job
from classmer.pipeline import ErrorEtapa
from classmer.pipeline.linea_de_tiempo import ejecutar, fecha_local, normalizar_hablante

SID = "tl-0001"


def armar(ctx, sid: str = SID, materia_id: str | None = "prueba", tipo: str | None = "teorica") -> Path:
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id=materia_id,
                       origen_materia="manual" if materia_id else "auto", tipo_clase=tipo,
                       inicio="2026-10-15T14:00:05-03:00", dispositivo="test")
    return ctx.sesion_dir(sid)


def bloque(ctx, sid: str, n: int, inicio: int, fin: int, segmentos: list[dict], estado: str = "transcripto"):
    carpeta = ctx.sesion_dir(sid)
    with ctx.base.conexion() as conn:
        conn.execute(
            "INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta, estado) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, n, inicio, fin, f"bloques/bloque_{n:02d}.mp3", estado),
        )
    (carpeta / "transcript").mkdir(exist_ok=True)
    datos = {"n": n, "inicio_ms": inicio, "fin_ms": fin, "motor": "fake", "modelo": "fake",
             "estado": estado, "avisos": [], "segmentos": segmentos}
    (carpeta / "transcript" / f"bloque_{n:02d}.json").write_text(json.dumps(datos, ensure_ascii=False),
                                                                 encoding="utf-8")


def seg(inicio: int, hablante: str | None, texto: str) -> dict:
    return {"inicio_ms": inicio, "fin_ms": inicio + 4000, "hablante": hablante, "texto": texto}


def jpeg(ruta: Path, tam: tuple[int, int] = (300, 200), orientacion: int | None = None) -> Path:
    """Mitad izquierda roja, derecha azul; con EXIF Orientation si se pide."""
    img = Image.new("RGB", tam, (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, tam[0] // 2, tam[1]))
    exif = Image.Exif()
    if orientacion:
        exif[0x0112] = orientacion
    img.save(ruta, "JPEG", quality=95, exif=exif.tobytes())
    return ruta


def evento(ctx, sid: str, eid: str, tipo: str, offset: int | None, **kw):
    sesiones.registrar_evento(ctx.cfg, ctx.base, sid, evento_id=eid, tipo=tipo, offset_ms=offset, **kw)


def foto(ctx, sid: str, eid: str, offset: int | None, origen: Path):
    evento(ctx, sid, eid, "foto", offset, archivo=origen, nombre_archivo=f"{eid}.jpg")


def job(sid: str = SID) -> Job:
    return Job(id=1, tipo="linea_de_tiempo", sesion_id=sid)


@pytest.fixture
def clase(ctx, tmp_path) -> Path:
    """Sesión completa: 2 bloques, fotos (una rotada, una grande, una rota, una sin hora) y eventos."""
    carpeta = armar(ctx)
    bloque(ctx, SID, 1, 0, 60_000, [
        seg(1_000, "PROFE", "Hoy vemos derivadas."),
        seg(10_800, "alumno", "¿Entra en el parcial?"),
    ])
    bloque(ctx, SID, 2, 60_000, 120_000, [
        seg(75_000, "", "texto\ncon   salto"),
        seg(61_000, "Profesora", "Sí, entra."),
        seg(80_000, "PROFE", "   "),  # vacío: se descarta
    ])
    origenes = tmp_path / "origenes"
    origenes.mkdir()
    foto(ctx, SID, "f-rotada", 10_200, jpeg(origenes / "a.jpg", (300, 200), orientacion=6))
    foto(ctx, SID, "f-grande", 5_000, jpeg(origenes / "b.jpg", (3000, 1500)))
    rota = origenes / "c.jpg"
    rota.write_bytes(b"\xff\xd8\xff no es un jpeg")
    foto(ctx, SID, "f-rota", 40_000, rota)
    foto(ctx, SID, "f-sin-hora", None, jpeg(origenes / "d.jpg", (400, 300)))
    evento(ctx, SID, "m-1", "marca", 10_900, marca="importante")
    evento(ctx, SID, "n-1", "nota", 30_000, texto='preguntar por qué\nel gradiente')
    # Llegan en otro orden; la hora del celu (creado) ordena pausa → reanudar.
    evento(ctx, SID, "r-1", "reanudar", 50_000, creado="2026-10-15T14:01:00-03:00")
    evento(ctx, SID, "p-1", "pausa", 50_000, creado="2026-10-15T14:00:55-03:00")
    evento(ctx, SID, "m-2", "marca", 75_400, marca="no_entendi")
    return carpeta


def test_timeline_formato_exacto(ctx, clase):
    r = ejecutar(ctx, job())
    assert r.siguientes == [("clasificar", {})]
    lineas = (clase / "timeline.md").read_text(encoding="utf-8").splitlines()
    assert lineas == [
        "# Clase: Materia de Prueba — 2026-10-15 — teórica",
        "[00:00:01] PROFE: Hoy vemos derivadas.",
        "[00:00:05] FOTO fotos/pizarra_01.jpg",
        "[00:00:10] ALUMNO: ¿Entra en el parcial?",   # el habla de ese segundo va antes
        "[00:00:10] FOTO fotos/pizarra_02.jpg",
        "[00:00:10] MARCA importante",
        '[00:00:30] NOTA "preguntar por qué el gradiente"',
        "[00:00:50] PAUSA",
        "[00:00:50] REANUDAR",
        "[00:01:01] PROFE: Sí, entra.",
        "[00:01:15] DESCONOCIDO: texto con salto",
        "[00:01:15] MARCA no_entendi",
        "# Fotos sin hora",
        "FOTO fotos/pizarra_03.jpg",
    ]


def test_transcript_json_y_md(ctx, clase):
    ejecutar(ctx, job())
    tr = json.loads((clase / "transcript.json").read_text(encoding="utf-8"))
    assert [(s["bloque"], s["inicio_ms"], s["hablante"]) for s in tr] == [
        (1, 1_000, "PROFE"), (1, 10_800, "ALUMNO"), (2, 61_000, "PROFE"), (2, 75_000, "DESCONOCIDO"),
    ]
    assert set(tr[0]) == {"bloque", "inicio_ms", "fin_ms", "hablante", "texto"}
    md = (clase / "transcript.md").read_text(encoding="utf-8").splitlines()
    assert md == [
        "# Transcript: Materia de Prueba — 2026-10-15 — teórica",
        "[00:00:01] PROFE: Hoy vemos derivadas.",
        "[00:00:10] ALUMNO: ¿Entra en el parcial?",
        "[00:01:01] PROFE: Sí, entra.",
        "[00:01:15] DESCONOCIDO: texto con salto",
    ]


def test_fotos_renombradas_nativas_y_rotadas(ctx, clase):
    ejecutar(ctx, job())
    fotos_dir = clase / "fotos_pdf"
    assert sorted(p.name for p in fotos_dir.iterdir()) == ["pizarra_01.jpg", "pizarra_02.jpg", "pizarra_03.jpg"]

    # §7, fotos nativas: la de 3000x1500 sin orientación pasa byte a byte (ni se achica ni se recomprime).
    assert (fotos_dir / "pizarra_01.jpg").read_bytes() == (clase / "fotos" / "f-grande.jpg").read_bytes()

    with Image.open(fotos_dir / "pizarra_02.jpg") as rot:  # Orientation=6: queda transpuesta
        assert rot.size == (200, 300)
        assert rot.getexif().get(0x0112) in (None, 1)
        arriba, abajo = rot.convert("RGB").getpixel((100, 60)), rot.convert("RGB").getpixel((100, 240))
    assert arriba[0] > 200 and arriba[2] < 60   # la mitad izquierda (roja) quedó arriba
    assert abajo[2] > 200 and abajo[0] < 60

    tl = json.loads((clase / "timeline.json").read_text(encoding="utf-8"))
    assert [(f["id"], f["nombre"], f["offset_ms"]) for f in tl["fotos"]] == [
        ("f-grande", "pizarra_01.jpg", 5_000),
        ("f-rotada", "pizarra_02.jpg", 10_200),
        ("f-sin-hora", "pizarra_03.jpg", None),
    ]
    assert tl["fotos"][0]["original"] == "fotos/f-grande.jpg"
    assert [(f["tratamiento"], f["tamano"]) for f in tl["fotos"]][:2] == [
        ("original", [3000, 1500]), ("rotada", [200, 300])]
    assert [e["tipo"] for e in tl["eventos"]] == ["foto", "foto", "marca", "nota", "pausa", "reanudar", "marca"]
    assert tl["eventos"][1]["foto"] == "pizarra_02.jpg"
    assert tl["eventos"][3]["texto"] == "preguntar por qué el gradiente"
    assert tl["eventos"][6]["marca"] == "no_entendi"
    assert any("fotos/f-rota.jpg" in a for a in tl["avisos"])  # la rota se avisa y se saltea
    log = (clase / "pipeline.log").read_text(encoding="utf-8")
    assert "linea_de_tiempo: aviso: no pude abrir la foto fotos/f-rota.jpg" in log


def test_reejecutar_borra_fotos_viejas(ctx, clase):
    ejecutar(ctx, job())
    with ctx.base.conexion() as conn:
        conn.execute("DELETE FROM assets WHERE id = 'f-grande'")
    ejecutar(ctx, job())
    assert sorted(p.name for p in (clase / "fotos_pdf").iterdir()) == ["pizarra_01.jpg", "pizarra_02.jpg"]
    lineas = (clase / "timeline.md").read_text(encoding="utf-8").splitlines()
    assert "[00:00:10] FOTO fotos/pizarra_01.jpg" in lineas
    assert lineas[-2:] == ["# Fotos sin hora", "FOTO fotos/pizarra_02.jpg"]


def test_sin_materia_sin_tipo_y_sin_bloques(ctx):
    carpeta = armar(ctx, "tl-auto", materia_id=None, tipo=None)
    evento(ctx, "tl-auto", "n-1", "nota", 3_723_000, texto="algo")
    r = ejecutar(ctx, job("tl-auto"))
    assert r.siguientes == [("clasificar", {})]
    assert (carpeta / "timeline.md").read_text(encoding="utf-8").splitlines() == [
        "# Clase: materia sin asignar — 2026-10-15 — tipo sin asignar",
        '[01:02:03] NOTA "algo"',
    ]
    assert json.loads((carpeta / "transcript.json").read_text(encoding="utf-8")) == []
    assert not (carpeta / "fotos_pdf").exists() or not any((carpeta / "fotos_pdf").iterdir())


def test_falta_transcript_de_un_bloque(ctx):
    armar(ctx)
    bloque(ctx, SID, 1, 0, 60_000, [seg(0, "PROFE", "hola")])
    with ctx.base.conexion() as conn:
        conn.execute("INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta, estado) "
                     "VALUES (?, 2, 60000, 120000, 'bloques/bloque_02.mp3', 'error')", (SID,))
    with pytest.raises(ErrorEtapa) as e:
        ejecutar(ctx, job())
    assert e.value.reintentable is False and "bloque 2" in str(e.value)


def test_bloque_dudoso_avisa(ctx):
    carpeta = armar(ctx)
    bloque(ctx, SID, 1, 0, 60_000, [seg(0, "PROFE", "hola")], estado="dudoso")
    ejecutar(ctx, job())
    tl = json.loads((carpeta / "timeline.json").read_text(encoding="utf-8"))
    assert any("bloque 1 quedó dudoso" in a for a in tl["avisos"])


def test_marca_silencios_descartados_y_huecos_de_bloques_dudosos(ctx):
    carpeta = armar(ctx)
    # Bloque 1 [0, 60.25 s]; silencio largo descartado 60–100 s; bloque 2 [99.75, 220 s] dudoso
    # con un hueco de 60 s con habla y sin texto (130–190 s).
    (carpeta / "silencios.json").write_text(json.dumps({
        "duracion_ms": 220_000, "silencios": [{"inicio_ms": 60_000, "fin_ms": 100_000}],
        "descartados": [{"inicio_ms": 60_000, "fin_ms": 100_000}],
        "habla": [{"inicio_ms": 0, "fin_ms": 60_000}, {"inicio_ms": 100_000, "fin_ms": 220_000}],
    }), encoding="utf-8")
    bloque(ctx, SID, 1, 0, 60_250, [
        seg(1_000, "PROFE", "Hola."),
        {"inicio_ms": 20_000, "fin_ms": 21_000, "hablante": "PROFE", "texto": "Corto."},  # hueco, pero no dudoso
    ])
    bloque(ctx, SID, 2, 99_750, 220_000, [
        {"inicio_ms": 100_000, "fin_ms": 130_000, "hablante": "PROFE", "texto": "Seguimos."},
        {"inicio_ms": 190_000, "fin_ms": 220_000, "hablante": "PROFE", "texto": "Listo."},
    ], estado="dudoso")
    evento(ctx, SID, "n-1", "nota", 70_000, texto="durante el silencio")

    ejecutar(ctx, job())

    assert (carpeta / "timeline.md").read_text(encoding="utf-8").splitlines() == [
        "# Clase: Materia de Prueba — 2026-10-15 — teórica",
        "[00:00:01] PROFE: Hola.",
        "[00:00:20] PROFE: Corto.",
        "[00:01:00] SILENCIO hasta 00:01:40 (no se transcribió)",
        '[00:01:10] NOTA "durante el silencio"',
        "[00:01:40] PROFE: Seguimos.",
        "[00:02:10] AVISO: tramo con habla sin transcribir hasta 00:03:10",
        "[00:03:10] PROFE: Listo.",
    ]
    # transcript.md y transcript.json siguen siendo sólo el habla.
    md = (carpeta / "transcript.md").read_text(encoding="utf-8")
    assert "SILENCIO" not in md and "AVISO" not in md
    assert len(json.loads((carpeta / "transcript.json").read_text(encoding="utf-8"))) == 4


def test_bloque_dudoso_sin_texto_no_agrega_aviso(ctx):
    # transcribir cubre el bloque entero con el texto de inaudible: ya está marcado, no hay hueco.
    carpeta = armar(ctx)
    bloque(ctx, SID, 1, 0, 600_000, [{"inicio_ms": 0, "fin_ms": 600_000, "hablante": "desconocido",
                                      "texto": "[inaudible: no se pudo transcribir este bloque]"}],
           estado="dudoso")
    ejecutar(ctx, job())
    lineas = (carpeta / "timeline.md").read_text(encoding="utf-8").splitlines()
    assert lineas[1:] == ["[00:00:00] DESCONOCIDO: [inaudible: no se pudo transcribir este bloque]"]


def test_helpers(cfg):
    assert fecha_local(cfg, "2026-10-16T01:30:00Z") == "2026-10-15"
    assert normalizar_hablante("profesor") == "PROFE"
    assert normalizar_hablante("Alumna") == "ALUMNO"
    assert normalizar_hablante(None) == "DESCONOCIDO"
    assert normalizar_hablante("ayudante") == "AYUDANTE"


# --- fotos nativas (§7): sólo se tocan si hay que rotarlas, achicarlas por la API o convertirlas ---

def _foto_pesada(ruta: Path, tam: tuple[int, int]) -> Path:
    """JPEG con ruido (comprime mal, como una foto real): 4000x3000 pesa varios MB."""
    ruido = Image.effect_noise(tam, 60).convert("RGB")
    fondo = Image.new("RGB", tam, (40, 90, 60))
    Image.blend(fondo, ruido, 0.5).save(ruta, "JPEG", quality=90)
    return ruta


def test_foto_de_4000x3000_de_varios_mb_pasa_intacta(tmp_path):
    from classmer.pipeline.linea_de_tiempo import BYTES_MAX_API, procesar_foto

    origen = _foto_pesada(tmp_path / "s23.jpg", (4000, 3000))
    assert 2_000_000 < origen.stat().st_size < BYTES_MAX_API
    ancho, alto, accion = procesar_foto(origen, tmp_path / "pizarra_01.jpg")
    assert (ancho, alto, accion) == (4000, 3000, "original")
    assert (tmp_path / "pizarra_01.jpg").read_bytes() == origen.read_bytes()


def test_foto_de_mas_de_8000_px_se_reduce_a_4000(tmp_path):
    from classmer.pipeline.linea_de_tiempo import procesar_foto

    origen = jpeg(tmp_path / "200mp.jpg", (8200, 1000))
    ancho, alto, accion = procesar_foto(origen, tmp_path / "p.jpg")
    assert accion == "reducida" and max(ancho, alto) == 4000
    with Image.open(tmp_path / "p.jpg") as img:
        assert img.size == (4000, 488) and img.format == "JPEG"


def test_foto_de_mas_bytes_que_la_api_se_reduce(tmp_path, monkeypatch):
    from classmer.pipeline import linea_de_tiempo

    origen = _foto_pesada(tmp_path / "pesada.jpg", (5000, 3750))
    monkeypatch.setattr(linea_de_tiempo, "BYTES_MAX_API", origen.stat().st_size - 1)
    ancho, alto, accion = linea_de_tiempo.procesar_foto(origen, tmp_path / "p.jpg")
    assert accion == "reducida" and (ancho, alto) == (4000, 3000)


def test_foto_con_orientacion_6_queda_derecha_y_png_se_convierte(tmp_path):
    from classmer.pipeline.linea_de_tiempo import procesar_foto

    rot = jpeg(tmp_path / "vertical.jpg", (300, 200), orientacion=6)
    assert procesar_foto(rot, tmp_path / "r.jpg") == (200, 300, "rotada")
    with Image.open(tmp_path / "r.jpg") as img:
        assert img.getexif().get(0x0112) in (None, 1)
        assert img.convert("RGB").getpixel((100, 60))[0] > 200   # la mitad roja quedó arriba
    png = tmp_path / "captura.png"
    Image.new("RGBA", (120, 80), (10, 20, 30, 255)).save(png)
    assert procesar_foto(png, tmp_path / "c.jpg") == (120, 80, "convertida")
    with Image.open(tmp_path / "c.jpg") as img:
        assert img.format == "JPEG"
