"""Артефакты: демонстрационный набор сходится с контрактом агента, а то, что сервер
прочитать не может, он называет явно — и не падает, и не показывает молча пустоту."""

from __future__ import annotations

import json

import pytest
from partest_atlas import artifacts
from partest_atlas.artifacts import KINDS, SCHEMA_VERSION, Scope

from tests.conftest import artifact_path, rewrite, make_client
from veha import config
from veha.core import sources

# ---------------------------------------------------------------- демо-набор


def test_every_demo_artifact_passes_validate(demo_root):
    files = sorted((demo_root / "atlas").rglob("*.json"))
    assert len(files) == 8
    for f in files:
        doc = artifacts.read(f)
        assert artifacts.validate(doc) == [], f
        assert artifacts.filename(doc["kind"]) == f.name, f
        assert doc["version"] == SCHEMA_VERSION


def test_demo_set_is_all_ok_and_in_default_scope(demo_root):
    arts = sources.load_artifacts(config.load(demo_root))
    assert arts.scope == Scope()
    states = {k: s.state for k, s in arts.statuses.items()}
    assert states == {k: ("missing" if k == "load.profile" else "ok") for k in KINDS}
    assert arts.problems == [] and arts.ignored == []


# ------------------------------------------------- несовместимое и битое


@pytest.fixture
def server(small_root):
    def make():
        return make_client(small_root)
    return make


def _status(client, kind):
    body = client.get("/api/artifacts").json()
    return next(a for a in body["artifacts"] if a["kind"] == kind), body


def test_newer_schema_is_reported_not_read(small_root, server):
    path = artifact_path(small_root, "db.schema")
    rewrite(path, version=SCHEMA_VERSION + 1)

    c = server()
    st, body = _status(c, "db.schema")
    assert st["state"] == "newer"
    assert any("новее" in p for p in st["problems"])
    assert body["problems"] == 1

    g = c.get("/api/graph").json()
    assert g["meta"]["artifactProblems"] == 1
    # не прочитан — значит, схемы на сцене нет, а не «схема из непонятного файла»
    assert g["db"]["databases"] == [] and g["collected"]["hasSchema"] is False
    # остальное на месте: один несовместимый файл не роняет карту
    assert g["meta"]["endpoints"] == 177


def test_broken_json_is_reported(small_root, server):
    artifact_path(small_root, "bus.flow").write_text("{не json", encoding="utf-8")
    st, _ = _status(server(), "bus.flow")
    assert st["state"] == "broken" and st["problems"]


def test_prototype_v1_artifact_is_invalid_with_a_hint(small_root, server):
    path = artifact_path(small_root, "ui.flow")
    path.write_text(json.dumps({"version": 1, "kind": "ui_flow", "links": []}),
                    encoding="utf-8")
    st, _ = _status(server(), "ui.flow")
    assert st["state"] == "invalid"
    assert any("снимите его заново" in p for p in st["problems"])


def test_kind_must_match_the_file(small_root, server):
    """`validate` пропустит вид из набора в чужом файле; сервер — нет."""
    schema = artifacts.read(artifact_path(small_root, "db.schema"))
    artifact_path(small_root, "db.effects").write_text(json.dumps(schema), encoding="utf-8")
    st, _ = _status(server(), "db.effects")
    assert st["state"] == "invalid"
    assert any("лежит в файле вида" in p for p in st["problems"])


def test_scope_in_file_differs_from_directory(small_root, server):
    path = artifact_path(small_root, "specs.requirements")
    rewrite(path, scope={"system": "shop", "env": "preprod"})
    st, _ = _status(server(), "specs.requirements")
    assert st["state"] == "ok" and st["warnings"]


def test_missing_artifact_is_a_normal_state(small_root, server):
    artifact_path(small_root, "contract.openapi").unlink()
    st, body = _status(server(), "contract.openapi")
    assert st["state"] == "missing" and body["problems"] == 0


def test_broken_atlas_yaml_does_not_crash_the_server(small_root, server):
    (small_root / "atlas.yaml").write_text("containers: [unclosed", encoding="utf-8")
    c = server()
    body = c.get("/api/artifacts").json()
    assert body["config"]["exists"] and body["config"]["error"]
    assert c.get("/api/graph").json()["meta"]["configError"]


def test_flat_layout_is_named_not_read(small_root):
    """Файлы прямо в atlas/ (плоская раскладка схемы 1) не читаются и перечисляются."""
    src = artifact_path(small_root, "db.schema")
    (small_root / "atlas" / "db_schema.json").write_text(src.read_text(encoding="utf-8"),
                                                         encoding="utf-8")
    arts = sources.load_artifacts(config.load(small_root))
    assert any("db_schema.json" in note for note in arts.ignored)


# ------------------------------------------------- граф при плохих артефактах


def test_graph_survives_a_broken_artifact(small_root, server):
    """Битые эффекты БД: связи «пишет в таблицу», снятые на прогоне, пропадают, сцена
    остаётся, причина названа."""
    artifact_path(small_root, "db.effects").write_text("[[[", encoding="utf-8")
    g = server().get("/api/graph").json()
    assert g["meta"]["artifactProblems"] == 1
    assert g["collected"]["dbEffects"] == 0 and g["dbEffects"] == []
    assert g["meta"]["endpoints"] == 177


def test_graph_survives_an_invalid_artifact(small_root, server):
    """Связи UI в формате схемы 1 не читаются: безымянных элементов, снятых агентом,
    на сцене нет, ручная карта на месте."""
    artifact_path(small_root, "ui.flow").write_text(
        json.dumps({"version": 1, "kind": "ui_flow", "links": [{"control": "x"}]}),
        encoding="utf-8")
    g = server().get("/api/graph").json()
    assert g["meta"]["artifactProblems"] == 1 and g["collected"]["uiLinks"] == 0
    assert not [n for n in g["nodes"] if n["id"].startswith("ui:?")]
    assert sum(1 for n in g["nodes"] if n["kind"] == "control") == 46


# ------------------------------------------------- подсказки и предупреждения


def test_flat_v1_and_flat_history_get_their_own_advice(small_root):
    base = small_root / "atlas"
    (base / "db_schema.json").write_text(
        json.dumps({"version": 1, "kind": "db_schema", "databases": []}), encoding="utf-8")
    (base / "runs.json").write_text(
        json.dumps({"version": 1, "kind": "runs", "runs": []}), encoding="utf-8")
    notes = sources.load_artifacts(config.load(small_root)).ignored
    schema = next(n for n in notes if n.startswith("db_schema.json"))
    runs = next(n for n in notes if n.startswith("runs.json"))
    assert "схема 1" in schema and "снимите заново" in schema
    # v1: обратимое переименование, а не удаление и не перенос — пока вопрос
    # «обёртка v1 → v2» открыт, интерфейс не решает за пользователя
    assert "runs.v1.json" in runs and "удалите" not in runs and "перенес" not in runs


def test_flat_v2_history_is_advised_to_move(small_root):
    src = artifact_path(small_root, "runner.history")
    (small_root / "atlas" / "runs.json").write_text(src.read_text(encoding="utf-8"),
                                                     encoding="utf-8")
    note = next(n for n in sources.load_artifacts(config.load(small_root)).ignored
                if n.startswith("runs.json"))
    assert "перенесите" in note and "<система>/<контур>/" in note


@pytest.mark.parametrize("content", ["", "{не json", "[]"])
def test_flat_broken_file_is_not_called_schema_one(small_root, content):
    (small_root / "atlas" / "bus_flow.json").write_text(content, encoding="utf-8")
    note = next(n for n in sources.load_artifacts(config.load(small_root)).ignored
                if n.startswith("bus_flow.json"))
    assert "не разбирается" in note and "схема 1" not in note


def test_old_history_in_scope_gets_its_own_advice(small_root):
    path = artifact_path(small_root, "runner.history")
    path.write_text(json.dumps({"version": 1, "kind": "runs", "runs": []}), encoding="utf-8")
    st = sources.load_artifacts(config.load(small_root)).statuses["runner.history"]
    text = "; ".join(st.problems)
    assert st.state == "invalid"
    assert "переснять прошлые прогоны нельзя" in text and "runs.v1.json" in text
    assert "снимите его заново агентом" not in text and "удалите" not in text


def test_missing_payload_key_is_a_warning_not_silent_ok(small_root):
    path = artifact_path(small_root, "bus.flow")
    doc = artifacts.read(path)
    doc["events"] = doc.pop("records")                 # ключ схемы 1 вместо агента
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    st = sources.load_artifacts(config.load(small_root)).statuses["bus.flow"]
    assert st.state == "ok" and any("records" in w for w in st.warnings)


def test_load_profile_is_not_read_until_agreed(small_root):
    path = artifact_path(small_root, "load.profile")
    path.write_text(json.dumps({"version": 2, "kind": "load.profile",
                                "scope": {"system": "default", "env": "default"},
                                "collected_at": "2026-10-06T10:00:00+03:00",
                                "collector": "partest-load"}), encoding="utf-8")
    st = sources.load_artifacts(config.load(small_root)).statuses["load.profile"]
    assert any("не согласован" in w for w in st.warnings)
    assert "load.profile" not in sources.PAYLOAD_KEY


def test_server_reads_the_schema_the_agent_writes():
    """Агент выпустил новую схему — этот тест падает раньше, чем сервер встретит её на
    установке. Чинится сознательно: прочитать changelog агента, поправить чтение,
    добавить версию в READS_SCHEMA."""
    assert SCHEMA_VERSION in sources.READS_SCHEMA, (
        f"агент пишет схему {SCHEMA_VERSION}, сервер читает {sorted(sources.READS_SCHEMA)}. "
        "Сначала сверь PAYLOAD_KEY и чтение графа (veha/core/graph.py) с новой схемой по "
        "CHANGELOG partest-atlas, потом добавляй версию в READS_SCHEMA — не наоборот")
