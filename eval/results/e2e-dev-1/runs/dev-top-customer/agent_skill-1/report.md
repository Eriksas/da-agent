> **AI 初稿，待人工复核。** 数字核查：39/39 个能在工具输出中找到出处（另有 1 个 ≤ 10 的整数未核查）；用 calculate 计算 4 次，其中 1 次的输入找不到出处；需人工复核的表述 0 处。流程检查：已加载 ecommerce-metric-diagnosis，必做步骤全部完成。

**问题：** 2011 年下单次数最多的客户是谁？下了多少单？

## 结论

**这个问题用当前工具集回答不了**：现有工具没有"客户 ID 维度的排行"能力——我已实际验证：`drilldown` 传入 `dimension="customer"` 会直接报错"dimension: Input should be 'country', 'customer_type' or 'product'"；`metric_summary` 和 `decompose_gmv` 只输出到"周"级别的汇总（活跃客户数、人均订单数等），不返回任何单个客户的名字和单数。

因此：**"2011 年下单次数最多的客户是谁、下了多少单"——工具未提供**。以下是能拿到的最接近的替代证据。

## 可得的相关数据（2011 年抽样周）

| 周 | 订单数 | 活跃客户数 | 人均订单数 | GMV |
|---|---|---|---|---|
| 2011-W04 | 290 | 243 | 1.0905 | 121,001.73 |
| 2011-W13 | 355 | 278 | 1.1439 | 186,550.75 |
| 2011-W22 | 259 | 212 | 1.1226 | 115,065.00 |
| 2011-W31 | 314 | 274 | 1.0985 | 167,894.34 |
| 2011-W40 | 488 | 389 | 1.2031 | 310,948.14 |
| 2011-W48 | 663 | 514 | 1.2276 | 309,381.73 |

- 上述抽样周的人均订单数平均 **1.1477 单/周**（calculate 计算），区间 1.0905–1.2276，说明**整体是"一人一周约一单"**，头部客户的多次下单是少数现象，不是平均行为的放大。
- 情景说明（非实际答案）：若某客户全年每一周都下单，按 2011-W48 的人均 1.2276 单/周推算，其年下单次数会远高于抽样周的客户平均水平；但这纯属假设情景，工具未提供任何真实客户的下单次数。

## 需要注意

- **口径缺口**：全量数据共有 **2922 笔订单缺少客户 ID**（data_quality_overview：clean_totals.orders_missing_customer），占全部 **39516 笔订单的 0.073945**（calculate 计算），这类订单无法归属到任何客户。其中 2011-W48 有 32 单（drilldown customer_type 的"未知"分组），占当周 663 单的 **4.8265%**（calculate 计算）。做"下单次数排行"时这批订单必须单列，否则会被摊到别人头上。
- **2011 年不是完整年度**：2011-W49 为不完整周（数据截止 2011-12-09 12:50）；2010-12-23 至 2011-01-04 停业 12 天、2011-04-21 至 2011-04-26 停业 5 天（data_quality_overview：partial_weeks、closures）。
- **抽样不是全量**：上表是我为节省调用次数选取的 2011 年代表周（2011-W04、W13、W22、W31、W40、W48），**不是 2011 年逐周的全量统计**，不能代表全年。
- **"下单次数"口径需先定义**：GMV 是下单口径（含之后被取消的订单），若要按净下单计，需排除 C 开头发票；取消/退货单（清洗规则 R04）已单独处理，不计入 GMV。
- 2011-W04 的基期 2011-W03 含极端大额行：发票 541431（MEDIUM CERAMIC TOP STORAGE JAR）金额 77,183.60，24 小时内被等额取消。不影响单数类指标，但影响金额解读。

## 建议下一步

要拿到"2011 年下单最多的客户"，需要绕过周度聚合工具，直接在清洗后的销售明细表上做客户级分组统计，建议口径如下：

- **取数逻辑**：按客户 ID 分组，对不同发票号计数（COUNT DISTINCT 发票号）；过滤条件为发票日期落在 2011 年、且不含 C 开头取消单（R04），数量与单价为正（R05、R06），非商品编码行（R03）与坏账单（R02）按清洗规则剔除；缺失客户 ID 的订单单列为"未知"组。
- **先确认口径**：是否计入后来被取消的订单（下单口径 vs 净下单口径）、是否排除缺客户 ID 的订单。
- **输出**：客户排行（订单数、GMV、客单价、活跃周数），并标出是否命中被标记的极端大额行（R09 共 38 笔，需人工复核）。
- 拿到名单后，可再用本套工具对齐其下单周，做复购节奏分析（逐周 `metric_summary` / `decompose_gmv`），验证其高单量属于"稳定高频复购"还是"少数几周集中下单"。

**可信度自评**：需修改后再分享——本文只给出周级抽样与口径说明，未能回答原始的客户排行问题，不能直接对外。

---

## 附录 A：流程检查与危险信号

- 流程 `ecommerce-metric-diagnosis`：必做步骤 metric_summary、decompose_gmv、drilldown；全部完成
- ⚠️ 2011-W40 GMV变化 +50.57%，超过 50%：请确认报告已先排查数据问题（极端大额行、不完整周等），再解读业务

## 附录 B：找不到出处的数字

初稿有 3 个数字找不到出处（63.8352、52、52），已退回模型修正 1 次。上面是修正后的版本，下面是对它的核查。

- calculate `1.2276 * 52`（情景测算：某客户若全年每周都下单，按 2011-W48 人均订单数推算的年下单次数）的输入 52 找不到出处，计算结果不算出处。

无。39 个数字都能在工具输出中找到出处。注意：这只说明数字不是编的，不保证每个数字被用在了正确的指标上。

## 附录 C：需要人工复核的表述

未发现因果表述或绝对化说法。推理是否成立仍需人工判断。

## 附录 D：工具调用

| 轮 | 工具 | 参数 | 结果 |
|---:|---|---|---|
| 1 | load_skill | `{"name": "ecommerce-metric-diagnosis"}` | 成功 |
| 1 | data_quality_overview | `{}` | 成功 |
| 2 | drilldown | `{"week": "2011-W48", "compare": "yoy", "metric": "gmv", "dimension": "customer", "top_n": 20}` | 失败：参数不合法：dimension: Input should be 'country', 'customer_type' or 'product' |
| 2 | drilldown | `{"week": "2011-W48", "compare": "wow", "metric": "gmv", "dimension": "customer_type", "top_n": 10}` | 成功 |
| 3 | decompose_gmv | `{"week": "2011-W48", "compare": "wow"}` | 成功 |
| 3 | metric_summary | `{"week": "2011-W48", "compare": "yoy", "metrics": ["gmv", "orders", "active_customers", "orders_per_customer", "aov", "unidentified_gmv"]}` | 成功 |
| 4 | metric_summary | `{"compare": "wow", "metrics": ["orders", "active_customers", "orders_per_customer", "gmv"], "week": "2011-W04"}` | 成功 |
| 4 | metric_summary | `{"compare": "wow", "metrics": ["orders", "active_customers", "orders_per_customer", "gmv"], "week": "2011-W13"}` | 成功 |
| 4 | metric_summary | `{"compare": "wow", "metrics": ["orders", "active_customers", "orders_per_customer", "gmv"], "week": "2011-W22"}` | 成功 |
| 4 | metric_summary | `{"compare": "wow", "metrics": ["orders", "active_customers", "orders_per_customer", "gmv"], "week": "2011-W31"}` | 成功 |
| 4 | metric_summary | `{"compare": "wow", "metrics": ["orders", "active_customers", "orders_per_customer", "gmv"], "week": "2011-W40"}` | 成功 |
| 5 | calculate | `{"expression": "(1.0905 + 1.1439 + 1.1226 + 1.0985 + 1.2031 + 1.2276) / 6", "purpose": "2011 年抽样 6 周的人均订单数平均值"}` | 成功 |
| 5 | calculate | `{"expression": "32 / 663", "purpose": "2011-W48 缺客户 ID 的“未知”订单占当周总订单的比例"}` | 成功 |
| 5 | calculate | `{"expression": "1.2276 * 52", "purpose": "情景测算：某客户若全年每周都下单，按 2011-W48 人均订单数推算的年下单次数"}` | 成功 |
| 7 | calculate | `{"expression": "2922 / 39516", "purpose": "全量数据中缺客户 ID 的订单占全部订单的比例"}` | 成功 |

## 附录 E：工具警告汇总

- 有分组反向变化，相互抵消后总变化较小，所以单个分组的贡献占比会超过 100% 或为负。
- 部分分组订单数少于 30，变化可能是偶然波动，不宜单独下结论。
- 基期 2011-W03 含极端大额行：发票 541431（MEDIUM CERAMIC TOP STORAGE JAR）金额 77,183.60，24 小时内被等量取消（GMV 含它，净销售额已扣除）。
- 基期 2011-W03 含极端大额行：发票 C541433（MEDIUM CERAMIC TOP STORAGE JAR）金额 -77,183.60。
- 交易天数不同：当期 2011-W22 5 天，基期 2011-W21 6 天。GMV、订单数等总量的变化有一部分来自天数差异，请同时比较日均：日均 GMV 26,577.30 → 23,013.00（-13.41%）。

## 附录 F：运行信息

- 运行编号 `20261117T002917-afecbb`，状态 completed，模型 MiniMax-M3.1-Flash
- 调用模型 8 次，token 76665 + 8237，用时 46.94 秒
- 工具调用上限 15 次
