"""看板方案的格式和静态校验。

方案是模型唯一的产出：放哪些指标（对表里哪一列做什么聚合）、哪些图（什么类型、横轴和分组是什么）、
哪些筛选器，以及每张图为什么放、看出了什么。格式用 pydantic 定义，同时生成给模型看的 JSON Schema。

静态校验只看方案本身和表的概况（列在不在、类型对不对、图和指标搭不搭），不查数据；
需要查数据的检查（取值在不在、单位有没有混在一起、结论里的数字有没有出处）在 compile 里做。
每条问题都写成模型能照着改的中文。
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from .sources import Source

Grain = Literal["year", "quarter", "month", "week", "day", "weekday", "hour"]
ADDITIVE = ("sum", "count")  # 可以按分组相加的聚合：只有它们能堆叠、能算“其他”和占比
MAX_SERIES = {"line": 5, "stacked_column": 4}  # 分类色的上限：堆叠超过 4 段、线超过 5 条就读不清
MAX_X = {"column": 36, "heatmap": 31, "table": 60}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _as_text(value: Any) -> Any:
    """取值一律按文字和数据比较（SQL 里是 CAST(列 AS VARCHAR)）。整数编码的列（流量来源 2、13），
    数据概况里显示的是数字，模型自然写成 [2, 13]；这里转成文字，不再因为类型退回（Online Shoppers 盲测里 4 次）。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value


Value = Annotated[str, BeforeValidator(_as_text)]  # 列里的一个取值


class Dimension(_Model):
    column: str = Field(description="列名")
    grain: Grain | None = Field(None, description=(
        "时间列必须给：year 年、quarter 季度、month 月、week 周、day 日、weekday 星期几、hour 小时；非时间列不要给"))
    keep: list[Value] = Field(default_factory=list, max_length=6, description=(
        "只保留这几个取值，其余合并成 other_label。例如 country 只保留 United Kingdom，其余合并成“海外”"))
    other_label: str = Field("其他", max_length=12, description="合并后的名字")
    order: list[Value] = Field(default_factory=list, max_length=60, description=(
        "分类的先后顺序，例如学历从低到高、年龄段从小到大；没列出的取值排在后面。"
        "不填时，月份和星期自动按日历排，其余按取值排序。只用于非时间列"))
    label: str = Field("", max_length=20, description="显示名，例如“国家”；不填用列名")


class Metric(_Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$", description="指标代号，小写字母、数字、下划线，例如 gmv")
    label: str = Field(min_length=1, max_length=20, description="显示名，例如“GMV”")
    agg: Literal["sum", "count", "count_distinct", "avg", "min", "max", "ratio", "difference"] = Field(description=(
        "sum 求和、count 行数、count_distinct 去重计数、avg 平均、min/max；"
        "ratio = left ÷ right、difference = left − right（left、right 是另外两个指标的 id）"))
    column: str | None = Field(None, description="被聚合的列；count、ratio、difference 不填")
    where: dict[str, list[Value]] = Field(default_factory=dict, description=(
        "只统计满足条件的行：列名 → 允许的取值，例如 {\"line_type\": [\"销售\"]}"))
    left: str | None = Field(None, description="ratio / difference 的第一个指标 id")
    right: str | None = Field(None, description="ratio / difference 的第二个指标 id")
    format: Literal["money", "number", "percent"] = Field("number", description="显示格式；percent 用于 0–1 的比率")


class Chart(_Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$", description="图的代号")
    title: str = Field(min_length=2, max_length=30, description="标题，写清看什么，例如“每月 GMV”")
    type: Literal["kpi", "line", "column", "stacked_column", "bar", "heatmap", "table"] = Field(description=(
        "kpi 指标卡（一个数）；line 折线（随时间变化，可按 series 分几条线）；column 柱状（随时间或少量类别）；"
        "stacked_column 堆叠柱（随时间的构成）；bar 横向条形（类别排名，配 top_n）；heatmap 热力图（x × series 两个维度）；"
        "table 表格（多个指标对照）"))
    metrics: list[str] = Field(min_length=1, max_length=6, description="用到的指标 id；除 table 外只能填 1 个")
    x: Dimension | None = Field(None, description="横轴（kpi 不填）")
    series: Dimension | None = Field(None, description="分组：折线的每条线、堆叠的每一段、热力图的纵轴")
    top_n: int | None = Field(None, ge=3, le=15, description="bar 只显示前几名，其余合并成“其他”")
    why: str = Field(min_length=2, max_length=120, description="为什么放这张图：它回答哪个业务问题")
    insight: str = Field("", max_length=160, description=(
        "这张图看出了什么（按全部数据，不加筛选）。里面的数字必须来自 preview_chart 返回的数据，程序会核查"))


class DashboardSpec(_Model):
    title: str = Field(min_length=2, max_length=30, description="看板标题")
    goal: str = Field(min_length=4, max_length=120, description="这个看板给谁看、帮他判断什么")
    metrics: list[Metric] = Field(min_length=1, max_length=12)
    charts: list[Chart] = Field(min_length=2, max_length=10)
    filters: list[Dimension] = Field(default_factory=list, max_length=2, description=(
        "页面顶部的筛选器（对所有图生效），每个最多 6 个取值，取值多时用 keep 合并"))
    year_filter: bool = Field(False, description="是否加按年份筛选（需要数据有时间列）")
    notes: list[str] = Field(default_factory=list, max_length=6, description="给读者的数据说明，例如口径和局限")


def base_metrics(ids: list[str], metrics: dict[str, Metric]) -> list[Metric]:
    """图里用到的基础指标：ratio / difference 展开成它引用的两个。"""
    out = []
    for i in ids:
        m = metrics.get(i)
        if m is None:
            continue
        if m.agg in ("ratio", "difference"):
            out += [metrics[x] for x in (m.left, m.right) if x in metrics]
        else:
            out.append(m)
    return out


def is_additive(m: Metric, metrics: dict[str, Metric]) -> bool:
    """能不能按分组相加：sum、count，以及两个可加指标的差（净销售额 = 销售 − 取消）。"""
    if m.agg == "difference":
        return all(side in metrics and metrics[side].agg in ADDITIVE for side in (m.left, m.right))
    return m.agg in ADDITIVE


def _dimension_problems(dim: Dimension, where: str, types: dict[str, str], source: Source) -> list[str]:
    problems = []
    kind = types.get(dim.column)
    if kind is None:
        return [f"{where}：没有 {dim.column} 这一列；可用的列：{list(types)}"]
    is_time = kind in ("timestamp", "date")
    if is_time and dim.grain is None:
        problems.append(f"{where}：{dim.column} 是时间列，要给 grain（例如 month）")
    if not is_time and dim.grain is not None:
        problems.append(f"{where}：{dim.column} 不是时间列，不能给 grain")
    if kind == "date" and dim.grain == "hour":
        problems.append(f"{where}：{dim.column} 只有日期，不能按小时")
    if dim.keep and (is_time or kind == "number"):
        problems.append(f"{where}：keep 只能用在分类列上")
    if dim.order and is_time:
        problems.append(f"{where}：时间列按时间排序，不用 order")
    if dim.column in source.pin_by or dim.column in source.unit_column:
        problems.append(f"{where}：{dim.column} 是数值列，不能当横轴或分组")
    return problems


def metric_problems(spec_metrics: list[Metric], types: dict[str, str]) -> list[str]:
    """指标本身的问题：列在不在、聚合方式和列的类型搭不搭、ratio / difference 引用对不对。"""
    problems: list[str] = []
    metrics = {m.id: m for m in spec_metrics}
    if len(metrics) < len(spec_metrics):
        problems.append("指标 id 有重复")
    for m in spec_metrics:
        where = f"指标 {m.id}"
        if m.agg in ("ratio", "difference"):
            if m.column is not None:
                problems.append(f"{where}：{m.agg} 不填 column，用 left 和 right 引用两个指标")
            for side in (m.left, m.right):
                if side not in metrics:
                    problems.append(f"{where}：left、right 必须是已定义的指标 id（收到 {side!r}）")
                elif metrics[side].agg in ("ratio", "difference"):
                    problems.append(f"{where}：left、right 只能引用基础指标，不能再引用 ratio / difference")
            continue
        if m.left or m.right:
            problems.append(f"{where}：只有 ratio / difference 才填 left、right")
        if m.agg == "count":
            if m.column is not None:
                problems.append(f"{where}：count 数的是行数，不填 column（数不同的值用 count_distinct）")
        elif m.column is None:
            problems.append(f"{where}：{m.agg} 要填 column")
        elif m.column not in types:
            problems.append(f"{where}：没有 {m.column} 这一列；可用的列：{list(types)}")
        elif m.agg in ("sum", "avg") and types[m.column] not in ("number", "integer"):
            problems.append(f"{where}：{m.column} 不是数值列，不能 {m.agg}")
        for column in m.where:
            if column not in types:
                problems.append(f"{where}：where 里没有 {column} 这一列")
        if m.format == "percent" and m.agg != "ratio":
            problems.append(f"{where}：percent 格式只用于 ratio 指标")
    return problems


def chart_problems(c: Chart, metrics: dict[str, Metric], types: dict[str, str], source: Source) -> list[str]:
    """一张图的问题：图的类型和指标、横轴、分组搭不搭，有没有把不同的东西加在一起。"""
    problems: list[str] = []
    where = f"图 {c.id}"
    unknown = [m for m in c.metrics if m not in metrics]
    if unknown:
        return [f"{where}：指标 {unknown} 没有定义"]
    if c.type != "table" and len(c.metrics) > 1:
        problems.append(f"{where}：{c.type} 只能放 1 个指标；多个指标对照用 table，或者拆成几张图（不做双坐标轴）")
    for name, dim in (("x", c.x), ("series", c.series)):
        if dim is not None:
            problems += _dimension_problems(dim, f"{where} 的 {name}", types, source)
    metric = metrics[c.metrics[0]]
    if c.type != "kpi" and c.x is None:
        problems.append(f"{where}：{c.type} 要给 x")
    if c.type == "kpi" and (c.x or c.series):
        problems.append(f"{where}：kpi 是一个数，不填 x 和 series")
    if c.type in ("stacked_column", "heatmap") and c.series is None:
        problems.append(f"{where}：{c.type} 要给 series")
    if c.type in ("kpi", "column", "bar", "table") and c.series is not None:
        problems.append(f"{where}：{c.type} 不用 series；要按组拆开请用 line、stacked_column 或 heatmap")
    if c.type == "stacked_column" and not is_additive(metric, metrics):
        problems.append(f"{where}：堆叠柱的各段要能相加，{metric.id} 是 {metric.agg}，不能堆叠；"
                        "改用 line 分组，或换成 sum / count 指标")
    if c.type == "bar":
        if c.x is not None and c.x.grain is not None:
            problems.append(f"{where}：bar 用来比较类别，随时间变化请用 line 或 column")
    elif c.top_n is not None:
        problems.append(f"{where}：只有 bar 用 top_n")
    if c.type in ("line", "stacked_column") and c.x is not None and c.x.grain in ("weekday", "hour"):
        problems.append(f"{where}：{c.type} 的横轴是时间趋势；星期几、小时这类周期用 column 或 heatmap")
    if c.type == "line" and c.x is not None and c.x.grain is None and types.get(c.x.column) != "integer":
        problems.append(f"{where}：折线的横轴要是时间；类别比较用 bar")
    grouped = {d.column for d in (c.x, c.series) if d is not None}
    for m in base_metrics(c.metrics, metrics):
        pins = source.pin_by.get(m.column or "")
        if pins and not grouped & set(pins) and not any(len(m.where.get(p, [])) == 1 for p in pins):
            problems.append(f"{where}：指标 {m.id} 聚合的 {m.column} 含义取决于 {pins[0]}，"
                            f"要在 where 里把 {pins[0]} 限定为一个取值，或者按 {pins[0]} 分组")
    return problems


def check_spec(spec: DashboardSpec, types: dict[str, str], source: Source) -> list[str]:
    """整个方案的静态校验，返回问题列表（空表示通过）。types：列名 → 类型（sources.column_types）。"""
    problems = metric_problems(spec.metrics, types)
    metrics = {m.id: m for m in spec.metrics}
    if len({c.id for c in spec.charts}) < len(spec.charts):
        problems.append("图的 id 有重复")
    used: set[str] = set()
    for c in spec.charts:
        problems += chart_problems(c, metrics, types, source)
        used.update(c.metrics)
        used.update(x for m in c.metrics if m in metrics for x in (metrics[m].left, metrics[m].right) if x)
    for m in spec.metrics:
        if m.id not in used:
            problems.append(f"指标 {m.id} 没有在任何图里用到")
    for i, dim in enumerate(spec.filters):
        problems += _dimension_problems(dim, f"筛选器 {i + 1}", types, source)
        if dim.grain is not None:
            problems.append(f"筛选器 {i + 1}：按时间筛选请用 year_filter")
    if spec.year_filter and source.time_column is None:
        problems.append("这份数据没有时间戳列，不能用 year_filter；可以把年份放在横轴上")
    if not any(c.type == "kpi" for c in spec.charts):
        problems.append("至少放一张 kpi：读者先看到最重要的数")
    return problems


def spec_schema() -> dict[str, Any]:
    """给模型看的方案格式。"""
    return DashboardSpec.model_json_schema()
