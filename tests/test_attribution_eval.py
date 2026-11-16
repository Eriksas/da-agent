"""数字归属核对的评测（M18）：材料怎么取、植入的错误怎么算“指到了”、汇总。不调用模型。"""

import json
from pathlib import Path

import pytest

from da_agent.attribution_eval import bases, build_materials, clean_flags, evaluate, load_plan, render_summary, summarize

ANSWER = ("本周 GMV 为 120,064.77，净销售额 118,850.04。\n"
          "英国 GMV 从 110,861.51 降到 98,233.40。\n"
          "- 活跃客户数 -13,310.62（53.52%）")
TOOLS = [
    {"tool": "metric_summary", "metrics": [
        {"metric": "gmv", "label": "GMV", "current": 120064.77}, {"metric": "net_sales", "label": "净销售额", "current": 118850.04}]},
    {"tool": "drilldown", "metric_label": "GMV", "dimension": "country", "segments": [
        {"segment": "United Kingdom", "base": 110861.51, "current": 98233.4}, {"segment": "Netherlands", "base": 18570.88}]},
    {"tool": "decompose_gmv", "contributions": [
        {"factor": "active_customers", "label": "活跃客户数", "shapley": -13310.62, "share_of_total_change": 0.5352}]}]


def write_run(folder: Path, answer: str = ANSWER, status: str = "completed") -> None:
    folder.mkdir(parents=True)
    messages = [{"role": "system", "content": "提示词"}, {"role": "user", "content": "这周怎么样？"}]
    messages += [{"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps(t, ensure_ascii=False)} for i, t in enumerate(TOOLS)]
    run = {"run_id": folder.name, "status": status, "answer": answer, "domain": "ecommerce", "messages": messages,
           "steps": [{"tool": t["tool"], "ok": True} for t in TOOLS]}
    (folder / "run.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict]:
    write_run(tmp_path / "old" / "a1")
    (tmp_path / "review.yaml").write_text("bases:\n  - {id: a1, run: old/a1}\n", encoding="utf-8")
    for model in ("flash", "m3"):
        write_run(tmp_path / "eval" / "results" / f"p-{model}-cases" / "runs" / "w48" / "agent_skill-1")
        write_run(tmp_path / "eval" / "results" / f"p-{model}-cases" / "runs" / "w48" / "agent-1")  # 别的对比组不取
    write_run(tmp_path / "eval" / "results" / "p-flash-cases" / "runs" / "w49" / "agent_skill-1", status="llm_error")
    write_run(tmp_path / "eval" / "results" / "p-m3-cases" / "runs" / "w49" / "agent_skill-1", answer="")
    plan = {"dev": [{"id": "d1", "run": "old/a1"}], "test_a": "review.yaml",
            "test_b": {"prefix": "p", "models": ["flash", "m3"], "suites": ["cases"], "runs": ["agent_skill-1"], "seeded_models": ["flash"]},
            "test_c": {"prefix": "p", "models": ["flash", "m3"], "suites": ["cases"], "runs": ["agent-1", "agent-2"],
                       "seeded_models": ["flash"]}}
    return tmp_path, plan


def test_held_out_answers_are_picked_by_rule(repo: tuple[Path, dict]) -> None:
    root, plan = repo
    assert [(b["id"], b["set"], b["seed"]) for b in bases(plan, "dev", root)] == [("d1", "dev", True)]
    picked = bases(plan, "test", root)
    assert [(b["id"], b["set"], b["seed"]) for b in picked] == [
        ("a1", "A", True), ("flash-cases-w48", "B", True), ("m3-cases-w48", "B", False)]  # 没正常结束、没有答案的不取
    assert [b["id"] for b in bases(plan, "dev2", root)] == ["d1", "a1", "flash-cases-w48", "m3-cases-w48"]  # 第二轮的开发材料
    assert [(b["id"], b["set"]) for b in bases(plan, "test2", root)] == [
        ("flash-cases-w48-agent-1", "C"), ("m3-cases-w48-agent-1", "C")]  # 其余对比组的运行；不存在的（agent-2）跳过
    kinds = [(m["base"], m["kind"]) for m in build_materials(plan, "test", root)]
    assert kinds.count(("m3-cases-w48", "clean")) == 1 and ("m3-cases-w48", "entity") not in kinds  # 只给 Flash 的答案植入


def test_planted_errors_are_detected_and_counted(repo: tuple[Path, dict]) -> None:
    root, plan = repo
    rows = evaluate(build_materials(plan, "test", root))
    by_id = {r["id"]: r for r in rows}
    assert by_id["a1-clean"]["flags"][0]["kind"] == "含义" and len(by_id["a1-clean"]["flags"]) == 1  # 原样答案只有一条占比提示
    entity = by_id["a1-entity"]
    assert entity["outcome"] == "detected" and "英国 净销售额 从 110,861.51" in entity["after"]  # GMV 被换成了净销售额
    flags = clean_flags(rows)
    assert [f["no"] for f in flags] == [1, 2, 3] and {f["material"] for f in flags} == {"a1-clean", "flash-cases-w48-clean",
                                                                                         "m3-cases-w48-clean"}
    summary = summarize(rows)
    assert summary["seeded"]["entity/合计"] == {"planned": 2, "skipped": 0, "ambiguous": 0, "counted": 2, "detected": 2}
    assert summary["seeded"]["entity/A"]["counted"] == 1 and summary["seeded"]["entity/B"]["counted"] == 1
    assert summary["clean"]["合计"]["answers"] == 3 and summary["clean"]["合计"]["含义"] == {"flags": 3, "answers": 3}
    assert summary["clean"]["合计"]["对象"] == {"flags": 0, "answers": 0} and not summary["judged"]
    text = render_summary(summary, {"name": "t", "split_label": "留出集", "started_at": "now"})
    assert "| 对象替换 | 合计 | 2 | 0 | 0 | 2 | 2/2（100%） |" in text and "需要人工逐条判断" in text


def test_judgements_are_joined_by_flag_number(repo: tuple[Path, dict]) -> None:
    root, plan = repo
    rows = evaluate(build_materials(plan, "test", root))
    summary = summarize(rows, {"1": "成立", "2": "成立", "3": "有争议"})
    assert summary["clean"]["合计"]["含义"] == {"flags": 3, "answers": 3, "成立": 2, "有争议": 1, "不成立": 0}
    assert summary["clean"]["A"]["含义"]["成立"] == 1
    text = render_summary(summary, {"name": "t", "split_label": "留出集", "started_at": "now"})
    assert "| 合计 | 3 | " in text and "| 3 | 3 | 2 | 1 | 0 |" in text and "需要人工逐条判断" not in text


def test_a_sentence_already_flagged_in_the_original_is_not_counted(repo: tuple[Path, dict]) -> None:
    """原样答案的这一句本来就有“对象”标记：植入后再有标记，分不清指的是哪个，不计入检出率。"""
    root, plan = repo
    # 原样就把英国的数说成了荷兰的
    write_run(root / "old" / "a2", answer="Netherlands 的 GMV 从 110,861.51 降到 98,233.40。\n净销售额另见下文。")
    (root / "review.yaml").write_text("bases:\n  - {id: a2, run: old/a2}\n", encoding="utf-8")
    rows = {r["id"]: r for r in evaluate(build_materials(plan, "test", root))}
    assert [(f["kind"], f["text"], f["sentence_says"]) for f in rows["a2-clean"]["flags"]] == [("对象", "110,861.51", "Netherlands")]
    assert "净销售额 从 110,861.51" in rows["a2-entity"]["after"] and rows["a2-entity"]["outcome"] == "ambiguous"
    summary = summarize(list(rows.values()) + [])
    assert summary["seeded"]["entity/A"] == {"planned": 1, "skipped": 0, "ambiguous": 1, "counted": 0, "detected": 0}


def test_the_committed_plan_is_usable() -> None:
    plan = load_plan()
    dev = bases(plan, "dev")
    assert len(dev) == 23 and len({b["run"] for b in dev}) == 23
    test_ids = {b["run"] for b in bases(plan, "test")}
    assert not test_ids & {b["run"] for b in dev}  # 开发集和留出集不重叠
    assert sum(b["set"] == "A" for b in bases(plan, "test")) == 16
    held_out = {b["run"] for b in bases(plan, "test2")}
    assert len(held_out) > 100 and not held_out & {b["run"] for b in bases(plan, "dev2")}  # 第二轮的留出集和两轮的开发材料都不重叠


def test_cli_writes_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from da_agent import evaluation
    from da_agent.cli import main

    monkeypatch.setattr(evaluation, "RESULTS_DIR", tmp_path)
    assert main(["eval-attribution", "--split", "dev", "--name", "attr-test"]) == 0
    assert "植入的对象替换：指到" in capsys.readouterr().out
    assert json.loads((tmp_path / "attr-test" / "meta.json").read_text(encoding="utf-8"))["split"] == "dev"
    assert (tmp_path / "attr-test" / "flags.json").exists() and "开发材料" in (tmp_path / "attr-test" / "summary.md").read_text(encoding="utf-8")
