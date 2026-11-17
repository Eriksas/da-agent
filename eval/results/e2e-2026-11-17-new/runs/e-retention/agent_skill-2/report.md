> **AI 初稿，待人工复核。** 数字核查：74/74 个能在工具输出中找到出处（另有 54 个 ≤ 10 的整数未核查）；用 calculate 计算 2 次，输入都有出处；需人工复核的表述 2 处。流程检查：已加载 ecommerce-metric-diagnosis，缺少必做步骤 decompose_gmv。

**问题：** 2011 年 9 月买过东西的客户，10 月还有多少人回来买？占多少？

# 2011 年 9 月客户 → 10 月回访

**分析对象：2011 年 9 月（9/1–9/25 的 4 个完整周）与 10 月（10/3–10/30 的 4 个完整周）**；数据覆盖至 2011-12-09，两个月均在数据范围内。

## 1. 结论

**现有工具无法给出"9 月客户在 10 月回来"的精确人数与回访率**——工具是周粒度汇总，没有客户 ID 级 cohort 关联能力，"9 月客户 ∩ 10 月客户"的交集数工具未提供。

能算的最接近口径是：**10 月有复购行为的老客（当周有购买、且此前已购买过的可识别客户）4 周合计 1,320 人次**（calculate 结果）；**9 月有购买的可识别客户 4 周合计 225+313+301+383 人次**（分母见 calculate 表达式）。两者相除 = **108.02%**（calculate 结果），**超过 100%**，说明这个比值不是"9 月客户留存率"，而是"10 月复购客池规模 ÷ 9 月客户池规模"——10 月回来的老客里还包含大量 9 月之前就已存在的客户。

## 2. 主要数据

**9 月客户基数**（metric_summary，各周 active_customers）：

| 周 | 日期 | 活跃客户 | 新客 | 老客 | 交易日 |
|---|---|---|---|---|---|
| 2011-W35 | 9/1–9/4 | 225 | 23 | 202 | 5（周一无交易，metric_summary 警告） |
| 2011-W36 | 9/5–9/11 | 313 | 26 | 287 | 6 |
| 2011-W37 | 9/12–9/18 | 301 | 41 | 260 | 6 |
| 2011-W38 | 9/19–9/25 | 383 | 64 | 319 | 6 |

**10 月"回来买"（老客口径，metric_summary returning_customers）**：

| 周 | 日期 | 活跃客户 | 新客 | 老客（回来买） | 交易日 |
|---|---|---|---|---|---|
| 2011-W40 | 10/3–10/9 | 389 | 67 | 322 | 6 |
| 2011-W41 | 10/10–10/16 | 360 | 49 | 311 | 6 |
| 2011-W42 | 10/17–10/23 | 364 | 47 | 317 | 6 |
| 2011-W43 | 10/24–10/30 | 419 | 49 | 370 | 6 |

- 10 月回来买的老客 4 周合计 = **1,320 人次**（calculate：322+311+317+370）。
- 10 月老客池 ÷ 9 月客户池 = **108.02%**（calculate：1320/(225+313+301+383)）。
- 结构事实：10 月每周边都有 47–67 名新客（W40 67、W41 49、W42 47、W43 49，metric_summary），这批人不在 9 月客户池内；10 月活跃客户的绝大部分是有复购行为的老客。
- 10 月各周老客规模（311–370）与 9 月各周老客规模（202–319）同一量级并呈上升，说明 9 月客户池是 10 月复购客的重要来源；但**工具无法拆分 10 月老客中有多少人来自 9 月、多少人来自 8 月及更早**。

**边界可给的范围**：交集人数 ≤ 10 月老客人次 1,320（且去重后更低）；下界用现有工具无法确认（无法排除"9 月客户 10 月全部未回来"这一极端情形）。

## 3. 需要注意

- **口径偷换风险最大的一点**：1,320 是"人次"而非"人数"——同一客户在多周下单会被重复计入（9 月分母 225+313+301+383 同理），所以分子分母都偏高；108.02% 只说明两个池子的规模关系，不能读作"留存率"。
- **月份边界**：2011-W39 覆盖 9/26–10/2（跨月）、2011-W44 覆盖 10/31–11/6（跨月）。为对称起见，主口径把 W39、W44 一并剔除，所以 **9/26–9/30、10/1–10/2、10/31 三段未纳入**（如需纳入：9 月加 W39 活跃 374，10 月加 W39 老客 329、W44 老客 391，方向不变）。
- **W35 只有 5 个交易日**（metric_summary 警告：9/1 起算，比 W34 少 1 天），该周活跃客户 225 与整周口径不可直接比。
- **缺失客户 ID 的订单无法进入任何客户群体**：全量 GMV 中 13.11%、2,922 笔订单缺客户 ID（data_quality_overview，规则 R08），客户级复购口径必然低估。
- **单周客户样本小**（活跃客户 225–441 人，全库仅 5,852 个客户），单月窗口的回访率波动会很大。
- 本文只陈述"同时发生"的数据关系，未做因果归因。

## 4. 建议下一步

1. **要精确回访率，必须拉客户 ID 明细做 cohort 关联**：以 2011-09 全月有购买记录的客户 ID 为分母，关联 2011-10 订单去重计数。现有 4 个工具（metric_summary / decompose_gmv / drilldown / data_quality_overview）都不返回客户级交集，这是唯一的缺口。
2. 顺带把 **10 月缺客户 ID 的订单占比**单独核对（R08，2,922 笔），否则修好明细后仍会有系统性缺口。
3. 补齐 W39/W44 的日期级拆分后重算，得到完整的 9/1–10/31 口径。
4. 运营层面：10 月新客稳定在每 47–67 人（metric_summary），获客投入未掉；但由于真实回访率不可得，**不建议**用 108.02% 做任何考核或预算依据。

## 5. 可信度自评

**需修改后再分享**：周度活跃/新老客的结构和 1,320 人次、108.02% 两个计算结果可直接引用；但"9 月客户 10 月回来的人数和占比"工具未提供，现有数字只能作为规模参照，不能作为留存率对外发布。

---

## 附录 A：流程检查与危险信号

- 流程 `ecommerce-metric-diagnosis`：必做步骤 metric_summary、decompose_gmv、drilldown；**缺少 decompose_gmv**
- ⚠️ 2011-W37 新客数变化 +57.69%，超过 50%：请确认报告已先排查数据问题（极端大额行、不完整周等），再解读业务
- ⚠️ 2011-W38 新客数变化 +56.10%，超过 50%：请确认报告已先排查数据问题（极端大额行、不完整周等），再解读业务

## 附录 B：找不到出处的数字

无。74 个数字都能在工具输出中找到出处。注意：这只说明数字不是编的，不保证每个数字被用在了正确的指标上。

## 附录 C：需要人工复核的表述

因果表述需要对照实验支持；绝对化或推测性的说法需要证据。请逐句判断：

| 类别 | 词 | 所在句子 |
|---|---|---|
| 因果表述 | 由于 | …–67 人（metric_summary），获客投入未掉；但由于真实回访率不可得，**不建议**用 108.02% 做任何考… |
| 绝对化或无依据的推测 | 必然 | …ality_overview，规则 R08），客户级复购口径必然低估。
- **单周客户样本小**（活跃客户 225–441… |

## 附录 D：工具调用

| 轮 | 工具 | 参数 | 结果 |
|---:|---|---|---|
| 1 | load_skill | `{"name": "ecommerce-metric-diagnosis"}` | 成功 |
| 1 | data_quality_overview | `{}` | 成功 |
| 2 | metric_summary | `{"week": "2011-W35", "compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"]}` | 成功 |
| 2 | metric_summary | `{"week": "2011-W36", "compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"]}` | 成功 |
| 2 | metric_summary | `{"week": "2011-W37", "compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"]}` | 成功 |
| 2 | metric_summary | `{"week": "2011-W38", "compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"]}` | 成功 |
| 2 | metric_summary | `{"week": "2011-W39", "compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"]}` | 成功 |
| 3 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"], "week": "2011-W40"}` | 成功 |
| 3 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"], "week": "2011-W41"}` | 成功 |
| 3 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"], "week": "2011-W42"}` | 成功 |
| 3 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"], "week": "2011-W43"}` | 成功 |
| 3 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "trading_days", "orders"], "week": "2011-W44"}` | 成功 |
| 3 | drilldown | `{"compare": "wow", "dimension": "customer_type", "metric": "gmv", "top_n": 5, "week": "2011-W43"}` | 成功 |
| 4 | calculate | `{"expression": "(322+311+317+370)", "purpose": "10月（10/3–10/30，W40–W43）\"回来购买\"的老客人次合计"}` | 成功 |
| 4 | calculate | `{"expression": "1320/(225+313+301+383)", "purpose": "10月老客人次 ÷ 9月（9/1–9/25，W35–W38）活跃客户人次"}` | 成功 |

## 附录 E：工具警告汇总

- 交易天数不同：当期 2011-W35 5 天，基期 2011-W34 6 天。GMV、订单数等总量的变化有一部分来自天数差异，请同时比较日均：日均 GMV 26,820.28 → 29,311.41（+9.29%）。
- 交易天数不同：当期 2011-W36 6 天，基期 2011-W35 5 天。GMV、订单数等总量的变化有一部分来自天数差异，请同时比较日均：日均 GMV 29,311.41 → 31,193.72（+6.42%）。
- 有分组反向变化，相互抵消后总变化较小，所以单个分组的贡献占比会超过 100% 或为负。
- 部分分组订单数少于 30，变化可能是偶然波动，不宜单独下结论。

## 附录 F：运行信息

- 运行编号 `20261117T010557-cd1227`，状态 completed，模型 MiniMax-M3.1-Flash
- 调用模型 5 次，token 66787 + 22304，用时 169.64 秒
- 工具调用上限 15 次，本次已用完
