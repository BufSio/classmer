"""config.yaml editable sin reiniciar: ediciones que conservan comentarios y recarga en caliente."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from classmer import config_vivo
from classmer.config import CONFIG_EJEMPLO, ConfigError, Materia, ModeloApunte, cargar_config
from classmer.config_vivo import ConfigViva, MateriaNoExiste, materia_cruda
from classmer.db import abrir
from classmer.ia.facturacion import Facturacion

# Una config como la que tiene el usuario hoy (copiada del config.example.yaml viejo): clasificar con
# Gemini, el apunte con el alias "opus" y sin esfuerzo, sin sección `claude`. Rutas inventadas.
CONFIG_VIEJA = """\
# Configuración de Classmer.
# Los secretos (CLASSMER_TOKEN, GEMINI_API_KEY) NO van acá: van en Classmer-data\\.env.

servidor:
  host: 0.0.0.0
  port: 8000

zona_horaria: America/Argentina/Buenos_Aires

# --- Motores de IA por tarea (§7) ------------------------------------------------

transcribir:
  motor: gemini              # gemini | whisper (v0.2)
  modelo: gemini-3.5-transcribe       # ASR de Gemini: tiempos por palabra y hablantes
  idioma: es-AR

clasificar:
  motor: gemini
  modelo: gemini-flash-latest
  umbral_confianza: 0.75     # por debajo, la sesión queda en esperando_usuario

apunte:
  motor: claude-agent        # Claude Agent SDK con el login de Claude Code (sin API key)
  modelo: opus               # alias o id de modelo de Claude
  motor_latex: pdflatex      # pdflatex | lualatex
  timeout_min: 45            # tope total del agente (escribir + compilar)

# --- Cortes del audio
segmentar:
  bloque_s: 600              # largo objetivo de cada bloque (~10 min)

# --- Estilo global del apunte (capa 2 del prompt, §7) ------------------------------
estilo_global: |
  Incluí gráficos (TikZ/pgfplots) cuando ayuden a entender.

# --- Materias (§9) ----------------------------------------------------------------
materias:
  - id: analisis
    nombre: "Análisis Matemático II"
    carpeta: 'C:\\Materias-de-prueba\\Análisis Matemático II'
    calendario:
      calendario: null
      titulo_regex: null     # TODO: completar
    tipos_clase: [teorica, practica]

  - id: historia
    nombre: "Historia Moderna"
    carpeta: 'C:\\Materias-de-prueba\\Historia'
    estilo: |
      Con fechas.
    # sin calendario todavía

  - id: riesgo
    nombre: "Riesgo Financiero"
    carpeta: 'C:\\Materias-de-prueba\\Riesgo'
    tipos_clase: [teorica, practica]
"""


@pytest.fixture
def home_ejemplo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Una copia de config.example.yaml con fin de línea CRLF, como la que edita el usuario en Windows."""
    home = tmp_path / "Classmer-data"
    home.mkdir()
    texto = CONFIG_EJEMPLO.read_text(encoding="utf-8").replace("\r\n", "\n")
    (home / "config.yaml").write_bytes(texto.replace("\n", "\r\n").encode("utf-8"))
    (home / ".env").write_text("CLASSMER_TOKEN=x\n", encoding="utf-8")
    monkeypatch.setenv("CLASSMER_HOME", str(home))
    return home


def lineas(home: Path) -> list[str]:
    return (home / "config.yaml").read_bytes().decode("utf-8").split("\r\n")


def test_editar_materia_toca_sólo_lo_que_cambia(home_ejemplo: Path):
    antes = lineas(home_ejemplo)
    cfg = cargar_config(home_ejemplo)
    viva = ConfigViva(cfg)
    viva.editar_materia("analisis", {
        "calendario": {"calendario": None, "titulo_regex": r"^An[aá]lisis Matem[aá]tico\b"},
        "numero_clase_inicial": 3, "tipos_clase": ["teorica"], "nombre": "Análisis Matemático II",
    })
    crudo = (home_ejemplo / "config.yaml").read_bytes()
    assert b"\r\n" in crudo and b"\n" not in crudo.replace(b"\r\n", b"")  # sigue en CRLF
    despues = lineas(home_ejemplo)
    cambiadas = [(a, d) for a, d in zip(antes, despues) if a != d]
    assert len(despues) == len(antes) + 1  # sólo se agregó numero_clase_inicial
    assert any("titulo_regex: '^An[aá]lisis Matem[aá]tico\\b'" in d and "# TODO" in d for _, d in cambiadas)
    assert "    tipos_clase: [teorica]" in despues
    assert "    numero_clase_inicial: 3" in despues
    # Comentarios, comillas y rutas con %USERPROFILE% intactos.
    assert all(c in despues for c in antes if c.lstrip().startswith("#"))
    assert "    carpeta: '%USERPROFILE%\\Documents\\Claude\\Projects\\Análisis Matemático II'" in despues
    # Aplicado en el mismo objeto, sin reiniciar.
    m = cfg.materia("analisis")
    assert m.numero_clase_inicial == 3 and m.tipos_clase == ["teorica"]
    assert m.calendario.titulo_regex == r"^An[aá]lisis Matem[aá]tico\b"


def test_desactivar_estilo_y_rutas(home_ejemplo: Path):
    cfg = cargar_config(home_ejemplo)
    viva = ConfigViva(cfg)
    viva.editar_materia("macro", {"activa": False, "estilo": "Gráficos IS-LM.\nSiempre con ejemplos.",
                                  "carpeta": r"%USERPROFILE%\Documents\Otra carpeta"})
    assert [m.id for m in cfg.materias_activas] == ["analisis", "teoria-juegos", "riesgo"]
    crudo = materia_cruda(home_ejemplo, "macro")
    assert crudo["activa"] is False and crudo["carpeta"] == r"%USERPROFILE%\Documents\Otra carpeta"
    texto = (home_ejemplo / "config.yaml").read_text(encoding="utf-8")
    assert "    estilo: |\n      Gráficos IS-LM.\n      Siempre con ejemplos.\n" in texto.replace("\r\n", "\n")
    assert cfg.materia("macro").estilo == "Gráficos IS-LM.\nSiempre con ejemplos.\n"
    viva.editar_materia("macro", {"activa": True})
    assert "macro" in [m.id for m in cfg.materias_activas]


def test_agregar_materia(home_ejemplo: Path):
    cfg = cargar_config(home_ejemplo)
    viva = ConfigViva(cfg)
    viva.agregar_materia({"id": "historia", "nombre": "Historia Contemporánea",
                          "carpeta": r"%USERPROFILE%\Documents\Claude\Projects\Historia Contemporanea",
                          "perfil": "PROMPT-AGENTE-HISTORIA.md",
                          "calendario": {"calendario": "", "titulo_regex": r"^Historia\b"},
                          "tipos_clase": ["teorica", "practica"], "numero_clase_inicial": 4})
    m = cfg.materia("historia")
    assert m is not None and m.numero_clase_inicial == 4 and m.carpeta_clases == "Clases-Classmer"
    texto = (home_ejemplo / "config.yaml").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert texto.endswith(
        "  - id: historia\n"
        "    nombre: Historia Contemporánea\n"
        "    carpeta: '%USERPROFILE%\\Documents\\Claude\\Projects\\Historia Contemporanea'\n"
        "    perfil: PROMPT-AGENTE-HISTORIA.md\n"
        "    numero_clase_inicial: 4\n"
        "    calendario:\n"
        "      calendario: null\n"
        "      titulo_regex: '^Historia\\b'\n"
        "    tipos_clase: [teorica, practica]\n")
    with pytest.raises(ConfigError, match="ya existe"):
        viva.agregar_materia({"id": "historia", "nombre": "Otra"})
    with pytest.raises(ConfigError, match="id de materia inválido"):
        viva.agregar_materia({"id": "Con Espacios", "nombre": "X"})


def test_edicion_invalida_no_escribe(home_ejemplo: Path):
    cfg = cargar_config(home_ejemplo)
    viva = ConfigViva(cfg)
    antes = (home_ejemplo / "config.yaml").read_bytes()
    with pytest.raises(ConfigError, match="regex"):
        viva.editar_materia("riesgo", {"calendario": {"titulo_regex": "([sin cerrar"}})
    with pytest.raises(ConfigError, match="nombre"):
        viva.editar_materia("riesgo", {"nombre": "  "})
    with pytest.raises(MateriaNoExiste):
        viva.editar_materia("no-existe", {"nombre": "X"})
    assert (home_ejemplo / "config.yaml").read_bytes() == antes


def test_estilo_global(home_ejemplo: Path):
    cfg = cargar_config(home_ejemplo)
    ConfigViva(cfg).editar_estilo_global("Más gráficos.\r\nY ejemplos numéricos.")
    assert cfg.estilo_global == "Más gráficos.\nY ejemplos numéricos.\n"
    texto = (home_ejemplo / "config.yaml").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "# --- Estilo global del apunte" in texto
    assert "estilo_global: |\n  Más gráficos.\n  Y ejemplos numéricos.\n" in texto


def test_recarga_de_ediciones_a_mano(home_ejemplo: Path):
    cfg = cargar_config(home_ejemplo)
    base = abrir(cfg)
    viva = ConfigViva(cfg, base)
    assert not viva.recargar()  # nada cambió
    ruta = home_ejemplo / "config.yaml"
    texto = ruta.read_text(encoding="utf-8")
    ruta.write_text(texto.replace('nombre: "Riesgo Financiero"', 'nombre: "RF"'), encoding="utf-8")
    assert viva.recargar()
    assert cfg.materia("riesgo").nombre == "RF"
    with base.conexion() as conn:
        assert conn.execute("SELECT nombre FROM materias WHERE id = 'riesgo'").fetchone()[0] == "RF"

    # Un error de sintaxis a mano: se sigue con la config anterior y queda el error a la vista.
    ruta.write_text(texto + "\nmaterias: [rota\n", encoding="utf-8")
    assert not viva.recargar()
    assert viva.error and "config.yaml inválido" in viva.error
    assert cfg.materia("riesgo").nombre == "RF"
    ruta.write_text(texto, encoding="utf-8")
    assert viva.recargar() and viva.error is None
    assert cfg.materia("riesgo").nombre == "Riesgo Financiero"


def test_cambios_que_piden_reiniciar(home_ejemplo: Path):
    cfg = cargar_config(home_ejemplo)
    viva = ConfigViva(cfg)
    ruta = home_ejemplo / "config.yaml"
    ruta.write_text(ruta.read_text(encoding="utf-8").replace("port: 8000", "port: 8123"), encoding="utf-8")
    assert viva.recargar()
    assert viva.pendientes_reinicio == ["servidor"] and cfg.servidor.port == 8000


def test_vigilante_en_segundo_plano(home_ejemplo: Path):
    import time

    cfg = cargar_config(home_ejemplo)
    viva = ConfigViva(cfg, intervalo_s=0.05)
    viva.iniciar()
    try:
        ruta = home_ejemplo / "config.yaml"
        ruta.write_text(ruta.read_text(encoding="utf-8").replace("umbral_confianza: 0.75", "umbral_confianza: 0.5"),
                        encoding="utf-8")
        limite = time.monotonic() + 5
        while cfg.clasificar.umbral_confianza != 0.5 and time.monotonic() < limite:
            time.sleep(0.05)
        assert cfg.clasificar.umbral_confianza == 0.5
    finally:
        viva.detener()


# --- modelos, esfuerzo y facturación (página Modelos) -------------------------------------------

@pytest.fixture
def home_vieja(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """CONFIG_VIEJA en CRLF, con una clave de API falsa en el .env."""
    home = tmp_path / "Classmer-data"
    home.mkdir()
    (home / "config.yaml").write_bytes(CONFIG_VIEJA.replace("\n", "\r\n").encode("utf-8"))
    (home / ".env").write_text("CLASSMER_TOKEN=x\nANTHROPIC_API_KEY=sk-ant-falsa-123\n", encoding="utf-8")
    monkeypatch.setenv("CLASSMER_HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return home


def texto(home: Path) -> str:
    return (home / "config.yaml").read_bytes().decode("utf-8").replace("\r\n", "\n")


def comentarios(lineas_: list[str]) -> list[str]:
    return [linea for linea in lineas_ if linea.lstrip().startswith("#")]


def test_editar_modelos_toca_sólo_lo_que_cambia(home_vieja: Path):
    antes = lineas(home_vieja)
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    devuelta = viva.editar_modelos({
        "transcribir": {"modelo": "gemini-3.5-transcribe"},   # igual: no se toca
        "clasificar": {"motor": "claude", "modelo": "claude-sonnet-5-5", "esfuerzo": "medium",
                       "umbral_confianza": "0,75"},
        "apunte": {"modelo": "claude-opus-5-5", "esfuerzo": "max"},
    })
    assert devuelta is cfg
    crudo = (home_vieja / "config.yaml").read_bytes()
    assert b"\n" not in crudo.replace(b"\r\n", b"")  # sigue en CRLF
    despues = lineas(home_vieja)
    quitadas = [linea for linea in antes if linea not in despues]
    agregadas = [linea for linea in despues if linea not in antes]
    assert quitadas == ["  motor: gemini", "  modelo: gemini-flash-latest",
                        "  modelo: opus               # alias o id de modelo de Claude"]
    assert agregadas[:2] == ["  motor: claude", "  modelo: claude-sonnet-5-5"]
    # El comentario de la línea se queda; el esfuerzo nuevo va justo después del modelo.
    assert agregadas[2].startswith("  modelo: claude-opus-5-5 ")
    assert agregadas[2].endswith("# alias o id de modelo de Claude")
    assert agregadas[3:] == ["  esfuerzo: max"]
    assert despues.index("  esfuerzo: max") == despues.index(agregadas[2]) + 1
    # clasificar no tenía esfuerzo y "medium" es el valor por defecto: no se agrega.
    assert len(despues) == len(antes) + 1
    assert comentarios(despues) == comentarios(antes)
    # Aplicado en el mismo objeto, sin reiniciar.
    assert (cfg.clasificar.motor, cfg.clasificar.modelo, cfg.clasificar.esfuerzo) == \
        ("claude", "claude-sonnet-5-5", "medium")
    assert (cfg.apunte.modelo, cfg.apunte.esfuerzo) == ("claude-opus-5-5", "max")
    assert cfg.modelo_apunte(cfg.materia("riesgo")) == ("claude-opus-5-5", "max", "global")


def test_sin_cambios_no_escribe(home_vieja: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    antes = (home_vieja / "config.yaml").read_bytes()

    def no_escribir(*_a, **_k):
        raise AssertionError("no tenía que escribir")

    monkeypatch.setattr(config_vivo, "escribir_atomico", no_escribir)
    assert viva.editar_modelos({}) is cfg
    assert viva.editar_modelos({"clasificar": {"motor": "gemini", "umbral_confianza": 0.75}}) is cfg
    # La sección `claude` no existe y los valores son los de siempre: no se crea.
    viva.editar_modelos({"claude": {"facturacion": "api_luego_max", "reintentar_api_h": "6"},
                         "apunte": {"esfuerzo": "high"}})
    assert (home_vieja / "config.yaml").read_bytes() == antes


def test_sección_claude_nueva(home_vieja: Path):
    cfg = cargar_config(home_vieja)
    base = abrir(cfg)
    viva = ConfigViva(cfg, base)
    fact = Facturacion(cfg, base)
    assert fact.medios_del_modo() == ["api", "max"]  # por defecto api_luego_max

    viva.editar_modelos({"claude": {"facturacion": "max"}})
    # En bloque, entre apunte y segmentar; el comentario de segmentar sigue arriba de segmentar.
    assert (
        "  timeout_min: 45            # tope total del agente (escribir + compilar)\n"
        "\n"
        "claude:\n"
        "  facturacion: max\n"
        "\n"
        "# --- Cortes del audio\n"
        "segmentar:\n") in texto(home_vieja)
    assert cfg.claude.facturacion == "max"
    assert fact.medios_del_modo() == ["max"]  # el próximo job ya lo ve

    viva.editar_modelos({"claude": {"reintentar_api_h": "12"}})
    assert "claude:\n  facturacion: max\n  reintentar_api_h: 12\n\n# --- Cortes del audio\n" in texto(home_vieja)
    assert cfg.claude.reintentar_api_h == 12

    viva.editar_modelos({"claude": {"facturacion": "max_luego_api", "reintentar_api_h": 1.5}})
    assert "claude:\n  facturacion: max_luego_api\n  reintentar_api_h: 1.5\n\n" in texto(home_vieja)
    assert fact.medios_del_modo() == ["max", "api"]
    assert comentarios(lineas(home_vieja)) == comentarios(CONFIG_VIEJA.split("\n"))


def test_secciones_nuevas_en_una_config_mínima(home: Path, cfg):
    """La config de los tests (yaml.safe_dump, sin secciones de modelos): se crean en bloque."""
    viva = ConfigViva(cfg)
    viva.editar_modelos({"apunte": {"modelo": "claude-sonnet-5-5", "esfuerzo": "low"},
                         "clasificar": {"motor": "gemini", "modelo": "gemini-flash-latest"},
                         "transcribir": {"modelo": "gemini-3.5-transcribe-v2"}})
    escrito = texto(home)
    assert "apunte:\n  modelo: claude-sonnet-5-5\n  esfuerzo: low\n" in escrito
    assert "{" not in escrito
    datos = yaml.safe_load(escrito)
    assert datos["clasificar"] == {"motor": "gemini", "modelo": "gemini-flash-latest"}
    assert datos["transcribir"] == {"modelo": "gemini-3.5-transcribe-v2"}
    assert [m["id"] for m in datos["materias"]] == ["prueba", "otra"]
    assert (cfg.apunte.modelo, cfg.apunte.esfuerzo) == ("claude-sonnet-5-5", "low")
    assert cfg.clasificar.motor == "gemini" and cfg.transcribir.modelo == "gemini-3.5-transcribe-v2"


def test_esfuerzo_vacío_es_null(home_vieja: Path):
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    assert (cfg.clasificar.esfuerzo, cfg.apunte.esfuerzo) == ("medium", "high")  # por defecto
    viva.editar_modelos({"clasificar": {"esfuerzo": ""}, "apunte": {"esfuerzo": None}})
    escrito = texto(home_vieja)
    # Explícito: sin la clave volvería el valor por defecto.
    assert "  modelo: gemini-flash-latest\n  esfuerzo: null\n  umbral_confianza: 0.75" in escrito
    assert "# alias o id de modelo de Claude\n  esfuerzo: null\n  motor_latex: pdflatex" in escrito
    assert cfg.clasificar.esfuerzo is None and cfg.apunte.esfuerzo is None
    viva.editar_modelos({"apunte": {"esfuerzo": "xhigh"}})
    assert "  esfuerzo: xhigh\n  motor_latex: pdflatex" in texto(home_vieja)
    assert cfg.apunte.esfuerzo == "xhigh"


def test_umbral_del_formulario(home_vieja: Path):
    cfg = cargar_config(home_vieja)
    ConfigViva(cfg).editar_modelos({"clasificar": {"umbral_confianza": "0,6"}})
    assert "  umbral_confianza: 0.6      # por debajo, la sesión queda en esperando_usuario" in lineas(home_vieja)
    assert cfg.clasificar.umbral_confianza == 0.6


@pytest.mark.parametrize(("cambios", "error"), [
    ({"clasificar": {"esfuerzo": "altísimo"}}, "clasificar.esfuerzo inválido"),
    ({"apunte": {"esfuerzo": "MAX"}}, "apunte.esfuerzo inválido"),
    ({"claude": {"facturacion": "gratis"}}, "claude.facturacion inválido"),
    ({"clasificar": {"motor": "gpt"}}, "clasificar.motor inválido"),
    ({"clasificar": {"umbral_confianza": "mucho"}}, "número inválido"),
    ({"clasificar": {"umbral_confianza": 1.5}}, "entre 0 y 1"),
    ({"claude": {"reintentar_api_h": 0}}, "mayor que 0"),
    ({"claude": {"reintentar_api_h": True}}, "número inválido"),
    ({"apunte": {"modelo": "  "}}, "falta apunte.modelo"),
    ({"transcribir": {"modelo": None}}, "falta transcribir.modelo"),
    ({"servidor": {"port": 1}}, "sección desconocida"),
    ({"apunte": {"motor_latex": "lualatex"}}, "campos desconocidos"),
    ({"claude": "max"}, "se esperaba un mapa"),
    (["claude", "max"], "se esperaba un mapa de secciones"),
    # Cada tarea con un modelo de su proveedor.
    ({"clasificar": {"motor": "claude"}}, "mismo proveedor"),
    ({"clasificar": {"modelo": "claude-sonnet-5-5"}}, "mismo proveedor"),
    ({"apunte": {"modelo": "gemini-flash-latest"}}, "lo escribe Claude"),
    ({"transcribir": {"modelo": "claude-haiku-5-5"}}, "transcribir usa Gemini"),
    ({"apunte": {"modelo": "models/gemini-flash-latest"}}, "lo escribe Claude"),   # el id largo de Google
    ({"clasificar": {"motor": "claude", "modelo": "models/gemini-flash-latest"}}, "mismo proveedor"),
    # Un id de modelo no lleva espacios (en el YAML quedaría un bloque `|`).
    ({"apunte": {"modelo": "claude-opus-5-5\nmax"}}, "no lleva espacios"),
    ({"clasificar": {"modelo": "gemini flash"}}, "no lleva espacios"),
    # Una parte válida y otra no: no se escribe nada.
    ({"apunte": {"modelo": "claude-opus-5-5"}, "claude": {"facturacion": "gratis"}}, "facturacion inválido"),
])
def test_modelos_inválidos_no_escriben(home_vieja: Path, cambios, error):
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    antes = (home_vieja / "config.yaml").read_bytes()
    with pytest.raises(ConfigError, match=error):
        viva.editar_modelos(cambios)
    assert (home_vieja / "config.yaml").read_bytes() == antes
    assert (cfg.clasificar.motor, cfg.apunte.modelo, cfg.claude.facturacion) == ("gemini", "opus", "api_luego_max")


def test_valida_el_documento_entero(home_vieja: Path):
    """Si el archivo quedó inválido por otra cosa (una edición a mano), no se escribe encima."""
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    ruta = home_vieja / "config.yaml"
    roto = ruta.read_bytes().replace(b"titulo_regex: null ", b"titulo_regex: '([sin cerrar'")
    ruta.write_bytes(roto)
    with pytest.raises(ConfigError, match="regex"):
        viva.editar_modelos({"claude": {"facturacion": "max"}})
    assert ruta.read_bytes() == roto
    assert cfg.claude.facturacion == "api_luego_max"


def test_config_de_ejemplo(home_ejemplo: Path):
    """Sobre config.example.yaml: cambia en el lugar, conserva comentarios y el CRLF."""
    antes = lineas(home_ejemplo)
    cfg = cargar_config(home_ejemplo)
    ConfigViva(cfg).editar_modelos({"claude": {"facturacion": "max"},
                                    "apunte": {"modelo": "claude-sonnet-5-5", "esfuerzo": "xhigh"},
                                    "clasificar": {"motor": "gemini", "modelo": "gemini-flash-latest"}})
    despues = lineas(home_ejemplo)
    assert comentarios(despues) == comentarios(antes)
    assert b"\n" not in (home_ejemplo / "config.yaml").read_bytes().replace(b"\r\n", b"")
    assert cfg.claude.facturacion == "max"
    assert (cfg.apunte.modelo, cfg.apunte.esfuerzo) == ("claude-sonnet-5-5", "xhigh")
    assert (cfg.clasificar.motor, cfg.clasificar.modelo) == ("gemini", "gemini-flash-latest")
    assert cargar_config(home_ejemplo).claude.facturacion == "max"


# --- modelo del apunte por materia --------------------------------------------------------------

def test_modelo_del_apunte_por_materia(home_vieja: Path):
    original = (home_vieja / "config.yaml").read_bytes()
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    assert materia_cruda(home_vieja, "analisis")["apunte"] is None
    assert cfg.modelo_apunte(cfg.materia("analisis")) == ("opus", "high", "global")

    m = viva.editar_materia("analisis", {"apunte": {"modelo": "claude-opus-5-5", "esfuerzo": "max"}})
    assert m.apunte == ModeloApunte(modelo="claude-opus-5-5", esfuerzo="max")
    assert (
        "    tipos_clase: [teorica, practica]\n"
        "    apunte:\n"
        "      modelo: claude-opus-5-5\n"
        "      esfuerzo: max\n"
        "\n"
        "  - id: historia\n") in texto(home_vieja)
    assert cfg.modelo_apunte(cfg.materia("analisis")) == ("claude-opus-5-5", "max", "materia")
    assert cfg.modelo_apunte(cfg.materia("riesgo")) == ("opus", "high", "global")
    assert materia_cruda(home_vieja, "analisis")["apunte"] == {"modelo": "claude-opus-5-5", "esfuerzo": "max"}
    assert materia_cruda(home_vieja, "riesgo")["apunte"] is None

    # Cambiar: sin esfuerzo propio, la clave no va y hereda el esfuerzo global.
    viva.editar_materia("analisis", {"apunte": {"modelo": "claude-sonnet-5-5", "esfuerzo": ""}})
    assert "    apunte:\n      modelo: claude-sonnet-5-5\n\n  - id: historia\n" in texto(home_vieja)
    assert cfg.modelo_apunte(cfg.materia("analisis")) == ("claude-sonnet-5-5", "high", "materia")
    assert materia_cruda(home_vieja, "analisis")["apunte"] == {"modelo": "claude-sonnet-5-5", "esfuerzo": None}
    # El global cambia y la materia sin esfuerzo propio lo sigue, sin reiniciar.
    viva.editar_modelos({"apunte": {"esfuerzo": "low"}})
    assert cfg.modelo_apunte(cfg.materia("analisis")) == ("claude-sonnet-5-5", "low", "materia")

    # Borrar: vuelve a heredar y el bloque de la materia queda como estaba.
    viva.editar_materia("analisis", {"apunte": None})
    assert cfg.materia("analisis").apunte is None
    assert cfg.modelo_apunte(cfg.materia("analisis")) == ("opus", "low", "global")
    viva.editar_modelos({"apunte": {"esfuerzo": "high"}})
    assert (home_vieja / "config.yaml").read_bytes() == original.replace(
        b"# alias o id de modelo de Claude\r\n", b"# alias o id de modelo de Claude\r\n  esfuerzo: high\r\n")


def test_apunte_de_materia_al_lado_de_comentarios(home_vieja: Path):
    """La materia termina en un `estilo: |` con un comentario abajo: el comentario sigue al final."""
    original = (home_vieja / "config.yaml").read_bytes()
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    viva.editar_materia("historia", {"apunte": {"modelo": "claude-haiku-5-5", "esfuerzo": "medium"}})
    assert (
        "    estilo: |\n"
        "      Con fechas.\n"
        "    apunte:\n"
        "      modelo: claude-haiku-5-5\n"
        "      esfuerzo: medium\n"
        "    # sin calendario todavía\n"
        "\n"
        "  - id: riesgo\n") in texto(home_vieja)
    assert cfg.materia("historia").estilo == "Con fechas.\n"
    viva.editar_materia("historia", {"apunte": {"modelo": "claude-haiku-5-5", "esfuerzo": None}})
    assert "      modelo: claude-haiku-5-5\n    # sin calendario todavía\n" in texto(home_vieja)
    # Modelo vacío = hereda: se borra la clave y el archivo vuelve a ser el de antes.
    viva.editar_materia("historia", {"apunte": {"modelo": "", "esfuerzo": "max"}})
    assert (home_vieja / "config.yaml").read_bytes() == original
    # La última materia del archivo también.
    viva.editar_materia("riesgo", {"apunte": {"modelo": "claude-opus-5-5"}})
    assert texto(home_vieja).endswith("    tipos_clase: [teorica, practica]\n    apunte:\n"
                                      "      modelo: claude-opus-5-5\n")
    viva.editar_materia("riesgo", {"apunte": None})
    assert (home_vieja / "config.yaml").read_bytes() == original


def test_borrar_apunte_entre_comentarios(tmp_path: Path):
    """Los comentarios de arriba y de abajo de la clave borrada quedan, con su sangría."""
    home = tmp_path / "datos"
    home.mkdir()
    (home / "config.yaml").write_text(
        "materias:\n"
        "  - id: h\n"
        "    nombre: H\n"
        "    estilo: |\n"
        "      Con fechas.\n"
        "    # nota del estilo\n"
        "    apunte:\n"
        "      modelo: claude-opus-5-5   # el bueno\n"
        "    # fin de h\n"
        "\n"
        "  - id: r\n"
        "    nombre: R\n", encoding="utf-8")
    cfg = cargar_config(home)
    ConfigViva(cfg).editar_materia("h", {"apunte": None})
    assert (home / "config.yaml").read_text(encoding="utf-8") == (
        "materias:\n"
        "  - id: h\n"
        "    nombre: H\n"
        "    estilo: |\n"
        "      Con fechas.\n"
        "    # nota del estilo\n"
        "    # fin de h\n"
        "\n"
        "  - id: r\n"
        "    nombre: R\n")
    assert cfg.materia("h").apunte is None


@pytest.mark.parametrize(("apunte", "error"), [
    ({"modelo": "claude-opus-5-5", "esfuerzo": "altísimo"}, "apunte.esfuerzo inválido"),
    ({"modelo": "gemini-flash-latest"}, "lo escribe Claude"),
    ({"modelo": "models/gemini-flash-latest"}, "lo escribe Claude"),
    ({"modelo": "claude opus"}, "no lleva espacios"),
    ({"modelo": "claude-opus-5-5", "motor": "otro"}, "campos desconocidos"),
    ("claude-opus-5-5", "se esperaba"),
])
def test_apunte_de_materia_inválido(home_vieja: Path, apunte, error):
    cfg = cargar_config(home_vieja)
    antes = (home_vieja / "config.yaml").read_bytes()
    with pytest.raises(ConfigError, match=error):
        ConfigViva(cfg).editar_materia("riesgo", {"apunte": apunte})
    assert (home_vieja / "config.yaml").read_bytes() == antes
    assert cfg.materia("riesgo").apunte is None


def test_apunte_de_materia_tras_una_lista_en_bloque(tmp_path: Path):
    """`tipos_clase` en bloque (`- teorica`): ruamel guarda lo que sigue (la línea en blanco y el
    comentario de la materia siguiente) en el último ítem; el apunte nuevo va arriba de eso."""
    home = tmp_path / "datos"
    home.mkdir()
    original = (
        "materias:\n"
        "  - id: a\n"
        "    nombre: A\n"
        "    tipos_clase:\n"
        "      - teorica\n"
        "      - practica   # pr\n"
        "\n"
        "  # de b\n"
        "  - id: b\n"
        "    nombre: B\n")
    (home / "config.yaml").write_text(original, encoding="utf-8")
    cfg = cargar_config(home)
    viva = ConfigViva(cfg)
    viva.editar_materia("a", {"apunte": {"modelo": "claude-opus-5-5"}})
    assert (home / "config.yaml").read_text(encoding="utf-8") == (
        "materias:\n"
        "  - id: a\n"
        "    nombre: A\n"
        "    tipos_clase:\n"
        "      - teorica\n"
        "      - practica   # pr\n"
        "    apunte:\n"
        "      modelo: claude-opus-5-5\n"
        "\n"
        "  # de b\n"
        "  - id: b\n"
        "    nombre: B\n")
    assert cfg.modelo_apunte(cfg.materia("a"))[0] == "claude-opus-5-5"
    viva.editar_materia("a", {"apunte": None})
    assert (home / "config.yaml").read_text(encoding="utf-8") == original


def test_conserva_el_bom(tmp_path: Path):
    home = tmp_path / "datos"
    home.mkdir()
    (home / "config.yaml").write_bytes("﻿# config\r\napunte:\r\n  modelo: opus\r\n".encode("utf-8"))
    cfg = cargar_config(home)
    ConfigViva(cfg).editar_modelos({"apunte": {"esfuerzo": "low"}})
    assert (home / "config.yaml").read_bytes() == \
        "﻿# config\r\napunte:\r\n  modelo: opus\r\n  esfuerzo: low\r\n".encode("utf-8")
    assert cfg.apunte.esfuerzo == "low"


def test_apunte_escrito_a_mano(home_vieja: Path):
    """Un `apunte: {…}` en flujo se edita en el lugar; un `apunte: null` se reemplaza por un bloque."""
    ruta = home_vieja / "config.yaml"
    crudo = ruta.read_bytes().decode("utf-8")
    crudo = crudo.replace("    # sin calendario todavía\r\n",
                          "    apunte: {modelo: claude-haiku-5-5}   # rápido\r\n    # sin calendario todavía\r\n")
    crudo = crudo.replace("    carpeta: 'C:\\Materias-de-prueba\\Riesgo'\r\n",
                          "    carpeta: 'C:\\Materias-de-prueba\\Riesgo'\r\n    apunte: null\r\n")
    ruta.write_bytes(crudo.encode("utf-8"))
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    assert cfg.modelo_apunte(cfg.materia("historia")) == ("claude-haiku-5-5", "high", "materia")
    assert materia_cruda(home_vieja, "historia")["apunte"] == {"modelo": "claude-haiku-5-5", "esfuerzo": None}
    assert materia_cruda(home_vieja, "riesgo")["apunte"] is None

    viva.editar_materia("historia", {"apunte": {"modelo": "claude-haiku-5-5", "esfuerzo": "low"}})
    linea = next(x for x in texto(home_vieja).split("\n") if x.startswith("    apunte:"))
    assert linea.startswith("    apunte: {modelo: claude-haiku-5-5, esfuerzo: low}") and linea.endswith("# rápido")
    assert cfg.materia("historia").apunte.esfuerzo == "low"

    viva.editar_materia("riesgo", {"apunte": ModeloApunte(modelo="claude-opus-5-5", esfuerzo="max")})
    assert ("    carpeta: 'C:\\Materias-de-prueba\\Riesgo'\n"
            "    apunte:\n      modelo: claude-opus-5-5\n      esfuerzo: max\n"
            "    tipos_clase: [teorica, practica]\n") in texto(home_vieja)
    assert cfg.modelo_apunte(cfg.materia("riesgo")) == ("claude-opus-5-5", "max", "materia")


def test_agregar_materia_con_apunte(home_vieja: Path):
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    viva.agregar_materia({"id": "macro", "nombre": "Macro", "tipos_clase": ["teorica"],
                          "apunte": {"modelo": "claude-sonnet-5-5", "esfuerzo": "medium"}, "activa": False})
    assert texto(home_vieja).endswith(
        "  - id: macro\n"
        "    nombre: Macro\n"
        "    calendario:\n"
        "      calendario: null\n"
        "      titulo_regex: null\n"
        "    tipos_clase: [teorica]\n"
        "    apunte:\n"
        "      modelo: claude-sonnet-5-5\n"
        "      esfuerzo: medium\n"
        "    activa: false\n")
    assert cfg.modelo_apunte(cfg.materia("macro")) == ("claude-sonnet-5-5", "medium", "materia")
    assert materia_cruda(home_vieja, "macro")["apunte"] == {"modelo": "claude-sonnet-5-5", "esfuerzo": "medium"}
    # Sin modelo propio no se escribe la clave.
    viva.agregar_materia({"id": "micro", "nombre": "Micro", "apunte": {"modelo": "", "esfuerzo": "max"}})
    assert "apunte" not in texto(home_vieja).split("  - id: micro\n")[1]
    assert cfg.modelo_apunte(cfg.materia("micro")) == ("opus", "high", "global")


def test_el_vigilante_no_pisa_las_ediciones(home_vieja: Path):
    """Con el vigilante andando, una edición del panel queda aplicada (no vuelve a la anterior)."""
    import time

    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg, intervalo_s=0.02)
    viva.iniciar()
    try:
        viva.editar_modelos({"claude": {"facturacion": "api"}, "apunte": {"modelo": "claude-opus-5-5"}})
        viva.editar_materia("riesgo", {"apunte": {"modelo": "claude-haiku-5-5", "esfuerzo": "low"}})
        time.sleep(0.2)
        assert cfg.claude.facturacion == "api" and cfg.apunte.modelo == "claude-opus-5-5"
        assert cfg.modelo_apunte(cfg.materia("riesgo")) == ("claude-haiku-5-5", "low", "materia")
        assert viva.error is None and viva.recargada
    finally:
        viva.detener()


# --- tipo de revisión del transcript por materia (§15) ----------------------------------------------

@pytest.mark.parametrize(("datos", "tipo"), [
    ({}, "automatica"),                                        # el default
    ({"revision": "automatica"}, "automatica"),
    ({"revision": "asistida"}, "asistida"),
    ({"revision": "apagada"}, "apagada"),
    ({"revision": " Asistida "}, "asistida"),
    ({"revision": None}, "automatica"),                        # `revision:` vacío
    ({"revisar_transcript": True}, "automatica"),              # el switch viejo
    ({"revisar_transcript": False}, "apagada"),
    ({"revisar_transcript": None}, "automatica"),
    ({"revisar_transcript": "false"}, "apagada"),              # como el bool de antes
    ({"revisar_transcript": "no"}, "apagada"),
    ({"revisar_transcript": 0}, "apagada"),
    ({"revisar_transcript": "true"}, "automatica"),
    ({"revisar_transcript": False, "revision": "asistida"}, "asistida"),   # el tipo gana
])
def test_tipo_de_revision(datos, tipo):
    m = Materia(id="macro", nombre="Macro", **datos)
    assert m.revision == tipo
    assert m.revisar_transcript is (tipo != "apagada")


def test_tipo_de_revision_inválido(home_vieja: Path):
    with pytest.raises(ValidationError):
        Materia(id="macro", nombre="Macro", revision="a-veces")
    with pytest.raises(ValidationError, match="revisar_transcript inválido"):
        Materia(id="macro", nombre="Macro", revisar_transcript="a veces")
    ruta = home_vieja / "config.yaml"
    ruta.write_text(texto(home_vieja).replace('    tipos_clase: [teorica, practica]\n\n  - id: historia',
                                              '    revision: a-veces\n\n  - id: historia', 1), encoding="utf-8")
    with pytest.raises(ConfigError, match="revision"):
        cargar_config(home_vieja)


def test_guardar_el_tipo_de_revisión(home_vieja: Path):
    original = (home_vieja / "config.yaml").read_bytes()
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    assert cfg.materia("riesgo").revision == "automatica"
    assert materia_cruda(home_vieja, "riesgo")["revision"] == "automatica"
    # La automática es el default: no se escribe.
    viva.editar_materia("riesgo", {"revision": "automatica"})
    assert (home_vieja / "config.yaml").read_bytes() == original
    for tipo in ("asistida", "apagada"):
        viva.editar_materia("riesgo", {"revision": tipo})
        assert cfg.materia("riesgo").revision == tipo and materia_cruda(home_vieja, "riesgo")["revision"] == tipo
        assert texto(home_vieja).endswith(f"    tipos_clase: [teorica, practica]\n    revision: {tipo}\n")
        assert cfg.materia("historia").revision == "automatica"
    # Volver a la automática con la clave escrita la deja escrita (como las otras claves).
    viva.editar_materia("riesgo", {"revision": "automatica"})
    assert cfg.materia("riesgo").revision == "automatica"
    assert texto(home_vieja).endswith("    revision: automatica\n")
    # Un tipo que no existe no escribe.
    antes = (home_vieja / "config.yaml").read_bytes()
    with pytest.raises(ConfigError, match="tipo de revisión inválido"):
        viva.editar_materia("riesgo", {"revision": "a-veces"})
    assert (home_vieja / "config.yaml").read_bytes() == antes
    # El campo viejo ya no se edita.
    with pytest.raises(ConfigError, match="campos desconocidos"):
        viva.editar_materia("riesgo", {"revisar_transcript": False})


CONFIG_SWITCH_VIEJO = """\
materias:
  - id: macro
    nombre: Macro
    revisar_transcript: false   # se lo mando al profe a mano
    # el profe, con nombre
    profesor: Laura Inés Gómez

  - id: historia
    nombre: Historia
    revisar_transcript: true
    activa: true

  - id: riesgo
    nombre: Riesgo
    revisar_transcript: false
"""


def test_el_switch_viejo_se_va_al_guardar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "Classmer-data"
    home.mkdir()
    (home / "config.yaml").write_bytes(CONFIG_SWITCH_VIEJO.replace("\n", "\r\n").encode("utf-8"))
    (home / ".env").write_text("CLASSMER_TOKEN=x\n", encoding="utf-8")
    monkeypatch.setenv("CLASSMER_HOME", str(home))
    cfg = cargar_config(home)
    viva = ConfigViva(cfg)
    assert [m.revision for m in cfg.materias] == ["apagada", "automatica", "apagada"]
    assert [materia_cruda(home, m)["revision"] for m in ("macro", "historia", "riesgo")] == \
        ["apagada", "automatica", "apagada"]
    # Guardar sin el tipo (otro cliente) no lo toca.
    viva.editar_materia("macro", {"numero_clase_inicial": 2})
    assert "revisar_transcript: false   # se lo mando" in texto(home)
    # Un tipo que no es el default va en el lugar de la clave vieja, con su comentario (en su columna).
    viva.editar_materia("macro", {"revision": "asistida"})
    assert ("  - id: macro\n"
            "    nombre: Macro\n"
            "    revision: asistida          # se lo mando al profe a mano\n"
            "    # el profe, con nombre\n"
            "    profesor: Laura Inés Gómez\n") in texto(home)
    # El default: la clave vieja se va y no se escribe nada; lo de alrededor queda.
    viva.editar_materia("historia", {"revision": "automatica"})
    assert "  - id: historia\n    nombre: Historia\n    activa: true\n\n  - id: riesgo\n" in texto(home)
    viva.editar_materia("riesgo", {"revision": "automatica"})
    assert texto(home).endswith("  - id: riesgo\n    nombre: Riesgo\n")
    assert "revisar_transcript" not in texto(home)
    assert b"\n" not in (home / "config.yaml").read_bytes().replace(b"\r\n", b"")   # sigue en CRLF
    assert [m.revision for m in cfg.materias] == ["asistida", "automatica", "automatica"]
    assert cargar_config(home).materia("macro").revision == "asistida"


def test_switch_viejo_sin_comillas_en_el_formulario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PyYAML (cargar) lee `no` como false y ruamel (el formulario) como texto: los dos dan apagada."""
    home = tmp_path / "Classmer-data"
    home.mkdir()
    (home / "config.yaml").write_text("materias:\n  - id: macro\n    nombre: Macro\n    revisar_transcript: no\n"
                                      "  - id: micro\n    nombre: Micro\n    revisar_transcript: 'a veces'\n",
                                      encoding="utf-8")
    (home / ".env").write_text("CLASSMER_TOKEN=x\n", encoding="utf-8")
    monkeypatch.setenv("CLASSMER_HOME", str(home))
    assert materia_cruda(home, "macro")["revision"] == "apagada"
    assert materia_cruda(home, "micro")["revision"] is None   # inválido: el formulario no marca ninguno
    with pytest.raises(ConfigError, match="revisar_transcript inválido"):
        cargar_config(home)


def test_materia_nueva_con_tipo_de_revisión(home_vieja: Path):
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    viva.agregar_materia({"id": "macro", "nombre": "Macro", "revision": "automatica"})
    viva.agregar_materia({"id": "micro", "nombre": "Micro", "revision": "asistida"})
    assert cfg.materia("macro").revision == "automatica" and cfg.materia("micro").revision == "asistida"
    bloque_macro = texto(home_vieja).split("  - id: macro\n")[1].split("  - id: micro\n")[0]
    assert "revision" not in bloque_macro
    assert "    revision: asistida\n" in texto(home_vieja).split("  - id: micro\n")[1]


def test_recarga_del_tipo_de_revisión(home_vieja: Path):
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    ruta = home_vieja / "config.yaml"
    original = texto(home_vieja)
    ruta.write_text(original.replace("    # sin calendario todavía\n",
                                     "    revision: asistida\n    # sin calendario todavía\n"), encoding="utf-8")
    assert viva.recargar()
    assert cfg.materia("historia").revision == "asistida" and cfg.materia("historia").revisar_transcript is True
    ruta.write_text(original.replace("    # sin calendario todavía\n",
                                     "    revisar_transcript: false\n    # sin calendario todavía\n"), encoding="utf-8")
    assert viva.recargar() and cfg.materia("historia").revision == "apagada"
    # Un tipo que no existe, escrito a mano: sigue la config anterior y el error queda a la vista.
    ruta.write_text(original.replace("    # sin calendario todavía\n",
                                     "    revision: siempre\n    # sin calendario todavía\n"), encoding="utf-8")
    assert not viva.recargar()
    assert viva.error and "revision" in viva.error
    assert cfg.materia("historia").revision == "apagada"
    ruta.write_text(original, encoding="utf-8")
    assert viva.recargar() and viva.error is None and cfg.materia("historia").revision == "automatica"


# --- apunte.web (búsqueda en internet del agente del apunte) --------------------------------------

def test_web_del_apunte_por_defecto(home_vieja: Path):
    cfg = cargar_config(home_vieja)  # CONFIG_VIEJA no tiene `web:`: prendida, con los topes por defecto
    assert (cfg.apunte.web.activa, cfg.apunte.web.max_busquedas, cfg.apunte.web.max_paginas) == (True, 12, 8)


@pytest.mark.parametrize("bloque, esperado", [
    ("  web:\n    activa: false\n", (False, 12, 8)),
    ("  web:\n    max_busquedas: 4\n", (True, 4, 8)),
    ("  web:\n    activa: true\n    max_busquedas: 1\n    max_paginas: 50\n", (True, 1, 50)),
    ("  web:\n", (True, 12, 8)),                          # `web:` vacío
])
def test_web_del_apunte_escrita_a_mano(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bloque, esperado):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.yaml").write_text(CONFIG_VIEJA.replace("  timeout_min: 45            # tope total del agente "
                                                           "(escribir + compilar)\n",
                                                           "  timeout_min: 45\n" + bloque), encoding="utf-8")
    monkeypatch.setenv("CLASSMER_HOME", str(home))
    w = cargar_config(home).apunte.web
    assert (w.activa, w.max_busquedas, w.max_paginas) == esperado


@pytest.mark.parametrize("bloque", [
    "  web:\n    max_busquedas: 0\n",
    "  web:\n    max_paginas: 51\n",
    "  web:\n    max_paginas: -1\n",
    "  web:\n    max_busquedas: muchas\n",
    "  web:\n    max_busquedas: 2.5\n",
    "  web:\n    max_busquedas: true\n",
    "  web:\n    activa: quizás\n",
    "  web: si\n",
])
def test_web_del_apunte_invalida(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bloque):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.yaml").write_text(CONFIG_VIEJA.replace("  timeout_min: 45            # tope total del agente "
                                                           "(escribir + compilar)\n",
                                                           "  timeout_min: 45\n" + bloque), encoding="utf-8")
    monkeypatch.setenv("CLASSMER_HOME", str(home))
    with pytest.raises((ConfigError, ValidationError)):
        cargar_config(home)


def test_casillero_de_la_web(home_vieja: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = cargar_config(home_vieja)
    viva = ConfigViva(cfg)
    antes = lineas(home_vieja)
    # Prendida (el default) y sin `web:` en el archivo: no se escribe nada.
    escrituras: list[Any] = []
    real = config_vivo.escribir_atomico
    monkeypatch.setattr(config_vivo, "escribir_atomico", lambda *a, **k: (escrituras.append(a), real(*a, **k)))
    viva.editar_modelos({"apunte": {"web": True}})
    assert escrituras == [] and lineas(home_vieja) == antes

    # Apagarla agrega sólo `web:` / `activa: false` al final de `apunte:`, y el resto queda igual.
    viva.editar_modelos({"apunte": {"web": False}})
    despues = lineas(home_vieja)
    assert [linea for linea in despues if linea not in antes] == ["  web:", "    activa: false"]
    assert [linea for linea in antes if linea not in despues] == []
    i = despues.index("  web:")
    assert despues[i - 1].startswith("  timeout_min: 45") and despues[i + 2] == ""
    assert comentarios(despues) == comentarios(antes)
    assert cfg.apunte.web.activa is False  # aplicada en vivo

    # Prenderla de nuevo cambia el valor (la clave ya estaba) y respeta los topes escritos a mano.
    texto_ = texto(home_vieja).replace("    activa: false\n", "    activa: false   # la apagué\n"
                                       "    max_busquedas: 3\n")
    (home_vieja / "config.yaml").write_text(texto_, encoding="utf-8")
    viva.recargar(forzar=True)
    viva.editar_modelos({"apunte": {"web": True, "esfuerzo": "high"}})
    t = texto(home_vieja)
    assert re.search(r"\n    activa: true +# la apagué\n    max_busquedas: 3\n", t), t  # el comentario se queda
    assert (cfg.apunte.web.activa, cfg.apunte.web.max_busquedas) == (True, 3)


def test_casillero_de_la_web_con_topes_sin_activa(home_vieja: Path):
    t = texto(home_vieja).replace("  timeout_min: 45            # tope total del agente (escribir + compilar)\n",
                                  "  timeout_min: 45\n  web:\n    max_paginas: 2\n")
    (home_vieja / "config.yaml").write_text(t, encoding="utf-8")
    cfg = cargar_config(home_vieja)
    ConfigViva(cfg).editar_modelos({"apunte": {"web": False}})
    assert "  web:\n    activa: false\n    max_paginas: 2\n" in texto(home_vieja)
    assert (cfg.apunte.web.activa, cfg.apunte.web.max_paginas) == (False, 2)


def test_casillero_de_la_web_sin_seccion_apunte(home: Path, cfg):
    ConfigViva(cfg).editar_modelos({"apunte": {"web": False}})
    guardada = yaml.safe_load(texto(home))
    assert guardada["apunte"] == {"web": {"activa": False}} and cfg.apunte.web.activa is False


@pytest.mark.parametrize("valor", ["si", 1, None, "on"])
def test_casillero_de_la_web_invalido(home_vieja: Path, valor):
    antes = (home_vieja / "config.yaml").read_bytes()
    with pytest.raises(ConfigError, match="apunte.web tiene que ser true o false"):
        ConfigViva(cargar_config(home_vieja)).editar_modelos({"apunte": {"web": valor}})
    assert (home_vieja / "config.yaml").read_bytes() == antes


def test_config_de_ejemplo_trae_la_web(home_ejemplo: Path):
    cfg = cargar_config(home_ejemplo)
    assert (cfg.apunte.web.activa, cfg.apunte.web.max_busquedas, cfg.apunte.web.max_paginas) == (True, 12, 8)
    assert "web:" in texto(home_ejemplo) and "max_busquedas: 12" in texto(home_ejemplo)
    ConfigViva(cfg).editar_modelos({"apunte": {"web": False}})
    t = texto(home_ejemplo)
    assert "activa: false" in t and "max_busquedas: 12" in t and cfg.apunte.web.activa is False
