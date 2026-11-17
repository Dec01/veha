"""Защита вебморды: чужой Host, чужая страница, произвольные аргументы pytest, выход за
каталог через имя вложения, секреты в ответах."""

from __future__ import annotations

import json

import pytest

from tests.conftest import BASE_URL, make_client

# ------------------------------------------------------------------ Host / CSRF


@pytest.mark.parametrize("host", ["evil.example:8770", "127.0.0.1:9999", "testserver",
                                  "127.0.0.1", "localhost.evil:8770"])
def test_foreign_host_is_refused(client, host):
    """DNS rebinding: чужое имя, указывающее на 127.0.0.1, ответа не получает."""
    r = client.get("/api/graph", headers={"Host": host})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8770", "localhost:8770"])
def test_own_host_is_served(client, host):
    assert client.get("/api/artifacts", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://localhost:8770"},          # другой источник, чем Host
    {"Referer": "http://evil.example/page"},
])
def test_cross_origin_change_is_refused(small_root, headers):
    c = make_client(small_root, readonly=False)
    r = c.post("/api/live/mr", json={"iid": 121, "status": "checked"}, headers=headers)
    assert r.status_code == 403


def test_same_origin_change_is_accepted(small_root):
    c = make_client(small_root, readonly=False)
    for headers in ({"Origin": BASE_URL}, {"Referer": BASE_URL + "/"}, {}):
        r = c.post("/api/live/mr", json={"iid": 121, "status": "checked"}, headers=headers)
        assert r.status_code == 200, headers


def test_https_public_origin_and_referer_are_accepted_only_for_their_host(small_root):
    c = make_client(small_root, readonly=False, allowed_hosts=["other.internal"],
                    public_origin="https://veha.internal")
    for headers in (
            {"Host": "veha.internal", "Origin": "https://veha.internal"},
            {"Host": "veha.internal", "Referer": "https://veha.internal/run/full"},
    ):
        r = c.post("/api/live/mr", json={"iid": 121, "status": "checked"}, headers=headers)
        assert r.status_code == 200, headers
    for headers in (
            {"Host": "veha.internal", "Origin": "http://veha.internal"},
            {"Host": "veha.internal", "Origin": "https://evil.internal"},
            {"Host": "other.internal", "Origin": "https://veha.internal"},
    ):
        r = c.post("/api/live/mr", json={"iid": 121, "status": "checked"}, headers=headers)
        assert r.status_code == 403, headers
    r = c.post("/api/live/mr", json={"iid": 121, "status": "checked"},
               headers={"Host": "other.internal", "Origin": "http://other.internal"})
    assert r.status_code == 200


def test_https_origin_is_not_accepted_without_public_origin(small_root):
    c = make_client(small_root, readonly=False, allowed_hosts=["veha.internal"])
    r = c.post("/api/live/mr", json={"iid": 121, "status": "checked"},
               headers={"Host": "veha.internal", "Origin": "https://veha.internal"})
    assert r.status_code == 403


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=x"])
def test_body_that_is_not_declared_json_is_refused(small_root, ctype):
    """Форма или `fetch` без preflight с чужой страницы шлёт тело без JSON-типа — оно не
    должно разбираться как JSON ни на какой версии fastapi."""
    c = make_client(small_root, readonly=False)
    headers = {"Content-Type": ctype} if ctype else {}
    r = c.post("/api/live/mr", content=json.dumps({"iid": 121}).encode(), headers=headers)
    assert r.status_code == 415
    assert "121" not in (small_root / "live" / "mr_checks.json").read_text(encoding="utf-8")


def test_security_headers_on_every_answer(client):
    for url in ("/", "/static/atlas.js", "/api/graph", "/api/nope"):
        h = client.get(url).headers
        assert "default-src 'self'" in h["content-security-policy"], url
        assert "script-src 'self'" in h["content-security-policy"]
        # Ни скриптов, ни стилей inline: та же страница уезжает снимком на сайт, где
        # style-src — только 'self'.
        assert "unsafe-inline" not in h["content-security-policy"], url
        assert "unsafe-eval" not in h["content-security-policy"], url
        assert h["x-frame-options"] == "DENY"
        assert h["x-content-type-options"] == "nosniff"
    assert client.get("/", headers={"Host": "evil:1"}).headers["x-frame-options"] == "DENY"


def test_page_has_no_inline_scripts_or_styles():
    """CSP запрещает inline-скрипты и inline-стили; страница должна без них обходиться."""
    import re
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "veha" / "viz" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert not re.search(r"\bstyle\s*=", html)
    for f in static.glob("*.js"):
        text = f.read_text(encoding="utf-8")
        # атрибут в собираемой разметке (`onclick="…"`), а не `img.onload = …` в коде
        assert not re.search(r"\son(click|load|error|change|input)\s*=\s*[\"']", text), f.name
        # style-src без 'unsafe-inline' отбрасывает атрибут в собираемой разметке:
        # стиль пишется `data-css="…"` и ставится через CSSOM (static/api.js)
        assert not re.search(r"\bstyle\s*=\s*[\"'`$]", text), f.name
        assert not re.search(r"\beval\s*\(|new\s+Function\b|setAttribute\(\s*['\"]style",
                             text), f.name


# ------------------------------------------------------------------ раннер


def test_runner_takes_no_arbitrary_pytest_arguments(small_root):
    """`extra` уходил в pytest как есть: `-p`, `-c`, `--rootdir` — выполнение кода."""
    c = make_client(small_root, readonly=False)
    r = c.post("/api/runner/start", json={"suite": "smoke", "extra": ["-p", "evil"]})
    assert r.status_code == 422


def test_rerun_refuses_options_and_outside_files(small_root):
    c = make_client(small_root, readonly=False)
    assert c.post("/api/rerun", json={"nodeid": "-p evil.py::x"}).status_code == 400
    assert c.post("/api/rerun", json={"nodeid": "../outside.py::t"}).status_code == 400


# ------------------------------------------------------------------ вложения


@pytest.mark.parametrize("name", ["D:secret.txt", "C:..\\x", "..", "a%2Fb", "con:x"])
def test_attachment_name_cannot_leave_the_directory(client, name):
    assert client.get(f"/api/attachment/{name}").status_code in (400, 404)
    assert client.get(f"/api/attachment/{name}").status_code != 200


# ------------------------------------------------------------------ секреты

SECRET = "hunter2-very-secret-value"  # publish-ok: вымышленные секрет и токен, проверяется их маскирование
TOKEN = "eyJhbGciOiJIUzI1NiJ9.payload.signature"  # publish-ok: вымышленные секрет и токен, проверяется их маскирование


@pytest.fixture
def leaky_root(small_root):
    """Набор, в котором секреты лежат везде, куда смотрит вебморда."""
    d = small_root / "allure-results"
    d.mkdir()
    (d / "0a110000-0000-4000-8000-000000000011-attachment.json").write_text(json.dumps({
        "request": {"headers": {"Authorization": f"Bearer {TOKEN}"},
                    "body": {"login": "a", "password": SECRET}}}), encoding="utf-8")
    (d / "0a120000-0000-4000-8000-000000000012-attachment.txt").write_text(
        f"curl -H 'Authorization: Bearer {TOKEN}' -d password={SECRET}", encoding="utf-8")
    (d / "r-result.json").write_text(json.dumps({
        "fullName": "tests.t_x.TestX#test_y", "name": "test_y", "status": "failed",
        "start": 1, "stop": 2,
        "statusDetails": {"message": f"AssertionError: token={SECRET}",
                          "trace": f"Authorization: Bearer {TOKEN}"},
        "parameters": [{"name": "api_key", "value": SECRET}],
        "steps": [{"name": f"HTTP GET /api/v1/x?token={SECRET}", "status": "failed",
                   "steps": [{"name": f"cookie: sid={SECRET}", "status": "failed"}],
                   # вложение шага: сервер отдаёт только то, что назвал тест
                   "attachments": [{"name": "curl", "source": "0a120000-0000-4000-8000-000000000012-attachment.txt"}]}],
        "attachments": [{"name": f"password={SECRET}",
                         "source": "0a110000-0000-4000-8000-000000000011-attachment.json"}],
    }), encoding="utf-8")
    logs = small_root / "logs"
    (logs / "containers").mkdir(parents=True)
    (logs / "pytest.log").write_text(f"INFO login password={SECRET}\n", encoding="utf-8")
    (logs / "containers" / "notifier.log").write_text(
        f"ERROR Authorization: Bearer {TOKEN}\n", encoding="utf-8")
    return small_root


def test_secrets_are_masked_in_every_answer(leaky_root):
    c = make_client(leaky_root)
    answers = [
        c.get("/api/attachment/0a110000-0000-4000-8000-000000000011-attachment.json").text,
        c.get("/api/attachment/0a120000-0000-4000-8000-000000000012-attachment.txt").text,
        c.get("/api/log").text,
        c.get("/api/live/container/notifier/logs").text,
        c.get("/api/test", params={"file": "r-result.json"}).text,
        c.get("/api/graph").text,
    ]
    for text in answers:
        assert SECRET not in text and TOKEN not in text, text[:300]
    # маскируется значение, а не смысл: схема и ключи остаются видны
    assert "Bearer" in answers[1] and "password" in answers[0]


def test_attachment_metadata_is_masked_not_only_its_text(leaky_root):
    c = make_client(leaky_root)
    detail = c.get("/api/test", params={"file": "r-result.json"}).json()
    assert detail["attachments"][0]["name"] == "password=***"
    assert SECRET not in json.dumps(detail["attachments"], ensure_ascii=False)


def test_legacy_map_state_problems_are_masked_on_read(small_root):
    from veha.core import map_state

    path = small_root / ".veha" / "map" / "default" / "default" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": map_state.VERSION,
        # publish-ok: вымышленный секрет в problems унаследованного state проверяет маскирование
        "problems": [f"повреждённая запись: DB_PASSWORD={SECRET}"],
    }), encoding="utf-8")
    response = make_client(small_root).get("/api/graph")
    assert response.status_code == 200
    assert SECRET not in response.text
    assert any("DB_PASSWORD=***" in problem
               for problem in response.json()["meta"]["reportProblems"])


def test_legacy_map_state_payload_is_masked_like_primary_sources(small_root):
    from partest_atlas.artifacts import Scope

    from veha.config import Config
    from veha.core import map_state, sources

    nodeid = "tests/test_saved.py::test_saved"
    path = small_root / ".veha" / "map" / "default" / "default" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": map_state.VERSION,
        "allure": {
            "tests": [{
                "name": "saved", "nodeid": nodeid, "variant": nodeid,
                "status": "failed", "start": 1, "stop": 2, "ms": 1,
                "feature": "", "story": "", "parentSuite": "", "isUi": False,
                "endpoints": ["api:GET /saved"], "file": "saved-result.json",
                # publish-ok: вымышленный секрет в ошибке унаследованного state проверяет маскирование
                "error": {"head": f"AssertionError: DB_PASSWORD={SECRET}",
                          "body": "", "trace": ""},
            }],
            "calls": {"api:GET /saved": [{
                "test": "saved", "nodeid": nodeid, "variant": nodeid,
                "checks": [{"name": f"token={SECRET}", "status": "failed"}],
            }]},
            "attachments": [f"token={SECRET}-attachment.txt"],
        },
        "coverage": {
            "endpoints": [{"id": "api:GET /saved", "method": "GET", "path": "/saved",
                           "coverage": 100, "note": f"password={SECRET}"}],
            # publish-ok: вымышленный секрет в coverage extras унаследованного state проверяет маскирование
            "extras": {"note": f"DB_PASSWORD={SECRET}"},
        },
        "artifacts": {"db.effects": {"records": [{
            "nodeid": nodeid, "table": "session", "note": f"Authorization: Bearer {SECRET}",
        }]}},
    }), encoding="utf-8")

    loaded = map_state.apply(sources.load(Config(root=small_root, raw={}), Scope()))
    assert loaded.allure["tests"][0]["error"]["head"] == "AssertionError: DB_PASSWORD=***"
    assert loaded.allure["calls"]["api:GET /saved"][0]["checks"][0]["name"] == "token=***"
    assert loaded.allure["attachments"] == {"token=***"}
    assert loaded.coverage["api:GET /saved"]["note"] == "password=***"
    assert loaded.coverage_extras["note"] == "DB_PASSWORD=***"
    effect = loaded.artifacts.payload("db.effects")[0]
    assert effect["note"] == "Authorization: Bearer ***"
    assert effect["table"] == "session"       # идентификаторы Atlas не искажаются

    response = make_client(small_root).get("/api/graph")
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert response.json()["run"]["failures"][0]["error"]["head"] == \
        "AssertionError: DB_PASSWORD=***"
    assert response.json()["coverage"]["note"] == "DB_PASSWORD=***"
    assert response.json()["dbEffects"][0]["note"] == "Authorization: Bearer ***"


@pytest.mark.parametrize("endpoint", ["token", "session", "authorization"])
def test_map_state_sensitive_endpoint_keys_keep_call_records(small_root, endpoint):
    from partest_atlas.artifacts import Scope

    from veha.config import Config
    from veha.core import map_state, sources

    nodeid = "tests/test_saved.py::test_saved"
    endpoint_id = f"api:GET /{endpoint}"
    path = small_root / ".veha" / "map" / "default" / "default" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": map_state.VERSION,
        "allure": {
            "tests": [],
            "calls": {endpoint_id: [{
                "test": "saved", "nodeid": nodeid, "variant": nodeid, "status": "failed",
                "checks": [{"name": f"token={SECRET}", "status": "failed"}],
            }]},
            "attachments": [],
        },
    }), encoding="utf-8")

    loaded = map_state.apply(sources.load(Config(root=small_root, raw={}), Scope()))
    records = loaded.allure["calls"][endpoint_id]
    assert isinstance(records, list) and len(records) == 1
    assert records[0]["checks"][0]["name"] == "token=***"

    response = make_client(small_root).get("/api/graph")
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert any(node["id"] == endpoint_id for node in response.json()["nodes"])


def test_result_filename_in_source_problems_is_masked(small_root, monkeypatch):
    from veha.core import sources

    raw = small_root / "allure-results"
    raw.mkdir()
    filename = f"token={SECRET}-result.json"
    (raw / filename).write_text("broken", encoding="utf-8")
    real_check = sources.allure_result_path

    def reject_secret_name(directory, name):
        if name == filename:
            raise ValueError("символическая ссылка не читается")
        return real_check(directory, name)

    monkeypatch.setattr(sources, "allure_result_path", reject_secret_name)
    response = make_client(small_root).get("/api/graph")
    assert response.status_code == 200
    assert SECRET not in response.text
    assert any("token=***" in problem and "символическая ссылка" in problem
               for problem in response.json()["meta"]["reportProblems"])
