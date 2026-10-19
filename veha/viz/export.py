"""Статический снимок вебморды: `veha export-static --root <каталог> --out <каталог>`.

Для витрины, где сервера нет: страница, статика и ответы API, которые интерфейс читает
в режиме просмотра, — JSON-файлами рядом со страницей. Интерфейс узнаёт режим по
`<meta name="veha-mode" content="static">` и читает файлы по относительным путям,
поэтому снимок кладут в любой подкаталог сайта. Действия, которым нужен сервер
(перезапуск кейса, раннер, опрос «Live» и лог контейнера, отметки МР, пересборка
графа), в снимке выключены.

**Снимок публикует всё, что показывает вебморда:** контейнеры и их состояние, логи,
merge request'ы, шаги и вложения тестов, схему базы. Маскирование секретов то же, что у
сервера, — эвристика. Снимок настоящих данных наружу не выкладывают; для витрины есть
вымышленный набор `veha/demo_data/` (`veha demo`).

Ответы берутся у тех же функций, что отвечают живому серверу, — у маршрутов приложения
из :func:`veha.viz.app.create_app`, а не пересобираются здесь. Иначе снимок разошёлся
бы с сервером при первой правке маршрута, и демо показывало бы не то, что видит
пользователь сервера.

Раскладка снимка::

    index.html                   страница с меткой режима
    static/                      скрипты и стили вебморды (белый список STATIC_FILES)
    screens/                     снимки экранов, на которые ссылается граф
    api/graph.json               /api/graph
    api/artifacts.json           /api/artifacts (путь к atlas.yaml — относительный)
    api/node/index.json          id узла → имя файла; api/node/<n>.json — /api/node/<id>
    api/tests.json               /api/tests без фильтра; фильтр — в браузере
    api/test/<файл результата>   /api/test?file=…
    api/attachment/<имя>.json    /api/attachment/<имя> (текст); PNG — файлом без .json
    api/log.json                 /api/log для каждого лога из раздела `logs:`
    api/runner.json              /api/runner/suites: наборы, история, тренд — для чтения
    api/live/containers.json     /api/live/containers — снимок стенда, только чтение
    api/live/mrs.json            /api/live/mrs — очередь МР, только чтение
    api/export.json              метка снимка: что выгружено, что пропущено и почему

Защита. Файлы из данных (экраны, вложения, результаты Allure) копируются только простыми
именами, только обычными файлами внутри своего каталога: символическая ссылка, путь,
уводящий за каталог после разрешения, и PNG без сигнатуры PNG пропускаются с причиной в
`skipped`. Абсолютные пути корня данных и домашнего каталога вычищаются из всех ответов, а
готовый снимок перед публикацией проверяется на них целиком: нашлось — снимок не
записывается. Снимок собирается в соседнем временном каталоге и подменяет прежний
переименованием; прежний снимок узнаётся по содержимому метки, а не по её наличию.

Снимок детерминирован: одинаковые данные дают побайтно одинаковые файлы.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from partest_atlas.artifacts import Scope

from veha import __version__
from veha import config as veha_config
from veha.core import sources as core_sources

STATIC = Path(__file__).resolve().parent / "static"
#: Что из `static/` уезжает в снимок. Белый список, а не «всё из каталога»: случайный
#: файл рядом со статикой (заметки, бэкап редактора) не должен попасть на сайт.
STATIC_FILES = ("api.js", "scene3d.js", "map2d.js", "atlas.js", "dock.js", "style.css")
#: Метка снимка: по ней экспорт узнаёт свой каталог и только его перезаписывает.
MARKER = "api/export.json"
#: То, из чего состоит снимок. Каталог, где есть что-то ещё, — не снимок.
OWNED = ("index.html", "static", "screens", "api")
MODE_META = '<meta name="veha-mode" content="static">'
GENERATOR_RE = re.compile(r"^veha \S+ export-static$")
#: Сколько строк лога отдаёт интерфейс по умолчанию — столько же и в снимке.
LOG_TAIL = 400
#: Предел вложения у сервера (`/api/attachment`): больше — 413 и в снимке.
ATTACHMENT_LIMIT = 4_000_000
#: Предел экрана и файла результата Allure: больше — не снимок, а ошибка данных.
FILE_LIMIT = 8_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: Простое имя файла: без разделителей, без `..`, и не `.`/`..` целиком.
_SAFE_NAME = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]+$")
_TEXT_SUFFIXES = (".json", ".html", ".js", ".css")


class ExportError(RuntimeError):
    """Снимок не может быть записан: каталог чужой, в выходе нашёлся путь машины и т.п."""


class _Skip(Exception):
    """Файл из данных в снимок не берётся; текст — причина для `skipped`."""


@dataclass
class ExportReport:
    out: Path
    files: int = 0
    bytes: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"files": self.files, "bytes": self.bytes, "counts": dict(self.counts),
                "skipped": list(self.skipped)}


# ------------------------------------------------------------------ пути машины

def _needles(root: Path) -> List[Tuple[str, str]]:
    """Что вычищать из ответов и на что менять: корень данных — `.`, дом — `~`.

    Корень раньше дома: он обычно лежит внутри домашнего каталога, и замена дома
    первой оставила бы от корня `~/…` — путь машины в другой записи.
    """
    pairs: List[Tuple[str, str]] = []
    for path, repl in ((root, "."), (Path.home(), "~")):
        for form in {str(path), path.as_posix()}:
            if len(form) > 3:          # «C:\» или «/» вычищать бессмысленно и опасно
                pairs.append((form, repl))
    return pairs


def _scrubber(root: Path) -> Callable[[Any], Any]:
    flags = re.IGNORECASE if os.name == "nt" else 0
    rules = [(re.compile(re.escape(n), flags), r) for n, r in _needles(root)]

    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [scrub(v) for v in obj]
        if isinstance(obj, str):
            for rx, repl in rules:
                obj = rx.sub(lambda _m, r=repl: r, obj)
        return obj

    return scrub


def _leaks(text: str, root: Path) -> bool:
    """Есть ли в тексте путь корня или домашнего каталога — как есть или в JSON."""
    low = text.lower() if os.name == "nt" else text
    for needle, _ in _needles(root):
        for form in (needle, needle.replace("\\", "\\\\")):
            if (form.lower() if os.name == "nt" else form) in low:
                return True
    return False


# ------------------------------------------------------------- каталог снимка

def _is_snapshot(out: Path) -> bool:
    """Прежний снимок: метка разбирается и сказана нашим экспортом, лишнего нет."""
    try:
        meta = json.loads((out / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not (isinstance(meta, dict) and meta.get("mode") == "static"
            and isinstance(meta.get("generator"), str)
            and GENERATOR_RE.match(meta["generator"])):
        return False
    return all(p.name in OWNED for p in out.iterdir())


def _check_out(out: Path, root: Path) -> None:
    """Каталог снимка: несуществующий, пустой или прежний снимок — и только он.

    Чужой непустой каталог не перезаписывается: `--out .` по ошибке стёр бы проект.
    """
    if out == root or root in out.parents or out in root.parents:
        raise ExportError("каталог снимка не должен пересекаться с корнем данных")
    if out.is_symlink():
        raise ExportError("каталог снимка — символическая ссылка")
    if out.exists() and not out.is_dir():
        raise ExportError(f"не каталог: {out.name}")
    if out.is_dir() and any(out.iterdir()) and not _is_snapshot(out):
        raise ExportError(f"каталог {out.name} не пуст и не прежний снимок "
                          f"(метки {MARKER} нет, она чужая или рядом лежит постороннее)")


def _swap(tmp: Path, out: Path) -> None:
    """Подменить снимок готовым: прежний уходит в сторону, новый встаёт на его место."""
    old = out.parent / f".{out.name}.veha-old"
    if old.exists():
        shutil.rmtree(old)
    if out.exists():
        out.rename(old)
    try:
        tmp.rename(out)
    except OSError:
        if old.exists() and not out.exists():
            old.rename(out)            # вернуть прежний, а не оставить пустое место
        raise
    if old.exists():
        shutil.rmtree(old)


# --------------------------------------------------------------- файлы данных

def _read_inside(base: Path, name: str, limit: int = FILE_LIMIT) -> bytes:
    """Байты файла *name* прямо в *base* — или `_Skip` с причиной.

    Как у `/api/attachment` сервера: простое имя и путь после разрешения внутри каталога.
    Сверх того — не символическая ссылка (снимок уезжает наружу, и ссылка на файл вне
    данных опаснее, чем на сервере за loopback) и PNG с сигнатурой PNG.
    """
    if not _SAFE_NAME.match(name or ""):
        raise _Skip("недопустимое имя")
    path = base / name
    if path.is_symlink():
        raise _Skip("символическая ссылка")
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _Skip("файла нет")
    if real.parent != base.resolve() or not real.is_file():
        raise _Skip("вне своего каталога")
    if real.stat().st_size > limit:
        raise _Skip(f"больше {limit} байт")
    data = real.read_bytes()
    if name.lower().endswith(".png") and not data.startswith(PNG_SIGNATURE):
        raise _Skip("не PNG")
    return data


def _page() -> bytes:
    html = (STATIC / "index.html").read_bytes().decode("utf-8").replace("\r\n", "\n")
    marker = '<meta charset="utf-8">'
    if marker not in html:
        raise ExportError("index.html: нет <meta charset> — некуда поставить метку режима")
    return html.replace(marker, marker + "\n" + MODE_META, 1).encode("utf-8")


def _endpoints(app) -> Dict[str, Callable[..., Any]]:
    return {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}


def _attachment_sources(detail: Any) -> List[str]:
    """Имена вложений теста и его шагов. Чужие данные: всё, что не того типа, — мимо."""
    found: List[str] = []

    def take(items: Any) -> None:
        for a in items if isinstance(items, list) else []:
            src = a.get("source") if isinstance(a, dict) else None
            if isinstance(src, str) and src:
                found.append(src)

    def walk(steps: Any, depth: int = 0) -> None:
        if depth > 64:
            return
        for st in steps if isinstance(steps, list) else []:
            if isinstance(st, dict):
                take(st.get("attachments"))
                walk(st.get("steps"), depth + 1)

    if isinstance(detail, dict):
        take(detail.get("attachments"))
        walk(detail.get("steps"))
    return found


def _relative_assets(obj: Any) -> Any:
    """`/screens/x.png` → `screens/x.png`: снимок живёт в подкаталоге сайта, и путь от
    корня домена указал бы мимо."""
    if isinstance(obj, dict):
        return {k: _relative_assets(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_relative_assets(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("/screens/"):
        return obj[1:]
    return obj


# --------------------------------------------------------------------- экспорт

def export_static(root: Path, out: Path, *, scope: Optional[Scope] = None) -> ExportReport:
    """Записать снимок вебморды над *root* в *out*. Возвращает, что записано."""
    root = Path(root).resolve()
    out = Path(out).absolute()
    if out.exists() and not out.is_symlink():
        out = out.resolve()
    if not root.is_dir():
        raise ExportError("нет каталога данных")
    _check_out(out, root)

    tmp = out.parent / f".{out.name}.veha-tmp"
    if tmp.exists():
        shutil.rmtree(tmp)            # обломок прошлого экспорта: имя наше и служебное
    tmp.mkdir(parents=True)
    try:
        report = _build(root, tmp, scope)
        for f in tmp.rglob("*"):
            if f.is_file() and f.suffix in _TEXT_SUFFIXES \
                    and _leaks(f.read_text(encoding="utf-8"), root):
                raise ExportError(f"в снимке путь машины: {f.relative_to(tmp).as_posix()} — "
                                  "снимок не записан")
        _swap(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    report.out = out
    return report


def _build(root: Path, dest: Path, scope: Optional[Scope]) -> ExportReport:
    from veha.viz.app import create_app  # noqa: PLC0415 — тяжёлый импорт нужен только здесь

    report = ExportReport(out=dest)
    scrub = _scrubber(root)

    def write(rel: str, data: bytes) -> None:
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        report.files += 1
        report.bytes += len(data)

    def dump(obj: Any) -> bytes:
        """JSON без пробелов и с кириллицей как есть: снимок кладут на сайт, вес важен.
        Пути машины вычищаются из каждого ответа — `config.error`, `problems`,
        `warnings`, заметки логов несут их как есть."""
        return json.dumps(scrub(jsonable_encoder(obj)), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")

    def count(key: str, n: int = 1) -> None:
        report.counts[key] = report.counts.get(key, 0) + n

    app = create_app(root, scope=scope, readonly=True)
    srv = app.state.veha
    api = _endpoints(app)
    try:
        g = srv.graph(refresh=True)

        write("index.html", _page())
        for name in STATIC_FILES:
            # Переводы строк — как в репозитории, а не как в рабочей копии: из чекаута с
            # CRLF и из `git archive` снимок обязан выйти побайтно одинаковым.
            write(f"static/{name}", (STATIC / name).read_bytes().replace(b"\r\n", b"\n"))

        write("api/graph.json", dump(_relative_assets(api["/api/graph"](refresh=False))))
        artifacts = api["/api/artifacts"](refresh=False)
        artifacts["config"]["path"] = veha_config.CONFIG_NAME
        write("api/artifacts.json", dump(artifacts))

        shots = core_sources.screens_dir(srv.cfg)
        shots_ok = shots is not None and root in [shots.resolve(), *shots.resolve().parents]
        for s in g.get("screens") or []:
            name = Path(str(s.get("image") or "")).name
            if not name:
                continue
            try:
                if not shots_ok:
                    raise _Skip("каталог экранов вне корня данных")
                write(f"screens/{name}", _read_inside(shots, name))
                count("screens")
            except _Skip as why:
                report.skipped.append(f"экран {name}: {why}")

        index: Dict[str, str] = {}
        for i, node in enumerate(g["nodes"]):
            fname = f"{i}.json"
            index[node["id"]] = fname
            write(f"api/node/{fname}",
                  dump(_relative_assets(api["/api/node/{node_id:path}"](node["id"]))))
        write("api/node/index.json", dump(index))
        count("nodes", len(index))

        tests = api["/api/tests"](status="", q="", limit=2000)
        if tests["total"] > len(tests["tests"]):
            report.skipped.append(f"список тестов урезан сервером: {len(tests['tests'])} "
                                  f"из {tests['total']}")
        write("api/tests.json", dump(tests))

        sources: List[str] = []
        for t in g.get("tests") or []:
            fname = t.get("file") or ""
            try:
                _read_inside(srv.allure_dir, fname)       # тот же замок, что у вложений
                detail = api["/api/test"](nodeid="", file=fname)
            except _Skip as why:
                report.skipped.append(f"тест {fname!r}: {why}")
                continue
            except HTTPException as exc:
                report.skipped.append(f"тест {fname}: {exc.detail}")
                continue
            write(f"api/test/{fname}", dump(detail))
            count("tests")
            sources.extend(_attachment_sources(detail))

        for source in sorted(set(sources)):
            try:
                data = _read_inside(srv.allure_dir, source, ATTACHMENT_LIMIT)
                if source.lower().endswith(".png"):
                    write(f"api/attachment/{source}", data)
                else:
                    write(f"api/attachment/{source}.json",
                          dump(api["/api/attachment/{source}"](source)))
                count("attachments")
            except _Skip as why:
                report.skipped.append(f"вложение {source!r}: {why}")
            except HTTPException as exc:
                report.skipped.append(f"вложение {source}: {exc.detail}")

        logs: Dict[str, Any] = {}
        names = list(dict(srv.cfg.section("logs")))
        for name in names:
            try:
                logs[name] = api["/api/log"](name=name, tail=LOG_TAIL)
            except HTTPException as exc:
                report.skipped.append(f"лог {name}: {exc.detail}")
        first = next(iter(logs), "")
        write("api/log.json", dump({
            "first": first, "names": list(logs),
            "logs": logs or {"": api["/api/log"](name="", tail=LOG_TAIL)}}))
        count("logs", len(logs))

        # «Live» в снимке — только чтение: состояние контейнеров и очередь МР на момент
        # снимка. Лог контейнера «в реальном времени» не выгружается — он требует сервера.
        containers = api["/api/live/containers"]()
        mrs = api["/api/live/mrs"]()
        # Сервер ставит очереди время чтения (`time.time()`); у снимка это время снимка
        # контейнеров — иначе одинаковые данные давали бы разные файлы.
        if "at" in mrs:
            mrs["at"] = containers.get("at")
        write("api/live/containers.json", dump(containers))
        write("api/live/mrs.json", dump(mrs))

        runner = api["/api/runner/suites"]()
        # Снимок — только просмотр: прогона нет и не будет, кнопки выключены.
        runner.update({"readonly": True, "busy": False, "run": None})
        write("api/runner.json", dump(runner))

        manifest = {"generator": f"veha {__version__} export-static", "mode": "static",
                    **report.as_dict()}
        write(MARKER, dump(manifest))
    finally:
        srv.shutdown()
    return report
