# 评测结果：holdout-before-2026-10-07

- 模型：MiniMax-M3.1-Flash-Preview；题目 5 道；共 20 次运行；Agent 组每题重复 2 次，直接问模型组每题 1 次
- 时间：2026-10-07 01:29；题集与评分规则见 [eval/holdout.yaml](../../holdout.yaml)

## 按对比组

| 组别 | 运行 | 通过 | 通过率 | 要点命中率 | 违规 | 数字有出处率 | 流程使用正确率 | 流程完成率 | 平均工具调用 | 平均模型调用 | 平均 token | 平均用时（秒） |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Agent 不带流程 | 10 | 6 | 60% | 100% | 0 | 99% | — | — | 6.3 | 3.5 | 22127 | 23.808 |
| Agent 带流程 | 10 | 5 | 50% | 100% | 0 | 99% | 100% | 100% | 7.6 | 5.3 | 36133 | 26.663 |

## 按题目（通过次数 / 运行次数）

| 题目 | 类别 | Agent 不带流程 | Agent 带流程 |
|---|---|---:|---:|
| w03-cancelled-order | 陷阱：被取消的极端大单（完整周） | 2/2 | 2/2 |
| w22-bank-holiday | 陷阱：交易天数不同（银行假日） | 2/2 | 1/2 |
| au-small-sample | 陷阱：小样本 | 0/2 | 0/2 |
| w41-why | 常规诊断 | 1/2 | 1/2 |
| ab-not-significant | 实验：不显著 | 1/2 | 1/2 |

## 未通过的运行

- `w22-bank-holiday` Agent 带流程 第 1 次：1 个数字找不到出处（[记录](runs/w22-bank-holiday/agent_skill-1/report.md)）
- `au-small-sample` Agent 不带流程 第 1 次：1 个数字找不到出处（[记录](runs/au-small-sample/agent-1/report.md)）
- `au-small-sample` Agent 不带流程 第 2 次：4 个数字找不到出处（[记录](runs/au-small-sample/agent-2/report.md)）
- `au-small-sample` Agent 带流程 第 1 次：4 个数字找不到出处（[记录](runs/au-small-sample/agent_skill-1/report.md)）
- `au-small-sample` Agent 带流程 第 2 次：2 个数字找不到出处（[记录](runs/au-small-sample/agent_skill-2/report.md)）
- `w41-why` Agent 不带流程 第 1 次：1 个数字找不到出处（[记录](runs/w41-why/agent-1/report.md)）
- `w41-why` Agent 带流程 第 2 次：1 个数字找不到出处（[记录](runs/w41-why/agent_skill-2/report.md)）
- `ab-not-significant` Agent 不带流程 第 2 次：2 个数字找不到出处（[记录](runs/ab-not-significant/agent-2/report.md)）
- `ab-not-significant` Agent 带流程 第 2 次：1 个数字找不到出处（[记录](runs/ab-not-significant/agent_skill-2/report.md)）

## 说明与局限

- 通过需要同时满足：正常结束、要点全部提到、没有违规说法、数字全部有出处、正确数值出现在答案里。
- 流程使用正确率：该用流程的题加载了流程、不该用的题（查数、实验、拒答）没有加载的比例；流程完成率只统计应该使用流程的题。
- “直接问模型”组可以自己计算，它算出的数字在指标表里找不到，所以数字有出处率低是预期的；这一列衡量的是“数字能不能被核对”，不是“算得对不对”。
- 关键词评分是近似的：同义表述可能漏判，否定句可能误判，需要抽样人工复核。
- 模型输出有随机性，题目数量也有限，结论只适用于这批题目。
