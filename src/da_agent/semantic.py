"""口径配置（M19）：查询 Agent 能看到哪些表、每一列是什么、每个指标怎么算，都写在一份 YAML 里。

为什么要有这一层：让模型直接对着原始表写 SQL，它每次都要自己决定“取消单算不算”“订单数怎么去重”，
同一个指标两次问可能算出两个数。这里把口径做成两样东西：
- 带口径的列：gmv_amount（销售行是金额，取消行是 0）、order_id（销售行是发票号，取消行是空）……
  对这些列做最普通的聚合，得到的就是和固定工具一致的指标；
- 指标表达式：GMV = sum(gmv_amount)，订单数 = count(DISTINCT order_id)。

换一份数据只需要换一份配置，不改代码；用户直接给一个 CSV 时没有配置，只有自动生成的列概况。

配置里的表在准备阶段算好，存进一个单独的数据库文件；查询时这个文件以只读方式打开（sqlrun.open_readonly），
所以模型能读到的只有配置里声明的表。
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field

from .paths import ROOT
from .sqlrun import open_readonly

SEMANTIC_DIR = ROOT / "semantic"
QUERY_DB_DIR = ROOT / "data" / "processed" / "query"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_LISTED_VALUES = 15  # 取值不超过这么多的文本列，把取值全部列给模型
EXAMPLE_VALUES = 5
CSV_TABLE = "data"
CSV_TYPES = ["BIGINT", "DOUBLE", "TIMESTAMP", "DATE", "VARCHAR"]  # 不自动转布尔：yes / no 保留原样


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TableConfig(_Config):
    description: str = Field(min_length=1)
    sql: str = Field(min_length=1)  # 从基础数据算出这张表的查询
    columns: dict[str, str]  # 列名 → 说明；表里的每一列都要写


class MetricConfig(_Config):
    label: str = Field(min_length=1)
    table: str
    sql: str = Field(min_length=1)  # 聚合表达式，例如 sum(gmv_amount)
    definition: str = Field(min_length=1)
    # 同一个指标在看板方案里的写法（M23）：agg、column、where、left、right、format，和 autodash.spec.Metric 一致；
    # ratio / difference 的 left、right 是配置里别的指标名。tests/test_semantic_dashboard.py 核对两种写法算出来一样
    dashboard: dict[str, Any] | None = None


class OffsetConfig(_Config):
    """会被之后的记录抵消的金额（M23，看板用）：按下单口径给类别排名时，大部分被抵消的分组会被高估。"""

    against: str = Field(min_length=1)  # 抵消它的记录叫什么，例如“取消”
    by: str | None = None  # 同一列、按分类区分：amount 在 line_type = 销售 的行被 line_type = 取消 的行抵消
    value: str | None = None
    column: str | None = None  # 抵消的金额在另一列：gmv_amount 被 cancelled_amount 抵消


class DashboardConfig(_Config):
    """看板 Agent 怎么用这份配置（M23）：用哪张表、时间列，以及校验器要强制执行的规则（见 autodash.sources.Source）。"""

    table: str
    time_column: str | None = None
    row_meaning: str | None = None
    pin_by: dict[str, list[str]] = Field(default_factory=dict)
    units: dict[str, str] = Field(default_factory=dict)
    offsets: dict[str, OffsetConfig] = Field(default_factory=dict)


class SemanticConfig(_Config):
    name: str
    label: str
    description: str
    base: str  # 基础数据从哪里来：retail（清洗后的电商数据）
    tables: dict[str, TableConfig] = Field(min_length=1)
    metrics: dict[str, MetricConfig] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    dashboard: DashboardConfig | None = None  # 不写时看板不能用这份配置


@dataclass(frozen=True)
class QueryDB:
    """一份准备好的查询数据：数据库文件 + 给模型看的数据说明。"""

    path: Path
    catalog: dict[str, Any]

    def connect(self) -> duckdb.DuckDBPyConnection:
        return open_readonly(self.path)


def load_config(path: Path) -> SemanticConfig:
    return SemanticConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def config_path(data: str) -> Path:
    """口径配置的名字（semantic/<名字>.yaml）或配置文件的路径 → 文件路径；找不到时报错并列出可选的名字。"""
    path = Path(data) if data.lower().endswith((".yaml", ".yml")) else SEMANTIC_DIR / f"{data}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in SEMANTIC_DIR.glob("*.yaml"))
        raise ValueError(f"没有名为 {data} 的口径配置；可选：{available}，或者一个 .csv 文件的路径")
    return path


def _profile(con: duckdb.DuckDBPyConnection, table: str, described: dict[str, str]) -> dict[str, Any]:
    """一张表的概况：行数，每列的类型、说明、取值（少的全列，多的举例）或范围、缺失比例。"""
    rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    columns = []
    for name, kind, *_ in con.execute(f'DESCRIBE "{table}"').fetchall():
        quoted = f'"{name}"'
        distinct, missing = con.execute(f'SELECT count(DISTINCT {quoted}), count(*) - count({quoted}) FROM "{table}"').fetchone()
        column: dict[str, Any] = {"name": name, "type": kind, "description": described.get(name, ""), "distinct": distinct}
        if missing:
            column["missing_share"] = round(missing / rows, 4)
        if kind == "VARCHAR":
            top = con.execute(f'SELECT {quoted} FROM "{table}" WHERE {quoted} IS NOT NULL GROUP BY 1 '
                              f"ORDER BY count(*) DESC, 1 LIMIT {MAX_LISTED_VALUES}").fetchall()
            values = [row[0] for row in top]
            column["values" if distinct <= MAX_LISTED_VALUES else "examples"] = (
                values if distinct <= MAX_LISTED_VALUES else values[:EXAMPLE_VALUES])
        elif rows:
            low, high = con.execute(f'SELECT min({quoted}), max({quoted}) FROM "{table}"').fetchone()
            column["range"] = [str(low), str(high)]
        columns.append(column)
    return {"rows": rows, "columns": columns}


def check_dashboard_config(config: SemanticConfig) -> None:
    """看板用法（dashboard 一节）里提到的表、列、指标都要存在，写错了直接报错。"""
    dash = config.dashboard
    if dash is None:
        return
    if dash.table not in config.tables:
        raise ValueError(f"看板用的表 {dash.table} 不在配置里")
    named = [*([dash.time_column] if dash.time_column else []), *dash.pin_by, *dash.units, *dash.offsets]
    named += [column for by in dash.pin_by.values() for column in by]
    for column, offset in dash.offsets.items():
        if bool(offset.column) == bool(offset.by and offset.value):
            raise ValueError(f"看板用法里 {column} 的抵消规则：要么写 by 和 value（同一列按分类区分），要么写 column（另一列）")
        named += [c for c in (offset.by, offset.column) if c]
    missing = sorted({c for c in named if c not in config.tables[dash.table].columns})
    if missing:
        raise ValueError(f"看板用法里的列不在表 {dash.table} 里：{missing}")
    for name, metric in config.metrics.items():
        if metric.dashboard is None:
            continue
        if metric.table != dash.table:
            raise ValueError(f"指标 {name} 有看板写法，但它用的表 {metric.table} 不是看板用的表 {dash.table}")
        for side in ("left", "right"):
            ref = metric.dashboard.get(side)
            if ref is not None and (ref not in config.metrics or config.metrics[ref].dashboard is None):
                raise ValueError(f"指标 {name} 的看板写法引用了 {ref}，它不是配置里有看板写法的指标")


def build_database(config: SemanticConfig, base: duckdb.DuckDBPyConnection, out_path: Path) -> QueryDB:
    """按配置从基础数据算出每张表，写进 out_path；检查配置和数据对得上；生成数据说明。

    对不上的配置直接报错：表里有没写说明的列、说明里有不存在的列、指标表达式跑不通。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    building = out_path.with_name(out_path.name + ".building")
    building.unlink(missing_ok=True)
    base.execute(f"ATTACH '{building.as_posix()}' AS query_out")
    try:
        for name, table in config.tables.items():
            if not IDENTIFIER.match(name):
                raise ValueError(f"表名 {name} 只能用小写字母、数字和下划线")
            base.execute(f'CREATE TABLE query_out."{name}" AS {table.sql}')
    finally:
        base.execute("DETACH query_out")
    con = duckdb.connect(str(building), read_only=True)
    try:
        tables = {}
        for name, table in config.tables.items():
            actual = [row[0] for row in con.execute(f'DESCRIBE "{name}"').fetchall()]
            if set(actual) != set(table.columns):
                raise ValueError(f"表 {name} 的列和说明对不上：没写说明 {sorted(set(actual) - set(table.columns))}，"
                                 f"说明里多出 {sorted(set(table.columns) - set(actual))}")
            tables[name] = {"description": table.description, **_profile(con, name, table.columns)}
        for name, metric in config.metrics.items():
            if metric.table not in config.tables:
                raise ValueError(f"指标 {name} 用的表 {metric.table} 不在配置里")
            try:
                con.execute(f'SELECT {metric.sql} FROM "{metric.table}"').fetchall()
            except duckdb.Error as exc:
                raise ValueError(f"指标 {name} 的表达式跑不通：{str(exc).splitlines()[0]}") from exc
        check_dashboard_config(config)
    finally:
        con.close()
    # 看板写法不放进数据说明：查询 Agent 看到的和以前一字不差
    catalog = {"name": config.name, "label": config.label, "description": config.description, "tables": tables,
               "metrics": {name: metric.model_dump(exclude={"dashboard"}) for name, metric in config.metrics.items()},
               "notes": config.notes}
    os.replace(building, out_path)
    out_path.with_suffix(".catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
                                                     encoding="utf-8", newline="\n")
    return QueryDB(out_path, catalog)


def build_csv_database(csv_path: Path, out_path: Path) -> QueryDB:
    """用户给的 CSV：一张表 data，没有口径配置。列的含义只能从列名和取值判断，数据说明里照实写。"""
    csv_path = csv_path.resolve()
    if not csv_path.exists():
        raise ValueError(f"找不到 CSV 文件：{csv_path}")
    base = duckdb.connect()
    try:
        base.execute(f'CREATE TABLE "{CSV_TABLE}" AS SELECT * FROM read_csv(?, header = true, '
                     f"auto_type_candidates = {CSV_TYPES})", [csv_path.as_posix()])
        columns = {row[0]: "" for row in base.execute(f'DESCRIBE "{CSV_TABLE}"').fetchall()}
        config = SemanticConfig(
            name=f"csv:{csv_path.name}", label=csv_path.name, base="csv",
            description=f"用户提供的 CSV（{csv_path.name}），没有数据说明和口径配置：列的含义只能从列名和取值判断。",
            tables={CSV_TABLE: TableConfig(description="CSV 的全部行，一行是什么需要从数据判断", columns=columns,
                                           sql=f'SELECT * FROM "{CSV_TABLE}"')},
            notes=["没有口径配置：聚合方式由你决定，并在提交时写清楚你是怎么算的。"])
        return build_database(config, base, out_path)
    finally:
        base.close()


def _retail_base() -> duckdb.DuckDBPyConnection:
    from .cleaning import connect
    from .dataset import PARQUET_PATH
    from .metrics import _ensure

    if not PARQUET_PATH.exists():
        raise ValueError("还没有准备电商数据，请先运行：da-agent prepare-data")
    con = connect(PARQUET_PATH)
    _ensure(con)
    return con


BASES = {"retail": _retail_base}


def _load_catalog(path: Path) -> QueryDB:
    return QueryDB(path, json.loads(path.with_suffix(".catalog.json").read_text(encoding="utf-8")))


def _fresh(out_path: Path, *sources: Path) -> bool:
    """数据库文件比它依赖的文件都新，就不用重建。"""
    catalog = out_path.with_suffix(".catalog.json")
    return (out_path.exists() and catalog.exists()
            and all(out_path.stat().st_mtime >= source.stat().st_mtime for source in sources if source.exists()))


def prepare(data: str, *, force: bool = False) -> QueryDB:
    """准备查询数据。data 是口径配置的名字（semantic/<名字>.yaml）、一份配置文件的路径，或一个 .csv 文件的路径。

    已经准备过、并且比数据和配置都新时直接复用。
    """
    if data.lower().endswith(".csv"):
        csv_path = Path(data).resolve()
        out = QUERY_DB_DIR / f"csv-{hashlib.sha1(csv_path.as_posix().encode()).hexdigest()[:12]}.duckdb"
        return _load_catalog(out) if not force and _fresh(out, csv_path) else build_csv_database(csv_path, out)
    path = config_path(data)
    config = load_config(path)
    if config.base not in BASES:
        raise ValueError(f"配置 {config.name} 的基础数据 {config.base} 不认识；可选：{list(BASES)}")
    out = QUERY_DB_DIR / f"{config.name}.duckdb"
    from .dataset import PARQUET_PATH

    if not force and _fresh(out, path, PARQUET_PATH):
        return _load_catalog(out)
    base = BASES[config.base]()
    try:
        return build_database(config, base, out)
    finally:
        base.close()


def catalog_text(catalog: dict[str, Any]) -> str:
    """数据说明的文字版，放进查询 Agent 的提示词。"""
    lines = [f"数据：{catalog['label']}。{catalog['description']}", ""]
    for name, table in catalog["tables"].items():
        lines += [f"### 表 {name}（{table['rows']:,} 行）", table["description"], "", "| 列 | 类型 | 说明 | 取值 |", "|---|---|---|---|"]
        for column in table["columns"]:
            if "values" in column:
                values = "全部取值：" + "、".join(str(v) for v in column["values"])
            elif "examples" in column:
                values = f"{column['distinct']:,} 种，例如 " + "、".join(str(v) for v in column["examples"])
            else:
                values = " 至 ".join(column.get("range", []))
            if column.get("missing_share"):
                values += f"；{column['missing_share']:.1%} 为空"
            lines.append(f"| {column['name']} | {column['type']} | {column['description'] or '（没有说明）'} | {values} |")
        lines.append("")
    if catalog["metrics"]:
        lines += ["### 指标口径（用到这些指标时，必须照这里的表达式写）", "", "| 指标 | 表 | 表达式 | 口径 |", "|---|---|---|---|"]
        lines += [f"| {m['label']}（{name}） | {m['table']} | `{m['sql']}` | {m['definition']} |"
                  for name, m in catalog["metrics"].items()]
        lines.append("")
    if catalog["notes"]:
        lines += ["### 注意", *[f"- {note}" for note in catalog["notes"]], ""]
    return "\n".join(lines)


def metrics_used(catalog: dict[str, Any], sql: str) -> list[str]:
    """SQL 里原样出现了哪些指标的表达式（忽略空白和大小写）。只是给读的人一个提示，不是证明。"""
    squashed = re.sub(r"\s+", "", sql).lower()
    return [name for name, metric in catalog["metrics"].items() if re.sub(r"\s+", "", metric["sql"]).lower() in squashed]
