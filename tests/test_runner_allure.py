"""Интеграция Runner с Allure и изоляция внешних opt-in переменных."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import ClassVar

import pytest
from partest_atlas.artifacts import Scope

from veha import allure_store, lifecycle
from veha import runner as runner_mod
from veha.config import Config


class FakePytest:
    calls: ClassVar[list] = []
    returncode: ClassVar[int] = 0
    write_results: ClassVar[bool] = True

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = None
        FakePytest.calls.append({"args": list(args), **kwargs})
        if "--alluredir" in args and FakePytest.write_results:
            raw = Path(kwargs["cwd"]) / args[args.index("--alluredir") + 1]
            raw.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}-result.json"
            (raw / name).write_text(json.dumps({"name": name, "status": "passed"}),
                                    encoding="utf-8")
        if FakePytest.returncode == 1:
            output = ("collected 1 item\nt.py::test_x FAILED [100%]\n"
                      "============================== 1 failed in 0.01s ==============================\n")
        elif FakePytest.returncode == 5:
            output = "============================== no tests ran in 0.01s ==============================\n"
        else:
            output = ("collected 1 item\nt.py::test_x PASSED [100%]\n"
                      "============================== 1 passed in 0.01s ==============================\n")
        self.stdout = io.StringIO(output)

    def poll(self):
        return FakePytest.returncode

    def wait(self, timeout=None):
        return FakePytest.returncode


@pytest.fixture
def fake_pytest(monkeypatch):
    FakePytest.calls = []
    FakePytest.returncode = 0
    FakePytest.write_results = True
    real = subprocess.Popen

    def selective(args, **kwargs):
        if list(args[:3]) == [sys.executable, "-m", "pytest"]:
            return FakePytest(args, **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(runner_mod.subprocess, "Popen", selective)
    yield FakePytest.calls
    FakePytest.returncode = 0
    FakePytest.write_results = True


@pytest.fixture(autouse=True)
def _portable_latest(tmp_path, monkeypatch):
    """Тот же адаптер, что в test_allure_store, если Windows не даёт создать symlink."""
    probe = tmp_path / "symlink-probe"
    try:
        os.symlink(Path("runs") / "probe", probe, target_is_directory=True)
    except (OSError, NotImplementedError):
        actual_is_link = allure_store._is_link

        def is_link(path: Path) -> bool:
            return actual_is_link(path) or (
                path.is_file() and (path.name == "latest" or path.name.startswith(".latest-")))

        monkeypatch.setattr(allure_store, "_is_link", is_link)
        monkeypatch.setattr(allure_store, "_read_link",
                            lambda path: path.read_text(encoding="utf-8"))
        monkeypatch.setattr(allure_store, "_create_link",
                            lambda target, path: path.write_text(os.fspath(target), encoding="utf-8"))
    else:
        probe.unlink()


def _config(root: Path, *, allure=None, env=None, suite_env=None, lifecycle_plugin=False) -> Config:
    suite = {"args": ["tests"], "full": True}
    if suite_env is not None:
        suite["env"] = suite_env
    runner = {"suites": {"full": suite, "other": {"args": ["other"]}}}
    if env is not None:
        runner["env"] = env
    if allure is not None:
        runner["allure"] = allure
    raw = {"runner": runner}
    if lifecycle_plugin:
        raw["lifecycle_plugins"] = {
            "cleanup": {"command": ["python", "scripts/plugin.py"],
                        "stages": ["validate", "after_run"], "required": True, "timeout": 5}}
    return Config(root=root, raw=raw)


def _allure(command=None, **extra):
    if command is None:
        command = [sys.executable, "scripts/fake_allure.py"]
    return {"store": ".veha/allure", "keep": 2,
            "command": command, "timeout": 5, **extra}


def _generator(root: Path, body: str = "") -> None:
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "fake_allure.py").write_text("""import json, sys
from pathlib import Path
raw, report = Path(sys.argv[1]), Path(sys.argv[3])
with Path('generator-inputs.jsonl').open('a', encoding='utf-8') as fh:
    fh.write(json.dumps({'files': sorted(p.name for p in raw.iterdir()),
                         'history': (raw / 'history' / 'marker.txt').is_file()}) + '\\n')
(report / 'index.html').write_text('ok', encoding='utf-8')
(report / 'history').mkdir(exist_ok=True)
(report / 'history' / 'marker.txt').write_text('history', encoding='utf-8')
""" + body, encoding="utf-8")


def _run(root: Path, cfg: Config, suite: str = "full"):
    runner = runner_mod.Runner(cfg)
    st = runner.start(suite, scope=Scope("shop", "test"))
    runner.wait(10)
    assert not runner.busy
    return runner, st


def _scope(root: Path, suite: str = "full") -> Path:
    return root / ".veha" / "allure" / "shop" / "test" / suite


def _state(root: Path, suite: str = "full") -> dict:
    return json.loads((_scope(root, suite) / "state.json").read_text(encoding="utf-8"))


def _inputs(root: Path) -> list:
    path = root / "generator-inputs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_no_allure_section_preserves_old_runner(tmp_path, fake_pytest):
    runner, st = _run(tmp_path, _config(tmp_path))
    assert st.status == "done" and st.report is None
    assert "--alluredir" not in fake_pytest[0]["args"]
    assert "report" not in runner.status()["run"]
    assert not (tmp_path / ".veha").exists()


def test_env_unset_set_suite_override_and_scope_win(tmp_path, fake_pytest, monkeypatch):
    monkeypatch.setenv("PROJECT_OPTIN", "outside")
    monkeypatch.setenv("PROJECT_MODE", "outside")
    monkeypatch.setenv("PROJECT_SECRET", "SECRET-VALUE")
    cfg = _config(tmp_path, env={"PROJECT_OPTIN": None, "PROJECT_MODE": "global",
                                 "ATLAS_SYSTEM": "wrong"},
                  suite_env={"PROJECT_MODE": "suite", "ATLAS_ENV": "wrong"})
    runner, _ = _run(tmp_path, cfg)
    env = fake_pytest[0]["env"]
    assert "PROJECT_OPTIN" not in env and env["PROJECT_MODE"] == "suite"
    assert (env["ATLAS_SYSTEM"], env["ATLAS_ENV"]) == ("shop", "test")
    serialized = json.dumps(runner.status(), ensure_ascii=False)
    assert "SECRET-VALUE" not in serialized and "PROJECT_SECRET" not in serialized
    assert "PROJECT_OPTIN" not in serialized and str(tmp_path) not in serialized


@pytest.mark.parametrize("env", [[], {"BAD=NAME": None}, {"FLAG": 1}, {"FLAG": "a\x00b"}])
def test_invalid_env_is_rejected_before_pytest(tmp_path, fake_pytest, env):
    with pytest.raises(ValueError):
        _run(tmp_path, _config(tmp_path, env=env))
    assert fake_pytest == []


def test_fresh_raw_history_and_two_snapshots_per_suite(tmp_path, fake_pytest):
    _generator(tmp_path)
    raw = tmp_path / "allure-results"
    raw.mkdir()
    old = "00000000-0000-0000-0000-000000000000-result.json"
    (raw / old).write_text("old", encoding="utf-8")
    (raw / "categories.json").write_text("[]", encoding="utf-8")
    (raw / "project.txt").write_text("keep", encoding="utf-8")
    runner = runner_mod.Runner(_config(tmp_path, allure=_allure()))
    states = []
    for _ in range(3):
        st = runner.start("full", scope=Scope("shop", "test"))
        runner.wait(10)
        assert st.status == "done" and st.report["status"] == "published"
        states.append(st.report["snapshot"])

    state = _state(tmp_path)
    assert state["runs"] == states[1:] and state["latest"] == states[-1]
    assert sorted(p.name for p in (_scope(tmp_path) / "runs").iterdir()) == sorted(states[1:])
    assert sorted(p.name for p in (_scope(tmp_path) / "raw").iterdir()) == sorted(states[1:])
    inputs = _inputs(tmp_path)
    assert all(sum(name.endswith("-result.json") for name in item["files"]) == 1
               for item in inputs)
    assert old not in inputs[0]["files"]
    assert [item["history"] for item in inputs] == [False, True, True]
    assert (raw / "categories.json").is_file() and (raw / "project.txt").is_file()
    assert "--alluredir" in fake_pytest[0]["args"]
    assert not Path(fake_pytest[0]["args"][-1]).is_absolute()


def test_interleaved_suites_keep_independent_publications(tmp_path, fake_pytest):
    _generator(tmp_path)
    runner = runner_mod.Runner(_config(tmp_path, allure=_allure()))
    for suite in ("full", "other", "full"):
        st = runner.start(suite, scope=Scope("shop", "test"))
        runner.wait(10)
        assert st.report["status"] == "published"
    assert len(_state(tmp_path, "full")["runs"]) == 2
    assert len(_state(tmp_path, "other")["runs"]) == 1


@pytest.mark.parametrize("code,write,expected", [(1, True, "published"), (5, False, "no_results")])
def test_failed_pytest_and_empty_results_have_separate_report_state(
        tmp_path, fake_pytest, code, write, expected):
    _generator(tmp_path)
    FakePytest.returncode = code
    FakePytest.write_results = write
    _, st = _run(tmp_path, _config(tmp_path, allure=_allure()))
    assert st.returncode == code and st.report["status"] == expected
    assert (tmp_path / "generator-inputs.jsonl").exists() is write


@pytest.mark.parametrize("body,timeout", [("raise SystemExit(7)\n", 5),
                                           ("import time; time.sleep(5)\n", 0.05)])
def test_generator_failure_and_timeout_do_not_change_pytest_result(
        tmp_path, fake_pytest, body, timeout):
    _generator(tmp_path, body)
    _, st = _run(tmp_path, _config(tmp_path, allure=_allure(timeout=timeout)))
    assert st.returncode == 0 and st.status == "done"
    assert st.report["status"] == "failed" and st.report["error"]
    assert str(tmp_path) not in st.report["error"]


def test_required_after_run_sees_failed_report_and_still_runs(tmp_path, fake_pytest):
    _generator(tmp_path, "raise SystemExit(9)\n")
    (tmp_path / "scripts" / "plugin.py").write_text("""import json, sys
from pathlib import Path
event = json.load(sys.stdin)
with Path('stages.jsonl').open('a', encoding='utf-8') as fh:
    fh.write(json.dumps({'stage': event['stage'],
                         'report': event.get('report_status')}) + '\\n')
print(json.dumps({'status': 'passed', 'summary': event['stage'], 'metrics': {}}))
""", encoding="utf-8")
    cfg = _config(tmp_path, allure=_allure(), lifecycle_plugin=True)
    runner, st = _run(tmp_path, cfg)
    events = [json.loads(line) for line in (tmp_path / "stages.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [event["stage"] for event in events] == ["validate", "after_run"]
    assert events[-1]["report"] == "failed"
    assert st.returncode == 0 and st.status == "done" and st.report["status"] == "failed"
    manifest = json.loads(lifecycle.manifest_path(cfg, st.id).read_text(encoding="utf-8"))
    assert manifest["report_status"] == "failed" and manifest["state"] == "finished"
    assert runner.status()["run"]["report"]["status"] == "failed"


def test_rotation_failure_does_not_roll_back_new_latest(tmp_path, fake_pytest, monkeypatch):
    _generator(tmp_path)
    runner = runner_mod.Runner(_config(tmp_path, allure=_allure()))
    first = runner.start("full", scope=Scope("shop", "test"))
    runner.wait(10)

    def fail_prune(*args, **kwargs):
        raise OSError("диск занят")

    monkeypatch.setattr(allure_store, "_prune", fail_prune)
    second = runner.start("full", scope=Scope("shop", "test"))
    runner.wait(10)
    assert second.returncode == 0 and second.report["status"] == "rotation_failed"
    state = _state(tmp_path)
    assert state["latest"] == second.report["snapshot"]
    assert first.report["snapshot"] != second.report["snapshot"]
    assert (_scope(tmp_path) / "runs" / second.report["snapshot"] / "index.html").is_file()


@pytest.mark.parametrize("allure", [
    {"keep": 0}, {"keep": True}, {"command": "allure generate"}, {"command": []},
    {"timeout": 0}, {"timeout": True}, {"unknown": 1}, {"store": "."},
    {"store": "allure-results/store"},
])
def test_invalid_allure_config_never_starts_pytest(tmp_path, fake_pytest, allure):
    settings = _allure(**allure)
    with pytest.raises(ValueError):
        _run(tmp_path, _config(tmp_path, allure=settings))
    assert fake_pytest == []


def test_allure_without_lifecycle_still_uses_os_lock(tmp_path, fake_pytest):
    cfg = _config(tmp_path, allure=_allure())
    lock = lifecycle.FileLock(lifecycle.state_dir(cfg) / "runner.lock")
    assert lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="другим процессом"):
            runner_mod.Runner(cfg).start("full", scope=Scope("shop", "test"))
    finally:
        lock.release()
    assert fake_pytest == []
