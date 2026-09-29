"""命令行入口。

- `da-agent doctor [--ping]`：检查配置；加 --ping 发 1 次真实请求测试连通性
- `da-agent demo`：离线演示，用假模型剧本在内置小样例上走完整个 Agent 循环
- `da-agent prepare-data`：下载并转换数据集
- `da-agent quality`：生成数据质量报告（reports/data_quality.md 和 .json）
- `da-agent week 2011-W48`：不经过模型，直接用分析工具输出某一周的指标、拆解和下钻
- `da-agent ask "问题"`：用真实模型（或 --llm fake 加剧本）回答问题，运行记录、核查结果和报告存到 runs/
- `da-agent check runs/<运行编号>`：对已保存的运行补做核查，重新生成报告，不调用模型

过程日志用 logging；命令行给用户看的结果用 print。
"""

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .config import MissingApiKeyError, Settings
from .llm import FakeLLM
from .paths import FIXTURES_DIR, REPORTS_DIR, RUNS_DIR

if TYPE_CHECKING:
    from .agent import AgentRun

DEMO_DATA = FIXTURES_DIR / "demo/transactions.csv"
DEMO_SCRIPT = FIXTURES_DIR / "fake_llm/demo_run.json"
DEMO_QUESTION = "2011-W02 的 GMV 为什么变了？"


def doctor(settings: Settings, ping_model: bool = False) -> int:
    """打印配置状态；加 --ping 时发 1 次真实请求测试连通性。永远不打印密钥原文。"""
    print(f"模型地址：{settings.llm_base_url}")
    print(f"模型名称：{settings.llm_model}")
    print(f"API Key：{'已配置' if settings.has_api_key() else '未配置（只能使用 --llm fake）'}")
    print(f"单次运行工具调用上限：{settings.llm_max_tool_calls}")
    if not ping_model:
        return 0
    from .llm import LLMError, ping

    if not settings.has_api_key():
        print("无法测试连通性：没有配置 API Key")
        return 2
    print("正在发送 1 次测试请求……")
    try:
        result = ping(settings)
    except LLMError as exc:
        print(f"连通性测试失败：{exc}")
        return 2
    print(f"连通性测试成功：用时 {result['seconds']} 秒，模型回复“{result['reply']}”，token 用量 {result['usage']}")
    return 0


def _print_run(run: "AgentRun", run_dir: Path) -> None:
    """打印一次 Agent 运行：每一步调了什么工具、最终答案、状态和用量。"""
    import json

    print(f"问题：{run.question}")
    print()
    print("## 工具调用")
    for step in run.steps:
        mark = "成功" if step["ok"] else f"失败：{step['error']}"
        print(f"- 第 {step['round']} 轮 {step['tool']} {json.dumps(step['arguments'], ensure_ascii=False)} → {mark}（{step['ms']} 毫秒）")
    if not run.steps:
        print("- 无")
    print()
    print("## 回答")
    print(run.answer or f"（没有答案：{run.error or run.status}）")
    print()
    usage = run.usage
    print(f"状态：{run.status}；模型 {run.model}；调用模型 {usage['llm_calls']} 次，token {usage['prompt_tokens']} + "
          f"{usage['completion_tokens']}；用时 {run.seconds} 秒" + ("；工具次数已用完" if run.budget_exhausted else ""))
    print(f"运行记录：{run_dir}")


def _review(run_dir: Path) -> dict:
    """生成核查结果和报告，并打印核查摘要。"""
    from .report import write_review

    checks = write_review(run_dir)
    print(checks["summary"])
    for mention in checks["numbers"]["ungrounded"]:
        print(f"  找不到出处：{mention['text']}（{mention['context']}）")
    print(f"报告：{run_dir / 'report.md'}")
    return checks


def demo(llm_kind: str) -> int:
    """离线演示：在内置的两周小样例上，用假模型剧本走完整个 Agent 循环。不需要密钥和真实数据。"""
    if llm_kind != "fake":
        raise SystemExit("demo 只用假模型；真实模型请用 da-agent ask")
    from .agent import run_agent, save_run
    from .cleaning import connect_frame
    from .dataset import read_normalized_csv

    with connect_frame(read_normalized_csv(DEMO_DATA)) as con:
        run = run_agent(DEMO_QUESTION, llm=FakeLLM.from_file(DEMO_SCRIPT), con=con,
                        max_tool_calls=Settings().llm_max_tool_calls)
    run_dir = save_run(run, RUNS_DIR)
    _print_run(run, run_dir)
    _review(run_dir)
    return 0 if run.status == "completed" else 1


def ask(question: str, llm_kind: str, script: Path | None) -> int:
    """在真实数据上回答问题。默认调用真实模型；--llm fake 时按剧本回放（用于调试）。"""
    from .agent import run_agent, save_run
    from .cleaning import connect
    from .dataset import PARQUET_PATH
    from .llm import OpenAICompatibleLLM

    if not PARQUET_PATH.exists():
        raise SystemExit("还没有准备数据，请先运行：da-agent prepare-data")
    settings = Settings()
    if llm_kind == "fake":
        if script is None:
            raise SystemExit("--llm fake 需要用 --script 指定剧本文件")
        llm = FakeLLM.from_file(script)
    else:
        llm = OpenAICompatibleLLM(settings)
    with connect(PARQUET_PATH) as con:
        run = run_agent(question, llm=llm, con=con, max_tool_calls=settings.llm_max_tool_calls)
    run_dir = save_run(run, RUNS_DIR)
    _print_run(run, run_dir)
    _review(run_dir)
    return 0 if run.status == "completed" else 1


def check(run_dir: Path) -> int:
    """对已保存的运行补做核查。有找不到出处的数字时返回 1，方便在自动化流程里当关卡用。"""
    if not (run_dir / "run.json").exists():
        raise SystemExit(f"{run_dir} 下没有 run.json")
    checks = _review(run_dir)
    return 1 if checks["numbers"]["ungrounded"] else 0


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


def _fmt(value: float | int | None, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "money":
        return f"{value:,.2f}"
    if unit == "rate":
        return f"{value:.2%}"
    if unit == "count":
        return f"{value:,}"
    return f"{value:.4f}"


def week(week_label: str, compare: str) -> int:
    """输出某一周的确定性分析：指标对比、GMV 拆解、按国家和商品下钻、警告。"""
    from .cleaning import connect
    from .dataset import PARQUET_PATH
    from .decompose import decompose_gmv
    from .metrics import drilldown, metric_summary

    if not PARQUET_PATH.exists():
        raise SystemExit("还没有准备数据，请先运行：da-agent prepare-data")
    with connect(PARQUET_PATH) as con:
        summary = metric_summary(con, week_label, compare)  # 周写错、超出范围会在这里报错
        try:
            decomposition, refusal = decompose_gmv(con, week_label, compare), ""
        except ValueError as exc:  # 例如基期整周停业，没有客户可拆：工具拒绝计算，其余部分照常输出
            decomposition, refusal = None, str(exc)
        by_country = drilldown(con, week_label, compare, dimension="country")
        by_product = drilldown(con, week_label, compare, dimension="product")
    period = summary["period"]
    print(f"# {period['current']} 对比 {period['base']}（{period['compare_label']}）")
    print()
    print("## 指标")
    for row in summary["metrics"]:
        change = _fmt(row["change"], "rate" if row["unit"] == "rate" else row["unit"])
        if row["unit"] == "rate" and row["change"] is not None:
            change = f"{row['change'] * 100:+.2f} 个百分点"
        pct = "" if row["change_pct"] is None else f"（{row['change_pct']:+.2%}）"
        print(f"- {row['label']}：{_fmt(row['current'], row['unit'])}，基期 {_fmt(row['base'], row['unit'])}，变化 {change}{pct}")
    print()
    if decomposition is None:
        print("## GMV 拆解")
        print(f"- 无法拆解：{refusal}")
    else:
        print(f"## GMV 拆解（{decomposition['identity']}）")
        print(f"- 总变化：{decomposition['total_change']:,.2f}")
        for item in decomposition["contributions"]:
            low, high = item["range_across_orders"]
            print(f"- {item['label']}：默认顺序 {item['chain']:,.2f}；Shapley {item['shapley']:,.2f}；6 种顺序范围 [{low:,.2f}, {high:,.2f}]")
    for result in (by_country, by_product):
        print()
        print(f"## GMV 变化按{result['dimension_label']}下钻（前 {len(result['segments'])} 个，共 {result['segment_count']} 个分组）")
        for seg in result["segments"]:
            name = seg["segment"] + (f" {seg['description']}" if seg.get("description") else "")
            share = "—" if seg["share_of_total_change"] is None else f"{seg['share_of_total_change']:.1%}"
            flag = "（样本不足）" if seg["small_sample"] else ""
            print(f"- {name}：{seg['base']:,.2f} → {seg['current']:,.2f}，变化 {seg['change']:+,.2f}，占总变化 {share}{flag}")
    all_warnings = summary["warnings"] + (decomposition["warnings"] if decomposition else []) \
        + by_country["warnings"] + by_product["warnings"]
    warnings = list(dict.fromkeys(all_warnings))  # 去重并保持顺序
    print()
    print("## 警告")
    for item in warnings or ["无"]:
        print(f"- {item}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """解析子命令并执行。"""
    parser = argparse.ArgumentParser(prog="da-agent", description="运营周报与指标异动分析 Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_parser = commands.add_parser("doctor", help="检查配置；加 --ping 发 1 次真实请求测试连通性")
    doctor_parser.add_argument("--ping", action="store_true", help="发 1 次真实请求，确认地址、模型和密钥可用")
    demo_parser = commands.add_parser("demo", help="离线演示：假模型剧本 + 内置小样例，走完整个 Agent 循环")
    demo_parser.add_argument("--llm", choices=["fake"], default="fake", help="演示只用假模型")
    ask_parser = commands.add_parser("ask", help="用 Agent 回答一个数据问题，运行记录存到 runs/")
    ask_parser.add_argument("question", help="例如：上周 GMV 为什么下降？")
    ask_parser.add_argument("--llm", choices=["real", "fake"], default="real", help="real 调用真实模型（默认）；fake 按剧本回放")
    ask_parser.add_argument("--script", type=Path, help="--llm fake 时使用的剧本文件")
    check_parser = commands.add_parser("check", help="对已保存的运行补做核查并重新生成报告，不调用模型")
    check_parser.add_argument("run_dir", type=Path, help="运行目录，例如 runs/20260929T144747-a7db30")
    prepare_parser = commands.add_parser("prepare-data", help="下载并转换 UCI Online Retail II 数据集")
    prepare_parser.add_argument("--force", action="store_true", help="即使已有 parquet 也重新转换")
    commands.add_parser("quality", help="生成数据质量报告")
    week_parser = commands.add_parser("week", help="不经过模型，输出某一周的指标、GMV 拆解和下钻")
    week_parser.add_argument("week", help="ISO 周，例如 2011-W48")
    week_parser.add_argument("--compare", choices=["wow", "yoy"], default="wow", help="wow 环比（默认），yoy 同比")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # OpenAI SDK 的网络库每次请求都打一行日志，太吵
    try:
        if args.command == "doctor":
            return doctor(Settings(), ping_model=args.ping)
        if args.command == "prepare-data":
            return prepare_data(args.force)
        if args.command == "quality":
            return quality()
        if args.command == "week":
            return week(args.week, args.compare)
        if args.command == "ask":
            return ask(args.question, args.llm, args.script)
        if args.command == "check":
            return check(args.run_dir)
        return demo(args.llm)
    except (ValueError, MissingApiKeyError) as exc:  # 输入不合法或缺少密钥：给出原因，不打印报错堆栈
        print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
