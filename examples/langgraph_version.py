"""用 LangGraph 重写 Agent 循环（M3b 对照示例）。

主流程（src/da_agent/agent.py）不依赖 LangGraph，这个文件只用来对比。工具、数字核查、模型客户端都直接复用，
换掉的只有“控制流程”：agent.py 里的 while 循环，在这里变成一张图——节点是一步要做的事，边决定下一步去哪。

    START → model ─┬─ 有工具请求 → tools ──┬─ 轮数没用完 → model
                   ├─ 文字回答 → review ──┼─ 要重发、要退回修正 → model
                   └─ 模型报错 ───────────┴─ 结束 → finish → END

对照要求：同一份假模型剧本下，两个版本发给模型的内容、工具调用、对话、最终答案和状态完全一致，
见 tests/test_langgraph_version.py。运行前安装可选依赖：uv sync --group langgraph
"""

import operator
import time
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict
from uuid import uuid4

import duckdb
from langgraph.graph import END, START, StateGraph

from da_agent.agent import (
    BUDGET_NOTICE, FORMAT_NOTICE, MALFORMED_ERROR, REPAIR_TOOL_CALLS, TOOL_CALL_TEXT, AgentRun,
    build_system_prompt, repair_notice,
)
from da_agent.checks import number_check
from da_agent.llm import LLMClient, LLMError, ToolCall
from da_agent.tools import ToolOutcome, execute, tool_specs

FINAL = ("completed", "empty_answer", "malformed_answer")


class State(TypedDict):
    """图里流转的状态。messages 和 steps 只追加（operator.add 合并），其他字段每次整体替换。"""

    messages: Annotated[list[dict[str, Any]], operator.add]
    steps: Annotated[list[dict[str, Any]], operator.add]
    calls: list[ToolCall]  # 模型这一轮请求的工具
    content: str  # 模型这一轮的文字
    usage: dict[str, int]
    round_no: int
    max_rounds: int
    used: int
    limit: int
    notified_at: int | None
    budget_exhausted: bool
    malformed: int
    repair: dict[str, Any] | None
    status: str
    answer: str
    error: str | None


def build_graph(llm: LLMClient, con: duckdb.DuckDBPyConnection, specs: list[dict[str, Any]],
                repair_enabled: bool) -> Any:
    """把四道闸、退回修正、格式错误防护画成一张图。节点只返回要更新的字段。"""
    allowed = [spec["function"]["name"] for spec in specs]

    def model(state: State) -> dict[str, Any]:
        round_no = state["round_no"] + 1
        try:
            reply = llm.chat(state["messages"], tools=specs)
        except LLMError as exc:
            return {"round_no": round_no, "status": "llm_error", "error": str(exc), "calls": [], "content": ""}
        usage = {**state["usage"], "llm_calls": state["usage"]["llm_calls"] + 1}
        for key in ("prompt_tokens", "completion_tokens"):
            usage[key] += (reply.usage or {}).get(key, 0)
        return {"round_no": round_no, "usage": usage, "messages": [reply.as_message()],
                "calls": list(reply.tool_calls), "content": reply.content}

    def tools(state: State) -> dict[str, Any]:
        used, steps, messages = state["used"], [], []
        for call in state["calls"]:
            if used >= state["limit"]:
                outcome = ToolOutcome(False, {"error": "工具调用次数已用完，请直接根据已有结果回答"}, 0)
            elif call.name not in allowed:
                used += 1
                outcome = ToolOutcome(False, {"error": f"没有名为 {call.name} 的工具；可用工具：{allowed}"}, 0)
            else:
                used += 1
                outcome = execute(con, call.name, call.arguments)
            steps.append({"round": state["round_no"], "tool": call.name, "arguments": call.arguments, "ok": outcome.ok,
                          "error": None if outcome.ok else outcome.content["error"], "ms": outcome.duration_ms})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": outcome.to_json()})
        update: dict[str, Any] = {"used": used, "steps": steps}
        if used >= state["limit"] and state["notified_at"] != state["limit"]:
            messages.append({"role": "user", "content": BUDGET_NOTICE})
            update |= {"notified_at": state["limit"], "budget_exhausted": True}
        return update | {"messages": messages}

    def review(state: State) -> dict[str, Any]:
        content = state["content"]
        if TOOL_CALL_TEXT.search(content or ""):  # 把工具调用写成了文字：提示重发一次，再错就结束
            malformed = state["malformed"] + 1
            if malformed == 1:
                return {"malformed": malformed, "messages": [{"role": "user", "content": FORMAT_NOTICE}]}
            return {"malformed": malformed, "status": "malformed_answer", "error": MALFORMED_ERROR}
        if repair_enabled and state["repair"] is None and content:
            ungrounded = number_check(content, state["messages"])["ungrounded"]
            if ungrounded:  # 退回修正：给修正留出工具次数和轮数
                repair = {"draft": content, "draft_message_index": len(state["messages"]) - 1,
                          "draft_round": state["round_no"],
                          "ungrounded": [{"text": m["text"], "context": m["context"]} for m in ungrounded]}
                return {"repair": repair, "messages": [{"role": "user", "content": repair_notice(ungrounded)}],
                        "limit": state["used"] + REPAIR_TOOL_CALLS,
                        "max_rounds": state["round_no"] + REPAIR_TOOL_CALLS + 1}
        return {"answer": content, "status": "completed" if content else "empty_answer"}

    def finish(state: State) -> dict[str, Any]:
        if state["repair"] is not None and state["status"] != "completed":  # 修正失败：保留初稿
            return {"repair": {**state["repair"], "error": state["error"] or state["status"]},
                    "answer": state["repair"]["draft"], "status": "completed", "error": None}
        return {}

    def next_round(state: State) -> str:  # 轮数上限：和 while 循环开头的判断一样
        return "model" if state["round_no"] < state["max_rounds"] else "finish"

    def after_model(state: State) -> str:
        if state["status"] == "llm_error":
            return "finish"
        return "tools" if state["calls"] else "review"

    def after_review(state: State) -> str:
        return "finish" if state["status"] in FINAL else next_round(state)

    graph = StateGraph(State)
    for name, node in (("model", model), ("tools", tools), ("review", review), ("finish", finish)):
        graph.add_node(name, node)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", after_model, {"tools": "tools", "review": "review", "finish": "finish"})
    graph.add_conditional_edges("tools", next_round, {"model": "model", "finish": "finish"})
    graph.add_conditional_edges("review", after_review, {"model": "model", "finish": "finish"})
    graph.add_edge("finish", END)
    return graph.compile()


def run_agent_langgraph(question: str, *, llm: LLMClient, con: duckdb.DuckDBPyConnection, max_tool_calls: int,
                        system_prompt: str | None = None, use_skills: bool = True, repair: bool = True,
                        domain: str = "ecommerce") -> AgentRun:
    """和 da_agent.agent.run_agent 的参数、返回值完全相同，只是控制流程由 LangGraph 驱动。"""
    start = time.perf_counter()
    run = AgentRun(run_id=f"{datetime.now():%Y%m%dT%H%M%S}-{uuid4().hex[:6]}", question=question, model=llm.model,
                   max_tool_calls=max_tool_calls, use_skills=use_skills, domain=domain,
                   started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    specs = [s for s in tool_specs(domain) if use_skills or s["function"]["name"] != "load_skill"]
    app = build_graph(llm, con, specs, repair)
    max_rounds = max_tool_calls + 2
    initial: State = {
        "messages": [{"role": "system",
                      "content": system_prompt or build_system_prompt(con, max_tool_calls, use_skills, domain)},
                     {"role": "user", "content": question}],
        "steps": [], "calls": [], "content": "", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
        "round_no": 0, "max_rounds": max_rounds, "used": 0, "limit": max_tool_calls, "notified_at": None,
        "budget_exhausted": False, "malformed": 0, "repair": None, "status": "max_rounds", "answer": "", "error": None,
    }
    # 每轮最多走两个节点（model 加 tools 或 review），再加修正留出的轮数和 finish
    final = app.invoke(initial, config={"recursion_limit": 2 * (max_rounds + REPAIR_TOOL_CALLS + 1) + 5})
    for key in ("messages", "steps", "usage", "status", "answer", "error", "repair", "budget_exhausted"):
        setattr(run, key, final[key])
    run.malformed_replies = final["malformed"]
    run.seconds = round(time.perf_counter() - start, 2)
    return run
