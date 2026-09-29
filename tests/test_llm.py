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


# ---- 真实模型客户端：用构造出来的返回值测试，不联网 ----

import openai
from openai.types.chat import ChatCompletion

from da_agent.config import Settings
from da_agent.llm import INVALID_JSON_KEY, LLMError, OpenAICompatibleLLM, parse_response


def completion(content: str | None, tool_calls: list | None = None, extra: dict | None = None,
               choices: bool = True) -> ChatCompletion:
    message = {"role": "assistant", "content": content, **({"tool_calls": tool_calls} if tool_calls else {}), **(extra or {})}
    return ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}] if choices else [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


def test_think_tags_removed_from_answer_but_kept_in_history() -> None:
    reply = parse_response(completion("<think>先看 GMV</think>\n结论：GMV 上升。", extra={"reasoning_details": [{"text": "…"}]}))
    assert reply.content == "结论：GMV 上升。"
    assert reply.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    history = reply.as_message()
    assert "<think>" in history["content"]  # 原样放回对话历史，保持推理连贯
    assert history["reasoning_details"] == [{"text": "…"}]  # 服务商的扩展字段也保留


def test_tool_call_arguments_parsed_and_invalid_json_kept_for_tool_layer() -> None:
    calls = [{"id": "c1", "type": "function", "function": {"name": "metric_summary", "arguments": '{"week": "2011-W48"}'}},
             {"id": "c2", "type": "function", "function": {"name": "drilldown", "arguments": '{"week": '}}]
    reply = parse_response(completion(None, calls))
    assert reply.content == ""
    assert reply.tool_calls[0].arguments == {"week": "2011-W48"}
    assert reply.tool_calls[1].arguments == {INVALID_JSON_KEY: '{"week": '}


def test_empty_choices_raise() -> None:
    with pytest.raises(LLMError, match="没有 choices"):
        parse_response(completion("x", choices=False))


def test_fake_reply_becomes_openai_style_message() -> None:
    reply = LLMReply(content="", tool_calls=(ToolCall(id="c1", name="drilldown", arguments={"week": "2011-W48"}),))
    message = reply.as_message()
    assert message["tool_calls"][0] == {"id": "c1", "type": "function",
                                        "function": {"name": "drilldown", "arguments": '{"week": "2011-W48"}'}}


def _client(monkeypatch: pytest.MonkeyPatch, **settings: object) -> OpenAICompatibleLLM:
    monkeypatch.setenv("LLM_API_KEY", "sk-test-2222222222222222222222")  # secret-scan: allow
    return OpenAICompatibleLLM(Settings(_env_file=None, **settings))


def test_request_carries_tools_and_reasoning_split(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    sent: dict = {}
    monkeypatch.setattr(client._client.chat.completions, "create", lambda **kw: sent.update(kw) or completion("好"))
    assert client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}]).content == "好"
    assert sent["extra_body"] == {"reasoning_split": True}
    assert sent["tools"] == [{"type": "function"}]
    assert "temperature" not in sent  # 没配置就用模型默认值
    client_t = _client(monkeypatch, llm_temperature=0.2)
    monkeypatch.setattr(client_t._client.chat.completions, "create", lambda **kw: sent.update(kw) or completion("好"))
    client_t.chat([])
    assert sent["temperature"] == 0.2


def test_api_errors_become_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx2  # OpenAI SDK v3 使用的网络库

    client = _client(monkeypatch)

    def fail(**_: object) -> None:
        raise openai.APITimeoutError(request=httpx2.Request("POST", "https://example.invalid"))

    monkeypatch.setattr(client._client.chat.completions, "create", fail)
    with pytest.raises(LLMError, match="APITimeoutError"):
        client.chat([])
