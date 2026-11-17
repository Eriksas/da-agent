> **AI 初稿，待人工复核。** 数字核查：60/60 个能在工具输出中找到出处（另有 41 个 ≤ 10 的整数未核查）；用 calculate 计算 3 次，输入都有出处；需人工复核的表述 0 处。流程检查：已加载 ecommerce-metric-diagnosis，缺少必做步骤 decompose_gmv、drilldown。

**问题：** 2011 年 9 月买过东西的客户，10 月还有多少人回来买？占多少？

## 结论

**分析窗口**：9 月 = 2011-W35 ~ W39，10 月 = 2011-W40 ~ W44（工具按 ISO 周统计）。

- **能直接答的部分**：10 月"回来买"（此前有购买记录）的人次为 **1,711 人次**，占 10 月活跃人次的 **86.72%**（来自 `calculate`，基于 `metric_summary` 各周 active_customers / returning_customers）。
- **不能精确答的部分**：您问的"9 月买过的客户，10 月回来多少人、占多少"是 **cohort 交叉口径**（9 月客户池 ∩ 10 月客户池），当前工具**不提供**这个交叉：`metric_summary` 只有周度的活跃/新客/老客计数，`drilldown` 的维度只有国家/新老客/商品，无法按客户 ID 做跨月去重。因此这个数字我只能给到边界与代理口径，不能给一个精确值。

## 能确定的数字（均来自工具）

| 指标 | 9 月（W35–W39） | 10 月（W40–W44） | 来源 |
|---|---|---|---|
| 活跃客户（周度） | 225 / 313 / 301 / 383 / 374 | 389 / 360 / 364 / 419 / 441 | metric_summary |
| 新客（周度） | 23 / 26 / 41 / 64 / 45 | 67 / 49 / 47 / 49 / 50 | metric_summary |
| 老客＝回来买（周度） | 202 / 287 / 260 / 319 / 329 | 322 / 311 / 317 / 370 / 391 | metric_summary |
| 9 月首次购买客户（跨周去重合计） | **199 人** | — | calculate（23+26+41+64+45） |
| 10 月"回来买"人次合计 | — | **1,711 人次**，占活跃人次 **86.72%** | calculate |

- "9 月买过的客户"里**唯一可精确去重**的子集是 199 名 9 月新客（首次购买按全历史判定，跨周天然不重复）。
- 10 月新客（67/49/47/49/50 人）按定义不可能在 9 月买过，不会污染"9 月客户回访"的分子。

## 边界（能推到什么程度）

- **9 月去重客户数**：工具未提供跨周去重的月度值。可确认下界为 **383 人**（单周最高活跃，W38）；上界是各周活跃数之和，因跨周重复严重，上界远高于真实值。→ **至少 383 人在 9 月买过**。
- **10 月"回来买"的去重人数**：下界 **391 人**（单周老客最高，W44），上界 **1,711 人次**（同一人跨周重复计数）。1,711 是**人次**不是人数。
- **9 月客户在 10 月老客中的占比**：**工具无法拆分**——10 月的 1,711 老客人次里同时包含 9 月客户、9 月之前的老客户、8 月客户等，没有任何字段能区分。

## 需要注意

1. **周与自然月不对齐**：W35 = 8/29–9/4（因此"9 月"含 8/29–8/31），W39 = 9/26–10/2（含 10/1–10/2），W44 = 10/31–11/6（含 11/1–11/6）。严格自然月 cohort 需按日期重算，工具只支持 ISO 周。
2. **metric_summary 警告**：W35 当期只有 **5 个交易日**（基期 W34 为 6 天），总量对比受天数差异影响。
3. **口径差异（最关键）**：工具里的"老客 = 活跃 − 新客"指**当周之前买过**，**不等于**"9 月买过"。用它当"9 月客户回访"会高估。
4. **缺客户 ID**：2,922 笔订单、GMV 占比 **13.11%** 不计入活跃客户/复购统计（清洗规则 R08），客户类指标系统性偏低，对外汇报需加口径说明。
5. **极端大额行**：清单中最近的是 2011-06-10 与 2011-12-09，本窗口（2011-09/10）无极端行，GMV 口径不受其影响；但客户留存与 GMV 变化不要混着讲。
6. **左删失**：2011-09 距数据起点（2009-12）约 21 个月，9 月新客被误判为老客的风险相对低。
7. **数据完整性**：数据范围 2009-12-01 ~ 2011-12-09，9/10 月不属不完整周（不完整周为 2009-W49、2011-W49），无停业周影响这两月。
8. 无对照实验，以上只作**事实陈述**，不作因果解释。

## 建议下一步

1. **要精确答案，需客户级 cohort 表**：导出 customer_id / order_date / amount，以 9/1–9/30 客户集合为分母，统计 30 日内是否再下单及复购 GMV。当前工具集（metric_summary / drilldown / decompose_gmv）均为周度聚合，做不到这一层。
2. **建议改用自然月切分**而不是 ISO 周，避免 W35/W44 跨月污染。
3. **用 net_sales 复核一次**：GMV 是下单口径（含之后被取消的订单），10 月的取消单可能对应 9 月订单，会直接影响"9 月客户在 10 月是否回来买东西"的判断。

## 可信度自评

**说明局限后分享**：回访人次 1,711 与占比 86.72% 可直接引用工具与 calculate；但"9 月客户 → 10 月回归"的**去重人数与复购率**超出当前工具口径，只能给上下界（9 月客户 ≥383 人；10 月回来买 ≥391 人），需要客户级数据才能定论。

---

## 附录 A：流程检查与危险信号

- 流程 `ecommerce-metric-diagnosis`：必做步骤 metric_summary、decompose_gmv、drilldown；**缺少 decompose_gmv、drilldown**
- ⚠️ 2011-W40 GMV变化 +50.57%，超过 50%：请确认报告已先排查数据问题（极端大额行、不完整周等），再解读业务
- ⚠️ 2011-W37 新客数变化 +57.69%，超过 50%：请确认报告已先排查数据问题（极端大额行、不完整周等），再解读业务
- ⚠️ 2011-W38 新客数变化 +56.10%，超过 50%：请确认报告已先排查数据问题（极端大额行、不完整周等），再解读业务

## 附录 B：找不到出处的数字

无。60 个数字都能在工具输出中找到出处。注意：这只说明数字不是编的，不保证每个数字被用在了正确的指标上。

## 附录 C：需要人工复核的表述

未发现因果表述或绝对化说法。推理是否成立仍需人工判断。

## 附录 D：工具调用

| 轮 | 工具 | 参数 | 结果 |
|---:|---|---|---|
| 1 | data_quality_overview | `{}` | 成功 |
| 1 | metric_summary | `{"week": "2011-W36", "compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv", "trading_days"]}` | 成功 |
| 1 | metric_summary | `{"week": "2011-W40", "compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv", "trading_days"]}` | 成功 |
| 2 | load_skill | `{"name": "ecommerce-metric-diagnosis"}` | 成功 |
| 2 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv"], "week": "2011-W35"}` | 成功 |
| 2 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv"], "week": "2011-W37"}` | 成功 |
| 2 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv"], "week": "2011-W38"}` | 成功 |
| 2 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv"], "week": "2011-W39"}` | 成功 |
| 2 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv"], "week": "2011-W41"}` | 成功 |
| 2 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv"], "week": "2011-W42"}` | 成功 |
| 2 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv"], "week": "2011-W43"}` | 成功 |
| 2 | metric_summary | `{"compare": "wow", "metrics": ["active_customers", "new_customers", "returning_customers", "orders", "gmv"], "week": "2011-W44"}` | 成功 |
| 3 | calculate | `{"expression": "(23+26+41+64+45)", "purpose": "9月（2011-W35~W39）首次购买客户合计：各周 new_customers 求和（首次购买按全历史判定，跨周天然去重）"}` | 成功 |
| 3 | calculate | `{"expression": "(389+360+364+419+441)-(67+49+47+49+50)", "purpose": "10月（2011-W40~W44）\"回来买\"的老客人周合计 = 活跃客户人周 − 新客人周"}` | 成功 |
| 3 | calculate | `{"expression": "(322+311+317+370+391)/(389+360+364+419+441)", "purpose": "10月老客人周占10月活跃客人周的比例"}` | 成功 |

## 附录 E：工具警告汇总

- 交易天数不同：当期 2011-W36 6 天，基期 2011-W35 5 天。GMV、订单数等总量的变化有一部分来自天数差异，请同时比较日均：日均 GMV 29,311.41 → 31,193.72（+6.42%）。
- 交易天数不同：当期 2011-W35 5 天，基期 2011-W34 6 天。GMV、订单数等总量的变化有一部分来自天数差异，请同时比较日均：日均 GMV 26,820.28 → 29,311.41（+9.29%）。

## 附录 F：运行信息

- 运行编号 `20261117T010252-7a9ae0`，状态 completed，模型 MiniMax-M3.1-Flash
- 调用模型 4 次，token 48451 + 21766，用时 184.92 秒
- 工具调用上限 15 次，本次已用完
