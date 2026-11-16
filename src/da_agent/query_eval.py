"""查询 Agent 的评测（M19）：它算出来的数，和人写的参考 SQL 算出来的是不是一样。

每道题是一个分析请求加一条参考 SQL（eval/query_cases.yaml）。参考 SQL 在另一条路上执行：
电商题用清洗后的基础表，不经过口径配置里的表；所以两边对上，不是因为用了同一段代码。

打分由程序完成，不靠人读：
- 应该回答的题：结果表里要能找到参考结果的每一个数。有 keys 的题，数要出现在对应的那一行；
  没有 keys 的题，出现在结果表的任何位置都算。结果的行数不能比参考结果多太多（防止把整张表倒出来碰运气）。
- 应该拒答的题：没有提交结果、用文字说明了原因，才算对。
- 每次运行的结局是四种之一：correct、wrong（交了结果但不对，最要紧的一种）、declined（该答的没答）、failed（没跑完）。

对照组：电商题同时在两份配置上跑。retail 把口径做成了现成的列和表达式；retail_raw 只有文字口径。
"""

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import yaml

from .analyst import run_query
from .llm import LLMClient
from .paths import ROOT
from .semantic import CSV_TABLE, CSV_TYPES, QueryDB, prepare
from .sqlrun import _plain

PLAN_PATH = ROOT / "eval" / "query_cases.yaml"
EXTRA_ROWS = 5  # 结果最多可以比参考结果多这么多行
RELATIVE_TOLERANCE = 0.005  # 四舍五入之外，数值最多可以差这么多（相对）
API_RETRIES = 4  # 模型接口报错（限流、超时）时最多重跑几次；这不是查询 Agent 的表现，不该算成它没答上来
RETRY_WAIT_SECONDS = 45  # 第 n 次重跑前等 n × 这么多秒
RETAIL_CONDITIONS = {"dev": ("retail",), "holdout": ("retail", "retail_raw")}
OUTCOMES = ("correct", "wrong", "declined", "failed")
GROUP_LABELS = {"tool": "固定工具也能答", "beyond": "固定工具答不了", "decline": "应该拒答", "csv": "没有口径配置的 CSV"}


@dataclass(frozen=True)
class QueryCase:
    id: str
    data: str  # retail，或 datasets 里的一个 CSV
    group: str
    request: str
    kind: str = "answer"  # answer / decline
    reference_sql: str = ""
    keys: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    rates: tuple[str, ...] = field(default=())


def load_plan(path: Path = PLAN_PATH) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    plan: dict[str, Any] = {"datasets": raw["datasets"]}
    for split in ("dev", "holdout"):
        cases = [QueryCase(**{**item, **{k: tuple(item.get(k, ())) for k in ("keys", "values", "rates")}}) for item in raw[split]]
        for case in cases:
            if case.kind == "answer" and not (case.reference_sql and case.values):
                raise ValueError(f"题目 {case.id} 缺参考 SQL 或要核对的数值列")
            if not set(case.rates) <= set(case.values):
                raise ValueError(f"题目 {case.id} 的 rates 必须是 values 里的列")
        if len({case.id for case in cases}) != len(cases):
            raise ValueError(f"{split} 里有重复的题目编号")
        plan[split] = cases
    return plan


def reference_connection(data: str, plan: dict[str, Any], root: Path = ROOT) -> duckdb.DuckDBPyConnection:
    """参考 SQL 执行的地方：电商是清洗后的基础表；CSV 是原样读进来的一张表。"""
    if data == "retail":
        from .semantic import _retail_base

        return _retail_base()
    con = duckdb.connect()
    con.execute(f'CREATE TABLE "{CSV_TABLE}" AS SELECT * FROM read_csv(?, header = true, auto_type_candidates = {CSV_TYPES})',
                [(root / plan["datasets"][data]).as_posix()])
    return con


def reference_rows(con: duckdb.DuckDBPyConnection, case: QueryCase) -> list[dict[str, Any]]:
    cursor = con.execute(case.reference_sql)
    columns = [item[0] for item in cursor.description]
    rows = [dict(zip(columns, (_plain(v) for v in row), strict=True)) for row in cursor.fetchall()]
    missing = [c for c in (*case.keys, *case.values) if c not in columns]
    if missing or not rows:
        raise ValueError(f"题目 {case.id} 的参考 SQL 没有返回行，或者缺少列 {missing}")
    return rows


def _decimals(value: float | int) -> int:
    text = repr(float(value))
    return 0 if "e" in text or text.endswith(".0") else min(len(text.split(".")[1]), 6)


def close(result: Any, expected: float, rate: bool = False) -> bool:
    """结果里的一个值是不是参考值：允许按它自己的小数位四舍五入，此外相对误差不超过 0.5%。比率也认百分数写法。"""
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        return False
    places = _decimals(result)
    # 百分数除以 100 后小数位多 2 位（37.93 → 0.3793）；按位数取整，免得浮点误差让它看起来有十几位小数
    candidates = [(result, places)] + ([(round(result / 100, places + 2), places + 2)] if rate else [])
    for candidate, digits in candidates:
        rounding = 0.5 * 10 ** -digits + 1e-9
        if abs(candidate - expected) <= rounding and abs(candidate - expected) <= max(RELATIVE_TOLERANCE * abs(expected), 1e-9):
            return True
    return False


def _same_text(cell: Any, key: Any) -> bool:
    return cell is not None and str(cell).strip().casefold() == str(key).strip().casefold()


def score(case: QueryCase, expected: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    """给一次运行打分。返回 {outcome, detail}。"""
    status = result["status"]
    if status == "failed":
        return {"outcome": "failed", "detail": result.get("message", "")[:200]}
    if case.kind == "decline":
        return ({"outcome": "correct", "detail": "没有提交结果"} if status == "declined"
                else {"outcome": "wrong", "detail": "数据里没有答案，却交出了结果"})
    if status == "declined":
        return {"outcome": "declined", "detail": result.get("message", "")[:200]}
    rows = result["rows"]
    if len(rows) > len(expected) + EXTRA_ROWS:
        return {"outcome": "wrong", "detail": f"结果有 {len(rows)} 行，参考结果只有 {len(expected)} 行"}
    missing = []
    for want in expected:
        if case.keys:
            pool = [row for row in rows if all(any(_same_text(cell, want[key]) for cell in row.values()) for key in case.keys)]
            label = "、".join(str(want[key]) for key in case.keys)
            if not pool:
                missing.append(f"没有 {label} 这一行")
                continue
        else:
            pool, label = rows, ""
        for column in case.values:
            if want[column] is None:
                continue
            if not any(close(cell, want[column], column in case.rates) for row in pool for cell in row.values()):
                missing.append(f"{label + ' 的 ' if label else ''}{column} 应为 {want[column]}")
    if missing:
        return {"outcome": "wrong", "detail": "；".join(missing[:4]) + (f"……共 {len(missing)} 处" if len(missing) > 4 else "")}
    return {"outcome": "correct", "detail": ""}


def tasks(plan: dict[str, Any], split: str, repeats: int) -> list[tuple[QueryCase, str, int]]:
    """要跑的每一次：（题目，用哪份数据，第几次）。电商题在每份对照配置上各跑一遍。"""
    out = []
    for case in plan[split]:
        for condition in (RETAIL_CONDITIONS[split] if case.data == "retail" else (case.data,)):
            out += [(case, condition, n) for n in range(1, repeats + 1)]
    return out


def run_query_eval(plan: dict[str, Any], split: str, repeats: int, make_llm: Callable[[], LLMClient], out_dir: Path,
                   workers: int = 3, databases: dict[str, QueryDB] | None = None,
                   references: dict[str, duckdb.DuckDBPyConnection] | None = None,
                   wait: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """跑一遍评测，逐次保存运行记录和打分，返回每次运行一行的结果。databases、references、wait 留给测试传入。

    模型接口报错（状态 llm_error）的运行等一会儿重跑，最多 API_RETRIES 次；重跑了几次记在 api_retries 里。
    """
    todo = tasks(plan, split, repeats)
    if databases is None:
        databases = {name: prepare(name if name.startswith("retail") else str(ROOT / plan["datasets"][name]))
                     for name in sorted({condition for _, condition, _ in todo})}
    own_references = references is None
    if references is None:
        references = {data: reference_connection(data, plan) for data in sorted({case.data for case, _, _ in todo})}
    expected = {case.id: reference_rows(references[case.data], case) for case in plan[split] if case.kind == "answer"}
    if own_references:
        for con in references.values():
            con.close()

    def one(task: tuple[QueryCase, str, int]) -> dict[str, Any]:
        case, condition, n = task
        for attempt in range(API_RETRIES + 1):
            out = run_query(case.request, llm=make_llm(), db=databases[condition])
            if out["run"]["status"] != "llm_error" or attempt == API_RETRIES:
                break
            wait(RETRY_WAIT_SECONDS * (attempt + 1))
        result = out["result"]
        verdict = score(case, expected.get(case.id, []), result)
        run_dir = out_dir / "runs" / case.id / f"{condition}-{n}"
        run_dir.mkdir(parents=True, exist_ok=True)
        for name, content in (("run.json", out["run"]), ("query.json", result),
                              ("score.json", verdict | {"expected": expected.get(case.id)})):
            (run_dir / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, default=str) + "\n",
                                        encoding="utf-8", newline="\n")
        blocked = [s["error"] for s in out["run"]["steps"] if s["error"] and ("只能执行以" in s["error"] or "不能使用" in s["error"])]
        return {"case": case.id, "group": case.group, "data": case.data, "condition": condition, "repeat": n, **verdict,
                "status": result["status"], "sql_runs": result["sql_runs"], "rejected": result["rejected"],
                "blocked": len(blocked), "metrics_used": result.get("metrics_used", []), "api_retries": attempt,
                "tokens": result["usage"]["prompt_tokens"] + result["usage"]["completion_tokens"], "seconds": result["seconds"]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(one, todo))
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按“哪份数据 × 哪类题”汇总四种结局，以及成本。"""
    def block(picked: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"runs": len(picked), **{o: sum(r["outcome"] == o for r in picked) for o in OUTCOMES}}
        out["avg_tokens"] = round(sum(r["tokens"] for r in picked) / len(picked)) if picked else 0
        out["avg_seconds"] = round(sum(r["seconds"] for r in picked) / len(picked), 1) if picked else 0
        return out

    conditions = list(dict.fromkeys(r["condition"] for r in rows))
    table = {}
    for condition in conditions:
        mine = [r for r in rows if r["condition"] == condition]
        for group in [g for g in GROUP_LABELS if any(r["group"] == g for r in mine)]:
            table[f"{condition}/{group}"] = block([r for r in mine if r["group"] == group])
        table[f"{condition}/合计"] = block(mine)
    cases = {}
    for r in rows:
        cases.setdefault(r["case"], {}).setdefault(r["condition"], []).append(r["outcome"])
    return {"table": table, "cases": cases, "blocked_statements": sum(r["blocked"] for r in rows),
            "api_retries": sum(r["api_retries"] for r in rows),
            "not_correct": [{k: r[k] for k in ("case", "condition", "repeat", "outcome", "detail")} for r in rows if r["outcome"] != "correct"]}


def _share(part: int, whole: int) -> str:
    return f"{part}/{whole}" + (f"（{part / whole:.1%}）" if whole else "")


def render_summary(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    lines = [f"# 查询 Agent 的评测：{meta['name']}", "",
             f"题目：{meta['split_label']}；模型 {meta['model']}；每道题每份数据跑 {meta['repeats']} 次；{meta['finished_at']}。"
             "评分标准：[query_rubric.md](../../query_rubric.md)。", "",
             "| 数据 | 题目类型 | 运行 | 算对 | 交了但不对 | 该答没答 | 没跑完 | 平均 token | 平均用时（秒） |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for key, b in summary["table"].items():
        condition, group = key.split("/")
        lines.append(f"| {condition} | {GROUP_LABELS.get(group, group)} | {b['runs']} | {_share(b['correct'], b['runs'])} | {b['wrong']} | "
                     f"{b['declined']} | {b['failed']} | {b['avg_tokens']:,} | {b['avg_seconds']} |")
    lines += ["", f"被检查挡下的语句（不是 SELECT，或调用了不允许的函数）：{summary['blocked_statements']} 条。"
              f"因模型接口报错（限流、超时）重跑：{summary['api_retries']} 次。", ""]
    if summary["not_correct"]:
        lines += ["## 没算对的运行", "", "| 题目 | 数据 | 第几次 | 结局 | 说明 |", "|---|---|---:|---|---|"]
        labels = {"wrong": "交了但不对", "declined": "该答没答", "failed": "没跑完"}
        lines += [f"| {r['case']} | {r['condition']} | {r['repeat']} | {labels[r['outcome']]} | {r['detail'].replace('|', '/')} |"
                  for r in summary["not_correct"]]
    return "\n".join(lines) + "\n"
