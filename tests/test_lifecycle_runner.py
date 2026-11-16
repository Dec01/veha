"""Связь lifecycle с Runner: validate → pytest → after_run → durable final state."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from partest_atlas.artifacts import Scope

from veha import lifecycle
from veha import runner as runner_mod
from veha.config import load


class FakePytest:
    calls: ClassVar[list] = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = None
        self.stdout = io.StringIO(
            "collecting ... collected 1 item\n"
            "t.py::test_x PASSED [100%]\n"
            "============================== 1 passed in 0.01s ==============================\n")
        FakePytest.calls.append(args)

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_pytest(monkeypatch):
    FakePytest.calls = []
    real = subprocess.Popen

    def selective(args, **kwargs):
        if list(args[:3]) == [sys.executable, "-m", "pytest"]:
            return FakePytest(args, **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(runner_mod.subprocess, "Popen", selective)
    return FakePytest.calls


def config(tmp_path: Path, *, plugin_result="passed") -> object:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "atlas.yaml").write_text("""runner:
  suites:
    full:
      args: [tests]
      full: true
lifecycle_plugins:
  cleanup:
    command: [python, scripts/plugin.py]
    stages: [validate, after_run, recovery]
    required: true
    timeout: 5
""", encoding="utf-8")
    (tmp_path / "scripts" / "plugin.py").write_text(f"""import json, sys
from pathlib import Path
event = json.load(sys.stdin)
with Path('stages.jsonl').open('a', encoding='utf-8') as fh:
    fh.write(json.dumps({{'stage': event['stage'], 'run': event['run_id'],
                          'tests': event['tests_status']}}) + '\\n')
print(json.dumps({{'status': '{plugin_result}', 'summary': event['stage'], 'metrics': {{}}}}))
""", encoding="utf-8")
    return load(tmp_path)


def stages(root: Path):
    path = root / "stages.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_runner_runs_validate_then_pytest_then_after_run(tmp_path, fake_pytest):
    cfg = config(tmp_path)
    runner = runner_mod.Runner(cfg)
    st = runner.start("full", scope=Scope("shop", "test"))
    runner.wait(10)
    assert not runner.busy
    assert st.status == "done"
    assert [x["stage"] for x in stages(tmp_path)] == ["validate", "after_run"]
    assert stages(tmp_path)[1]["tests"] == "done"
    assert [x["stage"] for x in st.plugins] == ["validate", "after_run"]
    manifest = json.loads(lifecycle.manifest_path(cfg, st.id).read_text(encoding="utf-8"))
    assert manifest["state"] == "finished" and manifest["final_status"] == "done"
    assert len(fake_pytest) == 1


def test_required_validate_failure_means_no_pytest(tmp_path, fake_pytest):
    cfg = config(tmp_path, plugin_result="failed")
    runner = runner_mod.Runner(cfg)
    with pytest.raises(RuntimeError, match="pytest не запускался"):
        runner.start("full", scope=Scope("shop", "test"))
    assert fake_pytest == [] and not runner.busy
    assert [x["stage"] for x in stages(tmp_path)] == ["validate"]
    manifest = json.loads(lifecycle.manifest_path(cfg, runner.status()["run"]["id"])
                          .read_text(encoding="utf-8"))
    assert manifest["state"] == "validation_failed"


def test_required_after_run_failure_is_not_hidden_by_passed_pytest(tmp_path, fake_pytest):
    cfg = config(tmp_path)
    path = tmp_path / "scripts" / "plugin.py"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "'status': 'passed'", "'status': 'failed' if event['stage'] == 'after_run' else 'passed'"),
        encoding="utf-8")
    # Изменение до snapshot законно; validate и after_run получат один digest.
    runner = runner_mod.Runner(cfg)
    st = runner.start("full", scope=Scope("shop", "test"))
    runner.wait(10)
    assert st.returncode == 0 and st.status == "error"
    assert "required after_run" in st.note
    manifest = json.loads(lifecycle.manifest_path(cfg, st.id).read_text(encoding="utf-8"))
    assert manifest["state"] == "finished" and manifest["final_status"] == "error"


def test_os_lock_blocks_another_runner(tmp_path, fake_pytest):
    cfg = config(tmp_path)
    lock = lifecycle.FileLock(lifecycle.state_dir(cfg) / "runner.lock")
    assert lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="другим процессом"):
            runner_mod.Runner(cfg).start("full", scope=Scope("shop", "test"))
    finally:
        lock.release()
    assert fake_pytest == []


@pytest.mark.parametrize("interrupted_state", ["running", "finish_failed"])
def test_interrupted_manifest_is_recovered_before_next_pytest(
        tmp_path, fake_pytest, interrupted_state):
    cfg = config(tmp_path)
    selected = lifecycle.snapshots(cfg, "full")
    lifecycle.write_manifest(cfg, {"run_id": "d" * 12, "suite": "full",
                                   "state": interrupted_state,
                                   "scope": {"system": "shop", "env": "test"},
                                   "tests_status": "interrupted", "plugins": selected})
    runner = runner_mod.Runner(cfg)
    st = runner.start("full", scope=Scope("shop", "test"))
    runner.wait(10)
    assert st.status == "done"
    assert [x["stage"] for x in stages(tmp_path)] == ["recovery", "validate", "after_run"]
    old_manifest = json.loads(lifecycle.manifest_path(cfg, "d" * 12).read_text(encoding="utf-8"))
    assert old_manifest["state"] == "interrupted"


def test_no_lifecycle_section_preserves_runner_without_state_files(tmp_path, fake_pytest):
    (tmp_path / "atlas.yaml").write_text(
        "runner:\n  suites:\n    full: {args: [tests], full: true}\n", encoding="utf-8")
    runner = runner_mod.Runner(load(tmp_path))
    st = runner.start("full", scope=Scope("shop", "test"))
    runner.wait(10)
    assert st.status == "done" and not (tmp_path / ".veha").exists()
