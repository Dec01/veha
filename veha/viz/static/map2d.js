/* Веха, карта по слоям — вид по умолчанию.
 *
 * Система раскладывается слева направо по ходу запроса: экраны → API → шина и
 * сервисы → хранилища. Камеры нет: колонки стоят на своих местах, и глаз не
 * ищет, куда повернулась сцена.
 *
 *   · эндпоинт — квадрат в блоке своего домена, внутри блока по сервисам;
 *     цвет — покрытие по методике, красная рамка — упал в последнем прогоне;
 *   · экраны, топики, сервисы и таблицы стоят подписанными списками; подпись
 *     длиннее колонки обрезается, полное имя — в подсказке;
 *   · связи не рисуются, пока не выбран узел: тогда видна его цепочка, а
 *     остальное гаснет. Происхождение связи — начертанием, не цветом.
 *
 * Рисуем SVG строкой: узлов на систему сотни (architecture.md §8), а SVG даёт
 * наведение, подсказку и попадание курсором без своего кода.
 * Стили — только классами (style.css): CSP вебморды не пускает атрибут style.
 */
'use strict';

window.Map2D = (function () {
  const SQ = 11, PITCH = 18, PER_ROW = 11;
  const BLOCK_W = PER_ROW * PITCH, BLOCK_GAP = 30;
  // колонки: экраны · API · шина · хранилища; зазор перед шиной шире — там идут связи
  const COL_SCR = 150, COL_BUS = 150, COL_ST = 190, GAP = 26, GAP_BUS = 42;
  const TOP = 46;

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
  const fmt = (v, d = 0) => num(v).toFixed(d).replace('.', ',');
  const twMono = (s, px) => String(s).length * px * 0.6;
  const twSans = (s, px) => String(s).length * px * 0.56;
  const plural = (k, f) => {
    const n = Math.abs(num(k)) % 100, d = n % 10;
    return `${num(k)} ${f[d === 1 && n !== 11 ? 0 : (d >= 2 && d <= 4 && (n < 12 || n > 14) ? 1 : 2)]}`;
  };
  /* Подпись в ширину колонки: длиннее — обрезаем с «…», полное имя остаётся в подсказке.
   * Так подписи не налезают на соседнюю колонку при любых именах. */
  const fit = (s, maxPx, px, mono) => {
    const t = String(s ?? '');
    const per = px * (mono ? 0.6 : 0.56);
    const max = Math.max(4, Math.floor(maxPx / per));
    return t.length <= max ? t : t.slice(0, max - 1) + '…';
  };
  const KIND_HEAD = {
    backend: 'Сервисы', service: 'Сервисы', worker: 'Воркеры', external: 'Внешние системы',
    auth: 'Аутентификация', store: 'Хранилища', storage: 'Хранилища', bus: 'Шины',
  };

  /* --------------------------------------------------------------- семантика */
  /* Покрытие эндпоинта: покрыт, частично, без тестов (0 % по методике) и «нет в
   * coverage.json» — эндпоинт вызывали тесты или он только объявлен в карте, а в
   * отчёте покрытия его нет. Последнее — не «тестов нет», а «замера нет». */
  function cov(n) {
    const st = (n.meta || {}).status;
    if (st === 'full') return 'ok';
    if (st === 'partial') return 'part';
    if (st === 'empty') return 'none';
    return 'unseen';
  }

  /* Происхождение связи. Красная линия Вехи — догадка подписана догадкой — на
   * карте держится начертанием:
   *   measured — снято на прогоне и привязано к вызову (UI-рекордер, `direct`);
   *   inferred — выведено: приписано по окну теста (запись могла сделать
   *              фикстура), событие «в тишине записей», упоминание в логах;
   *   declared — объявлено в ручной карте или atlas.yaml.
   * Связь записи UI-рекордера без `confidence` приходит без подписи и неотличима
   * от ручной — здесь она «объявлена»; поле происхождения у ребра — в proposals. */
  const WINDOW_PREFIX = 'снято в окне теста';
  function evidence(e) {
    const lab = String(e.label || '');
    if (e.kind === 'logged' || lab === 'window' || lab.startsWith(WINDOW_PREFIX)
        || lab.startsWith('снято: событие в тишине')) return 'inferred';
    if (lab === 'direct' || lab.startsWith('снято')) return 'measured';
    return 'declared';
  }
  /* Встречено ли на прогоне вообще — как бы ни было приписано. */
  function observed(e) {
    const lab = String(e.label || '');
    return lab.startsWith('снято') || lab === 'direct' || lab === 'window';
  }
  const EV_RANK = { measured: 0, declared: 1, inferred: 2 };
  const EV_TEXT = { measured: 'снято на прогоне', declared: 'объявлено', inferred: 'выведено' };

  /* Цепочка для карты: соседи узла и следующий шаг там, где без него путь
   * обрывается на полуслове — потребитель за топиком, экран за контролом,
   * вызовы контролов выбранного экрана. Полный транзитивный путь — по кнопке
   * в карточке: на нём цепочка одной корзины тянула 13 таблиц. */
  function chainOf(G, id) {
    const self = G.nodes.find((n) => n.id === id);
    const isApi = self && self.layer === 'api';
    const out = [];
    const hop1 = new Set();
    if (self && self.kind === 'screen') {
      const key = id.slice(id.indexOf(':') + 1);
      const sp = self.meta.screenPath;
      G.nodes.forEach((c) => {
        if (c.kind === 'control' && (c.meta.screen === key || (sp && c.meta.screenPath === sp))) hop1.add(c.id);
      });
    }
    G.edges.forEach((e) => {
      if (e.source !== id && e.target !== id) return;
      if (e.kind === 'logged' && isApi) return;   // факт о контейнере, не о вызове
      out.push(e);
      hop1.add(e.source === id ? e.target : e.source);
    });
    G.edges.forEach((e) => {
      if (e.kind === 'consume' && hop1.has(e.source) && e.source.startsWith('topic:')) out.push(e);
      if (e.kind === 'contains' && hop1.has(e.target)) out.push(e);
      if (self && self.kind === 'screen' && e.kind === 'calls' && hop1.has(e.source)) out.push(e);
    });
    return edgesToChain(id, out);
  }

  function edgesToChain(id, list) {
    const nodes = new Set([id]);
    const seen = new Set();
    const edges = [];
    list.forEach((e) => {
      const k = e.source + '\u0001' + e.target + '\u0001' + e.kind + '\u0001' + (e.label || '');
      if (seen.has(k)) return;
      seen.add(k);
      edges.push(e);
      nodes.add(e.source); nodes.add(e.target);
    });
    return { nodes, edges };
  }

  /* ------------------------------------------------------------------ раскладка */
  let host = null, G = null, handlers = {};
  let touched = new Set();   // узлы, встреченные на прогоне
  let measuredDb = true, measuredBus = true;   // собирались ли замеры вообще
  let pos = {};              // id → { l, r, y }: якоря связей
  let lastState = null;

  const OTHER = (k) => !k || k === 'other';

  /* Блоки API. Домены — из atlas.yaml; эндпоинты без домена — «Прочее». Если
   * доменов не объявлено вовсе, блоками становятся сервисы: одна полоса на
   * тысячу эндпоинтов не читается. */
  function apiBlocks(api) {
    const declared = (G.domains || []).filter((d) => !OTHER(d.key));
    const noDomains = !declared.length || api.every((n) => OTHER(n.meta.domain));
    if (noDomains) {
      const bySvc = Object.create(null);   // имя сервиса из данных — не ключ прототипа
      api.forEach((n) => { (bySvc[n.meta.service || '—'] ||= []).push(n); });
      return Object.keys(bySvc).sort((a, b) => bySvc[b].length - bySvc[a].length || a.localeCompare(b))
        .map((s) => ({ key: '', label: s === '—' ? 'Без сервиса' : s, services: [s], eps: bySvc[s] }));
    }
    const out = declared.map((d) => ({ key: d.key, label: d.label, services: d.services || [],
      eps: api.filter((n) => n.meta.domain === d.key) }));
    const known = new Set(declared.map((d) => d.key));
    const rest = Object.create(null);
    api.filter((n) => !known.has(n.meta.domain)).forEach((n) => {
      (rest[OTHER(n.meta.domain) ? 'other' : n.meta.domain] ||= []).push(n);
    });
    Object.keys(rest).sort((a, b) => (a === 'other') - (b === 'other') || a.localeCompare(b))
      .forEach((k) => out.push({ key: k, label: k === 'other' ? 'Прочее' : k, services: [], eps: rest[k] }));
    return out.filter((b) => b.eps.length);
  }

  function computeTouched() {
    touched = new Set();
    G.edges.forEach((e) => {
      if (observed(e)) { touched.add(e.source); touched.add(e.target); }
    });
    G.nodes.forEach((n) => {
      const m = n.meta || {};
      if (m.sessionTouched || num(m.fileEvents) > 0) touched.add(n.id);
    });
    const col = G.collected || {};
    // нет ни одной записи агента о БД или шине — «не задели» сказать нельзя
    // очищенные отчёты: записи агента есть, но соединить их не с чем — замера тоже нет
    measuredDb = !col.reportsCleared
      && (num(col.dbEffects) > 0 || G.nodes.some((n) => n.kind === 'table' && n.meta.sessionTouched));
    measuredBus = !col.reportsCleared && (num(col.busEvents) > 0 || num(col.busEdges) > 0);
  }

  /* Сколько колонок блоков доменов помещается в ширину. */
  function apiCols(width) {
    const free = width - COL_SCR - GAP - GAP_BUS - COL_BUS - GAP - COL_ST;
    return Math.max(1, Math.min(3, Math.floor((free + BLOCK_GAP) / (BLOCK_W + BLOCK_GAP))));
  }

  /* Классы прогона и стенда — отдельно от раскладки: их обновляет опрос раз в
   * секунду, и пересобирать ради этого карту нельзя (подсказка под курсором гасла). */
  function markCls(n, state) {
    let c = '';
    if (n.layer === 'api' && (n.meta.runStatus === 'failed' || (state.runFailed && state.runFailed.has(n.id)))) c += ' m-fail';
    if (state.runTouched && state.runTouched.has(n.id)) c += ' m-touch';
    const live = state.liveOf ? state.liveOf(n) : '';
    if (live === 'down') c += ' m-down';
    else if (live === 'restart') c += ' m-restart';
    return c;
  }

  /* ------------------------------------------------------------------- отрисовка */
  function render(state) {
    lastState = state;
    if (!host || !G) return;
    const W0 = Math.max(820, num(host.clientWidth));
    const byId = {};
    G.nodes.forEach((n) => { byId[n.id] = n; });
    const api = G.nodes.filter((n) => n.layer === 'api');
    const blocks = apiBlocks(api);
    const k = Math.min(apiCols(W0), Math.max(1, blocks.length));
    const X_API = COL_SCR + GAP;
    const X_BUS = X_API + k * BLOCK_W + (k - 1) * BLOCK_GAP + GAP_BUS;
    const X_ST = X_BUS + COL_BUS + GAP;
    const W = X_ST + COL_ST;
    const sel = state.selected || '';
    const ch = state.chain;                   // { nodes:Set, edges:[] } | null
    const hot = (id) => !!ch && ch.nodes.has(id);
    const nodeCls = (n) => {
      let c = 'm-node';
      if (ch) c += hot(n.id) ? ' m-hot' : ' m-dim';
      else if (state.dimmed && state.dimmed(n)) c += ' m-dim';
      return c + markCls(n, state);
    };
    const nodeAttrs = (n, label) => `class="${nodeCls(n)}" data-id="${esc(n.id)}" tabindex="0" role="button"
      aria-label="${esc(label || n.label)}"`;
    const tip = (lines) => `<title>${esc(lines.filter(Boolean).join('\n'))}</title>`;
    const out = [];
    pos = {};
    const colH = (x, t) => out.push(`<text class="m-colh" x="${x}" y="14">${esc(t.toUpperCase())}</text>`);

    /* --- экраны; контролы цепочки раскрыты под своим экраном */
    const screens = G.nodes.filter((n) => n.kind === 'screen');
    const controls = G.nodes.filter((n) => n.kind === 'control');
    colH(0, `Экраны · ${screens.length}`);
    let y = TOP;
    const placedCtl = new Set();
    const controlRows = (list, x0) => list.forEach((c) => {
      y += 17;
      out.push(`<g ${nodeAttrs(c)}>${tip([c.label, c.meta.testid])}
        <text class="m-mono" x="${x0}" y="${y}">↳ ${esc(fit(c.label, COL_SCR - x0 - 12, 11.5, true))}</text></g>`);
      pos[c.id] = { l: 0, r: Math.min(COL_SCR, x0 + twMono('↳ ' + c.label, 11.5) + 6), y: y - 4 };
    });
    screens.forEach((s) => {
      const key = s.id.slice(s.id.indexOf(':') + 1);
      const sp = s.meta.screenPath || s.meta.path;
      const mine = controls.filter((c) => !placedCtl.has(c.id)
        && (c.meta.screen === key || (sp && c.meta.screenPath === sp)));
      mine.forEach((c) => placedCtl.add(c.id));
      out.push(`<g ${nodeAttrs(s)}>${tip([s.label, s.meta.path,
        plural(mine.length, ['элемент', 'элемента', 'элементов'])])}
        <rect class="m-off" x="0" y="${y - 9}" width="11" height="9" rx="1.5"/>
        <text class="m-scr" x="18" y="${y}">${esc(fit(s.label, COL_SCR - 18, 13, false))}</text></g>`);
      pos[s.id] = { l: 0, r: Math.min(COL_SCR, 18 + twSans(s.label, 13) + 6), y: y - 4 };
      const open = mine.filter((c) => hot(c.id) || c.id === sel);
      if (open.length) controlRows(open, 18);
      else {
        out.push(`<text class="m-sub" x="18" y="${y + 15}">${
          esc(plural(mine.length, ['элемент', 'элемента', 'элементов']))}</text>`);
        y += 15;
      }
      // контрол не из цепочки стоит на карте своим экраном
      mine.forEach((c) => { if (!pos[c.id]) pos[c.id] = pos[s.id]; });
      y += 27;
    });
    // контролы, которые ни к одному экрану не привязались, — своей группой, а не
    // от чужого экрана: их связи иначе выходили бы из первой строки колонки
    const orphans = controls.filter((c) => !placedCtl.has(c.id));
    if (orphans.length) {
      out.push(`<text class="m-grp" x="0" y="${y}">Без экрана</text>
        <text class="m-grpn" x="${COL_SCR - 4}" y="${y}" text-anchor="end">${orphans.length}</text>`);
      const headY = y;
      const open = orphans.filter((c) => hot(c.id) || c.id === sel);
      controlRows(open, 0);
      orphans.forEach((c) => { if (!pos[c.id]) pos[c.id] = { l: 0, r: 80, y: headY - 4 }; });
      y += 27;
    }
    const hScr = y;

    /* --- API: блоки, раскладка по колонкам с выравниванием высот */
    colH(X_API, `API · ${plural(api.length, ['эндпоинт', 'эндпоинта', 'эндпоинтов'])}`);
    blocks.forEach((b) => {
      const order = {};
      b.services.forEach((s, i) => { order[s] = i; });
      b.eps.sort((a, c) =>
        ((order[a.meta.service] ?? 99) - (order[c.meta.service] ?? 99))
        || String(a.meta.service || '').localeCompare(String(c.meta.service || ''))
        || String(a.meta.path || a.label).localeCompare(String(c.meta.path || c.label))
        || String(a.meta.method || '').localeCompare(String(c.meta.method || '')));
      b.h = 34 + Math.ceil(b.eps.length / PER_ROW) * PITCH + 28;
    });
    const cols = Array.from({ length: k }, (_, i) => ({ x: X_API + i * (BLOCK_W + BLOCK_GAP), y: TOP - 6, items: [] }));
    // крупные блоки первыми — колонки выходят ровнее; порядок внутри колонки — объявленный
    const declaredOrder = new Map(blocks.map((b, i) => [b, i]));
    blocks.slice().sort((a, b) => b.h - a.h).forEach((b) => {
      const c = cols.reduce((m, x) => (x.y < m.y ? x : m), cols[0]);
      c.items.push(b); c.y += b.h;
    });
    let selXY = null;
    cols.forEach((c) => {
      let yy = TOP - 6;
      c.items.sort((a, b) => declaredOrder.get(a) - declaredOrder.get(b)).forEach((b) => {
        const x0 = c.x;
        // правилом отчёта: при `meta.avgRule: 2` строки exception вне среднего, как у partest
        const avg = window.vehaApi.coverageAverage(G, b.eps).value;
        const cnt = { ok: 0, part: 0, none: 0, unseen: 0 };
        b.eps.forEach((n) => { cnt[cov(n)] += 1; });
        const failed = b.eps.filter((n) => n.meta.runStatus === 'failed').length;
        const full = `${b.eps.length}${avg == null ? '' : ` · ${fmt(avg)} %`}${failed ? ` · ${failed} упало` : ''}`;
        const showAvg = avg != null && BLOCK_W - 8 - twMono(full, 11.5) >= twSans(b.label, 13);
        const right = `${b.eps.length}${showAvg ? ` · ${fmt(avg)} %` : ''}${failed ? ` · ${failed} упало` : ''}`;
        const labelMax = BLOCK_W - 8 - twMono(right, 11.5);
        out.push(`<g class="m-dom"${b.key ? ` data-dom="${esc(b.key)}"` : ''}>${tip([b.label,
          avg == null ? '' : `покрытие в среднем ${fmt(avg)} %`])}
          <text class="m-grp" x="${x0}" y="${yy + 10}">${esc(fit(b.label, labelMax, 13, false))}</text>
          <text class="m-grpn" x="${x0 + BLOCK_W - 4}" y="${yy + 10}" text-anchor="end">${b.eps.length}${
            showAvg ? ` · ${fmt(avg)} %` : ''}${failed ? ` · <tspan class="f">${failed} упало</tspan>` : ''}</text></g>`);
        let bx = x0;
        ['ok', 'part', 'none', 'unseen'].forEach((st) => {
          const w = (BLOCK_W - 4) * cnt[st] / b.eps.length;
          if (w > 0) {
            out.push(`<rect class="m-bar-${st}" x="${bx.toFixed(1)}" y="${yy + 17}" width="${Math.max(w - 1, 0.5).toFixed(1)}" height="3" rx="1"/>`);
            bx += w;
          }
        });
        b.eps.forEach((n, i) => {
          const x = x0 + (i % PER_ROW) * PITCH;
          const y2 = yy + 30 + Math.floor(i / PER_ROW) * PITCH;
          const m = n.meta;
          const st = cov(n);
          out.push(`<g ${nodeAttrs(n)}>${tip([
            n.label,
            `${m.service || '—'} · ${st === 'unseen' ? 'нет в отчёте покрытия'
              : `покрытие ${fmt(m.coverage, Number.isInteger(num(m.coverage)) ? 0 : 1)} %`}`,
            m.runStatus === 'failed' ? `упал в прогоне: ${num(m.runFailed)} из ${num(m.runTotal)}`
              : (m.runStatus === 'passed' ? 'прогон зелёный' : 'в прогоне не вызывался'),
          ])}<rect class="m-${st}" x="${x}" y="${y2}" width="${SQ}" height="${SQ}" rx="2"/>
            <rect class="m-ring" x="${x - 2}" y="${y2 - 2}" width="${SQ + 4}" height="${SQ + 4}" rx="3"/>
            <circle class="m-touchdot" cx="${x + SQ / 2}" cy="${y2 + SQ + 3.5}" r="1.6"/></g>`);
          pos[n.id] = { l: x - 3, r: x + SQ + 3, y: y2 + SQ / 2 };
          if (n.id === sel) selXY = { x, y: y2, x0 };
        });
        yy += b.h;
      });
    });
    const hApi = Math.max(...cols.map((c) => c.y));

    /* --- шина и сервисы: по видам узлов, у каждого вида свой заголовок */
    colH(X_BUS, 'Шина и сервисы');
    y = TOP;
    const bus = G.nodes.filter((n) => n.layer === 'bus');
    const buses = bus.filter((n) => n.kind === 'bus');
    const topics = bus.filter((n) => n.kind === 'topic').sort((a, b) => a.label.localeCompare(b.label));
    const rowList = (items, x, colW, opts) => items.forEach((n) => {
      const o = opts || {};
      const on = o.observedMark && touched.has(n.id);
      const mk = on ? 'm-on' : (o.observedMark && !o.measured ? 'm-na' : 'm-off');
      const mark = o.mono
        ? `<circle class="${mk}" cx="${x + 4}" cy="${y - 4}" r="3.5"/>`
        : `<rect class="${mk}" x="${x}" y="${y - 8}" width="8" height="8" rx="1.5"/>`;
      const fact = !o.observedMark ? '' : (on ? 'встречен на прогоне'
        : (o.measured ? 'на прогоне не встречен' : 'замера на прогоне нет'));
      const label = fit(n.label, colW - 13, o.mono ? 11.5 : 12.5, o.mono);
      out.push(`<g ${nodeAttrs(n)}>${tip([n.label, n.meta.note, fact])}${mark}
        <text class="${o.mono ? 'm-mono' : 'm-svc'}" x="${x + 13}" y="${y}">${esc(label)}</text></g>`);
      pos[n.id] = { l: x - 1, r: x + 13 + (o.mono ? twMono(label, 11.5) : twSans(label, 12.5)) + 4, y: y - 4 };
      y += o.mono ? 19 : 20;
    });
    const groupHead = (x, colW, label, count, node) => {
      const attrs = node ? ` ${nodeAttrs(node)}` : '';
      const lab = fit(label, colW - 30, 13, false);
      out.push(`<g${attrs}>${node ? tip([node.label, node.meta.note]) : ''}<text class="m-grp" x="${x}" y="${y}">${esc(lab)}</text>
        <text class="m-grpn" x="${x + colW}" y="${y}" text-anchor="end">${count}</text></g>`);
      if (node) pos[node.id] = { l: x - 1, r: x + twSans(lab, 13) + 4, y: y - 4 };
      y += 22;
    };
    if (topics.length || buses.length === 1) {
      // одна шина — заголовок над её топиками; несколько — строками, топики общим списком
      groupHead(X_BUS, COL_BUS, buses.length === 1 ? buses[0].label : 'Топики', topics.length,
        buses.length === 1 ? buses[0] : null);
      rowList(topics, X_BUS, COL_BUS, { mono: true, observedMark: true, measured: measuredBus });
      y += 16;
    }
    if (buses.length > 1) {
      groupHead(X_BUS, COL_BUS, 'Шины', buses.length, null);
      rowList(buses, X_BUS, COL_BUS, {});
      y += 16;
    }
    const restKinds = [...new Set(bus.filter((n) => n.kind !== 'topic' && n.kind !== 'bus').map((n) => n.kind))];
    restKinds.forEach((kind) => {
      const items = bus.filter((n) => n.kind === kind);
      groupHead(X_BUS, COL_BUS, Object.hasOwn(KIND_HEAD, kind) ? KIND_HEAD[kind] : kind, items.length, null);
      rowList(items, X_BUS, COL_BUS, {});
      y += 16;
    });
    const hBus = y;

    /* --- хранилища: таблицы — плотный список жетонов, подписаны всегда, но тихо */
    colH(X_ST, 'Хранилища');
    y = TOP;
    const st = G.nodes.filter((n) => n.layer === 'storage');
    const dbs = st.filter((n) => n.kind === 'db');
    const tables = st.filter((n) => n.kind === 'table').sort((a, b) => a.label.localeCompare(b.label));
    if (dbs.length > 1) {
      groupHead(X_ST, COL_ST, 'Базы данных', dbs.length, null);
      rowList(dbs, X_ST, COL_ST, {});
      y += 16;
    }
    if (tables.length) {
      const head = dbs.length === 1 ? dbs[0] : null;
      groupHead(X_ST, COL_ST, head ? head.label : 'Таблицы', tables.length, head);
      y -= 8;
      let x = X_ST;
      tables.forEach((t) => {
        const label = fit(t.label, COL_ST - 10, 10.5, true);
        const w = twMono(label, 10.5) + 10;
        if (x > X_ST && x + w > X_ST + COL_ST) { x = X_ST; y += 21; }
        const on = touched.has(t.id);
        const cls = on ? 'm-tok-on' : (measuredDb ? 'm-tok-off' : 'm-tok-na');
        out.push(`<g ${nodeAttrs(t)}>${tip([t.label,
          on ? 'тесты писали или читали' : (measuredDb ? 'тесты не задели' : 'замера записи нет')])}
          <rect class="${cls}" x="${x.toFixed(1)}" y="${y}" width="${w.toFixed(1)}" height="16" rx="3"/>
          <text class="m-tok" x="${(x + 5).toFixed(1)}" y="${y + 11.5}">${esc(label)}</text></g>`);
        pos[t.id] = { l: x - 1, r: x + w + 1, y: y + 8 };
        x += w + 4;
      });
      y += 21 + 26;
    }
    const rest = st.filter((n) => n.kind !== 'table' && !(n.kind === 'db' && (dbs.length > 1 || tables.length)));
    if (rest.length) {
      groupHead(X_ST, COL_ST, tables.length || dbs.length ? 'Внешнее и прочее' : 'Хранилища', rest.length, null);
      rowList(rest, X_ST, COL_ST, {});
    }
    const hSt = y;

    const H = Math.max(hScr, hApi, hBus, hSt, 200) + 10;

    /* --- связи: цепочка выбранного узла, под узлами */
    const edgeSvg = ch ? edgePaths(ch.edges, '') : '';

    let callout = '';
    if (selXY) {
      const lab = fit((byId[sel] || {}).label || '', BLOCK_W * 2, 12, true);
      const w = twMono(lab, 12) + 16;
      const cx = Math.max(0, Math.min(selXY.x - 4, selXY.x0 + BLOCK_W - w));
      callout = `<rect class="m-sel" x="${selXY.x - 3.5}" y="${selXY.y - 3.5}" width="${SQ + 7}" height="${SQ + 7}" rx="3.5"/>
        <g class="m-callout"><rect x="${cx.toFixed(1)}" y="${selXY.y - 30}" width="${w.toFixed(1)}" height="21" rx="4"/>
        <text x="${(cx + 8).toFixed(1)}" y="${selXY.y - 15.5}">${esc(lab)}</text></g>`;
    }

    host.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 -4 ${W} ${H + 4}" role="group"
      aria-label="Карта системы по слоям: экраны, API, шина и сервисы, хранилища">
      <g class="m-edges">${edgeSvg}</g><g class="m-preview"></g>${out.join('')}${callout}</svg>`;
  }

  /* Прогон и стенд без пересборки карты: только классы у узлов, которые уже есть. */
  function marks(state) {
    if (!host || !G || !lastState) return;
    lastState = { ...lastState, runTouched: state.runTouched, runFailed: state.runFailed, liveOf: state.liveOf };
    const byId = {};
    G.nodes.forEach((n) => { byId[n.id] = n; });
    // без цепочки погашенность считает фильтр, и она может смениться вместе с прогоном
    const redim = !lastState.chain && lastState.dimmed;
    host.querySelectorAll('[data-id]').forEach((el) => {
      const n = byId[el.dataset.id];
      if (!n) return;
      const drop = redim ? /^m-(fail|touch|down|restart|dim)$/ : /^m-(fail|touch|down|restart)$/;
      let cls = el.getAttribute('class').split(/\s+/).filter((c) => !drop.test(c)).join(' ');
      if (redim && lastState.dimmed(n)) cls += ' m-dim';
      el.setAttribute('class', cls + markCls(n, lastState));
    });
  }

  /* Связь — кривая слева направо; внутри одной колонки (топик → воркер) — скобкой. */
  function edgePaths(edges, extra) {
    const best = new Map();
    edges.forEach((e) => {
      if (e.kind === 'contains') return;     // экран и его контрол стоят рядом
      const k = e.source + '\u0001' + e.target;
      const ev = evidence(e);
      const cur = best.get(k);
      if (!cur || EV_RANK[ev] < EV_RANK[cur.ev]) best.set(k, { e, ev });
    });
    const out = [];
    best.forEach(({ e, ev }) => {
      const a = pos[e.source], b = pos[e.target];
      if (!a || !b || a === b) return;
      let d;
      if (Math.abs(a.l - b.l) < 5) {
        const x0 = a.l - 2;
        d = `M${x0},${a.y} C${x0 - 26},${a.y} ${x0 - 26},${b.y} ${x0},${b.y}`;
      } else {
        const [p, q] = a.l <= b.l ? [a, b] : [b, a];
        const x1 = p.r, x2 = q.l, mx = (x1 + x2) / 2;
        d = `M${x1.toFixed(1)},${p.y.toFixed(1)} C${mx.toFixed(1)},${p.y.toFixed(1)} ${mx.toFixed(1)},${q.y.toFixed(1)} ${x2.toFixed(1)},${q.y.toFixed(1)}`;
      }
      out.push(`<path class="m-e ${ev}${extra ? ' ' + extra : ''}" d="${d}"><title>${esc(e.kind)} · ${esc(EV_TEXT[ev])}${
        e.label && !/^(direct|window)$/.test(e.label) ? ' · ' + esc(e.label) : ''}</title></path>`);
    });
    return out.join('');
  }

  /* Наведение: цепочку показываем бледно поверх, не пересобирая карту — иначе
   * курсор каждый раз «теряет» узел под собой. */
  function preview(chain) {
    if (!host) return;
    const g = host.querySelector('.m-preview');
    if (g) g.innerHTML = chain ? edgePaths(chain.edges, 'preview') : '';
  }

  function nodeEl(id) {
    if (!host) return null;
    return [...host.querySelectorAll('[data-id]')].find((el) => el.dataset.id === id) || null;
  }

  /* Где стоит узел, у которого своей строки сейчас нет: контрол — на своём экране. */
  function anchorEl(id) {
    const c = G && G.nodes.find((n) => n.id === id);
    if (!c || c.kind !== 'control') return null;
    const s = G.nodes.find((n) => n.kind === 'screen' && (n.id === 'screen:' + c.meta.screen
      || (c.meta.screenPath && n.meta.screenPath === c.meta.screenPath)));
    return s ? nodeEl(s.id) : null;
  }

  function mount(el, h) {
    host = el;
    handlers = h || {};
    host.addEventListener('click', (e) => {
      const n = e.target.closest('[data-id]');
      if (n) { handlers.pick && handlers.pick(n.dataset.id); return; }
      const d = e.target.closest('[data-dom]');
      if (d) { handlers.domain && handlers.domain(d.dataset.dom); return; }
      if (e.target.closest('.m-dom')) return;
      handlers.pick && handlers.pick(null);
    });
    // клавиатура: Tab по узлам, Enter или пробел — выбрать
    host.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const n = e.target.closest('[data-id]');
      if (!n) return;
      e.preventDefault();
      const id = n.dataset.id;
      handlers.pick && handlers.pick(id);
      const again = nodeEl(id) || anchorEl(id);
      if (again) again.focus({ preventScroll: true });
    });
    let last = '';
    const over = (id) => {
      if (id === last) return;
      last = id;
      handlers.hover && handlers.hover(id || null);
    };
    host.addEventListener('mouseover', (e) => {
      const n = e.target.closest('[data-id]');
      over(n ? n.dataset.id : '');
    });
    host.addEventListener('focusin', (e) => {
      const n = e.target.closest('[data-id]');
      over(n ? n.dataset.id : '');
    });
    host.addEventListener('mouseleave', () => over(''));
    host.addEventListener('focusout', (e) => { if (!host.contains(e.relatedTarget)) over(''); });
  }

  function setGraph(graph) {
    G = graph;
    // сервер кладёт meta всегда; карта не падает и на данных без неё
    (G.nodes || []).forEach((n) => { if (!n.meta || typeof n.meta !== 'object') n.meta = {}; });
    computeTouched();
  }

  let rz = 0;
  window.addEventListener('resize', () => {
    cancelAnimationFrame(rz);
    rz = requestAnimationFrame(() => { if (lastState && host && host.offsetParent) render(lastState); });
  });

  return { mount, setGraph, render, marks, preview, chainOf, edgesToChain, evidence, EV_TEXT, cov, nodeEl,
           get touched() { return touched; } };
}());
