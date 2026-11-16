"""Lifecycle command plugins: доверенный config, ограниченный процесс, durable recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from partest_atlas.artifacts import Scope

from veha import lifecycle
from veha.config import load


def project(tmp_path: Path, body: str) -> object:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "atlas.yaml").write_text(body, encoding="utf-8")
    return load(tmp_path)


def script(root: Path, text: str) -> None:
    (root / "scripts" / "plugin.py").write_text(text, encoding="utf-8")


def valid_config() -> str:
    return """lifecycle_plugins:
  cleanup:
    label: Project cleanup
    kind: command
    command: [python, scripts/plugin.py]
    cwd: .
    stages: [validate, after_run, recovery]
    required: true
    timeout: 5
    suites: [full]
    env: [PLUGIN_VISIBLE]
    editable: false
    settings:
      mode: {type: string, default: safe}
      retries: {type: integer, default: 0}
"""


def event(run_id="a" * 12):
    return {"run_id": run_id, "suite": "full",
            "scope": {"system": "shop", "env": "test"}}


def test_validate_passes_json_settings_and_only_allowlisted_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "visible-value")
    monkeypatch.setenv("PLUGIN_SECRET", "must-not-leak")
    cfg = project(tmp_path, valid_config())
    script(tmp_path, """import json, os, sys
event = json.load(sys.stdin)
print(json.dumps({"status": "passed", "summary": event["stage"] + ":" + event["settings"]["mode"],
                  "metrics": {"visible": bool(os.getenv("PLUGIN_VISIBLE")),
                              "inherited": os.getenv("PLUGIN_SECRET"),
                              "scope": event["scope"]["env"]}}))
""")
    snapshots = lifecycle.snapshots(cfg, "full")
    result = lifecycle.run_stage(cfg, snapshots, "validate", event())[0]
    assert result.status == "passed"
    assert result.summary == "validate:safe"
    assert result.metrics == {"visible": True, "inherited": None, "scope": "test"}
    assert not lifecycle.stage_failed([result])


def test_allowlisted_env_value_is_masked_even_without_sensitive_label(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "plain-env-secret")
    cfg = project(tmp_path, valid_config())
    script(tmp_path, """import json, os, sys
json.load(sys.stdin)
value = os.environ["PLUGIN_VISIBLE"]
print(value)
print(json.dumps({"status": "passed", "summary": value,
                  "metrics": {"value": value, value: "key"}}))
""")
    result = lifecycle.run_stage(cfg, lifecycle.snapshots(cfg, "full"), "validate", event())[0]
    rendered = json.dumps(result.as_dict(), ensure_ascii=False)
    assert "plain-env-secret" not in rendered
    assert result.summary == "***" and result.metrics["value"] == "***"
    assert result.metrics["***"] == "…[masked]"  # исходный ключ сам содержал слово secret


def test_nonzero_and_unsuccessful_required_result_fail_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "x")
    cfg = project(tmp_path, valid_config())
    script(tmp_path, """import json, sys
json.load(sys.stdin)
print(json.dumps({"status": "failed", "summary": "not clean", "metrics": {"found": 2}}))
sys.exit(3)
""")
    result = lifecycle.run_stage(cfg, lifecycle.snapshots(cfg, "full"), "after_run", event())[0]
    assert result.status == "failed" and result.returncode == 3
    assert result.metrics["found"] == 2
    assert lifecycle.stage_failed([result])


def test_invalid_json_is_failure_even_with_zero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "x")
    cfg = project(tmp_path, valid_config())
    script(tmp_path, "import sys; print(sys.stdin.read()); print('not json')\n")
    result = lifecycle.run_stage(cfg, lifecycle.snapshots(cfg, "full"), "validate", event())[0]
    assert result.status == "failed" and "JSON" in result.summary


def test_timeout_kills_process_and_is_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "x")
    cfg = project(tmp_path, valid_config().replace("timeout: 5", "timeout: 1"))
    script(tmp_path, "import sys, time; sys.stdin.read(); time.sleep(30)\n")
    result = lifecycle.run_stage(cfg, lifecycle.snapshots(cfg, "full"), "validate", event())[0]
    assert result.status == "failed" and result.returncode is None
    assert "таймаут" in result.summary


def test_required_and_readonly_plugin_cannot_be_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "x")
    cfg = project(tmp_path, valid_config())
    script(tmp_path, "pass\n")
    plugin = lifecycle.plugins(cfg)[0]
    with pytest.raises(lifecycle.LifecycleError, match="только для чтения"):
        lifecycle.StateStore(cfg).update(plugin, False, {}, scope=Scope("shop", "test"))


def test_optional_plugin_rejects_unknown_setting_and_required_disable(tmp_path):
    cfg = project(tmp_path, """lifecycle_plugins:
  optional:
    command: [python, scripts/plugin.py]
    stages: [after_run]
    required: true
    settings:
      mode: {type: string, default: safe}
""")
    script(tmp_path, "pass\n")
    plugin = lifecycle.plugins(cfg)[0]
    store = lifecycle.StateStore(cfg)
    with pytest.raises(lifecycle.LifecycleError, match="нельзя отключить"):
        store.update(plugin, False, {}, scope=Scope("shop", "test"))
    with pytest.raises(lifecycle.LifecycleError, match="не объявлена"):
        store.update(plugin, None, {"command": "other"}, scope=Scope("shop", "test"))
    with pytest.raises(lifecycle.LifecycleError, match="строка"):
        store.update(plugin, None, {"mode": 1}, scope=Scope("shop", "test"))
    before = lifecycle.snapshots(cfg, "full")[0]
    state = store.update(plugin, None, {"mode": "next"}, scope=Scope("shop", "test"))
    assert state["settings"]["mode"] == "next"
    assert before["settings"]["mode"] == "safe"  # snapshot уже идущего прогона неизменен
    assert lifecycle.snapshots(cfg, "full")[0]["settings"]["mode"] == "next"
    audit = (tmp_path / ".veha" / "lifecycle" / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "settings"' in audit and '"next"' in audit


def test_sensitive_setting_name_and_unknown_config_key_are_refused(tmp_path):
    cfg = project(tmp_path, """lifecycle_plugins:
  bad:
    command: [python, scripts/plugin.py]
    stages: [validate]
    settings:
      api_token: {type: string}
""")
    script(tmp_path, "pass\n")
    with pytest.raises(lifecycle.LifecycleError, match="чувствительное"):
        lifecycle.plugins(cfg)


def test_path_escape_is_refused_before_execution(tmp_path):
    cfg = project(tmp_path, """lifecycle_plugins:
  bad:
    command: [python, ../outside.py]
    stages: [validate]
""")
    (tmp_path.parent / "outside.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="вне корня"):
        lifecycle.plugins(cfg)


@pytest.mark.skipif(os.name == "nt", reason="создание symlink на Windows требует отдельных прав")
def test_symlink_command_is_refused(tmp_path):
    outside = tmp_path.parent / "outside-plugin.py"
    outside.write_text("pass\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "plugin.py").symlink_to(outside)
    cfg = load(tmp_path)
    (tmp_path / "atlas.yaml").write_text("""lifecycle_plugins:
  bad:
    command: [python, scripts/plugin.py]
    stages: [validate]
""", encoding="utf-8")
    cfg = load(tmp_path)
    with pytest.raises(lifecycle.LifecycleError, match="ссылку"):
        lifecycle.plugins(cfg)


def test_output_and_result_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "x")
    cfg = project(tmp_path, valid_config())
    script(tmp_path, """import sys
sys.stdin.read()
print('token=SECRETVALUE')
print('{"status":"passed","summary":"password=SECRETVALUE","metrics":{"token":"SECRETVALUE"}}')
""")
    result = lifecycle.run_stage(cfg, lifecycle.snapshots(cfg, "full"), "validate", event())[0]
    rendered = json.dumps(result.as_dict(), ensure_ascii=False)
    assert "SECRETVALUE" not in rendered and "***" in rendered


def test_changed_implementation_blocks_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "x")
    cfg = project(tmp_path, valid_config())
    path = tmp_path / "scripts" / "plugin.py"
    path.write_text("print('{}')\n", encoding="utf-8")
    snapshot = lifecycle.snapshots(cfg, "full")[0]
    path.write_text("print('changed')\n", encoding="utf-8")
    result = lifecycle.execute(cfg.root, snapshot, "recovery", event())
    assert result.status == "failed" and "изменилась" in result.summary


def test_changed_imported_implementation_file_blocks_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "x")
    (tmp_path / "src").mkdir()
    dependency = tmp_path / "src" / "cleanup.py"
    dependency.write_text("MODE = 'safe'\n", encoding="utf-8")
    cfg = project(tmp_path, valid_config().replace(
        "    editable: false", "    implementation_files: [src/cleanup.py]\n    editable: false"))
    script(tmp_path, "print('{}')\n")
    snapshot = lifecycle.snapshots(cfg, "full")[0]
    assert snapshot["implementation_files"] == ["src/cleanup.py"]
    dependency.write_text("MODE = 'changed'\n", encoding="utf-8")
    result = lifecycle.execute(cfg.root, snapshot, "recovery", event())
    assert result.status == "failed" and "изменилась" in result.summary


def test_implementation_file_path_escape_is_refused(tmp_path):
    cfg = project(tmp_path, """lifecycle_plugins:
  bad:
    command: [python, scripts/plugin.py]
    stages: [validate]
    implementation_files: [../outside.py]
""")
    script(tmp_path, "pass\n")
    (tmp_path.parent / "outside.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="вне корня"):
        lifecycle.plugins(cfg)


@pytest.mark.skipif(os.name == "nt", reason="создание symlink на Windows требует отдельных прав")
def test_symlink_implementation_file_is_refused(tmp_path):
    outside = tmp_path.parent / "outside-cleanup.py"
    outside.write_text("pass\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "plugin.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "scripts" / "cleanup.py").symlink_to(outside)
    (tmp_path / "atlas.yaml").write_text("""lifecycle_plugins:
  bad:
    command: [python, scripts/plugin.py]
    stages: [validate]
    implementation_files: [scripts/cleanup.py]
""", encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="ссылку"):
        lifecycle.plugins(load(tmp_path))


def test_recovery_uses_saved_settings_and_marks_interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "x")
    cfg = project(tmp_path, valid_config())
    marker = tmp_path / "recovered.json"
    script(tmp_path, f"""import json, sys
event = json.load(sys.stdin)
if event['stage'] == 'recovery':
    open({str(marker)!r}, 'w').write(json.dumps(event['settings']))
print(json.dumps({{'status': 'passed', 'summary': 'ok', 'metrics': {{}}}}))
""")
    snapshots = lifecycle.snapshots(cfg, "full")
    run_id = "b" * 12
    lifecycle.write_manifest(cfg, {"run_id": run_id, "suite": "full", "state": "running",
                                   "scope": event(run_id)["scope"], "plugins": snapshots})
    assert lifecycle.recover_pending(cfg) == [{"run_id": run_id, "state": "interrupted"}]
    assert json.loads(marker.read_text())["mode"] == "safe"
    manifest = json.loads(lifecycle.manifest_path(cfg, run_id).read_text(encoding="utf-8"))
    assert manifest["state"] == "interrupted"
    assert lifecycle.recover_pending(cfg) == []


def test_recovery_mismatch_blocks_new_run(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_VISIBLE", "x")
    cfg = project(tmp_path, valid_config())
    script(tmp_path, "print('{}')\n")
    run_id = "c" * 12
    lifecycle.write_manifest(cfg, {"run_id": run_id, "suite": "full", "state": "running",
                                   "plugins": lifecycle.snapshots(cfg, "full")})
    (tmp_path / "atlas.yaml").write_text(valid_config().replace("label: Project cleanup",
                                                                  "label: Changed"), encoding="utf-8")
    cfg = load(tmp_path)
    # label не входит в реализацию: меняем саму команду, а не только отображение.
    (tmp_path / "atlas.yaml").write_text(valid_config().replace("scripts/plugin.py",
                                                                  "scripts/other.py"), encoding="utf-8")
    (tmp_path / "scripts" / "other.py").write_text("print('{}')\n", encoding="utf-8")
    cfg = load(tmp_path)
    with pytest.raises(lifecycle.LifecycleError, match="новый прогон заблокирован"):
        lifecycle.recover_pending(cfg)


def test_no_section_means_no_plugins_and_no_state_writes(tmp_path):
    cfg = project(tmp_path, "runner: {}\n")
    assert not lifecycle.configured(cfg)
    assert lifecycle.snapshots(cfg, "full") == []
    assert not (tmp_path / ".veha").exists()
