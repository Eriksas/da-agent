"""生成演示用的 Excel（M20）：模仿运营同学手上的月度报表，数字来自清洗后的电商数据。

故意带上真实 Excel 里常见的坑：合并的标题行和说明行、表头上面的空行、每月一行“小计”、最后一行“合计”、
几个存成文本的数字、一个只有说明文字的工作表。看板入口要自己处理这些，不能把小计、合计再加一遍。

运行：uv run python examples/excel/make_demo_excel.py（需要先 da-agent prepare-data）
"""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from da_agent.cleaning import connect, fetch_rows
from da_agent.dataset import PARQUET_PATH

OUT = Path(__file__).with_name("2011年各国月度销售汇总.xlsx")
COUNTRIES = ["United Kingdom", "Germany", "France", "EIRE", "Netherlands"]
HEADER = ["月份", "国家", "GMV", "取消金额", "订单数", "下单客户数"]


def monthly_rows() -> list[dict]:
    con = connect(PARQUET_PATH)
    group = "CASE WHEN country IN ({}) THEN country ELSE '其他国家' END".format(", ".join(f"'{c}'" for c in COUNTRIES))
    return fetch_rows(con, f"""
        WITH s AS (SELECT strftime(invoice_date, '%Y-%m') AS month, {group} AS country, sum(amount) AS gmv,
                          count(DISTINCT invoice) AS orders, count(DISTINCT customer_id) AS customers
                   FROM sales WHERE year(invoice_date) = 2011 GROUP BY 1, 2),
             c AS (SELECT strftime(invoice_date, '%Y-%m') AS month, {group} AS country, -sum(amount) AS cancelled
                   FROM cancellations WHERE year(invoice_date) = 2011 GROUP BY 1, 2)
        SELECT s.month, s.country, round(s.gmv, 2) AS gmv, round(coalesce(c.cancelled, 0), 2) AS cancelled, s.orders, s.customers
        FROM s LEFT JOIN c USING (month, country)
        ORDER BY s.month, CASE s.country WHEN '其他国家' THEN 1 ELSE 0 END, s.gmv DESC""")


def main() -> None:
    rows = monthly_rows()
    book = Workbook()
    readme = book.active
    readme.title = "说明"
    for i, text in enumerate(["本表由运营部每月 10 号更新。", "GMV 为下单口径，含之后被取消的订单；取消金额按取消发生的月份统计。",
                              "数据截至 2011-12-09，12 月不是完整月。"], start=1):
        readme.cell(row=i, column=1, value=text)
    sheet = book.create_sheet("月度销售")
    sheet["A1"] = "2011 年各国月度销售汇总（单位：英镑）"
    sheet["A1"].font = Font(bold=True, size=14)
    sheet.merge_cells("A1:F1")
    sheet["A2"] = "数据截至 2011-12-09（12 月不完整）；制表：运营部"
    sheet.merge_cells("A2:F2")
    bold, fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    for j, name in enumerate(HEADER, start=1):  # 第 3 行空着，表头在第 4 行
        cell = sheet.cell(row=4, column=j, value=name)
        cell.font, cell.fill, cell.alignment = bold, fill, Alignment(horizontal="center")
    r = 5
    months = sorted({row["month"] for row in rows})
    for month in months:
        mine = [row for row in rows if row["month"] == month]
        for k, row in enumerate(mine):
            values = [month, row["country"], row["gmv"], row["cancelled"], row["orders"], row["customers"]]
            if month == "2011-03" and k < 2:  # 从别的系统粘贴过来的几行：数字存成了文本
                values[2], values[4] = f"{row['gmv']:,.2f}", str(row["orders"])
            for j, value in enumerate(values, start=1):
                sheet.cell(row=r, column=j, value=value)
            r += 1
        subtotal = [month, "小计", round(sum(x["gmv"] for x in mine), 2), round(sum(x["cancelled"] for x in mine), 2),
                    sum(x["orders"] for x in mine), None]  # 客户数不能相加（同一个客户可能在几个国家都下过单），小计里空着
        for j, value in enumerate(subtotal, start=1):
            sheet.cell(row=r, column=j, value=value).font = bold
        r += 1
    total = ["合计", None, round(sum(x["gmv"] for x in rows), 2), round(sum(x["cancelled"] for x in rows), 2),
             sum(x["orders"] for x in rows), None]
    for j, value in enumerate(total, start=1):
        sheet.cell(row=r, column=j, value=value).font = bold
    for column, width in zip("ABCDEF", (10, 18, 14, 12, 10, 12), strict=True):
        sheet.column_dimensions[column].width = width
    for row in sheet.iter_rows(min_row=5, min_col=3, max_col=4):
        for cell in row:
            if isinstance(cell.value, float):
                cell.number_format = "#,##0.00"
    book.save(OUT)
    print(f"{OUT}：{len(rows)} 行数据，{len(months)} 行小计，1 行合计")


if __name__ == "__main__":
    main()
