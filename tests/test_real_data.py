"""真实数据交叉核对：用 pandas 把清洗规则独立实现一遍，和 SQL 版的结果比对。

两种写法各自独立，同时犯同一个错的概率很低。需要先运行 da-agent prepare-data；
CI 里没有这份数据，测试会自动跳过。
"""

import duckdb
import pandas as pd
import pytest

from da_agent.cleaning import GIFT_VOUCHER_PREFIX, NON_MERCHANDISE, connect
from da_agent.dataset import FIRST_SHEET, PARQUET_PATH, SECOND_SHEET
from da_agent.quality import build_report

pytestmark = pytest.mark.skipif(not PARQUET_PATH.exists(), reason="本地没有准备数据（da-agent prepare-data）")
BUSINESS = ["invoice", "stock_code", "description", "quantity", "invoice_date", "price", "customer_id", "country"]


def pandas_metrics() -> dict[str, float]:
    """与 cleaning.py 相同的规则，用 pandas 独立实现。"""
    df = duckdb.sql(f"SELECT * FROM read_parquet('{PARQUET_PATH.as_posix()}')").df()
    df = df.sort_values(["source_sheet", "source_row"]).reset_index(drop=True)
    df["amount"] = df["quantity"] * df["price"]
    second_start = df.loc[df["source_sheet"] == SECOND_SHEET, "invoice_date"].min()
    overlap = (df["source_sheet"] == FIRST_SHEET) & (df["invoice_date"] >= second_start)
    rest = df[~overlap].copy()
    is_c = rest["invoice"].str.startswith("C")
    duplicate = rest.duplicated(subset=BUSINESS, keep="first")
    valid = ~(rest["invoice"].str.startswith("A") | ((rest["quantity"] <= 0) & ~is_c)
              | (rest["price"] <= 0) | duplicate)
    non_merch = rest["stock_code"].isin(list(NON_MERCHANDISE)) | rest["stock_code"].str.startswith(GIFT_VOUCHER_PREFIX)
    sales = rest[valid & ~is_c & ~non_merch & (rest["quantity"] > 0) & (rest["price"] > 0)]
    cancels = rest[valid & is_c & ~non_merch]
    return {
        "excluded_rows": int(overlap.sum() + (~valid).sum()),
        "sales_rows": len(sales),
        "orders": sales["invoice"].nunique(),
        "customers": sales["customer_id"].nunique(),
        "gmv": round(float(sales["amount"].sum()), 2),
        "cancelled_amount": round(float(-cancels["amount"].sum()), 2),
    }


def test_sql_and_pandas_implementations_agree() -> None:
    with connect(PARQUET_PATH) as con:
        report = build_report(con)
    expected = pandas_metrics()
    actual = {
        "excluded_rows": report["excluded"]["rows"],
        "sales_rows": report["clean"]["sales_rows"],
        "orders": report["clean"]["orders"],
        "customers": report["clean"]["customers"],
        "gmv": report["clean"]["gmv"],
        "cancelled_amount": report["clean"]["cancelled_amount"],
    }
    assert actual == pytest.approx(expected)


def test_row_total_matches_uci_page() -> None:
    """UCI 页面写明 1,067,371 条记录；这个总数包含两个工作表的重叠部分。"""
    count = duckdb.sql(f"SELECT count(*) FROM read_parquet('{PARQUET_PATH.as_posix()}')").fetchone()[0]
    assert count == 1_067_371


def test_weekly_totals_add_up_to_report_totals() -> None:
    """恒等式：各周 GMV 和取消金额相加，等于质量报告里的总数。"""
    from da_agent.metrics import weekly_row

    with connect(PARQUET_PATH) as con:
        report = build_report(con)
        weekly_row(con, "2011-W01")  # 触发建表
        totals = con.execute("SELECT sum(gmv), sum(cancelled_amount), count(*) FROM weekly").fetchone()
    assert totals[0] == pytest.approx(report["clean"]["gmv"])
    assert totals[1] == pytest.approx(report["clean"]["cancelled_amount"])
    assert totals[2] == report["coverage"]["weeks"]


def test_every_week_decomposition_and_drilldown_add_up() -> None:
    """恒等式：每一周的拆解贡献之和、每种下钻的分组之和，都等于总变化。"""
    from da_agent.decompose import decompose_gmv
    from da_agent.metrics import drilldown

    with connect(PARQUET_PATH) as con:
        drilldown(con, "2011-W48")  # 触发建表
        weeks = [row[0] for row in con.execute("SELECT week FROM weekly ORDER BY monday").fetchall()]
        checked = 0
        for week in weeks[1:]:
            try:
                result = decompose_gmv(con, week)
            except ValueError:  # 当期或基期停业，工具拒绝拆解
                continue
            assert result["check"]["contributions_sum_to_total"], week
            checked += 1
        for dimension in ("country", "customer_type", "product"):
            for week in ("2010-W48", "2011-W23", "2011-W49"):
                assert drilldown(con, week, dimension=dimension)["check"]["segments_sum_to_total"], (week, dimension)
    assert checked >= 100


def test_ask_command_runs_on_real_data_with_fake_script(monkeypatch: pytest.MonkeyPatch, tmp_path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    """真实数据 + Agent 循环的完整路径（剧本回放，不调用真实模型）。"""
    from da_agent import cli

    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path)
    assert cli.main(["ask", "2011-W02 的 GMV 为什么变了？", "--llm", "fake", "--script", str(cli.DEMO_SCRIPT)]) == 0
    out = capsys.readouterr().out
    assert "状态：completed" in out and "decompose_gmv" in out
