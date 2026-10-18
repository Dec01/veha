"""Ссылки README и CHANGELOG на файлы репозитория — абсолютные, на тег версии и не битые.

README — это и описание пакета на PyPI, а там относительная ссылка ведёт в никуда.
Страница выпуска на PyPI неизменна, поэтому ссылки ведут не на ветку, а на тег
`v<версия>`: ветка уйдёт вперёд, а страница выпуска должна показывать файлы своего
выпуска. Тег ставится при выпуске; здесь проверяется только, что файл с таким путём
есть в дереве репозитория — без сети.
"""

from __future__ import annotations

import re

import pytest

import veha
from tests.conftest import REPO

REPO_URL = "https://github.com/Dec01/veha/"
BLOB = f"{REPO_URL}blob/v{veha.__version__}/"
LINK = re.compile(r"\]\(([^)\s]+)\)")


@pytest.mark.parametrize("doc", ["README.md", "CHANGELOG.md"])
def test_repo_links_are_absolute_tagged_and_exist(doc):
    targets = LINK.findall((REPO / doc).read_text(encoding="utf-8"))
    relative = [t for t in targets if not re.match(r"[a-z]+:", t) and not t.startswith("#")]
    assert not relative, f"{doc}: относительные ссылки на PyPI не работают: {relative}"
    untagged = [t for t in targets if t.startswith(REPO_URL + "blob/") and not t.startswith(BLOB)]
    assert not untagged, f"{doc}: ссылка не на тег v{veha.__version__}: {untagged}"
    missing = [t for t in targets if t.startswith(BLOB)
               and not (REPO / t[len(BLOB):].split("#")[0]).exists()]
    assert not missing, f"{doc}: нет файлов {missing}"
