"""把看板方案编译成数据：生成 SQL、按筛选条件算出每张图的数据、自动标出数据陷阱、核查结论里的数字。

SQL 由这里拼出来，模型写不了：列名只能是表里已有的列（静态校验已经查过，这里再加引号），
取值一律走参数，聚合方式只有白名单里的几种。

自动标出的数据陷阱（写进每张图的 notes，模型看得到，读者也看得到）：
- 不完整的周期：时间横轴上第一个或最后一个周期只覆盖了一部分（比如数据截止在 12 月 9 日）
- 一家独大：某个分组占了 60% 以上，其他分组在图里几乎看不见
- 只显示了前几名：排名图截掉了多少个分组
需要退回模型修改的问题（作为错误返回）：取值不存在、单位混在一起、分组太多、结论里的数字找不到出处。
"""

import itertools
import re
from datetime import date, datetime, timedelta
from typing import Any

import duckdb

from ..checks import extract_numbers
from ..cleaning import fetch_one, fetch_rows
from .sources import Source, column_types, source_of
from .spec import (MAX_SERIES, MAX_X, Chart, DashboardSpec, Dimension, Metric, base_metrics, chart_problems,
                   check_spec, is_additive, metric_problems)

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# 认得出的月份和星期写法（不分大小写）：取值全部能认出来时，按日历排，而不是按字母排（M13b 盲测里 apr、aug、dec……）
MONTH_NAMES = [(f"{m}月", f"{cn}月", en[:3], en, *extra) for m, (cn, en, *extra) in enumerate([
    ("一", "january"), ("二", "february"), ("三", "march"), ("四", "april"), ("五", "may"), ("六", "june"),
    ("七", "july"), ("八", "august"), ("九", "september", "sept"), ("十", "october"), ("十一", "november"),
    ("十二", "december")], 1)]
WEEKDAY_NAMES = [(f"周{cn}", f"星期{cn}", en[:3], en, *extra) for cn, en, *extra in [
    ("一", "monday"), ("二", "tuesday", "tues"), ("三", "wednesday"), ("四", "thursday", "thur", "thurs"),
    ("五", "friday"), ("六", "saturday"), ("日", "sunday", "周天", "星期天")]]
DOMINANT_SHARE = 0.6
OFFSET_ALERT = 0.5  # 一个分组的销售额有一半以上被之后的取消抵消时，提醒按下单口径会高估它
MAX_FILTER_OPTIONS = 6
MAX_SLICES = 60
PREVIEW_ROWS = 40
YEAR = "__year__"  # 年份筛选器的内部标记（不是表里的列）
UNIT_SYMBOLS = {"CNY": "¥", "USD": "$"}


class DashboardProblems(ValueError):
    """方案有问题：每条都是给模型看的修改意见。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("方案没有通过检查，请逐条修改后再提交：\n" + "\n".join(f"- {p}" for p in problems))
        self.problems = problems


def _q(column: str) -> str:
    return '"' + column.replace('"', '""') + '"'


def _dim_sql(dim: Dimension, params: list[Any]) -> str:
    col = _q(dim.column)
    match dim.grain:
        case "year":
            return f"strftime({col}, '%Y')"
        case "quarter":
            return f"strftime({col}, '%Y') || '-Q' || quarter({col})"
        case "month":
            return f"strftime({col}, '%Y-%m')"
        case "week":
            return f"strftime({col}, '%G-W%V')"
        case "day":
            return f"strftime({col}, '%Y-%m-%d')"
        case "weekday":
            return f"isodow({col})"
        case "hour":
            return f"hour({col})"
    if dim.bins:  # 分段：取值是段的序号 0..k，标签由 Compiler 按分界点和列的类型生成
        params += list(dim.bins)
        cases = " ".join(f"WHEN {col} < ? THEN {i}" for i in range(len(dim.bins)))
        return f"CASE WHEN {col} IS NULL THEN NULL {cases} ELSE {len(dim.bins)} END"
    if dim.keep:
        params += [*dim.keep, dim.other_label]
        return f"CASE WHEN {col} IN ({', '.join('?' * len(dim.keep))}) THEN CAST({col} AS VARCHAR) ELSE ? END"
    return col


def _metric_sql(m: Metric, metrics: dict[str, Metric], params: list[Any]) -> str:
    if m.agg in ("ratio", "difference"):
        left = _metric_sql(metrics[m.left], metrics, params)
        right = _metric_sql(metrics[m.right], metrics, params)
        if m.agg == "ratio":
            return f"(({left}) / nullif(({right}), 0))"
        # 差值的一边没有记录时按 0 算：没有取消的商品，净销售额就是销售额，不能变成空值从排名里消失
        return f"(coalesce(({left}), 0) - coalesce(({right}), 0))"
    col = _q(m.column) if m.column else ""
    base = {"sum": f"sum({col})", "count": "count(*)", "count_distinct": f"count(DISTINCT {col})",
            "avg": f"avg({col})", "min": f"min({col})", "max": f"max({col})"}[m.agg]
    if not m.where:
        return base
    return f"{base} FILTER (WHERE {_conditions(m.where, params)})"


def _conditions(where: dict[str, list[str]], params: list[Any]) -> str:
    parts = []
    for column, values in where.items():
        params += values
        parts.append(f"CAST({_q(column)} AS VARCHAR) IN ({', '.join('?' * len(values))})")
    return " AND ".join(parts)


def _label(dim: Dimension | None, value: Any) -> str | None:
    if dim is None:
        return None
    if value is None:
        return "（空）"
    if dim.grain == "weekday":
        return WEEKDAYS[int(value) - 1]
    if dim.grain == "hour":
        return f"{int(value)} 时"
    return str(value)


def _edge(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def bin_labels(edges: list[float], integer: bool, lo: float | None, hi: float | None) -> list[str]:
    """分段的标签。整数列（分界点也是整数）写成 ≤0、1–5、6–20、≥21；小数列写成 <60、60–300、≥300（左闭右开）。
    最小值正好落在第一段的上限、最大值正好等于最后一个分界点时，直接写那个数。"""
    if integer and all(float(e).is_integer() for e in edges):
        e = [int(x) for x in edges]
        first = str(e[0] - 1) if lo is not None and lo == e[0] - 1 else f"≤{e[0] - 1}"
        middle = [str(a) if a == b - 1 else f"{a}–{b - 1}" for a, b in zip(e, e[1:])]
        last = str(e[-1]) if hi is not None and hi == e[-1] else f"≥{e[-1]}"
        return [first, *middle, last]
    return [f"<{_edge(edges[0])}", *(f"{_edge(a)}–{_edge(b)}" for a, b in zip(edges, edges[1:])),
            f"≥{_edge(edges[-1])}"]


class Compiler:
    """一份数据上的编译器：缓存表的概况，按筛选条件（slice）算图的数据。"""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self.source: Source = source_of(con)
        self.types = column_types(con, self.source)
        self.table = _q(self.source.table)
        bounds = None
        if self.source.time_column:
            col = _q(self.source.time_column)
            bounds = fetch_one(con, f"SELECT min({col}) AS lo, max({col}) AS hi FROM {self.table}")
        self.time_bounds = (bounds["lo"], bounds["hi"]) if bounds else None
        self._ranges: dict[str, tuple[Any, Any]] = {}

    def label(self, dim: Dimension | None, value: Any) -> str | None:
        """取值 → 显示的标签（分段显示成范围）。"""
        if dim is not None and dim.bins and value is not None:
            if dim.column not in self._ranges:
                r = fetch_one(self.con, f"SELECT min({_q(dim.column)}) AS lo, max({_q(dim.column)}) AS hi FROM {self.table}")
                self._ranges[dim.column] = (r["lo"], r["hi"])
            lo, hi = self._ranges[dim.column]
            return bin_labels(dim.bins, self.types.get(dim.column) == "integer", lo, hi)[int(value)]
        return _label(dim, value)

    # ---------- 查询 ----------
    def _where(self, slice_filters: list[tuple[Dimension, str]], params: list[Any]) -> str:
        parts = []
        for dim, value in slice_filters:
            if dim.column == YEAR:
                parts.append(f"strftime({_q(self.source.time_column)}, '%Y') = ?")
                params.append(value)
            else:  # 统一按文字比较：财年这样的整数列也能用字符串取值筛选
                parts.append(f"CAST({_dim_sql(dim, params)} AS VARCHAR) = ?")
                params.append(value)
        return " AND ".join(parts) or "TRUE"

    def query(self, chart: Chart, metrics: dict[str, Metric],
              slice_filters: list[tuple[Dimension, str]] = ()) -> list[dict[str, Any]]:
        params: list[Any] = []
        select, group = [], []
        for name, dim in (("x", chart.x), ("s", chart.series)):
            if dim is not None:
                select.append(f"{_dim_sql(dim, params)} AS {name}")
                group.append(name)
        for i, metric_id in enumerate(chart.metrics):
            select.append(f"{_metric_sql(metrics[metric_id], metrics, params)} AS m{i}")
        where = self._where(list(slice_filters), params)
        # 只用指标真正统计的那些行来分组：否则只有取消、没有销售的时段也会变成一个值为 0 的格子
        # （M13 第一次真实运行里出现过“周四 21 时 0 笔”）。各指标的条件取并集，不影响每个指标自己的 FILTER
        bases = base_metrics(chart.metrics, metrics)
        if bases and all(m.where for m in bases):
            where += " AND (" + " OR ".join(f"({_conditions(m.where, params)})" for m in bases) + ")"
        sql = f"SELECT {', '.join(select)} FROM {self.table} WHERE {where}"
        if group:
            sql += f" GROUP BY {', '.join(group)}"
        return fetch_rows(self.con, sql, params)

    def distinct(self, dim: Dimension) -> list[str]:
        params: list[Any] = []
        expr = _dim_sql(dim, params)
        return [r["v"] for r in fetch_rows(self.con, f"SELECT DISTINCT {expr} AS v FROM {self.table} ORDER BY 1",
                                           params)]

    # ---------- 需要查数据的检查 ----------
    def data_problems(self, spec_metrics: list[Metric], charts: list[Chart],
                      filters: list[Dimension] = ()) -> list[str]:
        problems: list[str] = []
        metrics = {m.id: m for m in spec_metrics}
        for m in spec_metrics:
            for column, values in m.where.items():
                present = set(self._present(column, values))
                missing = [v for v in values if v not in present]
                if missing:
                    problems.append(f"指标 {m.id}：where 里 {column} 没有这些取值 {missing}；"
                                    f"常见取值：{self._common(column)}")
        dims = [(f"图 {c.id} 的 {name}", d) for c in charts for name, d in (("x", c.x), ("series", c.series)) if d]
        dims += [(f"筛选器 {i + 1}", d) for i, d in enumerate(filters)]
        for where, dim in dims:
            missing = [v for v in dim.keep if v not in set(self._present(dim.column, dim.keep))]
            if missing:
                problems.append(f"{where}：keep 里 {dim.column} 没有这些取值 {missing}；常见取值：{self._common(dim.column)}")
            listed = [v for v in dim.order if v != dim.other_label]
            missing = [v for v in listed if v not in set(self._present(dim.column, listed))] if listed else []
            if missing:
                problems.append(f"{where}：order 里 {dim.column} 没有这些取值 {missing}；常见取值：{self._common(dim.column)}")
        for m in spec_metrics:
            if m.format == "percent" and m.agg in ("avg", "min", "max") and m.column in self.types:
                top = fetch_one(self.con, f"SELECT max(abs({_q(m.column)})) AS v FROM {self.table}")["v"]
                if top is not None and top > 1:
                    problems.append(f"指标 {m.id}：percent 用于 0–1 之间的值，{m.column} 最大是 {_num(top)}；改用 number 格式")
        for i, dim in enumerate(filters):
            n = len(self.distinct(dim))
            if n > MAX_FILTER_OPTIONS + 1:
                problems.append(f"筛选器 {i + 1}：{dim.column} 有 {n} 个取值，太多了；用 keep 只保留几个，其余合并")
        for c in charts:
            problems += self._chart_data_problems(c, metrics)
        return problems

    def _present(self, column: str, values: list[str]) -> list[str]:
        rows = fetch_rows(self.con, f"SELECT DISTINCT CAST({_q(column)} AS VARCHAR) AS v FROM {self.table} "
                                    f"WHERE CAST({_q(column)} AS VARCHAR) IN ({', '.join('?' * len(values))})",
                          list(values))
        return [r["v"] for r in rows]

    def _common(self, column: str) -> list[str]:
        rows = fetch_rows(self.con, f"SELECT CAST({_q(column)} AS VARCHAR) AS v, count(*) AS n FROM {self.table} "
                                    "GROUP BY 1 ORDER BY 2 DESC LIMIT 8")
        return [r["v"] for r in rows]

    def _chart_data_problems(self, c: Chart, metrics: dict[str, Metric]) -> list[str]:
        problems = []
        if any(m not in metrics for m in c.metrics):
            return problems  # 静态校验已经报过
        x_limit = None if c.type == "scatter" and c.top_n else MAX_X.get(c.type)
        for name, dim, limit in (("series", c.series, MAX_SERIES.get(c.type)), ("x", c.x, x_limit)):
            if dim is not None and limit:
                n = len(self.distinct(dim))
                if n > limit:
                    problems.append(f"图 {c.id}：{name} 按 {dim.column} 有 {n} 组，{c.type} 最多 {limit} 组；"
                                    "换更粗的时间粒度，或用 keep 只保留几个取值")
        unit_cols = self.source.unit_column
        for metric_id in c.metrics:
            m = metrics[metric_id]
            bases = [metrics[x] for x in (m.left, m.right)] if m.agg in ("ratio", "difference") else [m]
            for base in bases:
                unit_col = unit_cols.get(base.column or "")
                if not unit_col:
                    continue
                # 每个格子里只能有一种单位；比率以外的指标，整张图也只能有一种单位
                units = self._units(c, base, metrics, unit_col)
                mixed_cell = max((len(u) for u in units.values()), default=0) > 1
                all_units = sorted(set().union(*units.values())) if units else []
                if mixed_cell:
                    problems.append(f"图 {c.id}：指标 {base.id} 在同一个格子里混合了不同的 {unit_col}（{all_units}），"
                                    "要按公司或币种分开")
                elif m.agg != "ratio" and len(all_units) > 1:
                    problems.append(f"图 {c.id}：指标 {m.id} 在不同分组里单位不同（{all_units}），金额不能放在同一个坐标轴上比较；"
                                    "改成比较比率（ratio），或者用筛选器 / where 只看一种单位")
        return problems

    def _units(self, c: Chart, base: Metric, metrics: dict[str, Metric], unit_col: str) -> dict[tuple, set[str]]:
        params: list[Any] = []
        dims = [d for d in (c.x, c.series) if d is not None]
        select = [f"{_dim_sql(d, params)} AS d{i}" for i, d in enumerate(dims)]
        where_parts = []
        for column, values in base.where.items():
            params += values
            where_parts.append(f"CAST({_q(column)} AS VARCHAR) IN ({', '.join('?' * len(values))})")
        sql = (f"SELECT {', '.join(select + [f'list(DISTINCT {_q(unit_col)}) AS units'])} FROM {self.table} "
               f"WHERE {' AND '.join(where_parts) or 'TRUE'} AND {_q(base.column)} IS NOT NULL")
        if dims:
            sql += " GROUP BY " + ", ".join(f"d{i}" for i in range(len(dims)))
        return {tuple(r.get(f"d{i}") for i in range(len(dims))): set(r["units"] or []) for r in fetch_rows(
            self.con, sql, params)}

    def unit_of(self, c: Chart, metrics: dict[str, Metric]) -> str | None:
        """图上数字的单位：比率没有单位；金额用固定单位或单位列里唯一的那个。"""
        m = metrics[c.metrics[0]]
        if m.format == "percent" or (m.agg == "ratio" and m.format != "money"):
            return None
        base = metrics[m.left] if m.agg in ("ratio", "difference") else m  # 金额 ÷ 单数（客单价）用分子的单位
        if base.column in self.source.units:
            return self.source.units[base.column]
        unit_col = self.source.unit_column.get(base.column or "")
        if unit_col:
            units = set().union(*self._units(c, base, metrics, unit_col).values())
            if len(units) == 1:
                unit = units.pop()
                return UNIT_SYMBOLS.get(unit, unit)
        return None

    # ---------- 一张图的数据 ----------
    def chart_data(self, c: Chart, metrics: dict[str, Metric],
                   slice_filters: list[tuple[Dimension, str]] = ()) -> dict[str, Any]:
        rows = self.query(c, metrics, slice_filters)
        notes: list[str] = []
        parts = self._ratio_parts(c, metrics, slice_filters)
        if c.type == "kpi":
            value = rows[0]["m0"] if rows else None
            facts = {"value": _num(value)}
            if parts:  # 比率卡片带上分子和分母：结论可以写“11.3%（4,640 / 41,188）”
                facts["numerator"], facts["denominator"] = parts.get((None, None), (None, None))
            return {"rows": [{"x": None, "s": None, "values": [_num(value)]}], "facts": facts, "notes": notes}
        out = [{"x": self.label(c.x, r["x"]), "s": self.label(c.series, r.get("s")), "_x": r["x"], "_s": r.get("s"),
                "values": [_num(r[f"m{i}"]) for i in range(len(c.metrics))]} for r in rows]
        for r in out:  # 比率的每个点都带上分子和分母：小样本的分组能看出、也能在结论里写出样本量
            if parts and (r["_x"], r["_s"]) in parts:
                r["numerator"], r["denominator"] = parts[(r["_x"], r["_s"])]
        if c.type in ("bar", "table"):  # 指标用 where 限定后，其余分组的值全是空的：类别图里不显示这些分组
            out = [r for r in out if any(v is not None for v in r["values"])]
        metric = metrics[c.metrics[0]]
        additive = is_additive(metric, metrics)
        if c.type == "bar":
            out.sort(key=lambda r: (-(r["values"][0] or 0), str(r["x"])))
            total = sum(r["values"][0] or 0 for r in out)
            n, top_n = len(out), c.top_n or 10
            top = out[:top_n]
            if n > top_n:
                rest = out[top_n:]
                if additive:
                    top.append({"x": f"其他 {len(rest)} 个", "s": None, "values": [_num(total - sum(
                        r["values"][0] or 0 for r in top))], "other": True})
                notes.append(f"只显示前 {top_n} 个，共 {n} 个" + ("，其余合并成“其他”" if additive else ""))
            out = top
        else:
            x_rank = _ranker(c.x, [r["_x"] for r in out])
            s_rank = _ranker(c.series, [r["_s"] for r in out])
            out.sort(key=lambda r: (x_rank(r["_x"]), s_rank(r["_s"])))
            out = self._fill_months(c, out, additive)
            out = self._fill_bins(c, out, additive)
            if c.type == "scatter" and c.top_n and len(out) > c.top_n:
                biggest = sorted(out, key=lambda r: -(r["values"][0] or 0))[:c.top_n]
                notes.append(f"只画了{metrics[c.metrics[0]].label}最大的 {c.top_n} 个，共 {len(out)} 个")
                out = [r for r in out if r in biggest]
        facts: dict[str, Any] = {}
        vi = 1 if c.type == "scatter" else 0  # 散点图的最大最小看纵轴指标（比率），不看规模
        values = [r["values"][vi] for r in out if r["values"][vi] is not None and not r.get("other")]
        if additive and c.type in ("bar", "column", "stacked_column") and values:
            total = sum(r["values"][0] or 0 for r in out)
            facts["total"] = _num(total)
            if total:
                for r in out:
                    r["share"] = round((r["values"][0] or 0) / total, 4)
                lead = max((r for r in out if not r.get("other")), key=lambda r: r["values"][0] or 0)
                if c.type != "stacked_column" and lead["share"] >= DOMINANT_SHARE and len(out) > 1:
                    notes.append(f"{lead['x']} 占 {lead['share']:.1%}，其他分组在图里很难看清；"
                                 f"可以用 keep 把 {lead['x']} 单独分开，或者再放一张只看其余分组的图")
        if c.type == "stacked_column" and additive:
            by_series: dict[str, float] = {}
            for r in out:
                by_series[r["s"]] = by_series.get(r["s"], 0) + (r["values"][0] or 0)
            grand = sum(by_series.values())
            if grand:
                facts["series_share"] = {k: round(v / grand, 4) for k, v in by_series.items()}
        if values:
            shown = [r for r in out if r["values"][vi] is not None and not r.get("other")]
            hi = max(shown, key=lambda r: r["values"][vi])
            lo = min(shown, key=lambda r: r["values"][vi])
            facts["max"] = {"x": hi["x"], "s": hi["s"], "value": hi["values"][vi]}
            facts["min"] = {"x": lo["x"], "s": lo["s"], "value": lo["values"][vi]}
        if c.reference:
            facts["reference"] = self._reference(c, metrics, slice_filters)
        notes += self._offset_notes(c, metrics, out, slice_filters)
        partial = self._mark_partial(c, out)
        if partial:
            notes.append(f"{'、'.join(partial)} 只覆盖了部分时间（数据范围 {self._range_text()}），不能和完整的周期直接比")
        if c.x is not None and c.x.grain in ("year", "quarter", "month", "week") and c.series is None:
            complete = [r for r in out if not r.get("partial") and r["values"][0] is not None]
            if len(complete) >= 2:
                last, prev = complete[-1], complete[-2]
                facts["last_complete"] = {"x": last["x"], "value": last["values"][0]}
                facts["previous"] = {"x": prev["x"], "value": prev["values"][0]}
                if prev["values"][0]:
                    facts["change_pct"] = round(last["values"][0] / prev["values"][0] - 1, 4)
        result = {"rows": out, "facts": facts, "notes": notes}
        if c.series is not None:  # 分组的顺序：keep 里的取值在前，其余按自然顺序（星期一到星期日、年份从小到大）
            raw = {r["s"]: r["_s"] for r in out}
            s_rank = _ranker(c.series, list(raw.values()))
            kept = [] if c.series.order else [v for v in c.series.keep if v in raw]
            result["series_order"] = kept + sorted((k for k in raw if k not in kept), key=lambda k: s_rank(raw[k]))
        for r in out:
            r.pop("_x", None)
            r.pop("_s", None)
        return result

    def _reference(self, c: Chart, metrics: dict[str, Metric],
                   slice_filters: list[tuple[Dimension, str]]) -> dict[str, Any]:
        """参考线：同一个指标不分组时的值（当前筛选下），比率还带分子分母。"""
        metric_id = c.metrics[1] if c.type == "scatter" else c.metrics[0]
        overall = c.model_copy(update={"type": "kpi", "x": None, "series": None, "metrics": [metric_id]})
        rows = self.query(overall, metrics, slice_filters)
        ref: dict[str, Any] = {"value": _num(rows[0]["m0"]) if rows else None}
        parts = self._ratio_parts(overall, metrics, slice_filters)
        if parts:
            ref["numerator"], ref["denominator"] = parts.get((None, None), (None, None))
        return ref

    def _fill_bins(self, c: Chart, rows: list[dict[str, Any]], additive: bool) -> list[dict[str, Any]]:
        """分段的横轴补上没有记录的段：分布图上空的段也要看得到（计数为 0，比率为空）。"""
        if c.x is None or not c.x.bins or c.series is not None or c.type not in ("column", "line", "table"):
            return rows
        have = {r["_x"]: r for r in rows if r["_x"] is not None}
        filled = [have.get(i) or {"x": self.label(c.x, i), "s": None, "_x": i, "_s": None,
                                  "values": [0 if additive else None] * len(c.metrics)}
                  for i in range(len(c.x.bins) + 1)]
        return filled + [r for r in rows if r["_x"] is None]

    def _ratio_parts(self, c: Chart, metrics: dict[str, Metric],
                     slice_filters: list[tuple[Dimension, str]]) -> dict[tuple, tuple]:
        """比率指标每个点的（分子，分母），按 (x, series) 的原始取值索引；不是比率就返回空。"""
        m = metrics[c.metrics[0]]
        if m.agg != "ratio" or len(c.metrics) > 1:
            return {}
        rows = self.query(c.model_copy(update={"metrics": [m.left, m.right]}), metrics, slice_filters)
        return {(r.get("x"), r.get("s")): (_num(r["m0"]), _num(r["m1"])) for r in rows}

    def _offset_notes(self, c: Chart, metrics: dict[str, Metric], rows: list[dict[str, Any]],
                      slice_filters: list[tuple[Dimension, str]]) -> list[str]:
        """类别图里，某个分组的销售额大部分被之后的取消抵消了：按下单口径排名会高估它（M13 第一次真实运行里，
        商品 Top 10 的第 3 名全部被取消、第 8 名 94.8% 被取消）。在这些分组的行上记下被抵消的金额和占比。"""
        m = metrics[c.metrics[0]]
        offset = self.source.offsets.get(m.column or "")
        if c.type not in ("bar", "column", "table") or c.x is None or c.x.grain is not None or offset is None:
            return []
        by, orig, against = offset
        if m.agg != "sum" or m.where.get(by) != [orig]:
            return []
        probe = Metric(id="zz_offset", label="抵消", agg="sum", column=m.column, where={**m.where, by: [against]})
        offsets = {self.label(c.x, r["x"]): r["m0"] or 0 for r in self.query(
            c.model_copy(update={"metrics": ["zz_offset"], "series": None}), {"zz_offset": probe}, slice_filters)}
        notes = []
        for r in rows:
            value, off = r["values"][0], offsets.get(r["x"], 0)
            if r.get("other") or not value or not off:
                continue
            r["offset"], r["offset_share"] = _num(off), round(off / value, 4)
            if r["offset_share"] >= OFFSET_ALERT:
                unit = self.source.units.get(m.column or "", "")
                notes.append(f"{r['x']} 的{m.label}有 {r['offset_share']:.1%} 后来被{against}（{unit}{off:,.2f}），"
                             f"按下单口径排名会高估它；看真实成交改用 difference（{orig} − {against}）")
        return notes[:3]

    @staticmethod
    def _fill_months(c: Chart, rows: list[dict[str, Any]], additive: bool) -> list[dict[str, Any]]:
        """按月的折线 / 柱子补上没有数据的月份，时间轴不会“跳月”：能相加的指标记 0，其余（平均、比率）记为空。"""
        if c.x is None or c.x.grain != "month" or c.series is not None or not rows:
            return rows
        have = {r["x"]: r for r in rows}
        first, last = rows[0]["x"], rows[-1]["x"]
        y, m = map(int, first.split("-"))
        out = []
        while f"{y:04d}-{m:02d}" <= last:
            key = f"{y:04d}-{m:02d}"
            out.append(have.get(key, {"x": key, "s": None, "values": [0 if additive else None] * len(c.metrics)}))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out

    def _range_text(self) -> str:
        lo, hi = self.time_bounds
        return f"{lo:%Y-%m-%d} 至 {hi:%Y-%m-%d}"

    def _mark_partial(self, c: Chart, rows: list[dict[str, Any]]) -> list[str]:
        """时间横轴上，第一个或最后一个周期没被数据完整覆盖的，标成 partial。"""
        if c.x is None or c.x.grain not in ("year", "quarter", "month", "week") or not self.time_bounds:
            return []
        lo, hi = (t.date() if isinstance(t, datetime) else t for t in self.time_bounds)
        partial = []
        for r in rows:
            start, end = _period(c.x.grain, r["x"])
            if start < lo or end > hi:
                r["partial"] = True
                if r["x"] not in partial:
                    partial.append(r["x"])
        return partial


def _period(grain: str, label: str) -> tuple[date, date]:
    """周期标签 → 起止日期（含）。"""
    if grain == "year":
        y = int(label)
        return date(y, 1, 1), date(y, 12, 31)
    if grain == "quarter":
        y, q = int(label[:4]), int(label[-1])
        start = date(y, 3 * q - 2, 1)
        end = date(y + (q == 4), 1 if q == 4 else 3 * q + 1, 1) - timedelta(days=1)
        return start, end
    if grain == "month":
        y, m = map(int, label.split("-"))
        return date(y, m, 1), date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    y, w = int(label[:4]), int(label[-2:])
    start = date.fromisocalendar(y, w, 1)
    return start, start + timedelta(days=6)


def _num(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return round(float(value), 4)


MONTH_NUMBER = {alias.lower(): i for i, aliases in enumerate(MONTH_NAMES, 1) for alias in aliases}


def _calendar(values: list[Any]) -> dict[Any, int] | None:
    """取值全部能认出是月份（或全部是星期）时，返回 取值 → 日历位置；否则 None。"""
    for names in (MONTH_NAMES, WEEKDAY_NAMES):
        lookup = {alias.lower(): i for i, aliases in enumerate(names) for alias in aliases}
        ranks = {v: lookup.get(str(v).strip().lower()) for v in values if v is not None}
        if ranks and all(r is not None for r in ranks.values()):
            return ranks
    return None


def _ranker(dim: Dimension | None, values: list[Any]):  # noqa: ANN202
    """一个维度上取值的先后：模型给的 order 最优先；其次月份、星期按日历；其余按取值本身。"""
    if dim is not None and dim.order:
        position = {v: i for i, v in enumerate(dim.order)}
        return lambda v: (0, position[str(v)], ()) if str(v) in position else (1, 0, _sort_key(v))
    calendar = _calendar(values) if dim is not None and dim.grain is None else None
    if calendar:
        return lambda v: (0, calendar.get(v, 99), _sort_key(v))
    return lambda v: (0, 0, _sort_key(v))


def _sort_key(value: Any) -> tuple:
    return (value is None, value if isinstance(value, (int, float)) else str(value or ""))


# ---------- 结论里的数字 ----------
def _pool(data: dict[str, Any]) -> list[float]:
    """一张图里能当出处的数字：每个值、占比、事实（合计、最大最小、变化），以及标签里的数字（如年份）。"""
    pool: list[float] = []

    def walk(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            pool.append(abs(float(value)))
        elif isinstance(value, str):
            pool.extend(m.value for m in extract_numbers(value)[0])
            pool.extend(float(x) for x in re.findall(r"\d+", value))
            if value.strip().lower() in MONTH_NUMBER:  # 标签是 Nov 时，结论写“11 月”也有出处（盲测里 3 次都因此被退回）
                pool.append(float(MONTH_NUMBER[value.strip().lower()]))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(data)
    return pool


def highlight_problems(c: Chart, data: dict[str, Any]) -> list[str]:
    """highlight 里的取值必须是图里的某个分组（照抄预览的 x）。"""
    labels = [r["x"] for r in data["rows"]]
    missing = [v for v in c.highlight if v not in labels]
    if not missing:
        return []
    hint = "；被 top_n 截掉的分组可以调大 top_n" if c.top_n else ""
    return [f"图 {c.id}：highlight 里这些取值在图里没有 {missing}；图里的取值：{labels[:15]}{hint}"]


def ungrounded_numbers(text: str, data: dict[str, Any]) -> list[str]:
    """结论句里在这张图的数据中找不到出处的数字（和报告核查用同一个数字提取规则）。"""
    pool = _pool(data)
    return [m.text for m in extract_numbers(text)[0]
            if not any(abs(m.value - p) < m.tolerance for p in pool)]


# ---------- 整个看板 ----------
def compile_dashboard(con: duckdb.DuckDBPyConnection, spec: DashboardSpec) -> dict[str, Any]:
    """校验并编译整个方案。有问题时抛出 DashboardProblems（每条都是修改意见）。"""
    compiler = Compiler(con)
    source = compiler.source
    problems = check_spec(spec, compiler.types, source)
    if problems:
        raise DashboardProblems(problems)
    problems = compiler.data_problems(spec.metrics, spec.charts, spec.filters)
    if problems:
        raise DashboardProblems(problems)
    metrics = {m.id: m for m in spec.metrics}
    filters: list[dict[str, Any]] = []
    axes: list[list[tuple[Dimension, str] | None]] = []
    if spec.year_filter:
        years = compiler.distinct(Dimension(column=source.time_column, grain="year"))
        filters.append({"id": "year", "label": "年份",
                        "options": [{"id": "all", "label": "全部"}] + [{"id": y, "label": f"{y} 年"} for y in years]})
        year_dim = Dimension(column=YEAR)
        axes.append([None] + [(year_dim, y) for y in years])
    for i, dim in enumerate(spec.filters):
        # 按钮的先后和图里一致：模型给的 order、月份星期的日历顺序；合并出来的“其他”放最后
        raw = compiler.distinct(dim)
        rank = _ranker(dim, raw)
        values = [str(v) for v in sorted(raw, key=lambda v: (bool(dim.keep) and v == dim.other_label, rank(v)))]
        filters.append({"id": f"f{i}", "label": dim.label or dim.column,
                        "options": [{"id": "all", "label": "全部"}] + [{"id": v, "label": v} for v in values]})
        axes.append([None] + [(dim, v) for v in values])
    combos = list(itertools.product(*axes)) if axes else [()]
    if len(combos) > MAX_SLICES:
        raise DashboardProblems([f"筛选器组合有 {len(combos)} 种，太多了（最多 {MAX_SLICES}）；减少筛选器或用 keep 合并取值"])
    slices: dict[str, dict[str, Any]] = {}
    for combo in combos:
        key = "|".join("all" if f is None else f[1] for f in combo) or "all"
        active = [f for f in combo if f is not None]
        slices[key] = {c.id: compiler.chart_data(c, metrics, active) for c in spec.charts}
    default = "|".join("all" for _ in axes) or "all"
    insight_problems = []
    for c in spec.charts:
        bad = ungrounded_numbers(c.insight, slices[default][c.id]) if c.insight else []
        if bad:
            insight_problems.append(f"图 {c.id} 的 insight 里这些数字在这张图的数据中找不到出处：{bad}；"
                                    "改成 preview_chart 返回的原数，或删掉")
        insight_problems += highlight_problems(c, slices[default][c.id])
    if insight_problems:
        raise DashboardProblems(insight_problems)
    return {
        "title": spec.title, "goal": spec.goal, "notes": spec.notes,
        "source": {"name": source.name, "label": source.label, "description": source.description,
                   "range": compiler._range_text() if compiler.time_bounds else None},
        "metrics": {m.id: {"label": m.label, "format": m.format} for m in spec.metrics},
        "charts": [{"id": c.id, "title": c.title, "type": c.type, "metrics": c.metrics, "why": c.why,
                    "ratio_parts": ([metrics[c.metrics[0]].left, metrics[c.metrics[0]].right]
                                    if metrics[c.metrics[0]].agg == "ratio" and len(c.metrics) == 1 else None),
                    "reference": c.reference, "highlight": c.highlight, "width": c.width, "section": c.section,
                    "metric_units": {m: compiler.unit_of(c.model_copy(update={"metrics": [m]}), metrics)
                                     for m in c.metrics},
                    "insight": c.insight, "unit": compiler.unit_of(c, metrics),
                    "x_label": (c.x.label or c.x.column) if c.x else None,
                    "series_label": (c.series.label or c.series.column) if c.series else None}
                   for c in spec.charts],
        "filters": filters, "default_slice": default, "slices": slices,
    }


def preview_chart(con: duckdb.DuckDBPyConnection, metrics: list[Metric], chart: Chart) -> dict[str, Any]:
    """先看一张图的数据长什么样（不加筛选）。返回的数字可以写进 insight。"""
    compiler = Compiler(con)
    by_id = {m.id: m for m in metrics}
    problems = metric_problems(metrics, compiler.types) + chart_problems(chart, by_id, compiler.types,
                                                                          compiler.source)
    if problems:
        raise DashboardProblems(problems)
    problems = compiler.data_problems(metrics, [chart])
    if problems:
        raise DashboardProblems(problems)
    data = compiler.chart_data(chart, by_id)
    problems = highlight_problems(chart, data)
    if problems:
        raise DashboardProblems(problems)
    rows = data["rows"]
    return {"tool": "preview_chart", "chart": chart.id, "type": chart.type, "unit": compiler.unit_of(chart, by_id),
            "rows": rows[:PREVIEW_ROWS], "row_count": len(rows), "facts": data["facts"], "notes": data["notes"],
            "truncated": len(rows) > PREVIEW_ROWS}
