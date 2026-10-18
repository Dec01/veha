/* Примитивы изометрической сцены: площадка, коробка, цилиндр, стопка пластин.
 *
 * Всё рисуется гранями от дальней к ближней, поэтому объекты корректно
 * перекрывают друг друга при любом повороте камеры — буфера глубины у Canvas 2D
 * нет. Проектор передаётся снаружи: модуль не знает про камеру атласа.
 *
 * Ось Y направлена вверх в мире и вниз на экране: основание объекта лежит на
 * `y`, вершина — на `y - height`.
 *
 * За «качество» отвечают три приёма, а не один:
 *   · градиент по грани вместо плоской заливки — грань перестаёт быть пятном;
 *   · светящаяся кромка верхней грани — читается силуэт объекта;
 *   · контактная тень под объектом — объект стоит на площадке, а не парит.
 */
'use strict';

window.S3 = (function () {
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  /** Осветлить/затемнить цвет: имитация освещения без настоящего света. */
  function shade(rgb, k, alpha = 1) {
    const f = (c) => clamp(Math.round(c * k), 0, 255);
    return `rgba(${f(rgb[0])},${f(rgb[1])},${f(rgb[2])},${alpha})`;
  }

  function polyPath(ctx, pts) {
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p.sx, p.sy) : ctx.moveTo(p.sx, p.sy)));
    ctx.closePath();
  }

  /** Заливка грани градиентом: от светлого угла к тёмному. */
  function faceFill(ctx, pts, rgb, k0, k1, alpha) {
    const a = pts[0], b = pts[Math.min(2, pts.length - 1)];
    const g = ctx.createLinearGradient(a.sx, a.sy, b.sx, b.sy);
    g.addColorStop(0, shade(rgb, k0, alpha));
    g.addColorStop(1, shade(rgb, k1, alpha));
    return g;
  }

  /** Контур скруглённого прямоугольника в плоскости XZ. */
  function roundRect(x, z, w, d, r, seg = 5) {
    const hw = w / 2, hd = d / 2;
    r = clamp(r, 0, Math.min(hw, hd));
    const out = [];
    const corners = [
      [x + hw - r, z - hd + r, -Math.PI / 2, 0],
      [x + hw - r, z + hd - r, 0, Math.PI / 2],
      [x - hw + r, z + hd - r, Math.PI / 2, Math.PI],
      [x - hw + r, z - hd + r, Math.PI, Math.PI * 1.5],
    ];
    corners.forEach(([cx, cz, a0, a1]) => {
      for (let i = 0; i <= seg; i++) {
        const a = a0 + (a1 - a0) * (i / seg);
        out.push({ x: cx + Math.cos(a) * r, z: cz + Math.sin(a) * r });
      }
    });
    return out;
  }

  /** Мягкая тень под объектом: объект должен касаться поверхности. */
  function contactShadow(ctx, project, o) {
    const c = project({ x: o.x, y: o.y + 0.5, z: o.z });
    const e = project({ x: o.x + o.r, y: o.y + 0.5, z: o.z });
    const rx = Math.abs(e.sx - c.sx) || 6;
    const ry = rx * 0.52;
    const g = ctx.createRadialGradient(c.sx, c.sy, 0, c.sx, c.sy, Math.max(rx, 4));
    g.addColorStop(0, `rgba(0,0,0,${o.alpha ?? 0.45})`);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save();
    ctx.translate(c.sx, c.sy);
    ctx.scale(1, ry / Math.max(rx, 1));
    ctx.beginPath();
    ctx.arc(0, 0, Math.max(rx, 4), 0, Math.PI * 2);
    ctx.restore();
    ctx.fillStyle = g;
    ctx.fill();
  }

  /* --------------------------------------------------------------- площадка */
  /** Плита-зона со скруглением, светящейся кромкой и разметкой. */
  function pad(ctx, project, o) {
    const h = o.h ?? 12;
    const outline = roundRect(o.x, o.z, o.w, o.d, o.r ?? 30);
    const bot = outline.map((p) => project({ x: p.x, y: o.y, z: p.z }));
    const top = outline.map((p) => project({ x: p.x, y: o.y - h, z: p.z }));
    const rgb = o.rgb;
    const alpha = o.alpha ?? 1;

    // боковая стенка сегментами, от дальнего к ближнему
    const segs = [];
    for (let i = 0; i < outline.length; i++) {
      const j = (i + 1) % outline.length;
      segs.push({
        pts: [bot[i], bot[j], top[j], top[i]],
        depth: (bot[i].depth + bot[j].depth) / 2,
      });
    }
    segs.sort((a, b) => b.depth - a.depth).forEach((s) => {
      polyPath(ctx, s.pts);
      ctx.fillStyle = faceFill(ctx, s.pts, rgb, 0.42, 0.2, alpha);
      ctx.fill();
    });

    // верхняя грань: тёмное стекло с лёгким свечением к центру
    polyPath(ctx, top);
    const c = project({ x: o.x, y: o.y - h, z: o.z });
    const edge = project({ x: o.x + o.w / 2, y: o.y - h, z: o.z });
    const rr = Math.max(20, Math.abs(edge.sx - c.sx));
    const g = ctx.createRadialGradient(c.sx, c.sy, 0, c.sx, c.sy, rr);
    g.addColorStop(0, shade(rgb, 0.5, (o.top ?? 0.3) * alpha));
    g.addColorStop(1, shade(rgb, 0.22, (o.top ?? 0.3) * 0.4 * alpha));
    ctx.fillStyle = g;
    ctx.fill();

    if (o.grid) {
      ctx.save();
      ctx.clip();
      ctx.strokeStyle = shade(rgb, 1.3, 0.13 * alpha);
      ctx.lineWidth = 1;
      const n = o.grid;
      for (let i = 1; i < n; i++) {
        const t = i / n;
        const gx = o.x - o.w / 2 + o.w * t, gz = o.z - o.d / 2 + o.d * t;
        const a = project({ x: gx, y: o.y - h, z: o.z - o.d / 2 });
        const b = project({ x: gx, y: o.y - h, z: o.z + o.d / 2 });
        const p = project({ x: o.x - o.w / 2, y: o.y - h, z: gz });
        const q = project({ x: o.x + o.w / 2, y: o.y - h, z: gz });
        ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(p.sx, p.sy); ctx.lineTo(q.sx, q.sy); ctx.stroke();
      }
      ctx.restore();
    }

    // кромка: главный носитель силуэта
    polyPath(ctx, top);
    ctx.strokeStyle = shade(rgb, 1.85, 0.9 * alpha);
    ctx.lineWidth = o.rim ?? 1.6;
    ctx.shadowColor = shade(rgb, 1.9, 1);
    ctx.shadowBlur = o.glow ?? 14;
    ctx.stroke();
    ctx.shadowBlur = 0;
    return { top, bot, centerTop: c };
  }

  /* ---------------------------------------------------------------- коробка */
  function boxCorners(x, y, z, w, h, d) {
    const hw = w / 2, hd = d / 2;
    return [
      { x: x - hw, y, z: z - hd }, { x: x + hw, y, z: z - hd },
      { x: x + hw, y, z: z + hd }, { x: x - hw, y, z: z + hd },
      { x: x - hw, y: y - h, z: z - hd }, { x: x + hw, y: y - h, z: z - hd },
      { x: x + hw, y: y - h, z: z + hd }, { x: x - hw, y: y - h, z: z + hd },
    ];
  }

  // грань: индексы вершин + пара коэффициентов освещённости для градиента
  const BOX_FACES = [
    [[4, 5, 6, 7], 1.15, 0.85],   // верх ловит свет
    [[0, 1, 5, 4], 0.62, 0.4],
    [[1, 2, 6, 5], 0.9, 0.6],
    [[2, 3, 7, 6], 0.62, 0.4],
    [[3, 0, 4, 7], 0.9, 0.6],
    [[0, 3, 2, 1], 0.28, 0.2],    // низ почти не виден
  ];

  /** Коробка: сервис, контейнер, «здание» эндпоинта.
   *
   * `lod` — уровень детализации. Каждая грань с градиентом стоит отдельного
   * объекта CanvasGradient, и на 269 объектах это полторы тысячи градиентов на
   * кадр — самая дорогая часть отрисовки. Для мелких объектов заливаем плоско,
   * для совсем мелких рисуем только силуэт: на экране разницы не видно.
   */
  function box(ctx, project, o) {
    const { x, y, z, w, h, d, rgb } = o;
    const alpha = o.alpha ?? 1;
    const dimK = o.dim ? 0.42 : 1;
    // 0 — плоская заливка без кромки, 1 — плоско с кромкой, 2 — градиенты.
    // Раньше нулевой уровень рисовал только две грани, и при вращении камеры
    // у объектов буквально пропадали стороны — экономия того не стоила.
    const lod = o.lod ?? 2;
    if (lod >= 2 && o.shadow !== false && h > 2) {
      contactShadow(ctx, project, { x, y, z, r: Math.max(w, d) * 0.72, alpha: 0.4 * alpha });
    }
    const pr = boxCorners(x, y, z, w, h, d).map(project);

    BOX_FACES
      .map(([idx, k0, k1]) => ({
        pts: idx.map((i) => pr[i]), k0, k1,
        depth: idx.reduce((a, i) => a + pr[i].depth, 0) / idx.length,
      }))
      .sort((a, b) => b.depth - a.depth)
      .forEach((f) => {
        polyPath(ctx, f.pts);
        ctx.fillStyle = lod >= 2
          ? faceFill(ctx, f.pts, rgb, f.k0 * dimK, f.k1 * dimK, alpha)
          : shade(rgb, ((f.k0 + f.k1) / 2) * dimK, alpha);
        ctx.fill();
      });

    // светящаяся кромка верхней грани; на нулевом уровне её не рисуем —
    // обводка каждого объекта заметна в профиле, а грани уже на месте
    if (lod >= 1) {
      const top = BOX_FACES[0][0].map((i) => pr[i]);
      polyPath(ctx, top);
      ctx.strokeStyle = shade(rgb, o.dim ? 0.9 : 2.0, alpha);
      ctx.lineWidth = 1.2;
      if (o.glow) { ctx.shadowColor = shade(rgb, 1.9, 1); ctx.shadowBlur = o.glow; }
      ctx.stroke();
      ctx.shadowBlur = 0;
    }
    return pr;
  }

  /* --------------------------------------------------------------- цилиндр */
  function cylinder(ctx, project, o) {
    const { x, y, z, r, h, rgb } = o;
    const alpha = o.alpha ?? 1;
    const dimK = o.dim ? 0.45 : 1;
    if ((o.lod ?? 2) >= 2 && o.shadow !== false) {
      contactShadow(ctx, project, { x, y, z, r: r * 1.5, alpha: 0.42 * alpha });
    }
    // сегментов по детализации: 26 колец на объект в четыре пикселя —
    // чистая трата кадра
    const N = o.lod === 0 ? 8 : (o.lod === 1 ? 14 : 26);
    const bottom = [], top = [];
    for (let i = 0; i < N; i++) {
      const a = (i / N) * Math.PI * 2;
      const px = x + Math.cos(a) * r, pz = z + Math.sin(a) * r;
      bottom.push(project({ x: px, y, z: pz }));
      top.push(project({ x: px, y: y - h, z: pz }));
    }
    const segs = [];
    for (let i = 0; i < N; i++) {
      const j = (i + 1) % N;
      const t = i / N;
      segs.push({
        pts: [bottom[i], bottom[j], top[j], top[i]],
        depth: (bottom[i].depth + bottom[j].depth) / 2,
        k: (0.36 + 0.62 * Math.pow(Math.abs(Math.sin(t * Math.PI * 2 + 0.8)), 0.7)) * dimK,
      });
    }
    segs.sort((a, b) => b.depth - a.depth).forEach((s) => {
      polyPath(ctx, s.pts);
      ctx.fillStyle = shade(rgb, s.k, alpha);
      ctx.fill();
    });
    polyPath(ctx, top);
    ctx.fillStyle = (o.lod ?? 2) >= 2
      ? faceFill(ctx, top, rgb, 1.3 * dimK, 0.85 * dimK, alpha)
      : shade(rgb, 1.1 * dimK, alpha);
    ctx.fill();
    ctx.strokeStyle = shade(rgb, o.dim ? 0.9 : 2.0, alpha);
    ctx.lineWidth = 1.3;
    if (o.glow) { ctx.shadowColor = shade(rgb, 1.9, 1); ctx.shadowBlur = o.glow; }
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  /* -------------------------------------------------- стопка пластин (БД) */
  /** Приём с изометрических референсов: хранилище — не монолит, а слои. */
  function stack(ctx, project, o) {
    const n = clamp(o.count ?? 3, 1, 6);
    const h = o.h ?? 7, gap = o.gap ?? 8;
    if (o.shadow !== false) {
      contactShadow(ctx, project, {
        x: o.x, y: o.y, z: o.z, r: Math.max(o.w, o.d) * 0.72, alpha: 0.42 * (o.alpha ?? 1),
      });
    }
    for (let i = n - 1; i >= 0; i--) {
      const grow = 1 - i * 0.06;   // нижние пластины чуть шире — стопка «оседает»
      box(ctx, project, {
        ...o, shadow: false,
        y: o.y - i * (h + gap), h,
        w: o.w * grow, d: o.d * grow,
        alpha: (o.alpha ?? 1) * (i === 0 ? 1 : 0.8),
        glow: i === 0 ? o.glow : 0,
      });
    }
  }

  /* ------------------------------------------------------- бирка с выноской */
  /** Подпись как на инженерных схемах: плашка и тонкая ножка к объекту. */
  /** Габарит бирки без отрисовки — нужен для раскладки без пересечений. */
  function tagBox(ctx, o) {
    const size = o.size || 10.5;
    ctx.font = (o.strong ? '650 ' : '') + size + 'px "Segoe UI", sans-serif';
    const w = ctx.measureText(o.text).width + 16;
    const h = size + 9;
    const lift = o.lift ?? 22;
    return { x: o.x - w / 2, y: o.y - lift - h, w, h };
  }

  function tag(ctx, o) {
    const { x, y, text, color } = o;
    const size = o.size || 10.5;
    const lift = o.lift ?? 22;
    ctx.font = (o.strong ? '650 ' : '') + size + 'px "Segoe UI", sans-serif';
    const w = ctx.measureText(text).width + 16;
    const hgt = size + 9;
    const bx = x - w / 2, by = y - lift - hgt;

    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x, by + hgt);
    ctx.strokeStyle = o.line || 'rgba(255,255,255,.28)';
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.beginPath();
    ctx.roundRect(bx, by, w, hgt, 4);
    ctx.fillStyle = o.bg || 'rgba(10,15,24,.9)';
    ctx.fill();
    ctx.strokeStyle = color ? shade(hexToRgb(color), 1, 0.55) : 'rgba(255,255,255,.14)';
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.fillStyle = color || '#e7ecf3';
    ctx.textAlign = 'center';
    ctx.fillText(text, x, by + hgt - 6);
    ctx.textAlign = 'left';
    return { x: bx, y: by, w, h: hgt };
  }

  const _hex = {};
  function hexToRgb(hex) {
    if (_hex[hex]) return _hex[hex];
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(String(hex).trim());
    const v = m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)]
                : [140, 150, 180];
    return (_hex[hex] = v);
  }

  return { shade, polyPath, faceFill, roundRect, contactShadow,
           pad, box, cylinder, stack, tag, tagBox, hexToRgb, boxCorners };
}());
