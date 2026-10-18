"""Разбор вывода pytest -v: обычный и под xdist.

Строки ниже — настоящий вывод pytest 9.1.1 и pytest-xdist 3.8.0, снятый 2026-10-06 на
наборе из семи тестов (параметр с пробелом, падение, ошибка в teardown, пропуск, отбор
по метке). Не придуманы по памяти: прошлый разбор был написан по памяти, и под xdist у
него `done` и `total` оставались нулями.
"""

from __future__ import annotations

import shutil

import pytest

from veha.runner import RunState

XDIST = """\
============================= test session starts =============================
created: 2/2 workers
2 workers [7 items]

scheduling tests via LoadScheduling

test_a.py::test_ok
test_a.py::test_param[POST /orders]
[gw1] [ 14%] PASSED test_a.py::test_ok
[gw0] [ 28%] PASSED test_a.py::test_param[POST /orders]
test_a.py::test_fail
test_a.py::test_param[GET /x]
[gw0] [ 42%] PASSED test_a.py::test_param[GET /x]
test_a.py::test_deselected
[gw0] [ 57%] PASSED test_a.py::test_deselected
test_a.py::test_skip
[gw0] [ 71%] SKIPPED test_a.py::test_skip
[gw1] [ 85%] FAILED test_a.py::test_fail
test_a.py::test_teardown_error
[gw1] [100%] PASSED test_a.py::test_teardown_error
[gw1] [100%] ERROR test_a.py::test_teardown_error
=========================== short test summary info ===========================
FAILED test_a.py::test_fail - assert False
ERROR test_a.py::test_teardown_error - RuntimeError: teardown boom
========= 1 failed, 5 passed, 1 skipped, 2 warnings, 1 error in 2.20s =========
"""

DESELECTED = """\
collecting ... collected 7 items / 1 deselected / 6 selected

test_a.py::test_param[POST /orders] PASSED                               [ 16%]
test_a.py::test_param[GET /x] PASSED                                     [ 33%]
test_a.py::test_ok PASSED                                                [ 50%]
test_a.py::test_fail FAILED                                              [ 66%]
test_a.py::test_teardown_error PASSED                                    [ 83%]
test_a.py::test_teardown_error ERROR                                     [ 83%]
test_a.py::test_skip SKIPPED (x)                                         [100%]
======== 1 failed, 4 passed, 1 skipped, 1 deselected, 1 error in 0.11s ========
"""

STOP_X = """\
collecting ... collected 7 items

test_a.py::test_param[POST /orders] PASSED                               [ 14%]
test_a.py::test_param[GET /x] PASSED                                     [ 28%]
test_a.py::test_ok PASSED                                                [ 42%]
test_a.py::test_fail FAILED                                              [ 57%]
=========================== short test summary info ===========================
FAILED test_a.py::test_fail - assert False
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
========================= 1 failed, 3 passed in 0.10s =========================
"""

MAXFAIL = """\
collecting ... collected 7 items

test_a.py::test_param[POST /orders] PASSED                               [ 14%]
test_a.py::test_param[GET /x] PASSED                                     [ 28%]
test_a.py::test_ok PASSED                                                [ 42%]
test_a.py::test_fail FAILED                                              [ 57%]
test_a.py::test_teardown_error PASSED                                    [ 71%]
test_a.py::test_teardown_error ERROR                                     [ 71%]
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 2 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
==================== 1 failed, 4 passed, 1 error in 0.11s =====================
"""

XDIST_X = """\
created: 2/2 workers
2 workers [7 items]
[gw1] [ 14%] PASSED test_a.py::test_ok
[gw0] [ 28%] PASSED test_a.py::test_param[POST /orders]
[gw1] [ 85%] FAILED test_a.py::test_fail
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
!!!!!!!!!!!! xdist.dsession.Interrupted: stopping after 1 failures !!!!!!!!!!!!
=================== 1 failed, 4 passed, 1 skipped in 2.26s ====================
"""

COLLECTION_ERROR = """\
collecting ... collected 7 items / 1 error
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
========================= 1 warning, 1 error in 0.16s =========================
"""


def run_of(text: str) -> RunState:
    st = RunState(id="x", suite="s", label="s", args=[], started=0.0)
    for line in text.splitlines():
        st.feed(line + "\n")
    return st


def test_xdist_progress_and_completeness():
    st = run_of(XDIST)
    assert st.total == 7 and st.done == 7 and st.pct == 100
    # счётчики — из итоговой строки: pytest считает тест с ошибкой teardown прошедшим и
    # отдельно — ошибку (`5 passed, 1 error` на 7 тестов); построчно сломан один тест
    assert (st.passed, st.failed, st.broken, st.skipped) == (5, 1, 1, 1)
    assert "test_a.py::test_teardown_error" in st.failures
    assert st.complete and st.summary.startswith("1 failed, 5 passed")


def test_deselected_collection_counts_only_selected():
    st = run_of(DESELECTED)
    assert st.total == 6 and st.done == 6 and st.complete


def test_teardown_error_after_passed_is_broken_once():
    st = run_of(DESELECTED)
    assert st.results["test_a.py::test_teardown_error"] == "ERROR"
    assert st.broken == 1 and "test_a.py::test_teardown_error" in st.failures
    assert st.touched.count("test_a.py::test_teardown_error") == 1


def test_parameter_with_a_space_keeps_its_nodeid():
    for text in (XDIST, DESELECTED):
        assert "test_a.py::test_param[POST /orders]" in run_of(text).results


@pytest.mark.parametrize("text,reason", [
    (STOP_X, "stopping after 1 failures"),
    (MAXFAIL, "stopping after 2 failures"),
    (XDIST_X, "stopping after 1 failures"),
    (COLLECTION_ERROR, "Interrupted: 1 error during collection"),
])
def test_interrupted_runs_are_not_complete(text, reason):
    st = run_of(text)
    assert st.summary and not st.complete and st.interrupted.startswith(reason)


def test_run_without_summary_is_not_complete():
    st = run_of(DESELECTED.rsplit("\n", 2)[0])        # итоговую строку отрезали
    assert st.done == 6 and not st.complete


# Настоящий вывод `pytest -v -rfE` (pytest 9.1.1) с сообщениями, в которых есть
# `ERROR`, `PASSED` и `::`. В разделах ERRORS/FAILURES и в краткой сводке это пересказ,
# а не результаты; прежний разбор превращал их в фантомные `done` и `broken`.
RFE = """\
collecting ... collected 5 items

test_b.py::test_server_error FAILED                                      [ 20%]
test_b.py::test_setup_error ERROR                                        [ 40%]
test_b.py::test_ok PASSED                                                [ 60%]
test_b.py::test_param[a b] PASSED                                        [ 80%]
test_b.py::test_param[c] PASSED                                          [100%]

=================================== ERRORS ====================================
_____________________ ERROR at setup of test_setup_error ______________________
>       raise RuntimeError("setup ERROR: PASSED tests/x.py::fake")
E       RuntimeError: setup ERROR: PASSED tests/x.py::fake
================================== FAILURES ===================================
>       assert False, "500 INTERNAL SERVER ERROR PASSED"
E       AssertionError: 500 INTERNAL SERVER ERROR PASSED
=========================== short test summary info ===========================
FAILED test_b.py::test_server_error - AssertionError: 500 INTERNAL SERVER ERR...
ERROR test_b.py::test_setup_error - RuntimeError: setup ERROR: PASSED tests/x...
==================== 1 failed, 3 passed, 1 error in 0.10s =====================
"""

# Живой вывод `pytest -v -rfE -n 2 -x` (pytest-xdist 3.8.0): обе строки прерывания
# обрамлены `!!!`.
RFE_XDIST_X = """\
2 workers [5 items]
[gw1] [ 20%] PASSED test_b.py::test_ok
[gw1] [ 40%] PASSED test_b.py::test_param[a b]
[gw1] [ 60%] PASSED test_b.py::test_param[c]
[gw0] [ 80%] FAILED test_b.py::test_server_error
================================== FAILURES ===================================
E       AssertionError: 500 INTERNAL SERVER ERROR PASSED
=========================== short test summary info ===========================
FAILED test_b.py::test_server_error - AssertionError: 500 INTERNAL SERVER ERR...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
!!!!!!!!!!!! xdist.dsession.Interrupted: stopping after 1 failures !!!!!!!!!!!!
========================= 1 failed, 3 passed in 1.75s =========================
"""


def test_short_summary_and_tracebacks_are_not_results():
    st = run_of(RFE)
    assert set(st.results) == {
        "test_b.py::test_server_error", "test_b.py::test_setup_error", "test_b.py::test_ok",
        "test_b.py::test_param[a b]", "test_b.py::test_param[c]"}
    assert (st.done, st.passed, st.failed, st.broken) == (5, 3, 1, 1)
    assert st.failures == ["test_b.py::test_server_error", "test_b.py::test_setup_error"]
    assert st.complete and st.summary == "1 failed, 3 passed, 1 error"


def test_xdist_interrupt_lines_are_both_recognised():
    from veha.runner import parse_interrupt

    st = run_of(RFE_XDIST_X)
    assert st.done == 4 and not st.complete
    assert st.interrupted == "stopping after 1 failures"
    lines = [ln for ln in RFE_XDIST_X.splitlines() if ln.startswith("!!!")]
    assert len(lines) == 2 and all(parse_interrupt(ln) for ln in lines)
    assert parse_interrupt(lines[1]).startswith("xdist.dsession.Interrupted")


@pytest.mark.parametrize("line", [
    "FAILED test_b.py::test_x - AssertionError: 500 INTERNAL SERVER ERROR",
    "ERROR test_b.py::test_y - RuntimeError: PASSED tests/x.py::fake",
    "PASSED a::b",
    "E       RuntimeError: setup ERROR: PASSED tests/x.py::fake",
    "a::b PASSED " * 300 + "zzz",                   # длиннее 2 КБ — не результат
])
def test_lines_that_are_not_results(line):
    from veha.runner import parse_result

    assert parse_result(line) is None


# ------------------------------------------------- сквозной прогон под xdist

SUITE = '''import pytest

@pytest.mark.parametrize("p", ["POST /orders", "GET /x"])
def test_param(p):
    pass

def test_ok():
    pass
'''


def test_real_xdist_run_records_coverage(tmp_path, demo_root):
    """Настоящий `pytest -n 2` через раннер: прогон завершён, и срез покрытия пишется."""
    pytest.importorskip("xdist")
    import json
    import os
    import time

    from tests.conftest import artifact_path, make_client

    root = tmp_path / "xd"
    root.mkdir()
    (root / "t_x.py").write_text(SUITE, encoding="utf-8")
    shutil.copy(demo_root / "coverage.json", root / "coverage.json")
    (root / "atlas.yaml").write_text(
        'runner:\n  suites:\n    all:\n      label: all\n      full: true\n'
        '      args: ["-n", "2", "t_x.py"]\n', encoding="utf-8")
    c = make_client(root, readonly=False)
    srv = c.app.state.veha
    assert c.post("/api/runner/start", json={"suite": "all"}).status_code == 200
    future = time.time() + 120          # coverage.json «записан этим прогоном»
    os.utime(root / "coverage.json", (future, future))
    srv.runner.wait(120)
    run = srv.runner.status()["run"]
    assert run["status"] == "done" and run["complete"], run["tail"][-5:]
    assert (run["total"], run["done"], run["passed"]) == (3, 3, 3)

    hist = artifact_path(root, "runner.history")
    end = time.time() + 10
    while not hist.is_file() and time.time() < end:
        time.sleep(0.1)
    last = json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]
    assert last["coverage"].get("avg") == 66.05


# ------------------------------------------------- повторы pytest-rerunfailures
# Настоящий вывод pytest 9.1.1 с pytest-rerunfailures 16.7 (`--reruns=1`), снятый
# 2026-10-06: один тест падает с первой попытки и проходит со второй, другой падает оба
# раза. Строки `RERUN` — не результат: итог теста — следующая строка по тому же nodeid.
RERUNS = """\
collecting ... collected 3 items
test_r.py::test_ok PASSED                                                [ 33%]
test_r.py::test_flaky_then_passes RERUN                                  [ 66%]
test_r.py::test_flaky_then_passes PASSED                                 [ 66%]
test_r.py::test_always_fails RERUN                                       [100%]
test_r.py::test_always_fails FAILED                                      [100%]
================================== FAILURES ===================================
E       AssertionError: 500 INTERNAL SERVER ERROR
=========================== short test summary info ===========================
FAILED test_r.py::test_always_fails - AssertionError: 500 INTERNAL SERVER ERROR
==================== 1 failed, 2 passed, 2 rerun in 0.10s =====================
"""

RERUNS_XDIST = """\
created: 2/2 workers
2 workers [3 items]
[gw0] [ 33%] PASSED test_r.py::test_ok
[gw1] [ 66%] RERUN test_r.py::test_flaky_then_passes
[gw0] [100%] RERUN test_r.py::test_always_fails
[gw1] [100%] PASSED test_r.py::test_flaky_then_passes
[gw0] [100%] FAILED test_r.py::test_always_fails
================================== FAILURES ===================================
=========================== short test summary info ===========================
FAILED test_r.py::test_always_fails - AssertionError: 500 INTERNAL SERVER ERROR
==================== 1 failed, 2 passed, 2 rerun in 1.56s =====================
"""

# `-q` в addopts проекта вместе с нашим `-v` — уровень 0: строк на каждый тест нет.
QUIET = """\
collected 3 items
test_r.py .FF                                                            [100%]
================================== FAILURES ===================================
=========================== short test summary info ===========================
FAILED test_r.py::test_flaky_then_passes - AssertionError: first attempt fails
FAILED test_r.py::test_always_fails - AssertionError: 500 INTERNAL SERVER ERROR
========================= 2 failed, 1 passed in 0.10s =========================
"""

QUIET_RERUNS = """\
collected 3 items
test_r.py .R.RF                                                          [100%]
================================== FAILURES ===================================
=========================== short test summary info ===========================
FAILED test_r.py::test_always_fails - AssertionError: 500 INTERNAL SERVER ERROR
==================== 1 failed, 2 passed, 2 rerun in 0.10s =====================
"""

# `-rP`: после рамок FAILURES и PASSES идёт захваченный вывод теста, в котором тест сам
# напечатал строки, похожие на результаты.
CAPTURED = """\
collecting ... collected 2 items
test_r.py::test_ok PASSED                                                [ 50%]
test_r.py::test_prints_fake_result_and_fails FAILED                      [100%]
================================== FAILURES ===================================
______________________ test_prints_fake_result_and_fails ______________________
E       AssertionError: boom
---------------------------- Captured stdout call -----------------------------
other.py::test_fake PASSED
[gw0] [ 50%] PASSED other.py::test_fake_xdist
=================================== PASSES ====================================
___________________________________ test_ok ___________________________________
---------------------------- Captured stdout call -----------------------------
other.py::test_passing_noise PASSED
========================= 1 failed, 1 passed in 0.09s =========================
"""


@pytest.mark.parametrize("text", [RERUNS, RERUNS_XDIST])
def test_rerun_lines_are_not_results(text):
    st = run_of(text)
    assert st.total == 3 and st.done == 3
    assert st.results == {"test_r.py::test_ok": "PASSED",
                          "test_r.py::test_flaky_then_passes": "PASSED",
                          "test_r.py::test_always_fails": "FAILED"}
    assert (st.passed, st.failed, st.rerun) == (2, 1, 2)
    assert st.complete and st.summary == "1 failed, 2 passed, 2 rerun"


@pytest.mark.parametrize("text,counts", [
    (QUIET, (1, 2, 0)),
    (QUIET_RERUNS, (2, 1, 2)),
])
def test_quiet_output_counts_come_from_the_summary(text, counts):
    """Строк на каждый тест нет — счётчики и `done` берутся из итоговой строки."""
    st = run_of(text)
    assert st.results == {}
    assert (st.passed, st.failed, st.rerun) == counts and st.done == 3 and st.complete


def test_captured_output_after_any_banner_is_not_a_result():
    st = run_of(CAPTURED)
    assert set(st.results) == {"test_r.py::test_ok",
                               "test_r.py::test_prints_fake_result_and_fails"}
    assert st.done == 2 and (st.passed, st.failed) == (1, 1)


def test_parse_counts():
    from veha.runner import parse_counts

    assert parse_counts("1 failed, 2 passed, 2 rerun, 3 warnings, 1 error") == {
        "failed": 1, "passed": 2, "rerun": 2, "broken": 1}
    assert parse_counts("2 xfailed, 1 xpassed, 4 deselected") == {
        "xfailed": 2, "xpassed": 1, "deselected": 4}
    assert parse_counts("no tests ran") == {}


@pytest.mark.parametrize("code,passed,run_status,complete,status,in_note", [
    (0, 3, "done", True, "checked", "3 прошло"),
    (0, 0, "done", True, "empty", "все пропущены"),               # всё пропущено
    (1, 2, "done", True, "failed", "1 упало"),
    (2, 1, "done", False, "failed", "прогон неполный"),          # -x: пока failed
    (3, 0, "error", False, "partial", "ошибка pytest или вызова (код 3)"),
    (4, 0, "error", False, "partial", "ошибка pytest или вызова (код 4)"),
    (5, 0, "done", True, "empty", "тестов не собрано"),
    (1, 0, "aborted", False, "partial", "оборван"),               # os._exit без итога
    (None, 0, "stopped", False, "partial", "остановлен вручную"),
    (-9, 0, "error", False, "partial", "неизвестный код выхода -9"),
])
def test_mr_verdict_comes_from_the_exit_code(code, passed, run_status, complete, status,
                                             in_note):
    from veha.viz.app import mr_verdict

    entry = {"returncode": code, "status": run_status, "complete": complete, "label": "s",
             "passed": passed, "failed": 1 if status == "failed" else 0}
    got, note = mr_verdict(entry)
    assert got == status and in_note in note, note


def test_only_exit_code_one_and_two_mean_failed():
    from veha.viz.app import mr_verdict

    for code in range(-1, 7):
        entry = {"returncode": code, "status": "done", "complete": True, "label": "s",
                 "passed": 1, "failed": 1}
        assert (mr_verdict(entry)[0] == "failed") == (code in (1, 2)), code


# ------------------------------------------------------------------


def test_run_without_summary_is_aborted_not_done(small_root, monkeypatch):
    """pytest-timeout в режиме thread завершает процесс через `os._exit(1)`: итоговой
    строки нет. В истории — «оборван», а не «завершён»; отметка МР — частичная."""
    import io
    import json
    import time

    from tests.conftest import artifact_path, make_client
    from veha import runner

    class NoSummary:
        pid = None

        def __init__(self, args, **kwargs):
            self.stdout = io.StringIO(
                "collecting ... collected 2 items\n"
                "t.py::test_a PASSED                                      [ 50%]\n"
                "+++++++++++++++++++++++++++++++++++ Timeout +++++++++++++++++++++++++++++++++++\n")

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(runner.subprocess, "Popen", NoSummary)
    c = make_client(small_root, readonly=False)
    hist = artifact_path(small_root, "runner.history")
    before = hist.read_bytes()
    assert c.post("/api/runner/start", json={"suite": "smoke", "mr": 121}).status_code == 200
    end = time.time() + 10
    while hist.read_bytes() == before and time.time() < end:
        time.sleep(0.05)
    last = json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]
    assert last["status"] == "aborted" and last["statusLabel"] == "оборван"
    assert last["coverage"] == {"note": "покрытие этим прогоном не записано"}
    mark = json.loads((small_root / "live" / "mr_checks.json").read_text(
        encoding="utf-8"))["checks"]["121"]
    assert mark["status"] == "partial" and "оборван" in mark["note"]


# ------------------------------------------------------------------

# Настоящий вывод `pytest -v -rP` без падений: первый баннер после результатов — PASSES,
# под ним в захваченном выводе тест напечатал строку, похожую на результат.
PASSES_FIRST = """\
collecting ... collected 1 item
test_r.py::test_ok PASSED                                                [100%]
=================================== PASSES ====================================
___________________________________ test_ok ___________________________________
---------------------------- Captured stdout call -----------------------------
other.py::test_noise PASSED
============================== 1 passed in 0.01s ==============================
"""


def test_passes_banner_first_closes_parsing():
    st = run_of(PASSES_FIRST)
    assert st.results == {"test_r.py::test_ok": "PASSED"}
    assert st.done == 1 and st.complete


QUIET_SUITE = """import pytest

def test_one():
    pass

def test_two():
    assert False, "500 INTERNAL SERVER ERROR"
"""


def test_quiet_project_run_marks_the_mr_failed(tmp_path):
    """Живой прогон: в addopts набора `-q`, один тест падает. Строк на тест нет, но МР
    получает `failed`, а счётчики истории совпадают с итоговой строкой pytest."""
    import json
    import time

    from tests.conftest import artifact_path, make_client

    root = tmp_path / "quiet"
    root.mkdir()
    (root / "t_q.py").write_text(QUIET_SUITE, encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    (root / "atlas.yaml").write_text(
        "live:\n  snapshot_dir: live\n"
        "runner:\n  suites:\n    all:\n      label: all\n      args: [\"t_q.py\"]\n",
        encoding="utf-8")
    c = make_client(root, readonly=False)
    srv = c.app.state.veha
    assert c.post("/api/runner/start", json={"suite": "all", "mr": 121}).status_code == 200
    srv.runner.wait(120)
    run = srv.runner.status()["run"]
    assert run["returncode"] == 1 and run["complete"], run["tail"][-5:]
    assert run["summary"] == "1 failed, 1 passed"
    assert (run["passed"], run["failed"], run["done"]) == (1, 1, 2)

    checks = root / "live" / "mr_checks.json"
    end = time.time() + 10
    while not checks.is_file() and time.time() < end:
        time.sleep(0.1)
    mark = json.loads(checks.read_text(encoding="utf-8"))["checks"]["121"]
    assert mark["status"] == "failed" and "1 упало" in mark["note"]

    hist = artifact_path(root, "runner.history")
    end = time.time() + 10
    while not hist.is_file() and time.time() < end:
        time.sleep(0.1)
    last = json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]
    assert (last["passed"], last["failed"], last["returncode"]) == (1, 1, 1)

