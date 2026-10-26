"""Пути из `atlas.yaml`, чьё содержимое сервер отдаёт по HTTP, — только внутри корня.

`screens.images_dir` монтируется как статика целиком, `/api/log` и логи контейнеров
отдают текст файла, `app_map` и манифест экранов уходят в граф. Путь вне корня (`..`,
абсолютный, сам корень), сетевой (UNC) или через ссылку не открывается вовсе: раздел не
отдаётся, причина — в `meta.reportProblems` без самого значения. В контейнере это и
граница тома: `images_dir: ../…` иначе отдавал бы `/proc/self/environ`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from tests.conftest import make_client

CANARY = "canary-7f3a"


def _patch(root: Path, **sections) -> None:
    cfg = root / "atlas.yaml"
    doc = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            doc[key].update(value)
        else:
            doc[key] = value
    cfg.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")


def _outside(tmp_path: Path) -> Path:
    d = tmp_path / "outside"
    d.mkdir()
    (d / "canary.txt").write_text(CANARY, encoding="utf-8")
    (d / "notifier.log").write_text(CANARY, encoding="utf-8")
    (d / "app_map.yaml").write_text(f"note: {CANARY}\n", encoding="utf-8")
    return d


def _problems(root: Path):
    from veha.core.graph import build_root

    return build_root(root)["meta"]["reportProblems"]


@pytest.mark.parametrize("value", ["../outside", "ABS", "."])
def test_images_dir_outside_the_root_is_not_mounted(small_root, tmp_path, value):
    outside = _outside(tmp_path)
    _patch(small_root, screens={"images_dir": str(outside) if value == "ABS" else value})
    c = make_client(small_root)
    assert c.get("/screens/canary.txt").status_code == 404
    assert c.get("/screens/atlas.yaml").status_code == 404
    problems = _problems(small_root)
    assert "screens.images_dir: путь вне корня данных — не отдаётся" in problems
    assert not any(str(outside) in p or "outside" in p for p in problems)


def test_images_dir_inside_the_root_is_served(small_root):
    shots = small_root / "screens"
    shots.mkdir()
    (shots / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    c = make_client(small_root)
    assert c.get("/screens/a.png").status_code == 200
    assert not any(p.startswith("screens.") for p in _problems(small_root))


UNC = [r"\\evil\share\shots", "//evil/share/shots", r"\\?\UNC\evil\share", r"\\.\pipe\x"]


@pytest.mark.parametrize("value", UNC)
def test_unc_paths_are_refused_without_touching_them(small_root, monkeypatch, value):
    """Сетевой путь отвергается по виду строки: ни `stat`, ни `resolve` его не трогают."""
    _patch(small_root, screens={"images_dir": value, "manifest": value + "/m.json"},
           live={"container_logs_dir": value}, app_map=value + "/map.yaml",
           logs={"чужой": value + "/x.log"})
    touched = []
    real_lstat, real_stat = os.lstat, os.stat

    def spy(real):
        def wrapped(path, *a, **kw):
            touched.append(os.fspath(path))
            return real(path, *a, **kw)
        return wrapped

    monkeypatch.setattr(os, "lstat", spy(real_lstat))
    monkeypatch.setattr(os, "stat", spy(real_stat))
    c = make_client(small_root)
    assert c.get("/api/graph").status_code == 200
    assert c.get("/api/log", params={"name": "чужой"}).status_code == 400
    assert not any("evil" in t or "pipe" in t for t in touched)
    problems = _problems(small_root)
    for where in ("screens.images_dir", "screens.manifest", "live.container_logs_dir",
                  "app_map", "logs.чужой"):
        assert any(p.startswith(f"{where}: сетевой путь (UNC)") for p in problems), where


def test_log_and_container_logs_outside_the_root_are_not_read(small_root, tmp_path):
    outside = _outside(tmp_path)
    _patch(small_root, live={"container_logs_dir": "../outside"},
           logs={"чужой": str(outside / "canary.txt"), "соседний": "../outside/canary.txt"})
    c = make_client(small_root)
    for name in ("чужой", "соседний"):
        r = c.get("/api/log", params={"name": name})
        assert r.status_code == 400 and CANARY not in r.text, name
    r = c.get("/api/live/container/notifier/logs")
    assert r.status_code == 200 and CANARY not in r.text
    assert "live.container_logs_dir: путь вне корня данных" in r.json()["note"]


def test_app_map_and_manifest_outside_the_root_are_not_read(small_root, tmp_path):
    outside = _outside(tmp_path)
    (outside / "manifest.json").write_text('{"scenes": [{"id": "' + CANARY + '"}]}',
                                           encoding="utf-8")
    _patch(small_root, app_map="../outside/app_map.yaml",
           screens={"manifest": str(outside / "manifest.json")})
    c = make_client(small_root)
    assert CANARY not in c.get("/api/graph").text
    problems = _problems(small_root)
    assert "app_map: путь вне корня данных — не отдаётся" in problems
    assert "screens.manifest: путь вне корня данных — не отдаётся" in problems


def _link_dir(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_a_link_inside_the_root_to_the_outside_is_refused(small_root, tmp_path):
    outside = _outside(tmp_path)
    try:
        _link_dir(small_root / "shots", outside)
    except (OSError, NotImplementedError, ImportError, AttributeError):
        pytest.skip("ссылку на каталог здесь не создать")
    _patch(small_root, screens={"images_dir": "shots"},
           logs={"через ссылку": "shots/canary.txt"})
    c = make_client(small_root)
    assert c.get("/screens/canary.txt").status_code == 404
    r = c.get("/api/log", params={"name": "через ссылку"})
    assert r.status_code == 400 and CANARY not in r.text
    assert "screens.images_dir: путь идёт через ссылку или вне корня — не отдаётся" \
        in _problems(small_root)


def test_demo_set_has_no_path_problems(demo_root):
    from veha import config

    assert config.load(demo_root).path_problems() == []
