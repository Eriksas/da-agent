> **AI 初稿，待人工复核。** 数字核查：60/60 个能在工具输出中找到出处（另有 11 个 ≤ 10 的整数未核查）；用 calculate 计算 5 次，输入都有出处；需人工复核的表述 0 处。流程检查：已加载 ecommerce-metric-diagnosis，缺少必做步骤 decompose_gmv、drilldown。

**问题：** 2011 年 11 月，周日的客单价和工作日（周一到周五）比，哪个高？各是多少？

## 结论

**这个问题用现有工具答不出来**：`metric_summary` 只提供按 ISO 周的固定指标，`drilldown` 只支持 country / customer_type / product 三个维度，**都没有"星期几"粒度**，因此 11 月各**周日**的客单价、11 月**周一至周五**的客单价，**工具未提供**。

实测确认（工具报错原文）：
- `metric_summary(metrics=["aov_sunday","aov_weekday"])` → 参数不合法，允许的指标只有 gmv、net_sales、cancelled_amount、cancel_rate、orders、aov、active_customers、new_customers、returning_customers、orders_per_customer、identified_aov、unidentified_gmv、trading_days、gmv_per_day、orders_per_day；
- `drilldown(dimension="weekday")` → 参数不合法：dimension 只能是 country、customer_type、product。

下面给出**工具能支持的最接近口径**：2011 年 11 月各整周的客单价，以及一条与"星期几"相关的可用线索。

## 能拿到的数字（均来自 metric_summary / calculate）

11 月跨 5 个 ISO 周（W44 尾部 → W48 头部；周为周一起算，见 data_quality_overview 的 "monday" 字段）：

| 周 | 覆盖日期 | GMV | 订单数 | 客单价 | 交易天数 | 客单价环比（change_pct 为小数形式） |
|---|---|---|---|---|---|---|
| 2011-W44 | 10/31–11/6（含 11/1–11/6） | 292,747.79 | 527 | 555.50 | 6 | +18.80%（0.188） |
| 2011-W45 | 11/7–11/13 | 357,706.72 | 634 | 564.21 | 6 | +1.57%（0.0157） |
| 2011-W46 | 11/14–11/20 | 373,178.36 | 687 | 543.20 | 6 | −3.72%（−0.0372） |
| 2011-W47 | 11/21–11/27 | 302,392.22 | 616 | 490.90 | 6 | −9.63%（−0.0963） |
| 2011-W48 | 11/28–12/4（含 11/28–11/30） | 309,381.73 | 663 | 466.64 | 6 | −4.94%（−0.0494） |

合计口径（均为 calculate 计算结果）：
- **完整落在 11 月内的三周（W45–W47）**：合计客单价 = **533.44**，其中订单数合计 = **1,937**（calculate：634 + 687 + 616）。
- **11/1–11/27（W44–W47）**：合计客单价 = **538.16**，其中 GMV 合计 = **1,326,025.09**（calculate：292,747.79 + 357,706.72 + 373,178.36 + 302,392.22）、订单数合计 = **2,464**（calculate：527 + 634 + 687 + 616）。

与"星期几"唯一相关的可用线索：**11 月涉及的每一周交易天数都是 6 天**（7 个自然日中有 1 天完全没有成交，metric_summary 的 trading_days），说明存在固定的休息日；但**工具未提供**该休息日是星期几，也未提供无成交日的明细，因此无法判断周日是否有营业、周日订单是多是少。

## 需要注意

- **口径不可直拼**：W44 含 10/31、W48 含 12/1–12/4，工具只到周粒度，严格的"11 月整月"客单价无法精确还原；上面 533.44 与 538.16 分别对应"完整三周"和"11/1–11/27"两种口径，不能互相替换。
- **样本量**：W44–W48 的订单数分别为 527 / 634 / 687 / 616 / 663，均远高于 30，不属于小样本问题；metric_summary 对这 5 周返回的 warnings 均为空（无极端大额行、不完整周提示）。
- **数据范围**：2009-12-01 07:45 至 2011-12-09 12:50；2011-W49 为不完整周（数据只到周五中午），不影响 11 月各周。
- **相关不等于因果**：11 月末客单价连续走低（W47 −9.63%、W48 −4.94%）是否与"周日/工作日结构"有关，现在无法判断，不下结论。

## 建议下一步

1. **要回答原问题，需要日粒度底表**：按 `invoice_date` 聚合出「日期、GMV、发票数」，即可直接算出周日与周一至周五各自的客单价；同时请数据同学确认固定不营业日是星期几（可解释"每周恰好 6 个交易日"）。
2. **工具侧改造**：给 `drilldown` 增加 `weekday` 维度、给 `metric_summary` 增加按日聚合接口，这类"按星期几"的常规问题以后可自助回答。
3. **若目的是排查 11 月末客单价走低的周报**：可走异动诊断流程（decompose_gmv + drilldown 的国家/新老客/商品维度），这几项数据现在就能取。

**可信度自评：说明局限后分享。** 周级 GMV / 订单数 / 客单价均为工具原始数据，可直接使用；但"周日 vs 工作日"这一问在现有工具下无法回答，只能给出周级替代口径并说明差距。

---

## 附录 A：流程检查与危险信号

- 流程 `ecommerce-metric-diagnosis`：必做步骤 metric_summary、decompose_gmv、drilldown；**缺少 decompose_gmv、drilldown**
- 没有变化超过 50% 的核心指标。

## 附录 B：找不到出处的数字

初稿有 4 个数字找不到出处（1,033,277.30、1,937、1,326,025.09、2,464），已退回模型修正 1 次。上面是修正后的版本，下面是对它的核查。

无。60 个数字都能在工具输出中找到出处。注意：这只说明数字不是编的，不保证每个数字被用在了正确的指标上。

## 附录 C：需要人工复核的表述

未发现因果表述或绝对化说法。推理是否成立仍需人工判断。

## 附录 D：工具调用

| 轮 | 工具 | 参数 | 结果 |
|---:|---|---|---|
| 1 | metric_summary | `{"week": "2011-W48", "compare": "wow", "metrics": ["gmv", "net_sales", "orders", "aov", "trading_days", "gmv_per_day", "orders_per_day", "active_customers", "orders_per_customer", "identified_aov", "unidentified_gmv"]}` | 成功 |
| 1 | data_quality_overview | `{}` | 成功 |
| 2 | load_skill | `{"name": "ecommerce-metric-diagnosis"}` | 成功 |
| 3 | drilldown | `{"week": "2011-W47", "compare": "wow", "dimension": "weekday", "metric": "gmv", "top_n": 10}` | 失败：参数不合法：dimension: Input should be 'country', 'customer_type' or 'product' |
| 3 | metric_summary | `{"week": "2011-W44", "compare": "wow", "metrics": ["gmv", "orders", "aov", "trading_days"]}` | 成功 |
| 3 | metric_summary | `{"week": "2011-W45", "compare": "wow", "metrics": ["gmv", "orders", "aov", "trading_days"]}` | 成功 |
| 3 | metric_summary | `{"week": "2011-W46", "compare": "wow", "metrics": ["gmv", "orders", "aov", "trading_days"]}` | 成功 |
| 3 | metric_summary | `{"week": "2011-W47", "compare": "wow", "metrics": ["gmv", "orders", "aov", "trading_days"]}` | 成功 |
| 4 | calculate | `{"expression": "(357706.72 + 373178.36 + 302392.22) / (634 + 687 + 616)", "purpose": "11 月内完整三周（2011-W45~W47，对应 11/7-11/27）合计 GMV ÷ 合计订单数的客单价"}` | 成功 |
| 4 | calculate | `{"expression": "(292747.79 + 357706.72 + 373178.36 + 302392.22) / (527 + 634 + 687 + 616)", "purpose": "2011-W44~W47（11/1-11/27）四周合计 GMV ÷ 合计订单数的客单价"}` | 成功 |
| 4 | metric_summary | `{"compare": "wow", "metrics": ["aov_sunday", "aov_weekday"], "week": "2011-W47"}` | 失败：参数不合法：metrics.0: Input should be 'gmv', 'net_sales', 'cancelled_amount', 'cancel_rate', 'orders', 'aov', 'active_customers', 'new_customers', 'returning_customers', 'orders_per_customer', 'identified_aov', 'unidentified_gmv', 'trading_days', 'gmv_per_day' or 'orders_per_day'；metrics.1: Input should be 'gmv', 'net_sales', 'cancelled_amount', 'cancel_rate', 'orders', 'aov', 'active_customers', 'new_customers', 'returning_customers', 'orders_per_customer', 'identified_aov', 'unidentified_gmv', 'trading_days', 'gmv_per_day' or 'orders_per_day' |
| 6 | calculate | `{"expression": "(292747.79 + 357706.72 + 373178.36 + 302392.22)", "purpose": "2011-W44~W47（11/1-11/27）四周 GMV 合计"}` | 成功 |
| 6 | calculate | `{"expression": "(634 + 687 + 616)", "purpose": "2011-W45~W47 三周订单数合计"}` | 成功 |
| 6 | calculate | `{"expression": "(527 + 634 + 687 + 616)", "purpose": "2011-W44~W47 四周订单数合计"}` | 成功 |

## 附录 E：工具警告汇总

- 无

## 附录 F：运行信息

- 运行编号 `20261117T192600-a4987a`，状态 completed，模型 MiniMax-M3.1-Flash
- 调用模型 7 次，token 77511 + 13612，用时 66.21 秒
- 工具调用上限 15 次，本次已用完
