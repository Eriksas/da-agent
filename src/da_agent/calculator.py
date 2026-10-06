"""calculate 工具背后的计算器：只做四则运算。

为什么需要：模型自己心算的数字（求和、相减、占比、变化率）在工具输出里找不到，核查时只能判为“没有出处”，
而且确实可能算错（M6 评测里出现过把 +9,583.93% 写成 +95,839.3%）。让模型把算式交给这个工具，
结果就进入工具输出，有了出处；算式本身也留在运行记录里，可以复查。

为什么不用 eval()：eval 会执行任意 Python 代码（读文件、删文件都可以）。这里先把表达式解析成语法树，
逐个检查节点，只允许数字、加减乘除、正负号和括号，其他一律拒绝。
"""

import ast
import math
import operator

OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
MAX_LENGTH = 200  # 足够写下一个变化率或几项求和；更长的式子请拆成几步，也方便复查


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):  # 排除 True/False、字符串和复数
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    raise ValueError("只支持数字、+ - * / 和括号，不支持函数、变量、乘方等其他写法")


def calculate(expression: str) -> float:
    """计算四则运算表达式。写法不对、除以 0、结果不是有限数时报错，错误说明会交还给模型。"""
    if len(expression) > MAX_LENGTH:
        raise ValueError(f"表达式太长（上限 {MAX_LENGTH} 个字符），请拆成几步计算")
    if "," in expression or "，" in expression:
        raise ValueError("数字不要带千分位逗号：168,469.60 写成 168469.60")
    if "%" in expression:
        raise ValueError("不要写 %：百分数写成小数，例如 1.9% 写成 0.019")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError):
        raise ValueError(f"表达式写法有误：{expression}") from None
    try:
        value = _evaluate(tree.body)
    except ZeroDivisionError:
        raise ValueError("除数为 0") from None
    if not math.isfinite(value):
        raise ValueError("结果太大或不是有限数")
    return value
