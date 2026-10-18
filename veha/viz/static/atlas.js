/* Веха, сцена — exploded axonometric: вертикальные плоскости, уходящие вглубь.
 *
 * Референсы:
 *   · exploded axonometric (архитектурные разнесённые планы) и Layerd —
 *     слои системы как отдельные плоскости, которые можно смотреть по одной
 *     или стопкой в 3D;
 *   · Edge DevTools 3D View / Chrome Layers panel — страница как плоскость
 *     в пространстве, элементы приподняты над ней по глубине.
 *
 * Первый слой — буквально страница SPA: каркас рисуется по `frame` из
 * app_map.yaml, а интерактивные элементы **отсоединены** от плоскости: висят
 * перед ней, соединены выноской со своим местом на странице и лучом уходят
 * назад к эндпоинту.
 *
 * Рендер на Canvas 2D без библиотек — работает офлайн и в закрытом контуре.
 */
'use strict';

const CSS = getComputedStyle(document.documentElement);
const C = (n) => CSS.getPropertyValue(n).trim();
const COV = { full: C('--ok'), partial: C('--warn'), empty: C('--bad'), unseen: C('--unseen') };
const RUN = { passed: C('--ok'), failed: C('--bad'), none: C('--unseen') };
let mode = 'map';        // map | ring — оба режима трёхмерные
let dataSet = 'coverage';  // coverage | tests — чем красим узлы
/* Три независимых слоя связей. Раньше набор рёбер был жёстко привязан к
 * раскраске, и посмотреть «покрытие цветом, а связи по тестам» было нельзя. */
const edgeLayers = { structure: true, coverage: true, tests: false };
let edgeDensity = 0.75;    // доля выводимых связей выбранного набора
let showDimEdges = true;   // гасить или прятать связи вне выделенной цепочки
let edgeList = [];         // активный набор рёбер: структура + срез данных
const LC = {
  frontend: C('--l-front'), api: C('--l-api'), bus: C('--l-bus'), storage: C('--l-store'),
};
const EDGE = {
  depends: [251, 191, 36],
  calls: [167, 139, 250], contains: [139, 155, 180], publish: [245, 165, 36],
  consume: [245, 165, 36], writes: [251, 113, 133], reads: [96, 165, 250],
  // снятые на прогоне связи через шину: отдельный цвет, потому что это
  // единственный вид связи, где писал не API, а консьюмер
  publishes: [253, 224, 71], consumes: [52, 211, 153],
  // из логов контейнера: факт о контейнере, а не о конкретном запросе
  logged: [148, 163, 184],
  storage: [34, 211, 238], flow: [139, 155, 180],
  cotest: [61, 214, 140], cofeature: [45, 212, 191],
  gap: [243, 18, 96], subtype: [124, 138, 165],
};
// структурные связи — правда об интеграциях, их видно всегда; выводные
// (ко-тесты, общие пробелы) держим тише, иначе они забивают картину
const EDGE_ALPHA = {
  calls: 0.7, contains: 0.34, publish: 0.75, consume: 0.75,
  publishes: 0.8, consumes: 0.8, logged: 0.3,
  writes: 0.62, reads: 0.55, storage: 0.7, flow: 0.5,
  cotest: 0.22, cofeature: 0.1, gap: 0.2, subtype: 0.07, depends: 0.7,
};

const CONTROL_POP = 74;               // насколько контрол вынесен перед страницей

const cv = document.getElementById('scene');
const ctx = cv.getContext('2d');
const tip = document.getElementById('tip');
const panel = document.getElementById('panel');

let G = null, placed = [], index = {}, linked = new Set();
/* Карту смотрим сверху-сбоку, кольцо — с большим наклоном, чтобы читался
 * эллипс. Положительный rx = камера над сценой. */
function defaultView() {
  return mode === 'ring'
    ? { rx: 0.72, ry: -0.35, zoom: 1, ox: 0, oy: 0 }
    : { rx: 0.42, ry: MAP_YAW, zoom: 1, ox: 0, oy: 0 };
}
let view = { rx: 0.42, ry: -0.34, zoom: 0.92, ox: 0, oy: 0 };
let hover = null, selected = null, spinning = false, t0 = performance.now();
let hovDistrict = null;
let liveContainers = {};   // ключ контейнера → его состояние на стенде
let runTouched = new Set(), runFailed = new Set();   // подсветка прогона
let lowDetail = false;     // во время перетаскивания рисуем упрощённо
let plates = [];   // подписи слоёв: рисуем в самом конце, поверх сцены
let rafPending = false;
let lastDraw = 0;  // импульс по лучу анимируем на ~22 fps: полный перерисов
                   // сцены каждый кадр съедал главный поток, и CSS-переходы
                   // (выезд панели) застревали на середине
const layerOn = { frontend: true, api: true, bus: true, storage: true };
let activeScreen = null;
let activeScreenPath = null;   // путь активного экрана — общий ключ для контролов
let activeShot = null;   // сцена из манифеста скриншотов, стоящая на экране
let filterFn = null;   // предикат из окна «Эндпоинты»: сцена и таблица
                       // должны показывать один и тот же срез
let scope = 'all';     // all | chain | domain | layer — быстрый срез сцены
let scopeDomain = null;
let scopeLayer = 'api';
/* Быстрые фильтры сцены. Живут отдельно от фильтров таблицы: там подробный
 * разбор, здесь — «убери лишнее, чтобы найти узел глазами». */
const SF = { method: '', status: '', run: '', gaps: false, untested: false,
             slow: false, external: false, linked: false };

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
    activeScreenPath = (activeShot && activeShot.screenPath) || null;
  }
  refreshEdges();
  linked = new Set();
  edgeList.forEach((e) => { linked.add(e.source); linked.add(e.target); });
  layout();
  fitView();
  renderStats(); renderLayers(); renderScreens();
  renderLegend(); renderEdgeLegend(); renderDomainChips();
  renderQuickChips(); renderShapeLegend(); renderFiltersPane();
  renderEdgeLayers();
  document.dispatchEvent(new CustomEvent('atlas:ready', { detail: G }));
  draw();
}

/* --------------------------------------------------------------- раскладка
 * Два режима, оба трёхмерные:
 *   map  — одна горизонтальная плоскость, на ней зоны системы слева направо
 *          по ходу запроса, а фронт стоит перед ними вертикальным экраном;
 *   ring — узлы по кольцу, связи идут хордами через центр.
 * Ось Y — вертикаль мира: меньше значение, выше объект. Земля на y = 0.
 */
const GROUND = { y: 0, x: 0, z: 0, w: 2600, d: 1560 };

/* Зоны читаются как схема потока: страница → API → шина → хранилища.
 * Делить на этажи не нужно — всё стоит на одной земле, разного размера и
 * на разной высоте подиума, поэтому иерархия видна и без ярусов. */
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
    .map((n) => n.meta.domain || 'other'));
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
    label: d ? d.label : 'Прочее',
    sub: ((G.layers || []).find((x) => x.key === 'api') || {}).sub || 'API',
    tier: d ? d.tier : 0,
    layer: 'api',
  };
}
/* Ступень потока: чем дальше от фронта, тем глубже ряд.
 * Домены нулевого яруса — первый ряд, первого яруса — второй, дальше шина и
 * хранилища. Порядок совпадает с тем, как запрос идёт по системе. */
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
const MAP_YAW = -0.34;            // разворот камеры по умолчанию в режиме карты
const WALL = { x: 0, y: 0, w: 600, h: 380, z: 0, rgb: [167, 139, 250] };
const RING = { r: 400, inner: 185, y: 0, innerY: -125, outer: 530, outerY: -240 };

const DISTRICT_H = 9;    // высота подиума района сервиса
const CONTROL_LIFT = 90; // насколько контрол вынесен перед страницей

/* Импульс по связи: первая половина цикла — запрос вниз по потоку,
 * вторая — ответ обратно. Односторонний импульс показывал половину
 * правды: нажали кнопку — данные ушли и вернулись. */
const PULSE_MS = 2600;
const RESPONSE_COLOR = '#7dd3fc';

let districts = [];   // районы сервисов: подиумы внутри зоны
let ringGroups = [];  // сектора кольца по сервисам

function layout() {
  buildServiceIndex();
  Object.keys(ZONES).forEach((k) => delete ZONES[k]);   // иначе копятся старые ключи
  placed = [];
  districts = [];
  ringGroups = [];
  if (mode === 'ring') layoutRing(); else layoutMap();
  index = Object.fromEntries(placed.map((n) => [n.id, n]));
}

const byLayer = () => {
  const out = {};
  G.nodes.forEach((n) => (out[n.layer] ||= []).push(n));
  return out;
};

/* Сервис узла. Эндпоинты, которых нет в coverage.json, приходят без поля
 * `service` — раскладывать их одной безымянной кучей нельзя, иначе в кольце
 * получается глухая дуга на треть окружности. Берём сегмент пути. */
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

/* ---- режим «Карта» ------------------------------------------------------ */
/* Зоны не заданы жёстко: сначала укладываем районы каждой зоны в локальные
 * координаты, потом из полученных габаритов собираем композицию на земле.
 * Так пустого места не остаётся и число зон ни к чему не привязано. */
function layoutMap() {
  const L = byLayer();
  const packs = {};
  // Площадка API разрезана по доменам: одна плита на 227 эндпоинтов нечитаема,
  // связи на ней не разобрать. Домены объявлены в atlas.yaml.
  ZONE_ORDER = apiZoneKeys().concat(['bus', 'storage']);
  ZONE_ORDER.forEach((k) => {
    const nodes = k === 'bus' || k === 'storage'
      ? (L[k] || [])
      : (L.api || []).filter((n) => (n.meta.domain || 'other') === k.slice(4));
    packs[k] = packLocal(nodes, k);
  });

  // Композиция — поток вглубь: экран → API → шина → хранилища. Раскладка
  // вширь давала ленту 1733×531 в почти квадратном холсте, и сцена ужималась
  // втрое ради пустых полей сверху и снизу.
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

  // Семь зон в одну колонну дают коридор длиной в две тысячи единиц: сцена
  // не помещается, а объекты становятся точками. Кладём рядами — ряд на
  // ступень потока, внутри ряда зоны стоят бок о бок.
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
  // считаем в базисе проекции:
  // при развороте камеры «слева» в мировых координатах — это не просто
  // меньший x, и экран иначе наезжает на город эндпоинтов.
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
  // Уводим экран влево ровно настолько, чтобы он не закрывал город, но и не
  // растягивал кадр: полный вынос давал лишние 400 единиц ширины сцены.
  const fullLeft = (mapMinSX - 70) / cy + (WALL.z * sy) / cy - WALL.w / 2;
  WALL.x = fullLeft * 0.62;

  // земля ровно по габариту композиции с полем: иначе половина кадра
  // уходит на пустую плоскость, а объекты становятся мелкими
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
 * Ширину ряда подбираем под пропорцию 3:2 — так зона не вытягивается в
 * бесконечную ленту и не превращается в узкий столбик. */
function packLocal(nodes, layer) {
  const empty = { w: 0, d: 0, districts: [], nodes: [] };
  if (!nodes.length) return empty;

  const isApi = layer.startsWith('dom:');
  const groups = {};
  nodes.forEach((n) => { (groups[isApi ? serviceOf(n) : n.kind] ||= []).push(n); });
  // воздух: на разреженной площадке связи видно, на плотной — нет
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
        color: GROUP_COLORS[(out.districts.length + (isApi ? 0 : 6))
                            % GROUP_COLORS.length],
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

/* ---- режим «Кольцо» ---------------------------------------------------- */
function layoutRing() {
  const L = byLayer();
  const api = (L.api || []).slice().sort((a, b) =>
    serviceOf(a).localeCompare(serviceOf(b)) || a.label.localeCompare(b.label));
  const groups = {};
  api.forEach((n) => (groups[serviceOf(n)] ||= []).push(n));
  const keys = Object.keys(groups).sort();
  const gap = 0.045;
  const usable = Math.PI * 2 - gap * keys.length;
  let ang = -Math.PI / 2;
  keys.forEach((k, gi) => {
    const items = groups[k];
    const span = usable * (items.length / Math.max(1, api.length));
    items.forEach((n, i) => {
      const a = ang + span * ((i + 0.5) / items.length);
      placed.push({
        ...n, group: k, ang: a, shape: 'ring',
        x: Math.cos(a) * RING.r, y: RING.y, z: Math.sin(a) * RING.r,
      });
    });
    ringGroups.push({
      key: k, a0: ang, a1: ang + span, count: items.length,
      color: GROUP_COLORS[gi % GROUP_COLORS.length],
    });
    ang += span + gap;
  });

  const inner = [...(L.bus || []), ...(L.storage || [])];
  inner.forEach((n, i) => {
    const a = ((i + 0.5) / inner.length) * Math.PI * 2 - Math.PI / 2;
    placed.push({
      ...n, ang: a, shape: 'ring',
      x: Math.cos(a) * RING.inner, y: RING.innerY, z: Math.sin(a) * RING.inner,
    });
  });

  const fe = (L.frontend || []).filter((n) => n.kind === 'control');
  fe.forEach((n, i) => {
    const a = ((i + 0.5) / fe.length) * Math.PI * 2 - Math.PI / 2;
    placed.push({
      ...n, ang: a, shape: 'control',
      x: Math.cos(a) * RING.outer, y: RING.outerY, z: Math.sin(a) * RING.outer,
    });
  });
}

/* Видимая «макушка» узла — к ней цепляем связи, подписи и попадание курсора. */
function nodeTop(n) {
  return { x: n.x, y: n.y - objHeight(n), z: n.z };
}

/* Узел, за которым стоит контейнер: сервис, внешняя система, шина, хранилище.
 * Эндпоинты сюда не относятся — у них свой цвет по покрытию или прогону. */
function containerKeyOf(n) {
  if (n.layer === 'api' || n.kind === 'control' || n.kind === 'screen') return '';
  return (n.id || '').startsWith('svc:') ? n.id.slice(4) : (n.meta.container || '');
}

/* Высота объекта. Для эндпоинта — нагрузка в логарифме: иначе один вызов
 * со 165 обращениями задавит всю остальную застройку. */
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

/* Опорные точки сцены: габарит земли и экрана фронта либо габарит кольца.
 * По ним кадр подгоняется даже когда узлы отфильтрованы почти в ноль. */
function sceneAnchors() {
  const out = [];
  if (mode === 'ring') {
    const R = RING.r + 130;
    for (let i = 0; i < 16; i++) {
      const a = (i / 16) * Math.PI * 2;
      out.push({ x: Math.cos(a) * R, y: RING.y, z: Math.sin(a) * R });
      out.push({ x: Math.cos(a) * RING.outer, y: RING.outerY, z: Math.sin(a) * RING.outer });
    }
    return out;
  }
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

const PAD = { left: 344, right: 34, top: 34, bottom: 58 };
function padLeft() {
  return openSide ? PAD.left : 78;   // без панели остаётся только рейл
}
function padRight() { return PAD.right; }
/* Док перекрывает низ холста, но пересчитывать кадр под него нельзя:
 * сцена начинает прыгать на каждое движение плашки. Резервируем постоянную
 * полосу — высоту свёрнутого дока. */
function padBottom() { return PAD.bottom + 46; }
function fitView() {
  if (!G || !placed.length) return;
  // холст может быть ещё не размечен (0×0) — тогда доступная ширина уходит
  // в минус, масштаб становится отрицательным и вся сцена выворачивается
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
  const availW = cv.clientWidth - padLeft() - padRight();
  const availH = cv.clientHeight - PAD.top - padBottom();
  const zoom = Math.max(0.15, Math.min(
    Math.max(1, availW) / Math.max(1, x1 - x0),
    Math.max(1, availH) / Math.max(1, y1 - y0), 1.6));
  // центр свободной области относительно центра холста
  const targetX = padLeft() + availW / 2 - cx0;
  const targetY = PAD.top + availH / 2 - cy0;
  view.zoom = zoom;
  view.ox = targetX - ((x0 + x1) / 2) * zoom;
  view.oy = targetY - ((y0 + y1) / 2) * zoom;
}

/* ------------------------------------------------------------- проекция 3D */
/* Расстояние камеры. Изометрические схемы, на которые мы равняемся, рисуются
 * без перспективы: у сильной перспективы ближний объект раздувается и съедает
 * кадр — сцена целиком перестаёт помещаться. Держим лёгкий намёк на глубину. */
let CAM_DIST = 5200;

function project(p) {
  const cy = Math.cos(view.ry), sy = Math.sin(view.ry);
  const cx = Math.cos(view.rx), sx = Math.sin(view.rx);
  // Вращаем вокруг начала координат: сцена и так строится симметрично
  // относительно центра, а прежний сдвиг ZC достался от стопки плоскостей и
  // теперь только перекашивал перспективу.
  let x = p.x * cy - p.z * sy;
  let z = p.x * sy + p.z * cy;
  let y = p.y * cx - z * sx;
  z = p.y * sx + z * cx;
  // за камерой знаменатель уходит в минус, масштаб становится отрицательным
  // и canvas падает на arc() с отрицательным радиусом — подрезаем
  const persp = CAM_DIST / Math.max(CAM_DIST * 0.2, CAM_DIST + z);
  return {
    sx: cv.clientWidth / 2 + x * persp * view.zoom + view.ox,
    sy: cv.clientHeight / 2 + y * persp * view.zoom + view.oy,
    k: persp * view.zoom,
    depth: z,
  };
}
const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

/* Обработчики вешаем только на то, что есть в разметке: перестановка панелей
 * уже роняла весь скрипт на addEventListener у null, и атлас не поднимался. */
function bind(id, event, fn) {
  const el = document.getElementById(id);
  if (el) el.addEventListener(event, fn);
}

/* Цвет узла зависит от режима: покрытие по методике или результат прогона. */
function nodeColor(n) {
  // Идущий прогон важнее всего: видно, где он сейчас и что уже сломал.
  if (runFailed.has(n.id)) return RUN.failed;
  if (runTouched.has(n.id)) return C('--accent');
  // Живое состояние важнее статической раскраски: упавший контейнер должен
  // быть виден сразу, без перехода во вкладку Live.
  const live = liveContainers[containerKeyOf(n)];
  if (live) return live.down ? RUN.failed : (live.restarting ? COV.partial : RUN.passed);
  if (n.layer !== 'api') return LC[n.layer] || C('--muted');
  if (dataSet === 'tests') return RUN[n.meta.runStatus] || RUN.none;
  return COV[n.meta.status] || COV.unseen;
}

const _rgbCache = {};
function hexRgb(hex) {
  if (_rgbCache[hex]) return _rgbCache[hex];
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim());
  const v = m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [140, 150, 180];
  return (_rgbCache[hex] = v);
}
function visible(n) {
  if (!layerOn[n.layer]) return false;
  if (n.kind === 'screen') return n.id === activeScreen;
  if (n.layer === 'frontend' && n.kind === 'control') {
    // сравниваем по пути экрана: ручная карта и рекордер называют экраны
    // по-разному, общий у них только путь
    if (activeScreenPath && n.meta.screenPath !== activeScreenPath) return false;
  }
  if (SF.linked && !linked.has(n.id)) return false;
  if (filterFn && n.layer === 'api' && !filterFn(n)) return false;
  if (scope === 'chain') {
    const f = selected || hover;
    if (f && !chain(f.id).nodes.has(n.id)) return false;
  }
  if (scope === 'domain' && scopeDomain && n.layer === 'api'
      && (n.meta.domain || 'other') !== scopeDomain) return false;
  if (scope === 'layer' && n.layer !== scopeLayer) return false;
  if (n.layer === 'api' && !sceneFilterOk(n)) return false;
  return true;
}

/* ------------------------------------------------- геометрия сцены --------*/
const GROUP_COLORS = ['#22d3ee', '#a78bfa', '#f472b6', '#34d399', '#f5a524',
                      '#60a5fa', '#fb7185', '#c084fc', '#2dd4bf', '#facc15',
                      '#4ade80', '#38bdf8'];
const ZONE_RGB = {
  api: [96, 165, 250], bus: [245, 165, 36], storage: [34, 211, 238],
};
/* Домены красим оттенками синего API, чтобы острова читались как один слой,
 * но различались между собой. */
const DOMAIN_RGB = [[96, 165, 250], [124, 160, 255], [80, 190, 235],
                    [130, 140, 250], [70, 200, 205], [110, 175, 235]];
function zoneRgb(key) {
  if (ZONE_RGB[key]) return ZONE_RGB[key];
  const i = Math.max(0, ZONE_ORDER.indexOf(key));
  return DOMAIN_RGB[i % DOMAIN_RGB.length];
}

/* Свечение и виньетка: без них сцена плоская, а объекты не отделяются от фона. */
function drawBackdrop() {
  const w = cv.clientWidth, h = cv.clientHeight;
  const g = ctx.createRadialGradient(w * 0.5, h * 0.34, 20, w * 0.5, h * 0.34, w * 0.8);
  g.addColorStop(0, 'rgba(30,58,102,.32)');
  g.addColorStop(0.5, 'rgba(15,26,44,.14)');
  g.addColorStop(1, 'rgba(8,11,17,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, w, h);
}

/* Земля: одна плоскость под всей картой. Сетка задаёт масштаб, тёмная заливка
 * отделяет карту от фона, светлая кромка очерчивает границу. */
function drawGround() {
  const { y, w, d } = GROUND;
  const cxg = GROUND.x, czg = GROUND.z;
  const corners = [
    { x: cxg - w / 2, y, z: czg - d / 2 }, { x: cxg + w / 2, y, z: czg - d / 2 },
    { x: cxg + w / 2, y, z: czg + d / 2 }, { x: cxg - w / 2, y, z: czg + d / 2 },
  ].map(project);

  S3.polyPath(ctx, corners);
  const c = project({ x: cxg, y, z: czg });
  const e = project({ x: cxg + w / 2, y, z: czg });
  const rr = Math.max(40, Math.abs(e.sx - c.sx));
  const g = ctx.createRadialGradient(c.sx, c.sy, 0, c.sx, c.sy, rr);
  g.addColorStop(0, 'rgba(18,30,48,.85)');
  g.addColorStop(1, 'rgba(10,14,22,.45)');
  ctx.fillStyle = g;
  ctx.fill();

  ctx.save();
  ctx.clip();
  ctx.strokeStyle = 'rgba(96,165,250,.09)';
  ctx.lineWidth = 1;
  const STEP = 120;
  for (let gx = cxg - w / 2; gx <= cxg + w / 2 + 1; gx += STEP) {
    const a = project({ x: gx, y, z: czg - d / 2 });
    const b = project({ x: gx, y, z: czg + d / 2 });
    ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke();
  }
  for (let gz = czg - d / 2; gz <= czg + d / 2 + 1; gz += STEP) {
    const a = project({ x: cxg - w / 2, y, z: gz });
    const b = project({ x: cxg + w / 2, y, z: gz });
    ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke();
  }
  ctx.restore();

  S3.polyPath(ctx, corners);
  ctx.strokeStyle = 'rgba(96,165,250,.3)';
  ctx.lineWidth = 1.4;
  ctx.stroke();
}

/* Зона системы: подиум со скруглением, кромкой и заголовком. */
function drawZone(key) {
  const z = ZONES[key];
  const rgb = zoneRgb(key);
  S3.pad(ctx, project, {
    x: z.x, y: 0, z: z.z, w: z.w, d: z.d, h: z.lift,
    rgb, r: 34, grid: 0, top: 0.34, glow: 16, rim: 1.7,
  });
  const m = zoneMeta(key);
  plates.push({
    p: project({ x: z.x - z.w / 2, y: -z.lift, z: z.z - z.d / 2 }),
    col: rgba(rgb, 1), title: m.label.toUpperCase(),
    sub: m.sub, align: 'left',
  });
}

/* Район сервиса — подиум внутри зоны с именем и числом эндпоинтов. */
function drawDistrict(d) {
  const rgb = hexRgb(d.color);
  const on = hovDistrict === d;
  S3.pad(ctx, project, {
    x: d.x, y: d.y, z: d.z, w: d.w, d: d.d, h: DISTRICT_H,
    rgb, r: 12, top: on ? 0.5 : 0.26, glow: on ? 20 : 9, rim: on ? 2 : 1.2,
    alpha: on ? 1 : 0.86,
  });
  if (d.count >= 4 || on) {
    const c = project({ x: d.x, y: d.y - DISTRICT_H, z: d.z - d.d / 2 });
    ctx.font = '600 ' + Math.max(8, 9.5 * c.k) + 'px "Segoe UI", sans-serif';
    ctx.fillStyle = S3.shade(rgb, 1.75, on ? 1 : 0.8);
    ctx.textAlign = 'center';
    ctx.fillText(d.key + ' · ' + d.count, c.sx, c.sy - 7);
    ctx.textAlign = 'left';
  }
}

/* Фронт — вертикальный экран, стоящий на земле перед картой.
 *
 * На экране висит НАСТОЯЩИЙ скриншот страницы: те же PNG, что снимает
 * попиксельное сравнение (`src/ui/baselines/reference`). Рисовать каркас по
 * координатам больше не нужно — реальный вид и честнее, и подробнее, а заодно
 * манифест скриншотов даёт готовый перечень экранов.
 *
 * Canvas не умеет рисовать картинку в произвольный четырёхугольник, поэтому
 * проецируем углы экрана и накладываем изображение аффинным преобразованием по
 * трём углам — для плоского прямоугольника этого достаточно.
 */
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
    rgb: WALL.rgb, alpha: 0.9, glow: 12,
  });

  // корпус монитора
  poly(quad(-3.4, -3.6, 106.8, 107.4), 'rgb(9,13,22)', 'rgba(167,139,250,.85)', 2);

  const shot = screenImage(activeShot && activeShot.image);
  const face = quad(0, 0, 100, 100);
  wallOccluder = {
    pts: quad(-3.4, -3.6, 106.8, 107.4),
    k: project({ x: WALL.x, y: py(50), z: WALL.z }).k,
  };
  if (shot) {
    drawImageOnQuad(shot, face);
    poly(face, null, 'rgba(167,139,250,.35)', 1);
  } else {
    poly(face, 'rgb(13,19,30)', 'rgba(167,139,250,.3)', 1);
    const c = project({ x: px(50), y: py(50), z: WALL.z });
    ctx.fillStyle = 'rgba(139,155,180,.75)';
    ctx.font = Math.max(9, 11 * c.k) + 'px "Segoe UI", sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(activeShot ? 'скриншот грузится…' : 'нет снимка экрана', c.sx, c.sy);
    ctx.textAlign = 'left';
  }

  // опоры к земле
  [-1, 1].forEach((sgn) => {
    const top = project({ x: WALL.x + sgn * WALL.w * 0.48, y: WALL.y, z: WALL.z });
    const bot = project({ x: WALL.x + sgn * WALL.w * 0.48, y: 0, z: WALL.z });
    ctx.beginPath(); ctx.moveTo(top.sx, top.sy); ctx.lineTo(bot.sx, bot.sy);
    ctx.strokeStyle = 'rgba(167,139,250,.3)'; ctx.lineWidth = 1.4;
    ctx.setLineDash([4, 5]); ctx.stroke(); ctx.setLineDash([]);
  });

  plates.push({
    p: project({ x: px(0), y: py(0), z: WALL.z }), col: LC.frontend,
    title: 'FRONTEND',
    sub: activeShot ? `${activeShot.title} · ${activeShot.path}` : (screen.label || ''),
    align: 'left',
  });
}

/* Наложить изображение на спроецированный четырёхугольник.
 * Три угла задают аффинное преобразование — плоскости этого хватает. */
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

/* Кольцо: несущая полоса, сектора сервисов и дорожки метрик снаружи. */
function drawRingScene() {
  const band = (r0, r1, a0, a1, fill, stroke) => {
    const pts = [];
    const step = Math.max(0.02, (a1 - a0) / 40);
    for (let a = a0; a <= a1 + 1e-6; a += step) {
      pts.push(project({ x: Math.cos(a) * r1, y: RING.y, z: Math.sin(a) * r1 }));
    }
    for (let a = a1; a >= a0 - 1e-6; a -= step) {
      pts.push(project({ x: Math.cos(a) * r0, y: RING.y, z: Math.sin(a) * r0 }));
    }
    S3.polyPath(ctx, pts);
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = 1.4; ctx.stroke(); }
  };

  band(RING.r - 26, RING.r + 26, -Math.PI, Math.PI,
       'rgba(20,30,48,.55)', 'rgba(96,165,250,.18)');

  ringGroups.forEach((g) => {
    const rgb = hexRgb(g.color);
    const on = hovDistrict === g;
    band(RING.r - 26, RING.r + 26, g.a0, g.a1,
         S3.shade(rgb, 1, on ? 0.34 : 0.16), S3.shade(rgb, 1.8, on ? 1 : 0.6));
    const mid = (g.a0 + g.a1) / 2;
    const p = project({
      x: Math.cos(mid) * (RING.r + 132), y: RING.y, z: Math.sin(mid) * (RING.r + 132),
    });
    if (g.count >= 4 || on) {
      ctx.font = '600 ' + Math.max(8, 10 * p.k) + 'px "Segoe UI", sans-serif';
      ctx.fillStyle = S3.shade(rgb, 1.75, 0.95);
      ctx.textAlign = 'center';
      ctx.fillText(g.key + ' · ' + g.count, p.sx, p.sy);
      ctx.textAlign = 'left';
    }
  });

  // дорожки метрик снаружи кольца: покрытие и нагрузка
  placed.filter((n) => n.layer === 'api' && visible(n)).forEach((n) => {
    const track = (rr, len, color, alpha) => {
      const a0 = project({ x: Math.cos(n.ang) * rr, y: RING.y, z: Math.sin(n.ang) * rr });
      const a1 = project({
        x: Math.cos(n.ang) * (rr + len), y: RING.y, z: Math.sin(n.ang) * (rr + len),
      });
      ctx.beginPath(); ctx.moveTo(a0.sx, a0.sy); ctx.lineTo(a1.sx, a1.sy);
      ctx.strokeStyle = rgba(hexRgb(color), alpha); ctx.lineWidth = 2.4; ctx.stroke();
    };
    const cov = n.meta.coverage;
    if (cov != null) {
      track(RING.r + 30, 4 + (cov / 100) * 44, COV[n.meta.status] || COV.unseen, 0.85);
    }
    const load = Math.min(1, Math.log10((n.meta.calls || 0) + 1) / 3);
    if (load > 0) track(RING.r + 84, 3 + load * 34, '#60a5fa', 0.5);
  });

  // внутреннее кольцо шины и хранилищ
  const inner = placed.filter((n) => n.layer === 'bus' || n.layer === 'storage');
  if (inner.length) {
    const pts = [];
    for (let a = 0; a <= Math.PI * 2 + 1e-6; a += 0.08) {
      pts.push(project({
        x: Math.cos(a) * RING.inner, y: RING.innerY, z: Math.sin(a) * RING.inner,
      }));
    }
    S3.polyPath(ctx, pts);
    ctx.strokeStyle = 'rgba(34,211,238,.34)'; ctx.lineWidth = 1.3; ctx.stroke();
  }
}

/* ----------------------------------------------------------------- отрисовка */
function drawPlates() {
  plates.forEach(({ p, col, title, sub, align }) => {
    const dx = align === 'right' ? -8 : 8;
    ctx.textAlign = align;
    ctx.fillStyle = col;
    ctx.font = '700 ' + Math.max(10, 12 * p.k) + 'px "Segoe UI", sans-serif';
    ctx.shadowColor = col; ctx.shadowBlur = 14;
    ctx.fillText(title, p.sx + dx, p.sy - 22);
    ctx.shadowBlur = 0;
    ctx.fillStyle = 'rgba(139,155,180,.85)';
    ctx.font = Math.max(9, 10 * p.k) + 'px "Segoe UI", sans-serif';
    ctx.fillText(sub, p.sx + dx, p.sy - 9);
    ctx.textAlign = 'left';
  });
}

function draw() {
  const dpr = window.devicePixelRatio || 1;
  cv.width = cv.clientWidth * dpr;
  cv.height = cv.clientHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cv.clientWidth, cv.clientHeight);
  if (!G) return;

  plates = [];
  tagSlots = [];
  pendingTags = [];
  wallOccluder = null;      // ставится заново в drawWall, если экран рисуется
  drawBackdrop();

  // экранная точка узла — его видимая макушка, к ней же цепляются связи
  const P = {};
  screenPts = new Map();
  placed.forEach((n) => {
    if (!visible(n)) return;
    const p = project(nodeTop(n));
    P[n.id] = { n, p };
    screenPts.set(n.id, p);
  });

  const focus = selected || hover;
  const ch = focus ? chain(focus.id) : null;
  const byDepth = (u, v) => v.p.depth - u.p.depth;

  if (mode === 'ring') {
    drawRingScene();
    drawEdges(P, focus, ch);
    Object.values(P).sort(byDepth).forEach(({ n, p }) => drawObject(n, p, focus, ch));
  } else {
    drawGround();

    // Всё стоит на одной земле, поэтому порядок задаёт только глубина:
    // зона, её районы и объекты районов рисуются от дальних к ближним.
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
    // Экран рисуем ПОСЛЕ связей: он непрозрачный и обязан их закрывать.
    // Раньше он шёл раньше, и линии слоя API ложились поверх страницы —
    // выглядело так, будто монитор просвечивает.
    const sc = placed.find((n) => n.id === activeScreen);
    if (sc && layerOn.frontend) drawWall(sc);
    Object.values(P).filter(({ n }) => n.layer === 'frontend').sort(byDepth)
      .forEach(({ n, p }) => drawObject(n, p, focus, ch));
  }

  flushTags();
  drawPlates();
  renderSceneHint();

  if (focus && !rafPending) {   // импульс вдоль выделенной цепочки
    rafPending = true;
    setTimeout(() => { rafPending = false; if (selected || hover) draw(); }, 45);
  }
}

/* Паутина. Рисуем в режиме сложения — пересечения светятся, как на
 * референсах, и тысяча тонких линий не превращается в серую кашу. */
function drawEdges(P, focus, ch) {
  const now = performance.now();
  ctx.globalCompositeOperation = 'lighter';
  // В режиме «Тесты» цепочка узла может тянуть сотни рёбер: при полной яркости
  // сложение цветов выжигает середину сцены в белое пятно. Гасим пропорционально.
  const hotScale = ch ? Math.max(0.18, Math.min(1, 26 / Math.max(1, ch.edges.size))) : 1;

  edgeList.forEach((e, ei) => {
    const A = P[e.source], B = P[e.target];
    if (!A || !B) return;
    const hot = focus && ch.edges.has(ei);
    const dim = focus && !hot;
    if (dim && !showDimEdges) return;

    const a = nodeTop(A.n), b = nodeTop(B.n);
    const col = EDGE[e.kind] || EDGE.flow;
    const lift = mode === 'ring' ? 26 : Math.hypot(a.x - b.x, a.z - b.z) * 0.22 + 40;
    // в кольце связь идёт хордой через центр, на карте — дугой над этажами
    const mid = mode === 'ring'
      ? { x: (a.x + b.x) * 0.10, y: (a.y + b.y) / 2 - lift, z: (a.z + b.z) * 0.10 }
      : { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - lift, z: (a.z + b.z) / 2 };

    // Отсечение по кадру: связь, обе стороны которой далеко за экраном,
    // всё равно не видна, а стоит тех же двенадцати проекций.
    const W = cv.clientWidth, H = cv.clientHeight, M = 140;
    const outA = A.p.sx < -M || A.p.sx > W + M || A.p.sy < -M || A.p.sy > H + M;
    const outB = B.p.sx < -M || B.p.sx > W + M || B.p.sy < -M || B.p.sy > H + M;
    if (outA && outB && !hot) return;

    // Сегментов ровно столько, сколько видно: тонкая полупрозрачная дуга на
    // двенадцати точках неотличима от дуги на шести.
    const span = Math.hypot(A.p.sx - B.p.sx, A.p.sy - B.p.sy);
    const N = hot ? 22 : (lowDetail ? 3 : (span > 260 ? 10 : (span > 90 ? 6 : 3)));
    const pts = [];
    for (let i = 0; i <= N; i++) {
      const t = i / N, u = 1 - t;
      pts.push(project({
        x: u * u * a.x + 2 * u * t * mid.x + t * t * b.x,
        y: u * u * a.y + 2 * u * t * mid.y + t * t * b.y,
        z: u * u * a.z + 2 * u * t * mid.z + t * t * b.z,
      }));
    }

    const w = e.w || 1;
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p.sx, p.sy) : ctx.moveTo(p.sx, p.sy)));

    if (hot) {
      // горячая связь окрашена от цвета источника к цвету цели
      const g = ctx.createLinearGradient(pts[0].sx, pts[0].sy,
                                         pts[N].sx, pts[N].sy);
      g.addColorStop(0, nodeColor(A.n));
      g.addColorStop(1, nodeColor(B.n));
      ctx.strokeStyle = g;
      ctx.globalAlpha = 0.3 + 0.45 * hotScale;
      ctx.lineWidth = 1.1 + 1.1 * hotScale;
      ctx.shadowColor = rgba(col, 0.7); ctx.shadowBlur = 3 + 6 * hotScale;
    } else {
      const base = EDGE_ALPHA[e.kind] ?? 0.13;
      ctx.strokeStyle = rgba(col, dim ? base * 0.14 : base * (0.7 + Math.min(1.6, w / 8)));
      ctx.lineWidth = 0.55 + Math.min(2.2, Math.log2(w + 1) * 0.5);
    }
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.globalAlpha = 1;

    if (hot && hotScale > 0.3) {
      // Запрос и ответ. Односторонний импульс показывал только половину
      // правды: нажали кнопку — данные ушли и вернулись, и обратный путь
      // такая же часть цепочки. Первая половина цикла — запрос «вниз» по
      // потоку, вторая — ответ обратно, другим цветом и чуть мельче.
      const step = Math.abs(ch.nodes.get(e.source) ?? 0);
      const cycle = ((now - t0) / PULSE_MS) % 1;
      const shifted = cycle - step * 0.07;
      if (shifted >= 0) {
        const half = shifted * 2;
        const back = half >= 1;
        const t = back ? 2 - half : half;         // туда, затем обратно
        if (t >= 0 && t <= 1) {
          const q = pts[Math.min(N, Math.max(0, Math.floor(t * N)))];
          const r = back ? 2.6 : 3.6;
          ctx.beginPath(); ctx.arc(q.sx, q.sy, r, 0, Math.PI * 2);
          ctx.fillStyle = back ? RESPONSE_COLOR : '#fff';
          ctx.shadowColor = back ? RESPONSE_COLOR : rgba(col, 1);
          ctx.shadowBlur = back ? 10 : 14;
          ctx.fill(); ctx.shadowBlur = 0;
        }
      }
    }
  });

  ctx.globalCompositeOperation = 'source-over';
}

/* Подпись с подложкой: поверх объектов и лучей белый текст без плашки
 * не читается, а на цепочке подписи нужны у каждого узла. */
function drawLabel(text, x, y, opts) {
  const o = opts || {};
  const size = o.size || 11;
  const t = String(text).slice(0, 42);
  ctx.font = (o.strong ? '650 ' : '') + size + 'px "Segoe UI", sans-serif';
  const w = ctx.measureText(t).width;
  const px = o.align === 'center' ? x - w / 2 : x;
  ctx.beginPath();
  ctx.roundRect(px - 5, y - size + 1, w + 10, size + 7, 4);
  ctx.fillStyle = 'rgba(8,12,20,.84)';
  ctx.fill();
  ctx.strokeStyle = 'rgba(255,255,255,.08)'; ctx.lineWidth = 1; ctx.stroke();
  ctx.fillStyle = o.color || C('--text');
  ctx.textAlign = 'left';
  ctx.fillText(t, px, y + 1);
}

/* Один узел как объёмный объект: столбик эндпоинта, цилиндр таблицы, шайба
 * топика, стопка контейнеров сервиса либо отсоединённая карточка контрола. */
/* Уровень детализации по экранному размеру объекта.
 * Полная отрисовка (градиенты на каждой грани, контактная тень, свечение)
 * стоит около 0.15 мс на объект — на 269 объектах это 40 мс на кадр. Мелким
 * объектам она не нужна: на экране разницы нет.
 */
function lodOf(p, focus) {
  if (focus) return 2;              // выделенное всегда рисуем полностью
  if (lowDetail) return 0;          // камера едет: плоско, но все грани на месте
  const px = 13 * p.k;
  return px < 5 ? 0 : (px < 11 ? 1 : 2);
}

function drawObject(n, p, focus, ch) {
  if (n.isWall) return;
  const isFocus = focus && n.id === focus.id;
  const onChain = focus && ch.nodes.has(n.id);
  const dim = focus && !onChain;
  const col = nodeColor(n);
  const rgb = hexRgb(col);
  const alpha = dim ? 0.22 : 1;
  const glow = isFocus ? 26 : (onChain ? 16 : 0);
  // свечение рисуется отдельным проходом размытия — включаем только там,
  // где оно несёт смысл, а не на каждом из двух с половиной сотен объектов
  const lod = lodOf(p, isFocus || onChain || (hover && hover.id === n.id));

  if (n.shape === 'control') {
    if (n.anchor) {
      const a = project(n.anchor);
      ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(p.sx, p.sy);
      ctx.strokeStyle = dim ? 'rgba(167,139,250,.10)' : 'rgba(167,139,250,.55)';
      ctx.setLineDash([3, 3]); ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]);
      ctx.beginPath(); ctx.arc(a.sx, a.sy, Math.max(1, 2.4 * a.k + 1), 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(167,139,250,.6)'; ctx.fill();
    }
    const w = 16 * p.k + 12, h = 9 * p.k + 8;
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.roundRect(p.sx - w, p.sy - h, w * 2, h * 2, 6);
    ctx.fillStyle = isFocus ? 'rgba(167,139,250,.38)' : 'rgba(167,139,250,.2)';
    ctx.strokeStyle = LC.frontend;
    ctx.lineWidth = isFocus ? 2 : 1.2;
    ctx.shadowColor = LC.frontend; ctx.shadowBlur = isFocus ? 24 : 9;
    ctx.fill(); ctx.shadowBlur = 0; ctx.stroke();
    ctx.globalAlpha = 1;
    if (!dim) {
      drawLabel(n.label, p.sx, p.sy + 3,
                { align: 'center', size: Math.max(9, 11 * p.k), strong: isFocus });
    }
    return;
  }

  if (n.shape === 'ring') {
    const r = Math.max(2.6, ringRadius(n) * p.k);
    ctx.globalAlpha = alpha;
    if (isFocus || onChain) {
      ctx.beginPath(); ctx.arc(p.sx, p.sy, r + 9, 0, Math.PI * 2);
      ctx.fillStyle = rgba(rgb, 0.2); ctx.fill();
    }
    const hollow = dataSet === 'coverage' && n.layer === 'api' && n.meta.coverage == null;
    ctx.beginPath(); ctx.arc(p.sx, p.sy, r, 0, Math.PI * 2);
    if (hollow) {
      ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.stroke();
    } else {
      ctx.fillStyle = col;
      ctx.shadowColor = col; ctx.shadowBlur = glow || 7;
      ctx.fill(); ctx.shadowBlur = 0;
      ctx.beginPath(); ctx.arc(p.sx - r * 0.3, p.sy - r * 0.32, r * 0.4, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(255,255,255,.45)'; ctx.fill();
    }
    ctx.globalAlpha = 1;
  } else if (n.shape === 'silo') {
    // хранилище: цилиндр с «поясом» — читается как база даже мелким
    S3.cylinder(ctx, project, {
      x: n.x, y: n.y, z: n.z, r: 21, h: objHeight(n), rgb, alpha, dim, lod,
      glow: lod >= 2 ? (glow || 9) : glow,
    });
  } else if (n.shape === 'disc') {
    S3.cylinder(ctx, project, {
      x: n.x, y: n.y, z: n.z, r: 30, h: objHeight(n), rgb, alpha, dim, lod,
      glow: lod >= 2 ? (glow || 14) : glow,
    });
  } else if (n.shape === 'stack') {
    S3.stack(ctx, project, {
      x: n.x, y: n.y, z: n.z, w: 58, d: 42, h: 8, gap: 7,
      count: lod === 0 ? 2 : 4, rgb, alpha, dim, lod,
      glow: lod >= 2 ? (glow || 10) : glow,
    });
  } else if (n.shape === 'cube') {
    S3.box(ctx, project, {
      x: n.x, y: n.y, z: n.z, w: 52, d: 40, h: objHeight(n),
      rgb, alpha, dim, lod, glow: lod >= 2 ? (glow || 11) : glow,
    });
  } else {
    // «здание» эндпоинта: сечение чуть растёт с числом тестов на нём
    const side = 9.5 + Math.min(6, (n.meta.testCount || 0) * 0.42);
    S3.box(ctx, project, {
      x: n.x, y: n.y, z: n.z, w: side, d: side, h: objHeight(n),
      rgb, alpha, dim, lod, glow: lod >= 2 ? glow : 0,
    });
  }

  // Бирки только у крупных объектов и у цепочки: подпись на каждом из 227
  // зданий превращает карту в кашу.
  const tagged = isFocus || onChain || (hover && hover.id === n.id)
    || (!focus && n.shape !== 'building' && n.shape !== 'ring');
  if (tagged && !dim) {
    // на цепочке подпись крупнее: она и есть ответ на вопрос «что это»
    const big = isFocus || onChain;
    placeTag({
      x: p.sx, y: p.sy, k: p.k, text: n.label.slice(0, 36),
      size: big ? Math.max(11.5, 13 * p.k) : Math.max(9, 10.5 * p.k),
      strong: big,
      color: isFocus ? '#fff' : C('--text'),
      line: isFocus ? col : 'rgba(255,255,255,.25)',
    }, big);
  }
}

/* Бирки не рисуем на месте, а копим: лучи связей идут после объектов и
 * затирали подписи. Выкладываем очередь в самом конце кадра — подпись всегда
 * поверх линий. Заодно раскладываем без пересечений: пробуем несколько высот
 * выноски и уступаем место соседу, кроме подписей на цепочке. */
let tagSlots = [];
let pendingTags = [];
/* Экран фронта непрозрачен, но бирки выкладываются последними и легли бы
 * поверх него. Запоминаем его многоугольник и глубину: подпись узла, который
 * стоит ЗА экраном и попадает в его площадь, не рисуем вовсе. */
let wallOccluder = null;

function pointInPoly(x, y, pts) {
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
  // k — масштаб проекции: больше значит ближе к камере
  return !!wallOccluder && o.k != null && o.k < wallOccluder.k
    && pointInPoly(o.x, o.y, wallOccluder.pts);
}

function placeTag(o, force) {
  pendingTags.push({ o, force });
}

function flushTags() {
  const hit = (r) => tagSlots.some((s) =>
    r.x < s.x + s.w + 3 && r.x + r.w + 3 > s.x
    && r.y < s.y + s.h + 3 && r.y + r.h + 3 > s.y);
  // важные подписи выкладываем первыми — им и достаётся лучшее место
  pendingTags.sort((a, b) => (b.force ? 1 : 0) - (a.force ? 1 : 0));
  pendingTags.forEach(({ o, force }) => {
    if (occluded(o)) return;
    for (const lift of [20, 40, 62, 86, 112]) {
      const box = S3.tagBox(ctx, { ...o, lift });
      if (!hit(box)) {
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

function ringRadius(n) {
  if (n.layer !== 'api') return 6.5;
  return 3.4 + Math.min(4.2, Math.log10((n.meta.calls || 1) + 1) * 2.4);
}


/* Структура паутины меняется вместе с переключателем: к постоянным связям
 * интеграций добавляется либо «ходили в одном тесте», либо «одинаковые
 * пробелы по методике». */
function refreshEdges() {
  const sets = (G && G.edgeSets) || {};
  const dens = (arr) => arr.slice(0, Math.round(arr.length * edgeDensity));
  let list = [];
  // структурные связи плотностью не режем: их всего семь десятков и это
  // единственная правда об интеграциях
  if (edgeLayers.structure) list = list.concat(sets.structure || G?.edges || []);
  if (edgeLayers.coverage) list = list.concat(dens(sets.coverage || []));
  if (edgeLayers.tests) list = list.concat(dens(sets.tests || []));
  edgeList = list;
  _chain = {}; _adj = null;
}

const EDGE_LAYER_LABEL = {
  structure: 'структура', coverage: 'покрытие', tests: 'тесты',
};
function renderEdgeLayers() {
  const box = document.getElementById('edge-layers');
  if (!box || !G) return;
  const sets = G.edgeSets || {};
  box.innerHTML = Object.keys(edgeLayers).map((k) => {
    const n = (sets[k] || []).length;
    return `<button class="chip ${edgeLayers[k] ? 'on' : ''}" data-el="${k}"
      title="${n} рёбер">${EDGE_LAYER_LABEL[k]}</button>`;
  }).join('');
  box.querySelectorAll('[data-el]').forEach((el) => el.addEventListener('click', () => {
    edgeLayers[el.dataset.el] = !edgeLayers[el.dataset.el];
    refreshEdges();
    renderEdgeLayers(); renderEdgeLegend(); renderStats();
    draw();
  }));
}

/* Структурные связи — это поток запроса, их обходим на всю глубину.
 * Выводные (ко-тесты, общая фича, общие пробелы) — это похожесть, а не поток:
 * если идти по ним транзитивно, «POST /brands» с двумя реальными связями
 * подсвечивает 227 узлов из 269. Поэтому их берём только на один шаг. */
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

/* ------------------------------------------------------------ интерактивность */
/* Экранные точки узлов последнего кадра: попадание курсором считаем по ним,
 * а не проецируем 269 узлов заново на каждое движение мыши. */
let screenPts = new Map();

function hit(mx, my) {
  let best = null, bestD = 18;
  placed.forEach((n) => {
    if (!visible(n) || n.isWall) return;
    const p = screenPts.get(n.id) || project(nodeTop(n));
    const d = Math.hypot(p.sx - mx, p.sy - my);
    const rad = n.shape === 'control' ? 24 * p.k
      : (n.shape === 'ring' ? ringRadius(n) * p.k + 6 : 13 * p.k + 5);
    if (d < Math.max(bestD, rad)) { best = n; bestD = d; }
  });
  return best;
}

/* Район под курсором: проверяем попадание в верхнюю грань подплатформы
 * (в кольце — в сектор). Наведение подсвечивает весь сервис целиком. */
function districtAt(mx, my) {
  if (mode === 'ring') {
    for (const g of ringGroups) {
      const pts = [];
      const step = Math.max(0.02, (g.a1 - g.a0) / 24);
      for (let a = g.a0; a <= g.a1; a += step) {
        pts.push(project({ x: Math.cos(a) * (RING.r + 26), y: RING.y,
                           z: Math.sin(a) * (RING.r + 26) }));
      }
      for (let a = g.a1; a >= g.a0; a -= step) {
        pts.push(project({ x: Math.cos(a) * (RING.r - 26), y: RING.y,
                           z: Math.sin(a) * (RING.r - 26) }));
      }
      if (pts.length > 2 && pointInPoly(pts, mx, my)) return g;
    }
    return null;
  }
  for (const d of districts) {
    if (!layerOn[d.layer]) continue;
    const y = d.y - 16 - DISTRICT_H;
    const pts = [
      { x: d.x - d.w / 2, y, z: d.z - d.d / 2 }, { x: d.x + d.w / 2, y, z: d.z - d.d / 2 },
      { x: d.x + d.w / 2, y, z: d.z + d.d / 2 }, { x: d.x - d.w / 2, y, z: d.z + d.d / 2 },
    ].map(project);
    if (pointInPoly(pts, mx, my)) return d;
  }
  return null;
}

function pointInPoly(pts, x, y) {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const a = pts[i], b = pts[j];
    if ((a.sy > y) !== (b.sy > y)
      && x < ((b.sx - a.sx) * (y - a.sy)) / (b.sy - a.sy) + a.sx) inside = !inside;
  }
  return inside;
}

/* Панели рисуются поверх холста, но события мыши всё равно долетали до сцены:
 * под курсором подсвечивались узлы «сквозь» окно, и по ним можно было кликнуть.
 * Считаем сцену занятой, если под курсором есть элемент интерфейса. */
const UI_SELECTOR = 'header, .rail, aside, .dock, .palette, .scene-bar, #tip, .ac';
function overUi(e) {
  const el = document.elementFromPoint(e.clientX, e.clientY);
  return !!(el && el !== cv && el.closest(UI_SELECTOR));
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
  if (overUi(e)) {                 // курсор над панелью — сцена его не видит
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
    tip.style.display = 'block';
    tip.style.left = Math.min(e.clientX + 14, innerWidth - 320) + 'px';
    tip.style.top = (e.clientY + 16) + 'px';
    tip.innerHTML = `<div>${esc(h.key)}</div>` +
      `<div class="sub">${recNum(h.count)} эндпоинтов · клик по объекту — цепочка</div>`;
    return;
  }
  if (n) {
    tip.style.display = 'block';
    tip.style.left = Math.min(e.clientX + 14, innerWidth - 360) + 'px';
    tip.style.top = (e.clientY + 16) + 'px';
    const sub = n.layer === 'api'
      ? `${n.meta.subtype || 'подтип не определён'} · покрытие ${n.meta.coverage ?? '—'}%`
      : (n.meta.testid || n.kind);
    tip.innerHTML = `<div>${esc(n.label)}</div><div class="sub">${esc(sub)}</div>`;
  } else tip.style.display = 'none';
});
window.addEventListener('mouseup', (e) => {
  if (lowDetail) { lowDetail = false; draw(); }   // вернуть полную детализацию
  if (drag && !drag.moved && !overUi(e)) {
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

/* ------------------------------------------------------------------ панель */
async function openNode(n) {
  selected = n; t0 = performance.now();
  panel.classList.add('open');
  draw();
  announceSelection();      // окна подсветят свою строку и дадут её снять
  document.getElementById('p-kind').textContent =
    ({ endpoint: 'Эндпоинт', control: 'Элемент UI', screen: 'Экран',
       topic: 'Топик шины', table: 'Таблица', store: 'Хранилище',
       backend: 'Сервис', bus: 'Шина', auth: 'Аутентификация',
       db: 'База данных' }[n.kind] || n.kind);
  document.getElementById('p-title').textContent = n.label;
  const body = document.getElementById('p-body');
  body.innerHTML = '<div class="empty">Загрузка…</div>';
  panelData = await window.vehaApi.node(n.id).catch(() => null);
  if (!panelData) { body.innerHTML = '<div class="empty">Нет данных по узлу</div>'; return; }
  body.innerHTML = renderNode(panelData);
  wirePanel(body);
}

/* Обработчики карточки. Вынесены отдельно: переключение закладки
 * перерисовывает тело, и слушателей надо навесить заново. */
function wirePanel(body) {
  body.querySelectorAll('[data-ptab]').forEach((b) =>
    b.addEventListener('click', () => {
      panelTab = b.dataset.ptab;
      body.innerHTML = renderNode(panelData);
      wirePanel(body);
    }));
  // Связь — не только надпись: по ней переходят к соседнему узлу
  body.querySelectorAll('[data-goto]').forEach((b) =>
    b.addEventListener('click', () => {
      const n = index[b.dataset.goto];
      if (n) openNode(n);
    }));
  body.querySelectorAll('[data-rerun]').forEach((b) =>
    b.addEventListener('click', () => rerun(b, b.dataset.rerun)));
  body.querySelectorAll('[data-req]').forEach((b) =>
    b.addEventListener('click', () => document.dispatchEvent(
      new CustomEvent('atlas:openreq', { detail: b.dataset.req }))));
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
  return `<section><h3>Как должно работать (${reqs.length})</h3>
    <div class="chips">${reqs.slice(0, 12).map((r) => {
      const [word, cls] = REQ_HOW[r.how] || REQ_HOW.none;
      return `<button class="chip ${cls}" data-req="${esc(r.id)}"
        title="${esc(r.source)} · ${esc(word)}">${esc(r.id)} ${esc(r.title)}</button>`;
    }).join('')}
    ${reqs.length > 12 ? `<span class="chip">и ещё ${reqs.length - 12}</span>` : ''}</div>
    <div class="empty" data-css="margin-top:8px">Клик открывает требование во
      вкладке «Требования» — там текст раздела и тесты, которыми он проверен.</div>
  </section>`;
}

/* Разделы карточки разложены по закладкам, как вкладки в книге: у эндпоинта
 * их пять, и одной простынёй они не читались — до «Прогона» приходилось
 * прокручивать мимо контракта и требований. Пустые закладки не показываем. */
const PANEL_TABS = [
  ['main', 'Обзор'],
  ['spec', 'Требования'],
  ['contract', 'Контракт'],
  ['links', 'Связи'],
  ['run', 'Прогон'],
];
let panelTab = 'main';
let panelOf = '';          // для какого узла выбрана закладка
let panelData = null;      // ответ /api/node — чтобы перерисовать без запроса

function renderNode(d) {
  const n = d.node, m = n.meta || {};
  const parts = {};
  const add = (key, html) => {
    if (!html) return;
    (parts[key] ||= []).push(html);
  };

  add('spec', renderRequirements(m));

  if (n.layer === 'api') {
    // число из coverage.json — в разметку и в data-css только числом
    const pct = m.coverage == null || !Number.isFinite(Number(m.coverage)) ? null
      : Number(m.coverage);
    add('main', `<section><h3>Покрытие по методике</h3><div class="kv">
      <div class="k">Подтип (ось A)</div><div>${esc(m.subtype || '—')}</div>
      <div class="k">Сервис</div><div>${esc(m.service || '—')}</div>
      <div class="k">Вызовов</div><div>${m.calls == null ? '—' : recNum(m.calls)}</div></div>
      ${pct == null ? '<div class="empty" data-css="margin-top:8px">Нет в coverage.json</div>'
        : `<div class="bar"><i data-css="width:${pct}%;background:${COV[m.status] || COV.unseen}"></i></div>
           <div data-css="font-size:12px;color:var(--muted)">${pct}% · ${esc(m.status || '')}</div>`}
    </section>`);
    if ((m.executed || []).length || (m.missing || []).length) {
      add('main', `<section><h3>Тест-кейсы (ось B)</h3><div class="chips">
        ${(m.executed || []).map((x) => `<span class="chip ok">${esc(x)}</span>`).join('')}
        ${(m.missing || []).map((x) => `<span class="chip bad">нет: ${esc(x)}</span>`).join('')}
      </div></section>`);
    }
    if (m.contract) {
      const k = m.contract;
      const body = k.body || null;
      const req = (f) => (body.requiredFields || []).includes(f);
      add('contract', `<section><h3>Объявлено в контракте${
        k.deprecated ? ' <span class="chip bad">deprecated</span>' : ''}</h3>
        <div class="kv">
          <div class="k">Операция</div><div><code>${esc(k.operationId || '—')}</code></div>
          <div class="k">Контроллер</div><div>${esc((k.tags || []).join(', ') || '—')}</div>
          <div class="k">Коды ответов</div><div>${esc((k.responses || []).join(', ') || '—')}</div>
        </div>
        ${(k.params || []).length ? `<div data-css="margin-top:9px"><div class="k">Параметры</div>
          <div class="chips">${k.params.map((pr) =>
            `<span class="chip ${pr.required ? '' : 'muted'}">${esc(pr.name)}
              <b>${esc(pr.in)}</b>${pr.required ? ' *' : ''}</span>`).join('')}</div></div>` : ''}
        ${body ? `<div data-css="margin-top:9px"><div class="k">Тело: <code>${
          esc(body.schema || '')}</code>${body.required ? ' (обязательно)' : ''}</div>
          <div class="chips">${(body.fields || []).map((f) =>
            `<span class="chip ${req(f) ? 'ok' : ''}">${esc(f)}${req(f) ? ' *' : ''}</span>`).join('')}</div>
          </div>` : ''}
        ${(m.contractGap || []).length ? `<div class="empty" data-css="margin-top:9px">
          Тесты ждали коды ${esc(m.contractGap.join(', '))}, которых контракт не
          объявляет. На этом контракте так почти везде: ошибочные коды не
          объявлены нигде — подробности во вкладке «Контракт» внизу.</div>` : ''}
      </section>`);
    } else if (m.contractMissing) {
      add('contract', `<section><h3>Объявлено в контракте</h3><div class="empty">Эндпоинт
        вызывается в тестах, но в <code>/v3/api-docs</code> его нет. Либо он не
        объявлен, либо объявлен под другим путём — вопрос к разработке.</div>
      </section>`);
    }
    const t = m.timing || {};
    if (t.msAvg != null) {
      add('main', `<section><h3>Тайминги</h3><div class="kv">
        <div class="k">среднее</div><div>${recNum(t.msAvg)} мс</div>
        <div class="k">p50 / p95</div><div>${recNum(t.msP50)} / ${recNum(t.msP95)} мс</div>
        <div class="k">макс</div><div>${recNum(t.msMax)} мс</div></div></section>`);
    }
  }

  if (n.kind === 'control') {
    add('main', `<section><h3>Элемент страницы</h3><div class="kv">
      <div class="k">data-testid</div><div><code>${esc(m.testid || '')}</code></div>
      <div class="k">Экран</div><div>${esc(m.screen || '—')}</div></div>
      ${(m.renders || []).length ? `<div data-css="margin-top:10px"><h3>Рисует</h3><div class="chips">${
        m.renders.map((x) => `<span class="chip">${esc(x)}</span>`).join('')}</div></div>` : ''}
      ${m.note ? `<div class="empty" data-css="margin-top:9px">${esc(m.note)}</div>` : ''}</section>`);
  }

  // контейнер: что видно в его логах
  if (m.logHits) {
    const groups = [['table', 'таблицы'], ['topic', 'топики'],
                    ['class', 'сервисы внутри'], ['listener', 'слушатели']];
    const rows = groups.filter(([k]) => m.logHits[k]).map(([k, title]) =>
      `<div data-css="margin-top:7px"><div class="k">${title}</div><div class="chips">${
        Object.entries(m.logHits[k]).slice(0, 12).map(([v, cnt]) =>
          `<span class="chip">${esc(v)} <b>${cnt}</b></span>`).join('')}</div></div>`);
    if (rows.length) {
      add('main', `<section><h3>Найдено в логах</h3>${rows.join('')}
        <div class="empty" data-css="margin-top:9px">Это факт о контейнере, а не о
          конкретном запросе: из строки лога не видно, в рамках какого вызова она
          возникла. Поэтому такие связи в цепочку потока не входят.
        </div></section>`);
    }
  }

  // шина: что реально прошло по топику на прогоне
  // `fileEvents` — сумма по всем записям артефакта: агент копит прогоны в одном
  // файле и не размечает их, поэтому это не «за прогон». Тишина считается только
  // по позадачным записям; у сессионного среза её нет (`silentTests` не задан).
  if (n.kind === 'topic' && m.fileEvents != null) {
    add('run', `<section><h3>Замеры шины</h3><div class="kv">
      <div class="k">Событий</div><div>${recNum(m.fileEvents)}</div>
      <div class="k">Записей</div><div>${recNum(m.fileRecords)}</div>
      ${m.silentTests != null ? `<div class="k">Тестов с событием в тишине</div>
        <div>${recNum(m.silentTests)}</div>` : ''}</div>
      <div class="empty" data-css="margin-top:8px">Сумма по всем записям артефакта
        <code>bus.flow</code>: агент дописывает прогоны в один файл, не размечая
        их, поэтому это не число за прогон.
        ${m.silentTests != null ? `«Тестов с событием в тишине» — разные
        тесты, у позадачных записей которых стоит признак <code>afterWrite</code>. Стенд общий, и признак не
        доказательство.` : `Замер снят на весь прогон: кто писал после события —
        API или консьюмер — из него не видно (под pytest-xdist признак
        <code>afterWrite</code> всегда <code>false</code>).`}
      </div></section>`);
  }

  // хранилище: схема таблицы прямо в карточке
  if (n.kind === 'table' || n.kind === 'store') {
    if (m.logOwners) {
      add('main', `<section><h3>Кто называет её в логах</h3><div class="chips">${
        Object.entries(m.logOwners).map(([k, v]) =>
          `<span class="chip">${esc(k)} <b>${recNum(v)}</b></span>`).join('')}</div>
        <div class="empty" data-css="margin-top:8px">Проверка ручной разметки
          <code>containers.match</code>: контейнер сам напечатал имя этой
          таблицы. Если здесь не тот контейнер, которому таблицу приписала
          карта, — разметку надо править.</div></section>`);
    }
    add('main', renderTable(n));
  }

  const links = (arr, title, dir) => arr.length ? `<section><h3>${title}</h3><div class="chips">${
    arr.map((e) => {
      const id = dir === 'out' ? e.target : e.source;
      return `<button class="chip" data-goto="${esc(id)}" title="${esc(e.label || '')}">${
        esc(e.kind)} → ${esc((index[id] || {}).label || id)}</button>`;
    }).join('')}</div></section>` : '';
  add('links', links(d.outgoing, 'Дальше по цепочке', 'out'));
  add('links', links(d.incoming, 'Кто обращается', 'in'));

  const obs = m.observed || [];
  if (obs.length) {
    add('run', `<section><h3>Проверки в прогоне (${recNum(m.checkCount)})</h3>` +
      obs.slice(0, 8).map((o) => `<div class="test">
        <div class="tname">${esc(o.test)}</div>
        <div class="nodeid">${esc(o.nodeid || '')}</div>
        <div class="row">
          <span class="chip ${o.status === 'passed' ? 'ok' : 'bad'}">${esc(o.status)}</span>
          ${o.expect ? `<span class="chip">ожидали ${esc(o.expect)}</span>` : ''}
          ${(o.attachments || []).map((a) => `<span class="chip">${esc(a)}</span>`).join('')}
        </div>
        <div class="checks">${(o.checks || []).map((c) => `<div>
          <span class="mark" data-css="color:${c.status === 'passed' ? COV.full : COV.empty}">
          ${c.status === 'passed' ? '✓' : '✕'}</span>${esc(c.name)}</div>`).join('')}</div>
        ${o.nodeid ? `<div class="row" data-css="margin-top:8px">
          ${window.vehaApi.isStatic ? window.vehaApi.offline('Перегнать кейс', 'act')
            : `<button class="act" data-rerun="${esc(o.nodeid)}">Перегнать кейс</button>`}</div>` : ''}
      </div>`).join('') + '</section>');
  } else if (n.layer === 'api') {
    add('run', '<section><div class="empty">В текущем allure-results вызовов нет</div></section>');
  }
  if (d.tests.length) {
    add('run', `<section><h3>Тесты, где встречается</h3><div class="chips">${
      d.tests.slice(0, 15).map((t) => `<span class="chip">${esc(t.name)}</span>`).join('')}</div></section>`);
  }

  // закладка живёт до смены узла: перерисовка карточки не должна сбрасывать её
  if (panelOf !== n.id) { panelOf = n.id; panelTab = 'main'; }
  const shown = PANEL_TABS.filter(([k]) => (parts[k] || []).length);
  if (!shown.length) return '<div class="empty">Про этот узел мы ничего не знаем</div>';
  if (!shown.some(([k]) => k === panelTab)) panelTab = shown[0][0];

  const strip = shown.length > 1 ? `<div class="ptabs">${shown.map(([k, l]) =>
    `<button class="ptab ${panelTab === k ? 'on' : ''}" data-ptab="${k}">${l}
      <b>${(parts[k] || []).length}</b></button>`).join('')}</div>` : '';
  return strip + `<div class="psec">${(parts[panelTab] || []).join('')}</div>`;
}

/* Схема таблицы: колонки, типы, ключи и внешние связи.
 * Данные — артефакт `db.schema` агента partest-atlas, снятый строго на чтение. */
function renderTable(n) {
  const t = tableByName(n.label);
  if (!t) {
    return `<section><h3>Схема таблицы</h3>
      <div class="empty">Нет снимка схемы: артефакт <code>db.schema</code>
      агента partest-atlas не собран или не прочитан (см. <code>/api/artifacts</code>).
      </div></section>`;
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
      ${recNum(touched.delete) ? `<span class="chip bad">−${recNum(touched.delete)}</span>` : ''}
    </div>
    <div class="hint-row">Сессионный замер: известно, что прогоны меняли таблицу,
      но не известно, какой именно тест — статистика PostgreSQL публикуется
      пачками. Числа — сумма по ${recNum(touched.records)} сессионным записям артефакта:
      агент копит прогоны в одном файле, не размечая их, и схлопывает совпадающие
      записи, поэтому это не число за прогон. Точную привязку даёт запуск немногих
      тестов без pytest-xdist, см. <code>db_effects.per_test_limit</code>.</div>
  </section>` : ''}
  ${eff.length ? `<section><h3>Что задели тесты (${eff.length})</h3>
    <div class="hint-row">Окно замера — подготовка, вызов и уборка теста: сюда
      входит и запись фикстур, а фикстура модуля или сессии приписана тесту, на
      котором сработала.</div>
    ${eff.slice(0, 12).map((e) => `<div class="test">
      <div class="tname">${esc(e.test)}</div>
      <div class="row">
        ${recNum(e.insert) ? `<span class="chip ok">+${recNum(e.insert)}</span>` : ''}
        ${recNum(e.update) ? `<span class="chip">~${recNum(e.update)}</span>` : ''}
        ${recNum(e.delete) ? `<span class="chip bad">−${recNum(e.delete)}</span>` : ''}
        ${e.shared ? '<span class="chip">стенд общий</span>' : ''}
      </div></div>`).join('')}</section>`
    : (touched ? '' : `<section><div class="empty">Нет данных о записи. Снять
       агентом partest-atlas: <code>ATLAS_DB_EFFECTS=1 pytest</code></div></section>`)}`;
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
      box.style.borderColor = d.passed ? COV.full : COV.empty;
    }
  } catch (e) { box.textContent = 'Сервис недоступен: ' + e; }
  finally { btn.disabled = false; btn.textContent = old; }
}

function closePanel() {
  selected = null;
  panel.classList.remove('open');
  draw();
  announceSelection();
}

/* Окнам нужно знать, что выделено: они подсвечивают строку и дают её снять. */
function announceSelection() {
  document.dispatchEvent(new CustomEvent('atlas:selection',
    { detail: selected ? selected.id : '' }));
}

/* ------------------------------------------------------------------ панели */
function renderStats() {
  const m = G.meta;
  const shown = placed.filter(visible).length;
  document.getElementById('stats').innerHTML = [
    ['узлов', `${shown}/${recNum(m.nodes)}`],
    ['связей', edgeList.length],
    [dataSet === 'tests' ? 'тестов' : 'эндпоинтов',
     recNum(dataSet === 'tests' ? m.tests : m.endpoints)],
  ].map(([k, v]) => `<span class="stat">${k} <b>${v}</b></span>`).join('')
    + artifactWarning();
}

/* Артефакт, который сервер не прочитал (новее известной схемы, битый, не сходится
 * с контрактом), не должен выглядеть как «ничего не собрано». Подробности —
 * в подсказке и в /api/artifacts. */
function artifactWarning() {
  const a = G.artifacts || {};
  const chip = (text, notes, color) => `<span class="stat" data-css="color:${color}"`
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
      a.scopeChosen === false ? '#c0392b' : '#d68910'));
  }

  const bad = (a.artifacts || []).filter((x) => x.state !== 'ok' && x.state !== 'missing');
  const notes = bad.map((x) => `${x.file}: ${x.state} — ${(x.problems || []).join('; ')}`);
  if (G.meta.configError) notes.unshift(`atlas.yaml: ${G.meta.configError}`);
  (a.ignored || []).forEach((x) => notes.push(x));
  if (notes.length) {
    out.push(chip(`артефакты: <b>${notes.length} не прочитано</b>`, notes, '#c0392b'));
  }

  const warns = (a.artifacts || []).flatMap((x) =>
    (x.warnings || []).map((w) => `${x.file}: ${w}`));
  if (warns.length) out.push(chip(`предупреждений: <b>${warns.length}</b>`, warns, '#d68910'));

  // Записи агента, не давшие ни одной связи, — по причинам. Пустые отчёты Allure — не
  // потеря, а очистка: так и сказано.
  const col = G.collected || {};
  const lost = Object.entries(col.lostRecords || {}).filter(([, n]) => n > 0);
  if (col.reportsCleared && (lost.length || col.dbEffects)) {
    out.push(chip('<b>отчёты очищены</b>',
      ['отчётов Allure нет — записи агента не с чем соединить; связи появятся после прогона'],
      '#d68910'));
  } else {
    if (lost.length) {
      const total = lost.reduce((a, [, n]) => a + n, 0);
      out.push(chip(`потеряно записей агента: <b>${total}</b>`,
        lost.map(([k, n]) => `${n} — ${(col.lostLabels || {})[k] || k}`), '#d68910'));
    }
    // не потеря: db.effects накопительный, записи от прошлых тестов в нём есть всегда
    if (col.notInReports) {
      const n = Number(col.notInReports) || 0;
      out.push(chip(`не в текущих отчётах: <b>${n}</b>`,
        [`${n} записей агента от тестов, которых нет в текущих отчётах`],
        '#7f8c8d'));
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
      '#d68910'));
  }
  // Снятый opt-in-набор — штатный полный прогон, но без этих тестов: не тревога,
  // а то, что надо знать, сравнивая процент с прогоном, где набор был включён.
  if (G.meta.optinActed && optinText.length) {
    out.push(chip(`opt-in: <b>${esc(optinText.join('; '))}</b>`,
      ['наборы маркеров, которые partest снимает с прогона, пока их не попросили '
        + '(-m или переменная окружения)',
       'покрытие описывает прогон в этом состоянии; с прогоном, где наборы решены '
        + 'иначе, процент несравним',
       ...optinText], '#7f8c8d'));
  }
  return out.join('');
}

/* Легенда паутины объясняет, что именно связывает узлы в текущем наборе. */
function renderEdgeLegend() {
  const items = [];
  if (edgeLayers.structure) items.push(
    ['calls', 'кнопка → эндпоинт'], ['publish', 'публикация в шину'],
    ['writes', 'пишет в таблицу'], ['reads', 'читает таблицу'],
    ['depends', 'ходит во внешнюю систему'],
    ['publishes', 'публикация, снятая на прогоне'],
    ['consumes', 'запись сделал консьюмер, не API'],
    ['logged', 'контейнер упомянул это в логах']);
  if (edgeLayers.coverage) items.push(
    ['gap', 'одинаковые пробелы оси B'], ['subtype', 'один подтип оси A']);
  if (edgeLayers.tests) items.push(
    ['cotest', 'вызваны в одном тесте'], ['cofeature', 'одна фича в отчёте']);
  if (!items.length) items.push(['flow', 'все слои связей выключены']);
  document.getElementById('edge-legend').innerHTML = items.map(([k, t]) =>
    `<div><span class="ln" data-css="background:${rgba(EDGE[k], 0.95)}"></span> ${t}</div>`
  ).join('') + `
    <div data-css="margin-top:7px"><span class="ln" data-css="background:#fff"></span>
      импульс запроса — от источника вниз по потоку</div>
    <div><span class="ln" data-css="background:${RESPONSE_COLOR}"></span>
      импульс ответа — обратно к источнику</div>`;
}
function renderLayers() {
  const counts = {};
  G.nodes.forEach((n) => (counts[n.layer] = (counts[n.layer] || 0) + 1));
  document.getElementById('layers').innerHTML = G.layers.map((l) => `
    <div class="layer${layerOn[l.key] ? '' : ' off'}" data-layer="${l.key}">
      <span class="sw" data-css="background:${LC[l.key]}"></span>
      <span class="name">${l.label}</span><span class="n">${counts[l.key] || 0}</span>
    </div>`).join('');
  document.querySelectorAll('.layer').forEach((el) => el.addEventListener('click', () => {
    const k = el.dataset.layer;
    layerOn[k] = !layerOn[k];
    el.classList.toggle('off', !layerOn[k]);
    draw();
  }));
}
function renderLegend() {
  const items = dataSet === 'tests'
    ? [[RUN.passed, 'все проверки прошли'], [RUN.failed, 'есть падения'],
       [RUN.none, 'в прогоне не вызывался']]
    : [[COV.full, 'полное'], [COV.partial, 'частичное'],
       [COV.empty, 'пустое'], [COV.unseen, 'нет данных']];
  document.getElementById('legend').innerHTML = items
    .map(([c, t]) => `<div><span class="sw" data-css="background:${c}"></span> ${t}</div>`).join('');
}

/* Список экранов — реальные снимки из манифеста попиксельного сравнения.
 * Он же самый честный перечень экранов продукта: 27 сцен с путями и PNG. */
function renderScreens() {
  const shots = G.screens || [];
  const alias = G.screenAlias || {};
  document.getElementById('screens').innerHTML = shots.map((s) => `
    <div class="screen${activeShot && s.id === activeShot.id ? ' on' : ''}"
         data-shot="${esc(s.id)}">
      <img src="${esc(s.image)}" alt="" loading="lazy">
      <span class="t">${esc(s.title || s.id)}</span>
      <span class="p">${esc(s.path || '')}</span>
    </div>`).join('') || '<div class="muted">нет снимков экранов</div>';

  document.querySelectorAll('[data-shot]').forEach((el) =>
    el.addEventListener('click', () => {
      activeShot = shots.find((s) => s.id === el.dataset.shot) || null;
      // подтягиваем экран из app_map, чтобы контролы встали на своё место
      const key = Object.keys(alias).find((k) => alias[k] === (activeShot || {}).id);
      const node = key ? G.nodes.find((n) => n.id === 'screen:' + key) : null;
      // раскладку не трогаем: экран влияет только на видимость контролов,
      // а полный пересчёт зон на каждый клик по превью — лишняя работа
      activeScreen = node ? node.id : null;
      activeScreenPath = (activeShot && activeShot.screenPath) || null;
      renderScreens(); draw();
    }));
}

bind('refresh', 'click', async (e) => {
  if (window.vehaApi.isStatic) return;   // снимок не пересобирается: кнопка выключена
  e.target.disabled = true; e.target.textContent = 'Собираю…';
  await load(true);
  e.target.disabled = false; e.target.textContent = 'Обновить данные';
});
bind('reset', 'click', () => { view = defaultView(); fitView(); draw(); });
// «В лоб» — карта разворачивается фасадом: страница читается как страница
bind('flat', 'click', () => {
  view = mode === 'ring'
    ? { rx: 1.35, ry: 0, zoom: 1, ox: 0, oy: 0 }
    : { rx: 0.06, ry: 0, zoom: 1, ox: 0, oy: 0 };
  fitView(); draw();
});
bind('close', 'click', closePanel);

/* Закладки: одна кнопка — одна панель. Повторный клик закрывает, поэтому
 * сцена всегда в одном клике от полного экрана. */
let openSide = null;
function toggleSide(name) {
  openSide = openSide === name ? null : name;
  document.querySelectorAll('[data-panel]').forEach((b) =>
    b.classList.toggle('on', b.dataset.panel === openSide));
  document.querySelectorAll('[data-side]').forEach((el) => {
    el.hidden = el.dataset.side !== openSide;
  });
  document.body.classList.toggle('no-left', !openSide);
  fitView(); draw();
}
const sceneClear = document.getElementById('scene-clear');
if (sceneClear) sceneClear.addEventListener('click', clearSceneFilters);

/* Легенда форм: без неё цилиндр и стопка читаются как «просто фигурки». */
function renderShapeLegend() {
  const box = document.getElementById('shape-legend');
  if (!box) return;
  box.innerHTML = [
    ['здание', 'эндпоинт, высота — нагрузка'],
    ['стопка', 'таблица БД'],
    ['цилиндр', 'хранилище'],
    ['шайба', 'топик шины'],
    ['куб', 'сервис или контейнер'],
    ['карточка', 'элемент интерфейса'],
  ].map(([k, v]) => `<div><b data-css="color:var(--text)">${k}</b> — ${v}</div>`).join('');
}

bind('rail', 'click', (e) => {
  const b = e.target.closest('[data-panel]');
  if (b) toggleSide(b.dataset.panel);
});
document.querySelectorAll('[data-close]').forEach((el) =>
  el.addEventListener('click', () => toggleSide(openSide)));

/* Быстрые фильтры на сцене. */
document.querySelectorAll('[data-scope]').forEach((el) =>
  el.addEventListener('click', () => setScope(el.dataset.scope)));

/* Быстрые фильтры применяются только к эндпоинтам: гасить таблицы и топики
 * по методу HTTP бессмысленно, они и так остаются точками маршрута. */
function sceneFilterOk(n) {
  const m = n.meta || {};
  if (SF.method && m.method !== SF.method) return false;
  if (SF.status && (m.status || 'unseen') !== SF.status) return false;
  if (SF.run && (m.runStatus || 'none') !== SF.run) return false;
  if (SF.gaps && !(m.missing || []).length) return false;
  if (SF.untested && (m.testCount || 0) > 0) return false;
  if (SF.slow && !((m.timing || {}).msP95 > 100)) return false;
  if (SF.external && !(m.depends || []).length) return false;
  return true;
}

function sceneFilterActive() {
  return Object.values(SF).some(Boolean);
}

function clearSceneFilters() {
  Object.keys(SF).forEach((k) => { SF[k] = typeof SF[k] === 'boolean' ? false : ''; });
  renderQuickChips(); renderFiltersPane(); renderStats(); draw();
}

function setScope(next) {
  scope = next;
  if (next !== 'domain') scopeDomain = null;
  if (next === 'domain' && !scopeDomain) {
    scopeDomain = (G.domains || [])[0] ? G.domains[0].key : null;
  }
  document.querySelectorAll('[data-scope]').forEach((x) =>
    x.classList.toggle('on', x.dataset.scope === next));
  renderDomainChips();
  renderStats();
  draw();
}

/* Чипы среза: домены или слои — в зависимости от выбранного режима. */
function renderDomainChips() {
  const box = document.getElementById('scope-chips');
  if (!box) return;
  if (scope === 'domain') {
    const counts = {};
    G.nodes.filter((n) => n.layer === 'api')
      .forEach((n) => { const k = n.meta.domain || 'other'; counts[k] = (counts[k] || 0) + 1; });
    box.innerHTML = (G.domains || []).filter((d) => counts[d.key]).map((d) =>
      `<button class="chip ${scopeDomain === d.key ? 'on' : ''}" data-dom="${esc(d.key)}">${
        esc(d.label)} <b>${counts[d.key]}</b></button>`).join('');
    box.querySelectorAll('[data-dom]').forEach((el) => el.addEventListener('click', () => {
      scopeDomain = el.dataset.dom; renderDomainChips(); renderStats(); draw();
    }));
    return;
  }
  if (scope === 'layer') {
    box.innerHTML = (G.layers || []).map((l) =>
      `<button class="chip ${scopeLayer === l.key ? 'on' : ''}" data-lay="${esc(l.key)}">${
        esc(l.label)}</button>`).join('');
    box.querySelectorAll('[data-lay]').forEach((el) => el.addEventListener('click', () => {
      scopeLayer = el.dataset.lay; renderDomainChips(); renderStats(); draw();
    }));
    return;
  }
  box.innerHTML = '';
}

/* Быстрые чипы: самые частые вопросы к карте — что упало, где дыры, что
 * тормозит, что вообще не тестировали. */
/* На полосе — только то, что спрашивают каждый день. Остальное живёт в панели
 * «Фильтры»: восемь чипов над сценой закрывали саму сцену. */
const QUICK = [
  ['run', 'failed', 'упало'],
  ['gaps', true, 'есть дыры'],
];
function renderQuickChips() {
  const box = document.getElementById('quick-chips');
  if (!box) return;
  box.innerHTML = QUICK.map(([k, v, label], i) => {
    const on = SF[k] === v;
    return `<button class="chip ${on ? 'on' : ''}" data-qf="${i}">${esc(label)}</button>`;
  }).join('');
  box.querySelectorAll('[data-qf]').forEach((el) => el.addEventListener('click', () => {
    const [k, v] = QUICK[Number(el.dataset.qf)];
    SF[k] = SF[k] === v ? (typeof v === 'boolean' ? false : '') : v;
    renderQuickChips(); renderFiltersPane(); renderStats(); draw();
  }));
  const clear = document.getElementById('scene-clear');
  if (clear) clear.classList.toggle('on', sceneFilterActive());
}

/* Подробная панель фильтров: то же состояние, но со всеми значениями. */
function renderFiltersPane() {
  const box = document.getElementById('filters-pane');
  if (!box || !G) return;
  const api = G.nodes.filter((n) => n.layer === 'api');
  const uniq = (fn) => [...new Set(api.map(fn).filter(Boolean))].sort();
  const group = (title, key, values, labels) => `<h3>${title}</h3><div class="chips">
      <button class="chip ${SF[key] ? '' : 'on'}" data-sf="${key}" data-v="">все</button>
      ${values.map((v) => `<button class="chip ${SF[key] === v ? 'on' : ''}"
        data-sf="${key}" data-v="${esc(v)}">${esc((labels && labels[v]) || v)}</button>`).join('')}
    </div><div class="divider"></div>`;

  const shown = placed.filter(visible).length;
  box.innerHTML = `
    <div class="hint-row">Показано ${shown} из ${placed.length} узлов сцены.</div>
    ${group('Метод', 'method', uniq((n) => n.meta.method))}
    ${group('Покрытие', 'status', ['full', 'partial', 'empty', 'unseen'],
            { full: 'полное', partial: 'частичное', empty: 'пустое', unseen: 'нет данных' })}
    ${group('Прогон', 'run', ['passed', 'failed', 'none'],
            { passed: 'зелёные', failed: 'с падениями', none: 'не вызывался' })}
    <h3>Признаки</h3>
    <label class="check"><input type="checkbox" data-sfb="gaps" ${SF.gaps ? 'checked' : ''}>
      есть недостающие TC</label>
    <label class="check"><input type="checkbox" data-sfb="untested" ${SF.untested ? 'checked' : ''}>
      не встречался в прогоне</label>
    <label class="check"><input type="checkbox" data-sfb="slow" ${SF.slow ? 'checked' : ''}>
      p95 больше 100 мс</label>
    <label class="check"><input type="checkbox" data-sfb="external" ${SF.external ? 'checked' : ''}>
      ходит во внешнюю систему</label>
    <label class="check"><input type="checkbox" data-sfb="linked" ${SF.linked ? 'checked' : ''}>
      только связанные с чем-нибудь</label>
    <div class="divider"></div>
    <button class="act" data-sf-clear>Снять фильтры сцены</button>`;

  box.querySelectorAll('[data-sf]').forEach((el) => el.addEventListener('click', () => {
    SF[el.dataset.sf] = el.dataset.v;
    renderQuickChips(); renderFiltersPane(); renderStats(); draw();
  }));
  box.querySelectorAll('[data-sfb]').forEach((el) => el.addEventListener('change', () => {
    SF[el.dataset.sfb] = el.checked;
    renderQuickChips(); renderFiltersPane(); renderStats(); draw();
  }));
  const c = box.querySelector('[data-sf-clear]');
  if (c) c.addEventListener('click', clearSceneFilters);
}

function renderSceneHint() {
  const el = document.getElementById('scene-hint');
  if (!el) return;
  const f = selected || hover;
  let text = '';
  if (scope === 'chain' && !f) text = 'выберите узел — покажу его цепочку';
  else if (f) {
    const c = chain(f.id);
    text = `${f.label.slice(0, 40)} · поток ${c.flow.size - 1} · похожих ${c.related.size}`;
  }
  el.textContent = text;
  el.classList.toggle('on', !!text);
}
/* Два главных переключателя: вид сцены и набор данных. Второй меняет и цвет
 * узлов, и саму структуру паутины, поэтому пересобираем рёбра и цепочки. */
function setMode(next) {
  if (mode === next) return;
  mode = next;
  document.querySelectorAll('[data-view]').forEach((x) =>
    x.classList.toggle('on', x.dataset.view === next));
  view = defaultView();
  layout(); fitView(); draw();
}

function setDataSet(next) {
  dataSet = next;
  document.querySelectorAll('[data-data]').forEach((x) =>
    x.classList.toggle('on', x.dataset.data === next));
  refreshEdges();
  renderLegend(); renderEdgeLegend(); renderDomainChips();
  renderQuickChips(); renderShapeLegend(); renderFiltersPane();
  renderEdgeLayers(); renderStats();
  document.dispatchEvent(new CustomEvent('atlas:dataset', { detail: dataSet }));
  draw();
}

document.querySelectorAll('[data-view]').forEach((el) =>
  el.addEventListener('click', () => setMode(el.dataset.view)));
document.querySelectorAll('[data-data]').forEach((el) =>
  el.addEventListener('click', () => setDataSet(el.dataset.data)));

const densityEl = document.getElementById('density');
if (densityEl) densityEl.addEventListener('input', (e) => {
  edgeDensity = Number(e.target.value) / 100;
  document.getElementById('density-val').textContent = e.target.value + '%';
  refreshEdges();
  renderStats(); renderEdgeLayers();
  draw();
});
const dimEl = document.getElementById('dim-edges');
if (dimEl) dimEl.addEventListener('change', (e) => {
  showDimEdges = !e.target.checked; draw();
});
// «только покрытие < 100%» и «только связанные» переехали в фильтры сцены:
// два отдельных чекбокса делали ровно то же, что чипы «есть дыры» и «связанные»
bind('spin', 'change', (e) => {
  spinning = e.target.checked; if (spinning) requestAnimationFrame(tick);
});
function tick() { if (!spinning) return; view.ry += 0.0022; draw(); requestAnimationFrame(tick); }
window.addEventListener('resize', () => { fitView(); draw(); });
window.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePanel(); });

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

/* ------------------------------------------------------- поиск по сцене ---
 * Ищем по всем узлам, а не только по эндпоинтам: таблицу и топик найти так же
 * нужно. Выбор ведёт к узлу и раскрывает его цепочку. */
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
      <span class="p">${esc(n.meta.service || n.kind)}</span>
    </div>`).join('');
  acEl.classList.add('on');
  acEl.querySelectorAll('[data-go]').forEach((el) =>
    el.addEventListener('mousedown', (e) => { e.preventDefault(); gotoNode(el.dataset.go); }));
}

/* Подвести камеру к узлу. Без этого выбор из таблицы «срабатывает», но узел
 * остаётся где-то за кадром, и путь всё равно приходится искать глазами. */
function focusNode(n, { zoom = Math.max(0.75, Math.min(1.15, view.zoom * 1.5)) } = {}) {
  if (!n) return;
  const p = project(nodeTop(n));
  const cx = padLeft() + (cv.clientWidth - padLeft() - padRight()) / 2;
  const cy = PAD.top + (cv.clientHeight - PAD.top - padBottom()) / 2;
  const k = Math.max(0.2, Math.min(2.4, zoom));
  // сдвигаем так, чтобы узел встал в центр свободной области при новом масштабе
  const rel = { x: p.sx - view.ox, y: p.sy - view.oy };
  view.ox = cx - (rel.x - cv.clientWidth / 2) * (k / view.zoom) - cv.clientWidth / 2;
  view.oy = cy - (rel.y - cv.clientHeight / 2) * (k / view.zoom) - cv.clientHeight / 2;
  view.zoom = k;
  draw();
}

function gotoNode(id) {
  acEl.classList.remove('on');
  qEl.blur();
  const n = index[id];
  if (!n) return;
  // если узел скрыт фильтром слоёв — включаем его слой, иначе выбор «немой»
  if (!layerOn[n.layer]) { layerOn[n.layer] = true; renderLayers(); }
  selected = n; openNode(n); focusNode(n);
}

if (qEl) {
  qEl.addEventListener('input', () => runSearch(qEl.value));
  qEl.addEventListener('focus', () => runSearch(qEl.value));
  qEl.addEventListener('blur', () => setTimeout(() => acEl.classList.remove('on'), 120));
  qEl.addEventListener('keydown', (e) => {
    if (!acItems.length) return;
    if (e.key === 'ArrowDown') { acSel = (acSel + 1) % acItems.length; renderAc(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { acSel = (acSel - 1 + acItems.length) % acItems.length; renderAc(); e.preventDefault(); }
    else if (e.key === 'Enter' && acSel >= 0) { gotoNode(acItems[acSel].id); e.preventDefault(); }
    else if (e.key === 'Escape') { acEl.classList.remove('on'); qEl.blur(); }
  });
}

/* --------------------------------------------------------- палитра команд
 * Один вход во всё: узлы, экраны, вкладки дока и режимы. Приём из
 * coverage_report.html, где он экономит больше всего кликов. */
const palEl = document.getElementById('palette');
const palQ = document.getElementById('pal-q');
const palList = document.getElementById('pal-list');
let palItems = [], palSel = 0;

function paletteCommands() {
  // палитру могут открыть до того, как приехал граф — команды сцены работают
  // и без него, а узлы просто ещё не в списке
  const cmds = [
    { kind: 'вид', label: 'Карта', hint: '1', run: () => setMode('map') },
    { kind: 'вид', label: 'Кольцо', hint: '2', run: () => setMode('ring') },
    { kind: 'данные', label: 'Покрытие', hint: 'q', run: () => setDataSet('coverage') },
    { kind: 'данные', label: 'Тесты', hint: 'w', run: () => setDataSet('tests') },
    { kind: 'сцена', label: 'Показать всё', run: () => setScope('all') },
    { kind: 'сцена', label: 'Только цепочка', run: () => setScope('chain') },
    { kind: 'сцена', label: 'Сбросить камеру', run: () => { view = defaultView(); fitView(); draw(); } },
  ];
  document.querySelectorAll('[data-tab]').forEach((b) => cmds.push({
    kind: 'окно', label: b.textContent.trim(), run: () => b.click(),
  }));
  if (!G) return cmds;
  (G.screens || []).forEach((sc) => cmds.push({
    kind: 'экран', label: sc.title || sc.id, hint: sc.path,
    run: () => {
      const el = document.querySelector(`[data-shot="${sc.id}"]`);
      if (el) el.click();
    },
  }));
  G.nodes.forEach((n) => cmds.push({
    kind: n.layer === 'api' ? 'эндпоинт' : (n.kind === 'table' ? 'таблица' : n.kind),
    label: n.label, hint: n.meta.service || '',
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
  else if (e.key === 'Escape') closePalette();
});
bind('palette-btn', 'click', openPalette);
window.addEventListener('mousedown', (e) => {
  if (palEl.classList.contains('on') && !palEl.contains(e.target)) closePalette();
});

/* Горячие клавиши: «/» — поиск, «1/2» — вид, «q/w» — набор данных. */
window.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    openPalette(); e.preventDefault(); return;
  }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (typing) return;
  if (e.key === '/') { qEl.focus(); e.preventDefault(); }
  else if (e.key === '1') setMode('map');
  else if (e.key === '2') setMode('ring');
  else if (e.key === 'q') setDataSet('coverage');
  else if (e.key === 'w') setDataSet('tests');
  else if (e.key === 'l') toggleSide(openSide || 'screens');
  else if (e.key === 'f') setScope(scope === 'chain' ? 'all' : 'chain');
});

// служебный хук: выбрать узел по id и посмотреть состояние камеры из консоли
window.atlas = {
  select(id) {
    // select(null) — снять выделение: окна должны уметь его отменить, иначе
    // выбранный в таблице эндпоинт снимался только перезагрузкой страницы
    if (!id) { closePanel(); return true; }
    const n = index[id];
    if (n) { selected = n; openNode(n); draw(); }
    return !!n;
  },
  get selectedId() { return selected ? selected.id : ''; },
  setFilter(fn) { filterFn = fn; draw(); },
  setDataSet(v) { setDataSet(v); },
  setMode(v) { setMode(v); },
  redraw() { draw(); },
  refit() { fitView(); draw(); },
  setCamDist(v) { CAM_DIST = v; fitView(); draw(); return CAM_DIST; },
  get view() { return view; },
  get graph() { return G; },
  focus(id) { const n = index[id]; if (n) { selected = n; openNode(n); focusNode(n); } return !!n; },
  get edgeList() { return edgeList; },
  visibleOf(n) { return visible(n); },
  /* Подсветка идущего прогона: эндпоинты, которых он уже коснулся, и те,
   * где упало. Обновляется раз в секунду отдельно от кадра сцены. */
  setRunHighlight(touched, failed) {
    runTouched = new Set(touched || []);
    runFailed = new Set(failed || []);
    draw();
  },
  get activeScreenPath() { return activeScreenPath; },
  /* Live: состояние контейнеров красит объекты сервисов на сцене. */
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
    draw();
  },
  chainInfo(id) { const c = chain(id); return { flow: c.flow.size, related: c.related.size, edges: c.edges.size }; },
  get dataSet() { return dataSet; },
  get mode() { return mode; },
  get placed() { return placed; },
  get zones() { return ZONES; },
  get ground() { return GROUND; },
  get wall() { return WALL; },
  fit() { fitView(); draw(); },
};

requestAnimationFrame(() => load(false));
