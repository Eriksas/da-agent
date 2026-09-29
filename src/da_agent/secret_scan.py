"""提交前检查：扫描将要进入 git 的文件里有没有疑似密钥。

只做简单的模式匹配，会漏掉格式特殊的密钥，所以它是第二道防线；
第一道防线是 .env 被 .gitignore 忽略、代码只从环境变量读取密钥。
"""

import logging
import re
import subprocess
import sys
from pathlib import Path

LOGGER = logging.getLogger(__name__)
ALLOW_MARKER = "secret-scan: allow"  # 行尾加这个标记表示“这是测试用假值”，只在测试文件里使用

PATTERNS = {
    "sk- 开头的 API Key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "JWT 格式的 Token": re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "赋值给 API_KEY 的长字符串": re.compile(r"(?i)api_key\s*[=:]\s*['\"]?(?!your-key-here)[A-Za-z0-9_\-]{20,}"),
}


def find_secrets(text: str) -> list[tuple[int, str]]:
    """返回 (行号, 命中的规则名) 列表；不返回密钥原文，避免二次泄露。"""
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                hits.append((number, label))
    return hits


def candidate_files(root: Path) -> list[Path]:
    """列出已跟踪和将要新增（未被忽略）的文件。"""
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    return [root / line for line in output.splitlines() if line]


def main() -> int:
    """扫描当前仓库；发现疑似密钥时返回 1。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = Path.cwd()
    found = 0
    for path in candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue  # 二进制文件或已删除的文件
        for number, label in find_secrets(text):
            LOGGER.error("%s:%d 疑似包含%s", path.relative_to(root), number, label)
            found += 1
    if found:
        LOGGER.error("发现 %d 处疑似密钥，请移除后再提交", found)
        return 1
    LOGGER.info("未发现疑似密钥")
    return 0


if __name__ == "__main__":
    sys.exit(main())
