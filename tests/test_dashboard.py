"""数据看板的数字：手算的小样例、已提交的 JSON 自洽、和数据质量报告对得上、网页里的数据安全。"""

import json
from pathlib import Path

import pytest

from da_agent import site
from da_agent.cleaning import connect, connect_frame
from da_agent.dashboard import build_dashboard_data, check_dashboard_data, load_dashboard_data
from da_agent.dataset import PARQUET_PATH
from da_agent.paths import REPORTS_DIR
from helpers import S2, UK, make_frame

# 三个月、两个国家。c1 12 月首购、1 月回购；c2 1 月首购（法国）、2 月回购；c3 2 月首购，一张订单从 12:59 录到 13:00；
# 1 月有一张缺客户 ID 的订单和一张取消单。GMV 合计 120，取消 4。
ROWS = [
    (S2, 2, "100001", "10001", "A", 10, "2010-12-06 10:00", 2.0, "c1", UK),         # 20，周一
    (S2, 3, "100002", "10001", "A", 5, "2011-01-10 11:00", 4.0, "c1", UK),          # 20，周一
    (S2, 4, "100003", "10002", "B", 3, "2011-01-12 14:00", 10.0, "c2", "France"),   # 30，周三
    (S2, 5, "100005", "10003", "C", 4, "2011-01-20 15:00", 5.0, None, UK),          # 20，周四，缺客户
    (S2, 6, "C100007", "10001", "A", -1, "2011-01-11 09:30", 4.0, "c1", UK),        # 取消 4
    (S2, 7, "100006", "10004", "D", 2, "2011-02-01 12:59", 5.0, "c3", UK),          # 10，周二
    (S2, 8, "100006", "10003", "C", 1, "2011-02-01 13:00", 10.0, "c3", UK),         # 10，同一张订单
    (S2, 9, "100004", "10002", "B", 1, "2011-02-15 09:00", 10.0, "c2", "France"),   # 10，周二
]


@pytest.fixture(scope="module")
def data() -> dict:
    with connect_frame(make_frame(ROWS)) as con:
        return build_dashboard_data(con)


def test_kpis_match_hand_calculation(data: dict) -> None:
    kpi = data["slices"]["all/all"]["kpi"]
    assert (kpi["gmv"], kpi["cancelled_amount"], kpi["net_sales"], kpi["orders"], kpi["aov"]) == (120, 4, 116, 6, 20)
    assert (kpi["active_customers"], kpi["repeat_rate"]) == (3, 0.6667)  # c1、c2 各 2 单，c3 1 单
    assert (kpi["cancel_rate"], kpi["unidentified_share"]) == (0.0333, 0.1667)


def test_months_split_gmv_into_new_returning_and_unidentified(data: dict) -> None:
    months = {m["month"]: m for m in data["slices"]["all/all"]["months"]}
    assert list(months) == ["2010-12", "2011-01", "2011-02"]
    jan = months["2011-01"]
    assert (jan["gmv"], jan["new_gmv"], jan["returning_gmv"], jan["unidentified_gmv"], jan["net_sales"]) == (70, 30, 20, 20, 66)
    assert (months["2011-02"]["new_gmv"], months["2011-02"]["returning_gmv"]) == (20, 10)
    # 数据从 12 月 6 日开始、2 月 15 日结束：头尾两个月都不完整，1 月完整
    assert [m["partial"] for m in months.values()] == [True, False, True]


def test_cohort_retention(data: dict) -> None:
    cohorts = {c["cohort"]: c for c in data["slices"]["all/all"]["cohorts"]}
    assert cohorts["2010-12"] == {"cohort": "2010-12", "size": 1, "retention": [1.0, 0.0]}  # c1：1 月回来，2 月没有
    assert cohorts["2011-01"]["retention"] == [1.0]  # c2：2 月回来
    assert cohorts["2011-02"]["retention"] == []  # 范围里看不到之后的月份


def test_heatmap_counts_each_order_once_at_its_first_line(data: dict) -> None:
    hours, heat = data["hours"], data["slices"]["all/all"]["heatmap"]
    assert hours == list(range(9, 16))
    cell = lambda weekday, hour: heat[weekday - 1][hours.index(hour)]  # noqa: E731
    assert cell(2, 12) == 1 and cell(2, 13) == 0  # 12:59 到 13:00 的订单只算在 12 点
    assert (cell(1, 10), cell(1, 11), cell(3, 14), cell(4, 15), cell(2, 9)) == (1, 1, 1, 1, 1)
    assert sum(map(sum, heat)) == 6


def test_countries_products_and_cleaning(data: dict) -> None:
    s = data["slices"]["all/all"]
    assert s["countries"]["uk_share"] == 0.6667
    assert [(c["label"], c["gmv"], c["customers"], c["orders"]) for c in s["countries"]["top"]] == [("法国", 40, 1, 2)]
    products = s["products"]
    assert [(p["stock_code"], p["net_sales"]) for p in products["top"]] == [("10002", 40), ("10001", 36), ("10003", 30),
                                                                            ("10004", 10)]
    assert products["pareto"][50] == round(76 / 116, 4) and products["pareto"][100] == 1
    rules = {r["id"]: r["rows"] for r in s["cleaning"]["rules"]}
    assert (s["cleaning"]["raw_rows"], s["cleaning"]["sales_rows"], rules["R04"], rules["R08"]) == (8, 7, 1, 1)


def test_filters_and_year_over_year(data: dict) -> None:
    assert data["slices"]["all/uk"]["kpi"]["gmv"] == 80 and data["slices"]["all/intl"]["kpi"]["gmv"] == 40
    assert data["slices"]["2011/all"]["kpi"]["gmv"] == 100
    assert data["slices"]["2011/all"]["compare"]["orders"] == 0  # 2010 年同期没有数据：对比值为 0，客单价为空
    assert data["slices"]["2011/all"]["compare"]["aov"] is None
    assert data["slices"]["2010/all"]["compare"] is None
    assert check_dashboard_data(data) == []


def test_consistency_check_catches_a_broken_number(data: dict) -> None:
    broken = json.loads(json.dumps(data))
    broken["slices"]["all/uk"]["kpi"]["gmv"] += 1
    problems = check_dashboard_data(broken)
    assert any("all/uk 各月 GMV 之和" in p for p in problems) and any("英国 + 海外的 gmv" in p for p in problems)


def test_committed_data_is_consistent_and_matches_quality_report() -> None:
    """已提交的看板数字（CI 里检查，不需要原始数据）：自洽，且和另一条代码路径算出的数据质量报告一致。"""
    data = load_dashboard_data()
    assert check_dashboard_data(data) == []
    assert len(data["slices"]) == 9
    quality = json.loads((REPORTS_DIR / "data_quality.json").read_text(encoding="utf-8"))
    clean, kpi = quality["clean"], data["slices"]["all/all"]["kpi"]
    assert (kpi["gmv"], kpi["orders"], kpi["active_customers"]) == (clean["gmv"], clean["orders"], clean["customers"])
    assert (kpi["cancelled_amount"], kpi["net_sales"]) == (clean["cancelled_amount"], clean["net_sales"])
    assert kpi["unidentified_share"] == round(clean["gmv_missing_customer_share"], 4)
    cleaning = data["slices"]["all/all"]["cleaning"]
    assert (cleaning["raw_rows"], cleaning["sales_rows"]) == (quality["dataset"]["raw_rows"], clean["sales_rows"])


@pytest.mark.skipif(not PARQUET_PATH.exists(), reason="本地没有准备数据（da-agent prepare-data）")
def test_committed_data_is_up_to_date() -> None:
    """用原始数据重新算一遍，必须和已提交的 JSON 一样（生成日期除外）：防止改了口径却忘了重新生成。"""
    with connect(PARQUET_PATH) as con:
        fresh = build_dashboard_data(con)
    committed = load_dashboard_data()
    fresh["generated_at"] = committed["generated_at"]
    assert json.loads(json.dumps(fresh)) == committed


def test_page_embeds_data_safely(monkeypatch: pytest.MonkeyPatch, data: dict, tmp_path: Path) -> None:
    """商品名来自原始数据：即使里面有 </script>，也不能提前结束页面里的数据块。"""
    evil = json.loads(json.dumps(data))
    evil["slices"]["all/all"]["products"]["top"][0]["description"] = "</script><script>alert(1)</script>"
    monkeypatch.setattr(site, "load_dashboard_data", lambda: evil)
    html = site.dashboard_page()
    block = html.split('<script id="dashboard-data" type="application/json">')[1].split("</script>")[0]
    assert "<" not in block and "alert(1)" in block
    assert json.loads(block)["slices"]["all/all"]["products"]["top"][0]["description"].startswith("</script>")
    assert 'aria-current=page' in html and "数据看板" in html
