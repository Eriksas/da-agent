"""命令行入口。

- `da-agent doctor [--ping]`：检查配置；加 --ping 发 1 次真实请求测试连通性
- `da-agent demo`：离线演示，用假模型剧本在内置小样例上走完整个 Agent 循环
- `da-agent prepare-data`：下载并转换数据集
- `da-agent quality`：生成数据质量报告（reports/data_quality.md 和 .json）
- `da-agent week 2011-W48`：不经过模型，直接用分析工具输出某一周的指标、拆解和下钻
- `da-agent ask "问题"`：用真实模型（或 --llm fake 加剧本）回答问题，运行记录、核查结果和报告存到 runs/
- `da-agent fetch-financials`：下载 SEC 财报数据，生成上市公司关键科目表
- `da-agent company-ask "问题"`：上市公司财报分析，用法同 ask
- `da-agent check runs/<运行编号>`：对已保存的运行补做核查，重新生成报告，不调用模型
- `da-agent eval`：评测，同一批题目在多个对比组各跑一遍，程序打分，结果写到 eval/results/
- `da-agent eval-compare 改进前目录 改进后目录`：对比两次评测，不调用模型
- `da-agent weekly --advance`：按回放游标生成下一周的周报（GitHub Actions 每周运行），写到 reports/weekly/

过程日志用 logging；命令行给用户看的结果用 print。
"""

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .config import MissingApiKeyError, MissingSecUserAgentError, Settings
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
    print(f"SEC 联系方式：{'已配置' if settings.sec_user_agent else '未配置（下载财报数据时需要）'}")
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
    print(checks["process"]["summary"])
    for signal in checks["danger_signals"]:
        print(f"  危险信号：{signal}")
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


def ask(question: str, llm_kind: str, script: Path | None, domain: str = "ecommerce") -> int:
    """在真实数据上回答问题。默认调用真实模型；--llm fake 时按剧本回放（用于调试）。

    domain：ecommerce 用电商交易数据（ask）；company 用上市公司关键科目表（company-ask）。
    """
    from .agent import run_agent, save_run
    from .cleaning import connect
    from .company import connect_financials
    from .dataset import PARQUET_PATH
    from .llm import OpenAICompatibleLLM

    if domain == "ecommerce" and not PARQUET_PATH.exists():
        raise SystemExit("还没有准备数据，请先运行：da-agent prepare-data")
    settings = Settings()
    if llm_kind == "fake":
        if script is None:
            raise SystemExit("--llm fake 需要用 --script 指定剧本文件")
        llm = FakeLLM.from_file(script)
    else:
        llm = OpenAICompatibleLLM(settings)
    with (connect_financials() if domain == "company" else connect(PARQUET_PATH)) as con:
        run = run_agent(question, llm=llm, con=con, max_tool_calls=settings.llm_max_tool_calls, domain=domain)
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


def evaluate(llm_kind: str, case_ids: str | None, conditions: str, repeats: int, name: str | None,
             case_file: str = "eval/cases.yaml") -> int:
    """运行评测并写出结果表。--llm fake 只用来检查流程是否跑得通，结果不代表模型表现。"""
    import json
    from datetime import datetime

    from .dataset import PARQUET_PATH
    from .evaluation import Connections, RESULTS_DIR, load_cases, render_summary, run_eval
    from .llm import LLMReply, OpenAICompatibleLLM
    from .paths import ROOT

    if not (ROOT / case_file).exists():
        raise ValueError(f"题集文件不存在：{case_file}")
    cases = load_cases(ROOT / case_file)
    if any(case.domain == "ecommerce" for case in cases) and not PARQUET_PATH.exists():
        raise SystemExit("还没有准备数据，请先运行：da-agent prepare-data")
    if case_ids:
        wanted = [i.strip() for i in case_ids.split(",") if i.strip()]
        unknown = sorted(set(wanted) - {case.id for case in cases})
        if unknown:
            raise ValueError(f"未知题目：{unknown}")
        cases = [case for case in cases if case.id in wanted]
    settings = Settings()
    if llm_kind == "fake":
        model = "fake"

        def make_llm(*_: object) -> FakeLLM:
            return FakeLLM(replies=[LLMReply(content="（假模型）这是检查评测流程用的固定回答，不代表任何真实结果。")])
    else:
        client = OpenAICompatibleLLM(settings)
        model = client.model

        def make_llm(*_: object) -> OpenAICompatibleLLM:
            return client
    name = name or f"{datetime.now():%Y%m%d-%H%M%S}-{llm_kind}"
    out_dir = RESULTS_DIR / name
    if out_dir.exists():
        raise SystemExit(f"{out_dir} 已存在，请换一个 --name")
    summary = run_eval(cases, [c.strip() for c in conditions.split(",")], repeats, make_llm, Connections(),
                       out_dir, settings.llm_max_tool_calls)
    meta = {"name": name, "model": model, "repeats": repeats, "cases_file": case_file,
            "finished_at": f"{datetime.now():%Y-%m-%d %H:%M}"}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_summary(summary, meta)
    (out_dir / "summary.md").write_text(markdown, encoding="utf-8")
    print()
    print(markdown.split("## 未通过的运行")[0].strip())
    print(f"完整结果：{out_dir / 'summary.md'}")
    return 0


def rescore_eval(out_dir: Path) -> int:
    """用当前规则给已保存的评测重新打分（不调用模型）；第一次重评时保留原结果表为 summary.original.md。"""
    import json
    import shutil
    from datetime import datetime

    from .evaluation import Connections, load_cases, render_summary, rescore
    from .paths import ROOT

    meta_path = out_dir / "meta.json"
    if not meta_path.exists():
        raise SystemExit(f"{out_dir} 下没有 meta.json，不是评测结果目录")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    original = out_dir / "summary.original.md"
    if (out_dir / "summary.md").exists() and not original.exists():
        shutil.copy(out_dir / "summary.md", original)
    # 用当时的题集重评；M7b 之前的结果没有记录题集文件，都来自 eval/cases.yaml
    summary = rescore(out_dir, load_cases(ROOT / meta.get("cases_file", "eval/cases.yaml")), Connections())
    meta["rescored_at"] = f"{datetime.now():%Y-%m-%d %H:%M}"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_summary(summary, meta)
    (out_dir / "summary.md").write_text(markdown, encoding="utf-8")
    print(markdown.split("## 未通过的运行")[0].strip())
    print(f"完整结果：{out_dir / 'summary.md'}（重评前的结果：{original.name}）")
    return 0


def compare_eval(before_dir: Path, after_dir: Path) -> int:
    """打印两次评测的对比表（读取两边的 summary.json，不调用模型）。"""
    import json

    from .evaluation import render_comparison

    summaries = []
    for out_dir in (before_dir, after_dir):
        path = out_dir / "summary.json"
        if not path.exists():
            raise SystemExit(f"{out_dir} 下没有 summary.json，不是评测结果目录")
        summaries.append(json.loads(path.read_text(encoding="utf-8")))
    print(f"改进前：{before_dir.as_posix()}；改进后：{after_dir.as_posix()}")
    print()
    print(render_comparison(*summaries))
    return 0


def weekly_report(week: str | None, llm_kind: str, advance: bool) -> int:
    """生成一周的周报。默认分析游标指向的周；--advance 在成功后把游标推进一周。"""
    import os

    from .cleaning import connect
    from .dataset import PARQUET_PATH
    from .llm import LLMReply, OpenAICompatibleLLM
    from .weekly import WEEKLY_DIR, generate_weekly, next_replay_week, read_cursor, write_cursor

    if llm_kind == "fake" and advance:
        raise ValueError("假模型生成的周报只用来检查流程，不能推进回放游标")
    if not PARQUET_PATH.exists():
        raise SystemExit("还没有准备数据，请先运行：da-agent prepare-data")
    target = week or read_cursor()
    if target is None:
        print("回放已结束：数据中没有更多的周。")
        return 0
    settings = Settings()
    if llm_kind == "real":
        llm, out_dir = OpenAICompatibleLLM(settings), WEEKLY_DIR
    else:  # 假模型只检查流程：输出写到不提交的 runs/ 下，不污染正式周报目录
        llm = FakeLLM(replies=[LLMReply(content="（假模型）检查周报流程用的固定回答，不代表任何真实分析。")])
        out_dir = RUNS_DIR / "weekly-fake"
    with connect(PARQUET_PATH) as con:
        result = generate_weekly(con, target, llm, settings.llm_max_tool_calls, out_dir)
        following = next_replay_week(con, target)
    if advance:
        write_cursor(following)
    print(f"周报：{result['report']}")
    print(f"AI 解读：{result['ai_status']}；数字有出处 {result['numbers_grounded']}/{result['numbers_checked']}")
    if advance:
        print(f"游标已推进到：{following or '（回放结束）'}")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:  # 在 GitHub Actions 里，把周次交给后续的提交步骤
        with open(github_output, "a", encoding="utf-8") as fh:
            print(f"week={target}", file=fh)
    return 0


def build_site(out_dir: Path) -> int:
    """生成项目展示网站（静态网页，GitHub Pages 托管）。不调用模型。"""
    from .site import build_site as build

    files = build(out_dir)
    print(f"已生成 {len(files)} 个页面：{out_dir / 'index.html'}")
    return 0


def fetch_financials(refresh: bool) -> int:
    """下载 SEC 财报数据并生成关键科目表 data/financials/key_facts.csv。"""
    from .financials import KEY_FACTS_PATH, build_key_facts, fetch_all

    fetch_all(Settings().require_sec_user_agent(), refresh=refresh)
    frame = build_key_facts()
    summary = frame.groupby("company")["fiscal_year"].agg(["min", "max", "count"])
    for company, row in summary.iterrows():
        print(f"{company}：{row['min']}–{row['max']} 财年，{row['count']} 个数字")
    print(f"关键科目表：{KEY_FACTS_PATH}")
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


def week(week_label: str, compare: str) -> int:
    """输出某一周的确定性分析：指标对比、GMV 拆解、按国家和商品下钻、警告。"""
    from .cleaning import connect
    from .dataset import PARQUET_PATH
    from .decompose import decompose_gmv
    from .metrics import drilldown, format_value as _fmt, metric_summary

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
    company_parser = commands.add_parser("company-ask", help="上市公司财报分析：用 Agent 回答一个问题，运行记录存到 runs/")
    company_parser.add_argument("question", help="例如：阿里巴巴最近一个财年的盈利能力怎么样？")
    company_parser.add_argument("--llm", choices=["real", "fake"], default="real", help="real 调用真实模型（默认）；fake 按剧本回放")
    company_parser.add_argument("--script", type=Path, help="--llm fake 时使用的剧本文件")
    weekly_parser = commands.add_parser("weekly", help="生成一周的周报（默认按回放游标）")
    weekly_parser.add_argument("--week", help="指定周，例如 2010-W02；不填则用回放游标")
    weekly_parser.add_argument("--llm", choices=["real", "fake"], default="real", help="real 真实模型；fake 只检查流程")
    weekly_parser.add_argument("--advance", action="store_true", help="生成成功后把回放游标推进一周")
    eval_parser = commands.add_parser("eval", help="评测：同一批题目在多个对比组各跑一遍，程序打分并汇总")
    eval_parser.add_argument("--llm", choices=["real", "fake"], default="real", help="real 真实模型；fake 只检查流程")
    eval_parser.add_argument("--case-file", default="eval/cases.yaml", help="题集文件（相对仓库根目录），例如 eval/holdout.yaml")
    eval_parser.add_argument("--cases", help="只跑这些题目，逗号分隔，例如 w48-why,w49-trap")
    eval_parser.add_argument("--conditions", default="baseline,agent,agent_skill", help="对比组，逗号分隔")
    eval_parser.add_argument("--repeats", type=int, default=2, help="Agent 组每题重复次数（直接问模型组固定 1 次）")
    eval_parser.add_argument("--name", help="结果目录名，默认用时间")
    rescore_parser = commands.add_parser("eval-rescore", help="用当前规则给已保存的评测重新打分，不调用模型")
    rescore_parser.add_argument("out_dir", type=Path, help="评测结果目录，例如 eval/results/trial-3cases")
    compare_parser = commands.add_parser("eval-compare", help="对比两次评测（改进前、改进后），不调用模型")
    compare_parser.add_argument("before_dir", type=Path, help="改进前的评测结果目录")
    compare_parser.add_argument("after_dir", type=Path, help="改进后的评测结果目录")
    check_parser = commands.add_parser("check", help="对已保存的运行补做核查并重新生成报告，不调用模型")
    check_parser.add_argument("run_dir", type=Path, help="运行目录，例如 runs/20260929T144747-a7db30")
    prepare_parser = commands.add_parser("prepare-data", help="下载并转换 UCI Online Retail II 数据集")
    prepare_parser.add_argument("--force", action="store_true", help="即使已有 parquet 也重新转换")
    commands.add_parser("quality", help="生成数据质量报告")
    site_parser = commands.add_parser("build-site", help="生成项目展示网站（静态网页，用于 GitHub Pages）")
    site_parser.add_argument("--out", type=Path, default=Path("_site"), help="输出目录，默认 _site")
    fin_parser = commands.add_parser("fetch-financials", help="下载 SEC 财报数据，生成关键科目表（需要 SEC_USER_AGENT）")
    fin_parser.add_argument("--refresh", action="store_true", help="重新下载，即使本地已有原始数据")
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
        if args.command == "fetch-financials":
            return fetch_financials(args.refresh)
        if args.command == "build-site":
            return build_site(args.out)
        if args.command == "week":
            return week(args.week, args.compare)
        if args.command == "ask":
            return ask(args.question, args.llm, args.script)
        if args.command == "company-ask":
            return ask(args.question, args.llm, args.script, domain="company")
        if args.command == "check":
            return check(args.run_dir)
        if args.command == "weekly":
            return weekly_report(args.week, args.llm, args.advance)
        if args.command == "eval-rescore":
            return rescore_eval(args.out_dir)
        if args.command == "eval-compare":
            return compare_eval(args.before_dir, args.after_dir)
        if args.command == "eval":
            return evaluate(args.llm, args.cases, args.conditions, args.repeats, args.name, args.case_file)
        return demo(args.llm)
    except (ValueError, MissingApiKeyError, MissingSecUserAgentError) as exc:  # 输入不合法或缺少配置：给出原因，不打印堆栈
        print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
