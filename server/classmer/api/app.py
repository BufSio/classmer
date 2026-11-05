"""Rutas de la API v1 (§5.1). Todas piden `Authorization: Bearer <token>`."""

from __future__ import annotations

import logging
import secrets
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import FormData, Headers, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send

from classmer import __version__, adjuntos, materias, sesiones
from classmer.config import Config, ConfigError, cargar_config
from classmer.config_vivo import ConfigViva, MateriaNoExiste
from classmer.db import abrir
from classmer.importador import AudioGrande, ErrorImportacion, dir_temporal, limpiar_temporales
from classmer.pipeline import Contexto, Servicios
from classmer.sesiones import ArchivoGrande, Conflicto, ErrorDatos, NoExiste
from classmer.util import nombre_seguro

log = logging.getLogger("classmer.api")

# Lo que suma el multipart al archivo (encabezados, id, offset_ms, creado): con el adjunto en el
# tope, el Content-Length igual pasa el primer chequeo; el tope exacto lo aplica adjuntos.guardar.
MARGEN_MULTIPART = 64 * 1024


# --- modelos de entrada ------------------------------------------------------------------------

class CrearSesion(BaseModel):
    id: str
    materia_id: str | None = None
    origen_materia: str | None = None
    tipo_clase: str | None = None
    inicio: str
    dispositivo: str | None = None
    numero_clase: int | None = Field(default=None, ge=0)
    # "Importar clase" (§5.1): sólo cuentan con importada = true.
    importada: bool = False
    audio_nombre: str | None = None      # nombre original del audio: de su extensión sale audio.<ext>
    audio_bytes: int | None = Field(default=None, ge=0)   # tamaño total: más de 2 GiB → 413


class CambiarNumeracion(BaseModel):
    """PATCH /api/materias/{id}: uno de los dos."""
    proximo_numero: int | None = Field(default=None, ge=1)
    numero_clase_inicial: int | None = None


class AsignarMateria(BaseModel):
    materia_id: str


class FinalizarCelu(BaseModel):
    """La segunda pista de `finalizar` (§15): el audio del celu cuando el reloj es el principal.
    `desfase_ms` es el momento del audio del reloj en que empieza el del celu (puede ser negativo)."""
    sha256: str = Field(pattern=r"^\s*[0-9a-fA-F]{64}\s*$")
    bytes_totales: int = Field(ge=0)
    duracion_ms: int | None = Field(default=None, ge=0)
    desfase_ms: int | None = None


class Finalizar(BaseModel):
    # 64 hex: un hash mal formado (p. ej., en base64) daría hash_distinto en cada intento y el
    # audio se descartaría una y otra vez; mejor un 422.
    sha256: str = Field(pattern=r"^\s*[0-9a-fA-F]{64}\s*$")
    bytes_totales: int = Field(ge=0)
    duracion_ms: int | None = Field(default=None, ge=0)
    eventos_totales: int = Field(default=0, ge=0)
    celu: FinalizarCelu | None = None


EtapaReproceso = Literal["verificar", "segmentar", "transcribir", "linea_de_tiempo", "clasificar",
                         "revisar_transcript", "apunte", "publicar", "notificar"]  # = pipeline.ETAPAS


class Reprocesar(BaseModel):
    # Obligatorio: una etapa inválida o un cuerpo vacío dan 422; el 409 queda para el estado.
    desde: EtapaReproceso


# --- autorización ------------------------------------------------------------------------------

def token_valido(esperado: str | None, encabezado: str) -> bool:
    """`Authorization: Bearer <token>` con el token de la config (comparación en tiempo constante)."""
    tipo, _, token = encabezado.strip().partition(" ")
    if not esperado or tipo.lower() != "bearer":
        return False
    return secrets.compare_digest(token.strip().encode(), esperado.encode())


def _respuesta_401() -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "token inválido"},
                        headers={"WWW-Authenticate": "Bearer"})


class Autorizacion:
    """Middleware ASGI: toda ruta /api pide token antes de rutear o leer el cuerpo.

    Así un token malo da 401 aunque el cuerpo sea inválido (FastAPI valida el JSON antes de
    las dependencias) o la ruta no exista. Las rutas igual tienen la dependencia `autorizar`.
    """

    def __init__(self, app: ASGIApp, cfg: Config):
        self.app = app
        self.cfg = cfg

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            ruta = scope.get("path", "")
            if ruta == "/api" or ruta.startswith("/api/"):
                encabezado = Headers(scope=scope).get("authorization", "")
                if not token_valido(self.cfg.token, encabezado):
                    await _respuesta_401()(scope, receive, send)
                    return
        await self.app(scope, receive, send)


# --- app ---------------------------------------------------------------------------------------

def crear_app(
    cfg: Config | None = None,
    *,
    iniciar_worker: bool = True,
    servicios: Servicios | None = None,
    vigilar_config: bool = True,
) -> FastAPI:
    cfg = cfg or cargar_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        base = abrir(cfg)
        ctx = Contexto(cfg=cfg, base=base, servicios=servicios or Servicios())
        app.state.cfg = cfg
        app.state.base = base
        app.state.ctx = ctx
        app.state.worker = None
        # Lo que el panel dejó a medias en el área temporal de "Importar clase" (más de 24 h).
        try:
            await run_in_threadpool(limpiar_temporales, cfg)
        except Exception:
            log.exception("no pude limpiar el área temporal de importaciones")
        # config.yaml editable sin reiniciar (§13): el panel y el PATCH escriben, esto recarga.
        app.state.config_viva = ConfigViva(cfg, base)
        if vigilar_config:
            app.state.config_viva.iniciar()
        if iniciar_worker and cfg.worker.habilitado:
            from classmer.worker import Worker

            app.state.worker = Worker(ctx)
            app.state.worker.iniciar()
        try:
            yield
        finally:
            app.state.config_viva.detener()
            if app.state.worker is not None:
                await run_in_threadpool(app.state.worker.detener)

    app = FastAPI(title="Classmer", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Autorizacion, cfg=cfg)
    from classmer.mcp import crear_router as router_mcp  # MCP en /mcp (§11), con su propio chequeo de token
    app.include_router(router_mcp(cfg))
    from classmer.panel import SoloLocal, crear_router  # panel en /panel (§13), sólo desde la PC

    app.add_middleware(SoloLocal)
    app.include_router(crear_router(cfg))

    def autorizar(request: Request) -> None:
        if not token_valido(cfg.token, request.headers.get("authorization", "")):
            raise HTTPException(401, "token inválido", headers={"WWW-Authenticate": "Bearer"})

    def ctx_de(request: Request) -> Contexto:
        return request.app.state.ctx

    def despertar(request: Request) -> None:
        worker = getattr(request.app.state, "worker", None)
        if worker is not None:
            worker.despertar()

    @app.exception_handler(ErrorDatos)
    async def _datos(_r: Request, e: ErrorDatos):
        return JSONResponse(status_code=422, content={"detail": str(e)})

    @app.exception_handler(NoExiste)
    async def _no_existe(_r: Request, _e: NoExiste):
        return JSONResponse(status_code=404, content={"detail": "sesión no encontrada"})

    @app.exception_handler(ArchivoGrande)
    async def _grande(_r: Request, e: ArchivoGrande):
        return JSONResponse(status_code=413, content={"detail": str(e)})

    @app.exception_handler(Conflicto)
    async def _conflicto(_r: Request, e: Conflicto):
        return JSONResponse(status_code=409, content={"detail": str(e)})

    @app.exception_handler(adjuntos.TipoNoSoportado)
    async def _tipo(_r: Request, e: adjuntos.TipoNoSoportado):
        return JSONResponse(status_code=415, content={"detail": str(e)})

    deps = [Depends(autorizar)]

    @app.get("/api/health", dependencies=deps)
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/api/materias", dependencies=deps)
    def listar_materias(request: Request) -> list[dict[str, Any]]:
        with ctx_de(request).base.conexion() as conn:
            return materias.materias_api(cfg, conn)

    @app.patch("/api/materias/{materia_id}", dependencies=deps)
    def cambiar_numeracion(request: Request, materia_id: str, cuerpo: CambiarNumeracion) -> dict[str, Any]:
        if (cuerpo.proximo_numero is None) == (cuerpo.numero_clase_inicial is None):
            raise HTTPException(422, "mandá proximo_numero o numero_clase_inicial (uno de los dos)")
        materia = cfg.materia(materia_id)
        if materia is None:
            raise HTTPException(404, "materia no encontrada")
        base = ctx_de(request).base
        inicial = cuerpo.numero_clase_inicial
        if cuerpo.proximo_numero is not None:
            with base.conexion() as conn:
                inicial = materias.inicial_para(conn, materia, cuerpo.proximo_numero)
        try:
            materia = request.app.state.config_viva.editar_materia(materia_id, {"numero_clase_inicial": inicial})
        except MateriaNoExiste as e:
            raise HTTPException(404, "materia no encontrada") from e
        except ConfigError as e:
            raise HTTPException(422, str(e)) from e
        with base.conexion() as conn:
            return materias.materia_api(conn, materia)

    @app.get("/api/sesiones", dependencies=deps)
    def listar_sesiones(request: Request, limite: int = Query(20, ge=1, le=500)) -> list[dict[str, Any]]:
        with ctx_de(request).base.conexion() as conn:
            return sesiones.listar(cfg, conn, limite)

    @app.post("/api/sesiones", dependencies=deps)
    def crear_sesion(request: Request, cuerpo: CrearSesion) -> JSONResponse:
        """Crea la sesión (idempotente por id). Una importada guarda el audio como audio.<ext> (la
        extensión de `audio_nombre`, si está en la lista blanca; si no, audio.bin) y da 413 si
        `audio_bytes` pasa los 2 GiB. Una normal queda como siempre (audio.ogg)."""
        importada = cuerpo.importada
        with ctx_de(request).base.conexion() as conn:
            creada, fila = sesiones.crear(
                cfg, conn,
                sesion_id=cuerpo.id, materia_id=cuerpo.materia_id,
                origen_materia=cuerpo.origen_materia, tipo_clase=cuerpo.tipo_clase,
                inicio=cuerpo.inicio, dispositivo=cuerpo.dispositivo, numero_clase=cuerpo.numero_clase,
                importada=importada,
                audio_nombre=sesiones.nombre_audio_importado(cuerpo.audio_nombre) if importada else "audio.ogg",
                audio_original=cuerpo.audio_nombre if importada else None,
                audio_bytes=cuerpo.audio_bytes if importada else None,
            )
        return JSONResponse(status_code=201 if creada else 200,
                            content={"id": fila["id"], "estado": fila["estado"]})

    @app.get("/api/sesiones/{sesion_id}", dependencies=deps)
    def ver_sesion(request: Request, sesion_id: str) -> dict[str, Any]:
        with ctx_de(request).base.conexion() as conn:
            return sesiones.a_api(cfg, conn, sesiones.fila_sesion(conn, sesion_id))

    @app.post("/api/sesiones/{sesion_id}/materia", dependencies=deps)
    def asignar_materia(request: Request, sesion_id: str, cuerpo: AsignarMateria) -> dict[str, Any]:
        ctx = ctx_de(request)
        estado = sesiones.asignar_materia(cfg, ctx.base, sesion_id, cuerpo.materia_id)
        if estado == "esperando_usuario":
            despertar(request)
        with ctx.base.conexion() as conn:
            return sesiones.a_api(cfg, conn, sesiones.fila_sesion(conn, sesion_id))

    @app.get("/api/sesiones/{sesion_id}/audio", dependencies=deps)
    def ver_audio(request: Request, sesion_id: str, pista: str | None = None) -> dict[str, Any]:
        """Bytes recibidos del audio principal o, con `?pista=celu`, de la segunda pista (§15). La
        segunda repite `"pista": "celu"`: así la app reconoce un server que separa las pistas aunque
        el principal todavía no tenga bytes."""
        pista = sesiones.pista_valida(pista)
        with ctx_de(request).base.conexion() as conn:
            sesiones.fila_sesion(conn, sesion_id)
            n = sesiones.tamano(sesiones.ruta_audio(cfg, conn, sesion_id, pista))
        return {"bytes_recibidos": n, "pista": pista} if pista else {"bytes_recibidos": n}

    @app.put("/api/sesiones/{sesion_id}/audio", dependencies=deps)
    async def subir_audio(request: Request, sesion_id: str, offset: int = Query(..., ge=0),
                          pista: str | None = None) -> JSONResponse:
        pista = sesiones.pista_valida(pista)
        largo = request.headers.get("content-length")
        if largo and largo.isdigit() and int(largo) > sesiones.MAX_AUDIO_REQUEST:
            raise HTTPException(413, "máximo 8 MB por request")
        partes: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > sesiones.MAX_AUDIO_REQUEST:
                raise HTTPException(413, "máximo 8 MB por request")
            partes.append(chunk)
        datos = b"".join(partes)
        ctx = ctx_de(request)
        res = await run_in_threadpool(sesiones.agregar_audio, cfg, ctx.base, sesion_id, offset, datos, pista)
        return JSONResponse(status_code=200 if res.ok else 409,
                            content={"bytes_recibidos": res.bytes_recibidos})

    @app.post("/api/sesiones/{sesion_id}/eventos", dependencies=deps)
    async def subir_evento(request: Request, sesion_id: str) -> JSONResponse:
        form = await _leer_form(request, max_files=2, max_fields=50)
        try:
            archivo = form.get("archivo")
            subida = archivo if isinstance(archivo, UploadFile) else None
            creado = await run_in_threadpool(
                sesiones.registrar_evento, cfg, ctx_de(request).base, sesion_id,
                evento_id=_texto(form.get("id")) or "",
                tipo=_texto(form.get("tipo")) or "",
                offset_ms=_texto(form.get("offset_ms")),
                creado=_texto(form.get("creado")),
                texto=_texto(form.get("texto")),
                marca=_texto(form.get("marca")),
                archivo=subida.file if subida else None,
                nombre_archivo=subida.filename if subida else None,
            )
        finally:
            await form.close()
        return JSONResponse(status_code=201 if creado else 200, content={"id": _texto(form.get("id"))})

    @app.get("/api/sesiones/{sesion_id}/eventos", dependencies=deps)
    def ver_eventos(request: Request, sesion_id: str) -> dict[str, list[str]]:
        with ctx_de(request).base.conexion() as conn:
            sesiones.fila_sesion(conn, sesion_id)
            return {"ids": sesiones.ids_eventos(conn, sesion_id)}

    @app.post("/api/sesiones/{sesion_id}/finalizar", dependencies=deps)
    async def finalizar(request: Request, sesion_id: str, cuerpo: Finalizar) -> JSONResponse:
        res = await run_in_threadpool(
            sesiones.finalizar, cfg, ctx_de(request).base, sesion_id,
            sha256=cuerpo.sha256, bytes_totales=cuerpo.bytes_totales,
            duracion_ms=cuerpo.duracion_ms, eventos_totales=cuerpo.eventos_totales,
            celu=cuerpo.celu.model_dump() if cuerpo.celu is not None else None,
        )
        if res.ok:
            despertar(request)
        return JSONResponse(status_code=200 if res.ok else 409, content=res.cuerpo)

    @app.get("/api/sesiones/{sesion_id}/apunte.pdf", dependencies=deps)
    def apunte_pdf(request: Request, sesion_id: str) -> FileResponse:
        with ctx_de(request).base.conexion() as conn:
            sesiones.fila_sesion(conn, sesion_id)
            ruta = sesiones.ruta_pdf_actual(cfg, conn, sesion_id)
        if ruta is None:
            raise HTTPException(404, "el apunte todavía no está listo")
        # La URL es la misma para todas las versiones (§5.1): que el cliente revalide siempre.
        return FileResponse(ruta, media_type="application/pdf", filename="apunte.pdf",
                            content_disposition_type="inline", headers={"Cache-Control": "no-cache"})

    @app.post("/api/sesiones/{sesion_id}/adjuntos", dependencies=deps)
    async def subir_adjunto(request: Request, sesion_id: str) -> JSONResponse:
        """PDF de slides o notas, o una imagen (§5.1): idempotente por id, hasta 100 MB, el tipo por
        el contenido. Responde el adjunto y la sesión (con `adjuntos_sin_usar`)."""
        largo = request.headers.get("content-length")
        if largo and largo.isdigit() and int(largo) > adjuntos.MAX_ADJUNTO + MARGEN_MULTIPART:
            raise HTTPException(413, f"el adjunto pesa más de {adjuntos.MAX_ADJUNTO // (1024 * 1024)} MB")
        ctx = ctx_de(request)
        with ctx.base.conexion() as conn:  # 404 antes de recibir 100 MB para una sesión que no existe
            sesiones.fila_sesion(conn, sesion_id)
        form = await _leer_form(request, max_files=1, max_fields=20)
        try:
            archivo = form.get("archivo")
            subida = archivo if isinstance(archivo, UploadFile) else None
            creado, info = await run_in_threadpool(
                adjuntos.guardar, cfg, ctx.base, sesion_id,
                adjunto_id=_texto(form.get("id")) or "",
                archivo=subida.file if subida else None,
                nombre_original=subida.filename if subida else None,
                offset_ms=_texto(form.get("offset_ms")),
                creado=_texto(form.get("creado")),
            )
        finally:
            await form.close()
        with ctx.base.conexion() as conn:
            info["sesion"] = sesiones.a_api(cfg, conn, sesiones.fila_sesion(conn, sesion_id))
        return JSONResponse(status_code=201 if creado else 200, content=info)

    @app.post("/api/sesiones/{sesion_id}/regenerar", dependencies=deps)
    def regenerar(request: Request, sesion_id: str) -> dict[str, Any]:
        """"Regenerar el apunte con los adjuntos" (app): re-ejecuta desde `apunte` una clase lista o en
        error. Cuesta lo que un apunte nuevo: la app pide confirmación."""
        from classmer.reproceso import ESTADOS_REGENERAR
        from classmer.reproceso import reprocesar as _reprocesar

        ctx = ctx_de(request)
        with ctx.base.conexion() as conn:
            sesiones.fila_sesion(conn, sesion_id)
        try:
            _reprocesar(ctx, sesion_id, "apunte", estados=ESTADOS_REGENERAR)
        except LookupError as e:
            raise HTTPException(409, str(e)) from e
        despertar(request)
        with ctx.base.conexion() as conn:
            return sesiones.a_api(cfg, conn, sesiones.fila_sesion(conn, sesion_id))

    @app.post("/api/importar", dependencies=deps)
    async def importar(request: Request) -> JSONResponse:
        """Plan B: audio, fotos y material sueltos en un multipart → sesión importada en cola. Campos:
        `audio`, `fotos[]`/`fotos`, `material[]`/`material` (PDF o imágenes, como adjuntos),
        `materia_id`, `tipo_clase`, `inicio` y `numero_clase` (opcionales). 413 si el audio pasa los
        2 GiB; el material rechazado no frena la importación (va en `material_rechazado`)."""
        from classmer import importador
        from classmer.util import leer_json, parsear_iso

        ctx = ctx_de(request)
        # El 413 del audio de más de 2 GiB sale mientras llega (_leer_form con tope_audio).
        form = await _leer_form(request, max_files=500, max_fields=50, tope_audio=True)
        tmp = Path(tempfile.mkdtemp(prefix="api-", dir=dir_temporal(cfg)))
        try:
            audio = form.get("audio")
            if not isinstance(audio, UploadFile):
                raise HTTPException(422, "falta el archivo de audio (campo audio)")
            materia_id = _texto(form.get("materia_id")) or None
            if materia_id == "auto":
                materia_id = None
            inicio_txt = _texto(form.get("inicio"))
            try:
                inicio = parsear_iso(inicio_txt, cfg.zona_horaria) if inicio_txt else None
            except ValueError as e:
                raise HTTPException(422, f"inicio inválido: {inicio_txt!r}") from e
            numero_txt = (_texto(form.get("numero_clase")) or "").strip()
            try:
                numero_clase = int(numero_txt) if numero_txt else None
            except ValueError as e:
                raise HTTPException(422, f"numero_clase inválido: {numero_txt!r}") from e
            if numero_clase is not None and numero_clase < 0:
                raise HTTPException(422, "numero_clase no puede ser negativo")
            ruta_audio = tmp / ("audio" + (_extension_valida(audio.filename).lower() or ".bin"))
            await run_in_threadpool(_copiar, audio.file, ruta_audio)
            fotos: list[Path] = []
            subidas = [f for clave in ("fotos[]", "fotos") for f in form.getlist(clave)
                       if isinstance(f, UploadFile)]
            for i, foto in enumerate(subidas):
                # Conserva el nombre original, pero válido en Windows ('a?b.jpg', '..').
                destino = tmp / "fotos" / f"{i:03d}" / _nombre_foto(foto.filename, i)
                try:
                    await run_in_threadpool(_copiar, foto.file, destino)
                except OSError as e:
                    raise HTTPException(422, f"foto inválida: {foto.filename!r} ({e.strerror or e})") from e
                fotos.append(destino)
            material: list[tuple[Path, str]] = []
            subidos = [m for clave in ("material[]", "material") for m in form.getlist(clave)
                       if isinstance(m, UploadFile)]
            for i, m in enumerate(subidos):
                # El nombre lo arma adjuntos.guardar con el original; acá sólo hace falta el contenido.
                destino = tmp / "material" / f"{i:03d}"
                try:
                    await run_in_threadpool(_copiar, m.file, destino)
                except OSError as e:
                    raise HTTPException(422, f"material inválido: {m.filename!r} ({e.strerror or e})") from e
                material.append((destino, m.filename or f"material_{i + 1:03d}"))
            try:
                sesion_id = await run_in_threadpool(
                    lambda: importador.importar_archivos(
                        ctx, ruta_audio, materia_id=materia_id, fotos=fotos, inicio=inicio,
                        tipo_clase=_texto(form.get("tipo_clase")) or None,
                        usar_mtime_fotos=False, usar_mtime_audio=False,
                        nombre_original=audio.filename, numero_clase=numero_clase, material=material,
                    )
                )
            except AudioGrande as e:
                raise HTTPException(413, str(e)) from e
            except (ErrorImportacion, ErrorDatos) as e:
                raise HTTPException(422, str(e)) from e
        finally:
            await form.close()
            shutil.rmtree(tmp, ignore_errors=True)
        despertar(request)
        contenido: dict[str, Any] = {"id": sesion_id, "estado": "en_cola"}
        resumen = leer_json(cfg.sesion_dir(sesion_id) / "importacion.json") or {}
        if resumen.get("material_rechazado"):
            contenido["material_rechazado"] = resumen["material_rechazado"]
        return JSONResponse(status_code=201, content=contenido)

    @app.post("/api/sesiones/{sesion_id}/reprocesar", dependencies=deps)
    def reprocesar(request: Request, sesion_id: str, cuerpo: Reprocesar) -> dict[str, str]:
        from classmer.reproceso import reprocesar as _reprocesar

        ctx = ctx_de(request)
        with ctx.base.conexion() as conn:
            sesiones.fila_sesion(conn, sesion_id)
        try:
            _reprocesar(ctx, sesion_id, cuerpo.desde)
        except LookupError as e:
            raise HTTPException(409, str(e)) from e
        despertar(request)
        return {"id": sesion_id, "estado": "en_cola"}

    return app


class _MultipartConTopeAudio(MultiPartParser):
    """El multipart de POST /api/importar con el tope del audio (2 GiB) mientras llega.

    Starlette guarda cada archivo en la carpeta temporal del sistema sin límite (max_part_size es sólo
    para los campos de texto): sin esto, un audio de 5 GB se escribía entero en %TEMP% antes del 413.
    Cuenta los bytes de cada parte de archivo del campo `audio` y da AudioGrande al pasar el tope."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._parte_audio: Any = None
        self._bytes_audio = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        parte = self._current_part
        if parte.file is not None and parte.field_name == "audio":
            if parte is not self._parte_audio:
                self._parte_audio, self._bytes_audio = parte, 0
            self._bytes_audio += end - start
            if self._bytes_audio > sesiones.MAX_AUDIO_IMPORTADO:
                raise AudioGrande(sesiones.mensaje_audio_grande())
        super().on_part_data(data, start, end)


async def _form_con_tope_audio(request: Request, **limites: Any) -> FormData:
    stream = request.stream()
    try:
        return await _MultipartConTopeAudio(request.headers, stream, **limites).parse()
    except AudioGrande:
        # Se termina de leer el pedido sin guardarlo: así el cliente recibe el 413 y no una conexión
        # cortada (como el panel). Starlette ya cerró los archivos que había abierto.
        try:
            async for _ in stream:
                pass
        except ClientDisconnect:
            pass
        raise


async def _leer_form(request: Request, *, tope_audio: bool = False, **limites: Any) -> FormData:
    """Sólo un multipart mal armado es 422 (la app lo toma como permanente y descarta el evento).
    Un error de disco al guardar las partes es 503: la app reintenta. Con `tope_audio` (POST
    /api/importar), un audio de más de 2 GiB da 413 apenas pasa el tope, no después de recibirlo."""
    try:
        if tope_audio and request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            return await _form_con_tope_audio(request, **limites)
        return await request.form(**limites)
    except AudioGrande as e:
        raise HTTPException(413, str(e)) from e
    except (StarletteHTTPException, MultiPartException) as e:
        detalle = getattr(e, "detail", None) or getattr(e, "message", None) or str(e)
        raise HTTPException(422, f"multipart inválido: {detalle}") from e
    except ClientDisconnect as e:
        # El celu cortó a mitad: no hay a quién responder; un código reintentable por las dudas.
        raise HTTPException(408, "se cortó la subida") from e
    except OSError as e:
        log.exception("error de disco al leer un multipart")
        raise HTTPException(503, "error de disco en el server") from e


def _extension_valida(nombre: str | None) -> str:
    """La extensión del nombre si sirve en Windows; si no, ''."""
    extension = Path(nombre or "").suffix
    return extension if extension and nombre_seguro(extension) == extension else ""


def _nombre_foto(nombre: str | None, i: int) -> str:
    """Nombre de la foto válido en Windows, con su extensión (o foto_NNN.jpg)."""
    original = Path(nombre or "").name
    extension = _extension_valida(original) or ".jpg"
    base = Path(original).stem if Path(original).suffix else original
    limpio = nombre_seguro(base, 100) if base else ""
    if not limpio or limpio == "Sin titulo":
        return f"foto_{i:03d}{extension}"
    return limpio + extension


def _texto(valor: Any) -> str | None:
    if valor is None or isinstance(valor, UploadFile):
        return None
    return str(valor)


def _copiar(origen: Any, destino: Path) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "wb") as f:
        shutil.copyfileobj(origen, f, 1024 * 1024)
