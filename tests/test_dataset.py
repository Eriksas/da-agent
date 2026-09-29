"""下载、解压、读取、类型统一：全部用本地临时文件，不联网。"""

import zipfile
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from da_agent.dataset import (
    DataIntegrityError, download, extract_xlsx, normalize, read_workbook, sha256_of, write_parquet,
)


def test_download_verifies_checksum(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello data")
    target = tmp_path / "out" / "data.zip"
    download(source.as_uri(), target, expected_sha256=sha256_of(source))
    assert target.read_bytes() == b"hello data"
    assert not (tmp_path / "out" / "data.zip.part").exists()


def test_download_rejects_wrong_checksum_and_leaves_nothing(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"tampered")
    target = tmp_path / "data.zip"
    with pytest.raises(DataIntegrityError, match="SHA256"):
        download(source.as_uri(), target, expected_sha256="0" * 64)
    assert list(tmp_path.iterdir()) == [source]


def test_download_stops_at_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 100)
    with pytest.raises(DataIntegrityError, match="上限"):
        download(source.as_uri(), tmp_path / "data.zip", expected_sha256=None, max_bytes=10)
    assert list(tmp_path.iterdir()) == [source]


def test_extract_ignores_directories_inside_zip(tmp_path: Path) -> None:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../evil.xlsx", b"content")  # 恶意路径：想写到上级目录
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    extracted = extract_xlsx(archive, out_dir)
    assert extracted == out_dir / "evil.xlsx"
    assert extracted.read_bytes() == b"content"


def test_extract_requires_exactly_one_xlsx(tmp_path: Path) -> None:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("one.xlsx", b"1")
        zf.writestr("two.xlsx", b"2")
    with pytest.raises(DataIntegrityError, match="有且只有 1 个"):
        extract_xlsx(archive, tmp_path)


def _write_workbook(path: Path, sheets: dict[str, pd.DataFrame]) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)


def _sheet(invoices: list, codes: list, descriptions: list, customers: list) -> pd.DataFrame:
    n = len(invoices)
    return pd.DataFrame({
        "Invoice": invoices, "StockCode": codes, "Description": descriptions, "Quantity": [1] * n,
        "InvoiceDate": [datetime(2010, 1, 1, 9, 0)] * n, "Price": [1.5] * n,
        "Customer ID": customers, "Country": ["United Kingdom"] * n,
    })


def test_read_and_normalize_mixed_types(tmp_path: Path) -> None:
    """真实文件里发票号、编码有时是数字有时是字符串，客户 ID 可能带 .0。"""
    path = tmp_path / "book.xlsx"
    _write_workbook(path, {
        "Year A": _sheet([489434, "C489449"], [85048, "79323P"], [" WHITE LIGHTS ", None], [13085.0, None]),
        "Year B": _sheet([536365], ["85123A"], ["HEART"], [17850]),
    })
    frame = normalize(read_workbook(path))
    assert frame["invoice"].tolist() == ["489434", "C489449", "536365"]
    assert frame["stock_code"].tolist() == ["85048", "79323P", "85123A"]
    assert frame["description"].iloc[0] == "WHITE LIGHTS"
    assert frame["customer_id"].iloc[0] == "13085"
    assert pd.isna(frame["customer_id"].iloc[1])
    assert frame["source_sheet"].tolist() == ["Year A", "Year A", "Year B"]
    assert frame["source_row"].tolist() == [2, 3, 2]  # Excel 行号，第 1 行是表头


def test_missing_column_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    _write_workbook(path, {"Year A": _sheet([1], ["A"], ["x"], [1]).drop(columns=["Price"])})
    with pytest.raises(DataIntegrityError, match="缺少列"):
        read_workbook(path)


def test_parquet_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    _write_workbook(path, {"Year A": _sheet([1, 2], ["A", "B"], ["x", "y"], [1, None])})
    parquet = write_parquet(normalize(read_workbook(path)), tmp_path / "t.parquet")
    count = duckdb.sql(f"SELECT count(*) FROM read_parquet('{parquet.as_posix()}')").fetchone()[0]
    assert count == 2
