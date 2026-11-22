"""看板 Agent 作为主 Agent 的工具（M22）：主 Agent 写目标，看板 Agent 搭看板，交回页面、图和数据问题。用假模型回放。"""

import json
from dataclasses import asdict
from pathlib import Path

import duckdb
import pytest

from da_agent import cli
from da_agent.agent import build_system_prompt, run_agent
from da_agent.checks import number_check
from da_agent.cleaning import connect_frame
from da_agent.dashboard_tool import DashboardService
from da_agent.dataset import read_normalized_csv
from da_agent.llm import FakeLLM, LLMReply, ToolCall

DASHBOARD_SCRIPT = Path(cli.__file__).parents[2] / "fixtures" / "fake_llm" / "dashboard_financials.json"
GOAL = "给研究员看四家电商平台的盈利能力，判断谁的利润率更高"


def call(name: str, arguments: dict, call_id: str = "c1") -> LLMReply:
    return LLMReply(content="", tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),))


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return connect_frame(read_normalized_csv(cli.DEMO_DATA))


def service(tmp_path: Path, script: Path | None = DASHBOARD_SCRIPT) -> DashboardService:
    llm = FakeLLM.from_file(script) if script else FakeLLM(replies=[])
    return DashboardService(llm=llm, source="financials", runs_dir=tmp_path / "dashboards")


def test_the_main_agent_hands_the_dashboard_to_the_dashboard_agent(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    main = FakeLLM(replies=[call("build_dashboard", {"goal": GOAL}), LLMReply(content="看板已经生成，页面位置见上。")])
    run = run_agent("帮我搭个看板，给研究员看四家平台的盈利能力", llm=main, con=con, max_tool_calls=5,
                    tool_contexts={"build_dashboard": service(tmp_path)})
    assert run.status == "completed" and run.steps[0]["tool"] == "build_dashboard" and run.steps[0]["ok"]
    sent = main.received[0]
    assert "## 搭看板（build_dashboard）" in sent["messages"][0]["content"]
    assert [t["function"]["name"] for t in sent["tools"]][-1] == "build_dashboard"
    result = json.loads(run.messages[3]["content"])
    assert result["status"] == "built" and Path(result["page"]).exists() and result["title"] == "电商平台盈利能力看板"
    assert [(c["title"], c["type"]) for c in result["charts"]][0][1] == "kpi" and len(result["charts"]) == 3
    assert result["unverified_notes"]["notes"][1] == "阿里巴巴的财年截至 3 月 31 日，其他三家是自然年。"
    assert result["dashboard_run"] == 1 and Path(result["page"]).parent.parent == tmp_path / "dashboards"
    assert len(run.sub_runs) == 1 and run.sub_runs[0]["domain"] == "dashboard" and run.sub_runs[0]["question"].endswith(GOAL)


def test_numbers_in_the_dashboard_agents_notes_are_not_sources(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    main = FakeLLM(replies=[call("build_dashboard", {"goal": GOAL}), LLMReply(content="看板做好了。")])
    run = run_agent("搭个看板", llm=main, con=con, max_tool_calls=5, tool_contexts={"build_dashboard": service(tmp_path)})
    assert number_check("阿里巴巴的财年截至 3 月 31 日。", run.messages)["ungrounded"][0]["text"] == "31"  # 来自看板 Agent 的说明


def test_a_dashboard_that_was_not_built_is_reported_as_an_error(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    main = FakeLLM(replies=[call("build_dashboard", {"goal": GOAL}), LLMReply(content="看板没有做出来。")])
    run = run_agent("搭个看板", llm=main, con=con, max_tool_calls=5, tool_contexts={"build_dashboard": service(tmp_path, None)})
    assert not run.steps[0]["ok"] and run.steps[0]["error"].startswith("看板 Agent 没有交出通过检查的方案")
    assert len(run.sub_runs) == 1  # 没做出来的那次也留下完整记录


def test_prompt_sections_follow_the_enabled_tools(con: duckdb.DuckDBPyConnection) -> None:
    plain = build_system_prompt(con, 15)
    both = build_system_prompt(con, 15, query=True, dashboard=True)
    only = build_system_prompt(con, 15, dashboard=True)
    assert "build_dashboard" not in plain and "query_data" not in plain
    assert "## 取数（query_data）" in both and "## 搭看板（build_dashboard）" in both
    assert "## 搭看板（build_dashboard）" in only and "## 取数（query_data）" not in only
    section = only[only.index("\n## 搭看板"):only.index("\n## 必须遵守")]
    assert only.replace(section, "") == plain  # 只多了这一段


def test_cli_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(cli, "ask", lambda *args, **kwargs: calls.append(kwargs) or 0)
    assert cli.main(["ask", "搭个看板", "--with-dashboard"]) == 0 and cli.main(["ask", "上周 GMV 为什么变化？"]) == 0
    assert [c["with_dashboard"] for c in calls] == [True, False]


def test_the_record_is_serialisable(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    main = FakeLLM(replies=[call("build_dashboard", {"goal": GOAL}), LLMReply(content="看板做好了。")])
    run = run_agent("搭个看板", llm=main, con=con, max_tool_calls=5, tool_contexts={"build_dashboard": service(tmp_path)})
    assert json.loads(json.dumps(asdict(run), ensure_ascii=False, default=str))["sub_runs"][0]["status"] == "completed"


def test_routing_is_scored() -> None:
    from da_agent.evaluation import Case, score

    checks = {"numbers": {"checked": 0, "ungrounded": []}, "process": {"skills": []}}

    def run_with(*steps: tuple[str, bool]) -> dict:
        return {"answer": "好的。", "status": "completed", "seconds": 1.0, "usage": {"llm_calls": 1, "prompt_tokens": 1, "completion_tokens": 1},
                "steps": [{"tool": tool, "ok": ok} for tool, ok in steps]}

    wants = Case(id="d", category="看板", question="搭个看板", expects_skill=False, require_tools=("build_dashboard",))
    asks = Case(id="q", category="提问", question="上周 GMV？", expects_skill=False, forbid_tools=("build_dashboard",))
    assert score(wants, run_with(("build_dashboard", True)), checks, None)["passed"] is True
    failed = score(wants, run_with(("build_dashboard", False)), checks, None)  # 调了但没搭出来，也不算
    assert failed["passed"] is False and failed["tools_missing"] == ["build_dashboard"]
    assert score(asks, run_with(("metric_summary", True)), checks, None)["passed"] is True
    wrong = score(asks, run_with(("build_dashboard", False)), checks, None)  # 不该搭看板的，试了也算错
    assert wrong["passed"] is False and wrong["tools_forbidden"] == ["build_dashboard"]


def test_the_dashboard_condition_end_to_end(tmp_path: Path) -> None:
    from test_evaluation import FixtureConnections

    from da_agent.evaluation import Case, render_summary, run_eval

    case = Case(id="d", category="看板", question="搭个看板给研究员看盈利能力", expects_skill=False,
                require_tools=("build_dashboard",), must_mention=(("dashboard.html", "看板"),))
    connections = FixtureConnections()
    connections.dashboard_source = "financials"
    connections.query_dir = tmp_path / "q"
    dashboard_replies = FakeLLM.from_file(DASHBOARD_SCRIPT).replies

    def make_llm(case: Case, condition: str, i: int) -> FakeLLM:  # 主 Agent 和看板 Agent 共用一个客户端：剧本按调用顺序排
        return FakeLLM(replies=[call("build_dashboard", {"goal": GOAL}), *dashboard_replies, LLMReply(content="看板做好了。")])

    summary = run_eval([case], ["agent_dashboard"], 1, make_llm, connections, tmp_path / "out", max_tool_calls=5, progress=lambda _: None)
    stats = summary["by_condition"]["agent_dashboard"]
    assert stats["passed"] == 1 and stats["runs_using_dashboard"] == 1
    pages = list((tmp_path / "out" / "runs" / "d" / "agent_dashboard-1" / "dashboards").glob("*/dashboard.html"))
    assert len(pages) == 1  # 看板存在这次评测运行自己的目录里
    text = render_summary(summary, {"name": "t", "model": "fake", "repeats": 1, "finished_at": "now"})
    assert "用到搭看板的运行" in text
