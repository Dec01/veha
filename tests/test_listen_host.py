"""`veha serve --host` и `--allowed-host`: адрес для контейнера и список допустимых `Host`.

По умолчанию ничего не меняется: loopback и `Host` только `127.0.0.1`/`localhost` под своим
портом. Другой адрес — литерал IPv4 (имя узла разрешалось бы через DNS) и предупреждение;
дополнительные `Host` — точные имена без шаблонов, иначе вернулся бы DNS rebinding. И в
режиме просмотра сервер ничего не пишет в корень данных: в контейнере это том `:ro`.
"""

from __future__ import annotations

import argparse
import builtins
import io
import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from tests.conftest import make_client


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ATLAS_SYSTEM", "ATLAS_ENV", "VEHA_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------ разбор ключей


@pytest.mark.parametrize("text, want", [("127.0.0.1", "127.0.0.1"), (" 0.0.0.0 ", "0.0.0.0"),
                                        ("10.1.2.3", "10.1.2.3")])
def test_listen_address_accepts_ipv4(text, want):
    from veha.cli import listen_address

    assert listen_address(text) == want


@pytest.mark.parametrize("text", ["localhost", "veha.example", "::1", "0.0.0.0:80", ""])
def test_listen_address_refuses_names_and_ipv6(text):
    from veha.cli import listen_address

    with pytest.raises(argparse.ArgumentTypeError):
        listen_address(text)


@pytest.mark.parametrize("text, want", [
    ("Veha.Example", "veha.example:8770"), ("veha.example:9000", "veha.example:9000"),
    ("10.0.0.5", "10.0.0.5:8770"), ("localhost:9000", "localhost:9000"),
    # `имя:` — Host без порта: так его шлёт браузер на 80/443 (за своим прокси)
    ("veha.internal:", "veha.internal"),
])
def test_allowed_host_is_normalised(text, want):
    from veha.cli import allowed_hosts_from

    assert allowed_hosts_from([text], "", 8770) == [want]


@pytest.mark.parametrize("text", ["*.example", "*", "a b", "x:0", "x:70000", "x:port",
                                  "-bad.example", "", "evil.example/"])
def test_allowed_host_refuses_patterns_and_junk(text):
    from veha.cli import allowed_host

    with pytest.raises(argparse.ArgumentTypeError):
        allowed_host(text, 8770)


def test_bare_name_form_survives_the_argument_parser():
    from veha.cli import allowed_host, allowed_hosts_from

    parsed = allowed_host("Veha.Internal:")          # как его вернёт argparse
    assert allowed_hosts_from([parsed], "veha.other:", 8770) == ["veha.internal", "veha.other"]


def test_bare_name_is_served_without_a_port(demo_root):
    from fastapi.testclient import TestClient

    from veha.viz.app import create_app

    c = TestClient(create_app(demo_root, port=8770, allowed_hosts=["veha.internal"]))
    assert c.get("/api/graph", headers={"host": "veha.internal"}).status_code == 200
    assert c.get("/api/graph", headers={"host": "veha.internal:8770"}).status_code == 400


def test_allow_run_refuses_a_non_loopback_host(demo_root, monkeypatch, capsys):
    """Изменяющие маршруты защищены от браузера, а не от любого клиента: без входа по
    токену запуск и запись — только на loopback, ключа-обхода нет."""
    code, seen = _serve(monkeypatch, demo_root, "--host", "0.0.0.0", "--allow-run")
    assert code == 2 and "bind" not in seen
    assert "--allow-run не сочетается с --host 0.0.0.0" in capsys.readouterr().err
    code, seen = _serve(monkeypatch, demo_root, "--host", "127.0.0.2", "--allow-run")
    assert code == 0 and seen["bind"] == ("127.0.0.2", 8770)    # loopback — можно


def test_allowed_hosts_come_from_flags_and_environment():
    from veha.cli import allowed_hosts_from

    got = allowed_hosts_from(["a.example"], " b.example:9000, ,c.example ", 8770)
    assert got == ["a.example:8770", "b.example:9000", "c.example:8770"]


# ------------------------------------------------------------- serve целиком


def _serve(monkeypatch, demo_root, *extra):
    """`veha serve` до запуска uvicorn: что занято, что разрешено, что напечатано."""
    import uvicorn

    from veha import cli

    seen = {}
    real_bind = cli.bind_socket

    def fake_bind(port, host="127.0.0.1"):
        # 0.0.0.0 по-настоящему не занимаем: проверке хватает, что адрес дошёл до сокета
        seen["bind"] = (host, port)
        return real_bind(0)

    def fake_run(server, *args, sockets=None, **kwargs):
        seen.update(host=server.config.host, app=server.config.app)

    monkeypatch.setattr(cli, "bind_socket", fake_bind)
    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    code = cli.main(["serve", "--root", str(demo_root), "--port", "8770", "--no-browser",
                     *extra])
    return code, seen


def test_default_is_loopback_without_a_warning(demo_root, monkeypatch, capsys):
    code, seen = _serve(monkeypatch, demo_root)
    assert code == 0 and seen["bind"] == ("127.0.0.1", 8770) and seen["host"] == "127.0.0.1"
    assert seen["app"].state.allowed_hosts == {"127.0.0.1:8770", "localhost:8770"}
    assert "не только loopback" not in capsys.readouterr().err


def test_non_loopback_host_warns_and_reaches_the_socket(demo_root, monkeypatch, capsys):
    monkeypatch.setenv("VEHA_ALLOWED_HOSTS", "veha.internal")
    code, seen = _serve(monkeypatch, demo_root, "--host", "0.0.0.0",
                        "--allowed-host", "localhost:9000")
    assert code == 0 and seen["bind"] == ("0.0.0.0", 8770) and seen["host"] == "0.0.0.0"
    captured = capsys.readouterr()
    assert "слушаю 0.0.0.0:8770 — не только loopback" in captured.err
    assert "Входа в вебморду нет" in captured.err
    assert "слушает 0.0.0.0:8770" in captured.out
    assert seen["app"].state.allowed_hosts == {
        "127.0.0.1:8770", "localhost:8770", "localhost:9000", "veha.internal:8770"}


def test_bad_allowed_hosts_in_the_environment_refuse_to_start(demo_root, monkeypatch, capsys):
    monkeypatch.setenv("VEHA_ALLOWED_HOSTS", "*.example")
    code, seen = _serve(monkeypatch, demo_root)
    assert code == 2 and "bind" not in seen
    assert "VEHA_ALLOWED_HOSTS" in capsys.readouterr().err


def test_bad_host_flag_is_an_argument_error(demo_root, capsys):
    from veha import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--root", str(demo_root), "--host", "localhost"])
    assert exc.value.code == 2
    assert "имя узла не принимается" in capsys.readouterr().err


# ---------------------------------------------------------------- проверка Host


def test_foreign_host_is_refused_and_allowed_one_is_served(demo_root):
    from fastapi.testclient import TestClient

    from veha.viz.app import create_app

    c = TestClient(create_app(demo_root, port=8770, allowed_hosts=["localhost:9000"]))
    ok = {"localhost:9000", "LOCALHOST:8770", "127.0.0.1:8770"}
    bad = {"evil.example:8770", "localhost:8771", "localhost", "127.0.0.1:9000", ""}
    for host in ok:
        assert c.get("/api/graph", headers={"host": host}).status_code == 200, host
    for host in bad:
        assert c.get("/api/graph", headers={"host": host}).status_code == 400, host
    # изменяющий запрос с разрешённого Host — проверка источника идёт от него же
    r = c.post("/api/runner/clean", headers={"host": "localhost:9000",
                                              "origin": "http://localhost:9000"})
    assert r.status_code == 409             # режим просмотра, а не «чужой источник»


# --------------------------------------------- режим просмотра не пишет в корень


GET_ROUTES = ["/", "/api/graph", "/api/graph?refresh=true", "/api/artifacts", "/api/tests",
              "/api/log", "/api/live/containers", "/api/live/mrs",
              "/api/live/container/notifier/logs", "/api/runner/suites",
              "/api/runner/status"]
POST_ROUTES = [("/api/live/mr", {"iid": 1, "status": "checked", "note": ""}),
               ("/api/runner/start", {"suite": "full"}), ("/api/runner/stop", None),
               ("/api/runner/clean", None), ("/api/rerun", {"nodeid": "t.py::test_x"})]


@pytest.fixture
def write_spy(monkeypatch):
    """Любая попытка открыть на запись, создать, переименовать или удалить файл —
    записывается. Подмена вместо атрибута «только чтение»: на Windows атрибут каталога
    записи в него не запрещает."""
    attempts = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def note(path):
        attempts.append(os.path.abspath(os.fspath(path)))

    def spy_open(file, mode="r", *a, **kw):
        if isinstance(file, (str, bytes, os.PathLike)) and any(m in mode for m in "wax+"):
            note(file)
        return real_open(file, mode, *a, **kw)

    def spy_io_open(file, mode="r", *a, **kw):
        if isinstance(file, (str, bytes, os.PathLike)) and any(m in mode for m in "wax+"):
            note(file)
        return real_io_open(file, mode, *a, **kw)

    def spy_os_open(path, flags, *a, **kw):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            note(path)
        return real_os_open(path, flags, *a, **kw)

    def recorder(real):
        def wrapped(path, *a, **kw):
            note(path)
            return real(path, *a, **kw)
        return wrapped

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_io_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    for mod, names in ((os, ("replace", "rename", "remove", "unlink", "mkdir", "makedirs",
                             "rmdir")),
                       (shutil, ("rmtree", "move", "copy", "copy2", "copyfile"))):
        for name in names:
            monkeypatch.setattr(mod, name, recorder(getattr(mod, name)))
    for name in ("write_text", "write_bytes", "mkdir", "touch", "unlink", "rename",
                 "replace", "rmdir"):
        monkeypatch.setattr(Path, name, recorder(getattr(Path, name)))
    monkeypatch.setattr(tempfile, "mkstemp", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError(f"mkstemp {a} {kw}")))
    return attempts


def test_read_only_mode_writes_nothing_under_the_root(small_root, write_spy):
    """Корень в контейнере — том `:ro`. Без `--allow-run` сервер не пишет в него ничего:
    ни истории, ни отметок, ни кэша; изменяющие запросы отвечают 409 до любой записи."""
    c = make_client(small_root)                 # readonly=True по умолчанию
    for url in GET_ROUTES:
        assert c.get(url).status_code == 200, url
    for url, body in POST_ROUTES:
        kw = {"content": json.dumps(body), "headers": {"content-type": "application/json"}} \
            if body is not None else {}
        assert c.post(url, **kw).status_code == 409, url
    root = os.path.abspath(small_root)
    inside = [p for p in write_spy if p == root or p.startswith(root + os.sep)]
    assert inside == []


def test_spy_actually_catches_a_write(small_root, write_spy):
    """Без этого проверка выше могла бы проходить потому, что ничего не ловит."""
    (small_root / "probe.txt").write_text("x", encoding="utf-8")
    with open(small_root / "probe2.txt", "w", encoding="utf-8") as f:
        f.write("x")
    assert os.path.abspath(small_root / "probe.txt") in write_spy
    assert os.path.abspath(small_root / "probe2.txt") in write_spy


@pytest.mark.skipif(os.name == "nt", reason="на Windows атрибут каталога запись не запрещает")
def test_read_only_root_on_posix(small_root):
    """На POSIX — по-настоящему: корень и всё в нём без права записи, сервер отвечает."""
    for d, dirs, files in os.walk(small_root):
        for name in files:
            os.chmod(os.path.join(d, name), 0o444)
        os.chmod(d, 0o555)
    try:
        c = make_client(small_root)
        for url in GET_ROUTES:
            assert c.get(url).status_code == 200, url
    finally:
        for d, dirs, files in os.walk(small_root):
            os.chmod(d, 0o755)


def test_listening_socket_default_is_loopback():
    from veha.listen import bind_socket

    sock = bind_socket(0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
        assert sock.family == socket.AF_INET
    finally:
        sock.close()
