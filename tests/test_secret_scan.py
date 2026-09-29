"""密钥扫描：能抓到常见格式，不误报占位值。"""

from da_agent.secret_scan import find_secrets


def test_detects_sk_style_key() -> None:
    key = "sk-cp-abcdefghijklmnopqrstuvwxyz123456"  # secret-scan: allow
    assert find_secrets(f'key = "{key}"') != []


def test_detects_jwt_style_token() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"  # secret-scan: allow
    assert find_secrets(f"token: {token}") != []


def test_reports_line_number_without_secret_text() -> None:
    hits = find_secrets("第一行\nLLM_API_KEY=abcdefghijklmnopqrstuvwxyz0123")  # secret-scan: allow
    assert hits == [(2, "赋值给 API_KEY 的长字符串")]


def test_placeholder_is_not_flagged() -> None:
    assert find_secrets("LLM_API_KEY=your-key-here") == []


def test_allow_marker_skips_line() -> None:
    line = "sk-cp-abcdefghijklmnopqrstuvwxyz123456  # secret-scan: allow"
    assert find_secrets(line) == []
