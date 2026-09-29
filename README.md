# da-agent

**运营周报与指标异动分析 Agent。** 给一份运营数据和一个业务问题（如“上周 GMV 为什么下降？”），模型负责选择分析工具，Python 负责计算每一个数字，最终报告里的数字都能回查到工具输出。

> 当前进度：**M3 Agent 循环**已完成。报告的数字核查（M4）和评测（M5）尚未实现，完整规划见 [PROJECT_BRIEF.md](PROJECT_BRIEF.md)。

## 进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 项目骨架、配置、假模型、CI | ✅ |
| M1 | 数据加载与质量检查 | ✅ |
| M2 | 确定性分析工具（指标、GMV 拆解、维度下钻、实验检验） | ✅ |
| M3 | Agent 循环（工具调用） | ✅ |
| M3b | 用 LangGraph 重写同一循环做对照（可选） | 未开始 |
| M4 | 报告与数字核查 | 未开始 |
| M5 | 评测集与基线对比 | 未开始 |
| M6 | GitHub Actions 自动周报 | 未开始 |

## 本地运行

需要 Python 3.13 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                          # 安装依赖
uv run da-agent doctor           # 查看配置状态（不调用模型，不打印密钥）
uv run da-agent demo             # 离线演示：假模型剧本在内置小样例上走完整个 Agent 循环，不需要密钥
uv run pytest                    # 运行测试

uv run da-agent prepare-data     # 从 UCI 下载数据（约 44MB）并转成 parquet，首次约需 3 分钟
uv run da-agent quality          # 生成数据质量报告 reports/data_quality.md
uv run da-agent week 2011-W48    # 不经过模型，输出某一周的指标、GMV 拆解和下钻（--compare yoy 为同比）

uv run da-agent doctor --ping    # 发 1 次真实请求，确认模型地址、名称和密钥可用
uv run da-agent ask "上周 GMV 为什么变化？"  # 用真实模型回答，运行记录存到 runs/
```

## 数据质量（M1 产出）

[数据质量报告](reports/data_quality.md)由 `da-agent quality` 生成，所有数字由 SQL 计算。清洗规则共 10 条，每条都写明理由、处理方式和影响行数，定义在 [cleaning.py](src/da_agent/cleaning.py)。探查中的主要发现：

- 原始文件的两个工作表时间重叠，重叠期记录逐行完全相同，直接拼接会重复计算。
- 非商品编码（运费、平台费、人工调整、测试记录、礼品卡）按业务含义逐个归类；格式特殊但确实是商品的编码予以保留。
- 缺客户 ID 的订单计入 GMV，但不计入客户类指标。
- 极端大额行进入人工复核清单，并自动识别“等量取消”和“人工调整冲销”两种冲销方式。
- 按完整周日历检查覆盖情况，标出不完整周和整周停业的周。

规则的正确性由两类测试保证：手算小样例（CI 中运行），以及用 pandas 独立重写规则、与 SQL 结果交叉核对（本地有数据时运行）。

## 分析工具（M2 产出）

Agent 在 M3 中调用的就是这些确定性函数，数字全部由 SQL 和 Python 计算：

| 工具 | 回答的问题 | 代码 |
|---|---|---|
| `metric_summary` | 某周的 GMV、订单、客单价、新老客、取消率等，与上周（环比）或去年同周（同比）比变了多少 | [metrics.py](src/da_agent/metrics.py) |
| `decompose_gmv` | GMV 的变化来自客户数、人均订单数还是客单价（连环替代法 + Shapley 分解） | [decompose.py](src/da_agent/decompose.py) |
| `drilldown` | 变化主要来自哪个国家、新客还是老客、哪个商品 | [metrics.py](src/da_agent/metrics.py) |
| `proportion_test` 等 | AB 实验的显著性、置信区间、所需样本量、样本比例失衡（SRM）检查 | [abtest.py](src/da_agent/abtest.py) |

设计要点：

- **每个结果自带口径和警告**：不完整周、整周停业、节假日、新老客划分的左删失、含极端大额订单的周，都会在结果里写明。例如 2011-W49 的 GMV 环比大幅上涨、净销售额却几乎不变，工具会提示这一周不完整，且含一笔当天被取消的超大订单。
- **比率类指标只报百分点变化**，不报容易误读的“变化百分比”。
- **连环替代法的顺序问题**：同时给出默认顺序的结果、6 种顺序的范围，以及与顺序无关的 Shapley 分解。
- **工具会拒绝不成立的计算**：例如基期整周停业时不做拆解，同比找不到对应的周时直接报错。

测试：手算小样例覆盖每个工具；AB 检验另用 scipy 原始数据检验对照、用蒙特卡洛模拟验证样本量公式；真实数据上检查恒等式（各周之和等于总数、每周拆解之和等于总变化）。

## Agent 循环（M3 产出）

Agent 就是一个循环：把问题和工具说明书发给模型 → 模型回复“请调用某个工具” → 程序执行工具、把结果放回对话 → 再发给模型……直到模型不再要工具、直接作答。模型负责决定下一步做什么，程序负责执行和约束。代码见 [agent.py](src/da_agent/agent.py)、[tools.py](src/da_agent/tools.py)、[llm.py](src/da_agent/llm.py)，系统提示词见 [prompts/agent_system.md](prompts/agent_system.md)。

- **工具只读**：6 个工具只查询和计算，不能改数据、不能写文件，也不能执行模型生成的代码。
- **参数先校验**：参数格式用 pydantic 定义，同一份定义生成给模型看的说明书，也在执行前校验。参数不合法、工具拒绝计算、工具内部出错，都会把原因交还给模型，让它改正或说明，程序不会崩溃。
- **四道闸**：工具调用次数上限；轮数上限；模型发出的每个工具请求都有对应回复（接口要求）；模型调用失败时以明确状态结束。
- **运行记录**：每次运行在 `runs/` 下保存完整对话（`run.json`）和答案（`answer.md`），可以逐步复盘，不含密钥。
- **模型接入**：OpenAI 兼容接口，默认 MiniMax。推理模型夹在正文里的思考过程会从答案中剥离，但在对话历史里原样保留，以保证多轮推理连贯。

测试全部使用假模型，覆盖各种“不按剧本走”的情况：参数写错后改正、调用不存在的工具、超出次数上限、模型一直要工具、模型调用失败、空答案。

## 配置模型（可选）

默认使用 MiniMax 的 OpenAI 兼容接口。复制 `.env.example` 为 `.env`，填入自己的 `LLM_API_KEY`。

- `.env` 已被 `.gitignore` 忽略，**不会被提交**。
- 代码只从环境变量或 `.env` 读取密钥，日志和报告里只显示“已配置/未配置”。
- CI 每次运行都会扫描仓库，发现疑似密钥会直接失败。

## 部署到 GitHub Actions

- **CI**（[ci.yml](.github/workflows/ci.yml)）：每次 push 自动运行测试和假模型示例，不需要密钥。
- **自动周报**：M6 实现，届时补充 Secrets 配置、手动触发和查看产物的步骤。

## 数据与致谢

- 数据：[UCI Online Retail II](https://archive.ics.uci.edu/dataset/502/online+retail+ii)，许可证 CC BY 4.0。原始文件不提交到本仓库。
- 灵感来源：[li-xiu-qi/data_analysis_agent](https://github.com/li-xiu-qi/data_analysis_agent)（MIT）的“自然语言需求 → 分析 → 报告”工作流。本项目从零实现，架构不同：模型只能调用预先写好并经过测试的分析工具，不执行模型生成的代码。

## 许可证

[MIT](LICENSE)
