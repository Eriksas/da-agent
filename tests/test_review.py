"""复核 Agent（M15）：复核意见的格式和程序校验、交给它的材料、完整的复核流程（假模型）、报告里的展示。"""

import json
from pathlib import Path

import pytest

from da_agent.cli import main
from da_agent.llm import FakeLLM, LLMReply, ToolCall
from da_agent.report import render_report, review_summary, write_review
from da_agent.review import ReviewContext, ReviewProblems, SubmitReviewArgs, locate, review_request, submit_review, tool_records
from da_agent.reviewer import final_review, review_run, write_agent_review
from da_agent.tools import execute

ANSWER = "本周 GMV 为 120,000.00，环比增长 6.00%。\n荷兰的订单数最多，共 35 单。客单价下降是因为促销力度加大。"
SUMMARY = {"tool": "metric_summary", "metrics": [{"label": "GMV", "unit": "money", "current": 120000.0, "change_pct": -0.06}],
           "warnings": ["2011-W49 只有 5 个交易日，总量不能和上周直接比较"]}
DRILL = {"tool": "drilldown", "rows": [{"group": "荷兰", "orders": 12}, {"group": "英国", "orders": 35}]}


def make_run(answer: str = ANSWER) -> dict:
    steps = [{"round": 1, "tool": "metric_summary", "arguments": {"week": "2011-W49"}, "ok": True, "error": None, "ms": 3},
             {"round": 1, "tool": "drilldown", "arguments": {"week": "2011-W49", "dimension": "country"}, "ok": True,
              "error": None, "ms": 4}]
    messages = [{"role": "system", "content": "提示词"}, {"role": "user", "content": "2011-W49 的 GMV 怎么样？"},
                {"role": "assistant", "content": "（这是写答案的模型的思考，不应该交给复核 Agent）"},
                {"role": "tool", "tool_call_id": "c1", "content": json.dumps(SUMMARY, ensure_ascii=False)},
                {"role": "tool", "tool_call_id": "c2", "content": json.dumps(DRILL, ensure_ascii=False)},
                {"role": "assistant", "content": answer}]
    return {"run_id": "r1", "question": "2011-W49 的 GMV 怎么样？", "model": "writer", "status": "completed", "answer": answer,
            "steps": steps, "messages": messages, "max_tool_calls": 15, "budget_exhausted": False, "repair": None,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "llm_calls": 1}, "seconds": 1.0}


DIRECTION = {"quote": "环比增长 6.00%", "category": "方向或比较说反", "severity": "错误",
             "explanation": "第 1 次工具调用里 GMV 环比是 -6.00%，答案写成了增长", "evidence": [1]}
CAUSE = {"quote": "客单价下降是因为促销力度加大", "category": "因果或推测没有依据", "severity": "存疑",
         "explanation": "工具结果里没有促销相关的数据"}
GOOD = {"verdict": "有问题", "findings": [DIRECTION, CAUSE], "checked": "核对了 GMV 的数值和方向、国家订单数、因果表述"}
CTX = ReviewContext(answer=ANSWER, tool_calls=2)


def _submit(payload: dict, ctx: ReviewContext = CTX) -> dict:
    return submit_review(ctx, SubmitReviewArgs.model_validate(payload))


# ---------- 程序校验复核意见 ----------
def test_accepted_findings_point_at_the_sentence() -> None:
    out = _submit(GOOD)
    first = out["findings"][0]
    assert out["verdict"] == "有问题" and ANSWER[first["start"]:first["end"]] == "环比增长 6.00%"
    assert out["findings"][1]["evidence"] == []  # “没有依据”这一类可以不填依据


@pytest.mark.parametrize(("change", "message"), [
    ({"findings": [DIRECTION | {"quote": "环比上涨了 6%"}]}, "quote 在答案里找不到"),
    ({"findings": [DIRECTION | {"evidence": [3]}]}, r"\[3\] 不是有效的工具调用编号（共 2 次"),
    ({"findings": [DIRECTION | {"evidence": [0]}]}, "不是有效的工具调用编号"),
    ({"findings": [DIRECTION | {"evidence": []}]}, "要在 evidence 里写出依据"),
    ({"verdict": "通过"}, "verdict 是“通过”，但有 1 条“错误”级别的意见"),
    ({"findings": [CAUSE]}, "verdict 是“有问题”，但没有“错误”级别的意见"),
])
def test_unsupported_reviews_are_sent_back(change: dict, message: str) -> None:
    with pytest.raises(ReviewProblems, match=message):
        _submit(GOOD | change)


def test_a_clean_pass_needs_no_findings() -> None:
    out = _submit({"verdict": "通过", "findings": [], "checked": "核对了全部结论句"})
    assert out["accepted"] and out["findings"] == []
    assert _submit({"verdict": "通过", "findings": [CAUSE], "checked": "核对了全部结论句"})["verdict"] == "通过"  # 只有存疑也算通过


def test_quotes_are_found_despite_whitespace() -> None:
    assert locate(ANSWER, "共 35 单。客单价下降") == (ANSWER.index("共 35 单"), ANSWER.index("客单价下降") + 5)
    start, end = locate(ANSWER, "环比增长6.00%。 荷兰的订单数最多")  # 模型照抄时常丢空格、把换行写成空格
    assert ANSWER[start:end] == "环比增长 6.00%。\n荷兰的订单数最多"
    assert locate(ANSWER, "环比下降 6.00%") is None and locate(ANSWER, "   ") is None


def test_the_tool_layer_turns_problems_into_feedback() -> None:
    bad = execute(CTX, "submit_review", GOOD | {"verdict": "通过"})
    assert not bad.ok and "复核意见没有通过检查" in bad.content["error"]
    nested = execute(CTX, "submit_review", {"review": GOOD})
    assert not nested.ok and "正确写法" in nested.content["error"]  # 参数结构写错时附上示例


# ---------- 交给复核 Agent 的材料 ----------
def test_request_holds_question_answer_and_numbered_tool_results_only() -> None:
    run = make_run()
    text = review_request(run)
    assert "### 第 1 次：metric_summary" in text and "### 第 2 次：drilldown" in text
    assert "只有 5 个交易日" in text and ANSWER in text and run["question"] in text
    assert "思考" not in text and "提示词" not in text  # 不给写答案的模型的思考过程和系统提示词
    assert [r["no"] for r in tool_records(run)] == [1, 2]
    assert "没有调用任何工具" in review_request(run | {"steps": [], "messages": run["messages"][:2]})


# ---------- 完整流程 ----------
def _call(payload: dict, call_id: str = "s1") -> LLMReply:
    return LLMReply(content="", tool_calls=(ToolCall(id=call_id, name="submit_review", arguments=payload),))


def test_review_run_keeps_the_last_accepted_submission() -> None:
    llm = FakeLLM(replies=[
        LLMReply(content="", tool_calls=(ToolCall(id="c", name="calculate", arguments={
            "expression": "35 - 12", "purpose": "英国和荷兰的订单数之差"}),)),
        _call(GOOD | {"findings": [DIRECTION | {"quote": "环比上涨"}]}),  # 引用不是原文：被退回
        _call(GOOD | {"findings": [DIRECTION]}, "s2"),  # 合格，但后面又交了一版更完整的
        _call(GOOD, "s3"),
        LLMReply(content="答案把 GMV 环比的方向说反了，共核对 37 句。")])  # 37 不在任何工具结果里：复核的结语不查数字出处
    out = review_run(make_run(), llm=llm)
    review = out["review"]
    assert (review["status"], review["verdict"], review["rejected_submissions"]) == ("reviewed", "有问题", 1)
    assert [f["category"] for f in review["findings"]] == ["方向或比较说反", "因果或推测没有依据"]  # 以最后一版为准
    assert final_review(out["run"])["checked"] == GOOD["checked"]
    sent = llm.received[0]
    assert [t["function"]["name"] for t in sent["tools"]] == ["calculate", "submit_review"]  # 复核 Agent 不能查数据
    assert "数据分析复核员" in sent["messages"][0]["content"] and "{max_tool_calls}" not in sent["messages"][0]["content"]
    assert out["run"]["repair"] is None and out["run"]["domain"] == "review"  # 复核意见不走“数字出处退回修正”


def test_a_rejected_resubmission_does_not_replace_the_accepted_one() -> None:
    llm = FakeLLM(replies=[_call(GOOD), _call(GOOD | {"verdict": "通过"}, "s2"),  # 先交了合格的，又交了一版自相矛盾的
                           LLMReply(content="复核完成。")])
    review = review_run(make_run(), llm=llm)["review"]
    assert (review["status"], review["verdict"], review["rejected_submissions"]) == ("reviewed", "有问题", 1)


def test_review_failures_do_not_raise() -> None:
    assert review_run(make_run(answer=""), llm=FakeLLM(replies=[]))["review"]["status"] == "no_answer"
    failed = review_run(make_run(), llm=FakeLLM(replies=[LLMReply(content="我觉得没问题。")]))["review"]
    assert failed["status"] == "failed" and failed["verdict"] is None and "没有提交合格的复核意见" in failed["error"]
    broken = review_run(make_run(), llm=FakeLLM(replies=[]))["review"]
    assert broken["status"] == "failed" and "假模型" in broken["error"]


# ---------- 报告和命令行 ----------
def test_report_shows_the_review_only_when_there_is_one(tmp_path: Path) -> None:
    run = make_run()
    (tmp_path / "run.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    write_review(tmp_path)
    assert "复核 Agent" not in (tmp_path / "report.md").read_text(encoding="utf-8")  # 没复核过：报告和以前一样
    review = write_agent_review(tmp_path, llm=FakeLLM(replies=[_call(GOOD), LLMReply(content="有 1 处错误。")]))
    assert review_summary(review) == "复核 Agent：指出 1 处错误、1 处存疑"
    assert json.loads((tmp_path / "review_run.json").read_text(encoding="utf-8"))["domain"] == "review"
    write_review(tmp_path)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "复核 Agent：指出 1 处错误、1 处存疑。" in report.splitlines()[0]
    assert "| 错误 | 方向或比较说反 | 环比增长 6.00% |" in report and "同样是 AI 的判断" in report
    assert review_summary({"status": "reviewed", "findings": []}) == "复核 Agent：没有发现问题"
    assert review_summary(None) == "" and "没有完成" in review_summary({"status": "failed", "error": "超时"})
    assert "附录 G" not in render_report(run, json.loads((tmp_path / "checks.json").read_text(encoding="utf-8")))


def test_cli_review_writes_files_and_signals_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "run.json").write_text(json.dumps(make_run(), ensure_ascii=False), encoding="utf-8")
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"replies": [
        {"tool_calls": [{"id": "s1", "name": "submit_review", "arguments": GOOD}]}, {"content": "有 1 处错误。"}]},
        ensure_ascii=False), encoding="utf-8")
    assert main(["review", str(tmp_path), "--llm", "fake", "--script", str(script)]) == 1  # 有“错误”级别的意见
    out = capsys.readouterr().out
    assert "复核 Agent：指出 1 处错误、1 处存疑" in out and "[错误] 方向或比较说反：「环比增长 6.00%」" in out
    assert (tmp_path / "review.json").exists() and "附录 G" in (tmp_path / "report.md").read_text(encoding="utf-8")
