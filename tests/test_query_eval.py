"""查询 Agent 的评测（M19）：打分规则、题目文件、整个流程（假模型）。"""

import json
from pathlib import Path

import pytest
from test_metrics import ROWS

from da_agent.cleaning import connect_frame
from da_agent.dataset import PARQUET_PATH
from da_agent.llm import FakeLLM, LLMReply, ToolCall
from da_agent.metrics import _ensure, drilldown, metric_summary
from da_agent.query_eval import (
    QueryCase,
    close,
    load_plan,
    reference_connection,
    reference_rows,
    render_summary,
    run_query_eval,
    score,
    summarize,
    tasks,
)
from da_agent.semantic import SEMANTIC_DIR, build_database, load_config
from helpers import make_frame

COUNTRIES = QueryCase("countries", "retail", "tool", "2011-W02 各国 GMV", keys=("country",), values=("gmv", "share"), rates=("share",),
                      reference_sql="SELECT country, sum(amount) AS gmv, sum(amount) / sum(sum(amount)) OVER () AS share "
                                    "FROM sales_enriched WHERE week = '2011-W02' GROUP BY 1")
TOTAL = QueryCase("total", "retail", "tool", "2011-W02 的 GMV 和订单数", values=("gmv", "orders"),
                  reference_sql="SELECT sum(amount) AS gmv, count(DISTINCT invoice) AS orders FROM sales_enriched WHERE week = '2011-W02'")
MARGIN = QueryCase("margin", "retail", "decline", "毛利率是多少", kind="decline")
EXPECTED = [{"country": "United Kingdom", "gmv": 55.0, "share": 0.37931}, {"country": "France", "gmv": 30.0, "share": 0.206897},
            {"country": "Germany", "gmv": 60.0, "share": 0.413793}]


def answered(rows: list[dict]) -> dict:
    return {"status": "answered", "rows": rows, "row_count": len(rows)}


@pytest.mark.parametrize(("result", "expected", "rate", "same"), [
    (309381.73, 309381.7299, False, True),   # 四舍五入到 2 位
    (309382, 309381.73, False, True),        # 取整
    (309381.7, 309381.73, False, True),
    (309300, 309381.73, False, False),       # 差得比四舍五入多
    (0.1127, 0.112654, True, True),          # 比率 4 位小数
    (11.27, 0.112654, True, True),           # 写成百分数
    (11.27, 0.112654, False, False),         # 不是比率的列不认百分数
    (0.11, 0.112654, True, False),           # 只留 2 位小数：差 2% 以上，不算
    (663, 663.0, False, True), (662, 663.0, False, False),
    (0.0, 0.0, False, True), (0.001, 0.0, False, False),
    ("663", 663.0, False, False), (None, 1.0, False, False), (True, 1.0, False, False),
])
def test_close_allows_rounding_but_not_a_different_number(result: object, expected: float, rate: bool, same: bool) -> None:
    assert close(result, expected, rate) is same


def test_keyed_values_must_sit_in_the_right_row() -> None:
    good = [{"country": "Germany", "gmv": 60.0, "pct": 41.38}, {"country": "united kingdom", "gmv": 55, "pct": 37.93},
            {"country": "France", "gmv": 30.0, "pct": 20.69}, {"country": "合计", "gmv": 145.0, "pct": 100.0}]
    assert score(COUNTRIES, EXPECTED, answered(good)) == {"outcome": "correct", "detail": ""}
    swapped = [dict(good[0], gmv=55.0), dict(good[1], gmv=60.0), good[2]]  # 数都在，但安在了别的国家上
    verdict = score(COUNTRIES, EXPECTED, answered(swapped))
    assert verdict["outcome"] == "wrong" and "United Kingdom 的 gmv 应为 55.0" in verdict["detail"]
    assert "没有 France 这一行" in score(COUNTRIES, EXPECTED, answered(good[:2]))["detail"]


def test_unkeyed_values_may_be_anywhere_but_the_table_must_be_small() -> None:
    expected = [{"gmv": 145.0, "orders": 5}]
    assert score(TOTAL, expected, answered([{"week": "2011-W02", "gmv": 145.0}, {"week": "订单", "gmv": 5}]))["outcome"] == "correct"
    assert score(TOTAL, expected, answered([{"gmv": 145.0, "orders": 4}]))["detail"] == "orders 应为 5"
    dump = [{"week": f"2011-W{n:02d}", "gmv": 140.0 + n, "orders": n} for n in range(1, 8)]  # 把每一周都倒出来碰运气
    verdict = score(TOTAL, expected, answered(dump))
    assert verdict == {"outcome": "wrong", "detail": "结果有 7 行，参考结果只有 1 行"}


def test_declining_is_right_only_when_there_is_no_answer() -> None:
    declined = {"status": "declined", "message": "数据里没有成本"}
    assert score(MARGIN, [], declined)["outcome"] == "correct"
    assert score(MARGIN, [], answered([{"margin": 0.3}])) == {"outcome": "wrong", "detail": "数据里没有答案，却交出了结果"}
    assert score(TOTAL, [{"gmv": 145.0, "orders": 5}], declined) == {"outcome": "declined", "detail": "数据里没有成本"}
    assert score(TOTAL, [], {"status": "failed", "message": "模型报错"}) == {"outcome": "failed", "detail": "模型报错"}


def test_the_committed_plan_is_complete() -> None:
    plan = load_plan()
    assert len(plan["dev"]) == 10 and len(plan["holdout"]) == 36
    groups = [case.group for case in plan["holdout"]]
    assert {g: groups.count(g) for g in set(groups)} == {"tool": 12, "beyond": 10, "decline": 3, "csv": 11}
    assert not {case.id for case in plan["dev"]} & {case.id for case in plan["holdout"]}
    assert not {case.request for case in plan["dev"]} & {case.request for case in plan["holdout"]}
    held = tasks(plan, "holdout", 2)
    assert len(held) == 24 * 2 * 2 + 12 * 2  # 电商题在两份配置上各跑 2 次，CSV 题跑 2 次
    assert {condition for case, condition, _ in held if case.data == "retail"} == {"retail", "retail_raw"}
    assert all(condition == "retail" for case, condition, _ in tasks(plan, "dev", 1) if case.data == "retail")


def test_a_plan_with_holes_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "cases.yaml"
    bad.write_text("datasets: {}\ndev:\n  - {id: a, data: retail, group: tool, request: 多少？}\nholdout: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺参考 SQL"):
        load_plan(bad)
    bad.write_text("datasets: {}\ndev:\n  - {id: a, data: retail, group: tool, request: 多少？, reference_sql: SELECT 1 AS x, "
                   "values: [x], rates: [y]}\nholdout: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rates 必须是 values 里的列"):
        load_plan(bad)


def _call(name: str, arguments: dict) -> LLMReply:
    return LLMReply(content="", tool_calls=(ToolCall(id="c1", name=name, arguments=arguments),))


def test_the_whole_flow_with_scripted_models(tmp_path: Path) -> None:
    base = connect_frame(make_frame(ROWS))
    _ensure(base)
    db = build_database(load_config(SEMANTIC_DIR / "retail.yaml"), base, tmp_path / "retail.duckdb")
    right = {"sql": "SELECT round(sum(gmv_amount), 2) AS gmv, count(DISTINCT order_id) AS orders FROM order_lines WHERE week = '2011-W02'",
             "columns": {"gmv": "GMV", "orders": "订单数"}, "definition": "2011-W02 的 GMV 和订单数"}
    wrong = right | {"sql": right["sql"].replace("gmv_amount", "amount")}  # 把取消的 5 也加进去了：150
    scripts = iter([
        [],  # 第一次调用模型就报错（比如被限流）：等一会儿重跑，不算它没答上来
        [_call("run_sql", {"sql": "DROP TABLE order_lines", "purpose": "试"}), _call("submit_query", right), LLMReply(content="完成。")],
        [_call("submit_query", wrong), LLMReply(content="完成。")],
        [LLMReply(content="数据里没有成本，算不了毛利率。")],
        [], [], [], [], [],  # 一直报错：重跑 4 次后记为没跑完
    ])
    plan = {"datasets": {}, "dev": [TOTAL, MARGIN]}
    waits: list[float] = []
    rows = run_query_eval(plan, "dev", 2, lambda: FakeLLM(replies=next(scripts)), tmp_path / "out", workers=1,
                          databases={"retail": db}, references={"retail": base}, wait=waits.append)
    assert [(r["case"], r["repeat"], r["outcome"], r["api_retries"]) for r in rows] == [
        ("total", 1, "correct", 1), ("total", 2, "wrong", 0), ("margin", 1, "correct", 0), ("margin", 2, "failed", 4)]
    assert waits == [45, 45, 90, 135, 180]
    assert rows[0]["blocked"] == 1 and rows[0]["metrics_used"] == ["gmv", "orders"] and rows[1]["detail"] == "gmv 应为 145.0"
    saved = json.loads((tmp_path / "out" / "runs" / "total" / "retail-2" / "score.json").read_text(encoding="utf-8"))
    assert saved["outcome"] == "wrong" and saved["expected"] == [{"gmv": 145.0, "orders": 5}]
    assert (tmp_path / "out" / "runs" / "margin" / "retail-1" / "run.json").exists()
    summary = summarize(rows)
    assert summary["table"]["retail/tool"] | {"avg_seconds": 0} == {
        "runs": 2, "correct": 1, "wrong": 1, "declined": 0, "failed": 0, "avg_tokens": 0, "avg_seconds": 0}
    assert summary["table"]["retail/合计"]["correct"] == 2 and summary["blocked_statements"] == 1 and summary["api_retries"] == 5
    assert summary["cases"]["total"] == {"retail": ["correct", "wrong"]}
    text = render_summary(summary, {"name": "t", "split_label": "开发题", "model": "fake", "repeats": 2, "finished_at": "now"})
    assert "| retail | 固定工具也能答 | 2 | 1/2（50.0%） | 1 | 0 | 0 |" in text and "被检查挡下的语句" in text and "重跑：5 次" in text
    assert "| total | retail | 2 | 交了但不对 | gmv 应为 145.0 |" in text and "| margin | retail | 2 | 没跑完 |" in text


@pytest.mark.skipif(not PARQUET_PATH.exists(), reason="本地没有准备数据（da-agent prepare-data）")
def test_reference_answers_agree_with_the_fixed_tools() -> None:
    """“固定工具也能答”的题：参考 SQL 的结果和固定工具（metric_summary、drilldown）给出的数一致。"""
    plan = load_plan()
    cases = {case.id: case for case in plan["holdout"]}
    con = reference_connection("retail", plan)

    def tool(week: str, compare: str = "wow") -> dict:
        return {m["metric"]: m for m in metric_summary(con, week, compare, ["gmv", "net_sales", "orders", "aov", "active_customers",
                                                                           "new_customers", "cancelled_amount", "cancel_rate",
                                                                           "trading_days", "gmv_per_day"])["metrics"]}

    def ref(case_id: str) -> list[dict]:
        return reference_rows(con, cases[case_id])

    w48, w47 = tool("2011-W48"), tool("2011-W47")
    assert ref("a-gmv-net")[0] == pytest.approx({"gmv": w48["gmv"]["current"], "net_sales": w48["net_sales"]["current"]}, abs=0.01)
    assert ref("a-orders-customers-aov")[0] == pytest.approx(
        {"orders": w47["orders"]["current"], "active_customers": w47["active_customers"]["current"], "aov": w47["aov"]["current"]}, abs=0.01)
    net = ref("a-net-wow")[0]
    assert net["change"] == pytest.approx(w48["net_sales"]["change"], abs=0.01)
    assert net["change_pct"] == pytest.approx(w48["net_sales"]["change_pct"], abs=1e-4)
    w50 = tool("2010-W50")
    assert ref("a-cancel")[0] == pytest.approx({"cancelled_amount": w50["cancelled_amount"]["current"],
                                                "cancel_rate": w50["cancel_rate"]["current"]}, abs=0.01)
    assert ref("a-new-share")[0]["new_customers"] == tool("2011-W36")["new_customers"]["current"]
    yoy = tool("2011-W48", "yoy")
    assert ref("a-orders-yoy")[0] == pytest.approx({"cur": yoy["orders"]["current"], "base": yoy["orders"]["base"],
                                                    "change_pct": yoy["orders"]["change_pct"]}, abs=1e-4)
    w49 = tool("2011-W49")
    assert ref("a-partial-week")[0] == pytest.approx({"gmv": w49["gmv"]["current"], "trading_days": w49["trading_days"]["current"]}, abs=0.01)
    days = {row["week"]: row for row in ref("a-trading-days")}
    assert days["2011-W22"]["gmv_per_day"] == pytest.approx(tool("2011-W22")["gmv_per_day"]["current"], abs=0.01)
    by_country = {s["segment"]: s for s in drilldown(con, "2011-W48", "wow", "gmv", "country", 20)["segments"]}
    for row in ref("a-top-countries"):
        assert row["gmv"] == pytest.approx(by_country[row["country"]]["current"], abs=0.01)
    drops = {s["segment"]: s for s in drilldown(con, "2011-W31", "wow", "gmv", "country", 20)["segments"]}
    for row in ref("a-country-drops"):
        assert row["change"] == pytest.approx(drops[row["country"]]["change"], abs=0.01)
    types = {s["segment"]: s for s in drilldown(con, "2011-W48", "wow", "gmv", "customer_type", 5)["segments"]}
    for row in ref("a-customer-types"):
        assert row["gmv"] == pytest.approx(types[row["customer_type"]]["current"], abs=0.01)
    products = {s["segment"]: s for s in drilldown(con, "2011-W41", "wow", "gmv", "product", 20)["segments"]}
    shared = [row for row in ref("a-product-gains") if row["stock_code"] in products]  # 下钻按变化的绝对值排，只对两边都有的商品
    assert len(shared) >= 2
    for row in shared:
        assert row["change"] == pytest.approx(products[row["stock_code"]]["change"], abs=0.01)
