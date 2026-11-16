"""HTTP страницы плагинов: никаких argv/путей из запроса, только объявленные settings."""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import make_client
from veha.viz.export import export_static


def root_with_plugins(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "plugin.py").write_text("print('{}')\n", encoding="utf-8")
    (tmp_path / "atlas.yaml").write_text("""lifecycle_plugins:
  optional:
    label: Optional hook
    command: [python, scripts/plugin.py]
    stages: [after_run]
    settings:
      mode: {type: string, default: safe}
      dry: {type: boolean, default: true}
  required:
    command: [python, scripts/plugin.py]
    stages: [validate, recovery]
    required: true
""", encoding="utf-8")
    return tmp_path


def test_get_shows_state_but_not_command_or_environment(tmp_path):
    c = make_client(root_with_plugins(tmp_path))
    r = c.get("/api/plugins")
    assert r.status_code == 200
    data = r.json()
    assert data["readonly"] is True and len(data["plugins"]) == 2
    assert data["plugins"][0]["settings"] == {"mode": "safe", "dry": True}
    assert '"command":' not in r.text and '"cwd":' not in r.text and '"env":' not in r.text


def test_readonly_and_required_plugin_cannot_be_changed(tmp_path):
    root = root_with_plugins(tmp_path)
    assert make_client(root).post("/api/plugins/optional/settings", json={
        "enabled": False, "settings": {}}).status_code == 409
    c = make_client(root, readonly=False)
    r = c.post("/api/plugins/required/settings", json={"enabled": False})
    assert r.status_code == 409 and "нельзя отключить" in r.json()["detail"]


def test_only_declared_scalar_settings_are_saved(tmp_path):
    c = make_client(root_with_plugins(tmp_path), readonly=False)
    r = c.post("/api/plugins/optional/settings", json={
        "enabled": False, "settings": {"mode": "next", "dry": False}})
    assert r.status_code == 200
    state = r.json()["plugins"][0]
    assert state["enabled"] is False and state["settings"] == {"mode": "next", "dry": False}
    bad = c.post("/api/plugins/optional/settings", json={
        "settings": {"command": ["anything"]}})
    assert bad.status_code == 409
    extra = c.post("/api/plugins/optional/settings", json={"argv": ["anything"]})
    assert extra.status_code == 422
    missing = c.post("/api/plugins/unknown/settings", json={})
    assert missing.status_code == 404


def test_static_export_contains_readonly_plugins(tmp_path):
    root = root_with_plugins(tmp_path / "root")
    out = tmp_path / "out"
    export_static(root, out)
    data = json.loads((out / "api" / "plugins.json").read_text(encoding="utf-8"))
    assert data["readonly"] is True and len(data["plugins"]) == 2
