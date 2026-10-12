"""看板可用的数据：每份数据一张表，加上我们写的语义标注。

每份数据只给一张表：看板里每个指标都是对这张表的一次聚合，不需要模型去关联多张表。
语义标注告诉模型和校验器哪些坑不能踩，校验器会强制执行：
- pin_by：某个数值列的含义取决于另一列。电商的 amount 要看 line_type（销售还是取消），
  财报的 value 要看 item（营业收入还是总资产）。聚合这些列时必须按那一列过滤或分组，否则就是把不同的东西加在一起
- unit_column：数值列的单位在另一列（财报的币种）。同一张图里不能混合不同单位
- units：单位固定的数值列（电商金额都是英镑）
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import duckdb

from ..cleaning import fetch_one, fetch_rows
from ..financials import COMPANIES, ITEMS

MAX_TOP_VALUES = 12  # 取值很多的文本列（编号、商品名）只列出最常见的这么多个
DIMENSION_MAX_DISTINCT = 60  # 取值不超过这么多的文本列，算作可以分组的维度


@dataclass(frozen=True)
class Source:
    name: str
    label: str
    table: str
    description: str
    columns: dict[str, str]  # 列名 → 中文说明
    connect: Callable[[], duckdb.DuckDBPyConnection]
    time_column: str | None = None
    pin_by: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unit_column: dict[str, str] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)


def _connect_retail() -> duckdb.DuckDBPyConnection:
    from ..cleaning import connect
    from ..dataset import PARQUET_PATH

    if not PARQUET_PATH.exists():
        raise ValueError("还没有准备电商数据，请先运行：da-agent prepare-data")
    con = connect(PARQUET_PATH)
    _retail_view(con)
    return con


def _retail_view(con: duckdb.DuckDBPyConnection) -> None:
    """销售和取消合成一张交易表（清洗规则已经应用过），金额和数量都取正数，用 line_type 区分。"""
    def columns(line_type: str) -> str:
        return (f"invoice, invoice_date AS invoice_time, '{line_type}' AS line_type, stock_code, description, "
                "abs(quantity) AS quantity, price, abs(amount) AS amount, customer_id, country")

    con.execute(f"""CREATE OR REPLACE VIEW transactions AS
                    SELECT {columns('销售')} FROM sales UNION ALL SELECT {columns('取消')} FROM cancellations""")


def _connect_financials() -> duckdb.DuckDBPyConnection:
    from ..company import connect_financials

    con = connect_financials()
    _financials_view(con)
    return con


def _financials_view(con: duckdb.DuckDBPyConnection) -> None:
    """关键科目表加上公司和科目的中文名。"""
    names = " ".join(f"WHEN '{key}' THEN '{c.name}'" for key, c in COMPANIES.items())
    labels = " ".join(f"WHEN '{key}' THEN '{item.label}'" for key, item in ITEMS.items())
    con.execute(f"""CREATE OR REPLACE VIEW financial_facts AS
                    SELECT company, CASE company {names} ELSE company END AS company_name, fiscal_year,
                           item, CASE item {labels} ELSE item END AS item_label, value, currency,
                           CAST(period_end AS DATE) AS period_end
                    FROM key_facts""")


SOURCES: dict[str, Source] = {
    "retail": Source(
        "retail", "电商交易（UCI Online Retail II，已清洗）", "transactions",
        "一家英国线上礼品批发零售商 2009-12 至 2011-12 的商品交易明细，每行是订单里的一个商品。"
        "清洗规则已经应用：删除了重复、坏账调整、单价为 0 等记录，运费等非商品行不在表里。",
        {"invoice": "发票号（订单号），C 开头的是取消单", "invoice_time": "下单（或取消）时间",
         "line_type": "销售 / 取消", "stock_code": "商品编码", "description": "商品名称",
         "quantity": "数量（取正数）", "price": "单价（英镑）", "amount": "金额 = 数量 × 单价（英镑，取正数）",
         "customer_id": "客户 ID，缺失表示游客或未登记客户", "country": "客户所在国家"},
        _connect_retail, time_column="invoice_time",
        pin_by={"amount": ("line_type",), "quantity": ("line_type",)}, units={"amount": "£", "price": "£"}),
    "financials": Source(
        "financials", "上市公司年报关键科目（SEC）", "financial_facts",
        "阿里巴巴、京东、拼多多、亚马逊 FY2017 起的年报关键科目，长表：每行是一家公司一个财年的一个科目。"
        "金额是各公司编报币种的原始数字，没有做汇率换算。阿里财年截至 3 月 31 日，其他三家是自然年。",
        {"company": "公司代码", "company_name": "公司中文名", "fiscal_year": "财年（按财年结束日期所在年份）",
         "item": "科目代码", "item_label": "科目中文名", "value": "金额（原始数字，单位见 currency）",
         "currency": "币种：CNY 人民币、USD 美元", "period_end": "期末日期"},
        _connect_financials, pin_by={"value": ("item", "item_label")}, unit_column={"value": "currency"}),
}


def source_of(con: duckdb.DuckDBPyConnection) -> Source:
    """连接属于哪份数据：看建了哪张表。"""
    tables = {r["name"] for r in fetch_rows(con, "SELECT view_name AS name FROM duckdb_views() UNION ALL "
                                                 "SELECT table_name FROM duckdb_tables()")}
    for source in SOURCES.values():
        if source.table in tables:
            return source
    raise ValueError("这个连接里没有看板可用的数据表")


def connect_source(name: str) -> duckdb.DuckDBPyConnection:
    if name not in SOURCES:
        raise ValueError(f"没有名为 {name} 的数据；可选：{list(SOURCES)}")
    return SOURCES[name].connect()


def column_types(con: duckdb.DuckDBPyConnection, source: Source) -> dict[str, str]:
    """列名 → 类型：number（小数）、integer、text、timestamp、date。"""
    out = {}
    for r in fetch_rows(con, f'DESCRIBE "{source.table}"'):
        t = r["column_type"].upper()
        out[r["column_name"]] = ("timestamp" if t.startswith("TIMESTAMP") else "date" if t == "DATE"
                                 else "integer" if t in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT")
                                 else "number" if t in ("DOUBLE", "FLOAT", "REAL") or t.startswith("DECIMAL")
                                 else "text")
    return out


def profile_table(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """给模型看的表格概况：每列的类型、取值个数、缺失比例、范围、最常见的取值，以及必须遵守的语义规则。"""
    source = source_of(con)
    table = f'"{source.table}"'
    rows = fetch_one(con, f"SELECT count(*) AS n FROM {table}")["n"]
    columns = []
    for name, kind in column_types(con, source).items():
        col = f'"{name}"'
        stats = fetch_one(con, f"SELECT count(DISTINCT {col}) AS distinct_values, "
                               f"count(*) FILTER (WHERE {col} IS NULL) AS nulls, min({col}) AS lo, max({col}) AS hi "
                               f"FROM {table}")
        info: dict[str, Any] = {"name": name, "type": kind, "description": source.columns.get(name, ""),
                                "distinct_values": stats["distinct_values"],
                                "null_share": round(stats["nulls"] / rows, 4) if rows else None}
        if kind in ("number", "integer"):
            info["min"], info["max"] = stats["lo"], stats["hi"]
        elif kind in ("timestamp", "date"):
            info["min"], info["max"] = str(stats["lo"]), str(stats["hi"])
        few = stats["distinct_values"] <= DIMENSION_MAX_DISTINCT
        if kind == "text" or (kind == "integer" and few):
            limit = DIMENSION_MAX_DISTINCT if few else MAX_TOP_VALUES  # 维度列列出全部取值，模型写 where 时才知道有哪些
            top = fetch_rows(con, f"SELECT {col} AS value, count(*) AS n FROM {table} GROUP BY 1 "
                                  f"ORDER BY 2 DESC, 1 LIMIT {limit}")
            info["top_values"] = [{"value": r["value"], "rows": r["n"], "row_share": round(r["n"] / rows, 4)}
                                  for r in top]
        info["role_hint"] = ("time" if kind in ("timestamp", "date") or name == source.time_column
                             else "measure" if kind == "number" or (kind == "integer" and not few)
                             else "dimension" if few
                             else "id")
        if name == "fiscal_year":
            info["role_hint"] = "time"
        columns.append(info)
    rules = [f"{value} 的含义取决于 {' 或 '.join(by)}：聚合 {value} 时必须用 where 把 {by[0]} 限定为一个取值，"
             f"或者在图里按 {by[0]} 分组，否则就是把不同的东西加在一起" for value, by in source.pin_by.items()]
    rules += [f"{value} 的单位在 {unit} 列：同一张图里只能出现一种单位；比较不同单位的分组时只比比率"
              for value, unit in source.unit_column.items()]
    rules += [f"{column} 的单位是 {unit}" for column, unit in source.units.items()]
    return {"tool": "profile_table", "source": source.name, "label": source.label, "table": source.table,
            "description": source.description, "rows": rows, "time_column": source.time_column,
            "columns": columns, "rules": rules}
