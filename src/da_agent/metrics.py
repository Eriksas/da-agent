"""周度指标与维度下钻。所有数字由 SQL 计算，每个结果都带口径说明和警告。

两个工具：
- metric_summary：某周的核心指标，以及与基期（上一周或去年同周）的对比
- drilldown：把某个金额指标的变化按维度（国家、新老客、商品）拆开，看各分组贡献了多少
"""

from datetime import datetime
from typing import Any

import duckdb

from .cleaning import fetch_one, fetch_rows
from .periods import COMPARE_LABELS, LOW_TRADING_DAYS, base_week, week_monday, week_of

CENSOR_WEEKS = 52  # 距数据起点不足这么多周时，新老客划分偏向“新客”（左删失）
MIN_ORDERS = 30  # 分组订单数低于这个值，视为样本不足

# 指标口径。单位决定取几位小数、变化怎么表达：rate 类只报百分点变化，不报变化百分比。
METRICS: dict[str, dict[str, str]] = {
    "gmv": {"label": "GMV", "unit": "money", "definition": "商品销售额（下单口径）：数量 × 单价之和，含之后被取消的订单"},
    "net_sales": {"label": "净销售额", "unit": "money", "definition": "GMV − 取消金额"},
    "cancelled_amount": {"label": "取消金额", "unit": "money", "definition": "当周 C 开头发票中的商品行金额（取正）；可能对应更早下的订单"},
    "cancel_rate": {"label": "取消率", "unit": "rate", "definition": "取消金额 ÷ GMV；分子分母不一定是同一批订单"},
    "orders": {"label": "订单数", "unit": "count", "definition": "不同发票号个数"},
    "aov": {"label": "客单价", "unit": "money", "definition": "GMV ÷ 订单数"},
    "active_customers": {"label": "活跃客户数", "unit": "count", "definition": "当周有购买的可识别客户数（不含缺失客户 ID）"},
    "new_customers": {"label": "新客数", "unit": "count", "definition": "首次购买落在当周的客户；越靠近数据起点，越可能把老客算成新客"},
    "returning_customers": {"label": "老客数", "unit": "count", "definition": "活跃客户数 − 新客数"},
    "orders_per_customer": {"label": "人均订单数", "unit": "number", "definition": "可识别客户的订单数 ÷ 活跃客户数"},
    "identified_aov": {"label": "可识别客户客单价", "unit": "money", "definition": "可识别客户的 GMV ÷ 其订单数"},
    "unidentified_gmv": {"label": "缺客户 ID 的 GMV", "unit": "money", "definition": "无法归到具体客户的销售额"},
    "trading_days": {"label": "交易天数", "unit": "count", "definition": "当周有销售的日期数"},
}
DEFAULT_METRICS = ("gmv", "net_sales", "orders", "aov", "active_customers", "new_customers",
                   "returning_customers", "cancel_rate", "trading_days")
ADDITIVE_METRICS = ("gmv", "net_sales", "cancelled_amount")  # 可以按分组相加的金额指标
DIMENSIONS: dict[str, dict[str, str]] = {
    "country": {"label": "国家",
                "expr": "CASE WHEN country IN ('Unspecified', 'European Community') THEN '国家不明确' ELSE coalesce(country, '未知') END"},
    "customer_type": {"label": "新老客", "expr": "customer_type"},
    "product": {"label": "商品", "expr": "stock_code"},
}


def _ensure(con: duckdb.DuckDBPyConnection) -> None:
    """第一次调用时建分析用的表：客户首购时间、带周和新老客标签的明细、完整周日历上的周度汇总。"""
    if fetch_one(con, "SELECT count(*) AS n FROM duckdb_tables() WHERE table_name = 'weekly'")["n"]:
        return
    con.execute("""
        CREATE OR REPLACE TABLE customer_first AS
        SELECT customer_id, min(invoice_date) AS first_time FROM sales WHERE customer_id IS NOT NULL GROUP BY 1
    """)
    customer_type = """CASE WHEN x.customer_id IS NULL THEN '未知'
                            WHEN strftime(f.first_time, '%G-W%V') = strftime(x.invoice_date, '%G-W%V') THEN '新客'
                            ELSE '老客' END"""
    for view, source in (("sales_enriched", "sales"), ("cancellations_enriched", "cancellations")):
        con.execute(f"""
            CREATE OR REPLACE VIEW {view} AS
            SELECT x.*, strftime(x.invoice_date, '%G-W%V') AS week, {customer_type} AS customer_type
            FROM {source} x LEFT JOIN customer_first f USING (customer_id)
        """)
    # 先生成完整周日历再匹配，整周停业的周也会出现（指标为 0），不会在时间序列里“消失”
    con.execute("""
        CREATE OR REPLACE TABLE weekly AS
        WITH bounds AS (SELECT date_trunc('week', min(invoice_date))::DATE AS first_monday,
                               date_trunc('week', max(invoice_date))::DATE AS last_monday FROM sales),
             calendar AS (SELECT unnest(generate_series(first_monday, last_monday, INTERVAL 7 DAY))::DATE AS monday FROM bounds),
             s AS (SELECT date_trunc('week', invoice_date)::DATE AS monday,
                          sum(amount) AS gmv, count(DISTINCT invoice) AS orders,
                          count(DISTINCT customer_id) AS active_customers,
                          coalesce(sum(amount) FILTER (WHERE customer_id IS NOT NULL), 0) AS identified_gmv,
                          count(DISTINCT invoice) FILTER (WHERE customer_id IS NOT NULL) AS identified_orders,
                          count(DISTINCT customer_id) FILTER (WHERE customer_type = '新客') AS new_customers,
                          count(DISTINCT invoice_date::DATE) AS trading_days
                   FROM sales_enriched GROUP BY 1),
             c AS (SELECT date_trunc('week', invoice_date)::DATE AS monday, -sum(amount) AS cancelled_amount
                   FROM cancellations GROUP BY 1)
        SELECT strftime(monday, '%G-W%V') AS week, monday,
               coalesce(gmv, 0) AS gmv, coalesce(orders, 0) AS orders,
               coalesce(active_customers, 0) AS active_customers, coalesce(identified_gmv, 0) AS identified_gmv,
               coalesce(identified_orders, 0) AS identified_orders, coalesce(new_customers, 0) AS new_customers,
               coalesce(trading_days, 0) AS trading_days, coalesce(cancelled_amount, 0) AS cancelled_amount
        FROM calendar LEFT JOIN s USING (monday) LEFT JOIN c USING (monday)
        ORDER BY monday
    """)


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def weekly_row(con: duckdb.DuckDBPyConnection, week: str) -> dict[str, Any]:
    """某一周的全部指标（未取整）。周不在数据范围内时报错。"""
    _ensure(con)
    week_monday(week)  # 先校验格式
    rows = fetch_rows(con, "SELECT * FROM weekly WHERE week = ?", [week])
    if not rows:
        bounds = fetch_one(con, "SELECT min(week) AS first, max(week) AS last FROM weekly")
        raise ValueError(f"{week} 不在数据范围内（{bounds['first']} 至 {bounds['last']}）")
    row = rows[0]
    row["net_sales"] = row["gmv"] - row["cancelled_amount"]
    row["cancel_rate"] = _ratio(row["cancelled_amount"], row["gmv"])
    row["aov"] = _ratio(row["gmv"], row["orders"])
    row["returning_customers"] = row["active_customers"] - row["new_customers"]
    row["orders_per_customer"] = _ratio(row["identified_orders"], row["active_customers"])
    row["identified_aov"] = _ratio(row["identified_gmv"], row["identified_orders"])
    row["unidentified_gmv"] = row["gmv"] - row["identified_gmv"]
    return row


def round_value(value: float | None, unit: str) -> float | int | None:
    """按单位取整：金额 2 位小数，比率和普通数值 4 位，计数取整。"""
    if value is None:
        return None
    if unit == "money":
        return round(value, 2)
    if unit == "count":
        return int(round(value))
    return round(value, 4)


def week_warnings(con: duckdb.DuckDBPyConnection, week: str, *, role: str = "当期",
                  customer_type: bool = False) -> list[str]:
    """这一周在比较时需要注意的问题：不完整、停业、节假日、左删失、极端大额行。"""
    _ensure(con)
    row = weekly_row(con, week)
    bounds = fetch_one(con, "SELECT min(invoice_date) AS first_time, max(invoice_date) AS last_time FROM sales")
    first_time: datetime = bounds["first_time"]
    last_time: datetime = bounds["last_time"]
    warnings = []
    if week == week_of(first_time.date()) and first_time.isoweekday() > 1:
        warnings.append(f"{role} {week} 是不完整周：数据从 {first_time:%Y-%m-%d}（周{'一二三四五六日'[first_time.weekday()]}）开始。")
    if week == week_of(last_time.date()) and last_time.isoweekday() < 7:
        warnings.append(f"{role} {week} 是不完整周：数据截止到 {last_time:%Y-%m-%d %H:%M}（周{'一二三四五六日'[last_time.weekday()]}）。")
    if row["trading_days"] == 0:
        warnings.append(f"{role} {week} 整周没有交易（停业），各项指标为 0。")
    elif row["trading_days"] < LOW_TRADING_DAYS:
        warnings.append(f"{role} {week} 只有 {row['trading_days']} 个交易日（节假日前后），与正常周比较会失真。")
    if customer_type and (week_monday(week) - first_time.date()).days < CENSOR_WEEKS * 7:
        warnings.append(f"{role} {week} 距数据起点不足 {CENSOR_WEEKS} 周：起点之前的购买看不到，部分老客会被算成新客。")
    for line in fetch_rows(con, """
            SELECT invoice, description, amount, cancelled_within_1d, offset_by_manual_within_1d
            FROM extreme_review WHERE strftime(invoice_date, '%G-W%V') = ? ORDER BY abs(amount) DESC""", [week]):
        note = ("，24 小时内被等量取消（GMV 含它，净销售额已扣除）" if line["cancelled_within_1d"]
                else "，被人工调整冲销（GMV 和净销售额都含它，净销售额偏高）" if line["offset_by_manual_within_1d"] else "")
        warnings.append(f"{role} {week} 含极端大额行：发票 {line['invoice']}（{line['description']}）金额 {line['amount']:,.2f}{note}。")
    return warnings


def compare_periods(week: str, compare: str) -> dict[str, str]:
    """当期、基期和对比方式。"""
    return {"current": week, "base": base_week(week, compare), "compare": compare, "compare_label": COMPARE_LABELS[compare]}


def metric_summary(con: duckdb.DuckDBPyConnection, week: str, compare: str = "wow",
                   metrics: tuple[str, ...] | list[str] | None = None) -> dict[str, Any]:
    """某周的核心指标及与基期的对比。"""
    names = list(metrics or DEFAULT_METRICS)
    unknown = [name for name in names if name not in METRICS]
    if unknown:
        raise ValueError(f"未知指标：{unknown}；可选：{list(METRICS)}")
    period = compare_periods(week, compare)
    current, base = weekly_row(con, week), weekly_row(con, period["base"])
    rows = []
    for name in names:
        unit = METRICS[name]["unit"]
        cur, prev = current[name], base[name]
        change = None if cur is None or prev is None else cur - prev
        change_pct = None if unit == "rate" or change is None or not prev else change / prev
        rows.append({"metric": name, "label": METRICS[name]["label"], "unit": unit,
                     "current": round_value(cur, unit), "base": round_value(prev, unit),
                     "change": round_value(change, "number" if unit == "rate" else unit),
                     "change_pct": None if change_pct is None else round(change_pct, 4)})
    customer_metrics = any(name in ("new_customers", "returning_customers") for name in names)
    return {
        "tool": "metric_summary", "period": period, "metrics": rows,
        "definitions": {name: METRICS[name]["definition"] for name in names},
        "notes": ["rate 类指标的 change 是百分点变化（0.01 = 1 个百分点），不计算变化百分比。"],
        "sample_size": {"current_orders": current["orders"], "base_orders": base["orders"]},
        "warnings": week_warnings(con, week, customer_type=customer_metrics)
                    + week_warnings(con, period["base"], role="基期", customer_type=customer_metrics),
    }


def drilldown(con: duckdb.DuckDBPyConnection, week: str, compare: str = "wow", metric: str = "gmv",
              dimension: str = "country", top_n: int = 5, min_orders: int = MIN_ORDERS) -> dict[str, Any]:
    """把金额指标的变化按维度拆开：每个分组变了多少，占总变化的多少。"""
    if metric not in ADDITIVE_METRICS:
        raise ValueError(f"下钻只支持可相加的金额指标：{list(ADDITIVE_METRICS)}")
    if dimension not in DIMENSIONS:
        raise ValueError(f"未知维度：{dimension}；可选：{list(DIMENSIONS)}")
    if not 1 <= top_n <= 20:
        raise ValueError("top_n 必须在 1 到 20 之间")
    _ensure(con)
    period = compare_periods(week, compare)
    current_total, base_total = weekly_row(con, week), weekly_row(con, period["base"])
    expr = DIMENSIONS[dimension]["expr"]
    rows = fetch_rows(con, f"""
        WITH s AS (SELECT {expr} AS segment, week, sum(amount) AS gmv, count(DISTINCT invoice) AS orders,
                          any_value(description) AS description
                   FROM sales_enriched WHERE week IN (?, ?) GROUP BY ALL),
             c AS (SELECT {expr} AS segment, week, -sum(amount) AS cancelled_amount, any_value(description) AS description
                   FROM cancellations_enriched WHERE week IN (?, ?) GROUP BY ALL)
        SELECT coalesce(s.segment, c.segment) AS segment, coalesce(s.week, c.week) AS week,
               coalesce(s.gmv, 0) AS gmv, coalesce(s.orders, 0) AS orders,
               coalesce(c.cancelled_amount, 0) AS cancelled_amount, coalesce(s.description, c.description) AS description
        FROM s FULL OUTER JOIN c ON s.segment = c.segment AND s.week = c.week""",
        [week, period["base"], week, period["base"]])
    segments: dict[str, dict[str, Any]] = {}
    for row in rows:
        slot = "current" if row["week"] == week else "base"
        seg = segments.setdefault(row["segment"], {"segment": row["segment"], "description": row["description"],
                                                   "base": 0.0, "current": 0.0, "base_orders": 0, "current_orders": 0})
        value = {"gmv": row["gmv"], "cancelled_amount": row["cancelled_amount"],
                 "net_sales": row["gmv"] - row["cancelled_amount"]}[metric]
        seg[slot] += value
        seg[f"{slot}_orders"] += row["orders"]
    total_change = current_total[metric] - base_total[metric]
    items = sorted(segments.values(), key=lambda s: (-abs(s["current"] - s["base"]), s["segment"]))
    result_rows = []
    for seg in items:
        change = seg["current"] - seg["base"]
        result_rows.append({
            "segment": seg["segment"],
            **({"description": seg["description"]} if dimension == "product" else {}),
            "base": round(seg["base"], 2), "current": round(seg["current"], 2), "change": round(change, 2),
            "change_pct": round(change / seg["base"], 4) if seg["base"] else None,
            "share_of_total_change": round(change / total_change, 4) if total_change else None,
            "base_orders": seg["base_orders"], "current_orders": seg["current_orders"],
            "small_sample": min(seg["base_orders"], seg["current_orders"]) < min_orders,
        })
    top, rest = result_rows[:top_n], result_rows[top_n:]
    others = None
    if rest:
        others_change = sum(r["change"] for r in rest)
        others = {"segments": len(rest), "base": round(sum(r["base"] for r in rest), 2),
                  "current": round(sum(r["current"] for r in rest), 2), "change": round(others_change, 2),
                  "share_of_total_change": round(others_change / total_change, 4) if total_change else None}
    segment_sum = sum(seg["current"] - seg["base"] for seg in segments.values())
    warnings = (week_warnings(con, week, customer_type=dimension == "customer_type")
                + week_warnings(con, period["base"], role="基期", customer_type=dimension == "customer_type"))
    if any(r["share_of_total_change"] is not None and abs(r["share_of_total_change"]) > 1 for r in top):
        warnings.append("有分组反向变化，相互抵消后总变化较小，所以单个分组的贡献占比会超过 100% 或为负。")
    if any(r["small_sample"] for r in top):
        warnings.append(f"部分分组订单数少于 {min_orders}，变化可能是偶然波动，不宜单独下结论。")
    return {
        "tool": "drilldown", "period": period, "metric": metric, "metric_label": METRICS[metric]["label"],
        "dimension": dimension, "dimension_label": DIMENSIONS[dimension]["label"],
        "definition": METRICS[metric]["definition"],
        "total": {"base": round(base_total[metric], 2), "current": round(current_total[metric], 2),
                  "change": round(total_change, 2)},
        "segments": top, "others": others, "segment_count": len(result_rows),
        "check": {"segments_sum_to_total": abs(segment_sum - total_change) < 0.01},
        "warnings": warnings,
    }
