"""`veha init` — заготовка `atlas.yaml` для существующего проекта на pytest + partest.

Пишет один файл в корне проекта и ничего больше. Существующий `atlas.yaml` не трогает:
в нём ручная работа (имена переменных подключения, контейнеры, наборы), и перезаписать её
молча — худший исход команды, которая должна экономить время.

**Код проекта не выполняется.** Спецификации объявлены в `confpartest.swagger_files`, но
`confpartest.py` — модуль проекта: импорт выполнил бы всё, что стоит у него на верхнем
уровне, в том числе загрузку спецификации по адресу. Это та же дыра в красной линии «ноль
исходящего трафика», что закрывал пол partest 2.6.0. Поэтому файл разбирается деревом
(`ast`) и из него берётся только литерал `swagger_files = {"имя": ["local"|"url", место]}`;
всё, что не литерал, становится заготовкой с подсказкой, что вписать и куда. Разбор —
один и только в отдельном процессе (`veha/_spec_child.py`): на Python 3.10 `ast.parse` на
глубоко вложенном выражении роняет интерпретатор целиком, а не бросает исключение, и
основной процесс файл не разбирает вовсе — он получает от дочернего готовый ответ JSON.

Каталоги и файлы проекта, которые оказались ссылкой (символической или junction), не
открываются и не обходятся: ссылка может вести за пределы корня, в том числе на
сетевой ресурс, и даже проверка существования стала бы обращением по сети.

**Всё из файлов проекта — чужой текст.** Он ограничен по размеру до чтения, в `atlas.yaml`
попадает только комментарием в одну строку без управляющих и bidi-символов, а значения —
скалярами в кавычках с экранированием всего вне печатного ASCII. Путь из `confpartest.py`
проверяется на диске, только если он относительный и внутри каталога `confpartest.py`:
сетевой путь (UNC) на Windows при проверке существования обратился бы по сети. Адрес
спецификации пишется без учётных данных, запроса и фрагмента; если адрес разобрать
однозначно нельзя (сырые `/ ? #` в пароле), он не показывается вовсе.

Отчёты прогона ищутся в настройках pytest проекта текстом, без импорта и без плагинов:
`PARTEST_COVERAGE_JSON` (pytest-env в `pytest.ini`/`pyproject.toml`, `setenv` в `tox.ini`)
и `--alluredir` (`addopts`). Не нашлось — умолчания раскладки корня.
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
# Только разбор файла дочерним интерпретатором (`_parse_isolated`): свой код, без оболочки.
import subprocess  # nosec B404
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from partest_atlas.artifacts import DEFAULT_SCOPE_KEY

from veha import __version__, redact
from veha.config import CONFIG_NAME, DEFAULT_REPORTS, is_link, local_to, report_path_problem

CONFPARTEST = "confpartest.py"
#: Виды источника, которые принимает partest (`validate_confpartest`): `file` и `path` —
#: то же, что `local`.
LOCAL_KINDS = ("local", "file", "path")
URL_KIND = "url"
#: Система и контур, если ни ключа, ни `ATLAS_SYSTEM`/`ATLAS_ENV`: то же, что у всех
#: команд обоих пакетов (контракт `atlas-config`), иначе собранное init-подсказками
#: окажется в одной области, а `veha serve` без ключей покажет другую.
DEFAULT_SCOPE = DEFAULT_SCOPE_KEY
#: Код дочернего разбора `confpartest.py` — исходник модуля, передаётся через `-c`.
_CHILD_SOURCE = Path(__file__).with_name("_spec_child.py")
#: Где pytest ищет настройки — в том же порядке, в каком он их выбирает.
SETTINGS_FILES = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
#: Где искать зависимости проекта — только имена пакетов, текстом.
DEPENDENCY_GLOBS = ("requirements*.txt", "requirements/*.txt", "pyproject.toml", "setup.cfg",
                    "setup.py", "Pipfile", "tox.ini")
#: Больше этого файл проекта не читается: всё, что init читает, — короткие файлы
#: настроек, а чужой файл в сотни мегабайт не должен стоить памяти и времени.
READ_LIMIT = 256 * 1024
#: Сколько символов значения из чужого файла попадает в комментарий.
SHOWN_LIMIT = 200
#: Сколько ждать дочерний разбор `confpartest.py`.
PARSE_TIMEOUT = 30
#: Подкаталоги, где `confpartest.py` не ищется: окружения, сборка, кеши.
_SKIP_DIRS = {"venv", "env", "node_modules", "site-packages", "build", "dist", "__pycache__"}

_SEGMENT_BAD = re.compile(r"[^A-Za-z0-9._-]+")
#: Имя, которое можно подставить в команду оболочки как есть — в любой из трёх оболочек.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+\Z")
#: Путь отчёта, годный и в YAML, и в команду любой оболочки без кавычек.
_SAFE_PATH = re.compile(r"^[\w./+-]+\Z")
#: Путь каталога для печатаемой команды — `_SAFE_PATH` плюс то, без чего не бывает пути
#: Windows (`C:`, `\`), и пробел (тогда путь в двойных кавычках). Ни `$`, ни обратной
#: кавычки, ни `%`, ни `!`, ни кавычек: внутри двойных кавычек их разворачивают bash,
#: PowerShell или cmd.
_SAFE_DIR = re.compile(r"^[\w./\\:+ -]+\Z")
DIR_PLACEHOLDER = "<путь к каталогу>"
#: Категории Unicode, которым не место в строке комментария и терминала: управляющие C0/C1
#: (в том числе NEL — перевод строки для YAML), форматирующие (нулевая ширина, bidi,
#: U+2060–2064, теги U+E0000–E007F, BOM), разделители строк и абзацев, суррогаты.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
#: `KEY=путь` (pytest-env в ini, setenv tox), `KEY = "путь"` и `KEY = { value = "путь" }`
#: (таблица pytest-env в pyproject.toml).
_COVERAGE_RE = re.compile(
    r"PARTEST_COVERAGE_JSON[\"']?\s*[=:]\s*(?:\{\s*value\s*=\s*)?[\"']?([^\"' \t\r\n,\]]+)")
#: `--alluredir=путь`, `--alluredir путь` и список addopts `"--alluredir", "путь"`.
#: Значение — до ASCII-пробела: `\s` в Python понимает и NEL, и разделитель строк, и
#: значение с ними посередине выглядело бы обрезанным, но годным путём.
_ALLURE_RE = re.compile(r"--alluredir(?:\s*=\s*|\s+|[\"']\s*,\s*)[\"']?([^\"' \t\r\n,\]]+)")
_TOXINIDIR = re.compile(r"^\{toxinidir\}[\\/]?")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]{0,30})://(.*)\Z", re.S)
#: Признаки базы: драйвер в зависимостях и имя переменной подключения в настройках.
_PG_DRIVER = re.compile(r"(?i)\b(psycopg2?(?:-binary)?|asyncpg|pg8000)\b")
_OTHER_DRIVER = re.compile(r"(?i)\b(pymysql|mysqlclient|cx[-_]oracle|oracledb|pyodbc|"
                           r"sqlalchemy)\b")
_DSN_NAME = re.compile(r"\b(DATABASE_URL|[A-Z][A-Z0-9_]{0,40}_DSN|DB_DSN|POSTGRES_DSN)\b")
_ALLURE_PYTEST = re.compile(r"(?i)\ballure[-_]pytest\b")


# --------------------------------------------------------------------- вывод


def one_line(value: Any, limit: int = SHOWN_LIMIT) -> str:
    """Значение из чужого файла — в одну безопасную строку комментария и терминала:
    каждый символ из :data:`_UNSAFE_CATEGORIES` — `?`."""
    s = str(value)[:limit + 1]
    s = "".join("?" if unicodedata.category(c) in _UNSAFE_CATEGORIES else c for c in s)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def has_unsafe(value: str) -> bool:
    """Есть ли в строке символ, которому не место в комментарии и терминале."""
    return any(unicodedata.category(c) in _UNSAFE_CATEGORIES for c in value)


def quoted(value: str) -> str:
    """Скаляр YAML в двойных кавычках. Строка JSON с `ensure_ascii` — допустимый скаляр
    YAML: всё вне печатного ASCII экранировано (`\\uXXXX`), и DEL тоже — его JSON
    оставляет как есть, а `yaml.safe_load` на нём падает."""
    return json.dumps(value).replace("\x7f", "\\u007f")


def safe_url(value: str) -> str:
    """Адрес без учётных данных, параметров запроса и фрагмента.

    Остаются схема, узел с портом и путь; путь проходит через `veha.redact.text`. Если
    адрес не разобрать однозначно, он закрывается целиком, без разбора: `@` где-либо
    после узла — в пути, запросе или фрагменте (сырые `/ ? # @` в пароле разбор режет и
    уносит хвост пароля туда: `user:p@ss/word@host` даёт узел `ss`), либо порт не
    число. Показать такой адрес — показать то, что в нём спрятано. Правило то же, что у
    готовящегося `partest.redact.redact_url`.
    """
    m = _SCHEME_RE.match(value.strip())
    if not m:
        return "<адрес не разобран>"
    scheme = m.group(1)
    hidden = f"{scheme}://<скрыто>"
    try:
        parts = urlsplit(value.strip())
        parts.port  # noqa: B018 — ValueError, если порт не число
    except ValueError:
        return hidden
    if "@" in parts.path or "@" in parts.query or "@" in parts.fragment:
        return hidden
    host = parts.netloc.rpartition("@")[2]
    if not host:
        return hidden
    return one_line(redact.text(f"{scheme}://{host}{parts.path}"))


def segment(value: str, fallback: str = "project") -> str:
    """Имя каталога — в сегмент области `[A-Za-z0-9._-]+` без `..`."""
    s = _SEGMENT_BAD.sub("-", value).strip("-.")
    s = re.sub(r"\.{2,}", ".", s)
    return s or fallback


def _read(path: Path, notes: Optional[List[str]] = None, label: str = "",
          root: Optional[Path] = None) -> Optional[bytes]:
    """Байты файла проекта не больше :data:`READ_LIMIT`; больше или не читается — `None`
    (и строка в *notes*, если она передана). Ссылка вместо файла или каталога по дороге
    от *root* (по умолчанию — каталог файла) не открывается."""
    try:
        if not local_to(root or path.parent, path):
            return None
        if not path.is_file():
            return None
        if path.stat().st_size > READ_LIMIT:
            if notes is not None:
                notes.append(f"{label or path.name}: больше {READ_LIMIT // 1024} КБ — не "
                             f"читается, разобран не будет")
            return None
        return path.read_bytes()
    except OSError:
        return None


def _text(path: Path, notes: Optional[List[str]] = None, label: str = "",
          root: Optional[Path] = None) -> str:
    data = _read(path, notes, label, root)
    return data.decode("utf-8", errors="replace") if data is not None else ""


# --------------------------------------------------------------- confpartest


@dataclass
class Service:
    """Одна запись `swagger_files`, как прочитана из литерала."""

    name: str
    kind: str = ""                  # local | url; пусто — не литерал
    shown: str = ""                 # место для комментария: путь как объявлен, адрес — без секретов
    note: str = ""                  # что с записью не так

    @property
    def command_name(self) -> str:
        """Имя для `--service` — только если его можно подставить в команду как есть."""
        return self.name if _SAFE_NAME.match(self.name) else ""


@dataclass
class Spec:
    """Что удалось узнать о спецификациях проекта, не выполняя его код."""

    path: Optional[Path] = None     # найденный confpartest.py
    literal: bool = False           # swagger_files — литерал словаря (целиком или по записям)
    services: List[Service] = field(default_factory=list)
    problem: str = ""               # почему спецификаций не узнать
    notes: List[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.path is not None


def _skip_dir(name: str) -> bool:
    low = name.lower()
    return name.startswith(".") or low in _SKIP_DIRS or low.startswith("venv")


def _subdirs(path: Path) -> List[Path]:
    """Подкаталоги без ссылок (символических и junction) — в них не заходим: ссылка может
    вести за пределы корня, в том числе на сетевой ресурс."""
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError:
        return []
    out: List[Path] = []
    for e in entries:
        p = Path(e.path)
        try:
            if e.is_dir(follow_symlinks=False) and not is_link(p) and not _skip_dir(e.name):
                out.append(p)
        except OSError:
            continue
    return out


def find_confpartest(root: Path) -> Tuple[Optional[Path], List[Path]]:
    """`confpartest.py`: в корне, иначе на один-два уровня глубже (`tests/`,
    `src/tests/`), мимо скрытых каталогов, окружений, сборки и ссылок; кандидат вне корня
    после `resolve` отбрасывается. Возвращает выбранный и остальные."""
    if local_to(root, root / CONFPARTEST) and (root / CONFPARTEST).is_file():
        return root / CONFPARTEST, []
    found: List[Path] = []
    level = [root]
    for _ in range(2):
        level = [d for parent in level for d in _subdirs(parent)]
        found = [d / CONFPARTEST for d in level
                 if local_to(root, d / CONFPARTEST) and (d / CONFPARTEST).is_file()]
        if found:
            break
    return (found[0], found[1:]) if found else (None, [])


def local_path_problem(where: str) -> str:
    """Почему локальный путь из `confpartest.py` не проверяется на диске; пусто — можно.

    Проверка существования — тоже обращение: сетевой путь (UNC) на Windows ушёл бы по
    сети. Поэтому `stat` — только для относительного пути внутри каталога `confpartest.py`.
    """
    problem = report_path_problem("путь", where)
    if problem:
        return problem.split(": ", 1)[1]
    if not inside_root(where)[0]:
        return "путь вне каталога confpartest.py — существование не проверяю"
    return ""


def _entry(name: str, kind: str, where: str, base: Path) -> Service:
    """Запись `swagger_files` по ответу дочернего разбора: вид и место — строки."""
    kind = kind.lower()
    if kind == URL_KIND:
        return Service(name, URL_KIND, safe_url(where))
    if kind not in LOCAL_KINDS:
        return Service(name, note=f"вид {one_line(kind, 40)!r} — partest ждёт local или url")
    if "://" in where:
        return Service(name, "local", safe_url(where),
                       note="вид local, но это адрес — partest прочитает его как путь к "
                            "файлу; поправьте вид на url в confpartest.py")
    shown = one_line(redact.text(where))
    problem = local_path_problem(where)
    if problem:
        return Service(name, "local", shown, note=problem)
    target = base / inside_root(where)[1]
    if not local_to(base, target):
        return Service(name, "local", shown, note="путь ведёт через ссылку — не проверяю")
    note = ""
    try:
        if not target.is_file():
            note = "файла нет (путь — от каталога confpartest.py)"
    except (OSError, ValueError):
        note = "путь не проверить"
    return Service(name, "local", shown, note=note)


_SHAPE_NOTES = {"short": "ожидалось [вид, путь_или_адрес]",
                "notstr": "ожидалось [вид, путь_или_адрес] строками",
                "nonlit": "не литерал — вид и место init не узнал; collect openapi их "
                          "разрешит сам"}


def _parse_isolated(data: bytes) -> Dict[str, Any]:
    """Единственный разбор `confpartest.py` — в дочернем `python -I` (`_spec_child.py`).
    Ответ — словарь из JSON; при любой неудаче — `{"error": причина}`, без трассировки."""
    exe = sys.executable
    if not exe:
        return {"error": "разбор не выполнен: неизвестен путь к интерпретатору Python "
                         "(sys.executable пуст)"}
    try:
        code = _CHILD_SOURCE.read_text(encoding="utf-8")
    except OSError:
        return {"error": "разбор не выполнен: нет модуля дочернего разбора"}
    try:
        # свой интерпретатор и свой код, список аргументов, без оболочки
        out = subprocess.run(  # nosec B603
            [exe, "-I", "-c", code], input=data, capture_output=True,
            timeout=PARSE_TIMEOUT, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"error": f"разбор не выполнен ({type(exc).__name__})"}
    if out.returncode != 0:
        return {"error": "разбор уронил интерпретатор (слишком глубокая вложенность "
                         "выражений)"}
    try:
        answer = json.loads(out.stdout.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return {"error": "разбор ответил не JSON"}
    return answer if isinstance(answer, dict) else {"error": "разбор ответил не словарём"}


def read_spec(root: Path) -> Spec:
    """`swagger_files` из `confpartest.py` проекта — по ответу дочернего разбора, без
    импорта и без разбора в этом процессе."""
    path, others = find_confpartest(root)
    if path is None:
        return Spec(problem=f"нет {CONFPARTEST} ни в корне, ни в подкаталогах (tests/ и т. п.)")
    spec = Spec(path=path)
    rel = one_line(path.relative_to(root).as_posix(), 120)
    if others:
        spec.notes.append(f"{CONFPARTEST} найден в нескольких местах — взят {rel}; ещё: "
                          + ", ".join(one_line(p.relative_to(root).as_posix(), 120)
                                      for p in others[:5]))
    data = _read(path, spec.notes, rel, root)
    if data is None:
        spec.problem = f"{rel} не прочитан"
        return spec
    answer = _parse_isolated(data)
    if "error" in answer:
        spec.problem = f"{rel} не разбирается: {one_line(answer['error'], 120)}"
        return spec
    if not answer.get("assignments"):
        spec.problem = f"в {rel} нет присваивания swagger_files на верхнем уровне"
        return spec
    if answer["assignments"] > 1:
        spec.notes.append("swagger_files присваивается несколько раз — взято последнее")
    if answer.get("touched"):
        spec.notes.append("swagger_files меняется и кодом (под условием, по ключу или "
                          "методом) — здесь только литерал верхнего уровня")
    if answer.get("value") != "Dict":
        spec.problem = (f"swagger_files в {rel} вычисляется кодом "
                        f"({one_line(answer.get('value'), 40)}), а init код проекта не "
                        f"выполняет")
        return spec
    spec.literal = True
    if answer.get("unpack"):
        spec.notes.append("в swagger_files есть **распаковка — её записи не прочитаны")
    for i, e in enumerate(answer.get("entries") or [], 1):
        e = e if isinstance(e, dict) else {}
        key = e.get("key")
        if not isinstance(key, str):
            why = "имя — не литерал" if e.get("key_type") == "nonlit" else "имя — не строка"
            spec.services.append(Service(f"<запись {i}>", note=why))
            continue
        name = one_line(key, 80)
        if e.get("shape") == "ok" and isinstance(e.get("kind"), str) \
                and isinstance(e.get("where"), str) and e["where"].strip():
            spec.services.append(_entry(name, e["kind"], e["where"], path.parent))
        else:
            shape = e.get("shape") if e.get("shape") in _SHAPE_NOTES else "nonlit"
            if shape == "ok":
                shape = "notstr"
            spec.services.append(Service(name, note=_SHAPE_NOTES[shape]))
    if not spec.services and not spec.notes:
        spec.problem = "swagger_files пуст"
    return spec


# ------------------------------------------------------------------ отчёты


@dataclass
class Found:
    """Путь к отчёту, найденный в настройках проекта."""

    path: str = ""                  # годный путь внутри корня; пусто — нет
    where: str = ""                 # файл настроек
    raw: str = ""                   # почему найденное не взято (без самого пути вне корня)
    #: Путь абсолютный или выходит из корня. В `reports` такой не пишется вовсе — ни
    #: действующим, ни в комментарии: корень — единица, которую видит сервер (в
    #: контейнере смонтирован только он), а путь машины в `atlas.yaml` из репозитория
    #: ломается у второго человека и раскрывает раскладку машины.
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


def _setting_lines(root: Path, notes: List[str]) -> List[Tuple[str, str]]:
    """Строки файлов настроек pytest без строк-комментариев: (файл, строка)."""
    out: List[Tuple[str, str]] = []
    for name in SETTINGS_FILES:
        # по `\n`, а не `splitlines`: тот режет и по NEL, `\u2028`, `\x0c` — и значение
        # с таким символом посередине выглядело бы обрезанным, но годным путём
        for line in _text(root / name, notes).split("\n"):
            line = line.rstrip("\r")
            if not line.lstrip().startswith(("#", ";")):
                out.append((name, line))
    return out


def _found(value: str, where: str) -> Found:
    value = _TOXINIDIR.sub("", value.strip())
    if not value:
        return Found(where=where, raw="пустое значение")
    if has_unsafe(value):
        return Found(where=where, raw="управляющие символы в значении — впишите путь сами")
    if any(c in value for c in "{}$%"):
        # подстановка tox или оболочки — разворачивать её здесь значило бы гадать
        return Found(where=where, raw=f"подстановка {one_line(redact.text(value), 80)!r} — "
                                      f"впишите путь сами")
    if report_path_problem("", value) or not inside_root(value)[0]:
        return Found(where=where, outside=True)
    norm = inside_root(value)[1]
    if redact.text(norm) != norm or not _SAFE_PATH.match(norm):
        return Found(where=where, raw="нестандартные символы в пути — впишите путь сами")
    return Found(path=norm, where=where)


def find_reports(root: Path, notes: Optional[List[str]] = None) -> Tuple[Found, Found]:
    """Где прогон проекта кладёт `coverage.json` и результаты Allure — по его настройкам."""
    coverage, allure = Found(), Found()
    for where, line in _setting_lines(root, notes if notes is not None else []):
        if not (coverage.path or coverage.raw or coverage.outside):
            m = _COVERAGE_RE.search(line)
            if m:
                coverage = _found(m.group(1), where)
        if not (allure.path or allure.raw or allure.outside):
            m = _ALLURE_RE.search(line)
            if m:
                allure = _found(m.group(1), where)
    return coverage, allure


# --------------------------------------------------------------- зависимости


@dataclass
class Signals:
    """Что известно о проекте по его зависимостям и настройкам — без импорта."""

    db: str = ""                    # где найден признак базы; пусто — не найден
    db_postgres: bool = False       # драйвер PostgreSQL (сборщик схемы знает только его)
    allure_pytest: bool = False     # allure-pytest в зависимостях
    deps_read: List[str] = field(default_factory=list)


def _dependency_files(root: Path) -> List[Path]:
    """Файлы зависимостей по :data:`DEPENDENCY_GLOBS` — без обхода ссылок: каталог
    `requirements/` и сами файлы, оказавшиеся ссылкой, не открываются."""
    files: List[Path] = []
    for pattern in DEPENDENCY_GLOBS:
        head, _, name = pattern.rpartition("/")
        base = root / head if head else root
        if head and (is_link(base) or not base.is_dir()):
            continue
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            p = Path(e.path)
            if fnmatch.fnmatchcase(e.name, name) and p not in files and local_to(root, p):
                files.append(p)
    return files


def find_signals(root: Path, notes: List[str]) -> Signals:
    sig = Signals()
    for f in _dependency_files(root):
        text = _text(f, notes, one_line(f.relative_to(root).as_posix(), 80), root)
        if not text:
            continue
        rel = f.relative_to(root).as_posix()
        sig.deps_read.append(rel)
        if _ALLURE_PYTEST.search(text):
            sig.allure_pytest = True
        pg, other = _PG_DRIVER.search(text), _OTHER_DRIVER.search(text)
        if pg and not sig.db_postgres:
            sig.db, sig.db_postgres = f"{pg.group(1).lower()} в {rel}", True
        elif other and not sig.db:
            sig.db = f"{other.group(1).lower()} в {rel}"
    if not sig.db:
        # имя переменной подключения в настройках pytest или в conftest.py корня
        for name in (*SETTINGS_FILES, "conftest.py"):
            m = _DSN_NAME.search(_text(root / name, notes))
            if m:
                sig.db = f"переменная {m.group(1)} в {name}"
                break
    return sig


def parent_config(root: Path) -> Optional[Path]:
    """`atlas.yaml` выше по дереву: агент из этого каталога до сих пор брал его."""
    for candidate in root.parents:
        path = candidate / CONFIG_NAME
        try:
            if path.is_file():
                return path
        except OSError:
            return None
    return None


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
    signals: Signals = field(default_factory=Signals)
    notes: List[str] = field(default_factory=list)    # что не прочитано и почему
    parent: Optional[Path] = None
    project: str = ""                                 # подпись проекта: имя каталога

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
        dirs = [self.root] + ([spec.path.parent] if spec.path else [])
        if any(safe_dir(d) == DIR_PLACEHOLDER for d in dirs):
            out.append(f"имя каталога нестандартное (символы, которые оболочка развернула бы "
                       f"внутри кавычек) — в командах ниже {DIR_PLACEHOLDER}; подставьте путь "
                       f"сами")
        if self.parent is not None:
            out.append(f"выше по дереву есть {self.parent} — агент partest-atlas из этого каталога "
                       f"брал его, теперь возьмёт новый; два atlas.yaml на один проект — "
                       f"уберите лишний")
        if not spec.found:
            out.append(f"{spec.problem} — контракт OpenAPI снимать неоткуда")
        elif spec.problem:
            out.append(spec.problem)
        if spec.found and not spec.literal:
            out.append("в atlas.yaml вписывать ничего не нужно: collect openapi сам "
                       "импортирует confpartest.py; чтобы init показал службы, впишите в "
                       "confpartest.py литерал swagger_files = {\"имя\": [\"url\", \"адрес\"]}")
        out += list(spec.notes)
        out += [f"swagger_files[{s.name}]: {s.note}" for s in spec.services if s.note]
        out += [f"swagger_files[{s.name}]: имя нестандартное — в команду collect "
                f"подставьте его сами" for s in spec.services
                if s.kind and not s.command_name and not s.name.startswith("<")]
        for key, found in (("coverage", self.coverage), ("allure", self.allure)):
            if found.outside:
                out.append(f"reports.{key}: в {found.where} путь вне корня — не записан: в "
                           f"контейнере Вехи смонтирован только корень, а путь машины в "
                           f"atlas.yaml ломается у других; держите отчёт внутри корня")
            elif found.raw:
                out.append(f"reports.{key}: в {found.where} {found.raw}")
        out += list(self.notes)
        return out


def plan(root: Path, *, system: str = "", env: str = "") -> Plan:
    """Собрать всё, что известно о проекте. *system* и *env* — уже выбранные значения
    (ключ, иначе `ATLAS_SYSTEM`/`ATLAS_ENV`); пусто — :data:`DEFAULT_SCOPE`, как у всех
    команд обоих пакетов. Имя каталога идёт только в `project.name` — это подпись, а не
    область."""
    root = Path(root).resolve()
    notes: List[str] = []
    coverage, allure = find_reports(root, notes)
    signals = find_signals(root, notes)
    return Plan(root=root, system=system or DEFAULT_SCOPE, env=env or DEFAULT_SCOPE,
                spec=read_spec(root), coverage=coverage, allure=allure, signals=signals,
                notes=sorted(set(notes)), parent=parent_config(root),
                project=segment(root.name))


def _report_lines(found: Found, key: str, value: str, what: str, default_note: str) -> List[str]:
    """Ключ раздела `reports` с комментарием, откуда он. Путь вне корня не пишется вовсе:
    сервер возьмёт умолчание в корне."""
    if found.outside:
        note = (f"в {found.where} путь вне корня — не записан: в контейнере Вехи смонтирован "
                f"только корень; перенесите отчёт внутрь корня")
    elif found.path:
        note = f"найдено в {found.where}"
    elif found.raw:
        note = f"в {found.where} {found.raw}"
    else:
        note = default_note
    return [f"  # {what}; {one_line(note, 300)}", f"  {key}: {quoted(value)}"]


def _spec_block(p: Plan) -> List[str]:
    spec = p.spec
    rel = spec.path.relative_to(p.root).as_posix() if spec.path else CONFPARTEST
    out = ["# ---------------------------------------------------------------------",
           f"# Контракт OpenAPI. Спецификации объявлены в {one_line(rel, 80)} (swagger_files);",
           "# снимает их  partest-atlas collect openapi  в окружении проекта (он",
           "# импортирует confpartest.py, как pytest). veha init код проекта не",
           "# выполняет и прочитал только литерал. Ниже — для справки, адреса без",
           "# учётных данных и параметров запроса. Вписывать сюда ничего не нужно.",
           "# ---------------------------------------------------------------------"]
    if spec.problem:
        out.append(f"# ! {one_line(spec.problem)}")
    for note in spec.notes:
        out.append(f"# ! {one_line(note)}")
    for s in spec.services:
        line = f"#   {s.name}: {s.kind} {s.shown}" if s.kind else f"#   {s.name}: ?"
        out.append(one_line(line + (f"   ! {s.note}" if s.note else ""), 400))
    if spec.found and (not spec.literal or any(not s.kind for s in spec.services)):
        out += ["# Что и куда вписать: ничего — collect openapi разрешит swagger_files сам.",
                "# Чтобы init и эта справка видели службы, в confpartest.py нужен литерал:",
                "#   swagger_files = {\"имя\": [\"url\", \"адрес\"]}  или  [\"local\", \"путь\"]",
                "# а адрес из окружения — читать уже в тестах, не в confpartest.py."]
    if len([s for s in spec.services if not s.name.startswith("<")]) > 1:
        out += ["# ! contract.openapi — один на область: каждая следующая collect openapi",
                "#   заменяет предыдущий. Пока partest-atlas не хранит несколько служб,",
                "#   снимайте главную (или по одной системе на службу: --system <имя>)."]
    return out


def _database_block(sig: Signals) -> List[str]:
    if sig.db:
        why = f"# Признак базы: {one_line(sig.db, 120)}."
        if not sig.db_postgres:
            why += " Сборщик схемы знает только PostgreSQL."
    else:
        why = "# Признаков базы в зависимостях и настройках не найдено."
    return [
        "# ---------------------------------------------------------------------",
        "# Подключение к базе — задаёт человек. Строку подключения сюда не писать:",
        "# файл называет переменную окружения. Пользователь базы — только на",
        "# чтение: сборщик схемы (partest-atlas collect db-schema) читает каталог.",
        why,
        "# ---------------------------------------------------------------------",
        "# database:",
        "#   dsn_env: DATABASE_URL          # или connect: \"пакет.модуль:функция\"",
        "#   schemas: auto                  # все несистемные схемы; или список [public]",
    ]


#: Шаблоны чужих разделов — минимально рабочая форма владельца (partest-atlas):
#: раскомментированный блок разбирается его кодом (`tests/test_init.py`).
CONTAINER_LOGS_TEMPLATE = [
    "# container_logs:                  # partest-atlas collect container-logs",
    "#   source: \"пакет.модуль:функция\"  # функция проекта: {контейнер: текст журнала}",
    "#   rules:                         # что искать в журналах",
    "#     - kind: table",
    "#       pattern: 'table \"(\\w+)\"'",
    "#       known_only: true            # только таблицы, известные схеме базы",
]


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
        "# прогон pytest, сборщики и сервер (без него у всех команд — default):",
        f"#   ATLAS_SYSTEM={p.system}  ATLAS_ENV={p.env}",
        "",
        "project:",
        f"  name: {quoted(p.project or p.system)}",
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
        "  # env:                           # доверенные overrides; null удаляет наследованное",
        "  #   PROJECT_OPTIN: null",
        "  # allure:                        # opt-in: fresh raw + два снапшота на suite",
        "  #   store: .veha/allure",
        "  #   keep: 2",
        "  #   command: [allure, generate] # argv без shell; runner добавляет raw/-o/--clean",
        "  #   timeout: 300",
        "  suites:",
        "    full:",
        "      label: \"Весь набор\"",
        "      args: []",
        "      full: true",
        "",
    ]
    lines += _spec_block(p)
    lines += [""] + _database_block(p.signals)
    lines += [
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
        *CONTAINER_LOGS_TEMPLATE,
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
    """Записать заготовку целиком или не записать вовсе.

    Текст собирается и кодируется до того, как появится файл. Пишется во временный файл
    рядом, затем `os.link` создаёт `atlas.yaml` — атомарно и без перезаписи: если файл (или
    висячая ссылка) уже есть, ссылка не создаётся. Где жёстких ссылок нет, — открытие с
    `x` и удаление при любой ошибке записи: полупустой `atlas.yaml` после сбоя хуже, чем
    никакого.
    """
    target = p.root / CONFIG_NAME
    data = render(p).encode("utf-8")
    if os.path.lexists(target):
        raise Exists(str(target))
    fd, name = tempfile.mkstemp(prefix=".atlas-", suffix=".yaml.tmp", dir=str(p.root))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # mkstemp создаёт файл 0600; atlas.yaml — обычный файл проекта, его читает и
        # контейнер Вехи под другим пользователем: права как у open(), 0o666 & ~umask
        os.chmod(name, 0o666 & ~_umask())
        try:
            os.link(name, target)
        except FileExistsError:
            raise Exists(str(target)) from None
        except (OSError, NotImplementedError):
            _write_exclusive(target, data)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    return target


def _umask() -> int:
    """Текущая маска прав процесса. Узнать её можно только установив: ставим и тут же
    возвращаем ту же — команда однопоточная."""
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def _write_exclusive(target: Path, data: bytes) -> None:
    try:
        f = open(target, "xb")
    except FileExistsError:
        raise Exists(str(target)) from None
    try:
        with f:
            f.write(data)
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- подсказки


def safe_dir(value: Any) -> str:
    """Путь каталога для печатаемой команды. Проходит :data:`_SAFE_DIR` — печатается (с
    пробелом — в двойных кавычках); нет — :data:`DIR_PLACEHOLDER`: `$(…)`, обратные
    кавычки, `%…%` внутри двойных кавычек развернули бы bash, PowerShell или cmd."""
    s = str(value)
    if len(s) > 400 or not _SAFE_DIR.match(s):
        return DIR_PLACEHOLDER
    return f'"{s}"' if " " in s else s


def env_forms(pairs: List[Tuple[str, str]]) -> Dict[str, str]:
    """Переменные окружения на время сессии — в трёх оболочках. Значения — сегменты и
    пути из :data:`_SAFE_PATH`, кавычки им не нужны."""
    return {
        "bash/zsh": "export " + " ".join(f"{k}={v}" for k, v in pairs),
        "cmd": " & ".join(f'set "{k}={v}"' for k, v in pairs),
        "PowerShell": "; ".join(f'$env:{k}="{v}"' for k, v in pairs),
    }


def next_steps(p: Plan) -> List[str]:
    """Что делать после заготовки — командами, по порядку."""
    root = safe_dir(p.root)
    scope = f"--system {p.system} --env {p.env}"
    sig = p.signals
    package = "partest-atlas[postgres]>=0.4" if sig.db_postgres else "partest-atlas>=0.4"
    install = f"pip install \"{package}\""
    if not sig.allure_pytest:
        install += " allure-pytest"
    steps = [f"в окружении проекта (там, где pytest):  {install}"
             + ("" if sig.allure_pytest else
                "   (allure-pytest в зависимостях проекта не найден — без него нет "
                "проверок и обмена на карте)")]
    pairs = [("ATLAS_SYSTEM", p.system), ("ATLAS_ENV", p.env)]
    if not p.coverage.usable:
        pairs.append(("PARTEST_COVERAGE_JSON", p.coverage_path))
    forms = env_forms(pairs)
    steps.append("переменные окружения прогона, сборщиков и сервера — в своей оболочке:\n"
                 + "\n".join(f"       {k:<11} {v}" for k, v in forms.items()))
    if sig.db:
        steps.append("база: раскомментируйте раздел database в atlas.yaml, строку "
                     "подключения — в переменную DATABASE_URL; пользователь — только на "
                     "чтение")
    if p.spec.found:
        spec_root = safe_dir(p.spec.path.parent) if p.spec.path else root
        named = [s for s in p.spec.services if not s.name.startswith("<")]
        if len(named) > 1:
            first = named[0]
            service = (f" --service {first.command_name}" if first.command_name
                       else " --service <имя службы>")
            steps.append(f"контракт:  partest-atlas collect openapi --root {spec_root} "
                         f"{scope}{service}   (служб {len(named)}: contract.openapi один на "
                         f"область — следующая команда заменит предыдущий)")
        else:
            steps.append(f"контракт:  partest-atlas collect openapi --root {spec_root} {scope}")
    if sig.db:
        steps.append(f"схема базы:  partest-atlas collect db-schema --root {root} {scope}")
    run = "pytest"
    if not p.allure.usable:
        run += f" --alluredir {p.allure_path}"
    steps.append(f"прогон в каталоге проекта:  {run}")
    steps.append(f"карта (из окружения Вехи):  veha serve --root {root} {scope}")
    return steps
