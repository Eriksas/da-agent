"""模型接口的统一形状、不联网的假模型，以及 OpenAI 兼容接口的真实模型客户端。

Agent 循环只依赖 LLMClient 这个“形状”：给它对话历史和工具说明，它返回一条回复。
真实模型和假模型都满足这个形状，所以测试和 CI 用假模型就能跑完整流程，不需要密钥。
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import Settings

THINK_TAGS = re.compile(r"<think>.*?</think>", re.DOTALL)  # 推理模型可能夹在正文里的思考过程
INVALID_JSON_KEY = "__invalid_json__"  # 模型给的工具参数不是合法 JSON 时，原文放在这个键下交给工具层报错


@dataclass(frozen=True)
class ToolCall:
    """模型要求调用的一个工具。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMReply:
    """模型的一条回复：文字答案，或一组工具调用请求（也可以两者都有）。"""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    raw_message: dict[str, Any] | None = None  # 真实模型返回的完整消息，原样放回对话历史
    usage: dict[str, int] | None = None  # token 用量

    def as_message(self) -> dict[str, Any]:
        """放回对话历史的 assistant 消息。

        真实模型的消息原样放回：MiniMax 要求多轮工具调用时保留完整消息（含思考内容），否则推理会断。
        """
        if self.raw_message is not None:
            return self.raw_message
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": call.id, "type": "function",
                 "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                for call in self.tool_calls
            ]
        return message


class LLMError(RuntimeError):
    """模型调用失败：网络、认证、限流、超时或返回格式不对。"""


class LLMClient(Protocol):
    """任何模型客户端都要提供的属性和方法。"""

    model: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMReply:
        """发送对话历史和可用工具，返回下一条回复。"""
        ...


class FakeLLMExhausted(RuntimeError):
    """假模型预设的回复已经用完。"""


@dataclass
class FakeLLM:
    """按顺序回放预先写好的回复。不联网、不需要密钥，并记录每次收到的输入方便测试检查。"""

    replies: list[LLMReply]
    received: list[dict[str, Any]] = field(default_factory=list)
    model: str = "fake"

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMReply:
        """返回下一条预设回复。"""
        self.received.append({"messages": list(messages), "tools": tools})
        if len(self.received) > len(self.replies):
            raise FakeLLMExhausted(f"假模型只预设了 {len(self.replies)} 条回复")
        return self.replies[len(self.received) - 1]

    @classmethod
    def from_file(cls, path: Path) -> "FakeLLM":
        """从 JSON 文件读取预设回复。格式：{"replies": [{"content": "...", "tool_calls": [...]}]}。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        replies = [
            LLMReply(
                content=item.get("content", ""),
                tool_calls=tuple(
                    ToolCall(id=call["id"], name=call["name"], arguments=call.get("arguments", {}))
                    for call in item.get("tool_calls", [])
                ),
            )
            for item in data["replies"]
        ]
        return cls(replies=replies)


def parse_response(response: Any) -> LLMReply:
    """把 OpenAI 兼容接口的返回值转成 LLMReply：剥离思考内容、解析工具参数、记录 token 用量。"""
    if not response.choices:
        raise LLMError("模型返回为空（没有 choices）")
    message = response.choices[0].message
    calls = []
    for call in message.tool_calls or []:
        raw_arguments = call.function.arguments or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict):  # 不是合法 JSON 对象：不在这里报错，交给工具层告诉模型改正
            arguments = {INVALID_JSON_KEY: raw_arguments}
        calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
    usage = None
    if response.usage is not None:
        usage = {"prompt_tokens": response.usage.prompt_tokens or 0,
                 "completion_tokens": response.usage.completion_tokens or 0}
    return LLMReply(content=THINK_TAGS.sub("", message.content or "").strip(), tool_calls=tuple(calls),
                    raw_message=message.model_dump(exclude_none=True), usage=usage)


class OpenAICompatibleLLM:
    """真实模型客户端（默认 MiniMax）。密钥只在构造时从配置读取，不写日志、不进运行记录。"""

    def __init__(self, settings: Settings) -> None:
        from openai import OpenAI

        self.model = settings.llm_model
        self.temperature = settings.llm_temperature
        self._client = OpenAI(base_url=settings.llm_base_url, api_key=settings.require_api_key(),
                              timeout=settings.llm_timeout_seconds, max_retries=2)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMReply:
        """调用一次模型。网络抖动和限流由 SDK 自动重试 2 次，仍失败则抛出 LLMError。"""
        import openai

        kwargs: dict[str, Any] = {
            "model": self.model, "messages": messages,
            "extra_body": {"reasoning_split": True},  # MiniMax 扩展参数：思考内容放进单独字段，不夹在正文里
        }
        if tools:
            kwargs["tools"] = tools
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        try:
            response = self._client.chat.completions.create(**kwargs)
        except openai.APIError as exc:  # 认证失败、限流、超时、服务端错误
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        return parse_response(response)


def ping(settings: Settings) -> dict[str, Any]:
    """发 1 次最小请求，确认地址、模型和密钥可用。返回耗时、回复和 token 用量，不返回密钥。"""
    client = OpenAICompatibleLLM(settings)
    start = time.perf_counter()
    reply = client.chat([{"role": "user", "content": "这是连通性测试，请只回复“收到”两个字。"}])
    return {"model": settings.llm_model, "reply": reply.content[:50],
            "seconds": round(time.perf_counter() - start, 2), "usage": reply.usage}
