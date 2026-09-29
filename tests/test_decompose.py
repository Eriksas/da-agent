"""GMV 拆解：先验证纯函数（手算），再验证接上数据后的结果。"""

import pytest

from da_agent.cleaning import connect_frame
from da_agent.decompose import FACTORS, chain_substitution, decompose_gmv, order_range, shapley
from helpers import make_frame
from test_metrics import ROWS

C, F, A = FACTORS
BASE = {C: 10, F: 2, A: 50}      # 乘积 1000
CURRENT = {C: 12, F: 2.5, A: 40}  # 乘积 1200


def test_chain_substitution_by_hand() -> None:
    result = chain_substitution(BASE, CURRENT, FACTORS)
    assert result[C] == pytest.approx((12 - 10) * 2 * 50)         # 200
    assert result[F] == pytest.approx(12 * (2.5 - 2) * 50)        # 300
    assert result[A] == pytest.approx(12 * 2.5 * (40 - 50))       # -300
    assert sum(result.values()) == pytest.approx(1200 - 1000)


def test_order_changes_the_answer() -> None:
    """倒过来先换客单价：客单价的贡献从 -300 变成 -200，但总和不变。"""
    reverse = chain_substitution(BASE, CURRENT, (A, F, C))
    assert reverse[A] == pytest.approx(10 * 2 * (40 - 50))        # 先换：ΔA × C0 × F0 = -200（默认顺序是 -300）
    assert reverse[C] == pytest.approx((12 - 10) * 2.5 * 40)      # 最后换：ΔC × F1 × A1 = 200，恰好和默认顺序相同
    assert sum(reverse.values()) == pytest.approx(200)


def test_shapley_matches_closed_form() -> None:
    """三因子乘积的 Shapley 值有公式：φ_C = ΔC ×（F0A0/3 + F1A0/6 + F0A1/6 + F1A1/3），其余同理。"""
    result = shapley(BASE, CURRENT, FACTORS)
    assert result[C] == pytest.approx(2 * (100 / 3 + 125 / 6 + 80 / 6 + 100 / 3))     # 201.67
    assert result[F] == pytest.approx(0.5 * (500 / 3 + 600 / 6 + 400 / 6 + 480 / 3))  # 246.67
    assert result[A] == pytest.approx(-10 * (20 / 3 + 24 / 6 + 25 / 6 + 30 / 3))      # -248.33
    assert sum(result.values()) == pytest.approx(200)


def test_order_range() -> None:
    low, high = order_range(BASE, CURRENT, FACTORS)[C]
    assert (low, high) == (pytest.approx(2 * 80), pytest.approx(2 * 125))  # 先换客单价时最小，先换人均订单时最大


def test_decompose_gmv_on_data() -> None:
    with connect_frame(make_frame(ROWS)) as con:
        result = decompose_gmv(con, "2011-W02")
    items = {item["factor"]: item for item in result["contributions"]}
    assert result["total_change"] == 5.0
    assert items["active_customers"]["chain"] == 0.0
    assert items["orders_per_customer"]["chain"] == round(3 * (4 / 3 - 1) * (100 / 3), 2)   # 33.33
    assert items["identified_aov"]["chain"] == round(3 * (4 / 3) * (115 / 4 - 100 / 3), 2)  # -18.33
    assert items["unidentified_gmv"]["chain"] == -10.0
    assert items["orders_per_customer"]["shapley"] == round((100 / 3 + 115 / 4) / 2, 2)       # 31.04
    assert result["check"]["contributions_sum_to_total"] is True


def test_decompose_refuses_week_without_customers() -> None:
    rows = [ROWS[0], (ROWS[0][0], 3, "300011", "10001", "A", 1, "2011-01-17 10:00", 1.0, "c1", "United Kingdom")]
    with connect_frame(make_frame(rows)) as con, pytest.raises(ValueError, match="基期 2011-W02 没有可识别客户"):
        decompose_gmv(con, "2011-W03")
