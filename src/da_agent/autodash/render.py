"""生成独立的看板页面：样式、脚本、数据都内嵌在一个 HTML 文件里，双击就能打开，也可以放到网站上。

页面顶部固定标注“AI 生成的看板草稿，待人工复核”：每个数字由程序算出，但放哪些图、怎么解读是模型提出的。
"""

import html
import json
from typing import Any

from ..site import ASSETS, CSS

EXTRA_CSS = """
/* 分类色第 4、5 个（折线最多 5 条、堆叠最多 4 段时用到），取自同一套经过校验的配色 */
:root{--series-4:#eda100;--series-5:#e87ba4}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){--series-4:#c98500;--series-5:#d55181}}
:root[data-theme="dark"]{--series-4:#c98500;--series-5:#d55181}
.draft{border-left:3px solid var(--series-2);background:var(--surface);padding:10px 14px;border-radius:6px;
font-size:14px;color:var(--text-2);margin:12px 0}
.auto-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
.auto-grid>.card{margin:16px 0 0}
.auto-grid>.span-2{grid-column:1/-1}
@media (max-width:820px){.auto-grid{grid-template-columns:1fr}}
.dash-card .why{color:var(--muted);font-size:13px;margin:8px 0 0}
.dash-card .note div{color:var(--text-2);font-size:13px;margin-top:4px}
.kpis.auto{grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.kpi .sub{color:var(--text-2);font-size:13px;margin-top:4px;font-variant-numeric:tabular-nums}
"""


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def render_dashboard(compiled: dict[str, Any], meta: dict[str, Any]) -> str:
    """meta：run_id、model、generated_at（显示在页头，说明这份看板从哪次运行来）。"""
    payload = json.dumps(compiled, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    source = compiled["source"]
    notes = "".join(f"<li>{_esc(n)}</li>" for n in compiled["notes"]) or "<li>模型没有写额外的数据说明。</li>"
    style = CSS + (ASSETS / "dashboard.css").read_text(encoding="utf-8") + EXTRA_CSS
    scripts = "".join(f"<script>{(ASSETS / name).read_text(encoding='utf-8')}</script>"
                      for name in ("charts.js", "autodash.js"))
    range_text = f"（{_esc(source['range'])}）" if source.get("range") else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(compiled["title"])} · Agent 生成的看板</title>
<style>{style}</style>
</head>
<body>
<header><div class="bar wide"><span class="brand">da-agent</span><span class="muted">Agent 生成的看板</span></div></header>
<main class="wide">
<div class="draft"><b>AI 生成的看板草稿，待人工复核。</b>每个数字都由程序从数据算出；放哪些图、结论怎么写是模型提出的。
每张图下面写了为什么放它，以及程序自动发现的数据问题。</div>
<h1>{_esc(compiled["title"])}</h1>
<p class="lead">{_esc(compiled["goal"])}</p>
<p class="small muted">数据：{_esc(source["label"])}{range_text}。生成于 {_esc(meta.get("generated_at", ""))}
· 模型 {_esc(meta.get("model", ""))} · 运行 {_esc(meta.get("run_id", ""))}</p>
<div class="filters" id="filters" role="group" aria-label="筛选（对整页生效）"></div>
<noscript><p class="card">看板的图表需要 JavaScript。</p></noscript>
<div class="kpis auto" id="kpis"></div>
<div class="auto-grid" id="charts"></div>
<section class="card"><h2>数据说明</h2><ul>{notes}</ul>
<p class="small muted">{_esc(source["description"])}</p></section>
<script id="dashboard-data" type="application/json">{payload}</script>
{scripts}
</main>
</body>
</html>
"""
