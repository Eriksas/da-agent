"""看板 Agent 和查询 Agent 共用一份口径配置（M23）。

- 配置里每个指标的看板写法，和它的 SQL 写法（查询 Agent 用的、已经和固定工具核对过的）算出来一样：整体、逐周、逐国家、逐月；
- 看板里和配置同名的指标，定义必须一致；
- 看板能看到配置里算好的列（新客 / 老客、周、月），按 GMV 排名时提示被取消抵消的分组；
- 配置写错时直接报错；查询 Agent 看到的数据说明不变。
"""

from pathlib import Path

import duckdb
import pytest
from test_metrics import ROWS

from da_agent import semantic
from da_agent.autodash.build import rerender, write_dashboard
from da_agent.autodash.compile import DashboardProblems, _metric_sql, compile_dashboard, preview_chart
from da_agent.autodash.sources import connect_source, open_semantic, profile_table, semantic_source, source_of
from da_agent.autodash.spec import Chart, DashboardSpec, Metric, configured_metrics, definition_problems
from da_agent.cleaning import connect, connect_frame
from da_agent.dashboard_tool import BuildDashboardArgs, DashboardService
from da_agent.dataset import PARQUET_PATH
from da_agent.evaluation import Connections
from da_agent.llm import FakeLLM
from da_agent.metrics import _ensure
from da_agent.semantic import SEMANTIC_DIR, build_database, check_dashboard_config, load_config
from helpers import S2, UK, make_frame

# test_metrics 的 11 行（2011-W01、W02），加上商品 F：W02 卖出 £100，第二天全部取消
EXTRA = [(S2, 13, "300011", "10006", "F", 2, "2011-01-12 10:00", 50.0, "c1", UK),
         (S2, 14, "C300012", "10006", "F", -2, "2011-01-13 10:00", 50.0, "c1", UK)]
CONFIG = SEMANTIC_DIR / "retail.yaml"


def _base(rows: list[tuple]):
    con = connect_frame(make_frame(rows))
    _ensure(con)
    return con


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> semantic.QueryDB:
    return build_database(load_config(CONFIG), _base(ROWS + EXTRA), tmp_path_factory.mktemp("m23") / "retail.duckdb")


@pytest.fixture
def con(db: semantic.QueryDB):
    connection = open_semantic(db.path, "semantic:retail", "电商（测试）")
    yield connection
    connection.close()


def _assert_both_forms_agree(con, groups: list[str]) -> int:
    """每个指标：配置的 SQL 写法和看板写法（看板编译器生成的 SQL），按每种分组逐格比较。返回比较了多少格。"""
    config = load_config(CONFIG)
    theirs = configured_metrics(source_of(con))
    assert set(theirs) == set(config.metrics)  # 每个指标都有看板写法
    compared = 0
    for group in groups:
        for key, metric in config.metrics.items():
            params: list = []
            dashboard_sql = _metric_sql(theirs[key], theirs, params)
            rows = con.execute(f"SELECT {group} AS g, {metric.sql} AS by_sql, {dashboard_sql} AS by_dashboard "
                               f"FROM order_lines GROUP BY ALL ORDER BY 1", params).fetchall()
            for g, by_sql, by_dashboard in rows:
                assert by_dashboard == pytest.approx(by_sql, abs=1e-6), (group, g, key)
                compared += 1
    return compared


def test_every_metric_computes_the_same_as_the_query_agent(con) -> None:
    assert _assert_both_forms_agree(con, ["'整体'", "week", "country", "month", "customer_type"]) > 9 * 5


@pytest.mark.skipif(not PARQUET_PATH.exists(), reason="本地没有准备数据（da-agent prepare-data）")
def test_every_metric_computes_the_same_on_real_data(tmp_path: Path) -> None:
    base = connect(PARQUET_PATH)
    _ensure(base)
    real = build_database(load_config(CONFIG), base, tmp_path / "retail.duckdb")
    with open_semantic(real.path, "semantic:retail", "电商") as c:
        assert _assert_both_forms_agree(c, ["week", "country"]) > 9 * 100


def test_the_dashboard_agent_sees_the_configured_table(con) -> None:
    source = source_of(con)
    assert (source.name, source.table, source.time_column) == ("semantic:retail", "order_lines", "order_time")
    profile = profile_table(con)
    by_name = {c["name"]: c for c in profile["columns"]}
    assert {"customer_type", "week", "month", "gmv_amount", "order_id", "buyer_id"} <= set(by_name)
    assert [v["value"] for v in by_name["customer_type"]["top_values"]] == ["老客", "新客", "未知"]
    assert by_name["customer_type"]["description"].startswith("新客（这位客户第一次购买")
    assert {m["id"] for m in profile["metrics"]} >= {"gmv", "aov", "new_customers"}
    assert {"id": "new_customers", "agg": "count_distinct", "column": "buyer_id", "where": {"customer_type": ["新客"]}}.items() \
        <= next(m for m in profile["metrics"] if m["id"] == "new_customers").items()
    assert any(rule.startswith("metrics 是口径配置里定义好的指标") for rule in profile["rules"])
    assert any("gmv_amount 可能被之后的取消抵消" in rule for rule in profile["rules"])
    assert any(rule.startswith("amount（line_type = 销售）可能被之后") for rule in profile["rules"])
    assert any(rule.startswith("amount 的含义取决于 line_type") for rule in profile["rules"])
    assert "2011-W49 不是完整的一周" in source.description and "同一份口径配置" in source.description
    assert profile["row_identity"]["note"] == "数据说明：每行是订单里的一个商品（取消单里的一个商品）"
    with pytest.raises(duckdb.Error):  # 和查询 Agent 一样只读
        con.execute("CREATE TABLE x AS SELECT 1")


GMV = Metric(id="gmv", label="GMV", agg="sum", column="gmv_amount", format="money")
ORDERS = Metric(id="orders", label="订单数", agg="count_distinct", column="order_id")


def test_same_named_metrics_must_follow_the_config(con) -> None:
    source = source_of(con)
    fine = [GMV, ORDERS,
            Metric(id="uk_gmv", label="GMV（英国）", agg="sum", column="gmv_amount", where={"country": [UK]}),  # 可以多加筛选
            Metric(id="sales", label="销售额", agg="sum", column="amount", where={"line_type": ["销售"]}),  # 不同名：不管
            Metric(id="n", label="单数", agg="count_distinct", column="order_id"),
            Metric(id="per_order", label="客单价（£）", agg="ratio", left="gmv", right="n", format="money"),  # 引用的指标换了名字
            Metric(id="new", label="新客数", agg="count_distinct", column="buyer_id", where={"customer_type": ["新客"]})]
    assert definition_problems(fine, source) == []
    wrong = [GMV,
             Metric(id="orders", label="订单", agg="count_distinct", column="invoice"),  # 同 id
             Metric(id="buyers", label="新客数", agg="count_distinct", column="customer_id", where={"customer_type": ["新客"]}),
             Metric(id="days", label="交易天数", agg="count_distinct", column="order_date"),  # 少了 line_type = 销售
             Metric(id="cancelled", label="取消金额", agg="sum", column="cancelled_amount"),
             Metric(id="cancel_rate", label="取消率", agg="ratio", left="gmv", right="cancelled", format="percent"),  # 分子分母反了
             Metric(id="gmv_alt", label="gmv", agg="sum", column="amount", where={"line_type": ["销售"]}),  # 同名（不分大小写）
             Metric(id="lines", label="订单数（张）", agg="count")]  # 括号里的补充不算
    problems = definition_problems(wrong, source)
    assert [p.split("）")[0] for p in problems] == ["指标 orders（订单", "指标 buyers（新客数", "指标 days（交易天数",
                                                  "指标 cancel_rate（取消率", "指标 gmv_alt（gmv", "指标 lines（订单数（张"]
    assert "配置的定义是 count_distinct(order_id)。" in problems[0]
    assert "count_distinct(order_date)，where line_type = ['销售']" in problems[2]
    assert "ratio：取消金额（sum(cancelled_amount)）÷ GMV（sum(gmv_amount)）" in problems[3]


def test_previews_and_submissions_both_check_definitions(con) -> None:
    bad = Metric(id="gmv", label="GMV", agg="sum", column="amount", where={"line_type": ["销售"]}, format="money")
    chart = Chart(id="weekly", title="每周 GMV", type="column", metrics=["gmv"], x={"column": "order_time", "grain": "week"},
                  why="看趋势")
    with pytest.raises(DashboardProblems, match="和口径配置里的“GMV”（gmv）同名"):
        preview_chart(con, [bad], chart)
    spec = DashboardSpec(title="测试", goal="给运营看每周 GMV", metrics=[bad],
                         charts=[Chart(id="k", title="GMV", type="kpi", metrics=["gmv"], why="规模"), chart])
    with pytest.raises(DashboardProblems, match="和口径配置里的“GMV”（gmv）同名"):
        compile_dashboard(con, spec)
    assert [(r["x"], r["values"]) for r in preview_chart(con, [GMV], chart)["rows"]] == [("2011-W01", [140.0]),
                                                                                      ("2011-W02", [245.0])]


NEW = Metric(id="new_customers", label="新客数", agg="count_distinct", column="buyer_id", where={"customer_type": ["新客"]})
OLD = Metric(id="old_customers", label="老客数", agg="count_distinct", column="buyer_id", where={"customer_type": ["老客"]})


def test_a_new_and_returning_customer_dashboard_can_be_built(con, tmp_path: Path, db, monkeypatch) -> None:
    """M22 两次都没做到的看板：每周新客、老客。"""
    by_type = Chart(id="by_type", title="每周活跃客户（新客 / 老客）", type="line", metrics=["active"],
                    x={"column": "order_time", "grain": "week"}, series={"column": "customer_type", "keep": ["新客", "老客"],
                                                                         "other_label": "缺客户 ID"},
                    why="新老客构成")
    spec = DashboardSpec(
        title="每周新老客", goal="给运营每周复盘新老客", metrics=[NEW, OLD, Metric(id="active", label="活跃客户数",
                                                                         agg="count_distinct", column="buyer_id")],
        charts=[Chart(id="k_new", title="新客数", type="kpi", metrics=["new_customers"], why="新客规模"),
                Chart(id="k_old", title="老客数", type="kpi", metrics=["old_customers"], why="老客规模"), by_type])
    compiled = compile_dashboard(con, spec)
    rows = compiled["slices"]["all"]["by_type"]["rows"]
    assert {(r["x"], r["s"]): r["values"][0] for r in rows if r["values"][0]} == {
        ("2011-W01", "新客"): 3, ("2011-W02", "新客"): 1, ("2011-W02", "老客"): 2}
    # 按名字连接（rerender、主 Agent 的 build_dashboard 都走这条路）：用 semantic:retail，读准备好的同一个数据库
    monkeypatch.setattr(semantic, "prepare", lambda name, force=False: db)
    page = write_dashboard("semantic:retail", spec, tmp_path, {"run_id": "t", "model": "fake", "generated_at": "now",
                                                               "source": "semantic:retail"})
    (tmp_path / "dashboard_meta.json").write_text('{"run_id": "t", "model": "fake", "source": "semantic:retail"}',
                                                  encoding="utf-8")
    assert "每周新老客" in page.read_text(encoding="utf-8") and rerender(tmp_path) == page
    with connect_source("semantic:retail") as c:
        assert source_of(c).name == "semantic:retail"


def test_ranking_by_gmv_flags_groups_that_were_mostly_cancelled(con) -> None:
    chart = Chart(id="products", title="商品 GMV 排名", type="bar", metrics=["gmv"], x={"column": "description"}, why="排名")
    out = preview_chart(con, [GMV], chart)
    shares = {r["x"]: r.get("offset_share") for r in out["rows"]}
    assert shares["F"] == 1.0 and shares["A"] == round(5 / 85, 4) and shares.get("D") is None  # A 取消了 £5；D 没有取消
    assert out["unit"] == "£"
    assert ("F 的GMV有 100.0% 后来被取消（£100.00），按下单口径排名会高估它；"
            "看真实成交改用 difference（gmv_amount 的合计 − cancelled_amount 的合计）") in out["notes"]
    net = Metric(id="net_sales", label="净销售额", agg="sum", column="net_amount", format="money")
    assert not any("后来被取消" in n for n in preview_chart(con, [net], chart.model_copy(update={"metrics": ["net_sales"]}))["notes"])


def test_a_wrong_dashboard_section_is_refused(tmp_path: Path) -> None:
    config = load_config(CONFIG)
    dash = config.dashboard

    def broken(**change):
        return config.model_copy(update={"dashboard": dash.model_copy(update=change)})

    with pytest.raises(ValueError, match="看板用的表 nope 不在配置里"):
        check_dashboard_config(broken(table="nope"))
    with pytest.raises(ValueError, match=r"不在表 order_lines 里：\['ghost', 'when'\]"):
        check_dashboard_config(broken(time_column="when", units={"ghost": "£"}))
    both = dash.offsets["gmv_amount"].model_copy(update={"by": "line_type", "value": "销售"})
    with pytest.raises(ValueError, match="要么写 by 和 value"):
        check_dashboard_config(broken(offsets={"gmv_amount": both}))
    aov = config.metrics["aov"].model_copy(update={"dashboard": {"agg": "ratio", "left": "gmv", "right": "ghost"}})
    with pytest.raises(ValueError, match="指标 aov 的看板写法引用了 ghost"):
        check_dashboard_config(config.model_copy(update={"metrics": {**config.metrics, "aov": aov}}))
    with pytest.raises(ValueError, match="看板用的表 nope"):  # 建库时也检查，不会留下可用的数据库
        build_database(broken(table="nope"), _base(ROWS), tmp_path / "x.duckdb")
    assert not (tmp_path / "x.duckdb").exists()
    with pytest.raises(ValueError, match="口径配置 retail_raw 没有写看板用法"):
        semantic_source("retail_raw")


def test_the_query_agent_sees_the_same_catalog_as_before(db) -> None:
    assert set(db.catalog["metrics"]["gmv"]) == {"label", "table", "sql", "definition"}  # 看板写法不进查询 Agent 的数据说明
    assert "dashboard" not in db.catalog


def test_the_main_agent_builds_dashboards_on_the_shared_table() -> None:
    assert DashboardService(llm=FakeLLM([])).source == "semantic:retail"
    assert Connections().dashboard_source == "semantic:retail"  # 评测里的主 Agent 也一样
    assert BuildDashboardArgs(goal="给运营" + "看" * 590).goal  # M22 里 300 字常被退回，现在放宽到 600
    with pytest.raises(ValueError):
        BuildDashboardArgs(goal="看" * 601)
