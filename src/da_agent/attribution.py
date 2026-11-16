"""数字归属核对（M18）：数字是不是被安在了正确的对象上。

数字核查（checks.py）只查“这个数在工具结果里出现过”。它查不出这样的错：数是对的，但答案把 GMV 的数说成了净销售额的，
把英国的数说成了荷兰的，或者把“占总变化的比重”写成了“变化率”。这类错以前只能靠人或者第二个 Agent 去看。

这里用程序查其中能确定的一部分，不调用模型：
1. 工具结果是结构化的，每个数都带着几类“名字”：
   - 指标：所在行的指标名（GMV、营业收入）、整张表下钻的是哪个指标、字段名对应的指标（同行对比表里字段就是指标）；
   - 分组：所在行的国家、商品、客户类型；
   - 公司：所在行或整张表是哪家公司。
2. 人读句子时，把数字归给它前面最近的那个名字（“荷兰 GMV 从 110,861.51 降到……”说的是荷兰的 GMV）。
   所以对答案里的每个数，在同一分句、它前面一小段里，每一类名字各找离它最近的一个：
   是它自己的名字就没问题；是同一类里别的名字，就标记为可能安错了对象。
3. 字段含义：一个数的所有出处都是“占总变化的比重”，而这一行没有“占、贡献、来自”这类词，提示它可能被读成变化率。

宁可漏，不乱报：
- 一个数只要有一处出处没有名字（警告里的数、calculate 算出来的），就不查它；
- 数字前面的分句里没有认得出的名字，不标记（常见的是承接上文）；
- 自己的名字从宽（提到一部分就算，“订单数”写成“订单”），别的名字从严（必须整个出现）；
- 表格里的数不查对象（名字在表头和首列，离得远，容易误判）。

第一次在留出的答案上检验时，8 条“对象”标记没有一条成立（eval/results/attribution-2026-11-15）。按误报的原因又收紧了五处：
- 名字只往前找到上一个数字为止：“订单数只降 8.2% 而单均金额降 22.5%”里，“订单数”属于 8.2%；
- 数字后面紧跟着它自己的名字，就不标记：“1,035.92 亿元净利润对应仅 501.50 亿元经营利润”；
- “占总变化的比重”这类数不查对象（只要有一处出处是占比）：它同时关系到部分和整体（“占 ROE 降幅 45.09%”里的 ROE 是整体）；
- 不到 3 位有效数字的数不查：“整体掉了 11%”会碰巧对上别的数；
- 反引号里的文字不当名字：`decompose_gmv` 是工具名，不是在说 GMV。
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from .checks import IGNORED, NUMBER, SCALES, extract_numbers

Name = tuple[str, str]  # （类别，名字）；类别：metric 指标、segment 分组、company 公司
METRIC_IDS = ("metric", "factor", "item", "ratio")  # 一行里有这些代号，这一行的 label 就是指标名
SHARE_FIELDS = ("share_of_total_change",)
SHARE_WORDS = ("占", "贡献", "比重", "份额", "解释", "来自", "总变化", "抵消", "抵掉", "share")
SENTENCE_END = re.compile(r"[。；;！!\n]")
WINDOW = 24  # 只看数字前面这么多个字符以内的名字：再远就说不清这个名字是不是在说这个数
CLAUSE_BREAK = re.compile(r"[，,；;、（(：:]")  # 而且不跨过逗号、括号、冒号：隔了一个分句，名字说的多半是别的东西
AFTER = 14  # 数字后面这么多个字符以内出现它自己的名字，也算说对了
MIN_DIGITS = 3  # 有效数字不到 3 位的数不查
CODE_SPAN = re.compile(r"`[^`\n]*`")
GENERIC_TOKENS = {"base", "current", "change", "pct", "prev", "last"}  # 字段名里不改变含义的部分：base_orders 仍然是订单数
# 这份数据里国家是英文写的，答案常写中文：两种写法算同一个名字
ALIASES = {"United Kingdom": ("英国", "UK"), "EIRE": ("爱尔兰",), "Netherlands": ("荷兰",), "France": ("法国",),
           "Germany": ("德国",), "Spain": ("西班牙",), "Denmark": ("丹麦",), "Australia": ("澳洲", "澳大利亚"),
           "Sweden": ("瑞典",), "Belgium": ("比利时",), "Switzerland": ("瑞士",), "Italy": ("意大利",), "Japan": ("日本",),
           "Portugal": ("葡萄牙",), "Norway": ("挪威",), "Cyprus": ("塞浦路斯",), "Austria": ("奥地利",),
           "Channel Islands": ("海峡群岛",), "United Arab Emirates": ("阿联酋", "UAE"), "Greece": ("希腊",),
           "Finland": ("芬兰",), "Poland": ("波兰",), "Singapore": ("新加坡",), "USA": ("美国",), "Israel": ("以色列",)}


@dataclass(frozen=True)
class Occurrence:
    """一个数在工具结果里的一处出处。names 为空，表示这一处没有名字可以对照。"""

    value: float
    call: int  # 第几次工具调用；0 表示提示词或用户的问题
    tool: str
    field: str
    names: frozenset[Name] = frozenset()


@dataclass(frozen=True)
class Index:
    occurrences: list[Occurrence]
    variants: dict[Name, tuple[str, ...]]  # 每个名字的各种写法：本身、别名、代号、括号内外的部分


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _usable(name: str) -> bool:
    return len(name) >= 2 and not name.replace(".", "").replace(" ", "").isdigit()  # 纯数字的编号不当名字


def _text(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    return value.strip() if isinstance(value, str) and _usable(value.strip()) else None


def _row_names(row: dict[str, Any]) -> frozenset[Name]:
    """一行的名字。有指标代号的行，label 是指标名；有公司代号的行，name 是公司名；segment、description 是分组名。"""
    names: set[Name] = set()
    if any(isinstance(row.get(k), str) for k in METRIC_IDS) and _text(row, "label"):
        names.add(("metric", _text(row, "label")))
    if isinstance(row.get("company"), str) and _text(row, "name"):
        names.add(("company", _text(row, "name")))
    names |= {("segment", _text(row, k)) for k in ("segment", "description") if _text(row, k)}
    return frozenset(names)


def _subject(content: dict[str, Any]) -> frozenset[Name]:
    """整张表是谁的：下钻的是哪个指标（metric_label），财报是哪家公司。"""
    names = set(_row_names({k: v for k, v in content.items() if k in ("company", "name")}))
    if _text(content, "metric_label"):
        names.add(("metric", _text(content, "metric_label")))
    return frozenset(names)


def _glossary(results: list[dict[str, Any]]) -> dict[str, Name]:
    """代号 → 名字，从同时带代号和名字的行里收集（{"ratio": "net_margin", "label": "归母净利率"}）。"""
    found: dict[str, Name] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in METRIC_IDS:
                if isinstance(node.get(key), str) and _text(node, "label"):
                    found.setdefault(node[key], ("metric", _text(node, "label")))
            if isinstance(node.get("company"), str) and _text(node, "name"):
                found.setdefault(node["company"], ("company", _text(node, "name")))
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(results)
    return found


def _text_numbers(text: str) -> list[float]:
    for pattern in IGNORED:  # 日期、周、编号里的数字不是数（“2011-01-18”里的 18）
        text = pattern.sub(" ", text)
    values = []
    for match in NUMBER.finditer(text):
        number = abs(float(match.group(1).replace(",", "").replace("−", "-")))
        values += [number] + ([number * SCALES[match.group(2)]] if match.group(2) else [])
    return values


def _field_names(field: str, glossary: dict[str, Name]) -> frozenset[Name]:
    """字段名里带的含义：net_margin → 归母净利率；base_orders 去掉 base 是 orders → 订单数。

    只去掉不改变含义的部分：gmv_missing_customer_share 不是 GMV，不给名字。
    """
    core = "_".join(t for t in field.split("_") if t not in GENERIC_TOKENS)
    return frozenset({glossary[core]}) if core in glossary and glossary[core][0] == "metric" else frozenset()


def build_index(messages: list[dict[str, Any]]) -> Index:
    """对话里每个可以当出处的数，连同它的名字。收集范围和数字核查的出处池一致。"""
    results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
    glossary = _glossary(results)
    found: list[Occurrence] = []

    def walk(node: Any, call: int, tool: str, field: str, names: frozenset[Name], in_row: bool) -> None:
        if _is_number(node):
            found.append(Occurrence(abs(float(node)), call, tool, field, names | _field_names(field, glossary)))
        elif isinstance(node, str):
            # 文字里的数只有在一行之内才算有名字（如“4,318.46 亿元”）；警告、说明里的数（“少于 30”）不属于任何一行
            found.extend(Occurrence(v, call, tool, field, names if in_row else frozenset()) for v in _text_numbers(node))
        elif isinstance(node, list):
            for item in node:
                row = _row_names(item) if isinstance(item, dict) else frozenset()
                walk(item, call, tool, field, names | row, in_row or bool(row))  # 表里的一行：加上这一行自己的名字
        elif isinstance(node, dict):
            for key, item in node.items():
                walk(item, call, tool, key, names, in_row)

    asked, call = False, 0
    for message in messages:
        if message["role"] == "system" or (message["role"] == "user" and not asked):
            asked = asked or message["role"] == "user"
            found.extend(Occurrence(v, 0, message["role"], "") for v in _text_numbers(message["content"]))
        elif message["role"] == "tool":
            call += 1
            content = json.loads(message["content"])
            if content.get("tool") == "calculate":  # 算出来的数没有名字：和出处池一样按几种单位各存一份
                if _is_number(content.get("result")):
                    result = abs(float(content["result"]))
                    found.extend(Occurrence(v, call, "calculate", "result") for v in (result, result / 100, result * 1e4, result * 1e8))
                continue
            subject = _subject(content)
            for key, item in content.items():
                # 最外层零散的数不带名字；嵌在里面的（各行、合计）带上“整张表是谁的”
                walk(item, call, str(content.get("tool")), key, subject if isinstance(item, (dict, list)) else frozenset(), False)
    codes: dict[Name, list[str]] = {}
    for code, name in glossary.items():
        codes.setdefault(name, []).append(code)
    names = {n for o in found for n in o.names} | set(glossary.values())
    return Index(found, {n: _variants(n[1], codes.get(n, [])) for n in names})


def _variants(name: str, codes: list[str]) -> tuple[str, ...]:
    """一个名字的各种写法：“拼多多（PDD Holdings）”也认“拼多多”和“PDD Holdings”；国家认中文；代号也认。"""
    parts = [p.strip() for p in re.split(r"[（）()]", name) if _usable(p.strip())]
    out = [name, *ALIASES.get(name, ()), *(p for p in parts if p != name), *(c for c in codes if len(c) >= 3)]
    return tuple(dict.fromkeys(out))


def _is_cjk(ch: str) -> bool:
    return 0x4E00 <= ord(ch) <= 0x9FFF


def _pieces(name: str) -> set[str]:
    """名字里能认出它来的片段：含汉字的至少 2 个字，纯字母数字的至少 3 个字符。"""
    out = set()
    for i in range(len(name)):
        for j in range(i + 2, len(name) + 1):
            piece = name[i:j].strip()
            enough = 2 if any(_is_cjk(ch) for ch in piece) else 3
            if len(piece) >= enough and any(ch.isalnum() for ch in piece) and not piece.isdigit():
                out.add(piece)
    return out


def nearest_name(before: str, own: frozenset[Name], index: Index, kind: str) -> tuple[str, bool] | None:
    """数字前面这一小段里，这一类名字中离数字最近的一个，以及它是不是这个数自己的。没有认得出的名字时返回 None。

    两个名字重叠时取长的（“日均 GMV”盖住“GMV”）；一样长时算自己的（“客单价”既是别的指标的全名，也是“可识别客户客单价”的一部分）。
    """
    lowered = before.lower()
    matches: list[tuple[int, int, bool, str]] = []  # （结束位置，长度，是不是自己的，名字）
    for (name_kind, name), variants in index.variants.items():
        if name_kind != kind:
            continue
        mine = (name_kind, name) in own
        candidates = set(variants) | (set().union(*(_pieces(v) for v in variants)) if mine else set())
        for text in candidates:
            at = lowered.rfind(text.lower())
            if at >= 0:
                matches.append((at + len(text), len(text), mine, name))
    covered = [m for m in matches if any(o[0] - o[1] <= m[0] - m[1] and m[0] <= o[0] and o[1] > m[1] for o in matches)]
    kept = [m for m in matches if m not in covered]
    if not kept:
        return None
    _end, _length, mine, name = max(kept, key=lambda m: (m[0], m[1], m[2]))
    return name, mine


def _sentences(text: str) -> list[tuple[int, int]]:
    spans, pos = [], 0
    for found in SENTENCE_END.finditer(text):
        spans.append((pos, found.start()))
        pos = found.end()
    return spans + [(pos, len(text))]


def _table_header(answer: str, start: int) -> str:
    lines = answer[:start].split("\n")
    header = ""
    for line in reversed(lines[:-1]):
        if not line.lstrip().startswith("|"):
            break
        header = line
    return header


def _line(answer: str, start: int, end: int) -> str:
    """这个数所在的那一行。占比写在括号里（“-2,889.31（-1.66%）”）时，同一行没说它是占比，读的人就会当成变化率。"""
    return answer[answer.rfind("\n", 0, start) + 1:end]


def _significant_digits(text: str) -> int:
    digits = "".join(ch for ch in text if ch.isdigit()).lstrip("0")
    return len(digits)


def _mentions_own(text: str, own: frozenset[Name], index: Index, kind: str) -> bool:
    lowered = text.lower()
    return any(piece.lower() in lowered for name in own if name[0] == kind
               for variant in index.variants[name] for piece in {variant, *_pieces(variant)})


def check_attribution(answer: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """核对答案里每个数的归属。flags 里每一条是一个可能安错了对象（或可能被读错含义）的数。"""
    index = build_index(messages)
    sentences = _sentences(answer)
    prose = CODE_SPAN.sub(lambda m: " " * len(m.group()), answer)  # 找名字时不看反引号里的文字；位置不变
    mentions = extract_numbers(answer)[0]
    flags, checked = [], 0
    for at, mention in enumerate(mentions):
        occurrences = [o for o in index.occurrences if abs(o.value - mention.value) < mention.tolerance - 1e-12]
        if not occurrences or not all(o.names for o in occurrences):
            continue  # 找不到出处的数由数字核查负责；有一处出处没有名字的不查
        checked += 1
        s, e = next((s, e) for s, e in sentences if s <= mention.start <= e)
        sentence = answer[s:e]
        in_table = sentence.lstrip().startswith("|")
        own = frozenset().union(*(o.names for o in occurrences))
        sources = sorted({(o.call, o.tool, o.field) for o in occurrences})
        base = {"text": mention.text, "start": mention.start, "end": mention.end, "sentence": sentence.strip(),
                "belongs_to": sorted(name for _, name in own), "sources": [{"call": c, "tool": t, "field": f} for c, t, f in sources]}
        share = all(o.field in SHARE_FIELDS for o in occurrences)
        previous = mentions[at - 1].end if at and mentions[at - 1].end > s else s  # 往前只找到上一个数字为止
        following = mentions[at + 1].start if at + 1 < len(mentions) and mentions[at + 1].start < e else e
        before = CLAUSE_BREAK.split(prose[max(previous, mention.start - WINDOW):mention.start])[-1]
        after = prose[mention.end:min(following, mention.end + AFTER)]
        wrong = []
        # 只核对每一处出处都有名字的那几类：31 既是新客数、又碰巧是某个商品的订单数时，它不一定是“某个商品的”
        kinds = set.intersection(*({k for k, _ in o.names} for o in occurrences))
        maybe_share = any(o.field in SHARE_FIELDS for o in occurrences)  # 有一处出处是占比，就可能是在说占比
        skip = in_table or maybe_share or _significant_digits(mention.text) < MIN_DIGITS
        for kind in ([] if skip else sorted(kinds)):
            found = nearest_name(before, own, index, kind)
            if found and not found[1] and not _mentions_own(after, own, index, kind):
                wrong.append(found[0])
        if wrong:
            flags.append(base | {"kind": "对象", "sentence_says": "、".join(wrong)})
            continue
        context = f"{_table_header(answer, mention.start)} {sentence}" if in_table else _line(answer, s, e)
        if share and not any(w in context for w in SHARE_WORDS):
            flags.append(base | {"kind": "含义", "sentence_says": "",
                                 "meaning": "这是占总变化的比重，不是这一项自己的变化率；这一行没有说明"})
    return {"checked": checked, "flags": flags}
