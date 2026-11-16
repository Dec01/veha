"""Команда `veha`.

    veha demo [--port 8770] [--no-browser]
    veha serve --root <каталог> [--port 8770] [--no-browser]
               [--system S --env E] [--allow-run]
               [--host 127.0.0.1] [--allowed-host ИМЯ[:ПОРТ] ...]
    veha export-static --root <каталог> --out <каталог> [--system S --env E]
    veha init [--root <каталог проекта>] [--system S] [--env E]

Сервер слушает 127.0.0.1: аутентификации во вебморде пока нет, а показывает она
сведённый граф всей тестируемой системы. Другой адрес (`--host`, нужен внутри
контейнера) — только литералом IPv4 и с предупреждением.
"""

from __future__ import annotations

import argparse
import atexit
import ipaddress
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional
from urllib.parse import urlsplit

from partest_atlas.artifacts import DEFAULT_SCOPE_KEY, Scope

from veha import __version__, scaffold
from veha.core.sources import valid_segment
from veha.listen import HOST, bind_socket

#: Дополнительные значения `Host` из окружения — через запятую, как `--allowed-host`.
ALLOWED_HOSTS_ENV = "VEHA_ALLOWED_HOSTS"
#: Публичный origin за обратным прокси, включая HTTPS. Только один точный адрес.
PUBLIC_ORIGIN_ENV = "VEHA_PUBLIC_ORIGIN"
#: Имя узла в `--allowed-host`: метки DNS через точку или литерал IPv4. Без `*`: шаблон
#: вернул бы DNS rebinding, от которого список и защищает.
_HOST_NAME_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                           r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")


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

    try:
        extra = allowed_hosts_from(args.allowed_host, os.getenv(ALLOWED_HOSTS_ENV, ""),
                                   args.port)
        raw_origin = (args.public_origin if args.public_origin is not None
                      else os.getenv(PUBLIC_ORIGIN_ENV, ""))
        public = public_origin(raw_origin) if raw_origin else ""
    except argparse.ArgumentTypeError as exc:
        print(f"{ALLOWED_HOSTS_ENV}/{PUBLIC_ORIGIN_ENV}: {exc}", file=sys.stderr)
        return 2
    if public:
        extra = sorted(set(extra) | {urlsplit(public).netloc})
    if not ipaddress.ip_address(args.host).is_loopback:
        if args.allow_run:
            # Изменяющие маршруты (запуск набора, очистка, перезапуск, отметки) защищены
            # от браузера — `Origin`/`Referer`; клиент без них (curl, скрипт в сети) с
            # подставленным `Host: 127.0.0.1:<порт>` прошёл бы. Пока нет входа по токену
            # (этап 3), запись и запуск — только на loopback, без ключа-обхода.
            print(f"--allow-run не сочетается с --host {args.host}: входа в вебморду нет, "
                  f"и запускать наборы и чистить отчёты смог бы любой клиент в сети. "
                  f"Запуск из вебморды — только на loopback (--host 127.0.0.1).",
                  file=sys.stderr)
            return 2
        print(f"! слушаю {args.host}:{args.port} — не только loopback. Входа в вебморду "
              f"нет: любой, кто дотянется до этого адреса, увидит граф и логи. Наружу — "
              f"только за своей сетью (контейнер с портом на 127.0.0.1 хоста, VPN).",
              file=sys.stderr)
    return _run(root, scope=scope, allow_run=args.allow_run, port=args.port,
                no_browser=args.no_browser, host=args.host, allowed_hosts=extra,
                public_origin=public)


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
         no_browser: bool, note: str = "", host: str = HOST,
         allowed_hosts: Optional[List[str]] = None, public_origin: str = "") -> int:
    try:
        sock = bind_socket(port, host)
    except OSError as exc:
        print(f"порт {port} на {host} занят или недоступен ({exc.strerror or exc}); "
              f"укажите другой: --port N", file=sys.stderr)
        return 2
    try:
        return _run_bound(root, sock, scope=scope, allow_run=allow_run, port=port,
                          no_browser=no_browser, note=note, host=host,
                          allowed_hosts=allowed_hosts or [], public_origin=public_origin)
    finally:
        sock.close()


def _run_bound(root: Path, sock, *, scope: Optional[Scope], allow_run: bool,
               port: int, no_browser: bool, note: str, host: str = HOST,
               allowed_hosts: Optional[List[str]] = None, public_origin: str = "") -> int:
    from veha.viz.app import create_app  # noqa: PLC0415 — тяжёлый импорт нужен серверу

    app = create_app(root, scope=scope, readonly=not allow_run, port=port,
                     allowed_hosts=allowed_hosts or [], public_origin=public_origin)
    # Запасной путь к остановке дерева процессов, если lifespan не отработал
    # (жёсткий выход интерпретатора): прогон не должен пережить сервер.
    atexit.register(app.state.veha.shutdown)
    url = f"http://{HOST}:{port}"
    print(f"Веха {__version__} → {url}" + (f"  ({note})" if note else ""))
    if host != HOST:
        print(f"  слушает {host}:{port}")
    extra = sorted(app.state.allowed_hosts - _default_hosts(port))
    if extra:
        print("  Host, кроме loopback: " + ", ".join(extra))
    print(f"  корень {root}")
    print("  режим: " + ("запуск прогонов разрешён" if allow_run else "только просмотр"))
    _report(app.state.veha.graph(refresh=True), root, cfg=app.state.veha.cfg)
    from veha import runner as veha_runner  # noqa: PLC0415

    if not any(s.full for s in veha_runner.suites(app.state.veha.cfg)):
        print("  ! в runner.suites нет набора с full: true — тренда среднего покрытия "
              "не будет")
    # Браузер — только когда сервер действительно поднялся (startup uvicorn прошёл).
    opener = None if no_browser else (lambda: _open_browser(url))
    serve_app(app, port, sock=sock, on_started=opener, host=host)
    return 0


def _default_hosts(port: int) -> set:
    from veha.viz.app import default_hosts  # noqa: PLC0415

    return default_hosts(port)


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

    Система и контур — как у всех команд обоих пакетов: ключ, иначе `ATLAS_SYSTEM` /
    `ATLAS_ENV`, иначе `default` — одинаково с `collect` и `serve` (контракт
    `atlas-config`). В файл область не пишется, она нужна только подсказкам.
    """
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"нет каталога: {root}", file=sys.stderr)
        return 2
    chosen = {}
    for key, label, flag, env_name in (("system", "система", args.system, "ATLAS_SYSTEM"),
                                       ("env", "контур", args.env, "ATLAS_ENV")):
        value = flag or os.getenv(env_name) or ""
        if value and not valid_segment(value):
            print(f"{label} {scaffold.one_line(value, 80)!r} (--{key} или {env_name}): "
                  f"допустим один сегмент [A-Za-z0-9._-]+ без '..'", file=sys.stderr)
            return 2
        chosen[key] = value
    p = scaffold.plan(root, system=chosen["system"], env=chosen["env"])
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
    print(f"  область для подсказок: {p.system}/{p.env}")
    services = [s for s in p.spec.services if s.kind]
    if services:
        rel = (scaffold.one_line(p.spec.path.relative_to(root).as_posix(), 120)
               if p.spec.path else "")
        print(f"  спецификации из {rel}: "
              + ", ".join(f"{s.name} ({s.kind})" for s in services))
    read = [n for n in scaffold.SETTINGS_FILES if (root / n).is_file()]
    print("  пути отчётов искались в: " + (", ".join(read) if read else
                                          "нигде — нет ни pytest.ini, ни pyproject.toml, "
                                          "ни tox.ini, ни setup.cfg"))
    for key, found, value in (("coverage", p.coverage, p.coverage_path),
                              ("allure", p.allure, p.allure_path)):
        how = f"из {found.where}" if found.usable else "по умолчанию"
        print(f"  reports.{key}: {value} — {how}")
    print("  база: " + (scaffold.one_line(p.signals.db, 120) if p.signals.db
                        else "признаков не найдено — раздел database оставлен шаблоном"))
    for warning in p.warnings():
        print(f"  ! {scaffold.one_line(warning, 600)}")
    print("Дальше:")
    for i, step in enumerate(scaffold.next_steps(p), 1):
        print(f"  {i}. {step}")
    return 0


def serve_app(app, port: int, *, sock: Optional[Any] = None,
              on_started: Optional[Callable[[], None]] = None, host: str = HOST) -> None:
    """Запустить uvicorn так, чтобы сигнал остановки сначала убивал деревья процессов.

    uvicorn на SIGINT/SIGTERM (и SIGBREAK на Windows) ставит флаг выхода и ждёт, пока
    допишутся идущие запросы. Синхронный `/api/rerun` идёт до десяти минут, и lifespan
    до тех пор не срабатывает: тест продолжал бы гонять стенд после Ctrl+C. Поэтому
    обработчик выхода сначала убивает деревья — тогда перезапуск отвечает сразу, и
    uvicorn завершается штатно.

    *sock* — уже занятый порт (:func:`bind_socket`); *on_started* вызывается, когда
    сервер принял сокет и прошёл startup.
    """
    make_server(app, port, on_started=on_started, host=host).run(
        sockets=[sock] if sock else None)


def make_server(app, port: int, on_started: Optional[Callable[[], None]] = None,
                host: str = HOST):
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

    return VehaServer(uvicorn.Config(app, host=host, port=port, log_level="warning"))


def listen_address(text: str) -> str:
    """Адрес для `--host`: литерал IPv4. Имя узла не принимается — его разрешение было бы
    обращением к DNS; IPv6 — не поддержан (сокет `AF_INET`)."""
    try:
        addr = ipaddress.ip_address(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r}: ожидался адрес IPv4, например 127.0.0.1 или 0.0.0.0 (имя узла не "
            f"принимается — его пришлось бы разрешать через DNS)") from None
    if addr.version != 4:
        raise argparse.ArgumentTypeError(f"{text!r}: IPv6 не поддержан, нужен адрес IPv4")
    return str(addr)


def allowed_host(text: str, port: Optional[int] = None) -> str:
    """Значение `Host` для `--allowed-host`, в нижнем регистре.

    `имя:порт` — как есть; `имя` — с портом сервера (*port*); `имя:` — заголовок без
    порта: так его шлёт браузер на стандартный порт (80, 443) — например, за своим
    обратным прокси. *port* `None` — форма сохраняется, порт подставит
    :func:`allowed_hosts_from`.
    """
    value = text.strip().lower()
    name, sep, tail = value.rpartition(":")
    if not sep:
        name, tail = value, ""
    if tail:
        try:
            number = int(tail)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r}: порт не число") from None
        if not 1 <= number <= 65535:
            raise argparse.ArgumentTypeError(f"{text!r}: порт вне 1..65535")
    if not _HOST_NAME_RE.match(name):
        raise argparse.ArgumentTypeError(
            f"{text!r}: ожидалось имя узла или адрес IPv4, возможно с :порт; шаблоны "
            f"(`*`) не принимаются")
    if tail:
        return f"{name}:{int(tail)}"
    if sep:
        return name if port is not None else f"{name}:"   # без порта — нарочно
    return name if port is None else f"{name}:{port}"


def public_origin(text: str) -> str:
    """Один точный HTTP/HTTPS origin за прокси, без учётной записи, пути и query.

    Стандартный порт опускается: браузер именно так сериализует `Origin`. Имя узла
    проходит ту же проверку, что `--allowed-host`; его нормализованный Host затем
    автоматически добавляется в список разрешённых.
    """
    value = text.strip()
    try:
        parts = urlsplit(value)
        number = parts.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r}: негодный origin ({exc})") from None
    default = {"http": 80, "https": 443}.get(parts.scheme)
    if (default is None or not parts.netloc or parts.username is not None
            or parts.password is not None or parts.path not in ("", "/")
            or parts.query or parts.fragment or parts.netloc.endswith(":")
            or (number is not None and not 1 <= number <= 65535)):
        raise argparse.ArgumentTypeError(
            f"{text!r}: ожидался http(s)://имя[:порт] без userinfo, пути, query и fragment")
    name = allowed_host(parts.hostname or "")
    netloc = name if number in (None, default) else f"{name}:{number}"
    return f"{parts.scheme}://{netloc}"


def allowed_hosts_from(flags: Optional[List[str]], env_value: str, port: int) -> List[str]:
    """Дополнительные `Host` из ключей и из `VEHA_ALLOWED_HOSTS` (через запятую)."""
    raw = list(flags or []) + [x for x in env_value.split(",") if x.strip()]
    return sorted({allowed_host(x, port) for x in raw})


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
    s.add_argument("--port", type=port_number, default=8770, help="порт, 1..65535")
    s.add_argument("--host", type=listen_address, default=HOST,
                   help="адрес IPv4, на котором слушать (по умолчанию 127.0.0.1). Другой "
                        "адрес — для контейнера: входа в вебморду нет, наружу только за "
                        "своей сетью")
    s.add_argument("--allowed-host", action="append", type=lambda t: allowed_host(t),
                   metavar="ИМЯ[:ПОРТ]",
                   help="ещё одно допустимое значение заголовка Host (повторяемый; также "
                        f"{ALLOWED_HOSTS_ENV} через запятую). Без порта — порт сервера; "
                        "`имя:` — заголовок без порта, как его шлёт браузер на 80/443. "
                        "127.0.0.1 и localhost разрешены всегда; --allow-run — только при "
                        "--host на loopback")
    s.add_argument("--public-origin", metavar="HTTP(S)://ИМЯ[:ПОРТ]",
                   help="точный публичный origin за своим HTTPS/HTTP прокси (также "
                        f"{PUBLIC_ORIGIN_ENV}); его Host разрешается автоматически")
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
                                   "перезаписывается.",
                       epilog="Коды возврата: 0 — заготовка записана; 2 — ничего не "
                              "записано: atlas.yaml уже есть, нет каталога, негодное имя "
                              "системы или контура, файл не создать.")
    i.add_argument("--root", default=".",
                   help="корень проекта: confpartest.py, pytest.ini (по умолчанию — текущий)")
    i.add_argument("--system", default="",
                   help="система для подсказок (по умолчанию — ATLAS_SYSTEM, иначе "
                        f"{scaffold.DEFAULT_SCOPE}, как у serve и collect)")
    i.add_argument("--env", default="",
                   help=f"контур для подсказок (по умолчанию — ATLAS_ENV, иначе "
                        f"{scaffold.DEFAULT_SCOPE})")
    i.set_defaults(func=init)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
