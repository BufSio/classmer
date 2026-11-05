#!/usr/bin/env python3
"""Autoprueba del servidor falso de Classmer.

Levanta servidor_falso.py en un puerto libre con una carpeta de datos temporal y recorre todo el
flujo de la app con urllib (sólo biblioteca estándar). Imprime PASS/FAIL por paso y sale con
código distinto de 0 si algo falla.

Secciones: flujo principal, numeración de clases, PATCH de materias, recarga en caliente de
materias.json, esperando_usuario y POST /materia, --rechazar-materia / --cualquier-token,
--materias explícito con sesiones de la versión anterior, fallas de red simuladas, adjuntos y
regenerar, --sin-adjuntos (server viejo), clases importadas, Ambos (la pista del celu), la espera de
la revisión del transcript y --sin-pista (server de antes de Ambos).

    python probar_servidor_falso.py              # borra la carpeta temporal si todo pasa
    python probar_servidor_falso.py --conservar  # la deja para mirarla (logs, PDF, fotos)
"""

import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SERVIDOR = Path(__file__).resolve().with_name("servidor_falso.py")
TOKEN = "token-de-prueba"
ETAPAS = ["verificar", "segmentar", "transcribir", "linea_de_tiempo", "apunte", "publicar"]
ETAPAS_AUTO = ["verificar", "segmentar", "transcribir", "linea_de_tiempo", "clasificar"]
ACTIVAS = ["analisis", "macro", "riesgo"]  # las de §9 menos teoria-juegos (inactiva)
CLAVES_MATERIA = {"id", "nombre", "calendario", "tipos_clase", "proximo_numero"}
CLAVES_SESION = {"id", "materia_id", "estado", "etapa", "progreso", "bytes_recibidos", "pdf", "error",
                 "inicio", "tipo_clase", "duracion_ms", "numero_clase", "titulo", "eventos",
                 "adjuntos", "adjuntos_sin_usar", "importada"}
CLAVES_ADJUNTO = {"id", "nombre", "nombre_original", "tipo", "bytes", "offset_ms", "creado", "estado"}
# Puertos que usan otros servidores en la PC (el server real, otro falso): nunca se tocan.
PUERTOS_OCUPADOS = {8000, 8765, 8777, 8778}
SIN_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
resultados: list[tuple[str, bool]] = []


def verificar(nombre: str, ok: bool, detalle="") -> bool:
    resultados.append((nombre, bool(ok)))
    print(f"PASS  {nombre}" if ok else f"FAIL  {nombre}  -> {detalle}", flush=True)
    return bool(ok)


def puerto_libre() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            if (puerto := s.getsockname()[1]) not in PUERTOS_OCUPADOS:
                return puerto


class Respuesta:
    def __init__(self, status, headers, datos: bytes):
        self.status, self.headers, self.datos = status, headers, datos

    @property
    def json(self):
        try:
            return json.loads(self.datos)
        except ValueError:
            return None

    def __repr__(self):
        return f"<{self.status} {self.datos[:200]!r}>"


class Servidor:
    """Un servidor_falso.py corriendo en un subproceso."""

    def __init__(self, datos: Path, *extra: str):
        self.datos, self.extra, self.puerto, self.proc = datos, extra, puerto_libre(), None

    def arrancar(self):
        self.salida = open(self.datos.parent / f"{self.datos.name}.salida.txt", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVIDOR), "--puerto", str(self.puerto), "--token", TOKEN,
             "--datos", str(self.datos), *self.extra],
            stdout=self.salida, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
        limite = time.monotonic() + 15
        while time.monotonic() < limite:
            if self.proc.poll() is not None:
                raise RuntimeError(f"el servidor murió al arrancar (ver {self.salida.name})")
            try:
                if self.pedir("GET", "/api/health", timeout=1).status == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError("el servidor no respondió /api/health a tiempo")

    def parar(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()  # abrupto a propósito: es como si se cortara la luz
            self.proc.wait(10)
        if self.proc:
            self.salida.close()

    def pedir(self, metodo, ruta, cuerpo=None, json_=None, tipo=None, token=TOKEN, timeout=10):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if json_ is not None:
            cuerpo, tipo = json.dumps(json_).encode(), "application/json"
        if tipo:
            headers["Content-Type"] = tipo
        pedido = urllib.request.Request(f"http://127.0.0.1:{self.puerto}{ruta}", data=cuerpo,
                                        method=metodo, headers=headers)
        try:
            with SIN_PROXY.open(pedido, timeout=timeout) as r:
                return Respuesta(r.status, r.headers, r.read())
        except urllib.error.HTTPError as e:
            return Respuesta(e.code, e.headers, e.read())

    def audio(self, sid, offset, datos):
        return self.pedir("PUT", f"/api/sesiones/{sid}/audio?offset={offset}", datos,
                          tipo="application/octet-stream")

    def bytes_recibidos(self, sid):
        return self.pedir("GET", f"/api/sesiones/{sid}/audio").json["bytes_recibidos"]


def multipart(campos: dict, archivo: tuple[str, bytes] | None = None) -> tuple[bytes, str]:
    limite = "----classmer" + uuid.uuid4().hex
    cuerpo = b""
    for clave, valor in campos.items():
        cuerpo += (f'--{limite}\r\nContent-Disposition: form-data; name="{clave}"\r\n\r\n'
                   f"{valor}\r\n").encode()
    if archivo:
        cuerpo += (f'--{limite}\r\nContent-Disposition: form-data; name="archivo"; '
                   f'filename="{archivo[0]}"\r\nContent-Type: image/jpeg\r\n\r\n').encode()
        cuerpo += archivo[1] + b"\r\n"
    return cuerpo + f"--{limite}--\r\n".encode(), f"multipart/form-data; boundary={limite}"


def evento(servidor, sid, campos, archivo=None):
    cuerpo, tipo = multipart(campos, archivo)
    return servidor.pedir("POST", f"/api/sesiones/{sid}/eventos", cuerpo, tipo=tipo)


def nueva_sesion(**cambios) -> dict:
    return {"id": str(uuid.uuid4()), "materia_id": "analisis", "origen_materia": "calendario",
            "tipo_clase": "teorica", "inicio": "2026-10-15T14:00:05-03:00",
            "dispositivo": "autoprueba", **cambios}


def sesion_auto(**cambios) -> dict:
    return nueva_sesion(materia_id=None, origen_materia="auto", **cambios)


def crear(servidor, sesion: dict) -> dict | None:
    """Crea la sesión y devuelve su GET (None si no se pudo crear)."""
    if servidor.pedir("POST", "/api/sesiones", json_=sesion).status not in (200, 201):
        return None
    return servidor.pedir("GET", f"/api/sesiones/{sesion['id']}").json


def proximos(servidor) -> dict:
    """{id de materia activa: proximo_numero}, en el orden en que llegan."""
    return {m["id"]: m["proximo_numero"] for m in servidor.pedir("GET", "/api/materias").json}


def subir_y_finalizar(servidor, sid, audio: bytes):
    servidor.audio(sid, 0, audio)
    return servidor.pedir("POST", f"/api/sesiones/{sid}/finalizar", json_={
        "sha256": hashlib.sha256(audio).hexdigest(), "bytes_totales": len(audio),
        "duracion_ms": 60_000, "eventos_totales": 0})


def seguir(servidor, sid, finales: set, limite=30.0, pausa=0.15, al_ver=None):
    """Consulta la sesión hasta que llegue a un estado de `finales` (o se acabe el tiempo).

    Devuelve (estados vistos sin repetir, etapas y progresos mientras procesaba, último objeto).
    `al_ver(objeto)` se llama con cada respuesta."""
    vistos, etapas, progresos, ultimo = [], [], [], None
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        ultimo = servidor.pedir("GET", f"/api/sesiones/{sid}").json
        if not vistos or vistos[-1] != ultimo["estado"]:
            vistos.append(ultimo["estado"])
        if ultimo["estado"] == "procesando":
            etapas.append(ultimo["etapa"])
            progresos.append(ultimo["progreso"])
        if al_ver:
            al_ver(ultimo)
        if ultimo["estado"] in finales:
            break
        time.sleep(pausa)
    return vistos, etapas, progresos, ultimo


def escribir_json(ruta: Path, obj) -> None:
    """Como un editor prolijo: temporal + renombrar (el servidor nunca ve un archivo a medias)."""
    tmp = ruta.with_name(ruta.name + ".autoprueba.tmp")
    tmp.write_bytes(obj if isinstance(obj, bytes) else
                    json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))
    os.replace(tmp, ruta)


def lineas_log(datos: Path, texto: str) -> int:
    return sum(texto in linea for linea in (datos / "servidor.log").read_text("utf-8").splitlines())


def subida_cortada(puerto, sid, declarado, enviado) -> bytes:
    """PUT a mano que manda menos bytes que Content-Length y cierra. Devuelve lo que contestó."""
    with socket.create_connection(("127.0.0.1", puerto), timeout=5) as s:
        s.sendall((f"PUT /api/sesiones/{sid}/audio?offset=0 HTTP/1.1\r\nHost: x\r\n"
                   f"Authorization: Bearer {TOKEN}\r\nContent-Type: application/octet-stream\r\n"
                   f"Content-Length: {declarado}\r\n\r\n").encode() + os.urandom(enviado))
        s.shutdown(socket.SHUT_WR)
        try:
            return s.recv(1000)  # el servidor tiene que cerrar sin contestar
        except OSError:
            return b""


def xref_ok(pdf: bytes) -> bool:
    """Chequea que startxref, la tabla xref y /Length apunten a donde tienen que apuntar."""
    inicio = int(pdf[pdf.rfind(b"startxref") + 9:].split()[0])
    if not pdf[inicio:].startswith(b"xref\n"):
        return False
    lineas = pdf[inicio:].split(b"\n")
    cantidad = int(lineas[1].split()[1])
    for n in range(1, cantidad):
        entrada = lineas[2 + n] + b"\n"
        if len(entrada) != 20 or not pdf[int(entrada[:10]):].startswith(b"%d 0 obj" % n):
            return False
    m = re.search(rb"/Length (\d+) >>\nstream\n", pdf)
    return bool(m) and pdf[m.end() + int(m[1]):].startswith(b"\nendstream")


# ----------------------------------------------------------------------------- flujo principal

def flujo_principal(tmp: Path, servidores: list):
    s = Servidor(tmp / "datos", "--procesar-seg", "3")
    servidores.append(s)
    s.arrancar()

    r = s.pedir("GET", "/api/health", token=None)
    verificar("401 sin token", r.status == 401 and r.json == {"detail": "token inválido"}, r)
    r = s.pedir("GET", "/api/materias", token="otro-token")
    verificar("401 con token incorrecto", r.status == 401, r)
    r = s.pedir("GET", "/api/health")
    verificar("health", r.status == 200 and r.json == {"ok": True, "version": "falso-0.2"}, r)

    r = s.pedir("GET", "/api/materias")
    m = r.json or []
    verificar("materias: las activas de §9 con reglas, tipos y proximo_numero",
              r.status == 200 and [x["id"] for x in m] == ACTIVAS
              and all(set(x) == CLAVES_MATERIA and x["proximo_numero"] == 1 for x in m)
              and any(x["calendario"] is None for x in m)
              and all(x["calendario"] is None or "titulo_regex" in x["calendario"] for x in m)
              and all(x["tipos_clase"] == ["teorica", "practica"] for x in m), r)

    # --- sesión
    sesion = nueva_sesion()
    sid = sesion["id"]
    r = s.pedir("POST", "/api/sesiones", json_=sesion)
    verificar("crear sesión -> 201 grabando", r.status == 201 and r.json == {"id": sid, "estado": "grabando"}, r)
    r = s.pedir("POST", "/api/sesiones", json_=sesion)
    verificar("crear la misma sesión -> 200", r.status == 200 and r.json["estado"] == "grabando", r)
    malos = {"inicio sin zona": {"inicio": "2026-10-15T14:00:05"}, "origen raro": {"origen_materia": "x"},
             "materia desconocida": {"materia_id": "quimica"}, "id no UUID": {"id": "abc"},
             "tipo_clase raro": {"tipo_clase": "parcial"}}
    for nombre, cambio in malos.items():
        r = s.pedir("POST", "/api/sesiones", json_=nueva_sesion(**cambio))
        verificar(f"crear sesión con {nombre} -> 400", r.status == 400 and "detail" in r.json, r)
    r = s.pedir("POST", "/api/sesiones", b"{no es json", tipo="application/json")
    verificar("crear sesión con JSON roto -> 400", r.status == 400, r)
    r = s.pedir("GET", f"/api/sesiones/{uuid.uuid4()}")
    verificar("sesión desconocida -> 404", r.status == 404, r)
    r = s.pedir("GET", f"/api/sesiones/{sid}")
    claves = CLAVES_SESION
    verificar("GET sesión: forma y estado inicial",
              r.status == 200 and set(r.json) == claves and r.json["estado"] == "grabando"
              and r.json["bytes_recibidos"] == 0 and r.json["eventos"] == 0
              and r.json["numero_clase"] == 1 and r.json["titulo"] is None and r.json["error"] is None, r)

    # --- audio
    audio = random.Random(42).randbytes(300_000)
    cortes = [0, 100_000, 200_000, 300_000]
    for i in range(3):
        r = s.audio(sid, cortes[i], audio[cortes[i]:cortes[i + 1]])
        verificar(f"PUT audio parte {i + 1}/3", r.status == 200 and r.json == {"bytes_recibidos": cortes[i + 1]}, r)
    r = s.audio(sid, 1000, b"x" * 500)
    verificar("PUT en offset equivocado -> 409 con el tamaño real",
              r.status == 409 and r.json == {"bytes_recibidos": 300_000}, r)
    r = s.pedir("PUT", f"/api/sesiones/{sid}/audio", b"x", tipo="application/octet-stream")
    verificar("PUT sin offset -> 400", r.status == 400, r)
    verificar("GET audio -> bytes_recibidos", s.bytes_recibidos(sid) == 300_000)
    verificar("audio en disco idéntico", (tmp / "datos/sesiones" / sid / "audio.ogg").read_bytes() == audio)

    sesion2 = nueva_sesion(materia_id=None, origen_materia="auto", tipo_clase="practica",
                           inicio="2026-10-16T09:00:00-03:00")
    sid2 = sesion2["id"]
    verificar("crear sesión Auto (materia null)", s.pedir("POST", "/api/sesiones", json_=sesion2).status == 201)
    verificar("sesión Auto sin numero_clase", s.pedir("GET", f"/api/sesiones/{sid2}").json["numero_clase"] is None)
    resto = subida_cortada(s.puerto, sid2, 50_000, 20_000)
    time.sleep(0.3)
    verificar("cliente corta a mitad del cuerpo: sin respuesta y no se guarda nada",
              resto == b"" and s.bytes_recibidos(sid2) == 0, (resto, s.bytes_recibidos(sid2)))

    # --- eventos
    foto = bytes(range(256)) * 4 + b"\r\n--\r\n\r\n\n\r" + random.Random(7).randbytes(3000) + b"\r"
    creado = "2026-10-15T14:05:00-03:00"
    eventos = [
        ({"tipo": "foto", "offset_ms": 5000}, ("pizarra_01.jpg", foto)),
        ({"tipo": "nota", "offset_ms": 12000, "texto": "preguntar por qué el gradiente es ⊥ (ñandú)"}, None),
        ({"tipo": "marca", "offset_ms": 754000, "marca": "importante"}, None),
        ({"tipo": "marca", "offset_ms": 755000, "marca": "no_entendi"}, None),
        ({"tipo": "pausa", "offset_ms": 800000}, None),
        ({"tipo": "reanudar", "offset_ms": 800000}, None),
    ]
    ids = []
    for campos, archivo in eventos:
        eid = str(uuid.uuid4())
        ids.append(eid)
        r = evento(s, sid, {"id": eid, "creado": creado, **campos}, archivo)
        verificar(f"evento {campos['tipo']} {campos.get('marca', '')}".rstrip() + " -> 201",
                  r.status == 201 and r.json == {"id": eid, "nuevo": True}, r)
    r = evento(s, sid, {"id": ids[0], "creado": creado, **eventos[0][0]}, eventos[0][1])
    verificar("evento foto repetido -> 200 nuevo=false", r.status == 200 and r.json == {"id": ids[0], "nuevo": False}, r)
    guardada = tmp / "datos/sesiones" / sid / "fotos/pizarra_01.jpg"
    verificar("foto guardada byte a byte", guardada.exists() and guardada.read_bytes() == foto)
    nota = json.loads((tmp / "datos/sesiones" / sid / "eventos" / f"{ids[1]}.json").read_text("utf-8"))
    verificar("nota guardada con su texto UTF-8", nota["texto"] == eventos[1][0]["texto"], nota)
    invalidos = {
        "nota sin texto": {"tipo": "nota", "offset_ms": 1},
        "marca desconocida": {"tipo": "marca", "offset_ms": 1, "marca": "genial"},
        "offset negativo": {"tipo": "pausa", "offset_ms": -5},
        "foto sin archivo": {"tipo": "foto", "offset_ms": 1},
        "tipo desconocido": {"tipo": "video", "offset_ms": 1},
        "creado sin zona": {"tipo": "pausa", "offset_ms": 1, "creado": "2026-10-15T14:05:00"},
    }
    for nombre, campos in invalidos.items():
        r = evento(s, sid, {"id": str(uuid.uuid4()), "creado": creado, **campos})
        verificar(f"evento con {nombre} -> 400", r.status == 400, r)
    r = s.pedir("POST", f"/api/sesiones/{sid}/eventos", b'{"tipo": "nota"}', tipo="application/json")
    verificar("evento que no es multipart -> 400", r.status == 400, r)
    verificar("GET sesión cuenta 6 eventos", s.pedir("GET", f"/api/sesiones/{sid}").json["eventos"] == 6)

    # --- finalizar
    fin = {"sha256": hashlib.sha256(audio).hexdigest(), "bytes_totales": 300_000,
           "duracion_ms": 5_400_000, "eventos_totales": 6}
    ruta_fin = f"/api/sesiones/{sid}/finalizar"
    r = s.pedir("POST", ruta_fin, json_={**fin, "eventos_totales": 7})
    verificar("finalizar con eventos de más -> 409 faltan_eventos",
              r.status == 409 and r.json == {"motivo": "faltan_eventos", "eventos_recibidos": 6}, r)
    r = s.pedir("GET", f"/api/sesiones/{sid}/eventos")
    verificar("GET eventos -> los 6 ids", r.status == 200 and len(r.json["ids"]) == 6, r)
    r = s.pedir("POST", ruta_fin, json_={**fin, "bytes_totales": 300_001})
    verificar("finalizar con más bytes que el server -> 409 faltan_bytes",
              r.status == 409 and r.json == {"motivo": "faltan_bytes", "bytes_recibidos": 300_000}, r)
    r = s.pedir("POST", ruta_fin, json_={**fin, "sha256": "0" * 64})
    verificar("finalizar con otro hash -> 409 hash_distinto",
              r.status == 409 and r.json == {"motivo": "hash_distinto", "bytes_recibidos": 0}, r)
    verificar("después de hash_distinto el audio vuelve a 0", s.bytes_recibidos(sid) == 0)
    r = s.pedir("POST", ruta_fin, json_={**fin, "duracion_ms": "mucho"})
    verificar("finalizar con duracion_ms no entera -> 400", r.status == 400, r)
    for a, b in [(0, 150_000), (150_000, 300_000)]:
        r = s.audio(sid, a, audio[a:b])
        verificar(f"re-subida desde {a}", r.status == 200 and r.json["bytes_recibidos"] == b, r)
    r = s.pedir("POST", ruta_fin, json_=fin)
    verificar("finalizar OK -> 200 en_cola", r.status == 200 and r.json == {"estado": "en_cola"}, r)
    r = s.pedir("GET", f"/api/sesiones/{sid}")
    verificar("GET sesión -> en_cola con duracion_ms",
              r.json["estado"] == "en_cola" and r.json["duracion_ms"] == 5_400_000 and r.json["pdf"] is None, r)
    r = s.pedir("POST", ruta_fin, json_=fin)
    verificar("finalizar repetido -> 200", r.status == 200, r)
    r = s.audio(sid, 300_000, b"mas")
    verificar("PUT después de finalizar -> 409", r.status == 409 and r.json == {"bytes_recibidos": 300_000}, r)
    r = evento(s, sid, {"id": ids[2], "creado": creado, **eventos[2][0]})
    verificar("evento repetido después de finalizar -> 200", r.status == 200 and r.json["nuevo"] is False, r)
    r = evento(s, sid, {"id": str(uuid.uuid4()), "creado": creado, "tipo": "pausa", "offset_ms": 1})
    verificar("evento nuevo después de finalizar -> 409", r.status == 409, r)
    r = s.pedir("GET", f"/api/sesiones/{sid}/apunte.pdf")
    verificar("PDF antes de estar lista -> 404", r.status == 404, r)

    # --- pipeline simulado
    vistos, etapas, progresos, ultimo = seguir(s, sid, {"lista"})
    verificar("pipeline en_cola -> procesando -> lista", vistos == ["en_cola", "procesando", "lista"], vistos)
    verificar("etapas en orden y progreso creciente",
              len(set(etapas)) >= 4 and etapas == sorted(etapas, key=ETAPAS.index)
              and progresos == sorted(progresos) and all(0 <= p < 1 for p in progresos), (etapas, progresos))
    falso = importlib.util.module_from_spec(importlib.util.spec_from_file_location("falso", SERVIDOR))
    falso.__spec__.loader.exec_module(falso)
    casi = [falso.tramo(falso.SEG_EN_COLA + 3 - 1e-7, 3, ETAPAS, base, peso, ("lista", None, 1.0))
            for base, peso in [(0.0, 1.0), (0.0, 0.5), (0.5, 0.5)]]
    verificar("en el último instante de procesando el progreso no llega al final del tramo",
              [c[0] for c in casi] == ["procesando"] * 3
              and [c[2] for c in casi] == [0.999, 0.499, 0.999], casi)
    verificar("lista con pdf y progreso 1",
              ultimo["pdf"] == f"/api/sesiones/{sid}/apunte.pdf" and ultimo["progreso"] == 1.0, ultimo)
    verificar("lista con titulo y numero_clase",
              ultimo["titulo"] == "Apunte de prueba · Clase 1" and ultimo["numero_clase"] == 1
              and ultimo["error"] is None, ultimo)

    r = s.pedir("GET", ultimo["pdf"])
    pdf = r.datos
    verificar("descargar PDF", r.status == 200 and r.headers["Content-Type"] == "application/pdf", r)
    verificar("PDF empieza con %PDF y termina con %%EOF",
              pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF"), pdf[:20] + pdf[-20:])
    verificar("PDF con xref y /Length correctos", xref_ok(pdf))
    verificar("PDF con el número de clase", b"Clase 1" in pdf)
    (tmp / "apunte.pdf").write_bytes(pdf)
    r = s.pedir("GET", f"/api/sesiones/{sid2}/apunte.pdf")
    verificar("PDF de sesión sin finalizar -> 404", r.status == 404, r)

    # --- listado y rutas raras
    r = s.pedir("GET", "/api/sesiones?limite=10")
    verificar("listar sesiones, la más nueva primero",
              r.status == 200 and [x["id"] for x in r.json] == [sid2, sid] and set(r.json[1]) == claves, r)
    r = s.pedir("GET", "/api/sesiones?limite=1")
    verificar("listar con limite=1", r.status == 200 and len(r.json) == 1, r)
    r = s.pedir("GET", "/api/sesiones?limite=muchas")
    verificar("listar con limite inválido -> 400", r.status == 400, r)
    r = s.pedir("GET", "/api/no-existe")
    verificar("ruta desconocida -> 404 JSON", r.status == 404 and "detail" in r.json, r)
    r = s.pedir("DELETE", f"/api/sesiones/{sid}")
    verificar("método no permitido -> 405", r.status == 405, r)

    # --- reinicio: el estado tiene que sobrevivir
    s.parar()
    s.arrancar()
    r = s.pedir("GET", f"/api/sesiones/{sid}")
    verificar("tras reiniciar: sesión lista con sus datos",
              r.status == 200 and r.json["estado"] == "lista" and r.json["bytes_recibidos"] == 300_000
              and r.json["eventos"] == 6 and r.json["duracion_ms"] == 5_400_000
              and r.json["titulo"] == "Apunte de prueba · Clase 1", r)
    verificar("tras reiniciar: proximo_numero cuenta la clase", proximos(s)["analisis"] == 2)
    r = s.pedir("GET", f"/api/sesiones/{sid2}")
    verificar("tras reiniciar: la otra sigue grabando", r.status == 200 and r.json["estado"] == "grabando", r)
    r = evento(s, sid, {"id": ids[3], "creado": creado, **eventos[3][0]})
    verificar("tras reiniciar: evento repetido -> 200", r.status == 200 and r.json["nuevo"] is False, r)
    r = s.pedir("POST", "/api/sesiones", json_=sesion)
    verificar("tras reiniciar: crear la misma sesión -> 200 lista", r.status == 200 and r.json["estado"] == "lista", r)
    r = s.audio(sid2, 0, audio[:1000])
    verificar("tras reiniciar: se puede seguir subiendo", r.status == 200 and r.json["bytes_recibidos"] == 1000, r)
    verificar("tras reiniciar: el PDF sigue", s.pedir("GET", ultimo["pdf"]).datos == pdf)
    log = (tmp / "datos/servidor.log").read_text("utf-8")
    verificar("servidor.log registra los pedidos",
              "audio +100000 B -> 200000" in log and "finalizar OK sha256=" in log and "409 offset=1000" in log)


# ----------------------------------------------------------------------------- numeración y materias

def numeracion_y_materias(tmp: Path, servidores: list):
    s = Servidor(tmp / "datos-numeracion", "--procesar-seg", "2")
    servidores.append(s)
    s.arrancar()
    ruta = s.datos / "materias.json"

    # --- materias.json por defecto
    original = json.loads(ruta.read_text("utf-8")) if ruta.exists() else []
    verificar("materias.json se crea con las 4 de §9 (teoria-juegos inactiva)",
              [m["id"] for m in original] == ["analisis", "teoria-juegos", "macro", "riesgo"]
              and [m.get("activa", True) for m in original] == [True, False, True, True], original)
    verificar("proximo_numero arranca en 1", proximos(s) == {mid: 1 for mid in ACTIVAS})

    # --- numero_clase al crear sesiones
    v = crear(s, nueva_sesion(materia_id="macro"))
    verificar("sesión sin numero_clase toma el próximo (1)",
              v and v["numero_clase"] == 1 and v["titulo"] is None and set(v) == CLAVES_SESION, v)
    verificar("después proximo_numero de macro = 2", proximos(s)["macro"] == 2)
    con_ocho = nueva_sesion(materia_id="macro", numero_clase=8)
    v = crear(s, con_ocho)
    verificar("sesión con numero_clase=8 lo guarda", v and v["numero_clase"] == 8, v)
    verificar("proximo_numero cuenta clases, no usa el override (3)", proximos(s)["macro"] == 3)
    v = crear(s, nueva_sesion(materia_id="macro", numero_clase=None))
    verificar("numero_clase null toma el próximo (3)", v and v["numero_clase"] == 3, v)
    for nombre, valor in {"texto": "x", "cero": 0, "true": True, "negativo": -1, "decimal": 1.5}.items():
        r = s.pedir("POST", "/api/sesiones", json_=nueva_sesion(materia_id="macro", numero_clase=valor))
        verificar(f"numero_clase {nombre} -> 400",
                  r.status == 400 and "numero_clase" in (r.json or {}).get("detail", ""), r)
    verificar("los rechazados no cuentan (macro sigue en 4)", proximos(s)["macro"] == 4)
    r = s.pedir("POST", "/api/sesiones", json_={**con_ocho, "numero_clase": 99})
    v = s.pedir("GET", f"/api/sesiones/{con_ocho['id']}").json
    verificar("crear otra vez una que existía -> 200 sin cambiar el número",
              r.status == 200 and v["numero_clase"] == 8 and proximos(s)["macro"] == 4, (r, v))
    v = crear(s, sesion_auto())
    verificar("sesión Auto sin numero_clase -> null", v and v["numero_clase"] is None, v)
    v = crear(s, sesion_auto(numero_clase=5))
    verificar("sesión Auto con numero_clase=5 lo guarda", v and v["numero_clase"] == 5, v)
    verificar("las otras materias no se mueven",
              proximos(s) == {"analisis": 1, "macro": 4, "riesgo": 1}, proximos(s))
    v = crear(s, nueva_sesion(materia_id="teoria-juegos"))
    verificar("materia inactiva: crear sesión igual anda (clase 1) y sigue oculta en GET",
              v and v["materia_id"] == "teoria-juegos" and v["numero_clase"] == 1
              and "teoria-juegos" not in proximos(s), v)

    # --- PATCH /api/materias/{id}
    r = s.pedir("PATCH", "/api/materias/macro", json_={"proximo_numero": 10})
    verificar("PATCH proximo_numero=10 -> 200 con la materia",
              r.status == 200 and set(r.json) == CLAVES_MATERIA and r.json["id"] == "macro"
              and r.json["proximo_numero"] == 10 and r.json["tipos_clase"] == ["teorica", "practica"], r)
    verificar("GET /api/materias muestra 10", proximos(s)["macro"] == 10)
    v = crear(s, nueva_sesion(materia_id="macro"))
    verificar("sesión nueva toma el 10", v and v["numero_clase"] == 10, v)
    verificar("y proximo pasa a 11", proximos(s)["macro"] == 11)
    r = s.pedir("PATCH", "/api/materias/macro", json_={"numero_clase_inicial": 5})
    verificar("PATCH numero_clase_inicial=5 -> proximo 5 + 4 clases = 9",
              r.status == 200 and r.json["proximo_numero"] == 9 and proximos(s)["macro"] == 9, r)
    r = s.pedir("PATCH", "/api/materias/macro", json_={"proximo_numero": 1})
    archivo = json.loads(ruta.read_text("utf-8"))
    inicial = next(m for m in archivo if m["id"] == "macro")["numero_clase_inicial"]
    verificar("PATCH proximo_numero=1 deja numero_clase_inicial en -3 (está bien)",
              r.status == 200 and r.json["proximo_numero"] == 1 and inicial == -3, (r, inicial))
    r = s.pedir("PATCH", "/api/materias/macro", json_={"proximo_numero": 7, "otra_cosa": "se ignora"})
    verificar("PATCH proximo_numero=7 (con una clave extra que se ignora)",
              r.status == 200 and r.json["proximo_numero"] == 7, r)
    r = s.pedir("PATCH", "/api/materias/quimica", json_={"proximo_numero": 3})
    verificar("PATCH materia desconocida -> 404",
              r.status == 404 and r.json == {"detail": "materia no encontrada"}, r)
    r = s.pedir("PATCH", "/api/materias/teoria-juegos", json_={"proximo_numero": 4})
    verificar("PATCH materia inactiva -> 200 (y sigue oculta en GET)",
              r.status == 200 and r.json["id"] == "teoria-juegos" and r.json["proximo_numero"] == 4
              and "teoria-juegos" not in proximos(s), r)
    malos = {"objeto vacío": {}, "las dos claves": {"proximo_numero": 3, "numero_clase_inicial": 2},
             "texto": {"proximo_numero": "diez"}, "cero": {"proximo_numero": 0},
             "true": {"proximo_numero": True}, "negativo": {"numero_clase_inicial": -1},
             "decimal": {"proximo_numero": 2.5}, "null": {"proximo_numero": None},
             "lista": [10], "otra clave": {"numero": 3}}
    for nombre, cuerpo in malos.items():
        r = s.pedir("PATCH", "/api/materias/macro", json_=cuerpo)
        verificar(f"PATCH con {nombre} -> 422", r.status == 422 and "detail" in r.json, r)
    r = s.pedir("PATCH", "/api/materias/macro", b"{roto", tipo="application/json")
    verificar("PATCH con JSON roto -> 422", r.status == 422 and "detail" in r.json, r)
    verificar("los PATCH malos no cambian nada", proximos(s)["macro"] == 7)
    r = s.pedir("GET", "/api/materias/macro")
    verificar("GET /api/materias/{id} -> 405", r.status == 405, r)

    archivo = json.loads(ruta.read_text("utf-8"))
    esperado = [{**m, "numero_clase_inicial": 3} if m["id"] in ("macro", "teoria-juegos") else m
                for m in original]
    verificar("materias.json: JSON válido, mismo orden y los otros campos intactos",
              archivo == esperado and all(list(a) == list(b) for a, b in zip(archivo, original)), archivo)
    s.parar()
    s.arrancar()
    verificar("tras reiniciar: la numeración se mantiene",
              proximos(s) == {"analisis": 1, "macro": 7, "riesgo": 1}, proximos(s))
    verificar("tras reiniciar: materias.json igual", json.loads(ruta.read_text("utf-8")) == esperado)

    # --- recarga en caliente
    escribir_json(ruta, [{**m, "activa": False} if m["id"] == "macro" else m for m in esperado])
    verificar("recarga en caliente: activa=false saca a macro",
              list(proximos(s)) == ["analisis", "riesgo"], proximos(s))
    v = crear(s, nueva_sesion(materia_id="macro"))
    verificar("materia desactivada: crear sesión igual anda y sigue la numeración (7)",
              v and v["materia_id"] == "macro" and v["numero_clase"] == 7, v)
    r = s.pedir("PATCH", "/api/materias/macro", json_={"proximo_numero": 20})
    desactivada = json.loads(ruta.read_text("utf-8"))
    macro = next(m for m in desactivada if m["id"] == "macro")
    verificar("materia desactivada: PATCH igual anda (20 = 15 + 5 clases) y queda inactiva",
              r.status == 200 and r.json["proximo_numero"] == 20 and macro["numero_clase_inicial"] == 15
              and macro["activa"] is False and "macro" not in proximos(s), (r, macro))
    roto = b'[{"id": "analisis", '
    escribir_json(ruta, roto)
    servidas = [list(proximos(s)) for _ in range(3)]
    verificar("JSON roto: se siguen sirviendo las anteriores",
              servidas == [["analisis", "riesgo"]] * 3, servidas)
    avisos = lineas_log(s.datos, "AVISO: materias.json inválido")
    verificar("JSON roto: un solo AVISO en el log", avisos == 1, avisos)
    r = s.pedir("PATCH", "/api/materias/riesgo", json_={"proximo_numero": 4})
    verificar("JSON roto: PATCH -> 503 sin pisar el archivo",
              r.status == 503 and ruta.read_bytes() == roto and proximos(s)["riesgo"] == 1, r)
    escribir_json(ruta, b'[{"id": "macro", "activa": "no"}]')  # otra versión: JSON válido que no sirve
    servidas = [list(proximos(s)) for _ in range(2)]
    avisos = lineas_log(s.datos, "AVISO: materias.json inválido")
    verificar("otra versión inválida: un AVISO más (uno por versión) y siguen las anteriores",
              avisos == 2 and servidas == [["analisis", "riesgo"]] * 2, (avisos, servidas))
    escribir_json(ruta, [{**m, "activa": True} if m["id"] == "macro" else m for m in desactivada])
    verificar("se arregla el archivo y se reactiva macro: vuelve con su número (20)",
              proximos(s) == {"analisis": 1, "macro": 20, "riesgo": 1}, proximos(s))
    verificar("servidor.log registra las recargas", lineas_log(s.datos, "materias.json recargado") >= 2)
    arreglado = json.loads(ruta.read_text("utf-8"))
    # 40 y no 4: así cambia el tamaño y la recarga no depende de la resolución del mtime.
    escribir_json(ruta, [{**m, "numero_clase_inicial": 40} if m["id"] == "riesgo" else m for m in arreglado])
    verificar("numero_clase_inicial editado a mano se ve sin reiniciar (riesgo 40)",
              proximos(s)["riesgo"] == 40, proximos(s))

    # Romperlo y restaurarlo de una copia que conserva la fecha (copy, Explorador): la marca vuelve
    # a ser la de la versión cargada y PATCH tiene que volver a andar (no quedarse en 503).
    st, contenido = ruta.stat(), ruta.read_bytes()
    escribir_json(ruta, roto)
    proximos(s)
    escribir_json(ruta, contenido)
    os.utime(ruta, ns=(st.st_atime_ns, st.st_mtime_ns))
    r = s.pedir("PATCH", "/api/materias/riesgo", json_={"proximo_numero": 6})
    riesgo = next((m for m in json.loads(ruta.read_text("utf-8")) if m["id"] == "riesgo"), {})
    verificar("restaurado con la fecha de la versión cargada: PATCH vuelve a andar",
              r.status == 200 and r.json["proximo_numero"] == 6 and riesgo.get("numero_clase_inicial") == 6
              and proximos(s)["riesgo"] == 6, (r, riesgo))

    # --- concurrencia (el servidor es multihilo)
    cuerpos = [nueva_sesion(materia_id="analisis") for _ in range(12)]
    with ThreadPoolExecutor(len(cuerpos)) as hilos:
        codigos = list(hilos.map(lambda c: s.pedir("POST", "/api/sesiones", json_=c).status, cuerpos))
    numeros = sorted(s.pedir("GET", f"/api/sesiones/{c['id']}").json["numero_clase"] for c in cuerpos)
    verificar("12 sesiones creadas a la vez: clases 1 a 12 sin repetir",
              codigos == [201] * 12 and numeros == list(range(1, 13))
              and proximos(s)["analisis"] == 13, (codigos, numeros))
    with ThreadPoolExecutor(8) as hilos:
        rs = list(hilos.map(lambda n: s.pedir("PATCH", "/api/materias/analisis",
                                              json_={"proximo_numero": n}), range(30, 38)))
    final = proximos(s)["analisis"]
    archivo = json.loads(ruta.read_text("utf-8"))  # JSON válido después de 8 escrituras a la vez
    inicial = next(m for m in archivo if m["id"] == "analisis")["numero_clase_inicial"]
    verificar("8 PATCH a la vez: todos 200 y el archivo queda válido y coherente",
              all(r.status == 200 for r in rs) and 30 <= final < 38 and inicial == final - 12
              and [m["id"] for m in archivo] == [m["id"] for m in original], (rs, final, inicial))


# ----------------------------------------------------------------------------- esperando_usuario

def esperando_usuario(tmp: Path, servidores: list):
    s = Servidor(tmp / "datos-esperando", "--procesar-seg", "3")
    servidores.append(s)
    s.arrancar()
    audio = random.Random(5).randbytes(5000)
    r = s.pedir("PATCH", "/api/materias/macro", json_={"proximo_numero": 5})
    verificar("macro arranca en la clase 5", r.status == 200 and r.json["proximo_numero"] == 5, r)

    auto = sesion_auto()
    sid = auto["id"]
    crear(s, auto)
    r = subir_y_finalizar(s, sid, audio)
    verificar("Auto: finalizar -> 200 en_cola", r.status == 200 and r.json == {"estado": "en_cola"}, r)

    # Una con materia (analisis, clase 1), recién finalizada: en en_cola se le cambia la materia.
    otra = nueva_sesion()
    crear(s, otra)
    subir_y_finalizar(s, otra["id"], audio)
    ruta_otra = f"/api/sesiones/{otra['id']}/materia"
    r = s.pedir("POST", ruta_otra, json_={"materia_id": "macro"})
    verificar("POST /materia en en_cola cambia la materia y toma su próximo número (5)",
              r.status == 200 and set(r.json) == CLAVES_SESION and r.json["estado"] == "en_cola"
              and r.json["materia_id"] == "macro" and r.json["numero_clase"] == 5, r)
    verificar("reasignar mueve la cuenta de clases",
              proximos(s) == {"analisis": 1, "macro": 6, "riesgo": 1}, proximos(s))
    r = s.pedir("POST", ruta_otra, json_={"materia_id": "macro"})
    verificar("POST /materia con la misma materia no cambia el número",
              r.status == 200 and r.json["numero_clase"] == 5 and proximos(s)["macro"] == 6, r)

    # Auto que todavía graban: una a riesgo y otra a una materia inactiva.
    grabando = sesion_auto()
    crear(s, grabando)
    r = s.pedir("POST", f"/api/sesiones/{grabando['id']}/materia", json_={"materia_id": "riesgo"})
    verificar("POST /materia en grabando -> 200 con el próximo número de la materia",
              r.status == 200 and r.json["estado"] == "grabando" and r.json["materia_id"] == "riesgo"
              and r.json["numero_clase"] == 1 and proximos(s)["riesgo"] == 2, r)
    inactiva = sesion_auto()
    crear(s, inactiva)
    r = s.pedir("POST", f"/api/sesiones/{inactiva['id']}/materia", json_={"materia_id": "teoria-juegos"})
    verificar("POST /materia con una materia inactiva -> 200 (y sigue oculta en GET)",
              r.status == 200 and r.json["materia_id"] == "teoria-juegos" and r.json["numero_clase"] == 1
              and "teoria-juegos" not in proximos(s), r)

    # Mientras la Auto procesa, "otra" pasa por procesando: en las primeras etapas se puede reasignar.
    temprano = []

    def reasignar_temprano(_):
        if temprano:
            return
        v = s.pedir("GET", f"/api/sesiones/{otra['id']}").json
        if v["estado"] == "procesando" and v["etapa"] in ETAPAS[:3]:  # lejos de apunte
            temprano.append((v["etapa"], s.pedir("POST", ruta_otra, json_={"materia_id": "riesgo"})))

    vistos, etapas, progresos, ultimo = seguir(s, sid, {"esperando_usuario"}, limite=20, pausa=0.05,
                                               al_ver=reasignar_temprano)
    etapa_otra, r = temprano[0] if temprano else (None, None)
    verificar("POST /materia en procesando antes del apunte -> 200, materia y número nuevos",
              r is not None and r.status == 200 and r.json["estado"] == "procesando"
              and r.json["materia_id"] == "riesgo" and r.json["numero_clase"] == 2, (etapa_otra, r))
    verificar("la cuenta de clases sigue a la reasignación",
              proximos(s) == {"analisis": 1, "macro": 5, "riesgo": 3}, proximos(s))

    verificar("Auto: en_cola -> procesando -> esperando_usuario",
              vistos == ["en_cola", "procesando", "esperando_usuario"], vistos)
    verificar("Auto: etapas hasta clasificar, en orden, progreso creciente < 0.5",
              etapas and etapas[-1] == "clasificar" and set(etapas) <= set(ETAPAS_AUTO)
              and etapas == sorted(etapas, key=ETAPAS_AUTO.index)
              and progresos == sorted(progresos) and all(0 <= p < 0.5 for p in progresos),
              (etapas, progresos))
    verificar("esperando_usuario: etapa clasificar, sin progreso, pdf, materia ni título",
              ultimo["etapa"] == "clasificar" and ultimo["progreso"] is None and ultimo["pdf"] is None
              and ultimo["materia_id"] is None and ultimo["numero_clase"] is None
              and ultimo["titulo"] is None and set(ultimo) == CLAVES_SESION | {"espera"}, ultimo)
    verificar("esperando_usuario: espera la materia (§15), sin dudas",
              ultimo.get("espera") == "materia" and "dudas" not in ultimo, ultimo)
    error = ultimo["error"] or ""
    verificar("esperando_usuario: error como el del server real",
              error.startswith("No pude determinar la materia (confianza 0.30: servidor falso).")
              and "Sugerida: analisis." in error
              and f"uv run classmer asignar {sid} --materia <materia>" in error
              and error.endswith("(opciones: analisis, macro, riesgo)"), error)
    time.sleep(0.3)
    lista = s.pedir("GET", "/api/sesiones").json
    en_lista = next((x for x in lista if x["id"] == sid), {})
    verificar("esperando_usuario se queda ahí y sale igual en la lista",
              en_lista.get("estado") == "esperando_usuario" and en_lista.get("error") == error, en_lista)

    ruta = f"/api/sesiones/{sid}/materia"
    r = s.pedir("POST", ruta, json_={"materia_id": "quimica"})
    verificar("POST /materia con materia inexistente -> 422",
              r.status == 422 and r.json == {"detail": "materia desconocida: 'quimica'"}, r)
    for nombre, cuerpo in {"objeto vacío": {}, "materia_id número": {"materia_id": 5},
                           "materia_id null": {"materia_id": None}, "lista": ["macro"]}.items():
        r = s.pedir("POST", ruta, json_=cuerpo)
        verificar(f"POST /materia con {nombre} -> 422", r.status == 422 and "detail" in r.json, r)
    r = s.pedir("POST", ruta, b"{roto", tipo="application/json")
    verificar("POST /materia con JSON roto -> 422", r.status == 422 and "detail" in r.json, r)
    r = s.pedir("POST", f"/api/sesiones/{uuid.uuid4()}/materia", json_={"materia_id": "macro"})
    verificar("POST /materia de sesión desconocida -> 404", r.status == 404, r)
    r = s.pedir("GET", ruta)
    verificar("GET /materia -> 405", r.status == 405, r)
    v = s.pedir("GET", f"/api/sesiones/{sid}").json
    verificar("los rechazos no cambian la sesión",
              v["estado"] == "esperando_usuario" and v["materia_id"] is None, v)

    numero = proximos(s)["riesgo"]
    r = s.pedir("POST", ruta, json_={"materia_id": "riesgo"})
    verificar("POST /materia válido -> 200 con materia y número",
              r.status == 200 and set(r.json) == CLAVES_SESION and r.json["materia_id"] == "riesgo"
              and r.json["numero_clase"] == numero and r.json["estado"] == "en_cola"
              and r.json["error"] is None, r)
    meta = json.loads((s.datos / "sesiones" / sid / "sesion.json").read_text("utf-8"))
    verificar("sesion.json: origen manual y asignada_en",
              meta["origen_materia"] == "manual" and isinstance(meta.get("asignada_en"), float)
              and meta["numero_clase"] == numero, meta)
    verificar("la clase asignada cuenta para proximo_numero", proximos(s)["riesgo"] == numero + 1)

    tarde = []

    def reasignar_tarde(x):
        if x["estado"] == "procesando" and not tarde:
            tarde.append((x["etapa"], s.pedir("POST", ruta, json_={"materia_id": "macro"})))

    vistos, etapas, progresos, ultimo = seguir(s, sid, {"lista"}, limite=20, pausa=0.05,
                                               al_ver=reasignar_tarde)
    verificar("asignada: en_cola -> procesando -> lista", vistos == ["en_cola", "procesando", "lista"], vistos)
    verificar("asignada: retoma en apunte y publicar, progreso de 0.5 a 1",
              etapas and set(etapas) <= {"apunte", "publicar"} and etapas == sorted(etapas)
              and progresos == sorted(progresos) and all(0.5 <= p < 1 for p in progresos),
              (etapas, progresos))
    etapa_tarde, r = tarde[0] if tarde else (None, None)
    verificar("POST /materia procesando en apunte/publicar -> 409",
              etapa_tarde in ("apunte", "publicar") and r.status == 409
              and r.json == {"detail": "el apunte ya empezó (estado=procesando)"}, tarde)
    verificar("lista: título, pdf y la materia elegida",
              ultimo["titulo"] == f"Apunte de prueba · Clase {numero}" and ultimo["materia_id"] == "riesgo"
              and ultimo["pdf"] == f"/api/sesiones/{sid}/apunte.pdf" and ultimo["progreso"] == 1.0
              and ultimo["error"] is None, ultimo)
    r = s.pedir("GET", ultimo["pdf"])
    verificar("PDF de la sesión asignada", r.status == 200 and xref_ok(r.datos)
              and b"Riesgo" in r.datos and f"Clase {numero}".encode() in r.datos, r)
    r = s.pedir("POST", ruta, json_={"materia_id": "macro"})
    verificar("POST /materia sobre una sesión lista -> 409",
              r.status == 409 and r.json == {"detail": "el apunte ya empezó (estado=lista)"}, r)
    v = seguir(s, otra["id"], {"lista"}, limite=10)[3]
    verificar("la reasignada procesando termina lista por el camino normal",
              v["estado"] == "lista" and v["materia_id"] == "riesgo"
              and v["titulo"] == "Apunte de prueba · Clase 2", v)

    s.parar()
    s.arrancar()
    v = s.pedir("GET", f"/api/sesiones/{sid}").json
    verificar("tras reiniciar: la asignada sigue lista con su materia",
              v["estado"] == "lista" and v["materia_id"] == "riesgo" and v["numero_clase"] == numero, v)
    v = s.pedir("GET", f"/api/sesiones/{grabando['id']}").json
    verificar("tras reiniciar: la que grababa conserva la materia elegida",
              v["estado"] == "grabando" and v["materia_id"] == "riesgo" and v["numero_clase"] == 1, v)
    v = s.pedir("GET", f"/api/sesiones/{inactiva['id']}").json
    verificar("tras reiniciar: la de la materia inactiva también",
              v["materia_id"] == "teoria-juegos" and v["numero_clase"] == 1, v)
    asignaciones = lineas_log(s.datos, "materia asignada Auto -> riesgo")
    verificar("servidor.log registra las asignaciones", asignaciones == 2, asignaciones)

    # Auto que el celu creó con numero_clase: al elegir la materia conserva ese número (como el
    # server real); si después se cambia por otra materia, toma el próximo de la nueva.
    con_numero = sesion_auto(numero_clase=9)
    crear(s, con_numero)
    ruta_cn = f"/api/sesiones/{con_numero['id']}/materia"
    macro_antes = proximos(s)["macro"]
    r = s.pedir("POST", ruta_cn, json_={"materia_id": "macro"})
    verificar("Auto con numero_clase=9: al asignarla conserva el 9",
              r.status == 200 and r.json["materia_id"] == "macro" and r.json["numero_clase"] == 9
              and proximos(s)["macro"] == macro_antes + 1, r)
    riesgo_antes = proximos(s)["riesgo"]
    r = s.pedir("POST", ruta_cn, json_={"materia_id": "riesgo"})
    verificar("y al cambiarla de materia toma el próximo de la nueva",
              r.status == 200 and r.json["materia_id"] == "riesgo" and r.json["numero_clase"] == riesgo_antes
              and proximos(s)["macro"] == macro_antes, r)


# ----------------------------------------------------------------------------- flags nuevos

def rechazos_y_token(tmp: Path, servidores: list):
    for codigo, detalle in [(409, "el apunte ya empezó (simulado)"), (422, "materia desconocida (simulado)")]:
        s = Servidor(tmp / f"datos-rechazar{codigo}", "--rechazar-materia", str(codigo))
        servidores.append(s)
        s.arrancar()
        sesion = sesion_auto()
        crear(s, sesion)
        ruta = f"/api/sesiones/{sesion['id']}/materia"
        r = s.pedir("POST", ruta, json_={"materia_id": "macro"})
        verificar(f"--rechazar-materia {codigo}: POST /materia -> {codigo}",
                  r.status == codigo and r.json == {"detail": detalle}, r)
        r = s.pedir("POST", ruta, b"{roto", tipo="application/json")
        verificar(f"--rechazar-materia {codigo}: siempre {codigo}, aunque el cuerpo esté mal",
                  r.status == codigo, r)
        v = s.pedir("GET", f"/api/sesiones/{sesion['id']}").json
        verificar(f"--rechazar-materia {codigo}: no cambia nada",
                  v["materia_id"] is None and v["numero_clase"] is None and proximos(s)["macro"] == 1, v)
        r = s.pedir("POST", f"/api/sesiones/{uuid.uuid4()}/materia", json_={"materia_id": "macro"})
        verificar(f"--rechazar-materia {codigo}: sesión desconocida sigue siendo 404", r.status == 404, r)
        s.parar()

    t = Servidor(tmp / "datos-token", "--cualquier-token")
    servidores.append(t)
    t.arrancar()
    r = t.pedir("GET", "/api/health", token="el-token-del-server-real")
    verificar("--cualquier-token: otro token -> 200", r.status == 200, r)
    r = t.pedir("GET", "/api/materias", token="x")
    verificar("--cualquier-token: materias con otro token -> 200", r.status == 200, r)
    r = t.pedir("GET", "/api/health", token=None)
    verificar("--cualquier-token: sin token -> 401", r.status == 401, r)
    r = t.pedir("GET", "/api/health", token=" ")
    verificar("--cualquier-token: token vacío -> 401", r.status == 401, r)
    t.parar()


def materias_explicitas_y_compat(tmp: Path, servidores: list):
    """--materias PATH, y una sesión guardada por falso-0.1 (sin numero_clase)."""
    datos = tmp / "datos-compat"
    vieja = str(uuid.uuid4())
    (datos / "sesiones" / vieja).mkdir(parents=True)
    (datos / "sesiones" / vieja / "sesion.json").write_text(json.dumps({
        "id": vieja, "materia_id": "fisica", "origen_materia": "manual", "tipo_clase": "teorica",
        "inicio": "2026-10-01T10:00:00-03:00", "dispositivo": "falso-0.1",
        "creada": "2026-10-01T10:00:00-03:00", "finalizacion": {
            "sha256": "0" * 64, "bytes_totales": 0, "duracion_ms": 60_000, "eventos_totales": 0,
            "finalizada_en": time.time() - 3600, "finalizada_iso": "2026-10-01T11:00:00-03:00"}}),
        "utf-8")
    ruta = tmp / "mis-materias.json"
    mias = [{"id": "fisica", "nombre": "Física", "numero_clase_inicial": 7},
            {"id": "quimica", "nombre": "Química", "calendario": {"titulo_regex": "Qu[ií]mica"},
             "tipos_clase": ["teorica"], "activa": False}]
    ruta.write_text(json.dumps(mias, ensure_ascii=False), "utf-8")

    s = Servidor(datos, "--materias", str(ruta))
    servidores.append(s)
    s.arrancar()
    r = s.pedir("GET", "/api/materias")
    verificar("--materias: sólo las activas, con valores por defecto y la sesión vieja contada",
              r.status == 200 and r.json == [{"id": "fisica", "nombre": "Física", "calendario": None,
                                             "tipos_clase": ["teorica", "practica"], "proximo_numero": 8}], r)
    verificar("--materias: no crea <datos>/materias.json", not (datos / "materias.json").exists())
    v = s.pedir("GET", f"/api/sesiones/{vieja}").json
    verificar("sesión de falso-0.1: numero_clase null y título sin número",
              v["estado"] == "lista" and v["numero_clase"] is None and v["titulo"] == "Apunte de prueba"
              and set(v) == CLAVES_SESION, v)
    r = s.pedir("GET", f"/api/sesiones/{vieja}/apunte.pdf")
    verificar("sesión de falso-0.1: el PDF sale igual", r.status == 200 and xref_ok(r.datos), r)
    r = s.pedir("PATCH", "/api/materias/fisica", json_={"proximo_numero": 3})
    archivo = json.loads(ruta.read_text("utf-8"))
    verificar("--materias: PATCH reescribe ese archivo y agrega sólo lo que cambió",
              r.status == 200 and r.json["proximo_numero"] == 3
              and archivo == [{**mias[0], "numero_clase_inicial": 2}, mias[1]]
              and list(archivo[0]) == ["id", "nombre", "numero_clase_inicial"], archivo)
    de_quimica = nueva_sesion(materia_id="quimica")
    v = crear(s, de_quimica)
    verificar("--materias: crear con la inactiva anda (clase 1)",
              v and v["materia_id"] == "quimica" and v["numero_clase"] == 1, v)
    r = s.pedir("POST", "/api/sesiones", json_=nueva_sesion(materia_id="quimica", tipo_clase="practica"))
    verificar("--materias: respeta los tipos_clase de la materia (practica -> 400)", r.status == 400, r)

    escribir_json(ruta, archivo[:1])  # se saca quimica del archivo
    r = s.pedir("POST", "/api/sesiones", json_=de_quimica)
    verificar("materia sacada del archivo: el reintento de una sesión que ya existía -> 200",
              r.status == 200 and r.json == {"id": de_quimica["id"], "estado": "grabando"}, r)
    r = s.pedir("POST", "/api/sesiones", json_=nueva_sesion(materia_id="quimica"))
    verificar("materia sacada del archivo: una sesión nueva -> 400 desconocida",
              r.status == 400 and "desconocida" in r.json["detail"], r)
    r = s.pedir("POST", f"/api/sesiones/{de_quimica['id']}/materia", json_={"materia_id": "quimica"})
    verificar("materia sacada del archivo: POST /materia -> 422",
              r.status == 422 and r.json == {"detail": "materia desconocida: 'quimica'"}, r)
    s.parar()

    malas = {"inexistente": None, "con 'activa' que no es bool": [{"id": "x", "activa": "sí"}],
             "con ids repetidos": [{"id": "x"}, {"id": "x"}], "que no es una lista": {"id": "x"}}
    for n, (nombre, contenido) in enumerate(malas.items()):
        mal = tmp / f"materias-malas-{n}.json"
        if contenido is not None:
            mal.write_text(json.dumps(contenido), "utf-8")
        proc = subprocess.run(
            [sys.executable, str(SERVIDOR), "--puerto", str(puerto_libre()), "--materias", str(mal),
             "--datos", str(tmp / "datos-materias-malas")],
            capture_output=True, timeout=15, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        verificar(f"--materias {nombre}: no arranca y dice por qué",
                  proc.returncode != 0 and mal.name.encode() in proc.stderr, proc.stderr[-300:])


# ----------------------------------------------------------------------------- fallas simuladas

def fallas_simuladas(tmp: Path, servidores: list):
    f = Servidor(tmp / "datos-fallas", "--fallar-prob", "1", "--latencia-ms", "150")
    servidores.append(f)
    f.arrancar()
    t0 = time.monotonic()
    r = f.pedir("GET", "/api/health")
    verificar("--fallar-prob 1: health igual responde 200", r.status == 200, r)
    verificar("--latencia-ms 150 demora la respuesta", time.monotonic() - t0 >= 0.15)
    r = f.pedir("GET", "/api/materias")
    verificar("--fallar-prob 1: materias -> 503", r.status == 503 and r.json == {"detail": "falla simulada"}, r)
    r = f.pedir("POST", "/api/sesiones", json_=nueva_sesion())
    verificar("--fallar-prob 1: crear sesión -> 503", r.status == 503, r)
    f.parar()

    for extra, esperado in [((), 0), (("--parciales",), 50_000)]:
        c = Servidor(tmp / f"datos-cortar{len(extra)}", "--cortar-prob", "1", *extra)
        servidores.append(c)
        c.arrancar()
        sesion = nueva_sesion()
        c.pedir("POST", "/api/sesiones", json_=sesion)
        try:
            resultado = c.audio(sesion["id"], 0, os.urandom(100_000))
        except OSError as e:  # URLError, RemoteDisconnected, ConnectionResetError...
            resultado = e
        nombre = "--cortar-prob 1" + (" --parciales" if extra else "")
        verificar(f"{nombre}: el PUT se corta sin respuesta", isinstance(resultado, OSError), resultado)
        verificar(f"{nombre}: quedan {esperado} bytes", c.bytes_recibidos(sesion["id"]) == esperado,
                  c.bytes_recibidos(sesion["id"]))
        c.parar()

    p = Servidor(tmp / "datos-parciales", "--parciales", "--sin-reset-hash")
    servidores.append(p)
    p.arrancar()
    sesion = nueva_sesion()
    p.pedir("POST", "/api/sesiones", json_=sesion)
    resto = subida_cortada(p.puerto, sesion["id"], 50_000, 20_000)
    verificar("--parciales: cliente corta y se guardan los 20000 bytes que llegaron",
              resto == b"" and p.bytes_recibidos(sesion["id"]) == 20_000, (resto, p.bytes_recibidos(sesion["id"])))
    r = p.pedir("POST", f"/api/sesiones/{sesion['id']}/finalizar",
                json_={"sha256": "a" * 64, "bytes_totales": 20_000, "duracion_ms": 1, "eventos_totales": 0})
    verificar("--sin-reset-hash: hash_distinto sin vaciar el audio",
              r.status == 409 and r.json == {"motivo": "hash_distinto", "bytes_recibidos": 20_000}
              and p.bytes_recibidos(sesion["id"]) == 20_000, r)
    p.parar()


# ----------------------------------------------------------------------------- adjuntos

def adjunto(servidor, sid, campos: dict, archivo: tuple[str, bytes, str] | None = None):
    """POST /adjuntos como lo manda OkHttp: el filename en UTF-8 sin codificar."""
    limite = "----classmer" + uuid.uuid4().hex
    cuerpo = b""
    for clave, valor in campos.items():
        cuerpo += (f'--{limite}\r\nContent-Disposition: form-data; name="{clave}"\r\n\r\n'
                   f"{valor}\r\n").encode()
    if archivo:
        nombre, datos, tipo = archivo
        cuerpo += (f'--{limite}\r\nContent-Disposition: form-data; name="archivo"; '
                   f'filename="{nombre}"\r\nContent-Type: {tipo}\r\n\r\n').encode("utf-8") + datos + b"\r\n"
    cuerpo += f"--{limite}--\r\n".encode()
    return servidor.pedir("POST", f"/api/sesiones/{sid}/adjuntos", cuerpo,
                          tipo=f"multipart/form-data; boundary={limite}")


def muestras(rnd: random.Random) -> dict[str, bytes]:
    """Un archivo de cada tipo que acepta el server, reconocible por sus primeros bytes."""
    return {
        "pdf": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + rnd.randbytes(5000),
        "jpeg": b"\xff\xd8\xff\xe0\x00\x10JFIF" + rnd.randbytes(3000),
        "png": b"\x89PNG\r\n\x1a\n" + rnd.randbytes(2000),
        "webp": b"RIFF" + (4000).to_bytes(4, "little") + b"WEBPVP8 " + rnd.randbytes(4000),
        "heic": b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + rnd.randbytes(3000),
    }


def estados_adjuntos(servidor, sid) -> tuple[list[str], int]:
    v = servidor.pedir("GET", f"/api/sesiones/{sid}").json
    return [a["estado"] for a in v["adjuntos"]], v["adjuntos_sin_usar"]


def adjuntos(tmp: Path, servidores: list):
    s = Servidor(tmp / "datos-adjuntos", "--procesar-seg", "2", "--max-adjunto-mb", "1")
    servidores.append(s)
    s.arrancar()
    rnd = random.Random(11)
    m = muestras(rnd)
    creado = "2026-10-22T15:03:10-03:00"
    sesion = nueva_sesion()
    sid = sesion["id"]
    crear(s, sesion)

    # --- un PDF mientras se graba, con su minuto
    a1 = str(uuid.uuid4())
    original = "Slides clase 5 (versión final).pdf"
    campos1 = {"id": a1, "offset_ms": 2_040_000, "creado": creado}
    r = adjunto(s, sid, campos1, (original, m["pdf"], "application/pdf"))
    verificar("adjunto PDF grabando -> 201, nombre ASCII, original intacto y pendiente",
              r.status == 201 and set(r.json) == CLAVES_ADJUNTO | {"sesion"} and r.json["id"] == a1
              and r.json["nombre"] == "Slides_clase_5_version_final.pdf" and r.json["nombre_original"] == original
              and r.json["tipo"] == "pdf" and r.json["bytes"] == len(m["pdf"])
              and r.json["offset_ms"] == 2_040_000 and r.json["creado"] == creado
              and r.json["estado"] == "pendiente", r)
    sesion_json = (r.json or {}).get("sesion") or {}
    verificar("la respuesta trae la sesión con el adjunto",
              set(sesion_json) == CLAVES_SESION and [a["id"] for a in sesion_json["adjuntos"]] == [a1]
              and sesion_json["adjuntos_sin_usar"] == 0 and sesion_json["estado"] == "grabando", sesion_json)
    carpeta = s.datos / "sesiones" / sid / "adjuntos"
    verificar("adjunto guardado byte a byte",
              (carpeta / "Slides_clase_5_version_final.pdf").read_bytes() == m["pdf"])
    r = adjunto(s, sid, campos1, (original, m["pdf"], "application/pdf"))
    verificar("mismo id -> 200 sin otra copia",
              r.status == 200 and r.json["nombre"] == "Slides_clase_5_version_final.pdf"
              and len(list(carpeta.iterdir())) == 1, r)

    # --- la consulta sin el archivo: la app pregunta así antes de mandar uno de hasta 100 MB
    r = adjunto(s, sid, {"id": a1})
    verificar("consulta sin archivo de uno que ya está -> 200 con el adjunto y la sesión (como el server real)",
              r.status == 200 and set(r.json) == CLAVES_ADJUNTO | {"sesion"} and r.json["id"] == a1
              and r.json["nombre"] == "Slides_clase_5_version_final.pdf"
              and [a["id"] for a in r.json["sesion"]["adjuntos"]] == [a1], r)
    r = adjunto(s, sid, {"id": str(uuid.uuid4())})
    verificar("consulta sin archivo de uno nuevo -> 422 falta el archivo (entonces la app lo manda)",
              r.status == 422 and r.json == {"detail": "falta el archivo (campo archivo)"}, r)
    r = adjunto(s, str(uuid.uuid4()), {"id": str(uuid.uuid4())})
    verificar("consulta sin archivo de una clase que no existe -> 404 sesión no encontrada",
              r.status == 404 and r.json == {"detail": "sesión no encontrada"}, r)
    verificar("las consultas no guardan nada", len(list(carpeta.iterdir())) == 1)

    # --- el tipo sale del contenido; nombres únicos sin distinguir mayúsculas
    casos = [("Slides.pdf", "pdf", "Slides.pdf"), ("slides.JPG", "jpeg", "slides-2.jpg"),
             ("captura sin extensión", "png", "captura_sin_extension.png"),
             ("foto.webp", "webp", "foto.webp"), ("IMG_1234.HEIC", "heic", "IMG_1234.heic"),
             ("Slides.png", "jpeg", "Slides-3.jpg")]
    for nombre, tipo, esperado in casos:
        r = adjunto(s, sid, {"id": str(uuid.uuid4())}, (nombre, m[tipo], "application/octet-stream"))
        verificar(f"adjunto {nombre!r} -> 201 {tipo} como {esperado}, sin minuto",
                  r.status == 201 and r.json["tipo"] == tipo and r.json["nombre"] == esperado
                  and r.json["offset_ms"] is None and r.json["nombre_original"] == nombre, r)
    r = adjunto(s, sid, {"id": str(uuid.uuid4())}, ("../../" + "á" * 80 + ".v2.pdf", m["pdf"], "application/pdf"))
    verificar("nombre con ruta, puntos y muy largo: sin path traversal, <= 60 + extensión",
              r.status == 201 and r.json["nombre"] == "a" * 60 + ".pdf", r)
    r = adjunto(s, sid, {"id": str(uuid.uuid4())}, ("tarde.pdf", b" " * 1000 + b"%PDF-1.4" + bytes(20), "application/pdf"))
    verificar("%PDF- dentro de los primeros 1024 bytes -> PDF", r.status == 201 and r.json["tipo"] == "pdf", r)

    # --- rechazos
    r = adjunto(s, sid, {"id": str(uuid.uuid4())}, ("notas.txt", "hola, esto es texto".encode(), "application/pdf"))
    verificar("texto -> 415 aunque diga PDF",
              r.status == 415 and r.json == {"detail": "el archivo no es PDF, JPEG, PNG, WebP ni HEIC"}, r)
    r = adjunto(s, sid, {"id": str(uuid.uuid4())}, ("tarde.pdf", b" " * 1020 + b"%PDF-1.4", "application/pdf"))
    verificar("%PDF- después del byte 1024 -> 415", r.status == 415, r)
    for nombre, extra in [("apenas más de 1 MB", 10), ("mucho más (no se lee entero)", 300_000)]:
        grande = b"%PDF-1.4\n" + rnd.randbytes(1024 * 1024 + extra)
        r = adjunto(s, sid, {"id": str(uuid.uuid4())}, ("Libro.pdf", grande, "application/pdf"))
        verificar(f"adjunto de {nombre} -> 413", r.status == 413 and r.json == {"detail": "archivo de más de 1 MB"}, r)
    eid = str(uuid.uuid4())
    evento(s, sid, {"id": eid, "creado": creado, "tipo": "marca", "offset_ms": 1, "marca": "importante"})
    otra = nueva_sesion()
    crear(s, otra)
    invalidos = {
        "sin id": ({}, ("x.pdf", m["pdf"], "application/pdf")),
        "id que no es UUID": ({"id": "abc"}, ("x.pdf", m["pdf"], "application/pdf")),
        "sin archivo": ({"id": str(uuid.uuid4())}, None),
        "archivo vacío": ({"id": str(uuid.uuid4())}, ("x.pdf", b"", "application/pdf")),
        "offset_ms negativo": ({"id": str(uuid.uuid4()), "offset_ms": -5}, ("x.pdf", m["pdf"], "application/pdf")),
        "offset_ms que no es número": ({"id": str(uuid.uuid4()), "offset_ms": "ya"}, ("x.pdf", m["pdf"], "application/pdf")),
        "creado sin zona": ({"id": str(uuid.uuid4()), "creado": "2026-10-22T15:03:10"}, ("x.pdf", m["pdf"], "application/pdf")),
        "id de un evento": ({"id": eid}, ("x.pdf", m["pdf"], "application/pdf")),
    }
    for nombre, (campos, archivo) in invalidos.items():
        r = adjunto(s, sid, campos, archivo)
        verificar(f"adjunto inválido ({nombre}) -> 422", r.status == 422 and "detail" in r.json, r)
    r = adjunto(s, otra["id"], campos1, (original, m["pdf"], "application/pdf"))
    verificar("id que ya es de otra clase -> 422",
              r.status == 422 and r.json == {"detail": "el id ya es de otra sesión o de un evento"}, r)
    r = s.pedir("POST", f"/api/sesiones/{sid}/adjuntos", b'{"id": "x"}', tipo="application/json")
    verificar("adjunto que no es multipart -> 422", r.status == 422, r)
    r = adjunto(s, str(uuid.uuid4()), {"id": str(uuid.uuid4())}, ("x.pdf", m["pdf"], "application/pdf"))
    verificar("adjunto de una clase que no existe -> 404 sesión no encontrada",
              r.status == 404 and r.json == {"detail": "sesión no encontrada"}, r)
    r = s.pedir("GET", f"/api/sesiones/{sid}/adjuntos")
    verificar("GET /adjuntos -> 405", r.status == 405, r)
    estados, sin_usar = estados_adjuntos(s, sid)
    verificar("GET sesión: 9 adjuntos pendientes, en orden de llegada, sin contar como eventos",
              estados == ["pendiente"] * 9 and sin_usar == 0
              and s.pedir("GET", f"/api/sesiones/{sid}").json["eventos"] == 1, estados)

    # --- se termina la clase: el apunte los usa
    audio = rnd.randbytes(4000)
    s.audio(sid, 0, audio)
    r = s.pedir("POST", f"/api/sesiones/{sid}/finalizar", json_={
        "sha256": hashlib.sha256(audio).hexdigest(), "bytes_totales": len(audio),
        "duracion_ms": 60_000, "eventos_totales": 1})
    verificar("finalizar: los adjuntos no cuentan en eventos_totales", r.status == 200, r)
    r = s.pedir("POST", f"/api/sesiones/{sid}/regenerar", json_={})
    verificar("regenerar en cola -> 409 ya se está procesando",
              r.status == 409 and r.json["detail"].startswith("ya se está procesando"), r)
    ultimo = seguir(s, sid, {"lista"}, limite=15)[3]
    verificar("lista: los 9 adjuntos usados",
              ultimo["estado"] == "lista" and [a["estado"] for a in ultimo["adjuntos"]] == ["usado"] * 9
              and ultimo["adjuntos_sin_usar"] == 0, ultimo)
    pdf = s.pedir("GET", ultimo["pdf"]).datos
    verificar("el PDF lista los adjuntos que usó", xref_ok(pdf) and b"Adjuntos que us\\363 este apunte \\(9\\)" in pdf
              and b"[34:00] ADJUNTO adjuntos/Slides_clase_5_version_final.pdf" in pdf, pdf[:300])

    # --- llega material después: queda sin usar hasta regenerar
    tarde = str(uuid.uuid4())
    r = adjunto(s, sid, {"id": tarde, "creado": creado}, ("Práctica 3.pdf", m["pdf"], "application/pdf"))
    verificar("adjunto con la clase lista -> 201 sin_usar",
              r.status == 201 and r.json["estado"] == "sin_usar" and r.json["nombre"] == "Practica_3.pdf"
              and r.json["sesion"]["adjuntos_sin_usar"] == 1, r)
    verificar("GET sesión: adjuntos_sin_usar = 1", estados_adjuntos(s, sid) == (["usado"] * 9 + ["sin_usar"], 1))
    r = s.pedir("POST", f"/api/sesiones/{sid}/regenerar", b"", tipo="application/json")
    verificar("regenerar (cuerpo vacío) -> 200 en_cola con todos pendientes",
              r.status == 200 and set(r.json) == CLAVES_SESION and r.json["estado"] == "en_cola"
              and [a["estado"] for a in r.json["adjuntos"]] == ["pendiente"] * 10
              and r.json["adjuntos_sin_usar"] == 0, r)
    r = s.pedir("POST", f"/api/sesiones/{sid}/regenerar", json_={})
    verificar("regenerar dos veces -> 409", r.status == 409 and "ya se está procesando" in r.json["detail"], r)
    vistos, etapas, _, ultimo = seguir(s, sid, {"lista"}, limite=15)
    verificar("regenerada: en_cola -> procesando (apunte, publicar) -> lista",
              vistos == ["en_cola", "procesando", "lista"] and set(etapas) <= {"apunte", "publicar"}, (vistos, etapas))
    verificar("regenerada: los 10 usados",
              [a["estado"] for a in ultimo["adjuntos"]] == ["usado"] * 10 and ultimo["adjuntos_sin_usar"] == 0, ultimo)
    verificar("el PDF nuevo incluye el que llegó tarde", b"Practica_3.pdf" in s.pedir("GET", ultimo["pdf"]).datos)
    r = s.pedir("POST", f"/api/sesiones/{otra['id']}/regenerar", json_={})
    verificar("regenerar una clase grabando -> 409",
              r.status == 409 and r.json == {"detail": "la clase no está lista ni en error (estado=grabando)"}, r)
    r = s.pedir("POST", f"/api/sesiones/{uuid.uuid4()}/regenerar", json_={})
    verificar("regenerar una clase que no existe -> 404", r.status == 404 and r.json == {"detail": "sesión no encontrada"}, r)

    # --- una clase en error (simulada en su sesion.json)
    mala = nueva_sesion()
    crear(s, mala)
    previo = str(uuid.uuid4())
    adjunto(s, mala["id"], {"id": previo}, ("Guía.pdf", m["pdf"], "application/pdf"))
    subir_y_finalizar(s, mala["id"], audio)
    s.parar()
    ruta = s.datos / "sesiones" / mala["id"] / "sesion.json"
    meta = json.loads(ruta.read_text("utf-8"))
    escribir_json(ruta, {**meta, "error_simulado": "falló el apunte (simulado)"})
    s.arrancar()
    v = s.pedir("GET", f"/api/sesiones/{mala['id']}").json
    verificar("clase en error: su adjunto queda sin usar (regenerable)",
              v["estado"] == "error" and v["error"] == "falló el apunte (simulado)"
              and [a["estado"] for a in v["adjuntos"]] == ["sin_usar"] and v["adjuntos_sin_usar"] == 1, v)
    r = adjunto(s, mala["id"], {"id": str(uuid.uuid4())}, ("Otra.pdf", m["pdf"], "application/pdf"))
    verificar("adjunto nuevo en una clase en error -> 201, sin usar (se suma al regenerar)",
              r.status == 201 and r.json["estado"] == "sin_usar" and r.json["sesion"]["adjuntos_sin_usar"] == 2, r)
    r = adjunto(s, mala["id"], {"id": previo}, ("Guía.pdf", m["pdf"], "application/pdf"))
    verificar("reenvío de uno que ya estaba, en error -> 200", r.status == 200 and r.json["nombre"] == "Guia.pdf", r)
    r = adjunto(s, mala["id"], {"id": previo})
    verificar("consulta sin archivo de uno que ya estaba, en error -> 200", r.status == 200 and r.json["id"] == previo, r)
    r = adjunto(s, mala["id"], {"id": str(uuid.uuid4())})
    verificar("consulta sin archivo de uno nuevo, en error -> 422",
              r.status == 422 and r.json == {"detail": "falta el archivo (campo archivo)"}, r)
    r = s.pedir("POST", f"/api/sesiones/{mala['id']}/regenerar", json_={})
    verificar("regenerar desde error -> 200 en_cola sin el error",
              r.status == 200 and r.json["estado"] == "en_cola" and r.json["error"] is None
              and [a["estado"] for a in r.json["adjuntos"]] == ["pendiente", "pendiente"], r)
    v = seguir(s, mala["id"], {"lista"}, limite=15)[3]
    verificar("la regenerada desde error termina lista con los dos adjuntos usados",
              v["estado"] == "lista" and [a["estado"] for a in v["adjuntos"]] == ["usado", "usado"], v)
    verificar("tras reiniciar: los adjuntos siguen", estados_adjuntos(s, sid) == (["usado"] * 10, 0))
    verificar("servidor.log registra adjuntos y regeneraciones",
              lineas_log(s.datos, "adjunto Slides_clase_5_version_final.pdf (pdf") == 3
              and lineas_log(s.datos, "repetido, consulta sin archivo") == 2
              and lineas_log(s.datos, "regenerar desde lista") == 1
              and lineas_log(s.datos, "regenerar desde error") == 1)
    s.parar()


def sin_adjuntos(tmp: Path, servidores: list):
    s = Servidor(tmp / "datos-sin-adjuntos", "--sin-adjuntos")
    servidores.append(s)
    s.arrancar()
    sesion = nueva_sesion()
    v = crear(s, sesion)
    verificar("--sin-adjuntos: las sesiones no traen los campos de adjuntos",
              v and set(v) == CLAVES_SESION - {"adjuntos", "adjuntos_sin_usar"}, v)
    pdf = muestras(random.Random(3))["pdf"]
    for sid, nombre in [(sesion["id"], "una clase que existe"), (str(uuid.uuid4()), "una que no existe")]:
        r = adjunto(s, sid, {"id": str(uuid.uuid4())}, ("Slides.pdf", pdf, "application/pdf"))
        verificar(f"--sin-adjuntos: POST /adjuntos de {nombre} -> 404 Not Found",
                  r.status == 404 and r.json == {"detail": "Not Found"}, r)
    r = adjunto(s, sesion["id"], {"id": str(uuid.uuid4())}, ("Grande.pdf", pdf + os.urandom(3 << 20), "application/pdf"))
    verificar("--sin-adjuntos: uno grande también (lee el cuerpo y lo tira)",
              r.status == 404 and r.json == {"detail": "Not Found"}, r)
    r = adjunto(s, sesion["id"], {"id": str(uuid.uuid4())})
    verificar("--sin-adjuntos: la consulta sin archivo (unos bytes) -> 404 Not Found",
              r.status == 404 and r.json == {"detail": "Not Found"}, r)
    r = s.pedir("POST", f"/api/sesiones/{sesion['id']}/regenerar", json_={})
    verificar("--sin-adjuntos: POST /regenerar -> 404 Not Found", r.status == 404 and r.json == {"detail": "Not Found"}, r)
    verificar("--sin-adjuntos: no guardó nada", not (s.datos / "sesiones" / sesion["id"] / "adjuntos").exists())
    s.parar()


# ----------------------------------------------------------------------------- importadas

def importadas(tmp: Path, servidores: list):
    """Clases importadas desde la app: campos nuevos, fotos sin minuto, duración null y el tope."""
    s = Servidor(tmp / "datos-importadas", "--max-audio-importada-mb", "1", "--procesar-seg", "1")
    servidores.append(s)
    s.arrancar()
    tope = 1 << 20
    creado = "2026-10-24T18:00:00-03:00"
    sesion = nueva_sesion(materia_id="macro", numero_clase=4, importada=True,
                          audio_nombre="PTT-20261022-WA0003.opus", audio_bytes=4000)
    sid = sesion["id"]
    r = s.pedir("POST", "/api/sesiones", json_=sesion)
    verificar("importada: POST con importada, audio_nombre y audio_bytes -> 201", r.status == 201, r)
    v = s.pedir("GET", f"/api/sesiones/{sid}").json
    verificar("importada: GET trae importada=true", v and v["importada"] is True and set(v) == CLAVES_SESION, v)
    meta = json.loads((s.datos / "sesiones" / sid / "sesion.json").read_text("utf-8"))
    verificar("importada: guarda audio_nombre y audio_bytes",
              meta.get("audio_nombre") == "PTT-20261022-WA0003.opus" and meta.get("audio_bytes") == 4000, meta)
    normal = nueva_sesion(audio_nombre="no-cuenta.m4a", audio_bytes=10 * tope)
    v = crear(s, normal)
    verificar("una grabada: importada=false y los campos nuevos no cuentan (ni el tope)", v and v["importada"] is False
              and "audio_nombre" not in json.loads((s.datos / "sesiones" / normal["id"] / "sesion.json").read_text("utf-8")), v)

    fid = str(uuid.uuid4())
    r = evento(s, sid, {"id": fid, "tipo": "foto", "creado": creado}, (f"{fid}.jpg", os.urandom(3000)))
    verificar("importada: una foto sin offset_ms -> 201", r.status == 201, r)
    guardado = json.loads((s.datos / "sesiones" / sid / "eventos" / f"{fid}.json").read_text("utf-8"))
    verificar("la foto queda sin hora (offset_ms null)", guardado["offset_ms"] is None and guardado["bytes"] == 3000, guardado)
    r = evento(s, sid, {"id": str(uuid.uuid4()), "tipo": "nota", "texto": "sin minuto", "creado": creado})
    verificar("una nota sin offset_ms sigue rechazada (400)", r.status == 400, r)

    audio = os.urandom(4000)
    r = s.audio(sid, 0, audio)
    verificar("importada: PUT de audio dentro del tope", r.status == 200 and r.json == {"bytes_recibidos": 4000}, r)
    r = s.pedir("POST", f"/api/sesiones/{sid}/finalizar", json_={
        "sha256": hashlib.sha256(audio).hexdigest(), "bytes_totales": len(audio), "duracion_ms": None,
        "eventos_totales": 1})
    verificar("finalizar con duracion_ms null -> 200 en_cola", r.status == 200 and r.json == {"estado": "en_cola"}, r)
    r = s.pedir("POST", f"/api/sesiones/{normal['id']}/finalizar", json_={
        "sha256": hashlib.sha256(b"").hexdigest(), "bytes_totales": 0, "eventos_totales": 0})
    verificar("finalizar sin duracion_ms (el campo falta) sigue siendo 400", r.status == 400, r)
    vistos, _, _, ultimo = seguir(s, sid, {"lista", "error"})
    verificar("importada: el pipeline simulado llega a lista con la duración en null",
              ultimo["estado"] == "lista" and ultimo["duracion_ms"] is None and ultimo["importada"] is True, (vistos, ultimo))
    pdf = s.pedir("GET", f"/api/sesiones/{sid}/apunte.pdf").datos
    verificar("el PDF de juguete muestra la foto sin hora y la duración sin medir",
              xref_ok(pdf) and b"sin hora" in pdf and b"sin medir" in pdf, pdf[:200])

    grande = nueva_sesion(importada=True, audio_nombre="VID-20261022-WA0001.mp4", audio_bytes=tope + 1)
    r = s.pedir("POST", "/api/sesiones", json_=grande)
    verificar("importada de más del tope: POST -> 413 sin crear nada",
              r.status == 413 and "2 GB" in r.json["detail"]
              and s.pedir("GET", f"/api/sesiones/{grande['id']}").status == 404, r)
    otra = nueva_sesion(importada=True, audio_nombre="clase.m4a")
    s.pedir("POST", "/api/sesiones", json_=otra)
    mitad = tope // 2 + 10
    r1 = s.audio(otra["id"], 0, os.urandom(mitad))
    r2 = s.audio(otra["id"], mitad, os.urandom(mitad))
    verificar("importada sin audio_bytes: el PUT que pasa el tope -> 413 y no guarda nada",
              r1.status == 200 and r2.status == 413 and s.bytes_recibidos(otra["id"]) == mitad, (r1, r2))
    r = s.audio(normal["id"], 0, os.urandom(tope + 10))
    verificar("una grabada no tiene ese tope", r.status == 200, r)
    s.parar()


# ----------------------------------------------------------------------------- Ambos y revisión (§15)

def celu(servidor, sid, offset, datos):
    return servidor.pedir("PUT", f"/api/sesiones/{sid}/audio?offset={offset}&pista=celu", datos,
                          tipo="application/octet-stream")


def bloque_celu(datos: bytes, desfase=-12_345, **cambios) -> dict:
    return {"sha256": hashlib.sha256(datos).hexdigest(), "bytes_totales": len(datos),
            "duracion_ms": 61_000, "desfase_ms": desfase, **cambios}


def finalizar(servidor, sid, audio: bytes, celu_=None, eventos=0):
    cuerpo = {"sha256": hashlib.sha256(audio).hexdigest(), "bytes_totales": len(audio),
              "duracion_ms": 60_000, "eventos_totales": eventos}
    if celu_ is not None:
        cuerpo["celu"] = celu_
    return servidor.pedir("POST", f"/api/sesiones/{sid}/finalizar", json_=cuerpo)


def ambos_y_revision(tmp: Path, servidores: list):
    """La pista del celu (?pista=celu y el bloque celu de /finalizar) y la espera de la revisión."""
    s = Servidor(tmp / "datos-ambos", "--procesar-seg", "1", "--dudas", "2")
    servidores.append(s)
    s.arrancar()
    rnd = random.Random(15)
    principal, pista = rnd.randbytes(3000), rnd.randbytes(2500)
    sesion = nueva_sesion(dispositivo="galaxy-watch8-classic")
    sid = sesion["id"]
    crear(s, sesion)
    s.audio(sid, 0, principal)
    r = s.pedir("GET", f"/api/sesiones/{sid}/audio?pista=celu")
    verificar("GET ?pista=celu de una pista que no empezó -> 0 (no lo del principal, y sin repetir pista)",
              r.status == 200 and r.json == {"bytes_recibidos": 0}, r)
    r = celu(s, sid, 0, pista[:2000])
    verificar("PUT ?pista=celu agrega a la pista del celu", r.status == 200 and r.json == {"bytes_recibidos": 2000}, r)
    r = celu(s, sid, 100, pista[2000:])
    verificar("PUT ?pista=celu con otro offset -> 409 con lo de esa pista",
              r.status == 409 and r.json == {"bytes_recibidos": 2000}, r)
    verificar("las pistas no se mezclan",
              s.bytes_recibidos(sid) == 3000
              and s.pedir("GET", f"/api/sesiones/{sid}/audio?pista=celu").json["bytes_recibidos"] == 2000)
    r = s.pedir("PUT", f"/api/sesiones/{sid}/audio?offset=0&pista=reloj", b"x", tipo="application/octet-stream")
    verificar("otra pista -> 422", r.status == 422, r)

    r = finalizar(s, sid, principal, bloque_celu(pista))
    verificar("finalizar con la pista del celu incompleta -> 409 faltan_bytes con pista celu",
              r.status == 409 and r.json == {"motivo": "faltan_bytes", "bytes_recibidos": 2000, "pista": "celu"}, r)
    celu(s, sid, 2000, pista[2000:])
    r = finalizar(s, sid, principal, bloque_celu(pista, sha256="0" * 64))
    verificar("finalizar con otro hash de la pista -> 409 hash_distinto con pista celu, que vuelve a 0",
              r.status == 409 and r.json == {"motivo": "hash_distinto", "bytes_recibidos": 0, "pista": "celu"}
              and s.bytes_recibidos(sid) == 3000, r)
    r = finalizar(s, sid, principal, {**bloque_celu(pista), "desfase_ms": 1.5})
    verificar("desfase_ms que no es entero -> 400", r.status == 400, r)
    r = finalizar(s, sid, principal, {k: v for k, v in bloque_celu(pista).items() if k != "duracion_ms"})
    verificar("bloque celu sin duracion_ms -> 400", r.status == 400, r)
    celu(s, sid, 0, pista)
    r = finalizar(s, sid, principal, bloque_celu(pista))
    verificar("finalizar con el bloque celu (desfase negativo) -> 200", r.status == 200 and r.json == {"estado": "en_cola"}, r)
    meta = json.loads((s.datos / "sesiones" / sid / "sesion.json").read_text("utf-8"))
    verificar("guarda la pista: audio.celu.ogg y el bloque en la finalización",
              (s.datos / "sesiones" / sid / "audio.celu.ogg").read_bytes() == pista
              and meta["finalizacion"]["celu"] == bloque_celu(pista) and meta.get("dudas") == 2, meta)
    r = celu(s, sid, 2500, b"mas")
    verificar("PUT ?pista=celu después de finalizar -> 409", r.status == 409 and r.json == {"bytes_recibidos": 2500}, r)

    vistos, _, _, ultimo = seguir(s, sid, {"esperando_usuario", "lista"}, limite=15)
    verificar("con --dudas, la clase espera la revisión (espera=revision, dudas=2, sin error)",
              vistos[-1] == "esperando_usuario" and ultimo["espera"] == "revision" and ultimo["dudas"] == 2
              and ultimo["etapa"] == "revisar_transcript" and ultimo["error"] is None
              and set(ultimo) == CLAVES_SESION | {"espera", "dudas"}, (vistos, ultimo))
    time.sleep(0.5)
    verificar("y se queda esperando", s.pedir("GET", f"/api/sesiones/{sid}").json["estado"] == "esperando_usuario")
    r = s.pedir("POST", f"/api/sesiones/{sid}/revision", json_={"accion": "otra"})
    verificar("POST /revision con otra acción -> 422", r.status == 422, r)
    r = s.pedir("POST", f"/api/sesiones/{sid}/revision", json_={"accion": "listo"})
    verificar("POST /revision listo -> 200 y sigue el apunte",
              r.status == 200 and r.json["estado"] == "en_cola" and "espera" not in r.json, r)
    vistos, etapas, _, ultimo = seguir(s, sid, {"lista"}, limite=15)
    verificar("después de la revisión: apunte, publicar y lista", ultimo["estado"] == "lista"
              and set(etapas) <= {"apunte", "publicar"}, (vistos, etapas))
    r = s.pedir("POST", f"/api/sesiones/{sid}/revision", json_={"accion": "listo"})
    verificar("POST /revision de una que no espera -> 409", r.status == 409, r)
    pdf = s.pedir("GET", f"/api/sesiones/{sid}/apunte.pdf").datos
    verificar("el PDF de juguete muestra la pista del celu", xref_ok(pdf) and b"Pista del celu: 2500 bytes" in pdf
              and b"desfase -12.345 s" in pdf, pdf[:300])

    # Auto: primero la materia, después la revisión.
    auto = sesion_auto()
    crear(s, auto)
    subir_y_finalizar(s, auto["id"], b"a" * 100)
    _, _, _, ultimo = seguir(s, auto["id"], {"esperando_usuario"}, limite=15)
    verificar("Auto con --dudas: primero espera la materia", ultimo["espera"] == "materia" and "dudas" not in ultimo, ultimo)
    s.pedir("POST", f"/api/sesiones/{auto['id']}/materia", json_={"materia_id": "macro"})
    vistos, _, _, ultimo = seguir(s, auto["id"], {"esperando_usuario", "lista"}, limite=10)
    verificar("y después de elegirla, la revisión", vistos[0] == "en_cola" and ultimo["estado"] == "esperando_usuario"
              and ultimo["espera"] == "revision" and ultimo["materia_id"] == "macro", (vistos, ultimo))
    r = s.pedir("POST", f"/api/sesiones/{auto['id']}/revision", json_={"accion": "sin_revisar"})
    verificar("generar sin revisar -> 200", r.status == 200, r)
    verificar("guarda cómo se resolvió", json.loads((s.datos / "sesiones" / auto["id"] / "sesion.json")
                                                     .read_text("utf-8")).get("revision") == "sin_revisar")

    # Pista sin bloque: se acepta y se ignora (queda en el log).
    sin_bloque = nueva_sesion()
    crear(s, sin_bloque)
    s.audio(sin_bloque["id"], 0, principal)
    celu(s, sin_bloque["id"], 0, pista)
    r = finalizar(s, sin_bloque["id"], principal)
    verificar("finalizar sin el bloque aunque llegó la pista -> 200, y el log lo dice",
              r.status == 200 and lineas_log(s.datos, "sin el bloque celu: se ignoran") == 1, r)
    s.parar()

    # --revision-seg: se resuelve sola.
    s = Servidor(tmp / "datos-revision-sola", "--procesar-seg", "1", "--dudas", "1", "--revision-seg", "1")
    servidores.append(s)
    s.arrancar()
    sola = nueva_sesion()
    crear(s, sola)
    subir_y_finalizar(s, sola["id"], b"b" * 100)
    vistos, _, _, ultimo = seguir(s, sola["id"], {"lista"}, limite=20, pausa=0.1)
    verificar("--revision-seg: espera la revisión y sigue sola hasta lista",
              "esperando_usuario" in vistos and ultimo["estado"] == "lista", vistos)
    s.parar()


def sin_pista(tmp: Path, servidores: list):
    """Un server de antes de Ambos: la app tiene que poder distinguirlo con el GET (y no subirle la pista)."""
    s = Servidor(tmp / "datos-sin-pista", "--sin-pista", "--procesar-seg", "1")
    servidores.append(s)
    s.arrancar()
    sesion = nueva_sesion()
    sid = sesion["id"]
    crear(s, sesion)
    s.audio(sid, 0, b"p" * 1000)
    r = s.pedir("GET", f"/api/sesiones/{sid}/audio?pista=celu")
    verificar("--sin-pista: GET ?pista=celu contesta lo del principal (así la app lo reconoce)",
              r.status == 200 and r.json == {"bytes_recibidos": 1000}, r)
    r = celu(s, sid, 1000, b"c" * 500)
    verificar("--sin-pista: un PUT ?pista=celu se pega al principal (lo que la app nunca tiene que hacer)",
              r.status == 200 and s.bytes_recibidos(sid) == 1500 and lineas_log(s.datos, "IGNORADA: server viejo") >= 2, r)
    audio = (s.datos / "sesiones" / sid / "audio.ogg").read_bytes()
    r = finalizar(s, sid, audio, bloque_celu(b"c" * 500))
    verificar("--sin-pista: ignora el bloque celu", r.status == 200 and "celu" not in json.loads(
        (s.datos / "sesiones" / sid / "sesion.json").read_text("utf-8"))["finalizacion"], r)
    auto = sesion_auto()
    crear(s, auto)
    subir_y_finalizar(s, auto["id"], b"a" * 10)
    _, _, _, ultimo = seguir(s, auto["id"], {"esperando_usuario"}, limite=15)
    verificar("--sin-pista: esperando_usuario sin espera (como antes)",
              ultimo["estado"] == "esperando_usuario" and set(ultimo) == CLAVES_SESION, ultimo)
    r = s.pedir("POST", f"/api/sesiones/{auto['id']}/revision", json_={"accion": "listo"})
    verificar("--sin-pista: sin /revision (404 Not Found)", r.status == 404 and r.json == {"detail": "Not Found"}, r)
    s.parar()


def main():
    conservar = "--conservar" in sys.argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    tmp = Path(tempfile.mkdtemp(prefix="classmer-falso-"))
    servidores: list[Servidor] = []
    try:
        flujo_principal(tmp, servidores)
        numeracion_y_materias(tmp, servidores)
        esperando_usuario(tmp, servidores)
        rechazos_y_token(tmp, servidores)
        materias_explicitas_y_compat(tmp, servidores)
        fallas_simuladas(tmp, servidores)
        adjuntos(tmp, servidores)
        sin_adjuntos(tmp, servidores)
        importadas(tmp, servidores)
        ambos_y_revision(tmp, servidores)
        sin_pista(tmp, servidores)
    except Exception:
        traceback.print_exc()
        verificar("la autoprueba terminó sin excepciones", False, "ver traceback")
    finally:
        for servidor in servidores:
            servidor.parar()
    fallidos = [nombre for nombre, ok in resultados if not ok]
    print(f"\n{len(resultados) - len(fallidos)}/{len(resultados)} pasos OK")
    if fallidos or conservar:
        print(f"Datos y salida de los servidores en: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if fallidos else 0)


if __name__ == "__main__":
    main()
