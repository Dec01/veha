"""Сборка графа над демонстрационным набором: эталонные цифры сцены.

Цифры — свойство набора `demo/generate.py`: поменялся генератор — меняются они, и это
видно здесь, а не на демонстрации. Три места определяет формат агента, а не вывод:

* `ui.flow` агента не хранит видимый текст элемента без `data-testid`, и 17 таких
  элементов сведены в 11 узлов «элемент без data-testid» — по одному на экран (рёбер
  столько же, 17);
* у контракта нет `source`: агент не пишет, откуда снят документ;
* у `containers.logs` нет числа строк лога (`logLines` на узлах контейнеров).
"""

from __future__ import annotations

from collections import Counter


def test_scene_numbers(demo_graph):
    m = demo_graph["meta"]
    assert (m["nodes"], m["edges"], m["endpoints"]) == (322, 740, 177)
    assert (m["tests"], m["uiTests"]) == (605, 46)
    assert (m["requirements"], m["requirementsCovered"]) == (30, 24)
    assert m["hasCoverage"] and m["hasAllure"]
    assert m["artifactProblems"] == 0 and m["configError"] == ""


def test_edge_sets(demo_graph):
    sizes = {k: len(v) for k, v in demo_graph["edgeSets"].items()}
    assert sizes == {"structure": 740, "tests": 361, "coverage": 947}
    kinds = Counter(e["kind"] for e in demo_graph["edges"])
    assert kinds == {
        "contains": 46, "calls": 133, "publish": 12, "consume": 8, "storage": 2,
        "writes": 262, "reads": 116, "depends": 23, "publishes": 70, "consumes": 26,
        "logged": 42,
    }


def test_endpoint_statuses(demo_graph):
    eps = [n for n in demo_graph["nodes"] if n["kind"] == "endpoint"]
    assert Counter(n["meta"].get("status") for n in eps) == {
        "full": 51, "partial": 111, "empty": 9, "unseen": 6}
    assert Counter(n["meta"].get("runStatus") for n in eps) == {
        "passed": 129, "failed": 35, None: 13}


def test_node_kinds(demo_graph):
    kinds = Counter(n["kind"] for n in demo_graph["nodes"])
    assert kinds == {
        "endpoint": 177, "screen": 11, "control": 57, "backend": 6, "auth": 1, "db": 1,
        "bus": 1, "storage": 1, "topic": 12, "store": 1, "table": 52, "external": 2,
    }


def test_controls_without_testid_are_not_named_by_guess(demo_graph):
    """Элемент без data-testid назван честно — «без data-testid», по экрану."""
    anon = [n for n in demo_graph["nodes"] if n["id"].startswith("ui:?")]
    assert len(anon) == 11
    assert all(n["meta"]["testid"] is None and n["meta"]["observed"] for n in anon)
    calls = [e for e in demo_graph["edges"]
             if e["kind"] == "calls" and e["source"].startswith("ui:?")]
    assert len(calls) == 17


def test_contract_and_collected(demo_graph):
    c = demo_graph["contract"]
    assert c["operations"] == 174
    assert (len(c["declaredUnused"]), len(c["undeclaredCodes"]),
            len(c["missingFromContract"])) == (13, 97, 3)
    assert "source" not in c
    collected = dict(demo_graph["collected"])
    collected.pop("lostLabels")
    assert collected == {
        "uiLinks": 75, "dbEffects": 133, "busEvents": 18, "busEdges": 96,
        "lostRecords": {"emptyJoin": 0, "filtered": 0, "malformed": 0}, "notInReports": 0,
        "reportsCleared": False, "logEdges": 42, "hasSchema": True}
    assert demo_graph["db"]["stats"] == {"tables": 52, "columns": 369, "foreignKeys": 60}


def test_run_and_specs(demo_graph):
    run = demo_graph["run"]
    assert {k: run[k] for k in ("total", "passed", "failed", "broken", "runsFound")} == {
        "total": 605, "passed": 566, "failed": 24, "broken": 15, "runsFound": 1}
    how = Counter(r["how"] for r in demo_graph["specs"]["requirements"])
    assert how == {"direct": 17, "anchor": 6, "table": 1, "none": 6}


def test_consumer_edges_are_labelled_as_inference(demo_graph):
    """`consumes` — вывод из тишины записей, и подписан как вывод, а не как факт."""
    consumes = [e for e in demo_graph["edges"] if e["kind"] == "consumes"]
    assert consumes and all("тишине" in e["label"] for e in consumes)


def test_ui_markers_come_from_config(tmp_path):
    """Признак UI-теста — из atlas.yaml; без него сервер не угадывает каталог проекта."""
    import json

    from veha.core import sources

    d = tmp_path / "allure-results"
    d.mkdir()
    (d / "1-result.json").write_text(json.dumps({
        "fullName": "src.ui.tests.orders.test_list.TestList#test_open",
        "name": "test_open", "status": "passed", "start": 1, "stop": 2}), encoding="utf-8")

    assert sources.load_allure(tmp_path, [])["tests"][0]["isUi"] is False
    assert sources.load_allure(tmp_path, ["src.ui"])["tests"][0]["isUi"] is True


def test_agent_screen_form_is_normalised():
    """Агент пишет `location.hash || location.pathname`: `#/orders/42`."""
    from veha.core.sources import norm_screen

    assert norm_screen("#/catalog/products/42") == "/#/catalog/products/{id}"
    assert norm_screen("/#/orders/{orderId}") == "/#/orders/{id}"
    assert norm_screen("#/orders/0b8e1f9a-3c2d-4e5f-8a9b-1c2d3e4f5a6b/lines") \
        == "/#/orders/{id}/lines"
    assert norm_screen("/admin/accounts/") == "/admin/accounts"


def test_screen_carries_the_controls_key(demo_graph):
    """У экрана путь в том же виде, что у его контролов: в карте `{orderId}`, у
    рекордера `{id}` — вебморда сводит их по `screenPath`, не по сырому `path`."""
    from veha.core.sources import norm_screen

    screens = [n for n in demo_graph["nodes"] if n["kind"] == "screen"]
    assert screens and all(n["meta"]["screenPath"] == norm_screen(n["meta"]["path"])
                           for n in screens)
    keys = {n["meta"]["screenPath"] for n in screens}
    controls = [n for n in demo_graph["nodes"] if n["kind"] == "control"]
    assert all(c["meta"].get("screenPath") in keys for c in controls if c["meta"].get("screenPath"))
    assert any(n["meta"]["path"] != n["meta"]["screenPath"] for n in screens)


def test_demo_ui_flow_is_in_the_agents_form(demo_root):
    import json

    from tests.conftest import artifact_path

    links = json.loads(artifact_path(demo_root, "ui.flow").read_text(encoding="utf-8"))["links"]
    assert all(l["screen"].startswith("#/") for l in links)
    assert any(any(ch.isdigit() for ch in l["screen"]) for l in links)
    assert not any("{" in l["screen"] for l in links)
