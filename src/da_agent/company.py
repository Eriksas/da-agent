"""上市公司财报分析工具：读关键科目表，计算推导量、比率、同比、杜邦拆解和同行对比。全部是确定性计算。

口径约定：
- 金额是各公司编报币种的原始数字（元或美元），不做汇率换算，所以跨公司只比比率和增速。
- 财年按结束日期所在年份命名：阿里 FY2025 = 2024-04-01 至 2025-03-31；其他三家是自然年。
- ROE、ROA、周转率用年初、年末的平均数：需要上一财年末的资产负债表。
- 标准分类里没有的科目标为“未披露”，相关比率不计算，不当成 0。
- 只做财务分析和风险提示：不做估值、评级，不构成投资建议。
"""

from pathlib import Path
from typing import Any

import duckdb

from .cleaning import fetch_rows
from .decompose import chain_substitution, order_range, shapley
from .financials import COMPANIES, ITEMS, KEY_FACTS_PATH

DERIVED = {
    "gross_profit": ("毛利", "营业收入 − 营业成本"),
    "free_cash_flow": ("自由现金流", "经营活动现金流净额 − 购建固定资产支出"),
}
SUMMARY_ITEMS = ("revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income", "net_income_total",
                 "rd_expense", "marketing_expense", "operating_cash_flow", "capex", "free_cash_flow",
                 "assets", "liabilities", "equity", "cash")
RATIOS: dict[str, tuple[str, str]] = {
    "revenue_growth": ("营业收入同比", "本年营业收入 ÷ 上年营业收入 − 1"),
    "gross_margin": ("毛利率", "（营业收入 − 营业成本）÷ 营业收入；各公司计入营业成本的项目不同，跨公司比较要谨慎"),
    "operating_margin": ("经营利润率", "经营利润 ÷ 营业收入"),
    "net_margin": ("归母净利率", "归母净利润 ÷ 营业收入"),
    "rd_ratio": ("研发费用率", "研发费用 ÷ 营业收入"),
    "marketing_ratio": ("销售及市场费用率", "销售及市场费用 ÷ 营业收入"),
    "roe": ("ROE", "归母净利润 ÷ 年初年末归母股东权益的平均值"),
    "roa": ("ROA", "归母净利润 ÷ 年初年末总资产的平均值"),
    "asset_turnover": ("总资产周转率", "营业收入 ÷ 年初年末总资产的平均值"),
    "equity_multiplier": ("权益乘数", "年初年末总资产的平均值 ÷ 年初年末归母股东权益的平均值"),
    "debt_ratio": ("资产负债率", "总负债 ÷ 总资产"),
    "current_ratio": ("流动比率", "流动资产 ÷ 流动负债"),
    "ocf_to_net_income": ("经营现金流 ÷ 归母净利润", "大于 1 说明利润有现金支撑；归母净利润不为正时不计算"),
}
PEER_RATIOS = ("revenue_growth", "gross_margin", "operating_margin", "net_margin", "roe", "debt_ratio", "ocf_to_net_income")
DUPONT_FACTORS = ("net_margin", "asset_turnover", "equity_multiplier")
NOTICE = "只做财务分析：不做估值、评级，不构成投资建议。"


def connect_financials(path: Path = KEY_FACTS_PATH) -> duckdb.DuckDBPyConnection:
    """把关键科目表读进内存数据库。"""
    if not path.exists():
        raise FileNotFoundError(f"缺少关键科目表 {path}，请先运行 da-agent fetch-financials")
    con = duckdb.connect()
    con.execute("CREATE TABLE key_facts AS SELECT * FROM read_csv(?, header = true, all_varchar = false, "
                "types = {'period_start': 'VARCHAR', 'period_end': 'VARCHAR', 'filed': 'VARCHAR'})",
                [path.as_posix()])
    return con


def _available(con: duckdb.DuckDBPyConnection) -> list[str]:
    """关键科目表里有数据的公司，按 COMPANIES 的顺序。"""
    present = {row["company"] for row in fetch_rows(con, "SELECT DISTINCT company FROM key_facts")}
    return [key for key in COMPANIES if key in present]


def _company(company: str) -> Any:
    if company not in COMPANIES:
        raise ValueError(f"没有 {company} 的数据；可选：{list(COMPANIES)}")
    return COMPANIES[company]


def _facts(con: duckdb.DuckDBPyConnection, company: str) -> dict[int, dict[str, dict[str, Any]]]:
    """某公司各财年的原始数字：{财年: {科目: 一行}}。"""
    by_year: dict[int, dict[str, dict[str, Any]]] = {}
    for row in fetch_rows(con, "SELECT * FROM key_facts WHERE company = ? ORDER BY fiscal_year", [company]):
        by_year.setdefault(int(row["fiscal_year"]), {})[row["item"]] = row
    if not by_year:
        raise ValueError(f"关键科目表里没有 {company} 的数据")
    return by_year


def _values(facts: dict[int, dict[str, dict[str, Any]]], year: int) -> tuple[dict[str, float | None], dict[str, str]]:
    """一个财年的科目值（含推导量），以及推导量的算法说明。"""
    raw = facts.get(year, {})
    value: dict[str, float | None] = {key: (float(raw[key]["value"]) if key in raw else None) for key in ITEMS}
    derived: dict[str, str] = {}
    if value["revenue"] is not None and value["cost_of_revenue"] is not None:
        value["gross_profit"], derived["gross_profit"] = value["revenue"] - value["cost_of_revenue"], DERIVED["gross_profit"][1]
    else:
        value["gross_profit"] = None
    if value["operating_cash_flow"] is not None and value["capex"] is not None:
        value["free_cash_flow"] = value["operating_cash_flow"] - value["capex"]
        derived["free_cash_flow"] = DERIVED["free_cash_flow"][1]
    else:
        value["free_cash_flow"] = None
    if value["liabilities"] is None and value["liabilities_and_equity"] is not None:
        equity = value["equity_total"] if value["equity_total"] is not None else value["equity"]
        if equity is not None:
            value["liabilities"] = value["liabilities_and_equity"] - equity
            derived["liabilities"] = "负债和权益合计 − 股东权益（该公司没有直接标注总负债）"
    return value, derived


def _div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _avg(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else (a + b) / 2


def _ratios(facts: dict[int, dict[str, dict[str, Any]]], year: int) -> dict[str, float | None]:
    """一个财年的全部比率（未取整）。需要平均数的比率要用到上一财年末的余额。"""
    cur, _ = _values(facts, year)
    prev, _ = _values(facts, year - 1)
    avg_assets, avg_equity = _avg(cur["assets"], prev["assets"]), _avg(cur["equity"], prev["equity"])
    if avg_equity is not None and avg_equity <= 0:
        avg_equity = None  # 权益为负或为零时 ROE、权益乘数没有意义
    net_income = cur["net_income"]
    return {
        "revenue_growth": None if _div(cur["revenue"], prev["revenue"]) is None else cur["revenue"] / prev["revenue"] - 1,
        "gross_margin": _div(cur["gross_profit"], cur["revenue"]),
        "operating_margin": _div(cur["operating_income"], cur["revenue"]),
        "net_margin": _div(net_income, cur["revenue"]),
        "rd_ratio": _div(cur["rd_expense"], cur["revenue"]),
        "marketing_ratio": _div(cur["marketing_expense"], cur["revenue"]),
        "roe": _div(net_income, avg_equity),
        "roa": _div(net_income, avg_assets),
        "asset_turnover": _div(cur["revenue"], avg_assets),
        "equity_multiplier": _div(avg_assets, avg_equity),
        "debt_ratio": _div(cur["liabilities"], cur["assets"]),
        "current_ratio": _div(cur["current_assets"], cur["current_liabilities"]),
        "ocf_to_net_income": _div(cur["operating_cash_flow"], net_income) if net_income and net_income > 0 else None,
    }


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _period(facts: dict[int, dict[str, dict[str, Any]]], year: int) -> dict[str, Any]:
    row = facts.get(year, {}).get("revenue") or next(iter(facts.get(year, {}).values()), None)
    if row is None:
        return {"fiscal_year": year, "start": None, "end": None}
    return {"fiscal_year": year, "start": row["period_start"] or None, "end": row["period_end"]}


def _pick_year(facts: dict[int, dict[str, dict[str, Any]]], fiscal_year: int | None, name: str) -> int:
    years = [y for y, items in facts.items() if "revenue" in items]
    if fiscal_year is None:
        return max(years)
    if fiscal_year not in years:
        raise ValueError(f"{name}没有 FY{fiscal_year} 的年报数据；可用财年：{min(years)}–{max(years)}")
    return fiscal_year


def _year_end_note(company: Any, facts: dict[int, dict[str, dict[str, Any]]], year: int) -> list[str]:
    period = _period(facts, year)
    if period["end"] and not period["end"].endswith("12-31"):
        month, day = int(period["end"][5:7]), int(period["end"][8:10])
        return [f"{company.name}的财年截至 {month} 月 {day} 日：FY{year} 指 {period['start']} 至 {period['end']}，"
                "与自然年不对齐。"]
    return []


def company_overview(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """有哪些公司、财年范围、财年截止日、币种、最近一次年报、哪些科目没有标准数据。"""
    rows = []
    for key in _available(con):
        company = COMPANIES[key]
        facts = _facts(con, key)
        years = sorted(y for y, items in facts.items() if "revenue" in items)
        latest = facts[years[-1]]["revenue"]
        never = [ITEMS[item].label for item in ("rd_expense", "marketing_expense", "capex", "net_income_total")
                 if item not in facts[years[-1]]]
        rows.append({"company": key, "name": company.name, "ticker": company.ticker, "currency": latest["currency"],
                     "fiscal_year_end": latest["period_end"][5:], "fiscal_years": [years[0], years[-1]],
                     "latest_annual_report": {"form": latest["form"], "filed": latest["filed"], "accn": latest["accn"]},
                     "not_disclosed_in_latest_year": never})
    return {"tool": "company_overview", "companies": rows,
            "source": "SEC EDGAR XBRL companyfacts（美国证监会公开数据），只含标准分类（us-gaap）里的科目，只用年报",
            "notes": ["金额是各公司编报币种的原始数字（元或美元），未做汇率换算；跨公司只比较比率和增速。",
                      "财年按结束日期所在年份命名；ROE 等用到平均数的比率需要上一财年末的数据。", NOTICE]}


def company_catalog(con: duckdb.DuckDBPyConnection) -> str:
    """放进系统提示词的公司目录：每家公司一行。"""
    currency = {"CNY": "人民币", "USD": "美元"}
    return "\n".join(f"- `{c['company']}` {c['name']}（{c['ticker']}）：FY{c['fiscal_years'][0]}–FY{c['fiscal_years'][1]}，"
                     f"财年截至 {c['fiscal_year_end']}，{currency.get(c['currency'], c['currency'])}"
                     for c in company_overview(con)["companies"])


def financial_summary(con: duckdb.DuckDBPyConnection, company: str, fiscal_year: int | None = None) -> dict[str, Any]:
    """一家公司一个财年的关键科目和比率，对比上一财年。"""
    info = _company(company)
    facts = _facts(con, company)
    year = _pick_year(facts, fiscal_year, info.name)
    base = year - 1
    cur, derived = _values(facts, year)
    prev, _ = _values(facts, base)
    currency = facts[year]["revenue"]["currency"]
    items, missing, switched = [], [], []
    for key in SUMMARY_ITEMS:
        label = DERIVED[key][0] if key in DERIVED else ITEMS[key].label
        if cur[key] is None:
            missing.append(label)
            continue
        change = None if prev[key] is None else cur[key] - prev[key]
        row = facts[year].get(key)
        items.append({"item": key, "label": label, "current": round(cur[key]), "base": None if prev[key] is None else round(prev[key]),
                      "change": None if change is None else round(change),
                      "change_pct": _r(_div(change, abs(prev[key]) if prev[key] else None)),
                      "source": ({"derived": derived[key]} if key in derived else
                                 {"concept": row["concept"], "form": row["form"], "accn": row["accn"], "filed": row["filed"]})})
        old = facts.get(base, {}).get(key)
        if row is not None and old is not None and old["concept"] != row["concept"]:
            switched.append(f"{label}：FY{year} 用 {row['concept']} 标注，FY{base} 用 {old['concept']}，口径可能有变化")
    current_ratios, base_ratios = _ratios(facts, year), _ratios(facts, base)
    ratios = [{"ratio": key, "label": label, "current": _r(current_ratios[key]), "base": _r(base_ratios[key]),
               "change": _r(None if current_ratios[key] is None or base_ratios[key] is None
                            else current_ratios[key] - base_ratios[key]),
               "definition": definition} for key, (label, definition) in RATIOS.items()]
    warnings = _year_end_note(info, facts, year)
    if missing:
        warnings.append(f"未披露（标准分类里没有对应数字，不等于 0，相关比率不计算）：{'、'.join(missing)}。")
    if "gross_profit" in derived:
        warnings.append("毛利按 营业收入 − 营业成本 计算；各公司计入营业成本的项目不同，跨公司比较毛利率要谨慎。")
    if "liabilities" in derived:
        warnings.append(f"总负债按 {derived['liabilities']} 计算。")
    warnings += switched
    if cur["net_income"] is not None and cur["net_income"] <= 0:
        warnings.append("归母净利润不为正：净利率为负，经营现金流 ÷ 归母净利润不计算。")
    if cur["net_income"] and cur["net_income_total"] is not None:
        gap = abs(cur["net_income_total"] - cur["net_income"]) / abs(cur["net_income"])
        if gap > 0.05:
            warnings.append(f"含少数股东的净利润与归母净利润相差 {gap:.1%}：引用净利润时要写明口径。")
    if base_ratios["roe"] is None and current_ratios["roe"] is not None:
        warnings.append(f"FY{base} 的 ROE 等需要 FY{base - 1} 年末的数据，表里没有，不计算。")
    return {"tool": "financial_summary", "company": company, "name": info.name, "currency": currency,
            "unit": f"金额单位：{'元（人民币）' if currency == 'CNY' else '美元'}，原始数字，未做换算；比率是小数（0.1234 = 12.34%）",
            "period": {"current": _period(facts, year), "base": _period(facts, base)},
            "items": items, "ratios": ratios, "warnings": warnings, "notes": [NOTICE]}


def dupont(con: duckdb.DuckDBPyConnection, company: str, fiscal_year: int | None = None) -> dict[str, Any]:
    """杜邦分析：ROE = 归母净利率 × 总资产周转率 × 权益乘数，并把 ROE 的同比变化拆到三个因子上。

    拆解复用 M2 的连环替代法和 Shapley 分解：三个因子相乘，结构和 GMV 拆解里的三个因子一样。
    """
    info = _company(company)
    facts = _facts(con, company)
    year = _pick_year(facts, fiscal_year, info.name)
    base = year - 1
    current, previous = _ratios(facts, year), _ratios(facts, base)
    for label, values, y in (("当期", current, year), ("基期", previous, base)):
        if any(values[f] is None for f in DUPONT_FACTORS):
            raise ValueError(f"{label} FY{y} 缺少计算杜邦分析需要的数字（营业收入、归母净利润、两个年末的总资产和"
                             f"归母股东权益，且平均权益必须为正），无法拆解")
    factors = {f: (previous[f], current[f]) for f in DUPONT_FACTORS}
    base_values, current_values = {f: v[0] for f, v in factors.items()}, {f: v[1] for f, v in factors.items()}
    chain = chain_substitution(base_values, current_values, DUPONT_FACTORS)
    fair = shapley(base_values, current_values, DUPONT_FACTORS)
    ranges = order_range(base_values, current_values, DUPONT_FACTORS)
    total = current["roe"] - previous["roe"]
    return {
        "tool": "dupont", "company": company, "name": info.name,
        "identity": "ROE = 归母净利率 × 总资产周转率 × 权益乘数（资产、权益取年初年末平均）",
        "method": "连环替代法，默认顺序：净利率 → 周转率 → 权益乘数；Shapley 为 6 种顺序的平均，与顺序无关",
        "period": {"current": _period(facts, year), "base": _period(facts, base)},
        "base": {f: _r(previous[f]) for f in (*DUPONT_FACTORS, "roe")},
        "current": {f: _r(current[f]) for f in (*DUPONT_FACTORS, "roe")},
        "roe_change": _r(total),
        "contributions": [{"factor": f, "label": RATIOS[f][0], "chain": _r(chain[f]), "shapley": _r(fair[f]),
                           "range_across_orders": [_r(ranges[f][0]), _r(ranges[f][1])],
                           "share_of_total_change": _r(_div(fair[f], total))} for f in DUPONT_FACTORS],
        "check": {"contributions_sum_to_total": abs(sum(fair.values()) - total) < 1e-9},
        "notes": ["比率和贡献都是小数：ROE 变化 0.0123 = 1.23 个百分点。", "权益乘数越高，财务杠杆越高。", NOTICE],
        "warnings": _year_end_note(info, facts, year),
    }


def peer_compare(con: duckdb.DuckDBPyConnection, companies: list[str] | None = None, fiscal_year: int | None = None,
                 ratios: list[str] | None = None) -> dict[str, Any]:
    """多家公司的比率对比。不填财年时每家用各自最近的财年；只比比率，不比金额。"""
    keys = companies or _available(con)
    names = list(ratios or PEER_RATIOS)
    unknown = [n for n in names if n not in RATIOS]
    if unknown:
        raise ValueError(f"未知比率：{unknown}；可选：{list(RATIOS)}")
    rows, periods, currencies = [], set(), set()
    for key in keys:
        info = _company(key)
        facts = _facts(con, key)
        year = _pick_year(facts, fiscal_year, info.name)
        values = _ratios(facts, year)
        period = _period(facts, year)
        periods.add((period["start"], period["end"]))
        currencies.add(facts[year]["revenue"]["currency"])
        rows.append({"company": key, "name": info.name, "period": period,
                     **{name: _r(values[name]) for name in names}})
    warnings = []
    if len(periods) > 1:
        warnings.append("各公司的财年期间不完全相同（例如阿里财年截至 3 月 31 日），对比的是各自的财年，不是同一段时间。")
    if len(currencies) > 1:
        warnings.append(f"币种不同（{'、'.join(sorted(currencies))}），只比较比率和增速，不比较金额。")
    if "gross_margin" in names:
        warnings.append("各公司计入营业成本的项目不同，毛利率跨公司比较要谨慎。")
    if any(row[name] is None for row in rows for name in names):
        warnings.append("表中空值表示该公司没有对应的标准数据（未披露）或比率没有意义（如净利润为负），不等于 0。")
    return {"tool": "peer_compare", "rows": rows, "definitions": {n: RATIOS[n][1] for n in names},
            "labels": {n: RATIOS[n][0] for n in names}, "warnings": warnings, "notes": [NOTICE]}
