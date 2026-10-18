"""`summary.avg: null` в coverage.json: отчёт, который ничего не посчитал.

partest 2.7.0 пишет так отчёт с `endpoints: 0` вместо прежнего `avg: 0`. Сервер, тренд,
вывод при старте, снимок и вебморда переживают `null` и показывают «—», а не 0 % и не
падение.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.conftest import make_client


@pytest.fixture
def null_root(small_root):
    cov = small_root / "coverage.json"
    doc = json.loads(cov.read_text(encoding="utf-8"))
    doc["summary"].update(avg=None, endpoints=0, full=0, partial=0, empty=0)
    cov.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return small_root


def test_graph_and_api_carry_null(null_root):
    c = make_client(null_root)
    g = c.get("/api/graph").json()
    assert g["coverage"]["summary"]["avg"] is None
    assert g["meta"]["endpoints"] > 0               # сцена строится как обычно


def test_startup_report_survives_null(null_root, capsys):
    from veha.cli import _report
    from veha.core.graph import build_root

    _report(build_root(null_root), null_root)
    assert "узлов" in capsys.readouterr().out


def test_snapshot_and_trend_skip_null(null_root):
    from veha import runner

    snap = runner.coverage_snapshot(null_root, suite_full=True)
    assert snap["avg"] is None
    runs = [{"startedAt": 1, "suite": "full", "coverage": {"avg": 60.0, "endpoints": 9}},
            {"startedAt": 2, "suite": "full", "coverage": snap}]
    assert [p["avg"] for p in runner.trend(runs)] == [60.0]
    series = runner.trend_series(runs)
    assert [p["avg"] for s in series for p in s["points"]] == [60.0]


def test_export_survives_null(null_root, tmp_path):
    from veha.viz.export import export_static

    rep = export_static(null_root, tmp_path / "site")
    graph = json.loads((rep.out / "api" / "graph.json").read_text(encoding="utf-8"))
    assert graph["coverage"]["summary"]["avg"] is None


def test_overview_and_trend_show_dash_for_null():
    from veha.viz.app import STATIC

    dock = (STATIC / "dock.js").read_text(encoding="utf-8")
    # средний процент и разница с прошлым — только когда среднее есть
    assert "const noAvg = s.avg == null" in dock
    assert "runs.deltaCoverage == null" in dock
    # точки тренда — через pct(), который даёт «—» на null, а не `${num(x.avg)}%` («0%»)
    assert "pct(x.avg)" in dock and "${num(x.avg)}%" not in dock


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node — помощники не исполнить")
def test_pct_helper_renders_dash_for_null():
    from veha.viz.app import STATIC

    lines = (STATIC / "dock.js").read_text(encoding="utf-8").splitlines()
    helpers = [ln for ln in lines if ln.startswith(("const num = ", "const pct = "))]
    assert len(helpers) == 2
    script = "\n".join(helpers) + (
        "\nconsole.log(JSON.stringify([pct(null), pct(undefined), pct('x'), pct(66.05), pct(0)]));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30,
                         check=True).stdout
    assert json.loads(out) == ["—", "—", "—", "66.05%", "0%"]
