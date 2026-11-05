"""Panel (§13), "Importar clase": la recepción de los archivos y lo que muestran el formulario y la clase.

- `recibir` lee el multipart del navegador mientras llega y escribe cada archivo directo en el área
  temporal del importador (Classmer-data/importaciones/<token>/), con el tope de su tipo
  (`importador.ArchivoTemporal`): nada pasa por la carpeta temporal del sistema ni queda entero en
  memoria, y un audio de más de 2 GiB se corta al pasar el tope.
- La lógica de la importación es la del importador (`analizar_archivos`, `importar_clase`): acá no
  se decide nada del inicio ni de las fotos, sólo se arma lo que se ve.
"""

from __future__ import annotations

import errno
import shutil
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.concurrency import run_in_threadpool
from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from starlette.requests import ClientDisconnect, Request

from classmer import audio as audio_mod
from classmer import importador, sesiones
from classmer.ia.facturacion import API, MAX, Facturacion
from classmer.importador import ArchivoTemporal, AudioGrande
from classmer.sesiones import ArchivoGrande
from classmer.util import escribir_json, leer_json, ms_a_hhmmss, parsear_iso, zona

# Lo que ofrece el selector del audio: lo que lee ffmpeg (el tipo de verdad lo dice ffprobe).
ACEPTA_AUDIO = ",".join(("audio/*", "video/mp4", "video/3gpp", "application/ogg", *audio_mod.EXTENSIONES_AUDIO))
# Campos de archivo de cada paso → tipo en el área temporal (con [] o sin, como /api/importar).
ARCHIVOS_ANALIZAR = {"audio": "audio", "fotos": "fotos", "fotos[]": "fotos"}
ARCHIVOS_IMPORTAR = {"material": "material", "material[]": "material"}
ANALISIS = "analisis.json"   # en <token>/: el audio, las fotos y lo que respondió el último Analizar
REANALIZANDO = "reanalizando"   # en analisis.json: hay un Analizar de nuevo en curso (o se cortó)
MAX_FOTOS_FORM = 300
MAX_MATERIAL_FORM = 20
MAX_CAMPOS_FORM = 30
MAX_CAMPO = 64 * 1024        # un campo de texto (token, materia, inicio…)
TANDA = 1024 * 1024          # se escribe al disco de a 1 MiB (en un hilo: no frena al resto del server)
COSTOS_PROMEDIADOS = 10      # los últimos apuntes con costo, para el aviso de "Importar y procesar"
SIN_COSTOS = "alrededor de US$ 1–2 con la API"   # si ningún apunte tiene costo todavía
DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
TEXTO_TIPO = {"teorica": "Teórica", "practica": "Práctica"}
FALTA_ANALISIS = "falta el análisis: elegí el audio y esperá a que se analice"
# El aviso del importador sobre el material que no entró: el panel ya lo muestra con su motivo.
AVISO_MATERIAL = "de material no entraron"


class ErrorRecepcion(ValueError):
    """El formulario no se pudo leer (mal formado, demasiados campos o archivos): 422."""


@dataclass
class Recibido:
    token: str | None
    campos: dict[str, str]
    archivos: dict[str, list[tuple[Path, str]]]   # tipo → [(ruta en el área temporal, nombre original)]
    rechazados: list[dict[str, str]]              # [{"tipo", "nombre", "motivo"}]: pasaron el tope (no el audio)


@dataclass
class _Parte:
    campo: str = ""
    nombre: str | None = None                      # el del archivo; None = campo de texto
    datos: bytearray = field(default_factory=bytearray)
    destino: ArchivoTemporal | None = None
    tipo: str | None = None


TokenDe = Callable[[dict[str, str]], Awaitable[str]]


class _Lector:
    """Los callbacks de python-multipart sólo anotan lo que llega; `procesar` lo escribe después de
    cada pedazo del pedido (abrir y escribir van en un hilo, como hace Starlette con sus archivos)."""

    def __init__(self, cfg: Any, archivos: Mapping[str, str], token_de: TokenDe, reemplazar: Iterable[str],
                 max_archivos: int):
        self.cfg = cfg
        self.tipos = dict(archivos)
        self.token_de = token_de
        self.reemplazar = set(reemplazar)
        self.max_archivos = max_archivos
        self.token: str | None = None
        self.campos: dict[str, str] = {}
        self.archivos: dict[str, list[tuple[Path, str]]] = {}
        self.rechazados: list[dict[str, str]] = []
        self._reemplazados: set[str] = set()
        self._eventos: list[tuple[str, _Parte, bytes]] = []
        self._abiertos: list[ArchivoTemporal] = []
        self._parte = _Parte()
        self._encabezado = b""
        self._valor = b""
        self._disposicion = b""
        self._n_archivos = 0
        self._n_campos = 0
        self.completo = False   # llegó el cierre (--boundary--): finalize() de python-multipart no lo mira

    def callbacks(self) -> dict[str, Any]:
        return {"on_part_begin": self._inicio_parte, "on_header_field": self._campo_encabezado,
                "on_header_value": self._valor_encabezado, "on_header_end": self._fin_encabezado,
                "on_headers_finished": self._fin_encabezados, "on_part_data": self._datos,
                "on_part_end": self._fin_parte, "on_end": self._fin}

    # --- callbacks (sin E/S) ---
    def _inicio_parte(self) -> None:
        self._parte = _Parte()
        self._disposicion = b""

    def _campo_encabezado(self, data: bytes, start: int, end: int) -> None:
        self._encabezado += data[start:end]

    def _valor_encabezado(self, data: bytes, start: int, end: int) -> None:
        self._valor += data[start:end]

    def _fin_encabezado(self) -> None:
        if self._encabezado.lower() == b"content-disposition":
            self._disposicion = self._valor
        self._encabezado = self._valor = b""

    def _fin_encabezados(self) -> None:
        _, opciones = parse_options_header(self._disposicion)
        if b"name" not in opciones:
            raise ErrorRecepcion("el formulario llegó mal formado (una parte sin nombre)")
        self._parte.campo = opciones[b"name"].decode("utf-8", errors="replace")
        if b"filename" in opciones:
            self._n_archivos += 1
            if self._n_archivos > self.max_archivos:
                raise ErrorRecepcion(f"demasiados archivos: hasta {self.max_archivos} por vez")
            # Sólo el nombre: sin carpetas (el navegador ya manda así, pero por las dudas).
            nombre = opciones[b"filename"].decode("utf-8", errors="replace").replace("\\", "/").split("/")[-1]
            self._parte.nombre = nombre
            self._eventos.append(("archivo", self._parte, b""))
        else:
            self._n_campos += 1
            if self._n_campos > MAX_CAMPOS_FORM:
                raise ErrorRecepcion(f"demasiados campos: hasta {MAX_CAMPOS_FORM}")

    def _datos(self, data: bytes, start: int, end: int) -> None:
        if self._parte.nombre is None:
            if len(self._parte.datos) + (end - start) > MAX_CAMPO:
                raise ErrorRecepcion(f"el campo {self._parte.campo!r} es demasiado largo")
            self._parte.datos += data[start:end]
        else:
            self._eventos.append(("datos", self._parte, data[start:end]))

    def _fin_parte(self) -> None:
        if self._parte.nombre is None:
            self.campos[self._parte.campo] = self._parte.datos.decode("utf-8", errors="replace")
        else:
            self._eventos.append(("fin", self._parte, b""))

    def _fin(self) -> None:
        self.completo = True

    # --- E/S ---
    async def procesar(self) -> None:
        eventos, self._eventos = self._eventos, []
        for clase, parte, datos in eventos:
            if clase == "archivo":
                await self._abrir(parte)
            elif parte.destino is None:
                continue  # sin archivo elegido, un campo que no se espera o algo que pasó el tope
            elif clase == "datos":
                parte.datos += datos
                if len(parte.datos) >= TANDA:
                    await self._volcar(parte)
            else:  # fin
                await self._volcar(parte)
                if parte.destino is not None:
                    destino, parte.destino = parte.destino, None
                    ruta = await run_in_threadpool(destino.cerrar)
                    self._abiertos.remove(destino)
                    self.archivos.setdefault(parte.tipo or "", []).append((ruta, parte.nombre or ruta.name))

    async def _abrir(self, parte: _Parte) -> None:
        tipo = self.tipos.get(parte.campo)
        if not parte.nombre or tipo is None:
            return  # un input sin archivo elegido manda una parte vacía; un campo que no se espera, se ignora
        if self.token is None:
            self.token = await self.token_de(self.campos)
        if tipo in self.reemplazar and tipo not in self._reemplazados:
            self._reemplazados.add(tipo)
            carpeta = importador.ruta_temporal(self.cfg, self.token) / tipo
            await run_in_threadpool(shutil.rmtree, carpeta, True)
        parte.tipo = tipo
        parte.destino = await run_in_threadpool(ArchivoTemporal, self.cfg, self.token, tipo, parte.nombre)
        self._abiertos.append(parte.destino)

    async def _volcar(self, parte: _Parte) -> None:
        if parte.destino is None or not parte.datos:
            return
        datos = bytes(parte.datos)
        parte.datos.clear()
        destino = parte.destino
        try:
            await run_in_threadpool(destino.escribir, datos)
        except AudioGrande:
            parte.destino = None
            self._abiertos.remove(destino)
            raise
        except ArchivoGrande as e:  # una foto o un material: no entra, pero el resto sigue
            parte.destino = None
            self._abiertos.remove(destino)
            self.rechazados.append({"tipo": parte.tipo or "", "nombre": parte.nombre or "", "motivo": str(e)})

    async def terminar(self) -> None:
        if self.token is None and self.campos.get("token", "").strip():
            self.token = await self.token_de(self.campos)

    async def descartar(self) -> None:
        """Si el pedido se corta o falla, lo que estaba a medio escribir no queda."""
        for destino in self._abiertos:
            await run_in_threadpool(destino.descartar)
        self._abiertos.clear()


async def recibir(request: Request, cfg: Any, *, archivos: Mapping[str, str], token_de: TokenDe,
                  reemplazar: Iterable[str] = (), max_archivos: int = MAX_FOTOS_FORM) -> Recibido:
    """Lee el multipart del pedido y deja cada archivo en el área temporal.

    - `archivos`: campo del formulario → tipo del área temporal (audio, fotos o material). Las partes
      de otros campos y las de un input sin archivo elegido se ignoran.
    - `token_de(campos)` da el token del análisis (lo valida o crea uno) la primera vez que hace
      falta: al llegar el primer archivo o, si no hay archivos y vino el campo `token`, al final. Por
      eso el campo `token` va antes que los archivos (el JS del panel lo manda así).
    - `reemplazar`: los tipos cuyos archivos anteriores en ese token se borran al llegar el primero
      nuevo (Analizar de nuevo con otras fotos, otro intento de importar con su material).
    - Topes de importador.TOPES_TEMPORAL mientras llega: una foto o un material que pasa el suyo va a
      `rechazados` y el resto sigue; un audio de más de 2 GiB da AudioGrande (se termina de leer el
      pedido sin guardarlo, así el navegador recibe el 413 en vez de una conexión cortada).
    ErrorRecepcion si el formulario no se puede leer o llegó sin el cierre (el último archivo quedaría
    a medias sin que nadie se entere); ErrorImportacion si el token no vale."""
    tipo, opciones = parse_options_header(request.headers.get("content-type", ""))
    limite = opciones.get(b"boundary")
    if tipo != b"multipart/form-data" or not limite:
        raise ErrorRecepcion("se esperaba un formulario multipart/form-data")
    lector = _Lector(cfg, archivos, token_de, reemplazar, max_archivos)
    parser = MultipartParser(limite, lector.callbacks())
    stream = request.stream()
    try:
        async for pedazo in stream:
            parser.write(pedazo)
            await lector.procesar()
        parser.finalize()
        if not lector.completo:
            raise ErrorRecepcion("el formulario llegó cortado")
        await lector.procesar()
        await lector.terminar()
    except AudioGrande:
        await lector.descartar()
        try:
            async for _ in stream:
                pass
        except ClientDisconnect:
            pass
        raise
    except FormParserError as e:
        await lector.descartar()
        raise ErrorRecepcion("el formulario llegó mal formado") from e
    except BaseException:
        await lector.descartar()
        raise
    return Recibido(lector.token, lector.campos, lector.archivos, lector.rechazados)


# --- Analizar --------------------------------------------------------------------------------------

def _ms(texto: Any) -> int | None:
    """`audio_modificado` (File.lastModified, ms desde 1970); None si no sirve."""
    try:
        ms = int(str(texto if texto is not None else "").strip())
    except ValueError:
        return None
    return ms if 946_684_800_000 <= ms <= 4_102_444_800_000 else None  # 2000–2100: un 0 no es una fecha


def modificado(texto: Any, nombre_zona: str) -> datetime | None:
    """`audio_modificado` → datetime en la zona de Classmer (el `mtime_audio` del importador)."""
    ms = _ms(texto)
    return None if ms is None else datetime.fromtimestamp(ms / 1000, zona(nombre_zona))


def _texto_fecha(dt: datetime) -> str:
    return f"{DIAS[dt.weekday()]} {dt:%d/%m/%Y %H:%M:%S}"


def vista_analisis(nombre_zona: str, token: str, resultado: dict[str, Any], fotos_recibidas: list[tuple[Path, str]],
                   rechazadas: list[dict[str, str]]) -> dict[str, Any]:
    """La respuesta de POST /panel/importar/analizar: lo de `analizar_archivos` (con los nombres
    originales de las fotos) más lo que necesita el formulario para mostrarlo: el inicio para el
    datetime-local (`inicio_local`, en la zona de Classmer), el texto de cada candidato, el minuto de
    cada foto y el resumen. `fotos_recibidas`: (ruta, nombre original) en el orden en que se pasaron
    a analizar_archivos. Las fotos que pasaron el tope al recibirlas van con las ilegibles."""
    tz = zona(nombre_zona)
    nombres_fotos = [nombre for _, nombre in fotos_recibidas]
    originales = {ruta.name: nombre for ruta, nombre in reversed(fotos_recibidas)}
    inicio = parsear_iso(resultado["inicio"], nombre_zona).astimezone(tz)
    offset = inicio.strftime("%z")
    candidatos = []
    for c in resultado.get("candidatos_inicio") or []:
        dt = parsear_iso(c["inicio"], nombre_zona).astimezone(tz)
        candidatos.append({**c, "inicio_local": f"{dt:%Y-%m-%dT%H:%M:%S}", "texto": _texto_fecha(dt)})
    fotos = []
    for f in resultado.get("fotos") or []:
        nombre = nombres_fotos[f["indice"]] if 0 <= f["indice"] < len(nombres_fotos) else f["nombre"]
        hora = parsear_iso(f["hora"], nombre_zona).astimezone(tz) if f.get("hora") else None
        fotos.append({**f, "nombre": nombre, "hora_texto": f"{hora:%d/%m %H:%M:%S}" if hora else None,
                      "minuto": ms_a_hhmmss(f["offset_ms"]) if f.get("offset_ms") is not None else None})
    # Las ilegibles traen el nombre del archivo en el área temporal (saneado): va el original.
    ilegibles = [{**f, "nombre": originales.get(f.get("nombre"), f.get("nombre"))}
                 for f in resultado.get("fotos_ilegibles") or []]
    ilegibles += [{"nombre": r["nombre"], "motivo": r["motivo"]} for r in rechazadas if r.get("tipo") == "fotos"]
    return {
        **resultado,
        "token": token,
        "inicio_local": f"{inicio:%Y-%m-%dT%H:%M:%S}",
        "inicio_texto": _texto_fecha(inicio),
        "offset_zona": f"{offset[:3]}:{offset[3:]}" if offset else "",
        "margen_ms": importador.MARGEN_MS,
        "candidatos_inicio": candidatos,
        "fotos": fotos,
        "fotos_ilegibles": ilegibles,
        "resumen_fotos": {
            "total": len(fotos) + len(ilegibles),
            "con_hora": sum(1 for f in fotos if f.get("hora")),
            "sin_hora": sum(1 for f in fotos if not f.get("hora")),
            "fuera_de_rango": sum(1 for f in fotos if f.get("fuera_de_rango")),
            "ilegibles": len(ilegibles),
        },
    }


def audio_a_analizar(carpeta: Path, nuevo: tuple[Path, str] | None,
                     modificado_ms: str | None) -> tuple[Path, str, int | None] | None:
    """El audio a analizar: el que acaba de llegar o, al volver a analizar sin audio, el del análisis
    anterior (que tiene que seguir dentro de la carpeta del token). Devuelve (ruta, nombre original,
    lastModified en ms o None) o None si no hay audio."""
    ms = _ms(modificado_ms)
    if nuevo is not None:
        return nuevo[0], nuevo[1], ms
    audio = (leer_analisis(carpeta) or {}).get("audio") or {}
    ruta = _dentro(carpeta, audio.get("archivo"))
    if ruta is None:
        return None
    return ruta, audio.get("nombre") or ruta.name, ms or _ms(audio.get("modificado_ms"))


def leer_analisis(carpeta: Path) -> dict[str, Any] | None:
    try:
        analisis = leer_json(carpeta / ANALISIS)
    except (OSError, ValueError):
        return None
    return analisis if isinstance(analisis, dict) else None


def marcar_reanalisis(carpeta: Path) -> None:
    """Antes de volver a analizar un token: las fotos anteriores se borran apenas llega la primera
    nueva, así que hasta que el análisis nuevo se escriba, el que está ya no dice qué hay en disco. Si
    el pedido se corta, la marca queda y "Importar y procesar" pide volver a analizar (no importa con
    menos fotos de las que se mostraron)."""
    analisis = leer_analisis(carpeta)
    if analisis is not None and not analisis.get(REANALIZANDO):
        escribir_json(carpeta / ANALISIS, {**analisis, REANALIZANDO: True})


def _dentro(carpeta: Path, relativa: Any) -> Path | None:
    """`carpeta/relativa` si es un archivo dentro de la carpeta del token (nunca afuera)."""
    if not isinstance(relativa, str) or not relativa:
        return None
    try:
        ruta = (carpeta / relativa).resolve()
        if ruta.is_relative_to(carpeta.resolve()) and ruta.is_file():
            return ruta
    except OSError:
        pass
    return None


class CampoInvalido(ValueError):
    """Un dato del formulario de importar no sirve: vuelve como {detail, campo} (422)."""

    def __init__(self, campo: str, mensaje: str):
        super().__init__(mensaje)
        self.campo = campo


def datos_importacion(cfg: Any, campos: Mapping[str, str], carpeta: Path) -> dict[str, Any]:
    """Valida "Importar y procesar" contra el análisis del token y arma los argumentos de
    importador.importar_clase (salvo ctx y material). CampoInvalido si algo no sirve."""
    analisis = leer_analisis(carpeta)
    if not analisis or not isinstance(analisis.get("audio"), dict):
        raise CampoInvalido("audio", "El análisis no terminó: esperá a que termine o volvé a elegir el audio.")
    audio = _dentro(carpeta, analisis["audio"].get("archivo"))
    if audio is None:
        raise CampoInvalido("audio", "El audio del análisis ya no está: volvé a elegirlo.")
    if analisis.get(REANALIZANDO):
        raise CampoInvalido("fotos", "El último análisis de las fotos no terminó (¿se cortó la subida?): "
                                     "hay que volver a analizarlas.")
    rutas = [_dentro(carpeta, f.get("archivo")) for f in analisis.get("fotos") or [] if isinstance(f, dict)]
    fotos = [r for r in rutas if r is not None]
    if len(fotos) < len(rutas):  # nunca se importa con menos fotos de las que mostró el análisis
        raise CampoInvalido("fotos", f"{len(rutas) - len(fotos)} foto(s) del análisis ya no están: "
                                     "hay que volver a analizarlas.")

    materia_id: str | None = (campos.get("materia_id") or "").strip()
    if materia_id in ("", "auto"):
        materia_id = None
    elif cfg.materia(materia_id) is None:
        raise CampoInvalido("materia_id", f"La materia {materia_id!r} no existe.")
    tipo = (campos.get("tipo_clase") or "").strip() or None
    try:
        sesiones.validar_materia_y_tipo(cfg, materia_id, tipo)
    except sesiones.ErrorDatos as e:
        permitidos = cfg.materia(materia_id).tipos_clase if materia_id else list(sesiones.TIPOS_CLASE)
        raise CampoInvalido("tipo_clase", f"Tipo de clase inválido: {tipo!r} (opciones: "
                                          f"{', '.join(permitidos) or 'ninguno'}).") from e
    numero = None
    texto = (campos.get("numero_clase") or "").strip()
    if materia_id is not None and texto:  # en Auto lo pone el server al asignar la materia
        try:
            numero = int(texto)
        except ValueError as e:
            raise CampoInvalido("numero_clase", f"Número de clase inválido: {texto!r}.") from e
        if numero < 0:
            raise CampoInvalido("numero_clase", "El número de clase no puede ser negativo.")
    texto = (campos.get("inicio") or "").strip()
    if not texto:
        raise CampoInvalido("inicio", "Poné el inicio de la clase (fecha y hora).")
    try:
        inicio = parsear_iso(texto, cfg.zona_horaria)
    except ValueError:
        inicio = None
    if inicio is None or not 2000 <= inicio.year <= 2100:
        raise CampoInvalido("inicio", f"Inicio inválido: {texto!r}.")
    candidatos = [c for c in (analisis.get("resultado") or {}).get("candidatos_inicio") or [] if isinstance(c, dict)]
    return {
        "audio": audio,
        "nombre_original": analisis["audio"].get("nombre") or audio.name,
        "fotos": fotos,
        "materia_id": materia_id,
        "tipo_clase": tipo,
        "numero_clase": numero,
        "inicio": inicio,
        "mtime_audio": modificado(analisis["audio"].get("modificado_ms"), cfg.zona_horaria),
        "inicio_elegido": origen_elegido(inicio, candidatos, cfg.zona_horaria),
        "candidatos_panel": candidatos_para_guardar(candidatos),
    }


def origen_elegido(inicio: datetime, candidatos: list[dict[str, Any]], nombre_zona: str) -> str:
    """De dónde sacó el usuario el inicio que mandó: el `origen` del primer candidato del mismo minuto (tocó
    ese botón) o "manual" (lo escribió)."""
    for c in candidatos:
        try:
            if abs(parsear_iso(c["inicio"], nombre_zona) - inicio) < timedelta(minutes=1):
                return c["origen"]
        except (KeyError, TypeError, ValueError):
            continue
    return "manual"


def candidatos_para_guardar(candidatos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Los candidatos del análisis para importacion.json, sin lo que es sólo de la vista."""
    return [{k: v for k, v in c.items() if k not in ("inicio_local", "texto")} for c in candidatos]


# --- "Importar y procesar": el aviso del costo -------------------------------------------------------

def texto_error_disco(e: OSError) -> str:
    """Un OSError del área temporal o de la carpeta de la sesión, para leer en el panel (sin rutas)."""
    if e.errno == errno.ENOSPC:  # en Windows, ERROR_DISK_FULL también llega como ENOSPC
        return "no hay lugar en el disco de la PC: liberá espacio y volvé a intentar"
    return f"error de disco en el server ({e.strerror or type(e).__name__})"


def texto_confirmacion(cfg: Any, base: Any, conn: Any, modo_texto: str) -> str:
    """El confirm() de "Importar y procesar": que se genera un apunte nuevo y cuánto cuesta, con la
    facturación de ahora (el primer medio que se probaría, como en Modelos) y el promedio de lo que
    costaron los últimos apuntes (apuntes.costo; con el plan Max es el equivalente API)."""
    medios, _ = Facturacion(cfg, base).plan()
    costos = [f["costo"] for f in conn.execute(
        "SELECT costo FROM apuntes WHERE costo > 0 ORDER BY id DESC LIMIT ?", (COSTOS_PROMEDIADOS,))]
    if costos:
        cuantos = "el último apunte" if len(costos) == 1 else f"los últimos {len(costos)} apuntes"
        cifra = f"unos US$ {sum(costos) / len(costos):.2f} con la API (el promedio de {cuantos})"
    else:
        cifra = SIN_COSTOS
    primero = medios[0] if medios else None
    if primero == MAX:
        costo = f"Ahora va por el plan Max: no se cobra aparte (serían {cifra})."
    elif primero == API:
        costo = f"Ahora va por la API: {cifra}."
    else:
        costo = f"Ahora ni la API ni el plan Max están disponibles: el apunte espera a que alguno ande ({cifra})."
    return ("Se va a generar un apunte nuevo de esta clase con Claude.\n\n"
            f"Facturación: {modo_texto}. {costo}\n\n¿Importar y procesar?")


# --- la página de la clase ---------------------------------------------------------------------------

def texto_origen_inicio(imp: dict[str, Any]) -> str:
    """De dónde salió el inicio de una clase importada, para leer (importacion.json)."""
    origen = imp.get("origen_inicio")
    candidatos = [c for c in imp.get("candidatos_inicio") or [] if isinstance(c, dict)]
    if origen == "parametro":
        elegido = imp.get("inicio_elegido")
        if elegido == "manual":
            propuesto = next((c for c in candidatos if c.get("elegido")), None)
            if propuesto and propuesto.get("etiqueta"):
                etiqueta = propuesto["etiqueta"]
                return f"puesto a mano en el panel (el server proponía otro: {etiqueta[:1].lower()}{etiqueta[1:]})"
            return "puesto a mano en el panel"
        if elegido:
            c = next((c for c in candidatos if c.get("origen") == elegido), {})
            etiqueta = c.get("etiqueta") or importador.ETIQUETAS_INICIO.get(elegido, elegido)
            return f"elegido en el panel: {etiqueta[:1].lower()}{etiqueta[1:]}"
        return "lo dio quien importó (--inicio o el campo inicio)"
    if origen == "calendario":
        return "el evento del calendario elegido en la app"
    if origen == "manual":
        return "elegido a mano en la app"
    etiqueta = importador.ETIQUETAS_INICIO.get(origen or "")
    if etiqueta is None:
        return origen or "no se sabe"
    aviso = importador.AVISO_ORIGEN.get(origen or "")
    return f"{etiqueta[:1].lower()}{etiqueta[1:]}" + (f" ({aviso})" if aviso else "")


def resumen_importacion(imp: Any, dispositivo: str | None) -> dict[str, Any] | None:
    """Lo que muestra la página de una clase importada de su importacion.json (None si no hay)."""
    if not isinstance(imp, dict):
        return None
    fotos = [f for f in imp.get("fotos") or [] if isinstance(f, dict)]
    fuera = sum(1 for f in fotos if f.get("fuera_de_rango"))
    sin_hora = sum(1 for f in fotos if f.get("offset_ms") is None)
    audio = imp.get("audio") if isinstance(imp.get("audio"), dict) else {}
    if imp.get("via") == "app":
        via = "desde la app"
    elif dispositivo == "panel":
        via = "desde el panel"
    else:
        via = "con classmer importar (o POST /api/importar)"

    def lista(clave: str) -> list[dict[str, Any]]:
        return [x for x in imp.get(clave) or [] if isinstance(x, dict)]

    return {
        "via": via,
        "importado": imp.get("importado"),
        "audio": audio,
        # El tipo, por la extensión del archivo de la sesión (ffprobe dice "mov,mp4,m4a,…" para un m4a).
        "audio_tipo": Path(str(audio.get("archivo") or "")).suffix.lstrip(".").lower(),
        "inicio": imp.get("inicio"),
        "origen_inicio": texto_origen_inicio(imp),
        "fotos": len(fotos),
        "ubicadas": len(fotos) - fuera - sin_hora,
        "sin_hora": sin_hora,
        "fuera": fuera,
        "ilegibles": lista("fotos_ilegibles"),
        "material": lista("material"),
        "material_rechazado": lista("material_rechazado"),
        "avisos": sin_aviso_material([a for a in imp.get("avisos") or [] if isinstance(a, str)]),
    }


def sin_aviso_material(avisos: list[str]) -> list[str]:
    """Los avisos del importador sin el del material que no entró (va aparte, con el motivo de cada uno)."""
    return [a for a in avisos if AVISO_MATERIAL not in a]
