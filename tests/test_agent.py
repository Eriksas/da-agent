"""Agent 循环：正常流程，以及各种“不按剧本走”的情况。全部用假模型，不联网。"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from da_agent.agent import BUDGET_NOTICE, FORMAT_NOTICE, AgentRun, run_agent, save_run
from da_agent.checks import check_run
from da_agent.cleaning import connect_frame
from da_agent.llm import FakeLLM, LLMError, LLMReply, ToolCall
from helpers import make_frame
from test_metrics import ROWS

QUESTION = "2011-W02 的 GMV 为什么变了？"


def call(n: int, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=f"call_{n}", name=name, arguments=dict(arguments))


def tools(*calls: ToolCall) -> LLMReply:
    return LLMReply(content="", tool_calls=calls)


def run_with(replies: list[LLMReply], max_tool_calls: int = 5, llm: FakeLLM | None = None) -> tuple[AgentRun, FakeLLM]:
    llm = llm or FakeLLM(replies=replies)
    with connect_frame(make_frame(ROWS)) as con:
        return run_agent(QUESTION, llm=llm, con=con, max_tool_calls=max_tool_calls), llm


def assert_every_tool_call_answered(run: AgentRun) -> None:
    """接口要求：模型发出的每个工具请求都必须有一条对应的 tool 回复。"""
    requested = [c["id"] for m in run.messages if m["role"] == "assistant" for c in m.get("tool_calls", [])]
    answered = [m["tool_call_id"] for m in run.messages if m["role"] == "tool"]
    assert requested == answered


def test_happy_path() -> None:
    run, llm = run_with([
        tools(call(1, "metric_summary", week="2011-W02")),
        tools(call(2, "decompose_gmv", week="2011-W02"), call(3, "drilldown", week="2011-W02", dimension="country")),
        LLMReply(content="结论：GMV 为 145.0。"),
    ])
    assert (run.status, run.answer) == ("completed", "结论：GMV 为 145.0。")
    assert [(s["round"], s["tool"], s["ok"]) for s in run.steps] == [
        (1, "metric_summary", True), (2, "decompose_gmv", True), (2, "drilldown", True)]
    assert run.usage["llm_calls"] == 3
    assert_every_tool_call_answered(run)
    first_result = json.loads(next(m for m in run.messages if m["role"] == "tool")["content"])
    assert first_result["tool"] == "metric_summary"
    assert llm.received[0]["tools"] is not None  # 每轮都把工具说明书发给模型


def test_system_prompt_has_data_range_and_latest_full_week() -> None:
    run, _ = run_with([LLMReply(content="好")], max_tool_calls=7)
    system = run.messages[0]["content"]
    assert "2011-W01 至 2011-W02" in system
    assert "指 2011-W01（最近的完整周）" in system  # W02 不完整，所以“上周”指 W01
    assert "最多调用 7 次工具" in system
    assert "- `ecommerce-metric-diagnosis`：电商指标异动诊断" in system  # 只放流程目录
    assert "陷阱清单" not in system  # 流程正文按需加载，不预先放进系统提示词


def test_model_recovers_from_bad_arguments() -> None:
    run, _ = run_with([
        tools(call(1, "metric_summary", week="2011-W99")),
        tools(call(2, "metric_summary", week="2011-W02")),
        LLMReply(content="已改正参数后完成分析。"),
    ])
    assert run.status == "completed"
    assert run.steps[0]["ok"] is False and "没有第 99 周" in run.steps[0]["error"]
    assert run.steps[1]["ok"] is True


def test_unknown_tool_is_refused_not_crashed() -> None:
    run, _ = run_with([tools(call(1, "delete_everything")), LLMReply(content="没有这个工具。")])
    assert run.status == "completed"
    assert "没有名为 delete_everything 的工具" in run.steps[0]["error"]


def test_tool_budget_is_enforced() -> None:
    run, _ = run_with([
        tools(call(1, "metric_summary", week="2011-W02"), call(2, "decompose_gmv", week="2011-W02"),
              call(3, "drilldown", week="2011-W02")),
        LLMReply(content="根据已有结果作答。"),
    ], max_tool_calls=2)
    assert run.status == "completed" and run.budget_exhausted
    assert [s["ok"] for s in run.steps] == [True, True, False]
    assert "工具调用次数已用完" in run.steps[2]["error"]
    assert any(m["role"] == "user" and m["content"] == BUDGET_NOTICE for m in run.messages)
    assert_every_tool_call_answered(run)


def test_model_that_never_stops_is_cut_off() -> None:
    replies = [tools(call(n, "metric_summary", week="2011-W02")) for n in range(1, 10)]
    run, llm = run_with(replies, max_tool_calls=1)
    assert run.status == "max_rounds"
    assert len(llm.received) == 1 + 2  # 上限 1 次工具，最多 3 轮
    assert_every_tool_call_answered(run)


def test_llm_failure_is_recorded() -> None:
    class FailingSecondCall(FakeLLM):
        def chat(self, messages, tools=None):  # type: ignore[override]
            if self.received:
                raise LLMError("RateLimitError: 限流")
            return super().chat(messages, tools)

    llm = FailingSecondCall(replies=[tools(call(1, "metric_summary", week="2011-W02"))])
    run, _ = run_with([], llm=llm)
    assert run.status == "llm_error" and "限流" in run.error
    assert len(run.steps) == 1  # 失败前的步骤保留


def test_empty_answer_is_not_completed() -> None:
    run, _ = run_with([LLMReply(content="")])
    assert run.status == "empty_answer"


def test_saved_run_has_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_key = "sk-test-3333333333333333333333"  # secret-scan: allow
    monkeypatch.setenv("LLM_API_KEY", fake_key)
    run, _ = run_with([tools(call(1, "metric_summary", week="2011-W02")), LLMReply(content="好")])
    run_dir = save_run(run, tmp_path)
    text = (run_dir / "run.json").read_text(encoding="utf-8")
    assert json.loads(text)["status"] == "completed"
    assert fake_key not in text
    assert (run_dir / "answer.md").read_text(encoding="utf-8").strip() == "好"


def test_ungrounded_answer_is_sent_back_once_and_fixed() -> None:
    run, llm = run_with([
        tools(call(1, "metric_summary", week="2011-W02")),
        LLMReply(content="GMV 为 145.0，约合 1,234.56。"),  # 1,234.56 在工具结果里找不到
        tools(call(2, "calculate", expression="145 - 140", purpose="GMV 比上周多多少")),
        LLMReply(content="GMV 为 145.0，比上周多 5.0。"),
    ])
    assert (run.status, run.answer) == ("completed", "GMV 为 145.0，比上周多 5.0。")
    assert run.repair["draft"] == "GMV 为 145.0，约合 1,234.56。"
    assert [m["text"] for m in run.repair["ungrounded"]] == ["1,234.56"]
    notice = llm.received[2]["messages"][-1]
    assert notice["role"] == "user" and notice["content"].startswith("（系统核查）") and "「1,234.56」" in notice["content"]
    assert check_run(asdict(run))["numbers"]["ungrounded"] == []  # 退回提示里列出的数字不算出处，修正后的答案也没有问题
    assert_every_tool_call_answered(run)


def test_repair_failure_keeps_the_draft() -> None:
    run, _ = run_with([LLMReply(content="GMV 为 1,234.56。")])  # 剧本没有修正这一轮：等同于修正时模型调用失败
    assert (run.status, run.answer, run.error) == ("completed", "GMV 为 1,234.56。", None)
    assert "假模型只预设了 1 条回复" in run.repair["error"]


def test_repair_happens_only_once() -> None:
    run, llm = run_with([LLMReply(content="GMV 为 1,234.56。"), LLMReply(content="GMV 为 6,543.21。")])
    assert run.answer == "GMV 为 6,543.21。"  # 修正后仍有问题也不再退回，交给核查报告标出来
    assert len(llm.received) == 2


def test_repair_can_be_turned_off() -> None:
    with connect_frame(make_frame(ROWS)) as con:
        run = run_agent(QUESTION, llm=FakeLLM(replies=[LLMReply(content="GMV 为 1,234.56。")]), con=con,
                        max_tool_calls=5, repair=False)
    assert run.repair is None and run.answer == "GMV 为 1,234.56。"


def test_repair_gets_its_own_small_tool_budget() -> None:
    def calc(n: int) -> ToolCall:
        return call(n, "calculate", expression="145 - 140", purpose="差额")

    run, _ = run_with([
        tools(call(1, "metric_summary", week="2011-W02")),  # 上限 1 次，已经用完
        LLMReply(content="GMV 为 1,234.56。"),
        tools(calc(2), calc(3), calc(4), calc(5)),  # 修正时最多再调用 3 次，第 4 次被拒绝
        LLMReply(content="GMV 为 145.0。"),
    ], max_tool_calls=1)
    assert [s["ok"] for s in run.steps] == [True, True, True, True, False]
    assert run.answer == "GMV 为 145.0。"
    assert [m["content"] for m in run.messages if m["role"] == "user"].count(BUDGET_NOTICE) == 2
    assert_every_tool_call_answered(run)


GARBLED = ']<]minimax[>[<tool_call>\n]<]minimax[>[ invoke name="load_skill">]<]minimax[>[</invoke>'  # 评测中出现过的样子（节选）


def test_tool_call_written_as_text_is_retried_once() -> None:
    run, llm = run_with([LLMReply(content=GARBLED), tools(call(1, "metric_summary", week="2011-W02")),
                         LLMReply(content="GMV 为 145.0。")])
    assert (run.status, run.answer, run.malformed_replies) == ("completed", "GMV 为 145.0。", 1)
    assert llm.received[1]["messages"][-1]["content"] == FORMAT_NOTICE


def test_tool_call_written_as_text_twice_is_not_an_answer() -> None:
    """以前这段乱码会被当成最终答案，在周报里作为“AI 解读”发布。"""
    run, _ = run_with([LLMReply(content=GARBLED), LLMReply(content=GARBLED)])
    assert (run.status, run.answer) == ("malformed_answer", "")
    assert "连续两次" in run.error


def test_company_domain_uses_its_own_prompt_and_tools(tmp_path: Path) -> None:
    """同一个循环换一个领域：提示词、工具和流程目录都换成财报的，核查和退回修正不变。"""
    from da_agent.company import connect_financials
    from test_company import write_key_facts

    llm = FakeLLM(replies=[
        tools(call(1, "metric_summary", week="2011-W02")),  # 电商工具：在财报领域里不存在
        tools(call(2, "financial_summary", company="alibaba", fiscal_year=2024)),
        LLMReply(content="阿里巴巴 FY2024 的 ROE 为 8.70%，营业收入同比 25.00%。"),
    ])
    with connect_financials(write_key_facts(tmp_path / "key_facts.csv")) as con:
        run = run_agent("阿里巴巴 FY2024 的盈利能力怎么样？", llm=llm, con=con, max_tool_calls=5, domain="company")
    system = run.messages[0]["content"]
    assert "- `alibaba` 阿里巴巴（BABA）：FY2023–FY2024，财年截至 03-31，人民币" in system
    assert "company-financial-analysis" in system and "ecommerce-metric-diagnosis" not in system
    assert {s["function"]["name"] for s in llm.received[0]["tools"]} >= {"financial_summary", "dupont", "calculate"}
    assert "metric_summary" not in {s["function"]["name"] for s in llm.received[0]["tools"]}
    assert run.steps[0]["ok"] is False and "没有名为 metric_summary 的工具" in run.steps[0]["error"]
    assert (run.status, run.domain, run.repair) == ("completed", "company", None)
    assert check_run(asdict(run))["numbers"]["ungrounded"] == []
