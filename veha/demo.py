"""Демонстрационный набор внутри пакета: `veha demo`.

Набор вымышлен целиком и лежит в `veha/demo_data/` — тем же каталогом в репозитории и в
колесе. Пишет его `demo/generate.py` из корня репозитория; в колесо генератор не входит.

Сервер над набором поднимается только в режиме просмотра и читает его на месте. В этом
режиме он ничего не пишет (всё пишущее — за `--allow-run`), поэтому каталог установки
остаётся нетронутым и копия во временный каталог не нужна.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

#: Каталог набора внутри установленного пакета.
DATA = Path(__file__).resolve().parent / "demo_data"

#: Без этих файлов сцена пуста — набор считается неполным.
REQUIRED = ("atlas.yaml", "coverage.json", "atlas/default/default/db_schema.json",
            "allure-results")


def data_root() -> Path:
    """Каталог демонстрационного набора в установленном пакете."""
    return DATA


def missing(root: Path) -> List[str]:
    """Чего не хватает набору: пусто — набор на месте."""
    return [name for name in REQUIRED if not (root / name).exists()]
