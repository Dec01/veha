"""Детерминированные offline-проверки браузера, без запуска тяжёлого браузера."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from veha.viz.app import STATIC

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="нет node — offline JS не исполнить")


def _run(tmp_path, name: str, script: str) -> str:
    path = tmp_path / name
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([NODE, str(path)], check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_json_http_error_and_static_run_selection_are_explicit(tmp_path):
    api = (STATIC / "api.js").read_text(encoding="utf-8")
    dock = (STATIC / "dock.js").read_text(encoding="utf-8")
    run_bar = "function runBar()" + _between(dock, "function runBar()", "/* Половина «Падения»")
    script = f"""
const assert = require('node:assert/strict');
global.window = global;
global.document = {{
  documentElement: {{}},
  querySelector: () => null,
  getElementById: () => null,
}};
global.navigator = {{}};
global.MutationObserver = class {{ constructor(fn) {{}} observe() {{}} }};
global.XMLHttpRequest = function () {{}};
global.XMLHttpRequest.prototype.open = function () {{}};
global.fetch = async () => ({{ok: false, status: 404,
  json: async () => ({{detail: 'сохранённый прогон не найден'}})}});
{api}
(async () => {{
  await assert.rejects(vehaApi.tests('', '', 400, 'missing'),
    /HTTP 404 — сохранённый прогон не найден/);

  document.querySelector = (q) => q === 'meta[name="veha-mode"]'
    ? {{content: 'static'}} : null;
  {api}
  await assert.rejects(vehaApi.graph(false, 'not-exported'), /один прогон/);
  await assert.rejects(vehaApi.tests('', '', 400, 'not-exported'), /один прогон/);
  assert.throws(() => vehaApi.attachmentImage('same.png', 'not-exported'), /один прогон/);

  const esc = (s) => String(s ?? '');
  const num = (v) => Number(v);
  const ms = (v) => String(v);
  const g = {{run: {{runKey: 'ui'}}, savedRuns: [{{key: 'api'}}, {{key: 'ui'}}]}};
  const RS = {{run: ''}};
  {run_bar}
  const html = runBar();
  assert(!html.includes('data-run-select'));
  assert(html.includes('Статический снимок содержит один прогон'));
  process.stdout.write('ok');
}})().catch((e) => {{ console.error(e); process.exitCode = 1; }});
"""
    assert _run(tmp_path, "api-errors.js", script) == "ok"


def test_deferred_filters_attachments_and_404_preserve_current_state(tmp_path):
    dock = (STATIC / "dock.js").read_text(encoding="utf-8")
    state = "const RS =" + _between(
        dock, "const RS =", "/* ------------------------------------------------------------------- live */")
    script = f"""
const assert = require('node:assert/strict');
global.window = global;
let g = {{marker: 'old graph'}};
let tab = 'run';
function render() {{}}
const esc = (s) => String(s ?? '');
{state}
(async () => {{
  const pending = [];
  vehaApi = {{
    tests(status, q, limit, run) {{
      return new Promise((resolve) => pending.push({{status, q, run, resolve}}));
    }},
  }};
  RS.status = ''; RS.q = 'first'; RS.run = 'run-a';
  const oldTests = loadTests();
  RS.status = 'failed'; RS.q = 'second'; RS.run = 'run-b';
  const newTests = loadTests();
  assert.deepEqual(pending.map((p) => [p.status, p.q, p.run]), [
    ['', 'first', 'run-a'], ['failed', 'second', 'run-b']]);
  pending[1].resolve({{tests: [{{file: 'new.json'}}], total: 1}});
  await newTests;
  pending[0].resolve({{tests: [{{file: 'old.json'}}], total: 1}});
  await oldTests;
  assert.deepEqual(RS.tests, [{{file: 'new.json'}}]);

  // graph уже переключил шапку на B, а tests упал: список A не должен остаться.
  RS.tests = [{{file: 'a.json'}}]; RS.total = 1; RS.run = 'run-a';
  vehaApi.graph = async () => ({{run: {{runKey: 'run-b'}}}});
  await selectRun('run-b');
  assert.equal(RS.run, 'run-b');
  assert.equal(g.run.runKey, 'run-b');
  assert.deepEqual(RS.tests, []);
  assert.equal(RS.total, 0);
  let requestedRun = '';
  vehaApi.tests = async (status, q, limit, run) => {{
    requestedRun = run;
    return {{tests: [{{file: 'b.json'}}], total: 1}};
  }};
  await loadTests();
  assert.equal(requestedRun, 'run-b');
  assert.equal(RS.run, g.run.runKey);
  assert.deepEqual(RS.tests, [{{file: 'b.json'}}]);
  vehaApi.tests = async () => {{ throw new Error('HTTP 500 — raw unavailable'); }};
  await loadTests();
  assert.deepEqual(RS.tests, []);
  assert.equal(RS.total, 0);
  assert.match(RS.error, /список тестов не прочитан.*HTTP 500/);

  const oldGraph = g;
  const oldDetail = {{test: {{name: 'keep'}}}};
  RS.detail = oldDetail; RS.open = 'old.json'; RS.run = 'run-b';
  vehaApi.graph = async () => {{ throw new Error('HTTP 404 — нет прогона'); }};
  await selectRun('missing');
  assert.equal(g, oldGraph);
  assert.equal(RS.detail, oldDetail);
  assert.equal(RS.run, 'run-b');
  assert.match(RS.error, /прогон не открыт.*HTTP 404/);

  vehaApi.test = async () => {{ throw new Error('HTTP 404 — нет теста'); }};
  await openTest('missing.json');
  assert.equal(RS.detail, oldDetail);
  assert.match(RS.error, /тест не открыт.*HTTP 404/);
  await openTest('requirement.json', true);
  assert.equal(RS.detail, oldDetail);
  assert.match(RS.error, /Тест из требования недоступен/);

  const attachments = [];
  vehaApi.attachment = (source, run) => new Promise((resolve) =>
    attachments.push({{source, run, resolve}}));
  RS.open = 'test-a'; RS.run = 'run-a';
  const oldAttachment = openAttachment('old.txt', 'old');
  RS.open = 'test-b'; RS.run = 'run-b';
  const newAttachment = openAttachment('new.txt', 'new');
  attachments[1].resolve({{text: 'new attachment', size: 3}});
  await newAttachment;
  attachments[0].resolve({{text: 'old attachment', size: 3}});
  await oldAttachment;
  assert.equal(RS.att.text, 'new attachment');
  assert.equal(RS.attName, 'new');
  assert.deepEqual(attachments.map((a) => [a.source, a.run]), [
    ['old.txt', 'run-a'], ['new.txt', 'run-b']]);
  process.stdout.write('ok');
}})().catch((e) => {{ console.error(e); process.exitCode = 1; }});
"""
    assert _run(tmp_path, "run-state.js", script) == "ok"


def test_requirement_navigation_marks_incompatible_test_explicitly():
    dock = (STATIC / "dock.js").read_text(encoding="utf-8")
    listener = dock.split("dockBody.querySelectorAll('[data-req-test]')", 1)[1]
    assert "openTest(el.dataset.reqTest, true);" in listener
    assert "Тест из требования недоступен в выбранном сохранённом прогоне" in dock
    assert 'class="warn" role="alert">${esc(RS.error)}' in dock


def test_runner_scope_from_api_drives_both_badge_and_filter(tmp_path):
    dock = (STATIC / "dock.js").read_text(encoding="utf-8")
    runner_view = "const RN =" + _between(dock, "const RN =", "/* Одна строка о записи истории")
    script = f"""
const assert = require('node:assert/strict');
global.window = global;
window.vehaApi = {{isStatic: false, OFFLINE: 'offline', offline: () => ''}};
const esc = (s) => String(s ?? '');
const num = (v) => Number(v);
const ms = (v) => String(v);
const pct = (v) => String(v);
const historyLine = () => '';
const trendLine = () => '';
const loadRunner = () => {{}};
{runner_view}
RN.loaded = true;
RN.suites = [
  {{key: 'browser', label: 'Browser', args: ['tests/ui'], scope: 'ui', browser: true}},
  {{key: 'api', label: 'API', args: ['tests/api'], scope: 'api', browser: false}},
];
RN.mapScope = 'ui';
const ui = viewRunner();
assert(ui.includes('Browser'));
assert(ui.includes('<span class="chip">UI</span>'));
assert(!ui.includes('data-run="api"'));
assert(ui.includes('1 из 2'));
RN.mapScope = 'api';
const api = viewRunner();
assert(!api.includes('data-run="browser"'));
assert(api.includes('data-run="api"'));
assert(api.includes('<span class="chip">API</span>'));
process.stdout.write('ok');
"""
    assert _run(tmp_path, "runner-scope.js", script) == "ok"


def test_refresh_keeps_selected_run_and_does_not_fallback_when_it_disappears(tmp_path):
    atlas = (STATIC / "atlas.js").read_text(encoding="utf-8")
    dock = (STATIC / "dock.js").read_text(encoding="utf-8")
    # Автозагрузку отключаем, а настоящие refresh-обработчик и IIFE dock исполняем целиком.
    # Доступ к RS нужен только для наблюдения за состоянием; atlas его не получает.
    atlas = atlas.replace("load(false);", "")
    dock = dock.rsplit("}());", 1)[0] + "window.__dockTest = { RS };\n}());"
    script = f"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const calls = [];
const graph = {{
  nodes: [], edges: [], edgeSets: {{}}, screens: [], domains: [], layers: [],
  run: {{runKey: 'run-a', failures: []}}, savedRuns: [], coverage: {{summary: {{}}}}, meta: {{}},
  artifacts: {{artifacts: []}}, collected: {{}},
}};
const elements = new Map();
function element(id) {{
  if (elements.has(id)) return elements.get(id);
  const listeners = {{}};
  const el = {{
    id, dataset: {{}}, style: {{}}, listeners, clientWidth: 1200, clientHeight: 800,
    innerHTML: '', textContent: '', value: '', hidden: false,
    classList: {{add() {{}}, remove() {{}}, toggle() {{}}, contains() {{ return false; }}}},
    addEventListener(type, fn) {{ (listeners[type] ||= []).push(fn); }},
    querySelectorAll() {{ return []; }}, querySelector() {{ return null; }},
    setAttribute() {{}}, removeAttribute() {{}}, contains() {{ return false; }},
    getContext() {{ return {{}}; }}, focus() {{}}, blur() {{}},
  }};
  elements.set(id, el);
  return el;
}}
const documentListeners = {{}};
const document = {{
  documentElement: {{}}, activeElement: {{tagName: 'BODY'}},
  getElementById: element, querySelectorAll() {{ return []; }},
  addEventListener(type, fn) {{ (documentListeners[type] ||= []).push(fn); }},
  dispatchEvent(event) {{ (documentListeners[event.type] || []).forEach((fn) => fn(event)); }},
}};
const context = {{
  console, document, setTimeout, clearTimeout,
  CustomEvent: class {{ constructor(type, init) {{ this.type = type; this.detail = init.detail; }} }},
  getComputedStyle: () => ({{getPropertyValue: () => ''}}),
  matchMedia: () => ({{matches: false, addEventListener() {{}}}}),
  Map2D: new Proxy({{cov: () => 'none'}}, {{get(target, key) {{
    return key in target ? target[key] : () => {{}};
  }}}}),
  vehaApi: {{
    isStatic: false,
    graph: async (refresh, run) => {{ calls.push([refresh, run]); return graph; }},
    coverageAverage: () => ({{value: null, reason: ''}}),
    offline: () => '',
  }},
}};
context.window = context;
context.addEventListener = () => {{}};
vm.createContext(context);
vm.runInContext({atlas!r}, context, {{filename: 'atlas.js'}});
vm.runInContext({dock!r}, context, {{filename: 'dock.js'}});
(async () => {{
  const RS = context.__dockTest.RS;
  RS.run = 'run-a';
  const refresh = elements.get('refresh').listeners.click[0];
  const target = elements.get('refresh');
  await refresh({{target}});
  assert.deepEqual(calls, [[true, 'run-a']]);
  const old = context.atlas.graph;
  context.vehaApi.graph = async (refresh, run) => {{
    calls.push([refresh, run]);
    throw new Error('HTTP 404 — сохранённый прогон удалён');
  }};
  await refresh({{target}});
  assert.deepEqual(calls[1], [true, 'run-a']);
  assert.equal(context.atlas.graph, old);
  assert.equal(RS.run, 'run-a');
  assert.match(RS.error, /данные не обновлены: Error: HTTP 404/);
  assert(elements.get('dock-body').innerHTML.includes('данные не обновлены'));
  assert(target.disabled === false);
  process.stdout.write('ok');
}})().catch((e) => {{ console.error(e); process.exitCode = 1; }});
"""
    assert _run(tmp_path, "refresh-run.js", script) == "ok"
    assert "RS.error" not in atlas and "openTab(" not in atlas



def test_live_browser_pins_mrs_and_log_to_containers_generation(tmp_path):
    dock = (STATIC / "dock.js").read_text(encoding="utf-8")
    live_state = "const LV =" + _between(dock, "const LV =", "function viewLive()")
    load_live = "async function loadLive()" + _between(
        dock, "async function loadLive()", "function scheduleLive()")
    load_log = "async function loadContainerLog(cid)" + _between(
        dock, "async function loadContainerLog(cid)",
        "/* ---------------------------------------------------------------- раннер */")
    script = f"""
const assert = require('node:assert/strict');
global.window = global;
const calls = [];
const rendered = [];
let tab = 'live';
const AT = () => ({{setContainerState: (c) => rendered.push(c.generation)}});
const render = () => {{}};
const scheduleLive = () => {{}};
window.vehaApi = {{
  isStatic: false,
  liveContainers: async () => {{
    calls.push(['containers', undefined]);
    return {{generation: 'gold', containers: [{{id: 'api'}}]}};
  }},
  liveMrs: async (generation) => {{
    calls.push(['mrs', generation]);
    return {{generation, mrs: []}};
  }},
  liveContainerLog: async (cid, generation) => {{
    calls.push(['log', cid, generation]);
    return {{generation, lines: ['old']}};
  }},
}};
{live_state}
LV.logs = {{lines: ['stale']}};
LV.logOf = 'old-container';
{load_live}
{load_log}
(async () => {{
  await loadLive();
  assert.deepEqual(calls, [['containers', undefined], ['mrs', 'gold']]);
  assert.deepEqual(rendered, ['gold']);
  assert.equal(LV.generation, 'gold');
  assert.equal(LV.mrs.generation, 'gold');
  assert.equal(LV.logs, null);
  assert.equal(LV.logOf, '');

  await loadContainerLog('api');
  assert.deepEqual(calls[2], ['log', 'api', 'gold']);
  assert.equal(LV.logs.generation, 'gold');
  assert.deepEqual(LV.logs.lines, ['old']);
  process.stdout.write('ok');
}})().catch((e) => {{ console.error(e); process.exitCode = 1; }});
"""
    assert _run(tmp_path, "live-generation.js", script) == "ok"
