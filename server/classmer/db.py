"""SQLite en modo WAL: esquema (§10), conexiones y sincronización de materias."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from classmer.util import ahora_iso

if TYPE_CHECKING:
    from classmer.config import Config

VERSION_ESQUEMA = 5

ESQUEMA = """
CREATE TABLE IF NOT EXISTS materias (
    id               TEXT PRIMARY KEY,
    nombre           TEXT NOT NULL,
    carpeta          TEXT,
    perfil           TEXT,
    carpeta_clases   TEXT,
    regla_calendario TEXT,              -- JSON {"calendario", "titulo_regex"}
    tipos_clase      TEXT,              -- JSON ["teorica", "practica"]
    estilo           TEXT,
    activa           INTEGER NOT NULL DEFAULT 1,
    actualizado      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS temas (
    id          TEXT PRIMARY KEY,
    materia_id  TEXT NOT NULL REFERENCES materias(id),
    unidad      TEXT,
    nombre      TEXT NOT NULL,
    descripcion TEXT,
    sinonimos   TEXT,                   -- JSON
    estado      TEXT NOT NULL DEFAULT 'pendiente'   -- aprobado | pendiente
);

CREATE TABLE IF NOT EXISTS sesiones (
    id              TEXT PRIMARY KEY,   -- UUID generado en el celu
    materia_id      TEXT REFERENCES materias(id),
    tipo_clase      TEXT,
    inicio          TEXT NOT NULL,      -- ISO 8601 con zona
    duracion_ms     INTEGER,
    origen_materia  TEXT,               -- calendario | manual | auto
    confianza       REAL,
    estado          TEXT NOT NULL DEFAULT 'grabando',
    etapa           TEXT,
    espera          TEXT,               -- en esperando_usuario: materia | revision (esquema 5)
    error           TEXT,
    dispositivo     TEXT,
    importada       INTEGER NOT NULL DEFAULT 0,   -- 1: audio que no grabó la app (importar clase; esquema 4)
    sha256          TEXT,
    bytes_totales   INTEGER,
    eventos_totales INTEGER,
    numero_clase    INTEGER,
    titulo          TEXT,
    ruta_publicada  TEXT,
    creado          TEXT NOT NULL,
    actualizado     TEXT NOT NULL,
    finalizado      TEXT
);
CREATE INDEX IF NOT EXISTS sesiones_por_inicio ON sesiones(inicio DESC, creado DESC);
CREATE INDEX IF NOT EXISTS sesiones_por_materia ON sesiones(materia_id, inicio);

CREATE TABLE IF NOT EXISTS assets (
    id              TEXT PRIMARY KEY,   -- UUID del evento; el audio usa 'audio:<sesion>'
    sesion_id       TEXT NOT NULL REFERENCES sesiones(id),
    tipo            TEXT NOT NULL,      -- audio | foto | nota | marca | pausa | reanudar | adjunto
    offset_ms       INTEGER,
    ruta            TEXT,               -- relativa a la carpeta de la sesión
    sha256          TEXT,
    bytes_recibidos INTEGER,
    datos           TEXT,               -- JSON (texto, marca, creado, ...)
    creado          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_por_sesion ON assets(sesion_id, tipo);

CREATE TABLE IF NOT EXISTS bloques (
    sesion_id TEXT NOT NULL REFERENCES sesiones(id),
    n         INTEGER NOT NULL,
    inicio_ms INTEGER NOT NULL,
    fin_ms    INTEGER NOT NULL,
    ruta      TEXT NOT NULL,
    estado    TEXT NOT NULL DEFAULT 'pendiente',   -- pendiente | transcripto | dudoso | error
    motor     TEXT,
    pista     TEXT,                     -- NULL: el audio principal; 'celu': la segunda pista (n desde 101; esquema 5)
    PRIMARY KEY (sesion_id, n)
);

CREATE TABLE IF NOT EXISTS segmentos (
    id        INTEGER PRIMARY KEY,
    sesion_id TEXT NOT NULL REFERENCES sesiones(id),
    bloque    INTEGER NOT NULL,
    inicio_ms INTEGER NOT NULL,
    fin_ms    INTEGER NOT NULL,
    hablante  TEXT,
    texto     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS segmentos_por_sesion ON segmentos(sesion_id, inicio_ms);

CREATE TABLE IF NOT EXISTS apuntes (
    id             INTEGER PRIMARY KEY,
    sesion_id      TEXT NOT NULL REFERENCES sesiones(id),
    version        INTEGER NOT NULL,
    actual         INTEGER NOT NULL DEFAULT 0,
    motor          TEXT,
    modelo         TEXT,
    prompt_version TEXT,
    ruta_tex       TEXT,
    ruta_pdf       TEXT,
    compilo        INTEGER NOT NULL DEFAULT 0,
    titulo         TEXT,
    resumen        TEXT,
    costo          REAL,
    esfuerzo       TEXT,               -- low | medium | high | xhigh | max (null: el default del modelo)
    facturacion    TEXT,               -- api | max: con qué se pagó
    adjuntos       TEXT,               -- JSON: ids de los adjuntos que usó esta versión (NULL: ninguno)
    creado         TEXT NOT NULL,
    UNIQUE (sesion_id, version)
);

CREATE TABLE IF NOT EXISTS secciones (
    id          INTEGER PRIMARY KEY,
    apunte_id   INTEGER NOT NULL REFERENCES apuntes(id),
    orden       INTEGER NOT NULL,
    titulo      TEXT NOT NULL,
    inicio_ms   INTEGER,
    fin_ms      INTEGER,
    texto_plano TEXT
);

CREATE TABLE IF NOT EXISTS seccion_temas (
    seccion_id INTEGER NOT NULL REFERENCES secciones(id),
    tema_id    TEXT NOT NULL,
    PRIMARY KEY (seccion_id, tema_id)
);

CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY,
    materia_id   TEXT,
    sesion_id    TEXT REFERENCES sesiones(id),
    apunte_id    INTEGER REFERENCES apuntes(id),
    tipo         TEXT NOT NULL,         -- parcial | tarea | fecha | duda | aviso
    texto        TEXT NOT NULL,
    fecha_limite TEXT,
    offset_ms    INTEGER,
    estado       TEXT NOT NULL DEFAULT 'abierto',   -- abierto | hecho | descartado
    creado       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompts (
    id         INTEGER PRIMARY KEY,
    tarea      TEXT NOT NULL,
    alcance    TEXT NOT NULL,           -- global | materia
    materia_id TEXT,
    version    TEXT NOT NULL,
    contenido  TEXT NOT NULL,
    activo     INTEGER NOT NULL DEFAULT 1,
    creado     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS prompts_version
    ON prompts(tarea, alcance, IFNULL(materia_id, ''), version);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    tipo         TEXT NOT NULL,
    sesion_id    TEXT,
    args         TEXT,                  -- JSON
    clave        TEXT,                  -- evita duplicados mientras el job está activo
    estado       TEXT NOT NULL DEFAULT 'pendiente',  -- pendiente | corriendo | hecho | error | cancelado
    prioridad    INTEGER NOT NULL DEFAULT 100,
    intentos     INTEGER NOT NULL DEFAULT 0,
    max_intentos INTEGER NOT NULL DEFAULT 3,
    run_after    REAL NOT NULL DEFAULT 0,   -- epoch en segundos
    lease_hasta  REAL,
    motor        TEXT,
    error        TEXT,
    log          TEXT,
    costo        REAL,
    facturacion  TEXT,                  -- api | max (sólo los jobs que usaron Claude)
    creado       REAL NOT NULL,
    actualizado  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_listos ON jobs(estado, run_after, prioridad, creado);
CREATE INDEX IF NOT EXISTS jobs_por_sesion ON jobs(sesion_id, tipo);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_clave_activa
    ON jobs(clave) WHERE clave IS NOT NULL AND estado IN ('pendiente', 'corriendo');

-- Carpetas que publicó cada sesión en la carpeta de la materia (§9). Sólo la vigente queda ahí: al
-- republicar, la anterior se mueve a Classmer-data/apuntes-anteriores/. Si Windows no deja moverla
-- (un PDF abierto), queda 'sin_archivar' y se archiva en la próxima publicación de la sesión.
CREATE TABLE IF NOT EXISTS publicaciones (
    id          INTEGER PRIMARY KEY,
    sesion_id   TEXT NOT NULL REFERENCES sesiones(id),
    ruta        TEXT NOT NULL,          -- carpeta en la materia
    version     INTEGER,                -- vN del apunte publicado
    compilo     INTEGER,
    estado      TEXT NOT NULL,          -- vigente | sin_archivar | archivada | no_esta
    archivo     TEXT,                   -- dónde quedó archivada (Classmer-data/apuntes-anteriores/…)
    aviso       TEXT,                   -- p. ej. por qué no se pudo archivar
    creado      TEXT NOT NULL,
    actualizado TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS publicaciones_por_sesion ON publicaciones(sesion_id, estado);

-- Facturación de Claude (§7): un medio sin fila está ok. Lo escriben las tareas que usan Claude.
CREATE TABLE IF NOT EXISTS facturacion (
    medio        TEXT PRIMARY KEY,      -- api | max
    estado       TEXT NOT NULL,         -- sin_creditos | clave_invalida | sin_cupo | sin_login
    motivo       TEXT,
    desde        REAL NOT NULL,         -- epoch del error
    hasta        REAL,                  -- epoch desde el que se vuelve a probar (max); api: desde + reintentar_api_h
    huella_clave TEXT                   -- api: huella corta de la clave con la que falló (si cambia, se vuelve a probar)
);

-- Catálogo de modelos elegibles en el panel (§7). Arranca con los conocidos y se actualiza con
-- "Redescubrir modelos" (APIs de Anthropic y Google).
CREATE TABLE IF NOT EXISTS modelos (
    proveedor    TEXT NOT NULL,         -- anthropic | google
    id           TEXT NOT NULL,         -- id de la API (sin "models/")
    nombre       TEXT,                  -- nombre visible
    tareas       TEXT NOT NULL,         -- JSON: ["apunte", "clasificar"], ["transcribir"]…
    detalles     TEXT,                  -- JSON: lo que informó la API (métodos, capacidades, esfuerzos)
    descubierto  TEXT NOT NULL,         -- ISO: cuándo entró al catálogo
    visto        TEXT,                  -- ISO: último descubrimiento en que apareció
    presente     INTEGER NOT NULL DEFAULT 1,   -- 0: no apareció en el último descubrimiento de su proveedor
    origen       TEXT NOT NULL DEFAULT 'semilla',  -- semilla | api
    PRIMARY KEY (proveedor, id)
);

CREATE TABLE IF NOT EXISTS descubrimientos (
    proveedor TEXT PRIMARY KEY,         -- anthropic | google
    momento   TEXT NOT NULL,            -- ISO del último intento
    ok        INTEGER NOT NULL,         -- 1 si la API respondió
    detalle   TEXT,                     -- error o aviso (p. ej. "falta ANTHROPIC_API_KEY")
    cantidad  INTEGER                   -- modelos que devolvió para Classmer
);

CREATE VIRTUAL TABLE IF NOT EXISTS busqueda USING fts5(
    texto, materia, tipo, ref, inicio_ms UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def conectar(ruta: Path) -> sqlite3.Connection:
    """Conexión en autocommit (transacciones explícitas), WAL, claves foráneas y espera por locks."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ruta, isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# Columnas que se sumaron después de la v1 del esquema: `CREATE TABLE IF NOT EXISTS` no las agrega a
# una base que ya existe, así que se agregan acá (idempotente, sin tocar los datos).
COLUMNAS_NUEVAS = (
    ("apuntes", "esfuerzo", "TEXT"),
    ("apuntes", "facturacion", "TEXT"),
    ("jobs", "facturacion", "TEXT"),
    ("apuntes", "adjuntos", "TEXT"),
    ("sesiones", "importada", "INTEGER NOT NULL DEFAULT 0"),
    ("sesiones", "espera", "TEXT"),
    ("bloques", "pista", "TEXT"),
)


def migrar(conn: sqlite3.Connection) -> None:
    """Columnas nuevas y datos de las versiones anteriores (idempotente: mira `user_version`)."""
    previa = conn.execute("PRAGMA user_version").fetchone()[0]
    for tabla, columna, tipo in COLUMNAS_NUEVAS:
        existentes = {c[1] for c in conn.execute(f'PRAGMA table_info("{tabla}")')}
        if columna not in existentes:
            conn.execute(f'ALTER TABLE "{tabla}" ADD COLUMN {columna} {tipo}')
    if previa < 4:
        # Esquema 4: las que armó el importador (plan B) son importadas.
        conn.execute("UPDATE sesiones SET importada = 1 WHERE dispositivo = 'importador' AND importada = 0")
    if previa < 5:
        # Esquema 5: la espera de siempre (§15) es la de la materia.
        conn.execute("UPDATE sesiones SET espera = 'materia' WHERE estado = 'esperando_usuario' AND espera IS NULL")


def inicializar(ruta: Path) -> None:
    conn = conectar(ruta)
    try:
        conn.executescript(ESQUEMA)
        migrar(conn)
        conn.execute(f"PRAGMA user_version = {VERSION_ESQUEMA}")
    finally:
        conn.close()


@contextmanager
def transaccion(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE … COMMIT (o ROLLBACK si hay excepción)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class Base:
    """Acceso a la base: una conexión nueva por uso (sirve igual desde la API, el worker y la CLI)."""

    def __init__(self, ruta: Path):
        self.ruta = ruta

    def inicializar(self) -> None:
        inicializar(self.ruta)

    @contextmanager
    def conexion(self) -> Iterator[sqlite3.Connection]:
        conn = conectar(self.ruta)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaccion(self) -> Iterator[sqlite3.Connection]:
        with self.conexion() as conn, transaccion(conn):
            yield conn


def sincronizar_materias(conn: sqlite3.Connection, cfg: Config) -> None:
    """Copia las materias de config.yaml a la tabla `materias` (las que ya no están, o tienen
    `activa: false`, quedan inactivas). Se llama al abrir y en cada recarga de la config."""
    momento = ahora_iso(cfg.zona_horaria)
    with transaccion(conn):
        conn.execute("UPDATE materias SET activa = 0")
        for m in cfg.materias:
            conn.execute(
                """
                INSERT INTO materias (id, nombre, carpeta, perfil, carpeta_clases, regla_calendario,
                                      tipos_clase, estilo, activa, actualizado)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    nombre = excluded.nombre, carpeta = excluded.carpeta, perfil = excluded.perfil,
                    carpeta_clases = excluded.carpeta_clases,
                    regla_calendario = excluded.regla_calendario,
                    tipos_clase = excluded.tipos_clase, estilo = excluded.estilo,
                    activa = excluded.activa, actualizado = excluded.actualizado
                """,
                (
                    m.id,
                    m.nombre,
                    str(m.carpeta) if m.carpeta else None,
                    m.perfil,
                    m.carpeta_clases,
                    json.dumps(m.calendario.model_dump(), ensure_ascii=False),
                    json.dumps(m.tipos_clase, ensure_ascii=False),
                    m.estilo,
                    1 if m.activa else 0,
                    momento,
                ),
            )


def abrir(cfg: Config) -> Base:
    """Crea las carpetas de datos, inicializa el esquema, sincroniza materias y siembra el catálogo
    de modelos (los conocidos que falten)."""
    from classmer.ia.modelos import sembrar

    cfg.crear_carpetas()
    base = Base(cfg.db_path)
    base.inicializar()
    with base.conexion() as conn:
        sincronizar_materias(conn, cfg)
        sembrar(conn, cfg.zona_horaria)
    return base
