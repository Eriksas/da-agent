"""calculate 工具背后的计算器：能算四则运算，其他一律拒绝。"""

import pytest

from da_agent.calculator import MAX_LENGTH, calculate


@pytest.mark.parametrize("expression, expected", [
    ("1 + 2 * 3", 7.0),
    ("(315255.39 - 309381.59) / 309381.59", (315255.39 - 309381.59) / 309381.59),
    ("-5 + +3", -2.0),
    ("483725.06 - 168469.6", 483725.06 - 168469.6),
    ("115065 / 5", 23013.0),
], ids=["precedence", "change-rate", "signs", "subtract", "per-day"])
def test_arithmetic(expression: str, expected: float) -> None:
    assert calculate(expression) == pytest.approx(expected)


@pytest.mark.parametrize("expression, message", [
    ("__import__('os').remove('x')", "只支持"),  # 函数调用：eval 会执行它，这里在语法树检查时就被拒绝
    ("open('.env').read()", "只支持"),
    ("x + 1", "只支持"),
    ("2 ** 1000", "只支持"),  # 乘方可以造出极大的数，不支持
    ("7 // 2", "只支持"),
    ("True + 1", "只支持"),
    ("'1' + '2'", "只支持"),
    ("168,469.60 - 1", "千分位"),
    ("1.9% * 2", "不要写 %"),
    ("1 / (2 - 2)", "除数为 0"),
    ("1e308 * 10", "不是有限数"),
    ("1 +", "写法有误"),
    ("1" + "+1" * MAX_LENGTH, "太长"),
], ids=["import", "open-file", "name", "power", "floor-div", "bool", "string", "comma", "percent", "zero",
        "overflow", "syntax", "too-long"])
def test_everything_else_is_refused(expression: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        calculate(expression)
