"""Excel 入口（M20）：运营同学手上那种 Excel——标题行、空行空列、合计行、存成文本的数字、多个工作表。"""

from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from da_agent.autodash.compile import compile_dashboard
from da_agent.autodash.excel import read_excel_table
from da_agent.autodash.sources import connect_source, profile_table, source_of, split_excel_name
from da_agent.autodash.spec import DashboardSpec

ROWS = [
    ["月份", "国家", None, "GMV（英镑）", "订单数", "下单日期"],
    ["2011-10", "United Kingdom", None, "1,200.50", 10, datetime(2011, 10, 3)],
    ["2011-10", "France", None, 300, 3, datetime(2011, 10, 5)],
    [None, None, None, None, None, None],                                  # 中间的空行
    ["2011-11", "United Kingdom", None, "1,500.00", 12, datetime(2011, 11, 7)],
    ["2011-11", "France", None, 450.25, 4, datetime(2011, 11, 9)],
    ["2011-11", "小计", None, 1950.25, 16, None],                           # 中间的小计行
    ["合计", None, None, 3450.75, 29, None],                                # 末尾的合计行
]


def write_book(path: Path) -> Path:
    book = Workbook()
    notes = book.active
    notes.title = "说明"
    notes["A1"] = "这份表每月 10 号更新"
    sales = book.create_sheet("销售")
    sales["A1"] = "2011 年各国月度销售汇总（单位：英镑，含之后被取消的订单）"
    sales["A2"] = "制表：运营部"
    for i, row in enumerate(ROWS, start=4):  # 第 3 行空着，表头在第 4 行
        for j, value in enumerate(row, start=1):
            if value is not None:
                sales.cell(row=i, column=j, value=value)
    book.save(path)
    return path


@pytest.fixture
def book(tmp_path: Path) -> Path:
    return write_book(tmp_path / "月度销售.xlsx")


def test_a_messy_sheet_becomes_a_clean_table(book: Path) -> None:
    frame, notes, sheet = read_excel_table(book, "销售")
    assert sheet == "销售" and list(frame.columns) == ["月份", "国家", "GMV（英镑）", "订单数", "下单日期"]
    assert len(frame) == 4 and frame["GMV（英镑）"].tolist() == [1200.5, 300.0, 1500.0, 450.25]  # 文本转成了数字
    assert str(frame["订单数"].dtype) == "Int64" and frame["下单日期"].dtype.kind == "M"
    assert frame["月份"].dtype.kind == "M" and frame["月份"].iloc[0] == datetime(2011, 10, 1)  # “2011-10”是年月，不是普通文字
    text = "；".join(notes)
    assert "工作簿有 2 个工作表（说明、销售），读的是“销售”" in text
    assert "表头在第 4 行，上面 3 行是标题或说明：2011 年各国月度销售汇总（单位：英镑，含之后被取消的订单）；制表：运营部" in text
    assert "剔除了 2 行“合计、小计”" in text and "去掉了 1 个空列" in text and "去掉了 1 个空行" in text
    assert "列“GMV（英镑）”有 2 个数字存成了文本，已转成数字" in text and "列“月份”年月写成了文字，已转成日期（取每月 1 号）" in text


def test_sheet_names_and_errors(book: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="找不到表头.*--sheet 指定别的工作表：\\['销售'\\]"):
        read_excel_table(book)  # 第一个工作表只有一句说明
    with pytest.raises(ValueError, match="没有名为“库存”的工作表"):
        read_excel_table(book, "库存")
    assert split_excel_name("a/b.xlsx#销售") == (Path("a/b.xlsx"), "销售")
    assert split_excel_name(f"xlsx:{book.as_posix()}#销售") == (book, "销售")
    assert split_excel_name("a/b.XLSX") == (Path("a/b.XLSX"), None) and split_excel_name("a.csv") is None


def test_duplicate_and_missing_headers_get_names(tmp_path: Path) -> None:
    book = Workbook()
    sheet = book.active
    for row in (["国家", "GMV", "GMV", None], ["UK", 1, 2, 3], ["FR", 4, 5, 6]):
        sheet.append(row)
    book.save(tmp_path / "x.xlsx")
    frame, notes, _ = read_excel_table(tmp_path / "x.xlsx")
    assert list(frame.columns) == ["国家", "GMV", "GMV_2", "列4"]
    assert any("重复" in n for n in notes) and any("没有表头" in n for n in notes)


def test_the_dashboard_pipeline_reads_excel_like_csv(book: Path) -> None:
    with connect_source(f"{book}#销售") as con:
        source = source_of(con)
        profile = profile_table(con)
    assert source.name == f"xlsx:{book.resolve().as_posix()}#销售" and source.time_column == "月份"  # 能按月画折线了
    assert "剔除了 2 行" in profile["description"] and "单位：英镑" in profile["description"]  # 模型看得到做了什么处理
    spec = DashboardSpec.model_validate({
        "title": "月度销售", "goal": "看各国 GMV",
        "metrics": [{"id": "gmv", "label": "GMV", "agg": "sum", "column": "GMV（英镑）"}],
        "charts": [{"id": "k", "title": "总 GMV", "type": "kpi", "metrics": ["gmv"], "why": "整体"},
                   {"id": "c", "title": "各国 GMV", "type": "bar", "metrics": ["gmv"], "x": {"column": "国家"}, "why": "对比"}]})
    with connect_source(source.name) as con:  # 按记下的名字重新连接（重新生成看板时就是这样）
        out = compile_dashboard(con, spec)
    assert out["slices"]["all"]["k"]["facts"]["value"] == pytest.approx(3450.75)  # 合计行没有被再加一遍
    assert {r["x"]: r["values"][0] for r in out["slices"]["all"]["c"]["rows"]} == {"United Kingdom": 2700.5, "France": 750.25}


def test_cli_passes_the_sheet(book: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from da_agent import cli
    from da_agent.autodash import build

    script = tmp_path / "script.json"
    script.write_text('{"replies": []}', encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(build, "build_dashboard", lambda source, *a, **k: seen.append(source) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        cli.main(["build-dashboard", str(book), "--sheet", "销售", "--llm", "fake", "--script", str(script)])
    assert seen == [f"{book}#销售"]
    assert cli.main(["build-dashboard", "data.csv", "--sheet", "销售", "--llm", "fake", "--script", "x"]) == 2  # 只用于 Excel


@pytest.mark.parametrize(("values", "expected"), [
    (["2011-01", "2011-12"], [datetime(2011, 1, 1), datetime(2011, 12, 1)]),
    (["2011年1月", "2011 年 12 月"], [datetime(2011, 1, 1), datetime(2011, 12, 1)]),
    (["2011/01/03", "2011-12-09"], [datetime(2011, 1, 3), datetime(2011, 12, 9)]),
])
def test_dates_written_as_text_become_dates(tmp_path: Path, values: list[str], expected: list[datetime]) -> None:
    book = Workbook()
    for row in (["时间", "GMV"], [values[0], 1], [values[1], 2]):
        book.active.append(row)
    book.save(tmp_path / "d.xlsx")
    frame, _, _ = read_excel_table(tmp_path / "d.xlsx")
    assert frame["时间"].tolist() == expected


def test_mixed_text_is_left_alone(tmp_path: Path) -> None:
    book = Workbook()
    for row in (["时间", "GMV"], ["2011-01", 1], ["上半年", 2]):
        book.active.append(row)
    book.save(tmp_path / "m.xlsx")
    frame, _, _ = read_excel_table(tmp_path / "m.xlsx")
    assert frame["时间"].tolist() == ["2011-01", "上半年"]  # 整列不都是日期时不转


def test_a_period_limits_the_whole_dashboard(book: Path) -> None:
    """整个看板只看一段时间（M22）：所有图、年份筛选、数据范围都只算这段时间；预览也一样。"""
    from da_agent.autodash.compile import DashboardProblems, preview_chart
    from da_agent.autodash.spec import Metric, Period

    base = {"title": "月度销售", "goal": "看 2011 年 11 月各国的 GMV",
            "metrics": [{"id": "gmv", "label": "GMV", "agg": "sum", "column": "GMV（英镑）"}],
            "charts": [{"id": "k", "title": "总 GMV", "type": "kpi", "metrics": ["gmv"], "why": "整体"},
                       {"id": "c", "title": "各国 GMV", "type": "bar", "metrics": ["gmv"], "x": {"column": "国家"}, "why": "对比"}],
            "year_filter": True}
    with connect_source(f"{book}#销售") as con:
        out = compile_dashboard(con, DashboardSpec.model_validate(base | {"period": {"start": "2011-11", "end": "2011-11"}}))
        assert out["slices"]["all"]["k"]["facts"]["value"] == pytest.approx(1950.25)  # 只有 11 月：1,500.00 + 450.25
        assert out["source"]["range"] == "2011-11 至 2011-11（按月汇总）"
        everything = compile_dashboard(con, DashboardSpec.model_validate(base))
        assert everything["slices"]["all"]["k"]["facts"]["value"] == pytest.approx(3450.75)  # 不给 period 时看全部
        preview = preview_chart(con, [Metric.model_validate(base["metrics"][0])], DashboardSpec.model_validate(base).charts[1],
                                Period(start="2011-10", end="2011-10"))
        assert {r["x"]: r["values"][0] for r in preview["rows"]} == {"United Kingdom": 1200.5, "France": 300.0}
        with pytest.raises(DashboardProblems, match="period 2012 里没有数据；数据范围是 2011-10-01 至 2011-11-01"):
            compile_dashboard(con, DashboardSpec.model_validate(base | {"period": {"start": "2012", "end": "2012"}}))
        with pytest.raises(DashboardProblems, match="开始 2011-11 晚于结束 2011-10"):
            compile_dashboard(con, DashboardSpec.model_validate(base | {"period": {"start": "2011-11", "end": "2011-10"}}))


def test_period_bounds() -> None:
    from datetime import date

    from da_agent.autodash.spec import Period

    assert Period(start="2010", end="2010").bounds() == (date(2010, 1, 1), date(2011, 1, 1))
    assert Period(start="2010-07", end="2010-12").bounds() == (date(2010, 7, 1), date(2011, 1, 1))
    assert Period(start="2010-07-15", end="2010-07-20").bounds() == (date(2010, 7, 15), date(2010, 7, 21))
    with pytest.raises(ValueError):
        Period(start="2010-7", end="2010")  # 月份要两位
