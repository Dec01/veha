"""Сборка графа: источники → узлы, связи и выводы.

Слои:
    frontend  — экраны и элементы UI
    api       — эндпоинты HTTP API
    bus       — топики шины и сервисы
    storage   — таблицы БД, объектное хранилище, внешние системы

Здесь живут выводы — то, ради чего сервер существует: кто писал в таблицу, API или
потребитель шины (`afterWrite`); какой вид изменения какому методу приписать; чем
объявленное расходится с увиденным; как требование связано с тестом. Чтение источников —
в :mod:`veha.core.sources`, и сюда форматы чужих файлов не протекают дальше, чем нужно.

Каждая связь подписана тем, откуда взялась: ручная карта, снятое на прогоне, логи,
эвристика. Догадка не должна выглядеть как факт.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Optional

from partest_atlas.artifacts import Scope

from veha import config as veha_config
from veha.core import sources as src
from veha.core.sources import (PLACEHOLDER_RE, Sources, api_id, base_nodeid, norm_path,
                               norm_screen)


@dataclass
class Node:
    id: str
    label: str
    layer: str
    kind: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    kind: str
    label: str = ""


def domain_of(domains: List[dict], service: Optional[str]) -> str:
    """Домен сервиса. Сервис без домена — отдельный остров «Прочее»."""
    if service:
        low = service.strip().lower()
        for d in domains:
            if any(low == s.strip().lower() for s in d["services"]):
                return d["key"]
    return "other"


def depends_for(depends: List[dict], path: str) -> List[dict]:
    return [d for d in depends if fnmatch.fnmatch(path, d.get("path", ""))]


# --------------------------------------------------------------------------
# прогон: какие тесты считать последним прогоном и что в нём было
# --------------------------------------------------------------------------
_RUN_GAP_MS = 5 * 60 * 1000


def latest_run(tests: List[dict]) -> tuple:
    """Оставить тесты последнего прогона; вернуть их и число прогонов в папке.

    `allure-results` между запусками никто не чистит, и результаты нескольких прогонов
    лежат вперемешку. Режем по разрыву во времени старта — внутри прогона тесты идут
    вплотную, даже при параллельном запуске. Это вывод, а не чтение: граница прогона в
    файлах не записана.
    """
    timed = sorted((t for t in tests if t.get("start")), key=lambda t: t["start"])
    if not timed:
        return tests, 1
    groups: List[List[dict]] = [[timed[0]]]
    for prev, cur in zip(timed, timed[1:]):
        if cur["start"] - prev["start"] > _RUN_GAP_MS:
            groups.append([])
        groups[-1].append(cur)
    return groups[-1], len(groups)


def run_summary(tests: List[dict], runs_found: int = 1) -> Optional[dict]:
    """Сводка последнего прогона: сколько чего, сколько шло, что упало и где болит."""
    if not tests:
        return None
    by_status = Counter(t["status"] or "unknown" for t in tests)
    starts = [t["start"] for t in tests if t["start"]]
    ends = [t["start"] + t["ms"] for t in tests if t["start"]]
    bad = [t for t in tests if t["status"] in ("failed", "broken")]
    by_feature = Counter(t["feature"] or t["parentSuite"] or "—" for t in bad)
    by_error = Counter((t.get("error") or {}).get("head", "") for t in bad)
    return {
        "runsFound": runs_found,
        "total": len(tests),
        "passed": by_status.get("passed", 0),
        "failed": by_status.get("failed", 0),
        "broken": by_status.get("broken", 0),
        "skipped": by_status.get("skipped", 0),
        "startedAt": min(starts) if starts else 0,
        "finishedAt": max(ends) if ends else 0,
        "wallMs": (max(ends) - min(starts)) if starts and ends else 0,
        "sumMs": sum(t["ms"] for t in tests),
        "slowest": sorted(tests, key=lambda t: -t["ms"])[:15],
        "failures": sorted(bad, key=lambda t: (t["feature"], t["name"]))[:400],
        "failuresByFeature": by_feature.most_common(20),
        "failuresByError": by_error.most_common(20),
    }


class Joiner:
    """Соединение записи агента (nodeid pytest целиком, с параметрами) с тестом Allure.

    Структура (кто пишет в таблицу, кто публикует событие) — свойство системы, а не
    последнего запуска: строится по тестам всех прогонов в папке. Последний прогон нужен
    только для статуса и раскраски.

    Параметризованный тест — главная ловушка. Запись агента относится к **одному**
    варианту, а эндпоинты известны по тестам Allure. Порядок:

    1. Вариант известен точно (у Allure он есть в `name`, у агента — в nodeid) —
       эндпоинты этого варианта.
    2. Иначе у базового nodeid ровно один вариант и одна форма вызовов — его эндпоинты.
    3. Иначе вариантов несколько, а запись не говорит, который её. Берутся только
       эндпоинты, **общие для всех** вариантов: их вызывал любой вариант, и связь с ними
       остаётся снятой, а не придуманной. Объединение приписало бы запись одного варианта
       эндпоинтам, которых он не вызывал (rbac-тест по эндпоинтам, где один вариант пишет
       в `audit_log`, превратился бы в «все эндпоинты пишут в audit_log»).

    Подпись «параметры не различены» вместо пересечения отвергнута: ребро всё равно
    нарисовалось бы между эндпоинтом и таблицей, которых ничто не связывало, и подпись
    это не исправляет.
    """

    def __init__(self, tests: List[dict], records: List[dict]):
        # варианты, которые видит агент: несколько записей с разными параметрами при
        # одном базовом nodeid — значит, тест параметризован, даже если Allure их не
        # различает
        self.agent_variants: Dict[str, set] = defaultdict(set)
        agent_ids = set()
        for r in records:
            if r.get("nodeid"):
                self.agent_variants[base_nodeid(r["nodeid"])].add(r["nodeid"])
                agent_ids.add(r["nodeid"])

        self.by_variant: Dict[str, List[List[str]]] = defaultdict(list)
        self.variants: Dict[str, set] = defaultdict(set)
        for test in tests:
            key = self.key_of(test, agent_ids)
            if not key:
                continue
            self.variants[base_nodeid(key)].add(key)
            # Результат без вызовов (пропущен, сломался до первого запроса) ничего не
            # говорит о том, какие эндпоинты у варианта: в пересечение он внёс бы
            # пустоту и обнулил бы связь у всех остальных.
            eps = list(test.get("endpoints") or [])
            if eps and test.get("status") != "skipped":
                self.by_variant[key].append(eps)
        self._cache: Dict[str, Optional[dict]] = {}

    @staticmethod
    def key_of(test: dict, agent_ids: Optional[set] = None) -> str:
        """Вариант теста. `name` с `[...]` — точно; без него (`@allure.title`) — по
        параметрам Allure, но только если агент записал ровно такой nodeid."""
        key = test.get("variant") or test.get("nodeid") or ""
        guess = test.get("paramVariant") or ""
        if key and base_nodeid(key) == key and guess and agent_ids and guess in agent_ids:
            return guess
        return key

    @staticmethod
    def _union(sets: List[List[str]]) -> List[str]:
        out: List[str] = []
        for eps in sets:
            out += [e for e in eps if e not in out]
        return out

    @staticmethod
    def _common(sets: List[List[str]]) -> List[str]:
        if not sets:
            return []
        rest = [set(s) for s in sets[1:]]
        return [e for e in dict.fromkeys(sets[0]) if all(e in s for s in rest)]

    def resolve(self, nodeid: str) -> Optional[dict]:
        """`{"nodeid", "endpoints", "exact"}` или `None`, если теста нет в Allure.

        `exact` — запись однозначно указывает на один набор вызовов; только такие
        записи годятся для привязки требований по таблице.
        """
        if not nodeid:
            return None
        if nodeid in self._cache:
            return self._cache[nodeid]
        base = base_nodeid(nodeid)
        has_params = base != nodeid
        agent_many = len(self.agent_variants.get(base, ())) > 1
        # точный вариант: у записи есть параметры и Allure знает этот же вариант, либо
        # тест не параметризован ни у агента, ни у Allure
        known = self.variants.get(base, set())
        exact_variant = nodeid in known and (has_params or not agent_many)
        keys = [nodeid] if nodeid in self.variants.get(base, ()) \
            else sorted(self.variants.get(base, ()))
        out: Optional[dict] = None
        if keys:
            sets = [eps for k in keys for eps in self.by_variant.get(k, [])]
            ambiguous = not exact_variant and (len(keys) > 1 or agent_many or has_params)
            out = {"nodeid": keys[0] if len(keys) == 1 else base,
                   "endpoints": self._common(sets) if ambiguous else self._union(sets),
                   "exact": not ambiguous or all(s == sets[0] for s in sets)}
        self._cache[nodeid] = out
        return out

    def get(self, nodeid: str, default=None):
        return self.resolve(nodeid) or default


# --------------------------------------------------------------------------
# наборы рёбер: паутина меняется вместе с переключателем «покрытие / тесты»
# --------------------------------------------------------------------------
_COTEST_LIMIT = 1800
_METHOD_LIMIT = 1400

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: Подпись рёбер из позадачных окон агента: «эндпоинт пишет / читает таблицу»
#: (`db.effects`) и «эндпоинт публикует событие» (`bus.flow`).
#: Окно замера у агента (partest-atlas ≥ 0.2) — подготовка, вызов и уборка теста: в
#: дельту входит и то, что записали фикстуры, а эффект фикстуры модуля или сессии
#: приписан тесту, на подготовке или уборке которого она сработала. Эндпоинты теста
#: сервер берёт из тела теста в Allure, фикстур в них нет, — значит, ребро может
#: приписать эндпоинту теста запись фикстуры. Отделить её нечем: фаз запись не хранит.
#: Поэтому подпись называет окно, а не выдаёт ребро за измеренную запись эндпоинта.
WINDOW_LABEL = "снято в окне теста, с фикстурами: {}"


#: Почему запись агента не дала ни одного ребра.
#: `unjoined` (теста нет в текущих отчётах) сюда не входит: `db.effects` накопительный,
#: и записи от тестов прошлых прогонов на живом проекте есть всегда. Это не потеря, а
#: сведение — оно идёт отдельной строкой (`collected.notInReports`).
LOSS_REASONS = {
    "emptyJoin": "пустое пересечение: варианты неразличимы и общих вызовов нет",
    "filtered": "отсечено фильтром: метод не мог сделать это изменение "
                "(или событие объяснимо без шины)",
    "malformed": "запись не по форме: счётчик не число, nodeid не строка "
                 "или нет таблицы/топика — отброшена",
}


def _loss_of(test: Optional[dict]) -> Optional[str]:
    """Причина потери ещё до фильтров: теста нет или вызовов нет."""
    if test is None:
        return "unjoined"
    if not test.get("endpoints"):
        return "emptyJoin"
    return None


def _bus_edges(ax: dict, nodes: Dict[str, Node], add,
               tests_by_nodeid: dict, loss: Optional[Counter] = None) -> List[Edge]:
    """Связи через шину: кто публикует событие и кто пишет после него.

    Из ручной карты известно «эндпоинт публикует в топик», из `db.effects` — «тест
    задел таблицу», но кто записал — API или потребитель — не различалось.

    Различаем по признаку `afterWrite` из `bus.flow`: событие прошло уже после
    последнего пишущего запроса теста. Всё, что изменилось в базе дальше, сам API
    попросить записать не мог, значит это работа потребителя.

    Признак не доказательство: стенд общий, и в окно попадает чужая активность.
    Поэтому такие рёбра идут отдельным видом `consumes` и подписаны тем, откуда взялись.

    Так признак задуман. У агента (partest-atlas 0.2) он значит меньшее — «часы HTTP
    видели хоть одну пишущую запись» (его `docs/wiki/proposals.md`, «afterWrite —
    почти константа»), а плагин `bus_flow` пишет только сессионные записи. Рёбра
    `consumes` строятся лишь из позадачных записей, которых агент сегодня не пишет;
    сессионные сюда не идут вовсе. Что делать с выводом — открытый вопрос
    (`docs/wiki/proposals.md` этого репозитория).
    """
    events = ax.get("busFlow") or []
    if not events:
        return []

    tables_by_test: Dict[str, List[str]] = defaultdict(list)
    for eff in ax["dbEffects"]:
        nodeid = eff.get("nodeid")
        if not nodeid or eff.get("scope") == "session":
            continue
        if eff.get("insert") or eff.get("update") or eff.get("delete"):
            tables_by_test[nodeid].append(eff["table"])

    # Таблицы, которые пишутся и без всякой шины. Дельта `db.effects` относится ко
    # всему тесту, а тест сначала засевает данные через API. Если таблицу пишет хоть
    # один тест, у которого события в тишине не было, — она объяснима без шины, и
    # приписывать её потребителю нечестно.
    silent_tests = {ev.get("nodeid") for ev in events if ev.get("afterWrite")}
    explained: set = set()
    for nodeid, tables in tables_by_test.items():
        if nodeid not in silent_tests:
            explained.update(tables)

    out: List[Edge] = []
    seen: set = set()
    # Тесты, а не записи: агент копит прогоны в одном файле, и один тест даёт по записи
    # на прогон. Ключ узла появляется, только если позадачные записи у топика есть.
    silent_by_topic: Dict[str, set] = {}
    for ev in events:
        topic = ev.get("topic")
        if not topic:
            continue
        tid = f"topic:{topic}"
        if tid not in nodes:
            add(Node(id=tid, label=topic, layer="bus", kind="topic",
                     meta={"observed": True}))
        meta = nodes[tid].meta
        # Сумма по записям файла, а не число за прогон: агент дописывает прогоны в один
        # файл, не размечая их, и схлопывает совпадающие записи. Отсюда имена `file*`.
        meta["fileEvents"] = int(meta.get("fileEvents") or 0) + ev["events"]
        meta["fileRecords"] = int(meta.get("fileRecords") or 0) + 1

        if not ev.get("nodeid") or ev.get("scope") == "session":
            # Сессионный срез: тест за событием не стоит — не потеря. И `afterWrite` у
            # него ничего не говорит о тишине: у агента он значит «часы HTTP видели
            # хоть одну пишущую запись» (у прогона, который писал, — почти всегда
            # `true`), а под pytest-xdist часы живут в воркерах, и он всегда `false`
            # при нулевых `http`. Ни то, ни другое не «писал потребитель» — в счёт
            # тишины сессионные записи не идут.
            continue
        silent = silent_by_topic.setdefault(tid, set())
        if ev.get("afterWrite"):
            silent.add(ev["nodeid"])
        test = tests_by_nodeid.get(ev.get("nodeid") or "")
        lost = _loss_of(test)
        if lost:
            if loss is not None:
                loss[lost] += 1
            continue
        produced = 0

        # публикация: событие появилось, пока тест дёргал пишущие эндпоинты
        for eid in test.get("endpoints") or []:
            node = nodes.get(eid)
            if node is None or (node.meta.get("method") or "").upper() not in _WRITE_METHODS:
                continue
            produced += 1
            key = (eid, tid, "publishes")
            if key not in seen:
                seen.add(key)
                out.append(Edge(eid, tid, "publishes", WINDOW_LABEL.format("событие")))

        # потребитель — только если событие прошло в тишине записей теста
        for table in (tables_by_test.get(ev.get("nodeid") or "", [])
                      if ev.get("afterWrite") else []):
            if table in explained:
                continue
            produced += 1
            sid = f"store:{table}"
            if sid not in nodes:
                add(Node(id=sid, label=table, layer="storage", kind="table",
                         meta={"observed": True}))
            key = (tid, sid, "consumes")
            if key not in seen:
                seen.add(key)
                out.append(Edge(tid, sid, "consumes",
                                "снято: событие в тишине записей"))
        if not produced and loss is not None:
            loss["filtered"] += 1
    for tid, silent in silent_by_topic.items():
        nodes[tid].meta["silentTests"] = len(silent)
    return out


#: Подпись узла для элемента, у которого нет `data-testid`.
_ANON_CONTROL = "элемент без data-testid"


def _collected_edges(ax: dict, nodes: Dict[str, Node], add, tests_by_nodeid: dict,
                     loss: Optional[Counter] = None):
    """Рёбра, снятые сборщиками, а не описанные руками.

    `ui.flow` даёт «элемент экрана → эндпоинт» по реальным кликам, `db.effects` —
    «эндпоинт → таблица» по дельте статистики вокруг теста. Оба источника помечаются
    `observed`, чтобы было видно, где карта ручная, а где снята с прогона.

    Элемент в `ui.flow` назван своим `data-testid` (поле `control`). Если его нет,
    агент не сохраняет ничего, что отличало бы один такой элемент экрана от другого, —
    и сервер честно не различает их: все безымянные элементы экрана сводятся в один
    узел «элемент без data-testid» этого экрана. Склеивать по видимому тексту нечем:
    текста в артефакте нет.
    """
    ui_edges: List[Edge] = []
    seen_ui = set()

    by_testid = {n.meta.get("testid"): nid for nid, n in nodes.items()
                 if n.kind == "control" and n.meta.get("testid")}

    def _control_id(link: dict) -> str:
        testid = link.get("control")
        if testid:
            return by_testid.get(testid) or f"ui:{testid}"
        return f"ui:?{norm_screen(link.get('screen'))}"

    for link in ax["uiFlow"]:
        eid = api_id(link.get("method", ""), link.get("path", ""))
        if eid not in nodes:
            continue
        testid = link.get("control")
        uid = _control_id(link)
        if uid not in nodes:
            add(Node(id=uid,
                     label=testid or _ANON_CONTROL,
                     layer="frontend", kind="control",
                     meta={"testid": testid,
                           "screenPath": norm_screen(link.get("screen")),
                           "action": link.get("action"),
                           "confidence": link.get("confidence"),
                           "observed": True, "renders": [],
                           **({} if testid else {
                               "note": "агент не записал, какой это элемент: у него нет "
                                       "data-testid. Все такие элементы экрана сведены "
                                       "в этот узел"})}))
            if testid:
                by_testid[testid] = uid
        key = (uid, eid)
        if key in seen_ui:
            continue
        seen_ui.add(key)
        ui_edges.append(Edge(uid, eid, "calls", link.get("confidence", "")))

    # эффекты привязаны к тесту; тест знает, какие эндпоинты дёргал
    db_edges: List[Edge] = []
    seen_db = set()
    for eff in ax["dbEffects"]:
        if not eff.get("table"):
            continue
        # Сессионный замер относится ко всему прогону: конкретного теста, а значит и
        # эндпоинта, за ним нет. Связь из него построить нельзя, но сам факт «прогон
        # задел таблицу» ценен — вешаем его на таблицу.
        if eff.get("scope") == "session" or not eff.get("nodeid"):
            tid = f"store:{eff['table']}"
            if tid not in nodes:
                add(Node(id=tid, label=eff["table"], layer="storage", kind="table",
                         meta={"observed": True}))
            meta = nodes[tid].meta
            # Сумма по сессионным записям файла, а не «что сделал прогон»: агент копит
            # прогоны в одном файле без метки прогона и схлопывает совпадающие записи,
            # поэтому разложить сумму по прогонам нельзя. Число записей — рядом, чтобы
            # было видно, из скольких слагаемых она.
            touched = meta.setdefault("sessionTouched",
                                      {"insert": 0, "update": 0, "delete": 0, "records": 0})
            for k in ("insert", "update", "delete"):
                touched[k] += eff[k]
            touched["records"] += 1
            continue

        test = tests_by_nodeid.get(eff.get("nodeid"))
        lost = _loss_of(test)
        if lost:
            if loss is not None:
                loss[lost] += 1
            continue
        produced = 0
        tid = f"store:{eff['table']}"
        if tid not in nodes:
            add(Node(id=tid, label=eff["table"], layer="storage", kind="table",
                     meta={"observed": True}))
        # Дельта относится ко всему тесту, а тест обычно дёргает несколько эндпоинтов.
        # Приписывать вставку всем подряд нечестно — получался «GET пишет в таблицу».
        # Поэтому вид изменения сопоставляется с методом.
        by_method = {
            "insert": {"POST", "PUT"},
            "update": {"PUT", "PATCH", "POST"},
            "delete": {"DELETE"},
        }
        for change, methods in by_method.items():
            if not eff.get(change):
                continue
            for eid in test.get("endpoints") or []:
                node = nodes.get(eid)
                if node is None or (node.meta.get("method") or "").upper() not in methods:
                    continue
                produced += 1
                key = (eid, tid, "writes", change)
                if key in seen_db:
                    continue
                seen_db.add(key)
                db_edges.append(Edge(eid, tid, "writes", WINDOW_LABEL.format(change)))

        # Чтение: если тест ничего не менял, а таблицу задел, это был GET.
        if not (eff.get("insert") or eff.get("update") or eff.get("delete")):
            for eid in test.get("endpoints") or []:
                node = nodes.get(eid)
                if node is None or (node.meta.get("method") or "").upper() != "GET":
                    continue
                produced += 1
                key = (eid, tid, "reads", "select")
                if key in seen_db:
                    continue
                seen_db.add(key)
                db_edges.append(Edge(eid, tid, "reads", WINDOW_LABEL.format("select")))
        if not produced and loss is not None:
            loss["filtered"] += 1
    return ui_edges, db_edges


def _apply_contract(ax: dict, nodes: Dict[str, Node], calls: Dict[str, List[dict]],
                    add) -> Dict[str, Any]:
    """Контракт OpenAPI на узлах и сверка «объявлено против увиденного».

    * эндпоинт объявлен, но за прогон его никто не дёрнул;
    * эндпоинт дёргали, а в контракте его нет;
    * тест ждал код, которого контракт не объявляет.
    """
    ops = ax.get("openapi") or []
    if not ops:
        return {"operations": 0}

    def key(method: str, path: str) -> str:
        return f"{(method or '').upper()} {PLACEHOLDER_RE.sub('{id}', (path or '').rstrip('/'))}"

    by_key = {key(o["method"], o["path"]): o for o in ops}
    node_keys = {key(n.meta.get("method"), n.meta.get("path")): nid
                 for nid, n in nodes.items() if n.kind == "endpoint"}

    declared_unused: List[str] = []
    undeclared_codes: List[dict] = []
    missing_from_contract: List[str] = []

    for k, op in by_key.items():
        nid = node_keys.get(k)
        if nid is None:
            # контракт объявляет путь, которого нет даже среди вызванных — создаём
            # узел: иначе объявленное, но не тронутое, невидимо
            method, path = k.split(" ", 1)
            nid = api_id(method, path)
            add(Node(id=nid, label=f"{method} {path}", layer="api", kind="endpoint",
                     meta={"method": method, "path": path, "status": "unseen",
                           "coverage": None, "executed": [], "missing": [],
                           "note": "объявлен в контракте, в прогоне не встречался"}))
            node_keys[k] = nid
        meta = nodes[nid].meta
        meta["contract"] = {
            "operationId": op.get("operationId"),
            "tags": op.get("tags") or [],
            "deprecated": op.get("deprecated"),
            "params": op.get("params") or [],
            "body": op.get("body"),
            "responses": op.get("responses") or [],
        }
        # контроллер из контракта — авторитетнее догадки по сегменту пути
        if not meta.get("service") and op.get("tags"):
            meta["service"] = op["tags"][0]

        records = calls.get(nid) or []
        if not records:
            declared_unused.append(nid)
            continue
        seen_codes = {c.strip() for r in records
                      for c in (r.get("expect") or "").split(",") if c.strip()}
        extra = sorted(seen_codes - set(op.get("responses") or []))
        if extra:
            meta["contractGap"] = extra
            undeclared_codes.append({"id": nid, "codes": extra})

    for nid, node in nodes.items():
        if node.kind != "endpoint" or node.meta.get("contract"):
            continue
        if calls.get(nid):
            node.meta["contractMissing"] = True
            missing_from_contract.append(nid)

    # Сводка по кодам. Если контракт не объявляет ни одного ошибочного кода, то
    # «тест ждал код, которого контракт не объявляет» верно почти везде. Это один
    # системный факт, а не сотня находок, и показывать его надо один раз.
    declared_codes = Counter(c for o in ops for c in (o.get("responses") or []))
    error_codes = {c for c in declared_codes if c[:1] in ("4", "5")}
    systemic = not error_codes and len(undeclared_codes) > len(ops) / 3

    return {
        "operations": len(ops),
        "declaredUnused": sorted(declared_unused),
        "undeclaredCodes": undeclared_codes,
        "missingFromContract": sorted(missing_from_contract),
        "codes": {
            "declared": dict(declared_codes.most_common()),
            "errorCodesDeclared": sorted(error_codes),
            "perOperation": dict(Counter(len(o.get("responses") or [])
                                         for o in ops).most_common()),
            "systemic": systemic,
        },
    }


def _log_edges(ax: dict, nodes: Dict[str, Node], add, cfg: veha_config.Config) -> List[Edge]:
    """Связи из логов контейнеров: кто какую таблицу и какой топик называет.

    Это факт о **контейнере**, а не об эндпоинте: из строки лога не видно, в рамках
    какого запроса она возникла. Поэтому связи отдельного вида `logged` и не участвуют
    в цепочке как поток — иначе один контейнер API соединил бы всё со всем.

    Агент пишет контейнер тем именем, которое отдал источник журналов; ключ узла
    находится через `atlas.yaml` (ключ или `container_names`).
    """
    out: List[Edge] = []
    for entry in ax.get("containerLogs") or []:
        name = entry.get("name") or ""
        key = cfg.container_key(name) or name
        sid = f"svc:{key}"
        if sid not in nodes:
            continue          # контейнер не попал на сцену — вешать не на что
        found = entry.get("found") or {}
        meta = nodes[sid].meta
        meta["logHits"] = found
        for table, count in (found.get("table") or {}).items():
            tid = f"store:{table}"
            if tid not in nodes:
                add(Node(id=tid, label=table, layer="storage", kind="table",
                         meta={"observed": True}))
            # на таблице отмечаем, какой контейнер её реально называет: это и есть
            # проверка ручной разметки `containers.match`
            owners = nodes[tid].meta.setdefault("logOwners", {})
            owners[key] = owners.get(key, 0) + count
            out.append(Edge(sid, tid, "logged", f"в логах {count}×"))
        for topic, count in (found.get("topic") or {}).items():
            tid = f"topic:{topic}"
            if tid not in nodes:
                add(Node(id=tid, label=topic, layer="bus", kind="topic",
                         meta={"observed": True}))
            out.append(Edge(sid, tid, "logged", f"в логах {count}×"))
    return out


def _cotest_edges(tests: List[dict], known: set, limit: int = _COTEST_LIMIT) -> List[dict]:
    """«Вызывались в одном тесте»: вес — сколько тестов связывают пару.

    Сценарные тесты дёргают десятки эндпоинтов и связали бы всё со всем, поэтому такие
    в попарные связи не берём — их путь и так нарисован структурными рёбрами.
    """
    pair: Counter = Counter()
    by_feature: Dict[str, set] = defaultdict(set)
    for t in tests:
        eps = sorted({e for e in t.get("endpoints") or [] if e in known})
        feature = t.get("feature") or t.get("parentSuite")
        if feature:
            by_feature[feature].update(eps)
        if not 2 <= len(eps) <= 12:
            continue
        for a, b in combinations(eps, 2):
            pair[(a, b)] += 1

    out = [{"source": a, "target": b, "kind": "cotest", "w": w}
           for (a, b), w in pair.most_common(limit)]

    # Вторая честная связь — общая фича Allure: эндпоинты, которые проверяются как
    # одна область. Рисуется слабее, чем прямая ко-встречаемость.
    seen = {(e["source"], e["target"]) for e in out}
    room = max(0, limit - len(out))
    feat: Counter = Counter()
    for eps in by_feature.values():
        ids = sorted(eps)
        if not 2 <= len(ids) <= 16:
            continue
        for a, b in combinations(ids, 2):
            if (a, b) not in seen:
                feat[(a, b)] += 1
    out += [{"source": a, "target": b, "kind": "cofeature", "w": w}
            for (a, b), w in feat.most_common(room)]
    return out


def _methodology_edges(cov: Dict[str, dict], known: set,
                       limit: int = _METHOD_LIMIT) -> List[dict]:
    """Связи по методике: общий подтип оси A и общие пробелы оси B.

    Признак, встречающийся у половины эндпоинтов, ничего не различает — такие группы
    пропускаем, иначе получится равномерный ком без структуры.
    """
    by_subtype: Dict[str, List[str]] = defaultdict(list)
    by_gap: Dict[str, List[str]] = defaultdict(list)
    for eid, e in cov.items():
        if eid not in known:
            continue
        if e.get("subtype"):
            by_subtype[e["subtype"]].append(eid)
        for tc in e.get("missing") or []:
            by_gap[tc].append(eid)

    def _pairs(groups: Dict[str, List[str]], cap: int) -> Counter:
        acc: Counter = Counter()
        for ids in groups.values():
            if not 2 <= len(ids) <= cap:
                continue
            for a, b in combinations(sorted(ids), 2):
                acc[(a, b)] += 1
        return acc

    gaps = _pairs(by_gap, 34)
    subs = _pairs(by_subtype, 22)
    out = [{"source": a, "target": b, "kind": "gap", "w": w}
           for (a, b), w in gaps.most_common(limit)]
    seen = {(e["source"], e["target"]) for e in out}
    room = max(0, limit - len(out))
    out += [{"source": a, "target": b, "kind": "subtype", "w": w}
            for (a, b), w in subs.most_common(room * 2)
            if (a, b) not in seen][:room]
    return out


def _norm_feature(text: str) -> str:
    return re.sub(r"[^\wа-яё]+", " ", (text or "").lower()).strip()


def tests_for_links(tests: List[dict]) -> List[dict]:
    """Тесты всех прогонов, по одному на вариант; статус — последнего прогона, где он был.

    Привязка требования к тесту — структура, как и связи на сцене: перегнали один кейс —
    и требование не должно стать «не проверенным». Статус же берётся из последнего
    появления теста, то есть из последнего прогона, если тест в нём был.
    """
    out: Dict[str, dict] = {}
    for test in sorted(tests, key=lambda x: x.get("start") or 0):
        out[test.get("variant") or test["nodeid"]] = test
    return list(out.values())


#: Худший статус варианта — первым: требование «проверено», только если прошли все.
_STATUS_RANK = {"failed": 5, "broken": 4, None: 3, "unknown": 3, "skipped": 2, "passed": 0}


#: Насколько время прогона в истории и время тестов в Allure могут расходиться, секунд.
_RUN_MATCH_SLACK = 60


def last_run_is_full(history: List[dict], latest: List[dict]) -> bool:
    """Последний прогон в отчётах Allure — полный и доведённый до конца?

    Да, только если последняя запись истории — набор с `full: true`, прогон завершён
    (`complete`, `status == "done"`) и по времени это тот же прогон, что последний в
    Allure. Перезапуск одного кейса, прогон из CI мимо сервера, оборванный прогон —
    нет: по ним нельзя судить, что вариантов, которых в нём нет, больше не существует.
    """
    if not history or not latest:
        return False
    last = history[-1]
    if not (last.get("suiteFull") and last.get("complete") and last.get("status") == "done"):
        return False
    starts = [t["start"] for t in latest if t.get("start")]
    if not starts:
        return False
    first = min(starts) / 1000
    began = float(last.get("startedAt") or 0)
    ended = float(last.get("finishedAt") or began)
    return began - _RUN_MATCH_SLACK <= first <= ended + _RUN_MATCH_SLACK


def _test_entry(base: str, how: str, variants: List[dict], latest_keys: set,
                last_run_full: bool = False) -> dict:
    """Тест у требования: по базовому nodeid, худший статус из вариантов, когда тест
    видели последний раз и был ли он в последнем прогоне.

    Худший статус берётся только из вариантов последнего прогона, **если этот прогон
    полный** (`last_run_full`): тогда вариант `[old]`, упавший неделю назад и убранный
    из набора, требование не красит. Если последний прогон — перезапуск одного кейса
    или неполный набор, вариантов в нём меньше, чем существует, и худший статус берётся
    по всем вариантам, каждый — со своим последним статусом: перезапуск `[a]` не должен
    скрывать упавший `[b]`.
    """
    current = [v for v in variants if (v.get("variant") or v["nodeid"]) in latest_keys]
    pool = current if (last_run_full and current) else variants
    worst = max(pool, key=lambda v: _STATUS_RANK.get(v.get("status"), 3))
    seen = [v.get("start") or 0 for v in variants]
    return {"nodeid": base, "how": how, "name": worst.get("name", base),
            "status": worst.get("status"), "file": worst.get("file"),
            "variants": len(variants), "lastSeen": max(seen) if seen else 0,
            "notInLastRun": not current}


def _link_specs(requirements: List[dict], nodes: Dict[str, Node], tests: List[dict],
                db_effects: List[dict], joiner: Optional["Joiner"] = None,
                latest_keys: Optional[set] = None,
                last_run_full: bool = False) -> Dict[str, Any]:
    """Связать требования с узлами карты и с тестами.

    Способы, от точного к приблизительному, и каждая связь подписана своим:

    * `direct` — тест сам сослался на требование (`@allure.link`, тег);
    * `anchor` — требование упоминает эндпоинт, тест его дёргает;
    * `table` — требование упоминает таблицу, тест её задел;
    * `feature` — фича/story Allure совпала с названием раздела.
    """
    reqs = requirements or []
    if not reqs:
        return {"requirements": [], "sources": []}

    by_path: Dict[str, List[str]] = defaultdict(list)
    for n in nodes.values():
        if n.kind == "endpoint" and n.meta.get("path"):
            by_path[norm_path(n.meta["path"])].append(n.id)

    tests_by_feature: Dict[str, List[dict]] = defaultdict(list)
    for t in tests:
        for key in (t.get("feature"), t.get("story")):
            if key:
                tests_by_feature[_norm_feature(key)].append(t)
    tests_by_endpoint: Dict[str, List[dict]] = defaultdict(list)
    for t in tests:
        for eid in t.get("endpoints") or []:
            tests_by_endpoint[eid].append(t)

    # тест у требования — базовый nodeid: у параметризованного теста варианты одного
    # требования, и статус берётся худший из них
    variants_of: Dict[str, List[dict]] = defaultdict(list)
    for t in tests:
        variants_of[base_nodeid(t["nodeid"])].append(t)

    # «тест задел таблицу» — из снятых эффектов; только позадачные записи
    by_variant = {(t.get("variant") or t["nodeid"]): t for t in tests}
    tests_by_table: Dict[str, List[dict]] = defaultdict(list)
    for eff in db_effects:
        # только точное соединение: какой из неразличимых вариантов задел таблицу,
        # неизвестно, и привязать требование ко всем значило бы придумать связь
        hit = joiner.resolve(eff.get("nodeid") or "") if joiner else None
        t = by_variant.get(hit["nodeid"]) if hit and hit["exact"] else None
        if t and eff.get("table"):
            tests_by_table[f"store:{eff['table']}"].append(t)

    out: List[dict] = []
    for r in reqs:
        anchors = r.get("anchors") or {}
        methods = set(anchors.get("method") or [])
        node_ids: List[str] = []
        missing: List[str] = []
        for path in anchors.get("api") or []:
            found = [eid for eid in by_path.get(path, [])
                     if not methods or nodes[eid].meta.get("method") in methods]
            if found:
                node_ids += found
            elif not by_path.get(path):
                # спецификация описывает путь, которого в реализации нет —
                # это расхождение, и терять его нельзя
                missing.append(path)
        for table in anchors.get("table") or []:
            tid = f"store:{table}"
            if tid in nodes:
                node_ids.append(tid)
        for key in anchors.get("screen") or []:
            sid = f"screen:{key}"
            if sid in nodes:
                node_ids.append(sid)
        node_ids = sorted(set(node_ids))

        linked: Dict[str, str] = {}          # базовый nodeid теста → способ привязки
        for t in tests:
            if r["id"] in (t.get("reqs") or []):
                linked[base_nodeid(t["nodeid"])] = "direct"
        for eid in node_ids:
            for t in tests_by_endpoint.get(eid, []):
                linked.setdefault(base_nodeid(t["nodeid"]), "anchor")
            for t in tests_by_table.get(eid, []):
                linked.setdefault(base_nodeid(t["nodeid"]), "table")
        for t in tests_by_feature.get(_norm_feature(r.get("title", "")), []):
            linked.setdefault(base_nodeid(t["nodeid"]), "feature")

        ways = set(linked.values())
        best = next((w for w in ("direct", "anchor", "table", "feature")
                     if w in ways), "none")
        out.append({
            "id": r["id"], "title": r.get("title", ""), "section": r.get("section", ""),
            "level": r.get("level", 0), "path": r.get("path") or [],
            "source": r.get("source", ""),
            "lead": r.get("lead") or r.get("text", ""),
            "chars": len(r.get("text") or ""), "anchors": anchors,
            "nodes": node_ids, "missingApi": missing,
            # имя и статус кладём сюда: сцене список тестов не отдают, а вкладке
            # требований он нужен без второго запроса
            "tests": [_test_entry(nid, how, variants_of[nid], latest_keys or set(),
                                  last_run_full)
                      for nid, how in sorted(linked.items())][:60],
            "testCount": len(linked),
            "how": best,
        })

    # обратная сторона: на узле — какие требования его описывают
    for req in out:
        for nid in req["nodes"]:
            nodes[nid].meta.setdefault("requirements", []).append(
                {"id": req["id"], "title": req["title"], "source": req["source"],
                 "how": req["how"]})

    per_source = Counter(r["source"] for r in out)
    return {"requirements": out,
            "sources": [{"label": s, "requirements": n} for s, n in per_source.items()]}


def _count(value: Any) -> Optional[int]:
    """Счётчик записи агента: целое (или целое число с плавающей точкой), нет — ноль.
    `None` — значение не число: запись не по форме."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _clean_records(records: Any, name_key: str, counters: tuple) -> tuple:
    """Записи `db.effects` / `bus.flow`, приведённые к форме, и число отброшенных.

    Контракт проверяет шапку артефакта, а не поля записей. Запись с счётчиком-строкой
    или с nodeid не строкой роняла бы `/api/graph` целиком (`int()`, `base_nodeid`);
    такая запись отбрасывается и считается потерей `malformed`, а не молча.
    """
    if not isinstance(records, list):
        return [], (0 if records in (None, {}, "") else 1)
    out: List[dict] = []
    bad = 0
    for r in records:
        if not isinstance(r, dict):
            bad += 1
            continue
        name = r.get(name_key)
        nums = {k: _count(r.get(k)) for k in counters}
        if (not isinstance(name, str) or not name
                or any(v is None for v in nums.values())
                or any(not isinstance(r.get(k), (str, type(None)))
                       for k in ("nodeid", "test", "scope"))
                or not isinstance(r.get("afterWrite"), (bool, int, type(None)))):
            bad += 1
            continue
        out.append({**r, **nums, "nodeid": r.get("nodeid") or "",
                    "test": r.get("test") or ""})
    return out, bad


def build(s: Sources) -> dict:
    """Собрать граф из прочитанных источников."""
    arts = s.artifacts
    db_effects, bad_effects = _clean_records(arts.payload("db.effects"), "table",
                                             ("insert", "update", "delete"))
    bus_flow, bad_bus = _clean_records(arts.payload("bus.flow"), "topic", ("events",))
    ax = {
        "uiFlow": arts.payload("ui.flow"),
        "dbEffects": db_effects,
        "busFlow": bus_flow,
        "containerLogs": arts.payload("containers.logs"),
        "openapi": arts.payload("contract.openapi"),
    }
    db = {"databases": arts.payload("db.schema"),
          "stats": arts.payload("db.schema", "stats", {})}
    amap = s.app_map
    cov, cov_extras = s.coverage, s.coverage_extras
    allure = s.allure
    calls: Dict[str, List[dict]] = allure["calls"]
    latest, runs_found = latest_run(allure["tests"])
    run = run_summary(latest, runs_found)
    # статус и раскраска — по последнему прогону: иначе узел красится по результату
    # старого запуска, лежащего в той же папке
    tests_by_nodeid = {t["nodeid"]: t for t in latest}
    # структура — по всем прогонам в папке, с различением параметризованных вариантов
    structure_tests = Joiner(allure["tests"], [*ax["dbEffects"], *ax["busFlow"]])

    nodes: Dict[str, Node] = {}
    edges: List[Edge] = []

    def add(node: Node) -> Node:
        nodes.setdefault(node.id, node)
        return nodes[node.id]

    # --- api: объединение coverage.json и того, что видели в Allure ---------
    # Сервис знают только эндпоинты из coverage.json. Остальные остались бы без
    # домена и свалились в один остров «Прочее», поэтому достраиваем по сегменту пути.
    svc_by_seg: Dict[str, str] = {}
    for e in cov.values():
        seg = (e.get("path") or "").split("/")[3:4]
        label = e.get("serviceLabel") or e.get("service")
        if seg and seg[0] and label:
            svc_by_seg.setdefault(seg[0].lower(), label)

    def _service_of(path: Optional[str], service: Optional[str]) -> Optional[str]:
        if service:
            return service
        seg = (path or "").split("/")[3:4]
        return svc_by_seg.get(seg[0].lower()) if seg and seg[0] else None

    def _owner(path: str, service: Optional[str]) -> Dict[str, Any]:
        svc = _service_of(path, service)
        return {
            "container": s.cfg.container_for(path or ""),
            "service": svc,
            "domain": domain_of(s.domains, svc),
            "depends": depends_for(s.depends, path or ""),
        }

    for eid, e in cov.items():
        svc = e.get("serviceLabel") or e.get("service")
        add(Node(
            id=eid, label=f"{e['method']} {e['path']}", layer="api", kind="endpoint",
            meta={
                **_owner(e.get("path"), svc),
                "method": e.get("method"), "path": e.get("path"),
                "subtype": e.get("subtype"),
                "coverage": e.get("coverage"), "status": e.get("status"),
                "executed": e.get("executed") or [], "missing": e.get("missing") or [],
                "calls": e.get("calls"), "timing": e.get("timing") or {},
            },
        ))
    for eid in calls:
        if eid in nodes:
            continue
        method, path = eid[4:].split(" ", 1)
        add(Node(id=eid, label=f"{method} {path}", layer="api", kind="endpoint",
                 meta={**_owner(path, None),
                       "method": method, "path": path, "status": "unseen",
                       "coverage": None, "executed": [], "missing": []}))

    # прогон: тесты и проверки на эндпоинт
    for eid, records in calls.items():
        if eid not in nodes:
            continue
        meta = nodes[eid].meta
        meta["observed"] = records[:40]
        meta["testCount"] = len({r["nodeid"] for r in records})
        meta["checkCount"] = sum(len(r["checks"]) for r in records)

        involved = {r["nodeid"] for r in records} & set(tests_by_nodeid)
        bad = sorted(nid for nid in involved
                     if (tests_by_nodeid.get(nid) or {}).get("status")
                     in ("failed", "broken"))
        meta["runTotal"] = len(involved)
        meta["runFailed"] = len(bad)
        meta["runStatus"] = "failed" if bad else ("passed" if involved else "none")
        meta["failedTests"] = [
            {"nodeid": nid,
             "name": tests_by_nodeid[nid]["name"],
             "error": tests_by_nodeid[nid].get("error", {})}
            for nid in bad[:25]
        ]

    # --- frontend: ручная карта экранов и элементов --------------------------
    screens = amap.get("screens") or {}
    screen_path = {key: (spec or {}).get("path") for key, spec in screens.items()}
    for key, sc in screens.items():
        add(Node(id=f"screen:{key}", label=sc.get("label", key), layer="frontend",
                 kind="screen", meta={"path": sc.get("path"),
                                      # общий ключ с контролами (`screenPath` у них
                                      # тоже через norm_screen): в карте экран
                                      # `/#/orders/{orderId}`, у рекордера — `{id}`
                                      "screenPath": norm_screen(sc.get("path")),
                                      "pageObject": sc.get("page_object"),
                                      "frame": sc.get("frame") or []}))
    for el in amap.get("ui_elements") or []:
        uid = f"ui:{el['testid']}"
        add(Node(id=uid, label=el.get("label", el["testid"]), layer="frontend",
                 kind="control",
                 meta={"testid": el["testid"], "screen": el.get("screen"),
                       # Путь — общий ключ для ручной карты и снятых связей: в карте
                       # экран зовётся `orders`, а рекордер отдаёт `/#/orders`.
                       "screenPath": norm_screen(screen_path.get(el.get("screen"))),
                       "slot": el.get("slot") or {"x": 50, "y": 50},
                       "renders": el.get("renders") or [], "note": el.get("note")}))
        if el.get("screen"):
            edges.append(Edge(f"screen:{el['screen']}", uid, "contains"))
        for call in el.get("calls") or []:
            method, path = call.split(" ", 1)
            eid = api_id(method, path)
            if eid not in nodes:
                add(Node(id=eid, label=call, layer="api", kind="endpoint",
                         meta={"method": method, "path": path, "status": "unseen",
                               "coverage": None, "executed": [], "missing": [],
                               "note": "объявлен в карте, не найден в coverage.json"}))
            edges.append(Edge(uid, eid, "calls"))

    # --- bus / сервисы ------------------------------------------------------
    for key, sv in (amap.get("services") or {}).items():
        layer = "bus" if sv.get("kind") in ("backend", "bus") else "storage"
        add(Node(id=f"svc:{key}", label=sv.get("label", key), layer=layer,
                 kind=sv.get("kind", "service"), meta={"note": sv.get("note")}))

    for ig in amap.get("integrations") or []:
        source = ig["from"]
        dst = ig["to"]
        src_id = source if ":" in source.split(" ")[0] else api_id(*source.split(" ", 1))
        for nid in (src_id, dst):
            if nid.startswith("topic:") and nid not in nodes:
                add(Node(id=nid, label=nid[6:], layer="bus", kind="topic", meta={}))
            if nid.startswith("store:") and nid not in nodes:
                add(Node(id=nid, label=nid[6:], layer="storage", kind="store", meta={}))
        if src_id not in nodes:
            method, path = src_id[4:].split(" ", 1)
            add(Node(id=src_id, label=f"{method} {path}", layer="api", kind="endpoint",
                     meta={"method": method, "path": path, "status": "unseen"}))
        edges.append(Edge(src_id, dst, ig.get("kind", "flow"), ig.get("label", "")))

    # --- storage: таблицы из ручной карты -----------------------------------
    for section, kind in (("storage_writes", "writes"), ("storage_reads", "reads")):
        for call, tables in (amap.get(section) or {}).items():
            method, path = call.split(" ", 1)
            eid = api_id(method, path)
            if eid not in nodes:
                add(Node(id=eid, label=call, layer="api", kind="endpoint",
                         meta={"method": method, "path": path, "status": "unseen"}))
            for t in tables:
                tid = f"store:{t}"
                if tid not in nodes:
                    add(Node(id=tid, label=t, layer="storage", kind="table", meta={}))
                edges.append(Edge(eid, tid, kind))

    # --- внешние зависимости: кого дёргает backend, обслуживая путь ---------
    for c in s.containers:
        if c["kind"] in ("external", "auth", "worker"):
            add(Node(id=f"svc:{c['key']}", label=c["label"],
                     layer="bus" if c["kind"] == "worker" else "storage",
                     kind=c["kind"], meta={"note": c["note"]}))
    for node in list(nodes.values()):
        for dep in node.meta.get("depends") or []:
            target = f"svc:{dep['to']}"
            if target in nodes:
                edges.append(Edge(node.id, target, "depends", dep.get("label", "")))

    # --- связи, снятые агентом на прогоне -----------------------------------
    # записи агента, не давшие ни одного ребра, — по причинам; потеря не должна быть тихой
    loss: Counter = Counter()
    loss["malformed"] += bad_effects + bad_bus
    observed_ui, observed_db = _collected_edges(ax, nodes, add, structure_tests, loss)
    edges.extend(observed_ui)
    edges.extend(observed_db)
    observed_bus = _bus_edges(ax, nodes, add, structure_tests, loss)
    edges.extend(observed_bus)
    observed_logs = _log_edges(ax, nodes, add, s.cfg)
    edges.extend(observed_logs)

    # --- контракт: что объявлено против того, что видели --------------------
    contract = _apply_contract(ax, nodes, calls, add)

    # --- требования из спецификаций: «как должно работать» ------------------
    specs = _link_specs(arts.payload("specs.requirements"), nodes,
                        tests_for_links(allure["tests"]), ax["dbEffects"], structure_tests,
                        {(t.get("variant") or t["nodeid"]) for t in latest},
                        last_run_is_full(arts.payload("runner.history"), latest))


    # Частичный прогон (контракт `coverage-payload` partest: `meta.partialRun`,
    # `meta.selection`) — не то же самое, что полный: сравнивать его средний процент с
    # полным прогоном нельзя, и сцена обязана это сказать.
    cov_meta = (cov_extras.get("meta") or {}) if isinstance(cov_extras, dict) else {}
    optin = src.optin_record(cov_meta)

    tests = allure["tests"]
    known = set(nodes)
    edge_sets = {
        "structure": [e.__dict__ for e in edges],
        "tests": _cotest_edges(latest, known),
        "coverage": _methodology_edges(cov, known),
    }
    artifacts_status = arts.as_dict()
    return {
        "meta": {
            "endpoints": sum(1 for n in nodes.values() if n.kind == "endpoint"),
            "nodes": len(nodes),
            "edges": len(edges),
            "tests": len(tests),
            "uiTests": sum(1 for t in tests if t["isUi"]),
            "hasCoverage": bool(cov),
            "hasAllure": bool(tests),
            "requirements": len(specs["requirements"]),
            "requirementsCovered": sum(1 for r in specs["requirements"]
                                       if r["how"] != "none"),
            "artifactProblems": artifacts_status["problems"],
            "configError": s.cfg.error,
            "configExists": s.cfg.exists,
            "scopeChosen": artifacts_status["scopeChosen"],
            "partialRun": bool(src.partial_reasons(cov_meta)),
            "partialReasons": src.partial_reasons(cov_meta),
            "selection": dict(cov_meta.get("selection") or {}),
            # opt-in-наборы partest ≥ 2.5.0: что снято, что включено и чем. В шапку —
            # готовым текстом, чтобы подпись была одна на сцену, раннер и консоль
            "optin": optin,
            "optinText": src.optin_phrases(optin),
            "optinActed": src.optin_acted(optin),
        },
        # состояние каждого артефакта: что прочитано, что нет и почему
        "artifacts": artifacts_status,
        "specs": specs,
        "contract": contract,
        "run": run,
        "coverage": cov_extras,
        "db": db,
        "screens": s.screens,
        "screenAlias": s.screen_alias,
        "dbEffects": ax["dbEffects"],
        "containers": s.containers,
        "domains": s.domains,
        "collected": {
            "uiLinks": len(ax["uiFlow"]),
            "dbEffects": len(ax["dbEffects"]),
            "busEvents": len(ax.get("busFlow") or []),
            "busEdges": len(observed_bus),
            # Записи агента без единого ребра — по причинам (`LOSS_REASONS`). Если
            # отчётов Allure нет вовсе, это не потеря, а очищенные отчёты: так и сказано.
            "lostRecords": {k: loss.get(k, 0) for k in LOSS_REASONS},
            "lostLabels": dict(LOSS_REASONS),
            # записи от тестов, которых нет в текущих отчётах Allure, — сведение
            "notInReports": loss.get("unjoined", 0),
            "reportsCleared": not allure["tests"],
            "logEdges": len(observed_logs),
            "hasSchema": bool(db.get("databases")),
        },
        # z — глубина (растёт вглубь), yoff — ступенька вверх: вместе дают
        # «разнесённую аксонометрию», где стопка плоскостей читается
        "layers": [
            {"key": "frontend", "label": "Frontend", "z": 0, "yoff": 0,
             "sub": "страница SPA"},
            {"key": "api", "label": "API", "z": 720, "yoff": -70,
             "sub": next((c["label"] for c in s.containers
                          if c.get("default")), "HTTP API")},
            {"key": "bus", "label": "Шина и сервисы", "z": 1440, "yoff": -140,
             "sub": "шина сообщений"},
            {"key": "storage", "label": "Хранилища", "z": 2160, "yoff": -210,
             "sub": "база и объектное хранилище"},
        ],
        "nodes": [n.__dict__ for n in nodes.values()],
        "edges": [e.__dict__ for e in edges],
        "edgeSets": edge_sets,
        # компактно: трейсы и разбор ошибок уже лежат в run.failures
        "tests": [{k: t[k] for k in
                   ("name", "nodeid", "status", "ms", "feature", "story",
                    "parentSuite", "isUi", "endpoints", "file", "start")}
                  for t in tests],
    }


def build_root(root: Path, scope: Optional[Scope] = None) -> dict:
    """Прочитать корень и собрать граф — для командной строки и проверок."""
    return build(src.load(veha_config.load(root), scope))
