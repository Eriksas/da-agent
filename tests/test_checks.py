"""报告核查：数字提取、出处匹配、措辞标记、报告渲染。"""

import json
from pathlib import Path

import pytest

from da_agent.checks import check_run, extract_numbers, flagged_phrases
from da_agent.report import render_report, write_review


def texts(answer: str) -> list[str]:
    return [m.text for m in extract_numbers(answer)[0]]


def test_extracts_common_formats() -> None:
    answer = "GMV 309,381.73，环比 +6,989.51（+2.31%），取消率 -1.31 个百分点，约 30.9 万，占比13.11%，贡献 −13,088.20"
    assert texts(answer) == ["309,381.73", "+6,989.51", "+2.31%", "-1.31 个百分点", "30.9 万", "13.11%", "−13,088.20"]


def test_ignores_weeks_dates_codes_and_list_markers() -> None:
    answer = "1. 分析 2011-W48，数据截止 2011-12-09 12:50，规则 R09，发票 C581484\n## 2. 结论"
    assert texts(answer) == []


def test_small_integers_are_counted_not_checked() -> None:
    mentions, skipped = extract_numbers("交易天数 5 天对 6 天，订单 663 单")
    assert [m.text for m in mentions] == ["663"]
    assert skipped == 2


def run_with(answer: str, tool_result: dict, question: str = "问题") -> dict:
    return {"answer": answer, "messages": [
        {"role": "system", "content": "数据范围 2009-W49 至 2011-W49"},
        {"role": "user", "content": question},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(tool_result, ensure_ascii=False)}]}


TOOL = {"gmv": 309381.73, "change_pct": 0.0231, "cancel_rate_change": -0.0131,
        "warnings": ["部分分组订单数少于 30"]}


@pytest.mark.parametrize("answer", [
    "GMV 309,381.73", "增长 2.31%", "增长 2.3%", "下降 1.31 个百分点", "约 30.9 万", "订单少于 30 的分组"])
def test_grounded_numbers(answer: str) -> None:
    assert check_run(run_with(answer, TOOL))["numbers"]["ungrounded"] == []


@pytest.mark.parametrize("answer", ["GMV 309,381.37", "增长 2.41%", "约 31.9 万"])
def test_fabricated_or_miscopied_numbers_are_caught(answer: str) -> None:
    ungrounded = check_run(run_with(answer, TOOL))["numbers"]["ungrounded"]
    assert len(ungrounded) == 1 and ungrounded[0]["context"] == answer


def test_numbers_from_user_question_count_as_source() -> None:
    """AB 实验这类场景，数字由用户提供，引用它们不算编造。"""
    run = run_with("对照组 2000 人中 200 人转化", {}, question="对照组 2000 人、200 人转化，实验组呢？")
    assert check_run(run)["numbers"]["ungrounded"] == []


def test_model_own_words_are_not_a_source() -> None:
    run = run_with("GMV 123.45", TOOL)
    run["messages"].append({"role": "assistant", "content": "我算出来是 123.45"})
    assert len(check_run(run)["numbers"]["ungrounded"]) == 1


def test_flagged_phrases() -> None:
    found = flagged_phrases("增长全靠老客。取消率下降导致净销售额上升。分子分母不一定是同一批订单。")
    assert [(p["phrase"], p["category"]) for p in found] == [("导致", "因果表述"), ("全靠", "绝对化或无依据的推测")]


def test_report_has_draft_label_and_appendices(tmp_path: Path) -> None:
    run = {**run_with("GMV 309,381.73，编造的 12.34%。增长全靠老客。", TOOL),
           "run_id": "r1", "question": "为什么？", "status": "completed", "model": "fake", "max_tool_calls": 5,
           "budget_exhausted": False, "seconds": 0.1, "usage": {"llm_calls": 2, "prompt_tokens": 0, "completion_tokens": 0},
           "steps": [{"round": 1, "tool": "metric_summary", "arguments": {"week": "2011-W48"}, "ok": True, "error": None, "ms": 5}]}
    checks = check_run(run)
    report = render_report(run, checks)
    assert report.startswith("> **AI 初稿，待人工复核。**")
    assert "| 12.34% |" in report                 # 找不到出处的数字
    assert "| 绝对化或无依据的推测 | 全靠 |" in report
    assert "部分分组订单数少于 30" in report       # 警告汇总
    (tmp_path / "run.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    assert write_review(tmp_path)["numbers"]["checked"] == 2
    assert (tmp_path / "report.md").exists() and (tmp_path / "checks.json").exists()


def run_steps(*tools: tuple[str, bool], tool_results: list[dict] | None = None) -> dict:
    messages = [{"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps(r, ensure_ascii=False)}
                for i, r in enumerate(tool_results or [])]
    return {"answer": "", "messages": messages,
            "steps": [{"tool": name, "ok": ok} for name, ok in tools]}


LOADED = {"tool": "load_skill", "name": "ecommerce-metric-diagnosis",
          "required_tools": ["metric_summary", "decompose_gmv", "drilldown"], "content": "…"}


def test_process_check_lists_missing_required_steps() -> None:
    from da_agent.checks import process_check

    run = run_steps(("load_skill", True), ("metric_summary", True), ("decompose_gmv", False), tool_results=[LOADED])
    result = process_check(run)
    assert result["skills"][0]["missing"] == ["decompose_gmv", "drilldown"]  # 失败的调用不算完成
    assert "缺少必做步骤 decompose_gmv、drilldown" in result["summary"]


def test_process_check_passes_and_reports_no_skill() -> None:
    from da_agent.checks import process_check

    done = run_steps(("load_skill", True), ("metric_summary", True), ("decompose_gmv", True), ("drilldown", True),
                     tool_results=[LOADED])
    assert process_check(done)["summary"].endswith("必做步骤全部完成")
    assert process_check(run_steps(("metric_summary", True)))["summary"] == "流程检查：本次没有加载分析流程"


def test_danger_signals_for_big_changes_only() -> None:
    from da_agent.checks import danger_signals

    summary = {"tool": "metric_summary", "period": {"current": "2011-W49"}, "metrics": [
        {"label": "GMV", "unit": "money", "change_pct": 0.5635},
        {"label": "订单数", "unit": "count", "change_pct": -0.2323},
        {"label": "取消率", "unit": "rate", "change_pct": None},
        {"label": "老客数", "unit": "count", "change_pct": None}]}
    signals = danger_signals(run_steps(tool_results=[summary]))
    assert len(signals) == 1 and signals[0].startswith("2011-W49 GMV变化 +56.35%，超过 50%")


def test_percent_inside_tool_text_counts_as_source() -> None:
    """真实运行发现的 bug：流程原文里写着“超过 50%”，报告引用“超过 50% 的阈值”却被判为找不到出处。"""
    run = run_with("客单价已超过 50% 的危险阈值", {"content": "任一核心指标变化超过 50%，先排查数据问题"})
    assert check_run(run)["numbers"]["ungrounded"] == []


def test_chinese_week_notation_is_not_a_number() -> None:
    """评测试跑发现：答案写“2011 年 45 周”，其中的 45 被当成了没有出处的数字。"""
    run = run_with("2011 年 45 周（第 45 周）的订单数为 634 单。", {"orders": 634})
    assert check_run(run)["numbers"]["ungrounded"] == []


@pytest.mark.parametrize("answer, grounded", [
    ("与 21,616 量级相当", True),   # 四舍五入
    ("与 21,615 量级相当", True),   # 直接截断：评测试跑中的真实写法
    ("与 21,614 量级相当", False),  # 差了 1 个单位以上，算错
    ("约 2.16 万", True),
])
def test_truncation_within_one_unit_is_accepted(answer: str, grounded: bool) -> None:
    run = run_with(answer, {"change": -21615.74})
    assert (check_run(run)["numbers"]["ungrounded"] == []) is grounded


@pytest.mark.parametrize("answer", ["绝对差 +1.2pp", "置信区间 [-0.48pp, +1.62pp]", "提升 1.2 pp"])
def test_pp_is_percentage_points(answer: str) -> None:
    """正式评测发现：模型常用 pp 表示百分点，原来只认“个百分点”，把正确的数判成了没有出处。"""
    run = run_with(answer, {"difference": 0.012, "confidence_interval": [-0.004788, 0.016217]})
    assert check_run(run)["numbers"]["ungrounded"] == []


def test_multi_level_heading_numbers_are_not_numbers() -> None:
    """首份真实周报：小节编号“### 2.1”被当成数字，判为没有出处。"""
    assert texts("### 2.1 按 Shapley 拆解\n#### 3.2.1 细节") == []


def calc(expression: str, result: float, purpose: str = "测试") -> dict:
    return {"role": "tool", "tool_call_id": "calc",
            "content": json.dumps({"tool": "calculate", "expression": expression, "purpose": purpose, "result": result})}


def with_messages(answer: str, *extra: dict, tool: dict | None = None) -> dict:
    run = run_with(answer, TOOL if tool is None else tool)
    run["messages"] += list(extra)
    return run


W49 = {"gmv": 309381.73, "extreme_line": 168469.6, "base_gmv": 138290.0}  # 示意数字


def test_calculation_from_sourced_inputs_is_a_source() -> None:
    """两步：先剔除大单，再用第一步的结果算环比。每一步的输入都能追溯到工具结果。"""
    run = with_messages("剔除后 GMV 为 140,912.13，环比 +1.90%", calc("309381.73 - 168469.6", 140912.13),
                        calc("(140912.13 - 138290) / 138290", 0.018961), tool=W49)
    numbers = check_run(run)["numbers"]
    assert numbers["ungrounded"] == [] and numbers["calculations"] == 2
    assert numbers["unsupported_calculations"] == []


def test_calculation_from_made_up_inputs_is_not_a_source() -> None:
    """把编的数放进算式，结果也不能算有出处；后面用到这个结果的计算同样不算。"""
    run = with_messages("剔除后 GMV 为 140,912.13，环比 +1.90%", calc("309381.73 - 168469.6", 140912.13),
                        calc("(140912.13 - 138290) / 138290", 0.018961), tool={"gmv": 309381.73, "base_gmv": 138290.0})
    numbers = check_run(run)["numbers"]
    assert [m["text"] for m in numbers["ungrounded"]] == ["140,912.13", "+1.90%"]
    assert [c["unsupported_inputs"] for c in numbers["unsupported_calculations"]] == [["168469.6"], ["140912.13"]]


def test_percent_written_from_a_times_100_calculation() -> None:
    """模型先乘 100（结果 1.896…），再在回答里写成 1.90%：也算有出处。100 是允许的换算常数。"""
    run = with_messages("环比 +1.90%", calc("(140912.13 - 138290) / 138290 * 100", 1.896106),
                        tool={"current": 140912.13, "base": 138290.0})
    assert check_run(run)["numbers"]["ungrounded"] == []


def test_inputs_written_as_percent_numbers_are_accepted() -> None:
    """算式里把 0.0231 写成 2.31（百分数的数值），也能找到出处。"""
    assert check_run(with_messages("差 1.00 个百分点", calc("2.31 - 1.31", 1.0)))["numbers"]["ungrounded"] == []


def test_only_the_first_user_message_is_a_source() -> None:
    """后面的“用户消息”是程序加的提示（例如核查退回时列出的数字），不能把这些数字变成有出处。"""
    notice = {"role": "user", "content": "（系统核查）找不到出处的数字：「5,555.55」"}
    assert [m["text"] for m in check_run(with_messages("增长 5,555.55", notice))["numbers"]["ungrounded"]] == ["5,555.55"]
