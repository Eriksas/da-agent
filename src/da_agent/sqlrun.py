"""只读 SQL 执行（M19）：查询 Agent 写的 SQL 在这里检查和执行。

模型写的 SQL 不能直接信任。四层限制，每一层都有测试（tests/test_sqlrun.py）：
1. 数据库文件以只读方式打开：建表、删表、插入、更新在引擎层就被拒绝。文件里只有口径配置里声明的表。
2. 关闭外部访问并锁住配置：不能读写磁盘上的其他文件、不能装扩展、不能联网，也不能把这些开关改回来。
3. 执行前检查语句：只能是一条 SELECT（或 WITH … SELECT）；不能调用查看系统信息的函数（会暴露本机路径）。
4. 资源上限：超时中断、返回行数上限、内存和线程上限。

第 1、2 层是 DuckDB 自己保证的；第 3 层是字符串检查，挡的是“没必要让模型用”的东西，不是唯一的防线。
"""

import math
import re
import threading
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

MAX_ROWS = 200  # 一次查询最多返回多少行：再多就该先聚合
TIMEOUT_SECONDS = 20
MAX_SQL_CHARS = 4000
CONFIG = {"enable_external_access": False, "lock_configuration": True, "memory_limit": "2GB", "threads": 2}
LITERAL = re.compile(r"'(?:[^']|'')*'")  # 字符串常量：检查关键字前先去掉，免得把商品名里的词当成函数
COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
STARTS_WITH_QUERY = re.compile(r"^[\s(]*(select|with)\b", re.IGNORECASE)
# 系统信息和读文件的函数、系统表。读文件在第 2 层已经做不到；这里提前拒绝，给模型一个看得懂的理由
FORBIDDEN = re.compile(
    r"\b(pragma_\w*|duckdb_\w*|read_\w+|\w+_scan|sniff_csv|glob|getenv|current_setting|query|query_table)\s*\("
    r"|\b(information_schema|pg_catalog|sqlite_master|sqlite_schema)\b", re.IGNORECASE)


def open_readonly(path: Path) -> duckdb.DuckDBPyConnection:
    """以只读方式打开查询用的数据库文件，并关闭外部访问。同一个文件可以同时开多个连接（评测并行时每次运行一个）。"""
    if not path.exists():
        raise ValueError(f"查询用的数据库不存在：{path}")
    return duckdb.connect(str(path), read_only=True, config=CONFIG)


def check_sql(sql: str) -> str:
    """检查一条 SQL 能不能执行。能就返回去掉末尾分号的 SQL；不能就抛 ValueError，理由是写给模型看的。"""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise ValueError("SQL 是空的")
    if len(text) > MAX_SQL_CHARS:
        raise ValueError(f"SQL 超过 {MAX_SQL_CHARS} 个字符，请简化")
    try:
        statements = duckdb.extract_statements(text)
    except duckdb.Error as exc:
        raise ValueError(f"SQL 语法错误：{str(exc).splitlines()[0][:200]}") from exc
    if len(statements) != 1:
        raise ValueError("一次只能执行一条语句")
    bare = COMMENT.sub(" ", LITERAL.sub("''", text))
    if statements[0].type != duckdb.StatementType.SELECT or not STARTS_WITH_QUERY.match(bare):
        raise ValueError("只能执行以 SELECT 或 WITH 开头的查询，不能修改数据、改设置或执行其他命令")
    found = FORBIDDEN.search(bare)
    if found:
        raise ValueError(f"不能使用 {found.group().rstrip('( ')}：查询只能读数据说明里列出的表")
    return text


def _plain(value: Any) -> Any:
    """把查询结果里的值变成可以写进 JSON 的普通类型。小数保留 6 位，和 calculate 的结果一致。"""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else round(value, 6)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def run_select(con: duckdb.DuckDBPyConnection, sql: str, *, max_rows: int = MAX_ROWS,
               timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """检查并执行一条查询。返回 {sql, columns, rows, row_count, truncated}；任何问题都抛 ValueError。

    rows 最多 max_rows 行；结果更多时 truncated 为 True（row_count 是实际返回的行数，不是总行数）。
    """
    text = check_sql(sql)
    finished = threading.Event()

    def stop() -> None:
        if not finished.is_set():
            con.interrupt()

    timer = threading.Timer(timeout, stop)
    timer.start()
    try:
        cursor = con.execute(text)
        columns = [item[0] for item in cursor.description]
        fetched = cursor.fetchmany(max_rows + 1)
    except duckdb.InterruptException as exc:
        raise ValueError(f"查询超过 {timeout:g} 秒被中断，请缩小范围或先聚合") from exc
    except duckdb.Error as exc:
        raise ValueError(f"SQL 执行出错：{str(exc).strip()[:400]}") from exc
    finally:
        finished.set()
        timer.cancel()
    if len(set(columns)) != len(columns):
        raise ValueError(f"结果里有重名的列 {columns}，请用 AS 给每一列起不同的名字")
    rows = [dict(zip(columns, (_plain(v) for v in row), strict=True)) for row in fetched[:max_rows]]
    return {"sql": text, "columns": columns, "rows": rows, "row_count": len(rows), "truncated": len(fetched) > max_rows}
