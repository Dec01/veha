"""Накопленная карта: partial/full по областям и независимость от raw-ротации."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from partest_atlas import artifacts
from partest_atlas.artifacts import Scope
from partest_atlas.collect import ui_flow as atlas_ui_flow

from tests.conftest import artifact_path
from veha.config import Config
from veha.core import map_state, sources
from veha.runner import Suite


def _result(root: Path, name: str, *, ui: bool, endpoint: str) -> None:
    raw = root / "allure-results"
    raw.mkdir(exist_ok=True)
    for old in raw.glob("*-result.json"):
        old.unlink()
    module = "src.ui.tests.test_screen" if ui else "src.api.tests.test_resource"
    (raw / f"{name}-result.json").write_text(json.dumps({
        "name": name,
        "fullName": f"{module}#{name}",
        "status": "passed",
        "start": 1000,
        "stop": 1100,
        "steps": [] if ui else [{"name": f"HTTP GET {endpoint}", "status": "passed"}],
    }), encoding="utf-8")
    (root / "coverage.json").write_text(json.dumps({
        "endpoints": [] if ui else [{"method": "GET", "path": endpoint, "coverage": 100}],
        "meta": {"avgRule": 2},
    }), encoding="utf-8")


def _update(root: Path, name: str, *, ui: bool, endpoint: str, full: bool) -> None:
    _result(root, name, ui=ui, endpoint=endpoint)
    scope_name = "ui" if ui else "api"
    map_state.update(Config(root=root, raw={"tests": {"ui_markers": ["src.ui"]}}),
                     Scope(), Suite(key=name, label=name, map_scope=scope_name, map_full=full))


def _tests(root: Path):
    loaded = map_state.apply(sources.load(Config(
        root=root, raw={"tests": {"ui_markers": ["src.ui"]}}), Scope()))
    return loaded, {t["name"] for t in loaded.allure["tests"]}


def test_partial_upserts_and_full_replaces_only_own_scope(tmp_path):
    _update(tmp_path, "api_old", ui=False, endpoint="/api/old", full=True)
    _update(tmp_path, "ui_state", ui=True, endpoint="", full=True)
    _, names = _tests(tmp_path)
    assert names == {"api_old", "ui_state"}

    _update(tmp_path, "api_new", ui=False, endpoint="/api/new", full=False)
    loaded, names = _tests(tmp_path)
    assert names == {"api_old", "api_new", "ui_state"}
    assert set(loaded.allure["calls"]) == {"api:GET /api/old", "api:GET /api/new"}

    _update(tmp_path, "api_latest", ui=False, endpoint="/api/latest", full=True)
    loaded, names = _tests(tmp_path)
    assert names == {"api_latest", "ui_state"}
    assert set(loaded.allure["calls"]) == {"api:GET /api/latest"}
    assert set(loaded.coverage) == {"api:GET /api/latest"}

    # State не является ссылкой на raw: ротация/очистка отчёта не стирает карту.
    for path in (tmp_path / "allure-results").glob("*"):
        path.unlink()
    (tmp_path / "coverage.json").unlink()
    _, names = _tests(tmp_path)
    assert names == {"api_latest", "ui_state"}


def test_full_ui_does_not_remove_api(tmp_path):
    _update(tmp_path, "api_state", ui=False, endpoint="/api/state", full=True)
    _update(tmp_path, "ui_old", ui=True, endpoint="", full=True)
    _update(tmp_path, "ui_new", ui=True, endpoint="", full=True)
    _, names = _tests(tmp_path)
    assert names == {"api_state", "ui_new"}


def test_broken_state_is_visible_and_does_not_fabricate_merge(tmp_path):
    _result(tmp_path, "current", ui=False, endpoint="/api/current")
    cfg = Config(root=tmp_path, raw={})
    path = map_state.state_path(cfg, Scope())
    path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")
    loaded = map_state.apply(sources.load(cfg, Scope()))
    assert [t["name"] for t in loaded.allure["tests"]] == ["current"]
    assert any("накопленная карта не прочитана" in p for p in loaded.report_problems)


def _case(root: Path, file: str, name: str, endpoint: str) -> None:
    raw = root / "allure-results"
    raw.mkdir(exist_ok=True)
    (raw / file).write_text(json.dumps({
        "name": name,
        "fullName": "src.api.tests.test_resource#test_case",
        "status": "passed",
        "steps": [{"name": f"HTTP GET {endpoint}", "status": "passed"}],
    }), encoding="utf-8")


def _partial_update(root: Path) -> None:
    map_state.update(Config(root=root, raw={}), Scope(),
                     Suite(key="partial", label="partial", map_scope="api", map_full=False))


def test_partial_rerun_replaces_only_one_parameterized_variant(tmp_path):
    base = "src/api/tests/test_resource.py::test_case"
    _case(tmp_path, "a-old-result.json", "test_case[a]", "/api/a-old")
    _case(tmp_path, "b-result.json", "test_case[b]", "/api/b")
    _partial_update(tmp_path)

    for path in (tmp_path / "allure-results").glob("*-result.json"):
        path.unlink()
    _case(tmp_path, "a-new-result.json", "test_case[a]", "/api/a-new")
    _partial_update(tmp_path)

    loaded = map_state.apply(sources.load(Config(root=tmp_path, raw={}), Scope()))
    assert {t["variant"] for t in loaded.allure["tests"]} == {base + "[a]", base + "[b]"}
    calls = loaded.allure["calls"]
    assert set(calls) == {"api:GET /api/a-new", "api:GET /api/b"}
    assert calls["api:GET /api/a-new"][0]["variant"] == base + "[a]"
    assert calls["api:GET /api/b"][0]["variant"] == base + "[b]"


def test_partial_rerun_replaces_unparameterized_test_calls(tmp_path):
    nodeid = "src/api/tests/test_resource.py::test_case"
    _case(tmp_path, "old-result.json", "test_case", "/api/old")
    _partial_update(tmp_path)

    for path in (tmp_path / "allure-results").glob("*-result.json"):
        path.unlink()
    _case(tmp_path, "new-result.json", "test_case", "/api/new")
    _partial_update(tmp_path)

    loaded = map_state.apply(sources.load(Config(root=tmp_path, raw={}), Scope()))
    assert [t["variant"] for t in loaded.allure["tests"]] == [nodeid]
    assert set(loaded.allure["calls"]) == {"api:GET /api/new"}
    assert loaded.allure["calls"]["api:GET /api/new"][0]["variant"] == nodeid


# ------------------------------------------------------------------
# Наблюдения из append-артефактов: текущий run и повторные variants


def _save_observations(root: Path, kind: str, records: list[dict], *, append: bool = False) -> None:
    path = artifact_path(root, kind)
    key = sources.PAYLOAD_KEY[kind]
    if append:
        artifacts.append_records(path, kind, key, records, scope=Scope(), collector="tests")
    else:
        # Форма и статистика — настоящий сборщик Atlas, не тестовый аналог контракта.
        payload = atlas_ui_flow.build(records) if kind == "ui.flow" else {key: records}
        artifacts.write(path, kind, payload, scope=Scope(), collector="tests")


def _update_observations(root: Path, run_id: str, *, scope_name: str = "api",
                         full: bool = False) -> None:
    map_state.update(
        Config(root=root, raw={"tests": {"ui_markers": ["src.ui"]}}), Scope(),
        Suite(key="observed", label="observed", map_scope=scope_name, map_full=full),
        run_id=run_id)


def _saved_artifact(root: Path, kind: str) -> dict:
    path = map_state.state_path(Config(root=root, raw={}), Scope())
    return json.loads(path.read_text(encoding="utf-8"))["artifacts"][kind]


def _run(run_id: str) -> dict:
    return {"id": run_id, "startedAt": f"2026-11-16T12:00:{run_id[-2:]}+03:00"}


def test_partial_replaces_rerun_observations_and_ignores_append_history(tmp_path):
    base = "src/api/tests/test_resource.py::test_case"
    _case(tmp_path, "a-old-result.json", "test_case[a]", "/api/a-old")
    _case(tmp_path, "b-result.json", "test_case[b]", "/api/b")
    _save_observations(tmp_path, "db.effects", [
        {"nodeid": base, "variant": base + "[a]", "table": "a_old", "run": _run("run-01")},
        {"nodeid": base, "variant": base + "[b]", "table": "b", "run": _run("run-01")},
    ], append=True)
    _update_observations(tmp_path, "run-01")

    for path in (tmp_path / "allure-results").glob("*-result.json"):
        path.unlink()
    _case(tmp_path, "a-new-result.json", "test_case[a]", "/api/a-new")
    _save_observations(tmp_path, "db.effects", [
        # Этой записи не было в state после run-01: повторное обновление не импортирует её.
        {"nodeid": "other.py::test_other", "table": "history", "run": _run("run-01")},
        {"nodeid": base, "variant": base + "[a]", "table": "a_new", "run": _run("run-02")},
    ], append=True)
    _update_observations(tmp_path, "run-02")

    records = _saved_artifact(tmp_path, "db.effects")["records"]
    assert {(r["variant"], r["table"], r["run"]["id"]) for r in records} == {
        (base + "[a]", "a_new", "run-02"),
        (base + "[b]", "b", "run-01"),
    }

    # Повтор A вообще без обращения к БД удаляет его прежнюю связь, но не трогает B.
    for path in (tmp_path / "allure-results").glob("*-result.json"):
        path.unlink()
    _case(tmp_path, "a-empty-result.json", "test_case[a]", "/api/a-empty")
    _update_observations(tmp_path, "run-03")
    records = _saved_artifact(tmp_path, "db.effects")["records"]
    assert [(r["variant"], r["table"]) for r in records] == [(base + "[b]", "b")]


def test_partial_observations_distinguish_variants_and_legacy_nodeids(tmp_path):
    base = "src/api/tests/test_resource.py::test_case"
    _case(tmp_path, "a-result.json", "test_case[a]", "/api/a")
    _case(tmp_path, "b-result.json", "test_case[b]", "/api/b")
    _save_observations(tmp_path, "db.effects", [
        {"nodeid": base, "variant": base + "[a]", "table": "a_old", "run": _run("run-01")},
        {"nodeid": base, "variant": base + "[b]", "table": "b", "run": _run("run-01")},
    ], append=True)
    _update_observations(tmp_path, "run-01")

    # Так выглядят записи, уже лежащие в state от версии без run-меток.
    path = map_state.state_path(Config(root=tmp_path, raw={}), Scope())
    state = json.loads(path.read_text(encoding="utf-8"))
    state["artifacts"]["db.effects"]["records"].extend([
        {"nodeid": base + "[a]", "table": "legacy_exact"},
        {"nodeid": base, "table": "legacy_base"},
    ])
    path.write_text(json.dumps(state), encoding="utf-8")

    for result in (tmp_path / "allure-results").glob("*-result.json"):
        result.unlink()
    _case(tmp_path, "a-new-result.json", "test_case[a]", "/api/a-new")
    _save_observations(tmp_path, "db.effects", [
        {"nodeid": base, "variant": base + "[a]", "table": "a_new", "run": _run("run-02")},
    ], append=True)
    _update_observations(tmp_path, "run-02")

    records = _saved_artifact(tmp_path, "db.effects")["records"]
    assert {r["table"] for r in records} == {"a_new", "b", "legacy_base"}
    assert sum(r["table"] == "legacy_base" for r in records) == 1


def test_full_replaces_current_run_only_inside_own_scope(tmp_path):
    _result(tmp_path, "api_old", ui=False, endpoint="/api/old")
    _save_observations(tmp_path, "db.effects", [
        {"nodeid": "api_old", "table": "api_old", "run": _run("run-01")}], append=True)
    _save_observations(tmp_path, "bus.flow", [
        {"nodeid": "api_old", "topic": "api_old", "run": _run("run-01")}], append=True)
    _save_observations(tmp_path, "containers.logs", [{"container": "old", "phase": "before"}])
    _update_observations(tmp_path, "run-01", full=True)

    _result(tmp_path, "ui_old", ui=True, endpoint="")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": "ui_old", "screen": "/old", "path": "/api/old", "run": _run("run-01")}])
    _update_observations(tmp_path, "run-01", scope_name="ui", full=True)

    _result(tmp_path, "api_new", ui=False, endpoint="/api/new")
    _save_observations(tmp_path, "db.effects", [
        {"nodeid": "api_new", "table": "api_new", "run": _run("run-02")}], append=True)
    _save_observations(tmp_path, "bus.flow", [
        {"nodeid": "api_new", "topic": "api_new", "run": _run("run-02")}], append=True)
    _save_observations(tmp_path, "containers.logs", [{"container": "new", "phase": "after"}])
    _update_observations(tmp_path, "run-02", full=True)

    assert [r["table"] for r in _saved_artifact(tmp_path, "db.effects")["records"]] == ["api_new"]
    assert [r["topic"] for r in _saved_artifact(tmp_path, "bus.flow")["records"]] == ["api_new"]
    assert [r["screen"] for r in _saved_artifact(tmp_path, "ui.flow")["links"]] == ["/old"]
    assert [r["container"] for r in _saved_artifact(
        tmp_path, "containers.logs")["containers"]] == ["new"]

    _result(tmp_path, "ui_new", ui=True, endpoint="")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": "ui_old", "screen": "/stale", "path": "/api/stale", "run": _run("run-01")},
        {"nodeid": "ui_new", "screen": "/new", "path": "/api/new", "run": _run("run-03")},
    ])
    _update_observations(tmp_path, "run-03", scope_name="ui", full=True)

    saved_ui = _saved_artifact(tmp_path, "ui.flow")
    assert [r["screen"] for r in saved_ui["links"]] == ["/new"]
    assert saved_ui["stats"]["links"] == 1
    assert [r["table"] for r in _saved_artifact(tmp_path, "db.effects")["records"]] == ["api_new"]
    assert [r["container"] for r in _saved_artifact(
        tmp_path, "containers.logs")["containers"]] == ["new"]


def test_containers_logs_keep_partial_union_and_full_replace(tmp_path):
    _result(tmp_path, "api_old", ui=False, endpoint="/api/old")
    _save_observations(tmp_path, "containers.logs", [{"container": "old", "phase": "before"}])
    _update_observations(tmp_path, "run-01")

    _result(tmp_path, "api_new", ui=False, endpoint="/api/new")
    _save_observations(tmp_path, "containers.logs", [{"container": "new", "phase": "after"}])
    _update_observations(tmp_path, "run-02")
    assert [r["container"] for r in _saved_artifact(
        tmp_path, "containers.logs")["containers"]] == ["old", "new"]

    _save_observations(tmp_path, "containers.logs", [{"container": "latest", "phase": "after"}])
    _update_observations(tmp_path, "run-03", full=True)
    assert [r["container"] for r in _saved_artifact(
        tmp_path, "containers.logs")["containers"]] == ["latest"]


def test_marked_ui_flow_partial_replaces_a_and_empty_a_preserves_b(tmp_path):
    base = "src/ui/tests/test_screen.py::test_case"
    _result(tmp_path, "test_case[a]", ui=True, endpoint="")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": base + "[a]", "screen": "/a", "path": "/api/old",
         "run": _run("run-01")},
        {"nodeid": base, "variant": base + "[b]", "screen": "/b", "path": "/api/b",
         "run": _run("run-01")},
    ])
    _update_observations(tmp_path, "run-01", scope_name="ui")

    _result(tmp_path, "test_case[a]", ui=True, endpoint="")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": base + "[a]", "screen": "/a", "path": "/api/new",
         "run": _run("run-02")},
    ])
    _update_observations(tmp_path, "run-02", scope_name="ui")
    links = _saved_artifact(tmp_path, "ui.flow")["links"]
    assert {(r["variant"], r["path"]) for r in links} == {
        (base + "[a]", "/api/new"), (base + "[b]", "/api/b")}

    # Включённый collector пишет настоящий пустой build(): связей A больше нет.
    _save_observations(tmp_path, "ui.flow", [])
    _update_observations(tmp_path, "run-03", scope_name="ui")
    saved = _saved_artifact(tmp_path, "ui.flow")
    assert [(r["variant"], r["path"]) for r in saved["links"]] == [
        (base + "[b]", "/api/b")]
    assert saved["stats"]["links"] == 1


def test_title_variants_partial_replaces_only_a_calls_and_ui_observations(tmp_path):
    base = "src/ui/tests/test_screen.py::test_case"
    raw = tmp_path / "allure-results"
    raw.mkdir()

    def case(file: str, title: str, value: str, endpoint: str) -> None:
        (raw / file).write_text(json.dumps({
            "name": title, "fullName": "src.ui.tests.test_screen#test_case",
            "status": "passed", "start": 100, "stop": 200,
            "parameters": [{"name": "entity", "value": f"'{value}'"}],
            "steps": [{"name": f"HTTP GET {endpoint}", "status": "passed"}],
        }), encoding="utf-8")

    case("a-old-result.json", "Заказ", "orders", "/api/a-old")
    case("b-result.json", "Счёт", "invoices", "/api/b")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": base + "[orders]", "screen": "/a",
         "path": "/api/ui-a-old", "run": _run("run-01")},
        {"nodeid": base, "variant": base + "[invoices]", "screen": "/b",
         "path": "/api/ui-b", "run": _run("run-01")},
    ])
    _update_observations(tmp_path, "run-01", scope_name="ui")

    for result in raw.glob("*-result.json"):
        result.unlink()
    case("a-new-result.json", "Заказ", "orders", "/api/a-new")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": base + "[orders]", "screen": "/a",
         "path": "/api/ui-a-new", "run": _run("run-02")},
    ])
    _update_observations(tmp_path, "run-02", scope_name="ui")

    loaded, names = _tests(tmp_path)
    assert names == {"Заказ", "Счёт"}
    assert set(loaded.allure["calls"]) == {"api:GET /api/a-new", "api:GET /api/b"}
    links = _saved_artifact(tmp_path, "ui.flow")["links"]
    assert {(r["variant"], r["path"], r["run"]["id"]) for r in links} == {
        (base + "[orders]", "/api/ui-a-new", "run-02"),
        (base + "[invoices]", "/api/ui-b", "run-01"),
    }


def test_colliding_atlas_alias_partial_preserves_observations_and_reports_problem(tmp_path):
    base = "src/ui/tests/test_screen.py::test_case"
    alias = base + "[a-b-c]"
    raw = tmp_path / "allure-results"
    raw.mkdir()

    def case(file: str, first: str, second: str, endpoint: str) -> None:
        (raw / file).write_text(json.dumps({
            "name": "Скрытый variant", "fullName": "src.ui.tests.test_screen#test_case",
            "status": "passed", "start": 100, "stop": 200,
            "parameters": [{"name": "first", "value": first},
                           {"name": "second", "value": second}],
            "steps": [{"name": f"HTTP GET {endpoint}", "status": "passed"}],
        }), encoding="utf-8")

    case("a-old-result.json", "a-b", "c", "/api/a-old")
    case("b-result.json", "a", "b-c", "/api/b")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": alias, "screen": "/a", "path": "/api/ui-a-old",
         "run": _run("run-01")},
        {"nodeid": base, "variant": alias, "screen": "/b", "path": "/api/ui-b",
         "run": _run("run-01")},
    ])
    _update_observations(tmp_path, "run-01", scope_name="ui")

    for result in raw.glob("*-result.json"):
        result.unlink()
    case("a-new-result.json", "a-b", "c", "/api/a-new")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": alias, "screen": "/a", "path": "/api/ui-a-new",
         "run": _run("run-02")},
    ])
    _update_observations(tmp_path, "run-02", scope_name="ui")

    loaded, _ = _tests(tmp_path)
    assert len(loaded.allure["tests"]) == 2
    assert len({t["paramVariant"] for t in loaded.allure["tests"]}) == 2
    assert set(loaded.allure["calls"]) == {"api:GET /api/a-new", "api:GET /api/b"}
    links = _saved_artifact(tmp_path, "ui.flow")["links"]
    assert {(r["variant"], r["path"], r["run"]["id"]) for r in links} == {
        (alias, "/api/ui-a-old", "run-01"),
        (alias, "/api/ui-b", "run-01"),
        (alias, "/api/ui-a-new", "run-02"),
    }
    assert any(alias in p and "неоднозначен" in p and "observations" in p
               and "сохранены" in p for p in loaded.report_problems)


def test_unmarked_ui_flow_partial_preserves_old_and_reports_problem(tmp_path):
    base = "src/ui/tests/test_screen.py::test_case"
    _result(tmp_path, "test_case[a]", ui=True, endpoint="")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": base + "[a]", "screen": "/a", "path": "/api/old",
         "run": _run("run-01")},
        {"nodeid": base, "variant": base + "[b]", "screen": "/b", "path": "/api/b",
         "run": _run("run-01")},
    ])
    _update_observations(tmp_path, "run-01", scope_name="ui")

    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": base + "[a]", "screen": "/a", "path": "/api/new"},
    ])
    _update_observations(tmp_path, "run-02", scope_name="ui")

    links = _saved_artifact(tmp_path, "ui.flow")["links"]
    assert {(r["variant"], r["path"]) for r in links} == {
        (base + "[a]", "/api/old"), (base + "[b]", "/api/b")}
    loaded, _ = _tests(tmp_path)
    assert any("ui.flow" in p and "без меток run" in p
               for p in loaded.report_problems)


def test_unmarked_ui_flow_full_is_compatible_and_new_empty_full_clears(tmp_path):
    base = "src/ui/tests/test_screen.py::test_case"
    _result(tmp_path, "test_case[a]", ui=True, endpoint="")
    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": base + "[a]", "screen": "/a", "path": "/api/old",
         "run": _run("run-01")},
    ])
    _update_observations(tmp_path, "run-01", scope_name="ui", full=True)

    _save_observations(tmp_path, "ui.flow", [
        {"nodeid": base, "variant": base + "[a]", "screen": "/a", "path": "/api/legacy"},
    ])
    _update_observations(tmp_path, "run-02", scope_name="ui", full=True)
    assert [r["path"] for r in _saved_artifact(tmp_path, "ui.flow")["links"]] == [
        "/api/legacy"]

    _save_observations(tmp_path, "ui.flow", [])
    _update_observations(tmp_path, "run-03", scope_name="ui", full=True)
    assert _saved_artifact(tmp_path, "ui.flow")["links"] == []
    assert _saved_artifact(tmp_path, "ui.flow")["stats"]["links"] == 0


def test_accumulated_legacy_records_are_not_current_without_run_id(tmp_path):
    _result(tmp_path, "api_old", ui=False, endpoint="/api/old")
    _save_observations(tmp_path, "db.effects", [
        {"nodeid": "api_old", "table": "saved", "run": _run("run-01")}], append=True)
    _update_observations(tmp_path, "run-01")
    before = _saved_artifact(tmp_path, "db.effects")

    _save_observations(tmp_path, "db.effects", [{"nodeid": "legacy", "table": "legacy"}])
    _update_observations(tmp_path, "")
    assert _saved_artifact(tmp_path, "db.effects") == before
    assert all(r.get("run", {}).get("id") == "run-01"
               for r in _saved_artifact(tmp_path, "db.effects")["records"])


# ------------------------------------------------------------------
# Повторные attempts и membership вложений


def _attempt(root: Path, file: str, *, name: str = "test_case", status: str = "passed",
             start: int = 100, stop: int = 200, endpoint: str = "/api/current",
             attachment: str = "") -> None:
    raw = root / "allure-results"
    raw.mkdir(exist_ok=True)
    attachments = [{"name": "test log", "source": attachment}] if attachment else []
    steps = [{"name": f"HTTP GET {endpoint}", "status": status,
              "attachments": attachments}]
    (raw / file).write_text(json.dumps({
        "name": name,
        "fullName": f"src.api.tests.test_resource#{name}",
        "status": status,
        "start": start,
        "stop": stop,
        "attachments": attachments,
        "steps": steps,
    }), encoding="utf-8")
    if attachment:
        (raw / attachment).write_text(attachment, encoding="utf-8")


def test_rerun_uses_chronological_winner_for_tests_calls_and_attachments(tmp_path):
    old = "00000000-0000-0000-0000-000000000001-attachment.txt"
    new = "00000000-0000-0000-0000-000000000002-attachment.txt"
    # Имя файла специально противоположно chronology: UUID/сортировка не решают.
    _attempt(tmp_path, "z-original-result.json", status="failed", start=100, stop=200,
             endpoint="/api/original", attachment=old)
    _attempt(tmp_path, "a-rerun-result.json", status="passed", start=300, stop=400,
             endpoint="/api/rerun", attachment=new)
    _partial_update(tmp_path)

    loaded, _ = _tests(tmp_path)
    assert [(t["file"], t["status"]) for t in loaded.allure["tests"]] == [
        ("a-rerun-result.json", "passed")]
    assert set(loaded.allure["calls"]) == {"api:GET /api/rerun"}
    assert loaded.allure["attachments"] == {new}
    assert (tmp_path / "allure-results" / "z-original-result.json").is_file()
    assert (tmp_path / "allure-results" / "a-rerun-result.json").is_file()


def test_equal_attempt_timestamps_use_mtime_not_reversed_file_names(tmp_path):
    old = "00000000-0000-0000-0000-000000000003-attachment.txt"
    new = "00000000-0000-0000-0000-000000000004-attachment.txt"
    _attempt(tmp_path, "z-original-result.json", status="failed", start=100, stop=200,
             endpoint="/api/original", attachment=old)
    _attempt(tmp_path, "a-rerun-result.json", status="passed", start=100, stop=200,
             endpoint="/api/rerun", attachment=new)
    raw = tmp_path / "allure-results"
    os.utime(raw / "z-original-result.json", ns=(1_000_000_000, 1_000_000_000))
    os.utime(raw / "a-rerun-result.json", ns=(2_000_000_000, 2_000_000_000))
    _partial_update(tmp_path)

    loaded, _ = _tests(tmp_path)
    assert [(t["file"], t["status"]) for t in loaded.allure["tests"]] == [
        ("a-rerun-result.json", "passed")]
    assert set(loaded.allure["calls"]) == {"api:GET /api/rerun"}
    assert loaded.allure["attachments"] == {new}


def test_equal_attempt_time_and_mtime_use_deterministic_content_fallback(tmp_path):
    _attempt(tmp_path, "z-original-result.json", status="failed", start=100, stop=200)
    _attempt(tmp_path, "a-rerun-result.json", status="passed", start=100, stop=200)
    raw = tmp_path / "allure-results"
    for path in raw.glob("*-result.json"):
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))

    # В каноническом JSON "passed" > "failed"; это tie-break, а не выдуманная хронология.
    first = sources.load_allure(raw)
    second = sources.load_allure(raw)
    assert [t["status"] for t in first["tests"]] == ["passed"]
    assert first == second


def test_attachments_are_rebuilt_after_partial_and_full_merges(tmp_path):
    first = "00000000-0000-0000-0000-000000000011-attachment.txt"
    second = "00000000-0000-0000-0000-000000000012-attachment.txt"
    _attempt(tmp_path, "a-result.json", name="test_a", endpoint="/api/a", attachment=first)
    _attempt(tmp_path, "b-result.json", name="test_b", endpoint="/api/b", attachment=second)
    _partial_update(tmp_path)
    loaded, _ = _tests(tmp_path)
    assert loaded.allure["attachments"] == {first, second}

    for path in (tmp_path / "allure-results").glob("*-result.json"):
        path.unlink()
    _attempt(tmp_path, "a-new-result.json", name="test_a", start=300, stop=400,
             endpoint="/api/a-new")
    _partial_update(tmp_path)
    loaded, names = _tests(tmp_path)
    assert names == {"test_a", "test_b"}
    assert set(loaded.allure["calls"]) == {"api:GET /api/a-new", "api:GET /api/b"}
    assert loaded.allure["attachments"] == {second}

    for path in (tmp_path / "allure-results").glob("*-result.json"):
        path.unlink()
    map_state.update(Config(root=tmp_path, raw={}), Scope(),
                     Suite(key="empty", label="empty", map_scope="api", map_full=True))
    loaded, names = _tests(tmp_path)
    assert names == set()
    assert loaded.allure["calls"] == {}
    assert loaded.allure["attachments"] == set()


# ------------------------------------------------------------------
# Безопасность пути state


def _junction(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.mark.parametrize("relative", [".veha", ".veha/map"])
def test_state_rejects_linked_parents_before_any_creation(tmp_path, relative):
    root = tmp_path / "project"
    foreign = tmp_path / "foreign"
    root.mkdir()
    foreign.mkdir()
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        _junction(link, foreign)
    except (OSError, NotImplementedError, ImportError, AttributeError):
        pytest.skip("ссылку на каталог здесь не создать")

    cfg = Config(root=root, raw={})
    path = map_state.state_path(cfg, Scope())
    with pytest.raises(ValueError, match="ссылка"):
        map_state._write(cfg.root, path, {"version": map_state.VERSION})
    assert list(foreign.iterdir()) == []

    loaded = map_state.apply(sources.load(cfg, Scope()))
    assert any("накопленная карта не прочитана" in p for p in loaded.report_problems)
    assert list(foreign.iterdir()) == []


def test_state_never_creates_a_lexically_external_path(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside" / "state.json"
    with pytest.raises(ValueError, match="вне корня"):
        map_state._write(root, outside, {"version": map_state.VERSION})
    assert not outside.exists()
    assert not outside.parent.exists()


def test_partial_rerun_custom_bracket_title_does_not_create_duplicate(tmp_path):
    nodeid = "src/api/tests/test_resource.py::test_case"
    _case(tmp_path, "old-result.json", "Проверка [smoke]", "/api/old")
    _partial_update(tmp_path)

    for path in (tmp_path / "allure-results").glob("*-result.json"):
        path.unlink()
    _case(tmp_path, "new-result.json", "Проверка [smoke]", "/api/new")
    _partial_update(tmp_path)

    loaded, names = _tests(tmp_path)
    assert names == {"Проверка [smoke]"}
    assert [t["variant"] for t in loaded.allure["tests"]] == [nodeid]
    assert set(loaded.allure["calls"]) == {"api:GET /api/new"}
