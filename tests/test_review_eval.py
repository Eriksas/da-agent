"""复核 Agent 的评测（M15）：四种植入方式、是否瞒得过数字核查、怎么算“指到了”、汇总。不调用真实模型。"""

import json
from pathlib import Path

import pytest

from da_agent import review_eval
from da_agent.llm import FakeLLM, LLMReply, ToolCall
from da_agent.review_eval import (
    Mutation,
    build_cases,
    invisible_to_number_check,
    load_plan,
    mutate_cause,
    mutate_direction,
    mutate_entity,
    mutate_swap,
    render_summary,
    run_review_eval,
    score_case,
    summarize,
)

ANSWER = ("本周 GMV 为 120,000.00，环比下降 6.00%。订单数 314 单。\n"
          "- 荷兰 GMV 从 10,798.24 降到 104.00。\n"
          "- 是否与促销有关还不能确定，可能下降 3.50% 只是波动。\n"
          "| 英国 | 增长 12.30% |\n"
          "客单价 382.17，高于上周的 351.20。")
TOOLS = [{"tool": "metric_summary", "metrics": [{"label": "GMV", "unit": "money", "current": 120000.0, "change_pct": -0.06},
                                                {"label": "订单数", "unit": "count", "current": 314},
                                                {"label": "客单价", "unit": "money", "current": 382.17, "base": 351.2}]},
         {"tool": "drilldown", "dimension": "country", "rows": [
             {"segment": "荷兰", "base": 10798.24, "current": 104.0}, {"segment": "英国", "change_pct": 0.123},
             {"segment": "法国", "change_pct": 0.035}]},
         {"tool": "drilldown", "dimension": "customer_type", "rows": [{"segment": "老客", "orders": 200},
                                                                      {"segment": "新客", "orders": 114}]}]


def make_run(answer: str = ANSWER, domain: str = "ecommerce") -> dict:
    messages = [{"role": "system", "content": "提示词"}, {"role": "user", "content": "这周怎么样？"}]
    messages += [{"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps(t, ensure_ascii=False)} for i, t in enumerate(TOOLS)]
    steps = [{"round": 1, "tool": t["tool"], "arguments": {}, "ok": True, "error": None, "ms": 1} for t in TOOLS]
    return {"run_id": "base", "question": "这周怎么样？", "model": "writer", "status": "completed", "answer": answer,
            "domain": domain, "steps": steps, "messages": messages + [{"role": "assistant", "content": answer}]}


# ---------- 四种植入方式 ----------
def test_direction_flips_a_word_that_is_followed_by_a_number() -> None:
    m = mutate_direction(ANSWER, make_run())
    assert m.after == "本周 GMV 为 120,000.00，环比上升 6.00%" and m.lines == [0]
    assert m.answer[m.spans[0][0]:m.spans[0][1]] == m.after and m.answer.count("环比上升") == 1
    # 不确定的句子、表格里的、没有紧跟数字的方向词都不改
    assert mutate_direction("是否下降 3.50% 还不能确定。\n| 英国 | 增长 12.30% |\n这种增长值得关注，共 314 单。", make_run()) is None
    assert mutate_direction("先不要对外说增长 11.77%。", make_run()) is None


def test_swap_exchanges_two_look_alike_numbers_in_different_sentences() -> None:
    m = mutate_swap(ANSWER, make_run())
    assert m.lines == [0, 3]  # 120,000.00 和 10,798.24 在两句里：都是两位小数、没有单位、相差 20% 以上
    assert "本周 GMV 为 10,798.24" in m.answer and "荷兰 GMV 从 120,000.00 降到" in m.answer
    assert invisible_to_number_check(make_run(), m.answer)  # 两个数都还在工具结果里：数字核查看不出差别
    # 日期、比例里的数不拿来换；带正负号的不换；同一句里的两个数不换
    assert mutate_swap("财年截止 12-31。\n样本 5000/4000。", make_run()) is None
    assert mutate_swap("变化 +22.50。\n另一项 -11.20。", make_run()) is None
    assert mutate_swap("GMV 从 10,798.24 涨到 120,000.00。", make_run()) is None


def test_entity_swaps_only_within_the_same_kind_of_name() -> None:
    m = mutate_entity(ANSWER, make_run())
    assert m.before == "本周 GMV 为 120,000.00，环比下降 6.00%" and m.after == "本周 订单数 为 120,000.00，环比下降 6.00%"
    countries = mutate_entity("荷兰的订单从 10,798.24 降到 104.00。\n英国增长 12.30%。\nGMV 合计 120,000.00。", make_run())
    assert countries.after.startswith("英国的订单从")  # 国家只换成国家，不会换成指标名（GMV 在答案里出现得更早）
    assert mutate_entity("荷兰的订单从 10,798.24 降到 104.00。\n老客共 200 单。", make_run()) is None  # 国家不会换成客户类型
    assert mutate_entity("共 314 单，英国和荷兰都在内。", make_run()) is None  # 两个名字都在这一句里：不换
    assert mutate_entity("新老客合计 314 单。\n新客更活跃。", make_run()) is None  # “新老客”里的“老客”不是单独提到的


def test_cause_appends_an_unsupported_reason_after_a_conclusion() -> None:
    m = mutate_cause(ANSWER, make_run())
    start, end = m.spans[0]
    assert m.answer[start:end] == "这一变化主要是因为当周站内促销活动的调整。"
    assert m.answer.startswith("本周 GMV 为 120,000.00，环比下降 6.00%。这一变化主要是因为")
    assert "定价策略" in mutate_cause(ANSWER, make_run(domain="company")).answer
    assert invisible_to_number_check(make_run(), m.answer)
    assert not invisible_to_number_check(make_run(), ANSWER.replace("314", "999"))  # 编一个数：数字核查能查出来，不算“瞒得过”


# ---------- 怎么算“指到了” ----------
def _review(findings: list[dict], status: str = "reviewed") -> dict:
    errors = any(f["severity"] == "错误" for f in findings)
    return {"status": status, "verdict": "有问题" if errors else "通过", "findings": findings,
            "usage": {"prompt_tokens": 900, "completion_tokens": 100}, "seconds": 2.0, "rejected_submissions": 0}


def _finding(answer: str, quote: str, severity: str = "错误") -> dict:
    start = answer.index(quote)
    return {"quote": quote, "category": "方向或比较说反", "severity": severity, "start": start, "end": start + len(quote)}


def test_a_finding_must_land_on_the_changed_sentence() -> None:
    run = make_run()
    m = mutate_direction(ANSWER, run)
    case = {"id": "b-direction", "base": "b", "kind": "direction", "mutation": {"spans": m.spans, "lines": m.lines}}
    hit = score_case(case, _review([_finding(m.answer, "环比上升 6.00%")]), _review([]), ANSWER)
    assert (hit["outcome"], hit["as_error"]) == ("detected", True)
    doubt = score_case(case, _review([_finding(m.answer, "环比上升 6.00%", "存疑")]), _review([]), ANSWER)
    assert (doubt["outcome"], doubt["as_error"]) == ("detected", False)
    elsewhere = score_case(case, _review([_finding(m.answer, "高于上周的 351.20")]), _review([]), ANSWER)
    assert elsewhere["outcome"] == "missed"
    # 原样答案里这一句本来就被指出过：分不清指的是哪个问题，不计入
    already = score_case(case, _review([_finding(m.answer, "环比上升 6.00%")]),
                         _review([_finding(ANSWER, "本周 GMV 为 120,000.00")]), ANSWER)
    assert already["outcome"] == "ambiguous"
    assert "outcome" not in score_case(case, _review([], status="failed") | {"verdict": None}, None, None)


# ---------- 完整流程和汇总 ----------
def test_eval_runs_end_to_end_and_summarizes(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "run.json").write_text(json.dumps(make_run(), ensure_ascii=False), encoding="utf-8")
    plan = {"bases": [{"id": "b", "run": "base"}],
            "natural": [{"id": "nat", "run": "base", "known_error": "测试用"}]}
    cases = build_cases(plan, root=tmp_path)
    assert [c["kind"] for c in cases] == ["clean", "direction", "swap", "entity", "cause", "natural"]
    assert all("skipped" not in c for c in cases)

    def make_llm() -> FakeLLM:  # 只会指出“环比上升”这一处，别的都判通过
        return FlagRisingLLM(replies=[])

    results = run_review_eval(cases, make_llm, tmp_path / "out", workers=2)
    outcomes = {r["kind"]: r.get("outcome") for r in results}
    assert outcomes == {"clean": None, "direction": "detected", "swap": "missed", "entity": "missed", "cause": "missed",
                        "natural": None}
    saved = json.loads((tmp_path / "out" / "runs" / "b-direction" / "material.json").read_text(encoding="utf-8"))
    assert "环比上升" in saved["answer"] and saved["mutation"]["kind"] == "direction" and "run" not in saved
    summary = summarize(results)
    assert summary["seeded_total"] == {"planned": 4, "skipped": 0, "failed": 0, "ambiguous": 0, "counted": 4,
                                       "detected": 1, "as_error": 1}
    assert summary["clean"] == {"answers": 1, "passed": 1, "findings": 0, "errors": 0}
    assert summary["cost"]["reviews"] == 6
    text = render_summary(summary, {"name": "t", "model": "fake", "started_at": "now"})
    assert "| 方向反转 | 1 | 0 | 0 | 0 | 1 | 1/1（100%） | 1/1（100%） |" in text and "| 合计 | 4 |" in text


class FlagRisingLLM(FakeLLM):
    """测试用的假复核员：答案里有“环比上升”就指出来，否则通过。"""

    def chat(self, messages: list, tools: list | None = None) -> LLMReply:
        if messages[-1]["role"] == "tool":
            return LLMReply(content="复核完成。")
        flagged = "环比上升 6.00%" in messages[1]["content"]
        review = {"verdict": "有问题" if flagged else "通过", "checked": "核对了方向",
                  "findings": [{"quote": "环比上升 6.00%", "category": "方向或比较说反", "severity": "错误",
                                "explanation": "第 1 次工具调用里是 -6.00%", "evidence": [1]}] if flagged else []}
        return LLMReply(content="", tool_calls=(ToolCall(id="s", name="submit_review", arguments=review),))


def test_the_committed_plan_builds_without_the_model() -> None:
    """仓库里的评测计划能展开：每份材料都在，植入后都瞒得过数字核查（瞒不过的会被标成 skipped）。"""
    cases = build_cases(load_plan())
    kinds = [c["kind"] for c in cases]
    assert kinds.count("clean") == 16 and kinds.count("natural") == 4
    planted = [c for c in cases if "mutation" in c]
    assert len(planted) >= 50
    originals = {c["base"]: c["run"] for c in cases if c["kind"] == "clean"}
    for c in planted:
        original = originals[c["base"]]
        assert c["run"]["answer"] != original["answer"] and invisible_to_number_check(original, c["run"]["answer"])


@pytest.mark.parametrize("command", [["eval-review", "--llm", "fake", "--name", "review-fake-test"]])
def test_cli_eval_review_with_the_fake_model(command: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    from da_agent import evaluation
    from da_agent.cli import main

    monkeypatch.setattr(evaluation, "RESULTS_DIR", tmp_path)
    assert main(command) == 0
    out = capsys.readouterr().out
    assert "植入的错误：指到了 0/" in out  # 假模型一律判通过：一处都指不出来
    assert (tmp_path / "review-fake-test" / "summary.md").exists()


def test_ambiguous_cases_stay_out_of_the_detection_rate() -> None:
    rows = [{"kind": "direction", "status": "reviewed", "outcome": outcome, "as_error": outcome == "detected",
             "findings": 1, "errors": 1, "tokens": 1000, "seconds": 1.0, "rejected_submissions": 0, "verdict": "有问题"}
            for outcome in ("detected", "missed", "ambiguous")]
    rows.append({"kind": "direction", "status": "skipped"})
    assert summarize(rows)["seeded"]["direction"] == {"planned": 4, "skipped": 1, "failed": 0, "ambiguous": 1, "counted": 2,
                                                      "detected": 1, "as_error": 1}


def test_a_planted_error_the_number_check_can_see_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """植入的必须是数字核查查不出的错。改完后核查结果变了（例如多了一个没出处的数），这份材料就不用。"""
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "run.json").write_text(json.dumps(make_run(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setitem(review_eval.MUTATORS, "cause",
                        lambda answer, run: Mutation("cause", answer + "另有 99,999.00 没有统计。", [(0, 1)], [0], "", ""))
    cases = {c["kind"]: c for c in build_cases({"bases": [{"id": "b", "run": "base"}]}, root=tmp_path)}
    assert cases["cause"]["skipped"] == "改完后数字核查的结果变了（程序自己能查出来）" and "run" not in cases["cause"]
    assert "mutation" in cases["direction"]
