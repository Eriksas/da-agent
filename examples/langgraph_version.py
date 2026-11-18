"""用 LangGraph 重写 Agent 循环（M3b 对照示例；M21 扩成多 Agent，并行取数）。

主流程（src/da_agent/agent.py）不依赖 LangGraph，这个文件只用来对比。工具、数字核查、模型客户端都直接复用，
换掉的只有“控制流程”：agent.py 里的 while 循环，在这里变成一张图——节点是一步要做的事，边决定下一步去哪。

    START → model ─┬─ 有工具请求 → plan ─┬─ Send → fixed_tools ───┬→ collect ─┬─ 轮数没用完 → model
                   │                      └─ Send × N → query_agent ┘           └─ 结束 → finish → END
                   ├─ 文字回答 → review ──┬─ 要重发、要退回修正 → model
                   └─ 模型报错 ───────────┴─ 结束 → finish → END

多 Agent（M21）：主 Agent 和查询 Agent 是同一张图，换了工具和提示词。主 Agent 请求取数（query_data）时，
query_agent 节点在里面再跑一张查询 Agent 的图（子图），交回程序执行得到的结果表。

并行取数：模型一轮里发出几个取数请求时，plan 节点用 Send 把每个请求扇出成一个 query_agent 任务，
LangGraph 在同一步里并行执行它们；固定工具（同一个 DuckDB 连接不能多线程同时用）放在一个 fixed_tools 任务里按顺序执行。
collect 节点按调用顺序汇合结果，所以对话、编号、嵌套记录和手写版按顺序执行时完全一样，只有耗时不同。

对照要求：同一份假模型剧本下，两个版本发给模型的内容（主 Agent 和每个查询 Agent）、工具调用、对话、嵌套记录、
最终答案和状态完全一致，见 tests/test_langgraph_version.py。运行前安装可选依赖：uv sync --group langgraph
"""

import json
import operator
import time
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import ValidationError

from da_agent.agent import (
    BUDGET_NOTICE,
    FORMAT_NOTICE,
    MALFORMED_ERROR,
    REPAIR_TOOL_CALLS,
    TOOL_CALL_TEXT,
    AgentRun,
    build_system_prompt,
    repair_notice,
)
from da_agent.analyst import QueryDataArgs, QueryService
from da_agent.checks import number_check
from da_agent.llm import LLMClient, LLMError, ToolCall
from da_agent.tools import ToolOutcome, execute, tool_specs

FINAL = ("completed", "empty_answer", "malformed_answer")
NODES_PER_ROUND = 4  # model、plan、工具（并行的任务算同一步）、collect


def _pending(old: list[dict[str, Any]], new: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """并行任务的结果先放在 pending 里攒着；collect 汇合后传 None 清空。"""
    return [] if new is None else old + new


class State(TypedDict):
    """图里流转的状态。messages、steps、sub_runs 只追加（operator.add 合并），其他字段每次整体替换。"""

    messages: Annotated[list[dict[str, Any]], operator.add]
    steps: Annotated[list[dict[str, Any]], operator.add]
    sub_runs: Annotated[list[dict[str, Any]], operator.add]  # 查询 Agent 的完整记录，按调用顺序
    pending: Annotated[list[dict[str, Any]], _pending]  # 这一轮各个工具任务的结果，等 collect 按顺序汇合
    plan: list[dict[str, Any]]  # 这一轮每个工具请求怎么处理：budget 次数用完、unknown 没有这个工具、execute、query 取数
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


def build_graph(llm: LLMClient, con: Any, specs: list[dict[str, Any]], repair_enabled: bool,
                contexts: dict[str, Any] | None = None, parallel: bool = True) -> Any:
    """把四道闸、退回修正、格式错误防护、取数扇出画成一张图。节点只返回要更新的字段。

    contexts：可选工具的上下文（{"query_data": QueryService}），和 run_agent 的 tool_contexts 相同。
    parallel：False 时取数也放进 fixed_tools 按顺序执行，用来和并行对比耗时。
    """
    allowed = [spec["function"]["name"] for spec in specs]
    contexts = contexts or {}
    service: QueryService | None = contexts.get("query_data")

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

    def plan(state: State) -> dict[str, Any]:
        """按顺序决定每个请求怎么处理，并记下用掉的次数：和手写版逐个处理时的判断一模一样。"""
        used, entries = state["used"], []
        for index, call in enumerate(state["calls"]):
            if used >= state["limit"]:
                kind = "budget"
            elif call.name not in allowed:
                used, kind = used + 1, "unknown"
            else:
                used, kind = used + 1, "execute"
                if call.name == "query_data" and service is not None:
                    try:  # 参数不合法的取数不会真的去跑查询 Agent：和普通工具一样当场返回错误
                        QueryDataArgs.model_validate(call.arguments)
                        kind = "query"
                    except ValidationError:
                        pass
            entries.append({"index": index, "call": call, "kind": kind})
        return {"plan": entries, "used": used}

    def fan_out(state: State) -> list[Send]:
        """取数请求各开一个并行任务；其余请求放进一个按顺序执行的任务。"""
        round_no = state["round_no"]
        queries = [e for e in state["plan"] if e["kind"] == "query"] if parallel else []
        others = [e for e in state["plan"] if e not in queries]
        sends = [Send("fixed_tools", {"entries": others, "round_no": round_no})] if others else []
        return sends + [Send("query_agent", {"entry": e, "round_no": round_no}) for e in queries]

    def run_entry(entry: dict[str, Any], round_no: int) -> dict[str, Any]:
        call, kind = entry["call"], entry["kind"]
        record = None
        if kind == "budget":
            outcome = ToolOutcome(False, {"error": "工具调用次数已用完，请直接根据已有结果回答"}, 0)
        elif kind == "unknown":
            outcome = ToolOutcome(False, {"error": f"没有名为 {call.name} 的工具；可用工具：{allowed}"}, 0)
        elif kind == "query":
            # 每个取数任务用自己的 QueryService：并行时不共用一个 records 列表；编号由 collect 按调用顺序重排
            mine = QueryService(llm=service.llm, db=service.db, max_tool_calls=service.max_tool_calls,
                                agent=run_agent_langgraph)
            outcome = execute(mine, call.name, call.arguments)
            record = mine.records[0] if mine.records else None
        else:
            outcome = execute(contexts.get(call.name, con), call.name, call.arguments)
        step = {"round": round_no, "tool": call.name, "arguments": call.arguments, "ok": outcome.ok,
                "error": None if outcome.ok else outcome.content["error"], "ms": outcome.duration_ms}
        return {"index": entry["index"], "call_id": call.id, "content": outcome.content, "step": step, "record": record}

    def fixed_tools(task: dict[str, Any]) -> dict[str, Any]:
        return {"pending": [run_entry(entry, task["round_no"]) for entry in task["entries"]]}

    def query_agent(task: dict[str, Any]) -> dict[str, Any]:
        return {"pending": [run_entry(task["entry"], task["round_no"])]}

    def collect(state: State) -> dict[str, Any]:
        """按调用顺序汇合：工具消息、步骤、查询 Agent 的记录和编号，以及次数用完的提示。"""
        messages, steps, records = [], [], []
        number = len(state["sub_runs"])
        for item in sorted(state["pending"], key=lambda item: item["index"]):
            content = item["content"]
            if item["record"] is not None:
                number += 1
                records.append(item["record"])
                if "query_run" in content:
                    content = {**content, "query_run": number}
            steps.append(item["step"])
            messages.append({"role": "tool", "tool_call_id": item["call_id"],
                             "content": json.dumps(content, ensure_ascii=False, default=str)})
        update: dict[str, Any] = {"steps": steps, "sub_runs": records, "pending": None}
        if state["used"] >= state["limit"] and state["notified_at"] != state["limit"]:
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
        return "plan" if state["calls"] else "review"

    def after_review(state: State) -> str:
        return "finish" if state["status"] in FINAL else next_round(state)

    graph = StateGraph(State)
    for name, node in (("model", model), ("plan", plan), ("fixed_tools", fixed_tools), ("query_agent", query_agent),
                       ("collect", collect), ("review", review), ("finish", finish)):
        graph.add_node(name, node)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", after_model, {"plan": "plan", "review": "review", "finish": "finish"})
    graph.add_conditional_edges("plan", fan_out, ["fixed_tools", "query_agent"])
    graph.add_edge("fixed_tools", "collect")
    graph.add_edge("query_agent", "collect")
    graph.add_conditional_edges("collect", next_round, {"model": "model", "finish": "finish"})
    graph.add_conditional_edges("review", after_review, {"model": "model", "finish": "finish"})
    graph.add_edge("finish", END)
    return graph.compile()


def run_agent_langgraph(question: str, *, llm: LLMClient, con: Any, max_tool_calls: int,
                        system_prompt: str | None = None, use_skills: bool = True, repair: bool = True,
                        domain: str = "ecommerce", tool_contexts: dict[str, Any] | None = None,
                        parallel: bool = True) -> AgentRun:
    """和 da_agent.agent.run_agent 的参数、返回值相同（没有 on_step），只是控制流程由 LangGraph 驱动。

    给了 tool_contexts={"query_data": QueryService} 时就是多 Agent：查询 Agent 也用这个函数跑（子图）。
    """
    start = time.perf_counter()
    contexts = tool_contexts or {}
    run = AgentRun(run_id=f"{datetime.now():%Y%m%dT%H%M%S}-{uuid4().hex[:6]}", question=question, model=llm.model,
                   max_tool_calls=max_tool_calls, use_skills=use_skills, domain=domain,
                   started_at=datetime.now(UTC).isoformat(timespec="seconds"))
    specs = [s for s in tool_specs(domain, optional=list(contexts)) if use_skills or s["function"]["name"] != "load_skill"]
    app = build_graph(llm, con, specs, repair, contexts, parallel)
    max_rounds = max_tool_calls + 2
    prompt = system_prompt or build_system_prompt(con, max_tool_calls, use_skills, domain, query="query_data" in contexts)
    initial: State = {
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": question}],
        "steps": [], "sub_runs": [], "pending": [], "plan": [], "calls": [], "content": "",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0},
        "round_no": 0, "max_rounds": max_rounds, "used": 0, "limit": max_tool_calls, "notified_at": None,
        "budget_exhausted": False, "malformed": 0, "repair": None, "status": "max_rounds", "answer": "", "error": None,
    }
    # 每轮最多走 NODES_PER_ROUND 步，再加修正留出的轮数和 finish
    limit = NODES_PER_ROUND * (max_rounds + REPAIR_TOOL_CALLS + 1) + 5
    final = app.invoke(initial, config={"recursion_limit": limit})
    for key in ("messages", "steps", "usage", "status", "answer", "error", "repair", "budget_exhausted", "sub_runs"):
        setattr(run, key, final[key])
    run.malformed_replies = final["malformed"]
    run.seconds = round(time.perf_counter() - start, 2)
    return run


def graph_mermaid() -> str:
    """这张图的 Mermaid 写法（README 里的图由它生成），不需要模型和数据。"""
    return build_graph(llm=None, con=None, specs=[], repair_enabled=True).get_graph().draw_mermaid()  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    """用真实模型跑一次 LangGraph 版的多 Agent（电商问答，开启取数），记录存到 runs/，和 da-agent ask 一样生成核查和报告。

    uv run --group langgraph python examples/langgraph_version.py "2011 年 10 月和 11 月，各自哪天卖得最好？"
    """
    import argparse

    from da_agent.agent import save_run
    from da_agent.cleaning import connect
    from da_agent.config import Settings
    from da_agent.dataset import PARQUET_PATH
    from da_agent.llm import OpenAICompatibleLLM
    from da_agent.paths import RUNS_DIR
    from da_agent.report import write_review
    from da_agent.semantic import prepare

    parser = argparse.ArgumentParser(description="LangGraph 版多 Agent：真实模型回答一个电商问题")
    parser.add_argument("question")
    parser.add_argument("--sequential", action="store_true", help="取数按顺序执行（默认并行），用来对比耗时")
    args = parser.parse_args(argv)
    settings = Settings()
    llm = OpenAICompatibleLLM(settings)
    service = QueryService(llm=llm, db=prepare("retail"), agent=run_agent_langgraph)
    with connect(PARQUET_PATH) as con:
        run = run_agent_langgraph(args.question, llm=llm, con=con, max_tool_calls=settings.llm_max_tool_calls,
                                  tool_contexts={"query_data": service}, parallel=not args.sequential)
    run_dir = save_run(run, RUNS_DIR)
    checks = write_review(run_dir)
    rounds: dict[int, int] = {}
    for step in run.steps:
        if step["tool"] == "query_data":
            rounds[step["round"]] = rounds.get(step["round"], 0) + 1
    parallel_rounds = {r: n for r, n in rounds.items() if n > 1}
    print(run.answer)
    print(f"\n状态：{run.status}；用时 {run.seconds} 秒；取数 {sum(rounds.values())} 次，其中一轮里同时取数的轮次：{parallel_rounds or '没有'}；"
          f"查询 Agent 的耗时：{[sub['seconds'] for sub in run.sub_runs]}")
    print(checks["summary"])
    print(f"运行记录：{run_dir}")
    return 0 if run.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
