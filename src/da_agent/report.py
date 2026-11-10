"""把一次运行整理成给人看的报告：AI 初稿 + 自动核查结果 + 工具调用记录 + 警告汇总。

报告顶部固定标注“AI 初稿，待人工复核”：数字可以自动核查，推理必须由人判断。
"""

import json
from pathlib import Path
from typing import Any

from .checks import check_run


def _warnings(run: dict[str, Any]) -> list[str]:
    """汇总本次所有工具结果里的警告，去重并保持出现顺序。"""
    found: list[str] = []
    for message in run["messages"]:
        if message["role"] == "tool":
            found += json.loads(message["content"]).get("warnings", [])
    return list(dict.fromkeys(found))


def review_summary(review: dict[str, Any] | None) -> str:
    """复核 Agent 结论的一句话摘要；没有复核过返回空串。"""
    if not review or review["status"] == "no_answer":
        return ""
    if review["status"] != "reviewed":
        return f"复核 Agent：没有完成（{review['error']}）"
    errors = sum(f["severity"] == "错误" for f in review["findings"])
    doubts = len(review["findings"]) - errors
    if not review["findings"]:
        return "复核 Agent：没有发现问题"
    return "复核 Agent：" + "、".join(part for part in (f"指出 {errors} 处错误" if errors else "",
                                                        f"{doubts} 处存疑" if doubts else "") if part)


def _review_section(review: dict[str, Any] | None) -> list[str]:
    if not review:  # 没有复核过的运行，报告和以前一样
        return []
    lines = ["", "## 附录 G：复核 Agent 的意见", ""]
    if review["status"] != "reviewed":
        return lines + [f"复核没有完成：{review.get('error') or '原运行没有答案'}。"]
    lines += [f"第二个 Agent（模型 {review['model']}）只看问题、答案和工具调用记录，核对程序查不了的内容："
              "数字有没有用错对象、方向有没有说反、概括和因果有没有依据。**它的意见同样是 AI 的判断，供人工复核时参考。**", "",
              f"- 结论：**{review['verdict']}**。{review['checked']}",
              f"- 调用模型 {review['usage']['llm_calls']} 次，token {review['usage']['prompt_tokens']} + "
              f"{review['usage']['completion_tokens']}，用时 {review['seconds']} 秒", ""]
    if review["findings"]:
        lines += ["| 级别 | 类别 | 答案原句 | 理由 | 依据（第几次工具调用） |", "|---|---|---|---|---|"]
        lines += [f"| {f['severity']} | {f['category']} | {f['quote'].replace('|', '/')} | {f['explanation'].replace('|', '/')} | "
                  f"{'、'.join(str(n) for n in f['evidence']) or '—'} |" for f in review["findings"]]
    return lines


def render_report(run: dict[str, Any], checks: dict[str, Any], review: dict[str, Any] | None = None) -> str:
    """渲染 report.md。只排版，不新增任何数字。review 是复核 Agent 的意见（review.json），没有就不显示结论。"""
    numbers, phrases = checks["numbers"], checks["phrases"]
    usage = run["usage"]
    verdict = review_summary(review)
    lines = [
        f"> **AI 初稿，待人工复核。** {checks['summary']}。{checks['process']['summary']}。" + (f"{verdict}。" if verdict else ""),
        "",
        f"**问题：** {run['question']}", "",
        run.get("answer") or f"（没有答案，状态：{run['status']}）", "",
        "---", "", "## 附录 A：流程检查与危险信号", "",
    ]
    for skill in checks["process"]["skills"]:
        lines.append(f"- 流程 `{skill['name']}`：必做步骤 {'、'.join(skill['required'])}；"
                     + (f"**缺少 {'、'.join(skill['missing'])}**" if skill["missing"] else "全部完成"))
    if not checks["process"]["skills"]:
        lines.append("- 本次没有加载分析流程。")
    lines += [f"- ⚠️ {signal}" for signal in checks["danger_signals"]] or ["- 没有变化超过 50% 的核心指标。"]
    lines += ["", "## 附录 B：找不到出处的数字", ""]
    repair = run.get("repair")
    if repair:
        lines += [f"初稿有 {len(repair['ungrounded'])} 个数字找不到出处（{'、'.join(m['text'] for m in repair['ungrounded'])}），"
                  "已退回模型修正 1 次" + (f"，修正失败（{repair['error']}），上面仍是初稿。" if repair.get("error")
                                         else "。上面是修正后的版本，下面是对它的核查。"), ""]
    for calc in numbers["unsupported_calculations"]:
        lines.append(f"- calculate `{calc['expression']}`（{calc['purpose']}）的输入 "
                     f"{'、'.join(calc['unsupported_inputs'])} 找不到出处，计算结果不算出处。")
    if numbers["unsupported_calculations"]:
        lines.append("")
    if numbers["ungrounded"]:
        lines += ["这些数字在本次工具输出里找不到，可能是模型自己算的或编的，**必须核对**：", "",
                  "| 数字 | 所在句子 |", "|---|---|"]
        lines += [f"| {m['text']} | {m['context'].replace('|', '/')} |" for m in numbers["ungrounded"]]
    else:
        lines.append(f"无。{numbers['checked']} 个数字都能在工具输出中找到出处。"
                     "注意：这只说明数字不是编的，不保证每个数字被用在了正确的指标上。")
    lines += ["", "## 附录 C：需要人工复核的表述", ""]
    if phrases:
        lines += ["因果表述需要对照实验支持；绝对化或推测性的说法需要证据。请逐句判断：", "",
                  "| 类别 | 词 | 所在句子 |", "|---|---|---|"]
        lines += [f"| {p['category']} | {p['phrase']} | {p['context'].replace('|', '/')} |" for p in phrases]
    else:
        lines.append("未发现因果表述或绝对化说法。推理是否成立仍需人工判断。")
    lines += ["", "## 附录 D：工具调用", "", "| 轮 | 工具 | 参数 | 结果 |", "|---:|---|---|---|"]
    for step in run["steps"]:
        result = "成功" if step["ok"] else f"失败：{step['error']}"
        lines.append(f"| {step['round']} | {step['tool']} | `{json.dumps(step['arguments'], ensure_ascii=False)}` | {result} |")
    lines += ["", "## 附录 E：工具警告汇总", ""]
    lines += [f"- {w}" for w in _warnings(run)] or ["- 无"]
    lines += ["", "## 附录 F：运行信息", "",
              f"- 运行编号 `{run['run_id']}`，状态 {run['status']}，模型 {run['model']}",
              f"- 调用模型 {usage['llm_calls']} 次，token {usage['prompt_tokens']} + {usage['completion_tokens']}，用时 {run['seconds']} 秒",
              f"- 工具调用上限 {run['max_tool_calls']} 次" + ("，本次已用完" if run["budget_exhausted"] else "")]
    lines += _review_section(review) + [""]
    return "\n".join(lines)


def write_review(run_dir: Path) -> dict[str, Any]:
    """读取 run.json，写出 checks.json 和 report.md，返回核查结果。可以对旧的运行记录重复执行。"""
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    checks = check_run(run)
    (run_dir / "checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8", newline="\n")
    review_path = run_dir / "review.json"  # 复核过（da-agent review）就把意见一起写进报告
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else None
    (run_dir / "report.md").write_text(render_report(run, checks, review), encoding="utf-8", newline="\n")
    return checks
