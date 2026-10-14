/* 数据看板（M12）：读取页面里内嵌的 JSON（reports/dashboard/dashboard.json），画出全部图表。不依赖第三方库。
   商品名、国家名来自原始数据，当作不可信文本：一律用 textContent / 文本节点写进页面，不拼 HTML。
   每张图都有表格视图，悬停能看到的数字在表格里都能找到。 */
(() => {
  'use strict';
  const DATA = JSON.parse(document.getElementById('dashboard-data').textContent);
  const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const state = {period: 'all', market: 'all'};

  const {h, s, label, token, num, money, moneyShort, pct, monthName, sum, seqColor, showTip, hideTip, hover,
         drawColumns, drawBars, drawHeatmap, scaleLegend, drawPareto, sparkline, table, empty, legendOf} = window.DashCharts;

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
        points: months.map(mo => ({key: mo.month, partial: mo.partial})), name: monthName,
        title: '每月 GMV', format: moneyShort,
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
        points: months.map(mo => ({key: mo.month, partial: mo.partial})), name: monthName,
        series, title: '每月 GMV 按新老客拆分', format: moneyShort,
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
        `${WEEKDAYS[quietest]}最少，这段时间一共只有 ${num(byDay[quietest])} 单。` : '',
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
