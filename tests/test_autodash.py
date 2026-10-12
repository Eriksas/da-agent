"""Agent 自主搭看板（M13）：表的概况、方案校验、编译出的数字、自动发现的陷阱、结论核查、端到端（假模型）、页面安全。"""

import json
import sys
from pathlib import Path

import pytest

from da_agent.autodash import sources
from da_agent.autodash.build import build_dashboard, final_spec, rerender
from da_agent.autodash.compile import DashboardProblems, compile_dashboard, preview_chart, ungrounded_numbers
from da_agent.autodash.render import render_dashboard
from da_agent.autodash.sources import column_types, connect_source, profile_table
from da_agent.autodash.spec import Chart, DashboardSpec, Metric, check_spec
from da_agent.cleaning import connect_frame
from da_agent.dataset import PARQUET_PATH
from da_agent.llm import FakeLLM
from da_agent.paths import FIXTURES_DIR
from da_agent.tools import execute, tool_specs
from helpers import S2, UK, make_frame

sys.path.insert(0, str(Path(__file__).parent))
from fixtures_dashboard_spec import RETAIL_SPEC  # noqa: E402

# 2010-12 到 2011-02：英国 4 单、法国 2 单、1 张取消单。数据到 2 月 15 日为止，2 月不完整
ROWS = [
    (S2, 2, "100001", "10001", "A", 10, "2010-12-06 10:00", 2.0, "c1", UK),         # 20
    (S2, 3, "100002", "10001", "A", 5, "2011-01-10 11:00", 4.0, "c1", UK),          # 20
    (S2, 4, "100003", "10002", "B", 3, "2011-01-12 14:00", 10.0, "c2", "France"),   # 30
    (S2, 5, "100005", "10003", "C", 4, "2011-01-20 15:00", 5.0, None, UK),          # 20
    (S2, 6, "C100007", "10001", "A", -1, "2011-01-11 09:30", 4.0, "c1", UK),        # 取消 4
    (S2, 7, "100006", "10004", "D", 2, "2011-02-01 12:59", 5.0, "c3", UK),          # 10
    (S2, 8, "100004", "10002", "B", 1, "2011-02-15 09:00", 10.0, "c2", "France"),   # 10
]
GMV = {"id": "gmv", "label": "GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]}, "format": "money"}
ORDERS = {"id": "orders", "label": "订单数", "agg": "count_distinct", "column": "invoice", "where": {"line_type": ["销售"]}}
CANCELLED = {"id": "cancelled", "label": "取消金额", "agg": "sum", "column": "amount", "where": {"line_type": ["取消"]},
             "format": "money"}
RATE = {"id": "cancel_rate", "label": "取消率", "agg": "ratio", "left": "cancelled", "right": "gmv", "format": "percent"}
KPI = {"id": "gmv_kpi", "title": "GMV", "type": "kpi", "metrics": ["gmv"], "why": "整体规模"}
MONTHLY = {"id": "monthly", "title": "每月 GMV", "type": "column", "metrics": ["gmv"],
           "x": {"column": "invoice_time", "grain": "month"}, "why": "看趋势"}
COUNTRIES = {"id": "countries", "title": "各国 GMV", "type": "bar", "metrics": ["gmv"], "x": {"column": "country"},
             "top_n": 3, "why": "哪个市场最大"}


@pytest.fixture
def con():
    connection = connect_frame(make_frame(ROWS))
    sources._retail_view(connection)
    yield connection
    connection.close()


def spec(**overrides: object) -> DashboardSpec:
    base = {"title": "测试看板", "goal": "给运营看整体情况", "metrics": [GMV, ORDERS, CANCELLED, RATE],
            "charts": [KPI, {**KPI, "id": "rate_kpi", "title": "取消率", "metrics": ["cancel_rate"]},
                       {**KPI, "id": "orders_kpi", "title": "订单数", "metrics": ["orders"]}, MONTHLY, COUNTRIES]}
    return DashboardSpec.model_validate(base | overrides)


# ---------- 表的概况 ----------
def test_profile_lists_values_roles_and_rules(con) -> None:
    profile = profile_table(con)
    cols = {c["name"]: c for c in profile["columns"]}
    assert profile["rows"] == 7 and profile["source"] == "retail"
    assert {v["value"] for v in cols["line_type"]["top_values"]} == {"销售", "取消"}
    assert (cols["invoice_time"]["role_hint"], cols["amount"]["role_hint"], cols["country"]["role_hint"]) == \
        ("time", "measure", "dimension")
    assert any("amount 的含义取决于 line_type" in r for r in profile["rules"])
    assert cols["amount"]["min"] == 4  # 取消单的金额也取正数


# ---------- 静态校验 ----------
@pytest.mark.parametrize(("change", "message"), [
    ({"metrics": [GMV | {"column": "no_such"}, ORDERS, CANCELLED, RATE]}, "没有 no_such 这一列"),
    ({"metrics": [GMV | {"column": "country"}, ORDERS, CANCELLED, RATE]}, "country 不是数值列，不能 sum"),
    ({"metrics": [GMV | {"where": {}}, ORDERS, CANCELLED, RATE]}, "含义取决于 line_type"),
    ({"metrics": [GMV, ORDERS, CANCELLED, RATE | {"right": "missing"}]}, "left、right 必须是已定义的指标"),
    ({"charts": [MONTHLY, COUNTRIES]}, "至少放一张 kpi"),
])
def test_static_check_catches_mistakes(con, change: dict, message: str) -> None:
    problems = check_spec(spec(**change), column_types(con, sources.source_of(con)), sources.SOURCES["retail"])
    assert any(message in p for p in problems), problems


@pytest.mark.parametrize(("chart", "message"), [
    ({"id": "s", "title": "取消率构成", "type": "stacked_column", "metrics": ["cancel_rate"],
      "x": {"column": "invoice_time", "grain": "month"}, "series": {"column": "country"}, "why": "测试"}, "不能堆叠"),
    ({"id": "b", "title": "按月条形", "type": "bar", "metrics": ["gmv"], "x": {"column": "invoice_time", "grain": "month"},
      "why": "测试"}, "bar 用来比较类别"),
    ({"id": "c", "title": "两个指标", "type": "column", "metrics": ["gmv", "orders"],
      "x": {"column": "invoice_time", "grain": "month"}, "why": "测试"}, "不做双坐标轴"),
    ({"id": "t", "title": "没有粒度", "type": "line", "metrics": ["gmv"], "x": {"column": "invoice_time"}, "why": "测试"},
     "要给 grain"),
])
def test_chart_rules(con, chart: dict, message: str) -> None:
    problems = check_spec(spec(charts=[KPI, {**KPI, "id": "k2", "metrics": ["cancel_rate"]},
                                       {**KPI, "id": "k3", "metrics": ["orders"]}, chart]),
                          column_types(con, sources.source_of(con)), sources.SOURCES["retail"])
    assert any(message in p for p in problems), problems


# ---------- 编译出的数字 ----------
def test_compiled_numbers_match_hand_calculation(con) -> None:
    out = compile_dashboard(con, spec())
    data = out["slices"]["all"]
    assert data["gmv_kpi"]["facts"]["value"] == 110 and data["orders_kpi"]["facts"]["value"] == 6
    assert data["rate_kpi"]["facts"]["value"] == round(4 / 110, 4)
    months = data["monthly"]["rows"]
    assert [(r["x"], r["values"][0], r.get("partial", False)) for r in months] == [
        ("2010-12", 20, True), ("2011-01", 70, False), ("2011-02", 20, True)]  # 数据从 12 月 6 日开始、2 月 15 日结束
    assert any("2010-12、2011-02 只覆盖了部分时间" in n for n in data["monthly"]["notes"])


def test_bar_folds_the_tail_and_flags_a_dominant_group(con) -> None:
    out = compile_dashboard(con, spec())
    bar = out["slices"]["all"]["countries"]
    assert [(r["x"], r["values"][0], r["share"]) for r in bar["rows"]] == [
        ("United Kingdom", 70, round(70 / 110, 4)), ("France", 40, round(40 / 110, 4))]
    assert any("United Kingdom 占 63.6%" in n for n in bar["notes"])


def test_keep_groups_values_and_filters_make_slices(con) -> None:
    market = {"column": "country", "keep": ["United Kingdom"], "other_label": "海外"}
    out = compile_dashboard(con, spec(
        charts=[KPI, {**KPI, "id": "k2", "metrics": ["cancel_rate"]}, {**KPI, "id": "k3", "metrics": ["orders"]},
                {"id": "mix", "title": "市场构成", "type": "stacked_column", "metrics": ["gmv"],
                 "x": {"column": "invoice_time", "grain": "month"}, "series": market, "why": "构成"}],
        filters=[market | {"label": "市场"}], year_filter=True))
    assert [f["id"] for f in out["filters"]] == ["year", "f0"]
    assert set(out["slices"]) == {f"{y}|{m}" for y in ("all", "2010", "2011") for m in ("all", "United Kingdom", "海外")}
    assert out["slices"]["2011|海外"]["gmv_kpi"]["facts"]["value"] == 40
    mix = out["slices"]["all|all"]["mix"]
    assert mix["series_order"] == ["United Kingdom", "海外"]  # keep 里的取值在前
    assert mix["facts"]["series_share"] == {"United Kingdom": round(70 / 110, 4), "海外": round(40 / 110, 4)}


def test_heatmap_orders_weekdays_naturally(con) -> None:
    heat = {"id": "when", "title": "什么时候下单", "type": "heatmap", "metrics": ["orders"],
            "x": {"column": "invoice_time", "grain": "hour"}, "series": {"column": "invoice_time", "grain": "weekday"},
            "why": "排班"}
    out = compile_dashboard(con, spec(charts=[KPI, {**KPI, "id": "k2", "metrics": ["cancel_rate"]},
                                              {**KPI, "id": "k3", "metrics": ["orders"]}, heat]))
    data = out["slices"]["all"]["when"]
    assert data["series_order"] == ["周一", "周二", "周三", "周四"]
    assert sum(r["values"][0] for r in data["rows"]) == 6


def test_missing_values_are_sent_back(con) -> None:
    with pytest.raises(DashboardProblems) as exc:
        compile_dashboard(con, spec(metrics=[GMV | {"where": {"line_type": ["Sales"]}}, ORDERS, CANCELLED, RATE]))
    assert "没有这些取值 ['Sales']" in str(exc.value) and "销售" in str(exc.value)


# ---------- 结论核查 ----------
def test_insight_numbers_must_come_from_the_chart(con) -> None:
    good = MONTHLY | {"insight": "2011-01 的 GMV 最高，为 70；2011-02 只有部分数据"}
    compile_dashboard(con, spec(charts=[KPI, {**KPI, "id": "k2", "metrics": ["cancel_rate"]},
                                        {**KPI, "id": "k3", "metrics": ["orders"]}, good, COUNTRIES]))
    bad = MONTHLY | {"insight": "2011-01 的 GMV 为 75，同比增长 12.5%"}
    with pytest.raises(DashboardProblems) as exc:
        compile_dashboard(con, spec(charts=[KPI, {**KPI, "id": "k2", "metrics": ["cancel_rate"]},
                                            {**KPI, "id": "k3", "metrics": ["orders"]}, bad, COUNTRIES]))
    assert "['75', '12.5%']" in str(exc.value)


def test_shares_in_insights_can_be_rounded() -> None:
    data = {"rows": [{"x": "United Kingdom", "values": [70], "share": 0.6364}], "facts": {}}
    assert ungrounded_numbers("英国占 63.6%", data) == [] and ungrounded_numbers("英国占 64.1%", data) == ["64.1%"]


# ---------- 单位（财报数据，已提交，CI 里也能跑） ----------
def test_money_in_different_currencies_cannot_share_an_axis() -> None:
    revenue = {"id": "revenue", "label": "营业收入", "agg": "sum", "column": "value", "where": {"item": ["revenue"]},
               "format": "money"}
    chart = {"id": "rev", "title": "各公司营业收入", "type": "bar", "metrics": ["revenue"],
             "x": {"column": "company_name"}, "why": "比较规模"}
    with connect_source("financials") as con:
        with pytest.raises(DashboardProblems) as exc:
            preview_chart(con, [Metric.model_validate(revenue)], Chart.model_validate(chart))
        assert "单位不同（['CNY', 'USD']）" in str(exc.value)
        kpi = chart | {"id": "all_rev", "type": "kpi", "x": None}
        with pytest.raises(DashboardProblems) as exc:
            preview_chart(con, [Metric.model_validate(revenue)], Chart.model_validate(kpi))
        assert "同一个格子里混合了不同的 currency" in str(exc.value)
        one_company = revenue | {"where": {"item": ["revenue"], "company": ["pdd"]}}
        result = preview_chart(con, [Metric.model_validate(one_company)], Chart.model_validate(chart))
        assert result["unit"] == "¥" and result["row_count"] == 1


def test_tools_hand_problems_back_to_the_model() -> None:
    with connect_source("financials") as con:
        outcome = execute(con, "submit_dashboard", {"title": "看板", "goal": "看四家公司", "metrics": [
            {"id": "n", "label": "公司数", "agg": "count_distinct", "column": "company"}],
            "charts": [{"id": "k", "title": "公司数", "type": "kpi", "metrics": ["n"], "why": "范围"},
                       {"id": "b", "title": "公司", "type": "bar", "metrics": ["n"], "x": {"column": "nope"}, "why": "测试"}]})
    assert not outcome.ok and "没有 nope 这一列" in outcome.content["error"]
    schema = json.dumps(tool_specs("dashboard"), ensure_ascii=False)
    assert "$ref" not in schema and "submit_dashboard" in schema  # 嵌套的方案格式已展开


# ---------- 端到端：假模型剧本 ----------
def test_build_dashboard_end_to_end(tmp_path: Path) -> None:
    """剧本先提交一个把人民币和美元放在一起比的方案，被退回；改成比较净利率后通过，生成页面。"""
    script = FIXTURES_DIR / "fake_llm" / "dashboard_financials.json"
    run, run_dir, page = build_dashboard("financials", None, FakeLLM.from_file(script), runs_dir=tmp_path)
    assert run.status == "completed"
    submits = [(s["ok"], s["error"] or "") for s in run.steps if s["tool"] == "submit_dashboard"]
    assert [ok for ok, _ in submits] == [False, True] and "单位不同" in submits[0][1]
    assert page is not None and (run_dir / "spec.json").exists()
    html = page.read_text(encoding="utf-8")
    assert "AI 生成的看板草稿，待人工复核" in html and "FY2025 净利率对比" in html
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert final_spec(run_record).charts[-1].id == "margin_2025"  # 取最后一次通过的方案
    edited = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
    edited["title"] = "人工改过的标题"
    (run_dir / "edited.json").write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
    assert "人工改过的标题" in rerender(run_dir, run_dir / "edited.json").read_text(encoding="utf-8")


def test_final_spec_is_the_last_accepted_submission() -> None:
    """通过之后又提交了一个没通过的方案：看板用最后一次通过的，不用最后一次提交的。"""
    accepted = spec(title="第一版").model_dump()
    run = {"steps": [{"tool": "submit_dashboard", "ok": True, "arguments": accepted},
                     {"tool": "preview_chart", "ok": True, "arguments": {}},
                     {"tool": "submit_dashboard", "ok": False, "arguments": {"title": "第二版"}}]}
    assert final_spec(run).title == "第一版"
    assert final_spec({"steps": run["steps"][1:]}) is None


def test_page_treats_model_text_as_untrusted(con) -> None:
    out = compile_dashboard(con, spec(title="<script>alert(1)</script>"))
    out["charts"][3]["why"] = "</script><script>alert(2)</script>"
    html = render_dashboard(out, {"run_id": "r", "model": "m", "generated_at": "t"})
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html
    payload = html.split('<script id="dashboard-data" type="application/json">')[1].split("</script>")[0]
    assert "<" not in payload and json.loads(payload)["charts"][3]["why"].startswith("</script>")


# ---------- 真实数据：和 M12 手工看板同口径的数字一致 ----------
@pytest.mark.skipif(not PARQUET_PATH.exists(), reason="本地没有准备数据（da-agent prepare-data）")
def test_same_definitions_give_the_same_numbers_as_the_hand_built_dashboard() -> None:
    from da_agent.dashboard import load_dashboard_data

    with connect_source("retail") as con:
        out = compile_dashboard(con, DashboardSpec.model_validate(RETAIL_SPEC))
    m12 = load_dashboard_data()["slices"]
    data = out["slices"]["all|all"]
    assert data["gmv_total"]["facts"]["value"] == pytest.approx(m12["all/all"]["kpi"]["gmv"], abs=0.01)
    assert data["cancel_rate_kpi"]["facts"]["value"] == m12["all/all"]["kpi"]["cancel_rate"]
    months = {r["x"]: r["values"][0] for r in data["monthly_gmv"]["rows"]}
    assert all(months[m["month"]] == pytest.approx(m["gmv"], abs=0.01) for m in m12["all/all"]["months"])
    assert out["slices"]["2011|United Kingdom"]["gmv_total"]["facts"]["value"] == \
        pytest.approx(m12["2011/uk"]["kpi"]["gmv"], abs=0.01)
    # 去重计数不能跨格子相加：有一张订单从 11:59 录到 12:00，热力图格子加起来比订单数多 1（M12 里按第一行时间算一次）
    heat = sum(r["values"][0] for r in data["order_hours"]["rows"])
    assert heat == m12["all/all"]["kpi"]["orders"] + 1
