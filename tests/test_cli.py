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
    assert "数字核查：12/12 个能在工具输出中找到出处" in out  # 演示剧本的数字全部来自工具输出
    assert "流程检查：已加载 ecommerce-metric-diagnosis，必做步骤全部完成" in out
    assert len(list(tmp_path.glob("*/report.md"))) == 1


def test_check_command_gates_on_ungrounded_numbers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    import json

    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path)
    cli.main(["demo"])
    run_dir = next(tmp_path.iterdir())
    assert cli.main(["check", str(run_dir)]) == 0
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    data["answer"] += "补充：GMV 其实是 999.99。"  # 手动塞进一个编造的数字
    (run_dir / "run.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["check", str(run_dir)]) == 1
    assert "找不到出处：999.99" in capsys.readouterr().out


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


def test_ui_without_streamlit_explains_how_to_install(monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert cli.main(["ui"]) == 2
    assert "uv sync --group ui" in capsys.readouterr().out


def test_ui_listens_on_localhost_only_and_turns_off_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """实时提问用的是自己的密钥：界面只监听本机，不让局域网里的其他人调用；也不向 Streamlit 发送使用统计。"""
    import importlib.util
    import subprocess

    launched: list[list[str]] = []
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(subprocess, "call", lambda args: launched.append(args) or 0)
    assert cli.main(["ui", "--port", "8600", "--no-browser"]) == 0
    args = launched[0]
    assert args[1:4] == ["-m", "streamlit", "run"] and args[4].endswith("ui_app.py")
    options = dict(zip(args[5::2], args[6::2], strict=True))
    assert options["--server.address"] == "127.0.0.1" and options["--server.port"] == "8600"
    assert options["--browser.gatherUsageStats"] == "false"
