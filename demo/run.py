#!/usr/bin/env python
"""Демонстрация «Вехи»: поднять сервер над вымышленным набором данных.

    python run.py                 # откроет браузер на http://127.0.0.1:8770
    python run.py --port 8900     # другой порт
    python run.py --no-browser    # не открывать браузер

То же самое, что `veha serve --root <эта папка>`: сервер берётся из установленного
пакета `veha`, а не из копии рядом. Ничего, кроме этой папки, не читается. Сеть не
нужна — ни сервер, ни страница наружу не ходят.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    p = argparse.ArgumentParser(description="Демонстрация «Вехи»")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = p.parse_args()

    try:
        from veha.cli import main as veha_main
    except ImportError:
        print("пакет veha не установлен. Из корня репозитория:  pip install -e .")
        return 2

    missing = [f for f in ("atlas.yaml", "coverage.json", "atlas/default/default/db_schema.json")
               if not (ROOT / f).exists()]
    if missing:
        print("нет файлов набора: " + ", ".join(missing))
        print("собрать набор:  python generate.py")
        return 2

    argv = ["serve", "--root", str(ROOT), "--port", str(args.port)]
    if args.no_browser:
        argv.append("--no-browser")
    return veha_main(argv)


if __name__ == "__main__":
    sys.exit(main())
