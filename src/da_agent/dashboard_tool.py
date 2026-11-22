"""看板 Agent 作为主 Agent 的工具（M22）：用户要看板时，主 Agent 写出“给谁看、判断什么”，交给看板 Agent 去搭。

看板 Agent 就是 M13 的自主搭看板（autodash.build）：它写看板方案，程序校验口径、算数、画图，结论里的数字必须在图的数据里。
这里只做“接上”：
- 主 Agent 调用 build_dashboard(goal)，看板 Agent 跑完一整次，页面存到它自己的运行目录；
- 交回去的是程序生成的东西：页面位置、每张图的标题和类型、图里的结论（数字由看板程序核对过）、程序自动发现的数据问题；
- 看板 Agent 自己写的数据说明放在 unverified_notes 里，数字核查不把它当出处（和查询 Agent 的说明一样）；
- 看板 Agent 的完整运行记录附在主运行的 sub_runs 里。
没有交出通过检查的方案时（工具次数用完、模型报错），工具报错，主 Agent 照实告诉用户。
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .llm import LLMClient

UNVERIFIED_NOTE = "看板 Agent 自己写的数据说明，程序没有核对：其中的数字不能当作出处，推测不能当作事实"


class BuildDashboardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=8, max_length=300, description=(
        "看板给谁看、帮他判断什么，例如“给运营负责人看 2011 年各国销售走势和取消情况，判断哪些市场要重点跟进”"))


@dataclass
class DashboardService:
    """主 Agent 调用看板 Agent 时用的上下文。每次运行新建一个：records 是这次运行里每次搭看板的完整记录。"""

    llm: LLMClient
    source: str = "retail"
    max_tool_calls: int | None = None  # 不填用看板 Agent 自己的上限（autodash.build.MAX_TOOL_CALLS）
    runs_dir: Path | None = None  # 看板运行目录放哪；不填放 runs/
    records: list[dict[str, Any]] = field(default_factory=list)
    # 一次回答里最多搭几次：搭一次要几分钟、几十万 token，没搭出来时主 Agent 会把目标缩小再试，但不能一直试
    max_builds: int = 2


def _accepted_charts(record: dict[str, Any]) -> list[dict[str, Any]]:
    """看板 Agent 最后一次通过检查的提交，程序返回的每张图的自动提示（数据问题）。"""
    results = [json.loads(m["content"]) for m in record["messages"] if m["role"] == "tool"]
    for step, result in zip(reversed(record["steps"]), reversed(results), strict=True):
        if step["tool"] == "submit_dashboard" and step["ok"]:
            return result["charts"]
    return []


def build_dashboard_tool(service: DashboardService, args: BuildDashboardArgs) -> dict[str, Any]:
    from .autodash.build import MAX_TOOL_CALLS, build_dashboard, final_spec

    if len(service.records) >= service.max_builds:
        raise ValueError(f"这次回答已经试过 {service.max_builds} 次搭看板，不再尝试。请在回答中照实说明，并建议用户把要看的内容说得更具体")
    run, run_dir, page = build_dashboard(service.source, args.goal, service.llm, service.max_tool_calls or MAX_TOOL_CALLS,
                                         runs_dir=service.runs_dir)
    record = asdict(run)
    service.records.append(record)
    spec = final_spec(record)
    if page is None or spec is None:
        reason = run.error or ("工具次数用完" if run.budget_exhausted else f"状态 {run.status}")
        raise ValueError(f"看板 Agent 没有交出通过检查的方案（{reason}）。可以把读者和要判断的事写得更具体再试一次，或者在回答中说明")
    notes = {c["id"]: c["notes"] for c in _accepted_charts(record)}
    problems = list(dict.fromkeys(note for chart_notes in notes.values() for note in chart_notes))
    return {"tool": "build_dashboard", "status": "built", "goal": args.goal, "page": str(page), "run_id": run.run_id,
            "dashboard_run": len(service.records), "title": spec.title,
            "charts": [{"title": c.title, "type": c.type, **({"insight": c.insight} if c.insight else {})} for c in spec.charts],
            "data_problems": problems,
            "unverified_notes": {"note": UNVERIFIED_NOTE, "notes": list(spec.notes)},
            "message": "看板已经生成。回答时告诉用户页面在哪、有哪几张图、读的时候要注意哪些数据问题；不要编造看板里没有的数字。"}
