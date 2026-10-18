"""`veha demo`: набор берётся из установленного пакета, сервер — только просмотр.

Сервер в этих тестах не слушает порт: `serve_app` подменяется, и проверяется приложение,
которое команда собрала бы для uvicorn. Настоящий запуск из колеса в чистом окружении —
ручная проверка перед выпуском (`/api/graph` на поднятом `veha demo --no-browser`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import veha
from veha import cli
from veha import demo as veha_demo
from tests.conftest import REPO


def _tree(root: Path) -> dict:
    """Снимок каталога: имя → (размер, mtime). Любая запись его меняет."""
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*")}


@pytest.fixture
def started(monkeypatch):
    """Приложение, которое `veha demo` отдал бы uvicorn, и порт."""
    seen = {}

    def fake_serve(app, port):
        seen["app"], seen["port"] = app, port

    monkeypatch.setattr(cli, "serve_app", fake_serve)
    # atexit от каждого вызова копился бы до конца сессии pytest
    monkeypatch.setattr(cli.atexit, "register", lambda fn: None)
    return seen


def test_data_ships_inside_the_package():
    root = veha_demo.data_root()
    assert root.parent == Path(veha.__file__).resolve().parent
    assert root.name == "demo_data"
    assert veha_demo.missing(root) == []
    assert len(list((root / "allure-results").glob("*-result.json"))) == 605
    # в каталоге данных пакета нет кода: генератор в колесо не входит
    assert not list(root.rglob("*.py"))


def test_generator_writes_into_the_package():
    """Источник набора один: генератор пишет туда, откуда его берёт `veha demo`."""
    text = (REPO / "demo" / "generate.py").read_text(encoding="utf-8")
    assert 'ROOT = Path(__file__).resolve().parents[1] / "veha" / "demo_data"' in text
    # и рядом с генератором второй копии набора нет
    assert not (REPO / "demo" / "atlas.yaml").exists()
    assert not (REPO / "demo" / "allure-results").exists()


def test_demo_has_no_write_or_root_keys():
    parser = cli.build_parser()
    for extra in (["--allow-run"], ["--root", "."], ["--system", "x"]):
        with pytest.raises(SystemExit):
            parser.parse_args(["demo", *extra])
    args = parser.parse_args(["demo", "--port", "8901", "--no-browser"])
    assert (args.port, args.no_browser, args.func) == (8901, True, cli.demo)


def test_demo_serves_packaged_set_read_only(started, monkeypatch, capsys):
    # переменные области, оставшиеся в оболочке от настоящего проекта, демо не сбивают
    monkeypatch.setenv("ATLAS_SYSTEM", "shop")
    monkeypatch.setenv("ATLAS_ENV", "test")
    root = veha_demo.data_root()
    before = _tree(root)

    assert cli.main(["demo", "--no-browser", "--port", "8901"]) == 0
    app = started["app"]
    assert started["port"] == 8901
    assert app.state.veha.readonly is True
    assert app.state.veha.root == root
    out = capsys.readouterr().out
    assert "только просмотр" in out and "вымышленных данных" in out
    assert "область default/default" in out

    url = "http://127.0.0.1:8901"           # сервер отвечает только на свой Host
    c = TestClient(app, base_url=url)
    g = c.get("/api/graph").json()
    assert g["meta"]["endpoints"] == 177 and g["meta"]["tests"] == 605
    same_origin = {"Origin": url}
    writes = [
        ("/api/runner/start", {"suite": "smoke"}),
        ("/api/runner/stop", None),
        ("/api/runner/clean", None),
        ("/api/rerun", {"nodeid": "tests/x.py::test_x"}),
        ("/api/live/mr", {"iid": 1, "status": "checked"}),
    ]
    for path, body in writes:
        r = c.post(path, json=body, headers=same_origin)
        assert r.status_code == 409, (path, r.status_code, r.text)
        assert "--allow-run" in r.json()["detail"]
    for path in ("/api/artifacts", "/api/runner/suites", "/api/live/containers",
                 "/api/live/mrs"):
        assert c.get(path).status_code == 200, path

    assert _tree(root) == before, "демо записало в каталог пакета"


def test_demo_without_packaged_set_says_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(veha_demo, "DATA", tmp_path / "demo_data")
    assert cli.main(["demo", "--no-browser"]) == 2
    err = capsys.readouterr().err
    assert "atlas.yaml" in err and "demo/generate.py" in err


def test_version_is_one_number():
    """Версия пакета и версия в pyproject не расходятся."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
    assert declared == veha.__version__
