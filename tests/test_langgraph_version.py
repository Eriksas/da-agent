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

from da_agent import cli  # noqa: E402
from da_agent.agent import run_agent  # noqa: E402
from da_agent.cleaning import connect_frame  # noqa: E402
from da_agent.company import connect_financials  # noqa: E402
from da_agent.dataset import read_normalized_csv  # noqa: E402
from da_agent.llm import FakeLLM, LLMReply, ToolCall  # noqa: E402
from test_company import write_key_facts  # noqa: E402

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
