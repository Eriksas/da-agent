"""复核 Agent 的输入、产出格式和程序校验（M15）。

程序核查（checks.py）只能查“数字在工具输出里出现过”，查不了数字有没有被安在正确的指标上、方向有没有说反、
概括和因果有没有依据。这一部分交给第二个 Agent：它只看问题、答案和工具调用记录（不看写答案的模型的思考过程），
逐条核对后提交结构化的复核意见。

和项目里其他地方一样，模型只做判断，程序校验它交出来的东西：
- 每条意见必须引用答案里的原句（quote 必须是答案的原文片段），界面和报告靠它定位；
- 依据必须指向真实存在的工具调用（第几次）；“没有依据”这一类可以不填；
- 结论和意见要一致：有“错误”级别的意见就不能给“通过”。
校验不过就退回，让它改了再交。复核意见只标记、不改写答案：改不改由人决定。
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_FINDINGS = 12
Category = Literal["数字用错对象", "方向或比较说反", "概括与数据不符", "因果或推测没有依据", "忽略了工具的警告", "推理不成立"]
NO_EVIDENCE_NEEDED = ("因果或推测没有依据",)  # 这一类的问题恰恰是“工具结果里没有”，可以不填依据


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Finding(_Model):
    quote: str = Field(min_length=4, max_length=200, description="有问题的那句话：从答案里原样照抄一段（不改字、不省略）")
    category: Category = Field(description=(
        "数字用错对象：数字是对的，但安在了别的指标、分组或时期上；方向或比较说反：升说成降、高说成低；"
        "概括与数据不符：“都”“越…越…”“最高”与数据不一致；因果或推测没有依据：工具结果里没有支持它的证据；"
        "忽略了工具的警告：工具提示了不完整周期、小样本等，答案却照常下结论；推理不成立：其他推理错误"))
    severity: Literal["错误", "存疑"] = Field(description="错误：对照工具结果可以确定是错的；存疑：证据不足，需要人判断")
    explanation: str = Field(min_length=6, max_length=300, description="为什么有问题：工具结果实际是什么，答案说成了什么")
    evidence: list[int] = Field(default_factory=list, max_length=6, description=(
        "依据：第几次工具调用（编号见“工具调用记录”）。“因果或推测没有依据”可以不填，其余至少填 1 个"))


class SubmitReviewArgs(_Model):
    verdict: Literal["通过", "有问题"] = Field(description="通过：没有发现“错误”级别的问题；有问题：至少有 1 处错误")
    findings: list[Finding] = Field(default_factory=list, max_length=MAX_FINDINGS, description=(
        "发现的问题，最重要的在前；没有就留空。只写能指出具体句子的问题，不写泛泛的建议"))
    checked: str = Field(min_length=4, max_length=300, description="核对了哪些内容，例如“核对了 GMV、订单数、客单价的数值和方向，以及三条结论的依据”")


@dataclass(frozen=True)
class ReviewContext:
    """复核工具的上下文：被复核的答案和它依据的工具调用次数。在工具注册表里占的是分析工具“数据库连接”的位置。"""

    answer: str
    tool_calls: int


class ReviewProblems(ValueError):
    """复核意见本身不合格：每条都是给复核 Agent 看的修改意见。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("复核意见没有通过检查，请逐条修改后重新提交：\n" + "\n".join(f"- {p}" for p in problems))
        self.problems = problems


def _squash(text: str) -> str:
    return "".join(text.split())


def locate(answer: str, quote: str) -> tuple[int, int] | None:
    """quote 在答案里的位置（忽略空白的差别）。找不到返回 None。"""
    start = answer.find(quote)
    if start >= 0:
        return start, start + len(quote)
    target = _squash(quote)
    if not target:
        return None
    kept = [(i, ch) for i, ch in enumerate(answer) if not ch.isspace()]  # 去掉空白后的每个字符在原文里的位置
    at = "".join(ch for _, ch in kept).find(target)
    if at < 0:
        return None
    return kept[at][0], kept[at + len(target) - 1][0] + 1


def review_problems(args: SubmitReviewArgs, ctx: ReviewContext) -> list[str]:
    problems = []
    for i, f in enumerate(args.findings, 1):
        if locate(ctx.answer, f.quote) is None:
            problems.append(f"第 {i} 条：quote 在答案里找不到。必须从答案原样照抄一段，不要改写或拼接（收到：{f.quote[:40]}）")
        bad = [n for n in f.evidence if not 1 <= n <= ctx.tool_calls]
        if bad:
            problems.append(f"第 {i} 条：evidence 里的 {bad} 不是有效的工具调用编号（共 {ctx.tool_calls} 次，编号从 1 开始）")
        if not f.evidence and f.category not in NO_EVIDENCE_NEEDED:
            problems.append(f"第 {i} 条：“{f.category}”要在 evidence 里写出依据的是第几次工具调用")
    errors = sum(f.severity == "错误" for f in args.findings)
    if args.verdict == "通过" and errors:
        problems.append(f"verdict 是“通过”，但有 {errors} 条“错误”级别的意见：改成“有问题”，或者把证据不足的降为“存疑”")
    if args.verdict == "有问题" and not errors:
        problems.append("verdict 是“有问题”，但没有“错误”级别的意见：指出具体的错误，或者改成“通过”")
    return problems


def submit_review(ctx: ReviewContext, args: SubmitReviewArgs) -> dict[str, Any]:
    """校验复核意见。通过后返回带位置的意见（报告和界面用位置标出原句）。"""
    problems = review_problems(args, ctx)
    if problems:
        raise ReviewProblems(problems)
    findings = []
    for f in args.findings:
        start, end = locate(ctx.answer, f.quote)
        findings.append({**f.model_dump(), "start": start, "end": end})
    return {"tool": "submit_review", "accepted": True, "verdict": args.verdict, "findings": findings,
            "checked": args.checked, "message": "复核意见已记录。请用一两句话说明结论，不要重复列出每条意见。"}


def tool_records(run: dict[str, Any]) -> list[dict[str, Any]]:
    """被复核的运行里每一次工具调用：编号、工具、参数、结果（失败的也算，编号和报告附录 D 的顺序一致）。"""
    results = [json.loads(m["content"]) for m in run["messages"] if m["role"] == "tool"]
    return [{"no": i, "tool": step["tool"], "arguments": step["arguments"], "result": result}
            for i, (step, result) in enumerate(zip(run["steps"], results, strict=True), 1)]


def review_request(run: dict[str, Any]) -> str:
    """交给复核 Agent 的材料：问题、答案、工具调用记录。不包含写答案的模型的思考过程和中间回复。"""
    lines = ["## 用户的问题", "", run["question"], "", "## 待复核的答案", "", run.get("answer") or "", "", "## 工具调用记录", ""]
    records = tool_records(run)
    for r in records:
        lines += [f"### 第 {r['no']} 次：{r['tool']} {json.dumps(r['arguments'], ensure_ascii=False)}", "",
                  json.dumps(r["result"], ensure_ascii=False), ""]
    if not records:
        lines += ["（这次运行没有调用任何工具。）", ""]
    return "\n".join(lines)
