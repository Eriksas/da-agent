"""假模型：按顺序回放、记录输入、用完报错。"""

from pathlib import Path

import pytest

from da_agent.llm import FakeLLM, FakeLLMExhausted, LLMReply, ToolCall


def test_replays_in_order_and_records_input() -> None:
    llm = FakeLLM(replies=[LLMReply(content="第一条"), LLMReply(content="第二条")])
    assert llm.chat([{"role": "user", "content": "a"}]).content == "第一条"
    assert llm.chat([{"role": "user", "content": "b"}], tools=[{"name": "t"}]).content == "第二条"
    assert llm.received[1]["tools"] == [{"name": "t"}]


def test_raises_when_replies_run_out() -> None:
    llm = FakeLLM(replies=[LLMReply(content="唯一一条")])
    llm.chat([])
    with pytest.raises(FakeLLMExhausted):
        llm.chat([])


def test_loads_tool_calls_from_file(tmp_path: Path) -> None:
    path = tmp_path / "replies.json"
    path.write_text(
        '{"replies": [{"content": "", "tool_calls": [{"id": "c1", "name": "metric_summary", "arguments": {"week": "2011-W01"}}]}]}',
        encoding="utf-8",
    )
    reply = FakeLLM.from_file(path).chat([])
    assert reply.tool_calls == (ToolCall(id="c1", name="metric_summary", arguments={"week": "2011-W01"}),)
