"""财报分析工具：手算的小样例。

阿里（财年截至 3 月 31 日，人民币）：
  FY2024：收入 1000、成本 600、经营利润 150、归母净利润 100（换了标注写法）、含少数股东净利润 120、经营现金流 130
  FY2023：收入 800、成本 480、归母净利润 60
  年末总资产 1800 / 2000 / 2200，总负债 800 / 900 / 1000，归母权益 1000 / 1100 / 1200（FY2022 / 2023 / 2024）
  FY2024 ROE = 100 ÷ ((1100 + 1200) / 2) = 0.086957 = 净利率 0.1 × 周转率 1000/2100 × 权益乘数 2100/1150
亚马逊（自然年，美元）：FY2024 收入 500、成本 300、归母净利润 −10；没有标注总负债，负债和权益合计 700、权益 300
"""

from pathlib import Path

import pandas as pd
import pytest

from da_agent.company import company_overview, connect_financials, dupont, financial_summary, peer_compare
from da_agent.financials import COLUMNS


def rows_for(company: str, currency: str, year_end: str, values: dict[str, dict[int, float]],
             concepts: dict[tuple[str, int], str] | None = None) -> list[dict]:
    out = []
    for item, by_year in values.items():
        for year, value in by_year.items():
            end = f"{year}-{year_end}"
            start = f"{year - 1}-04-01" if year_end == "03-31" else f"{year}-01-01"
            instant = item in ("assets", "liabilities", "equity", "liabilities_and_equity")
            out.append({"company": company, "fiscal_year": year, "item": item, "value": value, "currency": currency,
                        "period_start": "" if instant else start, "period_end": end,
                        "concept": (concepts or {}).get((item, year), item), "form": "20-F", "accn": f"a{year}",
                        "filed": f"{year}-06-01"})
    return out


def write_key_facts(path: Path) -> Path:
    """写出本文件开头说明的手算样例，供各测试文件共用。"""
    alibaba = rows_for("alibaba", "CNY", "03-31", {
        "revenue": {2023: 800, 2024: 1000}, "cost_of_revenue": {2023: 480, 2024: 600},
        "operating_income": {2023: 80, 2024: 150}, "net_income": {2023: 60, 2024: 100},
        "net_income_total": {2023: 62, 2024: 120}, "operating_cash_flow": {2023: 90, 2024: 130},
        "assets": {2022: 1800, 2023: 2000, 2024: 2200}, "liabilities": {2022: 800, 2023: 900, 2024: 1000},
        "equity": {2022: 1000, 2023: 1100, 2024: 1200},
    }, concepts={("net_income", 2024): "NetIncomeLossAvailableToCommonStockholdersBasic",
                 ("net_income", 2023): "NetIncomeLoss"})
    amazon = rows_for("amazon", "USD", "12-31", {
        "revenue": {2023: 400, 2024: 500}, "cost_of_revenue": {2023: 240, 2024: 300},
        "net_income": {2023: 20, 2024: -10}, "operating_cash_flow": {2024: 50},
        "assets": {2023: 600, 2024: 700}, "liabilities_and_equity": {2023: 600, 2024: 700},
        "equity": {2023: 280, 2024: 300},
    })
    pd.DataFrame(alibaba + amazon, columns=COLUMNS).to_csv(path, index=False)
    return path


@pytest.fixture(scope="module")
def con(tmp_path_factory: pytest.TempPathFactory):
    connection = connect_financials(write_key_facts(tmp_path_factory.mktemp("fin") / "key_facts.csv"))
    yield connection
    connection.close()


def ratio(summary: dict, name: str) -> dict:
    return next(r for r in summary["ratios"] if r["ratio"] == name)


def test_summary_ratios_by_hand(con) -> None:
    s = financial_summary(con, "alibaba", 2024)
    assert ratio(s, "roe")["current"] == 0.087  # 100 / 1150
    assert ratio(s, "roe")["base"] == 0.0571  # 60 / 1050
    assert ratio(s, "revenue_growth")["current"] == 0.25
    assert ratio(s, "gross_margin")["current"] == 0.4 and ratio(s, "gross_margin")["change"] == 0.0
    assert ratio(s, "debt_ratio")["current"] == 0.4545
    assert ratio(s, "ocf_to_net_income")["current"] == 1.3
    gross = next(i for i in s["items"] if i["item"] == "gross_profit")
    assert (gross["current"], gross["source"]) == (400, {"derived": "营业收入 − 营业成本"})


def test_summary_warnings(con) -> None:
    warnings = "\n".join(financial_summary(con, "alibaba", 2024)["warnings"])
    assert "财年截至 3 月 31 日：FY2024 指 2023-04-01 至 2024-03-31" in warnings
    assert "未披露（标准分类里没有对应数字，不等于 0" in warnings and "研发费用" in warnings and "购建固定资产支出" in warnings
    assert "归母净利润：FY2024 用 NetIncomeLossAvailableToCommonStockholdersBasic 标注，FY2023 用 NetIncomeLoss" in warnings
    assert "含少数股东的净利润与归母净利润相差 20.0%" in warnings


def test_derived_liabilities_and_negative_profit(con) -> None:
    s = financial_summary(con, "amazon", 2024)
    assert ratio(s, "debt_ratio")["current"] == 0.5714  # (700 − 300) / 700
    assert ratio(s, "ocf_to_net_income")["current"] is None
    warnings = "\n".join(s["warnings"])
    assert "总负债按 负债和权益合计 − 股东权益" in warnings and "归母净利润不为正" in warnings
    assert "财年截至" not in warnings  # 自然年不需要提示


def test_dupont_identity_and_shapley(con) -> None:
    d = dupont(con, "alibaba", 2024)
    current = d["current"]
    assert current["roe"] == 0.087
    assert current["net_margin"] * current["asset_turnover"] * current["equity_multiplier"] == pytest.approx(0.087, abs=1e-4)
    assert d["roe_change"] == pytest.approx(0.0298, abs=1e-4)  # 0.086957 − 0.057143
    assert d["check"]["contributions_sum_to_total"] is True
    assert sum(c["shapley"] for c in d["contributions"]) == pytest.approx(d["roe_change"], abs=2e-4)


def test_dupont_refuses_without_prior_balances(con) -> None:
    with pytest.raises(ValueError, match="无法拆解"):
        dupont(con, "alibaba", 2023)  # FY2023 的基期 FY2022 需要 FY2021 年末的数


def test_peer_compare_warns_about_periods_currency_and_gaps(con) -> None:
    p = peer_compare(con)
    assert [row["company"] for row in p["rows"]] == ["alibaba", "amazon"]
    assert [row["period"]["end"] for row in p["rows"]] == ["2024-03-31", "2024-12-31"]
    warnings = "\n".join(p["warnings"])
    assert "财年期间不完全相同" in warnings and "币种不同（CNY、USD）" in warnings and "不等于 0" in warnings


def test_bad_requests_are_refused(con) -> None:
    with pytest.raises(ValueError, match="没有 FY2030"):
        financial_summary(con, "alibaba", 2030)
    with pytest.raises(ValueError, match="没有 tencent"):
        financial_summary(con, "tencent")
    with pytest.raises(ValueError, match="未知比率"):
        peer_compare(con, ratios=["pe_ratio"])  # 估值指标不在工具里


def test_overview_lists_only_companies_in_the_table(con) -> None:
    overview = company_overview(con)
    assert [(c["company"], c["fiscal_year_end"], c["currency"]) for c in overview["companies"]] == [
        ("alibaba", "03-31", "CNY"), ("amazon", "12-31", "USD")]
    assert "不构成投资建议" in overview["notes"][-1]


def test_amounts_in_yi_are_readable_and_sourced() -> None:
    """首次真实运行：原始数字太长，模型自己换算成“百万”，换算后的数找不到出处。现在工具直接给出“亿”的写法。"""
    from da_agent.checks import check_run
    from da_agent.company import _yi
    import json

    assert _yi(1023670000000, "CNY") == "10,236.70 亿元" and _yi(716924000000, "USD") == "7,169.24 亿美元"
    assert _yi(None, "CNY") is None
    tool = {"tool": "financial_summary", "items": [{"current": 1023670000000, "base": 996347000000,
                                                    "in_yi": {"current": "10,236.70 亿元", "base": "9,963.47 亿元"}}]}
    calc = {"tool": "calculate", "expression": "10236.70 - 9963.47", "purpose": "收入增加额（亿元）", "result": 273.23}
    run = {"answer": "营业收入 10,236.70 亿元，比上年多 273.23 亿元（20-F 文件编号 0001193125-26-231755）。",
           "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
                        {"role": "tool", "content": json.dumps(tool, ensure_ascii=False)},
                        {"role": "tool", "content": json.dumps(calc, ensure_ascii=False)}]}
    numbers = check_run(run)["numbers"]
    assert numbers["ungrounded"] == [] and numbers["unsupported_calculations"] == []
    assert numbers["checked"] == 2  # 文件编号不当成数字


def test_real_summary_gives_yi_text() -> None:
    with connect_financials() as real:
        revenue = next(i for i in financial_summary(real, "alibaba", 2026)["items"] if i["item"] == "revenue")
    assert revenue["in_yi"] == {"current": "10,236.70 亿元", "base": "9,963.47 亿元", "change": "273.23 亿元"}
