"""复核 Agent 的评测（M15）：它能不能查出程序查不出的错误，又会不会乱报。

“模型查模型”要拿得出证据，所以用三组材料量它：
- 植入错误：取已保存的真实答案，用程序在其中改一处，改完后程序的数字核查结果和原来完全一样
  （数字都还“有出处”），也就是现有核查按设计查不出的错。看复核 Agent 是否指到被改的那一句。
- 原样答案：同一批答案不做改动，看它报了多少问题。这些答案不保证没错，所以“错误”级别的意见要人工逐条判断。
- 自然错误：人工复核时标出推理有错的几份答案，看它是否指到同一处。

四种植入方式（每种在一份答案里最多改一处）：
- direction 方向反转：把“环比下降 17%”这类紧跟着数字的方向词改成相反的。
- swap 数字对调：把两句里单位和小数位数相同、相差 20% 以上的两个数字互换。
- entity 对象替换：把一句里的指标、分组或公司名换成答案里出现过的另一个同类名字。
- cause 无依据的原因：在一句结论后面加一句工具结果里没有任何依据的原因。
"""

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .checks import extract_numbers, number_check
from .llm import LLMClient
from .paths import ROOT
from .reviewer import review_run

CASES_PATH = ROOT / "eval" / "review_cases.yaml"
KINDS = ("direction", "swap", "entity", "cause")
KIND_LABELS = {"direction": "方向反转", "swap": "数字对调", "entity": "对象替换", "cause": "无依据的原因",
               "clean": "原样答案", "natural": "自然错误"}
OPPOSITES = [("增长", "下降"), ("上升", "下降"), ("增加", "减少"), ("上涨", "下跌"), ("提升", "下降"), ("扩大", "收窄"),
             ("高于", "低于")]
FLIP = {a: b for a, b in OPPOSITES} | {"下降": "上升", "减少": "增加", "下跌": "上涨", "收窄": "扩大", "低于": "高于"}
# 方向词后面紧跟着一个数（“环比下降 17.16%”“减少 33 人”“高于京东的 2,100.28”）：这样的句子是在陈述一个算出来的变化，
# 改掉方向词一定是错的。只是提到“增长”二字的句子（“从 0 算起的增长没有业务含义”）不改
DIRECTION = re.compile("(" + "|".join(sorted(FLIP, key=len, reverse=True)) + r")[ \t*了到约至为的]{0,6}[+\-−]?\d")
UNCERTAIN = ("是否", "如果", "若", "？", "?", "可能", "假设", "不是", "并非", "没有", "不要", "推断")  # 没有下结论或带否定的句子不植入
CAUSES = {"ecommerce": "这一变化主要是因为当周站内促销活动的调整。", "company": "这一变化主要是因为公司当年调整了定价策略。"}
ENTITY_KEYS = ("segment", "label", "name", "company")  # 工具结果里放分组名、指标名、公司名的字段
SWAP_MIN_GAP = 0.2
UNIT = re.compile(r"(%|个百分点|pp|万|亿)$")
SENTENCE_END = re.compile(r"[。；;！!\n]")
JOINERS = "-/~～:：+−"  # 数字两边挨着这些符号时多半是日期、区间、比例的一部分（12-31、5000/5000），不拿来对调


@dataclass(frozen=True)
class Mutation:
    kind: str
    answer: str  # 改过的答案
    spans: list[tuple[int, int]]  # 被改的句子在新答案里的位置（对调有两处）
    lines: list[int]  # 被改的是第几句（原答案和新答案里相同），用来和原样答案的复核结果对照
    before: str
    after: str


def _lines(text: str) -> list[tuple[int, int, str]]:
    """按句切开（句号、分号、换行），返回每句的（起，止，文字）。答案里一段常有好几个结论，按句才能说清改的是哪一个。"""
    out, pos = [], 0
    for found in SENTENCE_END.finditer(text):
        out.append((pos, found.start(), text[pos:found.start()]))
        pos = found.end()
    out.append((pos, len(text), text[pos:]))
    return out


def _line_of(lines: list[tuple[int, int, str]], offset: int) -> int:
    return next(i for i, (start, end, _) in enumerate(lines) if start <= offset <= end)


def _conclusion_lines(answer: str) -> list[tuple[int, int, int, str]]:
    """可以植入错误的句子：有一个要核查的数字、不在表格里、不是本来就不确定或带否定的句子。返回（第几句，起，止，文字）。"""
    lines = _lines(answer)
    numbered = {_line_of(lines, m.start) for m in extract_numbers(answer)[0]}
    return [(i, s, e, t) for i, (s, e, t) in enumerate(lines)
            if i in numbered and "|" not in t and not any(w in t for w in UNCERTAIN)]


def mutate_direction(answer: str, run: dict[str, Any]) -> Mutation | None:
    for i, start, end, text in _conclusion_lines(answer):
        found = DIRECTION.search(text)
        if not found:
            continue
        word = found.group(1)
        new_line = text[:found.start()] + FLIP[word] + text[found.start() + len(word):]
        mutated = answer[:start] + new_line + answer[end:]
        return Mutation("direction", mutated, [(start, start + len(new_line))], [i], text, new_line)
    return None


def _plain(answer: str, mention: Any) -> bool:
    """能拿来对调的数字：不带正负号，两边不挨着日期、区间、比例的连接符。"""
    before = answer[mention.start - 1] if mention.start else " "
    after = answer[mention.end] if mention.end < len(answer) else " "
    return mention.text[0].isdigit() and before not in JOINERS and after not in JOINERS


def mutate_swap(answer: str, run: dict[str, Any]) -> Mutation | None:
    lines = _lines(answer)
    usable = {i for i, *_ in _conclusion_lines(answer)}
    mentions = [m for m in extract_numbers(answer)[0] if _plain(answer, m) and _line_of(lines, m.start) in usable]

    def shape(m: Any) -> tuple[str, int]:  # 单位和小数位数都相同的才互换：换完看起来仍然像一个正常的数
        number = UNIT.sub("", m.text).strip()
        found = UNIT.search(m.text)
        return (found.group(1) if found else "", len(number.partition(".")[2]))

    for a_i, a in enumerate(mentions):
        for b in mentions[a_i + 1:]:
            gap = abs(abs(a.value) - abs(b.value)) / max(abs(a.value), abs(b.value), 1e-12)
            la, lb = _line_of(lines, a.start), _line_of(lines, b.start)
            if shape(a) != shape(b) or la == lb or gap < SWAP_MIN_GAP:
                continue
            mutated = answer[:a.start] + b.text + answer[a.end:b.start] + a.text + answer[b.end:]
            new_lines = _lines(mutated)
            return Mutation("swap", mutated, [new_lines[la][:2], new_lines[lb][:2]], [la, lb],
                            f"{lines[la][2]} ‖ {lines[lb][2]}", f"{new_lines[la][2]} ‖ {new_lines[lb][2]}")
    return None


def _entities(run: dict[str, Any]) -> list[list[str]]:
    """工具结果里出现过的名字，分成一组组同类的：同一个工具、同一个字段（下钻还要同一个维度）。

    只在同类之间替换：指标换指标、国家换国家、公司换公司。
    """
    groups: dict[tuple, list[str]] = {}

    def walk(value: Any, kind: tuple) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ENTITY_KEYS and isinstance(item, str) and 2 <= len(item) <= 20:
                    names = groups.setdefault((*kind, key), [])
                    if item not in names:
                        names.append(item)
                walk(item, kind)
        elif isinstance(value, list):
            for item in value:
                walk(item, kind)

    for message in run["messages"]:
        if message["role"] == "tool":
            content = json.loads(message["content"])
            walk(content, (content.get("tool"), content.get("dimension")))
    return list(groups.values())


def _standalone(text: str, name: str) -> bool:
    """名字前面不是汉字或字母：“新老客”里的“老客”不算单独提到。"""
    at = text.find(name)
    return at == 0 or not (text[at - 1].isalnum() or "\u4e00" <= text[at - 1] <= "\u9fff")


def mutate_entity(answer: str, run: dict[str, Any]) -> Mutation | None:
    groups = [[n for n in names if n in answer] for names in _entities(run)]
    for i, start, end, text in _conclusion_lines(answer):
        for names in groups:
            here = [n for n in names if n in text]
            # 这一句只提到一个名字时才换：两个名字都在的句子多半是在比较，换一个可能变成废话而不是错误
            others = [n for n in names if n not in text and not any(n in h or h in n for h in here)]
            if len(here) != 1 or not others or not _standalone(text, here[0]):
                continue
            new_line = text.replace(here[0], others[0], 1)
            mutated = answer[:start] + new_line + answer[end:]
            return Mutation("entity", mutated, [(start, start + len(new_line))], [i], text, new_line)
    return None


def mutate_cause(answer: str, run: dict[str, Any]) -> Mutation | None:
    sentence = CAUSES["company" if run.get("domain") == "company" else "ecommerce"]
    for i, _start, end, text in _conclusion_lines(answer):
        if not DIRECTION.search(text):
            continue
        at = end + 1 if end < len(answer) and answer[end] != "\n" else end  # 接在这句的句号后面；这句没有句号就接在句尾
        mutated = answer[:at] + sentence + answer[at:]
        return Mutation("cause", mutated, [(at, at + len(sentence))], [i], text, text + " ⟶ 后面加上：" + sentence)
    return None


MUTATORS: dict[str, Callable[[str, dict[str, Any]], Mutation | None]] = {
    "direction": mutate_direction, "swap": mutate_swap, "entity": mutate_entity, "cause": mutate_cause}


def invisible_to_number_check(run: dict[str, Any], mutated: str) -> bool:
    """改完之后，程序的数字核查结果和原答案一样（要核查的数字个数、有出处的个数都没变）。"""
    before, after = number_check(run["answer"], run["messages"]), number_check(mutated, run["messages"])
    return (before["checked"], before["grounded"]) == (after["checked"], after["grounded"])


def load_plan(path: Path = CASES_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def build_cases(plan: dict[str, Any], root: Path = ROOT) -> list[dict[str, Any]]:
    """把评测计划展开成一条条待复核的材料。植入不了、或者植入后数字核查能看出差别的，记为 skipped。"""
    cases = []
    for base in plan["bases"]:
        run = json.loads((root / base["run"] / "run.json").read_text(encoding="utf-8"))
        cases.append({"id": f"{base['id']}-clean", "base": base["id"], "kind": "clean", "run": run})
        for kind in KINDS:
            mutation = MUTATORS[kind](run["answer"], run)
            case: dict[str, Any] = {"id": f"{base['id']}-{kind}", "base": base["id"], "kind": kind}
            if mutation is None:
                cases.append(case | {"skipped": "这份答案里没有可以这样改的句子"})
            elif not invisible_to_number_check(run, mutation.answer):
                cases.append(case | {"skipped": "改完后数字核查的结果变了（程序自己能查出来）"})
            else:
                cases.append(case | {"run": run | {"answer": mutation.answer}, "mutation": asdict(mutation) | {"answer": None}})
    for item in plan.get("natural", []):
        run = json.loads((root / item["run"] / "run.json").read_text(encoding="utf-8"))
        cases.append({"id": item["id"], "base": item["id"], "kind": "natural", "run": run, "known_error": item["known_error"]})
    return cases


def _overlaps(finding: dict[str, Any], spans: list[list[int]] | list[tuple[int, int]]) -> bool:
    return any(finding["start"] < end and start < finding["end"] for start, end in spans)


def score_case(case: dict[str, Any], review: dict[str, Any], clean_review: dict[str, Any] | None,
               clean_answer: str | None) -> dict[str, Any]:
    """一条材料的结果。植入错误：复核意见有没有落在被改的地方。

    如果原样答案的复核里，同一句本来就被指出过问题，就分不清这次指的是植入的错还是原有的错，记为 ambiguous，不计入检出率。
    """
    findings = review["findings"]
    row = {"id": case["id"], "base": case["base"], "kind": case["kind"], "status": review["status"],
           "verdict": review["verdict"], "findings": len(findings), "errors": sum(f["severity"] == "错误" for f in findings),
           "tokens": review.get("usage", {}).get("prompt_tokens", 0) + review.get("usage", {}).get("completion_tokens", 0),
           "seconds": review.get("seconds", 0), "rejected_submissions": review.get("rejected_submissions", 0)}
    if case["kind"] in ("clean", "natural") or review["status"] != "reviewed":
        return row
    mutation = case["mutation"]
    hits = [f for f in findings if _overlaps(f, mutation["spans"])]
    already = False
    if clean_review and clean_answer is not None and case["kind"] != "cause":  # 加的那句原答案里没有，不会混淆
        clean_lines = _lines(clean_answer)
        spans = [clean_lines[i][:2] for i in mutation["lines"]]
        already = any(_overlaps(f, spans) for f in clean_review["findings"])
    outcome = "ambiguous" if hits and already else "detected" if hits else "missed"
    return row | {"outcome": outcome, "as_error": any(f["severity"] == "错误" for f in hits),
                  "categories": sorted({f["category"] for f in hits})}


def run_review_eval(cases: list[dict[str, Any]], make_llm: Callable[[], LLMClient], out_dir: Path,
                    workers: int = 1) -> list[dict[str, Any]]:
    """逐条复核并保存记录。每条材料用一个新的模型客户端（假模型按剧本回放，不能共用）。"""
    todo = [c for c in cases if "run" in c]

    def one(case: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        out = review_run(case["run"], llm=make_llm())
        case_dir = out_dir / "runs" / case["id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        files = {"review.json": out["review"], "review_run.json": out["run"],
                 "material.json": {k: v for k, v in case.items() if k != "run"} | {"answer": case["run"]["answer"]}}
        for name, content in files.items():
            (case_dir / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, default=str) + "\n",
                                         encoding="utf-8", newline="\n")
        return case["id"], out["review"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        reviews = dict(pool.map(one, todo))
    clean = {c["base"]: (reviews[c["id"]], c["run"]["answer"]) for c in todo if c["kind"] == "clean"}
    results = []
    for case in cases:
        if "run" not in case:
            results.append({"id": case["id"], "base": case["base"], "kind": case["kind"], "status": "skipped",
                            "reason": case["skipped"]})
            continue
        clean_review, clean_answer = clean.get(case["base"], (None, None))
        results.append(score_case(case, reviews[case["id"]], clean_review, clean_answer))
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    seeded = {}
    for kind in KINDS:
        rows = [r for r in results if r["kind"] == kind]
        done = [r for r in rows if r["status"] == "reviewed"]
        counted = [r for r in done if r["outcome"] != "ambiguous"]
        seeded[kind] = {"planned": len(rows), "skipped": sum(r["status"] == "skipped" for r in rows),
                        "failed": sum(r["status"] == "failed" for r in rows),
                        "ambiguous": len(done) - len(counted), "counted": len(counted),
                        "detected": sum(r["outcome"] == "detected" for r in counted),
                        "as_error": sum(r["outcome"] == "detected" and r["as_error"] for r in counted)}
    total = {key: sum(s[key] for s in seeded.values()) for key in next(iter(seeded.values()))}
    clean = [r for r in results if r["kind"] == "clean" and r["status"] == "reviewed"]
    reviewed = [r for r in results if r["status"] == "reviewed"]
    return {"seeded": seeded, "seeded_total": total,
            "clean": {"answers": len(clean), "passed": sum(r["verdict"] == "通过" for r in clean),
                      "findings": sum(r["findings"] for r in clean), "errors": sum(r["errors"] for r in clean)},
            "natural": [r for r in results if r["kind"] == "natural"],
            "cost": {"reviews": len(reviewed), "failed": sum(r["status"] == "failed" for r in results),
                     "tokens_per_review": round(sum(r["tokens"] for r in reviewed) / len(reviewed)) if reviewed else 0,
                     "seconds_per_review": round(sum(r["seconds"] for r in reviewed) / len(reviewed), 1) if reviewed else 0,
                     "rejected_submissions": sum(r["rejected_submissions"] for r in reviewed)}}


def _rate(part: int, whole: int) -> str:
    return f"{part}/{whole}" + (f"（{part / whole:.0%}）" if whole else "")


def render_summary(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    total, clean, cost = summary["seeded_total"], summary["clean"], summary["cost"]
    lines = [f"# 复核 Agent 评测：{meta['name']}", "",
             f"复核模型 {meta['model']}；{meta['started_at']}。评分表：[review_rubric.md](../../review_rubric.md)。", "",
             "## 植入的错误（程序的数字核查查不出）", "",
             "| 植入方式 | 计划 | 没法植入 | 复核失败 | 分不清（原样答案里这一句已被指出） | 计入 | 指到了被改的那一句 | 其中判为“错误” |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for kind, s in list(summary["seeded"].items()) + [("合计", total)]:
        lines.append(f"| {KIND_LABELS.get(kind, kind)} | {s['planned']} | {s['skipped']} | {s['failed']} | {s['ambiguous']} | "
                     f"{s['counted']} | {_rate(s['detected'], s['counted'])} | {_rate(s['as_error'], s['counted'])} |")
    lines += ["", "## 原样答案（没有改动）", "",
              f"- {clean['answers']} 份答案里，{clean['passed']} 份判为通过；共提出 {clean['findings']} 条意见，"
              f"其中“错误”级别 {clean['errors']} 条。",
              "- 这些答案不保证没有错，“错误”级别的意见是否成立需要人工逐条判断，见复核记录。", "",
              "## 自然错误（人工复核时标出推理有错的答案）", "",
              "| 材料 | 结论 | 意见数（其中错误） |", "|---|---|---:|"]
    lines += [f"| {r['id']} | {r['verdict'] or r['status']} | {r['findings']}（{r['errors']}） |" for r in summary["natural"]]
    lines += ["", "是否指到了人工标出的那一处，要对照意见内容人工判断，见复核记录。", "",
              "## 代价", "",
              f"- 完成复核 {cost['reviews']} 次，失败 {cost['failed']} 次；每次平均 {cost['tokens_per_review']:,} token、"
              f"{cost['seconds_per_review']} 秒；复核意见被程序退回 {cost['rejected_submissions']} 次。", ""]
    return "\n".join(lines)
