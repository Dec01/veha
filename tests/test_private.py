"""В пакете и в демонстрационном наборе нет закрытых имён.

Список имён лежит в `.private-names` в корне репозитория; файл закрыт `.gitignore` и
есть только на машине разработчика — без него проверка пропускается, а не проходит.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".js", ".html", ".css", ".json", ".yaml", ".md", ".log", ".txt", ".toml"}


def _names():
    f = REPO / ".private-names"
    if not f.is_file():
        pytest.skip("нет .private-names — проверять не по чему")
    return [ln.strip().lower() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def test_no_private_names_in_package_and_demo():
    names = _names()
    roots = [REPO / "veha", REPO / "demo", REPO / "tests", REPO / "pyproject.toml"]
    files = []
    for r in roots:
        files += [r] if r.is_file() else [p for p in r.rglob("*")
                                          if p.is_file() and p.suffix in SUFFIXES]
    hits = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace").lower()
        hits += [(f.relative_to(REPO).as_posix(), i) for i, n in enumerate(names) if n in text]
    # номер строки списка, а не само имя: вывод проверки не должен его повторять
    assert not hits, hits[:10]
