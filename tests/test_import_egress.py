"""Импорт сервера не читает `confpartest` проекта и не ходит в сеть.

Сервер импортирует `partest.redact`, а с ним — пакет `partest` целиком. До partest
2.6.0 импорт пакета читал `confpartest` с `sys.path` и загружал каждую спецификацию из
`swagger_files`, в том числе по URL: `python -m veha serve`, запущенный из корня набора
тестов проекта, ходил бы в сеть раньше, чем сервер успел что-либо обслужить. Проверки
`test_no_egress.py` закрывают сеть после импорта и этого не видят.
"""

from __future__ import annotations

import os
import subprocess
import sys

#: Подпроцесс закрывает сеть до первого импорта и записывает каждую попытку: код,
#: который глотает `OSError`, иначе спрятал бы попытку за молчаливым отказом.
_GUARDED_IMPORT = r'''
import pathlib, socket, sys
log = pathlib.Path(sys.argv[1])
def deny(*args, **kwargs):
    with log.open("a", encoding="utf-8") as f:
        f.write(repr(args[1:] or args) + "\n")
    raise OSError("outgoing connection denied by the test")
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
socket.getaddrinfo = deny
import veha.redact, veha.viz.app, veha.cli, veha.runner
'''


def test_importing_the_server_does_not_read_the_project_confpartest(tmp_path):
    flag = tmp_path / "confpartest.imported"
    attempts = tmp_path / "egress.log"
    (tmp_path / "confpartest.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(flag)!r}).write_text('1')\n"
        "swagger_files = {'api': {'type': 'url', 'path': 'http://192.0.2.1/openapi.json'}}\n",
        encoding="utf-8")
    # `-c` кладёт текущий каталог первым в `sys.path` — как `python -m veha` из корня
    # набора тестов проекта.
    out = subprocess.run(
        [sys.executable, "-c", _GUARDED_IMPORT, str(attempts)],
        cwd=tmp_path, env=dict(os.environ), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    logged = attempts.read_text(encoding="utf-8") if attempts.exists() else ""
    assert not logged, "импорт сервера пытался соединиться: " + logged
    assert not flag.exists(), "импорт сервера прочитал confpartest проекта"
