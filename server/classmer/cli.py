"""CLI: `classmer init | token | serve | importar | estado | asignar | reprocesar | reindexar | modelos`."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import secrets
import shutil
import sys
from pathlib import Path

from classmer import __version__
from classmer.config import (
    CONFIG_EJEMPLO,
    ENV_EJEMPLO,
    Config,
    ConfigError,
    cargar_config,
    classmer_home,
)


LOG_SERVER = "classmer.log"
# Los demás comandos escriben en otro archivo: en Windows dos procesos con el mismo
# RotatingFileHandler hacen fallar la rotación (se pierden registros y backups).
LOG_CLI = "classmer-cli.log"


def configurar_logging(cfg: Config | None, nivel: int = logging.INFO, archivo: str = LOG_SERVER) -> None:
    raiz = logging.getLogger()
    if getattr(raiz, "_classmer", False):
        return
    raiz.setLevel(nivel)
    formato = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    consola = logging.StreamHandler(sys.stderr)
    consola.setFormatter(formato)
    raiz.addHandler(consola)
    if cfg is not None:
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        rotativo = logging.handlers.RotatingFileHandler(
            cfg.logs_dir / archivo, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        rotativo.setFormatter(formato)
        raiz.addHandler(rotativo)
    for ruidoso in ("httpx", "httpcore", "google_genai", "multipart"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
    raiz._classmer = True  # type: ignore[attr-defined]


def _salida_utf8() -> None:
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _cargar() -> Config:
    try:
        return cargar_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(2) from e


# --- init / token ----------------------------------------------------------------------------

def _leer_lineas_env(ruta: Path) -> list[str]:
    return ruta.read_text(encoding="utf-8").splitlines() if ruta.is_file() else []


def _token_en_env(lineas: list[str]) -> str | None:
    for linea in lineas:
        clave, _, valor = linea.partition("=")
        if clave.strip() == "CLASSMER_TOKEN" and valor.strip():
            return valor.strip().strip('"').strip("'")
    return None


def cmd_init(_args: argparse.Namespace) -> int:
    home = classmer_home()
    home.mkdir(parents=True, exist_ok=True)
    print(f"Carpeta de datos: {home}")

    config = home / "config.yaml"
    if config.exists():
        print(f"  config.yaml ya existe (no lo toco): {config}")
    else:
        shutil.copyfile(CONFIG_EJEMPLO, config)
        print(f"  config.yaml creado desde el ejemplo: {config}")

    env = home / ".env"
    lineas = _leer_lineas_env(env)
    if not lineas:
        lineas = ENV_EJEMPLO.read_text(encoding="utf-8").splitlines()
    if _token_en_env(lineas):
        print("  CLASSMER_TOKEN ya existe (no lo cambio).")
    else:
        token = secrets.token_urlsafe(32)
        nuevas, puesto = [], False
        for linea in lineas:
            if linea.partition("=")[0].strip() == "CLASSMER_TOKEN" and not puesto:
                nuevas.append(f"CLASSMER_TOKEN={token}")
                puesto = True
            else:
                nuevas.append(linea)
        if not puesto:
            nuevas.append(f"CLASSMER_TOKEN={token}")
        lineas = nuevas
        print("  CLASSMER_TOKEN generado. Mostralo con `uv run classmer token`.")
    tmp = env.with_suffix(".tmp")
    tmp.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    os.replace(tmp, env)
    print(f"  .env: {env}")

    cfg = cargar_config(home)
    from classmer.db import abrir

    abrir(cfg)
    print(f"  Base lista: {cfg.db_path}")
    if not cfg.gemini_api_key:
        print(f"  Falta GEMINI_API_KEY: pegala vos en {env} (no la compartas en ningún chat).")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    cfg = _cargar()
    if not cfg.token:
        print("No hay CLASSMER_TOKEN. Corré `uv run classmer init`.", file=sys.stderr)
        return 1
    if not getattr(args, "qr", False):
        print(cfg.token)
        return 0
    # QR para escanearlo con la cámara del celu y pegarlo en Ajustes (tipear 43 caracteres es un dolor).
    # Queda en la carpeta de datos, al lado del .env: borralo cuando lo cargues.
    import qrcode

    ruta = cfg.home / "token-qr.png"
    qrcode.make(cfg.token, box_size=12, border=4).save(ruta)
    print(f"QR del token en {ruta} (borralo después de cargarlo en la app).")
    if hasattr(os, "startfile"):
        os.startfile(ruta)  # type: ignore[attr-defined]
    return 0


# --- serve -----------------------------------------------------------------------------------

def _puerto_ocupado(host: str, port: int) -> str | None:
    """Prueba bindear el puerto. Devuelve el error si está tomado, None si está libre.

    En Windows, sin SO_REUSEADDR (con esa opción el bind andaría aunque haya otro server). En
    POSIX sí, como uvicorn: ahí sólo saltea las conexiones en TIME_WAIT.
    """
    import socket

    sonda = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            sonda.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sonda.bind((host, port))
    except OSError as e:
        return str(e)
    finally:
        sonda.close()
    return None


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _cargar()
    configurar_logging(cfg)
    if not cfg.token:
        print("No hay CLASSMER_TOKEN. Corré `uv run classmer init`.", file=sys.stderr)
        return 1
    host = args.host or cfg.servidor.host
    port = args.port or cfg.servidor.port
    # Antes de crear la app: uvicorn arranca el worker (lifespan) antes de bindear, y una
    # segunda instancia tomaría jobs para soltarlos enseguida.
    ocupado = _puerto_ocupado(host, port)
    if ocupado:
        print(f"No puedo escuchar en {host}:{port} ({ocupado}): ¿ya hay otro classmer serve, "
              f"p. ej. la tarea programada?", file=sys.stderr)
        return 4
    # Lo primero que dice el log es qué carpeta de datos se va a abrir (una prueba con CLASSMER_HOME mal
    # puesto se ve en la primera línea).
    logging.getLogger("classmer").info("Classmer %s: carpeta de datos %s", __version__, cfg.datos)
    # Antes de abrir la base (abrir() la migra): si otro proceso es el server de esta carpeta de datos,
    # no se toca nada. El 27/10 un server de prueba sin CLASSMER_HOME abrió la base de producción y la
    # migró de esquema mientras producción seguía andando.
    from classmer.worker import tomar_lock_worker

    if not tomar_lock_worker(cfg.datos):
        print(f"Ya hay otro classmer serve (o un importar --procesar) con la carpeta de datos {cfg.datos}: "
              f"no abro la base. Para probar, usá otra carpeta de datos (CLASSMER_HOME).", file=sys.stderr)
        return 5
    import uvicorn

    from classmer.api.app import crear_app

    app = crear_app(cfg, iniciar_worker=not args.sin_worker)
    logging.getLogger("classmer").info("Classmer %s escuchando en %s:%s", __version__, host, port)
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
    return 0


# --- importar / estado / asignar / reprocesar ---------------------------------------------------

def _contexto(cfg: Config):
    from classmer.db import abrir
    from classmer.pipeline import Contexto

    return Contexto(cfg=cfg, base=abrir(cfg))


def cmd_importar(args: argparse.Namespace) -> int:
    cfg = _cargar()
    configurar_logging(cfg, archivo=LOG_CLI)
    from classmer.importador import ErrorImportacion, importar_clase
    from classmer.util import parsear_iso

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
        fotos = sorted(p for p in carpeta.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic", ".webp"))
    material = [Path(m) for m in args.material or []]
    for m in material:
        if not m.is_file():
            print(f"No existe el material: {m}", file=sys.stderr)
            return 2
    if args.numero is not None and args.numero < 0:
        print("--numero no puede ser negativo", file=sys.stderr)
        return 2
    try:
        inicio = parsear_iso(args.inicio, cfg.zona_horaria) if args.inicio else None
    except ValueError:
        print(f"--inicio inválido (ISO 8601, p. ej. 2026-10-15T14:00:00-03:00): {args.inicio}", file=sys.stderr)
        return 2
    materia = None if args.materia in (None, "", "auto") else args.materia

    ctx = _contexto(cfg)
    try:
        r = importar_clase(
            ctx, audio, materia_id=materia, fotos=fotos, inicio=inicio,
            tipo_clase=args.tipo, usar_mtime_fotos=True, numero_clase=args.numero,
            material=[(m, m.name) for m in material],
        )
    except ErrorImportacion as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    sesion_id = r.sesion_id
    print(f"Sesión importada: {sesion_id}")
    if r.material:
        print(f"Material adjunto: {', '.join(m['nombre_original'] for m in r.material)}")
    for m in r.material_rechazado:
        print(f"Material que no entró: {m['nombre']} ({m['motivo']})", file=sys.stderr)
    if not args.procesar:
        print("Quedó en cola: la procesa el server (`uv run classmer serve`).")
        return 0
    from classmer.worker import Worker

    # Sólo esta sesión; si el server ya es el worker, procesar_hasta sólo espera (y lo avisa).
    estado = Worker(ctx, solo_sesion=sesion_id).procesar_hasta(
        sesion_id, timeout_s=args.timeout * 60 if args.timeout else None)
    return _mostrar_sesion(ctx, sesion_id, estado_final=estado)


def _mostrar_sesion(ctx, sesion_id: str, estado_final: str | None = None) -> int:
    from classmer.pipeline import calcular_progreso

    with ctx.base.conexion() as conn:
        s = conn.execute("SELECT * FROM sesiones WHERE id = ?", (sesion_id,)).fetchone()
        if s is None:
            print(f"No existe la sesión {sesion_id}", file=sys.stderr)
            return 1
        print(f"Sesión {s['id']}  materia={s['materia_id']}  estado={s['estado']}  etapa={s['etapa']}  "
              f"progreso={calcular_progreso(conn, sesion_id):.0%}")
        if s["error"]:
            print(f"  error: {s['error']}")
        if s["ruta_publicada"]:
            print(f"  publicado en: {s['ruta_publicada']}")
        for j in conn.execute("SELECT * FROM jobs WHERE sesion_id = ? ORDER BY id", (sesion_id,)):
            extra = f" n={j['args']}" if j["args"] not in (None, "{}") else ""
            err = f"  [{j['error']}]" if j["error"] else ""
            print(f"  job {j['id']:>4} {j['tipo']:<16} {j['estado']:<10} intentos={j['intentos']}{extra}{err}")
    if estado_final is not None:
        return 0 if estado_final == "lista" else 1
    return 0


def cmd_estado(args: argparse.Namespace) -> int:
    cfg = _cargar()
    configurar_logging(cfg, archivo=LOG_CLI)
    ctx = _contexto(cfg)
    if args.sesion:
        return _mostrar_sesion(ctx, args.sesion)
    with ctx.base.conexion() as conn:
        filas = conn.execute(
            "SELECT id, materia_id, estado, etapa, inicio, error FROM sesiones "
            "ORDER BY inicio DESC, creado DESC LIMIT ?",
            (args.limite,),
        ).fetchall()
    if not filas:
        print("No hay sesiones.")
    for f in filas:
        err = f"  [{f['error']}]" if f["error"] else ""
        print(f"{f['id']}  {f['inicio']}  {f['materia_id'] or 'auto':<14} {f['estado']:<17} {f['etapa'] or ''}{err}")
    return 0


def cmd_asignar(args: argparse.Namespace) -> int:
    """Asigna la materia a una sesión (resuelve esperando_usuario) y retoma el pipeline."""
    cfg = _cargar()
    configurar_logging(cfg, archivo=LOG_CLI)
    if cfg.materia(args.materia) is None:
        print(f"No existe la materia {args.materia!r} en config.yaml", file=sys.stderr)
        return 2
    ctx = _contexto(cfg)
    from classmer.sesiones import Conflicto, NoExiste, asignar_materia

    try:
        estado = asignar_materia(cfg, ctx.base, args.sesion, args.materia, permitir_terminada=True)
    except NoExiste:
        print(f"No existe la sesión {args.sesion}", file=sys.stderr)
        return 1
    except Conflicto as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if estado == "esperando_usuario":
        print(f"Materia {args.materia} asignada; sigue la revisión del transcript y el apunte.")
    elif estado in ("lista", "error"):
        print(f"Materia {args.materia} asignada. Para rehacer el apunte: "
              f"uv run classmer reprocesar {args.sesion} --desde apunte")
    else:
        print(f"Materia {args.materia} asignada; el pipeline sigue solo (estado: {estado}).")
    return 0


def cmd_reprocesar(args: argparse.Namespace) -> int:
    cfg = _cargar()
    configurar_logging(cfg, archivo=LOG_CLI)
    ctx = _contexto(cfg)
    from classmer.pipeline import ETAPAS
    from classmer.reproceso import reprocesar

    if args.desde not in ETAPAS:
        print(f"Etapa inválida. Opciones: {', '.join(ETAPAS)}", file=sys.stderr)
        return 2
    try:
        reprocesar(ctx, args.sesion, args.desde)
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Sesión {args.sesion}: reprocesa desde {args.desde}.")
    return 0


def cmd_reindexar(_args: argparse.Namespace) -> int:
    """Rearma el índice de búsqueda (FTS) con las clases que ya tienen apunte."""
    cfg = _cargar()
    configurar_logging(cfg, archivo=LOG_CLI)
    ctx = _contexto(cfg)
    from classmer.busqueda import reindexar

    r = reindexar(cfg, ctx.base)
    print(f"Índice de búsqueda rearmado: {r.sesiones} clases, {r.filas} fragmentos.")
    return 0


# --- modelos ---------------------------------------------------------------------------------------

def _fecha(iso: str | None) -> str:
    return iso[:16].replace("T", " ") if iso else "?"


def _mostrar_catalogo(conn, cfg: Config) -> None:
    from classmer.ia import modelos

    elegidos = modelos.elegidos_config(cfg)
    print("Catálogo de modelos (* = elegido en config.yaml)")
    for tarea in modelos.TAREAS:
        print(f"\n{tarea}:")
        opciones = modelos.opciones_tarea(conn, tarea, list(elegidos[tarea]))
        if not opciones:
            print("  (ninguno)")
        ancho = max((len(o["id"]) for o in opciones), default=0)
        ancho_nombre = max((len(o["nombre"]) for o in opciones), default=0)
        for o in opciones:
            marca = "*" if o["elegido"] else " "
            proveedor = f"[{o['proveedor'] or '?'}]"
            desde = f"  desde {o['descubierto'][:10]}" if o["descubierto"] else ""
            linea = f"  {marca} {o['id']:<{ancho}}  {o['nombre']:<{ancho_nombre}}  {proveedor:<11}{desde}"
            donde = elegidos[tarea].get(o["id"])
            if donde:
                linea += f"  <- {'; '.join(donde)}"
            print(linea)
            if o["aviso"]:
                print(f"      ! {o['aviso']}")
    print("\nÚltimo descubrimiento:")
    ultimos = modelos.ultimo_descubrimiento(conn)
    if not ultimos:
        print("  nunca (sólo los modelos conocidos): uv run classmer modelos --redescubrir")
    for proveedor in [*modelos.PROVEEDORES, *(p for p in ultimos if p not in modelos.PROVEEDORES)]:
        d = ultimos.get(proveedor)
        if d is None:
            if ultimos:
                print(f"  {proveedor:<10} nunca")
            continue
        if d["ok"]:
            estado = f"ok, {d['cantidad']} modelos para Classmer"
        else:
            estado = f"no se actualizó: {d['detalle']}"
            if d["ultimo_ok"]:
                estado += f" (el último que anduvo: {_fecha(d['ultimo_ok'])})"
        print(f"  {proveedor:<10} {_fecha(d['momento'])}  {estado}")


def cmd_modelos(args: argparse.Namespace) -> int:
    """Lista el catálogo de modelos elegibles por tarea; con --redescubrir lo actualiza antes con las
    APIs de Anthropic y Google."""
    cfg = _cargar()
    configurar_logging(cfg, archivo=LOG_CLI)
    from classmer.db import abrir
    from classmer.ia.modelos import redescubrir

    base = abrir(cfg)
    codigo = 0
    if args.redescubrir:
        resumen = redescubrir(cfg, base)
        print("Redescubrir modelos:")
        for linea in resumen.lineas():
            print(f"  {linea}")
        print()
        codigo = 0 if resumen.ok else 1
    with base.conexion() as conn:
        _mostrar_catalogo(conn, cfg)
    return codigo


# --- main ------------------------------------------------------------------------------------

def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="classmer", description="Server de Classmer")
    p.add_argument("--version", action="version", version=f"classmer {__version__}")
    sub = p.add_subparsers(dest="comando", required=True)

    s = sub.add_parser("init", help="crea la carpeta de datos, config.yaml, .env y el token")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("token", help="muestra el token para cargarlo en la app")
    s.add_argument("--qr", action="store_true", help="abre un QR con el token para escanearlo con el celu")
    s.set_defaults(func=cmd_token)

    s = sub.add_parser("serve", help="levanta la API y el worker")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--sin-worker", action="store_true", help="sólo la API, sin procesar jobs")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("importar", help="importa un audio suelto (plan B, importar clase) y lo encola")
    s.add_argument("audio")
    s.add_argument("--materia", required=True, help="id de la materia (o 'auto')")
    s.add_argument("--fotos", help="carpeta con fotos (se ubican por EXIF)")
    s.add_argument("--inicio", help="inicio de la grabación, ISO 8601 (si no: mtime - duración)")
    s.add_argument("--tipo", choices=["teorica", "practica"], help="tipo de clase")
    s.add_argument("--numero", type=int, help="número de clase (si no: el próximo de la materia)")
    s.add_argument("--material", action="append", metavar="ARCHIVO",
                   help="PDF o imagen para adjuntar a la clase (se puede repetir)")
    s.add_argument("--procesar", action="store_true", help="procesa acá mismo hasta terminar")
    s.add_argument("--timeout", type=float, default=None, help="minutos máximos con --procesar")
    s.set_defaults(func=cmd_importar)

    s = sub.add_parser("estado", help="muestra sesiones recientes o el detalle de una")
    s.add_argument("sesion", nargs="?")
    s.add_argument("--limite", type=int, default=20)
    s.set_defaults(func=cmd_estado)

    s = sub.add_parser("asignar", help="asigna la materia a una sesión en esperando_usuario")
    s.add_argument("sesion")
    s.add_argument("--materia", required=True)
    s.set_defaults(func=cmd_asignar)

    s = sub.add_parser("reprocesar", help="re-ejecuta el pipeline desde una etapa")
    s.add_argument("sesion")
    s.add_argument("--desde", default="apunte")
    s.set_defaults(func=cmd_reprocesar)

    s = sub.add_parser("reindexar", help="rearma el índice de búsqueda con las clases ya publicadas")
    s.set_defaults(func=cmd_reindexar)

    s = sub.add_parser("modelos", help="lista el catálogo de modelos elegibles por tarea")
    s.add_argument("--redescubrir", action="store_true",
                   help="antes, lo actualiza con las APIs de modelos de Anthropic y Google")
    s.set_defaults(func=cmd_modelos)
    return p


def main(argv: list[str] | None = None) -> int:
    _salida_utf8()
    args = construir_parser().parse_args(argv)
    return args.func(args)
