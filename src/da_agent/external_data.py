"""外部公开数据集：用来检验 Agent 在没写过规则的数据上能不能自己搭看板（M13b）。

这些数据只交给看板 Agent，当作“用户提供的 CSV”：不写语义标注，也不给数据说明，Agent 只能从列名和取值判断。
数据说明（例如 Bank Marketing 的 bank-additional-names.txt、Online Shoppers 的 UCI 页面）只用来写评分表，不交给 Agent。
"""

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .dataset import DataIntegrityError, download, sha256_of
from .paths import ROOT

RAW_DIR = ROOT / "data" / "raw"


@dataclass(frozen=True)
class ExternalDataset:
    name: str
    url: str
    sha256: str  # 2026-10-14 第一次下载时算出的值（UCI 没有公布官方校验值），以后下载必须一致
    inner_zip: str | None  # 外层压缩包里套着的压缩包；没有套就是 None
    members: tuple[str, ...]  # 要解压出来的文件（第一个是数据，其余是说明）
    license: str
    citation: str

    @property
    def folder(self) -> Path:
        return RAW_DIR / self.name

    @property
    def csv(self) -> Path:
        return self.folder / Path(self.members[0]).name


DATASETS = {
    "bank-marketing": ExternalDataset(
        "bank-marketing", "https://archive.ics.uci.edu/static/public/222/bank+marketing.zip",
        "e0bf5f5de5b846e2f18e9d90606637267d46dfa260e0f17bb12e605db5efbeb4", "bank-additional.zip",
        ("bank-additional/bank-additional-full.csv", "bank-additional/bank-additional-names.txt"),
        "CC BY 4.0", "Moro, S., Rita, P., & Cortez, P. (2014). Bank Marketing. UCI Machine Learning Repository."),
    "online-shoppers": ExternalDataset(
        "online-shoppers", "https://archive.ics.uci.edu/static/public/468/online+shoppers+purchasing+intention+dataset.zip",
        "2972e6184d3ad7beaaa831d9fc2b059dc3ee29df69d1ec593c466a5cd8485d14", None, ("online_shoppers_intention.csv",),
        "CC BY 4.0", "Sakar, C. & Kastro, Y. (2018). Online Shoppers Purchasing Intention Dataset. "
                     "UCI Machine Learning Repository. https://doi.org/10.24432/C5F88Q"),
}


def extract(dataset: ExternalDataset, zip_path: Path) -> Path:
    """解压出数据和说明（有内层压缩包就从内层取），返回数据文件路径。"""
    with zipfile.ZipFile(zip_path) as outer:
        archive = zipfile.ZipFile(io.BytesIO(outer.read(dataset.inner_zip))) if dataset.inner_zip else outer
        with archive:
            for member in dataset.members:
                (dataset.folder / Path(member).name).write_bytes(archive.read(member))
    return dataset.csv


def fetch(name: str) -> Path:
    """下载（已下载就跳过）、校验 SHA256、解压。返回 CSV 路径。"""
    if name not in DATASETS:
        raise ValueError(f"没有名为 {name} 的数据集；可选：{list(DATASETS)}")
    dataset = DATASETS[name]
    dataset.folder.mkdir(parents=True, exist_ok=True)
    zip_path = dataset.folder / Path(dataset.url).name
    if zip_path.exists():
        actual = sha256_of(zip_path)
        if actual != dataset.sha256:
            raise DataIntegrityError(f"本地压缩包 SHA256 不一致：预期 {dataset.sha256}，实际 {actual}；请删除后重新下载")
    else:
        download(dataset.url, zip_path, expected_sha256=dataset.sha256, timeout=180)
    return extract(dataset, zip_path)
