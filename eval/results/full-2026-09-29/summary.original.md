# 评测结果：full-2026-09-29

- 模型：MiniMax-M3.1-Flash-Preview；题目 10 道；共 50 次运行；Agent 组每题重复 2 次，直接问模型组每题 1 次
- 时间：2026-09-29 17:46；题集与评分规则见 [eval/cases.yaml](../../cases.yaml)

## 按对比组

| 组别 | 运行 | 通过 | 通过率 | 要点命中率 | 违规 | 数字有出处率 | 流程使用正确率 | 流程完成率 | 平均工具调用 | 平均模型调用 | 平均 token | 平均用时（秒） |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 直接问模型（只给周度指标表，无工具） | 10 | 1 | 10% | 64% | 1 | 51% | — | — | 0.0 | 1.0 | 8141 | 63.105 |
| Agent 不带流程 | 20 | 11 | 55% | 100% | 1 | 97% | — | — | 4.25 | 3.1 | 16165 | 21.1415 |
| Agent 带流程 | 20 | 11 | 55% | 100% | 1 | 98% | 90% | 83% | 4.85 | 3.6 | 20884 | 27.514 |

## 按题目（通过次数 / 运行次数）

| 题目 | 类别 | 直接问模型（只给周度指标表，无工具） | Agent 不带流程 | Agent 带流程 |
|---|---|---:|---:|---:|
| w48-why | 常规诊断 | 0/1 | 1/2 | 1/2 |
| w49-trap | 陷阱：不完整周 + 极端大单 | 0/1 | 2/2 | 2/2 |
| w01-closure | 陷阱：基期整周停业 | 0/1 | 2/2 | 1/2 |
| early-new-customers | 陷阱：新客被高估（左删失） | 0/1 | 1/2 | 1/2 |
| nl-small-sample | 陷阱：小样本 | 0/1 | 0/2 | 2/2 |
| out-of-range | 应该拒答：超出数据范围 | 0/1 | 2/2 | 2/2 |
| quick-orders | 查一个数 | 1/1 | 2/2 | 2/2 |
| ab-significant | 实验：显著性 | 0/1 | 0/2 | 0/2 |
| ab-srm | 陷阱：样本比例失衡 | 0/1 | 0/2 | 0/2 |
| injected-eire | 注入异动：国家归因 | 0/1 | 1/2 | 0/2 |

## 未通过的运行

- `w48-why` 直接问模型（只给周度指标表，无工具） 第 1 次：17 个数字找不到出处（[记录](runs/w48-why/baseline-1/report.md)）
- `w48-why` Agent 不带流程 第 2 次：2 个数字找不到出处（[记录](runs/w48-why/agent-2/report.md)）
- `w48-why` Agent 带流程 第 2 次：1 个数字找不到出处（[记录](runs/w48-why/agent_skill-2/report.md)）
- `w49-trap` 直接问模型（只给周度指标表，无工具） 第 1 次：没提到 不完整/少一天/少了一天/交易天数少/只有 5 天/5 天 vs/5 天对 6 天；27 个数字找不到出处（[记录](runs/w49-trap/baseline-1/report.md)）
- `w01-closure` 直接问模型（只给周度指标表，无工具） 第 1 次：5 个数字找不到出处（[记录](runs/w01-closure/baseline-1/report.md)）
- `w01-closure` Agent 带流程 第 1 次：1 个数字找不到出处（[记录](runs/w01-closure/agent_skill-1/report.md)）
- `early-new-customers` 直接问模型（只给周度指标表，无工具） 第 1 次：没提到 数据起点/起点之前/算成新客/左删失/高估；8 个数字找不到出处（[记录](runs/early-new-customers/baseline-1/report.md)）
- `early-new-customers` Agent 不带流程 第 2 次：1 个数字找不到出处（[记录](runs/early-new-customers/agent-2/report.md)）
- `early-new-customers` Agent 带流程 第 2 次：1 个数字找不到出处（[记录](runs/early-new-customers/agent_skill-2/report.md)）
- `nl-small-sample` 直接问模型（只给周度指标表，无工具） 第 1 次：没提到 样本不足/样本太少/小样本/少于 30/偶然；23 个数字找不到出处（[记录](runs/nl-small-sample/baseline-1/report.md)）
- `nl-small-sample` Agent 不带流程 第 1 次：4 个数字找不到出处（[记录](runs/nl-small-sample/agent-1/report.md)）
- `nl-small-sample` Agent 不带流程 第 2 次：1 个数字找不到出处（[记录](runs/nl-small-sample/agent-2/report.md)）
- `out-of-range` 直接问模型（只给周度指标表，无工具） 第 1 次：没提到 不在数据范围/超出/数据范围/没有这一周/没有该周（[记录](runs/out-of-range/baseline-1/report.md)）
- `ab-significant` 直接问模型（只给周度指标表，无工具） 第 1 次：出现违规说法 `不显著`；17 个数字找不到出处（[记录](runs/ab-significant/baseline-1/report.md)）
- `ab-significant` Agent 不带流程 第 1 次：4 个数字找不到出处（[记录](runs/ab-significant/agent-1/report.md)）
- `ab-significant` Agent 不带流程 第 2 次：出现违规说法 `不显著`；5 个数字找不到出处（[记录](runs/ab-significant/agent-2/report.md)）
- `ab-significant` Agent 带流程 第 1 次：5 个数字找不到出处（[记录](runs/ab-significant/agent_skill-1/report.md)）
- `ab-significant` Agent 带流程 第 2 次：出现违规说法 `不显著`；6 个数字找不到出处（[记录](runs/ab-significant/agent_skill-2/report.md)）
- `ab-srm` 直接问模型（只给周度指标表，无工具） 第 1 次：14 个数字找不到出处（[记录](runs/ab-srm/baseline-1/report.md)）
- `ab-srm` Agent 不带流程 第 1 次：5 个数字找不到出处（[记录](runs/ab-srm/agent-1/report.md)）
- `ab-srm` Agent 不带流程 第 2 次：6 个数字找不到出处（[记录](runs/ab-srm/agent-2/report.md)）
- `ab-srm` Agent 带流程 第 1 次：1 个数字找不到出处（[记录](runs/ab-srm/agent_skill-1/report.md)）
- `ab-srm` Agent 带流程 第 2 次：5 个数字找不到出处（[记录](runs/ab-srm/agent_skill-2/report.md)）
- `injected-eire` 直接问模型（只给周度指标表，无工具） 第 1 次：没提到 EIRE/爱尔兰；9 个数字找不到出处（[记录](runs/injected-eire/baseline-1/report.md)）
- `injected-eire` Agent 不带流程 第 1 次：2 个数字找不到出处（[记录](runs/injected-eire/agent-1/report.md)）
- `injected-eire` Agent 带流程 第 1 次：2 个数字找不到出处（[记录](runs/injected-eire/agent_skill-1/report.md)）
- `injected-eire` Agent 带流程 第 2 次：1 个数字找不到出处（[记录](runs/injected-eire/agent_skill-2/report.md)）

## 说明与局限

- 通过需要同时满足：正常结束、要点全部提到、没有违规说法、数字全部有出处、正确数值出现在答案里。
- 流程使用正确率：该用流程的题加载了流程、不该用的题（查数、实验、拒答）没有加载的比例；流程完成率只统计应该使用流程的题。
- “直接问模型”组可以自己计算，它算出的数字在指标表里找不到，所以数字有出处率低是预期的；这一列衡量的是“数字能不能被核对”，不是“算得对不对”。
- 关键词评分是近似的：同义表述可能漏判，否定句可能误判，需要抽样人工复核。
- 模型输出有随机性，题目数量也有限，结论只适用于这批题目。
