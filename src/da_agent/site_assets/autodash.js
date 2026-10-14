/* Agent 生成的看板（M13）：读取页面里内嵌的编译结果，按方案画出指标卡和图。图表组件和 M12 手工看板共用（charts.js）。
   标题、结论、取值都来自模型或数据，当作不可信文本：一律用文本节点写进页面。 */
(() => {
  'use strict';
  const DATA = JSON.parse(document.getElementById('dashboard-data').textContent);
  const {h, num, pct, token, sum, showTip, hideTip, hover, drawColumns, drawLines, drawBars, drawHeatmap, scaleLegend,
         table, empty, legendOf} = window.DashCharts;
  const SLOTS = ['--series-1', '--series-2', '--series-3', '--series-4', '--series-5'];
  const state = Object.fromEntries(DATA.filters.map(f => [f.id, 'all']));

  const sliceKey = () => DATA.filters.map(f => state[f.id]).join('|') || 'all';
  const isDefault = () => sliceKey() === DATA.default_slice;

  // 数字格式：金额带单位，1 万以上按万、亿缩写；计数等其他数字 100 万以内写全（39,516 而不是“4万”）；比率按百分比
  function formatter(chart, full = false) {
    const metric = DATA.metrics[chart.metrics[0]];
    const unit = chart.unit || '';
    if (metric.format === 'percent') return v => (v == null ? '—' : pct(v, full ? 2 : 1));
    const shortFrom = metric.format === 'money' ? 1e4 : 1e6;
    return v => {
      if (v == null) return '—';
      const a = Math.abs(v);
      if (!full && a >= 1e8) return unit + (v / 1e8).toLocaleString('en-US', {maximumFractionDigits: 2}) + '亿';
      if (!full && a >= shortFrom) return unit + (v / 1e4).toLocaleString('en-US', {maximumFractionDigits: 1}) + '万';
      return unit + Number(v).toLocaleString('en-US', {maximumFractionDigits: 2});
    };
  }

  // 分组的颜色跟着分组走，不跟着排名走：按默认视图（全部数据）里的分组顺序固定下来，筛选后不会换色
  const seriesOrder = Object.fromEntries(DATA.charts.map(c => [c.id,
    DATA.slices[DATA.default_slice][c.id].series_order || []]));
  const colorOf = (chart, name) => token(SLOTS[Math.max(seriesOrder[chart.id].indexOf(name), 0) % SLOTS.length]);

  function pivot(chart, rows) {
    const xs = [...new Set(rows.map(r => r.x))];
    const partial = Object.fromEntries(rows.map(r => [r.x, !!r.partial]));
    const names = seriesOrder[chart.id].filter(n => rows.some(r => r.s === n));
    const cell = new Map(rows.map(r => [`${r.x}\u0000${r.s}`, r.values[0]]));
    return {points: xs.map(x => ({key: x, partial: partial[x]})),
            series: names.map(n => ({name: n, color: colorOf(chart, n), values: xs.map(x => cell.get(`${x}\u0000${n}`) ?? null)}))};
  }

  function renderKpis(slice) {
    const kpis = DATA.charts.filter(c => c.type === 'kpi');
    document.getElementById('kpis').replaceChildren(...kpis.map(c => {
      const value = slice[c.id].rows[0].values[0];
      return h('div', {class: 'kpi'}, h('div', {class: 'label'}, c.title),
               h('div', {class: 'value', title: formatter(c, true)(value)}, formatter(c)(value)),
               h('div', {class: 'note'}, c.why));
    }));
  }

  function chartView(chart, data) {
    const fmt = formatter(chart), full = formatter(chart, true);
    const metricLabel = DATA.metrics[chart.metrics[0]].label;
    const rows = data.rows;
    if (!rows.length) return {draw: empty('当前筛选下没有数据。')};
    if (chart.type === 'column') {
      return {draw: host => drawColumns(host, {
        points: rows.map(r => ({key: r.x, partial: r.partial})), format: fmt, title: chart.title,
        series: [{name: metricLabel, color: token('--series-1'), values: rows.map(r => r.values[0])}],
        onTip: i => [rows[i].x + (rows[i].partial ? '（不完整）' : ''), [{value: full(rows[i].values[0]), label: metricLabel}]
          .concat(rows[i].share != null ? [{value: pct(rows[i].share), label: '占比'}] : [])]})};
    }
    if (chart.type === 'stacked_column' || chart.type === 'line') {
      const {points, series} = chart.series_label ? pivot(chart, rows)
        : {points: rows.map(r => ({key: r.x, partial: r.partial})),
           series: [{name: metricLabel, color: token('--series-1'), values: rows.map(r => r.values[0])}]};
      const draw = chart.type === 'line' ? drawLines : drawColumns;
      return {
        draw: host => draw(host, {points, series, format: fmt, title: chart.title,
                                  onTip: i => [points[i].key + (points[i].partial ? '（不完整）' : ''),
                                               series.map(sr => ({color: sr.color, value: full(sr.values[i]), label: sr.name}))]}),
        legend: series.length > 1 ? legendOf(series) : null,
      };
    }
    if (chart.type === 'bar') {
      return {draw: host => drawBars(host, rows.map(r => ({
        label: r.x, value: Math.max(r.values[0] || 0, 0), valueText: fmt(r.values[0]), muted: !!r.other,
        tip: [{value: full(r.values[0]), label: metricLabel}].concat(r.share != null ? [{value: pct(r.share), label: '占比'}] : []),
      })))};
    }
    if (chart.type === 'heatmap') {
      const xs = [...new Set(rows.map(r => r.x))], ys = seriesOrder[chart.id].filter(n => rows.some(r => r.s === n));
      const cell = new Map(rows.map(r => [`${r.x}\u0000${r.s}`, r.values[0]]));
      const values = ys.map(y => xs.map(x => cell.get(`${x}\u0000${y}`) ?? null));
      const max = Math.max(0, ...values.flat().filter(v => v != null));
      return {
        draw: host => drawHeatmap(host, {rowLabels: ys, colLabels: xs, colEvery: xs.length > 12 ? 2 : 1, values, max,
                                         title: chart.title, labelW: 64,
                                         onTip: (i, j) => [`${ys[i]} · ${xs[j]}`, [{value: full(values[i][j]), label: metricLabel}]]}),
        legend: scaleLegend(fmt(0), fmt(max)),
      };
    }
    return {draw: host => host.replaceChildren(tableOf(chart, rows))};  // table
  }

  function tableOf(chart, rows) {
    const fmts = chart.metrics.map(id => formatter({...chart, metrics: [id]}, true));
    const columns = [{label: chart.x_label || ''}].concat(chart.series_label ? [{label: chart.series_label}] : [])
      .concat(chart.metrics.map(id => ({label: DATA.metrics[id].label, num: true})))
      .concat(rows.some(r => r.share != null) ? [{label: '占比', num: true}] : []);
    return table(columns, rows.map(r => [r.x + (r.partial ? '（不完整）' : '')]
      .concat(chart.series_label ? [r.s] : [])
      .concat(r.values.map((v, i) => fmts[i](v)))
      .concat(rows.some(x => x.share != null) ? [r.share == null ? '' : pct(r.share)] : [])));
  }

  function renderCharts(slice) {
    for (const chart of DATA.charts.filter(c => c.type !== 'kpi')) {
      const card = document.getElementById(`chart-${chart.id}`);
      const data = slice[chart.id];
      const view = chartView(chart, data);
      view.draw(card.querySelector('.chart-host'));
      card.querySelector('.extras').replaceChildren(...(view.legend ? [view.legend] : []));
      card.querySelector('.table-host').replaceChildren(data.rows.length ? tableOf(chart, data.rows) : '');
      const insight = card.querySelector('.insight');
      insight.textContent = chart.insight ? (isDefault() ? chart.insight : '（结论是按全部数据写的，加了筛选后不显示）') : '';
      insight.classList.toggle('muted', !isDefault());
      card.querySelector('.note').replaceChildren(...data.notes.map(n => h('div', {}, `⚠ ${n}`)));
    }
  }

  function buildCards() {
    const grid = document.getElementById('charts');
    // 条形图两两并排占半行；落单的条形图和其他图都占满一行
    const charts = DATA.charts.filter(c => c.type !== 'kpi');
    const half = new Set();
    for (let i = 0; i < charts.length - 1; i++) {
      if (charts[i].type === 'bar' && charts[i + 1].type === 'bar' && !half.has(charts[i].id)) {
        half.add(charts[i].id).add(charts[i + 1].id);
      }
    }
    grid.replaceChildren(...charts.map(chart => {
      const toggle = h('button', {type: 'button', class: 'view-toggle', 'aria-pressed': 'false'}, '看表格');
      const card = h('section', {class: `card dash-card${half.has(chart.id) ? '' : ' span-2'}`, id: `chart-${chart.id}`},
        h('div', {class: 'card-head'}, h('h2', {}, chart.title), toggle),
        h('p', {class: 'insight'}), h('div', {class: 'chart-host'}), h('div', {class: 'extras'}),
        h('div', {class: 'table-host table-wrap', hidden: true}),
        h('p', {class: 'why'}, `为什么放这张图：${chart.why}`), h('div', {class: 'note'}));
      toggle.addEventListener('click', () => {
        const showTable = toggle.getAttribute('aria-pressed') !== 'true';
        toggle.setAttribute('aria-pressed', String(showTable));
        toggle.textContent = showTable ? '看图' : '看表格';
        card.querySelector('.chart-host').hidden = showTable;
        card.querySelector('.extras').hidden = showTable;
        card.querySelector('.table-host').hidden = !showTable;
      });
      return card;
    }));
  }

  function buildFilters() {
    const host = document.getElementById('filters');
    if (!DATA.filters.length) { host.remove(); return; }
    host.replaceChildren(...DATA.filters.map(f => h('fieldset', {}, h('legend', {}, f.label),
      h('div', {class: 'seg'}, f.options.map((o, i) => {
        const input = h('input', {type: 'radio', name: f.id, value: o.id, checked: i === 0});
        input.addEventListener('change', () => { state[f.id] = o.id; hideTip(); render(); });
        return h('label', {}, input, h('span', {}, o.label));
      })))));
  }

  function render() {
    const slice = DATA.slices[sliceKey()];
    renderKpis(slice);
    renderCharts(slice);
  }

  buildFilters();
  buildCards();
  let timer, lastWidth = 0;
  const later = () => { clearTimeout(timer); timer = setTimeout(render, 120); };
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', later);
  new ResizeObserver(entries => {
    const w = Math.round(entries[0].contentRect.width);
    if (w !== lastWidth) { lastWidth = w; later(); }
  }).observe(document.querySelector('main'));
  render();
})();
