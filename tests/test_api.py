"""API отвечает на основные маршруты; режим просмотра ничего не запускает."""

from __future__ import annotations

import pytest

from tests.conftest import make_client


@pytest.mark.parametrize("url", [
    "/", "/static/api.js", "/static/atlas.js", "/static/dock.js", "/static/scene3d.js", "/static/style.css",
    "/api/graph", "/api/artifacts", "/api/tests", "/api/log",
    "/api/live/containers", "/api/live/mrs", "/api/live/container/storefront-api/logs",
    "/api/runner/suites", "/api/runner/status", "/screens/orders_list.png",
])
def test_route_answers(client, url):
    assert client.get(url).status_code == 200


def test_graph_is_light_and_node_card_is_full(client):
    g = client.get("/api/graph").json()
    assert "tests" not in g
    eid = next(n["id"] for n in g["nodes"]
               if n["kind"] == "endpoint" and n["meta"].get("testCount"))
    assert all("observed" not in n["meta"] for n in g["nodes"])
    card = client.get(f"/api/node/{eid}").json()
    assert card["node"]["meta"]["observed"] and card["tests"]
    assert client.get("/api/node/api:NOPE /x").status_code == 404
    # id безымянного элемента содержит `?` и `#`; страница кодирует его
    # encodeURIComponent, и сервер обязан найти узел
    from urllib.parse import quote

    anon = next(n["id"] for n in g["nodes"] if n["id"].startswith("ui:?"))
    assert client.get("/api/node/" + quote(anon, safe="")).json()["node"]["id"] == anon


def test_test_card_and_attachment(client):
    rows = client.get("/api/tests", params={"status": "failed"}).json()
    assert rows["total"] == 24
    detail = client.get("/api/test", params={"nodeid": rows["tests"][0]["nodeid"]}).json()
    assert detail["steps"]
    src = next((a["source"] for st in detail["steps"] for a in st.get("attachments") or []),
               None) or detail["attachments"][0]["source"]
    assert client.get(f"/api/attachment/{src}").status_code == 200
    assert client.get("/api/attachment/..%5Csecret").status_code in (400, 404)


def test_live_reads_snapshots(client):
    c = client.get("/api/live/containers").json()
    assert (c["enabled"], c["total"], c["down"], c["restarting"]) == (True, 11, 1, 1)
    m = client.get("/api/live/mrs").json()
    assert (m["enabled"], m["opened"], m["merged"], m["unchecked"]) == (True, 3, 7, 6)


def test_runner_history_and_suites(client):
    r = client.get("/api/runner/suites").json()
    # седьмой прогон — ночной полный с opt-in-набором (partest ≥ 2.5.0)
    assert len(r["suites"]) == 7 and len(r["history"]) == 7
    assert r["historyState"] == "ok" and r["readonly"] is True


@pytest.mark.parametrize("url,body", [
    ("/api/runner/start", {"suite": "smoke"}),
    ("/api/runner/clean", None),
    ("/api/runner/stop", None),
    ("/api/rerun", {"nodeid": "tests/x.py::test_y"}),
    ("/api/live/mr", {"iid": 121, "status": "checked"}),
])
def test_readonly_changes_nothing(client, url, body):
    """Всё, что запускает процессы или пишет на диск, закрыто режимом просмотра."""
    assert client.post(url, json=body).status_code == 409


def test_interactive_docs_are_off(client):
    """Swagger UI тянет скрипты и стили с CDN — в закрытом контуре это исходящий запрос."""
    for url in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(url).status_code == 404


def test_mr_mark_goes_to_the_checks_file_only(small_root):
    """Отметки пишет сервер — в свой файл; снимок адаптера со списком МР не трогается."""
    mrs_file = small_root / "live" / "mrs.json"
    snapshot = mrs_file.read_bytes()
    c = make_client(small_root, readonly=False)
    before = c.get("/api/live/mrs").json()["unchecked"]
    iid = next(m["iid"] for m in c.get("/api/live/mrs").json()["mrs"]
               if m["state"] == "merged" and not m["check"])
    after = c.post("/api/live/mr", json={"iid": iid, "status": "checked"}).json()
    assert after["unchecked"] == before - 1
    checks = (small_root / "live" / "mr_checks.json").read_text(encoding="utf-8")
    assert str(iid) in checks and '"mrs"' not in checks
    assert mrs_file.read_bytes() == snapshot


def test_live_without_config_says_so(small_root):
    text = (small_root / "atlas.yaml").read_text(encoding="utf-8")
    (small_root / "atlas.yaml").write_text(
        text.replace("  snapshot_dir: live\n", "").replace(
            "  container_logs_dir: logs/containers\n", ""), encoding="utf-8")
    c = make_client(small_root, readonly=False)
    body = c.get("/api/live/containers").json()
    assert body["enabled"] is False and "snapshot_dir" in body["note"]
    assert "container_logs_dir" in c.get("/api/live/container/x/logs").json()["note"]
    assert c.post("/api/live/mr", json={"iid": 1}).status_code == 409
