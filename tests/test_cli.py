"""命令行：没有密钥也能运行，且不打印密钥。"""

import pytest

from da_agent import cli


def test_demo_runs_with_fake_llm(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["demo", "--llm", "fake"]) == 0
    assert "假模型回复" in capsys.readouterr().out


def test_doctor_never_prints_the_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake_key = "sk-test-1111111111111111111111"  # secret-scan: allow
    monkeypatch.setenv("LLM_API_KEY", fake_key)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "已配置" in out
    assert fake_key not in out
