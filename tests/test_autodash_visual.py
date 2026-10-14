"""看板的表达能力（M13e）：数值分段、散点图、参考线、重点、分区和宽窄、0–1 均值用百分比。

用一份手算得出每个数的小 CSV：
pages 按 [1, 6, 21] 分段 → 0（1 行）、1–5（4 行）、6–20（2 行）、≥21（3 行）；bought = yes 共 4 行，整体 40%。
"""

from pathlib import Path

import pytest

from da_agent.autodash.compile import DashboardProblems, bin_labels, compile_dashboard, preview_chart
from da_agent.autodash.sources import column_types, connect_source, source_of
from da_agent.autodash.spec import Chart, DashboardSpec, Metric, check_spec

CSV = """segment;pages;bounce;bought;amount
a;0;0.2;no;0
a;3;0.1;no;0
a;8;0.05;yes;30
b;12;0.0;yes;50
b;25;0.02;yes;80
b;2;0.15;no;0
c;40;0.01;yes;120
c;5;0.08;no;0
c;30;0.03;no;0
d;1;0.2;no;0
"""

METRICS = [
    {"id": "buys", "label": "购买数", "agg": "count", "where": {"bought": ["yes"]}},
    {"id": "records", "label": "记录数", "agg": "count"},
    {"id": "rate", "label": "购买率", "agg": "ratio", "left": "buys", "right": "records", "format": "percent"},
    {"id": "bounce", "label": "平均跳出率", "agg": "avg", "column": "bounce", "format": "percent"},
]
KPI = {"id": "k", "title": "购买率", "type": "kpi", "metrics": ["rate"], "why": "整体"}
BOUNCE = {"id": "kb", "title": "平均跳出率", "type": "kpi", "metrics": ["bounce"], "why": "访问质量"}


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    path = tmp_path / "visits.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


def _preview(path: Path, chart: dict, metrics: list[dict] = METRICS) -> dict:
    with connect_source(str(path)) as con:
        return preview_chart(con, [Metric.model_validate(m) for m in metrics],
                             Chart.model_validate({"id": "c", "title": "测试图", "why": "测试", **chart}))


def _compile(path: Path, charts: list[dict], **extra: object) -> dict:
    with connect_source(str(path)) as con:
        return compile_dashboard(con, DashboardSpec.model_validate(
            {"title": "访问看板", "goal": "看购买转化", "metrics": METRICS, "charts": charts + [BOUNCE], **extra}))


def _static(path: Path, charts: list[dict], **extra: object) -> list[str]:
    with connect_source(str(path)) as con:
        source = source_of(con)
        return check_spec(DashboardSpec.model_validate(
            {"title": "访问看板", "goal": "看购买转化", "metrics": METRICS, "charts": charts, **extra}),
            column_types(con, source), source)


# ---------- 数值分段 ----------
def test_bin_labels_read_like_ranges() -> None:
    assert bin_labels([1, 6, 21], True, 0, 705) == ["0", "1–5", "6–20", "≥21"]
    assert bin_labels([1, 6, 21], True, -3, 21) == ["≤0", "1–5", "6–20", "21"]
    assert bin_labels([2, 3], True, 0, 9) == ["≤1", "2", "≥3"]
    assert bin_labels([60, 300.5], False, 0, 9e4) == ["<60", "60–300.5", "≥300.5"]
    assert bin_labels([1.5, 3], True, 0, 9) == ["<1.5", "1.5–3", "≥3"]  # 分界点不是整数时按小数写


def test_bins_count_rows_in_order_and_show_empty_ranges(csv_path: Path) -> None:
    hist = _preview(csv_path, {"type": "column", "metrics": ["records"], "x": {"column": "pages", "bins": [1, 6, 21]}})
    assert [(r["x"], r["values"][0]) for r in hist["rows"]] == [("0", 1), ("1–5", 4), ("6–20", 2), ("≥21", 3)]
    rates = _preview(csv_path, {"type": "column", "metrics": ["rate"],
                                "x": {"column": "pages", "bins": [1, 6, 21, 100, 200]}})
    assert [(r["x"], r["values"][0]) for r in rates["rows"]] == [
        ("0", 0), ("1–5", 0), ("6–20", 1), ("21–99", pytest.approx(2 / 3, abs=1e-4)), ("100–199", None), ("≥200", None)]
    counts = _preview(csv_path, {"type": "column", "metrics": ["records"],
                                 "x": {"column": "pages", "bins": [1, 6, 21, 100, 200]}})
    assert [r["values"][0] for r in counts["rows"]][-2:] == [0, 0]  # 空的段计数为 0，比率为空
    decimal = _preview(csv_path, {"type": "column", "metrics": ["records"], "x": {"column": "bounce", "bins": [0.05, 0.1]}})
    assert [(r["x"], r["values"][0]) for r in decimal["rows"]] == [("<0.05", 4), ("0.05–0.1", 2), ("≥0.1", 4)]


@pytest.mark.parametrize(("x", "message"), [
    ({"column": "segment", "bins": [1, 2]}, "bins 只用于数值列"),
    ({"column": "pages", "bins": [6, 1]}, "从小到大、不重复"),
    ({"column": "pages", "bins": [1, 1]}, "从小到大、不重复"),
    ({"column": "pages", "bins": [1, 6], "order": ["0"]}, "不和 keep、order 一起用"),
])
def test_bins_are_checked(csv_path: Path, x: dict, message: str) -> None:
    problems = _static(csv_path, [KPI, {"id": "c", "title": "测试图", "type": "column", "metrics": ["records"],
                                        "x": x, "why": "测试"}])
    assert any(message in p for p in problems), problems


def test_filters_do_not_take_bins(csv_path: Path) -> None:
    problems = _static(csv_path, [KPI, {**KPI, "id": "k2", "metrics": ["records"]}],
                       filters=[{"column": "pages", "bins": [5]}])
    assert any("筛选器不用 bins" in p for p in problems), problems


# ---------- 散点图 ----------
def test_scatter_puts_size_against_rate_and_keeps_the_biggest(csv_path: Path) -> None:
    out = _preview(csv_path, {"type": "scatter", "metrics": ["records", "rate"], "x": {"column": "segment"}})
    assert [(r["x"], r["values"][0], r["values"][1]) for r in out["rows"]] == [
        ("a", 3, pytest.approx(1 / 3, abs=1e-4)), ("b", 3, pytest.approx(2 / 3, abs=1e-4)),
        ("c", 3, pytest.approx(1 / 3, abs=1e-4)), ("d", 1, 0)]
    assert out["facts"]["max"]["x"] == "b" and out["facts"]["min"]["x"] == "d"  # 最大最小看纵轴（比率），不看规模
    top = _preview(csv_path, {"type": "scatter", "metrics": ["records", "rate"], "x": {"column": "segment"}, "top_n": 3})
    assert [r["x"] for r in top["rows"]] == ["a", "b", "c"]
    assert top["notes"] == ["只画了记录数最大的 3 个，共 4 个"]


@pytest.mark.parametrize(("chart", "message"), [
    ({"type": "scatter", "metrics": ["rate"], "x": {"column": "segment"}}, "scatter 要填 2 个指标"),
    ({"type": "scatter", "metrics": ["records", "rate"], "x": {"column": "segment"},
      "series": {"column": "bought"}}, "scatter 不用 series"),
    ({"type": "bar", "metrics": ["records", "rate"], "x": {"column": "segment"}}, "规模和比率一起看用 scatter"),
    ({"type": "column", "metrics": ["rate"], "x": {"column": "segment"}, "top_n": 3}, "只有 bar 和 scatter 用 top_n"),
])
def test_scatter_rules(csv_path: Path, chart: dict, message: str) -> None:
    problems = _static(csv_path, [KPI, {"id": "c", "title": "测试图", "why": "测试", **chart}])
    assert any(message in p for p in problems), problems


# ---------- 参考线 ----------
def test_reference_is_the_overall_value_with_its_parts(csv_path: Path) -> None:
    bar = _preview(csv_path, {"type": "bar", "metrics": ["rate"], "x": {"column": "segment"}, "reference": "overall"})
    assert bar["facts"]["reference"] == {"value": 0.4, "numerator": 4, "denominator": 10}
    scatter = _preview(csv_path, {"type": "scatter", "metrics": ["records", "rate"], "x": {"column": "segment"},
                                  "reference": "overall"})
    assert scatter["facts"]["reference"]["value"] == 0.4  # 散点图的参考线画在纵轴指标上
    out = _compile(csv_path, [KPI, {"id": "s", "title": "各分组购买率", "type": "bar", "metrics": ["rate"],
                                    "x": {"column": "segment"}, "reference": "overall", "why": "对比",
                                    "insight": "b 为 66.7%，整体 40.0%（4 / 10）"}],
                   filters=[{"column": "bought"}])
    assert out["slices"]["no"]["s"]["facts"]["reference"]["value"] == 0  # 每个筛选切片按自己的数据算
    assert out["charts"][1]["reference"] == "overall"


@pytest.mark.parametrize(("chart", "message"), [
    ({"type": "bar", "metrics": ["records"], "x": {"column": "segment"}}, "参考线没有意义"),
    ({"type": "table", "metrics": ["rate"], "x": {"column": "segment"}}, "reference 只用于"),
    ({"type": "line", "metrics": ["rate"], "x": {"column": "pages"}, "series": {"column": "segment"}}, "reference 只用于"),
])
def test_reference_rules(csv_path: Path, chart: dict, message: str) -> None:
    problems = _static(csv_path, [KPI, {"id": "c", "title": "测试图", "why": "测试", "reference": "overall", **chart}])
    assert any(message in p for p in problems), problems


# ---------- 重点 ----------
def test_highlight_must_name_a_group_in_the_chart(csv_path: Path) -> None:
    ok = _preview(csv_path, {"type": "bar", "metrics": ["rate"], "x": {"column": "segment"}, "highlight": ["b"]})
    assert ok["rows"][0]["x"] == "b"
    with pytest.raises(DashboardProblems, match=r"highlight 里这些取值在图里没有 \['B'\]"):
        _preview(csv_path, {"type": "bar", "metrics": ["rate"], "x": {"column": "segment"}, "highlight": ["B"]})
    with pytest.raises(DashboardProblems, match="调大 top_n"):
        _preview(csv_path, {"type": "scatter", "metrics": ["records", "rate"], "x": {"column": "segment"},
                            "top_n": 3, "highlight": ["d"]})
    with pytest.raises(DashboardProblems, match="highlight 里这些取值在图里没有"):
        _compile(csv_path, [KPI, {"id": "s", "title": "各分组购买率", "type": "bar", "metrics": ["rate"],
                                  "x": {"column": "segment"}, "highlight": ["z"], "why": "对比"}])
    problems = _static(csv_path, [KPI, {"id": "c", "title": "测试图", "type": "table", "metrics": ["rate"],
                                        "x": {"column": "segment"}, "highlight": ["b"], "why": "测试"}])
    assert any("highlight 只用于" in p for p in problems), problems


# ---------- 分区和宽窄 ----------
def test_sections_cover_every_chart_and_are_few(csv_path: Path) -> None:
    bar = {"title": "各分组", "type": "bar", "metrics": ["rate"], "x": {"column": "segment"}, "why": "对比"}
    problems = _static(csv_path, [KPI, {**bar, "id": "a", "section": "客群"}, {**bar, "id": "b"}])
    assert any("这些图没有写：['b']" in p for p in problems), problems
    problems = _static(csv_path, [KPI] + [{**bar, "id": f"c{i}", "section": f"分区{i}"} for i in range(6)])
    assert any("最多 5 个" in p for p in problems), problems
    out = _compile(csv_path, [KPI, {**bar, "id": "a", "section": "客群", "width": "half"}])
    assert (out["charts"][1]["section"], out["charts"][1]["width"]) == ("客群", "half")


# ---------- 0–1 之间的均值可以用百分比 ----------
def test_percent_averages_must_stay_between_zero_and_one(csv_path: Path) -> None:
    assert _preview(csv_path, {"type": "kpi", "metrics": ["bounce"]})["facts"]["value"] == pytest.approx(0.084)
    with pytest.raises(DashboardProblems, match="amount 最大是 120"):
        _preview(csv_path, {"type": "kpi", "metrics": ["avg_amount"]},
                 [{"id": "avg_amount", "label": "平均金额", "agg": "avg", "column": "amount", "format": "percent"}])
    problems = _static(csv_path, [KPI, {**KPI, "id": "k2", "metrics": ["total"]}],
                       metrics=METRICS + [{"id": "total", "label": "合计", "agg": "sum", "column": "amount",
                                           "format": "percent"}])
    assert any("sum 不能用" in p for p in problems), problems


def test_each_metric_keeps_its_own_unit(csv_path: Path) -> None:
    out = _compile(csv_path, [KPI, {"id": "t", "title": "明细", "type": "table", "metrics": ["records", "rate"],
                                    "x": {"column": "segment"}, "why": "查数"}])
    assert out["charts"][1]["metric_units"] == {"records": None, "rate": None}
