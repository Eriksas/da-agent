"""Agent 可以调用的工具：参数格式（给模型看的说明书）+ 执行（带错误兜底）。

- 参数用 pydantic 定义：同一份定义既生成给模型看的 JSON Schema，也在执行前校验模型给的参数。
- 参数不合法、工具拒绝计算、工具内部出错，都不会让程序崩溃，而是把原因交还给模型，让它改正或说明。
- 工具全部只读：只查询和计算，不修改数据、不写文件。
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .abtest import proportion_test, sample_size_proportions, srm_check
from .decompose import decompose_gmv
from .llm import INVALID_JSON_KEY
from .metrics import METRICS, drilldown, metric_summary
from .quality import build_report

LOGGER = logging.getLogger(__name__)
MAX_RESULT_CHARS = 30_000  # 单个工具结果的长度上限，防止把超大结果塞进对话

MetricName = Literal[tuple(METRICS)]  # type: ignore[valid-type]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 模型多给了参数也算错，避免它以为某个参数生效了


class WeekArgs(_Args):
    week: str = Field(pattern=r"^\d{4}-W\d{2}$", description="要分析的 ISO 周，例如 2011-W48")
    compare: Literal["wow", "yoy"] = Field("wow", description="wow：和上一周比（环比）；yoy：和去年同一周比（同比）")


class MetricSummaryArgs(WeekArgs):
    metrics: list[MetricName] = Field(default_factory=list, description="要看的指标；留空返回常用指标")


class DrilldownArgs(WeekArgs):
    metric: Literal["gmv", "net_sales", "cancelled_amount"] = Field("gmv", description="要拆开的金额指标")
    dimension: Literal["country", "customer_type", "product"] = Field(
        "country", description="按什么拆：country 国家，customer_type 新老客，product 商品")
    top_n: int = Field(5, ge=1, le=20, description="返回变化最大的前几个分组")


class NoArgs(_Args):
    pass


class ProportionTestArgs(_Args):
    control_success: int = Field(ge=0, description="对照组转化人数")
    control_total: int = Field(gt=0, description="对照组总人数")
    treatment_success: int = Field(ge=0, description="实验组转化人数")
    treatment_total: int = Field(gt=0, description="实验组总人数")
    expected_control_share: float = Field(0.5, gt=0, lt=1, description="设计的对照组流量占比，用于样本比例失衡检查")
    alpha: float = Field(0.05, gt=0, lt=0.5, description="显著性水平")


class SampleSizeArgs(_Args):
    baseline_rate: float = Field(gt=0, lt=1, description="基线转化率，例如 0.1")
    min_detectable_effect: float = Field(description="想检测到的最小绝对提升，例如从 10% 到 12% 填 0.02")
    alpha: float = Field(0.05, gt=0, lt=0.5, description="显著性水平")
    power: float = Field(0.8, gt=0, lt=1, description="统计把握度")


def _quality_overview(con: duckdb.DuckDBPyConnection, _: NoArgs) -> dict[str, Any]:
    """质量报告的精简版：口径、规则、覆盖情况、需要复核的大单。"""
    report = build_report(con)
    return {
        "tool": "data_quality_overview",
        "data_range": {"first": report["dataset"]["first_time"], "last": report["dataset"]["last_time"]},
        "clean_totals": report["clean"],
        "definitions": {name: METRICS[name]["definition"] for name in ("gmv", "net_sales", "cancelled_amount", "aov",
                                                                        "active_customers", "new_customers")},
        "rules": [{"id": r["id"], "name": r["name"], "action": r["action"], "rows": r["rows"]} for r in report["rules"]],
        "sheet_overlap_identical": report["checks"]["sheet_overlap"]["identical"],
        "coverage": {key: report["coverage"][key] for key in ("weeks", "partial_weeks", "low_trading_weeks", "closures")},
        "extreme_lines": [{key: row[key] for key in ("invoice", "description", "amount", "invoice_date",
                                                     "cancelled_within_1d", "offset_by_manual_within_1d")}
                          for row in report["extreme_lines"]],
    }


def _ab_test(_: duckdb.DuckDBPyConnection, args: ProportionTestArgs) -> dict[str, Any]:
    return {
        "tool": "ab_proportion_test",
        "srm": srm_check(args.control_total, args.treatment_total, args.expected_control_share),
        "test": proportion_test(args.control_success, args.control_total, args.treatment_success,
                                args.treatment_total, args.alpha),
    }


@dataclass(frozen=True)
class Tool:
    """一个工具：名字、给模型看的用途说明、参数格式、执行函数。"""

    name: str
    description: str
    args_model: type[_Args]
    run: Callable[[duckdb.DuckDBPyConnection, Any], dict[str, Any]]


TOOLS: dict[str, Tool] = {tool.name: tool for tool in (
    Tool("metric_summary",
         "查看某一周的核心运营指标（GMV、净销售额、订单数、客单价、活跃客户、新老客、取消率、交易天数），"
         "以及与上一周（wow）或去年同一周（yoy）的对比。返回值含指标口径、样本量和警告。",
         MetricSummaryArgs,
         lambda con, a: metric_summary(con, a.week, a.compare, a.metrics or None)),
    Tool("decompose_gmv",
         "把某周相对基期的 GMV 变化拆成：活跃客户数、人均订单数、客单价、缺客户 ID 部分各贡献多少"
         "（连环替代法，并给出与顺序无关的 Shapley 值）。用于回答“GMV 为什么变了”。",
         WeekArgs,
         lambda con, a: decompose_gmv(con, a.week, a.compare)),
    Tool("drilldown",
         "把 GMV、净销售额或取消金额的变化按维度拆开（国家、新老客、商品），返回变化最大的前 N 个分组"
         "及其占总变化的比例。用于回答“变化来自哪里”。分组订单少于 30 时会标记样本不足。"
         "返回的 others 是前 N 名以外所有分组的合计，不等于只剔除某一个分组后的结果。",
         DrilldownArgs,
         lambda con, a: drilldown(con, a.week, a.compare, a.metric, a.dimension, a.top_n)),
    Tool("data_quality_overview",
         "查看数据质量与口径：清洗规则、指标定义、不完整或停业的周、需要人工复核的极端大额订单。"
         "需要解释数据口径或异常时调用。",
         NoArgs, _quality_overview),
    Tool("ab_proportion_test",
         "AB 实验转化率检验：输入对照组和实验组的转化人数与总人数，返回差异、置信区间、p 值、是否显著，"
         "并先做样本比例失衡（SRM）检查。数字必须来自用户提供的实验数据，不能自己编。",
         ProportionTestArgs, _ab_test),
    Tool("ab_sample_size",
         "计算 AB 实验每组所需样本量：输入基线转化率和想检测到的最小绝对提升。",
         SampleSizeArgs,
         lambda _, a: {"tool": "ab_sample_size", "per_group": sample_size_proportions(
             a.baseline_rate, a.min_detectable_effect, a.alpha, a.power), "alpha": a.alpha, "power": a.power}),
)}


def _drop_titles(schema: Any) -> Any:
    """去掉 pydantic 自动生成的 title 字段：对模型没用，只会多占 token。"""
    if isinstance(schema, dict):
        return {key: _drop_titles(value) for key, value in schema.items() if key != "title"}
    if isinstance(schema, list):
        return [_drop_titles(item) for item in schema]
    return schema


def tool_specs() -> list[dict[str, Any]]:
    """发给模型的工具说明书（OpenAI function calling 格式）。"""
    return [{"type": "function", "function": {"name": tool.name, "description": tool.description,
                                              "parameters": _drop_titles(tool.args_model.model_json_schema())}}
            for tool in TOOLS.values()]


@dataclass(frozen=True)
class ToolOutcome:
    """一次工具调用的结果。失败时 content 是 {"error": 原因}。"""

    ok: bool
    content: dict[str, Any]
    duration_ms: int

    def to_json(self) -> str:
        return json.dumps(self.content, ensure_ascii=False, default=str)


def execute(con: duckdb.DuckDBPyConnection, name: str, arguments: dict[str, Any]) -> ToolOutcome:
    """执行一次工具调用。任何失败都变成给模型看的错误说明，不向外抛异常。"""
    start = time.perf_counter()

    def done(ok: bool, content: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(ok, content, int((time.perf_counter() - start) * 1000))

    tool = TOOLS.get(name)
    if tool is None:
        return done(False, {"error": f"没有名为 {name} 的工具；可用工具：{list(TOOLS)}"})
    if INVALID_JSON_KEY in arguments:
        return done(False, {"error": f"参数不是合法的 JSON 对象：{arguments[INVALID_JSON_KEY][:200]}"})
    try:
        args = tool.args_model.model_validate(arguments)
    except ValidationError as exc:
        problems = "；".join(f"{'.'.join(str(p) for p in err['loc']) or '参数'}: {err['msg']}" for err in exc.errors())
        return done(False, {"error": f"参数不合法：{problems}"})
    try:
        result = tool.run(con, args)
    except ValueError as exc:  # 工具主动拒绝，例如周不在数据范围内、基期没有客户
        return done(False, {"error": str(exc)})
    except Exception as exc:  # 工具内部错误：记日志，告诉模型，不让整个分析崩溃
        LOGGER.exception("工具 %s 内部错误", name)
        return done(False, {"error": f"工具内部错误（{type(exc).__name__}），请换一种方式分析或在回答中说明"})
    outcome = done(True, result)
    if len(outcome.to_json()) > MAX_RESULT_CHARS:
        return done(False, {"error": f"结果超过 {MAX_RESULT_CHARS} 字符，请缩小范围（例如减小 top_n）"})
    return outcome
