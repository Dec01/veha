"""Просмотр выбранного завершённого Allure-прогона, без сети и внешнего Allure."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from partest_atlas.artifacts import Scope
from partest_atlas.collect import history as atlas_history

from tests.conftest import make_client
from veha import allure_store


@pytest.fixture(autouse=True)
def _portable_latest(tmp_path, monkeypatch):
    """Тот же portable-адаптер symlink, что у интеграционных проверок хранилища."""
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


def _result(name: str, attachment: str, text: str, png: str = "") -> dict:
    attachments = [{"name": "log", "source": attachment}]
    if png:
        attachments.append({"name": "screen", "source": png})
    return {
        "uuid": name,
        "name": name,
        "fullName": f"src.api.tests.test_demo::{name}",
        "status": "failed",
        "statusDetails": {"message": f"{name}: assertion", "trace": "Traceback\nassert False"},
        "attachments": attachments,
        "steps": [{"name": text, "status": "passed", "steps": []}],
    }


def _publish(root: Path, suite: str, name: str, *, shared_png: bool = False) -> str:
    raw = root / "input" / suite
    raw.mkdir(parents=True)
    suffix = "1" if suite == "api" else "2"
    attachment = f"00000000-0000-0000-0000-00000000000{suffix}-attachment.txt"
    (raw / attachment).write_text(name, encoding="utf-8")
    png = "10000000-0000-0000-0000-000000000000-attachment.png" if shared_png else ""
    if png:
        (raw / png).write_bytes(b"\x89PNG\r\n\x1a\n" + suite.encode("ascii"))
    (raw / f"{name}-result.json").write_text(json.dumps(
        _result(name, attachment, name, png)), encoding="utf-8")

    def generate(_raw: Path, report: Path) -> None:
        (report / "index.html").write_text("ok", encoding="utf-8")

    result = allure_store.publish(root / ".veha" / "allure", system="shop", env="test",
                                  suite=suite, results=raw, generate=generate)
    return result.run_id


def _history(root: Path, entries: list[dict]) -> None:
    path = Scope("shop", "test").path(root / "atlas", atlas_history.KIND)
    for entry in entries:
        atlas_history.append(path, entry, scope=Scope("shop", "test"))


def _entry(suite: str, snapshot: str, **changes) -> dict:
    return {
        "suite": suite, "label": suite.upper(), "status": "done", "complete": True,
        "startedAt": 1, "finishedAt": 2, "total": 1, "passed": 0, "failed": 1,
        "report": {"status": "published", "snapshot": snapshot}, **changes,
    }


def test_selects_saved_raw_and_rejects_missing_or_foreign_attachment(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "atlas.yaml").write_text(
        "artifacts_dir: atlas\nreports:\n  allure: allure-results\n"
        "runner:\n  allure:\n    store: .veha/allure\n", encoding="utf-8")
    api = _publish(root, "api", "api-old")
    ui = _publish(root, "ui", "ui-new")
    _history(root, [_entry("api", api), _entry("ui", ui),
                    _entry("api", "not-published", report={"status": "failed"}),
                    _entry("api", "not-finished", complete=False)])
    client = make_client(root, scope=Scope("shop", "test"))

    graph = client.get("/api/graph").json()
    assert [r["key"] for r in graph["savedRuns"]] == [api, ui]
    assert graph["run"]["runKey"] == ui
    assert client.get("/api/graph", params={"run": api}).json()["run"]["runKey"] == api
    old = client.get("/api/tests", params={"run": api}).json()
    new = client.get("/api/tests", params={"run": ui}).json()
    assert [t["name"] for t in old["tests"]] == ["api-old"]
    assert [t["name"] for t in new["tests"]] == ["ui-new"]
    file = old["tests"][0]["file"]
    detail = client.get("/api/test", params={"file": file, "run": api}).json()
    assert detail["test"]["name"] == "api-old"
    assert detail["statusDetails"]["trace"].startswith("Traceback")
    source = detail["attachments"][0]["source"]
    response = client.get(f"/api/attachment/{source}", params={"run": api})
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "api-old"
    other = client.get("/api/test", params={"file": new["tests"][0]["file"], "run": ui}).json()
    foreign = other["attachments"][0]["source"]
    assert client.get(f"/api/attachment/{foreign}", params={"run": api}).status_code == 404
    assert client.get("/api/graph", params={"run": "deleted"}).status_code == 404
    assert client.get("/api/tests", params={"run": "deleted"}).status_code == 404

    raw = root / ".veha" / "allure" / "shop" / "test" / "api" / "raw" / api
    for path in raw.iterdir():
        path.unlink()
    raw.rmdir()
    assert client.get("/api/graph", params={"run": api}).status_code == 404
    assert [r["key"] for r in client.get("/api/graph").json()["savedRuns"]] == [ui]


def test_attachment_resolves_default_latest_once_per_request(tmp_path, monkeypatch):
    """Membership и байты относятся к одному raw, даже если latest сменился."""
    from veha.viz.app import Server

    root = tmp_path / "project"
    _configured_project(root)
    raw_a = root / "raw-a"
    raw_b = root / "raw-b"
    raw_a.mkdir()
    raw_b.mkdir()
    source = "00000000-0000-0000-0000-000000000001-attachment.txt"
    other = "00000000-0000-0000-0000-000000000002-attachment.txt"
    (raw_a / source).write_text("from-a", encoding="utf-8")
    (raw_b / source).write_text("from-b", encoding="utf-8")
    (raw_a / "a-result.json").write_text(json.dumps(
        _result("a", source, "a")), encoding="utf-8")
    (raw_b / "b-result.json").write_text(json.dumps(
        _result("b", other, "b")), encoding="utf-8")

    resolves = []

    def changing_latest(self, snapshot: str = ""):
        assert snapshot == ""
        resolves.append(snapshot)
        run = "run-a" if len(resolves) == 1 else "run-b"
        raw = raw_a if len(resolves) == 1 else raw_b
        return {"snapshot": run}, raw

    monkeypatch.setattr(Server, "selected_run", changing_latest)
    client = make_client(root, scope=Scope("shop", "test"))
    response = client.get(f"/api/attachment/{source}")

    assert response.status_code == 200, response.text
    assert response.json()["text"] == "from-a"
    assert resolves == [""]


def test_export_uses_one_saved_run_for_tests_details_and_same_named_png(tmp_path):
    """Накопленный граф и текущий raw не подмешиваются к выбранному снимку."""
    from veha.viz.export import export_static

    root = tmp_path / "project"
    root.mkdir()
    (root / "atlas.yaml").write_text(
        "artifacts_dir: atlas\nreports:\n  allure: allure-results\n"
        "runner:\n  allure:\n    store: .veha/allure\n", encoding="utf-8")
    api = _publish(root, "api", "api-old", shared_png=True)
    ui = _publish(root, "ui", "ui-new", shared_png=True)
    _history(root, [_entry("api", api), _entry("ui", ui)])

    current = root / "allure-results"
    current.mkdir()
    current_attachment = "20000000-0000-0000-0000-000000000000-attachment.txt"
    current_png = "10000000-0000-0000-0000-000000000000-attachment.png"
    (current / current_attachment).write_text("current-only", encoding="utf-8")
    (current / current_png).write_bytes(b"\x89PNG\r\n\x1a\ncurrent")
    (current / "current-only-result.json").write_text(json.dumps(
        _result("current-only", current_attachment, "current-only", current_png)),
        encoding="utf-8")

    out = tmp_path / "site"
    report = export_static(root, out, scope=Scope("shop", "test"))
    graph = json.loads((out / "api" / "graph.json").read_text(encoding="utf-8"))
    tests = json.loads((out / "api" / "tests.json").read_text(encoding="utf-8"))
    assert graph["run"]["runKey"] == ui
    assert [r["key"] for r in graph["savedRuns"]] == [api, ui]
    assert [t["name"] for t in tests["tests"]] == ["ui-new"]
    assert (out / "api" / "test" / "ui-new-result.json").is_file()
    assert not (out / "api" / "test" / "api-old-result.json").exists()
    assert not (out / "api" / "test" / "current-only-result.json").exists()
    assert (out / "api" / "attachment" / current_png).read_bytes() == \
        b"\x89PNG\r\n\x1a\nui"
    assert report.counts["tests"] == 1 and report.counts["attachments"] == 2
    assert report.skipped == []


def _configured_project(root: Path) -> None:
    root.mkdir()
    (root / "atlas.yaml").write_text(
        "artifacts_dir: atlas\nreports:\n  allure: allure-results\n"
        "runner:\n  allure:\n    store: .veha/allure\n", encoding="utf-8")


def test_saved_run_collapses_reruns_by_time_not_reversed_file_names(tmp_path):
    root = tmp_path / "project"
    _configured_project(root)
    raw = root / "rerun-input"
    raw.mkdir()
    old = "00000000-0000-0000-0000-000000000021-attachment.txt"
    new = "00000000-0000-0000-0000-000000000022-attachment.txt"
    for source, text in ((old, "original"), (new, "rerun")):
        (raw / source).write_text(text, encoding="utf-8")
    for file, status, start, stop, endpoint, source in (
            ("z-original-result.json", "failed", 100, 200, "/api/original", old),
            ("a-rerun-result.json", "passed", 300, 400, "/api/rerun", new)):
        (raw / file).write_text(json.dumps({
            "name": "test_case", "fullName": "src.api.tests.test_demo#test_case",
            "status": status, "start": start, "stop": stop,
            "attachments": [{"name": "log", "source": source}],
            "steps": [{"name": f"HTTP GET {endpoint}", "status": status}],
        }), encoding="utf-8")

    def generate(_raw: Path, report: Path) -> None:
        (report / "index.html").write_text("ok", encoding="utf-8")

    published = allure_store.publish(
        root / ".veha" / "allure", system="shop", env="test", suite="api",
        results=raw, generate=generate)
    _history(root, [_entry("api", published.run_id)])
    client = make_client(root, scope=Scope("shop", "test"))

    graph = client.get("/api/graph", params={"run": published.run_id}).json()
    assert graph["run"]["total"] == 1
    assert graph["run"]["passed"] == 1 and graph["run"]["failed"] == 0
    tests = client.get("/api/tests", params={"run": published.run_id}).json()
    assert [(t["file"], t["status"], t["endpoints"]) for t in tests["tests"]] == [
        ("a-rerun-result.json", "passed", ["api:GET /api/rerun"])]
    assert tests["tests"][0]["attachments"] == [new]
    assert client.get("/api/test", params={
        "file": "z-original-result.json", "run": published.run_id}).status_code == 404
    assert client.get(f"/api/attachment/{old}", params={
        "run": published.run_id}).status_code == 404
    response = client.get(f"/api/attachment/{new}", params={"run": published.run_id})
    assert response.status_code == 200 and response.json()["text"] == "rerun"
    assert (raw / "z-original-result.json").is_file()
    saved = root / ".veha" / "allure" / "shop" / "test" / "api" / "raw" / published.run_id
    assert (saved / "z-original-result.json").is_file()
    assert (saved / "a-rerun-result.json").is_file()


def test_rotation_failed_raw_is_selectable_before_recovery_without_history_rewrite(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    _configured_project(root)
    first = _publish(root, "api", "old")
    raw = root / "new-input"
    raw.mkdir()
    (raw / "new-result.json").write_text(json.dumps(
        _result("new", "00000000-0000-0000-0000-000000000001-attachment.txt", "new")),
        encoding="utf-8")

    def generate(_raw: Path, report: Path) -> None:
        (report / "index.html").write_text("new", encoding="utf-8")

    def fail_state(*args, **kwargs):
        raise OSError("state занят")

    monkeypatch.setattr(allure_store, "_write_state", fail_state)
    second = allure_store.publish(
        root / ".veha" / "allure", system="shop", env="test", suite="api",
        results=raw, generate=generate)
    assert second.rotation_status == "failed"
    _history(root, [
        _entry("api", first),
        _entry("api", second.run_id, report={
            "status": "rotation_failed", "snapshot": second.run_id,
            "error": "state занят"}),
    ])
    history_path = Scope("shop", "test").path(root / "atlas", atlas_history.KIND)
    before = history_path.read_bytes()
    client = make_client(root, scope=Scope("shop", "test"))

    graph = client.get("/api/graph", params={"run": second.run_id})
    assert graph.status_code == 200, graph.text
    assert graph.json()["run"]["runKey"] == second.run_id
    assert [t["name"] for t in client.get("/api/tests", params={
        "run": second.run_id}).json()["tests"]] == ["new"]
    assert history_path.read_bytes() == before
    history = client.get("/api/runner/suites").json()["history"]
    assert history[-1]["report"]["status"] == "rotation_failed"
    assert history[-1]["report"]["error"] == "state занят"


def test_symlink_result_is_visible_and_never_read_by_saved_run_api(tmp_path):
    root = tmp_path / "project"
    _configured_project(root)
    snapshot = _publish(root, "api", "linked")
    _history(root, [_entry("api", snapshot)])
    saved = root / ".veha" / "allure" / "shop" / "test" / "api" / "raw" / snapshot
    result = saved / "linked-result.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(result.read_bytes())
    result.unlink()
    try:
        os.symlink(outside, result)
    except (OSError, NotImplementedError):
        pytest.skip("символическую ссылку на файл здесь не создать")
    client = make_client(root, scope=Scope("shop", "test"))

    tests = client.get("/api/tests", params={"run": snapshot})
    assert tests.status_code == 200
    assert tests.json()["tests"] == []
    assert any("символическая ссылка" in p for p in tests.json()["problems"])
    assert client.get("/api/test", params={
        "file": "linked-result.json", "run": snapshot}).status_code == 404
    assert "linked: assertion" not in tests.text


def _junction(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_linked_raw_parent_hides_saved_run_before_external_reads(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _configured_project(root)
    snapshot = _publish(root, "api", "inside")
    _history(root, [_entry("api", snapshot)])
    raw_parent = root / ".veha" / "allure" / "shop" / "test" / "api" / "raw"
    raw_parent.rename(tmp_path / "original-raw")

    outside = tmp_path / "outside"
    external_raw = outside / snapshot
    external_raw.mkdir(parents=True)
    source = "00000000-0000-0000-0000-000000000099-attachment.txt"
    result_path = external_raw / "outside-result.json"
    attachment_path = external_raw / source
    result_path.write_text(json.dumps(_result("outside", source, "outside")), encoding="utf-8")
    attachment_path.write_text("OUTSIDE-SECRET", encoding="utf-8")
    try:
        _junction(raw_parent, outside)
    except (OSError, NotImplementedError, ImportError, AttributeError):
        pytest.skip("ссылку на каталог здесь не создать")

    real_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path in (result_path, attachment_path):
            raise AssertionError("API прочитал данные за junction raw")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    client = make_client(root, scope=Scope("shop", "test"))
    graph = client.get("/api/graph")
    assert graph.status_code == 200
    assert graph.json()["savedRuns"] == []
    tests = client.get("/api/tests", params={"run": snapshot})
    attachment = client.get(f"/api/attachment/{source}", params={"run": snapshot})
    assert tests.status_code == 404
    assert attachment.status_code == 404
    assert "outside: assertion" not in tests.text
    assert "OUTSIDE-SECRET" not in attachment.text


def _changing_raw_pair(root: Path):
    raw_a = root / "raw-a"
    raw_b = root / "raw-b"
    raw_a.mkdir()
    raw_b.mkdir()
    source_a = "00000000-0000-0000-0000-000000000001-attachment.txt"
    source_b = "00000000-0000-0000-0000-000000000002-attachment.txt"
    (raw_a / source_a).write_text("from-a", encoding="utf-8")
    (raw_b / source_b).write_text("from-b", encoding="utf-8")
    (raw_a / "a-result.json").write_text(json.dumps(
        _result("a", source_a, "step-a")), encoding="utf-8")
    (raw_b / "b-result.json").write_text(json.dumps(
        _result("b", source_b, "step-b")), encoding="utf-8")
    return raw_a, raw_b


def test_graph_pins_default_raw_when_latest_changes_inside_request(tmp_path, monkeypatch):
    from veha.viz.app import Server

    root = tmp_path / "project"
    _configured_project(root)
    raw_a, raw_b = _changing_raw_pair(root)
    resolves = []

    def changing_latest(self, snapshot: str = ""):
        assert snapshot == ""
        resolves.append(snapshot)
        first = len(resolves) == 1
        return ({"snapshot": "run-a" if first else "run-b", "suite": "api",
                 "label": "API"}, raw_a if first else raw_b)

    monkeypatch.setattr(Server, "selected_run", changing_latest)
    client = make_client(root, scope=Scope("shop", "test"))
    response = client.get("/api/graph")

    assert response.status_code == 200, response.text
    graph = response.json()
    assert graph["run"]["runKey"] == "run-a"
    assert graph["run"]["total"] == 1
    assert [failure["name"] for failure in graph["run"]["failures"]] == ["a"]
    assert resolves == [""]


def test_test_detail_pins_record_steps_and_attachments_to_one_raw(tmp_path, monkeypatch):
    from veha.viz.app import Server

    root = tmp_path / "project"
    _configured_project(root)
    raw_a, raw_b = _changing_raw_pair(root)
    resolves = []

    def changing_latest(self, snapshot: str = ""):
        assert snapshot == ""
        resolves.append(snapshot)
        first = len(resolves) == 1
        return ({"snapshot": "run-a" if first else "run-b"}, raw_a if first else raw_b)

    monkeypatch.setattr(Server, "selected_run", changing_latest)
    client = make_client(root, scope=Scope("shop", "test"))
    response = client.get("/api/test", params={"file": "a-result.json"})

    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["test"]["name"] == "a"
    assert detail["test"]["file"] == "a-result.json"
    assert detail["steps"][0]["name"] == "step-a"
    assert detail["attachments"][0]["source"].endswith("0001-attachment.txt")
    assert "step-b" not in response.text and '"name": "b"' not in response.text
    assert resolves == [""]


def test_disappearing_saved_raw_never_falls_back_to_current_test(tmp_path, monkeypatch):
    """Между выбором saved run и raw_snapshot снимок мог исчезнуть."""
    from veha.viz.app import Server

    root = tmp_path / "project"
    _configured_project(root)
    current = root / "allure-results"
    current.mkdir()
    source = "00000000-0000-0000-0000-000000000001-attachment.txt"
    (current / source).write_text("current", encoding="utf-8")
    (current / "current-result.json").write_text(json.dumps(
        _result("current", source, "current")), encoding="utf-8")

    def disappearing_saved_run(self, snapshot: str = ""):
        assert snapshot == "deleted-run"
        return {"snapshot": snapshot, "suite": "api", "label": "API"}, None

    monkeypatch.setattr(Server, "selected_run", disappearing_saved_run)
    client = make_client(root, scope=Scope("shop", "test"))
    for url, params in (
            ("/api/tests", {"run": "deleted-run"}),
            ("/api/test", {"file": "current-result.json", "run": "deleted-run"}),
            (f"/api/attachment/{source}", {"run": "deleted-run"})):
        response = client.get(url, params=params)
        assert response.status_code == 404, response.text
        assert "current" not in response.text
