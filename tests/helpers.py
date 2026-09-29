"""测试共用的小工具：把手写的样例行转成和 dataset.normalize 输出一致的 DataFrame。"""

import pandas as pd

from da_agent.dataset import FIRST_SHEET, SECOND_SHEET

S1, S2 = FIRST_SHEET, SECOND_SHEET
UK = "United Kingdom"
COLUMNS = ["source_sheet", "source_row", "invoice", "stock_code", "description", "quantity",
           "invoice_date", "price", "customer_id", "country"]


def make_frame(rows: list[tuple]) -> pd.DataFrame:
    """每行依次是：工作表, 行号, 发票, 编码, 描述, 数量, 时间, 单价, 客户, 国家。"""
    frame = pd.DataFrame(rows, columns=COLUMNS)
    return frame.astype({"quantity": "int64", "price": "float64", "source_row": "int64",
                         "description": "string", "customer_id": "string", "country": "string"}).assign(
        invoice_date=pd.to_datetime(frame["invoice_date"]))
