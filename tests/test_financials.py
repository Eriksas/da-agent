"""SEC 财报数据整理：手写的小样例覆盖每一个真实陷阱；另用提交到仓库的关键科目表核对几处真实情况。"""

import io
import json
from pathlib import Path

import pandas as pd
import pytest

from da_agent import financials
from da_agent.financials import COMPANIES, KEY_FACTS_PATH, Company, company_key_facts, fiscal_year_end, reporting_currency


def fact(val: float, end: str, start: str | None = None, form: str = "20-F", filed: str = "2024-05-23",
         accn: str = "new") -> dict:
    row = {"end": end, "val": val, "accn": accn, "fy": 2099, "fp": "FY", "form": form, "filed": filed}  # fy 故意写错
    return {**row, "start": start} if start else row


DATA = {"entityName": "Example Holding", "facts": {"us-gaap": {
    "Revenues": {"units": {
        "CNY": [fact(100, "2023-03-31", "2022-04-01", filed="2023-07-21", accn="old"),
                fact(100, "2023-03-31", "2022-04-01"),  # 同一个数字在下一年的年报里作为对比列再出现
                fact(120, "2024-03-31", "2023-04-01"),
                fact(30, "2023-12-31", "2023-10-01", form="6-K")],  # 季度数字
        "USD": [fact(17, "2024-03-31", "2023-04-01")],  # 便利换算的美元数字
    }},
    "NetIncomeLoss": {"units": {"CNY": [fact(10, "2023-03-31", "2022-04-01")]}},
    "NetIncomeLossAvailableToCommonStockholdersBasic": {"units": {"CNY": [
        fact(9, "2023-03-31", "2022-04-01"), fact(12, "2024-03-31", "2023-04-01")]}},
    "Assets": {"units": {"CNY": [
        fact(500, "2024-03-31"),
        fact(480, "2023-03-31", filed="2023-07-21", accn="old"), fact(490, "2023-03-31"),  # 重述：取后提交的 490
        fact(510, "2023-09-30", form="6-K")]}},  # 半年末余额
}}}
COMPANY = Company("example", 1, "示例", "EX", "Example Holding")


@pytest.fixture(scope="module")
def rows() -> dict[tuple[str, int], dict]:
    return {(r["item"], r["fiscal_year"]): r for r in company_key_facts(COMPANY, DATA)}


def test_currency_and_fiscal_year_end() -> None:
    gaap = DATA["facts"]["us-gaap"]
    assert reporting_currency(gaap) == "CNY"  # 美元只是少量便利换算
    assert fiscal_year_end(gaap, "CNY") == "03-31"


def test_fiscal_year_comes_from_end_date_and_latest_filing_wins(rows) -> None:
    assert rows[("revenue", 2023)]["value"] == 100 and rows[("revenue", 2023)]["accn"] == "new"
    assert rows[("revenue", 2024)]["period_start"] == "2023-04-01"
    assert ("revenue", 2099) not in rows  # 没有用 fy 字段
    assert rows[("assets", 2023)]["value"] == 490  # 重述后的数字


def test_quarterly_and_mid_year_numbers_are_dropped(rows) -> None:
    assert {key for key in rows if key[0] == "revenue"} == {("revenue", 2023), ("revenue", 2024)}
    assert {key for key in rows if key[0] == "assets"} == {("assets", 2023), ("assets", 2024)}


def test_concept_fallback_is_recorded(rows) -> None:
    assert (rows[("net_income", 2023)]["value"], rows[("net_income", 2023)]["concept"]) == (10, "NetIncomeLoss")
    assert rows[("net_income", 2024)]["concept"] == "NetIncomeLossAvailableToCommonStockholdersBasic"


def test_missing_items_have_no_row(rows) -> None:
    """标准分类里没有的科目：表里就没有这一行，分析时标为未披露，不当成 0。"""
    assert not any(key[0] == "capex" for key in rows)


def test_fetch_checks_entity_and_sends_user_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = {}

    def fake_urlopen(request, timeout):  # 不联网：记录请求头，返回一家对不上的公司
        sent["agent"] = request.get_header("User-agent")
        return io.BytesIO(json.dumps({"entityName": "Some Other Inc", "facts": {}}).encode())

    monkeypatch.setattr(financials.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError, match="请检查 CIK"):
        financials.fetch_companyfacts(COMPANIES["jd"], "da-agent tester t@example.com", tmp_path)
    assert sent["agent"] == "da-agent tester t@example.com"
    assert not list(tmp_path.iterdir())  # 公司对不上时不保存


def test_real_key_facts_table() -> None:
    """提交到仓库的关键科目表（2026-10-07 生成）里的几处真实情况。"""
    table = pd.read_csv(KEY_FACTS_PATH, dtype={"period_start": str})
    def one(company: str, year: int, item: str) -> pd.Series:
        return table[(table.company == company) & (table.fiscal_year == year) & (table.item == item)].iloc[0]
    alibaba = one("alibaba", 2024, "revenue")
    assert (alibaba["value"], alibaba["currency"], alibaba["period_end"]) == (941168000000, "CNY", "2024-03-31")
    assert one("jd", 2024, "net_income")["concept"] == "NetIncomeLossAvailableToCommonStockholdersBasic"
    assert table[(table.company == "amazon") & (table.item == "liabilities")].empty  # 亚马逊没有标注总负债
    assert set(table.groupby("company")["currency"].first()) == {"CNY", "USD"}
