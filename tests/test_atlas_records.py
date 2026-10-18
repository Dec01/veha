"""Записи агента так, как их пишет partest-atlas 0.2: окно теста `db.effects` с
фикстурами, сессионные записи под pytest-xdist, прогоны, накопленные в одном файле.

Записи здесь собраны руками в форме сборщиков агента (`collect.db_effects.diff`,
`collect.bus_flow.diff_offsets`): формат 0.2 тот же, что 0.1, поменялось, когда и
какие записи пишутся."""

from __future__ import annotations

import json

from tests.conftest import artifact_path
from veha import runner
from veha.core.graph import WINDOW_LABEL, build_root

NODE = "tests/orders/test_orders.py::TestOrders::test_create"
FULL = "tests.orders.test_orders.TestOrders#test_create"
ORDERS = "/api/v1/orders"


def _allure(root):
    d = root / "allure-results"
    d.mkdir()
    steps = [{"name": f"HTTP GET {ORDERS} → expect status=200", "status": "passed"},
             {"name": f"HTTP POST {ORDERS} → expect status=201", "status": "passed"}]
    (d / "orders-1-result.json").write_text(json.dumps({
        "fullName": FULL, "name": "test_create", "status": "passed",
        "start": 1_000, "stop": 2_000, "steps": steps}), encoding="utf-8")


def _records(root, kind, records):
    path = artifact_path(root, kind)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["records"] = records
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _session_effect(table, insert=0, update=0, delete=0):
    return {"test": "", "nodeid": "", "table": table, "insert": insert, "update": update,
            "delete": delete, "shared": False, "scope": "session"}


def _session_bus(topic, events, after_write):
    """Сессионная запись `bus.flow`. Под xdist — `afterWrite: false` и нули в `http`."""
    http = {"requests": 40, "writes": 12} if after_write else {"requests": 0, "writes": 0}
    return {"test": "", "nodeid": "", "topic": topic, "events": events,
            "scope": "session", "afterWrite": after_write, "http": http}


def _node(g, nid):
    return next(n for n in g["nodes"] if n["id"] == nid)


def test_session_effects_of_several_runs_are_not_shown_as_one_run(small_root):
    """Агент копит прогоны в одном файле без метки прогона. Сумма двух сессионных
    записей — не «что сделал прогон»: сервер отдаёт её с числом слагаемых и под
    именем, в котором нет прогона."""
    _records(small_root, "db.effects", [_session_effect("audit_log", insert=3),
                                        _session_effect("audit_log", insert=5, delete=1)])
    meta = _node(build_root(small_root), "store:audit_log")["meta"]
    assert "runTouched" not in meta
    assert meta["sessionTouched"] == {"insert": 8, "update": 0, "delete": 1, "records": 2}


def test_session_bus_records_never_count_as_silence(small_root):
    """`afterWrite` сессионной записи — «часы HTTP видели запись» (без xdist) или
    всегда `false` (под xdist). Ни то, ни другое не «событие в тишине»: счёт тишины у
    топика не появляется, рёбер `consumes` нет, хотя тест таблицу писал."""
    _allure(small_root)
    _records(small_root, "db.effects", [
        {"test": "test_create", "nodeid": NODE, "table": "audit_log", "insert": 1,
         "update": 0, "delete": 0, "shared": False, "scope": "test"}])
    _records(small_root, "bus.flow", [_session_bus("audit.recorded", 4, True),
                                      _session_bus("audit.recorded", 2, False)])
    g = build_root(small_root)
    meta = _node(g, "topic:audit.recorded")["meta"]
    assert "silentTests" not in meta and "runSilent" not in meta
    assert (meta["fileEvents"], meta["fileRecords"]) == (6, 2)
    assert not [e for e in g["edges"] if e["kind"] == "consumes"]
    # сессионная запись — не потеря: теста за ней нет по построению
    assert g["collected"]["lostRecords"] == {"emptyJoin": 0, "filtered": 0, "malformed": 0}


def test_per_test_silence_counts_tests_not_records(small_root):
    """Позадачная запись с `afterWrite` по-прежнему считается — тишину сервер
    убирает только у сессионного среза. Считаются тесты: два прогона одного теста —
    две записи в файле, но один тест."""
    _allure(small_root)
    per_test = {"test": "test_create", "nodeid": NODE, "topic": "audit.recorded",
                "scope": "test", "afterWrite": True}
    _records(small_root, "bus.flow", [
        {**per_test, "events": 1, "http": {"requests": 2, "writes": 1}},
        {**per_test, "events": 2, "http": {"requests": 3, "writes": 1}},
        _session_bus("audit.recorded", 3, False)])
    meta = _node(build_root(small_root), "topic:audit.recorded")["meta"]
    assert meta["silentTests"] == 1
    assert (meta["fileEvents"], meta["fileRecords"]) == (6, 3)


def test_publish_and_read_edges_from_a_test_window_name_the_window(small_root):
    """`publishes` и `reads` строятся из тех же позадачных окон, что и `writes`."""
    _allure(small_root)
    _records(small_root, "db.effects", [
        {"test": "test_create", "nodeid": NODE, "table": "audit_log", "insert": 0,
         "update": 0, "delete": 0, "shared": False, "scope": "test"}])
    _records(small_root, "bus.flow", [
        {"test": "test_create", "nodeid": NODE, "topic": "audit.recorded", "events": 1,
         "scope": "test", "afterWrite": False, "http": {"requests": 2, "writes": 1}}])
    g = build_root(small_root)
    labels = {e["kind"]: e["label"] for e in g["edges"]
              if e["kind"] in ("publishes", "reads")
              and (e["target"] in ("topic:audit.recorded", "store:audit_log"))}
    assert labels == {"publishes": WINDOW_LABEL.format("событие"),
                      "reads": WINDOW_LABEL.format("select")}


def test_malformed_records_are_dropped_and_counted_not_fatal(small_root):
    """Контракт проверяет шапку, а не поля записей. Счётчик строкой или nodeid
    числом раньше ронял `/api/graph`; теперь такая запись — потеря `malformed`."""
    _allure(small_root)
    _records(small_root, "db.effects", [
        _session_effect("audit_log", insert=2),
        {**_session_effect("audit_log"), "insert": "5"},
        {**_session_effect("audit_log", insert=1), "nodeid": 123, "scope": "test"},
        "не запись"])
    _records(small_root, "bus.flow", [{**_session_bus("audit.recorded", 1, False),
                                       "events": "x"}])
    g = build_root(small_root)
    assert g["collected"]["lostRecords"]["malformed"] == 4
    assert _node(g, "store:audit_log")["meta"]["sessionTouched"] == {
        "insert": 2, "update": 0, "delete": 0, "records": 1}
    assert "malformed" in g["collected"]["lostLabels"]


def test_xdist_run_without_per_test_records_draws_no_write_edges(small_root):
    """Под xdist агент пишет только сессионную запись. Отсутствие позадачных — не
    «тест не писал»: рёбер от эндпоинтов нет, таблица помечена сессионным замером."""
    _allure(small_root)
    _records(small_root, "db.effects", [_session_effect("audit_log", insert=7)])
    g = build_root(small_root)
    assert not [e for e in g["edges"]
                if e["kind"] == "writes" and e["target"] == "store:audit_log"]
    assert _node(g, "store:audit_log")["meta"]["sessionTouched"]["insert"] == 7


def test_write_edge_from_a_test_window_names_the_window(small_root):
    """Окно позадачного замера включает фикстуры, в том числе фикстуры модуля и
    сессии на первом и последнем тесте. Эндпоинты теста — из тела теста. Ребро
    подписано окном, а не выдано за измеренную запись эндпоинта."""
    _allure(small_root)
    _records(small_root, "db.effects", [
        {"test": "test_create", "nodeid": NODE, "table": "audit_log", "insert": 2,
         "update": 0, "delete": 0, "shared": False, "scope": "test"}])
    g = build_root(small_root)
    labels = {e["label"] for e in g["edges"]
              if e["kind"] == "writes" and e["target"] == "store:audit_log"}
    assert labels == {WINDOW_LABEL.format("insert")}
    assert "фикстур" in WINDOW_LABEL and WINDOW_LABEL.startswith("снято")


def test_trend_skips_a_report_that_counted_nothing():
    """Отчёт, который ничего не посчитал (`endpoints: 0`), — не «0 %». До partest
    2.6.0 такой отчёт выходил молча; с ним в тренде был бы рост с нуля, которого не
    было."""
    runs = [
        {"startedAt": 1, "suite": "full", "coverage": {"avg": 0.0, "endpoints": 0}},
        {"startedAt": 2, "suite": "full", "coverage": {"avg": 0.0, "endpoints": 0}},
        {"startedAt": 3, "suite": "full", "coverage": {"avg": 62.0, "endpoints": 140}},
        # срез без ключа `endpoints` (старая история) — как раньше, по `avg`
        {"startedAt": 4, "suite": "full", "coverage": {"avg": 63.0}},
    ]
    assert [t["avg"] for t in runner.trend(runs)] == [62.0, 63.0]


def test_requirement_by_table_and_uncounted_history_are_named_in_the_page():
    """Подписи живут в статике: привязка требования «по таблице» строится из тех же
    позадачных окон, а срез без замера в истории — «не посчитано», а не «0%»."""
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "veha" / "viz" / "static"
    atlas_js = static.joinpath("atlas.js").read_text(encoding="utf-8")
    dock_js = static.joinpath("dock.js").read_text(encoding="utf-8")
    assert "table: ['выведено по таблице, по окну теста с фикстурами'" in atlas_js
    assert "table: ['по таблице, по окну теста с фикстурами'" in dock_js
    assert "cov.endpoints === 0" in dock_js and "не посчитано" in dock_js


def test_uncounted_last_run_does_not_become_the_current_series():
    """Последний прогон ничего не посчитал и шёл с другим состоянием opt-in. Точки он
    не даёт — и текущей остаётся серия последнего прогона с замером, как у частичного."""
    off = {"slow": {"state": "off", "env": "RUN_SLOW", "deselected": 14}}
    env = {"slow": {"state": "env", "env": "RUN_SLOW", "deselected": 0}}
    runs = [
        {"startedAt": 1, "suite": "full", "coverage": {"avg": 60.0, "endpoints": 9, "optin": off}},
        {"startedAt": 2, "suite": "full", "coverage": {"avg": 0.0, "endpoints": 0, "optin": env}},
    ]
    series = runner.trend_series(runs)
    assert len(series) == 1 and series[0]["current"]
    assert [p["avg"] for p in series[0]["points"]] == [60.0]
