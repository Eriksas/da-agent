"""命令行入口：`da-agent doctor` 检查配置，`da-agent demo` 跑示例。

过程日志用 logging；命令行给用户看的结果用 print。
"""

import argparse
from pathlib import Path

from .config import Settings
from .llm import FakeLLM

ROOT = Path(__file__).resolve().parents[2]
HELLO_FIXTURE = ROOT / "fixtures/fake_llm/hello.json"


def doctor(settings: Settings) -> int:
    """打印配置状态。永远不打印密钥原文。"""
    print(f"模型地址：{settings.llm_base_url}")
    print(f"模型名称：{settings.llm_model}")
    print(f"API Key：{'已配置' if settings.has_api_key() else '未配置（只能使用 --llm fake）'}")
    print(f"单次运行工具调用上限：{settings.llm_max_tool_calls}")
    return 0


def demo(llm_kind: str) -> int:
    """M0 冒烟测试：用假模型走通一次“发消息 → 收回复”。Agent 循环在 M3 实现。"""
    if llm_kind != "fake":
        raise SystemExit("M0 只支持 --llm fake；真实模型在 M3 接入")
    llm = FakeLLM.from_file(HELLO_FIXTURE)
    reply = llm.chat([{"role": "user", "content": "你好，请确认你能工作。"}])
    print(f"假模型回复：{reply.content}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """解析子命令并执行。"""
    parser = argparse.ArgumentParser(prog="da-agent", description="运营周报与指标异动分析 Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查配置，不调用模型")
    demo_parser = commands.add_parser("demo", help="运行示例")
    demo_parser.add_argument("--llm", choices=["fake"], default="fake", help="使用哪种模型；M0 只有 fake")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor(Settings())
    return demo(args.llm)


if __name__ == "__main__":
    raise SystemExit(main())
