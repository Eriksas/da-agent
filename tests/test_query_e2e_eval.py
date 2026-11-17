"""端到端评测里的取数（M19b）：agent_query 组、参考 SQL 算出的期望值、嵌套运行的成本、接口报错重跑。"""

from pathlib import Path
from typing import Any

import pytest
from test_evaluation import CHECKS_OK, FixtureConnections, fake_run

from da_agent.evaluation import CONDITIONS, Case, Connections, expected_sql_values, load_cases, render_summary, run_eval, score
from da_agent.llm import FakeLLM, LLMError, LLMReply, ToolCall
from da_agent.paths import ROOT

WEEK = Case(id="w", category="取数", question="2011-W02 哪个国家 GMV 最高？", expects_skill=False,
            expect_sql=("SELECT sum(amount) AS gmv, count(DISTINCT invoice) AS orders FROM sales_enriched WHERE week = '2011-W02' "
                        "AND country = 'Germany'", "SELECT sum(amount) / 145 AS share FROM sales WHERE country = 'Germany'"))


def test_the_question_files_are_valid_and_separate() -> None:
    held, dev = load_cases(ROOT / "eval" / "query_e2e.yaml"), load_cases(ROOT / "eval" / "query_e2e_dev.yaml")
    assert len(held) == 10 and len(dev) == 3
    others = {c.id for c in load_cases()} | {c.id for c in load_cases(ROOT / "eval" / "holdout.yaml")}
    assert not ({c.id for c in held} & {c.id for c in dev}) and not ({c.id for c in held} | {c.id for c in dev}) & others
    assert all(c.expect_sql for c in held if c.id != "e-margin") and all(not c.expects_skill for c in held + dev)
    assert "agent_query" in CONDITIONS


def test_expected_sql_values_are_computed_on_the_case_data() -> None:
    con = FixtureConnections().con
    assert expected_sql_values(con, WEEK) == [60.0, 1.0, pytest.approx(60 / 145)]
    assert expected_sql_values(con, Case(id="n", category="x", question="q")) == []
    with pytest.raises(ValueError, match="要返回 1 行"):
        expected_sql_values(con, Case(id="b", category="x", question="q", expect_sql=("SELECT country FROM sales GROUP BY 1",)))


@pytest.mark.parametrize(("answer", "passed"), [
    ("德国 GMV 60.00，占 41.38%。", True),
    ("德国 GMV 60，占 41.4%。", True),          # 按答案自己的精度比
    ("德国 GMV 61.00，占 41.38%。", False),     # 数不对
    ("德国 GMV 60.00。", False),                # 少了一个数
])
def test_every_reference_value_must_appear(answer: str, passed: bool) -> None:
    result = score(WEEK, fake_run(answer), CHECKS_OK, None, [60.0, 60 / 145])
    assert result["passed"] is passed and len(result["expected_sql_values"]) == 2
    assert result["value_found"] is passed


class QueryConnections(FixtureConnections):
    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.query_dir = folder


def _call(name: str, arguments: dict[str, Any], call_id: str = "c1") -> LLMReply:
    return LLMReply(content="", tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),))


SUBMIT = {"sql": "SELECT round(sum(gmv_amount), 2) AS gmv FROM order_lines WHERE week = '2011-W02' AND country = 'Germany'",
          "columns": {"gmv": "德国 GMV"}, "definition": "2011-W02 德国 GMV"}


def test_agent_query_condition_end_to_end(tmp_path: Path) -> None:
    case = Case(id="g", category="取数", question="2011-W02 德国 GMV 是多少？", expects_skill=False,
                expect_sql=("SELECT sum(amount) AS gmv FROM sales_enriched WHERE week = '2011-W02' AND country = 'Germany'",))

    def make_llm(case: Case, condition: str, i: int) -> FakeLLM:
        answer = LLMReply(content="2011-W02 德国 GMV 为 60.00。")
        if condition == "agent_skill":  # 原来的 Agent 没有取数工具：只能说工具答不了
            return FakeLLM(replies=[LLMReply(content="固定工具只能按周看全站，答不了某一国的 GMV。")])
        # 主 Agent 和查询 Agent 共用一个模型客户端：剧本按调用顺序排
        return FakeLLM(replies=[_call("query_data", {"request": "2011-W02 德国 GMV"}), _call("submit_query", SUBMIT),
                                LLMReply(content="交出了。"), answer])

    summary = run_eval([case], ["agent_skill", "agent_query"], 1, make_llm, QueryConnections(tmp_path / "q"), tmp_path / "out",
                       max_tool_calls=5, progress=lambda _: None)
    stats = summary["by_condition"]
    assert stats["agent_skill"]["passed"] == 0 and stats["agent_query"]["passed"] == 1
    assert stats["agent_query"]["runs_using_query"] == 1 and stats["agent_skill"]["runs_using_query"] == 0
    assert stats["agent_query"]["avg_query_calls"] == 1 and stats["agent_query"]["avg_query_tokens"] == 0  # 假模型不报 token
    assert summary["failures"][0]["missing_values"] == [60.0]
    assert (tmp_path / "q").glob("eval-*.duckdb")  # 取数用的数据库按这道题的数据建
    text = render_summary(summary, {"name": "t", "model": "fake", "repeats": 1, "finished_at": "now"})
    assert "## 取数（query_data）" in text and "没有给出正确数值 60.0" in text


class FlakyLLM(FakeLLM):
    """前几次调用报限流错误，之后按剧本回复。"""

    def __init__(self, replies: list[LLMReply], failures: int) -> None:
        super().__init__(replies=replies)
        self.failures = failures

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMReply:
        if self.failures:
            self.failures -= 1
            raise LLMError("RateLimitError: Error code: 429")
        return super().chat(messages, tools)


def test_rate_limits_are_retried_not_scored(tmp_path: Path) -> None:
    case = Case(id="r", category="查数", question="2011-W02 的 GMV？", expects_skill=False)
    flaky = FlakyLLM([LLMReply(content="2011-W02 的 GMV 下降了。")], failures=2)
    waits: list[float] = []
    summary = run_eval([case], ["agent"], 1, lambda *_: flaky, FixtureConnections(), tmp_path, max_tool_calls=5,
                       progress=lambda _: None, wait=waits.append)
    assert waits == [45, 90] and summary["by_condition"]["agent"]["api_retries"] == 2 and summary["by_condition"]["agent"]["passed"] == 1
    other = FakeLLM(replies=[])  # 别的错误（这里是剧本用完）不重跑
    summary = run_eval([case], ["agent"], 1, lambda *_: other, FixtureConnections(), tmp_path / "b", max_tool_calls=5,
                       progress=lambda _: None, wait=waits.append)
    assert waits == [45, 90] and summary["by_condition"]["agent"]["passed"] == 0


def test_query_data_uses_the_injected_data(tmp_path: Path) -> None:
    """注入了异动的题：取数看到的也必须是注入后的数据，不然两边对不上。"""
    from test_metrics import ROWS

    from da_agent.cleaning import connect_frame
    from da_agent.evaluation import inject
    from da_agent.sqlrun import run_select
    from helpers import make_frame

    spec = {"drop_country": "Germany", "week": "2011-W02"}
    connections = Connections(query_dir=tmp_path)
    case = Case(id="i", category="注入", question="q", inject=spec)
    connections._cache[connections._key(case)] = connect_frame(inject(make_frame(ROWS), spec))
    db = connections.query_db(case)
    con = db.connect()
    rows = run_select(con, "SELECT count(*) AS n FROM order_lines WHERE country = 'Germany'")["rows"]
    con.close()
    assert rows == [{"n": 0}] and connections.query_db(case) is db  # 同一份数据只建一次


def test_rescore_keeps_reference_values_and_retries(tmp_path: Path) -> None:
    """中断后补跑、合并结果时用 rescore 重新汇总：参考 SQL 的期望值和重跑次数都要保留。"""
    import json

    from da_agent.evaluation import rescore

    case = Case(id="g", category="取数", question="2011-W02 德国 GMV 是多少？", expects_skill=False,
                expect_sql=("SELECT sum(amount) AS gmv FROM sales_enriched WHERE week = '2011-W02' AND country = 'Germany'",))
    flaky = FlakyLLM([LLMReply(content="2011-W02 德国 GMV 为 61.00。")], failures=1)
    run_eval([case], ["agent"], 1, lambda *_: flaky, FixtureConnections(), tmp_path, max_tool_calls=5,
             progress=lambda _: None, wait=lambda _: None)
    summary = rescore(tmp_path, [case], FixtureConnections())
    assert summary["failures"][0]["missing_values"] == [60.0] and summary["by_condition"]["agent"]["api_retries"] == 1
    assert json.loads((tmp_path / "runs" / "g" / "agent-1" / "score.json").read_text(encoding="utf-8"))["api_retries"] == 1
