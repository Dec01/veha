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


def test_listening_module_only_listens():
    """`veha/listen.py` занимает порт и ничего не открывает наружу; другим модулям
    пакета `socket` по-прежнему нельзя (тест выше)."""
    text = LISTEN.read_text(encoding="utf-8")
    for call in (".connect(", ".connect_ex(", "create_connection", "getaddrinfo",
                 ".sendto(", "fromfd", "fromshare"):
        assert call not in text, call
    assert re.search(r'^HOST = "127\.0\.0\.1"$', text, re.M)
    assert "sock.bind((HOST, port))" in text


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
