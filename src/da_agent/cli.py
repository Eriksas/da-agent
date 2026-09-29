"""命令行入口。

- `da-agent doctor`：检查配置，不调用模型
- `da-agent demo --llm fake`：用假模型跑示例
- `da-agent prepare-data`：下载并转换数据集
- `da-agent quality`：生成数据质量报告（reports/data_quality.md 和 .json）

过程日志用 logging；命令行给用户看的结果用 print。
"""

import argparse
import logging

from .config import Settings
from .llm import FakeLLM
from .paths import FIXTURES_DIR, REPORTS_DIR

HELLO_FIXTURE = FIXTURES_DIR / "fake_llm/hello.json"


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


def prepare_data(force: bool) -> int:
    """下载、校验并转换数据集。"""
    from .dataset import prepare

    path = prepare(force=force)
    print(f"数据已就绪：{path}")
    return 0


def quality() -> int:
    """在已准备好的数据上生成质量报告，并打印结论。"""
    from .cleaning import connect
    from .dataset import PARQUET_PATH
    from .quality import build_report, render_markdown, write_reports

    if not PARQUET_PATH.exists():
        raise SystemExit("还没有准备数据，请先运行：da-agent prepare-data")
    with connect(PARQUET_PATH) as con:
        report = build_report(con)
    json_path, md_path = write_reports(report, REPORTS_DIR)
    conclusion = render_markdown(report).split("## 1.")[0]
    print(conclusion.strip())
    print(f"\n完整报告：{md_path}\n机器可读：{json_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """解析子命令并执行。"""
    parser = argparse.ArgumentParser(prog="da-agent", description="运营周报与指标异动分析 Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查配置，不调用模型")
    demo_parser = commands.add_parser("demo", help="运行示例")
    demo_parser.add_argument("--llm", choices=["fake"], default="fake", help="使用哪种模型；M0 只有 fake")
    prepare_parser = commands.add_parser("prepare-data", help="下载并转换 UCI Online Retail II 数据集")
    prepare_parser.add_argument("--force", action="store_true", help="即使已有 parquet 也重新转换")
    commands.add_parser("quality", help="生成数据质量报告")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command == "doctor":
        return doctor(Settings())
    if args.command == "prepare-data":
        return prepare_data(args.force)
    if args.command == "quality":
        return quality()
    return demo(args.llm)


if __name__ == "__main__":
    raise SystemExit(main())
