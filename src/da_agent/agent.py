"""Agent 循环：模型决定调用哪个工具，程序执行工具并把结果交回模型，直到模型给出答案。

一轮 = 调一次模型。模型要么返回工具调用请求（执行后进入下一轮），要么返回文字答案（结束）。

四道闸：
1. 工具调用次数上限（LLM_MAX_TOOL_CALLS）：用完后，多出的请求收到“次数已用完”的回复，并提示直接作答
2. 轮数上限：兜底，防止模型一直请求工具
3. 协议完整性：模型发出的每个工具请求都有一条对应的回复（接口要求，被拒绝的请求也要回）
4. 失败不崩溃：模型调用失败或工具出错都会记录，并以明确的状态结束
"""

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb

from .llm import LLMClient, LLMError
from .metrics import data_range
from .paths import ROOT
from .tools import ToolOutcome, execute, tool_specs

SYSTEM_PROMPT_PATH = ROOT / "prompts" / "agent_system.md"
BUDGET_NOTICE = "（系统提示）工具调用次数已用完。请不要再调用工具，直接根据已有的工具结果给出最终回答。"


@dataclass
class AgentRun:
    """一次运行的完整记录。messages 是和模型的全部对话，可用来复盘每一步。"""

    run_id: str
    question: str
    model: str
    max_tool_calls: int
    status: str = "running"  # completed / empty_answer / llm_error / max_rounds
    answer: str = ""
    error: str | None = None
    budget_exhausted: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0})
    started_at: str = ""
    seconds: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)


def build_system_prompt(con: duckdb.DuckDBPyConnection, max_tool_calls: int) -> str:
    """把数据范围、最近完整周、工具上限填进系统提示词模板。"""
    template = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(**data_range(con), max_tool_calls=max_tool_calls)


def run_agent(question: str, *, llm: LLMClient, con: duckdb.DuckDBPyConnection, max_tool_calls: int,
              system_prompt: str | None = None) -> AgentRun:
    """运行一次 Agent 循环，返回完整记录。任何失败都体现在 status 里，不向外抛异常。"""
    start = time.perf_counter()
    run = AgentRun(run_id=f"{datetime.now():%Y%m%dT%H%M%S}-{uuid4().hex[:6]}", question=question, model=llm.model,
                   max_tool_calls=max_tool_calls, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    run.messages = [{"role": "system", "content": system_prompt or build_system_prompt(con, max_tool_calls)},
                    {"role": "user", "content": question}]
    specs = tool_specs()
    used = 0
    run.status = "max_rounds"
    for round_no in range(1, max_tool_calls + 3):  # 正常情况下每轮至少用 1 次工具，所以这个轮数足够
        try:
            reply = llm.chat(run.messages, tools=specs)
        except LLMError as exc:
            run.status, run.error = "llm_error", str(exc)
            break
        run.usage["llm_calls"] += 1
        for key in ("prompt_tokens", "completion_tokens"):
            run.usage[key] += (reply.usage or {}).get(key, 0)
        run.messages.append(reply.as_message())
        if not reply.tool_calls:
            run.answer = reply.content
            run.status = "completed" if reply.content else "empty_answer"
            break
        for call in reply.tool_calls:
            if used >= max_tool_calls:
                outcome = ToolOutcome(False, {"error": f"工具调用次数已用完（上限 {max_tool_calls} 次），请直接根据已有结果回答"}, 0)
            else:
                used += 1
                outcome = execute(con, call.name, call.arguments)
            run.steps.append({"round": round_no, "tool": call.name, "arguments": call.arguments, "ok": outcome.ok,
                              "error": None if outcome.ok else outcome.content["error"], "ms": outcome.duration_ms})
            run.messages.append({"role": "tool", "tool_call_id": call.id, "content": outcome.to_json()})
        if used >= max_tool_calls and not run.budget_exhausted:
            run.budget_exhausted = True
            run.messages.append({"role": "user", "content": BUDGET_NOTICE})
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
