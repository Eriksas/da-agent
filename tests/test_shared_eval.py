"""M23 评测的两条新规则：看板方案里用到了哪些列、有没有按口径配置算的指标（不看名字，比定义）。"""

from pathlib import Path

import pytest

from da_agent.autodash.spec import DashboardSpec, spec_columns
from da_agent.evaluation import Case, dashboard_missing, load_cases, score
from da_agent.paths import ROOT

CHECKS = {"numbers": {"checked": 0, "ungrounded": []}, "process": {"skills": []}}
KPI = {"id": "k", "title": "新客数", "type": "kpi", "metrics": ["new"], "why": "规模"}
WEEKLY = {"id": "w", "title": "每周活跃客户", "type": "line", "metrics": ["active"], "why": "趋势",
          "x": {"column": "order_time", "grain": "week"}, "series": {"column": "customer_type"}}
SPEC = {"title": "新老客", "goal": "每周复盘新老客",
        "metrics": [{"id": "new", "label": "新客", "agg": "count_distinct", "column": "buyer_id", "where": {"customer_type": ["新客"]}},
                    {"id": "active", "label": "活跃", "agg": "count_distinct", "column": "buyer_id"},
                    {"id": "sales", "label": "销售", "agg": "sum", "column": "gmv_amount", "format": "money"},
                    {"id": "n", "label": "单", "agg": "count_distinct", "column": "order_id"},
                    {"id": "per", "label": "每单", "agg": "ratio", "left": "sales", "right": "n", "format": "money"}],
        "charts": [KPI, WEEKLY], "filters": [{"column": "country", "keep": ["United Kingdom"]}]}


def dashboard_run(*specs: dict | None) -> dict:
    """主 Agent 的一次运行：每个 spec 是看板 Agent 的一次运行（None 表示没有交出通过检查的方案）。"""
    usage = {"llm_calls": 1, "prompt_tokens": 1, "completion_tokens": 1}
    subs = [{"domain": "query", "steps": [{"tool": "run_sql", "ok": True}], "usage": usage}]
    for spec in specs:
        steps = [{"tool": "submit_dashboard", "ok": False, "arguments": {}}]
        if spec is not None:
            steps.append({"tool": "submit_dashboard", "ok": True, "arguments": spec})
        subs.append({"domain": "dashboard", "steps": steps, "usage": usage})
    return {"answer": "页面在 runs/x/dashboard.html", "status": "completed", "seconds": 1.0, "sub_runs": subs,
            "usage": usage,
            "steps": [{"tool": "build_dashboard", "ok": spec is not None} for spec in specs]}


def test_spec_columns_cover_metrics_charts_and_filters() -> None:
    assert spec_columns(DashboardSpec.model_validate(SPEC)) == {"buyer_id", "customer_type", "gmv_amount", "order_id",
                                                                 "order_time", "country"}
    only_where = DashboardSpec.model_validate({**SPEC, "charts": [KPI, {**WEEKLY, "series": None}], "filters": []})
    assert "customer_type" in spec_columns(only_where)  # 只出现在指标的 where 里（新客数）也算用到了


def test_required_columns_and_metrics() -> None:
    wants = Case(id="c", category="看板", question="新老客看板", expects_skill=False, require_tools=("build_dashboard",),
                 dashboard_columns=("customer_type",), dashboard_metrics=("gmv", "aov", "new_customers"))
    assert dashboard_missing(wants, dashboard_run(SPEC)) == []  # 名字不同（sales、per、new），定义和配置一致
    assert score(wants, dashboard_run(SPEC), CHECKS, None)["passed"] is True
    plain = {**SPEC, "charts": [KPI, {**WEEKLY, "series": None}], "filters": [],
             "metrics": [{**SPEC["metrics"][0], "where": {}}, *SPEC["metrics"][1:]]}  # 没有按新老客拆
    result = score(wants, dashboard_run(plain), CHECKS, None)
    assert result["passed"] is False and result["dashboard_missing"] == ["列 customer_type", "按口径算的新客数"]
    other = {**SPEC, "metrics": [*SPEC["metrics"][:4], {**SPEC["metrics"][4], "left": "n", "right": "sales"}]}
    assert dashboard_missing(wants, dashboard_run(other)) == ["按口径算的客单价"]  # 分子分母反了
    assert dashboard_missing(wants, dashboard_run(plain, SPEC)) == []  # 搭了两个看板，有一个满足就算
    assert dashboard_missing(wants, dashboard_run(None)) == ["没有搭出看板"]
    no_rules = Case(id="d", category="看板", question="看板", expects_skill=False)
    assert dashboard_missing(no_rules, dashboard_run(None)) == [] and score(no_rules, dashboard_run(plain), CHECKS, None)["passed"]


def test_the_case_file_is_valid(tmp_path: Path) -> None:
    cases = {case.id: case for case in load_cases(ROOT / "eval" / "dashboard_shared.yaml")}
    assert len(cases) == 10 and cases["s-dash-members"].dashboard_columns == ("customer_type", "country")
    assert cases["s-dash-weekly"].dashboard_metrics == ("gmv", "net_sales", "orders", "aov")
    bad = tmp_path / "bad.yaml"
    bad.write_text("cases:\n  - id: x\n    category: 看板\n    question: 看板\n    dashboard_metrics: [margin]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="口径配置里没有指标 margin"):
        load_cases(bad)
