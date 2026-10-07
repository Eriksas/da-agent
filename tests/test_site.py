"""项目展示网站：用仓库里已提交的真实数据生成全部页面，检查数字、链接、转义和密钥。"""

import re
from pathlib import Path

import pytest

from da_agent.secret_scan import find_secrets
from da_agent.site import build_site, grouped_columns, render_markdown


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("site")
    build_site(out)
    return out


def read(site: Path, name: str) -> str:
    return (site / name).read_text(encoding="utf-8")


def test_untrusted_text_cannot_inject_html() -> None:
    """模型回答和问题都当作不可信文本：里面的 HTML 只显示成文字。"""
    rendered = render_markdown('<script>alert(1)</script> **粗体** <img src=x onerror="alert(1)">')
    assert "<script>" not in rendered and "<img" not in rendered
    assert "&lt;script&gt;" in rendered and "<strong>粗体</strong>" in rendered


def test_relative_links_point_to_github() -> None:
    rendered = render_markdown("[记录](runs/a/run.json) [外部](https://example.com) [锚点](#x)", "https://g/base/")
    assert 'href="https://g/base/runs/a/run.json"' in rendered
    assert 'href="https://example.com"' in rendered and 'href="#x"' in rendered


def test_all_pages_are_built(site: Path) -> None:
    names = {p.relative_to(site).as_posix() for p in site.rglob("*.html")}
    assert {"index.html", "evals.html", "company.html", "weekly/index.html", "runs/index.html",
            "weekly/2010-W02.html"} <= names
    assert len([n for n in names if n.startswith("runs/") and n != "runs/index.html"]) == 4  # 4 次真实运行
    assert (site / ".nojekyll").exists()


def test_numbers_come_from_result_files(site: Path) -> None:
    index = read(site, "index.html")
    assert "18/40 → 38/40" in index and "11/20 → 18/20" in index  # M7b 原题、留出题
    assert "16/16 对 4/8" in index  # M9 财报评测
    company = read(site, "company.html")
    assert "26.9%" in company and "不构成投资建议" in company  # 拼多多 FY2025 ROE，工具现算
    assert "拼多多（PDD Holdings） FY2018 -114.6%" in company  # 图里截掉的极端值写在说明里
    assert "122/129" in read(site, "weekly/index.html")


def test_internal_links_resolve(site: Path) -> None:
    for page in site.rglob("*.html"):
        for href in re.findall(r'href="([^"#]+)"', page.read_text(encoding="utf-8")):
            if href.startswith(("http://", "https://")):
                continue
            assert (page.parent / href).resolve().exists(), f"{page.name} → {href}"


def test_no_secrets_in_site(site: Path) -> None:
    for page in site.rglob("*.html"):
        assert find_secrets(page.read_text(encoding="utf-8")) == [], page.name


def test_chart_has_tooltips_legend_and_value_labels() -> None:
    svg = grouped_columns(["甲", "乙"], [("改进前", "--series-2", [(0.45, "45%（18/40）"), (0.55, "55%")]),
                                       ("改进后", "--series-1", [(0.95, "95%"), (0.9, "90%")])], "测试")
    assert svg.count('data-tip="') == 4 and svg.count('tabindex="0"') == 4  # 每根柱子可悬停、可用键盘聚焦
    assert '<div class="legend">' in svg and ">95%</text>" in svg
