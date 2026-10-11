"""图形界面（M11）的逻辑部分：不依赖 Streamlit，可以单独测试。页面本身在 ui_app.py，用 `da-agent ui` 启动。

界面只在本机运行、只监听 127.0.0.1：实时提问会用你自己的密钥调用付费模型，放到公网谁都能消耗额度。
公开展示用 GitHub Pages 上的静态网站，界面是给演示用的。

- replay_events：从运行记录还原出 run_agent(on_step=...) 实时发出的事件，回放和实时提问用同一套显示代码 show_event
- to_streamlit_markdown：把答案转成 Streamlit 能安全显示的 Markdown，找不到出处的数字按核查器记录的位置标红
- saved_runs：可回放的运行，包括已提交的真实运行样例和本机 runs/ 下的记录
- ask：实时提问，运行记录、核查结果和报告和命令行一样存到 runs/
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import run_agent, save_run
from .config import Settings
from .llm import LLMClient
from .paths import RUNS_DIR
from .report import write_review
from .site import REAL_RUNS

REPLAY_DELAY = 0.5  # 逐步播放时每一步停留的秒数；测试里改成 0
DOMAINS = {"ecommerce": "电商运营（UCI 交易数据）", "company": "上市公司财报（SEC 年报）"}
EXAMPLE_QUESTIONS = {
    "ecommerce": ["上周 GMV 为什么变化？", "2011-W49 的 GMV 大涨，是好消息吗？", "2011-W48 荷兰的 GMV 为什么跌了这么多？",
                  "我们做了一个满减实验，按 50/50 分流：对照组 5000 人中 400 人下单，实验组 5000 人中 460 人下单。效果显著吗？"],
    "company": ["阿里巴巴最近一个财年的盈利能力怎么样？变化主要来自哪里？", "拼多多和京东相比，哪家的盈利能力和现金流质量更好？",
                "拼多多 2025 财年的 ROE 为什么下降了？"],
}
TOOL_LABELS = {
    "metric_summary": "指标对比", "decompose_gmv": "GMV 拆解", "drilldown": "维度下钻",
    "data_quality_overview": "数据质量概览", "load_skill": "加载分析流程", "ab_proportion_test": "AB 实验检验",
    "ab_sample_size": "AB 样本量", "calculate": "计算器", "company_overview": "公司概览",
    "financial_summary": "财务摘要", "dupont": "杜邦分析", "peer_compare": "同行对比",
}
# 代码块原样显示；其余部分里 $ 会被 Streamlit 当成公式、~ 会被当成删除线（“3~5 天”会被划掉），要转义
CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)
MARKDOWN_ESCAPES = str.maketrans({"$": r"\$", "~": r"\~"})
HEADING = re.compile(r"(?m)^#{1,4}(?=[ \t])")  # 答案里的标题降两级：模型常用一级标题，会比页面上的问题还大


def tool_label(name: str) -> str:
    return f"{TOOL_LABELS[name]}（{name}）" if name in TOOL_LABELS else name


def compact(arguments: dict[str, Any]) -> str:
    """工具参数的一行写法：week=2011-W48, dimension=country。"""
    return ", ".join(f"{k}={v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
                     for k, v in arguments.items()) or "无参数"


def show_event(box: Any, event: dict[str, Any], label_prefix: str | None = None) -> None:
    """在进度框里显示一步，实时提问和回放都用它。box 是 Streamlit 的 st.status，测试里换成记录调用的假对象。

    改标题时必须同时传 expanded=True：Streamlit 1.65 的 update 不传 expanded 会把进度框收起来，
    后面的步骤都藏在折叠里（第一次真实运行时发现）。
    """
    kind = event["type"]
    if kind == "model":
        if label_prefix:
            box.update(label=f"{label_prefix}：第 {event['round']} 轮，等模型决定下一步", expanded=True)
        box.caption(f"第 {event['round']} 轮 · 调用模型")
    elif kind == "tool":
        step = event["step"]
        line = (f"{'✅' if step['ok'] else '❌'} **{tool_label(step['tool'])}** · `{compact(step['arguments'])}`"
                f" · {step['ms']} 毫秒")
        box.markdown(line + ("" if step["ok"] else f"\n\n:red[{to_streamlit_markdown(step['error'])}]"))
        for warning in event["result"].get("warnings", []):
            box.caption(f"⚠️ {to_streamlit_markdown(warning)}")
    elif kind == "repair":
        numbers = "、".join(m["text"] for m in event["ungrounded"])
        box.warning(f"自动核查：初稿有 {len(event['ungrounded'])} 个数字找不到出处（{to_streamlit_markdown(numbers)}），"
                    "退回模型修正 1 次")
    else:
        box.info(to_streamlit_markdown(event["text"].removeprefix("（系统提示）")))


def replay_events(run: dict[str, Any]) -> list[dict[str, Any]]:
    """从运行记录（run.json 的内容）还原事件序列。

    和实时运行发出的事件逐条相同，只有一处差别：模型调用失败的那一轮没有留下消息，回放里没有它的 model 事件；
    失败原因在运行状态里。
    """
    events: list[dict[str, Any]] = []
    steps = iter(run["steps"])  # 每次工具调用都同时留下一条 step 和一条 tool 消息，顺序一致
    repair = run.get("repair")
    round_no = 0
    for index, message in enumerate(run["messages"]):
        if index < 2:  # 系统提示词和用户问题
            continue
        if message["role"] == "assistant":
            round_no += 1
            events.append({"type": "model", "round": round_no})
        elif message["role"] == "tool":
            events.append({"type": "tool", "step": next(steps), "result": json.loads(message["content"])})
        elif repair is not None and index == repair["draft_message_index"] + 1:
            events.append({"type": "repair", "ungrounded": repair["ungrounded"]})
        else:
            events.append({"type": "notice", "text": message["content"]})
    return events


def to_streamlit_markdown(text: str, ungrounded: Sequence[dict[str, Any]] = ()) -> str:
    """转义 $ 和 ~、标题降两级（代码块里都不动），并把找不到出处的数字标红。

    按核查器记录的位置标红，不按字符串查找：同一个数字可能出现多次，只有核查器指出的那一处没有出处。
    位置和原文对不上（旧记录没有位置）或落在代码块里的数字不标红，核查页里照样列出。
    """
    code = [m.span() for m in CODE.finditer(text)]

    def in_code(position: int) -> bool:
        return any(a <= position < b for a, b in code)

    spans = [(start, end, "code") for start, end in code]
    spans += [(m.start(), m.start(), "heading") for m in HEADING.finditer(text) if not in_code(m.start())]
    for mention in ungrounded:
        start, end = mention.get("start", 0), mention.get("end", 0)
        if end > start and text[start:end] == mention["text"] and not in_code(start):
            spans.append((start, end, "mark"))
    parts, cursor = [], 0
    for start, end, kind in sorted(spans):
        if start < cursor:  # 重叠的标记只保留第一个
            continue
        parts.append(text[cursor:start].translate(MARKDOWN_ESCAPES))
        piece = text[start:end]
        if kind == "heading":
            parts.append("##")
        else:
            parts.append(piece if kind == "code" else f":red-background[{piece.translate(MARKDOWN_ESCAPES)}]")
        cursor = end
    parts.append(text[cursor:].translate(MARKDOWN_ESCAPES))
    return "".join(parts)


@dataclass(frozen=True)
class SavedRun:
    """一次可回放的运行。source：样例（examples/real_runs，已提交）或本机（runs/，不进 git）。"""

    path: Path
    source: str
    run: dict[str, Any]

    @property
    def label(self) -> str:
        run = self.run
        domain = "财报" if run.get("domain") == "company" else "电商"
        return f"［{self.source}］{run['question']}　{domain} · {run['model']} · {run['started_at'][:10]}"


def load_run(run_dir: Path, source: str = "本机") -> SavedRun:
    return SavedRun(run_dir, source, json.loads((run_dir / "run.json").read_text(encoding="utf-8")))


def saved_runs(example_dir: Path = REAL_RUNS, local_dir: Path | None = None) -> list[SavedRun]:
    """先列样例（按时间），再列本机记录（新的在前）。本机记录里和样例重复的不再列出；读不了的目录跳过。"""
    found: list[SavedRun] = []
    seen: set[str] = set()
    local_dir = RUNS_DIR if local_dir is None else local_dir
    for root, source, newest_first in ((example_dir, "样例", False), (local_dir, "本机", True)):
        for path in sorted(root.glob("*/run.json"), reverse=newest_first):
            try:
                saved = load_run(path.parent, source)
            except (OSError, ValueError):
                continue
            if saved.run.get("run_id") in seen or "messages" not in saved.run:
                continue
            seen.add(saved.run.get("run_id"))
            found.append(saved)
    return found


def live_problem(domain: str, settings: Settings) -> str | None:
    """实时提问前的检查：没有密钥或数据时给出原因，界面上禁用“开始分析”。"""
    from .dataset import PARQUET_PATH

    if not settings.has_api_key():
        return "没有配置模型密钥（.env 里的 LLM_API_KEY），只能回放已保存的运行。"
    if domain == "ecommerce" and not PARQUET_PATH.exists():
        return "还没有准备电商数据，请先在命令行运行：da-agent prepare-data"
    return None


def make_llm(settings: Settings) -> LLMClient:
    """实时提问用的模型客户端。测试里替换成假模型。"""
    from .llm import OpenAICompatibleLLM

    return OpenAICompatibleLLM(settings)


def ask(question: str, domain: str, settings: Settings,
        on_step: Callable[[dict[str, Any]], None] | None = None) -> Path:
    """回答一个问题，保存运行记录并生成核查结果和报告（和命令行的 ask、company-ask 相同），返回运行目录。"""
    from .cleaning import connect
    from .company import connect_financials
    from .dataset import PARQUET_PATH

    llm = make_llm(settings)
    with (connect_financials() if domain == "company" else connect(PARQUET_PATH)) as con:
        run = run_agent(question, llm=llm, con=con, max_tool_calls=settings.llm_max_tool_calls, domain=domain,
                        on_step=on_step)
    run_dir = save_run(run, RUNS_DIR)
    write_review(run_dir)
    return run_dir
