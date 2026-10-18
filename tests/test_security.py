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
    (d / "att-1-attachment.json").write_text(json.dumps({
        "request": {"headers": {"Authorization": f"Bearer {TOKEN}"},
                    "body": {"login": "a", "password": SECRET}}}), encoding="utf-8")
    (d / "att-2-attachment.txt").write_text(
        f"curl -H 'Authorization: Bearer {TOKEN}' -d password={SECRET}", encoding="utf-8")
    (d / "r-result.json").write_text(json.dumps({
        "fullName": "tests.t_x.TestX#test_y", "name": "test_y", "status": "failed",
        "start": 1, "stop": 2,
        "statusDetails": {"message": f"AssertionError: token={SECRET}",
                          "trace": f"Authorization: Bearer {TOKEN}"},
        "parameters": [{"name": "api_key", "value": SECRET}],
        "steps": [{"name": f"HTTP GET /api/v1/x?token={SECRET}", "status": "failed",
                   "steps": [{"name": f"cookie: sid={SECRET}", "status": "failed"}]}],
        "attachments": [{"name": "req", "source": "att-1-attachment.json"}],
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
        c.get("/api/attachment/att-1-attachment.json").text,
        c.get("/api/attachment/att-2-attachment.txt").text,
        c.get("/api/log").text,
        c.get("/api/live/container/notifier/logs").text,
        c.get("/api/test", params={"file": "r-result.json"}).text,
        c.get("/api/graph").text,
    ]
    for text in answers:
        assert SECRET not in text and TOKEN not in text, text[:300]
    # маскируется значение, а не смысл: схема и ключи остаются видны
    assert "Bearer" in answers[1] and "password" in answers[0]
