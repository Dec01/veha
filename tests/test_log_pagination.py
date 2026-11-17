"""Порции журнала по абсолютным offsets и совместимость статического снимка."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.conftest import make_client

NODE = shutil.which("node")


def _long_log(root, count: int = 1005):
    lines = [f"line-{i:04d}" for i in range(count)]
    path = root / "logs" / "pytest.log"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def test_log_pages_are_contiguous_and_have_no_duplicates(small_root):
    expected = _long_log(small_root)
    client = make_client(small_root)

    first = client.get("/api/log", params={"name": "Прогон pytest", "tail": 400}).json()
    assert (first["start"], first["end"], first["total"], first["hasMore"]) == \
        (605, 1005, 1005, True)
    assert first["lines"] == expected[605:]

    previous = client.get("/api/log", params={
        "name": "Прогон pytest", "tail": 400, "end": first["start"]}).json()
    assert (previous["start"], previous["end"], previous["hasMore"]) == (205, 605, True)
    assert previous["lines"] == expected[205:605]

    oldest = client.get("/api/log", params={
        "name": "Прогон pytest", "tail": 400, "end": previous["start"]}).json()
    assert (oldest["start"], oldest["end"], oldest["hasMore"]) == (0, 205, False)
    assert oldest["lines"] == expected[:205]
    assert oldest["lines"] + previous["lines"] + first["lines"] == expected
    assert len(set(oldest["lines"] + previous["lines"] + first["lines"])) == len(expected)


def test_log_page_boundaries_and_limits(small_root):
    expected = _long_log(small_root)
    client = make_client(small_root)
    params = {"name": "Прогон pytest", "tail": 400}

    at_zero = client.get("/api/log", params={**params, "end": 0}).json()
    assert at_zero["lines"] == []
    assert (at_zero["start"], at_zero["end"], at_zero["total"], at_zero["hasMore"]) == \
        (0, 0, 1005, False)
    negative = client.get("/api/log", params={**params, "end": -20}).json()
    assert negative["lines"] == [] and negative["start"] == negative["end"] == 0

    first = client.get("/api/log", params=params).json()
    beyond = client.get("/api/log", params={**params, "end": 9000}).json()
    assert beyond == first
    one = client.get("/api/log", params={**params, "end": 1}).json()
    assert one["lines"] == expected[:1] and one["hasMore"] is False

    capped = client.get("/api/log", params={**params, "tail": 5001}).json()
    assert capped["lines"] == expected and capped["hasMore"] is False
    minimum = client.get("/api/log", params={**params, "tail": 0}).json()
    assert minimum["lines"] == expected[-1:]
    assert client.get("/api/log", params={**params, "end": "bad"}).status_code == 422


def test_log_client_sends_page_cursor_and_disables_it_offline():
    from veha.viz.app import STATIC

    api = (STATIC / "api.js").read_text(encoding="utf-8")
    body = api.split("async log(name, tail = 400, end)", 1)[1].split("async tests", 1)[0]
    assert "new URLSearchParams({ tail: String(tail) })" in body
    assert "q.set('end', String(end))" in body
    assert "return { ...found, hasMore: false }" in body


@pytest.mark.skipif(NODE is None, reason="нет node — offline API не исполнить")
def test_offline_log_cannot_prepend_the_same_exported_page(tmp_path):
    from veha.viz.app import STATIC

    payload = {
        "first": "main",
        "names": ["main"],
        "logs": {"main": {
            "files": ["main"], "name": "main",
            "lines": [f"line-{i:04d}" for i in range(605, 1005)],
            "start": 605, "end": 1005, "total": 1005, "hasMore": True,
        }},
    }
    script = tmp_path / "offline-log.js"
    script.write_text(f"""
const payload = {json.dumps(payload)};
global.window = global;
global.document = {{
  documentElement: {{}},
  querySelector: (q) => q === 'meta[name="veha-mode"]' ? {{content: 'static'}} : null,
  getElementById: () => null,
}};
global.navigator = {{}};
global.MutationObserver = class {{ constructor(fn) {{}} observe() {{}} }};
global.XMLHttpRequest = function () {{}};
global.XMLHttpRequest.prototype.open = function () {{}};
global.fetch = async () => ({{ok: true, json: async () => payload}});
{(STATIC / 'api.js').read_text(encoding='utf-8')}
(async () => {{
  const first = await vehaApi.log('main');
  const previous = await vehaApi.log('main', 400, first.start);
  const merged = previous.lines.concat(first.lines);
  process.stdout.write(JSON.stringify({{
    hasMore: first.hasMore,
    same: JSON.stringify(previous) === JSON.stringify(first),
    unique: new Set(merged).size,
    count: merged.length,
  }}));
}})().catch((e) => {{ console.error(e); process.exitCode = 1; }});
""", encoding="utf-8")
    run = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"hasMore": False, "same": True, "unique": 400, "count": 800}
