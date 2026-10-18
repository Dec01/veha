"""Ноль исходящего трафика: ни сервер, ни страница никуда не ходят.

Проверяется тремя способами, потому что каждый ловит своё:

* сеть закрыта на уровне сокетов, и все маршруты отвечают — сервер не пытается
  соединиться ни с чем, пока обслуживает запросы;
* в статике нет внешних адресов — страница не тянет ни шрифтов, ни библиотек, ни CDN;
* в коде пакета нет сетевых клиентов и адресов по умолчанию.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

from tests.conftest import make_client

PKG = Path(__file__).resolve().parents[1] / "veha"
STATIC = PKG / "viz" / "static"

# Любой адрес со схемой или протокол-независимая ссылка в src/href/url(...)
_URL_RE = re.compile(r"""(?:https?|wss?|ftp)://|(?:src|href)\s*=\s*["']//|url\(\s*["']?//""",
                     re.I)
#: Адрес самого сервера: в коде он собирается из `HOST`, который сам равен 127.0.0.1.
_LOCAL_RE = re.compile(r"(?:https?|wss?)://(?:127\.0\.0\.1|localhost|\{HOST\})", re.I)

ROUTES = [
    "/", "/api/graph", "/api/artifacts", "/api/tests", "/api/log",
    "/api/live/containers", "/api/live/mrs", "/api/live/container/notifier/logs",
    "/api/runner/suites", "/api/runner/status",
]


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def no_network(monkeypatch):
    """Всё, что не loopback, запрещено и записывается.

    Loopback пропускается не из снисхождения: на Windows цикл событий asyncio строит
    свой внутренний канал через соединение с 127.0.0.1, и без этого не поднялся бы
    даже тестовый клиент. Наружу это не выходит.
    """
    attempts = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _host(address):
        return address[0] if isinstance(address, tuple) else address

    def guard(real, method=False):
        def wrapped(*args, **kwargs):
            target = args[1] if method else args[0]
            if _host(target) not in _LOOPBACK:
                attempts.append(target)
                raise OSError(f"исходящее соединение запрещено проверкой: {target!r}")
            return real(*args, **kwargs)
        return wrapped

    monkeypatch.setattr(socket.socket, "connect", guard(real_connect, method=True))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(real_connect_ex, method=True))
    monkeypatch.setattr(socket, "create_connection", guard(real_create))
    monkeypatch.setattr(socket, "getaddrinfo", guard(real_getaddrinfo))
    return attempts


def test_guard_actually_catches_egress(no_network):
    """Без этого проверка ниже могла бы проходить потому, что ничего не ловит."""
    with pytest.raises(OSError):
        socket.create_connection(("example.invalid", 80), timeout=1)
    assert no_network == [("example.invalid", 80)]


def test_server_makes_no_connections(demo_root, no_network):
    c = make_client(demo_root)      # приложение в процессе, без сокетов
    for url in ROUTES:
        assert c.get(url).status_code == 200, url
    assert no_network == []


def test_static_has_no_external_addresses():
    files = sorted(p for p in STATIC.iterdir() if p.is_file())
    assert files
    for f in files:
        text = f.read_text(encoding="utf-8")
        found = [m.group(0) for m in _URL_RE.finditer(text)]
        assert not found, f"{f.name}: {found}"
        assert "@import" not in text and "@font-face" not in text, f.name


def test_api_answers_carry_no_external_addresses(client):
    for url in ROUTES[1:]:
        text = client.get(url).text
        external = [m.group(0) for m in re.finditer(r"(?:https?|wss?)://[^\s\"']+", text)
                    if not _LOCAL_RE.match(m.group(0))]
        assert not external, f"{url}: {external[:3]}"


#: Единственный модуль, которому можно `socket`: слушающий сокет сервера на loopback.
LISTEN = PKG / "listen.py"


def test_package_has_no_network_clients():
    forbidden = re.compile(
        r"^\s*(?:import|from)\s+(?:requests|httpx|aiohttp|urllib\.request|http\.client|"
        r"socket|smtplib|ftplib)\b", re.M)
    clients = re.compile(r"^\s*(?:import|from)\s+(?:requests|httpx|aiohttp|urllib\.request|"
                         r"http\.client|smtplib|ftplib)\b", re.M)
    for f in PKG.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not (clients if f == LISTEN else forbidden).search(text), f
        hosts = [m.group(0) for m in _URL_RE.finditer(text)
                 if not _LOCAL_RE.match(text[m.start():m.start() + 30])]
        assert not hosts, f"{f}: {hosts}"


#: Что `listen.py` может взять у модуля `socket` — белым списком, а не перечнем
#: запретов: новое имя (`create_connection`, `AF_INET6`, `socketpair`…) не пройдёт, даже
#: если его забыли запретить.
_SOCKET_NAMES = {"socket", "AF_INET", "SOCK_STREAM", "SOL_SOCKET"}
#: Методы, которые модуль может вызвать на чём угодно: настроить, занять, закрыть.
#: Ни `connect`, ни `listen`/`accept`, ни `send`/`recv` — слушать начинает uvicorn.
_METHODS = {"setsockopt", "set_inheritable", "bind", "close"}
#: Встроенные функции, которые модулю можно вызвать.
_BUILTINS = {"hasattr"}


def test_listening_module_only_listens():
    """`veha/listen.py` занимает порт на loopback и ничего больше; другим модулям пакета
    `socket` по-прежнему нельзя (тест выше). Проверяется дерево разбора, а не текст."""
    import ast

    tree = ast.parse(LISTEN.read_text(encoding="utf-8"))
    imports = [(n.module if isinstance(n, ast.ImportFrom) else None,
                tuple(a.name for a in n.names))
               for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert sorted(imports, key=str) == sorted(
        [("__future__", ("annotations",)), (None, ("socket",))], key=str), imports

    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "socket"}
    extra = {a for a in used if a not in _SOCKET_NAMES and not a.startswith("SO_")}
    assert not extra, f"из socket взято лишнее: {sorted(extra)}"

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    methods = [c.func.attr for c in calls if isinstance(c.func, ast.Attribute)
               and not (isinstance(c.func.value, ast.Name) and c.func.value.id == "socket")]
    assert set(methods) <= _METHODS, f"вызовы вне белого списка: {sorted(set(methods) - _METHODS)}"
    builtins = {c.func.id for c in calls if isinstance(c.func, ast.Name)}
    assert builtins <= _BUILTINS, builtins
    # `hasattr(socket, "SO_…")` — проверка опции, а не обход белого списка по имени
    for c in calls:
        if isinstance(c.func, ast.Name) and c.func.id == "hasattr":
            assert isinstance(c.args[1], ast.Constant) and c.args[1].value.startswith("SO_")

    binds = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "bind"]
    assert len(binds) == 1, "ровно один bind"
    (addr,) = binds[0].args
    assert (isinstance(addr, ast.Tuple)
            and [getattr(e, "id", None) for e in addr.elts] == ["HOST", "port"]), \
        "bind только на (HOST, port)"
    hosts = [n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and [getattr(t, "id", None) for t in n.targets] == ["HOST"]]
    assert hosts == ["127.0.0.1"], hosts


def test_listening_socket_is_not_inherited():
    """Порт сервера не уходит дочерним процессам раннера (pytest и то, что он запустит)."""
    from veha.listen import bind_socket

    sock = bind_socket(0)
    try:
        assert sock.get_inheritable() is False
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_server_listens_on_loopback_only(demo_root, monkeypatch, capsys):
    """Проверяется то, что реально уходит в uvicorn, а не константа в модуле."""
    import uvicorn

    from veha import cli

    seen = {}

    def fake_run(server, *args, sockets=None, **kwargs):
        cfg = server.config
        seen.update(host=cfg.host, port=cfg.port, app=cfg.app, server=server,
                    bound=[s.getsockname() for s in sockets or []])

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli.main(["serve", "--root", str(demo_root), "--port", "8791",
                     "--no-browser"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8791
    # uvicorn получает уже занятый сокет — проверяется, где он на самом деле слушает
    assert seen["bound"] == [("127.0.0.1", 8791)]
    assert seen["app"].state.allowed_hosts == {"127.0.0.1:8791", "localhost:8791"}
    assert seen["app"].state.veha.readonly is True
    assert "только просмотр" in capsys.readouterr().out
