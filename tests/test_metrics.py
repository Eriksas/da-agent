"""周度指标与下钻：两周的手算小样例。

2011-W01（1 月 3 日–9 日）：c1 20、c2 30、c3 50、缺客户 40 → GMV 140，4 单，3 个客户（都是新客）
2011-W02（1 月 10 日–16 日）：c1 25、c2 10 + 20、c4 60（新客）、缺客户 30 → GMV 145，5 单；c3 取消 5
数据止于 2011-01-14（周五），所以 W02 是不完整周。
"""

import pytest

from da_agent.cleaning import connect_frame
from da_agent.metrics import drilldown, metric_summary, week_warnings, weekly_row
from helpers import S2, UK, make_frame

ROWS = [
    (S2, 2, "300001", "10001", "A", 10, "2011-01-03 10:00", 1.0, "c1", UK),         # 同一单两行，共 20
    (S2, 3, "300001", "10002", "B", 5, "2011-01-03 10:00", 2.0, "c1", UK),
    (S2, 4, "300002", "10003", "C", 3, "2011-01-04 10:00", 10.0, "c2", "France"),  # 30
    (S2, 5, "300003", "10001", "A", 50, "2011-01-05 10:00", 1.0, "c3", UK),        # 50
    (S2, 6, "300004", "10004", "D", 4, "2011-01-06 10:00", 10.0, None, UK),        # 40，缺客户
    (S2, 7, "300005", "10001", "A", 25, "2011-01-10 10:00", 1.0, "c1", UK),        # 25
    (S2, 8, "300006", "10003", "C", 1, "2011-01-11 10:00", 10.0, "c2", "France"),  # 10
    (S2, 9, "300007", "10003", "C", 2, "2011-01-12 10:00", 10.0, "c2", "France"),  # 20
    (S2, 10, "300008", "10005", "E", 6, "2011-01-13 10:00", 10.0, "c4", "Germany"),  # 60，新客
    (S2, 11, "300009", "10004", "D", 3, "2011-01-14 10:00", 10.0, None, UK),       # 30，缺客户
    (S2, 12, "C300010", "10001", "A", -5, "2011-01-14 11:00", 1.0, "c3", UK),      # 取消 5
]


@pytest.fixture(scope="module")
def con():
    connection = connect_frame(make_frame(ROWS))
    yield connection
    connection.close()


def test_weekly_rows_match_hand_calculation(con) -> None:
    w1, w2 = weekly_row(con, "2011-W01"), weekly_row(con, "2011-W02")
    assert (w1["gmv"], w1["orders"], w1["active_customers"], w1["new_customers"]) == (140, 4, 3, 3)
    assert (w2["gmv"], w2["orders"], w2["active_customers"], w2["new_customers"]) == (145, 5, 3, 1)
    assert w2["returning_customers"] == 2
    assert w2["cancelled_amount"] == pytest.approx(5)
    assert w2["net_sales"] == pytest.approx(140)
    assert w2["aov"] == pytest.approx(29)
    assert w2["orders_per_customer"] == pytest.approx(4 / 3)
    assert w2["identified_aov"] == pytest.approx(115 / 4)
    assert w2["unidentified_gmv"] == pytest.approx(30)
    assert (w1["trading_days"], w2["trading_days"]) == (4, 5)


def test_metric_summary_changes(con) -> None:
    result = metric_summary(con, "2011-W02")
    rows = {row["metric"]: row for row in result["metrics"]}
    assert result["period"] == {"current": "2011-W02", "base": "2011-W01", "compare": "wow", "compare_label": "环比（上一周）"}
    assert (rows["gmv"]["change"], rows["gmv"]["change_pct"]) == (5.0, round(5 / 140, 4))
    assert (rows["aov"]["change"], rows["aov"]["change_pct"]) == (-6.0, round(-6 / 35, 4))
    assert rows["returning_customers"]["change_pct"] is None  # 基期为 0，不计算变化百分比
    assert rows["cancel_rate"]["change"] == round(5 / 145, 4)  # 比率类：百分点变化
    assert rows["cancel_rate"]["change_pct"] is None
    assert result["sample_size"] == {"current_orders": 5, "base_orders": 4}


def test_metric_summary_warns_about_partial_week_and_new_customers(con) -> None:
    warnings = metric_summary(con, "2011-W02")["warnings"]
    assert any("2011-W02 是不完整周" in w and "周五" in w for w in warnings)
    assert any("部分老客会被算成新客" in w for w in warnings)


def test_drilldown_by_country(con) -> None:
    result = drilldown(con, "2011-W02", dimension="country")
    segments = {s["segment"]: s for s in result["segments"]}
    assert [s["segment"] for s in result["segments"]] == ["Germany", "United Kingdom", "France"]  # 按变化绝对值排序
    assert (segments["United Kingdom"]["base"], segments["United Kingdom"]["current"]) == (110.0, 55.0)
    assert segments["Germany"]["share_of_total_change"] == 12.0  # 60 ÷ 5
    assert segments["Germany"]["change_pct"] is None  # 基期为 0
    assert result["total"] == {"base": 140.0, "current": 145.0, "change": 5.0}
    assert result["check"]["segments_sum_to_total"] is True
    assert any("超过 100%" in w for w in result["warnings"])


def test_drilldown_net_sales_with_zero_total_change(con) -> None:
    result = drilldown(con, "2011-W02", metric="net_sales", dimension="country")
    segments = {s["segment"]: s for s in result["segments"]}
    assert segments["United Kingdom"]["change"] == -60.0  # 110 → 55 − 取消 5
    assert result["total"]["change"] == 0.0
    assert segments["Germany"]["share_of_total_change"] is None  # 总变化为 0，不计算占比


def test_drilldown_by_customer_type(con) -> None:
    result = drilldown(con, "2011-W02", dimension="customer_type")
    changes = {s["segment"]: s["change"] for s in result["segments"]}
    assert changes == {"老客": 55.0, "新客": -40.0, "未知": -10.0}


def test_drilldown_top_n_and_others(con) -> None:
    result = drilldown(con, "2011-W02", dimension="product", top_n=2)
    assert [(s["segment"], s["change"]) for s in result["segments"]] == [("10005", 60.0), ("10001", -35.0)]
    assert result["segments"][0]["description"] == "E"
    assert result["others"] == {"segments": 3, "base": 80.0, "current": 60.0, "change": -20.0,
                                "share_of_total_change": -4.0}


@pytest.mark.parametrize("kwargs", [{"metric": "orders"}, {"dimension": "city"}, {"top_n": 0}])
def test_drilldown_rejects_bad_arguments(con, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        drilldown(con, "2011-W02", **kwargs)


def test_week_outside_data_is_rejected(con) -> None:
    with pytest.raises(ValueError, match="不在数据范围内"):
        weekly_row(con, "2011-W10")


def test_closed_week_appears_with_zero_and_warning() -> None:
    rows = [ROWS[0], (S2, 3, "300011", "10001", "A", 1, "2011-01-17 10:00", 1.0, "c1", UK)]  # W01 和 W03，W02 停业
    with connect_frame(make_frame(rows)) as con:
        assert weekly_row(con, "2011-W02")["gmv"] == 0
        warnings = metric_summary(con, "2011-W03")["warnings"]
    assert any("基期 2011-W02 整周没有交易" in w for w in warnings)


def test_extreme_line_is_flagged_in_week_warnings() -> None:
    rows = [ROWS[0],
            (S2, 3, "400001", "10007", "BIG ORDER", 5000, "2011-01-04 09:00", 3.0, "c4", UK),
            (S2, 4, "C400002", "10007", "BIG ORDER", -5000, "2011-01-04 13:00", 3.0, "c4", UK)]
    with connect_frame(make_frame(rows)) as con:
        warnings = week_warnings(con, "2011-W01")
    assert any("400001" in w and "24 小时内被等量取消" in w for w in warnings)


def test_results_are_json_serializable(con) -> None:
    """工具结果要能直接转成 JSON 交给模型（M3）。"""
    import json

    from da_agent.decompose import decompose_gmv

    json.dumps(metric_summary(con, "2011-W02"), ensure_ascii=False)
    json.dumps(drilldown(con, "2011-W02", dimension="product"), ensure_ascii=False)
    json.dumps(decompose_gmv(con, "2011-W02"), ensure_ascii=False)
