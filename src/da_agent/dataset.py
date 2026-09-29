"""下载并准备 UCI Online Retail II 数据集。

流程：下载 zip → 校验 SHA256 → 解压出 xlsx → 读取两个工作表 → 统一列名和类型 → 保存为 parquet。
原始文件和 parquet 都不进 git：体积大，而且随时可以用本模块重新生成。
"""

import hashlib
import logging
import urllib.request
import zipfile
from pathlib import Path

import duckdb
import pandas as pd

from .paths import PROCESSED_DIR, RAW_DIR

LOGGER = logging.getLogger(__name__)

DATA_URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
# UCI 没有公布官方校验值。这里记录的是 2026-09-29 首次下载时算出的 SHA256；
# 以后再下载如果对不上，说明文件被替换或下载损坏，程序会停下来。
EXPECTED_SHA256: str | None = "572e36277c2390fbfde10664750731e0a86f55e33470d91919085f0408e67bfb"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
ZIP_NAME = "online_retail_ii.zip"
PARQUET_PATH = PROCESSED_DIR / "transactions.parquet"
# 两个工作表的名字（2026-09-29 核对）。第 1 表延续到 2010-12-09，与第 2 表重叠，清洗规则 R01 处理。
FIRST_SHEET = "Year 2009-2010"
SECOND_SHEET = "Year 2010-2011"


class DataIntegrityError(RuntimeError):
    """文件和预期不一致：校验值不符、体积超限、内容结构不对。"""


def sha256_of(path: Path) -> str:
    """分块计算文件的 SHA256，大文件也不会占满内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path, *, expected_sha256: str | None,
             max_bytes: int = MAX_DOWNLOAD_BYTES, timeout: float = 60) -> Path:
    """先下载到 .part 临时文件，校验通过后再改名为正式文件，避免留下半截文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    received = 0
    too_large = False
    with urllib.request.urlopen(url, timeout=timeout) as response, partial.open("wb") as file:
        while chunk := response.read(1024 * 1024):
            received += len(chunk)
            if received > max_bytes:
                too_large = True
                break
            file.write(chunk)
    if too_large:
        partial.unlink()
        raise DataIntegrityError(f"文件超过 {max_bytes // 1024 // 1024} MB 上限，已中止下载")
    actual = sha256_of(partial)
    if expected_sha256 is None:
        LOGGER.warning("未设置预期 SHA256，本次无法校验；实际 SHA256=%s", actual)
    elif actual != expected_sha256:
        partial.unlink()
        raise DataIntegrityError(f"SHA256 不一致：预期 {expected_sha256}，实际 {actual}")
    partial.replace(target)
    LOGGER.info("下载完成：%s（%.1f MB）", target.name, received / 1024 / 1024)
    return target


def extract_xlsx(zip_path: Path, out_dir: Path) -> Path:
    """从 zip 中取出唯一的 xlsx 文件。

    只取文件名、丢掉 zip 里的目录部分，防止恶意压缩包把文件写到别的目录（zip slip）。
    """
    with zipfile.ZipFile(zip_path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".xlsx")]
        if len(members) != 1:
            raise DataIntegrityError(f"压缩包里应有且只有 1 个 xlsx 文件，实际：{members}")
        target = out_dir / Path(members[0]).name
        target.write_bytes(archive.read(members[0]))
    return target


# 实际文件的列名（2026-09-29 核对）。注意和 UCI 网页上写的 InvoiceNo / UnitPrice / CustomerID 不同。
COLUMN_MAP = {
    "Invoice": "invoice",
    "StockCode": "stock_code",
    "Description": "description",
    "Quantity": "quantity",
    "InvoiceDate": "invoice_date",
    "Price": "price",
    "Customer ID": "customer_id",
    "Country": "country",
}


def read_workbook(xlsx_path: Path) -> pd.DataFrame:
    """读取所有工作表并上下拼接，记录每行来自哪个工作表、第几行，方便追溯。"""
    sheets = pd.read_excel(xlsx_path, sheet_name=None, dtype=object, engine="openpyxl")
    frames = []
    for sheet_name, frame in sheets.items():
        missing = set(COLUMN_MAP) - set(frame.columns)
        if missing:
            raise DataIntegrityError(f"工作表 {sheet_name} 缺少列：{sorted(missing)}")
        frame = frame[list(COLUMN_MAP)].rename(columns=COLUMN_MAP)
        frame["source_sheet"] = sheet_name
        frame["source_row"] = range(2, len(frame) + 2)  # Excel 行号：第 1 行是表头
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def normalize(frame: pd.DataFrame) -> pd.DataFrame:
    """统一类型。发票号、商品编码、客户 ID 都是“编号”不是“数值”，一律转成字符串。"""
    result = pd.DataFrame({
        "invoice": frame["invoice"].astype(str).str.strip(),
        "stock_code": frame["stock_code"].astype(str).str.strip(),
        "description": frame["description"].astype("string").str.strip(),
        "quantity": pd.to_numeric(frame["quantity"], errors="raise").astype("int64"),
        "invoice_date": pd.to_datetime(frame["invoice_date"], errors="raise"),
        "price": pd.to_numeric(frame["price"], errors="raise").astype("float64"),
        # 客户 ID 在 Excel 里可能是 13085 或 13085.0：先转整数再转字符串，缺失保持为空
        "customer_id": pd.to_numeric(frame["customer_id"], errors="raise").astype("Int64").astype("string"),
        "country": frame["country"].astype("string").str.strip(),
        "source_sheet": frame["source_sheet"].astype(str),
        "source_row": frame["source_row"].astype("int64"),
    })
    return result


def write_parquet(frame: pd.DataFrame, path: Path) -> Path:
    """用 DuckDB 把 DataFrame 写成 parquet（不需要额外安装 pyarrow）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as con:
        con.register("frame", frame)
        con.execute(f"COPY frame TO '{path.as_posix()}' (FORMAT parquet)")
    return path


def prepare(*, force: bool = False) -> Path:
    """一键准备数据：缺什么补什么。已有 parquet 且未要求重建时直接返回。"""
    if PARQUET_PATH.exists() and not force:
        LOGGER.info("已存在 %s，跳过（需要重建请加 --force）", PARQUET_PATH.name)
        return PARQUET_PATH
    zip_path = RAW_DIR / ZIP_NAME
    if zip_path.exists():
        actual = sha256_of(zip_path)
        if EXPECTED_SHA256 and actual != EXPECTED_SHA256:
            raise DataIntegrityError(f"本地压缩包 SHA256 不一致：预期 {EXPECTED_SHA256}，实际 {actual}；请删除后重新下载")
    else:
        download(DATA_URL, zip_path, expected_sha256=EXPECTED_SHA256, timeout=180)
    xlsx_path = extract_xlsx(zip_path, RAW_DIR)
    LOGGER.info("读取 Excel（约需 2 分钟）……")
    frame = normalize(read_workbook(xlsx_path))
    LOGGER.info("共 %d 行，写入 %s", len(frame), PARQUET_PATH.name)
    return write_parquet(frame, PARQUET_PATH)
