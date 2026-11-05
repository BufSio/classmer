#!/usr/bin/env python3
"""Servidor falso de Classmer (API v1) para probar la app de Android.

Implementa el contrato de ARCHITECTURE.md §5.1 sin pipeline real: guarda en disco lo que sube el
celu, simula las etapas del procesamiento con el reloj y genera un PDF de juguete.
Sólo usa la biblioteca estándar de Python (3.12 o más nuevo).

Uso
---
    python servidor_falso.py                     # puerto 8000, token classmer-dev
    python servidor_falso.py --puerto 8000 --token classmer-dev --procesar-seg 20
    python servidor_falso.py --fallar-prob 0.2 --cortar-prob 0.1 --latencia-ms 300   # red mala
    python servidor_falso.py --parciales         # guarda también los cuerpos de audio cortados
    python servidor_falso.py --cualquier-token   # acepta cualquier token Bearer no vacío
    python servidor_falso.py --rechazar-materia 409   # POST /materia siempre 409 (o 422)
    python servidor_falso.py --materias otras.json    # otro archivo de materias
    python servidor_falso.py --sin-adjuntos      # server viejo: sin /adjuntos ni /regenerar
    python servidor_falso.py --max-adjunto-mb 1  # tope de adjuntos más chico (para ver el 413)
    python servidor_falso.py --max-audio-importada-mb 5  # tope del audio importado (para ver el 413)
    python servidor_falso.py --dudas 3 --revision-seg 30  # el apunte espera revisar 3 dudas (30 s)
    python servidor_falso.py --sin-pista         # server de antes de Ambos: ignora pista y el bloque celu
    python probar_servidor_falso.py              # autoprueba de todo el flujo

--cualquier-token sirve para apuntar el celu al falso sin cambiarle el token que usa con el server
real (sin header Authorization, o vacío, sigue dando 401). --rechazar-materia hace que
POST /api/sesiones/{id}/materia conteste siempre ese código sin cambiar nada (si la sesión existe),
para probar cómo lo muestra la app.

Conectar el celu (misma red Wi-Fi que la PC):
  1. En la PC corré `ipconfig` y buscá la "Dirección IPv4" del adaptador Wi-Fi o Ethernet
     (por ejemplo 192.168.1.10). Al arrancar, el servidor también muestra la IP que detecta.
  2. En la app, Ajustes -> URL del server: http://<IP>:<puerto> (ej. http://192.168.1.10:8000)
     y el mismo token que se pasó en --token.
  3. La primera vez, el Firewall de Windows puede preguntar si deja a Python usar la red:
     permitilo en redes privadas. Si no preguntó y el celu no llega, revisá que la red Wi-Fi
     esté marcada como privada en Windows.
  4. Prueba rápida desde el navegador del celu: http://<IP>:<puerto>/api/health. Un 401 (sin
     token) ya demuestra que el celu llega a la PC.
  5. Es HTTP sin TLS: la app tiene que permitir tráfico en claro hacia esa IP
     (usesCleartextTraffic o network_security_config); si no, Android bloquea la conexión.

Estado en disco (sobrevive reinicios, para probar que el celu retoma las subidas):
    <datos>/servidor.log
    <datos>/materias.json   (si no se pasa --materias; se crea la primera vez)
    <datos>/sesiones/<id>/sesion.json  audio.ogg  eventos/<evento_id>.json  fotos/<archivo>
Para empezar de cero, borrá la carpeta de datos.

Materias
--------
Salen de --materias PATH o, si no se pasa, de <datos>/materias.json, que se crea con las de
ARCHITECTURE.md §9. Es una lista JSON; cada materia tiene "id", "nombre", "calendario" (o null),
"tipos_clase" y además, opcionales:
    "activa": true/false            (por defecto true)
    "numero_clase_inicial": entero  (por defecto 1)
Se puede editar con el servidor andando: antes de cada pedido se fija si el archivo cambió y lo
vuelve a leer. Para desactivar una materia poné "activa": false: deja de salir en
GET /api/materias, pero se sigue aceptando al crear sesiones, al asignarla con POST /materia y en
PATCH (así una lista vieja en el celu no hace perder una clase). Sólo una materia que no está en
el archivo da 404/422. Si el JSON queda roto, el servidor sigue con las materias anteriores y lo
avisa una vez en el log.
proximo_numero = numero_clase_inicial + sesiones de esa materia que no terminaron en error.
PATCH /api/materias/{id} reescribe numero_clase_inicial en el archivo (sin tocar lo demás).

Pipeline simulado (con el reloj, sin hacer nada de verdad)
----------------------------------------------------------
- Con materia: en_cola 3 s -> procesando (verificar ... publicar) durante --procesar-seg -> lista.
- Auto (materia null): en_cola 3 s -> procesando hasta clasificar durante la mitad de
  --procesar-seg -> esperando_usuario (el clasificador falso nunca se anima). Se resuelve con
  POST /api/sesiones/{id}/materia: en_cola 3 s -> procesando (apunte, publicar) la otra mitad ->
  lista. Si la materia se elige antes (grabando, en_cola o procesando), sigue el camino normal.
- POST /api/sesiones/{id}/materia se acepta hasta que empieza el apunte: 409 si la sesión está
  lista o en error, o procesando en las etapas apunte o publicar. Si la materia cambia, la sesión
  toma el proximo_numero de la nueva; si es la misma, o era Auto y ya traía numero_clase,
  conserva su número.

Adjuntos (PDF de slides o notas, imágenes)
------------------------------------------
- POST /api/sesiones/{id}/adjuntos (multipart: id, archivo, offset_ms y creado opcionales): guarda
  el archivo en <datos>/sesiones/<id>/adjuntos/<nombre>, con el nombre como el server real (ASCII,
  sin puntos internos, único por clase). El tipo sale del contenido (415 si no es PDF, JPEG, PNG,
  WebP ni HEIC), 413 si pasa --max-adjunto-mb, idempotente por id (200), 422 si falta id o archivo,
  el id no es UUID, offset_ms o creado no sirven, el archivo está vacío o el id ya es de otra clase
  o de un evento. Se acepta en cualquier estado de la clase, también en error (para sumar material
  antes de regenerar). Responde el adjunto con "sesion".
- Sin la parte "archivo" (la consulta de unos bytes que hace la app antes de mandarlo), como el
  server real: 200 con lo guardado si ese id ya está en la clase, 422 "falta el archivo" si no, y
  404 "sesión no encontrada" si no existe la clase.
- Estado de cada adjunto en GET /api/sesiones: "pendiente" si el próximo apunte lo va a usar,
  "usado" con la clase lista, "sin_usar" si llegó con el apunte ya escrito o escribiéndose (y todos,
  con la clase en error). adjuntos_sin_usar los cuenta.
- POST /api/sesiones/{id}/regenerar: con la clase lista o en error, vuelve a en_cola desde el apunte
  (la mitad de --procesar-seg) y el apunte nuevo usa todos los adjuntos; si no, 409.
- Una clase en error: poné "error_simulado": "<motivo>" en su sesion.json y reiniciá el falso.
- --sin-adjuntos simula un server viejo: esas dos rutas dan 404 {"detail": "Not Found"} y las
  sesiones no traen los campos de adjuntos.

Clases importadas (el audio que mandó un amigo, cargado desde la app)
--------------------------------------------------------------------
- POST /api/sesiones acepta "importada" (bool), "audio_nombre" y "audio_bytes" (opcionales; sólo
  cuentan con "importada": true). Con audio_bytes de más de --max-audio-importada-mb (2048, los
  2 GiB del server real) responde 413 sin crear nada. GET /api/sesiones trae "importada".
- PUT /audio de una importada: si offset + largo pasa ese tope, 413 (una grabada no cambia).
- POST /eventos: una foto puede venir sin offset_ms (las de una importada, que el server real
  ubica por su EXIF); queda "sin hora". En el PDF de juguete van al final.
- POST /finalizar: duracion_ms puede ser null (el celu no la pudo medir).
- El audio se guarda como audio.ogg igual que el de una grabada (como un server viejo).

Grabar con los dos (Ambos) y la revisión del transcript (ARCHITECTURE.md §15)
-------------------------------------------------------------------------------
- GET y PUT /api/sesiones/{id}/audio?pista=celu: la segunda pista, con el mismo esquema reanudable que
  el principal (409 con bytes_recibidos, 409 si ya se finalizó). Se guarda en audio.celu.ogg. Otra
  pista da 422. El server real repite "pista": "celu" en el GET (desde el 27/10); este no, a propósito,
  para probar que la app también distingue un server viejo comparando con el principal.
- POST /finalizar acepta "celu": {"sha256", "bytes_totales", "duracion_ms", "desfase_ms"} (desfase_ms
  puede ser negativo). Los 409 de esa pista traen "pista": "celu" (faltan_bytes, y hash_distinto, que
  vacía audio.celu.ogg). Orden: bytes del principal, bytes del celu, eventos, hash del principal, hash
  del celu. Si llegó la pista pero no el bloque, se acepta y se ignora la pista (queda en el log).
- --dudas N: las clases con materia pasan por revisar_transcript y quedan en esperando_usuario con
  "espera": "revision" y "dudas": N (las de Auto, después de elegir la materia). La espera de la materia
  trae "espera": "materia". Se resuelve con POST /api/sesiones/{id}/revision {"accion": "listo"} o
  {"accion": "sin_revisar"} (ruta sólo del falso: en el server real lo hace el panel), o sola a los
  --revision-seg segundos. Después sigue el apunte.
- --sin-pista simula un server de antes de §15: ignora "pista" (el PUT de la pista del celu se pegaría
  al audio principal, y el log lo marca "pista IGNORADA"), ignora el bloque "celu", no manda "espera"
  ni "dudas" y no tiene /revision (ni --dudas).
"""

import argparse
import copy
import email.parser
import email.policy
import hashlib
import hmac
import json
import os
import random
import re
import socket
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

VERSION = "falso-0.2"
SEG_EN_COLA = 3.0
ETAPAS = ["verificar", "segmentar", "transcribir", "linea_de_tiempo", "apunte", "publicar"]
ETAPAS_AUTO = ["verificar", "segmentar", "transcribir", "linea_de_tiempo", "clasificar"]
ETAPAS_TRAS_ASIGNAR = ["apunte", "publicar"]
ETAPAS_REVISION = ["verificar", "segmentar", "transcribir", "linea_de_tiempo", "revisar_transcript"]
ESPERA_REVISION = ("esperando_usuario", "revisar_transcript", None)
PISTAS = {"celu"}
# POST /materia: se acepta hasta que arranca el apunte (procesando en apunte o publicar -> 409).
ESTADOS_ASIGNABLES = {"esperando_usuario", "grabando", "en_cola", "procesando"}
TIPOS_EVENTO = {"foto", "nota", "marca", "pausa", "reanudar"}
MARCAS = {"importante", "no_entendi"}
ORIGENES = {"calendario", "manual", "auto"}
TIPOS_CLASE = ["teorica", "practica"]
RE_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
RE_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
RE_ENTERO = re.compile(r"[0-9]+")
MAX_EVENTOS_PDF = 36
# Adjuntos: tipo -> (mime, extensión), como el server real.
TIPOS_ADJUNTO = {"pdf": ("application/pdf", ".pdf"), "jpeg": ("image/jpeg", ".jpg"),
                 "png": ("image/png", ".png"), "webp": ("image/webp", ".webp"),
                 "heic": ("image/heic", ".heic")}
MARCAS_HEIC = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
MARGEN_MULTIPART = 64 * 1024  # lo que suman los otros campos y encabezados del multipart
MAX_ADJUNTOS_PDF = 12

# Las de ARCHITECTURE.md §9. "macro" excluye las clases particulares ("Clase particular - ...") y
# "riesgo" no tiene regla de calendario (sólo se elige a mano). "teoria-juegos" es del primer
# cuatrimestre: está inactiva, como en la config real.
MATERIAS_POR_DEFECTO = [
    {"id": "analisis", "nombre": "Análisis Matemático II",
     "calendario": {"calendario": None, "titulo_regex": "An[aá]lisis Matem[aá]tico"},
     "tipos_clase": TIPOS_CLASE, "activa": True, "numero_clase_inicial": 1},
    {"id": "teoria-juegos", "nombre": "Teoría de Juegos",
     "calendario": {"calendario": None, "titulo_regex": "Juegos"},
     "tipos_clase": TIPOS_CLASE, "activa": False, "numero_clase_inicial": 1},
    {"id": "macro", "nombre": "Macroeconomía I",
     "calendario": {"calendario": None, "titulo_regex": "^(?!Clase particular).*Macro"},
     "tipos_clase": TIPOS_CLASE, "activa": True, "numero_clase_inicial": 1},
    {"id": "riesgo", "nombre": "Riesgo Financiero",
     "calendario": None,
     "tipos_clase": TIPOS_CLASE, "activa": True, "numero_clase_inicial": 1},
]


# ----------------------------------------------------------------------------- utilidades

def ahora_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def escribir_json(ruta: Path, obj) -> None:
    """Escritura atómica: archivo temporal + renombrar."""
    tmp = ruta.with_name(ruta.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ruta)


def es_uno_de(valor, opciones) -> bool:
    return isinstance(valor, str) and valor in opciones


def fecha_con_zona(valor) -> bool:
    if not isinstance(valor, str):
        return False
    try:
        return datetime.fromisoformat(valor).tzinfo is not None
    except ValueError:
        return False


def mmss(ms: int | None) -> str:
    if ms is None:
        return "sin hora"
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def hms(ms: int | None) -> str:
    if ms is None:
        return "sin medir"
    s = ms // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def sha256_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    if ruta.exists():
        with open(ruta, "rb") as f:
            while bloque := f.read(1 << 20):
                h.update(bloque)
    return h.hexdigest()


def nombre_seguro(nombre: str) -> str:
    """Nombre de archivo sin rutas ni caracteres raros (sirve en Windows)."""
    nombre = re.sub(r"[^A-Za-z0-9._-]", "_", nombre.replace("\\", "/").rsplit("/", 1)[-1])
    return nombre.lstrip(".")[:100]


def ip_lan() -> str:
    """IP de la PC en la LAN (el connect UDP no manda paquetes)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "<IP de la PC>"


def parsear_multipart(content_type: str, cuerpo: bytes) -> dict[str, tuple[str | None, bytes]]:
    """Devuelve {nombre del campo: (nombre de archivo o None, bytes)}."""
    if not content_type.lower().startswith("multipart/form-data"):
        raise error400("se esperaba multipart/form-data")
    cabecera = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    mensaje = email.parser.BytesParser(policy=email.policy.default).parsebytes(cabecera + cuerpo)
    if not mensaje.is_multipart():
        raise error400("multipart mal formado (¿falta el boundary?)")
    partes = {}
    for parte in mensaje.iter_parts():
        nombre = parte.get_param("name", header="content-disposition")
        if nombre:
            partes[nombre] = (parte.get_filename(), parte.get_payload(decode=True) or b"")
    return partes


def nombre_archivo_crudo(cuerpo: bytes, campo: str) -> str | None:
    """El filename de la parte `campo` tal como vino. El parser de email cambia por '?' lo que no
    es ASCII (OkHttp lo manda en UTF-8, sin codificar); también acepta filename*=UTF-8''..."""
    m = re.search(rb'Content-Disposition:[^\r\n]*\bname="' + re.escape(campo.encode()) + rb'"[^\r\n]*',
                  cuerpo, re.IGNORECASE)
    if not m:
        return None
    if f := re.search(rb"filename\*=UTF-8''([^;\r\n]+)", m[0], re.IGNORECASE):
        return unquote(f[1].decode("ascii", "replace"), encoding="utf-8", errors="replace")
    if f := re.search(rb'filename="([^"]*)"', m[0]):
        return f[1].decode("utf-8", errors="replace")
    return None


def tipo_adjunto(datos: bytes) -> str | None:
    """El tipo por el contenido (bytes mágicos), no por la extensión ni el Content-Type."""
    if datos[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if datos[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "webp"
    if datos[4:8] == b"ftyp" and datos[8:12] in MARCAS_HEIC:
        return "heic"
    if b"%PDF-" in datos[:1024]:
        return "pdf"
    return None


def nombre_adjunto(original: str, tipo: str, usados: set[str]) -> str:
    """Como el server real: el nombre original en ASCII (sin tildes; espacios y símbolos -> _; sin
    puntos internos; hasta 60 caracteres) más la extensión del tipo detectado. Único por clase sin
    distinguir mayúsculas en la raíz: Slides.pdf y Slides.jpg no conviven (el segundo es Slides-2.jpg).
    `usados` son las raíces que ya hay, en minúsculas."""
    raiz = original.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in raiz:
        raiz = raiz.rsplit(".", 1)[0]
    raiz = unicodedata.normalize("NFKD", raiz).encode("ascii", "ignore").decode("ascii")
    raiz = re.sub(r"[^A-Za-z0-9-]+", "_", raiz).strip("_-")[:60].strip("_-") or "adjunto"
    candidata, n = raiz, 1
    while candidata.lower() in usados:
        n += 1
        candidata = f"{raiz[:60 - len(str(n)) - 1]}-{n}"
    return candidata + TIPOS_ADJUNTO[tipo][1]


class ErrorHttp(Exception):
    def __init__(self, status: int, cuerpo: dict, resumen: str | None = None):
        super().__init__(resumen or json.dumps(cuerpo, ensure_ascii=False))
        self.status, self.cuerpo, self.resumen = status, cuerpo, resumen


def error400(detalle: str) -> ErrorHttp:
    return ErrorHttp(400, {"detail": detalle}, f"400 {detalle}")


def error422(detalle: str) -> ErrorHttp:
    return ErrorHttp(422, {"detail": detalle}, f"422 {detalle}")


def es_entero(valor) -> bool:
    """Entero de JSON de verdad (True/False son int en Python, pero no cuentan)."""
    return type(valor) is int


class Registro:
    """Escribe cada línea en la consola y en <datos>/servidor.log."""

    def __init__(self, ruta: Path):
        self.ruta, self.lock = ruta, threading.Lock()

    def __call__(self, mensaje: str) -> None:
        linea = f"{datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + " " + mensaje
        with self.lock:
            print(linea, flush=True)
            with open(self.ruta, "a", encoding="utf-8") as f:
                f.write(linea + "\n")


# ----------------------------------------------------------------------------- materias

def validar_materias(datos) -> str | None:
    """Motivo por el que la lista de materias no sirve, o None si está bien."""
    if not isinstance(datos, list):
        return "tiene que ser una lista de materias"
    vistos = set()
    for i, m in enumerate(datos, start=1):
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"]:
            return f"la materia #{i} no es un objeto con 'id' (texto)"
        mid = m["id"]
        if mid in vistos:
            return f"id repetido: {mid!r}"
        vistos.add(mid)
        if "nombre" in m and not isinstance(m["nombre"], str):
            return f"{mid}: 'nombre' tiene que ser texto"
        if m.get("calendario") is not None and not isinstance(m["calendario"], dict):
            return f"{mid}: 'calendario' tiene que ser un objeto o null"
        tipos = m.get("tipos_clase")
        if tipos is not None and not (isinstance(tipos, list) and tipos
                                      and all(isinstance(t, str) for t in tipos)):
            return f"{mid}: 'tipos_clase' tiene que ser una lista de textos"
        if "activa" in m and not isinstance(m["activa"], bool):
            return f"{mid}: 'activa' tiene que ser true o false"
        if "numero_clase_inicial" in m and not es_entero(m["numero_clase_inicial"]):
            return f"{mid}: 'numero_clase_inicial' tiene que ser un entero"
    return None


def esta_activa(materia: dict) -> bool:
    return materia.get("activa", True)


def numero_inicial(materia: dict) -> int:
    return materia.get("numero_clase_inicial", 1)


class Materias:
    """El archivo de materias, con recarga en caliente.

    Antes de usarlas se fija si cambió el archivo (mtime_ns o tamaño) y lo vuelve a leer. Si la
    versión nueva no sirve, sigue con las anteriores y lo avisa una sola vez por versión.
    Tiene su propio lock. Quien necesite también el del almacén lo toma ANTES que éste (orden fijo
    almacén -> materias), así dos hilos nunca se esperan mutuamente.
    """

    def __init__(self, ruta: Path, registro: Registro):
        self.ruta, self.registro = ruta, registro
        self.lock = threading.Lock()
        self.crudas: list[dict] = []  # tal cual el archivo, para reescribirlo sin perder campos
        self.marca = None             # (mtime_ns, tamaño) de la versión cargada
        self.marca_fallida = None     # versión inválida que ya se avisó

    def _marca_archivo(self) -> tuple:
        try:
            st = self.ruta.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return ("sin archivo",)

    def _leer(self) -> list[dict]:
        """Lee y valida el archivo. ValueError u OSError con el motivo si no sirve."""
        for intento in range(5):
            try:
                texto = self.ruta.read_text(encoding="utf-8")
                break
            except PermissionError:
                # En Windows un editor o el antivirus pueden tenerlo tomado un instante: si se
                # diera por inválida esta versión, quedaría ignorada hasta la próxima edición.
                if intento == 4:
                    raise
                time.sleep(0.02)
        datos = json.loads(texto)
        problema = validar_materias(datos)
        if problema:
            raise ValueError(problema)
        return datos

    def describir(self) -> str:
        activas = [m["id"] for m in self.crudas if esta_activa(m)]
        inactivas = [m["id"] for m in self.crudas if not esta_activa(m)]
        return (f"activas: {', '.join(activas) or '(ninguna)'}"
                + (f"; inactivas: {', '.join(inactivas)}" if inactivas else ""))

    def cargar(self) -> None:
        """Primera lectura, al arrancar: si el archivo no sirve se corta con el motivo."""
        marca = self._marca_archivo()  # antes de leer: si cambia en el medio, se relee después
        try:
            self.crudas = self._leer()
        except (OSError, ValueError) as e:
            sys.exit(f"{self.ruta}: {e}")
        self.marca = marca

    def _refrescar(self) -> None:
        """Con el lock tomado. Relee el archivo si cambió desde la última vez."""
        marca = self._marca_archivo()
        if marca == self.marca:
            # Volvió a ser la versión cargada (por ejemplo, restaurada de una copia que conserva
            # la fecha): ya no hay un archivo roto que proteger.
            self.marca_fallida = None
            return
        if marca == self.marca_fallida:
            return
        try:
            crudas = self._leer()
        except (OSError, ValueError) as e:
            self.marca_fallida = marca
            self.registro(f"AVISO: {self.ruta.name} inválido ({e}); sigo con las materias anteriores "
                          f"({self.describir()})")
            return
        self.crudas, self.marca, self.marca_fallida = crudas, marca, None
        self.registro(f"{self.ruta.name} recargado ({self.describir()})")

    def todas(self) -> list[dict]:
        with self.lock:
            self._refrescar()
            return self.crudas

    def activas(self) -> list[dict]:
        """Las activas, en el orden del archivo. No hay que modificar los dicts que devuelve."""
        with self.lock:
            self._refrescar()
            return [m for m in self.crudas if esta_activa(m)]

    def fijar_inicial(self, mid: str, valor: int) -> dict | None:
        """Cambia numero_clase_inicial de una materia (activa o no) y reescribe el archivo
        (atómico), conservando los demás campos y el orden. None si la materia no existe."""
        with self.lock:
            self._refrescar()
            if self.marca_fallida is not None:  # no pisar un archivo que alguien está editando
                raise ErrorHttp(503, {"detail": f"{self.ruta.name} tiene un error: arreglalo y "
                                                "volvé a intentar"},
                                f"503 {self.ruta.name} inválido, no lo piso")
            nuevas = copy.deepcopy(self.crudas)
            materia = next((m for m in nuevas if m["id"] == mid), None)
            if materia is None:
                return None
            materia["numero_clase_inicial"] = valor
            try:
                escribir_json(self.ruta, nuevas)
            except OSError as e:  # en Windows, por ejemplo, si otro programa lo tiene abierto
                self.ruta.with_name(self.ruta.name + ".tmp").unlink(missing_ok=True)
                raise ErrorHttp(503, {"detail": f"no pude guardar {self.ruta.name}: {e}"},
                                f"503 no pude guardar {self.ruta.name}: {e!r}") from None
            self.crudas, self.marca = nuevas, self._marca_archivo()  # así no se relee de gusto
            return materia


# ----------------------------------------------------------------------------- estado

def mensaje_esperando(sid: str, ids_activos: list[str]) -> str:
    """Como el `error` que deja el clasificador del server real cuando no se anima."""
    sugerida = f" Sugerida: {ids_activos[0]}." if ids_activos else ""
    return (f"No pude determinar la materia (confianza 0.30: servidor falso).{sugerida} "
            f"Asignala con: uv run classmer asignar {sid} --materia <materia> "
            f"(opciones: {', '.join(sorted(ids_activos))})")


def tramo(t: float, segundos: float, etapas: list[str], base: float, peso: float,
          final: tuple) -> tuple[str, str | None, float | None]:
    """en_cola SEG_EN_COLA s, después procesando por `etapas` durante `segundos`, y `final`.

    El progreso va de `base` a `base + peso` mientras procesa."""
    if t < SEG_EN_COLA:
        return "en_cola", None, base
    t -= SEG_EN_COLA
    if t < segundos:
        fraccion = t / segundos
        etapa = etapas[min(int(fraccion * len(etapas)), len(etapas) - 1)]
        # Truncado y no redondeado: con round() el último milisegundo daba 1.0 estando procesando.
        return "procesando", etapa, int((base + fraccion * peso) * 1000) / 1000
    return final


class Sesion:
    """Una clase grabada. Todo lo que la describe vive en su carpeta."""

    # False con --sin-adjuntos: las sesiones no traen los campos de adjuntos (como un server viejo).
    con_adjuntos = True
    # False con --sin-pista: esperando_usuario no trae "espera" (como un server de antes de §15).
    con_espera = True
    # --revision-seg: la revisión del transcript se resuelve sola a los N segundos (None: nunca).
    revision_seg: float | None = None

    def __init__(self, carpeta: Path, meta: dict):
        self.carpeta, self.meta = carpeta, meta
        self.ruta_audio = carpeta / "audio.ogg"
        self.bytes_recibidos = self.ruta_audio.stat().st_size if self.ruta_audio.exists() else 0
        # La pista del celu (Ambos): ?pista=celu.
        self.ruta_audio_celu = carpeta / "audio.celu.ogg"
        self.bytes_celu = self.ruta_audio_celu.stat().st_size if self.ruta_audio_celu.exists() else 0
        self.eventos: dict[str, dict] = {}
        (carpeta / "eventos").mkdir(parents=True, exist_ok=True)
        (carpeta / "fotos").mkdir(exist_ok=True)
        for ruta in sorted((carpeta / "eventos").glob("*.json")):
            evento = json.loads(ruta.read_text(encoding="utf-8"))
            self.eventos[evento["id"]] = evento

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def finalizacion(self) -> dict | None:
        return self.meta.get("finalizacion")

    def guardar(self) -> None:
        escribir_json(self.carpeta / "sesion.json", self.meta)

    def bytes_de(self, pista: str | None) -> int:
        return self.bytes_celu if pista == "celu" else self.bytes_recibidos

    def agregar_audio(self, datos: bytes, pista: str | None = None) -> None:
        with open(self.ruta_audio_celu if pista == "celu" else self.ruta_audio, "ab") as f:
            f.write(datos)
            if pista == "celu":
                self.bytes_celu = f.tell()
            else:
                self.bytes_recibidos = f.tell()

    def resetear_audio(self, pista: str | None = None) -> None:
        if pista == "celu":
            open(self.ruta_audio_celu, "wb").close()
            self.bytes_celu = 0
        else:
            open(self.ruta_audio, "wb").close()
            self.bytes_recibidos = 0

    @property
    def dudas(self) -> int:
        """Las dudas del transcript que simula --dudas (se fijan al finalizar)."""
        return self.meta.get("dudas") or 0

    def revisada_en(self, procesar_seg: float) -> float | None:
        """Cuándo se resolvió la revisión: con POST /revision, o sola a los --revision-seg segundos."""
        if self.meta.get("revisada_en") is not None:
            return self.meta["revisada_en"]
        if not self.dudas or Sesion.revision_seg is None:
            return None
        asignada_en = self.meta.get("asignada_en")
        if asignada_en is not None:
            espera = asignada_en + SEG_EN_COLA + procesar_seg / 4
        elif self.meta.get("materia_id") is not None:
            espera = self.finalizacion["finalizada_en"] + SEG_EN_COLA + procesar_seg / 2
        else:
            return None
        lista = espera + Sesion.revision_seg
        return lista if time.time() >= lista else None

    @property
    def numero_clase(self) -> int | None:
        return self.meta.get("numero_clase")  # las guardadas por falso-0.1 no lo tienen

    def pipeline(self, procesar_seg: float) -> tuple[str, str | None, float | None]:
        """(estado, etapa, progreso) simulados según el reloj."""
        fin = self.finalizacion
        if not fin:
            return "grabando", None, None
        ahora, asignada_en = time.time(), self.meta.get("asignada_en")
        regenerada_en = self.meta.get("regenerada_en")
        if regenerada_en is not None:
            # POST /regenerar: vuelve a escribir el apunte (con todos los adjuntos) desde la etapa apunte.
            return tramo(ahora - regenerada_en, procesar_seg / 2, ETAPAS_TRAS_ASIGNAR, 0.5, 0.5,
                         ("lista", None, 1.0))
        if self.meta.get("error_simulado"):
            return "error", None, None
        revisada_en = self.revisada_en(procesar_seg)
        if revisada_en is not None:
            # Revisaron las dudas del transcript (o se generó sin revisar): sigue el apunte.
            return tramo(ahora - revisada_en, procesar_seg / 2, ETAPAS_TRAS_ASIGNAR, 0.5, 0.5,
                         ("lista", None, 1.0))
        if asignada_en is not None:
            if self.dudas:
                # Con --dudas, la que eligieron la materia pasa por revisar_transcript y espera la revisión.
                return tramo(ahora - asignada_en, procesar_seg / 4, ["revisar_transcript"], 0.5, 0.0,
                             ESPERA_REVISION)
            # Auto que quedó en esperando_usuario y después se asignó: retoma desde el apunte
            # (re-encolada conserva el progreso, como en el server real).
            return tramo(ahora - asignada_en, procesar_seg / 2, ETAPAS_TRAS_ASIGNAR, 0.5, 0.5,
                         ("lista", None, 1.0))
        t = ahora - fin["finalizada_en"]
        if self.meta.get("materia_id") is None:
            # Auto: el clasificador falso nunca se anima y la sesión espera que elijan la materia.
            return tramo(t, procesar_seg / 2, ETAPAS_AUTO, 0.0, 0.5,
                         ("esperando_usuario", "clasificar", None))
        if self.dudas:
            # Con --dudas: el apunte espera que se revisen las dudas del transcript (§15).
            return tramo(t, procesar_seg / 2, ETAPAS_REVISION, 0.0, 0.5, ESPERA_REVISION)
        return tramo(t, procesar_seg, ETAPAS, 0.0, 1.0, ("lista", None, 1.0))

    def a_json(self, procesar_seg: float, ids_activos: list[str]) -> dict:
        """Objeto de GET /api/sesiones/{id}. `ids_activos` (en el orden del archivo) arma el
        mensaje de esperando_usuario."""
        estado, etapa, progreso = self.pipeline(procesar_seg)
        fin, numero = self.finalizacion, self.numero_clase
        titulo = None
        if estado == "lista":
            titulo = "Apunte de prueba" + (f" · Clase {numero}" if numero is not None else "")
        revision = estado == "esperando_usuario" and etapa == "revisar_transcript"
        datos = {
            "id": self.id, "materia_id": self.meta.get("materia_id"), "estado": estado,
            "etapa": etapa, "progreso": progreso, "bytes_recibidos": self.bytes_recibidos,
            "pdf": f"/api/sesiones/{self.id}/apunte.pdf" if estado == "lista" else None,
            "error": mensaje_esperando(self.id, ids_activos) if estado == "esperando_usuario" and not revision
            else None,
            # Informativos del contrato.
            "inicio": self.meta.get("inicio"), "tipo_clase": self.meta.get("tipo_clase"),
            "duracion_ms": fin["duracion_ms"] if fin else None,
            "numero_clase": numero, "titulo": titulo,
            "importada": bool(self.meta.get("importada")),
            # Extra (no está en el contrato, pero le sirve a la lista de la app).
            "eventos": len(self.eventos),
        }
        if estado == "error":
            datos["error"] = self.meta.get("error_simulado")
        if estado == "esperando_usuario" and Sesion.con_espera:
            # Qué espera: la materia (la de siempre) o la revisión de las dudas del transcript (§15).
            datos["espera"] = "revision" if revision else "materia"
            if revision:
                datos["dudas"] = self.dudas
        if Sesion.con_adjuntos:
            adjuntos = self.adjuntos_json(estado)
            datos["adjuntos"] = adjuntos
            datos["adjuntos_sin_usar"] = sum(a["estado"] == "sin_usar" for a in adjuntos)
        return datos

    def adjuntos_json(self, estado: str) -> list[dict]:
        """Los adjuntos como los informa la API, en orden de llegada. `usado_por` es la corrida del
        apunte que los incluye (None: llegaron con el apunte ya escrito o escribiéndose)."""
        salida = []
        for a in self.meta.get("adjuntos", []):
            if a.get("usado_por") is None or estado == "error":
                uso = "sin_usar"
            else:
                uso = "usado" if estado == "lista" else "pendiente"
            salida.append({"id": a["id"], "nombre": a["nombre"], "nombre_original": a["nombre_original"],
                           "tipo": a["tipo"], "bytes": a["bytes"], "offset_ms": a.get("offset_ms"),
                           "creado": a.get("creado"), "estado": uso})
        return salida


class Almacen:
    """Todas las sesiones en memoria, respaldadas en disco. Se toca siempre con el lock."""

    def __init__(self, carpeta: Path):
        self.carpeta = carpeta
        self.lock = threading.RLock()
        self.sesiones: dict[str, Sesion] = {}

    def cargar(self, registro: Registro) -> int:
        self.carpeta.mkdir(parents=True, exist_ok=True)
        for ruta in sorted(self.carpeta.glob("*/sesion.json")):
            try:
                sesion = Sesion(ruta.parent, json.loads(ruta.read_text(encoding="utf-8")))
                self.sesiones[sesion.id] = sesion
            except (OSError, ValueError, KeyError) as e:
                registro(f"AVISO: no pude cargar {ruta}: {e!r}")
        return len(self.sesiones)

    def crear(self, meta: dict) -> Sesion:
        sesion = Sesion(self.carpeta / meta["id"], meta)
        sesion.guardar()
        self.sesiones[sesion.id] = sesion
        return sesion

    def clases_de(self, materia_id: str, procesar_seg: float, sin: Sesion | None = None) -> int:
        """Sesiones de la materia que no terminaron en error (sin contar `sin`). Con el lock."""
        return sum(1 for s in self.sesiones.values()
                   if s is not sin and s.meta.get("materia_id") == materia_id
                   and s.pipeline(procesar_seg)[0] != "error")


def clave_inicio(sesion: Sesion) -> float:
    try:
        return datetime.fromisoformat(sesion.meta["inicio"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------------------------- PDF

def orden_evento(ev: dict) -> tuple:
    """Pausa y reanudar comparten offset_ms (lo pausado no cuenta): desempatar por creado y llegada.
    Las fotos sin offset_ms (de una importada) van al final."""
    sin_hora = ev.get("offset_ms") is None
    return sin_hora, ev.get("offset_ms") or 0, datetime.fromisoformat(ev["creado"]).timestamp(), ev.get("n", 0)


def describir_evento(ev: dict) -> str:
    match ev["tipo"]:
        case "foto":
            return f"FOTO fotos/{ev.get('archivo')}"
        case "nota":
            return f'NOTA "{ev.get("texto", "")}"'
        case "marca":
            return f"MARCA {ev.get('marca')}"
    return ev["tipo"].upper()


def texto_pdf(texto: str) -> str:
    """Cadena literal de PDF en WinAnsi: escapa ( ) \\ y pasa lo no-ASCII a octal."""
    salida = []
    for c in texto.encode("cp1252", errors="replace"):
        if c in b"()\\":
            salida.append("\\" + chr(c))
        elif 32 <= c < 127:
            salida.append(chr(c))
        else:
            salida.append(f"\\{c:03o}")
    return "".join(salida)


def generar_pdf(sesion: Sesion, nombre_materia: str) -> bytes:
    """PDF mínimo de una página (Helvetica, A4) con los datos de la sesión y sus eventos."""
    fin = sesion.finalizacion
    lineas = [
        ("F2", 16, "Apunte de prueba - Classmer (servidor falso)"),
        ("F1", 11, ""),
        ("F1", 11, f"Sesión: {sesion.id}"),
        ("F1", 11, f"Materia: {nombre_materia}" + (
            f"    Clase {sesion.numero_clase}" if sesion.numero_clase is not None else "")),
        ("F1", 11, f"Tipo de clase: {sesion.meta.get('tipo_clase')}    Inicio: {sesion.meta.get('inicio')}"),
        ("F1", 11, f"Duración: {hms(fin['duracion_ms'])}    Audio: {sesion.bytes_recibidos} bytes"),
        ("F1", 11, ""),
        ("F2", 13, f"Eventos ({len(sesion.eventos)})"),
    ]
    if fin.get("celu"):
        c = fin["celu"]
        lineas[-2:-2] = [("F1", 11, f"Pista del celu: {c['bytes_totales']} bytes, {hms(c['duracion_ms'])}, "
                                     f"desfase {c['desfase_ms'] / 1000:+.3f} s")]
    # Los adjuntos que usó este apunte, antes de los eventos (le quitan lugar a los eventos).
    usados = [a for a in sesion.meta.get("adjuntos", []) if a.get("usado_por") is not None]
    if usados:
        bloque = [("F2", 13, f"Adjuntos que usó este apunte ({len(usados)})")]
        for a in usados[:MAX_ADJUNTOS_PDF]:
            minuto = f"[{mmss(a['offset_ms'])}] " if a.get("offset_ms") is not None else ""
            bloque.append(("F1", 11, f'{minuto}ADJUNTO adjuntos/{a["nombre"]}: "{a["nombre_original"]}"'[:90]))
        if len(usados) > MAX_ADJUNTOS_PDF:
            bloque.append(("F1", 11, f"... y {len(usados) - MAX_ADJUNTOS_PDF} adjuntos más"))
        lineas[-1:-1] = bloque + [("F1", 11, "")]
    tope = MAX_EVENTOS_PDF - (len(lineas) - 8)
    eventos = sorted(sesion.eventos.values(), key=orden_evento)
    for ev in eventos[:tope]:
        lineas.append(("F1", 11, f"[{mmss(ev['offset_ms'])}] {describir_evento(ev)}"[:90]))
    if len(eventos) > tope:
        lineas.append(("F1", 11, f"... y {len(eventos) - tope} eventos más"))
    flujo, y = [], 790
    for fuente, tam, texto in lineas:
        if texto:
            flujo.append(f"BT /{fuente} {tam} Tf 50 {y} Td ({texto_pdf(texto)}) Tj ET")
        y -= tam + 5
    contenido = "\n".join(flujo).encode("ascii")

    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(contenido) + contenido + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, objeto in enumerate(objetos, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % n + objeto + b"\nendobj\n"
    inicio_xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objetos) + 1)
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset  # cada entrada mide exactamente 20 bytes
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objetos) + 1, inicio_xref)
    return bytes(pdf)


# ----------------------------------------------------------------------------- HTTP

RUTAS = [(metodo, re.compile(patron), accion) for metodo, patron, accion in [
    ("GET", r"/api/health", "health"),
    ("GET", r"/api/materias", "materias"),
    ("PATCH", r"/api/materias/(?P<mid>[^/]+)", "cambiar_materia"),
    ("GET", r"/api/sesiones", "listar_sesiones"),
    ("POST", r"/api/sesiones", "crear_sesion"),
    ("GET", r"/api/sesiones/(?P<sid>[^/]+)", "ver_sesion"),
    ("POST", r"/api/sesiones/(?P<sid>[^/]+)/materia", "asignar_materia"),
    ("GET", r"/api/sesiones/(?P<sid>[^/]+)/audio", "ver_audio"),
    ("PUT", r"/api/sesiones/(?P<sid>[^/]+)/audio", "subir_audio"),
    ("POST", r"/api/sesiones/(?P<sid>[^/]+)/eventos", "subir_evento"),
    ("GET", r"/api/sesiones/(?P<sid>[^/]+)/eventos", "ver_eventos"),
    ("POST", r"/api/sesiones/(?P<sid>[^/]+)/finalizar", "finalizar"),
    ("GET", r"/api/sesiones/(?P<sid>[^/]+)/apunte\.pdf", "apunte_pdf"),
    ("POST", r"/api/sesiones/(?P<sid>[^/]+)/adjuntos", "subir_adjunto"),
    ("POST", r"/api/sesiones/(?P<sid>[^/]+)/regenerar", "regenerar"),
    ("POST", r"/api/sesiones/(?P<sid>[^/]+)/revision", "revision"),
]]


class ServidorFalso(ThreadingHTTPServer):
    daemon_threads = True
    # En Windows SO_REUSEADDR deja que dos procesos escuchen en el mismo puerto sin avisar.
    allow_reuse_address = os.name != "nt"

    def __init__(self, args, materias: Materias, almacen: Almacen, registro: Registro):
        self.args, self.materias, self.almacen, self.registro = args, materias, almacen, registro
        super().__init__(("0.0.0.0", args.puerto), Manejador)


class Manejador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClassmerFalso/0.2"
    sys_version = ""
    timeout = 60  # segundos sin recibir nada antes de dar por muerta la conexión

    def do_GET(self):
        self._atender()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

    def log_request(self, code="-", size="-"):
        pass  # cada pedido se registra en _atender, con más detalle

    def log_message(self, formato, *args):
        self.server.registro(f"{self.client_address[0]} http.server: {formato % args}")

    # --- despacho

    def _atender(self):
        t0 = time.monotonic()
        self._cuerpo, self._status, self._resumen = None, None, ""
        url = urlsplit(self.path)
        self._query = parse_qs(url.query)
        args = self.server.args
        try:
            if args.latencia_ms:
                time.sleep(args.latencia_ms / 1000)
            if not url.path.startswith("/api/"):
                raise ErrorHttp(404, {"detail": "ruta desconocida"})
            if not self._token_ok():
                raise ErrorHttp(401, {"detail": "token inválido"}, "sin token o token incorrecto")
            accion, params = self._rutear(url.path)
            if url.path != "/api/health" and random.random() < args.fallar_prob:
                raise ErrorHttp(503, {"detail": "falla simulada"}, "falla simulada")
            getattr(self, "_" + accion)(**params)
        except ErrorHttp as e:
            self._resumen = e.resumen or self._resumen or str(e)
            self._responder(e.status, e.cuerpo)
        except (ConnectionError, TimeoutError) as e:
            self.close_connection = True
            self._resumen += f" (cliente desconectado: {e!r})"
        except Exception as e:  # un bug del servidor falso: que se vea en el log
            self.close_connection = True
            self._resumen = "ERROR INTERNO\n" + traceback.format_exc()
            if self._status is None:
                try:
                    self._responder(500, {"detail": f"error interno: {e!r}"})
                except OSError:
                    pass
        finally:
            largo = self.headers.get("Content-Length") or (
                "chunked" if self.headers.get("Transfer-Encoding") else "-")
            ms = (time.monotonic() - t0) * 1000
            self.server.registro(
                f"{self.client_address[0]} {self.command} {self.path} cl={largo} -> "
                f"{self._status or '---'} ({ms:.0f} ms) {self._resumen}")

    def _token_ok(self) -> bool:
        esquema, _, token = self.headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if esquema.lower() != "bearer" or not token:
            return False
        return self.server.args.cualquier_token or hmac.compare_digest(
            token.encode(), self.server.args.token.encode())

    def _rutear(self, ruta: str) -> tuple[str, dict]:
        existe_ruta = False
        for metodo, patron, accion in RUTAS:
            m = patron.fullmatch(ruta)
            if m:
                existe_ruta = True
                if metodo == self.command:
                    return accion, m.groupdict()
        if existe_ruta:
            raise ErrorHttp(405, {"detail": "método no permitido"})
        raise ErrorHttp(404, {"detail": "ruta desconocida"})

    # --- lectura y respuesta

    def _leer_cuerpo(self, limite: int | None = None) -> tuple[bytes, bool]:
        """Lee el cuerpo (Content-Length o chunked). Devuelve (bytes, completo).

        Si el cliente corta a mitad de camino devuelve lo que llegó con completo=False.
        Con `limite` deja de leer al llegar a esa cantidad (para simular un corte).
        """
        if self._cuerpo is not None:
            return self._cuerpo
        partes, total = [], 0

        def leer(n: int) -> bool:
            nonlocal total
            while n > 0:
                if limite is not None and total >= limite:
                    return False
                tope = min(n, 65536) if limite is None else min(n, 65536, limite - total)
                pedazo = self.rfile.read1(tope)
                if not pedazo:
                    return False
                partes.append(pedazo)
                total += len(pedazo)
                n -= len(pedazo)
            return True

        completo = True
        try:
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                while completo:
                    tam = int(self.rfile.readline(100).split(b";")[0].strip() or b"-", 16)
                    if tam == 0:
                        while self.rfile.readline(1000).strip():
                            pass  # trailers
                        break
                    completo = leer(tam) and self.rfile.readline(10) == b"\r\n"
            else:
                largo = int(self.headers.get("Content-Length") or 0)
                completo = largo >= 0 and leer(largo)
        except (OSError, ValueError):
            completo = False
        if not completo:
            self.close_connection = True
        self._cuerpo = (b"".join(partes), completo)
        return self._cuerpo

    def _cuerpo_json(self, status: int = 400) -> dict:
        """El cuerpo como objeto JSON. Si no lo es, ErrorHttp con `status` (las rutas viejas del
        falso contestan 400; las nuevas, 422 como FastAPI)."""
        error = error422 if status == 422 else error400
        datos, completo = self._leer_cuerpo()
        if not completo:
            raise error("cuerpo incompleto o mal formado")
        try:
            obj = json.loads(datos.decode("utf-8"))
        except ValueError:
            raise error("JSON inválido") from None
        if not isinstance(obj, dict):
            raise error("se esperaba un objeto JSON")
        return obj

    def _responder(self, status: int, cuerpo, tipo="application/json; charset=utf-8",
                   extra: dict | None = None):
        self._leer_cuerpo()  # drenar lo que no se leyó, así el cliente recibe la respuesta
        datos = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo, ensure_ascii=False).encode()
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        for clave, valor in (extra or {}).items():
            self.send_header(clave, valor)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(datos)

    def _param_int(self, nombre: str, por_defecto: int | None = None) -> int:
        valores = self._query.get(nombre)
        if not valores:
            if por_defecto is None:
                raise error400(f"falta el parámetro {nombre}")
            return por_defecto
        if not RE_ENTERO.fullmatch(valores[0]):
            raise error400(f"{nombre} tiene que ser un entero >= 0")
        return int(valores[0])

    def _sesion(self, sid: str) -> Sesion:
        sesion = self.server.almacen.sesiones.get(sid)
        if sesion is None:
            raise ErrorHttp(404, {"detail": "sesión no encontrada"}, f"404 sesión desconocida {sid}")
        return sesion

    def _materia(self, mid) -> dict | None:
        """La materia del archivo, activa o no (las inactivas sólo se ocultan en GET /api/materias)."""
        return next((m for m in self.server.materias.todas() if m["id"] == mid), None)

    def _ids_activos(self) -> list[str]:
        return [m["id"] for m in self.server.materias.activas()]

    def _proximo(self, materia: dict, sin: Sesion | None = None) -> int:
        """proximo_numero de la materia. Con el lock del almacén tomado."""
        return numero_inicial(materia) + self.server.almacen.clases_de(
            materia["id"], self.server.args.procesar_seg, sin)

    def _materia_api(self, materia: dict) -> dict:
        """Una materia como la devuelve GET /api/materias. Con el lock del almacén tomado."""
        return {"id": materia["id"], "nombre": materia.get("nombre", materia["id"]),
                "calendario": materia.get("calendario"),
                "tipos_clase": materia.get("tipos_clase") or list(TIPOS_CLASE),
                "proximo_numero": self._proximo(materia)}

    # --- rutas

    def _max_audio_importada(self) -> int:
        return int(self.server.args.max_audio_importada_mb * 1024 * 1024)

    def _health(self):
        self._resumen = "ok"
        self._responder(200, {"ok": True, "version": VERSION})

    def _materias(self):
        with self.server.almacen.lock:
            lista = [self._materia_api(m) for m in self.server.materias.activas()]
        self._resumen = f"{len(lista)} activas: " + ", ".join(
            f"{m['id']} (próxima {m['proximo_numero']})" for m in lista)
        self._responder(200, lista)

    def _cambiar_materia(self, mid):
        d = self._cuerpo_json(status=422)
        claves = [c for c in ("proximo_numero", "numero_clase_inicial") if c in d]
        if len(claves) != 1:
            raise error422("mandá exactamente uno de proximo_numero o numero_clase_inicial")
        clave, valor = claves[0], d[claves[0]]
        if not es_entero(valor) or valor < 1:
            raise error422(f"{clave} tiene que ser un entero >= 1")
        no_esta = ErrorHttp(404, {"detail": "materia no encontrada"},
                            f"404 materia no encontrada: {mid!r}")
        almacen = self.server.almacen
        with almacen.lock:
            materia = self._materia(mid)
            if materia is None:
                raise no_esta
            clases = almacen.clases_de(mid, self.server.args.procesar_seg)
            antes = numero_inicial(materia) + clases
            inicial = valor - clases if clave == "proximo_numero" else valor
            materia = self.server.materias.fijar_inicial(mid, inicial)
            if materia is None:  # la borraron del archivo justo
                raise no_esta
            datos = self._materia_api(materia)
        inactiva = "" if esta_activa(materia) else " [inactiva]"
        self._resumen = (f"materia {mid}{inactiva}: {clave}={valor} -> próxima clase {antes} -> "
                         f"{datos['proximo_numero']} (numero_clase_inicial={inicial}, "
                         f"{clases} clases; guardado en {self.server.materias.ruta.name})")
        self._responder(200, datos)

    def _listar_sesiones(self):
        limite = self._param_int("limite", por_defecto=20)
        almacen = self.server.almacen
        with almacen.lock:
            ids = self._ids_activos()
            sesiones = sorted(almacen.sesiones.values(), key=clave_inicio, reverse=True)[:limite]
            lista = [s.a_json(self.server.args.procesar_seg, ids) for s in sesiones]
        self._resumen = f"{len(lista)} sesiones (limite={limite})"
        self._responder(200, lista)

    def _crear_sesion(self):
        d = self._cuerpo_json()
        sid, materia_id = d.get("id"), d.get("materia_id")
        if not isinstance(sid, str) or not RE_UUID.fullmatch(sid):
            raise error400("id tiene que ser un UUID")
        almacen = self.server.almacen
        with almacen.lock:  # todo junto: validar, numerar y crear (orden de locks almacén -> materias)
            existente = almacen.sesiones.get(sid)
            if existente:
                # Como el server real: si ya existe no se valida el resto. Así el reintento del celu
                # da 200 aunque mientras tanto hayan sacado su materia del archivo.
                estado = existente.pipeline(self.server.args.procesar_seg)[0]
                self._resumen = f"sesion ya existía (estado={estado}, no se toca)"
                respuesta = 200, {"id": sid, "estado": estado}
            else:
                respuesta = 201, {"id": sid, "estado": "grabando"}
                materias = {m["id"]: m for m in self.server.materias.todas()}  # inactivas incluidas
                if materia_id is not None and not es_uno_de(materia_id, materias):
                    raise error400(f"materia_id desconocida: {materia_id!r}")
                numero = d.get("numero_clase")
                if numero is not None and (not es_entero(numero) or numero < 1):
                    raise error400("numero_clase tiene que ser un entero >= 1")
                if not es_uno_de(d.get("origen_materia"), ORIGENES):
                    raise error400("origen_materia tiene que ser calendario, manual o auto")
                tipos = materias[materia_id].get("tipos_clase") if materia_id else None
                tipos = tipos or TIPOS_CLASE
                if not es_uno_de(d.get("tipo_clase"), tipos):
                    raise error400(f"tipo_clase tiene que ser uno de {tipos}")
                if not fecha_con_zona(d.get("inicio")):
                    raise error400("inicio tiene que ser ISO 8601 con zona horaria")
                if d.get("dispositivo") is not None and not isinstance(d["dispositivo"], str):
                    raise error400("dispositivo tiene que ser texto")
                importada = d.get("importada", False)
                if not isinstance(importada, bool):
                    raise error400("importada tiene que ser true o false")
                audio_nombre, audio_bytes = d.get("audio_nombre"), d.get("audio_bytes")
                if audio_nombre is not None and not isinstance(audio_nombre, str):
                    raise error400("audio_nombre tiene que ser texto")
                if audio_bytes is not None and (not es_entero(audio_bytes) or audio_bytes < 0):
                    raise error400("audio_bytes tiene que ser un entero >= 0")
                tope = self._max_audio_importada()
                if importada and audio_bytes is not None and audio_bytes > tope:
                    raise ErrorHttp(413, {"detail": f"el audio pesa más de 2 GB: {audio_bytes} bytes"},
                                    f"413 importada con audio_bytes={audio_bytes} > {tope}")
                elegido = "elegido por el celu"
                if numero is None and materia_id is not None:
                    numero, elegido = self._proximo(materias[materia_id]), "proximo_numero"
                meta = {
                    "id": sid, "materia_id": materia_id, "origen_materia": d["origen_materia"],
                    "tipo_clase": d["tipo_clase"], "numero_clase": numero, "inicio": d["inicio"],
                    "dispositivo": d.get("dispositivo"), "creada": ahora_iso(), "finalizacion": None,
                }
                if importada:  # los campos nuevos sólo cuentan en una importada
                    meta.update(importada=True, audio_nombre=audio_nombre, audio_bytes=audio_bytes)
                almacen.crear(meta)
                clase = f"clase={numero} ({elegido})" if numero is not None else "clase=sin número"
                extra = f" IMPORTADA audio={audio_nombre!r} ({audio_bytes} B)" if importada else ""
                self._resumen = f"sesion creada materia={materia_id} origen={d['origen_materia']} {clase}{extra}"
        self._responder(*respuesta)

    def _ver_sesion(self, sid):
        sesion = self._sesion(sid)
        with self.server.almacen.lock:
            datos = sesion.a_json(self.server.args.procesar_seg, self._ids_activos())
        self._resumen = f"estado={datos['estado']}"
        if datos["estado"] == "procesando":
            self._resumen += f" etapa={datos['etapa']} {datos['progreso']:.0%}"
        self._responder(200, datos)

    def _asignar_materia(self, sid):
        sesion = self._sesion(sid)
        args, almacen = self.server.args, self.server.almacen
        if args.rechazar_materia:
            detalle = {409: "el apunte ya empezó (simulado)",
                       422: "materia desconocida (simulado)"}[args.rechazar_materia]
            raise ErrorHttp(args.rechazar_materia, {"detail": detalle},
                            f"{args.rechazar_materia} {detalle} (--rechazar-materia)")
        d = self._cuerpo_json(status=422)
        mid = d.get("materia_id")
        if not isinstance(mid, str):
            raise error422("materia_id tiene que ser texto")
        with almacen.lock:
            ids = self._ids_activos()
            materia = self._materia(mid)
            if materia is None:
                raise ErrorHttp(422, {"detail": f"materia desconocida: '{mid}'"},
                                f"422 materia desconocida: {mid!r}")
            estado, etapa, _ = sesion.pipeline(args.procesar_seg)
            en = f" etapa={etapa}" if estado == "procesando" else ""
            if estado not in ESTADOS_ASIGNABLES or etapa in ETAPAS_TRAS_ASIGNAR:
                raise ErrorHttp(409, {"detail": f"el apunte ya empezó (estado={estado})"},
                                f"409 el apunte ya empezó (estado={estado}{en}), pedían {mid}")
            antes, numero_antes = sesion.meta.get("materia_id"), sesion.numero_clase
            # Como el server real: si cambia la materia, toma el próximo número de la nueva; salvo
            # una Auto que ya traía numero_clase del celu, que lo conserva.
            renumerar = mid != antes and (antes is not None or numero_antes is None)
            if renumerar:
                sesion.meta["numero_clase"] = self._proximo(materia, sin=sesion)
            sesion.meta.update(materia_id=mid, origen_materia="manual")
            if estado == "esperando_usuario":
                sesion.meta["asignada_en"] = time.time()
            sesion.guardar()
            datos = sesion.a_json(args.procesar_seg, ids)
        if renumerar:
            clase = f"clase {numero_antes or 'sin número'} -> {sesion.numero_clase}"
        elif mid != antes:
            clase = f"conserva la clase {numero_antes} que mandó el celu"
        else:
            clase = f"misma materia, clase {sesion.numero_clase} sin cambios"
        inactiva = "" if esta_activa(materia) else " [inactiva]"
        self._resumen = (f"materia asignada {antes or 'Auto'} -> {mid}{inactiva} en estado={estado}{en}, "
                         f"{clase}" + (" (retoma desde el apunte)" if estado == "esperando_usuario" else ""))
        self._responder(200, datos)

    def _pista(self) -> str | None:
        """El parámetro `pista` de /audio (None: el principal). Con --sin-pista se ignora, como un server
        viejo, y queda anotado en el resumen."""
        valores = self._query.get("pista")
        if not valores:
            return None
        if self.server.args.sin_pista:
            self._ignorada = f" [pista={valores[0]} IGNORADA: server viejo]"
            return None
        if valores[0] not in PISTAS:
            raise error422(f"pista desconocida: {valores[0]!r}")
        return valores[0]

    def _ver_audio(self, sid):
        sesion = self._sesion(sid)
        self._ignorada = ""
        pista = self._pista()
        n = sesion.bytes_de(pista)
        self._resumen = f"bytes_recibidos={n}" + (f" (pista {pista})" if pista else "") + self._ignorada
        self._responder(200, {"bytes_recibidos": n})

    def _subir_audio(self, sid):
        sesion = self._sesion(sid)
        offset = self._param_int("offset")
        self._ignorada = ""
        pista = self._pista()
        de_pista = (f" (pista {pista})" if pista else "") + self._ignorada
        args, lock = self.server.args, self.server.almacen.lock
        tipo = self.headers.get("Content-Type", "")
        aviso = "" if tipo.startswith("application/octet-stream") else f" [aviso: Content-Type={tipo!r}]"
        declarado = self.headers.get("Content-Length")
        cortar = args.cortar_prob and random.random() < args.cortar_prob
        if cortar:  # leer más o menos la mitad y colgar sin responder
            limite = int(declarado) // 2 if declarado and declarado.isdigit() else 32768
            datos, completo = self._leer_cuerpo(limite)
        else:
            datos, completo = self._leer_cuerpo()
        with lock:
            actual = sesion.bytes_de(pista)
            if cortar or not completo:
                self.close_connection = True
                guardar = args.parciales and datos and offset == actual and not sesion.finalizacion
                if guardar:
                    sesion.agregar_audio(datos, pista)
                quien = "CORTE simulado" if cortar else "el cliente cortó"
                destino = f"guardados -> {sesion.bytes_de(pista)}" if guardar else "descartados"
                self._resumen = f"{quien} tras {len(datos)}/{declarado or '?'} B ({destino}){de_pista}{aviso}"
                return
            if sesion.finalizacion or offset != actual:
                ya = " (ya finalizada)" if sesion.finalizacion else ""
                raise ErrorHttp(409, {"bytes_recibidos": actual},
                                f"409 offset={offset} pero tengo {actual}{ya}{de_pista}")
            tope = self._max_audio_importada()
            if sesion.meta.get("importada") and pista is None and offset + len(datos) > tope:
                raise ErrorHttp(413, {"detail": f"el audio pesa más de 2 GB: pasaría los {tope} bytes"},
                                f"413 importada: offset={offset} + {len(datos)} > {tope}")
            sesion.agregar_audio(datos, pista)
            nuevo_total = sesion.bytes_de(pista)
        self._resumen = f"audio +{len(datos)} B -> {nuevo_total}{de_pista}{aviso}"
        self._responder(200, {"bytes_recibidos": nuevo_total})

    def _subir_evento(self, sid):
        sesion = self._sesion(sid)
        datos, completo = self._leer_cuerpo()
        if not completo:
            raise error400("cuerpo incompleto")
        partes = parsear_multipart(self.headers.get("Content-Type", ""), datos)

        def campo(nombre):
            return partes[nombre][1].decode("utf-8", errors="replace") if nombre in partes else None

        eid, tipo, offset_txt, creado = campo("id"), campo("tipo"), campo("offset_ms"), campo("creado")
        texto, marca = campo("texto"), campo("marca")
        if not eid or not RE_UUID.fullmatch(eid.strip()):
            raise error400("id tiene que ser un UUID")
        eid = eid.strip()
        if tipo not in TIPOS_EVENTO:
            raise error400(f"tipo tiene que ser uno de {sorted(TIPOS_EVENTO)}")
        # Una foto puede venir sin offset_ms (la de una clase importada: la ubica el server por su EXIF).
        sin_hora = tipo == "foto" and not (offset_txt and offset_txt.strip())
        if not sin_hora and (not offset_txt or not RE_ENTERO.fullmatch(offset_txt.strip())):
            raise error400("offset_ms tiene que ser un entero >= 0")
        if not fecha_con_zona(creado and creado.strip()):
            raise error400("creado tiene que ser ISO 8601 con zona horaria")
        evento = {"id": eid, "tipo": tipo, "offset_ms": None if sin_hora else int(offset_txt),
                  "creado": creado.strip()}
        if tipo == "nota":
            if not texto or not texto.strip():
                raise error400("una nota necesita texto")
        if texto is not None:
            evento["texto"] = texto
        if tipo == "marca":
            if marca not in MARCAS:
                raise error400(f"marca tiene que ser uno de {sorted(MARCAS)}")
            evento["marca"] = marca
        archivo = partes.get("archivo")
        if tipo == "foto" and (not archivo or not archivo[1]):
            raise error400("una foto necesita el archivo")

        with self.server.almacen.lock:
            nuevo = eid not in sesion.eventos
            if nuevo and sesion.finalizacion:
                raise ErrorHttp(409, {"detail": "la sesión ya fue finalizada"},
                                f"409 evento {tipo} nuevo en sesión finalizada")
            if nuevo:
                if tipo == "foto":
                    nombre = nombre_seguro(archivo[0] or "") or f"{eid}.jpg"
                    if (sesion.carpeta / "fotos" / nombre).exists():
                        nombre = f"{eid}_{nombre}"
                    (sesion.carpeta / "fotos" / nombre).write_bytes(archivo[1])
                    evento.update(archivo=nombre, bytes=len(archivo[1]),
                                  sha256=hashlib.sha256(archivo[1]).hexdigest())
                evento.update(recibido=ahora_iso(), n=len(sesion.eventos) + 1)
                escribir_json(sesion.carpeta / "eventos" / f"{eid}.json", evento)
                sesion.eventos[eid] = evento
            else:
                evento = sesion.eventos[eid]
        detalle = {"marca": f"marca={evento.get('marca')}", "foto": f"foto {evento.get('archivo')}"
                   f" {evento.get('bytes')} B", "nota": f"nota {evento.get('texto', '')[:40]!r}"}
        minuto = "sin hora" if evento["offset_ms"] is None else evento["offset_ms"]
        self._resumen = (f"evento {detalle.get(tipo, tipo)} offset_ms={minuto} "
                         f"({'nuevo' if nuevo else 'repetido'})")
        self._responder(201 if nuevo else 200, {"id": eid, "nuevo": nuevo})

    def _ver_eventos(self, sid):
        sesion = self._sesion(sid)
        with self.server.almacen.lock:
            ids = list(sesion.eventos)
        self._resumen = f"{len(ids)} eventos"
        self._responder(200, {"ids": ids})

    def _finalizar(self, sid):
        sesion = self._sesion(sid)
        d = self._cuerpo_json()
        sha = d.get("sha256")
        if not isinstance(sha, str) or not RE_SHA256.fullmatch(sha):
            raise error400("sha256 tiene que ser 64 caracteres hexadecimales")
        for nombre in ("bytes_totales", "duracion_ms", "eventos_totales"):
            # duracion_ms puede ser null: una importada cuya duración el celu no pudo medir.
            if nombre == "duracion_ms" and d.get(nombre, 0) is None:
                continue
            if type(d.get(nombre)) is not int or d[nombre] < 0:
                raise error400(f"{nombre} tiene que ser un entero >= 0")
        sha, args = sha.lower(), self.server.args
        # Ambos: el bloque de la pista del celu. Un server viejo (--sin-pista) lo ignora.
        celu = None if args.sin_pista else d.get("celu")
        if celu is not None:
            if not isinstance(celu, dict):
                raise error400("celu tiene que ser un objeto")
            if not isinstance(celu.get("sha256"), str) or not RE_SHA256.fullmatch(celu["sha256"]):
                raise error400("celu.sha256 tiene que ser 64 caracteres hexadecimales")
            for nombre in ("bytes_totales", "duracion_ms"):
                if type(celu.get(nombre)) is not int or celu[nombre] < 0:
                    raise error400(f"celu.{nombre} tiene que ser un entero >= 0")
            if type(celu.get("desfase_ms")) is not int:
                raise error400("celu.desfase_ms tiene que ser un entero (puede ser negativo)")

        def rechazo(motivo, resumen, **extra):
            return ErrorHttp(409, {"motivo": motivo, **extra}, f"finalizar 409 {motivo}: {resumen}")

        with self.server.almacen.lock:
            fin = sesion.finalizacion
            if fin:  # ya estaba finalizada: idempotente si es el mismo audio
                if fin["sha256"] != sha:
                    raise rechazo("hash_distinto", "ya estaba finalizada con otro hash (no se toca)")
                estado = sesion.pipeline(args.procesar_seg)[0]
                self._resumen = f"finalizar repetido OK (estado={estado})"
                return self._responder(200, {"estado": estado})
            # Contrato v1: con MÁS bytes que el celu, faltan_bytes no lo arregla -> hash_distinto + reset.
            if d["bytes_totales"] < sesion.bytes_recibidos and not args.sin_reset_hash:
                tenia = sesion.bytes_recibidos
                sesion.resetear_audio()
                raise rechazo("hash_distinto", f"tengo {tenia} B, más que el celu -> audio reseteado a 0",
                              bytes_recibidos=0)
            if d["bytes_totales"] != sesion.bytes_recibidos:
                raise rechazo("faltan_bytes",
                              f"el celu dice {d['bytes_totales']} B, tengo {sesion.bytes_recibidos}",
                              bytes_recibidos=sesion.bytes_recibidos)
            if celu is not None:
                if celu["bytes_totales"] < sesion.bytes_celu and not args.sin_reset_hash:
                    tenia = sesion.bytes_celu
                    sesion.resetear_audio("celu")
                    raise rechazo("hash_distinto", f"pista celu: tengo {tenia} B, más que el celu -> reseteada a 0",
                                  bytes_recibidos=0, pista="celu")
                if celu["bytes_totales"] != sesion.bytes_celu:
                    raise rechazo("faltan_bytes",
                                  f"pista celu: el celu dice {celu['bytes_totales']} B, tengo {sesion.bytes_celu}",
                                  bytes_recibidos=sesion.bytes_celu, pista="celu")
            if d["eventos_totales"] != len(sesion.eventos):
                raise rechazo("faltan_eventos",
                              f"el celu dice {d['eventos_totales']}, tengo {len(sesion.eventos)}",
                              eventos_recibidos=len(sesion.eventos))
            real = sha256_archivo(sesion.ruta_audio)
            if real != sha:
                if args.sin_reset_hash:
                    raise rechazo("hash_distinto", f"tengo {real[:12]}... (audio intacto)",
                                  bytes_recibidos=sesion.bytes_recibidos)
                sesion.resetear_audio()
                raise rechazo("hash_distinto", f"tengo {real[:12]}... -> audio reseteado a 0",
                              bytes_recibidos=0)
            if celu is not None:
                real_celu = sha256_archivo(sesion.ruta_audio_celu)
                if real_celu != celu["sha256"].lower():
                    if args.sin_reset_hash:
                        raise rechazo("hash_distinto", f"pista celu: tengo {real_celu[:12]}... (intacta)",
                                      bytes_recibidos=sesion.bytes_celu, pista="celu")
                    sesion.resetear_audio("celu")
                    raise rechazo("hash_distinto", f"pista celu: tengo {real_celu[:12]}... -> reseteada a 0",
                                  bytes_recibidos=0, pista="celu")
            sesion.meta["finalizacion"] = {
                "sha256": sha, "bytes_totales": d["bytes_totales"], "duracion_ms": d["duracion_ms"],
                "eventos_totales": d["eventos_totales"], "finalizada_en": time.time(),
                "finalizada_iso": ahora_iso(),
            }
            if celu is not None:
                sesion.meta["finalizacion"]["celu"] = {**celu, "sha256": celu["sha256"].lower()}
            if args.dudas and not args.sin_pista:
                sesion.meta["dudas"] = args.dudas
            sesion.guardar()
        extra = ""
        if celu is not None:
            extra = (f" + pista celu {celu['bytes_totales']} B, {hms(celu['duracion_ms'])}, "
                     f"desfase {celu['desfase_ms'] / 1000:+.3f} s")
        elif sesion.bytes_celu:
            extra = f" (llegaron {sesion.bytes_celu} B de pista celu sin el bloque celu: se ignoran)"
        elif args.sin_pista and "celu" in d:
            extra = " (bloque celu IGNORADO: server viejo)"
        self._resumen = f"finalizar OK sha256={sha[:12]}... duracion={hms(d['duracion_ms'])}{extra}"
        self._responder(200, {"estado": "en_cola"})

    def _apunte_pdf(self, sid):
        sesion = self._sesion(sid)
        with self.server.almacen.lock:
            estado = sesion.pipeline(self.server.args.procesar_seg)[0]
            if estado != "lista":
                raise ErrorHttp(404, {"detail": f"el apunte todavía no está listo (estado={estado})"},
                                f"404 apunte no listo (estado={estado})")
            materia = next((m for m in self.server.materias.todas()
                            if m["id"] == sesion.meta.get("materia_id")), None)
            nombre = f"{materia.get('nombre', materia['id'])} ({materia['id']})" if materia else "Auto (IA)"
            pdf = generar_pdf(sesion, nombre)
        self._resumen = f"apunte.pdf {len(pdf)} B"
        self._responder(200, pdf, "application/pdf",
                        {"Content-Disposition": f'inline; filename="apunte-{sid[:8]}.pdf"'})

    # --- adjuntos

    def _descartar_cuerpo(self) -> None:
        """Lee el cuerpo y lo tira, sin juntarlo en memoria (para contestar un error a un adjunto
        de cientos de MB). Con chunked lo lee normal."""
        largo = self.headers.get("Content-Length") or ""
        if self._cuerpo is not None or not largo.isdigit() or "chunked" in self.headers.get(
                "Transfer-Encoding", "").lower():
            self._leer_cuerpo()
            return
        restante, completo = int(largo), True
        try:
            while restante > 0:
                pedazo = self.rfile.read1(min(restante, 1 << 20))
                if not pedazo:
                    completo = False
                    break
                restante -= len(pedazo)
        except OSError:
            completo = False
        if not completo:
            self.close_connection = True
        self._cuerpo = (b"", completo)

    def _exigir_adjuntos(self) -> None:
        """Con --sin-adjuntos, como un server viejo: FastAPI sin la ruta contesta 404 Not Found."""
        if self.server.args.sin_adjuntos:
            self._descartar_cuerpo()
            raise ErrorHttp(404, {"detail": "Not Found"}, "404 Not Found (--sin-adjuntos: server viejo)")

    def _subir_adjunto(self, sid):
        self._exigir_adjuntos()
        sesion = self._sesion(sid)
        args, almacen = self.server.args, self.server.almacen
        maximo = int(args.max_adjunto_mb * 1024 * 1024)
        demasiado = ErrorHttp(413, {"detail": f"archivo de más de {args.max_adjunto_mb:g} MB"},
                              f"413 adjunto de más de {args.max_adjunto_mb:g} MB")
        declarado = self.headers.get("Content-Length") or ""
        if declarado.isdigit() and int(declarado) > maximo + MARGEN_MULTIPART:
            self._descartar_cuerpo()
            raise demasiado
        datos, completo = self._leer_cuerpo()
        if not completo:
            raise error400("cuerpo incompleto")
        try:
            partes = parsear_multipart(self.headers.get("Content-Type", ""), datos)
        except ErrorHttp as e:  # FastAPI contesta 422 a un form que no se puede leer
            raise error422(e.cuerpo["detail"]) from None

        def campo(nombre):
            return partes[nombre][1].decode("utf-8", errors="replace").strip() if nombre in partes else None

        aid, offset_txt, creado = campo("id"), campo("offset_ms"), campo("creado")
        if not aid or not RE_UUID.fullmatch(aid):
            raise error422("id tiene que ser un UUID")
        if offset_txt is not None and not RE_ENTERO.fullmatch(offset_txt):
            raise error422("offset_ms tiene que ser un entero >= 0")
        if creado is not None and not fecha_con_zona(creado):
            raise error422("creado tiene que ser ISO 8601 con zona horaria")
        # Como el server real: un id que ya está en esta clase devuelve lo guardado sin mirar el archivo
        # (también con la clase en error). La app pregunta así, sin el archivo, antes de mandarlo.
        with almacen.lock:
            previo = next((a for a in sesion.meta.get("adjuntos", []) if a["id"] == aid), None)
            if previo is not None:
                estado, _, _ = sesion.pipeline(args.procesar_seg)
                datos_sesion = sesion.a_json(args.procesar_seg, self._ids_activos())
        if previo is not None:
            objeto = next(a for a in datos_sesion["adjuntos"] if a["id"] == aid)
            minuto = f" en {mmss(previo['offset_ms'])}" if previo.get("offset_ms") is not None else ""
            como = "repetido" if "archivo" in partes else "repetido, consulta sin archivo"
            self._resumen = (f"adjunto {previo['nombre']} ({previo['tipo']}, {previo['bytes']} B){minuto} "
                             f"original={previo['nombre_original']!r} -> {objeto['estado']} (clase {estado}; {como})")
            self._responder(200, {**objeto, "sesion": datos_sesion})
            return
        if "archivo" not in partes:
            raise error422("falta el archivo (campo archivo)")
        contenido = partes["archivo"][1]
        if not contenido:
            raise error422("el archivo está vacío")
        if len(contenido) > maximo:
            raise demasiado
        tipo = tipo_adjunto(contenido)
        if tipo is None:
            raise ErrorHttp(415, {"detail": "el archivo no es PDF, JPEG, PNG, WebP ni HEIC"},
                            f"415 adjunto de tipo desconocido ({contenido[:8]!r})")
        original = nombre_archivo_crudo(datos, "archivo") or partes["archivo"][0] or "adjunto"

        with almacen.lock:
            existente = next((a for a in sesion.meta.get("adjuntos", []) if a["id"] == aid), None)
            nuevo = existente is None
            estado, etapa, _ = sesion.pipeline(args.procesar_seg)
            if nuevo:
                otra = aid in sesion.eventos or any(
                    aid in s.eventos or any(a["id"] == aid for a in s.meta.get("adjuntos", []))
                    for s in almacen.sesiones.values() if s is not sesion)
                if otra:
                    raise error422("el id ya es de otra sesión o de un evento")
                usados = {a["nombre"].rsplit(".", 1)[0].lower() for a in sesion.meta.get("adjuntos", [])}
                nombre = nombre_adjunto(original, tipo, usados)
                carpeta = sesion.carpeta / "adjuntos"
                carpeta.mkdir(exist_ok=True)
                (carpeta / nombre).write_bytes(contenido)
                # El próximo apunte lo usa si todavía no empezó a escribirse; si no, queda sin usar.
                a_tiempo = estado in ("grabando", "en_cola", "esperando_usuario") or (
                    estado == "procesando" and etapa not in ETAPAS_TRAS_ASIGNAR)
                existente = {
                    "id": aid, "nombre": nombre, "nombre_original": original, "tipo": tipo,
                    "mime": TIPOS_ADJUNTO[tipo][0], "bytes": len(contenido),
                    "sha256": hashlib.sha256(contenido).hexdigest(),
                    "offset_ms": int(offset_txt) if offset_txt is not None else None,
                    "creado": creado or ahora_iso(), "recibido": ahora_iso(), "estado_sesion": estado,
                    "usado_por": sesion.meta.get("corrida", 0) if a_tiempo else None,
                }
                sesion.meta.setdefault("adjuntos", []).append(existente)
                sesion.guardar()
            datos_sesion = sesion.a_json(args.procesar_seg, self._ids_activos())
        objeto = next(a for a in datos_sesion["adjuntos"] if a["id"] == aid)
        minuto = f" en {mmss(existente['offset_ms'])}" if existente.get("offset_ms") is not None else ""
        self._resumen = (f"adjunto {existente['nombre']} ({tipo}, {existente['bytes']} B){minuto} "
                         f"original={existente['nombre_original']!r} -> {objeto['estado']} "
                         f"(clase {estado}; {'nuevo' if nuevo else 'repetido'})")
        self._responder(201 if nuevo else 200, {**objeto, "sesion": datos_sesion})

    def _revision(self, sid):
        """Lo que en el server real hace el panel: "Listo" o "Generar sin revisar" (§15). Sólo del falso."""
        if self.server.args.sin_pista:
            self._leer_cuerpo()
            raise ErrorHttp(404, {"detail": "Not Found"}, "404 sin revisión (--sin-pista)")
        sesion = self._sesion(sid)
        d = self._cuerpo_json(status=422)
        accion = d.get("accion", "listo")
        if accion not in ("listo", "sin_revisar"):
            raise error422("accion tiene que ser listo o sin_revisar")
        args = self.server.args
        with self.server.almacen.lock:
            estado, etapa, _ = sesion.pipeline(args.procesar_seg)
            if (estado, etapa) != ESPERA_REVISION[:2]:
                raise ErrorHttp(409, {"detail": f"no espera la revisión (estado={estado})"},
                                f"409 revisión: no la espera (estado={estado}, etapa={etapa})")
            sesion.meta.update(revisada_en=time.time(), revision=accion)
            sesion.guardar()
            datos = sesion.a_json(args.procesar_seg, self._ids_activos())
        self._resumen = f"revisión {accion}: {sesion.dudas} dudas -> sigue el apunte"
        self._responder(200, datos)

    def _regenerar(self, sid):
        self._exigir_adjuntos()
        sesion = self._sesion(sid)
        self._leer_cuerpo()  # vacío o {}: no se usa
        args = self.server.args
        with self.server.almacen.lock:
            estado, etapa, _ = sesion.pipeline(args.procesar_seg)
            if estado not in ("lista", "error"):
                detalle = (f"ya se está procesando (estado={estado})" if estado in ("en_cola", "procesando")
                           else f"la clase no está lista ni en error (estado={estado})")
                raise ErrorHttp(409, {"detail": detalle}, f"409 regenerar: {detalle}")
            corrida = sesion.meta.get("corrida", 0) + 1
            adjuntos = sesion.meta.get("adjuntos", [])
            for a in adjuntos:  # el apunte nuevo usa todos
                a["usado_por"] = corrida
            sesion.meta.update(corrida=corrida, regenerada_en=time.time())
            sesion.meta.pop("error_simulado", None)
            sesion.guardar()
            datos = sesion.a_json(args.procesar_seg, self._ids_activos())
        self._resumen = f"regenerar desde {estado}: corrida {corrida} con {len(adjuntos)} adjuntos -> en_cola"
        self._responder(200, datos)


# ----------------------------------------------------------------------------- arranque

def main():
    p = argparse.ArgumentParser(description="Servidor falso de Classmer (API v1) para probar la app.")
    p.add_argument("--puerto", type=int, default=8000)
    p.add_argument("--token", default="classmer-dev")
    p.add_argument("--datos", type=Path, default=Path("datos-servidor-falso"),
                   help="carpeta donde se guarda el estado (default: ./datos-servidor-falso)")
    p.add_argument("--materias", type=Path,
                   help="JSON con la lista de materias (default: <datos>/materias.json, que se "
                        "crea con las de ARCHITECTURE.md §9 si no existe); se relee si cambia")
    p.add_argument("--procesar-seg", type=float, default=20.0,
                   help="cuánto dura la etapa 'procesando' simulada")
    p.add_argument("--fallar-prob", type=float, default=0.0,
                   help="probabilidad de responder 503 (todas las rutas menos /api/health)")
    p.add_argument("--cortar-prob", type=float, default=0.0,
                   help="probabilidad de colgar un PUT de audio a mitad del cuerpo")
    p.add_argument("--latencia-ms", type=int, default=0, help="demora agregada a cada pedido")
    p.add_argument("--parciales", action="store_true",
                   help="guardar los bytes de audio que llegaron aunque el cuerpo quede cortado")
    p.add_argument("--sin-reset-hash", action="store_true",
                   help="ante hash_distinto no vaciar el audio")
    p.add_argument("--cualquier-token", action="store_true",
                   help="aceptar cualquier token Bearer no vacío (sin token sigue siendo 401)")
    p.add_argument("--rechazar-materia", type=int, choices=[409, 422],
                   help="POST /api/sesiones/{id}/materia contesta siempre este código, sin cambiar nada")
    p.add_argument("--sin-adjuntos", action="store_true",
                   help="simular un server viejo: POST /adjuntos y /regenerar dan 404 {\"detail\": "
                        "\"Not Found\"} y las sesiones no traen los campos de adjuntos")
    p.add_argument("--max-adjunto-mb", type=float, default=100.0,
                   help="tope de un adjunto en MB (default 100, como el server real); más -> 413")
    p.add_argument("--max-audio-importada-mb", type=float, default=2048.0,
                   help="tope del audio de una clase importada en MB (default 2048 = 2 GiB, como el "
                        "server real); más -> 413 al crearla o en el PUT que lo pase")
    p.add_argument("--dudas", type=int, default=0,
                   help="las clases que se finalicen esperan revisar N dudas del transcript "
                        "(esperando_usuario con espera=revision); 0 = sin revisión")
    p.add_argument("--revision-seg", type=float,
                   help="la revisión se resuelve sola a los N segundos (si no, con POST /revision)")
    p.add_argument("--sin-pista", action="store_true",
                   help="simular un server de antes de Ambos: ignora pista=celu (se pegaría al principal), "
                        "el bloque celu de /finalizar y no manda espera ni dudas")
    args = p.parse_args()
    if not (0 <= args.fallar_prob <= 1 and 0 <= args.cortar_prob <= 1):
        p.error("las probabilidades van de 0 a 1")
    if args.procesar_seg < 0 or args.latencia_ms < 0:
        p.error("--procesar-seg y --latencia-ms no pueden ser negativos")
    if args.max_adjunto_mb <= 0 or args.max_audio_importada_mb <= 0:
        p.error("--max-adjunto-mb y --max-audio-importada-mb tienen que ser positivos")
    if args.dudas < 0 or (args.revision_seg is not None and args.revision_seg < 0):
        p.error("--dudas y --revision-seg no pueden ser negativos")
    if args.sin_pista and args.dudas:
        p.error("--sin-pista simula un server sin revisión: no va con --dudas")
    Sesion.con_adjuntos = not args.sin_adjuntos
    Sesion.con_espera = not args.sin_pista
    Sesion.revision_seg = args.revision_seg
    if args.materias is not None and not args.materias.is_file():
        p.error(f"no existe el archivo de materias {args.materias}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # que una tilde no tire abajo el log

    args.datos.mkdir(parents=True, exist_ok=True)
    registro = Registro(args.datos / "servidor.log")
    ruta_materias = args.materias or args.datos / "materias.json"
    materias_nuevas = not ruta_materias.exists()
    if materias_nuevas:
        escribir_json(ruta_materias, MATERIAS_POR_DEFECTO)
    materias = Materias(ruta_materias, registro)
    materias.cargar()
    almacen = Almacen(args.datos / "sesiones")
    cantidad = almacen.cargar(registro)
    servidor = ServidorFalso(args, materias, almacen, registro)
    registro(f"Servidor falso de Classmer {VERSION} escuchando en 0.0.0.0:{args.puerto}")
    token = "cualquiera (--cualquier-token)" if args.cualquier_token else args.token
    registro(f"Desde el celu: http://{ip_lan()}:{args.puerto}   token: {token}")
    registro(f"Datos en {args.datos.resolve()} ({cantidad} sesiones cargadas)")
    registro(f"Materias en {ruta_materias.resolve()}{' (recién creado)' if materias_nuevas else ''}"
             f" -> {materias.describir()}. Se relee solo si lo editás.")
    registro(f"Simulación: procesar={args.procesar_seg:g} s, fallar={args.fallar_prob:g}, "
             f"cortar={args.cortar_prob:g}, latencia={args.latencia_ms} ms, "
             f"parciales={'sí' if args.parciales else 'no'}, "
             f"reset por hash={'no' if args.sin_reset_hash else 'sí'}"
             + (f", POST /materia siempre {args.rechazar_materia}" if args.rechazar_materia else "")
             + (", SIN adjuntos (server viejo)" if args.sin_adjuntos
                else f", adjuntos hasta {args.max_adjunto_mb:g} MB")
             + (", SIN pista del celu ni espera (server viejo)" if args.sin_pista else "")
             + (f", revisión de {args.dudas} dudas" + (f" (sola a los {args.revision_seg:g} s)"
                                                      if args.revision_seg is not None else "")
                if args.dudas else ""))
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        registro("Cortado con Ctrl+C. Chau.")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
