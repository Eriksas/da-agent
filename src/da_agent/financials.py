"""上市公司财报数据：从 SEC EDGAR 下载 XBRL 数据，整理成“公司 × 财年 × 科目”的关键科目表。

数据来源：SEC 的 companyfacts 接口（美国证监会公开数据，属于公共领域）。每家公司一个 JSON，
里面是这家公司在所有报表里用标准分类（us-gaap）标注过的每一个数字。

整理时要处理的真实问题（都写在测试里）：
- 同一个数字会在多份年报里重复出现（今年的年报会把去年的数字作为对比列再报一次）：取最新提交的那份，
  财报重述后的数字会覆盖旧数字。
- 每条记录的 fy 是“哪一年的报表”，不是“哪一年的数字”：财年按期间的结束日期判断。
- 阿里的财年截至 3 月 31 日：FY2024 = 2023-04-01 至 2024-03-31。
- 中概股用人民币编报，但报表里还有少量“便利换算”的美元数字：取数字最多的币种作为编报币种，不混用。
- 同一个科目有几种标准写法（例如收入）：按顺序取第一个有数的写法，并记下用了哪一个。
- 公司用自定义科目标注的数字不在标准分类里：表里就没有这一行，分析时标为“未披露”，不能当成 0。

这里只保存报表上的原始数字和出处；毛利、自由现金流、比率等推导量由分析工具现算。
"""

import json
import logging
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .paths import RAW_DIR, ROOT

LOGGER = logging.getLogger(__name__)
SEC_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_RAW_DIR = RAW_DIR / "sec"
KEY_FACTS_PATH = ROOT / "data" / "financials" / "key_facts.csv"
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A"}
FIRST_FISCAL_YEAR = 2017  # 从 2017 财年起：2018 财年的平均资产、平均权益要用到 2017 年末的数


@dataclass(frozen=True)
class Company:
    key: str
    cik: int
    name: str
    ticker: str
    entity: str  # SEC 登记的公司名，用来核对 CIK 没有写错


COMPANIES: dict[str, Company] = {c.key: c for c in (
    Company("alibaba", 1577552, "阿里巴巴", "BABA", "Alibaba Group Holding"),
    Company("jd", 1549802, "京东", "JD", "JD.com"),
    Company("pdd", 1737806, "拼多多（PDD Holdings）", "PDD", "PDD Holdings"),
    Company("amazon", 1018724, "亚马逊", "AMZN", "AMAZON COM"),
)}


@dataclass(frozen=True)
class Item:
    label: str
    kind: str  # duration：一个财年内的发生额（利润表、现金流量表）；instant：财年末的余额（资产负债表）
    concepts: tuple[str, ...]  # 标准分类里的写法，按优先顺序


ITEMS: dict[str, Item] = {
    "revenue": Item("营业收入", "duration", ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")),
    "cost_of_revenue": Item("营业成本", "duration", ("CostOfRevenue", "CostOfGoodsAndServicesSold")),
    "operating_income": Item("经营利润", "duration", ("OperatingIncomeLoss",)),
    # 京东 2017 年起不再用 NetIncomeLoss 标注归母净利润，改用“归属于普通股股东的净利润”
    "net_income": Item("归母净利润", "duration", ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic")),
    "net_income_total": Item("净利润（含少数股东）", "duration", ("ProfitLoss",)),
    "rd_expense": Item("研发费用", "duration", ("ResearchAndDevelopmentExpense",)),
    "marketing_expense": Item("销售及市场费用", "duration", ("SellingAndMarketingExpense", "MarketingExpense")),
    "ga_expense": Item("管理费用", "duration", ("GeneralAndAdministrativeExpense",)),
    "operating_cash_flow": Item("经营活动现金流净额", "duration", ("NetCashProvidedByUsedInOperatingActivities",)),
    "capex": Item("购建固定资产支出", "duration",
                  ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")),
    "assets": Item("总资产", "instant", ("Assets",)),
    "liabilities": Item("总负债", "instant", ("Liabilities",)),
    "liabilities_and_equity": Item("负债和权益合计", "instant", ("LiabilitiesAndStockholdersEquity",)),
    "equity": Item("归母股东权益", "instant", ("StockholdersEquity",)),
    "equity_total": Item("股东权益（含少数股东）", "instant",
                         ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",)),
    "current_assets": Item("流动资产", "instant", ("AssetsCurrent",)),
    "current_liabilities": Item("流动负债", "instant", ("LiabilitiesCurrent",)),
    "cash": Item("现金及现金等价物", "instant", ("CashAndCashEquivalentsAtCarryingValue",)),
}
COLUMNS = ["company", "fiscal_year", "item", "value", "currency", "period_start", "period_end", "concept",
           "form", "accn", "filed"]


def fetch_companyfacts(company: Company, user_agent: str, out_dir: Path = SEC_RAW_DIR, timeout: float = 60) -> Path:
    """下载一家公司的 companyfacts 原始 JSON。SEC 要求请求头写明联系方式；请求头不写日志。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"CIK{company.cik:010d}.json"
    request = urllib.request.Request(SEC_URL.format(cik=company.cik), headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError(f"{company.key} 的数据超过 {MAX_RESPONSE_BYTES // 1024 // 1024} MB，已中止")
    data = json.loads(body)
    if company.entity.lower() not in data.get("entityName", "").lower():
        raise ValueError(f"CIK {company.cik} 对应的是 {data.get('entityName')}，不是 {company.entity}，请检查 CIK")
    target.write_bytes(body)
    LOGGER.info("已下载 %s（%s，%.1f MB）", company.name, data["entityName"], len(body) / 1024 / 1024)
    return target


def _days(row: dict[str, Any]) -> int:
    return (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days


def reporting_currency(gaap: dict[str, Any]) -> str:
    """编报币种：关键科目里数字最多的币种。中概股的美元数字只是便利换算，数量少得多。"""
    counts: Counter[str] = Counter()
    for item in ITEMS.values():
        for concept in item.concepts:
            for unit, rows in gaap.get(concept, {}).get("units", {}).items():
                counts[unit] += len(rows)
    if not counts:
        raise ValueError("没有找到任何关键科目")
    return counts.most_common(1)[0][0]


def fiscal_year_end(gaap: dict[str, Any], currency: str) -> str:
    """财年截止的月-日，按营业收入年度数字的结束日期取最常见的那个，例如阿里是 03-31。"""
    ends: Counter[str] = Counter()
    for concept in ITEMS["revenue"].concepts:
        for row in gaap.get(concept, {}).get("units", {}).get(currency, []):
            if row["form"] in ANNUAL_FORMS and row.get("start") and 350 <= _days(row) <= 380:
                ends[row["end"][5:]] += 1
    if not ends:
        raise ValueError("没有找到年度营业收入，无法判断财年截止日")
    return ends.most_common(1)[0][0]


def _annual_rows(gaap: dict[str, Any], concept: str, item: Item, currency: str, year_end: str) -> list[dict[str, Any]]:
    """一个科目在年报里的年度数字：发生额要求期间约一年；余额要求是财年末的数。"""
    rows = []
    for row in gaap.get(concept, {}).get("units", {}).get(currency, []):
        if row["form"] not in ANNUAL_FORMS or row["end"][5:] != year_end:
            continue
        if item.kind == "duration" and not (row.get("start") and 350 <= _days(row) <= 380):
            continue
        if item.kind == "instant" and row.get("start"):
            continue
        rows.append(row)
    return rows


def company_key_facts(company: Company, data: dict[str, Any]) -> list[dict[str, Any]]:
    """把一家公司的 companyfacts 整理成关键科目表的行。"""
    gaap = data["facts"].get("us-gaap", {})
    currency = reporting_currency(gaap)
    year_end = fiscal_year_end(gaap, currency)
    out = []
    for key, item in ITEMS.items():
        chosen: dict[int, dict[str, Any]] = {}
        for concept in item.concepts:  # 按优先顺序：某一年前面的写法已经有数，就不用后面的写法
            by_year: dict[int, dict[str, Any]] = {}
            for row in _annual_rows(gaap, concept, item, currency, year_end):
                year = int(row["end"][:4])  # 财年按结束日期判断，不用 row["fy"]（那是报表所属年度）
                if year not in by_year or row["filed"] > by_year[year]["filed"]:  # 重复出现时取最新提交的
                    by_year[year] = {**row, "concept": concept}
            for year, row in by_year.items():
                chosen.setdefault(year, row)
        for year, row in sorted(chosen.items()):
            if year < FIRST_FISCAL_YEAR:
                continue
            out.append({"company": company.key, "fiscal_year": year, "item": key, "value": row["val"],
                        "currency": currency, "period_start": row.get("start", ""), "period_end": row["end"],
                        "concept": row["concept"], "form": row["form"], "accn": row["accn"], "filed": row["filed"]})
    return out


def build_key_facts(raw_dir: Path = SEC_RAW_DIR, out_path: Path = KEY_FACTS_PATH) -> pd.DataFrame:
    """读取已下载的原始 JSON，生成关键科目表并保存为 CSV（提交到仓库，测试和演示不用联网）。"""
    rows = []
    for company in COMPANIES.values():
        path = raw_dir / f"CIK{company.cik:010d}.json"
        if not path.exists():
            raise FileNotFoundError(f"缺少 {company.name} 的原始数据：{path}，请先运行 da-agent fetch-financials")
        rows += company_key_facts(company, json.loads(path.read_text(encoding="utf-8")))
    frame = pd.DataFrame(rows, columns=COLUMNS).sort_values(["company", "fiscal_year", "item"], ignore_index=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out_path, index=False, encoding="utf-8", lineterminator="\n")
    return frame


def fetch_all(user_agent: str, raw_dir: Path = SEC_RAW_DIR, refresh: bool = False) -> list[Path]:
    """下载全部公司的原始数据。已有文件默认不重复下载；SEC 限制每秒 10 次请求，这里每次间隔 0.5 秒。"""
    paths = []
    for company in COMPANIES.values():
        path = raw_dir / f"CIK{company.cik:010d}.json"
        if refresh or not path.exists():
            path = fetch_companyfacts(company, user_agent, raw_dir)
            time.sleep(0.5)
        paths.append(path)
    return paths
