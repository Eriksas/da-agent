"""评测模块：题集校验、异动注入、打分规则、端到端汇总。全部用小样例和假模型。"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from da_agent import cli
from da_agent.agent import AgentRun, run_agent
from da_agent.checks import check_run
from da_agent.cleaning import connect_frame
from da_agent.evaluation import (
    HOLDOUT_PATH, Case, Connections, inject, load_cases, render_summary, rescore, run_eval, score, score_run,
    verify_injection,
)
from da_agent.llm import FakeLLM, LLMReply, ToolCall
from helpers import make_frame
from test_metrics import ROWS


def test_real_case_file_is_valid() -> None:
    cases = load_cases()
    assert len(cases) == 10
    assert {case.id for case in cases} >= {"w49-trap", "out-of-range", "injected-eire"}
    assert next(c for c in cases if c.id == "injected-eire").inject == {"drop_country": "EIRE", "week": "2011-W31"}


def test_holdout_file_is_valid_and_separate() -> None:
    holdout = load_cases(HOLDOUT_PATH)
    assert len(holdout) == 5
    assert not {c.id for c in holdout} & {c.id for c in load_cases()}  # 和原题集重名的话，结果会混在一起


def test_rescore_uses_the_case_file_recorded_in_meta(tmp_path: Path) -> None:
    run = asdict(AgentRun(run_id="r", question="2011-W41 的 GMV 为什么下降？", model="fake", max_tool_calls=5,
                          status="completed", answer="2011-W41 的客单价下降。"))
    run["messages"] = [{"role": "system", "content": "s"}, {"role": "user", "content": run["question"]}]
    run_dir = tmp_path / "runs" / "w41-why" / "agent-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    meta = {"name": "t", "model": "fake", "repeats": 1, "cases_file": "eval/holdout.yaml", "finished_at": "now"}
    (tmp_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert cli.main(["eval-rescore", str(tmp_path)]) == 0  # w41-why 只在留出题里：读错题集会找不到这道题
    assert "[eval/holdout.yaml](../../holdout.yaml)" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))[0]["passed"] is True


@pytest.mark.parametrize("text, message", [
    ("cases:\n  - {id: a, category: x, question: q}\n  - {id: a, category: x, question: q}\n", "重复"),
    ("cases:\n  - {id: a, category: x, question: q, must_not: ['(']}\n", "subpattern"),
], ids=["duplicate-id", "bad-regex"])
def test_invalid_case_files_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(Exception, match=message):
        load_cases(path)


def test_inject_drops_one_country_week() -> None:
    frame = make_frame(ROWS)
    injected = inject(frame, {"drop_country": "United Kingdom", "week": "2011-W02"})
    assert len(frame) - len(injected) == 3  # W02 英国：300005、300009、C300010
    with pytest.raises(ValueError, match="没有命中"):
        inject(frame, {"drop_country": "Japan", "week": "2011-W02"})
    with pytest.raises(ValueError, match="不支持"):
        inject(frame, {"double_country": "UK"})


def test_injection_is_verified_before_scoring() -> None:
    frame = make_frame(ROWS)
    uk = {"drop_country": "United Kingdom", "week": "2011-W02"}
    with connect_frame(inject(frame, uk)) as con:
        verify_injection(con, uk)  # 英国 110 → 0，成为最大拖累项
    germany = {"drop_country": "Germany", "week": "2011-W02"}  # 德国上周本来就是 0，删掉后不会成为拖累项
    with connect_frame(inject(frame, germany)) as con, pytest.raises(ValueError, match="标准答案不成立"):
        verify_injection(con, germany)


CASE = Case(id="c1", category="测试", question="2011-W02 的 GMV 为什么变了？",
            must_mention=(("2011-W02",), ("客单价", "AOV")), must_not=("全靠",))
CHECKS_OK = {"numbers": {"checked": 2, "ungrounded": []}, "process": {"skills": []}}


def fake_run(answer: str, status: str = "completed") -> dict:
    return {"answer": answer, "status": status, "steps": [], "seconds": 1.0,
            "usage": {"llm_calls": 1, "prompt_tokens": 10, "completion_tokens": 5}}


def test_score_pass_and_each_failure_reason() -> None:
    assert score(CASE, fake_run("2011-W02 的客单价下降。"), CHECKS_OK, None)["passed"] is True
    missed = score(CASE, fake_run("2011-W02 的 GMV 上升。"), CHECKS_OK, None)
    assert missed["passed"] is False and missed["mentions"][1]["hit"] is False
    violated = score(CASE, fake_run("2011-W02 的客单价下降，增长全靠老客。"), CHECKS_OK, None)
    assert violated["violations"] == ["全靠"]
    assert violated["passed"] is False  # 变异测试发现：原来只查了“记录了违规”，没查“有违规就不通过”
    ungrounded = {"numbers": {"checked": 2, "ungrounded": [{"text": "9.9"}]}, "process": {"skills": []}}
    assert score(CASE, fake_run("2011-W02 的客单价下降。"), ungrounded, None)["passed"] is False
    assert score(CASE, fake_run("2011-W02 的客单价下降。", status="llm_error"), CHECKS_OK, None)["passed"] is False


def test_expected_value_must_appear() -> None:
    case = Case(id="c2", category="查数", question="订单数？")
    assert score(case, fake_run("订单数为 1,234 单。"), CHECKS_OK, 1234.0)["value_found"] is True
    assert score(case, fake_run("订单数为 1,243 单。"), CHECKS_OK, 1234.0)["passed"] is False


class FixtureConnections(Connections):
    """评测端到端测试用：不读真实数据，直接用小样例。"""

    def __init__(self) -> None:
        super().__init__()
        self.con = connect_frame(make_frame(ROWS))

    def get(self, case: Case):  # type: ignore[override]
        return self.con


def test_run_eval_end_to_end(tmp_path: Path) -> None:
    def make_llm(case: Case, condition: str, i: int) -> FakeLLM:
        answer = LLMReply(content="2011-W02 的 GMV 为 145.0，客单价下降。")
        if condition == "baseline":
            return FakeLLM(replies=[answer])  # 直接问模型：145.00 在它拿到的指标表里
        calls = (ToolCall(id="s", name="load_skill", arguments={"name": "ecommerce-metric-diagnosis"}),) \
            if condition == "agent_skill" else ()
        return FakeLLM(replies=[LLMReply(content="", tool_calls=calls + (
            ToolCall(id="m", name="metric_summary", arguments={"week": "2011-W02"}),)), answer])

    summary = run_eval([CASE], ["baseline", "agent", "agent_skill"], 2, make_llm, FixtureConnections(),
                       tmp_path, max_tool_calls=5, progress=lambda _: None)
    stats = summary["by_condition"]
    assert summary["runs"] == 1 + 2 + 2  # 直接问模型固定 1 次，Agent 两组各 2 次
    assert stats["baseline"]["passed"] == 1 and stats["agent"]["passed"] == 2
    assert stats["agent_skill"]["process_complete_rate"] == 0.0  # 加载了流程，但没做拆解和下钻
    assert stats["agent_skill"]["skill_use_correct_rate"] == 1.0  # 这道题应该用流程，也确实加载了
    assert stats["agent"]["avg_tool_calls"] == 1.0
    assert (tmp_path / "runs" / "c1" / "agent_skill-2" / "report.md").exists()
    saved = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert len(saved) == 5
    markdown = render_summary(summary, {"name": "t", "model": "fake", "repeats": 2, "finished_at": "now"})
    assert "| Agent 带流程 | 2 | 2 | 100% |" in markdown
    rescored = rescore(tmp_path, [CASE], FixtureConnections())  # 规则没变时，离线重评结果应完全一致
    assert rescored["by_condition"] == summary["by_condition"]


def test_quick_lookup_without_skill_is_correct_use() -> None:
    """查数题按流程文件的要求不加载流程：不能算成“流程没完成”。"""
    case = Case(id="q", category="查数", question="订单数？", expects_skill=False)
    result = score(case, fake_run("订单数为 634 单。"), CHECKS_OK, None)
    assert result["skill_loaded"] is False and result["skill_expected"] is False
    assert result["process_complete"] is None


def test_compare_with_human_counts_both_views() -> None:
    from da_agent.evaluation import compare_with_human

    results = [
        {"case": "c", "condition": "agent", "repeat": 1, "status": "completed", "passed": False,
         "mentions": [{"hit": True}], "violations": []},   # 要点都对，但有自己算的数字：自动判失败
        {"case": "c", "condition": "agent", "repeat": 2, "status": "completed", "passed": True,
         "mentions": [{"hit": True}], "violations": []},   # 自动通过，人工发现推理错误
    ]
    review = {"reviewer": "r", "method": "m", "labels": [
        {"case": "c", "condition": "agent", "repeat": 1, "blind": True, "human_pass": True, "reason": "对"},
        {"case": "c", "condition": "agent", "repeat": 2, "blind": False, "human_pass": False, "reason": "推理错"}]}
    result = compare_with_human(results, review)
    assert (result["agree_substance"], result["agree_passed"], result["blind"]) == (1, 0, 1)
    assert result["by_condition"] == {"agent": {"human_passed": 1, "reviewed": 2}}


def test_repair_is_scored_before_and_after(tmp_path: Path) -> None:
    def make_llm(case: Case, condition: str, i: int) -> FakeLLM:
        return FakeLLM(replies=[
            LLMReply(content="", tool_calls=(ToolCall(id="m", name="metric_summary", arguments={"week": "2011-W02"}),)),
            LLMReply(content="2011-W02 的客单价下降，GMV 约 999.99。"),  # 编的数字：被退回修正
            LLMReply(content="2011-W02 的客单价下降，GMV 为 145.0。")])

    summary = run_eval([CASE], ["agent"], 1, make_llm, FixtureConnections(), tmp_path, max_tool_calls=5,
                       progress=lambda _: None)
    stats = summary["by_condition"]["agent"]
    assert (stats["repaired"], stats["passed_without_repair"], stats["passed"]) == (1, 0, 1)
    markdown = render_summary(summary, {"name": "t", "model": "fake", "repeats": 1, "finished_at": "now"})
    assert "| Agent 不带流程 | 1 | 1 | 0 | 1 |" in markdown
    assert rescore(tmp_path, [CASE], FixtureConnections())["by_condition"] == summary["by_condition"]


def test_draft_is_checked_only_against_what_existed_before_repair() -> None:
    """初稿和修正稿文字相同，区别只在修正时补了一次 calculate：初稿不能借用修正阶段才有的出处。"""
    text = "2011-W02 的客单价下降，GMV 比上周多 3.5714%。"  # 工具只给到 0.0357，4 位小数的写法找不到出处
    llm = FakeLLM(replies=[
        LLMReply(content="", tool_calls=(ToolCall(id="m", name="metric_summary", arguments={"week": "2011-W02"}),)),
        LLMReply(content=text),
        LLMReply(content="", tool_calls=(ToolCall(id="c", name="calculate",
                                                  arguments={"expression": "(145 - 140) / 140", "purpose": "环比"}),)),
        LLMReply(content=text)])
    with connect_frame(make_frame(ROWS)) as con:
        run = asdict(run_agent(CASE.question, llm=llm, con=con, max_tool_calls=5))
    result = score_run(CASE, run, check_run(run), None)
    assert (result["repaired"], result["passed_without_repair"], result["passed"]) == (True, False, True)
