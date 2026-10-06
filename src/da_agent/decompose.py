"""GMV 拆解：用连环替代法把 GMV 的变化分到“客户数、人均订单数、客单价”三个因子上。

恒等式：GMV = 活跃客户数 × 人均订单数 × 可识别客户客单价 + 缺客户 ID 的 GMV
（缺客户 ID 的部分无法拆成客户数 × 人均订单，单独列一项。）

连环替代法：按顺序把因子逐个从基期值换成当期值，每换一个，乘积的变化就是这个因子的贡献；
各项贡献相加正好等于总变化。局限是替换顺序会影响结果，所以同时给出：
- 全部 6 种顺序下每个因子贡献的最小值和最大值（看结论对顺序有多敏感）
- 6 种顺序的平均值，即 Shapley 分解，结果和顺序无关
"""

from itertools import permutations
from math import prod
from typing import Any

import duckdb

from .metrics import METRICS, compare_periods, comparison_warnings, week_warnings, weekly_row

FACTORS = ("active_customers", "orders_per_customer", "identified_aov")  # 默认顺序：量 → 频 → 价
IDENTITY = "GMV = 活跃客户数 × 人均订单数 × 可识别客户客单价 + 缺客户 ID 的 GMV"


def chain_substitution(base: dict[str, float], current: dict[str, float],
                       order: tuple[str, ...]) -> dict[str, float]:
    """按 order 依次把因子从基期值换成当期值，返回每个因子的贡献。"""
    values = {factor: base[factor] for factor in order}
    before = prod(values.values())
    contributions = {}
    for factor in order:
        values[factor] = current[factor]
        after = prod(values.values())
        contributions[factor] = after - before
        before = after
    return contributions


def shapley(base: dict[str, float], current: dict[str, float], factors: tuple[str, ...]) -> dict[str, float]:
    """全部替换顺序下贡献的平均值（Shapley 分解），与顺序无关。"""
    orders = list(permutations(factors))
    results = [chain_substitution(base, current, order) for order in orders]
    return {factor: sum(result[factor] for result in results) / len(orders) for factor in factors}


def order_range(base: dict[str, float], current: dict[str, float],
                factors: tuple[str, ...]) -> dict[str, tuple[float, float]]:
    """每个因子在全部替换顺序下贡献的最小值和最大值。"""
    results = [chain_substitution(base, current, order) for order in permutations(factors)]
    return {factor: (min(r[factor] for r in results), max(r[factor] for r in results)) for factor in factors}


def decompose_gmv(con: duckdb.DuckDBPyConnection, week: str, compare: str = "wow") -> dict[str, Any]:
    """拆解某周相对基期的 GMV 变化。"""
    period = compare_periods(week, compare)
    current, base = weekly_row(con, week), weekly_row(con, period["base"])
    for role, row in (("当期", current), ("基期", base)):
        if not row["active_customers"] or not row["identified_orders"]:
            raise ValueError(f"{role} {row['week']} 没有可识别客户的订单，无法按客户数 × 人均订单 × 客单价拆解")
    chain = chain_substitution(base, current, FACTORS)
    fair = shapley(base, current, FACTORS)
    ranges = order_range(base, current, FACTORS)
    unidentified_change = current["unidentified_gmv"] - base["unidentified_gmv"]
    total_change = current["gmv"] - base["gmv"]
    explained = sum(chain.values()) + unidentified_change

    def factor_values(row: dict[str, Any]) -> dict[str, float]:
        return {f: round(row[f], 4 if METRICS[f]["unit"] != "money" else 2) for f in (*FACTORS, "unidentified_gmv", "gmv")}

    return {
        "tool": "decompose_gmv", "period": period, "identity": IDENTITY,
        "method": "连环替代法，默认顺序：活跃客户数 → 人均订单数 → 客单价",
        "base": factor_values(base), "current": factor_values(current),
        "total_change": round(total_change, 2),
        "contributions": [
            *({"factor": f, "label": METRICS[f]["label"],
               "chain": round(chain[f], 2), "shapley": round(fair[f], 2),
               "range_across_orders": [round(ranges[f][0], 2), round(ranges[f][1], 2)],
               "share_of_total_change": round(fair[f] / total_change, 4) if total_change else None}
              for f in FACTORS),
            {"factor": "unidentified_gmv", "label": METRICS["unidentified_gmv"]["label"],
             "chain": round(unidentified_change, 2), "shapley": round(unidentified_change, 2),
             "range_across_orders": [round(unidentified_change, 2)] * 2,
             "share_of_total_change": round(unidentified_change / total_change, 4) if total_change else None},
        ],
        "check": {"contributions_sum_to_total": abs(explained - total_change) < 0.01},
        "notes": ["chain 是默认顺序下的贡献；shapley 是 6 种顺序的平均，与顺序无关；两者相差大时，说明结论对顺序敏感。",
                  "share_of_total_change 按 shapley 计算；因子反向变化时占比可能超过 100% 或为负。"],
        "warnings": (week_warnings(con, week) + week_warnings(con, period["base"], role="基期")
                     + comparison_warnings(con, week, period["base"])),
    }
