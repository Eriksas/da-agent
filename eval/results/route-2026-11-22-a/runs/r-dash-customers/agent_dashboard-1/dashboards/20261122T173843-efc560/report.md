> **AI 初稿，待人工复核。** 数字核查：58/58 个能在工具输出中找到出处（另有 4 个 ≤ 10 的整数未核查）；需人工复核的表述 0 处。流程检查：已加载 dashboard-design，缺少必做步骤 submit_dashboard。

**问题：** 请为下面的读者搭一个看板。读者和目标：给运营负责人的新老客周度复盘看板：按周展示新客数、老客数、新客GMV、老客GMV及各自占比趋势，外加GMV与订单数周度趋势，聚焦2011年最近20周（最近完整周为2011-W48），帮他判断规模变化由新客还是老客驱动。只用可识别客户口径。

## 交付说明
工具调用额度已用尽（23/25），最后一次 KPI 预览被拒，**未能调用 `submit_dashboard` 落库**。下面是已按预览数据核对完成的完整方案与结论，所有数字均来自 `preview_chart` 的返回。

## 看板：2011 最近 20 周新老客周度复盘（2011-W29~W48，2011-07-18~2011-12-04）

**读者/目标**：运营负责人周度复盘，判断规模变化由新客（客群扩张）还是老客（复购/客单）驱动。

**指标口径**（8 个）：销售GMV、取消GMV、净GMV(销售−取消)、订单数、活跃可识别客户数、客单价、每客订单数、取消占比。

| 图 | 类型 | 回答什么 | 洞察 |
|---|---|---|---|
| 20周净GMV | kpi | 整体成交规模 | £4,690,821.88（销售 4,805,235.32 − 取消 114,413.44） |
| 20周可识别客户数 | kpi | 参与交易的可识别客户 | 20 周去重 3,203 人 |
| 每周净GMV | line | 规模怎么变 | W35 最低 £137,221.35 → W46 最高 £369,129.47，W48 £306,115.06（环比 +3.7%） |
| 每周订单数 | line | 是单量还是单均驱动 | W32 最低 270 单 → W46 最高 687 单，W48 663 单（+7.6%） |
| 每周活跃可识别客户数 | line | 客群是否扩张 | W29 281 → W46 最高 569，W48 514（+4.5%） |
| 每周客单价 | line | 客单价是否支撑增长 | £528.96 → £466.64（-4.9%），峰值 W38 £709.96 |
| 每周每客订单数 | line | 复购强度 | 1.15~1.29 单，W48 最高 1.29（663 单 / 514 客户） |
| 每周取消占比 | line | GMV 波动有多少被取消 | 整体 2.38%，W41 最高 7.69%，W48 1.06% |
| 20周 GMV 前 10 客户 | bar | 成交是否集中于少数大客户 | 无ID行 £656,152.80 占 13.65%；最大单一客户 14646 仅占 2.75%，其余 3193 人占 71.55% |

## 结论
20 周内净GMV、订单数、可识别客户数同向上行（W29→W48：£182,809→£306,115、355→663 单、281→514 客户），而客单价反降 4.9%、每客订单数基本持平在 1.2~1.3 单——**规模变化主要由客群（客户数与订单量）扩张驱动，而非客单价或复购加深**；成交高度分散，无单一老客户可解释增长。

## 数据口径提醒（notes）
1. **新客无法按周拆分**：数据无首单/注册标记，指标引擎只支持同周去重、无法跨周取首单，故用「活跃可识别客户数＋每客订单数＋客单价」近似回答新老客驱动问题。
2. **GMV/订单数为全量销售口径**：引擎不支持按 customer_id 非空过滤金额与订单；20 周内无 customer_id 的行 GMV £656,152.80（13.65%）混在其中，客户数指标已自动排除这 22.2% 的行。
3. 区间只含 20 个完整周（2011-W29~W48），W49（12-05~12-09）不完整未纳入；每周客户数为当周去重，不可跨周相加。
4. 取消单会抵消下单额（整体取消占比 2.38%），看真实成交用净GMV。

---

## 附录 A：流程检查与危险信号

- 流程 `dashboard-design`：必做步骤 profile_table、preview_chart、submit_dashboard；**缺少 submit_dashboard**
- 没有变化超过 50% 的核心指标。

## 附录 B：找不到出处的数字

无。58 个数字都能在工具输出中找到出处。注意：这只说明数字不是编的，不保证每个数字被用在了正确的指标上。

## 附录 C：需要人工复核的表述

未发现因果表述或绝对化说法。推理是否成立仍需人工判断。

## 附录 D：工具调用

| 轮 | 工具 | 参数 | 结果 |
|---:|---|---|---|
| 1 | load_skill | `{"name": "dashboard-design"}` | 成功 |
| 1 | profile_table | `{}` | 成功 |
| 2 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "cust_sales", "label": "可识别客户数", "agg": "count_distinct", "column": "customer_id", "where": {"line_type": ["销售"]}, "format": "number"}], "chart": {"id": "t_a", "type": "line", "title": "每周可识别客户数", "metrics": ["cust_sales"], "x": {"column": "invoice_time", "grain": "week"}, "why": "探索每周可识别客户数口径"}}` | 成功 |
| 2 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_all", "label": "全部GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]}, "format": "money"}], "chart": {"id": "t_b", "type": "line", "title": "每周GMV(全部客户)", "metrics": ["gmv_all"], "x": {"column": "invoice_time", "grain": "week"}, "why": "探索GMV是否包含游客"}}` | 成功 |
| 2 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_null", "label": "缺失客户GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"], "customer_id": ["null"]}, "format": "money"}], "chart": {"id": "t_c", "type": "line", "title": "测试缺失客户过滤", "metrics": ["gmv_null"], "x": {"column": "invoice_time", "grain": "week"}, "why": "测试customer_id缺失值过滤写法"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_null：where 里 customer_id 没有这些取值 ['null']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 3 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "probe", "label": "探针", "agg": "sum", "column": "amount", "where": {"first_purchase_week": ["x"]}, "format": "money"}], "chart": {"id": "p1", "type": "line", "title": "探针列名", "metrics": ["probe"], "x": {"column": "invoice_time", "grain": "week"}, "why": "探查引擎是否支持派生列"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 probe：where 里没有 first_purchase_week 这一列
- 图 p1：指标 probe 聚合的 amount 含义取决于 line_type，要在 where 里把 line_type 限定为一个取值，或者按 line_type 分组 |
| 3 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_guest", "label": "游客GMV", "agg": "sum", "column": "amount", "where": {"customer_id": ["null"], "line_type": ["销售"]}, "format": "money"}], "chart": {"id": "p2", "type": "line", "title": "每周游客GMV", "metrics": ["gmv_guest"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证游客口径可过滤"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_guest：where 里 customer_id 没有这些取值 ['null']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 4 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_guest", "label": "游客GMV", "agg": "sum", "column": "amount", "where": {"customer_id": ["null"], "line_type": ["销售"]}, "format": "money"}], "chart": {"id": "p_guest", "type": "line", "title": "每周游客GMV", "metrics": ["gmv_guest"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证游客口径过滤是否可用"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_guest：where 里 customer_id 没有这些取值 ['null']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 4 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_all", "label": "全部GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]}, "format": "money"}, {"id": "gmv_guest", "label": "游客GMV", "agg": "sum", "column": "amount", "where": {"customer_id": ["null"], "line_type": ["销售"]}, "format": "money"}, {"id": "gmv_id", "label": "可识别客户GMV", "agg": "difference", "left": "gmv_all", "right": "gmv_guest", "format": "money"}], "chart": {"id": "p_id", "type": "line", "title": "每周可识别客户GMV", "metrics": ["gmv_id"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证difference能否得到可识别客户GMV"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_guest：where 里 customer_id 没有这些取值 ['null']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 5 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_guest", "label": "游客GMV", "agg": "sum", "column": "amount", "where": {"customer_id": [""], "line_type": ["销售"]}, "format": "money"}], "chart": {"id": "p_guest2", "type": "line", "title": "每周游客GMV", "metrics": ["gmv_guest"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证缺失客户口径"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_guest：where 里 customer_id 没有这些取值 ['']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 6 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_guest", "label": "游客GMV", "agg": "sum", "column": "amount", "where": {"customer_id": ["null"], "line_type": ["销售"]}, "format": "money"}], "chart": {"id": "p_guest3", "type": "line", "title": "每周游客GMV", "metrics": ["gmv_guest"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证缺失客户口径"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_guest：where 里 customer_id 没有这些取值 ['null']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 6 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_guest", "label": "游客GMV", "agg": "sum", "column": "amount", "where": {"customer_id": ["None"], "line_type": ["销售"]}, "format": "money"}], "chart": {"id": "p_guest4", "type": "line", "title": "每周游客GMV", "metrics": ["gmv_guest"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证缺失客户口径"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_guest：where 里 customer_id 没有这些取值 ['None']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 7 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_guest", "label": "游客GMV", "agg": "sum", "column": "amount", "where": {"customer_id": ["null"], "line_type": ["销售"]}, "format": "money"}], "chart": {"id": "g1", "type": "line", "title": "每周游客GMV", "metrics": ["gmv_guest"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证缺失客户口径"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_guest：where 里 customer_id 没有这些取值 ['null']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 7 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_guest", "label": "游客GMV", "agg": "sum", "column": "amount", "where": {"customer_id": ["*"], "line_type": ["销售"]}, "format": "money"}], "chart": {"id": "g2", "type": "line", "title": "每周游客GMV", "metrics": ["gmv_guest"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证缺失客户口径"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_guest：where 里 customer_id 没有这些取值 ['*']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 8 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "probe2", "label": "探针", "agg": "count", "column": "quantity", "where": {"quantity": ["null"]}}], "chart": {"id": "probe2", "type": "line", "title": "探针空值写法", "metrics": ["probe2"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证空值能否透传"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 probe2：count 数的是行数，不填 column（数不同的值用 count_distinct）
- 图 probe2：指标 probe2 聚合的 quantity 含义取决于 line_type，要在 where 里把 line_type 限定为一个取值，或者按 line_type 分组 |
| 8 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "orders", "label": "订单数", "agg": "count_distinct", "column": "invoice", "where": {"line_type": ["销售"]}, "format": "number"}], "chart": {"id": "p_orders", "type": "line", "title": "每周订单数", "metrics": ["orders"], "x": {"column": "invoice_time", "grain": "week"}, "why": "订单数周度趋势"}}` | 成功 |
| 9 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "probe3", "label": "探针", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"], "quantity": ["null"]}, "format": "money"}], "chart": {"id": "probe3", "type": "line", "title": "探针空值写法", "metrics": ["probe3"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证空值能否透传"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 probe3：where 里 quantity 没有这些取值 ['null']；常见取值：['1', '2', '12', '4', '6', '3', '24', '10'] |
| 9 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_all", "label": "GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]}, "format": "money"}, {"id": "orders", "label": "订单数", "agg": "count_distinct", "column": "invoice", "where": {"line_type": ["销售"]}, "format": "number"}, {"id": "aov", "label": "客单价", "agg": "ratio", "left": "gmv_all", "right": "orders", "format": "money"}], "chart": {"id": "p_aov", "type": "line", "title": "每周客单价", "metrics": ["aov"], "x": {"column": "invoice_time", "grain": "week"}, "why": "验证比率指标按周可算"}}` | 成功 |
| 10 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_sales", "label": "销售GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]}, "format": "money"}, {"id": "gmv_cancel", "label": "取消GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["取消"]}, "format": "money"}, {"id": "net_gmv", "label": "净GMV", "agg": "difference", "left": "gmv_sales", "right": "gmv_cancel", "format": "money"}], "chart": {"id": "p_net", "type": "line", "title": "每周净GMV", "metrics": ["net_gmv"], "x": {"column": "invoice_time", "grain": "week"}, "why": "周度规模趋势"}}` | 成功 |
| 10 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "orders", "label": "订单数", "agg": "count_distinct", "column": "invoice", "where": {"line_type": ["销售"]}, "format": "number"}, {"id": "customers", "label": "活跃可识别客户数", "agg": "count_distinct", "column": "customer_id", "where": {"line_type": ["销售"]}, "format": "number"}, {"id": "opc", "label": "每客订单数", "agg": "ratio", "left": "orders", "right": "customers", "format": "number"}], "chart": {"id": "p_opc", "type": "line", "title": "每周每客订单数", "metrics": ["opc"], "x": {"column": "invoice_time", "grain": "week"}, "reference": "overall", "why": "复购强度"}}` | 成功 |
| 10 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_sales", "label": "销售GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]}, "format": "money"}, {"id": "gmv_cancel", "label": "取消GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["取消"]}, "format": "money"}, {"id": "cancel_share", "label": "取消占比", "agg": "ratio", "left": "gmv_cancel", "right": "gmv_sales", "format": "percent"}], "chart": {"id": "p_cancel", "type": "line", "title": "每周取消金额占销售比例", "metrics": ["cancel_share"], "x": {"column": "invoice_time", "grain": "week"}, "reference": "overall", "why": "数据口径占比"}}` | 成功 |
| 10 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_sales", "label": "销售GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]}, "format": "money"}], "chart": {"id": "p_topcust", "type": "bar", "title": "20周 GMV 前 10 客户", "metrics": ["gmv_sales"], "x": {"column": "customer_id", "label": "客户"}, "top_n": 10, "why": "客户集中度"}}` | 成功 |
| 11 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_guest", "label": "未登记客户GMV", "agg": "sum", "column": "amount", "where": {"customer_id": ["（空）"], "line_type": ["销售"]}, "format": "money"}], "chart": {"id": "p_label", "type": "line", "title": "每周未登记客户GMV", "metrics": ["gmv_guest"], "x": {"column": "invoice_time", "grain": "week"}, "why": "测试空标签可否作为过滤值"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 指标 gmv_guest：where 里 customer_id 没有这些取值 ['（空）']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 11 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_sales", "label": "销售GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]}, "format": "money"}], "chart": {"id": "p_stack_guest", "type": "stacked_column", "title": "每周销售GMV构成", "metrics": ["gmv_sales"], "x": {"column": "invoice_time", "grain": "week"}, "series": {"column": "customer_id", "keep": ["（空）"], "other_label": "可识别客户"}, "why": "看每周未登记与可识别客户GMV构成"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 图 p_stack_guest 的 series：keep 里 customer_id 没有这些取值 ['（空）']；常见取值：[None, '14096', '17841', '14911', '12748', '14606', '15311', '14456'] |
| 12 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "gmv_sales", "label": "销售GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]}, "format": "money"}, {"id": "gmv_cancel", "label": "取消GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["取消"]}, "format": "money"}, {"id": "net_gmv", "label": "净GMV", "agg": "difference", "left": "gmv_sales", "right": "gmv_cancel", "format": "money"}], "chart": {"id": "kpi_net", "type": "kpi", "title": "20周净GMV", "metrics": ["net_gmv"], "why": "看20周整体成交规模"}}` | 成功 |
| 12 | preview_chart | `{"period": {"end": "2011-12-04", "start": "2011-07-18"}, "metrics": [{"id": "customers", "label": "可识别客户数", "agg": "count_distinct", "column": "customer_id", "where": {"line_type": ["销售"]}, "format": "number"}], "chart": {"id": "kpi_cust", "type": "kpi", "title": "20周可识别客户数", "metrics": ["customers"], "why": "看20周参与交易的可识别客户规模"}}` | 失败：工具调用次数已用完，请直接根据已有结果回答 |

## 附录 E：工具警告汇总

- 无

## 附录 F：运行信息

- 运行编号 `20261122T173843-efc560`，状态 completed，模型 MiniMax-M3.1-Flash
- 调用模型 13 次，token 477049 + 55531，用时 531.8 秒
- 工具调用上限 25 次，本次已用完
