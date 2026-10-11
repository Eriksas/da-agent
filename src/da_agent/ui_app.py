"""da-agent 图形界面（Streamlit 页面）。用 `da-agent ui` 启动，只在本机运行；逻辑部分在 ui.py。

两种用法：
- 回放：已保存的运行，不调用模型、不需要密钥，可以逐步播放。演示时网络或模型出问题，也能用它把流程讲完
- 实时提问：调用真实模型，每一步实时显示；结束后和命令行一样把记录、核查结果和报告存到 runs/

所有给人看的数字都来自运行记录和核查器，页面只排版。
"""

import json
import time
from pathlib import Path
from typing import Any

import streamlit as st

from da_agent import ui
from da_agent.checks import check_run
from da_agent.config import Settings
from da_agent.report import render_report

st.set_page_config(page_title="da-agent 分析台", page_icon="📊", layout="wide")
st.session_state.setdefault("running", False)
st.session_state.setdefault("live_dir", None)


def one_line(text: str) -> str:
    """表格里的引文：表格单元格也按 Markdown 显示，换行会变成列表，压成一行。"""
    return ui.to_streamlit_markdown(" ".join(text.split()))


def run_stats(run: dict[str, Any]) -> str:
    usage = run["usage"]
    return f"{len(run['steps'])} 次工具调用 · 调用模型 {usage['llm_calls']} 次 · {run['seconds']} 秒"


def show_steps(run: dict[str, Any], label: str, expanded: bool = False, delay: float = 0.0) -> None:
    """把一次运行的全部事件放进一个进度框。delay > 0 时逐步播放。"""
    with st.status(label, expanded=expanded, state="running" if delay else "complete") as box:
        for event in ui.replay_events(run):
            ui.show_event(box, event, "回放中" if delay else None)
            time.sleep(delay)
        box.update(label=label, state="complete" if run["status"] == "completed" else "error", expanded=expanded)


def show_result(saved: ui.SavedRun) -> None:
    """一次运行的结果：答案（找不到出处的数字标红）、每一步、核查、人工复核、下载。"""
    run = saved.run
    checks = check_run(run)  # 用当前规则现算：位置信息用来标红；和已提交的 checks.json 一致（有测试）
    events = ui.replay_events(run)
    usage = run["usage"]
    ok = run["status"] == "completed"
    st.subheader(ui.to_streamlit_markdown(run["question"]))
    st.markdown(" ".join([
        f":blue-badge[{'财报' if run.get('domain') == 'company' else '电商'}]", f":gray-badge[{run['model']}]",
        f":{'green' if ok else 'red'}-badge[状态 {run['status']}]", f":gray-badge[调用模型 {usage['llm_calls']} 次]",
        f":gray-badge[token {usage['prompt_tokens']} + {usage['completion_tokens']}]",
        f":gray-badge[{run['seconds']} 秒]"]))
    st.info(f"**AI 初稿，待人工复核。** {checks['summary']}")
    repair = run.get("repair")
    if repair:
        st.caption(f"初稿有 {len(repair['ungrounded'])} 个数字找不到出处（{'、'.join(m['text'] for m in repair['ungrounded'])}），"
                   "已退回模型修正 1 次" + ("，修正失败，下面仍是初稿" if repair.get("error") else "；下面是修正后的版本"))
    for signal in checks["danger_signals"]:
        st.warning(signal)
    review = saved.path / "review.md"
    names = ["报告", "每一步", "核查"] + (["人工复核"] if review.exists() else [])
    tabs = st.tabs(names)
    with tabs[0]:
        ungrounded = checks["numbers"]["ungrounded"]
        if ungrounded:
            st.caption(f"标红的 {len(ungrounded)} 个数字在本次工具输出里找不到出处，必须核对。")
        if run.get("answer"):
            st.markdown(ui.to_streamlit_markdown(run["answer"], ungrounded))
        else:
            st.error(f"没有答案：{run.get('error') or run['status']}")
    with tabs[1]:
        for event in events:
            if event["type"] != "tool":
                continue
            step, result = event["step"], event["result"]
            title = (f"第 {step['round']} 轮 · {'✅' if step['ok'] else '❌'} {ui.tool_label(step['tool'])}"
                     f" · {ui.compact(step['arguments'])}")
            with st.expander(title):
                for warning in result.get("warnings", []):
                    st.warning(ui.to_streamlit_markdown(warning))
                st.json(result, expanded=1)
        if not run["steps"]:
            st.caption("本次没有调用工具。")
    with tabs[2]:
        numbers = checks["numbers"]
        st.markdown(f"**数字出处：** {numbers['grounded']}/{numbers['checked']} 个能在工具输出中找到"
                    f"（另有 {numbers['skipped_small_integers']} 个 ≤ 10 的整数不核查）")
        if numbers["ungrounded"]:
            st.table([{"数字": m["text"], "所在句子": one_line(m["context"])} for m in numbers["ungrounded"]])
        for calc in numbers["unsupported_calculations"]:
            st.warning(f"calculate `{calc['expression']}` 的输入 {'、'.join(calc['unsupported_inputs'])} 找不到出处，"
                       "计算结果不算出处")
        st.markdown("**需要人工复核的表述**（因果、绝对化、推测）")
        if checks["phrases"]:
            st.table([{"类别": p["category"], "词": p["phrase"], "所在句子": one_line(p["context"])}
                      for p in checks["phrases"]])
        else:
            st.caption("未发现。推理是否成立仍需人工判断。")
        st.markdown("**分析流程**")
        for skill in checks["process"]["skills"]:
            missing = skill["missing"]
            st.markdown(f"- `{skill['name']}`：" + (f":red[缺少 {'、'.join(missing)}]" if missing else "必做步骤全部完成"))
        if not checks["process"]["skills"]:
            st.caption("本次没有加载分析流程。")
        warnings = list(dict.fromkeys(w for e in events if e["type"] == "tool" for w in e["result"].get("warnings", [])))
        st.markdown("**工具警告**")
        for warning in warnings or ["无"]:
            st.markdown(f"- {ui.to_streamlit_markdown(warning)}")
    if review.exists():
        with tabs[3]:
            st.caption("人工复核记录：自动核查查不出来的推理问题。")
            st.markdown(ui.to_streamlit_markdown(review.read_text(encoding="utf-8")))
    left, right, _ = st.columns([1, 1, 3])
    left.download_button("下载报告（Markdown）", render_report(run, checks), file_name=f"{run['run_id']}-report.md",
                         mime="text/markdown", on_click="ignore")
    right.download_button("下载完整记录（JSON）", json.dumps(run, ensure_ascii=False, indent=2),
                          file_name=f"{run['run_id']}-run.json", mime="application/json", on_click="ignore")


def replay_page() -> None:
    st.header("回放已保存的运行")
    st.caption("不调用模型、不需要密钥：看一次运行从提问、调用工具、自动核查到出报告的全过程。")
    runs = ui.saved_runs()
    if not runs:
        st.info("还没有可回放的运行。")
        return
    index = st.selectbox("选择一次运行", range(len(runs)), format_func=lambda i: runs[i].label)
    saved = runs[index]
    if st.button("▶ 逐步播放"):
        show_steps(saved.run, f"回放完成：{run_stats(saved.run)}", expanded=True, delay=ui.REPLAY_DELAY)
    show_result(saved)


def use_example(key: str) -> None:
    if st.session_state[key]:
        st.session_state.question = st.session_state[key]


def start() -> None:
    st.session_state.running, st.session_state.live_dir = True, None


def live_page() -> None:
    settings = Settings()
    running = st.session_state.running
    st.header("实时提问")
    st.caption(f"调用真实模型（{settings.llm_model}），每次提问会多次调用、消耗 token；"
               f"工具调用上限 {settings.llm_max_tool_calls} 次。结果和命令行一样存到 runs/。")
    domain = st.radio("分析对象", list(ui.DOMAINS), format_func=ui.DOMAINS.get, horizontal=True, key="domain",
                      disabled=running)
    key = f"example_{domain}"
    st.pills("示例问题", ui.EXAMPLE_QUESTIONS[domain], key=key, on_change=use_example, args=(key,), disabled=running)
    question = st.text_area("问题", key="question", height=80, disabled=running,
                            placeholder="例如：上周 GMV 为什么变化？")
    problem = ui.live_problem(domain, settings)
    if problem:
        st.warning(problem)
    st.button("开始分析", type="primary", on_click=start, disabled=running or bool(problem) or not question.strip())
    if running:
        with st.status("分析中：准备数据", expanded=True) as box:
            try:
                run_dir = ui.ask(question.strip(), domain, settings, on_step=lambda e: ui.show_event(box, e, "分析中"))
            finally:  # 中途停止（异常中断）也要让输入框恢复可用
                st.session_state.running = False
        st.session_state.live_dir = str(run_dir)
        st.rerun()  # 重新画一遍页面：输入框恢复可用，结果在下面显示
    if st.session_state.live_dir:
        saved = ui.load_run(Path(st.session_state.live_dir))
        show_steps(saved.run, f"已完成：{run_stats(saved.run)}")
        st.caption(f"运行记录：{saved.path}")
        show_result(saved)


with st.sidebar:
    st.title("da-agent 分析台")
    st.caption("运营数据分析 Agent：模型选工具，Python 算数字，每个数字自动核查出处。")
    mode = st.radio("运行方式", ["回放", "实时提问"], captions=["已保存的运行，不调用模型", "调用真实模型，消耗 token"],
                    key="mode")
    st.divider()
    st.markdown("[项目网站](https://eriksas.github.io/da-agent/) · [GitHub](https://github.com/Eriksas/da-agent)")

if mode == "回放":
    replay_page()
else:
    live_page()
