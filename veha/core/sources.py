"""Чтение источников: артефакты агента, отчёт покрытия, Allure, ручная карта.

Здесь сервер только **читает** и приводит прочитанное к общему виду (пути, nodeid,
маскирование секретов). Выводы — что считать последним прогоном, кто писал в таблицу,
чем объявленное расходится с увиденным — в :mod:`veha.core.graph`. Граница нужна не для
красоты: чтение зависит от чужих форматов и меняется вместе с ними, а вывод — это
продукт, и его не должно трясти от того, что агент переложил поле.

Артефакты агента
----------------
Формат принадлежит `partest-atlas`, и копии его здесь нет. Файл читается
`partest_atlas.artifacts.read`, проверяется `partest_atlas.artifacts.validate`, путь к
нему строит `partest_atlas.artifacts.Scope`. Каждый вид получает **состояние**:

``ok``       прочитан, сходится с контрактом, данные идут в граф;
``missing``  файла нет — законное состояние проекта, который этого ещё не собирал;
``broken``   файл есть, но не разбирается;
``newer``    версия схемы новее известной — читать вслепую нельзя, данные не берутся;
``invalid``  контракт не сходится (в том числе артефакт схемы 1) — данные
             не берутся, расхождения перечислены.

Данные берутся **только** из ``ok``. Показать пустую сцену с объяснением честнее, чем
сцену, построенную из файла, который сервер не понимает.

Область (система, контур)
-------------------------
Агент раскладывает артефакты по ``<artifacts_dir>/<система>/<контур>/``. Сервер
показывает одну область за раз; как она выбирается — :func:`choose_scope`.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from partest_atlas import __version__ as ATLAS_VERSION
from partest_atlas import artifacts
from partest_atlas.artifacts import KINDS, SCHEMA_VERSION, Scope

from veha import redact
from veha.config import Config

# ---------------------------------------------------------------------------
# артефакты агента
# ---------------------------------------------------------------------------

#: Версии схемы, которые сервер умеет читать. Своя константа, а не `SCHEMA_VERSION`
#: агента: агент, выпустивший схему 3, не должен молча стать «известной» серверу
#: версией. Тест сверяет её с агентом и падает, когда агент ушёл вперёд.
READS_SCHEMA = frozenset({2})

#: Какой ключ полезной нагрузки несёт записи у каждого вида — так, как их пишут
#: сборщики `partest-atlas` 0.1.
#: Пока формат агента не объявляет ключи полезной нагрузки в контракте, своя таблица:
#: они видны только в коде сборщиков. `load.profile` не читается вовсе, пока формат не
#: согласован с `partest-load`.
PAYLOAD_KEY = {
    "db.schema": "databases",
    "db.effects": "records",
    "bus.flow": "records",
    "containers.logs": "containers",
    "contract.openapi": "operations",
    "specs.requirements": "requirements",
    "ui.flow": "links",
    "runner.history": "runs",
}

#: Ключи, значения под которыми — идентификаторы соединения (таблица, топик, путь,
#: nodeid, имя контейнера), а не текст. Маскирование их не трогает: исказить имя
#: таблицы значит разорвать связь, а секретов в именах схемы не бывает.
IDENTIFIER_KEYS = frozenset({
    "version", "kind", "scope", "collected_at", "collector",
    "table", "topic", "path", "rawPath", "method", "nodeid", "test", "screen", "control",
    "operationId", "name", "schema", "id", "section", "source", "tags", "responses",
})

#: Один сегмент пути: имя системы или контура. Никаких разделителей и `..`: область
#: становится каталогом, и выход за `atlas/` через неё недопустим.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_segment(value: str) -> bool:
    return bool(_SEGMENT_RE.match(value or "")) and value not in (".", "..") \
        and ".." not in value


@dataclass
class ArtifactStatus:
    kind: str
    file: str
    state: str                      # ok | missing | broken | newer | invalid
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    version: Any = None
    collected_at: str = ""
    collector: str = ""
    records: Optional[int] = None

    @property
    def usable(self) -> bool:
        return self.state == "ok"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "file": self.file, "state": self.state,
            "problems": list(self.problems), "warnings": list(self.warnings),
            "version": self.version, "collectedAt": self.collected_at,
            "collector": self.collector, "records": self.records,
        }


@dataclass(frozen=True)
class ScopeChoice:
    scope: Scope
    how: str
    #: `False` — на диске несколько областей, и ни одна не выбрана. Сервер показывает
    #: пустоту с требованием выбрать и ничего в такую область не пишет.
    chosen: bool = True

    @property
    def notable(self) -> bool:
        """Стоит ли говорить о выборе в шапке: молчим, только когда выбирать было не из
        чего или выбрал человек."""
        return self.how not in (HOW_EXPLICIT, HOW_SINGLE)


HOW_EXPLICIT = "задана явно"
HOW_SINGLE = "единственная область на диске"


def discover_scopes(artifacts_dir: Path) -> List[Scope]:
    """Области, для которых на диске лежит хотя бы один артефакт известного вида."""
    known = set(KINDS.values())
    out: List[Scope] = []
    base = Path(artifacts_dir)
    if not base.is_dir():
        return out
    for system in sorted(p for p in base.iterdir() if p.is_dir()):
        for env in sorted(p for p in system.iterdir() if p.is_dir()):
            if any((env / name).is_file() for name in known):
                out.append(Scope(system=system.name, env=env.name))
    return out


def choose_scope(artifacts_dir: Path, requested: Optional[Scope] = None) -> ScopeChoice:
    """Какую область показывать и почему. Причина уходит в API рядом с выбором.

    1. Область задана явно (ключи `--system/--env` или `ATLAS_SYSTEM/ATLAS_ENV` — те
       же переменные, которыми агент выбирает, **куда** писать) — берётся она, даже
       если на диске её нет: пустая сцена с названной причиной лучше подмены.
    2. На диске ровно одна область — она.
    3. Несколько, и среди них `default/default` — она; остальные перечислены.
    4. Несколько без `default/default` — не выбрана. Угадывать нельзя: показать стенд
       не того контура — хуже, чем ничего.
    """
    if requested is not None:
        return ScopeChoice(requested, HOW_EXPLICIT)
    found = discover_scopes(artifacts_dir)
    if not found:
        return ScopeChoice(Scope(), "артефактов нет ни в одной области")
    if len(found) == 1:
        return ScopeChoice(found[0], HOW_SINGLE)
    names = ", ".join(f"{s.system}/{s.env}" for s in found)
    if Scope() in found:
        return ScopeChoice(Scope(), f"по умолчанию; на диске есть ещё: {names}")
    return ScopeChoice(Scope(), (f"не выбрана: на диске несколько областей ({names}) — "
                                 "укажите --system и --env"), chosen=False)


def _count_records(kind: str, doc: Dict[str, Any]) -> Optional[int]:
    key = PAYLOAD_KEY.get(kind)
    value = doc.get(key) if key else None
    return len(value) if isinstance(value, (list, dict)) else None


def inspect(path: Path, kind: str, scope: Scope, root: Path) -> Tuple[ArtifactStatus, Dict[str, Any]]:
    """Прочитать один артефакт и вынести решение о нём. Данные — только при ``ok``."""
    try:
        rel = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        rel = str(path)
    if not path.is_file():
        return ArtifactStatus(kind, rel, "missing"), {}

    doc = artifacts.read(path)
    if not doc:
        # `read` снисходителен нарочно и отдаёт пустой словарь на битом файле. Файл
        # при этом есть — значит, это не «не собирали», а «собрали и испортили».
        return ArtifactStatus(kind, rel, "broken", problems=[
            "файл есть, но не разбирается как JSON-объект (или пуст)"]), {}
    if not isinstance(doc, dict):
        return ArtifactStatus(kind, rel, "broken", problems=artifacts.validate(doc)), {}

    problems = artifacts.validate(doc)
    version = doc.get("version")
    status = ArtifactStatus(kind, rel, "ok", version=version,
                            collected_at=str(doc.get("collected_at") or ""),
                            collector=str(doc.get("collector") or ""))

    if isinstance(version, int) and not isinstance(version, bool) \
            and version > max(READS_SCHEMA):
        status.state = "newer"
        status.problems = problems or [f"version={version} новее известной {max(READS_SCHEMA)}"]
        status.problems.append(
            f"сервер читает схему {sorted(READS_SCHEMA)} (partest-atlas {ATLAS_VERSION}); "
            "артефакт не читается — обновите сервер или пересоберите артефакт агентом "
            "той версии, которую сервер знает")
        return status, {}

    # Пока формат агента не проверяет это сам (`validate(doc, kind=…)`), сверяем здесь:
    # `validate` знает только «вид из набора», а не «вид этого файла», и файл схемы под
    # именем эффектов пропустит. Имя файла выводится из вида (`artifacts.filename`),
    # значит, обратное — тоже часть контракта.
    if doc.get("kind") in KINDS and doc.get("kind") != kind:
        problems = [*problems, f"kind={doc.get('kind')!r} лежит в файле вида {kind!r}"]
    if not problems and version not in READS_SCHEMA:
        problems = [f"version={version!r}: сервер читает схему {sorted(READS_SCHEMA)}"]
    if problems:
        status.state = "invalid"
        status.problems = problems
        if isinstance(version, int) and version < min(READS_SCHEMA):
            if kind == "runner.history":
                # Историю не снять заново: прошлые прогоны уже прошли. Пишут её сервер
                # (`veha/runner`) и агент (`partest-atlas/history` по умолчанию) — но
                # оба только в новой схеме.
                # Обратимо, пока не решено, переносить ли старую историю
                # (proposals, «обёртка v1 → v2»): удалить её значило бы решить за
                # пользователя.
                status.problems.append(
                    f"история схемы {version}: сервер её не читает, а переснять прошлые "
                    f"прогоны нельзя — переименуйте файл в runs.v{version}.json (сервер "
                    "его не читает), новая история начнётся с первого прогона")
            else:
                status.problems.append(
                    f"артефакт версии {version}: снимите его заново агентом partest-atlas "
                    "— править старые файлы руками не нужно")
        return status, {}

    # Пока формат агента не говорит, обязан ли `scope` в файле совпадать с каталогом, —
    # предупреждение, не отказ.
    doc_scope = Scope.from_doc(doc)
    if doc_scope != scope:
        status.warnings.append(
            f"в файле scope={doc_scope.system}/{doc_scope.env}, а лежит он в "
            f"{scope.system}/{scope.env}")
    key = PAYLOAD_KEY.get(kind)
    if key is None:
        status.warnings.append(
            "формат полезной нагрузки этого вида не согласован — сервер её не читает")
    elif key not in doc:
        status.warnings.append(
            f"нет ключа полезной нагрузки {key!r} — заголовок сходится, но данных нет")
    status.records = _count_records(kind, doc)
    return status, doc


def _flat_note(path: Path, base: Path) -> str:
    """Почему файл в корне каталога артефактов не читается и что с ним делать."""
    doc = artifacts.read(path)
    head = f"{path.name} лежит в корне {base.name}/ и не читается"
    if not isinstance(doc, dict) or not doc:
        return f"{head}: файл не разбирается (пуст или битый)"
    version = doc.get("version")
    if path.name == artifacts.filename("runner.history"):
        if version in READS_SCHEMA:
            return (f"{head}: схема {version} читается — перенесите файл в "
                    "<система>/<контур>/ той области, чью историю он хранит")
        # обратимо, пока не решено, переносить ли старую историю (proposals, G)
        return (f"{head}: историю этого формата сервер не читает — переименуйте в "
                f"runs.v{version if isinstance(version, int) else 1}.json (сервер его не "
                "читает), новая история начнётся с первого прогона")
    if version == 1 or doc.get("kind") not in KINDS:
        return f"{head}: схема 1, переносить бесполезно — снимите заново агентом"
    return f"{head}: артефакты раскладываются по <система>/<контур>/"


@dataclass
class ArtifactSet:
    """Артефакты одной области: документы и решение о каждом."""

    scope: Scope
    scope_how: str
    scope_chosen: bool
    scope_notable: bool
    scopes: List[Scope]
    statuses: Dict[str, ArtifactStatus]
    docs: Dict[str, Dict[str, Any]]
    ignored: List[str] = field(default_factory=list)

    def payload(self, kind: str, key: Optional[str] = None, default: Any = None) -> Any:
        """Полезная нагрузка вида или *default*, если артефакт не годится."""
        doc = self.docs.get(kind) or {}
        value = doc.get(key or PAYLOAD_KEY[kind])
        if value is None:
            return [] if default is None else default
        return value

    def doc(self, kind: str) -> Dict[str, Any]:
        return self.docs.get(kind) or {}

    @property
    def problems(self) -> List[ArtifactStatus]:
        return [s for s in self.statuses.values() if s.state not in ("ok", "missing")]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "readsSchema": sorted(READS_SCHEMA),
            "atlasVersion": ATLAS_VERSION,
            "scope": self.scope.as_dict(),
            "scopeHow": self.scope_how,
            "scopeChosen": self.scope_chosen,
            "scopeNotable": self.scope_notable,
            "scopes": [s.as_dict() for s in self.scopes],
            "artifacts": [s.as_dict() for s in self.statuses.values()],
            "problems": len(self.problems),
            "ignored": list(self.ignored),
        }


def load_artifacts(cfg: Config, requested: Optional[Scope] = None) -> ArtifactSet:
    base = cfg.artifacts_dir
    choice = choose_scope(base, requested)
    statuses: Dict[str, ArtifactStatus] = {}
    docs: Dict[str, Dict[str, Any]] = {}
    for kind in KINDS:
        if not choice.chosen:
            statuses[kind] = ArtifactStatus(kind, "", "missing")
            continue
        status, doc = inspect(choice.scope.path(base, kind), kind, choice.scope, cfg.root)
        statuses[kind] = status
        if status.usable:
            # Свободный текст артефакта — в интерфейс, поэтому маскируется здесь. Агент
            # обязан маскировать при сборе (его красная линия 6), но `redact` в
            # partest_atlas нет — граница сейчас держится только на сервере.
            docs[kind] = redact.strings(doc, skip=IDENTIFIER_KEYS)

    # Файлы известных видов прямо в корне каталога — плоская раскладка схемы 1. Агент так
    # не пишет; молча читать их значило бы смешать области, молча пропускать —
    # показать пустоту без объяснения. Перечисляем с тем, что с ними делать.
    ignored = []
    if base.is_dir():
        for name in sorted(set(KINDS.values())):
            if (base / name).is_file():
                ignored.append(_flat_note(base / name, base))
    return ArtifactSet(scope=choice.scope, scope_how=choice.how, scope_chosen=choice.chosen,
                       scope_notable=choice.notable,
                       scopes=discover_scopes(base), statuses=statuses, docs=docs,
                       ignored=ignored)


# ---------------------------------------------------------------------------
# ландшафт из atlas.yaml
# ---------------------------------------------------------------------------
def containers(cfg: Config) -> List[dict]:
    return [{
        "key": c.key, "label": c.label, "kind": c.kind,
        "default": c.default, "note": c.note, "tier": c.tier,
    } for c in cfg.containers]


def domains(cfg: Config) -> List[dict]:
    out = []
    for key, spec in cfg.section("domains").items():
        spec = spec or {}
        out.append({
            "key": key,
            "label": spec.get("label", key),
            "tier": int(spec.get("tier", 0)),
            "services": list(spec.get("services") or []),
        })
    return out


#: `{orderId}`, `{productId}` и `{id}` — один и тот же плейсхолдер.
PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")
# id в пути → плейсхолдер. Отрицательные id обязательны: тесты на «несуществующий
# ресурс» ходят в `/…/-1`, и без этого на сцене появлялись узлы-фантомы.
_ID_RE = re.compile(
    r"/(?:-?\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?=/|$)"
)


def norm_screen(path: Optional[str]) -> str:
    """Путь экрана к общему виду — для манифеста, ручной карты и записей агента.

    Агент пишет `location.hash || location.pathname`, то есть `#/orders/42`; манифест
    — `/#/orders`, ручная карта — `/#/orders/{orderId}`. Всё это один экран
    `/#/orders/{id}`: ведущий `/` перед `#`, запрос `?…` отрезан (как у `norm_path`),
    числа и uuid — `{id}`, имена плейсхолдеров — `{id}`.

    Корень `/` остаётся `/`: пустая строка значит «экран неизвестен», и корень с ней
    сливаться не должен.
    """
    p = (path or "").strip()
    if not p:
        return ""
    if p.startswith("#"):
        p = "/" + p
    p = p.split("?", 1)[0]
    p = PLACEHOLDER_RE.sub("{id}", p.rstrip("/")) or "/"
    return _ID_RE.sub("/{id}", p)


_PARAM_RE = re.compile(r"\[.*\]$")


def base_nodeid(nodeid: Optional[str]) -> str:
    """nodeid без параметров: `t.py::T::test[a-1]` → `t.py::T::test`.

    Параметры — только в последнем сегменте после `::`: `tests/[legacy]/x.py::t[a]`
    теряет `[a]`, а не всё от первой скобки. Без `::` это не nodeid теста, и строка
    остаётся как есть.
    """
    nodeid = nodeid or ""
    head, sep, last = nodeid.rpartition("::")
    if not sep:
        return nodeid
    return head + sep + _PARAM_RE.sub("", last)


def param_variant(nodeid: str, parameters: List[dict]) -> str:
    """Кандидат в вариант по параметрам Allure, когда `name` его не несёт.

    `@allure.title` заменяет `name`, и `[...]` из него пропадает. Параметры остаются, а
    id pytest для простых значений — значения через `-`. Это **догадка**: явные `ids=`
    дают другой id. Поэтому кандидат годится, только если агент записал ровно такой
    nodeid; иначе соединение уходит в неоднозначную ветку (см. `graph.Joiner`).
    Значения берутся после маскирования: секретный параметр вариантом не станет.
    """
    if not nodeid or base_nodeid(nodeid) != nodeid or not parameters:
        return ""
    values = []
    for p in redact.parameters(parameters):
        v = str(p.get("value", "")) if isinstance(p, dict) else ""
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        values.append(v)
    return f"{nodeid}[{'-'.join(values)}]"


def variant_of(nodeid: str, name: str) -> str:
    """Вариант параметризованного теста по результату Allure.

    `fullName` у Allure без параметров, а `name` — с ними (`test_x[a-1]`). Склеенные,
    они дают nodeid варианта таким, каким его пишет агент.
    """
    if not nodeid or base_nodeid(nodeid) != nodeid:
        return nodeid
    m = re.search(r"\[.*\]$", name or "")
    return nodeid + m.group(0) if m else nodeid


def screens(cfg: Config) -> List[dict]:
    """Экраны из манифеста скриншотов, путь к которому назван в `screens.manifest`."""
    f = cfg.path(cfg.screens.get("manifest"))
    if f is None or not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    featured = list(cfg.screens.get("featured") or [])
    # Манифест хранит путь, по которому на экран заходят, а не конечный URL. Для
    # сопоставления с контролами нужен настоящий путь — он задаётся в `screens.paths`.
    overrides = dict(cfg.screens.get("paths") or {})
    out = []
    for s in data.get("scenes") or []:
        sid = s.get("id")
        out.append({
            "id": sid,
            "title": s.get("title"),
            "path": s.get("path"),
            "screenPath": norm_screen(overrides.get(sid) or s.get("path")),
            "file": s.get("file"),
            "image": f"/screens/{s.get('file')}" if s.get("file") else None,
            "featured": sid in featured,
        })
    out.sort(key=lambda s: (not s["featured"], featured.index(s["id"])
                            if s["id"] in featured else 0, s["id"]))
    return out


def screens_dir(cfg: Config) -> Optional[Path]:
    d = cfg.path(cfg.screens.get("images_dir"))
    return d if d is not None and d.is_dir() else None


def app_map(cfg: Config) -> Dict[str, Any]:
    """Ручная карта интеграций. Путь — ключ `app_map` в `atlas.yaml`; нет ключа — нет карты.

    Это данные проекта, а не часть сервера: сервер не знает ничьего ландшафта, пока
    его не назвали в конфигурации.
    """
    f = cfg.path(cfg.raw.get("app_map"))
    if f is None or not f.is_file():
        return {}
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# coverage.json — покрытие по методике partest (контракт `coverage-payload`)
# ---------------------------------------------------------------------------
def api_id(method: str, path: str) -> str:
    return f"api:{method.upper()} {path}"


def partial_reasons(meta: Dict[str, Any], *, suite_full: Optional[bool] = None) -> List[str]:
    """Почему прогон описывает не весь набор — каждой причиной отдельно.

    Решает partest, а не сервер: причины из `meta` (контракт `coverage-payload`)
    выводятся, **только если partest поставил `partialRun`**. Своего порога у сервера
    нет — копия порога разошлась бы с partest молча. partest называет три причины:
    выбор `-m`/`-k` (`selection`), неслитые воркеры (`workers`/`merged`) и много
    непосещённых; последняя — то, что остаётся, когда первые две не подтвердились.

    Выбор по путям partest не помечает — его знает только тот, кто запускал: набор
    раннера, не объявленный полным (`suite_full=False`), — отдельная причина.
    """
    meta = meta or {}
    reasons: List[str] = []
    if suite_full is False:
        reasons.append("выбор набора")
    if not meta.get("partialRun"):
        return reasons
    named: List[str] = []
    selection = meta.get("selection") or {}
    if selection.get("markexpr") or selection.get("keyword"):
        named.append("выбор -m/-k")
    if int(meta.get("workers") or 1) > 1 and not meta.get("merged"):
        named.append("воркеры не слиты")
    ratio = meta.get("unseenRatio")
    if not named:
        named.append("много непосещённых" + (
            f" (unseenRatio {ratio})" if isinstance(ratio, (int, float))
            and not isinstance(ratio, bool) else ""))
    return reasons + named


# ---------------------------------------------------------------------------
# opt-in-наборы маркеров (partest ≥ 2.5.0: `meta.selection.optin`)
# ---------------------------------------------------------------------------
# Набор маркеров, объявленный в `partest_optin_markers`, partest **снимает** с прогона
# (deselect, не skip), пока его не попросили выражением `-m` или переменной окружения.
# Как решён каждый набор, partest пишет в `meta.selection.optin`:
# `{"slow": {"state": "off" | "markexpr" | "env", "env": "RUN_SLOW", "deselected": 3}}`.
# `partialRun` этот ключ не читает: прогон без снятого набора — полный прогон, но
# другого набора тестов. Поэтому два полных прогона с разными состояниями несравнимы,
# а больше нигде в отчёте разница не видна. Ключ аддитивный: отчёт partest до 2.5.0 и
# проект без opt-in-наборов его не пишут — это «не знаем», а не «ничего не снято».

#: Состояния, которые пишет partest 2.5.0. Неизвестное показывается как есть.
OPTIN_STATES = ("off", "markexpr", "env")


def _count(value: Any) -> int:
    """Счётчик из чужого отчёта: целое ≥ 0, всё прочее — 0."""
    if isinstance(value, bool):
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def normalize_optin(raw: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """Запись opt-in в проверенном виде; ``None`` — записи нет.

    Имена наборов — строки в порядке сортировки (запись в истории и подписи серий не
    должны зависеть от порядка ключей в чужом JSON); ``state`` — строка или ``None``;
    ``env`` — только если был; ``deselected`` — целое ≥ 0.
    """
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Dict[str, Any]] = {}
    for name in sorted(raw, key=str):
        info = raw[name] if isinstance(raw[name], dict) else {}
        state, env = info.get("state"), info.get("env")
        out[str(name)] = {
            "state": None if state is None else str(state),
            **({"env": str(env)} if env else {}),
            "deselected": _count(info.get("deselected")),
        }
    return out


def optin_record(meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Any]]]:
    """`meta.selection.optin` отчёта покрытия, приведённый :func:`normalize_optin`."""
    selection = (meta or {}).get("selection") if isinstance(meta, dict) else None
    if not isinstance(selection, dict):
        return None
    return normalize_optin(selection.get("optin"))


def optin_signature(optin: Optional[Dict[str, Dict[str, Any]]]) -> Optional[tuple]:
    """По чему различаются прогоны: состояние каждого набора, без счётчиков.

    То же правило, что у `partest.reports.compare` (``_optin_states``): несравнимы
    прогоны, у которых записи есть у обоих и состояния наборов расходятся. Число снятых
    тестов в подпись не входит — оно меняется, когда в набор добавили тест, а состав
    прогона от этого не меняется. ``None`` — записи нет.
    """
    if optin is None:
        return None
    return tuple((name, (info or {}).get("state")) for name, info in sorted(optin.items()))


def optin_acted(optin: Optional[Dict[str, Dict[str, Any]]]) -> bool:
    """Набор что-то сделал с прогоном: снял тесты или был включён.

    Выключенный набор, которому нечего было снимать, прогон не изменил — о нём молчим.
    """
    return any(info.get("deselected") or info.get("state") in ("markexpr", "env")
               for info in (optin or {}).values())


def _tests(n: int) -> str:
    """«1 тест», «3 теста», «14 тестов»."""
    if n % 10 == 1 and n % 100 != 11:
        word = "тест"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "теста"
    else:
        word = "тестов"
    return f"{n} {word}"


def optin_phrases(optin: Optional[Dict[str, Dict[str, Any]]], *,
                  counts: bool = True) -> List[str]:
    """Каждый набор — по-человечески: «slow — снят (3 теста)», «slow — включён через -m»,
    «slow — включён через RUN_SLOW».

    Текст, а не разметка: экранирует тот, кто вставляет в HTML. ``counts=False`` —
    подпись серии тренда, где число снятых у прогонов разное, а состояние одно.
    """
    out: List[str] = []
    for name, info in (optin or {}).items():
        state, n = info.get("state"), _count(info.get("deselected"))
        if state == "off":
            if not counts:
                text = "снят"
            elif n:
                text = f"снят ({_tests(n)})"
            else:
                text = "выключен, снимать было нечего"
        elif state in ("markexpr", "env"):
            how = "-m" if state == "markexpr" else (info.get("env") or "переменную")
            text = f"включён через {how}"
            if counts and n:
                # набор включён, но тест, помеченный ещё и другим набором, снят им
                text += f", снято {_tests(n)}"
        else:
            text = f"состояние не распознано ({state or 'нет'})"
        out.append(f"{name} — {text}")
    return out


def load_coverage(root: Path) -> Tuple[Dict[str, dict], dict]:
    """Эндпоинты по id + остальные срезы отчёта как есть (включая `meta`)."""
    f = root / "coverage.json"
    if not f.is_file():
        return {}, {}
    data = json.loads(f.read_text(encoding="utf-8"))
    out: Dict[str, dict] = {}
    for e in data.get("endpoints", []):
        out[api_id(e.get("method", ""), e.get("path", ""))] = e
    extras = {k: v for k, v in data.items() if k != "endpoints"}
    return out, extras


# ---------------------------------------------------------------------------
# allure-results — тесты, шаги, проверки, примеры обмена
# ---------------------------------------------------------------------------
# «HTTP GET /api/v1/orders/1 → expect status=404» — заголовок шага, который пишет
# partest. Формат шага контрактом не объявлен; см. proposals.
_STEP_RE = re.compile(
    r"^HTTP\s+(?P<method>[A-Z]+)\s+(?P<path>\S+)(?:\s+→\s+expect status=(?P<code>[\d,\s]+))?"
)


def norm_path(path: str) -> str:
    """`/api/v1/orders/42` → `/api/v1/orders/{id}` для склейки с coverage."""
    path = path.split("?")[0].rstrip("/")
    return _ID_RE.sub("/{id}", path)


def _walk_steps(steps: List[dict], acc: List[dict], depth: int = 0) -> None:
    for s in steps or []:
        acc.append({"name": s.get("name", ""), "status": s.get("status"), "depth": depth,
                    "attachments": [a.get("name") for a in (s.get("attachments") or [])]})
        _walk_steps(s.get("steps") or [], acc, depth + 1)


def _labels(r: dict) -> Dict[str, str]:
    """Метки Allure → плоский словарь; повторяющиеся `tag` склеиваем."""
    out: Dict[str, str] = {}
    tags: List[str] = []
    for lb in r.get("labels") or []:
        name, value = lb.get("name"), lb.get("value")
        if not name:
            continue
        if name == "tag":
            tags.append(value)
        else:
            out.setdefault(name, value)
    if tags:
        out["tags"] = ", ".join(tags)
    return out


def short_error(details: dict) -> Dict[str, str]:
    """Из statusDetails вытащить то, что реально помогает: заголовок и разбор.

    Текст ошибки уходит в интерфейс, поэтому маскируется: в сообщении ассерта нередко
    оказывается весь запрос с заголовками.
    """
    msg = redact.text((details or {}).get("message") or "")
    lines = [ln.rstrip() for ln in msg.splitlines() if ln.strip()]
    head = lines[0] if lines else ""
    body = [ln.strip() for ln in lines[1:8]]
    return {"head": head[:300], "body": "\n".join(body)[:900],
            "trace": redact.text((details or {}).get("trace") or "")[-1600:]}


# Идентификатор требования: «REQ-6.5», «API-2.1». Форма общая, префикс не наш.
_REQ_REF_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,7}-\d+(?:\.\d+)*)\b")


def _req_refs(result: dict, labels: Dict[str, str]) -> List[str]:
    """Явные ссылки теста на требования: `links` и теги (маркер `req` доезжает тегом)."""
    found: List[str] = []
    for link in result.get("links") or []:
        for src in (link.get("name"), link.get("url")):
            found += _REQ_REF_RE.findall(src or "")
    found += _REQ_REF_RE.findall(labels.get("tags", ""))
    return sorted(set(found))


def nodeid_from_full(full: str) -> str:
    """`pkg.tests.x.TestY#test_z` → `pkg/tests/x.py::TestY::test_z`."""
    if "#" not in full:
        return ""
    mod, test = full.split("#", 1)
    parts = mod.split(".")
    cls = ""
    if parts and parts[-1][:1].isupper():
        cls = parts[-1]
        parts = parts[:-1]
    path = "/".join(parts) + ".py"
    return f"{path}::{cls}::{test}" if cls else f"{path}::{test}"


def _is_ui(full: str, labels: Dict[str, str], ui_markers: List[str]) -> bool:
    """UI-тест или нет. Признак задаётся в `atlas.yaml` (`tests.ui_markers`) — у
    каждого проекта свой каталог UI-тестов, и зашить его значило бы знать проект."""
    hay = f"{full} {labels.get('tags', '')} {labels.get('parentSuite', '')}"
    return any(m and m in hay for m in ui_markers)


def load_allure(root: Path, ui_markers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Вызовы по эндпоинтам и список тестов. Что из них — последний прогон, решает граф."""
    d = root / "allure-results"
    calls: Dict[str, List[dict]] = defaultdict(list)
    tests: List[dict] = []
    if not d.is_dir():
        return {"calls": calls, "tests": tests}
    markers = list(ui_markers or [])

    for f in sorted(d.glob("*result.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        full = r.get("fullName") or ""
        nodeid = nodeid_from_full(full)
        lab = _labels(r)
        start, stop = r.get("start") or 0, r.get("stop") or 0
        status = r.get("status")
        test = {
            "name": r.get("name") or full.split("#")[-1],
            "nodeid": nodeid,
            "variant": variant_of(nodeid, r.get("name") or ""),
            "paramVariant": param_variant(nodeid, r.get("parameters") or []),
            "status": status,
            "suite": full,
            "isUi": _is_ui(full, lab, markers),
            "endpoints": [],
            "start": start,
            "ms": max(0, stop - start) if start and stop else 0,
            "feature": lab.get("feature", ""),
            "story": lab.get("story", ""),
            "epic": lab.get("epic", ""),
            "parentSuite": lab.get("parentSuite", ""),
            "tags": lab.get("tags", ""),
            "params": [f"{p.get('name')}={p.get('value')}"
                       for p in redact.parameters(r.get("parameters") or [])][:6],
            "reqs": _req_refs(r, lab),
            # детали (дерево шагов, вложения) читаются по требованию
            "file": f.name,
        }
        if status in ("failed", "broken"):
            test["error"] = short_error(r.get("statusDetails") or {})
        flat: List[dict] = []
        _walk_steps(r.get("steps") or [], flat)

        current: Optional[str] = None
        for st in flat:
            m = _STEP_RE.match(st["name"])
            if m:
                eid = api_id(m.group("method"), norm_path(m.group("path")))
                current = eid
                calls[eid].append({
                    "test": test["name"],
                    "nodeid": nodeid,
                    "status": st["status"],
                    "expect": (m.group("code") or "").strip(),
                    "checks": [],
                    "attachments": st["attachments"],
                })
                if eid not in test["endpoints"]:
                    test["endpoints"].append(eid)
            elif current and calls[current] and st["depth"] > 0:
                calls[current][-1]["checks"].append(
                    {"name": redact.text(st["name"]), "status": st["status"]}
                )
        tests.append(test)
    return {"calls": calls, "tests": tests}


# ---------------------------------------------------------------------------
# всё вместе
# ---------------------------------------------------------------------------
@dataclass
class Sources:
    """Всё, что сервер прочитал для одной сборки графа."""

    cfg: Config
    artifacts: ArtifactSet
    app_map: Dict[str, Any]
    coverage: Dict[str, dict]
    coverage_extras: Dict[str, Any]
    allure: Dict[str, Any]
    containers: List[dict]
    domains: List[dict]
    depends: List[dict]
    screens: List[dict]
    screen_alias: Dict[str, Any]


def load(cfg: Config, scope: Optional[Scope] = None) -> Sources:
    arts = load_artifacts(cfg, scope)
    cov, extras = load_coverage(cfg.root)
    tests_cfg = cfg.section("tests")
    return Sources(
        cfg=cfg,
        artifacts=arts,
        app_map=app_map(cfg),
        coverage=cov,
        coverage_extras=extras,
        allure=load_allure(cfg.root, list(tests_cfg.get("ui_markers") or [])),
        containers=containers(cfg),
        domains=domains(cfg),
        depends=list(cfg.raw.get("depends") or []),
        screens=screens(cfg),
        screen_alias=dict(cfg.screens.get("alias") or {}),
    )

