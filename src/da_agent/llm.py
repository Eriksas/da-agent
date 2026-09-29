"""模型接口的统一形状，以及不联网的假模型。

Agent 循环（M3）只依赖 LLMClient 这个“形状”：给它消息和工具说明，它返回一条回复。
真实模型和假模型都满足这个形状，所以测试和 CI 可以用假模型跑完整流程，不需要密钥。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    """模型要求调用的一个工具。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMReply:
    """模型的一条回复：要么是文字，要么是一组工具调用请求（也可以两者都有）。"""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()


class LLMClient(Protocol):
    """任何模型客户端都要提供的方法。"""

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
