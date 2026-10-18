"""Остановка и таймаут убивают всё дерево процессов, и замок отпускается.

Настоящие процессы, без подделок: тест pytest запускает «внука», который пережил бы
родителя (сервис, браузер). Если сервер убивает только pytest, внук держит пайп, прогон
висит, а стенд остаётся занятым.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from tests.conftest import artifact_path, make_client

HANG = '''import pathlib, subprocess, sys, time

def test_hang():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pathlib.Path("child.pid").write_text(str(child.pid))
    time.sleep(120)
'''

ATLAS = '''runner:
  suites:
    hang:
      label: hang
      args: ["t_hang.py"]
'''


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259                          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def hang_root(tmp_path):
    root = tmp_path / "hang"
    root.mkdir()
    (root / "atlas.yaml").write_text(ATLAS, encoding="utf-8")
    (root / "t_hang.py").write_text(HANG, encoding="utf-8")
    return root


def _child_pid(root, timeout=60):
    pid_file = root / "child.pid"
    assert _wait(pid_file.is_file, timeout), "тест не успел запустить внука"
    pid = int(pid_file.read_text())
    assert pid_alive(pid)
    return pid


def test_stop_kills_the_tree_and_writes_a_stopped_run(hang_root):
    c = make_client(hang_root, readonly=False)
    srv = c.app.state.veha
    assert c.post("/api/runner/start", json={"suite": "hang"}).status_code == 200
    child = _child_pid(hang_root)

    assert c.post("/api/runner/stop").json()["stopped"] is True
    srv.runner.wait(20)
    assert not srv.runner.busy
    assert _wait(lambda: not pid_alive(child), 10), "внук пережил остановку"

    hist = artifact_path(hang_root, "runner.history")
    assert _wait(hist.is_file, 10)
    last = json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]
    assert last["status"] == "stopped" and last["statusLabel"] == "остановлен"
    assert last["coverage"] == {"note": "покрытие этим прогоном не записано"}
    # замок свободен: следующий прогон стартует
    assert c.post("/api/runner/start", json={"suite": "hang"}).status_code == 200
    c.post("/api/runner/stop")
    srv.runner.wait(20)


def test_rerun_timeout_kills_the_tree_and_a_second_rerun_does_not_block(hang_root,
                                                                         monkeypatch):
    from veha.viz import app as app_module

    monkeypatch.setattr(app_module, "RERUN_TIMEOUT", 15)
    c = make_client(hang_root, readonly=False)
    srv = c.app.state.veha
    first = {}

    def run_first():
        first["r"] = c.post("/api/rerun", json={"nodeid": "t_hang.py::test_hang"})

    th = threading.Thread(target=run_first)
    th.start()
    child = _child_pid(hang_root)

    t0 = time.time()
    second = c.post("/api/rerun", json={"nodeid": "t_hang.py::test_hang"})
    assert second.status_code == 409 and time.time() - t0 < 5, "второй перезапуск ждал"

    th.join(60)
    assert first["r"].status_code == 504
    assert not srv.rerun_lock.locked()
    assert _wait(lambda: not pid_alive(child), 10), "внук пережил таймаут"


# ------------------------------------------------------------------

ORPHAN = '''import pathlib, subprocess, sys

def test_leaves_a_grandchild():
    # внук наследует stdout pytest и переживает его: конец пайпа наступит не скоро
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pathlib.Path("child.pid").write_text(str(child.pid))
'''


def _kill_pid(pid):
    if not pid_alive(pid):
        return
    if os.name == "nt":
        import subprocess

        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        import signal

        os.kill(pid, signal.SIGKILL)


def test_rerun_ends_when_pytest_ends_not_when_the_pipe_closes(hang_root, monkeypatch):
    """Тест прошёл и оставил внука, который держит **пайп раннера**. Перезапуск отвечает
    сразу, без ложного 504, и замок свободен.

    `-s` в `pytest.ini` обязателен: с захватом вывода внук наследует файл захвата pytest,
    а не пайп, и тест не отличил бы ожидание смерти процесса от ожидания конца пайпа
    (`communicate`). Проверено мутацией: с `communicate` тест падает по таймауту.
    """
    from veha.viz import app as app_module

    monkeypatch.setattr(app_module, "RERUN_TIMEOUT", 20)
    (hang_root / "pytest.ini").write_text("[pytest]\naddopts = -s\n", encoding="utf-8")
    (hang_root / "t_orphan.py").write_text(ORPHAN, encoding="utf-8")
    c = make_client(hang_root, readonly=False)
    srv = c.app.state.veha
    t0 = time.time()
    r = c.post("/api/rerun", json={"nodeid": "t_orphan.py::test_leaves_a_grandchild"})
    took = time.time() - t0
    child = int((hang_root / "child.pid").read_text())
    try:
        assert r.status_code == 200 and r.json()["passed"] is True, r.text
        assert took < 10, f"перезапуск ждал конца пайпа: {took:.1f} с"
        assert not srv.rerun_lock.locked()
    finally:
        _kill_pid(child)


# ------------------------------------------------------------------


def test_exit_signal_kills_an_inflight_rerun(hang_root):
    """Ctrl+C во время синхронного перезапуска: uvicorn ждал бы конца запроса до десяти
    минут, а lifespan не срабатывал. Обработчик выхода сначала убивает деревья — запрос
    отвечает сразу, внук мёртв, новых запусков нет."""
    import signal
    import threading

    from veha import cli

    c = make_client(hang_root, readonly=False)
    srv = c.app.state.veha
    result = {}
    th = threading.Thread(target=lambda: result.setdefault("r", c.post(
        "/api/rerun", json={"nodeid": "t_hang.py::test_hang"})))
    th.start()
    child = _child_pid(hang_root)

    t0 = time.time()
    cli.make_server(c.app, 8770).handle_exit(signal.SIGINT, None)
    th.join(30)
    assert not th.is_alive() and time.time() - t0 < 30
    assert result["r"].status_code == 200 and result["r"].json()["passed"] is False
    assert _wait(lambda: not pid_alive(child), 10), "внук пережил остановку сервера"
    assert srv.closing
    assert c.post("/api/rerun", json={"nodeid": "t_hang.py::test_hang"}).status_code == 409
    assert c.post("/api/runner/start", json={"suite": "hang"}).status_code == 409


def test_process_registered_after_shutdown_is_killed_at_once(hang_root):
    """Окно между запуском процесса и его регистрацией закрыто: процесс, пришедший к
    регистрации после начала остановки, убивается сразу."""
    import subprocess
    import sys

    from veha import runner

    c = make_client(hang_root, readonly=False)
    srv = c.app.state.veha
    srv.shutdown()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            **runner.popen_group_kwargs())
    srv.remember_rerun(proc)
    assert _wait(lambda: proc.poll() is not None, 10), "процесс пережил регистрацию"


# ------------------------------------------------------------------


def test_missing_taskkill_falls_back_and_says_so(hang_root, monkeypatch):
    """Дерево убить нечем — убивается pytest, прогон «остановлен», причина названа; ни
    500, ни вечного «останавливается»."""
    from veha import runner

    if os.name == "nt":
        monkeypatch.setattr(runner, "taskkill_path", lambda: r"C:\nowhere\taskkill.exe")  # publish-ok: заведомо несуществующий путь для проверки отказа taskkill
    else:
        def deny(pid, sig):
            raise PermissionError("denied by test")
        monkeypatch.setattr(os, "killpg", deny)
    c = make_client(hang_root, readonly=False)
    srv = c.app.state.veha
    assert c.post("/api/runner/start", json={"suite": "hang"}).status_code == 200
    child = _child_pid(hang_root)
    try:
        r = c.post("/api/runner/stop")
        assert r.status_code == 200 and r.json()["stopped"] is True
        srv.runner.wait(20)
        run = srv.runner.status()["run"]
        assert not srv.runner.busy and run["status"] == "stopped"
        assert "не убит" in run["note"] or "не убита" in run["note"]
    finally:
        _kill_pid(child)


# ------------------------------------------------------------------


def test_server_shutdown_kills_the_running_tree(hang_root):
    """Ctrl+C у сервера: прогон не продолжает гонять стенд без хозяина."""
    from fastapi.testclient import TestClient

    from veha.viz.app import create_app

    app = create_app(hang_root, readonly=False)
    with TestClient(app, base_url="http://127.0.0.1:8770") as c:     # lifespan
        assert c.post("/api/runner/start", json={"suite": "hang"}).status_code == 200
        child = _child_pid(hang_root)
    srv = app.state.veha
    srv.runner.wait(20)
    assert not srv.runner.busy
    assert _wait(lambda: not pid_alive(child), 10), "внук пережил остановку сервера"

