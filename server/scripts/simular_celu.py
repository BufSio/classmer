"""Hace de celu contra un server real de Classmer (API v1, §5.1).

Crea la sesión, sube el audio por partes respetando los 409 (retoma desde bytes_recibidos), sube
fotos, una nota y marcas a medida que "avanza la grabación", finaliza (manejando faltan_bytes,
faltan_eventos y hash_distinto) y después sigue el estado cada 10 s hasta que la sesión queda
lista, en error o esperando al usuario. Si queda lista, baja el PDF.

Uso (desde server/):
    uv run python scripts/simular_celu.py --audio C:\\ruta\\clase-prueba.ogg --fotos C:\\ruta\\fotos \\
        --materia analisis [--url http://192.168.1.10:8000] [--chunk 1048576]

El token sale de --token o, si no se pasa, de Classmer-data\\.env (classmer.config). Nunca se
imprime. Para retomar una sesión cortada: --sesion <id> (los ids de los eventos son estables).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ESTADOS_FINALES = ("lista", "error", "esperando_usuario")
MAX_CHUNK = 8 * 1024 * 1024
EXTENSIONES_FOTO = (".jpg", ".jpeg")
ZONA = "America/Argentina/Buenos_Aires"


class ErrorSimulador(RuntimeError):
    """El server respondió algo que el celu no sabe manejar."""


@dataclass
class Evento:
    id: str
    tipo: str
    offset_ms: int
    creado: str
    texto: str | None = None
    marca: str | None = None
    foto: Path | None = None

    def form(self) -> dict[str, str]:
        datos = {"id": self.id, "tipo": self.tipo, "offset_ms": str(self.offset_ms), "creado": self.creado}
        if self.texto:
            datos["texto"] = self.texto
        if self.marca:
            datos["marca"] = self.marca
        return datos


class Celu:
    def __init__(self, cliente: httpx.Client, *, imprimir: Callable[[str], None] = print,
                 reintentos: int = 4, espera_base_s: float = 2.0):
        self.cliente = cliente
        self.imprimir = imprimir
        self.reintentos = reintentos
        self.espera_base_s = espera_base_s

    # --- HTTP con reintentos (como WorkManager: espera creciente si no hay red) ---
    def pedir(self, metodo: str, ruta: str, **kw: Any) -> httpx.Response:
        for intento in range(self.reintentos + 1):
            try:
                r = self.cliente.request(metodo, ruta, **kw)
            except httpx.TransportError as e:
                if intento == self.reintentos:
                    raise ErrorSimulador(f"sin conexión con el server ({type(e).__name__}): {e}") from e
                espera = self.espera_base_s * 2 ** intento
                self.imprimir(f"  sin red ({type(e).__name__}); reintento en {espera:.0f} s")
                time.sleep(espera)
                continue
            if r.status_code == 401:
                raise ErrorSimulador("401: token inválido (revisá --token o Classmer-data\\.env)")
            if r.status_code >= 500 and intento < self.reintentos:
                espera = self.espera_base_s * 2 ** intento
                self.imprimir(f"  el server respondió {r.status_code}; reintento en {espera:.0f} s")
                time.sleep(espera)
                continue
            return r
        raise AssertionError("no se llega")

    @staticmethod
    def esperar_estado(r: httpx.Response, *codigos: int) -> dict[str, Any]:
        if r.status_code not in codigos:
            raise ErrorSimulador(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}


def sha256_de(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        while bloque := f.read(1024 * 1024):
            h.update(bloque)
    return h.hexdigest()


def armar_eventos(sid: str, inicio: datetime, dur_ms: int, fotos: list[Path],
                  offsets_fotos: list[int] | None) -> list[Evento]:
    """Fotos (en los offsets dados o repartidas), una nota y dos marcas. Ids estables por sesión."""
    base = uuid.UUID(sid) if _es_uuid(sid) else uuid.uuid5(uuid.NAMESPACE_URL, sid)

    def ev(clave: str, tipo: str, offset: int, **kw: Any) -> Evento:
        offset = max(0, min(int(offset), dur_ms))
        creado = (inicio + timedelta(milliseconds=offset)).isoformat(timespec="seconds")
        return Evento(id=str(uuid.uuid5(base, clave)), tipo=tipo, offset_ms=offset, creado=creado, **kw)

    eventos = []
    for i, foto in enumerate(fotos):
        if offsets_fotos and i < len(offsets_fotos):
            off = offsets_fotos[i]
        else:
            off = dur_ms * (i + 1) // (len(fotos) + 1)
        eventos.append(ev(f"foto:{foto.name}", "foto", off, foto=foto))
    eventos.append(ev("nota", "nota", dur_ms * 3 // 10,
                      texto="preguntar por qué el gradiente es perpendicular a las curvas de nivel"))
    eventos.append(ev("importante", "marca", dur_ms // 2, marca="importante"))
    eventos.append(ev("no_entendi", "marca", dur_ms * 7 // 10, marca="no_entendi"))
    return sorted(eventos, key=lambda e: e.offset_ms)


def _es_uuid(texto: str) -> bool:
    try:
        uuid.UUID(texto)
        return True
    except ValueError:
        return False


def simular(
    cliente: httpx.Client,
    audio: Path,
    *,
    fotos: Sequence[Path] = (),
    materia: str | None = None,
    tipo_clase: str | None = None,
    chunk: int = 1024 * 1024,
    sesion_id: str | None = None,
    duracion_ms: int | None = None,
    offsets_fotos: list[int] | None = None,
    inicio: datetime | None = None,
    esperar: bool = True,
    intervalo_s: float = 10.0,
    timeout_min: float = 90.0,
    salida_pdf: Path | None = None,
    probar_409: bool = False,
    pausa_chunk_s: float = 0.0,
    imprimir: Callable[[str], None] = print,
) -> str:
    """Corre todo el flujo del celu y devuelve el id de la sesión."""
    if not 0 < chunk <= MAX_CHUNK:
        raise ErrorSimulador(f"--chunk tiene que estar entre 1 y {MAX_CHUNK} bytes")
    celu = Celu(cliente, imprimir=imprimir)
    total = audio.stat().st_size
    if duracion_ms is None:
        from classmer.audio import ErrorAudio
        from classmer.audio import duracion_ms as medir

        try:
            duracion_ms = medir(audio)
        except ErrorAudio as e:
            raise ErrorSimulador(f"no pude medir la duración del audio ({e}); pasá --duracion-ms") from e
    from classmer.util import zona

    inicio = inicio or (datetime.now(zona(ZONA)) - timedelta(milliseconds=duracion_ms))
    sid = sesion_id or str(uuid.uuid4())
    sha = sha256_de(audio)
    eventos = armar_eventos(sid, inicio, duracion_ms, list(fotos), offsets_fotos)
    imprimir(f"Sesión {sid}: {audio.name} ({total} bytes, {duracion_ms / 1000:.0f} s), "
             f"{len(eventos)} eventos, materia {materia or 'auto'}")

    # 1. crear (idempotente por id)
    r = celu.pedir("POST", "/api/sesiones", json={
        "id": sid, "materia_id": materia, "origen_materia": "manual" if materia else "auto",
        "tipo_clase": tipo_clase, "inicio": inicio.isoformat(timespec="seconds"), "dispositivo": "simulador",
    })
    estado = celu.esperar_estado(r, 200, 201)["estado"]
    imprimir(f"  POST /api/sesiones → {r.status_code} ({estado})")

    if estado == "grabando":
        enviados: set[str] = set()

        def subir_eventos_hasta(offset_ms: float) -> None:
            for e in eventos:
                if e.id not in enviados and e.offset_ms <= offset_ms:
                    _subir_evento(celu, sid, e)
                    enviados.add(e.id)

        def subir_audio(forzar_desde: int | None = None) -> None:
            if forzar_desde is None:
                desde = celu.esperar_estado(celu.pedir("GET", f"/api/sesiones/{sid}/audio"), 200)["bytes_recibidos"]
            else:
                desde = forzar_desde
            probado = not probar_409
            with open(audio, "rb") as f:
                while desde < total:
                    # Los eventos van subiendo a medida que "avanza" la grabación.
                    subir_eventos_hasta(duracion_ms * desde / total)
                    f.seek(desde)
                    datos = f.read(chunk)
                    r = celu.pedir("PUT", f"/api/sesiones/{sid}/audio", params={"offset": desde}, content=datos,
                                   headers={"Content-Type": "application/octet-stream"})
                    cuerpo = celu.esperar_estado(r, 200, 409)
                    if r.status_code == 409:
                        imprimir(f"  409 en offset {desde}: el server tiene {cuerpo['bytes_recibidos']}, retomo de ahí")
                    desde = cuerpo["bytes_recibidos"]
                    if not probado and desde > 0:
                        # Reenvía la primera parte a propósito para ver el 409.
                        probado = True
                        f.seek(0)
                        r = celu.pedir("PUT", f"/api/sesiones/{sid}/audio", params={"offset": 0},
                                       content=f.read(min(chunk, total)),
                                       headers={"Content-Type": "application/octet-stream"})
                        cuerpo = celu.esperar_estado(r, 200, 409)
                        imprimir(f"  (prueba) PUT offset 0 otra vez → {r.status_code} {cuerpo}")
                        desde = cuerpo["bytes_recibidos"]
                    imprimir(f"  audio {desde}/{total} bytes ({100 * desde / total:.0f}%)")
                    if pausa_chunk_s:
                        time.sleep(pausa_chunk_s)
            if desde > total:
                imprimir(f"  el server tiene más bytes ({desde}) que el archivo ({total}); finalizar lo va a resolver")

        subir_audio()
        subir_eventos_hasta(float("inf"))

        # 2. finalizar, resolviendo lo que falte (§5.1)
        for _ in range(8):
            r = celu.pedir("POST", f"/api/sesiones/{sid}/finalizar", json={
                "sha256": sha, "bytes_totales": total, "duracion_ms": duracion_ms,
                "eventos_totales": len(eventos),
            })
            cuerpo = celu.esperar_estado(r, 200, 409)
            if r.status_code == 200:
                imprimir(f"  finalizar → 200 {cuerpo}")
                break
            motivo = cuerpo.get("motivo")
            imprimir(f"  finalizar → 409 {cuerpo}")
            if motivo == "faltan_bytes":
                subir_audio(cuerpo["bytes_recibidos"])
            elif motivo == "faltan_eventos":
                ids = set(celu.esperar_estado(celu.pedir("GET", f"/api/sesiones/{sid}/eventos"), 200)["ids"])
                for e in eventos:
                    if e.id not in ids:
                        _subir_evento(celu, sid, e)
            elif motivo == "hash_distinto":
                subir_audio(0)
            else:
                raise ErrorSimulador(f"finalizar: 409 con motivo desconocido {cuerpo}")
        else:
            raise ErrorSimulador("finalizar no cerró después de 8 intentos")
    else:
        imprimir("  la sesión ya estaba cerrada: no subo nada")

    if esperar:
        _seguir(celu, sid, intervalo_s, timeout_min, salida_pdf, imprimir)
    return sid


def _subir_evento(celu: Celu, sid: str, e: Evento) -> None:
    archivos = None
    if e.foto is not None:
        archivos = {"archivo": (f"{e.id}.jpg", e.foto.read_bytes(), "image/jpeg")}
    r = celu.pedir("POST", f"/api/sesiones/{sid}/eventos", data=e.form(), files=archivos)
    celu.esperar_estado(r, 200, 201)
    detalle = e.foto.name if e.foto else (e.marca or e.texto or "")
    celu.imprimir(f"  evento {e.tipo:<5} {e.offset_ms / 1000:>7.1f} s → {r.status_code}  {detalle}")


def _seguir(celu: Celu, sid: str, intervalo_s: float, timeout_min: float, salida_pdf: Path | None,
            imprimir: Callable[[str], None]) -> None:
    imprimir(f"Siguiendo la sesión cada {intervalo_s:.0f} s (Ctrl+C para dejar de mirar; el server sigue)…")
    t0 = time.monotonic()
    ultimo = None
    s: dict[str, Any] = {}
    while time.monotonic() - t0 < timeout_min * 60:
        s = celu.esperar_estado(celu.pedir("GET", f"/api/sesiones/{sid}"), 200)
        resumen = (s["estado"], s["etapa"], s["progreso"], s["error"])
        if resumen != ultimo:
            transcurrido = time.strftime("%M:%S", time.gmtime(time.monotonic() - t0))
            extra = f"  error: {s['error']}" if s["error"] else ""
            imprimir(f"  [{transcurrido}] {s['estado']:<17} etapa={s['etapa'] or '-':<16} "
                     f"progreso={100 * (s['progreso'] or 0):.0f}%{extra}")
            ultimo = resumen
        if s["estado"] in ESTADOS_FINALES:
            break
        time.sleep(intervalo_s)
    else:
        imprimir(f"Corté después de {timeout_min:g} min; la sesión sigue en {s.get('estado')}.")
        return
    if s["estado"] == "lista" and s.get("pdf"):
        r = celu.pedir("GET", s["pdf"])
        if r.status_code != 200:  # el cuerpo es un PDF: no pasar por esperar_estado (parsea JSON)
            raise ErrorSimulador(f"GET {s['pdf']}: {r.status_code} {r.text[:300]}")
        destino = salida_pdf or Path.cwd() / f"apunte-{sid[:8]}.pdf"
        destino.write_bytes(r.content)
        imprimir(f"PDF guardado en {destino} ({len(r.content)} bytes)")
    elif s["estado"] == "esperando_usuario":
        imprimir(f"La sesión espera una decisión: uv run classmer asignar {sid} --materia <id>")


# --- CLI ---------------------------------------------------------------------------------------

def _token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    from classmer.config import ConfigError, cargar_config

    try:
        token = cargar_config(exigir_archivo=False).token
    except ConfigError as e:
        raise ErrorSimulador(f"no pude leer la config para sacar el token: {e}") from e
    if not token:
        raise ErrorSimulador("no hay CLASSMER_TOKEN en Classmer-data\\.env: pasalo con --token")
    return token


def main(argv: list[str] | None = None) -> int:
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Simula la app del celu contra un server de Classmer")
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="URL base del server")
    ap.add_argument("--token", help="token Bearer (si no, se lee de Classmer-data\\.env)")
    ap.add_argument("--audio", required=True, help="archivo de audio a subir (p. ej. clase-prueba.ogg)")
    ap.add_argument("--fotos", help="carpeta con fotos .jpg")
    ap.add_argument("--materia", help="id de la materia (sin esto o 'auto': la decide la IA)")
    ap.add_argument("--tipo", choices=["teorica", "practica"], help="tipo de clase")
    ap.add_argument("--chunk", type=int, default=1024 * 1024, help="bytes por PUT (default 1 MB, máx 8 MB)")
    ap.add_argument("--sesion", help="id de una sesión a retomar (default: uuid4 nuevo)")
    ap.add_argument("--duracion-ms", type=int, help="duración del audio si no hay ffprobe")
    ap.add_argument("--offsets-fotos", help="segundos de cada foto, separados por coma (default: repartidas)")
    ap.add_argument("--inicio", help="inicio ISO 8601 (default: ahora − duración)")
    ap.add_argument("--sin-esperar", action="store_true", help="no seguir el procesamiento")
    ap.add_argument("--intervalo", type=float, default=10.0, help="segundos entre consultas de estado")
    ap.add_argument("--timeout-min", type=float, default=90.0, help="minutos máximos siguiendo la sesión")
    ap.add_argument("--pdf", help="dónde guardar el PDF (default: ./apunte-<id>.pdf)")
    ap.add_argument("--probar-409", action="store_true", help="reenvía la primera parte para provocar un 409")
    ap.add_argument("--pausa-chunk", type=float, default=0.0, help="segundos de pausa entre PUTs")
    args = ap.parse_args(argv)

    audio = Path(args.audio)
    if not audio.is_file():
        print(f"No existe el audio: {audio}", file=sys.stderr)
        return 2
    fotos: list[Path] = []
    if args.fotos:
        carpeta = Path(args.fotos)
        if not carpeta.is_dir():
            print(f"No existe la carpeta de fotos: {carpeta}", file=sys.stderr)
            return 2
        fotos = sorted(p for p in carpeta.iterdir() if p.suffix.lower() in EXTENSIONES_FOTO)
    try:
        from classmer.util import parsear_iso, tiempo_a_ms

        offsets = [tiempo_a_ms(x) for x in args.offsets_fotos.split(",") if x.strip()] if args.offsets_fotos else None
        inicio = parsear_iso(args.inicio, ZONA) if args.inicio else None
        token = _token(args)
        materia = None if args.materia in (None, "", "auto") else args.materia
        with httpx.Client(base_url=args.url.rstrip("/"), headers={"Authorization": f"Bearer {token}"},
                          timeout=httpx.Timeout(120, connect=10)) as cliente:
            simular(cliente, audio, fotos=fotos, materia=materia, tipo_clase=args.tipo, chunk=args.chunk,
                    sesion_id=args.sesion, duracion_ms=args.duracion_ms, offsets_fotos=offsets,
                    inicio=inicio, esperar=not args.sin_esperar, intervalo_s=args.intervalo,
                    timeout_min=args.timeout_min, salida_pdf=Path(args.pdf) if args.pdf else None,
                    probar_409=args.probar_409, pausa_chunk_s=args.pausa_chunk)
    except (ErrorSimulador, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCortado. Para retomar: --sesion <id> con el mismo audio.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
