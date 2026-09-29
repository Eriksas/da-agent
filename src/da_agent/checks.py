"""报告核查：数字能否在工具输出里找到出处，哪些表述需要人工复核。

能查的：
- 每个数字能否在本次运行的工具输出（以及用户问题）里找到。找不到，说明是模型自己算的或编的。
- 哪些句子用了因果表述或绝对化的说法。只标记、不拦截，交给人判断。

查不了的（必须人工复核）：
- 数字是否被安在了正确的指标上：只要工具输出里出现过这个数，就算“有出处”。
- 推理是否成立：例如把“前 5 名以外的商品”当成“剔除某一项后”，数字全对，结论却错了。
"""

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

# 不参与核查的文本：周、日期时间、单独的时间、列表序号
IGNORED = [
    re.compile(r"\d{4}-W\d{2}"),
    re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?"),
    re.compile(r"\b\d{1,2}:\d{2}\b"),
    re.compile(r"(?m)^[ \t]*(?:#+[ \t]*)?\d+[.、)][ \t]"),
]
# 数字前面不能是英文字母、数字、下划线或小数点（排除 W49、R09、C581484 这类编号），但可以紧挨汉字
NUMBER = re.compile(r"(?<![A-Za-z0-9_.])([+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)[ \t]*(%|个百分点|万|亿)?")
SCALES = {"%": 0.01, "个百分点": 0.01, "万": 1e4, "亿": 1e8, None: 1.0}
SMALL_INTEGER = 10  # 绝对值不超过它的整数（如“5 天”“前 3 名”）几乎总能碰巧对上，不核查，单独计数

PHRASES: dict[str, tuple[str, ...]] = {
    "因果表述": ("导致", "造成", "因为", "由于", "归因于", "引起", "带动", "拉动"),
    "绝对化或无依据的推测": ("显然", "肯定", "一定", "必然", "毫无疑问", "完全是", "全靠", "全部来自", "更可能", "大概率"),
}
NEGATABLE = ("一定", "肯定", "必然")  # 前面是“不”时是在表达不确定，不需要标记（如“不一定是同一批订单”）


@dataclass(frozen=True)
class NumberMention:
    """报告里出现的一个数字。"""

    text: str
    value: float  # 换算到工具输出的单位后的值（2.31% → 0.0231）
    tolerance: float  # 按显示精度允许的误差
    context: str


def _context(text: str, start: int, end: int, width: int = 30) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line = text[line_start: line_end if line_end != -1 else len(text)].strip()
    return line if len(line) <= 2 * width else "…" + text[max(line_start, start - width): end + width].strip() + "…"


def extract_numbers(text: str) -> tuple[list[NumberMention], int]:
    """提取报告里的数字。返回（需要核查的数字，跳过的小整数个数）。"""
    masked = text
    for pattern in IGNORED:
        masked = pattern.sub(lambda m: " " * len(m.group()), masked)  # 用等长空格替换，保持位置不变
    mentions, skipped = [], 0
    for match in NUMBER.finditer(masked):
        raw, unit = match.group(1), match.group(2)
        number = float(raw.replace(",", "").replace("−", "-"))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        if unit is None and decimals == 0 and abs(number) <= SMALL_INTEGER:
            skipped += 1
            continue
        scale = SCALES[unit]
        mentions.append(NumberMention(text=match.group().strip(), value=abs(number) * scale,
                                      tolerance=0.5 * 10 ** -decimals * scale,
                                      context=_context(text, match.start(), match.end())))
    return mentions, skipped


def _numbers_in(value: Any, pool: list[float]) -> None:
    """收集工具输出里所有的数：数值字段，以及文字里出现的数（例如警告里的“少于 30”）。"""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        pool.append(abs(float(value)))
    elif isinstance(value, str):
        for match in NUMBER.finditer(value):
            number = abs(float(match.group(1).replace(",", "").replace("−", "-")))
            pool.append(number)
            if match.group(2):  # 文字里带单位的数（如“超过 50%”），同时存一份换算后的值（0.5），和报告里的写法对得上
                pool.append(number * SCALES[match.group(2)])
    elif isinstance(value, dict):
        for item in value.values():
            _numbers_in(item, pool)
    elif isinstance(value, list):
        for item in value:
            _numbers_in(item, pool)


def source_numbers(messages: list[dict[str, Any]]) -> list[float]:
    """可以作为出处的数：工具输出、用户问题、系统提示词（数据范围等）。模型自己的话不算出处。"""
    pool: list[float] = []
    for message in messages:
        if message["role"] == "tool":
            _numbers_in(json.loads(message["content"]), pool)
        elif message["role"] in ("user", "system"):
            _numbers_in(message["content"], pool)
    return pool


def flagged_phrases(text: str) -> list[dict[str, str]]:
    """找出因果表述和绝对化说法，附上所在的句子。"""
    found = []
    for category, words in PHRASES.items():
        for word in words:
            for match in re.finditer(re.escape(word), text):
                if word in NEGATABLE and text[max(0, match.start() - 1):match.start()] == "不":
                    continue
                found.append({"phrase": word, "category": category,
                              "context": _context(text, match.start(), match.end())})
    return found


DANGER_CHANGE = 0.5  # 核心指标变化超过 50%：先确认是不是数据问题，再解读业务


def _tool_results(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(m["content"]) for m in run["messages"] if m["role"] == "tool"]


def process_check(run: dict[str, Any]) -> dict[str, Any]:
    """加载了分析流程后，流程要求的必做工具是否都成功调用了。"""
    loaded = [r for r in _tool_results(run) if r.get("tool") == "load_skill"]
    called = {step["tool"] for step in run.get("steps", []) if step["ok"]}
    skills = [{"name": r["name"], "required": r["required_tools"],
               "missing": [tool for tool in r["required_tools"] if tool not in called]} for r in loaded]
    if not skills:
        summary = "流程检查：本次没有加载分析流程"
    else:
        missing = sorted({tool for skill in skills for tool in skill["missing"]})
        names = "、".join(skill["name"] for skill in skills)
        summary = f"流程检查：已加载 {names}，" + (f"缺少必做步骤 {'、'.join(missing)}" if missing else "必做步骤全部完成")
    return {"skills": skills, "summary": summary}


def danger_signals(run: dict[str, Any]) -> list[str]:
    """核心指标变化超过 50% 的提醒。比率类指标只看百分点变化，不在此列。"""
    signals = []
    for result in _tool_results(run):
        if result.get("tool") != "metric_summary":
            continue
        for row in result["metrics"]:
            if row["unit"] != "rate" and row["change_pct"] is not None and abs(row["change_pct"]) > DANGER_CHANGE:
                signals.append(f"{result['period']['current']} {row['label']}变化 {row['change_pct']:+.2%}，超过 50%："
                               "请确认报告已先排查数据问题（极端大额行、不完整周等），再解读业务")
    return list(dict.fromkeys(signals))


def check_run(run: dict[str, Any]) -> dict[str, Any]:
    """核查一次运行的答案。run 是 run.json 的内容。"""
    pool = source_numbers(run["messages"])
    mentions, skipped = extract_numbers(run.get("answer") or "")
    ungrounded = [m for m in mentions
                  if not any(abs(value - m.value) <= m.tolerance + 1e-9 for value in pool)]
    phrases = flagged_phrases(run.get("answer") or "")
    return {
        "numbers": {"checked": len(mentions), "grounded": len(mentions) - len(ungrounded),
                    "ungrounded": [asdict(m) for m in ungrounded], "skipped_small_integers": skipped},
        "phrases": phrases,
        "process": process_check(run),
        "danger_signals": danger_signals(run),
        "summary": (f"数字核查：{len(mentions) - len(ungrounded)}/{len(mentions)} 个能在工具输出中找到出处"
                    f"（另有 {skipped} 个 ≤ {SMALL_INTEGER} 的整数未核查）；需人工复核的表述 {len(phrases)} 处"),
    }
