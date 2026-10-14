"""任意 CSV 入口（M13b）：没有语义标注、没有数据说明；外部数据集的解压。"""

import io
import zipfile
from pathlib import Path

import pytest

from da_agent import external_data
from da_agent.autodash.compile import compile_dashboard
from da_agent.autodash.sources import connect_source, profile_table, source_of
from da_agent.autodash.spec import DashboardSpec

CSV = """age;job;pdays;emp.var.rate;y;signup_date
30;admin.;999;1.1;no;2010-05-03
45;student;3;-1.8;yes;2010-06-10
60;retired;999;-1.8;yes;2011-01-20
22;student;999;1.1;no;2011-02-01
"""


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    path = tmp_path / "campaign.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


def test_csv_keeps_values_as_written_and_has_no_rules(csv_path: Path) -> None:
    with connect_source(str(csv_path)) as con:
        source = source_of(con)
        profile = profile_table(con)
    assert source.name == f"csv:{csv_path.resolve().as_posix()}" and source.time_column == "signup_date"
    cols = {c["name"]: c for c in profile["columns"]}
    assert {v["value"] for v in cols["y"]["top_values"]} == {"yes", "no"}  # 没有被自动转成布尔
    assert cols["pdays"]["top_values"][0] == {"value": 999, "rows": 3, "row_share": 0.75}  # 特殊值看得到，但没人告诉它含义
    assert profile["rules"] == [] and "没有数据说明" in profile["description"]
    assert all(c["description"] == "" for c in profile["columns"])


def test_csv_dashboard_compiles(csv_path: Path) -> None:
    spec = DashboardSpec.model_validate({
        "title": "营销转化", "goal": "看转化率",
        "metrics": [{"id": "subs", "label": "订阅数", "agg": "count", "where": {"y": ["yes"]}},
                    {"id": "contacts", "label": "联系人数", "agg": "count"},
                    {"id": "rate", "label": "转化率", "agg": "ratio", "left": "subs", "right": "contacts", "format": "percent"},
                    {"id": "emp", "label": "就业变化率", "agg": "avg", "column": "emp.var.rate"}],
        "charts": [{"id": "k", "title": "转化率", "type": "kpi", "metrics": ["rate"], "why": "整体"},
                   {"id": "j", "title": "各职业转化率", "type": "bar", "metrics": ["rate"], "x": {"column": "job"},
                    "why": "客群", "insight": "学生的转化率为 50.0%"},
                   {"id": "e", "title": "就业变化率", "type": "kpi", "metrics": ["emp"], "why": "列名带点"}],
        "year_filter": True})
    with connect_source(f"csv:{csv_path.as_posix()}") as con:
        out = compile_dashboard(con, spec)
    assert out["slices"]["all"]["k"]["facts"]["value"] == 0.5
    assert {r["x"]: r["values"][0] for r in out["slices"]["all"]["j"]["rows"]} == {"admin.": 0, "student": 0.5,
                                                                                  "retired": 1}
    assert set(out["slices"]) == {"all", "2010", "2011"}


def test_fetch_extracts_the_nested_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("bank-additional/bank-additional-full.csv", "age;y\n30;no\n")
        z.writestr("bank-additional/bank-additional-names.txt", "说明")
    outer = tmp_path / "bank+marketing.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("bank-additional.zip", inner.getvalue())
    monkeypatch.setattr(external_data, "RAW_DIR", tmp_path)
    dataset = external_data.DATASETS["bank-marketing"]
    (tmp_path / "bank-marketing").mkdir()
    path = external_data.extract(dataset, outer)
    assert path.read_text(encoding="utf-8") == "age;y\n30;no\n"
    assert (tmp_path / "bank-marketing" / "bank-additional-names.txt").exists()
    with pytest.raises(ValueError, match="没有名为"):
        external_data.fetch("nope")


def test_extract_reads_a_flat_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outer = tmp_path / "shoppers.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("online_shoppers_intention.csv", "Month,Revenue\nJune,FALSE\n")
    monkeypatch.setattr(external_data, "RAW_DIR", tmp_path)
    dataset = external_data.DATASETS["online-shoppers"]
    (tmp_path / "online-shoppers").mkdir()
    assert external_data.extract(dataset, outer).read_text(encoding="utf-8") == "Month,Revenue\nJune,FALSE\n"
