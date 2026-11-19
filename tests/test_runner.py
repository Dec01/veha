"""Раннер и перезапуск: область доходит до подпроцесса, без области ничего не пишется,
история не затирается, структура переживает перезапуск одного кейса."""

from __future__ import annotations

import io
import json
import os
import shutil
import time
from collections import Counter

import pytest
from pathlib import Path
from partest_atlas.artifacts import Scope

from tests.conftest import artifact_path, make_client, rewrite


class FakeProc:
    """Подпроцесс pytest, который сразу закончился: проверяем, с чем его позвали."""

    calls: list = []
    #: отчёт, который «прогон» перезаписывает (`_fresh_coverage`); `None` — не пишет
    rewrite = None

    def __init__(self, args, **kwargs):
        FakeProc.calls.append({"args": args, **kwargs})
        if FakeProc.rewrite is not None:
            # как настоящий прогон: новый файл на месте старого (os.replace меняет inode),
            # свежесть раннер узнаёт по отпечатку файла, а не по часам
            target = FakeProc.rewrite
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(target.read_bytes())
            os.replace(tmp, target)
        self.stdout = io.StringIO(
            "collecting ... collected 1 item\n"
            "t.py::test_x PASSED                                       [100%]\n"
            "============================== 1 passed in 0.01s ==============================\n")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_popen(monkeypatch):
    from veha import runner

    FakeProc.calls = []
    FakeProc.rewrite = None
    monkeypatch.setattr(runner.subprocess, "Popen", FakeProc)
    yield FakeProc.calls
    FakeProc.rewrite = None


@pytest.fixture(autouse=True)
def _no_leftover_rewrite():
    """`FakeProc.rewrite` — атрибут класса: следующий тест не должен переписывать отчёт
    в чужом, уже удалённом корне."""
    yield
    FakeProc.rewrite = None


def _wait_for_history(path, before=None, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if path.is_file() and path.read_bytes() != before:
                return
        except (FileNotFoundError, PermissionError):
            pass
        time.sleep(0.05)


@pytest.mark.parametrize("error_cls", [PermissionError, FileNotFoundError])
def test_wait_for_history_ignores_transient_read_errors(tmp_path, monkeypatch, error_cls):
    path = tmp_path / "runs.json"
    path.write_bytes(b"before")
    attempts = 0

    def read_bytes(self):
        nonlocal attempts
        assert self == path
        attempts += 1
        if attempts == 1:
            raise error_cls()
        return b"after"

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    _wait_for_history(path, b"before", timeout=1.0)
    assert attempts == 2


def _two_scopes_without_default(root):
    base = root / "atlas"
    shutil.move(str(base / "default" / "default"), str(base / "shop" / "test"))
    shutil.rmtree(base / "default")
    shutil.copytree(base / "shop" / "test", base / "shop" / "preprod")


# ------------------------------------------------------------------


def test_run_gets_the_shown_scope(small_root, fake_popen):
    c = make_client(small_root, readonly=False)
    response = c.post("/api/runner/start", json={"suite": "smoke"})
    assert response.status_code == 200
    env = fake_popen[0]["env"]
    assert (env["ATLAS_SYSTEM"], env["ATLAS_ENV"]) == ("default", "default")
    assert env["ATLAS_RUN_ID"] == response.json()["run"]["id"]
    args = fake_popen[0]["args"]
    assert "-p" in args and args[args.index("-p") + 1] == "no:cacheprovider"


def test_run_gets_the_explicit_scope(small_root, fake_popen):
    c = make_client(small_root, readonly=False, scope=Scope("shop", "preprod"))
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200
    env = fake_popen[0]["env"]
    assert (env["ATLAS_SYSTEM"], env["ATLAS_ENV"]) == ("shop", "preprod")
    hist = artifact_path(small_root, "runner.history", "shop", "preprod")
    _wait_for_history(hist)
    assert json.loads(hist.read_text(encoding="utf-8"))["scope"] == {
        "system": "shop", "env": "preprod"}


def test_rerun_gets_the_scope(small_root, monkeypatch):
    from veha.viz import app as app_module

    seen = {}

    class FakeRerun:
        pid, returncode = None, 0

        def __init__(self, args, **kwargs):
            seen.update(kwargs)
            self.stdout = io.StringIO("1 passed token=SECRETVALUE\n")

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(app_module.subprocess, "Popen", FakeRerun)
    (small_root / "t_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    c = make_client(small_root, readonly=False, scope=Scope("shop", "test"))
    r = c.post("/api/rerun", json={"nodeid": "t_x.py::test_x"})
    assert r.status_code == 200 and "SECRETVALUE" not in r.json()["output"]
    assert (seen["env"]["ATLAS_SYSTEM"], seen["env"]["ATLAS_ENV"]) == ("shop", "test")


# ------------------------------------------------------------------


def test_no_scope_chosen_means_no_run_and_no_phantom_default(small_root, fake_popen):
    _two_scopes_without_default(small_root)
    (small_root / "t_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    c = make_client(small_root, readonly=False)
    assert c.get("/api/artifacts").json()["scopeChosen"] is False
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 409
    assert c.post("/api/rerun", json={"nodeid": "t_x.py::test_x"}).status_code == 409
    assert fake_popen == []
    assert not (small_root / "atlas" / "default").exists()


# ------------------------------------------------------------------


def test_history_not_understood_at_write_time_is_kept(small_root, fake_popen):
    c = make_client(small_root, readonly=False)
    assert c.get("/api/runner/suites").json()["historyState"] == "ok"   # граф в кэше
    hist = artifact_path(small_root, "runner.history")
    rewrite(hist, version=99)                                        # файл сменился
    before = hist.read_bytes()
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200
    time.sleep(0.5)
    assert hist.read_bytes() == before


def test_history_is_appended_when_understood(small_root, fake_popen):
    c = make_client(small_root, readonly=False)
    hist = artifact_path(small_root, "runner.history")
    before = hist.read_bytes()
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200
    _wait_for_history(hist, before)
    doc = json.loads(hist.read_text(encoding="utf-8"))
    assert len(doc["runs"]) == 8 and doc["collector"] == "veha/runner"
    assert doc["runs"][-1]["suite"] == "smoke"


# -------------------------------------------------------------


@pytest.fixture
def full_copy(tmp_path, demo_root):
    root = tmp_path / "full"
    shutil.copytree(demo_root, root, ignore=shutil.ignore_patterns("screens", "logs"))
    return root


STRUCTURE_KINDS = ("writes", "reads", "publishes", "consumes")


def _structure(g):
    kinds = Counter(e["kind"] for e in g["edges"])
    return {k: kinds[k] for k in STRUCTURE_KINDS}


def test_rerun_of_one_case_keeps_the_structure(full_copy, demo_graph):
    """Перегнали один кейс через час — связи полного прогона остаются на сцене."""
    from veha.core.graph import build_root

    d = full_copy / "allure-results"
    src = next(f for f in sorted(d.glob("*-result.json"))
               if json.loads(f.read_text(encoding="utf-8")).get("status") == "failed")
    doc = json.loads(src.read_text(encoding="utf-8"))
    doc["start"] += 3_600_000
    doc["stop"] += 3_600_000
    doc["status"] = "passed"
    (d / "zz-rerun-result.json").write_text(json.dumps(doc), encoding="utf-8")

    g = build_root(full_copy)
    assert g["run"]["total"] == 1 and g["run"]["runsFound"] == 2
    assert _structure(g) == _structure(demo_graph)
    assert _structure(g)["writes"] == 262


def test_parametrized_nodeids_join_with_allure(full_copy, demo_graph):
    """Агент пишет `…::test[p]`, Allure — без параметров. Связи обязаны сойтись."""
    from veha.core.graph import build_root

    for kind in ("db.effects", "bus.flow"):
        path = artifact_path(full_copy, kind)
        doc = json.loads(path.read_text(encoding="utf-8"))
        for r in doc["records"]:
            if r.get("nodeid") and "[" not in r["nodeid"]:
                r["nodeid"] += "[p-1]"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    assert _structure(build_root(full_copy)) == _structure(demo_graph)


def test_demo_set_has_a_parametrized_effect(demo_root):
    doc = json.loads(artifact_path(demo_root, "db.effects").read_text(encoding="utf-8"))
    assert any("[" in (r.get("nodeid") or "") for r in doc["records"])


# ------------------------------------------------------------------


def test_partial_run_is_marked_and_kept_out_of_the_average(small_root):
    from veha import runner
    from veha.core.graph import build_root

    cov = small_root / "coverage.json"
    doc = json.loads(cov.read_text(encoding="utf-8"))
    doc["meta"]["partialRun"] = True
    doc["meta"]["selection"] = {"markexpr": "smoke"}
    cov.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    meta = build_root(small_root)["meta"]
    assert meta["partialRun"] is True and meta["selection"] == {"markexpr": "smoke"}
    snap = runner.coverage_snapshot(small_root / "coverage.json")
    assert "avg" not in snap and snap["partialRun"] is True
    assert snap["selection"] == {"markexpr": "smoke"}


def test_full_run_snapshot_keeps_the_average(demo_root):
    from veha import runner

    snap = runner.coverage_snapshot(demo_root / "coverage.json")
    assert snap["avg"] == 66.05 and "partialRun" not in snap


# ------------------------------------------------------------------


class BusyProc(FakeProc):
    """Прогон, который ещё идёт."""

    def poll(self):
        return None


def test_rerun_waits_for_a_running_suite(small_root, monkeypatch):
    from veha import runner

    monkeypatch.setattr(runner.subprocess, "Popen", BusyProc)
    (small_root / "t_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    c = make_client(small_root, readonly=False)
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200
    r = c.post("/api/rerun", json={"nodeid": "t_x.py::test_x"})
    assert r.status_code == 409 and "прогон набора" in r.json()["detail"]


def test_suite_waits_for_a_running_rerun(small_root, fake_popen):
    c = make_client(small_root, readonly=False)
    srv = c.app.state.veha
    assert srv.rerun_lock.acquire(blocking=False)       # кейс перезапускается
    try:
        r = c.post("/api/runner/start", json={"suite": "smoke"})
        assert r.status_code == 409 and "перезапуск" in r.json()["detail"]
        assert c.post("/api/runner/clean").status_code == 409
    finally:
        srv.rerun_lock.release()
    assert fake_popen == []


# ------------------------------------------------------------------


def test_rerun_of_one_case_keeps_requirement_links(full_copy, demo_graph):
    from veha.core.graph import build_root

    d = full_copy / "allure-results"
    src = next(f for f in sorted(d.glob("*-result.json")))
    doc = json.loads(src.read_text(encoding="utf-8"))
    doc["start"] += 3_600_000
    doc["stop"] += 3_600_000
    (d / "zz-rerun-result.json").write_text(json.dumps(doc), encoding="utf-8")
    g = build_root(full_copy)
    assert g["run"]["total"] == 1
    assert g["meta"]["requirementsCovered"] == demo_graph["meta"]["requirementsCovered"] == 24


# ------------------------------------------------------------------


@pytest.mark.parametrize("meta,reasons", [
    ({}, []),
    # поля без `partialRun` — не причина: решает partest, не сервер
    ({"selection": {"markexpr": "smoke"}}, []),
    ({"workers": 4, "merged": False}, []),
    ({"unseenRatio": 0.9}, []),
    ({"partialRun": True, "selection": {"markexpr": "smoke"}}, ["выбор -m/-k"]),
    ({"partialRun": True, "selection": {"keyword": "orders"}}, ["выбор -m/-k"]),
    ({"partialRun": True, "workers": 4, "merged": False}, ["воркеры не слиты"]),
    ({"partialRun": True, "workers": 4, "merged": True, "unseenRatio": 0.31},
     ["много непосещённых (unseenRatio 0.31)"]),
    ({"partialRun": True, "unseenRatio": None}, ["много непосещённых"]),
])
def test_partial_reasons_from_partest_meta(meta, reasons):
    from veha.core.sources import partial_reasons

    assert partial_reasons(meta) == reasons
    assert partial_reasons(meta, suite_full=False) == ["выбор набора", *reasons]
    assert partial_reasons(meta, suite_full=True) == reasons


def test_server_has_no_threshold_of_its_own():
    """Порог «много непосещённых» — у partest. Копия у сервера разошлась бы молча."""
    from veha.core import sources

    assert not hasattr(sources, "UNSEEN_PARTIAL")
    text = Path(sources.__file__).read_text(encoding="utf-8")
    assert "0.2" not in text


def test_suite_not_declared_full_is_partial(demo_root):
    from veha import runner

    assert runner.coverage_snapshot(demo_root / "coverage.json", suite_full=True)["avg"] == 66.05
    snap = runner.coverage_snapshot(demo_root / "coverage.json", suite_full=False)
    assert "avg" not in snap and snap["partialReasons"] == ["выбор набора"]


def test_trend_is_built_from_full_runs_only(client):
    r = client.get("/api/runner/suites").json()
    full = [s["key"] for s in r["suites"] if s["full"]]
    assert full == ["full"]
    assert len(r["history"]) == 7
    # оба полных прогона — в тренде, но в разных сериях: ночной шёл с RUN_SLOW
    assert [t["suite"] for t in r["trend"]] == ["full", "full"]
    assert r["trend"][-1]["avg"] == 66.05
    assert [len(s["points"]) for s in r["trendSeries"]] == [1, 1]


def _fresh_coverage(root):
    """coverage.json, записанный «этим прогоном»: поддельный pytest перезапишет его."""
    FakeProc.rewrite = root / "coverage.json"


def test_run_records_whether_its_suite_is_full(small_root, fake_popen):
    _fresh_coverage(small_root)
    c = make_client(small_root, readonly=False)
    hist = artifact_path(small_root, "runner.history")
    before = hist.read_bytes()
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200
    _wait_for_history(hist, before)
    last = json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]
    assert last["suite"] == "smoke" and last["suiteFull"] is False
    assert last["coverage"]["partialReasons"] == ["выбор набора"]


# ------------------------------------------------------------------


def test_trend_skips_partial_runs_even_with_an_average():
    """Запись с `avg` и `partialRun: true` в тренд не попадает — не по отсутствию avg,
    а по признаку частичности."""
    from veha import runner

    runs = [
        {"startedAt": 1, "suite": "full", "coverage": {"avg": 60.0}},
        {"startedAt": 2, "suite": "smoke", "coverage": {"avg": 99.0, "partialRun": True}},
        {"startedAt": 3, "suite": "full", "coverage": {"note": "покрытие этим прогоном не записано"}},
        {"startedAt": 4, "suite": "full", "coverage": {"avg": 61.5}},
    ]
    assert [t["avg"] for t in runner.trend(runs)] == [60.0, 61.5]


def test_unfinished_or_stale_coverage_is_not_recorded(small_root, monkeypatch):
    from veha import runner

    class Interrupted(FakeProc):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            self.stdout = io.StringIO("collected 5 items\nt.py::test_x PASSED [ 20%]\n")

    monkeypatch.setattr(runner.subprocess, "Popen", Interrupted)
    _fresh_coverage(small_root)
    c = make_client(small_root, readonly=False)
    hist = artifact_path(small_root, "runner.history")
    before = hist.read_bytes()
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200
    _wait_for_history(hist, before)
    last = json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]
    assert last["done"] == 1 and last["total"] == 5
    assert last["coverage"] == {"note": "покрытие этим прогоном не записано"}


def test_stale_coverage_file_is_not_this_runs(small_root, fake_popen):
    import os

    old = time.time() - 3600
    os.utime(small_root / "coverage.json", (old, old))
    c = make_client(small_root, readonly=False)
    hist = artifact_path(small_root, "runner.history")
    before = hist.read_bytes()
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200
    _wait_for_history(hist, before)
    last = json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]
    assert last["coverage"] == {"note": "покрытие этим прогоном не записано"}


def test_startup_warns_when_no_suite_is_full(small_root, monkeypatch, capsys):
    import uvicorn

    from veha import cli

    text = (small_root / "atlas.yaml").read_text(encoding="utf-8")
    (small_root / "atlas.yaml").write_text(text.replace("      full: true\n", ""),
                                           encoding="utf-8")
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **kw: None)
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli.main(["serve", "--root", str(small_root), "--no-browser"]) == 0
    assert "тренда среднего покрытия не будет" in capsys.readouterr().out


def test_demo_startup_does_not_warn_about_trend(demo_root, monkeypatch, capsys):
    import uvicorn

    from veha import cli

    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **kw: None)
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli.main(["serve", "--root", str(demo_root), "--no-browser"]) == 0
    assert "тренда" not in capsys.readouterr().out


def test_rotation_failed_still_updates_map_after_history(small_root, fake_popen, monkeypatch):
    """Raw уже опубликован: prune-ошибка видна, но не оставляет карту старой."""
    import yaml

    from veha import allure_runner
    from veha.core import map_state
    from veha.runner import Runner

    config_path = small_root / "atlas.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["runner"]["allure"] = {}
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
    monkeypatch.setattr(allure_runner, "prepare",
                        lambda *args, **kwargs: {"recovery": "ok"})

    def published_with_failed_prune(self):
        self._set_report({"status": "rotation_failed", "snapshot": "published-raw",
                          "error": "prune failed"})

    monkeypatch.setattr(Runner, "_publish_allure", published_with_failed_prune)
    updates = []
    monkeypatch.setattr(map_state, "update",
                        lambda *args, **kwargs: updates.append((args, kwargs)))
    client = make_client(small_root, readonly=False)
    hist = artifact_path(small_root, "runner.history")
    before = hist.read_bytes()
    response = client.post("/api/runner/start", json={"suite": "smoke"})
    assert response.status_code == 200
    client.app.state.veha.runner.wait(5)
    _wait_for_history(hist, before)

    assert len(updates) == 1
    last = json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]
    assert last["report"]["status"] == "rotation_failed"
    assert last["report"]["error"] == "prune failed"


# ------------------------------------------------------------------


def test_suite_display_scope_is_shared_and_does_not_change_map_scope(tmp_path):
    from veha.config import Config
    from veha.runner import suites

    cfg = Config(root=tmp_path, raw={"runner": {"suites": {
        "browser": {"args": ["tests/ui"], "browser": True},
        "windows": {"args": [r"tests\ui\smoke"]},
        "acceptance": {"args": ["checks/browser"], "browser": True},
        "api": {"args": ["tests/api"], "map_scope": "api"},
        "ui-map": {"args": ["tests/api"], "map_scope": "ui"},
    }}})
    by_key = {suite.key: suite for suite in suites(cfg)}
    assert by_key["browser"].display_scope == "ui"
    assert by_key["browser"].browser is True
    assert by_key["browser"].map_scope == ""
    assert by_key["windows"].display_scope == "ui"
    assert by_key["acceptance"].display_scope == "ui"
    assert by_key["api"].display_scope == "api"
    assert by_key["ui-map"].display_scope == "ui"


def test_runner_suites_api_exposes_display_scope_and_browser(small_root):
    import yaml

    path = small_root / "atlas.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["runner"]["suites"] = {
        "browser": {"args": ["tests/ui"], "browser": True},
        "api": {"args": ["tests/api"]},
    }
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    suites = make_client(small_root).get("/api/runner/suites").json()["suites"]
    assert suites[0]["scope"] == "ui" and suites[0]["browser"] is True
    assert suites[0]["mapScope"] == ""
    assert suites[1]["scope"] == "api" and suites[1]["browser"] is False


# ------------------------------------------------------------------


def test_maintenance_persists_and_blocks_new_runner_operations(small_root, fake_popen):
    (small_root / "t_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    c = make_client(small_root, readonly=False)
    response = c.post("/api/runner/maintenance", json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["maintenance"] is True
    assert c.get("/api/runner/status").json()["maintenance"] is True
    assert c.get("/api/runner/suites").json()["maintenance"] is True
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 409
    assert c.post("/api/rerun", json={"nodeid": "t_x.py::test_x"}).status_code == 409
    assert c.post("/api/runner/clean").status_code == 409
    assert fake_popen == []

    restarted = make_client(small_root, readonly=False)
    assert restarted.get("/api/runner/status").json()["maintenance"] is True
    assert restarted.post("/api/runner/start", json={"suite": "smoke"}).status_code == 409
    assert fake_popen == []
    assert restarted.post(
        "/api/runner/maintenance", json={"enabled": False}).json()["maintenance"] is False
    assert restarted.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200


def test_maintenance_does_not_disable_emergency_stop(small_root, monkeypatch):
    from veha import runner

    monkeypatch.setattr(runner.subprocess, "Popen", BusyProc)
    monkeypatch.setattr(runner, "kill_tree", lambda proc: "")
    c = make_client(small_root, readonly=False)
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200
    assert c.post("/api/runner/maintenance", json={"enabled": True}).status_code == 200
    response = c.post("/api/runner/stop")
    assert response.status_code == 200
    assert response.json()["stopped"] is True
    assert response.json()["maintenance"] is True


def test_maintenance_is_writable_only_and_unreadable_state_fails_closed(
        small_root, fake_popen):
    readonly = make_client(small_root, readonly=True)
    path = small_root / ".veha" / "runner-maintenance.json"
    assert readonly.post("/api/runner/maintenance", json={"enabled": True}).status_code == 409
    assert not path.exists()

    path.parent.mkdir()
    path.write_text("not json", encoding="utf-8")
    c = make_client(small_root, readonly=False)
    assert c.get("/api/runner/status").status_code == 409
    assert c.get("/api/runner/suites").status_code == 409
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 409
    assert fake_popen == []


def test_maintenance_rejects_linked_state_directory(small_root, tmp_path, fake_popen):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    linked = small_root / ".veha"
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(foreign), str(linked))
        else:
            os.symlink(foreign, linked, target_is_directory=True)
    except (OSError, NotImplementedError, ImportError, AttributeError):
        pytest.skip("ссылку на каталог здесь не создать")

    c = make_client(small_root, readonly=False)
    assert c.get("/api/runner/status").status_code == 409
    assert c.post("/api/runner/maintenance", json={"enabled": True}).status_code == 409
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 409
    assert not (foreign / "runner-maintenance.json").exists()
    assert fake_popen == []


def test_admitted_rerun_is_busy_during_maintenance(small_root):
    c = make_client(small_root, readonly=False)
    srv = c.app.state.veha
    assert srv.rerun_lock.acquire(blocking=False)
    try:
        response = c.post("/api/runner/maintenance", json={"enabled": True})
        assert response.status_code == 200
        assert response.json()["busy"] is True
        assert c.get("/api/runner/status").json()["busy"] is True
        assert c.get("/api/runner/suites").json()["busy"] is True
    finally:
        srv.rerun_lock.release()
    assert c.get("/api/runner/status").json()["busy"] is False
