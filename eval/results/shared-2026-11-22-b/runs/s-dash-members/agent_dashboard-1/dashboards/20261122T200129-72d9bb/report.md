> **AI 初稿，待人工复核。** 数字核查：50/50 个能在工具输出中找到出处（另有 18 个 ≤ 10 的整数未核查）；用 calculate 计算 1 次，输入都有出处；需人工复核的表述 0 处。流程检查：已加载 dashboard-design，缺少必做步骤 submit_dashboard。

**问题：** 请为下面的读者搭一个看板。读者和目标：给会员运营同学看的月度会员看板，时间 2011 年 1 月到 12 月。看三件事：(1) 每月新客人数和老客人数（新客=该月首次购买，老客=该月之前买过、当月又买）各多少，叠加柱状图或双轴图；(2) 每月新客与老客分别贡献的订单数和 GMV，折线图；(3) 新客来自哪些国家，每月新客人数 Top 10 国家堆叠柱状图或排名图。12 月数据只到 12-09，需在图中标注为不完整月。

## 看板方案（2011 年月度会员看板 · 会员运营）

**KPI（3 张）**：新客人数 **1,537**；老客人数 **3,466**；新客 GMV 占比 **7.19%**（£680,706.03 / £9,471,580.96）

**分区一 客群规模**
- 每月新客与老客人数（line，两序列）：老客每月远多于新客，老客最高 2011-11 **1,492 人**，新客最高 2011-10 **221 人**，12 月老客 585 人、新客 30 人
- 每月新客人数（column）：9–10 月抬头，10 月 221 人、11 月 191 人、12 月仅 30 人（不完整）

**分区二 客群贡献**
- 每月新客与老客订单数（line）：2011-11 老客 2,432 笔、新客 210 笔、缺客户 ID 的「未知」109 笔
- 每月新客与老客 GMV（line）：2011-11 老客 £1,064,539.67、新客 £71,994.33、未知 £315,581.98

**分区三 新客来源**
- 2011 年新客来源国家 Top10（bar，top_n=10）：英国 1,357 人占全年新客 **88.3%**，法国 46、德国 38，其后西班牙 14、比利时 11；共 28 国产生新客
- 每月新客来源国家（heatmap：英国／法国／其他国家）：英国每月 63–195 人，法国每月 1–7 人，其他国家每月 5–21 人

**数据说明**：① 12 月只到 12-09、1 月因 12-23 至 01-03 停业只从 01-04 起，均为不完整月，不与完整月直接比；② 约 22% 行缺客户 ID，人数口径不计（为 0），GMV/订单计入并单列「未知」；③ 新客按 ISO 周判定首次购买后落到所在月；④ GMV 为下单口径，含之后被取消的订单，取消按发生时间入账，不回溯原订单。

---

**最终回答**：看板面向会员运营同学，2011 全年三问三答：① 每月新客（最高 2011-10 的 221 人）与老客（最高 2011-11 的 1,492 人）人数，line 加柱状图；② 新老客每月订单数与 GMV 折线，2011-11 老客订单 2,432 笔、GMV £1,064,539.67，新客仅 210 笔、£71,994.33；③ 新客国家 Top10 排名与月度热力图，英国 1,357 人占 88.3%，法国 46、德国 38，共 28 国。注意 12 月仅到 9 日、1 月自 4 日起，均不完整；约 22% 行缺客户 ID，人数不计但金额计入。

---

## 附录 A：流程检查与危险信号

- 流程 `dashboard-design`：必做步骤 profile_table、preview_chart、submit_dashboard；**缺少 submit_dashboard**
- 没有变化超过 50% 的核心指标。

## 附录 B：找不到出处的数字

无。50 个数字都能在工具输出中找到出处。注意：这只说明数字不是编的，不保证每个数字被用在了正确的指标上。

## 附录 C：需要人工复核的表述

未发现因果表述或绝对化说法。推理是否成立仍需人工判断。

## 附录 D：工具调用

| 轮 | 工具 | 参数 | 结果 |
|---:|---|---|---|
| 1 | load_skill | `{"name": "dashboard-design"}` | 成功 |
| 1 | profile_table | `{}` | 成功 |
| 2 | preview_chart | `{"metrics": [{"id": "active_customers", "label": "活跃客户数", "agg": "count_distinct", "column": "buyer_id"}], "chart": {"id": "c1", "title": "每月新客与老客人数", "type": "stacked_column", "metrics": ["active_customers"], "x": {"column": "order_time", "grain": "month"}, "series": {"column": "customer_type", "keep": ["新客", "老客"], "other_label": "其他"}}, "period": {"start": "2011-01", "end": "2011-12"}}` | 失败：参数不合法：chart.why: Field required。正确写法：{"metrics": [完整的指标定义，ratio 要连同它引用的两个指标], "chart": {"id": "c1", "title": "各访客类型转化率", "type": "bar", "metrics": ["rate"], "x": {"column": "VisitorType"}, "reference": "overall", "highlight": ["New_Visitor"], "why": "……"}}。metrics 放指标定义；chart.metrics 只写指标 id；top_n、reference、highlight、width、section 是 chart 的字段，不写在 x 里 |
| 2 | preview_chart | `{"metrics": [{"id": "orders", "label": "订单数", "agg": "count_distinct", "column": "order_id"}], "chart": {"id": "c2", "title": "每月新客与老客订单数", "type": "line", "metrics": ["orders"], "x": {"column": "order_time", "grain": "month"}, "series": {"column": "customer_type", "keep": ["新客", "老客"], "other_label": "其他"}}, "period": {"start": "2011-01", "end": "2011-12"}}` | 失败：参数不合法：chart.why: Field required。正确写法：{"metrics": [完整的指标定义，ratio 要连同它引用的两个指标], "chart": {"id": "c1", "title": "各访客类型转化率", "type": "bar", "metrics": ["rate"], "x": {"column": "VisitorType"}, "reference": "overall", "highlight": ["New_Visitor"], "why": "……"}}。metrics 放指标定义；chart.metrics 只写指标 id；top_n、reference、highlight、width、section 是 chart 的字段，不写在 x 里 |
| 2 | preview_chart | `{"metrics": [{"id": "gmv", "label": "GMV", "agg": "sum", "column": "gmv_amount", "format": "money"}], "chart": {"id": "c3", "title": "每月新客与老客GMV", "type": "line", "metrics": ["gmv"], "x": {"column": "order_time", "grain": "month"}, "series": {"column": "customer_type", "keep": ["新客", "老客"], "other_label": "其他"}}, "period": {"start": "2011-01", "end": "2011-12"}}` | 失败：参数不合法：chart.why: Field required。正确写法：{"metrics": [完整的指标定义，ratio 要连同它引用的两个指标], "chart": {"id": "c1", "title": "各访客类型转化率", "type": "bar", "metrics": ["rate"], "x": {"column": "VisitorType"}, "reference": "overall", "highlight": ["New_Visitor"], "why": "……"}}。metrics 放指标定义；chart.metrics 只写指标 id；top_n、reference、highlight、width、section 是 chart 的字段，不写在 x 里 |
| 2 | preview_chart | `{"metrics": [{"id": "new_customers", "label": "新客数", "agg": "count_distinct", "column": "buyer_id", "where": {"customer_type": ["新客"]}}], "chart": {"id": "c4", "title": "新客来源国家Top10", "type": "bar", "metrics": ["new_customers"], "x": {"column": "country", "label": "国家"}, "top_n": 10}, "period": {"start": "2011-01", "end": "2011-12"}}` | 失败：参数不合法：chart.why: Field required。正确写法：{"metrics": [完整的指标定义，ratio 要连同它引用的两个指标], "chart": {"id": "c1", "title": "各访客类型转化率", "type": "bar", "metrics": ["rate"], "x": {"column": "VisitorType"}, "reference": "overall", "highlight": ["New_Visitor"], "why": "……"}}。metrics 放指标定义；chart.metrics 只写指标 id；top_n、reference、highlight、width、section 是 chart 的字段，不写在 x 里 |
| 3 | preview_chart | `{"chart": {"id": "c1", "insight": "", "metrics": ["active_customers"], "section": "客群规模", "series": {"column": "customer_type", "keep": ["新客", "老客"], "other_label": "其他"}, "title": "每月新客与老客人数", "type": "stacked_column", "why": "看每月新客和老客各有多少人", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "active_customers", "label": "活跃客户数"}], "period": {"end": "2011-12", "start": "2011-01"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 图 c1：堆叠柱的各段要能相加，active_customers 是 count_distinct，不能堆叠；改用 line 分组，或换成 sum / count 指标 |
| 3 | preview_chart | `{"chart": {"id": "c2", "insight": "", "metrics": ["orders"], "section": "贡献", "series": {"column": "customer_type", "keep": ["新客", "老客"], "other_label": "其他"}, "title": "每月新客与老客订单数", "type": "line", "why": "看新客和老客各自贡献多少订单", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "order_id", "id": "orders", "label": "订单数"}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 3 | preview_chart | `{"chart": {"id": "c3", "insight": "", "metrics": ["gmv"], "section": "贡献", "series": {"column": "customer_type", "keep": ["新客", "老客"], "other_label": "其他"}, "title": "每月新客与老客GMV", "type": "line", "why": "看新客和老客各自贡献多少GMV", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "sum", "column": "gmv_amount", "format": "money", "id": "gmv", "label": "GMV"}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 3 | preview_chart | `{"chart": {"id": "c4", "insight": "", "metrics": ["new_customers"], "section": "新客来源", "title": "新客来源国家Top10", "top_n": 10, "type": "bar", "why": "看新客主要来自哪些国家", "x": {"column": "country", "label": "国家"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "new_customers", "label": "新客数", "where": {"customer_type": ["新客"]}}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 4 | preview_chart | `{"chart": {"id": "c1", "insight": "", "metrics": ["active_customers"], "section": "客群规模", "series": {"column": "customer_type", "keep": ["新客", "老客"], "other_label": "未知"}, "title": "每月新客与老客人数", "type": "column", "why": "看每月新客和老客各有多少人", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "active_customers", "label": "活跃客户数"}], "period": {"end": "2011-12", "start": "2011-01"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 图 c1：column 不用 series；要按组拆开请用 line、stacked_column 或 heatmap |
| 4 | preview_chart | `{"chart": {"id": "c5", "insight": "", "metrics": ["new_customers"], "section": "新客来源", "series": {"column": "country", "keep": ["United Kingdom", "France", "Germany", "Spain", "Belgium"], "other_label": "其他"}, "title": "每月新客国家构成Top5", "type": "stacked_column", "why": "看新客每月来自哪些国家", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "new_customers", "label": "新客数", "where": {"customer_type": ["新客"]}}], "period": {"end": "2011-12", "start": "2011-01"}}` | 失败：方案没有通过检查，请逐条修改后再提交：
- 图 c5：堆叠柱的各段要能相加，new_customers 是 count_distinct，不能堆叠；改用 line 分组，或换成 sum / count 指标 |
| 4 | preview_chart | `{"chart": {"id": "k_new", "insight": "", "metrics": ["new_customers"], "title": "2011年新客人数", "type": "kpi", "why": "看全年新增会员规模"}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "new_customers", "label": "新客数", "where": {"customer_type": ["新客"]}}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 4 | preview_chart | `{"chart": {"id": "k_old", "insight": "", "metrics": ["returning_customers"], "title": "2011年老客人数", "type": "kpi", "why": "看全年活跃的回头客规模"}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "returning_customers", "label": "老客数", "where": {"customer_type": ["老客"]}}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 5 | preview_chart | `{"chart": {"id": "c1_line", "insight": "", "metrics": ["active_customers"], "section": "客群规模", "series": {"column": "customer_type", "keep": ["新客", "老客"], "other_label": "未知"}, "title": "每月新客与老客人数（12月仅到9日）", "type": "line", "why": "看每月新客和老客各有多少人", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "active_customers", "label": "活跃客户数"}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 5 | preview_chart | `{"chart": {"id": "c2_col", "insight": "", "metrics": ["new_customers"], "section": "客群规模", "title": "每月新客人数（12月仅到9日）", "type": "column", "why": "看每月新增会员的绝对人数", "width": "half", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "new_customers", "label": "新客数", "where": {"customer_type": ["新客"]}}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 5 | preview_chart | `{"chart": {"id": "c3_orders", "insight": "", "metrics": ["orders"], "section": "客群贡献", "series": {"column": "customer_type", "keep": ["新客", "老客", "未知"], "other_label": "其他"}, "title": "每月新客与老客订单数（12月仅到9日）", "type": "line", "why": "看新客和老客各自贡献多少订单", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "order_id", "id": "orders", "label": "订单数"}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 5 | preview_chart | `{"chart": {"id": "c4_gmv", "insight": "", "metrics": ["gmv"], "section": "客群贡献", "series": {"column": "customer_type", "keep": ["新客", "老客", "未知"], "other_label": "其他"}, "title": "每月新客与老客GMV（12月仅到9日）", "type": "line", "why": "看新客和老客各自贡献多少GMV", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "sum", "column": "gmv_amount", "format": "money", "id": "gmv", "label": "GMV"}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 6 | preview_chart | `{"chart": {"id": "c1_line2", "insight": "", "metrics": ["member_customers"], "section": "客群规模", "series": {"column": "customer_type", "keep": ["新客", "老客"], "other_label": "其他"}, "title": "每月新客与老客人数（12月仅到9日）", "type": "line", "why": "看每月新客和老客各有多少人", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "member_customers", "label": "有客户ID的活跃客户数", "where": {"customer_type": ["新客", "老客"]}}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 6 | preview_chart | `{"chart": {"id": "c6_heat", "insight": "", "metrics": ["new_customers"], "section": "新客来源", "series": {"column": "country", "keep": ["United Kingdom", "France", "Germany", "Spain", "Belgium", "Norway"], "other_label": "其他"}, "title": "每月新客来源国家（12月仅到9日）", "type": "heatmap", "why": "看新客每月来自哪些国家", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "new_customers", "label": "新客数", "where": {"customer_type": ["新客"]}}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 6 | preview_chart | `{"chart": {"id": "k_gmv", "insight": "", "metrics": ["gmv"], "title": "2011年GMV", "type": "kpi", "why": "看全年大盘规模"}, "metrics": [{"agg": "sum", "column": "gmv_amount", "format": "money", "id": "gmv", "label": "GMV"}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 6 | preview_chart | `{"chart": {"id": "k_share", "insight": "", "metrics": ["new_gmv_share"], "title": "新客GMV占比", "type": "kpi", "why": "看新客贡献了多少GMV"}, "metrics": [{"agg": "sum", "column": "gmv_amount", "format": "money", "id": "new_gmv", "label": "新客GMV", "where": {"customer_type": ["新客"]}}, {"agg": "sum", "column": "gmv_amount", "format": "money", "id": "gmv", "label": "GMV"}, {"agg": "ratio", "format": "percent", "id": "new_gmv_share", "label": "新客GMV占比", "left": "new_gmv", "right": "gmv"}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 7 | preview_chart | `{"chart": {"id": "c6_heat", "insight": "", "metrics": ["new_customers"], "section": "新客来源", "series": {"column": "country", "keep": ["United Kingdom", "France", "Germany"], "other_label": "其他国家"}, "title": "每月新客来源国家（12月仅到9日）", "type": "heatmap", "why": "看新客每月来自哪些国家", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "new_customers", "label": "新客数", "where": {"customer_type": ["新客"]}}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |
| 7 | calculate | `{"expression": "1357 / 1537", "purpose": "2011年英国新客占全年新客人数的比例"}` | 成功 |
| 8 | preview_chart | `{"chart": {"id": "c6_heat", "insight": "", "metrics": ["new_customers"], "section": "新客来源", "series": {"column": "country", "keep": ["United Kingdom", "France"], "other_label": "其他国家"}, "title": "每月新客来源国家（12月仅到9日）", "type": "heatmap", "why": "看新客每月来自哪些国家", "x": {"column": "order_time", "grain": "month"}}, "metrics": [{"agg": "count_distinct", "column": "buyer_id", "id": "new_customers", "label": "新客数", "where": {"customer_type": ["新客"]}}], "period": {"end": "2011-12", "start": "2011-01"}}` | 成功 |

## 附录 E：工具警告汇总

- 无

## 附录 F：运行信息

- 运行编号 `20261122T200129-72d9bb`，状态 completed，模型 MiniMax-M3.1-Flash
- 调用模型 9 次，token 304425 + 37799，用时 503.35 秒
- 工具调用上限 25 次，本次已用完
