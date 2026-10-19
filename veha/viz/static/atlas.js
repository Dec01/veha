/* Веха — раздел «Карта»: состояние, два вида карты, карточка узла, поиск, палитра.
 *
 * Два вида одной карты в одной палитре:
 *   · «Слои» — по умолчанию: 2D по ходу запроса, рисует map2d.js;
 *   · «Объём» — та же система изометрической сценой на Canvas 2D: зоны на одной
 *     земле, фронт экраном перед ними (приём exploded axonometric).
 * Цвет — только у покрытия и прогона; связи видны у выбранного узла, а не все
 * сразу; подписи — у списков, у цепочки и при наведении.
 *
 * Рендер без библиотек — работает офлайн и в закрытом контуре.
 */
'use strict';

/* Палитра — из CSS, в момент отрисовки: тема идёт за системой, и цвета,
 * прочитанные один раз на старте, рисовали бы светлую сцену в тёмной теме. */
const CSS = getComputedStyle(document.documentElement);
const C = (n) => CSS.getPropertyValue(n).trim();
const PAL = {};
function readPalette() {
  ['accent', 'part', 'fail', 'line', 'line-2', 'ink', 'ink-2', 'ink-3', 'bg', 'bg-alt', 'surface']
    .forEach((k) => { PAL[k] = C('--' + k); });
  PAL.dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
}
readPalette();
// цвета для разметки карточек: переменные, а не значения, — тема меняется на лету
const COV = { full: 'var(--ok)', partial: 'var(--part)', empty: 'var(--line-2)', unseen: 'var(--line-2)' };
/* Упал ли эндпоинт: в последнем отчёте или в идущем прогоне. Один предикат для
 * рамки, пилюли «упало», её счётчика и подсветки. */
const isFailed = (n) => n.meta.runStatus === 'failed' || runFailed.has(n.id);
/* Домен эндпоинта: без домена и «other» сервера — один остров «Прочее». */
const domKey = (n) => (!n.meta.domain || n.meta.domain === 'other' ? 'other' : n.meta.domain);

let mode = 'map';            // map — «Слои» (2D), volume — «Объём» (3D)
let edgeList = [];           // связи структуры: правда об интеграциях

const cv = document.getElementById('scene');
const ctx = cv.getContext('2d');
const tip = document.getElementById('tip');
const panel = document.getElementById('panel');
const mapHost = document.getElementById('map2d');
const mapWrap = document.getElementById('map-wrap');

let G = null, placed = [], index = {};
let view = { rx: 0.42, ry: -0.34, zoom: 0.92, ox: 0, oy: 0 };
let hover = null, selected = null;
let hovDistrict = null;
let liveContainers = {};   // ключ контейнера → его состояние на стенде
let runTouched = new Set(), runFailed = new Set();   // подсветка идущего прогона
let lowDetail = false;     // во время перетаскивания рисуем упрощённо
let plates = [];           // подписи зон: рисуем в самом конце, поверх сцены
const layerOn = { frontend: true, api: true, bus: true, storage: true };
let activeScreen = null;
let activeScreenPath = null;   // путь активного экрана — общий ключ для контролов
let activeShot = null;         // сцена из манифеста скриншотов, стоящая на экране
let filterFn = null;   // предикат из раздела «Эндпоинты»: карта и таблица показывают один срез
let pill = '';         // '' | ok | part | none | fail — что подсветить на карте
let domainSel = '';    // домен, который оставить ярким
let fullChain = false; // карточка: путь целиком, а не соседи

/* ------------------------------------------------------------------ данные */
async function load(refresh) {
  G = await window.vehaApi.graph(refresh);
  if (!activeShot) {
    const shots = G.screens || [];
    activeShot = shots.find((s) => s.featured) || shots[0] || null;
  }
  if (!activeScreen) {
    const alias = G.screenAlias || {};
    const key = Object.keys(alias).find((k) => alias[k] === (activeShot || {}).id);
    const node = (key && G.nodes.find((n) => n.id === 'screen:' + key))
      || G.nodes.find((n) => n.kind === 'screen');
    activeScreen = node ? node.id : null;
    activeScreenPath = (activeShot && activeShot.screenPath)
      || (node && (node.meta.screenPath || node.meta.path)) || null;
  }
  refreshEdges();
  Map2D.setGraph(G);
  layout();
  fitView();
  renderStats(); renderPills(); renderDomainSelect();
  if (selected) {
    // после «Обновить данные» узел мог исчезнуть — карточка не должна висеть на пустом месте
    const again = index[selected.id] || G.nodes.find((n) => n.id === selected.id);
    if (again) openNode(again); else closePanel();
  } else renderSummary();
  document.dispatchEvent(new CustomEvent('atlas:ready', { detail: G }));
  draw();
}

/* Связи структуры — правда об интеграциях. Выведенные наборы (общие пробелы,
 * один тест) — сходство, а не связь: они живут в разделе «Система → Связи». */
function refreshEdges() {
  edgeList = ((G && G.edgeSets && G.edgeSets.structure) || (G && G.edges) || []);
  _chain = {}; _adj = null;
}

/* --------------------------------------------------------------- раскладка «Объёма»
 * Одна горизонтальная плоскость, на ней зоны системы по ходу запроса, а фронт
 * стоит перед ними вертикальным экраном. Ось Y — вертикаль мира: меньше значение,
 * выше объект. Земля на y = 0.
 */
const GROUND = { y: 0, x: 0, z: 0, w: 2600, d: 1560 };

/* Зоны читаются как схема потока: страница → API → шина → хранилища. */
const ZONES = {};                 // заполняется раскладкой под фактический размер
let ZONE_ORDER = [];              // порядок зон от ближней к дальней
const ZONE_LIFT = { bus: 44, storage: 12 };   // высота подиума зоны

/* Ключи зон домена: `dom:<domain>`. Порядок и ярус берём из atlas.yaml, а
 * пропущенные домены (сервис без разметки) добираем из данных, чтобы ни один
 * эндпоинт не потерялся. */
function apiZoneKeys() {
  const declared = (G.domains || []).map((d) => d.key);
  const found = new Set(G.nodes
    .filter((n) => n.layer === 'api')
    .map((n) => domKey(n)));
  const keys = declared.filter((k) => found.has(k));
  [...found].sort().forEach((k) => { if (!keys.includes(k)) keys.push(k); });
  return keys.map((k) => 'dom:' + k);
}

function zoneMeta(key) {
  if (key === 'bus' || key === 'storage') {
    const l = (G.layers || []).find((x) => x.key === key) || {};
    return { label: l.label || key, sub: l.sub || '', tier: 0, layer: key };
  }
  const dk = key.slice(4);
  const d = (G.domains || []).find((x) => x.key === dk);
  return {
    label: d ? d.label : (dk === 'other' ? 'Прочее' : dk),
    sub: ((G.layers || []).find((x) => x.key === 'api') || {}).sub || 'API',
    tier: d ? d.tier : 0,
    layer: 'api',
  };
}
/* Ступень потока: чем дальше от фронта, тем глубже ряд. */
function zoneStage(key) {
  if (key === 'storage') return 3;
  if (key === 'bus') return 2;
  return zoneMeta(key).tier > 0 ? 1 : 0;
}

function zoneLift(key) {
  const m = zoneMeta(key);
  return (ZONE_LIFT[key] ?? 20) + m.tier * 46;   // ярус поднимает палубу
}
const WALL_GAP = 150;             // насколько экран фронта вынесен вперёд карты
const MAP_YAW = -0.34;            // разворот камеры по умолчанию
const WALL = { x: 0, y: 0, w: 600, h: 380, z: 0 };
const DISTRICT_H = 9;    // высота подиума района сервиса
const CONTROL_LIFT = 90; // насколько контрол вынесен перед страницей

let districts = [];   // районы сервисов: подиумы внутри зоны

function layout() {
  buildServiceIndex();
  Object.keys(ZONES).forEach((k) => delete ZONES[k]);   // иначе копятся старые ключи
  placed = [];
  districts = [];
  layoutMap();
  index = Object.fromEntries(placed.map((n) => [n.id, n]));
}

const byLayer = () => {
  const out = {};
  G.nodes.forEach((n) => (out[n.layer] ||= []).push(n));
  return out;
};

/* Сервис узла. Эндпоинты, которых нет в coverage.json, приходят без поля
 * `service` — раскладывать их одной безымянной кучей нельзя. Берём сегмент пути. */
let _svcByKey = {};
function buildServiceIndex() {
  _svcByKey = {};
  G.nodes.forEach((n) => {
    if (n.layer !== 'api' || !n.meta.service) return;
    const seg = (n.meta.path || '').split('/')[3];
    if (seg) _svcByKey[seg.toLowerCase()] = n.meta.service;
    _svcByKey[n.meta.service.toLowerCase()] = n.meta.service;
  });
}
function serviceOf(n) {
  if (n.meta.service) return n.meta.service;
  const seg = (n.meta.path || '').split('/')[3] || '';
  return _svcByKey[seg.toLowerCase()] || seg || '—';
}

/* Зоны не заданы жёстко: сначала укладываем районы каждой зоны в локальные
 * координаты, потом из полученных габаритов собираем композицию на земле. */
function layoutMap() {
  const L = byLayer();
  const packs = {};
  ZONE_ORDER = apiZoneKeys().concat(['bus', 'storage']);
  ZONE_ORDER.forEach((k) => {
    const nodes = k === 'bus' || k === 'storage'
      ? (L[k] || [])
      : (L.api || []).filter((n) => domKey(n) === k.slice(4));
    packs[k] = packLocal(nodes, k);
  });

  const GAP_Z = 150;
  const order = ZONE_ORDER.filter((k) => packs[k].d);

  const place = (key, cx, cz) => {
    const zone = packs[key];
    if (!zone.w) return;
    const lift = zoneLift(key);
    ZONES[key] = { x: cx, z: cz, w: zone.w, d: zone.d, lift };
    zone.districts.forEach((d) => {
      d.x += cx; d.z += cz; d.y = -lift;
      districts.push(d);
    });
    zone.nodes.forEach((n) => {
      n.x += cx; n.z += cz; n.y = -lift - DISTRICT_H;
      placed.push(n);
    });
  };

  // ряд на ступень потока, внутри ряда зоны стоят бок о бок
  const GAP_X = 130;
  const rows = [];
  order.forEach((k) => {
    const stage = zoneStage(k);
    (rows[stage] ||= []).push(k);
  });
  const used = rows.filter(Boolean);
  const rowD = used.map((r) => Math.max(...r.map((k) => packs[k].d)));
  const depth = rowD.reduce((a, d) => a + d, 0) + GAP_Z * Math.max(0, used.length - 1);

  let zCur = -depth / 2;
  used.forEach((row, i) => {
    const rowW = row.reduce((a, k) => a + packs[k].w, 0) + GAP_X * (row.length - 1);
    let xCur = -rowW / 2;
    row.forEach((k) => {
      place(k, xCur + packs[k].w / 2, zCur + rowD[i] / 2);
      xCur += packs[k].w + GAP_X;
    });
    zCur += rowD[i] + GAP_Z;
  });

  // Экран фронта стоит перед картой: он первый по ходу запроса. Позицию
  // считаем в базисе проекции, иначе экран наезжает на город эндпоинтов.
  const front = ZONES[used[0] && used[0][0]] || { x: 0, z: 0, d: 400, w: 400 };
  WALL.z = front.z - front.d / 2 - WALL_GAP;
  const cy = Math.cos(MAP_YAW), sy = Math.sin(MAP_YAW);
  const screenX = (x, z) => x * cy - z * sy;
  let mapMinSX = Infinity;
  Object.values(ZONES).forEach((z) => {
    [-1, 1].forEach((ax) => [-1, 1].forEach((az) => {
      mapMinSX = Math.min(mapMinSX, screenX(z.x + ax * z.w / 2, z.z + az * z.d / 2));
    }));
  });
  const fullLeft = (mapMinSX - 70) / cy + (WALL.z * sy) / cy - WALL.w / 2;
  WALL.x = fullLeft * 0.62;

  // земля ровно по габариту композиции с полем
  const xs = [WALL.x - WALL.w / 2, WALL.x + WALL.w / 2];
  const zs = [WALL.z - CONTROL_LIFT - 40, WALL.z + 50];
  Object.values(ZONES).forEach((z) => {
    xs.push(z.x - z.w / 2, z.x + z.w / 2);
    zs.push(z.z - z.d / 2, z.z + z.d / 2);
  });
  const PADG = 110;
  if (!xs.every(Number.isFinite) || !zs.every(Number.isFinite)) {
    GROUND.x = GROUND.z = 0; GROUND.w = 1800; GROUND.d = 1200;
    return;
  }
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const zmin = Math.min(...zs), zmax = Math.max(...zs);
  GROUND.x = (xmin + xmax) / 2;
  GROUND.z = (zmin + zmax) / 2;
  GROUND.w = xmax - xmin + PADG * 2;
  GROUND.d = zmax - zmin + PADG * 2;

  (L.frontend || []).forEach((n) => {
    if (n.kind === 'screen') {
      placed.push({ ...n, x: WALL.x, y: WALL.y, z: WALL.z, isWall: true });
      return;
    }
    const sl = n.meta.slot || { x: 50, y: 50 };
    const wx = WALL.x + (sl.x / 100 - 0.5) * WALL.w;
    const wy = -WALL.h + WALL.h * (sl.y / 100);
    placed.push({
      ...n, shape: 'control',
      x: wx, y: wy, z: WALL.z - CONTROL_LIFT,
      anchor: { x: wx, y: wy, z: WALL.z },
    });
  });
}

/* Упаковка районов зоны в локальные координаты вокруг нуля.
 * Ширину ряда подбираем под пропорцию 3:2. */
function packLocal(nodes, layer) {
  const empty = { w: 0, d: 0, districts: [], nodes: [] };
  if (!nodes.length) return empty;

  const isApi = layer.startsWith('dom:');
  const groups = {};
  nodes.forEach((n) => { (groups[isApi ? serviceOf(n) : n.kind] ||= []).push(n); });
  const SP = isApi ? 30 : 94;
  const GAP = isApi ? 54 : 50;
  const boxes = Object.entries(groups).map(([key, items]) => {
    const cols = Math.max(1, Math.ceil(Math.sqrt(items.length * 1.6)));
    const rows = Math.ceil(items.length / cols);
    return { key, items, cols, rows, w: cols * SP + GAP, d: rows * SP + GAP };
  }).sort((a, b) => b.d - a.d || b.w - a.w);

  const area = boxes.reduce((acc, b) => acc + b.w * b.d, 0);
  const rowMax = Math.max(...boxes.map((b) => b.w), Math.sqrt(area * 1.9));

  const rows = [];
  let row = { items: [], w: 0, d: 0 };
  boxes.forEach((b) => {
    if (row.items.length && row.w + b.w > rowMax) { rows.push(row); row = { items: [], w: 0, d: 0 }; }
    row.items.push(b); row.w += b.w; row.d = Math.max(row.d, b.d);
  });
  if (row.items.length) rows.push(row);

  const totalD = rows.reduce((acc, r) => acc + r.d, 0);
  const totalW = Math.max(...rows.map((r) => r.w));
  const out = { w: totalW + 60, d: totalD + 60, districts: [], nodes: [] };

  let zCur = -totalD / 2;
  rows.forEach((r) => {
    let xCur = -r.w / 2;
    r.items.forEach((b) => {
      const cx = xCur + b.w / 2, cz = zCur + b.d / 2;
      out.districts.push({
        layer, key: b.key, count: b.items.length, y: 0,
        x: cx, z: cz, w: b.w - 16, d: b.d - 16,
      });
      b.items.forEach((n, i) => {
        const ix = (i % b.cols) - (b.cols - 1) / 2;
        const iz = Math.floor(i / b.cols) - (b.rows - 1) / 2;
        out.nodes.push({
          ...n, group: b.key, zone: layer,
          x: cx + ix * SP, y: 0, z: cz + iz * SP, shape: shapeFor(n),
        });
      });
      xCur += b.w;
    });
    zCur += r.d;
  });
  return out;
}

function shapeFor(n) {
  if (n.kind === 'table') return 'stack';
  if (n.kind === 'topic') return 'disc';
  if (n.kind === 'db' || n.kind === 'store') return 'silo';
  if (n.kind === 'backend' || n.kind === 'bus' || n.kind === 'auth'
      || n.kind === 'service' || n.kind === 'storage') return 'cube';
  return 'building';
}

/* Видимая «макушка» узла — к ней цепляем связи, подписи и попадание курсора. */
function nodeTop(n) {
  return { x: n.x, y: n.y - objHeight(n), z: n.z };
}

/* Узел, за которым стоит контейнер: сервис, внешняя система, шина, хранилище.
 * Эндпоинты сюда не относятся — у них свой цвет по покрытию. */
function containerKeyOf(n) {
  if (n.layer === 'api' || n.kind === 'control' || n.kind === 'screen') return '';
  return (n.id || '').startsWith('svc:') ? n.id.slice(4) : (n.meta.container || '');
}

/* Высота объекта. Для эндпоинта — нагрузка в логарифме. */
function objHeight(n) {
  switch (n.shape) {
    case 'building': {
      const load = n.meta.calls || n.meta.runTotal || 0;
      return 9 + Math.min(104, Math.log10(load + 1) * 40);
    }
    case 'stack': return 46;
    case 'silo': return 54;
    case 'disc': return 16;
    case 'cube': return 44;
    default: return 0;
  }
}

/* Опорные точки сцены: габарит земли и экрана фронта. */
function sceneAnchors() {
  const out = [];
  Object.entries(ZONES).forEach(([key, z]) => {
    if (!layerOn[zoneMeta(key).layer]) return;
    [-1, 1].forEach((sx) => [-1, 1].forEach((sz) => {
      out.push({ x: z.x + (sx * z.w) / 2, y: -z.lift, z: z.z + (sz * z.d) / 2 });
    }));
  });
  if (layerOn.frontend) {
    [-1, 1].forEach((sx) => {
      out.push({ x: WALL.x + (sx * WALL.w) / 2, y: -WALL.h, z: WALL.z });
      out.push({ x: WALL.x + (sx * WALL.w) / 2, y: WALL.y, z: WALL.z - CONTROL_LIFT });
    });
  }
  return out;
}

const PAD = { left: 24, right: 24, top: 44, bottom: 24 };
function fitView() {
  if (!G || !placed.length || mode !== 'volume') return;
  // холст может быть ещё не размечен (0×0) — тогда масштаб уходит в минус
  if (cv.clientWidth < 80 || cv.clientHeight < 80) return;
  const save = { ...view };
  view.zoom = 1; view.ox = 0; view.oy = 0;
  const pts = [];
  placed.forEach((n) => { if (visible(n) && !n.isWall) pts.push(project(nodeTop(n))); });
  sceneAnchors().forEach((q) => pts.push(project(q)));
  if (!pts.length) { view = save; return; }
  const cx0 = cv.clientWidth / 2, cy0 = cv.clientHeight / 2;
  const xs = pts.map((p) => p.sx - cx0), ys = pts.map((p) => p.sy - cy0);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  const availW = cv.clientWidth - PAD.left - PAD.right;
  const availH = cv.clientHeight - PAD.top - PAD.bottom;
  const zoom = Math.max(0.15, Math.min(
    Math.max(1, availW) / Math.max(1, x1 - x0),
    Math.max(1, availH) / Math.max(1, y1 - y0), 1.6));
  const targetX = PAD.left + availW / 2 - cx0;
  const targetY = PAD.top + availH / 2 - cy0;
  view.zoom = zoom;
  view.ox = targetX - ((x0 + x1) / 2) * zoom;
  view.oy = targetY - ((y0 + y1) / 2) * zoom;
}

/* ------------------------------------------------------------- проекция 3D */
/* Лёгкий намёк на глубину: сильная перспектива раздувает ближний объект. */
let CAM_DIST = 5200;

function project(p) {
  const cy = Math.cos(view.ry), sy = Math.sin(view.ry);
  const cx = Math.cos(view.rx), sx = Math.sin(view.rx);
  let x = p.x * cy - p.z * sy;
  let z = p.x * sy + p.z * cy;
  const y = p.y * cx - z * sx;
  z = p.y * sx + z * cx;
  // за камерой знаменатель уходит в минус — подрезаем
  const persp = CAM_DIST / Math.max(CAM_DIST * 0.2, CAM_DIST + z);
  return {
    sx: cv.clientWidth / 2 + x * persp * view.zoom + view.ox,
    sy: cv.clientHeight / 2 + y * persp * view.zoom + view.oy,
    k: persp * view.zoom,
    depth: z,
  };
}

/* Обработчики вешаем только на то, что есть в разметке. */
function bind(id, event, fn) {
  const el = document.getElementById(id);
  if (el) el.addEventListener(event, fn);
}

/* Цвет узла в «Объёме»: прогон и стенд важнее статической раскраски, у эндпоинта —
 * покрытие, у остального — чернила: слой узнаётся по месту, а не по цвету. */
function nodeColor(n) {
  if (runFailed.has(n.id)) return PAL.fail;
  const live = liveContainers[containerKeyOf(n)];
  if (live && (live.down || live.restarting)) return live.down ? PAL.fail : PAL.part;
  if (n.layer !== 'api') return PAL['ink-3'];
  return ({ full: PAL.accent, partial: PAL.part })[n.meta.status] || PAL['line-2'];
}

const _rgbCache = {};
function hexRgb(hex) {
  if (_rgbCache[hex]) return _rgbCache[hex];
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(String(hex).trim());
  const v = m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [140, 150, 180];
  return (_rgbCache[hex] = v);
}

/* Видимость в «Объёме»: экран и его контролы. Фильтры не прячут, а гасят —
 * см. passes(). */
function visible(n) {
  if (!layerOn[n.layer]) return false;
  if (n.kind === 'screen') return n.id === activeScreen;
  if (n.layer === 'frontend' && n.kind === 'control') {
    // ключ экрана (ручная карта) или нормализованный путь (рекордер) — то же
    // правило, что у карты по слоям
    const key = activeScreen ? activeScreen.slice(activeScreen.indexOf(':') + 1) : '';
    const byKey = key && n.meta.screen === key;
    const byPath = activeScreenPath && n.meta.screenPath === activeScreenPath;
    if ((key || activeScreenPath) && !byKey && !byPath) return false;
  }
  return true;
}

/* Подсветка раздела: пилюли, домен и фильтр из таблицы. Касается эндпоинтов —
 * таблицы и топики и так нейтральны, гасить их по методу HTTP бессмысленно. */
function passes(n) {
  if (n.layer !== 'api') return true;
  const m = n.meta || {};
  if (filterFn && !filterFn(n)) return false;
  if (domainSel && domKey(n) !== domainSel) return false;
  if (pill === 'fail') return isFailed(n);
  if (pill) return Map2D.cov(n) === pill;
  return true;
}

/* Цепочка, которую сейчас показывает карта: соседи выбранного узла или путь
 * целиком — по кнопке в карточке. */
function currentChain(id) {
  if (!id || !G) return null;
  if (!fullChain) return Map2D.chainOf(G, id);
  const c = chain(id);
  return Map2D.edgesToChain(id, [...c.edges].map((i) => edgeList[i]));
}

/* ------------------------------------------------- «Объём»: сцена --------*/
function drawGround() {
  const { y, w, d } = GROUND;
  const cxg = GROUND.x, czg = GROUND.z;
  const corners = [
    { x: cxg - w / 2, y, z: czg - d / 2 }, { x: cxg + w / 2, y, z: czg - d / 2 },
    { x: cxg + w / 2, y, z: czg + d / 2 }, { x: cxg - w / 2, y, z: czg + d / 2 },
  ].map(project);
  S3.polyPath(ctx, corners);
  ctx.fillStyle = PAL['bg-alt'];
  ctx.fill();
  ctx.strokeStyle = PAL.line;
  ctx.lineWidth = 1;
  ctx.stroke();
}

/* Зона системы: плоский подиум с кромкой и заголовком. */
function drawZone(key) {
  const z = ZONES[key];
  S3.pad(ctx, project, {
    x: z.x, y: 0, z: z.z, w: z.w, d: z.d, h: z.lift,
    rgb: hexRgb(PAL['line-2']), r: 34, top: PAL.dark ? 0.35 : 0.5, rim: 1,
    rimColor: PAL['line-2'],
  });
  const m = zoneMeta(key);
  plates.push({
    p: project({ x: z.x - z.w / 2, y: -z.lift, z: z.z - z.d / 2 }),
    title: m.label, sub: m.sub,
  });
}

/* Район сервиса — подиум внутри зоны с именем и числом эндпоинтов. */
function drawDistrict(d) {
  const on = hovDistrict === d;
  S3.pad(ctx, project, {
    x: d.x, y: d.y, z: d.z, w: d.w, d: d.d, h: DISTRICT_H,
    rgb: hexRgb(PAL.line), r: 12, top: on ? 0.9 : 0.55, rim: on ? 1.4 : 0.8,
    rimColor: on ? PAL['ink-3'] : PAL['line-2'],
  });
  if (d.count >= 4 || on) {
    const c = project({ x: d.x, y: d.y - DISTRICT_H, z: d.z - d.d / 2 });
    ctx.font = Math.max(9, 10 * c.k) + 'px ' + C('--sans');
    ctx.fillStyle = on ? PAL.ink : PAL['ink-3'];
    ctx.textAlign = 'center';
    ctx.fillText(d.key + ' · ' + d.count, c.sx, c.sy - 7);
    ctx.textAlign = 'left';
  }
}

/* Фронт — вертикальный экран, стоящий на земле перед картой. На экране висит
 * настоящий скриншот страницы из манифеста попиксельного сравнения. Canvas не
 * умеет рисовать картинку в произвольный четырёхугольник, поэтому накладываем
 * изображение аффинным преобразованием по трём углам. */
const _shots = new Map();   // url → HTMLImageElement | 'loading' | 'error'

function screenImage(url) {
  if (!url) return null;
  const cached = _shots.get(url);
  if (cached === 'loading' || cached === 'error') return null;
  if (cached) return cached;
  _shots.set(url, 'loading');
  const img = new Image();
  img.onload = () => { _shots.set(url, img); draw(); };
  img.onerror = () => _shots.set(url, 'error');
  img.src = url;
  return null;
}

function drawWall(screen) {
  const px = (u) => WALL.x + (u / 100 - 0.5) * WALL.w;
  const py = (v) => (WALL.y - WALL.h) + WALL.h * (v / 100);
  const quad = (x, y, w, h) => [
    project({ x: px(x), y: py(y), z: WALL.z }),
    project({ x: px(x + w), y: py(y), z: WALL.z }),
    project({ x: px(x + w), y: py(y + h), z: WALL.z }),
    project({ x: px(x), y: py(y + h), z: WALL.z }),
  ];
  const poly = (pts, fill, stroke, lw = 1) => {
    S3.polyPath(ctx, pts);
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = lw; ctx.stroke(); }
  };

  // подставка: экран должен стоять, а не парить
  S3.box(ctx, project, {
    x: WALL.x, y: 0, z: WALL.z, w: WALL.w * 1.06, h: 12, d: 56,
    rgb: hexRgb(PAL['line-2']), alpha: 0.9, rimColor: PAL['line-2'],
  });
  poly(quad(-3.4, -3.6, 106.8, 107.4), PAL.surface, PAL['ink-3'], 1.5);

  const shot = screenImage(activeShot && activeShot.image);
  const face = quad(0, 0, 100, 100);
  wallOccluder = {
    pts: quad(-3.4, -3.6, 106.8, 107.4),
    k: project({ x: WALL.x, y: py(50), z: WALL.z }).k,
  };
  if (shot) {
    drawImageOnQuad(shot, face);
    poly(face, null, PAL.line, 1);
  } else {
    poly(face, PAL['bg-alt'], PAL.line, 1);
    const c = project({ x: px(50), y: py(50), z: WALL.z });
    ctx.fillStyle = PAL['ink-3'];
    ctx.font = Math.max(9, 11 * c.k) + 'px ' + C('--sans');
    ctx.textAlign = 'center';
    ctx.fillText(activeShot ? 'скриншот грузится…' : 'нет снимка экрана', c.sx, c.sy);
    ctx.textAlign = 'left';
  }

  plates.push({
    p: project({ x: px(0), y: py(0), z: WALL.z }),
    title: 'Экран',
    sub: activeShot ? `${activeShot.title} · ${activeShot.path}` : (screen.label || ''),
  });
}

function drawImageOnQuad(img, q) {
  const [tl, tr, , bl] = q;
  const w = img.naturalWidth || img.width;
  const h = img.naturalHeight || img.height;
  if (!w || !h) return;
  ctx.save();
  S3.polyPath(ctx, q);
  ctx.clip();
  ctx.transform(
    (tr.sx - tl.sx) / w, (tr.sy - tl.sy) / w,
    (bl.sx - tl.sx) / h, (bl.sy - tl.sy) / h,
    tl.sx, tl.sy,
  );
  ctx.drawImage(img, 0, 0, w, h);
  ctx.restore();
}

function drawPlates() {
  plates.forEach(({ p, title, sub }) => {
    ctx.textAlign = 'left';
    ctx.fillStyle = PAL.ink;
    ctx.font = '600 ' + Math.max(11, 13 * p.k) + 'px ' + C('--sans');
    ctx.fillText(title, p.sx + 8, p.sy - 22);
    ctx.fillStyle = PAL['ink-3'];
    ctx.font = Math.max(9, 10.5 * p.k) + 'px ' + C('--sans');
    ctx.fillText(sub, p.sx + 8, p.sy - 9);
  });
}

/* ----------------------------------------------------------------- отрисовка */
function draw() {
  if (!G) return;
  renderSceneHint();
  if (mode === 'map') { drawMap(); return; }
  readPalette();
  S3.theme(PAL.dark);
  const dpr = window.devicePixelRatio || 1;
  cv.width = cv.clientWidth * dpr;
  cv.height = cv.clientHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cv.clientWidth, cv.clientHeight);

  plates = [];
  tagSlots = [];
  pendingTags = [];
  wallOccluder = null;      // ставится заново в drawWall, если экран рисуется

  const P = {};
  screenPts = new Map();
  placed.forEach((n) => {
    if (!visible(n)) return;
    const p = project(nodeTop(n));
    P[n.id] = { n, p };
    screenPts.set(n.id, p);
  });

  const focus = selected || hover;
  const ch = focus ? currentChain(focus.id) : null;
  const byDepth = (u, v) => v.p.depth - u.p.depth;

  drawGround();
  // Всё стоит на одной земле, поэтому порядок задаёт только глубина.
  const zones = Object.keys(ZONES).filter((k) => layerOn[zoneMeta(k).layer]);
  zones.sort((a, b) =>
    project({ x: ZONES[b].x, y: 0, z: ZONES[b].z }).depth
    - project({ x: ZONES[a].x, y: 0, z: ZONES[a].z }).depth);

  zones.forEach((key) => {
    drawZone(key);
    const inZone = districts.filter((d) => d.layer === key)
      .map((d) => ({ d, depth: project({ x: d.x, y: d.y, z: d.z }).depth }))
      .sort((u, v) => v.depth - u.depth);
    inZone.forEach(({ d }) => {
      drawDistrict(d);
      Object.values(P).filter((e) => e.n.group === d.key && e.n.zone === key)
        .sort(byDepth).forEach(({ n, p }) => drawObject(n, p, focus, ch));
    });
  });

  drawEdges(P, focus, ch);
  // Экран рисуем после связей: он непрозрачный и обязан их закрывать.
  const sc = placed.find((n) => n.id === activeScreen);
  if (sc && layerOn.frontend) drawWall(sc);
  Object.values(P).filter(({ n }) => n.layer === 'frontend').sort(byDepth)
    .forEach(({ n, p }) => drawObject(n, p, focus, ch));

  flushTags();
  drawPlates();
}

/* «Слои»: состояние для map2d.js. Связи — только у выбранного узла. */
const liveOf = (n) => {
  const live = liveContainers[containerKeyOf(n)];
  return !live ? '' : (live.down ? 'down' : (live.restarting ? 'restart' : ''));
};
function drawMap() {
  Map2D.render({
    selected: selected ? selected.id : '',
    chain: selected ? currentChain(selected.id) : null,
    dimmed: (n) => !passes(n),
    runTouched, runFailed, liveOf,
  });
}
/* Прогон и стенд опрашиваются раз в секунду: на «Слоях» меняем только классы
 * узлов — пересборка карты гасила подсказку под курсором. */
function redrawMarks() {
  if (mode === 'map') Map2D.marks({ runTouched, runFailed, liveOf });
  else draw();
}

/* Связи в «Объёме»: только цепочка выделенного узла, одним цветом чернил;
 * происхождение — штрихом, как и на «Слоях». Без цепочки линий нет. */
function drawEdges(P, focus, ch) {
  if (!ch) return;
  const dash = { measured: [], declared: [6, 4], inferred: [1.5, 3.5] };
  ch.edges.forEach((e) => {
    if (e.kind === 'contains') return;
    const A = P[e.source], B = P[e.target];
    if (!A || !B) return;
    const a = nodeTop(A.n), b = nodeTop(B.n);
    const lift = Math.hypot(a.x - b.x, a.z - b.z) * 0.22 + 40;
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - lift, z: (a.z + b.z) / 2 };
    const N = 18;
    ctx.beginPath();
    for (let i = 0; i <= N; i++) {
      const t = i / N, u = 1 - t;
      const p = project({
        x: u * u * a.x + 2 * u * t * mid.x + t * t * b.x,
        y: u * u * a.y + 2 * u * t * mid.y + t * t * b.y,
        z: u * u * a.z + 2 * u * t * mid.z + t * t * b.z,
      });
      if (i) ctx.lineTo(p.sx, p.sy); else ctx.moveTo(p.sx, p.sy);
    }
    ctx.setLineDash(dash[Map2D.evidence(e)]);
    ctx.strokeStyle = PAL['ink-2'];
    ctx.globalAlpha = selected ? 0.8 : 0.45;
    ctx.lineWidth = 1.3;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
  });
}

/* Уровень детализации по экранному размеру объекта: мелким градиенты не нужны. */
function lodOf(p, focus) {
  if (focus) return 2;
  if (lowDetail) return 0;
  const px = 13 * p.k;
  return px < 5 ? 0 : (px < 11 ? 1 : 2);
}

function drawObject(n, p, focus, ch) {
  if (n.isWall) return;
  const isFocus = focus && n.id === focus.id;
  const onChain = !!ch && ch.nodes.has(n.id);
  const dim = ch ? !onChain : !passes(n);
  const col = nodeColor(n);
  const rgb = hexRgb(col);
  const alpha = dim ? 0.22 : 1;
  const lod = lodOf(p, isFocus || onChain || (hover && hover.id === n.id));
  // упавший в прогоне эндпоинт — красная кромка поверх цвета покрытия
  const failRim = n.layer === 'api' && (n.meta.runStatus === 'failed' || runFailed.has(n.id))
    ? PAL.fail : (runTouched.has(n.id) ? PAL.ink : null);

  if (n.shape === 'control') {
    if (n.anchor) {
      const a = project(n.anchor);
      ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(p.sx, p.sy);
      ctx.strokeStyle = PAL['line-2'];
      ctx.globalAlpha = dim ? 0.3 : 1;
      ctx.setLineDash([3, 3]); ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]);
    }
    const w = 16 * p.k + 12, h = 9 * p.k + 8;
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.roundRect(p.sx - w, p.sy - h, w * 2, h * 2, 5);
    ctx.fillStyle = PAL.surface;
    ctx.strokeStyle = isFocus ? PAL.ink : PAL['ink-3'];
    ctx.lineWidth = isFocus ? 2 : 1;
    ctx.fill(); ctx.stroke();
    ctx.globalAlpha = 1;
    if (!dim) {
      drawLabel(n.label, p.sx, p.sy + 3,
                { align: 'center', size: Math.max(9, 11 * p.k), strong: isFocus });
    }
    return;
  }

  if (n.shape === 'silo') {
    S3.cylinder(ctx, project, { x: n.x, y: n.y, z: n.z, r: 21, h: objHeight(n), rgb, alpha, dim, lod });
  } else if (n.shape === 'disc') {
    S3.cylinder(ctx, project, { x: n.x, y: n.y, z: n.z, r: 30, h: objHeight(n), rgb, alpha, dim, lod });
  } else if (n.shape === 'stack') {
    S3.stack(ctx, project, {
      x: n.x, y: n.y, z: n.z, w: 58, d: 42, h: 8, gap: 7,
      count: lod === 0 ? 2 : 4, rgb, alpha, dim, lod,
    });
  } else if (n.shape === 'cube') {
    S3.box(ctx, project, { x: n.x, y: n.y, z: n.z, w: 52, d: 40, h: objHeight(n), rgb, alpha, dim, lod });
  } else {
    // «здание» эндпоинта: сечение чуть растёт с числом тестов на нём
    const side = 9.5 + Math.min(6, (n.meta.testCount || 0) * 0.42);
    S3.box(ctx, project, {
      x: n.x, y: n.y, z: n.z, w: side, d: side, h: objHeight(n),
      rgb, alpha, dim, lod, rimColor: failRim, rimWidth: failRim ? 2 : null,
    });
  }

  // Бирки — только у цепочки и под курсором: подпись на каждом объекте
  // превращала сцену в кашу.
  if ((isFocus || onChain || (hover && hover.id === n.id)) && !dim) {
    placeTag({
      x: p.sx, y: p.sy, k: p.k, text: n.label.slice(0, 36),
      size: isFocus ? Math.max(11.5, 13 * p.k) : Math.max(10, 11 * p.k),
      strong: isFocus,
      color: PAL.ink, bg: PAL.surface, line: isFocus ? PAL.ink : PAL['line-2'],
    }, isFocus || onChain);
  }
}

/* Подпись с подложкой — у контролов на экране. */
function drawLabel(text, x, y, opts) {
  const o = opts || {};
  const size = o.size || 11;
  const t = String(text).slice(0, 42);
  ctx.font = (o.strong ? '600 ' : '') + size + 'px ' + C('--sans');
  const w = ctx.measureText(t).width;
  const px = o.align === 'center' ? x - w / 2 : x;
  ctx.fillStyle = PAL.ink;
  ctx.textAlign = 'left';
  ctx.fillText(t, px, y + 1);
}

/* Бирки не рисуем на месте, а копим и выкладываем в конце кадра без пересечений:
 * пробуем несколько высот выноски и уступаем место соседу, кроме цепочки. */
let tagSlots = [];
let pendingTags = [];
/* Экран фронта непрозрачен: подпись узла за ним не рисуем. */
let wallOccluder = null;

function pointInPolyXY(x, y, pts) {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const xi = pts[i].sx, yi = pts[i].sy, xj = pts[j].sx, yj = pts[j].sy;
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

function occluded(o) {
  return !!wallOccluder && o.k != null && o.k < wallOccluder.k
    && pointInPolyXY(o.x, o.y, wallOccluder.pts);
}

function placeTag(o, force) {
  pendingTags.push({ o, force });
}

function flushTags() {
  const hitBox = (r) => tagSlots.some((s) =>
    r.x < s.x + s.w + 3 && r.x + r.w + 3 > s.x
    && r.y < s.y + s.h + 3 && r.y + r.h + 3 > s.y);
  pendingTags.sort((a, b) => (b.force ? 1 : 0) - (a.force ? 1 : 0));
  pendingTags.forEach(({ o, force }) => {
    if (occluded(o)) return;
    for (const lift of [20, 40, 62, 86, 112]) {
      const box = S3.tagBox(ctx, { ...o, lift });
      if (!hitBox(box)) {
        tagSlots.push(box);
        S3.tag(ctx, { ...o, lift });
        return;
      }
    }
    if (force) {
      const box = S3.tagBox(ctx, { ...o, lift: 20 });
      tagSlots.push(box);
      S3.tag(ctx, { ...o, lift: 20 });
    }
  });
  pendingTags = [];
}

/* Структурные связи — это поток запроса, их обходим на всю глубину. Связи из
 * логов — факт о контейнере, а не о вызове: их берём только на один шаг. */
const FLOW_KINDS = new Set(['calls', 'contains', 'publish', 'consume',
                            'publishes', 'consumes',
                            'writes', 'reads', 'storage', 'depends', 'flow']);

let _chain = {}, _adj = null;
function chain(id) {
  if (_chain[id]) return _chain[id];
  if (!_adj) {
    _adj = { f: {}, b: {} };
    edgeList.forEach((e, i) => {
      const flow = FLOW_KINDS.has(e.kind);
      (_adj.f[e.source] ||= []).push([e.target, i, flow]);
      (_adj.b[e.target] ||= []).push([e.source, i, flow]);
    });
  }
  const nodes = new Map([[id, 0]]);
  const edges = new Set();
  const related = new Set();     // похожие: один шаг, в поток дальше не идут
  for (const dir of ['f', 'b']) {
    const seen = new Set([id]);
    let front = [id], depth = 0;
    while (front.length && depth < 12) {
      depth += 1;
      const next = [];
      front.forEach((cur) => (_adj[dir][cur] || []).forEach(([to, ei, flow]) => {
        if (!flow) {
          if (cur !== id) return;      // похожесть — только от выбранного узла
          edges.add(ei);
          related.add(to);
          if (!nodes.has(to)) nodes.set(to, dir === 'f' ? depth : -depth);
          return;
        }
        edges.add(ei);
        if (seen.has(to)) return;
        seen.add(to); next.push(to);
        if (!nodes.has(to)) nodes.set(to, dir === 'f' ? depth : -depth);
      }));
      front = next;
    }
  }
  return (_chain[id] = {
    nodes, edges, related,
    flow: new Set([...nodes.keys()].filter((k) => !related.has(k))),
    max: Math.max(...nodes.values(), 1),
  });
}

/* ------------------------------------------------------------ «Объём»: мышь */
/* Экранные точки узлов последнего кадра: попадание считаем по ним. */
let screenPts = new Map();

function hit(mx, my) {
  let best = null, bestD = 18;
  placed.forEach((n) => {
    if (!visible(n) || n.isWall) return;
    const p = screenPts.get(n.id) || project(nodeTop(n));
    const d = Math.hypot(p.sx - mx, p.sy - my);
    const rad = n.shape === 'control' ? 24 * p.k : 13 * p.k + 5;
    if (d < Math.max(bestD, rad)) { best = n; bestD = d; }
  });
  return best;
}

/* Район под курсором: наведение подсвечивает весь сервис целиком. */
function districtAt(mx, my) {
  for (const d of districts) {
    if (!layerOn[d.layer]) continue;
    const y = d.y - 16 - DISTRICT_H;
    const pts = [
      { x: d.x - d.w / 2, y, z: d.z - d.d / 2 }, { x: d.x + d.w / 2, y, z: d.z - d.d / 2 },
      { x: d.x + d.w / 2, y, z: d.z + d.d / 2 }, { x: d.x - d.w / 2, y, z: d.z + d.d / 2 },
    ].map(project);
    if (pointInPolyXY(mx, my, pts)) return d;
  }
  return null;
}

let drag = null;
cv.addEventListener('mousedown', (e) => {
  drag = { x: e.clientX, y: e.clientY, rx: view.rx, ry: view.ry,
           ox: view.ox, oy: view.oy, pan: e.shiftKey || e.button === 1, moved: false };
  lowDetail = true;
  cv.classList.add('dragging');
});
let hoverRaf = 0;
window.addEventListener('mousemove', (e) => {
  if (mode !== 'volume') return;
  if (!drag && hoverRaf) return;      // наведение считаем не чаще кадра
  if (!drag) hoverRaf = requestAnimationFrame(() => { hoverRaf = 0; });
  if (drag) {
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.pan) { view.ox = drag.ox + dx; view.oy = drag.oy + dy; }
    else {
      view.ry = drag.ry + dx * 0.005;
      view.rx = Math.max(-1.1, Math.min(1.1, drag.rx + dy * 0.005));
    }
    draw();
    return;
  }
  if (e.target !== cv) {             // курсор над интерфейсом — сцена его не видит
    if (hover || hovDistrict) { hover = null; hovDistrict = null; draw(); }
    tip.style.display = 'none';
    return;
  }
  const rect = cv.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const n = hit(mx, my);
  const h = n ? null : districtAt(mx, my);
  if (n !== hover || h !== hovDistrict) { hover = n; hovDistrict = h; draw(); }
  if (!n && h) {
    showTip(e, h.key, `${recNum(h.count)} эндпоинтов · клик по объекту — цепочка`);
    return;
  }
  if (n) showTip(e, n.label, tipSub(n));
  else tip.style.display = 'none';
});
window.addEventListener('mouseup', (e) => {
  if (lowDetail) { lowDetail = false; if (mode === 'volume') draw(); }
  if (drag && !drag.moved && e.target === cv) {
    const rect = cv.getBoundingClientRect();
    const n = hit(e.clientX - rect.left, e.clientY - rect.top);
    if (n) openNode(n); else closePanel();
  }
  drag = null; cv.classList.remove('dragging');
});
cv.addEventListener('wheel', (e) => {
  e.preventDefault();
  view.zoom = Math.max(0.2, Math.min(4, view.zoom * (e.deltaY < 0 ? 1.12 : 0.89)));
  draw();
}, { passive: false });

function showTip(e, title, sub) {
  tip.style.display = 'block';
  tip.style.left = Math.min(e.clientX + 14, innerWidth - 360) + 'px';
  tip.style.top = (e.clientY + 16) + 'px';
  tip.innerHTML = `<div class="t">${esc(title)}</div><div class="sub">${esc(sub)}</div>`;
}
function tipSub(n) {
  if (n.layer !== 'api') return n.meta.testid || KIND_LABEL[n.kind] || n.kind;
  const c = n.meta.coverage;
  return `${n.meta.subtype || 'подтип не определён'} · покрытие ${c == null ? '—' : recNum(c) + '%'}${
    n.meta.runStatus === 'failed' ? ' · упал в прогоне' : ''}`;
}

/* ---------------------------------------------------------------- «Слои»: мышь */
Map2D.mount(mapHost, {
  pick(id) {
    if (!id) { if (selected) closePanel(); return; }
    const n = index[id] || (G && G.nodes.find((x) => x.id === id));
    if (!n) return;
    if (selected && selected.id === id) closePanel(); else openNode(n);
  },
  hover(id) {
    // при выбранном узле наведение не перекрашивает карту: цепочка уже видна
    if (selected || !G) { Map2D.preview(null); return; }
    Map2D.preview(id ? Map2D.chainOf(G, id) : null);
  },
  domain(key) {
    domainSel = domainSel === key ? '' : key;
    renderDomainSelect(); draw();
  },
});

/* ------------------------------------------------------------------ карточка */
const KIND_LABEL = {
  endpoint: 'Эндпоинт', control: 'Элемент интерфейса', screen: 'Экран',
  topic: 'Топик шины', table: 'Таблица', store: 'Хранилище',
  backend: 'Сервис', bus: 'Шина', auth: 'Аутентификация',
  db: 'База данных', external: 'Внешняя система', storage: 'Хранилище',
};
const MONO_KINDS = new Set(['endpoint', 'table', 'topic', 'store']);

/* Экран на стене «Объёма» — тот, что выбран сейчас (сам экран или его элемент):
 * выбор экрана или его элемента на любой из двух карт ставит на стену его снимок. */
function showScreenOf(n) {
  let node = null;
  if (n.kind === 'screen') node = n;
  else if (n.kind === 'control') {
    node = (n.meta.screen && G.nodes.find((x) => x.id === 'screen:' + n.meta.screen))
      || (n.meta.screenPath && G.nodes.find((x) => x.kind === 'screen'
        && x.meta.screenPath === n.meta.screenPath)) || null;
  }
  if (!node) return;
  const key = node.id.slice(node.id.indexOf(':') + 1);
  const shot = (G.screens || []).find((s) => s.id === (G.screenAlias || {})[key]) || null;
  activeScreen = node.id;
  activeShot = shot;
  activeScreenPath = (shot && shot.screenPath) || node.meta.screenPath || node.meta.path || null;
}

async function openNode(n) {
  const again = selected && selected.id === n.id;
  selected = n;
  if (!again) fullChain = false;
  showScreenOf(n);
  draw();
  announceSelection();      // разделы подсветят свою строку
  const el = mode === 'map' && Map2D.nodeEl(n.id);
  if (el) el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  const dom = (G.domains || []).find((d) => d.key === n.meta.domain);
  document.getElementById('p-kind').textContent = [KIND_LABEL[n.kind] || n.kind,
    n.layer === 'api' ? (dom ? dom.label : '') : '', n.layer === 'api' ? n.meta.service || '' : '']
    .filter(Boolean).join(' · ');
  const title = document.getElementById('p-title');
  title.textContent = n.label;
  title.classList.toggle('mono', MONO_KINDS.has(n.kind));
  document.getElementById('close').hidden = false;
  const body = document.getElementById('p-body');
  if (!again) body.innerHTML = '<p class="empty">Загрузка…</p>';
  const want = n.id;
  const data = await window.vehaApi.node(n.id).catch(() => null);
  if (!selected || selected.id !== want) return;   // пока грузилось, выбрали другой
  panelData = data;
  if (!panelData) { body.innerHTML = '<p class="empty">Нет данных по узлу</p>'; return; }
  body.innerHTML = renderNode(panelData);
  wirePanel(body);
}
let panelData = null;      // ответ /api/node — чтобы перерисовать без запроса

/* Обработчики карточки: тело перерисовывается, слушателей вешаем заново. */
function wirePanel(body) {
  // Связь — не только надпись: по ней переходят к соседнему узлу
  body.querySelectorAll('[data-goto]').forEach((b) =>
    b.addEventListener('click', () => {
      const n = index[b.dataset.goto] || G.nodes.find((x) => x.id === b.dataset.goto);
      if (n) openNode(n);
    }));
  body.querySelectorAll('[data-rerun]').forEach((b) =>
    b.addEventListener('click', () => rerun(b, b.dataset.rerun)));
  body.querySelectorAll('[data-req]').forEach((b) =>
    b.addEventListener('click', () => document.dispatchEvent(
      new CustomEvent('atlas:openreq', { detail: b.dataset.req }))));
  body.querySelectorAll('[data-chain]').forEach((b) =>
    b.addEventListener('click', () => {
      fullChain = !fullChain;
      draw();
      if (panelData) { body.innerHTML = renderNode(panelData); wirePanel(body); }
    }));
  body.querySelectorAll('[data-goto-tab]').forEach((b) =>
    b.addEventListener('click', () => goSection(b.dataset.gotoTab, b.dataset.set, b.dataset.val)));
}

/* Переход в раздел: разделами управляет dock.js. */
function goSection(tab, set, val) {
  document.dispatchEvent(new CustomEvent('atlas:goto', { detail: { tab, set, val } }));
}

/* «Как должно работать»: требования из спецификаций аналитики.
 * Источник — atlas/specs.json; способ привязки подписан у каждого требования,
 * потому что три из четырёх способов — выведенные, а не заявленные автором. */
const REQ_HOW = {
  direct: ['прямая ссылка из теста', 'ok'],
  anchor: ['выведено по эндпоинту', 'ok'],
  table: ['выведено по таблице, по окну теста с фикстурами', 'warn'],
  feature: ['выведено по названию фичи', 'warn'],
  none: ['ни одним тестом не проверено', 'bad'],
};

function renderRequirements(m) {
  const reqs = m.requirements || [];
  if (!reqs.length) return '';
  return `<section><h3>Как должно работать · ${reqs.length}</h3>
    <ul class="rows">${reqs.slice(0, 8).map((r) => {
      const [word, cls] = REQ_HOW[r.how] || REQ_HOW.none;
      return `<li><button class="lnk" data-req="${esc(r.id)}" title="${esc(r.source)}"><code>${
        esc(r.id)}</code> ${esc(r.title)}</button><span class="ev ${cls === 'warn' ? 'warn' : ''}">${esc(word)}</span></li>`;
    }).join('')}</ul>
    ${reqs.length > 8 ? `<button class="more" data-goto-tab="specs">ещё ${reqs.length - 8} — в разделе «Требования»</button>` : ''}
  </section>`;
}

/* Значки статуса под заголовком: покрытие и прогон рядом, без переключателя. */
function badges(n) {
  const m = n.meta || {};
  if (n.layer !== 'api') return '';
  const st = Map2D.cov(n);
  const pct = m.coverage == null || !Number.isFinite(Number(m.coverage)) ? null : Number(m.coverage);
  const covTxt = st === 'unseen' || pct == null ? 'нет в отчёте покрытия'
    : ({ ok: 'покрыт', part: 'покрыт частично', none: 'тестов нет' }[st]) + ` · ${pct}%`;
  const run = m.runStatus === 'failed'
    ? `<span class="badge bad">упал ${recNum(m.runFailed)} из ${recNum(m.runTotal)}</span>`
    : (m.runStatus === 'passed' ? `<span class="badge ok">прогон зелёный · ${recNum(m.runTotal)}</span>`
      : '<span class="badge none">в прогоне не вызывался</span>');
  return `<div class="badges"><span class="badge ${st}">${esc(covTxt)}</span>${run}</div>`;
}

/* Карточка — одна колонка разделов сверху вниз: сначала то, что болит (прогон),
 * потом методика, связи, контракт и требования. Длинное обрывается ссылкой на
 * раздел, а оговорки о происхождении данных остаются текстом рядом с числами. */
function renderNode(d) {
  const n = d.node, m = n.meta || {};
  const out = [badges(n)];

  if (n.kind === 'screen') {
    const key = n.id.slice(n.id.indexOf(':') + 1);
    const shotId = (G.screenAlias || {})[key];
    const shot = (G.screens || []).find((s) => s.id === shotId);
    out.push(`<section>${shot && shot.image
      ? `<img class="shot-card" src="${esc(shot.image)}" alt="Снимок экрана ${esc(n.label)}" loading="lazy">`
      : '<p class="empty">Снимка экрана нет в манифесте</p>'}
      <div class="kv"><div class="k">Путь</div><div><code>${esc(m.path || '—')}</code></div>
      ${m.pageObject ? `<div class="k">Page object</div><div><code>${esc(m.pageObject)}</code></div>` : ''}</div>
    </section>`);
  }

  if (n.layer === 'api') {
    const obs = m.observed || [];
    // упавшие — первыми и развёрнутыми; зелёные — строкой, их подробности в разделе «Прогон»
    const fails = obs.filter((o) => o.status !== 'passed');
    const rest = obs.filter((o) => o.status === 'passed');
    // Падения из сводки прогона: тест, задевший эндпоинт, мог упасть не на его
    // проверке — тогда в `observed` его нет, а значок «упал» есть. Показываем оба.
    const seen = new Set(fails.map((o) => o.nodeid));
    const runFails = ((G.run || {}).failures || [])
      .filter((f) => (f.endpoints || []).includes(n.id) && !seen.has(f.nodeid)).slice(0, 3);
    const runFailHtml = runFails.map((f) => `<div class="test">
        <div class="tname">${esc(f.name)}</div>
        <div class="row"><span class="chip bad">${esc(f.status)}</span>
          ${f.feature ? `<span class="chip">${esc(f.feature)}</span>` : ''}</div>
        ${f.error && f.error.head ? `<div class="out">${esc(f.error.head)}</div>` : ''}
      </div>`).join('');
    if (obs.length || runFails.length) {
      out.push(`<section><h3>Прогон · проверок ${recNum(m.checkCount)}</h3>` + runFailHtml +
        fails.slice(0, 3).map((o) => `<div class="test">
          <div class="tname">${esc(o.test)}</div>
          <div class="nodeid">${esc(o.nodeid || '')}</div>
          <div class="row">
            <span class="chip bad">${esc(o.status)}</span>
            ${o.expect ? `<span class="chip">ожидали ${esc(o.expect)}</span>` : ''}
            ${(o.attachments || []).map((a) => `<span class="chip">${esc(a)}</span>`).join('')}
          </div>
          <div class="checks">${(o.checks || []).map((c) => `<div>
            <span class="mark" data-css="color:${c.status === 'passed' ? 'var(--ok)' : 'var(--bad)'}">
            ${c.status === 'passed' ? '✓' : '✕'}</span>${esc(c.name)}</div>`).join('')}</div>
          ${o.nodeid ? `<div class="row" data-css="margin-top:8px">
            ${window.vehaApi.isStatic ? window.vehaApi.offline('Перегнать кейс', 'act')
              : `<button class="act" data-rerun="${esc(o.nodeid)}">Перегнать кейс</button>`}</div>` : ''}
        </div>`).join('') +
        (rest.length ? `<ul class="rows">${rest.slice(0, 5).map((o) => `<li><code>${esc(o.test)}</code>
          <span class="ev">${esc(o.status)}</span></li>`).join('')}</ul>` : '') +
        (fails.length > 3 || rest.length > 5
          ? `<button class="more" data-goto-tab="run">все ${obs.length} — в разделе «Прогон»</button>` : '') +
        '</section>');
    } else {
      out.push('<section><h3>Прогон</h3><p class="empty">В текущем allure-results вызовов нет</p></section>');
    }

    // число из coverage.json — в разметку и в data-css только числом
    const pct = m.coverage == null || !Number.isFinite(Number(m.coverage)) ? null
      : Number(m.coverage);
    const t = m.timing || {};
    out.push(`<section><h3>Методика</h3><div class="kv">
      <div class="k">Подтип (ось A)</div><div><code>${esc(m.subtype || '—')}</code></div>
      <div class="k">Сервис</div><div>${esc(m.service || '—')}</div>
      <div class="k">Вызовов</div><div>${m.calls == null ? '—' : recNum(m.calls)}</div>
      ${t.msAvg != null ? `<div class="k">Отклик</div><div><code>p50 ${recNum(t.msP50)} · p95 ${
        recNum(t.msP95)} · макс ${recNum(t.msMax)} мс</code></div>` : ''}</div>
      ${pct == null ? '<p class="empty">Нет в coverage.json</p>'
        : `<div class="bar"><i data-css="width:${pct}%;background:${(Object.hasOwn(COV, m.status) ? COV[m.status] : COV.unseen)}"></i></div>`}
      ${(m.executed || []).length || (m.missing || []).length ? `<div class="chips" data-css="margin-top:8px">
        ${(m.executed || []).map((x) => `<span class="chip">✓ ${esc(x)}</span>`).join('')}
        ${(m.missing || []).map((x) => `<span class="chip ghost" title="тест-кейса нет">${esc(x)}</span>`).join('')}
      </div>` : ''}
    </section>`);
  }

  if (n.kind === 'control') {
    out.push(`<section><h3>Элемент страницы</h3><div class="kv">
      <div class="k">data-testid</div><div><code>${esc(m.testid || '')}</code></div>
      <div class="k">Экран</div><div>${esc(m.screen || '—')}</div></div>
      ${(m.renders || []).length ? `<div class="chips" data-css="margin-top:8px">${
        m.renders.map((x) => `<span class="chip">рисует: ${esc(x)}</span>`).join('')}</div>` : ''}
      ${m.note ? `<p class="hint-row">${esc(m.note)}</p>` : ''}</section>`);
  }
  if (m.note && n.kind !== 'control') out.push(`<section><p class="hint-row">${esc(m.note)}</p></section>`);

  // шина: что реально прошло по топику на прогоне
  // `fileEvents` — сумма по всем записям артефакта: агент копит прогоны в одном
  // файле и не размечает их, поэтому это не «за прогон». Тишина считается только
  // по позадачным записям; у сессионного среза её нет (`silentTests` не задан).
  if (n.kind === 'topic' && m.fileEvents != null) {
    out.push(`<section><h3>Замеры шины</h3><div class="kv">
      <div class="k">Событий</div><div>${recNum(m.fileEvents)}</div>
      <div class="k">Записей</div><div>${recNum(m.fileRecords)}</div>
      ${m.silentTests != null ? `<div class="k">Тестов с событием в тишине</div>
        <div>${recNum(m.silentTests)}</div>` : ''}</div>
      <p class="hint-row">Сумма по всем записям артефакта
        <code>bus.flow</code>: агент дописывает прогоны в один файл, не размечая
        их, поэтому это не число за прогон.
        ${m.silentTests != null ? `«Тестов с событием в тишине» — разные
        тесты, у позадачных записей которых стоит признак <code>afterWrite</code>. Стенд общий, и признак не
        доказательство.` : `Замер снят на весь прогон: кто писал после события —
        API или консьюмер — из него не видно (под pytest-xdist признак
        <code>afterWrite</code> всегда <code>false</code>).`}
      </p></section>`);
  }

  // хранилище: схема таблицы прямо в карточке
  if (n.kind === 'table' || n.kind === 'store') {
    if (m.logOwners) {
      out.push(`<section><h3>Кто называет её в логах</h3><div class="chips">${
        Object.entries(m.logOwners).map(([k, v]) =>
          `<span class="chip">${esc(k)} <b>${recNum(v)}</b></span>`).join('')}</div>
        <p class="hint-row">Проверка ручной разметки
          <code>containers.match</code>: контейнер сам напечатал имя этой
          таблицы. Если здесь не тот контейнер, которому таблицу приписала
          карта, — разметку надо править.</p></section>`);
    }
    out.push(renderTable(n));
  }

  out.push(renderLinks(d));

  // контейнер: что видно в его логах
  if (m.logHits) {
    const groups = [['table', 'таблицы'], ['topic', 'топики'],
                    ['class', 'сервисы внутри'], ['listener', 'слушатели']];
    const rows = groups.filter(([k]) => m.logHits[k]).map(([k, title]) =>
      `<div data-css="margin-top:7px"><div class="rows-h">${title}</div><div class="chips">${
        Object.entries(m.logHits[k]).slice(0, 12).map(([v, cnt]) =>
          `<span class="chip">${esc(v)} <b>${recNum(cnt)}</b></span>`).join('')}</div></div>`);
    if (rows.length) {
      out.push(`<section><h3>Найдено в логах</h3>${rows.join('')}
        <p class="hint-row">Это факт о контейнере, а не о
          конкретном запросе: из строки лога не видно, в рамках какого вызова она
          возникла. Поэтому такие связи в цепочку потока не входят.
        </p></section>`);
    }
  }

  if (m.contract) {
    const k = m.contract;
    const body = k.body || null;
    const req = (f) => (body.requiredFields || []).includes(f);
    out.push(`<section><h3>Контракт${k.deprecated ? ' · <span class="chip bad">deprecated</span>' : ''}</h3>
      <div class="kv">
        <div class="k">Операция</div><div><code>${esc(k.operationId || '—')}</code></div>
        <div class="k">Контроллер</div><div>${esc((k.tags || []).join(', ') || '—')}</div>
        <div class="k">Коды ответов</div><div><code>${esc((k.responses || []).join(', ') || '—')}</code></div>
      </div>
      ${(k.params || []).length ? `<div class="chips" data-css="margin-top:8px">${k.params.map((pr) =>
          `<span class="chip ${pr.required ? '' : 'muted'}">${esc(pr.name)}
            <b>${esc(pr.in)}</b>${pr.required ? ' *' : ''}</span>`).join('')}</div>` : ''}
      ${body ? `<div data-css="margin-top:8px"><div class="rows-h">Тело: <code>${
        esc(body.schema || '')}</code>${body.required ? ' (обязательно)' : ''}</div>
        <div class="chips">${(body.fields || []).map((f) =>
          `<span class="chip ${req(f) ? 'ok' : ''}">${esc(f)}${req(f) ? ' *' : ''}</span>`).join('')}</div>
        </div>` : ''}
      ${(m.contractGap || []).length ? `<p class="hint-row">
        Тесты ждали коды <code>${esc(m.contractGap.join(', '))}</code>, которых контракт не
        объявляет. На этом контракте так почти везде: ошибочные коды не
        объявлены нигде — подробности в разделе «Требования → Контракт».</p>` : ''}
    </section>`);
  } else if (m.contractMissing) {
    out.push(`<section><h3>Контракт</h3><p class="empty">Эндпоинт
      вызывается в тестах, но в <code>/v3/api-docs</code> его нет. Либо он не
      объявлен, либо объявлен под другим путём — вопрос к разработке.</p>
    </section>`);
  }

  out.push(renderRequirements(m));

  if (d.tests.length) {
    out.push(`<section><h3>Тесты, где встречается · ${d.tests.length}</h3><div class="chips">${
      d.tests.slice(0, 8).map((t) => `<span class="chip">${esc(t.name)}</span>`).join('')}
      ${d.tests.length > 8 ? `<span class="chip ghost">и ещё ${d.tests.length - 8}</span>` : ''}</div></section>`);
  }

  const html = out.filter(Boolean).join('');
  return html.trim() ? html : '<p class="empty">Про этот узел мы ничего не знаем</p>';
}

/* Связи узла: куда ведёт и кто обращается, с происхождением каждой. */
const LINK_TEXT = {
  calls: 'вызывает', contains: 'содержит', publish: 'публикует', consume: 'читает',
  publishes: 'публикует', consumes: 'запись после события', writes: 'пишет', reads: 'читает',
  storage: 'кладёт в хранилище', depends: 'ходит во внешнюю систему', logged: 'в логах',
};
function renderLinks(d) {
  const n = d.node;
  const list = (arr, dir) => {
    const seen = new Map();
    arr.forEach((e) => {
      const id = dir === 'out' ? e.target : e.source;
      const ev = Map2D.evidence(e);
      const ops = String(e.label || '').startsWith('снято') ? String(e.label).split(': ').pop() : '';
      const cur = seen.get(id + e.kind);
      if (cur) { if (ops && !cur.ops.includes(ops)) cur.ops.push(ops); return; }
      seen.set(id + e.kind, { id, e, ev, ops: ops ? [ops] : [] });
    });
    const items = [...seen.values()];
    return items.slice(0, 12).map(({ id, e, ev, ops }) => {
      const o = index[id] || G.nodes.find((x) => x.id === id) || {};
      return `<li><button class="lnk" data-goto="${esc(id)}">${MONO_KINDS.has(o.kind)
        ? `<code>${esc(o.label || id)}</code>` : esc(o.label || id)}</button>
        <span class="ev" title="${esc(e.label || '')}">${esc(LINK_TEXT[e.kind] || e.kind)} · ${
          esc(Map2D.EV_TEXT[ev])}${ops.length ? ' · ' + esc(ops.join(', ')) : ''}</span></li>`;
    }).join('') + (items.length > 12
      ? `<li><button class="more" data-goto-tab="links" data-set="lq" data-val="${esc(n.label)}">ещё ${
        items.length - 12} — в «Система → Связи»</button><span></span></li>` : '');
  };
  const outRows = list(d.outgoing || [], 'out');
  const inRows = list(d.incoming || [], 'in');
  if (!outRows && !inRows) return '';
  const full = chain(n.id);
  const byWindow = (d.outgoing || []).concat(d.incoming || [])
    .some((e) => String(e.label || '').includes('в окне теста, с фикстурами'));
  return `<section><h3>Связи</h3>
    ${inRows ? `<p class="rows-h">Кто обращается</p><ul class="rows">${inRows}</ul>` : ''}
    ${outRows ? `<p class="rows-h">Дальше по цепочке</p><ul class="rows">${outRows}</ul>` : ''}
    ${byWindow ? `<p class="hint-row">Запись «в окне теста, с фикстурами» может
      принадлежать фикстуре, а не самому вызову.</p>` : ''}
    <button class="more" data-chain="1">${fullChain ? 'На карте — только соседи'
      : `На карте — путь целиком (${recNum(full.flow.size - 1)} узлов)`}</button>
  </section>`;
}

/* Схема таблицы: колонки, типы, ключи и внешние связи.
 * Данные — артефакт `db.schema` агента partest-atlas, снятый строго на чтение. */
function renderTable(n) {
  const t = tableByName(n.label);
  if (!t) {
    return `<section><h3>Схема таблицы</h3>
      <p class="empty">Нет снимка схемы: артефакт <code>db.schema</code>
      агента partest-atlas не собран или не прочитан (см. <code>/api/artifacts</code>).
      </p></section>`;
  }
  // Позадачный замер даёт «какой тест что записал», сессионный — только
  // «прогон задел таблицу». Смешивать их в один список нечестно: во втором
  // случае теста за записью нет.
  const all = (G.dbEffects || []).filter((e) => e.table === t.name);
  const eff = all.filter((e) => e.scope !== 'session' && e.nodeid);
  const touched = n.meta.sessionTouched;
  const cols = t.columns.map((c) => `<tr>
      <td>${c.pk ? '<span class="key pk">PK</span>' : ''}${
        c.fk ? '<span class="key fk">FK</span>' : ''}</td>
      <td class="cname">${esc(c.name)}</td>
      <td class="muted">${esc(c.type)}</td>
      <td class="muted">${c.nullable ? '' : 'NOT NULL'}</td>
      <td class="muted">${c.fk
        ? `→ ${esc(c.fk.table)}.${esc(c.fk.column)} <span class="chip">${
            esc(c.fk.onDelete)}</span>` : ''}</td>
    </tr>`).join('');

  return `<section>
    <h3>Схема таблицы</h3>
    <div class="kv">
      <div class="k">Схема</div><div>${esc(t.schema)}</div>
      <div class="k">Колонок</div><div>${t.columns.length}</div>
      <div class="k">Строк</div><div>${t.rows == null ? '—' : '≈ ' + recNum(t.rows)}</div>
    </div>
    <table class="cols">${cols}</table>
  </section>
  ${touched ? `<section><h3>Прогоны задели таблицу</h3>
    <div class="row">
      ${recNum(touched.insert) ? `<span class="chip ok">+${recNum(touched.insert)}</span>` : ''}
      ${recNum(touched.update) ? `<span class="chip">~${recNum(touched.update)}</span>` : ''}
      ${recNum(touched.delete) ? `<span class="chip">−${recNum(touched.delete)}</span>` : ''}
    </div>
    <p class="hint-row">Сессионный замер: известно, что прогоны меняли таблицу,
      но не известно, какой именно тест — статистика PostgreSQL публикуется
      пачками. Числа — сумма по ${recNum(touched.records)} сессионным записям артефакта:
      агент копит прогоны в одном файле, не размечая их, и схлопывает совпадающие
      записи, поэтому это не число за прогон. Точную привязку даёт запуск немногих
      тестов без pytest-xdist, см. <code>db_effects.per_test_limit</code>.</p>
  </section>` : ''}
  ${eff.length ? `<section><h3>Что задели тесты · ${eff.length}</h3>
    <p class="hint-row">Окно замера — подготовка, вызов и уборка теста: сюда
      входит и запись фикстур, а фикстура модуля или сессии приписана тесту, на
      котором сработала.</p>
    ${eff.slice(0, 8).map((e) => `<div class="test">
      <div class="tname">${esc(e.test)}</div>
      <div class="row">
        ${recNum(e.insert) ? `<span class="chip ok">+${recNum(e.insert)}</span>` : ''}
        ${recNum(e.update) ? `<span class="chip">~${recNum(e.update)}</span>` : ''}
        ${recNum(e.delete) ? `<span class="chip">−${recNum(e.delete)}</span>` : ''}
        ${e.shared ? '<span class="chip">стенд общий</span>' : ''}
      </div></div>`).join('')}
    ${eff.length > 8 ? `<p class="muted">и ещё ${eff.length - 8}</p>` : ''}</section>`
    : (touched ? '' : `<section><p class="empty">Нет данных о записи. Снять
       агентом partest-atlas: <code>ATLAS_DB_EFFECTS=1 pytest</code></p></section>`)}`;
}

function tableByName(name) {
  for (const db of (G.db && G.db.databases) || []) {
    const t = (db.tables || []).find((x) => x.name === name);
    if (t) return t;
  }
  return null;
}

async function rerun(btn, nodeid) {
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = 'Гоняю…';
  const box = document.createElement('div');
  box.className = 'out'; box.textContent = 'pytest ' + nodeid;
  btn.parentElement.after(box);
  try {
    const r = await fetch('/api/rerun', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nodeid }) });
    const d = await r.json();
    if (!r.ok) box.textContent = 'Ошибка: ' + (d.detail || r.status);
    else {
      box.textContent = (d.passed ? '✓ прошёл\n\n' : '✕ упал\n\n') + d.output;
      box.style.borderColor = d.passed ? 'var(--ok)' : 'var(--bad)';
    }
  } catch (e) { box.textContent = 'Сервис недоступен: ' + e; }
  finally { btn.disabled = false; btn.textContent = old; }
}

function closePanel() {
  selected = null;
  fullChain = false;
  panelData = null;
  draw();
  renderSummary();
  announceSelection();
}

/* Разделам нужно знать, что выделено: они подсвечивают строку. */
function announceSelection() {
  document.dispatchEvent(new CustomEvent('atlas:selection',
    { detail: selected ? selected.id : '' }));
}

/* Колонка справа без выбора — сводка системы: покрытие, прогон, чего не хватает, где болит. */
function renderSummary() {
  if (!G || selected) return;
  const s = (G.coverage || {}).summary || {};
  const runs = (G.coverage || {}).runs || {};
  const r = G.run || {};
  const cm = (G.coverage || {}).meta || {};
  // `summary.avg: null` — отчёт ничего не посчитал: среднего нет, разницы тоже
  const noAvg = s.avg == null || !Number.isFinite(Number(s.avg));
  const delta = noAvg || runs.deltaCoverage == null || !Number.isFinite(Number(runs.deltaCoverage))
    ? null : Number(runs.deltaCoverage);
  const failed = recNum(r.failed) + recNum(r.broken);
  const miss = ((G.coverage || {}).missingTop || []).slice(0, 5);
  const top = miss.length ? Math.max(1, recNum(miss[0].count)) : 1;
  const feats = (r.failuresByFeature || []).slice(0, 5);
  const ms = (v) => (v == null || !Number.isFinite(Number(v)) ? '—'
    : (Number(v) >= 1000 ? `${(Number(v) / 1000).toFixed(1)} с` : `${Math.round(Number(v))} мс`));

  document.getElementById('p-kind').textContent =
    `Система${cm.generated ? ' · срез ' + cm.generated : ''}`;
  const title = document.getElementById('p-title');
  title.textContent = 'Покрытие API по методике';
  title.classList.remove('mono');
  document.getElementById('close').hidden = true;
  const body = document.getElementById('p-body');
  body.innerHTML = `
    <div class="big">${noAvg ? '—' : recNum(s.avg) + ' %'}</div>
    <p class="delta">${noAvg && s.avg === null ? 'не посчитано: отчёт пуст'
      : delta == null ? 'по методике partest'
      : `<span class="${delta >= 0 ? 'up' : ''}">${delta >= 0 ? '▲' : '▼'} ${Math.abs(delta)} п. п.</span> к прошлому прогону`}</p>
    ${recNum(r.runsFound) > 1 ? `<div class="warn">В <code>allure-results</code> ${recNum(r.runsFound)}
      прогона — показан последний. Чистите папку перед запуском.</div>` : ''}
    <div class="kv">
      <div class="k">Эндпоинтов</div><div>${recNum(G.meta.endpoints)}${
        s.endpoints == null ? '' : ` <span class="muted">· в отчёте ${recNum(s.endpoints)}</span>`}</div>
      <div class="k">Прогон</div><div>${r.total ? `${recNum(r.passed)} из ${recNum(r.total)} зелёные` : 'нет allure-results'}</div>
      ${r.total ? `<div class="k">Упало</div><div${failed ? ' data-css="color:var(--bad)"' : ''}>${failed}
        <span class="muted">· failed ${recNum(r.failed)} + broken ${recNum(r.broken)}</span></div>` : ''}
      ${r.wallMs != null ? `<div class="k">Время</div><div>${ms(r.wallMs)}</div>` : ''}
      ${s.msP95 != null ? `<div class="k">Отклик p95</div><div>${ms(s.msP95)}</div>` : ''}
      ${G.meta.requirements ? `<div class="k">Требования</div><div>${recNum(G.meta.requirementsCovered)} из ${
        recNum(G.meta.requirements)} с тестами</div>` : ''}
    </div>
    ${miss.length ? `<section data-css="margin-top:14px"><h3>Чего не хватает чаще всего</h3>
      <ul class="rows">${miss.map((x) => `<li class="click" data-goto-tab="endpoints" data-set="missing" data-val="${esc(x.tc)}">
        <span><code>${esc(x.tc)}</code></span><span class="ev">${recNum(x.count)}</span>
        <span class="bar"><i data-css="width:${(100 * recNum(x.count)) / top}%"></i></span></li>`).join('')}</ul>
      <button class="more" data-goto-tab="matrix">Методика</button></section>` : ''}
    ${feats.length ? `<section><h3>Где болит в прогоне</h3>
      <ul class="rows">${feats.map(([name, cnt]) => `<li class="click" data-goto-tab="run" data-feature="${esc(name)}">
        <span>${esc(name)}</span><span class="ev" data-css="color:var(--bad)">${recNum(cnt)}</span></li>`).join('')}</ul>
      <button class="more" data-goto-tab="run">Весь прогон</button></section>` : ''}`;
  body.querySelectorAll('[data-goto-tab]').forEach((el) => el.addEventListener('click', () => {
    document.dispatchEvent(new CustomEvent('atlas:goto', { detail: {
      tab: el.dataset.gotoTab, set: el.dataset.set, val: el.dataset.val, feature: el.dataset.feature,
    } }));
  }));
}

/* ------------------------------------------------------------------ шапка */
/* Строка под шапкой — только оговорки о данных. Счётчики узлов и связей ушли:
 * они ничего не говорили о системе, а место занимали первым. */
function renderStats() {
  document.getElementById('stats').innerHTML = artifactWarning();
  const badge = document.getElementById('run-badge');
  if (badge) {
    const n = recNum((G.run || {}).failed) + recNum((G.run || {}).broken);
    badge.textContent = n ? String(n) : '';
  }
}

/* Артефакт, который сервер не прочитал (новее известной схемы, битый, не сходится
 * с контрактом), не должен выглядеть как «ничего не собрано». Подробности —
 * в подсказке и в /api/artifacts. */
function artifactWarning() {
  const a = G.artifacts || {};
  const chip = (text, notes, level) => `<span class="stat ${level}"`
    + ` title="${esc(notes.join('\n'))}">${text}</span>`;
  const out = [];

  // Область: молчим, только когда выбирать было не из чего или выбрал человек.
  // «Не выбрана» видно всегда — даже когда все артефакты просто отсутствуют.
  const sc = a.scope || {};
  if (a.scopeChosen === false || a.scopeNotable) {
    const others = (a.scopes || []).map((s) => `${s.system}/${s.env}`);
    out.push(chip(
      a.scopeChosen === false ? '<b>область не выбрана</b>'
        : `область <b>${esc(sc.system)}/${esc(sc.env)}</b>`,
      [a.scopeHow || '', ...(others.length ? ['на диске: ' + others.join(', ')] : [])],
      a.scopeChosen === false ? 'bad' : 'warn'));
  }

  const bad = (a.artifacts || []).filter((x) => x.state !== 'ok' && x.state !== 'missing');
  const notes = bad.map((x) => `${x.file}: ${x.state} — ${(x.problems || []).join('; ')}`);
  if (G.meta.configError) notes.unshift(`atlas.yaml: ${G.meta.configError}`);
  (a.ignored || []).forEach((x) => notes.push(x));
  if (notes.length) {
    out.push(chip(`артефакты: <b>${notes.length} не прочитано</b>`, notes, 'bad'));
  }

  const warns = (a.artifacts || []).flatMap((x) =>
    (x.warnings || []).map((w) => `${x.file}: ${w}`));
  if (warns.length) out.push(chip(`предупреждений: <b>${warns.length}</b>`, warns, 'warn'));

  // Записи агента, не давшие ни одной связи, — по причинам. Пустые отчёты Allure — не
  // потеря, а очистка: так и сказано.
  const col = G.collected || {};
  const lost = Object.entries(col.lostRecords || {}).filter(([, n]) => n > 0);
  if (col.reportsCleared && (lost.length || col.dbEffects)) {
    out.push(chip('<b>отчёты очищены</b>',
      ['отчётов Allure нет — записи агента не с чем соединить; связи появятся после прогона'],
      'warn'));
  } else {
    if (lost.length) {
      const total = lost.reduce((acc, [, n]) => acc + recNum(n), 0);
      out.push(chip(`потеряно записей агента: <b>${total}</b>`,
        lost.map(([k, n]) => `${recNum(n)} — ${(col.lostLabels || {})[k] || k}`), 'warn'));
    }
    // не потеря: db.effects накопительный, записи от прошлых тестов в нём есть всегда
    if (col.notInReports) {
      const n = Number(col.notInReports) || 0;
      out.push(chip(`не в текущих отчётах: <b>${n}</b>`,
        [`${n} записей агента от тестов, которых нет в текущих отчётах`], ''));
    }
  }

  // opt-in-наборы — отдельным значком ниже; объект в строку не выводим никогда:
  // `${v}` от вложенного объекта давал в подсказке «[object Object]»
  const optinText = (G.meta.optinText || []).map(String);
  if (G.meta.partialRun) {
    const sel = Object.entries(G.meta.selection || {})
      .filter(([k, v]) => k !== 'optin' && (v === null || typeof v !== 'object'))
      .map(([k, v]) => `${k}: ${v}`);
    const why = G.meta.partialReasons || [];
    out.push(chip(`<b>частичный прогон</b>${why.length ? ': ' + esc(why.join(', ')) : ''}`,
      ['покрытие снято с части набора — с полным прогоном несравнимо', ...why, ...sel],
      'warn'));
  }
  // Снятый opt-in-набор — штатный полный прогон, но без этих тестов: не тревога,
  // а то, что надо знать, сравнивая процент с прогоном, где набор был включён.
  if (G.meta.optinActed && optinText.length) {
    out.push(chip(`opt-in: <b>${esc(optinText.join('; '))}</b>`,
      ['наборы маркеров, которые partest снимает с прогона, пока их не попросили '
        + '(-m или переменная окружения)',
       'покрытие описывает прогон в этом состоянии; с прогоном, где наборы решены '
        + 'иначе, процент несравним',
       ...optinText], ''));
  }
  return out.join('');
}

/* Пилюли над картой: легенда и фильтр сразу. Нажатая гасит остальные эндпоинты. */
const PILLS = [
  ['', '', 'Все'], ['ok', 'ok', 'покрыто'], ['part', 'part', 'частично'],
  ['none', 'none', 'нет тестов'], ['unseen', 'unseen', 'нет в отчёте'], ['fail', 'fail', 'упало'],
];
function renderPills(countsOnly) {
  const box = document.getElementById('pills');
  if (!box || !G) return;
  const api = G.nodes.filter((n) => n.layer === 'api');
  const count = (k) => (!k ? api.length : (k === 'fail'
    ? api.filter(isFailed).length
    : api.filter((n) => Map2D.cov(n) === k).length));
  // «нет в отчёте» пропадает из ряда при нулевом счёте — нажатой невидимой ей не быть
  if (pill === 'unseen' && !count(pill)) pill = '';
  const shownKeys = PILLS.filter(([k]) => k !== 'unseen' || count(k)).map(([k]) => k).join(',');
  // На тике прогона меняются только числа — кнопки не пересобираются, иначе
  // клик между mousedown и mouseup терялся, а фокус с клавиатуры сбрасывался
  if (countsOnly && box.dataset.keys === shownKeys) {
    box.querySelectorAll('[data-pill]').forEach((b) => {
      const el = b.querySelector('b');
      if (el) el.textContent = String(count(b.dataset.pill));
    });
    return;
  }
  box.dataset.keys = shownKeys;
  box.innerHTML = PILLS.filter(([k]) => k !== 'unseen' || count(k)).map(([k, sw, label]) => `<button class="pill" data-pill="${k}"
    aria-pressed="${pill === k}" title="${k ? 'Оставить ярким только это' : 'Снять подсветку'}">${
    sw ? `<span class="sw sw-${sw}"></span>` : ''}${label} <b>${count(k)}</b></button>`).join('');
  box.querySelectorAll('[data-pill]').forEach((el) => el.addEventListener('click', () => {
    pill = pill === el.dataset.pill ? '' : el.dataset.pill;
    renderPills(); draw();
  }));
  const note = document.getElementById('filter-note');
  if (note) {
    const shown = filterFn ? api.filter(filterFn).length : 0;
    note.hidden = !filterFn;
    note.innerHTML = filterFn ? `фильтр из таблицы: ${shown} из ${api.length}
      <button data-clear-filter>снять</button>` : '';
    const b = note.querySelector('[data-clear-filter]');
    if (b) b.addEventListener('click', () => document.dispatchEvent(new CustomEvent('atlas:clearfilter')));
  }
}

function renderDomainSelect() {
  const el = document.getElementById('domain');
  if (!el || !G) return;
  const counts = {};
  G.nodes.filter((n) => n.layer === 'api').forEach((n) => {
    const k = domKey(n); counts[k] = (counts[k] || 0) + 1;
  });
  const label = {};
  (G.domains || []).forEach((d) => { label[d.key] = d.label; });
  const keys = (G.domains || []).map((d) => d.key).filter((k) => counts[k] && k !== 'other')
    .concat(Object.keys(counts).filter((k) => !(k in label) && k !== 'other').sort())
    .concat(counts.other ? ['other'] : []);
  el.innerHTML = '<option value="">все</option>' + keys
    .map((k) => `<option value="${esc(k)}"${domainSel === k ? ' selected' : ''}>${
      esc(k === 'other' ? 'Прочее' : (label[k] || k))} · ${counts[k]}</option>`).join('');
}
bind('domain', 'change', (e) => { domainSel = e.target.value; draw(); });

function renderSceneHint() {
  const el = document.getElementById('scene-hint');
  if (!el) return;
  if (selected) {
    el.textContent = `Цепочка ${selected.label.slice(0, 60)}: сплошная — снято на прогоне, `
      + 'штрих — объявлено, точки — выведено (по окну теста, из логов). Esc — снять выделение.';
  } else if (mode === 'volume') {
    el.textContent = 'Перетаскивание — поворот, Shift — сдвиг, колесо — масштаб. '
      + 'Нажмите на объект — покажем его цепочку.';
  } else {
    el.textContent = 'Связи показываются для выбранного узла: нажмите на эндпоинт, таблицу, '
      + 'топик или экран. Подпись эндпоинта — при наведении.';
  }
}

bind('refresh', 'click', async (e) => {
  if (window.vehaApi.isStatic) return;   // снимок не пересобирается: кнопка спрятана
  e.target.disabled = true; e.target.textContent = 'Собираю…';
  await load(true);
  e.target.disabled = false; e.target.textContent = 'Обновить данные';
});
bind('close', 'click', closePanel);

/* Вид карты: «Слои» или «Объём». Выбранный узел и подсветка переходят в новый вид. */
function setMode(next) {
  if (next !== 'map' && next !== 'volume') return;
  if (mode === next) return;
  mode = next;
  document.querySelectorAll('[data-view]').forEach((x) =>
    x.setAttribute('aria-pressed', String(x.dataset.view === next)));
  cv.hidden = mode !== 'volume';
  mapHost.hidden = mode === 'volume';
  mapWrap.classList.toggle('volume', mode === 'volume');
  tip.style.display = 'none';
  hover = null;
  if (mode === 'volume') { view = { rx: 0.42, ry: MAP_YAW, zoom: 1, ox: 0, oy: 0 }; fitView(); }
  draw();
}
document.querySelectorAll('[data-view]').forEach((el) =>
  el.addEventListener('click', () => setMode(el.dataset.view)));

window.addEventListener('resize', () => { if (mode === 'volume') { fitView(); draw(); } });
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && selected && !palEl.classList.contains('on')) closePanel();
});
// тема сменилась вместе с системой — «Объём» перечитывает палитру на следующем кадре
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => draw());

/* Число из артефакта — в разметку только числом: поле записи агента не строка
 * для innerHTML, даже если сервер его уже привёл. */
function recNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ------------------------------------------------------- поиск по карте ---
 * Ищем по всем узлам, а не только по эндпоинтам: таблицу и топик найти так же
 * нужно. Выбор ведёт к узлу на карте и раскрывает его цепочку. */
const qEl = document.getElementById('q');
const acEl = document.getElementById('ac');
let acItems = [], acSel = -1;

function runSearch(text) {
  const q = text.trim().toLowerCase();
  if (!q || !G) { acEl.classList.remove('on'); acItems = []; return; }
  const score = (n) => {
    const label = n.label.toLowerCase();
    const svc = (n.meta.service || '').toLowerCase();
    if (label.startsWith(q)) return 0;
    if (label.includes(q)) return 1;
    if (svc.includes(q)) return 2;
    return (n.meta.path || '').toLowerCase().includes(q) ? 3 : 99;
  };
  acItems = G.nodes.map((n) => ({ n, s: score(n) }))
    .filter((x) => x.s < 99)
    .sort((a, b) => a.s - b.s || a.n.label.length - b.n.label.length)
    .slice(0, 12).map((x) => x.n);
  acSel = acItems.length ? 0 : -1;
  renderAc();
}

function renderAc() {
  if (!acItems.length) { acEl.classList.remove('on'); return; }
  acEl.innerHTML = acItems.map((n, i) => `
    <div class="${i === acSel ? 'sel' : ''}" data-go="${esc(n.id)}">
      <span>${esc(n.label)}</span>
      <span class="p">${esc(n.meta.service || KIND_LABEL[n.kind] || n.kind)}</span>
    </div>`).join('');
  acEl.classList.add('on');
  acEl.querySelectorAll('[data-go]').forEach((el) =>
    el.addEventListener('mousedown', (e) => { e.preventDefault(); gotoNode(el.dataset.go); }));
}

/* Подвести «Объём» к узлу: иначе выбор срабатывает, а узел остаётся за кадром. */
function focusNode(n, { zoom = Math.max(0.75, Math.min(1.15, view.zoom * 1.5)) } = {}) {
  if (!n || mode !== 'volume') return;
  const p = project(nodeTop(n));
  const cx = PAD.left + (cv.clientWidth - PAD.left - PAD.right) / 2;
  const cy = PAD.top + (cv.clientHeight - PAD.top - PAD.bottom) / 2;
  const k = Math.max(0.2, Math.min(2.4, zoom));
  const rel = { x: p.sx - view.ox, y: p.sy - view.oy };
  view.ox = cx - (rel.x - cv.clientWidth / 2) * (k / view.zoom) - cv.clientWidth / 2;
  view.oy = cy - (rel.y - cv.clientHeight / 2) * (k / view.zoom) - cv.clientHeight / 2;
  view.zoom = k;
  draw();
}

/* Найденный узел показываем на карте: если открыт другой раздел — переходим. */
function gotoNode(id) {
  acEl.classList.remove('on');
  qEl.blur();
  const n = index[id] || (G && G.nodes.find((x) => x.id === id));
  if (!n) return;
  goSection('map');
  openNode(n); focusNode(n);
}

if (qEl) {
  qEl.addEventListener('input', () => runSearch(qEl.value));
  qEl.addEventListener('focus', () => runSearch(qEl.value));
  qEl.addEventListener('blur', () => setTimeout(() => acEl.classList.remove('on'), 120));
  qEl.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { acEl.classList.remove('on'); qEl.blur(); e.stopPropagation(); return; }
    if (!acItems.length) return;
    if (e.key === 'ArrowDown') { acSel = (acSel + 1) % acItems.length; renderAc(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { acSel = (acSel - 1 + acItems.length) % acItems.length; renderAc(); e.preventDefault(); }
    else if (e.key === 'Enter' && acSel >= 0) { gotoNode(acItems[acSel].id); e.preventDefault(); }
  });
}

/* --------------------------------------------------------- палитра команд
 * Один вход во всё: узлы, экраны, разделы и виды. Ctrl+K — кнопки в шапке нет. */
const palEl = document.getElementById('palette');
const palQ = document.getElementById('pal-q');
const palList = document.getElementById('pal-list');
let palItems = [], palSel = 0;

function paletteCommands() {
  // палитру могут открыть до того, как приехал граф — команды вида работают
  // и без него, а узлы просто ещё не в списке
  const cmds = [
    { kind: 'вид', label: 'Слои', hint: '1', run: () => { goSection('map'); setMode('map'); } },
    { kind: 'вид', label: 'Объём', hint: '2', run: () => { goSection('map'); setMode('volume'); } },
  ];
  (window.vehaSections || []).forEach(([tab, label]) => cmds.push({
    kind: 'раздел', label, run: () => goSection(tab),
  }));
  if (!G) return cmds;
  G.nodes.forEach((n) => cmds.push({
    kind: n.layer === 'api' ? 'эндпоинт' : (KIND_LABEL[n.kind] || n.kind).toLowerCase(),
    label: n.label, hint: n.meta.service || n.meta.path || '',
    run: () => gotoNode(n.id),
  }));
  return cmds;
}

function openPalette() {
  palEl.classList.add('on');
  palQ.value = '';
  runPalette('');
  palQ.focus();
}
function closePalette() { palEl.classList.remove('on'); }

function runPalette(text) {
  const q = text.trim().toLowerCase();
  const all = paletteCommands();
  palItems = (q
    ? all.filter((c) => (c.label + ' ' + (c.hint || '')).toLowerCase().includes(q))
    : all.slice(0, 12)).slice(0, 40);
  palSel = 0;
  renderPalette();
}
function renderPalette() {
  palList.innerHTML = palItems.map((c, i) => `
    <div class="${i === palSel ? 'sel' : ''}" data-pal="${i}">
      <span class="kindtag">${esc(c.kind)}</span>
      <span>${esc(c.label)}</span>
      <span class="hintk">${esc(c.hint || '')}</span>
    </div>`).join('') || '<div class="hintk" data-css="padding:14px 18px">ничего не нашлось</div>';
  palList.querySelectorAll('[data-pal]').forEach((el) =>
    el.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const c = palItems[Number(el.dataset.pal)];
      closePalette();
      if (c) c.run();
    }));
}

palQ.addEventListener('input', () => runPalette(palQ.value));
palQ.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown') { palSel = (palSel + 1) % Math.max(1, palItems.length); renderPalette(); e.preventDefault(); }
  else if (e.key === 'ArrowUp') { palSel = (palSel - 1 + palItems.length) % Math.max(1, palItems.length); renderPalette(); e.preventDefault(); }
  else if (e.key === 'Enter') { const c = palItems[palSel]; closePalette(); if (c) c.run(); }
  else if (e.key === 'Escape') { closePalette(); e.stopPropagation(); }
});
window.addEventListener('mousedown', (e) => {
  if (palEl.classList.contains('on') && !palEl.contains(e.target)) closePalette();
});

/* Горячие клавиши: «/» — поиск, Ctrl+K — палитра, «1/2» — вид карты. */
window.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    openPalette(); e.preventDefault(); return;
  }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '/') { qEl.focus(); e.preventDefault(); }
  else if (e.key === '1') { goSection('map'); setMode('map'); }
  else if (e.key === '2') { goSection('map'); setMode('volume'); }
});

// служебный хук для dock.js и для консоли
window.atlas = {
  select(id) {
    // select(null) — снять выделение: разделы должны уметь его отменить
    if (!id) { closePanel(); return true; }
    const n = index[id] || (G && G.nodes.find((x) => x.id === id));
    if (n) { openNode(n); focusNode(n); }
    return !!n;
  },
  get selectedId() { return selected ? selected.id : ''; },
  setFilter(fn) { filterFn = fn; renderPills(); draw(); },
  redraw() { draw(); },
  /* раздел «Карта» снова на экране: размеры могли смениться, пока он был скрыт */
  shown() { if (mode === 'volume') fitView(); draw(); },
  refit() { fitView(); draw(); },
  setCamDist(v) { CAM_DIST = v; fitView(); draw(); return CAM_DIST; },
  get view() { return view; },
  get graph() { return G; },
  focus(id) { const n = index[id]; if (n) { openNode(n); focusNode(n); } return !!n; },
  get edgeList() { return edgeList; },
  visibleOf(n) { return visible(n); },
  /* Подсветка идущего прогона: эндпоинты, которых он уже коснулся, и те,
   * где упало. Обновляется раз в секунду отдельно от кадра сцены. */
  setRunHighlight(touched, failed, failedTests) {
    runTouched = new Set(touched || []);
    runFailed = new Set(failed || []);
    // число у раздела «Прогон» — упавшие тесты (как в отчёте), пока прогон идёт
    const badge = document.getElementById('run-badge');
    if (badge && failedTests != null) badge.textContent = recNum(failedTests) ? String(recNum(failedTests)) : '';
    renderPills(true);
    redrawMarks();
  },
  get activeScreenPath() { return activeScreenPath; },
  /* Live: состояние контейнеров красит сервисы на карте. */
  setContainerState(payload) {
    liveContainers = {};
    ((payload && payload.containers) || []).forEach((c) => {
      if (!c.key) return;
      const cur = liveContainers[c.key];
      // если под ключом несколько контейнеров, худшее состояние побеждает:
      // «сервис жив» нельзя показывать, когда одна из его реплик легла
      const rank = c.down ? 2 : (c.restarting ? 1 : 0);
      if (!cur || rank > cur.rank) liveContainers[c.key] = { ...c, rank };
    });
    redrawMarks();
  },
  chainInfo(id) { const c = chain(id); return { flow: c.flow.size, related: c.related.size, edges: c.edges.size }; },
  get mode() { return mode; },
  setMode(v) { setMode(v); },
  get placed() { return placed; },
  get zones() { return ZONES; },
  get ground() { return GROUND; },
  get wall() { return WALL; },
  fit() { fitView(); draw(); },
};

// не через requestAnimationFrame: в фоновой вкладке он не срабатывает, и карта
// оставалась пустой до первого взгляда на неё
load(false);
