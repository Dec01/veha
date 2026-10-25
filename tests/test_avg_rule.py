"""Правило среднего (`meta.avgRule`, partest 2.7.0) и запись истории, которая не легла.

partest 2.7.0 вынес строки `exception` из среднего (правило 2); отчёт без ключа посчитан
по правилу 1. Контракт `coverage-payload`: тренд и сравнение средних — только в пределах
одного правила, а потребитель, хранящий срезы, хранит и правило.

Образцы `tests/data/coverage-2.6.0.json` и `coverage-2.7.0.json` записаны функциями
сводки самого partest (`partest.reports.payload`: `summarize`, `aggregate_*`) — 2.6.0 из
окружения и 2.7.0 из кандидата — над одними и теми же шестью строками: три `exception`
(одна у `orders`, две — весь сервис `probes`). Среднее 75.0 по правилу 1 и 50.0 по
правилу 2; у `probes` по правилу 2 среднего нет.

Проверки проходят и с `partest-atlas` 0.2.0 (срез без `exception`/`scored`/`depth`), и
с 0.2.1: то, что зависит от версии агента, проверяется по его `COVERAGE_KEYS`.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from partest_atlas.collect import history

from tests.conftest import REPO, artifact_path, make_client, rewrite

DATA = Path(__file__).resolve().parent / "data"
#: Агент 0.2.1 и новее переносит в срез `exception`, `scored` и `depth`.
ATLAS_021 = "scored" in history.COVERAGE_KEYS


def _doc(version: str) -> dict:
    return json.loads((DATA / f"coverage-{version}.json").read_text(encoding="utf-8"))


def _root_with(small_root: Path, doc: dict) -> Path:
    (small_root / "coverage.json").write_text(json.dumps(doc, ensure_ascii=False),
                                              encoding="utf-8")
    return small_root


# ------------------------------------------------------------------ срез


def test_fixtures_are_what_partest_wrote():
    """Образцы — то, что они заявляют: правило, средние, сервис из одних exception."""
    old, new = _doc("2.6.0"), _doc("2.7.0")
    assert "avgRule" not in old["meta"] and new["meta"]["avgRule"] == 2
    assert old["summary"]["avg"] == 75.0 and new["summary"]["avg"] == 50.0
    assert new["summary"]["scored"] == 3 and new["summary"]["exception"] == 3
    probes = {s["key"]: s for s in new["services"]}["probes"]
    assert probes["avgCoverage"] is None and probes["scored"] == 0


@pytest.mark.parametrize("version,rule,avg", [("2.6.0", 1, 75.0), ("2.7.0", 2, 50.0)])
def test_snapshot_records_the_rule(small_root, version, rule, avg):
    from veha import runner

    snap = runner.coverage_snapshot(_root_with(small_root, _doc(version)), suite_full=True)
    assert snap["avgRule"] == rule and snap["avg"] == avg
    if ATLAS_021:
        assert snap["exception"] == 3
        if version == "2.7.0":
            assert snap["scored"] == 3 and snap["depth"]["statusPct"] == 80.0


def test_rule_is_recorded_even_when_the_agent_does_not(monkeypatch):
    """Агент до 0.2.1 правила в срез не пишет — сервер берёт его из `meta` отчёта."""
    from veha import runner

    def old_agent(document):            # `coverage_snapshot` partest-atlas 0.2.0
        summary = (document or {}).get("summary") or {}
        return {k: summary.get(k) for k in ("avg", "full", "partial", "empty", "endpoints")
                if k in summary}

    monkeypatch.setattr(runner.history, "coverage_snapshot", old_agent)
    assert runner.snapshot_of(_doc("2.7.0"), suite_full=True)["avgRule"] == 2
    assert runner.snapshot_of(_doc("2.6.0"), suite_full=True)["avgRule"] == 1
    no_meta = {k: v for k, v in _doc("2.6.0").items() if k != "meta"}
    assert runner.snapshot_of(no_meta, suite_full=True)["avgRule"] == 1


@pytest.mark.parametrize("meta,rule", [
    ({"avgRule": 2}, 2), ({"avgRule": 7}, 7), ({}, 1),
    ({"avgRule": "2"}, None), ({"avgRule": True}, None), ({"avgRule": 2.0}, None),
    ({"avgRule": None}, None), ([], None), ("x", None),
])
def test_rule_as_written_or_unknown(meta, rule):
    """Как у агента 0.2.1: целое — как записано, не целое и `meta` не объект —
    неизвестно, нет ключа — 1."""
    from veha import runner

    assert runner.avg_rule_of({"meta": meta, "summary": {"avg": 1.0}}) == rule
    assert runner.snapshot_of({"meta": meta, "summary": {"avg": 1.0}})["avgRule"] == rule


def test_partial_run_drops_average_and_depth_shares(small_root):
    """Любой тренд проверяет частичность: у частичного прогона нет ни среднего, ни
    долей глубины — они посчитаны по части набора. Счётчики остаются."""
    from veha import runner

    snap = runner.coverage_snapshot(_root_with(small_root, _doc("2.7.0")), suite_full=False)
    assert "avg" not in snap and snap["partialRun"] is True and snap["avgRule"] == 2
    depth = snap.get("depth") or {}
    assert not [k for k in depth if k.endswith("Pct")]
    if ATLAS_021:
        assert depth["cells"] == 5 and depth["status"] == 4


@pytest.mark.parametrize("meta", [
    [], "x", 3,
    {"partialRun": True, "selection": "x", "workers": "many"},
    {"partialRun": True, "selection": ["markexpr", "slow"], "workers": None},
])
def test_snapshot_survives_meta_that_is_not_an_object(small_root, meta):
    """`meta` и `meta.selection` из чужого файла, которые не объекты, — «нет», а не
    исключение в потоке раннера."""
    from veha import runner

    doc = {**_doc("2.7.0"), "meta": meta}
    snap = runner.coverage_snapshot(_root_with(small_root, doc), suite_full=True)
    assert snap.get("partialRun") or snap["avg"] == 50.0
    if isinstance(meta, dict):
        assert snap["partialReasons"] == ["много непосещённых"] and "selection" not in snap


def test_snapshot_of_a_document_that_is_not_an_object(small_root):
    from veha import runner

    assert runner.coverage_snapshot(_root_with(small_root, [1, 2])) == {}
    assert runner.snapshot_of(None) == {} and runner.snapshot_of("x") == {}


def test_graph_survives_meta_that_is_not_an_object(small_root):
    from veha.core.graph import build_root

    doc = {**_doc("2.7.0"), "meta": {"partialRun": True, "selection": "x"}}
    meta = build_root(_root_with(small_root, doc))["meta"]
    assert meta["selection"] == {} and meta["partialRun"] is True


# ------------------------------------------------------------------ тренд


def _run(t, avg, rule="absent", optin=None, **cov):
    c = {"avg": avg, "endpoints": 6, **cov}
    if rule != "absent":
        c["avgRule"] = rule
    if optin is not None:
        c["optin"] = optin
    return {"startedAt": t, "suite": "full", "coverage": c}


SLOW_OFF = {"slow": {"state": "off", "env": "RUN_SLOW", "deselected": 3}}
SLOW_ENV = {"slow": {"state": "env", "env": "RUN_SLOW", "deselected": 0}}


def test_trend_never_joins_rules_1_and_2():
    """Правила 1 и 2 — разные серии; без записи правила (и с правилом не целым) —
    своя серия «правило не записано», отношение к текущей — unknown."""
    from veha import runner

    runs = [_run(1, 70.0), _run(2, 60.0, 1), _run(3, 61.0, 1), _run(4, 50.0, 2),
            _run(5, 49.0, None), _run(6, 48.0, "2"), _run(7, 99.0, 2, partialRun=True),
            _run(8, 51.0, 2)]
    series = runner.trend_series(runs)
    by_points = [[p["avg"] for p in s["points"]] for s in series]
    assert by_points == [[50.0, 51.0], [70.0, 49.0, 48.0], [60.0, 61.0]]
    assert [s["avgRule"] for s in series] == [2, None, 1]
    assert [s["relation"] for s in series] == ["same", "unknown", "differs"]
    assert [s["relationBy"]["avgRule"] for s in series] == ["same", "unknown", "differs"]
    assert [s["ruleLabel"] for s in series] == [
        "правило среднего 2", "правило среднего не записано", "правило среднего 1"]
    assert series[0]["current"] and not any(s["current"] for s in series[1:])
    # ни одна серия не смешивает правила
    flat = runner.trend(runs)
    for s in series:
        assert len({p["avgRule"] for p in flat if p["series"] == s["series"]}) == 1


def test_rule_and_optin_together():
    from veha import runner

    runs = [_run(1, 60.0, 1, SLOW_OFF),          # правило другое — несравнимо
            _run(2, 61.0, "absent", SLOW_OFF),   # правило не записано — не подтверждено
            _run(3, 62.0, "absent", SLOW_ENV),   # opt-in другой — несравнимо
            _run(4, 63.0, 2, None),              # opt-in не записан — не подтверждено
            _run(5, 50.0, 2, SLOW_OFF)]
    series = {s["points"][0]["avg"]: s for s in runner.trend_series(runs)}
    assert series[50.0]["current"]
    assert series[60.0]["relation"] == "differs"
    assert series[60.0]["relationBy"] == {"avgRule": "differs", "optin": "same"}
    assert series[61.0]["relation"] == "unknown"
    assert series[62.0]["relation"] == "differs"
    assert series[62.0]["relationBy"] == {"avgRule": "unknown", "optin": "differs"}
    assert series[63.0]["relationBy"] == {"avgRule": "same", "optin": "unknown"}


def test_one_rule_names_no_rule():
    """Правило в подписи — только когда серии по нему расходятся."""
    from veha import runner

    series = runner.trend_series([_run(1, 60.0, 2, SLOW_ENV), _run(2, 61.0, 2, SLOW_OFF)])
    assert [s["ruleLabel"] for s in series] == ["", ""]
    assert [s["label"] for s in series] == ["slow — снят", "slow — включён через RUN_SLOW"]


def test_trend_over_snapshots_of_both_epochs():
    """Срезы с отчётов 2.6.0 и 2.7.0 — разные серии: 75 → 50 — не падение покрытия."""
    from veha import runner

    runs = [{"startedAt": i, "suite": "full",
             "coverage": runner.snapshot_of(_doc(v), suite_full=True)}
            for i, v in enumerate(("2.6.0", "2.7.0"))]
    series = runner.trend_series(runs)
    assert [[p["avg"] for p in s["points"]] for s in series] == [[50.0], [75.0]]
    assert series[1]["relation"] == "differs"


def test_demo_history_records_rule_2(client):
    r = client.get("/api/runner/suites").json()
    full = [h for h in r["history"] if h.get("suiteFull")]
    assert {h["coverage"]["avgRule"] for h in full} == {2}
    assert all(s["ruleLabel"] == "" for s in r["trendSeries"])


# ------------------------------------------------------------------ запись истории


class FakeProc:
    """Подпроцесс pytest, который сразу закончился."""

    def __init__(self, args, **kwargs):
        self.stdout = io.StringIO(
            "collecting ... collected 1 item\n"
            "t.py::test_x PASSED                                       [100%]\n"
            "============================== 1 passed in 0.01s ==============================\n")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_popen(monkeypatch):
    from veha import runner

    monkeypatch.setattr(runner.subprocess, "Popen", FakeProc)


def _run_and_wait(c, timeout=5.0) -> dict:
    assert c.post("/api/runner/start", json={"suite": "smoke"}).status_code == 200
    end = time.time() + timeout
    while time.time() < end:
        r = c.get("/api/runner/suites").json()
        if r["historyWrite"] is not None:
            return r
        time.sleep(0.05)
    raise AssertionError("прогон не записан и не пропущен за отведённое время")


def test_history_write_is_reported(small_root, fake_popen):
    c = make_client(small_root, readonly=False)
    assert c.get("/api/runner/suites").json()["historyWrite"] is None
    r = _run_and_wait(c)
    assert r["historyWrite"]["state"] == "written" and r["historyState"] == "ok"


def test_history_not_understood_is_reported_not_silent(small_root, fake_popen):
    """Файл, которого сервер не понял, не тронут, и окно раннера говорит, что прогон не
    записан."""
    c = make_client(small_root, readonly=False)
    hist = artifact_path(small_root, "runner.history")
    rewrite(hist, version=99)
    before = hist.read_bytes()
    r = _run_and_wait(c)
    assert hist.read_bytes() == before
    assert r["historyState"] == "newer"
    assert r["historyWrite"]["state"] == "skipped" and "не понят" in r["historyWrite"]["reason"]


def test_history_the_agent_refuses_is_reported(small_root, fake_popen):
    """Файл, который сервер счёл годным, а агент записать не смог: `runs` — не список
    записей. Агент 0.2.1 его не трогает (`UnreadableHistory` и `[]`), 0.2.0 падает до
    записи — в обоих случаях файл цел, а прогон помечен незаписанным."""
    c = make_client(small_root, readonly=False)
    hist = artifact_path(small_root, "runner.history")
    rewrite(hist, runs=[1, 2])
    before = hist.read_bytes()
    r = _run_and_wait(c)
    assert hist.read_bytes() == before
    assert r["historyWrite"]["state"] == ("skipped" if ATLAS_021 else "error")


def test_empty_return_means_skipped(small_root, fake_popen, monkeypatch):
    """При пределе ≥ 1 пустой возврат `append` — пропуск: так агент 0.2.1 говорит, что
    файл не тронул."""
    from veha import runner

    monkeypatch.setattr(runner, "append_history", lambda cfg, scope, entry: [])
    r = _run_and_wait(make_client(small_root, readonly=False))
    assert r["historyWrite"]["state"] == "skipped"


def test_failed_write_still_drops_the_graph(small_root, fake_popen, monkeypatch):
    """Исключение при записи не проглатывается молча и не мешает сбросить граф: прогон
    мог переписать артефакты."""
    from veha import runner

    hist = artifact_path(small_root, "runner.history")

    def boom(cfg, scope, entry):
        rewrite(hist, version=99)            # артефакт сменился за время прогона
        raise RuntimeError("диск кончился")

    monkeypatch.setattr(runner, "append_history", boom)
    c = make_client(small_root, readonly=False)
    assert c.get("/api/runner/suites").json()["historyState"] == "ok"     # граф в кэше
    r = _run_and_wait(c)
    assert r["historyWrite"]["state"] == "error"
    assert "RuntimeError" in r["historyWrite"]["reason"]
    assert r["historyState"] == "newer"                                  # граф пересобран


# ------------------------------------------------------------------ вебморда


def test_graph_carries_rule_and_exception_rows(small_root):
    """Вебморде для среднего группы нужны правило отчёта и статус строки."""
    from veha.core.graph import build_root

    g = build_root(_root_with(small_root, _doc("2.7.0")))
    assert g["coverage"]["meta"]["avgRule"] == 2
    statuses = {n["label"]: n["meta"]["status"] for n in g["nodes"]
                if n["layer"] == "api" and n["meta"].get("coverage") is not None}
    assert statuses["GET /health"] == "exception" and statuses["GET /api/v1/orders"] == "full"


def _js_average_source() -> str:
    """`avgRule` и `coverageAverage` из `static/api.js` — как они лежат в пакете."""
    src = (REPO / "veha" / "viz" / "static" / "api.js").read_text(encoding="utf-8")
    start = src.index("  function avgRule(graph)")
    tail = src.index("  function coverageAverage(", start)
    end = src.index("\n  }\n", tail) + len("\n  }\n")
    return src[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node — среднее вебморды "
                                                         "сверяется только при нём")
@pytest.mark.parametrize("version", ["2.6.0", "2.7.0"])
def test_web_group_average_matches_partest(small_root, tmp_path, version):
    """Среднее группы «по сервису» в вебморде — то же, что `avgCoverage` сервиса у
    partest в этом же файле: по правилу 1 exception внутри, по правилу 2 — вне, у
    сервиса из одних exception среднего нет."""
    from veha.core.graph import build_root

    doc = _doc(version)
    g = build_root(_root_with(small_root, doc))
    data = tmp_path / "g.json"
    data.write_text(json.dumps({"coverage": g["coverage"], "nodes": [
        n for n in g["nodes"] if n["layer"] == "api"]}), encoding="utf-8")
    script = tmp_path / "avg.js"
    script.write_text(
        _js_average_source()
        + "\nconst G = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));\n"
        "const by = {};\n"
        "G.nodes.forEach((n) => { (by[n.meta.service || '-'] ||= []).push(n); });\n"
        "const out = {};\n"
        "Object.keys(by).forEach((k) => { out[k] = coverageAverage(G, by[k]); });\n"
        "process.stdout.write(JSON.stringify(out));\n", encoding="utf-8")
    run = subprocess.run(["node", str(script), str(data)], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stderr
    web = json.loads(run.stdout)
    for svc in doc["services"]:
        got = web[svc["label"]]
        if svc["avgCoverage"] is None:
            assert got == {"value": None, "reason": "exception"}
        else:
            assert round(got["value"], 2) == svc["avgCoverage"]
    assert re.search(r"r === 1 \|\| r === 2", _js_average_source())


# ------------------------------------------------------------------ серии и подписи окна


def test_series_without_rule_is_not_a_trend():
    """Серия «правило не записано» может держать точки разных правил: partest обновили
    до 2.7.0 раньше, чем сервер стал писать правило, — 75 → 75 → 50 → 50 было бы
    падением, которого не было. Её точки не тренд."""
    from veha import runner

    runs = [_run(1, 75.0), _run(2, 75.0), _run(3, 50.0), _run(4, 50.0),
            _run(5, 50.0, 2, exception=3)]
    series = runner.trend_series(runs)
    cur, old = series
    assert cur["avgRule"] == 2 and cur["isTrend"] is True
    assert old["avgRule"] is None and old["isTrend"] is False
    assert [p["avg"] for p in old["points"]] == [75.0, 75.0, 50.0, 50.0]


def test_dock_draws_no_arrows_for_a_series_without_rule():
    src = (REPO / "veha" / "viz" / "static" / "dock.js").read_text(encoding="utf-8")
    assert "s.isTrend === false ? ', ' : ' → '" in src
    assert "не тренд: правило среднего не записано" in src
    assert "Правило среднего сменилось (exception в среднем или вне его) — прежние прогоны " \
        "отдельной серией" in src
    assert "В записях прежних прогонов правило среднего не записано — они отдельной серией" in src
    assert "oneNow ? '; тренд — со следующего полного прогона' : ''" in src
    assert "по новому правилу" not in src


def test_no_exception_rows_no_rule_split():
    """Без строк exception правила 1 и 2 дают одно число (`partest.reports.compare`):
    переход 2.6 → 2.7 серию не делит; со строками exception — делит."""
    from veha import runner

    same = runner.trend_series([_run(1, 60.0, 1, exception=0), _run(2, 61.0, 2, exception=0)])
    assert len(same) == 1 and [p["avg"] for p in same[0]["points"]] == [60.0, 61.0]
    split = runner.trend_series([_run(1, 60.0, 1, exception=2), _run(2, 55.0, 2, exception=2)])
    assert len(split) == 2 and split[1]["relation"] == "differs"
    # счётчик не записан — правило как записано
    unknown = runner.trend_series([_run(1, 60.0, 1), _run(2, 55.0, 2)])
    assert len(unknown) == 2


def test_exception_count_is_recorded_even_when_the_agent_does_not(monkeypatch):
    from veha import runner

    def old_agent(document):            # `coverage_snapshot` partest-atlas 0.2.0
        summary = (document or {}).get("summary") or {}
        return {k: summary.get(k) for k in ("avg", "full", "partial", "empty", "endpoints")
                if k in summary}

    monkeypatch.setattr(runner.history, "coverage_snapshot", old_agent)
    old, new = _doc("2.6.0"), _doc("2.7.0")
    for doc in (old, new):
        doc["summary"]["exception"] = 0
    runs = [{"startedAt": i, "suite": "full", "coverage": runner.snapshot_of(d, suite_full=True)}
            for i, d in enumerate((old, new))]
    assert [r["coverage"]["exception"] for r in runs] == [0, 0]
    assert len(runner.trend_series(runs)) == 1


def test_history_rows_name_a_rule_other_than_the_current(small_root):
    hist = artifact_path(small_root, "runner.history")
    rewrite(hist, runs=[
        {"startedAt": 1, "suite": "full", "suiteFull": True,
         "coverage": {"avg": 62.5, "endpoints": 6, "avgRule": 1, "exception": 3}},
        {"startedAt": 2, "suite": "full", "suiteFull": True,
         "coverage": {"avg": 70.0, "endpoints": 6}},
        {"startedAt": 3, "suite": "full", "suiteFull": True,
         "coverage": {"avg": 50.0, "endpoints": 6, "avgRule": 2, "exception": 3}},
    ])
    r = make_client(small_root).get("/api/runner/suites").json()
    marks = [h["coverage"].get("ruleMark") for h in r["history"]]
    assert marks == ["правило 1", "правило не записано", None]
    # в артефакт пометка не пишется
    assert "ruleMark" not in hist.read_text(encoding="utf-8")


@pytest.mark.parametrize("value,limit,warned", [
    (None, 10, False), (3, 3, False), (0, 10, True), (-2, 10, True), ("abc", 10, True),
    (2.5, 10, True), (True, 10, True), ("5", 10, True),
])
def test_history_limit_only_positive_int(small_root, value, limit, warned):
    from veha import config, runner

    text = (small_root / "atlas.yaml").read_text(encoding="utf-8")
    line = "" if value is None else f"  history_limit: {json.dumps(value)}"
    (small_root / "atlas.yaml").write_text(text.replace("  history_limit: 10", line),
                                           encoding="utf-8")
    got, note = runner.history_limit_checked(config.load(small_root))
    assert got == limit and bool(note) is warned


def test_bad_history_limit_does_not_wipe_history(small_root, fake_popen):
    """Предел 0 у агента стёр бы историю. Сервер берёт предел по умолчанию и говорит
    об этом в окне раннера."""
    text = (small_root / "atlas.yaml").read_text(encoding="utf-8")
    (small_root / "atlas.yaml").write_text(
        text.replace("  history_limit: 10", "  history_limit: 0"), encoding="utf-8")
    hist = artifact_path(small_root, "runner.history")
    before = len(json.loads(hist.read_text(encoding="utf-8"))["runs"])
    r = _run_and_wait(make_client(small_root, readonly=False))
    assert r["historyWrite"]["state"] == "written"
    assert "history_limit" in r["historyWrite"]["note"]
    assert len(json.loads(hist.read_text(encoding="utf-8"))["runs"]) == before + 1


def test_os_error_reason_has_no_absolute_path(small_root, fake_popen, monkeypatch):
    from veha import runner

    target = artifact_path(small_root, "runner.history")

    def denied(cfg, scope, entry):
        raise PermissionError(13, "Permission denied", str(target))

    monkeypatch.setattr(runner, "append_history", denied)
    reason = _run_and_wait(make_client(small_root, readonly=False))["historyWrite"]["reason"]
    assert "Permission denied" in reason and "runs.json" in reason
    assert str(small_root) not in reason and str(target.parent) not in reason


def test_other_error_reason_hides_the_data_root(small_root, fake_popen, monkeypatch):
    from veha import runner

    def boom(cfg, scope, entry):
        raise RuntimeError(f"не вышло в {small_root}")

    monkeypatch.setattr(runner, "append_history", boom)
    reason = _run_and_wait(make_client(small_root, readonly=False))["historyWrite"]["reason"]
    assert str(small_root) not in reason and "<каталог данных>" in reason


def test_map_blocks_average_through_the_rule():
    """Блоки доменов на карте считают среднее правилом отчёта, а не своим средним."""
    src = (REPO / "veha" / "viz" / "static" / "map2d.js").read_text(encoding="utf-8")
    assert "const avg = window.vehaApi.coverageAverage(G, b.eps).value;" in src
    assert "known.reduce" not in src and ".reduce((a, v) => a + Number(v)" not in src


def test_services_without_average_say_not_measured():
    src = (REPO / "veha" / "viz" / "static" / "dock.js").read_text(encoding="utf-8")
    body = src[src.index("function viewServices()"):src.index("function renderDockNav()")]
    assert "s.avgCoverage == null" in body and "не измерено" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node")
@pytest.mark.parametrize("coverage,rule", [
    ({}, 1), ({"meta": {}}, 1), ({"meta": {"avgRule": 2}}, 2), ({"meta": {"avgRule": 1}}, 1),
    ({"meta": {"avgRule": 3}}, None), ({"meta": {"avgRule": "2"}}, None),
    ({"meta": None}, None), ({"meta": []}, None), ({"meta": "x"}, None),
])
def test_web_rule_is_the_rule_of_the_server(tmp_path, coverage, rule):
    """Правило в вебморде — то же, что у `veha.runner.avg_rule_of` (вебморда знает только
    1 и 2; правило 3 сервер записал бы как есть, вебморда своё среднее не считает)."""
    from veha import runner

    script = tmp_path / "rule.js"
    script.write_text(_js_average_source() + "\nprocess.stdout.write(JSON.stringify("
                      f"avgRule({{coverage: {json.dumps(coverage)}}})));\n", encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == rule
    server = runner.avg_rule_of({**coverage, "summary": {}})
    assert server == rule or (server == 3 and rule is None)


@pytest.mark.parametrize("runs,series_count", [
    # проект на partest 2.6: у всех правило 1, exception-строка появилась у третьего
    ([(1, 0), (1, 0), (1, 1)], 1),
    # 2.6 без exception → 2.7 без exception
    ([(1, 0), (1, 0), (2, 0)], 1),
    # откат 2.7 → 2.6 без exception
    ([(2, 0), (2, 0), (1, 0)], 1),
    # правило 1 со строками exception рядом с текущим правилом 2 — отдельно
    ([(1, 2), (1, 0), (2, 0)], 2),
    # откат 2.7 → 2.6 со строками exception
    ([(2, 2), (1, 2)], 2),
])
def test_rule_neutral_points_follow_the_current_series(runs, series_count, small_root):
    """Срез без строк exception относится к правилу текущей серии, а не к константе:
    иначе проект на 2.6 получил бы лишнюю серию, чип «правило 2» и строку о смене."""
    from veha import runner

    history = [_run(i, 60.0 + i, rule, exception=exc) for i, (rule, exc) in enumerate(runs)]
    series = runner.trend_series(history)
    assert len(series) == series_count
    if series_count == 1:
        assert series[0]["ruleLabel"] == "" and series[0]["avgRule"] in (1, 2)
    rewrite(artifact_path(small_root, "runner.history"),
            runs=[{**r, "suiteFull": True} for r in history])
    marks = [h["coverage"].get("ruleMark")
             for h in make_client(small_root).get("/api/runner/suites").json()["history"]]
    assert (marks.count(None) == len(runs)) is (series_count == 1)


@pytest.mark.parametrize("runs", [[(2, 3), (2, 3), (1, 0)], [(2, 3), (1, 0), (1, 0)]])
def test_neutral_rule_1_point_takes_the_rule_of_an_earlier_point(runs):
    """Правило 1 с exception: 0 ничего не решает: правило текущей серии — у ближайшей
    более ранней точки, которая решает. Одна серия правила 2, строки о смене нет."""
    from veha import runner

    history = [_run(i, 50.0 + i, rule, exception=exc) for i, (rule, exc) in enumerate(runs)]
    series = runner.trend_series(history)
    assert len(series) == 1 and series[0]["avgRule"] == 2
    assert series[0]["relationBy"]["avgRule"] == "same"
    assert runner.current_rule([_run(1, 50.0, 1, exception=0)]) == 1


def test_points_without_rule_are_skipped_when_choosing_the_current_rule():
    """Последний прогон без правила: его серия текущая и без правила; правило для
    нейтральных точек ищется дальше, мимо него."""
    from veha import runner

    history = [_run(1, 50.0, 2, exception=3), _run(2, 51.0, 1, exception=0), _run(3, 52.0)]
    assert runner.current_rule(history) == 2
    cur, other = runner.trend_series(history)
    assert cur["avgRule"] is None and cur["current"]
    assert other["avgRule"] == 2 and [p["avg"] for p in other["points"]] == [50.0, 51.0]
    src = (REPO / "veha" / "viz" / "static" / "dock.js").read_text(encoding="utf-8")
    assert "cur.avgRule === null" in src
    assert "У последнего прогона правило среднего не записано — прежние прогоны " \
        "отдельной серией" in src


def test_limit_note_survives_the_error_path(small_root, fake_popen, monkeypatch):
    from veha import runner

    text = (small_root / "atlas.yaml").read_text(encoding="utf-8")
    (small_root / "atlas.yaml").write_text(
        text.replace("  history_limit: 10", "  history_limit: 0"), encoding="utf-8")

    def boom(cfg, scope, entry):
        raise RuntimeError("сбой")

    monkeypatch.setattr(runner, "append_history", boom)
    w = _run_and_wait(make_client(small_root, readonly=False))["historyWrite"]
    assert w["state"] == "error" and "history_limit" in w["note"]
