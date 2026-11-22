"""看板可用的数据：每份数据一张表，加上我们写的语义标注。

每份数据只给一张表：看板里每个指标都是对这张表的一次聚合，不需要模型去关联多张表。
语义标注告诉模型和校验器哪些坑不能踩，校验器会强制执行：
- pin_by：某个数值列的含义取决于另一列。电商的 amount 要看 line_type（销售还是取消），
  财报的 value 要看 item（营业收入还是总资产）。聚合这些列时必须按那一列过滤或分组，否则就是把不同的东西加在一起
- unit_column：数值列的单位在另一列（财报的币种）。同一张图里不能混合不同单位
- units：单位固定的数值列（电商金额都是英镑）
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from ..cleaning import fetch_one, fetch_rows
from ..financials import COMPANIES, ITEMS

MAX_TOP_VALUES = 12  # 取值很多的文本列（编号、商品名）只列出最常见的这么多个
DIMENSION_MAX_DISTINCT = 60  # 取值不超过这么多的文本列，算作可以分组的维度


@dataclass(frozen=True)
class Source:
    name: str
    label: str
    table: str
    description: str
    columns: dict[str, str]  # 列名 → 中文说明
    connect: Callable[[], duckdb.DuckDBPyConnection]
    time_column: str | None = None
    pin_by: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unit_column: dict[str, str] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    # 会被之后的记录抵消的数值：列 → (分类列, 原记录的取值, 抵消记录的取值)。电商的销售会被之后的取消抵消
    offsets: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    row_meaning: str | None = None  # 数据说明里写明的“一行是什么”；用户给的 CSV 没有
    # 抵消的金额记在另一列（M23，口径配置的表）：列 → (记着抵消金额的列, 抵消记录叫什么)。gmv_amount 被 cancelled_amount 抵消
    offset_columns: dict[str, tuple[str, str]] = field(default_factory=dict)
    # 口径配置里的指标（M23）：id → 看板方案里的写法（agg、column、where、left、right、format）加 label、definition。
    # 看板里和它们同名的指标，定义必须一致（spec.definition_problems）
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)


def _connect_retail() -> duckdb.DuckDBPyConnection:
    from ..cleaning import connect
    from ..dataset import PARQUET_PATH

    if not PARQUET_PATH.exists():
        raise ValueError("还没有准备电商数据，请先运行：da-agent prepare-data")
    con = connect(PARQUET_PATH)
    _retail_view(con)
    return con


def _retail_view(con: duckdb.DuckDBPyConnection) -> None:
    """销售和取消合成一张交易表（清洗规则已经应用过），金额和数量都取正数，用 line_type 区分。"""
    def columns(line_type: str) -> str:
        return (f"invoice, invoice_date AS invoice_time, '{line_type}' AS line_type, stock_code, description, "
                "abs(quantity) AS quantity, price, abs(amount) AS amount, customer_id, country")

    con.execute(f"""CREATE OR REPLACE VIEW transactions AS
                    SELECT {columns('销售')} FROM sales UNION ALL SELECT {columns('取消')} FROM cancellations""")


def _connect_financials() -> duckdb.DuckDBPyConnection:
    from ..company import connect_financials

    con = connect_financials()
    _financials_view(con)
    return con


def _financials_view(con: duckdb.DuckDBPyConnection) -> None:
    """关键科目表加上公司和科目的中文名。"""
    names = " ".join(f"WHEN '{key}' THEN '{c.name}'" for key, c in COMPANIES.items())
    labels = " ".join(f"WHEN '{key}' THEN '{item.label}'" for key, item in ITEMS.items())
    con.execute(f"""CREATE OR REPLACE VIEW financial_facts AS
                    SELECT company, CASE company {names} ELSE company END AS company_name, fiscal_year,
                           item, CASE item {labels} ELSE item END AS item_label, value, currency,
                           CAST(period_end AS DATE) AS period_end
                    FROM key_facts""")


SOURCES: dict[str, Source] = {
    "retail": Source(
        "retail", "电商交易（UCI Online Retail II，已清洗）", "transactions",
        "一家英国线上礼品批发零售商 2009-12 至 2011-12 的商品交易明细，每行是订单里的一个商品。"
        "清洗规则已经应用：删除了重复、坏账调整、单价为 0 等记录，运费等非商品行不在表里。",
        {"invoice": "发票号（订单号），C 开头的是取消单", "invoice_time": "下单（或取消）时间",
         "line_type": "销售 / 取消", "stock_code": "商品编码", "description": "商品名称",
         "quantity": "数量（取正数）", "price": "单价（英镑）", "amount": "金额 = 数量 × 单价（英镑，取正数）",
         "customer_id": "客户 ID，缺失表示游客或未登记客户", "country": "客户所在国家"},
        _connect_retail, time_column="invoice_time", row_meaning="订单里的一个商品（取消单里的一个商品）",
        pin_by={"amount": ("line_type",), "quantity": ("line_type",)}, units={"amount": "£", "price": "£"},
        offsets={"amount": ("line_type", "销售", "取消")}),
    "financials": Source(
        "financials", "上市公司年报关键科目（SEC）", "financial_facts",
        "阿里巴巴、京东、拼多多、亚马逊 FY2017 起的年报关键科目，长表：每行是一家公司一个财年的一个科目。"
        "金额是各公司编报币种的原始数字，没有做汇率换算。阿里财年截至 3 月 31 日，其他三家是自然年。",
        {"company": "公司代码", "company_name": "公司中文名", "fiscal_year": "财年（按财年结束日期所在年份）",
         "item": "科目代码", "item_label": "科目中文名", "value": "金额（原始数字，单位见 currency）",
         "currency": "币种：CNY 人民币、USD 美元", "period_end": "期末日期"},
        _connect_financials, pin_by={"value": ("item", "item_label")}, unit_column={"value": "currency"},
        row_meaning="一家公司一个财年的一个科目"),
}


CSV_TABLE = "csv_data"
# 读 CSV 时不自动转成布尔：yes / no 这样的取值保留原样，模型看到的就是数据本来的写法
CSV_TYPES = ["BIGINT", "DOUBLE", "TIMESTAMP", "DATE", "VARCHAR"]


# 年月写成文字：2011-01、2011/1、2011.01、2011年1月。整列都是这种写法时转成日期（取每月 1 号），不然看板认不出时间列
MONTH_TEXT_SQL = r"^\s*\d{4}\s*[-/.年]\s*\d{1,2}\s*月?\s*$"


def _month_text_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """整列（非空的值）都是年月写法的文字列。"""
    found = []
    for name, kind, *_ in con.execute(f'DESCRIBE "{table}"').fetchall():
        if kind != "VARCHAR":
            continue
        q = f'"{name}"'
        filled, matched = con.execute(f"SELECT count({q}), count(*) FILTER (WHERE regexp_matches({q}, ?)) FROM \"{table}\"",
                                      [MONTH_TEXT_SQL]).fetchone()
        if filled and filled == matched:
            found.append(name)
    return found


def _months_to_dates(con: duckdb.DuckDBPyConnection, table: str, columns: list[str]) -> None:
    for name in columns:
        q = f'"{name}"'
        con.execute(f"""ALTER TABLE "{table}" ALTER COLUMN {q} TYPE DATE USING
                        make_date(CAST(regexp_extract({q}, '(\\d{{4}})', 1) AS INTEGER),
                                  CAST(regexp_extract({q}, '\\d{{4}}\\s*[-/.年]\\s*(\\d{{1,2}})', 1) AS INTEGER), 1)""")


def csv_source(path: Path | str, label: str | None = None) -> Source:
    """用户给的任意 CSV：一张表，没有语义标注，也没有列的说明。分隔符、类型由 DuckDB 自动识别。

    写成文字的年月（“2011-01”）DuckDB 认不出是时间，这里整列转成日期，并写进数据说明（M22，和 Excel 入口一致）。
    """
    path = Path(path).resolve()
    if not path.exists():
        raise ValueError(f"找不到 CSV 文件：{path}")
    name = f"csv:{path.as_posix()}"
    label = label or path.name
    read = f"CREATE TABLE {CSV_TABLE} AS SELECT * FROM read_csv(?, header = true, auto_type_candidates = {CSV_TYPES})"
    probe = duckdb.connect()
    try:
        probe.execute(read, [path.as_posix()])
        months = _month_text_columns(probe, CSV_TABLE)
    finally:
        probe.close()
    description = f"用户提供的 CSV（{path.name}），没有数据说明：列的含义只能从列名和取值判断。"
    if months:
        description += f"读取时程序把这些写成文字的年月转成了日期（取每月 1 号）：{'、'.join(months)}。"

    def connect() -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        con.execute(read, [path.as_posix()])
        _months_to_dates(con, CSV_TABLE, months)
        con.execute("CREATE TABLE dash_source AS SELECT ? AS name, ? AS label", [name, label])
        return con

    return Source(name, label, CSV_TABLE, description, {}, connect, time_column=None)


EXCEL_SUFFIXES = (".xlsx", ".xlsm")


def split_excel_name(name: str) -> tuple[Path, str | None] | None:
    """“xlsx:路径#工作表”或“文件.xlsx#工作表”“文件.xlsx” → (路径, 工作表)；不是 Excel 时返回 None。"""
    text = name.removeprefix("xlsx:")
    for suffix in EXCEL_SUFFIXES:
        at = text.lower().find(suffix + "#")
        if at >= 0:
            return Path(text[:at + len(suffix)]), text[at + len(suffix) + 1:] or None
        if text.lower().endswith(suffix):
            return Path(text), None
    return None


def excel_source(path: Path | str, sheet: str | None = None, label: str | None = None) -> Source:
    """用户给的 Excel（M20）：读一张工作表，整理成规整的表（excel.read_excel_table），之后和 CSV 走同一条路。

    整理时做了什么（找表头、剔除合计行、文本转数字……）连同表头上方的说明文字，写进数据说明交给模型。
    """
    from .excel import read_excel_table

    path = Path(path).resolve()
    if not path.exists():
        raise ValueError(f"找不到 Excel 文件：{path}")
    frame, notes, sheet = read_excel_table(path, sheet)
    name = f"xlsx:{path.as_posix()}#{sheet}"
    label = label or f"{path.name}（{sheet}）"
    description = (f"用户提供的 Excel（{path.name}，工作表“{sheet}”），没有数据说明：列的含义只能从列名、取值和下面的说明判断。"
                   + (f"读取时程序做了这些处理：{'；'.join(notes)}。" if notes else ""))

    def connect() -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        con.register("excel_frame", frame)
        con.execute(f"CREATE TABLE {CSV_TABLE} AS SELECT * FROM excel_frame")
        con.unregister("excel_frame")
        con.execute("CREATE TABLE dash_source AS SELECT ? AS name, ? AS label", [name, label])
        return con

    return Source(name, label, CSV_TABLE, description, {}, connect, time_column=None)


SEMANTIC_PREFIX = "semantic:"


def semantic_source(config: str) -> Source:
    """口径配置里的表（M23）：和查询 Agent 读同一个数据库文件、同一张表、同一份指标口径。

    config 是口径配置的名字（semantic/<名字>.yaml），配置里要有 dashboard 一节。
    连接时才准备数据库（semantic.prepare：已经准备过、比数据和配置都新时直接复用），以只读方式打开，和查询 Agent 一样。
    """
    from ..semantic import config_path, load_config

    cfg = load_config(config_path(config))
    dash = cfg.dashboard
    if dash is None:
        raise ValueError(f"口径配置 {config} 没有写看板用法（dashboard 一节），看板不能用它")
    table = cfg.tables[dash.table]
    name = f"{SEMANTIC_PREFIX}{config}"
    notes = "".join(f"\n- {note}" for note in cfg.notes)
    description = (f"{cfg.description}表 {dash.table}：{table.description}这张表和查询数据的同事用的是同一份口径配置。"
                   + (f"\n注意：{notes}" if notes else ""))
    metrics = {key: {"id": key, "label": m.label, "definition": m.definition, **m.dashboard}
               for key, m in cfg.metrics.items() if m.dashboard is not None}

    def connect() -> duckdb.DuckDBPyConnection:
        from ..semantic import prepare

        return open_semantic(prepare(config).path, name, cfg.label)

    return Source(name, cfg.label, dash.table, description, dict(table.columns), connect, time_column=dash.time_column,
                  pin_by={k: tuple(v) for k, v in dash.pin_by.items()}, units=dict(dash.units),
                  offsets={k: (o.by, o.value, o.against) for k, o in dash.offsets.items() if o.by and o.value},
                  offset_columns={k: (o.column, o.against) for k, o in dash.offsets.items() if o.column},
                  row_meaning=dash.row_meaning, metrics=metrics)


def open_semantic(path: Path, name: str, label: str) -> duckdb.DuckDBPyConnection:
    """以只读方式打开口径配置的数据库（和查询 Agent 同样的限制），记下它是哪份数据。临时表只属于这个连接。"""
    from ..sqlrun import open_readonly

    con = open_readonly(path)
    con.execute("CREATE TEMP TABLE dash_source AS SELECT ? AS name, ? AS label", [name, label])
    return con


def _with_time_column(con: duckdb.DuckDBPyConnection, source: Source) -> Source:
    """CSV 的时间列：第一个时间戳或日期列（没有就是 None，不能按年筛选）。"""
    for column, kind in column_types(con, source).items():
        if kind in ("timestamp", "date"):
            return Source(**{**source.__dict__, "time_column": column})
    return source


def source_of(con: duckdb.DuckDBPyConnection) -> Source:
    """连接属于哪份数据：CSV 看 dash_source 表里的记录，内置数据看建了哪张表。"""
    tables = {r["name"] for r in fetch_rows(con, "SELECT view_name AS name FROM duckdb_views() UNION ALL "
                                                 "SELECT table_name FROM duckdb_tables()")}
    if "dash_source" in tables:
        meta = fetch_one(con, "SELECT name, label FROM dash_source")
        if meta["name"].startswith(SEMANTIC_PREFIX):
            return semantic_source(meta["name"].removeprefix(SEMANTIC_PREFIX))
        excel = split_excel_name(meta["name"]) if meta["name"].startswith("xlsx:") else None
        source = excel_source(*excel, meta["label"]) if excel else csv_source(meta["name"].removeprefix("csv:"), meta["label"])
        return _with_time_column(con, source)
    for source in SOURCES.values():
        if source.table in tables:
            return source
    raise ValueError("这个连接里没有看板可用的数据表")


def connect_source(name: str) -> duckdb.DuckDBPyConnection:
    """内置数据用名字（retail、financials）；口径配置用 semantic:名字（例如 semantic:retail，和查询 Agent 同一份数据）；
    CSV 用 csv:路径，或者直接给一个 .csv 文件的路径；
    Excel 给 .xlsx 文件的路径，要读别的工作表时写成“文件.xlsx#工作表名”（不写读第一个工作表）。"""
    if name.startswith(SEMANTIC_PREFIX):
        return semantic_source(name.removeprefix(SEMANTIC_PREFIX)).connect()
    if name.startswith("csv:") or name.lower().endswith(".csv"):
        return csv_source(name.removeprefix("csv:")).connect()
    excel = split_excel_name(name)
    if excel:
        return excel_source(*excel).connect()
    if name not in SOURCES:
        raise ValueError(f"没有名为 {name} 的数据；可选：{list(SOURCES)}、semantic:retail，或者一个 .csv / .xlsx 文件的路径")
    return SOURCES[name].connect()


def column_types(con: duckdb.DuckDBPyConnection, source: Source) -> dict[str, str]:
    """列名 → 类型：number（小数）、integer、text、timestamp、date。"""
    out = {}
    for r in fetch_rows(con, f'DESCRIBE "{source.table}"'):
        t = r["column_type"].upper()
        out[r["column_name"]] = ("timestamp" if t.startswith("TIMESTAMP") else "date" if t == "DATE"
                                 else "integer" if t in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT")
                                 else "number" if t in ("DOUBLE", "FLOAT", "REAL") or t.startswith("DECIMAL")
                                 else "text")
    return out


def profile_table(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """给模型看的表格概况：每列的类型、取值个数、缺失比例、范围、最常见的取值，以及必须遵守的语义规则。"""
    source = source_of(con)
    table = f'"{source.table}"'
    rows = fetch_one(con, f"SELECT count(*) AS n FROM {table}")["n"]
    columns = []
    for name, kind in column_types(con, source).items():
        col = f'"{name}"'
        stats = fetch_one(con, f"SELECT count(DISTINCT {col}) AS distinct_values, "
                               f"count(*) FILTER (WHERE {col} IS NULL) AS nulls, min({col}) AS lo, max({col}) AS hi "
                               f"FROM {table}")
        info: dict[str, Any] = {"name": name, "type": kind, "description": source.columns.get(name, ""),
                                "distinct_values": stats["distinct_values"],
                                "null_share": round(stats["nulls"] / rows, 4) if rows else None}
        if kind in ("number", "integer"):
            info["min"], info["max"] = stats["lo"], stats["hi"]
        elif kind in ("timestamp", "date"):
            info["min"], info["max"] = str(stats["lo"]), str(stats["hi"])
        few = stats["distinct_values"] <= DIMENSION_MAX_DISTINCT
        if kind == "text" or (kind == "integer" and few):
            limit = DIMENSION_MAX_DISTINCT if few else MAX_TOP_VALUES  # 维度列列出全部取值，模型写 where 时才知道有哪些
            top = fetch_rows(con, f"SELECT {col} AS value, count(*) AS n FROM {table} GROUP BY 1 "
                                  f"ORDER BY 2 DESC, 1 LIMIT {limit}")
            info["top_values"] = [{"value": r["value"], "rows": r["n"], "row_share": round(r["n"] / rows, 4)}
                                  for r in top]
        info["role_hint"] = ("time" if kind in ("timestamp", "date") or name == source.time_column
                             else "measure" if kind == "number" or (kind == "integer" and not few)
                             else "dimension" if few
                             else "id")
        if name == "fiscal_year":
            info["role_hint"] = "time"
        columns.append(info)
    rules = [f"{value} 的含义取决于 {' 或 '.join(by)}：聚合 {value} 时必须用 where 把 {by[0]} 限定为一个取值，"
             f"或者在图里按 {by[0]} 分组，否则就是把不同的东西加在一起" for value, by in source.pin_by.items()]
    rules += [f"{value} 的单位在 {unit} 列：同一张图里只能出现一种单位；比较不同单位的分组时只比比率"
              for value, unit in source.unit_column.items()]
    rules += [f"{column} 的单位是 {unit}" for column, unit in source.units.items()]
    rules += [f"{value}（{by} = {orig}）可能被之后 {by} = {offset} 的记录抵消：按下单口径给类别排名时，"
              f"大部分被抵消的分组会被高估，preview 的 notes 会提示；看真实成交用 difference（{orig} − {offset}）"
              for value, (by, orig, offset) in source.offsets.items()]
    rules += [f"{value} 可能被之后的{against}抵消（抵消的金额在 {column} 列）：按下单口径给类别排名时，"
              f"大部分被抵消的分组会被高估，preview 的 notes 会提示；看真实成交用 difference（{value} 的合计 − {column} 的合计）"
              for value, (column, against) in source.offset_columns.items()]
    if source.metrics:
        rules.append("metrics 是口径配置里定义好的指标，查询数据和固定报表用的是同一套口径：用到这些指标时照抄它们的 "
                     "agg、column、where、left、right、format（ratio 引用的两个指标也要一起定义）；看板里的指标和它们同名"
                     "（id 或显示名相同）时定义必须一致，校验器会检查。需要别的口径就换一个名字，并在 notes 里写明")
    profile = {"tool": "profile_table", "source": source.name, "label": source.label, "table": source.table,
               "description": source.description, "rows": rows, "time_column": source.time_column,
               "row_identity": _row_identity(con, source, columns, rows), "possible_missing": _possible_missing(columns),
               "columns": columns, "rules": rules}
    if source.metrics:
        profile["metrics"] = list(source.metrics.values())
    return profile


MISSING_LIKE = {"unknown", "unk", "n/a", "na", "null", "missing", "?", "未知", "不详", "缺失", ""}


def _possible_missing(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """看起来是缺失值的取值（unknown、未知……）：它们在图里会单独成一组，按真实分组解读会出错。
    M13e 的一次运行没写 default 有 20.9% 是 unknown；这里直接把占比算出来，任何数据都适用。"""
    return [{"column": c["name"], "value": v["value"], "rows": v["rows"], "share": v["row_share"]}
            for c in columns for v in c.get("top_values", [])
            if isinstance(v["value"], str) and v["value"].strip().lower() in MISSING_LIKE]


def _row_identity(con: duckdb.DuckDBPyConnection, source: Source, columns: list[dict[str, Any]],
                  rows: int) -> dict[str, Any]:
    """一行代表什么：哪些列能唯一标识一行、有多少完全重复的行。这些是算出来的事实，对任何数据都成立。

    M13b 盲测里，3 次有 2 次把“一行是一次通话、同一客户会重复计入”的猜测写成了事实；数据里没有编号列，
    其实无法判断。所以没有说明、也没有唯一标识时，明确告诉模型：判断不了，就按“记录”说。
    """
    unique = [c["name"] for c in columns if c["distinct_values"] == rows and c["null_share"] == 0]
    distinct_rows = fetch_one(con, f'SELECT count(*) AS n FROM (SELECT DISTINCT * FROM "{source.table}")')["n"]
    duplicates = rows - distinct_rows
    if source.row_meaning:
        note = f"数据说明：每行是{source.row_meaning}"
    elif unique:
        note = f"{'、'.join(unique)} 在每行都不同，可以唯一标识一行；一行代表什么仍要结合列名判断，不确定时不要断言"
    else:
        note = ("没有哪一列能唯一标识一行，也没有数据说明：无法判断一行代表一个客户、一次联系还是一笔交易。"
                "按行计数的指标叫“记录数”，不要断言一行是什么，也不要断言同一对象会不会重复出现")
    if duplicates:
        note += f"；有 {duplicates} 行和其他行完全相同"
    return {"unique_columns": unique, "duplicate_rows": duplicates, "note": note}
