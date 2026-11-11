"""多模型对比（M16）：同一套题目、同一套评分规则，在几个模型上各跑一遍，把结果并排列出来。

之前所有结论都来自一个模型。这里只做汇总：读每次评测已经算好的 summary.json，不重新打分、不调用模型。
评测本身仍然用 `da-agent eval` 跑，换模型靠环境变量 LLM_MODEL。
"""

import json
from pathlib import Path
from typing import Any

import yaml

from .evaluation import CONDITIONS, RESULTS_DIR
from .paths import ROOT

PLAN_PATH = ROOT / "eval" / "model_comparison.yaml"


def load_plan(path: Path = PLAN_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def collect(plan: dict[str, Any], results_dir: Path = RESULTS_DIR) -> dict[str, Any]:
    """按计划读出每个模型在每套题上的汇总。还没跑完的记为 None。返回 {套题: {模型: {summary, meta}}}。"""
    out: dict[str, Any] = {}
    for suite in plan["suites"]:
        out[suite["id"]] = {}
        for model in plan["models"]:
            folder = results_dir / f"{plan['prefix']}-{model['id']}-{suite['id']}"
            if not (folder / "summary.json").exists():
                out[suite["id"]][model["id"]] = None
                continue
            out[suite["id"]][model["id"]] = {
                "summary": json.loads((folder / "summary.json").read_text(encoding="utf-8")),
                "meta": json.loads((folder / "meta.json").read_text(encoding="utf-8")), "folder": folder.name}
    return out


def _cell(row: dict[str, Any] | None, key: str) -> str:
    if row is None:
        return "没跑"
    if key == "passed":
        return f"{row['passed']}/{row['runs']}"
    if key == "draft":
        return f"{row['passed_without_repair']}/{row['runs']}"
    if key == "grounded":
        return "—" if row["grounded_rate"] is None else f"{row['grounded_rate']:.0%}"
    if key == "completed":
        return f"{round(row['completed_rate'] * row['runs'])}/{row['runs']}"
    if key == "tokens":
        return f"{row['avg_tokens']:,.0f}"
    return f"{row['avg_seconds']:.0f} 秒"


METRICS = [("passed", "通过"), ("draft", "只看初稿通过"), ("grounded", "数字有出处"), ("completed", "正常结束"),
           ("tokens", "平均 token"), ("seconds", "平均用时")]


def render_comparison(plan: dict[str, Any], data: dict[str, Any]) -> str:
    """并排的结果表：每套题、每个对比组一张，行是指标，列是模型；再列出各模型结果不同的题目。"""
    models = plan["models"]
    lines = [f"# 多模型对比：{plan['prefix']}", "",
             "同一套题目、同一套评分规则、同一份代码，只换模型（环境变量 LLM_MODEL）。计划和读法见 "
             "[model_comparison_plan.md](../model_comparison_plan.md)。", "",
             "| 模型 | 说明 | 评测记录里的模型名 |", "|---|---|---|"]
    for m in models:
        names = sorted({d[m["id"]]["meta"]["model"] for d in data.values() if d[m["id"]]})
        lines.append(f"| {m['id']} | {m['note']} | {'、'.join(names) or '没跑'} |")
    for suite in plan["suites"]:
        per_model = data[suite["id"]]
        for condition in suite["conditions"]:
            rows = {m["id"]: (per_model[m["id"]]["summary"]["by_condition"].get(condition) if per_model[m["id"]] else None)
                    for m in models}
            lines += ["", f"## {suite['label']}：{CONDITIONS[condition]}", "",
                      "| 指标 | " + " | ".join(m["id"] for m in models) + " |", "|---|" + "---:|" * len(models)]
            for key, label in METRICS:
                if condition == "baseline" and key == "draft":
                    continue  # 直接问模型没有退回修正
                lines.append(f"| {label} | " + " | ".join(_cell(rows[m["id"]], key) for m in models) + " |")
        lines += _differences(suite, per_model, models)
    return "\n".join(lines) + "\n"


def _differences(suite: dict[str, Any], per_model: dict[str, Any], models: list[dict[str, Any]]) -> list[str]:
    """各模型通过次数不一样的题目（只看 Agent 的对比组）。"""
    ready = [m["id"] for m in models if per_model[m["id"]]]
    conditions = [c for c in suite["conditions"] if c != "baseline"]
    if len(ready) < 2 or not conditions:
        return []
    by_case = {m: per_model[m]["summary"]["by_case"] for m in ready}
    lines = []
    for case in by_case[ready[0]]:
        cells = {m: (sum(by_case[m][case][c]["passed"] for c in conditions), sum(by_case[m][case][c]["runs"] for c in conditions))
                 for m in ready}
        if len({passed for passed, _ in cells.values()}) > 1:
            lines.append(f"| {case} | " + " | ".join(f"{cells[m][0]}/{cells[m][1]}" if m in cells else "没跑"
                                                    for m in (x["id"] for x in models)) + " |")
    if not lines:
        return ["", f"{suite['label']}：每道题各模型的通过次数都相同。"]
    return ["", f"### {suite['label']}：通过次数不同的题目（Agent 各组合计）", "",
            "| 题目 | " + " | ".join(m["id"] for m in models) + " |", "|---|" + "---:|" * len(models), *lines]
