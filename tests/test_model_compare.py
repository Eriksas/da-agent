"""多模型对比的汇总（M16）：只读已有的评测结果，并排列出来。"""

import json
from pathlib import Path

import pytest

from da_agent.model_compare import collect, load_plan, render_comparison

PLAN = {"prefix": "models-test",
        "models": [{"id": "a", "model": "model-a", "note": "一直在用的"}, {"id": "b", "model": "model-b", "note": "更大的"}],
        "suites": [{"id": "cases", "label": "原题", "case_file": "eval/cases.yaml", "conditions": ["baseline", "agent"]}]}


def _row(passed: int, runs: int, draft: int, grounded: float | None, tokens: float, completed: float = 1.0) -> dict:
    return {"runs": runs, "passed": passed, "passed_without_repair": draft, "grounded_rate": grounded,
            "completed_rate": completed, "avg_tokens": tokens, "avg_seconds": 41.4}


def _write(folder: Path, model: str, baseline: dict, agent: dict, by_case: dict) -> None:
    folder.mkdir(parents=True)
    summary = {"by_condition": {"baseline": baseline, "agent": agent}, "by_case": by_case}
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"model": model}), encoding="utf-8")


def test_models_are_listed_side_by_side_and_differences_are_named(tmp_path: Path) -> None:
    case = lambda passed: {"category": "x", "baseline": {"passed": 0, "runs": 1}, "agent": {"passed": passed, "runs": 2}}  # noqa: E731
    _write(tmp_path / "models-test-a-cases", "model-a", _row(2, 10, 2, 0.51, 3000), _row(19, 20, 11, 1.0, 30286.4),
           {"w48": case(2), "w49": case(2)})
    _write(tmp_path / "models-test-b-cases", "model-b-returned", _row(4, 10, 4, None, 3100), _row(17, 20, 15, 0.98, 51234, 0.95),
           {"w48": case(2), "w49": case(0)})
    text = render_comparison(PLAN, collect(PLAN, tmp_path))
    assert "| a | 一直在用的 | model-a |" in text and "| b | 更大的 | model-b-returned |" in text  # 模型名取自评测记录
    assert "## 原题：直接问模型" in text and "| 通过 | 2/10 | 4/10 |" in text and "| 数字有出处 | 51% | — |" in text
    agent = text.split("## 原题：Agent")[1]
    assert "| 通过 | 19/20 | 17/20 |" in agent and "| 只看初稿通过 | 11/20 | 15/20 |" in agent
    assert "| 正常结束 | 20/20 | 19/20 |" in agent and "| 平均 token | 30,286 | 51,234 |" in agent and "| 平均用时 | 41 秒 | 41 秒 |" in agent
    assert "只看初稿" not in text.split("## 原题：Agent")[0]  # 直接问模型没有退回修正，不列这一行
    assert "| w49 | 2/2 | 0/2 |" in text and "| w48 |" not in text  # 只列通过次数不同的题


def test_missing_runs_are_marked_and_identical_results_say_so(tmp_path: Path) -> None:
    case = {"category": "x", "baseline": {"passed": 0, "runs": 1}, "agent": {"passed": 2, "runs": 2}}
    _write(tmp_path / "models-test-a-cases", "model-a", _row(2, 10, 2, 0.5, 3000), _row(20, 20, 20, 1.0, 30000), {"w48": case})
    data = collect(PLAN, tmp_path)
    assert data["cases"]["b"] is None
    text = render_comparison(PLAN, data)
    assert "| 通过 | 2/10 | 没跑 |" in text and "| b | 更大的 | 没跑 |" in text
    _write(tmp_path / "models-test-b-cases", "model-b", _row(2, 10, 2, 0.5, 3000), _row(20, 20, 20, 1.0, 30000), {"w48": case})
    assert "原题：每道题各模型的通过次数都相同。" in render_comparison(PLAN, collect(PLAN, tmp_path))


def test_the_committed_plan_names_three_models_and_three_suites() -> None:
    plan = load_plan()
    assert [m["id"] for m in plan["models"]] == ["flash", "m3", "m27"]
    assert [(s["id"], s["conditions"]) for s in plan["suites"]] == [
        ("cases", ["baseline", "agent", "agent_skill"]), ("holdout", ["agent", "agent_skill"]),
        ("company", ["baseline", "agent_skill"])]
    for suite in plan["suites"]:
        assert (Path(__file__).parent.parent / suite["case_file"]).exists()


def test_cli_writes_the_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from da_agent import evaluation, model_compare
    from da_agent.cli import main

    monkeypatch.setattr(evaluation, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(model_compare, "RESULTS_DIR", tmp_path)
    assert main(["eval-models"]) == 0
    assert "还没有结果：flash-cases" in capsys.readouterr().out
    assert "没跑" in (tmp_path / "models-2026-11-11.md").read_text(encoding="utf-8")
