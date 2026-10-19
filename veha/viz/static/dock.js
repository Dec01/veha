/* Разделы Вехи: Покрытие, Система, Требования, Прогон — страницы рядом с картой.
 *
 * Числа не пересчитываем — берём готовые срезы coverage.json (те же, что в
 * coverage_report.html) и сводку прогона из allure-results. Раздел связан с
 * картой: фильтр таблицы гасит узлы на карте, клик по строке открывает узел
 * на карте с его цепочкой.
 */
'use strict';

// Обёртка обязательна: atlas.js держит в глобальной области `esc` и `CSS`
// (последняя перекрывает штатный window.CSS), иначе скрипт падает на загрузке.
(function () {
const AT = () => window.atlas;
const dockBody = document.getElementById('dock-body');
const dockNote = document.getElementById('dock-note');

let g = null;
let tab = 'overview';
let section = 'map';       // map — карта; иначе раздел-страница
/* История переходов между вкладками: из «Обзора» кликом уходят в «Эндпоинты»,
 * из требования — в «Отчёт», и вернуться было некуда. */
const trail = [];
let selNode = '';          // что выделено на карте — подсвечиваем строку
let sortKey = 'coverage', sortDir = 1;
const F = { q: '', method: '', service: '', subtype: '', status: '', missing: '', run: '' };

/* Пять разделов в шапке: карта и четыре страницы, у каждой свои подразделы.
 * Подраздел — вид со своей разметкой (view*), раздел только выбирает, какой показать. */
const SECTIONS = [
  ['cov', 'Покрытие', [['overview', 'Обзор'], ['endpoints', 'Эндпоинты'], ['matrix', 'Методика']]],
  ['sys', 'Система', [['links', 'Связи'], ['schema', 'Схема БД'], ['services', 'Сервисы']]],
  ['req', 'Требования', [['specs', 'Требования'], ['contract', 'Контракт']]],
  ['run', 'Прогон', [['run', 'Прогон'], ['runner', 'Раннер'], ['log', 'Лог'], ['live', 'Live']]],
];
const TABS = SECTIONS.flatMap(([, , tabs]) => tabs);
const sectionOf = (t) => (SECTIONS.find(([, , tabs]) => tabs.some(([k]) => k === t)) || ['cov'])[0];
const lastTab = { cov: 'overview', sys: 'links', req: 'specs', run: 'run' };
// для палитры команд: разделы и подразделы
window.vehaSections = [['map', 'Карта']].concat(
  SECTIONS.flatMap(([, label, tabs]) => tabs.map(([k, l]) => [k, l === label ? l : `${label} → ${l}`])));

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
/* Число из данных — только число: всё, что пришло строкой с разметкой, становится
 * пустотой, а не HTML. */
const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : ''; };
/* Ссылка наружу — только http(s): `javascript:` в href из чужого снимка выполнился
 * бы в контексте вебморды. */
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || '')) ? String(u) : '#');
const pct = (v) => (v == null || num(v) === '' ? '—' : `${num(v)}%`);
const covColor = (st) => (Object.hasOwn(COV_VAR, st) ? COV_VAR[st] : 'var(--unseen)');
const COV_VAR = { full: 'var(--ok)', partial: 'var(--part)',
  empty: 'var(--line-2)' };
const ms = (v) => (v == null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)} с` : `${Math.round(v)} мс`);

/* ------------------------------------------------------------------ данные */
function endpoints() {
  return g.nodes.filter((n) => n.layer === 'api');
}

/* Один предикат на всё: и таблица, и сцена показывают один и тот же срез. */
function matches(n) {
  const m = n.meta || {};
  if (F.q) {
    const hay = `${n.label} ${m.service || ''} ${m.subtype || ''} ${(m.missing || []).join(' ')}`;
    if (!hay.toLowerCase().includes(F.q.toLowerCase())) return false;
  }
  if (F.method && m.method !== F.method) return false;
  if (F.service && (m.service || '—') !== F.service) return false;
  if (F.subtype && (m.subtype || '') !== F.subtype) return false;
  if (F.status && (m.status || 'unseen') !== F.status) return false;
  if (F.missing && !(m.missing || []).includes(F.missing)) return false;
  if (F.run && (m.runStatus || 'none') !== F.run) return false;
  return true;
}

function filtered() {
  const rows = endpoints().filter(matches);
  const dir = sortDir;
  rows.sort((a, b) => {
    const va = pick(a, sortKey), vb = pick(b, sortKey);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return (typeof va === 'string' ? va.localeCompare(vb) : va - vb) * dir;
  });
  return rows;
}
function pick(n, key) {
  const m = n.meta || {};
  return { path: n.label, method: m.method, service: m.service, subtype: m.subtype,
    coverage: m.coverage, calls: m.calls, missing: (m.missing || []).length,
    p95: (m.timing || {}).msP95, fails: m.runFailed }[key];
}

function applyFilter() {
  const anyFilter = Object.values(F).some(Boolean);
  AT().setFilter(anyFilter ? matches : null);
  const total = endpoints().length, shown = endpoints().filter(matches).length;
  dockNote.textContent = anyFilter ? `фильтр: ${shown} из ${total}` : `${total} эндпоинтов`;
  renderActiveFilters();
  render();
}

/* Активные фильтры собраны в одном месте и снимаются по клику: иначе легко
 * забыть про включённый срез и удивляться, куда делись узлы. */
const F_LABEL = { q: 'поиск', method: 'метод', service: 'сервис', subtype: 'подтип',
                  status: 'покрытие', missing: 'нет TC', run: 'прогон' };
function renderActiveFilters() {
  const box = document.getElementById('active-filters');
  if (!box) return;
  const on = Object.entries(F).filter(([, v]) => v);
  if (!on.length) { box.innerHTML = '<span class="muted">фильтров нет</span>'; return; }
  box.innerHTML = on.map(([k, v]) =>
    `<button class="chip on" data-drop="${k}">${F_LABEL[k] || k}: ${esc(v)} ×</button>`
  ).join('') + '<button class="chip" data-drop="*">снять всё</button>';
  box.querySelectorAll('[data-drop]').forEach((el) => el.addEventListener('click', () => {
    if (el.dataset.drop === '*') Object.keys(F).forEach((k) => (F[k] = ''));
    else F[el.dataset.drop] = '';
    applyFilter();
  }));
}

/* ------------------------------------------------------------------- обзор */
function viewOverview() {
  const s = (g.coverage || {}).summary || {};
  const runs = (g.coverage || {}).runs || {};
  const r = g.run || {};
  // `summary.avg: null` — отчёт ничего не посчитал (partest 2.7.0 при `endpoints: 0`):
  // среднего нет, и разницы с прошлым прогоном тоже
  // (`num(null)` — 0, поэтому null проверяется отдельно)
  const noAvg = s.avg == null || num(s.avg) === '';
  const d = noAvg || runs.deltaCoverage == null || num(runs.deltaCoverage) === '' ? null
    : num(runs.deltaCoverage);
  const passRate = r.total ? Math.round((r.passed / r.total) * 100) : null;

  const kpi = (label, value, sub, color) => `<div class="kpi">
    <div class="kv-label">${esc(label)}</div>
    <div class="kv-value" ${color ? `data-css="color:${color}"` : ''}>${value}</div>
    <div class="kv-sub">${sub || ''}</div></div>`;

  const stack = [['full', num(s.full) || 0, 'полное'],
                 ['partial', num(s.partial) || 0, 'частичное'],
                 ['empty', num(s.empty) || 0, 'пустое']];
  const stackTotal = stack.reduce((a, x) => a + (x[1] || 0), 0) || 1;

  const warn = r.runsFound > 1 ? `<div class="warn">
      В <code>allure-results</code> лежит ${num(r.runsFound)} прогона — показан только
      последний. Чтобы цифры были однозначными, чистите папку перед запуском:
      <code>rm -rf allure-results</code>.
    </div>` : '';

  return warn + `<div class="grid-kpi">
    ${kpi('Среднее покрытие', pct(s.avg), noAvg && s.avg === null ? 'не посчитано'
      : d == null ? 'по методике'
      : `${d > 0 ? '▲' : '▼'} ${Math.abs(d)} п.п. к прошлому прогону`,
      d == null || d < 0 ? null : 'var(--ok)')}
    ${kpi('Эндпоинтов в отчёте', s.endpoints == null ? '—' : num(s.endpoints),
      `${num(g.meta.endpoints)} узлов на карте`)}
    ${kpi('Прогон', r.total ? `${num(r.passed)}/${num(r.total)}` : '—',
      passRate == null ? 'нет allure-results' : `${passRate}% зелёных`,
      passRate == null ? null : (passRate === 100 ? 'var(--ok)' : 'var(--warn)'))}
    ${kpi('Упало', (num(r.failed) || 0) + (num(r.broken) || 0),
      r.skipped ? `+${num(r.skipped)} пропущено` : 'failed + broken',
      (r.failed || r.broken) ? 'var(--bad)' : 'var(--ok)')}
    ${kpi('Время прогона', ms(r.wallMs), `сумма тестов ${ms(r.sumMs)}`)}
    ${kpi('Отклик API', ms(s.msAvg), `p95 ${ms(s.msP95)} · макс ${ms(s.msMax)}`)}
  </div>

  <div class="cols">
    <section class="win">
      <h3>Покрытие по статусам</h3>
      <div class="stack">${stack.map(([k, v]) =>
        `<i data-css="width:${((v || 0) / stackTotal) * 100}%;background:${covColor(k)}"
            title="${esc(k)}: ${v || 0}"></i>`).join('')}</div>
      <div class="legend-row">${stack.map(([k, v, label]) =>
        `<span><i data-css="background:${covColor(k)}"></i>${label} <b>${v || 0}</b></span>`).join('')}</div>
      <div class="hint-row">Клик по статусу фильтрует и таблицу, и сцену</div>
      <div class="chips">${stack.map(([k, , label]) =>
        `<button class="chip" data-set="status" data-val="${k}">${label}</button>`).join('')}
        <button class="chip" data-set="status" data-val="">все</button></div>
    </section>

    <section class="win">
      <h3>Чего не хватает чаще всего (ось B)</h3>
      <table class="mini"><tbody>${((g.coverage || {}).missingTop || []).slice(0, 8)
        .map((x) => `<tr class="click" data-set="missing" data-val="${esc(x.tc)}">
          <td>${esc(x.tc)}</td><td class="num">${num(x.count)}</td>
          <td class="barcell"><i data-css="width:${Math.min(100, (num(x.count) || 0) * 4)}%"></i></td></tr>`)
        .join('') || '<tr><td class="muted">нет данных</td></tr>'}</tbody></table>
    </section>

    <section class="win">
      <h3>Где болит в прогоне</h3>
      <table class="mini"><tbody>${(r.failuresByFeature || []).slice(0, 8)
        .map(([name, cnt]) => `<tr class="click" data-goto-feature="${esc(name)}">
          <td>${esc(name)}</td><td class="num bad">${num(cnt)}</td>
          <td class="barcell"><i class="bad" data-css="width:${Math.min(100, (num(cnt) || 0) * 1.5)}%"></i></td></tr>`)
        .join('') || '<tr><td class="muted">падений нет</td></tr>'}</tbody></table>
    </section>
  </div>`;
}

/* -------------------------------------------------------------- эндпоинты */
const GROUP_BY = [['', 'без группировки'], ['service', 'по сервису'],
                  ['domain', 'по домену'], ['method', 'по методу'],
                  ['status', 'по покрытию'], ['runStatus', 'по прогону']];
let groupBy = '';
const collapsed = new Set();

function groupKey(n) {
  const m = n.meta || {};
  const dl = ((g && g.domains) || []).find((d) => d.key === m.domain);
  const domain = !m.domain || m.domain === 'other' ? 'Прочее' : (dl ? dl.label : m.domain);
  return ({ service: m.service, domain, method: m.method,
            status: m.status, runStatus: m.runStatus }[groupBy]) || '—';
}

function viewEndpoints() {
  const rows = filtered();
  const cols = [['method', 'Метод'], ['path', 'Путь'], ['service', 'Сервис'],
    ['subtype', 'Подтип (ось A)'], ['coverage', 'Покрытие'], ['missing', 'Нет TC'],
    ['calls', 'Вызовов'], ['p95', 'p95'], ['fails', 'Падений']];
  const head = `<thead><tr>${cols.map(([k, l]) =>
      `<th data-sort="${k}" class="${sortKey === k ? 'on' : ''}">${l}${
        sortKey === k ? (sortDir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr></thead>`;

  if (groupBy) {
    const groups = {};
    rows.forEach((n) => (groups[groupKey(n)] ||= []).push(n));
    const keys = Object.keys(groups).sort((a, b) => groups[b].length - groups[a].length);
    return filterBar() + groupBar() + `<div class="tbl-wrap"><table class="tbl">${head}
      <tbody>${keys.map((k) => {
        const open = !collapsed.has(k);
        return `<tr class="grp" data-grp="${esc(k)}">
            <td colspan="${cols.length}">
              <span class="caret">${open ? '▾' : '▸'}</span> ${esc(k)}
              <span class="chip">${groups[k].length}</span>
              <span class="muted">${avgCoverage(groups[k])}</span>
            </td></tr>`
          + (open ? groups[k].slice(0, 120).map(rowHtml).join('') : '');
      }).join('')}</tbody></table></div>`;
  }

  return filterBar() + groupBar() + `
    <div class="tbl-wrap">
      <table class="tbl">
        ${head}
        <tbody>${rows.slice(0, 400).map((n) => {
          const m = n.meta || {};
          return `<tr class="click" data-node="${esc(n.id)}">
            <td><span class="m m-${esc((m.method || '').toLowerCase())}">${esc(m.method || '')}</span></td>
            <td class="path">${esc(m.path || n.label)}</td>
            <td>${esc(m.service || '—')}</td>
            <td class="muted">${esc(m.subtype || '—')}</td>
            <td><span class="dotc" data-css="background:${covColor(m.status)}"></span>${pct(m.coverage)}</td>
            <td>${(m.missing || []).length
              ? `<span class="chip">${(m.missing || []).length}</span>` : ''}</td>
            <td class="num">${m.calls == null ? '' : num(m.calls)}</td>
            <td class="num">${num((m.timing || {}).msP95 ?? '')}</td>
            <td class="num">${m.runFailed ? `<span class="chip bad">${num(m.runFailed)}</span>` : ''}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
      ${rows.length > 400 ? `<div class="muted pad">показаны первые 400 из ${rows.length}</div>` : ''}
      ${rows.length ? '' : '<div class="muted pad">под фильтр ничего не подошло</div>'}
    </div>`;
}

function groupBar() {
  return `<div class="chips pad-b">
    <span class="muted" data-css="font-size:12px;margin-right:4px">Группировать:</span>
    ${GROUP_BY.map(([k, l]) => `<button class="chip ${groupBy === k ? 'on' : ''}"
      data-grpby="${k}">${l}</button>`).join('')}</div>`;
}

function avgCoverage(list) {
  const vals = list.map((n) => n.meta.coverage).filter((v) => v != null)
    .map(Number).filter(Number.isFinite);
  if (!vals.length) return '';
  return `покрытие ${(vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(1)}%`;
}

function rowHtml(n) {
  const m = n.meta || {};
  return `<tr class="click" data-node="${esc(n.id)}">
    <td><span class="m m-${esc((m.method || '').toLowerCase())}">${esc(m.method || '')}</span></td>
    <td class="path">${esc(m.path || n.label)}</td>
    <td>${esc(m.service || '—')}</td>
    <td class="muted">${esc(m.subtype || '—')}</td>
    <td><span class="dotc" data-css="background:${covColor(m.status)}"></span>${pct(m.coverage)}</td>
    <td>${(m.missing || []).length ? `<span class="chip">${(m.missing || []).length}</span>` : ''}</td>
    <td class="num">${m.calls == null ? '' : num(m.calls)}</td>
    <td class="num">${num((m.timing || {}).msP95 ?? '')}</td>
    <td class="num">${m.runFailed ? `<span class="chip bad">${num(m.runFailed)}</span>` : ''}</td>
  </tr>`;
}

function filterBar() {
  const uniq = (fn) => [...new Set(endpoints().map(fn).filter(Boolean))].sort();
  const sel = (key, label, values) => `<select data-f="${key}">
    <option value="">${label}</option>
    ${values.map((v) => `<option value="${esc(v)}" ${F[key] === v ? 'selected' : ''}>${esc(v)}</option>`).join('')}
  </select>`;
  const allMissing = [...new Set(endpoints().flatMap((n) => n.meta.missing || []))].sort();
  return `<div class="filters">
    <input type="search" data-f="q" value="${esc(F.q)}" placeholder="путь, сервис, подтип, missing…">
    ${sel('method', 'метод', uniq((n) => n.meta.method))}
    ${sel('service', 'сервис', uniq((n) => n.meta.service))}
    ${sel('subtype', 'подтип', uniq((n) => n.meta.subtype))}
    ${sel('status', 'покрытие', ['full', 'partial', 'empty', 'unseen'])}
    ${sel('missing', 'нет TC', allMissing)}
    ${sel('run', 'прогон', ['passed', 'failed', 'none'])}
    <button class="act" id="f-clear">Сбросить</button>
  </div>`;
}

/* ----------------------------------------------------------------- матрица */
/* Вкладка «Методика»: два взгляда на одни и те же данные оси A × оси B —
 * сетка и списки пробелов. Раньше это были вкладки «Матрица» и «Пробелы»:
 * разными они не были, просто разной подробности. */
let mxMode = 'grid';           // grid | gaps
function viewMethod() {
  const sw = `<div class="chips pad-b">
      <button class="chip ${mxMode === 'grid' ? 'on' : ''}" data-mxmode="grid">Матрица</button>
      <button class="chip ${mxMode === 'gaps' ? 'on' : ''}" data-mxmode="gaps">Пробелы</button>
    </div>`;
  return sw + (mxMode === 'gaps' ? viewGaps() : viewMatrix());
}

function viewMatrix() {
  const rows = filtered();
  const tcs = [...new Set(endpoints().flatMap(
    (n) => [...(n.meta.executed || []), ...(n.meta.missing || [])]))].sort();
  if (!tcs.length) return '<div class="muted pad">нет coverage.json — матрицу строить не из чего</div>';
  return filterBar() + `
    <div class="tbl-wrap">
      <table class="tbl matrix">
        <thead><tr><th class="sticky">Эндпоинт</th>
          ${tcs.map((t) => `<th class="rot"><span>${esc(t.replace(/^Request/, ''))}</span></th>`).join('')}
        </tr></thead>
        <tbody>${rows.slice(0, 250).map((n) => {
          const m = n.meta || {};
          const ex = new Set(m.executed || []), mi = new Set(m.missing || []);
          return `<tr class="click" data-node="${esc(n.id)}">
            <td class="sticky path"><span class="m m-${esc((m.method || '').toLowerCase())}">${
              esc(m.method || '')}</span> ${esc(m.path || n.label)}</td>
            ${tcs.map((t) => `<td class="cell ${ex.has(t) ? 'ok' : mi.has(t) ? 'no' : 'na'}"
              title="${esc(t)}">${ex.has(t) ? '✓' : mi.has(t) ? '✕' : ''}</td>`).join('')}
          </tr>`;
        }).join('')}</tbody>
      </table>
      ${rows.length > 250 ? `<div class="muted pad">показаны первые 250 из ${rows.length}</div>` : ''}
    </div>`;
}

/* ------------------------------------------------------------------ прогон */
let runFeature = '';
/* Шапка прогона: одна на обе половины вкладки — падения и полный отчёт
 * говорят про один и тот же последний прогон, и цифры у них общие. */
function runBar() {
  const r = g.run;
  if (!r) return '';
  const when = r.startedAt ? new Date(r.startedAt).toLocaleString('ru-RU') : '—';
  const warn = r.runsFound > 1 ? `<div class="warn">
      В папке ${num(r.runsFound)} прогона — показан только последний.</div>` : '';
  return warn + `<div class="runbar">
      <span class="stat">начало <b>${esc(when)}</b></span>
      <span class="stat">длительность <b>${ms(r.wallMs)}</b></span>
      <span class="stat ok">passed <b>${num(r.passed)}</b></span>
      <span class="stat bad">failed <b>${num(r.failed)}</b></span>
      ${r.broken ? `<span class="stat bad">broken <b>${num(r.broken)}</b></span>` : ''}
      ${r.skipped ? `<span class="stat">skipped <b>${num(r.skipped)}</b></span>` : ''}
    </div>`;
}

/* Половина «Падения»: что упало и почему. */
function failsBody() {
  const r = g.run;
  if (!r) return '<div class="muted pad">в allure-results нет результатов</div>';
  const feats = r.failuresByFeature || [];
  const list = r.failures.filter((t) => !runFeature || (t.feature || t.parentSuite) === runFeature);
  return `<div class="chips pad-b">
      <button class="chip ${runFeature ? '' : 'on'}" data-feat="">все падения</button>
      ${feats.map(([n, c]) => `<button class="chip ${runFeature === n ? 'on' : ''}"
        data-feat="${esc(n)}">${esc(n)} <b>${num(c)}</b></button>`).join('')}
    </div>
    <div class="fails">${list.slice(0, 120).map((t) => `
      <div class="fail">
        <div class="fail-h">
          <span class="chip bad">${esc(t.status)}</span>
          <span class="fail-n">${esc(t.name)}</span>
          <span class="muted">${esc(t.feature || '')}</span>
        </div>
        <div class="nodeid">${esc(t.nodeid)}</div>
        <div class="err">${esc((t.error || {}).head || '')}</div>
        ${(t.error || {}).body ? `<pre class="errb">${esc(t.error.body)}</pre>` : ''}
        <div class="row">
          ${rerunButton(t.nodeid)}
          <button class="act sm" data-trace="${esc(t.nodeid)}">Трейс</button>
        </div>
        <pre class="out hidden" data-trace-for="${esc(t.nodeid)}">${esc((t.error || {}).trace || '')}</pre>
      </div>`).join('') || '<div class="muted pad">в этой группе падений нет</div>'}
    </div>
    <section class="win">
      <h3>Самые долгие тесты</h3>
      <table class="mini"><tbody>${(r.slowest || []).slice(0, 10).map((t) => `
        <tr><td>${esc(t.name)}</td><td class="num">${ms(t.ms)}</td></tr>`).join('')}</tbody></table>
    </section>`;
}

/* ------------------------------------------------------------------ связи */
/* Связи списком: структура и выбранный здесь набор сходства (`linkSet`); видно
 * вес связи и обе стороны. На карте сходства нет — только здесь. */
let linkKind = '';
/* Выведенные связи — сходство, а не поток: на карте их нет, здесь они рядом со
 * структурой, и набор выбирается тут же. */
let linkSet = 'coverage';
let linkQ = '';
function viewLinks() {
  const sets = g.edgeSets || {};
  const ds = linkSet;
  const all = (sets.structure || []).concat(ds === 'tests' ? sets.tests || [] : sets.coverage || []);
  const label = (id) => {
    const n = g.nodes.find((x) => x.id === id);
    return n ? n.label : id;
  };
  const KINDS = {
    cotest: 'вызваны в одном тесте', cofeature: 'одна фича в отчёте',
    gap: 'одинаковые пробелы оси B', subtype: 'один подтип оси A',
    calls: 'кнопка → эндпоинт', contains: 'экран содержит',
    publish: 'публикует в топик', consume: 'читает из топика',
    writes: 'пишет в таблицу', reads: 'читает таблицу', storage: 'кладёт в хранилище',
  };
  const counts = {};
  all.forEach((e) => (counts[e.kind] = (counts[e.kind] || 0) + 1));
  const rows = all
    .filter((e) => !linkKind || e.kind === linkKind)
    .filter((e) => !linkQ || (label(e.source) + ' ' + label(e.target)).toLowerCase()
      .includes(linkQ.toLowerCase()))
    .sort((a, b) => (b.w || 0) - (a.w || 0));

  return `<div class="chips pad-b">
      <span class="muted" data-css="font-size:12.5px;margin-right:4px">Кроме структуры — сходство:</span>
      <button class="chip ${ds === 'coverage' ? 'on' : ''}" data-lset="coverage">общие пробелы и подтип</button>
      <button class="chip ${ds === 'tests' ? 'on' : ''}" data-lset="tests">общий тест и фича</button>
    </div>
    <div class="filters">
      <input type="search" data-lq value="${esc(linkQ)}" placeholder="узел в связи…">
      <div class="chips">
        <button class="chip ${linkKind ? '' : 'on'}" data-lk="">все ${all.length}</button>
        ${Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([k, c]) =>
          `<button class="chip ${linkKind === k ? 'on' : ''}" data-lk="${esc(k)}">${
            esc(KINDS[k] || k)} <b>${num(c)}</b></button>`).join('')}
      </div>
    </div>
    <div class="tbl-wrap"><table class="tbl">
      <thead><tr><th>Откуда</th><th>Куда</th><th>Связь</th><th>Вес</th></tr></thead>
      <tbody>${rows.slice(0, 400).map((e) => `
        <tr class="click" data-node="${esc(e.source)}">
          <td class="path">${esc(label(e.source))}</td>
          <td class="path">${esc(label(e.target))}</td>
          <td class="muted">${esc(KINDS[e.kind] || e.kind)}</td>
          <td class="num">${num(e.w) || ''}</td></tr>`).join('')}</tbody>
    </table>
    ${rows.length > 400 ? `<div class="muted pad">первые 400 из ${rows.length}</div>` : ''}
    </div>`;
}

/* ---------------------------------------------------------------- пробелы */
/* Обратный срез матрицы: не «что покрыто у эндпоинта», а «кому не хватает
 * этого тест-кейса». Прямая рабочая очередь. */
function viewGaps() {
  const eps = endpoints();
  const byTc = {};
  eps.forEach((n) => (n.meta.missing || []).forEach((tc) => (byTc[tc] ||= []).push(n)));
  const bySub = {};
  eps.forEach((n) => {
    if (!(n.meta.missing || []).length) return;
    const k = n.meta.subtype || '—';
    (bySub[k] ||= { n: 0, holes: 0 });
    bySub[k].n += 1;
    bySub[k].holes += n.meta.missing.length;
  });
  const list = Object.entries(byTc).sort((a, b) => b[1].length - a[1].length);
  if (!list.length) return '<div class="muted pad">пробелов по методике нет</div>';

  return `<div class="cols">
      <section class="win">
        <h3>Подтипы с дырами (ось A)</h3>
        <table class="mini"><tbody>${Object.entries(bySub)
          .sort((a, b) => b[1].holes - a[1].holes).slice(0, 10)
          .map(([k, v]) => `<tr class="click" data-set="subtype" data-val="${esc(k)}">
            <td>${esc(k)}</td><td class="num">${num(v.n)}</td>
            <td class="num">${num(v.holes)}</td></tr>`).join('')}</tbody></table>
      </section>
      <section class="win">
        <h3>Как читать</h3>
        <div class="hint-row">Слева — сколько эндпоинтов подтипа имеют дыры и сколько
          всего недостающих TC. Ниже — по каждому тест-кейсу список эндпоинтов,
          где он не выполнялся. Клик по строке ведёт к узлу на карте.</div>
      </section>
    </div>
    <div class="gaps">${list.map(([tc, arr]) => `
      <section class="win">
        <h3>${esc(tc)} <span class="chip">${arr.length}</span></h3>
        <div class="chips">${arr.slice(0, 40).map((n) => `
          <button class="chip" data-node="${esc(n.id)}"
            title="${esc(n.meta.service || '')}">${esc(n.meta.path || n.label)}</button>`).join('')}
          ${arr.length > 40 ? `<span class="muted">и ещё ${arr.length - 40}</span>` : ''}
        </div>
      </section>`).join('')}</div>`;
}

/* ---------------------------------------------------------------- контракт */
/* Что объявлено в OpenAPI против того, что видели на прогоне. Ценность именно
 * в разнице: объявленное и не тронутое, вызванное и не объявленное. */
function viewContract() {
  const c = g.contract || {};
  if (!c.operations) {
    return `<div class="pad muted">Контракт не снят: нет артефакта
      <code>contract.openapi</code> агента partest-atlas (файл
      <code>atlas/&lt;система&gt;/&lt;контур&gt;/openapi.json</code>) или он не прочитан —
      причина в <code>/api/artifacts</code>.</div>`;
  }
  const codes = c.codes || {};
  const unused = c.declaredUnused || [];
  const missing = c.missingFromContract || [];
  const byMethod = {};
  missing.forEach((id) => {
    const m = id.slice(4).split(' ')[0];
    (byMethod[m] ||= []).push(id);
  });

  return `<div class="cols">
      <section class="win">
        <h3>Контракт</h3>
        <table class="mini"><tbody>
          <tr><td>операций</td><td class="num">${num(c.operations)}</td></tr>
          <tr><td>объявлены, но за прогон не вызывались</td>
            <td class="num warn">${unused.length}</td></tr>
          <tr><td>вызывались, но не объявлены</td>
            <td class="num bad">${missing.length}</td></tr>
        </tbody></table>
        <div class="req-path">${esc(c.source || '')}</div>
      </section>
      <section class="win">
        <h3>Коды ответов</h3>
        ${codes.systemic ? `<div class="hint-row"><b>Контракт объявляет только
          успешный код.</b> Каждая из ${num(c.operations)} операций несёт ровно один
          код, ошибочных (4xx/5xx) не объявлено нигде. Поэтому «тест ждал код,
          которого нет в контракте» верно почти везде — это один системный
          факт, а не сотня находок, и в списке ниже он не повторяется.</div>`
          : '<div class="hint-row">Коды по контракту:</div>'}
        <div class="chips">${Object.entries(codes.declared || {}).map(([k, v]) =>
          `<span class="chip">${esc(k)} <b>${num(v)}</b></span>`).join('')}</div>
      </section>
      <section class="win">
        <h3>Как читать</h3>
        <div class="hint-row">Описаний в этом контракте нет ни у одной операции
          — Spring их не аннотирует, поэтому «как должно работать» приходит из
          спецификаций аналитики, а не отсюда. Отсюда приходят: контроллер,
          обязательные поля тела, параметры и объявленные коды.</div>
      </section>
    </div>
    ${unused.length ? `<section class="win">
      <h3>Объявлены, но за прогон не вызывались <span class="chip warn">${unused.length}</span></h3>
      <div class="chips">${unused.map((id) =>
        `<button class="chip" data-node="${esc(id)}">${esc(id.slice(4))}</button>`).join('')}</div>
    </section>` : ''}
    <div class="gaps">${Object.entries(byMethod).sort().map(([m, ids]) => `
      <section class="win">
        <h3>Не объявлены в контракте · ${esc(m)} <span class="chip bad">${ids.length}</span></h3>
        <div class="chips">${ids.map((id) =>
          `<button class="chip bad" data-node="${esc(id)}">${esc(id.slice(4 + m.length + 1))}</button>`).join('')}</div>
      </section>`).join('')}</div>`;
}

/* -------------------------------------------------------------- требования */
/* «Как должно работать» из спецификаций аналитики, разобранных сборщиком
 * `specs` агента partest-atlas. Требование связано с узлами карты по якорям
 * (пути API, таблицы, экраны) и с тестами — четырьмя способами, от точного к
 * приблизительному. Способ подписан у каждой связи: догадка не должна
 * выглядеть как факт. */
const SP = { q: '', how: '', src: '', open: '' };

const HOW = {
  direct: ['прямая ссылка', 'ok'],
  anchor: ['по эндпоинту', 'ok'],
  table: ['по таблице, по окну теста с фикстурами', 'warn'],
  feature: ['по названию фичи', 'warn'],
  none: ['не проверено', ''],
};

function viewSpecs() {
  const sp = g.specs || { requirements: [], sources: [] };
  const all = sp.requirements;
  if (!all.length) {
    return `<div class="pad muted">Требования не собраны: нет артефакта
      <code>specs.requirements</code> агента partest-atlas (сборщик разбирает
      спецификации из секции <code>specs</code> в <code>atlas.yaml</code>) или он не
      прочитан — причина в <code>/api/artifacts</code>.</div>`;
  }
  const list = all.filter((r) => {
    if (SP.how && r.how !== SP.how) return false;
    if (SP.src && r.source !== SP.src) return false;
    if (SP.q) {
      const hay = `${r.id} ${r.title} ${r.lead} ${r.path.join(' ')}`.toLowerCase();
      if (!hay.includes(SP.q.toLowerCase())) return false;
    }
    return true;
  });

  const cnt = {};
  all.forEach((r) => (cnt[r.how] = (cnt[r.how] || 0) + 1));
  const sources = [...new Set(all.map((r) => r.source))];
  const missing = [...new Set(all.flatMap((r) => r.missingApi || []))].sort();

  return `<div class="cols">
      <section class="win">
        <h3>Чем проверено</h3>
        <table class="mini"><tbody>${Object.keys(HOW).map((k) => `
          <tr class="click" data-sp="how" data-val="${k}">
            <td>${HOW[k][0]}</td>
            <td class="num ${HOW[k][1]}">${cnt[k] || 0}</td></tr>`).join('')}
          <tr class="click" data-sp="how" data-val=""><td class="muted">всё</td>
            <td class="num">${all.length}</td></tr></tbody></table>
      </section>
      <section class="win">
        <h3>Спецификация против реализации <span class="chip bad">${missing.length}</span></h3>
        <div class="hint-row">Пути, которые описаны в документе, но которых нет
          среди известных атласу эндпоинтов (coverage.json, прогон Allure,
          карта интеграций). Либо не реализовано, либо реализовано иначе —
          и то и другое стоит вопроса аналитику.</div>
        <div class="chips">${missing.slice(0, 24).map((p) =>
          `<span class="chip bad">${esc(p)}</span>`).join('')}
          ${missing.length > 24 ? `<span class="muted">и ещё ${missing.length - 24}</span>` : ''}
        </div>
      </section>
      <section class="win">
        <h3>Как читать</h3>
        <div class="hint-row">«Прямая ссылка» — тест сам сослался на требование
          (<code>@allure.link("REQ-6.5")</code>). Остальное выведено: по
          эндпоинту из текста, по задетой таблице, по совпадению фичи. Это
          догадки, и они помечены как догадки. Непроверенное требование —
          такой же пробел, как недостающий TC.</div>
      </section>
    </div>
    <div class="filters">
      <input data-sp="q" value="${esc(SP.q)}" placeholder="поиск по требованиям">
      <select data-sp="src"><option value="">все документы</option>
        ${sources.map((s) => `<option ${SP.src === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}
      </select>
      <span class="muted">${list.length} из ${all.length}</span>
    </div>
    <div class="gaps">${list.slice(0, 200).map((r) => {
      const [word, cls] = HOW[r.how] || HOW.none;
      const open = SP.open === r.id;
      return `<section class="win">
        <h3><button class="req-id" data-sp-open="${esc(r.id)}">${esc(r.id)}</button>
          ${esc(r.title)} <span class="chip ${cls}">${word}</span>
          ${r.testCount ? `<span class="chip">тестов ${num(r.testCount)}</span>` : ''}</h3>
        <div class="req-path">${esc(r.path.join(' / '))} · ${esc(r.source)}</div>
        ${open ? `<pre class="req-text">${esc(r.lead.slice(0, 4000))}</pre>` : ''}
        ${r.nodes.length ? `<div class="chips">${r.nodes.map((id) =>
          `<button class="chip" data-node="${esc(id)}">${esc(nodeLabel(id))}</button>`).join('')}</div>` : ''}
        ${(r.missingApi || []).length ? `<div class="chips">${r.missingApi.map((p) =>
          `<span class="chip bad" title="в реализации не найден">${esc(p)}</span>`).join('')}</div>` : ''}
        ${open && r.tests.length ? `<div class="chips">${r.tests.slice(0, 30).map((t) =>
          `<button class="chip ${t.status === 'passed' ? 'ok' : t.status ? 'bad' : ''}"
            data-req-test="${esc(t.file || '')}" title="${esc((HOW[t.how] || [])[0] || t.how)}">${
            esc(t.name)}</button>`).join('')}</div>` : ''}
      </section>`;
    }).join('')}</div>`;
}

let _labels = null;
function nodeLabel(id) {
  if (!_labels) {
    _labels = new Map((g.nodes || []).map((n) => [n.id, n.label]));
  }
  return _labels.get(id) || id;
}

/* ---------------------------------------------------------------- схема БД */
/* Снимок схемы — артефакт `db.schema` агента partest-atlas. Отсюда видно, во что
 * упирается тест: какие таблицы есть, какие у них поля и куда ведут ключи. */
let schemaTable = '';
function viewSchema() {
  const dbs = (g.db && g.db.databases) || [];
  if (!dbs.length) {
    return `<div class="pad muted">Схема не снята: нет артефакта
      <code>db.schema</code> агента partest-atlas или он не прочитан — причина в
      <code>/api/artifacts</code>.</div>`;
  }
  const db = dbs[0];
  const q = (F.q || '').toLowerCase();
  const tables = (db.tables || []).filter((t) =>
    !q || t.name.toLowerCase().includes(q)
    || t.columns.some((c) => c.name.toLowerCase().includes(q)));
  const cur = tables.find((t) => t.name === schemaTable) || tables[0];
  const eff = {};
  ((g.dbEffects) || []).forEach((e) => { eff[e.table] = (eff[e.table] || 0) + 1; });

  return `<div class="filters">
      <input type="search" data-f="q" value="${esc(F.q)}" placeholder="таблица или поле…">
      <span class="muted">${esc(db.label)} · таблиц ${(db.tables || []).length}
        · колонок ${num((g.db.stats || {}).columns) || '—'}
        · FK ${num((g.db.stats || {}).foreignKeys) || '—'}</span>
    </div>
    <div class="schema">
      <div class="schema-list">${tables.map((t) => `
        <div class="srow ${cur && t.name === cur.name ? 'on' : ''}" data-tbl="${esc(t.name)}">
          <span class="n">${esc(t.name)}</span>
          <span class="muted">${t.columns.length}</span>
          <span class="muted">${t.rows == null ? '' : '≈' + num(t.rows)}</span>
          ${eff[t.name] ? `<span class="chip ok">${num(eff[t.name])}</span>` : ''}
        </div>`).join('') || '<div class="pad muted">ничего не нашлось</div>'}</div>
      <div class="schema-detail">${cur ? renderTableCard(cur) : ''}</div>
    </div>`;
}

function renderTableCard(t) {
  const refs = [];
  ((g.db.databases[0] || {}).tables || []).forEach((o) =>
    o.columns.forEach((c) => {
      if (c.fk && c.fk.table === t.name) refs.push(`${o.name}.${c.name}`);
    }));
  return `<section class="win">
      <h3>${esc(t.schema)}.${esc(t.name)}
        <span class="chip">${t.rows == null ? '—' : '≈' + num(t.rows) + ' строк'}</span></h3>
      <table class="cols">${t.columns.map((c) => `<tr>
        <td>${c.pk ? '<span class="key pk">PK</span>' : ''}${
          c.fk ? '<span class="key fk">FK</span>' : ''}</td>
        <td class="cname">${esc(c.name)}</td>
        <td class="muted">${esc(c.type)}</td>
        <td class="muted">${c.nullable ? '' : 'NOT NULL'}</td>
        <td class="muted">${c.fk ? `→ ${esc(c.fk.table)}.${esc(c.fk.column)}
          <span class="chip">${esc(c.fk.onDelete)}</span>` : ''}</td>
      </tr>`).join('')}</table>
      ${refs.length ? `<div data-css="margin-top:9px">
        <h3>На неё ссылаются (${refs.length})</h3>
        <div class="chips">${refs.slice(0, 24).map((r) =>
          `<span class="chip">${esc(r)}</span>`).join('')}</div></div>` : ''}
      <div class="row" data-css="margin-top:10px">
        <button class="act sm" data-node="store:${esc(t.name)}">Показать на карте</button>
      </div>
    </section>`;
}

/* --------------------------------------------------------------------- лог */
let logState = { name: '', lines: [], files: [], note: '', filter: '',
                 loading: false, fetched: false };

function viewLog() {
  // грузим один раз: без флага пустой лог заставлял перезапрашивать
  // сервер на каждую перерисовку вкладки
  if (!logState.fetched && !logState.loading) loadLog();
  const q = logState.filter.toLowerCase();
  const lines = q ? logState.lines.filter((l) => l.toLowerCase().includes(q))
                  : logState.lines;
  const level = (l) => /error|traceback|failed|критич/i.test(l) ? 'err'
    : (/warn|предупре/i.test(l) ? 'warn' : '');
  return `<div class="filters">
      ${logState.files.map((f) => `<button class="chip ${
        logState.name === f ? 'on' : ''}" data-log="${esc(f)}">${esc(f)}</button>`).join('')}
      <input type="search" data-logq value="${esc(logState.filter)}" placeholder="фильтр строк…">
      <button class="act" data-log-reload>Обновить</button>
      <span class="muted">${logState.loading ? 'читаю…'
        : `${lines.length} из ${logState.lines.length} строк`}</span>
    </div>
    ${logState.note ? `<div class="warn">${esc(logState.note)}</div>` : ''}
    <pre class="logview">${lines.map((l) =>
      `<span class="${level(l)}">${esc(l)}</span>`).join('\n')}</pre>`;
}

async function loadLog(name) {
  logState.loading = true;
  try {
    const d = await window.vehaApi.log(name);
    logState = { ...logState, ...d, loading: false, fetched: true,
                 note: d.note || '' };
  } catch (e) {
    logState = { ...logState, loading: false, fetched: true,
                 note: 'не прочитать: ' + e };
  }
  if (tab === 'log') render();
}

/* ------------------------------------------------------------------ отчёт */
/* Allure-отчёт внутри атласа: список тестов прогона, дерево шагов и вложения
 * (request/response, лог, скриншот) — без запуска allure serve. */
const RS = { status: '', q: '', tests: [], total: 0, loaded: false,
             open: null, detail: null, att: null, attName: '', groupBy: 'feature',
             mode: 'fails' };   // fails | tests — две половины вкладки «Прогон»

/* Вкладка «Прогон»: шапка с итогом и две половины — падения и полный отчёт.
 * Раньше это были две вкладки, «Прогон» и «Отчёт». Разными они не были:
 * обе про один и тот же последний прогон, просто с разной стороны. */
function viewRun() {
  const mode = RS.mode || 'fails';
  const sw = `<div class="chips pad-b">
      <button class="chip ${mode === 'fails' ? 'on' : ''}" data-rmode="fails">Падения</button>
      <button class="chip ${mode === 'tests' ? 'on' : ''}" data-rmode="tests">Все тесты и шаги</button>
    </div>`;
  return runBar() + sw + (mode === 'tests' ? reportBody() : failsBody());
}

function reportBody() {
  if (!RS.loaded) { loadTests(); return '<div class="pad muted">читаю allure-results…</div>'; }

  const groups = {};
  RS.tests.forEach((t) => {
    const k = RS.groupBy === 'feature' ? (t.feature || t.parentSuite || '—')
      : (RS.groupBy === 'status' ? t.status : (t.parentSuite || '—'));
    (groups[k] ||= []).push(t);
  });
  const keys = Object.keys(groups).sort((a, b) => groups[b].length - groups[a].length);
  const bad = (t) => t.status === 'failed' || t.status === 'broken';

  return `<div class="filters">
      ${['', 'failed', 'broken', 'passed', 'skipped'].map((st) =>
        `<button class="chip ${RS.status === st ? 'on' : ''}" data-rst="${st}">${
          st || 'все'}</button>`).join('')}
      <input type="search" data-rq value="${esc(RS.q)}" placeholder="тест, фича, nodeid…">
      <span class="muted">группировка:</span>
      ${[['feature', 'по фиче'], ['parentSuite', 'по модулю'], ['status', 'по статусу']]
        .map(([k, l]) => `<button class="chip ${RS.groupBy === k ? 'on' : ''}"
          data-rgb="${k}">${l}</button>`).join('')}
      <span class="muted">${RS.tests.length} из ${RS.total}</span>
    </div>
    <div class="report">
      <div class="rep-list">${keys.map((k) => `
        <div class="rep-grp">${esc(k)} <span class="chip">${groups[k].length}</span></div>
        ${groups[k].map((t) => `
          <div class="rep-row ${RS.open === t.file ? 'on' : ''} ${bad(t) ? 'bad' : ''}"
               data-test="${esc(t.file)}">
            <span class="st st-${esc(t.status)}"></span>
            <span class="nm">${esc(t.name)}</span>
            <span class="muted">${t.ms ? Math.round(t.ms) + ' мс' : ''}</span>
          </div>`).join('')}`).join('')
        || '<div class="pad muted">под фильтр ничего не подошло</div>'}</div>
      <div class="rep-detail">${RS.detail ? renderTestDetail() :
        '<div class="pad muted">выберите тест слева</div>'}</div>
    </div>`;
}

function renderTestDetail() {
  const d = RS.detail;
  const t = d.test;
  const sd = d.statusDetails || {};
  const stepRows = [];
  const walk = (steps, depth) => (steps || []).forEach((st) => {
    stepRows.push({ st, depth });
    walk(st.steps, depth + 1);
  });
  walk(d.steps, 0);

  const att = (a) => `<button class="chip" data-att="${esc(a.source)}"
      data-attn="${esc(a.name)}">${esc(a.name)}</button>`;

  return `<section class="win">
      <h3>${esc(t.name)}</h3>
      <div class="nodeid">${esc(t.nodeid)}</div>
      <div class="row" data-css="margin:6px 0">
        <span class="chip ${t.status === 'passed' ? 'ok' : 'bad'}">${esc(t.status)}</span>
        ${t.ms ? `<span class="chip">${Math.round(t.ms)} мс</span>` : ''}
        ${(d.labels || {}).feature ? `<span class="chip">${esc(d.labels.feature)}</span>` : ''}
        ${(d.labels || {}).story ? `<span class="chip">${esc(d.labels.story)}</span>` : ''}
      </div>
      ${(d.parameters || []).length ? `<div class="chips">${d.parameters.map((p) =>
        `<span class="chip">${esc(p.name)}=${esc(p.value)}</span>`).join('')}</div>` : ''}
      ${sd.message ? `<div class="err" data-css="margin-top:8px">${esc(
        String(sd.message).split('\n')[0])}</div>
        <pre class="errb">${esc(String(sd.message).split('\n').slice(1, 10).join('\n'))}</pre>`
        : ''}
      ${(d.attachments || []).length ? `<h3 data-css="margin-top:10px">Вложения теста</h3>
        <div class="chips">${d.attachments.map(att).join('')}</div>` : ''}
    </section>

    <section class="win">
      <h3>Шаги (${stepRows.length})</h3>
      <div class="steps">${stepRows.map(({ st, depth }) => `
        <div class="step" data-css="padding-left:${depth * 14}px">
          <span class="st st-${esc(st.status || 'unknown')}"></span>
          <span class="sn">${esc(st.name || '')}</span>
          ${(st.attachments || []).map(att).join('')}
        </div>`).join('') || '<div class="muted">шагов нет</div>'}</div>
    </section>

    ${RS.att ? `<section class="win">
      <h3>${esc(RS.attName)} <span class="chip">${RS.att.size} б</span></h3>
      ${RS.att.image ? `<img class="shot" src="${esc(RS.att.image)}" alt="">`
        : `<pre class="attach">${esc(RS.att.text || '')}</pre>`}
    </section>` : ''}

    <div class="row" data-css="margin-top:8px">
      ${rerunButton(t.nodeid)}
    </div>`;
}

async function loadTests() {
  RS.loaded = true;
  try {
    const d = await window.vehaApi.tests(RS.status, RS.q, 600);
    RS.tests = d.tests || [];
    RS.total = d.total || 0;
  } catch (e) {
    RS.tests = []; RS.total = 0;
  }
  if (tab === 'run') render();
}

async function openTest(file) {
  RS.open = file; RS.att = null; RS.attName = '';
  try {
    RS.detail = await window.vehaApi.test(file);
  } catch (e) { RS.detail = null; }
  render();
}

async function openAttachment(source, name) {
  RS.attName = name;
  if (source.endsWith('.png')) {
    RS.att = { size: 0, image: window.vehaApi.attachmentImage(source) };
    render();
    return;
  }
  try {
    RS.att = await window.vehaApi.attachment(source);
  } catch (e) { RS.att = { text: 'не прочитать: ' + e, size: 0 }; }
  render();
}

/* ------------------------------------------------------------------- live */
/* Состояние стенда рядом с картой: контейнеры и поток merge request'ов.
 * Сервер читает снимки из `live.snapshot_dir` (`veha/viz/live.py`) и сам наружу
 * не ходит: откуда снимки берутся — дело адаптеров. */
const LV = {
  containers: null, mrs: null, logs: null, logOf: '',
  poll: 0, auto: true, loading: false,
  runFor: '',            // номер МР, для которого выбирают набор
};

function viewLive() {
  // В снимке «Live» — только чтение: состояние и очередь на момент снимка; опрос, лог
  // контейнера и отметки МР требуют сервера.
  const OFF = window.vehaApi.isStatic;
  if (!LV.containers && !LV.loading) loadLive();
  // наборы и история нужны для кнопки «прогнать» у МР
  if (!RN.loaded) loadRunner();
  const c = LV.containers, m = LV.mrs;

  const cState = (x) => x.down ? 'bad' : (x.restarting ? 'warn' : 'ok');
  const containersBlock = !c ? '<div class="pad muted">читаю снимок контейнеров…</div>'
    : (!c.enabled ? `<div class="warn">${esc(c.note || 'снимка состояния контейнеров нет')}</div>`
    : (c.error ? `<div class="warn">состояние контейнеров: ${esc(c.error)}</div>` : `
      <div class="runbar">
        <span class="stat">контейнеров <b>${c.total}</b></span>
        <span class="stat ${c.down ? 'bad' : 'ok'}">down <b>${c.down}</b></span>
        ${c.restarting ? `<span class="stat bad">restarting <b>${c.restarting}</b></span>` : ''}
        <span class="stat">обновлено <b>${when(c.at)}</b></span>
      </div>
      <div class="cgrid">${c.containers.map((x) => `
        <div class="cbox ${cState(x)} ${LV.logOf === x.id ? 'on' : ''}" data-clog="${esc(x.id)}"
             title="${esc(x.status || '')}">
          <div class="ch"><span class="st st-${x.down ? 'failed' : (x.restarting ? 'broken' : 'passed')}"></span>
            <b>${esc(x.name)}</b></div>
          <div class="cm">${esc(x.state)}${x.uptime ? ' · ' + esc(x.uptime) : ''}</div>
          <div class="cm">${x.key ? `<span class="chip">${esc(x.key)}</span>` : ''}
            <span class="muted">${esc((x.image || '').split('/').pop().slice(0, 28))}</span></div>
        </div>`).join('')}</div>`));

  // прогоны, привязанные к МР: очередь проверки должна быть видна рядом со
  // строкой, а не в другой вкладке
  const mrRuns = {};
  (RN.history || []).forEach((h) => {
    const iid = (h.mr || {}).iid;
    if (iid) (mrRuns[iid] ||= []).push(h);
  });

  const mrRow = (x) => `<tr class="click" data-mr="${num(x.iid)}">
      <td><a href="${esc(safeUrl(x.url))}" target="_blank" rel="noopener">!${num(x.iid)}</a></td>
      <td>${esc(x.title || '')}</td>
      <td><span class="chip ${x.state === 'opened' ? '' : 'ok'}">${esc(x.state)}</span></td>
      <td class="muted">${esc(x.author || '')}</td>
      <td class="muted">${esc((x.merged || x.updated || '').slice(0, 10))}</td>
      <td>${x.check
        ? `<span class="chip ${x.check === 'checked' ? 'ok' : 'bad'}">${esc(x.check)}</span>`
        : '<span class="muted">—</span>'}</td>
      <td class="row">${OFF ? window.vehaApi.offline('прогнать · отметить') : `
        <button class="act sm" data-mrrun="${num(x.iid)}"
          ${RN.busy ? 'disabled title="прогон уже идёт"' : ''}>прогнать</button>
        <button class="act sm" data-mrset="checked" data-iid="${num(x.iid)}">проверен</button>
        <button class="act sm" data-mrset="todo" data-iid="${num(x.iid)}">в очередь</button>`}
      </td>
    </tr>
    ${(mrRuns[x.iid] || []).slice(0, 2).map((h) => `<tr class="mrrun">
      <td></td><td colspan="6" class="muted">прогон «${esc(h.label || h.suite)}»
        ${esc(when(h.startedAt))}: ${h.passed || 0} прошло${
        (h.failed || 0) + (h.broken || 0) ? `, <b class="bad">${
          (h.failed || 0) + (h.broken || 0)} упало</b>` : ''}</td>
    </tr>`).join('')}`;

  const mrsBlock = !m ? '<div class="pad muted">читаю очередь МР…</div>'
    : (!m.enabled ? `<div class="warn">${esc(m.note || 'очереди МР нет')}</div>`
    : (m.error ? `<div class="warn">очередь МР не прочитана: ${esc(m.error)}</div>` : `
      <div class="runbar">
        <span class="stat">открытых <b>${m.opened}</b></span>
        <span class="stat">смерженных <b>${m.merged}</b></span>
        <span class="stat ${m.unchecked ? 'bad' : 'ok'}">не проверено <b>${m.unchecked}</b></span>
        <span class="stat">${esc(m.project || '')}</span>
      </div>
      ${m.warn ? `<div class="warn">${esc(m.warn)}</div>` : ''}
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>MR</th><th>Название</th><th>Состояние</th><th>Автор</th>
          <th>Дата</th><th>Проверка</th><th></th></tr></thead>
        <tbody>${m.mrs.map(mrRow).join('')}</tbody></table></div>`));

  return `<div class="filters">${OFF ? `<div class="warn">Снимок стенда на момент сборки
      демо — только чтение. Опрос стенда, лог контейнера в реальном времени, прогон и
      отметки merge request'ов — ${window.vehaApi.OFFLINE}: им нужен сервер veha.</div>` : `
      <button class="act" data-live-reload>Обновить сейчас</button>
      <label class="check"><input type="checkbox" data-live-auto ${LV.auto ? 'checked' : ''}>
        обновлять автоматически</label>
      <span class="muted">${LV.loading ? 'опрашиваю…' : ''}</span>`}
    </div>
    ${LV.runFor ? `<section class="win">
      <h3>Прогон в счёт !${esc(LV.runFor)}</h3>
      <div class="hint-row">Прогон привяжется к этому merge request'у: запись
        попадёт в историю с его номером, а по итогу проставится отметка
        проверки. <b>Атлас не проверяет, что на стенде развёрнута именно эта
        ветка</b> — тесты идут на общий стенд, каким бы он ни был. Убедитесь,
        что МР выкачен, иначе проверите не то.</div>
      <div class="chips">${(RN.suites || []).map((sv) =>
        `<button class="act sm" data-mrsuite="${esc(sv.key)}">${esc(sv.label)}</button>`).join('')}
        <button class="act sm" data-mrcancel>отмена</button></div>
    </section>` : ''}
    <div class="cols">
      <section class="win"><h3>Контейнеры стенда</h3>${containersBlock}</section>
      <section class="win"><h3>Merge request'ы</h3>${mrsBlock}</section>
    </div>
    ${LV.logs ? `<section class="win">
      <h3>Лог контейнера ${esc(LV.logOf)}
        ${LV.logs.errorCount ? `<span class="chip bad">${LV.logs.errorCount} ошибок</span>` : ''}</h3>
      <pre class="logview">${(LV.logs.lines || []).map((l) =>
        `<span class="${/error|exception|fatal/i.test(l) ? 'err' : ''}">${esc(l)}</span>`)
        .join('\n') || esc(LV.logs.note || 'пусто')}</pre>
    </section>` : ''}`;
}

function when(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

async function loadLive() {
  LV.loading = true;
  const pull = async (url) => {
    try { return await url(); } catch (e) { return { error: String(e) }; }
  };
  // Источники грузим независимо: медленный ответ одного не должен задерживать
  // другой, а общий Promise.all держал бы контейнеры заложником его таймаута.
  const cp = pull(window.vehaApi.liveContainers).then((c) => {
    LV.containers = c;
    if (AT().setContainerState) AT().setContainerState(c);
    if (tab === 'live') render();
  });
  const mp = pull(window.vehaApi.liveMrs).then((m) => {
    LV.mrs = m;
    if (tab === 'live') render();
  });
  await Promise.allSettled([cp, mp]);
  LV.loading = false;
  if (tab === 'live') render();
  scheduleLive();
}

function scheduleLive() {
  clearTimeout(LV.poll);
  if (!LV.auto || window.vehaApi.isStatic) return;   // снимок не меняется: опрашивать нечего
  LV.poll = setTimeout(loadLive, 60000);
}

async function setMr(iid, status) {
  try {
    LV.mrs = await (await fetch('/api/live/mr', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ iid: Number(iid), status }),
    })).json();
  } catch (e) { /* ошибку покажет следующий опрос */ }
  render();
}

async function loadContainerLog(cid) {
  LV.logOf = cid;
  if (window.vehaApi.isStatic) {
    LV.logs = { lines: [], note: `лог контейнера в реальном времени — ${window.vehaApi.OFFLINE}` };
    render();
    return;
  }
  LV.logs = { lines: [], note: 'читаю…' };
  render();
  try {
    LV.logs = await (await fetch(`/api/live/container/${encodeURIComponent(cid)}/logs`)).json();
  } catch (e) { LV.logs = { lines: [], note: 'не прочитать: ' + e }; }
  render();
}

/* ---------------------------------------------------------------- раннер */
/* Запуск наборов прямо из атласа. Прогресс читается из вывода pytest, поэтому
 * визуал может отставать на секунду — тесты от этого не страдают. */
const RN = { suites: [], history: [], trend: [], trendSeries: [], run: null,
             busy: false, loaded: false, poll: 0, note: '' };

function viewRunner() {
  if (!RN.loaded) { loadRunner(); return '<div class="pad muted">читаю наборы…</div>'; }
  const OFF = window.vehaApi.isStatic;
  const r = RN.run;
  // процент pytest (есть и под xdist), иначе своё деление; итог — 100%
  const pct = !r ? 0 : (r.complete ? 100 : (num(r.pct) || (r.total
    ? Math.round((num(r.done) / num(r.total)) * 100) || 0 : 0)));
  const live = r && r.status === 'running';

  const progress = !r ? '' : `
    <section class="win">
      <h3>${esc(r.label)}
        <span class="chip ${live ? '' : (r.failed ? 'bad' : 'ok')}">${esc(r.status)}</span>
      </h3>
      <div class="stack"><i data-css="width:${pct}%;background:${
        r.failed ? 'var(--warn)' : 'var(--ok)'}"></i></div>
      <div class="runbar" data-css="margin-top:8px">
        <span class="stat">${num(r.done)}/${num(r.total) || '?'}</span>
        <span class="stat ok">passed <b>${num(r.passed)}</b></span>
        <span class="stat ${r.failed ? 'bad' : ''}">failed <b>${num(r.failed)}</b></span>
        ${r.broken ? `<span class="stat bad">broken <b>${num(r.broken)}</b></span>` : ''}
        ${r.skipped ? `<span class="stat">skipped <b>${num(r.skipped)}</b></span>` : ''}
        <span class="stat">${ms(r.wallMs)}</span>
      </div>
      ${r.current ? `<div class="nodeid">сейчас: ${esc(r.current)}</div>` : ''}
      ${(r.failures || []).length ? `<div class="chips pad-b">${
        r.failures.slice(-8).map((f) => `<button class="chip bad" data-failnode="${esc(f)}"
          title="${esc(f)}">${esc(f.split('::').pop())}</button>`).join('')}</div>` : ''}
      ${live ? '<button class="act sm" data-run-stop>Остановить</button>' : ''}
      <pre class="logview" data-css="max-height:150px;margin-top:8px">${
        (r.tail || []).slice(-14).map(esc).join('\n')}</pre>
    </section>`;

  const cards = RN.suites.map((sv) => `
    <div class="suite">
      <div class="sn">${esc(sv.label)}</div>
      <div class="muted">${esc(sv.note || sv.args.join(' '))}</div>
      ${OFF ? window.vehaApi.offline('Запустить')
        : `<button class="act sm" data-run="${esc(sv.key)}" ${RN.busy ? 'disabled' : ''}>
        Запустить</button>`}
    </div>`).join('');

  const hist = RN.history.slice().reverse().map((h) => {
    const cov = h.coverage || {};
    const cn = h.containers || {};
    return `<tr>
      <td>${esc(new Date(h.startedAt * 1000).toLocaleString('ru-RU'))}</td>
      <td>${esc(h.label || h.suite)}${h.mr && h.mr.iid
        ? ` <a class="chip" href="${esc(safeUrl(h.mr.url))}" target="_blank"
             rel="noopener" title="${esc(h.mr.title || '')}">!${num(h.mr.iid)}</a>` : ''}</td>
      <td><span class="chip ${h.failed ? 'bad' : 'ok'}">${num(h.passed)}/${num(h.total)}</span></td>
      <td class="num">${num(h.failed) || ''}</td>
      <td class="num">${ms(h.wallMs)}</td>
      <td class="num">${cov.partialRun
        ? `<span class="chip" title="${esc((cov.partialReasons || []).join(', '))
          || 'в тренд среднего не входит'}">частичный: ${
          esc((cov.partialReasons || ['?'])[0])}</span>`
        : cov.endpoints === 0
          ? `<span class="chip" title="отчёт ничего не посчитал (endpoints: 0) — в тренд среднего не входит">не посчитано</span>`
          : (cov.avg != null ? num(cov.avg) + '%' : '—')}${
        (cov.optinText || []).length ? ` <span class="chip" title="${
          esc('opt-in: ' + cov.optinText.map(String).join('; '))}">opt-in</span>` : ''}</td>
      <td>${cn.down ? `<span class="chip bad" title="${esc((cn.bad || []).join(', '))}">
        ${num(cn.down)} лежит</span>` : '<span class="muted">стенд цел</span>'}</td>
    </tr>`;
  }).join('');

  return `<div class="filters">
      ${OFF ? `<div class="warn">Запуск наборов, остановка и очистка отчётов —
        ${window.vehaApi.OFFLINE}: им нужен сервер veha. История прогонов и тренд — из
        снимка.</div>` : `<button class="act" data-run-reload>Обновить</button>
      <button class="act" data-run-clean ${RN.busy ? 'disabled' : ''}
        title="Удалить allure-results — иначе прогоны копятся в одной папке">
        Очистить отчёты</button>`}
      ${RN.note ? `<span class="muted">${esc(RN.note)}</span>` : ''}
    </div>
    ${progress}
    <section class="win"><h3>Наборы</h3><div class="suites">${cards}</div></section>
    <section class="win">
      <h3>История прогонов <span class="chip">${RN.history.length}</span></h3>
      ${trendLine()}
      ${RN.history.length ? `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Когда</th><th>Набор и МР</th><th>Итог</th><th>Упало</th>
          <th>Время</th><th>Покрытие</th><th>Стенд</th></tr></thead>
        <tbody>${hist}</tbody></table></div>`
        : '<div class="muted">прогонов ещё не было</div>'}
    </section>`;
}

/* Тренд среднего покрытия — строкой, только по полным прогонам: процент частичного
 * посчитан по части набора и с полным несравним. Нет полного набора — так и сказано.
 * Прогоны с разным состоянием opt-in-наборов — разные серии (`trendSeries`): у них
 * разный состав тестов, и одна линия нарисовала бы скачок, которого не было. Первой
 * идёт серия последнего полного прогона. */
function trendLine() {
  const full = RN.suites.some((s) => s.full);
  if (!full) {
    return '<div class="muted">Тренда покрытия не будет: в <code>runner.suites</code> '
      + 'нет набора с <code>full: true</code>.</div>';
  }
  const series = RN.trendSeries.length ? RN.trendSeries
    : (RN.trend.length ? [{ current: true, label: '', points: RN.trend }] : []);
  if (!series.length) {
    return '<div class="muted">Тренд покрытия (только полные прогоны): полных прогонов '
      + 'ещё не было.</div>';
  }
  const pts = (s) => (s.points || []).slice(-12).map((x) => pct(x.avg)).join(' → ');
  const many = series.length > 1;
  const why = {
    differs: 'opt-in-наборы решены иначе — другой состав тестов, с текущей серией несравнимо',
    unknown: 'у одной из серий нет записи opt-in — сопоставимость не подтверждена',
  };
  return series.map((s) => {
    const label = s.label ? `opt-in: ${s.label}`
      : (many ? 'без записи opt-in' : '');
    if (s.current) {
      return `<div class="muted" title="только полные прогоны">Тренд покрытия `
        + `(только полные прогоны${label ? ', ' + esc(label) : ''}): <b>${pts(s)}</b></div>`;
    }
    return `<div class="muted" title="${esc(why[s.relation] || '')}">отдельная серия — `
      + `${esc(label)}: <b>${pts(s)}</b></div>`;
  }).join('');
}

async function loadRunner() {
  RN.loaded = true;
  try {
    const d = await window.vehaApi.runner();
    RN.suites = d.suites || []; RN.history = d.history || []; RN.trend = d.trend || [];
    RN.trendSeries = d.trendSeries || [];
    RN.busy = !!d.busy; RN.run = d.run || null;
  } catch (e) { RN.note = 'не прочитать: ' + e; }
  // «Live» тоже показывает наборы и историю: там кнопка «прогнать» у МР
  if (tab === 'runner' || tab === 'live') render();
  pollRunner();
}

function pollRunner() {
  clearTimeout(RN.poll);
  if (!RN.busy) return;
  RN.poll = setTimeout(async () => {
    try {
      const d = await (await fetch('/api/runner/status')).json();
      RN.busy = !!d.busy; RN.run = d.run || RN.run;
      // подсветка на карте идёт отдельно от кадра: анимация не должна
      // ни тормозить тесты, ни держать перерисовку сцены
      if (AT().setRunHighlight && RN.run) {
        AT().setRunHighlight(RN.run.touchedEndpoints || [],
                             RN.run.failedEndpoints || [],
                             (num(RN.run.failed) || 0) + (num(RN.run.broken) || 0));
      }
      if (!RN.busy) { await loadRunner(); return; }   // дописать историю
    } catch (e) { /* сеть моргнула — попробуем на следующем тике */ }
    if (tab === 'runner') render();
    pollRunner();
  }, 1200);
}

async function startRun(suite, mr) {
  try {
    const r = await fetch('/api/runner/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(mr ? { suite, mr: Number(mr) } : { suite }),
    });
    const d = await r.json();
    if (!r.ok) { RN.note = d.detail || 'не запустилось'; render(); return; }
    RN.busy = !!d.busy; RN.run = d.run; RN.note = '';
    render(); pollRunner();
  } catch (e) { RN.note = String(e); render(); }
}

/* ---------------------------------------------------------------- сервисы */
function viewServices() {
  const svc = (g.coverage || {}).services || [];
  if (!svc.length) return '<div class="muted pad">нет coverage.json</div>';
  const runBySvc = {};
  endpoints().forEach((n) => {
    const k = n.meta.service || '—';
    (runBySvc[k] ||= { fail: 0, total: 0 });
    runBySvc[k].fail += n.meta.runFailed || 0;
    runBySvc[k].total += n.meta.runTotal || 0;
  });
  return `<div class="tbl-wrap"><table class="tbl">
    <thead><tr><th>Сервис</th><th>Эндпоинтов</th><th>Покрытие</th><th></th>
      <th>full</th><th>partial</th><th>empty</th><th>Вызовов</th><th>Падений</th></tr></thead>
    <tbody>${svc.map((s) => {
      const rr = runBySvc[s.label] || runBySvc[s.key] || { fail: 0 };
      return `<tr class="click" data-set="service" data-val="${esc(s.label)}">
        <td>${esc(s.label)}</td><td class="num">${num(s.totalEndpoints)}</td>
        <td class="num">${pct(s.avgCoverage)}</td>
        <td class="barcell"><i data-css="width:${num(s.avgCoverage) || 0}%;background:${
          s.avgCoverage >= 99 ? 'var(--ok)' : s.avgCoverage > 0 ? 'var(--part)' : 'var(--line-2)'
        }"></i></td>
        <td class="num">${num(s.full)}</td><td class="num">${num(s.partial)}</td>
        <td class="num">${num(s.empty) || 0}</td><td class="num">${num(s.calls)}</td>
        <td class="num">${rr.fail ? `<span class="chip bad">${num(rr.fail)}</span>` : ''}</td>
      </tr>`;
    }).join('')}</tbody></table></div>`;
}

/* Кнопки шапки: «назад» по следу переходов и снятие выделения.
 * Обе появляются только тогда, когда есть что делать. */
function renderDockNav() {
  const back = document.getElementById('dock-back');
  if (back) {
    back.hidden = !trail.length;
    back.title = trail.length
      ? `Вернуться: ${(TABS.find(([k]) => k === trail[trail.length - 1]) || [])[1] || ''}`
      : '';
  }
}

/* Переход на другую вкладку с запоминанием, откуда пришли. */
function go(next) {
  if (next !== tab) {
    trail.push(tab);
    if (trail.length > 20) trail.shift();
    tab = next;
  }
  section = sectionOf(tab);
  lastTab[section] = tab;
  // раздел мог смениться (из требования — в «Прогон»): шапка идёт за ним
  document.querySelectorAll('#sections [data-section]').forEach((b) => {
    if (b.dataset.section === section) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
}

/* ------------------------------------------------------------------ рендер */
function render() {
  if (section === 'map') return;      // страница скрыта — рисовать нечего
  // счётчик и снятие фильтров эндпоинтов — только в разделе, где фильтр живёт
  dockNote.hidden = section !== 'cov';
  const af = document.getElementById('active-filters');
  if (af) af.hidden = section !== 'cov';
  const tabs = (SECTIONS.find(([k]) => k === section) || SECTIONS[0])[2];
  const fails = (num((g.run || {}).failed) || 0) + (num((g.run || {}).broken) || 0);
  document.getElementById('dock-tabs').innerHTML = tabs.map(([k, l]) => {
    const badge = k === 'run' && fails ? `<b>${fails}</b>` : '';
    return `<button data-tab="${k}"${tab === k ? ' aria-current="page"' : ''}>${l}${badge}</button>`;
  }).join('');
  renderDockNav();
  dockBody.innerHTML = ({ overview: viewOverview, endpoints: viewEndpoints,
    matrix: viewMethod, links: viewLinks, specs: viewSpecs,
    contract: viewContract, schema: viewSchema,
    runner: viewRunner, live: viewLive, log: viewLog, run: viewRun,
    services: viewServices }[tab])();
  wire();
}

function wire() {
  dockBody.querySelectorAll('[data-rmode]').forEach((el) =>
    el.addEventListener('click', () => { RS.mode = el.dataset.rmode; render(); }));
  dockBody.querySelectorAll('[data-mxmode]').forEach((el) =>
    el.addEventListener('click', () => { mxMode = el.dataset.mxmode; render(); }));
  // Строка ведёт на карту: узел выбран, его цепочка видна. Фильтр таблицы
  // остаётся на карте подсветкой — снять его можно там же.
  dockBody.querySelectorAll('[data-node]').forEach((el) => {
    if (el.dataset.node === selNode) el.classList.add('sel');
    el.addEventListener('click', () => {
      showSection('map');
      AT().select(el.dataset.node);
    });
  });
  dockBody.querySelectorAll('[data-lset]').forEach((el) =>
    el.addEventListener('click', () => { linkSet = el.dataset.lset; render(); }));
  const lqEl = dockBody.querySelector('[data-lq]');
  if (lqEl) lqEl.addEventListener('input', () => {
    linkQ = lqEl.value; render();
    const again = dockBody.querySelector('[data-lq]');
    if (again) { again.focus(); again.setSelectionRange(9999, 9999); }
  });
  // фильтры вкладки «Требования»: свой набор, чтобы не мешать фильтру эндпоинтов
  dockBody.querySelectorAll('[data-sp]').forEach((el) => {
    if (el.dataset.val !== undefined) {
      el.addEventListener('click', () => { SP[el.dataset.sp] = el.dataset.val; render(); });
      return;
    }
    const ev = el.tagName === 'SELECT' ? 'change' : 'input';
    el.addEventListener(ev, () => {
      SP[el.dataset.sp] = el.value;
      const focus = el.dataset.sp;
      render();
      const again = dockBody.querySelector(`[data-sp="${focus}"]`);
      if (again && ev === 'input') { again.focus(); again.setSelectionRange(9999, 9999); }
    });
  });
  // тест из требования открываем во вкладке «Отчёт»: там дерево шагов и вложения
  dockBody.querySelectorAll('[data-req-test]').forEach((el) =>
    el.addEventListener('click', () => {
      if (!el.dataset.reqTest) return;
      go('run'); RS.mode = 'tests';
      openTest(el.dataset.reqTest);
    }));
  dockBody.querySelectorAll('[data-sp-open]').forEach((el) =>
    el.addEventListener('click', () => {
      SP.open = SP.open === el.dataset.spOpen ? '' : el.dataset.spOpen;
      render();
    }));
  dockBody.querySelectorAll('[data-set]').forEach((el) => el.addEventListener('click', () => {
    F[el.dataset.set] = el.dataset.val;
    // фильтр виден там, где он действует: в «Эндпоинтах», откуда бы ни нажали
    if (tab !== 'endpoints') go('endpoints');
    applyFilter();
  }));
  dockBody.querySelectorAll('[data-f]').forEach((el) => {
    const ev = el.tagName === 'SELECT' ? 'change' : 'input';
    el.addEventListener(ev, () => {
      F[el.dataset.f] = el.value;
      const focus = el.dataset.f;
      applyFilter();
      const again = dockBody.querySelector(`[data-f="${focus}"]`);
      if (again && ev === 'input') { again.focus(); again.setSelectionRange(9999, 9999); }
    });
  });
  const clear = document.getElementById('f-clear');
  if (clear) clear.addEventListener('click', () => {
    Object.keys(F).forEach((k) => (F[k] = ''));
    applyFilter();
  });
  dockBody.querySelectorAll('[data-run]').forEach((el) =>
    el.addEventListener('click', () => startRun(el.dataset.run)));
  const rs = dockBody.querySelector('[data-run-stop]');
  if (rs) rs.addEventListener('click', async () => {
    await fetch('/api/runner/stop', { method: 'POST' });
    loadRunner();
  });
  const rr = dockBody.querySelector('[data-run-reload]');
  if (rr) rr.addEventListener('click', () => { RN.loaded = false; render(); });
  const rc = dockBody.querySelector('[data-run-clean]');
  if (rc) rc.addEventListener('click', async () => {
    const r = await fetch('/api/runner/clean', { method: 'POST' });
    const d = await r.json();
    RN.note = r.ok ? `удалено файлов: ${d.removed}` : (d.detail || 'не вышло');
    render();
  });
  dockBody.querySelectorAll('[data-failnode]').forEach((el) =>
    el.addEventListener('click', () => {
      go('run'); RS.mode = 'tests'; RS.q = el.dataset.failnode.split('::').pop();
      RS.loaded = false; render();
    }));
  dockBody.querySelectorAll('[data-clog]').forEach((el) =>
    el.addEventListener('click', () => loadContainerLog(el.dataset.clog)));
  dockBody.querySelectorAll('[data-mrrun]').forEach((el) =>
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      LV.runFor = el.dataset.mrrun;
      render();
    }));
  dockBody.querySelectorAll('[data-mrsuite]').forEach((el) =>
    el.addEventListener('click', async (e) => {
      e.stopPropagation();
      const iid = LV.runFor;
      LV.runFor = '';
      await startRun(el.dataset.mrsuite, iid);
      go('runner');
      render();
    }));
  const mrc = dockBody.querySelector('[data-mrcancel]');
  if (mrc) mrc.addEventListener('click', () => { LV.runFor = ''; render(); });
  dockBody.querySelectorAll('[data-mrset]').forEach((el) =>
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      setMr(el.dataset.iid, el.dataset.mrset);
    }));
  const lrl = dockBody.querySelector('[data-live-reload]');
  if (lrl) lrl.addEventListener('click', loadLive);
  const lau = dockBody.querySelector('[data-live-auto]');
  if (lau) lau.addEventListener('change', () => { LV.auto = lau.checked; scheduleLive(); });
  dockBody.querySelectorAll('[data-test]').forEach((el) =>
    el.addEventListener('click', () => openTest(el.dataset.test)));
  dockBody.querySelectorAll('[data-att]').forEach((el) =>
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      openAttachment(el.dataset.att, el.dataset.attn);
    }));
  dockBody.querySelectorAll('[data-rst]').forEach((el) => el.addEventListener('click', () => {
    RS.status = el.dataset.rst; RS.loaded = false; render();
  }));
  dockBody.querySelectorAll('[data-rgb]').forEach((el) => el.addEventListener('click', () => {
    RS.groupBy = el.dataset.rgb; render();
  }));
  const rq = dockBody.querySelector('[data-rq]');
  if (rq) rq.addEventListener('input', () => {
    RS.q = rq.value; RS.loaded = false; render();
    const again = dockBody.querySelector('[data-rq]');
    if (again) { again.focus(); again.setSelectionRange(9999, 9999); }
  });
  dockBody.querySelectorAll('[data-log]').forEach((el) => el.addEventListener('click', () => {
    logState.lines = []; logState.fetched = false; loadLog(el.dataset.log);
  }));
  const lq = dockBody.querySelector('[data-logq]');
  if (lq) lq.addEventListener('input', () => {
    logState.filter = lq.value; render();
    const again = dockBody.querySelector('[data-logq]');
    if (again) { again.focus(); again.setSelectionRange(9999, 9999); }
  });
  const lr = dockBody.querySelector('[data-log-reload]');
  if (lr) lr.addEventListener('click', () => loadLog(logState.name));
  dockBody.querySelectorAll('[data-grpby]').forEach((el) => el.addEventListener('click', () => {
    groupBy = el.dataset.grpby; collapsed.clear(); render();
  }));
  dockBody.querySelectorAll('[data-grp]').forEach((el) => el.addEventListener('click', () => {
    const k = el.dataset.grp;
    if (collapsed.has(k)) collapsed.delete(k); else collapsed.add(k);
    render();
  }));
  dockBody.querySelectorAll('[data-sort]').forEach((el) => el.addEventListener('click', () => {
    if (sortKey === el.dataset.sort) sortDir = -sortDir;
    else { sortKey = el.dataset.sort; sortDir = 1; }
    render();
  }));
  dockBody.querySelectorAll('[data-tbl]').forEach((el) => el.addEventListener('click', () => {
    schemaTable = el.dataset.tbl; render();
  }));
  dockBody.querySelectorAll('[data-lk]').forEach((el) => el.addEventListener('click', () => {
    linkKind = el.dataset.lk; render();
  }));
  dockBody.querySelectorAll('[data-feat]').forEach((el) => el.addEventListener('click', () => {
    runFeature = el.dataset.feat; render();
  }));
  dockBody.querySelectorAll('[data-goto-feature]').forEach((el) =>
    el.addEventListener('click', () => {
      runFeature = el.dataset.gotoFeature; go('run'); render();
    }));
  dockBody.querySelectorAll('[data-trace]').forEach((el) => el.addEventListener('click', () => {
    const pre = [...dockBody.querySelectorAll('[data-trace-for]')]
      .find((x) => x.dataset.traceFor === el.dataset.trace);
    if (pre) pre.classList.toggle('hidden');
  }));
  dockBody.querySelectorAll('[data-rerun]').forEach((el) =>
    el.addEventListener('click', () => rerunFromDock(el, el.dataset.rerun)));
}

/* «Перегнать кейс» — процесс на машине сервера; в снимке кнопка выключена. */
function rerunButton(nodeid) {
  return window.vehaApi.isStatic ? window.vehaApi.offline('Перегнать кейс')
    : `<button class="act sm" data-rerun="${esc(nodeid)}">Перегнать кейс</button>`;
}

async function rerunFromDock(btn, nodeid) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Гоняю…';
  const box = document.createElement('pre');
  box.className = 'out';
  box.textContent = 'pytest ' + nodeid;
  btn.parentElement.after(box);
  try {
    const r = await fetch('/api/rerun', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nodeid }) });
    const d = await r.json();
    box.textContent = r.ok ? (d.passed ? '✓ прошёл\n\n' : '✕ упал\n\n') + d.output
                           : 'Ошибка: ' + (d.detail || r.status);
    box.style.borderColor = r.ok && d.passed ? 'var(--ok)' : 'var(--bad)';
  } catch (e) { box.textContent = 'Сервис недоступен: ' + e; }
  finally { btn.disabled = false; btn.textContent = label; }
}

/* --------------------------------------------------------------- каркас UI */
/* Переключение разделов: карта или страница. Карта при возвращении
 * перерисовывается — пока страница была открыта, могли смениться размеры. */
function showSection(next) {
  section = next === 'map' ? 'map' : (SECTIONS.some(([k]) => k === next) ? next : 'cov');
  if (section !== 'map') tab = lastTab[section];
  document.getElementById('view-map').hidden = section !== 'map';
  document.getElementById('view-page').hidden = section === 'map';
  document.querySelectorAll('#sections [data-section]').forEach((b) => {
    if (b.dataset.section === section) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  if (section === 'map') { if (AT()) AT().shown(); return; }
  if (g) render();
  const page = document.getElementById('view-page');
  if (page) page.scrollTop = 0;
}

/* Переход на подраздел (или на карту) — из карточки, сводки, палитры. */
function openTab(t) {
  if (!t || t === 'map') { showSection('map'); return; }
  if (!TABS.some(([k]) => k === t)) return;
  go(t);
  showSection(section);
}

document.querySelectorAll('[data-section]').forEach((b) => b.addEventListener('click', (e) => {
  e.preventDefault();
  showSection(b.dataset.section);
}));

document.getElementById('dock-tabs').addEventListener('click', (e) => {
  const b = e.target.closest('[data-tab]');
  if (!b) return;
  go(b.dataset.tab);
  render();
});

document.getElementById('dock-back').addEventListener('click', () => {
  if (!trail.length) return;
  tab = trail.pop();
  section = sectionOf(tab);
  lastTab[section] = tab;
  showSection(section);
});

document.addEventListener('atlas:selection', (e) => {
  selNode = e.detail || '';
  if (g && section !== 'map') render();
});

document.addEventListener('atlas:ready', (e) => {
  g = e.detail;
  applyFilter();
});

// из карточки или сводки: подраздел, при необходимости — с фильтром таблицы
document.addEventListener('atlas:goto', (e) => {
  const d = e.detail || {};
  if (d.set === 'lq') { linkQ = d.val || ''; linkKind = ''; }
  else if (d.set && Object.prototype.hasOwnProperty.call(F, d.set)) {
    F[d.set] = d.val || '';
    applyFilter();
  }
  if (d.feature) runFeature = d.feature;
  openTab(d.tab);
});

// «снять» у подсветки на карте снимает фильтр таблицы
document.addEventListener('atlas:clearfilter', () => {
  Object.keys(F).forEach((k) => (F[k] = ''));
  applyFilter();
});

// требование из карточки узла: раскрыть его в разделе «Требования»
document.addEventListener('atlas:openreq', (e) => {
  if (!g) return;
  SP.how = ''; SP.src = ''; SP.q = ''; SP.open = e.detail;
  openTab('specs');
  // без CSS.escape: в этом файле глобальный `CSS` — палитра из atlas.js
  const card = [...dockBody.querySelectorAll('[data-sp-open]')]
    .find((el) => el.dataset.spOpen === e.detail);
  if (card) card.closest('section').scrollIntoView({ block: 'center' });
});
}());
