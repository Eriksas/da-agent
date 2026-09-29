"""清洗规则与质量报告：用手算的小样例验证每条规则和清洗后口径。

样例共 18 行，每行的预期结果写在行尾注释里。
"""

import pandas as pd
import pytest

from da_agent.cleaning import connect_frame
from da_agent.dataset import FIRST_SHEET, SECOND_SHEET
from da_agent.quality import build_report, render_markdown

S1, S2 = FIRST_SHEET, SECOND_SHEET
UK = "United Kingdom"
ROWS = [
    # 工作表, 行号, 发票, 编码, 描述, 数量, 时间, 单价, 客户, 国家
    (S1, 2, "100001", "10001", "A", 2, "2010-11-30 10:00", 5.0, "c1", UK),         # 销售 10
    (S1, 3, "100002", "10002", "B", 1, "2010-11-30 11:00", 3.0, None, UK),         # 销售 3，缺客户（R08）
    (S1, 4, "100010", "10003", "C", 4, "2010-12-02 09:00", 1.0, "c2", "France"),   # 重叠副本（R01），删除
    (S2, 2, "100011", "POST", "POSTAGE", 1, "2010-12-01 09:00", 18.0, "c1", UK),   # 运费（R03）
    (S2, 3, "100010", "10003", "C", 4, "2010-12-02 09:00", 1.0, "c2", "France"),   # 销售 4（第 2 表那份）
    (S2, 4, "C100012", "10001", "A", -1, "2010-12-03 09:00", 5.0, "c1", UK),       # 取消 5（R04）
    (S2, 5, "A100013", "B", "Adjust bad debt", 1, "2010-12-03 10:00", -100.0, None, UK),  # R02 R03 R06 R08
    (S2, 6, "100014", "10004", "damaged", -3, "2010-12-03 11:00", 0.0, None, UK),  # R05 R06 R08
    (S2, 7, "100015", "10005", "E", 2, "2010-12-04 09:00", 0.0, "c3", UK),         # 单价 0（R06）
    (S2, 8, "100016", "10006", "F", 1, "2010-12-05 09:00", 7.0, "c3", UK),         # 销售 7
    (S2, 9, "100016", "10006", "F", 1, "2010-12-05 09:00", 7.0, "c3", UK),         # 完全重复（R07）
    (S2, 10, "100017", "10007", "G", 5000, "2010-12-06 09:00", 3.0, "c4", UK),     # 销售 15000，极端（R09）
    (S2, 11, "100018", "DCGS0058", "GUM", 2, "2010-12-06 10:00", 1.0, "c5", "Unspecified"),  # 销售 2（非标准编码但是商品），R10
    (S2, 12, "100019", "TEST001", "test", 1, "2010-12-06 11:00", 4.5, "c5", UK),   # 测试记录（R03）
    (S2, 13, "100020", "gift_0001_10", "voucher", 1, "2010-12-06 12:00", 8.33, "c5", UK),  # 礼品卡（R03）
    (S2, 14, "C100021", "10007", "G", -5000, "2010-12-06 13:00", 3.0, "c4", UK),   # 取消 15000（R04 R09），等量取消第 12 行
    (S2, 15, "100022", "10008", "H", 1, "2010-12-07 09:00", 2.0, None, UK),        # 销售 2，缺客户（R08）
    (S2, 16, "100022", "10008", "H", 1, "2010-12-07 09:00", 2.0, None, UK),        # 缺客户的完全重复（R07 R08）
]


def make_frame(rows: list[tuple]) -> pd.DataFrame:
    """把样例行转成和 dataset.normalize 输出一致的 DataFrame。"""
    columns = ["source_sheet", "source_row", "invoice", "stock_code", "description", "quantity",
               "invoice_date", "price", "customer_id", "country"]
    frame = pd.DataFrame(rows, columns=columns)
    return frame.astype({"quantity": "int64", "price": "float64", "source_row": "int64",
                         "description": "string", "customer_id": "string", "country": "string"}).assign(
        invoice_date=pd.to_datetime(frame["invoice_date"]))


@pytest.fixture(scope="module")
def report() -> dict:
    with connect_frame(make_frame(ROWS)) as con:
        return build_report(con)


EXPECTED_RULES = {  # 规则: (命中行数, 金额)，逐行手算
    "R01": (1, 4.0),
    "R02": (1, -100.0),
    "R03": (4, 18.0 - 100.0 + 4.5 + 8.33),
    "R04": (2, -5.0 - 15000.0),
    "R05": (1, 0.0),
    "R06": (3, -100.0),
    "R07": (2, 7.0 + 2.0),
    "R08": (5, 3.0 - 100.0 + 0.0 + 2.0 + 2.0),
    "R09": (2, 0.0),
    "R10": (1, 2.0),
}


@pytest.mark.parametrize("rule_id", sorted(EXPECTED_RULES))
def test_each_rule_hits_expected_rows(report: dict, rule_id: str) -> None:
    rule = next(r for r in report["rules"] if r["id"] == rule_id)
    rows, amount = EXPECTED_RULES[rule_id]
    assert rule["rows"] == rows
    assert rule["amount"] == pytest.approx(amount)


def test_clean_metrics_match_hand_calculation(report: dict) -> None:
    clean = report["clean"]
    assert report["excluded"]["rows"] == 6  # 第 3、7、8、9、11、18 行
    assert clean["sales_rows"] == 7
    assert clean["gmv"] == pytest.approx(10 + 3 + 4 + 7 + 15000 + 2 + 2)
    assert clean["orders"] == 7
    assert clean["customers"] == 5  # c1–c5，缺失不计
    assert clean["gmv_missing_customer"] == pytest.approx(3 + 2)
    assert clean["cancelled_amount"] == pytest.approx(5 + 15000)
    assert clean["net_sales"] == pytest.approx(15028 - 15005)


def test_overlap_is_verified_identical(report: dict) -> None:
    assert report["checks"]["sheet_overlap"] == {
        "overlap_rows": 1, "only_in_first": 0, "only_in_second": 0, "identical": True}


def test_overlap_mismatch_is_reported() -> None:
    rows = list(ROWS)
    rows[4] = rows[4][:7] + (1.5,) + rows[4][8:]  # 第 2 表那份改了单价，两份不再相同
    with connect_frame(make_frame(rows)) as con:
        overlap = build_report(con)["checks"]["sheet_overlap"]
    assert overlap["identical"] is False
    assert (overlap["only_in_first"], overlap["only_in_second"]) == (1, 1)


def test_non_merchandise_grouped_by_category(report: dict) -> None:
    by_category = {row["category"]: row["amount"] for row in report["non_merchandise"]}
    assert by_category == {"运费与手续费": 18.0, "测试记录": 4.5, "礼品卡": 8.33}  # 第 7 行已被 R02 删除，不计入


def test_extreme_lines_and_cancellation_match(report: dict) -> None:
    lines = {row["invoice"]: row for row in report["extreme_lines"]}
    assert set(lines) == {"100017", "C100021"}
    assert lines["100017"]["cancelled_within_1d"] is True
    assert lines["100017"]["offset_by_manual_within_1d"] is False


def test_manual_offset_is_not_mistaken_for_cancellation() -> None:
    """真实数据里遇到过：大额行被人工调整（M）冲销，同客户另一款商品恰好等量取消。"""
    rows = [
        (S2, 2, "200001", "22502", "BASKET 60 PIECES", 60, "2011-06-10 15:28", 649.5, "c9", UK),
        (S2, 3, "C200002", "M", "Manual", -1, "2011-06-10 15:31", 38970.0, "c9", UK),
        (S2, 4, "200003", "22502", "BASKET SMALL", 60, "2011-06-10 15:22", 4.95, "c9", UK),
        (S2, 5, "C200004", "22502", "BASKET SMALL", -60, "2011-06-10 15:39", 4.95, "c9", UK),
    ]
    with connect_frame(make_frame(rows)) as con:
        line = build_report(con)["extreme_lines"][0]
    assert line["invoice"] == "200001"
    assert line["cancelled_within_1d"] is False  # 单价不同，不是这笔的取消
    assert line["offset_by_manual_within_1d"] is True


def test_calendar_weeks_and_partial_weeks(report: dict) -> None:
    coverage = report["coverage"]
    assert coverage["weeks"] == 2  # 2010-W48、2010-W49
    assert coverage["partial_weeks"] == ["2010-W48", "2010-W49"]  # 首日周二、末日周二


def test_markdown_contains_every_rule(report: dict) -> None:
    markdown = render_markdown(report)
    for rule_id in EXPECTED_RULES:
        assert f"| {rule_id} |" in markdown
