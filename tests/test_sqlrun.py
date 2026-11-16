"""只读 SQL 执行（M19）：四层限制各自管用。重点是“绕过字符串检查也改不了数据、读不了文件”。"""

from pathlib import Path

import duckdb
import pytest

from da_agent.sqlrun import check_sql, open_readonly, run_select


@pytest.fixture
def con(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    path = tmp_path / "q.duckdb"
    writer = duckdb.connect(str(path))
    writer.execute("CREATE TABLE t AS SELECT range AS id, range % 3 AS g, range * 1.5 AS amount, "
                   "DATE '2011-01-01' + CAST(range % 5 AS INTEGER) AS day FROM range(1000)")
    writer.close()
    connection = open_readonly(path)
    yield connection
    connection.close()


@pytest.mark.parametrize("sql", [
    "SELECT g, sum(amount) AS total FROM t GROUP BY g",
    "  with a as (select * from t) select count(*) as n from a ;",
    "(SELECT 1 AS x) UNION ALL (SELECT 2)",
    "SELECT 'DROP TABLE t; read_csv(' AS text",  # 字符串常量里的词不算
    "SELECT 1 AS x -- 末尾的注释",
])
def test_queries_are_accepted(sql: str) -> None:
    assert check_sql(sql)


@pytest.mark.parametrize(("sql", "reason"), [
    ("", "空的"),
    ("DROP TABLE t", "只能执行以 SELECT 或 WITH 开头"),
    ("INSERT INTO t SELECT * FROM t", "只能执行以 SELECT 或 WITH 开头"),
    ("CREATE TEMP TABLE z AS SELECT 1", "只能执行以 SELECT 或 WITH 开头"),
    ("COPY t TO 'out.csv'", "只能执行以 SELECT 或 WITH 开头"),
    ("ATTACH 'other.db' AS o", "只能执行以 SELECT 或 WITH 开头"),
    ("SET threads = 8", "只能执行以 SELECT 或 WITH 开头"),
    ("PRAGMA database_list", "只能执行以 SELECT 或 WITH 开头"),  # DuckDB 把它归为 SELECT 类语句，要靠开头的检查拦
    ("SELECT 1; DROP TABLE t", "一次只能执行一条"),
    ("SELECT * FROM read_csv('C:/Windows/win.ini')", "不能使用 read_csv"),
    ("SELECT * FROM pragma_database_list()", "不能使用 pragma_database_list"),  # 会暴露数据库文件在本机的路径
    ("SELECT * FROM duckdb_settings()", "不能使用 duckdb_settings"),
    ("SELECT current_setting('threads')", "不能使用 current_setting"),
    ("SELECT * FROM information_schema.tables", "不能使用 information_schema"),
    ("SELECT * FROM query('SELECT 1')", "不能使用 query"),
    ("SELECT * FROM parquet_scan('x.parquet')", "不能使用 parquet_scan"),
    ("SELEC 1", "语法错误"),
    ("SELECT " + "1 + " * 2000 + "1", "超过"),
])
def test_everything_else_is_refused_with_a_reason(sql: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        check_sql(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE t", "DELETE FROM t", "UPDATE t SET amount = 0", "INSERT INTO t SELECT * FROM t", "CREATE TABLE z AS SELECT 1",
    "CREATE VIEW v AS SELECT 1", "SELECT * FROM read_csv('C:/Windows/win.ini')", "SELECT * FROM read_text('C:/Windows/win.ini')",
    "COPY t TO 'stolen.csv'", "ATTACH 'other.duckdb' AS o", "INSTALL httpfs", "SET enable_external_access = true",
    "RESET enable_external_access", "SET lock_configuration = false", "EXPORT DATABASE 'dump'",
])
def test_the_engine_itself_refuses_even_without_the_text_check(con: duckdb.DuckDBPyConnection, sql: str, tmp_path: Path) -> None:
    """不经过 check_sql，直接把语句交给连接：只读数据库和关闭外部访问仍然把它挡住。"""
    with pytest.raises(duckdb.Error):
        con.execute(sql).fetchall()
    assert con.execute("SELECT count(*) FROM t").fetchone() == (1000,)
    assert not list(tmp_path.glob("stolen*")) and not list(tmp_path.glob("dump*"))


def test_results_are_plain_values(con: duckdb.DuckDBPyConnection) -> None:
    result = run_select(con, "SELECT g, sum(amount) AS total, CAST(sum(amount) AS DECIMAL(18, 2)) AS money, min(day) AS first_day, "
                             "1.0 / 3 AS third, CAST('nan' AS DOUBLE) AS broken, NULL AS empty FROM t GROUP BY g ORDER BY g;")
    assert result["columns"] == ["g", "total", "money", "first_day", "third", "broken", "empty"]
    assert result["rows"][0] == {"g": 0, "total": 250249.5, "money": 250249.5, "first_day": "2011-01-01", "third": 0.333333,
                                 "broken": None, "empty": None}
    assert result["row_count"] == 3 and not result["truncated"] and not result["sql"].endswith(";")


def test_row_limit_and_duplicate_columns(con: duckdb.DuckDBPyConnection) -> None:
    capped = run_select(con, "SELECT id FROM t ORDER BY id", max_rows=10)
    assert capped["row_count"] == 10 and capped["truncated"] and capped["rows"][-1] == {"id": 9}
    exact = run_select(con, "SELECT id FROM t ORDER BY id LIMIT 10", max_rows=10)
    assert exact["row_count"] == 10 and not exact["truncated"]
    with pytest.raises(ValueError, match="重名的列"):
        run_select(con, "SELECT g, g FROM t")


def test_errors_and_timeouts_come_back_as_reasons(con: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ValueError, match="SQL 执行出错.*no_such_column"):
        run_select(con, "SELECT no_such_column FROM t")
    with pytest.raises(ValueError, match="被中断"):
        run_select(con, "SELECT count(*) FROM range(100000000000) a, range(1000) b", timeout=0.5)
    assert run_select(con, "SELECT count(*) AS n FROM t")["rows"] == [{"n": 1000}]  # 中断之后连接还能用


def test_a_missing_database_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        open_readonly(tmp_path / "nope.duckdb")
