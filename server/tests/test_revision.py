"""Revisión del transcript (§15): la etapa, las dudas, la espera, el panel y el transcript revisado."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from classmer import cola, revision, sesiones
from classmer.cola import Job
from classmer.ia import revisor
from classmer.ia.claude_json import RespuestaInvalida, RespuestaJSON
from classmer.ia.transcriptor import ResultadoTranscripcion, SegmentoRel
from classmer.pipeline import EsperandoUsuario, Servicios
from classmer.pipeline import linea_de_tiempo as ldt
from classmer.pipeline import revisar_transcript as etapa
from classmer.util import leer_json
from conftest import requiere

SID = "rev-0001"


# --- un Claude falso con respuestas armadas por paso ----------------------------------------------

class RevisorFalso:
    """Responde según el esquema (el paso): concilia eligiendo el celu, limpia sacando "eh" y poniendo
    mayúscula y punto, y no resuelve dudas (salvo `resolver`). Anota cada pedido."""

    modelo = "claude-falso"
    esfuerzo = "medium"

    def __init__(self, *, dudas_conciliar: set[str] = frozenset(), dudas_limpiar: dict[str, str] | None = None,
                 resolver: dict[str, str] | None = None, alumnos: set[str] = frozenset(),
                 vaciar: set[str] = frozenset(), cortar_limpiar: bool = False):
        self.dudas_conciliar = set(dudas_conciliar)
        self.dudas_limpiar = dudas_limpiar or {}
        self.resolver = resolver or {}
        self.alumnos = set(alumnos)
        self.vaciar = set(vaciar)
        self.cortar_limpiar = cortar_limpiar
        self.pedidos: list[tuple[str, str]] = []

    def consultar(self, prompt: str, esquema: dict[str, Any]) -> RespuestaJSON:
        if esquema is revisor.ESQUEMA_CONCILIAR:
            paso, datos = "conciliar", self._conciliar(prompt)
        elif esquema is revisor.ESQUEMA_LIMPIAR:
            paso, datos = "limpiar", self._limpiar(prompt)
        else:
            paso, datos = "resolver", self._resolver(prompt)
        self.pedidos.append((paso, prompt))
        return RespuestaJSON(datos=datos, modelo=self.modelo, facturacion="api", costo=0.01,
                             uso={"entrada": 100, "salida": 50})

    def _conciliar(self, prompt: str) -> dict[str, Any]:
        pedidas = re.search(r"con el mismo id: (.*?)\. En `solo_celu`", prompt, re.S).group(1).split(", ")
        lineas = []
        for lid in pedidas:
            m = re.search(rf"^{lid} .*\n  reloj: (.*)\n  celu:  (.*)$", prompt, re.M)
            if m is None:
                continue
            lineas.append({"id": lid, "texto": m.group(2), "eleccion": "celu", "motivo": "el celu tiene sentido",
                           "confianza": "media", "duda": lid in self.dudas_conciliar})
        solo = [{"id": m.group(1), "incluir": True, "hablante": "alumno", "texto": m.group(2), "motivo": "pregunta"}
                for m in re.finditer(r"^(C\d{4}) \[[^\]]+\] sólo en el celu, \w+: (.*)$", prompt, re.M)]
        return {"lineas": lineas, "solo_celu": solo}

    def _limpiar(self, prompt: str) -> dict[str, Any]:
        if self.cortar_limpiar:
            raise RespuestaInvalida("cortada", cortada=True, costo=0.02)
        bloque = prompt.split("## Líneas", 1)[1]
        lineas = []
        actual = None
        for linea in bloque.splitlines():
            m = re.match(r"^([LC]\d{4}) \[[^\]]+\] (\w+): (.*)$", linea)
            if m:
                lid, rol, texto = m.groups()
                limpio = " ".join(w for w in texto.split() if w.lower().strip(",.") != "eh").strip()
                limpio = (limpio[:1].upper() + limpio[1:]).rstrip(".") + "." if limpio else ""
                if lid in self.vaciar:
                    limpio = ""
                actual = {"id": lid, "hablante": "alumno" if lid in self.alumnos or rol == "ALUMNO" else "profe",
                          "texto": limpio, "cambios": [], "alternativas": [],
                          "duda": ({"alternativa": self.dudas_limpiar[lid], "motivo": "puede ser otra cosa"}
                                   if lid in self.dudas_limpiar else None)}
                lineas.append(actual)
            elif linea.startswith("   (duda de la conciliación:") and actual is not None:
                for fuente, texto in re.findall(r"(\w+) «([^»]*)»", linea):
                    actual["alternativas"].append({"fuente": fuente, "texto": texto.upper()})
        return {"lineas": lineas}

    def _resolver(self, prompt: str) -> dict[str, Any]:
        dudas = []
        for m in re.finditer(r"^(D\d{2}) \[", prompt, re.M):
            did = m.group(1)
            if did in self.resolver:
                dudas.append({"id": did, "resuelta": True, "texto": self.resolver[did], "eleccion": "audio",
                              "motivo": "se oye claro", "confianza": "alta"})
        return {"dudas": dudas}


class TranscriptorFalso:
    nombre = "falso"
    modelo = "asr-falso"

    def __init__(self) -> None:
        self.llamadas: list[str] = []

    def transcribir(self, audio: Path, contexto: Any) -> ResultadoTranscripcion:
        self.llamadas.append(audio.name)
        return ResultadoTranscripcion(segmentos=[SegmentoRel(0, 1000, "profe", f"oído en {audio.stem}")],
                                      motor="falso", modelo="asr-falso")


# --- armado de la clase -----------------------------------------------------------------------------

def con_revisor(ctx, falso: RevisorFalso, transcriptor: TranscriptorFalso | None = None) -> None:
    ctx.servicios = Servicios(revisor=lambda cfg: falso,
                              transcriptor=(lambda cfg: transcriptor) if transcriptor else None)


def prender(ctx, *, profesor: str | None = "Ana Pérez", escuchar: int = 0, tipo: str = "asistida") -> None:
    materia = ctx.cfg.materia("prueba")
    materia.revision = tipo
    materia.profesor = profesor
    ctx.cfg.revisar_transcript.escuchar_max = escuchar


def armar(ctx, sid: str = SID, segmentos: list[dict] | None = None, *, estado: str = "procesando") -> Path:
    with ctx.base.conexion() as conn:
        sesiones.crear(ctx.cfg, conn, sesion_id=sid, materia_id="prueba", origen_materia="manual",
                       tipo_clase="teorica", inicio="2026-10-26T14:00:00-03:00", dispositivo="reloj")
        conn.execute("UPDATE sesiones SET estado = ?, duracion_ms = 120000 WHERE id = ?", (estado, sid))
    carpeta = ctx.sesion_dir(sid)
    segmentos = segmentos if segmentos is not None else [
        {"inicio_ms": 1000, "fin_ms": 4000, "hablante": "profe", "texto": "eh bueno hoy vemos inflación"},
        {"inicio_ms": 5000, "fin_ms": 8000, "hablante": "desconocido", "texto": "lo podemos corregir aquí"},
        {"inicio_ms": 9000, "fin_ms": 11000, "hablante": "alumno", "texto": "¿entra en el parcial?"},
        {"inicio_ms": 12000, "fin_ms": 15000, "hablante": "profe", "texto": "eh"},
    ]
    with ctx.base.conexion() as conn:
        conn.execute("INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta, estado) VALUES (?, 1, 0, 60000, ?, ?)",
                     (sid, "bloques/bloque_01.mp3", "transcripto"))
        conn.executemany("INSERT INTO segmentos (sesion_id, bloque, inicio_ms, fin_ms, hablante, texto) "
                         "VALUES (?, 1, ?, ?, ?, ?)",
                         [(sid, s["inicio_ms"], s["fin_ms"], s["hablante"], s["texto"]) for s in segmentos])
    (carpeta / "transcript").mkdir(parents=True, exist_ok=True)
    (carpeta / "transcript" / "bloque_01.json").write_text(json.dumps(
        {"n": 1, "inicio_ms": 0, "fin_ms": 60000, "estado": "transcripto", "segmentos": segmentos}), encoding="utf-8")
    sesiones.registrar_evento(ctx.cfg, ctx.base, sid, evento_id="11111111-1111-4111-8111-111111111111",
                              tipo="marca", offset_ms=6000, marca="importante")
    ldt.ejecutar(ctx, Job(id=1, tipo="linea_de_tiempo", sesion_id=sid))
    return carpeta


def celu(ctx, sid: str, segmentos: list[dict], desfase: int = 0) -> None:
    """Una segunda pista ya transcripta (bloque 101) y linea_de_tiempo de nuevo."""
    carpeta = ctx.sesion_dir(sid)
    with ctx.base.conexion() as conn:
        sesiones._asegurar_asset_celu(ctx.cfg, conn, sid)
        sesiones.guardar_datos_celu(conn, sid, {"cerrada": True, "desfase_ms": desfase, "duracion_ms": 120000})
        conn.execute("INSERT INTO bloques (sesion_id, n, inicio_ms, fin_ms, ruta, estado, pista) "
                     "VALUES (?, 101, 0, 120000, 'bloques_celu/bloque_101.mp3', 'transcripto', 'celu')", (sid,))
    (carpeta / "transcript" / "bloque_101.json").write_text(json.dumps(
        {"n": 101, "inicio_ms": 0, "fin_ms": 120000, "estado": "transcripto", "segmentos": segmentos}),
        encoding="utf-8")
    ldt.ejecutar(ctx, Job(id=1, tipo="linea_de_tiempo", sesion_id=sid))


def correr(ctx, sid: str = SID):
    return etapa.ejecutar(ctx, Job(id=7, tipo="revisar_transcript", sesion_id=sid))


def sesion(ctx, sid: str = SID):
    with ctx.base.conexion() as conn:
        return conn.execute("SELECT * FROM sesiones WHERE id = ?", (sid,)).fetchone()


# --- alinear y tramos (sin IA) ---------------------------------------------------------------------

def s(inicio: int, texto: str, hablante: str = "profe") -> dict[str, Any]:
    return {"inicio_ms": inicio, "fin_ms": inicio + 3000, "hablante": hablante, "texto": texto}


def test_alinear_empareja_lineas_y_separa_lo_que_solo_esta_en_el_celu():
    reloj = [s(0, "bueno esto de grabar está funcionando"), s(4000, "me dijo todo lo que había dicho malo"),
             s(8000, "y esto lo corregimos aquí"), s(30000, "lo que el celu no oyó para nada")]
    celu = [s(100, "bueno esto de grabar está funcionando"), s(4100, "me dijo todo lo que había dicho mal"),
            s(6500, "profe una pregunta sobre el parcial", "alumno"), s(8100, "y esto lo corregimos a tiempo")]
    lineas, solo = revisor.alinear(reloj, celu)
    assert lineas[0]["celu"] == "bueno esto de grabar está funcionando" and lineas[0]["similitud"] == 1.0
    assert lineas[1]["celu"] == "me dijo todo lo que había dicho mal" and lineas[1]["similitud"] < 1
    assert lineas[2]["celu"] == "y esto lo corregimos a tiempo"
    assert lineas[3]["celu"] is None   # sin par en el celu
    assert [x["texto"] for x in solo] == ["profe una pregunta sobre el parcial"]
    assert revisor.difieren("eh bueno, bueno esto", "Bueno esto.") is False
    assert revisor.difieren("puede usar", "no puede usar") is True


def test_tramos_parejos():
    assert revisor.tramos(0, 70) == []
    assert revisor.tramos(10, 70) == [(0, 10)]
    cortes = revisor.tramos(150, 70)
    assert cortes == [(0, 75), (75, 150)]
    assert sum(b - a for a, b in revisor.tramos(899, 70)) == 899


def test_rehacer_timeline_reemplaza_solo_el_habla():
    crudos = [s(1000, "uno"), s(5000, "dos"), s(9000, "tres")]
    texto = ("# Clase: X — 2026-10-26 — teórica\n[00:00:01] PROFE: uno\n[00:00:05] PROFE: dos\n"
             "[00:00:06] MARCA importante\n[00:00:07] SILENCIO hasta 00:00:08 (no se transcribió)\n"
             "[00:00:09] PROFE: tres\n# Material adjunto\nADJUNTO adjuntos/a.pdf (PDF, 1 página): \"a.pdf\"\n")
    crudos = [{**c, "hablante": "PROFE"} for c in crudos]
    nuevos = [{**s(1000, "Uno."), "hablante": "Ana"}, {**s(9000, "Tres."), "hablante": "Alumno"}]
    salida = ldt.rehacer_timeline(texto, crudos, nuevos).splitlines()
    assert salida == ["# Clase: X — 2026-10-26 — teórica", "[00:00:01] Ana: Uno.", "[00:00:06] MARCA importante",
                      "[00:00:07] SILENCIO hasta 00:00:08 (no se transcribió)", "[00:00:09] Alumno: Tres.",
                      "# Material adjunto", "ADJUNTO adjuntos/a.pdf (PDF, 1 página): \"a.pdf\""]


# --- la etapa ----------------------------------------------------------------------------------------

def test_apagada_es_lo_de_siempre(ctx):
    carpeta = armar(ctx)
    falso = RevisorFalso()
    con_revisor(ctx, falso)
    antes = {n: (carpeta / n).read_bytes() for n in ("transcript.json", "transcript.md", "timeline.md")}
    r = correr(ctx)
    assert r.siguientes == [("apunte", {})] and "apagada" in r.log
    assert falso.pedidos == []
    assert {n: (carpeta / n).read_bytes() for n in antes} == antes
    assert not (carpeta / "revision").exists()


def test_limpieza_con_un_solo_audio_sin_dudas(ctx):
    prender(ctx)
    carpeta = armar(ctx)
    falso = RevisorFalso()
    con_revisor(ctx, falso)
    r = correr(ctx)
    assert r.siguientes == [("apunte", {})]
    assert r.motor == "claude:claude-falso" and r.facturacion == "api" and r.costo == pytest.approx(0.01)
    assert [p for p, _ in falso.pedidos] == ["limpiar"]   # sin celu no se concilia; sin dudas no se resuelve
    tr = leer_json(carpeta / "transcript.json")
    assert [(x["hablante"], x["texto"]) for x in tr] == [
        ("Ana Pérez", "Bueno hoy vemos inflación."), ("Ana Pérez", "Lo podemos corregir aquí."),
        ("Alumno", "¿entra en el parcial?."), ]   # la línea que era sólo "eh" se va
    md = (carpeta / "transcript.md").read_text(encoding="utf-8").splitlines()
    assert md[0].startswith("# Transcript: Materia de Prueba") and md[1] == "[00:00:01] Ana Pérez: Bueno hoy vemos inflación."
    tl = (carpeta / "timeline.md").read_text(encoding="utf-8")
    assert "[00:00:05] Ana Pérez: Lo podemos corregir aquí." in tl and "[00:00:06] MARCA importante" in tl
    assert "PROFE:" not in tl and "DESCONOCIDO:" not in tl
    # Los crudos quedan.
    assert "DESCONOCIDO: lo podemos corregir aquí" in (carpeta / "transcript.crudo.md").read_text(encoding="utf-8")
    assert "DESCONOCIDO: lo podemos corregir aquí" in (carpeta / "timeline.crudo.md").read_text(encoding="utf-8")
    # La tabla `segmentos` (búsqueda y MCP) es la revisada.
    with ctx.base.conexion() as conn:
        filas = conn.execute("SELECT hablante, texto FROM segmentos WHERE sesion_id = ? ORDER BY inicio_ms",
                             (SID,)).fetchall()
    assert [tuple(f) for f in filas][0] == ("Ana Pérez", "Bueno hoy vemos inflación.")
    rev = revision.cargar(ctx.cfg, SID)
    assert rev["estado"] == "aplicada" and rev["modo"] == "limpieza" and rev["dudas"] == []
    registro = leer_json(carpeta / "revision" / "limpiar.json")
    assert {x["linea"] for x in registro["cambios"]} >= {"L0001", "L0002", "L0004"}
    assert (carpeta / "revision" / "cambios.md").is_file()


def test_sin_profesor_dice_profe(ctx):
    prender(ctx, profesor=None)
    carpeta = armar(ctx)
    con_revisor(ctx, RevisorFalso())
    correr(ctx)
    assert leer_json(carpeta / "transcript.json")[0]["hablante"] == "Profe"


def test_cache_no_vuelve_a_pagar(ctx):
    prender(ctx)
    armar(ctx)
    falso = RevisorFalso()
    con_revisor(ctx, falso)
    correr(ctx)
    correr(ctx)
    assert len(falso.pedidos) == 1


def test_respuesta_cortada_deja_el_tramo_sin_revisar(ctx):
    prender(ctx)
    carpeta = armar(ctx)
    con_revisor(ctx, RevisorFalso(cortar_limpiar=True))
    r = correr(ctx)
    assert r.siguientes == [("apunte", {})]
    rev = revision.cargar(ctx.cfg, SID)
    assert any("sin revisar" in a for a in rev["avisos"]) and rev["costo"] == pytest.approx(0.02)
    assert leer_json(carpeta / "transcript.json")[1]["texto"] == "lo podemos corregir aquí"


def test_dudas_esperan_al_usuario_y_listo_encola_el_apunte(ctx, client: TestClient):
    prender(ctx)
    carpeta = armar(ctx)
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."}))
    with pytest.raises(EsperandoUsuario) as e:
        correr(ctx)
    assert e.value.espera == "revision" and e.value.costo == pytest.approx(0.02)
    # Lo que hace el worker con la excepción:
    with ctx.base.conexion() as conn:
        from classmer.pipeline import actualizar_sesion
        actualizar_sesion(conn, SID, ctx.cfg.zona_horaria, estado="esperando_usuario", error=str(e.value),
                          espera=e.value.espera)
    cuerpo = client.get(f"/api/sesiones/{SID}").json()
    assert (cuerpo["estado"], cuerpo["espera"], cuerpo["dudas"]) == ("esperando_usuario", "revision", 1)
    # Mientras espera, el transcript ya es el revisado con la propuesta (lo que se oyó).
    assert leer_json(carpeta / "transcript.json")[1]["texto"] == "Lo podemos corregir aquí."

    # Elegir la materia no se saltea la revisión.
    r = client.post(f"/api/sesiones/{SID}/materia", json={"materia_id": "otra"})
    assert r.status_code == 200 and r.json()["estado"] == "esperando_usuario" and r.json()["espera"] == "revision"
    with ctx.base.conexion() as conn:
        assert cola.jobs_de_sesion(conn, SID) == []
        conn.execute("UPDATE sesiones SET materia_id = 'prueba' WHERE id = ?", (SID,))

    rev = revision.cargar(ctx.cfg, SID)
    d = rev["dudas"][0]
    assert d["paso"] == "limpiar" and [o["fuente"] for o in d["opciones"]] == ["audio", "propuesta"]
    revision.responder(ctx.cfg, SID, {d["id"]: {"opcion": 1}})
    assert revision.terminar(ctx, SID) == 0
    assert sesion(ctx)["estado"] == "en_cola" and sesion(ctx)["espera"] is None
    with ctx.base.conexion() as conn:
        assert [(j.tipo, j.estado) for j in cola.jobs_de_sesion(conn, SID)] == [("apunte", "pendiente")]
    assert leer_json(carpeta / "transcript.json")[1]["texto"] == "Lo podemos corregir a tiempo."
    assert revision.cargar(ctx.cfg, SID)["aplicada"]["como"] == "listo"
    assert client.get(f"/api/sesiones/{SID}").json()["dudas"] == 0
    with pytest.raises(revision.NoSePuede):
        revision.terminar(ctx, SID)


def test_generar_sin_revisar_usa_la_propuesta(ctx):
    prender(ctx)
    carpeta = armar(ctx)
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."}))
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE sesiones SET estado = 'esperando_usuario', espera = 'revision' WHERE id = ?", (SID,))
    assert revision.terminar(ctx, SID, sin_revisar=True) == 1
    assert leer_json(carpeta / "transcript.json")[1]["texto"] == "Lo podemos corregir aquí."


def test_respuesta_editada_y_herencia_al_reprocesar(ctx):
    prender(ctx)
    carpeta = armar(ctx)
    falso = RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."})
    con_revisor(ctx, falso)
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    pagados = len(falso.pedidos)
    assert [p for p, _ in falso.pedidos] == ["limpiar", "resolver"]
    d = revision.cargar(ctx.cfg, SID)["dudas"][0]
    assert revision.responder(ctx.cfg, SID, {d["id"]: {"texto": "  Lo podemos   corregir ya. "}}) == 1
    rev = revision.cargar(ctx.cfg, SID)
    assert rev["dudas"][0]["respuesta"]["fuente"] == "usuario" and rev["dudas"][0]["estado"] == "respondida"
    # Reprocesar desde la revisión: misma duda, misma respuesta (y sin volver a pagar).
    r = correr(ctx)
    assert r.siguientes == [("apunte", {})] and len(falso.pedidos) == pagados
    assert leer_json(carpeta / "transcript.json")[1]["texto"] == "Lo podemos corregir ya."
    # Una respuesta vacía la vuelve a dejar pendiente.
    revision.responder(ctx.cfg, SID, {d["id"]: {"texto": "  "}})
    assert revision.pendientes(revision.cargar(ctx.cfg, SID))


def test_apagar_despues_vuelve_al_crudo(ctx):
    prender(ctx)
    carpeta = armar(ctx)
    crudo = (carpeta / "transcript.crudo.json").read_bytes()
    con_revisor(ctx, RevisorFalso())
    correr(ctx)
    assert (carpeta / "transcript.json").read_bytes() != crudo
    ctx.cfg.materia("prueba").revision = "apagada"
    r = correr(ctx)
    assert "se deshizo" in r.log
    assert (carpeta / "transcript.json").read_bytes() == crudo
    assert (carpeta / "timeline.md").read_bytes() == (carpeta / "timeline.crudo.md").read_bytes()
    with ctx.base.conexion() as conn:
        assert conn.execute("SELECT hablante FROM segmentos WHERE sesion_id = ? ORDER BY inicio_ms",
                            (SID,)).fetchone()[0] == "PROFE"
    assert revision.cargar(ctx.cfg, SID)["estado"] == "apagada"


def test_conciliacion_con_el_celu(ctx):
    prender(ctx)
    carpeta = armar(ctx)
    celu(ctx, SID, [
        {"inicio_ms": 900, "fin_ms": 3900, "hablante": "profe", "texto": "bueno hoy vemos inflación"},
        {"inicio_ms": 4900, "fin_ms": 7900, "hablante": "profe", "texto": "lo podemos corregir a tiempo"},
        {"inicio_ms": 8000, "fin_ms": 8800, "hablante": "alumno", "texto": "perdón profe una pregunta"},
        {"inicio_ms": 8900, "fin_ms": 10900, "hablante": "alumno", "texto": "¿entra en el parcial?"},
        # Después de que se cortó el principal (120 s): respaldo.
        {"inicio_ms": 121000, "fin_ms": 124000, "hablante": "profe", "texto": "y con esto terminamos"},
    ], desfase=100)
    tc = leer_json(carpeta / "transcript.celu.json")
    assert tc[0]["inicio_ms"] == 1000 and tc[0]["origen"] == "celu"   # en el tiempo del principal
    crudo = leer_json(carpeta / "transcript.crudo.json")
    assert crudo[-1] == {"bloque": 101, "inicio_ms": 121100, "fin_ms": 124100, "hablante": "PROFE",
                         "texto": "y con esto terminamos", "origen": "celu"}
    falso = RevisorFalso(dudas_conciliar={"L0002"})
    con_revisor(ctx, falso)
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    pasos = [p for p, _ in falso.pedidos]
    assert pasos == ["conciliar", "limpiar", "resolver"]
    prompt_conciliar = falso.pedidos[0][1]
    assert "  reloj: lo podemos corregir aquí\n  celu:  lo podemos corregir a tiempo" in prompt_conciliar
    assert "sólo en el celu, ALUMNO: perdón profe una pregunta" in prompt_conciliar
    rev = revision.cargar(ctx.cfg, SID)
    assert rev["modo"] == "conciliacion" and rev["desfase_ms"] == 100
    d = rev["dudas"][0]
    assert d["paso"] == "conciliar"
    # Las versiones, limpias por el paso de limpiar (el falso las pasa a mayúsculas), y la propuesta.
    assert [o["fuente"] for o in d["opciones"]] == ["reloj", "celu", "propuesta"]
    assert d["opciones"][1]["texto"] == "LO PODEMOS CORREGIR A TIEMPO" and d["propuesta"] == 2
    tr = leer_json(carpeta / "transcript.json")
    textos = [(x["hablante"], x["texto"]) for x in tr]
    assert ("Alumno", "Perdón profe una pregunta.") in textos   # la del celu entró
    assert tr[-1]["origen"] == "celu" and tr[-1]["texto"] == "Y con esto terminamos."
    registro = leer_json(carpeta / "revision" / "conciliar.json")
    assert registro["desfase_ms"] == 100 and registro["cambios"][0]["eleccion"] == "celu"
    assert registro["solo_celu"][0]["incluir"] is True


def tono(ruta: Path, segundos: int) -> None:
    from classmer import audio

    r = audio.ejecutar(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=f=440:d={segundos}:sample_rate=48000", "-ac", "1", "-c:a", "libopus", str(ruta)],
                       timeout=60)
    assert r.returncode == 0, r.stderr


@requiere("ffmpeg")
def test_volver_a_escuchar_y_resolver(ctx):
    prender(ctx, escuchar=10)
    carpeta = armar(ctx)
    tono(carpeta / "audio.ogg", 20)
    falso = RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."},
                         resolver={"D01": "Lo podemos corregir a tiempo."})
    asr = TranscriptorFalso()
    con_revisor(ctx, falso, asr)
    r = correr(ctx)
    assert r.siguientes == [("apunte", {})]
    rev = revision.cargar(ctx.cfg, SID)
    d = rev["dudas"][0]
    assert d["estado"] == "resuelta" and d["escuchado"] == {"reloj": "oído en escuchar-reloj-1"}
    assert "escuchado en el reloj: oído en escuchar-reloj-1" in falso.pedidos[-1][1]
    assert leer_json(carpeta / "transcript.json")[1]["texto"] == "Lo podemos corregir a tiempo."
    assert asr.llamadas == ["escuchar-reloj-1.mp3"] and rev["escucha"] == {"pedidos": 1, "grupos": 1, "segundos": 9}


@requiere("ffmpeg")
def test_sin_cuota_de_gemini_no_se_espera(ctx):
    from classmer.pipeline import CuotaAgotada

    class SinCuota(TranscriptorFalso):
        def transcribir(self, audio, contexto):
            self.llamadas.append(audio.name)
            raise CuotaAgotada("Gemini 429 RESOURCE_EXHAUSTED", espera_s=3600)

    prender(ctx, escuchar=10)
    carpeta = armar(ctx)
    tono(carpeta / "audio.ogg", 20)
    asr = SinCuota()
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0001": "Bueno, hoy vemos deflación.",
                                                 "L0002": "Lo podemos corregir a tiempo."}), asr)
    t0 = time.time()
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    assert time.time() - t0 < 30 and asr.llamadas == ["escuchar-reloj-1.mp3"]   # un solo pedido, sin esperar
    rev = revision.cargar(ctx.cfg, SID)
    assert [d["escuchado"] for d in rev["dudas"]] == [{}, {}]
    assert any(a.startswith("Gemini sin cuota") for a in rev["avisos"])


@requiere("ffmpeg")
def test_el_tope_por_minuto_se_espera(ctx, monkeypatch):
    from classmer.pipeline import CuotaAgotada

    class PorMinuto(TranscriptorFalso):
        rechazos = 0

        def transcribir(self, audio, contexto):
            if self.rechazos < 2:
                self.rechazos += 1
                raise CuotaAgotada("Gemini 429 [cuota: …PerMinute]", espera_s=9)
            return super().transcribir(audio, contexto)

    esperas: list[float] = []
    monkeypatch.setattr(etapa.time, "sleep", esperas.append)
    prender(ctx, escuchar=10)
    carpeta = armar(ctx)
    tono(carpeta / "audio.ogg", 20)
    asr = PorMinuto()
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."}), asr)
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    assert esperas == [9, 9] and asr.rechazos == 2 and asr.llamadas == ["escuchar-reloj-1.mp3"]
    assert revision.cargar(ctx.cfg, SID)["dudas"][0]["escuchado"] == {"reloj": "oído en escuchar-reloj-1"}
    # Si sigue sin cuota después de los reintentos, ese grupo queda sin escuchar (y se avisa).
    asr.llamadas.clear()
    esperas.clear()

    class Siempre(TranscriptorFalso):
        def transcribir(self, audio, contexto):
            self.llamadas.append(audio.name)
            raise CuotaAgotada("Gemini 429", espera_s=9)

    siempre = Siempre()
    ctx.cfg.revisar_transcript.modelo = "otro"   # que no salga de la caché de la corrida anterior
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."}), siempre)
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    assert len(siempre.llamadas) == etapa.ESCUCHA_REINTENTOS_MINUTO + 1 and len(esperas) == etapa.ESCUCHA_REINTENTOS_MINUTO
    assert any("sin cuota por minuto" in a for a in revision.cargar(ctx.cfg, SID)["avisos"])


def test_escuchas_agrupadas():
    tramos = [("d", 0, 30_000)] * 15
    lotes = revisor.lotes_escucha(tramos)
    assert [len(x) for x in lotes] == [9, 6]   # hasta 5 min de audio (con los silencios) por pedido
    assert [len(x) for x in revisor.lotes_escucha([("d", 0, 5000)] * 30)] == [12, 12, 6]
    seg = [SegmentoRel(0, 3000, "", "uno"), SegmentoRel(4000, 6000, "", "dos"), SegmentoRel(9000, 9800, "", "tres"),
           SegmentoRel(10_500, 12_000, "", "cuatro")]
    # Tramos de 5 s y 4 s separados por 2,5 s: el segundo empieza en 7,5 s.
    assert revisor.repartir_escucha(seg, [0, 7500], [5000, 4000]) == ["uno dos", "tres cuatro"]


# --- el panel ---------------------------------------------------------------------------------------

@pytest.fixture
def panel(client: TestClient) -> TestClient:
    """El mismo server, visto desde la PC (sin token)."""
    return TestClient(client.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50123),
                      follow_redirects=False)


def test_panel_muestra_las_dudas_y_listo(ctx, client: TestClient, panel: TestClient):
    prender(ctx)
    armar(ctx)
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."}))
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE sesiones SET estado = 'esperando_usuario', espera = 'revision' WHERE id = ?", (SID,))
    html = panel.get(f"/panel/clases/{SID}").text
    assert "Revisión del transcript" in html and "El apunte espera tu revisión" in html
    assert 'name="eleccion_D01"' in html and "Lo podemos corregir a tiempo." in html
    # Sin responder no viene nada tildado: "Guardar" sólo guarda lo que el usuario eligió.
    assert not re.search(r'name="eleccion_D01" value="\d+" checked', html)
    assert "Generar sin revisar" in html and 'value="listo"' in html
    for cual in ("clase", "crudo", "cambios"):
        r = panel.get(f"/panel/clases/{SID}/transcript/{cual}")
        assert r.status_code == 200, cual
    assert panel.get(f"/panel/clases/{SID}/transcript/celu").status_code == 404
    assert panel.get(f"/panel/clases/{SID}/transcript/../../config.yaml").status_code == 404
    r = panel.post(f"/panel/clases/{SID}/revision", data={"accion": "guardar", "eleccion_D01": "editar",
                                                          "texto_D01": "Lo corregimos ya."}, follow_redirects=False)
    assert r.status_code == 303 and "Guardado" in r.headers["location"]
    assert revision.cargar(ctx.cfg, SID)["dudas"][0]["respuesta"]["texto"] == "Lo corregimos ya."
    assert 'name="eleccion_D01" value="editar" checked' in panel.get(f"/panel/clases/{SID}").text
    r = panel.post(f"/panel/clases/{SID}/revision", data={"accion": "listo"}, follow_redirects=False)
    assert r.status_code == 303 and "Revisi%C3%B3n+aplicada" in r.headers["location"]
    assert sesion(ctx)["estado"] == "en_cola"
    r = panel.post(f"/panel/clases/{SID}/revision", data={"accion": "listo"}, follow_redirects=False)
    assert "No+se+pudo" in r.headers["location"]


def test_mcp_transcript_con_nombres(ctx, client: TestClient):
    from classmer.mcp import herramientas

    prender(ctx)
    armar(ctx)
    con_revisor(ctx, RevisorFalso())
    correr(ctx)
    entorno = herramientas.Entorno(cfg=ctx.cfg, base=ctx.base)
    datos, texto = herramientas.transcript(entorno, SID)
    assert datos["revisado"] is True
    assert texto.splitlines()[0] == "[00:00:01] Ana Pérez: Bueno hoy vemos inflación."
    assert herramientas.hablante_visible("profe") == "PROFE" and herramientas.hablante_visible(None) == "DESCONOCIDO"


def test_una_resolucion_que_mete_la_linea_vecina_no_sirve():
    d = {"opciones": [{"fuente": "audio", "texto": "la foto de los primos."}],
         "antes": "[01:21:49] Y esto nuevo acá les voy a mostrar…", "despues": "[01:21:55] Le puede salir 2 meses."}
    assert etapa._se_come_vecinas("Y esto es nuevo, acá les voy a mostrar la foto de los primos.", d)
    assert not etapa._se_come_vecinas("La foto de los primos.", d)
    assert not etapa._se_come_vecinas("La foto de los primos, le puede salir.", d)   # menos de 4 palabras


def test_config_de_la_materia_y_del_modelo_desde_el_panel(ctx, client: TestClient, panel: TestClient):
    html = panel.get("/panel/materias/prueba").text
    assert 'name="revision"' in html and 'name="profesor"' in html
    # Las tres opciones, con la de la materia marcada (la de los tests: apagada).
    for valor in ("automatica", "asistida", "apagada"):
        assert f'name="revision" value="{valor}"' in html
    assert re.search(r'value="apagada" id="revision_apagada" checked', html)
    assert not re.search(r'value="(automatica|asistida)" id="revision_\w+" checked', html)
    assert "Tipo de revisión" in html and "Automática" in html and "Asistida" in html and "Apagada" in html
    datos = {"nombre": "Materia de Prueba", "tipos_clase": "teorica, practica", "revision": "asistida",
             "profesor": "  Laura  Inés Gómez ", "activa": "on"}
    r = panel.post("/panel/materias/prueba", data=datos)
    assert r.status_code == 303, r.text
    cfg = client.app.state.cfg
    assert cfg.materia("prueba").revision == "asistida" and cfg.materia("prueba").profesor == "Laura Inés Gómez"
    texto = (cfg.home / "config.yaml").read_text(encoding="utf-8")
    assert "profesor: Laura Inés Gómez" in texto and "revision: asistida" in texto
    assert re.search(r'value="asistida" id="revision_asistida" checked', panel.get("/panel/materias/prueba").text)
    for tipo in ("apagada", "automatica"):
        panel.post("/panel/materias/prueba", data={**datos, "revision": tipo})
        assert cfg.materia("prueba").revision == tipo
    # Sin la sección (otro cliente) no se toca; un tipo que no existe no se guarda.
    panel.post("/panel/materias/prueba", data={"nombre": "Materia de Prueba", "tipos_clase": "teorica", "activa": "on"})
    assert cfg.materia("prueba").revision == "automatica" and cfg.materia("prueba").profesor == "Laura Inés Gómez"
    r = panel.post("/panel/materias/prueba", data={**datos, "revision": "a-veces"})
    assert r.status_code == 422 and "tipo de revisión inválido" in r.text
    assert cfg.materia("prueba").revision == "automatica"
    # Una materia nueva viene con la automática.
    html = panel.get("/panel/materias/nueva").text
    assert re.search(r'value="automatica" id="revision_automatica" checked', html)
    # Modelos: la tarea nueva, de la lista.
    html = panel.get("/panel/modelos").text
    assert 'name="revisar_transcript"' in html and "Revisar el transcript" in html
    r = panel.post("/panel/modelos", data={"revisar_transcript": "claude-opus-5-5", "revisar_transcript_esfuerzo": "high"})
    assert r.status_code == 303 and cfg.revisar_transcript.modelo == "claude-opus-5-5"
    assert cfg.revisar_transcript.esfuerzo == "high"
    r = panel.post("/panel/modelos", data={"revisar_transcript": "gemini-3.5-transcribe"})
    assert "tipo=error" in r.headers["location"] and cfg.revisar_transcript.modelo == "claude-opus-5-5"


# --- tipo de revisión: automática ------------------------------------------------------------------

def test_automatica_con_dudas_no_espera_y_encola_el_apunte(ctx, client: TestClient):
    prender(ctx, tipo="automatica")
    carpeta = armar(ctx)
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."}))
    r = correr(ctx)   # sin EsperandoUsuario
    assert r.siguientes == [("apunte", {})]
    assert r.motor == "claude:claude-falso" and r.facturacion == "api" and r.costo == pytest.approx(0.02)
    assert "1 con la propuesta del modelo" in r.log
    rev = revision.cargar(ctx.cfg, SID)
    d = rev["dudas"][0]
    propuesta = d["opciones"][d["propuesta"]]["texto"]
    assert propuesta == "Lo podemos corregir aquí."
    # El transcript de la clase (archivos y tabla) queda con la propuesta.
    assert leer_json(carpeta / "transcript.json")[1]["texto"] == propuesta
    assert f"Ana Pérez: {propuesta}" in (carpeta / "transcript.md").read_text(encoding="utf-8")
    with ctx.base.conexion() as conn:
        textos = [f[0] for f in conn.execute("SELECT texto FROM segmentos WHERE sesion_id = ?", (SID,))]
    assert propuesta in textos
    assert rev["estado"] == "aplicada"
    assert rev["aplicada"]["como"] == "automatica" and rev["aplicada"]["con_propuesta"] == 1
    assert rev["aplicada"]["momento"]
    cambios = (carpeta / "revision" / "cambios.md").read_text(encoding="utf-8")
    assert "Aplicada automáticamente: 1 duda(s) con la propuesta del modelo." in cambios
    log = (carpeta / "pipeline.log").read_text(encoding="utf-8")
    assert "revisar_transcript: revisión automática (1 duda(s) con la propuesta del modelo)" in log
    # Nunca queda esperando: la API no cuenta dudas.
    assert sesion(ctx)["espera"] is None
    assert client.get(f"/api/sesiones/{SID}").json()["dudas"] == 0
    # "Listo" no tiene nada que hacer.
    with pytest.raises(revision.NoSePuede):
        revision.terminar(ctx, SID)


def test_automatica_respeta_lo_que_el_usuario_ya_respondio(ctx):
    """Al reprocesar, una duda que el usuario respondió en una asistida queda como la respondió."""
    prender(ctx)
    carpeta = armar(ctx)
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."}))
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    d = revision.cargar(ctx.cfg, SID)["dudas"][0]
    revision.responder(ctx.cfg, SID, {d["id"]: {"opcion": 1}})
    prender(ctx, tipo="automatica")
    r = correr(ctx)
    assert r.siguientes == [("apunte", {})]
    assert leer_json(carpeta / "transcript.json")[1]["texto"] == "Lo podemos corregir a tiempo."
    assert revision.cargar(ctx.cfg, SID)["aplicada"]["como"] == "sin_dudas"


def test_panel_automatica_con_respondidas_las_cuenta_aparte(ctx, panel: TestClient):
    prender(ctx)
    armar(ctx)
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0001": "Bueno hoy vemos deflación.",
                                                 "L0002": "Lo podemos corregir a tiempo."}))
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    d1, d2 = revision.cargar(ctx.cfg, SID)["dudas"]
    revision.responder(ctx.cfg, SID, {d1["id"]: {"texto": "Bueno, hoy vemos inflación y deflación."}})
    prender(ctx, tipo="automatica")
    correr(ctx)
    rev = revision.cargar(ctx.cfg, SID)
    assert rev["aplicada"] == {**rev["aplicada"], "como": "automatica", "con_propuesta": 1}
    html = panel.get(f"/panel/clases/{SID}").text
    assert "aplicada automáticamente: 1 duda con la propuesta del modelo" in html
    assert "Dudas aplicadas: 1 con la propuesta del modelo, 1 respondida</summary>" in html


def test_la_propuesta_devuelve_una_linea_que_limpiar_habia_vaciado():
    rev = {"lineas": [{"id": "L0001", "inicio_ms": 0, "texto": "", "eliminada": True, "rol": "profe"},
                      {"id": "L0002", "inicio_ms": 1000, "texto": "", "eliminada": True, "rol": "profe"}],
           "dudas": [{"id": "D01", "linea": "L0001", "estado": "pendiente", "propuesta": 0,
                      "opciones": [{"fuente": "reloj", "texto": "Hola a todos."}, {"fuente": "celu", "texto": "Hola."}]}]}
    assert revision.usar_propuestas(rev) == 1
    assert [s["texto"] for s in revision.segmentos_revisados(rev, None)] == ["Hola a todos."]   # L0002 sigue afuera


def test_automatica_sin_dudas(ctx):
    prender(ctx, tipo="automatica")
    armar(ctx)
    con_revisor(ctx, RevisorFalso())
    r = correr(ctx)
    assert r.siguientes == [("apunte", {})] and r.costo == pytest.approx(0.01)
    rev = revision.cargar(ctx.cfg, SID)
    assert rev["estado"] == "aplicada" and rev["aplicada"]["como"] == "sin_dudas" and rev["aplicada"]["con_propuesta"] == 0


def test_apagada_con_el_tipo(ctx):
    prender(ctx, tipo="apagada")
    falso = RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."})
    con_revisor(ctx, falso)
    carpeta = armar(ctx)
    r = correr(ctx)
    assert r.siguientes == [("apunte", {})] and "apagada" in r.log and falso.pedidos == []
    assert not (carpeta / "revision").exists()


def test_cambiar_el_tipo_no_toca_las_que_esperan(ctx):
    prender(ctx)   # asistida
    carpeta = armar(ctx)
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."}))
    with pytest.raises(EsperandoUsuario):
        correr(ctx)
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE sesiones SET estado = 'esperando_usuario', espera = 'revision' WHERE id = ?", (SID,))
    ctx.cfg.materia("prueba").revision = "automatica"
    # Sigue esperando hasta "Listo", con las mismas dudas.
    assert sesion(ctx)["espera"] == "revision"
    assert revision.dudas_pendientes(ctx.cfg, SID) == 1
    d = revision.cargar(ctx.cfg, SID)["dudas"][0]
    revision.responder(ctx.cfg, SID, {d["id"]: {"opcion": 1}})
    assert revision.terminar(ctx, SID) == 0
    assert revision.cargar(ctx.cfg, SID)["aplicada"]["como"] == "listo"
    assert leer_json(carpeta / "transcript.json")[1]["texto"] == "Lo podemos corregir a tiempo."


def test_panel_de_una_clase_con_revision_automatica(ctx, client: TestClient, panel: TestClient):
    prender(ctx, tipo="automatica")
    armar(ctx)
    con_revisor(ctx, RevisorFalso(dudas_limpiar={"L0002": "Lo podemos corregir a tiempo."}))
    correr(ctx)
    with ctx.base.conexion() as conn:
        conn.execute("UPDATE sesiones SET estado = 'en_cola', etapa = 'apunte' WHERE id = ?", (SID,))
    html = panel.get(f"/panel/clases/{SID}").text
    assert "aplicada automáticamente: 1 duda con la propuesta del modelo" in html
    assert "Dudas aplicadas: 1 con la propuesta del modelo</summary>" in html
    # Las dudas se ven (la elegida y la otra versión), pero no se responden.
    assert "Lo podemos corregir aquí." in html and "«Lo podemos corregir a tiempo.»" in html
    assert 'name="eleccion_D01"' not in html and 'value="listo"' not in html
    assert "El apunte espera tu revisión" not in html


def test_el_tramo_del_celu_termina_con_la_linea():
    # El reloj no tiene "y el nivel de precios subía" (lo dijo después de su línea); el celu sí, en el
    # mismo segmento: no va pegado a la línea sino como línea propia, con su minuto.
    reloj = [{"inicio_ms": 0, "fin_ms": 2000, "hablante": "profe", "texto": "y entonces"},
             {"inicio_ms": 9000, "fin_ms": 11000, "hablante": "profe", "texto": "esto es otra cosa"}]
    celu = [{"inicio_ms": 0, "fin_ms": 8000, "hablante": "profe",
             "texto": "y entonces mientras tanto el nivel de precios subía todo el tiempo"},
            {"inicio_ms": 9000, "fin_ms": 11000, "hablante": "profe", "texto": "esto es otra cosa"}]
    lineas, solo = revisor.alinear(reloj, celu)
    assert lineas[0]["celu"] == "y entonces" and lineas[1]["celu"] == "esto es otra cosa"
    assert [(x["texto"], x["inicio_ms"] > 1000) for x in solo] == [
        ("mientras tanto el nivel de precios subía todo el tiempo", True)]
    # Un segmento que es casi todo de la línea, con una cola después: la cola (pasado el fin de la línea
    # más el margen) va aparte.
    reloj = [{"inicio_ms": 0, "fin_ms": 4000, "hablante": "profe", "texto": "uno dos tres cuatro cinco seis siete ocho"}]
    celu = [{"inicio_ms": 0, "fin_ms": 12000, "hablante": "profe",
             "texto": "uno dos tres cuatro cinco seis siete ocho nueve diez once doce"}]
    lineas, solo = revisor.alinear(reloj, celu)
    assert lineas[0]["celu"] == "uno dos tres cuatro cinco seis siete ocho"
    assert [x["texto"] for x in solo] == ["nueve diez once doce"]
