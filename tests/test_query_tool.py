"""原来的 Agent 调用查询 Agent（M19b）：query_data 工具、提示词、数字核查、嵌套运行的记录。用两个假模型按剧本回放。"""

import json
from dataclasses import asdict

import duckdb
import pytest
from test_metrics import ROWS

from da_agent.agent import build_system_prompt, run_agent
from da_agent.analyst import QueryService
from da_agent.checks import check_run, number_check
from da_agent.cleaning import connect_frame
from da_agent.llm import FakeLLM, LLMReply, ToolCall
from da_agent.metrics import _ensure
from da_agent.semantic import SEMANTIC_DIR, QueryDB, build_database, load_config
from helpers import make_frame

SUBMIT = {"sql": "SELECT country, round(sum(gmv_amount), 2) AS gmv FROM order_lines WHERE week = '2011-W02' GROUP BY 1 ORDER BY 2 DESC",
          "columns": {"country": "国家", "gmv": "GMV（英镑）"}, "definition": "2011-W02 各国 GMV，共 3 个国家",
          "caveats": ["德国只有 1 单，可能是一次性的批发采购，金额 777.77"]}  # 提醒里的数字和原因都是查询 Agent 自己写的


@pytest.fixture(scope="module")
def base() -> duckdb.DuckDBPyConnection:
    con = connect_frame(make_frame(ROWS))
    _ensure(con)
    return con


@pytest.fixture(scope="module")
def db(base: duckdb.DuckDBPyConnection, tmp_path_factory: pytest.TempPathFactory) -> QueryDB:
    return build_database(load_config(SEMANTIC_DIR / "retail.yaml"), base, tmp_path_factory.mktemp("q") / "retail.duckdb")


def call(name: str, arguments: dict, call_id: str = "c1") -> LLMReply:
    return LLMReply(content="", tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),))


def test_the_prompt_and_tools_are_unchanged_without_the_query_tool(base: duckdb.DuckDBPyConnection) -> None:
    plain = build_system_prompt(base, 15)
    assert "query_data" not in plain and "{skills_catalog}\n\n## 必须遵守" not in plain and "\n\n## 必须遵守" in plain
    with_query = build_system_prompt(base, 15, query=True)
    assert "## 取数（query_data）" in with_query and "unverified_notes" in with_query
    assert with_query.replace(with_query[with_query.index("\n## 取数"):with_query.index("\n## 必须遵守")], "") == plain
    llm = FakeLLM(replies=[LLMReply(content="本周 GMV 为 145。")])
    run_agent("GMV？", llm=llm, con=base, max_tool_calls=5)
    assert "query_data" not in [t["function"]["name"] for t in llm.received[0]["tools"]]


def test_the_main_agent_gets_a_computed_table(base: duckdb.DuckDBPyConnection, db: QueryDB) -> None:
    analyst = FakeLLM(replies=[call("submit_query", SUBMIT), LLMReply(content="交出了各国 GMV。")])
    main = FakeLLM(replies=[call("query_data", {"request": "2011-W02 各国 GMV，从高到低"}),
                            LLMReply(content="2011-W02 德国 GMV 最高，为 60.00，英国 55.00，法国 30.00。")])
    service = QueryService(llm=analyst, db=db)
    run = run_agent("2011-W02 哪个国家卖得最多？", llm=main, con=base, max_tool_calls=5, tool_contexts={"query_data": service})
    assert run.status == "completed" and run.steps[0]["tool"] == "query_data" and run.steps[0]["ok"]
    assert "## 取数（query_data）" in main.received[0]["messages"][0]["content"]
    assert [t["function"]["name"] for t in main.received[0]["tools"]][-1] == "query_data"
    result = json.loads(run.messages[3]["content"])
    assert result["status"] == "answered" and result["rows"][0] == {"country": "Germany", "gmv": 60.0} and result["query_run"] == 1
    assert result["unverified_notes"]["caveats"] == SUBMIT["caveats"] and "不能当作出处" in result["unverified_notes"]["note"]
    assert "sql" in result and "definition" not in result  # 口径说明只在 unverified_notes 里
    assert len(run.sub_runs) == 1 and run.sub_runs[0]["domain"] == "analyst" and run.sub_runs[0]["question"] == "2011-W02 各国 GMV，从高到低"
    assert check_run(asdict(run))["numbers"]["ungrounded"] == []


def test_numbers_in_the_query_agents_notes_are_not_sources(base: duckdb.DuckDBPyConnection, db: QueryDB) -> None:
    analyst = FakeLLM(replies=[call("submit_query", SUBMIT), LLMReply(content="完成。")])
    main = FakeLLM(replies=[call("query_data", {"request": "2011-W02 各国 GMV，从高到低"}),
                            LLMReply(content="德国 GMV 60.00，是一笔 777.77 的批发采购。"),
                            LLMReply(content="德国 GMV 60.00，只有 1 单。")])  # 被退回后删掉了没有出处的数
    run = run_agent("哪个国家卖得最多？", llm=main, con=base, max_tool_calls=5,
                    tool_contexts={"query_data": QueryService(llm=analyst, db=db)})
    assert [m["text"] for m in run.repair["ungrounded"]] == ["777.77"] and run.answer == "德国 GMV 60.00，只有 1 单。"
    assert number_check("德国 GMV 60.00", run.messages)["ungrounded"] == []


def test_declined_and_failed_queries(base: duckdb.DuckDBPyConnection, db: QueryDB) -> None:
    analyst = FakeLLM(replies=[LLMReply(content="数据里没有成本，算不了毛利率。"), ])  # 第二次取数时它已经没有回复可给：模型报错
    main = FakeLLM(replies=[call("query_data", {"request": "2011-W02 的毛利率"}), call("query_data", {"request": "2011-W02 的 GMV"}, "c2"),
                            LLMReply(content="数据里没有成本，算不了毛利率。")])
    run = run_agent("毛利率多少？", llm=main, con=base, max_tool_calls=5, tool_contexts={"query_data": QueryService(llm=analyst, db=db)})
    declined = json.loads(run.messages[3]["content"])
    assert declined["status"] == "declined" and "rows" not in declined and declined["unverified_notes"]["reason"].startswith("数据里没有成本")
    assert not run.steps[1]["ok"] and run.steps[1]["error"].startswith("取数没有完成") and len(run.sub_runs) == 2


def test_a_query_tool_call_without_a_service_is_refused(base: duckdb.DuckDBPyConnection) -> None:
    """没有开启 query_data 时，模型硬要调用也不执行（工具不在这次运行的列表里）。"""
    main = FakeLLM(replies=[call("query_data", {"request": "2011-W02 各国 GMV"}), LLMReply(content="不取数了。")])
    run = run_agent("哪个国家卖得最多？", llm=main, con=base, max_tool_calls=5)
    assert run.steps[0]["error"].startswith("没有名为 query_data 的工具") and run.sub_runs == []
