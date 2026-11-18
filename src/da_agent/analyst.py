"""查询 Agent（M19）：把一个分析请求写成只读 SQL，算出一张结果表。它不写报告，只负责把数算对。

和项目里其他地方一样，模型只做判断，程序做执行和校验：
- 模型写 SQL；程序检查、在只读数据库上执行（sqlrun.py）；
- 交出去的结果是程序最后一次执行得到的表，不是模型抄写的数字；
- 提交时程序检查：结果不能是空的、不能超过行数上限、每一列都要说明含义；
- 数据里没有请求需要的信息时，模型应当不提交，用文字说明原因（status = declined）。

用的是同一个 Agent 循环（agent.run_agent），换了提示词和两个工具（run_sql、submit_query），不做数字出处的退回修正。
"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from .llm import LLMClient
from .semantic import QueryDB, catalog_text, metrics_used
from .sqlrun import MAX_ROWS, MAX_SQL_CHARS, run_select

MAX_TOOL_CALLS = 8
PREVIEW_ROWS = 30  # run_sql 只把前这么多行交给模型看：试查询用不着更多，省 token


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunSqlArgs(_Model):
    sql: str = Field(min_length=1, max_length=MAX_SQL_CHARS, description="一条 DuckDB 的 SELECT 查询（可以用 WITH），只能读数据说明里列出的表")
    purpose: str = Field(min_length=1, max_length=100, description="这次查询想确认什么，例如“看 2011-W48 各国 GMV”")


class SubmitQueryArgs(_Model):
    sql: str = Field(min_length=1, max_length=MAX_SQL_CHARS, description="最终的那一条查询。程序会重新执行它，执行结果就是交出去的全部内容")
    columns: dict[str, str] = Field(min_length=1, description=(
        "结果里每一列的含义和单位，键是列名，例如 {\"country\": \"国家\", \"gmv\": \"GMV（英镑）\", \"share\": \"占全站 GMV 的比例（小数）\"}"))
    definition: str = Field(min_length=4, max_length=500, description="口径：用了哪些指标、什么时间范围、哪些筛选条件、怎么去重")
    caveats: list[str] = Field(default_factory=list, max_length=5, description="读这个结果要注意什么，例如样本少、周不完整；没有就留空")


@dataclass
class QueryContext:
    """查询工具的上下文：只读连接和数据说明。在工具注册表里占的是分析工具“数据库连接”的位置。"""

    con: duckdb.DuckDBPyConnection
    catalog: dict[str, Any]


def run_sql(ctx: QueryContext, args: RunSqlArgs) -> dict[str, Any]:
    """试一条查询：返回前几行。出错时 sqlrun 抛出的 ValueError 由工具层交还给模型。"""
    result = run_select(ctx.con, args.sql)
    shown = result["rows"][:PREVIEW_ROWS]
    note = None
    if result["truncated"]:
        note = f"结果超过 {MAX_ROWS} 行，这里只显示前 {len(shown)} 行。最终提交的查询不能超过 {MAX_ROWS} 行，请聚合或加 LIMIT"
    elif len(shown) < result["row_count"]:
        note = f"共 {result['row_count']} 行，这里只显示前 {len(shown)} 行"
    return {"tool": "run_sql", "purpose": args.purpose, "columns": result["columns"], "rows": shown,
            "row_count": result["row_count"], **({"note": note} if note else {})}


def submit_query(ctx: QueryContext, args: SubmitQueryArgs) -> dict[str, Any]:
    """提交最终查询：重新执行并检查。通过后返回的 rows 就是交给下游的结果。"""
    result = run_select(ctx.con, args.sql)
    problems = []
    if result["truncated"]:
        problems.append(f"结果超过 {MAX_ROWS} 行：请聚合到请求需要的粒度，或者用 ORDER BY 加 LIMIT 只留需要的行")
    if result["row_count"] == 0:
        problems.append("查询没有返回任何行：检查筛选条件的写法（对照数据说明里的取值）。如果数据里确实没有请求要的内容，不要提交，直接用文字说明")
    missing = [c for c in result["columns"] if c not in args.columns]
    extra = [c for c in args.columns if c not in result["columns"]]
    if missing or extra:
        problems.append(f"columns 要和结果的列一一对应：结果的列是 {result['columns']}"
                        + (f"，没有说明 {missing}" if missing else "") + (f"，多写了 {extra}" if extra else ""))
    if result["rows"] and all(value is None for row in result["rows"] for value in row.values()):
        problems.append("结果全是空值：检查聚合的列和筛选条件")
    if problems:
        raise ValueError("提交没有通过：" + "；".join(problems))
    return {"tool": "submit_query", "accepted": True, "sql": result["sql"],
            "columns": [{"name": name, "meaning": args.columns[name]} for name in result["columns"]],
            "rows": result["rows"], "row_count": result["row_count"], "definition": args.definition,
            "caveats": args.caveats, "metrics_used": metrics_used(ctx.catalog, result["sql"])}


def final_result(record: dict[str, Any]) -> dict[str, Any] | None:
    """查询运行里最后一次通过检查的 submit_query 的结果；没有就返回 None。"""
    results = [json.loads(m["content"]) for m in record["messages"] if m["role"] == "tool"]
    for step, result in zip(reversed(record["steps"]), reversed(results), strict=True):
        if step["tool"] == "submit_query" and step["ok"]:
            return result
    return None


def build_prompt(catalog: dict[str, Any], max_tool_calls: int = MAX_TOOL_CALLS) -> str:
    from .agent import SYSTEM_PROMPTS

    return SYSTEM_PROMPTS["analyst"].read_text(encoding="utf-8").format(
        catalog=catalog_text(catalog), max_tool_calls=max_tool_calls, max_rows=MAX_ROWS)


class QueryDataArgs(_Model):
    request: str = Field(min_length=6, max_length=500, description=(
        "一个明确的取数请求：时间范围、指标、分组、筛选条件、排序和要几行。"
        "例如“2011 年 11 月按星期几（周一到周日）分别合计 GMV 和订单数”"))


@dataclass
class QueryService:
    """原来的 Agent 调用查询 Agent 时用的上下文（M19b）。每次运行新建一个：records 是这次运行里每次取数的完整记录。"""

    llm: LLMClient
    db: QueryDB
    max_tool_calls: int = MAX_TOOL_CALLS
    records: list[dict[str, Any]] = field(default_factory=list)
    # 查询 Agent 用哪种循环实现；不填就是 agent.run_agent。LangGraph 对照版（examples/langgraph_version.py）换成它自己的
    agent: Callable[..., Any] | None = None


UNVERIFIED_NOTE = "查询 Agent 自己写的说明，程序没有核对：其中的数字不能当作出处，原因推测不能当作事实"


def query_data(service: QueryService, args: QueryDataArgs) -> dict[str, Any]:
    """原来的 Agent 的 query_data 工具：把请求交给查询 Agent，返回程序执行得到的结果表。

    交回去的内容分两部分：rows、columns、sql 是程序执行出来的，可以直接引用；
    查询 Agent 写的口径说明和注意事项放在 unverified_notes 里，数字核查不把它们当出处（checks.UNVERIFIED_KEYS）。
    """
    out = run_query(args.request, llm=service.llm, db=service.db, max_tool_calls=service.max_tool_calls, agent=service.agent)
    service.records.append(out["run"])
    result = out["result"]
    base = {"tool": "query_data", "request": args.request, "status": result["status"], "query_run": len(service.records)}
    if result["status"] == "answered":
        return base | {"columns": result["columns"], "rows": result["rows"], "row_count": result["row_count"], "sql": result["sql"],
                       "unverified_notes": {"note": UNVERIFIED_NOTE, "definition": result["definition"], "caveats": result["caveats"]}}
    if result["status"] == "declined":
        return base | {"unverified_notes": {"note": UNVERIFIED_NOTE, "reason": result["message"]},
                       "message": "查询 Agent 认为数据里没有这个请求需要的信息，没有给出结果。"}
    raise ValueError(f"取数没有完成：{result['message'][:200]}。可以换一种更明确的写法再试一次，或者在回答中说明")


def run_query(request: str, *, llm: LLMClient, db: QueryDB, max_tool_calls: int = MAX_TOOL_CALLS,
              agent: Callable[..., Any] | None = None) -> dict[str, Any]:
    """让查询 Agent 回答一个分析请求。返回 {"result": 结果, "run": 查询 Agent 自己的完整运行记录}。

    result.status：answered（交出了通过检查的结果表）、declined（没有提交，用文字说明了为什么答不了）、
    failed（模型报错、次数用完等，没有结果）。agent：循环的实现，不填就是 agent.run_agent。
    """
    from .agent import run_agent

    con = db.connect()
    try:
        record = asdict((agent or run_agent)(request, llm=llm, con=QueryContext(con, db.catalog), max_tool_calls=max_tool_calls,
                                  system_prompt=build_prompt(db.catalog, max_tool_calls), repair=False, domain="analyst"))
    finally:
        con.close()
    accepted = final_result(record)
    if accepted:
        status = "answered"
    elif record["status"] == "completed" and record["answer"]:
        status = "declined"
    else:
        status = "failed"
    result = {"status": status, "request": request, "data": db.catalog["name"], "model": record["model"],
              **({k: accepted[k] for k in ("sql", "columns", "rows", "row_count", "definition", "caveats", "metrics_used")}
                 if accepted else {}),
              "message": record["answer"] if status != "failed" else (record["error"] or f"没有交出结果（状态 {record['status']}）"),
              "sql_runs": sum(s["tool"] == "run_sql" for s in record["steps"]),
              "rejected": sum(not s["ok"] for s in record["steps"]),
              "usage": record["usage"], "seconds": record["seconds"]}
    return {"result": result, "run": record}
