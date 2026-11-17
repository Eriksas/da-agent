# 评测结果：e2e-2026-11-17-new

- 模型：MiniMax-M3.1-Flash；题目 10 道；共 40 次运行；Agent 组每题重复 2 次，直接问模型组每题 1 次
- 时间：2026-11-17 19:50；题集与评分规则见 [eval/query_e2e.yaml](../../query_e2e.yaml)

## 按对比组

| 组别 | 运行 | 通过 | 通过率 | 要点命中率 | 违规 | 数字有出处率 | 流程使用正确率 | 流程完成率 | 平均工具调用 | 平均模型调用 | 平均 token | 平均用时（秒） |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Agent 带流程 | 20 | 4 | 20% | 88% | 0 | 98% | 20% | — | 15.6 | 6.85 | 123457 | 191.306 |
| Agent 带流程 + 取数（query_data） | 20 | 15 | 75% | 100% | 1 | 99% | 85% | — | 11.45 | 5.65 | 136045 | 115.8755 |

## 核查后退回修正

答案里有找不到出处的数字时，程序把这些数字退回模型修正一次。同一次运行，初稿和修正后各打一次分：

| 组别 | 运行 | 被退回 | 修正前通过 | 修正后通过 |
|---|---:|---:|---:|---:|
| Agent 带流程 | 20 | 13 | 1 | 4 |
| Agent 带流程 + 取数（query_data） | 20 | 16 | 4 | 15 |

## 取数（query_data）

token 含嵌套的查询 Agent。

| 组别 | 运行 | 用到取数的运行 | 平均取数次数 | 平均取数 token | 模型接口报错后重跑 |
|---|---:|---:|---:|---:|---:|
| Agent 带流程 | 20 | 0 | 0.0 | 0 | 0 |
| Agent 带流程 + 取数（query_data） | 20 | 20 | 2.05 | 78618 | 0 |

## 按题目（通过次数 / 运行次数）

| 题目 | 类别 | Agent 带流程 | Agent 带流程 + 取数（query_data） |
|---|---|---:|---:|
| e-peak-hour | 取数：按小时 | 0/2 | 2/2 |
| e-q4-vs-q3 | 取数：按季度（陷阱：最后一个季度不完整） | 0/2 | 2/2 |
| e-retention | 取数：复购 | 0/2 | 2/2 |
| e-top-cancelled-product | 取数：商品排名（陷阱：当天下单当天全部取消的极端大单） | 2/2 | 2/2 |
| e-top-customers | 取数：客户集中度 | 0/2 | 1/2 |
| e-new-customers-2011 | 取数：新客 | 0/2 | 2/2 |
| e-sunday-aov | 取数：按星期几 | 0/2 | 0/2 |
| e-germany-quarters | 取数：某国按季度 | 0/2 | 2/2 |
| e-overseas-october | 取数：按月、海外 | 0/2 | 1/2 |
| e-margin | 拒答：数据里没有成本 | 2/2 | 1/2 |

## 未通过的运行

- `e-peak-hour` Agent 带流程 第 1 次：没提到 12 点/12点/12 时/12时/12:00/中午；没有给出正确数值 1,716.0（[记录](runs/e-peak-hour/agent_skill-1/report.md)）
- `e-peak-hour` Agent 带流程 第 2 次：没有给出正确数值 1,716.0（[记录](runs/e-peak-hour/agent_skill-2/report.md)）
- `e-q4-vs-q3` Agent 带流程 第 1 次：没有给出正确数值 2,440,756.222、3,169,921.15、729,164.928（[记录](runs/e-q4-vs-q3/agent_skill-1/report.md)）
- `e-q4-vs-q3` Agent 带流程 第 2 次：8 个数字找不到出处；没有给出正确数值 2,440,756.222、3,169,921.15、729,164.928（[记录](runs/e-q4-vs-q3/agent_skill-2/report.md)）
- `e-retention` Agent 带流程 第 1 次：没有给出正确数值 1,259.0、487.0、0.386815（[记录](runs/e-retention/agent_skill-1/report.md)）
- `e-retention` Agent 带流程 第 2 次：没有给出正确数值 1,259.0、487.0、0.386815（[记录](runs/e-retention/agent_skill-2/report.md)）
- `e-top-customers` Agent 带流程 第 1 次：没有给出正确数值 1,457,769.64、0.15391（[记录](runs/e-top-customers/agent_skill-1/report.md)）
- `e-top-customers` Agent 带流程 第 2 次：没有给出正确数值 1,457,769.64、0.15391（[记录](runs/e-top-customers/agent_skill-2/report.md)）
- `e-top-customers` Agent 带流程 + 取数（query_data） 第 1 次：4 个数字找不到出处（[记录](runs/e-top-customers/agent_query-1/report.md)）
- `e-new-customers-2011` Agent 带流程 第 1 次：没有给出正确数值 1,537.0、1,652,562.711（[记录](runs/e-new-customers-2011/agent_skill-1/report.md)）
- `e-new-customers-2011` Agent 带流程 第 2 次：7 个数字找不到出处；没有给出正确数值 1,537.0、1,652,562.711（[记录](runs/e-new-customers-2011/agent_skill-2/report.md)）
- `e-sunday-aov` Agent 带流程 第 1 次：没有给出正确数值 370.381763、550.504736（[记录](runs/e-sunday-aov/agent_skill-1/report.md)）
- `e-sunday-aov` Agent 带流程 第 2 次：没有给出正确数值 370.381763、550.504736（[记录](runs/e-sunday-aov/agent_skill-2/report.md)）
- `e-sunday-aov` Agent 带流程 + 取数（query_data） 第 1 次：1 个数字找不到出处（[记录](runs/e-sunday-aov/agent_query-1/report.md)）
- `e-sunday-aov` Agent 带流程 + 取数（query_data） 第 2 次：1 个数字找不到出处（[记录](runs/e-sunday-aov/agent_query-2/report.md)）
- `e-germany-quarters` Agent 带流程 第 1 次：没有给出正确数值 36,984.58、46,590.84、48,929.54、59,632.45（[记录](runs/e-germany-quarters/agent_skill-1/report.md)）
- `e-germany-quarters` Agent 带流程 第 2 次：没有给出正确数值 36,984.58、46,590.84、48,929.54、59,632.45（[记录](runs/e-germany-quarters/agent_skill-2/report.md)）
- `e-overseas-october` Agent 带流程 第 1 次：没有给出正确数值 191,846.87、0.173882（[记录](runs/e-overseas-october/agent_skill-1/report.md)）
- `e-overseas-october` Agent 带流程 第 2 次：没有给出正确数值 191,846.87、0.173882（[记录](runs/e-overseas-october/agent_skill-2/report.md)）
- `e-overseas-october` Agent 带流程 + 取数（query_data） 第 1 次：3 个数字找不到出处（[记录](runs/e-overseas-october/agent_query-1/report.md)）
- `e-margin` Agent 带流程 + 取数（query_data） 第 2 次：出现违规说法 `毛利率(?:约|大概|大约|为|是|：|:|\s)*[0-9]`（[记录](runs/e-margin/agent_query-2/report.md)）

## 说明与局限

- 通过需要同时满足：正常结束、要点全部提到、没有违规说法、数字全部有出处、正确数值出现在答案里。
- 流程使用正确率：该用流程的题加载了流程、不该用的题（查数、实验、拒答）没有加载的比例；流程完成率只统计应该使用流程的题。
- “直接问模型”组可以自己计算，它算出的数字在指标表里找不到，所以数字有出处率低是预期的；这一列衡量的是“数字能不能被核对”，不是“算得对不对”。
- 关键词评分是近似的：同义表述可能漏判，否定句可能误判，需要抽样人工复核。
- 模型输出有随机性，题目数量也有限，结论只适用于这批题目。
