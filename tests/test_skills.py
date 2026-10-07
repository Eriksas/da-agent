"""分析流程文件：符合 Agent Skills 规范，按需加载。"""

from pathlib import Path

import pytest

from da_agent.skills import SkillError, catalog, load_skills, parse_skill


def write_skill(root: Path, folder: str, text: str) -> Path:
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_real_skill_is_valid() -> None:
    skills = load_skills()
    skill = skills["ecommerce-metric-diagnosis"]
    assert skill.required_tools == ("metric_summary", "decompose_gmv", "drilldown")
    assert "陷阱清单" in skill.body and "可信度自评" in skill.body
    assert "不适用于" in skill.description  # 说明里要写清楚什么时候不用


def test_catalog_only_has_name_and_description() -> None:
    text = catalog(load_skills(domain="ecommerce"))
    assert text.startswith("- `ecommerce-metric-diagnosis`：")
    assert "陷阱清单" not in text  # 正文不进系统提示词，按需加载
    assert "company-financial-analysis" not in text  # 每个领域只列自己的流程


def test_real_company_skill_is_valid() -> None:
    skills = load_skills(domain="company")
    assert list(skills) == ["company-financial-analysis"]
    skill = skills["company-financial-analysis"]
    assert skill.required_tools == ("company_overview", "financial_summary")
    assert "不适用于" in skill.description and "买卖建议" in skill.description
    assert "未披露不等于 0" in skill.body and "财年不对齐" in skill.body


@pytest.mark.parametrize("folder, text, message", [
    ("good-name", "no frontmatter", "必须以 ---"),
    ("good-name", "---\nname: Bad_Name\ndescription: x\n---\nbody", "小写字母"),
    ("good-name", "---\nname: other-name\ndescription: x\n---\nbody", "与文件夹同名"),
    ("good-name", "---\nname: good-name\ndescription: ''\n---\nbody", "description 不能为空"),
    ("good-name", "---\nname: good-name\ndescription: x\n---\n" + "字" * 13_000, "超过"),
], ids=["no-frontmatter", "bad-name", "folder-mismatch", "empty-description", "too-long"])
def test_invalid_skills_are_rejected(tmp_path: Path, folder: str, text: str, message: str) -> None:
    with pytest.raises(SkillError, match=message):
        parse_skill(write_skill(tmp_path, folder, text))


def test_required_tools_are_optional(tmp_path: Path) -> None:
    skill = parse_skill(write_skill(tmp_path, "plain", "---\nname: plain\ndescription: 用途\n---\n步骤"))
    assert skill.required_tools == ()
