"""从头到尾：让 Agent 设计看板 → 取出它最后一次通过检查的方案 → 编译 → 生成页面。

看板方案就是运行记录里最后一次成功的 submit_dashboard 调用的参数，所以只要有 run.json 就能重新生成看板，
不用再调用模型。人工改了 spec.json 也能直接重新生成（render-dashboard）。
"""

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agent import AgentRun, run_agent, save_run
from ..llm import LLMClient
from ..paths import RUNS_DIR
from ..report import write_review
from .compile import compile_dashboard
from .render import render_dashboard
from .sources import connect_source
from .spec import DashboardSpec

DEFAULT_GOAL = "业务负责人：先看整体规模和趋势，再看构成和需要注意的地方"
MAX_TOOL_CALLS = 25  # 了解数据、预览几张图、提交（可能被退回一两次），比回答一个问题需要更多次


def final_spec(run: dict[str, Any]) -> DashboardSpec | None:
    """运行记录里最后一次通过检查的 submit_dashboard 的方案；没有就返回 None。"""
    for step in reversed(run["steps"]):
        if step["tool"] == "submit_dashboard" and step["ok"]:
            return DashboardSpec.model_validate(step["arguments"])
    return None


def write_dashboard(source: str, spec: DashboardSpec, run_dir: Path, meta: dict[str, Any]) -> Path:
    """编译方案，写出 spec.json（可以手工修改）和 dashboard.html。"""
    with connect_source(source) as con:
        compiled = compile_dashboard(con, spec)
    (run_dir / "spec.json").write_text(json.dumps(spec.model_dump(exclude_defaults=True), ensure_ascii=False, indent=2)
                                       + "\n", encoding="utf-8", newline="\n")
    path = run_dir / "dashboard.html"
    path.write_text(render_dashboard(compiled, meta), encoding="utf-8", newline="\n")
    return path


def build_dashboard(source: str, goal: str | None, llm: LLMClient, max_tool_calls: int = MAX_TOOL_CALLS,
                    runs_dir: Path | None = None,
                    on_step: Callable[[dict[str, Any]], None] | None = None) -> tuple[AgentRun, Path, Path | None]:
    """返回（运行记录、运行目录、看板页面；Agent 没有提交通过的方案时页面为 None）。"""
    question = f"请为下面的读者搭一个看板。读者和目标：{goal or DEFAULT_GOAL}"
    with connect_source(source) as con:
        run = run_agent(question, llm=llm, con=con, max_tool_calls=max_tool_calls, domain="dashboard", on_step=on_step)
    run_dir = save_run(run, runs_dir or RUNS_DIR)
    write_review(run_dir)
    spec = final_spec(asdict(run))
    page = None
    if spec is not None:
        meta = {"run_id": run.run_id, "model": run.model, "generated_at": f"{datetime.now():%Y-%m-%d %H:%M}"}
        page = write_dashboard(source, spec, run_dir, meta | {"source": source})
        (run_dir / "dashboard_meta.json").write_text(json.dumps(meta | {"source": source}, ensure_ascii=False) + "\n",
                                                     encoding="utf-8", newline="\n")
    return run, run_dir, page


def rerender(run_dir: Path, spec_path: Path | None = None) -> Path:
    """不调用模型，用运行目录里的方案（或改过的 spec.json）重新生成看板。"""
    meta_path = run_dir / "dashboard_meta.json"
    if not meta_path.exists():
        raise ValueError(f"{run_dir} 不是看板运行目录（没有 dashboard_meta.json）")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    path = spec_path or run_dir / "spec.json"
    spec = DashboardSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))
    meta["generated_at"] = f"{datetime.now():%Y-%m-%d %H:%M}（按 {path.name} 重新生成）"
    return write_dashboard(meta["source"], spec, run_dir, meta)
