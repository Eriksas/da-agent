# da-agent

**运营周报与指标异动分析 Agent。** 给一份运营数据和一个业务问题（如“上周 GMV 为什么下降？”），模型负责选择分析工具，Python 负责计算每一个数字，最终报告里的数字都能回查到工具输出。

> 当前进度：**M0 项目骨架**。数据层、分析工具、Agent 循环尚未实现，完整规划见 [PROJECT_BRIEF.md](PROJECT_BRIEF.md)。

## 进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 项目骨架、配置、假模型、CI | ✅ |
| M1 | 数据加载与质量检查 | 未开始 |
| M2 | 确定性分析工具（指标、GMV 拆解、维度下钻、实验检验） | 未开始 |
| M3 | Agent 循环（工具调用） | 未开始 |
| M3b | 用 LangGraph 重写同一循环做对照（可选） | 未开始 |
| M4 | 报告与数字核查 | 未开始 |
| M5 | 评测集与基线对比 | 未开始 |
| M6 | GitHub Actions 自动周报 | 未开始 |

## 本地运行

需要 Python 3.13 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                          # 安装依赖
uv run da-agent doctor           # 查看配置状态（不调用模型，不打印密钥）
uv run da-agent demo --llm fake  # 用假模型跑示例，不需要密钥
uv run pytest                    # 运行测试
```

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
