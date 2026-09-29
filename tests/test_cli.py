"""命令行：没有密钥也能运行，且不打印密钥。"""

from pathlib import Path

import pytest

from da_agent import cli


def test_demo_runs_full_agent_loop_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path)
    assert cli.main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "状态：completed" in out
    assert "metric_summary" in out and "decompose_gmv" in out and "drilldown" in out
    assert len(list(tmp_path.glob("*/run.json"))) == 1


def test_doctor_never_prints_the_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake_key = "sk-test-1111111111111111111111"  # secret-scan: allow
    monkeypatch.setenv("LLM_API_KEY", fake_key)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "已配置" in out
    assert fake_key not in out


def test_bad_week_gives_message_not_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    from da_agent.dataset import PARQUET_PATH

    if not PARQUET_PATH.exists():
        pytest.skip("本地没有准备数据")
    assert cli.main(["week", "2011-W60"]) == 2
    assert "错误：2011 年没有第 60 周" in capsys.readouterr().out
