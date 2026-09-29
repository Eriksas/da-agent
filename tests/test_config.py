"""配置读取与密钥保护。"""

import pytest

from da_agent.config import MissingApiKeyError, Settings


def make_settings(**kwargs: object) -> Settings:
    """不读取本机 .env，保证测试结果和本机配置无关。"""
    return Settings(_env_file=None, **kwargs)


def test_defaults_point_to_minimax(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    settings = make_settings()
    assert settings.llm_base_url == "https://api.minimax.cn/v1"
    assert settings.llm_model == "MiniMax-M3"
    assert settings.has_api_key() is False


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "MiniMax-M2.7")
    monkeypatch.setenv("LLM_MAX_TOOL_CALLS", "5")
    settings = make_settings()
    assert settings.llm_model == "MiniMax-M2.7"
    assert settings.llm_max_tool_calls == 5


@pytest.mark.parametrize("value", ["", "your-key-here", "  "])
def test_placeholder_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LLM_API_KEY", value)
    settings = make_settings()
    assert settings.has_api_key() is False
    with pytest.raises(MissingApiKeyError, match="--llm fake"):
        settings.require_api_key()


def test_key_is_hidden_when_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_key = "sk-test-0000000000000000000000"  # secret-scan: allow
    monkeypatch.setenv("LLM_API_KEY", fake_key)
    settings = make_settings()
    assert fake_key not in repr(settings)
    assert fake_key not in str(settings.model_dump())
    assert settings.require_api_key() == fake_key


def test_tool_call_limit_is_bounded() -> None:
    with pytest.raises(ValueError):
        make_settings(llm_max_tool_calls=0)
