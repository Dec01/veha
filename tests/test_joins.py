"""Соединение записей агента с тестами Allure и экранов с манифестом: параметризованные
варианты, пути экранов, nodeid со скобками в каталоге."""

from __future__ import annotations

import json

import pytest

from tests.conftest import artifact_path
from veha.core.graph import build_root
from veha.core.sources import base_nodeid, norm_screen, variant_of

NODE = "tests/rbac/test_rbac.py::TestRbac::test_write"
FULL = "tests.rbac.test_rbac.TestRbac#test_write"
ME, ORDERS, INVOICES = "/api/v1/accounts/me", "/api/v1/orders", "/api/v1/invoices"


# ------------------------------------------------------------------


@pytest.mark.parametrize("raw,norm", [
    ("#/orders?tab=2", "/#/orders"),
    ("#/orders/42?x=1&y=2", "/#/orders/{id}"),
    ("/#/catalog/products/{productId}", "/#/catalog/products/{id}"),
    ("/", "/"),
    ("#/", "/#"),
    ("", ""),
    (None, ""),
])
def test_screen_normalisation(raw, norm):
    assert norm_screen(raw) == norm


def test_root_screen_does_not_merge_with_no_screen():
    assert norm_screen("/") != norm_screen("")


# ------------------------------------------------------------------


@pytest.mark.parametrize("nodeid,base", [
    ("tests/[legacy]/x.py::test[a]", "tests/[legacy]/x.py::test"),
    ("tests/[legacy]/x.py::test", "tests/[legacy]/x.py::test"),
    ("t.py::T::test[a-[b]]", "t.py::T::test"),
    ("not a nodeid [x]", "not a nodeid [x]"),
])
def test_params_are_cut_from_the_last_segment_only(nodeid, base):
    assert base_nodeid(nodeid) == base


def test_variant_from_allure_name():
    assert variant_of(NODE, "test_write[orders]") == NODE + "[orders]"
    assert variant_of(NODE, "test_write") == NODE
    assert variant_of(NODE + "[x]", "test_write[x]") == NODE + "[x]"


def _result(d, idx, name, write_path):
    steps = [{"name": f"HTTP GET {ME} → expect status=200", "status": "passed"},
             {"name": f"HTTP POST {write_path} → expect status=201", "status": "passed"}]
    (d / f"rbac-{idx}-result.json").write_text(json.dumps({
        "fullName": FULL, "name": name, "status": "passed",
        "start": 1_000 + idx, "stop": 2_000 + idx, "steps": steps}), encoding="utf-8")


def _rbac_root(small_root, names):
    d = small_root / "allure-results"
    d.mkdir()
    _result(d, 1, names[0], ORDERS)
    _result(d, 2, names[1], INVOICES)
    path = artifact_path(small_root, "db.effects")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["records"] = [{"test": "test_write[invoices]", "nodeid": NODE + "[invoices]",
                       "table": "audit_log", "insert": 1, "update": 0, "delete": 0,
                       "shared": False, "scope": "test"}]
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return small_root


def _writers(g, table):
    return sorted(e["source"] for e in g["edges"]
                  if e["kind"] == "writes" and e["target"] == f"store:{table}"
                  and e["label"].startswith("снято"))


def test_distinguishable_variant_writes_only_from_its_own_endpoints(small_root):
    """Allure различает варианты по `name` — запись идёт ровно к эндпоинту варианта."""
    g = build_root(_rbac_root(small_root, ["test_write[orders]", "test_write[invoices]"]))
    assert _writers(g, "audit_log") == [f"api:POST {INVOICES}"]


def test_indistinguishable_variants_do_not_spread_a_write(small_root):
    """Allure вариантов не различает: эндпоинты, которых этот вариант мог не вызывать,
    ребро не получают. Общий у вариантов только GET — он не пишет."""
    g = build_root(_rbac_root(small_root, ["test_write", "test_write"]))
    assert _writers(g, "audit_log") == []


def test_bus_attribution_is_per_variant(small_root):
    """Вариант [b] пишет таблицу без события в тишине — таблица объяснима без шины, и
    потребителю её не приписываем, даже если у варианта [a] событие было."""
    root = _rbac_root(small_root, ["test_write[a]", "test_write[b]"])
    eff = artifact_path(root, "db.effects")
    doc = json.loads(eff.read_text(encoding="utf-8"))
    doc["records"] = [
        {"nodeid": NODE + "[a]", "table": "audit_log", "insert": 1, "scope": "test"},
        {"nodeid": NODE + "[b]", "table": "audit_log", "insert": 1, "scope": "test"}]
    eff.write_text(json.dumps(doc), encoding="utf-8")
    bus = artifact_path(root, "bus.flow")
    doc = json.loads(bus.read_text(encoding="utf-8"))
    doc["records"] = [{"nodeid": NODE + "[a]", "topic": "audit.recorded", "events": 1,
                       "scope": "test", "afterWrite": True}]
    bus.write_text(json.dumps(doc), encoding="utf-8")
    g = build_root(root)
    assert not [e for e in g["edges"]
                if e["kind"] == "consumes" and e["target"] == "store:audit_log"]


# ------------------------------------------------------------------


def _write_result(d, idx, name, write_path=None, status="passed", parameters=None):
    steps = [] if write_path is None else [
        {"name": f"HTTP GET {ME} → expect status=200", "status": "passed"},
        {"name": f"HTTP POST {write_path} → expect status=201", "status": "passed"}]
    (d / f"e2-{idx}-result.json").write_text(json.dumps({
        "fullName": FULL, "name": name, "status": status, "parameters": parameters or [],
        "start": 1_000 + idx, "stop": 2_000 + idx, "steps": steps}), encoding="utf-8")


def _effects(root, nodeids):
    path = artifact_path(root, "db.effects")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["records"] = [{"nodeid": n, "table": "audit_log", "insert": 1, "scope": "test"}
                      for n in nodeids]
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_results_without_calls_do_not_empty_the_intersection(small_root):
    """Пропущенный и сломанный до запроса варианты не обнуляют общие вызовы."""
    d = small_root / "allure-results"
    d.mkdir()
    _write_result(d, 1, "test_write", INVOICES)
    _write_result(d, 2, "test_write", INVOICES)
    _write_result(d, 3, "test_write", None, status="skipped")
    _write_result(d, 4, "test_write", None, status="broken")
    _effects(small_root, [NODE + "[invoices]"])
    g = build_root(small_root)
    assert _writers(g, "audit_log") == [f"api:POST {INVOICES}"]
    assert g["collected"]["lostRecords"]["emptyJoin"] == 0


def test_lost_records_are_counted(small_root):
    d = small_root / "allure-results"
    d.mkdir()
    _write_result(d, 1, "test_write", ORDERS)
    _write_result(d, 2, "test_write", INVOICES)
    _effects(small_root, [NODE + "[orders]", "tests/nowhere.py::test_gone"])
    bus = artifact_path(small_root, "bus.flow")
    doc = json.loads(bus.read_text(encoding="utf-8"))
    doc["records"] = []                      # записи шины демо про тесты, которых тут нет
    bus.write_text(json.dumps(doc), encoding="utf-8")
    c = build_root(small_root)["collected"]
    # общих пишущих вызовов у вариантов нет — пересечение только GET, записи не пусто;
    # тест, которого нет в Allure, — потерян
    # тест, которого нет в Allure, — не потеря, а сведение (db.effects накопительный)
    assert c["notInReports"] == 1 and "unjoined" not in c["lostRecords"]


def test_empty_intersection_is_counted(small_root):
    d = small_root / "allure-results"
    d.mkdir()
    for idx, path in ((1, ORDERS), (2, INVOICES)):
        (d / f"e2-{idx}-result.json").write_text(json.dumps({
            "fullName": FULL, "name": "test_write", "status": "passed",
            "start": 1_000 + idx, "stop": 2_000 + idx,
            "steps": [{"name": f"HTTP POST {path} → expect status=201", "status": "passed"}]}),
            encoding="utf-8")
    _effects(small_root, [NODE + "[orders]"])
    assert build_root(small_root)["collected"]["lostRecords"]["emptyJoin"] == 1


def test_allure_title_still_joins_by_parameters(small_root):
    """`@allure.title` убирает `[...]` из `name`; вариант опознаётся по параметрам Allure."""
    d = small_root / "allure-results"
    d.mkdir()
    _write_result(d, 1, "Запись заказа", ORDERS,
                  parameters=[{"name": "entity", "value": "'orders'"}])
    _write_result(d, 2, "Запись счёта", INVOICES,
                  parameters=[{"name": "entity", "value": "'invoices'"}])
    _effects(small_root, [NODE + "[invoices]"])
    assert _writers(build_root(small_root), "audit_log") == [f"api:POST {INVOICES}"]


def test_unrecognisable_parameters_go_to_the_ambiguous_branch(small_root):
    """Явные `ids=` дают id, которого по параметрам не угадать, — честная неоднозначность."""
    d = small_root / "allure-results"
    d.mkdir()
    _write_result(d, 1, "Запись заказа", ORDERS, parameters=[{"name": "e", "value": "1"}])
    _write_result(d, 2, "Запись счёта", INVOICES, parameters=[{"name": "e", "value": "2"}])
    _effects(small_root, [NODE + "[invoices]"])
    assert _writers(build_root(small_root), "audit_log") == []


# ------------------------------------------------------------------


def test_requirement_shows_worst_variant_and_last_seen(small_root):
    from veha.core.graph import _test_entry

    v = [{"nodeid": NODE, "variant": NODE + "[a]", "status": "passed", "start": 10},
         {"nodeid": NODE, "variant": NODE + "[b]", "status": "failed", "start": 20,
          "name": "test_write[b]", "file": "b-result.json"}]
    # оба варианта в последнем прогоне — худший из них
    e = _test_entry(NODE, "anchor", v, {NODE + "[a]", NODE + "[b]"})
    assert (e["status"], e["file"], e["variants"]) == ("failed", "b-result.json", 2)
    assert e["lastSeen"] == 20 and e["notInLastRun"] is False
    assert _test_entry(NODE, "anchor", v, set())["notInLastRun"] is True


def test_requirement_tests_are_listed_by_base_nodeid(full_copy_for_specs):
    g = build_root(full_copy_for_specs)
    for req in g["specs"]["requirements"]:
        ids = [t["nodeid"] for t in req["tests"]]
        assert all(base_nodeid(n) == n for n in ids)
        assert all("lastSeen" in t and "notInLastRun" in t for t in req["tests"])


@pytest.fixture
def full_copy_for_specs(tmp_path, demo_root):
    import shutil

    root = tmp_path / "full"
    shutil.copytree(demo_root, root, ignore=shutil.ignore_patterns("screens", "logs"))
    return root


# ------------------------------------------------------------------


def _clear_bus(root):
    bus = artifact_path(root, "bus.flow")
    doc = json.loads(bus.read_text(encoding="utf-8"))
    doc["records"] = []
    bus.write_text(json.dumps(doc), encoding="utf-8")


def test_method_filter_loss_is_counted_separately(small_root):
    """Тест только читал (GET), а агент записал вставку — ребра «пишет» не будет, и это
    не «не соединилась», а «отсечено фильтром метода»."""
    d = small_root / "allure-results"
    d.mkdir()
    (d / "f3-result.json").write_text(json.dumps({
        "fullName": FULL, "name": "test_write", "status": "passed", "start": 1, "stop": 2,
        "steps": [{"name": f"HTTP GET {ME} → expect status=200", "status": "passed"}]}),
        encoding="utf-8")
    _effects(small_root, [NODE])
    _clear_bus(small_root)
    lost = build_root(small_root)["collected"]["lostRecords"]
    assert lost == {"emptyJoin": 0, "filtered": 1, "malformed": 0}


def test_cleared_reports_are_not_called_a_loss(small_root, capsys, monkeypatch):
    """allure-results нет — записи агента не с чем соединить: это «отчёты очищены»."""
    import uvicorn

    from veha import cli

    g = build_root(small_root)
    assert g["collected"]["reportsCleared"] is True
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **kw: None)
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli.main(["serve", "--root", str(small_root), "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "отчёты Allure очищены" in out and "без единой связи" not in out


def test_losses_are_printed_at_startup(small_root, capsys, monkeypatch):
    import uvicorn

    from veha import cli

    d = small_root / "allure-results"
    d.mkdir()
    _write_result(d, 1, "test_write", ORDERS)
    _effects(small_root, [NODE, "tests/nowhere.py::test_gone"])
    _clear_bus(small_root)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **kw: None)
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli.main(["serve", "--root", str(small_root), "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "записей агента без единой связи" not in out
    assert "записей агента от тестов, которых нет в текущих отчётах: 1" in out


def test_skipped_variant_with_calls_stays_out_of_the_intersection(small_root):
    """qa: пропущенный вариант с непустыми вызовами в пересечение не входит. Без условия
    на `skipped` его вызовы сузили бы пересечение до общего GET, и запись счёта
    потеряла бы ребро."""
    d = small_root / "allure-results"
    d.mkdir()
    _write_result(d, 1, "test_write", INVOICES)
    _write_result(d, 2, "test_write", ORDERS, status="skipped")
    _effects(small_root, [NODE + "[invoices]"])
    assert _writers(build_root(small_root), "audit_log") == [f"api:POST {INVOICES}"]


# ------------------------------------------------------------------


def test_old_removed_variant_does_not_colour_the_requirement():
    """Вариант `[old]` упал неделю назад и из набора убран; в последнем прогоне есть
    только `[new]`, и он прошёл — требование зелёное."""
    from veha.core.graph import _test_entry

    week_ago, today = 1_000, 1_000 + 7 * 86_400_000
    v = [{"nodeid": NODE, "variant": NODE + "[old]", "status": "failed", "start": week_ago,
          "file": "old-result.json"},
         {"nodeid": NODE, "variant": NODE + "[new]", "status": "passed", "start": today,
          "file": "new-result.json"}]
    # последний прогон — полный: `[old]` в нём нет, значит, его больше не существует
    e = _test_entry(NODE, "anchor", v, {NODE + "[new]"}, last_run_full=True)
    assert (e["status"], e["file"], e["notInLastRun"]) == ("passed", "new-result.json", False)
    assert e["lastSeen"] == today and e["variants"] == 2


def test_rerun_of_one_variant_does_not_hide_a_failed_sibling():
    """`[b]` упал в полном прогоне, потом перегнали только `[a]` — требование
    остаётся красным. Перезапуск одного кейса — не полный прогон."""
    from veha.core.graph import _test_entry

    v = [{"nodeid": NODE, "variant": NODE + "[a]", "status": "passed", "start": 2_000,
          "file": "a-rerun.json"},
         {"nodeid": NODE, "variant": NODE + "[b]", "status": "failed", "start": 1_000,
          "file": "b-full.json"}]
    e = _test_entry(NODE, "anchor", v, {NODE + "[a]"}, last_run_full=False)
    assert (e["status"], e["file"]) == ("failed", "b-full.json")


def _history(full=True, complete=True, status="done", started=100.0, finished=200.0):
    return [{"suiteFull": full, "complete": complete, "status": status,
             "startedAt": started, "finishedAt": finished}]


@pytest.mark.parametrize("history,first_ms,full", [
    (_history(), 120_000, True),                         # тот же прогон
    (_history(full=False), 120_000, False),              # набор не полный
    (_history(complete=False), 120_000, False),          # оборван
    (_history(status="stopped"), 120_000, False),
    (_history(), 3_600_000, False),                      # последний в Allure — позже
    ([], 120_000, False),                                # истории нет (прогон из CI)
])
def test_last_run_is_full(history, first_ms, full):
    from veha.core.graph import last_run_is_full

    assert last_run_is_full(history, [{"start": first_ms}]) is full


def test_without_any_current_variant_worst_of_all_is_shown():
    from veha.core.graph import _test_entry

    v = [{"nodeid": NODE, "variant": NODE + "[a]", "status": "failed", "start": 1},
         {"nodeid": NODE, "variant": NODE + "[b]", "status": "passed", "start": 2}]
    e = _test_entry(NODE, "anchor", v, set())
    assert e["status"] == "failed" and e["notInLastRun"] is True


# ------------------------------------------------------------------


def test_records_of_old_tests_are_information_not_loss(small_root, capsys, monkeypatch):
    """Накопительный db.effects хранит записи тестов прошлых прогонов. На живом проекте
    их всегда больше нуля — показывать это как потерю значит приучить не читать шапку."""
    import uvicorn

    from veha import cli

    d = small_root / "allure-results"
    d.mkdir()
    _write_result(d, 1, "test_write", INVOICES)
    _effects(small_root, [NODE, "tests/old.py::test_a", "tests/old.py::test_b"])
    _clear_bus(small_root)
    c = build_root(small_root)["collected"]
    assert c["notInReports"] == 2 and sum(c["lostRecords"].values()) == 0
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **kw: None)
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli.main(["serve", "--root", str(small_root), "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "  ! записей агента" not in out
    assert "  записей агента от тестов, которых нет в текущих отчётах: 2" in out

