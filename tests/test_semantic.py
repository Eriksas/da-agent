"""口径配置（M19）：配置里的指标和固定工具（metrics.py）算出来的一样；配置和数据对不上时报错。"""

from pathlib import Path

import duckdb
import pytest
from test_metrics import ROWS

from da_agent import semantic
from da_agent.cleaning import connect, connect_frame
from da_agent.dataset import PARQUET_PATH
from da_agent.metrics import METRICS, _ensure, weekly_row
from da_agent.semantic import (
    SEMANTIC_DIR,
    MetricConfig,
    QueryDB,
    build_csv_database,
    build_database,
    catalog_text,
    load_config,
    metrics_used,
    prepare,
)
from da_agent.sqlrun import run_select
from helpers import make_frame


def _base() -> duckdb.DuckDBPyConnection:
    con = connect_frame(make_frame(ROWS))
    _ensure(con)
    return con


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> QueryDB:
    return build_database(load_config(SEMANTIC_DIR / "retail.yaml"), _base(), tmp_path_factory.mktemp("q") / "retail.duckdb")


def _assert_same_as_fixed_tools(db: QueryDB, base: duckdb.DuckDBPyConnection, weeks: list[str]) -> None:
    con = db.connect()
    shared = [name for name in db.catalog["metrics"] if name in METRICS]
    select = ", ".join(f"{db.catalog['metrics'][name]['sql']} AS {name}" for name in shared)
    for week in weeks:
        row = run_select(con, f"SELECT {select} FROM order_lines WHERE week = '{week}'")["rows"][0]
        expected = weekly_row(base, week)
        for name in shared:
            assert row[name] == pytest.approx(expected[name], abs=1e-5), (week, name)
    con.close()


def test_configured_metrics_equal_the_fixed_tools(db: QueryDB) -> None:
    assert {"gmv", "net_sales", "cancelled_amount", "cancel_rate", "orders", "aov", "active_customers", "new_customers",
            "trading_days"} <= set(db.catalog["metrics"])
    _assert_same_as_fixed_tools(db, _base(), ["2011-W01", "2011-W02"])
    con = db.connect()
    assert run_select(con, "SELECT sum(gmv_amount) AS gmv, sum(net_amount) AS net, count(DISTINCT order_id) AS orders "
                           "FROM order_lines WHERE week = '2011-W02'")["rows"] == [{"gmv": 145.0, "net": 140.0, "orders": 5}]
    con.close()


@pytest.mark.skipif(not PARQUET_PATH.exists(), reason="本地没有准备数据（da-agent prepare-data）")
def test_configured_metrics_equal_the_fixed_tools_on_real_data(tmp_path: Path) -> None:
    base = connect(PARQUET_PATH)
    _ensure(base)
    real = build_database(load_config(SEMANTIC_DIR / "retail.yaml"), base, tmp_path / "retail.duckdb")
    weeks = [row[0] for row in base.execute("SELECT week FROM weekly WHERE orders > 0 ORDER BY week").fetchall()]
    assert len(weeks) > 100
    _assert_same_as_fixed_tools(real, base, weeks)


def test_the_catalog_describes_every_column(db: QueryDB) -> None:
    table = db.catalog["tables"]["order_lines"]
    assert table["rows"] == 11 and all(column["description"] for column in table["columns"])
    by_name = {column["name"]: column for column in table["columns"]}
    assert by_name["line_type"]["values"] == ["销售", "取消"] and by_name["country"]["values"][0] == "United Kingdom"
    assert by_name["order_time"]["range"] == ["2011-01-03 10:00:00", "2011-01-14 11:00:00"]
    assert by_name["order_id"]["missing_share"] == round(1 / 11, 4)  # 取消行没有订单号
    text = catalog_text(db.catalog)
    assert "### 表 order_lines（11 行）" in text and "| GMV（gmv） | order_lines | `sum(gmv_amount)` |" in text
    assert "全部取值：销售、取消" in text and "2011-W49 不是完整的一周" in text
    assert metrics_used(db.catalog, "SELECT SUM(gmv_amount) AS a,\n count(distinct order_id) FROM order_lines") == ["gmv", "orders"]


def test_the_comparison_config_has_the_same_rows_without_ready_made_columns(tmp_path: Path) -> None:
    raw = build_database(load_config(SEMANTIC_DIR / "retail_raw.yaml"), _base(), tmp_path / "raw.duckdb")
    columns = [c["name"] for c in raw.catalog["tables"]["order_lines"]["columns"]]
    assert "gmv_amount" not in columns and "order_id" not in columns and raw.catalog["metrics"] == {}
    assert "指标口径（文字）" in catalog_text(raw.catalog) and "### 指标口径" not in catalog_text(raw.catalog)
    con = raw.connect()
    assert run_select(con, "SELECT sum(amount) FILTER (WHERE line_type = '销售') AS gmv FROM order_lines "
                           "WHERE week = '2011-W02'")["rows"] == [{"gmv": 145.0}]
    con.close()


def test_a_config_that_does_not_match_the_data_is_refused(tmp_path: Path) -> None:
    config = load_config(SEMANTIC_DIR / "retail.yaml")
    table = config.tables["order_lines"]
    undocumented = config.model_copy(update={"tables": {"order_lines": table.model_copy(
        update={"columns": {k: v for k, v in table.columns.items() if k != "week"} | {"ghost": "不存在的列"}})}})
    with pytest.raises(ValueError, match=r"没写说明 \['week'\]，说明里多出 \['ghost'\]"):
        build_database(undocumented, _base(), tmp_path / "a.duckdb")
    broken = config.model_copy(update={"metrics": {"bad": MetricConfig(label="坏指标", table="order_lines", sql="sum(nope)",
                                                                       definition="引用了不存在的列")}})
    with pytest.raises(ValueError, match="指标 bad 的表达式跑不通"):
        build_database(broken, _base(), tmp_path / "b.duckdb")
    with pytest.raises(ValueError, match="表名 Bad-Name"):
        build_database(config.model_copy(update={"tables": {"Bad-Name": table}}), _base(), tmp_path / "c.duckdb")
    assert not (tmp_path / "a.duckdb").exists() and not (tmp_path / "b.duckdb").exists()  # 没通过检查的不会留下可用的数据库


def test_a_csv_needs_no_config(tmp_path: Path) -> None:
    csv = tmp_path / "leads.csv"
    csv.write_text("month,channel,visits,bought\nJan,ads,10,yes\nJan,search,30,no\nFeb,ads,20,no\n", encoding="utf-8")
    db = build_csv_database(csv, tmp_path / "leads.duckdb")
    table = db.catalog["tables"]["data"]
    assert table["rows"] == 3 and [c["name"] for c in table["columns"]] == ["month", "channel", "visits", "bought"]
    assert {c["name"]: c["type"] for c in table["columns"]}["bought"] == "VARCHAR"  # yes / no 不被转成布尔
    assert db.catalog["metrics"] == {} and "（没有说明）" in catalog_text(db.catalog)
    con = db.connect()
    assert run_select(con, "SELECT channel, sum(visits) AS visits FROM data GROUP BY 1 ORDER BY 1")["rows"] == [
        {"channel": "ads", "visits": 30}, {"channel": "search", "visits": 30}]
    with pytest.raises(duckdb.Error):
        con.execute("SELECT * FROM read_csv(?)", [csv.as_posix()])  # 建好之后，连原来的 CSV 也读不了
    con.close()
    with pytest.raises(ValueError, match="找不到 CSV"):
        build_csv_database(tmp_path / "nope.csv", tmp_path / "x.duckdb")


def test_prepared_data_is_reused_until_the_source_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setattr(semantic, "QUERY_DB_DIR", tmp_path / "query")
    csv = tmp_path / "leads.csv"
    csv.write_text("channel,visits\nads,10\n", encoding="utf-8")
    first = prepare(str(csv))
    built_at = first.path.stat().st_mtime_ns
    assert prepare(str(csv)).path == first.path and first.path.stat().st_mtime_ns == built_at  # 没变就复用
    csv.write_text("channel,visits\nads,10\nsearch,5\n", encoding="utf-8")
    os.utime(csv, (first.path.stat().st_mtime + 5, first.path.stat().st_mtime + 5))
    assert prepare(str(csv)).catalog["tables"]["data"]["rows"] == 2  # 数据更新后重建
    with pytest.raises(ValueError, match="没有名为 nope 的口径配置"):
        prepare("nope")
