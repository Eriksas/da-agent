"""运行复核 Agent：把一次运行的答案和工具调用记录交给第二个 Agent，取回它最后一次通过校验的复核意见。

复核 Agent 用的是同一个 Agent 循环（agent.run_agent），只是换了提示词和工具（calculate、submit_review），
并且不做“数字出处退回修正”：它的产出是结构化的意见，不是带数字的报告。
复核本身的运行记录也完整保存（review_run.json），复核意见可以像答案一样被追溯。
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .agent import SYSTEM_PROMPTS, run_agent
from .llm import LLMClient
from .report import render_agent_review
from .review import ReviewContext, review_request, tool_records

# 验算几次、提交（可能被退回一两次）。第一次评测时是 6 次，有一次复核把次数全用在验算上，没能提交
MAX_TOOL_CALLS = 10


def final_review(review_run: dict[str, Any]) -> dict[str, Any] | None:
    """复核运行里最后一次通过校验的 submit_review 的结果；没有就返回 None。"""
    results = [json.loads(m["content"]) for m in review_run["messages"] if m["role"] == "tool"]
    for step, result in zip(reversed(review_run["steps"]), reversed(results), strict=True):
        if step["tool"] == "submit_review" and step["ok"]:
            return result
    return None


def review_run(run: dict[str, Any], *, llm: LLMClient, max_tool_calls: int = MAX_TOOL_CALLS) -> dict[str, Any]:
    """复核一次运行（run 是 run.json 的内容）。返回 {"review": 复核结果, "run": 复核 Agent 自己的运行记录}。

    review.status：reviewed（拿到了通过校验的意见）、no_answer（原运行没有答案，不复核）、
    failed（复核 Agent 没有交出合格的意见：模型报错、次数用完等）。失败不影响原来的答案和程序核查。
    """
    if not run.get("answer"):
        return {"review": {"status": "no_answer", "verdict": None, "findings": []}, "run": None}
    ctx = ReviewContext(answer=run["answer"], tool_calls=len(tool_records(run)))
    prompt = SYSTEM_PROMPTS["review"].read_text(encoding="utf-8").format(max_tool_calls=max_tool_calls)
    record = asdict(run_agent(review_request(run), llm=llm, con=ctx, max_tool_calls=max_tool_calls,
                              system_prompt=prompt, repair=False, domain="review"))
    accepted = final_review(record)
    review = {"status": "reviewed" if accepted else "failed", "model": record["model"],
              "verdict": accepted["verdict"] if accepted else None,
              "findings": accepted["findings"] if accepted else [],
              "checked": accepted["checked"] if accepted else "",
              "error": None if accepted else (record["error"] or f"没有提交合格的复核意见（状态 {record['status']}）"),
              "rejected_submissions": sum(s["tool"] == "submit_review" and not s["ok"] for s in record["steps"]),
              "usage": record["usage"], "seconds": record["seconds"], "reviewed_run": run["run_id"]}
    return {"review": review, "run": record}


def write_agent_review(run_dir: Path, *, llm: LLMClient) -> dict[str, Any]:
    """复核 run_dir 里的运行，写出 review.json（意见）、review.md（给人看的）和 review_run.json（复核 Agent 的完整记录）。

    不改 report.md，原因见 report.render_agent_review。
    """
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    out = review_run(run, llm=llm)
    for name, content in (("review.json", out["review"]), ("review_run.json", out["run"])):
        if content is not None:
            (run_dir / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, default=str) + "\n",
                                        encoding="utf-8", newline="\n")
    (run_dir / "review.md").write_text(render_agent_review(out["review"]), encoding="utf-8", newline="\n")
    return out["review"]
