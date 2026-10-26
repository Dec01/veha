"""`veha init` — заготовка `atlas.yaml` для существующего проекта на pytest + partest.

Пишет один файл в корне проекта и ничего больше. Существующий `atlas.yaml` не трогает:
в нём ручная работа (имена переменных подключения, контейнеры, наборы), и перезаписать её
молча — худший исход команды, которая должна экономить время.

**Код проекта не выполняется.** Спецификации объявлены в `confpartest.swagger_files`, но
`confpartest.py` — модуль проекта: импорт выполнил бы всё, что стоит у него на верхнем
уровне, в том числе загрузку спецификации по адресу. Это та же дыра в красной линии «ноль
исходящего трафика», что закрывал пол partest 2.6.0. Поэтому файл разбирается деревом
(`ast`) и из него берётся только литерал `swagger_files = {"имя": ["local"|"url", место]}`;
всё, что не литерал, становится заготовкой с подсказкой, что вписать.

Адрес спецификации попадает в `atlas.yaml` (файл лежит в репозитории) только без учётных
данных, параметров запроса и фрагмента, остаток проходит через `veha.redact.text`.

Отчёты прогона ищутся в настройках pytest проекта текстом, без импорта и без плагинов:
`PARTEST_COVERAGE_JSON` (pytest-env в `pytest.ini`/`pyproject.toml`, `setenv` в `tox.ini`)
и `--alluredir` (`addopts`). Не нашлось — умолчания раскладки корня.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

from veha import __version__, redact
from veha.config import CONFIG_NAME, DEFAULT_REPORTS

CONFPARTEST = "confpartest.py"
#: Виды источника, которые принимает partest (`validate_confpartest`): `file` и `path` —
#: то же, что `local`.
LOCAL_KINDS = ("local", "file", "path")
URL_KIND = "url"
#: Контур по умолчанию: заготовка для машины разработчика.
DEFAULT_ENV = "local"
#: Где pytest ищет настройки — в том же порядке, в каком он их выбирает.
SETTINGS_FILES = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
#: Сколько символов значения из чужого файла попадает в комментарий.
SHOWN_LIMIT = 200

_SEGMENT_BAD = re.compile(r"[^A-Za-z0-9._-]+")
#: Символы, которые разрывают строку комментария YAML (и всё прочее управляющее).
_LINE_BREAKERS = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")
#: `KEY=путь` (pytest-env в ini, setenv tox), `KEY = "путь"` и `KEY = { value = "путь" }`
#: (таблица pytest-env в pyproject.toml).
_COVERAGE_RE = re.compile(
    r"PARTEST_COVERAGE_JSON[\"']?\s*[=:]\s*(?:\{\s*value\s*=\s*)?[\"']?([^\"'\s,\]]+)")
#: `--alluredir=путь`, `--alluredir путь` и список addopts `"--alluredir", "путь"`.
_ALLURE_RE = re.compile(r"--alluredir(?:\s*=\s*|\s+|[\"']\s*,\s*)[\"']?([^\"'\s,\]]+)")
_TOXINIDIR = re.compile(r"^\{toxinidir\}[\\/]?")


# --------------------------------------------------------------------- вывод


def one_line(value: Any, limit: int = SHOWN_LIMIT) -> str:
    """Значение из чужого файла — в одну безопасную строку комментария."""
    s = _LINE_BREAKERS.sub("?", str(value))
    return s if len(s) <= limit else s[:limit - 1] + "…"


def quoted(value: str) -> str:
    """Скаляр YAML в двойных кавычках. Строка JSON — допустимый скаляр YAML; символы,
    которые YAML считает переводом строки, экранируются вместе со всем не-ASCII."""
    out = json.dumps(value, ensure_ascii=False)
    return json.dumps(value) if _LINE_BREAKERS.search(out) else out


def safe_url(value: str) -> str:
    """Адрес без учётных данных, параметров запроса и фрагмента.

    Остаются схема, узел с портом и путь; путь проходит через `veha.redact.text` —
    вдруг секрет записан в нём парой `ключ=значение`. Адрес, который не разбирается, не
    показывается вовсе: показать его как есть — значит показать и то, что в нём спрятано.
    """
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return "<адрес не разобран>"
    host = parts.netloc.rpartition("@")[2]
    if not parts.scheme or not host:
        return "<адрес не разобран>"
    return one_line(redact.text(f"{parts.scheme}://{host}{parts.path}"))


def segment(value: str, fallback: str = "project") -> str:
    """Имя каталога — в сегмент области `[A-Za-z0-9._-]+` без `..`."""
    s = _SEGMENT_BAD.sub("-", value).strip("-.")
    s = re.sub(r"\.{2,}", ".", s)
    return s or fallback


# --------------------------------------------------------------- confpartest


@dataclass
class Service:
    """Одна запись `swagger_files`, как прочитана из литерала."""

    name: str
    kind: str = ""                  # local | url; пусто — не литерал
    shown: str = ""                 # место для комментария: путь как объявлен, адрес — без секретов
    note: str = ""                  # что с записью не так


@dataclass
class Spec:
    """Что удалось узнать о спецификациях проекта, не выполняя его код."""

    found: bool = False             # confpartest.py есть
    literal: bool = False           # swagger_files — литерал словаря (целиком или по записям)
    services: List[Service] = field(default_factory=list)
    problem: str = ""               # почему спецификаций не узнать
    notes: List[str] = field(default_factory=list)


def _is_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "swagger_files"


def _assigned(stmt: ast.stmt) -> Optional[ast.AST]:
    """Значение, присвоенное `swagger_files` этим оператором верхнего уровня."""
    if isinstance(stmt, ast.Assign) and any(_is_name(t) for t in stmt.targets):
        return stmt.value
    if isinstance(stmt, ast.AnnAssign) and _is_name(stmt.target) and stmt.value is not None:
        return stmt.value
    return None


#: Методы словаря, которые его меняют; `.get`, `.items` и прочее чтение — не изменение.
_MUTATORS = frozenset({"update", "setdefault", "pop", "popitem", "clear", "__setitem__",
                       "__delitem__", "__ior__"})


def _touched_elsewhere(tree: ast.Module, top: List[ast.stmt]) -> bool:
    """Меняется ли `swagger_files` где-то, кроме присваиваний верхнего уровня:
    `swagger_files["x"] = …`, `.update(…)`, присваивание под `if`/`try`, `+=`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "swagger_files" \
                and isinstance(node.ctx, (ast.Store, ast.Del)):
            if not any(node in ast.walk(stmt) for stmt in top):
                return True
        if isinstance(node, ast.Subscript) and _is_name(node.value) \
                and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and _is_name(node.func.value) and node.func.attr in _MUTATORS:
            return True
    return False


def _entry(name: str, node: ast.AST, base: Path) -> Service:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return Service(name, note="не литерал — впишите вид и место")
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return Service(name, note="ожидалось [вид, путь_или_адрес]")
    kind, where = value[0], value[1]
    if not isinstance(kind, str) or not isinstance(where, str) or not where.strip():
        return Service(name, note="ожидалось [вид, путь_или_адрес] строками")
    kind = kind.lower()
    if kind == URL_KIND:
        return Service(name, URL_KIND, safe_url(where))
    if kind not in LOCAL_KINDS:
        return Service(name, note=f"вид {one_line(kind, 40)!r} — partest ждёт local или url")
    if "://" in where:
        # шаблон partest-gen до исправления писал "local" и для адреса
        return Service(name, "local", safe_url(where),
                       note="вид local, но это адрес — partest прочитает его как путь к "
                            "файлу; поправьте вид на url в confpartest.py")
    note = ""
    try:
        if not (base / where).is_file():
            note = "файла нет (путь — от каталога confpartest.py)"
    except (OSError, ValueError):
        note = "путь не проверить"
    return Service(name, "local", one_line(where), note=note)


def read_spec(root: Path) -> Spec:
    """`swagger_files` из `confpartest.py` в *root* — деревом разбора, без импорта."""
    path = root / CONFPARTEST
    if not path.is_file():
        return Spec(problem=f"нет {CONFPARTEST} в корне проекта")
    spec = Spec(found=True)
    try:
        tree = ast.parse(path.read_bytes(), filename=CONFPARTEST)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        spec.problem = f"{CONFPARTEST} не разбирается: {type(exc).__name__}"
        return spec
    top = [s for s in tree.body if _assigned(s) is not None]
    if not top:
        spec.problem = f"в {CONFPARTEST} нет присваивания swagger_files на верхнем уровне"
        return spec
    if len(top) > 1:
        spec.notes.append("swagger_files присваивается несколько раз — взято последнее")
    if _touched_elsewhere(tree, top):
        spec.notes.append("swagger_files меняется и кодом (под условием, по ключу или "
                          "методом) — здесь только литерал верхнего уровня")
    value = _assigned(top[-1])
    if not isinstance(value, ast.Dict):
        spec.problem = (f"swagger_files — не литерал словаря ({type(value).__name__}): "
                        "чтобы его прочитать, нужно выполнить код проекта")
        return spec
    spec.literal = True
    for i, (key, node) in enumerate(zip(value.keys, value.values), 1):
        if key is None:
            spec.notes.append("в swagger_files есть **распаковка — её записи не прочитаны")
            continue
        try:
            name = ast.literal_eval(key)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            spec.services.append(Service(f"<запись {i}>", note="имя — не литерал"))
            continue
        if not isinstance(name, str):
            spec.services.append(Service(f"<запись {i}>", note="имя — не строка"))
            continue
        spec.services.append(_entry(one_line(name, 80), node, path.parent))
    if not spec.services and not spec.notes:
        spec.problem = "swagger_files пуст"
    return spec


# ------------------------------------------------------------------ отчёты


@dataclass
class Found:
    """Путь к отчёту, найденный в настройках проекта."""

    path: str = ""                  # пусто — не найден или не развернуть
    where: str = ""                 # файл настроек
    raw: str = ""                   # как записан, если развернуть нельзя
    #: Путь абсолютный или выходит из корня. В `reports` такой не пишется действующим:
    #: корень — единица, которую видит сервер (в контейнере смонтирован только он, и
    #: абсолютного пути хоста там нет), а `atlas.yaml` с путём машины в репозитории
    #: ломается у второго человека.
    outside: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.path) and not self.outside


def inside_root(value: str) -> Tuple[bool, str]:
    """Относительный путь внутри корня — и его вид с прямыми косыми чертами."""
    norm = value.replace("\\", "/")
    if norm.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", norm):
        return False, norm
    norm = posixpath.normpath(norm)
    if norm == ".." or norm.startswith("../"):
        return False, norm
    return True, norm


def _setting_lines(root: Path) -> List[Tuple[str, str]]:
    """Строки файлов настроек pytest без строк-комментариев: (файл, строка)."""
    out: List[Tuple[str, str]] = []
    for name in SETTINGS_FILES:
        f = root / name
        try:
            text = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
        except OSError:
            continue
        for line in text.splitlines():
            if not line.lstrip().startswith(("#", ";")):
                out.append((name, line))
    return out


def _found(value: str, where: str) -> Found:
    value = _TOXINIDIR.sub("", value.strip())
    if not value or any(c in value for c in "{}$%") or "\x00" in value:
        # подстановка tox или оболочки — разворачивать её здесь значило бы гадать
        return Found(where=where, raw=one_line(value, 80))
    inside, norm = inside_root(value)
    return Found(path=norm if inside else one_line(value), where=where, outside=not inside)


def find_reports(root: Path) -> Tuple[Found, Found]:
    """Где прогон проекта кладёт `coverage.json` и результаты Allure — по его настройкам."""
    coverage, allure = Found(), Found()
    for where, line in _setting_lines(root):
        if not (coverage.path or coverage.raw):
            m = _COVERAGE_RE.search(line)
            if m:
                coverage = _found(m.group(1), where)
        if not (allure.path or allure.raw):
            m = _ALLURE_RE.search(line)
            if m:
                allure = _found(m.group(1), where)
    return coverage, allure


# ----------------------------------------------------------------- заготовка


@dataclass
class Plan:
    """Всё, из чего собирается файл и итог в терминале."""

    root: Path
    system: str
    env: str
    spec: Spec
    coverage: Found
    allure: Found

    @property
    def coverage_path(self) -> str:
        return self.coverage.path if self.coverage.usable else DEFAULT_REPORTS["coverage"]

    @property
    def allure_path(self) -> str:
        return self.allure.path if self.allure.usable else DEFAULT_REPORTS["allure"]

    def warnings(self) -> List[str]:
        """Что человеку стоит знать сразу: печатается после записи с «!»."""
        out: List[str] = []
        spec = self.spec
        if not spec.found:
            out.append(f"{spec.problem} — контракт OpenAPI снимать неоткуда; раздел "
                       "в atlas.yaml — заготовка")
        elif spec.problem:
            out.append(f"{spec.problem} — в atlas.yaml заготовка, что вписать")
        out += list(spec.notes)
        out += [f"swagger_files[{s.name}]: {s.note}" for s in spec.services if s.note]
        for key, found in (("coverage", self.coverage), ("allure", self.allure)):
            if found.outside:
                out.append(f"reports.{key}: в {found.where} путь вне корня ({found.path}) — "
                           f"записан закомментированным: в контейнере Вехи смонтирован "
                           f"только корень, а путь машины в atlas.yaml ломается у других; "
                           f"держите отчёт внутри корня")
            elif found.raw:
                out.append(f"reports.{key}: в {found.where} подстановка {found.raw!r} — "
                           f"впишите путь сами")
        return out


def plan(root: Path, *, system: str = "", env: str = "") -> Plan:
    root = Path(root).resolve()
    coverage, allure = find_reports(root)
    return Plan(root=root, system=system or segment(root.name), env=env or DEFAULT_ENV,
                spec=read_spec(root), coverage=coverage, allure=allure)


def _report_lines(found: Found, key: str, value: str, what: str, default_note: str) -> List[str]:
    """Ключ раздела `reports` с комментарием, откуда он. Путь вне корня — только
    закомментированным: сервер возьмёт умолчание в корне."""
    if found.outside:
        return [f"  # {what}; в {found.where} путь вне корня: в контейнере Вехи",
                "  # смонтирован только корень, а путь машины в репозитории ломается у",
                "  # других. Перенесите отчёт внутрь корня и впишите путь вместо:",
                f"  # {key}: {quoted(found.path)}"]
    if found.path:
        note = f"найдено в {found.where}"
    elif found.raw:
        note = f"в {found.where} записано {found.raw!r} — подстановку не разворачиваю, впишите путь"
    else:
        note = default_note
    return [f"  # {what}; {note}", f"  {key}: {quoted(value)}"]


def _spec_block(spec: Spec) -> List[str]:
    out = ["# ---------------------------------------------------------------------",
           "# Контракт OpenAPI. Спецификации объявлены в confpartest.swagger_files;",
           "# снимает их  partest-atlas collect openapi  в окружении проекта (он",
           "# импортирует confpartest.py, как pytest). veha init код проекта не",
           "# выполняет и прочитал только литерал. Ниже — для справки, адреса без",
           "# учётных данных и параметров запроса.",
           "# ---------------------------------------------------------------------"]
    if spec.problem:
        out.append(f"# ! {one_line(spec.problem)}")
    for note in spec.notes:
        out.append(f"# ! {one_line(note)}")
    for s in spec.services:
        line = f"#   {s.name}: {s.kind} {s.shown}" if s.kind else f"#   {s.name}: ?"
        out.append(line + (f"   ! {s.note}" if s.note else ""))
    if not spec.literal or any(not s.kind for s in spec.services):
        out += ["# Что вписать: в confpartest.py — swagger_files = {\"имя\": [\"local\", \"путь\"]}",
                "# или [\"url\", \"адрес\"]; если источник вычисляется (переменная окружения,",
                "# функция) — оставьте как есть и впишите сюда для справки:",
                "#   имя: url|local  адрес_без_токена_или_путь"]
    return out


def render(p: Plan) -> str:
    """Текст `atlas.yaml`. Значения — скалярами в кавычках, чужой текст — только в
    комментариях одной строкой."""
    lines: List[str] = [
        f"# atlas.yaml — заготовка veha init {__version__}.",
        "#",
        "# Один файл на агент сбора (partest-atlas) и на сервер (veha). Каждый раздел",
        "# необязателен: нет раздела — нет сборщика или пусто на карте.",
        "#",
        "# Область (система и контур) в файл не пишется — её задаёт окружение, одно на",
        "# прогон pytest, сборщики и сервер:",
        f"#   ATLAS_SYSTEM={p.system}  ATLAS_ENV={p.env}",
        "",
        "project:",
        f"  name: {quoted(p.system)}",
        "",
        "# Куда агент пишет артефакты: <artifacts_dir>/<система>/<контур>/.",
        "artifacts_dir: atlas",
        "",
        "# Отчёты прогона — относительно этого каталога и внутри него.",
        "reports:",
        *_report_lines(p.coverage, "coverage", p.coverage_path, "отчёт покрытия partest",
                       "в настройках не найден — partest пишет его, если задана "
                       "PARTEST_COVERAGE_JSON"),
        *_report_lines(p.allure, "allure", p.allure_path, "результаты allure-pytest",
                       "в addopts нет --alluredir — добавьте его или ключ в команду"),
        "",
        "# Наборы для запуска из вебморды (veha serve --allow-run). Сервер зовёт",
        "# python -m pytest <args> в этом каталоге; набор с full: true даёт тренд",
        "# среднего покрытия. Пустой args — testpaths и addopts проекта.",
        "runner:",
        "  suites:",
        "    full:",
        "      label: \"Весь набор\"",
        "      args: []",
        "      full: true",
        "",
    ]
    lines += _spec_block(p.spec)
    lines += [
        "",
        "# ---------------------------------------------------------------------",
        "# Подключение к базе — задаёт человек. Строку подключения сюда не писать:",
        "# файл называет переменную окружения. Пользователь базы — только на",
        "# чтение: сборщик схемы (partest-atlas collect db-schema) читает каталог.",
        "# ---------------------------------------------------------------------",
        "# database:",
        "#   dsn_env: DATABASE_URL          # или connect: \"пакет.модуль:функция\"",
        "#   schemas: auto                  # все несистемные схемы; или список [public]",
        "",
        "# Позже, по мере надобности:",
        "#",
        "# containers:                      # кто обслуживает какие пути",
        "#   api:",
        "#     label: \"backend\"",
        "#     kind: service                # service | worker | external | auth | store | bus",
        "#     default: true                # всё, что не совпало с match других",
        "#     match: [\"/api/*\"]",
        "#     container_names: [api]       # имена в среде запуска",
        "#",
        "# container_logs:                  # partest-atlas collect container-logs",
        "#   source: \"пакет.модуль:функция\"  # чем читать журналы контейнеров",
        "#",
        "# logs:                            # журналы во вкладке логов: подпись → путь",
        "#   Прогон pytest: logs/pytest.log",
        "#",
        "# screens:                         # снимки экранов UI-тестов",
        "#   manifest: screens/manifest.json",
        "#   images_dir: screens",
    ]
    return "\n".join(lines) + "\n"


class Exists(RuntimeError):
    """`atlas.yaml` уже есть — заготовка не пишется."""


def write(p: Plan) -> Path:
    """Записать заготовку. Существующий файл (и даже ссылку на несуществующий) не трогает:
    открытие с `x` создаёт файл или отказывает — без окна между проверкой и записью."""
    target = p.root / CONFIG_NAME
    text = render(p)
    try:
        with open(target, "x", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except FileExistsError:
        raise Exists(str(target)) from None
    return target


def _arg(value: Any) -> str:
    """Аргумент команды для показа: с пробелом — в кавычках."""
    s = str(value)
    return f'"{s}"' if any(c.isspace() for c in s) else s


def next_steps(p: Plan) -> List[str]:
    """Что делать после заготовки — командами, по порядку."""
    root = _arg(p.root)
    # и записи, которые init не прочитал (не литерал): collect их разрешит, импортировав
    # confpartest.py; без имени (`<запись N>`) назвать ключ нечем
    services = [s.name for s in p.spec.services if not s.name.startswith("<")]
    steps = [
        "в окружении проекта (там, где pytest):  pip install \"partest-atlas[postgres]\""
        "  (команды collect — с partest-atlas 0.3.0)",
        f"область — в окружение прогона, сборщиков и сервера:  ATLAS_SYSTEM={p.system}  "
        f"ATLAS_ENV={p.env}",
        "база: раскомментируйте раздел database в atlas.yaml, строку подключения — в "
        "переменную DATABASE_URL; пользователь — только на чтение",
    ]
    if len(services) > 1:
        steps += [f"контракт:  partest-atlas collect openapi --root {root} --service {_arg(name)}"
                  for name in services]
    else:
        steps.append(f"контракт:  partest-atlas collect openapi --root {root}")
    steps.append(f"схема базы:  partest-atlas collect db-schema --root {root}")
    # Ключ и переменная — только там, где настройки проекта сами их не задают (или
    # задают путь вне корня: тогда отчёт должен лечь туда, где его ждёт сервер).
    run = "pytest"
    if not p.allure.usable:
        run += f" --alluredir {p.allure_path}"
    if not p.coverage.usable:
        run = f"PARTEST_COVERAGE_JSON={p.coverage_path} {run}"
    steps.append(f"прогон в каталоге проекта:  {run}")
    steps.append(f"карта (из окружения Вехи):  veha serve --root {root} --system {p.system} "
                 f"--env {p.env}")
    return steps
