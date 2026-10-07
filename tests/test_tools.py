"""工具层：说明书格式正确；参数错误、工具拒绝、未知工具都变成给模型看的错误说明。"""

import json

import pytest

from da_agent.cleaning import connect_frame
from da_agent.llm import INVALID_JSON_KEY
from da_agent.tools import TOOLS, execute, tool_specs
from helpers import make_frame
from test_metrics import ROWS


@pytest.fixture(scope="module")
def con():
    connection = connect_frame(make_frame(ROWS))
    yield connection
    connection.close()


def test_specs_are_valid_function_definitions() -> None:
    specs = tool_specs()
    assert [s["function"]["name"] for s in specs] == list(TOOLS)
    for spec in specs:
        assert spec["type"] == "function"
        assert spec["function"]["description"]
        assert spec["function"]["parameters"]["type"] == "object"
        assert '"title"' not in json.dumps(spec)  # 自动生成的 title 已去掉


def test_successful_call(con) -> None:
    outcome = execute(con, "metric_summary", {"week": "2011-W02"})
    assert outcome.ok and outcome.content["tool"] == "metric_summary"
    json.loads(outcome.to_json())


@pytest.mark.parametrize("name, arguments, message", [
    ("delete_everything", {}, "没有名为 delete_everything 的工具"),
    ("metric_summary", {"week": "2011-48"}, "参数不合法：week"),
    ("metric_summary", {"week": "2011-W02", "region": "UK"}, "参数不合法：region"),
    ("drilldown", {"week": "2011-W02", "dimension": "city"}, "参数不合法：dimension"),
    ("metric_summary", {"week": "2011-W10"}, "不在数据范围内"),
    ("metric_summary", {INVALID_JSON_KEY: '{"week": '}, "不是合法的 JSON"),
])
def test_failures_become_error_messages(con, name: str, arguments: dict, message: str) -> None:
    outcome = execute(con, name, arguments)
    assert outcome.ok is False
    assert message in outcome.content["error"]


def test_internal_error_does_not_crash(con, monkeypatch: pytest.MonkeyPatch) -> None:
    from da_agent import tools

    def boom(*_: object) -> dict:
        raise KeyError("oops")

    monkeypatch.setitem(tools.TOOLS, "metric_summary", tools.Tool("metric_summary", "x", tools.MetricSummaryArgs, boom))
    outcome = execute(con, "metric_summary", {"week": "2011-W02"})
    assert outcome.ok is False and "工具内部错误（KeyError）" in outcome.content["error"]


def test_ab_tool_runs_srm_check_first(con) -> None:
    outcome = execute(con, "ab_proportion_test", {"control_success": 200, "control_total": 2000,
                                                  "treatment_success": 250, "treatment_total": 2000})
    assert outcome.ok
    assert outcome.content["srm"]["sample_ratio_mismatch"] is False
    assert outcome.content["test"]["significant"] is True


def test_quality_overview_is_compact_and_serializable(con) -> None:
    outcome = execute(con, "data_quality_overview", {})
    assert outcome.ok
    assert {"clean_totals", "rules", "coverage", "extreme_lines"} <= set(outcome.content)
    json.loads(outcome.to_json())


def test_load_skill_returns_body_and_required_tools(con) -> None:
    outcome = execute(con, "load_skill", {"name": "ecommerce-metric-diagnosis"})
    assert outcome.ok
    assert outcome.content["required_tools"] == ["metric_summary", "decompose_gmv", "drilldown"]
    assert "陷阱清单" in outcome.content["content"]
    missing = execute(con, "load_skill", {"name": "no-such-skill"})
    assert missing.ok is False and "没有名为 no-such-skill 的分析流程" in missing.content["error"]


def test_calculate_tool(con) -> None:
    outcome = execute(con, "calculate", {"expression": "(145 - 140) / 140", "purpose": "GMV 环比"})
    assert outcome.ok and outcome.content == {"tool": "calculate", "expression": "(145 - 140) / 140",
                                              "purpose": "GMV 环比", "result": 0.035714}
    refused = execute(con, "calculate", {"expression": "__import__('os')", "purpose": "x"})
    assert refused.ok is False and "只支持" in refused.content["error"]
    missing = execute(con, "calculate", {"expression": "1 + 1"})  # 必须写明算的是什么，方便复查
    assert missing.ok is False and "purpose" in missing.content["error"]


def test_sample_size_tool_states_its_assumptions(con) -> None:
    """M6：模型引用目标转化率（8% + 0.8pp）、总样本量时，这些数在工具结果里找不到。"""
    outcome = execute(con, "ab_sample_size", {"baseline_rate": 0.08, "min_detectable_effect": 0.008})
    content = outcome.content
    assert (content["baseline_rate"], content["min_detectable_effect"], content["target_rate"]) == (0.08, 0.008, 0.088)
    assert content["total"] == 2 * content["per_group"]
