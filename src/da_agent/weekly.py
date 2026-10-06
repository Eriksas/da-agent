"""自动周报：按“回放游标”逐周生成运营周报，由 GitHub Actions 每周运行一次。

- 回放：公开数据是 2009–2011 年的历史记录。state/replay_cursor.json 记录下一次要分析哪一周，
  每生成一份周报就往前推一周，到数据的最后一周后停止。这是模拟上线，不是实时业务数据。
- 周报分两部分：第一部分由程序直接计算（指标、拆解、下钻、警告），不经过模型；
  第二部分是 AI 解读，附自动核查结果，标注“待人工复核”。AI 出错时第一部分照常发布。
"""

import json
import re
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb

from .agent import run_agent
from .checks import check_run
from .decompose import IDENTITY, decompose_gmv
from .llm import LLMClient
from .metrics import data_range, drilldown, format_value, metric_summary
from .paths import REPORTS_DIR, ROOT
from .periods import week_monday, week_of

CURSOR_PATH = ROOT / "state" / "replay_cursor.json"
WEEKLY_DIR = REPORTS_DIR / "weekly"
FIRST_REPLAY_WEEK = "2010-W02"  # 2009 年底圣诞停业之后的第一个完整周
QUESTION = "请为 {week} 写一份运营周报：和上一周相比，GMV、订单和客户发生了什么变化，主要原因是什么，需要注意什么。"


def read_cursor(path: Path = CURSOR_PATH) -> str | None:
    """下一次要分析的周。文件不存在时从第一周开始；回放结束后为 None。"""
    if not path.exists():
        return FIRST_REPLAY_WEEK
    return json.loads(path.read_text(encoding="utf-8"))["next_week"]


def write_cursor(next_week: str | None, path: Path = CURSOR_PATH) -> None:
    """更新游标。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    note = "回放已结束：数据中没有更多的周" if next_week is None else "下一次自动周报要分析的周（历史数据回放，不是实时数据）"
    path.write_text(json.dumps({"next_week": next_week, "note": note}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def next_replay_week(con: duckdb.DuckDBPyConnection, week: str) -> str | None:
    """下一周；已经是数据的最后一周时返回 None。"""
    following = week_monday(week) + timedelta(days=7)
    return None if following > week_monday(data_range(con)["last_week"]) else week_of(following)


def _change(row: dict[str, Any]) -> str:
    """指标变化的写法：金额和计数带正负号和变化百分比，比率只写百分点。"""
    if row["change"] is None:
        return "—"
    if row["unit"] == "rate":
        return f"{row['change'] * 100:+.2f} 个百分点"
    text = ("+" if row["change"] > 0 else "") + format_value(row["change"], row["unit"])
    return text + (f"（{row['change_pct']:+.2%}）" if row["change_pct"] is not None else "")


def facts_section(con: duckdb.DuckDBPyConnection, week: str) -> tuple[list[str], dict[str, Any]]:
    """第一部分：程序直接计算的内容。返回 Markdown 行和写进索引的摘要。"""
    summary = metric_summary(con, week)
    lines = ["## 一、核心指标（程序计算）", "", f"对比 {summary['period']['base']}（上一周）。", "",
             "| 指标 | 本周 | 上周 | 变化 |", "|---|---:|---:|---:|"]
    lines += [f"| {r['label']} | {format_value(r['current'], r['unit'])} | {format_value(r['base'], r['unit'])} | {_change(r)} |"
              for r in summary["metrics"]]
    warnings = list(summary["warnings"])
    lines += ["", f"### GMV 拆解（{IDENTITY}）", ""]
    try:
        decomposition = decompose_gmv(con, week)
    except ValueError as exc:  # 例如上一周整周停业：拆解没有意义，工具拒绝计算
        lines.append(f"- 无法拆解：{exc}")
    else:
        lines += ["| 因子 | 贡献（Shapley） | 占总变化 |", "|---|---:|---:|"]
        for item in decomposition["contributions"]:
            share = "—" if item["share_of_total_change"] is None else f"{item['share_of_total_change']:.1%}"
            lines.append(f"| {item['label']} | {item['shapley']:+,.2f} | {share} |")
        warnings += decomposition["warnings"]
    for dimension in ("country", "customer_type"):
        result = drilldown(con, week, dimension=dimension, top_n=3)
        lines += ["", f"### GMV 变化最大的{result['dimension_label']}（前 3）", "",
                  "| 分组 | 上周 | 本周 | 变化 | 订单（上周 → 本周） |", "|---|---:|---:|---:|---:|"]
        for seg in result["segments"]:
            flag = "（样本不足）" if seg["small_sample"] else ""
            lines.append(f"| {seg['segment']}{flag} | {seg['base']:,.2f} | {seg['current']:,.2f} | {seg['change']:+,.2f} | "
                         f"{seg['base_orders']} → {seg['current_orders']} |")
        if result["others"]:
            other = result["others"]
            lines.append(f"| 前 3 名以外（{other['segments']} 个分组） | {other['base']:,.2f} | {other['current']:,.2f} | "
                         f"{other['change']:+,.2f} | — |")
        warnings += result["warnings"]
    lines += ["", "### 警告", ""] + ([f"- {w}" for w in dict.fromkeys(warnings)] or ["- 无"])
    gmv = next(r for r in summary["metrics"] if r["metric"] == "gmv")
    return lines, {"base": summary["period"]["base"], "gmv_change_pct": gmv["change_pct"]}


def demote_headings(markdown: str, levels: int = 2) -> str:
    """AI 回答里的标题降两级再放进周报：周报的“二、AI 解读”是二级标题，AI 的一级标题放在它下面，目录会乱。"""
    return re.sub(r"(?m)^(#{1,6})(?=[ \t])", lambda m: "#" * min(len(m.group(1)) + levels, 6), markdown)


def ai_section(run: dict[str, Any], checks: dict[str, Any]) -> list[str]:
    """第二部分：AI 解读，开头先放自动核查结果。"""
    lines = ["## 二、AI 解读（初稿，待人工复核）", "",
             f"> 自动核查：{checks['summary']}。{checks['process']['summary']}。"]
    repair = run.get("repair")
    if repair:
        lines.append(f"> 核查后退回修正 1 次：初稿有 {len(repair['ungrounded'])} 个数字找不到出处（"
                     + "、".join(m["text"] for m in repair["ungrounded"])
                     + ("），修正失败，下面仍是初稿。" if repair.get("error") else "），下面是修正后的版本。"))
    lines += [f"> ⚠️ {signal}" for signal in checks["danger_signals"]]
    if checks["numbers"]["ungrounded"]:
        lines.append("> 找不到出处的数字：" + "、".join(m["text"] for m in checks["numbers"]["ungrounded"]))
    lines.append("")
    if run["status"] == "completed":
        lines.append(demote_headings(run["answer"]))
    else:
        lines.append(f"（AI 解读未完成：{run['error'] or run['status']}。第一部分的指标由程序计算，不受影响。）")
    return lines


def write_index(out_dir: Path) -> Path:
    """周报目录的索引：每周一行，按周排序。"""
    rows = sorted((json.loads(p.read_text(encoding="utf-8")) for p in out_dir.glob("runs/*/summary.json")),
                  key=lambda r: week_monday(r["week"]))
    lines = ["# 自动周报", "",
             "由 GitHub Actions 每周自动生成（UCI Online Retail II 历史数据回放）。第一部分由程序计算；第二部分是 AI 初稿，待人工复核。", "",
             "| 周 | GMV 环比 | AI 解读 | 数字有出处 |", "|---|---:|---|---:|"]
    for r in rows:
        pct = "—" if r["gmv_change_pct"] is None else f"{r['gmv_change_pct']:+.2%}"
        ai = "完成" if r["ai_status"] == "completed" else f"未完成（{r['ai_status']}）"
        lines.append(f"| [{r['week']}]({r['week']}.md) | {pct} | {ai} | {r['numbers_grounded']}/{r['numbers_checked']} |")
    path = out_dir / "README.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def generate_weekly(con: duckdb.DuckDBPyConnection, week: str, llm: LLMClient, max_tool_calls: int,
                    out_dir: Path = WEEKLY_DIR) -> dict[str, Any]:
    """生成一周的周报：程序计算部分 + AI 解读 + 核查，写出报告、运行记录和索引。"""
    facts, meta = facts_section(con, week)
    run = asdict(run_agent(QUESTION.format(week=week), llm=llm, con=con, max_tool_calls=max_tool_calls))
    checks = check_run(run)
    run_dir = out_dir / "runs" / week
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (("run.json", run), ("checks.json", checks)):
        (run_dir / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, default=str) + "\n",
                                    encoding="utf-8", newline="\n")
    summary = {"week": week, **meta, "ai_status": run["status"], "model": run["model"],
               "numbers_checked": checks["numbers"]["checked"], "numbers_grounded": checks["numbers"]["grounded"]}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8", newline="\n")
    usage = run["usage"]
    lines = [f"# 运营周报：{week}", "",
             "> 由 GitHub Actions 自动生成。数据为 UCI Online Retail II 的历史回放（2009–2011），不是实时业务数据。",
             "> 第一部分由程序直接计算；第二部分是 AI 初稿，已做自动核查，**待人工复核**。", "",
             *facts, "", *ai_section(run, checks), "",
             "## 三、运行信息", "",
             f"- 模型 {run['model']}；调用模型 {usage['llm_calls']} 次，token {usage['prompt_tokens']} + "
             f"{usage['completion_tokens']}，用时 {run['seconds']} 秒",
             f"- 完整运行记录：[runs/{week}/run.json](runs/{week}/run.json)", ""]
    report = out_dir / f"{week}.md"
    report.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    write_index(out_dir)
    return {**summary, "report": str(report)}
