"""opt-in-наборы маркеров partest ≥ 2.5.0 (`meta.selection.optin`).

Набор, объявленный в `partest_optin_markers`, снимается с прогона (deselect), пока его
не попросили `-m` или переменной. `partialRun` на это не смотрит: прогон полный, но
другого состава. Здесь — что сервер показывает и как не смешивает такие прогоны в тренде.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import artifact_path, make_client
from tests.test_runner import _fresh_coverage, _wait_for_history, fake_popen  # noqa: F401

SLOW_OFF = {"slow": {"state": "off", "env": "RUN_SLOW", "deselected": 14}}
SLOW_ENV = {"slow": {"state": "env", "env": "RUN_SLOW", "deselected": 0}}


def _set_selection(root, selection, **meta):
    cov = root / "coverage.json"
    doc = json.loads(cov.read_text(encoding="utf-8"))
    if selection is None:
        doc["meta"].pop("selection", None)
    else:
        doc["meta"]["selection"] = selection
    doc["meta"].update(meta)
    cov.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ чтение


@pytest.mark.parametrize("meta", [
    {}, {"selection": None}, {"selection": "x"}, {"selection": {}},
    {"selection": {"markexpr": "smoke"}}, {"selection": {"optin": ["slow"]}},
])
def test_no_record_is_none_not_empty(meta):
    """Нет ключа — «не знаем», а не «ничего не снято»."""
    from veha.core.sources import optin_record

    assert optin_record(meta) is None


def test_record_is_normalized_and_sorted():
    from veha.core.sources import optin_record

    rec = optin_record({"selection": {"optin": {
        "slow": {"state": "off", "deselected": "3"},
        "db": {"state": "env", "env": "RUN_DB", "deselected": True},
        "e2e": None,
        "net": {"state": "off", "deselected": -2},
    }}})
    assert list(rec) == ["db", "e2e", "net", "slow"]
    assert rec["db"] == {"state": "env", "env": "RUN_DB", "deselected": 0}
    assert rec["e2e"] == {"state": None, "deselected": 0}
    assert rec["net"]["deselected"] == 0 and rec["slow"]["deselected"] == 3
    assert "env" not in rec["slow"]


@pytest.mark.parametrize("info,text", [
    ({"state": "off", "deselected": 1}, "slow — снят (1 тест)"),
    ({"state": "off", "deselected": 3}, "slow — снят (3 теста)"),
    ({"state": "off", "deselected": 14}, "slow — снят (14 тестов)"),
    ({"state": "off", "deselected": 21}, "slow — снят (21 тест)"),
    ({"state": "off", "deselected": 112}, "slow — снят (112 тестов)"),
    ({"state": "off", "deselected": 0}, "slow — выключен, снимать было нечего"),
    ({"state": "markexpr", "deselected": 0}, "slow — включён через -m"),
    ({"state": "markexpr", "deselected": 2}, "slow — включён через -m, снято 2 теста"),
    ({"state": "env", "env": "RUN_SLOW", "deselected": 0}, "slow — включён через RUN_SLOW"),
    ({"state": "env", "deselected": 0}, "slow — включён через переменную"),
    ({"state": "later", "deselected": 0}, "slow — состояние не распознано (later)"),
    ({"deselected": 0}, "slow — состояние не распознано (нет)"),
])
def test_phrases_read_like_a_sentence(info, text):
    from veha.core.sources import normalize_optin, optin_phrases

    assert optin_phrases(normalize_optin({"slow": info})) == [text]


def test_phrases_without_counts_name_the_state_only():
    from veha.core.sources import optin_phrases

    assert optin_phrases(SLOW_OFF, counts=False) == ["slow — снят"]
    assert optin_phrases(SLOW_ENV, counts=False) == ["slow — включён через RUN_SLOW"]
    assert optin_phrases(None) == []


def test_acted_means_something_was_dropped_or_switched_on():
    from veha.core.sources import optin_acted

    assert optin_acted(SLOW_OFF) and optin_acted(SLOW_ENV)
    assert optin_acted({"slow": {"state": "markexpr", "deselected": 0}})
    assert not optin_acted({"slow": {"state": "off", "deselected": 0}})
    assert not optin_acted(None) and not optin_acted({})


def test_signature_ignores_counts():
    """Тест, добавленный в снятый набор, состав прогона не меняет — серия та же."""
    from veha.core.sources import optin_signature

    more = {"slow": {**SLOW_OFF["slow"], "deselected": 20}}
    assert optin_signature(SLOW_OFF) == optin_signature(more)
    assert optin_signature(SLOW_OFF) != optin_signature(SLOW_ENV)
    assert optin_signature(None) is None


def _payload(optin):
    meta = {"workers": 1, "merged": True, "partialRun": False}
    if optin is not None:
        meta["selection"] = {"optin": optin}
    ep = {"method": "GET", "path": "/x", "coverage": 50.0, "calls": 10, "kind": "partial"}
    return {"meta": meta, "endpoints": [ep]}


@pytest.mark.parametrize("a,b", [
    (SLOW_OFF, SLOW_OFF),
    (SLOW_OFF, {"slow": {**SLOW_OFF["slow"], "deselected": 2}}),
    (SLOW_OFF, SLOW_ENV),
    (SLOW_OFF, {"slow": {"state": "markexpr", "deselected": 0}}),
    (SLOW_OFF, {**SLOW_OFF, "db": {"state": "off", "deselected": 1}}),
    (SLOW_OFF, None),
    (None, SLOW_ENV),
    (None, None),
])
def test_series_rule_is_the_rule_of_partest_compare(a, b):
    """Серия тренда делится ровно там, где `compare_payloads` partest говорит «несравнимо».

    Правило записано у нас копией (у partest оно не публичное), поэтому сверяется с
    публичным `compare_payloads`: разойдутся — упадёт здесь, а не на установке.
    """
    from partest.reports.compare import compare_payloads

    from veha import runner

    runs = [{"startedAt": i, "suite": "full", "coverage": {"avg": 50.0, "optin": o}}
            for i, o in enumerate((a, b))]
    for r, o in zip(runs, (a, b)):
        if o is None:
            del r["coverage"]["optin"]
    series = runner.trend_series(runs)
    result = compare_payloads(_payload(a), _payload(b))
    if a is not None and b is not None:
        assert (len(series) == 1) == result["comparable"]
    else:
        # одна запись: partest — сравнимо, с пометкой; тренд — отдельная серия
        assert result["comparable"]
        assert len(series) == (1 if a is None and b is None else 2)
        if len(series) == 2:
            assert series[1]["relation"] == "unknown"


# ------------------------------------------------------------------ граф и шапка


def test_demo_graph_names_the_dropped_set(demo_graph):
    meta = demo_graph["meta"]
    assert meta["partialRun"] is False
    assert meta["optin"] == SLOW_OFF
    assert meta["optinText"] == ["slow — снят (14 тестов)"]
    assert meta["optinActed"] is True


def test_graph_without_the_key_says_nothing(small_root):
    from veha.core.graph import build_root

    _set_selection(small_root, None)
    meta = build_root(small_root)["meta"]
    assert meta["optin"] is None and meta["optinText"] == [] and meta["optinActed"] is False


def test_partial_run_keeps_optin_out_of_selection_text(small_root):
    """Подсказка частичного прогона перечисляет выбор — запись opt-in в ней не объект."""
    from veha.core.graph import build_root

    _set_selection(small_root, {"markexpr": "slow", "optin": {
        "slow": {"state": "markexpr", "env": "RUN_SLOW", "deselected": 0}}}, partialRun=True)
    meta = build_root(small_root)["meta"]
    assert meta["partialReasons"] == ["выбор -m/-k"]
    assert meta["optinText"] == ["slow — включён через -m"]


def test_header_script_does_not_print_objects():
    """`${k}: ${v}` по выбору давал «optin: [object Object]» — объекты отсеиваются."""
    from tests.conftest import REPO

    js = (REPO / "veha" / "viz" / "static" / "atlas.js").read_text(encoding="utf-8")
    block = js[js.index("if (G.meta.partialRun)"):js.index("/* Пилюли над картой")]
    assert "k !== 'optin'" in block and "typeof v !== 'object'" in block
    assert "esc(optinText.join" in block


def test_startup_prints_the_optin_state(demo_root, monkeypatch, capsys):
    import uvicorn

    from veha import cli

    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **kw: None)
    monkeypatch.delenv("ATLAS_SYSTEM", raising=False)
    monkeypatch.delenv("ATLAS_ENV", raising=False)
    assert cli.main(["serve", "--root", str(demo_root), "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "coverage.json — opt-in: slow — снят (14 тестов)" in out
    assert "[object Object]" not in out


# ------------------------------------------------------------------ история


def test_snapshot_keeps_the_record_for_full_runs(demo_root):
    from veha import runner

    snap = runner.coverage_snapshot(demo_root)
    assert snap["avg"] == 66.05 and snap["optin"] == SLOW_OFF


def test_snapshot_keeps_the_record_for_partial_runs_once(small_root):
    from veha import runner

    _set_selection(small_root, {"markexpr": "smoke", "optin": SLOW_OFF}, partialRun=True)
    snap = runner.coverage_snapshot(small_root)
    assert "avg" not in snap and snap["selection"] == {"markexpr": "smoke"}
    assert snap["optin"] == SLOW_OFF


def test_snapshot_without_the_key_has_no_record(small_root):
    from veha import runner

    _set_selection(small_root, None)
    assert "optin" not in runner.coverage_snapshot(small_root)


def test_run_records_the_optin_it_saw(small_root, fake_popen):  # noqa: F811
    _fresh_coverage(small_root)
    c = make_client(small_root, readonly=False)
    hist = artifact_path(small_root, "runner.history")
    before = hist.read_bytes()
    assert c.post("/api/runner/start", json={"suite": "full"}).status_code == 200
    _wait_for_history(hist, before)
    last = json.loads(hist.read_text(encoding="utf-8"))["runs"][-1]
    assert last["suite"] == "full" and last["coverage"]["optin"] == SLOW_OFF
    # подпись для окна — только в ответе API, в артефакт не пишется
    assert "optinText" not in last["coverage"]


# ------------------------------------------------------------------ тренд


def _run(t, avg, optin=None, **cov):
    c = {"avg": avg, **cov}
    if optin is not None:
        c["optin"] = optin
    return {"startedAt": t, "suite": "full", "coverage": c}


def test_trend_without_records_is_one_series():
    from veha import runner

    series = runner.trend_series([_run(1, 60.0), _run(2, 61.0)])
    assert len(series) == 1
    assert series[0]["current"] and series[0]["label"] == "" and series[0]["series"] == ""
    assert [p["avg"] for p in series[0]["points"]] == [60.0, 61.0]


def test_trend_splits_by_state_and_puts_the_current_first():
    from veha import runner

    more = {"slow": {**SLOW_OFF["slow"], "deselected": 16}}
    runs = [_run(1, 70.0, SLOW_ENV), _run(2, 66.0, SLOW_OFF), _run(3, 71.0, SLOW_ENV),
            _run(4, 99.0, SLOW_OFF, partialRun=True), _run(5, 67.0, more)]
    series = runner.trend_series(runs)
    assert [s["label"] for s in series] == ["slow — снят", "slow — включён через RUN_SLOW"]
    assert [p["avg"] for p in series[0]["points"]] == [66.0, 67.0]
    assert [p["avg"] for p in series[1]["points"]] == [70.0, 71.0]
    assert [s["relation"] for s in series] == ["same", "differs"]
    assert series[0]["optin"] == more            # подпись — по свежей точке серии
    flat = runner.trend(runs)
    assert [p["avg"] for p in flat] == [70.0, 66.0, 71.0, 67.0]
    assert len({p["series"] for p in flat}) == 2


def test_runs_before_the_record_are_their_own_series():
    """Набор обычно появляется вместе с записью: до неё медленные тесты шли в общем
    прогоне. Одна линия нарисовала бы падение, которого не было."""
    from veha import runner

    series = runner.trend_series([_run(1, 72.0), _run(2, 72.5), _run(3, 66.0, SLOW_OFF)])
    assert [len(s["points"]) for s in series] == [1, 2]
    assert series[0]["current"] and series[1]["relation"] == "unknown"
    assert series[1]["label"] == ""


def test_no_full_runs_no_series():
    from veha import runner

    assert runner.trend_series([]) == []
    assert runner.trend_series([_run(1, 50.0, partialRun=True)]) == []


def test_api_shows_demo_series_and_labels(client):
    r = client.get("/api/runner/suites").json()
    cur, other = r["trendSeries"]
    assert cur["current"] and cur["label"] == "slow — снят"
    assert [p["avg"] for p in cur["points"]] == [66.05]
    assert other["relation"] == "differs" and other["label"] == "slow — включён через RUN_SLOW"
    assert [p["avg"] for p in other["points"]] == [67.95]
    full = [h for h in r["history"] if h.get("suiteFull")]
    assert [h["coverage"]["optinText"] for h in full] == [
        ["slow — включён через RUN_SLOW"], ["slow — снят (14 тестов)"]]
    partial = [h for h in r["history"] if not h.get("suiteFull")]
    assert all("optinText" not in h["coverage"] for h in partial)
