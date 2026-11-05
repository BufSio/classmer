"""Fixtures comunes: una carpeta de datos temporal con una materia de prueba y un token fijo.

Nunca se usa la carpeta real de Classmer-data ni las carpetas reales de las materias.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from classmer.config import Config, cargar_config
from classmer.db import Base, abrir
from classmer.pipeline import Contexto, Servicios

TOKEN = "token-de-prueba-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def requiere(programa: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(shutil.which(programa) is None, reason=f"falta {programa}")


@pytest.fixture(autouse=True)
def sin_claude_de_verdad(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red de seguridad para toda la suite: el clasificador y el apunte usan Claude por defecto. Un
    test que se olvide de inyectar los falsos no puede lanzar el CLI real (el plan Max del usuario) ni
    mandar un pedido a la API de Anthropic."""
    import anthropic._base_client
    import claude_agent_sdk

    def prohibido(*_a, **_k):
        raise AssertionError("un test intentó usar Claude de verdad (inyectá un cliente falso)")

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", prohibido)
    monkeypatch.setattr(anthropic._base_client.SyncAPIClient, "request", prohibido)


@pytest.fixture
def materia_dir(tmp_path: Path) -> Path:
    carpeta = tmp_path / "materia-prueba"
    carpeta.mkdir()
    (carpeta / "PROMPT-AGENTE.md").write_text(
        "# Materia de prueba\nNotación: vectores en negrita.\n", encoding="utf-8"
    )
    return carpeta


@pytest.fixture
def home(tmp_path: Path, materia_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "Classmer-data"
    home.mkdir()
    config = {
        "zona_horaria": "America/Argentina/Buenos_Aires",
        "worker": {"poll_s": 0.05},
        "segmentar": {"bloque_s": 60, "ventana_s": 15, "silencio_largo_s": 8},
        "materias": [
            {
                "id": "prueba",
                "nombre": "Materia de Prueba",
                "carpeta": str(materia_dir),
                "carpeta_clases": "Clases-Classmer",
                "calendario": {"calendario": None, "titulo_regex": "Prueba"},
                "tipos_clase": ["teorica", "practica"],
                # La revisión del transcript (§15) viene automática; los tests de siempre la apagan (es
                # exactamente lo de antes) y los de la revisión eligen el tipo.
                "revision": "apagada",
            },
            {
                "id": "otra",
                "nombre": "Otra Materia",
                "carpeta": str(tmp_path / "otra-materia"),
                "revision": "apagada",
            },
        ],
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    (home / ".env").write_text(f"CLASSMER_TOKEN={TOKEN}\nGEMINI_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("CLASSMER_HOME", str(home))
    monkeypatch.delenv("CLASSMER_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return home


@pytest.fixture
def cfg(home: Path) -> Config:
    return cargar_config(home)


@pytest.fixture
def base(cfg: Config) -> Base:
    return abrir(cfg)


@pytest.fixture
def ctx(cfg: Config, base: Base) -> Contexto:
    return Contexto(cfg=cfg, base=base, servicios=Servicios())


@pytest.fixture
def client(cfg: Config):
    from classmer.api.app import crear_app

    app = crear_app(cfg, iniciar_worker=False)
    with TestClient(app) as c:
        c.headers.update(AUTH)
        yield c
