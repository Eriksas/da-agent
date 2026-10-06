"""自动周报：游标推进、两部分报告、AI 失败时的降级、停业周、CLI 保护。全部用内置小样例和假模型。"""

import json
from pathlib import Path

import pytest

from da_agent import cli
from da_agent.cleaning import connect_frame
from da_agent.dataset import read_normalized_csv
from da_agent.llm import FakeLLM, LLMError, LLMReply, ToolCall
from da_agent.weekly import (
    FIRST_REPLAY_WEEK, demote_headings, generate_weekly, next_replay_week, read_cursor, write_cursor,
)
from helpers import S2, UK, make_frame
from test_metrics import ROWS


@pytest.fixture
def con():
    connection = connect_frame(read_normalized_csv(cli.DEMO_DATA))  # 两周小样例：2011-W01、2011-W02
    yield connection
    connection.close()


def test_cursor_starts_at_first_week_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "state" / "cursor.json"
    assert read_cursor(path) == FIRST_REPLAY_WEEK
    write_cursor("2010-W03", path)
    assert read_cursor(path) == "2010-W03"
    write_cursor(None, path)
    assert read_cursor(path) is None
    assert "回放已结束" in path.read_text(encoding="utf-8")


def test_next_week_stops_at_end_of_data(con) -> None:
    assert next_replay_week(con, "2011-W01") == "2011-W02"
    assert next_replay_week(con, "2011-W02") is None


def test_report_has_program_part_ai_part_and_index(con, tmp_path: Path) -> None:
    llm = FakeLLM(replies=[
        LLMReply(content="", tool_calls=(ToolCall(id="m", name="metric_summary", arguments={"week": "2011-W02"}),)),
        LLMReply(content="GMV 为 145.0，比上一周增加 5.0。"),
    ])
    result = generate_weekly(con, "2011-W02", llm, max_tool_calls=5, out_dir=tmp_path)
    report = (tmp_path / "2011-W02.md").read_text(encoding="utf-8")
    assert "## 一、核心指标（程序计算）" in report
    assert "| GMV | 145.00 | 140.00 | +5.00（+3.57%） |" in report  # 程序计算的部分，不经过模型
    assert "## 二、AI 解读（初稿，待人工复核）" in report and "GMV 为 145.0" in report
    assert "数字核查：2/2 个能在工具输出中找到出处" in report
    assert result["ai_status"] == "completed" and result["gmv_change_pct"] == pytest.approx(0.0357)
    assert json.loads((tmp_path / "runs" / "2011-W02" / "run.json").read_text(encoding="utf-8"))["status"] == "completed"
    index = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "| [2011-W02](2011-W02.md) | +3.57% | 完成 | 2/2 |" in index


def test_model_failure_still_publishes_program_part(con, tmp_path: Path) -> None:
    class Down(FakeLLM):
        def chat(self, messages, tools=None):  # type: ignore[override]
            raise LLMError("APITimeoutError: 超时")

    result = generate_weekly(con, "2011-W02", Down(replies=[]), max_tool_calls=5, out_dir=tmp_path)
    report = (tmp_path / "2011-W02.md").read_text(encoding="utf-8")
    assert "| 订单数 | 5 | 4 |" in report
    assert "AI 解读未完成：APITimeoutError: 超时" in report
    assert result["ai_status"] == "llm_error"
    assert "未完成（llm_error）" in (tmp_path / "README.md").read_text(encoding="utf-8")


def test_closed_base_week_is_explained_not_crashed(tmp_path: Path) -> None:
    rows = [ROWS[0], (S2, 3, "300011", "10001", "A", 1, "2011-01-17 10:00", 1.0, "c1", UK)]  # W02 整周停业
    with connect_frame(make_frame(rows)) as con:
        generate_weekly(con, "2011-W03", FakeLLM(replies=[LLMReply(content="好")]), max_tool_calls=5, out_dir=tmp_path)
    report = (tmp_path / "2011-W03.md").read_text(encoding="utf-8")
    assert "无法拆解：基期 2011-W02 没有可识别客户" in report
    assert "基期 2011-W02 整周没有交易" in report


def test_fake_model_cannot_advance_cursor(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["weekly", "--llm", "fake", "--advance"]) == 2
    assert "不能推进回放游标" in capsys.readouterr().out


def test_program_part_has_per_day_rows(con, tmp_path: Path) -> None:
    generate_weekly(con, "2011-W02", FakeLLM(replies=[LLMReply(content="好")]), max_tool_calls=5, out_dir=tmp_path)
    report = (tmp_path / "2011-W02.md").read_text(encoding="utf-8")
    assert "| 日均 GMV | 29.00 | 35.00 | -6.00（-17.14%） |" in report  # 5 天对 4 天：总量 +3.57%，日均 −17.14%
    assert "交易天数不同：当期 2011-W02 5 天，基期 2011-W01 4 天" in report


def test_repair_note_and_demoted_headings(con, tmp_path: Path) -> None:
    llm = FakeLLM(replies=[
        LLMReply(content="", tool_calls=(ToolCall(id="m", name="metric_summary", arguments={"week": "2011-W02"}),)),
        LLMReply(content="# 周报\n\nGMV 约 1,234.56。"),
        LLMReply(content="# 周报\n\n## 结论\n\nGMV 为 145.0。"),
    ])
    generate_weekly(con, "2011-W02", llm, max_tool_calls=5, out_dir=tmp_path)
    report = (tmp_path / "2011-W02.md").read_text(encoding="utf-8")
    assert "> 核查后退回修正 1 次：初稿有 1 个数字找不到出处（1,234.56），下面是修正后的版本。" in report
    assert "\n### 周报\n" in report and "\n#### 结论\n" in report  # AI 的标题降两级，放在“二、AI 解读”下面
    assert "\n# 周报\n" not in report


def test_demote_headings_leaves_other_lines_alone() -> None:
    assert demote_headings("# A\n#不是标题\n正文 # 号\n###### F") == "### A\n#不是标题\n正文 # 号\n###### F"
