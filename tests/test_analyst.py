"""查询 Agent（M19）：模型写 SQL，程序执行和检查；交出去的是程序算出来的表。用假模型按剧本回放。"""

from pathlib import Path

import pytest
from test_metrics import ROWS

from da_agent.analyst import MAX_TOOL_CALLS, build_prompt, final_result, run_query
from da_agent.cleaning import connect_frame
from da_agent.llm import FakeLLM, LLMReply, ToolCall
from da_agent.metrics import _ensure
from da_agent.semantic import SEMANTIC_DIR, QueryDB, build_database, load_config
from da_agent.tools import tool_specs
from helpers import make_frame

BY_COUNTRY = ("SELECT country, round(sum(gmv_amount), 2) AS gmv, count(DISTINCT order_id) AS orders FROM order_lines "
              "WHERE week = '2011-W02' GROUP BY 1 ORDER BY 2 DESC")
GOOD = {"sql": BY_COUNTRY, "columns": {"country": "国家", "gmv": "GMV（英镑）", "orders": "订单数"},
        "definition": "2011-W02 各国 GMV 和订单数，按 GMV 从高到低", "caveats": ["每个国家的订单都很少"]}


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> QueryDB:
    base = connect_frame(make_frame(ROWS))
    _ensure(base)
    return build_database(load_config(SEMANTIC_DIR / "retail.yaml"), base, tmp_path_factory.mktemp("q") / "retail.duckdb")


def call(name: str, arguments: dict, call_id: str = "c1") -> LLMReply:
    return LLMReply(content="", tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),))


def test_the_result_is_what_the_program_computed(db: QueryDB) -> None:
    llm = FakeLLM(replies=[call("run_sql", {"sql": BY_COUNTRY, "purpose": "看各国 GMV"}), call("submit_query", GOOD, "c2"),
                           LLMReply(content="交出了 2011-W02 各国的 GMV。")])
    out = run_query("2011-W02 各国的 GMV 是多少？", llm=llm, db=db)
    result = out["result"]
    assert result["status"] == "answered" and result["data"] == "retail" and result["sql"] == BY_COUNTRY
    assert result["rows"] == [{"country": "Germany", "gmv": 60.0, "orders": 1}, {"country": "United Kingdom", "gmv": 55.0, "orders": 2},
                              {"country": "France", "gmv": 30.0, "orders": 2}] and result["row_count"] == 3
    assert result["columns"] == [{"name": "country", "meaning": "国家"}, {"name": "gmv", "meaning": "GMV（英镑）"},
                                 {"name": "orders", "meaning": "订单数"}]
    assert result["metrics_used"] == ["gmv", "orders"]  # 口径表里的表达式原样出现在 SQL 里（包在 round 里也算）
    assert result["sql_runs"] == 1 and result["rejected"] == 0 and result["caveats"] == ["每个国家的订单都很少"]
    assert out["run"]["domain"] == "analyst" and out["run"]["repair"] is None and final_result(out["run"])["accepted"]
    sent = llm.received[0]
    assert [t["function"]["name"] for t in sent["tools"]] == ["run_sql", "submit_query"]
    assert "### 表 order_lines" in sent["messages"][0]["content"] and "`sum(gmv_amount)`" in sent["messages"][0]["content"]


def test_a_bad_submission_is_sent_back_with_reasons(db: QueryDB) -> None:
    empty = GOOD | {"sql": BY_COUNTRY.replace("2011-W02", "2011-W2")}
    unexplained = GOOD | {"columns": {"country": "国家", "total": "合计"}}
    llm = FakeLLM(replies=[call("submit_query", empty), call("submit_query", unexplained, "c2"), call("submit_query", GOOD, "c3"),
                           LLMReply(content="完成。")])
    out = run_query("2011-W02 各国的 GMV？", llm=llm, db=db)
    errors = [step["error"] for step in out["run"]["steps"]]
    assert "没有返回任何行" in errors[0] and "不要提交，直接用文字说明" in errors[0]
    assert "没有说明 ['gmv', 'orders']" in errors[1] and "多写了 ['total']" in errors[1] and errors[2] is None
    assert out["result"]["status"] == "answered" and out["result"]["rejected"] == 2


def test_too_many_rows_and_empty_values_are_refused(db: QueryDB, monkeypatch: pytest.MonkeyPatch) -> None:
    from da_agent import analyst

    monkeypatch.setattr(analyst, "run_select", lambda con, sql: __import__("da_agent.sqlrun", fromlist=["run_select"]).run_select(
        con, sql, max_rows=5))
    lines = {"sql": "SELECT invoice, amount FROM order_lines", "columns": {"invoice": "发票号", "amount": "金额"}, "definition": "全部明细"}
    nulls = {"sql": "SELECT sum(amount) AS total FROM order_lines WHERE week = '2015-W01'", "columns": {"total": "金额"},
             "definition": "2015 年第 1 周的金额"}
    out = run_query("给我全部明细", llm=FakeLLM(replies=[call("submit_query", lines), call("submit_query", nulls, "c2"),
                                                      LLMReply(content="数据只到 2011 年，没有 2015 年的记录。")]), db=db)
    errors = [step["error"] for step in out["run"]["steps"]]
    assert "结果超过" in errors[0] and "结果全是空值" in errors[1]
    assert out["result"]["status"] == "declined" and "没有 2015 年" in out["result"]["message"] and "rows" not in out["result"]


def test_unsafe_sql_is_returned_to_the_model_and_changes_nothing(db: QueryDB) -> None:
    llm = FakeLLM(replies=[call("run_sql", {"sql": "DROP TABLE order_lines", "purpose": "清理"}),
                           call("run_sql", {"sql": "SELECT * FROM read_csv('C:/Windows/win.ini')", "purpose": "读文件"}, "c2"),
                           call("metric_summary", {"week": "2011-W02"}, "c3"),  # 别的领域的工具不给用
                           call("submit_query", GOOD, "c4"), LLMReply(content="完成。")])
    out = run_query("2011-W02 各国的 GMV？", llm=llm, db=db)
    errors = [step["error"] for step in out["run"]["steps"]]
    assert "只能执行以 SELECT 或 WITH 开头" in errors[0] and "不能使用 read_csv" in errors[1] and "没有名为 metric_summary 的工具" in errors[2]
    assert out["result"]["status"] == "answered" and out["result"]["row_count"] == 3  # 表还在，数据没变
    assert out["result"]["rejected"] == 3


def test_declining_and_failing_are_told_apart(db: QueryDB) -> None:
    declined = run_query("这周的利润率是多少？", llm=FakeLLM(replies=[LLMReply(content="数据里没有成本，算不了利润率。")]), db=db)["result"]
    assert declined["status"] == "declined" and declined["message"] == "数据里没有成本，算不了利润率。" and declined["sql_runs"] == 0
    failed = run_query("GMV？", llm=FakeLLM(replies=[]), db=db)["result"]
    assert failed["status"] == "failed" and "假模型只预设了 0 条回复" in failed["message"]
    looping = FakeLLM(replies=[call("run_sql", {"sql": "SELECT 1 AS x", "purpose": "试"}, f"c{i}") for i in range(MAX_TOOL_CALLS + 3)])
    stuck = run_query("GMV？", llm=looping, db=db)
    assert stuck["result"]["status"] == "failed" and stuck["run"]["budget_exhausted"]  # 一直试、不提交：次数用完，没有结果
    assert sum(step["ok"] for step in stuck["run"]["steps"]) == MAX_TOOL_CALLS


def test_previews_are_short_but_say_how_many_rows_there_are(db: QueryDB, monkeypatch: pytest.MonkeyPatch) -> None:
    from da_agent import analyst

    monkeypatch.setattr(analyst, "PREVIEW_ROWS", 4)
    llm = FakeLLM(replies=[call("run_sql", {"sql": "SELECT invoice, amount FROM order_lines ORDER BY 1", "purpose": "看明细"}),
                           LLMReply(content="不提交。")])
    out = run_query("看一下明细", llm=llm, db=db)
    import json

    preview = json.loads(out["run"]["messages"][3]["content"])
    assert len(preview["rows"]) == 4 and preview["row_count"] == 11 and preview["note"] == "共 11 行，这里只显示前 4 行"


def test_tool_schema_and_prompt(db: QueryDB, tmp_path: Path) -> None:
    specs = {spec["function"]["name"]: spec["function"]["parameters"] for spec in tool_specs("analyst")}
    assert set(specs) == {"run_sql", "submit_query"} and specs["submit_query"]["required"] == ["sql", "columns", "definition"]
    prompt = build_prompt(db.catalog, 5)
    assert "最多调用 5 次工具" in prompt and "不超过 200 行" in prompt and "{" not in prompt.split("## 怎么做")[1]
