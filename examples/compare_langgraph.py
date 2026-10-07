"""M3b 对照：手写循环和 LangGraph 版的代码量、依赖、导入时间、运行开销，以及 LangGraph 自动画出的流程图。

在仓库根目录运行：uv run --group langgraph python examples/compare_langgraph.py
README 里的对照表来自这个脚本的输出；耗时和机器有关，每次运行会有差别。
"""

import ast
import importlib.util
import statistics
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from da_agent import cli
from da_agent.agent import run_agent
from da_agent.cleaning import connect_frame
from da_agent.dataset import read_normalized_csv
from da_agent.llm import FakeLLM

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("langgraph_version", ROOT / "examples" / "langgraph_version.py")
langgraph_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(langgraph_version)


def code_lines(path: Path, names: set[str]) -> int:
    """指定的类和函数一共多少行代码：不算空行、注释和文档字符串。"""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    total = 0
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) or node.name not in names:
            continue
        docstring = set()
        if ast.get_docstring(node) is not None:
            first = node.body[0]
            docstring = set(range(first.lineno, first.end_lineno + 1))
        total += sum(1 for n in range(node.lineno, node.end_lineno + 1)
                     if n not in docstring and lines[n - 1].strip() and not lines[n - 1].strip().startswith("#"))
    return total


def closure(packages: dict[str, dict], start: list[str]) -> set[str]:
    """从 start 出发，按锁文件里的依赖关系能到达的全部包（不区分操作系统）。"""
    seen, todo = set(), list(start)
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += [dep["name"] for dep in packages[name].get("dependencies", [])]
    return seen


def extra_packages() -> list[str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {p["name"]: p for p in lock["package"]}
    root = packages["da-agent"]
    base = closure(packages, [d["name"] for d in root["dependencies"]])
    with_group = closure(packages, [d["name"] for d in root["dev-dependencies"]["langgraph"]])
    return sorted(with_group - base)


def import_seconds(module: str, repeats: int = 5) -> float:
    """新开一个 Python 进程导入模块要多久（中位数）。"""
    code = f"import time; t = time.perf_counter(); import {module}; print(time.perf_counter() - t)"
    return statistics.median(float(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                                  check=True).stdout) for _ in range(repeats))


def run_ms(runner, repeats: int = 30) -> float:
    """用内置样例和演示剧本跑一次 Agent 要多少毫秒（中位数，假模型，不联网）。"""
    times = []
    with connect_frame(read_normalized_csv(cli.DEMO_DATA)) as con:
        for _ in range(repeats):
            llm = FakeLLM.from_file(cli.DEMO_SCRIPT)
            start = time.perf_counter()
            runner(cli.DEMO_QUESTION, llm=llm, con=con, max_tool_calls=5)
            times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


def main() -> None:
    hand_lines = code_lines(ROOT / "src" / "da_agent" / "agent.py", {"run_agent"})
    graph_lines = code_lines(ROOT / "examples" / "langgraph_version.py", {"State", "build_graph", "run_agent_langgraph"})
    extra = extra_packages()
    lg_import = import_seconds("langgraph.graph")
    hand_ms, graph_ms = run_ms(run_agent), run_ms(langgraph_version.run_agent_langgraph)
    print("| 对比项 | 手写循环 | LangGraph 版 |")
    print("|---|---:|---:|")
    print(f"| 控制流程的代码行数（不含空行、注释、文档字符串） | {hand_lines} | {graph_lines} |")
    print(f"| 额外依赖的包（按 uv.lock 计） | 0 | {len(extra)} |")
    print(f"| 额外导入时间（新进程导入 langgraph.graph，中位数） | — | {lg_import:.2f} 秒 |")
    print(f"| 跑一次演示剧本（假模型，中位数） | {hand_ms:.1f} 毫秒 | {graph_ms:.1f} 毫秒 |")
    print()
    print("额外依赖的包：" + "、".join(extra))
    print()
    with connect_frame(read_normalized_csv(cli.DEMO_DATA)) as con:
        app = langgraph_version.build_graph(FakeLLM(replies=[]), con, specs=[], repair_enabled=True)
        print("```mermaid")
        print(app.get_graph().draw_mermaid().strip())
        print("```")


if __name__ == "__main__":
    main()
