"""分析流程（Skill）的加载与校验。格式遵循 Agent Skills 规范：每个流程一个文件夹，里面一个 SKILL.md。

按需加载（progressive disclosure）：
- 系统提示词里只放每个流程的名字和一句话说明，占用很少；
- 模型判断需要时，调用 load_skill 工具，才拿到完整步骤。
流程再多也不会把上下文塞满，改流程也不用改代码。
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .paths import ROOT

SKILLS_DIR = ROOT / "skills"
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_BODY_CHARS = 12_000  # 规范建议正文少于 5000 token；中文约每字 1 token 左右，这里留出余量


class SkillError(ValueError):
    """流程文件不符合规范。"""


@dataclass(frozen=True)
class Skill:
    """一个分析流程。required_tools：加载后必须调用的工具，用于流程检查。"""

    name: str
    description: str
    body: str
    required_tools: tuple[str, ...]


def parse_skill(path: Path) -> Skill:
    """读取并校验一个 SKILL.md。"""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SkillError(f"{path}：必须以 --- 开头的配置段开始")
    try:
        _, header, body = text.split("---\n", 2)
    except ValueError as exc:
        raise SkillError(f"{path}：配置段没有用 --- 结束") from exc
    meta = yaml.safe_load(header) or {}
    name, description = meta.get("name", ""), meta.get("description", "")
    if not NAME_PATTERN.fullmatch(str(name)) or name != path.parent.name:
        raise SkillError(f"{path}：name 必须是小写字母、数字和连字符，并与文件夹同名（收到 {name!r}）")
    if not str(description).strip():
        raise SkillError(f"{path}：description 不能为空，它决定模型什么时候使用这个流程")
    if len(body) > MAX_BODY_CHARS:
        raise SkillError(f"{path}：正文 {len(body)} 字符，超过 {MAX_BODY_CHARS}；请精简或拆出参考文件")
    tools = str((meta.get("metadata") or {}).get("required_tools", ""))
    return Skill(name=name, description=str(description).strip(), body=body.strip(),
                 required_tools=tuple(t.strip() for t in tools.split(",") if t.strip()))


def load_skills(skills_dir: Path = SKILLS_DIR) -> dict[str, Skill]:
    """读取目录下的全部流程，按名字索引。"""
    return {skill.name: skill for skill in
            (parse_skill(path) for path in sorted(skills_dir.glob("*/SKILL.md")))}


def catalog(skills: dict[str, Skill]) -> str:
    """放进系统提示词的流程目录：每个流程只有名字和一句话说明。"""
    if not skills:
        return "（当前没有可用的分析流程）"
    return "\n".join(f"- `{skill.name}`：{skill.description}" for skill in skills.values())
