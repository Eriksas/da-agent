"""数据清洗规则：每条规则都有名字、理由、处理方式，并能统计影响了多少行和多少金额。

做法：不直接删数据，而是给每一行打上“命中了哪些规则”的标记（flagged 表），
再用视图定义“商品销售”（sales）和“取消”（cancellations）。规则改了只需重跑，原始数据不动。

规则是在 2026-09-29 用 SQL 逐项探查数据后定下的，探查结论写在每条规则的 reason 里。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from .dataset import FIRST_SHEET, SECOND_SHEET

# 非商品编码，按业务含义逐个归类。没有用“编码不是 5 位数字就排除”这种格式规则，
# 因为 DCGS 开头的编码（如 DCGS0058 口香糖）是真实商品，格式规则会误删。
NON_MERCHANDISE: dict[str, str] = {
    "POST": "运费与手续费", "DOT": "运费与手续费", "C2": "运费与手续费", "C3": "运费与手续费",
    "BANK CHARGES": "运费与手续费", "AMAZONFEE": "运费与手续费", "CRUK": "运费与手续费",
    "M": "人工调整", "m": "人工调整", "D": "人工调整", "S": "人工调整",
    "ADJUST": "人工调整", "ADJUST2": "人工调整", "B": "人工调整",
    "TEST001": "测试记录", "TEST002": "测试记录",
    "GIFT": "礼品卡",
}
GIFT_VOUCHER_PREFIX = "gift_0001_"  # 礼品卡：gift_0001_10 表示 10 英镑面值
AMBIGUOUS_COUNTRIES = ("Unspecified", "European Community")
# 极端大额行阈值（英镑）。质量报告会给出单行金额的分位数，这个阈值远高于 99.9 分位，
# 只用来挑出需要人工看一眼的行，不删除。
EXTREME_LINE_AMOUNT = 10_000
BUSINESS_COLUMNS = "invoice, stock_code, description, quantity, invoice_date, price, customer_id, country"


@dataclass(frozen=True)
class Rule:
    """一条清洗规则。column 是 flagged 表里对应的布尔列。"""

    id: str
    name: str
    reason: str
    action: str
    column: str
    excludes: bool  # True：命中的行不进入任何指标计算


RULES: tuple[Rule, ...] = (
    Rule("R01", "两个工作表重叠期的记录",
         "第 1 表的结束时间晚于第 2 表的开始时间，重叠期的记录两个表里都有；直接拼接会重复计算。",
         "删除第 1 表中的重叠记录，保留第 2 表的", "r01_sheet_overlap", True),
    Rule("R02", "坏账调整单（发票号 A 开头）",
         "会计调账分录，不是买卖交易；金额很大，会扭曲 GMV。",
         "删除", "r02_bad_debt", True),
    Rule("R03", "非商品编码（运费、手续费、人工调整、测试、礼品卡）",
         "不是卖出的商品。礼品卡在售卡和用卡时各记一次，计入会重复。",
         "不计入商品 GMV，按类别单独汇总", "r03_non_merchandise", False),
    Rule("R04", "取消/退货单（发票号 C 开头）",
         "数量为负，代表订单被取消或退货。",
         "不计入 GMV（下单口径），单独计为取消金额", "r04_cancellation", False),
    Rule("R05", "非取消单但数量 ≤ 0",
         "单价为 0，多为库存调整或损耗记录，不是交易。",
         "删除", "r05_nonpositive_quantity", True),
    Rule("R06", "单价 ≤ 0",
         "赠品、缺描述的录入记录或调账，没有销售金额。",
         "删除", "r06_nonpositive_price", True),
    Rule("R07", "完全重复行",
         "8 个业务字段完全相同。可能是重复录入，也可能是同一商品扫了两次，无法确定。",
         "默认删除（保留第一条）；报告写明影响金额，删错会让 GMV 偏低", "r07_duplicate", True),
    Rule("R08", "缺少客户 ID",
         "游客或未登记客户下单，交易真实，但无法识别是谁。",
         "计入 GMV 和订单数；不计入活跃客户、复购、人均订单", "r08_missing_customer", False),
    Rule("R09", "极端大额行",
         f"单行金额绝对值 ≥ {EXTREME_LINE_AMOUNT:,} 英镑，可能是批发大单、误录或随后被取消的订单。",
         "仅标记，列入人工复核清单", "r09_extreme_line", False),
    Rule("R10", "国家不明确",
         "取值是 Unspecified 或 European Community，不是具体国家。",
         "仅标记；按国家分析时单独成组", "r10_ambiguous_country", False),
)


def _sql_list(values: list[str] | tuple[str, ...]) -> str:
    """把常量列表写成 SQL 的 ('a', 'b')。这里的值都来自代码常量，不来自用户输入。"""
    return "(" + ", ".join("'" + value.replace("'", "''") + "'" for value in values) + ")"


def _build_tables(con: duckdb.DuckDBPyConnection, source_sql: str) -> None:
    """建 raw → flagged 两张表和 sales / cancellations 两个视图。"""
    con.execute(f"""
        CREATE OR REPLACE TABLE raw AS
        SELECT row_number() OVER (ORDER BY source_sheet, source_row) AS line_id, *, quantity * price AS amount
        FROM {source_sql}
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE flagged AS
        WITH overlap AS (
            SELECT raw.*,
                   (source_sheet = '{FIRST_SHEET}' AND invoice_date >= (
                        SELECT min(invoice_date) FROM raw WHERE source_sheet = '{SECOND_SHEET}')) AS r01_sheet_overlap
            FROM raw
        ),
        numbered AS (
            -- 重复行只在非重叠记录里找：PARTITION BY 把空值当成同一组，所以缺客户 ID 的重复行也能被发现
            SELECT *, row_number() OVER (PARTITION BY r01_sheet_overlap, {BUSINESS_COLUMNS} ORDER BY line_id) AS copy_no
            FROM overlap
        )
        SELECT * EXCLUDE (copy_no),
               starts_with(invoice, 'A') AS r02_bad_debt,
               (stock_code IN {_sql_list(list(NON_MERCHANDISE))} OR starts_with(stock_code, '{GIFT_VOUCHER_PREFIX}')) AS r03_non_merchandise,
               starts_with(invoice, 'C') AS r04_cancellation,
               (quantity <= 0 AND NOT starts_with(invoice, 'C')) AS r05_nonpositive_quantity,
               (price <= 0) AS r06_nonpositive_price,
               (NOT r01_sheet_overlap AND copy_no > 1) AS r07_duplicate,
               (customer_id IS NULL) AS r08_missing_customer,
               (abs(amount) >= {EXTREME_LINE_AMOUNT}) AS r09_extreme_line,
               (country IN {_sql_list(AMBIGUOUS_COUNTRIES)}) AS r10_ambiguous_country
        FROM numbered
    """)
    excluded = " OR ".join(rule.column for rule in RULES if rule.excludes)
    con.execute("ALTER TABLE flagged ADD COLUMN is_valid BOOLEAN")
    con.execute(f"UPDATE flagged SET is_valid = NOT ({excluded})")
    con.execute("""
        CREATE OR REPLACE VIEW sales AS
        SELECT * FROM flagged
        WHERE is_valid AND NOT r04_cancellation AND NOT r03_non_merchandise AND quantity > 0 AND price > 0
    """)
    con.execute("""
        CREATE OR REPLACE VIEW cancellations AS
        SELECT * FROM flagged WHERE is_valid AND r04_cancellation AND NOT r03_non_merchandise
    """)
    # 人工复核清单：会影响商品指标的极端大额行，并检查 24 小时内是否被同一客户冲销。两种冲销方式：
    # 1) 等量取消：同商品、同数量、同单价的 C 单；2) 人工调整冲销：金额相等的 M（Manual）C 单。
    # 第 2 种按 R03 属于非商品，不会从商品净销售额里扣除，所以要单独标出来。
    con.execute("""
        CREATE OR REPLACE VIEW extreme_review AS
        SELECT f.*,
               (f.quantity > 0 AND EXISTS (
                    SELECT 1 FROM flagged c
                    WHERE c.r04_cancellation AND c.is_valid AND c.customer_id = f.customer_id AND c.stock_code = f.stock_code
                      AND c.quantity = -f.quantity AND c.price = f.price
                      AND c.invoice_date BETWEEN f.invoice_date AND f.invoice_date + INTERVAL 1 DAY
               )) AS cancelled_within_1d,
               (f.quantity > 0 AND EXISTS (
                    SELECT 1 FROM flagged c
                    WHERE c.r04_cancellation AND c.is_valid AND c.stock_code IN ('M', 'm') AND c.customer_id = f.customer_id
                      AND abs(c.amount + f.amount) < 0.005
                      AND c.invoice_date BETWEEN f.invoice_date AND f.invoice_date + INTERVAL 1 DAY
               )) AS offset_by_manual_within_1d
        FROM flagged f WHERE f.r09_extreme_line AND f.is_valid AND NOT f.r03_non_merchandise
    """)


def fetch_rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """执行 SQL，返回字典列表。参数用 ? 占位传入，不拼进 SQL 字符串。"""
    cursor = con.execute(sql, params or [])
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def fetch_one(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    """执行只返回一行的 SQL。"""
    return fetch_rows(con, sql, params)[0]


def connect(parquet_path: Path) -> duckdb.DuckDBPyConnection:
    """从 parquet 建一个内存数据库，并完成打标记。"""
    con = duckdb.connect()
    _build_tables(con, f"read_parquet('{parquet_path.as_posix()}')")
    return con


def connect_frame(frame: pd.DataFrame) -> duckdb.DuckDBPyConnection:
    """从 DataFrame 建库，给测试用；列必须和 dataset.normalize 的输出一致。"""
    con = duckdb.connect()
    con.register("source_frame", frame)
    _build_tables(con, "source_frame")
    return con
