/* Откуда вебморда берёт данные — и как она живёт под строгой CSP.
 *
 * Два режима. «Сервер» — `veha serve`: всё по `/api/*`, как всегда. «Снимок» —
 * `veha export-static`: те же ответы лежат JSON-файлами рядом со страницей и читаются
 * по относительным путям (снимок кладут в подкаталог сайта), а действия, которым нужен
 * сервер, выключены с подписью «в демо недоступно». Режим задаёт разметка:
 * `<meta name="veha-mode" content="static">` ставит экспорт.
 *
 * Стили из собираемой строками разметки. CSP без 'unsafe-inline' в style-src
 * отбрасывает атрибут style в такой разметке, поэтому она пишет `data-css="…"`, а стиль
 * ставится здесь через CSSOM (`el.style.cssText`) — его CSP не ограничивает.
 * Наблюдатель срабатывает до отрисовки кадра, так что разметка без стиля не мелькает.
 */
'use strict';

window.vehaApi = (function () {
  const meta = document.querySelector('meta[name="veha-mode"]');
  const STATIC = !!meta && meta.content === 'static';
  const OFFLINE = 'в демо недоступно';

  /* ---------------------------------------------------------- стили под CSP */
  const applyCss = (el) => { el.style.cssText = el.getAttribute('data-css') || ''; };
  const sweep = (node) => {
    if (node.nodeType !== 1) return;
    if (node.hasAttribute('data-css')) applyCss(node);
    node.querySelectorAll('[data-css]').forEach(applyCss);
  };
  new MutationObserver((records) => records.forEach((r) => {
    if (r.type === 'attributes') applyCss(r.target);
    else r.addedNodes.forEach(sweep);
  })).observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, attributeFilter: ['data-css'],
  });
  sweep(document.documentElement);

  /* ------------------------------------------------------------------ снимок */
  const files = new Map();
  // Имя файла из данных (результат Allure, вложение) — только простое имя: путь из
  // чужих данных не должен уводить чтение за каталог снимка. `.` и `..` — не имена.
  const safe = (name) => /^(?!\.{1,2}$)[A-Za-z0-9._-]+$/.test(String(name || ''));

  // В снимке нет сервера, и ни один запрос не должен уходить с методом, который что-то
  // меняет: кнопки таких действий выключены, а это — второй замок на случай, если
  // какая-то из них выключена не будет. Ошибка бросается сразу, до сети.
  if (STATIC) {
    const realFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const method = String((init && init.method)
        || (input && typeof input === 'object' && input.method) || 'GET').toUpperCase();
      if (method !== 'GET' && method !== 'HEAD') {
        throw new Error(`${method} — ${OFFLINE}: в снимке нет сервера`);
      }
      return realFetch(input, init);
    };
    if (navigator.sendBeacon) {
      navigator.sendBeacon = () => { throw new Error(`sendBeacon — ${OFFLINE}`); };
    }
    const realOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, ...rest) {
      const m = String(method || 'GET').toUpperCase();
      if (m !== 'GET' && m !== 'HEAD') throw new Error(`${m} — ${OFFLINE}: в снимке нет сервера`);
      return realOpen.call(this, method, ...rest);
    };
  }

  function file(path) {
    if (!files.has(path)) {
      const p = fetch(path).then((r) => {
        if (!r.ok) throw new Error(`${path}: ${r.status}`);
        return r.json();
      });
      p.catch(() => files.delete(path));
      files.set(path, p);
    }
    return files.get(path);
  }

  const json = async (url) => (await fetch(url)).json();

  /* Фильтр списка тестов — тот же, что у сервера (`/api/tests`): снимок хранит
   * список целиком, уже упорядоченный, и режет его здесь. */
  function filterTests(d, status, q, limit) {
    const low = (q || '').toLowerCase();
    let rows = d.tests || [];
    if (status) rows = rows.filter((t) => t.status === status);
    if (low) {
      rows = rows.filter((t) => (t.name || '').toLowerCase().includes(low)
        || (t.nodeid || '').toLowerCase().includes(low)
        || (t.feature || '').toLowerCase().includes(low));
    }
    return { total: rows.length, failedKnown: d.failedKnown || 0,
             tests: rows.slice(0, Math.max(1, Math.min(limit || 400, 2000))) };
  }

  /* Кнопка действия, которому нужен сервер: в снимке — выключена и подписана. */
  function offline(label, cls) {
    return `<button class="${cls || 'act sm'}" disabled title="${OFFLINE}: нужен сервер veha">`
      + `${label}</button> <span class="muted">${OFFLINE}</span>`;
  }

  if (STATIC) {
    const brand = document.querySelector('header .brand');
    if (brand) {
      const tag = document.createElement('span');
      tag.className = 'badge';
      tag.title = 'Статический снимок на вымышленных данных: только просмотр';
      tag.textContent = 'демо · только просмотр';
      brand.after(tag);
    }
    // Пересобрать снимок некому: кнопки нет совсем, а не выключенной в шапке
    const refresh = document.getElementById('refresh');
    if (refresh) {
      refresh.disabled = true;
      refresh.hidden = true;
      refresh.title = `${OFFLINE}: данные — снимок, пересобирать их некому`;
    }
  }

  return {
    isStatic: STATIC,
    OFFLINE,
    offline,

    graph: (refresh) => (STATIC ? file('api/graph.json')
      : json('/api/graph' + (refresh ? '?refresh=true' : ''))),

    /* Карточка узла или null, если узла нет. */
    async node(id) {
      if (STATIC) {
        const index = await file('api/node/index.json');
        const f = Object.prototype.hasOwnProperty.call(index, id) ? index[id] : '';
        return f && safe(f) ? file('api/node/' + f) : null;
      }
      const r = await fetch('/api/node/' + encodeURIComponent(id));
      return r.ok ? r.json() : null;
    },

    async log(name) {
      if (!STATIC) return json('/api/log' + (name ? '?name=' + encodeURIComponent(name) : ''));
      const d = await file('api/log.json');
      const key = name || d.first;
      return (d.logs || {})[key]
        || { files: d.names || [], name: key, lines: [], note: `нет лога ${key}` };
    },

    async tests(status, q, limit) {
      if (!STATIC) {
        return json(`/api/tests?status=${encodeURIComponent(status)}`
          + `&q=${encodeURIComponent(q)}&limit=${limit}`);
      }
      return filterTests(await file('api/tests.json'), status, q, limit);
    },

    test(name) {
      if (!STATIC) return json('/api/test?file=' + encodeURIComponent(name));
      return safe(name) ? file('api/test/' + name) : Promise.reject(new Error('имя теста'));
    },

    attachment(source) {
      if (!STATIC) return json('/api/attachment/' + encodeURIComponent(source));
      return safe(source) ? file('api/attachment/' + source + '.json')
        : Promise.reject(new Error('имя вложения'));
    },

    attachmentImage(source) {
      if (!STATIC) return '/api/attachment/' + encodeURIComponent(source);
      return safe(source) ? 'api/attachment/' + source : '';
    },

    runner: () => (STATIC ? file('api/runner.json') : json('/api/runner/suites')),

    /* «Live»: у сервера — опрос снимков стенда, в снимке демо — те же ответы файлом. */
    liveContainers: () => (STATIC ? file('api/live/containers.json')
      : json('/api/live/containers')),
    liveMrs: () => (STATIC ? file('api/live/mrs.json') : json('/api/live/mrs')),
  };
}());
