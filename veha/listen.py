"""Слушающий сокет сервера — единственное место пакета, где есть `socket`.

Только серверная сторона: занять порт и отдать сокет uvicorn. Исходящих соединений здесь
нет и быть не может — клиентских вызовов в модуле не должно быть ни одного, это держит
`tests/test_no_egress.py`.

Адрес — loopback по умолчанию. Другой адрес (`veha serve --host`, нужен внутри
контейнера) — только литерал IPv4: его проверяет и командная строка, и сама
:func:`bind_socket` до создания сокета. Имя узла сюда не проходит: его разрешение —
обращение к DNS.
"""

from __future__ import annotations

import ipaddress
import socket

#: Аутентификации во вебморде нет — по умолчанию слушаем только loopback.
HOST = "127.0.0.1"


def bind_socket(port: int, host: str = HOST) -> socket.socket:
    """Занять порт на *host* сейчас, до баннера и до uvicorn.

    Занятый порт должен выясняться раньше, чем напечатан адрес и открыт браузер: иначе
    браузер откроет чужой процесс на этом порту. Исключительное владение — потому что
    на Windows `SO_REUSEADDR` (его ставит uvicorn) разрешает второй сокет на занятом
    порту, и занятость не обнаружилась бы вовсе.

    *host* — только литерал IPv4, проверяется здесь же, до сокета: имя узла `bind`
    разрешил бы через DNS, а это обращение по сети. Не литерал — `ValueError`.
    """
    ipaddress.IPv4Address(host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Явно, а не по умолчанию PEP 446: раннер запускает pytest и всё, что тот
        # запустит, и ни один из этих процессов не должен унаследовать порт сервера.
        sock.set_inheritable(False)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):            # Windows
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:                                                  # POSIX: TIME_WAIT не мешает
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock
