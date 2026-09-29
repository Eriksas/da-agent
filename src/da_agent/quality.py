"""数据质量报告：规则命中统计、清洗后口径、时间覆盖、人工复核清单。

所有数字都由 SQL 计算；“结论”一节也是用模板把数字填进去，不经过模型。
"""

import json
from pathlib import Path
from typing import Any

import duckdb

from .cleaning import BUSINESS_COLUMNS, EXTREME_LINE_AMOUNT, GIFT_VOUCHER_PREFIX, NON_MERCHANDISE, RULES
from .dataset import DATA_URL, EXPECTED_SHA256

LOW_TRADING_DAYS = 4  # 一周交易天数少于这个值，视为节假日周
CLOSURE_GAP_DAYS = 3  # 相邻两个交易日相隔超过这个天数，视为停业（周六不营业，正常周末间隔是 2 天）


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    """执行 SQL，返回字典列表。"""
    cursor = con.execute(sql)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> dict[str, Any]:
    """执行只返回一行的 SQL。"""
    return _rows(con, sql)[0]


def _category_case() -> str:
    """把非商品编码映射成类别的 SQL CASE 表达式。"""
    whens = " ".join(f"WHEN '{code}' THEN '{category}'" for code, category in NON_MERCHANDISE.items())
    return f"CASE WHEN starts_with(stock_code, '{GIFT_VOUCHER_PREFIX}') THEN '礼品卡' ELSE (CASE stock_code {whens} END) END"


def build_report(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """在已打好标记的库上统计全部质量指标。"""
    overview = _one(con, """
        SELECT count(*) AS raw_rows, min(invoice_date) AS first_time, max(invoice_date) AS last_time,
               isodow(min(invoice_date)) AS first_isodow, isodow(max(invoice_date)) AS last_isodow
        FROM raw""")
    sheets = _rows(con, "SELECT source_sheet, count(*) AS rows, min(invoice_date) AS first_time, max(invoice_date) AS last_time FROM raw GROUP BY 1 ORDER BY 1")

    # 前提核对：R01 假设重叠期两表逐行相同。每次都重新验证，不成立就在报告里显著标出。
    overlap = _one(con, f"""
        WITH a AS (SELECT {BUSINESS_COLUMNS} FROM flagged WHERE r01_sheet_overlap),
             window_ AS (SELECT min(invoice_date) AS start_, max(invoice_date) AS end_ FROM flagged WHERE r01_sheet_overlap),
             b AS (SELECT {BUSINESS_COLUMNS} FROM flagged, window_
                   WHERE NOT r01_sheet_overlap AND invoice_date BETWEEN start_ AND end_
                     AND source_sheet <> (SELECT any_value(source_sheet) FROM flagged WHERE r01_sheet_overlap))
        SELECT (SELECT count(*) FROM a) AS overlap_rows,
               (SELECT count(*) FROM (SELECT * FROM a EXCEPT ALL SELECT * FROM b)) AS only_in_first,
               (SELECT count(*) FROM (SELECT * FROM b EXCEPT ALL SELECT * FROM a)) AS only_in_second""")
    overlap["identical"] = overlap["only_in_first"] == 0 and overlap["only_in_second"] == 0

    counts = _one(con, "SELECT " + ", ".join(
        f"count(*) FILTER (WHERE {rule.column}) AS {rule.id}_rows, "
        f"coalesce(round(sum(amount) FILTER (WHERE {rule.column}), 2), 0) AS {rule.id}_amount"
        for rule in RULES) + " FROM flagged")
    rules = [{
        "id": rule.id, "name": rule.name, "reason": rule.reason, "action": rule.action, "excludes": rule.excludes,
        "rows": counts[f"{rule.id}_rows"], "row_share": counts[f"{rule.id}_rows"] / overview["raw_rows"],
        "amount": counts[f"{rule.id}_amount"],
    } for rule in RULES]
    excluded = _one(con, "SELECT count(*) AS rows, round(sum(amount), 2) AS amount FROM flagged WHERE NOT is_valid")

    sales = _one(con, """
        SELECT count(*) AS rows, count(DISTINCT invoice) AS orders, count(DISTINCT customer_id) AS customers,
               round(sum(amount), 2) AS gmv,
               coalesce(round(sum(amount) FILTER (WHERE customer_id IS NULL), 2), 0) AS gmv_missing_customer,
               count(DISTINCT invoice) FILTER (WHERE customer_id IS NULL) AS orders_missing_customer
        FROM sales""")
    cancelled = _one(con, "SELECT count(*) AS rows, coalesce(round(-sum(amount), 2), 0) AS amount FROM cancellations")
    clean = {
        "sales_rows": sales["rows"], "orders": sales["orders"], "customers": sales["customers"],
        "gmv": sales["gmv"], "cancelled_rows": cancelled["rows"], "cancelled_amount": cancelled["amount"],
        "net_sales": round(sales["gmv"] - cancelled["amount"], 2),
        "gmv_missing_customer": sales["gmv_missing_customer"],
        "gmv_missing_customer_share": sales["gmv_missing_customer"] / sales["gmv"] if sales["gmv"] else None,
        "orders_missing_customer": sales["orders_missing_customer"],
    }
    non_merchandise = _rows(con, f"""
        SELECT {_category_case()} AS category, count(*) AS rows, round(sum(amount), 2) AS amount
        FROM flagged WHERE is_valid AND r03_non_merchandise GROUP BY 1 ORDER BY abs(sum(amount)) DESC""")
    line_quantiles = _one(con, """
        SELECT round(quantile_cont(amount, 0.5), 2) AS p50, round(quantile_cont(amount, 0.99), 2) AS p99,
               round(quantile_cont(amount, 0.999), 2) AS p999, round(max(amount), 2) AS max FROM sales""")

    # 人工复核清单：极端大额行，并检查 24 小时内是否被同一客户冲销。两种冲销方式：
    # 1) 等量取消：同商品、同数量、同单价的 C 单；2) 人工调整冲销：金额相等的 M（Manual）C 单。
    # 第 2 种按 R03 属于非商品，不会从商品净销售额里扣除，所以要单独标出来。
    extreme = _rows(con, """
        SELECT f.invoice, f.stock_code, f.description, f.quantity, f.price, round(f.amount, 2) AS amount,
               f.customer_id, f.invoice_date, f.is_valid,
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
        FROM flagged f WHERE r09_extreme_line AND is_valid AND NOT r03_non_merchandise
        ORDER BY abs(f.amount) DESC""")
    # 其余极端行：已被规则删除，或属于非商品编码，都不影响商品指标，只报数量
    extreme_other = _one(con, """
        SELECT count(*) FILTER (WHERE NOT is_valid) AS deleted, count(*) FILTER (WHERE is_valid AND r03_non_merchandise) AS non_merchandise
        FROM flagged WHERE r09_extreme_line""")

    # 先生成完整的周日历，再匹配销售；否则整周停业的周不会出现在结果里
    weeks = _rows(con, """
        WITH bounds AS (SELECT date_trunc('week', min(invoice_date))::DATE AS first_monday,
                               date_trunc('week', max(invoice_date))::DATE AS last_monday FROM sales),
             calendar AS (SELECT unnest(generate_series(first_monday, last_monday, INTERVAL 7 DAY))::DATE AS monday FROM bounds),
             activity AS (SELECT date_trunc('week', invoice_date)::DATE AS monday,
                                 count(DISTINCT invoice_date::DATE) AS trading_days FROM sales GROUP BY 1)
        SELECT strftime(c.monday, '%G-W%V') AS week, c.monday, coalesce(a.trading_days, 0) AS trading_days
        FROM calendar c LEFT JOIN activity a USING (monday) ORDER BY c.monday""")
    partial_weeks = []
    if overview["first_isodow"] > 1:
        partial_weeks.append(weeks[0]["week"])
    if overview["last_isodow"] < 7:
        partial_weeks.append(weeks[-1]["week"])
    low_weeks = [w for w in weeks if w["trading_days"] < LOW_TRADING_DAYS and w["week"] not in partial_weeks]
    saturdays = _rows(con, "SELECT invoice_date::DATE AS day, count(*) AS rows FROM sales WHERE isodow(invoice_date) = 6 GROUP BY 1 ORDER BY 1")
    closures = _rows(con, f"""
        WITH days AS (SELECT DISTINCT invoice_date::DATE AS day FROM sales),
             paired AS (SELECT lag(day) OVER (ORDER BY day) AS last_open, day AS reopen FROM days)
        SELECT last_open, reopen, datediff('day', last_open, reopen) AS gap_days
        FROM paired WHERE datediff('day', last_open, reopen) > {CLOSURE_GAP_DAYS} ORDER BY last_open""")

    return {
        "dataset": {"source": DATA_URL, "source_sha256": EXPECTED_SHA256, **overview, "sheets": sheets},
        "checks": {"sheet_overlap": overlap},
        "rules": rules,
        "excluded": excluded,
        "clean": clean,
        "non_merchandise": non_merchandise,
        "line_amount_quantiles": line_quantiles,
        "extreme_lines": extreme,
        "extreme_lines_other": extreme_other,
        "coverage": {"weeks": len(weeks), "partial_weeks": partial_weeks, "low_trading_weeks": low_weeks,
                     "saturday_trading_days": saturdays, "closures": closures},
    }


def _money(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def render_markdown(report: dict[str, Any]) -> str:
    """把报告字典渲染成中文 Markdown。只做排版，不新增任何数字。"""
    data, clean, overlap, cov = report["dataset"], report["clean"], report["checks"]["sheet_overlap"], report["coverage"]
    extreme = report["extreme_lines"]
    cancelled_pairs = sum(1 for row in extreme if row["cancelled_within_1d"])
    manual_offsets = [row for row in extreme if row["offset_by_manual_within_1d"]]
    lines = [
        "# 数据质量报告：UCI Online Retail II", "",
        "> 由 `da-agent quality` 生成。所有数字由 SQL 计算；规则定义见 [cleaning.py](../src/da_agent/cleaning.py)。", "",
        "## 结论", "",
        f"- 原始 {data['raw_rows']:,} 行；规则删除 {report['excluded']['rows']:,} 行"
        f"（{_pct(report['excluded']['rows'] / data['raw_rows'])}），清洗后商品销售 {clean['sales_rows']:,} 行。",
        f"- 两个工作表重叠 {overlap['overlap_rows']:,} 行，逐行核对"
        + ("**完全一致**，已删除第 1 表中的副本。" if overlap["identical"] else
           f"**不一致**（仅第 1 表 {overlap['only_in_first']:,} 行，仅第 2 表 {overlap['only_in_second']:,} 行），R01 的前提不成立，需人工检查。"),
        f"- GMV（下单口径）{_money(clean['gmv'])}；取消 {_money(clean['cancelled_amount'])}；净销售额 {_money(clean['net_sales'])}。",
        f"- 缺客户 ID 的销售占 GMV {_pct(clean['gmv_missing_customer_share'])}：活跃客户、复购等客户类指标只覆盖其余部分。",
        f"- {len(extreme)} 行影响商品指标的极端大额记录待人工复核：{cancelled_pairs} 行在 24 小时内被同一客户等量取消；"
        f"{len(manual_offsets)} 行被人工调整冲销（金额 {_money(sum(row['amount'] for row in manual_offsets))}），"
        "这部分计入了 GMV，但不会从商品净销售额中扣除，净销售额因此偏高。",
        f"- 共 {cov['weeks']} 个自然周（ISO 周），其中不完整周 {len(cov['partial_weeks'])} 个、交易天数少于 {LOW_TRADING_DAYS} 天的周 {len(cov['low_trading_weeks'])} 个（含整周停业）；做周环比时应排除或单独说明。",
        "", "## 1. 数据概况", "",
        f"- 来源：<{data['source']}>（CC BY 4.0），文件 SHA256 `{data['source_sha256']}`",
        f"- 时间范围：{data['first_time']} 至 {data['last_time']}", "",
        "| 工作表 | 行数 | 最早 | 最晚 |", "|---|---:|---|---|",
    ]
    lines += [f"| {s['source_sheet']} | {s['rows']:,} | {s['first_time']} | {s['last_time']} |" for s in data["sheets"]]
    lines += ["", "## 2. 清洗规则与影响", "",
              "规则命中可能重叠（一行可以同时命中多条），所以各行相加不等于删除总数。金额为命中行的金额合计（数量 × 单价，含正负）。", "",
              "| 规则 | 名称 | 命中行数 | 占原始行 | 涉及金额 | 处理方式 | 理由 |", "|---|---|---:|---:|---:|---|---|"]
    lines += [f"| {r['id']} | {r['name']} | {r['rows']:,} | {_pct(r['row_share'])} | {_money(r['amount'])} | {r['action']} | {r['reason']} |"
              for r in report["rules"]]
    lines += ["", "## 3. 清洗后的口径", "",
              "| 指标 | 数值 | 口径 |", "|---|---:|---|",
              f"| 商品销售行 | {clean['sales_rows']:,} | 未被删除、不是取消单、不是非商品编码、数量和单价都为正 |",
              f"| 订单数 | {clean['orders']:,} | 不同发票号个数 |",
              f"| 可识别客户数 | {clean['customers']:,} | 不同客户 ID 个数（不含缺失） |",
              f"| GMV | {_money(clean['gmv'])} | 下单口径：数量 × 单价，含之后被取消的订单 |",
              f"| 取消金额 | {_money(clean['cancelled_amount'])} | C 开头发票中的商品行，取正数 |",
              f"| 净销售额 | {_money(clean['net_sales'])} | GMV − 取消金额 |",
              f"| 缺客户 ID 的 GMV | {_money(clean['gmv_missing_customer'])}（{_pct(clean['gmv_missing_customer_share'])}） | 这部分无法计入客户类指标 |",
              "", "## 4. 非商品金额（已从商品 GMV 中剔除）", "",
              "| 类别 | 行数 | 金额 |", "|---|---:|---:|"]
    lines += [f"| {row['category']} | {row['rows']:,} | {_money(row['amount'])} |" for row in report["non_merchandise"]]
    q = report["line_amount_quantiles"]
    lines += ["", "## 5. 人工复核清单：极端大额行", "",
              f"商品销售单行金额分位数：中位数 {_money(q['p50'])}，99% 分位 {_money(q['p99'])}，99.9% 分位 {_money(q['p999'])}，最大 {_money(q['max'])}。"
              f"阈值 {EXTREME_LINE_AMOUNT:,} 只用于挑出需要人工看的行，这些行不会被删除。"
              f"下表只列会影响商品指标的行；另有 {report['extreme_lines_other']['deleted']} 行已被其他规则删除、"
              f"{report['extreme_lines_other']['non_merchandise']} 行属于非商品编码，不影响商品指标，未列出。", "",
              "| 发票 | 编码 | 描述 | 数量 | 单价 | 金额 | 客户 | 时间 | 24 小时内被冲销 |", "|---|---|---|---:|---:|---:|---|---|---|"]
    lines += [f"| {row['invoice']} | {row['stock_code']} | {row['description'] or ''} | {row['quantity']:,} | {_money(row['price'])} | "
              f"{_money(row['amount'])} | {row['customer_id'] or '缺失'} | {row['invoice_date']} | {'等量取消' if row['cancelled_within_1d'] else '人工调整冲销' if row['offset_by_manual_within_1d'] else ''} |"
              for row in extreme]
    lines += ["", "## 6. 时间覆盖", "",
              f"- 不完整周：{', '.join(cov['partial_weeks']) or '无'}（数据首日或末日落在周中）。",
              f"- 交易天数少于 {LOW_TRADING_DAYS} 天的周：" + (", ".join(f"{w['week']}（{w['trading_days']} 天）" for w in cov["low_trading_weeks"]) or "无") + "。0 天表示整周停业，这些周在按周汇总时会缺失，需要补 0 或单独说明。",
              f"- 周六有交易的日期：" + (", ".join(f"{d['day']}（{d['rows']:,} 行）" for d in cov["saturday_trading_days"]) or "无") + "；其余周六没有交易，按日环比时要注意。",
              f"- 连续停业超过 {CLOSURE_GAP_DAYS} 天：" + ("；".join(f"{c['last_open']} 之后到 {c['reopen']} 重新开业（间隔 {c['gap_days']} 天）" for c in cov["closures"]) or "无") + "。",
              "", "## 7. 已知局限", "",
              "- R07 删除的完全重复行可能是真实的重复购买，若删错，GMV 会偏低（影响金额见第 2 节）。",
              "- 取消单没有“原订单号”字段，无法把每笔取消和原订单一一对应；数据起点之前下的订单若在窗口内被取消，只能看到取消记录。",
              "- 数据没有商品品类字段；按品类分析需要另做映射。",
              "- 缺客户 ID 的订单无法区分新老客户。", ""]
    return "\n".join(lines)


def write_reports(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """写出 JSON（给程序和 Agent 读）和 Markdown（给人读）。内容不含运行时间，重跑结果不变。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "data_quality.json"
    md_path = out_dir / "data_quality.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return json_path, md_path
