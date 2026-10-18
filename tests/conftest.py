"""Общие приспособления. Ни сети, ни живого стенда: всё над демонстрационным набором
и над его копиями во временном каталоге."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "demo"


@pytest.fixture(scope="session")
def demo_root() -> Path:
    assert (DEMO / "atlas.yaml").is_file(), "нет демонстрационного набора: python demo/generate.py"
    return DEMO


@pytest.fixture(scope="session")
def demo_graph(demo_root):
    from veha.core.graph import build_root

    return build_root(demo_root)


BASE_URL = "http://127.0.0.1:8770"


def make_client(root, **kwargs):
    """Тестовый клиент, который ходит так же, как браузер на машине: Host — loopback."""
    from fastapi.testclient import TestClient

    from veha.viz.app import create_app

    return TestClient(create_app(root, **kwargs), base_url=BASE_URL)


@pytest.fixture(scope="session")
def client(demo_root):
    return make_client(demo_root)


@pytest.fixture
def small_root(tmp_path, demo_root) -> Path:
    """Копия набора без allure-results: быстрая, и её можно портить."""
    root = tmp_path / "set"
    root.mkdir()
    for name in ("atlas.yaml", "app_map.yaml", "coverage.json"):
        shutil.copy(demo_root / name, root / name)
    shutil.copytree(demo_root / "atlas", root / "atlas")
    shutil.copytree(demo_root / "live", root / "live")
    return root


def artifact_path(root: Path, kind: str, system: str = "default", env: str = "default") -> Path:
    from partest_atlas.artifacts import Scope

    return Scope(system, env).path(root / "atlas", kind)


def rewrite(path: Path, **changes) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(changes)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
