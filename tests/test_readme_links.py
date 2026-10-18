"""Ссылки README и CHANGELOG на файлы репозитория — абсолютные и не битые.

README — это и описание пакета на PyPI, а там относительная ссылка ведёт в никуда.
Поэтому ссылки на файлы репозитория абсолютные, на GitHub, — и проверка «битая ли
ссылка» для них делается здесь, по дереву репозитория, без сети.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import REPO

BLOB = "https://github.com/Dec01/veha/blob/master/"
LINK = re.compile(r"\]\(([^)\s]+)\)")


@pytest.mark.parametrize("doc", ["README.md", "CHANGELOG.md"])
def test_repo_links_are_absolute_and_exist(doc):
    targets = LINK.findall((REPO / doc).read_text(encoding="utf-8"))
    relative = [t for t in targets if not re.match(r"[a-z]+:", t) and not t.startswith("#")]
    assert not relative, f"{doc}: относительные ссылки на PyPI не работают: {relative}"
    missing = [t for t in targets if t.startswith(BLOB)
               and not (REPO / t[len(BLOB):].split("#")[0]).exists()]
    assert not missing, f"{doc}: нет файлов {missing}"
