"""评测模块：题集校验、异动注入、打分规则、端到端汇总。全部用小样例和假模型。"""

import json
from pathlib import Path

import pytest

from da_agent.cleaning import connect_frame
from da_agent.evaluation import (
    Case, Connections, inject, load_cases, render_summary, run_eval, score, verify_injection,
)
from da_agent.llm import FakeLLM, LLMReply, ToolCall
from helpers import make_frame
from test_metrics import ROWS


def test_real_case_file_is_valid() -> None:
    cases = load_cases()
    assert len(cases) == 10
    assert {case.id for case in cases} >= {"w49-trap", "out-of-range", "injected-eire"}
    assert next(c for c in cases if c.id == "injected-eire").inject == {"drop_country": "EIRE", "week": "2011-W31"}


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
    assert stats["agent"]["avg_tool_calls"] == 1.0
    assert (tmp_path / "runs" / "c1" / "agent_skill-2" / "report.md").exists()
    saved = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert len(saved) == 5
    markdown = render_summary(summary, {"name": "t", "model": "fake", "repeats": 2, "finished_at": "now"})
    assert "| Agent 带流程 | 2 | 2 | 100% |" in markdown
