"""Атомарная публикация Allure: два снапшота на suite, сбои и восстановление."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from veha import allure_store
from veha.allure_store import AllureStoreError, publish, recover


def _results(root: Path, value: str) -> Path:
    path = root / f"input-{value}"
    path.mkdir()
    (path / "result.json").write_text(value, encoding="utf-8")
    return path


def _generate(value: str, fail: bool = False):
    def generate(raw: Path, report: Path) -> None:
        assert (raw / "result.json").read_text(encoding="utf-8") == value
        previous_marker = raw / "history" / "marker.txt"
        previous = previous_marker.read_text(encoding="utf-8") if previous_marker.exists() else ""
        history = report / "history"
        history.mkdir(exist_ok=True)
        (history / "marker.txt").write_text(previous + value + "\n", encoding="utf-8")
        (report / "index.html").write_text(value, encoding="utf-8")
        if fail:
            raise OSError("генератор упал")
    return generate


def _publish(root: Path, value: str, suite: str = "api", keep: int = 2):
    return publish(root, system="aqa", env="dev", suite=suite,
                   results=_results(root, value), generate=_generate(value), keep=keep)


def _state(root: Path, suite: str = "api") -> dict:
    return json.loads((root / "aqa" / "dev" / suite / "state.json").read_text(encoding="utf-8"))


def _scope(root: Path, suite: str = "api") -> Path:
    return root / "aqa" / "dev" / suite


def _junction(link: Path, target: Path) -> None:
    """Junction на Windows без прав администратора, символическая ссылка на POSIX."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture(autouse=True)
def _portable_latest(tmp_path, monkeypatch):
    """Без права Windows на symlink проверяем алгоритм через файловый адаптер.

    Это не замена POSIX-проверке: настоящие symlink, nginx и права проверяются на Linux.
    Проверку настоящих reparse points адаптер не подменяет.
    """
    probe = tmp_path / "symlink-probe"
    try:
        os.symlink(Path("runs") / "probe", probe, target_is_directory=True)
    except (OSError, NotImplementedError):
        actual_is_link = allure_store._is_link

        def is_link(path: Path) -> bool:
            return actual_is_link(path) or (
                path.is_file() and (path.name == "latest" or path.name.startswith(".latest-")))

        def read_link(path: Path) -> str:
            return path.read_text(encoding="utf-8")

        def create_link(target: Path, path: Path) -> None:
            path.write_text(os.fspath(target), encoding="utf-8")

        monkeypatch.setattr(allure_store, "_is_link", is_link)
        monkeypatch.setattr(allure_store, "_read_link", read_link)
        monkeypatch.setattr(allure_store, "_create_link", create_link)
    else:
        probe.unlink()


def _latest(scope: Path) -> Path:
    return scope / Path(allure_store._read_link(scope / "latest"))


def _run_names(root: Path, suite: str = "api") -> list:
    return sorted(p.name for p in (_scope(root, suite) / "runs").iterdir())


def test_three_publications_keep_the_previous_and_current_snapshot(tmp_path):
    first = _publish(tmp_path, "one")
    second = _publish(tmp_path, "two")
    third = _publish(tmp_path, "three")
    scope = _scope(tmp_path)

    assert first.rotation_status == second.rotation_status == third.rotation_status == "ok"
    assert third.report_status == "published"
    assert _state(tmp_path)["runs"] == [second.run_id, third.run_id]
    assert _run_names(tmp_path) == sorted([second.run_id, third.run_id])
    assert sorted(p.name for p in (scope / "raw").iterdir()) == sorted(
        [second.run_id, third.run_id])
    assert _latest(scope) == scope / "runs" / third.run_id
    assert (_latest(scope) / "index.html").read_text(encoding="utf-8") == "three"
    history = (_latest(scope) / "history" / "marker.txt").read_text(encoding="utf-8")
    assert history == "one\ntwo\nthree\n"
    assert (scope / "raw" / third.run_id / "result.json").read_text(encoding="utf-8") == "three"


def test_interleaved_suites_have_independent_history_raw_and_latest(tmp_path):
    _publish(tmp_path, "a1")
    ui_first = _publish(tmp_path, "u1", suite="ui")
    api_second = _publish(tmp_path, "a2")
    ui_second = _publish(tmp_path, "u2", suite="ui")
    api_third = _publish(tmp_path, "a3")

    ui_scope = _scope(tmp_path, "ui")
    assert _state(tmp_path)["runs"] == [api_second.run_id, api_third.run_id]
    assert _state(tmp_path, "ui")["runs"] == [ui_first.run_id, ui_second.run_id]
    # После третьего api у ui остаются собственные два отчёта и его прежний latest.
    assert len(_run_names(tmp_path, "ui")) == 2
    assert (_latest(ui_scope) / "index.html").read_text(encoding="utf-8") == "u2"
    assert (ui_scope / "raw" / ui_second.run_id / "result.json").read_text(
        encoding="utf-8") == "u2"
    assert len(_run_names(tmp_path, "api")) == 2
    assert (_latest(_scope(tmp_path)) / "history" / "marker.txt").read_text(
        encoding="utf-8") == "a1\na2\na3\n"


def test_generation_failure_preserves_every_published_artifact(tmp_path):
    first = _publish(tmp_path, "one")
    scope = _scope(tmp_path)
    before_state = (scope / "state.json").read_bytes()
    before_history = (_latest(scope) / "history" / "marker.txt").read_bytes()

    with pytest.raises(OSError, match="генератор упал"):
        publish(tmp_path, system="aqa", env="dev", suite="api",
                results=_results(tmp_path, "bad"), generate=_generate("bad", fail=True))

    assert _latest(scope) == scope / "runs" / first.run_id
    assert (scope / "state.json").read_bytes() == before_state
    assert (_latest(scope) / "history" / "marker.txt").read_bytes() == before_history
    assert _run_names(tmp_path) == [first.run_id]
    assert [p.name for p in (scope / "raw").iterdir()] == [first.run_id]
    assert list((scope / ".staging").iterdir()) == []


def test_missing_report_index_is_a_failed_publication(tmp_path):
    first = _publish(tmp_path, "one")

    def no_index(raw: Path, report: Path) -> None:
        pass

    with pytest.raises(AllureStoreError, match="index.html"):
        publish(tmp_path, system="aqa", env="dev", suite="api",
                results=_results(tmp_path, "bad"), generate=no_index)
    assert _run_names(tmp_path) == [first.run_id]
    assert _latest(_scope(tmp_path)) == _scope(tmp_path) / "runs" / first.run_id


def test_latest_rename_failure_does_not_publish_the_new_snapshot(tmp_path, monkeypatch):
    first = _publish(tmp_path, "one")
    scope = _scope(tmp_path)
    before_state = (scope / "state.json").read_bytes()
    real_replace = os.replace

    def fail_latest_swap(src, dst, **kwargs):
        if Path(src).name.startswith(".latest-"):
            raise OSError("rename запрещён")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(allure_store.os, "replace", fail_latest_swap)
    with pytest.raises(OSError, match="rename запрещён"):
        _publish(tmp_path, "two")

    assert _latest(scope) == scope / "runs" / first.run_id
    assert (scope / "state.json").read_bytes() == before_state
    assert _run_names(tmp_path) == [first.run_id]
    assert [p.name for p in (scope / "raw").iterdir()] == [first.run_id]
    assert list((scope / ".staging").iterdir()) == []


def test_prune_failure_is_visible_and_recover_retries_it(tmp_path, monkeypatch):
    first = _publish(tmp_path, "one")
    second = _publish(tmp_path, "two")
    real_rmtree = allure_store.shutil.rmtree

    def fail_old_run(path, *args, **kwargs):
        if Path(path) == _scope(tmp_path) / "runs" / first.run_id:
            raise OSError("каталог занят")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(allure_store.shutil, "rmtree", fail_old_run)
    third = _publish(tmp_path, "three")
    assert third.report_status == "published"
    assert third.rotation_status == "failed" and "каталог занят" in third.rotation_error
    assert (_latest(_scope(tmp_path)) / "index.html").read_text(encoding="utf-8") == "three"
    assert first.run_id in _run_names(tmp_path)

    monkeypatch.setattr(allure_store.shutil, "rmtree", real_rmtree)
    recovered = recover(tmp_path, system="aqa", env="dev", suite="api")
    assert recovered.report_status == "recovered" and recovered.rotation_status == "ok"
    assert recovered.runs == (second.run_id, third.run_id)
    assert _run_names(tmp_path) == sorted([second.run_id, third.run_id])


def test_failure_after_raw_removal_is_also_retryable(tmp_path, monkeypatch):
    first = _publish(tmp_path, "one")
    _publish(tmp_path, "two")
    real_rmtree = allure_store.shutil.rmtree

    def fail_old_raw(path, *args, **kwargs):
        if Path(path) == _scope(tmp_path) / "raw" / first.run_id:
            raise OSError("raw занят")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(allure_store.shutil, "rmtree", fail_old_raw)
    third = _publish(tmp_path, "three")
    assert third.rotation_status == "failed"
    assert not (_scope(tmp_path) / "runs" / first.run_id).exists()
    assert (_scope(tmp_path) / "raw" / first.run_id).is_dir()

    monkeypatch.setattr(allure_store.shutil, "rmtree", real_rmtree)
    recovered = recover(tmp_path, system="aqa", env="dev", suite="api")
    assert recovered.rotation_status == "ok"
    assert not (_scope(tmp_path) / "raw" / first.run_id).exists()


def test_crash_between_latest_and_state_is_recovered_from_latest(tmp_path, monkeypatch):
    first = _publish(tmp_path, "one")
    real_write_state = allure_store._write_state

    def fail_state(*args, **kwargs):
        raise OSError("state не записан")

    monkeypatch.setattr(allure_store, "_write_state", fail_state)
    second = _publish(tmp_path, "two")
    assert second.report_status == "published" and second.rotation_status == "failed"
    assert _state(tmp_path)["runs"] == [first.run_id]
    assert _latest(_scope(tmp_path)) == _scope(tmp_path) / "runs" / second.run_id

    monkeypatch.setattr(allure_store, "_write_state", real_write_state)
    recovered = recover(tmp_path, system="aqa", env="dev", suite="api", keep=1)
    assert recovered.runs == (second.run_id,)
    assert _state(tmp_path)["runs"] == [second.run_id]
    assert _run_names(tmp_path) == [second.run_id]


def test_unpublished_run_does_not_replace_history_on_recovery(tmp_path):
    current = _publish(tmp_path, "one")
    orphan = _scope(tmp_path) / "runs" / "29990101T000000.000000Z-orphan"
    orphan.mkdir()
    (orphan / "index.html").write_text("orphan", encoding="utf-8")
    (orphan / allure_store.MANIFEST).write_text(json.dumps({
        "version": 1, "system": "aqa", "env": "dev", "suite": "api",
        "run_id": orphan.name,
    }), encoding="utf-8")

    recovered = recover(tmp_path, system="aqa", env="dev", suite="api", keep=1)
    assert recovered.runs == (current.run_id,)
    assert _latest(_scope(tmp_path)) == _scope(tmp_path) / "runs" / current.run_id
    assert orphan.is_dir()                       # не опубликован, но и не удалён молча


def test_legacy_latest_directory_is_refused_without_migration(tmp_path):
    scope = _scope(tmp_path)
    (scope / "latest").mkdir(parents=True)
    (scope / "latest" / "index.html").write_text("legacy", encoding="utf-8")

    with pytest.raises(AllureStoreError, match="не мигрируется"):
        _publish(tmp_path, "one")
    assert (scope / "latest" / "index.html").read_text(encoding="utf-8") == "legacy"
    assert not (scope / "state.json").exists()


@pytest.mark.parametrize("value", ["", ".", "..", "a..b", "a/b", "a\\b", "/tmp/x",
                                   "C:/x", "\\\\host\\share", "a b", "x\ny"])
def test_unsafe_scope_segments_are_rejected(tmp_path, value):
    results = _results(tmp_path, "safe")
    with pytest.raises(ValueError, match="безопасный сегмент"):
        publish(tmp_path, system=value, env="dev", suite="api",
                results=results, generate=_generate("safe"))
    with pytest.raises(ValueError, match="безопасный сегмент"):
        recover(tmp_path, system=value, env="dev", suite="api")


def test_symlink_in_raw_results_is_rejected_and_target_is_untouched(tmp_path):
    current = _publish(tmp_path, "one")
    results = _results(tmp_path, "two")
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("не трогать", encoding="utf-8")
    try:
        os.symlink(foreign, results / "escape.txt")
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки здесь не создать")

    with pytest.raises(AllureStoreError, match="символическая ссылка"):
        publish(tmp_path, system="aqa", env="dev", suite="api",
                results=results, generate=_generate("two"))
    assert foreign.read_text(encoding="utf-8") == "не трогать"
    assert _latest(_scope(tmp_path)) == _scope(tmp_path) / "runs" / current.run_id
    assert _run_names(tmp_path) == [current.run_id]


def test_linked_scope_directory_is_rejected_before_writing(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    try:
        _junction(root / "aqa", foreign)
    except (OSError, NotImplementedError, ImportError, AttributeError):
        pytest.skip("ссылку на каталог здесь не создать")

    with pytest.raises(AllureStoreError, match="обычный каталог"):
        publish(root, system="aqa", env="dev", suite="api",
                results=_results(tmp_path, "one"), generate=_generate("one"))
    assert list(foreign.iterdir()) == []


def test_linked_raw_directory_is_rejected_without_reading_target(tmp_path):
    current = _publish(tmp_path, "one")
    results = _results(tmp_path, "two")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "secret.txt").write_text("не читать", encoding="utf-8")
    try:
        _junction(results / "escape", foreign)
    except (OSError, NotImplementedError, ImportError, AttributeError):
        pytest.skip("ссылку на каталог здесь не создать")

    with pytest.raises(AllureStoreError, match="символическая ссылка"):
        publish(tmp_path, system="aqa", env="dev", suite="api",
                results=results, generate=_generate("two"))
    assert (foreign / "secret.txt").read_text(encoding="utf-8") == "не читать"
    assert _run_names(tmp_path) == [current.run_id]


def test_linked_report_directory_is_rejected_before_traversal(tmp_path, monkeypatch):
    current = _publish(tmp_path, "one")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    foreign_file = foreign / "secret.txt"
    foreign_file.write_text("не читать", encoding="utf-8")

    def generate(raw: Path, report: Path) -> None:
        (report / "index.html").write_text("two", encoding="utf-8")
        try:
            _junction(report / "escape", foreign)
        except (OSError, NotImplementedError, ImportError, AttributeError):
            pytest.skip("ссылку на каталог здесь не создать")

    real_lstat = Path.lstat

    def guarded_lstat(path):
        if path == foreign_file:
            raise AssertionError("обход зашёл внутрь связанного каталога")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", guarded_lstat)
    with pytest.raises(AllureStoreError, match="символическая ссылка"):
        publish(tmp_path, system="aqa", env="dev", suite="api",
                results=_results(tmp_path, "two"), generate=generate)
    assert foreign_file.read_text(encoding="utf-8") == "не читать"
    assert _run_names(tmp_path) == [current.run_id]


def test_latest_symlink_escape_is_refused(tmp_path):
    _publish(tmp_path, "one")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "index.html").write_text("foreign", encoding="utf-8")
    latest = _scope(tmp_path) / "latest"
    latest.unlink()
    allure_store._create_link(foreign, latest)

    with pytest.raises(AllureStoreError, match="вне управляемой раскладки"):
        recover(tmp_path, system="aqa", env="dev", suite="api")
    assert (foreign / "index.html").read_text(encoding="utf-8") == "foreign"
    assert allure_store._is_link(latest)


@pytest.mark.parametrize("keep", [0, -1, True, 1.5, "2"])
def test_bad_retention_is_rejected_before_writing(tmp_path, keep):
    with pytest.raises(ValueError, match="keep"):
        _publish(tmp_path, "one", keep=keep)
    assert not (tmp_path / "aqa").exists()
