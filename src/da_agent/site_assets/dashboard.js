/* 数据看板（M12）：读取页面里内嵌的 JSON（reports/dashboard/dashboard.json），画出全部图表。不依赖第三方库。
   商品名、国家名来自原始数据，当作不可信文本：一律用 textContent / 文本节点写进页面，不拼 HTML。
   每张图都有表格视图，悬停能看到的数字在表格里都能找到。 */
(() => {
  'use strict';
  const DATA = JSON.parse(document.getElementById('dashboard-data').textContent);
  const NS = 'http://www.w3.org/2000/svg';
  const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const state = {period: 'all', market: 'all'};

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
  // 柱状图（单系列或堆叠）：柱宽不超过 24px，顶端 4px 圆角、底部贴基线；堆叠段之间留 2px 背景色缝
  function drawColumns(host, {months, series, format, onTip, title, height = 240}) {
    const W = Math.max(host.clientWidth, 280), H = height, m = {l: 64, r: 8, t: 24, b: 28};
    const pw = W - m.l - m.r, ph = H - m.t - m.b, n = months.length;
    const totals = months.map((_, i) => sum(series.map(sr => Math.max(0, sr.values[i]))));
    const ticks = niceTicks(Math.max(...totals, 1));
    const top = ticks[ticks.length - 1];
    const y = v => m.t + ph - (v / top) * ph;
    const svg = s('svg', {viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': title});
    for (const t of ticks) {
      s('line', {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), class: t ? 'grid' : 'base'}, svg);
      label(svg, m.l - 8, y(t) + 4, format(t), {class: 'tick', 'text-anchor': 'end'});
    }
    const band = pw / n, bw = Math.min(24, band * 0.72);
    const every = [1, 2, 3, 6, 12].find(k => band * k >= 58) || 12;  // 月份标签对齐到 1、4、7、10 月这类整月
    months.forEach((mo, i) => {
      if ((Number(mo.month.slice(5)) - 1) % every === 0) {
        label(svg, m.l + band * i + band / 2, H - 8, mo.month, {class: 'tick', 'text-anchor': 'middle'});
      }
    });
    months.forEach((mo, i) => {
      const g = s('g', {class: 'mark' + (mo.partial ? ' partial' : '')}, svg);
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
      // 未满月只标一个 *：手机上柱子只有十几像素宽，写字会压到旁边的柱子；说明写在图下方
      if (mo.partial) label(svg, x + bw / 2, base - 6, '*', {class: 'val', 'text-anchor': 'middle'});
      const hit = s('rect', {x: m.l + band * i, y: m.t, width: band, height: ph, class: 'hit', tabindex: 0,
                             'aria-label': `${monthName(mo.month)}：${format(totals[i])}`}, g);
      hover(hit, () => onTip(i));
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

  // ---------- 卡片：标题在页面里，这里填结论、图、表 ----------
  function fill(id, {insight, chart, tableView, note, legend}) {
    const card = document.getElementById(id);
    card.querySelector('.insight').textContent = insight || '';
    const chartHost = card.querySelector('.chart-host'), tableHost = card.querySelector('.table-host');
    chart(chartHost);
    const extras = card.querySelector('.extras');
    extras.replaceChildren(...(legend ? [legend] : []));
    tableHost.replaceChildren(tableView ? table(tableView.columns, tableView.rows) : '');
    card.querySelector('.note').textContent = note || '';
  }

  function empty(message) {
    return host => host.replaceChildren(h('p', {class: 'empty'}, message));
  }

  function legendOf(items) {  // items: [{name, color}]
    return h('div', {class: 'legend'}, items.map(it => h('span', {}, h('i', {style: `background:${it.color}`}), it.name)));
  }

  // ---------- 各部分 ----------
  const current = () => DATA.slices[`${state.period}/${state.market}`];
  const periodInfo = () => DATA.periods.find(p => p.id === state.period);
  const marketInfo = () => DATA.markets.find(mk => mk.id === state.market);

  const KPIS = [
    {key: 'gmv', label: 'GMV（下单口径）', fmt: moneyShort, full: money, hero: true},
    {key: 'net_sales', label: '净销售额', fmt: moneyShort, full: money, help: 'GMV − 取消金额'},
    {key: 'orders', label: '订单数', fmt: num},
    {key: 'aov', label: '客单价', fmt: money},
    {key: 'active_customers', label: '活跃客户数', fmt: num, help: '不含缺少客户 ID 的订单'},
    {key: 'repeat_rate', label: '复购客户占比', fmt: v => pct(v), rate: true, help: '下单 ≥ 2 次的客户 ÷ 活跃客户'},
    {key: 'cancel_rate', label: '取消率', fmt: v => pct(v, 2), rate: true, upIsBad: true, help: '取消金额 ÷ GMV'},
  ];

  function delta(def, cur, prev, compareLabel) {
    if (prev == null || cur == null || (!def.rate && !prev)) return null;
    const change = def.rate ? (cur - prev) * 100 : (cur - prev) / prev;
    const up = change > 0, good = def.upIsBad ? !up : up;
    const text = def.rate ? `${Math.abs(change).toFixed(2)} 个百分点` : pct(Math.abs(change));
    return h('div', {class: 'delta'},
      h('b', {class: `${up ? 'up' : 'down'}-${good ? 'good' : 'bad'}`}, `${up ? '▲' : '▼'} ${text}`), ` ${compareLabel}`);
  }

  function renderKpis(slice) {
    const {kpi, compare} = slice, compareLabel = periodInfo().compare_label;
    const complete = slice.months.filter(mo => !mo.partial);
    const host = document.getElementById('kpis');
    host.replaceChildren(...KPIS.map(def => {
      const value = kpi[def.key];
      return h('div', {class: 'kpi' + (def.hero ? ' hero' : '')},
        h('div', {class: 'label'}, def.label),
        h('div', {class: 'value', title: def.full && value != null ? def.full(value) : null}, value == null ? '—' : def.fmt(value)),
        compare ? delta(def, value, compare[def.key], compareLabel) : null,
        def.help ? h('div', {class: 'note'}, def.help) : null,
        def.hero && complete.length > 1
          ? h('div', {class: 'spark-host'}, h('div', {class: 'note'}, `每月 GMV，${complete[0].month} 至 ${complete[complete.length - 1].month}（不含未满月）`))
          : null);
    }));
    const sparkHost = host.querySelector('.spark-host');
    if (sparkHost) sparkHost.prepend(sparkline(complete.map(mo => mo.gmv), Math.max(sparkHost.clientWidth, 120)));
  }

  const partialNote = months => months.filter(mo => mo.partial)
    .map(mo => `* ${mo.month} 只有 ${mo.trading_days} 个交易日，未满月。`).join('');

  function renderMonthly(slice) {
    const months = slice.months;
    const complete = months.filter(mo => !mo.partial && mo.gmv > 0);
    const peak = complete.reduce((a, b) => (b.gmv > a.gmv ? b : a), complete[0] || months[0]);
    const partial = months.filter(mo => mo.partial);
    fill('card-monthly', {
      insight: peak ? `最高的是 ${monthName(peak.month)}：${moneyShort(peak.gmv)}，${num(peak.orders)} 单。` +
        (partial.length ? `${monthName(partial[0].month)}只有 ${partial[0].trading_days} 个交易日（数据截止），未满月，不能和其他月份直接比。` : '')
        : '',
      chart: host => drawColumns(host, {
        months, title: '每月 GMV', format: moneyShort,
        series: [{name: 'GMV', color: token('--series-1'), values: months.map(mo => mo.gmv)}],
        onTip: i => [monthName(months[i].month) + (months[i].partial ? '（未满月）' : ''), [
          {value: money(months[i].gmv), label: 'GMV'}, {value: money(months[i].net_sales), label: '净销售额'},
          {value: num(months[i].orders), label: '订单'}, {value: num(months[i].customers), label: '活跃客户'},
          {value: num(months[i].trading_days), label: '交易天数'}]],
      }),
      tableView: {
        columns: [{label: '月份'}, {label: 'GMV', num: true}, {label: '净销售额', num: true}, {label: '订单数', num: true},
                  {label: '活跃客户', num: true}, {label: '交易天数', num: true}, {label: '备注'}],
        rows: months.map(mo => [mo.month, money(mo.gmv), money(mo.net_sales), num(mo.orders), num(mo.customers),
                                num(mo.trading_days), mo.partial ? '未满月' : '']),
      },
      note: 'GMV 按下单口径，含之后被取消的订单；净销售额 = GMV − 取消金额。' + partialNote(months),
    });
  }

  function renderNewReturning(slice) {
    const months = slice.months;
    const colors = ['--series-1', '--series-2', '--series-3'].map(token);
    const series = [
      {name: '老客', color: colors[0], values: months.map(mo => mo.returning_gmv)},
      {name: '新客', color: colors[1], values: months.map(mo => mo.new_gmv)},
      {name: '缺客户 ID', color: colors[2], values: months.map(mo => mo.unidentified_gmv)},
    ];
    const total = sum(months.map(mo => mo.gmv));
    const shares = series.map(sr => sum(sr.values) / (total || 1));
    const censored = months.length && months[0].month === DATA.data_start.slice(0, 7);
    fill('card-customers', {
      insight: total ? `老客贡献 ${pct(shares[0])} 的 GMV，新客 ${pct(shares[1])}，无法识别客户的订单 ${pct(shares[2])}。` : '',
      chart: host => drawColumns(host, {
        months, series, title: '每月 GMV 按新老客拆分', format: moneyShort,
        onTip: i => [monthName(months[i].month), series.map(sr => ({color: sr.color, value: money(sr.values[i]),
                                                                  label: sr.name}))],
      }),
      legend: legendOf(series),
      tableView: {
        columns: [{label: '月份'}, ...series.map(sr => ({label: sr.name, num: true})), {label: '合计', num: true}],
        rows: months.map((mo, i) => [mo.month, ...series.map(sr => money(sr.values[i])), money(mo.gmv)]),
      },
      note: '新客：第一次购买落在当月的客户（看全部历史）。' + partialNote(months) +
            (censored ? '数据从 2009-12 开始，那个月的客户都算作新客，其中有不少其实是老客。' : ''),
    });
  }

  function renderHours(slice) {
    const counts = slice.heatmap, hours = DATA.hours;
    const max = Math.max(...counts.flat(), 1);
    let peak = [0, 0];
    counts.forEach((row, i) => row.forEach((v, j) => { if (v > counts[peak[0]][peak[1]]) peak = [i, j]; }));
    const byDay = counts.map(sum), total = sum(byDay);
    const quietest = byDay.indexOf(Math.min(...byDay));
    fill('card-hours', {
      insight: total ? `最忙的是${WEEKDAYS[peak[0]]} ${hours[peak[1]]}:00–${hours[peak[1]] + 1}:00（${num(counts[peak[0]][peak[1]])} 单）；` +
        `${WEEKDAYS[quietest]}最少，全期只有 ${num(byDay[quietest])} 单。` : '',
      chart: total ? host => drawHeatmap(host, {
        rowLabels: WEEKDAYS, colLabels: hours.map(String), colEvery: hours.length > 12 ? 2 : 1, values: counts, max,
        title: '星期 × 小时的订单数', cellH: 32,
        onTip: (i, j) => [`${WEEKDAYS[i]} ${hours[j]}:00–${hours[j] + 1}:00`, [{value: num(counts[i][j]), label: '单'}]],
      }) : empty('这个范围里没有订单。'),
      legend: total ? scaleLegend('0 单', `${num(max)} 单`) : null,
      tableView: {
        columns: [{label: '星期'}, ...hours.map(hr => ({label: `${hr} 时`, num: true})), {label: '合计', num: true}],
        rows: counts.map((row, i) => [WEEKDAYS[i], ...row.map(num), num(byDay[i])]),
      },
      note: '按下单时间（数据里的时间，英国时间），每张订单算一次。',
    });
  }

  function renderCountries(slice) {
    const c = slice.countries, gmv = slice.kpi.gmv;
    const items = c.top.map(t => ({
      label: t.label, value: t.gmv, valueText: moneyShort(t.gmv), tipTitle: `${t.label}（${t.names.join('、')}）`,
      tip: [{value: money(t.gmv), label: 'GMV'}, {value: num(t.customers), label: '个客户'},
            {value: num(t.orders), label: '单'}, {value: moneyShort(t.gmv / Math.max(t.customers, 1)), label: '每个客户'}],
    }));
    if (c.other_count) {
      items.push({label: `其他 ${c.other_count} 个`, value: c.other_gmv, valueText: moneyShort(c.other_gmv), muted: true,
                  tip: [{value: money(c.other_gmv), label: 'GMV'}]});
    }
    const lead = c.top[0];
    const ukText = c.uk_share != null && lead ? `英国占 ${pct(c.uk_share)}，放在一起其他国家都看不见，所以这里只比海外。` : '';
    fill('card-countries', {
      insight: lead ? ukText + `海外第一是${lead.label}：${num(lead.customers)} 个客户贡献 ${moneyShort(lead.gmv)}。` : ukText,
      chart: items.length ? host => drawBars(host, items) : empty('当前只看英国市场，没有海外国家可以比较。'),
      tableView: {
        columns: [{label: '国家'}, {label: '数据里的写法'}, {label: 'GMV', num: true}, {label: '占本范围 GMV', num: true},
                  {label: '客户数', num: true}, {label: '订单数', num: true}],
        rows: c.top.map(t => [t.label, t.names.join('、'), money(t.gmv), pct(t.gmv / gmv), num(t.customers), num(t.orders)])
          .concat(c.other_count ? [[`其他 ${c.other_count} 个`, '', money(c.other_gmv), pct(c.other_gmv / gmv), '', '']] : []),
      },
      note: '客户数不含缺少客户 ID 的订单。“国家不明确”是数据里的 Unspecified 和 European Community。',
    });
  }

  function renderProducts(slice) {
    const p = slice.products;
    const items = p.top.map(t => ({
      label: t.description || t.stock_code, value: Math.max(t.net_sales, 0), valueText: moneyShort(t.net_sales),
      tipTitle: `${t.description}（${t.stock_code}）`,
      tip: [{value: money(t.net_sales), label: '净销售额'}, {value: money(t.gmv), label: 'GMV'},
            {value: money(t.cancelled), label: '取消'}],
    }));
    const dropped = p.dropped_by_cancellation[0];
    fill('card-products', {
      insight: p.top.length ? `第一名是 ${p.top[0].description}，净销售额 ${moneyShort(p.top[0].net_sales)}。` : '',
      chart: items.length ? host => drawBars(host, items) : empty('这个范围里没有商品销售。'),
      tableView: {
        columns: [{label: '编码'}, {label: '商品'}, {label: '净销售额', num: true}, {label: 'GMV', num: true},
                  {label: '取消', num: true}],
        rows: p.top.map(t => [t.stock_code, t.description, money(t.net_sales), money(t.gmv), money(t.cancelled)]),
      },
      note: '按净销售额（GMV − 取消）排名。' + (dropped
        ? `按下单口径，${dropped.description} 会进前 10（GMV ${moneyShort(dropped.gmv)}），但其中 ${moneyShort(dropped.cancelled)} 被取消了。`
        : ''),
    });
  }

  function renderPareto(slice) {
    const p = slice.products;
    fill('card-pareto', {
      insight: p.count ? `${num(p.count)} 个商品里，排名前 20% 的贡献了 ${pct(p.top20_share)} 的净销售额。` : '',
      chart: p.count ? host => drawPareto(host, p.pareto, p.count, '商品集中度曲线') : empty('这个范围里没有商品销售。'),
      tableView: {
        columns: [{label: '排名前'}, {label: '商品数', num: true}, {label: '累计净销售额占比', num: true}],
        rows: [0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100].map(k => [`${k}%`, num(Math.floor(p.count * k / 100)),
                                                                         pct(p.pareto[k])]),
      },
      note: '只统计净销售额为正的商品。',
    });
  }

  function renderCohorts(slice) {
    const cohorts = slice.cohorts.filter(c => c.retention.length);  // 范围里最后一个月的新客还看不到之后的月份
    const width = Math.max(0, ...cohorts.map(c => c.retention.length));
    const values = cohorts.map(c => Array.from({length: width}, (_, k) => c.retention[k] ?? null));
    const firstMonth = DATA.data_start.slice(0, 7);
    const fair = cohorts.filter(c => c.cohort !== firstMonth && c.retention.length);
    const m1 = sum(fair.map(c => c.retention[0] * c.size)) / (sum(fair.map(c => c.size)) || 1);
    const max = Math.max(0, ...fair.flatMap(c => c.retention.filter(v => v != null)));
    fill('card-cohorts', {
      insight: fair.length ? `新客第二个月还会回来买的平均占 ${pct(m1)}（按队列人数加权` +
        (fair.length < cohorts.length ? `，不含 ${firstMonth} 队列）。` : '）。') : '',
      chart: width ? host => {
        const wrap = h('div', {class: 'scroll-x'});
        host.replaceChildren(wrap);
        drawHeatmap(wrap, {
          rowLabels: cohorts.map(c => c.cohort + (c.cohort === firstMonth ? '*' : '')),
          colLabels: Array.from({length: width}, (_, k) => `${k + 1}`), colEvery: width > 12 ? 2 : 1,
          values, max, title: '新客留存', minWidth: 520, cellH: 20, labelW: 64,
          onTip: (i, j) => [`${monthName(cohorts[i].cohort)}的新客（${num(cohorts[i].size)} 人）`,
                            [{value: pct(values[i][j]), label: `第 ${j + 1} 个月还在买`}]],
        });
      } : empty('这个范围太短，看不到留存。'),
      legend: width ? scaleLegend('0%', pct(max) + (cohorts.some(c => c.cohort === firstMonth) ? '（* 行可能超出）' : '')) : null,
      tableView: {
        columns: [{label: '首购月'}, {label: '人数', num: true}, ...Array.from({length: width}, (_, k) => ({label: `第 ${k + 1} 月`, num: true}))],
        rows: cohorts.map((c, i) => [c.cohort, num(c.size), ...values[i].map(v => (v == null ? '' : pct(v)))]),
      },
      note: '行是首次购买的月份，列是之后第几个月；格子是那个月还在买的客户占比，只看筛选范围内的月份。' +
            (cohorts.some(c => c.cohort === firstMonth) ? `* ${firstMonth} 是数据的第一个月，包含之前就在买的老客，留存偏高。` : ''),
    });
  }

  function renderCleaning(slice) {
    const c = slice.cleaning;
    const items = c.rules.map(r => ({
      label: `${r.id} ${r.name}`, value: r.rows, valueText: num(r.rows), muted: !r.excludes,
      tip: [{value: num(r.rows), label: '行'}, {value: r.action, label: ''}],
    }));
    fill('card-cleaning', {
      insight: `原始 ${num(c.raw_rows)} 行，清洗后得到 ${num(c.sales_rows)} 行商品销售记录。`,
      chart: host => drawBars(host, items),
      legend: legendOf([{name: '删除', color: token('--series-1')}, {name: '保留，单独统计或标记', color: token('--muted')}]),
      tableView: {
        columns: [{label: '规则'}, {label: '处理方式'}, {label: '命中行数', num: true}],
        rows: c.rules.map(r => [`${r.id} ${r.name}`, r.action, num(r.rows)]),
      },
      note: '一行可能命中多条规则。完整的清洗说明见数据质量报告。',
    });
  }

  function render() {
    const slice = current();
    document.getElementById('period-note').textContent = `${periodInfo().label}：${periodInfo().note} · ${marketInfo().label}`;
    renderKpis(slice);
    for (const draw of [renderMonthly, renderNewReturning, renderHours, renderCountries, renderProducts, renderPareto,
                        renderCohorts, renderCleaning]) {
      draw(slice);
    }
  }

  // ---------- 筛选、视图切换、主题和尺寸变化 ----------
  function readHash() {
    const params = new URLSearchParams(location.hash.slice(1));
    if (DATA.periods.some(p => p.id === params.get('period'))) state.period = params.get('period');
    if (DATA.markets.some(mk => mk.id === params.get('market'))) state.market = params.get('market');
    for (const [name, value] of Object.entries(state)) {
      const input = document.querySelector(`input[name="${name}"][value="${value}"]`);
      if (input) input.checked = true;
    }
  }
  document.querySelectorAll('.filters input').forEach(input => input.addEventListener('change', () => {
    state[input.name] = input.value;
    history.replaceState(null, '', `#period=${state.period}&market=${state.market}`);
    hideTip();
    render();
  }));
  document.querySelectorAll('.view-toggle').forEach(button => button.addEventListener('click', () => {
    const card = button.closest('.dash-card'), showTable = button.getAttribute('aria-pressed') !== 'true';
    button.setAttribute('aria-pressed', String(showTable));
    button.textContent = showTable ? '看图' : '看表格';
    card.querySelector('.chart-host').hidden = showTable;
    card.querySelector('.extras').hidden = showTable;
    card.querySelector('.table-host').hidden = !showTable;
  }));
  let timer;
  const later = () => { clearTimeout(timer); timer = setTimeout(render, 120); };
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', later);
  new MutationObserver(later).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
  let lastWidth = 0;
  new ResizeObserver(entries => {
    const w = Math.round(entries[0].contentRect.width);
    if (w !== lastWidth) { lastWidth = w; later(); }
  }).observe(document.querySelector('main'));
  window.addEventListener('hashchange', () => { readHash(); render(); });
  readHash();
  render();
})();
