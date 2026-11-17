"""图形界面的逻辑部分（不需要安装 Streamlit）：回放和实时的事件一致、答案的转义和标红、可回放的运行列表。"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from test_agent import QUESTION, call, tools
from test_metrics import ROWS

from da_agent import dataset, ui
from da_agent.agent import BUDGET_NOTICE, FORMAT_NOTICE, run_agent, save_run
from da_agent.checks import check_run, extract_numbers
from da_agent.cleaning import connect_frame
from da_agent.config import Settings
from da_agent.llm import FakeLLM, LLMReply
from da_agent.site import REAL_RUNS
from helpers import make_frame


def test_replayed_events_equal_live_events(tmp_path: Path) -> None:
    """回放从保存的 run.json 还原事件，必须和实时运行发出的逐条相同：两边用同一套显示代码。

    剧本覆盖全部四种事件：次数用完的提示、写成文字的工具调用、找不到出处被退回修正。
    """
    live: list[dict] = []
    replies = [
        tools(call(1, "metric_summary", week="2011-W02"), call(2, "decompose_gmv", week="2011-W02")),  # 上限 1 次
        LLMReply(content="<tool_call>metric_summary</tool_call>"),
        LLMReply(content="GMV 为 145.0，约合 1,234.56。"),
        LLMReply(content="GMV 为 145.0。"),
    ]
    with connect_frame(make_frame(ROWS)) as con:
        run = run_agent(QUESTION, llm=FakeLLM(replies=replies), con=con, max_tool_calls=1, on_step=live.append)
    saved = json.loads((save_run(run, tmp_path) / "run.json").read_text(encoding="utf-8"))
    assert run.status == "completed"
    assert [e["type"] for e in live] == ["model", "tool", "tool", "notice", "model", "notice", "model", "repair", "model"]
    assert [e["text"] for e in live if e["type"] == "notice"] == [BUDGET_NOTICE, FORMAT_NOTICE]
    assert ui.replay_events(saved) == live


def test_errors_raised_by_the_callback_stop_the_run() -> None:
    """界面上点“停止”时，Streamlit 在回调里抛异常中断运行；循环不能把它吞掉。"""
    class Stop(Exception):
        pass

    def stop(event: dict) -> None:
        if event["type"] == "tool":
            raise Stop

    with connect_frame(make_frame(ROWS)) as con, pytest.raises(Stop):
        run_agent(QUESTION, llm=FakeLLM(replies=[tools(call(1, "metric_summary", week="2011-W02"))]), con=con,
                  max_tool_calls=5, on_step=stop)


class Box:
    """假的进度框：记录每次调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        return lambda *args, **kwargs: self.calls.append((name, args, kwargs))


def test_progress_box_stays_open_when_its_label_changes() -> None:
    """Streamlit 1.65 改标题时不传 expanded 会把进度框收起来，后面的步骤都看不见（第一次真实运行时发现）。"""
    box = Box()
    ui.show_event(box, {"type": "model", "round": 2}, "分析中")
    assert box.calls[0] == ("update", (), {"label": "分析中：第 2 轮，等模型决定下一步", "expanded": True})
    quiet = Box()
    ui.show_event(quiet, {"type": "model", "round": 2})  # 一次性画出全部步骤时不改标题
    assert [name for name, _, _ in quiet.calls] == ["caption"]


def test_dollar_and_tilde_are_escaped_outside_code() -> None:
    """Streamlit 把 $…$ 当公式、~ 当删除线；代码块里的原样保留。"""
    text = "区间 3~5 天，约 $12 到 $15。代码 `a~b $x` 不动\n```\n$y ~z\n```"
    out = ui.to_streamlit_markdown(text)
    assert r"3\~5" in out and r"\$12 到 \$15" in out
    assert "`a~b $x`" in out and "```\n$y ~z\n```" in out


def test_quotes_in_tables_are_shown_as_plain_text() -> None:
    """表格里的引文是截出来的半句话：开头的“- ”不能变成列表，截断的 ** 和反引号要原样显示（真实运行时出现过）。"""
    assert ui.plain_text("- 注意：**半句\n`metric…") == r"\- 注意：\*\*半句 \`metric…"
    assert ui.plain_text(":red[x] $1~2") == r"\:red\[x\] \$1\~2"


def test_headings_are_demoted_two_levels_outside_code() -> None:
    out = ui.to_streamlit_markdown("# 标题\n## 小节\n正文 #1\n```\n# 注释\n```")
    assert out == "### 标题\n#### 小节\n正文 #1\n```\n# 注释\n```"


def test_only_the_flagged_occurrence_is_highlighted() -> None:
    """按核查器给的位置标红：234.5 也是 1,234.5 的一部分，按字符串替换会标错地方。"""
    answer = "GMV 1,234.5，其中 234.5 来自英国"
    flagged = [asdict(m) for m in extract_numbers(answer)[0] if m.text == "234.5"]
    assert ui.to_streamlit_markdown(answer, flagged) == "GMV 1,234.5，其中 :red-background[234.5] 来自英国"


def test_positions_that_do_not_match_or_sit_in_code_are_not_highlighted() -> None:
    answer = "数字 `12.5` 和 99.9"
    stale = {"text": "99.9", "start": 0, "end": 0}  # 旧的核查结果没有位置
    in_code = {"text": "12.5", "start": 4, "end": 8}
    assert ui.to_streamlit_markdown(answer, [stale, in_code]) == answer


def write_run(folder: Path, run_id: str, question: str) -> None:
    folder.mkdir(parents=True)
    (folder / "run.json").write_text(json.dumps({"run_id": run_id, "question": question, "messages": []}),
                                     encoding="utf-8")


def test_saved_runs_lists_examples_first_then_local_without_duplicates(tmp_path: Path) -> None:
    examples, local = tmp_path / "examples", tmp_path / "runs"
    write_run(examples / "a", "a", "样例问题")
    write_run(local / "a", "a", "样例问题")  # 本机也留着一份同一次运行
    write_run(local / "b", "b", "旧问题")
    write_run(local / "c", "c", "新问题")
    (local / "weekly-fake").mkdir()  # 不是运行记录的目录
    (local / "broken").mkdir()
    (local / "broken" / "run.json").write_text("{", encoding="utf-8")
    found = ui.saved_runs(examples, local)
    assert [(r.source, r.run["run_id"]) for r in found] == [("样例", "a"), ("本机", "c"), ("本机", "b")]


@pytest.mark.parametrize("folder", sorted(p.parent.name for p in REAL_RUNS.glob("*/run.json")))
def test_ui_checks_match_committed_checks(folder: str) -> None:
    """界面上的核查结果是用当前规则现算的，必须和网站展示的、已提交的 checks.json 一致。"""
    run = json.loads((REAL_RUNS / folder / "run.json").read_text(encoding="utf-8"))
    committed = json.loads((REAL_RUNS / folder / "checks.json").read_text(encoding="utf-8"))
    fresh = check_run(run)
    assert fresh["summary"] == committed["summary"]
    assert [m["text"] for m in fresh["numbers"]["ungrounded"]] == [m["text"] for m in committed["numbers"]["ungrounded"]]


def test_live_problem_explains_what_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert "LLM_API_KEY" in ui.live_problem("company", Settings(_env_file=None))
    with_key = Settings(_env_file=None, llm_api_key="dummy")
    assert ui.live_problem("company", with_key) is None  # 财报数据已提交，不需要准备
    monkeypatch.setattr(dataset, "PARQUET_PATH", tmp_path / "missing.parquet")
    assert "prepare-data" in ui.live_problem("ecommerce", with_key)


def test_ask_saves_the_run_with_checks_and_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """实时提问和命令行的 company-ask 存下同样的三份文件。用假模型，不联网。"""
    llm = FakeLLM(replies=[tools(call(1, "company_overview")), LLMReply(content="拼多多的财年是自然年。")])
    monkeypatch.setattr(ui, "make_llm", lambda settings: llm)
    monkeypatch.setattr(ui, "RUNS_DIR", tmp_path)
    events: list[dict] = []
    run_dir = ui.ask("拼多多的财年是怎么算的？", "company", Settings(_env_file=None), on_step=events.append)
    assert run_dir.parent == tmp_path
    assert {p.name for p in run_dir.iterdir()} >= {"run.json", "answer.md", "checks.json", "report.md"}
    assert "AI 初稿，待人工复核" in (run_dir / "report.md").read_text(encoding="utf-8")
    assert [e["type"] for e in events] == ["model", "tool", "model"]
    assert ui.replay_events(ui.load_run(run_dir).run) == events


def test_ask_can_hand_data_pulls_to_the_query_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """界面里打开“允许取数”：和命令行 ask --with-query 一样，查询 Agent 的完整记录附在 sub_runs 里。"""
    from test_metrics import ROWS

    from da_agent import semantic
    from da_agent.cleaning import connect_frame
    from da_agent.metrics import _ensure
    from da_agent.semantic import SEMANTIC_DIR, build_database, load_config
    from helpers import make_frame

    base = connect_frame(make_frame(ROWS))
    _ensure(base)
    db = build_database(load_config(SEMANTIC_DIR / "retail.yaml"), base, tmp_path / "q.duckdb")
    monkeypatch.setattr(semantic, "prepare", lambda name: db)
    import da_agent.cleaning as cleaning

    monkeypatch.setattr(cleaning, "connect", lambda path: base)
    submit = {"sql": "SELECT round(sum(gmv_amount), 2) AS gmv FROM order_lines WHERE week = '2011-W02'", "columns": {"gmv": "GMV"},
              "definition": "2011-W02 的 GMV"}
    llm = FakeLLM(replies=[tools(call(1, "query_data", request="2011-W02 的 GMV")),
                           tools(call(2, "submit_query", **submit)), LLMReply(content="完成。"),
                           LLMReply(content="2011-W02 的 GMV 为 145.00。")])
    monkeypatch.setattr(ui, "make_llm", lambda settings: llm)
    monkeypatch.setattr(ui, "RUNS_DIR", tmp_path)
    run_dir = ui.ask("2011-W02 的 GMV？", "ecommerce", Settings(_env_file=None), with_query=True)
    saved = ui.load_run(run_dir).run
    assert saved["steps"][0]["tool"] == "query_data" and saved["steps"][0]["ok"] and len(saved["sub_runs"]) == 1
    assert saved["status"] == "completed" and "145.00" in saved["answer"]


def test_query_results_show_the_sql_and_the_first_rows() -> None:
    result = {"status": "answered", "sql": "SELECT country, sum(gmv_amount) AS gmv FROM order_lines GROUP BY 1",
              "columns": [{"name": "country", "meaning": "国家"}, {"name": "gmv", "meaning": "GMV"}],
              "rows": [{"country": f"C{i}", "gmv": i * 1.5} for i in range(10)] + [{"country": "A|B", "gmv": None}], "row_count": 11}
    text = ui.query_preview(result)
    assert "```sql\nSELECT country" in text and "| country | gmv |" in text and "| C0 | 0.0 |" in text
    assert "| C7 | 10.5 |" in text and "C8" not in text and "共 11 行，这里显示前 8 行。" in text
    assert "没有给出结果" in ui.query_preview({"status": "declined"})
    box = Box()
    ui.show_event(box, {"type": "tool", "step": {"tool": "query_data", "ok": True, "arguments": {"request": "各国 GMV"}, "ms": 5},
                        "result": result})
    texts = [args[0] for name, args, _ in box.calls if name == "markdown"]
    assert "取数（查询 Agent）（query_data）" in texts[0] and "```sql" in texts[1]
