/* 看板图表组件：M12 手工看板和 M13 Agent 生成的看板共用。不依赖第三方库，图直接画成 SVG。
   规范（dataviz 方法）：柱宽不超过 24px、数据端 4px 圆角、2px 背景色缝、2px 折线、≥8px 端点、发丝细网格；
   每张图都能悬停（也能用 Tab 键聚焦）看数，并有表格视图。来自数据的文字一律用文本节点写进页面，不拼 HTML。 */
window.DashCharts = (() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';

  // ---------- 小工具 ----------
  const h = (tag, attrs = {}, ...children) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style') el.style.cssText = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  };
  const s = (tag, attrs, parent) => {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
    if (parent) parent.append(el);
    return el;
  };
  const label = (parent, x, y, content, attrs = {}) => {
    const t = s('text', {x, y, ...attrs}, parent);
    t.textContent = content;
    return t;
  };
  const token = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const num = v => Number(v).toLocaleString('en-US');
  const money = v => '£' + Number(v).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const moneyShort = v => {
    const a = Math.abs(v);
    if (a >= 1e8) return '£' + (v / 1e8).toLocaleString('en-US', {maximumFractionDigits: 2}) + '亿';
    if (a >= 1e4) return '£' + (v / 1e4).toLocaleString('en-US', {maximumFractionDigits: 1}) + '万';
    return '£' + Math.round(v).toLocaleString('en-US');
  };
  const pct = (r, d = 1) => (r == null ? '—' : (r * 100).toFixed(d) + '%');
  const monthName = m => `${m.slice(0, 4)} 年 ${Number(m.slice(5))} 月`;
  const sum = xs => xs.reduce((a, b) => a + b, 0);

  // 坐标刻度取整：1、2、2.5、5 × 10^k
  function niceTicks(max, count = 4) {
    const raw = max / count;
    const mag = 10 ** Math.floor(Math.log10(raw || 1));
    const step = [1, 2, 2.5, 5, 10].map(f => f * mag).find(st => st >= raw);
    const ticks = [];
    for (let t = 0; t < max + step * 0.999; t += step) ticks.push(t);
    return ticks.length > 1 ? ticks : [0, step];
  }

  // 顺序色阶：7 个色阶之间线性插值。深色模式的色阶在 CSS 里已经反过来（数值小的接近背景）
  function seqColor(t) {
    const steps = [0, 1, 2, 3, 4, 5, 6].map(i => token(`--seq-${i}`));
    const x = Math.min(Math.max(t, 0), 1) * 6, i = Math.min(Math.floor(x), 5), f = x - i;
    const rgb = hex => [1, 3, 5].map(k => parseInt(hex.slice(k, k + 2), 16));
    const [a, b] = [rgb(steps[i]), rgb(steps[i + 1])];
    return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(',')})`;
  }

  // ---------- 悬停提示：数值在前（加粗），名称在后；系列用短线标识 ----------
  const tip = h('div', {class: 'tip dash-tip', role: 'status'});
  tip.hidden = true;
  document.body.append(tip);
  function showTip([title, rows], x, y) {
    tip.replaceChildren(h('div', {class: 'tip-title'}, title), ...rows.map(r => h('div', {class: 'tip-row'},
      r.color ? h('i', {class: 'key', style: `background:${r.color}`}) : null,
      h('strong', {}, r.value), h('span', {}, r.label))));
    tip.hidden = false;
    const b = tip.getBoundingClientRect();
    tip.style.left = Math.min(Math.max(8, x + 14), innerWidth - b.width - 8) + 'px';
    tip.style.top = (y - b.height - 14 < 8 ? y + 18 : y - b.height - 14) + 'px';
  }
  const hideTip = () => { tip.hidden = true; };
  function hover(el, getTip) {
    el.addEventListener('pointermove', e => showTip(getTip(e), e.clientX, e.clientY));
    el.addEventListener('pointerleave', hideTip);
    el.addEventListener('focus', () => {
      const b = el.getBoundingClientRect();
      showTip(getTip(null), b.left + b.width / 2, b.top);
    });
    el.addEventListener('blur', hideTip);
  }

  // ---------- 图表 ----------
  // 柱状图（单系列或堆叠）：柱宽不超过 24px，顶端 4px 圆角、底部贴基线；堆叠段之间留 2px 背景色缝。
  // points：[{key, partial}]；key 是 YYYY-MM 时，横轴标签对齐到 1、4、7、10 月这类整月，否则每隔几个标一个
  function drawColumns(host, {points, series, format, onTip, title, name = k => k, height = 240}) {
    const W = Math.max(host.clientWidth, 280), H = height, m = {l: 64, r: 8, t: 24, b: 28};
    const pw = W - m.l - m.r, ph = H - m.t - m.b, n = points.length;
    const totals = points.map((_, i) => sum(series.map(sr => Math.max(0, sr.values[i] || 0))));
    const ticks = niceTicks(Math.max(...totals, 1));
    const top = ticks[ticks.length - 1];
    const y = v => m.t + ph - (v / top) * ph;
    const svg = s('svg', {viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': title});
    for (const t of ticks) {
      s('line', {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), class: t ? 'grid' : 'base'}, svg);
      label(svg, m.l - 8, y(t) + 4, format(t), {class: 'tick', 'text-anchor': 'end'});
    }
    const band = pw / n, bw = Math.min(24, band * 0.72);
    xLabels(svg, points.map(p => p.key), i => m.l + band * i + band / 2, H - 8, band);
    points.forEach((p, i) => {
      const g = s('g', {class: 'mark' + (p.partial ? ' partial' : '')}, svg);
      const x = m.l + band * i + (band - bw) / 2;
      const shown = series.filter(sr => sr.values[i] > 0);
      let base = y(0);
      for (const sr of shown) {
        const top_ = base - (sr.values[i] / top) * ph;
        const bottom = sr === shown[0] ? base : base - 2;
        if (bottom - top_ > 0.5) {
          const r = sr === shown[shown.length - 1] ? Math.min(4, (bottom - top_) / 2, bw / 2) : 0;
          s('path', {d: `M${x},${bottom}V${top_ + r}Q${x},${top_} ${x + r},${top_}H${x + bw - r}` +
                       `Q${x + bw},${top_} ${x + bw},${top_ + r}V${bottom}Z`, fill: sr.color}, g);
        }
        base = top_;
      }
      // 不完整的周期只标一个 *：手机上柱子只有十几像素宽，写字会压到旁边的柱子；说明写在图下方
      if (p.partial) label(svg, x + bw / 2, base - 6, '*', {class: 'val', 'text-anchor': 'middle'});
      const hit = s('rect', {x: m.l + band * i, y: m.t, width: band, height: ph, class: 'hit', tabindex: 0,
                             'aria-label': `${name(p.key)}：${format(totals[i])}`}, g);
      hover(hit, () => onTip(i));
    });
    host.replaceChildren(svg);
  }

  // 横轴标签：YYYY-MM 对齐整月；其他每隔几个标一个，保证标签之间至少 58px
  function xLabels(svg, keys, xAt, y, band) {
    const monthly = keys.every(k => /^\d{4}-\d{2}$/.test(k));
    const every = [1, 2, 3, 6, 12].find(k => band * k >= 58) || Math.ceil(58 / band);
    keys.forEach((k, i) => {
      const show = monthly ? (Number(k.slice(5)) - 1) % every === 0 : i % every === 0;
      if (show) label(svg, xAt(i), y, k, {class: 'tick', 'text-anchor': 'middle'});
    });
  }

  // 折线（可多条）：2px 线、round 连接；竖线跟随指针找到最近的横轴位置，提示里列出每条线在这一点的值
  function drawLines(host, {points, series, format, onTip, title, name = k => k, height = 240}) {
    const W = Math.max(host.clientWidth, 280), H = height, m = {l: 64, r: 16, t: 16, b: 28};
    const pw = W - m.l - m.r, ph = H - m.t - m.b, n = points.length;
    // 纵轴从 0 起（有负数时包含负数）：按整齐的步长向外取整，0 一定是其中一条刻度线
    const all = series.flatMap(sr => sr.values).filter(v => v != null);
    const lo = Math.min(0, ...all), hi = Math.max(0, ...all);
    const tickStep = niceTicks(hi - lo || 1)[1];
    const bottom = Math.floor(lo / tickStep) * tickStep, top = Math.ceil(hi / tickStep) * tickStep || tickStep;
    const ticks = [];
    for (let t = bottom; t <= top + tickStep * 1e-9; t += tickStep) ticks.push(Math.abs(t) < tickStep * 1e-9 ? 0 : t);
    const x = i => m.l + (n > 1 ? (i / (n - 1)) * pw : pw / 2);
    const y = v => m.t + ph - ((v - bottom) / (top - bottom || 1)) * ph;
    const svg = s('svg', {viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': title});
    for (const t of ticks) {
      s('line', {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), class: t === 0 ? 'base' : 'grid'}, svg);
      label(svg, m.l - 8, y(t) + 4, format(t), {class: 'tick', 'text-anchor': 'end'});
    }
    xLabels(svg, points.map(p => p.key), x, H - 8, pw / Math.max(n - 1, 1));
    for (const sr of series) {
      let d = '';
      sr.values.forEach((v, i) => { if (v != null) d += `${d && sr.values[i - 1] != null ? 'L' : 'M'}${x(i)},${y(v)}`; });
      s('path', {d, fill: 'none', stroke: sr.color, 'stroke-width': 2, 'stroke-linejoin': 'round',
                 'stroke-linecap': 'round'}, svg);
      const last = sr.values.map((v, i) => [v, i]).filter(([v]) => v != null).pop();
      if (last) s('circle', {cx: x(last[1]), cy: y(last[0]), r: 4, fill: sr.color, stroke: token('--surface'),
                             'stroke-width': 2}, svg);
    }
    points.forEach((p, i) => {
      if (p.partial) label(svg, x(i), m.t + 10, '*', {class: 'val', 'text-anchor': 'middle'});
    });
    const cross = s('line', {y1: m.t, y2: m.t + ph, class: 'cross', visibility: 'hidden'}, svg);
    const step = n > 1 ? pw / (n - 1) : pw;
    points.forEach((p, i) => {
      const hit = s('rect', {x: x(i) - step / 2, y: m.t, width: step, height: ph, class: 'hit', tabindex: 0,
                             'aria-label': `${name(p.key)}：` + series.map(sr => `${sr.name} ${sr.values[i] == null ? '—' : format(sr.values[i])}`).join('，')}, svg);
      hover(hit, () => {
        cross.setAttribute('x1', x(i));
        cross.setAttribute('x2', x(i));
        cross.setAttribute('visibility', 'visible');
        return onTip(i);
      });
      hit.addEventListener('pointerleave', () => cross.setAttribute('visibility', 'hidden'));
    });
    host.replaceChildren(svg);
  }

  // 横向条形（HTML）：数据端 4px 圆角，数值在右侧一列；长名字截断，完整名字在悬停提示和表格里
  function drawBars(host, items) {
    const max = Math.max(...items.map(it => it.value), 1);
    host.replaceChildren(h('div', {class: 'hbars', role: 'list'}, items.map(it => {
      const row = h('div', {class: 'hbar' + (it.muted ? ' muted' : ''), role: 'listitem', tabindex: 0,
                            'aria-label': `${it.label}：${it.valueText}`},
        h('span', {class: 'hbar-label'}, it.label),
        h('span', {class: 'hbar-track'}, h('span', {class: 'hbar-fill', style: `width:${(it.value / max) * 100}%`})),
        h('span', {class: 'hbar-value'}, it.valueText));
      hover(row, () => [it.tipTitle || it.label, it.tip]);
      return row;
    })));
  }

  // 热力图：格子之间 2px 背景色缝；悬停格子描边
  function drawHeatmap(host, {rowLabels, colLabels, colEvery = 1, values, max, onTip, title, minWidth = 0,
                              cellH = 24, labelW = 44}) {
    const W = Math.max(host.clientWidth, minWidth), cols = colLabels.length, m = {l: labelW, r: 2, t: 18, b: 2};
    const cw = (W - m.l - m.r) / cols, H = m.t + rowLabels.length * cellH + m.b;
    const svg = s('svg', {viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': title,
                          style: minWidth ? `min-width:${minWidth}px` : null});
    colLabels.forEach((c, j) => {
      if (j % colEvery === 0) label(svg, m.l + cw * j + cw / 2, 12, c, {class: 'tick', 'text-anchor': 'middle'});
    });
    rowLabels.forEach((r, i) => label(svg, m.l - 6, m.t + cellH * i + cellH / 2 + 4, r,
                                      {class: 'tick', 'text-anchor': 'end'}));
    values.forEach((row, i) => row.forEach((v, j) => {
      if (v == null) return;
      const cell = s('rect', {x: m.l + cw * j + 1, y: m.t + cellH * i + 1, width: Math.max(cw - 2, 1),
                              height: cellH - 2, rx: 2, fill: seqColor(max ? v / max : 0), class: 'cell'}, svg);
      hover(cell, () => onTip(i, j));
    }));
    host.replaceChildren(svg);
  }

  function scaleLegend(minText, maxText) {
    const stops = [0, 1, 2, 3, 4, 5, 6].map(i => token(`--seq-${i}`)).join(',');
    return h('div', {class: 'scale'}, minText, h('span', {class: 'ramp', style: `background:linear-gradient(90deg,${stops})`}),
             maxText);
  }

  // 集中度曲线：横轴是商品排名前 x%，纵轴是这些商品贡献的净销售额占比；竖线跟随指针，方向键也能移动
  function drawPareto(host, pareto, count, title) {
    const W = Math.max(host.clientWidth, 280), H = 260, m = {l: 44, r: 16, t: 14, b: 44};
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const x = p => m.l + (p / 100) * pw, y = v => m.t + ph - v * ph;
    const svg = s('svg', {viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': title});
    for (const v of [0, 0.25, 0.5, 0.75, 1]) {
      s('line', {x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: v ? 'grid' : 'base'}, svg);
      label(svg, m.l - 8, y(v) + 4, pct(v, 0), {class: 'tick', 'text-anchor': 'end'});
    }
    for (const p of [0, 20, 40, 60, 80, 100]) label(svg, x(p), m.t + ph + 18, p + '%', {class: 'tick', 'text-anchor': 'middle'});
    label(svg, m.l + pw / 2, H - 4, '商品数量占比（按净销售额从高到低）', {class: 'tick', 'text-anchor': 'middle'});
    s('line', {x1: x(0), y1: y(0), x2: x(100), y2: y(1), class: 'ref'}, svg);
    label(svg, x(62), y(0.62) + 16, '如果每个商品卖得一样多', {class: 'tick', 'text-anchor': 'middle'});
    const pts = pareto.map((v, p) => `${x(p).toFixed(1)},${y(v).toFixed(1)}`);
    const color = token('--series-1');
    s('path', {d: `M${x(0)},${y(0)}L${pts.join('L')}L${x(100)},${y(0)}Z`, fill: color, 'fill-opacity': 0.1}, svg);
    s('polyline', {points: pts.join(' '), fill: 'none', stroke: color, 'stroke-width': 2,
                   'stroke-linejoin': 'round', 'stroke-linecap': 'round'}, svg);
    s('circle', {cx: x(20), cy: y(pareto[20]), r: 4, fill: color, stroke: token('--surface'), 'stroke-width': 2}, svg);
    label(svg, x(20) + 10, y(pareto[20]) + 16, `前 20% 的商品贡献 ${pct(pareto[20])}`, {class: 'ann'});
    const cross = s('line', {y1: m.t, y2: m.t + ph, class: 'cross', visibility: 'hidden'}, svg);
    const dot = s('circle', {r: 4, fill: color, stroke: token('--surface'), 'stroke-width': 2, visibility: 'hidden'}, svg);
    const overlay = s('rect', {x: m.l, y: m.t, width: pw, height: ph, class: 'hit', tabindex: 0,
                               'aria-label': `${title}；用左右方向键移动`}, svg);
    let at = 20;
    const place = p => {
      at = Math.min(100, Math.max(0, p));
      for (const [el, k, v] of [[cross, 'x1', x(at)], [cross, 'x2', x(at)], [dot, 'cx', x(at)], [dot, 'cy', y(pareto[at])]]) {
        el.setAttribute(k, v);
      }
      cross.setAttribute('visibility', 'visible');
      dot.setAttribute('visibility', 'visible');
    };
    const tipFor = () => [`排名前 ${at}% 的商品（${num(Math.floor(count * at / 100))} 个）`,
                          [{value: pct(pareto[at]), label: '的净销售额'}]];
    hover(overlay, e => {
      if (e) place(Math.round(((e.clientX - svg.getBoundingClientRect().left) * (W / svg.getBoundingClientRect().width) - m.l) / pw * 100));
      else place(at);
      return tipFor();
    });
    overlay.addEventListener('keydown', e => {
      const step = {ArrowLeft: -1, ArrowRight: 1}[e.key];
      if (!step) return;
      e.preventDefault();
      place(at + step);
      const b = overlay.getBoundingClientRect();
      showTip(tipFor(), b.left + (at / 100) * b.width, b.top);
    });
    overlay.addEventListener('pointerleave', () => {
      cross.setAttribute('visibility', 'hidden');
      dot.setAttribute('visibility', 'hidden');
    });
    host.replaceChildren(svg);
  }

  function sparkline(values, W) {
    const H = 48, pad = 5, max = Math.max(...values), min = Math.min(...values);
    const x = i => pad + (i / Math.max(values.length - 1, 1)) * (W - pad * 2);
    const y = v => H - pad - ((v - min) / (max - min || 1)) * (H - pad * 2);
    const svg = s('svg', {viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: 'spark', 'aria-hidden': 'true'});
    s('polyline', {points: values.map((v, i) => `${x(i)},${y(v)}`).join(' '), fill: 'none', stroke: token('--muted'),
                   'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}, svg);
    const last = values.length - 1;
    s('circle', {cx: x(last), cy: y(values[last]), r: 4, fill: token('--series-1'), stroke: token('--surface'),
                 'stroke-width': 2}, svg);
    return svg;
  }

  function table(columns, rows) {
    return h('table', {}, h('thead', {}, h('tr', {}, columns.map(c => h('th', {class: c.num ? 'num' : null}, c.label)))),
             h('tbody', {}, rows.map(r => h('tr', {}, r.map((v, i) => h('td', {class: columns[i].num ? 'num' : null}, v))))));
  }

  function empty(message) {
    return host => host.replaceChildren(h('p', {class: 'empty'}, message));
  }

  function legendOf(items) {  // items: [{name, color}]
    return h('div', {class: 'legend'}, items.map(it => h('span', {}, h('i', {style: `background:${it.color}`}), it.name)));
  }


  return {h, s, label, token, num, money, moneyShort, pct, monthName, sum, niceTicks, seqColor, showTip, hideTip, hover, drawColumns, drawLines, drawBars, drawHeatmap, scaleLegend, drawPareto, sparkline, table, empty, legendOf};
})();
