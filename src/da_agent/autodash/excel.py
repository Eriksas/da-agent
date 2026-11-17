"""读 Excel 工作表（M20）：把运营同学手上那种 Excel 整理成一张规整的表，并记下做了哪些处理。

真实业务里的 Excel 很少是“第一行表头、下面全是数据”。这里处理最常见的几种情况，每一处处理都写进 notes，
连同表头上方的说明文字（常常写着单位、口径）一起告诉模型和看板的读者：
- 表头上面有标题行、制表人、空行：找到真正的表头所在的行，上面的文字保留为说明；
- 末尾或中间有“合计 / 总计 / 小计”行：剔除。不剔除的话，任何加总都会把总数再算一遍；
- 空行、空列：去掉；
- 数字存成了文本（“1,234.50”“12.5%”“£ 300”）：整列都能转成数字时才转换，转不了的保持原样；
- 日期、年月写成了文字（“2011-01-03”“2011-01”“2011年1月”）：整列都是这种写法时转成日期（年月取每月 1 号），
  不然看板认不出时间列，按月的折线图画不了（M20 第一次真实运行就卡在这里，工具次数用完也没交出方案）；
- 表头为空或重名：改成“列3”“GMV_2”这样不重名的名字。

不处理的（遇到时照原样交给模型，由它在说明里写明）：合并单元格展开、一张工作表里有多张表、透视表、公式重算。
"""

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

HEADER_SCAN_ROWS = 20  # 只在前这么多行里找表头
TOTAL_LABEL = re.compile(r"^(合\s*计|总\s*计|小\s*计|总\s*和|汇\s*总|全\s*部|grand\s*total|sub\s*total|subtotal|total|sum)[:：]?$",
                         re.IGNORECASE)
DATE_TEXT = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
MONTH_TEXT = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*月?$")
NUMBER_TEXT = re.compile(r"^[-+]?[£$¥€￥]?\s*[-+]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?\s*%?$")
MAX_NOTE_TEXT = 80


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or (isinstance(value, str) and not value.strip())


def _clean_text(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and not NUMBER_TEXT.match(value.strip())


def sheet_names(path: Path) -> list[str]:
    with pd.ExcelFile(path, engine="openpyxl") as book:
        return [str(name) for name in book.sheet_names]


def _find_header(rows: list[list[Any]]) -> int | None:
    """表头所在的行（从 0 数）：前几行里第一行“非空单元格够多，而且全是文字”的行，下面还要有数据。"""
    filled = [sum(not _blank(v) for v in row) for row in rows[:HEADER_SCAN_ROWS]]
    if not filled or max(filled) < 2:
        return None
    needed = max(2, (max(filled) + 1) // 2)
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        cells = [v for v in row if not _blank(v)]
        if len(cells) >= needed and all(_is_text(v) for v in cells) and i + 1 < len(rows):
            return i
    return None


def _unique_names(header: list[Any]) -> tuple[list[str], list[str]]:
    names, renamed, seen = [], [], set()
    for i, value in enumerate(header):
        base = str(value).strip().replace("\n", " ") if not _blank(value) else f"列{i + 1}"
        if _blank(value):
            renamed.append(f"第 {i + 1} 列没有表头，命名为“{base}”")
        name, n = base, 2
        while name in seen:
            name, n = f"{base}_{n}", n + 1
        if name != base:
            renamed.append(f"表头“{base}”重复，改名为“{name}”")
        seen.add(name)
        names.append(name)
    return names, renamed


def _to_number(text: str) -> float:
    cleaned = re.sub(r"[£$¥€￥,\s]", "", text)
    percent = cleaned.endswith("%")
    value = float(cleaned.rstrip("%"))
    return value / 100 if percent else value


def _convert_column(series: pd.Series) -> tuple[pd.Series, str | None]:
    """整列都是数字（含文本格式的数字）时转成数字；整列都是日期时转成日期。返回（新列，说明）。"""
    values = [v for v in series if not _blank(v)]
    if not values:
        return series, None
    if all(isinstance(v, (datetime, date, pd.Timestamp)) for v in values):
        return pd.to_datetime(series, errors="coerce"), None
    if all(isinstance(v, str) and (DATE_TEXT.match(v.strip()) or MONTH_TEXT.match(v.strip())) for v in values):
        months = all(MONTH_TEXT.match(v.strip()) for v in values)

        def parse(v: Any) -> Any:
            if _blank(v):
                return None
            found = (MONTH_TEXT if months else DATE_TEXT).match(v.strip())
            return datetime(int(found.group(1)), int(found.group(2)), 1 if months else int(found.group(3)))
        return pd.to_datetime(series.map(parse)), ("年月写成了文字，已转成日期（取每月 1 号）" if months else "日期写成了文字，已转成日期")
    numeric = [isinstance(v, (int, float)) and not isinstance(v, bool) for v in values]
    texts = [isinstance(v, str) and bool(NUMBER_TEXT.match(v.strip())) for v in values]
    if not all(n or t for n, t in zip(numeric, texts, strict=True)):
        return series.map(lambda v: None if _blank(v) else str(v).strip()), None
    converted = series.map(lambda v: None if _blank(v) else _to_number(v) if isinstance(v, str) else float(v))
    note = f"{sum(texts)} 个数字存成了文本，已转成数字" if any(texts) else None
    if converted.dropna().map(float.is_integer).all():
        converted = converted.astype("Int64")
    return converted, note


def read_excel_table(path: Path, sheet: str | None = None) -> tuple[pd.DataFrame, list[str], str]:
    """读一张工作表，返回（整理后的表，做了哪些处理，读的是哪个工作表）。找不到表头时报错，提示换工作表。"""
    sheets = sheet_names(path)
    if sheet is None:
        sheet = sheets[0]
    elif sheet not in sheets:
        raise ValueError(f"{path.name} 里没有名为“{sheet}”的工作表；有：{sheets}")
    raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object, engine="openpyxl")
    notes = [f"工作簿有 {len(sheets)} 个工作表（{'、'.join(sheets)}），读的是“{sheet}”"] if len(sheets) > 1 else []
    rows = [[_clean_text(v) for v in row] for row in raw.itertuples(index=False, name=None)]
    keep_columns = [j for j in range(raw.shape[1]) if any(not _blank(row[j]) for row in rows)]
    if len(keep_columns) < raw.shape[1]:
        notes.append(f"去掉了 {raw.shape[1] - len(keep_columns)} 个空列")
    rows = [[row[j] for j in keep_columns] for row in rows]
    header_at = _find_header(rows)
    if header_at is None:
        others = [s for s in sheets if s != sheet]
        raise ValueError(f"工作表“{sheet}”里找不到表头（一行里至少两个文字单元格）"
                         + (f"；可以用 --sheet 指定别的工作表：{others}" if others else ""))
    above = [" ".join(str(v) for v in row if not _blank(v)) for row in rows[:header_at]]
    above = [text for text in above if text]
    if header_at:
        notes.append(f"表头在第 {header_at + 1} 行，上面 {header_at} 行是标题或说明"
                     + (f"：{'；'.join(t[:MAX_NOTE_TEXT] for t in above)}" if above else ""))
    names, renamed = _unique_names(rows[header_at])
    notes += renamed
    body = [row for row in rows[header_at + 1:] if any(not _blank(v) for v in row)]
    empty = len(rows) - header_at - 1 - len(body)
    if empty:
        notes.append(f"去掉了 {empty} 个空行")
    totals = [row for row in body if any(isinstance(v, str) and TOTAL_LABEL.match(v.strip()) for v in row)]
    if totals:
        labels = sorted({v.strip() for row in totals for v in row if isinstance(v, str) and TOTAL_LABEL.match(v.strip())})
        notes.append(f"剔除了 {len(totals)} 行“{'、'.join(labels)}”（汇总行；不剔除的话，任何加总都会把总数再算一遍）")
        body = [row for row in body if row not in totals]
    if not body:
        raise ValueError(f"工作表“{sheet}”的表头下面没有数据")
    frame = pd.DataFrame(body, columns=names)
    for name in names:
        frame[name], note = _convert_column(frame[name])
        if note:
            notes.append(f"列“{name}”{'有 ' if '个数字' in note else ''}{note}")
    return frame, notes, sheet
