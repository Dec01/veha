"""Команда `veha`.

    veha demo [--port 8770] [--no-browser]
    veha serve --root <каталог> [--port 8770] [--no-browser]
               [--system S --env E] [--allow-run]
    veha export-static --root <каталог> --out <каталог> [--system S --env E]
    veha init [--root <каталог проекта>] [--system S] [--env E]

Сервер слушает только 127.0.0.1: аутентификации во вебморде пока нет, а показывает она
сведённый граф всей тестируемой системы.
"""

from __future__ import annotations

import argparse
import atexit
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional

from partest_atlas.artifacts import DEFAULT_SCOPE_KEY, Scope

from veha import __version__, scaffold
from veha.core.sources import valid_segment
from veha.listen import HOST, bind_socket


class ScopeError(ValueError):
    """Имя системы или контура не годится в сегмент пути."""


def _requested_scope(args: argparse.Namespace) -> Optional[Scope]:
    """Область, заданная явно: ключами или теми же переменными, что у агента.

    Имя становится каталогом под `atlas/`, поэтому допустим один сегмент
    `[A-Za-z0-9._-]+` без `..` — иначе через область можно выйти за каталог артефактов.
    """
    system = args.system or os.getenv("ATLAS_SYSTEM") or ""
    env = args.env or os.getenv("ATLAS_ENV") or ""
    if not (system or env):
        return None
    for label, value in (("система", system), ("контур", env)):
        if value and not valid_segment(value):
            raise ScopeError(f"{label} {value!r}: допустим один сегмент [A-Za-z0-9._-]+ без '..'")
    return Scope(system=system or DEFAULT_SCOPE_KEY, env=env or DEFAULT_SCOPE_KEY)


def _report(g: dict, root: Path, out=None, cfg: Any = None) -> None:
    """Что прочитано при старте. *cfg* — конфигурация сервера: по ней называются пути
    отчётов, заданные в `reports` (терминал локальный, путь здесь уместен — в вебморду
    он не уходит)."""
    out = out or sys.stdout
    if cfg is None:
        from veha import config as veha_config  # noqa: PLC0415

        cfg = veha_config.load(root)
    cov_rep, allure_rep = cfg.report("coverage"), cfg.report("allure")
    meta, arts = g["meta"], g["artifacts"]
    scope = arts["scope"]
    mark = "" if arts["scopeChosen"] else "! "
    print(f"  {mark}область {scope['system']}/{scope['env']} — {arts['scopeHow']}", file=out)
    print(
        f"  узлов {meta['nodes']} · рёбер {meta['edges']} · "
        f"эндпоинтов {meta['endpoints']} · тестов из Allure {meta['tests']}", file=out)
    if not meta.get("configExists"):
        print(f"  ! нет atlas.yaml в {root} — ландшафт пуст, артефакты ищутся в atlas/",
              file=out)
    if meta.get("configError"):
        print(f"  ! atlas.yaml не прочитан: {meta['configError']}", file=out)
    for a in arts["artifacts"]:
        if a["state"] not in ("ok", "missing"):
            print(f"  ! {a['file']}: {a['state']} — {'; '.join(a['problems'])}", file=out)
        for w in a.get("warnings") or []:
            print(f"  ! {a['file']}: {w}", file=out)
    for note in arts.get("ignored") or []:
        print(f"  ! {note}", file=out)
    collected = g.get("collected") or {}
    lost = {k: v for k, v in (collected.get("lostRecords") or {}).items() if v}
    if collected.get("reportsMissing"):
        if lost or collected.get("dbEffects"):
            print(f"  ! {collected['reportsMissing']} — записи агента не с чем соединить",
                  file=out)
    elif collected.get("reportsCleared") and (lost or collected.get("dbEffects")):
        print("  ! отчёты Allure очищены — записи агента не с чем соединить, связи "
              "появятся после прогона", file=out)
    else:
        labels = collected.get("lostLabels") or {}
        for key, n in lost.items():
            print(f"  ! записей агента без единой связи: {n} — {labels.get(key, key)}",
                  file=out)
        if collected.get("notInReports"):
            # не потеря: db.effects накопительный, старые тесты в нём есть всегда
            print(f"  записей агента от тестов, которых нет в текущих отчётах: "
                  f"{collected['notInReports']}", file=out)
    if meta.get("partialRun"):
        print("  ! coverage.json — частичный прогон: процент с полным прогоном несравним",
              file=out)
    if meta.get("optinActed"):
        # не предупреждение: снятый набор — штатный полный прогон, но другого набора
        print("  coverage.json — opt-in: " + "; ".join(meta.get("optinText") or []),
              file=out)
    for rep in (cov_rep, allure_rep):
        if rep.configured and rep.path is not None:
            print(f"  {rep.label} → {os.path.normpath(rep.path)}", file=out)
    for problem in meta.get("reportProblems") or []:
        print(f"  ! {problem}", file=out)
    if not meta["hasCoverage"]:
        print(f"  ! нет {cov_rep.label} — покрытие по методике не будет показано", file=out)
    if not meta["hasAllure"]:
        print(f"  ! нет {allure_rep.label} — проверки и обмен не будут показаны", file=out)


def serve(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"нет каталога: {root}", file=sys.stderr)
        return 2
    try:
        scope = _requested_scope(args)
    except ScopeError as exc:
        print(f"область: {exc}", file=sys.stderr)
        return 2

    return _run(root, scope=scope, allow_run=args.allow_run, port=args.port,
                no_browser=args.no_browser)


def demo(args: argparse.Namespace) -> int:
    """Вымышленный набор из пакета: только просмотр, ничего не пишется.

    Область не берётся ни из ключей, ни из `ATLAS_SYSTEM`/`ATLAS_ENV`: набор описывает
    один проект, а переменные, оставшиеся в оболочке от настоящего проекта, показали бы
    пустую сцену.
    """
    from veha import demo as veha_demo  # noqa: PLC0415

    root = veha_demo.data_root()
    lost = veha_demo.missing(root)
    if lost:
        print(f"демонстрационного набора нет в пакете ({root}): нет " + ", ".join(lost),
              file=sys.stderr)
        print("из исходников набор собирает  python demo/generate.py", file=sys.stderr)
        return 2
    return _run(root, scope=None, allow_run=False, port=args.port,
                no_browser=args.no_browser, note="демонстрация на вымышленных данных")


def _run(root: Path, *, scope: Optional[Scope], allow_run: bool, port: int,
         no_browser: bool, note: str = "") -> int:
    try:
        sock = bind_socket(port)
    except OSError as exc:
        print(f"порт {port} на {HOST} занят или недоступен ({exc.strerror or exc}); "
              f"укажите другой: --port N", file=sys.stderr)
        return 2
    try:
        return _run_bound(root, sock, scope=scope, allow_run=allow_run, port=port,
                          no_browser=no_browser, note=note)
    finally:
        sock.close()


def _run_bound(root: Path, sock, *, scope: Optional[Scope], allow_run: bool,
               port: int, no_browser: bool, note: str) -> int:
    from veha.viz.app import create_app  # noqa: PLC0415 — тяжёлый импорт нужен серверу

    app = create_app(root, scope=scope, readonly=not allow_run, port=port)
    # Запасной путь к остановке дерева процессов, если lifespan не отработал
    # (жёсткий выход интерпретатора): прогон не должен пережить сервер.
    atexit.register(app.state.veha.shutdown)
    url = f"http://{HOST}:{port}"
    print(f"Веха {__version__} → {url}" + (f"  ({note})" if note else ""))
    print(f"  корень {root}")
    print("  режим: " + ("запуск прогонов разрешён" if allow_run else "только просмотр"))
    _report(app.state.veha.graph(refresh=True), root, cfg=app.state.veha.cfg)
    from veha import runner as veha_runner  # noqa: PLC0415

    if not any(s.full for s in veha_runner.suites(app.state.veha.cfg)):
        print("  ! в runner.suites нет набора с full: true — тренда среднего покрытия "
              "не будет")
    # Браузер — только когда сервер действительно поднялся (startup uvicorn прошёл).
    opener = None if no_browser else (lambda: _open_browser(url))
    serve_app(app, port, sock=sock, on_started=opener)
    return 0


def _open_browser(url: str) -> None:
    # в отдельном потоке: из цикла событий uvicorn нельзя ждать запуска браузера
    threading.Thread(target=lambda: __import__("webbrowser").open(url), daemon=True).start()


def export_static(args: argparse.Namespace) -> int:
    """Статический снимок вебморды для сайта: страница, статика, ответы API файлами."""
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"нет каталога: {root}", file=sys.stderr)
        return 2
    try:
        scope = _requested_scope(args)
    except ScopeError as exc:
        print(f"область: {exc}", file=sys.stderr)
        return 2

    from veha.viz.export import ExportError  # noqa: PLC0415 — тянет fastapi
    from veha.viz.export import export_static as run_export  # noqa: PLC0415

    try:
        rep = run_export(root, Path(args.out), scope=scope)
    except ExportError as exc:
        print(f"снимок не записан: {exc}", file=sys.stderr)
        return 2
    counts = ", ".join(f"{k} {v}" for k, v in sorted(rep.counts.items()))
    print(f"Веха {__version__}: снимок → {rep.out}")
    print("  ! снимок публикует всё, что видно в вебморде: на настоящих данных наружу "
          "не выкладывать")
    print(f"  файлов {rep.files}, {rep.bytes / 1_000_000:.1f} МБ; {counts}")
    for problem in rep.report_problems:
        print(f"  ! {problem}")
    for note in rep.skipped:
        print(f"  ! пропущено: {note}")
    return 0


def init(args: argparse.Namespace) -> int:
    """Заготовка `atlas.yaml` для существующего проекта. Код проекта не выполняется.

    Существующий файл не перезаписывается, ключа `--force` нет: в `atlas.yaml` ручная
    работа, и вернуть заготовку — одно явное действие человека (удалить или переименовать
    файл), а не флаг, который легко добавить по привычке и потерять настройку.
    """
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"нет каталога: {root}", file=sys.stderr)
        return 2
    for label, value in (("система", args.system), ("контур", args.env)):
        if value and not valid_segment(value):
            print(f"{label} {value!r}: допустим один сегмент [A-Za-z0-9._-]+ без '..'",
                  file=sys.stderr)
            return 2
    p = scaffold.plan(root, system=args.system, env=args.env)
    try:
        target = scaffold.write(p)
    except scaffold.Exists as exc:
        print(f"{exc} уже есть — не перезаписываю: в нём может быть ручная настройка. "
              f"Чтобы получить заготовку заново, переименуйте или удалите файл.",
              file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"atlas.yaml не записан: {exc.strerror or exc}", file=sys.stderr)
        return 2
    print(f"Веха {__version__}: заготовка → {target}")
    services = [s for s in p.spec.services if s.kind]
    if services:
        print("  спецификации из confpartest.swagger_files: "
              + ", ".join(f"{s.name} ({s.kind})" for s in services))
    for key, found, value in (("coverage", p.coverage, p.coverage_path),
                              ("allure", p.allure, p.allure_path)):
        how = f"из {found.where}" if found.usable else "по умолчанию"
        print(f"  reports.{key}: {value} — {how}")
    for warning in p.warnings():
        print(f"  ! {warning}")
    print("Дальше:")
    for i, step in enumerate(scaffold.next_steps(p), 1):
        print(f"  {i}. {step}")
    return 0


def serve_app(app, port: int, *, sock: Optional[Any] = None,
              on_started: Optional[Callable[[], None]] = None) -> None:
    """Запустить uvicorn так, чтобы сигнал остановки сначала убивал деревья процессов.

    uvicorn на SIGINT/SIGTERM (и SIGBREAK на Windows) ставит флаг выхода и ждёт, пока
    допишутся идущие запросы. Синхронный `/api/rerun` идёт до десяти минут, и lifespan
    до тех пор не срабатывает: тест продолжал бы гонять стенд после Ctrl+C. Поэтому
    обработчик выхода сначала убивает деревья — тогда перезапуск отвечает сразу, и
    uvicorn завершается штатно.

    *sock* — уже занятый порт (:func:`bind_socket`); *on_started* вызывается, когда
    сервер принял сокет и прошёл startup.
    """
    make_server(app, port, on_started=on_started).run(sockets=[sock] if sock else None)


def make_server(app, port: int, on_started: Optional[Callable[[], None]] = None):
    """uvicorn.Server, у которого выход по сигналу сначала убивает деревья процессов."""
    import uvicorn  # noqa: PLC0415

    class VehaServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started and not self.should_exit and on_started is not None:
                on_started()

        def handle_exit(self, sig, frame):
            # в отдельном потоке: обработчик сигнала не должен ждать taskkill
            threading.Thread(target=app.state.veha.shutdown, daemon=True).start()
            super().handle_exit(sig, frame)

    return VehaServer(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))


def port_number(text: str) -> int:
    """Порт для `--port`: целое 1..65535.

    0 — «любой свободный»: сервер поднялся бы на случайном порту, а проверка `Host`
    ждала бы 0, и страница не открылась бы. За 65535 сокет падает `OverflowError` с
    трассировкой вместо сообщения.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"не число: {text!r}") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"{value} вне диапазона 1..65535")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="veha", description="Веха — карта тестирования")
    p.add_argument("--version", action="version", version=f"veha {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="карта на вымышленном наборе из пакета: только просмотр",
                       description="Поднять вебморду над вымышленным набором, который "
                                   "поставляется в пакете. Только просмотр: прогоны не "
                                   "запускаются, ничего не пишется.")
    d.add_argument("--port", type=port_number, default=8770, help="порт на 127.0.0.1, 1..65535")
    d.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    d.set_defaults(func=demo)

    s = sub.add_parser("serve", help="поднять вебморду над каталогом данных")
    s.add_argument("--root", required=True,
                   help="корень данных: atlas.yaml, atlas/, coverage.json, allure-results/ "
                        "(пути отчётов — раздел reports в atlas.yaml)")
    s.add_argument("--port", type=port_number, default=8770, help="порт на 127.0.0.1, 1..65535")
    s.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    s.add_argument("--system", default="", help="система (по умолчанию — ATLAS_SYSTEM)")
    s.add_argument("--env", default="", help="контур (по умолчанию — ATLAS_ENV)")
    s.add_argument("--allow-run", action="store_true",
                   help="разрешить запуск наборов pytest и запись отметок из вебморды")
    s.set_defaults(func=serve)

    e = sub.add_parser("export-static",
                       help="статический снимок вебморды для сайта: только просмотр",
                       description="Статический снимок вебморды: страница, статика и "
                                   "ответы API файлами, без сервера. ВНИМАНИЕ: Снимок публикует всё, что показывает вебморда: контейнеры, логи, merge request'ы, шаги и вложения тестов, схему базы. Маскирование секретов — эвристика. Снимок настоящих данных наружу не выкладывать: для витрины есть вымышленный набор: veha demo.")
    e.add_argument("--root", required=True, help="корень данных, как у serve")
    e.add_argument("--out", required=True,
                   help="каталог снимка: пустой, новый или прежний снимок")
    e.add_argument("--system", default="", help="система (по умолчанию — ATLAS_SYSTEM)")
    e.add_argument("--env", default="", help="контур (по умолчанию — ATLAS_ENV)")
    e.set_defaults(func=export_static)

    i = sub.add_parser("init", help="заготовка atlas.yaml для проекта на pytest + partest",
                       description="Записать atlas.yaml в корень проекта: спецификации из "
                                   "confpartest.swagger_files (разбором файла, без импорта "
                                   "и без выполнения кода проекта), пути отчётов из "
                                   "настроек pytest, набор full для тренда, шаблоны "
                                   "остальных разделов. Существующий atlas.yaml не "
                                   "перезаписывается.")
    i.add_argument("--root", default=".",
                   help="корень проекта: confpartest.py, pytest.ini (по умолчанию — текущий)")
    i.add_argument("--system", default="",
                   help="система для подсказок (по умолчанию — имя каталога)")
    i.add_argument("--env", default="",
                   help=f"контур для подсказок (по умолчанию — {scaffold.DEFAULT_ENV})")
    i.set_defaults(func=init)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
