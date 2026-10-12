"""数据看板（M12）：把清洗后的电商数据汇总成一份 JSON，网站上的看板页面只负责画图。

为什么先汇总成 JSON：网站由 GitHub Actions 生成，那里没有 44MB 的原始数据；汇总结果只有几百 KB，
提交到仓库里（reports/dashboard/dashboard.json），网页上的每个数字都能在这份文件里找到。数据更新后运行
`da-agent dashboard-data` 重新生成。

筛选：3 个时间范围 × 3 个市场 = 9 份切片，每份切片都是完整的一套数字，切换筛选时所有图表一起变。

口径（和分析工具一致）：
- GMV：商品销售额（下单口径），含之后被取消的订单；净销售额 = GMV − 取消金额；取消率 = 取消金额 ÷ GMV
- 新客：全局首次购买落在当月的客户（按月，不是分析工具里的按周）。数据从 2009-12 开始，那个月的客户全部算作新客
- 商品排名和集中度按净销售额：一笔 80,995 件、12 分钟后就被取消的订单，按下单口径会让一个商品排进前 5
"""

import json
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from .cleaning import RULES, fetch_one, fetch_rows
from .paths import REPORTS_DIR

DASHBOARD_PATH = REPORTS_DIR / "dashboard" / "dashboard.json"
UK = "United Kingdom"
MARKETS = {
    "all": ("全部市场", "TRUE"),
    "uk": ("英国", f"country = '{UK}'"),
    "intl": ("海外", f"coalesce(country, '') <> '{UK}'"),  # 含国家不明确的记录
}
TOP_N = 10
COUNTRY_LABELS = {
    "Australia": "澳大利亚", "Austria": "奥地利", "Bahrain": "巴林", "Belgium": "比利时", "Bermuda": "百慕大",
    "Brazil": "巴西", "Canada": "加拿大", "Channel Islands": "海峡群岛", "Cyprus": "塞浦路斯", "Czech Republic": "捷克",
    "Denmark": "丹麦", "EIRE": "爱尔兰", "Finland": "芬兰", "France": "法国", "Germany": "德国", "Greece": "希腊",
    "Hong Kong": "中国香港", "Iceland": "冰岛", "Israel": "以色列", "Italy": "意大利", "Japan": "日本", "Korea": "韩国",
    "Lebanon": "黎巴嫩", "Lithuania": "立陶宛", "Malta": "马耳他", "Netherlands": "荷兰", "Nigeria": "尼日利亚",
    "Norway": "挪威", "Poland": "波兰", "Portugal": "葡萄牙", "RSA": "南非", "Saudi Arabia": "沙特阿拉伯",
    "Singapore": "新加坡", "Spain": "西班牙", "Sweden": "瑞典", "Switzerland": "瑞士", "Thailand": "泰国",
    "USA": "美国", "United Arab Emirates": "阿联酋", "United Kingdom": "英国", "West Indies": "西印度群岛",
    "Unspecified": "国家不明确", "European Community": "国家不明确",
}


def _money(value: float | None) -> float:
    return round(float(value or 0), 2) + 0.0  # + 0.0：浮点误差留下的 -0.0 变成 0.0


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _months(start: date, end: date) -> list[str]:
    """start 到 end（不含）之间的每个月，YYYY-MM。"""
    months, current = [], date(start.year, start.month, 1)
    while current < end:
        months.append(f"{current:%Y-%m}")
        current = date(current.year + current.month // 12, current.month % 12 + 1, 1)
    return months


def _periods(first: date, last: date) -> dict[str, dict[str, Any]]:
    """3 个时间范围：全部、2010 年、2011 年（到数据截止日，对比 2010 年同期）。"""
    end = last + timedelta(days=1)
    return {
        "all": {"label": "全部", "note": f"{first:%Y-%m-%d} 至 {last:%Y-%m-%d}", "start": first, "end": end},
        "2010": {"label": "2010 年", "note": "1 月 1 日至 12 月 31 日", "start": date(2010, 1, 1), "end": date(2011, 1, 1)},
        "2011": {"label": "2011 年", "note": f"1 月 1 日至 {last.month} 月 {last.day} 日（数据截止）",
                 "start": date(2011, 1, 1), "end": end,
                 "compare": {"label": "比 2010 年同期", "start": date(2010, 1, 1), "end": end.replace(year=2010)}},
    }


def _kpis(con: duckdb.DuckDBPyConnection, start: date, end: date, market: str) -> dict[str, Any]:
    """一个时间范围、一个市场的核心指标。"""
    s = fetch_one(con, f"""
        SELECT sum(amount) AS gmv, count(DISTINCT invoice) AS orders, count(DISTINCT customer_id) AS customers,
               coalesce(sum(amount) FILTER (WHERE customer_id IS NULL), 0) AS unidentified_gmv
        FROM sales WHERE invoice_date >= ? AND invoice_date < ? AND {market}""", [start, end])
    repeat = fetch_one(con, f"""
        SELECT count(*) FILTER (WHERE n >= 2) AS repeaters FROM (
            SELECT customer_id, count(DISTINCT invoice) AS n FROM sales
            WHERE customer_id IS NOT NULL AND invoice_date >= ? AND invoice_date < ? AND {market} GROUP BY 1)""",
                     [start, end])
    cancelled = fetch_one(con, f"""
        SELECT -coalesce(sum(amount), 0) AS c FROM cancellations
        WHERE invoice_date >= ? AND invoice_date < ? AND {market}""", [start, end])["c"]
    gmv = float(s["gmv"] or 0)
    return {"gmv": _money(gmv), "net_sales": _money(gmv - cancelled), "cancelled_amount": _money(cancelled),
            "cancel_rate": _ratio(cancelled, gmv), "orders": s["orders"],
            "aov": round(gmv / s["orders"], 2) if s["orders"] else None, "active_customers": s["customers"],
            "repeat_rate": _ratio(repeat["repeaters"], s["customers"]),
            "unidentified_share": _ratio(s["unidentified_gmv"], gmv)}


def _monthly(con: duckdb.DuckDBPyConnection, start: date, end: date, market: str,
             first: date, last: date) -> list[dict[str, Any]]:
    """每月 GMV、订单、客户，以及 GMV 按新客 / 老客 / 缺客户 ID 拆开。缺数据的月份记为 0，不会消失。"""
    rows = {r["month"]: r for r in fetch_rows(con, f"""
        SELECT strftime(x.invoice_date, '%Y-%m') AS month, sum(x.amount) AS gmv, count(DISTINCT x.invoice) AS orders,
               count(DISTINCT x.customer_id) AS customers, count(DISTINCT x.invoice_date::DATE) AS trading_days,
               coalesce(sum(x.amount) FILTER (WHERE x.customer_id IS NULL), 0) AS unidentified_gmv,
               coalesce(sum(x.amount) FILTER (WHERE date_trunc('month', f.first_time) = date_trunc('month', x.invoice_date)), 0)
                   AS new_gmv
        FROM sales x LEFT JOIN dash_first f USING (customer_id)
        WHERE x.invoice_date >= ? AND x.invoice_date < ? AND {market} GROUP BY 1""", [start, end])}
    cancelled = {r["month"]: r["c"] for r in fetch_rows(con, f"""
        SELECT strftime(invoice_date, '%Y-%m') AS month, -sum(amount) AS c FROM cancellations
        WHERE invoice_date >= ? AND invoice_date < ? AND {market} GROUP BY 1""", [start, end])}
    out = []
    for month in _months(start, end):
        r = rows.get(month, {})
        year, mon = map(int, month.split("-"))
        partial = (date(year, mon, monthrange(year, mon)[1]) > last) or (date(year, mon, 1) < first)
        gmv, new, unidentified = (float(r.get(k) or 0) for k in ("gmv", "new_gmv", "unidentified_gmv"))
        out.append({"month": month, "gmv": _money(gmv), "net_sales": _money(gmv - float(cancelled.get(month) or 0)),
                    "orders": r.get("orders", 0), "customers": r.get("customers", 0),
                    "trading_days": r.get("trading_days", 0), "new_gmv": _money(new),
                    "returning_gmv": _money(gmv - new - unidentified), "unidentified_gmv": _money(unidentified),
                    "partial": partial})
    return out


def _cohorts(con: duckdb.DuckDBPyConnection, start: date, end: date, market: str) -> list[dict[str, Any]]:
    """按首购月分组的留存：第 k 个月仍有购买的客户占比（k ≥ 1，只看筛选范围内的月份）。"""
    rows = fetch_rows(con, f"""
        WITH s AS (SELECT DISTINCT x.customer_id, date_trunc('month', x.invoice_date) AS m,
                          date_trunc('month', f.first_time) AS cohort
                   FROM sales x JOIN dash_first f USING (customer_id)
                   WHERE x.invoice_date >= ? AND x.invoice_date < ? AND {market}),
             members AS (SELECT DISTINCT customer_id, cohort FROM s WHERE m = cohort)
        SELECT strftime(members.cohort, '%Y-%m') AS cohort, datediff('month', members.cohort, s.m) AS k,
               count(DISTINCT s.customer_id) AS n
        FROM members JOIN s USING (customer_id) WHERE s.m >= members.cohort GROUP BY 1, 2 ORDER BY 1, 2""",
                      [start, end])
    months = _months(start, end)
    out = []
    for cohort in months:
        counts = {r["k"]: r["n"] for r in rows if r["cohort"] == cohort}
        size = counts.get(0, 0)
        if not size:
            continue
        horizon = len(months) - months.index(cohort) - 1
        out.append({"cohort": cohort, "size": size,
                    "retention": [_ratio(counts.get(k, 0), size) for k in range(1, horizon + 1)]})
    return out


def _heatmap(con: duckdb.DuckDBPyConnection, start: date, end: date, market: str, hours: list[int]) -> list[list[int]]:
    """周一到周日 × 每个小时的订单数。每张订单按第一行的时间算一次：有一张订单从 11:59 录到 12:00，
    按行算会在两个格子里各算一次，格子加起来就比订单数多 1。"""
    counts = {(r["d"], r["h"]): r["n"] for r in fetch_rows(con, f"""
        WITH o AS (SELECT invoice, min(invoice_date) AS t FROM sales
                   WHERE invoice_date >= ? AND invoice_date < ? AND {market} GROUP BY 1)
        SELECT isodow(t) AS d, hour(t) AS h, count(*) AS n FROM o GROUP BY 1, 2""", [start, end])}
    return [[counts.get((d, h), 0) for h in hours] for d in range(1, 8)]


def _countries(con: duckdb.DuckDBPyConnection, start: date, end: date, market: str, gmv: float) -> dict[str, Any]:
    """英国占比，以及海外国家按 GMV 排名（英国一家独大，放在一张图里其他国家都看不见）。"""
    rows = fetch_rows(con, f"""
        SELECT coalesce(country, '未知') AS country, sum(amount) AS gmv, count(DISTINCT customer_id) AS customers,
               count(DISTINCT invoice) AS orders
        FROM sales WHERE invoice_date >= ? AND invoice_date < ? AND {market} GROUP BY 1""", [start, end])
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:  # “国家不明确”的两种写法合成一组，和分析工具的下钻一致
        label = COUNTRY_LABELS.get(r["country"], r["country"])
        g = grouped.setdefault(label, {"label": label, "names": [], "gmv": 0.0, "customers": 0, "orders": 0})
        g["names"].append(r["country"])
        g["gmv"] += float(r["gmv"])
        g["customers"] += r["customers"]
        g["orders"] += r["orders"]
    uk = grouped.pop(COUNTRY_LABELS[UK], None)
    ranked = sorted(grouped.values(), key=lambda g: (-g["gmv"], g["label"]))
    top = [{**g, "names": sorted(g["names"]), "gmv": _money(g["gmv"])} for g in ranked[:TOP_N]]
    rest = ranked[TOP_N:]
    return {"uk_share": _ratio(uk["gmv"], gmv) if uk else None, "intl_count": len(ranked), "top": top,
            "other_gmv": _money(sum(g["gmv"] for g in rest)), "other_count": len(rest)}


def _products(con: duckdb.DuckDBPyConnection, start: date, end: date, market: str) -> dict[str, Any]:
    """商品按净销售额排名，以及集中度曲线（排名前 x% 的商品贡献了多少净销售额）。"""
    rows = fetch_rows(con, f"""
        WITH g AS (SELECT stock_code, sum(amount) AS gmv FROM sales
                   WHERE invoice_date >= ? AND invoice_date < ? AND {market} GROUP BY 1),
             c AS (SELECT stock_code, -sum(amount) AS cancelled FROM cancellations
                   WHERE invoice_date >= ? AND invoice_date < ? AND {market} GROUP BY 1)
        SELECT g.stock_code, d.description, g.gmv, coalesce(c.cancelled, 0) AS cancelled,
               g.gmv - coalesce(c.cancelled, 0) AS net_sales
        FROM g LEFT JOIN c USING (stock_code) LEFT JOIN dash_names d USING (stock_code)
        ORDER BY net_sales DESC, g.stock_code""", [start, end, start, end])
    positive = [float(r["net_sales"]) for r in rows if r["net_sales"] > 0]
    total = sum(positive)
    cumulative, running = [0.0], 0.0
    for value in positive:
        running += value
        cumulative.append(running)
    # 第 i 个点：排名前 i% 的商品（向下取整）的累计占比
    pareto = [round(cumulative[len(positive) * i // 100] / total, 4) if total else 0 for i in range(101)]
    top = [{"stock_code": r["stock_code"], "description": r["description"] or "", "net_sales": _money(r["net_sales"]),
            "gmv": _money(r["gmv"]), "cancelled": _money(r["cancelled"])} for r in rows[:TOP_N]]
    gmv_rank = sorted(rows, key=lambda r: (-r["gmv"], r["stock_code"]))[:TOP_N]
    return {"top": top, "count": len(positive), "pareto": pareto, "top20_share": pareto[20],
            "dropped_by_cancellation": [  # 按下单口径能进前 10、扣掉取消后掉出前 10 的商品
                {"stock_code": r["stock_code"], "description": r["description"] or "", "gmv": _money(r["gmv"]),
                 "cancelled": _money(r["cancelled"])}
                for r in gmv_rank if r["stock_code"] not in {t["stock_code"] for t in top}]}


def _cleaning(con: duckdb.DuckDBPyConnection, start: date, end: date, market: str) -> dict[str, Any]:
    """每条清洗规则在这个范围里命中的行数（一行可能命中多条规则）。"""
    columns = ", ".join(f"count(*) FILTER (WHERE {rule.column}) AS {rule.id}" for rule in RULES)
    counts = fetch_one(con, f"""SELECT count(*) AS raw_rows, {columns} FROM flagged
                                WHERE invoice_date >= ? AND invoice_date < ? AND {market}""", [start, end])
    sales_rows = fetch_one(con, f"""SELECT count(*) AS n FROM sales
                                    WHERE invoice_date >= ? AND invoice_date < ? AND {market}""", [start, end])["n"]
    return {"raw_rows": counts["raw_rows"], "sales_rows": sales_rows,
            "rules": [{"id": rule.id, "name": rule.name, "action": rule.action, "excludes": rule.excludes,
                       "rows": counts[rule.id]} for rule in RULES]}


def build_dashboard_data(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """汇总看板需要的全部数字。con 是 cleaning.connect 返回的连接（已建好 sales、cancellations、flagged）。"""
    con.execute("""CREATE OR REPLACE TEMP TABLE dash_first AS
                   SELECT customer_id, min(invoice_date) AS first_time FROM sales
                   WHERE customer_id IS NOT NULL GROUP BY 1""")
    # 商品名：同一编码的描述前后改过写法，用出现最多的那个（并列时取字母序靠前的），所有切片里名字一致
    con.execute("""CREATE OR REPLACE TEMP TABLE dash_names AS
                   SELECT stock_code, description FROM (
                       SELECT stock_code, description, row_number() OVER (
                           PARTITION BY stock_code ORDER BY count(*) DESC, description) AS k
                       FROM sales WHERE description IS NOT NULL GROUP BY 1, 2)
                   WHERE k = 1""")
    bounds = fetch_one(con, "SELECT min(invoice_date) AS a, max(invoice_date) AS b, min(hour(invoice_date)) AS h0, "
                            "max(hour(invoice_date)) AS h1 FROM sales")
    first, last = bounds["a"].date(), bounds["b"].date()
    hours = list(range(bounds["h0"], bounds["h1"] + 1))
    periods = _periods(first, last)
    slices = {}
    for pid, period in periods.items():
        start, end = period["start"], period["end"]
        for mid, (_, market) in MARKETS.items():
            kpi = _kpis(con, start, end, market)
            compare = period.get("compare")
            slices[f"{pid}/{mid}"] = {
                "kpi": kpi,
                "compare": _kpis(con, compare["start"], compare["end"], market) if compare else None,
                "months": _monthly(con, start, end, market, first, last),
                "cohorts": _cohorts(con, start, end, market),
                "heatmap": _heatmap(con, start, end, market, hours),
                "countries": _countries(con, start, end, market, kpi["gmv"]),
                "products": _products(con, start, end, market),
                "cleaning": _cleaning(con, start, end, market),
            }
    return {
        "generated_at": f"{datetime.now():%Y-%m-%d}",
        "data_start": f"{first:%Y-%m-%d}", "data_end": f"{bounds['b']:%Y-%m-%d %H:%M}",
        "periods": [{"id": pid, "label": p["label"], "note": p["note"],
                     "compare_label": p["compare"]["label"] if "compare" in p else None} for pid, p in periods.items()],
        "markets": [{"id": mid, "label": label} for mid, (label, _) in MARKETS.items()],
        "hours": hours,
        "slices": slices,
    }


def check_dashboard_data(data: dict[str, Any]) -> list[str]:
    """看板数字之间应当对得上的关系；返回对不上的地方（空列表表示全部对得上）。

    不需要原始数据，CI 里直接检查已提交的 JSON：各月加起来等于全年、英国加海外等于全部、
    新客 + 老客 + 缺客户 ID 等于当月 GMV、热力图格子加起来等于订单数……
    """
    problems: list[str] = []

    def close(a: float | None, b: float | None, tolerance: float, what: str) -> None:
        if a is None or b is None or abs(a - b) > tolerance:
            problems.append(f"{what}：{a} ≠ {b}")

    for key, s in data["slices"].items():
        kpi, months = s["kpi"], s["months"]
        cent = 0.01 * (len(months) + 1)  # 每个数各自四舍五入到分，允许累计误差
        close(sum(m["gmv"] for m in months), kpi["gmv"], cent, f"{key} 各月 GMV 之和")
        close(sum(m["orders"] for m in months), kpi["orders"], 0, f"{key} 各月订单数之和")
        close(kpi["net_sales"], kpi["gmv"] - kpi["cancelled_amount"], 0.02, f"{key} 净销售额")
        close(sum(map(sum, s["heatmap"])), kpi["orders"], 0, f"{key} 热力图订单数之和")
        if kpi["orders"]:
            close(kpi["aov"], kpi["gmv"] / kpi["orders"], 0.01, f"{key} 客单价")
        for m in months:
            close(m["new_gmv"] + m["returning_gmv"] + m["unidentified_gmv"], m["gmv"], 0.03, f"{key} {m['month']} 新老客拆分")
        pareto = s["products"]["pareto"]
        if s["products"]["count"] and (pareto[0] != 0 or pareto[100] != 1 or pareto != sorted(pareto)):
            problems.append(f"{key} 集中度曲线应从 0 单调升到 1")
        for c in s["cohorts"]:
            if any(r is None or not 0 <= r <= 1 for r in c["retention"]):
                problems.append(f"{key} {c['cohort']} 留存率应在 0 到 1 之间")
        if (s["compare"] is not None) != key.startswith("2011/"):
            problems.append(f"{key} 只有 2011 年有同期对比")
    for period in data["periods"]:
        whole, uk, intl = (data["slices"][f"{period['id']}/{m}"] for m in ("all", "uk", "intl"))
        for field in ("gmv", "cancelled_amount", "orders"):
            close(uk["kpi"][field] + intl["kpi"][field], whole["kpi"][field], 0.02, f"{period['id']} 英国 + 海外的 {field}")
        close(uk["cleaning"]["raw_rows"] + intl["cleaning"]["raw_rows"], whole["cleaning"]["raw_rows"], 0,
              f"{period['id']} 英国 + 海外的原始行数")
        if uk["countries"]["top"] or intl["countries"]["uk_share"] is not None:
            problems.append(f"{period['id']} 英国切片不应有海外国家，海外切片不应有英国占比")
    return problems


def write_dashboard_data(data: dict[str, Any], path: Path = DASHBOARD_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    return path


def load_dashboard_data(path: Path = DASHBOARD_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
