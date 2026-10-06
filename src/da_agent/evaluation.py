"""评测：同一批题目，三种做法各跑一遍，由程序打分，汇总成结果表。

三个对比组：
- baseline（直接问模型）：只给一张周度指标表，不给工具。代表“把报表贴给大模型问”的常见用法。
- agent（Agent 不带流程）：有工具，没有分析流程。
- agent_skill（Agent 带流程）：有工具，也有分析流程，是当前的默认做法。

每次运行的完整记录都保存下来，结果表里的任何一个数都能回溯到原始对话。
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import pandas as pd
import yaml

from .agent import AgentRun, run_agent
from .checks import check_run, extract_numbers
from .cleaning import connect, connect_frame, fetch_rows
from .dataset import PARQUET_PATH
from .llm import LLMClient, LLMError
from .metrics import data_range, drilldown, weekly_row
from .paths import ROOT
from .periods import week_monday
from .report import render_report

CASES_PATH = ROOT / "eval" / "cases.yaml"
HOLDOUT_PATH = ROOT / "eval" / "holdout.yaml"  # 留出题：改进之前冻结，用来检验改进能不能推广
RESULTS_DIR = ROOT / "eval" / "results"
CONDITIONS = {
    "baseline": "直接问模型（只给周度指标表，无工具）",
    "agent": "Agent 不带流程",
    "agent_skill": "Agent 带流程",
}
BASELINE_PROMPT = """你是一名电商运营数据分析助手。下面是程序导出的周度指标表（ISO 周，周一到周日），数据范围 {first_week} 至 {last_week}。
请只根据这张表和用户提供的信息回答问题，用中文 Markdown 写出结论、主要原因、需要注意的地方和建议下一步。

{table}
"""


@dataclass(frozen=True)
class Case:
    """一道评测题及其评分规则，含义见 eval/cases.yaml 开头的说明。"""

    id: str
    category: str
    question: str
    must_mention: tuple[tuple[str, ...], ...] = ()
    must_not: tuple[str, ...] = ()
    expect_value: dict[str, str] | None = None
    inject: dict[str, str] | None = None
    expects_skill: bool = True


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    """读取题集，并检查规则本身是否有效（ID 不重复、正则能编译）。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [Case(id=item["id"], category=item["category"], question=item["question"],
                  must_mention=tuple(tuple(group) for group in item.get("must_mention") or []),
                  must_not=tuple(item.get("must_not") or []),
                  expect_value=item.get("expect_value"), inject=item.get("inject"),
                  expects_skill=bool(item.get("expects_skill", True)))
             for item in data["cases"]]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"题目 ID 重复：{sorted({i for i in ids if ids.count(i) > 1})}")
    for case in cases:
        for pattern in case.must_not:
            re.compile(pattern)
    return cases


def inject(frame: pd.DataFrame, spec: dict[str, str]) -> pd.DataFrame:
    """在数据副本上注入已知异动。目前支持：删除某国某周的全部记录（销售和取消）。"""
    if set(spec) != {"drop_country", "week"}:
        raise ValueError(f"不支持的注入方式：{spec}")
    start = pd.Timestamp(week_monday(spec["week"]))
    mask = ((frame["country"] == spec["drop_country"])
            & (frame["invoice_date"] >= start) & (frame["invoice_date"] < start + timedelta(days=7)))
    if not mask.any():
        raise ValueError(f"注入没有命中任何记录：{spec}")
    return frame.loc[~mask].reset_index(drop=True)


def verify_injection(con: duckdb.DuckDBPyConnection, spec: dict[str, str]) -> None:
    """确认注入后，目标国家确实是这一周 GMV 变化的最大拖累项；否则标准答案不成立，停止评测。"""
    top = drilldown(con, spec["week"], dimension="country", top_n=1)["segments"][0]
    if top["segment"] != spec["drop_country"] or top["change"] >= 0:
        raise ValueError(f"注入后最大变化来自 {top['segment']}（{top['change']}），不是 {spec['drop_country']}，标准答案不成立")


def weekly_table(con: duckdb.DuckDBPyConnection) -> str:
    """直接问模型组拿到的输入：每周一行的核心指标表。"""
    weeks = [row["week"] for row in fetch_rows(con, "SELECT week FROM weekly ORDER BY monday")]
    lines = ["| 周 | 交易天数 | GMV | 净销售额 | 订单数 | 客单价 | 活跃客户数 | 新客数 | 取消金额 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for week in weeks:
        r = weekly_row(con, week)
        aov = "—" if r["aov"] is None else f"{r['aov']:.2f}"
        lines.append(f"| {week} | {r['trading_days']} | {r['gmv']:.2f} | {r['net_sales']:.2f} | {r['orders']} | {aov} | "
                     f"{r['active_customers']} | {r['new_customers']} | {r['cancelled_amount']:.2f} |")
    return "\n".join(lines)


def run_baseline(question: str, *, llm: LLMClient, con: duckdb.DuckDBPyConnection) -> AgentRun:
    """直接问模型：一次调用，不给工具。记录格式和 Agent 运行一致，方便用同一套核查和打分。"""
    start = time.perf_counter()
    run = AgentRun(run_id=f"{datetime.now():%Y%m%dT%H%M%S}-{uuid4().hex[:6]}", question=question, model=llm.model,
                   max_tool_calls=0, use_skills=False, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    bounds = data_range(con)
    run.messages = [{"role": "system", "content": BASELINE_PROMPT.format(
                        first_week=bounds["first_week"], last_week=bounds["last_week"], table=weekly_table(con))},
                    {"role": "user", "content": question}]
    try:
        reply = llm.chat(run.messages, tools=None)
    except LLMError as exc:
        run.status, run.error = "llm_error", str(exc)
    else:
        run.usage["llm_calls"] = 1
        for key in ("prompt_tokens", "completion_tokens"):
            run.usage[key] += (reply.usage or {}).get(key, 0)
        run.messages.append(reply.as_message())
        run.answer = reply.content
        run.status = "completed" if reply.content else "empty_answer"
    run.seconds = round(time.perf_counter() - start, 2)
    return run


def expected_value(con: duckdb.DuckDBPyConnection, case: Case) -> float | None:
    """题目要求答案里出现的正确数值，由工具现算。"""
    if not case.expect_value:
        return None
    return float(weekly_row(con, case.expect_value["week"])[case.expect_value["metric"]])


def score(case: Case, run: dict[str, Any], checks: dict[str, Any], expected: float | None) -> dict[str, Any]:
    """按题目的规则给一次运行打分。"""
    answer = run.get("answer") or ""
    lowered = answer.lower()
    mentions = [{"any_of": list(group), "hit": any(alt.lower() in lowered for alt in group)}
                for group in case.must_mention]
    violations = [pattern for pattern in case.must_not if re.search(pattern, answer)]
    value_found = None
    if expected is not None:
        value_found = any(abs(m.value - expected) < m.tolerance - 1e-12 for m in extract_numbers(answer)[0])
    numbers = checks["numbers"]
    skills = checks["process"]["skills"]
    passed = (run["status"] == "completed" and all(m["hit"] for m in mentions) and not violations
              and not numbers["ungrounded"] and value_found is not False)
    return {
        "passed": passed, "status": run["status"], "mentions": mentions, "violations": violations,
        "expected_value": expected, "value_found": value_found,
        "numbers_checked": numbers["checked"], "numbers_ungrounded": len(numbers["ungrounded"]),
        "skill_expected": case.expects_skill, "skill_loaded": bool(skills),
        # 只对应该使用流程的题统计“流程是否完成”；查数这类题按流程文件的要求本来就不加载流程
        "process_complete": (bool(skills) and not any(s["missing"] for s in skills)) if case.expects_skill else None,
        "tool_calls": len(run["steps"]), "llm_calls": run["usage"]["llm_calls"],
        "tokens": run["usage"]["prompt_tokens"] + run["usage"]["completion_tokens"], "seconds": run["seconds"],
    }


def _save(run_dir: Path, run: dict[str, Any], checks: dict[str, Any], result: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    files = {"run.json": run, "checks.json": checks, "score.json": result}
    for name, content in files.items():
        (run_dir / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, default=str) + "\n",
                                    encoding="utf-8", newline="\n")
    (run_dir / "report.md").write_text(render_report(run, checks), encoding="utf-8", newline="\n")


class Connections:
    """每种数据只建一次库：真实数据一个，每个注入场景一个。"""

    def __init__(self, parquet_path: Path = PARQUET_PATH) -> None:
        self.parquet_path = parquet_path
        self._cache: dict[str, duckdb.DuckDBPyConnection] = {}

    def get(self, case: Case) -> duckdb.DuckDBPyConnection:
        key = json.dumps(case.inject, sort_keys=True) if case.inject else "real"
        if key not in self._cache:
            if case.inject:
                frame = duckdb.sql(f"SELECT * FROM read_parquet('{self.parquet_path.as_posix()}')").df()
                con = connect_frame(inject(frame, case.inject))
                verify_injection(con, case.inject)
            else:
                con = connect(self.parquet_path)
            self._cache[key] = con
        return self._cache[key]


def run_eval(cases: list[Case], conditions: list[str], repeats: int,
             make_llm: Callable[[Case, str, int], LLMClient], connections: Connections, out_dir: Path,
             max_tool_calls: int, progress: Callable[[str], None] = print) -> dict[str, Any]:
    """逐题、逐组、逐次运行并打分；结果和每次运行的完整记录写到 out_dir。"""
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown:
        raise ValueError(f"未知对比组：{unknown}；可选：{list(CONDITIONS)}")
    plan = [(case, condition, i) for case in cases for condition in conditions
            for i in range(1, (1 if condition == "baseline" else repeats) + 1)]
    results = []
    for n, (case, condition, i) in enumerate(plan, start=1):
        con = connections.get(case)
        llm = make_llm(case, condition, i)
        if condition == "baseline":
            run = run_baseline(case.question, llm=llm, con=con)
        else:
            run = run_agent(case.question, llm=llm, con=con, max_tool_calls=max_tool_calls,
                            use_skills=condition == "agent_skill")
        run_dict = asdict(run)
        checks = check_run(run_dict)
        result = score(case, run_dict, checks, expected_value(con, case))
        _save(out_dir / "runs" / case.id / f"{condition}-{i}", run_dict, checks, result)
        results.append({"case": case.id, "category": case.category, "condition": condition, "repeat": i, **result})
        progress(f"[{n}/{len(plan)}] {case.id} {condition} #{i} → {'通过' if result['passed'] else '未通过'}"
                 f"（模型 {result['llm_calls']} 次，工具 {result['tool_calls']} 次，{result['seconds']} 秒）")
    summary = summarize(results, cases, conditions)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8", newline="\n")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8", newline="\n")
    return summary


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarize(results: list[dict[str, Any]], cases: list[Case], conditions: list[str]) -> dict[str, Any]:
    """按对比组和题目汇总。所有比例和平均值都在这里由程序算出。"""
    by_condition = {}
    for condition in conditions:
        rows = [r for r in results if r["condition"] == condition]
        mention_total = sum(len(r["mentions"]) for r in rows)
        by_condition[condition] = {
            "label": CONDITIONS[condition], "runs": len(rows), "passed": sum(r["passed"] for r in rows),
            "pass_rate": _ratio(sum(r["passed"] for r in rows), len(rows)),
            "completed_rate": _ratio(sum(r["status"] == "completed" for r in rows), len(rows)),
            "mention_hit_rate": _ratio(sum(m["hit"] for r in rows for m in r["mentions"]), mention_total),
            "violations": sum(len(r["violations"]) for r in rows),
            "grounded_rate": _ratio(sum(r["numbers_checked"] - r["numbers_ungrounded"] for r in rows),
                                    sum(r["numbers_checked"] for r in rows)),
            "skill_use_correct_rate": (_ratio(sum(r["skill_loaded"] == r["skill_expected"] for r in rows), len(rows))
                                       if condition == "agent_skill" else None),
            "process_complete_rate": (_ratio(sum(r["process_complete"] is True for r in rows if r["skill_expected"]),
                                             sum(1 for r in rows if r["skill_expected"]))
                                      if condition == "agent_skill" else None),
            "avg_tool_calls": _ratio(sum(r["tool_calls"] for r in rows), len(rows)),
            "avg_llm_calls": _ratio(sum(r["llm_calls"] for r in rows), len(rows)),
            "avg_tokens": _ratio(sum(r["tokens"] for r in rows), len(rows)),
            "avg_seconds": _ratio(sum(r["seconds"] for r in rows), len(rows)),
        }
    by_case = {case.id: {"category": case.category, **{
        condition: {"passed": sum(r["passed"] for r in results if r["case"] == case.id and r["condition"] == condition),
                    "runs": sum(1 for r in results if r["case"] == case.id and r["condition"] == condition)}
        for condition in conditions}} for case in cases}
    failures = [{"case": r["case"], "condition": r["condition"], "repeat": r["repeat"], "status": r["status"],
                 "missed": [m["any_of"] for m in r["mentions"] if not m["hit"]], "violations": r["violations"],
                 "numbers_ungrounded": r["numbers_ungrounded"], "value_found": r["value_found"]}
                for r in results if not r["passed"]]
    return {"conditions": conditions, "cases": len(cases), "runs": len(results),
            "by_condition": by_condition, "by_case": by_case, "failures": failures}


def compare_with_human(results: list[dict[str, Any]], review: dict[str, Any]) -> dict[str, Any]:
    """人工复核与自动评分的一致程度，分两种口径：

    - 完整口径：自动评分的“通过”（含数字出处等全部条件）
    - 要点口径：只看要点和禁止说法，和人工复核的判断标准一致
    """
    index = {(r["case"], r["condition"], r["repeat"]): r for r in results}
    rows = []
    for label in review["labels"]:
        r = index[(label["case"], label["condition"], label["repeat"])]
        substance = r["status"] == "completed" and all(m["hit"] for m in r["mentions"]) and not r["violations"]
        rows.append({"case": label["case"], "condition": label["condition"], "repeat": label["repeat"],
                     "blind": label["blind"], "human": label["human_pass"], "auto_passed": r["passed"],
                     "auto_substance": substance, "reason": label["reason"]})
    by_condition = {}
    for row in rows:
        stats = by_condition.setdefault(row["condition"], {"human_passed": 0, "reviewed": 0})
        stats["reviewed"] += 1
        stats["human_passed"] += row["human"]
    return {"reviewer": review["reviewer"], "method": review["method"], "reviewed": len(rows),
            "agree_passed": sum(row["human"] == row["auto_passed"] for row in rows),
            "agree_substance": sum(row["human"] == row["auto_substance"] for row in rows),
            "blind": sum(row["blind"] for row in rows), "by_condition": by_condition, "rows": rows}


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def render_summary(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    """结果表（Markdown）。只排版，不新增任何数字。"""
    cases_file = meta.get("cases_file", "eval/cases.yaml")
    lines = [f"# 评测结果：{meta['name']}", "",
             f"- 模型：{meta['model']}；题目 {summary['cases']} 道；共 {summary['runs']} 次运行；"
             f"Agent 组每题重复 {meta['repeats']} 次，直接问模型组每题 1 次",
             f"- 时间：{meta['finished_at']}；题集与评分规则见 [{cases_file}](../../{Path(cases_file).name})", "",
             "## 按对比组", "",
             "| 组别 | 运行 | 通过 | 通过率 | 要点命中率 | 违规 | 数字有出处率 | 流程使用正确率 | 流程完成率 | 平均工具调用 | 平均模型调用 | 平均 token | 平均用时（秒） |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for stats in summary["by_condition"].values():
        lines.append(f"| {stats['label']} | {stats['runs']} | {stats['passed']} | {_pct(stats['pass_rate'])} | "
                     f"{_pct(stats['mention_hit_rate'])} | {stats['violations']} | {_pct(stats['grounded_rate'])} | "
                     f"{_pct(stats['skill_use_correct_rate'])} | {_pct(stats['process_complete_rate'])} | "
                     f"{stats['avg_tool_calls']} | {stats['avg_llm_calls']} | "
                     f"{stats['avg_tokens']:.0f} | {stats['avg_seconds']} |")
    labels = [CONDITIONS[c] for c in summary["conditions"]]
    lines += ["", "## 按题目（通过次数 / 运行次数）", "",
              "| 题目 | 类别 | " + " | ".join(labels) + " |", "|---|---|" + "---:|" * len(labels)]
    for case_id, row in summary["by_case"].items():
        cells = " | ".join(f"{row[c]['passed']}/{row[c]['runs']}" for c in summary["conditions"])
        lines.append(f"| {case_id} | {row['category']} | {cells} |")
    lines += ["", "## 未通过的运行", ""]
    if not summary["failures"]:
        lines.append("无。")
    for f in summary["failures"]:
        reasons = []
        if f["status"] != "completed":
            reasons.append(f"状态 {f['status']}")
        reasons += [f"没提到 {'/'.join(group)}" for group in f["missed"]]
        reasons += [f"出现违规说法 `{pattern}`" for pattern in f["violations"]]
        if f["numbers_ungrounded"]:
            reasons.append(f"{f['numbers_ungrounded']} 个数字找不到出处")
        if f["value_found"] is False:
            reasons.append("没有给出正确数值")
        lines.append(f"- `{f['case']}` {CONDITIONS[f['condition']]} 第 {f['repeat']} 次：{'；'.join(reasons)}"
                     f"（[记录](runs/{f['case']}/{f['condition']}-{f['repeat']}/report.md)）")
    human = summary.get("human_review")
    if human:
        n = human["reviewed"]
        lines += ["", "## 人工复核（陷阱题）", "", f"- 复核人：{human['reviewer']}。{human['method']}",
                  "- 人工判断通过：" + "；".join(f"{CONDITIONS[c]} {s['human_passed']}/{s['reviewed']}"
                                         for c, s in human["by_condition"].items()),
                  f"- 自动评分与人工一致：只看要点和禁止说法（与人工标准相同）{human['agree_substance']}/{n}；"
                  f"按自动“通过”（还要求数字全部来自工具）{human['agree_passed']}/{n}。{human['blind']}/{n} 份为盲评。", "",
                  "| 题目 | 组别 | 次 | 人工 | 自动（要点口径） | 自动（通过） | 人工理由 |", "|---|---|---:|---|---|---|---|"]
        for row in human["rows"]:
            if row["human"] != row["auto_substance"] or row["human"] != row["auto_passed"]:
                mark = {True: "通过", False: "不通过"}
                lines.append(f"| {row['case']} | {CONDITIONS[row['condition']]} | {row['repeat']} | {mark[row['human']]} | "
                             f"{mark[row['auto_substance']]} | {mark[row['auto_passed']]} | {row['reason']} |")
    lines += ["", "## 说明与局限", "",
              "- 通过需要同时满足：正常结束、要点全部提到、没有违规说法、数字全部有出处、正确数值出现在答案里。",
              "- 流程使用正确率：该用流程的题加载了流程、不该用的题（查数、实验、拒答）没有加载的比例；流程完成率只统计应该使用流程的题。",
              "- “直接问模型”组可以自己计算，它算出的数字在指标表里找不到，所以数字有出处率低是预期的；这一列衡量的是“数字能不能被核对”，不是“算得对不对”。",
              "- 关键词评分是近似的：同义表述可能漏判，否定句可能误判，需要抽样人工复核。",
              "- 模型输出有随机性，题目数量也有限，结论只适用于这批题目。", ""]
    return "\n".join(lines)


def rescore(out_dir: Path, cases: list[Case], connections: Connections) -> dict[str, Any]:
    """用当前的核查和评分规则，给已保存的运行重新打分，不调用模型。

    评分规则修改后（例如试跑发现误判），用它把旧运行重新评一遍，保证所有结果使用同一套规则。
    """
    case_map = {case.id: case for case in cases}
    order = {case.id: i for i, case in enumerate(cases)}
    condition_order = list(CONDITIONS)
    results = []
    for run_path in (out_dir / "runs").glob("*/*/run.json"):
        case = case_map[run_path.parent.parent.name]
        condition, repeat = run_path.parent.name.rsplit("-", 1)
        run = json.loads(run_path.read_text(encoding="utf-8"))
        checks = check_run(run)
        expected = expected_value(connections.get(case), case) if case.expect_value else None
        result = score(case, run, checks, expected)
        _save(run_path.parent, run, checks, result)
        results.append({"case": case.id, "category": case.category, "condition": condition, "repeat": int(repeat),
                        **result})
    results.sort(key=lambda r: (order[r["case"]], condition_order.index(r["condition"]), r["repeat"]))
    used_cases = [case for case in cases if any(r["case"] == case.id for r in results)]
    conditions = [c for c in condition_order if any(r["condition"] == c for r in results)]
    summary = summarize(results, used_cases, conditions)
    review_path = out_dir / "human_review.json"
    if review_path.exists():  # 有人工复核记录时，一并计算与自动评分的一致程度
        summary["human_review"] = compare_with_human(results, json.loads(review_path.read_text(encoding="utf-8")))
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + chr(10),
                                          encoding="utf-8", newline=chr(10))
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + chr(10),
                                          encoding="utf-8", newline=chr(10))
    return summary
