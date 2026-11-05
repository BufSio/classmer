"""CLI: init (carpeta de datos, config, .env y token), token (texto y QR), serve, modelos y logging."""

from __future__ import annotations

import logging
import logging.handlers
import os
import socket
from pathlib import Path

import pytest

from classmer import cli


@pytest.fixture
def home_vacio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "Classmer-data"
    monkeypatch.setenv("CLASSMER_HOME", str(home))
    monkeypatch.delenv("CLASSMER_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return home


def test_init_crea_todo_y_no_muestra_el_token(home_vacio: Path, capsys):
    assert cli.main(["init"]) == 0
    salida = capsys.readouterr().out
    assert (home_vacio / "config.yaml").is_file()
    assert (home_vacio / "classmer.db").is_file()
    env = (home_vacio / ".env").read_text(encoding="utf-8")
    token = next(l.split("=", 1)[1] for l in env.splitlines() if l.startswith("CLASSMER_TOKEN="))
    assert len(token) >= 40
    assert token not in salida


def test_init_no_cambia_un_token_existente(home_vacio: Path):
    cli.main(["init"])
    antes = (home_vacio / ".env").read_text(encoding="utf-8")
    cli.main(["init"])
    assert (home_vacio / ".env").read_text(encoding="utf-8") == antes


def test_token_texto_y_qr(home_vacio: Path, capsys, monkeypatch: pytest.MonkeyPatch):
    cli.main(["init"])
    capsys.readouterr()
    assert cli.main(["token"]) == 0
    token = capsys.readouterr().out.strip()
    assert len(token) >= 40

    abiertos: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda ruta: abiertos.append(str(ruta)), raising=False)
    assert cli.main(["token", "--qr"]) == 0
    qr = home_vacio / "token-qr.png"
    assert qr.is_file() and qr.read_bytes().startswith(b"\x89PNG")
    assert token not in capsys.readouterr().out
    assert abiertos == [str(qr)]


# --- serve -----------------------------------------------------------------------------------------

@pytest.fixture
def logging_anotado(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No toca el logging global del proceso de tests; anota con qué archivo se configuró."""
    archivos: list[str] = []
    monkeypatch.setattr(cli, "configurar_logging",
                        lambda cfg, nivel=logging.INFO, archivo=cli.LOG_SERVER: archivos.append(archivo))
    return archivos


def test_serve_con_el_puerto_ocupado_no_arranca_el_worker(home: Path, logging_anotado, capsys,
                                                         monkeypatch: pytest.MonkeyPatch):
    # uvicorn arranca el lifespan (y el worker) antes de bindear: con el puerto tomado, una
    # segunda instancia agarraba jobs y los soltaba enseguida. Ahora ni crea la app.
    from classmer.api import app as app_mod

    monkeypatch.setattr(app_mod, "crear_app", lambda *a, **k: pytest.fail("no tenía que crear la app"))
    ocupado = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        ocupado.bind(("127.0.0.1", 0))
        ocupado.listen()
        puerto = ocupado.getsockname()[1]
        codigo = cli.main(["serve", "--host", "127.0.0.1", "--port", str(puerto)])
    finally:
        ocupado.close()
    assert codigo not in (0, None)
    err = capsys.readouterr().err
    assert f"127.0.0.1:{puerto}" in err and "ya hay otro classmer serve" in err
    assert logging_anotado == [cli.LOG_SERVER]


def test_serve_con_el_puerto_libre_levanta_uvicorn(home: Path, logging_anotado,
                                                   monkeypatch: pytest.MonkeyPatch):
    import uvicorn

    from classmer.api import app as app_mod

    llamadas: list[tuple] = []
    monkeypatch.setattr(app_mod, "crear_app", lambda cfg, iniciar_worker: ("app", iniciar_worker))
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **_kw: llamadas.append((app, host, port)))
    sonda = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sonda.bind(("127.0.0.1", 0))
    puerto = sonda.getsockname()[1]
    sonda.close()
    assert cli.main(["serve", "--host", "127.0.0.1", "--port", str(puerto)]) == 0
    assert llamadas == [(("app", True), "127.0.0.1", puerto)]


# --- logging -----------------------------------------------------------------------------------------

def test_configurar_logging_con_otro_archivo(cfg):
    # serve e importar en dos procesos con el mismo RotatingFileHandler rompen la rotación en
    # Windows: los comandos de la CLI escriben en classmer-cli.log.
    raiz = logging.getLogger()
    antes, nivel = list(raiz.handlers), raiz.level
    marca = raiz.__dict__.pop("_classmer", None)
    try:
        cli.configurar_logging(cfg, archivo=cli.LOG_CLI)
        nuevos = [h for h in raiz.handlers if h not in antes]
        archivos = [Path(h.baseFilename).name for h in nuevos
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert archivos == ["classmer-cli.log"]
        assert (cfg.logs_dir / "classmer-cli.log").is_file()
        assert not (cfg.logs_dir / "classmer.log").exists()
    finally:
        for h in [h for h in raiz.handlers if h not in antes]:
            raiz.removeHandler(h)
            h.close()
        raiz.setLevel(nivel)
        raiz.__dict__.pop("_classmer", None)
        if marca is not None:
            raiz._classmer = marca  # type: ignore[attr-defined]


@pytest.mark.parametrize("argumentos", [
    ["importar", "no-existe.wav", "--materia", "prueba"],
    ["estado"],
    ["asignar", "no-hay-tal-sesion", "--materia", "prueba"],
    ["reprocesar", "no-hay-tal-sesion", "--desde", "apunte"],
    ["modelos"],
])
def test_comandos_de_la_cli_usan_su_propio_log(home: Path, logging_anotado, argumentos: list[str]):
    cli.main(argumentos)
    assert logging_anotado == [cli.LOG_CLI]


# --- modelos -----------------------------------------------------------------------------------------

CLAVE_ANTHROPIC = "sk-ant-falsa-123"
CLAVE_GEMINI = "gemini-falsa-456"


@pytest.fixture
def apis_falsas(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Reemplaza los clientes de las APIs de modelos: nunca se llama a una API real."""
    from types import SimpleNamespace

    from classmer.ia import modelos

    def capacidades(estructurada: bool, esfuerzo: bool) -> dict:
        return {"image_input": {"supported": True}, "structured_outputs": {"supported": estructurada},
                "effort": {"supported": esfuerzo, **{n: {"supported": esfuerzo}
                                                     for n in ("low", "medium", "high", "xhigh", "max")}}}

    anthropic_ = [
        SimpleNamespace(id="claude-opus-5-5", display_name="Claude Opus 5.5", capabilities=capacidades(True, True),
                        max_input_tokens=1_000_000, max_tokens=128_000),
        SimpleNamespace(id="claude-sonnet-5-5", display_name="Claude Sonnet 5.5",
                        capabilities=capacidades(True, True), max_input_tokens=1_000_000, max_tokens=128_000),
        SimpleNamespace(id="claude-haiku-4-5", display_name="Claude Haiku 4.5",
                        capabilities=capacidades(True, False), max_input_tokens=200_000, max_tokens=64_000),
    ]
    google = [SimpleNamespace(name="models/gemini-3.5-transcribe", display_name="Gemini 3.5 Transcribe",
                              supported_actions=["generateContent"], input_token_limit=1, output_token_limit=1),
              SimpleNamespace(name="models/gemini-flash-latest", display_name="Gemini Flash Latest",
                              supported_actions=["generateContent"], input_token_limit=1, output_token_limit=1)]
    creados: dict[str, list] = {"anthropic": [], "google": []}

    def crear_anthropic(cfg):
        creados["anthropic"].append(cfg.anthropic_api_key)
        pagina = SimpleNamespace(data=anthropic_, has_more=False, last_id="claude-haiku-4-5")
        return SimpleNamespace(models=SimpleNamespace(list=lambda **_kw: pagina))

    def crear_google(cfg):
        creados["google"].append(cfg.gemini_api_key)
        return SimpleNamespace(models=SimpleNamespace(list=lambda **_kw: iter(google)))

    monkeypatch.setattr(modelos, "_cliente_anthropic", crear_anthropic)
    monkeypatch.setattr(modelos, "_cliente_google", crear_google)
    return creados


def test_modelos_lista_el_catalogo_por_tarea(home: Path, logging_anotado, apis_falsas, capsys):
    import yaml

    ruta = home / "config.yaml"
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    datos["materias"][0]["apunte"] = {"modelo": "claude-sonnet-5-5", "esfuerzo": "max"}
    datos["clasificar"] = {"motor": "gemini", "modelo": "gemini-2.0-viejo"}   # ya no está en el catálogo
    ruta.write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")

    assert cli.main(["modelos"]) == 0
    salida = capsys.readouterr().out
    assert apis_falsas == {"anthropic": [], "google": []}          # sin --redescubrir no consulta nada
    secciones = {t: salida.split(f"\n{t}:\n")[1].split("\n\n")[0] for t in ("transcribir", "clasificar", "apunte")}
    assert "* gemini-3.5-transcribe" in secciones["transcribir"]
    assert "claude" not in secciones["transcribir"]
    assert "gemini" not in secciones["apunte"]
    lineas_apunte = secciones["apunte"].splitlines()
    opus = next(l for l in lineas_apunte if "claude-opus-5-5" in l)
    sonnet = next(l for l in lineas_apunte if "claude-sonnet-5-5" in l)
    assert opus.lstrip().startswith("*") and "apunte.modelo (global) (esfuerzo high)" in opus
    assert sonnet.lstrip().startswith("*") and "materia prueba (esfuerzo max)" in sonnet
    assert not next(l for l in lineas_apunte if "claude-haiku-5-5" in l).lstrip().startswith("*")
    assert "* gemini-2.0-viejo" in secciones["clasificar"] and "no está en el catálogo" in secciones["clasificar"]
    assert "nunca" in salida and "--redescubrir" in salida


def test_modelos_redescubrir_imprime_el_resumen(home: Path, logging_anotado, apis_falsas, capsys):
    (home / ".env").write_text(f"CLASSMER_TOKEN=x\nANTHROPIC_API_KEY={CLAVE_ANTHROPIC}\n"
                               f"GEMINI_API_KEY={CLAVE_GEMINI}\n", encoding="utf-8")
    assert cli.main(["modelos", "--redescubrir"]) == 0
    salida = capsys.readouterr().out
    assert apis_falsas == {"anthropic": [CLAVE_ANTHROPIC], "google": [CLAVE_GEMINI]}
    assert CLAVE_ANTHROPIC not in salida and CLAVE_GEMINI not in salida
    resumen = salida.split("Redescubrir modelos:")[1].split("Catálogo de modelos")[0]
    assert "Anthropic: 3 modelos para Classmer (de 3 que lista la API)" in resumen
    assert "nuevos: claude-haiku-4-5" in resumen
    assert "no aparecieron: claude-fable-5-1, claude-haiku-5-5" in resumen
    assert "Google: 2 modelos para Classmer" in resumen
    apunte = salida.split("\napunte:\n")[1]
    assert "no apareció en el último descubrimiento" in apunte          # fable y haiku 5.5
    assert "anthropic" in salida.split("Último descubrimiento:")[1] and "ok, 3 modelos" in salida
    assert logging_anotado == [cli.LOG_CLI]


def test_modelos_redescubrir_sin_claves_avisa_y_no_toca_nada(home: Path, logging_anotado, apis_falsas, capsys):
    assert cli.main(["modelos", "--redescubrir"]) == 1
    salida = capsys.readouterr().out
    assert apis_falsas == {"anthropic": [], "google": []}
    assert "falta ANTHROPIC_API_KEY" in salida and "falta GEMINI_API_KEY" in salida
    assert "queda lo que había" in salida
    assert "no apareció" not in salida
    assert "no se actualizó: falta GEMINI_API_KEY" in salida.split("Último descubrimiento:")[1]


def test_modelos_muestra_nunca_para_el_proveedor_sin_descubrimiento(home: Path, logging_anotado, capsys):
    from classmer.config import cargar_config
    from classmer.db import abrir

    base = abrir(cargar_config(home))
    with base.conexion() as conn:
        conn.execute("INSERT INTO descubrimientos (proveedor, momento, ok, detalle, cantidad) "
                     "VALUES ('google', '2026-10-16T10:00:00-03:00', 1, NULL, 2)")
    assert cli.main(["modelos"]) == 0
    ultimo = capsys.readouterr().out.split("Último descubrimiento:")[1]
    assert "anthropic  nunca" in ultimo
    assert "google     2026-10-16 10:00  ok, 2 modelos" in ultimo
