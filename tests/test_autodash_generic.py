"""对任何数据都起作用的改进（M13c、M13d）：分类的先后、比率带分子分母、一行代表什么、整数取值、月份名称的出处。

用一份没有任何说明的小 CSV：程序只能看取值本身。
"""

from pathlib import Path

import pytest

from da_agent.autodash.compile import (DashboardProblems, _calendar, compile_dashboard, preview_chart,
                                       ungrounded_numbers)
from da_agent.autodash.sources import connect_source, profile_table
from da_agent.autodash.spec import Chart, DashboardSpec, Metric

CSV = """month;day_of_week;education;y;duration;traffic
mar;mon;illiterate;yes;100;2
mar;tue;basic.4y;no;200;1
may;mon;basic.4y;no;300;2
may;wed;high.school;yes;400;3
may;wed;high.school;no;150;2
jun;tue;university.degree;yes;250;13
jun;fri;university.degree;no;120;1
dec;fri;high.school;yes;330;2
dec;mon;basic.4y;no;90;3
jun;tue;university.degree;yes;250;13
"""

METRICS = [
    {"id": "subs", "label": "订阅数", "agg": "count", "where": {"y": ["yes"]}},
    {"id": "records", "label": "记录数", "agg": "count"},
    {"id": "rate", "label": "转化率", "agg": "ratio", "left": "subs", "right": "records", "format": "percent"},
]


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    path = tmp_path / "campaign.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


def _preview(path: Path, chart: dict) -> dict:
    with connect_source(str(path)) as con:
        return preview_chart(con, [Metric.model_validate(m) for m in METRICS],
                             Chart.model_validate({"id": "c", "title": "测试图", "why": "测试", **chart}))


def test_month_and_weekday_names_sort_by_calendar(csv_path: Path) -> None:
    by_month = _preview(csv_path, {"type": "column", "metrics": ["records"], "x": {"column": "month"}})
    assert [r["x"] for r in by_month["rows"]] == ["mar", "may", "jun", "dec"]  # 不是 dec、jun、mar、may
    with connect_source(str(csv_path)) as con:
        out = compile_dashboard(con, DashboardSpec.model_validate({
            "title": "营销看板", "goal": "看整体转化", "metrics": METRICS,
            "charts": [{"id": "k", "title": "转化率", "type": "kpi", "metrics": ["rate"], "why": "整体"},
                       {"id": "h", "title": "月份 × 星期", "type": "heatmap", "metrics": ["records"],
                        "x": {"column": "month"}, "series": {"column": "day_of_week"}, "why": "分布"}]}))
    heat = out["slices"]["all"]["h"]
    assert heat["series_order"] == ["mon", "tue", "wed", "fri"]
    assert list(dict.fromkeys(r["x"] for r in heat["rows"])) == ["mar", "may", "jun", "dec"]


def test_calendar_needs_every_value_to_be_a_month_or_weekday() -> None:
    assert _calendar(["Jan", "march", "Sept", "12月", "十一月"]) == {"Jan": 0, "march": 2, "Sept": 8, "12月": 11,
                                                                 "十一月": 10}
    assert _calendar(["周日", "Mon", "星期三"]) == {"周日": 6, "Mon": 0, "星期三": 2}
    assert _calendar(["may", "maybe"]) is None  # 有一个认不出就不按日历排
    assert _calendar(["mon", "jan"]) is None  # 月份和星期混在一起也不算


def test_order_puts_listed_values_first(csv_path: Path) -> None:
    table = _preview(csv_path, {"type": "table", "metrics": ["rate"],
                                "x": {"column": "education", "order": ["illiterate", "basic.4y", "high.school"]}})
    assert [r["x"] for r in table["rows"]] == ["illiterate", "basic.4y", "high.school", "university.degree"]
    with pytest.raises(DashboardProblems, match="order 里 education 没有这些取值"):
        _preview(csv_path, {"type": "table", "metrics": ["rate"], "x": {"column": "education", "order": ["phd"]}})


def test_order_beats_the_calendar_and_keeps_the_merged_group(csv_path: Path) -> None:
    rows = _preview(csv_path, {"type": "column", "metrics": ["records"],
                               "x": {"column": "month", "keep": ["dec", "mar"], "other_label": "其余月份",
                                     "order": ["其余月份", "dec"]}})["rows"]
    assert [r["x"] for r in rows] == ["其余月份", "dec", "mar"]


def test_ratio_rows_and_kpis_carry_numerator_and_denominator(csv_path: Path) -> None:
    bar = _preview(csv_path, {"type": "bar", "metrics": ["rate"], "x": {"column": "education"}})
    rows = {r["x"]: r for r in bar["rows"]}
    assert (rows["high.school"]["values"][0], rows["high.school"]["numerator"], rows["high.school"]["denominator"]) \
        == (pytest.approx(2 / 3, abs=1e-4), 2, 3)
    assert rows["illiterate"]["numerator"] == 1 and rows["illiterate"]["denominator"] == 1
    plain = _preview(csv_path, {"type": "bar", "metrics": ["records"], "x": {"column": "education"}})
    assert all("numerator" not in r for r in plain["rows"])  # 不是比率就不带
    spec = DashboardSpec.model_validate({
        "title": "营销看板", "goal": "看整体转化", "metrics": METRICS,
        "charts": [{"id": "k", "title": "转化率", "type": "kpi", "metrics": ["rate"], "why": "整体",
                    "insight": "整体转化率 50.0%（5 / 10）"},
                   {"id": "e", "title": "各学历", "type": "bar", "metrics": ["rate"], "x": {"column": "education"},
                    "why": "客群", "insight": "high.school 为 66.7%（2 / 3）"}]})
    with connect_source(str(csv_path)) as con:
        out = compile_dashboard(con, spec)  # 结论里引用分子分母不再被退回
    assert out["slices"]["all"]["k"]["facts"] == {"value": 0.5, "numerator": 5, "denominator": 10}
    assert out["charts"][0]["ratio_parts"] == ["subs", "records"]


def test_profile_says_when_the_row_grain_is_unknown(csv_path: Path, tmp_path: Path) -> None:
    with connect_source(str(csv_path)) as con:
        identity = profile_table(con)["row_identity"]
    assert identity["unique_columns"] == [] and identity["duplicate_rows"] == 1
    assert "记录数" in identity["note"] and "1 行和其他行完全相同" in identity["note"]
    with_id = tmp_path / "with_id.csv"
    with_id.write_text("id;y\n1;yes\n2;no\n3;no\n", encoding="utf-8")
    with connect_source(str(with_id)) as con:
        identity = profile_table(con)["row_identity"]
    assert identity["unique_columns"] == ["id"] and identity["duplicate_rows"] == 0
    assert "唯一标识" in identity["note"] and "记录数" not in identity["note"]


def test_values_of_integer_coded_columns_can_be_written_as_numbers(csv_path: Path) -> None:
    """流量来源这类整数编码的列，模型会写 keep: [2, 13]、where: {"traffic": [2]}（Online Shoppers 盲测里 4 次被退回）。"""
    rows = _preview(csv_path, {"type": "bar", "metrics": ["records"],
                               "x": {"column": "traffic", "keep": [2, 13], "other_label": "其他来源"}})["rows"]
    assert {r["x"]: r["values"][0] for r in rows} == {"2": 4, "13": 2, "其他来源": 4}
    with connect_source(str(csv_path)) as con:
        kpi = preview_chart(con, [Metric.model_validate({"id": "t2", "label": "来源 2", "agg": "count",
                                                          "where": {"traffic": [2]}})],
                            Chart.model_validate({"id": "k", "title": "来源 2 的记录", "type": "kpi", "metrics": ["t2"],
                                                  "why": "测试"}))
    assert kpi["facts"]["value"] == 4
    with pytest.raises(ValueError, match="valid string"):  # 真假值不转：转成 True 也对不上数据里的写法
        Chart.model_validate({"id": "c", "title": "测试图", "type": "bar", "metrics": ["records"], "why": "测试",
                              "x": {"column": "traffic", "keep": [True]}})


def test_month_numbers_are_grounded_by_month_name_labels() -> None:
    """标签是 Nov 时，结论写“11 月”也有出处（Online Shoppers 盲测 3 次都因此被退回）；没有这个月份就仍然退回。"""
    data = {"rows": [{"x": "Nov", "values": [0.2535]}, {"x": "December", "values": [0.1251]},
                     {"x": "十月", "values": [0.2095]}]}
    assert ungrounded_numbers("11 月最高 25.4%，12 月 12.5%，10 月 21.0%", data) == []
    assert ungrounded_numbers("11 月最高", {"rows": [{"x": "Oct", "values": [0.2]}, {"x": "Dec", "values": [0.1]}]}) \
        == ["11"]  # ≤ 10 的整数本来就不核查，所以用 11 月检验
    assert ungrounded_numbers("11 月", {"rows": [{"x": "Novak", "values": [1]}]}) == ["11"]  # 只认整个标签是月份


def test_filter_buttons_follow_the_same_order_as_the_charts(csv_path: Path) -> None:
    """筛选器按钮以前按字母排（fri、mon、thu……，M13e 复核发现）；现在和图一样：日历顺序、模型给的 order、“其他”放最后。"""
    with connect_source(str(csv_path)) as con:
        out = compile_dashboard(con, DashboardSpec.model_validate({
            "title": "营销看板", "goal": "看整体转化", "metrics": METRICS,
            "charts": [{"id": "k", "title": "转化率", "type": "kpi", "metrics": ["rate"], "why": "整体"},
                       {"id": "k2", "title": "记录数", "type": "kpi", "metrics": ["records"], "why": "规模"}],
            "filters": [{"column": "day_of_week"},
                        {"column": "education", "keep": ["university.degree", "basic.4y"], "other_label": "Other"}]}))
    labels = [[o["label"] for o in f["options"]] for f in out["filters"]]
    assert labels[0] == ["全部", "mon", "tue", "wed", "fri"]
    assert labels[1] == ["全部", "basic.4y", "university.degree", "Other"]  # Other 按字母本该排第一
