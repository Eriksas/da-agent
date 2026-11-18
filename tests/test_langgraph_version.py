"""M3b 对照：LangGraph 版和手写版在同样的假模型剧本下行为完全一致。

比较的内容：每一轮发给模型的对话和工具说明书、工具调用记录、完整对话、最终答案、状态、用量、退回修正记录。
不比较运行编号、开始时间和耗时。需要可选依赖：uv sync --group langgraph（CI 会安装）。
"""

import importlib.util
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest

pytest.importorskip("langgraph", reason="需要可选依赖：uv sync --group langgraph")

from test_company import write_key_facts

from da_agent import cli
from da_agent.agent import run_agent
from da_agent.cleaning import connect_frame
from da_agent.company import connect_financials
from da_agent.dataset import read_normalized_csv
from da_agent.llm import FakeLLM, LLMReply, ToolCall

EXAMPLE = Path(__file__).parents[1] / "examples" / "langgraph_version.py"
_spec = importlib.util.spec_from_file_location("langgraph_version", EXAMPLE)
langgraph_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(langgraph_version)

QUESTION = "2011-W02 的 GMV 为什么变了？"
GARBLED = ']<]minimax[>[<tool_call>\n]<]minimax[>[ invoke name="load_skill">]<]minimax[>[</invoke>'


def call(n: int, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=f"call_{n}", name=name, arguments=dict(arguments))


def tools(*calls: ToolCall) -> LLMReply:
    return LLMReply(content="", tool_calls=calls)


def answer(text: str) -> LLMReply:
    return LLMReply(content=text)


SCENARIOS: dict[str, tuple[Callable[[], list[LLMReply]], int]] = {
    "demo-script": (lambda: FakeLLM.from_file(cli.DEMO_SCRIPT).replies, 5),
    "budget-exhausted": (lambda: [tools(call(1, "metric_summary", week="2011-W02"), call(2, "decompose_gmv", week="2011-W02"),
                                        call(3, "drilldown", week="2011-W02")), answer("根据已有结果作答。")], 2),
    "repair-fixed": (lambda: [tools(call(1, "metric_summary", week="2011-W02")), answer("GMV 为 145.0，约合 1,234.56。"),
                              tools(call(2, "calculate", expression="145 - 140", purpose="差额")),
                              answer("GMV 为 145.0，比上周多 5.0。")], 5),
    "repair-failed": (lambda: [answer("GMV 为 1,234.56。")], 5),
    "repair-own-budget": (lambda: [tools(call(1, "metric_summary", week="2011-W02")), answer("GMV 为 1,234.56。"),
                                   tools(*(call(n, "calculate", expression="145 - 140", purpose="差额") for n in range(2, 6))),
                                   answer("GMV 为 145.0。")], 1),
    "malformed-then-ok": (lambda: [answer(GARBLED), tools(call(1, "metric_summary", week="2011-W02")),
                                   answer("GMV 为 145.0。")], 5),
    "malformed-twice": (lambda: [answer(GARBLED), answer(GARBLED)], 5),
    "never-stops": (lambda: [tools(call(n, "metric_summary", week="2011-W02")) for n in range(1, 10)], 1),
    "unknown-tool": (lambda: [tools(call(1, "delete_everything")), answer("没有这个工具。")], 5),
    "llm-error": (lambda: [], 5),
    "empty-answer": (lambda: [answer("")], 5),
}


def comparable(run) -> dict:
    data = asdict(run)
    for key in ("run_id", "started_at", "seconds"):
        data.pop(key)
    for step in data["steps"]:
        step.pop("ms")  # 工具耗时每次不同
    return data


def run_both(replies: Callable[[], list[LLMReply]], con, **kwargs) -> tuple[tuple[dict, list], tuple[dict, list]]:
    hand, graph = FakeLLM(replies=replies()), FakeLLM(replies=replies())
    a = run_agent(QUESTION, llm=hand, con=con, **kwargs)
    b = langgraph_version.run_agent_langgraph(QUESTION, llm=graph, con=con, **kwargs)
    return (comparable(a), hand.received), (comparable(b), graph.received)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_same_behaviour_as_hand_written_loop(name: str) -> None:
    replies, max_tool_calls = SCENARIOS[name]
    with connect_frame(read_normalized_csv(cli.DEMO_DATA)) as con:
        (hand, hand_inputs), (graph, graph_inputs) = run_both(replies, con, max_tool_calls=max_tool_calls)
    assert graph == hand
    assert graph_inputs == hand_inputs  # 每一轮发给模型的内容也一样


def test_same_behaviour_in_company_domain(tmp_path: Path) -> None:
    replies = lambda: [tools(call(1, "metric_summary", week="2011-W02")),  # noqa: E731  别的领域的工具：被拒绝
                       tools(call(2, "financial_summary", company="alibaba", fiscal_year=2024)),
                       answer("阿里巴巴 FY2024 的 ROE 为 8.70%。")]
    with connect_financials(write_key_facts(tmp_path / "key_facts.csv")) as con:
        (hand, hand_inputs), (graph, graph_inputs) = run_both(replies, con, max_tool_calls=5, domain="company")
    assert graph == hand and graph_inputs == hand_inputs
    assert hand["status"] == "completed" and hand["steps"][0]["ok"] is False


def test_scenarios_cover_every_ending() -> None:
    """对照场景覆盖了所有结束方式，不只是正常路径。"""
    endings = set()
    with connect_frame(read_normalized_csv(cli.DEMO_DATA)) as con:
        for replies, max_tool_calls in SCENARIOS.values():
            run = run_agent(QUESTION, llm=FakeLLM(replies=replies()), con=con, max_tool_calls=max_tool_calls)
            endings.add((run.status, run.repair is not None, run.budget_exhausted))
    statuses = {status for status, _, _ in endings}
    assert statuses == {"completed", "max_rounds", "malformed_answer", "llm_error", "empty_answer"}
    assert any(repaired for _, repaired, _ in endings) and any(exhausted for _, _, exhausted in endings)


# ---------- 多 Agent（M21）：主 Agent 用 query_data 把取数交给查询 Agent，查询 Agent 也是一张图（子图）----------

import json  # noqa: E402
import threading  # noqa: E402

from da_agent.analyst import QueryService  # noqa: E402
from da_agent.metrics import _ensure  # noqa: E402
from da_agent.semantic import SEMANTIC_DIR, build_database, load_config  # noqa: E402

BY_COUNTRY = {"sql": "SELECT country, round(sum(gmv_amount), 2) AS gmv FROM order_lines GROUP BY 1 ORDER BY 2 DESC, 1",
              "columns": {"country": "国家", "gmv": "GMV"}, "definition": "各国 GMV", "caveats": ["样本小"]}
ORDERS = {"sql": "SELECT count(DISTINCT order_id) AS orders FROM order_lines", "columns": {"orders": "订单数"},
          "definition": "订单数"}


class ByQuestion:
    """查询 Agent 用的假模型：按它收到的请求（对话里的用户问题）取各自的剧本。并行时各个查询 Agent 互不干扰。"""

    model = "fake"

    def __init__(self, scripts: dict[str, list[LLMReply]], delay: float = 0.0) -> None:
        self.scripts = {question: list(replies) for question, replies in scripts.items()}
        self.received: dict[str, list[dict]] = {}
        self.delay = delay
        self.lock = threading.Lock()

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply:
        import time

        from da_agent.llm import FakeLLMExhausted

        question = messages[1]["content"]
        with self.lock:
            self.received.setdefault(question, []).append({"messages": list(messages), "tools": tools})
            script = self.scripts.get(question, [])
            reply = script.pop(0) if script else None
        time.sleep(self.delay)
        if reply is None:
            raise FakeLLMExhausted(f"没有为“{question}”准备的回复")
        return reply


MULTI: dict[str, tuple[Callable[[], list[LLMReply]], Callable[[], dict[str, list[LLMReply]]], int]] = {
    "one-query": (lambda: [tools(call(1, "query_data", request="各国 GMV")), answer("United Kingdom 的 GMV 最高。")],
                  lambda: {"各国 GMV": [tools(call(1, "submit_query", **BY_COUNTRY)), answer("交出了。")]}, 5),
    # 同一轮里两个取数加一个固定工具：LangGraph 版并行取数，汇合后的顺序和编号要和手写版一样
    "parallel-round": (lambda: [tools(call(1, "query_data", request="各国 GMV"), call(2, "metric_summary", week="2011-W02"),
                                      call(3, "query_data", request="2011 年的毛利率")), answer("毛利率算不了，各国 GMV 见上。")],
                       lambda: {"各国 GMV": [tools(call(1, "submit_query", **BY_COUNTRY)), answer("交出了。")],
                                "2011 年的毛利率": [answer("数据里没有成本，算不了。")]}, 5),
    # 参数不合法、查询 Agent 失败、正常、次数用完，混在同一轮
    "mixed-failures": (lambda: [tools(call(1, "query_data", wrong="x"), call(2, "query_data", request="会失败的请求"),
                                      call(3, "query_data", request="全部的销售订单数"), call(4, "query_data", request="这个请求来不及执行")),
                                answer("根据已有结果作答。")],
                       lambda: {"全部的销售订单数": [tools(call(1, "submit_query", **ORDERS)), answer("交出了。")]}, 3),
    # 跨两轮取数：第二轮的编号接着第一轮
    "two-rounds": (lambda: [tools(call(1, "query_data", request="各国 GMV")), tools(call(2, "query_data", request="全部的销售订单数")),
                            answer("各国 GMV 和订单数见上。")],
                   lambda: {"各国 GMV": [tools(call(1, "submit_query", **BY_COUNTRY)), answer("交出了。")],
                            "全部的销售订单数": [tools(call(1, "submit_query", **ORDERS)), answer("交出了。")]}, 5),
}


def comparable_multi(run) -> dict:
    data = comparable(run)
    for sub in data["sub_runs"]:
        for key in ("run_id", "started_at", "seconds"):
            sub.pop(key)
        for step in sub["steps"]:
            step.pop("ms")
    return data


@pytest.fixture(scope="module")
def demo() -> tuple:
    con = connect_frame(read_normalized_csv(cli.DEMO_DATA))
    _ensure(con)
    import tempfile

    folder = Path(tempfile.mkdtemp())
    return con, build_database(load_config(SEMANTIC_DIR / "retail.yaml"), con, folder / "q.duckdb")


def run_multi(name: str, demo: tuple, **kwargs) -> tuple[tuple, tuple]:
    main_script, query_scripts, max_tool_calls = MULTI[name]
    con, db = demo
    results = []
    for runner in (run_agent, langgraph_version.run_agent_langgraph):
        main, queries = FakeLLM(replies=main_script()), ByQuestion(query_scripts())
        extra = kwargs if runner is not run_agent else {}
        run = runner(QUESTION, llm=main, con=con, max_tool_calls=max_tool_calls,
                     tool_contexts={"query_data": QueryService(llm=queries, db=db)}, **extra)
        results.append((comparable_multi(run), main.received, queries.received))
    return results[0], results[1]


@pytest.mark.parametrize("name", list(MULTI))
def test_multi_agent_same_behaviour_as_hand_written(name: str, demo: tuple) -> None:
    (hand, hand_main, hand_queries), (graph, graph_main, graph_queries) = run_multi(name, demo)
    assert graph == hand
    assert graph_main == hand_main  # 主 Agent 每一轮收到的一样
    assert graph_queries == hand_queries  # 每个查询 Agent 每一轮收到的也一样


def test_multi_agent_scenarios_cover_what_they_claim(demo: tuple) -> None:
    (parallel, _, _), _ = run_multi("parallel-round", demo)
    assert [s["tool"] for s in parallel["steps"]] == ["query_data", "metric_summary", "query_data"]
    assert [json.loads(m["content"]).get("query_run") for m in parallel["messages"] if m["role"] == "tool"] == [1, None, 2]
    assert [sub["question"] for sub in parallel["sub_runs"]] == ["各国 GMV", "2011 年的毛利率"]
    (mixed, _, _), _ = run_multi("mixed-failures", demo)
    errors = [s["error"] or "" for s in mixed["steps"]]
    assert "参数不合法" in errors[0] and errors[1].startswith("取数没有完成") and errors[2] == "" and "次数已用完" in errors[3]
    assert len(mixed["sub_runs"]) == 2 and json.loads(mixed["messages"][5]["content"])["query_run"] == 2  # 失败的那次也有记录、占编号
    assert mixed["budget_exhausted"]
    (two, _, _), _ = run_multi("two-rounds", demo)
    assert [s["round"] for s in two["steps"]] == [1, 2] and len(two["sub_runs"]) == 2


def test_sequential_mode_gives_the_same_result(demo: tuple) -> None:
    _, (parallel, _, _) = run_multi("parallel-round", demo)
    _, (sequential, _, _) = run_multi("parallel-round", demo, parallel=False)
    assert parallel == sequential


def test_query_agents_really_run_in_parallel(demo: tuple) -> None:
    """两个取数：一个要调两次模型、一个调一次，每次 0.4 秒。按顺序约 1.2 秒，并行约 0.8 秒（取决于慢的那个）。"""
    import time

    con, db = demo
    timings = {}
    for parallel in (False, True):
        main, _queries, max_tool_calls = MULTI["parallel-round"]
        slow = ByQuestion(MULTI["parallel-round"][1](), delay=0.4)
        start = time.perf_counter()
        langgraph_version.run_agent_langgraph(QUESTION, llm=FakeLLM(replies=main()), con=con, max_tool_calls=max_tool_calls,
                                              tool_contexts={"query_data": QueryService(llm=slow, db=db)}, parallel=parallel)
        timings[parallel] = time.perf_counter() - start
    assert timings[False] > 1.15 and timings[True] < timings[False] - 0.3, timings


def test_the_graph_can_be_drawn() -> None:
    text = langgraph_version.graph_mermaid()
    assert "query_agent" in text and "fixed_tools" in text and "collect" in text
