"""图形界面的页面（Streamlit AppTest：不开浏览器，直接运行页面脚本并检查画出来的元素）。

没装界面依赖（uv sync --group ui）时整个文件跳过。实时提问用假模型，不联网、不花钱。
"""

from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="需要可选依赖：uv sync --group ui")
from streamlit.testing.v1 import AppTest  # noqa: E402

from da_agent import ui  # noqa: E402
from da_agent.llm import FakeLLM, LLMReply  # noqa: E402
from test_agent import call, tools  # noqa: E402

APP = str(Path(ui.__file__).with_name("ui_app.py"))


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AppTest:
    monkeypatch.setattr(ui, "RUNS_DIR", tmp_path)  # 本机运行记录换成空目录：只列已提交的样例，新运行也存到这里
    monkeypatch.setattr(ui, "REPLAY_DELAY", 0)
    return AppTest.from_file(APP, default_timeout=60)


def button(at: AppTest, label: str):  # noqa: ANN201
    return next(b for b in at.button if b.label == label)


def test_replay_shows_the_first_example_with_checks(app: AppTest) -> None:
    at = app.run()
    assert not at.exception
    assert at.subheader[0].value == "上周 GMV 为什么变化？"
    assert "AI 初稿，待人工复核" in at.info[0].value and "85/85" in at.info[0].value
    assert [t.label for t in at.tabs] == ["报告", "每一步", "核查", "人工复核"]  # 样例带人工复核记录


def test_step_by_step_replay(app: AppTest) -> None:
    at = app.run()
    button(at, "▶ 逐步播放").click().run()
    assert not at.exception
    assert at.status[0].label == "回放完成：8 次工具调用 · 调用模型 4 次 · 26.02 秒"
    assert at.status[0].proto.expanded  # 播放完不收起：刚看完的步骤还要能看到
    assert any("指标对比（metric_summary）" in m.value for m in at.markdown)


def test_live_question_runs_saves_and_highlights(app: AppTest, monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    """实时提问：选示例问题、开始分析，结果存到 runs/，找不到出处的数字在答案里标红。"""
    monkeypatch.setenv("LLM_API_KEY", "dummy")  # 只让“开始分析”可点；模型换成了假的，不会用到它
    llm = FakeLLM(replies=[tools(call(1, "company_overview")), LLMReply(content="拼多多收入约 123.45 亿。")])
    monkeypatch.setattr(ui, "make_llm", lambda settings: llm)
    at = app.run()
    at.sidebar.radio(key="mode").set_value("实时提问").run()
    at.radio(key="domain").set_value("company").run()
    question = ui.EXAMPLE_QUESTIONS["company"][0]
    at.pills(key="example_company").set_value(question).run()
    assert at.text_area(key="question").value == question
    button(at, "开始分析").click().run()
    assert not at.exception
    assert len(list(tmp_path.glob("*/report.md"))) == 1
    assert at.subheader[0].value == question
    # 剧本没有准备修正这一轮：修正失败，保留初稿，并说明
    assert any("修正失败" in c.value for c in at.caption)
    assert any(":red-background[123.45 亿]" in m.value for m in at.markdown)
    assert not button(at, "开始分析").disabled  # 运行结束后输入恢复可用
