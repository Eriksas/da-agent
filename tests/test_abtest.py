"""AB 检验：手算例子、与 scipy 原始数据检验的一致性、用蒙特卡洛模拟验证样本量公式。"""

import numpy as np
import pytest
from scipy import stats

from da_agent.abtest import mean_test, proportion_test, sample_size_proportions, srm_check


def test_proportion_test_by_hand() -> None:
    """对照 200/2000 = 10%，实验 250/2000 = 12.5%。
    合并比例 0.1125，标准误 √(0.1125 × 0.8875 × (1/2000 + 1/2000)) = 0.009992，z = 0.025 / 0.009992 = 2.502。"""
    result = proportion_test(200, 2000, 250, 2000)
    assert result["difference"] == pytest.approx(0.025)
    assert result["z"] == pytest.approx(2.502, abs=1e-3)
    assert result["p_value"] == pytest.approx(0.01235, abs=1e-4)
    assert result["significant"] is True
    se = (0.1 * 0.9 / 2000 + 0.125 * 0.875 / 2000) ** 0.5  # 非合并标准误 0.009984
    assert result["confidence_interval"] == pytest.approx([0.025 - 1.959964 * se, 0.025 + 1.959964 * se], abs=1e-5)
    assert result["relative_lift"] == pytest.approx(0.25)


def test_proportion_test_warns_on_tiny_counts() -> None:
    assert proportion_test(2, 20, 5, 20)["warnings"]


@pytest.mark.parametrize("args", [(5, 0, 1, 10), (11, 10, 1, 10), (-1, 10, 1, 10)])
def test_proportion_test_rejects_invalid_counts(args: tuple) -> None:
    with pytest.raises(ValueError):
        proportion_test(*args)


def test_mean_test_matches_scipy_on_raw_data() -> None:
    rng = np.random.default_rng(7)
    control, treatment = rng.normal(100, 20, 400), rng.normal(104, 30, 300)
    expected = stats.ttest_ind(treatment, control, equal_var=False)
    result = mean_test(control.mean(), control.std(ddof=1), 400, treatment.mean(), treatment.std(ddof=1), 300)
    assert result["t"] == pytest.approx(expected.statistic, abs=1e-4)
    assert result["p_value"] == pytest.approx(expected.pvalue, abs=1e-6)


def test_sample_size_textbook_value() -> None:
    """基线 10%，想检测到 +2 个百分点，α = 0.05，把握度 80%：每组约 3841。"""
    assert sample_size_proportions(0.10, 0.02) == 3841


def test_sample_size_gives_about_80_percent_power_in_simulation() -> None:
    """用算出的样本量模拟 4000 次实验，真实差异存在时，约 80% 的实验应判为显著。"""
    n = sample_size_proportions(0.10, 0.02)
    rng = np.random.default_rng(42)
    control, treatment = rng.binomial(n, 0.10, 4000), rng.binomial(n, 0.12, 4000)
    power = np.mean([proportion_test(int(c), n, int(t), n)["significant"] for c, t in zip(control, treatment)])
    assert power == pytest.approx(0.80, abs=0.025)


def test_srm_check() -> None:
    """5000 对 5200：卡方 = (100² + 100²) / 5100 = 3.92，p = 0.048，在 0.001 的阈值下不算失衡。"""
    ok = srm_check(5000, 5200)
    assert ok["chi2"] == pytest.approx(3.9216, abs=1e-4)
    assert ok["sample_ratio_mismatch"] is False
    assert srm_check(5000, 5600)["sample_ratio_mismatch"] is True


def test_results_are_json_serializable() -> None:
    """结果要能直接转成 JSON 交给模型；numpy 的布尔值和数值类型会让转换失败。"""
    import json

    json.dumps(proportion_test(200, 2000, 250, 2000))
    json.dumps(mean_test(100, 20, 400, 104, 30, 300))
    json.dumps(srm_check(5000, 5200))
