"""AB 实验的统计检验。输入是各组的汇总数字，不需要逐行明细。

返回值全部转成 Python 原生类型（float、bool）：scipy 返回的是 numpy 类型，
numpy 的布尔值无法直接转成 JSON，M3 把结果交给模型时会出错。

- proportion_test：转化率类指标，两比例 z 检验
- mean_test：客单价类指标，Welch t 检验（不假设两组方差相等）
- sample_size_proportions：实验开始前，每组至少需要多少样本
- srm_check：两组样本比例是否符合设计（样本比例失衡检验，SRM）；不通过时先查分流，别看效果
"""

from math import ceil, sqrt
from typing import Any

from scipy import stats


def proportion_test(control_success: int, control_total: int, treatment_success: int, treatment_total: int,
                    alpha: float = 0.05) -> dict[str, Any]:
    """两比例 z 检验。检验统计量用合并比例（原假设下两组相同），置信区间用非合并标准误。"""
    for success, total in ((control_success, control_total), (treatment_success, treatment_total)):
        if total <= 0 or not 0 <= success <= total:
            raise ValueError("每组样本数必须大于 0，成功数必须在 0 到样本数之间")
    p1, p2 = control_success / control_total, treatment_success / treatment_total
    diff = p2 - p1
    pooled = (control_success + treatment_success) / (control_total + treatment_total)
    se_pooled = sqrt(pooled * (1 - pooled) * (1 / control_total + 1 / treatment_total))
    z = diff / se_pooled if se_pooled else None
    p_value = float(2 * stats.norm.sf(abs(z))) if z is not None else None
    se = sqrt(p1 * (1 - p1) / control_total + p2 * (1 - p2) / treatment_total)
    margin = float(stats.norm.ppf(1 - alpha / 2)) * se
    warnings = []
    if min(control_success, control_total - control_success, treatment_success, treatment_total - treatment_success) < 10:
        warnings.append("某组的成功数或失败数少于 10，正态近似不可靠，结论需谨慎。")
    return {
        "method": "两比例 z 检验（检验用合并比例，置信区间用非合并标准误）",
        "control_rate": round(p1, 6), "treatment_rate": round(p2, 6),
        "difference": round(diff, 6), "relative_lift": round(diff / p1, 6) if p1 else None,
        "z": None if z is None else round(z, 4), "p_value": None if p_value is None else round(p_value, 6),
        "confidence_interval": [round(diff - margin, 6), round(diff + margin, 6)], "confidence_level": 1 - alpha,
        "significant": p_value is not None and p_value < alpha, "warnings": warnings,
    }


def mean_test(control_mean: float, control_std: float, control_n: int,
              treatment_mean: float, treatment_std: float, treatment_n: int, alpha: float = 0.05) -> dict[str, Any]:
    """Welch t 检验：用两组的均值、标准差（样本标准差）和样本量。"""
    if control_n < 2 or treatment_n < 2 or control_std < 0 or treatment_std < 0:
        raise ValueError("每组至少 2 个样本，标准差不能为负")
    v1, v2 = control_std ** 2 / control_n, treatment_std ** 2 / treatment_n
    se = sqrt(v1 + v2)
    if se == 0:
        raise ValueError("两组标准差都为 0，无法检验")
    diff = treatment_mean - control_mean
    df = (v1 + v2) ** 2 / (v1 ** 2 / (control_n - 1) + v2 ** 2 / (treatment_n - 1))  # Welch–Satterthwaite 自由度
    t = diff / se
    p_value = float(2 * stats.t.sf(abs(t), df))
    margin = float(stats.t.ppf(1 - alpha / 2, df)) * se
    warnings = []
    if min(control_n, treatment_n) < 30:
        warnings.append("某组样本少于 30，t 检验对非正态分布（如长尾的客单价）不稳健，结论需谨慎。")
    return {
        "method": "Welch t 检验（不假设两组方差相等）",
        "difference": round(diff, 6), "relative_lift": round(diff / control_mean, 6) if control_mean else None,
        "t": round(t, 4), "df": round(df, 2), "p_value": round(p_value, 6),
        "confidence_interval": [round(diff - margin, 6), round(diff + margin, 6)], "confidence_level": 1 - alpha,
        "significant": p_value < alpha, "warnings": warnings,
    }


def sample_size_proportions(baseline_rate: float, min_detectable_effect: float,
                            alpha: float = 0.05, power: float = 0.8) -> int:
    """每组所需样本量。min_detectable_effect 是想检测到的绝对差，例如从 10% 到 12% 填 0.02。"""
    p1, p2 = baseline_rate, baseline_rate + min_detectable_effect
    if not (0 < p1 < 1 and 0 < p2 < 1) or min_detectable_effect == 0:
        raise ValueError("基线转化率和目标转化率都必须在 0 到 1 之间，且效果不能为 0")
    z_alpha, z_beta = float(stats.norm.ppf(1 - alpha / 2)), float(stats.norm.ppf(power))
    p_bar = (p1 + p2) / 2
    n = (z_alpha * sqrt(2 * p_bar * (1 - p_bar)) + z_beta * sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2 / (p2 - p1) ** 2
    return ceil(n)


def srm_check(control_n: int, treatment_n: int, expected_control_share: float = 0.5,
              alpha: float = 0.001) -> dict[str, Any]:
    """卡方拟合优度检验两组样本比例。SRM 是护栏检查，惯例用更严格的阈值（0.001）避免误报。"""
    total = control_n + treatment_n
    if total <= 0 or not 0 < expected_control_share < 1:
        raise ValueError("样本总数必须大于 0，预期比例必须在 0 到 1 之间")
    expected = [total * expected_control_share, total * (1 - expected_control_share)]
    chi2, p_value = stats.chisquare([control_n, treatment_n], f_exp=expected)
    mismatch = bool(p_value < alpha)
    return {
        "method": "卡方拟合优度检验",
        # 观察值和期望值并排给出：说明“偏了多少”时直接引用，不用自己算
        "total": total, "observed_control_share": round(control_n / total, 6),
        "observed_treatment_share": round(treatment_n / total, 6), "expected_control_share": expected_control_share,
        "expected_control": round(expected[0], 2), "expected_treatment": round(expected[1], 2),
        "control_minus_expected": round(control_n - expected[0], 2), "treatment_minus_control": treatment_n - control_n,
        "chi2": round(float(chi2), 4), "p_value": round(float(p_value), 6), "sample_ratio_mismatch": mismatch,
        "advice": "样本比例与设计不符，先排查分流和埋点，暂不解读实验效果。" if mismatch else "样本比例与设计相符。",
    }
