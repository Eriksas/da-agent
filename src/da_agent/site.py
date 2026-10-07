"""项目展示网站：从仓库里已有的数据生成静态网页，由 GitHub Pages 托管（M10）。

GitHub Pages 只能放静态文件：这里不运行 Agent、不调用模型、不需要任何密钥。网页上的每个数字都来自仓库里
已经提交的结果文件，或者由分析工具在生成网页时现算；图表由 Python 直接画成 SVG。

页面：
- index.html：项目介绍、架构图、关键结果
- weekly/：自动周报（每周一由 GitHub Actions 生成，网站随后自动更新）
- evals.html：评测结果
- company.html：上市公司财报比率
- runs/：真实运行回放（问题 → 每一步调用的工具 → 答案 → 核查 → 人工复核）

模型的回答、用户的问题都当作不可信文本：渲染 Markdown 前先转义 “<”，原始 HTML 不会进入网页。
"""

import html
import json
import re
from pathlib import Path
from typing import Any

import markdown

from .company import RATIOS, company_overview, connect_financials, financial_summary
from .paths import ROOT

REPO_URL = "https://github.com/Eriksas/da-agent"
BLOB = f"{REPO_URL}/blob/main/"
RESULTS = ROOT / "eval" / "results"
WEEKLY = ROOT / "reports" / "weekly"
REAL_RUNS = ROOT / "examples" / "real_runs"
NAV = (("index.html", "首页"), ("weekly/index.html", "自动周报"), ("evals.html", "评测"),
       ("company.html", "财报分析"), ("runs/index.html", "运行回放"))
CHART_FROM = 2020  # 财报小多图的起始财年
COMPANY_RATIOS = ("revenue_growth", "gross_margin", "operating_margin", "net_margin", "roe", "debt_ratio",
                  "ocf_to_net_income")

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--text:#0b0b0b;--text-2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--series-1:#2a78d6;--series-2:#eb6834;
--up:#2a78d6;--down:#e34948;--link:#256abf;--code:#f0efec}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;
--surface:#1a1a19;--text:#fff;--text-2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--up:#3987e5;--down:#e66767;
--link:#86b6ef;--code:#262624}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--text:#fff;--text-2:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;
--series-2:#d95926;--up:#3987e5;--down:#e66767;--link:#86b6ef;--code:#262624}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--text);
font:16px/1.65 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
a{color:var(--link)}
header{border-bottom:1px solid var(--border);background:var(--surface)}
.bar{max-width:1000px;margin:0 auto;padding:12px 16px;display:flex;flex-wrap:wrap;gap:6px 20px;align-items:baseline}
.brand{font-weight:600;color:var(--text);text-decoration:none;margin-right:8px}
nav{display:flex;flex-wrap:wrap;gap:4px 18px}
nav a{color:var(--text-2);text-decoration:none;white-space:nowrap}
nav a[aria-current=page]{color:var(--text);font-weight:600}
main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:28px;line-height:1.3;margin:0 0 8px}
h2{font-size:20px;margin:36px 0 12px}
h3{font-size:17px;margin:24px 0 8px}
.lead{color:var(--text-2);margin:0 0 20px;max-width:720px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px 20px;margin:16px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:20px 0}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.tile .label{color:var(--text-2);font-size:14px}
.tile .value{font-size:28px;font-weight:600;line-height:1.3;margin-top:2px}
.tile .note{color:var(--muted);font-size:13px}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0}
th,td{border-bottom:1px solid var(--grid);padding:6px 10px;text-align:left;vertical-align:top}
th{color:var(--text-2);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.muted{color:var(--muted)}
.small{font-size:13px}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--text-2);font-size:13px;margin:4px 0 0}
.legend i{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;vertical-align:-1px}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
svg.chart.wide{max-width:520px}
.chart.wide .tick,.chart.wide .val{font-size:14px}
.chart.wide .cat{font-size:15px}
svg.chart text{font-family:inherit}
.chart .tick{fill:var(--muted);font-size:12px}
.chart .cat{fill:var(--text-2);font-size:13px}
.chart .val{fill:var(--text-2);font-size:12px}
.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .base{stroke:var(--axis);stroke-width:1}
.chart .hit{fill:transparent}
.chart g.mark:hover path,.chart g.mark:focus path,.chart g.mark:hover circle,.chart g.mark:focus circle{opacity:.8}
.chart g.mark:focus{outline:none}
.multiples{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.tip{position:fixed;z-index:10;pointer-events:none;background:var(--surface);color:var(--text);
border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px;white-space:pre-line;
box-shadow:0 2px 8px rgba(0,0,0,.12)}
.prose{max-width:820px}
.prose table{display:block;overflow-x:auto}
.prose blockquote{margin:12px 0;padding:4px 14px;border-left:3px solid var(--axis);color:var(--text-2)}
.prose code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.92em}
.prose pre{background:var(--code);padding:12px;border-radius:6px;overflow-x:auto}
.prose pre code{padding:0;background:none}
.steps td:nth-child(3){font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}
.steps td:nth-child(4),.steps th:nth-child(4){min-width:5em}
.badge{display:inline-block;font-size:12px;padding:1px 8px;border-radius:10px;border:1px solid var(--border);
color:var(--text-2);margin-right:6px}
footer{border-top:1px solid var(--border);color:var(--muted);font-size:13px}
footer .bar{display:block}
"""

TOOLTIP_JS = """
(() => {
  const tip = document.createElement('div');
  tip.className = 'tip'; tip.hidden = true; document.body.append(tip);
  const place = (x, y) => {
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.min(Math.max(8, x + 12), innerWidth - r.width - 8) + 'px';
    tip.style.top = Math.max(8, y - r.height - 12) + 'px';
  };
  document.querySelectorAll('[data-tip]').forEach(el => {
    const show = (x, y) => { tip.textContent = el.dataset.tip; tip.hidden = false; place(x, y); };
    el.addEventListener('pointermove', e => show(e.clientX, e.clientY));
    el.addEventListener('pointerleave', () => { tip.hidden = true; });
    el.addEventListener('focus', () => { const b = el.getBoundingClientRect(); show(b.left + b.width / 2, b.top); });
    el.addEventListener('blur', () => { tip.hidden = true; });
  });
})();
"""


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def pct(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def render_markdown(text: str, link_base: str | None = None) -> str:
    """把 Markdown 渲染成 HTML。先转义“<”：模型回答里的原始 HTML 只会显示成文字，不会执行。

    link_base：把相对链接改写成 GitHub 上的地址（网页里没有这些 JSON 文件）。
    """
    source = text.replace("&", "&amp;").replace("<", "&lt;")
    if link_base:
        source = re.sub(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)", lambda m: f"]({link_base}{m.group(1)})", source)
    return markdown.markdown(source, extensions=["tables", "fenced_code", "sane_lists"])


def page(title: str, body: str, depth: int, active: str, scripts: str = "") -> str:
    """完整的 HTML 页面：统一的页头导航和页脚。depth 是页面所在的目录层数，用来拼相对链接。"""
    up = "../" * depth
    nav = "".join(f'<a href="{up}{href}"{" aria-current=page" if href == active else ""}>{label}</a>'
                  for href, label in NAV)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · da-agent</title>
<style>{CSS}</style>
</head>
<body>
<header><div class="bar"><a class="brand" href="{up}index.html">da-agent</a><nav>{nav}</nav></div></header>
<main>
{body}
</main>
<footer><div class="bar">
网页由 GitHub Actions 根据仓库里的数据自动生成，不运行模型、不含密钥；每个数字都可以在
<a href="{REPO_URL}">GitHub 仓库</a>的结果文件里找到。电商数据：UCI Online Retail II（CC BY 4.0）的历史回放；
财报数据：SEC EDGAR 公开年报。只做数据分析，不构成投资建议。
</div></footer>
<script>{TOOLTIP_JS}</script>
{scripts}
</body>
</html>
"""


# ---------- 图表：Python 直接画 SVG ----------

def _column(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """一根柱子：顶端 4px 圆角，底部贴着基线是直角。"""
    r = min(r, h, w / 2)
    if h <= 0:
        return ""
    return f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + w - r:.1f}" \
           f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z"


def _column_down(x: float, top: float, w: float, h: float, r: float = 4) -> str:
    """向下的柱子（负值）：从基线往下画，圆角在下端。"""
    r = min(r, h, w / 2)
    if h <= 0:
        return ""
    bottom = top + h
    return f"M{x:.1f},{top:.1f}V{bottom - r:.1f}Q{x:.1f},{bottom:.1f} {x + r:.1f},{bottom:.1f}H{x + w - r:.1f}" \
           f"Q{x + w:.1f},{bottom:.1f} {x + w:.1f},{bottom - r:.1f}V{top:.1f}Z"


def grouped_columns(categories: list[str], series: list[tuple[str, str, list[tuple[float, str]]]],
                    title: str) -> str:
    """分组柱状图（纵轴 0–100%）。series：(名称, 颜色变量, [(比例, 提示文字)])。柱顶标数值，悬停看分子分母。"""
    width, height, left, right, top, bottom = 480, 260, 48, 8, 20, 42
    plot_w, plot_h = width - left - right, height - top - bottom
    band = plot_w / len(categories)
    bar, gap = 24, 2
    group = len(series) * bar + (len(series) - 1) * gap
    parts = [f'<svg class="chart wide" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">']
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        y = top + plot_h * (1 - tick)
        parts.append(f'<line class="{"base" if tick == 0 else "grid"}" x1="{left}" x2="{width - right}" '
                     f'y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{tick * 100:.0f}%</text>')
    for i, category in enumerate(categories):
        x0 = left + band * i + (band - group) / 2
        for j, (name, color, values) in enumerate(series):
            value, tip = values[i]
            x = x0 + j * (bar + gap)
            h = plot_h * value
            y = top + plot_h - h
            parts.append(f'<g class="mark" tabindex="0" data-tip="{esc(tip)}">'
                         f'<rect class="hit" x="{x - 4:.1f}" y="{top}" width="{bar + 8}" height="{plot_h}"/>'
                         f'<path d="{_column(x, y, bar, h)}" fill="var({color})"/></g>')
            parts.append(f'<text class="val" x="{x + bar / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle">'
                         f'{value * 100:.0f}%</text>')
        parts.append(f'<text class="cat" x="{left + band * i + band / 2:.1f}" y="{height - 14}" '
                     f'text-anchor="middle">{esc(category)}</text>')
    parts.append("</svg>")
    legend = "".join(f'<span><i style="background:var({color})"></i>{esc(name)}</span>' for name, color, _ in series)
    return f'<div class="legend">{legend}</div>' + "".join(parts)


def change_columns(labels: list[str], values: list[float], tips: list[str], title: str) -> str:
    """有正有负的柱状图（例如 GMV 环比）：0 是基线，上涨蓝色、下跌红色，柱端标数值。"""
    width, height, left, right, top, bottom = 480, 230, 48, 8, 22, 38
    plot_w, plot_h = width - left - right, height - top - bottom
    limit = max(0.05, max(abs(v) for v in values) * 1.15)
    scale = plot_h / (2 * limit)
    zero = top + plot_h / 2
    band = plot_w / len(labels)
    bar = min(24.0, band * 0.6)
    parts = [f'<svg class="chart wide" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">']
    for tick in (-limit, 0, limit):
        y = zero - tick * scale
        parts.append(f'<line class="{"base" if tick == 0 else "grid"}" x1="{left}" x2="{width - right}" '
                     f'y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{"0" if tick == 0 else f"{tick * 100:+.0f}"}%</text>')
    for i, (label, value, tip) in enumerate(zip(labels, values, tips, strict=True)):
        x = left + band * i + (band - bar) / 2
        h = abs(value) * scale
        if value >= 0:
            path, color, label_y = _column(x, zero - h, bar, h), "--up", zero - h - 5
        else:
            path, color, label_y = _column_down(x, zero, bar, h), "--down", zero + h + 14
        parts.append(f'<g class="mark" tabindex="0" data-tip="{esc(tip)}">'
                     f'<rect class="hit" x="{x - 4:.1f}" y="{top}" width="{bar + 8:.1f}" height="{plot_h}"/>'
                     f'<path d="{path}" fill="var({color})"/></g>')
        parts.append(f'<text class="val" x="{x + bar / 2:.1f}" y="{label_y:.1f}" text-anchor="middle">'
                     f'{value * 100:+.2f}%</text>')
        parts.append(f'<text class="cat" x="{x + bar / 2:.1f}" y="{height - 10}" text-anchor="middle">{esc(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def small_line(title: str, points: list[tuple[str, float]], low: float, high: float, unit_label: str) -> str:
    """小多图里的一张折线图：同一组图共用纵轴范围，便于横向比较。末端标数值，每个点可悬停。"""
    width, height, left, right, top, bottom = 260, 150, 40, 44, 14, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    step = plot_w / max(1, len(points) - 1)

    def y_of(value: float) -> float:
        return top + plot_h * (high - value) / (high - low)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">']
    for tick in (low, 0.0, high) if low < 0 else (0.0, high / 2, high):
        y = y_of(tick)
        parts.append(f'<line class="{"base" if tick == 0 else "grid"}" x1="{left}" x2="{width - right}" '
                     f'y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{tick * 100:.0f}%</text>')
    coords = [(left + step * i, y_of(v)) for i, (_, v) in enumerate(points)]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    parts.append(f'<path d="{line}" fill="none" stroke="var(--series-1)" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
    for (x, y), (label, value) in zip(coords, points, strict=True):
        parts.append(f'<g class="mark" tabindex="0" data-tip="{pct(value)}\n{esc(label)} · {esc(unit_label)}">'
                     f'<rect class="hit" x="{x - 12:.1f}" y="{y - 12:.1f}" width="24" height="24"/>'
                     f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--series-1)" stroke="var(--surface)" '
                     f'stroke-width="2"/></g>')
    last_x, last_y = coords[-1]
    parts.append(f'<text class="val" x="{last_x + 8:.1f}" y="{last_y + 4:.1f}">{pct(points[-1][1], 1)}</text>')
    for i in (0, len(points) - 1):
        parts.append(f'<text class="tick" x="{coords[i][0]:.1f}" y="{height - 6}" text-anchor="middle">'
                     f'{esc(points[i][0])}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------- 数据 ----------

def _summary(name: str) -> dict[str, Any]:
    return json.loads((RESULTS / name / "summary.json").read_text(encoding="utf-8"))


def pooled(summary: dict[str, Any], conditions: tuple[str, ...] = ("agent", "agent_skill"),
           key: str = "passed") -> tuple[int, int]:
    """几个对比组合计：通过次数、运行次数。"""
    rows = [summary["by_condition"][c] for c in conditions if c in summary["by_condition"]]
    return sum(r[key] for r in rows), sum(r["runs"] for r in rows)


def _ratio_text(passed: int, runs: int) -> str:
    return f"{passed / runs:.0%}（{passed}/{runs}）"


def weekly_rows() -> list[dict[str, Any]]:
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in WEEKLY.glob("runs/*/summary.json")]
    return sorted(rows, key=lambda r: r["week"])


def _real_runs() -> list[tuple[Path, dict[str, Any]]]:
    runs = []
    for path in sorted(REAL_RUNS.glob("*/run.json")):
        runs.append((path.parent, json.loads(path.read_text(encoding="utf-8"))))
    return runs


def _mermaid_from_readme() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"## 架构\s+```mermaid\n(.*?)```", readme, re.S)
    return match.group(1) if match else ""


# ---------- 页面 ----------

def index_page() -> str:
    m6, after = _summary("full-2026-09-29"), _summary("m7b-after-2026-10-07")
    before_h, after_h = _summary("holdout-before-2026-10-07"), _summary("holdout-after-2026-10-07")
    company = _summary("company-2026-10-07")
    weeks = weekly_rows()
    tiles = [
        ("原题通过率（Agent 两组合计）", f"{pooled(m6)[0] / pooled(m6)[1]:.0%} → {pooled(after)[0] / pooled(after)[1]:.0%}",
         f"{pooled(m6)[0]}/{pooled(m6)[1]} → {pooled(after)[0]}/{pooled(after)[1]}；只看初稿 "
         f"{pooled(after, key='passed_without_repair')[0]}/{pooled(after)[1]}"),
        ("留出题通过率（改代码前冻结的 5 道新题）",
         f"{pooled(before_h)[0] / pooled(before_h)[1]:.0%} → {pooled(after_h)[0] / pooled(after_h)[1]:.0%}",
         f"{pooled(before_h)[0]}/{pooled(before_h)[1]} → {pooled(after_h)[0]}/{pooled(after_h)[1]}"),
        ("财报评测：Agent 对直接问模型",
         f"{pooled(company, ('agent_skill',))[0]}/{pooled(company, ('agent_skill',))[1]} 对 "
         f"{pooled(company, ('baseline',))[0]}/{pooled(company, ('baseline',))[1]}",
         "评分规则按同一批答案校准过，偏乐观"),
        ("自动周报", f"{len(weeks)} 份", f"每周一由 GitHub Actions 生成，最新一份 {weeks[-1]['week'] if weeks else '—'}"),
    ]
    tile_html = "".join(f'<div class="tile"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>'
                        f'<div class="note">{esc(note)}</div></div>' for label, value, note in tiles)
    body = f"""
<h1>运营周报与指标异动分析 Agent</h1>
<p class="lead">给一份运营数据和一个业务问题（如“上周 GMV 为什么下降？”），模型负责选择分析工具，Python 负责计算每一个数字，
报告里的数字都能回查到工具输出。同一个 Agent 还接入了上市公司财报分析。</p>
<div class="tiles">{tile_html}</div>
<p class="small muted">提升主要来自“核查后退回修正”，它和评分用的是同一个核查器，所以“数字有出处”这一项的提升有一部分是构造出来的；
详见<a href="evals.html">评测</a>。人工复核由 Claude 辅助完成，待项目作者确认。</p>
<h2>架构</h2>
<div class="card"><pre class="mermaid">{esc(_mermaid_from_readme())}</pre></div>
<ul>
<li><b>模型只做判断，不做计算</b>：模型决定调用哪个工具、怎么解读；数字全部来自经过测试的 SQL 和 Python 函数，模型不能执行自己写的代码。</li>
<li><b>每个数字都能追溯</b>：程序逐个检查答案里的数字能否在工具输出里找到；找不到就退回模型修正一次，仍找不到的在报告里列出。</li>
<li><b>结论要人来定</b>：报告标注“AI 初稿，待人工复核”，并列出因果表述、警告和工具调用记录。</li>
</ul>
<h2>看什么</h2>
<ul>
<li><a href="weekly/index.html">自动周报</a>：用公开的历史交易数据按周回放，每周一自动生成一份。</li>
<li><a href="evals.html">评测</a>：直接问模型和 Agent 的对比、对症改进前后的对比、留出题。</li>
<li><a href="company.html">财报分析</a>：阿里巴巴、京东、拼多多、亚马逊的关键比率（SEC 公开年报）。</li>
<li><a href="runs/index.html">运行回放</a>：真实运行时 Agent 每一步调用了什么工具、给出什么答案、核查结果和人工复核。</li>
</ul>
<p>源代码、测试和每次运行的完整记录：<a href="{REPO_URL}">{REPO_URL}</a></p>
"""
    mermaid_js = ('<script type="module">import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";'
                  'mermaid.initialize({startOnLoad: true, theme: matchMedia("(prefers-color-scheme: dark)").matches '
                  '? "dark" : "default"});</script>')
    return page("首页", body, 0, "index.html", mermaid_js)


def weekly_index_page() -> str:
    rows = weekly_rows()
    body = ['<h1>自动周报</h1>',
            '<p class="lead">GitHub Actions 每周一用 UCI Online Retail II 的历史数据回放一周，生成一份周报：第一部分由程序计算，'
            '第二部分是 AI 初稿，附自动核查。这是历史数据的模拟上线，不是实时业务数据。</p>']
    changes = [r for r in rows if r["gmv_change_pct"] is not None]
    if changes:
        body.append('<div class="card"><h3>GMV 环比</h3>')
        body.append(change_columns([r["week"] for r in changes], [r["gmv_change_pct"] for r in changes],
                                   [f"{pct(r['gmv_change_pct'])}\n{r['week']} 对 {r['base']}" for r in changes],
                                   "每周 GMV 环比"))
        body.append("</div>")
    body.append('<div class="table-wrap"><table><thead><tr><th>周</th><th class="num">GMV 环比</th><th>AI 解读</th>'
                '<th class="num">数字有出处</th><th>模型</th></tr></thead><tbody>')
    for r in reversed(rows):
        ai = "完成" if r["ai_status"] == "completed" else f"未完成（{r['ai_status']}）"
        body.append(f'<tr><td><a href="{esc(r["week"])}.html">{esc(r["week"])}</a></td><td class="num">'
                    f'{pct(r["gmv_change_pct"])}</td><td>{esc(ai)}</td><td class="num">{r["numbers_grounded"]}/'
                    f'{r["numbers_checked"]}</td><td class="muted">{esc(r["model"])}</td></tr>')
    body.append("</tbody></table></div>")
    return page("自动周报", "\n".join(body), 1, "weekly/index.html")


def weekly_report_page(week: str) -> str:
    text = (WEEKLY / f"{week}.md").read_text(encoding="utf-8")
    body = (f'<p class="small"><a href="index.html">← 全部周报</a> · <a href="{BLOB}reports/weekly/{esc(week)}.md">'
            f'在 GitHub 上查看原文</a></p><div class="prose">{render_markdown(text, f"{BLOB}reports/weekly/")}</div>')
    return page(f"周报 {week}", body, 1, "weekly/index.html")


def _condition_table(summary: dict[str, Any]) -> str:
    rows = ['<div class="table-wrap"><table><thead><tr><th>组别</th><th class="num">通过</th><th class="num">数字有出处率</th>'
            '<th class="num">平均 token</th><th class="num">平均用时（秒）</th></tr></thead><tbody>']
    for stats in summary["by_condition"].values():
        draft = ""
        if stats.get("repaired"):
            draft = f'<span class="muted">（初稿 {stats["passed_without_repair"]}/{stats["runs"]}）</span>'
        rows.append(f'<tr><td>{esc(stats["label"])}</td><td class="num">{stats["passed"]}/{stats["runs"]} {draft}</td>'
                    f'<td class="num">{pct(stats["grounded_rate"], 0)}</td><td class="num">{stats["avg_tokens"]:,.0f}</td>'
                    f'<td class="num">{stats["avg_seconds"]:.1f}</td></tr>')
    rows.append("</tbody></table></div>")
    return "".join(rows)


def evals_page() -> str:
    m6, after = _summary("full-2026-09-29"), _summary("m7b-after-2026-10-07")
    before_h, after_h = _summary("holdout-before-2026-10-07"), _summary("holdout-after-2026-10-07")
    company = _summary("company-2026-10-07")

    def rate(pair: tuple[int, int], name: str) -> tuple[float, str]:
        return pair[0] / pair[1], f"{pair[0] / pair[1]:.0%}（{pair[0]}/{pair[1]}）\n{name}"

    improve = grouped_columns(
        ["原题（10 道）", "留出题（5 道）"],
        [("改进前", "--series-2", [rate(pooled(m6), "原题 · 改进前（M6）"), rate(pooled(before_h), "留出题 · 改进前")]),
         ("改进后", "--series-1", [rate(pooled(after), "原题 · 改进后（M7b）"), rate(pooled(after_h), "留出题 · 改进后")])],
        "Agent 两组合计通过率：改进前后")
    versus = grouped_columns(
        ["电商（M6，10 道）", "财报（M9，8 道）"],
        [("直接问模型", "--series-2", [rate(pooled(m6, ("baseline",)), "电商 · 直接问模型"),
                                    rate(pooled(company, ("baseline",)), "财报 · 直接问模型")]),
         ("Agent", "--series-1", [rate(pooled(m6), "电商 · Agent 两组合计（改进前）"),
                                  rate(pooled(company, ("agent_skill",)), "财报 · Agent 带流程")])],
        "直接问模型对 Agent 的通过率")
    links = lambda name: f'<a href="{BLOB}eval/results/{name}/summary.md">完整结果</a>'  # noqa: E731
    body = f"""
<h1>评测</h1>
<p class="lead">同一批题目，几种做法各跑一遍，由程序按写好的规则打分：必须提到的要点、不能出现的说法、正确数值、
每个数字是否有出处。每次运行的完整记录都提交在仓库里。</p>
<div class="card"><h3>对症改进前后（M7b）</h3>
<p class="small muted">改进：计算工具、核查后退回修正、日均指标。留出题在改代码之前冻结并先跑出改进前成绩，用来检验改进能不能推广。</p>
{improve}</div>
<div class="card"><h3>直接问模型对 Agent</h3>
<p class="small muted">直接问模型：只给一张数据表、没有工具。它失分主要是自己算的数字没有出处；财报题里它算出的 ROE 是对的。</p>
{versus}</div>
<h2>M6：电商评测（10 道题）</h2>{_condition_table(m6)}<p class="small">{links("full-2026-09-29")}</p>
<h2>M7b：改进后（原题）</h2>{_condition_table(after)}<p class="small">{links("m7b-after-2026-10-07")}</p>
<h2>M7b：留出题</h2>
<h3>改进前</h3>{_condition_table(before_h)}<h3>改进后</h3>{_condition_table(after_h)}
<p class="small">{links("holdout-before-2026-10-07")} · {links("holdout-after-2026-10-07")} ·
<a href="{BLOB}eval/results/m7b-comparison.md">前后对比和解读</a></p>
<h2>M9：财报评测（8 道题）</h2>{_condition_table(company)}<p class="small">{links("company-2026-10-07")}</p>
<h2>说明与局限</h2>
<ul>
<li>提升主要来自“核查后退回修正”，它和评分用的是同一个核查器，所以“数字有出处”这一项的提升有一部分是构造出来的。括号里的“初稿”是不靠退回修正的成绩。</li>
<li>评分器本身先后发现并修正了 15 处缺陷，每次修改都对前后结果同时重评，原结果保留。</li>
<li>题目少、每题只跑 1–2 次、单一模型（MiniMax-M3.1-Flash-Preview）；关键词评分是近似的。</li>
</ul>
"""
    return page("评测", body, 0, "evals.html")


def company_page() -> str:
    with connect_financials() as con:
        overview = company_overview(con)["companies"]
        series: dict[str, list[tuple[int, dict[str, float | None]]]] = {}
        for info in overview:
            first, last = info["fiscal_years"]
            rows = []
            for year in range(first, last + 1):
                try:
                    summary = financial_summary(con, info["company"], year)
                except ValueError:
                    continue
                rows.append((year, {r["ratio"]: r["current"] for r in summary["ratios"]}))
            series[info["company"]] = rows
    # 四张图共用纵轴和起始财年，才能横向比较。更早的年份里有极端值（如上市初期亏损、权益很小），
    # 放进图里会把其他公司压成直线：图从 CHART_FROM 开始，被截掉的年份写在图下方，完整数字在表格里
    shown = {c: [(y, v["roe"]) for y, v in rows if y >= CHART_FROM and v["roe"] is not None]
             for c, rows in series.items()}
    values = [roe for points in shown.values() for _, roe in points]
    high = (int(max(values) * 10) + 1) / 10
    low = -((int(-min(values) * 10) + 1) / 10) if min(values) < 0 else 0.0
    charts = []
    for info in overview:
        points = [(f"FY{y}", roe) for y, roe in shown[info["company"]]]
        short = info["name"].split("（")[0]  # 卡片里用短名，标题不换行，四张图才能对齐
        charts.append(f'<div class="card"><h3>{esc(short)} · {esc(info["ticker"])}</h3>'
                      f'{small_line(info["name"] + " ROE", points, low, high, "ROE")}</div>')
    earlier = [f"{info['name']} FY{y} {pct(v['roe'], 1)}" for info in overview for y, v in series[info["company"]]
               if y < CHART_FROM and v["roe"] is not None and not low <= v["roe"] <= high]
    chart_note = (f"图从 FY{CHART_FROM} 开始，四家共用同一纵轴。更早年份里超出这个范围的值："
                  f"{'、'.join(earlier) or '无'}；完整数字见下方表格。")
    tables = []
    for info in overview:
        head = "".join(f'<th class="num">{esc(RATIOS[k][0])}</th>' for k in COMPANY_RATIOS)
        body_rows = "".join(
            f'<tr><td>FY{y}</td>' + "".join(f'<td class="num">{pct(v[k], 1)}' if k != "ocf_to_net_income"
                                            else f'<td class="num">{"—" if v[k] is None else f"{v[k]:.2f}"}'
                                            for k in COMPANY_RATIOS) + "</tr>"
            for y, v in reversed(series[info["company"]]))
        currency = {"CNY": "人民币", "USD": "美元"}.get(info["currency"], info["currency"])
        missing = "、".join(info["not_disclosed_in_latest_year"]) or "无"
        tables.append(f'<h2>{esc(info["name"])}（{esc(info["ticker"])}）</h2>'
                      f'<p class="small muted">财年截至 {esc(info["fiscal_year_end"])}，{currency}；最近一个财年没有标准数据的科目：'
                      f'{esc(missing)}（标为未披露，不当成 0）</p>'
                      f'<div class="table-wrap"><table><thead><tr><th>财年</th>{head}</tr></thead><tbody>{body_rows}'
                      f'</tbody></table></div>')
    definitions = "".join(f"<li><b>{esc(RATIOS[k][0])}</b>：{esc(RATIOS[k][1])}</li>" for k in COMPANY_RATIOS)
    body = f"""
<h1>上市公司财报分析</h1>
<p class="lead">数据来自 SEC EDGAR 的公开年报（标准分类科目），比率由分析工具在生成网页时现算。各公司币种和财年截止日不同，
所以跨公司只比比率，不比金额。只做财务分析，不做估值、评级，不构成投资建议。</p>
<h2>ROE</h2>
<div class="multiples">{"".join(charts)}</div>
<p class="small muted">{esc(chart_note)}</p>
{"".join(tables)}
<h2>比率定义</h2><ul class="small">{definitions}</ul>
<p class="small">关键科目表和每个数字的出处：<a href="{BLOB}data/financials/README.md">data/financials</a></p>
"""
    return page("财报分析", body, 0, "company.html")


def runs_index_page(runs: list[tuple[Path, dict[str, Any]]]) -> str:
    rows = []
    for folder, run in runs:
        checks = json.loads((folder / "checks.json").read_text(encoding="utf-8"))
        domain = "财报" if run.get("domain") == "company" else "电商"
        rows.append(f'<tr><td><a href="{esc(folder.name)}.html">{esc(run["question"])}</a></td><td>{domain}</td>'
                    f'<td class="num">{checks["numbers"]["grounded"]}/{checks["numbers"]["checked"]}</td>'
                    f'<td class="num">{len(run["steps"])}</td><td class="muted">{esc(run["started_at"][:10])}</td></tr>')
    body = ('<h1>运行回放</h1><p class="lead">真实模型（MiniMax-M3.1-Flash-Preview）回答问题的完整过程：每一步调用了什么工具、'
            '最终答案、自动核查结果，以及人工复核发现的问题。</p><div class="table-wrap"><table><thead><tr><th>问题</th>'
            '<th>领域</th><th class="num">数字有出处</th><th class="num">工具调用</th><th>日期</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div>")
    return page("运行回放", body, 1, "runs/index.html")


def run_page(folder: Path, run: dict[str, Any]) -> str:
    checks = json.loads((folder / "checks.json").read_text(encoding="utf-8"))
    base = f"{BLOB}examples/real_runs/{folder.name}/"
    steps = "".join(
        f'<tr><td class="num">{s["round"]}</td><td>{esc(s["tool"])}</td>'
        f'<td>{esc(json.dumps(s["arguments"], ensure_ascii=False))}</td>'
        f'<td>{"成功" if s["ok"] else "失败：" + esc(s["error"])}</td></tr>' for s in run["steps"])
    usage = run["usage"]
    repair = run.get("repair")
    repair_note = ""
    if repair:
        repair_note = (f'<p class="small">初稿有 {len(repair["ungrounded"])} 个数字找不到出处，已退回模型修正 1 次'
                       f'{"（修正失败，保留初稿）" if repair.get("error") else ""}。</p>')
    review = folder / "review.md"
    review_html = (f'<h2>人工复核</h2><div class="prose card">{render_markdown(review.read_text(encoding="utf-8"), base)}</div>'
                   if review.exists() else "")
    body = f"""
<p class="small"><a href="index.html">← 全部运行</a> · <a href="{base}run.json">完整运行记录（JSON）</a></p>
<h1>{esc(run["question"])}</h1>
<p><span class="badge">{"财报" if run.get("domain") == "company" else "电商"}</span>
<span class="badge">{esc(run["model"])}</span><span class="badge">状态 {esc(run["status"])}</span>
<span class="badge">调用模型 {usage["llm_calls"]} 次</span><span class="badge">{run["seconds"]} 秒</span></p>
<div class="card small">自动核查：{esc(checks["summary"])}</div>
{repair_note}
<h2>每一步</h2>
<div class="table-wrap"><table class="steps"><thead><tr><th class="num">轮</th><th>工具</th><th>参数</th><th>结果</th></tr></thead>
<tbody>{steps}</tbody></table></div>
<h2>答案（AI 初稿）</h2>
<div class="prose card">{render_markdown(run["answer"] or "（没有答案）", base)}</div>
{review_html}
"""
    return page(run["question"], body, 1, "runs/index.html")


def build_site(out_dir: Path) -> list[Path]:
    """生成整个网站，返回写出的文件。"""
    files: dict[str, str] = {"index.html": index_page(), "evals.html": evals_page(), "company.html": company_page(),
                             "weekly/index.html": weekly_index_page()}
    for row in weekly_rows():
        files[f"weekly/{row['week']}.html"] = weekly_report_page(row["week"])
    runs = _real_runs()
    files["runs/index.html"] = runs_index_page(runs)
    for folder, run in runs:
        files[f"runs/{folder.name}.html"] = run_page(folder, run)
    written = []
    for name, content in files.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")  # 告诉 GitHub Pages 原样发布，不用 Jekyll 处理
    return written
