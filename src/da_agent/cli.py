"""命令行入口。

- `da-agent doctor`：检查配置，不调用模型
- `da-agent demo --llm fake`：用假模型跑示例
- `da-agent prepare-data`：下载并转换数据集
- `da-agent quality`：生成数据质量报告（reports/data_quality.md 和 .json）
- `da-agent week 2011-W48`：不经过模型，直接用分析工具输出某一周的指标、拆解和下钻

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
    commands.add_parser("doctor", help="检查配置，不调用模型")
    demo_parser = commands.add_parser("demo", help="运行示例")
    demo_parser.add_argument("--llm", choices=["fake"], default="fake", help="使用哪种模型；M0 只有 fake")
    prepare_parser = commands.add_parser("prepare-data", help="下载并转换 UCI Online Retail II 数据集")
    prepare_parser.add_argument("--force", action="store_true", help="即使已有 parquet 也重新转换")
    commands.add_parser("quality", help="生成数据质量报告")
    week_parser = commands.add_parser("week", help="不经过模型，输出某一周的指标、GMV 拆解和下钻")
    week_parser.add_argument("week", help="ISO 周，例如 2011-W48")
    week_parser.add_argument("--compare", choices=["wow", "yoy"], default="wow", help="wow 环比（默认），yoy 同比")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "doctor":
            return doctor(Settings())
        if args.command == "prepare-data":
            return prepare_data(args.force)
        if args.command == "quality":
            return quality()
        if args.command == "week":
            return week(args.week, args.compare)
        return demo(args.llm)
    except ValueError as exc:  # 输入不合法（如周写错）：给出原因，不打印报错堆栈
        print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
