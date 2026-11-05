"""Configuración: Classmer-data/config.yaml + Classmer-data/.env (fuera del repo, §5 y §9)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

# Carpetas del repo (server/), resueltas desde el paquete.
SERVER_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = SERVER_DIR / "prompts"
PLANTILLAS_DIR = SERVER_DIR / "templates" / "latex"
CONFIG_EJEMPLO = SERVER_DIR / "config.example.yaml"
ENV_EJEMPLO = SERVER_DIR / ".env.example"

CARPETA_CLASES_DEFAULT = "Clases-Classmer"
PERFIL_DEFAULT = "PROMPT-AGENTE.md"
ID_MATERIA_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Niveles de esfuerzo de Claude (Messages API `output_config.effort` y `--effort` del CLI).
ESFUERZOS = ("low", "medium", "high", "xhigh", "max")
# Cómo se paga lo que Classmer le pide a Claude (§7): sólo API, sólo plan Max o uno con respaldo del otro.
FACTURACIONES = ("api", "max", "api_luego_max", "max_luego_api")
MOTORES_CLASIFICAR = ("claude", "gemini")
# Nombres en el transcript revisado (§15): el profe por su nombre (campo `profesor`) y los alumnos.
NOMBRE_PROFE_DEFAULT = "Profe"
NOMBRE_ALUMNO = "Alumno"
# Tipo de revisión del transcript de una materia (§15): la automática aplica la propuesta del modelo en
# cada duda y sigue al apunte; la asistida espera a que el usuario las revise en el panel; apagada va el crudo.
TIPOS_REVISION = ("automatica", "asistida", "apagada")
REVISION_DEFAULT = "automatica"


class ConfigError(Exception):
    """La configuración no se puede cargar o es inválida."""


def classmer_home() -> Path:
    """Carpeta de config y secretos: CLASSMER_HOME o %USERPROFILE%\\Documents\\Classmer-data."""
    env = os.environ.get("CLASSMER_HOME")
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    return Path.home() / "Documents" / "Classmer-data"


def expandir_ruta(valor: str | os.PathLike[str] | None) -> Path | None:
    if valor is None or str(valor).strip() == "":
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(valor))))


class _Modelo(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ReglaCalendario(_Modelo):
    calendario: str | None = None
    titulo_regex: str | None = None

    @field_validator("titulo_regex")
    @classmethod
    def _regex_valida(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"titulo_regex inválida: {e}") from e
        return v


def _esfuerzo_valido(v: Any) -> Any:
    if v in (None, ""):
        return None
    if v not in ESFUERZOS:
        raise ValueError(f"esfuerzo inválido: {v!r} (opciones: {', '.join(ESFUERZOS)})")
    return v


class ModeloApunte(_Modelo):
    """Modelo propio del apunte de una materia (`apunte:` dentro de la materia). Sin esa clave, la
    materia hereda el modelo y el esfuerzo globales (`apunte.modelo` / `apunte.esfuerzo`)."""

    modelo: str
    esfuerzo: str | None = None   # None = el esfuerzo global

    @field_validator("modelo")
    @classmethod
    def _modelo_no_vacio(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("el modelo del apunte no puede quedar vacío")
        return str(v).strip()

    @field_validator("esfuerzo", mode="before")
    @classmethod
    def _esfuerzo(cls, v: Any) -> Any:
        return _esfuerzo_valido(v)


def tipo_revision(datos: Any) -> Any:
    """El tipo de revisión de una materia tal como está escrita: `revision`, o el switch viejo
    `revisar_transcript` (false = apagada; true o sin escribir = la automática). El switch se lee como
    el bool de antes ("no", "off", "0" también son false): uno inválido es ValueError."""
    valor = datos.get("revision")
    if valor is None:
        viejo = datos.get("revisar_transcript")
        if viejo is None:
            return REVISION_DEFAULT
        try:
            prendida = TypeAdapter(bool).validate_python(viejo)
        except ValidationError as e:
            raise ValueError(f"revisar_transcript inválido: {viejo!r} (true o false; mejor `revision`)") from e
        return REVISION_DEFAULT if prendida else "apagada"
    return str(valor).strip().lower() if isinstance(valor, str) else valor


class Materia(_Modelo):
    id: str
    nombre: str
    carpeta: Path | None = None
    perfil: str | None = None
    carpeta_clases: str = CARPETA_CLASES_DEFAULT
    calendario: ReglaCalendario = Field(default_factory=ReglaCalendario)
    tipos_clase: list[str] = Field(default_factory=lambda: ["teorica", "practica"])
    estilo: str | None = None
    numero_clase_inicial: int = 1
    activa: bool = True
    apunte: ModeloApunte | None = None   # None = hereda el modelo global del apunte
    # Tipo de revisión del transcript (§15, TIPOS_REVISION): automática por defecto. Apagada, la clase
    # sigue como antes (el crudo y el apunte directo), salvo el respaldo del celu donde el principal no
    # tiene audio.
    revision: Literal["automatica", "asistida", "apagada"] = REVISION_DEFAULT
    profesor: str | None = None          # el nombre del profe en el transcript revisado (sin él, "Profe")

    @field_validator("id")
    @classmethod
    def _id_valido(cls, v: str) -> str:
        if not ID_MATERIA_RE.fullmatch(v):
            raise ValueError(f"id de materia inválido: {v!r} (minúsculas, números, - y _)")
        return v

    @field_validator("carpeta", mode="before")
    @classmethod
    def _expandir(cls, v: Any) -> Any:
        return expandir_ruta(v) if isinstance(v, str) else v

    @field_validator("calendario", mode="before")
    @classmethod
    def _calendario_vacio(cls, v: Any) -> Any:
        return {} if v is None else v

    @field_validator("carpeta_clases", mode="before")
    @classmethod
    def _carpeta_clases_default(cls, v: Any) -> Any:
        return CARPETA_CLASES_DEFAULT if v in (None, "") else v

    @field_validator("profesor", mode="before")
    @classmethod
    def _profesor(cls, v: Any) -> Any:
        if v is None:
            return None
        texto = " ".join(str(v).split())
        return texto or None

    @model_validator(mode="before")
    @classmethod
    def _revision_vieja(cls, datos: Any) -> Any:
        if isinstance(datos, dict):
            datos = {**datos, "revision": tipo_revision(datos)}
        return datos

    @property
    def revisar_transcript(self) -> bool:
        """Si la revisión corre (automática o asistida)."""
        return self.revision != "apagada"

    @property
    def nombre_profe(self) -> str:
        """Cómo aparece el profe en el transcript revisado (§15): su nombre o "Profe"."""
        return self.profesor or NOMBRE_PROFE_DEFAULT

    def ruta_perfil(self) -> Path | None:
        """Perfil de la materia: el configurado si existe; si no, PROMPT-AGENTE.md; si no, None."""
        if self.carpeta is None:
            return None
        candidatos = [self.perfil] if self.perfil else []
        candidatos.append(PERFIL_DEFAULT)
        for nombre in candidatos:
            ruta = self.carpeta / nombre
            if ruta.is_file():
                return ruta
        return None

    def ruta_clases(self) -> Path | None:
        if self.carpeta is None:
            return None
        return self.carpeta / self.carpeta_clases

    def para_api(self, proximo_numero: int) -> dict[str, Any]:
        """Forma de GET /api/materias (§5.1)."""
        return {
            "id": self.id,
            "nombre": self.nombre,
            "calendario": {
                "calendario": self.calendario.calendario,
                "titulo_regex": self.calendario.titulo_regex,
            },
            "tipos_clase": list(self.tipos_clase),
            "proximo_numero": proximo_numero,
        }


class Servidor(_Modelo):
    host: str = "0.0.0.0"
    port: int = 8000


class Transcribir(_Modelo):
    motor: str = "gemini"
    modelo: str = "gemini-3.5-transcribe"
    modelo_respaldo: str | None = "gemini-pro-latest"
    idioma: str = "es-AR"
    respaldo: str | None = None
    paralelo: int = 3
    intentos_validacion: int = 2


class Clasificar(_Modelo):
    motor: str = "claude"                 # claude | gemini
    modelo: str = "claude-sonnet-5-5"
    esfuerzo: str | None = "medium"       # sólo con Claude
    umbral_confianza: float = 0.75

    @field_validator("motor")
    @classmethod
    def _motor(cls, v: str) -> str:
        if v not in MOTORES_CLASIFICAR:
            raise ValueError(f"clasificar.motor tiene que ser {' o '.join(MOTORES_CLASIFICAR)}")
        return v

    @field_validator("esfuerzo", mode="before")
    @classmethod
    def _esfuerzo(cls, v: Any) -> Any:
        return _esfuerzo_valido(v)


class RevisarTranscript(_Modelo):
    """La tarea `revisar_transcript` (§15): conciliar los dos transcripts y limpiar. Siempre con Claude
    (API o plan Max, según `claude.facturacion`). El tipo es por materia (`revision` en la materia)."""

    modelo: str = "claude-sonnet-5-5"
    esfuerzo: str | None = "medium"
    escuchar_max: int = 30               # dudas que Gemini vuelve a escuchar en los dos audios, por clase

    @field_validator("esfuerzo", mode="before")
    @classmethod
    def _esfuerzo(cls, v: Any) -> Any:
        return _esfuerzo_valido(v)

    @field_validator("escuchar_max")
    @classmethod
    def _escuchar(cls, v: int) -> int:
        if v < 0:
            raise ValueError("revisar_transcript.escuchar_max no puede ser negativo")
        return v


class Apunte(_Modelo):
    motor: str = "claude-agent"
    modelo: str | None = "claude-opus-5-5"
    esfuerzo: str | None = "high"
    motor_latex: str = "pdflatex"
    max_intentos_compilacion: int = 5
    tope_correccion_min: float = 10
    timeout_min: float = 45

    @field_validator("motor_latex")
    @classmethod
    def _motor_latex(cls, v: str) -> str:
        if v not in ("pdflatex", "lualatex"):
            raise ValueError("motor_latex tiene que ser pdflatex o lualatex")
        return v

    @field_validator("esfuerzo", mode="before")
    @classmethod
    def _esfuerzo(cls, v: Any) -> Any:
        return _esfuerzo_valido(v)


class Claude(_Modelo):
    """Facturación de todo lo que Classmer le pide a Claude (clasificar, apunte y lo que venga)."""

    facturacion: str = "api_luego_max"
    reintentar_api_h: float = 6           # tras quedarse sin créditos, cuándo se vuelve a probar la API

    @field_validator("facturacion")
    @classmethod
    def _facturacion(cls, v: str) -> str:
        if v not in FACTURACIONES:
            raise ValueError(f"claude.facturacion tiene que ser una de: {', '.join(FACTURACIONES)}")
        return v

    @field_validator("reintentar_api_h")
    @classmethod
    def _horas(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("claude.reintentar_api_h tiene que ser mayor que 0")
        return v


class Segmentar(_Modelo):
    bloque_s: float = 600
    ventana_s: float = 120
    silencio_db: float = -35
    silencio_min_s: float = 0.7
    silencio_largo_s: float = 30


class Worker(_Modelo):
    habilitado: bool = True
    poll_s: float = 1.0
    lease_s: int = 900
    paralelo_general: int = 2


class Config(_Modelo):
    """Configuración completa. `home` tiene config.yaml y .env; `datos`, la base y las sesiones."""

    home: Path
    datos: Path
    servidor: Servidor = Field(default_factory=Servidor)
    zona_horaria: str = "America/Argentina/Buenos_Aires"
    transcribir: Transcribir = Field(default_factory=Transcribir)
    clasificar: Clasificar = Field(default_factory=Clasificar)
    revisar_transcript: RevisarTranscript = Field(default_factory=RevisarTranscript)
    apunte: Apunte = Field(default_factory=Apunte)
    claude: Claude = Field(default_factory=Claude)
    segmentar: Segmentar = Field(default_factory=Segmentar)
    worker: Worker = Field(default_factory=Worker)
    estilo_global: str = ""
    materias: list[Materia] = Field(default_factory=list)

    # Secretos: se cargan del .env / entorno y nunca se imprimen.
    token: str | None = Field(default=None, repr=False)
    gemini_api_key: str | None = Field(default=None, repr=False)
    # Sólo del .env de la carpeta de datos (nunca del entorno del proceso) y nunca se exporta a
    # os.environ: el agente la recibe en el entorno de su corrida sólo cuando factura por API.
    anthropic_api_key: str | None = Field(default=None, repr=False)

    @field_validator("materias")
    @classmethod
    def _ids_unicos(cls, v: list[Materia]) -> list[Materia]:
        vistos: set[str] = set()
        for m in v:
            if m.id in vistos:
                raise ValueError(f"materia repetida: {m.id}")
            vistos.add(m.id)
        return v

    # --- rutas de datos ---
    @property
    def db_path(self) -> Path:
        return self.datos / "classmer.db"

    @property
    def sesiones_dir(self) -> Path:
        return self.datos / "sesiones"

    @property
    def jobs_dir(self) -> Path:
        return self.datos / "jobs"

    @property
    def backups_dir(self) -> Path:
        return self.datos / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.datos / "logs"

    def sesion_dir(self, sesion_id: str) -> Path:
        return self.sesiones_dir / sesion_id

    def materia(self, materia_id: str | None) -> Materia | None:
        """La materia con ese id, activa o no (las sesiones viejas siguen apuntando a ella)."""
        if materia_id is None:
            return None
        for m in self.materias:
            if m.id == materia_id:
                return m
        return None

    @property
    def materias_activas(self) -> list[Materia]:
        return [m for m in self.materias if m.activa]

    def modelo_apunte(self, materia: Materia | None) -> tuple[str | None, str | None, str]:
        """(modelo, esfuerzo, origen) del apunte de esa materia: el propio de la materia
        (origen 'materia') o el global (origen 'global'). El esfuerzo propio vacío hereda el global."""
        propio = materia.apunte if materia is not None else None
        if propio is not None:
            return propio.modelo, propio.esfuerzo or self.apunte.esfuerzo, "materia"
        return self.apunte.modelo, self.apunte.esfuerzo, "global"

    def crear_carpetas(self) -> None:
        for d in (self.datos, self.sesiones_dir, self.jobs_dir, self.backups_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


# Claves que también se toman del entorno del proceso (con prioridad sobre el .env). ANTHROPIC_API_KEY
# no: una variable suelta (p. ej. de una sesión de Claude Code) no tiene que facturar por API.
CLAVES_DEL_ENTORNO = ("CLASSMER_TOKEN", "GEMINI_API_KEY")


def leer_env(home: Path) -> dict[str, str]:
    """Valores del .env de la carpeta de datos; para CLASSMER_TOKEN y GEMINI_API_KEY, el entorno del
    proceso tiene prioridad."""
    valores: dict[str, str] = {}
    ruta = home / ".env"
    if ruta.is_file():
        try:
            leidos = dotenv_values(ruta, encoding="utf-8-sig")
        except UnicodeDecodeError as e:  # p. ej. guardado como "Unicode" (UTF-16) en el Bloc de notas
            raise ConfigError(f"{ruta} no está en UTF-8: guardalo como UTF-8 ({e.reason})") from e
        valores.update({k: v.strip() for k, v in leidos.items() if v and v.strip()})
    for clave in CLAVES_DEL_ENTORNO:
        if os.environ.get(clave):
            valores[clave] = os.environ[clave]
    return valores


def cargar_config(home: Path | None = None, *, exigir_archivo: bool = True) -> Config:
    """Carga config.yaml + .env. Sin config.yaml usa los valores por defecto si `exigir_archivo` es False."""
    home = home or classmer_home()
    ruta = home / "config.yaml"
    if ruta.is_file():
        return config_desde_texto(home, ruta.read_text(encoding="utf-8-sig"))
    if exigir_archivo:
        raise ConfigError(f"No existe {ruta}. Corré `uv run classmer init`.")
    return config_desde_texto(home, "")


def config_desde_texto(home: Path, texto: str) -> Config:
    """Valida el texto de un config.yaml (el panel lo usa antes de escribir) y suma el .env."""
    ruta = home / "config.yaml"
    try:
        datos = yaml.safe_load(texto) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yaml inválido ({ruta}): {e}") from e
    if not isinstance(datos, dict):
        raise ConfigError(f"config.yaml tiene que ser un mapa ({ruta})")

    datos["home"] = home
    datos["datos"] = expandir_ruta(datos.get("datos")) or home
    env = leer_env(home)
    datos["token"] = env.get("CLASSMER_TOKEN")
    datos["gemini_api_key"] = env.get("GEMINI_API_KEY")
    datos["anthropic_api_key"] = env.get("ANTHROPIC_API_KEY")
    try:
        return Config.model_validate(datos)
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(f"config.yaml inválido ({ruta}): {e}") from e
