"""Agent 循环：模型决定调用哪个工具，程序执行工具并把结果交回模型，直到模型给出答案。

一轮 = 调一次模型。模型要么返回工具调用请求（执行后进入下一轮），要么返回文字答案（结束）。

四道闸：
1. 工具调用次数上限（LLM_MAX_TOOL_CALLS）：用完后，多出的请求收到“次数已用完”的回复，并提示直接作答
2. 轮数上限：兜底，防止模型一直请求工具
3. 协议完整性：模型发出的每个工具请求都有一条对应的回复（接口要求，被拒绝的请求也要回）
4. 失败不崩溃：模型调用失败或工具出错都会记录，并以明确的状态结束

核查后退回修正（M7b）：模型交出答案后，程序先用和事后核查相同的规则查一遍数字出处。
有找不到出处的数字，就把它们连同所在句子退回给模型，要求改成原数、用 calculate 计算或删掉，最多退回 1 次。
修正这一步出了问题（模型报错、空回答、轮数用完）时保留修正前的答案：核查只能让答案变好，不能把答案弄丢。

格式错误的工具调用（M7b 评测发现）：模型偶尔把工具调用写成一段文字（带 <tool_call>、<invoke> 之类的标记），
而不是按接口格式发出。程序以前会把这段乱码当成最终答案。现在识别出来后提示模型重发一次；再错就以 malformed_answer 结束，
不把乱码当答案发布。
"""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb

from .checks import number_check
from .company import company_catalog
from .llm import LLMClient, LLMError
from .metrics import data_range
from .paths import ROOT
from .skills import catalog, load_skills
from .tools import ToolOutcome, execute, tool_specs

SYSTEM_PROMPT_PATH = ROOT / "prompts" / "agent_system.md"
# 每个领域一份系统提示词和一组工具；循环、核查、退回修正对所有领域都一样
SYSTEM_PROMPTS = {"ecommerce": SYSTEM_PROMPT_PATH, "company": ROOT / "prompts" / "company_system.md"}
BUDGET_NOTICE = "（系统提示）工具调用次数已用完。请不要再调用工具，直接根据已有的工具结果给出最终回答。"
FORMAT_NOTICE = ("（系统提示）上一条回复里有工具调用的文字标记，但没有按函数调用的格式发出，程序无法执行。"
                 "需要工具时请直接发起工具调用；不需要时请给出文字回答。")
TOOL_CALL_TEXT = re.compile(r"<tool_call>|</?invoke\b|minimax\[>")  # 2026-10-07 评测中出现过的写法
MALFORMED_ERROR = "模型连续两次把工具调用写成文字，没有按接口格式发出"
REPAIR_TOOL_CALLS = 3  # 修正时最多再调用几次工具（通常是 calculate）
REPAIR_NOTICE = """（系统核查）回答里有 {count} 个数字在工具结果中找不到出处：
{items}
请逐个处理：工具结果里有原数的，改成原数；需要计算的，用 calculate 计算后引用结果（最多再调用 {calls} 次工具）；\
无法确定的，删掉或改成不带数字的描述。然后输出修改后的完整回答，不要只写改动的部分，也不要提到这次核查。"""


@dataclass
class AgentRun:
    """一次运行的完整记录。messages 是和模型的全部对话，可用来复盘每一步。"""

    run_id: str
    question: str
    model: str
    max_tool_calls: int
    use_skills: bool = True  # 评测对比用：False 时不提供分析流程
    domain: str = "ecommerce"  # ecommerce 电商运营 / company 上市公司财报
    status: str = "running"  # completed / empty_answer / llm_error / max_rounds / malformed_answer
    answer: str = ""
    error: str | None = None
    budget_exhausted: bool = False
    repair: dict[str, Any] | None = None  # 被退回修正时：初稿、退回的数字、初稿在对话中的位置；没退回为 None
    malformed_replies: int = 0  # 把工具调用写成文字的次数
    steps: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0})
    started_at: str = ""
    seconds: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)


def build_system_prompt(con: duckdb.DuckDBPyConnection, max_tool_calls: int, use_skills: bool = True,
                        domain: str = "ecommerce") -> str:
    """把数据范围（电商：周范围和最近完整周；财报：公司和财年）、工具上限、本领域的流程目录填进提示词模板。"""
    template = SYSTEM_PROMPTS[domain].read_text(encoding="utf-8")
    skills = catalog(load_skills(domain=domain) if use_skills else {})
    if domain == "company":
        return template.format(companies=company_catalog(con), max_tool_calls=max_tool_calls, skills_catalog=skills)
    return template.format(**data_range(con), max_tool_calls=max_tool_calls, skills_catalog=skills)


def repair_notice(ungrounded: list[dict[str, Any]]) -> str:
    items = "\n".join(f"- 「{m['text']}」：{m['context']}" for m in ungrounded)
    return REPAIR_NOTICE.format(count=len(ungrounded), items=items, calls=REPAIR_TOOL_CALLS)


def run_agent(question: str, *, llm: LLMClient, con: duckdb.DuckDBPyConnection, max_tool_calls: int,
              system_prompt: str | None = None, use_skills: bool = True, repair: bool = True,
              domain: str = "ecommerce") -> AgentRun:
    """运行一次 Agent 循环，返回完整记录。任何失败都体现在 status 里，不向外抛异常。"""
    start = time.perf_counter()
    run = AgentRun(run_id=f"{datetime.now():%Y%m%dT%H%M%S}-{uuid4().hex[:6]}", question=question, model=llm.model,
                   max_tool_calls=max_tool_calls, use_skills=use_skills, domain=domain,
                   started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    run.messages = [{"role": "system",
                     "content": system_prompt or build_system_prompt(con, max_tool_calls, use_skills, domain)},
                    {"role": "user", "content": question}]
    specs = [s for s in tool_specs(domain) if use_skills or s["function"]["name"] != "load_skill"]
    allowed = [s["function"]["name"] for s in specs]  # 只执行这个领域的工具：别的领域的工具连的是另一份数据
    used, limit, notified_at = 0, max_tool_calls, None
    max_rounds = max_tool_calls + 2  # 正常情况下每轮至少用 1 次工具，再加作答的一轮和一轮余量
    run.status = "max_rounds"
    round_no = 0
    while round_no < max_rounds:
        round_no += 1
        try:
            reply = llm.chat(run.messages, tools=specs)
        except LLMError as exc:
            run.status, run.error = "llm_error", str(exc)
            break
        run.usage["llm_calls"] += 1
        for key in ("prompt_tokens", "completion_tokens"):
            run.usage[key] += (reply.usage or {}).get(key, 0)
        run.messages.append(reply.as_message())
        if not reply.tool_calls and TOOL_CALL_TEXT.search(reply.content or ""):
            run.malformed_replies += 1
            if run.malformed_replies == 1:  # 提示重发一次
                run.messages.append({"role": "user", "content": FORMAT_NOTICE})
                continue
            run.status, run.error = "malformed_answer", MALFORMED_ERROR
            break
        if not reply.tool_calls:
            if repair and run.repair is None and reply.content:
                ungrounded = number_check(reply.content, run.messages)["ungrounded"]
                if ungrounded:  # 退回修正：本轮不结束，给修正留出工具次数和轮数
                    run.repair = {"draft": reply.content, "draft_message_index": len(run.messages) - 1,
                                  "draft_round": round_no,
                                  "ungrounded": [{"text": m["text"], "context": m["context"]} for m in ungrounded]}
                    run.messages.append({"role": "user", "content": repair_notice(ungrounded)})
                    limit, max_rounds = used + REPAIR_TOOL_CALLS, round_no + REPAIR_TOOL_CALLS + 1
                    continue
            run.answer = reply.content
            run.status = "completed" if reply.content else "empty_answer"
            break
        for call in reply.tool_calls:
            if used >= limit:
                outcome = ToolOutcome(False, {"error": "工具调用次数已用完，请直接根据已有结果回答"}, 0)
            elif call.name not in allowed:
                used += 1
                outcome = ToolOutcome(False, {"error": f"没有名为 {call.name} 的工具；可用工具：{allowed}"}, 0)
            else:
                used += 1
                outcome = execute(con, call.name, call.arguments)
            run.steps.append({"round": round_no, "tool": call.name, "arguments": call.arguments, "ok": outcome.ok,
                              "error": None if outcome.ok else outcome.content["error"], "ms": outcome.duration_ms})
            run.messages.append({"role": "tool", "tool_call_id": call.id, "content": outcome.to_json()})
        if used >= limit and notified_at != limit:
            notified_at, run.budget_exhausted = limit, True
            run.messages.append({"role": "user", "content": BUDGET_NOTICE})
    if run.repair is not None and run.status != "completed":  # 修正失败：保留初稿
        run.repair["error"] = run.error or run.status
        run.answer, run.status, run.error = run.repair["draft"], "completed", None
    run.seconds = round(time.perf_counter() - start, 2)
    return run


def save_run(run: AgentRun, runs_dir: Path) -> Path:
    """保存运行记录：run.json（完整记录）和 answer.md（答案）。不包含任何密钥。"""
    run_dir = runs_dir / run.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "run.json").write_text(json.dumps(asdict(run), ensure_ascii=False, indent=2, default=str) + "\n",
                                      encoding="utf-8", newline="\n")
    (run_dir / "answer.md").write_text((run.answer or f"（没有答案，状态：{run.status}）") + "\n",
                                       encoding="utf-8", newline="\n")
    return run_dir
